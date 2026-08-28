#!/usr/bin/env python3
"""
SessionStart hook: load implementation context into Claude's session.

Reads progress, features, waves, git log, and brownfield intelligence.
Outputs JSON with additionalContext for injection into Claude's context.
"""

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.feature_list import get_progress_summary, load_feature_list
from aah.core.intake.intake import load_intake, get_intake_summary
from aah.core.common.git_utils import get_log
from aah.core.common.io_utils import read_json, read_yaml, read_text
from aah.core.common.progress import load_progress


def load_impl_context(project_path: Path, subagent: bool = False) -> dict:
    """
    Build the implementation context summary.

    Returns a dict with:
    - status_summary: human-readable status
    - progress: current progress data
    - feature_summary: feature completion stats
    - current_wave: current wave details
    - recent_commits: last N git commits
    - brownfield_summary: brownfield intelligence (if available)
    - recommended_action: suggested next step
    """
    aah_path = project_path / ".aah"
    context = {}

    # Load progress
    progress_path = aah_path / "claude-progress.json"
    progress = load_progress(progress_path if progress_path.exists() else None)
    context["progress"] = progress

    # Load manifest
    manifest_path = aah_path / "manifest.yaml"
    if manifest_path.exists():
        context["manifest"] = read_yaml(manifest_path)

    # Sync feature-list.json from YAMLs (catches offline changes, branch switches)
    features_dir = aah_path / "plan" / "features"
    fl_path = aah_path / "feature-list.json"
    if features_dir.is_dir() and any(features_dir.glob("*.md")) and fl_path.exists():
        try:
            from aah.core.common.feature_list import sync_features_from_yaml, validate_json_matches_yamls
            sync_features_from_yaml(features_dir, fl_path)
            # Verify sync produced correct result
            drift_errors = validate_json_matches_yamls(features_dir, fl_path)
            if drift_errors:
                context["sync_warnings"] = drift_errors
                import sys as _sys
                print(
                    f"Warning: feature-list.json drift detected after sync: "
                    f"{len(drift_errors)} issue(s). First: {drift_errors[0]}",
                    file=_sys.stderr,
                )
        except Exception as e:
            context["sync_error"] = str(e)
            import sys as _sys
            print(f"Warning: feature-list sync failed on session start: {e}", file=_sys.stderr)

    # Load feature list summary
    if fl_path.exists():
        fl_data = load_feature_list(fl_path)
        context["feature_summary"] = get_progress_summary(fl_data)
    else:
        context["feature_summary"] = {"total": 0, "passing": 0, "failing": 0, "completion_pct": 0.0}

    # Load current wave info
    waves_path = aah_path / "plan" / "waves.json"
    if waves_path.exists():
        waves_data = read_json(waves_path)
        current_wave_idx = progress.get("current_wave")
        from aah.core.plan.compute_waves import flatten_waves
        waves = flatten_waves(waves_data)
        if current_wave_idx is not None and current_wave_idx < len(waves):
            context["current_wave"] = {
                "index": current_wave_idx,
                "features": waves[current_wave_idx],
                "total_waves": len(waves),
            }

    # Load intake context
    intake_path = aah_path / "intake.json"
    if intake_path.exists():
        try:
            intake = load_intake(intake_path)
            context["intake_summary"] = get_intake_summary(intake)
        except Exception:
            pass  # Don't let malformed intake crash the session hook

    # Load recent git log
    try:
        context["recent_commits"] = get_log(cwd=project_path, count=20)
    except Exception:
        context["recent_commits"] = []

    # Load codebase intelligence (unified path first, brownfield fallback)
    for intel_path in [
        aah_path / "codebase-intel" / "codebase-structure.md",
        aah_path / "brownfield" / "codebase-structure.md",
    ]:
        if intel_path.exists():
            try:
                content = read_text(intel_path)
                # Only include first 200 lines to avoid context bloat
                lines = content.split("\n")[:200]
                context["brownfield_summary"] = "\n".join(lines)
            except Exception:
                pass
            break

    # Load codemap context — pure loader, no checks, no gates
    # The implement skill already verified codemap availability before we get here.
    try:
        from aah.core.common.codemap_utils import get_codemap, get_db_path, read_metadata as read_codemap_metadata

        codemap_db = get_db_path(project_path)
        codemap_meta = read_codemap_metadata(project_path)
        manifest = context.get("manifest", {})
        is_brownfield = manifest.get("project_type") == "brownfield"

        if is_brownfield and codemap_db.exists():
            # Brownfield: LIVE queries against codemap.db for proactive discovery
            cm = get_codemap(project_path)

            # A. Structural overview
            stats_result = cm.stats()
            context["codemap_overview"] = {
                "files": stats_result.get("files", 0),
                "symbols": stats_result.get("symbols", 0),
                "relations": stats_result.get("relations", 0),
                "languages": stats_result.get("languages", {}),
                "entry_points": stats_result.get("entry_points", []),
                "hotspots": stats_result.get("hotspots", [])[:10],
            }

            # B. Semantic feature context (active feature's description)
            if codemap_meta and codemap_meta.get("semantic_index_built"):
                active_feature = _get_active_feature(progress, aah_path)
                if active_feature:
                    ask_results = cm.ask(active_feature["description"], top=10)
                    context["codemap_relevant_symbols"] = {
                        "feature": active_feature["id"],
                        "description": active_feature["description"],
                        "relevant_code": ask_results,
                    }

            # C. Architectural backbone (communities + god-nodes)
            from codemap_scale.orchestrator import ScaledCodeMapTools
            tools = ScaledCodeMapTools(cm)
            communities_result = cm.detect_communities()
            god_nodes_result = tools.get_god_nodes(top=10)
            context["codemap_architecture"] = {
                "communities": communities_result[:10] if communities_result else [],
                "god_nodes": god_nodes_result[:10] if god_nodes_result else [],
            }

        elif not is_brownfield and codemap_meta:
            # Greenfield: metadata only — NO live queries
            context["codemap_status"] = {
                "available": True,
                "files_indexed": codemap_meta.get("files_scanned", 0),
                "symbols_indexed": codemap_meta.get("symbols_extracted", 0),
                "last_wave": codemap_meta.get("last_wave"),
                "semantic_index_built": codemap_meta.get("semantic_index_built", False),
            }
    except Exception:
        pass

    # Load expertise (Tier 1 — project-wide knowledge)
    try:
        expertise_path = aah_path / "codebase-intel" / "expertise.yaml"
        if expertise_path.exists():
            expertise = read_yaml(expertise_path)
            if expertise and isinstance(expertise, dict):
                views = expertise.get("consumption_views") or {}
                context["expertise"] = {
                    "style_guide": views.get("style_guide", ""),
                    "known_concerns": views.get("known_concerns", ""),
                    "tech_patterns": expertise.get("tech_patterns", []),
                    "project_knowledge": expertise.get("project_knowledge", []),
                }
    except Exception:
        pass

    # Domain knowledge (Tier 2) — delivered to subagents via get_wave_context()
    # in update_impl_state.py (available_domains field). Not needed at session start.

    # Load knowledge base context (parsed project documentation)
    try:
        from aah.core.knowledge.parser import build_knowledge_context, find_knowledge_dir
        if find_knowledge_dir(project_path):
            context["knowledge_context"] = build_knowledge_context(project_path)
    except (Exception, SystemExit):
        pass

    # Load knowledge graph report (if available)
    for graph_path in [
        aah_path / "codebase-intel" / "CODEBASE_GRAPH_REPORT.md",
        aah_path / "brownfield" / "CODEBASE_GRAPH_REPORT.md",
    ]:
        if graph_path.exists():
            try:
                content = read_text(graph_path)
                lines = content.split("\n")[:100]
                context["knowledge_graph_summary"] = "\n".join(lines)
            except Exception:
                pass
            break

    # Load Build Playbook for the project's technology domain (if published)
    try:
        from aah.core.playbooks.context import build_playbook_context
        playbook_ctx = build_playbook_context(project_path)
        if playbook_ctx:
            context["playbook_context"] = playbook_ctx
            # Keep `domain_context` populated for back-compat with older consumers
            # that scrape that key.
            context["domain_context"] = playbook_ctx
    except (Exception, SystemExit):
        pass

    # Load Domain Intelligence for the project's attached industry domain.
    # The renderer itself enforces the research/analysis phase gate and the
    # presence of manifest.industry_domain_path.
    try:
        from aah.core.domain_briefs.context import build_domain_context_summary
        domain_intel = build_domain_context_summary(project_path)
        if domain_intel:
            context["domain_intelligence"] = domain_intel
    except (Exception, SystemExit):
        pass

    # Load subagent learnings
    try:
        from aah.core.build.capture_subagent_learnings import get_accumulated_learnings
        learnings = get_accumulated_learnings(project_path)
        if any(learnings.values()):
            context["subagent_learnings"] = learnings
    except (Exception, SystemExit):
        pass

    # Load validated runtime resources (cloud-readiness + data-schema-snapshot).
    # aah-feature-implementer subagents read this so they wire features to the
    # *real* bucket / endpoint / table names that the gates already proved
    # exist, instead of inventing placeholders.
    try:
        from aah.core.common.readiness import (
            load_runtime_resources,
            render_runtime_env_vars,
        )
        runtime_resources = load_runtime_resources(aah_path)
        if runtime_resources:
            context["runtime_resources"] = runtime_resources
            context["runtime_env_vars"] = render_runtime_env_vars(runtime_resources)
    except (Exception, SystemExit):
        pass

    # Load resolved enterprise standards (shared reference for all features)
    resolved_standards_path = aah_path / "plan" / "resolved-standards.yaml"
    if resolved_standards_path.exists():
        try:
            resolved = read_yaml(resolved_standards_path)
            rules = resolved.get("rules", [])
            if rules:
                context["enterprise_standards"] = {
                    "total_rules": len(rules),
                    "summary": resolved.get("summary", {}),
                    "rules": rules,
                }
        except Exception:
            pass

    # Load MCP server context (available tools for this session)
    try:
        from aah.core.mcp_servers.mcp_server_loader import build_mcp_context_summary
        mcp_summary = build_mcp_context_summary()
        if mcp_summary:
            context["mcp_server_guides"] = mcp_summary
    except Exception:
        pass  # MCP discovery is non-critical

    # Compute recommended next action
    context["recommended_action"] = compute_recommended_action(progress, context)

    # Build human-readable status summary
    context["status_summary"] = build_status_summary(context)

    return context



