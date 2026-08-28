"""Canonical verification-profile catalog readers, hashes, and bindings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_yaml


PROFILE_LEVELS = frozenset({"standard", "deep"})


def _aah_path(project_or_aah_path: Path) -> Path:
    path = Path(project_or_aah_path)
    return path if path.name == ".aah" else path / ".aah"


def canonical_profile_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def profile_hash(value: object) -> str:
    return hashlib.sha256(canonical_profile_bytes(value)).hexdigest()


def catalog_hash(profiles: dict) -> str:
    return profile_hash(profiles)


verification_profiles_hash = catalog_hash


def load_profile_catalog(
    project_or_aah_path: Path,
) -> tuple[dict[str, dict] | None, str]:
    """Load the checkpoint verification-profile mapping with stable reasons."""
    path = _aah_path(project_or_aah_path) / "plan" / "checkpoint-config.yaml"
    if not path.exists():
        return None, "missing_profile"
    try:
        config = read_yaml(path)
    except Exception:
        return None, "malformed_checkpoint_config"
    if not isinstance(config, dict):
        return None, "malformed_checkpoint_config"
    checkpoint = config.get("checkpoint_configuration")
    if not isinstance(checkpoint, dict):
        return None, "missing_verification_profiles"
    profiles = checkpoint.get("verification_profiles")
    if not isinstance(profiles, dict):
        return None, "missing_verification_profiles"
    if validate_profile_catalog(profiles):
        return None, "malformed_verification_profiles"
    return profiles, ""


def load_verification_profiles(project_or_aah_path: Path) -> dict[str, dict] | None:
    profiles, _reason = load_profile_catalog(project_or_aah_path)
    return profiles


def validate_profile_catalog(profiles: object) -> list[str]:
    """Return fail-closed structural errors for a persisted profile catalog."""
    if not isinstance(profiles, dict):
        return ["verification_profiles must be a mapping"]
    errors: list[str] = []
    for feature_id, entry in profiles.items():
        label = str(feature_id)
        if not isinstance(feature_id, str) or not feature_id:
            errors.append("verification_profiles keys must be non-empty strings")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{label}: profile must be a mapping")
            continue
        if entry.get("level") not in PROFILE_LEVELS:
            errors.append(f"{label}: level must be standard or deep")
        reasons = entry.get("reasons", [])
        if not isinstance(reasons, list) or any(
            not isinstance(item, str) for item in reasons
        ):
            errors.append(f"{label}: reasons must be a list of strings")
    return errors


def resolve_profile_override(
    project_path: Path,
    feature_id: str,
    *,
    subject_sha: str | None,
    checkpoint_config_hash: str,
) -> dict | None:
    if not subject_sha:
        return None
    try:
        from aah.core.build.profile_override import read_current_override

        return read_current_override(
            Path(project_path),
            feature_id,
            subject_sha=subject_sha,
            checkpoint_config_hash=checkpoint_config_hash,
        )
    except Exception:
        return None


def profile_binding_for_feature(
    project_path: Path,
    feature_id: str,
    *,
    subject_sha: str | None = None,
) -> dict[str, Any]:
    """Return the current profile as a QA-time routing snapshot."""
    sentinel: dict[str, Any] = {
        "level": None,
        "rule_version": None,
        "reasons": [],
        "checkpoint_config_hash": None,
        "override_ref": None,
        "no_signal": "missing_profile",
    }
    profiles, reason = load_profile_catalog(project_path)
    if profiles is None:
        return {**sentinel, "no_signal": reason}
    entry = profiles.get(feature_id)
    if not isinstance(entry, dict):
        return dict(sentinel)
    level = entry.get("level")
    if level not in PROFILE_LEVELS:
        return {**sentinel, "no_signal": "malformed_profile_level"}

    binding_hash = profile_hash(entry)
    override = resolve_profile_override(
        project_path,
        feature_id,
        subject_sha=subject_sha,
        checkpoint_config_hash=binding_hash,
    )
    if override is None:
        legacy_binding_hash = catalog_hash(profiles)
        if legacy_binding_hash != binding_hash:
            override = resolve_profile_override(
                project_path,
                feature_id,
                subject_sha=subject_sha,
                checkpoint_config_hash=legacy_binding_hash,
            )
    reasons = entry.get("reasons", [])
    return {
        "level": level,
        "rule_version": entry.get("rule_version"),
        "reasons": list(reasons or []) if isinstance(reasons, list) else [],
        "checkpoint_config_hash": binding_hash,
        "override_ref": override.get("override") if override else None,
        "no_signal": None,
    }


def effective_profile_level(binding: dict[str, Any]) -> str | None:
    if binding.get("no_signal") is not None:
        return None
    level = binding.get("level")
    if level == "deep" and binding.get("override_ref") is not None:
        return "standard"
    return level if level in PROFILE_LEVELS else None
