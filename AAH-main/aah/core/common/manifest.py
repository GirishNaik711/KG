#!/usr/bin/env python3
"""Read/write manifest.yaml — the per-project metadata registry."""

import argparse
import json
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping

from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.common.validators import validate_dict_schema, MANIFEST_SCHEMA


MANIFEST_FILENAME = "manifest.yaml"

DEFAULT_FEATURES = {
    "runtime_profile_v1": "report_only",
    "runtime_cloud_probes_v1": False,
    "semantic_smoke_v2": False,
}

VERIFICATION_ROLLOUT_DEFAULTS = DEFAULT_FEATURES


class RolloutMode(str, Enum):
    OFF = "off"
    REPORT_ONLY = "report_only"
    ENFORCE = "enforce"


def _manifest_path(path: Path) -> Path:
    if path.name == MANIFEST_FILENAME or path.suffix in (".yaml", ".yml"):
        return path
    return path / MANIFEST_FILENAME if path.name == ".aah" else path / ".aah" / MANIFEST_FILENAME


def _rollout_features(manifest_or_path: Mapping | Path) -> tuple[Mapping, bool]:
    """Return ``(features, malformed)`` without consulting the environment."""
    if isinstance(manifest_or_path, Mapping):
        manifest: object = manifest_or_path
    else:
        path = _manifest_path(Path(manifest_or_path))
        if not path.exists():
            return {}, False
        try:
            manifest = read_yaml(path)
        except Exception:
            return {}, True
    if not isinstance(manifest, Mapping):
        return {}, True
    if "features" not in manifest or manifest.get("features") is None:
        return {}, False
    features = manifest.get("features")
    return (features, False) if isinstance(features, Mapping) else ({}, True)


def rollout_value(manifest_or_path: Mapping | Path, feature_name: str) -> bool | str:
    """Read a declared rollout; malformed values enforce and env is ignored."""
    if feature_name not in DEFAULT_FEATURES:
        raise KeyError(f"unknown rollout feature: {feature_name}")
    features, malformed = _rollout_features(manifest_or_path)
    if malformed:
        return True
    value = features.get(feature_name, DEFAULT_FEATURES[feature_name])
    if type(value) is bool or value == "report_only":
        return value
    return True


def rollout_mode(manifest_or_path: Mapping | Path, feature_name: str) -> RolloutMode:
    """Return ``off``, ``report_only``, or ``enforce`` for a rollout flag."""
    value = rollout_value(manifest_or_path, feature_name)
    if value is True:
        return RolloutMode.ENFORCE
    if value == "report_only":
        return RolloutMode.REPORT_ONLY
    return RolloutMode.OFF

# Legacy project_type values that have been merged/renamed. On read, they
# translate to their current replacement and emit a one-time DeprecationWarning.
LEGACY_PROJECT_TYPE_ALIASES = {
    "ai-agents": "ai-infra-platforms",
}


