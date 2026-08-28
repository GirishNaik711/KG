"""Identity primitives for cloudless agents.

Three identity flavors:

  - **`CognitoIdentity`** (in `cloudless.runtime.peer`) — M2M client-credentials
    flow. Best for AWS↔GCP A2A peer auth.

  - **`SigV4Identity`** — AWS SigV4 request signing. Use when the peer endpoint
    sits behind an API Gateway / Lambda Function URL with IAM auth.

  - **`OAuth3LOIdentity`** — 3-legged OAuth for end-user-scoped tool calls.
    Implements the authorization-code flow with PKCE. Tokens are stored in a
    pluggable `TokenStore` (in-memory by default; deploy adapters plug in
    Secrets Manager / Secret Manager).

The HITL pattern for OAuth 3LO:
  1. Agent needs to call a tool that requires the end user's authority
  2. `OAuth3LOIdentity.get_token(user_id)` checks the store
  3. If missing, raise `OAuthRequired(authorize_url, state)` — the agent
     yields a `PauseChunk(reason="oauth", pending_action={"url": ...})`
  4. The end user visits authorize_url, completes the consent screen
  5. Callback exchanges code for token; `OAuth3LOIdentity.store_token(user_id, token)`
  6. Agent resumes; second `get_token(user_id)` returns the stored token
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

# --------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------- #


class OAuthRequired(Exception):
    """Raised when an end user must visit an authorize URL.

    The agent should yield a PauseChunk carrying `authorize_url` so the
    UI can redirect, and resume once the callback completes.
    """

    def __init__(self, authorize_url: str, state: str) -> None:
        super().__init__(f"OAuth consent required at {authorize_url}")
        self.authorize_url = authorize_url
        self.state = state


# --------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------- #


@dataclass
class StoredToken:
    """Stored OAuth credentials for one (user, provider) tuple."""
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0
    scopes: tuple[str, ...] = ()


class TokenStore(Protocol):
    def get(self, *, user_id: str, provider: str) -> StoredToken | None: ...
    def put(self, *, user_id: str, provider: str, token: StoredToken) -> None: ...
    def delete(self, *, user_id: str, provider: str) -> None: ...


class InMemoryTokenStore:
    """Process-local store for dev + tests."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], StoredToken] = {}

    def get(self, *, user_id: str, provider: str) -> StoredToken | None:
        # Return stored token regardless of expiry; expiry handling lives in
        # OAuth3LOIdentity (so it can attempt a refresh).
        return self._tokens.get((user_id, provider))

    def put(self, *, user_id: str, provider: str, token: StoredToken) -> None:
        self._tokens[(user_id, provider)] = token

    def delete(self, *, user_id: str, provider: str) -> None:
        self._tokens.pop((user_id, provider), None)


# --------------------------------------------------------------------- #
# OAuth 3LO identity
# --------------------------------------------------------------------- #


@dataclass
class OAuth3LOConfig:
    """Provider-specific OAuth config."""
    provider: str  # "google", "github", "slack", ...
    client_id: str
    client_secret: str
    authorize_endpoint: str
    token_endpoint: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()


