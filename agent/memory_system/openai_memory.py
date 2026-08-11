from __future__ import annotations

import json
import os
import re
from typing import Any

from agent.main_agent.config import OPENAI_MEMORY_MODEL


class MemoryModelError(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|private[_-]?key)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact_sensitive(value: str) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    return text


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value)
    return value


def _text_from_response_object(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("type") == "output_text" and isinstance(value.get("text"), str):
            return value["text"]
        for key in ("output", "content"):
            text = _text_from_response_object(value.get(key))
            if text:
                return text
    elif isinstance(value, list):
        for item in value:
            text = _text_from_response_object(item)
            if text:
                return text
    return ""


def _response_output_text(response: Any) -> str:
    """Read SDK objects and SSE strings returned by local OpenAI-compatible gateways."""
    if isinstance(response, str):
        deltas: list[str] = []
        completed_response: dict[str, Any] | None = None
        for line in response.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            elif event_type == "response.completed":
                response_value = event.get("response")
                if isinstance(response_value, dict):
                    completed_response = response_value
        if deltas:
            return "".join(deltas)
        return _text_from_response_object(completed_response or {})
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    if hasattr(response, "model_dump"):
        return _text_from_response_object(response.model_dump())
    return ""


async def complete_json(
    *,
    instructions: str,
    payload: Any,
    schema_name: str,
    schema: dict[str, Any],
    model_name: str = OPENAI_MEMORY_MODEL,
) -> dict[str, Any]:
    if os.getenv("OPENAI_MEMORY_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        raise MemoryModelError("OpenAI memory model is disabled.")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise MemoryModelError("OPENAI_API_KEY is not set; candidates remain pending.")

    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise MemoryModelError("The openai package is not installed.") from exc

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if base_url:
        client_kwargs["base_url"] = base_url
    client = AsyncOpenAI(**client_kwargs)
    try:
        response = await client.responses.create(
            model=model_name,
            instructions=instructions,
            input=json.dumps(redact_payload(payload), ensure_ascii=False),
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        )
    except Exception as exc:
        raise MemoryModelError(str(exc)) from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()

    if not isinstance(response, str) and getattr(response, "status", "completed") != "completed":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
        raise MemoryModelError(f"OpenAI memory response incomplete: {reason}")
    try:
        parsed = json.loads(_response_output_text(response))
    except (TypeError, json.JSONDecodeError) as exc:
        raise MemoryModelError("OpenAI memory response was not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise MemoryModelError("OpenAI memory response must be a JSON object.")
    return parsed
