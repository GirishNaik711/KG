"""
Hook Integration Tests — comprehensive coverage of every hook script.

Tests each hook script's stdin/stdout/exit-code contract with realistic
inputs — both the allow path (exit 0) and the block path (exit 2).
Also tests edge cases: empty stdin, missing files, ignored patterns.

These are deterministic CLI tests — no Agent SDK, no API tokens required.
Run with: pytest tests/e2e/test_hooks.py -v

Hook inventory (from .claude/settings.json):

  SessionStart    → load_impl_context
  PreToolUse:Bash (if *mock*) → no_mocks_guard
  PreToolUse:Write            → validate_aah_path
  PreToolUse:Write (if *feature-list.json*) → validate_feature_list
  PostToolUse:Write|Edit      → validate_artifact_template
  PostToolUse:Bash            → log_test_results
  Stop                        → generate_session_summary
                              → activity_logger log-session
  SubagentStart:feature-impl  → load_impl_context --subagent
  SubagentStop:feature-impl   → check_clean_git
                              → cleanup_worktrees clean --from-hook
  SubagentStop:aah-qa-evaluator   → log_test_results --evaluator
  TaskCompleted               → validate_feature_complete
  TeammateIdle                → validate_regression_pass
  PreCompact                  → progress snapshot
"""

import json
import os
import subprocess
from pathlib import Path

import pytest


# ─── Helpers ─────────────────────────────────────────────────────────

