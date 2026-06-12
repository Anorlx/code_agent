from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Callable

logger = logging.getLogger(__name__)

ModelCall = Callable[..., AsyncGenerator[dict[str, Any], None]]

VERIFY_REVIEW_SYSTEM_PROMPT = """你是 verifier code review sub_agent。
你的任务是根据工具执行结果、git diff、验证命令输出，判断本轮修改是否可信。
只输出 JSON，不要调用工具。

输出格式:
{
  "status": "passed|failed|warning",
  "summary": "...",
  "issues": [
    {
      "severity": "high|medium|low",
      "location": "file:line 或 unknown",
      "problem": "...",
      "suggestion": "..."
    }
  ]
}
"""


def _trim(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def review_verification(
    *,
    user_input: str,
    tool_results: list[dict[str, Any]],
    diff_snapshot: dict[str, Any],
    verification_results: list[dict[str, Any]],
    model_call: ModelCall | None,
    model_name: str,
) -> dict[str, Any]:
    if model_call is None:
        return {}
    payload = {
        "user_input": user_input,
        "tool_results": [
            {
                "name": result.get("name"),
                "summary": result.get("summary"),
                "ok": (result.get("raw_result") or {}).get("ok")
                if isinstance(result.get("raw_result"), dict)
                else None,
                "content": _trim(str(result.get("content") or ""), 2000),
            }
            for result in tool_results
        ],
        "diff": {
            "changed_files": diff_snapshot.get("changed_files", []),
            "stat": diff_snapshot.get("stat", ""),
            "diff": _trim(str(diff_snapshot.get("diff", "")), 6000),
        },
        "verification_results": [
            {
                "command": result.get("command"),
                "ok": result.get("ok"),
                "exit_code": result.get("exit_code"),
                "output": _trim(str(result.get("stderr") or result.get("stdout") or ""), 2000),
            }
            for result in verification_results
        ],
    }
    content = ""
    try:
        async for event in model_call(
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            system_prompt=VERIFY_REVIEW_SYSTEM_PROMPT,
            tools=[],
            model_name=model_name,
        ):
            if event.get("type") == "assistant_delta":
                content += event.get("content", "")
    except Exception as exc:
        logger.warning("verifier model review failed error=%s", exc)
        return {}
    return _extract_json(content)
