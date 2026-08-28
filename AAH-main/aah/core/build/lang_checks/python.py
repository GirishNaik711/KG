"""Python language adapter.

Harvested from:
    run_feature_tests.py:134-138       — pytest with -k feature filter
    run_regression_suite.py:90-95      — full pytest suite
    quality_checks.py:_resolve_*       — ruff, bandit, pytest --cov
    aah-runtime-validator.md step 2        — python3 -c imports
    scaffold/project.py:130-148        — stack tokens (python|fastapi|django|flask)
    scaffold/project.py:152-163        — file fingerprints (pyproject.toml, etc.)

Detection: stack-string substring (python|fastapi|django|flask) OR
file fingerprint (pyproject.toml, requirements.txt, setup.py, Pipfile).
"""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path
from typing import Any

from aah.core.build.lang_checks._helpers import (
    manifest_stack_field,
    manifest_stack_primary,
    path_to_python_module,
)
from aah.core.build.lang_checks.base import Cmd, LanguageAdapter

_STACK_TOKENS = ("python", "fastapi", "django", "flask")
_FINGERPRINT_FILES = ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")
# Candidate requirements files, in the order install_deps prefers them.
_REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-test.txt",
    "requirements-dev.txt",
)

# Test-directory discovery (see _tests_paths).
_TESTS_DIR_NAMES = ("tests", "test")
# <root>/tests, <root>/*/tests, <root>/*/*/tests — deep enough for
# backend/tests and services/api/tests, shallow enough to stay cheap.
_TESTS_MAX_DEPTH = 3
_TESTS_EXCLUDED_DIRS = frozenset({
    "node_modules", ".venv", "venv", ".aah", ".claude", "__pycache__",
    ".git", "build", "dist", ".tox", "site-packages",
})


