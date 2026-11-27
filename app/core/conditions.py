"""Safe condition language (Part 10): `run_if`, loop `until`, `replan_if`, `best_by`.

Evaluated by CODE over task outputs through an AST whitelist (never eval). Names are task keys (a loop's base key
resolves to its latest finished iteration, done by the caller when it builds `outputs`). Each name exposes
`.output` (the task's output) and `.status`.

    review_guide.output.approved and review_guide.output.score >= 8
    research_china.output.confidence == 'low' or len(research_china.output.gaps) > 3

Errors: a bad expression or an unknown task key -> ConditionSyntaxError (plan time); a missing field or a task
without output at run time -> ConditionError (the caller records `condition_error`; never a crash).
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

MAX_CHARS = 300
MAX_NODES = 80
MAX_DEPTH = 12
MAX_EXPONENT = 64
MAX_VALUE_LEN = 10_000  # a string / list built by a condition (repetition, concatenation) stays small

_BIN: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CMP: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}
_FUNCS: dict[str, Callable[..., Any]] = {
    "len": len, "lower": lambda s: str(s).lower(), "upper": lambda s: str(s).upper(), "min": min, "max": max,
    "abs": abs, "round": round, "any": any, "all": all,
}
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.Compare,
            ast.BinOp, ast.Constant, ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Call, ast.List, ast.Tuple,
            *_BIN, *_CMP)


class ConditionSyntaxError(ValueError):
    """The expression is not allowed or references an unknown task (caught at plan time)."""


class ConditionError(ValueError):
    """The expression could not be evaluated on the actual outputs (recorded as condition_error)."""


def _depth(node: ast.AST, level: int = 0) -> int:
    return max([level, *(_depth(c, level + 1) for c in ast.iter_child_nodes(node))])


def _path(node: ast.AST) -> str | None:
    """'a.output.x' for a pure Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _check_size(op: ast.operator, left: Any, right: Any) -> None:
    """Refuse a repetition before it allocates ('a' * 10**12 would exhaust memory before any length check)."""
    if not isinstance(op, ast.Mult):
        return
    for seq, n in ((left, right), (right, left)):
        if isinstance(seq, str | list | tuple) and isinstance(n, int) and not isinstance(n, bool):
            if len(seq) * max(n, 0) > MAX_VALUE_LEN:
                raise ConditionError(f"value longer than {MAX_VALUE_LEN} items")


