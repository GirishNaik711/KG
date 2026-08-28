#!/usr/bin/env python3
"""
RAPIDS Demo Tag Discovery.

Discovers git tags that contain valid RAPIDS state.json files and classifies
them by which phase they enable for demo.  Format-agnostic: works with ANY
tag naming convention as long as the tagged commit contains a parseable
state.json with current_phase and phase_status fields.

Usage:
    python3 demo-tag-discovery.py <project-path> [--state-path <custom/state.json>]

Output:
    JSON object with discovered tags grouped by demo phase.

Examples:
    python3 demo-tag-discovery.py .rapids/projects/banking-rag
    python3 demo-tag-discovery.py .rapids/projects/banking-rag --state-path "custom/path/state.json"
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

PHASE_ORDER = ["research", "analysis", "plan", "implement", "deploy", "sustain"]


def find_git_root(cwd=None):
    """Find the git repository root directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
            cwd=cwd
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def run_git(args, cwd=None, check=True):
    """Run a git command and return (returncode, stdout, stderr)."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, "", "git command failed"


def _manifest_to_state(manifest):
    """Convert a manifest.yaml dict into the state dict format expected by classify_tags."""
    current = manifest.get("current_phase")
    if not current:
        return None
    artifacts = manifest.get("completed_artifacts", [])

    # Determine which phases have completed artifacts
    phases_with_artifacts = set()
    for art in artifacts:
        prefix = art.split("/")[0] if "/" in art else None
        if prefix in PHASE_ORDER:
            phases_with_artifacts.add(prefix)

    current_idx = PHASE_ORDER.index(current) if current in PHASE_ORDER else -1
    phase_status = {}
    for i, phase in enumerate(PHASE_ORDER):
        if i < current_idx:
            phase_status[phase] = {"status": "complete"}
        elif i == current_idx:
            # If the current phase already has artifacts, it's complete
            if phase in phases_with_artifacts:
                phase_status[phase] = {"status": "complete"}
            else:
                phase_status[phase] = {"status": "in_progress"}
        else:
            phase_status[phase] = {"status": "not_started"}

    return {
        "project_name": manifest.get("project_name"),
        "current_phase": current,
        "phase_status": phase_status,
    }


def _read_state_from_tag(git_root, tag, state_paths, manifest_paths):
    """Try to read manifest.yaml or state.json from a tagged commit.

    Returns (tag, state_dict) or (tag, None) if no valid state found.
    """
    # Try manifest.yaml first (harness primary)
    for mpath in manifest_paths:
        rc, out, _ = run_git(
            ["show", f"{tag}:{mpath}"],
            cwd=git_root, check=False
        )
        if rc == 0 and out.strip():
            try:
                manifest = yaml.safe_load(out)
                if manifest and isinstance(manifest, dict):
                    state = _manifest_to_state(manifest)
                    if state:
                        return tag, state
            except yaml.YAMLError:
                continue

    # Fall back to state.json (legacy)
    for spath in state_paths:
        rc, out, _ = run_git(
            ["show", f"{tag}:{spath}"],
            cwd=git_root, check=False
        )
        if rc == 0 and out.strip():
            try:
                return tag, json.loads(out)
            except json.JSONDecodeError:
                continue

    return tag, None


def _transition_key(current_phase):
    """Build the transition_responses key for a phase."""
    idx = PHASE_ORDER.index(current_phase) if current_phase in PHASE_ORDER else -1
    if idx <= 0:
        return None
    prev = PHASE_ORDER[idx - 1]
    return f"{prev}_to_{current_phase}"


def classify_tags(git_root, project_name, tags, state_paths, manifest_paths):
    """Read state.json or manifest.yaml from each tag and classify by phase state.

    Uses concurrent reads for performance with many tags.
    """
    tag_states = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_read_state_from_tag, git_root, tag, state_paths, manifest_paths): tag
            for tag in tags
        }
        for future in as_completed(futures):
            tag, state = future.result()
            if not state:
                continue

            current_phase = state.get("current_phase")
            phase_status = state.get("phase_status", {})
            if not current_phase or not phase_status:
                continue

            # Optional: filter by project name if state contains it
            state_project = state.get("project_name")
            if state_project and project_name and state_project != project_name:
                continue

            tk = _transition_key(current_phase)
            has_transition_qa = False
            if tk:
                has_transition_qa = bool(
                    state.get("transition_responses", {}).get(tk)
                )

            tag_states.append({
                "tag": tag,
                "current_phase": current_phase,
                "phase_status": phase_status,
                "current_activity": state.get("current_activity"),
                "selected_activities": state.get("selected_activities", {}),
                "selected_blocks": state.get("selected_blocks", []),
                "completed_blocks": state.get("completed_blocks", []),
                "has_transition_qa": has_transition_qa,
            })

    return tag_states


def group_tags_by_demo_phase(tag_states):
    """Group discovered tags by which phase they enable for demo.

    A tag is useful for demoing phase P if:
    - All phases before P are 'completed' in the tag's state
    - Phase P is 'not_started' or 'in_progress'

    For each phase, tags are sorted into categories:
    - 'phase_start': Phase P is not_started (can demo full phase from scratch)
    - 'phase_in_progress': Phase P is in_progress (can resume mid-phase)
    """
    grouped = {}

    for entry in tag_states:
        phase_status = entry["phase_status"]

        for i, phase in enumerate(PHASE_ORDER):
            ps = phase_status.get(phase, {})
            status = ps.get("status", "not_started") if isinstance(ps, dict) else ps

            # Check: all prior phases completed?
            prior_complete = all(
                _get_phase_status(phase_status, PHASE_ORDER[j]) in ("complete", "completed")
                for j in range(i)
            )

            if not prior_complete:
                continue

            if status == "not_started":
                grouped.setdefault(phase, {"phase_start": [], "phase_in_progress": []})
                grouped[phase]["phase_start"].append(entry)
            elif status == "in_progress":
                grouped.setdefault(phase, {"phase_start": [], "phase_in_progress": []})
                grouped[phase]["phase_in_progress"].append(entry)

    return grouped


def _get_phase_status(phase_status, phase):
    """Extract status string from phase_status entry (handles both dict and str)."""
    ps = phase_status.get(phase, {})
    if isinstance(ps, dict):
        return ps.get("status", "not_started")
    return ps if isinstance(ps, str) else "not_started"


def discover_tags(git_root, project_name, extra_state_paths=None):
    """Discover all tags and classify by reading state.json from each tagged commit.

    Two-pass discovery: try known prefix first, fall back to all tags.
    """
    manifest_paths = [
        ".rapids/manifest.yaml",
        "manifest.yaml",
        f".rapids/projects/{project_name}/manifest.yaml",
    ]
    state_paths = [
        "state.json",
        f".rapids/projects/{project_name}/state.json",
    ]
    if extra_state_paths:
        for p in extra_state_paths:
            if p not in state_paths:
                state_paths.append(p)

    # Pass 1: Try project-namespaced tags
    rc, out, _ = run_git(
        ["tag", "--list", f"rapids/{project_name}/*"],
        cwd=git_root, check=False
    )
    namespaced = [t.strip() for t in out.strip().split("\n") if t.strip()]

    if namespaced:
        tag_states = classify_tags(git_root, project_name, namespaced, state_paths, manifest_paths)
        results = group_tags_by_demo_phase(tag_states)
        if results:
            return results, tag_states

    # Pass 2: Fall back to ALL tags
    rc, out, _ = run_git(["tag", "--list"], cwd=git_root, check=False)
    all_tags = [t.strip() for t in out.strip().split("\n") if t.strip()]

    if not all_tags:
        return {}, []

    tag_states = classify_tags(git_root, project_name, all_tags, state_paths, manifest_paths)
    results = group_tags_by_demo_phase(tag_states)
    return results, tag_states


def select_best_tag(phase, grouped):
    """Select the best default tag for a phase.

    Priority:
    1. phase_in_progress with 0 completed activities (ready to execute)
    2. phase_start (full phase from scratch)
    3. phase_in_progress with fewest completed (closest to start)
    """
    phase_data = grouped.get(phase, {})
    in_progress = phase_data.get("phase_in_progress", [])
    start = phase_data.get("phase_start", [])

    # Priority 1: in_progress with 0 completed
    zero_completed = [t for t in in_progress if len(t.get("completed_blocks", [])) == 0]
    if zero_completed:
        return zero_completed[0]

    # Priority 2: phase_start
    if start:
        return start[0]

    # Priority 3: in_progress with fewest completed
    if in_progress:
        return min(in_progress, key=lambda t: len(t.get("completed_blocks", [])))

    return None


def format_output(project_name, grouped, tag_states):
    """Format discovery results as JSON."""
    phases = {}
    for phase in PHASE_ORDER:
        if phase in grouped:
            phase_data = grouped[phase]
            best = select_best_tag(phase, grouped)
            phases[phase] = {
                "phase_start": [
                    _format_tag_entry(t) for t in phase_data.get("phase_start", [])
                ],
                "phase_in_progress": [
                    _format_tag_entry(t) for t in phase_data.get("phase_in_progress", [])
                ],
                "best_default": best["tag"] if best else None,
            }

    return {
        "project": project_name,
        "has_tags": len(tag_states) > 0,
        "tag_count": len(tag_states),
        "phases": phases,
    }


def _format_tag_entry(entry):
    """Format a single tag entry for output."""
    selected = entry.get("selected_blocks", [])
    completed = entry.get("completed_blocks", [])
    return {
        "tag": entry["tag"],
        "current_phase": entry["current_phase"],
        "phase_status": entry["phase_status"],
        "selected_count": len(selected) if isinstance(selected, list) else 0,
        "completed_count": len(completed) if isinstance(completed, list) else 0,
        "has_transition_qa": entry.get("has_transition_qa", False),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: demo-tag-discovery.py <project-path> [--state-path <path>]")
        sys.exit(1)

    project_path = os.path.abspath(sys.argv[1])
    project_name = os.path.basename(project_path)
    # If called with a .rapids path, derive project name from parent directory
    if project_name == ".rapids":
        project_name = os.path.basename(os.path.dirname(project_path))

    extra_state_paths = []
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--state-path" and i + 1 < len(sys.argv):
            extra_state_paths.append(sys.argv[i + 1])
            i += 2
        else:
            i += 1

    git_root = find_git_root(cwd=project_path)
    if not git_root:
        print(json.dumps({
            "project": project_name,
            "has_tags": False,
            "tag_count": 0,
            "phases": {},
            "error": "Not inside a git repository",
        }, indent=2))
        sys.exit(0)

    grouped, tag_states = discover_tags(git_root, project_name, extra_state_paths)
    output = format_output(project_name, grouped, tag_states)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
