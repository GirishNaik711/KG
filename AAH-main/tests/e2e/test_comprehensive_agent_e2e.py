"""
Comprehensive Agent SDK E2E Tests for RAPIDS Delivery Framework.

Tests the entire RAPIDS framework through real Claude Agent SDK sessions,
covering every major subsystem and workflow. Organized into groups:

  Q — Scenario Detection Engine via Agent (5 tests)
  R — Iteration Lifecycle via Agent (5 tests)
  S — Codebase Profiler via Agent (3 tests)
  T — Cross-Phase Navigation via Agent (4 tests)
  U — Multi-Project-Type & Registry via Agent (5 tests)
  V — Error Handling & Edge Cases via Agent (4 tests)
  W — Deploy & Sustain Phase Activities via Agent (4 tests)
  X — Intake & Classification Workflow via Agent (4 tests)
  Y — Activity Depth & Dependency Analysis via Agent (4 tests)
  Z — Full Workflow Integration via Agent (3 tests)

Run with:
  pytest tests/e2e/test_comprehensive_agent_e2e.py -m e2e -v
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.e2e.conftest import e2e, skip_no_claude, skip_no_api_key, AgentSession


# ─── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def framework_root() -> Path:
    """The RAPIDS framework root directory."""
    current = Path(__file__).parent
    for d in [current, *current.parents]:
        if (d / "CLAUDE.md").exists():
            return d
    return Path(__file__).parent.parent.parent.parent


@pytest.fixture
def greenfield_dir(tmp_path) -> Path:
    """An empty directory simulating a greenfield project."""
    d = tmp_path / "greenfield-project"
    d.mkdir()
    return d


@pytest.fixture
def brownfield_dir(tmp_path) -> Path:
    """A directory with source files simulating a brownfield project."""
    d = tmp_path / "brownfield-project"
    d.mkdir()
    src = d / "src"
    src.mkdir()
    (src / "main.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    (src / "models.py").write_text("class User:\n    pass\n")
    (src / "utils.py").write_text("def helper(): pass\n")
    (d / "requirements.txt").write_text("flask==3.0\nsqlalchemy==2.0\n")
    (d / "Dockerfile").write_text("FROM python:3.12\nCOPY . /app\n")
    return d


@pytest.fixture
def rapids_project_dir(tmp_path) -> Path:
    """A directory simulating a project with .rapids/ and iteration history."""
    d = tmp_path / "rapids-project"
    d.mkdir()
    rapids = d / ".rapids"
    rapids.mkdir()
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text("print('hello')\n")

    # Write iteration history with one completed iteration
    history = {
        "iterations": [
            {
                "number": 1,
                "started": "2026-03-01",
                "completed": "2026-03-15",
                "scenario": "S1",
                "scope": "Initial build of auth system",
                "project_types": ["ai-infra-platforms"],
                "features_built": ["F001", "F002", "F003"],
                "key_decisions": ["ADR-001: Use FastAPI", "ADR-002: PostgreSQL"],
                "artifacts_produced": ["research/framework-eval.md", "analysis/architecture.md"],
                "status": "completed",
            }
        ]
    }
    with open(rapids / "iteration-history.yaml", "w") as f:
        yaml.dump(history, f)

    # Create some research/analysis artifacts for synthesis
    (rapids / "research").mkdir()
    (rapids / "research" / "framework-eval.md").write_text(
        "# Framework Evaluation\n\n## Problem Framing\nEvaluated FastAPI vs Flask.\n\n"
        "## Findings\nFastAPI chosen for async support.\n"
    )
    (rapids / "analysis").mkdir()
    (rapids / "analysis" / "architecture.md").write_text(
        "# Architecture\n\n## Overview\nMicroservices with event bus.\n\n"
        "## Components\nAPI Gateway, Auth Service, Agent Pool.\n"
    )
    return d


@pytest.fixture
def activity_library_path(framework_root) -> Path:
    """Path to the activity library."""
    return framework_root / "activity-library"


# ─────────────────────────────────────────────────────────────────────
# Group Q — Scenario Detection Engine via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupQ_ScenarioDetection:
    """
    Agent SDK tests for the Scenario Detection Engine.
    Verifies the agent can assess project state, classify problems,
    and build phase trajectories.
    """

    @pytest.mark.slow
    async def test_assess_greenfield_project(self, framework_root, greenfield_dir):
        """Agent assesses an empty directory as greenfield."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.scenario assess "
                f"--project-path {greenfield_dir}\n"
                "Is it greenfield or brownfield? How many source files were found?"
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
        assert "greenfield" in result_text.lower()

    @pytest.mark.slow
    async def test_assess_brownfield_project(self, framework_root, brownfield_dir):
        """Agent assesses a directory with source files as brownfield."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.scenario assess "
                f"--project-path {brownfield_dir}\n"
                "Is it greenfield or brownfield? What source files exist?"
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
        # Should not be greenfield since source files exist
        assert any(
            w in result_text.lower()
            for w in ["brownfield", "not greenfield", "source file", "existing"]
        )

    @pytest.mark.slow
    async def test_detect_full_scenario_pipeline(self, framework_root, brownfield_dir):
        """Agent runs full scenario detection and reports scenario ID + trajectory."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.scenario detect "
                f"--project-path {brownfield_dir} "
                f"--problem-statement 'Add a comprehensive authentication system with OAuth2, "
                f"JWT tokens, and role-based access control'\n"
                "What scenario ID was detected? What phases are in the trajectory?"
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
        # Should have a scenario ID (S1-S9)
        assert any(f"S{i}" in result_text for i in range(1, 10))
        # Should mention phases
        assert any(
            phase in result_text.lower()
            for phase in ["research", "analysis", "plan", "implement"]
        )

    @pytest.mark.slow
    async def test_trajectory_shows_phase_depths(self, framework_root):
        """Agent builds trajectory for S1 and shows phase depth levels."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.scenario trajectory --scenario S1\n"
                "What phases are included? What depth is each phase set to?"
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
        # S1 is greenfield full build — should include all major phases
        assert "research" in result_text.lower()
        assert "implement" in result_text.lower()
        # Should mention depth
        assert any(d in result_text.lower() for d in ["standard", "deep", "light", "depth"])

    @pytest.mark.slow
    async def test_bug_fix_trajectory_is_minimal(self, framework_root):
        """Agent builds S7 (bug fix) trajectory with minimal phases."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.scenario trajectory --scenario S7\n"
                "How many phases does S7 have? Which ones are skipped?"
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
        # S7 bug fix should skip heavy phases
        assert any(
            w in result_text.lower()
            for w in ["skip", "minimal", "light", "plan", "implement", "not required", "false"]
        )


