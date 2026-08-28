"""Q19 portable governance: @cloudless.policy decorator + stage hooks.

A *policy* is a callable invoked at well-known stages of agent execution:

    before_llm   — receives `prompt: str`, may return modified prompt or raise PolicyViolation
    after_llm    — receives `prompt: str, response: str`, may return modified response
    before_tool  — receives `tool_name: str, args: dict`, may return modified args
    after_tool   — receives `tool_name: str, args: dict, result: Any`, may return modified result
    before_peer  — receives `peer: str, prompt: str`, may return modified prompt
    after_peer   — receives `peer: str, prompt: str, response: str`, may return modified response

Policies run in registration order. Each stage's pipeline short-circuits if any
policy raises `PolicyViolation` (or `GuardrailBlocked`).

Cloud-portable design rationale:
  - Bedrock Guardrails + Gemini Safety Filters are cloud-native. cloudless
    policies sit ABOVE those (Python-level redaction, custom block lists,
    org-specific rules) and run in the embedded runtime regardless of cloud.
  - For an audit story (Q19), every policy invocation is recorded on
    ctx.audit_log if a policy raises.

Usage:

    @cloudless.policy(stages=["before_llm"])
    def block_ssn(stage, prompt, **kw):
        if SSN_REGEX.search(prompt):
            raise cloudless.PolicyViolation("SSN detected")
        return prompt   # could also transform

    @cloudless.policy(stages=["after_llm"], priority=10)
    def redact_emails(stage, prompt, response, **kw):
        return EMAIL_REGEX.sub("<email>", response)
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cloudless.exceptions import GuardrailBlocked, PolicyViolation

VALID_STAGES = frozenset({
    "before_llm",
    "after_llm",
    "before_tool",
    "after_tool",
    "before_peer",
    "after_peer",
})


@dataclass
class PolicyEntry:
    name: str
    stages: tuple[str, ...]
    priority: int
    fn: Callable[..., Any]


class PolicyRegistry:
    """In-memory registry of policies. One per process; agents share it.

    The deploy planner can additionally serialize/deserialize this registry
    so it travels into the cloud runtime container.
    """

    def __init__(self) -> None:
        self._entries: list[PolicyEntry] = []

    def register(self, entry: PolicyEntry) -> None:
        self._entries.append(entry)
        # Stable sort by priority (higher runs first), then registration order
        self._entries.sort(key=lambda e: -e.priority)

    def clear(self) -> None:
        """Used by tests. Not for production code."""
        self._entries.clear()

    def for_stage(self, stage: str) -> list[PolicyEntry]:
        return [e for e in self._entries if stage in e.stages]

    def run(self, stage: str, /, **payload: Any) -> dict[str, Any]:
        """Run all policies for `stage`, threading payload through transforms.

        Args:
            stage: One of VALID_STAGES.
            **payload: Stage-specific kwargs. The keys you may transform are:
                - before_llm:   "prompt"
                - after_llm:    "response"
                - before_tool:  "args"
                - after_tool:   "result"
                - before_peer:  "prompt"
                - after_peer:   "response"

        Returns:
            The (possibly transformed) payload dict.

        Raises:
            PolicyViolation / GuardrailBlocked if any policy blocks.
        """
        if stage not in VALID_STAGES:
            raise ValueError(f"unknown policy stage {stage!r}")

        # Output keys the policy may overwrite
        transformable = {
            "before_llm": "prompt",
            "after_llm": "response",
            "before_tool": "args",
            "after_tool": "result",
            "before_peer": "prompt",
            "after_peer": "response",
        }[stage]

        from cloudless.runtime.audit import emit_audit
        primary = payload.get(transformable)

        for entry in self.for_stage(stage):
            try:
                ret = entry.fn(stage, **payload)
            except (PolicyViolation, GuardrailBlocked) as e:
                emit_audit(
                    stage=stage,
                    decision="block",
                    policy_name=entry.name,
                    reason=str(e),
                    payload=primary,
                )
                raise
            except Exception as e:
                emit_audit(
                    stage=stage,
                    decision="block",
                    policy_name=entry.name,
                    reason=f"buggy policy raised: {e}",
                    payload=primary,
                )
                raise PolicyViolation(
                    f"policy {entry.name!r} raised at stage {stage!r}: {e}"
                ) from e
            if ret is not None:
                emit_audit(
                    stage=stage,
                    decision="transform",
                    policy_name=entry.name,
                    payload=primary,
                )
                payload[transformable] = ret
                primary = ret
        return payload


# Module-level singleton; users register via the decorator.
_REGISTRY = PolicyRegistry()


def get_registry() -> PolicyRegistry:
    return _REGISTRY


def policy(
    *,
    stages: Iterable[str],
    name: str | None = None,
    priority: int = 0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that registers a callable as a policy.

    Args:
        stages: Iterable of stage names from VALID_STAGES.
        name: Optional human-readable identifier. Defaults to the function name.
        priority: Higher priorities run first. Default 0.
    """
    stage_tuple = tuple(stages)
    bad = set(stage_tuple) - VALID_STAGES
    if bad:
        raise ValueError(
            f"@cloudless.policy: unknown stage(s) {sorted(bad)!r}. "
            f"Valid: {sorted(VALID_STAGES)!r}"
        )
    if not stage_tuple:
        raise ValueError("@cloudless.policy: stages must be non-empty")

    def _decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        resolved_name: str = name or getattr(fn, "__name__", None) or "anonymous"
        entry = PolicyEntry(
            name=resolved_name,
            stages=stage_tuple,
            priority=priority,
            fn=fn,
        )
        _REGISTRY.register(entry)
        # Expose the entry on the function so users can inspect / unregister.
        fn.__cloudless_policy__ = entry  # type: ignore[attr-defined]
        return fn

    return _decorate
