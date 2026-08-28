"""Functional tests for `ports.executor init` against a real temp project."""

from aah.core.common.io_utils import read_yaml
from aah.core.ports import executor


class TestInit:
    def test_materializes_defaults(self, project):
        result = executor.op_init(project)
        assert result["action"] == "init"
        # The shipped catalog has exactly one default (ACT-INTENT-MEDIATOR).
        assert "ACT-INTENT-MEDIATOR" in result["defaults_added"]

        reg = read_yaml(executor.registry_path(project))
        assert reg["schema_version"] == "2.0"
        assert reg["initialized_at"]
        assert reg["compiled_at"] is None
        ids = {a["id"] for a in reg["activities"]}
        assert "ACT-INTENT-MEDIATOR" in ids
        # discuss-triggered activities are NOT present until compile.
        assert "ACT-UX" not in ids

    def test_default_is_stub_marked_deferred(self, project):
        executor.op_init(project)
        reg = read_yaml(executor.registry_path(project))
        med = next(a for a in reg["activities"] if a["id"] == "ACT-INTENT-MEDIATOR")
        # Every catalog entry is a stub for now → status deferred-stub.
        assert med["status"] == "deferred-stub"

    def test_idempotent(self, project):
        executor.op_init(project)
        # Mutate runtime state, then re-init: it must be preserved.
        executor.op_update(project, "ACT-INTENT-MEDIATOR", "completed")
        result = executor.op_init(project)
        assert result["defaults_added"] == []  # nothing re-added

        reg = read_yaml(executor.registry_path(project))
        med = next(a for a in reg["activities"] if a["id"] == "ACT-INTENT-MEDIATOR")
        assert med["status"] == "completed"  # runtime state survived re-init

    def test_no_registry_before_init(self, project):
        # before() on a project with no registry is inert.
        assert executor.op_before(project, "architecture")["activities"] == []