def get_default_manifest(project_name: str, project_type: str = "greenfield") -> dict:
    """Return a default manifest for a new project."""
    return {
        "project_name": project_name,
        "project_type": project_type,
        "current_phase": "init",
        "complexity_tier": None,
        "completed_artifacts": [],
        "stack_choices": {},
        "features": dict(DEFAULT_FEATURES),
        "dag_state": None,
        "current_iteration": 1,
        "branching_config": {
            "main_branch": "main",
            "develop_branch": "develop",
            "integration_prefix": "integration/wave-",
        },
        # industry_domain_path is set by /aah-discuss when the user
        # confirms a Domain Brief attachment. None means no industry domain
        # is attached (domain-context injection is skipped).
        "industry_domain_path": None,
        "last_session_summary": None,
        "execution_mode": "aah_root",
        "delegated_to": None,
        "delegation_timestamp": None,
        "synthesis_mode": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def find_manifest(start_dir: Path | None = None) -> Path | None:
    """Find manifest.yaml. If start_dir given, walk up from it. Otherwise use config."""
    # If explicit start_dir, walk up from it first
    if start_dir:
        for directory in [start_dir, *start_dir.parents]:
            candidate = directory / ".aah" / MANIFEST_FILENAME
            if candidate.exists():
                return candidate
        return None
    # No start_dir: resolve from aah-config.yaml
    from aah.core.common.config import get_active_project_aah_path
    aah_path = get_active_project_aah_path()
    if aah_path:
        candidate = aah_path / MANIFEST_FILENAME
        if candidate.exists():
            return candidate
    return None


def load_manifest(manifest_path: Path | None = None) -> dict:
    """Load manifest.yaml from the given path or by searching.

    Translates legacy project_type values (ai-agents, ai-platforms) to their
    current equivalent (ai-infra-platforms) on read, emitting a
    DeprecationWarning so legacy projects continue to work after the
    technology-domain taxonomy was refactored.
    """
    if manifest_path is None:
        manifest_path = find_manifest()
    if manifest_path is None:
        print("Error: manifest.yaml not found. Run 'aah-scaffold' to create a project.", file=sys.stderr)
        sys.exit(1)
    if not manifest_path.exists():
        print(f"Error: manifest.yaml not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)
    data = read_yaml(manifest_path)
    legacy = data.get("project_type")
    if legacy in LEGACY_PROJECT_TYPE_ALIASES:
        import warnings
        new_type = LEGACY_PROJECT_TYPE_ALIASES[legacy]
        warnings.warn(
            f"manifest.project_type=`{legacy}` is deprecated; translating to `{new_type}`. "
            "Update manifest.yaml to the new value to silence this warning.",
            DeprecationWarning,
            stacklevel=2,
        )
        data["project_type"] = new_type
    return data


def save_manifest(data: dict, manifest_path: Path) -> None:
    """Save manifest.yaml, updating the timestamp."""
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_yaml(data, manifest_path)


def validate_manifest(data: dict) -> list[str]:
    """Validate manifest against schema. Returns list of errors."""
    errors = validate_dict_schema(data, MANIFEST_SCHEMA, context="manifest.yaml")
    features = data.get("features")
    if features is not None and not isinstance(features, dict):
        errors.append("manifest.yaml: field 'features' expected dict")
        return errors
    features = features or {}
    for name in ("runtime_profile_v1",):
        value = features.get(name)
        if name in features and not (type(value) is bool or value == "report_only"):
            errors.append(
                f"manifest.yaml: features.{name} must be false, report_only, or true"
            )
    for name in ("runtime_cloud_probes_v1", "semantic_smoke_v2"):
        if name in features and type(features[name]) is not bool:
            errors.append(f"manifest.yaml: features.{name} must be boolean")
    return errors


def migrate_verification_flags(manifest_path: Path) -> dict:
    """Add missing verification rollout controls without changing existing values."""
    data = load_manifest(manifest_path)
    features = data.setdefault("features", {})
    if not isinstance(features, dict):
        raise ValueError("manifest features must be a mapping")
    for name, default in DEFAULT_FEATURES.items():
        features.setdefault(name, default)
    errors = validate_manifest(data)
    if errors:
        raise ValueError("; ".join(errors))
    save_manifest(data, manifest_path)
    return data


def update_phase(manifest_path: Path, phase: str) -> dict:
    """Update the current phase in manifest and sync to claude-progress.json."""
    valid_phases = ["init", "discuss", "architecture", "plan", "build", "deploy", "complete"]
    if phase not in valid_phases:
        print(f"Error: invalid phase '{phase}'. Must be one of {valid_phases}", file=sys.stderr)
        sys.exit(1)
    data = load_manifest(manifest_path)
    data["current_phase"] = phase
    save_manifest(data, manifest_path)

    # Keep claude-progress.json in sync so guards see the same phase
    progress_file = manifest_path.parent / "claude-progress.json"
    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding='utf-8'))
            if progress.get("current_phase") != phase:
                progress["current_phase"] = phase
                progress_file.write_text(json.dumps(progress, indent=2) + "\n", encoding='utf-8')
        except Exception:
            pass  # Best-effort sync; manifest is authoritative

    print(f"Phase updated to '{phase}'")
    return data


def set_stack(manifest_path: Path, key: str, value: str) -> dict:
    """Set a technology stack choice in manifest."""
    data = load_manifest(manifest_path)
    if "stack_choices" not in data:
        data["stack_choices"] = {}
    data["stack_choices"][key] = value
    save_manifest(data, manifest_path)
    return data


