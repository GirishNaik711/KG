"""Installation process for the RAPIDS harness.

The three source-of-truth directories hold data only:
  * ``aah/skills``  — skill directories (SKILL.md + assets)
  * ``aah/agents``  — agent definitions (.md, YAML frontmatter + body)
  * ``aah/hooks``   — aah-hooks.yaml + settings-defaults.json

This package owns all install/uninstall/status logic. Each host is a
``PlatformAdapter`` (``adapters/*.py``) that renders the source into its own
on-disk format; the ``driver`` is host-agnostic and just runs whichever adapter
the ``registry`` returns.
"""

from .driver import install, status, uninstall

__all__ = ["install", "uninstall", "status"]
