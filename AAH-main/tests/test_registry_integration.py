"""Integration tests for the full registry workflow.

Tests the complete research flow (init → context → regimes → eliminations)
and analysis flow (resolve → skip → gate readiness) through CLI commands.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def run_cmd(module: str, *args: str, input_data: str | None = None) -> dict | list:
    cmd = [sys.executable, "-m", module, *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(SCRIPTS_DIR),
        input=input_data,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{module} failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


class TestResearchFlow:
    """Simulate the research phase: init → populate context → detect regimes → apply eliminations."""

    def test_full_research_flow(self, tmp_path):
        registry_path = tmp_path / "decision-registry.yaml"

        # Step 1: Init registry
        result = run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "integration-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )
        assert result["progress"]["total"] > 0
        assert result["progress"]["open"] == result["progress"]["total"]

        # Step 2: Add facts (scoping)
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"Customer-facing chatbot for commercial banking"',
        )
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"Must handle sensitive financial data"',
        )

        # Step 3: Add integrations
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "integrations",
            "--value", '{"system": "Salesforce", "protocol": "REST", "auth": "OAuth2", "direction": "bidirectional"}',
        )

        # Step 4: Add infrastructure givens
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "infrastructure_givens",
            "--value", '{"cloud": "GCP", "identity_provider": "Azure AD"}',
        )

        # Step 5: Add compliance regimes
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "compliance_regimes",
            "--value", '"soc2"',
        )

        # Step 6: Add NFRs
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "nfrs",
            "--value", '{"category": "performance", "target": "p95 < 500ms"}',
        )

        # Step 7: Add organizational constraints
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "organizational_constraints",
            "--value", '{"statement": "Team of 3 engineers", "flexibility": "fixed"}',
        )

        # Verify context is populated
        status = run_cmd(
            "aah.core.registry.registry",
            "status",
            "--registry", str(registry_path),
        )
        ctx = status["context_populated"]
        assert ctx["facts"] == 2
        assert ctx["integrations"] == 1
        assert ctx["compliance_regimes"] == 1
        assert ctx["nfrs"] == 1
        assert ctx["constraints"] == 1

        # Step 8: Apply eliminations
        elim_result = run_cmd(
            "aah.core.registry.registry",
            "apply-eliminations",
            "--registry", str(registry_path),
        )
        assert elim_result["applied"] is True

        # Step 9: Apply skip conditions (no resolved decisions yet, should be no-op)
        skip_result = run_cmd(
            "aah.core.registry.registry",
            "apply-skip-conditions",
            "--registry", str(registry_path),
        )
        assert skip_result["applied"] is True

        # Verify final registry state
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        assert registry["schema_version"] == "2.0"
        assert registry["project"]["name"] == "integration-test"
        assert len(registry["context"]["facts"]) == 2
        assert registry["context"]["infrastructure_givens"]["cloud"] == "GCP"

    def test_research_flow_registry_round_trip(self, tmp_path):
        """Verify init → update → status produces consistent state."""
        registry_path = tmp_path / "decision-registry.yaml"

        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "round-trip-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )

        # Add some facts
        for fact in ["Fact A", "Fact B", "Fact C"]:
            run_cmd(
                "aah.core.registry.registry",
                "update-context",
                "--registry", str(registry_path),
                "--field", "facts",
                "--value", json.dumps(fact),
            )

        # Status should reflect updates
        status = run_cmd(
            "aah.core.registry.registry",
            "status",
            "--registry", str(registry_path),
        )
        assert status["context_populated"]["facts"] == 3
        assert status["project"]["name"] == "round-trip-test"


class TestAnalysisFlow:
    """Simulate the analysis phase: resolve decisions in order → skip conditions → completion."""

    @pytest.fixture
    def initialized_registry(self, tmp_path):
        registry_path = tmp_path / "decision-registry.yaml"
        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "analysis-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )
        # Add minimal context
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"Internal knowledge assistant"',
        )
        return registry_path

    def test_resolve_decisions_in_order(self, initialized_registry):
        registry_path = initialized_registry

        # Get first unresolved decision
        first = run_cmd(
            "aah.core.registry.registry",
            "next-unresolved",
            "--registry", str(registry_path),
        )
        assert "ddr_id" in first
        assert first["ddr"] is not None

        first_ddr_id = first["ddr_id"]
        first_options = first["ddr"].get("options", [])
        chosen_option = first_options[0]["label"] if first_options else "default-option"

        # Resolve the first decision
        run_cmd(
            "aah.core.registry.registry",
            "update-decision",
            "--registry", str(registry_path),
            "--ddr-id", first_ddr_id,
            "--status", "resolved",
            "--resolved", chosen_option,
            "--confirmed", "true",
        )

        # Next-unresolved should return a different decision
        second = run_cmd(
            "aah.core.registry.registry",
            "next-unresolved",
            "--registry", str(registry_path),
        )
        assert "ddr_id" in second
        assert second["ddr_id"] != first_ddr_id

        # Progress should show 1 resolved
        status = run_cmd(
            "aah.core.registry.registry",
            "status",
            "--registry", str(registry_path),
        )
        assert status["progress"]["resolved"] == 1

    def test_resolve_all_decisions(self, initialized_registry):
        registry_path = initialized_registry

        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        total = len(registry["decisions"])

        # Resolve all decisions
        for d in registry["decisions"]:
            run_cmd(
                "aah.core.registry.registry",
                "update-decision",
                "--registry", str(registry_path),
                "--ddr-id", d["ddr_id"],
                "--status", "resolved",
                "--resolved", "test-option",
                "--confirmed", "true",
            )

        # Next-unresolved should indicate all resolved
        result = run_cmd(
            "aah.core.registry.registry",
            "next-unresolved",
            "--registry", str(registry_path),
        )
        assert result.get("status") == "all_resolved"

        # Progress should match
        status = run_cmd(
            "aah.core.registry.registry",
            "status",
            "--registry", str(registry_path),
        )
        assert status["progress"]["resolved"] == total
        assert status["progress"]["open"] == 0

    def test_skip_conditions_after_resolve(self, initialized_registry):
        registry_path = initialized_registry

        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))

        # Find DDR-L5-001 (single-vs-multi-agent) and DDR-L5-004 (shared state)
        # L5-004 has skip_if: [{ddr_id: DDR-L5-001, resolved_to: single-agent}]
        has_l5_001 = any(d["ddr_id"] == "DDR-L5-001" for d in registry["decisions"])
        has_l5_004 = any(d["ddr_id"] == "DDR-L5-004" for d in registry["decisions"])

        if not (has_l5_001 and has_l5_004):
            pytest.skip("Required DDRs L5-001 and L5-004 not found in registry")

        # Resolve L5-001 as "single-agent-rich-tooling" (the actual option label)
        run_cmd(
            "aah.core.registry.registry",
            "update-decision",
            "--registry", str(registry_path),
            "--ddr-id", "DDR-L5-001",
            "--status", "resolved",
            "--resolved", "single-agent-rich-tooling",
            "--confirmed", "true",
        )

        # Apply skip conditions
        skip_result = run_cmd(
            "aah.core.registry.registry",
            "apply-skip-conditions",
            "--registry", str(registry_path),
        )
        assert skip_result["applied"] is True

        # Check if L5-004 was skipped
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        l5_004 = next(d for d in registry["decisions"] if d["ddr_id"] == "DDR-L5-004")
        assert l5_004["status"] == "skipped"


class TestRegistryWithDDRLoader:
    """Integration between registry and ddr_loader modules."""

    def test_registry_ddrs_match_loader(self, tmp_path):
        """All DDRs from loader should appear in an initialized registry."""
        registry_path = tmp_path / "decision-registry.yaml"
        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "match-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )

        # Get DDR IDs from loader
        all_ddrs = run_cmd(
            "aah.core.registry.ddr_loader",
            "list-ddrs",
            "--format", "json",
        )
        loader_ids = {d["id"] for d in all_ddrs}

        # Get DDR IDs from registry
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        registry_ids = {d["ddr_id"] for d in registry["decisions"]}

        assert loader_ids == registry_ids, (
            f"Loader-only: {loader_ids - registry_ids}, "
            f"Registry-only: {registry_ids - loader_ids}"
        )

    def test_next_unresolved_loads_full_ddr(self, tmp_path):
        """next-unresolved should return the full DDR playbook data."""
        registry_path = tmp_path / "decision-registry.yaml"
        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "ddr-load-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )

        result = run_cmd(
            "aah.core.registry.registry",
            "next-unresolved",
            "--registry", str(registry_path),
        )

        ddr = result["ddr"]
        assert ddr is not None
        assert "decision_question" in ddr
        assert "forces" in ddr
        assert "options" in ddr

    def test_resolution_order_respects_dependencies(self, tmp_path):
        """Decisions should be served in dependency order."""
        registry_path = tmp_path / "decision-registry.yaml"
        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "order-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )

        # Get resolution order from loader
        waves_result = run_cmd(
            "aah.core.registry.ddr_loader",
            "resolution-order",
        )
        wave_order = []
        for wave in waves_result["waves"]:
            wave_order.extend(wave["ddr_ids"])

        # Get first unresolved from registry
        first = run_cmd(
            "aah.core.registry.registry",
            "next-unresolved",
            "--registry", str(registry_path),
        )

        # The first unresolved should be from the first wave
        first_wave_ids = set(waves_result["waves"][0]["ddr_ids"])
        assert first["ddr_id"] in first_wave_ids, (
            f"First unresolved {first['ddr_id']} not in wave 0: {first_wave_ids}"
        )


class TestRegistryWithRegimes:
    """Integration between registry, regimes, and ddr_loader."""

    def test_regime_detection_and_elimination(self, tmp_path):
        """Explicit compliance regimes should trigger eliminations."""
        registry_path = tmp_path / "decision-registry.yaml"
        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "regime-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )

        # Add compliance regime
        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "compliance_regimes",
            "--value", '"hipaa"',
        )

        # Apply eliminations
        result = run_cmd(
            "aah.core.registry.registry",
            "apply-eliminations",
            "--registry", str(registry_path),
        )
        assert result["applied"] is True

        # Direct regime detection should also find hipaa
        detect_result = run_cmd(
            "aah.core.registry.regimes",
            "detect-regimes",
            "--registry", str(registry_path),
        )
        assert "hipaa" in detect_result["triggered_regimes"]

    def test_fact_based_regime_triggers_eliminations(self, tmp_path):
        """Facts mentioning compliance keywords should trigger regime detection."""
        registry_path = tmp_path / "decision-registry.yaml"
        run_cmd(
            "aah.core.registry.registry",
            "init",
            "--project-name", "fact-regime-test",
            "--archetype", "ai-applications",
            "--output", str(registry_path),
        )

        run_cmd(
            "aah.core.registry.registry",
            "update-context",
            "--registry", str(registry_path),
            "--field", "facts",
            "--value", '"System handles protected health information under HIPAA"',
        )

        detect_result = run_cmd(
            "aah.core.registry.regimes",
            "detect-regimes",
            "--registry", str(registry_path),
        )
        regimes = detect_result["triggered_regimes"]
        assert any("hipaa" in r.lower() for r in regimes)
