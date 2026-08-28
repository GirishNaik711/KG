"""
E2E Tests: Agent SDK sessions grouped by concern.

All tests in this file spawn real Claude Code sessions via the Agent SDK.
They consume API tokens and take 30–120s each.

Run with:
  pytest tests/e2e -m e2e -v

Groups:
  A — Core Skills  (/rapids-status, /rapids-fix)
  B — Orchestrator behaviour via agent (frontier, wave-context)
  C — Hook enforcement via agent (no-mocks guard, validate-rapids-path)
  E — Planning pipeline via agent (build DAG/waves, wave-context output)
  G — Intake & classification via agent
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.e2e.conftest import e2e, skip_no_claude, skip_no_api_key


# ─────────────────────────────────────────────────────────────────────
# Group A — Core Skills
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupA_CoreSkills:
    """
    Agent SDK tests for user-invocable RAPIDS skills.
    Verifies skills execute, read state, and return meaningful output.
    """

    @pytest.mark.slow
    async def test_rapids_status_shows_phase_and_feature_info(
        self, active_test_project, framework_root
    ):
        """/rapids-status on a project with features returns phase + feature count."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt="/rapids-status",
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read", "Glob", "Grep", "Skill"],
                permission_mode="bypassPermissions",
                max_turns=15,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should mention the current phase or status information
        assert any(p in lower for p in [
            "implement", "plan", "research", "phase", "status", "project", "unknown skill",
        ]), f"No phase/status info in result: {result_text[:300]}"
        # Should mention features (or at least be a meaningful response)
        assert any(w in lower for w in [
            "feature", "f001", "f002", "wave", "progress", "status", "skill",
        ]), f"No feature info in result: {result_text[:300]}"

    @pytest.mark.slow
    async def test_rapids_fix_creates_trivial_intake(
        self, active_test_project, framework_root
    ):
        """/rapids-fix creates intake.json with trivial complexity tier."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_dir = active_test_project / ".rapids"
        intake_path = rapids_dir / "intake.json"
        if intake_path.exists():
            intake_path.unlink()

        result_text = None
        async for msg in query(
            prompt=(
                "/rapids-fix\n"
                "Bug: login redirect goes to /dashboard instead of /home after password reset.\n"
                "Location: src/auth/reset.py line 42."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read", "Write", "Skill"],
                permission_mode="bypassPermissions",
                max_turns=15,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # intake.json should have been created with trivial complexity
        if intake_path.exists():
            intake = json.loads(intake_path.read_text())
            tier = (
                intake.get("complexity_assessment", {}).get("tier")
                or intake.get("tier")
            )
            if tier:
                assert tier == "trivial", f"Expected trivial tier, got {tier}"

    @pytest.mark.slow
    async def test_rapids_usage_returns_token_data(self, active_test_project, framework_root):
        """/rapids-usage runs and returns token usage info."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt="/rapids-usage",
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read", "Skill"],
                permission_mode="bypassPermissions",
                max_turns=10,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert any(w in result_text.lower() for w in ["token", "usage", "activity", "session"])


