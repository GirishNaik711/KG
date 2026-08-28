#!/usr/bin/env python3
"""
Gate hook: verify a feature's standards compliance report exists and passes.

Exit 0 = pass (or not yet applicable), Exit 2 = fail (BLOCK verdict).
"""

import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_json


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    # Extract feature ID from hook context
    feature_id = hook_input.get("task", {}).get("metadata", {}).get("feature_id", "")
    if not feature_id:
        subject = hook_input.get("task", {}).get("subject", "")
        import re
        match = re.search(r"(F\d+)", subject)
        if match:
            feature_id = match.group(1)

    if not feature_id:
        sys.exit(0)

    standards_path = project_path / ".aah" / "build" / "quality-results" / f"{feature_id}-standards.json"
    if not standards_path.exists():
        # Standards check not yet run — don't block
        sys.exit(0)

    result = read_json(standards_path)
    verdict = result.get("verdict", "PASS")

    if verdict == "BLOCK":
        blocking = result.get("blocking_rules", [])
        print(f"Standards gate BLOCKED for {feature_id}: {blocking}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
