#!/usr/bin/env python3
"""Render a published Build Playbook as injectable markdown.

Called from the SessionStart hook path (load_impl_context.py) to produce
a compact `## Build Playbook` section. Returns the empty string when no
playbook is available, applicable, or published.
"""

from pathlib import Path
from typing import Optional

from aah.core.common.manifest import load_manifest
from aah.core.playbooks.loader import is_v2, load_playbook


def _load_project_manifest(project_path: Path) -> dict:
    """Load the project manifest, preferring `.aah/` with a `.rapids/` fallback.

    The folder=project model (aah/core/common/config.py) and session-start
    (load_impl_context.py) use `.aah/manifest.yaml`. This module historically read
    `.rapids/manifest.yaml`, so the manifest was never found and the playbook was
    silently never injected. Prefer `.aah/`; fall back to `.rapids/` for any
    project still on the legacy layout.
    """
    for rel in (".aah", ".rapids"):
        candidate = project_path / rel / "manifest.yaml"
        if candidate.exists():
            manifest = load_manifest(candidate)
            if isinstance(manifest, dict):
                return manifest
    return {}


def build_playbook_context(project_path: Path) -> str:
    """Build the injectable `## Build Playbook` markdown for a project.

    Resolution:
      1. Read manifest.project_type.
      2. Load the matching published playbook.
      3. Render as markdown.

    Returns empty string if project_type is missing, playbook is not found,
    or playbook is unpublished.
    """
    manifest = _load_project_manifest(project_path)
    project_type = manifest.get("project_type") if isinstance(manifest, dict) else None
    if not project_type:
        return ""

    playbook = load_playbook(project_type)
    if playbook is None:
        return ""

    if is_v2(playbook):
        return _render_playbook_v2(playbook)
    return _render_playbook(playbook)


def _render_playbook(playbook: dict) -> str:
    """Render a playbook dict as markdown. Budget-conscious (~2-4 KB typical)."""
    lines: list[str] = []
    name = playbook.get("name", playbook.get("technology_domain", "Build Playbook"))
    lines.append(f"## Build Playbook — {name}")
    lines.append("")

    description = (playbook.get("description") or "").strip()
    if description:
        lines.append(description)
        lines.append("")

    patterns = playbook.get("patterns") or []
    if patterns:
        lines.append("**Patterns:**")
        for p in patterns[:8]:
            nm = p.get("name", "")
            desc = (p.get("description") or "").strip().replace("\n", " ")
            # Compact one-line summary per pattern to stay within budget
            if len(desc) > 180:
                desc = desc[:177].rstrip() + "..."
            lines.append(f"- **{nm}** — {desc}")
        if len(patterns) > 8:
            lines.append(f"- _(+{len(patterns) - 8} more patterns)_")
        lines.append("")

    anti = playbook.get("anti_patterns") or []
    # anti_patterns may be a list of strings (nested inside patterns) OR a list
    # of dicts (global). Handle the dict shape here.
    anti_dicts = [a for a in anti if isinstance(a, dict)]
    if anti_dicts:
        lines.append("**Global anti-patterns:**")
        for a in anti_dicts[:5]:
            nm = a.get("name", "")
            why = (a.get("why_harmful") or a.get("description") or "").strip()
            if len(why) > 160:
                why = why[:157].rstrip() + "..."
            lines.append(f"- **{nm}** — {why}")
        lines.append("")

    archs = playbook.get("common_architectures") or []
    if archs:
        lines.append("**Common architectures:**")
        for a in archs[:4]:
            nm = a.get("name", "")
            desc = (a.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 180:
                desc = desc[:177].rstrip() + "..."
            lines.append(f"- **{nm}** — {desc}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _trunc(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _render_playbook_v2(playbook: dict) -> str:
    """Render a v2 playbook for BUILD-time injection. Budget-conscious (~2-4 KB).

    Emits guidance + patterns (name / when_to_apply / description / anti_patterns)
    + a de-duped "References (read these)" list gathered from top-level and
    per-pattern references[], EXCLUDING any entry marked `phase: discuss` — those
    are discuss-framing only and carry no build-time contract.
    """
    lines: list[str] = []
    name = playbook.get("name", playbook.get("technology_domain", "Build Playbook"))
    lines.append(f"## Build Playbook — {name}")
    lines.append("")

    description = (playbook.get("description") or "").strip()
    if description:
        lines.append(description)
        lines.append("")

    guidance = (playbook.get("guidance") or "").strip().replace("\n", " ")
    if guidance:
        lines.append(f"**Guidance:** {guidance}")
        lines.append("")

    # Collect build-relevant references (skip phase: discuss) while rendering
    # patterns, so the injected block carries only build-time contracts.
    build_refs: list[str] = []
    seen: set[str] = set()

    def _collect(refs) -> None:
        for ref in refs or []:
            if not isinstance(ref, dict):
                continue
            if ref.get("phase") == "discuss":
                continue
            kind, target = ref.get("kind"), ref.get("ref")
            if not target:
                continue
            # Emit the authored ref verbatim: repo-relative for doc, a bare name
            # for skill, a URL otherwise — all readable and portable. (Resolution
            # to an absolute path is the reader's job, and is validated at
            # validate-time via resolve_doc_ref / resolve_skill_ref.)
            label = f"{target}  (skill)" if kind == "skill" else str(target)
            if label not in seen:
                seen.add(label)
                build_refs.append(label)

    _collect(playbook.get("references"))

    patterns = playbook.get("patterns") or []
    if patterns:
        lines.append("**Patterns:**")
        for p in patterns[:12]:
            nm = p.get("name", "")
            desc = _trunc(p.get("description") or "", 180)
            lines.append(f"- **{nm}** — {desc}")
            when = _trunc(p.get("when_to_apply") or "", 140)
            if when:
                lines.append(f"  - _when:_ {when}")
            antis = [a for a in (p.get("anti_patterns") or []) if isinstance(a, str)]
            for a in antis[:3]:
                lines.append(f"  - _avoid:_ {_trunc(a, 140)}")
            _collect(p.get("references"))
        if len(patterns) > 12:
            lines.append(f"- _(+{len(patterns) - 12} more patterns)_")
        lines.append("")

    if build_refs:
        lines.append("**References (read these):**")
        for r in build_refs:
            lines.append(f"- {r}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