class OAuth3LOIdentity:
    """3-legged OAuth for end-user-scoped tool authority.

    Tokens are stored per (user_id, provider) in the configured `TokenStore`.
    """

    def __init__(
        self,
        config: OAuth3LOConfig,
        *,
        token_store: TokenStore | None = None,
    ) -> None:
        self.config = config
        self.token_store = token_store or InMemoryTokenStore()
        # PKCE state held in memory for the duration of the consent flow
        self._pending_pkce: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # User-facing API
    # ------------------------------------------------------------------ #

    async def get_token(self, *, user_id: str) -> str:
        """Return a fresh access token for `user_id`, refreshing if needed.

        Raises:
            OAuthRequired: when no token is stored and consent must be obtained.
        """
        stored = self.token_store.get(user_id=user_id, provider=self.config.provider)
        if stored and stored.access_token:
            if not stored.expires_at or stored.expires_at - 60 > time.time():
                return stored.access_token
            # Try refresh
            if stored.refresh_token:
                new = await self._refresh(stored.refresh_token)
                if new:
                    self.token_store.put(
                        user_id=user_id, provider=self.config.provider, token=new,
                    )
                    return new.access_token
        # Need consent
        authorize_url, state = self.build_authorize_url(user_id=user_id)
        raise OAuthRequired(authorize_url, state)

    def build_authorize_url(self, *, user_id: str) -> tuple[str, str]:
        """Build the OAuth authorize URL + opaque state token for PKCE.

        Returns (url, state). The caller stores state somewhere reachable
        from the callback handler; `handle_callback` validates it.
        """
        from urllib.parse import urlencode
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        self._pending_pkce[state] = verifier

        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # so we get a refresh_token (Google)
            "prompt": "consent",
        }
        url = f"{self.config.authorize_endpoint}?{urlencode(params)}"
        return url, state

    async def handle_callback(
        self, *, user_id: str, code: str, state: str,
    ) -> StoredToken:
        """Exchange `code` for tokens, persist them, return the StoredToken."""
        verifier = self._pending_pkce.pop(state, None)
        if verifier is None:
            raise ValueError(f"unknown OAuth state {state!r}")

        token = await self._exchange_code(code=code, verifier=verifier)
        self.token_store.put(user_id=user_id, provider=self.config.provider, token=token)
        return token

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #

    async def _exchange_code(self, *, code: str, verifier: str) -> StoredToken:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError("httpx is required for OAuth flows") from e

        data = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "code_verifier": verifier,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.config.token_endpoint, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"OAuth token exchange failed: {resp.status_code} {resp.text}")
        payload = resp.json()
        return self._token_from_payload(payload)

    async def _refresh(self, refresh_token: str) -> StoredToken | None:
        try:
            import httpx
        except ImportError:
            return None

        data = {
            "grant_type": "refresh_token",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": refresh_token,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.config.token_endpoint, data=data)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        payload = resp.json()
        # Refresh-token responses sometimes omit the refresh_token; preserve old.
        payload.setdefault("refresh_token", refresh_token)
        return self._token_from_payload(payload)

    @staticmethod
    def _token_from_payload(payload: dict) -> StoredToken:
        expires_in = int(payload.get("expires_in") or 3600)
        scopes = tuple((payload.get("scope") or "").split())
        return StoredToken(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=time.time() + expires_in,
            scopes=scopes,
        )


# --------------------------------------------------------------------- #
# SigV4 identity — alternative to Cognito JWT for A2A peer auth
# --------------------------------------------------------------------- #


class SigV4Identity:
    """AWS SigV4-signs each peer request.

    Use when the peer endpoint is fronted by an API Gateway, Lambda Function
    URL, or AppRunner service with IAM-auth. The peer doesn't need Cognito
    in that topology — every call is authenticated by AWS-native SigV4.

    The "token" returned by `mint_token()` is the rendered SigV4
    Authorization header value (cloudless.runtime.peer prepends "Bearer "
    only when the identity returns a JWT-shaped token; we use a custom
    signing hook for SigV4 — see `sign_request`).
    """

    def __init__(
        self,
        *,
        service: str = "execute-api",
        region: str = "us-east-1",
        session: Any = None,
    ) -> None:
        """
        Args:
            service: AWS service name for SigV4 (default: API Gateway).
                Common values: 'execute-api', 'lambda', 'apprunner'.
            region: AWS region. Required for SigV4 signing.
            session: Optional pre-built boto3.Session. Defaults to default chain.
        """
        self.service = service
        self.region = region
        if session is None:
            import boto3
            session = boto3.Session()
        self._session = session

    async def mint_token(self, *, audience: str) -> tuple[str, int]:
        """For backwards compat with CognitoIdentity's interface.

        SigV4 doesn't have a long-lived token — each request signs itself.
        Callers should use `sign_request()` directly. We return a sentinel
        marker so `A2APeerClient` knows to use SigV4 mode.
        """
        return "__sigv4__", 0  # 0 → never cache

    def sign_request(
        self, *, method: str, url: str, body: bytes,
        headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Return SigV4-signed headers for the given request.

        Caller attaches the returned headers to the HTTP request.
        """
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        creds = self._session.get_credentials()
        if creds is None:
            raise RuntimeError("no AWS credentials available for SigV4")
        request = AWSRequest(method=method, url=url, data=body,
                             headers=dict(headers or {}))
        SigV4Auth(creds, self.service, self.region).add_auth(request)
        return dict(request.headers.items())
