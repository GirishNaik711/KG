"""Tests for aah.core.intel.context_dashboard."""

from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json, write_text
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.intel.context_dashboard import (
    build_dashboard,
    get_layer1_status,
    get_layer2_status,
    get_layer3_status,
    render_dashboard_markdown,
)


class TestLayer1Knowledge:
    def test_no_knowledge_dir(self, tmp_path):
        result = get_layer1_status(tmp_path)
        assert result["found"] is False
        assert result["document_count"] == 0

    def test_with_knowledge_dir(self, tmp_path):
        kdir = tmp_path / "knowledge"
        kdir.mkdir()
        write_text("# API Design", kdir / "api-spec.md")
        write_text("# Requirements", kdir / "requirements.md")

        result = get_layer1_status(tmp_path)
        assert result["found"] is True
        assert result["document_count"] == 2
        assert result["folder"] == "knowledge"


class TestLayer2CodebaseIntel:
    def test_no_intel(self, tmp_path):
        result = get_layer2_status(tmp_path)
        assert result["found"] is False
        assert result["artifacts"] == []

    def test_brownfield_intel(self, tmp_path):
        bf = tmp_path / ".aah" / "brownfield"
        bf.mkdir(parents=True)
        write_text("# Structure", bf / "codebase-structure.md")
        write_text("# Learning", bf / "codebase-learning.md")

        result = get_layer2_status(tmp_path)
        assert result["found"] is True
        assert len(result["artifacts"]) == 2

    def test_codebase_intel_dir(self, tmp_path):
        ci = tmp_path / ".aah" / "codebase-intel"
        ci.mkdir(parents=True)
        write_json({"files": []}, ci / "codebase-profile.json")

        result = get_layer2_status(tmp_path)
        assert result["found"] is True
        assert any(a["name"] == "codebase-profile.json" for a in result["artifacts"])

    def test_staleness_included(self, tmp_path):
        bf = tmp_path / ".aah" / "brownfield"
        bf.mkdir(parents=True)
        write_text("# Structure", bf / "codebase-structure.md")

        result = get_layer2_status(tmp_path)
        assert "staleness" in result
        assert result["staleness"]["has_intel"] is True


class TestLayer3Session:
    def test_default_progress(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir(parents=True)
        progress = get_default_progress()
        save_progress(progress, aah_root / "claude-progress.json")

        result = get_layer3_status(tmp_path)
        assert result["phase"] == "init"
        assert result["session_count"] == 0

    def test_with_progress(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir(parents=True)
        progress = get_default_progress()
        progress["current_phase"] = "build"
        progress["current_wave"] = 2
        progress["session_history"] = [
            {"summary": "Session 1", "decisions_made": ["Used event sourcing"]},
            {"summary": "Session 2", "patterns_discovered": ["Tests need Docker"]},
        ]
        save_progress(progress, aah_root / "claude-progress.json")

        result = get_layer3_status(tmp_path)
        assert result["phase"] == "build"
        assert result["wave"] == 2
        assert result["session_count"] == 2
        assert result["accumulated_decisions"] == 1
        assert result["accumulated_patterns"] == 1


class TestBuildDashboard:
    def test_full_dashboard(self, tmp_path):
        # Set up all 3 layers
        aah_root = tmp_path / ".aah"
        aah_root.mkdir(parents=True)

        # Layer 1
        kdir = tmp_path / "knowledge"
        kdir.mkdir()
        write_text("# Spec", kdir / "spec.md")

        # Layer 2
        bf = aah_root / "brownfield"
        bf.mkdir()
        write_text("# Structure", bf / "codebase-structure.md")

        # Layer 3
        progress = get_default_progress()
        progress["current_phase"] = "plan"
        save_progress(progress, aah_root / "claude-progress.json")

        dashboard = build_dashboard(tmp_path)
        assert dashboard["layer1_knowledge"]["found"] is True
        assert dashboard["layer2_codebase_intel"]["found"] is True
        assert dashboard["layer3_session"]["phase"] == "plan"


class TestRenderMarkdown:
    def test_renders_all_layers(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir(parents=True)

        bf = aah_root / "brownfield"
        bf.mkdir()
        write_text("# Structure", bf / "codebase-structure.md")

        progress = get_default_progress()
        save_progress(progress, aah_root / "claude-progress.json")

        dashboard = build_dashboard(tmp_path)
        md = render_dashboard_markdown(dashboard)

        assert "Layer 1" in md
        assert "Layer 2" in md
        assert "Layer 3" in md
        assert "codebase-structure.md" in md

    def test_empty_project(self, tmp_path):
        dashboard = build_dashboard(tmp_path)
        md = render_dashboard_markdown(dashboard)
        assert "Not found" in md
        assert "No codebase intelligence found" in md
