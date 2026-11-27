"""Compliance evidence bundles (Part 12): one workflow, everything an auditor needs, verifiable OFFLINE.

bundle.zip
  report.pdf          human-readable summary (reportlab)
  evidence.json       machine-readable: workflow, plan versions, tasks, tool calls (redacted), approvals + votes +
                      policy rules, policy decisions, PII summary, budget/cost, timeline, manifests, audit refs
  audit_segment.jsonl the audit records of this workflow + the chain context to verify them: the segment runs from
                      the nearest signed checkpoint BEFORE the first record (or genesis) to the nearest checkpoint
                      AFTER the last one; records of other workflows inside the range are stubs (seq, prev_hash,
                      hash only: linkage is checked, their content is not included)
  checkpoints.json    the signed checkpoints covering the segment
  public_keys/        <key_id>.pem for every key used
  VERIFY.md           how to verify (scripts/verify_evidence.py, or by hand)
  SHA256SUMS          sha256 of every file above
  bundle.sig          {"key_id", "signature"}: Ed25519 over the SHA256SUMS bytes
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import chain as audit
from app.audit.canonical import canonical_json, digest, sha256_hex, ts
from app.audit.checkpoints import as_dict, maybe_checkpoint
from app.audit.keys import public_pem, sign, signing_key
from app.audit.verify import row_dict, verify_records
from app.config import Settings, get_settings
from app.db.base import utcnow
from app.models.audit import AuditCheckpoint, AuditLog

VERIFY_MD = """# Verifying this evidence bundle

Offline, with no database and no server:

    python scripts/verify_evidence.py bundle.zip --public-keys <trusted dir, e.g. docs/audit-public-keys>

