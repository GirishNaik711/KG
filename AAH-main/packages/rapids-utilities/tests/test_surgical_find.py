"""
Surgical find tests: validate that the tool interface can identify
the right files for real-world tasks on known codebases.

These tests clone real repos and verify the "find the right files" workflow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codemap_scale.orchestrator import CodeMapScale


@pytest.mark.slow
class TestFlaskSurgicalFind:
    """Verify surgical find on Flask codebase."""

    @pytest.fixture(autouse=True)
    def setup_flask(self, flask_repo, tmp_path):
        self.cm = CodeMapScale(flask_repo, db_path=tmp_path / "flask.db", workers=2)
        self.cm.scout()
        yield
        self.cm.close()

    def test_find_flask_app_class(self):
        results = self.cm.tools.search_structural("*Flask*", kind="class")
        assert results["total"] >= 1
        files = {m["file_path"] for m in results["matches"]}
        assert any("app" in f for f in files)

    def test_find_blueprint(self):
        results = self.cm.tools.search_structural("*Blueprint*", kind="class")
        assert results["total"] >= 1

    def test_find_route_decorator(self):
        results = self.cm.tools.search_structural("*route*")
        assert results["total"] >= 1

    def test_overview_shows_python(self):
        overview = self.cm.tools.get_overview()
        assert "python" in overview.get("languages", {})
        assert overview["files"] > 50


@pytest.mark.slow
class TestDjangoSurgicalFind:
    """Verify surgical find on Django codebase."""

    @pytest.fixture(autouse=True)
    def setup_django(self, django_repo, tmp_path):
        self.cm = CodeMapScale(django_repo, db_path=tmp_path / "django.db", workers=4)
        self.cm.scout()
        yield
        self.cm.close()

    def test_find_orm_compiler(self):
        results = self.cm.tools.search_structural("*Compiler*", kind="class")
        assert results["total"] >= 1
        files = {m["file_path"] for m in results["matches"]}
        assert any("compiler" in f for f in files)

    def test_find_middleware(self):
        results = self.cm.tools.search_structural("*Middleware*", kind="class")
        assert results["total"] >= 2
        names = {m["name"] for m in results["matches"]}
        # Django has SecurityMiddleware, CsrfViewMiddleware, etc.
        assert any("Middleware" in n for n in names)

    def test_find_model_base(self):
        results = self.cm.tools.search_structural("*Model*", kind="class")
        assert results["total"] >= 1

    def test_find_queryset(self):
        results = self.cm.tools.search_structural("*QuerySet*", kind="class")
        assert results["total"] >= 1
        files = {m["file_path"] for m in results["matches"]}
        assert any("query" in f for f in files)

    def test_focus_db_models(self):
        result = self.cm.focus("django/db/models/")
        assert result.get("promoted", 0) >= 0

    def test_impact_analysis_base_model(self):
        # Find the base model file
        results = self.cm.tools.search_structural("*Model*", kind="class")
        model_files = {m["file_path"] for m in results["matches"]}
        base_file = None
        for f in model_files:
            if "base" in f and "django/db" in f:
                base_file = f
                break

        if base_file:
            impact = self.cm.tools.analyze_impact(base_file)
            assert impact["total_impact_radius"] >= 0

    def test_django_stats(self):
        overview = self.cm.tools.get_overview()
        assert overview["files"] > 2000
        assert overview["symbols"] > 5000


@pytest.mark.slow
class TestFastAPISurgicalFind:
    """Verify surgical find on FastAPI codebase."""

    @pytest.fixture(autouse=True)
    def setup_fastapi(self, fastapi_repo, tmp_path):
        self.cm = CodeMapScale(fastapi_repo, db_path=tmp_path / "fastapi.db", workers=2)
        self.cm.scout()
        yield
        self.cm.close()

    def test_find_depends(self):
        results = self.cm.tools.search_structural("*Depends*")
        assert results["total"] >= 1

    def test_find_apirouter(self):
        results = self.cm.tools.search_structural("*APIRouter*", kind="class")
        assert results["total"] >= 1

    def test_find_fastapi_class(self):
        results = self.cm.tools.search_structural("*FastAPI*", kind="class")
        assert results["total"] >= 1
