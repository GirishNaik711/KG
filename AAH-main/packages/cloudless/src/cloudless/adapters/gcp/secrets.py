"""GCP Secret Manager backend for cloudless.Secrets."""
from __future__ import annotations

from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.cloud import secretmanager_v1


class SecretManagerBackend:
    """Reads secrets from GCP Secret Manager.

    Always reads the 'latest' version. Args:
        project: GCP project ID (required).
        prefix: Optional prefix applied to every secret name lookup.
            `Secrets(prefix="cloudless-").get("foo")` reads secret
            "cloudless-foo" from Secret Manager.
    """

    def __init__(self, *, project: str, prefix: str = "") -> None:
        self.project = project
        self.prefix = prefix
        self._client = secretmanager_v1.SecretManagerServiceClient()

    async def get(self, name: str) -> str | None:
        full = f"projects/{self.project}/secrets/{self.prefix}{name}/versions/latest"
        try:
            resp = self._client.access_secret_version(name=full)
        except NotFound:
            return None
        except GoogleAPICallError as e:
            raise self._translate(e) from e
        return resp.payload.data.decode("utf-8")

    async def list_names(self) -> list[str]:
        names: list[str] = []
        parent = f"projects/{self.project}"
        try:
            for s in self._client.list_secrets(parent=parent):
                short = s.name.rsplit("/", 1)[-1]
                if self.prefix and short.startswith(self.prefix):
                    names.append(short[len(self.prefix):])
                elif not self.prefix:
                    names.append(short)
        except GoogleAPICallError as e:
            raise self._translate(e) from e
        return sorted(names)

    @staticmethod
    def _translate(e: GoogleAPICallError) -> Exception:
        from cloudless.exceptions import (
            AuthenticationError,
            InvalidInputError,
            ThrottledError,
        )
        status = getattr(e, "grpc_status_code", None) or getattr(e, "code", lambda: 0)()
        if status == 429:
            return ThrottledError(str(e))
        if status in (401, 403):
            return AuthenticationError(str(e))
        if status in (400, 404):
            return InvalidInputError(str(e))
        return e
