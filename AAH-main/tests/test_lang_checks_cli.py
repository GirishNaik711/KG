"""Phase 3 L2 CLI tests + cutover integration.

The aah-runtime-validator subagent shells out to the lang_checks CLI. These
tests pin the CLI's contract — JSON schema, exit codes, --kind dispatch.
The integration tests at the bottom verify the cutover of
run_feature_tests.find_test_command and quality_checks._resolve_*
preserved behavior for Python and Node.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Subprocess invoke the lang_checks CLI."""
    proc_env = {**os.environ}
    proc_env.pop("AAH_CONTEXT", None)
    if env_extra:
        proc_env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "aah.core.build.lang_checks.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=proc_env,
    )


@pytest.fixture
def python_project(tmp_path):
    """Minimal AAH project that PythonAdapter detects."""
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    (aah_root / "manifest.yaml").write_text(
        "project_name: t\nproject_type: greenfield\n"
        "stack_choices:\n  primary: python-fastapi\n"
    )
    (tmp_path / "pyproject.toml").write_text("")  # PythonAdapter file fingerprint
    (tmp_path / "tests").mkdir()  # test dirs are DISCOVERED, so one must exist
    return tmp_path


@pytest.fixture
def node_project(tmp_path):
    """Minimal AAH project that NodeAdapter detects."""
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    (aah_root / "manifest.yaml").write_text(
        "project_name: t\nproject_type: greenfield\n"
        "stack_choices:\n  primary: node-react\n"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "t", "scripts": {"lint": "eslint .", "build": "vite build"}})
    )
    return tmp_path


# ---------------------------------------------------------------------------
# CLI output shape
# ---------------------------------------------------------------------------


