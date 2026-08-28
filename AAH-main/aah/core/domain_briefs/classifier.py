#!/usr/bin/env python3
"""LLM-based industry-domain classifier.

Uses the Anthropic SDK (Claude Opus) to pick the best industry-domain path
for a problem statement + intake answers, selecting from the top-K
candidates surfaced by the offline detector. Falls back to the detector's
top result (confidence=low) if:
  - ANTHROPIC_API_KEY is not set
  - The anthropic package is not installed
  - The API call fails for any reason

Usage:
  aah run core.domain_briefs.classifier classify \
    --project-path /path/to/project

Output JSON:
  {
    "chosen_id": "financial-services/.../commercial-client-onboarding" | null,
    "confidence": "high" | "medium" | "low",
    "source": "classifier" | "fallback_detector",
    "rationale": "...",
    "candidates_considered": [...]
  }
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from aah.core.domain_briefs.detector import (
    _read_intake_answers,
    rank_candidates,
)
from aah.core.domain_briefs.loader import load_node


MODEL_ID = "claude-opus-4-7"


def _build_candidate_summaries(candidates: list[dict]) -> list[dict]:
    """Return compact {id, name, description, strong_keywords} for each candidate."""
    summaries: list[dict] = []
    for cand in candidates:
        node = load_node(cand["id"])
        if node is None:
            continue
        kw = node.get("keywords") or {}
        summaries.append(
            {
                "id": cand["id"],
                "name": node.get("name", cand["id"]),
                "description": (node.get("description") or "").strip()[:400],
                "strong_keywords": kw.get("strong") or [],
            }
        )
    return summaries


def _opus_classify(
    problem_statement: str,
    intake_answers: list[str],
    candidates: list[dict],
) -> Optional[dict]:
    """Call Claude Opus via the Anthropic SDK. Returns None on any failure."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    summaries = _build_candidate_summaries(candidates)
    candidate_block = "\n\n".join(
        f"- id: {s['id']}\n  name: {s['name']}\n  description: {s['description']}"
        f"\n  strong_keywords: {', '.join(s['strong_keywords'])}"
        for s in summaries
    )

    user_prompt = (
        "You are classifying a software project into an industry-domain taxonomy.\n\n"
        f"Problem statement:\n{problem_statement}\n\n"
        "Intake context:\n"
        + "\n".join(f"- {a}" for a in intake_answers[:20] if a)
        + "\n\n"
        "Candidate industry-domain paths:\n"
        f"{candidate_block}\n\n"
        "Return JSON with fields:\n"
        '  chosen_id: the best-matching id OR null if none clearly fit\n'
        '  confidence: "high" | "medium" | "low"\n'
        '  rationale: one-sentence reason for the choice\n\n'
        "Respond with JSON only, no prose."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=MODEL_ID,
            max_tokens=400,
            temperature=0.0,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:
        return None

    # Extract text content
    text_parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    raw = "".join(text_parts).strip()

    # Strip optional ```json fences
    if raw.startswith("```"):
        raw = raw.strip("`")
        # drop possible "json\n" prefix
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None

    chosen_id = result.get("chosen_id")
    valid_ids = {c["id"] for c in candidates}
    if chosen_id is not None and chosen_id not in valid_ids:
        return None  # model hallucinated an id not in our candidate set

    return {
        "chosen_id": chosen_id,
        "confidence": result.get("confidence", "medium"),
        "rationale": result.get("rationale", ""),
    }


def classify(
    problem_statement: str,
    intake_answers: list[str],
    top_k: int = 5,
) -> dict:
    """Classify a problem statement. Always returns a structured result."""
    candidates = rank_candidates(problem_statement, intake_answers, top_k=top_k)

    if not candidates:
        return {
            "chosen_id": None,
            "confidence": "low",
            "source": "fallback_detector",
            "rationale": "No candidate industry domains matched the problem statement.",
            "candidates_considered": [],
        }

    llm_result = _opus_classify(problem_statement, intake_answers, candidates)

    if llm_result is not None:
        return {
            "chosen_id": llm_result["chosen_id"],
            "confidence": llm_result["confidence"],
            "source": "classifier",
            "rationale": llm_result["rationale"],
            "candidates_considered": candidates,
        }

    # Fallback: detector top result
    top = candidates[0]
    return {
        "chosen_id": top["id"],
        "confidence": "low",
        "source": "fallback_detector",
        "rationale": f"LLM classifier unavailable; returning detector top match (score={top['score']}).",
        "candidates_considered": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    classify_parser = sub.add_parser("classify", help="Classify a problem statement")
    classify_parser.add_argument("--project-path", type=Path, help="Reads .aah/intake.json")
    classify_parser.add_argument("--problem-statement")
    classify_parser.add_argument("--top-k", type=int, default=5)
    classify_parser.add_argument(
        "--write",
        action="store_true",
        help="Write result to intake.json.industry_domain (requires --project-path)",
    )

    args = parser.parse_args()

    if args.command == "classify":
        if args.problem_statement is not None:
            problem = args.problem_statement
            answers: list[str] = []
        elif args.project_path:
            problem, answers = _read_intake_answers(args.project_path)
        else:
            parser.error("provide --problem-statement or --project-path")

        result = classify(problem, answers, top_k=args.top_k)

        if args.write and args.project_path:
            _write_to_intake(args.project_path, result)

        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")


def _write_to_intake(project_path: Path, result: dict) -> None:
    """Persist classifier result to intake.json.industry_domain."""
    from datetime import datetime, timezone

    intake_path = project_path / ".aah" / "intake.json"
    if not intake_path.is_file():
        return

    intake = json.loads(intake_path.read_text(encoding='utf-8'))
    intake["industry_domain"] = {
        "path": result.get("chosen_id"),
        "confidence": result.get("confidence"),
        "source": result.get("source"),
        "rationale": result.get("rationale"),
        "classified_at": datetime.now(timezone.utc).isoformat(),
    }
    intake_path.write_text(json.dumps(intake, indent=2) + "\n", encoding='utf-8')


if __name__ == "__main__":
    main()
