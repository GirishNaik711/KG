"""Language adapter pack contract and per-language adapters.

Covers:
  - ABC contract (Cmd dataclass, abstract methods, default-None methods)
  - Registry detect() — order, fallback, manifest override, file fingerprints
  - Per-language adapters (Python, Node, Go, Rust, Java, Generic) — each
    adapter's detect() and resolved Cmd argv shapes.

Cutover behavior-preservation tests live in test_lang_checks_integration.py.
"""

from __future__ import annotations

import json
from dataclasses import is_dataclass
from pathlib import Path

import pytest

from aah.core.build.lang_checks import (
    Cmd,
    LanguageAdapter,
    detect,
    resolve_test_command,
)
from aah.core.build.lang_checks._helpers import rewrite_bare_venv_command
from aah.core.build.lang_checks.generic import GenericAdapter
from aah.core.build.lang_checks.go import GoAdapter
from aah.core.build.lang_checks.java import JavaAdapter
from aah.core.build.lang_checks.node import NodeAdapter
from aah.core.build.lang_checks.python import PythonAdapter
from aah.core.build.lang_checks.rust import RustAdapter

# ---------------------------------------------------------------------------
# ABC + Cmd dataclass
# ---------------------------------------------------------------------------


class TestCmdDataclass:
    def test_cmd_is_dataclass(self):
        assert is_dataclass(Cmd)

    def test_cmd_required_fields(self):
        c = Cmd(argv=["x"])
        assert c.argv == ["x"]
        assert c.cwd is None
        assert c.timeout_sec == 300
        assert c.env == {}
        assert c.label == ""

    def test_cmd_is_frozen(self):
        c = Cmd(argv=["x"])
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            c.argv = ["y"]

    def test_cmd_full_construction(self):
        c = Cmd(
            argv=["a", "b"],
            cwd=Path("/x"),
            timeout_sec=42,
            env={"K": "V"},
            label="test",
        )
        assert c.timeout_sec == 42
        assert c.env == {"K": "V"}


class TestLanguageAdapterABC:
    def test_un_instantiable(self):
        # ABC with abstract detect() and test_command() can't instantiate
        with pytest.raises(TypeError):
            LanguageAdapter(project_path=Path("/x"), manifest={})

    def test_default_methods_return_none(self, tmp_path):
        # GenericAdapter implements only the required abstracts; everything
        # else inherits the None defaults from the ABC.
        a = GenericAdapter(project_path=tmp_path, manifest={})
        assert a.deps_consistency_check() is None
        assert a.module_validation_command() is None
        assert a.compile_or_typecheck() is None
        assert a.build() is None
        assert a.lint_command() is None
        assert a.static_analysis_command() is None
        assert a.coverage_command() is None
        assert a.type_check_command() is None
        assert a.start_command(8000) is None
        assert a.cache_clean() == []
        assert a.health_probe_path() == "/"
        assert a.has_web_surface() is False


# ---------------------------------------------------------------------------
# Registry — detect() priority and fallback
# ---------------------------------------------------------------------------


