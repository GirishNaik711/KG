"""Generate CODEBASE_GRAPH_REPORT.md from the knowledge graph."""

from datetime import datetime, timezone

import networkx as nx

from aah.core.intel.knowledge_graph.graph_builder import (
    detect_communities,
    find_god_nodes,
    find_hub_nodes,
)


def generate_report(G: nx.DiGraph, project_name: str = "") -> str:
    """Generate a human-readable markdown report from the graph."""
    lines = []

    def a(line: str = "") -> None:
        lines.append(line)

    a("# Codebase Knowledge Graph Report")
    if project_name:
        a(f"**Project:** {project_name}")
    a(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    a(f"**Nodes:** {G.number_of_nodes()} | **Edges:** {G.number_of_edges()}")
    a("")

    # Communities
    communities = detect_communities(G)
    a("## Module Communities")
    a("")
    if communities:
        a(f"Found {len(communities)} community(ies) with 2+ connected modules.")
        a("")
        for i, community in enumerate(sorted(communities, key=len, reverse=True)[:10]):
            members = sorted(community)[:10]
            a(f"### Community {i + 1} ({len(community)} modules)")
            for m in members:
                a(f"- `{m}`")
            if len(community) > 10:
                a(f"- ... and {len(community) - 10} more")
            a("")
    else:
        a("No multi-module communities detected.")
        a("")

    # Hub nodes
    hubs = find_hub_nodes(G)
    a("## Hub Modules (High Betweenness Centrality)")
    a("")
    if hubs:
        a("These modules are critical connectors — changes ripple widely.")
        a("")
        a("| Module | Centrality |")
        a("|--------|-----------|")
        for node, centrality in hubs:
            a(f"| `{node}` | {centrality} |")
        a("")
    else:
        a("No significant hub modules detected.")
        a("")

    # God nodes
    gods = find_god_nodes(G)
    a("## God Nodes (High Degree — Too Many Responsibilities)")
    a("")
    if gods:
        a("These modules have many connections — consider decomposition.")
        a("")
        a("| Module | Connections |")
        a("|--------|-----------|")
        for node, degree in sorted(gods, key=lambda x: x[1], reverse=True):
            a(f"| `{node}` | {degree} |")
        a("")
    else:
        a("No god nodes detected (all modules have reasonable responsibility scope).")
        a("")

    # Edge type breakdown
    edge_types = {}
    for _, _, data in G.edges(data=True):
        etype = data.get("type", "unknown")
        edge_types[etype] = edge_types.get(etype, 0) + 1

    a("## Relationship Types")
    a("")
    a("| Type | Count |")
    a("|------|-------|")
    for etype, count in sorted(edge_types.items(), key=lambda x: x[1], reverse=True):
        a(f"| {etype} | {count} |")
    a("")

    return "\n".join(lines)
