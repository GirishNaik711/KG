"""`cloudless cost` — read cost-telemetry events and report rollups.

Two ingest paths:

  1. Stdin JSONL: read NDJSON cost records (one per LLM call), aggregate.
     Supports the structlog format the embedded runtime emits at INFO level
     for cost events.

  2. Cassette files: scan a cassette JSONL and reconstruct cost from
     model_id + prompt/response token estimates (uses cloudless.pricing).

Outputs a rich table by default; JSON via `--format json`.

Usage:

    cat events.jsonl | cloudless cost            # rollup by model
    cat events.jsonl | cloudless cost --by team  # rollup by team
    cloudless cost --cassette tests/cassettes/*.cassette
"""
from __future__ import annotations

import glob
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from cloudless.runtime.pricing import estimate_cost_usd

_console = Console()


def _read_jsonl_events(stream) -> Iterable[dict]:
    """Read NDJSON cost events from `stream`. Skips malformed lines."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _record_to_cost(record: dict) -> tuple[str, str, float]:
    """Extract (model, group_key, usd) from a structlog cost record.

    Accepts records in two shapes:
      - "llm_call" event with model/input_tokens/output_tokens fields
      - cassette entries with model + prompt/text (cost estimated from len)
    """
    model = record.get("model") or record.get("model_id") or "<unknown>"
    in_tok = int(record.get("input_tokens") or 0)
    out_tok = int(record.get("output_tokens") or 0)
    cached = int(record.get("cached_tokens") or 0)
    reasoning = int(record.get("reasoning_tokens") or 0)

    if in_tok == 0 and out_tok == 0:
        # Cassette entry — estimate from text length (≈4 chars/token).
        prompt = record.get("prompt") or ""
        text = record.get("text") or ""
        in_tok = max(1, len(prompt) // 4)
        out_tok = max(1, len(text) // 4)

    usd = estimate_cost_usd(
        model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )
    group = record.get("team") or record.get("attribution_team") or "<no-team>"
    return model, group, usd


def _rollup(records: Iterable[dict], *, by: str = "model") -> dict[str, dict]:
    """Aggregate cost records by either 'model' or 'team'."""
    if by not in {"model", "team"}:
        raise ValueError("--by must be 'model' or 'team'")
    totals: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0}
    )
    for r in records:
        model, team, usd = _record_to_cost(r)
        key = model if by == "model" else team
        bucket = totals[key]
        bucket["calls"] += 1
        bucket["usd"] += usd
        bucket["input_tokens"] += int(r.get("input_tokens") or 0)
        bucket["output_tokens"] += int(r.get("output_tokens") or 0)
    return dict(totals)


def _render_table(totals: dict[str, dict], *, by: str) -> None:
    table = Table(title=f"cloudless cost — by {by}")
    table.add_column(by.capitalize(), style="cyan")
    table.add_column("Calls", justify="right")
    table.add_column("Input tok", justify="right")
    table.add_column("Output tok", justify="right")
    table.add_column("USD", justify="right", style="green")

    grand_calls = grand_in = grand_out = 0
    grand_usd = 0.0
    sorted_keys = sorted(totals.keys(), key=lambda k: -totals[k]["usd"])
    for key in sorted_keys:
        b = totals[key]
        table.add_row(
            key,
            str(b["calls"]),
            f"{b['input_tokens']:,}",
            f"{b['output_tokens']:,}",
            f"${b['usd']:.4f}",
        )
        grand_calls += b["calls"]
        grand_in += b["input_tokens"]
        grand_out += b["output_tokens"]
        grand_usd += b["usd"]

    table.add_row(
        "[bold]TOTAL[/]",
        f"[bold]{grand_calls}[/]",
        f"[bold]{grand_in:,}[/]",
        f"[bold]{grand_out:,}[/]",
        f"[bold]${grand_usd:.4f}[/]",
    )
    _console.print(table)


def run(
    *,
    by: str = "model",
    cassette_globs: list[str] | None = None,
    output_format: str = "table",
) -> int:
    """Entry point for `cloudless cost`."""
    records: list[dict] = []

    if cassette_globs:
        for pat in cassette_globs:
            for path in sorted(glob.glob(pat)):
                p = Path(path)
                if not p.is_file():
                    continue
                records.extend(_read_jsonl_events(p.open()))
    elif not sys.stdin.isatty():
        records.extend(_read_jsonl_events(sys.stdin))
    else:
        _console.print(
            "[yellow]No cost records on stdin and no --cassette glob. "
            "Pipe events JSONL or pass --cassette PATH.[/]"
        )
        return 1

    if not records:
        _console.print("[yellow]No cost records found.[/]")
        return 1

    totals = _rollup(records, by=by)

    if output_format == "json":
        print(json.dumps(totals, indent=2, sort_keys=True))
    else:
        _render_table(totals, by=by)
    return 0
