"""AWS Secrets Manager backend for cloudless.Secrets."""
from __future__ import annotations

import boto3
from botocore.exceptions import ClientError


class SecretsManagerBackend:
    """Reads secrets from AWS Secrets Manager.

    Args:
        prefix: Optional prefix applied to every secret name.
            `Secrets(prefix="cloudless/").get("foo")` calls
            `GetSecretValue(SecretId="cloudless/foo")`.
        region: AWS region.

    Secret values can be either plain strings OR JSON objects. JSON
    secrets are returned verbatim as the JSON string; callers can
    json.loads() if they want structured data. (Following AWS convention.)
    """

    def __init__(self, *, prefix: str = "", region: str = "us-east-1") -> None:
        self.prefix = prefix
        self._client = boto3.client("secretsmanager", region_name=region)

    async def get(self, name: str) -> str | None:
        try:
            resp = self._client.get_secret_value(SecretId=self.prefix + name)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ResourceNotFoundException",):
                return None
            raise self._translate(e) from e
        # SecretString is set for string secrets; SecretBinary for binary.
        return resp.get("SecretString")

    async def list_names(self) -> list[str]:
        names: list[str] = []
        paginator = self._client.get_paginator("list_secrets")
        for page in paginator.paginate(
            Filters=[{"Key": "name", "Values": [self.prefix]}] if self.prefix else [],
            MaxResults=100,
        ):
            for s in page.get("SecretList", []):
                full = s.get("Name", "")
                if self.prefix and full.startswith(self.prefix):
                    names.append(full[len(self.prefix):])
                else:
                    names.append(full)
        return sorted(names)

    @staticmethod
    def _translate(e: ClientError) -> Exception:
        from cloudless.exceptions import (
            AuthenticationError,
            InvalidInputError,
            ThrottledError,
        )
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException",):
            return ThrottledError(str(e))
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return AuthenticationError(str(e))
        if code in ("InvalidParameterException", "InvalidRequestException"):
            return InvalidInputError(str(e))
        return e