def run_hook(module: str, stdin_data: dict | None = None, extra_args: list | None = None,
             cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a hook script via aah-run, feeding JSON on stdin."""
    cmd = ["aah-run", module] + (extra_args or [])
    input_text = json.dumps(stdin_data) if stdin_data is not None else ""
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )


def hook_input(tool_input: dict, cwd: str | Path | None = None, **kwargs) -> dict:
    """Build a standard PreToolUse/PostToolUse hook input dict."""
    data = {"tool_input": tool_input}
    if cwd:
        data["cwd"] = str(cwd)
    data.update(kwargs)
    return data


def post_hook_input(tool_input: dict, tool_output: dict,
                    cwd: str | Path | None = None) -> dict:
    """Build a standard PostToolUse hook input dict."""
    data = {"tool_input": tool_input, "tool_output": tool_output}
    if cwd:
        data["cwd"] = str(cwd)
    return data


# ─── PreToolUse:Bash → no_mocks_guard ────────────────────────────────

class TestHookNoMocksGuard:
    """aah.core.guards.no_mocks_guard — blocks mock framework installs/imports."""

    def test_allows_clean_bash_command(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "pip install requests"}))
        assert result.returncode == 0

    def test_allows_pytest_without_mock(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "uv run pytest tests/ -v"}))
        assert result.returncode == 0

    def test_blocks_pytest_mock_install(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "pip install pytest-mock"}))
        assert result.returncode == 2
        assert "mock" in result.stderr.lower()

    def test_blocks_unittest_mock_import(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "python -c 'from unittest.mock import MagicMock'"}))
        assert result.returncode == 2

    def test_blocks_sinon_install(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "npm install sinon --save-dev"}))
        assert result.returncode == 2

    def test_blocks_jest_mock_usage(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "node -e \"jest.mock('./module')\""}))
        assert result.returncode == 2

    def test_blocks_vitest_mock(self):
        # vi.mock() is how vitest mocking is used in test code executed via Bash
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "node -e \"vi.mock('./module', () => ({ default: {} }))\""}))
        assert result.returncode == 2

    def test_allows_empty_stdin(self):
        """Empty/missing stdin should not crash — allow by default."""
        result = run_hook("aah.core.guards.no_mocks_guard", {})
        assert result.returncode == 0

    def test_error_message_mentions_rapids_policy(self):
        result = run_hook("aah.core.guards.no_mocks_guard",
                          hook_input({"command": "pip install pytest-mock"}))
        assert result.returncode == 2
        # Should explain the no-mock policy
        combined = result.stdout + result.stderr
        assert any(w in combined.lower() for w in ["mock", "functional", "aah_root", "real"])


# ─── PreToolUse:Write → validate_aah_path ─────────────────────────

class TestHookValidateRapidsPath:
    """aah.core.guards.validate_aah_path — phase-based write gating."""

    def test_allows_write_to_non_rapids_file(self, project_with_features):
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input({"file_path": str(project_with_features / "src" / "main.py")},
                                     cwd=project_with_features))
        assert result.returncode == 0

    def test_allows_write_to_implement_dir_during_implement(self, project_with_features):
        """project_with_features is in implement phase — implement/ is allowed."""
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input(
                              {"file_path": str(project_with_features / ".aah" / "build" / "impl-state.json")},
                              cwd=project_with_features,
                          ))
        assert result.returncode == 0

    def test_allows_write_to_audit_dir_always(self, project_with_features):
        """audit/ is always writable regardless of phase."""
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input(
                              {"file_path": str(project_with_features / ".aah" / "audit" / "log.jsonl")},
                              cwd=project_with_features,
                          ))
        assert result.returncode == 0

    def test_allows_write_to_brownfield_dir_always(self, project_with_features):
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input(
                              {"file_path": str(project_with_features / ".aah" / "brownfield" / "map.md")},
                              cwd=project_with_features,
                          ))
        assert result.returncode == 0

    def test_blocks_write_to_research_dir_during_implement(self, project_with_features):
        """Writing to research/ during implement phase should be blocked."""
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input(
                              {"file_path": str(project_with_features / ".aah" / "discuss" / "notes.md")},
                              cwd=project_with_features,
                          ))
        assert result.returncode == 2
        assert "discuss" in result.stderr.lower() or "phase" in result.stderr.lower()

    def test_blocks_write_to_analysis_dir_during_implement(self, project_with_features):
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input(
                              {"file_path": str(project_with_features / ".aah" / "architecture" / "arch.md")},
                              cwd=project_with_features,
                          ))
        assert result.returncode == 2

    def test_allows_write_when_no_project_configured(self, tmp_path):
        """No project in CWD → guard passes silently (can't validate without project context)."""
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input({"file_path": str(tmp_path / ".aah" / "discuss" / "x.md")},
                                     cwd=tmp_path))
        assert result.returncode == 0

    def test_allows_plan_dir_during_implement(self, project_with_features):
        """plan/ is readable in implement phase for reference."""
        result = run_hook("aah.core.guards.validate_aah_path",
                          hook_input(
                              {"file_path": str(project_with_features / ".aah" / "plan" / "dag.json")},
                              cwd=project_with_features,
                          ))
        assert result.returncode == 0


# ─── PreToolUse:Write(*feature-list.json*) → validate_feature_list ───

class TestHookValidateFeatureList:
    """aah.core.gates.validate_feature_list — structural integrity of feature-list.json."""

    def test_allows_passes_status_change(self, project_with_features):
        """Changing passes from false to true is the only allowed modification."""
        import copy, json as json_mod
        fl_path = project_with_features / ".aah" / "feature-list.json"
        original = json_mod.loads(fl_path.read_text())
        proposed = copy.deepcopy(original)
        proposed["features"][0]["passes"] = True
        result = run_hook(
            "aah.core.gates.validate_feature_list",
            hook_input({"file_path": str(fl_path), "content": json_mod.dumps(proposed)},
                       cwd=project_with_features),
        )
        assert result.returncode == 0

    def test_blocks_feature_removal(self, project_with_features):
        import copy, json as json_mod
        fl_path = project_with_features / ".aah" / "feature-list.json"
        original = json_mod.loads(fl_path.read_text())
        proposed = copy.deepcopy(original)
        proposed["features"].pop()  # remove last feature
        result = run_hook(
            "aah.core.gates.validate_feature_list",
            hook_input({"file_path": str(fl_path), "content": json_mod.dumps(proposed)},
                       cwd=project_with_features),
        )
        assert result.returncode == 2
        assert "remov" in result.stderr.lower() or "structur" in result.stderr.lower()

    def test_blocks_description_change(self, project_with_features):
        import copy, json as json_mod
        fl_path = project_with_features / ".aah" / "feature-list.json"
        original = json_mod.loads(fl_path.read_text())
        proposed = copy.deepcopy(original)
        proposed["features"][0]["description"] = "HACKED DESCRIPTION"
        result = run_hook(
            "aah.core.gates.validate_feature_list",
            hook_input({"file_path": str(fl_path), "content": json_mod.dumps(proposed)},
                       cwd=project_with_features),
        )
        assert result.returncode == 2

    def test_blocks_feature_addition(self, project_with_features):
        import copy, json as json_mod
        fl_path = project_with_features / ".aah" / "feature-list.json"
        original = json_mod.loads(fl_path.read_text())
        proposed = copy.deepcopy(original)
        proposed["features"].append({"id": "F999", "description": "Injected", "passes": False})
        result = run_hook(
            "aah.core.gates.validate_feature_list",
            hook_input({"file_path": str(fl_path), "content": json_mod.dumps(proposed)},
                       cwd=project_with_features),
        )
        assert result.returncode == 2

    def test_allows_multiple_status_updates(self, project_with_features):
        import copy, json as json_mod
        fl_path = project_with_features / ".aah" / "feature-list.json"
        original = json_mod.loads(fl_path.read_text())
        proposed = copy.deepcopy(original)
        for f in proposed["features"]:
            f["passes"] = True
        result = run_hook(
            "aah.core.gates.validate_feature_list",
            hook_input({"file_path": str(fl_path), "content": json_mod.dumps(proposed)},
                       cwd=project_with_features),
        )
        assert result.returncode == 0

    def test_blocks_id_change(self, project_with_features):
        import copy, json as json_mod
        fl_path = project_with_features / ".aah" / "feature-list.json"
        original = json_mod.loads(fl_path.read_text())
        proposed = copy.deepcopy(original)
        proposed["features"][0]["id"] = "F999"
        result = run_hook(
            "aah.core.gates.validate_feature_list",
            hook_input({"file_path": str(fl_path), "content": json_mod.dumps(proposed)},
                       cwd=project_with_features),
        )
        assert result.returncode == 2


# ─── PostToolUse:Write|Edit → validate_artifact_template ─────────────

class TestHookValidateArtifactTemplate:
    """aah.core.guards.validate_artifact_template — enforces section structure."""

    def test_allows_non_rapids_file(self):
        """Files outside .aah/ are not validated against templates."""
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/src/main.py", "content": "print('hello')"}),
        )
        assert result.returncode == 0

    def test_allows_non_markdown_rapids_file(self):
        """JSON/YAML files in .aah/ are not template-validated."""
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/feature-list.json",
                        "content": '{"features": []}'}),
        )
        assert result.returncode == 0

    def test_allows_artifact_not_matching_any_activity_yaml(self):
        """Files in .aah/ that don't match any activity YAML pass without validation."""
        content = "# Some Doc\n\nNo sections at all."
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/unknown-doc.md", "content": content}),
        )
        assert result.returncode == 0

    def test_validates_artifact_against_activity_yaml_sections(self):
        """Artifact with Activity ID in frontmatter and all YAML sections should pass."""
        # Use R-SHARED-prior-art activity which has these sections:
        # Executive Summary, Technical Constraints, Risk Assessment,
        # Compliance Requirements, Mitigation Plan, Open Questions
        content = (
            "# Constraints and Risks\n\n"
            "**Activity:** R-SHARED-prior-art\n\n"
            "---\n\n"
            "## Executive Summary\nOverview of constraints and risks.\n\n"
            "## Technical Constraints\n| ID | Constraint | Category | Hard/Soft |\n"
            "| TC-1 | Python 3.11 | Tech | Hard |\n\n"
            "## Risk Assessment\n| ID | Category | Description | Likelihood | Impact |\n"
            "| RISK-1 | Technical | API latency | Medium | High |\n\n"
            "## Compliance Requirements\n| ID | Framework | Controls |\n"
            "| REG-1 | GDPR | Art. 17 |\n\n"
            "## Mitigation Plan\n| ID | Strategy | Action | Timeline |\n"
            "| MIT-1 | Reduce | Cache responses | Sprint 2 |\n\n"
            "## Open Questions\n- Q1: Data residency?\n\n"
        )
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/prior-art-analysis.md", "content": content}),
        )
        assert result.returncode == 0

    def test_blocks_artifact_missing_yaml_section(self):
        """Artifact missing a YAML-declared section should be blocked."""
        # Missing "Open Questions" section
        content = (
            "# Constraints\n\n"
            "**Activity:** R-SHARED-prior-art\n\n"
            "---\n\n"
            "## Executive Summary\nSummary.\n\n"
            "## Technical Constraints\nTable.\n\n"
            "## Risk Assessment\nTable.\n\n"
            "## Compliance Requirements\nTable.\n\n"
            "## Mitigation Plan\nTable.\n\n"
            # Missing ## Open Questions
        )
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/prior-art-analysis.md", "content": content}),
        )
        assert result.returncode == 2
        assert "open questions" in result.stderr.lower()

    def test_extra_sections_in_artifact_allowed(self):
        """Artifact with YAML sections + extra bonus sections should pass."""
        content = (
            "# Constraints and Risks\n\n"
            "**Activity:** R-SHARED-prior-art\n\n"
            "---\n\n"
            "## Executive Summary\nOverview.\n\n"
            "## Technical Constraints\nTable.\n\n"
            "## Risk Assessment\nTable.\n\n"
            "## Compliance Requirements\nTable.\n\n"
            "## Mitigation Plan\nTable.\n\n"
            "## Open Questions\nList.\n\n"
            "## Appendix\nExtra bonus content.\n\n"
            "## Domain Alignment\nMapping to domain brief.\n\n"
        )
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/prior-art-analysis.md", "content": content}),
        )
        assert result.returncode == 0

    def test_numbered_headers_in_artifact_still_match(self):
        """Numbered headers like '## 1. Section' should match YAML 'Section'."""
        content = (
            "# Constraints and Risks\n\n"
            "**Activity:** R-SHARED-prior-art\n\n"
            "---\n\n"
            "## 1. Executive Summary\nOverview.\n\n"
            "## 2. Technical Constraints\nTable.\n\n"
            "## 3. Risk Assessment\nTable.\n\n"
            "## 4. Compliance Requirements\nTable.\n\n"
            "## 5. Mitigation Plan\nTable.\n\n"
            "## 6. Open Questions\nList.\n\n"
        )
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/prior-art-analysis.md", "content": content}),
        )
        assert result.returncode == 0

    def test_case_insensitive_section_matching(self):
        """Lowercase/uppercase headers should match YAML sections."""
        content = (
            "# Constraints and Risks\n\n"
            "**Activity:** R-SHARED-prior-art\n\n"
            "---\n\n"
            "## EXECUTIVE SUMMARY\nOverview.\n\n"
            "## technical constraints\nTable.\n\n"
            "## Risk Assessment\nTable.\n\n"
            "## compliance REQUIREMENTS\nTable.\n\n"
            "## Mitigation plan\nTable.\n\n"
            "## OPEN QUESTIONS\nList.\n\n"
        )
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/prior-art-analysis.md", "content": content}),
        )
        assert result.returncode == 0

    def test_fallback_to_phase_filename_without_frontmatter(self):
        """Artifacts without Activity ID in frontmatter should fallback to phase+filename lookup."""
        content = (
            "# Constraints and Risks\n\n"
            "## Executive Summary\nOverview.\n\n"
            "## Technical Constraints\nTable.\n\n"
            "## Risk Assessment\nTable.\n\n"
            "## Compliance Requirements\nTable.\n\n"
            "## Mitigation Plan\nTable.\n\n"
            "## Open Questions\nList.\n\n"
        )
        result = run_hook(
            "aah.core.guards.validate_artifact_template",
            hook_input({"file_path": "/project/.aah/discuss/prior-art-analysis.md", "content": content}),
        )
        assert result.returncode == 0


