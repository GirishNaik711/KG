"""AWS-side adapters for cloudless service catalog + deploy.

Each module wires cloudless types to a concrete AWS service:
  - `agentcore` — AgentCore Runtime deploy + invoke + status
  - (M2+) `bedrock_llm`, `agentcore_memory`, `secrets_manager`, etc.
"""
from __future__ import annotations

from cloudless.adapters.aws.agentcore import (
    AgentCoreDeployer,
    DeploymentResult,
    deploy,
)

__all__ = ["AgentCoreDeployer", "DeploymentResult", "deploy"]
