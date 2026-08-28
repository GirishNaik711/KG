#!/usr/bin/env python3
"""
Build end-to-end traceability matrix.

Maps: problem statement -> research findings -> design decisions ->
specifications -> features -> test cases -> code.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.dag import load_features_from_yamls
from aah.core.common.io_utils import read_yaml, write_text


def build_traceability(project_path: Path) -> dict:
    """Build the traceability matrix from .aah/ artifacts."""
    rapids_path = project_path / ".aah"
    matrix = {
        "specs": {},
        "features": {},
        "orphaned_features": [],
    }

    # Load specs
    specs_dir = rapids_path / "plan" / "specs"
    if specs_dir.is_dir():
        for spec_file in sorted(specs_dir.glob("*.md")) + sorted(specs_dir.glob("*.yaml")):
            spec_id = spec_file.stem
            matrix["specs"][spec_id] = {
                "file": str(spec_file.relative_to(rapids_path)),
                "features": [],
            }

    # Load features and map to specs
    features_dir = rapids_path / "plan" / "features"
    features = load_features_from_yamls(features_dir)

    for feature in features:
        fid = feature["id"]
        spec_ref = feature.get("spec_ref", "")
        test_cases = feature.get("test_cases", [])

        # Check test results
        test_result_path = rapids_path / "implement" / "test-results" / f"{fid}.json"
        has_test_results = test_result_path.exists()

        matrix["features"][fid] = {
            "description": feature.get("description", ""),
            "spec_ref": spec_ref,
            "dependencies": feature.get("dependencies", []),
            "acceptance_criteria_count": len(feature.get("acceptance_criteria", [])),
            "test_cases_count": len(test_cases),
            "test_case_ids": [tc.get("id", "") for tc in test_cases if isinstance(tc, dict)],
            "has_test_results": has_test_results,
            "status": feature.get("status", "unknown"),
        }

        # Link back to spec
        if spec_ref in matrix["specs"]:
            matrix["specs"][spec_ref]["features"].append(fid)
        elif spec_ref:
            matrix["orphaned_features"].append({
                "feature": fid,
                "missing_spec": spec_ref,
            })

    # ADR → Features reverse mapping
    matrix["adr_coverage"] = {}
    decisions_dir = rapids_path / "analysis" / "decisions"
    if decisions_dir.is_dir():
        for f in sorted(decisions_dir.glob("ADR-*.md")):
            adr_id = f.stem
            matrix["adr_coverage"][adr_id] = {
                "file": str(f.relative_to(rapids_path)),
                "features": [],
            }

    for feature in features:
        for adr_ref in feature.get("adr_refs", []):
            if adr_ref in matrix["adr_coverage"]:
                matrix["adr_coverage"][adr_ref]["features"].append(feature["id"])

    # NFR → Features mapping
    matrix["nfr_coverage"] = {}
    for feature in features:
        for nfr_ref in feature.get("nfr_refs", []):
            matrix["nfr_coverage"].setdefault(nfr_ref, []).append(feature["id"])

    # Integration → Features mapping
    matrix["integration_coverage"] = {}
    for feature in features:
        for int_ref in feature.get("integration_refs", []):
            matrix["integration_coverage"].setdefault(int_ref, []).append(feature["id"])

    # Check for research artifacts
    research_dir = rapids_path / "research"
    matrix["research_artifacts"] = []
    if research_dir.is_dir():
        for f in sorted(research_dir.glob("*.md")):
            if f.name != "research-plan.yaml":
                matrix["research_artifacts"].append(str(f.relative_to(rapids_path)))

    # Check for ADRs
    decisions_dir = rapids_path / "analysis" / "decisions"
    matrix["decisions"] = []
    if decisions_dir.is_dir():
        for f in sorted(decisions_dir.glob("*.md")):
            matrix["decisions"].append(str(f.relative_to(rapids_path)))

    return matrix


def render_traceability_markdown(matrix: dict) -> str:
    """Render the traceability matrix as a markdown document."""
    lines = ["# Traceability Matrix", ""]

    # Research
    if matrix.get("research_artifacts"):
        lines.append("## Research Artifacts")
        for artifact in matrix["research_artifacts"]:
            lines.append(f"- {artifact}")
        lines.append("")

    # Decisions
    if matrix.get("decisions"):
        lines.append("## Architecture Decisions")
        for decision in matrix["decisions"]:
            lines.append(f"- {decision}")
        lines.append("")

    # Specs -> Features mapping
    lines.append("## Specification to Feature Mapping")
    lines.append("")
    lines.append("| Spec | Features | Coverage |")
    lines.append("|------|----------|----------|")
    for spec_id, spec_data in matrix.get("specs", {}).items():
        features = spec_data.get("features", [])
        coverage = "covered" if features else "**no features**"
        lines.append(f"| {spec_id} | {', '.join(features)} | {coverage} |")
    lines.append("")

    # Features detail
    lines.append("## Feature Traceability")
    lines.append("")
    lines.append("| Feature | Spec | Tests | Criteria | Results |")
    lines.append("|---------|------|-------|----------|---------|")
    for fid, fdata in matrix.get("features", {}).items():
        tests = fdata.get("test_cases_count", 0)
        criteria = fdata.get("acceptance_criteria_count", 0)
        results = "yes" if fdata.get("has_test_results") else "no"
        lines.append(f"| {fid} | {fdata.get('spec_ref', '')} | {tests} | {criteria} | {results} |")
    lines.append("")

    # Orphaned features
    if matrix.get("orphaned_features"):
        lines.append("## Orphaned Features (Missing Specs)")
        for orphan in matrix["orphaned_features"]:
            lines.append(f"- {orphan['feature']}: references missing spec {orphan['missing_spec']}")
        lines.append("")

    # ADR Coverage
    if matrix.get("adr_coverage"):
        lines.append("## ADR Coverage")
        lines.append("")
        lines.append("| ADR | Features | Coverage |")
        lines.append("|-----|----------|----------|")
        for adr_id, adr_data in matrix["adr_coverage"].items():
            features_list = adr_data.get("features", [])
            coverage = "covered" if features_list else "**uncovered**"
            lines.append(f"| {adr_id} | {', '.join(features_list)} | {coverage} |")
        lines.append("")

    # NFR Coverage
    if matrix.get("nfr_coverage"):
        lines.append("## NFR Coverage")
        lines.append("")
        lines.append("| NFR ID | Features |")
        lines.append("|--------|----------|")
        for nfr_id, feature_ids in matrix["nfr_coverage"].items():
            lines.append(f"| {nfr_id} | {', '.join(feature_ids)} |")
        lines.append("")

    # Integration Coverage
    if matrix.get("integration_coverage"):
        lines.append("## Integration Coverage")
        lines.append("")
        lines.append("| Integration | Features |")
        lines.append("|-------------|----------|")
        for int_id, feature_ids in matrix["integration_coverage"].items():
            lines.append(f"| {int_id} | {', '.join(feature_ids)} |")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build traceability matrix")
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None, help="Output path (default: .aah/audit/traceability-matrix.md)")
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    matrix = build_traceability(project_path)

    # Write JSON
    output_path = args.output or (project_path / ".aah" / "audit" / "traceability-matrix.md")
    markdown = render_traceability_markdown(matrix)
    write_text(markdown, output_path)

    # Also write raw JSON for machine consumption
    json_path = output_path.with_suffix(".json")
    from aah.core.common.io_utils import write_json
    write_json(matrix, json_path)

    json.dump({"output": str(output_path), "features": len(matrix.get("features", {}))}, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
