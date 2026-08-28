#!/usr/bin/env python3
"""Load, validate, and list DDR v2.0 playbooks."""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml


VERDICTS = {"favours", "neutral", "trades-off"}
INDEX_FILENAME = "ddr-index.json"


def resolve_resources_path(framework_root: Path | None = None) -> Path:
    if framework_root is None:
        framework_root = resolve_framework_root()
    if framework_root is None:
        print("Error: cannot determine framework root", file=sys.stderr)
        sys.exit(1)
    return Path(framework_root) / "build-playbooks"


def list_ddr_sets(resources_path: Path) -> list[str]:
    """Return all DDR set names (directories under _resources that have a decisions/ subfolder)."""
    sets = []
    if not resources_path.exists():
        return sets
    for entry in sorted(resources_path.iterdir()):
        if entry.is_dir() and (entry / "decisions").is_dir():
            sets.append(entry.name)
    return sets


def load_all_ddrs(resources_path: Path, ddr_sets: list[str] | None = None) -> list[dict]:
    """Load all DDR YAML files from the given DDR sets.

    Returns a list of dicts, each augmented with:
      - _ddr_set: the DDR set folder name (e.g., 'ai-applications', 'shared')
      - _path: absolute path to the YAML file
    """
    ddrs = []
    if ddr_sets is None:
        ddr_sets = list_ddr_sets(resources_path)

    for ddr_set in ddr_sets:
        decisions_dir = resources_path / ddr_set / "decisions"
        if not decisions_dir.exists():
            continue
        for yaml_file in sorted(decisions_dir.rglob("DDR-*.yaml")):
            try:
                data = read_yaml(yaml_file)
                data["_ddr_set"] = ddr_set
                data["_path"] = str(yaml_file)
                ddrs.append(data)
            except Exception as e:
                print(f"Warning: failed to parse {yaml_file}: {e}", file=sys.stderr)
    return ddrs


def load_ddr_by_id(resources_path: Path, ddr_id: str,
                    ddr_sets: list[str] | None = None) -> dict | None:
    """Load a specific DDR by its ID."""
    all_ddrs = load_all_ddrs(resources_path, ddr_sets)
    for ddr in all_ddrs:
        if ddr.get("id") == ddr_id:
            return ddr
    return None


def validate_ddr(ddr: dict) -> list[str]:
    """Validate a DDR playbook for v2.0 schema completeness.

    Returns a list of error strings. Empty list means valid.
    """
    errors = []
    path_label = ddr.get("_path", ddr.get("id", "unknown"))

    if ddr.get("schema_version") != "2.0":
        errors.append(f"{path_label}: schema_version must be '2.0', got '{ddr.get('schema_version')}'")

    for required in ("id", "category", "layer", "layer_number", "decision_question"):
        if required not in ddr:
            errors.append(f"{path_label}: missing required field '{required}'")

    if ddr.get("category") not in ("archetype", "shared"):
        errors.append(f"{path_label}: category must be 'archetype' or 'shared', got '{ddr.get('category')}'")

    forces = ddr.get("forces", [])
    if not forces:
        errors.append(f"{path_label}: forces list is empty")

    force_ids = set()
    for i, force in enumerate(forces):
        fid = force.get("id")
        if not fid:
            errors.append(f"{path_label}: force[{i}] missing 'id'")
        else:
            force_ids.add(fid)
        if not force.get("tension"):
            errors.append(f"{path_label}: force[{i}] missing 'tension'")
        if not force.get("description"):
            errors.append(f"{path_label}: force[{i}] missing 'description'")

    options = ddr.get("options", [])
    if not options:
        errors.append(f"{path_label}: options list is empty")

    for i, option in enumerate(options):
        label = option.get("label", f"option[{i}]")
        if not option.get("description"):
            errors.append(f"{path_label}: option '{label}' missing 'description'")

        fr = option.get("force_resolution", {})
        if not fr:
            errors.append(f"{path_label}: option '{label}' missing 'force_resolution'")
            continue

        for fid in force_ids:
            if fid not in fr:
                errors.append(f"{path_label}: option '{label}' missing force_resolution for '{fid}'")
            else:
                cell = fr[fid]
                verdict = cell.get("verdict")
                if verdict not in VERDICTS:
                    errors.append(
                        f"{path_label}: option '{label}' force '{fid}' "
                        f"verdict must be one of {VERDICTS}, got '{verdict}'"
                    )
                if not cell.get("detail"):
                    errors.append(
                        f"{path_label}: option '{label}' force '{fid}' missing 'detail'"
                    )

    return errors


