#!/usr/bin/env python3
"""
RAPIDS Demo Phase Presenter.

Renders a single phase's execution results in a rich terminal UI.
Shows phase summary, activities executed, artifacts produced, key findings,
and allows interactive browsing of artifact files.

Modes:
    full  — (default) Full phase review with activities, stats, artifact browser
    intro — Renders only the phase header + summary panel, then exits.
            Used by worktree demo to show previous phase context before execution.

Usage:
    python3 demo-phase-presenter.py <project-path> <phase>
    python3 demo-phase-presenter.py <project-path> <phase> --mode intro

Dependencies:
    pip install rich

Examples:
    python3 demo-phase-presenter.py .rapids/projects/my-project research
    python3 demo-phase-presenter.py .rapids/projects/my-project plan
    python3 demo-phase-presenter.py .rapids/projects/my-project research --mode intro
    python3 demo-phase-presenter.py .rapids/projects/my-project onboarding --mode intro
"""

import json
import os
import re
import sys
from datetime import datetime

try:
    import yaml as _yaml
except ImportError:
    _yaml = None

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree
    from rich import box
except ImportError:
    print("Error: 'rich' library is required. Install with: pip install rich")
    sys.exit(1)

PHASE_ORDER = ["research", "analysis", "plan", "implement", "deploy", "sustain"]

RAPIDS_BANNER = """
╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║   ██████╗  █████╗ ██████╗ ██╗██████╗ ███████╗                                 ║
║   ██╔══██╗██╔══██╗██╔══██╗██║██╔══██╗██╔════╝                                 ║
║   ██████╔╝███████║██████╔╝██║██║  ██║███████╗                                 ║
║   ██╔══██╗██╔══██║██╔═══╝ ██║██║  ██║╚════██║                                 ║
║   ██║  ██║██║  ██║██║     ██║██████╔╝███████║                                 ║
║   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═════╝ ╚══════╝                                 ║
║                                                                               ║
║   Robust AI Project Implementation & Deployment System  v1.0                  ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
""".strip()
PHASE_LABELS = {
    "research": "R — Research",
    "analysis": "A — Analysis",
    "plan": "P — Plan",
    "implement": "I — Implement",
    "deploy": "D — Deploy",
    "sustain": "S — Sustain",
}

PHASE_OBJECTIVES = {
    "research": (
        "Investigate constraints, prior art, and stakeholder needs.\n"
        "Produces the evidence base that guides all subsequent design decisions."
    ),
    "analysis": (
        "Translate research findings into a concrete system architecture.\n"
        "Defines integration design, data models, and key architectural decisions."
    ),
    "plan": (
        "Break the architecture into a prioritised, wave-ordered feature list.\n"
        "Produces technical specs, DAG, and sprint contracts ready for implementation."
    ),
    "implement": (
        "Build and test each feature wave in isolated git worktrees.\n"
        "Every feature is implemented, regression-tested, and merged before moving on."
    ),
    "deploy": (
        "Generate IaC, Dockerfiles, and CI/CD pipelines from architecture decisions.\n"
        "Validates deployment artefacts work end-to-end in the target environment."
    ),
    "sustain": (
        "Embed observability — logging, metrics, tracing — and write runbooks.\n"
        "Leaves the system in a fully operational, on-call-ready state."
    ),
}

ONBOARDING_ARTIFACT_PATHS = {
    ".rapids/intake.json":        "Project Intake",
    ".rapids/manifest.yaml":      "Project Manifest",
    ".rapids/activity-plan.yaml": "Activity Plan",
    ".rapids/phase-plan.yaml":    "Phase Plan",
    "knowledge/project-brief.md": "Project Brief",
}

ARTIFACT_LABELS = {
    "research/recommendation-brief.md": "Recommendation Brief",
    "research/prior-art-analysis.md": "Prior Art Analysis",
    "research/stakeholder-analysis.md": "Stakeholder Analysis",
    "research/ai-framework-evaluation.md": "AI Framework Evaluation",
    "research/llm-provider-evaluation.md": "LLM Provider Evaluation",
    "analysis/solution-integration.md": "Solution Integration Map",
    "analysis/agent-architecture.md": "Agent Architecture",
    "analysis/architecture-decisions.md": "Architecture Decisions",
    "analysis/orchestration-design.md": "Orchestration Design",
    "analysis/tool-integration-spec.md": "Tool Integration Spec",
    "analysis/data-model.md": "Data Model",
    "analysis/nfr-analysis.md": "NFR Analysis",
    "plan/feature-list.json": "Feature List",
    "plan/specs": "Technical Specifications",
    "plan/sprint-contracts": "Sprint Contracts",
    "implement/test-results": "Test Results",
}
PHASE_COLORS = {
    "research": "cyan",
    "analysis": "magenta",
    "plan": "yellow",
    "implement": "green",
    "deploy": "blue",
    "sustain": "red",
}
STATUS_ICONS = {
    "complete": "[bold green]\u2713[/bold green]",
    "in_progress": "[bold yellow]\u25b6[/bold yellow]",
    "not_started": "[dim]\u2500[/dim]",
}


