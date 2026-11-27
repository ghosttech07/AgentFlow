"""Part 12: PII detection + protection + PII-aware routing, manifests, encryption + shredding, erasure, evidence."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.config import get_settings
from app.llm.circuit import CircuitBreaker
from app.llm.fake_client import FakeLLMClient
from app.llm.pricing import Pricing
from app.llm.providers.fake_provider import FakeProvider
from app.llm.router import ModelRouter
from app.llm.routes import RouteConfig
from app.models.privacy import PiiToken, StoredContent, WorkflowKey
from app.privacy.detectors import detect, luhn_ok, verhoeff_check_digit, verhoeff_ok
from tests.engine_helpers import detail, started_workflow, task

# synthetic values that pass the checksums (FAKE, for tests only)
AADHAAR = "2345 6789 012" + verhoeff_check_digit("23456789012")
CARD = "4111 1111 1111 1111"
PAN = "ABCPK1234F"
EMAIL = "ravi.kumar@example.com"
PHONE = "+91 98765 43210"


# ------------------------------------------------------------------------------ detectors
@pytest.mark.parametrize(("text", "kind"), [
    (f"mail {EMAIL} now", "EMAIL"), (f"call {PHONE}", "PHONE"), (f"aadhaar {AADHAAR}", "AADHAAR"),
    (f"pan {PAN}", "PAN"), (f"card {CARD}", "CARD"), ("ifsc HDFC0001234", "IFSC"), ("ip 192.168.1.20", "IP"),
    ("ip 2001:db8::1", "IP"), ("key AIza" + "A" * 35, "SECRET"), ("token=ghp_" + "a1B2" * 9, "SECRET"),
    ("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", "SECRET"),
    ("api_key: Zx8vQ2mN4pL7sT1wK9rB3yH6", "SECRET"), ("passport K1234567", "PASSPORT"),
])
def test_detectors_find_true_positives(text: str, kind: str) -> None:
    assert kind in {f.type for f in detect(text)}


@pytest.mark.parametrize("text", [
    "order 1234 5678 9012",            # 12 digits that fail Verhoeff (and start-digit rules)
    "random 0234 5678 9012",           # starts with 0: never an Aadhaar
    "card 4111 1111 1111 1112",        # fails Luhn
    "pan ABCXK1234F",                  # X is not a valid PAN entity code
    "version 1.2.3.400",               # not an IP (octet > 255)
    "api_key: aaaaaaaaaaaaaaaaaaaa",   # low entropy
])
def test_detectors_reject_false_positives(text: str) -> None:
    assert not {"AADHAAR", "CARD", "PAN", "IP", "SECRET"} & {f.type for f in detect(text)}


def test_checksums() -> None:
    assert verhoeff_ok(AADHAAR.replace(" ", "")) and not verhoeff_ok("234567890120" if AADHAAR[-1] != "0" else "234567890121")
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")


# ------------------------------------------------------------------------------ router protection + routing
def router(sf, local: bool = True):  # noqa: ANN001, ANN201
    ext, loc = FakeLLMClient(), FakeLLMClient()
    providers = {"fake": FakeProvider(ext)}
    entries = ["fake:external-model"]
    if local:
        providers["ollama"] = FakeProvider(loc)
        entries.append("ollama:local-model")
    r = ModelRouter(providers=providers, routes=RouteConfig(routes={"default": entries}), pricing=Pricing({}),
                    settings=get_settings(), session_factory=sf, breaker=CircuitBreaker(3, 60))
    return r, ext, loc


async def test_external_provider_gets_tokens_not_values(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    r, ext, loc = router(session_factory)
    wf = await started_workflow(client, [task("a", input={"customer": f"Ravi Kumar, {EMAIL}, {PHONE}"})])
    await make_engine(r).run_until_idle()
    prompts = " ".join(c["user_prompt"] for c in ext.calls)
    assert ext.calls and EMAIL not in prompts and "98765" not in prompts
    assert "<EMAIL_1>" in prompts and "<PHONE_1>" in prompts
    async with session_factory() as s:
        tokens = list(await s.scalars(select(PiiToken).where(PiiToken.workflow_id == uuid.UUID(wf))))
        raw = (await s.execute(text("SELECT string_agg(encode(ciphertext, 'escape'), '') FROM pii_tokens"))).scalar()
    assert {t.token for t in tokens} == {"<EMAIL_1>", "<PHONE_1>"} and EMAIL not in (raw or "")  # encrypted at rest
    ev = [e for e in (await client.get(f"/workflows/{wf}/events")).json() if e["event_type"] == "privacy.protected"]
    assert ev and "email tokenized" in ev[0]["payload"]["summary"] and EMAIL not in json.dumps(ev)


async def test_aadhaar_goes_to_the_local_model_only(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    r, ext, loc = router(session_factory)
    wf = await started_workflow(client, [task("a", input={"id": f"Aadhaar {AADHAAR}"})])
    await make_engine(r).run_until_idle()
    assert not ext.calls and loc.calls  # routed away from the external entry
    assert (await detail(client, wf))["status"] == "COMPLETED"
    types = [e["event_type"] for e in (await client.get(f"/workflows/{wf}/events")).json()]
    assert "privacy.routed_away" in types


async def test_no_local_model_blocks_with_a_clear_reason(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    r, ext, _ = router(session_factory, local=False)
    wf = await started_workflow(client, [task("a", input={"pan": PAN}, max_attempts=1)])
    await make_engine(r).run_until_idle()
    t = (await client.get(f"/workflows/{wf}/tasks/a")).json()
    assert not ext.calls and t["error_type"] == "privacy_blocked" and "PAN" in t["error"]


async def test_secrets_never_reach_an_external_provider(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    r, ext, loc = router(session_factory)
    wf = await started_workflow(client, [task("a", input={"note": "use key AIza" + "Q" * 35})])
    await make_engine(r).run_until_idle()
    assert not ext.calls and loc.calls
    types = [e["event_type"] for e in (await client.get(f"/workflows/{wf}/events")).json()]
    assert "privacy.secret_blocked" in types


async def test_logs_contain_no_pii_or_secrets(caplog) -> None:  # noqa: ANN001
    import logging

    from app.core.logging import install_redaction

    install_redaction()
    logging.getLogger("t").warning("user %s key %s", EMAIL, "AIza" + "Z" * 35)
    text_ = caplog.text
    assert EMAIL not in text_ and "AIza" not in text_ and "[EMAIL]" in text_


# ------------------------------------------------------------------------------ manifests + encryption
async def test_manifest_is_complete_and_in_the_chain(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    r, _, _ = router(session_factory)
    wf = await started_workflow(client, [task("a"), task("b", "writer", ["a"])])
    await make_engine(r).run_until_idle()
    m = (await client.get(f"/workflows/{wf}/manifest")).json()
    assert m["repro"]["code_version"] and "tools_yaml" in m["repro"]
    call = m["llm_calls"][0]["manifest"]
    for k in ("prompt_template_hash", "rendered_prompt_hash", "response_hash", "parameters", "usage", "route_position"):
        assert k in call
    for t in m["tasks"]:
        att = t["attempts"][-1]
        assert att["manifest"]["output_hash"] and t["audit"]["manifest_digest"] == att["manifest_digest"]
    b = next(t for t in m["tasks"] if t["key"] == "b")
    assert "a" in b["attempts"][-1]["manifest"]["dependency_output_hashes"]


async def test_a_prompt_change_gives_a_new_hash(monkeypatch) -> None:  # noqa: ANN001
    from app.agents.researcher import ResearcherAgent
    from app.audit.canonical import sha256_hex
    from app.audit.manifest import template_of

    before = sha256_hex(template_of("researcher"))
    monkeypatch.setattr(ResearcherAgent, "system_prompt", ResearcherAgent.system_prompt + " Be brief.")
    assert sha256_hex(template_of("researcher")) != before


async def test_content_is_encrypted_at_rest_and_shredding_makes_it_unreadable(client: AsyncClient, make_engine,  # noqa: ANN001
                                                                               session_factory) -> None:  # noqa: ANN001
    from app.audit.verify import verify_chain

    r, _, _ = router(session_factory)
    wf = await started_workflow(client, [task("a", input={"secret_word": "PLAINTEXTMARKER"})])
    await make_engine(r).run_until_idle()
    async with session_factory() as s:
        raw = (await s.execute(text("SELECT string_agg(encode(ciphertext, 'escape'), '') FROM stored_contents"))).scalar()
        assert raw and "PLAINTEXTMARKER" not in raw
    m = (await client.get(f"/workflows/{wf}/manifest?include_content=true")).json()
    assert any("PLAINTEXTMARKER" in c["text"] for c in m["stored_content"])  # readable with the key
    async with session_factory() as s:
        await s.execute(text("DELETE FROM workflow_keys WHERE workflow_id = :w"), {"w": wf})
        await s.commit()
    m = (await client.get(f"/workflows/{wf}/manifest?include_content=true")).json()
    assert isinstance(m["stored_content"], str) and m["stored_content"].startswith("shredded")
    async with session_factory() as s:
        assert (await verify_chain(s, "system", get_settings().audit_public_keys_dirs)).ok  # chain unaffected


# ------------------------------------------------------------------------------ erasure + retention
async def test_erasure_needs_two_approvers_then_shreds(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    from app.audit.verify import verify_chain

    r, _, _ = router(session_factory)
    wf = await started_workflow(client, [task("a", input={"customer": f"Ravi {EMAIL}"})])
    await make_engine(r).run_until_idle()
    req = (await client.post("/privacy/erasure", json={"subjects": [EMAIL], "reason": "GDPR request",
                                                       "actor": "dana"})).json()
    assert req["status"] == "pending" and EMAIL not in json.dumps(req)
    url = f"/privacy/erasure/{req['id']}/approve"
    assert (await client.post(url, json={"actor": "dana"})).status_code == 403  # the requester can't vote
    assert (await client.post(url, json={"actor": "erin"})).json()["status"] == "pending"
    assert (await client.post(url, json={"actor": "erin"})).status_code == 409
    done = (await client.post(url, json={"actor": "frank"})).json()
    assert done["status"] == "completed" and wf in done["report"]["affected_workflows"]
    assert done["report"]["workflows_shredded"] == 1
    async with session_factory() as s:
        assert await s.get(WorkflowKey, uuid.UUID(wf)) is None
        assert not list(await s.scalars(select(PiiToken).where(PiiToken.workflow_id == uuid.UUID(wf))))
        assert (await verify_chain(s, "system", get_settings().audit_public_keys_dirs)).ok
    t = (await client.get(f"/workflows/{wf}/tasks/a")).json()
    assert EMAIL not in json.dumps(t)
    types = [e["event_type"] for e in (await client.get(f"/workflows/{wf}/events")).json()]
    assert EMAIL not in json.dumps((await client.get(f"/workflows/{wf}/events")).json()) and types


async def test_retention_deletes_per_category_and_audits(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    r, _, _ = router(session_factory)
    await started_workflow(client, [task("a")])
    await make_engine(r).run_until_idle()
    async with session_factory() as s:
        await s.execute(text("UPDATE stored_contents SET created_at = now() - interval '40 days'"))
        await s.commit()
    assert (await client.put("/privacy/retention", json={"days": {"stored_content": 30}})).status_code == 200
    out = (await client.post("/privacy/retention/apply")).json()
    assert out["deleted"]["stored_content"] >= 2
    async with session_factory() as s:
        assert not list(await s.scalars(select(StoredContent)))
    audit = (await client.get("/audit?action=retention.*")).json()["items"]
    assert any(a["action"] == "retention.applied" for a in audit)


# ------------------------------------------------------------------------------ evidence
async def _bundle(client: AsyncClient, make_engine, session_factory, tmp_path: Path) -> tuple[str, Path]:  # noqa: ANN001
    r, _, _ = router(session_factory)
    wf = await started_workflow(client, [task("a", input={"c": EMAIL}), task("b", "writer", ["a"])])
    await make_engine(r).run_until_idle()
    resp = await client.get(f"/workflows/{wf}/evidence?format=zip")
    assert resp.status_code == 200
    path = tmp_path / "bundle.zip"
    path.write_bytes(resp.content)
    return wf, path


async def test_evidence_bundle_verifies_offline(client: AsyncClient, make_engine, session_factory, tmp_path) -> None:  # noqa: ANN001
    from scripts.verify_evidence import verify

    wf, path = await _bundle(client, make_engine, session_factory, tmp_path)
    names = set(zipfile.ZipFile(path).namelist())
    assert {"report.pdf", "evidence.json", "audit_segment.jsonl", "SHA256SUMS", "bundle.sig", "VERIFY.md"} <= names
    assert zipfile.ZipFile(path).read("report.pdf").startswith(b"%PDF")
    assert EMAIL not in zipfile.ZipFile(path).read("evidence.json").decode()
    ok, lines = verify(path, get_settings().audit_public_keys_dirs)
    assert ok, lines
    audit = (await client.get(f"/audit?action=audit.export&object_id={wf}")).json()["items"]
    assert audit  # the export itself is audited


@pytest.mark.parametrize("victim", ["evidence.json", "audit_segment.jsonl", "report.pdf"])
async def test_a_tampered_bundle_fails_naming_the_file(client: AsyncClient, make_engine, session_factory,  # noqa: ANN001
                                                       tmp_path, victim: str) -> None:  # noqa: ANN001
    from scripts.verify_evidence import verify

    _, path = await _bundle(client, make_engine, session_factory, tmp_path)
    src = zipfile.ZipFile(path)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            data = src.read(n)
            z.writestr(n, data + b" " if n == victim else data)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(out.getvalue())
    ok, lines = verify(bad, get_settings().audit_public_keys_dirs)
    assert not ok and any(victim in line for line in lines if line.startswith("FAIL")), lines


async def test_evidence_needs_permission(client: AsyncClient) -> None:
    from tests.test_governance import setup_org

    keys = await setup_org(client)
    wf = (await client.post("/workflows", json={"name": "w", "goal": "g"}, headers=keys["olga"])).json()["id"]
    assert (await client.get(f"/workflows/{wf}/evidence", headers=keys["olga"])).status_code == 403  # operator
    assert (await client.get(f"/workflows/{wf}/evidence?format=json", headers=keys["root"])).status_code == 200