class TestRegistryDetect:
    def test_empty_project_is_generic(self, tmp_path):
        a = detect(tmp_path)
        assert a.name == "generic"

    def test_pyproject_toml_is_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert detect(tmp_path).name == "python"

    def test_package_json_is_node(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert detect(tmp_path).name == "node"

    def test_go_mod_is_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n")
        assert detect(tmp_path).name == "go"

    def test_cargo_toml_is_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        assert detect(tmp_path).name == "rust"

    def test_pom_xml_is_java(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")
        assert detect(tmp_path).name == "java"

    def test_build_gradle_is_java(self, tmp_path):
        (tmp_path / "build.gradle").write_text("apply plugin: 'java'")
        assert detect(tmp_path).name == "java"

    def test_manifest_stack_overrides_file_fingerprint(self, tmp_path):
        # package.json present but manifest says python — Python adapter
        # is registered first AND its detect() claims based on stack
        # token, so it wins.
        (tmp_path / "package.json").write_text("{}")
        a = detect(tmp_path, manifest={"stack_choices": {"primary": "python-fastapi"}})
        assert a.name == "python"

    def test_django_does_not_match_go(self, tmp_path):
        # Defensive: "django" contains "go" as a substring. The Go
        # adapter's detect() uses word-boundary checks, so "python-django"
        # must NOT trigger Go.
        a = detect(tmp_path, manifest={"stack_choices": {"primary": "python-django"}})
        assert a.name == "python", f"django falsely matched {a.name}"

    def test_loads_manifest_when_not_passed(self, tmp_path):
        aah_root = tmp_path / ".aah"
        aah_root.mkdir()
        (aah_root / "manifest.yaml").write_text(
            "stack_choices:\n  primary: python-fastapi\n"
        )
        a = detect(tmp_path)  # manifest is None — should auto-load
        assert a.name == "python"


# ---------------------------------------------------------------------------
# PythonAdapter
# ---------------------------------------------------------------------------


class TestPythonAdapter:
    def test_detect_via_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert PythonAdapter.detect(tmp_path, {})

    def test_detect_via_stack_token(self, tmp_path):
        for tok in ("python", "fastapi", "django", "flask"):
            assert PythonAdapter.detect(
                tmp_path, {"stack_choices": {"primary": f"python-{tok}"}}
            ), f"failed on token {tok}"

    def test_test_command_no_filter(self, tmp_path):
        # No tests/ on disk: the adapter passes NO path (discovery semantics)
        # and lets pytest discover from rootdir. `python -m pytest` rather than
        # bare pytest so cwd is on sys.path.
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.test_command()
        assert cmd.argv == [
            "uv", "run", "--python", "3.12", "--with", "pytest",
            "python", "-m", "pytest", "-v", "--tb=short",
        ]

    def test_test_command_includes_discovered_tests_dir(self, tmp_path):
        (tmp_path / "tests").mkdir()
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.test_command()
        assert cmd.argv == [
            "uv", "run", "--python", "3.12", "--with", "pytest",
            "python", "-m", "pytest", "tests/", "-v", "--tb=short",
        ]

    def test_test_command_with_filter(self, tmp_path):
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.test_command("F001")
        assert cmd.argv[-2:] == ["-k", "F001"]

    def test_test_command_filter_normalizes_hyphens(self, tmp_path):
        # Hyphenated feature ids must be converted to underscores so pytest -k
        # matches the underscore test-node names the harness generates.
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.test_command("F-MOD000-00")
        assert cmd.argv[-2:] == ["-k", "F_MOD000_00"]

    def test_module_validation_no_files_collects(self, tmp_path):
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.module_validation_command()
        assert "--collect-only" in cmd.argv

    def test_module_validation_with_files_imports(self, tmp_path):
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.module_validation_command(["src/db/connection.py", "src/main.py"])
        assert cmd.argv[:2] == ["uv", "run"]
        assert cmd.argv[-2] == "-c"
        assert "import src.db.connection" in cmd.argv[-1]
        assert "import src.main" in cmd.argv[-1]

    def test_lint_uses_ruff(self, tmp_path):
        a = PythonAdapter(project_path=tmp_path, manifest={})
        # --no-project is load-bearing, not an optimization: ruff needs only
        # itself, and letting uv discover the project's pyproject.toml makes it
        # write a uv.lock into the subject, which invalidates the standards
        # evidence the run just produced.
        assert a.lint_command().argv[:8] == [
            "uv", "run", "--python", "3.12", "--no-project",
            "--with", "ruff", "ruff",
        ]

    def test_lint_output_is_concise_not_json(self, tmp_path):
        """The consumer is aah-fix, which reads what a developer would read."""
        argv = PythonAdapter(project_path=tmp_path, manifest={}).lint_command().argv
        assert "--output-format=concise" in argv
        assert "--output-format=json" not in argv

    def test_static_analysis_skips_project_resolution(self, tmp_path):
        argv = PythonAdapter(
            project_path=tmp_path, manifest={}
        ).static_analysis_command().argv
        assert "--no-project" in argv
        assert "--project" not in argv

    def test_start_command_manifest_override(self, tmp_path):
        manifest = {
            "stack_choices": {
                "start_command": "python manage.py runserver 8000",
                "test_command": "python -m pytest backend/tests -q",
            }
        }
        a = PythonAdapter(project_path=tmp_path, manifest=manifest)
        cmd = a.start_command(8000)
        assert cmd.argv[0] == "python"
        test_cmd = resolve_test_command(tmp_path, manifest=manifest)
        assert test_cmd.argv == ["python", "-m", "pytest", "backend/tests", "-q"]

    def test_start_command_default_uvicorn(self, tmp_path):
        # Web surface required for the uvicorn default.
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        cmd = a.start_command(8000)
        assert "uvicorn" in cmd.argv

    def test_type_check_none_without_config(self, tmp_path):
        # pyproject.toml present but no [tool.mypy] section → skip (None).
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.type_check_command() is None

    def test_type_check_mypy_with_pyproject_section(self, tmp_path):
        # pyproject.toml and no requirements.txt → deps resolve via --project,
        # so mypy sees the project's own dependencies (R2).
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n\n[tool.mypy]\nstrict = true\n"
        )
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.type_check_command().argv == [
            "uv", "run", "--python", "3.12", "--project", ".",
            "--all-extras", "--all-groups",
            "--with", "mypy", "mypy", ".",
        ]

    def test_type_check_mypy_with_mypy_ini(self, tmp_path):
        (tmp_path / "mypy.ini").write_text("[mypy]\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.type_check_command().argv == [
            "uv", "run", "--python", "3.12", "--with", "mypy", "mypy", ".",
        ]

    def test_type_check_mypy_with_setup_cfg(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[mypy]\nstrict = True\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.type_check_command().argv == [
            "uv", "run", "--python", "3.12", "--with", "mypy", "mypy", ".",
        ]


    def test_no_web_surface_skips_start(self, tmp_path):
        # A pure library: a module, no web dep, no web entrypoint → None.
        (tmp_path / "requirements.txt").write_text("numpy\nrequests\n")
        (tmp_path / "lib.py").write_text("def f(): return 1\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.has_web_surface() is False
        assert a.start_command(8000) is None

    def test_web_surface_via_requirements_starts_uvicorn(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.has_web_surface() is True
        assert "uvicorn" in a.start_command(8000).argv

    def test_web_surface_via_main_entrypoint(self, tmp_path):
        (tmp_path / "main.py").write_text("app = object()\n")
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.has_web_surface() is True

    def test_start_override_wins_even_without_web_surface(self, tmp_path):
        # Manifest override is the escape hatch — bypasses the web-surface gate.
        manifest = {"stack_choices": {"start_command": "python worker.py"}}
        a = PythonAdapter(project_path=tmp_path, manifest=manifest)
        assert a.start_command(8000).argv == ["python", "worker.py"]

    def test_health_probe_path_default_and_override(self, tmp_path):
        a = PythonAdapter(project_path=tmp_path, manifest={})
        assert a.health_probe_path() == "/"
        a2 = PythonAdapter(
            project_path=tmp_path,
            manifest={"stack_choices": {"health_path": "/api"}},
        )
        assert a2.health_probe_path() == "/api"


# ---------------------------------------------------------------------------
# PythonAdapter — optional dependency resolution (extras + dependency-groups)
# ---------------------------------------------------------------------------


class TestPythonOptionalDeps:
    """uv resolves the `dev` GROUP by default but never an EXTRA unless asked.

    A project declaring pytest-asyncio in [project.optional-dependencies] got an
    env without it, so `asyncio_mode = "auto"` became an unknown option and
    --strict-markers turned the unregistered marker into a collection error that
    aborted the run — 54 tests collected, 0 executed, reported as a plain test
    failure. The fix takes EVERY optional section rather than guessing which one
    holds the test deps: a heuristic on names or contents cannot know about
    `httpx`, `testcontainers`, or a project's own test-helper package.
    """

    @staticmethod
    def _adapter(root: Path, pyproject: str) -> PythonAdapter:
        (root / "pyproject.toml").write_text(pyproject)
        return PythonAdapter(project_path=root, manifest={})

    def test_uv_run_takes_all_extras_and_groups_after_project(self, tmp_path):
        a = self._adapter(tmp_path, """
[project]
name = 'x'
version = '0'

[project.optional-dependencies]
dev = ["pytest>=8.3.0", "pytest-asyncio>=0.24.0"]
""")
        (tmp_path / "tests").mkdir()
        argv = a.test_command().argv
        assert argv[:6] == ["uv", "run", "--python", "3.12", "--project", "."]
        assert argv[6:8] == ["--all-extras", "--all-groups"]

    def test_flags_are_unconditional_on_the_project_branch(self, tmp_path):
        # No pyproject inspection: a project with only groups, or with none of
        # either, gets the same flags. uv treats an absent section as a no-op.
        a = self._adapter(tmp_path, """
[project]
name = 'x'
version = '0'

[dependency-groups]
integration = ["httpx>=0.27.0"]
""")
        (tmp_path / "tests").mkdir()
        argv = a.test_command().argv
        assert argv[6:8] == ["--all-extras", "--all-groups"]

    def test_requirements_branch_gets_no_optional_flags(self, tmp_path):
        # --all-extras/--all-groups are invalid without --project, so the
        # --with-requirements branch must never carry them.
        a = self._adapter(tmp_path, """
[project]
name = 'x'
version = '0'

[project.optional-dependencies]
dev = ["pytest>=8.3.0"]
""")
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        (tmp_path / "tests").mkdir()
        argv = a.test_command().argv
        assert "--all-extras" not in argv
        assert "--all-groups" not in argv
        assert "--with-requirements" in argv

    def test_no_project_branch_gets_no_optional_flags(self, tmp_path):
        # ruff/bandit run with --no-project and never import project code.
        a = self._adapter(tmp_path, """
[project]
name = 'x'
version = '0'
""")
        argv = a.lint_command().argv
        assert "--all-extras" not in argv
        assert "--all-groups" not in argv
        assert "--no-project" in argv


# ---------------------------------------------------------------------------
# PythonAdapter — layout matrix (test discovery + dependency resolution)
# ---------------------------------------------------------------------------


class TestPythonLayouts:
    """Arbitrary project layouts, not just the ones the scaffold happens to make.

    The invariant these encode: no project whose tests currently run may stop
    running, and a project whose tests were silently skipped must start running.
    """

    @staticmethod
    def _adapter(root: Path) -> PythonAdapter:
        return PythonAdapter(project_path=root, manifest={})

    @staticmethod
    def _pyproject(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[project]\nname='x'\nversion='0'\n")

    def test_case1_dep_and_tests_at_root(self, tmp_path):
        self._pyproject(tmp_path / "pyproject.toml")
        (tmp_path / "tests").mkdir()
        assert self._adapter(tmp_path)._tests_paths() == ["tests/"]

    def test_case2_dep_and_tests_under_package(self, tmp_path):
        # Must not regress: this is the layout that works today.
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        (tmp_path / "pkg" / "tests").mkdir()
        assert self._adapter(tmp_path)._tests_paths() == ["pkg/tests/"]

    def test_case3_dep_under_package_tests_at_root(self, tmp_path):
        # Today: pkg/tests/ invented -> pytest exit 4. After R1: finds tests/.
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        (tmp_path / "tests").mkdir()
        assert self._adapter(tmp_path)._tests_paths() == ["tests/"]

    def test_case4_tests_in_both_places(self, tmp_path):
        # Today: only pkg/tests/ returned — the root set silently never runs.
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        (tmp_path / "tests").mkdir()
        (tmp_path / "pkg" / "tests").mkdir()
        assert sorted(self._adapter(tmp_path)._tests_paths()) == [
            "pkg/tests/", "tests/",
        ]

    def test_case5_no_tests_anywhere(self, tmp_path):
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        assert self._adapter(tmp_path)._tests_paths() == []

    def test_case6_requirements_txt_keeps_with_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        argv = self._adapter(tmp_path).test_command().argv
        assert "--with-requirements" in argv
        assert argv[argv.index("--with-requirements") + 1] == "requirements.txt"
        assert "--project" not in argv

    def test_case6_requirements_txt_under_package(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "requirements.txt").write_text("fastapi\n")
        argv = self._adapter(tmp_path).test_command().argv
        assert argv[argv.index("--with-requirements") + 1] == "pkg/requirements.txt"

    def test_case7_pyproject_only_uses_project_flag(self, tmp_path):
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        argv = self._adapter(tmp_path).test_command().argv
        assert "--with-requirements" not in argv
        assert argv[argv.index("--project") + 1] == "pkg"

    def test_case7_pyproject_at_root_uses_project_dot(self, tmp_path):
        self._pyproject(tmp_path / "pyproject.toml")
        argv = self._adapter(tmp_path).test_command().argv
        assert argv[argv.index("--project") + 1] == "."

    def test_no_dependency_metadata_adds_no_resolution_flag(self, tmp_path):
        # setup.py-only / Pipfile-only: nothing for uv to resolve from.
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
        argv = self._adapter(tmp_path).test_command().argv
        assert "--project" not in argv
        assert "--with-requirements" not in argv

    def test_case8_project_conftest_does_not_change_command(self, tmp_path):
        # F' is additive: a project that manages sys.path/cwd in its own
        # conftest.py still gets the same adapter command.
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        (tmp_path / "tests").mkdir()
        (tmp_path / "conftest.py").write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "p = Path(__file__).parent / 'pkg'\n"
            "sys.path.insert(0, str(p))\n"
            "os.chdir(str(p))\n"
        )
        argv = self._adapter(tmp_path).test_command().argv
        assert argv == [
            "uv", "run", "--python", "3.12", "--project", "pkg",
            "--all-extras", "--all-groups",
            "--with", "pytest",
            "python", "-m", "pytest", "tests/", "-v", "--tb=short",
        ]

    def test_nested_and_split_test_dirs(self, tmp_path):
        # tests/unit + tests/integration: the parent tests/ is taken once and
        # not descended into, so the suite isn't listed twice.
        self._pyproject(tmp_path / "pyproject.toml")
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        (tmp_path / "tests" / "integration").mkdir(parents=True)
        assert self._adapter(tmp_path)._tests_paths() == ["tests/"]

    def test_two_package_roots_both_test_dirs_found(self, tmp_path):
        # _python_root() picks api/ alphabetically, but discovery is
        # independent of it — both suites run.
        self._pyproject(tmp_path / "api" / "pyproject.toml")
        self._pyproject(tmp_path / "backend" / "pyproject.toml")
        (tmp_path / "api" / "tests").mkdir()
        (tmp_path / "backend" / "tests").mkdir()
        assert sorted(self._adapter(tmp_path)._tests_paths()) == [
            "api/tests/", "backend/tests/",
        ]

    def test_excluded_directories_are_not_searched(self, tmp_path):
        self._pyproject(tmp_path / "pyproject.toml")
        for junk in ("node_modules", ".venv", "venv", ".aah", ".claude", "__pycache__"):
            (tmp_path / junk / "tests").mkdir(parents=True)
        assert self._adapter(tmp_path)._tests_paths() == []

    def test_discovery_beyond_max_depth_ignored(self, tmp_path):
        self._pyproject(tmp_path / "pyproject.toml")
        (tmp_path / "a" / "b" / "c" / "tests").mkdir(parents=True)
        assert self._adapter(tmp_path)._tests_paths() == []

    def test_all_three_builders_use_python_m_pytest(self, tmp_path):
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        (tmp_path / "tests").mkdir()
        a = self._adapter(tmp_path)
        for cmd in (a.test_command(), a.module_validation_command(), a.coverage_command()):
            argv = cmd.argv
            i = argv.index("python", 2)  # skip "uv run --python 3.12"
            assert argv[i:i + 4] == ["python", "-m", "pytest", "tests/"], argv
            # Everything before the runner belongs to the uv prefix — no bare
            # `pytest` as the first non-uv token.
            assert argv[i - 2] == "--with", f"bare pytest in {argv}"

    def test_builders_omit_path_when_no_tests_discovered(self, tmp_path):
        self._pyproject(tmp_path / "pkg" / "pyproject.toml")
        a = self._adapter(tmp_path)
        for cmd in (a.test_command(), a.module_validation_command(), a.coverage_command()):
            assert not any(arg.endswith("tests/") for arg in cmd.argv), cmd.argv


# ---------------------------------------------------------------------------
# NodeAdapter
# ---------------------------------------------------------------------------


class TestNodeAdapter:
    def _seed_pkg(self, tmp_path: Path, scripts: dict | None = None) -> Path:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "t", "scripts": scripts or {}})
        )
        return tmp_path

    def test_detect_via_package_json(self, tmp_path):
        self._seed_pkg(tmp_path)
        assert NodeAdapter.detect(tmp_path, {})

    def test_detect_via_stack_token(self, tmp_path):
        for tok in ("node", "react", "next", "vue", "angular", "express",
                    "typescript", "javascript"):
            assert NodeAdapter.detect(
                tmp_path, {"stack_choices": {"primary": tok}}
            ), f"failed on token {tok}"

    def test_test_command(self, tmp_path):
        self._seed_pkg(tmp_path)
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.test_command().argv == ["npm", "test"]
        assert a.test_command("F001").argv == ["npm", "test", "--", "--grep", "F001"]

    def test_lint_prefers_npm_run_lint(self, tmp_path):
        self._seed_pkg(tmp_path, scripts={"lint": "eslint ."})
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.lint_command().argv == ["npm", "run", "lint"]

    def test_lint_falls_back_to_eslint(self, tmp_path):
        self._seed_pkg(tmp_path)  # no scripts.lint
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.lint_command().argv[0:2] == ["npx", "eslint"]

    def test_module_validation_only_with_tsconfig(self, tmp_path):
        self._seed_pkg(tmp_path)
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.module_validation_command() is None
        (tmp_path / "tsconfig.json").write_text("{}")
        assert a.module_validation_command().argv == ["npx", "tsc", "--noEmit"]

    def test_build_only_when_script_defined(self, tmp_path):
        self._seed_pkg(tmp_path)
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.build() is None
        self._seed_pkg(tmp_path, scripts={"build": "vite build"})
        a2 = NodeAdapter(project_path=tmp_path, manifest={})
        assert a2.build().argv == ["npm", "run", "build"]

    def test_start_uses_dev_or_start(self, tmp_path):
        # Dev server gets --port as an argv flag (Vite ignores env).
        self._seed_pkg(tmp_path, scripts={"dev": "vite"})
        a = NodeAdapter(project_path=tmp_path, manifest={})
        cmd = a.start_command(8080)
        assert cmd.argv == ["npm", "run", "dev", "--", "--port", "8080"]
        assert cmd.env == {}

    def test_type_check_only_with_tsconfig(self, tmp_path):
        self._seed_pkg(tmp_path)
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.type_check_command() is None
        (tmp_path / "tsconfig.json").write_text("{}")
        assert a.type_check_command().argv == ["npx", "tsc", "--noEmit"]

    def test_dev_start_passes_port_flag_not_env(self, tmp_path):
        # Vite ignores env PORT — must pass --port as an argv flag.
        self._seed_pkg(tmp_path, scripts={"dev": "vite"})
        a = NodeAdapter(project_path=tmp_path, manifest={})
        cmd = a.start_command(8080)
        assert cmd.argv == ["npm", "run", "dev", "--", "--port", "8080"]
        assert cmd.env == {}

    def test_has_web_surface_via_dep(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "t", "dependencies": {"react": "^18"}})
        )
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.has_web_surface() is True

    def test_no_web_surface_bare_pkg(self, tmp_path):
        self._seed_pkg(tmp_path)  # no web deps, no scripts
        a = NodeAdapter(project_path=tmp_path, manifest={})
        assert a.has_web_surface() is False


