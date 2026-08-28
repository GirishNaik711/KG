"""The platform-adapter interface.

Adding a host = adding one adapter module that implements ``PlatformAdapter``
and registering it in ``registry.py``. The driver iterates whatever adapter the
registry returns and never contains host-specific logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstallCtx:
    """Everything an adapter needs to install/uninstall, resolved once by the driver."""
    project: bool                       # True => repo-local scope, False => user-global
    project_dir: Path | None = None     # base for project scope (defaults to CWD)


@dataclass
class Report:
    """One line of install/uninstall output, rendered by the driver."""
    kind: str                # "skills" | "agents" | "hooks" | "permissions"
    linked: int = 0          # newly created (links) / added (settings entries)
    current: int = 0         # already present / unchanged
    note: str = ""           # free text, e.g. "deferred: …" or a target path


@dataclass
class StatusRow:
    """One platform+scope entry for ``aah status``."""
    platform: str
    scope: str
    root: Path
    skills_linked: int = 0
    managed: str = "-"       # "managed" | "unmanaged" | "-"
    extra: str = ""
    agents_linked: int = 0
    version: str = ""        # aah version recorded in the _aah marker (if linked)
    linked_at: str = ""      # ISO timestamp recorded in the _aah marker (if linked)
    dead: int = 0            # count of stranded (dead-target) links


class PlatformAdapter(ABC):
    """Install/uninstall/report the framework's artifacts for one host platform."""

    name: str = ""

    @abstractmethod
    def config_root(self, *, project: bool, project_dir: Path | None = None) -> Path:
        """Resolve the host config root (e.g. ~/.claude or ~/.codex)."""

    @abstractmethod
    def capabilities(self) -> set[str]:
        """What this adapter actually installs today, e.g. {"skills","agents","hooks"}.

        Reported honestly so ``status`` and deferred features are not misrepresented.
        """

    @abstractmethod
    def install(self, ctx: InstallCtx) -> list[Report]:
        """Install links + config for this platform. Idempotent."""

    @abstractmethod
    def uninstall(self, ctx: InstallCtx) -> list[Report]:
        """Reverse exactly what ``install`` added; leave user's own keys intact."""

    @abstractmethod
    def status(self, ctx: InstallCtx) -> StatusRow:
        """Report what is currently linked/managed for this platform + scope."""

    def project_ignore_lines(self, project_dir: Path) -> list[str]:
        """`.gitignore` entries covering the machine-specific artifacts a
        *project-scope* install of this platform creates in ``project_dir``.

        These are symlinks/generated files that must never be committed (e.g.
        ``.claude/skills/*`` for Claude, ``.agents/skills/*`` for Codex). The
        scaffolder (the single ``.gitignore`` writer) unions these across all
        registered adapters. Lines for a platform the user did not install are
        harmless no-ops (the paths simply won't exist).

        Paths are returned **relative to ``project_dir``** with forward slashes.
        Default: no entries. ``install`` itself never writes git artifacts.
        """
        return []
