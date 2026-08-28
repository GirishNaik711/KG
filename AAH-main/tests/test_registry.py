"""Unit tests for aah.core.registry.registry — decision registry state machine."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
FRAMEWORK_ROOT = SCRIPTS_DIR.parent


def run_registry(*args: str, input_data: str | None = None) -> dict:
    cmd = [sys.executable, "-m", "aah.core.registry.registry", *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(SCRIPTS_DIR),
        input=input_data,
    )
    if result.returncode != 0:
        raise RuntimeError(f"registry command failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


class TestRegistryInit:
    def test_init_creates_registry(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        result = run_registry(
            "init",
            "--project-name", "test-project",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        assert output.exists()
        assert "progress" in result
        assert result["progress"]["total"] > 0

        registry = yaml.safe_load(output.read_text(encoding="utf-8"))
        assert registry["schema_version"] == "2.0"
        assert registry["project"]["name"] == "test-project"
        assert registry["project"]["archetype"] == "ai-applications"
        assert len(registry["decisions"]) > 0
        assert all(d["status"] == "open" for d in registry["decisions"])

    def test_init_with_shared_ddrs(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "test-project",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        registry = yaml.safe_load(output.read_text(encoding="utf-8"))
        ddr_ids = [d["ddr_id"] for d in registry["decisions"]]
        shared_ids = [d for d in ddr_ids if d.startswith("DDR-SHARED")]
        archetype_ids = [d for d in ddr_ids if d.startswith("DDR-L")]
        assert len(shared_ids) > 0, "Should include shared DDRs"
        assert len(archetype_ids) > 0, "Should include archetype DDRs"

    def test_init_progress_all_open(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        result = run_registry(
            "init",
            "--project-name", "test-project",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        progress = result["progress"]
        assert progress["open"] == progress["total"]
        assert progress["resolved"] == 0
        assert progress["skipped"] == 0


class TestRegistryUpdateContext:
    @pytest.fixture
    def registry_path(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "ctx-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        return output

    def test_update_fact(self, registry_path):
        result = run_registry(
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"Existing PostgreSQL deployment"',
        )
        assert result["updated"] is True

        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert "Existing PostgreSQL deployment" in registry["context"]["facts"]

    def test_update_integration(self, registry_path):
        run_registry(
            "update-context",
            "--registry", str(registry_path),
            "--field", "integrations",
            "--value", '{"system": "Salesforce", "protocol": "REST"}',
        )
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        integrations = registry["context"]["integrations"]
        assert len(integrations) == 1
        assert integrations[0]["system"] == "Salesforce"

    def test_update_infrastructure_givens(self, registry_path):
        run_registry(
            "update-context",
            "--registry", str(registry_path),
            "--field", "infrastructure_givens",
            "--value", '{"cloud": "AWS", "identity_provider": "Okta"}',
        )
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        givens = registry["context"]["infrastructure_givens"]
        assert givens["cloud"] == "AWS"
        assert givens["identity_provider"] == "Okta"

    def test_append_multiple_facts(self, registry_path):
        run_registry(
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"Fact one"',
        )
        run_registry(
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"Fact two"',
        )
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert len(registry["context"]["facts"]) == 2


class TestRegistryUpdateDecision:
    @pytest.fixture
    def registry_path(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "dec-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        return output

    def test_resolve_decision(self, registry_path):
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        first_ddr_id = registry["decisions"][0]["ddr_id"]

        result = run_registry(
            "update-decision",
            "--registry", str(registry_path),
            "--ddr-id", first_ddr_id,
            "--status", "resolved",
            "--resolved", "some-option",
            "--confirmed", "true",
        )
        assert result["updated"] is True

        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        decision = next(d for d in registry["decisions"] if d["ddr_id"] == first_ddr_id)
        assert decision["status"] == "resolved"
        assert decision["resolved"] == "some-option"
        assert decision["confirmed"] is True
        assert registry["progress"]["resolved"] == 1

    def test_invalid_status_fails(self, registry_path):
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        first_ddr_id = registry["decisions"][0]["ddr_id"]

        with pytest.raises(RuntimeError):
            run_registry(
                "update-decision",
                "--registry", str(registry_path),
                "--ddr-id", first_ddr_id,
                "--status", "invalid-status",
            )


class TestRegistryStatus:
    def test_status_output(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "status-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        result = run_registry("status", "--registry", str(output))
        assert "project" in result
        assert "progress" in result
        assert "context_populated" in result
        assert result["project"]["name"] == "status-test"


class TestNextUnresolved:
    def test_returns_first_decision(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "next-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        result = run_registry("next-unresolved", "--registry", str(output))
        assert "ddr_id" in result
        assert "decision" in result
        assert "ddr" in result

    def test_all_resolved_returns_status(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "all-resolved-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        registry = yaml.safe_load(output.read_text(encoding="utf-8"))
        for d in registry["decisions"]:
            d["status"] = "resolved"
            d["resolved"] = "test-option"
        registry["updated_at"] = "2025-01-01T00:00:00+00:00"
        output.write_text(yaml.dump(registry, sort_keys=False), encoding="utf-8")

        result = run_registry("next-unresolved", "--registry", str(output))
        assert result.get("status") == "all_resolved"


class TestApplyEliminations:
    def test_apply_eliminations_runs(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "elim-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        result = run_registry("apply-eliminations", "--registry", str(output))
        assert result["applied"] is True
        assert "total_eliminations" in result


class TestApplySkipConditions:
    def test_apply_skip_conditions_runs(self, tmp_path):
        output = tmp_path / "decision-registry.yaml"
        run_registry(
            "init",
            "--project-name", "skip-test",
            "--archetype", "ai-applications",
            "--output", str(output),
        )
        result = run_registry("apply-skip-conditions", "--registry", str(output))
        assert result["applied"] is True
        assert "skipped_decisions" in result