def _get_active_feature(progress: dict, aah_path: Path) -> dict | None:
    """Resolve the currently active feature for codemap semantic queries."""
    current_feature_id = progress.get("current_feature")
    if not current_feature_id:
        # Try in_progress_features list
        in_progress = progress.get("in_progress_features", [])
        if in_progress:
            current_feature_id = in_progress[0]
        else:
            return None
    from aah.core.common.feature_utils import find_feature_file, load_feature_data

    features_dir = aah_path / "plan" / "features"
    feature_data = load_feature_data(features_dir, current_feature_id)
    if feature_data:
        return {"id": current_feature_id, "description": feature_data.get("description", "")}
    return None


def compute_recommended_action(progress: dict, context: dict) -> str:
    """Compute the recommended next action based on current state."""
    phase = progress.get("current_phase", "init")
    env_state = progress.get("environment_state", "unknown")
    issues = progress.get("known_issues", [])
    in_progress = progress.get("in_progress_features", [])

    # If environment is unhealthy, fix that first
    if env_state == "unhealthy":
        return "Fix environment issues before starting new work. Run init.sh to diagnose."

    # If there are known issues, address those
    if issues:
        return f"Address known issues: {issues[0]}"

    # If features are in progress, continue them
    if in_progress:
        return f"Continue in-progress feature(s): {', '.join(in_progress)}"

    # Phase-specific recommendations
    if phase == "init":
        return "Run /aah-discuss to begin problem statement intake."
    elif phase == "discuss":
        return "Run /aah-discuss to begin the discuss phase."
    elif phase == "architecture":
        return "Run /aah-arch to begin the architecture phase."
    elif phase == "plan":
        return "Run /aah-plan to begin planning. Define features, DAG, and sprint contracts."
    elif phase == "build":
        feature_summary = context.get("feature_summary", {})
        if feature_summary.get("completion_pct", 0) == 100:
            return "All features complete! Run /aah-deploy to begin deployment."
        wave = context.get("current_wave", {})
        if wave:
            return f"Implement wave {wave.get('index', '?')}: features {wave.get('features', [])}"
        return "Run /aah-build to begin the next implementation wave."
    elif phase == "deploy":
        return "Run /aah-deploy to continue deployment configuration."

    return "Check project status with /aah-resume."


