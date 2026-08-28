"""Eval metrics (Q8): deterministic + LLM-as-judge."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MetricScore:
    name: str
    score: float
    """0.0 (fail) → 1.0 (pass) for binary metrics; or judge-rated [0,1]."""
    passed: bool
    detail: str = ""


class Metric(Protocol):
    name: str
    async def evaluate(self, *, case: Any, response: str) -> MetricScore: ...


# --------------------------------------------------------------------- #
# Deterministic
# --------------------------------------------------------------------- #


class ContainsSubstring:
    name = "contains_substring"

    def __init__(self, *, case_insensitive: bool = True) -> None:
        self.case_insensitive = case_insensitive

    async def evaluate(self, *, case, response: str) -> MetricScore:
        expected = getattr(case, "expected_contains", None)
        if not expected:
            return MetricScore(name=self.name, score=1.0, passed=True,
                                detail="(no expected_contains in case)")
        target = response.lower() if self.case_insensitive else response
        needle = expected.lower() if self.case_insensitive else expected
        passed = needle in target
        return MetricScore(
            name=self.name, score=1.0 if passed else 0.0, passed=passed,
            detail=f"expected={expected!r} in response" + ("" if passed else " — NOT FOUND"),
        )


class RegexMatch:
    name = "regex_match"

    def __init__(self, *, pattern: str | None = None) -> None:
        self.pattern_override = pattern

    async def evaluate(self, *, case, response: str) -> MetricScore:
        pat = self.pattern_override or getattr(case, "expected_regex", None)
        if not pat:
            return MetricScore(name=self.name, score=1.0, passed=True,
                                detail="(no pattern)")
        passed = bool(re.search(pat, response))
        return MetricScore(
            name=self.name, score=1.0 if passed else 0.0, passed=passed,
            detail=f"pattern={pat!r}" + ("" if passed else " — NO MATCH"),
        )


# --------------------------------------------------------------------- #
# LLM-as-judge
# --------------------------------------------------------------------- #


class LLMJudge:
    name = "llm_judge"

    def __init__(
        self,
        *,
        rubric: str,
        model: str = "nova-pro",
        region: str = "us-east-1",
        project: str | None = None,
        location: str | None = None,
        pass_threshold: float = 0.7,
    ) -> None:
        """Generic LLM-as-judge metric.

        `model` may be ANY alias the LLM primitive accepts:
          - Bedrock: 'nova-pro', 'claude-haiku', 'claude-sonnet', ...
          - Gemini:  'gemini-flash', 'gemini-pro'
          - Raw model IDs are also accepted.
        """
        self.rubric = rubric
        self.model = model
        self.region = region
        self.project = project
        self.location = location
        self.pass_threshold = pass_threshold

    async def evaluate(self, *, case, response: str) -> MetricScore:
        import cloudless
        prompt = (
            f"You are evaluating an agent response against a rubric.\n\n"
            f"Rubric: {self.rubric}\n\n"
            f"Prompt sent to agent: {case.prompt}\n\n"
            f"Agent response: {response}\n\n"
            f"Score the response on a scale of 0.0 to 1.0 against the rubric. "
            f"Reply with ONLY a JSON object: "
            f'{{"score": <float>, "reason": "<one sentence>"}}'
        )
        # Resolve provider to pass the right kwargs
        from cloudless.catalog.llm import resolve_model
        alias = resolve_model(self.model)
        kwargs: dict = {"model": self.model, "region": self.region}
        if alias.provider == "gemini":
            if self.project:
                kwargs["project"] = self.project
            if self.location:
                kwargs["location"] = self.location
        llm = cloudless.LLM(**kwargs)
        raw = await llm.invoke(prompt, max_tokens=256)
        # Best-effort JSON parse
        import json
        try:
            # Strip code fences if present
            stripped = raw.strip()
            if stripped.startswith("```"):
                stripped = stripped.strip("`")
                if stripped.startswith("json"):
                    stripped = stripped[4:]
                stripped = stripped.strip()
            data = json.loads(stripped)
            score = float(data.get("score", 0.0))
            reason = data.get("reason", "")[:200]
        except Exception:
            return MetricScore(name=self.name, score=0.0, passed=False,
                                detail=f"could not parse judge output: {raw[:120]!r}")
        passed = score >= self.pass_threshold
        return MetricScore(name=self.name, score=score, passed=passed, detail=reason)


# --------------------------------------------------------------------- #
# Public constructors
# --------------------------------------------------------------------- #


def contains_substring(**kwargs) -> ContainsSubstring:
    return ContainsSubstring(**kwargs)


def regex_match(**kwargs) -> RegexMatch:
    return RegexMatch(**kwargs)


def llm_judge(rubric: str, **kwargs) -> LLMJudge:
    return LLMJudge(rubric=rubric, **kwargs)
