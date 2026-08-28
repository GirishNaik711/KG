#!/usr/bin/env python3
"""
Generate a cumulative wave completion summary.

Prints a human-readable summary of everything completed so far and saves
it to .aah/build/wave-summaries/wave-N-summary.md.

Usage:
  aah run core.build.wave_summary --wave <N>
  aah run core.build.wave_summary --wave <N> --project-path /path/to/project
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.sequenced_store import sequenced_paths


# Claude Sonnet 4.6 pricing (per million tokens)
PRICING = {
    "input":          3.00,
    "output":        15.00,
    "cache_write":    3.75,
    "cache_read":     0.30,
}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _latest_human_decision(aah_root: Path, feature_id: str) -> dict:
    """Newest human-NNN.json for a feature, or {} if none.

    Names use a minimum width of three digits and are ordered numerically.
    """
    d = aah_root / "build" / "qa-results" / feature_id
    files = sequenced_paths(d, "human-")
    return _read_json(files[-1][1]) if files else {}


def _read_yaml(path: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception:
        return {}


def _git(args: list, cwd: Path) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _estimate_cost(tokens: dict) -> float:
    """Estimate USD cost from token-usage.json totals."""
    inp   = tokens.get("total_input_tokens", 0)
    out   = tokens.get("total_output_tokens", 0)
    cw    = tokens.get("total_cache_creation_tokens", 0)
    cr    = tokens.get("total_cache_read_tokens", 0)
    return (
        inp * PRICING["input"]      / 1_000_000
        + out * PRICING["output"]   / 1_000_000
        + cw  * PRICING["cache_write"] / 1_000_000
        + cr  * PRICING["cache_read"]  / 1_000_000
    )


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def generate(project_path: Path, wave_num: int) -> str:
    aah_root = project_path / ".aah"

    # ── Wave features ──────────────────────────────────────────────────
    from aah.core.plan.compute_waves import flatten_waves
    waves_data = _read_json(aah_root / "plan" / "waves.json")
    waves = flatten_waves(waves_data)
    wave_feature_ids = waves[wave_num] if wave_num < len(waves) else []

    # ── Feature list (all features + pass status) ──────────────────────
    fl_data = _read_json(aah_root / "feature-list.json")
    all_features = fl_data.get("features", [])
    by_id = {f["id"]: f for f in all_features}
    passing_ids = {f["id"] for f in all_features if f.get("passes")}

    # ── QA results for this wave ───────────────────────────────────────
    # Outcome, findings, attempt count, and subject SHA all come from the
    # append-only attempt tree; human decisions come from human-NNN.json.
    from aah.core.build import qa_evidence

    qa_dir = aah_root / "build" / "test-results"
    qa_results = {}
    qa_states = {}          # fid -> derive_qa_state(...) dict
    human_states = {}       # fid -> latest human decision dict (or {})
    for fid in wave_feature_ids:
        qa_results[fid] = qa_evidence.latest_attempt(project_path, fid) or {}
        qa_states[fid] = qa_evidence.derive_qa_state(project_path, fid)
        human_states[fid] = _latest_human_decision(aah_root, fid)

    # ── Token usage (cumulative) ───────────────────────────────────────
    tokens = _read_json(aah_root / "audit" / "token-usage.json")
    total_input  = tokens.get("total_input_tokens", 0)
    total_output = tokens.get("total_output_tokens", 0)
    total_cw     = tokens.get("total_cache_creation_tokens", 0)
    total_cr     = tokens.get("total_cache_read_tokens", 0)
    total_all    = tokens.get("total_all_tokens", total_input + total_output)
    sessions     = tokens.get("session_count", 0)
    est_cost     = _estimate_cost(tokens)

    # ── Git stats for this wave ────────────────────────────────────────
    # Commits on develop since the previous wave integration branch was merged
    wave_commits_raw = _git(
        ["log", "--oneline", f"develop", "--since=1.week.ago"],
        project_path
    )
    wave_commit_lines = [l for l in wave_commits_raw.splitlines() if l.strip()]

    # Total commits on develop
    total_commits = _git(["rev-list", "--count", "develop"], project_path)

    # ── Feature YAML details for this wave ────────────────────────────
    features_dir = aah_root / "plan" / "features"
    wave_feature_details = []
    for fid in wave_feature_ids:
        from aah.core.common.feature_utils import load_feature_data
        data = load_feature_data(features_dir, fid)

        # Git commits for this feature (by message prefix)
        commits_raw = _git(
            ["log", "--oneline", "--all", f"--grep=({fid})"],
            project_path,
        )
        feature_commits = [l for l in commits_raw.splitlines() if l.strip()]

        # Files touched in those commits
        files_changed: list[str] = []
        if feature_commits:
            sha = feature_commits[0].split()[0]
            diff_raw = _git(["diff-tree", "--no-commit-id", "-r", "--name-only", sha], project_path)
            files_changed = [f for f in diff_raw.splitlines() if f.strip()]

        # Test results
        tr_path = qa_dir / f"{fid}.json"
        test_results = _read_json(tr_path)

        wave_feature_details.append({
            "id": fid,
            "description": data.get("description", by_id.get(fid, {}).get("description", "")),
            "acceptance_criteria": data.get("acceptance_criteria", []),
            "test_cases": data.get("test_cases", []),
            "test_config": data.get("test_config", {}),
            "passes": fid in passing_ids,
            "commits": feature_commits,
            "files_changed": files_changed,
            "test_results": test_results,
        })

    # ── Build cumulative feature table ────────────────────────────────
    cumulative_rows = []
    for wave_idx, wave_fids in enumerate(waves):
        for fid in wave_fids:
            f = by_id.get(fid, {})
            status = "✅ PASS" if fid in passing_ids else "⏳ pending"
            cumulative_rows.append((wave_idx, fid, f.get("description", ""), status))

    # ── Compose markdown ──────────────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    a = lines.append

    a(f"# Wave {wave_num} Complete — Cumulative Summary")
    a(f"_Generated: {now}_")
    a("")

    # Progress bar
    done = len(passing_ids)
    total = len(all_features)
    pct = int(done / total * 100) if total else 0
    bar_len = 30
    filled = int(bar_len * done / total) if total else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    a(f"## Overall Progress")
    a(f"```")
    a(f"[{bar}] {done}/{total} features ({pct}%)")
    a(f"```")
    a("")

    # This wave
    a(f"## Wave {wave_num} — What Was Built")
    a("")
    for fd in wave_feature_details:
        status_icon = "✅" if fd["passes"] else "❌"
        a(f"### {status_icon} {fd['id']} — {fd['description']}")
        a("")

        # What it does — acceptance criteria as a readable list
        a(f"**What it does:**")
        for ac in fd["acceptance_criteria"]:
            a(f"- {ac}")
        a("")

        # Key files
        src_files = [f for f in fd["files_changed"] if not f.startswith("test") and not f.startswith(".aah")]
        test_files = [f for f in fd["files_changed"] if f.startswith("test")]
        if src_files:
            a(f"**Production code:**")
            for f in src_files[:8]:
                a(f"- `{f}`")
            if len(src_files) > 8:
                a(f"- _…and {len(src_files) - 8} more_")
            a("")
        if test_files:
            a(f"**Tests:**")
            for f in test_files[:4]:
                a(f"- `{f}`")
            a("")

        # Test case coverage
        tc_count = len(fd["test_cases"])
        tr = fd["test_results"]
        if tr:
            passed_tests = tr.get("passed", 0)
            failed_tests = tr.get("failed", 0)
            a(f"**Test results:** {passed_tests} passed / {failed_tests} failed "
              f"({tc_count} test cases defined)")
        elif tc_count:
            a(f"**Test cases defined:** {tc_count}")
        a("")

        # QA — real attempt tree + human decisions
        qa_state = qa_states.get(fd["id"], {})
        attempts = qa_state.get("attempts_total", 0)
        if attempts:
            verdict = (qa_state.get("current_verdict") or "unknown").upper()
            sha = qa_state.get("current_subject_sha")
            sha_disp = sha[:8] if isinstance(sha, str) else "none"
            line = f"**QA:** {attempts} attempt(s) — {verdict} (subject {sha_disp})"
            hs = human_states.get(fd["id"], {})
            if hs.get("decision"):
                line += f" · human: {hs['decision']}"
            a(line)
            issues = qa_results.get(fd["id"], {}).get("issues", [])
            critical = [i for i in issues if i.get("severity") == "critical"]
            if critical:
                a(f"_Issues: {', '.join(i.get('description', i.get('issue_id', '')) for i in critical[:3])}_")
        a("")

        # Reproduction / flake / cleanup — real evidence from the runner's
        # test-results/{fid}.json. All fields are read defensively.
        tr = fd["test_results"]
        if isinstance(tr, dict):
            # Flake classification (diagnostic rerun).
            diagnostic = tr.get("diagnostic")
            if isinstance(diagnostic, dict):
                classification = diagnostic.get("classification")
                if classification:
                    a(f"**Flake:** {classification}")

            # Reproduction bundle link (failure bundle manifest + sha256).
            artifacts = tr.get("artifacts")
            if isinstance(artifacts, dict):
                bundle = artifacts.get("failure_bundle")
                if isinstance(bundle, dict) and bundle.get("manifest"):
                    manifest_name = bundle.get("manifest")
                    sha = bundle.get("manifest_sha256")
                    sha_disp = sha[:12] if isinstance(sha, str) else "none"
                    bundle_dir = bundle.get("dir") or ""
                    a(f"**Reproduction:** `{manifest_name}` (sha256 {sha_disp}) — `{bundle_dir}`")

            # Cleanup state — namespace teardown (removed/leftover counts) or the
            # _stop_server shape / transport:"none" (no counts). Use .get(...) or []
            # so every shape renders cleanly.
            cleanup = tr.get("cleanup")
            if isinstance(cleanup, dict):
                stopped = cleanup.get("stopped")
                removed = cleanup.get("removed") or []
                leftover = cleanup.get("leftover") or []
                a(f"**Cleanup:** stopped={stopped} · removed={len(removed)} · leftover={len(leftover)}")

        a("---")
        a("")

    # Cumulative feature table
    a(f"## All Features — Cumulative Status")
    a("")
    a("| Wave | ID | Description | Status |")
    a("|------|----|-------------|--------|")
    for (widx, fid, desc, status) in cumulative_rows:
        short_desc = desc[:60] + "…" if len(desc) > 60 else desc
        a(f"| {widx} | {fid} | {short_desc} | {status} |")
    a("")

    # Token usage
    a(f"## Token Usage — Cumulative")
    a("")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Sessions | {sessions} |")
    a(f"| Input tokens | {_format_tokens(total_input)} |")
    a(f"| Output tokens | {_format_tokens(total_output)} |")
    a(f"| Cache writes | {_format_tokens(total_cw)} |")
    a(f"| Cache reads | {_format_tokens(total_cr)} |")
    a(f"| **Total tokens** | **{_format_tokens(total_all)}** |")
    a(f"| **Est. cost (Sonnet 4.6)** | **${est_cost:.2f}** |")
    a("")

    # Cost per feature
    features_done = len(passing_ids)
    if features_done > 0 and est_cost > 0:
        a(f"| Avg cost/feature | ${est_cost / features_done:.2f} |")
    if total_input > 0 and total_output > 0:
        a(f"| Output/input ratio | {total_output / total_input:.2f} |")
    a("")

    # Git stats
    a(f"## Git Stats")
    a(f"- Total commits on develop: {total_commits}")
    a(f"- Recent commits (last 7 days): {len(wave_commit_lines)}")
    a("")

    # Questions & Decisions from intake
    intake_path = aah_root / "intake.json"
    if intake_path.exists():
        try:
            from aah.core.intake.intake import load_intake
            intake = load_intake(intake_path)
            rounds = intake.get("rounds", [])
            if rounds:
                a("## Questions & Decisions")
                a("")
                for round_entry in rounds:
                    phase = round_entry.get("phase", "general")
                    questions = round_entry.get("questions", [])
                    shown = 0
                    for qa in questions:
                        q = qa.get("question")
                        answer = qa.get("answer")
                        if q and answer and shown < 5:
                            a(f"- **[{phase}]** {q}")
                            a(f"  {answer}")
                            shown += 1
                a("")
        except Exception:
            pass

    # Blockers encountered
    blocker_files = list((aah_root / "build").glob("*-blockers.md"))
    if blocker_files:
        a("## Blockers Encountered")
        a("")
        for bf in blocker_files[:10]:
            fid = bf.stem.replace("-blockers", "")
            try:
                content = bf.read_text(encoding='utf-8').strip().split("\n")[0][:120]
                a(f"- **{fid}**: {content}")
            except Exception:
                a(f"- **{fid}**: (could not read)")
        a("")

    # Known issues
    known_issues = _read_json(aah_root / "claude-progress.json").get("known_issues", [])
    if known_issues:
        a("## Known Issues")
        a("")
        for issue in known_issues[:5]:
            a(f"- {issue}")
        a("")

    # Remaining waves — only show waves that still have pending features
    remaining = [
        (widx, [fid for fid in wfids if fid not in passing_ids])
        for widx, wfids in enumerate(waves[wave_num + 1:], start=wave_num + 1)
    ]
    remaining = [(widx, fids) for widx, fids in remaining if fids]

    if remaining:
        a(f"## Remaining Work")
        remaining_count = sum(len(fids) for _, fids in remaining)
        a(f"_{remaining_count} features across {len(remaining)} wave(s)_")
        a("")
        for widx, fids in remaining:
            descs = [f"{fid} — {by_id.get(fid, {}).get('description', '')[:55]}" for fid in fids]
            a(f"**Wave {widx}**")
            for d in descs:
                a(f"- {d}")
            a("")
    else:
        a(f"## 🎉 All waves complete!")
        a("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate wave completion summary")
    parser.add_argument("--wave", type=int, required=True)
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)
    aah_root = project_path / ".aah"

    summary = generate(project_path, args.wave)

    # Save to file
    out_dir = aah_root / "build" / "wave-summaries"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"wave-{args.wave}-summary.md"
    out_file.write_text(summary, encoding="utf-8")
    print(f"Summary saved to {out_file}", file=sys.stderr)

    # Print to stdout so it surfaces in the main conversation
    print(summary)


if __name__ == "__main__":
    main()
