#!/usr/bin/env python3
"""Wave marker writer + freshness checker for the expertise gate.

Writes ``.aah/build/wave-{N}-expertise-updated.json`` atomically,
binding the marker to:
  - integration branch HEAD SHA at write-time
  - sha256 of ``expertise.yaml``
  - sha256 of the canonical ``domains/*.yaml`` manifest

The gate re-hashes those artifacts and refires when any of them drift,
so a marker can't survive an out-of-band edit or a HEAD advance.

Scope: expertise marker only.

Marker schema (schema_version=2):
    {
      "schema_version": 2,
      "wave": 0,
      "head_sha": "<code_subject_identity of integration/wave-N at write-time>",
      "head_branch": "integration/wave-0",
      "expertise_yaml_sha256": "<sha256 of expertise.yaml at write-time>",
      "domains_manifest_sha256": "<sha256 of canonical domains/*.yaml manifest>",
      "written_at": "<ISO-8601 UTC>",
      "writer": "aah.core.build.wave_markers.write_expertise_marker",
      "summary": { ... skill-supplied free-form summary ... }
    }

The "domains manifest" is sha256 over the canonical-JSON dump of
``sorted([(name, sha256(content)) for each domains/*.yaml])`` — catches
any change to any domain file (add, remove, edit) without re-hashing the
whole directory on every check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
WRITER_ID = "aah.core.build.wave_markers.write_expertise_marker"

OUTCOME_SCHEMA_VERSION = 1
OUTCOME_WRITER_ID = "aah.core.build.wave_markers.write_expertise_outcome"

# Required top-level fields. is_expertise_marker_fresh rejects a marker
# missing any of these. Order is documentation only — the check is
# membership, not sequence.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "wave",
    "head_sha",
    "expertise_yaml_sha256",
    "domains_manifest_sha256",
    "written_at",
    "writer",
)


# ---------------------------------------------------------------------------
# Hashing primitives
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Stream-hash a file. Returns sha256 hex digest.

    Streams in 64KB chunks so we don't load multi-megabyte expertise.yaml
    files into memory. Returns the empty-string hash for missing files —
    callers branch on the field being present, not the value.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _domains_manifest_sha256(domains_dir: Path) -> str:
    """Compute the canonical domains-manifest hash.

    Steps:
      1. List ``domains_dir/*.yaml`` (deterministic sort by filename).
      2. For each file, compute sha256 of its raw bytes.
      3. Build a list of [filename, hex_sha256] pairs.
      4. Hash the canonical-JSON encoding of that list.

    Why filename-only (not full path): the marker travels with the
    project, so absolute paths would break verification across machines.
    """
    if not domains_dir.exists() or not domains_dir.is_dir():
        # Empty directory hashes the same as no directory — tolerated by
        # the validator since the orchestrator's _validate_expertise_artifacts
        # check already rejects missing/empty domain files.
        items: list[list[str]] = []
    else:
        items = sorted(
            [name, _sha256_file(domains_dir / name)]
            for name in (p.name for p in domains_dir.glob("*.yaml"))
        )
    canonical = json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_write_json(payload: dict, path: Path) -> None:
    """Write JSON atomically via temp-file + os.replace.

    Same pattern as regenerate_attestation_secret.py:35-37 — a partial
    write or crash mid-write leaves the existing file untouched, so a
    concurrent reader sees either the old payload or the new one,
    never a torn intermediate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# Public writer
# ---------------------------------------------------------------------------


def write_expertise_marker(
    project_path: Path,
    wave: int,
    *,
    summary: dict[str, Any] | None = None,
    head_sha: str | None = None,
    head_branch: str | None = None,
) -> Path:
    """Write the expertise wave marker atomically.

    Captures content hashes of expertise.yaml and the domains/*.yaml
    directory at write-time. The orchestrator's freshness check
    re-computes these hashes and compares; any out-of-band edit to either
    artifact invalidates the marker.

    Args:
        project_path: project root.
        wave: wave number being marked.
        summary: skill-supplied free-form summary (tier1_changes,
                 tier2_domains_updated, etc.). Embedded verbatim in the
                 marker payload; not part of the freshness check.
        head_sha: optional content-identity override. When None, derived from
                  the integration branch. Primarily used by tests.
        head_branch: optional override for the integration branch name.
                     When None, defaults to ``integration/wave-{wave}``.

    Returns:
        Path to the written marker file.
    """
    aah_path = project_path / ".aah"
    expertise_yaml = aah_path / "codebase-intel" / "expertise.yaml"
    domains_dir = aah_path / "codebase-intel" / "domains"

    if head_branch is None:
        head_branch = f"integration/wave-{wave}"
    if head_sha is None:
        from aah.core.common.git_utils import code_subject_identity

        # Content identity ignores bookkeeping-only commits that would
        # otherwise make the marker invalidate itself (issue #131).
        resolved = code_subject_identity(cwd=project_path, ref=head_branch)
        if resolved is None:
            raise RuntimeError(
                f"wave_markers: integration branch {head_branch!r} is missing "
                "or its content identity is unavailable. Create the integration "
                "branch via merge_features_to_integration before writing the marker."
            )
        head_sha = resolved

    expertise_hash = _sha256_file(expertise_yaml) if expertise_yaml.exists() else ""
    domains_hash = _domains_manifest_sha256(domains_dir)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "wave": int(wave),
        "head_sha": head_sha,
        "head_branch": head_branch,
        "expertise_yaml_sha256": expertise_hash,
        "domains_manifest_sha256": domains_hash,
        "written_at": _now_iso(),
        "writer": WRITER_ID,
        "summary": summary or {},
    }

    marker_path = aah_path / "build" / f"wave-{wave}-expertise-updated.json"
    _atomic_write_json(payload, marker_path)
    return marker_path


# ---------------------------------------------------------------------------
# Durable, identity-bound expertise attempt outcome
# ---------------------------------------------------------------------------


def _expertise_outcome_path(project_path: Path, wave: int) -> Path:
    return project_path / ".aah" / "build" / f"wave-{wave}-expertise-outcome.json"


def write_expertise_outcome(
    project_path: Path, wave: int, *, outcome: str, reason: str = "",
) -> Path:
    """Record a durable expertise attempt outcome bound to the code identity.

    ``outcome`` is "warning". Success is represented by the fresh marker itself;
    the outcome is only the durable record of a failed, non-blocking attempt.

    Deliberately unsigned, like the expertise marker itself: it is workflow
    bookkeeping and no gate blocks on it.
    """
    if outcome != "warning":
        raise ValueError(f"outcome must be 'warning', got {outcome!r}")
    from aah.core.common.git_utils import code_subject_identity

    branch = f"integration/wave-{wave}"
    identity = code_subject_identity(cwd=project_path, ref=branch)
    if identity is None:
        raise RuntimeError(
            f"wave_markers: integration branch {branch!r} is missing or its "
            "content identity is unavailable."
        )
    path = _expertise_outcome_path(project_path, wave)
    _atomic_write_json({
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "wave": int(wave),
        "product_identity": identity,
        "outcome": outcome,
        "reason": reason,
        "written_at": _now_iso(),
        "writer": OUTCOME_WRITER_ID,
    }, path)
    return path


def expertise_outcome_matches_identity(project_path: Path, wave: int) -> bool:
    """Whether a recorded outcome matches the current product identity."""
    path = _expertise_outcome_path(project_path, wave)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict) or payload.get("outcome") != "warning":
        return False
    from aah.core.common.git_utils import code_subject_identity

    current = code_subject_identity(cwd=project_path, ref=f"integration/wave-{wave}")
    return current is not None and payload.get("product_identity") == current


# ---------------------------------------------------------------------------
# Public freshness check
# ---------------------------------------------------------------------------


def is_expertise_marker_fresh(
    marker_path: Path,
    project_path: Path,
    *,
    integration_branch: str | None = None,
) -> tuple[bool, str]:
    """Return (fresh, reason). Fresh iff:
      1. Marker exists and parses as JSON.
      2. Marker contains all v2 required fields.
      3. Marker.head_sha == code_subject_identity(integration_branch).
      4. Marker.expertise_yaml_sha256 == sha256(current expertise.yaml).
      5. Marker.domains_manifest_sha256 == manifest-hash(current domains/).

    Returns (False, "<short reason>") on any failure. Reasons are stable
    strings so callers can branch deterministically and audit logs can
    aggregate by reason.
    """
    # 1. Existence + JSON-parseability.
    if not marker_path.exists():
        return False, "marker not found"
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False, "marker not parseable"
    if not isinstance(payload, dict):
        return False, "marker not a JSON object"

    # 2. Required fields.
    missing = [f for f in _REQUIRED_FIELDS if f not in payload]
    if missing:
        return False, f"marker missing required fields: {missing}"
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False, (
            f"marker schema_version={payload.get('schema_version')!r}, "
            f"expected {SCHEMA_VERSION}"
        )

    # 3. Match code content, ignoring bookkeeping-only commits (issue #131).
    if integration_branch is None:
        integration_branch = (
            payload.get("head_branch") or f"integration/wave-{payload['wave']}"
        )
    from aah.core.common.git_utils import code_subject_identity, rev_parse

    current_sha = code_subject_identity(cwd=project_path, ref=integration_branch)
    if current_sha is None:
        return False, (
            f"integration branch {integration_branch!r} is missing or its "
            "content identity is unavailable"
        )
    recorded_sha = payload["head_sha"]
    if not isinstance(recorded_sha, str) or not recorded_sha:
        return False, "head_sha must be a non-empty string"
    if current_sha != recorded_sha:
        # Official markers written before the content-identity migration store
        # the full Git commit SHA. Compare that commit's code tree so an upgrade
        # does not refire expertise maintenance when only bookkeeping changed.
        legacy_commit = rev_parse(recorded_sha, cwd=project_path)
        legacy_identity = (
            code_subject_identity(cwd=project_path, ref=legacy_commit)
            if legacy_commit == recorded_sha
            else None
        )
        if legacy_identity == current_sha:
            recorded_sha = current_sha
    if current_sha != recorded_sha:
        return False, (
            f"head_sha mismatch (marker={payload['head_sha'][:8]}, "
            f"current={current_sha[:8]})"
        )

    # 4. expertise.yaml content hash.
    expertise_yaml = project_path / ".aah" / "codebase-intel" / "expertise.yaml"
    current_expertise_hash = (
        _sha256_file(expertise_yaml) if expertise_yaml.exists() else ""
    )
    if current_expertise_hash != payload["expertise_yaml_sha256"]:
        return False, "expertise_yaml_sha256 mismatch"

    # 5. Domains manifest hash.
    domains_dir = project_path / ".aah" / "codebase-intel" / "domains"
    current_domains_hash = _domains_manifest_sha256(domains_dir)
    if current_domains_hash != payload["domains_manifest_sha256"]:
        return False, "domains_manifest_sha256 mismatch"

    return True, ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wave marker writer (expertise gate, schema v2)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    we = sub.add_parser(
        "write-expertise",
        help="Write the wave-{N} expertise marker with content hashes.",
    )
    we.add_argument("--project-path", type=Path, default=None)
    we.add_argument("--wave", type=int, required=True)
    we.add_argument(
        "--summary",
        type=str,
        default="{}",
        help="JSON object with skill-supplied summary (tier1_changes etc.)",
    )

    wo = sub.add_parser(
        "write-expertise-outcome",
        help="Record a durable, identity-bound expertise attempt outcome.",
    )
    wo.add_argument("--project-path", type=Path, default=None)
    wo.add_argument("--wave", type=int, required=True)
    wo.add_argument("--outcome", choices=("warning",), required=True)
    wo.add_argument("--reason", type=str, default="")

    args = parser.parse_args()

    from aah.core.common.config import require_project_path

    project_path = require_project_path(args.project_path)

    if args.command == "write-expertise":
        try:
            summary = json.loads(args.summary)
        except json.JSONDecodeError as e:
            print(f"wave_markers: --summary is not valid JSON: {e}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(summary, dict):
            print("wave_markers: --summary must be a JSON object", file=sys.stderr)
            sys.exit(2)
        try:
            path = write_expertise_marker(project_path, args.wave, summary=summary)
        except RuntimeError as e:
            # Missing integration branch is a configuration error, not a
            # transient failure. Exit 2 matches verify.py's convention.
            print(f"wave_markers: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"wave_markers: wrote {path}")
        sys.exit(0)

    if args.command == "write-expertise-outcome":
        try:
            path = write_expertise_outcome(
                project_path, args.wave,
                outcome=args.outcome, reason=args.reason,
            )
        except RuntimeError as e:
            print(f"wave_markers: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"wave_markers: wrote {path}")
        sys.exit(0)


if __name__ == "__main__":
    main()
