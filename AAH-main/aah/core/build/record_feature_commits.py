#!/usr/bin/env python3
"""
Record commits from a feature branch as belonging to that feature.

Maintains .aah/build/feature-commits.json — a mapping of feature IDs
to commit SHAs. Used as an authoritative fallback by find_feature_commits()
when commit messages don't follow the conventional feat(FNNN): format.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.git_utils import (
    _FEATURE_COMMIT_PREFIXES,
    branch_contains_commit,
    branch_exists,
    rev_parse,
    run_git,
)


def record_feature_commits(project_path: Path, feature_id: str) -> dict:
    """Record recent commits for a feature in the feature-commits manifest."""
    aah_path = project_path / ".aah"
    impl_dir = aah_path / "build"
    impl_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = impl_dir / "feature-commits.json"

    # Load existing manifest
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
    else:
        data = {}

    # Attribute commits by feature-branch range, not a repo-wide message grep.
    # A free-text ``--all --grep=\b<id>\b`` matches unrelated develop-side chore
    # commits that merely mention the id (e.g. "auto-commit ... session end"),
    # which can never reach the integration branch and permanently deadlock the
    # merge gate's branch_contains_all_commits check. We scope to the feature
    # branch and require the conventional feat(<id>): / fix(<id>): subject shape.
    feature_branch = f"feature/{feature_id}"
    if not branch_exists(feature_branch, cwd=project_path):
        print(f"Error: branch {feature_branch} does not exist", file=sys.stderr)
        sys.exit(1)

    feature_head = rev_parse(feature_branch, cwd=project_path)
    if not feature_head:
        print(f"Error: could not resolve {feature_branch}", file=sys.stderr)
        sys.exit(1)

    shas = {
        sha for sha in data.get(feature_id, [])
        if branch_contains_commit(feature_branch, sha, cwd=project_path)
    }
    shas.add(feature_head)

    alt = "|".join(_FEATURE_COMMIT_PREFIXES)
    result = run_git(
        [
            "log", feature_branch, "--no-merges",
            f"--grep=^({alt})\\({feature_id}\\):", "--extended-regexp",
            "--pretty=format:%H",
            "-20",  # limit to last 20 matches
        ],
        cwd=project_path, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            sha = line.strip()
            if sha:
                shas.add(sha)

    data[feature_id] = sorted(shas)
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding='utf-8')

    print(f"Recorded {len(shas)} commits for {feature_id}", file=sys.stderr)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record feature commits to feature-commits.json"
    )
    parser.add_argument("feature_id", help="Feature ID (e.g., F005)")
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    record_feature_commits(project_path, args.feature_id)
    sys.exit(0)


if __name__ == "__main__":
    main()
