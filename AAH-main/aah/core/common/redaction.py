"""Secret redaction for subprocess and persisted diagnostic output."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_MAX_LOG_BYTES = 256 * 1024

_REDACTED = "***REDACTED***"

_SANITIZE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        _REDACTED,
    ),
    (re.compile(r"AKIA[0-9A-Z]{16}"), _REDACTED),
    (
        re.compile(r"(?i)(?P<scheme>authorization\s*[:=]\s*bearer|bearer)\s+\S+"),
        lambda m: f"{m.group('scheme')} {_REDACTED}",
    ),
    (
        re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s:/@]+:[^\s]*@"),
        lambda m: f"{m.group('scheme')}{_REDACTED}@",
    ),
    (
        re.compile(
            r"(?i)(?P<key>['\"]?(?:password|passwd|pwd|secret|token|api[_-]?key|"
            r"authorization|bearer|aws_secret_access_key|aws_access_key_id)['\"]?)"
            r"(?P<sep>\s*[:=]\s*|\s+)(?P<val>\"[^\"]*\"|'[^']*'|\S+)"
        ),
        lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}",
    ),
    (re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"), _REDACTED),
)


def sanitize_output(text: str, *, extra_patterns: Iterable[re.Pattern[str]] | None = None) -> str:
    """Redact credentials/private keys and bound the UTF-8 output size."""
    if not isinstance(text, str):
        text = str(text)

    redacted = text
    for pattern, replacement in _SANITIZE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    if extra_patterns:
        for pattern in extra_patterns:
            redacted = pattern.sub(_REDACTED, redacted)

    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_LOG_BYTES:
        dropped = len(encoded) - _MAX_LOG_BYTES
        head = encoded[:_MAX_LOG_BYTES].decode("utf-8", errors="replace")
        redacted = f"{head}\n...[truncated {dropped} bytes]...\n"
    return redacted


def exact_value_patterns(values: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Build longest-first literal redactors for sensitive runtime values.

    The values themselves are never returned or persisted.  Longest-first
    ordering prevents a shorter credential from partially masking a longer
    one before the longer value can be matched.
    """
    patterns: list[re.Pattern[str]] = []
    for value in sorted({str(value) for value in values if value}, key=len, reverse=True):
        escaped = re.escape(value)
        if re.fullmatch(r"[A-Za-z0-9_]+", value):
            # Token boundaries keep a short value such as "a" from corrupting
            # structural strings such as "fail", while still redacting
            # common output forms such as ``credential=a``.
            escaped = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
        patterns.append(re.compile(escaped))
    return tuple(patterns)


def sanitize_structure(
    value: Any,
    *,
    extra_patterns: Iterable[re.Pattern[str]] | None = None,
    preserve_keys: Iterable[str] | None = None,
) -> Any:
    """Return a recursively redacted copy of a JSON-like value.

    Mapping keys are schema identifiers and are deliberately preserved;
    string values at any nesting depth are sanitized unless their key is in
    ``preserve_keys``.  This makes structured test output safe even when a
    framework places credentials in testcase names, failure messages,
    tracebacks, or newly added metadata fields without corrupting trusted
    lifecycle/status fields.
    """
    patterns = tuple(extra_patterns or ())
    preserved = frozenset(preserve_keys or ())
    if isinstance(value, str):
        return sanitize_output(value, extra_patterns=patterns)
    if isinstance(value, Mapping):
        return {
            key: item
            if key in preserved
            else sanitize_structure(
                item,
                extra_patterns=patterns,
                preserve_keys=preserved,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_structure(
                item,
                extra_patterns=patterns,
                preserve_keys=preserved,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            sanitize_structure(
                item,
                extra_patterns=patterns,
                preserve_keys=preserved,
            )
            for item in value
        )
    return value
