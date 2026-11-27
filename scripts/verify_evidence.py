"""Verify an AgentFlow evidence bundle OFFLINE: no database, no server, only public keys (Part 12).

    python scripts/verify_evidence.py evidence-<id>.zip
    python scripts/verify_evidence.py evidence-<id>.zip --public-keys docs/audit-public-keys

Checks: SHA256SUMS vs every file (names the file that changed), the bundle signature, the hash-chain continuity of
the audit segment, every checkpoint signature + head, and that evidence.json's audit references match the segment.
Exit code 0 = PASS, 1 = FAIL. Uses only: Python's stdlib + `cryptography`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def verify(bundle: Path, trusted_dirs: list[Path]) -> tuple[bool, list[str]]:
    from app.audit.keys import verify as verify_sig
    from app.audit.verify import verify_records

    lines: list[str] = []
    ok = True

    def check(cond: bool, good: str, bad: str) -> bool:
        nonlocal ok
        lines.append(("PASS  " if cond else "FAIL  ") + (good if cond else bad))
        ok = ok and cond
        return cond

    try:
        z = zipfile.ZipFile(bundle)
    except (zipfile.BadZipFile, OSError) as exc:
        return False, [f"FAIL  not a readable zip: {exc}"]
    names = set(z.namelist())
    for required in ("SHA256SUMS", "bundle.sig", "evidence.json", "audit_segment.jsonl", "checkpoints.json"):
        if not check(required in names, f"{required} present", f"{required} missing"):
            return False, lines
    sums_bytes = z.read("SHA256SUMS")
    listed = {}
    for line in sums_bytes.decode().splitlines():
        digest, _, name = line.partition("  ")
        listed[name] = digest
    changed = [n for n, d in listed.items() if n not in names or hashlib.sha256(z.read(n)).hexdigest() != d]
    extra = sorted(names - set(listed) - {"SHA256SUMS", "bundle.sig"})
    check(not changed, "every file matches SHA256SUMS", f"file(s) changed or missing: {', '.join(changed)}")
    check(not extra, "no unlisted files", f"unlisted file(s) added: {', '.join(extra)}")

    keydir = Path(bundle).with_suffix(".keys")
    keydir.mkdir(exist_ok=True)
    for n in names:
        if n.startswith("public_keys/") and n.endswith(".pem"):
            (keydir / Path(n).name).write_bytes(z.read(n))
    dirs = [*trusted_dirs, keydir]
    trusted_only = [d for d in trusted_dirs if d.exists()]
    sig = json.loads(z.read("bundle.sig"))
    in_trusted = any((d / f"{sig['key_id']}.pem").exists() for d in trusted_only)
    check(verify_sig(sig["key_id"], sums_bytes, sig["signature"], dirs),
          f"bundle signature valid (key {sig['key_id']}{', trusted' if in_trusted else ', from the bundle only'})",
          "bundle signature INVALID")
    if not in_trusted:
        lines.append("NOTE  the signing key was not found in a trusted --public-keys dir; compare it with the "
                     "operator's published key")

    seg = [json.loads(line) for line in z.read("audit_segment.jsonl").decode().splitlines() if line.strip()]
    cpj = json.loads(z.read("checkpoints.json"))
    chain_id = seg[0]["chain_id"] if seg else "-"
    r = verify_records(chain_id, seg, cpj["checkpoints"], dirs, start_prev=cpj.get("start_prev"))
    check(r.ok, f"audit segment: {len(seg)} records, continuous hash chain up to seq {r.verified_seq}, "
                f"{len(cpj['checkpoints'])} signed checkpoints valid",
          f"audit segment broken at seq {r.first_broken_seq}: {r.kind} ({r.detail})")
    doc = json.loads(z.read("evidence.json"))
    by_seq = {x["seq"]: x for x in seg if not x.get("stub")}
    mism = [ref["seq"] for ref in doc["audit"]["records"] if by_seq.get(ref["seq"], {}).get("hash") != ref["hash"]]
    check(not mism, f"evidence.json references match the segment ({len(doc['audit']['records'])} records)",
          f"evidence.json references do not match the segment at seq {mism[:5]}")
    for t in doc.get("tasks", []):
        a = t.get("audit")
        if a and by_seq.get(a["seq"], {}).get("payload", {}).get("manifest_digest") != a.get("manifest_digest"):
            check(False, "", f"task {t['key']}: manifest digest differs from the audit record")
    return ok, lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--public-keys", type=Path, action="append", default=None,
                    help="trusted public key dir (default: docs/audit-public-keys and outputs/keys/public)")
    args = ap.parse_args()
    trusted = args.public_keys or [ROOT / "docs" / "audit-public-keys", ROOT / "outputs" / "keys" / "public"]
    ok, lines = verify(args.bundle, trusted)
    print("\n".join(lines))
    print(f"\nEvidence bundle {args.bundle.name}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
