#!/usr/bin/env python3
"""Slug decision-registry CRUD for /aah-discuss.

Owns .aah/discuss/decision-registry.yaml. The registry is slug-keyed
(never DDR-keyed), holds pre-resolved mandatory answers + walk decisions +
deferred ideas + a constraint_audit block, and is written incrementally
after each gray area (anti-data-loss checkpoint per HTML §Step-9).

Subcommands:
    init                Create an empty registry. Idempotent: if one already
                        exists it is reused as-is (--force rebuilds it empty).
    set-domains         Non-destructively set domain_ids/archetypes on an
                        existing registry (preserves pre_resolved/decisions).
    add-preresolved     Append a slug into `pre_resolved[]` (mandatory-Q
                        answer, brownfield fact, etc).
    add-decision        Append a walk decision into `decisions[]`.
    revise              Overwrite an existing decision; preserves the prior
                        answer under `revised_from` (used by the §10a
                        surgical re-float loop).
    read                Dump the registry as JSON.
    set-constraint-audit
                        Write the constraint_audit block (called by
                        validate_constraints).
    add-deferred        Append a string to deferred_ideas[].
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from aah.core.common.config import require_project_path
from aah.core.common.io_utils import read_yaml, write_yaml


def registry_path(project_root: Path) -> Path:
    return project_root / ".aah" / "discuss" / "decision-registry.yaml"


def _load(project_root: Path) -> dict:
    p = registry_path(project_root)
    if not p.exists():
        raise FileNotFoundError(
            f"decision-registry.yaml not found at {p}. Run `discuss.registry init` first."
        )
    return read_yaml(p)


def _save(project_root: Path, data: dict) -> None:
    p = registry_path(project_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _recompute_active_references(data)
    write_yaml(data, p)


def _recompute_active_references(data: dict) -> None:
    """Refresh data['active_references'] from current answers + selected overlays.

    Called on every _save so the block is always consistent with the decisions
    just written. It is the single auditable source the researcher agent reads —
    one structure holding every reference the user + harness have activated:
      * guidance-file references (chosen-option references[] + applicable
        always_references[]), each annotated with the slug/value that activated it
      * domain-brief + archetype-playbook overlay paths from Step 5.4 selections

    Best-effort: if the guidance/overlay resources can't be resolved (e.g. tests
    running outside the framework tree), the block is left as an empty scaffold
    rather than breaking the save.
    """
    block = {
        "computed_at": date.today().isoformat(),
        "guidance_references": [],   # [{source, kind, exists, activated_by[]}]
        "domain_brief_paths": [],
        "archetype_playbook_paths": [],
    }
    # Lazy imports: guidance/overlays are heavier and only needed here. guidance
    # does not import registry, so there is no import cycle.
    try:
        from aah.core.discuss import guidance as _g
        answers = _g.collect_answers(data)
        root = _g.find_framework_root()
        guidance = _g.load_guidance()
        block["guidance_references"] = _g.active_references_detailed(guidance, answers, root)
    except Exception:
        pass
    try:
        from aah.core.discuss import overlays as _ov
        overlay = _ov.build_overlay(data.get("domain_ids") or [], data.get("archetypes") or [])
        block["domain_brief_paths"] = overlay.get("domain_brief_paths", [])
        block["archetype_playbook_paths"] = overlay.get("archetype_playbook_paths", [])
    except Exception:
        pass
    data["active_references"] = block


def _empty_registry(
    project_name: str,
    complexity: str,
    archetype: str,
    domain_ids: list[str] | None = None,
    archetypes: list[str] | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "project": project_name,
        "complexity": complexity,
        # Scalar `archetype` is retained for back-compat with the DDR machinery
        # (which reads project.archetype). It is backfilled from archetypes[0]
        # when a list is supplied; otherwise it keeps the passed/default value.
        "archetype": archetype,
        "domain_ids": domain_ids or [],
        "archetypes": archetypes or [],
        "started_at": date.today().isoformat(),
        "pre_resolved": [],
        "decisions": [],
        "deferred_ideas": [],
        "constraint_audit": {"passed": None, "violations": []},
    }


# ─── operations ──────────────────────────────────────────────────────────

def op_init(
    project_root: Path,
    project_name: str,
    complexity: str,
    archetype: str,
    force: bool,
    domain_ids: list[str] | None = None,
    archetypes: list[str] | None = None,
) -> dict:
    p = registry_path(project_root)
    if p.exists() and not force:
        # Idempotent: an existing registry is reused as-is (never clobbered).
        # project_name/complexity/domains passed on a redundant init are ignored —
        # domains are set via `set-domains`, not init.
        return {"path": str(p), "status": "exists"}
    data = _empty_registry(project_name, complexity, archetype, domain_ids, archetypes)
    _save(project_root, data)
    return {"path": str(p), "status": "created"}


def op_set_domains(project_root: Path, domain_ids: list[str], archetypes: list[str]) -> dict:
    """Set domain_ids/archetypes on an EXISTING registry without destroying it.

    Non-destructive alternative to `init --force` for recording confirmed
    domain/archetype selections. `_save` recomputes active_references, so the
    overlay block is rebuilt from the new domains.
    """
    data = _load(project_root)
    data["domain_ids"] = domain_ids
    data["archetypes"] = archetypes
    if archetypes:  # backfill scalar (same rule as op_init)
        data["archetype"] = archetypes[0]
    _save(project_root, data)
    return {"status": "updated", "domain_ids": domain_ids, "archetypes": archetypes}


def op_add_preresolved(project_root: Path, slug_id: str, value: Any, source: str,
                      locked: bool = True,
                      notes: str | None = None, activates: list[str] | None = None,
                      source_detail: str | None = None) -> dict:
    data = _load(project_root)
    entry: dict[str, Any] = {
        "slug_id": slug_id,
        "value": value,
        "source": source,
        "locked": locked,
        "activates": list(activates or []),
    }

    # Combine notes + source_detail; source_detail always precedes notes if both given.
    note_parts = []
    if source_detail:
        note_parts.append(f"source-detail: {source_detail}")
    if notes:
        note_parts.append(notes)
    if note_parts:
        entry["notes"] = " · ".join(note_parts)

    existing = _find_by_slug(data["pre_resolved"], slug_id)
    if existing is not None:
        data["pre_resolved"] = [e for e in data["pre_resolved"] if e["slug_id"] != slug_id]
    data["pre_resolved"].append(entry)
    _save(project_root, data)
    return {"slug_id": slug_id, "status": "appended" if existing is None else "replaced"}


def op_add_decision(project_root: Path, slug_id: str, area: str, question: str,
                    options_presented: list, options_eliminated: list,
                    response: Any, response_type: str, source: str,
                    activates: list[str] | None = None,
                    notes: str | None = None, emergent: bool = False) -> dict:
    data = _load(project_root)

    if _find_by_slug(data["decisions"], slug_id) is not None:
        raise ValueError(
            f"decision {slug_id!r} already exists — use `revise` to overwrite it."
        )

    entry: dict[str, Any] = {
        "slug_id": slug_id,
        "area": area,
        "question": question,
        "options_presented": options_presented,
        "options_eliminated": options_eliminated,
        "response": response,
        "response_type": response_type,
        "source": source,
        "activates": list(activates or []),
    }
    if notes:
        entry["notes"] = notes
    if emergent:
        entry["emergent"] = True

    data["decisions"].append(entry)
    _save(project_root, data)
    return {"slug_id": slug_id, "status": "appended"}


def op_revise(project_root: Path, slug_id: str, response: Any,
              source: str = "user", reason: str | None = None) -> dict:
    data = _load(project_root)
    existing = _find_by_slug(data["decisions"], slug_id)
    if existing is None:
        raise ValueError(f"no decision found with slug_id {slug_id!r} to revise")

    prior_response = existing.get("response")
    existing.setdefault("revised_from", []).append({
        "response": prior_response,
        "revised_at": date.today().isoformat(),
        "reason": reason or "surgical re-float",
    })
    existing["response"] = response
    existing["source"] = source
    _save(project_root, data)
    return {"slug_id": slug_id, "status": "revised", "prior_response": prior_response}


def op_read(project_root: Path) -> dict:
    return _load(project_root)


def op_set_constraint_audit(project_root: Path, audit_json: str) -> dict:
    data = _load(project_root)
    data["constraint_audit"] = json.loads(audit_json)
    _save(project_root, data)
    return {"status": "written", "passed": data["constraint_audit"].get("passed")}


def op_add_deferred(project_root: Path, note: str) -> dict:
    data = _load(project_root)
    data.setdefault("deferred_ideas", []).append(note)
    _save(project_root, data)
    return {"status": "appended", "count": len(data["deferred_ideas"])}


def _find_by_slug(items: list[dict], slug_id: str) -> dict | None:
    for item in items or []:
        if item.get("slug_id") == slug_id:
            return item
    return None


# ─── CLI ─────────────────────────────────────────────────────────────────

def _parse_response(value: str) -> Any:
    """Auto-coerce a `--response` argument: JSON if it parses, else the raw string."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated CLI value into a clean list (empty/None → [])."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Discuss decision-registry CRUD")
    parser.add_argument("--project-path", type=Path, help="Override active project resolution")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--project-name", required=True)
    p_init.add_argument("--complexity", default="poc")
    # --archetype: back-compat single-value alias (DDR machinery reads the
    # scalar project.archetype). Prefer --archetypes for the multi-select flow;
    # when --archetypes is given, its first element backfills the scalar.
    p_init.add_argument("--archetype", default="ai-applications")
    p_init.add_argument("--archetypes", default="",
                        help="Comma-separated archetype keys (multi-select). "
                             "First element backfills the scalar --archetype.")
    p_init.add_argument("--domains", default="",
                        help="Comma-separated domain node ids (multi-select).")
    p_init.add_argument("--force", action="store_true")

    p_sd = sub.add_parser("set-domains",
                          help="Non-destructively set domain_ids/archetypes on an existing registry")
    p_sd.add_argument("--domains", default="",
                      help="Comma-separated domain node ids (multi-select).")
    p_sd.add_argument("--archetypes", default="",
                      help="Comma-separated archetype keys. First element backfills the scalar archetype.")

    # Canonical source enum — used across all subcommands.
    # user      = user answered interactively via AskUserQuestion
    # inferred  = Claude derived from brief / project-intent.yaml / knowledge base
    # codebase  = extracted from .aah/codebase-intel/* during brownfield Step 2b
    _SOURCE_CHOICES = ["user", "inferred", "codebase"]

    p_pre = sub.add_parser("add-preresolved")
    p_pre.add_argument("--slug-id", required=True)
    p_pre.add_argument("--value", required=True, help="JSON scalar/list or raw string")
    p_pre.add_argument("--source", required=True, choices=_SOURCE_CHOICES,
                       help="Canonical origin: user | inferred | codebase")
    p_pre.add_argument("--source-detail",
                       help="Optional pointer to the specific origin (e.g. 'knowledge/project-brief.md' "
                            "or 'codebase-intel/tech-stack.md'). Written to the entry's notes field.")
    p_pre.add_argument("--locked", action="store_true", default=True)
    p_pre.add_argument("--notes")
    p_pre.add_argument("--activates-json", default="[]")

    p_dec = sub.add_parser("add-decision")
    p_dec.add_argument("--slug-id", required=True)
    p_dec.add_argument("--area", required=True)
    p_dec.add_argument("--question", required=True)
    p_dec.add_argument("--options-presented-json", default="[]")
    p_dec.add_argument("--options-eliminated-json", default="[]")
    p_dec.add_argument("--response", required=True, help="JSON scalar/list or raw string")
    p_dec.add_argument("--response-type", default="single-select",
                       choices=["single-select", "multi-select", "free-text"])
    p_dec.add_argument("--source", default="user", choices=_SOURCE_CHOICES)
    p_dec.add_argument("--activates-json", default="[]")
    p_dec.add_argument("--notes")
    p_dec.add_argument("--emergent", action="store_true")

    p_rev = sub.add_parser("revise")
    p_rev.add_argument("--slug-id", required=True)
    p_rev.add_argument("--response", required=True)
    p_rev.add_argument("--source", default="user", choices=_SOURCE_CHOICES)
    p_rev.add_argument("--reason")

    sub.add_parser("read")

    p_aud = sub.add_parser("set-constraint-audit")
    p_aud.add_argument("--audit-json", required=True)

    p_def = sub.add_parser("add-deferred")
    p_def.add_argument("--note", required=True)

    args = parser.parse_args()

    project_root = require_project_path(args.project_path)

    if args.command == "init":
        domain_ids = _split_csv(args.domains)
        archetypes = _split_csv(args.archetypes)
        # Backfill the scalar archetype from the first list element when supplied,
        # else keep the --archetype value (default ai-applications) for back-compat.
        scalar_archetype = archetypes[0] if archetypes else args.archetype
        result = op_init(
            project_root, args.project_name, args.complexity, scalar_archetype,
            args.force, domain_ids=domain_ids, archetypes=archetypes,
        )
    elif args.command == "set-domains":
        result = op_set_domains(project_root, _split_csv(args.domains), _split_csv(args.archetypes))
    elif args.command == "add-preresolved":
        result = op_add_preresolved(
            project_root, args.slug_id, _parse_response(args.value), args.source,
            args.locked, args.notes, json.loads(args.activates_json),
            source_detail=args.source_detail,
        )
    elif args.command == "add-decision":
        result = op_add_decision(
            project_root, args.slug_id, args.area, args.question,
            json.loads(args.options_presented_json), json.loads(args.options_eliminated_json),
            _parse_response(args.response), args.response_type, args.source,
            json.loads(args.activates_json), args.notes, args.emergent,
        )
    elif args.command == "revise":
        result = op_revise(project_root, args.slug_id, _parse_response(args.response), args.source, args.reason)
    elif args.command == "read":
        result = op_read(project_root)
    elif args.command == "set-constraint-audit":
        result = op_set_constraint_audit(project_root, args.audit_json)
    elif args.command == "add-deferred":
        result = op_add_deferred(project_root, args.note)
    else:
        parser.error("unknown command")

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