What it checks:
1. `SHA256SUMS` matches every file; `bundle.sig` is a valid Ed25519 signature over `SHA256SUMS`.
2. `audit_segment.jsonl` is a continuous hash chain: each record's `hash = sha256(canonical_json(record without
   hash) + prev_hash)` (canonical JSON: sorted keys, no whitespace, UTF-8, UTC timestamps `...ffffffZ`) and each
   `prev_hash` equals the previous record's `hash`. Stubs (other workflows) are checked for linkage only.
3. Every checkpoint in `checkpoints.json` is signed (Ed25519 over canonical_json({chain_id, seq, head_hash, at,
   key_id})) and its `head_hash` equals the chain hash at that seq.
4. Every audit reference in `evidence.json` matches a record in the segment.

Trust: use public keys you obtained independently (the repository's docs/audit-public-keys, or the operator's
published keys), not only the copies inside the bundle: a forger could replace both the bundle and its keys.
A checkpoint that was also exported outside the database (anchor file, webhook, git) proves the chain existed in
this state at that time, even against a database administrator.
"""


async def _segment(session: AsyncSession, workflow_id: uuid.UUID) -> tuple[str | None, list[dict[str, Any]],
                                                                         list[dict[str, Any]], str | None]:
    wid = str(workflow_id)
    mine = list(await session.scalars(select(AuditLog).where(or_(
        AuditLog.payload["workflow_id"].astext == wid, AuditLog.object_id == wid)).order_by(AuditLog.seq)))
    if not mine:
        return None, [], [], None
    chain_id = mine[0].chain_id
    first, last = mine[0].seq, mine[-1].seq
    before = await session.scalar(select(AuditCheckpoint).where(AuditCheckpoint.chain_id == chain_id,
                                                                AuditCheckpoint.seq < first)
                                  .order_by(AuditCheckpoint.seq.desc()))
    after = await session.scalar(select(AuditCheckpoint).where(AuditCheckpoint.chain_id == chain_id,
                                                               AuditCheckpoint.seq >= last)
                                 .order_by(AuditCheckpoint.seq))
    lo = before.seq + 1 if before else 0
    hi = after.seq if after else last
    rows = list(await session.scalars(select(AuditLog).where(AuditLog.chain_id == chain_id, AuditLog.seq >= lo,
                                                             AuditLog.seq <= hi).order_by(AuditLog.seq)))
    ids = {r.id for r in mine}
    seg = []
    for r in rows:
        d = row_dict(r)
        if r.id in ids or r.action == "chain.genesis":
            seg.append({**d, "at": ts(r.at)})
        else:
            seg.append({"chain_id": r.chain_id, "seq": r.seq, "prev_hash": r.prev_hash, "hash": r.hash, "stub": True})
    cps = [as_dict(c) for c in await session.scalars(select(AuditCheckpoint).where(
        AuditCheckpoint.chain_id == chain_id, AuditCheckpoint.seq >= (before.seq if before else 0),
        AuditCheckpoint.seq <= hi).order_by(AuditCheckpoint.seq))]
    return chain_id, seg, cps, before.head_hash if before else None


async def build(session_factory: async_sessionmaker[AsyncSession], session: AsyncSession, workflow_id: uuid.UUID,
                settings: Settings | None = None) -> dict[str, Any]:
    """The evidence document (evidence.json) + the segment/checkpoints. Forces a checkpoint first so it is anchored."""
    from app.services import approval_service, budget_service, timeline_service, trust_service, workflow_service

    settings = settings or get_settings()
    await maybe_checkpoint(session_factory, settings, force=True)
    wf = await workflow_service.get_workflow(session, workflow_id)
    chain_id, seg, cps, start_prev = await _segment(session, workflow_id)
    from app.models.tool_call import ToolCall
    from app.privacy.redact import redact_obj
    from app.services.graph_service import list_plan_versions

    tools = list(await session.scalars(select(ToolCall).where(ToolCall.workflow_id == workflow_id)
                                       .order_by(ToolCall.created_at)))
    approvals = await approval_service.list_approvals(session, workflow_id=workflow_id)
    usage = await budget_service.get_usage(session, wf)
    timeline = await timeline_service.get_timeline(session, workflow_id)
    manifest = await trust_service.manifest(session, workflow_id)
    plan_versions = await list_plan_versions(session, workflow_id)
    vres = verify_records(chain_id or "-", seg, cps, settings.audit_public_keys_dirs,
                          start_prev=start_prev) if seg else None
    decisions = [{"seq": r["seq"], "action": r["action"], "decision": r["decision"]} for r in seg
                 if not r.get("stub") and r.get("decision")]
    doc = {
        "format": "agentflow-evidence/1", "generated_at": ts(utcnow()),
        "workflow": {"id": str(wf.id), "name": wf.name, "goal": redact_obj(wf.goal), "status": str(wf.status),
                     "created_at": ts(wf.created_at), "error": redact_obj(wf.error), "repro": wf.repro,
                     "budget": wf.budget, "result_summary": redact_obj((wf.result or {}).get("summary"))},
        "plan_versions": [{"version": v.version, "trigger": v.trigger, "status": v.status, "created_by": v.created_by,
                           "diff": (v.patch or {}).get("diff")} for v in plan_versions],
        "tasks": [{"key": t["key"], "agent_type": t["agent_type"], "status": t["status"], "attempts": t["attempts"],
                   "audit": t["audit"]} for t in manifest["tasks"]],
        "llm_calls": manifest["llm_calls"],
        "tool_calls": [{"tool": c.tool, "status": c.status, "side_effect": c.side_effect,
                        "arguments": redact_obj(c.arguments), "result_digest": digest(c.result),
                        "idempotency_key": c.idempotency_key} for c in tools],
        "approvals": [{"id": str(a.id), "kind": a.kind, "status": a.status, "decided_by": a.decided_by,
                       "required_approvals": a.required_approvals, "approver_role": a.approver_role,
                       "votes": a.votes, "policy_rules": a.policy_rules, "comment": redact_obj(a.comment)}
                      for a in approvals],
        "policy_decisions": decisions,
        "pii": await trust_service.pii_summary(session, workflow_id),
        "cost": {"llm_calls": usage.llm_calls, "tokens": usage.input_tokens + usage.output_tokens,
                 "cost_usd": usage.cost_usd},
        "timeline": [{"at": e.get("at"), "text": redact_obj(e.get("text"))} for e in timeline.get("entries", [])],
        "audit": {"chain_id": chain_id, "records": [{"seq": r["seq"], "hash": r["hash"], "action": r.get("action")}
                                                    for r in seg if not r.get("stub")],
                  "checkpoints": [{"seq": c["seq"], "head_hash": c["head_hash"], "key_id": c["key_id"]} for c in cps],
                  "verification": vres.to_dict() if vres else None},
    }
    return {"doc": doc, "segment": seg, "checkpoints": cps, "start_prev": start_prev}


def _pdf(doc: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    st = getSampleStyleSheet()
    esc = lambda v: str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    w = doc["workflow"]
    story: list[Any] = [Paragraph(f"AgentFlow evidence: {esc(w['name'])}", st["Title"]),
                        Paragraph(f"Workflow {esc(w['id'])} - status {esc(w['status'])} - generated "
                                  f"{esc(doc['generated_at'])}", st["Normal"]),
                        Paragraph(f"Goal: {esc(w['goal'])}", st["Normal"]),
                        Paragraph(f"Code version {esc((w.get('repro') or {}).get('code_version'))}", st["Normal"]),
                        Spacer(1, 10)]

    def table(title: str, header: list[str], rows: list[list[Any]]) -> None:
        story.append(Paragraph(esc(title), st["Heading2"]))
        if not rows:
            story.append(Paragraph("none", st["Normal"]))
            return
        t = Table([header] + [[Paragraph(esc(c), st["BodyText"]) for c in r] for r in rows], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(t)

    calls = {c["task_id"]: c for c in doc["llm_calls"] if c.get("task_id")}
    table("Plan versions", ["version", "trigger", "status", "diff"],
          [[v["version"], v["trigger"], v["status"], "; ".join(v.get("diff") or [])] for v in doc["plan_versions"]])
    table("Tasks", ["task", "agent", "status", "attempts (outcome)"],
          [[t["key"], t["agent_type"], t["status"], ", ".join(f"{a['attempt']}:{a['outcome']}" for a in t["attempts"])]
           for t in doc["tasks"]])
    table("Model calls", ["role", "provider:model", "prompt template", "PII"],
          [[c["role"], f"{c['provider']}:{c['model']}", ((c.get("manifest") or {}).get("prompt_template_hash") or "")[:16],
            json.dumps(((c.get("manifest") or {}).get("pii") or {}).get("actions") or {})] for c in doc["llm_calls"]])
    table("Tool calls (redacted)", ["tool", "status", "side effect", "arguments"],
          [[c["tool"], c["status"], c["side_effect"], json.dumps(c["arguments"])[:300]] for c in doc["tool_calls"]])
    table("Approvals", ["kind", "status", "needed", "votes", "policy rules"],
          [[a["kind"], a["status"], a["required_approvals"], ", ".join(v.get("voter", "") for v in a["votes"] or [])
            or a["decided_by"] or "", ", ".join(a["policy_rules"] or [])] for a in doc["approvals"]])
    table("Policy decisions", ["seq", "action", "decision"],
          [[d["seq"], d["action"], json.dumps(d["decision"])] for d in doc["policy_decisions"]])
    table("PII protection", ["task", "summary"],
          [[k, "; ".join(str(x.get("summary")) for x in v)] for k, v in doc["pii"].items()])
    c = doc["cost"]
    story.append(Paragraph(f"Cost: {c['llm_calls']} model calls, {c['tokens']} tokens, ${c['cost_usd'] or 0:.4f}",
                           st["Normal"]))
    table("Timeline", ["at (s)", "event"], [[e["at"], e["text"]] for e in doc["timeline"][:200]])
    a = doc["audit"]
    v = a.get("verification") or {}
    table("Audit chain segment", ["seq", "action", "hash"], [[r["seq"], r["action"], r["hash"]] for r in a["records"]])
    table("Signed checkpoints", ["seq", "head hash", "key"], [[x["seq"], x["head_hash"], x["key_id"]]
                                                              for x in a["checkpoints"]])
    story.append(Paragraph(f"Verification at export: {'OK' if v.get('ok') else 'FAILED'} "
                           f"(chain {esc(a['chain_id'])}, verified up to seq {v.get('verified_seq')})", st["Heading2"]))
    SimpleDocTemplate(buf, pagesize=A4, title="AgentFlow evidence").build(story)
    return buf.getvalue()


async def bundle_zip(session_factory: async_sessionmaker[AsyncSession], session: AsyncSession,
                     workflow_id: uuid.UUID, settings: Settings | None = None) -> bytes:
    settings = settings or get_settings()
    built = await build(session_factory, session, workflow_id, settings)
    doc, seg, cps = built["doc"], built["segment"], built["checkpoints"]
    files: dict[str, bytes] = {
        "evidence.json": json.dumps(doc, indent=2, ensure_ascii=False, default=str).encode(),
        "audit_segment.jsonl": "\n".join(canonical_json(r) for r in seg).encode() + b"\n",
        "checkpoints.json": json.dumps({"checkpoints": cps, "start_prev": built["start_prev"]}, indent=2).encode(),
        "report.pdf": _pdf(doc),
        "VERIFY.md": VERIFY_MD.encode(),
    }
    from app.audit.keys import load_public

    _, kid = signing_key()
    for k in {c["key_id"] for c in cps} | {kid}:
        pub = load_public(k, settings.audit_public_keys_dirs)
        if pub is not None:
            files[f"public_keys/{k}.pem"] = public_pem(pub)
    sums = "".join(f"{sha256_hex(data)}  {name}\n" for name, data in sorted(files.items())).encode()
    sig, kid = sign(sums)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(name, data)
        z.writestr("SHA256SUMS", sums)
        z.writestr("bundle.sig", json.dumps({"key_id": kid, "signature": sig}))
    await audit.append_now(session, action="audit.export", workflow_id=workflow_id, object_type="workflow",
                           object_id=workflow_id, payload={"format": "zip", "sha256": sha256_hex(out.getvalue()),
                                                           "records": len(doc["audit"]["records"])})
    await session.commit()
    return out.getvalue()
