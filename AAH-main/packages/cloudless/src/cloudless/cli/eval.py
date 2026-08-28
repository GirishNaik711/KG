"""`cloudless eval run <dataset>` CLI (Q8)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

import cloudless
from cloudless.eval import (
    EvalDataset,
    contains_substring,
    regex_match,
    run_eval,
)
from cloudless.eval.runner import write_jsonl

_console = Console()


async def _llm_target(prompt: str) -> str:
    """Default target — invoke cloudless.LLM(nova-micro) directly."""
    llm = cloudless.LLM(model="nova-micro")
    return await llm.invoke(prompt, max_tokens=200)


def run(
    *,
    dataset_path: str,
    output_path: str | None = None,
    model: str = "nova-micro",
    region: str = "us-east-1",
) -> int:
    """`cloudless eval run <dataset.jsonl>` — load + run + print summary."""
    path = Path(dataset_path)
    if not path.is_file():
        _console.print(f"[red]✗[/] dataset not found: {path}")
        return 1

    dataset = EvalDataset.from_jsonl(path)
    _console.print(f"[bold]eval[/]  dataset=[cyan]{path}[/]  cases={len(dataset.cases)}  model={model}")

    async def target(prompt: str) -> str:
        llm = cloudless.LLM(model=model, region=region)
        return await llm.invoke(prompt, max_tokens=200)

    metrics = [contains_substring(), regex_match()]
    run = asyncio.run(run_eval(
        dataset=dataset, target=target, metrics=metrics, dataset_path=str(path),
    ))

    # Summary table
    table = Table(title=f"eval run {run.run_id} ({run.duration_s:.1f}s)")
    table.add_column("metric", style="cyan")
    table.add_column("passed")
    table.add_column("total")
    table.add_column("pass rate")
    for metric, s in run.summary.items():
        table.add_row(metric, str(s["passed"]), str(s["total"]),
                      f"{s['pass_rate'] * 100:.1f}%")
    _console.print(table)

    if output_path:
        write_jsonl(run, output_path)
        _console.print(f"[dim]wrote results to {output_path}[/]")

    # Return non-zero if any metric below 100% (CI-gate friendly)
    if any(s["passed"] < s["total"] for s in run.summary.values()):
        return 2
    return 0
