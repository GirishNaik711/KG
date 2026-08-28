#!/usr/bin/env python3
"""Orchestrator-internal codemap refresh.

Runs codemap-scale as a subprocess and writes an attested result file at
``.aah/build/runtime-results/wave-{N}-codemap.json``. The
codemap gate verifies the attestation and re-checks freshness against
current state instead of trusting a marker file.

Subprocess (not in-process): isolates codemap-scale OOMs / segfaults
from the orchestrator and gives the attestation block a single
authoritative process owner for stdout/stderr hashes.

Payload schema:

    {
      "wave": 0,
      "head_sha_pre": "<HEAD before refresh>",
      "head_sha_post": "<HEAD after refresh — gate freshness checks against this>",
      "head_branch": "integration/wave-0",
      "codemap_db_sha256_pre": "<sha256 of codemap.db before, or '' if absent>",
      "codemap_db_sha256_post": "<sha256 of codemap.db after>",
      "files_indexed": <int>,
      "symbols_extracted": <int>,
      "relations_extracted": <int>,
      "duration_ms": <int>,
      "operation": "refresh"
    }

The codemap gate accepts the artifact iff:
  1. Attestation verifies under COMMAND_PREFIX.
  2. ``head_sha_post == git rev-parse <head_branch>`` — no commits
     landed after the refresh.
  3. ``codemap_db_sha256_post == sha256(current codemap.db)`` — no
     out-of-band edits.

Any check failing → gate refires this module.

Usage:
    aah run core.build.codemap_refresh --wave 0
    aah run core.build.codemap_refresh --wave 0 --project-path .
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aah.core.common.attestation import write_attested
from aah.core.common.codemap_utils import get_db_path


# Recorded in the attestation block. The codemap gate's _read_attested
# call requires this exact prefix.
COMMAND_PREFIX = ["aah", "run", "core.build.codemap_refresh"]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Stream-hash a file. Returns hex digest, or '' for missing files.

    Streamed in 64KB chunks because codemap.db can be hundreds of MB on
    large projects.
    """
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# codemap-scale subprocess
# ---------------------------------------------------------------------------


def _run_codemap_refresh(project_path: Path, db_path: Path) -> tuple[int, str, str, dict]:
    """Run ``codemap-scale`` over the project. Returns (rc, stdout, stderr, parsed_stats).

    Prefers the ``codemap`` CLI; falls back to importing
    ``codemap_scale`` directly so tests run without the CLI installed.
    """
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["codemap", "scout", str(project_path),
             "--db-path", str(db_path), "--json", "--force"],
            capture_output=True,
            text=True,
            timeout=1800,  # 30min cap — large monorepos take time
        )
    except FileNotFoundError:
        return _run_codemap_via_import(project_path, db_path, started)
    except subprocess.TimeoutExpired:
        return (
            124,
            "",
            f"codemap scout timed out after 1800s",
            {},
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    stats: dict[str, Any] = {}
    # codemap may print a banner before the JSON payload — scan from the
    # bottom for the last JSON-shaped line.
    for line in stdout.strip().splitlines()[::-1]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            stats = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    return completed.returncode, stdout, stderr, stats


def _run_codemap_via_import(
    project_path: Path,
    db_path: Path,
    started: float,
) -> tuple[int, str, str, dict]:
    """Import-based fallback when ``codemap`` is not on PATH but the
    ``codemap_scale`` package is importable. Returns the same shape as
    the CLI branch.
    """
    try:
        from codemap_scale.orchestrator import CodeMapScale
    except ImportError as e:
        return (
            127,
            "",
            f"codemap CLI not on PATH AND codemap_scale not importable: {e}",
            {},
        )

    try:
        scale = CodeMapScale(root=str(project_path), db_path=str(db_path))
        result = scale.scout(force=True) if hasattr(scale, "scout") else {}
        stats = {
            "files": result.get("files", 0) if isinstance(result, dict) else 0,
            "symbols": result.get("symbols", 0) if isinstance(result, dict) else 0,
            "relations": result.get("relations", 0) if isinstance(result, dict) else 0,
        }
        return 0, json.dumps(stats), "", stats
    except Exception as e:  # noqa: BLE001 — capture-and-report posture
        return 1, "", f"codemap_scale.scout() raised: {e}", {}


# ---------------------------------------------------------------------------
# Public refresh
# ---------------------------------------------------------------------------


def refresh(
    project_path: Path,
    wave: int,
    *,
    head_branch: str | None = None,
) -> int:
    """Run codemap refresh for the wave. Writes attested
    wave-{N}-codemap.json. Returns 0 on success, 1 on failure, 2 on
    internal error.
    """
    pipeline_started = time.monotonic()
    aah_path = project_path / ".aah"
    db_path = get_db_path(project_path)

    if head_branch is None:
        head_branch = f"integration/wave-{wave}"

    from aah.core.common.git_utils import rev_parse
    head_sha_pre = rev_parse(head_branch, cwd=project_path) or ""
    db_sha_pre = _sha256_file(db_path)

    rc, stdout, stderr, stats = _run_codemap_refresh(project_path, db_path)
    duration_ms = int((time.monotonic() - pipeline_started) * 1000)

    head_sha_post = rev_parse(head_branch, cwd=project_path) or ""
    db_sha_post = _sha256_file(db_path)

    overall_passed = rc == 0
    payload: dict[str, Any] = {
        "wave": int(wave),
        "operation": "refresh",
        "overall_passed": overall_passed,
        "head_sha_pre": head_sha_pre,
        "head_sha_post": head_sha_post,
        "head_branch": head_branch,
        "codemap_db_sha256_pre": db_sha_pre,
        "codemap_db_sha256_post": db_sha_post,
        "files_indexed": int(stats.get("files", 0)),
        "symbols_extracted": int(stats.get("symbols", 0)),
        "relations_extracted": int(stats.get("relations", 0)),
        "duration_ms": duration_ms,
        "exit_code": rc,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    output_dir = aah_path / "build" / "runtime-results"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"wave-{wave}-codemap.json"

    write_attested(
        payload,
        out_path,
        project_path=project_path,
        command=COMMAND_PREFIX + sys.argv[1:],
        exit_code=rc,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        artifact_name=f"wave-{wave} codemap refresh",
    )

    return 0 if overall_passed else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestrator-internal codemap refresh"
    )
    parser.add_argument("--wave", type=int, required=True)
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument(
        "--head-branch",
        type=str,
        default=None,
        help="Override the integration branch name (default: integration/wave-{wave})",
    )
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    try:
        rc = refresh(project_path, args.wave, head_branch=args.head_branch)
    except Exception as e:  # noqa: BLE001
        print(f"codemap_refresh.py: internal error: {e}", file=sys.stderr)
        sys.exit(2)
    sys.exit(rc)


if __name__ == "__main__":
    main()
