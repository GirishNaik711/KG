#!/usr/bin/env python3
"""Back-compat shim — delegates to the new playbooks module.

The old implementation lived at this import path and served what is now
called a "Build Playbook" (technology-domain engineering patterns). The
content moved to `build-playbooks/<technology-domain>/build-playbook.yaml`
and the canonical loader is now `aah.core.playbooks`.

This module preserves the old import path and function signatures so
external callers (the SessionStart hook via load_impl_context.py, tests,
or any downstream code) continue to work. It emits a DeprecationWarning
the first time any function is called, pointing users at the new module.

Remove this shim once all call sites have migrated.
"""

import warnings
from pathlib import Path
from typing import Optional

from aah.core.common.manifest import find_manifest, load_manifest
from aah.core.playbooks.context import _render_playbook as _render_new
from aah.core.playbooks.loader import load_playbook


_DEPRECATION_EMITTED = False


def _deprecate() -> None:
    global _DEPRECATION_EMITTED
    if _DEPRECATION_EMITTED:
        return
    _DEPRECATION_EMITTED = True
    warnings.warn(
        "aah.core.knowledge.domain_loader is deprecated; "
        "use aah.core.playbooks.context.build_playbook_context "
        "for technology-domain Build Playbooks or "
        "aah.core.domain_briefs.context.build_domain_context_summary "
        "for industry-domain Domain Briefs.",
        DeprecationWarning,
        stacklevel=3,
    )


# Legacy flat keyword → tech-domain map, retained for callers that don't
# have a project_type set in manifest yet. Each key corresponds to what
# the old domain_loader returned for a matching intake; each value is the
# new technology_domain key in the playbook registry.
_LEGACY_KEYWORD_DOMAIN_MAP = {
    "agentic-ai": "ai-infra-platforms",
}


def detect_domains(project_path: Path) -> list[str]:
    """Legacy: returns list of detected tech-domain keys via manifest project_type.

    Pre-refactor callers used this to get a list of domain identifiers. Now
    the tech-domain is explicitly stored in manifest.project_type, so this
    simply returns [project_type] when set. Returns empty list otherwise.
    """
    _deprecate()
    manifest_path = find_manifest(project_path)
    if manifest_path is None:
        return []
    try:
        manifest = load_manifest(manifest_path)
    except Exception:
        return []
    project_type = manifest.get("project_type") if isinstance(manifest, dict) else None
    if project_type and project_type not in ("greenfield", "brownfield"):
        # It's a tech archetype key (ai-infra-platforms, data-pipelines, etc.)
        return [project_type]
    return []


def load_domain_knowledge(domain_name: str) -> Optional[dict]:
    """Legacy: loads a Build Playbook for the given domain name.

    Translates legacy domain names (e.g. `agentic-ai`) into their current
    tech-domain key and delegates to `aah.core.playbooks.loader`.
    """
    _deprecate()
    key = _LEGACY_KEYWORD_DOMAIN_MAP.get(domain_name, domain_name)
    return load_playbook(key, include_unpublished=True)


def build_domain_context(project_path: Path) -> str:
    """Legacy: build injectable markdown for tech-domain knowledge.

    Delegates to the new playbooks.context rendering. Returns empty string
    if no playbook is published for the current project_type.
    """
    _deprecate()
    manifest_path = find_manifest(project_path)
    if manifest_path is None:
        return ""
    try:
        manifest = load_manifest(manifest_path)
    except Exception:
        return ""
    project_type = manifest.get("project_type") if isinstance(manifest, dict) else None
    if not project_type or project_type in ("greenfield", "brownfield"):
        return ""
    playbook = load_playbook(project_type)
    if playbook is None:
        return ""
    # Rewrite the new section heading so legacy callers that scrape for
    # the old heading still work. The new canonical heading is
    # `## Build Playbook — ...`. We keep that here.
    return _render_new(playbook)
