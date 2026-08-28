"""Eval runner — loads dataset, executes target, computes metrics (Q8)."""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cloudless.eval.metrics import Metric


@dataclass(frozen=True)
class EvalCase:
    """One row of an eval dataset."""
    id: str
    prompt: str
    expected_contains: str | None = None
    expected_regex: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EvalResult:
    """The score for one (case × metric) cell."""
    case_id: str
    metric: str
    score: float
    passed: bool
    detail: str
    response: str
    """The target's actual response — useful for diffs."""


@dataclass(frozen=True)
class EvalRun:
    """A whole eval run: dataset × target × metrics → results."""
    run_id: str
    dataset_path: str | None
    started_at: float
    duration_s: float
    results: list[EvalResult]
    summary: dict
    """{metric_name: {passed: int, total: int, pass_rate: float}}"""


# --------------------------------------------------------------------- #
# Dataset I/O
# --------------------------------------------------------------------- #


class EvalDataset:
    """A list of EvalCase. Loadable from JSONL."""

    def __init__(self, cases: list[EvalCase]) -> None:
        self.cases = cases

    @classmethod
    def from_jsonl(cls, path: str | Path) -> EvalDataset:
        cases: list[EvalCase] = []
        with Path(path).open() as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                cases.append(EvalCase(
                    id=str(obj.get("id", i)),
                    prompt=obj["prompt"],
                    expected_contains=obj.get("expected_contains"),
                    expected_regex=obj.get("expected_regex"),
                    metadata={k: v for k, v in obj.items()
                              if k not in ("id", "prompt", "expected_contains", "expected_regex")},
                ))
        return cls(cases)


# --------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------- #


TargetFn = Callable[[str], Awaitable[str]]
"""A target is anything that maps a prompt string → response string (async)."""


async def run_eval(
    dataset: EvalDataset,
    target: TargetFn,
    metrics: list[Metric],
    *,
    run_id: str | None = None,
    dataset_path: str | None = None,
) -> EvalRun:
    """Execute dataset against target; score each response with every metric."""
    run_id = run_id or uuid.uuid4().hex
    started = time.time()
    results: list[EvalResult] = []

    for case in dataset.cases:
        try:
            response = await target(case.prompt)
        except Exception as e:
            response = f"<ERROR: {type(e).__name__}: {e}>"
        for metric in metrics:
            score = await metric.evaluate(case=case, response=response)
            results.append(EvalResult(
                case_id=case.id, metric=score.name, score=score.score,
                passed=score.passed, detail=score.detail, response=response,
            ))

    duration = time.time() - started
    # Summary by metric
    summary: dict[str, dict] = {}
    for r in results:
        s = summary.setdefault(r.metric, {"passed": 0, "total": 0})
        s["total"] += 1
        if r.passed:
            s["passed"] += 1
    for _k, v in summary.items():
        v["pass_rate"] = v["passed"] / v["total"] if v["total"] else 0.0

    return EvalRun(
        run_id=run_id, dataset_path=dataset_path, started_at=started,
        duration_s=duration, results=results, summary=summary,
    )


# --------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------- #


def write_jsonl(run: EvalRun, path: str | Path) -> None:
    """Write eval results to a JSONL file for portability + diffing."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w") as f:
        for r in run.results:
            f.write(json.dumps(asdict(r)) + "\n")
