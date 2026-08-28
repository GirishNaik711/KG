#!/usr/bin/env python3
"""
Benchmark harness for codemap-scale.

Clones real open-source repos and measures:
- T0 inventory time, T1 parse time, files/sec
- Query latency (structural search, call chain)
- Incremental update time
- DB size and peak memory

Usage:
    python benchmarks/run_benchmark.py
    python benchmarks/run_benchmark.py --repos flask,django
    python benchmarks/run_benchmark.py --include-linux
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psutil

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from codemap_scale.orchestrator import CodeMapScale

# -------------------------------------------------------------------
# Repo definitions
# -------------------------------------------------------------------

REPOS = {
    "flask": {
        "url": "https://github.com/pallets/flask.git",
        "focus_dirs": ["src/flask/", "tests/"],
        "query_patterns": ["*route*", "*Flask*", "*Blueprint*", "*request*", "*Response*",
                           "*app*", "*view*", "*session*", "*config*", "*error*"],
        "chain_fqns": [],  # populated after scout
    },
    "fastapi": {
        "url": "https://github.com/fastapi/fastapi.git",
        "focus_dirs": ["fastapi/", "tests/"],
        "query_patterns": ["*Depends*", "*APIRouter*", "*FastAPI*", "*Request*", "*Response*",
                           "*middleware*", "*security*", "*params*", "*routing*", "*exception*"],
        "chain_fqns": [],
    },
    "django": {
        "url": "https://github.com/django/django.git",
        "focus_dirs": ["django/db/models/", "django/http/", "django/core/"],
        "query_patterns": ["*Model*", "*QuerySet*", "*View*", "*Middleware*", "*Compiler*",
                           "*Manager*", "*Field*", "*HttpResponse*", "*URLPattern*", "*Form*"],
        "chain_fqns": [],
    },
    "cpython": {
        "url": "https://github.com/python/cpython.git",
        "focus_dirs": ["Lib/", "Lib/test/"],
        "query_patterns": ["*import*", "*compile*", "*parse*", "*socket*", "*thread*",
                           "*async*", "*path*", "*json*", "*http*", "*logging*"],
        "chain_fqns": [],
    },
    "kubernetes": {
        "url": "https://github.com/kubernetes/kubernetes.git",
        "focus_dirs": ["pkg/api/", "pkg/controller/", "cmd/"],
        "query_patterns": ["*Controller*", "*Server*", "*Handler*", "*Config*", "*Client*",
                           "*Manager*", "*Pod*", "*Node*", "*Service*", "*Deploy*"],
        "chain_fqns": [],
    },
    "linux": {
        "url": "https://github.com/torvalds/linux.git",
        "focus_dirs": ["kernel/", "drivers/net/", "fs/"],
        "query_patterns": ["*init*", "*read*", "*write*", "*open*", "*close*",
                           "*alloc*", "*free*", "*lock*", "*sched*", "*irq*"],
        "chain_fqns": [],
    },
}

CACHE_DIR = Path.home() / ".cache" / "codemap-bench"


def clone_repo(name: str) -> Path:
    """Clone repo to cache dir (shallow clone, reuse if exists)."""
    info = REPOS[name]
    dest = CACHE_DIR / name
    if dest.exists():
        print(f"  Using cached repo: {dest}")
        return dest

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  Cloning {info['url']} → {dest} ...")
    subprocess.run(
        ["git", "clone", "--depth=1", info["url"], str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def measure_memory() -> float:
    """Return current RSS in MB."""
    proc = psutil.Process(os.getpid())
    return proc.memory_info().rss / (1024 * 1024)


def count_source_files(repo_path: Path) -> int:
    """Quick count of source files using the engine's walk."""
    from codemap_scale.parser.parallel_engine import ParallelParserEngine
    engine = ParallelParserEngine(root=repo_path, workers=1)
    return len(engine.walk_files())


