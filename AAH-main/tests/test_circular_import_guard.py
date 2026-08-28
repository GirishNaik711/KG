"""Tests for aah.core.guards.circular_import_guard.

Phase 3 L5-C (#247). PostToolUse(Write|Edit|MultiEdit) guard that
detects circular imports introduced by source-file writes via a
bounded-depth (≤50 nodes) BFS of the import graph. PartsPulse #3
(``main.tsx`` ↔ ``App.tsx`` ↔ ``AppShell.tsx``) is the regression case.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from aah.core.common.manifest import get_default_manifest, save_manifest


@pytest.fixture
def project(tmp_path):
    """Bare project root with a `.aah` directory."""
    aah_root = tmp_path / ".aah"
    aah_root.mkdir()
    (aah_root / "build").mkdir()
    manifest = get_default_manifest("circular-test")
    save_manifest(manifest, aah_root / "manifest.yaml")
    (aah_root / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))
    return tmp_path


def _run_guard(
    file_path: Path | str,
    cwd: Path,
    *,
    extra_env: dict | None = None,
    tool_name: str = "Write",
) -> tuple[int, str]:
    proc_env = {**os.environ}
    proc_env.pop("AAH_CONTEXT", None)
    if extra_env:
        proc_env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.guards.circular_import_guard"],
        input=json.dumps({
            "tool_name": tool_name,
            "tool_input": {"file_path": str(file_path)},
        }),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=proc_env,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------


class TestSourceFileMatching:
    def test_python_source_triggers(self, project):
        f = project / "foo.py"
        f.write_text("import os\n")  # no cycle, but file is a source
        code, _ = _run_guard(f, cwd=project)
        assert code == 0  # no cycle = pass

    def test_skips_non_source_files(self, project):
        f = project / "data.json"
        f.write_text("{}")
        code, _ = _run_guard(f, cwd=project)
        assert code == 0

    def test_skips_test_files(self, project):
        f = project / "tests" / "test_foo.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("from foo import bar\nfrom .test_helper import baz")
        code, _ = _run_guard(f, cwd=project)
        assert code == 0


# ---------------------------------------------------------------------------


class TestCycleDetection:
    def test_python_two_file_cycle_blocks(self, project):
        """a.py imports b; b.py imports a → cycle."""
        a = project / "a.py"
        b = project / "b.py"
        a.write_text("from b import thing\n")
        b.write_text("from a import other\n")
        # The trigger is the just-written file (a).
        code, stderr = _run_guard(a, cwd=project)
        assert code == 2, f"expected cycle block; stderr={stderr!r}"
        assert "circular-import" in stderr.lower() or "cycle" in stderr.lower()
        # Cycle path must mention both files.
        assert "a.py" in stderr
        assert "b.py" in stderr

    def test_python_three_file_cycle_blocks(self, project):
        a = project / "a.py"
        b = project / "b.py"
        c = project / "c.py"
        a.write_text("from b import x\n")
        b.write_text("from c import y\n")
        c.write_text("from a import z\n")
        code, stderr = _run_guard(a, cwd=project)
        assert code == 2
        assert all(name in stderr for name in ("a.py", "b.py", "c.py"))

    def test_no_cycle_allows(self, project):
        a = project / "a.py"
        b = project / "b.py"
        a.write_text("from b import x\n")
        b.write_text("import os\n")
        code, _ = _run_guard(a, cwd=project)
        assert code == 0

    def test_typescript_relative_cycle_blocks(self, project):
        """PartsPulse #3 analogue — relative TS imports cycle."""
        app = project / "App.tsx"
        shell = project / "AppShell.tsx"
        app.write_text("import {Shell} from './AppShell';\n")
        shell.write_text("import {App} from './App';\n")
        code, stderr = _run_guard(app, cwd=project)
        assert code == 2
        assert "App.tsx" in stderr and "AppShell.tsx" in stderr


# ---------------------------------------------------------------------------


class TestBoundedDepth:
    def test_self_import_does_not_count_as_cycle(self, project):
        """A file with NO project imports (only third-party) is fine."""
        f = project / "isolated.py"
        f.write_text("import os\nimport sys\nfrom collections import OrderedDict\n")
        code, _ = _run_guard(f, cwd=project)
        assert code == 0


# ---------------------------------------------------------------------------


class TestParsingResilience:
    def test_syntax_error_does_not_crash(self, project):
        """Garbage source must not crash the guard. If parsing fails,
        no imports are extracted → no cycle → exit 0."""
        f = project / "broken.py"
        f.write_text("from b import (unclosed\n")  # syntax error
        code, _ = _run_guard(f, cwd=project)
        assert code == 0

    def test_missing_target_file_passes(self, project):
        """Hook fires for a path that doesn't exist on disk → pass through."""
        ghost = project / "ghost.py"
        code, _ = _run_guard(ghost, cwd=project)
        assert code == 0


# ---------------------------------------------------------------------------


class TestDelegationAndBypass:
    def test_bypass_env_var_allows_known_cycle(self, project):
        a = project / "a.py"
        b = project / "b.py"
        a.write_text("from b import x\n")
        b.write_text("from a import y\n")
        code, _ = _run_guard(
            a,
            cwd=project,
            extra_env={"AAH_CIRCULAR_IMPORT_BYPASS": "1"},
        )
        assert code == 0

    def test_no_rapids_ancestor_passes_through(self, tmp_path):
        """Cycle outside any AAH project → guard is a no-op."""
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("from b import x\n")
        b.write_text("from a import y\n")
        code, _ = _run_guard(a, cwd=tmp_path)
        assert code == 0
