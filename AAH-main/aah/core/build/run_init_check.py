#!/usr/bin/env python3
"""
Execute init.sh and report environment health.

Every implementation session starts by running this to verify
the development server starts and basic smoke tests pass.
Exit 0 = healthy, Exit 2 = unhealthy.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _is_first_wave(project_path: Path) -> bool:
    """Check if this is wave 0 with no completed features."""
    fl_path = project_path / ".aah" / "feature-list.json"
    if not fl_path.exists():
        return True
    try:
        import json
        with open(fl_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        passing = [f for f in data.get("features", []) if f.get("passes", False)]
        return len(passing) == 0
    except Exception:
        return True


def run_init_check(project_path: Path) -> dict:
    """Execute init.sh and return health status."""
    init_script = project_path / ".aah" / "init.sh"

    if not init_script.exists():
        return {
            "healthy": True,
            "message": "No init.sh found — skipping health check",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # Check if this is the first wave (no completed features yet)
    # If so, init.sh failure is expected — infrastructure hasn't been built
    first_wave = _is_first_wave(project_path)

    try:
        result = subprocess.run(
            ["bash", str(init_script)],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=120,
        )

        healthy = result.returncode == 0

        if not healthy and first_wave:
            return {
                "healthy": True,
                "exit_code": result.returncode,
                "stdout": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
                "stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
                "message": "init.sh failed but this is wave 0 (infrastructure not built yet) — proceeding",
                "first_wave": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        return {
            "healthy": healthy,
            "exit_code": result.returncode,
            "stdout": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
            "stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
            "message": "Environment healthy" if healthy else "Environment unhealthy — fix before starting new work",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except subprocess.TimeoutExpired:
        return {
            "healthy": False,
            "message": "init.sh timed out (120s)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "healthy": False,
            "message": f"Error running init.sh: {e}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run init.sh health check")
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    result = run_init_check(project_path)
    json.dump(result, sys.stdout, indent=2)
    print()

    if result["healthy"]:
        sys.exit(0)
    else:
        print(result["message"], file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