# ─────────────────────────────────────────────────────────────────────
# Group R — Iteration Lifecycle via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupR_IterationLifecycle:
    """
    Agent SDK tests for the Iteration Lifecycle Manager.
    Verifies the agent can start, complete, recap, and synthesize iterations.
    """

    @pytest.mark.slow
    async def test_start_iteration_via_agent(self, framework_root, tmp_path):
        """Agent starts a new iteration and reports the iteration number."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration start "
                f"--rapids-path {rapids_path} "
                f"--scenario S4 --scope 'Add user dashboard with analytics' "
                f"--project-type ai-infra-platforms\n"
                "What iteration number was created? What's the status?"
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
        # First iteration should be number 1
        assert "1" in result_text
        assert any(w in result_text.lower() for w in ["in_progress", "started", "progress"])

    @pytest.mark.slow
    async def test_complete_iteration_via_agent(self, framework_root, tmp_path):
        """Agent completes an in-progress iteration with features and decisions."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        # Start an iteration first
        subprocess.run(
            ["rapids-run", "aah.core.activities.iteration", "start",
             "--rapids-path", str(rapids_path),
             "--scenario", "S4", "--scope", "Build auth system",
             "--project-type", "ai-infra-platforms"],
            capture_output=True, check=True, timeout=30,
        )

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration complete "
                f"--rapids-path {rapids_path} "
                f"--features F001,F002,F003 "
                f"--decisions 'ADR-001: Use JWT,ADR-002: PostgreSQL' "
                f"--artifacts 'research/eval.md,analysis/arch.md'\n"
                "What's the completion date? How many features were built?"
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
        # Should report 3 features
        assert "3" in result_text or "F001" in result_text
        assert any(w in result_text.lower() for w in ["completed", "complete", "2026"])

    @pytest.mark.slow
    async def test_recap_for_returning_user(self, framework_root, rapids_project_dir):
        """Agent generates re-entry recap for a returning user."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = rapids_project_dir / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration recap "
                f"--rapids-path {rapids_path}\n"
                "What iteration was last completed? How many features were built? "
                "What key decisions were made? What artifacts are available?"
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
        # Should reference iteration 1
        assert "1" in result_text
        # Should mention features
        assert any(w in result_text for w in ["F001", "F002", "3", "features"])
        # Should mention decisions or artifacts
        assert any(
            w in result_text.lower()
            for w in ["fastapi", "postgresql", "decision", "artifact", "adr"]
        )

    @pytest.mark.slow
    async def test_synthesize_prior_intelligence(self, framework_root, rapids_project_dir):
        """Agent synthesizes prior artifacts into intelligence summary."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = rapids_project_dir / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration synthesize "
                f"--rapids-path {rapids_path}\n"
                "How many research and analysis artifacts exist? "
                "What are the artifact titles?"
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
        # Should find the research and analysis artifacts
        assert any(
            w in result_text.lower()
            for w in ["research", "analysis", "framework", "architecture", "artifact"]
        )

    @pytest.mark.slow
    async def test_iteration_history_after_multiple(self, framework_root, tmp_path):
        """Agent reports full iteration history after start+complete+start."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        # Start and complete iteration 1
        subprocess.run(
            ["rapids-run", "aah.core.activities.iteration", "start",
             "--rapids-path", str(rapids_path),
             "--scenario", "S1", "--scope", "Initial build",
             "--project-type", "ai-infra-platforms"],
            capture_output=True, check=True, timeout=30,
        )
        subprocess.run(
            ["rapids-run", "aah.core.activities.iteration", "complete",
             "--rapids-path", str(rapids_path),
             "--features", "F001,F002"],
            capture_output=True, check=True, timeout=30,
        )
        # Start iteration 2
        subprocess.run(
            ["rapids-run", "aah.core.activities.iteration", "start",
             "--rapids-path", str(rapids_path),
             "--scenario", "S4", "--scope", "Add dashboard",
             "--project-type", "ai-infra-platforms"],
            capture_output=True, check=True, timeout=30,
        )

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration history "
                f"--rapids-path {rapids_path}\n"
                "How many iterations exist? What are their statuses and scenarios?"
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
        # Should show 2 iterations
        assert "2" in result_text
        # Should have both completed and in_progress
        assert any(w in result_text.lower() for w in ["completed", "complete"])
        assert any(w in result_text.lower() for w in ["in_progress", "in progress", "progress"])


# ─────────────────────────────────────────────────────────────────────
# Group S — Codebase Profiler via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupS_CodebaseProfiler:
    """
    Agent SDK tests for the codebase profiler script.
    Verifies the agent can run the profiler and interpret results.
    """

    @pytest.mark.slow
    async def test_profiler_reports_file_inventory(self, framework_root, brownfield_dir, tmp_path):
        """Agent runs profiler and reports file count, languages, and structure."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        output = tmp_path / "profile.json"
        profiler = framework_root / "aah" / "core" / "intel" / "codebase_profiler.py"

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: python3 {profiler} {brownfield_dir} --output {output}\n"
                f"Then read the JSON output at {output} using Bash (cat {output} | head -60).\n"
                "How many files were scanned? What languages were detected? "
                "What frameworks were found?"
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
        # Should find Python files
        assert any(w in result_text.lower() for w in ["python", ".py", "5", "file"])
        # Should detect Flask or requirements
        assert any(w in result_text.lower() for w in ["flask", "framework", "requirement"])

    @pytest.mark.slow
    async def test_profiler_on_rapids_reports_architecture(self, framework_root, tmp_path):
        """Agent profiles the RAPIDS codebase itself and reports architectural stats."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        output = tmp_path / "rapids-profile.json"
        profiler = framework_root / "aah" / "core" / "intel" / "codebase_profiler.py"

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: python3 {profiler} {framework_root}/scripts --output {output} --max-files 1000 --max-parse 500\n"
                f"Then run: cat {output} | python3 -c \"import json,sys; d=json.load(sys.stdin); "
                f"print('files:', d['summary']['total_files']); "
                f"print('symbols:', d['summary']['total_symbols']); "
                f"print('languages:', list(d['summary']['languages'].keys()))\"\n"
                "How many files and symbols? What languages were found? "
                "How many entry points and hotspots?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=12,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None, "Agent produced no ResultMessage — may have exhausted max_turns"
        # Should find Python as the primary language
        assert "python" in result_text.lower() or "Python" in result_text
        # Should report symbol count
        assert any(w in result_text.lower() for w in ["symbol", "class", "function"])

    @pytest.mark.slow
    async def test_profiler_detects_roles_and_coupling(self, framework_root, brownfield_dir, tmp_path):
        """Agent runs profiler and reports role classifications and coupling analysis."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        output = tmp_path / "profile.json"
        profiler = framework_root / "aah" / "core" / "intel" / "codebase_profiler.py"

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: python3 {profiler} {brownfield_dir} --output {output}\n"
                f"Then run: cat {output} | python3 -c \"import json,sys; d=json.load(sys.stdin); "
                f"print('entry_points:', d.get('entry_points',[])); "
                f"print('config_files:', d.get('config_files',[])); "
                f"print('data_model_files:', d.get('data_model_files',[]))\"\n"
                "Which files were classified as entry points? Config files? "
                "Data model files? What's the coupling analysis?"
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
        # Should identify main.py as entry point and requirements.txt/Dockerfile as config
        assert any(
            w in result_text.lower()
            for w in ["main.py", "entry", "requirements", "config", "dockerfile"]
        )


