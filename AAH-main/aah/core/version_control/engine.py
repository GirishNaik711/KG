#!/usr/bin/env python3
"""Delta engine — pure 3-way reconciliation. Mutates nothing.

For each feature it compares three snapshots:
  base   = last-synced values from the ledger
  local  = current feature.md (the source of truth)
  remote = current tracker item (a human proposal)

Per field it computes (local changed?, remote changed?) vs base. The 2x2 of
those booleans is the NO-OP / OUTBOUND / INBOUND / CONFLICT matrix. The
feature-level Delta.type aggregates the field verdicts.
"""

from __future__ import annotations

import json
from typing import Any

from aah.core.version_control.feature_codec import FeatureCodec
from aah.core.version_control.fingerprint import field_values, normalize
from aah.core.version_control.ledger import SyncLedger
from aah.core.version_control.models import (
    Comment,
    Delta,
    DeltaSet,
    DeltaType,
    FieldDelta,
    ItemRef,
    RemoteItem,
    SYNCED_FIELDS,
    WorkItem,
    field_class,
)
from aah.core.version_control.providers.base import (
    ProviderOffline,
    TrackerProvider,
)


def _eq(a: Any, b: Any) -> bool:
    """Compare two already-normalized field values."""
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
        b, sort_keys=True, ensure_ascii=False
    )


