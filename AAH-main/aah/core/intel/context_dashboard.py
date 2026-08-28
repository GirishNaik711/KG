#!/usr/bin/env python3
"""
Context dashboard: summarize all 3 layers of the RAPIDS knowledge system.

Layer 1 (Knowledge): Project docs parsed via LiteParse
Layer 2 (Codebase Intelligence): Profiler artifacts + knowledge graph
Layer 3 (Session): Progress, phase, wave, feature state, session history

Supports both greenfield and brownfield projects.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import read_json
from aah.core.common.progress import load_progress
from aah.core.intel.check_intel_update import (
    _find_intel_dir,
    _find_intel_artifact,
    _read_pending_changes,
    staleness_check,
)


def get_layer1_status(project_path: Path) -> dict:
    """Layer 1: Knowledge base (project docs)."""
    try:
        from aah.core.knowledge.parser import get_knowledge_index
        index = get_knowledge_index(project_path)
        return {
            "layer": "knowledge",
            "found": index.get("found", False),
            "folder": index.get("folder"),
            "document_count": index.get("document_count", 0),
            "documents": index.get("documents", []),
        }
    except Exception:
        return {"layer": "knowledge", "found": False, "folder": None, "document_count": 0}


def get_layer2_status(project_path: Path) -> dict:
    """Layer 2: Codebase intelligence (profiler + optional knowledge graph)."""
    intel_dir = _find_intel_dir(project_path)
    artifact = _find_intel_artifact(project_path)

    result = {
        "layer": "codebase_intelligence",
        "found": artifact is not None,
        "intel_dir": str(intel_dir) if intel_dir else None,
        "primary_artifact": str(artifact) if artifact else None,
        "artifacts": [],
        "staleness": staleness_check(project_path),
    }

    if intel_dir and intel_dir.exists():
        for f in sorted(intel_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                age_days = (datetime.now(timezone.utc).timestamp() - f.stat().st_mtime) / 86400
                result["artifacts"].append({
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "age_days": round(age_days, 1),
                })

    # Check for knowledge graph artifacts
    graph_report = None
    for d in [project_path / ".aah" / "codebase-intel", project_path / ".aah" / "brownfield"]:
        candidate = d / "CODEBASE_GRAPH_REPORT.md"
        if candidate.exists():
            graph_report = str(candidate)
            break

    result["knowledge_graph"] = {
        "found": graph_report is not None,
        "report_path": graph_report,
    }

    # Also check brownfield dir if intel_dir is codebase-intel
    brownfield_dir = project_path / ".aah" / "brownfield"
    if brownfield_dir.exists() and brownfield_dir != intel_dir:
        bf_artifacts = []
        for f in sorted(brownfield_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                bf_artifacts.append(f.name)
        if bf_artifacts:
            result["brownfield_artifacts"] = bf_artifacts

    return result


def get_layer3_status(project_path: Path) -> dict:
    """Layer 3: Session intelligence (progress, features, waves)."""
    rapids_path = project_path / ".aah"
    progress_path = rapids_path / "claude-progress.json"
    progress = load_progress(progress_path if progress_path.exists() else None)

    session_history = progress.get("session_history", [])

    result = {
        "layer": "session",
        "phase": progress.get("current_phase", "unknown"),
        "wave": progress.get("current_wave"),
        "environment": progress.get("environment_state", "unknown"),
        "known_issues": len(progress.get("known_issues", [])),
        "in_progress_features": progress.get("in_progress_features", []),
        "session_count": len(session_history),
    }

    # Feature progress
    fl_path = rapids_path / "feature-list.json"
    if fl_path.exists():
        try:
            from aah.core.common.feature_list import get_progress_summary, load_feature_list
            fl_data = load_feature_list(fl_path)
            result["feature_progress"] = get_progress_summary(fl_data)
        except Exception:
            pass

    # Session knowledge stats
    total_decisions = 0
    total_patterns = 0
    for entry in session_history:
        total_decisions += len(entry.get("decisions_made", []))
        total_patterns += len(entry.get("patterns_discovered", []))

    result["accumulated_decisions"] = total_decisions
    result["accumulated_patterns"] = total_patterns

    return result


def build_dashboard(project_path: Path) -> dict:
    """Build the full 3-layer dashboard."""
    return {
        "project_path": str(project_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "layer1_knowledge": get_layer1_status(project_path),
        "layer2_codebase_intel": get_layer2_status(project_path),
        "layer3_session": get_layer3_status(project_path),
    }


def render_dashboard_markdown(dashboard: dict) -> str:
    """Render dashboard as human-readable markdown."""
    lines = ["# RAPIDS Context Health Dashboard", ""]

    # Layer 1
    l1 = dashboard["layer1_knowledge"]
    lines.append("## Layer 1: Knowledge Base (Project Docs)")
    if l1["found"]:
        lines.append(f"- Folder: `{l1['folder']}`")
        lines.append(f"- Documents: {l1['document_count']}")
        for doc in l1.get("documents", [])[:10]:
            status = "parsed" if doc.get("parsed") else f"error: {doc.get('error', 'unknown')}"
            cached = " (cached)" if doc.get("cached") else ""
            lines.append(f"  - {doc['name']}: {status}{cached}")
    else:
        lines.append("- Not found. Add a `knowledge/` folder with project docs.")
    lines.append("")

    # Layer 2
    l2 = dashboard["layer2_codebase_intel"]
    lines.append("## Layer 2: Codebase Intelligence")
    if l2["found"]:
        lines.append(f"- Directory: `{l2['intel_dir']}`")
        staleness = l2.get("staleness", {})
        stale_label = "STALE" if staleness.get("stale") else "fresh"
        lines.append(f"- Status: {stale_label} ({staleness.get('commits_behind', 0)} commits behind, {staleness.get('pending_changes', 0)} pending changes)")
        for art in l2.get("artifacts", []):
            lines.append(f"  - {art['name']}: {art['size_bytes']} bytes, {art['age_days']}d old")
        kg = l2.get("knowledge_graph", {})
        if kg.get("found"):
            lines.append(f"- Knowledge Graph: available at `{kg['report_path']}`")
        if l2.get("brownfield_artifacts"):
            lines.append(f"- Brownfield artifacts: {', '.join(l2['brownfield_artifacts'])}")
    else:
        lines.append("- No codebase intelligence found. Run `/rapids-codebase-profile` to generate.")
    lines.append("")

    # Layer 3
    l3 = dashboard["layer3_session"]
    lines.append("## Layer 3: Session Intelligence")
    lines.append(f"- Phase: {l3['phase']}")
    if l3.get("wave") is not None:
        lines.append(f"- Wave: {l3['wave']}")
    lines.append(f"- Environment: {l3['environment']}")
    lines.append(f"- Sessions: {l3['session_count']}")
    lines.append(f"- Known issues: {l3['known_issues']}")
    if l3.get("in_progress_features"):
        lines.append(f"- In progress: {', '.join(l3['in_progress_features'])}")
    fp = l3.get("feature_progress")
    if fp and fp.get("total", 0) > 0:
        lines.append(f"- Features: {fp.get('passing', 0)}/{fp['total']} passing ({fp.get('completion_pct', 0)}%)")
    lines.append(f"- Accumulated decisions: {l3.get('accumulated_decisions', 0)}")
    lines.append(f"- Accumulated patterns: {l3.get('accumulated_patterns', 0)}")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS context dashboard")
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()

    from aah.core.common.config import resolve_project_path
    project_path = resolve_project_path(args.project_path)

    if project_path is None:
        print("Error: no RAPIDS project found", file=sys.stderr)
        sys.exit(1)

    dashboard = build_dashboard(project_path)

    if args.format == "json":
        json.dump(dashboard, sys.stdout, indent=2)
        print()
    else:
        print(render_dashboard_markdown(dashboard))

    sys.exit(0)


if __name__ == "__main__":
    main()
