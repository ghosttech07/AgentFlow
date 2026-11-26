"""sql_query: one SELECT only (sqlglot), read-only role at the DB level, row limit, timeout, never our own DB."""

from __future__ import annotations

import uuid

import pytest

from app.config import get_settings
from app.tools import sql_query
from app.tools.base import ToolContext, ToolError
from app.tools.sql_query import SqlQueryArgs, SqlQueryTool, validate_select


def ctx(**options: object) -> ToolContext:
    s = get_settings()
    opts = {"sources": {"sample": {"url": s.sample_data_url}}, "row_limit": 200, **options}
    return ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=s.workspaces_dir,
                       idempotency_key="k", settings=s, options=opts)


async def run(sql: str, **options: object):  # noqa: ANN201
    return await SqlQueryTool().execute(SqlQueryArgs(query=sql), ctx(**options))


async def test_select_returns_rows() -> None:
    out = await run("SELECT country, sum(units) AS units FROM ev_sales WHERE year = 2026 GROUP BY country ORDER BY 1")
    assert out.columns == ["country", "units"] and [r[0] for r in out.rows] == ["India", "Indonesia", "Vietnam"]
    assert out.truncated is False


async def test_with_select_is_allowed() -> None:
    out = await run("WITH m AS (SELECT month, units FROM ev_sales WHERE segment = '2W') SELECT count(*) FROM m")
    assert out.rows[0][0] > 0


@pytest.mark.parametrize("sql", [
    "INSERT INTO ev_sales (country, year, month, units, segment) VALUES ('x', 1, 1, 1, 'x')",
    "UPDATE ev_sales SET units = 0",
    "DELETE FROM ev_sales",
    "DROP TABLE ev_sales",
    "SELECT 1; DROP TABLE ev_sales",
    "SELECT 1; -- harmless\nDELETE FROM ev_sales",
    "SELECT * INTO stolen FROM ev_sales",
    "WITH d AS (DELETE FROM ev_sales RETURNING *) SELECT * FROM d",
    "COPY ev_sales TO '/tmp/x'",
    "SELECT pg_read_file('/etc/passwd')",
    "",
])
def test_non_select_statements_rejected(sql: str) -> None:
    with pytest.raises(ToolError):
        validate_select(sql)


def test_comment_inside_a_select_is_fine() -> None:
    assert validate_select("SELECT 1 -- ; DROP TABLE ev_sales") is not None


async def test_read_only_role_blocks_writes_even_if_the_parser_is_bypassed() -> None:
    with pytest.raises(ToolError, match="read-only"):
        await sql_query.execute_readonly(get_settings().sample_data_url, "DELETE FROM ev_sales", limit=10, timeout_s=5)


async def test_row_limit_truncates() -> None:
    out = await run("SELECT * FROM ev_sales", row_limit=7)
    assert len(out.rows) == 7 and out.truncated is True and out.row_count == 7


async def test_statement_timeout() -> None:
    with pytest.raises(ToolError, match="timed out"):
        await run("SELECT pg_sleep(3)", statement_timeout_seconds=1)


async def test_agentflow_own_database_is_refused() -> None:
    s = get_settings()
    with pytest.raises(ToolError, match="AgentFlow's own database"):
        await run("SELECT 1", sources={"sample": {"url": s.test_database_url}})


async def test_unknown_source() -> None:
    with pytest.raises(ToolError, match="unknown data source"):
        await SqlQueryTool().execute(SqlQueryArgs(query="SELECT 1", source="prod"), ctx())
