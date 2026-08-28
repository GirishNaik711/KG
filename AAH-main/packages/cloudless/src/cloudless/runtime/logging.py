"""cloudless.runtime.logging — structured JSON logging conventions (Q39).

`get_logger(name)` returns a structlog logger pre-wired with:
  - JSON output to stdout (matches the cloud-side convention from Q39)
  - Auto-injected required fields (agent.name, agent.version, run.id,
    session.id, trace.id, span.id) from contextvars
  - Auto-redaction of well-known secret patterns

Usage in a generated agent entrypoint:
    from cloudless.runtime.logging import get_logger, bind_invocation
    log = get_logger("cloudless.runtime.peer")
    bind_invocation(agent_name="hello", agent_version="v17", session_id=...)
    log.info("peer call succeeded", peer="orders", duration_ms=142)

Context is unbound at the end of each invocation via `clear_invocation()`.
"""
from __future__ import annotations

import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# ContextVar-backed context that auto-injects on every log call.
_invocation_ctx: ContextVar[dict] = ContextVar("cloudless_invocation_ctx", default={})  # noqa: B039 — read-only snapshot inside structlog processors; never mutated in place


# --------------------------------------------------------------------- #
# Sensitive-data redaction (Q39 best-effort)
# --------------------------------------------------------------------- #


_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.I), "Bearer <REDACTED>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<REDACTED:aws-access-key>"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "<REDACTED:aws-temp-key>"),
    # AWS secret keys (40 chars base64 - high false-positive risk so apply only after AKIA-style context)
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<REDACTED:provider-key>"),
    (re.compile(r"xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+"), "<REDACTED:slack-token>"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "<REDACTED:github-token>"),
    # JWT — three base64url segments separated by dots. Each segment uses
    # the base64url alphabet [A-Za-z0-9_-]. Header is typically 17-30 chars
    # so we require at least 10 after the eyJ prefix.
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<REDACTED:jwt>"),
]

_user_redact: list[tuple[re.Pattern, str]] = []


def add_redact_pattern(regex: str | re.Pattern, replacement: str = "<REDACTED>") -> None:
    """Add a user-defined regex to the redaction set (Q39 user-extensible)."""
    pat = re.compile(regex) if isinstance(regex, str) else regex
    _user_redact.append((pat, replacement))


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        s = value
        for pat, repl in _REDACT_PATTERNS:
            s = pat.sub(repl, s)
        for pat, repl in _user_redact:
            s = pat.sub(repl, s)
        return s
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v) for v in value)
    return value


def _redact_processor(_logger, _name, event_dict: dict) -> dict:
    """structlog processor that redacts known patterns in field values."""
    return _redact(event_dict)


def _inject_context_processor(_logger, _name, event_dict: dict) -> dict:
    """Inject contextvars-stored invocation context onto every log line."""
    ctx = _invocation_ctx.get()
    if ctx:
        # Don't override fields the caller already supplied
        for k, v in ctx.items():
            event_dict.setdefault(k, v)
    return event_dict


# --------------------------------------------------------------------- #
# Bindings — set by the embedded runtime entrypoint per invocation
# --------------------------------------------------------------------- #


def bind_invocation(
    *,
    agent_name: str | None = None,
    agent_version: str | None = None,
    agent_framework: str | None = None,
    cloud: str | None = None,
    region: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> None:
    """Bind Q39 required fields for the duration of this invocation."""
    fields = {
        "agent.name": agent_name,
        "agent.version": agent_version,
        "agent.framework": agent_framework,
        "cloud": cloud,
        "region": region,
        "run.id": run_id,
        "session.id": session_id,
        "trace.id": trace_id,
        "span.id": span_id,
    }
    # Drop Nones so they don't pollute logs
    ctx = {k: v for k, v in fields.items() if v is not None}
    _invocation_ctx.set(ctx)


def clear_invocation() -> None:
    """Clear the invocation-scoped fields. Call at end of every invocation."""
    _invocation_ctx.set({})


# --------------------------------------------------------------------- #
# Logger configuration
# --------------------------------------------------------------------- #


_configured = False


def configure(level: str = "INFO", *, json: bool = True) -> None:
    """Configure structlog. Idempotent."""
    global _configured
    if _configured:
        return
    import logging

    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")

    processors: list = [
        structlog.contextvars.merge_contextvars,
        _inject_context_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        context_class=dict,
        # Use a fresh-stream factory so pytest's capsys captures correctly.
        logger_factory=structlog.PrintLoggerFactory(),  # writes to sys.stdout at call time
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str = "cloudless"):
    """Return a configured structlog logger."""
    if not _configured:
        configure()
    return structlog.get_logger(name)