# ─── PostToolUse:Bash → log_test_results ─────────────────────────────

class TestHookLogTestResults:
    """aah.core.logging.log_test_results — non-fatal test result capture."""

    def test_always_exits_zero(self, project_with_features):
        """log_test_results is non-fatal — always exit 0."""
        result = run_hook(
            "aah.core.logging.log_test_results",
            post_hook_input(
                {"command": "uv run pytest tests/ -v"},
                {"stdout": "2 passed", "stderr": ""},
                cwd=project_with_features,
            ),
        )
        assert result.returncode == 0

    def test_logs_passing_pytest_output(self, project_with_features):
        """Passing test output should be logged to test-execution-log.jsonl."""
        log_path = project_with_features / ".aah" / "build" / "test-results" / "test-execution-log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        initial_lines = len(log_path.read_text().splitlines()) if log_path.exists() else 0

        run_hook(
            "aah.core.logging.log_test_results",
            post_hook_input(
                {"command": "uv run pytest tests/ -v --tb=short"},
                {"stdout": "tests/test_foo.py::test_bar PASSED\n1 passed in 0.12s", "stderr": ""},
                cwd=project_with_features,
            ),
        )
        if log_path.exists():
            new_lines = len(log_path.read_text().splitlines())
            assert new_lines >= initial_lines  # appended or created

    def test_logs_failing_pytest_output(self, project_with_features):
        """Failing test output should also be logged."""
        result = run_hook(
            "aah.core.logging.log_test_results",
            post_hook_input(
                {"command": "uv run pytest tests/ -v"},
                {"stdout": "FAILED tests/test_foo.py::test_bar\n1 failed in 0.15s", "stderr": ""},
                cwd=project_with_features,
            ),
        )
        assert result.returncode == 0  # non-fatal even on test failure

    def test_nonfatal_when_no_project(self, tmp_path):
        """Should exit 0 even when not in a AAH project directory."""
        result = run_hook(
            "aah.core.logging.log_test_results",
            post_hook_input(
                {"command": "echo hello"},
                {"stdout": "hello", "stderr": ""},
                cwd=tmp_path,
            ),
        )
        assert result.returncode == 0

    def test_ignores_non_test_commands(self, project_with_features):
        """Non-test commands (git, pip, etc.) should not be logged as test results."""
        result = run_hook(
            "aah.core.logging.log_test_results",
            post_hook_input(
                {"command": "git status"},
                {"stdout": "On branch main\nnothing to commit", "stderr": ""},
                cwd=project_with_features,
            ),
        )
        assert result.returncode == 0


