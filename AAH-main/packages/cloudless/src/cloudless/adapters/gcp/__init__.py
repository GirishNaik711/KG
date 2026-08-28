"""GCP-side adapters for cloudless deploy + service catalog.

M1.5 ships `agent_runtime` (deploy to Gemini Enterprise Agent Runtime,
formerly Vertex AI Agent Engine). Service-catalog adapters (Memory Bank,
Secret Manager, Vertex AI LLM) defer to M2.
"""
from __future__ import annotations

from cloudless.adapters.gcp.agent_runtime import (
    AgentRuntimeDeployer,
    DeploymentResult,
    deploy,
)

__all__ = ["AgentRuntimeDeployer", "DeploymentResult", "deploy"]
