"""docs/ARCHITECTURE.md state diagrams must match the state machine's transition tables exactly."""

from __future__ import annotations

import re

from app.config import PROJECT_ROOT
from app.core.state_machine import TASK_TRANSITIONS, WORKFLOW_TRANSITIONS

DOC = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
EDGE = re.compile(r"^\s*([A-Z_]+)\s*-->\s*([A-Z_]+)\b", re.M)


def _diagram_edges(heading: str) -> set[tuple[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    section = text.split(heading, 1)[1]
    block = section.split("```mermaid", 1)[1].split("```", 1)[0]
    assert "stateDiagram-v2" in block
    return set(EDGE.findall(block))


def _table_edges(table: dict) -> set[tuple[str, str]]:  # noqa: ANN001
    return {(str(f.value), str(t.value)) for f, ts in table.items() for t in ts}


def test_workflow_diagram_matches_transitions() -> None:
    assert _diagram_edges("### Workflow states") == _table_edges(WORKFLOW_TRANSITIONS)


def test_task_diagram_matches_transitions() -> None:
    assert _diagram_edges("### Task states") == _table_edges(TASK_TRANSITIONS)
