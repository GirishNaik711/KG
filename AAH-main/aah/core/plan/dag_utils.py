#!/usr/bin/env python3
"""DAG utilities for plan-phase module dispatch ordering.

Provides:
- compute_topological_waves: group modules by DAG depth for dispatch sequencing
- get_direct_deps: direct depends_on list for a module (summaries to pass)
"""

import argparse
import json
import sys
from pathlib import Path

import networkx as nx

from aah.core.common.dag import load_module_dag
from aah.core.common.io_utils import read_yaml


def get_direct_deps(module_id: str, module_map_path: Path) -> list[str]:
    """Get direct depends_on list for a module.

    Returns the module's direct dependencies from module-map.yaml.
    These are the modules whose summaries should be passed to the agent —
    only direct parents, not full transitive closure, because depends_on
    expresses demo/execution ordering and agents only need context about
    modules they actually import code from.
    """
    data = read_yaml(module_map_path)
    modules = data.get("modules", [])

    for mod in modules:
        mid = mod.get("id") or mod.get("name")
        if mid == module_id:
            return sorted(mod.get("depends_on", []) or [])

    return []


def compute_topological_waves(module_map_path: Path) -> list[list[str]]:
    """Group modules by topological depth for dispatch sequencing.

    Modules at the same depth have all ancestors in prior waves,
    so they can be dispatched in parallel within their wave.

    Returns: [[MOD-000], [MOD-001, MOD-002], [MOD-003, MOD-005], ...]
    """
    G = load_module_dag(module_map_path)

    if not nx.is_directed_acyclic_graph(G):
        print("Error: module DAG contains cycles", file=sys.stderr)
        sys.exit(1)

    waves = [sorted(gen) for gen in nx.topological_generations(G)]
    return waves


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DAG utilities for plan-phase module dispatch"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # compute-topo-waves
    topo_p = sub.add_parser(
        "compute-topo-waves",
        help="Compute topological waves for module dispatch ordering",
    )
    topo_p.add_argument(
        "--module-map", type=Path, required=True,
        help="Path to module-map.yaml",
    )

    # get-direct-deps
    deps_p = sub.add_parser(
        "get-direct-deps",
        help="Get direct depends_on list for a module (summaries to pass)",
    )
    deps_p.add_argument(
        "--module", type=str, required=True,
        help="Module ID to get dependencies for",
    )
    deps_p.add_argument(
        "--module-map", type=Path, required=True,
        help="Path to module-map.yaml",
    )

    args = parser.parse_args()

    if args.command == "compute-topo-waves":
        if not args.module_map.is_file():
            print(f"Error: module-map not found: {args.module_map}", file=sys.stderr)
            sys.exit(1)
        waves = compute_topological_waves(args.module_map)
        json.dump(waves, sys.stdout, indent=2)
        print()

    elif args.command == "get-direct-deps":
        if not args.module_map.is_file():
            print(f"Error: module-map not found: {args.module_map}", file=sys.stderr)
            sys.exit(1)
        deps = get_direct_deps(args.module, args.module_map)
        json.dump(deps, sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()
