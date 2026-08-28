#!/usr/bin/env python3
"""
PostToolUse hook: determine if codebase intelligence update is needed.

Triggered after Write|Edit operations. Checks if significant code changes
warrant updating codebase intelligence artifacts. Applies to BOTH brownfield
and greenfield projects — any project with codebase intelligence artifacts
benefits from staleness detection.

Exit 0 = no update needed, Exit 1 = update needed.

Also supports a `staleness-check` subcommand for explicit staleness queries.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# File patterns that indicate significant structural changes
SIGNIFICANT_PATTERNS = [
    # New directories or major files
    "__init__.py",
    "setup.py", "pyproject.toml", "package.json",
    "Dockerfile", "docker-compose",
    # Config changes
    ".env", "config.yaml", "config.json",
    # API/schema changes
    "schema", "migration", "models",
    "routes", "endpoints", "api",
    # Architecture changes
    "Makefile", "CMakeLists",
]

# Structural file extensions
STRUCTURAL_EXTENSIONS = {".proto", ".graphql", ".openapi", ".swagger"}

# Minimum number of pending significant changes to trigger update recommendation
THRESHOLD = 3

# Paths where codebase intelligence artifacts live (checked in order)
INTEL_DIRS = ["codebase-intel"]


def _find_intel_dir(project_path: Path) -> Path | None:
    """Find the codebase intelligence directory (unified or brownfield)."""
    aah_root = project_path / ".aah"
    for dirname in INTEL_DIRS:
        candidate = aah_root / dirname
        if candidate.is_dir():
            return candidate
    return None


def _find_intel_artifact(project_path: Path) -> Path | None:
    """Find the primary codebase intelligence artifact."""
    intel_dir = _find_intel_dir(project_path)
    if intel_dir is None:
        return None
    for name in ["codebase-structure.md", "codebase-learning.md", "codebase-profile.json"]:
        candidate = intel_dir / name
        if candidate.exists():
            return candidate
    return None


def _pending_changes_path(project_path: Path) -> Path:
    """Path to the pending changes counter file."""
    intel_dir = project_path / ".aah" / "codebase-intel"
    intel_dir.mkdir(parents=True, exist_ok=True)
    return intel_dir / "pending-changes.json"


def _read_pending_changes(project_path: Path) -> dict:
    """Read the pending changes counter."""
    path = _pending_changes_path(project_path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return {"count": 0, "files": []}


def _increment_pending_changes(project_path: Path, file_path: str) -> int:
    """Increment the pending changes counter. Returns new count."""
    data = _read_pending_changes(project_path)
    data["count"] = data.get("count", 0) + 1
    files = data.get("files", [])
    if file_path not in files:
        files.append(file_path)
    data["files"] = files[-20:]  # Keep last 20 files
    data["last_change"] = datetime.now(timezone.utc).isoformat()
    path = _pending_changes_path(project_path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding='utf-8')
    return data["count"]


def reset_pending_changes(project_path: Path) -> None:
    """Reset the pending changes counter (called after intelligence refresh)."""
    path = _pending_changes_path(project_path)
    data = {"count": 0, "files": [], "last_reset": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding='utf-8')
    # Clear session debounce flag so the next staleness event warns again
    warned_flag = project_path / ".aah" / "codebase-intel" / ".staleness-warned"
    if warned_flag.exists():
        try:
            warned_flag.unlink()
        except OSError:
            pass


def is_significant_change(file_path: str) -> bool:
    """Check if a file change matches significant structural patterns."""
    file_lower = file_path.lower()
    for pattern in SIGNIFICANT_PATTERNS:
        if pattern.lower() in file_lower:
            return True
    path = Path(file_path)
    if path.suffix in STRUCTURAL_EXTENSIONS:
        return True
    return False


def check_intel_update_needed(file_path: str, project_path: Path) -> bool:
    """
    Determine if a file change warrants a codebase intelligence update.

    Works for both greenfield and brownfield projects. Returns True if:
    - Codebase intelligence exists AND the changed file is structurally significant
    """
    # Check if ANY codebase intelligence exists (brownfield or unified)
    artifact = _find_intel_artifact(project_path)
    if artifact is None:
        return False

    return is_significant_change(file_path)


def staleness_check(project_path: Path) -> dict:
    """
    Explicit staleness check for codebase intelligence MD files.

    Returns dict with:
    - stale: bool
    - commits_behind: int (commits since last intel artifact modification)
    - pending_changes: int (significant file changes since last refresh)
    - has_intel: bool (whether any codebase intelligence exists)
    - intel_path: str | None

    Note: codemap.db staleness is handled separately by the implement skill
    (Step 2a) using codemap-metadata.json directly.
    """
    artifact = _find_intel_artifact(project_path)
    if artifact is None:
        return {
            "stale": False,
            "commits_behind": 0,
            "pending_changes": 0,
            "has_intel": False,
            "intel_path": None,
            "recommendation": "No codebase intelligence found. Run /aah-codebase-profile to generate.",
        }

    # Count commits since artifact was last modified
    artifact_mtime = artifact.stat().st_mtime
    artifact_iso = datetime.fromtimestamp(artifact_mtime, tz=timezone.utc).isoformat()

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--since={artifact_iso}"],
            cwd=project_path, capture_output=True, text=True, check=False,
        )
        commits_behind = len([l for l in result.stdout.strip().splitlines() if l.strip()])
    except Exception:
        commits_behind = 0

    pending = _read_pending_changes(project_path)
    pending_count = pending.get("count", 0)

    # Staleness is based on MD file freshness only.
    # Codemap staleness is handled separately by the implement skill (Step 2a).
    stale = commits_behind >= 50 or pending_count >= THRESHOLD

    recommendation = None
    if stale:
        recommendation = (
            f"Codebase intelligence is stale ({commits_behind} commits behind, "
            f"{pending_count} significant changes pending). "
            "Run /aah-codebase-profile --mode refresh to update."
        )

    return {
        "stale": stale,
        "commits_behind": commits_behind,
        "pending_changes": pending_count,
        "has_intel": True,
        "intel_path": str(artifact),
        "recommendation": recommendation,
    }


def main() -> None:
    # Check if called with subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "staleness-check":
        parser = argparse.ArgumentParser(description="Check codebase intelligence staleness")
        parser.add_argument("staleness-check")  # positional to consume the subcommand
        parser.add_argument("--project-path", type=Path, default=None)
        args = parser.parse_args()

        from aah.core.common.config import require_project_path
        project_path = require_project_path(args.project_path)

        result = staleness_check(project_path)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(1 if result["stale"] else 0)

    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        parser = argparse.ArgumentParser(description="Reset pending changes counter")
        parser.add_argument("reset")
        parser.add_argument("--project-path", type=Path, default=None)
        args = parser.parse_args()

        from aah.core.common.config import require_project_path
        project_path = require_project_path(args.project_path)

        reset_pending_changes(project_path)
        print("Pending changes counter reset", file=sys.stderr)
        sys.exit(0)

    # Default: PostToolUse hook mode (reads JSON from stdin)
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    from aah.core.common.config import resolve_project_path
    cwd = hook_input.get("cwd")
    project_path = resolve_project_path(Path(cwd) if cwd else None)
    if project_path is None:
        sys.exit(0)

    needs_update = check_intel_update_needed(file_path, project_path)

    if needs_update:
        count = _increment_pending_changes(project_path, file_path)
        if count >= THRESHOLD:
            # Only warn once per session to avoid storm on bulk edits.
            warned_flag = project_path / ".aah" / "codebase-intel" / ".staleness-warned"
            if not warned_flag.exists():
                try:
                    warned_flag.touch()
                except OSError:
                    pass
                print(
                    f"Codebase intelligence may need refreshing — "
                    f"{count} significant changes since last update (file: {file_path})",
                    file=sys.stderr,
                )
                sys.exit(1)  # Signal update needed (not blocking)

    sys.exit(0)


if __name__ == "__main__":
    main()
