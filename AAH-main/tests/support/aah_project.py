"""Fluent builder for real AAH test projects.

The builder owns only project setup. Passing verification evidence must still
be produced through the domain writer under test; no generic attestation
shortcut is exposed here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import yaml

from aah.core.build.regenerate_attestation_secret import ensure_attestation_secret
from aah.core.common.governance import configure_governance
from aah.core.common.io_utils import write_json, write_yaml


REPO_ROOT = Path(__file__).resolve().parents[2]

class AAHProjectBuilder:
    """Build a real Git-backed project and return its root via ``.path``."""
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
    @property
    def aah(self) -> Path:
        return self.path / ".aah"
    @classmethod
    def create(
        cls,
        parent: Path,
        *,
        name: str = "project",
        branch: str = "develop",
        git: bool = True,
    ) -> "AAHProjectBuilder":
        builder = cls(Path(parent) / name)
        builder.path.mkdir(parents=True)
        builder.dirs("plan/features", "build", "audit")
        if git:
            builder.git("init", "-b", branch)
            builder.git("config", "user.name", "Test")
            builder.git("config", "user.email", "test@example.com")
        return builder
    def dirs(self, *relative_paths: str) -> "AAHProjectBuilder":
        for relative_path in relative_paths:
            (self.aah / relative_path).mkdir(parents=True, exist_ok=True)
        return self
    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    def manifest(
        self,
        *,
        project_name: str = "test-project",
        current_phase: str = "build",
        features: dict[str, Any] | None = None,
        stack_choices: dict[str, Any] | None = None,
        **values: Any,
    ) -> "AAHProjectBuilder":
        payload: dict[str, Any] = {
            "project_name": project_name,
            "current_phase": current_phase,
            **values,
        }
        if features is not None:
            payload["features"] = features
        if stack_choices is not None:
            payload["stack_choices"] = stack_choices
        write_yaml(payload, self.aah / "manifest.yaml")
        return self
    def secret(self) -> "AAHProjectBuilder":
        ensure_attestation_secret(self.path)
        return self
    def governance(self, **owners: str) -> "AAHProjectBuilder":
        configure_governance(self.path, owners)
        return self
    def waves(
        self, entries: list, *, include_total: bool = True, **metadata: Any
    ) -> "AAHProjectBuilder":
        if include_total:
            metadata.setdefault("total_waves", len(entries))
        write_json(
            {"waves": entries, **metadata},
            self.aah / "plan" / "waves.json",
        )
        return self
    def feature(
        self,
        feature_id: str,
        *,
        frontmatter: dict[str, Any] | None = None,
        body: str | None = None,
    ) -> "AAHProjectBuilder":
        values = {
            "id": feature_id,
            "description": f"feature {feature_id}",
            **(frontmatter or {}),
        }
        content = (
            "---\n"
            + yaml.safe_dump(values, sort_keys=False)
            + "---\n\n"
            + (body if body is not None else f"# {feature_id}\n")
        )
        target = self.aah / "plan" / "features" / f"{feature_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self
    def file(self, relative_path: str, content: str) -> "AAHProjectBuilder":
        target = self.path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self
    def commit(
        self,
        message: str = "seed",
        paths: Iterable[str] = ("-A",),
    ) -> "AAHProjectBuilder":
        self.git("add", *paths)
        self.git("commit", "-m", message)
        return self
    def cli_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if value
        )
        return env
    def run_module(
        self,
        module: str,
        *args: str,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=REPO_ROOT,
            env=self.cli_env(),
            capture_output=True,
            text=True,
            check=check,
        )
