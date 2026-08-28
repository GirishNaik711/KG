#!/usr/bin/env python3
"""Durable conflict store — the recorded 'issues' the LLM resolves.

Conflicts survive across sessions so Claude can pick them up whenever any skill
next runs. Lives at <rapids_path>/version-control/conflicts.json.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from aah.core.common.io_utils import read_json, write_json
from aah.core.version_control.models import ConflictRecord


@contextmanager
def _file_lock(lock_path: Path, timeout: float = 10.0, poll: float = 0.05):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() - start > timeout:
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


def make_conflict_id(feature_id: str, field: str) -> str:
    """Deterministic id so re-detecting the same conflict updates, not dupes."""
    return f"CF-{feature_id}-{field}"


class ConflictStore:
    def __init__(self, rapids_path: Path):
        self.rapids_path = Path(rapids_path)
        self.dir = self.rapids_path / "version-control"
        self.path = self.dir / "conflicts.json"
        self.lock_path = self.dir / "conflicts.lock"
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            data = read_json(self.path)
            if isinstance(data, dict):
                return data
        return {"conflicts": []}

    def save(self) -> None:
        with _file_lock(self.lock_path):
            write_json(self._data, self.path)

    def _records(self) -> list[dict]:
        return self._data.setdefault("conflicts", [])

    def record(self, conflict: ConflictRecord) -> None:
        """Insert or update a conflict by its deterministic id (in memory)."""
        records = self._records()
        for i, existing in enumerate(records):
            if existing.get("conflict_id") == conflict.conflict_id:
                records[i] = conflict.to_dict()
                return
        records.append(conflict.to_dict())

    def get(self, conflict_id: str) -> ConflictRecord | None:
        for rec in self._records():
            if rec.get("conflict_id") == conflict_id:
                return ConflictRecord.from_dict(rec)
        return None

    def open(self) -> list[ConflictRecord]:
        return [
            ConflictRecord.from_dict(r)
            for r in self._records()
            if r.get("status", "open") == "open"
        ]

    def all(self) -> list[ConflictRecord]:
        return [ConflictRecord.from_dict(r) for r in self._records()]

    def mark_resolved(self, conflict_id: str) -> bool:
        for rec in self._records():
            if rec.get("conflict_id") == conflict_id:
                rec["status"] = "resolved"
                return True
        return False