class PythonAdapter(LanguageAdapter):
    name = "python"
    family = "python"

    @classmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        primary = manifest_stack_primary(manifest)
        if any(tok in primary for tok in _STACK_TOKENS):
            return True
        for fname in _FINGERPRINT_FILES:
            if (project_path / fname).exists():
                return True
            if any(project_path.glob(f"*/{fname}")):
                return True
        return False

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _python_root(self) -> Path:
        """Return the project root containing Python dependency metadata."""
        if any((self.project_path / name).is_file() for name in _FINGERPRINT_FILES):
            return self.project_path
        candidates = {
            path.parent
            for name in _FINGERPRINT_FILES
            for path in self.project_path.glob(f"*/{name}")
            if path.is_file()
        }
        return min(candidates, key=lambda path: path.as_posix()) if candidates else self.project_path

    def _relative_python_root(self) -> str:
        root = self._python_root()
        return "." if root == self.project_path else root.relative_to(self.project_path).as_posix()

    def _requirements(self) -> str | None:
        path = self._python_root() / "requirements.txt"
        return path.relative_to(self.project_path).as_posix() if path.is_file() else None

    def _requirements_file(self, root: Path) -> str | None:
        """First requirements file present in ``root``, by name only.

        Mirrors the candidate order the infra installer has always used, so
        moving the install here does not silently drop test/dev requirements.
        """
        for name in _REQUIREMENTS_FILES:
            if (root / name).is_file():
                return name
        return None

    def install_deps(self) -> list[Cmd]:
        root = self._python_root()
        requirements = self._requirements_file(root)
        has_pyproject = (root / "pyproject.toml").is_file()
        if not requirements and not has_pyproject:
            return []

        # ``uv sync`` is the right tool ONLY when a lockfile already exists.
        # Without one it writes uv.lock into the tree it is syncing, and the
        # tree here is the subject under test: the install runs after
        # capture_subject and before assert_subject_unchanged, so a new
        # uv.lock dirties the subject and invalidates the very evidence the
        # run is about to produce. ``--frozen`` never rewrites the lockfile
        # (verified), which makes this branch subject-safe.
        if has_pyproject and (root / "uv.lock").is_file():
            return [
                Cmd(
                    argv=["uv", "sync", "--frozen"],
                    cwd=root,
                    timeout_sec=600,
                    label="uv sync --frozen",
                )
            ]

        # Otherwise use the pip-compatible interface, which writes no
        # lockfile. This is also the branch that fixes the original defect:
        # ``uv pip install`` needs a virtualenv to install INTO, and a
        # worktree has none (``.venv/`` is gitignored, so it is absent from
        # every checkout). Creating it first is what turns the old
        # "No virtual environment found; run `uv venv`" abort into a working
        # install. ``.venv/`` being gitignored is also why this cannot dirty
        # the subject.
        cmds: list[Cmd] = []
        if not (root / ".venv").is_dir():
            # Conditional because `uv venv` refuses an existing environment
            # rather than treating it as a no-op.
            cmds.append(
                Cmd(argv=["uv", "venv"], cwd=root, timeout_sec=180, label="uv venv")
            )
        if requirements:
            cmds.append(
                Cmd(
                    argv=["uv", "pip", "install", "-r", requirements],
                    cwd=root,
                    timeout_sec=600,
                    label=f"uv pip install -r {requirements}",
                )
            )
        else:
            cmds.append(
                Cmd(
                    argv=["uv", "pip", "install", "-e", "."],
                    cwd=root,
                    timeout_sec=600,
                    label="uv pip install -e .",
                )
            )
        return cmds

    def _pyproject_root(self) -> str | None:
        """Project-root-relative python root iff it declares a pyproject.toml.

        Feeds ``uv run --project`` (R2) — the dependency source when a project
        has no requirements.txt.
        """
        root = self._python_root()
        if not (root / "pyproject.toml").is_file():
            return None
        return self._relative_python_root()

    def _uv_run(self, *tools: str, project_deps: bool = True) -> list[str]:
        """Build the ``uv run`` prefix for an ephemeral tool invocation.

        ``project_deps=False`` for tools that only need themselves (ruff,
        bandit). Such a run gets ``--no-project``, which is load-bearing rather
        than an optimization: uv otherwise DISCOVERS the ``pyproject.toml`` in
        cwd, resolves it, and writes a ``uv.lock`` into the subject — which
        trips ``assert_subject_unchanged`` and invalidates the very evidence the
        run just produced. Omitting ``--project`` is not enough; discovery has to
        be switched off explicitly. Anything that imports project code (pytest,
        coverage) genuinely needs the dependencies and keeps the default.
        """
        # Pin the interpreter uv resolves for ephemeral tool runs. Without this,
        # uv picks the newest installed CPython (e.g. 3.14/3.15), for which some
        # pinned deps (pydantic-core 2.23.4) ship no wheels and fail an in-place
        # Rust/maturin source build — so ruff/bandit/pytest --cov never start and
        # the standards gate fails closed with no_signal. Pin to a version with
        # wheels; override via AAH_UV_PYTHON when a project targets another.
        import os
        py = os.environ.get("AAH_UV_PYTHON", "3.12")
        argv = ["uv", "run"]
        if py:
            argv += ["--python", py]
        if not project_deps:
            argv += ["--no-project"]
            for tool in tools:
                argv += ["--with", tool]
            return argv
        requirements = self._requirements()
        if requirements:
            argv += ["--with-requirements", requirements]
        else:
            # No requirements.txt: resolve the project's own dependencies from
            # pyproject.toml. Without this the ephemeral env holds only what
            # --with supplies (no sqlalchemy, no pytest-asyncio), so collection
            # fails and the gate reports no_signal. --project (not
            # --with-editable) because it leaves cwd at the repo root, which
            # _tests_paths' relative paths and `python -m pytest`'s sys.path
            # entry both depend on.
            pyproject_root = self._pyproject_root()
            if pyproject_root:
                argv += ["--project", pyproject_root]
                # --project alone resolves main dependencies and the default
                # `dev` group only, so test tooling declared as an extra, or in a
                # group under any other name, is missing from the env: collection
                # then fails under --strict-markers and the suite executes zero
                # tests. Take every optional section rather than guessing which
                # one holds the test deps — a name/content heuristic cannot know
                # about `httpx`, `testcontainers` or a project's own test helper.
                # Only valid here; --with-requirements has no project context.
                argv += ["--all-extras", "--all-groups"]
        for tool in tools:
            argv += ["--with", tool]
        return argv

    def _tests_paths(self) -> list[str]:
        """Test directories that actually EXIST, project-root-relative POSIX.

        Discovery, not inference: the harness never defines where tests live
        (scaffold creates no tests/, no feature template prescribes a location),
        so layout is a per-project fact. Deriving it from the dependency root
        pointed pytest at invented paths (exit 4) and silently skipped a second
        test directory when one existed.

        Returns every match — a project may legitimately have more than one —
        and an empty list when there are none, so the caller can report "no
        tests discovered" instead of running against a path that isn't there.
        """
        found: list[str] = []

        def _walk(directory: Path, depth: int) -> None:
            if depth > _TESTS_MAX_DEPTH:
                return
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                return
            for entry in entries:
                if not entry.is_dir() or entry.name in _TESTS_EXCLUDED_DIRS:
                    continue
                if entry.name.startswith(".") or entry.name.endswith(".egg-info"):
                    continue
                if entry.name in _TESTS_DIR_NAMES:
                    found.append(
                        entry.relative_to(self.project_path).as_posix() + "/"
                    )
                    continue  # don't descend into a tests dir we already took
                _walk(entry, depth + 1)

        _walk(self.project_path, 1)
        return found

    def deps_consistency_check(self) -> Cmd | None:
        # uv pip check is the modern equivalent of `pip check` and works
        # under the framework's standard uv environment.
        return Cmd(
            argv=["uv", "pip", "check"],
            cwd=self.project_path,
            timeout_sec=30,
            label="pip check",
        )

    def module_validation_command(
        self, files_or_modules: list[str] | None = None
    ) -> Cmd | None:
        """Validate Python sources import.

        Two modes:
            - With ``files_or_modules``: build ``python3 -c "import a; import b"``
              from the path list (mirrors aah-runtime-validator.md step 2 prose).
            - Without: ``pytest --collect-only`` to confirm test discovery
              works — covers the common "did the imports break?" case
              for a wave-level check.
        """
        if not files_or_modules:
            # `python -m pytest` (not bare pytest) so cwd lands on sys.path and
            # root-anchored absolute imports (from backend.app.db import ...)
            # resolve at collection time.
            pytest_argv = ["python", "-m", "pytest"] + self._tests_paths()
            return Cmd(
                argv=self._uv_run("pytest") + pytest_argv + ["--collect-only", "-q"],
                cwd=self.project_path,
                timeout_sec=60,
                label="pytest collect",
            )
        modules = [
            path_to_python_module(f)
            for f in files_or_modules
            if isinstance(f, str) and f.endswith(".py")
        ]
        # Drop empties (e.g. a bare "__init__.py" at the root maps to "").
        modules = [m for m in modules if m]
        if not modules:
            return None
        import_stmts = "; ".join(f"import {m}" for m in modules)
        return Cmd(
            argv=self._uv_run() + ["python", "-c", import_stmts],
            cwd=self.project_path,
            timeout_sec=30,
            label=f"python imports ({len(modules)})",
        )

    # ------------------------------------------------------------------
    # Testing
    # ------------------------------------------------------------------

    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        """Pytest invocation — argv equivalent of:

            uv run python -m pytest <discovered tests dirs> -v --tb=short
                [-k <feature_filter>]

        Behavior-preserving with run_feature_tests.py:136 (with filter)
        and run_regression_suite.py:91 (without filter), modulo the
        --junitxml flag which the caller appends since it's pytest-
        specific output configuration, not a language-level fact.

        When no test directory is discovered we pass no path at all and let
        pytest discover from its rootdir — an honest "no tests ran" (exit 5)
        rather than exit 4 on a directory the adapter invented.
        """
        argv = self._uv_run("pytest") + [
            "python", "-m", "pytest", *self._tests_paths(), "-v", "--tb=short"
        ]
        if feature_filter:
            # Feature IDs are hyphenated (F-MOD000-00) but pytest -k matches
            # test node names, which are Python identifiers using underscores
            # (test_F_MOD000_00_...). Without this normalization -k selects 0
            # tests and the run silently reports success on an empty suite.
            argv += ["-k", feature_filter.replace("-", "_")]
        return Cmd(
            argv=argv,
            cwd=self.project_path,
            timeout_sec=300,
            label="pytest",
        )

    # ------------------------------------------------------------------
    # Quality
    # ------------------------------------------------------------------

    def lint_command(self) -> Cmd | None:
        # ruff is fast and the framework's standard. The old
        # quality_checks.py:290 chained ruff -> flake8 via shell || ;
        # we drop the chain here in favor of structured argv. If a
        # project doesn't have ruff configured, ruff exits non-zero —
        # the caller treats that as lint failure, which is correct.
        #
        # concise, NOT json. The consumer of lint output is aah-fix, which needs
        # what a developer would read (file:line: CODE message, plus an [*]
        # auto-fixable marker) — not a schema. JSON was ~10x larger for the same
        # findings, so a whole-codebase run overflowed the 3000-char stdout
        # capture and handed the repair route a truncated fragment.
        return Cmd(
            argv=self._uv_run("ruff", project_deps=False) + [
                "ruff", "check", self._relative_python_root(),
                "--output-format=concise",
            ],
            cwd=self.project_path,
            timeout_sec=60,
            label="ruff",
        )

    def static_analysis_command(self) -> Cmd | None:
        # bandit's exclude-matching only works when an entry's prefix matches
        # how it names discovered paths for this -r root ("./" when root is
        # ".", "backend/" when root is "backend") — wrong or missing prefix
        # silently no-ops and bandit scans the dir anyway. CLI --exclude also
        # replaces rather than merges with a .bandit file's own `exclude =`,
        # so we read+normalize+union that declaration instead of dropping it.
        def _normalize(entry: str, prefix: str) -> str:
            if entry.startswith(prefix) or entry.startswith("../"):
                return entry
            if entry.startswith("./"):
                entry = entry[2:]
            return f"{prefix}{entry.lstrip('/')}"

        root = self._relative_python_root()
        prefix = "./" if root == "." else f"{root}/"

        bandit_ini = self.project_path / ".bandit"
        excludes = {
            ".venv", "venv", ".git", "node_modules", ".aah", ".claude",
            "tests", "__pycache__",
        }
        if bandit_ini.exists():
            parser = configparser.ConfigParser()
            try:
                parser.read(bandit_ini)
                declared = parser.get("bandit", "exclude", fallback="")
            except configparser.Error:
                declared = ""
            excludes |= {e.strip() for e in declared.split(",") if e.strip()}

        exclude_arg = ",".join(sorted({_normalize(e, prefix) for e in excludes}))
        argv = self._uv_run("bandit", project_deps=False) + [
            "bandit", "-r", root, "--exclude", exclude_arg, "-f", "json"
        ]

        # --ini skips bandit's config-file DISCOVERY, which aborts with exit 2
        # ("Multiple .bandit files found") across per-feature worktrees.
        if bandit_ini.exists():
            argv += ["--ini", ".bandit"]

        return Cmd(
            argv=argv,
            cwd=self.project_path,
            timeout_sec=120,
            label="bandit",
        )

    def coverage_command(self) -> Cmd | None:
        pytest_argv = ["python", "-m", "pytest"] + self._tests_paths()
        return Cmd(
            argv=self._uv_run("pytest", "pytest-cov") + pytest_argv + [
                f"--cov={self._relative_python_root()}",
                "--cov-report=term-missing", "--tb=no", "-q",
            ],
            cwd=self.project_path,
            timeout_sec=300,
            label="coverage",
        )

    def _has_mypy_config(self) -> bool:
        """True iff the project opted into mypy.

        Presence check only — substring the section header rather than
        parse toml/ini (lazy: we just need "did they configure mypy?").
        """
        p = self._python_root()
        if (p / "mypy.ini").exists() or (p / ".mypy.ini").exists():
            return True
        for fname, section in (("pyproject.toml", "[tool.mypy]"),
                               ("setup.cfg", "[mypy]")):
            fpath = p / fname
            if fpath.exists():
                try:
                    if section in fpath.read_text(encoding="utf-8"):
                        return True
                except OSError:
                    pass
        return False

    def type_check_command(self) -> Cmd | None:
        # mypy only when the project configured it; untyped Python skips
        # (None) rather than force-running mypy over unannotated code.
        if not self._has_mypy_config():
            return None
        return Cmd(
            argv=self._uv_run("mypy") + ["mypy", self._relative_python_root()],
            cwd=self.project_path,
            timeout_sec=120,
            label="mypy",
        )

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    def _has_web_surface(self) -> bool:
        """True iff this project serves HTTP (mirror _has_mypy_config).

        Presence-based, cheap: manifest stack token, a web dependency in
        pyproject/requirements text, or a conventional web entrypoint.
        """
        primary = manifest_stack_primary(self.manifest)
        if any(tok in primary for tok in ("fastapi", "django", "flask")):
            return True
        p = self._python_root()
        web_deps = ("fastapi", "flask", "django", "uvicorn", "starlette", "gunicorn")
        for fname in ("pyproject.toml", "requirements.txt"):
            fpath = p / fname
            if fpath.exists():
                try:
                    text = fpath.read_text(encoding="utf-8").lower()
                    if any(dep in text for dep in web_deps):
                        return True
                except OSError:
                    pass
        return any((p / entry).exists() for entry in ("main.py", "app.py", "manage.py")) or any(
            (p / "app" / entry).exists() for entry in ("main.py", "app.py")
        )

    def has_web_surface(self) -> bool:
        return self._has_web_surface()

    def start_command(self, port: int) -> Cmd | None:
        # CLI/no-server projects: set stack_choices.no_server: true to skip.
        if (self.manifest or {}).get("stack_choices", {}).get("no_server"):
            return None
        # Manifest override wins — projects with non-FastAPI entrypoints
        # (Django manage.py, Flask app.py, etc.) set this explicitly.
        override = manifest_stack_field(self.manifest, "start_command")
        if override:
            return Cmd(
                argv=shlex.split(override),
                cwd=self.project_path,
                timeout_sec=60,
                label="start (from manifest)",
            )
        # Non-web projects (libraries, CLIs, workers)
        # have no server to boot. Return None → server_start skips cleanly
        # instead of force-booting uvicorn into a guaranteed failure.
        if not self._has_web_surface():
            return None
        # Default: FastAPI/uvicorn. Most greenfield Python web projects
        # in this codebase are FastAPI; non-FastAPI web projects set the
        # manifest override.
        python_root = self._python_root()
        entrypoint = next(
            (
                candidate for candidate in (
                    python_root / "app" / "main.py",
                    python_root / "main.py",
                    python_root / "app.py",
                )
                if candidate.is_file()
            ),
            None,
        )
        module = (
            entrypoint.relative_to(self.project_path).with_suffix("").as_posix().replace("/", ".")
            if entrypoint else "main"
        )
        return Cmd(
            argv=self._uv_run("uvicorn") + [
                "uvicorn", f"{module}:app", "--port", str(port)
            ],
            cwd=self.project_path,
            timeout_sec=60,
            label="uvicorn",
        )

    def cache_clean(self) -> list[Cmd]:
        return [
            Cmd(
                argv=[
                    "find", ".", "-name", "__pycache__", "-type", "d",
                    "-exec", "rm", "-rf", "{}", "+",
                ],
                cwd=self.project_path,
                timeout_sec=30,
                label="clean __pycache__",
            ),
        ]
