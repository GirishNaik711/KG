"""Node / TypeScript / JavaScript / React language adapter.

Harvested from:
    run_feature_tests.py:138       — npm test --grep <feature>
    run_regression_suite.py:93     — npm test
    quality_checks.py:_resolve_*   — eslint, npm run lint, npm run test:coverage
    aah-runtime-validator.md step 2    — npx tsc --noEmit
    scaffold/project.py            — stack tokens (node|react|next|vue|angular|express|typescript|javascript)

Detection: stack-string substring OR file fingerprint (package.json,
package-lock.json, yarn.lock, pnpm-lock.yaml).

Decisions that depend on package.json contents:
  - lint_command: prefer ``npm run lint`` if defined, else ``npx eslint``.
  - build: ``npm run build`` only if defined.
  - coverage: ``npm run test:coverage`` if defined; otherwise None
    (Node has no de-facto coverage default; users opt in).
  - module_validation: ``npx tsc --noEmit`` only if ``tsconfig.json``
    exists; otherwise None (vanilla JS doesn't have a typecheck step).
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from aah.core.build.lang_checks._helpers import (
    manifest_stack_field,
    manifest_stack_primary,
)
from aah.core.build.lang_checks.base import Cmd, LanguageAdapter


_STACK_TOKENS = (
    "node", "react", "next", "vue", "angular", "express",
    "typescript", "javascript",
)
_FINGERPRINT_FILES = (
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
)


def _read_pkg_json(project_path: Path) -> dict:
    """Return the parsed package.json dict, or empty dict."""
    pkg_path = project_path / "package.json"
    if not pkg_path.exists():
        return {}
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_npm_scripts(project_path: Path) -> dict[str, str]:
    """Return the ``scripts`` dict from package.json, or empty dict."""
    scripts = _read_pkg_json(project_path).get("scripts", {})
    return scripts if isinstance(scripts, dict) else {}


class NodeAdapter(LanguageAdapter):
    name = "node"
    family = "node"

    @classmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        primary = manifest_stack_primary(manifest)
        if any(tok in primary for tok in _STACK_TOKENS):
            return True
        for fname in _FINGERPRINT_FILES:
            if (project_path / fname).exists():
                return True
        return False

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def install_deps(self) -> list[Cmd]:
        if not (self.project_path / "package.json").is_file():
            return []
        # `ci` is preferred: it installs exactly the lockfile and, unlike
        # `install`, never rewrites it — which matters because this runs
        # against the subject under test, between capture_subject and
        # assert_subject_unchanged, so a rewritten lockfile would dirty the
        # subject and invalidate the run's own evidence.
        #
        # It requires a lockfile to exist in the tree, though. A project that
        # gitignores its lockfile has none in the worktree (a worktree holds
        # tracked files only), so `ci` would abort there and `install` is the
        # only option that works.
        if (self.project_path / "package-lock.json").is_file():
            argv = ["npm", "ci"]
        elif (self.project_path / "pnpm-lock.yaml").is_file():
            argv = ["pnpm", "install", "--frozen-lockfile"]
        elif (self.project_path / "yarn.lock").is_file():
            argv = ["yarn", "install", "--frozen-lockfile"]
        else:
            # --no-package-lock because a plain `npm install` WRITES
            # package-lock.json, and an untracked lockfile is subject drift:
            # it is not in DEFAULT_EPHEMERAL_GLOBS and the drift check does not
            # skip untracked entries, so assert_subject_unchanged would fail
            # the run it just installed for. There is no lockfile to honour on
            # this branch anyway, so nothing reproducible is given up.
            argv = ["npm", "install", "--no-package-lock"]
        return [
            Cmd(
                argv=argv,
                cwd=self.project_path,
                # Generous: a cold install of a browser-test stack pulls
                # hundreds of packages, and on a Windows-mounted filesystem
                # that is minutes, not seconds.
                timeout_sec=900,
                label=" ".join(argv),
            )
        ]

    def deps_consistency_check(self) -> Cmd | None:
        # `npm ls` exits non-zero on peer-dep mismatches and missing
        # packages — exactly the MUI v5/v9 mismatch class PartsPulse
        # iter-2 hit. Depth=2 keeps it fast on big trees.
        return Cmd(
            argv=["npm", "ls", "--depth=2", "--json"],
            cwd=self.project_path,
            timeout_sec=60,
            label="npm ls",
        )

    def module_validation_command(
        self, files_or_modules: list[str] | None = None
    ) -> Cmd | None:
        # Only TypeScript projects have a typecheck step. Vanilla JS
        # has no equivalent; the agent's old prose said
        # `node -e "require('./src/index')"` for JS but that's project-
        # specific (entrypoint varies); skip it here.
        if (self.project_path / "tsconfig.json").exists():
            return Cmd(
                argv=["npx", "tsc", "--noEmit"],
                cwd=self.project_path,
                timeout_sec=120,
                label="tsc --noEmit",
            )
        return None

    def build(self) -> Cmd | None:
        scripts = _read_npm_scripts(self.project_path)
        if "build" in scripts:
            return Cmd(
                argv=["npm", "run", "build"],
                cwd=self.project_path,
                timeout_sec=300,
                label="npm run build",
            )
        return None

    # ------------------------------------------------------------------
    # Testing
    # ------------------------------------------------------------------

    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        # Behavior-preserving with run_feature_tests.py:138 and
        # run_regression_suite.py:93. The `-- --grep` form passes
        # --grep through to the underlying test runner (mocha/jest/etc).
        argv = ["npm", "test"]
        if feature_filter:
            argv += ["--", "--grep", feature_filter]
        return Cmd(
            argv=argv,
            cwd=self.project_path,
            timeout_sec=300,
            label="npm test",
        )

    def test_report_globs(self) -> list[str]:
        # No JS runner writes JUnit XML by default — Playwright, vitest and
        # jest all need a reporter named in project config (for example
        # `reporter: [["junit", { outputFile: "test-results/junit.xml" }]]`).
        # These are the conventional paths those reporters default to, so a
        # project that configures one is picked up with no harness change.
        # A project that configures none reports no_signal rather than a
        # silent zero-test pass.
        return [
            "junit.xml",
            "test-results/junit.xml",
            "test-results/*.xml",
            "reports/junit.xml",
            "reports/junit/*.xml",
            "coverage/junit.xml",
        ]

    # ------------------------------------------------------------------
    # Quality
    # ------------------------------------------------------------------

    def lint_command(self) -> Cmd | None:
        # Mirrors quality_checks.py:294-298: prefer the project's
        # `npm run lint` script if defined; otherwise fall back to
        # `npx eslint`.
        scripts = _read_npm_scripts(self.project_path)
        if "lint" in scripts:
            return Cmd(
                argv=["npm", "run", "lint"],
                cwd=self.project_path,
                timeout_sec=60,
                label="npm run lint",
            )
        return Cmd(
            argv=["npx", "eslint", ".", "--format=json"],
            cwd=self.project_path,
            timeout_sec=60,
            label="eslint",
        )

    def static_analysis_command(self) -> Cmd | None:
        # quality_checks.py:307 runs eslint with a --rule '{"no-eval": "error"}'
        # override. Argv form needs a single string for --rule; jsonable.
        return Cmd(
            argv=[
                "npx", "eslint", ".",
                "--rule", '{"no-eval": "error"}',
                "--format=json",
            ],
            cwd=self.project_path,
            timeout_sec=60,
            label="eslint security",
        )

    def coverage_command(self) -> Cmd | None:
        # Only return a command if the project defined one. Unlike
        # Python's pytest --cov default, Node has no de-facto answer
        # (jest/vitest/c8/nyc all have different invocations).
        scripts = _read_npm_scripts(self.project_path)
        if "test:coverage" in scripts:
            return Cmd(
                argv=["npm", "run", "test:coverage"],
                cwd=self.project_path,
                timeout_sec=300,
                label="npm run test:coverage",
            )
        return None

    def type_check_command(self) -> Cmd | None:
        # Only TypeScript projects (tsconfig.json present) have a
        # typecheck step; vanilla JS / gradually-typed skips (None).
        # Same gate as module_validation_command — kept independent
        # since Python's module_validation is pytest-collect, not typing.
        if (self.project_path / "tsconfig.json").exists():
            return Cmd(
                argv=["npx", "tsc", "--noEmit"],
                cwd=self.project_path,
                timeout_sec=120,
                label="tsc --noEmit",
            )
        return None

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    def start_command(self, port: int) -> Cmd | None:
        override = manifest_stack_field(self.manifest, "start_command")
        if override:
            return Cmd(
                argv=shlex.split(override),
                cwd=self.project_path,
                timeout_sec=60,
                label="start (from manifest)",
            )
        # Default: most greenfield Node/React projects in this codebase
        # are Vite-style with `npm run dev`. Projects that need
        # different behavior set the manifest override.
        scripts = _read_npm_scripts(self.project_path)
        if "dev" in scripts:
            # Vite IGNORES env PORT (serves 5173) → guaranteed
            # health timeout. Pass --port through so the dev server binds
            # the port we actually probe. Vite/most dev servers honor it.
            return Cmd(
                argv=["npm", "run", "dev", "--", "--port", str(port)],
                cwd=self.project_path,
                timeout_sec=60,
                label="npm run dev",
            )
        if "start" in scripts:
            return Cmd(
                argv=["npm", "start"],
                cwd=self.project_path,
                timeout_sec=60,
                env={"PORT": str(port)},
                label="npm start",
            )
        return None

    def has_web_surface(self) -> bool:
        # A Node project serves HTTP if its stack token is a web framework
        # or a web dep is declared. Bare tooling/lib packages return False.
        primary = manifest_stack_primary(self.manifest)
        if any(tok in primary for tok in
               ("react", "next", "vue", "angular", "express")):
            return True
        pkg = _read_pkg_json(self.project_path)
        deps = {}
        for key in ("dependencies", "devDependencies"):
            d = pkg.get(key)
            if isinstance(d, dict):
                deps.update(d)
        web_markers = ("react", "vue", "next", "@angular", "express", "vite")
        return any(any(m in name for m in web_markers) for name in deps)

    def cache_clean(self) -> list[Cmd]:
        # Vite cache + node_modules/.cache cover the most common
        # cache staleness issues PartsPulse iter-2 hit (504 Outdated
        # Optimize Dep, etc.). Keep it minimal — destructive cleans
        # like `rm -rf node_modules` are user-initiated, not framework.
        return [
            Cmd(
                argv=["rm", "-rf", ".vite"],
                cwd=self.project_path,
                timeout_sec=10,
                label="clean .vite",
            ),
            Cmd(
                argv=["rm", "-rf", "node_modules/.cache"],
                cwd=self.project_path,
                timeout_sec=10,
                label="clean node_modules/.cache",
            ),
        ]
