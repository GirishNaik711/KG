#!/usr/bin/env python3
"""Project and framework path resolution — folder=project model.

There is no ``aah-config.yaml``, no workspace, and no active-project pointer.
A folder that contains ``.aah/manifest.yaml`` **is** a AAH project. Every
resolver here works by walking up from cwd (or an explicit path) to find that
anchor. Framework assets are resolved package-relative, independent of cwd.

Per-project settings that used to live in config (git branch names, complexity
tier) live in ``.aah/manifest.yaml`` and are read from there by their owners.
"""

import argparse
import sys
from pathlib import Path


def _walk_up_for_aah(start: Path) -> Path | None:
    """Return the first ancestor of ``start`` (inclusive) holding .aah/manifest.yaml.

    Reads ancestors only — never writes, never inspects siblings. Bounded at the
    filesystem root. Needed when cwd is a subdir of the project root (e.g. the
    user opens ``claude`` in ``myapp/backend``); in-worktree agent calls resolve
    at depth 0 because ``.aah/`` is git-tracked and checked out in the worktree.
    """
    for directory in [start, *start.parents]:
        if (directory / ".aah" / "manifest.yaml").exists():
            return directory
        if directory == directory.parent:
            break
    return None


def resolve_framework_root(config: dict | None = None, config_path: Path | None = None) -> Path | None:
    """Resolve the AAH framework asset root.

    Framework assets (``activity-library/``, ``build-playbooks/``,
    ``domain-briefs/``) are shipped as package data under the installed ``aah``
    package, so the root is simply the package directory — resolved
    package-relative, independent of cwd or install location.

    ``config`` / ``config_path`` are accepted for backwards compatibility with
    existing callers but are ignored.
    """
    # aah/core/common/config.py → parents[2] == aah/ (the package dir)
    return Path(__file__).resolve().parents[2]


def resolve_project_path(explicit_path: Path | None = None) -> Path | None:
    """Resolve the project directory (the folder holding ``.aah/``).

    Priority:
    1. Explicit ``--project-path`` argument (accepts the project dir or its
       ``.aah/`` dir directly).
    2. Walk up from cwd for ``.aah/manifest.yaml``.

    Returns ``None`` if no project is found (callers handle gracefully).
    """
    if explicit_path is not None:
        if (explicit_path / ".aah" / "manifest.yaml").exists():
            return explicit_path
        # Maybe they pointed at .aah/ directly
        if explicit_path.name == ".aah" and (explicit_path / "manifest.yaml").exists():
            return explicit_path.parent

    return _walk_up_for_aah(Path.cwd())


def get_active_project_aah_path() -> Path | None:
    """Resolve the project's ``.aah/`` directory by walking up from cwd."""
    project = _walk_up_for_aah(Path.cwd())
    return (project / ".aah") if project is not None else None


# Back-compat alias: the ``.rapids/`` → ``.aah/`` rename renamed this function
# but three callers (iteration.manager, intake.intake, logging.activity_logger)
# still import the old name. Keep the old name pointing at the new function so
# those callers don't ImportError.
get_active_project_rapids_path = get_active_project_aah_path


def require_project_path(explicit_path: Path | None = None) -> Path:
    """Resolve project path, exiting with a helpful message if not found.

    Use this in CLI main() functions that require an active project. For hook
    scripts, use resolve_project_path() and handle None gracefully.
    """
    path = resolve_project_path(explicit_path)
    if path is None:
        msg = "No AAH project found."
        if explicit_path is not None:
            msg += f"\n  Searched: {explicit_path} (no .aah/manifest.yaml found)"
        msg += (
            "\n  - cd to your project folder (the one containing .aah/), or"
            "\n  - point at it explicitly: --project-path <path>, or"
            "\n  - start a new project: run /aah-init-project in your project folder"
        )
        print(msg, file=sys.stderr)
        sys.exit(1)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH path resolver")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("project-path", help="Print the project's absolute path (walks up from cwd)")
    sub.add_parser("aah-path", help="Print the project's .aah/ absolute path")
    sub.add_parser("framework-path", help="Print the framework asset root (the installed aah package dir)")
    sub.add_parser("mode", help="Print 'project' if cwd resolves to a project, else 'none'")

    args = parser.parse_args()

    if args.command == "project-path":
        project_path = resolve_project_path()
        if project_path is None:
            print("Error: no AAH project found (no .aah/manifest.yaml from cwd upward)", file=sys.stderr)
            sys.exit(1)
        print(project_path)

    elif args.command == "aah-path":
        aah_path = get_active_project_aah_path()
        if aah_path is None:
            print("Error: no active project .aah/ found", file=sys.stderr)
            sys.exit(1)
        print(aah_path)

    elif args.command == "framework-path":
        fw_path = resolve_framework_root()
        if fw_path is None:
            print("Error: cannot determine framework root", file=sys.stderr)
            sys.exit(1)
        print(fw_path)

    elif args.command == "mode":
        print("project" if resolve_project_path() is not None else "none")

    sys.exit(0)


if __name__ == "__main__":
    main()
