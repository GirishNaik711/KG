#!/usr/bin/env python3
"""Compute increment-aware execution waves from DAG and module-map.

1 wave = 1 module. Each wave contains ordered batches (topological generations
split by file_scope). Features in the same batch are independent and touch
different files. Batches execute sequentially — batch N+1 may depend on batch N.
"""

import argparse
import json
import sys
from pathlib import Path

import networkx as nx

from aah.core.common.dag import (
    _split_by_file_scope,
    compute_execution_waves,
    dag_from_json,
    load_module_dag,
)
from aah.core.common.io_utils import read_json, read_yaml, write_json
from aah.core.common.feature_utils import flatten_wave_features


def get_wave_features(wave: list[list[str]]) -> list[str]:
    """Flatten a single wave's batches into a flat feature list."""
    return flatten_wave_features(wave)


def flatten_waves(waves_data: dict) -> list[list[str]]:
    """Flatten waves into a list of flat feature lists (one per wave).

    Each wave's batches are merged into a single list.
    Handles both new batched format and legacy flat format.
    """
    waves = waves_data.get("waves", []) if isinstance(waves_data, dict) else []
    return [get_wave_features(wave) for wave in waves] if isinstance(waves, list) else []


def compute_module_aware_waves(dag_data: dict, module_map_path: Path) -> dict:
    """Compute waves grouped by module following module DAG order.

    1 wave = 1 module. Within each wave, features are organized into
    sequential batches using topological generations + file_scope splitting.

    Returns:
        {"waves": [[[str, ...], ...], ...], "total_waves": int}
        waves[i] = batches for module i (in module DAG order)
        waves[i][j] = feature IDs in batch j (independent, non-overlapping files)
    """
    G = dag_from_json(dag_data)
    module_G = load_module_dag(module_map_path)
    module_map_data = read_yaml(module_map_path)

    # Build name→id map so feature module_ref (name) resolves to module id
    name_to_id: dict[str, str] = {}
    for mod in module_map_data.get("modules", []):
        mod_id = mod.get("id") or mod.get("name")
        mod_name = mod.get("name", "")
        if mod_id and mod_name:
            name_to_id[mod_name] = mod_id

    # Resolve each feature's module_ref name to module id
    node_to_module_id: dict[str, str | None] = {}
    for node in G.nodes:
        module_ref = G.nodes[node].get("module_ref")
        node_to_module_id[node] = name_to_id.get(module_ref, module_ref)

    # Group features by module
    module_order = list(nx.topological_sort(module_G))
    module_features: dict[str, list[str]] = {m: [] for m in module_order}
    unassigned = []

    for fid in G.nodes:
        mod_id = node_to_module_id.get(fid)
        if mod_id and mod_id in module_features:
            module_features[mod_id].append(fid)
        else:
            unassigned.append(fid)

    # Emit waves in module topological order (1 wave per module)
    waves: list[list[list[str]]] = []
    for mod_id in module_order:
        features = module_features[mod_id]
        if not features:
            continue
        batches = _compute_batches(G, features)
        waves.append(batches)

    if unassigned:
        waves.append([sorted(unassigned)])

    return {"waves": waves, "total_waves": len(waves)}


def _compute_batches(G: nx.DiGraph, features: list[str]) -> list[list[str]]:
    """Compute ordered batches within a module.

    Uses topological generations for dependency ordering, then splits
    each generation by file_scope to prevent merge conflicts.
    """
    subgraph = G.subgraph(features)
    batches: list[list[str]] = []
    for generation in nx.topological_generations(subgraph):
        sub_batches = _split_by_file_scope(G, sorted(generation))
        batches.extend(sub_batches)
    return batches


