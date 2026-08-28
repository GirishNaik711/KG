"""Q21 resilience middleware: retry + timeout + circuit-breaker.

Three composable async wrappers:

  with_retry(fn, *, attempts, backoff)
      Exponential backoff retry that honors `recoverable` on CloudlessError
      and the optional `retry_after` hint. Permanent errors raise immediately.

  with_timeout(fn, *, seconds)
      asyncio.wait_for-based timeout. Translates TimeoutError to
      cloudless.TimeoutError so retry sees it as transient.

  CircuitBreaker(name, *, failure_threshold, recovery_timeout)
      Half-open style circuit breaker. After N consecutive failures, the
      circuit opens; subsequent calls raise CircuitOpen until the recovery
      timeout passes; the next call is allowed through (half-open). One
      success closes the circuit; one failure re-opens it.

Combine via `resilient(...)`:

    @resilient(attempts=3, timeout_seconds=10, circuit="bedrock")
    async def call_llm(...):
        ...
"""
from __future__ import annotations

import asyncio
import functools
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from cloudless.exceptions import (
    CircuitOpen,
    CloudlessError,
    PermanentError,
    TransientError,
)
from cloudless.exceptions import (
    TimeoutError as CloudlessTimeoutError,
)

T = TypeVar("T")


# --------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------- #


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    backoff: float = 0.25,
    max_backoff: float = 10.0,
    jitter: float = 0.1,
) -> T:
    """Retry an async callable until it succeeds, runs out of attempts, or
    raises a non-recoverable error.

    Args:
        fn: Zero-arg async callable. Wrap your call in a lambda.
        attempts: Maximum attempts including the first. Must be >= 1.
        backoff: Base delay seconds for exponential backoff. Doubles each retry.
        max_backoff: Upper bound on delay between attempts.
        jitter: Random additive jitter, in seconds, on every wait.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except PermanentError:
            raise
        except CloudlessError as e:
            last = e
            if not getattr(e, "recoverable", False):
                raise
            if i == attempts - 1:
                raise
            wait = e.retry_after if e.retry_after is not None else min(
                max_backoff, backoff * (2 ** i)
            )
            wait += random.uniform(0, jitter)
            await asyncio.sleep(wait)
        except (TimeoutError, ConnectionError) as e:
            last = e
            if i == attempts - 1:
                raise CloudlessTimeoutError(str(e)) from e
            wait = min(max_backoff, backoff * (2 ** i)) + random.uniform(0, jitter)
            await asyncio.sleep(wait)
    # Unreachable, but mypy needs it
    assert last is not None
    raise last


# --------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------- #


async def with_timeout(fn: Callable[[], Awaitable[T]], *, seconds: float) -> T:
    """Run `fn` with a timeout; translate to cloudless.TimeoutError."""
    try:
        return await asyncio.wait_for(fn(), timeout=seconds)
    except TimeoutError as e:
        raise CloudlessTimeoutError(f"operation exceeded {seconds}s") from e


# --------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------- #


@dataclass
class CircuitBreaker:
    """Per-target circuit breaker shared across calls.

    States:
      CLOSED   — calls proceed; consecutive failures count toward threshold.
      OPEN     — calls raise CircuitOpen immediately.
      HALF_OPEN — one probe call is allowed; success closes, failure re-opens.
    """

    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0

    # Mutable state. Not for external use.
    _state: str = field(default="CLOSED", init=False, repr=False)
    _failures: int = field(default=0, init=False, repr=False)
    _opened_at: float = field(default=0.0, init=False, repr=False)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()

        if self._state == "OPEN":
            if now - self._opened_at >= self.recovery_timeout:
                self._state = "HALF_OPEN"
            else:
                raise CircuitOpen(
                    f"circuit {self.name!r} open (failures={self._failures})"
                )

        try:
            result = await fn()
        except TransientError:
            self._on_failure()
            raise
        except PermanentError:
            # Permanent errors don't trip the breaker — they're not infra issues.
            raise
        except CloudlessError:
            self._on_failure()
            raise
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result

    def _on_success(self) -> None:
        self._failures = 0
        self._state = "CLOSED"

    def _on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "OPEN"
            self._opened_at = time.monotonic()


# Registry: one breaker per name. CLI / hosted runtime reuse instances.
_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(
    name: str,
    *,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
) -> CircuitBreaker:
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
        )
    return _BREAKERS[name]


def reset_breakers() -> None:
    """Test helper — clears all breakers."""
    _BREAKERS.clear()


# --------------------------------------------------------------------- #
# Decorator that composes retry + timeout + circuit-breaker
# --------------------------------------------------------------------- #


def resilient(
    *,
    attempts: int = 3,
    backoff: float = 0.25,
    timeout_seconds: float | None = None,
    circuit: str | None = None,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Compose retry + timeout + circuit-breaker on an async function.

    Order: circuit-breaker(retry(timeout(fn))) — the circuit sees the final
    success/failure after retries are exhausted.
    """

    def _decorate(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        breaker = get_breaker(circuit, failure_threshold=failure_threshold,
                              recovery_timeout=recovery_timeout) if circuit else None

        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> T:
            async def _inner() -> T:
                async def _attempt() -> T:
                    if timeout_seconds is not None:
                        return await with_timeout(
                            lambda: fn(*args, **kwargs), seconds=timeout_seconds,
                        )
                    return await fn(*args, **kwargs)

                return await with_retry(_attempt, attempts=attempts, backoff=backoff)

            if breaker is not None:
                return await breaker.call(_inner)
            return await _inner()

        return _wrapped

    return _decorate
