#!/usr/bin/env python3
"""The TrackerProvider port — the single seam between core and tracker.

The engine and applier call ONLY these methods, and only with the canonical
model (WorkItem / ItemRef / Comment). No GitHub or Jira specifics leak across
this boundary. Adding a tracker = implementing this ABC + registering it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from aah.core.version_control.models import (
    Checkpoint,
    Comment,
    ItemRef,
    RemoteItem,
    WorkItem,
)


@dataclass
class ProviderCapabilities:
    """What surfaces a tracker supports. The core consults these instead of
    branching on provider name, so a tracker missing a surface degrades that
    field to a no-op rather than erroring."""
    comments: bool = True
    milestones: bool = True
    labels: bool = True
    dependency_links: bool = True


class ProviderError(Exception):
    """Raised on unrecoverable provider/transport errors."""


class ProviderOffline(ProviderError):
    """Raised when the tracker is unreachable / rate-limited. The engine
    catches this and emits an OFFLINE delta set instead of failing the run."""


class TrackerProvider(ABC):
    name: str = "base"
    capabilities: ProviderCapabilities = ProviderCapabilities()

    # --- read side (for delta) ---------------------------------------------
    @abstractmethod
    def fetch_item(self, ref: ItemRef) -> RemoteItem | None:
        """Fetch a single tracked item, or None if it no longer exists."""

    @abstractmethod
    def fetch_changed(self, refs_since: dict[str, tuple[ItemRef, Checkpoint]]) -> dict[str, RemoteItem]:
        """Fetch items that changed since their per-feature checkpoint.

        Input maps feature_id -> (ref, checkpoint). Implementations MAY use the
        checkpoint.remote_updated_at as a coarse pre-filter, but correctness must
        not depend on it (the engine re-hashes content regardless). Returns
        feature_id -> RemoteItem for every item that could be fetched.
        """

    @abstractmethod
    def fetch_comments(self, ref: ItemRef, since_id: int | None) -> list[Comment]:
        """Return comments newer than since_id (all if None)."""

    # --- write side (projection only — always feature.md-sourced) ----------------
    @abstractmethod
    def create_item(self, item: WorkItem) -> ItemRef:
        """Create a tracker item from a WorkItem; return its ref."""

    @abstractmethod
    def update_item(self, ref: ItemRef, item: WorkItem) -> RemoteItem:
        """Project a WorkItem onto an existing item; return the refreshed item
        (so the applier can recompute the remote fingerprint without a re-fetch)."""

    # --- adapter translation (native <-> canonical) -----------------------
    @abstractmethod
    def to_work_item(self, remote: object) -> WorkItem:
        """Native tracker object -> canonical WorkItem."""

    @abstractmethod
    def from_work_item(self, item: WorkItem) -> object:
        """Canonical WorkItem -> native tracker payload."""

    # --- health ------------------------------------------------------------
    def validate_auth(self) -> tuple[bool, str]:
        """Return (ok, message). Default: assume ok. Override to check creds."""
        return True, "ok"
