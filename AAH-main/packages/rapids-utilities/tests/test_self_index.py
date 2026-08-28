"""Self-indexing test: CodeMap indexes its own codebase."""

from __future__ import annotations

from pathlib import Path

import pytest

from codemap_scale.orchestrator import CodeMapScale


@pytest.mark.slow
class TestSelfIndex:
    def test_indexes_own_codebase(self, tmp_path):
        """codemap-scale can index itself."""
        project_root = Path(__file__).parent.parent
        db_path = tmp_path / "self_index.db"

        cm = CodeMapScale(project_root, db_path=db_path, workers=2)
        try:
            stats = cm.scout(force=True)

            # Should find our Python files
            assert stats["files"] > 0
            assert "python" in stats.get("languages", {})

            # Should find known symbols
            results = cm.tools.search_structural("*CodeMapScale*")
            assert results["total"] >= 1
            assert any("orchestrator" in m["file_path"] for m in results["matches"])

            results2 = cm.tools.search_structural("*SQLiteSymbolGraph*")
            assert results2["total"] >= 1

            results3 = cm.tools.search_structural("*ParallelParserEngine*")
            assert results3["total"] >= 1

            results4 = cm.tools.search_structural("*TierManager*")
            assert results4["total"] >= 1

            # DB size should be small for this tiny codebase
            assert stats["db_size_mb"] < 5
        finally:
            cm.close()
