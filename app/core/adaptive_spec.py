"""Part 10 plan extensions: feedback loops, conditional branches, re-planning, memory switches."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JoinMode = Literal["all_succeeded", "any_succeeded", "all_done"]
OnExhausted = Literal["continue_with_best", "fail", "ask_human"]


class LoopFeedback(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from", description="The task whose output is the feedback (usually the reviewer).")
    to: str = Field(description="The task that gets it as revision_feedback (usually the writer).")


class LoopSpec(BaseModel):
    """A bounded feedback loop over a contiguous chain of tasks, unrolled as <key>__v2, <key>__v3 (no cycles)."""

    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(description="snake_case loop name, e.g. 'improve_draft'.")
    body: list[str] = Field(description="Task keys of the loop, in order (each depends on the previous one).")
    until: str = Field(description="Stop condition over the body tasks, e.g. "
                                   "\"review.output.approved and review.output.score >= 8\".")
    max_iterations: int = Field(default=3, ge=1, description="Total iterations including the first (2-3 is typical).")
    on_exhausted: OnExhausted = "continue_with_best"
    best_by: str | None = Field(default=None, description="Expression ranking iterations; default: the reviewer's score.")
    feedback: LoopFeedback


class MemorySwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool = True
    write: bool = True
