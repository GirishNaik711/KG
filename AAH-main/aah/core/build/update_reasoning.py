"""Update implementation_reasoning in feature file.

Provides a deterministic script for aah-feature-implementer agents to persist
their reasoning trace (initial approach and revisions) into the feature .md file.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from aah.core.common.feature_utils import find_feature_file, append_section
from aah.core.common.config import resolve_project_path


def update_reasoning(
    feature_id: str,
    reasoning_type: str,
    project_path: Path | None = None,
    yaml_path_override: Path | None = None,
    **kwargs,
) -> None:
    """Write or append implementation reasoning to feature .md file as markdown."""
    if yaml_path_override:
        yaml_path = yaml_path_override
    else:
        project_dir = resolve_project_path(project_path)
        if project_dir is None:
            print("ERROR: Could not resolve project path.", file=sys.stderr)
            sys.exit(1)
        features_dir = project_dir / ".aah" / "plan" / "features"
        yaml_path = find_feature_file(features_dir, feature_id)
        if not yaml_path:
            print(f"ERROR: Feature file not found for {feature_id} in {features_dir}", file=sys.stderr)
            sys.exit(1)

    if not yaml_path.exists():
        print(f"ERROR: Feature file not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)

    if reasoning_type == "initial":
        section_lines = [
            f"### Initial Approach",
            "",
            kwargs["content"],
            "",
            f"_Timestamp: {datetime.now().isoformat()}_",
        ]
        append_section(yaml_path, "Implementation Reasoning", section_lines, accumulate=True)
        print(f"Wrote initial_approach for {feature_id}")

    elif reasoning_type == "revision":
        content = yaml_path.read_text(encoding="utf-8")
        rev_num = content.count("### Revision") + 1
        section_lines = [
            f"### Revision {rev_num}",
            f"- Trigger: {kwargs['trigger']}",
            f"- Change: {kwargs['change']}",
            f"- Timestamp: {datetime.now().isoformat()}",
        ]
        append_section(yaml_path, "Implementation Reasoning", section_lines, accumulate=True)
        print(f"Appended revision #{rev_num} for {feature_id}")


def main():
    parser = argparse.ArgumentParser(description="Update implementation reasoning")
    parser.add_argument(
        "--feature-id", required=True, help="Feature ID (e.g., F025)"
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=["initial", "revision"],
        help="Type of reasoning update",
    )
    parser.add_argument(
        "--content", help="Initial approach content (required for --type initial)"
    )
    parser.add_argument(
        "--trigger",
        help="What triggered the revision (required for --type revision)",
    )
    parser.add_argument(
        "--change",
        help="What was changed and why (required for --type revision)",
    )
    parser.add_argument(
        "--project-path",
        type=Path,
        default=None,
        help="Explicit project path (optional, resolves from config if omitted)",
    )
    parser.add_argument(
        "--yaml-path",
        type=Path,
        default=None,
        help="Explicit path to the feature YAML file (bypasses project-path resolution — use in worktree mode)",
    )

    args = parser.parse_args()

    if args.type == "initial" and not args.content:
        parser.error("--content is required when --type is 'initial'")
    if args.type == "revision" and (not args.trigger or not args.change):
        parser.error("--trigger and --change are required when --type is 'revision'")

    update_reasoning(
        feature_id=args.feature_id,
        reasoning_type=args.type,
        project_path=args.project_path,
        yaml_path_override=args.yaml_path,
        content=args.content,
        trigger=args.trigger,
        change=args.change,
    )


if __name__ == "__main__":
    main()
