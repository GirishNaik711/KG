"""Tests for the wave-free, whole-codebase standards gate (``run-project``).

The defect these pin: the gate resolved ONE adapter for the project, so on the
standard fullstack layout (``backend/pyproject.toml`` + ``frontend/package.json``)
it ran ``ruff check backend`` and eslint never ran — a PASS that had never looked
at the frontend.

NO MOCKS: a real Git-backed project under ``tmp_path``, the real CLI, the real
attested writer. The linters themselves may or may not be installed in the
environment, so these assert on SCOPE and BOOKKEEPING (which packages were
examined, which commands were recorded, that the subject stayed clean) rather
than on a particular verdict.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.build.quality_checks import (
    build_standards_artifact,
    project_standards_artifact,
    run_project_standards,
)
from aah.core.common.io_utils import read_json
from tests.support.aah_project import AAHProjectBuilder


def _fullstack(tmp_path: Path) -> AAHProjectBuilder:
    b = AAHProjectBuilder.create(tmp_path, git=True, branch="build/p")
    b.git("config", "core.autocrlf", "false")
    b.git("config", "core.safecrlf", "false")
    b.manifest(project_name="p", stack_choices={"primary": "python-fastapi"}).secret()
    b.file("backend/pyproject.toml", "[project]\nname = 'backend'\nversion = '0.1.0'\n")
    b.file("backend/app.py", "VALUE = 1\n")
    b.file("frontend/package.json", '{"name": "frontend", "private": true}\n')
    b.file("frontend/eslint.config.mjs", "export default [];\n")
    b.file("frontend/index.js", "export const value = 1;\n")
    b.file(".gitignore", ".venv/\nnode_modules/\n")
    b.commit("seed fullstack project")
    return b


class TestScope:
    def test_both_package_roots_are_examined(self, tmp_path):
        b = _fullstack(tmp_path)
        summary = run_project_standards(b.path)

        assert [(s["root"], s["stack"]) for s in summary["scopes"]] == [
            ("backend", "python"), ("frontend", "node"),
        ]

    def test_commands_are_recorded_for_every_package(self, tmp_path):
        """The multi-target fold records ``details.commands`` (a list), so a
        reader of only ``details.command`` recorded null for every real run."""
        b = _fullstack(tmp_path)
        summary = run_project_standards(b.path)

        for check in ("linting", "static_analysis"):
            commands = summary["commands"][check]
            assert commands, f"{check} recorded no command"
            assert len(commands) == 2, commands

    def test_tree_without_any_package_manifest_still_renders_a_verdict(self, tmp_path):
        b = AAHProjectBuilder.create(tmp_path, git=True, branch="build/p")
        b.manifest(project_name="p").secret()
        b.file("notes.txt", "no packages here\n").commit("seed")

        summary = run_project_standards(b.path)
        assert len(summary["scopes"]) == 1
        assert summary["scopes"][0]["root"] == "."
        assert summary["status"] in {"pass", "fail", "no_signal"}


class TestArtifact:
    def test_wave_free_run_writes_standards_latest(self, tmp_path):
        b = _fullstack(tmp_path)
        proc = b.run_module(
            "aah.core.build.quality_checks", "run-project",
            "--project-path", str(b.path), "--subject-branch", "build/p",
        )
        assert proc.returncode in (0, 2), proc.stderr

        artifact = build_standards_artifact(b.aah)
        assert artifact.is_file(), proc.stderr
        assert artifact.name == "standards-latest.json"
        assert not project_standards_artifact(b.aah, 1).exists()

        payload = read_json(artifact)
        assert payload["wave"] is None
        assert [s["root"] for s in payload["scopes"]] == ["backend", "frontend"]
        assert payload["subject"]["branch"] == "build/p"

    def test_wave_argument_keeps_the_legacy_path(self, tmp_path):
        b = _fullstack(tmp_path)
        proc = b.run_module(
            "aah.core.build.quality_checks", "run-project",
            "--project-path", str(b.path), "--wave", "3",
        )
        assert proc.returncode in (0, 2), proc.stderr

        assert project_standards_artifact(b.aah, 3).is_file()
        assert not build_standards_artifact(b.aah).exists()

    def test_gate_is_read_only_on_the_subject(self, tmp_path):
        """The run sits between capture_subject and assert_subject_unchanged, so
        anything it wrote into the tree would invalidate its own evidence."""
        b = _fullstack(tmp_path)
        before = b.git("rev-parse", "HEAD")

        proc = b.run_module(
            "aah.core.build.quality_checks", "run-project",
            "--project-path", str(b.path), "--subject-branch", "build/p",
        )
        assert proc.returncode in (0, 2), proc.stderr

        assert b.git("rev-parse", "HEAD") == before
        dirt = [
            line for line in b.git("status", "--porcelain").splitlines()
            if not line.split(maxsplit=1)[-1].startswith(".aah/")
        ]
        assert dirt == [], dirt
