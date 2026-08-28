#!/usr/bin/env python3
"""
Stop hook: validate research phase completion before transitioning.

Checks decision registry completeness (v2 path) or activity plan completion
(legacy path). Exit 0 = pass, Exit 2 = fail.
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml


def validate_decision_registry(aah_path: Path) -> list[str]:
    """Validate decision-registry.yaml exists and is properly populated."""
    issues = []
    registry_path = aah_path / "decision-registry.yaml"

    if not registry_path.exists():
        issues.append(
            "decision-registry.yaml missing from .aah/. "
            "Run rapids-registry init to create it."
        )
        return issues

    try:
        registry = read_yaml(registry_path)
    except Exception as e:
        return [f"decision-registry.yaml cannot be parsed: {e}"]

    if not isinstance(registry, dict):
        return ["decision-registry.yaml is not a valid YAML mapping"]

    if registry.get("schema_version") != "2.0":
        issues.append(
            f"decision-registry.yaml schema_version is '{registry.get('schema_version')}' "
            "(expected '2.0')"
        )

    project = registry.get("project", {})
    if not project.get("name"):
        issues.append("decision-registry.yaml missing project.name")
    if not project.get("archetype"):
        issues.append("decision-registry.yaml missing project.archetype")

    context = registry.get("context", {})
    populated_fields = 0
    for field in ("facts", "integrations", "nfrs", "organizational_constraints"):
        if context.get(field):
            populated_fields += 1
    if context.get("infrastructure_givens"):
        populated_fields += 1
    if context.get("deployment_target"):
        populated_fields += 1
    if context.get("delivery_intent"):
        populated_fields += 1

    if populated_fields == 0:
        issues.append(
            "decision-registry.yaml context block is empty — "
            "at least one fact category must be populated during research"
        )

    decisions = registry.get("decisions", [])
    if not decisions:
        issues.append("decision-registry.yaml has no decisions — run registry init")

    regimes = context.get("compliance_regimes", [])
    if regimes:
        has_eliminations = any(
            d.get("eliminated_options") for d in decisions
        )
        if not has_eliminations:
            issues.append(
                f"Compliance regimes detected ({regimes}) but no option eliminations applied. "
                "Run rapids-registry apply-eliminations."
            )

    # Validate leanings match actual DDR options (safety net)
    try:
        from aah.core.registry.ddr_loader import (
            get_valid_option_labels,
            resolve_resources_path,
        )

        resources_path = resolve_resources_path()
        ddr_sets = registry.get("project", {}).get("ddr_sets")
        for d in decisions:
            leaning = d.get("leaning_option")
            if leaning:
                valid = get_valid_option_labels(
                    resources_path, d["ddr_id"], ddr_sets
                )
                if valid and leaning not in valid:
                    issues.append(
                        f"Decision {d['ddr_id']}: leaning '{leaning}' is not a valid option. "
                        f"Valid: {valid}"
                    )
    except Exception as e:
        # Don't block the gate if index loading fails — warn instead
        issues.append(f"Warning: could not validate DDR leanings: {e}")

    return issues


def validate_research_gate(project_path: Path) -> tuple[bool, list[str]]:
    """Validate research phase completion."""
    issues = []
    aah_path = project_path / ".aah"

    manifest_path = aah_path / "manifest.yaml"
    if not manifest_path.exists():
        return True, []

    manifest = read_yaml(manifest_path)
    if manifest.get("current_phase") != "research":
        return True, []

    # aah-discuss is the sole discovery path — a slug-keyed registry + a
    # confirmed PRD under .aah/discuss/ are the contract. Deep validation is
    # owned by discuss.validate_constraints; here we only confirm both exist.
    discuss_registry_path = aah_path / "discuss" / "decision-registry.yaml"
    discuss_prd_path = aah_path / "discuss" / "discuss-prd.md"
    if not (discuss_registry_path.exists() and discuss_prd_path.exists()):
        issues.append(
            "discuss/decision-registry.yaml and discuss/discuss-prd.md not both "
            "found — /aah-discuss discovery phase is not complete"
        )
        return False, issues

    if issues:
        return False, issues

    return True, []


def _validate_legacy_research(rapids_path: Path) -> list[str]:
    """Validate legacy activity-plan.yaml based research."""
    issues = []
    activity_plan_path = rapids_path / "activity-plan.yaml"
    activity_plan = read_yaml(activity_plan_path)
    activities = activity_plan.get("activities", [])

    research_activities = [a for a in activities if a.get("phase") == "research"]
    if not research_activities:
        return []

    research_dir = rapids_path / "research"
    for activity in research_activities:
        activity_id = activity.get("activity_id", activity.get("id", "unknown"))
        status = activity.get("status", "pending")

        if status != "completed":
            issues.append(
                f"Activity '{activity_id}': status is '{status}' (expected 'completed')"
            )

        for artifact in activity.get("artifacts", []):
            artifact_name = artifact if isinstance(artifact, str) else artifact.get("file", artifact.get("template", ""))
            if artifact_name:
                artifact_path = research_dir / artifact_name
                if not artifact_path.exists():
                    candidates = [
                        research_dir / f"{artifact_name}.md",
                        research_dir / artifact_name.replace(".md", ""),
                    ]
                    if not any(c.exists() for c in candidates):
                        issues.append(f"Activity '{activity_id}': missing artifact '{artifact_name}'")

    return issues


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    passed, issues = validate_research_gate(project_path)

    if not passed:
        print("Research phase gate FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print("\nComplete all research activities before proceeding.", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
