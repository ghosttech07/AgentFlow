"""Create the read-only sample data source used by the sql_query tool (idempotent, safe to re-run).

    python scripts/setup_sample_data.py

* database `agentflow_sample` (separate from AgentFlow's own database), table `ev_sales` with clearly
  FAKE illustrative numbers (every row says so in its `note` column, and the table comment too);
* role `agentflow_reader`: LOGIN, CONNECT + SELECT only, `default_transaction_read_only = on`,
  `statement_timeout = 5s`. sql_query connects as this role, so read-only is enforced by Postgres
  itself, not only by the SQL parser.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import asyncpg
from sqlalchemy import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SAMPLE_DB = "agentflow_sample"
READER = "agentflow_reader"
NOTE = "FAKE sample data for demos - not real sales figures"
COUNTRIES = {"India": 1.0, "Indonesia": 0.35, "Vietnam": 0.25}
SEGMENTS = {"2W": 60_000, "3W": 30_000, "4W": 8_000, "bus": 300}


def _dsn(url: str, database: str | None = None) -> str:
    u = make_url(url).set(drivername="postgresql")
    if database:
        u = u.set(database=database)
    return u.render_as_string(hide_password=False)


def _rows() -> list[tuple[str, int, int, int, str, str]]:
    rng = random.Random(42)  # deterministic: the same fake numbers every time
    rows = []
    for country, scale in COUNTRIES.items():
        for year in (2025, 2026):
            for month in range(1, 13):
                if year == 2026 and month > 10:
                    continue
                growth = 1 + 0.025 * ((year - 2025) * 12 + month)
                for segment, base in SEGMENTS.items():
                    units = int(base * scale * growth * rng.uniform(0.9, 1.1))
                    rows.append((country, year, month, units, segment, NOTE))
    return rows


async def ensure_sample_data(admin_url: str, reader_password: str) -> None:
    admin = await asyncpg.connect(_dsn(admin_url))
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", SAMPLE_DB):
            try:
                await admin.execute(f'CREATE DATABASE "{SAMPLE_DB}"')
            except asyncpg.DuplicateDatabaseError:
                pass  # created concurrently
        pw = reader_password.replace("'", "''")
        if await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", READER):
            await admin.execute(f"ALTER ROLE {READER} WITH LOGIN PASSWORD '{pw}'")
        else:
            await admin.execute(f"CREATE ROLE {READER} WITH LOGIN PASSWORD '{pw}' NOSUPERUSER NOCREATEDB NOCREATEROLE")
        await admin.execute(f"ALTER ROLE {READER} SET default_transaction_read_only = on")
        await admin.execute(f"ALTER ROLE {READER} SET statement_timeout = '5s'")
        await admin.execute(f'GRANT CONNECT ON DATABASE "{SAMPLE_DB}" TO {READER}')
    finally:
        await admin.close()

    db = await asyncpg.connect(_dsn(admin_url, SAMPLE_DB))
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ev_sales (
                id serial PRIMARY KEY,
                country text NOT NULL,
                year int NOT NULL,
                month int NOT NULL CHECK (month BETWEEN 1 AND 12),
                units int NOT NULL,
                segment text NOT NULL,
                note text NOT NULL DEFAULT 'FAKE sample data for demos - not real sales figures'
            )""")
        await db.execute("COMMENT ON TABLE ev_sales IS 'FAKE illustrative sample data for AgentFlow demos. "
                         "Not real sales figures.'")
        if not await db.fetchval("SELECT count(*) FROM ev_sales"):
            await db.executemany(
                "INSERT INTO ev_sales (country, year, month, units, segment, note) VALUES ($1,$2,$3,$4,$5,$6)",
                _rows())
        await db.execute(f"REVOKE ALL ON SCHEMA public FROM {READER}")
        await db.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
        await db.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {READER}")
        await db.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER}")
    finally:
        await db.close()


def main() -> None:
    from app.config import get_settings

    s = get_settings()
    asyncio.run(ensure_sample_data(s.database_url, s.sample_db_reader_password.get_secret_value()))
    print(f"Sample data ready: database {SAMPLE_DB}, table ev_sales (FAKE numbers), read-only role {READER}.")


if __name__ == "__main__":
    main()
