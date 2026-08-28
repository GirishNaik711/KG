"""Shared NO-MOCKS test fixtures for version_control.

The MemoryProvider is a REAL TrackerProvider implementation backed by an
in-process dict — not a mock framework. It executes the same code paths the
GitHub provider does, just against memory instead of the network, so engine /
applier / resolve are exercised end-to-end without `gh`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aah.core.common.feature_utils import write_feature_frontmatter
from aah.core.version_control.conflict_store import ConflictStore
from aah.core.version_control.feature_codec import FeatureCodec
from aah.core.version_control.fingerprint import field_values, local_fp, remote_fp
from aah.core.version_control.ledger import SyncLedger
from aah.core.version_control.models import (
    Comment,
    ItemRef,
    RemoteItem,
    WorkItem,
)
from aah.core.version_control.providers.base import (
    ProviderCapabilities,
    TrackerProvider,
)


class MemoryProvider(TrackerProvider):
    """A functional in-memory tracker. Real logic, no network, no mocks."""

    name = "memory"
    capabilities = ProviderCapabilities()

    def __init__(self, items: dict[int, WorkItem] | None = None):
        self.items: dict[int, WorkItem] = dict(items or {})
        self.comments: dict[int, list[Comment]] = {}
        self.pushed: list[tuple[int, str]] = []
        self._next = 100

    def fetch_item(self, ref: ItemRef) -> RemoteItem | None:
        wi = self.items.get(ref.number)
        if wi is None:
            return None
        return RemoteItem(ref=ref, work_item=wi, updated_at="t")

    def fetch_changed(self, refs_since):
        out = {}
        for fid, (ref, _cp) in refs_since.items():
            if ref.number in self.items:
                out[fid] = RemoteItem(ref=ref, work_item=self.items[ref.number], updated_at="t")
        return out

    def fetch_comments(self, ref: ItemRef, since_id):
        out = []
        for c in self.comments.get(ref.number, []):
            if since_id is None or c.id > since_id:
                out.append(c)
        return out

    def create_item(self, item: WorkItem) -> ItemRef:
        n = self._next
        self._next += 1
        self.items[n] = item
        return ItemRef(number=n)

    def update_item(self, ref: ItemRef, item: WorkItem) -> RemoteItem:
        self.items[ref.number] = item
        self.pushed.append((ref.number, item.description))
        return RemoteItem(ref=ref, work_item=item, updated_at="t2")

    def to_work_item(self, remote):
        return remote.work_item if isinstance(remote, RemoteItem) else remote

    def from_work_item(self, item):
        return item


@pytest.fixture
def project(tmp_path: Path):
    """A throwaway .rapids project with helpers to seed feature.md + ledger base."""
    rapids = tmp_path / ".rapids"
    features = rapids / "plan" / "features"
    features.mkdir(parents=True)

    codec = FeatureCodec(rapids)
    ledger = SyncLedger(rapids)
    conflicts = ConflictStore(rapids)

    def write_feature(item: WorkItem, **extra):
        data = {
            "id": item.feature_id,
            "spec_ref": "SPEC-001",
            "description": item.description,
            "dependencies": item.dependencies,
            "acceptance_criteria": item.acceptance_criteria,
            "test_cases": item.test_cases,
            "status": item.status,
        }
        data.update(extra)
        write_feature_frontmatter(data, features / f"{item.feature_id}.md")

    def seed_base(item: WorkItem, number: int = 1):
        ledger.put_entry(
            item.feature_id,
            remote_ref=ItemRef(number=number),
            local_fingerprint=local_fp(item),
            remote_fingerprint=remote_fp(item),
            field_values=field_values(item),
        )
        ledger.save()

    return {
        "rapids": rapids,
        "codec": codec,
        "ledger": ledger,
        "conflicts": conflicts,
        "write_feature": write_feature,
        "seed_base": seed_base,
    }
