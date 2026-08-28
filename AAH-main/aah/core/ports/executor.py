#!/usr/bin/env python3
"""The ports executor — the deterministic engine behind the bookend pattern.

Reads the framework catalog (read-only DEFINITION) and owns the project's
``.aah/port-registry.yaml`` (per-project STATE). Every subcommand prints a
JSON summary the caller re-renders as markdown (collapsed-Bash rule).

Subcommands (design §9):
    init                Materialize all `default` activities into a fresh
                        project registry (before /aah-discuss). Idempotent.
                        Wired into the `aah-init-project` phase entry step.
    compile             Read the discuss slug registry, resolve each fired
                        activity against the catalog, and APPEND the discuss-
                        triggered activities (defaults untouched). Fires on
                        activates[] (primary) OR a triggered_by answer match
                        (fallback for a missing activates[] link).
    before  --node <n>  Ordered `before` activities for node <n>, plus `within`
                        activities returned as a PLAN (to run later at anchors).
    after   --node <n>  Ordered `after` activities for node <n>.
    within  --node <n> --anchor <a>
                        Ordered `within` activities bound to (node, anchor) —
                        called when a core skill reaches an inline anchor marker.
    update  --activity <id> --status <s> [--artifact <p> ...]
                        Mutate one activity's runtime state.
    reconcile [--node <n>]
                        Safety net: mark any fired port whose `produces` all
                        exist on disk as completed (tagged reconciled=true, since
                        the timestamp is detection time, not true finish). Run at
                        the phase-close gate to backstop a forgotten `update`.
    check-guarantees --node <n> --position <p>
                        Verify each in-scope activity's `consumes` exist under
                        the project. Exit 0 / non-zero.

Inert by default: no registry → before/after return []; the whole mechanism is
a no-op on projects that activated no ports.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import require_project_path
from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.ports import catalog as cat
from aah.core.ports import ordering


RUNTIME_STATE_FIELDS = {
    "trigger_reason": None,
    "status": "pending",
    "started_at": None,
    "completed_at": None,
    "artifacts_produced": [],
}

VALID_STATUS = {"pending", "in-progress", "completed", "failed", "skipped", "deferred-stub"}


# ---------------------------------------------------------------------------
# Paths & helpers
# ---------------------------------------------------------------------------

def registry_path(project_root: Path) -> Path:
    # Ports state lives under .aah/ (the current project-state dir).
    return project_root / ".aah" / "port-registry.yaml"


def discuss_registry_path(project_root: Path) -> Path:
    # The discuss slug registry is written by core.discuss.registry, which now
    # targets .aah/discuss/. Prefer .aah/, else fall back to .rapids/ so compile
    # still finds registries written by older harness versions. Do NOT hardcode
    # .aah/ only — compile reads this and it must match where registry.py writes.
    aah_path = project_root / ".aah" / "discuss" / "decision-registry.yaml"
    if aah_path.exists():
        return aah_path
    return project_root / ".rapids" / "discuss" / "decision-registry.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry(project_root: Path) -> dict | None:
    """Return the project registry, or None if it does not exist (inert case)."""
    p = registry_path(project_root)
    if not p.exists():
        return None
    return read_yaml(p)


def _save_registry(project_root: Path, data: dict) -> None:
    write_yaml(data, registry_path(project_root))


def _with_runtime_state(activity: dict, catalog: dict) -> dict:
    """Copy a catalog activity into a self-contained registry entry.

    Full details are copied (not just the id) so an archived snapshot stays
    readable without the framework version.
    """
    entry = dict(activity)
    for field, default in RUNTIME_STATE_FIELDS.items():
        entry.setdefault(field, list(default) if isinstance(default, list) else default)
    # A stub ref can never be invoked — mark it so downstream never tries.
    if cat.is_stub(activity):
        entry["status"] = "deferred-stub"
    return entry


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def op_init(project_root: Path) -> dict:
    """Materialize all `default` activities. Idempotent — preserves runtime
    state of defaults already present; never touches discuss-triggered rows."""
    catalog = cat.load_catalog()
    existing = load_registry(project_root)

    if existing is None:
        data = {
            "schema_version": catalog.get("schema_version", "2.0"),
            "initialized_at": _now(),
            "compiled_at": None,
            "source_catalog": "_resources/_references/_ports/_registry.yaml",
            "spine_order": cat.spine_order(catalog),
            "node_anchors": cat.node_anchors(catalog),
            "activities": [],
        }
    else:
        data = existing
        data.setdefault("initialized_at", _now())

    present = {a.get("id") for a in data.get("activities", [])}
    added = []
    for act in cat.default_activities(catalog):
        if act.get("id") in present:
            continue  # idempotent — keep existing runtime state
        data["activities"].append(_with_runtime_state(act, catalog))
        added.append(act.get("id"))

    _save_registry(project_root, data)
    return {
        "action": "init",
        "registry": str(registry_path(project_root)),
        "defaults_added": added,
        "defaults_total": len(cat.default_activities(catalog)),
        "already_present": sorted(present & {a.get("id") for a in cat.default_activities(catalog)}),
    }


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

def _collect_activated_ids(discuss_registry: dict) -> dict[str, dict]:
    """Flatten activates[] across pre_resolved + decisions.

    Activation is id-based: each activates[] entry MUST be a catalog activity id
    (e.g. "ACT-UX"). Returns {activity_id: {"slug_id":..., "option":...}}
    recording which answer switched it on. This is the PRIMARY firing signal.

    A discuss-triggered port fires when its id appears here. When it does NOT —
    e.g. the discuss walk forgot to pass ``--activates-json`` and the array is
    empty — ``op_compile`` falls back to matching the catalog's ``triggered_by``
    rule against the recorded answer (see ``_fires_by_trigger``), so a correct
    answer still fires its port even if the explicit activates[] link is missing.
    """
    fired: dict[str, dict] = {}

    def absorb(slug_id: str, value, activates):
        for entry in activates or []:
            # id-based only. Tolerate a legacy binding dict by reading its `id`.
            aid = entry if isinstance(entry, str) else entry.get("id")
            if aid:
                fired.setdefault(aid, {"slug_id": slug_id, "option": value})

    for pr in discuss_registry.get("pre_resolved", []) or []:
        absorb(pr.get("slug_id"), pr.get("value"), pr.get("activates"))
    for d in discuss_registry.get("decisions", []) or []:
        absorb(d.get("slug_id"), d.get("response"), d.get("activates"))
    return fired


def _answers_map(discuss_registry: dict) -> dict:
    """Build a {slug_id: value} map from pre_resolved[] + decisions[].

    Kept local (mirrors ``guidance.collect_answers``) so the executor stays
    self-contained — the same reason ``_collect_activated_ids`` does not import
    guidance. Later entries win on a slug collision, matching answer recency.
    """
    answers: dict = {}
    for pr in discuss_registry.get("pre_resolved", []) or []:
        if pr.get("slug_id") is not None:
            answers[pr["slug_id"]] = pr.get("value")
    for d in discuss_registry.get("decisions", []) or []:
        if d.get("slug_id") is not None:
            answers[d["slug_id"]] = d.get("response")
    return answers


def _fires_by_trigger(activity: dict, answers: dict) -> dict | None:
    """Fallback firing signal: does the catalog ``triggered_by`` match an answer?

    Returns the matching trigger dict ({type, slug_id, option}) or None. Only
    ``discuss``-type triggers are considered (defaults never gate on an answer).
    Multi-select aware: when the recorded answer is a list, an option matches by
    membership; otherwise by equality — mirroring ``guidance._matches``.

    This is the SECOND check ORed with the primary activates[] signal in
    ``op_compile``. It only ever fires MORE ports, never suppresses one.
    """
    for tb in cat.triggers(activity):
        if tb.get("type") != "discuss":
            continue
        slug_id = tb.get("slug_id")
        option = tb.get("option")
        if slug_id is None or option in (None, ""):
            continue
        actual = answers.get(slug_id)
        if actual is None:
            continue
        if (option in actual) if isinstance(actual, list) else (actual == option):
            return tb
    return None


def op_compile(project_root: Path) -> dict:
    """Merge discuss-triggered activities into the init'd registry.

    A discuss-triggered activity fires when EITHER signal is present:
      1. PRIMARY — its catalog id appears in some decision's ``activates[]``
         (the explicit, LLM-authored link written during the discuss walk).
      2. FALLBACK — its catalog ``triggered_by`` rule matches a recorded answer,
         even when ``activates[]`` is empty. This catches the case where the
         discuss walk answered correctly but forgot to pass ``--activates-json``,
         so a port is never silently missed on account of that omission.

    The two are ORed; the primary signal is preserved unchanged, so a correctly
    activated port behaves exactly as before. Ports that had to use the fallback
    are surfaced in ``fired_via_fallback`` + ``missing_activates`` (fire-only —
    the discuss registry is never modified) so the gap stays visible.
    """
    catalog = cat.load_catalog()

    dpath = discuss_registry_path(project_root)
    if not dpath.exists():
        return {"action": "compile", "error": f"discuss registry not found at {dpath}"}
    discuss_reg = read_yaml(dpath)

    fired_via_activates = _collect_activated_ids(discuss_reg)
    answers = _answers_map(discuss_reg)

    # Ensure defaults exist (compile is safe even if init was skipped).
    data = load_registry(project_root)
    if data is None:
        op_init(project_root)
        data = load_registry(project_root)

    present = {a.get("id") for a in data.get("activities", [])}
    fired_ids: list[str] = []
    skipped_ids: list[str] = []
    fired_via_fallback: list[str] = []
    missing_activates: list[dict] = []

    to_order: list[dict] = []
    for act in cat.discuss_activities(catalog):
        aid = act.get("id")

        # Primary: id appears in some decision's activates[] (explicit link).
        activation = fired_via_activates.get(aid)
        # Fallback: catalog triggered_by matches a recorded answer, even when
        # activates[] was empty (LLM omitted --activates-json during the walk).
        trigger_hit = None if activation is not None else _fires_by_trigger(act, answers)
        did_fire = activation is not None or trigger_hit is not None

        if aid in present:
            continue  # already materialized (e.g. re-compile) — leave as-is

        entry = _with_runtime_state(act, catalog)
        if did_fire:
            if activation is not None:
                entry["trigger_reason"] = (
                    f"activated via activates[] (answer to '{activation['slug_id']}')"
                )
            else:
                # Fallback fired it — record the gap so it is auditable in the
                # port registry, and report it up for a user-facing warning.
                entry["trigger_reason"] = (
                    f"activated via triggered_by fallback: "
                    f"{trigger_hit['slug_id']}={trigger_hit['option']} — "
                    f"activates[] was empty in decision-registry.yaml"
                )
                fired_via_fallback.append(aid)
                missing_activates.append({
                    "activity_id": aid,
                    "slug_id": trigger_hit["slug_id"],
                    "option": trigger_hit["option"],
                })
            if entry["status"] != "deferred-stub":
                entry["status"] = "pending"
            to_order.append(entry)
            fired_ids.append(aid)
        else:
            entry["trigger_reason"] = (
                "neither activates[] nor a triggered_by answer match — did not fire"
            )
            entry["status"] = "skipped"
            data["activities"].append(entry)
            skipped_ids.append(aid)

    # Resolve order across the fired discuss activities, then append.
    ordered = ordering.resolve_order(to_order, cat.spine_order(catalog))
    data["activities"].extend(ordered)

    data["compiled_at"] = _now()
    _save_registry(project_root, data)

    return {
        "action": "compile",
        "registry": str(registry_path(project_root)),
        "fired": fired_ids,
        "skipped": skipped_ids,
        "fired_via_fallback": fired_via_fallback,
        "missing_activates": missing_activates,
        "plan": _plan_view(data),
    }


def _plan_view(data: dict) -> list[dict]:
    """A compact, ordered view of the whole registry for the caller to render."""
    return [
        {
            "id": a.get("id"),
            "ref": a.get("ref"),
            "type": a.get("type"),
            "target": a.get("target"),
            "position": a.get("position"),
            "anchor": a.get("anchor"),
            "status": a.get("status"),
        }
        for a in data.get("activities", [])
    ]


# ---------------------------------------------------------------------------
# before / after
# ---------------------------------------------------------------------------

def _actionable(activity: dict, data: dict | None = None) -> dict:
    out = {
        "id": activity.get("id"),
        "ref": activity.get("ref"),
        "type": activity.get("type"),
        "impl_status": activity.get("impl_status", "available"),
        "name": activity.get("id"),
        "description": activity.get("trigger_reason", ""),
        "position": activity.get("position"),
        "anchor": activity.get("anchor"),
        "consumes": activity.get("consumes", []),
        "produces": activity.get("produces", []),
        "status": activity.get("status"),
    }
    # For `within` ports, surface WHERE the anchor sits (from node_anchors'
    # description) so the core skill can self-localize — no inline marker needed.
    if activity.get("position") == "within" and activity.get("anchor") and data is not None:
        loc = cat.anchor_description(
            data.get("node_anchors", {}) or {},
            activity.get("target"),
            activity.get("anchor"),
        )
        if loc:
            out["anchor_location"] = loc.strip()
    return out


def _node_activities(data: dict, node: str, positions: set[str]) -> list[dict]:
    acts = [
        a for a in data.get("activities", [])
        if a.get("target") == node
        and a.get("position") in positions
        and a.get("status") not in ("skipped",)
    ]
    ordered = ordering.resolve_order(acts, data.get("spine_order", []))
    return ordered


def op_before(project_root: Path, node: str) -> dict:
    data = load_registry(project_root)
    if data is None:
        return {"node": node, "position": "before", "activities": []}
    # `before` returns before + within (within tagged, carries anchor).
    acts = _node_activities(data, node, {"before", "within"})
    return {
        "node": node,
        "position": "before",
        "activities": [_actionable(a, data) for a in acts],
    }


def op_after(project_root: Path, node: str) -> dict:
    data = load_registry(project_root)
    if data is None:
        return {"node": node, "position": "after", "activities": []}
    acts = _node_activities(data, node, {"after"})
    return {
        "node": node,
        "position": "after",
        "activities": [_actionable(a, data) for a in acts],
    }


def op_within(project_root: Path, node: str, anchor: str) -> dict:
    """Ordered `within` activities bound to (node, anchor).

    Called when a core skill reaches the anchor point (self-localized from the
    plan's anchor_location). Returns only the ports whose target==node,
    position==within, anchor==<anchor>. Empty list → nothing planned for this
    anchor (skill proceeds with zero overhead).
    """
    data = load_registry(project_root)
    if data is None:
        return {"node": node, "anchor": anchor, "activities": []}
    acts = [
        a for a in _node_activities(data, node, {"within"})
        if a.get("anchor") == anchor
    ]
    return {
        "node": node,
        "anchor": anchor,
        "activities": [_actionable(a, data) for a in acts],
    }


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def op_update(project_root: Path, activity_id: str, status: str,
              artifacts: list[str] | None = None) -> dict:
    if status not in VALID_STATUS:
        return {"action": "update", "error": f"invalid status '{status}'"}
    data = load_registry(project_root)
    if data is None:
        return {"action": "update", "error": "no port registry — run init first"}

    for a in data.get("activities", []):
        if a.get("id") == activity_id:
            prior = a.get("status")
            a["status"] = status
            now = _now()
            if status == "in-progress" and not a.get("started_at"):
                a["started_at"] = now
            if status in ("completed", "failed", "skipped"):
                # Back-fill started_at if the caller went straight to a terminal
                # state without an explicit in-progress transition (the mandated
                # flow is in-progress→completed, but reconcile and older callers
                # may jump straight to completed). Skipped activities never ran,
                # so leave their started_at null.
                if status in ("completed", "failed") and not a.get("started_at"):
                    a["started_at"] = now
                a["completed_at"] = now
            if artifacts:
                merged = list(a.get("artifacts_produced", []) or [])
                for art in artifacts:
                    if art not in merged:
                        merged.append(art)
                a["artifacts_produced"] = merged
            _save_registry(project_root, data)
            return {"action": "update", "activity": activity_id,
                    "from": prior, "to": status,
                    "artifacts_produced": a.get("artifacts_produced", [])}

    return {"action": "update", "error": f"no activity with id '{activity_id}'"}


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

def op_reconcile(project_root: Path, node: str | None = None) -> dict:
    """Mechanical safety net for the main-loop firing model.

    A port is invoked by the core skill's main loop, which is then expected to
    `update` it to completed. If it forgets, this closes the gap: any fired port
    (status pending/in-progress) whose declared `produces` ALL exist on disk is
    marked completed here. The `completed_at` is stamped at reconcile time, so we
    also tag the entry `reconciled: true` + `reconciled_at` — a signal to the
    reader that the activity actually finished a little EARLIER (when its
    artifacts were written) and this timestamp is the detection time, not the
    true completion time.

    Ports with no `produces` cannot be auto-detected — left untouched (the
    explicit main-loop `update` remains the source of truth for those). Skipped
    and deferred-stub ports are never reconciled.
    """
    data = load_registry(project_root)
    if data is None:
        return {"action": "reconcile", "reconciled": [], "note": "no registry"}

    reconciled: list[str] = []
    for a in data.get("activities", []):
        if node is not None and a.get("target") != node:
            continue
        if a.get("status") in ("completed", "skipped", "deferred-stub"):
            continue
        produces = a.get("produces", []) or []
        if not produces:
            continue  # nothing to detect — explicit update owns these
        if not all(_produce_exists(project_root, p) for p in produces):
            continue  # not all artifacts present — genuinely not done

        now = _now()
        a["status"] = "completed"
        if not a.get("started_at"):
            a["started_at"] = now
        a["completed_at"] = now
        a["reconciled"] = True          # completed_at is detection time, not true finish
        a["reconciled_at"] = now
        merged = list(a.get("artifacts_produced", []) or [])
        for p in produces:
            if p not in merged:
                merged.append(p)
        a["artifacts_produced"] = merged
        reconciled.append(a.get("id"))

    if reconciled:
        _save_registry(project_root, data)
    return {"action": "reconcile", "node": node, "reconciled": reconciled}


def _produce_exists(project_root: Path, produced: str) -> bool:
    """A produces path may be given relative to the project root or .aah/."""
    p = str(produced)
    return (project_root / p).exists() or (project_root / ".aah" / p).exists()


# ---------------------------------------------------------------------------
# check-guarantees
# ---------------------------------------------------------------------------

def op_check_guarantees(project_root: Path, node: str, position: str) -> dict:
    """Verify each in-scope activity's `consumes` exist under the project.

    v1 precondition model: every path listed in `consumes` must exist relative
    to the project's .aah/ (or .rapids/ legacy, or the project root).
    """
    data = load_registry(project_root)
    if data is None:
        return {"node": node, "position": position, "ok": True, "missing": []}

    positions = {"before", "within"} if position == "before" else {position}
    missing: list[dict] = []

    for a in data.get("activities", []):
        if a.get("target") != node or a.get("position") not in positions:
            continue
        if a.get("status") in ("skipped", "deferred-stub"):
            continue
        for dep in a.get("consumes", []) or []:
            if not _consume_exists(project_root, dep):
                missing.append({"activity": a.get("id"), "consumes": dep})

    return {"node": node, "position": position, "ok": not missing, "missing": missing}


def _consume_exists(project_root: Path, dep: str) -> bool:
    """A consume path may be given relative to the project root or .aah/."""
    dep_str = str(dep)
    candidates = [
        project_root / dep_str,
        project_root / ".aah" / dep_str
    ]
    return any(c.exists() for c in candidates)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AAH ports executor")
    parser.add_argument("--project-path", type=Path, help="Override active project resolution")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Materialize default activities (before discuss)")
    sub.add_parser("compile", help="Merge discuss-triggered activities (discuss Step 13)")

    p_before = sub.add_parser("before", help="Ordered before+within activities for a node")
    p_before.add_argument("--node", required=True)

    p_after = sub.add_parser("after", help="Ordered after activities for a node")
    p_after.add_argument("--node", required=True)

    p_within = sub.add_parser("within", help="Ordered within activities for a (node, anchor)")
    p_within.add_argument("--node", required=True)
    p_within.add_argument("--anchor", required=True)

    p_update = sub.add_parser("update", help="Update one activity's runtime state")
    p_update.add_argument("--activity", required=True)
    p_update.add_argument("--status", required=True)
    p_update.add_argument("--artifact", action="append", default=[])

    p_rec = sub.add_parser("reconcile", help="Mark fired ports completed when their produces exist")
    p_rec.add_argument("--node", help="Restrict to one core node (default: all)")

    p_cg = sub.add_parser("check-guarantees", help="Verify consumes exist for a coordinate")
    p_cg.add_argument("--node", required=True)
    p_cg.add_argument("--position", required=True, choices=["before", "within", "after"])

    args = parser.parse_args()
    project_root = require_project_path(args.project_path)

    if args.command == "init":
        result = op_init(project_root)
    elif args.command == "compile":
        result = op_compile(project_root)
    elif args.command == "before":
        result = op_before(project_root, args.node)
    elif args.command == "after":
        result = op_after(project_root, args.node)
    elif args.command == "within":
        result = op_within(project_root, args.node, args.anchor)
    elif args.command == "update":
        result = op_update(project_root, args.activity, args.status, args.artifact)
    elif args.command == "reconcile":
        result = op_reconcile(project_root, args.node)
    elif args.command == "check-guarantees":
        result = op_check_guarantees(project_root, args.node, args.position)
    else:
        parser.error("unknown command")

    print(json.dumps(result, indent=2, default=str))

    # check-guarantees drives an executor-side abort: non-zero on missing.
    # Use exit code 1 (not 2) — the `aah run` wrapper reinterprets exit code 2
    # as an argparse rejection and swallows it, but passes 0/1 through cleanly.
    if args.command == "check-guarantees" and not result.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    main()
