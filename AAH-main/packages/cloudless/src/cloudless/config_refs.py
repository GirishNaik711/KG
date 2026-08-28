"""Resolve ${secret:NAME} and ${env:NAME} references inside a parsed config dict.

Walks a dict/list/string tree and replaces references in-place (well, in
a deep copy). The Secrets primitive is constructed lazily so resolution
works in dev (LocalFileBackend) and deployed (Secrets Manager / Secret
Manager) modes alike.

Reference syntax:

    ${secret:NAME}          → resolved via cloudless.Secrets().get("NAME")
    ${env:NAME}             → resolved via os.environ.get("NAME")
    ${env:NAME:default}     → env with a literal default

Strings may contain multiple references mixed with literal text:

    bearer_token: "Bearer ${secret:api_key}"

Unknown refs raise InvalidInputError so typos fail loudly at startup.
"""
from __future__ import annotations

import os
import re
from typing import Any

from cloudless.exceptions import InvalidInputError

_REF_RE = re.compile(r"\$\{(secret|env):([^:}]+)(?::([^}]*))?\}")


def resolve_refs(data: Any, *, secrets: Any = None) -> Any:
    """Walk `data` and substitute every `${secret:..}` / `${env:..}` reference.

    Args:
        data: A dict / list / string (or anything else, which is returned
            unchanged). Returns a copy with references replaced.
        secrets: Optional pre-built `cloudless.Secrets` instance. If None
            and a secret reference is encountered, one is constructed lazily
            using the default backend.
    """
    if isinstance(data, dict):
        return {k: resolve_refs(v, secrets=secrets) for k, v in data.items()}
    if isinstance(data, list):
        return [resolve_refs(v, secrets=secrets) for v in data]
    if isinstance(data, str):
        return _resolve_string(data, secrets=secrets)
    return data


def _resolve_string(s: str, *, secrets: Any) -> str:
    if "${" not in s:
        return s

    # Process refs one-by-one
    def _repl(m: re.Match) -> str:
        kind, name, default = m.group(1), m.group(2), m.group(3)
        if kind == "env":
            value = os.environ.get(name, default)
            if value is None:
                raise InvalidInputError(
                    f"unresolved ${{env:{name}}} — set env var or provide default"
                )
            return value
        if kind == "secret":
            value = _resolve_secret(name, secrets=secrets, default=default)
            if value is None:
                raise InvalidInputError(
                    f"unresolved ${{secret:{name}}} — no such secret and no default"
                )
            return value
        raise InvalidInputError(f"unknown reference kind {kind!r}")

    return _REF_RE.sub(_repl, s)


_secrets_singleton: Any = None


def _resolve_secret(name: str, *, secrets: Any, default: str | None) -> str | None:
    """Look up `name` via the Secrets primitive."""
    global _secrets_singleton
    s = secrets or _secrets_singleton
    if s is None:
        from cloudless.catalog.secrets import Secrets
        _secrets_singleton = Secrets()
        s = _secrets_singleton
    try:
        return s.get(name)
    except Exception:
        return default
