"""calculator (AST whitelist, never eval) + sandboxed file_write / file_read / file_list."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.config import get_settings
from app.tools.base import ToolContext, ToolError
from app.tools.calculator import CalculatorArgs, CalculatorTool, evaluate
from app.tools.files import (
    FileListArgs,
    FileListTool,
    FileReadArgs,
    FileReadTool,
    FileWriteArgs,
    FileWriteTool,
    safe_path,
)


@pytest.mark.parametrize(("expr", "value"), [
    ("mean([1, 2, 3]) * 2", 4), ("2**10", 1024), ("round(100 * (23 - 17.5) / 17.5, 1)", 31.4),
    ("sum([1, 2, 3.5])", 6.5), ("max(3, 9, 4) - min([2, 8])", 7), ("sqrt(16) + abs(-2)", 6),
    ("median([5, 1, 3])", 3), ("-(3 % 2) + 7 // 2", 2), ("pct_change(100, 125)", 25),
])
def test_valid_math(expr: str, value: float) -> None:
    assert evaluate(expr) == pytest.approx(value)


@pytest.mark.parametrize("expr", [
    "__import__('os').system('dir')", "(1).real", "[x for x in [1, 2]]", "9**9**9", "open('x')",
    "lambda: 1", "x", "().__class__", "{'a': 1}", "1 if 1 else 2", "eval('1')",
])
def test_rejects_anything_outside_the_whitelist(expr: str) -> None:
    with pytest.raises(ToolError):
        evaluate(expr)


async def test_calculator_tool() -> None:
    out = await CalculatorTool().execute(CalculatorArgs(expression="1 + 2"), _ctx(Path(".")))
    assert out.value == 3 and CalculatorTool.side_effect == "none"


def _ctx(sandbox: Path) -> ToolContext:
    return ToolContext(workflow_id=uuid.uuid4(), task_key="t", attempt=1, sandbox=sandbox, idempotency_key="k",
                       settings=get_settings())


@pytest.mark.parametrize("rel", ["../x.txt", "a/../../x.txt", "/etc/passwd", "C:\\Windows\\x", "C:x", "\\\\srv\\share\\x"])
def test_paths_escaping_the_sandbox_are_blocked(tmp_path: Path, rel: str) -> None:
    with pytest.raises(ToolError):
        safe_path(tmp_path, rel)


def test_symlink_escaping_the_sandbox_is_blocked(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    outside.mkdir()
    box = tmp_path / "box"
    box.mkdir()
    try:
        os.symlink(outside, box / "link", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode / admin on Windows")
    with pytest.raises(ToolError, match="symlink"):
        safe_path(box, "link/x.txt")


async def test_write_read_list_roundtrip_and_idempotent_write(tmp_path: Path) -> None:
    c = _ctx(tmp_path)
    w = await FileWriteTool().execute(FileWriteArgs(path="reports/summary.md", content="# Hi\n"), c)
    again = await FileWriteTool().execute(FileWriteArgs(path="reports/summary.md", content="# Hi\n"), c)
    assert w.path == again.path == "reports/summary.md" and w.bytes == 5
    assert (tmp_path / "reports" / "summary.md").read_bytes() == b"# Hi\n"
    assert (await FileReadTool().execute(FileReadArgs(path="reports/summary.md"), c)).content == "# Hi\n"
    listing = await FileListTool().execute(FileListArgs(path="."), c)
    assert listing.files == ["reports/summary.md"]
    assert FileWriteTool.side_effect == "internal_write" and FileWriteTool.supports_idempotency_key


async def test_read_missing_and_oversized(tmp_path: Path) -> None:
    c = _ctx(tmp_path)
    with pytest.raises(ToolError, match="not found"):
        await FileReadTool().execute(FileReadArgs(path="nope.txt"), c)
    with pytest.raises(ValueError):
        FileWriteArgs(path="big.txt", content="x" * 300_000)
