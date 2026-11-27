"""Create the restricted application DB role (Part 12) and grant it least privilege.

    python scripts/setup_db_roles.py                  # the database in DATABASE_URL
    python scripts/setup_db_roles.py --database agentflow_test

Role `agentflow_app` (password from AGENTFLOW_APP_DB_PASSWORD, default "app" for development):
  - SELECT/INSERT/UPDATE/DELETE on every table, EXCEPT audit_log and audit_checkpoints: SELECT + INSERT only;
  - no TRUNCATE anywhere, no DDL (it does not own the schema);
  - USAGE on the sequences.
Run the app with this role (DATABASE_URL=postgresql+asyncpg://agentflow_app:...) and keep the owner role for
migrations. A Postgres SUPERUSER can still bypass grants and the append-only trigger: that is why the chain is
hash-linked and signed checkpoints are exported outside the database (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
APPEND_ONLY_TABLES = ("audit_log", "audit_checkpoints")


async def setup(database: str | None = None, password: str | None = None) -> None:
    import asyncpg

    from app.config import get_settings

    url = get_settings().database_url.replace("+asyncpg", "")
    if database:
        url = url.rsplit("/", 1)[0] + "/" + database
    password = password or os.environ.get("AGENTFLOW_APP_DB_PASSWORD", "app")
    conn = await asyncpg.connect(url)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'agentflow_app'")
        quoted = password.replace("'", "''")
        if not exists:
            await conn.execute(f"CREATE ROLE agentflow_app LOGIN PASSWORD '{quoted}'")
        else:
            await conn.execute(f"ALTER ROLE agentflow_app LOGIN PASSWORD '{quoted}'")
        db = await conn.fetchval("SELECT current_database()")
        await conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO agentflow_app')
        await conn.execute("GRANT USAGE ON SCHEMA public TO agentflow_app")
        await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agentflow_app")
        await conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO agentflow_app")
        for t in APPEND_ONLY_TABLES:
            await conn.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM agentflow_app")
        print(f"agentflow_app ready on {db}: INSERT/SELECT only on {', '.join(APPEND_ONLY_TABLES)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database")
    asyncio.run(setup(ap.parse_args().database))
