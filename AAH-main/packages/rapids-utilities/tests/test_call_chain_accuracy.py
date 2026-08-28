"""Tests for call chain accuracy using synthetic repos with known call graphs."""

from __future__ import annotations

from pathlib import Path

from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph


class TestCallChainAccuracy:
    """Verify call chain traversal with known synthetic data."""

    def _make_graph(self, tmp_path: Path) -> SQLiteSymbolGraph:
        """Create a graph with a known call pattern: A→B→C, D→B."""
        graph = SQLiteSymbolGraph(tmp_path / "chain_test.db")

        graph.upsert_files_batch([
            {"path": "mod_a.py", "language": "python", "content_hash": "a", "size_bytes": 100, "line_count": 10, "tier": 2},
            {"path": "mod_b.py", "language": "python", "content_hash": "b", "size_bytes": 100, "line_count": 10, "tier": 2},
            {"path": "mod_c.py", "language": "python", "content_hash": "c", "size_bytes": 100, "line_count": 10, "tier": 2},
            {"path": "mod_d.py", "language": "python", "content_hash": "d", "size_bytes": 100, "line_count": 10, "tier": 2},
        ])

        graph.upsert_symbols_batch([
            {"fqn": "mod_a.func_a", "name": "func_a", "kind": "function", "language": "python", "file_path": "mod_a.py", "start_line": 1, "end_line": 5, "tier": 2},
            {"fqn": "mod_b.func_b", "name": "func_b", "kind": "function", "language": "python", "file_path": "mod_b.py", "start_line": 1, "end_line": 5, "tier": 2},
            {"fqn": "mod_c.func_c", "name": "func_c", "kind": "function", "language": "python", "file_path": "mod_c.py", "start_line": 1, "end_line": 5, "tier": 2},
            {"fqn": "mod_d.func_d", "name": "func_d", "kind": "function", "language": "python", "file_path": "mod_d.py", "start_line": 1, "end_line": 5, "tier": 2},
        ])

        graph.upsert_relations_batch([
            {"source_fqn": "mod_a.func_a", "target_fqn": "mod_b.func_b", "kind": "calls", "file_path": "mod_a.py", "line": 3},
            {"source_fqn": "mod_b.func_b", "target_fqn": "mod_c.func_c", "kind": "calls", "file_path": "mod_b.py", "line": 3},
            {"source_fqn": "mod_d.func_d", "target_fqn": "mod_b.func_b", "kind": "calls", "file_path": "mod_d.py", "line": 2},
        ])

        return graph

    def test_forward_chain_a_to_c(self, tmp_path):
        graph = self._make_graph(tmp_path)
        chain = graph.get_call_chain("mod_a.func_a", depth=3)

        assert chain["fqn"] == "mod_a.func_a"
        assert "calls" in chain
        called = {c["fqn"] for c in chain["calls"]}
        assert "mod_b.func_b" in called

        # B should have calls to C
        b_entry = next(c for c in chain["calls"] if c["fqn"] == "mod_b.func_b")
        assert "calls" in b_entry
        b_called = {c["fqn"] for c in b_entry["calls"]}
        assert "mod_c.func_c" in b_called

        graph.close()

    def test_reverse_chain_b_callers(self, tmp_path):
        graph = self._make_graph(tmp_path)
        impact = graph.get_reverse_call_chain("mod_b.func_b", depth=2)

        assert impact["fqn"] == "mod_b.func_b"
        assert "called_by" in impact
        callers = {c["fqn"] for c in impact["called_by"]}
        assert "mod_a.func_a" in callers
        assert "mod_d.func_d" in callers

        graph.close()

    def test_chain_depth_limit(self, tmp_path):
        graph = self._make_graph(tmp_path)
        # Depth 1 from A should show B but not C
        chain = graph.get_call_chain("mod_a.func_a", depth=1)
        assert "calls" in chain
        called = {c["fqn"] for c in chain["calls"]}
        assert "mod_b.func_b" in called

        # B shouldn't have further calls at depth 1
        b_entry = next(c for c in chain["calls"] if c["fqn"] == "mod_b.func_b")
        assert "calls" not in b_entry

        graph.close()

    def test_leaf_node_has_no_calls(self, tmp_path):
        graph = self._make_graph(tmp_path)
        chain = graph.get_call_chain("mod_c.func_c", depth=3)
        assert chain["fqn"] == "mod_c.func_c"
        assert "calls" not in chain  # C doesn't call anyone

        graph.close()

    def test_cycle_detection(self, tmp_path):
        """Verify cycles don't cause infinite recursion."""
        graph = SQLiteSymbolGraph(tmp_path / "cycle_test.db")

        graph.upsert_files_batch([
            {"path": "x.py", "language": "python", "content_hash": "x", "size_bytes": 100, "line_count": 10, "tier": 2},
            {"path": "y.py", "language": "python", "content_hash": "y", "size_bytes": 100, "line_count": 10, "tier": 2},
        ])

        graph.upsert_symbols_batch([
            {"fqn": "x.func_x", "name": "func_x", "kind": "function", "language": "python", "file_path": "x.py", "start_line": 1, "end_line": 5, "tier": 2},
            {"fqn": "y.func_y", "name": "func_y", "kind": "function", "language": "python", "file_path": "y.py", "start_line": 1, "end_line": 5, "tier": 2},
        ])

        # Create cycle: X → Y → X
        graph.upsert_relations_batch([
            {"source_fqn": "x.func_x", "target_fqn": "y.func_y", "kind": "calls", "file_path": "x.py", "line": 2},
            {"source_fqn": "y.func_y", "target_fqn": "x.func_x", "kind": "calls", "file_path": "y.py", "line": 2},
        ])

        # Should not hang or crash
        chain = graph.get_call_chain("x.func_x", depth=10)
        assert chain["fqn"] == "x.func_x"
        assert "calls" in chain
        # Y should be in calls
        called = {c["fqn"] for c in chain["calls"]}
        assert "y.func_y" in called

        graph.close()

    def test_diamond_dependency(self, tmp_path):
        """A→B, A→C, B→D, C→D (diamond pattern)."""
        graph = SQLiteSymbolGraph(tmp_path / "diamond_test.db")

        graph.upsert_files_batch([
            {"path": f"{n}.py", "language": "python", "content_hash": n, "size_bytes": 100, "line_count": 10, "tier": 2}
            for n in ("a", "b", "c", "d")
        ])

        graph.upsert_symbols_batch([
            {"fqn": f"{n}.func", "name": "func", "kind": "function", "language": "python", "file_path": f"{n}.py", "start_line": 1, "end_line": 5, "tier": 2}
            for n in ("a", "b", "c", "d")
        ])

        graph.upsert_relations_batch([
            {"source_fqn": "a.func", "target_fqn": "b.func", "kind": "calls", "file_path": "a.py", "line": 2},
            {"source_fqn": "a.func", "target_fqn": "c.func", "kind": "calls", "file_path": "a.py", "line": 3},
            {"source_fqn": "b.func", "target_fqn": "d.func", "kind": "calls", "file_path": "b.py", "line": 2},
            {"source_fqn": "c.func", "target_fqn": "d.func", "kind": "calls", "file_path": "c.py", "line": 2},
        ])

        chain = graph.get_call_chain("a.func", depth=3)
        # A calls B and C
        called = {c["fqn"] for c in chain.get("calls", [])}
        assert "b.func" in called
        assert "c.func" in called

        # Both B and C should have D in their calls, but D appears only once due to visited set
        all_deep_fqns = set()
        for c in chain.get("calls", []):
            for sub in c.get("calls", []):
                all_deep_fqns.add(sub["fqn"])
        assert "d.func" in all_deep_fqns

        graph.close()
