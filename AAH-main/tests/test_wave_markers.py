"""Unit tests for wave_markers — expertise marker writer + freshness checker.

NO MOCKS — every test uses a real git repo (via the git_repo fixture),
real file I/O, and real subprocess invocations for the CLI tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from aah.core.build.wave_markers import (
    SCHEMA_VERSION,
    WRITER_ID,
    _domains_manifest_sha256,
    _sha256_file,
    is_expertise_marker_fresh,
    write_expertise_marker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def expertise_project(git_repo):
    """A project with .aah skeleton, integration/wave-0 branch checked
    out, and seed expertise.yaml + one domain file.

    Returns (project_path, integration_branch_name).
    """
    aah_root = git_repo / ".aah"
    (aah_root / "build").mkdir(parents=True, exist_ok=True)
    (aah_root / "codebase-intel" / "domains").mkdir(parents=True, exist_ok=True)

    # Manifest is required by require_project_path() so CLI tests resolve
    # this directory as the active project.
    (aah_root / "manifest.yaml").write_text(
        "project_name: test-project\nproject_type: greenfield\ncurrent_phase: implement\n"
    )

    # Seed expertise.yaml so its hash is stable.
    expertise = aah_root / "codebase-intel" / "expertise.yaml"
    expertise.write_text(
        "consumption_views:\n  style_guide: 'use snake_case'\n  known_concerns: ''\n"
    )

    # Seed one domain file.
    (aah_root / "codebase-intel" / "domains" / "auth.yaml").write_text(
        "domain: auth\npatterns:\n  - 'jwt'\n"
    )

    # Make integration/wave-0 branch with a commit so HEAD is stable.
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "checkout", "-b", "integration/wave-0"],
                   cwd=git_repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "seed expertise"],
                   cwd=git_repo, check=True, capture_output=True, env=env)
    return git_repo


def _git_commit_empty(project_path: Path, msg: str = "advance HEAD") -> str:
    """Make an empty commit so HEAD advances. Returns new HEAD SHA."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "--allow-empty", "-m", msg],
                   cwd=project_path, check=True, capture_output=True, env=env)
    sha = subprocess.run(["git", "rev-parse", "HEAD"],
                         cwd=project_path, capture_output=True,
                         text=True, check=True).stdout.strip()
    return sha


