#!/usr/bin/env python3
"""
End-to-end test for research phase behavior.

Tests that:
1. constraints-resolved.yaml has valid structure for RB generation
2. Activity plan DDR references work
3. Recommendation Brief generation simulation
4. validate_research_artifacts accepts valid RB structure
5. validate_research_gate accepts RB-based project
6. Legacy EVD project backward compatibility
"""

import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

# Paths
KYC_RAPIDS = Path(
    r"C:\Users\anprasanth\Desktop\My Files\FY_26-27\MAS-Global\RAPIDS"
    r"\code_repo\monorepo\rapids-workspaces\ai-projects\KYC-DocVerify\.rapids"
)


def test_constraints_resolved_structure():
    """TEST 1: constraints-resolved.yaml has valid structure for RB generation."""
    cr_path = KYC_RAPIDS / "research" / "constraints-resolved.yaml"
    cr = yaml.safe_load(cr_path.read_text(encoding="utf-8"))

    required = [
        "triggered_regimes",
        "applied_mandates",
        "eliminated_options",
        "client_specific_constraints",
    ]
    for field in required:
        assert field in cr, f"Missing field: {field}"

    for entry in cr["eliminated_options"]:
        assert "ddr_id" in entry, f"entry missing ddr_id: {entry}"
        assert "option" in entry, f"entry missing option: {entry}"
        has_source = any(k.startswith("by_") for k in entry)
        assert has_source, f"entry missing source attribution: {entry}"


def test_activity_plan_ddr_references():
    """TEST 2: Activity plan research activities map to DDR library definitions."""
    from aah.core.common.config import resolve_framework_root

    ap_path = KYC_RAPIDS / "activity-plan.yaml"
    ap = yaml.safe_load(ap_path.read_text(encoding="utf-8"))

    research_activities = [a for a in ap["activities"] if a.get("phase") == "research"]
    assert len(research_activities) > 0, "No research activities in plan"

    fw_path = resolve_framework_root()
    assert fw_path is not None, "Cannot resolve framework root"
    lib_path = fw_path / "activity-library"

    ddr_count = 0
    for activity in research_activities:
        aid = activity["activity_id"]
        for yaml_file in lib_path.rglob("*.yaml"):
            if yaml_file.name.startswith("_"):
                continue
            try:
                defn = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if defn and defn.get("id") == aid:
                    decisions = defn.get("decisions_addressed", [])
                    ddr_count += len(decisions)
                    break
            except Exception:
                pass

    assert ddr_count > 0, "No DDRs found via decisions_addressed in activity library"


def test_eliminated_options_groupable_by_ddr():
    """TEST 3: eliminated_options can be grouped by DDR for RB Eliminated table."""
    cr_path = KYC_RAPIDS / "research" / "constraints-resolved.yaml"
    cr = yaml.safe_load(cr_path.read_text(encoding="utf-8"))

    by_ddr = defaultdict(list)
    for entry in cr["eliminated_options"]:
        by_ddr[entry["ddr_id"]].append(entry)

    assert len(by_ddr) > 0, "No DDR groupings in eliminated_options"

    for ddr_id, entries in by_ddr.items():
        assert ddr_id.startswith("DDR-"), f"Invalid DDR ID format: {ddr_id}"
        for e in entries:
            assert "option" in e


