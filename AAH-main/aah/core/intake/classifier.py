    #!/usr/bin/env python3
"""
Complexity classification and phase plan computation.

Analyzes intake answers to determine complexity tier and which RAPIDS
phases to run at what depth.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.intake.intake import load_intake, find_intake, get_all_answers, resolve_intake_path


# ─── Phase plan templates by tier ────────────────────────────────────

PHASE_PLANS = {
    "trivial": {
        "research": {"required": False, "depth": "skip", "reason": "Trivial scope — no research needed"},
        "analysis": {"required": False, "depth": "skip", "reason": "Trivial scope — no analysis needed"},
        "plan": {"required": True, "depth": "lightweight", "reason": "Minimal planning — feature list only"},
        "implement": {"required": True, "depth": "direct", "reason": "Direct implementation"},
        "deploy": {"required": False, "depth": "skip", "reason": "Deployment optional for trivial changes"},
        "sustain": {"required": False, "depth": "skip", "reason": "No sustain needed for trivial changes"},
    },
    "moderate": {
        "research": {"required": True, "depth": "lightweight", "reason": "Brief technology survey"},
        "analysis": {"required": True, "depth": "standard", "reason": "Standard architecture design"},
        "plan": {"required": True, "depth": "standard", "reason": "Standard feature decomposition"},
        "implement": {"required": True, "depth": "standard", "reason": "Standard implementation"},
        "deploy": {"required": True, "depth": "standard", "reason": "Standard deployment"},
        "sustain": {"required": True, "depth": "lightweight", "reason": "Basic observability"},
    },
    "significant": {
        "research": {"required": True, "depth": "standard", "reason": "Technology and risk research"},
        "analysis": {"required": True, "depth": "standard", "reason": "Full architecture design"},
        "plan": {"required": True, "depth": "full", "reason": "Detailed feature decomposition with DAG"},
        "implement": {"required": True, "depth": "full", "reason": "Full implementation with QA evaluation"},
        "deploy": {"required": True, "depth": "standard", "reason": "Container and CI/CD setup"},
        "sustain": {"required": True, "depth": "standard", "reason": "Observability and runbooks"},
    },
    "complex": {
        "research": {"required": True, "depth": "full", "reason": "Deep research with agent teams"},
        "analysis": {"required": True, "depth": "full", "reason": "Full architecture with ADRs"},
        "plan": {"required": True, "depth": "full", "reason": "Full planning with sprint contracts"},
        "implement": {"required": True, "depth": "full", "reason": "Full implementation with agent teams"},
        "deploy": {"required": True, "depth": "full", "reason": "IaC, containers, multi-environment"},
        "sustain": {"required": True, "depth": "full", "reason": "Full observability, runbooks, drift detection"},
    },
}


# ─── Project type to scope mapping ──────────────────────────────────

PROJECT_TYPE_SCOPE = {
    "New system from scratch": "new_system",
    "new_system": "new_system",
    "Enhancement to existing system": "enhancement",
    "enhancement": "enhancement",
    "Bug fix / maintenance": "bug_fix",
    "bug_fix": "bug_fix",
    "Migration / replatform": "migration",
    "migration": "migration",
}


def classify_complexity(intake: dict) -> dict:
    """
    Classify project complexity from intake answers.

    Returns a complexity assessment dict with:
    - scope, ambiguity, domain_complexity, integration_surface, tier, reasoning
    """
    answers = get_all_answers(intake)
    project_type = intake.get("project_type") or "moderate"

    # Determine scope
    scope = PROJECT_TYPE_SCOPE.get(project_type, "new_system")

    # Score dimensions from answers
    integration_score = _score_integration(answers)
    domain_score = _score_domain_complexity(answers)
    ambiguity_score = _score_ambiguity(intake)

    # Map scores to labels
    integration_surface = ["isolated", "moderate", "moderate", "extensive"][min(integration_score, 3)]
    domain_complexity = ["low", "medium", "high"][min(domain_score, 2)]
    ambiguity = "well_defined" if ambiguity_score <= 1 else "vague"

    # Compute tier
    tier = _compute_tier(scope, integration_score, domain_score, ambiguity_score)

    reasoning_parts = []
    if scope == "bug_fix":
        reasoning_parts.append("Bug fix scope suggests lower complexity")
    elif scope == "new_system":
        reasoning_parts.append("New system scope suggests higher complexity")

    if integration_score >= 2:
        reasoning_parts.append(f"significant integration surface ({integration_surface})")
    if domain_score >= 2:
        reasoning_parts.append(f"high domain complexity ({domain_complexity})")
    if ambiguity_score >= 2:
        reasoning_parts.append("requirements are still ambiguous")

    reasoning = ". ".join(reasoning_parts) if reasoning_parts else "Standard project complexity"

    return {
        "scope": scope,
        "ambiguity": ambiguity,
        "domain_complexity": domain_complexity,
        "integration_surface": integration_surface,
        "tier": tier,
        "reasoning": reasoning,
    }


def _score_integration(answers: dict) -> int:
    """Score integration complexity from answers (0-3)."""
    for q, a in answers.items():
        # Answers may be a list (multi-select) or a string — normalise to string
        a_str = " ".join(a) if isinstance(a, list) else str(a)
        if "external systems" in q.lower() or "integrate" in q.lower():
            if "none" in a_str.lower() or "standalone" in a_str.lower():
                return 0
            elif "1-2" in a_str:
                return 1
            elif "3-5" in a_str:
                return 2
            elif "6+" in a_str:
                return 3
    return 1  # default moderate


def _score_domain_complexity(answers: dict) -> int:
    """Score domain complexity from answers (0-2)."""
    score = 0
    for q, a in answers.items():
        # Answers may be a list (multi-select) or a string — normalise to string
        a_str = " ".join(a) if isinstance(a, list) else str(a)
        if "domain" in q.lower() or "technical" in q.lower():
            if "data" in a_str.lower() or "ml" in a_str.lower() or "ai" in a_str.lower():
                score = max(score, 2)
            elif "infra" in a_str.lower():
                score = max(score, 1)
        if "data landscape" in q.lower():
            if "real-time" in a_str.lower() or "multi-model" in a_str.lower():
                score = max(score, 2)
            elif "complex" in a_str.lower() or "analytics" in a_str.lower():
                score = max(score, 1)
    return score


def _score_ambiguity(intake: dict) -> int:
    """Score ambiguity based on how much has been answered (0-2)."""
    rounds = len(intake.get("rounds", []))
    has_problem = intake.get("problem_statement") is not None
    has_type = intake.get("project_type") is not None

    if rounds >= 2 and has_problem and has_type:
        return 0  # well-defined
    elif rounds >= 1 and has_problem:
        return 1  # somewhat defined
    else:
        return 2  # vague


def _compute_tier(scope: str, integration: int, domain: int, ambiguity: int) -> str:
    """Compute overall complexity tier from dimension scores."""
    # Bug fixes are trivial unless integration or domain is high
    if scope == "bug_fix" and integration <= 1 and domain <= 1:
        return "trivial"

    # Simple enhancements with low complexity
    if scope == "enhancement" and integration <= 1 and domain <= 1 and ambiguity <= 1:
        return "moderate"

    # Score-based for everything else
    total = integration + domain + ambiguity
    if scope == "new_system":
        total += 1  # bias toward higher complexity for new systems

    if total <= 2:
        return "moderate"
    elif total <= 4:
        return "significant"
    else:
        return "complex"


def compute_phase_plan(tier: str, project_type: str | None = None) -> dict:
    """Get the phase plan for a given complexity tier."""
    plan = PHASE_PLANS.get(tier, PHASE_PLANS["moderate"])

    # Bug fixes always skip research and analysis
    if project_type in ("bug_fix", "Bug fix / maintenance"):
        plan = {**plan}
        plan["research"] = {"required": False, "depth": "skip", "reason": "Bug fix — skip research"}
        plan["analysis"] = {"required": False, "depth": "skip", "reason": "Bug fix — skip analysis"}

    return plan


def is_trivial(intake: dict) -> bool:
    """Quick check: is this project trivial enough to skip phases?"""
    project_type = intake.get("project_type", "")
    if project_type in ("bug_fix", "Bug fix / maintenance"):
        # Check if integration is low
        answers = get_all_answers(intake)
        integration = _score_integration(answers)
        return integration <= 1

    assessment = intake.get("complexity_assessment")
    if assessment:
        return assessment.get("tier") == "trivial"

    return False


def main() -> None:
    path_parent = argparse.ArgumentParser(add_help=False)
    path_parent.add_argument("--path", "--intake-path", type=Path, default=None,
                             help="Path to intake.json or its parent directory")

    parser = argparse.ArgumentParser(description="RAPIDS complexity classifier")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("classify", parents=[path_parent], help="Classify complexity from intake")

    plan_p = sub.add_parser("phase-plan", help="Get phase plan for a tier")
    plan_p.add_argument("--tier", type=str, required=True, choices=["trivial", "moderate", "significant", "complex"])
    plan_p.add_argument("--type", dest="project_type", type=str, default=None)

    sub.add_parser("is-trivial", parents=[path_parent], help="Quick trivial check")

    args = parser.parse_args()

    if args.command == "classify":
        intake = load_intake(resolve_intake_path(args.path))
        assessment = classify_complexity(intake)
        json.dump(assessment, sys.stdout, indent=2)
        print()

    elif args.command == "phase-plan":
        plan = compute_phase_plan(args.tier, args.project_type)
        json.dump(plan, sys.stdout, indent=2)
        print()

    elif args.command == "is-trivial":
        intake = load_intake(resolve_intake_path(args.path))
        trivial = is_trivial(intake)
        json.dump({"trivial": trivial}, sys.stdout, indent=2)
        print()
        sys.exit(0 if trivial else 1)

    sys.exit(0)


if __name__ == "__main__":
    main()
