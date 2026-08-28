"""Go language adapter (Phase 3 L2).

Harvested from:
    aah-runtime-validator.md step 2     — go build ./...
    execution_risk_monitor.py:306   — go build, go test as build-equivalents
    scaffold/project.py             — stack tokens (go|golang); fingerprint go.mod

Detection: stack-string substring (go|golang) OR ``go.mod`` file.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from aah.core.build.lang_checks._helpers import (
    manifest_stack_field,
    manifest_stack_primary,
)
from aah.core.build.lang_checks.base import Cmd, LanguageAdapter


_STACK_TOKENS = ("go", "golang")


class GoAdapter(LanguageAdapter):
    name = "go"
    family = "go"

    @classmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        primary = manifest_stack_primary(manifest)
        # Token "go" is short — guard against false positives like
        # "django" or "ruby on rails go-langish-name". Match on word
        # boundaries via separator-aware check.
        for tok in _STACK_TOKENS:
            for sep in ("-", "_", " ", ""):
                # Detect tokens at start, middle, or end with common
                # separators. e.g. "go-net-http", "golang-server".
                if (
                    primary == tok
                    or primary.startswith(f"{tok}{sep}") and sep
                    or primary.endswith(f"{sep}{tok}") and sep
                    or f"{sep}{tok}{sep}" in primary and sep
                ):
                    return True
            if tok == "golang" and tok in primary:
                # "golang" is unambiguous regardless of separator.
                return True
        if (project_path / "go.mod").exists():
            return True
        return False

    def install_deps(self) -> list[Cmd]:
        if not (self.project_path / "go.mod").is_file():
            return []
        # Populates the module cache without building. `go test` would resolve
        # these itself, but doing it here means N parallel feature worktrees
        # do not each race the same downloads mid-test. Writes only to the
        # shared module cache, never into the subject.
        return [
            Cmd(
                argv=["go", "mod", "download"],
                cwd=self.project_path,
                timeout_sec=600,
                label="go mod download",
            )
        ]

    def deps_consistency_check(self) -> Cmd | None:
        return Cmd(
            argv=["go", "mod", "verify"],
            cwd=self.project_path,
            timeout_sec=60,
            label="go mod verify",
        )

    def module_validation_command(
        self, files_or_modules: list[str] | None = None
    ) -> Cmd | None:
        # `go build ./...` checks every package compiles. Doesn't write
        # a binary (that's a separate `build()`).
        return Cmd(
            argv=["go", "build", "./..."],
            cwd=self.project_path,
            timeout_sec=180,
            label="go build ./...",
        )

    def build(self) -> Cmd | None:
        # Production build — write binary to a throwaway path.
        # ``go build -o`` requires a path; we use a tmp-style filename
        # rather than the cwd-default to avoid polluting the project.
        return Cmd(
            argv=["go", "build", "-o", "/tmp/aah-go-build-output", "./..."],
            cwd=self.project_path,
            timeout_sec=300,
            label="go build (binary)",
        )

    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        argv = ["go", "test", "./..."]
        if feature_filter:
            # `go test -run <regex>` matches test function names.
            # Feature IDs like F001 work as substrings of test names
            # if the project follows a "TestF001_..." convention.
            argv += ["-run", feature_filter]
        return Cmd(
            argv=argv,
            cwd=self.project_path,
            timeout_sec=300,
            label="go test",
        )

    def test_report_globs(self) -> list[str]:
        # `go test` emits no XML at all; JUnit output requires gotestsum
        # (`gotestsum --junitfile=<path>`) or go-junit-report. These are the
        # paths those tools are conventionally pointed at, so a project that
        # adopts one is read automatically. Plain `go test` yields no report
        # and therefore no_signal, not a zero-test pass.
        return [
            "junit.xml",
            "test-results/junit.xml",
            "test-results/*.xml",
        ]

    def lint_command(self) -> Cmd | None:
        return Cmd(
            argv=["go", "vet", "./..."],
            cwd=self.project_path,
            timeout_sec=60,
            label="go vet",
        )

    def coverage_command(self) -> Cmd | None:
        return Cmd(
            argv=["go", "test", "-cover", "./..."],
            cwd=self.project_path,
            timeout_sec=300,
            label="go test -cover",
        )

    def start_command(self, port: int) -> Cmd | None:
        override = manifest_stack_field(self.manifest, "start_command")
        if override:
            return Cmd(
                argv=shlex.split(override),
                cwd=self.project_path,
                timeout_sec=60,
                label="start (from manifest)",
            )
        # No de-facto Go entrypoint. `go run main.go` works for many
        # projects but not all (cmd/server/main.go layouts, etc.).
        # Require manifest override for non-default layouts.
        if (self.project_path / "main.go").exists():
            return Cmd(
                argv=["go", "run", "main.go"],
                cwd=self.project_path,
                timeout_sec=60,
                env={"PORT": str(port)},
                label="go run main.go",
            )
        return None

    def cache_clean(self) -> list[Cmd]:
        return [
            Cmd(
                argv=["go", "clean", "-cache"],
                cwd=self.project_path,
                timeout_sec=30,
                label="go clean -cache",
            ),
        ]