def test_validate_research_artifacts_accepts_valid_rb():
    """TEST 4: validate_research_artifacts accepts a well-formed RB."""
    from aah.core.gates.validate_research_artifacts import (
        validate_research_artifacts,
    )

    rb_content = """# Recommendation Brief

## DDR-L2-001 — Framework Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| (none) | - | - |

### Forces That Matter

	Compliance + operational simplicity dominate.

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | LangGraph | Best for hub-and-spoke |
| Runner-up | CrewAI | Prefer if: simpler |

## DDR-L3-001 — Provider Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| test-option | by_mandate | CC-BSA-004 |

### Forces That Matter

Compliance + cost.

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | Vertex AI | GCP-native |
| Runner-up | OpenAI | Prefer if: no GCP mandate |
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        research_dir = Path(tmpdir) / "research"
        research_dir.mkdir()
        (research_dir / "recommendation-brief.md").write_text(
            rb_content, encoding="utf-8"
        )
        passed, issues = validate_research_artifacts(Path(tmpdir))
        assert passed, f"Validation failed: {issues}"


def test_validate_research_artifacts_rejects_missing_sections():
    """TEST 4b: validate_research_artifacts rejects RB with missing required sections."""
    from aah.core.gates.validate_research_artifacts import (
        validate_research_artifacts,
    )

    # Missing "Forces That Matter" section
    rb_bad = """# Recommendation Brief

## DDR-L2-001 — Framework Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| (none) | - | - |

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | LangGraph | Best |
| Runner-up | CrewAI | Alternative |
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        research_dir = Path(tmpdir) / "research"
        research_dir.mkdir()
        (research_dir / "recommendation-brief.md").write_text(
            rb_bad, encoding="utf-8"
        )
        passed, issues = validate_research_artifacts(Path(tmpdir))
        assert not passed, "Should have failed — missing Forces That Matter"
        assert any("Forces That Matter" in i for i in issues)


def test_validate_research_gate_rb_based_project():
    """TEST 5: validate_research_gate passes for a project using Recommendation Brief."""
    from aah.core.gates.validate_research_gate import validate_research_gate

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        rapids_dir = project_dir / ".rapids"
        rapids_dir.mkdir()

        manifest = {
            "project_name": "KYC-DocVerify-Test",
            "project_type": "greenfield",
            "current_phase": "research",
        }
        (rapids_dir / "manifest.yaml").write_text(
            yaml.dump(manifest), encoding="utf-8"
        )

        test_plan = {
            "activities": [
                {
                    "activity_id": "R-APP-L2-framework-tooling",
                    "phase": "research",
                    "status": "completed",
                    "decisions_addressed": ["DDR-L2-001", "DDR-L2-002"],
                    "artifacts": [],
                },
                {
                    "activity_id": "R-APP-L3-llm-selection",
                    "phase": "research",
                    "status": "completed",
                    "decisions_addressed": ["DDR-L3-001"],
                    "artifacts": [],
                },
            ]
        }
        (rapids_dir / "activity-plan.yaml").write_text(
            yaml.dump(test_plan), encoding="utf-8"
        )

        cr_test = {
            "triggered_regimes": ["bsa-aml"],
            "eliminated_options": [
                {
                    "ddr_id": "DDR-L3-001",
                    "option": "test-option",
                    "by_mandate": "CC-BSA-004",
                }
            ],
            "applied_mandates": [],
            "client_specific_constraints": [],
        }
        research_dir = rapids_dir / "research"
        research_dir.mkdir()
        (research_dir / "constraints-resolved.yaml").write_text(
            yaml.dump(cr_test), encoding="utf-8"
        )

        rb_test = """# Recommendation Brief

## DDR-L2-001 — Framework Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| (none) | - | - |

### Forces That Matter

Small team dominates.

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | LangGraph | Best for hub-and-spoke |
| Runner-up | CrewAI | Prefer if: simpler |

## DDR-L2-002 — Tooling Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| (none) | - | - |

### Forces That Matter

GCP mandate.

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | option-a | fits |
| Runner-up | option-b | alternative |

## DDR-L3-001 — Provider Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| test-option | by_mandate | CC-BSA-004 |

### Forces That Matter

Compliance + cost.

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | Vertex AI | GCP-native |
| Runner-up | OpenAI | Prefer if: no GCP mandate |
"""
        (research_dir / "recommendation-brief.md").write_text(
            rb_test, encoding="utf-8"
        )

        passed, issues = validate_research_gate(project_dir)
        assert passed, f"Research gate failed: {issues}"