def place_rework_entry(
    waves_data: dict,
    rework_id: str,
    prerequisites: list[str],
    current_wave: int,
    current_wave_rework: bool = True,
) -> dict:
    """Place a superseding rework entry into the DAG-valid wave.

    Current-wave rework model (the fix for the defer-past-promote deadlock):
      - Existing waves are NEVER reshuffled or renumbered.
      - **Rework of an already-built feature (``current_wave_rework=True``,
        the default) lands in the CURRENT wave** — appended as a NEW batch (its
        own tier) at the frontier of that wave, BEFORE the wave promotes. A
        rework's prerequisites are all already built (it supersedes a built
        feature), so the current wave is always DAG-valid for it. This lands the
        fix before the promote gate, so the runtime-checkpoint deadlock cannot
        form — and it works even when the current wave is the last wave.
      - **New work (``current_wave_rework=False``) — a brand-new feature (Case
        4)** — is placed in the earliest DAG-valid FUTURE wave: no earlier than
        ``current_wave + 1`` AND no earlier than the wave after the last wave
        containing any of its declared prerequisites.
      - If the target index falls within the existing wave list, the entry is
        appended as a NEW batch (its own tier) of that wave. If it falls past
        the end, a new wave holding just the entry is appended.

    Returns the mutated ``waves_data`` (also mutated in place).
    """
    waves = waves_data.get("waves", [])

    # Normalize legacy flat format (list[list[str]]) into batched format
    # (list[list[list[str]]]) so appending a batch is uniform.
    batched = bool(waves) and bool(waves[0]) and isinstance(waves[0][0], list)
    if waves and not batched:
        waves = [[w] for w in waves]

    # Locate the last wave that contains any prerequisite of the rework entry.
    prereq_set = set(prerequisites)
    last_prereq_wave = -1
    for idx, wave in enumerate(waves):
        wave_fids = {f for batch in wave for f in batch}
        if wave_fids & prereq_set:
            last_prereq_wave = idx

    if current_wave_rework:
        # Rework of a built feature → append at the frontier of the CURRENT
        # wave (all prereqs are already built, so current_wave is DAG-valid).
        # Clamp so a current_wave past the end still lands on a real wave.
        target_idx = current_wave if current_wave < len(waves) else len(waves)
    else:
        # New work → earliest DAG-valid FUTURE wave: after the current wave AND
        # after the last prerequisite's wave.
        target_idx = max(current_wave + 1, last_prereq_wave + 1)

    if target_idx < len(waves):
        # Append as a new batch (its own tier) of the target wave.
        waves[target_idx].append([rework_id])
    else:
        # Extend the list with placeholder waves if needed, then a new wave.
        while len(waves) < target_idx:
            waves.append([])
        waves.append([[rework_id]])

    waves_data["waves"] = waves
    waves_data["total_waves"] = len(waves)
    return waves_data


def compute_depth_based_waves(dag_data: dict) -> dict:
    """Fallback: compute waves using topological depth (no module grouping).

    Returns same structure — single wave with all features as batches.
    """
    G = dag_from_json(dag_data)

    try:
        flat_waves = compute_execution_waves(G)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Wrap as a single wave with each flat_wave as a batch
    return {"waves": [flat_waves], "total_waves": 1}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute increment-aware execution waves"
    )
    parser.add_argument(
        "--dag-path", type=Path, default=None,
        help="Path to dag.json (required for full compute)"
    )
    parser.add_argument(
        "--module-map", type=Path, default=None,
        help="Path to module-map.yaml (optional, enables module-aware waves)"
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="Output waves.json path"
    )
    # Current-wave rework: place a superseding rework entry into the CURRENT
    # wave (default) WITHOUT recomputing / renumbering existing waves. Pass
    # --future-wave to place brand-new work in a future wave instead.
    parser.add_argument(
        "--place-rework", type=str, default=None,
        help="Rework entry ID to place (preserve-existing mode; current wave by default)",
    )
    parser.add_argument(
        "--prerequisites", type=str, default="",
        help="Comma-separated prerequisite feature IDs for the rework entry",
    )
    parser.add_argument(
        "--current-wave", type=int, default=0,
        help="Current wave index (rework lands in this wave; new work in current_wave + 1)",
    )
    parser.add_argument(
        "--future-wave", action="store_true",
        help="Place the entry in a future wave (brand-new work, Case 4) instead of the current wave",
    )
    args = parser.parse_args()

    # Preserve-existing / append mode: mutate the existing waves.json in place.
    if args.place_rework:
        if not args.output.is_file():
            print(f"Error: waves file not found: {args.output}", file=sys.stderr)
            sys.exit(1)
        waves_data = read_json(args.output)
        prerequisites = [p.strip() for p in args.prerequisites.split(",") if p.strip()]
        waves_data = place_rework_entry(
            waves_data, args.place_rework, prerequisites, args.current_wave,
            current_wave_rework=not args.future_wave,
        )
        write_json(waves_data, args.output)

        # waves.json and checkpoint-config.yaml are always written together: the
        # UCR cadence is baked into the config, so a wave-count change here would
        # otherwise leave it disagreeing with orchestrator.is_checkpoint_wave.
        # Mirrors create_rework_entry; idempotent when the count did not change.
        from aah.core.plan.determine_checkpoints import regenerate_checkpoint_config

        regenerate_checkpoint_config(args.output.resolve().parents[2])

        json.dump(waves_data, sys.stdout, indent=2)
        print()
        print(
            f"Placed rework entry {args.place_rework} "
            f"(prereqs={prerequisites}, current_wave={args.current_wave}); "
            "regenerated checkpoint-config.yaml",
            file=sys.stderr,
        )
        sys.exit(0)

    if not args.dag_path or not args.dag_path.is_file():
        print(f"Error: DAG file not found: {args.dag_path}", file=sys.stderr)
        sys.exit(1)

    dag_data = read_json(args.dag_path)

    if args.module_map and args.module_map.is_file():
        waves_data = compute_module_aware_waves(dag_data, args.module_map)
    else:
        print(
            "Warning: No module-map found — using depth-based wave computation",
            file=sys.stderr,
        )
        waves_data = compute_depth_based_waves(dag_data)

    write_json(waves_data, args.output)
    # Full recompute: aah-plan Step 4 runs `determine_checkpoints generate` as its
    # next command, so the config is regenerated there rather than duplicated here.
    json.dump(waves_data, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
