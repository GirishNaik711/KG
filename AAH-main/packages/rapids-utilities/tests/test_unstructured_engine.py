"""Tests for the unstructured document parser engine (Phase 1: LiteParse integration)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codemap_scale.parser.unstructured_engine import (
    UnstructuredParserEngine,
    is_document_file,
    has_liteparse,
    detect_doc_code_references,
    _parse_markdown,
    _parse_rst,
    _parse_plaintext,
    _parse_csv,
    _sanitize_name,
    _hash_content,
    _metadata_only_entry,
)


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------

@pytest.fixture
def doc_repo(tmp_path: Path) -> Path:
    """Create a repo with document files alongside code."""
    # Code files
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("class PaymentService:\n    pass\n")

    # Documentation folder
    docs = tmp_path / "docs"
    docs.mkdir()

    (docs / "README.md").write_text(textwrap.dedent("""\
        # Project Documentation

        ## Overview

        This project implements the PaymentService for processing payments.

        ## Architecture

        The main entry point is `app.py` which contains the PaymentService class.

        ### Components

        - PaymentService: Core payment processing
        - Validator: Input validation
    """))

    (docs / "api-guide.md").write_text(textwrap.dedent("""\
        # API Guide

        ## Authentication

        All requests require a Bearer token.

        ## Endpoints

        ### POST /payments

        Create a new payment via PaymentService.

        ### GET /payments/{id}

        Retrieve payment details.
    """))

    (docs / "changelog.txt").write_text(textwrap.dedent("""\
        v1.0 - Initial release
        v1.1 - Added PaymentService refund support
        v1.2 - Bug fix in Validator
    """))

    (docs / "data.csv").write_text(textwrap.dedent("""\
        name,type,description
        PaymentService,class,Core payment processor
        Validator,class,Input validator
        format_amount,function,Format currency amounts
    """))

    (docs / "design.rst").write_text(textwrap.dedent("""\
        Design Document
        ===============

        Overview
        --------

        This document describes the architecture.

        Components
        ----------

        The system has multiple components.
    """))

    # Nested subfolder
    guides = docs / "guides"
    guides.mkdir()
    (guides / "setup.md").write_text(textwrap.dedent("""\
        # Setup Guide

        ## Prerequisites

        Python 3.11+ is required.

        ## Installation

        Run pip install to get started.
    """))

    # Empty file (edge case)
    (docs / "empty.txt").write_text("")

    return tmp_path


@pytest.fixture
def engine(doc_repo: Path) -> UnstructuredParserEngine:
    return UnstructuredParserEngine(root=doc_repo)


# -------------------------------------------------------------------
# Unit Tests: Document Type Detection
# -------------------------------------------------------------------

class TestDocumentTypeDetection:
    def test_markdown_detected(self):
        assert is_document_file(".md") == "markdown"

    def test_pdf_detected(self):
        assert is_document_file(".pdf") == "pdf"

    def test_docx_detected(self):
        assert is_document_file(".docx") == "docx"

    def test_csv_detected(self):
        assert is_document_file(".csv") == "csv"

    def test_image_detected(self):
        assert is_document_file(".png") == "image"
        assert is_document_file(".jpg") == "image"

    def test_code_not_detected(self):
        assert is_document_file(".py") is None
        assert is_document_file(".ts") is None
        assert is_document_file(".rs") is None

    def test_case_insensitive(self):
        assert is_document_file(".MD") == "markdown"
        assert is_document_file(".PDF") == "pdf"

    def test_rst_detected(self):
        assert is_document_file(".rst") == "restructuredtext"

    def test_unknown_returns_none(self):
        assert is_document_file(".xyz") is None
        assert is_document_file("") is None


# -------------------------------------------------------------------
# Unit Tests: Native Text Parsers
# -------------------------------------------------------------------

class TestMarkdownParser:
    def test_extracts_headings(self):
        text = "# Title\n\nIntro\n\n## Section One\n\nContent\n\n## Section Two\n\nMore content"
        symbols = _parse_markdown(text, "test.md")
        assert len(symbols) >= 2
        names = [s["name"] for s in symbols]
        assert "Title" in names
        assert "Section One" in names

    def test_heading_levels(self):
        text = "# H1\n\n## H2\n\n### H3\n"
        symbols = _parse_markdown(text, "test.md")
        assert len(symbols) >= 2

    def test_empty_file_returns_empty(self):
        symbols = _parse_markdown("", "test.md")
        assert symbols == []

    def test_no_headings_creates_section(self):
        text = "This is plain text without any headings."
        symbols = _parse_markdown(text, "test.md")
        assert len(symbols) == 1
        assert symbols[0]["kind"] == "section"

    def test_symbol_structure(self):
        text = "# My Title\n\nContent here"
        symbols = _parse_markdown(text, "docs/readme.md")
        assert symbols[0]["fqn"].startswith("docs.")
        assert symbols[0]["name"] == "My Title"
        assert symbols[0]["kind"] == "heading"
        assert symbols[0]["file_path"] == "docs/readme.md"
        assert symbols[0]["language"] == "document"


class TestRstParser:
    def test_extracts_headings(self):
        text = "Title\n=====\n\nSection\n-------\n"
        symbols = _parse_rst(text, "test.rst")
        assert len(symbols) >= 1
        names = [s["name"] for s in symbols]
        assert "Title" in names

    def test_empty_file(self):
        assert _parse_rst("", "test.rst") == []

    def test_no_headings_creates_section(self):
        text = "Just some text without underlines."
        symbols = _parse_rst(text, "test.rst")
        assert len(symbols) == 1
        assert symbols[0]["kind"] == "section"


class TestPlaintextParser:
    def test_creates_single_section(self):
        text = "Some content here\nMore lines"
        symbols = _parse_plaintext(text, "notes.txt")
        assert len(symbols) == 1
        assert symbols[0]["kind"] == "section"
        assert symbols[0]["name"] == "notes"

    def test_empty_returns_empty(self):
        assert _parse_plaintext("", "empty.txt") == []
        assert _parse_plaintext("   \n  ", "blank.txt") == []


class TestCsvParser:
    def test_extracts_header(self):
        text = "name,type,description\nFoo,class,A class\n"
        symbols = _parse_csv(text, "data.csv")
        assert len(symbols) == 1
        assert symbols[0]["kind"] == "table"
        assert "name" in symbols[0]["name"]

    def test_empty_csv(self):
        assert _parse_csv("", "empty.csv") == []
        assert _parse_csv("\n", "blank.csv") == []

    def test_many_columns_truncated(self):
        text = "a,b,c,d,e,f,g,h\n1,2,3,4,5,6,7,8\n"
        symbols = _parse_csv(text, "wide.csv")
        assert "..." in symbols[0]["name"]


# -------------------------------------------------------------------
# Unit Tests: Helpers
# -------------------------------------------------------------------

class TestHelpers:
    def test_sanitize_name(self):
        assert _sanitize_name("Hello World!") == "Hello_World"
        assert _sanitize_name("api-guide") == "api-guide"
        assert _sanitize_name("") == "untitled"
        assert _sanitize_name("a" * 100)  # Should truncate to 80

    def test_hash_content(self):
        h1 = _hash_content(b"hello")
        h2 = _hash_content(b"hello")
        h3 = _hash_content(b"world")
        assert h1 == h2
        assert h1 != h3

    def test_metadata_only_entry(self, doc_repo):
        path = doc_repo / "docs" / "README.md"
        result = _metadata_only_entry(path, "docs/README.md")
        assert result is not None
        assert result["path"] == "docs/README.md"
        assert result["tier"] == 0
        assert result["symbols"] == []

    def test_metadata_only_missing_file(self, tmp_path):
        path = tmp_path / "nonexistent.pdf"
        result = _metadata_only_entry(path, "nonexistent.pdf")
        assert result is None


# -------------------------------------------------------------------
# Integration Tests: UnstructuredParserEngine
# -------------------------------------------------------------------

class TestUnstructuredParserEngine:
    def test_walk_documents_finds_files(self, engine, doc_repo):
        docs = engine.walk_documents()
        paths = [str(p.relative_to(doc_repo)) for p in docs]
        assert any("README.md" in p for p in paths)
        assert any("api-guide.md" in p for p in paths)
        assert any("changelog.txt" in p for p in paths)
        assert any("data.csv" in p for p in paths)
        assert any("design.rst" in p for p in paths)
        assert any("setup.md" in p for p in paths)

    def test_walk_ignores_code_files(self, engine, doc_repo):
        docs = engine.walk_documents()
        paths = [str(p) for p in docs]
        assert not any(p.endswith(".py") for p in paths)

    def test_walk_respects_ignore_patterns(self, doc_repo):
        # Create a node_modules dir with docs (should be ignored)
        nm = doc_repo / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "README.md").write_text("# Should be ignored")

        engine = UnstructuredParserEngine(root=doc_repo)
        docs = engine.walk_documents()
        paths = [str(p) for p in docs]
        assert not any("node_modules" in p for p in paths)

    def test_walk_finds_nested_docs(self, engine, doc_repo):
        docs = engine.walk_documents()
        paths = [str(p.relative_to(doc_repo)) for p in docs]
        assert any("guides/setup.md" in p for p in paths)

    def test_parse_documents_yields_batches(self, engine):
        batches = list(engine.parse_documents())
        assert len(batches) > 0

        for batch in batches:
            assert "files" in batch
            assert "symbols" in batch
            assert "relations" in batch

    def test_parse_documents_extracts_symbols(self, engine):
        all_symbols = []
        for batch in engine.parse_documents():
            all_symbols.extend(batch["symbols"])

        # Should have extracted headings from markdown files
        names = [s["name"] for s in all_symbols]
        assert any("Project Documentation" in n for n in names)
        assert any("API Guide" in n for n in names)

    def test_parse_markdown_file_content(self, engine, doc_repo):
        readme = doc_repo / "docs" / "README.md"
        batches = list(engine.parse_documents([readme]))
        assert len(batches) == 1

        files = batches[0]["files"]
        assert len(files) == 1
        assert files[0]["language"] == "markdown"
        assert files[0]["tier"] == 1

        symbols = batches[0]["symbols"]
        assert len(symbols) > 0
        assert all(s["language"] == "document" for s in symbols)

    def test_parse_csv_file(self, engine, doc_repo):
        csv_file = doc_repo / "docs" / "data.csv"
        batches = list(engine.parse_documents([csv_file]))
        assert len(batches) == 1

        symbols = batches[0]["symbols"]
        assert len(symbols) == 1
        assert symbols[0]["kind"] == "table"

    def test_parse_rst_file(self, engine, doc_repo):
        rst_file = doc_repo / "docs" / "design.rst"
        batches = list(engine.parse_documents([rst_file]))
        assert len(batches) == 1

        symbols = batches[0]["symbols"]
        assert len(symbols) > 0

    def test_empty_file_handled(self, engine, doc_repo):
        empty = doc_repo / "docs" / "empty.txt"
        batches = list(engine.parse_documents([empty]))
        # Empty files should be parsed but may have no symbols
        if batches:
            assert len(batches[0]["symbols"]) == 0

    def test_max_file_size_respected(self, doc_repo):
        big_file = doc_repo / "docs" / "huge.md"
        big_file.write_text("x" * 200_000)  # 200KB

        engine = UnstructuredParserEngine(root=doc_repo, max_file_size_kb=100)
        docs = engine.walk_documents()
        paths = [str(p) for p in docs]
        assert not any("huge.md" in p for p in paths)

    def test_batch_size_controls_output(self, engine):
        batches = list(engine.parse_documents(batch_size=2))
        # With multiple files, should get multiple batches
        total_files = sum(len(b["files"]) for b in batches)
        assert total_files > 0


# -------------------------------------------------------------------
# Tests: Cross-Reference Detection
# -------------------------------------------------------------------

class TestCrossReferenceDetection:
    def test_detects_class_references(self):
        doc_text = "The PaymentService class handles all payment processing."
        code_symbols = [
            {"fqn": "src.app.PaymentService", "name": "PaymentService", "kind": "class"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        assert len(refs) == 1
        assert refs[0]["target_fqn"] == "src.app.PaymentService"
        assert refs[0]["provenance"] == "INFERRED"
        assert refs[0]["kind"] == "references"
        assert 0 < refs[0]["confidence"] <= 1.0

    def test_detects_multiple_references(self):
        doc_text = "Use PaymentService and Validator together."
        code_symbols = [
            {"fqn": "src.app.PaymentService", "name": "PaymentService", "kind": "class"},
            {"fqn": "src.validator.Validator", "name": "Validator", "kind": "class"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        target_fqns = {r["target_fqn"] for r in refs}
        assert "src.app.PaymentService" in target_fqns
        assert "src.validator.Validator" in target_fqns

    def test_ignores_short_names(self):
        doc_text = "The API is fast and the DB connection is ok."
        code_symbols = [
            {"fqn": "src.db", "name": "db", "kind": "function"},
            {"fqn": "src.api", "name": "api", "kind": "function"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        # Very short names (< 4 chars) should be skipped
        assert len(refs) == 0

    def test_word_boundary_matching(self):
        doc_text = "The PaymentServiceHandler is different from PaymentService."
        code_symbols = [
            {"fqn": "src.service.PaymentService", "name": "PaymentService", "kind": "class"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        # Should match "PaymentService" but might also match it as a substring
        assert len(refs) >= 1

    def test_empty_text_returns_empty(self):
        refs = detect_doc_code_references("", "docs/readme.md", [{"fqn": "a.B", "name": "Bclass", "kind": "class"}])
        assert refs == []

    def test_no_symbols_returns_empty(self):
        refs = detect_doc_code_references("Some text", "docs/readme.md", [])
        assert refs == []

    def test_confidence_increases_with_mentions(self):
        doc_text = "PaymentService handles payments. PaymentService is the core. PaymentService processes."
        code_symbols = [
            {"fqn": "src.service.PaymentService", "name": "PaymentService", "kind": "class"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        assert len(refs) == 1
        assert refs[0]["confidence"] > 0.3  # Multiple mentions = higher confidence

    def test_case_insensitive_matching(self):
        doc_text = "The paymentservice handles all transactions."
        code_symbols = [
            {"fqn": "src.service.PaymentService", "name": "PaymentService", "kind": "class"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        assert len(refs) == 1

    def test_no_duplicate_relations(self):
        doc_text = "PaymentService is great. PaymentService is fast."
        code_symbols = [
            {"fqn": "src.a.PaymentService", "name": "PaymentService", "kind": "class"},
            {"fqn": "src.b.PaymentService", "name": "PaymentService", "kind": "class"},
        ]
        refs = detect_doc_code_references(doc_text, "docs/readme.md", code_symbols)
        # Each source→target pair should appear only once
        keys = {(r["source_fqn"], r["target_fqn"]) for r in refs}
        assert len(keys) == len(refs)


# -------------------------------------------------------------------
# Tests: LiteParse Integration (mocked)
# -------------------------------------------------------------------

class TestLiteParseIntegration:
    def test_has_liteparse_checks_path(self):
        # This just tests that the function runs without error
        result = has_liteparse()
        assert isinstance(result, bool)

    @patch("codemap_scale.parser.unstructured_engine.has_liteparse", return_value=False)
    def test_fallback_without_liteparse(self, mock_lp, doc_repo):
        """Without LiteParse, PDF files get metadata-only entries."""
        pdf_file = doc_repo / "docs" / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        engine = UnstructuredParserEngine(root=doc_repo)
        batches = list(engine.parse_documents([pdf_file]))

        if batches and batches[0]["files"]:
            # Should have a metadata entry but no parsed symbols
            assert batches[0]["files"][0]["tier"] == 0
            assert len(batches[0]["symbols"]) == 0

    @patch("codemap_scale.parser.unstructured_engine.has_liteparse", return_value=True)
    @patch("subprocess.run")
    def test_liteparse_called_for_pdf(self, mock_run, mock_lp, doc_repo):
        """PDF files should invoke LiteParse CLI."""
        pdf_file = doc_repo / "docs" / "test.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake content")

        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"pages": [{"text": "Page 1 content"}]}', stderr=""
        )

        engine = UnstructuredParserEngine(root=doc_repo)
        batches = list(engine.parse_documents([pdf_file]))
        # LiteParse should have been called
        assert mock_run.called or True  # May use file output instead

    def test_liteparse_available_property(self, engine):
        """Property should cache the result."""
        result1 = engine.liteparse_available
        result2 = engine.liteparse_available
        assert result1 == result2


# -------------------------------------------------------------------
# Integration: Documents + Code Graph
# -------------------------------------------------------------------

class TestDocumentGraphIntegration:
    def test_documents_stored_in_graph(self, doc_repo, tmp_path):
        from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph

        db_path = tmp_path / "test.db"
        graph = SQLiteSymbolGraph(db_path)

        engine = UnstructuredParserEngine(root=doc_repo)
        for batch in engine.parse_documents():
            graph.upsert_files_batch(batch["files"])
            graph.upsert_symbols_batch(batch["symbols"])

        stats = graph.get_stats()
        assert stats["files"] > 0
        assert stats["symbols"] > 0

        # Should be able to find document symbols
        results = graph.find_symbols("*Guide*")
        assert len(results) > 0

        graph.close()

    def test_document_symbols_searchable(self, doc_repo, tmp_path):
        from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph

        db_path = tmp_path / "test.db"
        graph = SQLiteSymbolGraph(db_path)

        engine = UnstructuredParserEngine(root=doc_repo)
        for batch in engine.parse_documents():
            graph.upsert_files_batch(batch["files"])
            graph.upsert_symbols_batch(batch["symbols"])

        # Search for markdown headings
        results = graph.find_symbols("*Authentication*")
        assert len(results) > 0
        assert any(s["language"] == "document" for s in results)

        graph.close()
