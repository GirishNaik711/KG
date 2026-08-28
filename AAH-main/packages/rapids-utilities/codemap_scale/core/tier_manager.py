"""
Tier Manager: decides what analysis depth each file/region gets.

The core insight for 500K-file codebases: you don't need deep analysis
on every file. The tier system ensures the "cost of scouting" stays
proportional to the value of the intelligence.

Promotion rules:
- Agent queries a file → promote to Tier 2 (full extract)
- Agent searches semantically in a directory → promote that dir to Tier 3
- Frequently queried symbols → auto-promote to Tier 2
- Entry points (main, routes, handlers) → auto-promote to Tier 2
- Test files → Tier 1 only (unless agent specifically needs them)

Demotion:
- Files not queried in 30 days → demote back to Tier 1
- Deleted files → remove entirely
"""

from __future__ import annotations

import logging
import time
from typing import Any

from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph

logger = logging.getLogger(__name__)


# Heuristic patterns for auto-promotion to Tier 2
_ENTRY_POINT_PATTERNS = [
    "main.py", "app.py", "server.py", "index.ts", "index.js",
    "main.ts", "main.go", "main.rs", "lib.rs",
    "**/routes/**", "**/handlers/**", "**/controllers/**",
    "**/api/**", "**/endpoints/**",
    "setup.py", "setup.cfg", "pyproject.toml",
    "Dockerfile", "docker-compose.yml",
]

_TEST_PATTERNS = [
    "**/test_*", "**/*_test.*", "**/tests/**", "**/__tests__/**",
    "**/spec/**", "**/*.spec.*", "**/*.test.*",
]


class TierManager:
    """
    Manages intelligence tiers for files in the codebase.

    Tracks which files are at which tier and handles promotion/demotion.
    """

    def __init__(self, graph: SQLiteSymbolGraph) -> None:
        self.graph = graph
        self._query_counts: dict[str, int] = {}
        self._last_queried: dict[str, float] = {}

    def classify_for_auto_promotion(self, file_paths: list[str]) -> dict[int, list[str]]:
        """
        Classify files into initial tiers based on heuristics.

        Returns: {tier: [file_paths]}
        """
        import fnmatch

        tier2_files: list[str] = []
        tier1_files: list[str] = []

        for fp in file_paths:
            is_entry = any(fnmatch.fnmatch(fp, pat) for pat in _ENTRY_POINT_PATTERNS)
            is_test = any(fnmatch.fnmatch(fp, pat) for pat in _TEST_PATTERNS)

            if is_entry:
                tier2_files.append(fp)
            elif is_test:
                tier1_files.append(fp)  # Tests stay at Tier 1 unless demanded
            else:
                tier1_files.append(fp)

        return {1: tier1_files, 2: tier2_files}

    def record_query(self, file_path: str) -> None:
        """Record that an agent queried a file. Tracks for auto-promotion."""
        self._query_counts[file_path] = self._query_counts.get(file_path, 0) + 1
        self._last_queried[file_path] = time.time()

    def get_promotion_candidates(self, threshold: int = 3) -> list[str]:
        """
        Files that have been queried enough times to warrant Tier 2 promotion.
        """
        return [
            fp for fp, count in self._query_counts.items()
            if count >= threshold
        ]

    def get_demotion_candidates(self, days_inactive: int = 30) -> list[str]:
        """
        Files not queried recently that can be demoted back to Tier 1.
        """
        cutoff = time.time() - (days_inactive * 86400)
        return [
            fp for fp, ts in self._last_queried.items()
            if ts < cutoff
        ]

    def promote_files(self, file_paths: list[str], target_tier: int) -> list[str]:
        """
        Return files that need re-parsing at a higher tier.

        Checks current tier in the database and only returns files
        that actually need promotion.
        """
        conn = self.graph._get_conn()
        needs_promotion: list[str] = []

        for fp in file_paths:
            row = conn.execute(
                "SELECT tier FROM files WHERE path = ?", (fp,)
            ).fetchone()

            if row is None or row["tier"] < target_tier:
                needs_promotion.append(fp)

        return needs_promotion

    def promote_directory(self, dir_path: str, target_tier: int) -> list[str]:
        """
        Promote all files in a directory to a target tier.
        Used when an agent "focuses" on a region of the codebase.
        """
        conn = self.graph._get_conn()
        rows = conn.execute(
            "SELECT path FROM files WHERE path LIKE ? AND tier < ?",
            (dir_path + "%", target_tier),
        ).fetchall()

        return [row["path"] for row in rows]

    def get_tier_summary(self) -> dict[str, Any]:
        """Get a summary of the tier distribution."""
        return self.graph.get_stats().get("tiers", {})
