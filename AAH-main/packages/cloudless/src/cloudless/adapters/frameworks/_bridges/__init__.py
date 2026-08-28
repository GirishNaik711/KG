"""Cross-cloud framework bridges.

Each framework adapter's *native* SDK ships with a model/client class for
one cloud only:
  - Strands ships BedrockModel (AWS only)
  - Google ADK ships Gemini (GCP only)
  - Microsoft Agent Framework ships BedrockChatClient (AWS only)

For cloudless's 5-framework × 2-cloud value-prop to hold, each framework
needs to call the *other* cloud's LLM too. These bridges fill those
gaps using each framework's *native pluggable interface* (no LiteLLM,
no third-party adapters) routed through the cloud's official SDK
(boto3 for Bedrock, google-genai for Vertex).

The bridges:
  - `BedrockADKLlm` — drive Vertex-AI-flavoured `ADK` against AWS Bedrock
  - `VertexStrandsModel` — drive Bedrock-flavoured `Strands` against GCP Vertex Gemini
  - `VertexMAFChatClient` — drive Bedrock-flavoured `MAF` against GCP Vertex Gemini

Each is small (~150 lines) and lazy-imports the framework + cloud SDK
so the cloudless package keeps loading cleanly even without the extras
installed.
"""
from __future__ import annotations

try:
    from cloudless.adapters.frameworks._bridges.adk_bedrock import BedrockADKLlm
except ImportError:  # google-adk extra not installed
    BedrockADKLlm = None  # type: ignore[assignment,misc]

try:
    from cloudless.adapters.frameworks._bridges.strands_vertex import VertexStrandsModel
except ImportError:  # strands extra not installed
    VertexStrandsModel = None  # type: ignore[assignment,misc]

try:
    from cloudless.adapters.frameworks._bridges.maf_vertex import VertexMAFChatClient
except ImportError:  # maf extra not installed
    VertexMAFChatClient = None  # type: ignore[assignment,misc]

__all__ = ["BedrockADKLlm", "VertexMAFChatClient", "VertexStrandsModel"]
