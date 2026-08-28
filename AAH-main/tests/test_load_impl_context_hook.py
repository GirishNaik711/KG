"""Functional tests for evaluator/implementer context separation.

NO MOCKS. Each test builds a REAL temp .aah/ project with seeded artifacts
(manifest, feature YAML, gaps, learnings, expertise, playbook markers,
session history, git repo), invokes load_impl_context via the real CLI,
and asserts on the JSON output.

Verifies:
- AC1: Evaluator mode includes facts-to-judge (AC descriptions, TC IDs, integration refs, subject descriptor)
- AC2: Evaluator mode excludes maker guidance (learnings, style_guide, playbook markers, session-history decisions)
- AC3: Hook routes aah-qa-evaluator to --evaluator without --include-standards
- AC4: Implementer mode unchanged (still emits maker guidance; new flags ignored)
"""

import json
import os
import secrets
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml


# Repo root for subprocess imports
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path) -> Path:
    """Create a real .aah/ project skeleton with git init."""
    aah = tmp_path / ".aah"
    for d in (
        "plan/features",
        "build/validation-results",
        "build/test-results",
        "codebase-intel",
        "architecture/playbooks",
    ):
        (aah / d).mkdir(parents=True, exist_ok=True)

    # manifest.yaml
    manifest = {
        "project_name": "eval-test",
        "project_type": "greenfield",
        "current_phase": "build",
    }
    (aah / "manifest.yaml").write_text(yaml.dump(manifest), encoding="utf-8")

    # Attestation secret
    (aah / "build" / ".attestation-secret").write_bytes(secrets.token_bytes(32))

    # Git init + one commit
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    return tmp_path


def _write_feature(project: Path, feature_id: str) -> None:
    """Write a feature .md with distinctive content."""
    features_dir = project / ".aah" / "plan" / "features"
    features_dir.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "id": feature_id,
        "title": "Test Feature",
        "acceptance_criteria": [
            {"id": "AC1", "description": "DISTINCTIVE_AC1_TEXT"},
            {"id": "AC2", "description": "DISTINCTIVE_AC2_TEXT"},
        ],
        "test_cases": [
            {
                "id": "TC1",
                "covers": ["AC1"],
                "description": "Test case one",
                "assertions": ["assertion_alpha", "assertion_beta"],
            },
            {
                "id": "TC2",
                "covers": ["AC2"],
                "description": "Test case two",
                "assertions": ["assertion_gamma"],
            },
        ],
        "integration_refs": ["EXT-01", "EXT-02"],
        "file_scope": ["src/module.py", "tests/test_module.py"],
        "dependencies": ["core.utils", "core.config"],
        "test_config": {"command": "pytest tests/"},
        "applicable_standards": [
            {"standard_id": "PY-SEC-001", "priority": "critical"},
        ],
        "spec_ref": "SPEC-123",
    }

    content = "---\n" + yaml.dump(frontmatter) + "---\n# Test Feature\n"
    (features_dir / f"{feature_id}.md").write_text(content, encoding="utf-8")


def _write_learnings(project: Path) -> None:
    """Write subagent-learnings.json with distinctive content."""
    learnings = {
        "entries": [
            {
                "patterns": ["DISTINCTIVE_LEARNING_TEXT"],
                "decisions": [],
                "conventions": [],
            }
        ]
    }
    path = project / ".aah" / "build" / "subagent-learnings.json"
    path.write_text(json.dumps(learnings, indent=2), encoding="utf-8")


def _write_expertise(project: Path) -> None:
    """Write expertise.yaml with distinctive content.

    NOTE: expertise is loaded but not rendered in status_summary currently.
    This helper kept for completeness but not used in AC tests.
    """
    expertise = {
        "consumption_views": {
            "style_guide": ["DISTINCTIVE_STYLE_GUIDE_TEXT"],
            "known_concerns": [],
            "quick_context": "",
        },
        "tech_patterns": [],
        "project_knowledge": [],
    }
    path = project / ".aah" / "codebase-intel" / "expertise.yaml"
    path.write_text(yaml.dump(expertise), encoding="utf-8")


