"""Parse a Claude agent ``.md`` file into (frontmatter, body).

Agent definitions live in ``aah/agents/*.md`` as YAML frontmatter + a prose
body. Claude installs them as-is (symlink); other hosts that use a different
agent format (e.g. Codex's ``.toml``) transform them. This module is the shared
reader; the transform to a host format belongs to that host's adapter.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def parse(md_path: Path) -> tuple[dict, str]:
    """Return ``(frontmatter, body)`` for an agent ``.md`` file.

    Frontmatter is the YAML block delimited by leading/trailing ``---`` lines;
    body is everything after it. A file with no frontmatter yields ``({}, text)``.
    """
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    # Split on the closing delimiter of the leading frontmatter block.
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}, text
    front_raw = parts[0][len("---"):]        # drop the opening '---'
    body = parts[1].lstrip("\n")
    front = yaml.safe_load(front_raw) or {}
    if not isinstance(front, dict):
        front = {}
    return front, body
