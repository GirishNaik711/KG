#!/usr/bin/env python3
"""Create a superseding rework feature entry (current-wave rework model).

When feedback touches an ALREADY-BUILT feature (Cases 1 & 2), aah-plan updates
that feature's spec (``<F>.md``) in place and then calls this module to create a
distinct, schedulable **superseding entry** — ``<F>-rework-NN`` — that points
back at the updated spec. The normal wave engine builds the rework entry **in
the CURRENT wave, before that wave promotes**; the original ``<F>`` keeps
``passes: true`` (wave history + downstream dependents stay coherent) and is
superseded via ordinary forward merge once the rework entry passes.

Placing the rebuild in the current wave (not a future one) lands the fix BEFORE
the promote gate, so the runtime/system-checkpoint deadlock cannot form — and it
works even when the current wave is the last wave (no future wave need exist).

This module is deterministic mechanics only:
  1. Determine the next ``<F>-rework-NN`` id (NN increments across reworks).
  2. Write ``<F>-rework-NN.md`` as a POINTER STUB — ``## Id`` + ``## Supersedes``
     + ``## Spec File`` only, NO body. The base ``<F>.md`` (updated in place by
     plan) remains the single source of truth; ``parse_feature_frontmatter``
     resolves the pointer so every reader sees ``<F>``'s body under the rework
     id. This eliminates duplicated spec content.
  3. Sync feature-list.json (rework entry gets passes: false).
  4. Rebuild the DAG so the rework entry's edges reflect the updated spec
     (resolved through the pointer).
  5. Place the entry into the CURRENT wave via
     compute_waves.place_rework_entry — appended as a new tier/batch, existing
     waves never renumbered.

Usage:
    aah run core.plan.create_rework_entry create \
        --project-path . --feature-id F-MOD001-02 --current-wave 1
"""

import argparse
import json
import re
import sys
from pathlib import Path

from aah.core.common.feature_utils import (
    find_feature_file,
    load_feature_data,
    load_features_from_dir,
)
from aah.core.common.io_utils import read_json, write_json


REWORK_SUFFIX_RE = re.compile(r"^(?P<base>.+)-rework-(?P<num>\d+)$")


def next_rework_id(features_dir: Path, base_feature_id: str) -> str:
    """Return the next ``<base>-rework-NN`` id (2-digit, incrementing)."""
    highest = 0
    for feature in load_features_from_dir(features_dir):
        fid = feature.get("id", "")
        m = REWORK_SUFFIX_RE.match(fid)
        if m and m.group("base") == base_feature_id:
            highest = max(highest, int(m.group("num")))
    return f"{base_feature_id}-rework-{highest + 1:02d}"


def _render_rework_md(rework_id: str, base_feature_id: str) -> str:
    """Render the rework entry as a pointer stub — id + supersedes + spec_file.

    No body is copied. The base ``<F>.md`` stays the single source of truth;
    ``parse_feature_frontmatter`` follows ``## Spec File`` and merges the base
    body in under this rework id, so DAG/QA/regression see a fully-populated
    feature without any duplicated spec content.
    """
    return (
        f"## Id\n- {rework_id}\n\n"
        f"## Supersedes\n- {base_feature_id}\n\n"
        f"## Spec File\n- {base_feature_id}.md\n"
    )


