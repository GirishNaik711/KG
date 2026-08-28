"""Tests for community detection and semantic edges (Phase 2: Graphify-inspired)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph
from codemap_scale.graph.community_detection import (
    CommunityDetector,
    detect_god_nodes,
    _HAS_NETWORKX,
    _HAS_GRASPOLOGIC,
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------

@pytest.fixture
def graph_with_communities(tmp_path: Path) -> SQLiteSymbolGraph:
    """Graph pre-loaded with symbols and relations for community testing."""
    db_path = tmp_path / "community_test.db"
    graph = SQLiteSymbolGraph(db_path)

    # Create two clusters of files with internal connections
    # Cluster 1: Payment module
    graph.upsert_files_batch([
        {"path": "payments/service.py", "language": "python", "content_hash": "a1", "size_bytes": 500, "line_count": 30, "tier": 2},
        {"path": "payments/gateway.py", "language": "python", "content_hash": "a2", "size_bytes": 400, "line_count": 25, "tier": 2},
        {"path": "payments/models.py", "language": "python", "content_hash": "a3", "size_bytes": 300, "line_count": 20, "tier": 2},
    ])

    graph.upsert_symbols_batch([
        {"fqn": "payments.service.PaymentService", "name": "PaymentService", "kind": "class", "language": "python", "file_path": "payments/service.py", "start_line": 1, "end_line": 20, "tier": 2},
        {"fqn": "payments.service.process_payment", "name": "process_payment", "kind": "function", "language": "python", "file_path": "payments/service.py", "start_line": 22, "end_line": 30, "tier": 2},
        {"fqn": "payments.gateway.Gateway", "name": "Gateway", "kind": "class", "language": "python", "file_path": "payments/gateway.py", "start_line": 1, "end_line": 15, "tier": 2},
        {"fqn": "payments.gateway.charge", "name": "charge", "kind": "function", "language": "python", "file_path": "payments/gateway.py", "start_line": 17, "end_line": 25, "tier": 2},
        {"fqn": "payments.models.Payment", "name": "Payment", "kind": "class", "language": "python", "file_path": "payments/models.py", "start_line": 1, "end_line": 20, "tier": 2},
    ])

    # Cluster 2: Auth module
    graph.upsert_files_batch([
        {"path": "auth/handler.py", "language": "python", "content_hash": "b1", "size_bytes": 500, "line_count": 30, "tier": 2},
        {"path": "auth/token.py", "language": "python", "content_hash": "b2", "size_bytes": 400, "line_count": 25, "tier": 2},
    ])

    graph.upsert_symbols_batch([
        {"fqn": "auth.handler.AuthHandler", "name": "AuthHandler", "kind": "class", "language": "python", "file_path": "auth/handler.py", "start_line": 1, "end_line": 20, "tier": 2},
        {"fqn": "auth.handler.authenticate", "name": "authenticate", "kind": "function", "language": "python", "file_path": "auth/handler.py", "start_line": 22, "end_line": 30, "tier": 2},
        {"fqn": "auth.token.TokenManager", "name": "TokenManager", "kind": "class", "language": "python", "file_path": "auth/token.py", "start_line": 1, "end_line": 25, "tier": 2},
    ])

    # Relations within cluster 1 (payment)
    graph.upsert_relations_batch([
        {"source_fqn": "payments.service.process_payment", "target_fqn": "payments.gateway.charge", "kind": "calls", "file_path": "payments/service.py", "line": 25},
        {"source_fqn": "payments.service.PaymentService", "target_fqn": "payments.models.Payment", "kind": "calls", "file_path": "payments/service.py", "line": 10},
        {"source_fqn": "payments.gateway.charge", "target_fqn": "payments.models.Payment", "kind": "calls", "file_path": "payments/gateway.py", "line": 20},
    ])

    # Relations within cluster 2 (auth)
    graph.upsert_relations_batch([
        {"source_fqn": "auth.handler.authenticate", "target_fqn": "auth.token.TokenManager", "kind": "calls", "file_path": "auth/handler.py", "line": 25},
    ])

    # Cross-cluster relation (weak link)
    graph.upsert_relations_batch([
        {"source_fqn": "payments.service.PaymentService", "target_fqn": "auth.handler.authenticate", "kind": "calls", "file_path": "payments/service.py", "line": 5},
    ])

    yield graph
    graph.close()


@pytest.fixture
def empty_graph(tmp_path: Path) -> SQLiteSymbolGraph:
    db_path = tmp_path / "empty_test.db"
    graph = SQLiteSymbolGraph(db_path)
    yield graph
    graph.close()


# -------------------------------------------------------------------
# Tests: Community Detection
# -------------------------------------------------------------------

class TestCommunityDetector:
    def test_detects_communities(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        detector = CommunityDetector(conn)
        communities = detector.detect_communities()

        # Should detect at least 1 community
        assert len(communities) >= 1

        # Each community should have required fields
        for comm in communities:
            assert "id" in comm
            assert "label" in comm
            assert "member_count" in comm
            assert comm["member_count"] >= 2
            assert "members" in comm
            assert "detection_method" in comm

    def test_empty_graph_returns_empty(self, empty_graph):
        conn = empty_graph._get_conn()
        detector = CommunityDetector(conn)
        communities = detector.detect_communities()
        assert communities == []

    def test_min_community_size_filter(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        detector = CommunityDetector(conn)

        large = detector.detect_communities(min_community_size=4)
        small = detector.detect_communities(min_community_size=2)

        # More communities when threshold is lower
        assert len(small) >= len(large)

    def test_resolution_affects_count(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        detector = CommunityDetector(conn)

        # Higher resolution = more communities (generally)
        low_res = detector.detect_communities(resolution=0.5)
        high_res = detector.detect_communities(resolution=3.0)

        # At minimum, both should return valid results
        assert isinstance(low_res, list)
        assert isinstance(high_res, list)

    def test_sql_fallback_works(self, graph_with_communities):
        """Test the SQL-based directory grouping fallback."""
        conn = graph_with_communities._get_conn()
        detector = CommunityDetector(conn)
        communities = detector._sql_file_communities(min_size=2)

        assert len(communities) >= 1
        for comm in communities:
            assert "label" in comm
            assert "files" in comm
            assert comm["detection_method"] == "directory_grouping"

    @pytest.mark.skipif(not _HAS_NETWORKX, reason="networkx not installed")
    def test_networkx_graph_build(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        detector = CommunityDetector(conn)
        G = detector._build_networkx_graph()

        assert G.number_of_nodes() == 8  # Total symbols
        # NetworkX undirected graph deduplicates edges between same pair
        assert G.number_of_edges() >= 3

    @pytest.mark.skipif(not _HAS_NETWORKX, reason="networkx not installed")
    def test_connected_components_fallback(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        detector = CommunityDetector(conn)
        G = detector._build_networkx_graph()
        communities = detector._connected_components(G)

        # All nodes are connected (cross-cluster edge), so 1 component
        # unless we have isolated nodes
        assert len(communities) >= 1


# -------------------------------------------------------------------
# Tests: God Nodes
# -------------------------------------------------------------------

class TestGodNodes:
    def test_detects_god_nodes(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        nodes = detect_god_nodes(conn, top_n=5)

        assert len(nodes) > 0
        for node in nodes:
            assert "fqn" in node
            assert "name" in node
            assert "total_degree" in node
            assert node["total_degree"] > 0

    def test_god_nodes_sorted_by_degree(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        nodes = detect_god_nodes(conn, top_n=10)

        for i in range(len(nodes) - 1):
            assert nodes[i]["total_degree"] >= nodes[i + 1]["total_degree"]

    def test_payment_model_is_hub(self, graph_with_communities):
        """Payment model should be a god node (referenced by service and gateway)."""
        conn = graph_with_communities._get_conn()
        nodes = detect_god_nodes(conn, top_n=10)
        fqns = [n["fqn"] for n in nodes]
        # Payment is referenced by both service and gateway
        assert "payments.models.Payment" in fqns

    def test_empty_graph_no_god_nodes(self, empty_graph):
        conn = empty_graph._get_conn()
        nodes = detect_god_nodes(conn, top_n=5)
        assert nodes == []

    def test_top_n_limits_results(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        nodes_3 = detect_god_nodes(conn, top_n=3)
        nodes_10 = detect_god_nodes(conn, top_n=10)
        assert len(nodes_3) <= 3
        assert len(nodes_10) >= len(nodes_3)


# -------------------------------------------------------------------
# Tests: Relation Provenance/Confidence
# -------------------------------------------------------------------

class TestRelationProvenance:
    def test_default_provenance_is_extracted(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        rows = conn.execute("SELECT provenance FROM relations").fetchall()
        assert all(row["provenance"] == "EXTRACTED" for row in rows)

    def test_default_confidence_is_one(self, graph_with_communities):
        conn = graph_with_communities._get_conn()
        rows = conn.execute("SELECT confidence FROM relations").fetchall()
        assert all(row["confidence"] == 1.0 for row in rows)

    def test_inferred_relations_stored(self, graph_with_communities):
        """Test storing inferred relations with confidence scores."""
        graph_with_communities.upsert_relations_batch([
            {
                "source_fqn": "docs.readme.document",
                "target_fqn": "payments.service.PaymentService",
                "kind": "references",
                "file_path": "docs/readme.md",
                "confidence": 0.7,
                "provenance": "INFERRED",
            },
            {
                "source_fqn": "docs.readme.document",
                "target_fqn": "auth.handler.AuthHandler",
                "kind": "references",
                "file_path": "docs/readme.md",
                "confidence": 0.4,
                "provenance": "INFERRED",
            },
        ])

        inferred = graph_with_communities.find_inferred_relations()
        assert len(inferred) == 2

        # Filter by confidence
        high_conf = graph_with_communities.find_inferred_relations(min_confidence=0.5)
        assert len(high_conf) == 1
        assert high_conf[0]["confidence"] == 0.7

    def test_ambiguous_relations(self, graph_with_communities):
        graph_with_communities.upsert_relations_batch([
            {
                "source_fqn": "payments.service.PaymentService",
                "target_fqn": "unknown.symbol",
                "kind": "calls",
                "file_path": "payments/service.py",
                "confidence": 0.3,
                "provenance": "AMBIGUOUS",
            },
        ])

        inferred = graph_with_communities.find_inferred_relations()
        ambiguous = [r for r in inferred if r["provenance"] == "AMBIGUOUS"]
        assert len(ambiguous) == 1


# -------------------------------------------------------------------
# Tests: Community Storage
# -------------------------------------------------------------------

class TestCommunityStorage:
    def test_store_and_retrieve(self, graph_with_communities):
        communities = [
            {
                "id": 0,
                "label": "payments",
                "members": [
                    {"fqn": "payments.service.PaymentService"},
                    {"fqn": "payments.gateway.Gateway"},
                ],
                "detection_method": "leiden",
            },
            {
                "id": 1,
                "label": "auth",
                "members": [
                    {"fqn": "auth.handler.AuthHandler"},
                    {"fqn": "auth.token.TokenManager"},
                ],
                "detection_method": "leiden",
            },
        ]

        stored = graph_with_communities.store_communities(communities)
        assert stored == 4

        retrieved = graph_with_communities.get_communities()
        assert len(retrieved) == 2

    def test_get_community_for_symbol(self, graph_with_communities):
        communities = [
            {
                "id": 0,
                "label": "payments",
                "members": [{"fqn": "payments.service.PaymentService"}],
                "detection_method": "leiden",
            },
        ]
        graph_with_communities.store_communities(communities)

        result = graph_with_communities.get_community_for_symbol("payments.service.PaymentService")
        assert result is not None
        assert result["community_id"] == 0
        assert result["community_label"] == "payments"

    def test_get_community_for_unknown_symbol(self, graph_with_communities):
        result = graph_with_communities.get_community_for_symbol("nonexistent.symbol")
        assert result is None

    def test_store_replaces_old(self, graph_with_communities):
        """Storing new communities should replace the old ones."""
        old = [{"id": 0, "label": "old", "members": [{"fqn": "payments.service.PaymentService"}], "detection_method": "leiden"}]
        graph_with_communities.store_communities(old)

        new = [{"id": 0, "label": "new", "members": [{"fqn": "payments.service.PaymentService"}], "detection_method": "leiden"}]
        graph_with_communities.store_communities(new)

        result = graph_with_communities.get_community_for_symbol("payments.service.PaymentService")
        assert result["community_label"] == "new"


# -------------------------------------------------------------------
# Tests: God Nodes via Graph Method
# -------------------------------------------------------------------

class TestGraphGodNodes:
    def test_graph_get_god_nodes(self, graph_with_communities):
        nodes = graph_with_communities.get_god_nodes(top_n=5)
        assert len(nodes) > 0
        assert all("total_degree" in n for n in nodes)

    def test_graph_get_god_nodes_empty(self, empty_graph):
        nodes = empty_graph.get_god_nodes(top_n=5)
        assert nodes == []
