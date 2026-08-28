"""
E2E test fixtures using the Claude Agent SDK.

These fixtures orchestrate real Claude Code sessions to test the AAH
framework end-to-end. They require:
  - Claude CLI installed and authenticated
  - ANTHROPIC_API_KEY set
  - aah-run on PATH (uv tool install ./scripts)
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest


# ─── Skip if no Claude CLI ──────────────────────────────────────────

def _claude_available() -> bool:
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _api_key_set() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("CLAUDE_AUTH_TOKEN")
        or os.environ.get("CLAUDE_CODE_USE_BEDROCK")
    )


skip_no_claude = pytest.mark.skipif(
    not _claude_available(), reason="Claude CLI not installed"
)
skip_no_api_key = pytest.mark.skipif(
    not _api_key_set(), reason="ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set"
)
e2e = pytest.mark.e2e


# ─── Session tracker ────────────────────────────────────────────────

@dataclass
class AgentSession:
    """Tracks messages and state from a Claude Agent SDK session."""
    session_id: str | None = None
    messages: list = field(default_factory=list)
    results: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    skills_invoked: list = field(default_factory=list)
    agents_spawned: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def record_message(self, msg):
        self.messages.append(msg)
        # Track session ID
        if hasattr(msg, "type") and msg.type == "system" and hasattr(msg, "subtype") and msg.subtype == "init":
            self.session_id = getattr(msg, "session_id", None) or msg.data.get("session_id")
        # Track results
        if hasattr(msg, "result"):
            self.results.append(msg.result)
        # Track assistant messages for tool calls
        if hasattr(msg, "content"):
            for block in (msg.content if isinstance(msg.content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call = {"name": block.get("name"), "input": block.get("input", {})}
                    self.tool_calls.append(call)
                    if call["name"] == "Skill":
                        self.skills_invoked.append(call["input"].get("skill"))
                    elif call["name"] == "Agent":
                        self.agents_spawned.append(call["input"].get("description", ""))

    @property
    def last_result(self) -> str | None:
        return self.results[-1] if self.results else None

    def assert_skill_ran(self, skill_name: str):
        assert skill_name in self.skills_invoked, (
            f"Skill '{skill_name}' was not invoked. "
            f"Skills invoked: {self.skills_invoked}"
        )

    def assert_agent_spawned(self, description_contains: str):
        for desc in self.agents_spawned:
            if description_contains.lower() in desc.lower():
                return
        raise AssertionError(
            f"No agent spawned with description containing '{description_contains}'. "
            f"Agents spawned: {self.agents_spawned}"
        )


# ─── Protect aah-config.yaml from E2E test mutations ─────────────

@pytest.fixture
def protect_rapids_config(framework_root):
    """
    Save aah-config.yaml before a test and restore it after.
    Use this for any E2E test that invokes skills which modify config
    (e.g. /rapids-init-workspace, /rapids-new-project).
    """
    config_path = framework_root / "aah-config.yaml"
    original = config_path.read_text() if config_path.exists() else None
    yield
    if original is not None:
        config_path.write_text(original)
    elif config_path.exists():
        config_path.unlink()


# ─── Framework root fixture ─────────────────────────────────────────

@pytest.fixture(scope="session")
def framework_root() -> Path:
    """The AAH framework root directory."""
    # Walk up from this test file to find aah-config.yaml
    current = Path(__file__).parent
    for d in [current, *current.parents]:
        if (d / "aah-config.yaml").exists():
            return d
        if (d / "CLAUDE.md").exists():
            return d
    # Fallback
    return Path(__file__).parent.parent.parent.parent


# ─── Isolated workspace fixture ─────────────────────────────────────

@pytest.fixture
def test_workspace(tmp_path, framework_root) -> tuple[Path, Path]:
    """
    Create an isolated test workspace with its own aah-config.yaml.
    Returns (workspace_root, config_path).
    """
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    config = {
        "framework_root": str(framework_root),
        "workspace_root": str(workspace_root),
        "active_workspace": None,
        "active_project": None,
        "settings": {
            "default_complexity_tier": "moderate",
            "git_branching": {
                "main_branch": "main",
                "develop_branch": "develop",
                "integration_prefix": "integration/wave-",
            },
        },
    }

    config_path = tmp_path / "aah-config.yaml"
    import yaml
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    return workspace_root, config_path


# ─── Scaffolded project fixture ─────────────────────────────────────

@pytest.fixture
def scaffolded_project(test_workspace) -> Path:
    """
    Create a fully scaffolded greenfield project via aah.core.
    Returns the project path.
    """
    workspace_root, config_path = test_workspace

    # Create workspace
    result = subprocess.run(
        ["aah-run", "aah.core.scaffold.workspace", "create", "test-ws",
         "--config-path", str(config_path), "--workspace-root", str(workspace_root)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    assert result.returncode == 0, f"Workspace creation failed: {result.stderr}"

    # Create project
    result = subprocess.run(
        ["aah-run", "aah.core.scaffold.project", "create", "test-app",
         "--config-path", str(config_path), "--stack", "python-fastapi"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    assert result.returncode == 0, f"Project creation failed: {result.stderr}"

    project_path = workspace_root / "test-ws" / "test-app"
    assert project_path.exists()
    assert (project_path / ".aah" / "manifest.yaml").exists()

    return project_path


# ─── Active test project fixture ────────────────────────────────────

@pytest.fixture
def active_test_project(project_with_features, framework_root, protect_rapids_config):
    """
    Configure the framework's aah-config.yaml to point at project_with_features.
    Use this for Agent SDK tests that invoke skills requiring an active project.
    protect_rapids_config is included so config is restored automatically after.
    """
    import yaml
    config_path = framework_root / "aah-config.yaml"
    config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    project = project_with_features
    config["workspace_root"] = str(project.parent.parent)
    config["active_workspace"] = project.parent.name
    config["active_project"] = project.name
    config_path.write_text(yaml.dump(config))
    yield project_with_features


# ─── Project with features fixture ──────────────────────────────────

@pytest.fixture
def project_with_features(scaffolded_project) -> Path:
    """
    A project that has completed intake + planning with features, DAG, and waves.
    """
    import yaml as pyyaml
    project_path = scaffolded_project
    aah_root = project_path / ".aah"

    # Set manifest to plan phase
    manifest_path = aah_root / "manifest.yaml"
    manifest = pyyaml.safe_load(open(manifest_path))
    manifest["current_phase"] = "build"
    manifest["complexity_tier"] = "moderate"
    with open(manifest_path, "w") as f:
        pyyaml.dump(manifest, f)

    # Create features
    features_dir = aah_root / "plan" / "features"
    for fid, desc, deps in [
        ("F001", "User auth endpoint", []),
        ("F002", "User registration", []),
        ("F003", "Dashboard", ["F001"]),
        ("F004", "User profile", ["F001", "F002"]),
    ]:
        feature = {
            "id": fid, "spec_ref": "SPEC-001", "description": desc,
            "dependencies": deps,
            "acceptance_criteria": [f"{desc} works correctly"],
            "test_cases": [{"id": f"TC-{fid}", "description": f"Test {desc}"}],
            "status": "pending",
        }
        with open(features_dir / f"{fid}.yaml", "w") as f:
            pyyaml.dump(feature, f)

    # Build feature list, DAG, waves
    subprocess.run(
        ["aah-run", "aah.core.plan.build_feature_list",
         "--features-dir", str(features_dir),
         "--output", str(aah_root / "feature-list.json")],
        capture_output=True, check=True, timeout=30,
    )
    subprocess.run(
        ["aah-run", "aah.core.plan.build_dag",
         "--features-dir", str(features_dir),
         "--output", str(aah_root / "plan" / "dag.json")],
        capture_output=True, check=True, timeout=30,
    )
    subprocess.run(
        ["aah-run", "aah.core.plan.compute_waves",
         "--dag-path", str(aah_root / "plan" / "dag.json"),
         "--output", str(aah_root / "plan" / "waves.json")],
        capture_output=True, check=True, timeout=30,
    )

    # Create sprint contracts
    contracts_dir = aah_root / "plan" / "sprint-contracts"
    waves = json.loads((aah_root / "plan" / "waves.json").read_text())
    for i, wave in enumerate(waves["waves"]):
        contract = f"## Wave {i}\n\n## Features\n"
        for fid in wave:
            contract += f"- {fid}\n"
        contract += "\n## Pass/Fail Criteria\n- [ ] All tests pass\n"
        (contracts_dir / f"wave-{i}-contract.md").write_text(contract)

    # Create init.sh
    (aah_root / "init.sh").write_text("#!/bin/bash\necho 'OK'\nexit 0\n")
    (aah_root / "init.sh").chmod(0o755)

    # Set progress
    from aah.core.common.progress import save_progress, get_default_progress
    progress = get_default_progress()
    progress["current_phase"] = "build"
    progress["current_wave"] = 0
    progress["environment_state"] = "healthy"
    save_progress(progress, aah_root / "claude-progress.json")

    return project_path


# ─── Fixture: scaffolded project with commercial-onboarding intake ────

@pytest.fixture
def project_with_commercial_onboarding_intake(scaffolded_project) -> Path:
    """Scaffolded project pre-seeded with intake + manifest for a commercial
    client onboarding problem. Used by industry-domain E2E tests.

    - intake.json has a commercial-banking KYB problem statement
    - manifest.yaml has industry_domain_path set to the exemplar leaf
    - manifest.current_phase is 'research' (so Domain Intelligence injects)
    """
    import yaml as pyyaml
    from datetime import datetime, timezone

    project_path = scaffolded_project
    aah_root = project_path / ".aah"

    # Seed intake
    intake = {
        "version": "1.0",
        "status": "in_progress",
        "problem_statement": (
            "Build an agent that onboards new commercial banking clients with "
            "automated KYB, beneficial-owner discovery, and risk rating."
        ),
        "project_type": "new_system",
        "rounds": [
            {
                "phase": "start",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "questions": [
                    {
                        "header": "Technology",
                        "question": "What technology stack?",
                        "answer": "Python FastAPI with multi-agent orchestration",
                    },
                    {
                        "header": "Integrations",
                        "question": "Which external systems?",
                        "answer": "Dow Jones sanctions screening, D&B entity verification, core banking",
                    },
                ],
            }
        ],
        "industry_domain": {
            "path": "financial-services/commercial-banking/commercial-client-onboarding",
            "confidence": "high",
            "source": "classifier",
            "rationale": "Problem statement explicitly mentions KYB and commercial client onboarding.",
            "classified_at": datetime.now(timezone.utc).isoformat(),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    (aah_root / "intake.json").write_text(json.dumps(intake, indent=2) + "\n")

    # Update manifest
    manifest_path = aah_root / "manifest.yaml"
    manifest = pyyaml.safe_load(manifest_path.read_text())
    manifest["current_phase"] = "discuss"
    manifest["industry_domain_path"] = (
        "financial-services/commercial-banking/commercial-client-onboarding"
    )
    manifest_path.write_text(pyyaml.dump(manifest, sort_keys=False))

    return project_path
