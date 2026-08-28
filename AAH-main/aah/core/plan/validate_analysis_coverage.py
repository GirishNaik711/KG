#!/usr/bin/env python3
"""
Validate feature coverage against analysis synthesis.

Cross-references adr_refs, nfr_refs, and integration_refs in feature YAMLs
against the analysis-synthesis.json inventory. Reports gaps where analysis
decisions have no implementing feature.

Modes:
  gaps-only    — print uncovered items (for Step 4.5 gap detection)
  full-report  — write coverage-report.json with pass/fail (for Step 5.5 validation)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.dag import load_features_from_yamls
from aah.core.common.io_utils import read_json


def compute_coverage(synthesis: dict, features: list[dict]) -> dict:
    """Compute coverage of analysis items by features."""

    # Collect all refs from features
    feature_adr_refs: set[str] = set()
    feature_nfr_refs: set[str] = set()
    feature_int_refs: set[str] = set()

    for f in features:
        feature_adr_refs.update(f.get("adr_refs", []))
        feature_nfr_refs.update(f.get("nfr_refs", []))
        feature_int_refs.update(f.get("integration_refs", []))

    # ADR coverage
    adr_ids = {adr["id"] for adr in synthesis.get("adr_inventory", [])}
    covered_adrs = adr_ids & feature_adr_refs
    uncovered_adrs = sorted(adr_ids - feature_adr_refs)

    # NFR coverage (only critical/hard NFRs are required)
    all_nfrs = synthesis.get("nfr_inventory", [])
    critical_nfr_ids = {n["id"] for n in all_nfrs if n.get("priority", "").lower() == "hard"}
    all_nfr_ids = {n["id"] for n in all_nfrs}
    covered_critical = critical_nfr_ids & feature_nfr_refs
    uncovered_critical = sorted(critical_nfr_ids - feature_nfr_refs)

    # Integration coverage
    int_ids = {i["id"] for i in synthesis.get("integration_inventory", [])}
    covered_ints = int_ids & feature_int_refs
    uncovered_ints = sorted(int_ids - feature_int_refs)

    # Warnings: features referencing non-existent items
    warnings: list[str] = []
    for f in features:
        fid = f["id"]
        for ref in f.get("adr_refs", []):
            if ref not in adr_ids and adr_ids:
                warnings.append(f"{fid} references {ref} which does not exist in synthesis")
        for ref in f.get("nfr_refs", []):
            if ref not in all_nfr_ids and all_nfr_ids:
                warnings.append(f"{fid} references {ref} which does not exist in synthesis")
        for ref in f.get("integration_refs", []):
            if ref not in int_ids and int_ids:
                warnings.append(f"{fid} references {ref} which does not exist in synthesis")

    # Pass if no uncovered critical items
    pass_result = (
        len(uncovered_adrs) == 0
        and len(uncovered_critical) == 0
        and len(uncovered_ints) == 0
    )

    return {
        "coverage": {
            "adrs": {
                "total": len(adr_ids),
                "covered": len(covered_adrs),
                "uncovered": uncovered_adrs,
            },
            "nfrs": {
                "total": len(all_nfr_ids),
                "total_critical": len(critical_nfr_ids),
                "covered_critical": len(covered_critical),
                "uncovered_critical": uncovered_critical,
            },
            "integrations": {
                "total": len(int_ids),
                "covered": len(covered_ints),
                "uncovered": uncovered_ints,
            },
        },
        "warnings": warnings,
        "pass": pass_result,
        "user_acknowledged_gaps": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate feature coverage against analysis.")
    parser.add_argument(
        "--synthesis", type=Path, required=True,
        help="Path to analysis-synthesis.json",
    )
    parser.add_argument(
        "--features-dir", type=Path, required=True,
        help="Path to plan/features/ directory",
    )
    parser.add_argument(
        "--mode", choices=["gaps-only", "full-report"], default="full-report",
        help="Output mode",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output path for coverage-report.json (full-report mode only)",
    )
    args = parser.parse_args()

    if not args.synthesis.exists():
        print("No analysis-synthesis.json found — skipping coverage validation.", file=sys.stderr)
        sys.exit(0)

    synthesis = read_json(args.synthesis)
    features = load_features_from_yamls(args.features_dir)
    report = compute_coverage(synthesis, features)

    if args.mode == "gaps-only":
        coverage = report["coverage"]
        uncovered_adrs = coverage["adrs"]["uncovered"]
        uncovered_nfrs = coverage["nfrs"]["uncovered_critical"]
        uncovered_ints = coverage["integrations"]["uncovered"]

        if not uncovered_adrs and not uncovered_nfrs and not uncovered_ints:
            print("All analysis items covered by features.")
            sys.exit(0)

        if uncovered_adrs:
            print(f"Uncovered ADRs ({len(uncovered_adrs)}):")
            for adr_id in uncovered_adrs:
                adr_entry = next(
                    (a for a in synthesis.get("adr_inventory", []) if a["id"] == adr_id), {}
                )
                print(f"  {adr_id}: {adr_entry.get('decision_question', 'N/A')[:80]}")

        if uncovered_nfrs:
            print(f"Uncovered critical NFRs ({len(uncovered_nfrs)}):")
            for nfr_id in uncovered_nfrs:
                nfr_entry = next(
                    (n for n in synthesis.get("nfr_inventory", []) if n["id"] == nfr_id), {}
                )
                print(f"  {nfr_id} [{nfr_entry.get('category', '')}]: {nfr_entry.get('requirement', '')[:80]}")

        if uncovered_ints:
            print(f"Uncovered integrations ({len(uncovered_ints)}):")
            for int_id in uncovered_ints:
                int_entry = next(
                    (i for i in synthesis.get("integration_inventory", []) if i["id"] == int_id), {}
                )
                print(f"  {int_id}: {int_entry.get('system', 'N/A')} ({int_entry.get('protocol', '')})")

        sys.exit(0)

    # full-report mode
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Coverage report written to: {args.output}")

    json.dump(report, sys.stdout, indent=2)
    print()

    if not report["pass"]:
        coverage = report["coverage"]
        print(f"\nCoverage GAPS detected:", file=sys.stderr)
        if coverage["adrs"]["uncovered"]:
            print(f"  ADRs: {coverage['adrs']['uncovered']}", file=sys.stderr)
        if coverage["nfrs"]["uncovered_critical"]:
            print(f"  NFRs: {coverage['nfrs']['uncovered_critical']}", file=sys.stderr)
        if coverage["integrations"]["uncovered"]:
            print(f"  Integrations: {coverage['integrations']['uncovered']}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
