"""Tests for the parallel parser engine."""

from __future__ import annotations

from pathlib import Path


from codemap_scale.parser.parallel_engine import (
    ParallelParserEngine,
    _build_fqn,
    _detect_language,
    _parse_file_tier0,
)


class TestDetectLanguage:
    def test_python(self):
        assert _detect_language(".py") == "python"
        assert _detect_language(".pyi") == "python"

    def test_javascript(self):
        assert _detect_language(".js") == "javascript"
        assert _detect_language(".mjs") == "javascript"
        assert _detect_language(".jsx") == "jsx"

    def test_typescript(self):
        assert _detect_language(".ts") == "typescript"
        assert _detect_language(".tsx") == "tsx"

    def test_go(self):
        assert _detect_language(".go") == "go"

    def test_rust(self):
        assert _detect_language(".rs") == "rust"

    def test_java(self):
        assert _detect_language(".java") == "java"

    def test_unknown_returns_none(self):
        assert _detect_language(".txt") is None
        assert _detect_language(".md") is None
        assert _detect_language(".json") is None

    def test_case_insensitive(self):
        assert _detect_language(".PY") == "python"
        assert _detect_language(".Ts") == "typescript"


class TestBuildFqn:
    def test_python(self):
        assert _build_fqn("src/service.py", "PaymentService") == "src.service.PaymentService"

    def test_typescript(self):
        assert _build_fqn("frontend/index.ts", "App") == "frontend.index.App"

    def test_nested_path(self):
        assert _build_fqn("a/b/c/d.py", "foo") == "a.b.c.d.foo"

    def test_root_file(self):
        assert _build_fqn("main.py", "main") == "main.main"


