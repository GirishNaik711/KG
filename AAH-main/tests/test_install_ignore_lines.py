"""Tests for adapter-provided project .gitignore lines (folder=project install)."""

from pathlib import Path

from aah.core.install import registry
from aah.core.install.registry import get, all_project_ignore_lines


class TestPerAdapterIgnoreLines:
    def test_claude_covers_claude_dir(self):
        lines = get("claude").project_ignore_lines(Path("/proj"))
        assert all(l.startswith(".claude/") for l in lines)
        assert ".claude/settings.local.json" in lines
        assert any(l.startswith(".claude/skills/") for l in lines)
        assert any(l.startswith(".claude/agents/") for l in lines)

    def test_codex_covers_agents_and_codex_dirs(self):
        lines = get("codex").project_ignore_lines(Path("/proj"))
        assert any(l.startswith(".agents/skills/") for l in lines)
        assert ".codex/hooks.json" in lines
        # No .claude entries from the codex adapter.
        assert not any(l.startswith(".claude/") for l in lines)

    def test_lines_are_relative_forward_slash(self):
        for platform in registry.names():
            for line in get(platform).project_ignore_lines(Path("/proj")):
                assert not line.startswith("/proj"), "must be project-relative"
                assert "\\" not in line, "must use forward slashes"


class TestAllProjectIgnoreLines:
    def test_unions_every_platform(self):
        lines = all_project_ignore_lines(Path("/proj"))
        assert any(l.startswith(".claude/") for l in lines)     # claude
        assert any(l.startswith(".agents/skills/") for l in lines)  # codex

    def test_no_duplicates(self):
        lines = all_project_ignore_lines(Path("/proj"))
        assert len(lines) == len(set(lines))

    def test_stable_and_nonempty(self):
        a = all_project_ignore_lines(Path("/proj"))
        b = all_project_ignore_lines(Path("/proj"))
        assert a == b and len(a) > 0
