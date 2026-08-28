#!/usr/bin/env python3
"""
Standalone token-usage report — human-run, terminal only.

Scans ~/.claude/projects/ and reports one project's aggregated token usage to
the terminal. NOTHING is written to disk. Target resolution, in priority order:
  1. --project <name>            explicit clean name
  2. auto-detect from cwd        when this directory maps to a known project
  3. interactive menu            fallback (or forced with --menu)

Numbers mirror what .aah/audit/token-usage.json persists: headline totals are
the RAW sums of the 4 Anthropic counters (input + output + cache_read +
cache_creation), NOT output-based spend. Aggregation reuses
``activity_logger.parse_transcript`` so the math stays single-sourced. The
report also shows each row's share % of the project total.

Run manually, outside Claude:

    aah run core.logging.token_usage_report              # auto-detect cwd, else menu
    aah run core.logging.token_usage_report --menu       # force the picker
    aah run core.logging.token_usage_report --project ascend-agentic-harness
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Reuse, don't duplicate — the transcript walker and the counter-key tuple are
# the single source of truth for token math. We intentionally do NOT import

from aah.core.logging.activity_logger import (
    parse_transcript, fold_leading_none, _COUNTER_KEYS,
)
# Canonical AAH phase sequence — single source of truth for phase ordering.
from aah.core.common.phase_transition import PHASE_ORDER


# WHAT: reads a transcript JSONL and returns the first `cwd` it finds.
# WHY:  the ~/.claude/projects/ folder name is a mangled slug (every separator
#       collapsed to '-', so real underscores are lost and irreversible). The
#       transcript's `cwd` field carries the true on-disk path, so Path(cwd).name
#       recovers the real last project name. `cwd` is NOT always on line 1
#       (summary/leafUuid entries precede it), so we scan until we find it.
def _cwd_from_transcript(transcript_path: Path) -> str | None:
    """Return the first ``cwd`` recorded in a transcript, or None."""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


# WHAT: derives the clean display name for a project folder — Path(cwd).name when
#       a transcript carried a cwd, else the last hyphen-token of the slug.
# WHY:  the cwd is authoritative (preserves underscores/case); the slug fallback
#       is a best-effort last resort for folders whose transcripts have no cwd.
def _display_name(slug: str, cwd: str | None) -> str:
    """Best clean name for a project folder, given its authoritative cwd."""
    if cwd:
        # Handle both Windows and POSIX separators regardless of host OS.
        leaf = cwd.replace("\\", "/").rstrip("/").split("/")[-1]
        if leaf:
            return leaf
    # Fallback: last hyphen-separated token of the mangled slug.
    return slug.rstrip("-").split("-")[-1] or slug


# WHAT: cheap check — does a transcript contain at least one assistant turn?
# WHY:  Claude Code writes metadata-only stubs at session init (last-prompt /
#       mode / permission-mode records, no conversation). Those aren't real
#       sessions, so both the menu tally and the report exclude them. This scans
#       for the first "assistant" line and stops — no full JSON parse per line.
def _has_assistant_turn(transcript_path: Path) -> bool:
    """True if the transcript has any assistant turn (a real session)."""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type": "assistant"' in line or '"type":"assistant"' in line:
                    return True
    except OSError:
        pass
    return False


# WHAT: scans ~/.claude/projects/ and returns one entry per non-empty folder,
#       sorted most-recently-modified first, with a clean display name and a
#       disambiguator for rows that share a display name.
# WHY:  the menu needs clean names, but two folders can resolve to the same
#       name (e.g. multiple 'ascend-agentic-harness' under different parents).
#       Each slug stays its own row; colliding names get the parent path
#       segment appended so the user can pick the exact one.
def discover_projects() -> list[dict]:
    """Enumerate Claude Code projects under ~/.claude/projects/.

    Returns a list of dicts sorted by mtime (most recent first):
        {slug, display_name, disambiguator, session_count, mtime, dir, cwd}
    Folders with no *.jsonl transcripts are skipped.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []

    entries: list[dict] = []
    for child in projects_dir.iterdir():
        if not child.is_dir():
            continue
        transcripts = list(child.glob("*.jsonl"))
        if not transcripts:
            continue  # No sessions recorded — skip.
        # Newest transcript first — used both for mtime and as the cwd sample.
        transcripts.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        # Count only real sessions (with an assistant turn); ignore metadata stubs.
        real = [t for t in transcripts if _has_assistant_turn(t)]
        if not real:
            continue  # Folder holds only metadata stubs — not a real project.
        sample = real[0]
        sample_cwd = _cwd_from_transcript(sample)
        entries.append({
            "slug": child.name,
            "display_name": _display_name(child.name, sample_cwd),
            "disambiguator": "",
            "session_count": len(real),
            "mtime": sample.stat().st_mtime,
            "dir": child,
            "cwd": sample_cwd,
        })

    entries.sort(key=lambda e: e["mtime"], reverse=True)

    # Disambiguate colliding display names using the parent path segment of the
    # slug (the token just before the display leaf). Only applied where needed.
    name_counts: dict[str, int] = {}
    for e in entries:
        name_counts[e["display_name"]] = name_counts.get(e["display_name"], 0) + 1
    for e in entries:
        if name_counts[e["display_name"]] > 1:
            tokens = e["slug"].rstrip("-").split("-")
            # tokens[-1] is (roughly) the leaf; tokens[-2] is its parent segment.
            parent = tokens[-2] if len(tokens) >= 2 else e["slug"]
            e["disambiguator"] = parent

    return entries


