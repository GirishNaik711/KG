#!/usr/bin/env python3
"""Build Playbook loader.

Reads the build-playbooks/ tree and returns playbook content keyed by
technology domain. Respects the `published` flag — unpublished playbooks
are excluded from loaders used at injection time.
"""

from pathlib import Path
from typing import Optional

from aah.core.common.io_utils import read_yaml


# ─── v2 schema helpers ─────────────────────────────────────────────────────
# Build Playbooks come in two shapes. v1 (legacy, no `schema_version`) is a flat
# pattern/anti-pattern/architecture pack. v2 (`schema_version: "2.0"`) is a thin
# index: one cross-cutting `guidance` line + patterns + typed `references[]`
# (kind: skill|doc|url). These helpers are the SINGLE source of truth for the
# v1/v2 branch and for resolving reference targets — every consumer (validator,
# context renderer, discuss overlay) imports them rather than re-deriving.


def is_v2(playbook: dict) -> bool:
    """True if the playbook declares the v2 schema (`schema_version: "2.0"`).

    v1 playbooks have no `schema_version` key. Do not scatter `.get(...)` checks
    across consumers — call this so the version test lives in one place.
    """
    return str((playbook or {}).get("schema_version", "")).strip() == "2.0"


def _repo_root() -> Optional[Path]:
    """Return the repository root (the parent of the `aah/` package dir).

    v2 `kind: doc` refs are authored repo-relative (e.g. `aah/_resources/...`),
    so they must resolve against the repo root — NOT the `aah/` package dir that
    `config.resolve_framework_root()` returns (that would double the `aah/`
    segment). Delegates to the guidance loader's repo-root walker so there is a
    single definition of "repo root" shared with the discuss layer.
    """
    try:
        from aah.core.discuss.guidance import find_framework_root
        return find_framework_root()
    except Exception:
        return None


def resolve_doc_ref(ref: str) -> Optional[Path]:
    """Resolve a v2 `kind: doc` reference (repo-relative) to an existing Path.

    Returns None if the repo root can't be located or the file does not exist.
    """
    root = _repo_root()
    if root is None:
        return None
    candidate = root / ref
    return candidate if candidate.is_file() else None


def resolve_skill_ref(name: str) -> Optional[Path]:
    """Resolve a v2 `kind: skill` reference to its SKILL.md, if it exists.

    Skills live at `aah/skills/<name>/SKILL.md`. Returns None if absent.
    """
    root = _repo_root()
    if root is None:
        return None
    candidate = root / "aah" / "skills" / name / "SKILL.md"
    return candidate if candidate.is_file() else None


def find_playbooks_root() -> Optional[Path]:
    """Locate the build-playbooks/ directory by walking up from this file.

    Returns None if not found (e.g., during tests running in isolation).
    """
    current = Path(__file__).resolve()
    for parent in [current, *current.parents]:
        candidate = parent / "build-playbooks"
        if candidate.is_dir() and (candidate / "_registry.yaml").is_file():
            return candidate
    return None


def load_registry() -> dict:
    """Load build-playbooks/_registry.yaml."""
    root = find_playbooks_root()
    if root is None:
        return {"playbooks": {}}
    return read_yaml(root / "_registry.yaml")


def load_playbook(
    technology_domain: str,
    *,
    include_unpublished: bool = False,
) -> Optional[dict]:
    """Load a playbook by technology-domain key.

    Args:
        technology_domain: project_type key (e.g., "ai-infra-platforms").
        include_unpublished: if False (default), returns None for playbooks
            where `published: false`. Set True for CLI commands that need to
            see all playbooks regardless of status.

    Returns:
        The playbook dict or None if not found / not published.
    """
    root = find_playbooks_root()
    if root is None:
        return None

    registry = load_registry()
    entry = registry.get("playbooks", {}).get(technology_domain)
    if entry is None:
        return None

    playbook_path = root / entry["file"]
    if not playbook_path.is_file():
        return None

    playbook = read_yaml(playbook_path)
    if not include_unpublished and not playbook.get("published", False):
        return None

    return playbook


def list_published() -> list[str]:
    """Return the list of technology-domain keys whose playbooks are published."""
    root = find_playbooks_root()
    if root is None:
        return []
    registry = load_registry()
    published: list[str] = []
    for key, entry in registry.get("playbooks", {}).items():
        playbook_path = root / entry["file"]
        if not playbook_path.is_file():
            continue
        playbook = read_yaml(playbook_path)
        if playbook.get("published", False):
            published.append(key)
    return published
