#!/usr/bin/env python3
"""
Update impl-state.json from DAG, feature-list.json, and test results.

Maintains a persistent record of which features are complete, in-progress, or blocked.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.build.verification_evidence import RUNTIME_RESULTS_PREFIXES
from aah.core.common.dag import dag_from_json, get_execution_frontier
from aah.core.common.feature_list import load_feature_list
from aah.core.common.feature_utils import find_feature_file, flatten_wave_features
from aah.core.common.io_utils import read_json, read_yaml, write_json
from aah.core.common.verified_artifacts import ArtifactState, load_attested_artifact


def update_impl_state(project_path: Path) -> dict:
    """Build and return the current implementation state."""
    aah_path = project_path / ".aah"

    # Load feature list
    fl_path = aah_path / "feature-list.json"
    fl_data = load_feature_list(fl_path if fl_path.exists() else None)
    features = fl_data.get("features", [])

    # Categorize features
    completed = {f["id"] for f in features if f.get("passes", False)}
    all_ids = {f["id"] for f in features}
    pending = all_ids - completed

    # Load DAG for frontier calculation
    dag_path = aah_path / "plan" / "dag.json"
    frontier = []
    if dag_path.exists():
        try:
            dag_data = read_json(dag_path)
            G = dag_from_json(dag_data)
            frontier = get_execution_frontier(G, completed)
        except Exception:
            pass

    # Determine blocked features (pending but not in frontier)
    available = set(frontier)
    blocked = pending - available

    # Load waves
    waves_path = aah_path / "plan" / "waves.json"
    wave_status = []
    if waves_path.exists():
        try:
            waves_data = read_json(waves_path)
            for i, wave in enumerate(waves_data.get("waves", [])):
                # Flatten tiers: nested format is [[tier0_fids], [tier1_fids], ...]
                wave_feature_ids = flatten_wave_features(wave)
                wave_features = set(wave_feature_ids)
                wave_complete = wave_features.issubset(completed)
                wave_status.append({
                    "wave": i,
                    "features": wave_feature_ids,
                    "complete": wave_complete,
                    "in_progress": sorted(wave_features & available),
                    "blocked": sorted(wave_features & blocked),
                })
        except Exception:
            pass

    # Load checkpoint status
    checkpoint_status = _compute_checkpoint_status(aah_path, wave_status)

    # Block features in waves behind a failed checkpoint
    checkpoint_blocked = _get_checkpoint_blocked_features(aah_path, wave_status)
    if checkpoint_blocked:
        available -= checkpoint_blocked
        blocked |= checkpoint_blocked

    state = {
        "completed": sorted(completed),
        "available": sorted(available),
        "blocked": sorted(blocked),
        "total": len(all_ids),
        "completion_pct": round(len(completed) / len(all_ids) * 100, 1) if all_ids else 0.0,
        "wave_status": wave_status,
        "checkpoint_status": checkpoint_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    return state



def get_wave_context(project_path: Path, wave_num: int, tier_num: int | None = None) -> dict:
    """
    Return everything needed to implement a wave (or a specific tier) in ONE call.
    No file hunting needed — paths are pre-resolved.
    """
    aah_path = project_path / ".aah"
    features_dir = aah_path / "plan" / "features"

    # Load waves
    waves_path = aah_path / "plan" / "waves.json"
    if not waves_path.exists():
        return {"error": "waves.json not found"}
    waves_data = read_json(waves_path)
    waves = waves_data.get("waves", [])
    if wave_num >= len(waves):
        return {"error": f"Wave {wave_num} not found (total: {len(waves)})"}

    wave_raw = waves[wave_num]

    # Flat list of every feature ID in the wave (handles dict/nested/flat).
    all_wave_features = flatten_wave_features(wave_raw)

    # Tier split: nested tier format [[tier0], [tier1], ...]; dict and flat
    # waves collapse to a single tier holding all features.
    if isinstance(wave_raw, list) and wave_raw and isinstance(wave_raw[0], list):
        wave_tiers = wave_raw
    else:
        wave_tiers = [all_wave_features]

    total_tiers = len(wave_tiers)

    # Resolve features for the requested tier (or all if tier_num not specified)
    if tier_num is not None:
        if tier_num >= total_tiers:
            return {"error": f"Tier {tier_num} not found in wave {wave_num} (total tiers: {total_tiers})"}
        wave_features = wave_tiers[tier_num]
    else:
        wave_features = all_wave_features

    # Sprint contract
    contract_path = aah_path / "plan" / "sprint-contracts" / f"wave-{wave_num}-contract.md"
    if not contract_path.exists():
        # Try matching by content (wave numbering might be offset)
        for cp in (aah_path / "plan" / "sprint-contracts").glob("*.md"):
            contract_path = cp
            break

    # Resolve each feature
    features = []
    for fid in wave_features:
        from aah.core.common.feature_utils import load_feature_data
        feature_file_path = find_feature_file(features_dir, fid)
        feature_data = load_feature_data(features_dir, fid) or {}
        features.append({
            "id": fid,
            "feature_path": str(feature_file_path) if feature_file_path else f"NOT FOUND: {fid}",
            "description": feature_data.get("description", ""),
            "dependencies": feature_data.get("dependencies", []),
            "acceptance_criteria": feature_data.get("acceptance_criteria", []),
            "test_cases": feature_data.get("test_cases", []),
            "test_config": feature_data.get("test_config", {}),
            "layers": feature_data.get("layers", []),
        })

    # Check completion status
    fl_path = aah_path / "feature-list.json"
    completed = set()
    if fl_path.exists():
        fl = read_json(fl_path)
        completed = {f["id"] for f in fl.get("features", []) if f.get("passes")}

    available = [f for f in features if f["id"] not in completed]
    already_done = [f for f in features if f["id"] in completed]

    # Include knowledge base context if present
    knowledge_context = ""
    try:
        from aah.core.knowledge.parser import build_knowledge_context, find_knowledge_dir
        if find_knowledge_dir(project_path):
            knowledge_context = build_knowledge_context(project_path)
    except Exception:
        pass

    # Load consumption_views from expertise
    style_guide = ""
    known_concerns = ""
    quick_context = ""
    expertise_path = aah_path / "codebase-intel" / "expertise.yaml"
    if expertise_path.exists():
        try:
            expertise = read_yaml(expertise_path)
            views = expertise.get("consumption_views") or {}
            style_guide = views.get("style_guide", "")
            known_concerns = views.get("known_concerns", "")
            quick_context = views.get("quick_context", "")
        except Exception:
            pass

    # Load available Tier 2 domain files
    available_domains = []
    domains_dir = aah_path / "codebase-intel" / "domains"
    if domains_dir.exists() and domains_dir.is_dir():
        domain_files = list(domains_dir.glob("*.yaml"))
        available_domains = sorted(p.stem for p in domain_files)

    return {
        "wave": wave_num,
        "total_waves": len(waves),
        "tier": tier_num if tier_num is not None else 0,
        "total_tiers": total_tiers,
        "contract_path": str(contract_path),
        "project_dir": str(project_path),
        "features": available,
        "already_completed": [f["id"] for f in already_done],
        "feature_count": len(available),
        "max_parallel_batch": 4,
        "total_features_in_wave": len(all_wave_features),
        "total_features_in_tier": len(wave_features),
        "strategy": "parallel",
        "integration_branch": f"integration/wave-{wave_num}",
        "knowledge_context": knowledge_context,
        "style_guide": style_guide,
        "known_concerns": known_concerns,
        "quick_context": quick_context,
        "available_domains": available_domains,
    }


def _compute_checkpoint_status(aah_path: Path, wave_status: list[dict]) -> dict:
    """Compute checkpoint pass/fail status for each wave."""
    checkpoint_dir = aah_path / "build" / "checkpoint-results"
    status = {"waves": {}}

    for ws in wave_status:
        wave_num = ws["wave"]
        wave_info = {"system_passed": None, "approved": False}

        runtime = _runtime_checkpoint_result(aah_path, wave_num)
        if runtime is not None:
            wave_info["system_passed"] = runtime.get("overall_passed")

        if checkpoint_dir.is_dir():
            # Approval
            approval_file = checkpoint_dir / f"wave-{wave_num}-approval.json"
            if approval_file.exists():
                try:
                    data = read_json(approval_file)
                    wave_info["approved"] = data.get("approved", False)
                except Exception:
                    pass

        status["waves"][str(wave_num)] = wave_info

    return status


def _runtime_checkpoint_result(aah_path: Path, wave: int) -> dict | None:
    path = aah_path / "build" / "runtime-results" / f"wave-{wave}-all.json"
    artifact = load_attested_artifact(
        path, aah_path.parent, RUNTIME_RESULTS_PREFIXES, "json"
    )
    return artifact.payload if artifact.state is ArtifactState.VERIFIED else None


def _get_checkpoint_blocked_features(aah_path: Path, wave_status: list[dict]) -> set:
    """Get features that are blocked due to a failed checkpoint in a prior wave.

    If wave N's checkpoint failed, all features in wave N+1 onwards are blocked
    until the checkpoint passes.
    """
    blocked_features: set = set()

    # Find the first wave with a failed checkpoint
    failed_wave = None
    for ws in wave_status:
        wave_num = ws["wave"]
        if ws.get("complete"):
            data = _runtime_checkpoint_result(aah_path, wave_num)
            if data is not None and data.get("overall_passed") is False:
                failed_wave = wave_num
                break

    # Block all features in waves after the failed one
    if failed_wave is not None:
        for ws in wave_status:
            if ws["wave"] > failed_wave:
                wave_feature_ids = ws.get("features", [])
                if isinstance(wave_feature_ids, list):
                    blocked_features.update(wave_feature_ids)

    return blocked_features


def main() -> None:
    parser = argparse.ArgumentParser(description="Update implementation state")
    parser.add_argument("--project-path", type=Path, default=None)

    sub = parser.add_subparsers(dest="command")
    wave_p = sub.add_parser("wave-context", help="Get full context for a wave (features, paths, contract)")
    wave_p.add_argument("--wave", type=int, required=True)
    wave_p.add_argument("--tier", type=int, default=None)
    wave_p.add_argument("--project-path", type=Path, default=None, dest="wave_project_path")

    args = parser.parse_args()

    from aah.core.common.config import require_project_path

    if args.command == "wave-context":
        project_path = require_project_path(args.wave_project_path)
        context = get_wave_context(project_path, args.wave, tier_num=args.tier)
        json.dump(context, sys.stdout, indent=2)
        print()
        sys.exit(0)

    # Default: update impl state
    project_path = require_project_path(args.project_path)

    state = update_impl_state(project_path)

    # Write state file
    state_path = project_path / ".aah" / "build" / "impl-state.json"
    write_json(state, state_path)

    json.dump(state, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
