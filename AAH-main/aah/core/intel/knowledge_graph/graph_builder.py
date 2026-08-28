"""
Build a NetworkX graph from profiler data + AST relationships.

Consumes codebase-profile.json (from the existing profiler) and enriches
it with typed relationships extracted by ast_extractor.
"""

import json
from pathlib import Path

import networkx as nx

from aah.core.intel.knowledge_graph.ast_extractor import extract_relationships_from_file


def build_graph(
    profile_path: Path,
    codebase_root: Path,
    max_files: int = 100,
) -> nx.DiGraph:
    """
    Build a directed graph from profiler data + AST extraction.

    Args:
        profile_path: Path to codebase-profile.json
        codebase_root: Root of the codebase being analyzed
        max_files: Max key files to AST-parse (default 100)

    Returns:
        NetworkX DiGraph with nodes (files/modules) and typed edges.
    """
    profile = json.loads(profile_path.read_text(encoding='utf-8'))
    G = nx.DiGraph()

    # Add nodes from profiler inventory
    for file_info in profile.get("files", []):
        path = file_info.get("path", "")
        G.add_node(path, **{
            "language": file_info.get("language", "unknown"),
            "role": file_info.get("role", "other"),
            "size": file_info.get("size", 0),
            "symbols": file_info.get("symbol_count", 0),
        })

    # Add edges from profiler module_graph (T1 import map)
    for edge in profile.get("module_graph", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src and tgt:
            G.add_edge(src, tgt, type="imports", source="profiler")

    # Identify key files for AST extraction
    key_files = _select_key_files(profile, max_files)

    # AST-extract relationships from key files
    for file_path_str in key_files:
        full_path = codebase_root / file_path_str
        rels = extract_relationships_from_file(full_path)
        for rel in rels:
            G.add_edge(
                rel["source"],
                rel["target"],
                type=rel["type"],
                line=rel.get("line"),
                source="ast",
            )

    return G


def _select_key_files(profile: dict, max_files: int) -> list[str]:
    """Select key files for deep AST extraction based on profiler roles."""
    key_files = []

    # Priority order: entry points, APIs, models, hotspots, coupling hubs
    for category in ["entry_points", "api_layer_files", "data_model_files", "config_files"]:
        for f in profile.get(category, []):
            path = f if isinstance(f, str) else f.get("path", "")
            if path and path not in key_files:
                key_files.append(path)

    # Add hotspots
    for h in profile.get("architecture", {}).get("hotspots", []):
        path = h if isinstance(h, str) else h.get("path", "")
        if path and path not in key_files:
            key_files.append(path)

    # Add coupling hubs
    for h in profile.get("architecture", {}).get("coupling", {}).get("afferent_top", []):
        path = h if isinstance(h, str) else h.get("path", "")
        if path and path not in key_files:
            key_files.append(path)

    return key_files[:max_files]


def detect_communities(G: nx.DiGraph) -> list[set]:
    """
    Detect module communities using connected components on undirected view.

    Returns list of sets, each containing node names in a community.
    """
    undirected = G.to_undirected()
    return [c for c in nx.connected_components(undirected) if len(c) > 1]


def find_hub_nodes(G: nx.DiGraph, top_n: int = 10) -> list[tuple[str, float]]:
    """
    Find hub nodes by betweenness centrality.

    Returns list of (node, centrality) tuples, sorted descending.
    """
    if len(G) < 3:
        return []
    try:
        centrality = nx.betweenness_centrality(G)
        sorted_nodes = sorted(centrality.items(), key=lambda x: x[1], reverse=True)
        return [(n, round(c, 4)) for n, c in sorted_nodes[:top_n] if c > 0]
    except Exception:
        return []


def find_god_nodes(G: nx.DiGraph, threshold: int = 15) -> list[tuple[str, int]]:
    """
    Find "god nodes" — modules with too many responsibilities (high degree).

    Returns list of (node, degree) tuples.
    """
    return [
        (n, G.degree(n)) for n in G.nodes()
        if G.degree(n) >= threshold
    ]


def export_graph_json(G: nx.DiGraph) -> dict:
    """Export graph as JSON-serializable dict."""
    nodes = []
    for n, data in G.nodes(data=True):
        nodes.append({"id": n, **data})

    edges = []
    for u, v, data in G.edges(data=True):
        edges.append({"source": u, "target": v, **data})

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }
