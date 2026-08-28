"""CLI for the language adapter pack.

Used by the aah-runtime-validator subagent (which shells out, doesn't
import Python) and as a debugging interface for users.

Usage:
    aah run core.build.lang_checks.cli describe \\
      --kind <test|module_validation|compile|build|lint|static_analysis|coverage|deps_consistency|start|cache_clean> \\
      [--feature-filter F001] \\
      [--port 8000] \\
      [--project-path PATH]

Output (stdout, JSON):
    {
      "kind": "<kind>",
      "adapter": "<python|node|go|rust|java|generic>",
      "supported": true | false,
      "command": null | {
        "argv": ["uv", "run", "pytest", ...],
        "cwd": "/path/to/project",
        "timeout_sec": 300,
        "label": "pytest"
      }
    }

Exit codes:
    0 — JSON written to stdout (whether supported true or false).
    2 — argument or project-state error (bad --kind, project not found).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from aah.core.build.lang_checks import Cmd, detect, resolve_test_command


# Public ``--kind`` values map to the corresponding adapter method.
# `kind == "test"` calls ``adapter.test_command(feature_filter=...)``.
# `kind == "compile"` aliases to module_validation_command(None).
_KIND_DISPATCH = {
    "test": "test_command",
    "module_validation": "module_validation_command",
    "compile": "compile_or_typecheck",
    "build": "build",
    "lint": "lint_command",
    "static_analysis": "static_analysis_command",
    "coverage": "coverage_command",
    "deps_consistency": "deps_consistency_check",
    "start": "start_command",
    "cache_clean": "cache_clean",
}


def _serialize_cmd(cmd: Cmd | None) -> dict | None:
    if cmd is None:
        return None
    d = dataclasses.asdict(cmd)
    # Path is not JSON-serializable; coerce to string.
    if d.get("cwd") is not None:
        d["cwd"] = str(d["cwd"])
    return d


def _resolve_command(
    adapter, kind: str, feature_filter: str | None, port: int
):
    """Call the right adapter method for the given kind, with the
    right arguments. Returns ``Cmd | list[Cmd] | None``.
    """
    method_name = _KIND_DISPATCH[kind]
    method = getattr(adapter, method_name)
    if kind == "test":
        return method(feature_filter=feature_filter)
    if kind == "module_validation":
        return method(None)
    if kind == "start":
        return method(port=port)
    # Default: no-arg invocation for the rest.
    return method()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Describe per-language commands for the active project",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    describe = sub.add_parser(
        "describe",
        help="Return the resolved command for a given operation.",
    )
    describe.add_argument(
        "--kind",
        required=True,
        choices=sorted(_KIND_DISPATCH.keys()),
        help="What command to resolve.",
    )
    describe.add_argument(
        "--feature-filter",
        default=None,
        help="Feature ID to scope tests to (only for --kind test).",
    )
    describe.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for --kind start (default: 8000).",
    )
    describe.add_argument(
        "--project-path",
        type=Path,
        default=None,
        help="Project root (default: resolve via aah-config or cwd).",
    )

    args = parser.parse_args()

    if args.command == "describe":
        from aah.core.common.config import resolve_project_path
        project_path = resolve_project_path(args.project_path)
        if project_path is None:
            print(
                "Error: could not resolve a project path. Pass --project-path "
                "or run from a project directory.",
                file=sys.stderr,
            )
            sys.exit(2)

        adapter = detect(project_path)
        if args.kind == "test":
            result = resolve_test_command(project_path, args.feature_filter)
        else:
            result = _resolve_command(
                adapter, args.kind, args.feature_filter, args.port
            )

        # cache_clean returns list[Cmd]; everything else returns Cmd | None.
        if args.kind == "cache_clean":
            commands = [_serialize_cmd(c) for c in (result or [])]
            payload = {
                "kind": args.kind,
                "adapter": adapter.name,
                "supported": bool(commands),
                # For cache_clean, expose `commands` (list) rather than
                # singular `command`. Callers iterate.
                "commands": commands,
            }
        else:
            cmd_dict = _serialize_cmd(result)
            payload = {
                "kind": args.kind,
                "adapter": adapter.name,
                "supported": cmd_dict is not None,
                "command": cmd_dict,
            }

        json.dump(payload, sys.stdout, indent=2)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
