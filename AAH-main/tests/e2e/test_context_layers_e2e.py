"""E2E tests for the 3-layer context system (WS1.7)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json, write_text, write_yaml
from aah.core.common.manifest import get_default_manifest, save_manifest
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.build.load_impl_context import load_impl_context, build_status_summary
from aah.core.intake.intake import (
    add_qa_round,
    get_default_intake,
    save_intake,
    set_problem_statement,
    set_project_type,
)
from aah.core.intel.check_intel_update import (
    _increment_pending_changes,
    check_intel_update_needed,
    staleness_check,
    THRESHOLD,
)


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _setup_project(tmp_path, project_type="greenfield"):
    """Create a minimal project with .aah/ structure."""
    rapids = tmp_path / ".aah"
    for d in ["plan/features", "plan/specs", "discuss", "architecture/decisions",
               "build/test-results", "audit", "brownfield", "codebase-intel"]:
        (rapids / d).mkdir(parents=True)

    manifest = get_default_manifest("test-app", project_type)
    save_manifest(manifest, rapids / "manifest.yaml")

    progress = get_default_progress()
    progress["current_phase"] = "build"
    save_progress(progress, rapids / "claude-progress.json")

    return tmp_path


class TestGreenfield3Layers:
    def test_all_layers_present(self, tmp_path):
        """Greenfield project with all 3 layers should load them all."""
        proj = _setup_project(tmp_path, "greenfield")
        rapids = proj / ".aah"

        # Layer 1: Knowledge
        kdir = proj / "knowledge"
        kdir.mkdir()
        write_text("# API Design Guide\nUse REST.\n", kdir / "api-guide.md")

        # Layer 2: Codebase intel (greenfield mid-implementation)
        write_text("# Structure\nPython FastAPI project\n", rapids / "codebase-intel" / "codebase-structure.md")
        write_json({"files": ["main.py"]}, rapids / "codebase-intel" / "codebase-profile.json")

        # Layer 3: Intake + progress already set up
        intake = get_default_intake()
        set_problem_statement(intake, "Build an AI agent system")
        set_project_type(intake, "new_system")
        add_qa_round(intake, "start", [{"question": "Stack?", "answer": "Python FastAPI"}])
        save_intake(intake, rapids / "intake.json")

        context = load_impl_context(proj)

        assert context.get("knowledge_context"), "Layer 1 should be present"
        assert context.get("brownfield_summary"), "Layer 2 (codebase intel) should be present"
        assert context.get("intake_summary"), "Layer 3 intake should be present"

    def test_status_summary_references_all_layers(self, tmp_path):
        """Status summary should reference all active layers."""
        proj = _setup_project(tmp_path, "greenfield")
        rapids = proj / ".aah"

        kdir = proj / "knowledge"
        kdir.mkdir()
        write_text("# Requirements\n", kdir / "requirements.md")

        write_text("# Codebase structure\n", rapids / "codebase-intel" / "codebase-structure.md")

        intake = get_default_intake()
        set_problem_statement(intake, "Build task manager")
        save_intake(intake, rapids / "intake.json")

        context = load_impl_context(proj)
        summary = context["status_summary"]

        assert "test-app" in summary  # project name
        assert "build" in summary  # phase
        assert "task manager" in summary  # from intake

    def test_staleness_triggers_for_greenfield(self, tmp_path):
        """Staleness should fire for greenfield projects after significant writes."""
        proj = _setup_project(tmp_path, "greenfield")
        rapids = proj / ".aah"

        write_json({"files": []}, rapids / "codebase-intel" / "codebase-profile.json")

        # Significant changes should be detected
        assert check_intel_update_needed("src/__init__.py", proj)
        assert check_intel_update_needed("pyproject.toml", proj)

        # Accumulate past threshold
        for i in range(THRESHOLD):
            _increment_pending_changes(proj, f"file_{i}.py")

        result = staleness_check(proj)
        assert result["stale"] is True
        assert result["has_intel"] is True


class TestBrownfield3Layers:
    def test_all_layers_present(self, tmp_path):
        """Brownfield project with all 3 layers."""
        proj = _setup_project(tmp_path, "brownfield")
        rapids = proj / ".aah"

        # Layer 1
        kdir = proj / "docs"
        kdir.mkdir()
        write_text("# Architecture\nMicroservices.\n", kdir / "architecture.md")

        # Layer 2 (brownfield path)
        write_text("# Codebase Structure\nExisting Node.js app\n", rapids / "brownfield" / "codebase-structure.md")

        # Layer 3
        intake = get_default_intake()
        set_problem_statement(intake, "Add search to existing app")
        set_project_type(intake, "enhancement")
        add_qa_round(intake, "start", [{"question": "Scope?", "answer": "New search module"}])
        save_intake(intake, rapids / "intake.json")

        context = load_impl_context(proj)

        assert context.get("knowledge_context"), "Layer 1 should be present"
        assert context.get("brownfield_summary"), "Layer 2 should be present"
        assert context.get("intake_summary"), "Layer 3 should be present"
        assert "search" in context.get("intake_summary", "")


class TestSessionKnowledgeE2E:
    def test_session_history_in_context(self, tmp_path):
        """Session history should be re-injected into context."""
        proj = _setup_project(tmp_path, "greenfield")
        rapids = proj / ".aah"

        progress = get_default_progress()
        progress["current_phase"] = "build"
        progress["session_history"] = [
            {
                "timestamp": "2026-04-15T10:00:00Z",
                "summary": "Completed F001, fixed auth bug",
                "decisions_made": ["Used JWT tokens"],
                "patterns_discovered": ["Tests need env vars set"],
            },
        ]
        save_progress(progress, rapids / "claude-progress.json")

        context = load_impl_context(proj)
        summary = context["status_summary"]

        assert "Session Intelligence" in summary
        assert "JWT tokens" in summary
        assert "env vars" in summary