# ---------------------------------------------------------------------------
# GoAdapter
# ---------------------------------------------------------------------------


class TestGoAdapter:
    def test_detect_via_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n")
        assert GoAdapter.detect(tmp_path, {})

    def test_detect_via_stack_token(self, tmp_path):
        assert GoAdapter.detect(tmp_path, {"stack_choices": {"primary": "go"}})
        assert GoAdapter.detect(tmp_path, {"stack_choices": {"primary": "go-net-http"}})
        assert GoAdapter.detect(tmp_path, {"stack_choices": {"primary": "golang"}})

    def test_django_substring_does_not_match(self, tmp_path):
        # Critical: "go" is a substring of "django" but Go adapter's
        # detect() uses word boundaries.
        assert not GoAdapter.detect(
            tmp_path, {"stack_choices": {"primary": "python-django"}}
        )

    def test_module_validation_is_go_build(self, tmp_path):
        a = GoAdapter(project_path=tmp_path, manifest={})
        assert a.module_validation_command().argv == ["go", "build", "./..."]

    def test_test_command(self, tmp_path):
        a = GoAdapter(project_path=tmp_path, manifest={})
        assert a.test_command().argv == ["go", "test", "./..."]
        assert a.test_command("F001").argv == ["go", "test", "./...", "-run", "F001"]