def create_rework_entry(
    project_path: Path,
    base_feature_id: str,
    current_wave: int,
) -> dict:
    """Create the superseding rework entry and place it in the CURRENT wave."""
    aah_path = project_path / ".aah"
    features_dir = aah_path / "plan" / "features"

    source_file = find_feature_file(features_dir, base_feature_id)
    if source_file is None:
        return {"error": f"Source feature {base_feature_id} not found in {features_dir}"}

    source = load_feature_data(features_dir, base_feature_id)
    if source is None:
        return {"error": f"Could not parse source feature {base_feature_id}"}

    rework_id = next_rework_id(features_dir, base_feature_id)
    rework_file = features_dir / f"{rework_id}.md"
    fl_path = aah_path / "feature-list.json"
    dag_path = aah_path / "plan" / "dag.json"
    waves_path = aah_path / "plan" / "waves.json"
    rollback_paths = (fl_path, dag_path, waves_path)
    snapshots = {
        path: path.read_bytes() if path.exists() else None
        for path in rollback_paths
    }
    stage = "create rework entry"
    try:
        rework_file.write_text(
            _render_rework_md(rework_id, base_feature_id),
            encoding="utf-8",
        )

        result = {
            "rework_id": rework_id,
            "supersedes": base_feature_id,
            "spec_file": f"{base_feature_id}.md",
            "rework_file": str(rework_file),
            "prerequisites": sorted(source.get("dependencies", []) or []),
            "actions_taken": [f"Created rework entry {rework_id}.md"],
        }

        # Sync feature-list.json (new entry gets passes:false).
        stage = "sync feature-list.json"
        if fl_path.exists():
            from aah.core.common.feature_list import sync_features_from_yaml

            sync_features_from_yaml(features_dir, fl_path, feature_ids=[rework_id])
            result["actions_taken"].append("Synced feature-list.json")

        # Rebuild the DAG so the rework entry's edges come from the updated spec.
        stage = "rebuild dag.json"
        module_map = aah_path / "architecture" / "module-map.yaml"
        from aah.core.plan.build_dag import build_and_validate_dag

        dag_data = build_and_validate_dag(
            features_dir, module_map if module_map.is_file() else None
        )
        write_json(dag_data, dag_path)
        result["actions_taken"].append("Rebuilt dag.json")

        # Place the rework entry into the CURRENT wave (before it promotes). A
        # rework's prerequisites are all already built, so the current wave is
        # always DAG-valid for it.
        stage = "update waves.json"
        if waves_path.exists():
            from aah.core.plan.compute_waves import place_rework_entry

            waves_data = read_json(waves_path)
            place_rework_entry(
                waves_data, rework_id, result["prerequisites"], current_wave,
                current_wave_rework=True,
            )
            write_json(waves_data, waves_path)
            result["actions_taken"].append("Placed rework entry in current wave")
            result["waves"] = waves_data["waves"]

        stage = "regenerate checkpoint verification profiles"
        from aah.core.plan.determine_checkpoints import regenerate_checkpoint_config

        regenerate_checkpoint_config(project_path)
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            rework_file.unlink(missing_ok=True)
        except OSError as rollback_exc:
            rollback_errors.append(f"remove {rework_file}: {rollback_exc}")
        for path, content in snapshots.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            except OSError as rollback_exc:
                rollback_errors.append(f"restore {path}: {rollback_exc}")

        rollback_note = (
            "All rework changes were rolled back; retry is safe."
            if not rollback_errors
            else "Rollback was incomplete: " + "; ".join(rollback_errors)
        )
        return {
            "rework_id": rework_id,
            "supersedes": base_feature_id,
            "rolled_back": not rollback_errors,
            "error": (
                f"Could not {stage} for {rework_id}: {type(exc).__name__}: "
                f"{exc}. {rollback_note}"
            ),
        }
    result["actions_taken"].append("Regenerated checkpoint verification profiles")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a superseding rework feature entry")
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create", help="Create a rework entry for an implemented feature")
    create_p.add_argument("--project-path", type=Path, default=None)
    create_p.add_argument("--feature-id", type=str, required=True,
                          help="The implemented feature to supersede (e.g. F-MOD001-02)")
    create_p.add_argument("--current-wave", type=int, default=0,
                          help="Current wave index (rework placed no earlier than current_wave + 1)")

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "create":
        result = create_rework_entry(project_path, args.feature_id, args.current_wave)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(f"Created rework entry {result['rework_id']} (supersedes {result['supersedes']})", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
