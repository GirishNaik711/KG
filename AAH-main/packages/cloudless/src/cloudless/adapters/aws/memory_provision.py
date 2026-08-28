"""Deploy-time provisioning for AgentCore Memory.

When cloudless.yaml declares `service_catalog.memory`, the deploy adapter
calls `ensure_memory_resource(...)` to either find an existing resource
with the conventional name or create one fresh.

Conventional name: `cloudless-{project}-memory`.
"""
from __future__ import annotations

import time
from typing import Any


def _memory_name(project: str) -> str:
    """AgentCore name regex: ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no hyphens, ≤48 chars."""
    slug = project.replace("-", "_")
    base = f"cloudless_{slug}_memory"
    return base[:48]


def ensure_memory_resource(
    *,
    project: str,
    region: str = "us-east-1",
    strategy: str = "semantic",
    retention_days: int = 90,
    client: Any = None,
) -> str:
    """Return the AgentCore Memory ID, creating one if necessary.

    Args:
        project: cloudless project name from cloudless.yaml.
        region: AWS region.
        strategy: One of {"semantic", "user_preference", "summarization"}.
        retention_days: Event retention window.
        client: Optional pre-built `bedrock-agentcore-control` client.

    Returns:
        The Memory resource ID (string).
    """
    if client is None:
        import boto3
        client = boto3.client("bedrock-agentcore-control", region_name=region)

    name = _memory_name(project)

    # Search for existing memory by name
    try:
        for mem in client.list_memories().get("memories", []):
            if mem.get("name") == name:
                return mem.get("id") or mem.get("memoryId")
    except Exception:
        pass

    # AgentCore strategy regex disallows hyphens; default to a snake-cased name.
    strategy_name = f"default_{strategy}"

    # AgentCore expects camelCase strategy keys
    strategy_key_map = {
        "semantic":        "semanticMemoryStrategy",
        "summarization":   "summaryMemoryStrategy",
        "summary":         "summaryMemoryStrategy",
        "user_preference": "userPreferenceMemoryStrategy",
        "userpreference":  "userPreferenceMemoryStrategy",
        "custom":          "customMemoryStrategy",
        "episodic":        "episodicMemoryStrategy",
    }
    strategy_key = strategy_key_map.get(strategy.lower())
    if strategy_key is None:
        raise ValueError(
            f"unknown strategy {strategy!r}; valid: {sorted(set(strategy_key_map))}"
        )

    if strategy.lower() == "custom":
        strategy_config = {
            "customMemoryStrategy": {
                "name": strategy_name,
                "configuration": {
                    "semanticOverride": {
                        "extraction": {"appendToPrompt": "Extract any facts."},
                    },
                },
            },
        }
    else:
        strategy_config = {strategy_key: {"name": strategy_name}}

    try:
        resp = client.create_memory(
            name=name,
            description=f"cloudless-managed memory for project {project!r}",
            # eventExpiryDuration is in DAYS, max 365
            eventExpiryDuration=min(retention_days, 365),
            memoryStrategies=[strategy_config],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to create AgentCore Memory {name!r}: {e}") from e

    memory_id = (
        resp.get("memoryId")
        or resp.get("id")
        or resp.get("memory", {}).get("id")
        or resp.get("memory", {}).get("memoryId")
    )
    if not memory_id:
        raise RuntimeError(f"create_memory returned no id: {resp!r}")

    # AgentCore Memory creation is async — wait briefly for ACTIVE
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            got = client.get_memory(memoryId=memory_id)
            status = got.get("memory", {}).get("status") or got.get("status")
            if status == "ACTIVE":
                break
        except Exception:
            pass
        time.sleep(2)

    return memory_id
