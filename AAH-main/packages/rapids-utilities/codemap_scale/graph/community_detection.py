"""
Community detection and semantic edge analysis.

Inspired by Graphify's approach:
- Leiden community detection for module clustering
- God node detection (highest-degree hubs)
- Relationship transparency (EXTRACTED / INFERRED / AMBIGUOUS)
- Cross-file semantic similarity edges

Uses graspologic for the Leiden algorithm when available,
falls back to a simpler connected-components approach.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

# Try to import community detection libraries
try:
    import networkx as nx

    _HAS_NETWORKX = True
except ImportError:
    nx = None  # type: ignore[assignment]
    _HAS_NETWORKX = False

try:
    from graspologic.partition import leiden

    _HAS_GRASPOLOGIC = True
except ImportError:
    leiden = None  # type: ignore[assignment]
    _HAS_GRASPOLOGIC = False


class CommunityDetector:
    """
    Detects communities in the code graph using the Leiden algorithm.

    Communities represent clusters of closely related symbols/files that
    form logical modules. This helps agents understand the high-level
    structure of a codebase.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def detect_communities(
        self, resolution: float = 1.0, min_community_size: int = 2
    ) -> list[dict[str, Any]]:
        """
        Run community detection on the symbol graph.

        Uses Leiden algorithm (via graspologic) when available,
        falls back to connected components via NetworkX,
        or simple SQL-based file grouping as last resort.

        Args:
            resolution: Leiden resolution parameter (higher = more communities)
            min_community_size: Minimum symbols in a community to be reported

        Returns:
            List of communities, each with members, stats, and a label.
        """
        if _HAS_NETWORKX:
            G = self._build_networkx_graph()
            if G.number_of_nodes() == 0:
                return []

            if _HAS_GRASPOLOGIC and G.number_of_edges() > 0:
                communities = self._leiden_partition(G, resolution)
            else:
                communities = self._connected_components(G)

            return self._format_communities(communities, G, min_community_size)
        else:
            return self._sql_file_communities(min_community_size)

    def _build_networkx_graph(self) -> Any:
        """Build a NetworkX graph from the relations table."""
        G = nx.Graph()

        # Add nodes from symbols
        rows = self._conn.execute(
            "SELECT fqn, name, kind, file_path FROM symbols"
        ).fetchall()
        for row in rows:
            G.add_node(
                row["fqn"],
                name=row["name"],
                kind=row["kind"],
                file_path=row["file_path"],
            )

        # Add edges from relations
        rows = self._conn.execute(
            "SELECT source_fqn, target_fqn, kind FROM relations"
        ).fetchall()
        for row in rows:
            if G.has_node(row["source_fqn"]) and G.has_node(row["target_fqn"]):
                G.add_edge(
                    row["source_fqn"],
                    row["target_fqn"],
                    kind=row["kind"],
                )

        return G

    def _leiden_partition(
        self, G: Any, resolution: float
    ) -> dict[int, list[str]]:
        """Run Leiden community detection."""
        partition = leiden(G, resolution=resolution)

        communities: dict[int, list[str]] = {}
        for node, comm_id in partition.items():
            communities.setdefault(comm_id, []).append(node)

        return communities

    def _connected_components(self, G: Any) -> dict[int, list[str]]:
        """Fallback: use connected components as communities."""
        communities: dict[int, list[str]] = {}
        for i, component in enumerate(nx.connected_components(G)):
            communities[i] = list(component)
        return communities

    def _sql_file_communities(
        self, min_size: int
    ) -> list[dict[str, Any]]:
        """
        Last-resort fallback: group symbols by file directory.
        No external dependencies needed.
        """
        rows = self._conn.execute(
            """SELECT file_path, COUNT(*) as sym_count
               FROM symbols
               GROUP BY file_path
               HAVING sym_count >= ?
               ORDER BY sym_count DESC""",
            (min_size,),
        ).fetchall()

        # Group by directory
        dir_groups: dict[str, list[str]] = {}
        for row in rows:
            parts = row["file_path"].rsplit("/", 1)
            dir_name = parts[0] if len(parts) > 1 else "."
            dir_groups.setdefault(dir_name, []).append(row["file_path"])

        communities = []
        for i, (dir_name, files) in enumerate(dir_groups.items()):
            members = []
            for fp in files:
                syms = self._conn.execute(
                    "SELECT fqn, name, kind FROM symbols WHERE file_path = ?", (fp,)
                ).fetchall()
                members.extend(
                    {"fqn": s["fqn"], "name": s["name"], "kind": s["kind"]}
                    for s in syms
                )

            if len(members) >= min_size:
                communities.append({
                    "id": i,
                    "label": dir_name,
                    "member_count": len(members),
                    "members": members[:50],  # cap for readability
                    "files": files,
                    "detection_method": "directory_grouping",
                })

        return communities

    def _format_communities(
        self,
        communities: dict[int, list[str]],
        G: Any,
        min_size: int,
    ) -> list[dict[str, Any]]:
        """Format raw community assignments into structured output."""
        result = []

        for comm_id, members in communities.items():
            if len(members) < min_size:
                continue

            # Get details for each member
            member_details = []
            files_in_community: set[str] = set()
            for fqn in members:
                node_data = G.nodes.get(fqn, {})
                member_details.append({
                    "fqn": fqn,
                    "name": node_data.get("name", fqn.rsplit(".", 1)[-1]),
                    "kind": node_data.get("kind", "unknown"),
                })
                fp = node_data.get("file_path")
                if fp:
                    files_in_community.add(fp)

            # Compute internal edge density
            subgraph = G.subgraph(members)
            internal_edges = subgraph.number_of_edges()
            max_edges = len(members) * (len(members) - 1) / 2
            density = internal_edges / max_edges if max_edges > 0 else 0

            # Auto-label from most common file directory
            dirs: dict[str, int] = {}
            for fp in files_in_community:
                parts = fp.rsplit("/", 1)
                d = parts[0] if len(parts) > 1 else "."
                dirs[d] = dirs.get(d, 0) + 1
            label = max(dirs, key=dirs.get) if dirs else f"community_{comm_id}"

            method = "leiden" if _HAS_GRASPOLOGIC else "connected_components"

            result.append({
                "id": comm_id,
                "label": label,
                "member_count": len(members),
                "members": member_details[:50],
                "files": sorted(files_in_community),
                "internal_edges": internal_edges,
                "density": round(density, 3),
                "detection_method": method,
            })

        # Sort by size descending
        result.sort(key=lambda c: c["member_count"], reverse=True)
        return result


