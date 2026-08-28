"""cloudless.eval — portable offline eval framework (Q8).

Surface:
  - EvalCase, EvalDataset, EvalResult, EvalRun (data shapes)
  - run_eval(dataset, target, metrics) — executes dataset, returns EvalRun
  - Metrics: contains_substring, regex_match, llm_judge

Tied to cloudless.eval CLI commands in cloudless.cli.eval.
"""
from cloudless.eval.metrics import (
    Metric,
    contains_substring,
    llm_judge,
    regex_match,
)
from cloudless.eval.runner import (
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
    run_eval,
)

__all__ = [
    "EvalCase",
    "EvalDataset",
    "EvalResult",
    "EvalRun",
    "Metric",
    "contains_substring",
    "llm_judge",
    "regex_match",
    "run_eval",
]