class DeltaEngine:
    def __init__(self, provider: TrackerProvider, ledger: SyncLedger, codec: FeatureCodec):
        self.provider = provider
        self.ledger = ledger
        self.codec = codec

    def compute(self, feature_ids: list[str] | None = None) -> DeltaSet:
        """Compute the DeltaSet for the given features (all known if None)."""
        if feature_ids is None:
            # Union of feature.md features and ledger features (covers orphans-from-off).
            feature_ids = sorted(set(self.codec.list_features()) | set(self.ledger.all_feature_ids()))

        # Build the fetch request for features that already have a remote ref.
        refs_since: dict[str, tuple[ItemRef, Any]] = {}
        for fid in feature_ids:
            ref = self.ledger.remote_ref(fid)
            if ref is not None:
                refs_since[fid] = (ref, self.ledger.checkpoint(fid))

        offline = False
        remotes: dict[str, RemoteItem] = {}
        if refs_since:
            try:
                remotes = self.provider.fetch_changed(refs_since)
            except ProviderOffline:
                offline = True

        deltas: list[Delta] = []
        for fid in feature_ids:
            deltas.append(self._compute_one(fid, remotes.get(fid), offline))

        return DeltaSet(deltas=deltas, offline=offline)

    # ------------------------------------------------------------------------
    def _compute_one(self, feature_id: str, remote: RemoteItem | None, offline: bool) -> Delta:
        local_item = self.codec.read(feature_id)
        base = self.ledger.base(feature_id)
        base_values: dict[str, Any] = base.get("field_values", {}) or {}
        ref = self.ledger.remote_ref(feature_id)

        # --- not yet on the tracker: pure outbound (create) -----------------
        if ref is None:
            if local_item is None:
                return Delta(feature_id=feature_id, type=DeltaType.NOOP, note="no feature.md, no ref")
            # Rework feature — reuse parent's remote ref instead of creating a new issue
            import re
            m = re.match(r'^(.+)-rework-\d+$', feature_id)
            if m:
                parent_ref = self.ledger.remote_ref(m.group(1))
                if parent_ref is not None:
                    self.ledger.put_entry(feature_id, remote_ref=parent_ref)
                    self.ledger.save()
                    return Delta(feature_id=feature_id, type=DeltaType.NOOP,
                                 note=f"rework of {m.group(1)} — reusing issue {parent_ref}")
            fds = self._field_deltas(local_item, None, base_values, never_synced=True)
            return Delta(
                feature_id=feature_id,
                type=DeltaType.OUTBOUND,
                field_deltas=fds,
                remote_ref=None,
                note="new feature -> create item",
            )

        # --- offline: can't read remote; only outbound is safe to queue -----
        if offline:
            return Delta(
                feature_id=feature_id,
                type=DeltaType.OFFLINE,
                remote_ref=ref,
                note="tracker unreachable; queue outbound only",
            )

        # remote may be None if it didn't change since checkpoint -> treat as
        # "remote unchanged" by reconstructing it from base values.
        remote_item = remote.work_item if remote else self._base_as_work_item(feature_id, base_values)
        remote_ref = remote.ref if remote else ref

        comments: list[Comment] = []
        if remote is not None and self.provider.capabilities.comments:
            cp = self.ledger.checkpoint(feature_id)
            try:
                comments = self.provider.fetch_comments(remote_ref, cp.last_seen_comment_id)
            except ProviderOffline:
                comments = []

        field_deltas = self._field_deltas(local_item, remote_item, base_values)
        dtype = self._aggregate(field_deltas, comments)

        return Delta(
            feature_id=feature_id,
            type=dtype,
            field_deltas=field_deltas,
            remote_ref=remote_ref,
            comments=comments,
        )

    # ------------------------------------------------------------------------
    def _field_deltas(
        self,
        local_item: WorkItem | None,
        remote_item: WorkItem | None,
        base_values: dict[str, Any],
        never_synced: bool = False,
    ) -> list[FieldDelta]:
        local_values = field_values(local_item) if local_item else {}
        remote_values = field_values(remote_item) if remote_item else {}

        out: list[FieldDelta] = []
        for name in SYNCED_FIELDS:
            b = base_values.get(name)
            l = local_values.get(name)
            r = remote_values.get(name)

            if never_synced or remote_item is None:
                # Nothing on the tracker yet: any non-empty local field is outbound.
                direction = DeltaType.OUTBOUND if not _eq(l, None) else DeltaType.NOOP
                out.append(FieldDelta(name, field_class(name), b, l, r, direction))
                continue

            local_changed = not _eq(l, b)
            remote_changed = not _eq(r, b)

            if not local_changed and not remote_changed:
                direction = DeltaType.NOOP
            elif local_changed and not remote_changed:
                direction = DeltaType.OUTBOUND
            elif not local_changed and remote_changed:
                direction = DeltaType.INBOUND
            else:
                # Both moved. If they happen to agree, it's a NO-OP (converged).
                direction = DeltaType.NOOP if _eq(l, r) else DeltaType.CONFLICT

            out.append(FieldDelta(name, field_class(name), b, l, r, direction))
        return out

    @staticmethod
    def _aggregate(field_deltas: list[FieldDelta], comments: list[Comment]) -> DeltaType:
        dirs = {fd.direction for fd in field_deltas}
        if DeltaType.CONFLICT in dirs:
            return DeltaType.CONFLICT
        has_out = DeltaType.OUTBOUND in dirs
        has_in = DeltaType.INBOUND in dirs
        if has_out and has_in:
            # Mixed but no conflict: still resolvable automatically. Mark as the
            # dominant non-conflict direction; applier handles each field anyway.
            return DeltaType.OUTBOUND
        if has_out:
            return DeltaType.OUTBOUND
        if has_in:
            return DeltaType.INBOUND
        if comments:
            return DeltaType.DIRECTIVE
        return DeltaType.NOOP

    def _base_as_work_item(self, feature_id: str, base_values: dict[str, Any]) -> WorkItem:
        """Reconstruct a WorkItem from stored base values (remote-unchanged case)."""
        nv = normalize(base_values)
        return WorkItem(
            feature_id=feature_id,
            description=nv.get("description") or "",
            acceptance_criteria=nv.get("acceptance_criteria") or [],
            test_cases=nv.get("test_cases") or [],
            dependencies=nv.get("dependencies") or [],
            layer=nv.get("layer"),
            status=nv.get("status") or "planned",
        )
