"""Canonical hashing helpers shared by persisted AAH contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Return the strict, key-sorted, compact JSON bytes used by AAH hashes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_json_hash(value: Any) -> str:
    """Return SHA-256 over :func:`canonical_json_bytes`.

    Keeping serialization in one function makes the byte contract explicit;
    this produces exactly the same digest as the original implementation.
    """
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
