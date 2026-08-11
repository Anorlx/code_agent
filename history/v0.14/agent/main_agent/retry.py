from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 3
    initial_delay: float = 0.4
    max_delay: float = 4.0
    backoff_factor: float = 2.0
    jitter: float = 0.15


NON_RETRYABLE_MARKERS = {
    "invalidparameter",
    "invalid parameter",
    "invalid_api_key",
    "api key is not set",
    "authentication",
    "unauthorized",
    "permission denied",
    "forbidden",
    "schema",
    "missing required",
    "not installed",
}

RETRYABLE_MARKERS = {
    "timeout",
    "timed out",
    "temporarily",
    "temporary",
    "transient",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote disconnected",
    "server disconnected",
    "network",
    "rate limit",
    "ratelimit",
    "too many requests",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "cold start",
}


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True

    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status_code, int):
        if status_code in {408, 409, 425, 429} or 500 <= status_code <= 599:
            return True
        if 400 <= status_code < 500:
            return False

    text = " ".join(
        str(part)
        for part in (
            exc.__class__.__name__,
            getattr(exc, "code", ""),
            getattr(exc, "message", ""),
            str(exc),
        )
        if part
    ).lower()
    if any(marker in text for marker in NON_RETRYABLE_MARKERS):
        return False
    return any(marker in text for marker in RETRYABLE_MARKERS)


def retry_delay(attempt_index: int, config: RetryConfig) -> float:
    base = min(config.max_delay, config.initial_delay * (config.backoff_factor ** attempt_index))
    if config.jitter <= 0:
        return base
    return max(0.0, base + random.uniform(-config.jitter, config.jitter))


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    config: RetryConfig = RetryConfig(),
    should_retry: Callable[[BaseException], bool] = is_transient_error,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    last_error: BaseException | None = None
    attempts = max(1, config.attempts)
    for attempt_index in range(attempts):
        try:
            return await operation()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            last_error = exc
            is_last = attempt_index >= attempts - 1
            if is_last or not should_retry(exc):
                raise
            delay = retry_delay(attempt_index, config)
            if on_retry is not None:
                on_retry(attempt_index + 1, exc, delay)
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error
