"""Tests for the scaled CodeMap components."""

from __future__ import annotations

from pathlib import Path


from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph


class TestSQLiteGraph:
    """Test SQLite-backed symbol graph."""

    def setup_method(self, tmp_path=None):
        import tempfile
        self._tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self._tmpdir) / "test.db"
        self.graph = SQLiteSymbolGraph(self.db_path)

    def teardown_method(self):
        self.graph.close()

    def test_upsert_files(self):
        files = [
            {"path": "src/service.py", "language": "python", "content_hash": "abc123",
             "size_bytes": 500, "line_count": 30, "tier": 0},
            {"path": "src/utils.py", "language": "python", "content_hash": "def456",
             "size_bytes": 200, "line_count": 15, "tier": 0},
        ]
        count = self.graph.upsert_files_batch(files)
        assert count == 2

        stats = self.graph.get_stats()
        assert stats["files"] == 2

    def test_upsert_symbols(self):
        # First upsert files
        self.graph.upsert_files_batch([
            {"path": "src/service.py", "language": "python", "content_hash": "abc123",
             "size_bytes": 500, "line_count": 30, "tier": 1},
        ])

        symbols = [
            {"fqn": "src.service.PaymentService", "name": "PaymentService",
             "kind": "class", "language": "python", "file_path": "src/service.py",
             "start_line": 10, "end_line": 50, "tier": 1},
            {"fqn": "src.service.PaymentService.process", "name": "process",
             "kind": "method", "language": "python", "file_path": "src/service.py",
             "start_line": 15, "end_line": 30, "tier": 1},
        ]
        count = self.graph.upsert_symbols_batch(symbols)
        assert count == 2

        result = self.graph.get_symbol("src.service.PaymentService")
        assert result is not None
        assert result["name"] == "PaymentService"
        assert result["kind"] == "class"

    def test_find_symbols(self):
        self.graph.upsert_files_batch([
            {"path": "src/service.py", "language": "python", "content_hash": "abc",
             "size_bytes": 500, "line_count": 30, "tier": 1},
        ])
        self.graph.upsert_symbols_batch([
            {"fqn": "src.service.PaymentService", "name": "PaymentService",
             "kind": "class", "language": "python", "file_path": "src/service.py",
             "start_line": 1, "end_line": 50, "tier": 1},
            {"fqn": "src.service.RefundService", "name": "RefundService",
             "kind": "class", "language": "python", "file_path": "src/service.py",
             "start_line": 55, "end_line": 80, "tier": 1},
            {"fqn": "src.service.helper", "name": "helper",
             "kind": "function", "language": "python", "file_path": "src/service.py",
             "start_line": 85, "end_line": 90, "tier": 1},
        ])

        results = self.graph.find_symbols("%Service%")
        assert len(results) == 2

        results = self.graph.find_symbols("%", kind="function")
        assert len(results) == 1

    def test_call_chain(self):
        self.graph.upsert_files_batch([
            {"path": "s.py", "language": "python", "content_hash": "abc",
             "size_bytes": 100, "line_count": 10, "tier": 2},
        ])
        self.graph.upsert_symbols_batch([
            {"fqn": "s.a", "name": "a", "kind": "function", "language": "python",
             "file_path": "s.py", "start_line": 1, "end_line": 5, "tier": 2},
            {"fqn": "s.b", "name": "b", "kind": "function", "language": "python",
             "file_path": "s.py", "start_line": 6, "end_line": 10, "tier": 2},
            {"fqn": "s.c", "name": "c", "kind": "function", "language": "python",
             "file_path": "s.py", "start_line": 11, "end_line": 15, "tier": 2},
        ])
        self.graph.upsert_relations_batch([
            {"source_fqn": "s.a", "target_fqn": "s.b", "kind": "calls", "file_path": "s.py"},
            {"source_fqn": "s.b", "target_fqn": "s.c", "kind": "calls", "file_path": "s.py"},
        ])

        chain = self.graph.get_call_chain("s.a", depth=3)
        assert chain["name"] == "a"
        assert len(chain.get("calls", [])) == 1
        assert chain["calls"][0]["name"] == "b"
        assert len(chain["calls"][0].get("calls", [])) == 1

    def test_reverse_call_chain(self):
        self.graph.upsert_files_batch([
            {"path": "s.py", "language": "python", "content_hash": "abc",
             "size_bytes": 100, "line_count": 10, "tier": 2},
        ])
        self.graph.upsert_symbols_batch([
            {"fqn": "s.a", "name": "a", "kind": "function", "language": "python",
             "file_path": "s.py", "start_line": 1, "end_line": 5, "tier": 2},
            {"fqn": "s.b", "name": "b", "kind": "function", "language": "python",
             "file_path": "s.py", "start_line": 6, "end_line": 10, "tier": 2},
        ])
        self.graph.upsert_relations_batch([
            {"source_fqn": "s.a", "target_fqn": "s.b", "kind": "calls", "file_path": "s.py"},
        ])

        chain = self.graph.get_reverse_call_chain("s.b", depth=3)
        assert chain["name"] == "b"
        assert len(chain.get("called_by", [])) == 1
        assert chain["called_by"][0]["name"] == "a"

    def test_idempotent_upsert(self):
        files = [
            {"path": "x.py", "language": "python", "content_hash": "abc",
             "size_bytes": 100, "line_count": 10, "tier": 0},
        ]
        self.graph.upsert_files_batch(files)
        self.graph.upsert_files_batch(files)
        assert self.graph.get_stats()["files"] == 1

    def test_remove_file_cascades(self):
        self.graph.upsert_files_batch([
            {"path": "x.py", "language": "python", "content_hash": "abc",
             "size_bytes": 100, "line_count": 10, "tier": 1},
        ])
        self.graph.upsert_symbols_batch([
            {"fqn": "x.foo", "name": "foo", "kind": "function", "language": "python",
             "file_path": "x.py", "start_line": 1, "end_line": 5, "tier": 1},
        ])
        self.graph.upsert_relations_batch([
            {"source_fqn": "x.foo", "target_fqn": "x.bar", "kind": "calls", "file_path": "x.py"},
        ])

        self.graph.remove_file("x.py")

        stats = self.graph.get_stats()
        assert stats["files"] == 0
        assert stats["symbols"] == 0
        assert stats["relations"] == 0

    def test_hotspots(self):
        self.graph.upsert_files_batch([
            {"path": "s.py", "language": "python", "content_hash": "abc",
             "size_bytes": 100, "line_count": 10, "tier": 2},
        ])
        self.graph.upsert_symbols_batch([
            {"fqn": "s.hot", "name": "hot", "kind": "function", "language": "python",
             "file_path": "s.py", "start_line": 1, "end_line": 5, "tier": 2},
        ])
        self.graph.upsert_relations_batch([
            {"source_fqn": "s.a", "target_fqn": "s.hot", "kind": "calls", "file_path": "s.py"},
            {"source_fqn": "s.b", "target_fqn": "s.hot", "kind": "calls", "file_path": "s.py"},
            {"source_fqn": "s.c", "target_fqn": "s.hot", "kind": "calls", "file_path": "s.py"},
        ])

        hotspots = self.graph.get_hotspots(top_n=5)
        assert len(hotspots) >= 1
        assert hotspots[0]["fqn"] == "s.hot"
        assert hotspots[0]["ref_count"] == 3

    def test_db_size_stays_small(self):
        """Verify the DB stays compact even with many entries."""
        files = [
            {"path": f"src/file_{i}.py", "language": "python",
             "content_hash": f"hash_{i}", "size_bytes": 500, "line_count": 30, "tier": 1}
            for i in range(1000)
        ]
        self.graph.upsert_files_batch(files)

        symbols = [
            {"fqn": f"src.file_{i}.Cls{j}", "name": f"Cls{j}", "kind": "class",
             "language": "python", "file_path": f"src/file_{i}.py",
             "start_line": j * 10, "end_line": j * 10 + 9, "tier": 1}
            for i in range(1000) for j in range(5)
        ]
        self.graph.upsert_symbols_batch(symbols)

        stats = self.graph.get_stats()
        assert stats["files"] == 1000
        assert stats["symbols"] == 5000
        # DB should be well under 10MB for 1000 files
        assert stats["db_size_mb"] < 10