# ─────────────────────────────────────────────────────────────────────
# Group T — Cross-Phase Navigation via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupT_CrossPhaseNavigation:
    """
    Agent SDK tests for cross-phase navigation and gate validation.
    Verifies the agent can determine phase requirements, validate gates,
    and navigate the delivery lifecycle.
    """

    @pytest.mark.slow
    async def test_phase_plan_for_trivial_tier(self, framework_root):
        """Agent gets phase plan for trivial tier and confirms skipped phases."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.intake.classifier phase-plan --tier trivial\n"
                "Which phases are required and which are skipped for a trivial task?"
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
        # Trivial should skip research and analysis
        assert any(
            w in result_text.lower()
            for w in ["skip", "false", "not required", "no research"]
        )
        # Plan and implement should be required
        assert any(w in result_text.lower() for w in ["plan", "implement", "required", "true"])

    @pytest.mark.slow
    async def test_phase_plan_for_complex_tier(self, framework_root):
        """Agent gets phase plan for complex tier — all phases required."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.intake.classifier phase-plan --tier complex\n"
                "Which phases are required? What depth level for each?"
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
        # Complex tier should require all phases
        assert "research" in result_text.lower()
        assert "analysis" in result_text.lower()
        # Should mention depth
        assert any(d in result_text.lower() for d in ["deep", "standard", "depth"])

    @pytest.mark.slow
    async def test_scenario_trajectory_matches_phase_plan(self, framework_root):
        """Agent compares S1 trajectory with complex phase plan for consistency."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run these two commands and compare:\n"
                "1. rapids-run aah.core.activities.scenario trajectory --scenario S1\n"
                "2. rapids-run aah.core.intake.classifier phase-plan --tier complex\n"
                "Do both require research and analysis phases? "
                "Are the depth levels consistent?"
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
        # Agent should confirm both require research and analysis
        assert "research" in result_text.lower()
        assert "analysis" in result_text.lower()

    @pytest.mark.slow
    async def test_questionnaire_initial_returns_questions(self, framework_root):
        """Agent retrieves initial intake questions and reports structure."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.intake.questionnaires initial\n"
                "How many questions are returned? What topics do they cover?"
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
        # Should mention questions
        assert any(w in result_text.lower() for w in ["question", "4", "topic", "initial"])


