"""Persistent cost-telemetry sinks.

Mirrors the audit-sink design. Sinks are sync and write one record per
LLM/peer call. Default chain is empty (in-memory tracker only). Users
opt-in to persistence by appending a sink with `add_cost_sink(...)`.

Cloud-portable: works in `cloudless dev` and in deployed runtimes
without modification.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CostRecord:
    """One cost event."""
    timestamp: float
    kind: str
    """One of: 'llm', 'peer'."""

    model: str = ""
    """Model ID for llm; peer name for peer."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    usd: float = 0.0
    """Computed USD cost. 0.0 for peer calls unless the peer reported it."""

    session_id: str | None = None
    agent_name: str | None = None
    team: str | None = None
    project: str | None = None
    feature: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class CostSink:
    """Sink protocol — must be synchronous."""
    def write(self, record: CostRecord) -> None: ...


class JsonlCostSink:
    """Append-only JSONL file sink."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record: CostRecord) -> None:
        with open(self.path, "a") as f:
            f.write(record.to_json() + "\n")


class InMemoryCostSink:
    """Test sink — keeps records in a list."""

    def __init__(self) -> None:
        self.records: list[CostRecord] = []

    def write(self, record: CostRecord) -> None:
        self.records.append(record)


_SINKS: list[CostSink] = []


def get_cost_sinks() -> list[CostSink]:
    return list(_SINKS)


def set_cost_sinks(sinks: list[CostSink]) -> None:
    global _SINKS
    _SINKS = list(sinks)


def add_cost_sink(sink: CostSink) -> None:
    _SINKS.append(sink)


def reset_cost_sinks() -> None:
    """Test helper — clears the chain."""
    global _SINKS
    _SINKS = []


def emit_cost(
    *,
    kind: str,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    usd: float = 0.0,
    session_id: str | None = None,
    agent_name: str | None = None,
    team: str | None = None,
    project: str | None = None,
    feature: str | None = None,
    extra: dict[str, Any] | None = None,
) -> CostRecord:
    """Write a cost record to every configured sink."""
    record = CostRecord(
        timestamp=time.time(),
        kind=kind,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        usd=usd,
        session_id=session_id,
        agent_name=agent_name,
        team=team,
        project=project,
        feature=feature,
        extra=dict(extra or {}),
    )
    for sink in _SINKS:
        try:
            sink.write(record)
        except Exception:
            pass
    return record
