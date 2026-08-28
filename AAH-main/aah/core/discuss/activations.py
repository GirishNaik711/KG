#!/usr/bin/env python3
"""Activation resolver for /aah-discuss (Step 13).

Activation is now **ports-driven**: the authoritative resolver is
`core.ports.executor compile`, which reads each decision's `activates[]` (catalog
activity ids, e.g. ACT-UX, ACT-ACCESS), joins them against the ports catalog, and
materializes the ordered plan into `.aah/port-registry.yaml`. Adding a new port
requires NO change here — a catalog entry + a discuss-guidance `activates:[<id>]`
is enough.

This module:
  1. Runs `ports.executor compile` (the source of truth for what activates).
  2. Additionally flattens the raw `activates[]` from the slug registry as
     provenance (which slug switched each id on) for the human summary.

There is no hardcoded skill allow-list anymore — the old `PASS_2_SCOPE`
(`/aah-access`, `/aah-ui`) whitelist was a pre-ports scaffold and has been
removed. Activations are PROMPTS/plan, not auto-runs — the executor bookends fire
the ports later, at each spine node.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aah.core.common.config import require_project_path
from aah.core.common.io_utils import read_yaml


def resolve(project_root: Path) -> dict:
    """Flatten activates[] across pre_resolved + decisions as provenance.

    Returns {activity_id: [slug_ids that named it]} — no whitelist filtering.
    The ports catalog (via compile) decides what actually fires; this is just
    the "which answer switched on which id" trace for display.

    Reads the discuss registry via the same path resolution the ports executor
    uses (.aah/discuss/ preferred, .rapids/discuss/ legacy fallback), so it works
    regardless of where the discuss registry landed. Missing registry → empty.
    """
    from aah.core.ports import executor as ports_executor
    dpath = ports_executor.discuss_registry_path(project_root)
    data = read_yaml(dpath) if dpath.exists() else {}

    activated: dict[str, set[str]] = {}

    def _absorb(slug: str, activates: list | None):
        for entry in activates or []:
            aid = entry if isinstance(entry, str) else (entry.get("id") if isinstance(entry, dict) else None)
            if aid:
                activated.setdefault(aid, set()).add(slug)

    for entry in data.get("pre_resolved", []) or []:
        _absorb(entry["slug_id"], entry.get("activates"))
    for dec in data.get("decisions", []) or []:
        _absorb(dec["slug_id"], dec.get("activates"))

    return {
        "activated": {aid: sorted(list(slugs)) for aid, slugs in sorted(activated.items())},
    }


def compile_ports(project_root: Path) -> dict | None:
    """Delegate to the ports executor to materialize the ordered port plan.

    Returns the compile summary, or None if the ports catalog/executor is
    unavailable (older harness, no catalog) — activations still work without it.
    """
    try:
        from aah.core.ports import executor as ports_executor
        return ports_executor.op_compile(project_root)
    except Exception as e:  # ports mechanism optional — never break activations
        return {"action": "compile", "error": f"port compile skipped: {e}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Discuss activation resolver (ports-driven)")
    parser.add_argument("--project-path", type=Path)
    parser.add_argument("--json", action="store_true", help="Emit JSON; default is human summary")
    parser.add_argument("--no-compile", action="store_true",
                        help="Skip the ports.executor compile step (provenance flatten only)")
    args = parser.parse_args()

    project_root = require_project_path(args.project_path)
    result = resolve(project_root)

    ports = None if args.no_compile else compile_ports(project_root)
    if ports is not None:
        result["ports"] = ports

    if args.json:
        print(json.dumps(result, indent=2))
        return

    # Human summary — the port compile plan is authoritative.
    if ports and not ports.get("error"):
        fired = ports.get("fired", [])
        skipped = ports.get("skipped", [])
        if fired:
            print("Activated ports (compiled to .aah/port-registry.yaml):")
            for aid in fired:
                slugs = result["activated"].get(aid, [])
                via = f"  ← {', '.join(slugs)}" if slugs else ""
                print(f"  · {aid}{via}")
        else:
            print("No ports activated.")
        if skipped:
            print(f"\nNot activated (available but no answer named them): {', '.join(skipped)}")
        # Fire-only fallback: a port fired on a triggered_by answer match because
        # its activates[] was empty. Surface the gap so the user can make the
        # link explicit if they choose to (the discuss registry is not modified).
        for miss in ports.get("missing_activates", []) or []:
            print(
                f"\n⚠️  {miss['activity_id']} fired via triggered_by fallback — its "
                f"decision ({miss['slug_id']}={miss['option']}) had an empty activates[]. "
                f"For your awareness, you can add activates: [{miss['activity_id']}] in "
                f"decision-registry.yaml to make it explicit."
            )
    elif ports and ports.get("error"):
        # Ports unavailable — fall back to raw provenance so the user still sees intent.
        print(f"(ports: {ports['error']})")
        if result["activated"]:
            print("Recorded activates[] (unresolved — no ports catalog):")
            for aid, slugs in result["activated"].items():
                print(f"  · {aid}  ← {', '.join(slugs)}")
        else:
            print("No activations recorded.")
    else:
        # --no-compile: show raw provenance only.
        if result["activated"]:
            print("Recorded activates[]:")
            for aid, slugs in result["activated"].items():
                print(f"  · {aid}  ← {', '.join(slugs)}")
        else:
            print("No activations recorded.")


if __name__ == "__main__":
    main()
