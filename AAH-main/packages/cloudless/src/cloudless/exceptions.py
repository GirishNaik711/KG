"""cloudless exception hierarchy (Q21).

All cloudless errors derive from `CloudlessError`. The split between
`TransientError` (safe to retry) and `PermanentError` (do NOT retry) is
load-bearing — the embedded runtime lib's retry middleware (Q21)
reads `recoverable` on every exception to decide whether to retry.

The `retry_after` field allows server-side hints (e.g., Bedrock 429s with
a Retry-After header) to propagate cleanly through the framework.
"""
from __future__ import annotations


class CloudlessError(Exception):
    """Base for every framework-raised error.

    Attributes:
        recoverable: True iff a retry is reasonable. The default is False
            (don't retry); transient subclasses override to True.
        retry_after: Optional hint, in seconds, from the underlying service
            about when to retry. Honored by the retry middleware where set.
    """

    recoverable: bool = False
    retry_after: float | None = None

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        if retry_after is not None:
            self.retry_after = retry_after


# --------------------------------------------------------------------- #
# Transient — safe to retry per the retry policy in cloudless.yaml
# --------------------------------------------------------------------- #


class TransientError(CloudlessError):
    """Operation failed but a retry is likely to succeed."""

    recoverable = True


class TimeoutError(TransientError):
    """The service call exceeded its configured timeout."""


class ThrottledError(TransientError):
    """The downstream service rate-limited us."""


class PeerUnreachable(TransientError):
    """A2A peer endpoint could not be reached after retries."""


class CircuitOpen(TransientError):
    """The circuit breaker for the target is open; request short-circuited."""


# --------------------------------------------------------------------- #
# Permanent — DO NOT retry. The retry middleware raises these as-is.
# --------------------------------------------------------------------- #


class PermanentError(CloudlessError):
    """Operation failed in a way that retrying won't fix."""


class PolicyViolation(PermanentError):
    """A `@cloudless.policy` decorator returned Deny."""


class GuardrailBlocked(PermanentError):
    """Bedrock Guardrails / Model Armor blocked the request or response."""


class AuthenticationError(PermanentError):
    """JWT validation failed or SigV4 was rejected."""


class InvalidInputError(PermanentError):
    """The request was malformed in a way the service rejected."""


# --------------------------------------------------------------------- #
# Special cases — neither transient nor permanent in the retry sense
# --------------------------------------------------------------------- #


class CostCapExceeded(CloudlessError):
    """A cost-cap policy fired; the operation was denied."""

    # Not retryable — retrying would cost more.
    recoverable = False
