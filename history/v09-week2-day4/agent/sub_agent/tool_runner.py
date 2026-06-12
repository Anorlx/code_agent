from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from agent.sub_agent.context_builder import build_task_context, task_context_report
from agent.sub_agent.permission_review import review_tool_call

PermissionReviewer = Callable[
    [str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str],
    Awaitable[dict[str, Any]],
]
PermissionPrompter = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _tool_result_content(result: dict[str, Any]) -> str:
    if result.get("ok") and "content" in result:
        return str(result["content"])
    if result.get("ok"):
        return json.dumps(result, ensure_ascii=False)
    return f"ERROR: {result.get('error', 'tool failed')}"


def _tool_name(tool_call: dict[str, Any]) -> str | None:
    return tool_call.get("name") or tool_call.get("function", {}).get("name")


def _tool_arguments(tool_call: dict[str, Any]) -> dict[str, Any]:
    raw_args = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments") or {}
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"raw": raw_args}
    return raw_args if isinstance(raw_args, dict) else {}


def _tool_summary(name: str | None, arguments: dict[str, Any]) -> str:
    if not arguments:
        return ""
    parts = []
    for key in (
        "path",
        "pattern",
        "expression",
        "timezone",
        "cwd",
        "address",
        "city",
        "keywords",
        "origin",
        "destination",
    ):
        if key in arguments:
            parts.append(f"{key}={arguments[key]}")
    if "command" in arguments:
        command = arguments["command"]
        if isinstance(command, list):
            parts.append("command=" + " ".join(str(part) for part in command))
        else:
            parts.append(f"command={command}")
    if "content" in arguments:
        parts.append(f"content={len(str(arguments['content']))} chars")
    if not parts:
        for key, value in list(arguments.items())[:3]:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def _schema_type_matches(value: Any, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return True


def _validate_tool_input(
    tool_call: dict[str, Any],
    tool_info: dict[str, Any],
) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)
    spec = tool_info.get("spec") or {}
    parameters = spec.get("parameters") or {}
    required = parameters.get("required") or []
    properties = parameters.get("properties") or {}

    if name is None:
        return {
            "action": "ask",
            "allowed": False,
            "risk": "medium",
            "stage": "validateInput",
            "reason": "工具调用缺少工具名，需要用户确认是否继续。",
        }
    if not isinstance(arguments, dict):
        return {
            "action": "ask",
            "allowed": False,
            "risk": "medium",
            "stage": "validateInput",
            "reason": "工具参数不是对象，需要用户确认是否继续。",
        }

    for key in required:
        if key not in arguments:
            return {
                "action": "ask",
                "allowed": False,
                "risk": "medium",
                "stage": "validateInput",
                "reason": f"工具参数缺少必需字段: {key}",
            }

    for key, value in arguments.items():
        field_schema = properties.get(key) or {}
        schema_type = field_schema.get("type")
        if schema_type and not _schema_type_matches(value, str(schema_type)):
            return {
                "action": "ask",
                "allowed": False,
                "risk": "medium",
                "stage": "validateInput",
                "reason": f"字段 {key} 类型不符合 schema: expected {schema_type}",
            }
        enum = field_schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            return {
                "action": "ask",
                "allowed": False,
                "risk": "medium",
                "stage": "validateInput",
                "reason": f"字段 {key} 不在允许值范围内。",
            }

    return {
        "action": "passthrough",
        "allowed": False,
        "risk": "low",
        "stage": "validateInput",
        "reason": "schema ok",
    }


def _normalize_review(review: dict[str, Any] | None) -> dict[str, Any]:
    if review is None:
        return {
            "action": "passthrough",
            "allowed": False,
            "risk": "unknown",
            "stage": "checkPermissions",
            "reason": "没有上下文审查结果。",
        }
    action = str(review.get("action") or "").strip()
    if not action:
        action = "allow" if review.get("allowed") else "deny"
    normalized = dict(review)
    normalized["action"] = action
    normalized["allowed"] = action == "allow"
    normalized.setdefault("stage", "checkPermissions")
    normalized.setdefault("risk", "unknown")
    normalized.setdefault("reason", "")
    return normalized


def _merge_permission_decision(
    *,
    validation: dict[str, Any],
    review: dict[str, Any],
    tool_info: dict[str, Any],
) -> dict[str, Any]:
    if validation.get("action") == "ask":
        return validation
    if review.get("action") == "deny":
        return review
    if tool_info.get("permission") == "deny":
        return {
            "action": "deny",
            "allowed": False,
            "risk": "high",
            "stage": "hasPermissionsToUseTool",
            "reason": "工具被规则显式 deny。",
        }
    if tool_info.get("permission") == "allow":
        return {
            "action": "allow",
            "allowed": True,
            "risk": review.get("risk", "low"),
            "stage": "hasPermissionsToUseTool",
            "reason": review.get("reason") or "settings 明确允许该工具。",
        }
    if tool_info.get("permission") == "ask":
        return {
            "action": "ask",
            "allowed": False,
            "risk": review.get("risk", "medium"),
            "stage": "hasPermissionsToUseTool",
            "reason": review.get("reason") or "settings 要求该工具调用必须用户确认。",
        }
    if tool_info.get("requires_review") or review.get("risk") in {"medium", "high"}:
        return {
            "action": "ask",
            "allowed": False,
            "risk": review.get("risk", "medium"),
            "stage": "hasPermissionsToUseTool",
            "reason": review.get("reason") or "该工具调用需要用户确认。",
        }
    if review.get("action") == "allow":
        return review
    return {
        "action": "ask",
        "allowed": False,
        "risk": review.get("risk", "unknown"),
        "stage": "checkPermissions",
        "reason": review.get("reason") or "权限管线未明确放行，降级为用户确认。",
    }


