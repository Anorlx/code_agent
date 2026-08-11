from __future__ import annotations

import ast
import operator
from typing import Any


_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Only arithmetic expressions are allowed.")


async def calculator(arguments: dict[str, Any]) -> dict[str, Any]:
    expression = str(arguments.get("expression", "")).strip()
    if not expression:
        return {"ok": False, "error": "Missing expression."}
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree)
    except Exception as exc:
        return {"ok": False, "error": f"Only arithmetic expressions are allowed. {exc}"}
    return {"ok": True, "content": str(value)}


def calculator_spec() -> dict[str, Any]:
    return {
        "name": "calculator",
        "description": "计算安全的四则运算表达式，例如 2 + 3 * 4。只支持数字和算术运算。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的算术表达式。",
                }
            },
            "required": ["expression"],
        },
    }

