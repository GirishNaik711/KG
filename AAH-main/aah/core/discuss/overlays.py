#!/usr/bin/env python3
"""Overlay assembly for /aah-discuss Step 5.4.

Detection (choosing WHICH domains/archetypes match a project) is done by the
skill's own main thread, which reads the enriched index files directly:
    aah/domain-briefs/_taxonomy.yaml       (per-node description + keywords)
    aah/build-playbooks/_registry.yaml     (per-archetype description + keywords)
There is deliberately NO `detect` subcommand and NO LLM here.

This module owns only the deterministic second half: given the domains and
archetypes the user CONFIRMED, assemble the overlay the walk consumes —

    * `domain_brief_paths[]`       — brief files (ancestor chain) per domain
    * `archetype_playbook_paths[]` — build-playbook file per archetype
    * `overlay_summary_md`         — compact injectable markdown

Usage:
    aah run core.discuss.overlays load \
        --domains "financial-services/commercial-banking/commercial-client-onboarding" \
        --archetypes "ai-infra-platforms,ai-applications" --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.config import require_project_path
from aah.core.domain_briefs import loader as domain_loader
from aah.core.playbooks import loader as playbook_loader


# ─── helpers ───────────────────────────────────────────────────────────────

def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated CLI value into a clean list (empty → [])."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _short(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ─── domain overlay ──────────────────────────────────────────────────────────

def _domain_overlay(domain_ids: list[str]) -> dict:
    """Assemble per-domain brief paths and summary fragments."""
    brief_paths: list[str] = []
    summary_blocks: list[str] = []
    missing: list[str] = []

    for did in domain_ids:
        node = domain_loader.load_node(did)
        if node is None:
            missing.append(did)
            continue

        # Normalize to forward slashes — these paths are matched as strings and
        # rendered cross-platform (list_files returns OS-native separators).
        for f in domain_loader.list_files(did):
            f = f.replace("\\", "/")
            if f not in brief_paths:
                brief_paths.append(f)

        # compact summary block for this domain
        breadcrumb = " › ".join(_title_case(seg) for seg in did.split("/"))
        lines = [f"### 🌐 {breadcrumb}"]
        desc = _short(node.get("description") or "", 240)
        if desc:
            lines.append(desc)
        procs = [p.get("name", "") for p in (node.get("processes") or []) if p.get("name")]
        if procs:
            lines.append(f"**Key processes:** {', '.join(procs[:6])}")
        regs = [r.get("name", "") for r in (node.get("regulations") or []) if isinstance(r, dict) and r.get("name")]
        if regs:
            lines.append(f"**Regulations:** {', '.join(regs[:6])}")
        summary_blocks.append("\n".join(lines))

    return {
        "domain_brief_paths": brief_paths,
        "summary_blocks": summary_blocks,
        "missing_domains": missing,
    }


# ─── archetype overlay ───────────────────────────────────────────────────────

def _archetype_overlay(archetypes: list[str]) -> dict:
    """Assemble per-archetype playbook paths and summary fragments.

    Uses include_unpublished=True — discussion-time framing surfaces all
    archetypes regardless of the `published` flag (contrast with runtime
    injection which respects it).
    """
    registry = playbook_loader.load_registry()
    entries = registry.get("playbooks", {})
    root = playbook_loader.find_playbooks_root()

    playbook_paths: list[str] = []
    summary_blocks: list[str] = []
    missing: list[str] = []

    for arch in archetypes:
        entry = entries.get(arch)
        if entry is None:
            missing.append(arch)
            continue

        # path relative to the harness root (parent of build-playbooks/)
        if root is not None:
            rel = (root / entry["file"]).relative_to(root.parent).as_posix()
            if rel not in playbook_paths:
                playbook_paths.append(rel)

        playbook = playbook_loader.load_playbook(arch, include_unpublished=True)
        name = entry.get("name", arch)
        lines = [f"### 📦 {name}"]
        desc = _short(entry.get("description") or (playbook or {}).get("description") or "", 240)
        if desc:
            lines.append(desc)
        if playbook and playbook_loader.is_v2(playbook):
            # v2: surface the cross-cutting guidance + patterns, marking which
            # patterns are reference-backed (so the researcher knows which carry
            # authoritative substance to follow, vs. inline-enumerated ones).
            guidance = _short((playbook.get("guidance") or ""), 300)
            if guidance:
                lines.append(f"**Guidance:** {guidance}")
            pat_labels = []
            for p in (playbook.get("patterns") or []):
                if not (isinstance(p, dict) and p.get("name")):
                    continue
                label = p["name"]
                if p.get("references"):
                    label += " (refs)"
                pat_labels.append(label)
            if pat_labels:
                # Show all v2 patterns (curated, small) so reference-backed ones
                # — marked (refs) — are never truncated off; they carry the
                # authoritative substance the researcher must follow.
                lines.append(f"**Patterns:** {', '.join(pat_labels[:12])}")
        elif playbook:
            pats = [p.get("name", "") for p in (playbook.get("patterns") or []) if isinstance(p, dict) and p.get("name")]
            if pats:
                lines.append(f"**Patterns:** {', '.join(pats[:6])}")
        summary_blocks.append("\n".join(lines))

    return {
        "archetype_playbook_paths": playbook_paths,
        "summary_blocks": summary_blocks,
        "missing_archetypes": missing,
    }


def _title_case(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.split("-"))


# ─── public API ──────────────────────────────────────────────────────────────

def build_overlay(domain_ids: list[str], archetypes: list[str]) -> dict:
    """Assemble the overlay for the confirmed domains + archetypes."""
    dom = _domain_overlay(domain_ids)
    arc = _archetype_overlay(archetypes)

    summary_parts: list[str] = []
    if dom["summary_blocks"]:
        summary_parts.append("## Domain Overlay\n\n" + "\n\n".join(dom["summary_blocks"]))
    if arc["summary_blocks"]:
        summary_parts.append("## Archetype Overlay\n\n" + "\n\n".join(arc["summary_blocks"]))
    overlay_summary_md = ("\n\n".join(summary_parts).rstrip() + "\n") if summary_parts else ""

    return {
        "domain_ids": domain_ids,
        "archetypes": archetypes,
        "domain_brief_paths": dom["domain_brief_paths"],
        "archetype_playbook_paths": arc["archetype_playbook_paths"],
        "overlay_summary_md": overlay_summary_md,
        "missing_domains": dom["missing_domains"],
        "missing_archetypes": arc["missing_archetypes"],
    }


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Discuss domain/archetype overlay assembler")
    # --project-path is top-level (matches core.discuss.registry convention):
    #   aah run core.discuss.overlays --project-path <p> load --domains ...
    parser.add_argument("--project-path", type=Path, help="Override active project resolution")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="Assemble overlay for confirmed domains + archetypes")
    p_load.add_argument("--domains", default="", help="Comma-separated domain node ids")
    p_load.add_argument("--archetypes", default="", help="Comma-separated archetype keys")
    p_load.add_argument("--json", action="store_true", help="Emit JSON (default is human summary)")

    args = parser.parse_args()

    if args.command == "load":
        # Resolve project only to honor the CLI convention / validate context;
        # overlay assembly itself reads framework resources, not project state.
        require_project_path(args.project_path)
        result = build_overlay(_split_csv(args.domains), _split_csv(args.archetypes))
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_human(result)
    else:
        parser.error("unknown command")


def _print_human(result: dict) -> None:
    print(f"Domains: {', '.join(result['domain_ids']) or '(none)'}")
    print(f"Archetypes: {', '.join(result['archetypes']) or '(none)'}")
    print(f"Brief files: {len(result['domain_brief_paths'])}")
    print(f"Playbook files: {len(result['archetype_playbook_paths'])}")
    if result["missing_domains"]:
        print(f"⚠️  Unknown domains: {', '.join(result['missing_domains'])}")
    if result["missing_archetypes"]:
        print(f"⚠️  Unknown archetypes: {', '.join(result['missing_archetypes'])}")


if __name__ == "__main__":
    main()