def test_validate_research_gate_fails_missing_ddr_section():
    """TEST 5b: Gate fails when RB is missing a section for a DDR in the plan."""
    from aah.core.gates.validate_research_gate import validate_research_gate

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        rapids_dir = project_dir / ".rapids"
        rapids_dir.mkdir()

        manifest = {
            "project_name": "Test",
            "project_type": "greenfield",
            "current_phase": "research",
        }
        (rapids_dir / "manifest.yaml").write_text(
            yaml.dump(manifest), encoding="utf-8"
        )

        test_plan = {
            "activities": [
                {
                    "activity_id": "R-APP-L2-framework-tooling",
                    "phase": "research",
                    "status": "completed",
                    "decisions_addressed": ["DDR-L2-001", "DDR-L2-002"],
                    "artifacts": [],
                },
            ]
        }
        (rapids_dir / "activity-plan.yaml").write_text(
            yaml.dump(test_plan), encoding="utf-8"
        )

        cr_test = {
            "triggered_regimes": [],
            "eliminated_options": [],
            "applied_mandates": [],
            "client_specific_constraints": [],
        }
        research_dir = rapids_dir / "research"
        research_dir.mkdir()
        (research_dir / "constraints-resolved.yaml").write_text(
            yaml.dump(cr_test), encoding="utf-8"
        )

        # RB only has DDR-L2-001, missing DDR-L2-002
        rb_incomplete = """# Recommendation Brief

## DDR-L2-001 — Framework Selection

**Mode:** seed-based

### Eliminated

| Option | Source | Reason |
|--------|--------|--------|
| (none) | - | - |

### Forces That Matter

Small team.

### Recommendation

| | Option | Rationale |
|---|--------|-----------|
| Use | LangGraph | Best |
| Runner-up | CrewAI | Alt |
"""
        (research_dir / "recommendation-brief.md").write_text(
            rb_incomplete, encoding="utf-8"
        )

        passed, issues = validate_research_gate(project_dir)
        assert not passed, "Gate should fail — DDR-L2-002 missing from RB"
        assert any("DDR-L2-002" in i for i in issues)


def test_legacy_evd_backward_compatibility():
    """TEST 6: Legacy project with EVD files still passes the research gate."""
    from aah.core.gates.validate_research_gate import (
        validate_recommendation_brief,
    )

    ap = yaml.safe_load(
        (KYC_RAPIDS / "activity-plan.yaml").read_text(encoding="utf-8")
    )
    issues = validate_recommendation_brief(KYC_RAPIDS, ap)
    assert not issues, f"Legacy EVD project should pass: {issues}"


def test_validate_research_gate_incomplete_activities():
    """TEST 7: Gate fails when research activities are not completed."""
    from aah.core.gates.validate_research_gate import validate_research_gate

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        rapids_dir = project_dir / ".rapids"
        rapids_dir.mkdir()

        manifest = {
            "project_name": "Test",
            "project_type": "greenfield",
            "current_phase": "research",
        }
        (rapids_dir / "manifest.yaml").write_text(
            yaml.dump(manifest), encoding="utf-8"
        )

        test_plan = {
            "activities": [
                {
                    "activity_id": "R-APP-L2-framework-tooling",
                    "phase": "research",
                    "status": "pending",  # Not completed!
                    "decisions_addressed": ["DDR-L2-001"],
                    "artifacts": [],
                },
            ]
        }
        (rapids_dir / "activity-plan.yaml").write_text(
            yaml.dump(test_plan), encoding="utf-8"
        )

        cr_test = {
            "triggered_regimes": [],
            "eliminated_options": [],
            "applied_mandates": [],
            "client_specific_constraints": [],
        }
        research_dir = rapids_dir / "research"
        research_dir.mkdir()
        (research_dir / "constraints-resolved.yaml").write_text(
            yaml.dump(cr_test), encoding="utf-8"
        )

        passed, issues = validate_research_gate(project_dir)
        assert not passed, "Gate should fail — activity not completed"
        assert any("pending" in i for i in issues)


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
