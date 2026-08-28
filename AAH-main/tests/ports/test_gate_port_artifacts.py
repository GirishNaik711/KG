"""Functional tests for the port-artifact completeness gate.

Activation is id-based. ACT-UX is `available` so it fires as `pending`.
"""

from aah.core.gates.validate_port_artifacts import validate_port_artifacts
from aah.core.ports import executor

from tests.ports.conftest import write_discuss_registry


class TestGate:
    def test_no_registry_passes(self, project):
        # Ports optional — no registry → no checks.
        passed, issues = validate_port_artifacts(project)
        assert passed is True
        assert issues == []

    def test_skipped_passes(self, project):
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "cloud-vs-local", "response": "local", "activates": []},
        ])
        executor.op_compile(project)  # ACT-ACCESS never activated → skipped
        passed, issues = validate_port_artifacts(project, node="architecture")
        assert passed is True

    def test_completed_but_missing_produces_fails(self, project):
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
        ])
        executor.op_compile(project)
        # Force completed without creating the produced artifact.
        executor.op_update(project, "ACT-UX", "completed")
        passed, issues = validate_port_artifacts(project, node="architecture")
        assert passed is False
        assert any("applications_wireframes" in i for i in issues)

    def test_completed_with_produces_passes(self, project):
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
        ])
        executor.op_compile(project)
        # ACT-UX produces .aah/architecture/applications_wireframes/README.md
        art = project / ".aah" / "architecture" / "applications_wireframes" / "README.md"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text("x", encoding="utf-8")
        executor.op_update(project, "ACT-UX", "completed",
                           artifacts=[".aah/architecture/applications_wireframes/README.md"])
        passed, issues = validate_port_artifacts(project, node="architecture")
        assert passed is True

    def test_pending_fired_activity_fails(self, project):
        # An available activity that fired but was left pending → gate fails.
        executor.op_init(project)
        write_discuss_registry(project, [
            {"slug_id": "custom-ui-required", "response": "yes", "activates": ["ACT-UX"]},
        ])
        executor.op_compile(project)  # ACT-UX is available → pending
        passed, issues = validate_port_artifacts(project, node="architecture")
        assert passed is False  # pending + produces missing