def _git_commit_paths(project_path: Path, msg: str, paths: list[str]) -> str:
    """Stage the given paths and commit. Returns the new HEAD SHA."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "-A", "--", *paths],
                   cwd=project_path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", msg],
                   cwd=project_path, check=True, capture_output=True, env=env)
    sha = subprocess.run(["git", "rev-parse", "HEAD"],
                         cwd=project_path, capture_output=True,
                         text=True, check=True).stdout.strip()
    return sha


# ---------------------------------------------------------------------------
# B1 — atomic write
# ---------------------------------------------------------------------------


def test_write_expertise_marker_atomic(expertise_project):
    """B1: A concurrent reader during marker write sees either the old
    payload or the complete new one, never a torn intermediate.

    Strategy: pre-seed an old marker with a sentinel, then race a writer
    against many readers. Every observed read must EITHER raise (file
    in transition — file briefly absent during os.replace is acceptable)
    OR parse cleanly. No reader should successfully parse a torn JSON.
    """
    marker = expertise_project / ".aah" / "build" / "wave-0-expertise-updated.json"
    marker.parent.mkdir(parents=True, exist_ok=True)

    # Seed an old marker with a sentinel field.
    marker.write_text(json.dumps({"sentinel": "old"}, indent=2) + "\n")

    # Build a large summary so the write is non-trivial in size.
    big_summary = {"k_" + str(i): "x" * 500 for i in range(2000)}

    parse_errors: list[str] = []
    iters = [0]
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            iters[0] += 1
            try:
                txt = marker.read_text(encoding="utf-8")
                json.loads(txt)  # Must parse cleanly OR raise — never torn.
            except (json.JSONDecodeError, ValueError) as e:
                # Torn read — would prove the write isn't atomic.
                parse_errors.append(f"json error: {e}")
            except (FileNotFoundError, OSError):
                # File briefly missing during os.replace — tolerable.
                pass

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()

    try:
        # Re-write the marker many times; each call MUST be atomic.
        for _ in range(20):
            write_expertise_marker(expertise_project, 0, summary=big_summary)
            time.sleep(0.001)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)

    assert iters[0] > 0, "readers never ran — race window not exercised"
    assert not parse_errors, (
        f"observed torn reads — atomicity violated: {parse_errors[:3]}"
    )


# ---------------------------------------------------------------------------
# B2 — content hashes captured
# ---------------------------------------------------------------------------


def test_write_expertise_marker_captures_content_hashes(expertise_project):
    """B2: marker.expertise_yaml_sha256 matches recomputed hash; same
    for domains_manifest_sha256."""
    marker_path = write_expertise_marker(expertise_project, 0, summary={"x": 1})

    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["wave"] == 0
    assert payload["writer"] == WRITER_ID
    assert payload["summary"] == {"x": 1}

    expertise_yaml = expertise_project / ".aah" / "codebase-intel" / "expertise.yaml"
    domains_dir = expertise_project / ".aah" / "codebase-intel" / "domains"
    assert payload["expertise_yaml_sha256"] == _sha256_file(expertise_yaml)
    assert payload["domains_manifest_sha256"] == _domains_manifest_sha256(domains_dir)


# ---------------------------------------------------------------------------
# B3 — freshness passes when nothing has changed
# ---------------------------------------------------------------------------


def test_is_expertise_marker_fresh_passes_unchanged(expertise_project):
    """B3: write marker → immediately check → fresh."""
    marker_path = write_expertise_marker(expertise_project, 0, summary={})
    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is True, f"expected fresh; got reason={reason!r}"
    assert reason == ""


# ---------------------------------------------------------------------------
# B4 — drift on expertise.yaml
# ---------------------------------------------------------------------------


def test_is_expertise_marker_fresh_fails_on_yaml_drift(expertise_project):
    """B4: mutate expertise.yaml after writing marker → not fresh."""
    marker_path = write_expertise_marker(expertise_project, 0, summary={})
    expertise_yaml = expertise_project / ".aah" / "codebase-intel" / "expertise.yaml"
    expertise_yaml.write_text(expertise_yaml.read_text() + "\n# drift\n")

    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is False
    assert "expertise_yaml_sha256" in reason


# ---------------------------------------------------------------------------
# B5 — drift on domains manifest
# ---------------------------------------------------------------------------


def test_is_expertise_marker_fresh_fails_on_domains_drift(expertise_project):
    """B5: add a new domain file after writing marker → not fresh."""
    marker_path = write_expertise_marker(expertise_project, 0, summary={})
    new_domain = expertise_project / ".aah" / "codebase-intel" / "domains" / "billing.yaml"
    new_domain.write_text("domain: billing\n")

    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is False
    assert "domains_manifest_sha256" in reason


# ---------------------------------------------------------------------------
# B6 — drift on integration HEAD
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# B7 — legacy / hand-written markers rejected
# ---------------------------------------------------------------------------


def test_is_expertise_marker_fresh_rejects_legacy_marker(expertise_project):
    """A marker missing the v2 required fields is not fresh.

    A stub ``{"wave":N}`` marker satisfies neither schema_version nor
    head_sha and the gate must refire — handles AI agents or older
    skills that produced existence-only markers.
    """
    marker_path = expertise_project / ".aah" / "build" / "wave-0-expertise-updated.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({"wave": 0}) + "\n")

    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is False
    assert "missing required fields" in reason


def test_is_expertise_marker_fresh_rejects_corrupt_json(expertise_project):
    """B7-bonus: a marker that's not valid JSON is not fresh."""
    marker_path = expertise_project / ".aah" / "build" / "wave-0-expertise-updated.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("{not json")

    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is False
    assert "not parseable" in reason


def test_is_expertise_marker_fresh_rejects_missing_marker(expertise_project):
    """B7-bonus: missing marker file is not fresh."""
    marker_path = expertise_project / ".aah" / "build" / "wave-0-expertise-updated.json"
    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is False
    assert reason == "marker not found"


# ---------------------------------------------------------------------------
# CLI round-trip
# ---------------------------------------------------------------------------


def test_cli_write_expertise_round_trips(expertise_project):
    """The CLI invocation in the new SKILL.md step must produce a marker
    that immediately passes is_expertise_marker_fresh."""
    summary_json = json.dumps({"tier1_changes": ["style_guide"], "tier2_domains_updated": ["auth"]})
    result = subprocess.run(
        ["python3", "-m", "aah.core.build.wave_markers",
         "write-expertise",
         "--project-path", str(expertise_project),
         "--wave", "0",
         "--summary", summary_json],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}: stderr={result.stderr}, stdout={result.stdout}"
    )
    assert "wrote" in result.stdout

    marker_path = expertise_project / ".aah" / "build" / "wave-0-expertise-updated.json"
    assert marker_path.exists()
    fresh, reason = is_expertise_marker_fresh(
        marker_path, expertise_project, integration_branch="integration/wave-0",
    )
    assert fresh is True, f"CLI-written marker failed freshness check: {reason}"