def load_json(path):
    """Load a JSON file, returning None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_text(path):
    """Load a text file, returning empty string on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def parse_phase_summary(phase_summary_text, phase_name):
    """Extract a specific phase's section from phase_summary.md."""
    phase_index = PHASE_ORDER.index(phase_name) + 1
    phase_header_pattern = rf"^## {phase_index}\.\s"
    next_phase_pattern = rf"^## {phase_index + 1}\.\s" if phase_index < 6 else None

    lines = phase_summary_text.split("\n")
    capturing = False
    phase_lines = []
    activity_sections = []
    current_activity = None

    for line in lines:
        if re.match(phase_header_pattern, line):
            capturing = True
            continue
        if capturing and next_phase_pattern and re.match(next_phase_pattern, line):
            break
        if capturing and line.startswith("---"):
            break
        if capturing:
            activity_match = re.match(
                rf"^### {phase_index}\.(\d+)\s+(.+)$", line
            )
            if activity_match:
                if current_activity:
                    activity_sections.append(current_activity)
                current_activity = {
                    "number": activity_match.group(1),
                    "name": activity_match.group(2),
                    "lines": [],
                }
                continue
            if current_activity:
                current_activity["lines"].append(line)
            else:
                phase_lines.append(line)

    if current_activity:
        activity_sections.append(current_activity)

    # Extract phase-level summary
    phase_summary = ""
    for line in phase_lines:
        if line.startswith("**Summary:**"):
            phase_summary = line.replace("**Summary:**", "").strip()
            break

    # If no explicit summary line, take the first substantive paragraph
    if not phase_summary:
        for line in phase_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("**Phase") and not stripped.startswith("**Activity") and not stripped.startswith("**Key"):
                phase_summary = stripped
                break

    # Extract activity summaries
    activities = []
    for act in activity_sections:
        summary = ""
        for line in act["lines"]:
            if line.startswith("**Summary:**"):
                summary = line.replace("**Summary:**", "").strip()
                break
        if not summary:
            # Fallback: take first non-empty line
            for line in act["lines"]:
                if line.strip():
                    summary = line.strip()
                    break
        activities.append(
            {"number": act["number"], "name": act["name"], "summary": summary}
        )

    return phase_summary, activities


def scan_artifacts(artifacts_dir, phase_name):
    """Scan artifact directories for a phase, collecting metadata and file lists."""
    phase_dir = os.path.join(artifacts_dir, phase_name)
    if not os.path.isdir(phase_dir):
        return []

    activities = []
    for entry in sorted(os.listdir(phase_dir)):
        activity_dir = os.path.join(phase_dir, entry)
        if not os.path.isdir(activity_dir):
            continue

        metadata = load_json(os.path.join(activity_dir, "_metadata.json"))
        files = []
        for f in sorted(os.listdir(activity_dir)):
            if f == "_metadata.json":
                continue
            fpath = os.path.join(activity_dir, f)
            if os.path.isfile(fpath):
                size = os.path.getsize(fpath)
                files.append({"name": f, "path": fpath, "size": size})

        activities.append(
            {
                "dir_name": entry,
                "path": activity_dir,
                "metadata": metadata,
                "files": files,
            }
        )

    return activities