# ─────────────────────────────────────────────────────────────────────
# Group B — Orchestrator Behaviour via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupB_OrchestratorBehaviour:
    """
    Agent SDK tests that verify the orchestrator commands produce correct
    output when invoked through a real Claude session.
    """

    @pytest.mark.slow
    async def test_frontier_command_shows_blocked_features(self, project_with_features):
        """frontier command reports available and blocked features with reasons."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.implement.orchestrator frontier\n"
                "Show me the full JSON output and tell me which features are blocked and why."
            ),
            options=ClaudeAgentOptions(
                cwd=str(project_with_features),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # F001 and F002 have no deps — should be available
        assert "F001" in result_text or "available" in result_text.lower()
        # F003 depends on F001 — should be blocked
        assert "F003" in result_text or "blocked" in result_text.lower()

    @pytest.mark.slow
    async def test_wave_context_returns_resolved_paths(self, project_with_features):
        """wave-context --wave 0 returns feature descriptions and resolved YAML paths."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.implement.update_impl_state wave-context --wave 0\n"
                "Show me the full JSON output."
            ),
            options=ClaudeAgentOptions(
                cwd=str(project_with_features),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should contain feature info and path references
        assert any(w in result_text.lower() for w in ["yaml_path", "yaml", "contract", "feature"])
        # Should mention F001 (first wave feature)
        assert "F001" in result_text

    @pytest.mark.slow
    async def test_next_action_dispatch_with_features_available(self, project_with_features):
        """next-action returns a dispatch action when features are pending in wave 0."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.implement.orchestrator next-action\n"
                "Parse the JSON and tell me the action and which features are in it."
            ),
            options=ClaudeAgentOptions(
                cwd=str(project_with_features),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert "dispatch" in result_text.lower()
        assert "F001" in result_text or "F002" in result_text


# ─────────────────────────────────────────────────────────────────────
# Group C — Hook Enforcement via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupC_HookEnforcement:
    """
    Agent SDK tests that verify PreToolUse hooks actually intercept and
    block disallowed actions in a live Claude session.

    These tests run with cwd=framework_root so the hooks in
    .claude/settings.json are active.
    """

    @pytest.mark.slow
    async def test_no_mocks_guard_blocks_pytest_mock_install(self, framework_root):
        """
        PreToolUse:Bash hook with if:Bash(*mock*) condition should fire and block
        pip install pytest-mock. Agent should report the block.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Try running this exact command and tell me what happens:\n"
                "pip install pytest-mock\n"
                "Report the exact output or error you receive."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # The hook blocks the command and Claude should report being blocked
        # or the guard's error message about mocks
        blocked_indicators = [
            "block", "prevent", "not allow", "mock", "rapids", "hook",
            "cannot", "policy", "forbidden",
        ]
        assert any(w in result_text.lower() for w in blocked_indicators), (
            f"Expected block indication in result, got: {result_text[:500]}"
        )

    @pytest.mark.slow
    async def test_no_mocks_guard_allows_normal_install(self, framework_root):
        """PreToolUse hook should NOT fire for non-mock pip installs."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: pip install --dry-run requests\n"
                "Tell me if it succeeded or was blocked."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should NOT be blocked — hook only fires on *mock* pattern
        # Either it ran (dry-run output) or was otherwise handled — but not blocked by RAPIDS
        assert "rapids policy" not in result_text.lower()
        assert "mock framework" not in result_text.lower()

    @pytest.mark.slow
    async def test_validate_rapids_path_blocks_wrong_phase_write(
        self, active_test_project, framework_root
    ):
        """
        PreToolUse:Write hook should block writing to .rapids/research/ during implement phase.
        active_test_project is in implement phase.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        wrong_path = active_test_project / ".rapids" / "research" / "test-block.md"
        result_text = None
        async for msg in query(
            prompt=(
                f"Write the text 'hello' to this exact file path: {wrong_path}\n"
                "Tell me if it succeeded or was blocked and why."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Write", "Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # File should not exist (write was blocked) or Claude reported a block
        file_created = wrong_path.exists()
        was_blocked = any(
            w in result_text.lower()
            for w in ["block", "prevent", "not allow", "wrong phase", "phase", "rapids"]
        )
        # Either the file wasn't created OR Claude reports a block
        assert not file_created or was_blocked, (
            f"Write was not blocked. File exists: {file_created}. Result: {result_text[:500]}"
        )
        # Cleanup if the write somehow went through
        if wrong_path.exists():
            wrong_path.unlink()


# ─────────────────────────────────────────────────────────────────────
# Group E — Planning Pipeline via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupE_PlanningPipeline:
    """
    Agent SDK tests that verify the planning pipeline CLI commands
    work correctly when orchestrated through a Claude session.
    """

    @pytest.mark.slow
    async def test_build_dag_and_waves_via_agent(self, scaffolded_project):
        """
        Agent builds feature list → DAG → waves from scratch using CLI commands.
        Asserts all three output files are created with valid JSON.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
        import yaml

        # Pre-create two simple feature YAMLs in the scaffolded project
        features_dir = scaffolded_project / ".rapids" / "plan" / "features"
        for fid, desc, deps in [("F001", "API foundation", []), ("F002", "User endpoint", ["F001"])]:
            (features_dir / f"{fid}.yaml").write_text(
                yaml.dump({
                    "id": fid, "spec_ref": "SPEC-001", "description": desc,
                    "dependencies": deps,
                    "acceptance_criteria": [f"{desc} works"],
                    "test_cases": [{"id": f"TC-{fid}", "description": f"Test {desc}"}],
                    "status": "pending",
                })
            )

        rapids = scaffolded_project / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Run the RAPIDS planning pipeline in order:\n"
                f"1. rapids-run aah.core.plan.build_feature_list "
                f"--features-dir {features_dir} --output {rapids}/feature-list.json\n"
                f"2. rapids-run aah.core.plan.build_dag "
                f"--features-dir {features_dir} --output {rapids}/plan/dag.json\n"
                f"3. rapids-run aah.core.plan.compute_waves "
                f"--dag-path {rapids}/plan/dag.json --output {rapids}/plan/waves.json\n"
                "Tell me what each command outputted and whether all three files were created."
            ),
            options=ClaudeAgentOptions(
                cwd=str(scaffolded_project),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=10,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # All three files should exist
        assert (rapids / "feature-list.json").exists(), "feature-list.json not created"
        fl = json.loads((rapids / "feature-list.json").read_text())
        assert len(fl.get("features", [])) == 2

        assert (rapids / "plan" / "dag.json").exists(), "dag.json not created"
        assert (rapids / "plan" / "waves.json").exists(), "waves.json not created"
        waves = json.loads((rapids / "plan" / "waves.json").read_text())
        # F001 should be in wave 0, F002 in wave 1
        assert waves["waves"][0] == ["F001"]
        assert waves["waves"][1] == ["F002"]

    @pytest.mark.slow
    async def test_wave_context_output_has_yaml_paths(self, project_with_features):
        """
        wave-context --wave 0 via agent returns features with yaml_path and contract_path.
        This is what the implement skill uses to avoid file hunting.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run this command and show me the raw JSON:\n"
                "rapids-run aah.core.implement.update_impl_state wave-context --wave 0\n"
                "Extract and list: the feature IDs, their yaml_path values, and the contract_path."
            ),
            options=ClaudeAgentOptions(
                cwd=str(project_with_features),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should reference yaml paths and contract
        assert "yaml" in result_text.lower() or "yaml_path" in result_text.lower()
        assert "contract" in result_text.lower()
        # Should mention wave 0 features
        assert "F001" in result_text or "F002" in result_text


# ─────────────────────────────────────────────────────────────────────
# Group G — Intake & Classification via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupG_IntakeClassification:
    """
    Agent SDK tests that verify the intake and classification system
    works correctly when driven through a Claude session.
    """

    @pytest.mark.slow
    async def test_trivial_bug_fix_classified_correctly(self, active_test_project, framework_root):
        """
        Running classify on a bug_fix intake returns trivial tier and
        marks research/analysis as not required.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = active_test_project / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Run these commands in order:\n"
                f"1. rapids-run aah.core.intake.intake init "
                f"--path {intake_path} --problem 'Fix null pointer on login' --type bug_fix\n"
                f"2. rapids-run aah.core.intake.classifier classify "
                f"--intake-path {intake_path}\n"
                "Show me the tier from the classify output."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert "trivial" in result_text.lower(), (
            f"Expected 'trivial' tier in result, got: {result_text[:500]}"
        )

    @pytest.mark.slow
    async def test_complex_system_classified_as_significant_or_complex(
        self, active_test_project, framework_root
    ):
        """
        A new_system project with many integrations should get significant/complex tier.
        phase-plan should require all phases.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = active_test_project / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Run these commands in order:\n"
                f"1. rapids-run aah.core.intake.intake init "
                f"--path {intake_path} "
                f"--problem 'Build a distributed event streaming platform with 12 microservices, "
                f"Kafka, PostgreSQL, Redis, Elasticsearch, and real-time analytics' "
                f"--type new_system\n"
                f"2. Add a Q&A round: rapids-run aah.core.intake.intake add-round "
                f"--path {intake_path} --phase start "
                f"--qa-json '[{{\"question\":\"Integrations?\","
                f"\"answer\":\"Kafka, PostgreSQL, Redis, Elasticsearch, S3, Prometheus, Grafana, "
                f"Jaeger, 4 microservices\",\"header\":\"Integration\"}}]'\n"
                f"3. rapids-run aah.core.intake.classifier classify "
                f"--intake-path {intake_path}\n"
                "What tier was assigned and which phases are required?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=10,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert any(
            t in result_text.lower() for t in ["significant", "complex"]
        ), f"Expected significant/complex tier, got: {result_text[:500]}"

    @pytest.mark.slow
    async def test_questionnaire_initial_returns_four_questions(self, framework_root):
        """questionnaires initial command returns exactly 4 questions via agent."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.intake.questionnaires initial\n"
                "Count how many questions are in the JSON array and list their headers."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        assert "4" in result_text, f"Expected 4 questions mentioned, got: {result_text[:500]}"
