#!/usr/bin/env python3
"""Classify source content into best-fit archetype and architecture layer."""

import argparse
import json
import sys
from pathlib import Path


# Keywords per archetype for offline classification
ARCHETYPE_KEYWORDS = {
    "ai-applications": [
        "chatbot", "assistant", "conversational", "prompt", "llm", "rag",
        "retrieval", "agent", "intent", "natural language", "generation",
        "embedding", "vector", "fine-tuning", "model selection",
    ],
    "ai-platforms": [
        "platform", "multi-tenant", "model serving", "inference",
        "model registry", "feature store", "experiment tracking",
        "ml platform", "ai platform", "mlflow", "kubeflow",
    ],
    "ai-infra-platforms": [
        "orchestration", "multi-agent", "agent framework", "tool use",
        "mcp", "guardrails", "safety", "evaluation", "observability",
        "agent coordination", "worker", "router",
    ],
    "data-pipelines": [
        "etl", "elt", "pipeline", "ingestion", "transformation",
        "data flow", "streaming", "batch", "kafka", "spark",
        "airflow", "dbt", "data quality",
    ],
    "data-modernization": [
        "migration", "data lake", "data warehouse", "modernize",
        "legacy", "schema evolution", "data mesh", "lakehouse",
    ],
    "llm-ops": [
        "llmops", "prompt management", "model deployment", "a/b testing",
        "model monitoring", "prompt versioning", "guardrail ops",
        "llm observability", "token tracking",
    ],
    "ml-ops": [
        "mlops", "model training", "model deployment", "feature engineering",
        "experiment tracking", "model registry", "ml pipeline",
        "model monitoring", "drift detection",
    ],
    "cloud-modernization": [
        "cloud migration", "kubernetes", "containerization", "microservices",
        "serverless", "cloud native", "infrastructure", "terraform",
        "devops", "ci/cd",
    ],
    "security-updates": [
        "security", "vulnerability", "cve", "patch", "compliance",
        "audit", "penetration testing", "zero trust", "encryption",
    ],
}

# Keywords per architecture layer
LAYER_KEYWORDS = {
    1: ["user interface", "interaction", "intent", "ux", "conversation", "dialogue",
        "user journey", "slot", "input", "chat", "multi-turn"],
    2: ["framework", "tooling", "langchain", "langgraph", "autogen", "crewai",
        "sdk", "library", "runtime", "execution engine"],
    3: ["model selection", "llm", "gpt", "claude", "gemini", "provider",
        "fallback", "routing", "cost", "latency", "token"],
    4: ["retrieval", "rag", "vector", "embedding", "grounding", "search",
        "knowledge base", "chunk", "index", "semantic search"],
    5: ["agent", "coordination", "orchestration", "multi-agent", "delegation",
        "supervisor", "worker", "handoff", "routing"],
    6: ["memory", "context", "state", "session", "history", "window",
        "summarization", "long-term", "short-term"],
    7: ["safety", "evaluation", "guardrail", "toxicity", "hallucination",
        "bias", "testing", "benchmark", "red team"],
    8: ["tool", "mcp", "function calling", "api", "integration", "plugin",
        "action", "capability", "external service"],
    9: ["compute", "infrastructure", "gpu", "scaling", "cloud", "deployment",
        "cost optimization", "serverless", "container"],
}


def classify_archetype(content: str) -> dict:
    """Classify content into the best-fit archetype using keyword matching.

    Returns dict with archetype, confidence, and reasoning.
    """
    content_lower = content.lower()
    scores: dict[str, int] = {}

    for archetype, keywords in ARCHETYPE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[archetype] = score

    if not scores:
        return {
            "archetype": None,
            "archetype_confidence": 0.0,
            "is_shared": True,
            "could_be_new_archetype": True,
            "reasoning": "No archetype keywords matched. Content may require a new archetype or is cross-cutting (SHARED).",
        }

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_archetype, best_score = sorted_scores[0]
    max_possible = len(ARCHETYPE_KEYWORDS[best_archetype])
    confidence = min(best_score / max(max_possible * 0.4, 1), 1.0)

    # Determine if it could be shared (top 2 are close)
    is_shared = False
    if len(sorted_scores) >= 2:
        second_score = sorted_scores[1][1]
        if second_score >= best_score * 0.7:
            is_shared = True

    return {
        "archetype": best_archetype,
        "archetype_confidence": round(confidence, 2),
        "is_shared": is_shared,
        "could_be_new_archetype": False,
        "all_scores": {k: v for k, v in sorted_scores[:5]},
        "reasoning": f"Best match: {best_archetype} (score {best_score}/{max_possible}). "
                     f"{'Content spans multiple archetypes.' if is_shared else 'Strong single-archetype match.'}",
    }