def format_size(size_bytes):
    """Format byte size to human readable."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def format_duration(started_at, completed_at):
    """Calculate duration between two ISO timestamps."""
    if not started_at or not completed_at:
        return "N/A"
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(started_at, fmt)
        end = datetime.strptime(completed_at, fmt)
        delta = end - start
        minutes = int(delta.total_seconds() / 60)
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        remaining = minutes % 60
        return f"{hours}h {remaining}m"
    except (ValueError, TypeError):
        return "N/A"


def render_phase(console, project_path, phase_name):
    """Render the main phase overview."""
    project_name = os.path.basename(project_path)
    color = PHASE_COLORS.get(phase_name, "white")
    phase_label = PHASE_LABELS.get(phase_name, phase_name.title())
    phase_num = PHASE_ORDER.index(phase_name) + 1

    # Load project data
    state = load_json(os.path.join(project_path, "state.json"))
    phase_summary_text = load_text(os.path.join(project_path, "phase_summary.md"))
    artifacts_dir = os.path.join(project_path, "artifacts")

    if not state:
        console.print(f"[red]Error: Could not load state.json from {project_path}[/red]")
        return []

    phase_status = state.get("phase_status", {}).get(phase_name, {})
    status = phase_status.get("status", "not_started")
    started_at = phase_status.get("started_at")
    completed_at = phase_status.get("completed_at")
    blocks_total = phase_status.get("blocks_total", 0)
    blocks_completed = phase_status.get("blocks_completed", 0)
    depth = phase_status.get("depth", state.get("depth_level", "N/A"))

    # Header
    total_phases_with_data = sum(
        1
        for p in PHASE_ORDER
        if state.get("phase_status", {}).get(p, {}).get("status") != "not_started"
    )
    header_text = Text()
    header_text.append(f"  RAPIDS Demo \u2014 {project_name}\n", style=f"bold {color}")
    header_text.append(
        f"  Phase {phase_num}: {phase_label.upper()}",
        style=f"bold {color}",
    )
    console.print(
        Panel(
            header_text,
            box=box.DOUBLE,
            border_style=color,
            padding=(1, 2),
        )
    )
    console.print()

    # Phase summary
    phase_summary, activity_summaries = parse_phase_summary(
        phase_summary_text, phase_name
    )
    if phase_summary:
        # Truncate long summaries for display
        display_summary = phase_summary
        if len(display_summary) > 500:
            display_summary = display_summary[:497] + "..."
        console.print(
            Panel(
                Text(display_summary, style="white"),
                title="[bold]Phase Summary[/bold]",
                border_style=color,
                padding=(1, 2),
            )
        )
        console.print()

    # Scan artifacts
    artifact_activities = scan_artifacts(artifacts_dir, phase_name)

    # Activities table
    # Merge artifact data with summary data
    all_files = []  # Flat list for interactive selection
    file_index = 1

    if artifact_activities:
        console.print(
            f"  [bold {color}]Activities Executed ({len(artifact_activities)})[/bold {color}]"
        )
        console.print(f"  [dim]{'─' * 60}[/dim]")
        console.print()

        for i, act in enumerate(artifact_activities, 1):
            meta = act["metadata"]
            dir_name = act["dir_name"]

            # Activity name from metadata or directory name
            act_name = dir_name.replace("-", " ").title()
            agent = "N/A"
            if meta:
                act_name = meta.get("block_name", act_name)
                agent = meta.get("executed_by", "N/A")

            # Find matching summary
            act_summary = ""
            for s in activity_summaries:
                if s["name"].strip().lower().startswith(act_name.lower()[:20]):
                    act_summary = s["summary"]
                    break

            # Display activity header
            status_icon = STATUS_ICONS.get("complete", "\u2713")
            if meta and meta.get("status") != "complete":
                status_icon = STATUS_ICONS.get(
                    meta.get("status", "complete"), "\u2713"
                )
            console.print(
                f"  {status_icon} [bold]{i}. {act_name}[/bold]"
            )
            console.print(f"     [dim]Agent:[/dim] {agent}")

            # Brief summary (first 200 chars)
            if act_summary:
                brief = act_summary[:200] + ("..." if len(act_summary) > 200 else "")
                console.print(f"     [dim]Summary:[/dim] {brief}")

            # Key findings from metadata
            if meta and meta.get("key_findings"):
                raw_findings = meta["key_findings"]
                if isinstance(raw_findings, list):
                    findings = raw_findings[:3]
                elif isinstance(raw_findings, dict):
                    findings = [
                        f"{k}: {', '.join(str(x) for x in v)}" if isinstance(v, list)
                        else f"{k}: {v}"
                        for k, v in list(raw_findings.items())[:3]
                    ]
                else:
                    findings = []
                if findings:
                    console.print(f"     [dim]Key Findings:[/dim]")
                    for finding in findings:
                        finding_str = str(finding)
                        brief_finding = finding_str[:120] + ("..." if len(finding_str) > 120 else "")
                        console.print(f"       [dim]\u2022[/dim] {brief_finding}")

            # Artifact file tree
            if act["files"]:
                console.print(f"     [dim]Artifacts:[/dim]")
                for j, f in enumerate(act["files"]):
                    is_last = j == len(act["files"]) - 1
                    connector = "\u2514\u2500\u2500" if is_last else "\u251c\u2500\u2500"
                    size_str = format_size(f["size"])
                    console.print(
                        f"       {connector} [bold cyan][{file_index}][/bold cyan] {f['name']} [dim]({size_str})[/dim]"
                    )
                    all_files.append(
                        {"index": file_index, "name": f["name"], "path": f["path"]}
                    )
                    file_index += 1

            console.print()

    # Phase stats
    stats_table = Table(
        show_header=False,
        box=None,
        padding=(0, 2),
        show_edge=False,
    )
    stats_table.add_column("Key", style="dim", width=16)
    stats_table.add_column("Value", style="white")

    status_style = {
        "complete": "bold green",
        "in_progress": "bold yellow",
        "not_started": "dim",
    }.get(status, "white")

    stats_table.add_row("Status", f"[{status_style}]{status.replace('_', ' ').title()}[/{status_style}]")
    stats_table.add_row(
        "Activities",
        f"{blocks_completed} of {blocks_total} {'selected' if blocks_total else ''}",
    )
    stats_table.add_row("Depth", str(depth).title())
    if started_at:
        try:
            start_dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
            stats_table.add_row("Started", start_dt.strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            stats_table.add_row("Started", started_at)
    duration = format_duration(started_at, completed_at)
    if duration != "N/A":
        stats_table.add_row("Duration", duration)

    # Count total artifacts
    total_files = sum(len(a["files"]) for a in artifact_activities)
    total_size = sum(
        f["size"] for a in artifact_activities for f in a["files"]
    )
    if total_files > 0:
        stats_table.add_row(
            "Artifacts", f"{total_files} files ({format_size(total_size)})"
        )

    console.print(
        Panel(stats_table, title="[bold]Phase Stats[/bold]", border_style=color, padding=(1, 2))
    )
    console.print()

    return all_files


def render_artifact(console, file_info, color):
    """Render an artifact file's contents."""
    path = file_info["path"]
    name = file_info["name"]

    content = load_text(path)
    if not content:
        console.print(f"  [dim]File is empty or could not be read: {path}[/dim]")
        return

    # Determine rendering mode
    if name.endswith(".json"):
        # Pretty-print JSON
        try:
            parsed = json.loads(content)
            formatted = json.dumps(parsed, indent=2)
            console.print(
                Panel(
                    Text(formatted),
                    title=f"[bold]{name}[/bold]",
                    border_style=color,
                    padding=(1, 2),
                )
            )
        except json.JSONDecodeError:
            console.print(
                Panel(
                    Text(content),
                    title=f"[bold]{name}[/bold]",
                    border_style=color,
                    padding=(1, 2),
                )
            )
    elif name.endswith(".md"):
        # Render markdown
        console.print(
            Panel(
                Markdown(content),
                title=f"[bold]{name}[/bold]",
                border_style=color,
                padding=(1, 2),
            )
        )
    else:
        # Plain text
        console.print(
            Panel(
                Text(content),
                title=f"[bold]{name}[/bold]",
                border_style=color,
                padding=(1, 2),
            )
        )


