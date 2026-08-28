"""Platform-adapter registry.

Adding a host = importing its adapter and adding one line to ``_ADAPTERS``.
Nothing else in the install package needs to change.
"""

from __future__ import annotations

from pathlib import Path

from .adapter import PlatformAdapter
from .adapters.claude import ClaudeAdapter
from .adapters.codex import CodexAdapter

_ADAPTERS: dict[str, PlatformAdapter] = {
    a.name: a for a in (
        ClaudeAdapter(),
        CodexAdapter(),
    )
}

DEFAULT = "claude"


def names() -> list[str]:
    return list(_ADAPTERS)


def get(name: str) -> PlatformAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise KeyError(
            f"unsupported platform '{name}'. supported: {', '.join(_ADAPTERS)}"
        ) from None


def all_project_ignore_lines(project_dir: Path) -> list[str]:
    """Union of every registered adapter's project ``.gitignore`` entries.

    The scaffolder (single writer of the project ``.gitignore``) uses this so a
    project ignores the machine-specific artifacts of ANY supported platform.
    Lines for a platform the user did not install are harmless no-ops. Order is
    stable (adapter registration order); duplicates removed while preserving it.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for adapter in _ADAPTERS.values():
        for line in adapter.project_ignore_lines(project_dir):
            if line not in seen:
                seen.add(line)
                lines.append(line)
    return lines