# ---------------------------------------------------------------------------
# RustAdapter
# ---------------------------------------------------------------------------


class TestRustAdapter:
    def test_detect_via_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        assert RustAdapter.detect(tmp_path, {})

    def test_detect_via_stack_token(self, tmp_path):
        assert RustAdapter.detect(tmp_path, {"stack_choices": {"primary": "rust"}})

    def test_module_validation_is_cargo_check(self, tmp_path):
        a = RustAdapter(project_path=tmp_path, manifest={})
        assert a.module_validation_command().argv == ["cargo", "check"]

    def test_test_command_with_filter(self, tmp_path):
        a = RustAdapter(project_path=tmp_path, manifest={})
        assert a.test_command("F001").argv == ["cargo", "test", "F001"]


# ---------------------------------------------------------------------------
# JavaAdapter
# ---------------------------------------------------------------------------


class TestJavaAdapter:
    def test_detect_via_pom_xml(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")
        assert JavaAdapter.detect(tmp_path, {})

    def test_detect_via_build_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("apply plugin: 'java'")
        assert JavaAdapter.detect(tmp_path, {})

    def test_maven_when_pom_xml_present(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")
        a = JavaAdapter(project_path=tmp_path, manifest={})
        assert a.module_validation_command().argv == ["mvn", "compile", "-q"]
        assert a.test_command().argv == ["mvn", "test"]

    def test_gradle_when_only_build_gradle_present(self, tmp_path):
        (tmp_path / "build.gradle").write_text("")
        a = JavaAdapter(project_path=tmp_path, manifest={})
        assert a.module_validation_command().argv[0] == "gradle"
        assert a.test_command().argv[0] == "gradle"


# ---------------------------------------------------------------------------
# GenericAdapter
# ---------------------------------------------------------------------------


class TestGenericAdapter:
    def test_always_detects(self, tmp_path):
        assert GenericAdapter.detect(tmp_path, {})
        assert GenericAdapter.detect(
            tmp_path, {"stack_choices": {"primary": "haskell-yesod"}}
        )

    def test_test_command_returns_none(self, tmp_path):
        a = GenericAdapter(project_path=tmp_path, manifest={})
        assert a.test_command() is None

    def test_all_optional_methods_return_none(self, tmp_path):
        a = GenericAdapter(project_path=tmp_path, manifest={})
        for method in ("lint_command", "static_analysis_command",
                       "coverage_command", "build", "deps_consistency_check"):
            assert getattr(a, method)() is None
        assert a.start_command(8000) is None
        assert a.cache_clean() == []


# ---------------------------------------------------------------------------
# rewrite_bare_venv_command — route test commands through `uv run`
# ---------------------------------------------------------------------------


class TestRewriteBareVenvCommand:
    @pytest.fixture(autouse=True)
    def _default_uv_python(self, monkeypatch):
        # Exercise the default pin unless a case overrides it.
        monkeypatch.delenv("AAH_UV_PYTHON", raising=False)

    def test_bare_python_pytest_routes_through_uv(self):
        got = rewrite_bare_venv_command(
            "python -m pytest test_f_mod_000.py -v -p no:asyncio --tb=short"
        )
        assert got == (
            "uv run --python 3.12 --with pytest python -m pytest "
            "test_f_mod_000.py -v -p no:asyncio --tb=short"
        )

    def test_bare_python3_pytest(self):
        got = rewrite_bare_venv_command("python3 -m pytest tests/")
        assert got == "uv run --python 3.12 --with pytest python3 -m pytest tests/"

    def test_bare_pytest_tool(self):
        got = rewrite_bare_venv_command("pytest tests/ -q")
        assert got == "uv run --python 3.12 --with pytest pytest tests/ -q"

    def test_venv_scripts_python_no_with_python(self):
        # The interpreter comes from uv; `--with python` must never appear.
        got = rewrite_bare_venv_command(
            ".venv/Scripts/python -m pytest tests/test_a.py tests/test_b.py -v"
        )
        assert got == (
            "uv run --python 3.12 --with pytest python -m pytest "
            "tests/test_a.py tests/test_b.py -v"
        )
        assert "--with python " not in got

    def test_venv_bin_pytest(self):
        got = rewrite_bare_venv_command(".venv/bin/pytest -q")
        assert got == "uv run --python 3.12 --with pytest pytest -q"

    def test_windows_python_exe_suffix_stripped(self):
        got = rewrite_bare_venv_command("python.exe -m pytest test_x.py")
        assert got == "uv run --python 3.12 --with pytest python -m pytest test_x.py"

    def test_non_pytest_interpreter_no_with_pytest(self):
        got = rewrite_bare_venv_command("python -m unittest")
        assert got == "uv run --python 3.12 python -m unittest"

    def test_uv_python_override_env(self, monkeypatch):
        monkeypatch.setenv("AAH_UV_PYTHON", "3.11")
        got = rewrite_bare_venv_command("pytest")
        assert got == "uv run --python 3.11 --with pytest pytest"

    def test_non_python_command_unchanged(self):
        for cmd in ("npm test", "go test ./...", "PYTHONPATH=. python -m pytest x.py"):
            assert rewrite_bare_venv_command(cmd) == cmd
