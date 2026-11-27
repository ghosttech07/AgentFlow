"""Create the first global admin (or another user) directly in the database: the bootstrap for AUTH_MODE=required.

    python scripts/create_admin.py --name alice                 # asks for a password (hidden), prints the API key once
    python scripts/create_admin.py --name alice --no-password   # API key only
    AGENTFLOW_ADMIN_PASSWORD=... python scripts/create_admin.py --name alice   # non-interactive (CI)

The API key is printed ONCE; only its sha256 is stored. The password is stored as argon2id.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--role", default="admin", choices=["viewer", "operator", "approver", "admin"])
    ap.add_argument("--team", default=None, help="team name (created if missing); omit for a global user")
    ap.add_argument("--no-password", action="store_true")
    args = ap.parse_args()

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.db.session import SessionLocal
    from app.governance.principal import hash_key, new_key
    from app.governance.sessions import hash_password
    from app.models.governance import Team, User

    password = None
    if not args.no_password:
        password = os.environ.get("AGENTFLOW_ADMIN_PASSWORD") or getpass.getpass("Password (12+ characters): ")
    async with SessionLocal() as s:
        team_id = None
        if args.team:
            team_id = await s.scalar(select(Team.id).where(Team.name == args.team))
            if team_id is None:
                team = Team(name=args.team)
                s.add(team)
                await s.flush()
                team_id = team.id
        key = new_key()
        s.add(User(name=args.name, role=args.role, team_id=team_id, api_key_hash=hash_key(key),
                   password_hash=hash_password(password) if password else None))
        try:
            await s.commit()
        except IntegrityError:
            print(f"A user named {args.name!r} already exists.", file=sys.stderr)
            return 1
    print(f"Created {args.role} {args.name!r}{' in team ' + args.team if args.team else ' (global)'}.")
    print(f"API key (shown once, store it safely): {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
