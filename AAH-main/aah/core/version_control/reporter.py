#!/usr/bin/env python3
"""LLM-facing renderers — the [AAH-VC] structured blocks.

Mirrors the cli.py _claude_retry_hint convention: a parseable block addressed to
Claude, printed to stdout, with the script exiting 0 (non-blocking). Any skill
that runs apply/delta therefore surfaces conflicts to the LLM automatically.
"""

from __future__ import annotations

import json

from aah.core.version_control.models import ConflictRecord


def _classify(conflict: ConflictRecord) -> tuple[str, str]:
    """Distinguish a true both-sides conflict from a one-sided proposal.

    Returns (headline, guidance). The applier records both true conflicts AND
    remote-only structural inbound changes (the latter need LLM review because
    they affect the DAG), so the message must tell them apart.
    """
    local_changed = conflict.local != conflict.base
    remote_changed = conflict.remote != conflict.base
    if local_changed and remote_changed:
        return (
            "CONFLICT — both sides changed since last sync. Claude: decide & call `resolve`.",
            "Produce a merge that preserves the intent of BOTH sides where possible.",
        )
    if remote_changed and not local_changed:
        return (
            "STRUCTURAL INBOUND — only the tracker changed (a DAG-affecting field). "
            "Claude: review & call `resolve` (accept remote, keep local, or merge).",
            "The tracker proposes a structural change. Accept it with --resolution remote, "
            "reject it with --resolution local, or merge. Resolution stages a aah-plan "
            "re-entry to recompute the DAG/waves.",
        )
    # local-only or already-equal shouldn't normally be recorded; show neutrally.
    return (
        "REVIEW — recorded change on a structural field. Claude: decide & call `resolve`.",
        "Choose local, remote, or a merge.",
    )


def _fmt_value(value) -> str:
    if value is None:
        return "    _empty_"
    if isinstance(value, list):
        if not value:
            return "    _empty_"
        lines = []
        for v in value:
            if isinstance(v, (dict, list)):
                lines.append("    - " + json.dumps(v, ensure_ascii=False))
            else:
                lines.append(f"    - {v}")
        return "\n".join(lines)
    if isinstance(value, dict):
        return "    " + json.dumps(value, ensure_ascii=False)
    # scalar / multi-line string
    return "\n".join(f"    {line}" for line in str(value).splitlines()) or "    _empty_"


def render_conflict(conflict: ConflictRecord) -> str:
    issue_str = ""
    if conflict.remote_ref and conflict.remote_ref.number is not None:
        issue_str = f"   (issue #{conflict.remote_ref.number})"
    resolve_cmd = (
        "    aah run core.version_control.cli resolve \\\n"
        f"      --conflict-id {conflict.conflict_id} \\\n"
        "      --resolution merged --value-file /tmp/resolve.json"
    )
    if conflict.field_class == "cosmetic":
        resolve_cmd = (
            "    aah run core.version_control.cli resolve \\\n"
            f"      --conflict-id {conflict.conflict_id} \\\n"
            '      --resolution merged --value "<merged text>"'
        )
    headline, guidance = _classify(conflict)
    return "\n".join([
        f"[AAH-VC] {headline}",
        f"  conflict_id : {conflict.conflict_id}",
        f"  feature     : {conflict.feature_id}{issue_str}",
        f"  field       : {conflict.field}   [{conflict.field_class}]",
        "  -- BASE  (last synced) --",
        _fmt_value(conflict.base),
        "  -- LOCAL (feature.md = source of truth) --",
        _fmt_value(conflict.local),
        "  -- REMOTE (tracker, human proposal) --",
        _fmt_value(conflict.remote),
        "  -- Resolve (writes feature.md first, then projects to the tracker) --",
        resolve_cmd,
        f"  Guidance: {guidance}",
        "",
    ])


def print_conflicts(conflicts: list[ConflictRecord]) -> None:
    if not conflicts:
        print("[AAH-VC] No open conflicts.")
        return
    print(f"[AAH-VC] {len(conflicts)} item(s) need your decision — review & call `resolve` for each.\n")
    for c in conflicts:
        print(render_conflict(c))


def print_apply_summary(report: dict) -> None:
    """Human/LLM summary of an apply run. report keys: created, updated,
    inbound, conflicts, directives, offline, errors."""
    print("[AAH-VC] apply summary")
    print(f"  created   : {report.get('created', 0)}")
    print(f"  updated   : {report.get('updated', 0)}")
    print(f"  inbound   : {report.get('inbound', 0)}")
    print(f"  directives: {report.get('directives', 0)}")
    print(f"  conflicts : {report.get('conflicts', 0)} (recorded, non-blocking)")
    if report.get("offline"):
        print("  offline   : tracker unreachable — outbound queued, read-only")
    for err in report.get("errors", []) or []:
        print(f"  ! error   : {err}")
    if report.get("rework_staged"):
        print(
            f"  note      : {len(report['rework_staged'])} structural change(s) staged "
            "— run aah-plan re-entry to recompute DAG/waves: "
            + ", ".join(report["rework_staged"])
        )