def set_complexity(manifest_path: Path, tier: str) -> dict:
    """Set the complexity tier in manifest."""
    valid_tiers = ["trivial", "moderate", "significant", "complex"]
    if tier not in valid_tiers:
        print(f"Error: invalid tier '{tier}'. Must be one of {valid_tiers}", file=sys.stderr)
        sys.exit(1)
    data = load_manifest(manifest_path)
    data["complexity_tier"] = tier
    save_manifest(data, manifest_path)
    return data


def set_synthesis_mode(manifest_path: Path, mode: str) -> dict:
    """Set the synthesis mode in manifest."""
    valid_modes = ["light", "standard", "full"]
    if mode not in valid_modes:
        print(f"Error: invalid mode '{mode}'. Must be one of {valid_modes}", file=sys.stderr)
        sys.exit(1)
    data = load_manifest(manifest_path)
    data["synthesis_mode"] = mode
    save_manifest(data, manifest_path)
    return data


def add_artifact(manifest_path: Path, artifact: str) -> dict:
    """Add a completed artifact to the manifest."""
    data = load_manifest(manifest_path)
    if "completed_artifacts" not in data:
        data["completed_artifacts"] = []
    if artifact not in data["completed_artifacts"]:
        data["completed_artifacts"].append(artifact)
    save_manifest(data, manifest_path)
    return data


def set_execution_mode(manifest_path: Path, mode: str, plugin_id: str | None = None) -> dict:
    """Set the execution mode (aah_root or delegated) in manifest."""
    valid_modes = ["aah_root", "delegated"]
    if mode not in valid_modes:
        print(f"Error: invalid mode '{mode}'. Must be one of {valid_modes}", file=sys.stderr)
        sys.exit(1)
    data = load_manifest(manifest_path)
    data["execution_mode"] = mode
    data["delegated_to"] = plugin_id if mode == "delegated" else None
    data["delegation_timestamp"] = (
        datetime.now(timezone.utc).isoformat() if mode == "delegated" else None
    )
    save_manifest(data, manifest_path)
    return data


def set_version_control(
    manifest_path: Path,
    enabled: bool,
    provider: str = "github",
    repo: str | None = None,
    auth: str = "gh-cli",
) -> dict:
    """Write the version_control block in the manifest.

    Truth for the version_control module lives here (see
    ``core.version_control.config.load_vc_config``). Only the provider sub-block
    named by ``provider`` is written, so switching providers later never mixes
    config. When ``enabled`` is False the block is still written (disabled) so
    the choice is explicit and re-runs are idempotent.
    """
    data = load_manifest(manifest_path)
    block: dict = {"enabled": bool(enabled), "provider": provider}
    provider_block: dict = {}
    if repo:
        provider_block["repo"] = repo
    if auth:
        provider_block["auth"] = auth
    block[provider] = provider_block
    data["version_control"] = block
    save_manifest(data, manifest_path)
    return data


def get_status(manifest_path: Path) -> dict:
    """Get a status summary from the manifest."""
    data = load_manifest(manifest_path)
    return {
        "project_name": data.get("project_name"),
        "project_type": data.get("project_type"),
        "current_phase": data.get("current_phase"),
        "complexity_tier": data.get("complexity_tier"),
        "artifact_count": len(data.get("completed_artifacts", [])),
        "stack_choices": data.get("stack_choices", {}),
    }


