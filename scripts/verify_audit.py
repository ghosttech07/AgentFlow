"""Verify the audit chains in the database (Part 12): every hash, link, checkpoint signature and anchor file.

    python scripts/verify_audit.py              # every chain
    python scripts/verify_audit.py --chain system

Exit code 0 = all chains OK; 1 = a break (the first broken seq and its kind are printed).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    from app.audit.verify import chains, verify_chain
    from app.config import get_settings
    from app.db.session import SessionLocal

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain")
    args = ap.parse_args()
    s = get_settings()
    ok = True
    async with SessionLocal() as session:
        for c in [args.chain] if args.chain else await chains(session):
            r = await verify_chain(session, c, s.audit_public_keys_dirs, s.audit_anchor_dir)
            if r.ok:
                print(f"OK    chain {c}: {r.records} records verified up to seq {r.verified_seq}, "
                      f"{r.checkpoints} signed checkpoints")
            else:
                ok = False
                print(f"FAIL  chain {c}: first broken seq {r.first_broken_seq}: {r.kind} ({r.detail})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