def compute_resolution_order(ddrs: list[dict]) -> list[list[str]]:
    """Compute dependency-driven resolution waves.

    Returns a list of waves, each wave is a list of DDR IDs that can be
    resolved in parallel (all dependencies satisfied by prior waves).
    """
    id_to_deps: dict[str, list[str]] = {}
    for ddr in ddrs:
        ddr_id = ddr.get("id", "")
        deps = ddr.get("depends_on", [])
        id_to_deps[ddr_id] = deps

    resolved: set[str] = set()
    waves: list[list[str]] = []
    remaining = set(id_to_deps.keys())

    while remaining:
        wave = []
        for ddr_id in sorted(remaining):
            deps = id_to_deps[ddr_id]
            if all(d in resolved for d in deps):
                wave.append(ddr_id)
        if not wave:
            wave = sorted(remaining)
            print(
                f"Warning: circular dependency detected, forcing: {wave}",
                file=sys.stderr,
            )
        for ddr_id in wave:
            remaining.discard(ddr_id)
            resolved.add(ddr_id)
        waves.append(wave)

    return waves


def build_ddr_index(resources_path: Path, ddr_set: str) -> list[dict]:
    """Build a lightweight index for a single DDR set (archetype or shared).

    Called by maintainers/CI to regenerate the index after DDR changes.
    Reads all DDR YAML files in the set and extracts only the fields needed
    for probing and validation: id, decision_question, layer, options[].label.
    """
    all_ddrs = load_all_ddrs(resources_path, [ddr_set])
    index = []
    for ddr in all_ddrs:
        entry = {
            "id": ddr.get("id"),
            "decision_question": ddr.get("decision_question", ""),
            "layer": ddr.get("layer", ""),
            "layer_number": ddr.get("layer_number"),
            "category": ddr.get("category", ""),
            "options": [opt.get("label", "") for opt in ddr.get("options", [])],
        }
        if ddr.get("depends_on"):
            entry["depends_on"] = ddr["depends_on"]
        # Include option descriptions for richer probing context
        options_with_desc = ddr.get("options", [])
        if options_with_desc and isinstance(options_with_desc[0], dict):
            descs = {}
            for opt in options_with_desc:
                label = opt.get("label", "")
                desc = opt.get("description", "").strip()
                if label and desc:
                    descs[label] = desc[:200]  # Truncate for index size
            if descs:
                entry["option_descriptions"] = descs
        index.append(entry)
    return index


