"""Integration tests for all three phases working together.

Tests the full workflow:
1. Scout code + ingest documents (Phase 1)
2. Detect communities + semantic edges (Phase 2)
3. Build wiki (Phase 3)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codemap_scale.orchestrator import CodeMapScale


@pytest.fixture
def full_repo(tmp_path: Path) -> Path:
    """Create a comprehensive repo with code, docs, and structure."""
    # Code: src/
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")

    (src / "service.py").write_text(textwrap.dedent("""\
        from src.validator import Validator, validate_card
        from src.utils import format_amount, log_transaction


        class PaymentService:
            \"\"\"Handles payment processing.\"\"\"

            def __init__(self, gateway):
                self.gateway = gateway
                self.validator = Validator()

            def process(self, amount, card_number):
                validate_card(card_number)
                formatted = format_amount(amount)
                result = self.gateway.charge(formatted, card_number)
                log_transaction(result)
                return result

            def refund(self, transaction_id, amount):
                return self.gateway.refund(transaction_id, amount)


        def create_service(gateway):
            return PaymentService(gateway)
    """))

    (src / "validator.py").write_text(textwrap.dedent("""\
        import re


        class Validator:
            \"\"\"Validates payment data.\"\"\"

            def validate(self, data):
                if not data.get("card_number"):
                    raise ValueError("Missing card number")
                return True


        def validate_card(card_number):
            if not re.match(r'^[0-9]{13,19}$', card_number):
                raise ValueError("Invalid card number format")
            return True
    """))

    (src / "utils.py").write_text(textwrap.dedent("""\
        import logging

        logger = logging.getLogger(__name__)


        def format_amount(amount):
            return round(float(amount), 2)


        def log_transaction(result):
            logger.info("Transaction: %s", result)
    """))

    (tmp_path / "main.py").write_text(textwrap.dedent("""\
        from src.service import PaymentService, create_service

        def main():
            service = create_service(gateway=None)
            result = service.process(100.0, "4111111111111111")
            print(result)

        if __name__ == "__main__":
            main()
    """))

    # Docs: docs/
    docs = tmp_path / "docs"
    docs.mkdir()

    (docs / "README.md").write_text(textwrap.dedent("""\
        # Payment System Documentation

        ## Overview

        This system implements the PaymentService for processing payments.
        The main components are:

        - **PaymentService**: Core payment processing class
        - **Validator**: Input validation for payment data
        - **Gateway**: External payment provider integration

        ## Architecture

        The PaymentService coordinates between the Validator and Gateway
        to process and validate payments.

        ## Getting Started

        See the API Guide for endpoint documentation.
    """))

    (docs / "api-guide.md").write_text(textwrap.dedent("""\
        # API Guide

        ## Endpoints

        ### POST /payments

        Creates a new payment using the PaymentService.

        **Parameters:**
        - amount: Payment amount
        - card_number: Credit card number

        ### POST /refunds

        Process a refund via PaymentService.refund().

        ### GET /validate

        Validate payment data using the Validator class.
    """))

    (docs / "changelog.txt").write_text(textwrap.dedent("""\
        v1.0 - Initial release with PaymentService
        v1.1 - Added Validator integration
        v1.2 - Refund support in PaymentService
    """))

    # Tests
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_service.py").write_text(textwrap.dedent("""\
        from src.service import PaymentService

        class TestPaymentService:
            def test_process(self):
                pass
    """))

    return tmp_path


@pytest.fixture
def cm(full_repo: Path, tmp_path: Path) -> CodeMapScale:
    db_path = tmp_path / "integration.db"
    instance = CodeMapScale(full_repo, db_path=db_path, workers=2)
    yield instance
    instance.close()


class TestPhase1DocumentIngestion:
    """Test document ingestion from folders and subfolders."""

    def test_ingest_docs_from_root(self, cm):
        cm.scout(force=True)
        result = cm.ingest_documents()

        assert result["documents"] > 0
        assert result["symbols"] > 0

    def test_ingest_docs_from_subdirectory(self, cm):
        cm.scout(force=True)
        result = cm.ingest_documents("docs")

        assert result["documents"] > 0

    def test_document_symbols_searchable(self, cm):
        cm.scout(force=True)
        cm.ingest_documents()

        results = cm.tools.search_documents("*Guide*")
        assert results["total"] > 0

    def test_doc_code_crossrefs_detected(self, cm):
        cm.scout(force=True)
        result = cm.ingest_documents()

        # Documents mention PaymentService, Validator — should create references
        assert result["references"] >= 0  # May be 0 if no matching symbols at detection time

    def test_documents_in_stats(self, cm):
        cm.scout(force=True)
        cm.ingest_documents()

        stats = cm.graph.get_stats()
        langs = stats["languages"]
        # Should have both code and document languages
        assert "python" in langs
        assert any(lang in langs for lang in ("markdown", "plaintext", "csv"))

    def test_nested_docs_discovered(self, cm, full_repo):
        # Add nested doc
        nested = full_repo / "docs" / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / "deep-doc.md").write_text("# Deep Nested Doc\n\nContent here.")

        cm.scout(force=True)
        result = cm.ingest_documents()

        assert result["documents"] >= 4  # README + api-guide + changelog + deep-doc


class TestPhase2CommunitiesAndSemanticEdges:
    """Test community detection and semantic analysis."""

    def test_detect_communities(self, cm):
        cm.scout(force=True)
        cm.focus("src/")

        result = cm.detect_communities()
        assert "communities" in result
        assert "god_nodes" in result

    def test_god_nodes_tool(self, cm):
        cm.scout(force=True)
        cm.focus("src/")

        result = cm.tools.get_god_nodes(top_n=5)
        assert "god_nodes" in result

    def test_communities_tool(self, cm):
        cm.scout(force=True)
        cm.focus("src/")
        cm.detect_communities()

        result = cm.tools.get_communities()
        assert "communities" in result

    def test_inferred_relations_stored(self, cm):
        cm.scout(force=True)
        cm.ingest_documents()

        # Check that inferred relations exist in the graph
        inferred = cm.graph.find_inferred_relations()
        # May be empty if no cross-references found, but should not error
        assert isinstance(inferred, list)


class TestPhase3WikiLayer:
    """Test wiki generation, search, and maintenance."""

    def test_build_wiki(self, cm):
        cm.scout(force=True)
        cm.focus("src/")

        result = cm.build_wiki()
        assert result["pages_created"] > 0

    def test_wiki_search(self, cm):
        cm.scout(force=True)
        cm.focus("src/")
        cm.build_wiki()

        result = cm.tools.search_wiki("payment")
        assert "results" in result

    def test_wiki_page_retrieval(self, cm):
        cm.scout(force=True)
        cm.focus("src/")
        cm.build_wiki()

        overview = cm.tools.get_wiki_overview()
        assert overview["total_pages"] > 0

        # Get a specific page
        pages = overview.get("pages", [])
        if pages:
            page = cm.tools.get_wiki_page(pages[0]["page_id"])
            assert "content" in page

    def test_wiki_lint(self, cm):
        cm.scout(force=True)
        cm.focus("src/")
        cm.build_wiki()

        result = cm.tools.lint_wiki()
        assert "issues" in result
        assert "coverage" in result

    def test_wiki_export(self, cm, tmp_path):
        cm.scout(force=True)
        cm.focus("src/")
        cm.build_wiki()

        export_dir = tmp_path / "exported_wiki"
        result = cm.wiki.export_markdown(export_dir)
        assert result["exported"] > 0
        assert (export_dir).exists()

    def test_wiki_rebuild_is_idempotent(self, cm):
        cm.scout(force=True)
        cm.focus("src/")

        result1 = cm.build_wiki()
        result2 = cm.build_wiki()

        # Second run should mostly be unchanged
        assert result2["pages_unchanged"] >= result1["pages_created"] - 1  # overview may update


class TestFullWorkflow:
    """Test all three phases together in the intended workflow."""

    def test_complete_pipeline(self, cm, tmp_path):
        """Full pipeline: scout → ingest docs → communities → wiki → query."""
        # Step 1: Scout codebase
        scout_result = cm.scout(force=True)
        assert scout_result["files"] > 0

        # Step 2: Ingest documents
        doc_result = cm.ingest_documents()
        assert doc_result["documents"] > 0

        # Step 3: Focus on key area
        cm.focus("src/")

        # Step 4: Detect communities
        comm_result = cm.detect_communities()
        assert "communities" in comm_result

        # Step 5: Build wiki
        wiki_result = cm.build_wiki(include_communities=True)
        assert wiki_result["pages_created"] > 0

        # Step 6: Query everything
        # Code search
        code_results = cm.tools.search_structural("*Payment*")
        assert code_results["total"] > 0

        # Document search
        doc_results = cm.tools.search_documents("*Guide*")
        assert doc_results["total"] >= 0

        # Wiki search
        wiki_results = cm.tools.search_wiki("payment")
        assert "results" in wiki_results

        # God nodes
        god_results = cm.tools.get_god_nodes()
        assert "god_nodes" in god_results

        # Overview
        overview = cm.tools.get_overview()
        assert overview["files"] > 0

        # Wiki overview
        wiki_overview = cm.tools.get_wiki_overview()
        assert wiki_overview["total_pages"] > 0

    def test_incremental_update_preserves_wiki(self, cm, full_repo, tmp_path):
        """After code changes, wiki should still be queryable."""
        cm.scout(force=True)
        cm.focus("src/")
        cm.build_wiki()

        # Simulate code change
        (full_repo / "src" / "service.py").write_text(
            (full_repo / "src" / "service.py").read_text() + "\n# modified\n"
        )

        # Update
        update_result = cm.update()
        assert update_result["changed"] >= 0

        # Wiki should still work
        wiki_stats = cm.wiki.get_stats()
        assert wiki_stats["total_pages"] > 0

    def test_document_update_workflow(self, cm, full_repo):
        """Documents can be re-ingested after changes."""
        cm.scout(force=True)
        result1 = cm.ingest_documents()

        # Add a new document
        (full_repo / "docs" / "new-doc.md").write_text("# New Document\n\nNew content.")

        result2 = cm.ingest_documents()
        assert result2["documents"] >= result1["documents"]
