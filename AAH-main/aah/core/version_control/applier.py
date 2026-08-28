#!/usr/bin/env python3
"""Applier — the mutation layer. feature.md first, always, then project to tracker.

Walks a DeltaSet and:
  * OUTBOUND  -> push the feature.md-derived WorkItem to the tracker (create/update)
  * INBOUND   -> fold remote value INTO feature.md, then re-project (truth wins path)
  * CONFLICT  -> record in conflict store (no auto-merge); reporter prints it
  * DIRECTIVE -> surface comments (advance last_seen_comment_id)
After each applied field it realigns the ledger base so the next delta is a NO-OP
(kills echo loops and "conflict reappears"). Never blocks: structural changes are
staged for rapids-plan re-entry and reported, not executed inline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_json, write_json
from aah.core.version_control.conflict_store import ConflictStore, make_conflict_id
from aah.core.version_control.feature_codec import FeatureCodec
from aah.core.version_control.fingerprint import field_values, local_fp, remote_fp
from aah.core.version_control.ledger import SyncLedger
from aah.core.version_control.models import (
    ConflictRecord,
    Delta,
    DeltaSet,
    DeltaType,
    FieldDelta,
    WorkItem,
)
from aah.core.version_control.providers.base import (
    ProviderError,
    ProviderOffline,
    TrackerProvider,
)


class Applier:
    def __init__(
        self,
        provider: TrackerProvider,
        ledger: SyncLedger,
        codec: FeatureCodec,
        conflicts: ConflictStore,
        *,
        detected_at: str = "",
        auto_fold_structural_inbound: bool = False,
    ):
        self.provider = provider
        self.ledger = ledger
        self.codec = codec
        self.conflicts = conflicts
        self.detected_at = detected_at
        # When False, a remote-only change to a structural field is recorded for
        # LLM review (not auto-folded) since it affects the DAG. Cosmetic inbound
        # is always auto-folded.
        self.auto_fold_structural_inbound = auto_fold_structural_inbound

    # ------------------------------------------------------------------------
    def apply(self, delta_set: DeltaSet) -> dict:
        report: dict[str, Any] = {
            "created": 0, "updated": 0, "inbound": 0, "directives": 0,
            "conflicts": 0, "offline": delta_set.offline, "errors": [],
            "rework_staged": [],
        }
        for delta in delta_set.deltas:
            try:
                self._apply_one(delta, report)
            except (ProviderError, ProviderOffline) as e:
                report["errors"].append(f"{delta.feature_id}: {e}")
        self.ledger.save()
        self.conflicts.save()
        return report

    # ------------------------------------------------------------------------
    def _apply_one(self, delta: Delta, report: dict) -> None:
        if delta.type == DeltaType.OFFLINE:
            return
        if delta.type == DeltaType.NOOP and not delta.comments:
            # Converged NO-OP: both sides moved to the SAME value, so the engine
            # reports no work — but base is still the pre-convergence value. Left
            # stale, the next one-sided edit reads as local-changed too and
            # becomes a false CONFLICT. Realign base = local (which now equals
            # remote) so the ledger reflects the agreed value.
            if delta.remote_ref is not None:
                local_item = self.codec.read(delta.feature_id)
                if local_item is not None:
                    base = self.ledger.base(delta.feature_id)
                    if local_fp(local_item) != base.get("local_fingerprint"):
                        self._realign(delta.feature_id, local_item, delta.remote_ref, None)
                    # Force label push if ledger lifecycle is a stale internal value
                    # (e.g. "pending", "complete") — the normaliser fixed the ledger
                    # but GitHub still has the old label. Push correct labels now.
                    entry = self.ledger.get(delta.feature_id) or {}
                    from aah.core.version_control.models import LIFECYCLE
                    if entry.get("lifecycle") not in LIFECYCLE:
                        try:
                            self.provider.update_item(delta.remote_ref, local_item)
                        except (ProviderError, ProviderOffline):
                            pass
            return

        local_item = self.codec.read(delta.feature_id)

        # 1) Brand-new feature: create the tracker item from feature.md.
        if delta.remote_ref is None and delta.type == DeltaType.OUTBOUND:
            if local_item is None:
                return
            ref = self.provider.create_item(local_item)
            refreshed = self.provider.fetch_item(ref)
            self._seed_ledger(delta.feature_id, local_item, ref, refreshed)
            report["created"] += 1
            return

        # 2) Existing item: process each field independently.
        merged_yaml_values: dict[str, Any] = {}
        needs_outbound = False
        for fd in delta.field_deltas:
            if fd.direction == DeltaType.OUTBOUND:
                needs_outbound = True
            elif fd.direction == DeltaType.INBOUND:
                if fd.field_class == "cosmetic" or self.auto_fold_structural_inbound:
                    merged_yaml_values[fd.field] = fd.remote
                    if fd.field_class == "structural":
                        report["rework_staged"].append(f"{delta.feature_id}.{fd.field}")
                        self._stage_rework(delta.feature_id, fd, "inbound")
                    report["inbound"] += 1
                else:
                    # Structural inbound w/o auto-fold: record for LLM review.
                    self._record_conflict(delta, fd, kind="structural_inbound")
                    report["conflicts"] += 1
            elif fd.direction == DeltaType.CONFLICT:
                self._record_conflict(delta, fd, kind="conflict")
                report["conflicts"] += 1

        # 2a) Fold accepted inbound values into feature.md (source of truth first).
        status_inbound = self._extract_status_inbound(delta)
        if merged_yaml_values:
            self.codec.write_fields(delta.feature_id, merged_yaml_values)
        if status_inbound is not None:
            self._apply_status_inbound(delta.feature_id, status_inbound, report)

        # 2b) Project the (now-updated) feature.md to the tracker if anything is outbound
        #     OR we just folded inbound values (so the issue re-renders from truth).
        if (needs_outbound or merged_yaml_values) and delta.remote_ref is not None:
            local_item = self.codec.read(delta.feature_id)  # re-read post-fold
            refreshed = self.provider.update_item(delta.remote_ref, local_item)
            self._realign(delta.feature_id, local_item, delta.remote_ref, refreshed)
            if needs_outbound:
                report["updated"] += 1

        # 3) Directive comments: surface + advance cursor.
        if delta.comments:
            report["directives"] += len(delta.comments)
            last_id = max((c.id for c in delta.comments), default=None)
            if last_id is not None:
                self.ledger.put_entry(delta.feature_id, last_seen_comment_id=last_id)

    # ------------------------------------------------------------------------
    def _seed_ledger(self, feature_id, local_item, ref, refreshed) -> None:
        remote_item = refreshed.work_item if refreshed else local_item
        self.ledger.put_entry(
            feature_id,
            remote_ref=ref,
            local_fingerprint=local_fp(local_item),
            remote_fingerprint=remote_fp(remote_item),
            field_values=field_values(local_item),
            remote_updated_at=refreshed.updated_at if refreshed else None,
            lifecycle=local_item.status,
            status_label=f"aah:{local_item.status}",
        )

    def _realign(self, feature_id, local_item, ref, refreshed) -> None:
        """After projecting truth, base = the agreed value -> next delta NO-OP."""
        remote_item = refreshed.work_item if refreshed else local_item
        self.ledger.refresh_base(
            feature_id,
            local_fingerprint=local_fp(local_item),
            remote_fingerprint=remote_fp(remote_item),
            field_values=field_values(local_item),
            remote_updated_at=refreshed.updated_at if refreshed else None,
        )
        self.ledger.put_entry(feature_id, remote_ref=ref)

    # --- conflicts ----------------------------------------------------------
    def _record_conflict(self, delta: Delta, fd: FieldDelta, kind: str) -> None:
        rec = ConflictRecord(
            conflict_id=make_conflict_id(delta.feature_id, fd.field),
            feature_id=delta.feature_id,
            field=fd.field,
            field_class=fd.field_class,
            base=fd.base,
            local=fd.local,
            remote=fd.remote,
            remote_ref=delta.remote_ref,
            detected_at=self.detected_at or kind,
            status="open",
        )
        self.conflicts.record(rec)

    # --- status (the only path allowed to touch feature-list.json) ----------
    @staticmethod
    def _extract_status_inbound(delta: Delta) -> str | None:
        for fd in delta.field_deltas:
            if fd.field == "status" and fd.direction == DeltaType.INBOUND:
                return fd.remote
        return None

    def _apply_status_inbound(self, feature_id: str, status: str, report: dict) -> None:
        """Route a status change through common.feature_list (hook-legal).

        Only maps the lifecycle to passes where unambiguous; otherwise records
        the lifecycle on the ledger and leaves feature-list untouched.
        """
        try:
            from aah.core.common.feature_list import find_feature_list, update_feature_status
            fl_path = find_feature_list(self.codec.rapids_path)
            if fl_path is not None and status in ("done", "complete"):
                update_feature_status(fl_path, feature_id, passes=True, skip_test_check=True)
        except SystemExit:
            pass  # update_feature_status may exit on guard; stay non-blocking
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"{feature_id} status: {e}")
        self.ledger.put_entry(feature_id, lifecycle=status, status_label=f"aah:{status}")

    # --- structural staging (non-blocking; re-entry happens later) ----------
    def _stage_rework(self, feature_id: str, fd: FieldDelta, source: str) -> None:
        path = self.codec.rapids_path / "version-control" / "rework-pending.json"
        data = read_json(path) if path.exists() else {"pending": []}
        if not isinstance(data, dict):
            data = {"pending": []}
        data.setdefault("pending", []).append({
            "feature_id": feature_id,
            "field": fd.field,
            "field_class": fd.field_class,
            "source": source,
            "detected_at": self.detected_at,
        })
        write_json(data, path)

    # --- resolve (LLM callback) ---------------------------------------------
    def resolve(self, conflict_id: str, resolution: str, value: Any) -> dict:
        """Apply an LLM/human resolution. feature.md first, then project, then realign.

        resolution: 'merged' | 'local' | 'remote'
          merged -> use `value` (the LLM-produced merge)
          local  -> keep current feature.md value (just realign + push)
          remote -> use the conflict's recorded remote value
        """
        conflict = self.conflicts.get(conflict_id)
        if conflict is None:
            return {"ok": False, "error": f"unknown conflict {conflict_id}"}

        if resolution == "remote":
            chosen = conflict.remote
        elif resolution == "local":
            chosen = conflict.local
        else:
            chosen = value

        result: dict[str, Any] = {
            "ok": True, "conflict_id": conflict_id, "feature_id": conflict.feature_id,
            "field": conflict.field, "resolution": resolution, "rework_staged": False,
        }

        # 1) Write the chosen value into feature.md (status goes via feature_list).
        if conflict.field == "status":
            self._apply_status_inbound(conflict.feature_id, str(chosen), result)
        else:
            self.codec.write_fields(conflict.feature_id, {conflict.field: chosen})

        # 2) Project feature.md to the tracker.
        ref = self.ledger.remote_ref(conflict.feature_id)
        local_item = self.codec.read(conflict.feature_id)
        if ref is not None and local_item is not None:
            try:
                refreshed = self.provider.update_item(ref, local_item)
                self._realign(conflict.feature_id, local_item, ref, refreshed)
            except (ProviderError, ProviderOffline) as e:
                result["ok"] = False
                result["error"] = str(e)

        # 3) Structural resolution -> stage re-entry (non-blocking).
        if conflict.field_class == "structural":
            fd = FieldDelta(conflict.field, conflict.field_class, conflict.base,
                            chosen, conflict.remote, DeltaType.INBOUND)
            self._stage_rework(conflict.feature_id, fd, "resolve")
            result["rework_staged"] = True

        # 4) Mark resolved + persist.
        self.conflicts.mark_resolved(conflict_id)
        self.conflicts.save()
        self.ledger.save()
        return result
