"""Engine 3-way classification matrix — per-field, against fixture feature.md files."""

from aah.core.version_control.engine import DeltaEngine
from aah.core.version_control.models import DeltaType, WorkItem
from aah.core.version_control.tests.conftest import MemoryProvider


def _engine(project, remote_item):
    provider = MemoryProvider({1: remote_item})
    return DeltaEngine(provider, project["ledger"], project["codec"])


def _base():
    return WorkItem(feature_id="F001", description="orig", acceptance_criteria=["a"], status="planned")


def test_noop(project):
    base = _base()
    project["write_feature"](base)
    project["seed_base"](base)
    d = _engine(project, base).compute(["F001"]).deltas[0]
    assert d.type == DeltaType.NOOP


def test_outbound(project):
    base = _base()
    project["seed_base"](base)
    project["write_feature"](WorkItem(feature_id="F001", description="LOCAL", acceptance_criteria=["a"]))
    d = _engine(project, base).compute(["F001"]).deltas[0]
    assert d.type == DeltaType.OUTBOUND


def test_inbound(project):
    base = _base()
    project["write_feature"](base)
    project["seed_base"](base)
    remote = WorkItem(feature_id="F001", description="REMOTE", acceptance_criteria=["a"])
    d = _engine(project, remote).compute(["F001"]).deltas[0]
    assert d.type == DeltaType.INBOUND


def test_conflict(project):
    base = _base()
    project["seed_base"](base)
    project["write_feature"](WorkItem(feature_id="F001", description="LOCAL", acceptance_criteria=["a"]))
    remote = WorkItem(feature_id="F001", description="REMOTE", acceptance_criteria=["a"])
    d = _engine(project, remote).compute(["F001"]).deltas[0]
    assert d.type == DeltaType.CONFLICT


def test_per_field_isolation(project):
    # description clean (both unchanged), AC conflicts (both changed differently)
    base = _base()
    project["seed_base"](base)
    project["write_feature"](WorkItem(feature_id="F001", description="orig", acceptance_criteria=["a", "L"]))
    remote = WorkItem(feature_id="F001", description="orig", acceptance_criteria=["a", "R"])
    d = _engine(project, remote).compute(["F001"]).deltas[0]
    assert d.type == DeltaType.CONFLICT
    assert [f.field for f in d.conflicting_fields()] == ["acceptance_criteria"]


def test_new_feature_outbound_create(project):
    # no ledger entry -> outbound create
    project["write_feature"](WorkItem(feature_id="F002", description="new", acceptance_criteria=["x"]))
    provider = MemoryProvider({})
    eng = DeltaEngine(provider, project["ledger"], project["codec"])
    d = eng.compute(["F002"]).deltas[0]
    assert d.type == DeltaType.OUTBOUND
    assert d.remote_ref is None
