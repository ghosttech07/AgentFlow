# Part 12: Trust layer (design)

**Goal:** make trust provable. Anyone can check:
- what happened, who decided it, and which rule applied;
- which model and prompt produced each output;
- that no record was altered;
- that personal data was protected.

## Decisions (approved by the user 2026-11-27)

1. **Database roles in tests.** The main test suite keeps running as the DB owner, because it needs TRUNCATE and
   schema rebuilds. A dedicated test connects as the restricted role `agentflow_app` and proves it can only
   INSERT and SELECT on `audit_log` and `audit_checkpoints`.
2. **Erasure requests have their own table** (`erasure_requests`). They reuse the Part 11 vote rules: 2 distinct
   approvers, four eyes, one veto. Approvals belong to workflows, and an erasure request isn't one.
3. **The master key** is `AUDIT_MASTER_KEY` (base64, 32 bytes). In development, a key file is generated once in
   `outputs/keys/` (git-ignored). Production refuses to start without the key. The same applies to the Ed25519
   signing key (`AUDIT_SIGNING_KEY_PATH`).
4. **AUTH_MODE** `off` keeps today's development behaviour; `required` always needs a login; production refuses
   `off` (built in Step 0).
5. **Presidio:** only the adapter interface, switched off. The git-repo checkpoint target is optional and uses
   the git CLI.
6. **Deferred to Part 15** (the user's decision): policy versions in the DB, embedded policy tests,
   `/policies/simulate`, escalation beyond the unsatisfiable case, reject_mode, the admin, team-switcher and
   policies pages.

## The audit write path

- **One function.** `record_event()` (the single event writer) and `audit.enqueue()` for non-workflow actions
  (logins, users, exports, erasures) both put a pending record on `session.info["audit_pending"]`, in order.
- **Same transaction.** A SQLAlchemy `before_flush` listener (and `audit.append_now` when nothing else is
  flushed) appends the records in the same transaction:
  1. `pg_advisory_xact_lock(hash(chain_id))`;
  2. read and update the chain head row (`audit_chain_heads`);
  3. compute seq and hash;
  4. insert.
- **Atomic.** An error in the append fails the flush, so the state change rolls back. A rollback removes the
  audit rows too.
- **Chains.** One chain per team (`chain_id` = the team uuid) plus `system` for global events. Each chain starts
  with a genesis record (seq 0, `prev_hash` = 64 zeros).
- **The hash:** `sha256(canonical_json(record without "hash") + prev_hash)`. Canonical JSON means:
  - sorted keys, `separators=(",", ":")`, UTF-8 (`ensure_ascii=False`);
  - datetimes in UTC as `YYYY-MM-DDTHH:MM:SS.ffffffZ`;
  - Decimal values as strings and UUIDs as strings;
  - floats with Python `repr`;
  - no NaN or Infinity.
- **Hashed fields:** `chain_id, seq, at, team_id, actor_type, actor_id, auth_method, ip, action, object_type,
  object_id, decision, payload, payload_digest, prev_hash`. `payload` is the redacted payload; `payload_digest`
  is the sha256 of the canonical original.
- **Append-only:**
  - a trigger raises on UPDATE or DELETE of `audit_log` and `audit_checkpoints`;
  - `scripts/setup_db_roles.py` grants `agentflow_app` INSERT and SELECT only on those tables;
  - a DB superuser can still bypass both, which is why the chain is hashed and checkpoints are signed and
    exported outside the DB.
- **Backfill:** migration 0011 turns existing events into audit records, in their original order, with
  `payload.backfilled = true`.

## Checkpoints and verification

- **When:** every `AUDIT_CHECKPOINT_EVERY_RECORDS` (100) records or `AUDIT_CHECKPOINT_MINUTES` (15), the engine
  tick signs `{chain_id, seq, head_hash, at, key_id}` with Ed25519.
- **Where it goes:**
  - the `audit_checkpoints` table;
  - `outputs/audit-anchors/<chain>.jsonl`;
  - optionally a webhook;
  - optionally a git repo commit.
- **Keys:** `scripts/generate_audit_key.py`. Public keys live in `docs/audit-public-keys/<key_id>.pem`
  (committed) and, for dev keys, in `outputs/keys/public`. When keys are rotated, old public keys are kept.
- **Verification** (`app/audit/verify.py`) re-walks the chain and reports the first break and its kind:
  - `deleted`: a gap in seq;
  - `modified`: the hash recomputed from the record differs;
  - `inserted`: `prev_hash` matches no earlier hash;
  - `reordered`: `prev_hash` matches a different earlier record;
  - `checkpoint_mismatch`: a checkpoint's head differs from the chain, or the anchor file differs from the DB;
  - `bad_signature`.
- **Background verify:** a job re-checks every `AUDIT_VERIFY_MINUTES` and raises `audit.integrity_failed`
  (event + webhook) on failure.

## Manifests, content and encryption

- **Prompt registry:** `prompt_templates` (content hash, agent_type, name, text), registered at startup.
- **Per-call manifest:** every LLM call records a manifest entry on the `llm_calls` row:
  - provider, model and parameters;
  - the template hash and the rendered-prompt hash;
  - the input hashes and the response hash;
  - usage, latency and route position.
- **Per workflow:** the code version, and the hashes of `models.yaml`, `tools.yaml`, `policies.yaml` and
  `pricing.yaml`.
- **Content storage** (`AUDIT_STORE_CONTENT` = hash_only | redacted | full): content goes to `stored_contents`,
  encrypted with AES-256-GCM using a per-workflow data key (`workflow_keys`). The data key is itself wrapped by
  the master key.
- **Crypto-shredding** deletes the `workflow_keys` row, which makes the stored content unreadable.
- **Tamper-evidence:** a finished task's manifest digest goes into the chain (`task.manifest_recorded`).

## PII pipeline

- **Detectors** (`app/privacy/detectors.py`), each with validation: Verhoeff for Aadhaar, Luhn for cards, the PAN
  entity character, IFSC, passport, email, phone, IP addresses and secrets.
- **Modes** per type: mask, tokenize, block or allow.
- **Applied in the router before every external call:**
  - tokenization uses a per-workflow vault that is encrypted;
  - the policy decision point `llm_call` sees `{provider_locality, pii_types_found, team, agent_type}` and can
    say `only_local`, in which case the router skips external entries or blocks;
  - secrets are always blocked from external providers (safety floor).
- **Logs:** a logging filter masks PII and secrets in every log line.

## Erasure, retention, evidence

- **Retention:** per-team settings and a daily job; each deletion is audited with counts only.
- **Erasure:** an erasure request is approved by 2 people. The system then:
  1. finds the affected workflows (vault matches + content scans);
  2. deletes memories, outputs and files;
  3. shreds the data keys;
  4. records `privacy.erasure_completed`.
- **Evidence bundle** (zip):
  - `report.pdf` (reportlab), `evidence.json` and `audit_segment.jsonl`;
  - `public_keys/`, `VERIFY.md` and `SHA256SUMS`;
  - `bundle.sig` (Ed25519).
- **Offline check:** `scripts/verify_evidence.py` verifies a bundle offline.
