"""Sandboxed files: file_write / file_read / file_list inside outputs/workspaces/<workflow_id>/.

Only relative paths; no `..` escape, no absolute or drive paths, no symlink anywhere on the path.
Writes are atomic (temp + os.replace) with deterministic bytes, so re-running a write is harmless.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolError
from app.tools.registry import register_tool

MAX_WRITE_BYTES = 200_000
MAX_READ_CHARS = 20_000
_DRIVE = re.compile(r"^[a-zA-Z]:")


def safe_path(sandbox: Path, rel: str) -> Path:
    rel = (rel or "").strip()
    if not rel or rel.startswith(("/", "\\")) or _DRIVE.match(rel) or PureWindowsPath(rel).anchor:
        raise ToolError(f"path must be relative to the workflow workspace: {rel!r}")
    root = sandbox.resolve()
    candidate = root.joinpath(*Path(rel.replace("\\", "/")).parts)
    current = root
    for part in candidate.relative_to(root).parts if candidate.is_relative_to(root) else ():
        current = current / part
        if current.is_symlink():
            raise ToolError(f"symlinks are not allowed in the workspace: {rel!r}")
    resolved = candidate.resolve()
    if not candidate.is_relative_to(root) or not resolved.is_relative_to(root):
        raise ToolError(f"path escapes the workflow workspace: {rel!r}")
    return resolved


def _workspace(ctx: ToolContext) -> Path:
    ctx.sandbox.mkdir(parents=True, exist_ok=True)
    return ctx.sandbox


class FileWriteArgs(BaseModel):
    path: str = Field(min_length=1, max_length=200, description="Relative path, e.g. 'reports/summary.md'.")
    content: str = Field(max_length=MAX_WRITE_BYTES)


class FileWriteResult(BaseModel):
    path: str
    bytes: int


class FileReadArgs(BaseModel):
    path: str = Field(min_length=1, max_length=200)


class FileReadResult(BaseModel):
    path: str
    content: str
    truncated: bool


class FileListArgs(BaseModel):
    path: str = Field(default=".", max_length=200)


class FileListResult(BaseModel):
    files: list[str]


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root.resolve()).as_posix()


@register_tool
class FileWriteTool(Tool):
    name = "file_write"
    description = "Write a text file into this workflow's private workspace (relative path)."
    input_model = FileWriteArgs
    output_model = FileWriteResult
    side_effect = "internal_write"
    idempotent = True
    supports_idempotency_key = True
    timeout_seconds = 10.0

    async def execute(self, args: FileWriteArgs, ctx: ToolContext) -> FileWriteResult:
        root = _workspace(ctx)
        target = safe_path(root, args.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = args.content.encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return FileWriteResult(path=_rel(root, target), bytes=len(data))


@register_tool
class FileReadTool(Tool):
    name = "file_read"
    description = "Read a text file from this workflow's workspace."
    input_model = FileReadArgs
    output_model = FileReadResult
    side_effect = "none"
    idempotent = True
    timeout_seconds = 10.0

    async def execute(self, args: FileReadArgs, ctx: ToolContext) -> FileReadResult:
        root = _workspace(ctx)
        target = safe_path(root, args.path)
        if not target.is_file():
            raise ToolError(f"file not found: {args.path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        return FileReadResult(path=_rel(root, target), content=text[:MAX_READ_CHARS],
                              truncated=len(text) > MAX_READ_CHARS)


@register_tool
class FileListTool(Tool):
    name = "file_list"
    description = "List the files in this workflow's workspace."
    input_model = FileListArgs
    output_model = FileListResult
    side_effect = "none"
    idempotent = True
    timeout_seconds = 10.0

    async def execute(self, args: FileListArgs, ctx: ToolContext) -> FileListResult:
        root = _workspace(ctx)
        base = safe_path(root, args.path) if args.path not in (".", "", "./") else root.resolve()
        files = sorted(_rel(root, p) for p in base.rglob("*")
                       if p.is_file() and not p.name.startswith(".tmp-"))
        return FileListResult(files=files[:500])