# ─────────────────────────────────────────────────────────────────────
# Group U — Multi-Project-Type & Registry via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupU_MultiProjectTypeRegistry:
    """
    Agent SDK tests for multi-project-type handling and registry consistency.
    Verifies activity filtering, union/deduplication, and registry accuracy.
    """

    @pytest.mark.slow
    async def test_three_project_types_union(self, framework_root):
        """Agent lists activities for 3 combined project types and counts unique activities."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run these three commands and compare counts:\n"
                "1. rapids-run aah.core.activities.loader list --project-type ai-infra-platforms --format ids | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'\n"
                "2. rapids-run aah.core.activities.loader list --project-type data-pipelines --format ids | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'\n"
                "3. rapids-run aah.core.activities.loader list --project-type ai-infra-platforms data-pipelines llm-ops --format ids | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'\n"
                "How many activities does each individual type have? "
                "How many in the combined set? Is the combined set larger than any individual?"
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
        # ai-infra-platforms=23, data-pipelines=24, combined with llm-ops should be 50 (all)
        assert "50" in result_text or "combined" in result_text.lower()
        assert "23" in result_text or "24" in result_text

    @pytest.mark.slow
    async def test_registry_activity_count_matches_yaml_files(self, framework_root):
        """Agent counts activities in registry vs YAML files on disk."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "I want to verify the activity registry is consistent. Run:\n"
                "1. rapids-run aah.core.activities.loader validate\n"
                "2. Count YAML files: find activity-library -name '*.yaml' "
                "-not -name '_*' -not -name 'SCHEMA*' | grep -v _templates | grep -v _contributions | grep -v _feedback | wc -l\n"
                "Does the number of activities in the registry match the number of YAML files?"
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
        # Should report consistency
        assert any(
            w in result_text.lower()
            for w in ["39", "match", "consistent", "valid", "all"]
        )

    @pytest.mark.slow
    async def test_phase_filtering_research_only(self, framework_root):
        """Agent lists only research-phase activities for ai-infra-platforms."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader list "
                "--project-type ai-infra-platforms --phase research --format ids\n"
                "How many research activities? List them all."
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
        # Should have 6 research activities (3 shared + 3 AI)
        assert "R-" in result_text
        # Should NOT include analysis activities
        assert "A-AI-architecture" not in result_text

    @pytest.mark.slow
    async def test_phase_filtering_analysis_only(self, framework_root):
        """Agent lists only analysis-phase activities for data-pipelines."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader list "
                "--project-type data-pipelines --phase analysis --format ids\n"
                "How many analysis activities for data-pipelines? List them."
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
        # Should have analysis activities
        assert "A-" in result_text
        # Should include data-specific
        assert any(w in result_text for w in ["A-DATA", "DATA", "data"])

    @pytest.mark.slow
    async def test_all_project_types_listed_in_registry(self, framework_root):
        """Agent verifies all 8 project types are defined in the registry."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Read the activity-library/_registry.yaml file and tell me: "
                "How many project types are defined? List each type ID and name."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should find all 8 project types
        assert "8" in result_text or "eight" in result_text.lower()
        assert "ai-infra-platforms" in result_text
        assert "data-pipelines" in result_text
        assert "llm-ops" in result_text


# ─────────────────────────────────────────────────────────────────────
# Group V — Error Handling & Edge Cases via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupV_ErrorHandling:
    """
    Agent SDK tests for error handling and edge cases.
    Verifies the agent gracefully handles invalid inputs, missing data,
    and boundary conditions.
    """

    @pytest.mark.slow
    async def test_unknown_project_type_returns_shared_only(self, framework_root):
        """Agent handles unknown project type by returning only shared activities."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader list "
                "--project-type blockchain --format ids\n"
                "How many activities are returned for this unknown project type? "
                "Are they all shared activities?"
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
        # Should return shared-only activities (15)
        assert any(w in result_text for w in ["SHARED", "shared", "15"])

    @pytest.mark.slow
    async def test_duplicate_iteration_start_rejected(self, framework_root, tmp_path):
        """Agent reports error when trying to start a second iteration."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        # Start first iteration
        subprocess.run(
            ["rapids-run", "aah.core.activities.iteration", "start",
             "--rapids-path", str(rapids_path),
             "--scenario", "S4", "--scope", "First iteration",
             "--project-type", "ai-infra-platforms"],
            capture_output=True, check=True, timeout=30,
        )

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration start "
                f"--rapids-path {rapids_path} "
                f"--scenario S5 --scope 'Second iteration attempt' "
                f"--project-type ai-infra-platforms\n"
                "Did the command succeed or fail? What error message was shown?"
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
        # Should report an error about existing iteration
        assert any(
            w in result_text.lower()
            for w in ["error", "already", "in progress", "fail", "complete it"]
        )

    @pytest.mark.slow
    async def test_complete_without_in_progress_rejected(self, framework_root, tmp_path):
        """Agent reports error when completing without an in-progress iteration."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.activities.iteration complete "
                f"--rapids-path {rapids_path} "
                f"--features F001\n"
                "Did the command succeed or fail? What was the error?"
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
        assert any(
            w in result_text.lower()
            for w in ["error", "no iteration", "fail", "not", "in_progress"]
        )

    @pytest.mark.slow
    async def test_empty_problem_statement_classified(self, framework_root):
        """Agent handles empty problem statement in fast-path detection."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.fast_path detect "
                "--problem-statement ''\n"
                "What classification did it get? Was there an error or a default?"
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
        # Should return something (either a classification or error)
        assert len(result_text) > 20


# ─────────────────────────────────────────────────────────────────────
# Group W — Deploy & Sustain Phase Activities via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupW_DeploySustainActivities:
    """
    Agent SDK tests for deploy and sustain phase activities.
    Verifies these newer activities are properly registered and filterable.
    """

    @pytest.mark.slow
    async def test_deploy_activities_for_ai_agents(self, framework_root):
        """Agent lists deploy activities for ai-infra-platforms and finds domain-specific ones."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader list "
                "--project-type ai-infra-platforms --format summary | head -80\n"
                "Which deploy (D-) activities are available? "
                "Is D-AI-model-serving included? Is D-DATA-pipeline-deploy excluded?"
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

        assert result_text is not None, "Agent produced no ResultMessage — may have exhausted max_turns"
        # Should include AI-specific deploy activity
        assert any(w in result_text for w in ["D-AI", "model-serving", "D-SHARED"])
        # Should NOT include data-pipelines deploy
        assert "D-DATA" not in result_text or "not" in result_text.lower() or "exclu" in result_text.lower()

    @pytest.mark.slow
    async def test_sustain_activities_for_llm_ops(self, framework_root):
        """Agent lists sustain activities for llm-ops and finds model-drift."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader list "
                "--project-type llm-ops --format summary | head -80\n"
                "Which sustain (S-) activities are available for llm-ops? "
                "Is S-MLOPS-model-drift included?"
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
        # Should include MLOPS sustain activity
        assert any(
            w in result_text
            for w in ["S-MLOPS", "model-drift", "drift", "S-SHARED"]
        )

    @pytest.mark.slow
    async def test_brownfield_activities_in_dag(self, framework_root):
        """Agent builds DAG including brownfield activities and shows their wave."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader dag "
                "--project-type ai-infra-platforms\n"
                "Are there brownfield (B-) activities in the DAG? "
                "What wave are they in? Do they depend on anything?"
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

        assert result_text is not None, "Agent returned no result — may need more turns"
        # Should mention brownfield activities
        assert any(w in result_text for w in ["B-SHARED", "brownfield", "B-"]), (
            f"No brownfield activity reference in result: {result_text[:300]}"
        )
        # Should mention waves
        assert "wave" in result_text.lower(), (
            f"No wave reference in result: {result_text[:300]}"
        )

    @pytest.mark.slow
    async def test_deploy_sustain_dependency_chain(self, framework_root):
        """Agent verifies deploy activities depend on infrastructure design."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Read the activity YAML file at "
                "activity-library/deploy/shared/cicd-pipeline.yaml "
                "and tell me: What are its dependencies? "
                "Does it depend on D-SHARED-infra-design?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should confirm dependency on infra-design
        assert any(
            w in result_text
            for w in ["D-SHARED-infra-design", "infra-design", "infrastructure", "depends"]
        )


# ─────────────────────────────────────────────────────────────────────
# Group X — Intake & Classification Workflow via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupX_IntakeClassification:
    """
    Agent SDK tests for the intake and classification workflow.
    Verifies the agent can initialize intake, classify complexity,
    and generate phase plans.
    """

    @pytest.mark.slow
    async def test_classify_trivial_bug_fix(self, framework_root, tmp_path):
        """Agent classifies a trivial bug fix and reports complexity tier."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        # Create a minimal intake.json
        intake = {
            "version": "1.0",
            "status": "in_progress",
            "problem_statement": "Fix null pointer in login handler",
            "project_type": "bug_fix",
            "rounds": [
                {
                    "phase": "init",
                    "timestamp": "2026-04-09T00:00:00Z",
                    "questions": [
                        {"question": "Scope?", "header": "scope", "answer": "Single file fix"}
                    ],
                }
            ],
        }
        with open(rapids_path / "intake.json", "w") as f:
            json.dump(intake, f)

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.intake.classifier classify "
                f"--intake-path {rapids_path / 'intake.json'}\n"
                "What complexity tier was assigned? Is this a trivial task?"
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
        assert "trivial" in result_text.lower()

    @pytest.mark.slow
    async def test_phase_plan_moderate_with_project_type(self, framework_root):
        """Agent gets phase plan for moderate tier with ai-infra-platforms type."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.intake.classifier phase-plan "
                "--tier moderate --type ai-infra-platforms\n"
                "Which phases are required? What's the depth for research and analysis?"
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
        # Moderate should require research and analysis
        assert "research" in result_text.lower()
        assert any(w in result_text.lower() for w in ["standard", "light", "depth"])

    @pytest.mark.slow
    async def test_is_trivial_check(self, framework_root, tmp_path):
        """Agent uses is-trivial quick check on a bug fix intake."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()
        intake = {
            "version": "1.0",
            "status": "in_progress",
            "problem_statement": "Fix typo in error message",
            "project_type": "bug_fix",
            "rounds": [],
        }
        with open(rapids_path / "intake.json", "w") as f:
            json.dump(intake, f)

        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.intake.classifier is-trivial "
                f"--intake-path {rapids_path / 'intake.json'}\n"
                "Is this a trivial task? What's the confidence?"
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
        assert any(w in result_text.lower() for w in ["trivial", "true", "yes"])

    @pytest.mark.slow
    async def test_followup_questions_for_topic(self, framework_root):
        """Agent retrieves followup questions for a specific topic."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.intake.questionnaires followup "
                "--topic architecture\n"
                "What followup questions are returned? How many?"
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
        assert any(w in result_text.lower() for w in ["question", "architecture", "followup"])


# ─────────────────────────────────────────────────────────────────────
# Group Y — Activity Depth & Dependency Analysis via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupY_ActivityDepthDependency:
    """
    Agent SDK tests for activity depth levels and dependency analysis.
    Verifies the agent can inspect individual activities and understand
    task breakdowns at different depth levels.
    """

    @pytest.mark.slow
    async def test_activity_depth_levels_comparison(self, framework_root):
        """Agent reads an activity YAML and compares light vs deep task counts."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Read the file activity-library/research/ai-infra-platforms/framework-landscape.yaml "
                "and tell me:\n"
                "1. How many tasks does the 'light' depth level have?\n"
                "2. How many tasks does the 'deep' depth level have?\n"
                "3. What's the difference between light and deep?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should mention light and deep
        assert "light" in result_text.lower()
        assert "deep" in result_text.lower()
        # Deep should have more tasks than light
        assert any(w in result_text.lower() for w in ["more", "additional", "extra", "task"])

    @pytest.mark.slow
    async def test_activity_dependency_chain(self, framework_root):
        """Agent traces dependency chain for A-AI-tool-integration."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Read the activity-library/_registry.yaml and find the activity "
                "A-AI-tool-integration. What are its dependencies? "
                "Then check what THOSE activities depend on. "
                "Show the full dependency chain."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read", "Grep"],
                permission_mode="bypassPermissions",
                max_turns=10,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should trace back through architecture and shared activities
        assert any(
            w in result_text
            for w in ["A-AI-architecture", "A-APP-L8-tool-integration", "R-AI-frameworks"]
        )

    @pytest.mark.slow
    async def test_activity_task_actions_variety(self, framework_root):
        """Agent reads an activity and reports the variety of task actions used."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Read activity-library/research/shared/prior-art-analysis.yaml and tell me:\n"
                "What different action types are used in the tasks? "
                "(e.g., web_search, web_fetch, analyze, write_artifact, etc.)\n"
                "How many tasks of each type?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should identify multiple action types
        assert any(
            action in result_text.lower()
            for action in ["web_search", "analyze", "write_artifact", "web_fetch"]
        )

    @pytest.mark.slow
    async def test_dag_topological_order_valid(self, framework_root):
        """Agent verifies DAG waves respect dependency ordering."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Run: rapids-run aah.core.activities.loader dag "
                "--project-type ai-infra-platforms\n"
                "Do any activities appear in a wave BEFORE their dependencies? "
                "For example, does A-AI-architecture appear before R-AI-frameworks? "
                "Is the topological ordering valid?"
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
        # Should confirm valid ordering
        assert any(
            w in result_text.lower()
            for w in ["valid", "correct", "before", "after", "wave 0", "topological"]
        )


# ─────────────────────────────────────────────────────────────────────
# Group Z — Full Workflow Integration via Agent
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestAgentGroupZ_FullWorkflowIntegration:
    """
    Agent SDK tests for full multi-step workflow integration.
    Verifies the agent can chain multiple RAPIDS commands to complete
    realistic end-to-end workflows.
    """

    @pytest.mark.slow
    async def test_greenfield_intake_to_activities(self, framework_root, greenfield_dir):
        """Agent walks from scenario assessment through activity recommendation."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "I need you to run a sequence of RAPIDS commands for a new project. "
                "Run each command and briefly summarize the output before moving to the next:\n\n"
                f"1. rapids-run aah.core.activities.scenario detect "
                f"--project-path {greenfield_dir} "
                f"--problem-statement 'Build a multi-agent customer support platform "
                f"with autonomous triage and escalation'\n\n"
                "2. rapids-run aah.core.activities.scenario trajectory --scenario S1\n\n"
                "3. rapids-run aah.core.activities.recommend start "
                "--project-type ai-infra-platforms "
                "--problem-statement 'Build a multi-agent customer support platform' "
                "| head -40\n\n"
                "What scenario was detected? What trajectory applies? "
                "How many candidate activities were loaded?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=12,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        # Should detect scenario and show trajectory
        assert any(f"S{i}" in result_text for i in range(1, 10))
        # Should have loaded candidate activities
        assert any(w in result_text.lower() for w in ["candidate", "activities", "23", "loaded"])

    @pytest.mark.slow
    async def test_brownfield_assess_to_iteration(self, framework_root, brownfield_dir, tmp_path):
        """Agent assesses brownfield project, then starts an iteration."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()

        result_text = None
        async for msg in query(
            prompt=(
                "Run these commands in sequence:\n\n"
                f"1. rapids-run aah.core.activities.scenario assess "
                f"--project-path {brownfield_dir}\n\n"
                f"2. rapids-run aah.core.activities.iteration start "
                f"--rapids-path {rapids_path} "
                f"--scenario S3 --scope 'Modernize legacy Flask app' "
                f"--project-type ai-infra-platforms\n\n"
                f"3. rapids-run aah.core.activities.iteration history "
                f"--rapids-path {rapids_path}\n\n"
                "What state is the project in? What iteration was started? "
                "What scenario and scope?"
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
        # Should report iteration 1 started
        assert "1" in result_text
        assert any(w in result_text.lower() for w in ["s3", "started", "in_progress", "modernize"])

    @pytest.mark.slow
    async def test_iteration_complete_to_recap(self, framework_root, tmp_path):
        """Agent completes an iteration then generates a recap for re-entry."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids_path = tmp_path / ".rapids"
        rapids_path.mkdir()
        (rapids_path / "research").mkdir()
        (rapids_path / "research" / "eval.md").write_text(
            "# Evaluation\n\n## Problem Framing\nTest.\n\n## Findings\nDone.\n"
        )

        # Start and complete an iteration via CLI
        subprocess.run(
            ["rapids-run", "aah.core.activities.iteration", "start",
             "--rapids-path", str(rapids_path),
             "--scenario", "S1", "--scope", "Build auth system",
             "--project-type", "ai-infra-platforms"],
            capture_output=True, check=True, timeout=30,
        )

        result_text = None
        async for msg in query(
            prompt=(
                "Run these commands in sequence:\n\n"
                f"1. rapids-run aah.core.activities.iteration complete "
                f"--rapids-path {rapids_path} "
                f"--features F001,F002,F003 "
                f"--decisions 'Use FastAPI,Use PostgreSQL' "
                f"--artifacts 'research/eval.md'\n\n"
                f"2. rapids-run aah.core.activities.iteration recap "
                f"--rapids-path {rapids_path}\n\n"
                f"3. rapids-run aah.core.activities.iteration synthesize "
                f"--rapids-path {rapids_path}\n\n"
                "How many features were completed? What does the recap say? "
                "What artifacts were found during synthesis?"
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
        # Should report 3 features completed
        assert any(w in result_text for w in ["3", "F001", "F002", "F003"])
        # Should show recap info
        assert any(w in result_text.lower() for w in ["recap", "completed", "artifact", "research"])
