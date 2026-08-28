"""cloudless.runtime.peer — A2A peer-call SDK (Q12).

Provides `A2APeerClient` — a concrete `Context.peer(name).call(prompt)`
implementation that:
  1. Looks up the peer in the baked manifest
  2. Mints a Cognito M2M JWT for the peer's audience
  3. Issues a JSON-RPC `message/send` per A2A v0.3
  4. Translates HTTP errors to cloudless.PeerUnreachable / AuthenticationError
  5. Propagates cost attribution via X-Cloudless-Attribution-* headers (Q20)

`build_peer_client(name, manifest, *, cost=None, identity=None)` is the
factory used by AgentCore/Vertex contexts.

Note: this module imports `httpx` lazily so cloudless core has no hard
dependency on it.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from cloudless.exceptions import (
    AuthenticationError,
    InvalidInputError,
    PeerUnreachable,
)
from cloudless.exceptions import (
    TimeoutError as CloudlessTimeoutError,
)
from cloudless.runtime.manifest import Manifest, PeerEntry


class A2APeerClient:
    """JSON-RPC 2.0 over HTTPS A2A v0.3 client for one peer.

    Reusable across calls in the same invocation. The Cognito token is
    cached until ~60s before expiry.
    """

    def __init__(
        self,
        entry: PeerEntry,
        *,
        identity: CognitoIdentity | None = None,
        cost_tracker: Any = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.entry = entry
        self._identity = identity
        self._cost_tracker = cost_tracker
        self._timeout = timeout_seconds
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

    async def call(self, prompt: str, **kwargs: Any) -> Any:
        from cloudless.runtime import tracing
        with tracing.span(f"peer.{self.entry.name}", **{
            "peer.name": self.entry.name,
            "peer.cloud": self.entry.cloud,
        }):
            try:
                return await self._call_impl(prompt, **kwargs)
            except Exception as e:
                tracing.record_exception(e)
                raise

    async def _call_impl(self, prompt: str, **kwargs: Any) -> Any:
        if not self.entry.a2a_url:
            raise InvalidInputError(
                f"peer {self.entry.name!r} has no a2a_url in manifest"
            )
        if not self.entry.audience:
            raise InvalidInputError(
                f"peer {self.entry.name!r} has no audience in manifest"
            )

        # Detect SigV4 mode — `SigV4Identity.mint_token` returns the
        # sentinel "__sigv4__"; in that case we sign per-request instead
        # of attaching a Bearer header.
        token = await self._get_token()
        use_sigv4 = (token == "__sigv4__")

        request = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "user",
                    "parts": [{"kind": "text", "text": prompt}],
                },
                **kwargs.get("params", {}),
            },
        }

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if not use_sigv4:
            headers["Authorization"] = f"Bearer {token}"
        if self._cost_tracker is not None:
            try:
                attr_headers = self._cost_tracker.attribution_headers()
                headers.update(attr_headers)
            except AttributeError:
                pass

        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("httpx is required for A2A peer calls") from e

        # If SigV4 mode, sign the request and merge signed headers.
        # `_identity` may be a CognitoIdentity (no sign_request) OR a
        # SigV4Identity (has sign_request); duck-type to keep the call site
        # free of cross-module identity-class plumbing.
        if use_sigv4 and self._identity is not None:
            import json as _json
            body_bytes = _json.dumps(request).encode()
            sign_request = getattr(self._identity, "sign_request", None)
            if sign_request is None:
                raise AuthenticationError(
                    "SigV4 mode requires an identity with .sign_request(); "
                    f"got {type(self._identity).__name__}"
                )
            try:
                signed = sign_request(
                    method="POST", url=self.entry.a2a_url,
                    body=body_bytes, headers=headers,
                )
                headers.update(signed)
            except Exception as e:
                raise AuthenticationError(f"SigV4 signing failed: {e}") from e

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self.entry.a2a_url, json=request, headers=headers)
        except httpx.ConnectError as e:
            raise PeerUnreachable(f"could not connect to {self.entry.name!r}: {e}") from e
        except httpx.ReadTimeout as e:
            raise CloudlessTimeoutError(f"peer {self.entry.name!r} timed out") from e

        if resp.status_code in (401, 403):
            raise AuthenticationError(
                f"peer {self.entry.name!r} rejected token ({resp.status_code})"
            )
        if resp.status_code == 429:
            raise PeerUnreachable(f"peer {self.entry.name!r} throttled (429)")
        if resp.status_code >= 500:
            raise PeerUnreachable(
                f"peer {self.entry.name!r} returned {resp.status_code}"
            )
        if resp.status_code >= 400:
            raise InvalidInputError(
                f"peer {self.entry.name!r} returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise PeerUnreachable(f"peer returned non-JSON body: {e}") from e

        if "error" in payload:
            err = payload["error"]
            raise InvalidInputError(
                f"peer {self.entry.name!r} error {err.get('code')}: {err.get('message')}"
            )

        # Record peer call for cost telemetry; downstream peer may have
        # returned its own usd cost in metadata.
        if self._cost_tracker is not None and hasattr(self._cost_tracker, "record_peer_call"):
            result = payload.get("result", {})
            meta = result.get("metadata", {}) if isinstance(result, dict) else {}
            usd = float(meta.get("usd_cost", 0.0) or 0.0)
            self._cost_tracker.record_peer_call(peer=self.entry.name, usd=usd)

        return payload.get("result")

    # ------------------------------------------------------------------ #
    # Cognito M2M token caching
    # ------------------------------------------------------------------ #

    async def _get_token(self) -> str:
        if self._identity is None:
            raise AuthenticationError(
                "no identity configured; cannot authenticate to peer"
            )
        now = time.time()
        # expires_in == 0 → never cache (SigV4 signs per-request)
        if self._cached_token and self._token_expires_at and self._token_expires_at - 60 > now:
            return self._cached_token
        token, expires_in = await self._identity.mint_token(
            audience=self.entry.audience or self.entry.name,
        )
        if expires_in > 0:
            self._cached_token = token
            self._token_expires_at = now + expires_in
        return token


class CognitoIdentity:
    """Mints M2M tokens against a Cognito user pool / app client.

    Lazy: only attempts to import boto3 when `mint_token` is called.
    """

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> None:
        self.domain = domain
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope

    async def mint_token(self, *, audience: str) -> tuple[str, int]:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("httpx is required for Cognito token minting") from e

        token_url = f"https://{self.domain}/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            data["scope"] = self.scope

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise AuthenticationError(
                f"Cognito token endpoint returned {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        return payload["access_token"], int(payload.get("expires_in", 3600))


def build_peer_client(
    name: str,
    manifest: Manifest,
    *,
    identity: CognitoIdentity | None = None,
    cost_tracker: Any = None,
    timeout_seconds: float = 30.0,
) -> A2APeerClient:
    """Factory: look up `name` in `manifest` and build a peer client."""
    entry = manifest.get(name)
    if entry is None:
        raise InvalidInputError(
            f"unknown peer {name!r}; manifest has: {sorted(manifest.agents)}"
        )
    return A2APeerClient(
        entry,
        identity=identity,
        cost_tracker=cost_tracker,
        timeout_seconds=timeout_seconds,
    )
