"""Functional tests for `ports.executor reconcile` — the main-loop safety net.

reconcile marks a fired port completed when all its `produces` exist on disk, and
tags it `reconciled` so the timestamp reads as detection time (the activity really
finished a little earlier). NO MOCKS — real temp projects + real files.
"""

from aah.core.common.io_utils import read_yaml
from aah.core.ports import executor

from tests.ports.conftest import write_discuss_registry


def _by_id(project):
    reg = read_yaml(executor.registry_path(project))
    return {a["id"]: a for a in reg["activities"]}


def _fire_act_ux(project):
    """init + compile so ACT-UX is present and `pending` (fired, not yet run)."""
    executor.op_init(project)
    write_discuss_registry(project, [
        {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
    ])
    executor.op_compile(project)
    assert _by_id(project)["ACT-UX"]["status"] == "pending"


def _write_produces(project, act):
    """Create every declared produces artifact for one activity."""
    for p in act.get("produces", []):
        path = project / p
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("real artifact", encoding="utf-8")


class TestReconcile:
    def test_marks_completed_when_produces_exist(self, project):
        _fire_act_ux(project)
        _write_produces(project, _by_id(project)["ACT-UX"])

        result = executor.op_reconcile(project, node="architecture")
        assert "ACT-UX" in result["reconciled"]

        act = _by_id(project)["ACT-UX"]
        assert act["status"] == "completed"
        assert act["completed_at"]
        assert act["started_at"]  # back-filled
        # Tagged so the reader knows completed_at is detection time, not true finish.
        assert act["reconciled"] is True
        assert act["reconciled_at"] == act["completed_at"]
        # The detected artifact is recorded.
        assert ".aah/architecture/applications_wireframes/README.md" in act["artifacts_produced"]

    def test_leaves_port_pending_when_produces_missing(self, project):
        _fire_act_ux(project)
        # No artifact written → genuinely not done → untouched.
        result = executor.op_reconcile(project, node="architecture")
        assert result["reconciled"] == []
        act = _by_id(project)["ACT-UX"]
        assert act["status"] == "pending"
        assert "reconciled" not in act

    def test_does_not_touch_completed_or_skipped(self, project):
        _fire_act_ux(project)
        _write_produces(project, _by_id(project)["ACT-UX"])
        # An explicit update already completed it → reconcile must NOT re-tag it.
        executor.op_update(project, "ACT-UX", "completed")

        result = executor.op_reconcile(project, node="architecture")
        assert result["reconciled"] == []
        act = _by_id(project)["ACT-UX"]
        assert act["status"] == "completed"
        assert "reconciled" not in act  # not reconcile-completed — explicitly completed

    def test_ignores_ports_with_no_produces(self, project):
        # ACT-INTENT-MEDIATOR is a default with empty produces — nothing to detect.
        executor.op_init(project)
        result = executor.op_reconcile(project)
        assert result["reconciled"] == []

    def test_node_scoping(self, project):
        _fire_act_ux(project)
        _write_produces(project, _by_id(project)["ACT-UX"])
        # ACT-UX targets `architecture`; scoping to `build` must skip it.
        result = executor.op_reconcile(project, node="build")
        assert result["reconciled"] == []
        assert _by_id(project)["ACT-UX"]["status"] == "pending"

    def test_no_registry_is_inert(self, project):
        # No init → no registry → reconcile is a no-op, never errors.
        result = executor.op_reconcile(project)
        assert result["reconciled"] == []
