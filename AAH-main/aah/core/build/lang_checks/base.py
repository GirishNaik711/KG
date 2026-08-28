"""Language adapter contract for the AAH build phase.

Per-language commands (compile, build, test, lint, static-analysis,
coverage, module-validation, start-command, deps-check, cache-clean)
live in exactly one file per language under ``lang_checks/``. Every
implement-phase Python module and the aah-runtime-validator agent's
step-2 prose call into this pack instead of inlining per-language
branches. Adding a new language is one file (see Adding a new language
below).

Why structured Cmd objects rather than bare strings:
  - subprocess.run(shell=False) requires argv (list of strings); a
    structured type prevents accidental shell-injection and parameter
    splitting bugs.
  - The attestation library signs commands; a list[str] serialises
    cleanly to JSON, a shell string forces every signer to make the
    same shlex.split decision.
  - Callers can inspect timeout/cwd/env without parsing strings.

Adding a new language:
  1. Create lang_checks/<lang>.py with a class subclassing
     LanguageAdapter, implementing detect() and the relevant command
     methods. Methods you don't support return None.
  2. Register the class in lang_checks/__init__.py's _ADAPTERS tuple,
     before GenericAdapter (which is the always-true fallback).
  3. Add a fixture project under scripts/tests/fixtures/lang_<lang>/
     (just the detection fingerprint files) and a per-adapter test
     in test_lang_checks_<lang>.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Cmd:
    """A resolved command, structured rather than a bare shell string.

    Fields:
        argv: argument vector for subprocess.run(shell=False).
        cwd: working directory for the command. Caller may override.
        timeout_sec: subprocess timeout in seconds.
        env: extra environment variables to merge into the process env
            (does not replace os.environ; callers do {**os.environ, **cmd.env}).
        label: human-readable short label shown in CLI banners and
            stderr messages, e.g. "pytest" or "ruff".
    """

    argv: list[str]
    cwd: Path | None = None
    timeout_sec: int = 300
    env: dict[str, str] = field(default_factory=dict)
    label: str = ""


class LanguageAdapter(ABC):
    """Base class for per-language command adapters.

    All command methods return ``Cmd | None``. ``None`` means "this
    adapter doesn't support this operation" — caller skips it
    gracefully (no exception raised). This contract is what lets the
    GenericAdapter exist as a clean fallback for unknown stacks.

    Subclass requirements:
        - Set ``name`` (e.g. "python") and ``family`` (e.g. "python").
        - Implement ``detect(cls, project_path, manifest)`` (classmethod).
        - Implement ``test_command(self, feature_filter=None)``.
        - Override any other command method you support; defaults
          return ``None``.
    """

    # Short identifier, e.g. "python", "node". Reported in CLI output.
    name: str = ""
    # Broader family — multiple sub-adapters may share a family.
    # E.g. PythonAdapter handles "python", "fastapi", "django", "flask"
    # all under family "python".
    family: str = ""

    def __init__(self, project_path: Path, manifest: dict[str, Any]):
        self.project_path = project_path
        self.manifest = manifest or {}

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    @classmethod
    @abstractmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        """True iff this adapter is the right fit for the project.

        Recommended detection priority inside an implementation:
          1. ``manifest.stack_choices.primary`` substring match
             (e.g. "python-fastapi" matches Python).
          2. File-presence fingerprint (e.g. ``pyproject.toml`` exists).
          3. For the GenericAdapter only: always-True fallback.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Validation gates (in pipeline order)
    # ------------------------------------------------------------------

    def install_deps(self) -> list[Cmd]:
        """Commands that install this project's dependencies into ``cwd``.

        Returned as a list because some languages need more than one step (a
        requirements-only Python project needs ``uv venv`` before
        ``uv pip install``), following the same shape as ``cache_clean``.
        Callers run them in order and stop at the first failure.

        Why this exists on the adapter: a feature is built in its own git
        worktree, and a worktree is a checkout of *tracked* files only.
        Dependency directories are gitignored by design (``node_modules/``,
        ``.venv/``), so they are absent from every worktree until something
        installs them there. The installer used to know only Python, which
        meant a JS/Java/Go/Rust feature ran its tests with no test runner
        present and failed for reasons that had nothing to do with its code.

        Return ``[]`` when there is nothing to install (no dependency
        manifest, or a language whose test command resolves dependencies
        itself). An empty list is a clean no-op, never an error.
        """
        return []

    def deps_consistency_check(self) -> Cmd | None:
        """Verify dependencies install cleanly / no version conflicts.
        e.g. ``npm ls``, ``pip check``, ``go mod verify``, ``cargo tree -d``.
        Return None if the language has no equivalent.
        """
        return None

    def module_validation_command(
        self, files_or_modules: list[str] | None = None
    ) -> Cmd | None:
        """Validate that source files compile / import.

        For Python this is ``python3 -c "import a; import b"`` (or
        ``pytest --collect-only`` when ``files_or_modules`` is None);
        for Go it's ``go build ./...``; for Rust ``cargo check``; for
        Node ``npx tsc --noEmit`` if ``tsconfig.json`` exists.

        Replaces the aah-runtime-validator agent's step-2 prose. The
        ``files_or_modules`` parameter exists for languages that
        benefit from scoping (Python imports a list of modules); other
        languages may ignore it.
        """
        return None

    def compile_or_typecheck(self) -> Cmd | None:
        """No-arg alias for ``module_validation_command(None)``.

        Convenience for callers that don't have a specific file list.
        Override if a language has a separate "typecheck only" path
        distinct from module validation.
        """
        return self.module_validation_command(None)

    def build(self) -> Cmd | None:
        """Production build, if applicable.
        e.g. ``npm run build``, ``go build -o ...``, ``cargo build --release``.
        """
        return None

    # ------------------------------------------------------------------
    # Per-feature & full-suite testing
    # ------------------------------------------------------------------

    @abstractmethod
    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        """Test command, optionally scoped to a feature ID.

        ``feature_filter`` is a string like ``"F001"`` the test runner
        should match against test names (``pytest -k``, ``npm test --grep``,
        ``go test -run``, ``cargo test <name>``, ``mvn -Dtest=``,
        ``gradle --tests``). ``None`` means full suite.

        Return ``None`` if the adapter cannot resolve a runnable test
        command (e.g. GenericAdapter, or a project that needs explicit
        configuration via ``feature.test_config.command``).

        Test-directory contract — DISCOVER, never infer:
          An adapter that passes explicit test paths must pass only
          directories that EXIST, must pass ALL of them, and must pass none
          when there are none. The harness defines no test location: the
          scaffold creates no test directory and no feature template
          prescribes one, so layout is a per-project fact (greenfield,
          brownfield-imported, or pre-existing).

          Deriving the test path from the dependency root — the defect this
          contract exists to prevent — fails two ways: it points the runner at
          a directory that isn't there (pytest exit 4, reported as a build
          failure), and when tests live in two places it passes one and
          silently never runs the other, which looks green while covering half
          the suite. Prefer the runner's own rootdir discovery (bare
          ``pytest``, ``go test ./...``) when it covers every case.
        """
        raise NotImplementedError

    def test_report_globs(self) -> list[str]:
        """Glob patterns, relative to the test cwd, where this language's
        test runner writes JUnit XML of its own accord.

        The harness injects an explicit report path only for pytest (via
        PYTEST_ADDOPTS). Every other runner either writes JUnit XML to a
        conventional location (Maven Surefire, Gradle) or writes none at all
        until its project config names a JUnit reporter (Playwright, vitest,
        gotestsum, nextest). Declaring the conventional locations here is what
        lets the evidence layer read results for non-pytest stacks instead of
        recording an empty summary.

        Return ``[]`` when the language has no conventional location. An empty
        list is not "zero tests" — the caller reports ``no_signal`` when no
        report can be read, because an unreadable result must never be
        mistaken for a passing one.
        """
        return []

    # ------------------------------------------------------------------
    # Quality
    # ------------------------------------------------------------------

    def lint_command(self) -> Cmd | None:
        return None

    def static_analysis_command(self) -> Cmd | None:
        return None

    def coverage_command(self) -> Cmd | None:
        return None

    def type_check_command(self) -> Cmd | None:
        """Static type-check, config-gated. mypy / tsc --noEmit.

        Return None when the stack has no type config (gradually-typed
        TS, vanilla JS, untyped Python) — the caller skips cleanly and
        NEVER blocks on None.
        """
        return None

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    def start_command(self, port: int) -> Cmd | None:
        """Start the application on the given port.

        Adapters consult ``manifest.stack_choices.start_command`` first
        (parsed via ``shlex.split`` for argv), then fall back to
        language-specific defaults (e.g. ``uvicorn main:app`` for
        FastAPI; ``npm run dev`` for Vite).
        """
        return None

    def health_probe_path(self) -> str:
        """HTTP path the boot health-probe and smoke fallback hit.

        Consult ``manifest.stack_choices.health_path`` first (apps mounted
        under a non-root prefix like ``/api`` set this), else default ``/``.
        Overridden by adapters that need a framework-specific default.
        """
        from aah.core.build.lang_checks._helpers import manifest_stack_field
        return manifest_stack_field(self.manifest, "health_path") or "/"

    def has_web_surface(self) -> bool:
        """True iff this project serves HTTP (should be booted + probed).

        Drives the bootability gate: a web-surface project that skips
        server_start is a blocking gap (no signal ≠ green); a non-web
        project (library / CLI / worker) skips boot cleanly. Default False
        — adapters that can serve HTTP override this.
        """
        return False

    def cache_clean(self) -> list[Cmd]:
        """Cache-clean commands to run before a fresh build/test.

        Returned as a list because some languages need multiple steps
        (e.g. ``rm -rf .vite`` then ``rm -rf node_modules/.cache``).
        Default returns an empty list (no-op).
        """
        return []


__all__ = ["Cmd", "LanguageAdapter"]
