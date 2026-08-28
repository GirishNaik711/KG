"""Tests for aah.core.guards.dependency_policy_guard.

Phase 3 L5-A (#247). Guard rejects ``npm install`` / ``pip install`` /
similar commands that violate the project's policy file at
``.aah/dependency-policy.json``. PartsPulse #1-2 (MUI v5 + icons-v9
split) is the regression case: installing ``@mui/icons-material@^9``
into a project with policy ``"@mui/*": "^5"`` must exit 2.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from aah.core.common.io_utils import write_json
from aah.core.common.manifest import get_default_manifest, save_manifest


@pytest.fixture
def project_with_policy(tmp_path):
    """A temp project with a .aah dir, manifest, and a sample policy."""
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    (aah_root / "build").mkdir()
    manifest = get_default_manifest("dep-policy-test")
    save_manifest(manifest, aah_root / "manifest.yaml")
    (aah_root / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))
    write_json(
        {
            "npm": {
                "@mui/*": "^5",
                "react": "^18",
            },
            "pip": {
                "fastapi": "^1",
                "pydantic": "^2",
            },
        },
        aah_root / "dependency-policy.json",
    )
    return tmp_path


def _run_guard(
    command: str,
    cwd: Path,
    *,
    extra_env: dict | None = None,
    tool_name: str = "Bash",
) -> tuple[int, str]:
    """Invoke the guard as a subprocess. Returns (exit_code, stderr)."""
    proc_env = {**os.environ}
    proc_env.pop("AAH_CONTEXT", None)
    if extra_env:
        proc_env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.guards.dependency_policy_guard"],
        input=json.dumps({
            "tool_name": tool_name,
            "tool_input": {"command": command},
        }),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=proc_env,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# TestPatternDetection — each ecosystem's install pattern is recognized
# ---------------------------------------------------------------------------


class TestPatternDetection:
    def test_npm_install_violation_blocks(self, project_with_policy):
        code, stderr = _run_guard(
            "npm install @mui/icons-material@^9",
            cwd=project_with_policy,
        )
        assert code == 2
        assert "@mui/icons-material" in stderr
        assert "violation" in stderr.lower() or "blocked" in stderr.lower()

    def test_pip_install_violation_blocks(self, project_with_policy):
        # Policy pins fastapi to ^1; installing major 0 must block.
        code, stderr = _run_guard(
            "pip install fastapi==0.95.0",
            cwd=project_with_policy,
        )
        assert code == 2
        assert "fastapi" in stderr

    def test_pnpm_add_violation_blocks(self, project_with_policy):
        code, stderr = _run_guard(
            "pnpm add @mui/lab@^9",
            cwd=project_with_policy,
        )
        assert code == 2

    def test_yarn_add_violation_blocks(self, project_with_policy):
        code, stderr = _run_guard(
            "yarn add react@^17",
            cwd=project_with_policy,
        )
        assert code == 2
        assert "react" in stderr


# ---------------------------------------------------------------------------
# TestPolicyEnforcement
# ---------------------------------------------------------------------------


class TestPolicyEnforcement:
    def test_compatible_install_allows(self, project_with_policy):
        code, stderr = _run_guard(
            "npm install @mui/icons-material@^5.4.2",
            cwd=project_with_policy,
        )
        assert code == 0, f"compatible install should be allowed; stderr={stderr!r}"

    def test_install_without_version_allows(self, project_with_policy):
        """A bare `npm install react` (no version pin) should pass —
        we let the lockfile + deps_consistency_check enforce."""
        code, _ = _run_guard("npm install react", cwd=project_with_policy)
        assert code == 0

    def test_unrelated_package_allows(self, project_with_policy):
        """Package not in the policy → no rule to violate."""
        code, _ = _run_guard("npm install lodash@^4", cwd=project_with_policy)
        assert code == 0

    def test_missing_policy_file_allows(self, tmp_path):
        """Greenfield project with no policy file → guard is a no-op."""
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        manifest = get_default_manifest("no-policy")
        save_manifest(manifest, aah_root / "manifest.yaml")
        # No dependency-policy.json — guard must allow.
        code, _ = _run_guard(
            "npm install @mui/icons-material@^9",
            cwd=tmp_path,
        )
        assert code == 0

    def test_bypass_env_var_allows(self, project_with_policy):
        """AAH_DEPENDENCY_POLICY_BYPASS=1 short-circuits the guard."""
        code, _ = _run_guard(
            "npm install @mui/icons-material@^9",
            cwd=project_with_policy,
            extra_env={"AAH_DEPENDENCY_POLICY_BYPASS": "1"},
        )
        assert code == 0


# ---------------------------------------------------------------------------
# TestNonInstallCommands — non-install Bash commands must pass through
# ---------------------------------------------------------------------------


class TestNonInstallCommands:
    def test_ls_passes_through(self, project_with_policy):
        code, _ = _run_guard("ls -la", cwd=project_with_policy)
        assert code == 0

    def test_npm_run_passes_through(self, project_with_policy):
        """`npm run build` is not an install — must NOT match install patterns."""
        code, _ = _run_guard("npm run build", cwd=project_with_policy)
        assert code == 0

    def test_pip_show_passes_through(self, project_with_policy):
        code, _ = _run_guard("pip show fastapi", cwd=project_with_policy)
        assert code == 0


# ---------------------------------------------------------------------------
# TestMalformedInput — guard must be robust
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_empty_stdin_allows(self, project_with_policy):
        proc = subprocess.run(
            [sys.executable, "-m", "aah.core.guards.dependency_policy_guard"],
            input="",
            capture_output=True,
            text=True,
            cwd=str(project_with_policy),
        )
        assert proc.returncode == 0

    def test_non_bash_tool_allows(self, project_with_policy):
        code, _ = _run_guard(
            "irrelevant",
            cwd=project_with_policy,
            tool_name="Write",
        )
        assert code == 0


# ---------------------------------------------------------------------------
# TestProjectRootDiscovery — guard finds the policy via .aah ancestor
# ---------------------------------------------------------------------------


class TestProjectRootDiscovery:
    def test_finds_policy_from_subdir(self, project_with_policy):
        """Run the guard from a subdirectory of the project; it must
        still find .aah/dependency-policy.json."""
        subdir = project_with_policy / "src" / "components"
        subdir.mkdir(parents=True)
        code, stderr = _run_guard(
            "npm install @mui/icons-material@^9",
            cwd=subdir,
        )
        assert code == 2, (
            f"guard must walk up to find .aah; got {code}, stderr={stderr!r}"
        )

    def test_no_rapids_ancestor_allows(self):
        """When run outside any AAH project, the guard is a no-op."""
        with tempfile.TemporaryDirectory(prefix="dep-pol-no-aah-") as td:
            code, _ = _run_guard(
                "npm install @mui/icons-material@^9",
                cwd=Path(td),
            )
            assert code == 0