def main() -> None:
    # Parent parser for shared --path argument (inherited by all subcommands)
    path_parent = argparse.ArgumentParser(add_help=False)
    path_parent.add_argument("--path", type=Path, default=None, help="Path to manifest.yaml")

    parser = argparse.ArgumentParser(description="AAH manifest manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read", help="Read and display manifest", parents=[path_parent])
    sub.add_parser("get-status", help="Get status summary", parents=[path_parent])
    sub.add_parser("validate", help="Validate manifest schema", parents=[path_parent])
    sub.add_parser(
        "migrate-verification-flags",
        help="Add the canonical verification rollout flags to an existing manifest",
        parents=[path_parent],
    )

    phase_p = sub.add_parser("update-phase", help="Update current phase", parents=[path_parent])
    phase_p.add_argument("phase", type=str)

    stack_p = sub.add_parser("set-stack", help="Set a stack choice", parents=[path_parent])
    stack_p.add_argument("key", type=str)
    stack_p.add_argument("value", type=str)

    tier_p = sub.add_parser("set-complexity", help="Set complexity tier", parents=[path_parent])
    tier_p.add_argument("tier", type=str)

    art_p = sub.add_parser("add-artifact", help="Record a completed artifact", parents=[path_parent])
    art_p.add_argument("artifact", type=str)

    exec_p = sub.add_parser("set-execution-mode", help="Set execution mode", parents=[path_parent])
    exec_p.add_argument("mode", type=str, choices=["aah_root", "delegated"])
    exec_p.add_argument("--plugin", type=str, default=None, help="Plugin ID when delegating")

    vc_p = sub.add_parser(
        "set-version-control",
        help="Enable/disable issue-tracker sync and record the provider repo",
        parents=[path_parent],
    )
    vc_group = vc_p.add_mutually_exclusive_group(required=True)
    vc_group.add_argument("--enable", dest="enabled", action="store_true")
    vc_group.add_argument("--disable", dest="enabled", action="store_false")
    vc_p.add_argument("--provider", type=str, default="github")
    vc_p.add_argument("--repo", type=str, default=None, help="e.g. owner/name")
    vc_p.add_argument("--auth", type=str, default="gh-cli")

    init_p = sub.add_parser("init", help="Create a new manifest", parents=[path_parent])
    init_p.add_argument("project_name", type=str)
    init_p.add_argument("--type", dest="project_type", default="greenfield", choices=["greenfield", "brownfield"])

    args = parser.parse_args()
    manifest_path = args.path
    if manifest_path is not None and (manifest_path.is_dir() or manifest_path.suffix not in (".yaml", ".yml")):
        manifest_path = manifest_path / ".aah" / MANIFEST_FILENAME if manifest_path.name != ".aah" else manifest_path / MANIFEST_FILENAME

    if args.command == "init":
        if manifest_path is None:
            manifest_path = Path.cwd() / ".aah" / MANIFEST_FILENAME
        data = get_default_manifest(args.project_name, args.project_type)
        save_manifest(data, manifest_path)
        json.dump(data, sys.stdout, indent=2, default=str)
        print()
        sys.exit(0)

    # For all other commands, find the manifest
    if manifest_path is None:
        manifest_path = find_manifest()
        if manifest_path is None:
            print("Error: manifest.yaml not found", file=sys.stderr)
            sys.exit(1)

    if args.command == "read":
        data = load_manifest(manifest_path)
        json.dump(data, sys.stdout, indent=2, default=str)
        print()

    elif args.command == "get-status":
        status = get_status(manifest_path)
        json.dump(status, sys.stdout, indent=2)
        print()

    elif args.command == "validate":
        data = load_manifest(manifest_path)
        errors = validate_manifest(data)
        if errors:
            json.dump({"valid": False, "errors": errors}, sys.stdout, indent=2)
            print()
            sys.exit(1)
        json.dump({"valid": True, "errors": []}, sys.stdout, indent=2)
        print()

    elif args.command == "migrate-verification-flags":
        try:
            data = migrate_verification_flags(manifest_path)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        json.dump({"features": data["features"]}, sys.stdout, indent=2)
        print()

    elif args.command == "update-phase":
        update_phase(manifest_path, args.phase)
        print(f"Phase updated to '{args.phase}'", file=sys.stderr)

    elif args.command == "set-stack":
        set_stack(manifest_path, args.key, args.value)
        print(f"Stack '{args.key}' set to '{args.value}'", file=sys.stderr)

    elif args.command == "set-complexity":
        set_complexity(manifest_path, args.tier)
        print(f"Complexity set to '{args.tier}'", file=sys.stderr)

    elif args.command == "add-artifact":
        add_artifact(manifest_path, args.artifact)
        print(f"Artifact '{args.artifact}' recorded", file=sys.stderr)

    elif args.command == "set-execution-mode":
        result = set_execution_mode(manifest_path, args.mode, args.plugin)
        json.dump(
            {
                "execution_mode": result["execution_mode"],
                "delegated_to": result.get("delegated_to"),
                "delegation_timestamp": result.get("delegation_timestamp"),
            },
            sys.stdout,
            indent=2,
        )
        print()

    elif args.command == "set-version-control":
        result = set_version_control(
            manifest_path, args.enabled, args.provider, args.repo, args.auth
        )
        json.dump({"version_control": result["version_control"]}, sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()
