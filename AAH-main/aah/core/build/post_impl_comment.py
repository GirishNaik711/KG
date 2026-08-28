"""Post implementation reasoning as a GitHub issue comment.

Called immediately after update_reasoning completes for a feature.
Reads implementation_reasoning from the feature YAML, looks up the
GitHub issue number from the VC sync ledger, and posts a comment.

Non-fatal: if VC is disabled, the feature has no remote ref, or the
network is unreachable, a warning is printed and the process exits 0.
"""

import argparse
import sys
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.feature_utils import find_feature_file


def _parse_reasoning_from_markdown(content: str) -> dict:
    """Extract initial_approach and revisions from ## Implementation Reasoning section."""
    in_section = False
    initial_lines: list[str] = []
    revisions: list[dict] = []
    current_rev: dict | None = None

    for line in content.splitlines():
        if line.startswith("## Implementation Reasoning"):
            in_section = True
            continue
        if in_section and line.startswith("## ") and not line.startswith("## Implementation Reasoning"):
            break
        if not in_section:
            continue
        if line.startswith("### Initial Approach"):
            current_rev = None
            continue
        if line.startswith("### Revision"):
            if current_rev is not None:
                revisions.append(current_rev)
            current_rev = {"trigger": "", "change": ""}
            continue
        if current_rev is not None:
            if line.startswith("- Trigger:"):
                current_rev["trigger"] = line[len("- Trigger:"):].strip()
            elif line.startswith("- Change:"):
                current_rev["change"] = line[len("- Change:"):].strip()
        elif not line.startswith("_Timestamp"):
            initial_lines.append(line)

    if current_rev is not None:
        revisions.append(current_rev)

    return {
        "initial_approach": "\n".join(initial_lines).strip(),
        "revisions": revisions,
    }


def _build_comment_body(feature_id: str, reasoning: dict) -> str:
    lines = [f"## Implementation Reasoning — {feature_id}", ""]

    approach = reasoning.get("initial_approach", "").strip()
    if approach:
        lines += ["### Initial Approach", "", approach, ""]

    revisions = reasoning.get("revisions") or []
    if revisions:
        lines.append("### Revisions")
        lines.append("")
        for i, rev in enumerate(revisions, 1):
            trigger = (rev.get("trigger") or "").strip()
            change = (rev.get("change") or "").strip()
            lines.append(f"**Revision {i}**")
            if trigger:
                lines.append(f"- Trigger: {trigger}")
            if change:
                lines.append(f"- Change: {change}")
            lines.append("")

    return "\n".join(lines).rstrip()


# Kept as a re-export so callers that already import
# ``has_impl_comment_on_tracker`` from this module continue to work; the
# canonical definition now lives in ``tracker_state``.
from aah.core.build.tracker_state import has_impl_comment_on_tracker  # noqa: F401


def post_impl_comment(
    feature_id: str,
    project_path: Path | None = None,
    yaml_path_override: Path | None = None,
) -> bool:
    """Return True on success or graceful skip, False only on unexpected error."""
    project_dir = resolve_project_path(project_path)
    if project_dir is None:
        print("Warning: post_impl_comment — could not resolve project path; skipping.", file=sys.stderr)
        return True

    aah_path = project_dir / ".aah"

    # --- load VC config (non-fatal if disabled) ---
    try:
        from aah.core.version_control.config import load_vc_config
        config = load_vc_config(aah_path / "manifest.yaml")
    except Exception as e:  # noqa: BLE001
        print(f"Warning: post_impl_comment — could not load VC config: {e}; skipping.", file=sys.stderr)
        return True

    if not config.enabled:
        print(f"[post_impl_comment] version_control disabled — skipping comment for {feature_id}.")
        return True

    # --- resolve feature YAML path ---
    if yaml_path_override:
        yaml_path = yaml_path_override
    else:
        features_dir = aah_path / "plan" / "features"
        yaml_path = find_feature_file(features_dir, feature_id)

    if not yaml_path or not yaml_path.exists():
        print(f"Warning: post_impl_comment — feature file not found for {feature_id}; skipping.", file=sys.stderr)
        return True

    # --- read implementation_reasoning from markdown section ---
    # implementation_reasoning is stored as markdown sub-headings under
    # ## Implementation Reasoning, not as a YAML dict.
    try:
        content = yaml_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Warning: post_impl_comment — could not read feature file: {e}; skipping.", file=sys.stderr)
        return True

    reasoning = _parse_reasoning_from_markdown(content)
    if not reasoning.get("initial_approach"):
        print(f"Warning: post_impl_comment — no initial_approach in {feature_id}; skipping.", file=sys.stderr)
        return True

    # --- look up GitHub issue number from sync ledger ---
    try:
        from aah.core.version_control.ledger import SyncLedger
        ledger = SyncLedger(aah_path)
        remote_ref = ledger.remote_ref(feature_id)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: post_impl_comment — could not read sync ledger: {e}; skipping.", file=sys.stderr)
        return True

    if remote_ref is None or remote_ref.number is None:
        print(f"[post_impl_comment] No remote ref for {feature_id} — feature not yet synced to tracker; skipping.")
        return True

    issue_number = remote_ref.number

    # --- build comment body and initialise provider ---
    body = _build_comment_body(feature_id, reasoning)
    expected_header = f"## Implementation Reasoning — {feature_id}"

    try:
        from aah.core.version_control.providers.registry import get_provider
        from aah.core.version_control.providers.base import ProviderOffline
        from aah.core.version_control.models import ItemRef
        provider = get_provider(config)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: post_impl_comment — could not initialise provider: {e}; skipping.", file=sys.stderr)
        return True

    # --- idempotency guard: check GitHub for an existing matching comment.
    # GitHub is the source of truth. A local flag file cannot survive branch
    # switches performed by merge_wave_to_integration (working-tree state is
    # replaced when the tool checks out integration/develop), so we consult
    # the tracker itself.
    try:
        existing = provider.fetch_comments(ItemRef(number=issue_number), since_id=None)
        if any((c.body or "").lstrip().startswith(expected_header) for c in existing):
            print(
                f"[post_impl_comment] Comment already exists on issue #{issue_number} "
                f"for {feature_id} — skipping duplicate."
            )
            return True
    except ProviderOffline as e:
        print(
            f"Warning: post_impl_comment — tracker unreachable during dedup check: {e}; "
            f"skipping to avoid a possible duplicate.",
            file=sys.stderr,
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(
            f"Warning: post_impl_comment — dedup check failed: {e}; skipping to be safe.",
            file=sys.stderr,
        )
        return True

    # --- post the comment ---
    try:
        provider.comment(issue_number, body)
        print(f"[post_impl_comment] Posted implementation reasoning to issue #{issue_number} for {feature_id}.")
    except ProviderOffline as e:
        print(f"Warning: post_impl_comment — tracker unreachable: {e}; skipping.", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: post_impl_comment — failed to post comment: {e}; skipping.", file=sys.stderr)

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Post implementation reasoning as a GitHub issue comment")
    parser.add_argument("--feature-id", required=True, help="Feature ID (e.g., F-MOD001-01)")
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
        help="Explicit path to the feature YAML file (use in worktree mode)",
    )
    args = parser.parse_args()
    post_impl_comment(
        feature_id=args.feature_id,
        project_path=args.project_path,
        yaml_path_override=args.yaml_path,
    )


if __name__ == "__main__":
    main()