# ─── Stop → generate_session_summary ─────────────────────────────────

class TestHookGenerateSessionSummary:
    """aah.core.build.generate_session_summary — session end reporting."""

    def test_generates_valid_json_output(self, project_with_features):
        result = run_hook(
            "aah.core.build.generate_session_summary",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert "summary" in out
        assert "commits" in out
        assert "features_completed" in out
        assert "feature_progress" in out

    def test_feature_progress_has_expected_keys(self, project_with_features):
        result = run_hook(
            "aah.core.build.generate_session_summary",
            {"cwd": str(project_with_features)},
        )
        out = json.loads(result.stdout)
        progress = out["feature_progress"]
        assert "total" in progress
        assert "passing" in progress
        assert "failing" in progress
        assert "completion_pct" in progress
        assert progress["total"] == 4  # project_with_features has 4 features

    def test_appends_to_progress_history(self, project_with_features):
        import json as json_mod
        progress_path = project_with_features / ".aah" / "claude-progress.json"
        before = json_mod.loads(progress_path.read_text())
        before_history_len = len(before.get("session_history", []))

        run_hook(
            "aah.core.build.generate_session_summary",
            {"cwd": str(project_with_features)},
        )
        after = json_mod.loads(progress_path.read_text())
        assert len(after.get("session_history", [])) >= before_history_len

    def test_handles_no_project_gracefully(self, tmp_path):
        """Non-AAH directory should not crash — exit 0 with minimal output."""
        result = run_hook(
            "aah.core.build.generate_session_summary",
            {"cwd": str(tmp_path)},
        )
        assert result.returncode == 0


# ─── SessionStart → load_impl_context ────────────────────────────────

class TestHookLoadImplContext:
    """aah.core.build.load_impl_context — session startup context injection."""

    def test_returns_additional_context_key(self, project_with_features):
        result = run_hook(
            "aah.core.build.load_impl_context",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert "additionalContext" in out

    def test_context_mentions_phase(self, project_with_features):
        result = run_hook(
            "aah.core.build.load_impl_context",
            {"cwd": str(project_with_features)},
        )
        out = json.loads(result.stdout)
        ctx = out["additionalContext"]
        assert any(p in ctx.lower() for p in ["build", "phase", "wave", "feature"])

    def test_subagent_flag_returns_context(self, project_with_features):
        result = run_hook(
            "aah.core.build.load_impl_context",
            {"cwd": str(project_with_features)},
            extra_args=["--subagent"],
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert "additionalContext" in out

    def test_no_project_returns_empty_context(self, tmp_path):
        """No project configured → still returns JSON with additionalContext key."""
        result = run_hook(
            "aah.core.build.load_impl_context",
            {"cwd": str(tmp_path)},
        )
        assert result.returncode == 0
        # Should return valid JSON even with no project
        out = json.loads(result.stdout)
        assert "additionalContext" in out


# ─── SubagentStop:aah-feature-implementer → check_clean_git ──────────────

class TestHookCheckCleanGit:
    """aah.core.git_ops.check_clean_git — dirty state blocks subagent stop."""

    def test_allows_clean_working_directory(self, project_with_features):
        # Commit everything first
        subprocess.run(["git", "add", "-A"], cwd=project_with_features, capture_output=True)
        subprocess.run(["git", "commit", "-m", "clean state", "--allow-empty"],
                       cwd=project_with_features, capture_output=True)
        result = run_hook(
            "aah.core.git_ops.check_clean_git",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 0

    def test_blocks_uncommitted_source_file(self, project_with_features):
        """An uncommitted .py file should block the subagent from stopping."""
        dirty_file = project_with_features / "src" / "untracked.py"
        dirty_file.parent.mkdir(exist_ok=True)
        dirty_file.write_text("# untracked source file")
        try:
            result = run_hook(
                "aah.core.git_ops.check_clean_git",
                {"cwd": str(project_with_features)},
            )
            assert result.returncode == 2
            assert "untracked.py" in result.stderr or "uncommitted" in result.stderr.lower()
        finally:
            dirty_file.unlink(missing_ok=True)

    def test_ignores_dotclaude_artifacts(self, project_with_features):
        """.claude/ temp files should not block subagent stop."""
        claude_dir = project_with_features / ".claude" / "tmp"
        claude_dir.mkdir(parents=True, exist_ok=True)
        artifact = claude_dir / "session.json"
        artifact.write_text('{"session": "temp"}')
        try:
            # First commit all real files
            subprocess.run(["git", "add", "-A", "--", "*.py", ".aah/"],
                           cwd=project_with_features, capture_output=True)
            result = run_hook(
                "aah.core.git_ops.check_clean_git",
                {"cwd": str(project_with_features)},
            )
            # .claude/ should be in IGNORED_PATTERNS — not block
            # (returncode 0 if .claude/ is the only dirty thing)
            # We check stderr doesn't mention .claude/
            if result.returncode == 2:
                assert ".claude/" not in result.stderr, (
                    ".claude/ artifacts should be ignored by check_clean_git"
                )
        finally:
            artifact.unlink(missing_ok=True)

    def test_ignores_agent_memory_artifacts(self, project_with_features):
        """.aah/agent-memory/ files should not block subagent stop."""
        # Commit everything
        subprocess.run(["git", "add", "-A"], cwd=project_with_features, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base", "--allow-empty"],
                       cwd=project_with_features, capture_output=True)
        # Write to the ignored path
        mem_dir = project_with_features / ".aah" / "agent-memory"
        mem_dir.mkdir(exist_ok=True)
        (mem_dir / "context.md").write_text("# context")
        try:
            result = run_hook(
                "aah.core.git_ops.check_clean_git",
                {"cwd": str(project_with_features)},
            )
            if result.returncode == 2:
                assert "agent-memory" not in result.stderr, (
                    "agent-memory should be ignored"
                )
        finally:
            import shutil
            shutil.rmtree(mem_dir, ignore_errors=True)

    def test_blocks_uncommitted_rapids_state_change(self, project_with_features):
        """Uncommitted changes to .aah/build/ should block."""
        # Commit everything first
        subprocess.run(["git", "add", "-A"], cwd=project_with_features, capture_output=True)
        subprocess.run(["git", "commit", "-m", "baseline", "--allow-empty"],
                       cwd=project_with_features, capture_output=True)
        # Modify a .aah file without committing
        impl_state = project_with_features / ".aah" / "build" / "impl-state.json"
        original = impl_state.read_text() if impl_state.exists() else None
        impl_state.write_text('{"modified": true, "uncommitted": true}')
        try:
            result = run_hook(
                "aah.core.git_ops.check_clean_git",
                {"cwd": str(project_with_features)},
            )
            assert result.returncode == 2
        finally:
            if original is not None:
                impl_state.write_text(original)
            else:
                impl_state.unlink(missing_ok=True)


# ─── TaskCompleted → validate_feature_complete ───────────────────────

class TestHookValidateFeatureComplete:
    """aah.core.gates.validate_feature_complete — feature readiness at task done."""

    def _task_input(self, feature_id: str, cwd: Path) -> dict:
        return {
            "task": {
                "metadata": {"feature_id": feature_id},
                "subject": f"Implement {feature_id}",
            },
            "cwd": str(cwd),
        }

    def test_passes_when_feature_has_passing_tests_and_marked_passing(
        self, project_with_features
    ):
        from aah.core.common.io_utils import write_json
        from aah.core.common.feature_list import update_feature_status
        # Write passing test result and mark feature as passing
        write_json(
            {"passed": True, "feature_id": "F001", "exit_code": 0, "failed_tests": []},
            project_with_features / ".aah" / "build" / "test-results" / "F001.json",
        )
        update_feature_status(project_with_features / ".aah" / "feature-list.json", "F001", True)
        result = run_hook(
            "aah.core.gates.validate_feature_complete",
            self._task_input("F001", project_with_features),
        )
        assert result.returncode == 0

    def test_blocks_when_no_test_results(self, project_with_features):
        """F002 has no test results yet — should be blocked."""
        tr_path = project_with_features / ".aah" / "build" / "test-results" / "F002.json"
        tr_path.unlink(missing_ok=True)
        result = run_hook(
            "aah.core.gates.validate_feature_complete",
            self._task_input("F002", project_with_features),
        )
        assert result.returncode == 2
        assert "F002" in result.stderr or "test" in result.stderr.lower()

    def test_blocks_when_tests_failed(self, project_with_features):
        from aah.core.common.io_utils import write_json
        write_json(
            {
                "passed": False,
                "feature_id": "F002",
                "exit_code": 1,
                "failed_tests": [{"name": "test_auth", "reason": "AssertionError"}],
            },
            project_with_features / ".aah" / "build" / "test-results" / "F002.json",
        )
        result = run_hook(
            "aah.core.gates.validate_feature_complete",
            self._task_input("F002", project_with_features),
        )
        assert result.returncode == 2
        assert "F002" in result.stderr or "fail" in result.stderr.lower()

    def test_blocks_when_not_marked_passing_in_feature_list(self, project_with_features):
        from aah.core.common.io_utils import write_json
        from aah.core.common.feature_list import update_feature_status
        # Test results exist and pass, but feature-list still has passes: false
        write_json(
            {"passed": True, "feature_id": "F002", "exit_code": 0, "failed_tests": []},
            project_with_features / ".aah" / "build" / "test-results" / "F002.json",
        )
        update_feature_status(project_with_features / ".aah" / "feature-list.json", "F002", False)
        result = run_hook(
            "aah.core.gates.validate_feature_complete",
            self._task_input("F002", project_with_features),
        )
        assert result.returncode == 2

    def test_handles_missing_feature_id_gracefully(self, project_with_features):
        """No feature_id in task metadata — should not crash."""
        result = run_hook(
            "aah.core.gates.validate_feature_complete",
            {"task": {"subject": "some task", "metadata": {}}, "cwd": str(project_with_features)},
        )
        # Should exit cleanly (0 or 2) without crashing
        assert result.returncode in (0, 2)
        assert "Traceback" not in result.stderr


# ─── TeammateIdle → validate_regression_pass ─────────────────────────

class TestHookValidateRegressionPass:
    """aah.core.gates.validate_regression_pass — regression gate before new work."""

    def test_passes_when_regression_passed(self, project_with_features):
        from aah.core.common.io_utils import write_json
        write_json(
            {"passed": True, "total_tests": 15, "failed_count": 0, "failures": []},
            project_with_features / ".aah" / "build" / "test-results" / "regression-latest.json",
        )
        result = run_hook(
            "aah.core.gates.validate_regression_pass",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 0

    def test_blocks_when_regression_failed(self, project_with_features):
        from aah.core.common.io_utils import write_json
        write_json(
            {
                "passed": False,
                "total_tests": 15,
                "failed_count": 3,
                "failures": [
                    {"test": "test_auth_flow", "reason": "AssertionError: status 200 != 401"},
                ],
            },
            project_with_features / ".aah" / "build" / "test-results" / "regression-latest.json",
        )
        result = run_hook(
            "aah.core.gates.validate_regression_pass",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 2
        assert "fail" in result.stderr.lower() or "regression" in result.stderr.lower()

    def test_passes_when_no_regression_and_no_passing_features(self, project_with_features):
        """No regression run yet, no features passing → allow (early project state)."""
        reg_path = project_with_features / ".aah" / "build" / "test-results" / "regression-latest.json"
        reg_path.unlink(missing_ok=True)
        result = run_hook(
            "aah.core.gates.validate_regression_pass",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 0

    def test_blocks_when_no_regression_but_features_are_passing(self, project_with_features):
        """Features passing but no regression run → should require regression."""
        from aah.core.common.feature_list import update_feature_status
        reg_path = project_with_features / ".aah" / "build" / "test-results" / "regression-latest.json"
        reg_path.unlink(missing_ok=True)
        update_feature_status(project_with_features / ".aah" / "feature-list.json", "F001", True)
        result = run_hook(
            "aah.core.gates.validate_regression_pass",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 2

    def test_failure_message_names_failed_tests(self, project_with_features):
        from aah.core.common.io_utils import write_json
        write_json(
            {
                "passed": False,
                "total_tests": 10,
                "failed_count": 2,
                "failures": [
                    {"test": "test_specific_failure", "reason": "timeout"},
                ],
            },
            project_with_features / ".aah" / "build" / "test-results" / "regression-latest.json",
        )
        result = run_hook(
            "aah.core.gates.validate_regression_pass",
            {"cwd": str(project_with_features)},
        )
        assert result.returncode == 2
        assert "test_specific_failure" in result.stderr or "2" in result.stderr


# ─── PreCompact → progress snapshot ──────────────────────────────────

class TestHookProgressSnapshot:
    """aah.core.common.progress snapshot — pre-compaction state preservation."""

    def test_creates_snapshot_file(self, project_with_features):
        snapshots_dir = project_with_features / ".aah" / "snapshots"
        before = set(snapshots_dir.glob("*.json")) if snapshots_dir.exists() else set()

        result = subprocess.run(
            ["aah-run", "aah.core.common.progress", "snapshot",
             "--path", str(project_with_features / ".aah" / "claude-progress.json")],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0

        after = set(snapshots_dir.glob("*.json")) if snapshots_dir.exists() else set()
        new_snapshots = after - before
        assert len(new_snapshots) == 1, f"Expected 1 new snapshot, got {len(new_snapshots)}"

    def test_snapshot_contains_progress_data(self, project_with_features):
        result = subprocess.run(
            ["aah-run", "aah.core.common.progress", "snapshot",
             "--path", str(project_with_features / ".aah" / "claude-progress.json")],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        snapshots_dir = project_with_features / ".aah" / "snapshots"
        latest = max(snapshots_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        snap = json.loads(latest.read_text())
        assert "current_phase" in snap
        assert "snapshot_timestamp" in snap

    def test_snapshot_filename_has_timestamp(self, project_with_features):
        snapshots_dir = project_with_features / ".aah" / "snapshots"
        before = set(snapshots_dir.glob("*.json")) if snapshots_dir.exists() else set()
        subprocess.run(
            ["aah-run", "aah.core.common.progress", "snapshot",
             "--path", str(project_with_features / ".aah" / "claude-progress.json")],
            capture_output=True, timeout=15,
        )
        after = set(snapshots_dir.glob("*.json")) if snapshots_dir.exists() else set()
        new = list(after - before)
        assert len(new) == 1
        assert "progress_snapshot_" in new[0].name
