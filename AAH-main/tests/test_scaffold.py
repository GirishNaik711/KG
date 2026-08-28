"""Tests for aah.core.scaffold modules (project, rapids_dir) — folder=project model."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from aah.core.common.io_utils import read_json, read_yaml
from aah.core.scaffold.rapids_dir import RAPIDS_DIRS, create_rapids_dir, verify_rapids_dir
from aah.core.scaffold.project import (
    LINT_CONFIG_TEMPLATES,
    create_project,
    build_gitignore,
    detect_project_type,
    ensure_lint_config,
    is_harness_repo,
    load_scaffold_template,
)


# ─── rapids_dir ──────────────────────────────��──────────────

class TestCreateRapidsDir:
    def test_creates_all_directories(self, tmp_path):
        rapids_path = create_rapids_dir(tmp_path)
        assert rapids_path == tmp_path / ".aah"
        for d in RAPIDS_DIRS:
            assert (rapids_path / d).is_dir(), f"Missing directory: {d}"

    def test_idempotent(self, tmp_path):
        create_rapids_dir(tmp_path)
        create_rapids_dir(tmp_path)  # Should not raise
        for d in RAPIDS_DIRS:
            assert (tmp_path / ".aah" / d).is_dir()


class TestVerifyRapidsDir:
    def test_complete_structure(self, tmp_path):
        create_rapids_dir(tmp_path)
        missing = verify_rapids_dir(tmp_path)
        assert missing == []

    def test_missing_dirs(self, tmp_path):
        (tmp_path / ".aah").mkdir()
        missing = verify_rapids_dir(tmp_path)
        assert len(missing) == len(RAPIDS_DIRS)

    def test_partial_structure(self, tmp_path):
        rapids = tmp_path / ".aah"
        rapids.mkdir()
        (rapids / "discuss").mkdir()
        (rapids / "architecture").mkdir()
        missing = verify_rapids_dir(tmp_path)
        assert "discuss" not in missing
        assert "architecture" not in missing
        assert len(missing) == len(RAPIDS_DIRS) - 2


# ─── detect_project_type / is_harness_repo ──────────────────

class TestDetectProjectType:
    def test_empty_folder_is_greenfield(self, tmp_path):
        assert detect_project_type(tmp_path) == "greenfield"

    def test_folder_with_git_is_brownfield(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert detect_project_type(tmp_path) == "brownfield"

    def test_folder_with_code_is_brownfield(self, tmp_path):
        (tmp_path / "app.py").write_text("print('hi')")
        assert detect_project_type(tmp_path) == "brownfield"

    def test_folder_with_only_aah_is_greenfield(self, tmp_path):
        # A stray .aah/ dir (no manifest) should not force brownfield.
        (tmp_path / ".aah").mkdir()
        assert detect_project_type(tmp_path) == "greenfield"


class TestIsHarnessRepo:
    def test_true_for_aah_repo(self, tmp_path):
        (tmp_path / "aah" / "core").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "aah"\n')
        assert is_harness_repo(tmp_path) is True

    def test_false_for_normal_project(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "myapp"\n')
        assert is_harness_repo(tmp_path) is False

    def test_false_for_empty_folder(self, tmp_path):
        assert is_harness_repo(tmp_path) is False


# ─── project (folder=project) ───────────────────────────────

GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


@pytest.fixture
def git_env():
    """Set git author/committer env for scaffold commits, then clean up."""
    for k, v in GIT_ENV_KEYS.items():
        os.environ[k] = v
    try:
        yield
    finally:
        for k in GIT_ENV_KEYS:
            os.environ.pop(k, None)


class TestCreateProject:
    def test_scaffold_stamps_verification_rollout_states(self, tmp_path, git_env):
        project_path = create_project(target_dir=tmp_path, project_type="greenfield")
        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        assert manifest["features"] == {
            "runtime_profile_v1": "report_only",
            "runtime_cloud_probes_v1": False,
            "semantic_smoke_v2": False,
        }

    def test_scaffold_ignores_build_attestation_secret(self, tmp_path, git_env):
        project_path = create_project(target_dir=tmp_path, project_type="greenfield")
        secret = project_path / ".aah" / "build" / ".attestation-secret"
        assert secret.exists()
        assert len(secret.read_bytes()) == 32
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(secret.relative_to(project_path))],
            cwd=project_path,
        )
        assert ignored.returncode == 0
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=project_path, capture_output=True, text=True,
        )
        assert ".attestation-secret" not in status.stdout

    def test_greenfield_creates_structure(self, tmp_path, git_env):
        project_path = create_project(target_dir=tmp_path, project_type="greenfield")

        assert project_path == tmp_path.resolve()
        assert (project_path / ".aah").is_dir()
        assert verify_rapids_dir(project_path) == []

        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        assert manifest["project_name"] == tmp_path.name  # defaults to folder name
        assert manifest["project_type"] == "greenfield"
        assert manifest["current_phase"] == "init"

        progress = read_json(project_path / ".aah" / "claude-progress.json")
        assert progress["current_phase"] == "init"

        assert (project_path / ".git").is_dir()
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=project_path, capture_output=True, text=True,
        )
        branches = result.stdout.strip().split("\n")
        assert "main" in branches
        assert "develop" in branches

        gitignore = (project_path / ".gitignore").read_text()
        assert ".DS_Store" in gitignore
        assert "__pycache__/" in gitignore

    def test_explicit_name_overrides_folder_name(self, tmp_path, git_env):
        project_path = create_project("custom-name", target_dir=tmp_path, project_type="greenfield")
        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        assert manifest["project_name"] == "custom-name"

    def test_greenfield_auto_detected_for_empty_folder(self, tmp_path, git_env):
        # No project_type passed → auto-detect → greenfield for an empty folder.
        project_path = create_project(target_dir=tmp_path)
        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        assert manifest["project_type"] == "greenfield"

    def test_greenfield_node_stack_gitignore(self, tmp_path, git_env):
        project_path = create_project(
            target_dir=tmp_path, project_type="greenfield", stack="react-typescript-tailwind",
        )
        gitignore = (project_path / ".gitignore").read_text()
        assert "node_modules/" in gitignore
        assert ".next/" in gitignore
        assert ".DS_Store" in gitignore

    def test_greenfield_with_stack(self, tmp_path, git_env):
        project_path = create_project(
            target_dir=tmp_path, project_type="greenfield", stack="python-fastapi",
        )
        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        assert manifest["stack_choices"]["primary"] == "python-fastapi"

    def test_already_scaffolded_exits(self, tmp_path, git_env):
        create_project(target_dir=tmp_path, project_type="greenfield")
        with pytest.raises(SystemExit):
            create_project(target_dir=tmp_path)

    def test_harness_repo_refuses(self, tmp_path, git_env):
        (tmp_path / "aah" / "core").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "aah"\n')
        with pytest.raises(SystemExit):
            create_project(target_dir=tmp_path)

    def test_brownfield_auto_detected_from_existing_repo(self, tmp_path, git_env):
        # Simulate a cloned repo with history on a non-main branch.
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / "app.py").write_text("print('hi')")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "existing history"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "my-feature"], cwd=tmp_path, capture_output=True)
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True,
        ).stdout.strip()

        project_path = create_project(target_dir=tmp_path)  # auto-detect → brownfield

        manifest = read_yaml(project_path / ".aah" / "manifest.yaml")
        assert manifest["project_type"] == "brownfield"
        # Existing history and branch must be preserved (not stomped).
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True,
        ).stdout.strip()
        assert branch == "my-feature"
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True,
        ).stdout
        assert "existing history" in log
        assert (tmp_path / "app.py").exists()
        secret = project_path / ".aah" / "build" / ".attestation-secret"
        assert secret.exists()
        assert len(secret.read_bytes()) == 32

    def test_brownfield_detects_node_stack(self, tmp_path, git_env):
        (tmp_path / "package.json").write_text('{"name": "test"}')
        project_path = create_project(target_dir=tmp_path, project_type="brownfield")
        gitignore = (project_path / ".gitignore").read_text()
        assert "node_modules/" in gitignore

    def test_brownfield_existing_git_merges_gitignore(self, tmp_path, git_env):
        (tmp_path / "package.json").write_text('{"name": "test"}')
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        (tmp_path / ".gitignore").write_text("*.log\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        project_path = create_project(target_dir=tmp_path, project_type="brownfield")
        gitignore = (project_path / ".gitignore").read_text()
        assert "*.log" in gitignore  # original preserved
        assert "node_modules/" in gitignore  # new entry added
        assert "RAPIDS scaffold" in gitignore  # attribution comment

    def test_greenfield_preinit_git_not_stomped(self, tmp_path, git_env):
        # A folder that is `git init`'d but otherwise empty is detected as
        # brownfield (has .git); its branch/history must be preserved. But even
        # forcing greenfield must be idempotent about git init.
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        branch_before = subprocess.run(
            ["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True,
        ).stdout.strip()

        project_path = create_project(target_dir=tmp_path, project_type="greenfield")

        # Should NOT have created a separate 'main'+'develop' pair over the
        # user's existing branch; the pre-existing branch stays checked out.
        branch_after = subprocess.run(
            ["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True,
        ).stdout.strip()
        assert branch_after == branch_before
        assert (project_path / ".aah" / "manifest.yaml").exists()


# ─── build_gitignore ──────────────────────────────────────────

class TestBuildGitignore:
    def test_base_always_included(self):
        content = build_gitignore()
        assert ".DS_Store" in content

    def test_python_default_when_no_stack(self):
        content = build_gitignore()
        assert "__pycache__/" in content

    def test_node_stack(self):
        content = build_gitignore(stack="react-typescript-tailwind")
        assert "node_modules/" in content
        assert ".next/" in content

    def test_python_stack(self):
        content = build_gitignore(stack="python-fastapi")
        assert "__pycache__/" in content
        assert ".venv/" in content
        assert ".pytest_cache/" in content

    def test_go_stack(self):
        content = build_gitignore(stack="golang")
        assert "vendor/" in content

    def test_rust_stack(self):
        content = build_gitignore(stack="rust")
        assert "target/" in content

    def test_java_stack(self):
        content = build_gitignore(stack="java-spring")
        assert "*.class" in content
        assert ".gradle/" in content

    def test_detects_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        content = build_gitignore(project_path=tmp_path)
        assert "node_modules/" in content

    def test_detects_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")
        content = build_gitignore(project_path=tmp_path)
        assert "__pycache__/" in content

    def test_detects_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")
        content = build_gitignore(project_path=tmp_path)
        assert "vendor/" in content

    def test_detects_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        content = build_gitignore(project_path=tmp_path)
        assert "target/" in content

    def test_combined_stack_and_detection(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        content = build_gitignore(stack="python-fastapi", project_path=tmp_path)
        assert "node_modules/" in content
        assert "__pycache__/" in content


class TestScaffoldTemplates:
    """The lint-config bodies are shipped files, so a missing one is now possible.

    As Python strings they could not go absent; as ``_resources`` files they can
    be lost by a packaging change. These fail loudly if that ever happens, rather
    than letting an empty config reach a package root — the standards gate would
    read that as clean.
    """

    def test_every_declared_family_has_a_template_on_disk(self):
        for family, filename in LINT_CONFIG_TEMPLATES.items():
            body = load_scaffold_template(filename)
            assert body.strip(), f"{family} template {filename} is empty"

    def test_missing_template_raises_rather_than_writing_empty(self):
        with pytest.raises(FileNotFoundError):
            load_scaffold_template("does-not-exist.toml")

    def test_templates_carry_their_load_bearing_exceptions(self):
        # The reasons live in the template files now; if these markers vanish the
        # exceptions were dropped in a move.
        assert "B008" in load_scaffold_template("ruff.toml")
        eslint = load_scaffold_template("eslint.config.mjs")
        assert "ignores:" in eslint
        # Zero imports is the whole point of the eslint default: an unresolvable
        # import makes eslint fail to LOAD, which the gate reads as
        # tool_not_installed. Check statements, not the prose that explains them.
        code = [
            line.strip() for line in eslint.splitlines()
            if line.strip() and not line.strip().startswith("//")
        ]
        assert not any(
            line.startswith(("import ", "import{")) or "require(" in line
            for line in code
        ), "the eslint default must import nothing"


class TestEnsureLintConfig:
    """Configs land at PACKAGE roots, one per discovered package."""

    def test_python_package_root_stamps_ruff_toml(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        written = ensure_lint_config(tmp_path)
        assert written == ["ruff.toml"]
        body = (tmp_path / "ruff.toml").read_text()
        assert "[lint]" in body
        assert 'target-version = "py312"' in body
        assert "B008" in body  # FastAPI Depends() idiom exception

    def test_idempotent_never_clobbers(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        ensure_lint_config(tmp_path)
        (tmp_path / "ruff.toml").write_text("# user edits\nline-length = 79\n")
        written = ensure_lint_config(tmp_path)
        assert written == []
        assert (tmp_path / "ruff.toml").read_text() == "# user edits\nline-length = 79\n"

    def test_respects_existing_pyproject_ruff_table(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
        written = ensure_lint_config(tmp_path)
        assert written == []
        assert not (tmp_path / "ruff.toml").exists()

    def test_node_package_root_stamps_eslint_config_and_dependency(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "web"}')
        written = ensure_lint_config(tmp_path)
        assert written == ["package.json", "eslint.config.mjs"]
        body = (tmp_path / "eslint.config.mjs").read_text()
        assert "export default" in body
        # Zero imports (D12): an unresolvable import makes eslint fail to LOAD,
        # which the gate reads as "could not check".
        assert not any(
            line.lstrip().startswith("import ") for line in body.splitlines()
        )
        pkg = json.loads((tmp_path / "package.json").read_text())
        assert "eslint" in pkg["devDependencies"]

    def test_eslint_config_never_clobbered(self, tmp_path):
        (tmp_path / "package.json").write_text('{"devDependencies": {"eslint": "^8.0.0"}}')
        (tmp_path / "eslint.config.js").write_text("export default [];\n")
        assert ensure_lint_config(tmp_path) == []
        assert not (tmp_path / "eslint.config.mjs").exists()
        pkg = json.loads((tmp_path / "package.json").read_text())
        assert pkg["devDependencies"]["eslint"] == "^8.0.0"

    def test_eslintrc_and_package_json_config_also_honored(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "package.json").write_text('{"eslintConfig": {}}')
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "package.json").write_text("{}")
        (tmp_path / "b" / ".eslintrc.json").write_text("{}")

        written = ensure_lint_config(tmp_path)
        assert not (tmp_path / "a" / "eslint.config.mjs").exists()
        assert not (tmp_path / "b" / "eslint.config.mjs").exists()
        # Only the eslint devDependency declarations were added.
        assert written == ["a/package.json", "b/package.json"]

    def test_fullstack_gets_one_config_per_package(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='b'\n")
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text("{}")

        written = ensure_lint_config(tmp_path)
        assert written == [
            "backend/ruff.toml", "frontend/package.json", "frontend/eslint.config.mjs",
        ]
        assert not (tmp_path / "ruff.toml").exists()

    def test_package_root_argument_stamps_only_that_root(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname='b'\n")
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text("{}")

        assert ensure_lint_config(tmp_path, package_root=Path("backend")) == [
            "backend/ruff.toml"
        ]
        assert not (tmp_path / "frontend" / "eslint.config.mjs").exists()

    def test_no_package_root_is_noop(self, tmp_path):
        # An empty tree has nothing to discover — module 0 stamps the configs
        # for the packages it scaffolds.
        assert ensure_lint_config(tmp_path) == []


class TestGreenfieldLintConfig:
    def test_greenfield_stamps_nothing(self, tmp_path, git_env):
        # No package manifests exist at scaffold time, so there is no package
        # root to place a config at; module 0 owns it.
        project_path = create_project(
            target_dir=tmp_path, project_type="greenfield", stack="python-fastapi",
        )
        assert not (project_path / "ruff.toml").exists()

    def test_brownfield_stamps_every_package_root(self, tmp_path, git_env):
        repo = tmp_path / "repo"
        (repo / "backend").mkdir(parents=True)
        (repo / "backend" / "pyproject.toml").write_text("[project]\nname='b'\n")
        (repo / "frontend").mkdir()
        (repo / "frontend" / "package.json").write_text("{}")

        project_path = create_project(target_dir=repo, project_type="brownfield")
        assert (project_path / "backend" / "ruff.toml").is_file()
        assert (project_path / "frontend" / "eslint.config.mjs").is_file()
