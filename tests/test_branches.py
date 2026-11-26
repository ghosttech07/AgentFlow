"""Part 10 conditional branches: run_if, join modes, tolerated failed branches."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.agents.researcher import ResearchOutput
from app.core.joins import decide
from app.llm.errors import LLMPermanentError
from app.llm.fake_client import FakeLLMClient, FakeScript
from tests.engine_helpers import detail, match, started_workflow, task, tasks_by_key

LOW = {"summary": "s", "key_findings": ["a", "b", "c"], "sources": [], "confidence": "low", "gaps": []}
HIGH = {**LOW, "confidence": "high"}


class Dep:
    def __init__(self, status: str) -> None:
        self.status = status


class T:
    def __init__(self, join: str, *deps: str) -> None:
        self.join, self.dependencies = join, [Dep(s) for s in deps]


@pytest.mark.parametrize(("join", "deps", "expected"), [
    ("all_succeeded", ["SUCCEEDED", "SUCCEEDED"], "ready"),
    ("all_succeeded", ["SUCCEEDED", "RUNNING"], "wait"),
    ("all_succeeded", ["SUCCEEDED", "SKIPPED"], "skip"),
    ("all_succeeded", ["SUCCEEDED", "FAILED"], "wait"),
    ("any_succeeded", ["SKIPPED", "SUCCEEDED"], "ready"),
    ("any_succeeded", ["FAILED", "SUCCEEDED"], "ready"),
    ("any_succeeded", ["SKIPPED", "FAILED"], "skip"),
    ("any_succeeded", ["SUCCEEDED", "RUNNING"], "wait"),
    ("all_done", ["SKIPPED", "FAILED"], "ready"),
    ("all_done", ["SUCCEEDED", "PENDING"], "wait"),
])
def test_join_rules(join: str, deps: list[str], expected: str) -> None:
    assert decide(T(join, *deps))[0] == expected


def research_llm(**outputs: dict) -> FakeLLMClient:
    return FakeLLMClient(scripts=[FakeScript(match=match(k), response=v) for k, v in outputs.items()])


async def test_run_if_picks_the_branch(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await started_workflow(client, [
        task("research_india"), task("research_china"),
        task("deep_india", "researcher", ["research_india"], run_if="research_india.output.confidence == 'low'"),
        task("deep_china", "researcher", ["research_china"], run_if="research_china.output.confidence == 'low'"),
    ])
    await make_engine(research_llm(research_india=HIGH, research_china=LOW)).run_until_idle()
    t = await tasks_by_key(client, wf)
    assert t["deep_china"]["status"] == "SUCCEEDED"
    assert (t["deep_india"]["status"], t["deep_india"]["skip_reason"]) == ("SKIPPED", "condition_false")
    events = [e for e in (await client.get(f"/workflows/{wf}/events")).json() if e["event_type"] == "task.condition_evaluated"]
    india = next(e for e in events if e["payload"]["task_key"] == "deep_india")
    assert india["payload"]["result"] is False and india["payload"]["values"] == {"research_india.output.confidence": "high"}
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED" and "deep_india" in d["result"]["skipped"]
    assert "deep_india" not in d["result"]["final_outputs"]


async def test_skip_cascades_with_all_succeeded_but_not_with_any_succeeded(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await started_workflow(client, [
        task("a"), task("b", "researcher", ["a"], run_if="a.output.confidence == 'low'"),
        task("after_b", "writer", ["b"]),
        task("summary", "writer", ["a", "b"], join="any_succeeded"),
    ])
    await make_engine(research_llm(a=HIGH)).run_until_idle()
    t = await tasks_by_key(client, wf)
    assert t["b"]["status"] == "SKIPPED" and t["after_b"]["status"] == "SKIPPED"
    assert t["after_b"]["skip_reason"] == "upstream_skipped" and t["summary"]["status"] == "SUCCEEDED"


async def test_tolerated_failed_branch_completes_but_is_reported(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    llm = FakeLLMClient(scripts=[FakeScript(match=match("flaky"), error=LLMPermanentError("source site is down"))],
                        responses={ResearchOutput: HIGH})
    wf = await started_workflow(client, [task("good"), task("flaky", max_attempts=1),
                                         task("summary", "writer", ["good", "flaky"], join="all_done")])
    await make_engine(llm).run_until_idle()
    d = await detail(client, wf)
    assert d["status"] == "COMPLETED"
    assert d["result"]["failed_branches"] == [{"key": "flaky", "error_type": "permanent",
                                               "error": "source site is down"}]
    assert "workflow.completed_with_failed_branches" in [e["event_type"] for e in
                                                         (await client.get(f"/workflows/{wf}/events")).json()]


async def test_an_untolerated_failure_still_fails_the_workflow(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    llm = FakeLLMClient(scripts=[FakeScript(match=match("flaky"), error=LLMPermanentError("down"))])
    wf = await started_workflow(client, [task("good"), task("flaky", max_attempts=1),
                                         task("summary", "writer", ["good", "flaky"])])  # all_succeeded
    await make_engine(llm).run_until_idle()
    assert (await detail(client, wf))["status"] == "FAILED"


async def test_condition_error_fails_the_task(client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    wf = await started_workflow(client, [task("a"), task("b", "researcher", ["a"], run_if="a.output.rating > 3")])
    await make_engine(research_llm(a=HIGH)).run_until_idle()
    t = (await tasks_by_key(client, wf))["b"]
    assert t["status"] == "FAILED" and t["error_type"] == "condition_error" and "a.output.rating" in t["error"]
