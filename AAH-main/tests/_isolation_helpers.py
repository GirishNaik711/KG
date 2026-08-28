"""Shared builders for isolation / failure-bundle functional tests.

NO MOCKS. Every helper builds a REAL temporary git project with a real
attestation secret, a real manifest, a real feature .md whose
test_config.command runs a REAL pytest file,
and real governance via the official CLI. Nothing here mocks a writer, reader,
parser, subprocess, Docker client, or clock.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

from aah.core.common.git_utils import code_subject_identity
from tests.support.aah_project import AAHProjectBuilder


def free_port() -> int:
    """Allocate an OS-assigned free TCP port and release it for a child."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _git(repo: Path, *args: str) -> str:
    return AAHProjectBuilder(repo).git(*args)


def write_secret(project: Path) -> None:
    AAHProjectBuilder(project).secret()


def write_secret_dir(project: Path) -> None:
    """Alias used where the caller has already created .aah/build."""
    write_secret(project)


def configure_governance_cli(
    project: Path,
    *,
    delivery_owner: str = "alice",
    qa_governance_owner: str = "qa-lead",
    security_owner: str | None = "sec-owner",
) -> subprocess.CompletedProcess:
    args = [
        "configure",
        "--project-path", str(project),
        "--delivery-owner", delivery_owner,
        "--qa-governance-owner", qa_governance_owner,
    ]
    if security_owner is not None:
        args += ["--security-owner", security_owner]
    return AAHProjectBuilder(project).run_module(
        "aah.core.common.governance", *args, check=True
    )


_MANIFEST = (
    "project_name: {name}\n"
    "current_phase: build\n"
    "features: {{}}\n"
)


def write_manifest(
    project: Path,
    *,
    name: str = "iso-test",
) -> None:
    (project / ".aah" / "manifest.yaml").write_text(
        _MANIFEST.format(name=name),
        encoding="utf-8",
    )


def _feature_contract(feature_id: str, command: str) -> str:
    safe = command.replace('"', '\\"')
    return (
        "---\n"
        f"id: {feature_id}\n"
        "spec_ref: SPEC-001\n"
        f"description: isolation test for {feature_id}\n"
        "dependencies: []\n"
        "status: pending\n"
        "test_config:\n"
        f'  command: "{safe}"\n'
        "acceptance_criteria:\n"
        "  - id: AC1\n"
        "    description: test runs\n"
        "test_cases:\n"
        "  - id: TC1\n"
        "    covers: [AC1]\n"
        "    description: marker\n"
        "knowledge_used:\n"
        "  knowledge_folder: null\n"
        "---\n"
        f"# {feature_id}\n"
    )


def commit_worktree(state: dict, message: str = "declare infra") -> None:
    """Stage + commit everything in the worktree and refresh state['sha'].

    Use after writing files (infra-services.yaml, compose files) into the
    subject checkout so the evidence-v2 binding check still sees a CLEAN
    subject at the recorded SHA.
    """
    worktree = state["worktree"]
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)
    state["sha"] = code_subject_identity(cwd=worktree) or _git(worktree, "rev-parse", "HEAD")


def make_isolated_project(
    tmp_path: Path,
    *,
    feature_id: str = "F001",
    test_body: str,
    name: str = "iso-test",
) -> dict:
    """Build a real git project + worktree with a real pytest file.

    ``test_body`` is the full source of the test file placed at
    ``proj_tests/test_<feature_id>.py``; it runs inside the worktree checkout.
    Returns ``{project, worktree, feature_id, test_rel, cli_env}``.
    """
    builder = AAHProjectBuilder.create(tmp_path)
    project = builder.path
    builder.file(".gitignore", ".aah/build/\n.claude/\n__pycache__/\n")
    features = builder.aah / "plan" / "features"

    write_manifest(project, name=name)

    tests_dir = project / "proj_tests"
    tests_dir.mkdir()
    test_rel = f"proj_tests/test_{feature_id}.py"
    (project / test_rel).write_text(test_body, encoding="utf-8")

    command = f"{sys.executable} -m pytest {project / test_rel} -s -p no:cacheprovider"
    (features / f"{feature_id}.md").write_text(
        _feature_contract(feature_id, command), encoding="utf-8"
    )

    _git(project, "add", ".gitignore", ".aah", "proj_tests")
    _git(project, "commit", "-m", "seed isolation project")

    worktree = project / ".claude" / "worktrees" / feature_id
    _git(project, "worktree", "add", str(worktree), "-b", f"feature/{feature_id}", "develop")

    write_secret(project)
    configure_governance_cli(project)

    branch = f"feature/{feature_id}"
    sha = code_subject_identity(cwd=worktree) or _git(worktree, "rev-parse", "HEAD")

    return {
        "project": project,
        "worktree": worktree,
        "feature_id": feature_id,
        "test_rel": test_rel,
        "branch": branch,
        "sha": sha,
        "cli_env": builder.cli_env(),
    }
