#!/usr/bin/env python3
"""
Write the aah-runtime-validator's auxiliary test Dockerfile (AAH issue #247, Phase 2A).

Called by the aah-runtime-validator subagent in step 4 when the project has
no Dockerfile of its own. Persists the agent's composed Dockerfile to:

  .aah/build/Dockerfile.test

This file is NOT a verification artifact — Docker reads it during the
build, the orchestrator never reads it for gate dispatch. The
attestation library is not involved.

Existing as a tiny subprocess writer is the price of letting the
aah-runtime-validator agent drop the Write/Edit tools entirely. The agent
still composes the Dockerfile content (its job is to reason about the
project's stack and pick the right base image, deps, CMD); this script
just persists what the agent passes in.

Usage:
  cat dockerfile.txt | aah run core.build.write_test_dockerfile [--project-path PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aah.core.common.io_utils import write_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the aah-runtime-validator test Dockerfile")
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    content = sys.stdin.read()
    if not content.strip():
        print("Error: empty Dockerfile content on stdin.", file=sys.stderr)
        sys.exit(1)

    out_path = project_path / ".aah" / "build" / "Dockerfile.test"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_text(content, out_path)

    print(f"Test Dockerfile written to: {out_path}", file=sys.stderr)
    print(str(out_path))
    sys.exit(0)


if __name__ == "__main__":
    main()
