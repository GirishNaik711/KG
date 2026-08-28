"""Functional checks for the authoritative .aah decision-registry path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.gates.validate_analysis_gate import validate_registry_resolution
from aah.core.gates.validate_data_readiness import (
    check_use_case_alignment,
    discover_project_keywords,
)
from aah.core.gates.validate_research_gate import validate_decision_registry
from aah.core.registry.registry import (
    init_registry,
    save_registry,
    update_context,
    update_decision,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(module: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def test_all_registry_consumers_use_aah(tmp_path: Path) -> None:
    aah_path = tmp_path / ".aah"
    aah_path.mkdir()
    registry_path = aah_path / "decision-registry.yaml"
    _run(
        "aah.core.registry.registry",
        "init",
        "--project-name", "path-consumers-test",
        "--archetype", "ai-applications",
        "--output", str(registry_path),
    )
    _run(
        "aah.core.registry.registry",
        "update-context",
        "--registry", str(registry_path),
        "--field", "facts",
        "--value", '"authoritative_aah_marker"',
    )
    _run(
        "aah.core.registry.registry",
        "update-context",
        "--registry", str(registry_path),
        "--field", "domain_scope",
        "--value", '{"processes":["Authoritative Orders"]}',
    )
    save_manifest(get_default_manifest("path-consumers-test"), aah_path / "manifest.yaml")

    legacy = tmp_path / ".rapids"
    legacy.mkdir()
    legacy_registry = init_registry("legacy-decoy", "ai-applications")
    for decision in legacy_registry["decisions"]:
        update_decision(legacy_registry, decision["ddr_id"], status="skipped")
    update_decision(
        legacy_registry,
        "DDR-SHARED-017",
        status="resolved",
        resolved="cloudless-managed",
        confirmed=True,
    )
    update_context(legacy_registry, "facts", "legacy_rapids_marker")
    save_registry(legacy_registry, legacy / "decision-registry.yaml")

    assert validate_decision_registry(aah_path) == []
    analysis_issues = validate_registry_resolution(legacy)
    assert analysis_issues and "decisions not resolved/skipped" in analysis_issues[0]
    keywords = discover_project_keywords(tmp_path)
    assert "authoritative" in keywords
    assert "legacy_rapids_marker" not in keywords
    alignment = check_use_case_alignment(
        tmp_path,
        confirmed_tables=["authoritative_orders"],
        confirmed_prefixes=[],
        row_counts={"authoritative_orders": 1},
    )
    assert alignment and alignment[0]["process"] == "Authoritative Orders"

    env = dict(os.environ)
    env["RAPIDS_RUN"] = "1"
    extracted = _run(
        "aah.core.cloud.registry_extractor",
        "--project-path", str(tmp_path),
        env=env,
    )
    result = json.loads(extracted.stdout)
    assert result["total_services"] == 0
    assert result["provider"] is None

    assigned_sources = [
        "aah/core/gates/validate_analysis_gate.py",
        "aah/core/cloud/registry_extractor.py",
        "aah/core/gates/validate_research_gate.py",
        "aah/core/gates/validate_data_readiness.py",
    ]
    for relative in assigned_sources:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert '.rapids" / "decision-registry.yaml' not in source
        assert 'rapids_path / "decision-registry.yaml"' not in source
