#!/usr/bin/env python3
"""Blast-radius computation over the aah-fix YAML master log.

The aah-fix skill owns feedback capture and in-model classification. It writes
one YAML master log per feedback session at
``.aah/build/feedback/<feedback_id>.yaml`` and appends one step per phase.

This module's single deterministic job is ``compute-blast-radius``: given the
``affected_modules`` passed on the command line, find the features whose
``file_scope`` / ``module_ref`` fall inside those modules, expand them
downstream through the DAG, and write BOTH ``affected_modules`` and
``blast_radius_features`` into the plan step. The script is the authoritative
writer of those fields — there is no read-before-write ordering dependency. It
never globs ``*.json`` and never creates JSON feedback files.

Usage:
    aah run core.build.feedback_capture compute-blast-radius \
        --project-path . --file .aah/build/feedback/UF-06-147.yaml \
        --affected-modules "auth,session"
    aah run core.build.feedback_capture set-status \
        --project-path . --file .aah/build/feedback/UF-06-147.yaml --status completed
    aah run core.build.feedback_capture resolve-id \
        --project-path . --wave 6
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.dag import dag_from_json, get_dependents
from aah.core.common.io_utils import read_json, read_yaml, write_yaml
from aah.core.common.feature_utils import load_features_from_dir


# ---------------------------------------------------------------------------
# Master log helpers
# ---------------------------------------------------------------------------


def _resolve_master_log(project_path: Path, file_arg: Path | str | None) -> Path:
    """Resolve the YAML master-log path from --file.

    Accepts an absolute path, a path relative to the project root, or a bare
    ``<feedback_id>.yaml`` (looked up under ``.aah/build/feedback/``).
    """
    if file_arg is None:
        raise ValueError("--file <yaml> is required")
    p = Path(file_arg)
    if p.is_absolute() and p.exists():
        return p
    candidate = project_path / p
    if candidate.exists():
        return candidate
    # Bare filename → look under the feedback dir
    feedback_dir = project_path / ".aah" / "build" / "feedback"
    named = feedback_dir / p.name
    if named.exists():
        return named
    # Fall back to the project-relative interpretation (may not exist yet)
    return candidate


def _resolve_plan_step(master_log: dict) -> dict:
    """Return the plan step, creating and appending it if absent.

    The aah-fix master log holds one step per phase invoked. This is the
    authoritative writer for the plan step's ``affected_modules`` /
    ``blast_radius_features``, so it must always have a step to write into:
      1. The last step whose ``phase`` is ``plan``.
      2. Otherwise the last step overall (aah-fix appended it just before
         calling; the script fills in the module/blast fields).
      3. Otherwise a fresh ``phase: plan`` step, appended to ``steps``.
    """
    steps = master_log.setdefault("steps", [])
    plan_steps = [s for s in steps if str(s.get("phase")) == "plan"]
    if plan_steps:
        return plan_steps[-1]
    if steps:
        return steps[-1]
    step = {"phase": "plan", "completed_at": None}
    steps.append(step)
    return step


# ---------------------------------------------------------------------------
# Blast radius computation (YAML master log)
# ---------------------------------------------------------------------------


def compute_blast_radius(
    project_path: Path,
    file_arg: Path | str,
    affected_modules: list[str],
) -> dict:
    """Compute blast radius from the modules passed on the command line.

    Option B: ``affected_modules`` arrives as an explicit argument — the script
    never reads it back from the YAML. The script is the authoritative writer:
    it stamps BOTH ``affected_modules`` and ``blast_radius_features`` into the
    plan step, so there is no read-before-write ordering dependency on aah-fix.

    Steps:
      1. Resolve the given modules to seed features via ``module_ref`` match and
         ``file_scope`` path overlap.
      2. Expand each seed downstream through the DAG (transitive dependents).
      3. Write ``affected_modules`` + ``blast_radius_features`` into the plan
         step and persist.
    """
    aah_path = project_path / ".aah"
    master_log_path = _resolve_master_log(project_path, file_arg)

    if not master_log_path.exists():
        return {"error": f"Master log not found: {master_log_path}"}

    modules = [m.strip() for m in (affected_modules or []) if m and m.strip()]

    master_log = read_yaml(master_log_path)
    step = _resolve_plan_step(master_log)

    if not modules:
        # Nothing to expand from — record empty results so downstream readers
        # see a definitive answer rather than a missing key.
        step["affected_modules"] = []
        step["blast_radius_features"] = []
        write_yaml(master_log, master_log_path)
        return {
            "feedback_id": master_log.get("feedback_id"),
            "affected_modules": [],
            "seed_features": [],
            "blast_radius_features": [],
            "total_affected": 0,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    # Resolve modules → seed features.
    seed_features = _features_in_modules(project_path, modules)

    # Expand downstream through the DAG.
    all_affected = set(seed_features)
    dag_path = aah_path / "plan" / "dag.json"
    if dag_path.exists() and seed_features:
        dag = dag_from_json(read_json(dag_path))
        for fid in seed_features:
            if fid in dag:
                all_affected.update(get_dependents(dag, fid))

    blast_radius_features = sorted(all_affected)

    # Script is the authoritative writer — stamp BOTH fields into the plan step.
    step["affected_modules"] = modules
    step["blast_radius_features"] = blast_radius_features
    write_yaml(master_log, master_log_path)

    return {
        "feedback_id": master_log.get("feedback_id"),
        "affected_modules": modules,
        "seed_features": sorted(seed_features),
        "blast_radius_features": blast_radius_features,
        "total_affected": len(blast_radius_features),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def _features_in_modules(project_path: Path, affected_modules: list[str]) -> list[str]:
    """Find feature IDs that belong to any of the given modules.

    A feature belongs to a module when EITHER:
      - its ``module_ref`` matches the module name/id (case-insensitive), OR
      - any path in its ``file_scope`` contains the module token as a path part
        (e.g. module ``auth`` matches ``src/auth/service.py``).
    """
    aah_path = project_path / ".aah"
    features_dir = aah_path / "plan" / "features"
    features = load_features_from_dir(features_dir)

    module_tokens = {m.strip().lower() for m in affected_modules if m and m.strip()}
    if not module_tokens:
        return []

    matched: set[str] = set()
    for feature in features:
        fid = feature.get("id")
        if not fid:
            continue

        module_ref = str(feature.get("module_ref", "")).strip().lower()
        if module_ref and module_ref in module_tokens:
            matched.add(fid)
            continue

        for fp in feature.get("file_scope", []) or []:
            parts = {p.lower() for p in Path(fp).parts}
            if parts & module_tokens:
                matched.add(fid)
                break

    return sorted(matched)


# ---------------------------------------------------------------------------
# Master-log status
# ---------------------------------------------------------------------------


def set_status(project_path: Path, file_arg: Path | str, status: str) -> dict:
    """Set the ``status`` field on the YAML master log."""
    master_log_path = _resolve_master_log(project_path, file_arg)
    if not master_log_path.exists():
        return {"error": f"Master log not found: {master_log_path}"}

    master_log = read_yaml(master_log_path)
    master_log["status"] = status
    master_log["status_updated_at"] = datetime.now(timezone.utc).isoformat()
    write_yaml(master_log, master_log_path)
    return {"feedback_id": master_log.get("feedback_id"), "status": status}


# ---------------------------------------------------------------------------
# Feedback IDs — auto-incremented per wave
# ---------------------------------------------------------------------------
#
# Every feedback session id is: ``UF-<wave>-<seq>``.
# Both wave and sequence number are zero-padded to 2 digits (e.g. UF-06-03).
# The sequence number is auto-incremented by counting existing master logs for
# that wave. There is no GitHub issue dependency and no dedup gate — each
# aah-fix invocation is an intentional, distinct feedback session.


def resolve_feedback_id(project_path: Path, wave: int) -> str:
    """Return the next ``UF-<wave>-<seq>`` feedback id for the given wave.

    Counts existing ``.aah/build/feedback/UF-<wave>-*.yaml`` files and
    increments by one. Both wave and sequence are zero-padded to 2 digits.
    """
    try:
        wave_int = int(wave)
        wave_part = f"{wave_int:02d}"
    except (TypeError, ValueError):
        wave_part = str(wave)

    feedback_dir = project_path / ".aah" / "build" / "feedback"
    existing = list(feedback_dir.glob(f"UF-{wave_part}-*.yaml")) if feedback_dir.exists() else []
    seq = len(existing) + 1
    return f"UF-{wave_part}-{seq:02d}"


def find_inprogress_session_for_wave(project_path: Path, wave: int) -> Path | None:
    """Return the master-log path for any in-progress session for this wave, else None.

    Used as a UX check only — surfaces an existing session to the user so they
    can choose to continue it or start a new one. Not a hard dedup gate.
    """
    try:
        wave_int = int(wave)
        wave_part = f"{wave_int:02d}"
    except (TypeError, ValueError):
        wave_part = str(wave)

    feedback_dir = project_path / ".aah" / "build" / "feedback"
    if not feedback_dir.exists():
        return None
    for path in sorted(feedback_dir.glob(f"UF-{wave_part}-*.yaml")):
        try:
            data = read_yaml(path)
            if isinstance(data, dict) and data.get("status") == "in_progress":
                return path
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Feedback Blast-Radius (YAML master log)")
    sub = parser.add_subparsers(dest="command", required=True)

    blast_p = sub.add_parser("compute-blast-radius", help="Compute blast radius from the given modules")
    blast_p.add_argument("--project-path", type=Path, default=None)
    blast_p.add_argument("--file", type=str, required=True,
                         help="Path to the YAML master log (.aah/build/feedback/<id>.yaml)")
    blast_p.add_argument("--affected-modules", type=str, required=True,
                         help="Comma-separated module names touched by the feedback (e.g. 'auth,session')")

    status_p = sub.add_parser("set-status", help="Set status on the YAML master log")
    status_p.add_argument("--project-path", type=Path, default=None)
    status_p.add_argument("--file", type=str, required=True)
    status_p.add_argument("--status", type=str, required=True)

    resolve_p = sub.add_parser(
        "resolve-id",
        help="Print the next UF-<wave>-<seq> id and surface any in-progress session for the wave",
    )
    resolve_p.add_argument("--project-path", type=Path, default=None)
    resolve_p.add_argument("--wave", type=int, required=True)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "compute-blast-radius":
        modules = [m.strip() for m in args.affected_modules.split(",") if m.strip()]
        result = compute_blast_radius(project_path, args.file, modules)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(
            f"Blast radius: {result['total_affected']} feature(s) "
            f"from modules {result['affected_modules']}",
            file=sys.stderr,
        )
        sys.exit(0)

    elif args.command == "set-status":
        result = set_status(project_path, args.file, args.status)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0)

    elif args.command == "resolve-id":
        feedback_id = resolve_feedback_id(project_path, args.wave)
        active = find_inprogress_session_for_wave(project_path, args.wave)
        result = {
            "feedback_id": feedback_id,
            "in_progress_exists": active is not None,
            "master_log": str(active) if active else None,
        }
        json.dump(result, sys.stdout, indent=2)
        print()
        if active is not None:
            print(
                f"Existing in-progress session found: {active.name} — "
                "ask user whether to continue it or start a new one.",
                file=sys.stderr,
            )
        sys.exit(0)


if __name__ == "__main__":
    main()
