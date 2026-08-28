#!/usr/bin/env python3
"""Canonical, provider-neutral domain model for version_control.

Everything the core (engine, applier, ledger, codec, reporter) speaks. Provider
adapters translate native tracker objects (GitHub issues, Jira tickets) to and
from these types. The core never sees a GitHub issue or a Jira ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# --- Field classification ---------------------------------------------------
# Drives routing + protection on the way back into RAPIDS state. The applier
# decides where an inbound/resolved value is allowed to land based on this.
#   cosmetic   -> feature.md description; safe to auto-fold
#   structural -> AC / tests / deps / layer; DAG-affecting, routes via rapids-plan re-entry
#   status     -> lifecycle; only status/passes may touch feature-list.json
FIELD_CLASS: dict[str, str] = {
    "description": "cosmetic",
    "acceptance_criteria": "structural",
    "test_cases": "structural",
    "dependencies": "structural",
    "layer": "structural",
    "status": "status",
}

# The exact set of feature.md fields this module synchronises. Anything not in
# this list (knowledge_used, codemap_context, expertise, ...) is never synced and
# never contributes to a fingerprint.
SYNCED_FIELDS: list[str] = list(FIELD_CLASS.keys())

# Canonical lifecycle. Providers map their native states/labels to these.
LIFECYCLE: list[str] = [
    "planned",
    "queued",
    "implementing",
    "in_qa",
    "done",
    "blocked",
    "rework",
    "cancelled",
]


def field_class(name: str) -> str:
    """Return the classification of a synced field; defaults to 'cosmetic'."""
    return FIELD_CLASS.get(name, "cosmetic")


# --- Identity / references --------------------------------------------------
@dataclass
class ItemRef:
    """A reference to a remote tracker item, provider-neutral.

    For GitHub: number=issue number, node_id=GraphQL node id.
    For Jira: number can hold the numeric part, key holds e.g. "PROJ-12".
    """
    number: int | None = None
    node_id: str | None = None
    key: str | None = None
    url: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ItemRef | None":
        if not d:
            return None
        return cls(
            number=d.get("number") or d.get("issue"),
            node_id=d.get("node_id"),
            key=d.get("key"),
            url=d.get("url"),
        )


# --- The neutral feature representation -------------------------------------
@dataclass
class WorkItem:
    """Provider-neutral representation of a RAPIDS feature.

    Only SYNCED_FIELDS participate in fingerprinting and reconciliation. wave is
    projection-only metadata (milestone/sprint); it is never a conflict source.
    """
    feature_id: str
    title: str = ""
    description: str = ""
    acceptance_criteria: list = field(default_factory=list)
    test_cases: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    layer: str | None = None
    status: str = "planned"
    wave: int | None = None

    def synced_fields(self) -> dict[str, Any]:
        """Return only the fields this module synchronises (for hashing/compare)."""
        return {name: getattr(self, name) for name in SYNCED_FIELDS}

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Comment:
    """A tracker comment, provider-neutral."""
    id: int
    author: str
    body: str
    created_at: str = ""


# --- Remote snapshot the provider hands the engine --------------------------
@dataclass
class RemoteItem:
    """What a provider returns for a single fetched tracker item.

    work_item is the canonical projection (via provider.to_work_item); ref ties
    it back to the tracker; updated_at is the coarse pre-filter cursor.
    """
    ref: ItemRef
    work_item: WorkItem
    updated_at: str = ""
    raw: dict = field(default_factory=dict)  # provider-specific blob for debugging


# --- Delta classification ---------------------------------------------------
class DeltaType(str, Enum):
    NOOP = "noop"
    OUTBOUND = "outbound"      # local changed, remote did not -> push feature.md to tracker
    INBOUND = "inbound"        # remote changed, local did not -> fold tracker into feature.md
    CONFLICT = "conflict"      # both changed -> record + LLM resolves
    DIRECTIVE = "directive"    # new human comment(s)
    OFFLINE = "offline"        # tracker unreachable; read-only, queue outbound


@dataclass
class FieldDelta:
    """Per-field verdict for one feature."""
    field: str
    field_class: str
    base: Any
    local: Any
    remote: Any
    direction: DeltaType

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "field_class": self.field_class,
            "base": self.base,
            "local": self.local,
            "remote": self.remote,
            "direction": self.direction.value,
        }


@dataclass
class Delta:
    """One feature's reconciliation verdict, aggregated from its FieldDeltas."""
    feature_id: str
    type: DeltaType
    field_deltas: list[FieldDelta] = field(default_factory=list)
    remote_ref: ItemRef | None = None
    comments: list[Comment] = field(default_factory=list)
    note: str = ""

    def conflicting_fields(self) -> list[FieldDelta]:
        return [fd for fd in self.field_deltas if fd.direction == DeltaType.CONFLICT]

    def to_dict(self) -> dict:
        return {
            "feature_id": self.feature_id,
            "type": self.type.value,
            "field_deltas": [fd.to_dict() for fd in self.field_deltas],
            "remote_ref": self.remote_ref.to_dict() if self.remote_ref else None,
            "comments": [asdict(c) for c in self.comments],
            "note": self.note,
        }


@dataclass
class DeltaSet:
    """Engine output for a scope of features. Pure data; mutates nothing."""
    deltas: list[Delta] = field(default_factory=list)
    offline: bool = False

    def conflicts(self) -> list[Delta]:
        return [d for d in self.deltas if d.type == DeltaType.CONFLICT]

    def auto_applicable(self) -> list[Delta]:
        return [
            d
            for d in self.deltas
            if d.type in (DeltaType.OUTBOUND, DeltaType.INBOUND, DeltaType.DIRECTIVE)
        ]

    def by_type(self, dtype: DeltaType) -> list[Delta]:
        return [d for d in self.deltas if d.type == dtype]

    def to_dict(self) -> dict:
        return {
            "offline": self.offline,
            "deltas": [d.to_dict() for d in self.deltas],
        }


# --- Conflict record (durable) ----------------------------------------------
@dataclass
class ConflictRecord:
    """A both-sides-changed field, recorded for the LLM to resolve later."""
    conflict_id: str
    feature_id: str
    field: str
    field_class: str
    base: Any
    local: Any
    remote: Any
    remote_ref: ItemRef | None = None
    detected_at: str = ""
    status: str = "open"  # open | resolved

    def to_dict(self) -> dict:
        d = asdict(self)
        d["remote_ref"] = self.remote_ref.to_dict() if self.remote_ref else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ConflictRecord":
        return cls(
            conflict_id=d["conflict_id"],
            feature_id=d["feature_id"],
            field=d["field"],
            field_class=d["field_class"],
            base=d.get("base"),
            local=d.get("local"),
            remote=d.get("remote"),
            remote_ref=ItemRef.from_dict(d.get("remote_ref")),
            detected_at=d.get("detected_at", ""),
            status=d.get("status", "open"),
        )


@dataclass
class Checkpoint:
    """The cursor a provider uses to pre-filter 'what changed since last sync'."""
    remote_updated_at: str | None = None
    last_seen_comment_id: int | None = None
