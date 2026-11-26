"""calculator: safe arithmetic + statistics over an AST whitelist. Never eval; bounded exponents."""

from __future__ import annotations

import ast
import math
import operator
import statistics
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolError
from app.tools.registry import register_tool

_BIN: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _seq(*args: Any) -> list[float]:
    return list(args[0]) if len(args) == 1 and isinstance(args[0], list) else list(args)


FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs, "round": round, "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "min": lambda *a: min(_seq(*a)), "max": lambda *a: max(_seq(*a)), "sum": lambda *a: sum(_seq(*a)),
    "mean": lambda *a: statistics.fmean(_seq(*a)), "median": lambda *a: statistics.median(_seq(*a)),
    "stdev": lambda *a: statistics.stdev(_seq(*a)), "count": lambda *a: len(_seq(*a)),
    "pct_change": lambda old, new: (new - old) / old * 100,
}
CONSTS = {"pi": math.pi, "e": math.e}
MAX_EXPONENT = 100


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > MAX_EXPONENT or abs(left) > 1e6):
            raise ToolError(f"exponent too large (max {MAX_EXPONENT})")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.List):
        return [_eval(e) for e in node.elts]
    if isinstance(node, ast.Name) and node.id in CONSTS:
        return CONSTS[node.id]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS
            and not node.keywords):
        return FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ToolError(f"not allowed in a calculator expression: {type(node).__name__}"
                    + (f" '{node.id}'" if isinstance(node, ast.Name) else ""))


def evaluate(expression: str) -> Any:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"invalid expression: {exc.msg}") from exc
    try:
        return _eval(tree)
    except ToolError:
        raise
    except (ArithmeticError, ValueError, TypeError, statistics.StatisticsError) as exc:
        raise ToolError(f"math error: {exc}") from exc


class CalculatorArgs(BaseModel):
    expression: str = Field(min_length=1, max_length=500, description=(
        "Arithmetic expression, e.g. 'pct_change(1820, 2310)' or 'mean([12, 15, 9])'. Functions: "
        + ", ".join(sorted(FUNCS)) + "; constants pi, e."))


class CalculatorResult(BaseModel):
    expression: str
    value: float | int | list[float | int]


@register_tool
class CalculatorTool(Tool):
    name = "calculator"
    description = "Exact arithmetic and statistics (growth %, averages, sums). Use it instead of mental math."
    input_model = CalculatorArgs
    output_model = CalculatorResult
    side_effect = "none"
    idempotent = True
    timeout_seconds = 5.0

    async def execute(self, args: CalculatorArgs, ctx: ToolContext) -> CalculatorResult:
        return CalculatorResult(expression=args.expression, value=evaluate(args.expression))
