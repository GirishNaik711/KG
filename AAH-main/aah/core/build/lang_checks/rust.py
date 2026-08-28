"""Rust language adapter (Phase 3 L2).

Harvested from:
    aah-runtime-validator.md step 2     — cargo check
    execution_risk_monitor.py:306   — cargo build, cargo test as build-equivalents
    scaffold/project.py             — stack token (rust); fingerprint Cargo.toml

Detection: stack-string substring (rust) OR ``Cargo.toml`` file.
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


class RustAdapter(LanguageAdapter):
    name = "rust"
    family = "rust"

    @classmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        primary = manifest_stack_primary(manifest)
        if "rust" in primary:
            return True
        if (project_path / "Cargo.toml").exists():
            return True
        return False

    def install_deps(self) -> list[Cmd]:
        if not (self.project_path / "Cargo.toml").is_file():
            return []
        # Downloads dependencies into the shared registry cache without
        # building. Pre-fetching keeps parallel feature worktrees from each
        # re-resolving the same crates mid-test. `cargo fetch` writes
        # Cargo.lock only when it is absent; a project that commits its
        # lockfile — the norm for binaries — is untouched.
        return [
            Cmd(
                argv=["cargo", "fetch"],
                cwd=self.project_path,
                timeout_sec=600,
                label="cargo fetch",
            )
        ]

    def deps_consistency_check(self) -> Cmd | None:
        # `cargo tree -d` exits non-zero when duplicate-version
        # dependencies exist (a common cause of trait-conflict
        # compile errors).
        return Cmd(
            argv=["cargo", "tree", "-d"],
            cwd=self.project_path,
            timeout_sec=60,
            label="cargo tree -d",
        )

    def module_validation_command(
        self, files_or_modules: list[str] | None = None
    ) -> Cmd | None:
        # `cargo check` is faster than `cargo build` and only checks
        # compilation, not codegen. Right tool for module validation.
        return Cmd(
            argv=["cargo", "check"],
            cwd=self.project_path,
            timeout_sec=300,
            label="cargo check",
        )

    def build(self) -> Cmd | None:
        return Cmd(
            argv=["cargo", "build", "--release"],
            cwd=self.project_path,
            timeout_sec=600,
            label="cargo build --release",
        )

    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        argv = ["cargo", "test"]
        if feature_filter:
            # `cargo test <name>` filters tests by name substring.
            argv.append(feature_filter)
        return Cmd(
            argv=argv,
            cwd=self.project_path,
            timeout_sec=300,
            label="cargo test",
        )

    def test_report_globs(self) -> list[str]:
        # `cargo test` emits no XML. cargo-nextest does, when a profile sets
        # `junit.path`, and writes it under target/nextest/<profile>/. The
        # profile name is project-chosen, so glob across profiles rather than
        # assuming "ci". Plain `cargo test` yields no report and therefore
        # no_signal, not a zero-test pass.
        return [
            "target/nextest/*/junit.xml",
            "target/nextest/*/*.xml",
        ]

    def lint_command(self) -> Cmd | None:
        # clippy is the canonical Rust linter; `-D warnings` makes
        # warnings fail the lint gate.
        return Cmd(
            argv=["cargo", "clippy", "--", "-D", "warnings"],
            cwd=self.project_path,
            timeout_sec=300,
            label="cargo clippy",
        )

    def coverage_command(self) -> Cmd | None:
        # cargo-tarpaulin is the standard Rust coverage tool but isn't
        # installed by default. Return None and let projects opt-in
        # via a future manifest.stack_choices.coverage_command override.
        return None

    def start_command(self, port: int) -> Cmd | None:
        override = manifest_stack_field(self.manifest, "start_command")
        if override:
            return Cmd(
                argv=shlex.split(override),
                cwd=self.project_path,
                timeout_sec=60,
                label="start (from manifest)",
            )
        # `cargo run` works for binary crates with a default bin.
        return Cmd(
            argv=["cargo", "run", "--release"],
            cwd=self.project_path,
            timeout_sec=120,
            env={"PORT": str(port)},
            label="cargo run",
        )

    def cache_clean(self) -> list[Cmd]:
        return [
            Cmd(
                argv=["cargo", "clean"],
                cwd=self.project_path,
                timeout_sec=60,
                label="cargo clean",
            ),
        ]