# WHAT: mangles an on-disk path the way Claude Code names its projects folder —
#       every char outside [A-Za-z0-9] becomes '-'. E.g. "/mnt/c/My Proj_x" ->
#       "-mnt-c-My-Proj-x".
# WHY:  the ~/.claude/projects/ folder for a cwd is exactly this mangling, so we
#       can round-trip the current directory to its slug and match it deterministically.
def _mangle_path(path: str) -> str:
    """Return the Claude Code projects-folder slug for an on-disk path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


# WHAT: matches the current working directory against the discovered projects and
#       returns the one entry it belongs to, or None if it can't be determined.
# WHY:  running the report from inside a project should "just work" — no menu.
#       Primary match is the deterministic slug mangling of the cwd; the exact
#       recorded-cwd string is a secondary check. None => fall back to the menu.
def detect_current_project(projects: list[dict]) -> dict | None:
    """Return the project whose folder corresponds to the current cwd, or None."""
    try:
        cwd = str(Path.cwd())
    except OSError:
        return None
    slug = _mangle_path(cwd)
    for p in projects:
        if p["slug"] == slug:
            return p
    # Secondary: exact match on the authoritative cwd recorded in a transcript.
    for p in projects:
        if p.get("cwd") and p["cwd"].replace("\\", "/").rstrip("/") == cwd.replace("\\", "/").rstrip("/"):
            return p
    return None


# WHAT: prints the numbered menu and reads the user's choice from stdin.
# WHY:  the tool is run manually outside Claude, so a blocking input() prompt is
#       appropriate. Returns the chosen entry, or None if the user quits.
def prompt_selection(projects: list[dict]) -> dict | None:
    """Interactive numbered menu. Returns the chosen project or None to quit."""
    print("\nClaude Code projects (most recent first):\n")
    width = len(str(len(projects)))
    for i, p in enumerate(projects, start=1):
        name = p["display_name"]
        if p["disambiguator"]:
            name = f"{name}  — {p['disambiguator']}"
        when = _fmt_mtime(p["mtime"])
        sess = f"{p['session_count']} session" + ("s" if p["session_count"] != 1 else "")
        print(f"  {str(i).rjust(width)}) {name}   ·  {sess}  ·  {when}")

    print()
    while True:
        try:
            raw = input(f"Select a project [1-{len(projects)}, q to quit]: ").strip()
        except EOFError:
            return None
        if raw.lower() in ("q", "quit", "exit", ""):
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(projects):
                return projects[idx - 1]
        print("  Invalid selection — enter a number from the list, or 'q' to quit.")


# WHAT: turns an mtime float into a short YYYY-MM-DD string.
# WHY:  the menu shows when each project was last touched; a full timestamp is
#       noise, a date is enough to orient the user.
def _fmt_mtime(mtime: float) -> str:
    """Format an mtime as YYYY-MM-DD (local time)."""
    from datetime import datetime
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "unknown"


# WHAT: parses every transcript in a project folder and combines them into one
#       aggregate matching the token-usage.json schema (raw-sum totals).
# WHY:  a project has many sessions; the report is project-wide. We reuse
#       parse_transcript per session, then sum the 4 raw counters, carry the
#       pre-computed main/subagent split, merge per_model_usage, and keep a
#       per-session row list for the recent-sessions table.
def aggregate_project(project_dir: Path) -> dict:
    """Aggregate all sessions in ``project_dir`` into one report dict."""
    combined = {k: 0 for k in _COUNTER_KEYS}
    main_thread_tokens = 0
    subagent_tokens = 0
    per_model: dict[str, dict] = {}
    per_phase: dict[str, dict] = {}
    sessions: list[dict] = []

    # Buffer each session's per-phase dict (chronological order) so the leading
    # "(none)" prefix can be folded across sessions before we sum project-wide.
    session_phase_usages: list[dict] = []

    transcripts = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    for tp in transcripts:
        try:
            parsed = parse_transcript(tp)
        except Exception:
            continue  # Unreadable/half-written transcript — skip; totals stay valid.

        usage = parsed.get("token_usage", {}) or {}

        # Skip metadata-only stubs: transcripts Claude Code writes at session
        # init (last-prompt / mode / permission-mode records) with no assistant
        # turns and zero usage. These aren't real sessions the user prompted, so
        # they'd otherwise show as a 0-token row and inflate the session count.
        if not parsed.get("messages") and sum(int(usage.get(k, 0) or 0) for k in _COUNTER_KEYS) == 0:
            continue

        for k in _COUNTER_KEYS:
            combined[k] += int(usage.get(k, 0) or 0)
        main_thread_tokens += int(usage.get("main_thread_tokens", 0) or 0)
        subagent_tokens += int(usage.get("subagent_tokens", 0) or 0)

        # Merge per-model contributions (raw counters per model).
        for model_name, counters in (parsed.get("per_model_usage") or {}).items():
            if not isinstance(counters, dict):
                continue
            bucket = per_model.setdefault(
                model_name, {"model": model_name, "sessions": 0,
                             **{k: 0 for k in _COUNTER_KEYS}})
            bucket["sessions"] += 1
            for k in _COUNTER_KEYS:
                bucket[k] += int(counters.get(k, 0) or 0)

        # Buffer this session's per-phase dict (coerced to plain int counters);
        # summed into per_phase AFTER the cross-session (none)-fold below.
        this_phase: dict[str, dict] = {}
        for phase_name, counters in (parsed.get("per_phase_usage") or {}).items():
            if not isinstance(counters, dict):
                continue
            this_phase[phase_name] = {k: int(counters.get(k, 0) or 0) for k in _COUNTER_KEYS}
        session_phase_usages.append(this_phase)

        s_in = int(usage.get("input_tokens", 0) or 0)
        s_out = int(usage.get("output_tokens", 0) or 0)
        s_cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        s_cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
        s_io = s_in + s_out
        s_cache = s_cr + s_cc
        sessions.append({
            "session_id": parsed.get("session_id"),
            "phase": parsed.get("phase"),
            "total_all_tokens": s_io + s_cache,
            "total_io_tokens": s_io,
            "total_cache_tokens": s_cache,
            "total_input_tokens": s_in,
            "total_output_tokens": s_out,
            "total_cache_read_tokens": s_cr,
            "total_cache_creation_tokens": s_cc,
            "mtime": tp.stat().st_mtime,
        })

    # Cross-session fold: reattribute each session's leading "(none)" prefix to
    # the previous session's last phase (fallback: this session's first phase).
    # Then sum the folded per-session dicts into the project-wide per_phase.
    fold_leading_none(session_phase_usages)
    for this_phase in session_phase_usages:
        for phase_name, counters in this_phase.items():
            bucket = per_phase.setdefault(
                phase_name, {"phase": phase_name, "sessions": 0,
                             **{k: 0 for k in _COUNTER_KEYS}})
            bucket["sessions"] += 1
            for k in _COUNTER_KEYS:
                bucket[k] += int(counters.get(k, 0) or 0)

    total_io = combined["input_tokens"] + combined["output_tokens"]
    total_cache = combined["cache_read_input_tokens"] + combined["cache_creation_input_tokens"]
    total_all = total_io + total_cache

    # Derived aggregates on each model row + sort by spend.
    for m in per_model.values():
        m["total_io_tokens"] = m["input_tokens"] + m["output_tokens"]
        m["total_cache_tokens"] = m["cache_read_input_tokens"] + m["cache_creation_input_tokens"]
        m["total_all_tokens"] = m["total_io_tokens"] + m["total_cache_tokens"]
    models = sorted(per_model.values(), key=lambda m: -m["total_all_tokens"])

    # Derived aggregates on each phase row + sort by AAH phase sequence.
    for p in per_phase.values():
        p["total_io_tokens"] = p["input_tokens"] + p["output_tokens"]
        p["total_cache_tokens"] = p["cache_read_input_tokens"] + p["cache_creation_input_tokens"]
        p["total_all_tokens"] = p["total_io_tokens"] + p["total_cache_tokens"]
    # Order rows by the canonical AAH phase sequence (init, discuss, architecture,
    # plan, build, deploy, complete) rather than by spend, so the table reads as a
    # timeline. Phases not in PHASE_ORDER (e.g. "(none)" pre-phase turns, "audit")
    # sort after the known ones, most-spend first among themselves.
    def _phase_sort_key(p: dict) -> tuple:
        try:
            rank = PHASE_ORDER.index(p["phase"])
        except ValueError:
            rank = len(PHASE_ORDER)
        return (rank, -p["total_all_tokens"])
    phases = sorted(per_phase.values(), key=_phase_sort_key)

    return {
        "session_count": len(sessions),
        "total_all_tokens": total_all,
        "total_io_tokens": total_io,
        "total_cache_tokens": total_cache,
        "input_tokens": combined["input_tokens"],
        "output_tokens": combined["output_tokens"],
        "cache_read_input_tokens": combined["cache_read_input_tokens"],
        "cache_creation_input_tokens": combined["cache_creation_input_tokens"],
        "main_thread_tokens": main_thread_tokens,
        "subagent_tokens": subagent_tokens,
        "models": models,
        "phases": phases,
        "sessions": sessions,
    }


# WHAT: renders an aligned fixed-width table — a header row, an underline, then
#       one row per record — with per-column left/right justification.
# WHY:  the model / phase / session tables all share this shape; one helper keeps
#       every column header padded to its widest cell so the numbers line up.
def _aligned_table(headers: list[str], rows: list[list[str]],
                   aligns: list[str], indent: str = "    ") -> list[str]:
    """Return the text lines for a padded table (header + rule + rows)."""
    ncol = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i in range(ncol):
            widths[i] = max(widths[i], len(r[i]))

    def fmt(cells: list[str]) -> str:
        parts = [
            cells[i].ljust(widths[i]) if aligns[i] == "l" else cells[i].rjust(widths[i])
            for i in range(ncol)
        ]
        return (indent + "  ".join(parts)).rstrip()

    out = [fmt(headers), indent + "  ".join("-" * widths[i] for i in range(ncol))]
    out.extend(fmt(r) for r in rows)
    return out


# WHAT: renders the aggregate into a formatted, aligned multi-section report
#       string (thousands separators throughout).
# WHY:  terminal-only output — this is the whole point of the tool. Sections
#       mirror token-usage.json: headline totals, raw counters,
#       ratios, origin split, per-model / per-phase / per-session tables. Every
#       "by *" table carries the full counter breakdown, a share %, and a bar.
def render_report(project_name: str, agg: dict) -> str:
    """Build the human-facing terminal report string."""
    def n(v: int) -> str:
        return f"{int(v):,}"

    def pct(part: float, whole: float) -> str:
        return f"{part / whole * 100:.1f}%" if whole else "—"

    def share_bar(part: float, whole: float, width: int = 12) -> str:
        frac = (part / whole) if whole else 0.0
        frac = max(0.0, min(1.0, frac))
        filled = int(round(frac * width))
        return "█" * filled + "░" * (width - filled)

    sc = agg["session_count"]
    grand = agg["total_all_tokens"] or 0
    lines: list[str] = []
    bar = "=" * 78

    lines.append("")
    lines.append(bar)
    lines.append(f"  TOKEN USAGE — {project_name}")
    lines.append(bar)

    # Headline totals (raw sums — the token-usage.json definition).
    lines.append("")
    lines.append("  Totals")
    lines.append("  ------")
    lines.append(f"    Total (all)     : {n(agg['total_all_tokens'])}")
    lines.append(f"    Total I/O       : {n(agg['total_io_tokens'])}   ({pct(agg['total_io_tokens'], grand)} of all)")
    lines.append(f"    Total cache     : {n(agg['total_cache_tokens'])}   ({pct(agg['total_cache_tokens'], grand)} of all)")
    lines.append(f"    Sessions        : {n(sc)}")

    # Raw counters (Anthropic API long names).
    lines.append("")
    lines.append("  Raw counters")
    lines.append("  ------------")
    lines.append(f"    input_tokens                : {n(agg['input_tokens'])}")
    lines.append(f"    output_tokens               : {n(agg['output_tokens'])}")
    lines.append(f"    cache_read_input_tokens     : {n(agg['cache_read_input_tokens'])}")
    lines.append(f"    cache_creation_input_tokens : {n(agg['cache_creation_input_tokens'])}")

    # Every "by *" table shares the SAME column structure so they line up and
    # read consistently: a name column, a per-table context column (sessions or
    # date), then the identical token breakdown + share % + bar.
    bd_head = ["input", "output", "cache_rd", "cache_wr",
               "total_io", "total_cache", "total", "%", "share"]
    bd_align = ["r", "r", "r", "r", "r", "r", "r", "r", "l"]

    def bd(inp, out, crd, cwr, tio, tca, tot) -> list[str]:
        """Build the shared breakdown cells (input..total + % + bar)."""
        return [n(inp), n(out), n(crd), n(cwr), n(tio), n(tca), n(tot),
                pct(tot, grand), share_bar(tot, grand)]

    total_bd = bd(
        agg["input_tokens"], agg["output_tokens"],
        agg["cache_read_input_tokens"], agg["cache_creation_input_tokens"],
        agg["total_io_tokens"], agg["total_cache_tokens"], grand,
    )

    # Per-model table.
    lines.append("")
    lines.append("  By model")
    lines.append("  --------")
    if agg["models"]:
        headers = ["model", "sess"] + bd_head
        aligns = ["l", "r"] + bd_align
        rows: list[list[str]] = []
        for m in agg["models"]:
            rows.append([m["model"], n(m["sessions"])] + bd(
                m["input_tokens"], m["output_tokens"],
                m["cache_read_input_tokens"], m["cache_creation_input_tokens"],
                m["total_io_tokens"], m["total_cache_tokens"], m["total_all_tokens"]))
        rows.append(["TOTAL", ""] + total_bd)
        lines.extend(_aligned_table(headers, rows, aligns))
    else:
        lines.append("    (no per-model data)")

    # Per-phase table (per-turn attribution; "(none)" = pre-phase turns).
    lines.append("")
    lines.append("  By phase")
    lines.append("  --------")
    if agg["phases"]:
        headers = ["phase", "sess"] + bd_head
        aligns = ["l", "r"] + bd_align
        rows = []
        for p in agg["phases"]:
            rows.append([p["phase"], n(p["sessions"])] + bd(
                p["input_tokens"], p["output_tokens"],
                p["cache_read_input_tokens"], p["cache_creation_input_tokens"],
                p["total_io_tokens"], p["total_cache_tokens"], p["total_all_tokens"]))
        rows.append(["TOTAL", ""] + total_bd)
        lines.extend(_aligned_table(headers, rows, aligns))
    else:
        lines.append("    (no per-phase data)")

    # Per-session table (newest first) — one row per session + TOTAL.
    lines.append("")
    lines.append("  By session")
    lines.append("  ----------")
    by_session = sorted(agg["sessions"], key=lambda s: s["mtime"], reverse=True)
    if by_session:
        headers = ["session", "date"] + bd_head
        aligns = ["l", "l"] + bd_align
        rows = []
        for s in by_session:
            sid = s["session_id"] or "(unknown)"
            short = (sid[:8] + "…") if len(sid) > 8 else sid
            rows.append([short, _fmt_mtime(s["mtime"])] + bd(
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read_tokens"], s["total_cache_creation_tokens"],
                s["total_io_tokens"], s["total_cache_tokens"], s["total_all_tokens"]))
        rows.append(["TOTAL", ""] + total_bd)
        lines.extend(_aligned_table(headers, rows, aligns))
    else:
        lines.append("    (no sessions)")

    lines.append("")
    lines.append(bar)
    lines.append("")
    return "\n".join(lines)


# WHAT: CLI entry point. Discovers projects, then resolves a target in priority
#       order — explicit --project, else auto-detect from cwd, else the menu —
#       aggregates it, and prints the report. Never writes.
# WHY:  backs `aah run core.logging.token_usage_report`. Running it from inside a
#       project should just report that project; only when the cwd can't be
#       matched (or --menu is passed) do we fall back to the interactive picker.
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone token-usage report for a Claude Code project (terminal only)."
    )
    parser.add_argument(
        "--project", default=None,
        help="Clean project name to report on (skips auto-detect and the menu). "
             "Matches the display name; if ambiguous, the menu is shown.",
    )
    parser.add_argument(
        "--menu", action="store_true",
        help="Always show the interactive picker, even when the current "
             "directory maps to a known project.",
    )
    args = parser.parse_args()

    projects = discover_projects()
    if not projects:
        print("No Claude Code projects found under ~/.claude/projects/.", file=sys.stderr)
        sys.exit(1)

    chosen: dict | None = None
    if args.project:
        matches = [p for p in projects if p["display_name"] == args.project]
        if len(matches) == 1:
            chosen = matches[0]
        elif len(matches) == 0:
            print(f"No project named '{args.project}'. Choose from the list below.",
                  file=sys.stderr)
        else:
            print(f"'{args.project}' is ambiguous ({len(matches)} matches). "
                  "Choose the exact one below.", file=sys.stderr)
    elif not args.menu:
        # Auto-detect the current project from the working directory.
        detected = detect_current_project(projects)
        if detected is not None:
            chosen = detected
            print(f"Auto-detected current project: {detected['display_name']}")
        else:
            print("Could not auto-determine the current project "
                  "(this directory isn't a recorded Claude Code project).")

    if chosen is None:
        chosen = prompt_selection(projects)
    if chosen is None:
        print("No project selected.", file=sys.stderr)
        sys.exit(0)

    agg = aggregate_project(chosen["dir"])
    print(render_report(chosen["display_name"], agg))
    sys.exit(0)


if __name__ == "__main__":
    main()