def build_status_summary(context: dict) -> str:
    """Build a human-readable status summary."""
    lines = []
    progress = context.get("progress", {})
    manifest = context.get("manifest", {})
    fs = context.get("feature_summary", {})

    lines.append(f"Project: {manifest.get('project_name', 'unknown')}")
    lines.append(f"Phase: {progress.get('current_phase', 'unknown')}")
    lines.append(f"Complexity: {manifest.get('complexity_tier', 'not classified')}")

    intake_summary = context.get("intake_summary")
    if intake_summary:
        lines.append(f"\n{intake_summary}")

    knowledge = context.get("knowledge_context")
    if knowledge:
        lines.append(f"\n{knowledge}")

    if fs.get("total", 0) > 0:
        lines.append(f"Features: {fs['passing']}/{fs['total']} passing ({fs['completion_pct']}%)")

    wave = context.get("current_wave")
    if wave:
        lines.append(f"Wave: {wave['index']}/{wave['total_waves']} — features: {wave['features']}")

    in_prog = progress.get("in_progress_features", [])
    if in_prog:
        lines.append(f"In progress: {', '.join(in_prog)}")

    issues = progress.get("known_issues", [])
    if issues:
        lines.append(f"Known issues: {len(issues)}")
        for issue in issues[:3]:
            lines.append(f"  - {issue}")

    # Build Playbook (tech-domain engineering patterns)
    playbook_ctx = context.get("playbook_context") or context.get("domain_context")
    if playbook_ctx:
        lines.append(f"\n{playbook_ctx}")

    # Domain Intelligence (industry-domain functional context for research/analysis phases)
    domain_intel = context.get("domain_intelligence")
    if domain_intel:
        lines.append(f"\n{domain_intel}")

    subagent_learnings = context.get("subagent_learnings")
    if subagent_learnings:
        has_any = any(subagent_learnings.get(k) for k in ["patterns", "decisions", "conventions"])
        if has_any:
            lines.append("\nSubagent Learnings:")
            for p in subagent_learnings.get("patterns", [])[:5]:
                lines.append(f"  Pattern: {p}")
            for d in subagent_learnings.get("decisions", [])[:5]:
                lines.append(f"  Decision: {d}")
            for c in subagent_learnings.get("conventions", [])[:5]:
                lines.append(f"  Convention: {c}")

    # Session intelligence — re-inject accumulated knowledge from prior sessions
    session_history = progress.get("session_history", [])
    if session_history:
        lines.append("\nSession Intelligence:")
        # Show last 5 session summaries (most recent first)
        recent = session_history[-5:][::-1]
        for entry in recent:
            summary = entry.get("summary", "")
            if summary:
                ts = entry.get("timestamp", "")
                lines.append(f"  [{ts[:10] if ts else '?'}] {summary}")

        # Collect unique decisions across all sessions
        all_decisions = []
        all_patterns = []
        all_learnings = []
        for entry in session_history:
            all_decisions.extend(entry.get("decisions_made", []))
            all_patterns.extend(entry.get("patterns_discovered", []))
            all_learnings.extend(entry.get("key_learnings", []) if isinstance(entry.get("key_learnings"), list) else ([entry["key_learnings"]] if entry.get("key_learnings") else []))

        if all_decisions:
            unique_decisions = list(dict.fromkeys(all_decisions))[:10]
            lines.append("  Decisions:")
            for d in unique_decisions:
                lines.append(f"    - {d}")

        if all_patterns:
            unique_patterns = list(dict.fromkeys(all_patterns))[:10]
            lines.append("  Patterns:")
            for p in unique_patterns:
                lines.append(f"    - {p}")

        if all_learnings:
            unique_learnings = list(dict.fromkeys(all_learnings))[:5]
            lines.append("  Learnings:")
            for l in unique_learnings:
                lines.append(f"    - {l}")

    # Validated runtime resources — coordinates the gates already proved.
    # Feature-implementer agents must wire to these names, not invent new ones.
    rr = context.get("runtime_resources")
    if rr:
        services = rr.get("services") or []
        data = rr.get("data") or {}
        lines.append("\nValidated Runtime Resources "
                     f"(cloud_provider={rr.get('cloud_provider') or 'n/a'}, "
                     f"deployment_method={rr.get('deployment_method') or 'n/a'}):")
        for svc in services:
            coord_pairs = [f"{k}={v}" for k, v in (svc.get("coordinates") or {}).items()]
            sec = svc.get("secret_ref") or {}
            sec_pairs = [f"{k}={v}" for k, v in sec.items()]
            tail = ""
            if coord_pairs:
                tail += " | " + ", ".join(coord_pairs)
            if sec_pairs:
                tail += " | secret_ref: " + ", ".join(sec_pairs)
            lines.append(
                f"  - {svc.get('display_name') or svc.get('id')} "
                f"[{svc.get('service_type')}] "
                f"({svc.get('status')}, auth={svc.get('auth_method') or 'n/a'})"
                f"{tail}"
            )
        if data.get("databases"):
            lines.append("  Databases (validated schema):")
            for db in data["databases"]:
                tables = db.get("tables") or []
                table_names = ", ".join(t.get("name", "?") for t in tables[:8])
                more = f" (+{len(tables) - 8} more)" if len(tables) > 8 else ""
                lines.append(
                    f"    - {db.get('display_name') or db.get('service_type')}: "
                    f"{len(tables)} tables — {table_names}{more}"
                )
        if data.get("storage"):
            lines.append("  Storage (validated prefixes):")
            for st in data["storage"]:
                prefixes = st.get("prefixes") or []
                lines.append(
                    f"    - {st.get('display_name') or st.get('service_type')} "
                    f"bucket={st.get('bucket_name')}: {len(prefixes)} prefixes"
                )
        env_map = context.get("runtime_env_vars") or {}
        if env_map:
            lines.append(f"  Env vars exported to features ({len(env_map)}):")
            for k in sorted(env_map.keys())[:20]:
                lines.append(f"    {k}={env_map[k]}")
            if len(env_map) > 20:
                lines.append(f"    … {len(env_map) - 20} more")
        lines.append(
            "  RULE: feature code MUST read resource coordinates from these "
            "env vars / coordinates above. Do NOT hardcode bucket / endpoint "
            "/ table names."
        )

    # Enterprise standards summary
    standards = context.get("enterprise_standards")
    if standards and standards.get("total_rules", 0) > 0:
        summary = standards.get("summary", {})
        lines.append(f"\nEnterprise Standards: {standards['total_rules']} rules active")
        lines.append(f"  Critical: {summary.get('critical', 0)} | High: {summary.get('high', 0)} | Medium: {summary.get('medium', 0)} | Low: {summary.get('low', 0)}")
        lines.append("  (Full rule details available in session context — check applicable_standards in feature YAML for IDs)")

    # MCP server availability (connected servers + their reference guides)
    mcp_guides = context.get("mcp_server_guides")
    if mcp_guides:
        lines.append(f"\n{mcp_guides}")

    lines.append(f"\nEnvironment: {progress.get('environment_state', 'unknown')}")
    lines.append(f"\nRecommended: {context.get('recommended_action', 'N/A')}")

    return "\n".join(lines)