def detect_god_nodes(
    conn: sqlite3.Connection, top_n: int = 20
) -> list[dict[str, Any]]:
    """
    Find 'god nodes' — symbols with the highest degree (most connections).

    These are critical hubs that many other symbols depend on.
    Changes to god nodes have the widest blast radius.
    """
    rows = conn.execute(
        """SELECT fqn, name, kind, file_path,
                  (SELECT COUNT(*) FROM relations WHERE source_fqn = s.fqn) as out_degree,
                  (SELECT COUNT(*) FROM relations WHERE target_fqn = s.fqn) as in_degree
           FROM symbols s
           ORDER BY (
               (SELECT COUNT(*) FROM relations WHERE source_fqn = s.fqn) +
               (SELECT COUNT(*) FROM relations WHERE target_fqn = s.fqn)
           ) DESC
           LIMIT ?""",
        (top_n,),
    ).fetchall()

    return [
        {
            "fqn": row["fqn"],
            "name": row["name"],
            "kind": row["kind"],
            "file_path": row["file_path"],
            "in_degree": row["in_degree"],
            "out_degree": row["out_degree"],
            "total_degree": row["in_degree"] + row["out_degree"],
        }
        for row in rows
        if row["in_degree"] + row["out_degree"] > 0
    ]


def get_community_for_symbol(
    conn: sqlite3.Connection, fqn: str
) -> dict[str, Any] | None:
    """Look up which community a symbol belongs to."""
    row = conn.execute(
        "SELECT * FROM communities WHERE fqn = ?", (fqn,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)
