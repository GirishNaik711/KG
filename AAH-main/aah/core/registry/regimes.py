#!/usr/bin/env python3
"""Regime detection and option elimination for the decision registry.

Delegates to the core functions in aah.core.intake.resolve_constraints
(kept as a library) and provides a CLI interface for registry operations.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml
from aah.core.intake.resolve_constraints import (
    detect_triggered_regimes,
    collect_regime_mandates,
    flatten_eliminated_options,
    load_regime_catalog_files,
    load_archetype_catalog,
    flatten_intake_text,
    APPLIES_WHEN,
)


def resolve_resources_path(framework_root: Path | None = None) -> Path:
    if framework_root is None:
        framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root", file=sys.stderr)
        sys.exit(1)
    return Path(framework_root) / "build-playbooks"


def detect_regimes_from_context(context: dict, resources_path: Path) -> list[str]:
    """Detect compliance/infrastructure regimes from registry context block.

    Examines context.compliance_regimes (explicit list) and scans text fields
    for trigger signals from regime catalogs.
    """
    triggered = list(context.get("compliance_regimes", []))

    text_parts = []
    for fact in context.get("facts", []):
        text_parts.append(str(fact))
    for integration in context.get("integrations", []):
        text_parts.append(str(integration.get("system", "")))
        text_parts.append(str(integration.get("protocol", "")))
    givens = context.get("infrastructure_givens", {})
    for v in givens.values():
        text_parts.append(str(v))
    for nfr in context.get("nfrs", []):
        text_parts.append(str(nfr.get("target", "")))
        text_parts.append(str(nfr.get("category", "")))
    for constraint in context.get("organizational_constraints", []):
        text_parts.append(str(constraint.get("statement", "")))

    text = " ".join(text_parts).lower()

    shared_catalogs = load_regime_catalog_files(resources_path / "constraints")
    archetype_catalogs = load_regime_catalog_files(resources_path)
    all_catalogs = shared_catalogs + archetype_catalogs

    detected = detect_triggered_regimes(text, all_catalogs)
    for regime in detected:
        if regime not in triggered:
            triggered.append(regime)

    return triggered


def get_eliminations(
    context: dict,
    resources_path: Path,
    archetype: str = "ai-applications",
) -> list[dict]:
    """Get all option eliminations from triggered regimes and engineering mandates.

    Returns a flat list of:
      {ddr_id, option, reason, source: "mandate"|"regime"|"engineering"}
    """
    triggered = detect_regimes_from_context(context, resources_path)

    shared_catalogs = load_regime_catalog_files(resources_path / "constraints")
    archetype_constraint_dir = resources_path / archetype / "constraints"
    archetype_catalogs = load_regime_catalog_files(archetype_constraint_dir)
    all_catalogs = shared_catalogs + archetype_catalogs

    regime_mandates = collect_regime_mandates(all_catalogs, triggered)
    regime_eliminations = flatten_eliminated_options(
        regime_mandates, "by_mandate", "mandate_id"
    )
    for e in regime_eliminations:
        e["source"] = "regime"

    text_parts = []
    for fact in context.get("facts", []):
        text_parts.append(str(fact))
    text = " ".join(text_parts).lower()

    archetype_catalog_path = archetype_constraint_dir / "catalog.yaml"
    eng_mandates = load_archetype_catalog(archetype_catalog_path, text)
    eng_eliminations = flatten_eliminated_options(
        eng_mandates, "by_mandate", "mandate_id"
    )
    for e in eng_eliminations:
        e["source"] = "engineering"

    all_eliminations = regime_eliminations + eng_eliminations

    seen = set()
    deduped = []
    for e in all_eliminations:
        key = (e["ddr_id"], e["option"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(description="Regime detection and elimination")
    sub = parser.add_subparsers(dest="command", required=True)

    detect_p = sub.add_parser("detect-regimes", help="Detect triggered regimes from registry context")
    detect_p.add_argument("--registry", type=str, required=True, help="Path to decision-registry.yaml")

    elim_p = sub.add_parser("get-eliminations", help="Get option eliminations from regimes")
    elim_p.add_argument("--registry", type=str, required=True, help="Path to decision-registry.yaml")
    elim_p.add_argument("--archetype", type=str, default="ai-applications")

    args = parser.parse_args()
    resources_path = resolve_resources_path()

    if args.command == "detect-regimes":
        registry = read_yaml(Path(args.registry))
        context = registry.get("context", {})
        regimes = detect_regimes_from_context(context, resources_path)
        result = {"triggered_regimes": regimes}
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "get-eliminations":
        registry = read_yaml(Path(args.registry))
        context = registry.get("context", {})
        eliminations = get_eliminations(context, resources_path, args.archetype)
        result = {"elimination_count": len(eliminations), "eliminations": eliminations}
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
