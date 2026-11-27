"""Part 12: the tamper-evident audit chain (hash vectors, atomicity, concurrency, append-only, tamper detection)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.audit import chain as audit
from app.audit import checkpoints
from app.audit.canonical import GENESIS_PREV, canonical_json, record_hash
from app.audit.keys import generate
from app.audit.verify import verify_chain
from app.config import get_settings
from app.llm.fake_client import FakeLLMClient
from app.models.audit import AuditChainHead, AuditLog
from tests.engine_helpers import started_workflow, task


# ------------------------------------------------------------------------------ the algorithm (documented vectors)
def test_canonical_json_and_hash_vector() -> None:
    rec = {"chain_id": "system", "seq": 1, "at": datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC), "team_id": None,
           "actor_type": "system", "actor_id": "engine", "auth_method": None, "ip": None, "action": "x.y",
           "object_type": "workflow", "object_id": "w1", "decision": None, "payload": {"b": 1, "a": "é"},
           "payload_digest": "d", "prev_hash": GENESIS_PREV}
    assert canonical_json({"b": 1, "a": [1.5, "é"]}) == '{"a":[1.5,"é"],"b":1}'
    body = canonical_json({k: rec[k] for k in sorted(rec)})
    assert '"at":"2026-01-02T03:04:05.000006Z"' in body
    # documented vector (docs/ARCHITECTURE.md "Audit hash algorithm"); a change here breaks external verifiers
    assert record_hash(rec, GENESIS_PREV) == "a4b619b9023ad71ea3c11879c67bcdc979b4adc03baa959ee6a65c3920bac2cf"


async def _verify(sf, chain: str = "system"):  # noqa: ANN001, ANN202
    async with sf() as s:
        return await verify_chain(s, chain, get_settings().audit_public_keys_dirs)


async def test_a_workflow_run_is_in_a_valid_chain_with_genesis(client: AsyncClient, make_engine, session_factory) -> None:  # noqa: ANN001
    wf = await started_workflow(client, [task("a"), task("b", "writer", ["a"])])
    await make_engine(FakeLLMClient()).run_until_idle()
    async with session_factory() as s:
        rows = list(await s.scalars(select(AuditLog).where(AuditLog.chain_id == "system").order_by(AuditLog.seq)))
        n_events = await s.scalar(text("SELECT count(*) FROM workflow_events WHERE workflow_id = :w"), {"w": wf})
    assert rows[0].action == "chain.genesis" and rows[0].prev_hash == GENESIS_PREV
    assert len(rows) - 1 == n_events  # one audit record per event
    assert [r.seq for r in rows] == list(range(len(rows)))  # gapless
    assert (await _verify(session_factory)).ok


async def test_a_rolled_back_change_leaves_no_audit_record(session_factory) -> None:  # noqa: ANN001
    from app.core.events import record_event
    from app.models import Workflow

    async with session_factory() as s:
        wf = Workflow(id=uuid.uuid4(), name="w", goal="g")
        s.add(wf)
        record_event(s, workflow_id=wf.id, event_type="workflow.created", payload={})
        await s.flush()
        await s.rollback()
    async with session_factory() as s:
        assert await s.scalar(select(AuditLog).where(AuditLog.action == "workflow.created")) is None


async def test_an_audit_failure_rolls_back_the_state_change(session_factory, monkeypatch) -> None:  # noqa: ANN001
    from app.core.events import record_event
    from app.models import Workflow

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "_append_one", boom)
    wid = uuid.uuid4()
    async with session_factory() as s:
        s.add(Workflow(id=wid, name="w", goal="g"))
        record_event(s, workflow_id=wid, event_type="workflow.created", payload={})
        with pytest.raises(RuntimeError):
            await s.commit()
    async with session_factory() as s:
        assert await s.get(Workflow, wid) is None


async def test_parallel_appends_never_fork(session_factory) -> None:  # noqa: ANN001
    async def writer(i: int) -> None:
        for j in range(15):
            async with session_factory() as s:
                await audit.append_now(s, action="test.append", object_type="t", object_id=f"{i}-{j}")
                await s.commit()

    await asyncio.gather(*(writer(i) for i in range(4)))
    async with session_factory() as s:
        seqs = list(await s.scalars(select(AuditLog.seq).where(AuditLog.chain_id == "system").order_by(AuditLog.seq)))
    assert seqs == list(range(61))  # genesis + 60, gapless, no duplicates
    assert (await _verify(session_factory)).ok


async def test_the_trigger_blocks_update_and_delete(session_factory) -> None:  # noqa: ANN001
    async with session_factory() as s:
        await audit.append_now(s, action="test.x")
        await s.commit()
    for sql in ("UPDATE audit_log SET action = 'evil'", "DELETE FROM audit_log"):
        async with session_factory() as s:
            with pytest.raises(Exception, match="append-only"):
                await s.execute(text(sql))
            await s.rollback()


async def test_the_restricted_role_cannot_update(session_factory) -> None:  # noqa: ANN001
    import asyncpg

    from scripts.setup_db_roles import setup

    async with session_factory() as s:
        await audit.append_now(s, action="test.x")
        await s.commit()
        db = await s.scalar(text("SELECT current_database()"))
    await setup(database=db, password="app-test")
    url = get_settings().database_url.replace("+asyncpg", "").rsplit("/", 1)[0] + "/" + db
    url = url.replace(url.split("//")[1].split("@")[0], "agentflow_app:app-test")
    conn = await asyncpg.connect(url)
    try:
        assert await conn.fetchval("SELECT count(*) FROM audit_log") >= 1  # SELECT allowed
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE audit_log SET action = 'x'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("TRUNCATE audit_log")
    finally:
        await conn.close()


# ------------------------------------------------------------------------------ tamper detection
async def _chain_of(session_factory, n: int = 6) -> None:  # noqa: ANN001
    for i in range(n):
        async with session_factory() as s:
            await audit.append_now(s, action="test.append", object_id=str(i), payload={"i": i})
            await s.commit()


async def _superuser(session_factory, sql: str, **params) -> None:  # noqa: ANN001, ANN003
    """What a malicious DB admin can do: switch the trigger off, change rows, switch it back on."""
    async with session_factory() as s:
        await s.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
        await s.execute(text(sql), params)
        await s.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only"))
        await s.commit()


@pytest.fixture
async def restore_unique(session_factory):  # noqa: ANN001, ANN201
    """Tamper tests drop the (chain_id, seq) constraint like a malicious admin would; put the schema back after."""
    yield
    async with session_factory() as s:
        await s.execute(text("TRUNCATE audit_log, audit_chain_heads, audit_checkpoints"))
        await s.execute(text("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS uq_audit_log_chain_seq"))
        await s.execute(text("ALTER TABLE audit_log ADD CONSTRAINT uq_audit_log_chain_seq UNIQUE (chain_id, seq)"))
        await s.commit()


@pytest.mark.parametrize(("sql", "kind", "seq"), [
    ("UPDATE audit_log SET payload = '{\"i\": 99}' WHERE chain_id = 'system' AND seq = 3", "modified", 3),
    ("DELETE FROM audit_log WHERE chain_id = 'system' AND seq = 4", "deleted", 4),
    ("UPDATE audit_log SET seq = CASE seq WHEN 2 THEN 3 WHEN 3 THEN 2 END WHERE chain_id = 'system' AND seq IN (2, 3)",
     "reordered", 2),
])
async def test_tampering_is_located(session_factory, restore_unique, sql: str, kind: str, seq: int) -> None:  # noqa: ANN001
    await _chain_of(session_factory)
    await _superuser(session_factory, "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS uq_audit_log_chain_seq")
    await _superuser(session_factory, sql)
    r = await _verify(session_factory)
    assert (r.ok, r.kind, r.first_broken_seq) == (False, kind, seq), r


async def test_an_inserted_record_is_detected(session_factory, restore_unique) -> None:  # noqa: ANN001
    await _chain_of(session_factory)
    async with session_factory() as s:
        rows = list(await s.scalars(select(AuditLog).where(AuditLog.chain_id == "system").order_by(AuditLog.seq)))
    # shift seq >= 3 up by one and insert a self-consistent forged record at seq 3
    await _superuser(session_factory, "ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS uq_audit_log_chain_seq")
    await _superuser(session_factory, "UPDATE audit_log SET seq = seq + 1 WHERE chain_id = 'system' AND seq >= 3")
    forged = {"chain_id": "system", "seq": 3, "at": rows[2].at, "team_id": None, "actor_type": "user",
              "actor_id": "mallory", "auth_method": None, "ip": None, "action": "approval.approved",
              "object_type": "x", "object_id": "y", "decision": None, "payload": {}, "payload_digest": "z",
              "prev_hash": "f" * 64}
    h = record_hash(forged, forged["prev_hash"])
    await _superuser(session_factory, "INSERT INTO audit_log (chain_id, seq, team_id, at, actor_type, actor_id, action, "
                     "object_type, object_id, payload, payload_digest, prev_hash, hash) VALUES ('system', 3, NULL, :at, "
                     "'user', 'mallory', 'approval.approved', 'x', 'y', '{}', 'z', :p, :h)", at=rows[2].at,
                     p="f" * 64, h=h)
    r = await _verify(session_factory)
    assert (r.ok, r.kind, r.first_broken_seq) == (False, "inserted", 3), r


async def test_checkpoints_sign_and_detect_mismatch_and_bad_signature(session_factory, tmp_path: Path,  # noqa: ANN001
                                                                       monkeypatch) -> None:  # noqa: ANN001
    s0 = get_settings()
    monkeypatch.setattr(s0, "audit_anchor_dir", tmp_path / "anchors")
    await _chain_of(session_factory, 3)
    made = await checkpoints.maybe_checkpoint(session_factory, s0, force=True)
    assert made and (await _verify(session_factory)).ok
    anchors = tmp_path / "anchors" / "system.jsonl"
    assert anchors.exists()
    async with session_factory() as s:
        r = await verify_chain(s, "system", s0.audit_public_keys_dirs, tmp_path / "anchors")
    assert r.ok
    anchors.write_text(anchors.read_text().replace(made[0].head_hash, "0" * 64))  # someone edits the exported copy
    async with session_factory() as s:
        r = await verify_chain(s, "system", s0.audit_public_keys_dirs, tmp_path / "anchors")
    assert (r.ok, r.kind) == (False, "checkpoint_mismatch")
    async with session_factory() as s:
        await s.execute(text("ALTER TABLE audit_checkpoints DISABLE TRIGGER audit_checkpoints_append_only"))
        await s.execute(text("UPDATE audit_checkpoints SET signature = 'AAAA' || substr(signature, 5)"))
        await s.execute(text("ALTER TABLE audit_checkpoints ENABLE TRIGGER audit_checkpoints_append_only"))
        await s.commit()
    r = await _verify(session_factory)
    assert (r.ok, r.kind) == (False, "bad_signature")


async def test_key_rotation_old_checkpoints_still_verify(session_factory, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    from app.audit import keys

    s0 = get_settings()
    monkeypatch.setattr(s0, "audit_anchor_dir", None)
    pub = tmp_path / "pub"
    monkeypatch.setattr(s0, "audit_public_keys_dirs", [pub])
    old = generate(tmp_path / "k1.pem", pub)
    monkeypatch.setattr(s0, "audit_signing_key_path", tmp_path / "k1.pem")
    keys._load.cache_clear()
    await _chain_of(session_factory, 2)
    await checkpoints.maybe_checkpoint(session_factory, s0, force=True)
    new = generate(tmp_path / "k2.pem", pub)  # rotate
    monkeypatch.setattr(s0, "audit_signing_key_path", tmp_path / "k2.pem")
    keys._load.cache_clear()
    await _chain_of(session_factory, 2)
    await checkpoints.maybe_checkpoint(session_factory, s0, force=True)
    keys._load.cache_clear()
    async with session_factory() as s:
        kids = list(await s.scalars(text("SELECT key_id FROM audit_checkpoints ORDER BY seq")))
        r = await verify_chain(s, "system", [pub])
    assert kids == [old, new] and r.ok


async def test_audit_append_overhead_is_small(session_factory) -> None:  # noqa: ANN001
    import time

    n = 50
    t0 = time.perf_counter()
    for i in range(n):
        async with session_factory() as s:
            await audit.append_now(s, action="perf", object_id=str(i))
            await s.commit()
    per = (time.perf_counter() - t0) / n * 1000
    print(f"\naudit append: {per:.2f} ms per record (own transaction, incl. commit)")
    assert per < 250  # LLM calls take seconds; this is the worst case (a transaction per record)


async def test_heads_match_the_last_record(session_factory) -> None:  # noqa: ANN001
    await _chain_of(session_factory, 3)
    async with session_factory() as s:
        head = await s.get(AuditChainHead, "system")
        last = await s.scalar(select(AuditLog).where(AuditLog.chain_id == "system").order_by(AuditLog.seq.desc()))
    assert (head.seq, head.head_hash) == (last.seq, last.hash)
