"""Codec read/write preservation, ledger + conflict store persistence."""

from aah.core.common.feature_utils import parse_feature_frontmatter
from aah.core.version_control.conflict_store import ConflictStore, make_conflict_id
from aah.core.version_control.ledger import SyncLedger
from aah.core.version_control.models import ConflictRecord, ItemRef, WorkItem


def test_codec_write_preserves_nonsynced(project):
    project["write_feature"](
        WorkItem(feature_id="F001", description="d", acceptance_criteria=["a"]),
        knowledge_used={"x": 1}, adr_refs=["ADR-1"],
    )
    project["codec"].write_fields("F001", {"description": "updated", "status": "done"})
    data = parse_feature_frontmatter(project["rapids"] / "plan" / "features" / "F001.md")
    assert data["description"] == "updated"     # synced field written
    assert data["status"] != "done"             # status NOT written by codec (stays as seeded)
    assert data["knowledge_used"] == {"x": 1}   # preserved
    assert data["adr_refs"] == ["ADR-1"]        # preserved


def test_codec_read_to_workitem(project):
    project["write_feature"](WorkItem(feature_id="F001", description="hello",
                                      acceptance_criteria=["a", "b"], dependencies=["F000"]))
    wi = project["codec"].read("F001")
    assert wi.description == "hello"
    assert wi.acceptance_criteria == ["a", "b"]
    assert wi.dependencies == ["F000"]


def test_ledger_persists_and_realigns(project):
    led: SyncLedger = project["ledger"]
    led.put_entry("F001", remote_ref=ItemRef(number=5),
                  local_fingerprint="sha256:a", remote_fingerprint="sha256:b",
                  field_values={"description": "x"})
    led.save()
    reloaded = SyncLedger(project["rapids"])
    assert reloaded.remote_ref("F001").number == 5
    assert reloaded.base("F001")["local_fingerprint"] == "sha256:a"


def test_conflict_store_dedup_and_resolve(project):
    store: ConflictStore = project["conflicts"]
    cid = make_conflict_id("F001", "description")
    rec = ConflictRecord(cid, "F001", "description", "cosmetic", "b", "l", "r")
    store.record(rec)
    store.record(rec)  # same id -> update, not duplicate
    store.save()
    reloaded = ConflictStore(project["rapids"])
    assert len(reloaded.open()) == 1
    reloaded.mark_resolved(cid)
    reloaded.save()
    assert ConflictStore(project["rapids"]).open() == []
