"""Applier + resolve — feature.md-first, non-blocking, base realignment."""

from aah.core.common.feature_utils import parse_feature_frontmatter
from aah.core.version_control.applier import Applier
from aah.core.version_control.engine import DeltaEngine
from aah.core.version_control.models import DeltaType, WorkItem
from aah.core.version_control.tests.conftest import MemoryProvider


def _base():
    return WorkItem(feature_id="F001", description="orig", acceptance_criteria=["a"], status="planned")


def test_outbound_push_and_realign(project):
    base = _base()
    project["seed_base"](base)
    project["write_feature"](WorkItem(feature_id="F001", description="LOCAL", acceptance_criteria=["a"]))
    provider = MemoryProvider({1: base})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"])
    report = ap.apply(eng.compute(["F001"]))
    assert report["updated"] == 1
    # base realigned -> next delta is NO-OP (no echo loop)
    assert eng.compute(["F001"]).deltas[0].type == DeltaType.NOOP


def test_cosmetic_inbound_folds_into_yaml(project):
    base = _base()
    project["write_feature"](base, knowledge_used={"keep": "me"})
    project["seed_base"](base)
    remote = WorkItem(feature_id="F001", description="FROM_REMOTE", acceptance_criteria=["a"])
    provider = MemoryProvider({1: remote})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"])
    report = ap.apply(eng.compute(["F001"]))
    assert report["inbound"] >= 1
    data = parse_feature_frontmatter(project["rapids"] / "plan" / "features" / "F001.md")
    assert data["description"] == "FROM_REMOTE"          # folded into feature.md (truth)
    assert data["knowledge_used"] == {"keep": "me"}        # non-synced key preserved


def test_conflict_recorded_not_merged(project):
    base = _base()
    project["seed_base"](base)
    project["write_feature"](WorkItem(feature_id="F001", description="LOCAL", acceptance_criteria=["a"]))
    remote = WorkItem(feature_id="F001", description="REMOTE", acceptance_criteria=["a"])
    provider = MemoryProvider({1: remote})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"])
    report = ap.apply(eng.compute(["F001"]))
    assert report["conflicts"] == 1
    assert [c.field for c in project["conflicts"].open()] == ["description"]


def test_structural_inbound_recorded_for_review(project):
    base = _base()
    project["write_feature"](base)
    project["seed_base"](base)
    # remote-only structural change (AC), local unchanged -> recorded, not auto-folded
    remote = WorkItem(feature_id="F001", description="orig", acceptance_criteria=["a", "new"])
    provider = MemoryProvider({1: remote})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"],
                 auto_fold_structural_inbound=False)
    report = ap.apply(eng.compute(["F001"]))
    assert report["conflicts"] == 1


def test_resolve_merged_writes_yaml_and_clears(project):
    base = _base()
    project["seed_base"](base)
    project["write_feature"](WorkItem(feature_id="F001", description="orig",
                                      acceptance_criteria=["a", "L"]))
    remote = WorkItem(feature_id="F001", description="orig", acceptance_criteria=["a", "R"])
    provider = MemoryProvider({1: remote})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"])
    ap.apply(eng.compute(["F001"]))
    res = ap.resolve("CF-F001-acceptance_criteria", "merged", ["a", "L", "R"])
    assert res["ok"] and res["rework_staged"]
    data = parse_feature_frontmatter(project["rapids"] / "plan" / "features" / "F001.md")
    assert data["acceptance_criteria"] == ["a", "L", "R"]
    assert project["conflicts"].open() == []
    # converged: post-resolve delta is NO-OP
    assert eng.compute(["F001"]).deltas[0].type == DeltaType.NOOP


def test_converged_noop_realigns_base(project):
    """Both sides edit to the SAME value -> NO-OP, but base must be realigned.

    Regression: if base stays stale after convergence, the NEXT one-sided remote
    edit reads as local-changed-too and becomes a false CONFLICT. After apply the
    base should equal the agreed value so a subsequent remote-only edit is INBOUND.
    """
    base = _base()  # description="orig"
    project["seed_base"](base)
    # Both local and remote independently move to the SAME new value.
    project["write_feature"](WorkItem(feature_id="F001", description="SAME", acceptance_criteria=["a"]))
    remote = WorkItem(feature_id="F001", description="SAME", acceptance_criteria=["a"])
    provider = MemoryProvider({1: remote})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"])

    report = ap.apply(eng.compute(["F001"]))
    # Converged: nothing to push/fold, no conflict recorded.
    assert report["conflicts"] == 0
    assert project["conflicts"].open() == []
    # Base was realigned to the agreed value.
    assert project["ledger"].base("F001")["field_values"]["description"] == "SAME"

    # Now a remote-only edit must be a clean INBOUND, not a false CONFLICT.
    provider.items[1] = WorkItem(feature_id="F001", description="NEWER", acceptance_criteria=["a"])
    assert eng.compute(["F001"]).deltas[0].type == DeltaType.INBOUND


def test_create_seeds_ledger(project):
    project["write_feature"](WorkItem(feature_id="F009", description="new", acceptance_criteria=["x"]))
    provider = MemoryProvider({})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    ap = Applier(provider, project["ledger"], project["codec"], project["conflicts"])
    report = ap.apply(eng.compute(["F009"]))
    assert report["created"] == 1
    assert project["ledger"].has("F009")