class Condition:
    def __init__(self, expr: str, tree: ast.Expression, refs: set[str]) -> None:
        self.expr, self.tree, self.refs = expr, tree, refs

    def value(self, outputs: dict[str, dict[str, Any] | None]) -> Any:
        """The raw result (e.g. a score for best_by)."""
        try:
            return self._eval(self.tree.body, outputs, {})
        except ConditionError:
            raise
        except (TypeError, ValueError, ZeroDivisionError, ArithmeticError, IndexError, KeyError,
                MemoryError, RecursionError) as exc:
            raise ConditionError(f"could not evaluate {self.expr!r}: {type(exc).__name__}: {exc}") from exc

    def evaluate(self, outputs: dict[str, dict[str, Any] | None]) -> tuple[bool, dict[str, Any]]:
        values: dict[str, Any] = {}
        try:
            result = self._eval(self.tree.body, outputs, values)
        except ConditionError:
            raise
        except (TypeError, ValueError, ZeroDivisionError, ArithmeticError, IndexError, KeyError,
                MemoryError, RecursionError) as exc:
            raise ConditionError(f"could not evaluate {self.expr!r}: {type(exc).__name__}: {exc}") from exc
        return bool(result), values

    def _eval(self, node: ast.AST, out: dict[str, Any], values: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for v in node.values:
                    result = self._eval(v, out, values)
                    if not result:
                        return result
                return result
            result = False
            for v in node.values:
                result = self._eval(v, out, values)
                if result:
                    return result
            return result
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, out, values)
            return {ast.Not: lambda x: not x, ast.USub: operator.neg, ast.UAdd: operator.pos}[type(node.op)](operand)
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left, out, values), self._eval(node.right, out, values)
            if isinstance(node.op, ast.Pow) and (abs(right) > MAX_EXPONENT or abs(left) > 1e6):
                raise ConditionError("exponent too large")
            _check_size(node.op, left, right)
            result = _BIN[type(node.op)](left, right)
            if isinstance(result, str | list | tuple) and len(result) > MAX_VALUE_LEN:
                raise ConditionError(f"value longer than {MAX_VALUE_LEN} items")
            return result
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, out, values)
            for op, comp in zip(node.ops, node.comparators, strict=True):
                right = self._eval(comp, out, values)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.List | ast.Tuple):
            return [self._eval(e, out, values) for e in node.elts]
        if isinstance(node, ast.Call):
            fn = _FUNCS[node.func.id]  # validated at parse time
            return fn(*[self._eval(a, out, values) for a in node.args])
        if isinstance(node, ast.Subscript):
            base = self._eval(node.value, out, values)
            index = self._eval(node.slice, out, values)
            try:
                return base[index]
            except (IndexError, KeyError, TypeError) as exc:
                raise ConditionError(f"{ast.unparse(node)} does not exist") from exc
        if isinstance(node, ast.Name | ast.Attribute):
            return self._lookup(node, out, values)
        raise ConditionError(f"not allowed: {type(node).__name__}")

    def _lookup(self, node: ast.AST, out: dict[str, Any], values: dict[str, Any]) -> Any:
        chain: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):  # e.g. review.output.issues[0].severity: subscript in the chain
            base = self._eval(cur, out, values)
            for attr in reversed(chain):
                if not isinstance(base, dict) or attr not in base:
                    raise ConditionError(f"{ast.unparse(node)} does not exist")
                base = base[attr]
            return base
        key = cur.id
        task = out.get(key)
        if task is None:
            raise ConditionError(f"task '{key}' is not available")
        value: Any = task
        path = key
        for attr in reversed(chain):
            path += "." + attr
            if attr == "output" and value is task and task.get("output") is None:
                raise ConditionError(f"task '{key}' has no output (status {task.get('status')})")
            if not isinstance(value, dict) or attr not in value:
                raise ConditionError(f"{path} does not exist")
            value = value[attr]
        if chain:
            values[path] = value
        return value


def parse_condition(expr: str, known_keys: set[str] | None = None) -> Condition:
    if not isinstance(expr, str) or not expr.strip():
        raise ConditionSyntaxError("empty condition")
    if len(expr) > MAX_CHARS:
        raise ConditionSyntaxError(f"condition longer than {MAX_CHARS} characters")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except RecursionError as exc:
        raise ConditionSyntaxError("condition too deeply nested") from exc
    except SyntaxError as exc:
        raise ConditionSyntaxError(f"invalid condition {expr!r}: {exc.msg}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES or _depth(tree) > MAX_DEPTH:
        raise ConditionSyntaxError("condition too complex")
    refs: set[str] = set()
    for node in nodes:
        if not isinstance(node, _ALLOWED):
            raise ConditionSyntaxError(f"not allowed in a condition: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ConditionSyntaxError(f"not allowed in a condition: attribute {node.attr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
                raise ConditionSyntaxError(f"function not allowed: {ast.unparse(node.func)}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, str | int | float | bool | type(None)):
            raise ConditionSyntaxError("constant type not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in ("output", "status") and isinstance(node.value, ast.Name):
            raise ConditionSyntaxError(f"{_path(node)}: a task exposes only .output and .status")
    for node in nodes:
        if isinstance(node, ast.Name) and not any(isinstance(c, ast.Call) and c.func is node for c in nodes):
            refs.add(node.id)
    if known_keys is not None:
        for ref in sorted(refs):
            if ref not in known_keys:
                raise ConditionSyntaxError(f"unknown task '{ref}' in condition {expr!r}")
    return Condition(expr.strip(), tree, refs)
