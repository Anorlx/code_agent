from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agent.hooks import HookAction, HookEvent, HookManager, HookResult
from agent.sub_agent.context_builder import build_task_context, task_context_report
from agent.sub_agent.permission_review import review_tool_call

logger = logging.getLogger(__name__)

PermissionReviewer = Callable[
    [str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str],
    Awaitable[dict[str, Any]],
]
PermissionPrompter = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
InternalEventSink = Callable[[dict[str, Any]], None]


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
        "shared_goal",
        "task",
    ):
        if key in arguments:
            parts.append(f"{key}={arguments[key]}")
    if "tasks" in arguments and isinstance(arguments["tasks"], list):
        parts.append(f"tasks={len(arguments['tasks'])}")
    if "research_tasks" in arguments and isinstance(arguments["research_tasks"], list):
        parts.append(f"research_tasks={len(arguments['research_tasks'])}")
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


def _validate_tool_input(
    tool_call: dict[str, Any],
    tool_info: dict[str, Any],
) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)
    spec = tool_info.get("spec", {})
    parameters = (
        spec.get("parameters", {})
        if isinstance(spec, dict)
        else None
    )
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

    if not isinstance(parameters, (dict, bool)):
        schema_error_type = "InvalidSchemaType"
    else:
        try:
            Draft202012Validator.check_schema(parameters)
            Draft202012Validator(parameters).validate(arguments)
        except (SchemaError, ValidationError) as error:
            schema_error_type = type(error).__name__
        else:
            schema_error_type = None
    if schema_error_type is not None:
        logger.debug(
            "tool input schema validation failed error_type=%s",
            schema_error_type,
        )
        return {
            "action": "ask",
            "allowed": False,
            "risk": "medium",
            "stage": "validateInput",
            "reason": "Tool input does not match its schema.",
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
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)

    try:
        if name not in tools:
            result = {"ok": False, "error": f"Unknown tool: {name}"}
        elif tools[name].get("accepts_runtime_context"):
            result = await tools[name]["run"](arguments, runtime_context or {})
        else:
            result = await tools[name]["run"](arguments)
    except Exception as error:
        logger.error("tool runner failed error_type=%s", type(error).__name__)
        result = {"ok": False, "error": "Tool execution failed."}

    if not isinstance(result, dict):
        logger.error(
            "tool runner returned invalid result type=%s",
            type(result).__name__,
        )
        result = {"ok": False, "error": "Tool execution failed."}

    return _message_with_result(tool_call, result)


