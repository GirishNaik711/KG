"""Canonical append-only gate-decision audit events and legacy readers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from aah.core.common.time_utils import rfc3339_now


GATE_DECISION_SCHEMA_VERSION = 1
GATE_DECISIONS_REL_PATH = Path("audit") / "gate-decisions.jsonl"


def _aah_path(project_or_aah_path: Path) -> Path:
    path = Path(project_or_aah_path)
    return path if path.name == ".aah" else path / ".aah"


def gate_decision_event(
    *,
    gate: str,
    decision: str,
    wave: int | None = None,
    feature_id: str | None = None,
    reason: str | None = None,
    reviewer: str | None = None,
    timestamp: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if not isinstance(gate, str) or not gate:
        raise ValueError("gate must be a non-empty string")
    if not isinstance(decision, str) or not decision:
        raise ValueError("decision must be a non-empty string")
    event: dict[str, Any] = {
        "schema_version": GATE_DECISION_SCHEMA_VERSION,
        "ts": timestamp or rfc3339_now(),
        "gate": gate,
        "decision": decision,
    }
    if wave is not None:
        if not isinstance(wave, int) or isinstance(wave, bool):
            raise ValueError("wave must be an integer")
        event["wave"] = wave
    if feature_id is not None:
        event["feature_id"] = feature_id
    if reason is not None:
        event["reason"] = reason
    if reviewer is not None:
        event["reviewer"] = reviewer
    # Additive producer-specific context remains supported, but cannot replace
    # the canonical identity/version fields.
    for key, value in extra.items():
        if key not in event and key not in {"schema_version", "timestamp"}:
            event[key] = value
    return event


def normalize_gate_decision(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    gate = row.get("gate")
    decision = row.get("decision")
    timestamp = row.get("ts", row.get("timestamp"))
    if not isinstance(gate, str) or not gate:
        return None
    if not isinstance(decision, str) or not decision:
        return None
    if timestamp is not None and (not isinstance(timestamp, str) or not timestamp):
        return None

    normalized = dict(row)
    normalized.pop("timestamp", None)
    if timestamp is not None:
        normalized["ts"] = timestamp
    version = normalized.get("schema_version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        return None
    normalized["schema_version"] = version
    return normalized


def append_gate_decision(
    project_or_aah_path: Path,
    *,
    gate: str,
    decision: str,
    wave: int | None = None,
    feature_id: str | None = None,
    reason: str | None = None,
    reviewer: str | None = None,
    timestamp: str | None = None,
    **extra: Any,
) -> bool:
    event = gate_decision_event(
        gate=gate,
        decision=decision,
        wave=wave,
        feature_id=feature_id,
        reason=reason,
        reviewer=reviewer,
        timestamp=timestamp,
        **extra,
    )
    path = _aah_path(project_or_aah_path) / GATE_DECISIONS_REL_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


write_gate_decision = append_gate_decision


def read_gate_decisions(project_or_aah_path: Path) -> list[dict[str, Any]]:
    path = _aah_path(project_or_aah_path) / GATE_DECISIONS_REL_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        normalized = normalize_gate_decision(row)
        if normalized is not None:
            events.append(normalized)
    return events
