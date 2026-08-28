#!/usr/bin/env python3
"""DAG construction, validation, and topological sort using networkx."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import networkx as nx

from aah.core.common.io_utils import read_json, read_yaml, write_json


def build_dag_from_features(features: list[dict]) -> nx.DiGraph:
    """
    Build a directed acyclic graph from feature definitions.

    Each feature has an 'id' and a 'dependencies' list of feature IDs.
    Edges go from dependency -> dependent (dependency must complete first).
    """
    G = nx.DiGraph()

    for feature in features:
        fid = feature["id"]
        G.add_node(fid, **{k: v for k, v in feature.items() if k != "id"})

    for feature in features:
        fid = feature["id"]
        for dep in feature.get("dependencies", []):
            if dep not in G:
                raise ValueError(f"Feature '{fid}' depends on unknown feature '{dep}'")
            G.add_edge(dep, fid)

    return G


def validate_dag(G: nx.DiGraph) -> list[str]:
    """Validate DAG properties. Returns list of errors."""
    errors = []

    if not nx.is_directed_acyclic_graph(G):
        cycles = list(nx.simple_cycles(G))
        for cycle in cycles[:5]:
            errors.append(f"Cycle detected: {' -> '.join(cycle + [cycle[0]])}")

    return errors


def _expand_scopes_with_blast_radius(
    scopes: dict[str, set[str]], project_path: Path | None
) -> dict[str, set[str]]:
    """Expand declared file_scope sets with structural dependents from codemap.

    For brownfield projects with codemap.db available, each file in a feature's
    file_scope is expanded to include its direct dependents (files that import or
    call into it). This ensures that features touching structurally coupled files
    are serialized into separate sub-waves even when they don't share the same
    literal file path.

    Returns the original scopes unmodified when:
    - project_path is None
    - project is not brownfield
    - codemap.db does not exist
    - codemap-scale is not installed
    """
    if not project_path or not scopes:
        return scopes

    from aah.core.common.codemap_utils import get_db_path, is_available

    if not is_available():
        return scopes

    codemap_db = get_db_path(project_path)
    if not codemap_db.exists():
        return scopes

    # Load codemap instance once for all features
    try:
        from aah.core.common.codemap_utils import get_codemap

        cm = get_codemap(project_path)
    except Exception:
        return scopes

    expanded: dict[str, set[str]] = {}
    for fid, files in scopes.items():
        expanded_files = set(files)
        for file_path in files:
            try:
                impact = cm.tools.analyze_impact(file_path)
                # Add direct dependents — files that import/call this file
                direct_deps = impact.get("direct_dependents", [])
                expanded_files.update(direct_deps)
            except Exception:
                # File not in codemap index — keep original scope only
                continue
        expanded[fid] = expanded_files

    return expanded


def _split_by_file_scope(
    G: nx.DiGraph, generation: list[str], project_path: Path | None = None
) -> list[list[str]]:
    """
    Split a topological generation into sub-waves where no two features
    share files.  Uses a greedy bucket approach.

    If no features in the generation declare file_scope, returns the
    generation as a single wave (backward compatible).

    For brownfield projects with codemap.db available, file_scope is expanded
    with structural dependents (blast radius) to detect implicit coupling
    between features that touch related files.
    """
    scopes: dict[str, set[str]] = {}
    for fid in generation:
        fs = G.nodes[fid].get("file_scope") or []
        if fs:
            scopes[fid] = set(fs)

    if not scopes:
        return [generation]

    # Expand scopes with codemap blast radius for brownfield projects
    scopes = _expand_scopes_with_blast_radius(scopes, project_path)

    sub_waves: list[tuple[set[str], list[str]]] = []  # (combined_files, feature_ids)

    for fid in generation:
        fid_files = scopes.get(fid, set())
        placed = False

        for combined_files, bucket in sub_waves:
            if not (combined_files & fid_files):  # no overlap
                combined_files.update(fid_files)
                bucket.append(fid)
                placed = True
                break

        if not placed:
            sub_waves.append((set(fid_files), [fid]))

    return [sorted(bucket) for _, bucket in sub_waves]


def compute_execution_waves(
    G: nx.DiGraph, project_path: Path | None = None
) -> list[list[str]]:
    """
    Compute execution waves using topological generations (Kahn's algorithm).

    Each wave contains features that can be executed in parallel
    (all their dependencies are in earlier waves).  Within each
    generation, features that declare overlapping file_scope are
    split into separate sub-waves to prevent merge conflicts.

    For brownfield projects, pass project_path to enable codemap-based
    blast radius expansion — structural dependents from codemap.db are
    used to detect implicit file coupling beyond literal file_scope overlap.
    """
    if not nx.is_directed_acyclic_graph(G):
        raise ValueError("Cannot compute waves: graph contains cycles")

    waves = []
    for generation in nx.topological_generations(G):
        sub_waves = _split_by_file_scope(G, sorted(generation), project_path)
        waves.extend(sub_waves)

    return waves


def get_execution_frontier(G: nx.DiGraph, completed: set[str]) -> list[str]:
    """
    Get features whose dependencies are all satisfied (completed).
    These are the features available for implementation now.
    """
    frontier = []
    for node in G.nodes:
        if node in completed:
            continue
        predecessors = set(G.predecessors(node))
        if predecessors.issubset(completed):
            frontier.append(node)
    return sorted(frontier)


def get_feature_depth(G: nx.DiGraph, feature_id: str) -> int:
    """Get the depth (longest path from any root) of a feature in the DAG."""
    if feature_id not in G:
        raise ValueError(f"Feature '{feature_id}' not in DAG")
    ancestors = nx.ancestors(G, feature_id) | {feature_id}
    subgraph = G.subgraph(ancestors)
    return nx.dag_longest_path_length(subgraph)


def get_dependents(G: nx.DiGraph, feature_id: str) -> list[str]:
    """Get all features that depend (directly or transitively) on a feature."""
    if feature_id not in G:
        return []
    return sorted(nx.descendants(G, feature_id))


def get_dependencies(G: nx.DiGraph, feature_id: str) -> list[str]:
    """Get all features that a feature depends on (directly or transitively)."""
    if feature_id not in G:
        return []
    return sorted(nx.ancestors(G, feature_id))


_DAG_NODE_FIELDS = ("module_ref", "file_scope", "layers")


def dag_to_json(G: nx.DiGraph) -> dict:
    """Serialize DAG to a JSON-friendly dict."""
    nodes = []
    for node in sorted(G.nodes):
        entry = {"id": node}
        attrs = G.nodes[node]
        for key in _DAG_NODE_FIELDS:
            val = attrs.get(key)
            if val:
                entry[key] = val
        deps = sorted(G.predecessors(node))
        if deps:
            entry["dependencies"] = deps
        nodes.append(entry)

    return {
        "nodes": nodes,
        "edges": [{"from": u, "to": v} for u, v in sorted(G.edges)],
    }


def dag_from_json(data: dict) -> nx.DiGraph:
    """Deserialize DAG from a JSON dict."""
    G = nx.DiGraph()
    for node_data in data.get("nodes", []):
        fid = node_data["id"]
        attrs = {k: v for k, v in node_data.items() if k not in ("id", "dependencies", "dependents")}
        G.add_node(fid, **attrs)
    for edge in data.get("edges", []):
        G.add_edge(edge["from"], edge["to"])
    return G


# Backward-compatible aliases for external callers. The canonical loader is
# feature_utils.load_features_from_dir; these thin wrappers exist only so the
# YAML-era call sites keep working (features are .md now — the names are legacy).
def load_features_from_yamls(features_dir: Path) -> list[dict]:
    from aah.core.common.feature_utils import load_features_from_dir as _load
    return _load(features_dir)


load_features_from_md = load_features_from_yamls


def load_module_dag(module_map_path: Path) -> "nx.DiGraph":
    """Build a DAG from module-map.yaml's depends_on declarations.

    Returns a DiGraph where nodes are module IDs and edges represent
    dependency order (dependency → dependent).
    """
    data = read_yaml(module_map_path)
    G = nx.DiGraph()

    modules = data.get("modules", [])
    for mod in modules:
        mod_id = mod.get("id") or mod.get("name")
        if mod_id:
            G.add_node(mod_id, **{k: v for k, v in mod.items() if k != "id"})

    for mod in modules:
        mod_id = mod.get("id") or mod.get("name")
        for dep in mod.get("depends_on", []):
            if dep in G:
                G.add_edge(dep, mod_id)

    return G


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH DAG manager")
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build", help="Build DAG from feature YAMLs")
    build_p.add_argument("--features-dir", type=Path, required=True, help="Path to features/ directory")
    build_p.add_argument("--output", type=Path, required=True, help="Output dag.json path")

    validate_p = sub.add_parser("validate", help="Validate an existing DAG")
    validate_p.add_argument("--dag-path", type=Path, required=True, help="Path to dag.json")

    waves_p = sub.add_parser("waves", help="Compute execution waves")
    waves_p.add_argument("--dag-path", type=Path, required=True, help="Path to dag.json")
    waves_p.add_argument("--output", type=Path, help="Output waves.json path")

    frontier_p = sub.add_parser("frontier", help="Get current execution frontier")
    frontier_p.add_argument("--dag-path", type=Path, required=True, help="Path to dag.json")
    frontier_p.add_argument("--completed", type=str, nargs="*", default=[], help="Completed feature IDs")

    info_p = sub.add_parser("info", help="Get info about a specific feature in the DAG")
    info_p.add_argument("--dag-path", type=Path, required=True)
    info_p.add_argument("feature_id", type=str)

    args = parser.parse_args()

    if args.command == "build":
        features = load_features_from_yamls(args.features_dir)
        if not features:
            print("Error: no feature files (.md) found", file=sys.stderr)
            sys.exit(1)
        try:
            G = build_dag_from_features(features)
        except ValueError as e:
            print(f"Error building DAG: {e}", file=sys.stderr)
            sys.exit(1)
        errors = validate_dag(G)
        if errors:
            json.dump({"valid": False, "errors": errors}, sys.stdout, indent=2)
            print()
            sys.exit(1)
        dag_data = dag_to_json(G)
        write_json(dag_data, args.output)
        json.dump({"valid": True, "nodes": len(G.nodes), "edges": len(G.edges)}, sys.stdout, indent=2)
        print()

    elif args.command == "validate":
        dag_data = read_json(args.dag_path)
        G = dag_from_json(dag_data)
        errors = validate_dag(G)
        result = {"valid": len(errors) == 0, "errors": errors}
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if not errors else 1)

    elif args.command == "waves":
        dag_data = read_json(args.dag_path)
        G = dag_from_json(dag_data)
        from aah.core.common.config import require_project_path
        try:
            project_path = require_project_path()
        except Exception:
            project_path = None
        try:
            waves = compute_execution_waves(G, project_path)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if args.output:
            # Not for a project's .aah/plan/waves.json — unlike
            # core.plan.compute_waves this does not regenerate
            # checkpoint-config.yaml, so the UCR cadence would go stale.
            write_json({"waves": waves}, args.output)
        json.dump({"waves": waves, "total_waves": len(waves)}, sys.stdout, indent=2)
        print()

    elif args.command == "frontier":
        dag_data = read_json(args.dag_path)
        G = dag_from_json(dag_data)
        completed = set(args.completed)
        frontier = get_execution_frontier(G, completed)
        json.dump({"frontier": frontier}, sys.stdout, indent=2)
        print()

    elif args.command == "info":
        dag_data = read_json(args.dag_path)
        G = dag_from_json(dag_data)
        fid = args.feature_id
        if fid not in G:
            print(f"Error: feature '{fid}' not in DAG", file=sys.stderr)
            sys.exit(1)
        info = {
            "id": fid,
            "depth": get_feature_depth(G, fid),
            "direct_dependencies": sorted(G.predecessors(fid)),
            "all_dependencies": get_dependencies(G, fid),
            "direct_dependents": sorted(G.successors(fid)),
            "all_dependents": get_dependents(G, fid),
        }
        json.dump(info, sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()
