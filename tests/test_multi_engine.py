"""Two real engine loops (multi-node mode) racing on the same DB."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient

from app.llm.fake_client import FakeLLMClient
from tests.engine_helpers import detail, started_workflow, task
from tests.reliability_helpers import attempts, calls_for


async def test_two_engines_execute_each_task_exactly_once(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    keys = [f"r{i}" for i in range(6)]
    wf_id = await started_workflow(client, [task(k) for k in keys] + [task("join", "writer", keys)])
    llm = FakeLLMClient(delay=0.3)
    common = {"engine_max_concurrency": 3, "engine_single_node": False, "engine_poll_interval_seconds": 0.05}
    e1 = make_engine(llm, engine_worker_id="engine-1", **common)
    e2 = make_engine(llm, engine_worker_id="engine-2", **common)

    await asyncio.gather(e1.start(), e2.start())
    try:
        for _ in range(200):
            if (await detail(client, wf_id))["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
    finally:
        await asyncio.gather(e1.stop(), e2.stop())

    assert (await detail(client, wf_id))["status"] == "COMPLETED"
    workers = set()
    for key in [*keys, "join"]:
        assert calls_for(llm, key) == 1, key
        rows = await attempts(client, wf_id, key)
        assert len(rows) == 1 and rows[0]["outcome"] == "succeeded", key
        workers.add(rows[0]["worker_id"])
    assert workers == {"engine-1", "engine-2"}  # both engines did real work, none twice