def _load_manifest(worktree_path):
    """Load .rapids/manifest.yaml from the worktree, returning {} on failure."""
    if _yaml is None:
        return {}
    manifest_path = os.path.join(worktree_path, ".rapids", "manifest.yaml")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return _yaml.safe_load(f) or {}
    except Exception:
        return {}


def _artifacts_for_phase(manifest, phase_name):
    """Return display names of artifacts completed in a given phase."""
    completed = manifest.get("completed_artifacts", []) or []
    labels = []
    for a in [x for x in completed if x.startswith(f"{phase_name}/")]:
        label = ARTIFACT_LABELS.get(a)
        if label:
            labels.append(label)
        else:
            basename = os.path.splitext(os.path.basename(a))[0]
            labels.append(basename.replace("-", " ").title())
    return labels


def _scan_rapids_dir(worktree_path, phase_name):
    """Scan .rapids/{phase_name}/ for artifact files when manifest completed_artifacts is empty."""
    phase_dir = os.path.join(worktree_path, ".rapids", phase_name)
    if not os.path.isdir(phase_dir):
        return []
    skip = {"_metadata.json", ".gitkeep", ".gitignore"}
    names = []
    for entry in sorted(os.listdir(phase_dir)):
        if entry in skip or entry.startswith("."):
            continue
        key = f"{phase_name}/{entry}"
        label = ARTIFACT_LABELS.get(key)
        if label:
            names.append(label)
        else:
            base = os.path.splitext(entry)[0]
            names.append(base.replace("-", " ").title())
    return names


