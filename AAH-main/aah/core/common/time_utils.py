"""Small RFC3339 helpers for persisted verification contracts."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339_now() -> str:
    return utc_now().isoformat()


def parse_rfc3339(
    value: object,
    *,
    allow_naive: bool = False,
    normalize_utc: bool = True,
) -> datetime | None:
    """Parse RFC3339, optionally retaining the legacy naive-as-UTC behavior."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not allow_naive:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) if normalize_utc else parsed


def parse_legacy_rfc3339(value: object) -> datetime | None:
    return parse_rfc3339(value, allow_naive=True, normalize_utc=False)
