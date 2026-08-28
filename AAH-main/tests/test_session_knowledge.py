"""Tests for session knowledge accumulation and re-injection (WS1.6)."""

from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json
from aah.core.common.progress import get_default_progress, save_progress
from aah.core.build.capture_subagent_learnings import (
    capture_learnings,
    get_accumulated_learnings,
)
from aah.core.build.load_impl_context import build_status_summary


class TestSessionKnowledgeReinjection:
    def _setup_project(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir(parents=True)
        (aah_root / "build").mkdir(parents=True)
        return tmp_path

    def test_status_summary_includes_session_history(self, tmp_path):
        proj = self._setup_project(tmp_path)
        progress = get_default_progress()
        progress["current_phase"] = "build"
        progress["session_history"] = [
            {
                "timestamp": "2026-04-15T10:00:00Z",
                "summary": "3 commits, F001 completed",
                "decisions_made": ["Chose event sourcing for audit"],
                "patterns_discovered": ["Tests require Docker"],
            },
            {
                "timestamp": "2026-04-16T10:00:00Z",
                "summary": "5 commits, F002 and F003 completed",
                "decisions_made": ["Used JWT for auth"],
                "patterns_discovered": [],
                "key_learnings": "Auth module uses custom middleware",
            },
        ]
        save_progress(progress, proj / ".aah" / "claude-progress.json")

        context = {
            "progress": progress,
            "manifest": {"project_name": "test"},
            "feature_summary": {"total": 0, "passing": 0},
        }
        summary = build_status_summary(context)

        assert "Session Intelligence" in summary
        assert "F001 completed" in summary
        assert "event sourcing" in summary
        assert "Docker" in summary
        assert "JWT" in summary
        assert "custom middleware" in summary

    def test_status_summary_no_session_history(self, tmp_path):
        proj = self._setup_project(tmp_path)
        progress = get_default_progress()
        context = {
            "progress": progress,
            "manifest": {"project_name": "test"},
            "feature_summary": {"total": 0, "passing": 0},
        }
        summary = build_status_summary(context)
        assert "Session Intelligence" not in summary

    def test_decisions_deduplicated(self, tmp_path):
        proj = self._setup_project(tmp_path)
        progress = get_default_progress()
        progress["session_history"] = [
            {"summary": "S1", "decisions_made": ["Use PostgreSQL"]},
            {"summary": "S2", "decisions_made": ["Use PostgreSQL", "Use Redis"]},
        ]

        context = {
            "progress": progress,
            "manifest": {"project_name": "test"},
            "feature_summary": {"total": 0, "passing": 0},
        }
        summary = build_status_summary(context)
        # Should appear only once despite being in 2 sessions
        assert summary.count("Use PostgreSQL") == 1


class TestSubagentLearnings:
    def _setup_project(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "build").mkdir(parents=True)
        return tmp_path

    def test_capture_empty(self, tmp_path):
        proj = self._setup_project(tmp_path)
        result = capture_learnings(proj, "test-agent")
        assert result["agent_name"] == "test-agent"
        assert result["patterns"] == []

    def test_accumulated_empty(self, tmp_path):
        proj = self._setup_project(tmp_path)
        result = get_accumulated_learnings(proj)
        assert result["patterns"] == []
        assert result["decisions"] == []

    def test_capture_and_accumulate(self, tmp_path):
        proj = self._setup_project(tmp_path)

        # Simulate two subagent captures
        capture_learnings(proj, "agent-1")
        capture_learnings(proj, "agent-2")

        result = get_accumulated_learnings(proj)
        assert isinstance(result["patterns"], list)
        assert isinstance(result["decisions"], list)

    def test_learnings_file_created(self, tmp_path):
        proj = self._setup_project(tmp_path)
        capture_learnings(proj, "test-agent")
        assert (proj / ".aah" / "build" / "subagent-learnings.json").exists()

    def test_cap_at_20_entries(self, tmp_path):
        proj = self._setup_project(tmp_path)
        for i in range(25):
            capture_learnings(proj, f"agent-{i}")

        from aah.core.common.io_utils import read_json
        data = read_json(proj / ".aah" / "build" / "subagent-learnings.json")
        assert len(data["entries"]) == 20


class TestSubagentLearningsInContext:
    def test_learnings_in_status_summary(self, tmp_path):
        aah_root = tmp_path / ".aah"
        (aah_root / "build").mkdir(parents=True)

        # Write learnings file directly
        write_json({
            "entries": [{
                "agent_name": "agent-1",
                "patterns": ["pattern: Always use uv for dependencies"],
                "decisions": ["decision: Use SQLAlchemy ORM"],
                "conventions": ["fix: Import order matters in __init__.py"],
            }],
        }, aah_root / "build" / "subagent-learnings.json")

        context = {
            "progress": get_default_progress(),
            "manifest": {"project_name": "test"},
            "feature_summary": {"total": 0, "passing": 0},
            "subagent_learnings": get_accumulated_learnings(tmp_path),
        }
        summary = build_status_summary(context)

        assert "Subagent Learnings" in summary
        assert "uv for dependencies" in summary
        assert "SQLAlchemy" in summary
