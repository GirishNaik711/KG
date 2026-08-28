"""Tests for aah.core.guards.package_existence_guard.

Deterministic, offline gate: a batch install (`pip install -r`)
whose declared names all resolve in the committed lockfile passes; a name
absent from the lock fails. No network, no adjudication.

Functional, NO MOCKS — the guard runs as a real subprocess against real
lockfile fixtures built in tmp dirs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from aah.core.common.manifest import get_default_manifest, save_manifest

# Repo root so the spawned guard subprocess can import `aah` even when
# sys.executable is a bare interpreter without the package installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_project(tmp_path, *, lock_names=("requests", "pyyaml"), with_lock=True):
    """A temp project with a .aah dir, manifest, and (optionally) a uv.lock
    listing the given package names."""
    rapids = tmp_path / ".aah"
    rapids.mkdir()
    save_manifest(get_default_manifest("pkg-existence-test"), rapids / "manifest.yaml")
    if with_lock:
        body = "version = 1\n\n" + "".join(
            f'[[package]]\nname = "{n}"\nversion = "1.0.0"\n\n' for n in lock_names
        )
        (tmp_path / "uv.lock").write_text(body)
    return tmp_path


def _run_guard(command, cwd, *, extra_env=None, tool_name="Bash"):
    """Invoke the guard as a subprocess. Returns (exit_code, stderr)."""
    proc_env = {**os.environ}
    proc_env.pop("RAPIDS_CONTEXT", None)
    existing = proc_env.get("PYTHONPATH", "")
    proc_env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    )
    if extra_env:
        proc_env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-m", "aah.core.guards.package_existence_guard"],
        input=json.dumps({"tool_name": tool_name, "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=proc_env,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# AC1 — a name present in the committed lockfile passes
# ---------------------------------------------------------------------------


def test_ac1_resolvable_reqfile_passes(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests", "pyyaml"))
    (proj / "requirements.txt").write_text("requests==2.31.0\npyyaml>=6\n")
    code, stderr = _run_guard("pip install -r requirements.txt", cwd=proj)
    assert code == 0, f"resolvable install should pass; stderr={stderr!r}"


def test_ac1_pep503_normalization(tmp_path):
    """Lock has `ruamel-yaml`; reqfile declares `ruamel.yaml` — PEP 503 makes
    these the same name, so it resolves."""
    proj = _make_project(tmp_path, lock_names=("ruamel-yaml",))
    (proj / "requirements.txt").write_text("ruamel.yaml==0.18\n")
    code, _ = _run_guard("pip install -r requirements.txt", cwd=proj)
    assert code == 0


# ---------------------------------------------------------------------------
# AC2 — a name absent from the lockfile blocks deterministically
# ---------------------------------------------------------------------------


def test_ac2_hallucinated_name_blocks(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    (proj / "requirements.txt").write_text("reqeusts==2.31.0\n")  # typo/slopsquat
    code, stderr = _run_guard("pip install -r requirements.txt", cwd=proj)
    assert code == 2
    assert "reqeusts" in stderr
    assert "lockfile" in stderr.lower()


def test_ac2_mixed_resolvable_and_absent_blocks(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    (proj / "requirements.txt").write_text("requests==2.31.0\nfaketotallynotreal\n")
    code, stderr = _run_guard("pip install -r requirements.txt", cwd=proj)
    assert code == 2
    assert "faketotallynotreal" in stderr


# ---------------------------------------------------------------------------
# AC3 — no network (non-negotiable): the guard imports no network machinery
# ---------------------------------------------------------------------------


def test_ac3_no_network_imports():
    src = Path(
        "aah/core/guards/package_existence_guard.py"
    ).read_text(encoding="utf-8")
    for tok in ("urllib", "socket", "http.client", "requests.get", "urlopen"):
        assert tok not in src, f"network token {tok!r} must not appear in the guard"


def test_ac3_blocks_without_network(tmp_path):
    """The block path completes with no outbound call — proven by it returning
    the deterministic exit 2 offline (the subprocess makes no network setup)."""
    proj = _make_project(tmp_path, lock_names=("requests",))
    (proj / "requirements.txt").write_text("definitelynotapackage123\n")
    code, _ = _run_guard("pip install -r requirements.txt", cwd=proj)
    assert code == 2


# ---------------------------------------------------------------------------
# AC4 — the intentional-add path is NOT blocked
# ---------------------------------------------------------------------------


def test_ac4_bare_pip_install_passes(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    code, _ = _run_guard("pip install brandnewpkg", cwd=proj)
    assert code == 0


def test_ac4_uv_add_passes(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    code, _ = _run_guard("uv add brandnewpkg", cwd=proj)
    assert code == 0


def test_ac4_npm_ci_lockfile_native_passes(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    code, _ = _run_guard("npm ci", cwd=proj)
    assert code == 0


def test_ac4_uv_sync_passes(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    code, _ = _run_guard("uv sync", cwd=proj)
    assert code == 0


# ---------------------------------------------------------------------------
# No-op / robustness cases (mirror dependency_policy_guard suite)
# ---------------------------------------------------------------------------


def test_no_lockfile_allows(tmp_path):
    """No committed uv.lock → nothing to resolve against → guard no-ops, even
    for a name that would otherwise look hallucinated."""
    proj = _make_project(tmp_path, with_lock=False)
    (proj / "requirements.txt").write_text("reqeusts\n")
    code, _ = _run_guard("pip install -r requirements.txt", cwd=proj)
    assert code == 0


def test_ls_passes_through(tmp_path):
    proj = _make_project(tmp_path)
    code, _ = _run_guard("ls -la", cwd=proj)
    assert code == 0


def test_bypass_env_allows(tmp_path):
    proj = _make_project(tmp_path, lock_names=("requests",))
    (proj / "requirements.txt").write_text("reqeusts\n")
    code, _ = _run_guard(
        "pip install -r requirements.txt",
        cwd=proj,
        extra_env={"RAPIDS_PACKAGE_EXISTENCE_BYPASS": "1"},
    )
    assert code == 0


def test_non_bash_tool_allows(tmp_path):
    proj = _make_project(tmp_path)
    code, _ = _run_guard("irrelevant", cwd=proj, tool_name="Write")
    assert code == 0


def test_empty_stdin_allows(tmp_path):
    proj = _make_project(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    p = subprocess.run(
        [sys.executable, "-m", "aah.core.guards.package_existence_guard"],
        input="",
        capture_output=True,
        text=True,
        cwd=str(proj),
        env=env,
    )
    assert p.returncode == 0


def test_no_rapids_ancestor_allows():
    with tempfile.TemporaryDirectory(prefix="pkg-exist-no-rapids-") as td:
        (Path(td) / "uv.lock").write_text('[[package]]\nname = "requests"\n')
        (Path(td) / "requirements.txt").write_text("reqeusts\n")
        code, _ = _run_guard("pip install -r requirements.txt", cwd=Path(td))
        assert code == 0
