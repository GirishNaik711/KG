#!/usr/bin/env python3
"""Show the industry domain attached to the current project."""

import argparse
import sys
from pathlib import Path

from aah.core.common.manifest import load_manifest
from aah.core.domain_briefs.context import build_domain_context_summary
from aah.core.domain_briefs.loader import load_node


def show(project_path: Path) -> int:
    manifest = load_manifest(project_path / ".aah" / "manifest.yaml")
    if not isinstance(manifest, dict):
        print(f"no manifest at {project_path}/.aah/manifest.yaml", file=sys.stderr)
        return 1

    domain_path = manifest.get("industry_domain_path")
    if not domain_path:
        print("(no industry domain attached to this project)")
        return 0

    node = load_node(domain_path)
    if node is None:
        print(f"industry_domain_path is set to `{domain_path}` but no matching node found")
        return 1

    print(f"Industry domain: {domain_path}")
    print(f"Name: {node.get('name', '')}")
    print(f"Phase (research/analysis only get context injection): {manifest.get('current_phase')}")
    print()
    summary = build_domain_context_summary(project_path)
    if summary.strip():
        print(summary)
    else:
        print("(context summary empty — current phase may not trigger injection)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["show"])
    parser.add_argument("--project-path", type=Path, required=True)
    args = parser.parse_args()
    sys.exit(show(args.project_path))


if __name__ == "__main__":
    main()
