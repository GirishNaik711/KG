"""Build Playbook loading, validation, and context rendering.

Build Playbooks are per-technology-domain engineering pattern packs located
at build-playbooks/<technology-domain>/build-playbook.yaml in the repo root.

This module exposes three entry points:
  - loader.load_playbook(technology_domain) -> dict | None
  - context.build_playbook_context(project_path) -> str (markdown injection)
  - validate (CLI command) -> int (exit code)
"""

from aah.core.playbooks.loader import (
    find_playbooks_root,
    load_playbook,
    load_registry,
    list_published,
    is_v2,
    resolve_doc_ref,
    resolve_skill_ref,
)
from aah.core.playbooks.context import build_playbook_context

__all__ = [
    "find_playbooks_root",
    "load_playbook",
    "load_registry",
    "list_published",
    "is_v2",
    "resolve_doc_ref",
    "resolve_skill_ref",
    "build_playbook_context",
]