def _onboarding_artifacts(worktree_path):
    """Return display labels for onboarding artifacts that exist in the worktree."""
    labels = []
    for rel_path, label in ONBOARDING_ARTIFACT_PATHS.items():
        if os.path.exists(os.path.join(worktree_path, rel_path)):
            labels.append(label)
    return labels


def _next_phase_after(phase_name):
    """Return the phase that follows phase_name in RAPIDS order."""
    try:
        idx = PHASE_ORDER.index(phase_name)
        return PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else None
    except ValueError:
        return None


def render_intro(console, project_path, phase_name, next_phase=None, worktree_path=None):
    """Render intro banner: RAPIDS logo + previous phase artifacts + next phase objective."""
    project_name = os.path.basename(project_path)
    wt_path = worktree_path or project_path

    # Special case: "onboarding" — behaviour depends on whether this IS the onboarding tab
    # or the research tab (which calls render_intro with phase_name="onboarding" + next_phase set)
    if phase_name == "onboarding":
        if not next_phase:
            # Actual onboarding tab — show RAPIDS banner + welcome panel
            console.print(Text(RAPIDS_BANNER, style="bold cyan"))
            console.print()
            console.print(
                Panel(
                    Text(
                        "Welcome to the RAPIDS Demo.\n\n"
                        "This terminal demonstrates the Onboarding phase:\n"
                        "  1. /rapids-init-workspace demo-workspace\n"
                        "  2. /rapids-new-project demo-app --stack python-fastapi\n"
                        "  3. /rapids-research\n\n"
                        "Claude Code will launch next and walk through workspace initialisation.",
                        style="white",
                    ),
                    title="[bold cyan]Onboarding[/bold cyan]",
                    border_style="cyan",
                    padding=(1, 2),
                )
            )
            console.print()
        if next_phase and next_phase in PHASE_OBJECTIVES:
            # Previous phase header
            header_text = Text()
            header_text.append(f"  RAPIDS Demo — {project_name}\n", style="bold cyan")
            header_text.append("  Previous Phase: ONBOARDING", style="bold cyan")
            console.print(Panel(header_text, box=box.DOUBLE, border_style="cyan", padding=(1, 2)))
            console.print()

            # Artifacts produced by onboarding
            artifact_names = _onboarding_artifacts(wt_path)
            if artifact_names:
                items_per_line = max(1, (len(artifact_names) + 2) // 3)
                artifact_text = Text()
                for i in range(0, len(artifact_names), items_per_line):
                    chunk = artifact_names[i : i + items_per_line]
                    artifact_text.append("  ✓ " + "  ·  ".join(chunk) + "\n", style="green")
                console.print(
                    Panel(
                        artifact_text,
                        title="[bold cyan]Artifacts Produced[/bold cyan]",
                        border_style="cyan",
                        padding=(1, 2),
                    )
                )
                console.print()

            # Next phase objective
            next_label = PHASE_LABELS.get(next_phase, next_phase.title())
            next_color = PHASE_COLORS.get(next_phase, "white")
            console.print(
                Panel(
                    Text(PHASE_OBJECTIVES[next_phase], style="bold white"),
                    title=f"[bold {next_color}]▶  {next_label.upper()} — OBJECTIVE[/bold {next_color}]",
                    border_style=next_color,
                    box=box.DOUBLE,
                    padding=(1, 2),
                )
            )
            console.print()
        return

    color = PHASE_COLORS.get(phase_name, "white")
    phase_label = PHASE_LABELS.get(phase_name, phase_name.title())

    # Header — previous phase
    header_text = Text()
    header_text.append(f"  RAPIDS Demo — {project_name}\n", style=f"bold {color}")
    header_text.append(f"  Previous Phase: {phase_label.upper()}", style=f"bold {color}")
    console.print(Panel(header_text, box=box.DOUBLE, border_style=color, padding=(1, 2)))
    console.print()

    # Artifacts produced in the previous phase
    manifest = _load_manifest(wt_path)
    artifact_names = _artifacts_for_phase(manifest, phase_name)
    if not artifact_names:
        artifact_names = _scan_rapids_dir(wt_path, phase_name)
    if artifact_names:
        items_per_line = max(1, (len(artifact_names) + 2) // 3)
        artifact_text = Text()
        for i in range(0, len(artifact_names), items_per_line):
            chunk = artifact_names[i : i + items_per_line]
            line = "  ✓ " + "  ·  ".join(chunk)
            artifact_text.append(line + "\n", style="green")
        console.print(
            Panel(
                artifact_text,
                title=f"[bold {color}]Artifacts Produced[/bold {color}]",
                border_style=color,
                padding=(1, 2),
            )
        )
        console.print()

    # Next phase objective
    target_phase = next_phase or _next_phase_after(phase_name)
    if target_phase and target_phase in PHASE_OBJECTIVES:
        next_label = PHASE_LABELS.get(target_phase, target_phase.title())
        next_color = PHASE_COLORS.get(target_phase, "white")
        console.print(
            Panel(
                Text(PHASE_OBJECTIVES[target_phase], style="bold white"),
                title=f"[bold {next_color}]▶  {next_label.upper()} — OBJECTIVE[/bold {next_color}]",
                border_style=next_color,
                box=box.DOUBLE,
                padding=(1, 2),
            )
        )
        console.print()

def interactive_loop(console, all_files, phase_name):
    """Interactive loop for browsing artifacts."""
    color = PHASE_COLORS.get(phase_name, "white")

    if not all_files:
        console.print(f"  [dim]No artifacts to browse for this phase.[/dim]")
        console.print()
        console.print(f"  [dim]Press Enter to exit.[/dim]")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return

    while True:
        console.print(
            f"  [{color}]Enter artifact number to view, 'list' to re-list, or 'q' to exit:[/{color}] ",
            end="",
        )
        try:
            choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice.lower() in ("q", "quit", "exit"):
            break
        elif choice.lower() in ("list", "ls", "l"):
            console.print()
            for f in all_files:
                console.print(
                    f"  [bold cyan][{f['index']}][/bold cyan] {f['name']}"
                )
            console.print()
        elif choice.isdigit():
            idx = int(choice)
            matched = [f for f in all_files if f["index"] == idx]
            if matched:
                console.print()
                render_artifact(console, matched[0], color)
                console.print()
            else:
                console.print(f"  [red]No artifact with index {idx}[/red]")
        else:
            console.print(f"  [dim]Enter a number, 'list', or 'q'[/dim]")


def parse_args():
    """Parse command-line arguments."""
    args = sys.argv[1:]
    mode = "full"
    worktree_path = None
    next_phase = None
    positional = []

    i = 0
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1].lower()
            i += 2
        elif args[i] == "--worktree-path" and i + 1 < len(args):
            worktree_path = args[i + 1]
            i += 2
        elif args[i] == "--next-phase" and i + 1 < len(args):
            next_phase = args[i + 1].lower()
            i += 2
        else:
            positional.append(args[i])
            i += 1

    return positional, mode, worktree_path, next_phase

def main():
    positional, mode, worktree_path, next_phase = parse_args()

    if len(positional) < 2:
        print("Usage: demo-phase-presenter.py <project-path> <phase> [--mode full|intro] [--worktree-path PATH] [--next-phase PHASE]")
        print("Phases: onboarding, research, analysis, plan, implement, deploy, sustain")
        sys.exit(1)

    project_path = positional[0]
    phase_name = positional[1].lower()

    valid_phases = ["onboarding"] + PHASE_ORDER
    if phase_name not in valid_phases:
        print(f"Error: Unknown phase '{phase_name}'.")
        print(f"Valid phases: {', '.join(valid_phases)}")
        sys.exit(1)

    if mode not in ("full", "intro"):
        print(f"Error: Unknown mode '{mode}'. Use 'full' or 'intro'.")
        sys.exit(1)

    if not os.path.isdir(project_path):
        print(f"Error: Project path not found: {project_path}")
        sys.exit(1)

    # "onboarding" phase only valid in intro mode
    if phase_name == "onboarding" and mode != "intro":
        print("Error: 'onboarding' phase is only valid with --mode intro")
        sys.exit(1)

    console = Console()

    if mode == "intro":
        render_intro(console, project_path, phase_name,
                     next_phase=next_phase, worktree_path=worktree_path)
    else:
        console.clear()
        all_files = render_phase(console, project_path, phase_name)
        interactive_loop(console, all_files, phase_name)


if __name__ == "__main__":
    main()
