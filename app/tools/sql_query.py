"""sql_query: READ-ONLY queries against a configured external data source (never AgentFlow's own DB).

Two independent locks: (1) sqlglot must see exactly one SELECT (WITH ... SELECT / UNION ok) with no write
node anywhere in the tree; (2) the connection uses a read-only role inside a READ ONLY transaction with a
statement_timeout, so even a parser bypass cannot write. Results are capped at row_limit.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import asyncpg
import sqlglot
from pydantic import BaseModel, Field
from sqlalchemy import make_url
from sqlglot import exp

from app.config import Settings
from app.tools.base import Tool, ToolContext, ToolError
from app.tools.registry import register_tool

_WRITE_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, n) for n in ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Merge", "Command", "Copy",
                              "TruncateTable", "Into", "Grant", "Set", "Transaction", "Commit", "Rollback", "Use",
                              "LoadData", "Pragma") if hasattr(exp, n))
_FORBIDDEN_FUNCS = re.compile(r"^(pg_read|pg_ls_|pg_stat_file|lo_|dblink|pg_terminate|pg_cancel|set_config|"
                              r"pg_reload|pg_rotate|pg_file|query_to_xml|current_setting)", re.I)


class SqlQueryArgs(BaseModel):
    query: str = Field(min_length=1, max_length=5000, description="ONE read-only SELECT statement (Postgres SQL).")
    source: str = Field(default="sample", description="Configured data source name.")


class SqlQueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    source: str


def validate_select(sql: str) -> str:
    """Return the normalized single SELECT, or raise ToolError explaining why it is refused."""
    if not sql or not sql.strip():
        raise ToolError("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise ToolError(f"could not parse the query: {str(exc)[:200]}") from exc
    if len(statements) != 1:
        raise ToolError(f"exactly one statement is allowed (got {len(statements)})")
    tree = statements[0]
    if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except | exp.Subquery):
        raise ToolError(f"only SELECT queries are allowed (got {type(tree).__name__.upper()})")
    for node in tree.walk():
        if isinstance(node, _WRITE_NODES):
            raise ToolError(f"write or admin operation not allowed ({type(node).__name__.upper()})")
        if isinstance(node, exp.Func) and _FORBIDDEN_FUNCS.match(node.sql_name() if not isinstance(
                node, exp.Anonymous) else str(node.this)):
            raise ToolError(f"function not allowed: {node.sql()[:60]}")
    return tree.sql(dialect="postgres")


def _dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _same_database(url: str, settings: Settings) -> bool:
    target = make_url(url)
    for own in (settings.database_url, settings.test_database_url):
        o = make_url(own)
        if (target.database, (target.host or "localhost"), target.port or 5432) == (
                o.database, (o.host or "localhost"), o.port or 5432):
            return True
    return False


async def execute_readonly(url: str, sql: str, *, limit: int, timeout_s: float) -> tuple[list[str], list[list[Any]], bool]:
    """Run in a READ ONLY transaction with a statement timeout. Raises ToolError on any DB error."""
    try:
        conn = await asyncio.wait_for(asyncpg.connect(_dsn(url)), timeout=10)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        raise ToolError(f"could not connect to the data source: {type(exc).__name__}", transient=True) from exc
    try:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}")
            stmt = await conn.prepare(sql)
            records = await stmt.fetch()
            columns = [a.name for a in stmt.get_attributes()]
    except asyncpg.QueryCanceledError as exc:
        raise ToolError(f"query timed out after {timeout_s:g}s") from exc
    except asyncpg.ReadOnlySQLTransactionError as exc:
        raise ToolError(f"refused by the database: read-only connection ({exc})") from exc
    except asyncpg.PostgresError as exc:
        raise ToolError(f"query failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
    finally:
        await conn.close()
    rows = [[_json(v) for v in r.values()] for r in records[:limit]]
    return columns, rows, len(records) > limit


def _json(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)  # Decimal, dates, ...


@register_tool
class SqlQueryTool(Tool):
    name = "sql_query"
    description = ("Run ONE read-only SELECT on a configured data source (default 'sample': table ev_sales("
                   "country, year, month, units, segment, note) with FAKE illustrative numbers). Max 200 rows.")
    input_model = SqlQueryArgs
    output_model = SqlQueryResult
    side_effect = "external_read"
    idempotent = True
    timeout_seconds = 20.0

    def available(self, options: dict[str, Any], settings: Settings) -> str | None:
        return None if options.get("sources") else "no data sources configured (sql_query.sources)"

    async def execute(self, args: SqlQueryArgs, ctx: ToolContext) -> SqlQueryResult:
        import os

        sources: dict[str, dict[str, str]] = ctx.options.get("sources", {})
        spec = sources.get(args.source)
        if spec is None:
            raise ToolError(f"unknown data source '{args.source}' (configured: {', '.join(sources) or 'none'})")
        url = spec.get("url") or os.environ.get(spec.get("url_env", ""), "") or (
            ctx.settings.sample_data_url if args.source == "sample" else "")
        if not url:
            raise ToolError(f"data source '{args.source}' has no URL")
        if _same_database(url, ctx.settings):
            raise ToolError("refused: that is AgentFlow's own database (sql_query only reads external data)")
        sql = validate_select(args.query)
        limit = int(ctx.options.get("row_limit", 200))
        wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {limit + 1}"
        columns, rows, truncated = await execute_readonly(
            url, wrapped, limit=limit, timeout_s=float(ctx.options.get("statement_timeout_seconds", 5)))
        return SqlQueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated, source=args.source)