class TestCliDescribe:
    def test_python_test_command_shape(self, python_project):
        proc = _run_cli(
            "describe", "--kind", "test", "--project-path", str(python_project),
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        assert data["kind"] == "test"
        assert data["adapter"] == "python"
        assert data["supported"] is True
        # pyproject.toml at the root and a tests/ dir on disk: deps resolve via
        # --project (R2), the discovered path is passed (R1), and the runner is
        # `python -m pytest` so cwd is on sys.path (F').
        assert data["command"]["argv"] == [
            "uv", "run", "--python", "3.12", "--project", ".", "--with", "pytest",
            "python", "-m", "pytest", "tests/", "-v", "--tb=short",
        ]
        assert data["command"]["timeout_sec"] == 300

        (python_project / ".aah" / "manifest.yaml").write_text(
            "project_name: t\nproject_type: greenfield\n"
            "stack_choices:\n  primary: python-fastapi\n"
            "  test_command: python -m pytest backend/tests -q\n"
        )
        overridden = _run_cli(
            "describe", "--kind", "test", "--project-path", str(python_project),
        )
        assert overridden.returncode == 0, overridden.stderr
        override_data = json.loads(overridden.stdout)
        assert override_data["command"]["argv"] == [
            "python", "-m", "pytest", "backend/tests", "-q",
        ]

    def test_test_command_with_feature_filter(self, python_project):
        proc = _run_cli(
            "describe", "--kind", "test",
            "--feature-filter", "F042",
            "--project-path", str(python_project),
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert data["command"]["argv"][-2:] == ["-k", "F042"]

    def test_node_lint_command(self, node_project):
        proc = _run_cli(
            "describe", "--kind", "lint", "--project-path", str(node_project),
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert data["adapter"] == "node"
        assert data["supported"] is True
        # node_project has scripts.lint, so npm run lint is preferred.
        assert data["command"]["argv"] == ["npm", "run", "lint"]

    def test_unsupported_kind_returns_supported_false(self, python_project):
        # PythonAdapter doesn't override `build` (Python has no build step
        # by default). Should return supported=false, command=null.
        proc = _run_cli(
            "describe", "--kind", "build", "--project-path", str(python_project),
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert data["supported"] is False
        assert data["command"] is None

    def test_cache_clean_returns_commands_list(self, python_project):
        proc = _run_cli(
            "describe", "--kind", "cache_clean",
            "--project-path", str(python_project),
        )
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert "commands" in data
        assert isinstance(data["commands"], list)
        assert len(data["commands"]) >= 1
        # The Python __pycache__ clean command.
        assert any("find" in c["argv"] for c in data["commands"])


class TestCliErrorPaths:
    def test_bad_kind_exits_2(self, python_project):
        proc = _run_cli(
            "describe", "--kind", "nonexistent",
            "--project-path", str(python_project),
        )
        assert proc.returncode == 2
        # argparse prints the choices in the error.
        assert "test" in proc.stderr or "test" in proc.stdout

    def test_missing_project_path_errors(self, tmp_path):
        # Run from a tmp dir that has no .aah/ above it.
        proc_env = {**os.environ}
        proc_env.pop("AAH_CONTEXT", None)
        proc = subprocess.run(
            [sys.executable, "-m", "aah.core.build.lang_checks.cli",
             "describe", "--kind", "test"],
            capture_output=True, text=True,
            cwd=str(tmp_path),  # cwd has no project
            env=proc_env,
        )
        # Either exit 2 (no project resolvable) or exit 0 with adapter=generic
        # (resolve_project_path resolved to tmp_path itself, generic adapter,
        # supported=False). Both are acceptable shapes of "no real project."
        assert proc.returncode in (0, 2)
        if proc.returncode == 2:
            assert "could not resolve" in proc.stderr.lower()
        else:
            data = json.loads(proc.stdout)
            assert data["supported"] is False or data["adapter"] == "generic"


# ---------------------------------------------------------------------------
# Cutover integration — behavior preservation
# ---------------------------------------------------------------------------


class TestCutoverFindTestCommand:
    """Confirm find_test_command produces semantically-equivalent
    commands to the pre-cutover code for Python and Node projects."""

    def _seed_init_sh(self, tmp_path: Path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        (aah_root / "init.sh").write_text("#!/bin/bash\nexit 0\n")
        return aah_root

    def test_python_with_filter(self, tmp_path):
        aah_root = self._seed_init_sh(tmp_path)
        (aah_root / "manifest.yaml").write_text(
            "stack_choices:\n  primary: python-fastapi\n"
        )
        (tmp_path / "tests").mkdir()  # test dirs are DISCOVERED, so one must exist
        from aah.core.build.run_feature_tests import find_test_command
        cmd = find_test_command({"id": "F001"}, tmp_path)
        # Old: "uv run pytest tests/ -k F001 -v --tb=short"
        # New: "uv run ... python -m pytest tests/ -v --tb=short -k F001"
        # `python -m pytest` puts cwd on sys.path; the path is discovered.
        assert cmd.startswith("uv run")
        assert "python -m pytest tests/" in cmd
        assert "-k F001" in cmd
        assert "-v" in cmd
        assert "--tb=short" in cmd

    def test_python_no_junitxml_in_command(self, tmp_path):
        """JUnit is now configured via PYTEST_ADDOPTS, not command injection."""
        aah_root = self._seed_init_sh(tmp_path)
        (aah_root / "manifest.yaml").write_text(
            "stack_choices:\n  primary: python-fastapi\n"
        )
        from aah.core.build.run_feature_tests import find_test_command
        cmd = find_test_command({"id": "F002"}, tmp_path)
        # JUnit is injected via env, not the command string.
        assert "--junitxml" not in cmd

    def test_node_with_filter(self, tmp_path):
        aah_root = self._seed_init_sh(tmp_path)
        (aah_root / "manifest.yaml").write_text(
            "stack_choices:\n  primary: react-typescript\n"
        )
        from aah.core.build.run_feature_tests import find_test_command
        cmd = find_test_command({"id": "F001"}, tmp_path)
        assert "npm test" in cmd
        assert "--grep F001" in cmd

    def test_explicit_override_preserved(self, tmp_path):
        from aah.core.build.run_feature_tests import find_test_command
        cmd = find_test_command(
            {"id": "F001", "test_config": {"command": "pytest tests/foo"}},
            tmp_path,
        )
        # Explicit override path is unchanged by cutover.
        assert cmd == "pytest tests/foo"

    def test_no_init_sh_returns_none(self, tmp_path):
        from aah.core.build.run_feature_tests import find_test_command
        # No init.sh means we don't auto-resolve a default — preserved.
        cmd = find_test_command({"id": "F001"}, tmp_path)
        assert cmd is None

    def test_unrecognized_stack_with_init_sh_returns_none(self, tmp_path):
        aah_root = self._seed_init_sh(tmp_path)
        (aah_root / "manifest.yaml").write_text("stack_choices: {}\n")
        from aah.core.build.run_feature_tests import find_test_command
        # GenericAdapter returns None from test_command -> find_test_command returns None.
        cmd = find_test_command({"id": "F001"}, tmp_path)
        assert cmd is None


class TestCutoverQualityChecks:
    """Confirm _resolve_linter / _resolve_static_analyzer /
    _resolve_coverage_command return shell-strings via the adapter pack."""

    def test_python_linter_via_adapter(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        from aah.core.build.quality_checks import _resolve_linter
        cmd = _resolve_linter(tmp_path)
        assert cmd is not None
        assert "ruff" in cmd
        assert "--with ruff" in cmd

    def test_node_linter_via_adapter(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "t", "scripts": {"lint": "eslint ."}})
        )
        from aah.core.build.quality_checks import _resolve_linter
        cmd = _resolve_linter(tmp_path)
        assert cmd is not None
        assert "npm run lint" in cmd

    def test_unrecognized_returns_none(self, tmp_path):
        from aah.core.build.quality_checks import _resolve_linter
        # No language fingerprints — GenericAdapter returns None for lint.
        assert _resolve_linter(tmp_path) is None