def _is_parallel_safe(tool_call: dict[str, Any], tools: dict[str, dict[str, Any]]) -> bool:
    name = _tool_name(tool_call)
    return bool(name in tools and tools[name].get("parallel_safe", False))


def _tool_batches(
    tool_calls: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
) -> list[tuple[bool, list[dict[str, Any]]]]:
    batches: list[tuple[bool, list[dict[str, Any]]]] = []
    current_parallel: list[dict[str, Any]] = []

    for tool_call in tool_calls:
        if _is_parallel_safe(tool_call, tools):
            current_parallel.append(tool_call)
            continue

        if current_parallel:
            batches.append((True, current_parallel))
            current_parallel = []
        batches.append((False, [tool_call]))

    if current_parallel:
        batches.append((True, current_parallel))
    return batches


async def _run_tool_call(
    tool_call: dict[str, Any],
    tools: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)

    if name not in tools:
        result = {"ok": False, "error": f"Unknown tool: {name}"}
    else:
        result = await tools[name]["run"](arguments)

    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", name),
        "name": name,
        "arguments": arguments,
        "summary": _tool_summary(name, arguments),
        "content": _tool_result_content(result),
        "raw_result": result,
        "created_at": time.time(),
    }


def _blocked_tool_message(tool_call: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)
    reason = review.get("reason") or "Permission review blocked this tool call."
    result = {
        "ok": False,
        "error": f"Permission denied by permission_review: {reason}",
        "review": review,
    }
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", name),
        "name": name,
        "arguments": arguments,
        "summary": _tool_summary(name, arguments),
        "content": _tool_result_content(result),
        "raw_result": result,
        "created_at": time.time(),
    }


async def run_tool_subagent(
    user_input: str,
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    tools: dict[str, dict[str, Any]],
    permission_reviewer: PermissionReviewer | None,
    reviewer_model_name: str,
    permission_prompter: PermissionPrompter | None = None,
    memory_context: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    context_messages = build_task_context(
        user_input=user_input,
        messages=messages,
        tool_calls=tool_calls,
        memory_context=memory_context,
    )
    yield {
        "type": "sub_context",
        "agent": "tool_runner",
        "context": task_context_report(context_messages),
    }
    for is_parallel, batch in _tool_batches(tool_calls, tools):
        approved_batch: list[dict[str, Any]] = []
        for tool_call in batch:
            name = _tool_name(tool_call)
            info = tools.get(name or "", {})
            validation = _validate_tool_input(tool_call, info)
            if name not in tools:
                decision = {
                    "action": "deny",
                    "allowed": False,
                    "risk": "high",
                    "stage": "validateInput",
                    "reason": f"Unknown tool: {name}",
                }
            else:
                raw_review = None
                if permission_reviewer is not None:
                    raw_review = await permission_reviewer(
                        user_input,
                        context_messages,
                        tool_call,
                        info,
                        reviewer_model_name,
                    )
                elif info.get("requires_review"):
                    raw_review = await review_tool_call(
                        user_input=user_input,
                        messages=context_messages,
                        tool_call=tool_call,
                        tool_info=info,
                        model_name=reviewer_model_name,
                    )
                else:
                    raw_review = {
                        "action": "allow",
                        "allowed": True,
                        "risk": "low",
                        "reason": "工具未声明 requires_review，规则匹配直接放行。",
                    }
                review = _normalize_review(raw_review)
                decision = _merge_permission_decision(
                    validation=validation,
                    review=review,
                    tool_info=info,
                )

            yield {
                "type": "tool_review",
                "name": name,
                "arguments": _tool_arguments(tool_call),
                "summary": _tool_summary(name, _tool_arguments(tool_call)),
                "review": decision,
            }
            if decision.get("action") == "ask":
                if permission_prompter is None:
                    decision = {
                        **decision,
                        "action": "deny",
                        "allowed": False,
                        "reason": "需要用户确认，但当前没有交互式确认器。",
                    }
                else:
                    prompt_result = await permission_prompter(
                        {
                            "tool_call": tool_call,
                            "tool_name": name,
                            "arguments": _tool_arguments(tool_call),
                            "summary": _tool_summary(name, _tool_arguments(tool_call)),
                            "review": decision,
                        }
                    )
                    decision = {
                        **decision,
                        **prompt_result,
                        "stage": "interactivePrompt",
                    }
                    yield {
                        "type": "permission_decision",
                        "name": name,
                        "arguments": _tool_arguments(tool_call),
                        "summary": _tool_summary(name, _tool_arguments(tool_call)),
                        "review": decision,
                    }
            if not decision.get("allowed", False):
                yield {"type": "tool_result", "message": _blocked_tool_message(tool_call, decision)}
                continue
            approved_batch.append(tool_call)

        if not approved_batch:
            continue

        for tool_call in approved_batch:
            name = _tool_name(tool_call)
            arguments = _tool_arguments(tool_call)
            yield {
                "type": "tool_start",
                "name": name,
                "arguments": arguments,
                "summary": _tool_summary(name, arguments),
                "parallel": is_parallel and len(approved_batch) > 1,
            }

        if is_parallel and len(approved_batch) > 1:
            results = await asyncio.gather(
                *[_run_tool_call(tool_call, tools) for tool_call in approved_batch]
            )
            for result in results:
                yield {"type": "tool_result", "message": result}
        else:
            for tool_call in approved_batch:
                result = await _run_tool_call(tool_call, tools)
                yield {"type": "tool_result", "message": result}
