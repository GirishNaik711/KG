#!/usr/bin/env python3
"""Load the version_control block from manifest.yaml.

There is no separate ``rapids-config.yaml`` in this framework: all project
configuration lives in ``manifest.yaml`` (under ``.aah/`` — historically
``.rapids/``). This module reads an optional ``version_control:`` block from
there.

Schema (top-level in manifest.yaml):

    version_control:
      enabled: true
      provider: github          # which adapter to use
      github:
        repo: owner/name
        auth: gh-cli            # transport hint; gh CLI by default
      # jira:
      #   base_url: ...; project_key: ...; auth: ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from aah.core.common.io_utils import read_yaml
from aah.core.common.manifest import find_manifest


@dataclass
class VersionControlConfig:
    enabled: bool = False
    provider: str = "github"
    # The provider-specific sub-block (e.g. the contents of github:), passed
    # verbatim to the provider constructor. Keeps config provider-agnostic.
    provider_config: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def repo(self) -> str | None:
        return self.provider_config.get("repo")


def load_vc_config(manifest_path: Path | None = None) -> VersionControlConfig:
    """Read the version_control block from manifest.yaml.

    Never raises on a missing manifest or a missing block — returns an instance
    with ``enabled=False`` so callers can degrade gracefully (the module is
    opt-in). ``manifest_path`` may be given explicitly; otherwise the active
    project's manifest is located via ``common.manifest.find_manifest``.
    """
    if manifest_path is None:
        manifest_path = find_manifest()
    if manifest_path is None or not Path(manifest_path).exists():
        return VersionControlConfig()

    config = read_yaml(Path(manifest_path)) or {}

    block = config.get("version_control") or {}
    provider = block.get("provider", "github")
    provider_config = block.get(provider) or {}

    return VersionControlConfig(
        enabled=bool(block.get("enabled", False)),
        provider=provider,
        provider_config=provider_config,
        raw=block,
    )
