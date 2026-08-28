"""Micro-benchmarks using pytest-benchmark."""

from __future__ import annotations

from pathlib import Path

import pytest

from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph


@pytest.fixture
def bench_graph(tmp_path: Path) -> SQLiteSymbolGraph:
    """Graph pre-loaded with 1000 files and 5000 symbols for benchmarking."""
    db_path = tmp_path / "bench.db"
    graph = SQLiteSymbolGraph(db_path)

    files = [
        {
            "path": f"src/mod_{i}/file_{j}.py",
            "language": "python",
            "content_hash": f"hash_{i}_{j}",
            "size_bytes": 1000 + i,
            "line_count": 50 + j,
            "tier": 1,
        }
        for i in range(100)
        for j in range(10)
    ]
    graph.upsert_files_batch(files)

    symbols = [
        {
            "fqn": f"src.mod_{i}.file_{j}.func_{k}",
            "name": f"func_{k}",
            "kind": "function" if k % 3 != 0 else "class",
            "language": "python",
            "file_path": f"src/mod_{i}/file_{j}.py",
            "start_line": k * 10 + 1,
            "end_line": k * 10 + 8,
            "tier": 1,
        }
        for i in range(100)
        for j in range(10)
        for k in range(5)
    ]
    graph.upsert_symbols_batch(symbols)

    # Create some relations for call chain benchmarks
    relations = []
    for i in range(50):
        for j in range(5):
            relations.append({
                "source_fqn": f"src.mod_{i}.file_0.func_0",
                "target_fqn": f"src.mod_{i}.file_{j % 10}.func_{(j + 1) % 5}",
                "kind": "calls",
                "file_path": f"src/mod_{i}/file_0.py",
                "line": j * 10 + 5,
            })
    graph.upsert_relations_batch(relations)
    graph.optimize()

    yield graph
    graph.close()


@pytest.mark.benchmark(group="upsert")
class TestUpsertBenchmarks:
    @pytest.mark.parametrize("n", [100, 1000, 5000])
    def test_upsert_files(self, benchmark, tmp_path, n):
        graph = SQLiteSymbolGraph(tmp_path / "upsert_bench.db")
        files = [
            {
                "path": f"bench/file_{i}.py",
                "language": "python",
                "content_hash": f"h{i}",
                "size_bytes": 500,
                "line_count": 20,
                "tier": 1,
            }
            for i in range(n)
        ]
        benchmark(graph.upsert_files_batch, files)
        graph.close()

    @pytest.mark.parametrize("n", [100, 1000, 5000])
    def test_upsert_symbols(self, benchmark, tmp_path, n):
        graph = SQLiteSymbolGraph(tmp_path / "sym_bench.db")
        # Need files first
        graph.upsert_files_batch([
            {"path": "bench/f.py", "language": "python", "content_hash": "x", "size_bytes": 100, "line_count": 10, "tier": 1}
        ])
        symbols = [
            {
                "fqn": f"bench.f.sym_{i}",
                "name": f"sym_{i}",
                "kind": "function",
                "language": "python",
                "file_path": "bench/f.py",
                "start_line": i,
                "end_line": i + 5,
                "tier": 1,
            }
            for i in range(n)
        ]
        benchmark(graph.upsert_symbols_batch, symbols)
        graph.close()


@pytest.mark.benchmark(group="query")
class TestQueryBenchmarks:
    def test_find_symbols_wildcard(self, benchmark, bench_graph):
        benchmark(bench_graph.find_symbols, "%func_1%")

    def test_find_symbols_kind(self, benchmark, bench_graph):
        benchmark(bench_graph.find_symbols, "%func%", kind="class")

    def test_get_symbol(self, benchmark, bench_graph):
        benchmark(bench_graph.get_symbol, "src.mod_0.file_0.func_0")

    def test_get_file_symbols(self, benchmark, bench_graph):
        benchmark(bench_graph.get_file_symbols, "src/mod_0/file_0.py")

    def test_get_stats(self, benchmark, bench_graph):
        benchmark(bench_graph.get_stats)


@pytest.mark.benchmark(group="traversal")
class TestTraversalBenchmarks:
    @pytest.mark.parametrize("depth", [1, 2, 3])
    def test_call_chain(self, benchmark, bench_graph, depth):
        benchmark(bench_graph.get_call_chain, "src.mod_0.file_0.func_0", depth=depth)

    def test_reverse_call_chain(self, benchmark, bench_graph):
        benchmark(bench_graph.get_reverse_call_chain, "src.mod_0.file_0.func_1", depth=2)

    def test_hotspots(self, benchmark, bench_graph):
        benchmark(bench_graph.get_hotspots, top_n=20)


@pytest.mark.benchmark(group="engine")
class TestEngineBenchmarks:
    def test_tier0_scan(self, benchmark, sample_repo):
        from codemap_scale.parser.parallel_engine import ParallelParserEngine
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        benchmark(lambda: list(engine.scan_tier0()))

    def test_tier1_parse(self, benchmark, sample_repo):
        from codemap_scale.parser.parallel_engine import ParallelParserEngine
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        benchmark(lambda: list(engine.parse_tier1()))

    def test_walk_files(self, benchmark, sample_repo):
        from codemap_scale.parser.parallel_engine import ParallelParserEngine
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        benchmark(engine.walk_files)