def _rebuild_tool_call(
    tool_call: dict[str, Any],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    rebuilt = dict(tool_call)
    if isinstance(tool_call.get("function"), dict):
        function = dict(tool_call["function"])
        function["name"] = name
        raw_arguments = tool_call["function"].get("arguments")
        function["arguments"] = (
            json.dumps(arguments, ensure_ascii=False)
            if isinstance(raw_arguments, str)
            else dict(arguments)
        )
        rebuilt["function"] = function
    else:
        rebuilt["name"] = name
        raw_arguments = tool_call.get("arguments")
        rebuilt["arguments"] = (
            json.dumps(arguments, ensure_ascii=False)
            if isinstance(raw_arguments, str)
            else dict(arguments)
        )
    return rebuilt


def _message_with_result(
    tool_call: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    name = _tool_name(tool_call)
    arguments = _tool_arguments(tool_call)
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


def _opaque_hook_errors(result: HookResult, event_name: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "hook_error",
            "event_name": event_name,
            "handler_name": failure.handler_name,
            "error_type": failure.error_type,
            "message": f"Hook handler failed during {event_name}.",
        }
        for failure in result.failures
    ]


def _opaque_hook_error(
    event_name: str,
    *,
    handler_name: str,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return {
        "type": "hook_error",
        "event_name": event_name,
        "handler_name": handler_name,
        "error_type": error_type,
        "message": message,
    }


def _hook_payload_parts(
    payload: dict[str, Any] | None,
    *,
    original_name: str,
    require_result: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    if not isinstance(payload, dict) or payload.get("tool_name") != original_name:
        return None
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        return None
    result = payload.get("result")
    if require_result and not isinstance(result, dict):
        return None
    return dict(arguments), dict(result) if isinstance(result, dict) else None


def _hook_blocked_tool_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    result = {
        "ok": False,
        "error": "Tool blocked by lifecycle hook policy.",
        "hook_blocked": True,
    }
    return _message_with_result(tool_call, result)


async def _apply_result_hook(
    *,
    tool_call: dict[str, Any],
    message: dict[str, Any],
    hook_manager: HookManager,
    session_id: str,
    run_id: str,
    retry_attempt: int,
) -> tuple[dict[str, Any], HookResult, list[dict[str, Any]], bool]:
    name = _tool_name(tool_call) or ""
    result = message["raw_result"]
    event_name = "tool.error" if result.get("ok") is False else "tool.after"
    hook_result = await hook_manager.emit(
        HookEvent(
            event_name,
            session_id,
            {
                "tool_name": name,
                "arguments": copy.deepcopy(_tool_arguments(tool_call)),
                "result": copy.deepcopy(result),
                "tool_call_id": tool_call.get("id", name),
                # Retained for handlers written against the initial Task 3 API.
                "retry_attempt": retry_attempt,
            },
            metadata={
                "run_id": run_id,
                **(
                    {"hook_retry_attempt": retry_attempt}
                    if retry_attempt > 0
                    else {}
                ),
            },
        )
    )
    diagnostics = _opaque_hook_errors(hook_result, event_name)
    parts = _hook_payload_parts(
        hook_result.updated_payload,
        original_name=name,
        require_result=True,
    )
    if parts is None:
        diagnostics.append(
            _opaque_hook_error(
                event_name,
                handler_name="hook payload validator",
                error_type="HookProtocolError",
                message=f"Hook produced an invalid payload during {event_name}.",
            )
        )
        return message, hook_result, diagnostics, False
    arguments, updated_result = parts
    rebuilt = _rebuild_tool_call(tool_call, name, arguments)
    return (
        _message_with_result(rebuilt, updated_result or {}),
        hook_result,
        diagnostics,
        True,
    )


async def _run_tool_call_with_hooks(
    tool_call: dict[str, Any],
    tools: dict[str, dict[str, Any]],
    runtime_context: dict[str, Any] | None,
    hook_manager: HookManager | None,
    session_id: str,
    run_id: str,
    internal_event_sink: InternalEventSink | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if internal_event_sink is not None:
        internal_event_sink(
            {
                "type": "_tool_effective_arguments",
                "tool_call_id": tool_call.get("id", _tool_name(tool_call)),
                "arguments": copy.deepcopy(_tool_arguments(tool_call)),
            }
        )
    message = await _run_tool_call(tool_call, tools, runtime_context)
    if hook_manager is None:
        return message, []

    executed_message = message
    execution_failed = message["raw_result"].get("ok") is False
    message, hook_result, diagnostics, valid = await _apply_result_hook(
        tool_call=tool_call,
        message=message,
        hook_manager=hook_manager,
        session_id=session_id,
        run_id=run_id,
        retry_attempt=0,
    )
    if (
        not execution_failed
        or hook_result.action is not HookAction.RETRY
        or not valid
    ):
        return message, diagnostics

    retry_call = _rebuild_tool_call(tool_call, message["name"], message["arguments"])
    retry_validation = _validate_tool_input(
        retry_call,
        tools.get(message["name"] or "", {}),
    )
    if retry_validation.get("action") == "ask":
        diagnostics.append(
            _opaque_hook_error(
                "tool.error",
                handler_name="tool retry",
                error_type="HookRetryRejected",
                message="Tool hook retry was rejected.",
            )
        )
        return executed_message, diagnostics

    # Hook handlers are trusted Python extensions. Argument changes and retries
    # intentionally occur after the authoritative permission gate; same-tool
    # and schema validation constrain the effective call without re-reviewing it.
    diagnostics.append({"type": "hook_retry", "name": message["name"], "attempt": 1})
    if internal_event_sink is not None:
        internal_event_sink(
            {
                "type": "_tool_effective_arguments",
                "tool_call_id": retry_call.get("id", _tool_name(retry_call)),
                "arguments": copy.deepcopy(_tool_arguments(retry_call)),
            }
        )
    executed_retry_message = await _run_tool_call(retry_call, tools, runtime_context)
    retry_message, retry_result, retry_diagnostics, _ = await _apply_result_hook(
        tool_call=retry_call,
        message=executed_retry_message,
        hook_manager=hook_manager,
        session_id=session_id,
        run_id=run_id,
        retry_attempt=1,
    )
    diagnostics.extend(retry_diagnostics)
    if (
        executed_retry_message["raw_result"].get("ok") is False
        and retry_result.action is HookAction.RETRY
    ):
        retry_message = executed_retry_message
        diagnostics.append(
            _opaque_hook_error(
                "tool.error",
                handler_name="tool retry",
                error_type="RetryLimitExceeded",
                message="Tool hook retry limit reached.",
            )
        )
    return retry_message, diagnostics


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
    runtime_context: dict[str, Any] | None = None,
    *,
    hook_manager: HookManager | None = None,
    session_id: str = "",
    run_id: str = "",
    _internal_event_sink: InternalEventSink | None = None,
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

            approved_call = tool_call
            if hook_manager is not None:
                # Hook handlers are trusted Python extensions. tool.before runs
                # after permission approval and may change arguments, but never
                # the approved tool name; the effective call is schema-checked.
                original_name = name or ""
                hook_result = await hook_manager.emit(
                    HookEvent(
                        "tool.before",
                        session_id,
                        {
                            "tool_name": original_name,
                            "arguments": copy.deepcopy(_tool_arguments(tool_call)),
                            "tool_call_id": tool_call.get("id", original_name),
                        },
                        metadata={"run_id": run_id},
                    )
                )
                for error_event in _opaque_hook_errors(hook_result, "tool.before"):
                    yield error_event
                parts = _hook_payload_parts(
                    hook_result.updated_payload,
                    original_name=original_name,
                    require_result=False,
                )
                if parts is not None:
                    updated_arguments, _ = parts
                    candidate = _rebuild_tool_call(
                        tool_call,
                        original_name,
                        updated_arguments,
                    )
                    schema_valid = _validate_tool_input(candidate, info).get("action") != "ask"
                else:
                    candidate = tool_call
                    schema_valid = False
                if hook_result.action is HookAction.BLOCK or not schema_valid:
                    yield {
                        "type": "tool_result",
                        "message": _hook_blocked_tool_message(tool_call),
                    }
                    continue
                approved_call = candidate
            approved_batch.append(approved_call)

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
            results_with_events = await asyncio.gather(
                *[
                    _run_tool_call_with_hooks(
                        tool_call,
                        tools,
                        runtime_context,
                        hook_manager,
                        session_id,
                        run_id,
                        _internal_event_sink,
                    )
                    for tool_call in approved_batch
                ]
            )
            for result, diagnostic_events in results_with_events:
                for diagnostic_event in diagnostic_events:
                    yield diagnostic_event
                yield {"type": "tool_result", "message": result}
        else:
            for tool_call in approved_batch:
                result, diagnostic_events = await _run_tool_call_with_hooks(
                    tool_call,
                    tools,
                    runtime_context,
                    hook_manager,
                    session_id,
                    run_id,
                    _internal_event_sink,
                )
                for diagnostic_event in diagnostic_events:
                    yield diagnostic_event
                yield {"type": "tool_result", "message": result}
