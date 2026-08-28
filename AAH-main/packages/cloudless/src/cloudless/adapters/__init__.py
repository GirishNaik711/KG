"""cloudless adapter layer.

Adapters bridge:
  - `frameworks/*`: agent frameworks (LangGraph, Strands, ADK, MAF) → cloudless types
  - `aws/*`: cloudless service-catalog calls → AWS services (Bedrock, AgentCore, ...)
  - `gcp/*`: cloudless service-catalog calls → GCP services (Gemini Enterprise, ...)

Per Q35 (extensibility model): each adapter category is a Protocol contract.
Out-of-tree plugins register via Python entry points.
"""
