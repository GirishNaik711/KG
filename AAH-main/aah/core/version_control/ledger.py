#!/usr/bin/env python3
"""Sync ledger — the engine's durable memory.

Stores, per feature: the remote ref, the last-synced "base" (local + remote
fingerprints and the per-field values used for the 3-way), the remote_updated_at
pre-filter cursor, the last seen comment id, and lifecycle/status_label.

Lives at <rapids_path>/version-control/sync-state.json. Writes take a simple
file lock so a concurrent RAPIDS write cannot corrupt it.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_json, write_json
from aah.core.version_control.models import Checkpoint, ItemRef

SCHEMA_VERSION = "1.0"


@contextmanager
def _file_lock(lock_path: Path, timeout: float = 10.0, poll: float = 0.05):
    """A minimal cross-process lock using O_CREAT|O_EXCL. NO external deps."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() - start > timeout:
                # Stale lock fallback: break it rather than hang forever.
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            time.sleep(poll)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


class SyncLedger:
    def __init__(self, rapids_path: Path):
        self.rapids_path = Path(rapids_path)
        self.dir = self.rapids_path / "version-control"
        self.path = self.dir / "sync-state.json"
        self.lock_path = self.dir / "sync-state.lock"
        self._data: dict[str, Any] = self._load()

    # --- load / save --------------------------------------------------------
    # Maps old internal status values to canonical lifecycle states.
    _LIFECYCLE_NORMALISE = {
        "pending":     "planned",
        "in_progress": "implementing",
        "complete":    "done",
        "passed":      "done",
        "failed":      "rework",
    }

    def _load(self) -> dict:
        if self.path.exists():
            data = read_json(self.path)
            if isinstance(data, dict):
                self._normalise_lifecycle(data)
                return data
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": None,
            "repo": None,
            "features": {},
        }

    def _normalise_lifecycle(self, data: dict) -> None:
        """Rewrite stale internal status values to canonical lifecycle states in place."""
        for entry in data.get("features", {}).values():
            lc = entry.get("lifecycle")
            if lc in self._LIFECYCLE_NORMALISE:
                entry["lifecycle"] = self._LIFECYCLE_NORMALISE[lc]
            fv = entry.get("base", {}).get("field_values", {})
            if fv.get("status") in self._LIFECYCLE_NORMALISE:
                fv["status"] = self._LIFECYCLE_NORMALISE[fv["status"]]

    def save(self) -> None:
        with _file_lock(self.lock_path):
            write_json(self._data, self.path)

    def init(self, provider: str, repo: str | None) -> None:
        self._data.setdefault("schema_version", SCHEMA_VERSION)
        self._data["provider"] = provider
        self._data["repo"] = repo
        self._data.setdefault("features", {})
        self.save()

    # --- entries ------------------------------------------------------------
    def get(self, feature_id: str) -> dict | None:
        return self._data.get("features", {}).get(feature_id)

    def all_feature_ids(self) -> list[str]:
        return list(self._data.get("features", {}).keys())

    def has(self, feature_id: str) -> bool:
        return feature_id in self._data.get("features", {})

    def remote_ref(self, feature_id: str) -> ItemRef | None:
        entry = self.get(feature_id)
        if not entry:
            return None
        return ItemRef.from_dict(entry.get("remote_ref"))

    def checkpoint(self, feature_id: str) -> Checkpoint:
        entry = self.get(feature_id) or {}
        return Checkpoint(
            remote_updated_at=entry.get("remote_updated_at"),
            last_seen_comment_id=entry.get("last_seen_comment_id"),
        )

    def base(self, feature_id: str) -> dict:
        """Return the last-synced base block (fingerprints + field_values)."""
        entry = self.get(feature_id) or {}
        return entry.get("base", {})

    def put_entry(
        self,
        feature_id: str,
        *,
        remote_ref: ItemRef | None = None,
        local_fingerprint: str | None = None,
        remote_fingerprint: str | None = None,
        field_values: dict | None = None,
        remote_updated_at: str | None = None,
        last_seen_comment_id: int | None = None,
        lifecycle: str | None = None,
        status_label: str | None = None,
    ) -> None:
        """Create or update a feature's ledger entry (in memory; call save())."""
        features = self._data.setdefault("features", {})
        entry = features.setdefault(feature_id, {})
        if remote_ref is not None:
            entry["remote_ref"] = remote_ref.to_dict()
        base = entry.setdefault("base", {})
        if local_fingerprint is not None:
            base["local_fingerprint"] = local_fingerprint
        if remote_fingerprint is not None:
            base["remote_fingerprint"] = remote_fingerprint
        if field_values is not None:
            base["field_values"] = field_values
        if remote_updated_at is not None:
            entry["remote_updated_at"] = remote_updated_at
        if last_seen_comment_id is not None:
            entry["last_seen_comment_id"] = last_seen_comment_id
        if lifecycle is not None:
            entry["lifecycle"] = lifecycle
        if status_label is not None:
            entry["status_label"] = status_label

    def refresh_base(
        self,
        feature_id: str,
        *,
        local_fingerprint: str,
        remote_fingerprint: str,
        field_values: dict,
        remote_updated_at: str | None = None,
        last_seen_comment_id: int | None = None,
    ) -> None:
        """Align base to the now-agreed values so the next delta is a NO-OP.

        This is the anti-"conflict reappears" / anti-echo-loop step: after any
        apply or resolve, both sides + base agree, so the field stops deltaing.
        """
        self.put_entry(
            feature_id,
            local_fingerprint=local_fingerprint,
            remote_fingerprint=remote_fingerprint,
            field_values=field_values,
            remote_updated_at=remote_updated_at,
            last_seen_comment_id=last_seen_comment_id,
        )