def benchmark_repo(name: str, repo_path: Path) -> dict:
    """Run full benchmark suite on a repo."""
    info = REPOS[name]
    results = {"name": name, "path": str(repo_path)}

    db_path = CACHE_DIR / f"{name}_bench.db"
    if db_path.exists():
        db_path.unlink()

    # Force garbage collection before measuring
    gc.collect()
    mem_before = measure_memory()

    cm = CodeMapScale(repo_path, db_path=db_path, workers=None)

    try:
        # --- T0 + T1 Scout ---
        print(f"  Scouting {name}...")
        t_start = time.perf_counter()
        stats = cm.scout(force=True)
        scout_time = time.perf_counter() - t_start

        results["files"] = stats["files"]
        results["symbols"] = stats["symbols"]
        results["relations"] = stats["relations"]
        results["languages"] = stats.get("languages", {})
        results["scout_total_s"] = round(scout_time, 2)
        results["t0_s"] = stats["phases"]["tier0"]["elapsed_seconds"]
        results["t0_rate"] = stats["phases"]["tier0"]["rate"]
        results["t1_s"] = stats["phases"].get("tier1", {}).get("elapsed_seconds", 0)
        results["t1_rate"] = stats["phases"].get("tier1", {}).get("rate", 0)
        results["t1_symbols"] = stats["phases"].get("tier1", {}).get("symbols", 0)
        results["backend"] = stats["phases"]["tier0"].get("backend", "unknown")

        # --- T2 Focus ---
        focus_times = []
        for focus_dir in info["focus_dirs"][:3]:
            t = time.perf_counter()
            cm.focus(focus_dir)
            focus_times.append(time.perf_counter() - t)
        results["t2_focus_s"] = round(sum(focus_times), 2)
        results["t2_focus_dirs"] = len(focus_times)

        # --- Structural Query Latencies ---
        query_times = []
        for pattern in info["query_patterns"]:
            t = time.perf_counter()
            cm.tools.search_structural(pattern)
            query_times.append((time.perf_counter() - t) * 1000)  # ms

        results["query_count"] = len(query_times)
        results["query_p50_ms"] = round(statistics.median(query_times), 2)
        results["query_p95_ms"] = round(sorted(query_times)[int(len(query_times) * 0.95)], 2) if len(query_times) > 1 else round(query_times[0], 2)
        results["query_p99_ms"] = round(sorted(query_times)[-1], 2)

        # --- Call Chain (if symbols available) ---
        # Find a symbol to use for call chain
        overview = cm.tools.get_overview()
        hotspots = overview.get("hotspots", [])
        chain_times = []
        for hs in hotspots[:5]:
            if hs.get("fqn"):
                t = time.perf_counter()
                cm.tools.get_call_chain(hs["fqn"], depth=2)
                chain_times.append((time.perf_counter() - t) * 1000)

        if chain_times:
            results["chain_p50_ms"] = round(statistics.median(chain_times), 2)
            results["chain_p95_ms"] = round(sorted(chain_times)[-1], 2)
        else:
            results["chain_p50_ms"] = None
            results["chain_p95_ms"] = None

        # --- Incremental Update (no changes) ---
        t = time.perf_counter()
        update_result = cm.update()
        results["update_noop_s"] = round(time.perf_counter() - t, 2)

        # --- Incremental Update (touch 10 files) ---
        from codemap_scale.parser.parallel_engine import ParallelParserEngine
        engine = ParallelParserEngine(root=repo_path, workers=1)
        all_files = engine.walk_files()[:10]
        for f in all_files:
            f.touch()  # Update mtime (but content hash won't change for touch)

        # Actually modify content to trigger re-parse
        modified = 0
        for f in all_files:
            try:
                content = f.read_text()
                f.write_text(content + "\n# benchmark modification\n")
                modified += 1
            except (OSError, UnicodeDecodeError):
                continue

        t = time.perf_counter()
        update_result = cm.update()
        results["update_10files_s"] = round(time.perf_counter() - t, 2)
        results["update_10files_changed"] = update_result["changed"]

        # Restore modified files
        for f in all_files[:modified]:
            try:
                content = f.read_text()
                if content.endswith("\n# benchmark modification\n"):
                    f.write_text(content[:-len("\n# benchmark modification\n")])
            except (OSError, UnicodeDecodeError):
                continue

        # --- DB Size ---
        results["db_size_mb"] = round(db_path.stat().st_size / (1024 * 1024), 1)

        # --- Memory ---
        gc.collect()
        mem_after = measure_memory()
        results["peak_rss_mb"] = round(mem_after, 1)
        results["rss_delta_mb"] = round(mem_after - mem_before, 1)

    finally:
        cm.close()
        # Clean up benchmark DB
        if db_path.exists():
            db_path.unlink()

    return results


