"""Framework adapter bases.

Each framework gets a `cloudless.<Framework>Agent` base class that:
  - Inherits from `cloudless.Agent`
  - Implements the abstract `query()` by driving the framework
  - Translates framework-native events to `cloudless.chunks.Chunk`

The five supported frameworks:
  - LangGraph (cloudless.LangGraphAgent)
  - Strands Agents (cloudless.StrandsAgent)
  - Google ADK (cloudless.ADKAgent)
  - Anthropic Claude Agent SDK (cloudless.ClaudeAgentSDKAgent)
  - Microsoft Agent Framework (cloudless.MAFAgent)

Each adapter import is wrapped in try/except so missing optional
dependencies don't break the package — the corresponding attribute is
None when the framework's extra isn't installed.
"""
from __future__ import annotations

try:
    from cloudless.adapters.frameworks.langgraph import LangGraphAgent
except ImportError:  # langgraph extra not installed
    LangGraphAgent = None  # type: ignore[assignment,misc]

try:
    from cloudless.adapters.frameworks.strands import StrandsAgent
except ImportError:  # strands extra not installed
    StrandsAgent = None  # type: ignore[assignment,misc]

# ADKAgent has no eager import-time dependency on google-adk — the SDK
# is loaded inside `query()` / `_runner()`. The module imports cleanly
# whether or not the extra is installed.
from cloudless.adapters.frameworks.adk import ADKAgent

# Same lazy strategy for Claude SDK + MAF.
from cloudless.adapters.frameworks.claude_sdk import (
    ClaudeAgentSDKAgent,
    ClaudeSDKAgent,
)
from cloudless.adapters.frameworks.maf import MAFAgent

__all__ = [
    "ADKAgent",
    "ClaudeAgentSDKAgent",
    "ClaudeSDKAgent",
    "LangGraphAgent",
    "MAFAgent",
    "StrandsAgent",
]
