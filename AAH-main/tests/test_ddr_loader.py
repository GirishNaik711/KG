"""Unit tests for aah.core.registry.ddr_loader — DDR v2.0 playbook loader."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def run_loader(*args: str) -> dict | list:
    cmd = [sys.executable, "-m", "aah.core.registry.ddr_loader", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPTS_DIR))
    if result.returncode != 0:
        raise RuntimeError(f"ddr_loader failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


class TestListDDRs:
    def test_list_all_ddrs(self):
        ddrs = run_loader("list-ddrs", "--format", "json")
        assert isinstance(ddrs, list)
        assert len(ddrs) > 0
        for ddr in ddrs:
            assert "id" in ddr
            assert "decision_question" in ddr

    def test_list_by_ddr_set(self):
        ddrs = run_loader("list-ddrs", "--ddr-set", "ai-applications", "--format", "json")
        assert isinstance(ddrs, list)
        assert all(not d["id"].startswith("DDR-SHARED") for d in ddrs)

    def test_list_shared_ddrs(self):
        ddrs = run_loader("list-ddrs", "--ddr-set", "shared", "--format", "json")
        assert isinstance(ddrs, list)
        assert all(d["id"].startswith("DDR-SHARED") for d in ddrs)

    def test_list_ids_format(self):
        cmd = [sys.executable, "-m", "aah.core.registry.ddr_loader",
               "list-ddrs", "--format", "ids"]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPTS_DIR))
        ids = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        assert len(ids) > 0
        assert all(id.startswith("DDR-") for id in ids)


class TestLoadDDR:
    def test_load_existing_ddr(self):
        ddr = run_loader("load-ddr", "--id", "DDR-L1-001", "--format", "json")
        assert ddr["id"] == "DDR-L1-001"
        assert "decision_question" in ddr
        assert "forces" in ddr
        assert "options" in ddr

    def test_load_shared_ddr(self):
        ddr = run_loader("load-ddr", "--id", "DDR-SHARED-001", "--format", "json")
        assert ddr["id"] == "DDR-SHARED-001"
        assert ddr["category"] == "shared"

    def test_load_nonexistent_ddr_fails(self):
        with pytest.raises(RuntimeError, match="not found"):
            run_loader("load-ddr", "--id", "DDR-L99-999", "--format", "json")


class TestValidateDDR:
    def test_validate_all_ddrs_pass(self):
        result = run_loader("validate-ddr", "--all")
        assert result["valid"] is True
        assert result["error_count"] == 0

    def test_validate_single_ddr(self, tmp_path):
        valid_ddr = {
            "schema_version": "2.0",
            "id": "DDR-L1-999",
            "category": "archetype",
            "layer": "Test Layer",
            "layer_number": 1,
            "depends_on": [],
            "skip_if": [],
            "decision_question": "How should we test?",
            "forces": [
                {"id": "speed-vs-coverage", "tension": "Speed vs Coverage",
                 "description": "Fast tests vs comprehensive tests"},
                {"id": "cost-vs-quality", "tension": "Cost vs Quality",
                 "description": "Cheap tests vs thorough tests"},
            ],
            "options": [
                {
                    "label": "unit-tests-only",
                    "description": "Only unit tests",
                    "force_resolution": {
                        "speed-vs-coverage": {"verdict": "favours", "detail": "Fast"},
                        "cost-vs-quality": {"verdict": "trades-off", "detail": "Less thorough"},
                    },
                },
                {
                    "label": "full-suite",
                    "description": "Unit + integration + e2e",
                    "force_resolution": {
                        "speed-vs-coverage": {"verdict": "trades-off", "detail": "Slower"},
                        "cost-vs-quality": {"verdict": "favours", "detail": "More thorough"},
                    },
                },
            ],
            "anti_patterns": ["No tests at all"],
        }
        ddr_path = tmp_path / "test-ddr.yaml"
        import yaml
        ddr_path.write_text(yaml.dump(valid_ddr, sort_keys=False), encoding="utf-8")

        result = run_loader("validate-ddr", "--path", str(ddr_path))
        assert result["valid"] is True

    def test_validate_invalid_ddr_fails(self, tmp_path):
        invalid_ddr = {
            "schema_version": "1.0",
            "id": "DDR-L1-999",
            "forces": [],
            "options": [],
        }
        ddr_path = tmp_path / "bad-ddr.yaml"
        import yaml
        ddr_path.write_text(yaml.dump(invalid_ddr, sort_keys=False), encoding="utf-8")

        with pytest.raises(RuntimeError):
            run_loader("validate-ddr", "--path", str(ddr_path))


class TestResolutionOrder:
    def test_resolution_order_computes_waves(self):
        result = run_loader("resolution-order")
        assert "wave_count" in result
        assert result["wave_count"] > 0
        assert "waves" in result
        for wave in result["waves"]:
            assert "wave" in wave
            assert "ddr_ids" in wave
            assert len(wave["ddr_ids"]) > 0

    def test_resolution_order_contains_all_ddrs(self):
        all_ddrs = run_loader("list-ddrs", "--format", "json")
        all_ids = {d["id"] for d in all_ddrs}

        waves_result = run_loader("resolution-order")
        wave_ids = set()
        for wave in waves_result["waves"]:
            wave_ids.update(wave["ddr_ids"])

        assert all_ids == wave_ids, f"Missing from waves: {all_ids - wave_ids}"
