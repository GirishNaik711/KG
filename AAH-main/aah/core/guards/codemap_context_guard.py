#!/usr/bin/env python3
"""
PreToolUse hook guard: block source file writes until codemap context is gathered.

Fires on Write/Edit. If the agent is on a feature/* branch and writing to a
production source file, verifies that at least one codemap query has been
recorded in the feature YAML (codemap_context.queries). If not, blocks the
write — forcing the agent to run codemap queries first.

Exit 0 = allow, Exit 2 = block (stderr fed back to agent).
"""

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

# File extensions considered "source code" (production code that needs context)
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".kt", ".scala", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
    ".swift", ".dart", ".vue", ".svelte",
}

# Paths that are never blocked (even if they have source extensions)
EXEMPT_PATH_SEGMENTS = [
    "/tests/",
    "/test/",
    "/__tests__/",
    "/spec/",
    "/fixtures/",
    "/.aah/",
    "/.claude/",
    "/node_modules/",
    "/__pycache__/",
]

# Files that start with test_ are also exempt
EXEMPT_PREFIXES = ["test_", "conftest"]


def _is_source_file(file_path: str) -> bool:
    """Determine if a file path is a production source file."""
    path = PurePosixPath(file_path)

    # Check extension
    if path.suffix.lower() not in SOURCE_EXTENSIONS:
        return False

    # Check exempt path segments
    file_path_lower = file_path.lower().replace("\\", "/")
    for segment in EXEMPT_PATH_SEGMENTS:
        if segment in file_path_lower:
            return False

    # Check exempt filename prefixes
    name = path.name.lower()
    for prefix in EXEMPT_PREFIXES:
        if name.startswith(prefix):
            return False

    return True


def _get_feature_id_from_branch(cwd: str) -> str | None:
    """Extract feature ID from git branch name (feature/{id})."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        branch = result.stdout.strip()
        if branch.startswith("feature/"):
            return branch.removeprefix("feature/")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _find_project_path(cwd: str) -> Path | None:
    """Find the project root by looking for .aah/ directory."""
    current = Path(cwd)
    for _ in range(10):  # max 10 levels up
        if (current / ".aah").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
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

    # Only check source files
    if not _is_source_file(file_path):
        sys.exit(0)

    # Resolve context from the FILE being written, not from hook CWD.
    # When subagents run, hook CWD is the harness directory — but the file
    # being written lives in the project worktree (on the feature/* branch).
    # Using file_path's parent ensures we detect the correct git branch and
    # find the correct .aah/ directory.
    file_dir = str(Path(file_path).parent)

    # Try file's directory first, fall back to hook CWD
    feature_id = _get_feature_id_from_branch(file_dir)
    if not feature_id:
        # Fallback: try hook CWD (for cases where file_path is relative)
        cwd = hook_input.get("cwd", ".")
        feature_id = _get_feature_id_from_branch(cwd)
    if not feature_id:
        sys.exit(0)

    # Find project path from file's directory first, then CWD
    project_path = _find_project_path(file_dir)
    if not project_path:
        cwd = hook_input.get("cwd", ".")
        project_path = _find_project_path(cwd)
    if not project_path:
        sys.exit(0)

    # If no feature YAML exists for this feature ID, this is a bug fix or
    # rework branch — not a planned feature. Allow the write through since
    # there's no YAML to record codemap queries against.
    features_dir = project_path / ".aah" / "plan" / "features"
    has_feature_yaml = any(
        f.stem == feature_id or f.stem.upper() == feature_id.upper()
        for f in features_dir.glob("*.yaml")
    ) if features_dir.is_dir() else False
    if not has_feature_yaml:
        sys.exit(0)

    # Check if codemap queries exist using the existing check_queries function.
    # Wrapped in try/except: if codemap.db is absent, the module is missing, or
    # the DB query fails for any reason, allow the write silently — the guard's
    # job is to enforce context, not to block on its own infrastructure errors.
    try:
        from aah.core.build.codemap_runtime_query import check_queries
        result = check_queries(
            project_path=project_path,
            feature_id=feature_id,
            wave=None,
        )
    except Exception as e:
        print(
            f"[AAH codemap_context_guard] Skipping check — codemap unavailable: {e}",
            file=sys.stderr,
        )
        sys.exit(0)

    if result.get("has_queries"):
        # Queries exist (or auto-passed due to no codemap.db) — allow write
        sys.exit(0)

    # Block the write — agent must gather codemap context first
    print(
        f"BLOCKED: You must gather codemap context before writing source files.\n"
        f"\n"
        f"Feature '{feature_id}' has no codemap queries recorded yet.\n"
        f"Before writing production code, run codemap queries to understand\n"
        f"how your target files integrate with the rest of the codebase:\n"
        f"\n"
        f"  aah run core.build.codemap_runtime_query ask \\\n"
        f"    --project-path {project_path} \\\n"
        f"    --feature-id {feature_id} \\\n"
        f"    --wave <current_wave> \\\n"
        f"    --query \"<your question about codebase structure>\" \\\n"
        f"    --impact-files \"<files from file_scope>\"\n"
        f"\n"
        f"Run as many queries as you need to understand integration points,\n"
        f"callers, patterns, and dependencies. Then retry your write.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
