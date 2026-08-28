#!/usr/bin/env python3
"""
PreToolUse hook guard: verify artifacts land in correct .aah/ subdirectory.

Reads hook input JSON from stdin. Checks that Write operations targeting
.aah/ paths use the correct subdirectory for the current phase.
Exit 0 = allow, Exit 2 = block.
"""

import json
import sys
from pathlib import PurePosixPath


# Map of .aah/ subdirectories to allowed phases
PHASE_DIR_MAP = {
    "discuss": ["discuss"],
    "architecture": ["architecture", "discuss"],
    "plan": ["plan", "architecture"],
    "build": ["build", "plan", "architecture", "discuss"],
    "deploy": ["deploy", "build"],
}

# Directories that are always writable (cross-phase)
ALWAYS_WRITABLE = [
    "audit",
    "codebase-intel",
    "security",
]

# Files at .aah/ root that are always writable
ROOT_WRITABLE_FILES = [
    "manifest.yaml",
    "phase-plan.yaml",
    "claude-progress.json",
    "feature-list.json",
    "init.sh",
]


def validate_path(file_path: str, current_phase: str | None = None) -> str | None:
    """
    Validate that a file path targeting .aah/ is in the correct subdirectory.
    Returns a reason string if blocked, None if allowed.
    """
    # Normalize path
    path = PurePosixPath(file_path)

    # Find .aah in the path
    parts = path.parts
    try:
        rapids_idx = list(parts).index(".aah")
    except ValueError:
        # Not writing to .aah/ — always allow
        return None

    # Get the path relative to .aah/
    relative_parts = parts[rapids_idx + 1:]
    if not relative_parts:
        return None

    # Check if it's a root-level writable file
    if len(relative_parts) == 1 and relative_parts[0] in ROOT_WRITABLE_FILES:
        return None

    # Get the top-level subdirectory
    subdir = relative_parts[0]

    # Check always-writable directories
    if subdir in ALWAYS_WRITABLE:
        return None

    # If we don't know the phase, allow (can't validate)
    if current_phase is None or current_phase in ("init", "complete"):
        return None

    # Check phase-appropriate directories
    allowed_dirs = PHASE_DIR_MAP.get(current_phase, [])
    if subdir not in allowed_dirs and subdir not in ALWAYS_WRITABLE:
        return (
            f"Blocked: writing to .aah/{subdir}/ is not allowed during the "
            f"'{current_phase}' phase. Allowed directories: {allowed_dirs + ALWAYS_WRITABLE}"
        )

    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    # Try to determine current phase from the session context first,
    # then fall back to reading claude-progress.json from cwd.
    session_context = hook_input.get("session_context", {})
    current_phase = session_context.get("current_phase")

    if not current_phase:
        from pathlib import Path as _Path
        # Derive project root from the file_path (parent of .aah/)
        fp = _Path(file_path)
        parts = fp.parts
        project_root = None
        try:
            rapids_idx = list(parts).index(".aah")
            project_root = _Path(*parts[:rapids_idx]) if rapids_idx > 0 else _Path("/")
        except ValueError:
            pass

        # Primary source: manifest.yaml (authoritative — updated by update-phase)
        if project_root:
            manifest_file = project_root / ".aah" / "manifest.yaml"
            if manifest_file.exists():
                try:
                    import yaml  # noqa: delayed import
                    manifest = yaml.safe_load(manifest_file.read_text(encoding='utf-8'))
                    current_phase = manifest.get("current_phase") if manifest else None
                except (OSError, ValueError) as exc:
                    print(f"validate_aah_path: failed to read manifest ({exc})", file=sys.stderr)

        # Fallback: claude-progress.json (may lag behind manifest)
        if not current_phase and project_root:
            progress_file = project_root / ".aah" / "claude-progress.json"
            if progress_file.exists():
                try:
                    progress = json.loads(progress_file.read_text(encoding='utf-8'))
                    current_phase = progress.get("current_phase")
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"validate_aah_path: failed to read progress ({exc})", file=sys.stderr)

        # Last resort: cwd from hook input
        if not current_phase:
            cwd = hook_input.get("cwd") or "."
            for fname, loader in [("manifest.yaml", "yaml"), ("claude-progress.json", "json")]:
                fpath = _Path(cwd) / ".aah" / fname
                if fpath.exists():
                    try:
                        raw = fpath.read_text(encoding='utf-8')
                        if loader == "yaml":
                            import yaml  # noqa: delayed import
                            data = yaml.safe_load(raw)
                        else:
                            data = json.loads(raw)
                        current_phase = data.get("current_phase") if data else None
                        if current_phase:
                            break
                    except (OSError, ValueError) as exc:
                        print(f"validate_aah_path: failed to read {fname} ({exc})", file=sys.stderr)

    reason = validate_path(file_path, current_phase)
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