def classify_layer(content: str) -> dict:
    """Classify content into the best-fit architecture layer using keyword matching.

    Returns dict with layer number, name, confidence, and reasoning.
    """
    content_lower = content.lower()
    scores: dict[int, int] = {}

    for layer_num, keywords in LAYER_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[layer_num] = score

    if not scores:
        return {
            "layer": None,
            "layer_number": None,
            "layer_confidence": 0.0,
            "reasoning": "No layer keywords matched. Manual layer assignment needed.",
        }

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_layer, best_score = sorted_scores[0]
    max_possible = len(LAYER_KEYWORDS[best_layer])
    confidence = min(best_score / max(max_possible * 0.4, 1), 1.0)

    from aah.core.ddr_ops.loader import LAYERS
    layer_info = LAYERS.get(best_layer, {})

    return {
        "layer": layer_info.get("name", f"Layer {best_layer}"),
        "layer_number": best_layer,
        "layer_slug": layer_info.get("slug", f"layer-{best_layer}"),
        "layer_confidence": round(confidence, 2),
        "all_scores": {str(k): v for k, v in sorted_scores[:5]},
        "reasoning": f"Best match: L{best_layer} - {layer_info.get('name', '')} (score {best_score}/{max_possible}).",
    }


def classify_content(content: str) -> dict:
    """Full classification: archetype + layer."""
    archetype_result = classify_archetype(content)
    layer_result = classify_layer(content)

    return {
        **archetype_result,
        **layer_result,
    }


def batch_plan(sources: list[dict]) -> dict:
    """Plan execution strategy for multiple sources.

    Each source dict should have: {name, archetype, layer_number}
    Returns grouping and execution order.
    """
    groups: dict[str, list] = {}

    for source in sources:
        key = f"{source.get('archetype', 'unknown')}:L{source.get('layer_number', '?')}"
        if key not in groups:
            groups[key] = []
        groups[key].append(source)

    # Determine parallelization
    parallel_groups = []
    sequential_within = []

    for key, items in groups.items():
        if len(items) > 1:
            # Same archetype + layer = sequential
            sequential_within.append({"key": key, "items": items, "mode": "sequential"})
        else:
            parallel_groups.append({"key": key, "items": items, "mode": "parallel"})

    return {
        "total_sources": len(sources),
        "parallel_groups": parallel_groups,
        "sequential_groups": sequential_within,
        "can_parallelize": len(parallel_groups) > 1 or (len(parallel_groups) >= 1 and len(sequential_within) >= 1),
        "execution_plan": [
            *[{"group": g["key"], "mode": "parallel", "count": len(g["items"])} for g in parallel_groups],
            *[{"group": g["key"], "mode": "sequential", "count": len(g["items"])} for g in sequential_within],
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR classifier — classify content into archetype and layer")
    sub = parser.add_subparsers(dest="command", required=True)

    # classify
    classify_p = sub.add_parser("classify", help="Classify content into archetype and layer")
    classify_p.add_argument("--content-file", type=Path, help="Path to content text file")
    classify_p.add_argument("--content", type=str, help="Inline content text")

    # batch-plan
    batch_p = sub.add_parser("batch-plan", help="Plan execution for multiple sources")
    batch_p.add_argument("--sources", type=str, required=True,
                         help="JSON array of source objects [{name, archetype, layer_number}]")

    args = parser.parse_args()

    if args.command == "classify":
        if args.content_file:
            if not args.content_file.exists():
                print(f"Error: file not found: {args.content_file}", file=sys.stderr)
                sys.exit(1)
            content = args.content_file.read_text(encoding="utf-8")
        elif args.content:
            content = args.content
        else:
            print("Error: provide --content-file or --content", file=sys.stderr)
            sys.exit(1)

        result = classify_content(content)
        json.dump(result, sys.stdout, indent=2)
        print()

    elif args.command == "batch-plan":
        try:
            sources = json.loads(args.sources)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON for --sources: {e}", file=sys.stderr)
            sys.exit(1)

        result = batch_plan(sources)
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