def format_results_table(all_results: list[dict]) -> str:
    """Format results as a markdown table."""
    lines = []
    lines.append("# CodeMap-Scale Benchmark Results\n")
    lines.append(f"Date: {time.strftime('%Y-%m-%d %H:%M')}\n")

    # Summary table
    lines.append("| Repo | Files | Symbols | Scout (s) | T0 (s) | T1 (s) | T1 files/s | T2 Focus (s) | Query p95 (ms) | DB (MB) | RSS (MB) |")
    lines.append("|------|-------|---------|-----------|--------|--------|------------|--------------|----------------|---------|----------|")

    for r in all_results:
        lines.append(
            f"| {r['name']} | {r['files']:,} | {r['symbols']:,} | "
            f"{r['scout_total_s']} | {r['t0_s']} | {r['t1_s']} | {r['t1_rate']:,} | "
            f"{r['t2_focus_s']} | {r.get('query_p95_ms', 'N/A')} | "
            f"{r['db_size_mb']} | {r['peak_rss_mb']} |"
        )

    lines.append("")

    # Query performance
    lines.append("## Query Performance\n")
    lines.append("| Repo | Queries | p50 (ms) | p95 (ms) | p99 (ms) | Chain p50 (ms) | Chain p95 (ms) |")
    lines.append("|------|---------|----------|----------|----------|----------------|----------------|")
    for r in all_results:
        lines.append(
            f"| {r['name']} | {r['query_count']} | {r['query_p50_ms']} | "
            f"{r['query_p95_ms']} | {r['query_p99_ms']} | "
            f"{r.get('chain_p50_ms', 'N/A')} | {r.get('chain_p95_ms', 'N/A')} |"
        )

    lines.append("")

    # Update performance
    lines.append("## Incremental Update\n")
    lines.append("| Repo | No-op (s) | 10 files (s) | Files detected |")
    lines.append("|------|-----------|--------------|----------------|")
    for r in all_results:
        lines.append(
            f"| {r['name']} | {r['update_noop_s']} | {r['update_10files_s']} | "
            f"{r.get('update_10files_changed', 'N/A')} |"
        )

    lines.append("")

    # Language breakdown
    lines.append("## Language Distribution\n")
    for r in all_results:
        langs = r.get("languages", {})
        top_langs = sorted(langs.items(), key=lambda x: -x[1])[:5]
        lang_str = ", ".join(f"{l}: {c:,}" for l, c in top_langs)
        lines.append(f"- **{r['name']}**: {lang_str}")

    lines.append(f"\nBackend: {all_results[0].get('backend', 'unknown') if all_results else 'N/A'}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Benchmark codemap-scale on real codebases")
    parser.add_argument(
        "--repos", type=str, default="flask,fastapi",
        help="Comma-separated repo names (flask,fastapi,django,cpython,kubernetes,linux)",
    )
    parser.add_argument("--include-linux", action="store_true", help="Include Linux kernel (very large)")
    parser.add_argument("--output", type=str, default=None, help="Output file (default: benchmarks/RESULTS.md)")
    parser.add_argument("--json-output", type=str, default=None, help="Also write raw JSON results")
    args = parser.parse_args()

    repo_names = [r.strip() for r in args.repos.split(",")]
    if args.include_linux and "linux" not in repo_names:
        repo_names.append("linux")

    # Validate repo names
    for name in repo_names:
        if name not in REPOS:
            print(f"Unknown repo: {name}. Available: {', '.join(REPOS.keys())}")
            sys.exit(1)

    print(f"Benchmarking {len(repo_names)} repos: {', '.join(repo_names)}\n")

    all_results = []
    for name in repo_names:
        print(f"\n{'='*60}")
        print(f"Benchmarking: {name}")
        print(f"{'='*60}")

        try:
            repo_path = clone_repo(name)
            file_count = count_source_files(repo_path)
            print(f"  Source files: {file_count:,}")

            results = benchmark_repo(name, repo_path)
            all_results.append(results)

            print(f"\n  Results for {name}:")
            print(f"    Files: {results['files']:,}, Symbols: {results['symbols']:,}")
            print(f"    Scout: {results['scout_total_s']}s (T0: {results['t0_s']}s, T1: {results['t1_s']}s)")
            print(f"    T1 rate: {results['t1_rate']:,} files/sec")
            print(f"    Query p95: {results['query_p95_ms']}ms")
            print(f"    DB size: {results['db_size_mb']}MB, RSS: {results['peak_rss_mb']}MB")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()

    # Write results
    if all_results:
        output_path = Path(args.output) if args.output else Path(__file__).parent / "RESULTS.md"
        report = format_results_table(all_results)
        output_path.write_text(report)
        print(f"\nResults written to: {output_path}")

        if args.json_output:
            Path(args.json_output).write_text(json.dumps(all_results, indent=2, default=str))
            print(f"JSON written to: {args.json_output}")

        # Print summary table
        print(f"\n{'='*60}")
        print(report)


if __name__ == "__main__":
    main()
