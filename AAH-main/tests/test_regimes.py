"""Unit tests for aah.core.registry.regimes — regime detection and elimination."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def run_regimes(*args: str) -> dict:
    cmd = [sys.executable, "-m", "aah.core.registry.regimes", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPTS_DIR))
    if result.returncode != 0:
        raise RuntimeError(f"regimes failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def create_registry(tmp_path, context_overrides: dict | None = None) -> Path:
    registry = {
        "schema_version": "2.0",
        "project": {
            "name": "test-project",
            "archetype": "ai-applications",
            "domain": None,
            "ddr_sets": ["ai-applications", "shared"],
        },
        "context": {
            "deployment_target": "internal",
            "delivery_intent": "production",
            "compliance_regimes": [],
            "integrations": [],
            "infrastructure_givens": {},
            "nfrs": [],
            "organizational_constraints": [],
            "facts": [],
        },
        "decisions": [],
        "progress": {"total": 0, "resolved": 0, "recommended": 0, "partial": 0, "open": 0, "skipped": 0},
        "updated_at": "2025-01-01T00:00:00+00:00",
    }
    if context_overrides:
        registry["context"].update(context_overrides)

    registry_path = tmp_path / "decision-registry.yaml"
    registry_path.write_text(yaml.dump(registry, sort_keys=False), encoding="utf-8")
    return registry_path


class TestDetectRegimes:
    def test_detect_no_regimes(self, tmp_path):
        registry_path = create_registry(tmp_path)
        result = run_regimes("detect-regimes", "--registry", str(registry_path))
        assert "triggered_regimes" in result

    def test_detect_explicit_regimes(self, tmp_path):
        registry_path = create_registry(tmp_path, {
            "compliance_regimes": ["hipaa", "soc2"],
        })
        result = run_regimes("detect-regimes", "--registry", str(registry_path))
        regimes = result["triggered_regimes"]
        assert "hipaa" in regimes
        assert "soc2" in regimes

    def test_detect_regimes_from_facts(self, tmp_path):
        registry_path = create_registry(tmp_path, {
            "facts": ["HIPAA-compliant patient data handling required"],
        })
        result = run_regimes("detect-regimes", "--registry", str(registry_path))
        regimes = result["triggered_regimes"]
        assert any("hipaa" in r.lower() for r in regimes)


class TestGetEliminations:
    def test_eliminations_with_no_context(self, tmp_path):
        registry_path = create_registry(tmp_path)
        result = run_regimes(
            "get-eliminations",
            "--registry", str(registry_path),
            "--archetype", "ai-applications",
        )
        assert "elimination_count" in result
        assert "eliminations" in result

    def test_eliminations_have_required_fields(self, tmp_path):
        registry_path = create_registry(tmp_path, {
            "facts": ["production deployment with strict security requirements"],
        })
        result = run_regimes(
            "get-eliminations",
            "--registry", str(registry_path),
            "--archetype", "ai-applications",
        )
        for elim in result.get("eliminations", []):
            assert "ddr_id" in elim
            assert "option" in elim
            assert "source" in elim
            assert elim["source"] in ("regime", "engineering")