class TestWalkFiles:
    def test_finds_source_files(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        files = engine.walk_files()

        suffixes = {f.suffix for f in files}
        assert ".py" in suffixes

        # Should find our known files
        rel_paths = {str(f.relative_to(sample_repo)) for f in files}
        assert "main.py" in rel_paths
        assert "src/service.py" in rel_paths
        assert "src/validator.py" in rel_paths
        assert "src/utils.py" in rel_paths

    def test_ignores_pycache(self, sample_repo: Path):
        pycache = sample_repo / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.cpython-311.pyc").write_text("fake")

        engine = ParallelParserEngine(root=sample_repo, workers=1)
        files = engine.walk_files()
        assert not any("__pycache__" in str(f) for f in files)

    def test_ignores_node_modules(self, sample_repo: Path):
        nm = sample_repo / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("module.exports = {}")

        engine = ParallelParserEngine(root=sample_repo, workers=1)
        files = engine.walk_files()
        rel_paths = [str(f.relative_to(sample_repo)) for f in files]
        assert not any("node_modules" in rp for rp in rel_paths)

    def test_custom_ignore_patterns(self, sample_repo: Path):
        engine = ParallelParserEngine(
            root=sample_repo, workers=1,
            ignore_patterns=["*.py", "__pycache__", ".git"],
        )
        files = engine.walk_files()
        # All our files are .py, so nothing should be found
        assert len(files) == 0

    def test_multilang(self, sample_repo_multilang: Path):
        engine = ParallelParserEngine(root=sample_repo_multilang, workers=1)
        files = engine.walk_files()
        suffixes = {f.suffix for f in files}
        assert ".py" in suffixes
        assert ".ts" in suffixes
        assert ".go" in suffixes


class TestTier0:
    def test_returns_metadata(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        batches = list(engine.scan_tier0())
        assert len(batches) >= 1

        all_results = [r for batch in batches for r in batch]
        assert len(all_results) > 0

        # Check dict shape
        r = all_results[0]
        assert "path" in r
        assert "language" in r
        assert "content_hash" in r
        assert "size_bytes" in r
        assert "line_count" in r
        assert r["tier"] == 0

    def test_batching(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        batches = list(engine.scan_tier0(batch_size=2))
        total = sum(len(b) for b in batches)
        assert total > 0
        # With 7+ files and batch_size=2, we should have multiple batches
        assert len(batches) >= 2

    def test_content_hash_deterministic(self, sample_repo: Path):
        root_str = str(sample_repo)
        path_str = str(sample_repo / "main.py")
        r1 = _parse_file_tier0(path_str, root_str)
        r2 = _parse_file_tier0(path_str, root_str)
        assert r1["content_hash"] == r2["content_hash"]

    def test_content_hash_changes_with_content(self, sample_repo: Path):
        root_str = str(sample_repo)
        path = sample_repo / "main.py"
        r1 = _parse_file_tier0(str(path), root_str)
        path.write_text("print('modified')")
        r2 = _parse_file_tier0(str(path), root_str)
        assert r1["content_hash"] != r2["content_hash"]


class TestTier1:
    def test_extracts_symbols(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        batches = list(engine.parse_tier1())
        assert len(batches) >= 1

        all_symbols = []
        for batch in batches:
            assert "files" in batch
            assert "symbols" in batch
            all_symbols.extend(batch["symbols"])

        # Should find known symbols
        symbol_names = {s["name"] for s in all_symbols}
        assert "PaymentService" in symbol_names
        assert "Validator" in symbol_names
        assert "validate_card" in symbol_names
        assert "format_amount" in symbol_names
        assert "main" in symbol_names

    def test_symbol_fqns(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        batches = list(engine.parse_tier1())
        all_symbols = [s for batch in batches for s in batch["symbols"]]

        fqns = {s["fqn"] for s in all_symbols}
        assert any("service.PaymentService" in fqn for fqn in fqns)
        assert any("validator.validate_card" in fqn for fqn in fqns)

    def test_symbol_has_required_fields(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        batches = list(engine.parse_tier1())
        all_symbols = [s for batch in batches for s in batch["symbols"]]

        for sym in all_symbols:
            assert "fqn" in sym
            assert "name" in sym
            assert "kind" in sym
            assert sym["kind"] in ("function", "method", "class", "interface", "enum", "type_alias")
            assert "language" in sym
            assert "file_path" in sym
            assert "start_line" in sym
            assert "end_line" in sym
            assert sym["start_line"] > 0
            assert sym["end_line"] >= sym["start_line"]

    def test_skips_large_files(self, sample_repo: Path):
        # Create a file just over the limit
        big_file = sample_repo / "big.py"
        big_file.write_text("x = 1\n" * 100000)  # ~600KB

        engine = ParallelParserEngine(root=sample_repo, workers=1, max_file_size_kb=1)
        batches = list(engine.parse_tier1())
        all_files = [f for batch in batches for f in batch["files"]]
        paths = {f["path"] for f in all_files}
        # big.py should be skipped
        assert "big.py" not in paths

    def test_extracts_methods_from_classes(self, sample_repo: Path):
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        batches = list(engine.parse_tier1())
        all_symbols = [s for batch in batches for s in batch["symbols"]]

        methods = [s for s in all_symbols if s["kind"] == "method"]
        method_names = {s["name"] for s in methods}
        assert "process" in method_names
        assert "refund" in method_names
        assert "validate" in method_names

    def test_specific_file_paths(self, sample_repo: Path):
        """Parse only specific files instead of all files."""
        engine = ParallelParserEngine(root=sample_repo, workers=1)
        target_files = [sample_repo / "src" / "validator.py"]
        batches = list(engine.parse_tier1(file_paths=target_files))

        all_symbols = [s for batch in batches for s in batch["symbols"]]
        files_parsed = {s["file_path"] for s in all_symbols}
        # Only validator.py symbols should appear
        assert all("validator" in fp for fp in files_parsed)

    def test_multilang_symbols(self, sample_repo_multilang: Path):
        engine = ParallelParserEngine(root=sample_repo_multilang, workers=1)
        batches = list(engine.parse_tier1())
        all_symbols = [s for batch in batches for s in batch["symbols"]]
        languages = {s["language"] for s in all_symbols}
        assert "python" in languages
        # TS/Go parsing depends on tree-sitter availability
