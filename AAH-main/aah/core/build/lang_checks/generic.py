"""Generic fallback adapter (Phase 3 L2).

Always wins detection — registered last in the registry so it only
fires when no language-specific adapter claimed the project. Returns
``None`` from every command method, including ``test_command``.

Behavior-preserving with today's ``find_test_command`` returning None
when no recognized stack is present: callers handle ``None`` as "no
test command configured; require feature.test_config.command override
or fail clearly." The point of this adapter is that ``detect()``
*always* returns *something* — callers never have to handle ``None``
from ``lang_checks.detect()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aah.core.build.lang_checks.base import Cmd, LanguageAdapter


class GenericAdapter(LanguageAdapter):
    name = "generic"
    family = "generic"

    @classmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        # Always true — registered last so language-specific adapters
        # claim first.
        return True

    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        # Intentional None: caller must use feature.test_config.command
        # explicitly, or fail clearly. Don't paper over unknown stacks
        # with a default that might be wrong.
        return None

    # All other command methods inherit None defaults from the ABC.
