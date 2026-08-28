"""Deploy-time provisioning for Vertex Memory Bank.

Memory Bank lives under an Agent Engine (`reasoningEngine` resource).
This module either reuses an existing engine named `cloudless-{project}`
or creates a fresh one. Returns the full resource name.
"""
from __future__ import annotations

from typing import Any


def _engine_display_name(project: str) -> str:
    return f"cloudless-{project}"


def ensure_agent_engine(
    *,
    project: str,
    location: str = "us-central1",
    client: Any = None,
) -> str:
    """Return the Vertex Agent Engine resource name, creating one if missing.

    Args:
        project: cloudless project name.
        location: GCP region.
        client: Optional vertexai-initialized SDK module (`vertexai.agent_engines`).

    Returns:
        Full resource name, e.g.
        "projects/<id>/locations/us-central1/reasoningEngines/<engine_id>"
    """
    if client is None:
        import vertexai
        from vertexai import agent_engines as ae
        vertexai.init(project=project, location=location)
        client = ae

    name = _engine_display_name(project)

    # List existing engines and reuse if our display name is present
    try:
        for engine in client.list():
            display = getattr(engine, "display_name", "") or ""
            if display == name:
                return engine.resource_name
    except Exception:
        pass

    # Create a minimal engine. Memory Bank attaches via its own API once the
    # engine exists. We use the stock LangchainAgent template — a bare-bones
    # placeholder; the real agent application is deployed separately.
    try:
        engine = client.create(
            display_name=name,
            description=f"cloudless-managed Agent Engine for project {project!r}",
        )
    except Exception as e:
        raise RuntimeError(f"Failed to create Agent Engine {name!r}: {e}") from e

    return engine.resource_name