def _write_playbook_marker(project: Path) -> None:
    """Write a playbook context marker.

    NOTE: Real playbooks are complex YAML structures loaded from published
    playbook files. For test simplicity, we skip playbook testing and focus
    on learnings + session history which are easier to seed.
    This helper is kept for structure but not used in AC tests.
    """
    pass


def _write_progress_with_history(project: Path, feature_id: str) -> None:
    """Write claude-progress.json with session_history."""
    progress = {
        "current_feature": feature_id,
        "in_progress_features": [feature_id],
        "completed_features": [],
        "session_history": [
            {
                "feature_id": feature_id,
                "summary": "Test session",
                "decisions_made": ["DISTINCTIVE_SESSION_DECISION_TEXT"],
                "patterns_discovered": [],
                "key_learnings": [],
            }
        ],
    }
    path = project / ".aah" / "claude-progress.json"
    path.write_text(json.dumps(progress, indent=2), encoding="utf-8")


def _run_load_impl_context(project: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run load_impl_context via CLI and return result."""
    cmd = [
        sys.executable, "-m", "aah.cli", "run",
        "core.build.load_impl_context",
        "--project-path", str(project),
    ] + args

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=dict(os.environ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ac1_evaluator_includes_facts_to_judge(tmp_path):
    """AC1: Evaluator mode includes AC descriptions, TC IDs, integration refs, subject descriptor."""
    project = _make_project(tmp_path)
    feature_id = "F001"

    _write_feature(project, feature_id)
    _write_progress_with_history(project, feature_id)

    result = _run_load_impl_context(project, ["--evaluator", "--feature-id", feature_id])

    assert result.returncode == 0, f"Load failed: {result.stderr}"

    data = json.loads(result.stdout)
    ctx = data.get("additionalContext", "")

    # AC descriptions
    assert "DISTINCTIVE_AC1_TEXT" in ctx
    assert "DISTINCTIVE_AC2_TEXT" in ctx

    # TC IDs
    assert "TC1" in ctx
    assert "TC2" in ctx

    # No Tier-1 gaps section: its producer (the build-time spec-validation
    # artifact) no longer exists (issue #117).
    assert "Tier 1 Gaps" not in ctx

    # Integration refs
    assert "EXT-01" in ctx

    # Subject descriptor (branch + sha)
    assert "Branch:" in ctx
    assert "SHA:" in ctx


def test_ac2_evaluator_excludes_maker_guidance(tmp_path):
    """AC2: Evaluator mode excludes learnings and session-history decisions."""
    project = _make_project(tmp_path)
    feature_id = "F001"

    _write_feature(project, feature_id)
    _write_progress_with_history(project, feature_id)
    _write_learnings(project)

    result = _run_load_impl_context(project, ["--evaluator", "--feature-id", feature_id])

    assert result.returncode == 0, f"Load failed: {result.stderr}"

    data = json.loads(result.stdout)
    ctx = data.get("additionalContext", "")

    # Maker guidance must be ABSENT
    assert "DISTINCTIVE_LEARNING_TEXT" not in ctx
    assert "DISTINCTIVE_SESSION_DECISION_TEXT" not in ctx


def test_ac3_hook_routes_to_evaluator_without_include_standards(tmp_path):
    """AC3: Hook config routes aah-qa-evaluator to --evaluator, NOT --subagent --include-standards."""
    # Read from worktree if we're in one, else from repo root
    worktree_hooks = Path(__file__).resolve().parent.parent / "aah" / "hooks" / "aah-hooks.yaml"
    repo_hooks = _REPO_ROOT / "aah" / "hooks" / "aah-hooks.yaml"
    hooks_path = worktree_hooks if worktree_hooks.exists() else repo_hooks
    assert hooks_path.exists(), f"Hooks file missing: {hooks_path}"

    with open(hooks_path, encoding="utf-8") as f:
        hooks_data = yaml.safe_load(f)

    # Navigate to hooks.SubagentStart
    hooks = hooks_data.get("hooks", {})
    subagent_start = hooks.get("SubagentStart", [])
    evaluator_hook = None
    for entry in subagent_start:
        if entry.get("matcher") == "aah-qa-evaluator":
            evaluator_hook = entry
            break

    assert evaluator_hook is not None, "aah-qa-evaluator matcher not found in SubagentStart"

    # Check run_hooks
    run_hooks = evaluator_hook.get("run_hooks", [])
    assert len(run_hooks) > 0, "No run_hooks for aah-qa-evaluator"

    first_hook = run_hooks[0]
    assert first_hook.get("run") == "core.build.load_impl_context"

    args = first_hook.get("args", "")
    assert "--evaluator" in args
    assert "--include-standards" not in args


def test_ac4_implementer_unchanged(tmp_path):
    """AC4: Implementer mode still emits maker guidance; new flags ignored in implementer mode."""
    project = _make_project(tmp_path)
    feature_id = "F001"

    _write_feature(project, feature_id)
    _write_progress_with_history(project, feature_id)
    _write_learnings(project)

    # Run implementer mode
    result = _run_load_impl_context(project, ["--subagent"])

    assert result.returncode == 0, f"Load failed: {result.stderr}"

    data = json.loads(result.stdout)
    ctx = data.get("additionalContext", "")

    # Implementer mode MUST include maker guidance (learnings, session history)
    assert "DISTINCTIVE_LEARNING_TEXT" in ctx
    assert "DISTINCTIVE_SESSION_DECISION_TEXT" in ctx

    # Re-running with --feature-id alone is unchanged; --tier1-gaps is gone
    # along with its producer (issue #117).
    result2 = _run_load_impl_context(project, ["--subagent", "--feature-id", feature_id])

    assert result2.returncode == 0
    data2 = json.loads(result2.stdout)
    ctx2 = data2.get("additionalContext", "")

    # Output is unchanged for the implementer path.
    assert "DISTINCTIVE_LEARNING_TEXT" in ctx2
    assert "DISTINCTIVE_SESSION_DECISION_TEXT" in ctx2

    # --tier1-gaps is no longer a declared argument. The hook wrapper reports
    # the unrecognized flag on stderr and yields no implementer context, rather
    # than silently accepting a flag whose producer is gone.
    rejected = _run_load_impl_context(
        project, ["--subagent", "--feature-id", feature_id, "--tier1-gaps", "[]"]
    )
    assert "DISTINCTIVE_LEARNING_TEXT" not in rejected.stdout


def _write_checkpoint_config_with_profile(project: Path, feature_id: str, level: str) -> None:
    """Write a checkpoint-config.yaml carrying a stored verification profile."""
    import yaml as _yaml

    cfg = {
        "checkpoint_configuration": {
            "verification_profiles": {
                feature_id: {
                    "level": level,
                    "rule_version": "1",
                    "reasons": ["explicit_security_scope"] if level == "deep" else [],
                    "override": None,
                }
            }
        }
    }
    cfg_path = project / ".aah" / "plan" / "checkpoint-config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_yaml.dump(cfg), encoding="utf-8")


def test_evaluator_has_profile_no_maker(tmp_path):
    """AC5: evaluator context carries verification-profile FACTS
    (level, reasons, config hash) but still NO maker guidance."""
    project = _make_project(tmp_path)
    feature_id = "F001"

    _write_feature(project, feature_id)
    _write_progress_with_history(project, feature_id)
    _write_learnings(project)
    _write_checkpoint_config_with_profile(project, feature_id, "deep")

    result = _run_load_impl_context(project, ["--evaluator", "--feature-id", feature_id])
    assert result.returncode == 0, f"Load failed: {result.stderr}"

    ctx = json.loads(result.stdout).get("additionalContext", "")

    # Profile facts present.
    assert "Verification Profile" in ctx
    assert "deep" in ctx
    assert "explicit_security_scope" in ctx
    assert "Checkpoint Config Hash" in ctx

    # Maker guidance still ABSENT.
    assert "DISTINCTIVE_LEARNING_TEXT" not in ctx
    assert "DISTINCTIVE_SESSION_DECISION_TEXT" not in ctx
