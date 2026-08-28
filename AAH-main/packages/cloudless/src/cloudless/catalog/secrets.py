"""cloudless.Secrets — unified secrets primitive over Secrets Manager / Secret Manager.

Simple key/value secret retrieval with namespacing. Q9 v1-catalog primitive.

Backends (M1):
  - LocalFileBackend  — reads .cloudless/dev-secrets.yaml (for cloudless dev)
  - SecretsManagerBackend  — AWS Secrets Manager (production)
  - SecretManagerBackend (M2) — GCP Secret Manager

The default is LocalFileBackend so unit tests + `cloudless dev` work
without cloud credentials.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SecretsBackend(Protocol):
    async def get(self, name: str) -> str | None: ...
    async def list_names(self) -> list[str]: ...


# --------------------------------------------------------------------- #
# Local-file backend — default for tests + `cloudless dev`
# --------------------------------------------------------------------- #


class LocalFileBackend:
    """Read secrets from a YAML file (default: .cloudless/dev-secrets.yaml).

    Per Q13 (local dev) + Q24 (project layout): the convention is that
    `cloudless dev` reads dev-only secret values from a gitignored file
    in the project root.

    Example file:
        bedrock_region: us-east-1
        openai_api_key: sk-...
        slack_signing_secret: ...
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (Path.cwd() / ".cloudless" / "dev-secrets.yaml")
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self._path.is_file():
            self._cache = {}
            return self._cache
        import yaml
        with self._path.open() as f:
            data = yaml.safe_load(f) or {}
        # Coerce all values to strings — Secrets Manager strings only
        self._cache = {k: str(v) for k, v in data.items() if isinstance(k, str)}
        return self._cache

    async def get(self, name: str) -> str | None:
        return self._load().get(name)

    async def list_names(self) -> list[str]:
        return sorted(self._load().keys())

    # Test/dev helper — set a secret in memory (NOT persisted)
    def set_in_memory(self, name: str, value: str) -> None:
        self._load()  # ensure cache
        assert self._cache is not None
        self._cache[name] = value


# --------------------------------------------------------------------- #
# User-facing Secrets class
# --------------------------------------------------------------------- #


class Secrets:
    """Unified secrets primitive (Q9).

    Example:
        secrets = cloudless.Secrets()                  # local file (dev)
        secrets = cloudless.Secrets(backend="aws", prefix="cloudless/")

        api_key = await secrets.get("openai_api_key")
    """

    def __init__(
        self,
        *,
        backend: str | SecretsBackend = "local",
        prefix: str = "",
        region: str = "us-east-1",
        path: Path | None = None,
        project: str | None = None,
    ) -> None:
        if isinstance(backend, str):
            self._backend = self._build_backend(
                backend, prefix=prefix, region=region, path=path, project=project,
            )
        else:
            self._backend = backend
        self._prefix = prefix

    @staticmethod
    def _build_backend(
        name: str, *, prefix: str, region: str, path: Path | None,
        project: str | None = None,
    ) -> SecretsBackend:
        if name == "local":
            return LocalFileBackend(path=path)
        if name == "aws":
            from cloudless.adapters.aws.secrets import SecretsManagerBackend
            return SecretsManagerBackend(prefix=prefix, region=region)
        if name == "gcp":
            if not project:
                raise ValueError("backend='gcp' requires project")
            from cloudless.adapters.gcp.secrets import SecretManagerBackend
            return SecretManagerBackend(project=project, prefix=prefix)
        raise ValueError(f"Unknown secrets backend: {name!r}")

    async def get(self, name: str) -> str | None:
        return await self._backend.get(name)

    async def list_names(self) -> list[str]:
        return await self._backend.list_names()
