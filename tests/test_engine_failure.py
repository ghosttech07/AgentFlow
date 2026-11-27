from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.llm.errors import LLMOutputError, LLMPermanentError, LLMTransientError
from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.engine_helpers import detail, event_types, match, started_workflow, task, tasks_by_key

# a -> (b, c) -> d ; b fails (after its retries, if retryable); c is slow enough to still be
# running when the workflow fails.
# b fails on its first attempt: retries (Part 5) would add claim cycles and race c's fixed 2 s delay on a busy
# machine (seen in a 5x run). Retry behaviour has its own tests.
GRAPH = [task("a"), task("b", deps=["a"], max_attempts=1), task("c", deps=["a"]), task("d", "writer", ["b", "c"])]


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (LLMTransientError("Gemini API error 503 UNAVAILABLE"), "transient"),
        (LLMPermanentError("Gemini API error 403"), "permanent"),
        (LLMOutputError("Gemini output failed validation"), "output"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_failure_fails_workflow_and_preserves_sibling(
    client: AsyncClient, make_engine, error: Exception, error_type: str  # noqa: ANN001
) -> None:
    wf_id = await started_workflow(client, GRAPH)
    llm = FakeLLMClient(scripts=[FakeScript(match("b"), error=error), FakeScript(match("c"), delay=2.0)])
    await make_engine(llm).run_until_idle()

    wf = await detail(client, wf_id)
    assert wf["status"] == "FAILED"
    assert "Task 'b' failed" in wf["error"] and error_type in wf["error"]

    tasks = await tasks_by_key(client, wf_id)
    assert tasks["b"]["status"] == "FAILED"
    assert tasks["b"]["error_type"] == error_type and tasks["b"]["error"]
    assert tasks["b"]["lease_owner"] is None
    # The running sibling still finished and its output is preserved.
    assert tasks["c"]["status"] == "SUCCEEDED" and tasks["c"]["output"]
    # Downstream work is left PENDING (not skipped/cancelled) so Part 5 can resume it.
    assert tasks["d"]["status"] == "PENDING"
    assert not any("Task d" in c["user_prompt"] for c in llm.calls)

    types = await event_types(client, wf_id)
    assert "task.failed" in types and "workflow.failed" in types
    assert types.index("workflow.failed") < max(i for i, t in enumerate(types) if t == "task.succeeded")


async def test_timeout(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("slow")])
    llm = FakeLLMClient(scripts=[FakeScript(match("slow"), hang=True)])
    await make_engine(llm, task_timeout_seconds=0.3).run_until_idle()

    t = (await tasks_by_key(client, wf_id))["slow"]
    assert t["status"] == "FAILED" and t["error_type"] == "timeout"
    assert "timed out after 0.3s" in t["error"]
    assert (await detail(client, wf_id))["status"] == "FAILED"


async def test_no_new_claims_for_failed_workflow(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf_id = await started_workflow(client, [task("a"), task("b"), task("c", "writer", ["a", "b"])])
    llm = FakeLLMClient(scripts=[FakeScript(match("a"), error=LLMPermanentError("nope"))])
    await make_engine(llm, engine_max_concurrency=1).run_until_idle()

    tasks = await tasks_by_key(client, wf_id)
    # Only one slot: 'a' ran first and failed, so the READY sibling 'b' was never claimed.
    assert tasks["a"]["status"] == "FAILED"
    assert tasks["b"]["status"] == "READY" and tasks["b"]["attempt_count"] == 0
    assert tasks["c"]["status"] == "PENDING"
