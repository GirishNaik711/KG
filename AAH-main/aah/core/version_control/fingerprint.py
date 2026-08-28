#!/usr/bin/env python3
"""Content fingerprints — the authoritative 'what changed' mechanism.

Timestamps (issue.updatedAt) are only a coarse pre-filter (handled in the
provider). The real change decision is made here by hashing a *normalized
projection of the synced fields*, so cosmetic rendering noise never creates a
false delta and our own pushes do not echo back as remote changes.

Invariant the normaliser must guarantee: the round-trip
    feature.md -> issue body -> parse back -> re-hash
yields the SAME fingerprint when meaning did not change.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from aah.core.version_control.models import SYNCED_FIELDS, WorkItem

# Fields whose list order carries no meaning, so we sort before hashing.
# acceptance_criteria order is treated as insignificant; dependencies likewise
# (the DAG cares about the set, not the order). test_cases are dicts keyed by
# id, so we sort them by id. description/layer/status are scalars.
_ORDER_INSENSITIVE = {"acceptance_criteria", "dependencies"}


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        # Normalise line endings and strip trailing whitespace per line, then
        # strip the whole value. This absorbs editor/markdown round-trip noise.
        lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join(line.rstrip() for line in lines).strip()
    return value


def _normalize_value(name: str, value: Any) -> Any:
    if value is None:
        # Treat None and empty-collection as the same canonical "empty".
        return None
    if isinstance(value, list):
        items = [_normalize_value(name, v) for v in value]
        if name in _ORDER_INSENSITIVE:
            # Sort by canonical JSON so ordering differences do not move the hash.
            items = sorted(items, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        elif name == "test_cases":
            def _key(x: Any) -> str:
                if isinstance(x, dict) and "id" in x:
                    # Outer test-case entries: sort by id.
                    return str(x["id"])
                # Nested id-less entries (e.g. test_cases[].inputs): sort by
                # canonical JSON so reordering never moves the hash.
                return json.dumps(x, sort_keys=True, ensure_ascii=False)

            items = sorted(items, key=_key)
        return items
    if isinstance(value, dict):
        return {k: _normalize_value(name, v) for k, v in sorted(value.items())}
    return _normalize_scalar(value)


def normalize(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical, normalized projection of the synced fields only.

    Accepts any dict; ignores keys outside SYNCED_FIELDS so non-synced frontmatter keys
    (knowledge_used, codemap_context, expertise, ...) can never move a hash.
    """
    out: dict[str, Any] = {}
    for name in SYNCED_FIELDS:
        out[name] = _normalize_value(name, fields.get(name))
    return out


def _hash(normalized: dict[str, Any]) -> str:
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def fingerprint_fields(fields: dict[str, Any]) -> str:
    """Stable sha256 over the normalized synced-fields projection."""
    return _hash(normalize(fields))


def local_fp(item: WorkItem) -> str:
    """Fingerprint of a feature.md-derived WorkItem (the local side)."""
    return fingerprint_fields(item.synced_fields())


def remote_fp(item: WorkItem) -> str:
    """Fingerprint of a tracker-derived WorkItem (the remote side).

    Same normalisation as local_fp by design: both sides must hash identically
    when meaning matches, which is what makes the 3-way comparison honest.
    """
    return fingerprint_fields(item.synced_fields())


def field_values(item: WorkItem) -> dict[str, Any]:
    """The per-field normalized values stored as ledger 'base' for the 3-way.

    Used both to display BASE in conflicts and to classify which field moved.
    """
    return normalize(item.synced_fields())