def test_cli_rejects_non_object_summary(expertise_project):
    """--summary must be a JSON object, not an array or scalar."""
    result = subprocess.run(
        ["python3", "-m", "aah.core.build.wave_markers",
         "write-expertise",
         "--project-path", str(expertise_project),
         "--wave", "0",
         "--summary", "[1,2,3]"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert "must be a JSON object" in result.stderr


# ---------------------------------------------------------------------------
# Refuse to write marker when integration branch missing
# ---------------------------------------------------------------------------


@pytest.fixture
def project_without_integration(git_repo):
    """A project with .aah skeleton, manifest, expertise.yaml, and
    one domain file — but NO integration/wave-0 branch. Used to
    exercise the missing-branch failure path in write_expertise_marker."""
    aah_root = git_repo / ".aah"
    (aah_root / "build").mkdir(parents=True, exist_ok=True)
    (aah_root / "codebase-intel" / "domains").mkdir(parents=True, exist_ok=True)
    (aah_root / "manifest.yaml").write_text(
        "project_name: no-integration\nproject_type: greenfield\ncurrent_phase: implement\n"
    )
    (aah_root / "codebase-intel" / "expertise.yaml").write_text(
        "consumption_views:\n  style_guide: 'x'\n  known_concerns: ''\n"
    )
    (aah_root / "codebase-intel" / "domains" / "auth.yaml").write_text(
        "domain: auth\n"
    )
    # NOTE: deliberately not creating integration/wave-0.
    return git_repo


def test_write_expertise_marker_refuses_missing_branch(project_without_integration):
    """The writer must raise RuntimeError rather than silently capturing
    HEAD when the integration branch is absent. A marker referencing a
    SHA not in ``git log integration/wave-N`` is operationally
    confusing even though the gate would correctly refire."""
    from aah.core.build.wave_markers import write_expertise_marker
    with pytest.raises(RuntimeError) as exc_info:
        write_expertise_marker(project_without_integration, 0, summary={})
    msg = str(exc_info.value)
    assert "integration branch" in msg
    assert "integration/wave-0" in msg
    # Helpful remediation pointer.
    assert "merge_features_to_integration" in msg or "Create" in msg


def test_cli_exits_2_on_missing_branch(project_without_integration):
    """CLI catches the RuntimeError and exits 2 with a clear stderr
    message. Matches verify.py's 'internal error' exit convention."""
    result = subprocess.run(
        ["python3", "-m", "aah.core.build.wave_markers",
         "write-expertise",
         "--project-path", str(project_without_integration),
         "--wave", "0",
         "--summary", "{}"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, (
        f"expected exit 2 (configuration error); got {result.returncode}. "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert "integration branch" in result.stderr
    assert "integration/wave-0" in result.stderr
