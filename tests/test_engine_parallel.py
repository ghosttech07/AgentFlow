"""Diamond A -> (B, C) -> D with a 1 s delay per task."""

from __future__ import annotations

import time
from datetime import datetime

from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import started_workflow, task, tasks_by_key

DIAMOND = [task("a"), task("b", deps=["a"]), task("c", deps=["a"]), task("d", "writer", ["b", "c"])]


def window(t: dict) -> tuple[datetime, datetime]:
    return datetime.fromisoformat(t["started_at"]), datetime.fromisoformat(t["completed_at"])


async def run_diamond(client: AsyncClient, make_engine, concurrency: int) -> tuple[dict, float]:  # noqa: ANN001
    wf_id = await started_workflow(client, DIAMOND)
    t0 = time.perf_counter()
    await make_engine(FakeLLMClient(delay=1.0), engine_max_concurrency=concurrency).run_until_idle()
    return await tasks_by_key(client, wf_id), time.perf_counter() - t0


async def test_parallel_vs_sequential(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    parallel, t_parallel = await run_diamond(client, make_engine, concurrency=4)
    sequential, t_sequential = await run_diamond(client, make_engine, concurrency=1)

    # With free slots, B and C run at the same time; D waits for both.
    (b_start, b_end), (c_start, c_end) = window(parallel["b"]), window(parallel["c"])
    assert b_start < c_end and c_start < b_end, "B and C should overlap"
    assert window(parallel["d"])[0] >= max(b_end, c_end)

    # With max_concurrency=1 they never overlap.
    (b_start, b_end), (c_start, c_end) = window(sequential["b"]), window(sequential["c"])
    assert b_end <= c_start or c_end <= b_start, "with max_concurrency=1, B and C must not overlap"
    assert t_sequential >= 4.0
    # No wall-clock comparison between the two runs: the overlap / no-overlap windows above (stored task
    # timestamps) prove parallelism; a speed ratio only measures how busy the machine is (it failed on CI).
