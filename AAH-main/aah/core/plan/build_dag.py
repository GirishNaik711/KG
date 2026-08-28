#!/usr/bin/env python3
"""Build two-level DAG from feature .md files and module-map.yaml.

Combines the module DAG (from architecture) with the feature DAG (from
reconciliation) into a single validated graph. Validates that feature-level
cross-module edges never contradict the module order.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.dag import (
    build_dag_from_features,
    dag_to_json,
    load_features_from_md,
    load_module_dag,
    validate_dag,
)
from aah.core.common.io_utils import read_yaml, write_json


def validate_spec_refs(features_dir: Path, specs_dir: Path) -> None:
    """Fail when a feature points at a missing source specification."""
    errors = []
    for feature in load_features_from_md(features_dir):
        spec_ref = feature.get("spec_ref")
        if spec_ref and not list(specs_dir.glob(f"{spec_ref}*.md")):
            errors.append(f"{feature.get('id', '?')}: missing spec_ref '{spec_ref}'")
    if errors:
        print("ERROR: Dangling spec_ref(s) found:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)


def validate_module_consistency(
    features: list[dict], module_map_path: Path
) -> list[str]:
    """Validate that cross-module feature edges are consistent with module DAG.

    If feature F-TASKS-01 depends on F-AUTH-01, then module 'tasks' must
    depend on module 'auth' in the module-map DAG.

    This is the SOLE owner of build-order/module-edge validity. It subsumes the
    former standalone ``validate_dependency_consistency`` check: a cross-module
    edge with no path in the module DAG is flagged here, and the old "implies a
    module dependency not in the map" branch was redundant (it only fired when
    there was already no path, which this check catches). Resolving module_ref
    name→id makes this stricter than the deleted check.
    """
    import networkx as nx

    module_G = load_module_dag(module_map_path)
    module_map_data = read_yaml(module_map_path)

    # Build name→id map so feature module_ref (name) resolves to module id
    name_to_id: dict[str, str] = {}
    for mod in module_map_data.get("modules", []):
        mod_id = mod.get("id") or mod.get("name")
        mod_name = mod.get("name", "")
        if mod_id and mod_name:
            name_to_id[mod_name] = mod_id

    errors = []

    feature_to_module = {
        f["id"]: name_to_id.get(f.get("module_ref", ""), f.get("module_ref"))
        for f in features
    }

    for feature in features:
        fid = feature["id"]
        fmod = name_to_id.get(feature.get("module_ref", ""), feature.get("module_ref"))
        for dep_id in feature.get("dependencies", []):
            dep_mod = feature_to_module.get(dep_id)
            if not dep_mod or not fmod:
                continue
            if dep_mod == fmod:
                continue
            if dep_mod not in module_G or fmod not in module_G:
                continue
            if not nx.has_path(module_G, dep_mod, fmod):
                errors.append(
                    f"Feature edge {dep_id} ({dep_mod}) → {fid} ({fmod}) "
                    f"contradicts module DAG: no path from '{dep_mod}' to '{fmod}'"
                )

    return errors


def build_and_validate_dag(
    features_dir: Path, module_map_path: Path | None = None
) -> dict:
    """Load features, build DAG, validate, and return serialized data."""
    specs_dir = features_dir.parent / "specs"
    if specs_dir.is_dir():
        validate_spec_refs(features_dir, specs_dir)
    features = load_features_from_md(features_dir)
    if not features:
        print("Error: no feature files found in " + str(features_dir), file=sys.stderr)
        sys.exit(1)

    try:
        G = build_dag_from_features(features)
    except ValueError as e:
        print(f"Error building DAG: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate_dag(G)

    if module_map_path and module_map_path.is_file():
        module_errors = validate_module_consistency(features, module_map_path)
        errors.extend(module_errors)

    if errors:
        print("DAG validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    return dag_to_json(G)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build two-level DAG from feature files and module-map"
    )
    parser.add_argument(
        "--features-dir", type=Path, required=True,
        help="Path to features/ directory (.md or .yaml files)"
    )
    parser.add_argument(
        "--module-map", type=Path, default=None,
        help="Path to module-map.yaml (optional, enables module consistency check)"
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="Output dag.json path"
    )
    args = parser.parse_args()

    if not args.features_dir.is_dir():
        print(f"Error: features directory not found: {args.features_dir}", file=sys.stderr)
        sys.exit(1)

    dag_data = build_and_validate_dag(args.features_dir, args.module_map)
    write_json(dag_data, args.output)

    json.dump({
        "status": "built",
        "nodes": len(dag_data["nodes"]),
        "edges": len(dag_data["edges"]),
        "module_map_validated": args.module_map is not None,
        "output": str(args.output),
    }, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
