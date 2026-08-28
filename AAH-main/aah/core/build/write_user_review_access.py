#!/usr/bin/env python3
"""
Write the user-review access info file for a wave (AAH issue #247, Phase 2A).

Called by the aah-runtime-validator subagent in "Start and Keep Running"
mode. Writes a human-facing JSON describing how to access the running
application (URL, port, container name, etc.) so a human reviewer can
exercise the wave's features.

This file is NOT a verification artifact — the orchestrator does not
read it for gate dispatch. It exists purely as a hand-off note for
human review. Therefore it does NOT use attestation; a plain
write_json_verified is sufficient. Existing as a separate writer lets
the aah-runtime-validator agent drop the Write/Edit tools entirely (the
agent calls subprocesses for all file persistence).

Usage:
  aah run core.build.write_user_review_access \
    --wave N \
    --access-info-json '<JSON object>' \
    [--project-path PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import write_json_verified


def main() -> None:
    parser = argparse.ArgumentParser(description="Write user-review access info")
    parser.add_argument("--wave", type=int, required=True)
    parser.add_argument(
        "--access-info-json",
        required=True,
        help=(
            "JSON object describing how to access the running app: "
            "{access_url, access_type, port, container_name, ...}"
        ),
    )
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    try:
        access_info = json.loads(args.access_info_json)
    except json.JSONDecodeError as e:
        print(f"Error parsing --access-info-json: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(access_info, dict):
        print("Error: --access-info-json must be a JSON object.", file=sys.stderr)
        sys.exit(1)

    payload = {
        "wave": args.wave,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        **access_info,
    }

    output_dir = project_path / ".aah" / "build" / "checkpoint-results"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"wave-{args.wave}-user-review-access.json"
    write_json_verified(payload, out_path, artifact_name=f"wave-{args.wave} user review access")

    print(f"User-review access info written to: {out_path}", file=sys.stderr)
    json.dump(payload, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
