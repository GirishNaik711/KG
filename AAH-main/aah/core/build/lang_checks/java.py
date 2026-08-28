"""Java / Maven / Gradle language adapter (Phase 3 L2).

Harvested from:
    aah-runtime-validator.md step 2     — mvn compile -q OR gradle compileJava
    execution_risk_monitor.py:306   — mvn, gradle as build-equivalents
    scaffold/project.py             — stack tokens (java|spring|maven|gradle);
                                      fingerprints pom.xml, build.gradle

Detection: stack-string substring OR file fingerprint. Within Java,
Maven vs Gradle is decided by which build file is present (pom.xml
beats build.gradle if both are present, on the assumption that a
project with both is primarily Maven).
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


_STACK_TOKENS = ("java", "spring", "maven", "gradle")


class JavaAdapter(LanguageAdapter):
    name = "java"
    family = "java"

    @classmethod
    def detect(cls, project_path: Path, manifest: dict[str, Any]) -> bool:
        primary = manifest_stack_primary(manifest)
        if any(tok in primary for tok in _STACK_TOKENS):
            return True
        if (project_path / "pom.xml").exists():
            return True
        if (project_path / "build.gradle").exists():
            return True
        if (project_path / "build.gradle.kts").exists():
            return True
        return False

    def _is_maven(self) -> bool:
        """Maven if pom.xml exists; Gradle otherwise (default Gradle
        when neither is present, e.g. detection by stack-string only).
        """
        return (self.project_path / "pom.xml").exists()

    def install_deps(self) -> list[Cmd]:
        if self._is_maven():
            # Resolves every dependency and plugin into the shared ~/.m2 cache
            # up front. `mvn test` would resolve them anyway, but pre-fetching
            # keeps N parallel feature worktrees from racing the same
            # downloads into one local repository mid-test. Writes only to
            # ~/.m2, never into the subject.
            return [
                Cmd(
                    argv=["mvn", "-q", "-B", "dependency:go-offline"],
                    cwd=self.project_path,
                    timeout_sec=900,
                    label="mvn dependency:go-offline",
                )
            ]
        if (self.project_path / "build.gradle").is_file() or (
            self.project_path / "build.gradle.kts"
        ).is_file():
            return [
                Cmd(
                    argv=["gradle", "--quiet", "testClasses"],
                    cwd=self.project_path,
                    timeout_sec=900,
                    label="gradle testClasses",
                )
            ]
        return []

    def deps_consistency_check(self) -> Cmd | None:
        if self._is_maven():
            # `mvn dependency:tree` reports conflicts; doesn't fail
            # build but is a useful artifact for auditing.
            return Cmd(
                argv=["mvn", "dependency:tree", "-q"],
                cwd=self.project_path,
                timeout_sec=120,
                label="mvn dependency:tree",
            )
        return Cmd(
            argv=["gradle", "dependencies", "-q"],
            cwd=self.project_path,
            timeout_sec=120,
            label="gradle dependencies",
        )

    def module_validation_command(
        self, files_or_modules: list[str] | None = None
    ) -> Cmd | None:
        if self._is_maven():
            return Cmd(
                argv=["mvn", "compile", "-q"],
                cwd=self.project_path,
                timeout_sec=300,
                label="mvn compile",
            )
        return Cmd(
            argv=["gradle", "compileJava", "-q"],
            cwd=self.project_path,
            timeout_sec=300,
            label="gradle compileJava",
        )

    def build(self) -> Cmd | None:
        if self._is_maven():
            return Cmd(
                argv=["mvn", "package", "-q", "-DskipTests"],
                cwd=self.project_path,
                timeout_sec=600,
                label="mvn package",
            )
        return Cmd(
            argv=["gradle", "build", "-q", "-x", "test"],
            cwd=self.project_path,
            timeout_sec=600,
            label="gradle build",
        )

    def test_command(self, feature_filter: str | None = None) -> Cmd | None:
        if self._is_maven():
            argv = ["mvn", "test"]
            if feature_filter:
                # Maven test filter via -Dtest=<pattern>. The pattern
                # matches against the test class name; feature IDs map
                # to substrings of "F001Test", "F001IT", etc.
                argv.append(f"-Dtest=*{feature_filter}*")
            return Cmd(
                argv=argv,
                cwd=self.project_path,
                timeout_sec=600,
                label="mvn test",
            )
        argv = ["gradle", "test"]
        if feature_filter:
            argv += ["--tests", f"*{feature_filter}*"]
        return Cmd(
            argv=argv,
            cwd=self.project_path,
            timeout_sec=600,
            label="gradle test",
        )

    def test_report_globs(self) -> list[str]:
        # Both build tools write JUnit XML with no extra configuration, so
        # Java evidence is a discovery problem, not a wiring one: Surefire
        # writes one file per test class under target/surefire-reports/ and
        # Gradle writes build/test-results/test/. Both are declared because a
        # multi-module or mixed build can produce either, and Failsafe
        # (integration tests) writes its own directory alongside Surefire.
        return [
            "target/surefire-reports/TEST-*.xml",
            "target/failsafe-reports/TEST-*.xml",
            "**/target/surefire-reports/TEST-*.xml",
            "**/target/failsafe-reports/TEST-*.xml",
            "build/test-results/test/TEST-*.xml",
            "**/build/test-results/test/TEST-*.xml",
        ]

    def start_command(self, port: int) -> Cmd | None:
        override = manifest_stack_field(self.manifest, "start_command")
        if override:
            return Cmd(
                argv=shlex.split(override),
                cwd=self.project_path,
                timeout_sec=120,
                label="start (from manifest)",
            )
        # No de-facto Java entrypoint without project-specific config.
        # Spring Boot uses `mvn spring-boot:run` or `gradle bootRun`;
        # plain Java needs the jar path. Require manifest override.
        return None

    def cache_clean(self) -> list[Cmd]:
        if self._is_maven():
            return [
                Cmd(
                    argv=["mvn", "clean", "-q"],
                    cwd=self.project_path,
                    timeout_sec=60,
                    label="mvn clean",
                ),
            ]
        return [
            Cmd(
                argv=["gradle", "clean", "-q"],
                cwd=self.project_path,
                timeout_sec=60,
                label="gradle clean",
            ),
        ]