def load_evaluator_context(
    project_path: Path,
    feature_id: str | None = None,
) -> dict:
    """
    Build evaluator context: WHITELIST facts-to-judge only (no maker guidance).

    Returns dict with:
    - feature_id: resolved feature ID
    - acceptance_criteria: list of {id, description}
    - test_cases: list of {id, covers, description, assertions}
    - public_contract: {spec_ref, integration_refs, file_scope, dependencies, test_config, applicable_standards}
    - subject_descriptor: {path, branch, sha}
    """
    from aah.core.common.feature_utils import load_feature_data
    from aah.core.common.git_utils import current_branch, get_log

    aah_path = project_path / ".aah"
    context = {}

    # Resolve feature ID
    if not feature_id:
        progress_path = aah_path / "claude-progress.json"
        progress = load_progress(progress_path if progress_path.exists() else None)
        feature_id = progress.get("current_feature")
        if not feature_id:
            in_progress = progress.get("in_progress_features", [])
            if in_progress:
                feature_id = in_progress[0]

    context["feature_id"] = feature_id

    if not feature_id:
        # No active feature — return minimal context
        return context

    # Load feature data
    features_dir = aah_path / "plan" / "features"
    feature_data = load_feature_data(features_dir, feature_id)

    if feature_data:
        # Acceptance criteria
        from aah.core.common.validators import acceptance_criterion_fields

        ac_list = feature_data.get("acceptance_criteria", [])
        context["acceptance_criteria"] = [
            {
                "id": acceptance_criterion_fields(ac, idx)[0],
                "description": acceptance_criterion_fields(ac, idx)[1],
            }
            for idx, ac in enumerate(ac_list)
        ]

        # Test cases
        tc_list = feature_data.get("test_cases", [])
        context["test_cases"] = [
            {
                "id": tc.get("id"),
                "covers": tc.get("covers") or [],
                "description": tc.get("description"),
                "assertions": tc.get("assertions", [])
            }
            for tc in tc_list
        ]

        # Public contract
        context["public_contract"] = {
            "spec_ref": feature_data.get("spec_ref"),
            "integration_refs": feature_data.get("integration_refs", []),
            "file_scope": feature_data.get("file_scope", []),
            "dependencies": feature_data.get("dependencies", []),
            "test_config": feature_data.get("test_config", {}),
            "applicable_standards": feature_data.get("applicable_standards", [])
        }

    # No Tier-1 gap block. Its producer (the build-time spec-validation
    # artifact) no longer exists: the AC-to-planned-test leg is proven at
    # planning time and the planned-to-collected leg inside run_feature_tests.

    # Subject descriptor (evaluator cwd IS the subject worktree)
    try:
        branch = current_branch(project_path)
    except Exception:
        branch = None

    try:
        log = get_log(project_path, 1)
        sha = log[0]["hash"] if log else None
    except Exception:
        sha = None

    context["subject_descriptor"] = {
        "path": str(project_path),
        "branch": branch,
        "sha": sha
    }

    # Verification-profile FACTS only (no maker guidance). The evaluator
    # sees the stored routing profile so a deep feature gets heightened depth,
    # but never any implementation reasoning, style, learnings, or patterns.
    try:
        from aah.core.build.qa_evidence import profile_binding_for_feature

        binding = profile_binding_for_feature(project_path, feature_id, subject_sha=sha)
        context["verification_profile"] = {
            "level": binding.get("level"),
            "rule_version": binding.get("rule_version"),
            "reasons": binding.get("reasons", []),
            "checkpoint_config_hash": binding.get("checkpoint_config_hash"),
            "override_ref": binding.get("override_ref"),
            "no_signal": binding.get("no_signal"),
        }
    except Exception:
        context["verification_profile"] = None

    return context


