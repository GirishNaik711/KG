"""Fail-closed JSON/YAML attestation boundary with verified-only payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Sequence

import yaml

from aah.core.common import attestation
from aah.core.common.io_utils import read_json, read_yaml


class ArtifactState(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    MALFORMED = "malformed"
    STALE_SECRET = "stale_secret"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    state: ArtifactState
    payload: dict[str, Any] | None
    reason: str


ArtifactFormat = Literal["json", "yaml"]
CommandPrefix = Sequence[str]


def _load_payload(path: Path, artifact_format: ArtifactFormat) -> Any:
    if artifact_format == "json":
        return read_json(path)
    return read_yaml(path)


def _normalise_prefixes(
    prefixes: Sequence[CommandPrefix] | CommandPrefix | None,
) -> tuple[list[str] | None, ...]:
    if prefixes is None:
        return (None,)

    candidates = list(prefixes)
    if not candidates:
        return (None,)
    if all(isinstance(item, str) for item in candidates):
        return ([str(item) for item in candidates],)
    return tuple([str(item) for item in candidate] for candidate in candidates)


def load_attested_artifact(
    path: Path,
    project_path: Path,
    prefixes: Sequence[CommandPrefix] | CommandPrefix | None,
    format: ArtifactFormat,
) -> VerifiedArtifact:
    """Authenticate with precedence: verified, stale secret, first refusal."""
    path = Path(path)
    project_path = Path(project_path)
    artifact_format = str(format).lower()
    if artifact_format not in ("json", "yaml"):
        raise ValueError(f"unsupported artifact format: {format!r}")

    try:
        payload = _load_payload(path, artifact_format)
    except FileNotFoundError:
        return VerifiedArtifact(ArtifactState.MISSING, None, "missing_file")
    except (json.JSONDecodeError, yaml.YAMLError, OSError, UnicodeError):
        return VerifiedArtifact(ArtifactState.MALFORMED, None, "unparseable_file")

    if not isinstance(payload, dict):
        return VerifiedArtifact(ArtifactState.MALFORMED, None, "malformed_file")

    saw_stale = False
    first_reason = ""
    for prefix in _normalise_prefixes(prefixes):
        try:
            valid, reason = attestation.verify(
                payload,
                project_path=project_path,
                expected_command_prefix=prefix,
            )
        except (KeyError, TypeError, ValueError):
            valid, reason = False, attestation.REASON_MALFORMED

        if valid:
            return VerifiedArtifact(ArtifactState.VERIFIED, payload, "")
        if reason == attestation.REASON_STALE_SECRET:
            saw_stale = True
        elif not first_reason:
            first_reason = reason

    if saw_stale:
        return VerifiedArtifact(
            ArtifactState.STALE_SECRET,
            None,
            attestation.REASON_STALE_SECRET,
        )
    return VerifiedArtifact(ArtifactState.UNVERIFIED, None, first_reason)
