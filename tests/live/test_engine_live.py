"""Real end-to-end run on Gemini. Excluded by default; run with: pytest -m live"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.llm.gemini_client import GeminiClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(get_settings().gemini_api_key is None, reason="GEMINI_API_KEY not set"),
]


async def test_live_small_workflow_completes(app, client: AsyncClient, make_engine) -> None:  # noqa: ANN001
    from app.llm import get_llm_client

    llm = GeminiClient(get_settings())
    app.dependency_overrides[get_llm_client] = lambda: llm
    resp = await client.post("/workflows", json={
        "name": "live", "goal": "Write a 120-word explainer on what a heat pump is, for beginners.",
        "run": True, "constraints": {"max_tasks": 3},
    })
    assert resp.status_code == 202
    await make_engine(llm).run_until_idle(timeout=600)

    wf = (await client.get(f"/workflows/{resp.json()['id']}")).json()
    for t in wf["tasks"]:
        print(f"  {t['key']:24} {t['agent_type']:10} {t['status']:10} {t['metrics'] and t['metrics']['latency_ms']}ms")
    assert wf["status"] == "COMPLETED", wf["error"]
    assert 1 <= len(wf["tasks"]) <= 3