def build_evaluator_summary(context: dict) -> str:
    """
    Render evaluator context as human-readable text.

    This is INDEPENDENT from build_status_summary — no maker guidance.
    """
    lines = []
    lines.append("═" * 70)
    lines.append("QA EVALUATION FACTS (independent — no implementer guidance)")
    lines.append("═" * 70)

    # Subject
    subject = context.get("subject_descriptor", {})
    lines.append("\n## Subject Under Test")
    lines.append(f"Path: {subject.get('path', 'N/A')}")
    lines.append(f"Branch: {subject.get('branch', 'N/A')}")
    lines.append(f"SHA: {subject.get('sha', 'N/A')}")

    # Feature
    feature_id = context.get("feature_id")
    lines.append(f"\n## Feature: {feature_id or 'N/A'}")

    # Verification Profile — FACTS ONLY (no maker guidance)
    profile = context.get("verification_profile")
    if profile:
        lines.append("\n### Verification Profile")
        if profile.get("no_signal"):
            lines.append(f"Level: NO_SIGNAL ({profile.get('no_signal')})")
        else:
            lines.append(f"Level: {profile.get('level', 'N/A')}")
        lines.append(f"Rule Version: {profile.get('rule_version', 'N/A')}")
        reasons = profile.get("reasons", [])
        if reasons:
            lines.append(f"Reasons: {', '.join(str(r) for r in reasons)}")
        lines.append(f"Checkpoint Config Hash: {profile.get('checkpoint_config_hash', 'N/A')}")
        lines.append(f"Override Ref: {profile.get('override_ref')}")

    # Acceptance criteria
    ac_list = context.get("acceptance_criteria", [])
    if ac_list:
        lines.append("\n### Acceptance Criteria")
        for ac in ac_list:
            lines.append(f"  - {ac.get('id')}: {ac.get('description')}")

    # Test cases
    tc_list = context.get("test_cases", [])
    if tc_list:
        lines.append("\n### Test Cases")
        for tc in tc_list:
            covers = tc.get("covers", [])
            lines.append(f"  - {tc.get('id')} (covers: {', '.join(covers)}): {tc.get('description')}")
            assertions = tc.get("assertions", [])
            if assertions:
                for assertion in assertions:
                    lines.append(f"    → {assertion}")

    # Public contract
    contract = context.get("public_contract", {})
    lines.append("\n### Public Contract")
    lines.append(f"Spec: {contract.get('spec_ref', 'N/A')}")

    integration_refs = contract.get("integration_refs", [])
    if integration_refs:
        lines.append(f"Integration Points: {', '.join(integration_refs)}")

    file_scope = contract.get("file_scope", [])
    if file_scope:
        lines.append(f"File Scope: {len(file_scope)} file(s)")
        for f in file_scope[:5]:  # First 5
            lines.append(f"  - {f}")

    dependencies = contract.get("dependencies", [])
    if dependencies:
        lines.append(f"Dependencies: {', '.join(dependencies)}")

    test_cmd = contract.get("test_config", {}).get("command")
    if test_cmd:
        lines.append(f"Test Command: {test_cmd}")

    # Applicable standards — filter to only valid rule dicts (skip bare strings and lightweight format)
    standards = contract.get("applicable_standards", [])
    valid = [s for s in standards if isinstance(s, dict) and ("standard_id" in s or "id" in s)]
    if valid:
        lines.append("\n### Applicable Standards")
        for std in valid:
            std_id = std.get("standard_id") or std.get("id", "?")
            priority = std.get("priority", "?")
            lines.append(f"  - {std_id} (priority: {priority})")

    lines.append("\n" + "═" * 70)

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load implementation context")
    parser.add_argument("--subagent", action="store_true", help="Loading context for a subagent")
    parser.add_argument("--project-path", type=Path, default=None)
    parser.add_argument("--evaluator", action="store_true", help="Loading context for QA evaluator (facts-to-judge only)")
    parser.add_argument("--feature-id", type=str, default=None, help="Feature ID for evaluator context")
    args = parser.parse_args()

    # Try to read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    from aah.core.common.config import resolve_project_path
    explicit = args.project_path
    if explicit is None:
        cwd = hook_input.get("cwd")
        explicit = Path(cwd) if cwd else None
    project_path = resolve_project_path(explicit)

    if project_path is None or not (project_path / ".aah").is_dir():
        # Not a AAH project — output empty context
        json.dump({"additionalContext": ""}, sys.stdout, indent=2)
        print()
        sys.exit(0)

    # Evaluator path: whitelist facts-to-judge only (no maker guidance)
    if args.evaluator:
        context = load_evaluator_context(project_path, feature_id=args.feature_id)
        output = {"additionalContext": build_evaluator_summary(context)}
        json.dump(output, sys.stdout, indent=2)
        print()
        sys.exit(0)

    context = load_impl_context(project_path, subagent=args.subagent)

    # Output for SessionStart hook: additionalContext field
    output = {
        "additionalContext": context["status_summary"],
    }

    json.dump(output, sys.stdout, indent=2)
    print()
    sys.exit(0)


if __name__ == "__main__":
    main()