def load_ddr_index(resources_path: Path, ddr_sets: list[str] | None = None) -> list[dict]:
    """Load pre-built DDR indexes for the given sets and combine them.

    Follows the same ddr_sets convention as load_all_ddrs():
    - If ddr_sets is None, discovers all sets via list_ddr_sets()
    - Otherwise loads index for each specified set

    Falls back to building on-the-fly if an index file is missing (with warning).
    """
    if ddr_sets is None:
        ddr_sets = list_ddr_sets(resources_path)
    combined = []
    for ddr_set in ddr_sets:
        index_path = resources_path / ddr_set / INDEX_FILENAME
        if index_path.exists():
            with open(index_path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                combined.extend(data)
            elif isinstance(data, dict) and "layers" in data:
                for layer in data["layers"]:
                    combined.extend(layer.get("decisions", []))
            else:
                combined.extend(data if isinstance(data, list) else [])
        else:
            print(
                f"Warning: {index_path} not found, building on-the-fly",
                file=sys.stderr,
            )
            combined.extend(build_ddr_index(resources_path, ddr_set))
    return combined


def get_valid_option_labels(
    resources_path: Path, ddr_id: str, ddr_sets: list[str] | None = None
) -> list[str]:
    """Return valid option labels for a DDR by reading the pre-built index.

    Used by update_decision() and research gate for validation.
    Falls back to loading the full YAML if the index entry lacks options.
    """
    index = load_ddr_index(resources_path, ddr_sets)
    for entry in index:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == ddr_id:
            options = entry.get("options", [])
            if options:
                return options
            # Index entry lacks options — load from YAML
            ddr = load_ddr_by_id(resources_path, ddr_id, ddr_sets)
            if ddr:
                return [opt.get("label", "") for opt in ddr.get("options", [])]
            return []
    # Not found in index — try loading directly
    ddr = load_ddr_by_id(resources_path, ddr_id, ddr_sets)
    if ddr:
        return [opt.get("label", "") for opt in ddr.get("options", [])]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR v2.0 playbook loader")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list-ddrs", help="List all DDRs")
    list_p.add_argument("--ddr-set", type=str, default=None, help="Filter by DDR set")
    list_p.add_argument("--format", choices=["summary", "json", "ids"], default="summary")

    load_p = sub.add_parser("load-ddr", help="Load a single DDR by ID")
    load_p.add_argument("--id", type=str, required=True, help="DDR ID (e.g., DDR-L5-001)")
    load_p.add_argument("--format", choices=["yaml", "json"], default="json")

    val_p = sub.add_parser("validate-ddr", help="Validate DDR schema")
    val_p.add_argument("--path", type=str, default=None, help="Validate a specific file")
    val_p.add_argument("--all", action="store_true", help="Validate all DDRs")

    order_p = sub.add_parser("resolution-order", help="Compute resolution wave order")
    order_p.add_argument("--ddr-sets", type=str, nargs="+", default=None)

    build_idx_p = sub.add_parser("build-index", help="Build DDR index (developer/CI tool)")
    build_idx_p.add_argument("--ddr-set", type=str, default=None, help="Single DDR set to index")
    build_idx_p.add_argument("--all", action="store_true", help="Rebuild indexes for all DDR sets")

    load_idx_p = sub.add_parser("load-index", help="Load and combine pre-built DDR indexes")
    load_idx_p.add_argument("--ddr-sets", type=str, nargs="+", default=None)

    sub.add_parser("list-ddr-sets", help="List all available DDR sets (archetypes)")

    args = parser.parse_args()
    resources_path = resolve_resources_path()

    if args.command == "list-ddrs":
        ddr_sets = [args.ddr_set] if args.ddr_set else None
        ddrs = load_all_ddrs(resources_path, ddr_sets)
        if args.format == "summary":
            print(f"{'ID':<16} {'Set':<20} {'Layer':<25} {'Question'}")
            print(f"{'─'*16} {'─'*20} {'─'*25} {'─'*50}")
            for ddr in ddrs:
                q = ddr.get("decision_question", "").strip()[:55]
                print(
                    f"{ddr.get('id', 'N/A'):<16} "
                    f"{ddr.get('_ddr_set', 'N/A'):<20} "
                    f"{ddr.get('layer', 'N/A'):<25} "
                    f"{q}"
                )
        elif args.format == "json":
            clean = [{k: v for k, v in d.items() if not k.startswith("_")} for d in ddrs]
            json.dump(clean, sys.stdout, indent=2)
            print()
        elif args.format == "ids":
            for ddr in ddrs:
                print(ddr.get("id", ""))

    elif args.command == "load-ddr":
        ddr = load_ddr_by_id(resources_path, args.id)
        if ddr is None:
            print(f"Error: DDR '{args.id}' not found", file=sys.stderr)
            sys.exit(1)
        clean = {k: v for k, v in ddr.items() if not k.startswith("_")}
        if args.format == "json":
            json.dump(clean, sys.stdout, indent=2)
            print()
        else:
            import yaml as yaml_mod
            print(yaml_mod.dump(clean, default_flow_style=False, sort_keys=False))

    elif args.command == "validate-ddr":
        all_errors = []
        if args.path:
            data = read_yaml(Path(args.path))
            data["_path"] = args.path
            all_errors = validate_ddr(data)
        elif args.all:
            ddrs = load_all_ddrs(resources_path)
            for ddr in ddrs:
                all_errors.extend(validate_ddr(ddr))
        else:
            print("Error: specify --path or --all", file=sys.stderr)
            sys.exit(1)

        result = {"valid": len(all_errors) == 0, "error_count": len(all_errors), "errors": all_errors}
        json.dump(result, sys.stdout, indent=2)
        print()
        if all_errors:
            sys.exit(1)

    elif args.command == "resolution-order":
        ddrs = load_all_ddrs(resources_path, args.ddr_sets)
        waves = compute_resolution_order(ddrs)
        result = {"wave_count": len(waves), "waves": []}
        for i, wave in enumerate(waves):
            result["waves"].append({"wave": i, "ddr_ids": wave})
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "build-index":
        if args.all:
            sets_to_build = list_ddr_sets(resources_path)
        elif args.ddr_set:
            sets_to_build = [args.ddr_set]
        else:
            print("Error: specify --ddr-set <name> or --all", file=sys.stderr)
            sys.exit(1)

        for ddr_set in sets_to_build:
            index = build_ddr_index(resources_path, ddr_set)
            out_path = resources_path / ddr_set / INDEX_FILENAME
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=2)
                f.write("\n")
            print(
                f"Built {out_path}: {len(index)} DDRs indexed",
                file=sys.stderr,
            )

        json.dump(
            {"built": True, "sets": sets_to_build},
            sys.stdout,
            indent=2,
        )
        print()

    elif args.command == "load-index":
        index = load_ddr_index(resources_path, args.ddr_sets)
        json.dump(index, sys.stdout, indent=2)
        print()

    elif args.command == "list-ddr-sets":
        ddr_sets = list_ddr_sets(resources_path)
        json.dump({"ddr_sets": ddr_sets}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
