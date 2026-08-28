"""Tests for the Wiki Engine (Phase 3: LLM Wiki pattern)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph
from codemap_scale.wiki.engine import WikiEngine


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------

@pytest.fixture
def wiki_graph(tmp_path: Path) -> SQLiteSymbolGraph:
    """Graph pre-loaded with T2 data for wiki generation."""
    db_path = tmp_path / "wiki_test.db"
    graph = SQLiteSymbolGraph(db_path)

    # Files at T2
    graph.upsert_files_batch([
        {"path": "src/service.py", "language": "python", "content_hash": "aaa", "size_bytes": 500, "line_count": 30, "tier": 2},
        {"path": "src/validator.py", "language": "python", "content_hash": "bbb", "size_bytes": 400, "line_count": 25, "tier": 2},
        {"path": "src/utils.py", "language": "python", "content_hash": "ccc", "size_bytes": 200, "line_count": 15, "tier": 1},
    ])

    # Symbols
    graph.upsert_symbols_batch([
        {"fqn": "src.service.PaymentService", "name": "PaymentService", "kind": "class", "language": "python", "file_path": "src/service.py", "start_line": 5, "end_line": 22, "tier": 2, "docstring": "Handles payment processing."},
        {"fqn": "src.service.PaymentService.process", "name": "process", "kind": "method", "language": "python", "file_path": "src/service.py", "start_line": 12, "end_line": 18, "tier": 2, "signature": "def process(self, amount, card_number)"},
        {"fqn": "src.service.PaymentService.refund", "name": "refund", "kind": "method", "language": "python", "file_path": "src/service.py", "start_line": 20, "end_line": 22, "tier": 2, "signature": "def refund(self, transaction_id, amount)"},
        {"fqn": "src.service.create_service", "name": "create_service", "kind": "function", "language": "python", "file_path": "src/service.py", "start_line": 24, "end_line": 25, "tier": 2},
        {"fqn": "src.validator.Validator", "name": "Validator", "kind": "class", "language": "python", "file_path": "src/validator.py", "start_line": 4, "end_line": 15, "tier": 2, "docstring": "Validates payment data."},
        {"fqn": "src.validator.validate_card", "name": "validate_card", "kind": "function", "language": "python", "file_path": "src/validator.py", "start_line": 17, "end_line": 21, "tier": 2},
    ])

    # Relations
    graph.upsert_relations_batch([
        {"source_fqn": "src.service.PaymentService.process", "target_fqn": "src.validator.validate_card", "kind": "calls", "file_path": "src/service.py", "line": 14},
    ])

    yield graph
    graph.close()


@pytest.fixture
def wiki(wiki_graph: SQLiteSymbolGraph) -> WikiEngine:
    conn = wiki_graph._get_conn()
    return WikiEngine(conn)


@pytest.fixture
def symbols_service(wiki_graph: SQLiteSymbolGraph) -> list[dict[str, Any]]:
    return wiki_graph.get_file_symbols("src/service.py")


@pytest.fixture
def symbols_validator(wiki_graph: SQLiteSymbolGraph) -> list[dict[str, Any]]:
    return wiki_graph.get_file_symbols("src/validator.py")


# -------------------------------------------------------------------
# Tests: Wiki Page Ingestion
# -------------------------------------------------------------------

class TestWikiIngest:
    def test_ingest_module_creates_page(self, wiki, symbols_service):
        result = wiki.ingest_module("src/service.py", symbols_service)
        assert result["action"] == "created"
        assert result["version"] == 1
        assert "page_id" in result

    def test_ingest_module_idempotent(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        result2 = wiki.ingest_module("src/service.py", symbols_service)
        assert result2["action"] == "unchanged"

    def test_ingest_module_detects_update(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)

        # Modify symbols (simulate change)
        modified = symbols_service + [
            {"fqn": "src.service.new_func", "name": "new_func", "kind": "function",
             "language": "python", "file_path": "src/service.py", "start_line": 30,
             "end_line": 35, "tier": 2}
        ]
        result = wiki.ingest_module("src/service.py", modified)
        assert result["action"] == "updated"
        assert result["version"] == 2

    def test_ingest_module_content_has_symbols(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        page = wiki.get_page(wiki._path_to_page_id("src/service.py"))

        assert page is not None
        assert "PaymentService" in page["content"]
        assert "process" in page["content"]
        assert "refund" in page["content"]

    def test_ingest_with_custom_summary_fn(self, wiki, symbols_service):
        def custom_fn(symbols, path):
            return f"Custom summary for {path} with {len(symbols)} symbols."

        result = wiki.ingest_module("src/service.py", symbols_service, summary_fn=custom_fn)
        page = wiki.get_page(result["page_id"])
        assert "Custom summary" in page["content"]

    def test_ingest_overview(self, wiki):
        stats = {"files": 100, "symbols": 500, "relations": 200, "db_size_mb": 2.5, "languages": {"python": 80, "typescript": 20}}
        hotspots = [{"fqn": "a.b", "name": "b", "ref_count": 10, "file_path": "a.py"}]

        result = wiki.ingest_overview(stats, hotspots)
        assert result["action"] == "created"
        assert result["page_id"] == "_overview"

        page = wiki.get_page("_overview")
        assert "100" in page["content"]  # file count
        assert "500" in page["content"]  # symbol count

    def test_ingest_overview_with_communities(self, wiki):
        stats = {"files": 50, "symbols": 200, "relations": 100, "db_size_mb": 1.0, "languages": {"python": 50}}
        hotspots = []
        communities = [{"label": "payments", "member_count": 10, "density": 0.5, "files": ["a.py"]}]

        result = wiki.ingest_overview(stats, hotspots, communities)
        page = wiki.get_page("_overview")
        assert "payments" in page["content"]

    def test_ingest_directory(self, wiki, wiki_graph):
        files_syms = {
            "src/service.py": wiki_graph.get_file_symbols("src/service.py"),
            "src/validator.py": wiki_graph.get_file_symbols("src/validator.py"),
        }
        result = wiki.ingest_directory("src", files_syms)
        assert result["action"] == "created"

        page = wiki.get_page(result["page_id"])
        assert page is not None
        assert "src/service.py" in page["content"]
        assert "src/validator.py" in page["content"]


# -------------------------------------------------------------------
# Tests: Wiki Search
# -------------------------------------------------------------------

class TestWikiSearch:
    def test_search_finds_page(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)

        results = wiki.search("PaymentService")
        assert len(results) > 0
        assert any("PaymentService" in r.get("title", "") or "service" in r.get("page_id", "")
                    for r in results)

    def test_search_returns_ranked(self, wiki, symbols_service, symbols_validator):
        wiki.ingest_module("src/service.py", symbols_service)
        wiki.ingest_module("src/validator.py", symbols_validator)

        results = wiki.search("payment")
        assert len(results) > 0
        # Results should have relevance_rank
        assert all("relevance_rank" in r for r in results)

    def test_search_empty_query(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        # FTS5 may not handle empty queries well
        results = wiki.search("nonexistent_xyz_abc")
        assert len(results) == 0

    def test_search_limit(self, wiki, symbols_service, symbols_validator):
        wiki.ingest_module("src/service.py", symbols_service)
        wiki.ingest_module("src/validator.py", symbols_validator)

        results = wiki.search("class", limit=1)
        assert len(results) <= 1


# -------------------------------------------------------------------
# Tests: Wiki Page Retrieval
# -------------------------------------------------------------------

class TestWikiRetrieval:
    def test_get_page(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        page_id = wiki._path_to_page_id("src/service.py")
        page = wiki.get_page(page_id)

        assert page is not None
        assert page["title"]
        assert page["content"]
        assert page["page_type"] == "module"
        assert isinstance(page["source_symbols"], list)
        assert isinstance(page["tags"], list)

    def test_get_nonexistent_page(self, wiki):
        assert wiki.get_page("nonexistent_page") is None

    def test_get_all_pages(self, wiki, symbols_service, symbols_validator):
        wiki.ingest_module("src/service.py", symbols_service)
        wiki.ingest_module("src/validator.py", symbols_validator)

        pages = wiki.get_all_pages()
        assert len(pages) == 2

    def test_get_all_pages_by_type(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        stats = {"files": 10, "symbols": 50, "relations": 20, "db_size_mb": 0.1, "languages": {}}
        wiki.ingest_overview(stats, [])

        modules = wiki.get_all_pages(page_type="module")
        overviews = wiki.get_all_pages(page_type="overview")
        assert len(modules) == 1
        assert len(overviews) == 1

    def test_get_stats(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        stats = wiki.get_stats()

        assert stats["total_pages"] == 1
        assert "module" in stats["by_type"]


# -------------------------------------------------------------------
# Tests: Wiki Lint
# -------------------------------------------------------------------

class TestWikiLint:
    def test_lint_clean(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        result = wiki.lint()

        assert "issues" in result
        assert "coverage" in result
        assert result["total_pages"] == 1

    def test_lint_detects_empty_pages(self, wiki):
        # Insert a page with very little content
        conn = wiki._conn
        conn.execute(
            """INSERT INTO wiki_pages (page_id, title, content, page_type)
               VALUES ('empty_page', 'Empty', 'x', 'module')"""
        )
        conn.commit()

        result = wiki.lint()
        empty_issues = [i for i in result["issues"] if i["type"] == "empty_page"]
        assert len(empty_issues) == 1

    def test_lint_reports_coverage(self, wiki, symbols_service):
        wiki.ingest_module("src/service.py", symbols_service)
        result = wiki.lint()

        coverage = result["coverage"]
        assert "t2_files" in coverage
        assert "covered_files" in coverage
        assert "uncovered_files" in coverage


# -------------------------------------------------------------------
# Tests: Wiki Export
# -------------------------------------------------------------------

class TestWikiExport:
    def test_export_markdown(self, wiki, symbols_service, tmp_path):
        wiki.ingest_module("src/service.py", symbols_service)

        output_dir = tmp_path / "wiki_export"
        result = wiki.export_markdown(output_dir)

        assert result["exported"] == 1
        assert output_dir.exists()

        # Check exported file
        md_files = list(output_dir.glob("*.md"))
        assert len(md_files) == 1

        content = md_files[0].read_text()
        assert "---" in content  # YAML frontmatter
        assert "PaymentService" in content

    def test_export_creates_directory(self, wiki, symbols_service, tmp_path):
        wiki.ingest_module("src/service.py", symbols_service)
        output_dir = tmp_path / "new" / "nested" / "dir"
        result = wiki.export_markdown(output_dir)
        assert output_dir.exists()
        assert result["exported"] == 1

    def test_export_empty_wiki(self, wiki, tmp_path):
        output_dir = tmp_path / "empty_export"
        result = wiki.export_markdown(output_dir)
        assert result["exported"] == 0


# -------------------------------------------------------------------
# Tests: Wiki Delete
# -------------------------------------------------------------------

class TestWikiDelete:
    def test_delete_page(self, wiki, symbols_service):
        result = wiki.ingest_module("src/service.py", symbols_service)
        page_id = result["page_id"]

        assert wiki.delete_page(page_id) is True
        assert wiki.get_page(page_id) is None

    def test_delete_nonexistent(self, wiki):
        assert wiki.delete_page("nonexistent") is False

    def test_delete_all(self, wiki, symbols_service, symbols_validator):
        wiki.ingest_module("src/service.py", symbols_service)
        wiki.ingest_module("src/validator.py", symbols_validator)

        count = wiki.delete_all()
        assert count == 2
        assert wiki.get_stats()["total_pages"] == 0


# -------------------------------------------------------------------
# Tests: Content Generation
# -------------------------------------------------------------------

class TestContentGeneration:
    def test_page_content_includes_classes(self, wiki, symbols_service):
        content = wiki._generate_page_content("src/service.py", symbols_service)
        assert "## Classes" in content
        assert "PaymentService" in content

    def test_page_content_includes_functions(self, wiki, symbols_service):
        content = wiki._generate_page_content("src/service.py", symbols_service)
        assert "create_service" in content

    def test_page_content_includes_docstrings(self, wiki, symbols_service):
        content = wiki._generate_page_content("src/service.py", symbols_service)
        assert "Handles payment processing" in content

    def test_overview_content(self, wiki):
        stats = {"files": 100, "symbols": 500, "relations": 200, "db_size_mb": 2.5,
                 "languages": {"python": 80, "typescript": 20}}
        hotspots = [{"name": "main", "ref_count": 10, "file_path": "app.py"}]

        content = wiki._generate_overview_content(stats, hotspots)
        assert "# Codebase Overview" in content
        assert "100" in content
        assert "python" in content
        assert "main" in content

    def test_directory_content(self, wiki, wiki_graph):
        files_syms = {
            "src/service.py": wiki_graph.get_file_symbols("src/service.py"),
        }
        content = wiki._generate_directory_content("src", files_syms)
        assert "src/service.py" in content

    def test_path_to_page_id(self):
        assert WikiEngine._path_to_page_id("src/service.py") == "src_service_py"
        assert WikiEngine._path_to_page_id("src/") == "src"

    def test_generate_title(self):
        assert WikiEngine._generate_title("src/payment_service.py") == "Payment Service"
        assert WikiEngine._generate_title("src/PaymentGateway.py") == "Payment Gateway"

    def test_auto_tag(self):
        tags = WikiEngine._auto_tag("src/api/routes.py", [])
        assert "api" in tags

        tags = WikiEngine._auto_tag("tests/test_service.py", [])
        assert "testing" in tags

        tags = WikiEngine._auto_tag("src/auth/handler.py", [])
        assert "security" in tags

    def test_extract_summary(self):
        content = "# Title\n\nThis is the first meaningful paragraph of the document."
        summary = WikiEngine._extract_summary(content)
        assert "first meaningful paragraph" in summary


# -------------------------------------------------------------------
# Integration: Full Orchestrator Wiki Flow
# -------------------------------------------------------------------

class TestOrchestratorWikiFlow:
    def test_build_wiki_from_orchestrator(self, sample_repo, tmp_path):
        from codemap_scale.orchestrator import CodeMapScale

        db_path = tmp_path / "wiki_flow.db"
        cm = CodeMapScale(sample_repo, db_path=db_path, workers=2)
        try:
            cm.scout(force=True)
            cm.focus("src/")

            result = cm.build_wiki()
            assert result["pages_created"] > 0
            assert result["wiki_stats"]["total_pages"] > 0

            # Search should work
            search_result = cm.tools.search_wiki("payment")
            assert "results" in search_result

            # Wiki overview should work
            overview = cm.tools.get_wiki_overview()
            assert overview["total_pages"] > 0

            # Lint should work
            lint_result = cm.tools.lint_wiki()
            assert "issues" in lint_result
        finally:
            cm.close()

    def test_wiki_search_tool(self, sample_repo, tmp_path):
        from codemap_scale.orchestrator import CodeMapScale

        db_path = tmp_path / "wiki_search.db"
        cm = CodeMapScale(sample_repo, db_path=db_path, workers=2)
        try:
            cm.scout(force=True)
            cm.focus("src/")
            cm.build_wiki()

            result = cm.tools.search_wiki("PaymentService")
            assert "results" in result
        finally:
            cm.close()
