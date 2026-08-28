#!/usr/bin/env python3
"""feature.md <-> WorkItem codec — the ONLY reader/writer of feature files.

Feature files are Markdown with YAML frontmatter (``F###.md``). Centralising
access here means the engine/applier never touch feature files directly, and the
"feature.md is the source of truth" invariant has exactly one enforcement point.
Writes preserve the markdown body and every non-synced frontmatter key
(knowledge_used, codemap_context, expertise, status fields, etc.) untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aah.core.common.feature_utils import (
    find_feature_file,
    load_features_from_dir,
    parse_feature_frontmatter,
    write_feature_frontmatter,
)
from aah.core.version_control.models import SYNCED_FIELDS, WorkItem


class FeatureCodec:
    """Reads/writes feature .md files under <rapids_path>/plan/features/."""

    def __init__(self, rapids_path: Path):
        self.rapids_path = Path(rapids_path)
        self.features_dir = self.rapids_path / "plan" / "features"

    # --- discovery ----------------------------------------------------------
    def list_features(self) -> list[str]:
        """Return all feature IDs found in the features dir, sorted."""
        if not self.features_dir.is_dir():
            return []
        ids: list[str] = []
        for data in load_features_from_dir(self.features_dir):
            fid = data.get("id")
            if fid:
                ids.append(fid)
        return ids

    def path_for(self, feature_id: str) -> Path | None:
        return find_feature_file(self.features_dir, feature_id)

    # --- read ---------------------------------------------------------------
    def read(self, feature_id: str) -> WorkItem | None:
        """Load a feature .md into a canonical WorkItem (None if not found)."""
        path = self.path_for(feature_id)
        if path is None:
            return None
        data = parse_feature_frontmatter(path)
        if data is None:
            return None
        return self._to_work_item(data)

    def read_raw(self, feature_id: str) -> dict | None:
        """Load the full frontmatter dict (all keys), for write-back preservation."""
        path = self.path_for(feature_id)
        if path is None:
            return None
        return parse_feature_frontmatter(path)

    @staticmethod
    def _to_work_item(data: dict) -> WorkItem:
        return WorkItem(
            feature_id=data.get("id", ""),
            title=data.get("title", data.get("description", "")),
            description=data.get("description", ""),
            acceptance_criteria=list(data.get("acceptance_criteria", []) or []),
            test_cases=list(data.get("test_cases", []) or []),
            dependencies=list(data.get("dependencies", []) or []),
            layer=data.get("layer"),
            status=data.get("status", "pending"),
        )

    # --- write --------------------------------------------------------------
    def write_fields(self, feature_id: str, values: dict[str, Any]) -> Path:
        """Write specific synced fields back into the feature .md frontmatter.

        Only keys in ``values`` that are SYNCED_FIELDS are written; all other
        existing frontmatter keys and the markdown body are preserved verbatim.
        This is the single mutation point for the source of truth. ``status`` is
        intentionally NOT written here — status flows through
        common.feature_list (the hook-legal path); the applier handles that
        separately.
        """
        path = self.path_for(feature_id)
        if path is None:
            raise FileNotFoundError(f"No feature .md for {feature_id} in {self.features_dir}")
        data = parse_feature_frontmatter(path) or {}
        for key, val in values.items():
            if key in SYNCED_FIELDS and key != "status":
                data[key] = val
        write_feature_frontmatter(data, path)
        return path
