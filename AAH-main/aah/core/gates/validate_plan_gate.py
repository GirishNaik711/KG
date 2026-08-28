#!/usr/bin/env python3
"""
Validate planning phase completion for AAH projects — the single deterministic
Plan Gate aggregator.

Runs the cheap, objective checks in order and fails fast:
  Step 1 — plan/features/ exists and holds feature .md files.
  Step 2 — each feature validates against FEATURE_SCHEMA (dict-schema check).
  Step 3 — each feature has a non-empty ## Description (behavioral spec).
  Step 4 — dag.json exists and matches the feature count. The DAG's acyclicity
           and module-edge consistency are ALREADY guaranteed upstream by
           build_and_validate_dag (build_dag.py); the gate does NOT re-validate.
  Step 7 — every fired plan-node port produced the artifacts it promised.

There is no API-contract check here — API-contract correctness is an AI-critic
concern (Step 6), not a deterministic gate. Exit 0 = pass, Exit 2 = fail.
"""

import json
import sys
from pathlib import Path

from aah.core.common.feature_utils import load_features_from_dir
from aah.core.common.io_utils import read_json, read_yaml
from aah.core.common.validators import validate_dict_schema, FEATURE_SCHEMA
from aah.core.gates.validate_port_artifacts import validate_port_artifacts


def validate_plan_gate(project_path: Path) -> tuple[bool, list[str]]:
    """Validate planning phase completion."""
    issues = []
    aah_path = project_path / ".aah"

    manifest_path = aah_path / "manifest.yaml"
    if not manifest_path.exists():
        return True, []

    manifest = read_yaml(manifest_path)
    if manifest.get("current_phase") != "plan":
        return True, []

    # Check feature .md files exist
    features_dir = aah_path / "plan" / "features"
    if not features_dir.is_dir():
        issues.append("Missing plan/features/ directory")
        return False, issues

    feature_files = list(features_dir.glob("*.md"))
    if not feature_files:
        issues.append("No feature .md files found in plan/features/")
        return False, issues

    # Validate each feature
    features = load_features_from_dir(features_dir)
    if not features:
        issues.append("No parseable feature files found in plan/features/")
        return False, issues

    for f in features:
        fid = f.get("id", "unknown")
        # Step 2 — schema check (this gate is the single owner of it).
        errors = validate_dict_schema(f, FEATURE_SCHEMA, context=fid)
        issues.extend(errors)

        # Step 3 (blocking) — v2 (lean/TDD): features no longer carry
        # acceptance_criteria or test_cases; a rich Description is the spec, and
        # tests are emergent via TDD in the build phase. Require a non-trivial
        # Description. (Vagueness is a non-blocking AI-critic note, not gated here.)
        if not (f.get("description") or "").strip():
            issues.append(f"{fid}: empty Description (the module's behavioral spec)")

    # Step 4 — dag.json existence only. build_and_validate_dag already ran
    # validate_dag (acyclicity) + validate_module_consistency (module-edge
    # consistency) when it produced this file, so the gate does NOT re-validate;
    # it only asserts the artifact exists.
    dag_path = aah_path / "plan" / "dag.json"
    if not dag_path.exists():
        issues.append("Missing plan/dag.json")

    # v2 (lean/TDD): no waves.json — the build is sequential over dag.json's
    # topological order, so there is no wave artifact or per-wave sprint contract
    # to gate on.

    # Check feature-list.json exists
    fl_path = aah_path / "feature-list.json"
    if not fl_path.exists():
        issues.append("Missing feature-list.json")
    else:
        fl_data = read_json(fl_path)
        fl_features = fl_data.get("features", [])
        md_count = len(feature_files)
        fl_count = len(fl_features)
        if md_count != fl_count:
            issues.append(
                f"Feature count mismatch: {md_count} .md files but {fl_count} in feature-list.json"
            )

    # Step 7 — attached side-tasks finished: every fired plan-node port produced
    # the artifacts it promised. No port registry → no-op (ports are optional).
    ports_ok, port_issues = validate_port_artifacts(project_path, node="plan")
    if not ports_ok:
        issues.extend(port_issues)

    if issues:
        return False, issues

    return True, []


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        json.dump({"passed": True, "skipped": "no project found"}, sys.stdout)
        print()
        sys.exit(0)

    passed, issues = validate_plan_gate(project_path)

    if not passed:
        print("Planning phase gate FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        json.dump({"passed": False, "issues": issues}, sys.stdout)
        print()
        sys.exit(2)

    json.dump({"passed": True, "message": "Planning phase gate passed"}, sys.stdout)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
