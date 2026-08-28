"""Per-invocation runtime context — the `ctx` passed to `agent.query(ctx, prompt)`.

`Context` is a Protocol (structural typing). The default in-memory
implementation (`InMemoryContext`) is what `cloudless dev` uses. Cloud
deployments inject a fuller implementation that wires:
  - Session ID to AgentCore microVM session / GCP Agent Runtime session
  - User identity from inbound JWT / SigV4 caller
  - CostTracker to OTel cost spans
  - PeerClient to A2A peer-routing logic

Per Q12: `ctx.peer(name).call(...)` looks up `name` in the baked manifest
and dispatches a Cognito-authenticated A2A call. The PeerClient is left
abstract here; the concrete implementation lives in the AWS / GCP adapter
modules (different transport per cloud).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class User(Protocol):
    """The auth principal that initiated this invocation.

    Populated from the inbound JWT/SigV4 claims by the embedded runtime
    lib. Optional fields are None for anonymous / SigV4-IAM callers.
    """

    @property
    def id(self) -> str: ...
    """Stable principal ID. JWT `sub` claim or IAM principal ARN."""

    @property
    def email(self) -> str | None: ...
    """User email if available (from JWT). None for M2M / IAM principals."""

    @property
    def team(self) -> str | None: ...
    """Team tag for cost attribution (Q20)."""


class CostTracker(Protocol):
    """Per-invocation cost accumulator (Q20).

    Tracks tokens / vCPU-seconds / GB-seconds / sandbox runtime / tool
    invocations as they accumulate. Emits OTel span attributes and is
    read by `@cloudless.policy stage="before_*"` cost-cap policies.
    """

    async def session_total_usd(self) -> float:
        """Sum-to-date for this session in USD."""
        ...

    def attribute(self, *, team: str | None = None,
                  project: str | None = None,
                  feature: str | None = None) -> None:
        """Tag the current session for cost-attribution rollups.

        Tags propagate via the A2A `originating-attribution` header
        when this agent calls a peer (so finance gets accurate cross-
        agent rollups — Q20).
        """
        ...

    def record_llm_call(self, *, model: str, input_tokens: int,
                        output_tokens: int, cached_tokens: int = 0,
                        reasoning_tokens: int = 0) -> None:
        """Bookkeeping for a single LLM invocation."""
        ...

    def attribution_headers(self) -> dict[str, str]:
        """HTTP headers a peer call should attach for cost attribution."""
        ...

    def ingest_attribution_headers(self, headers: dict[str, str]) -> None:
        """Merge attribution from an inbound peer's headers."""
        ...

    def record_peer_call(self, *, peer: str, usd: float = 0.0) -> None:
        """Record a peer call and (optionally) the peer's reported cost."""
        ...


class PeerClient(Protocol):
    """Per-peer-name client for A2A calls (Q12).

    Resolved from the baked manifest. `call()` mints a Cognito JWT for
    the peer's audience and issues a JSON-RPC `message/send`.
    """

    async def call(self, prompt: str, **kwargs: Any) -> Any:
        """Issue an A2A `message/send` to this peer; return the result."""
        ...


@runtime_checkable
class Session(Protocol):
    """Stable session identifier across one user-agent conversation.

    Maps to AgentCore's `runtimeSessionId` (which is anchored to a
    Firecracker microVM) on AWS, or to a custom session ID on GCP. The
    framework adapter wires this through to the underlying framework's
    session/thread concept (LangGraph thread_id, Strands session_id,
    ADK session_service).
    """

    @property
    def id(self) -> str:
        """The session UUID."""
        ...


@runtime_checkable
class Context(Protocol):
    """Per-invocation context, injected by the embedded runtime.

    Use:
        async def query(self, ctx: cloudless.Context, prompt: str):
            if await ctx.cost.session_total_usd() > 5:
                yield cloudless.ErrorChunk(error="cost_cap_exceeded")
                return
            response = await ctx.peer("orders").call(task)
            yield cloudless.TextChunk(text=str(response))
    """

    @property
    def session(self) -> Session: ...
    @property
    def user(self) -> User | None: ...
    @property
    def cost(self) -> CostTracker: ...

    def peer(self, name: str) -> PeerClient:
        """Look up a peer by manifest name; return a client for that peer."""
        ...


# --------------------------------------------------------------------- #
# In-memory implementation — used by unit tests + `cloudless dev`.
# Cloud deployments substitute real implementations that wire to
# AgentCore Memory / Cognito / observability sinks.
# --------------------------------------------------------------------- #


class _InMemorySession:
    def __init__(self, session_id: str) -> None:
        self._id = session_id

    @property
    def id(self) -> str:
        return self._id


class _InMemoryUser:
    def __init__(self, user_id: str, email: str | None = None,
                 team: str | None = None) -> None:
        self._id = user_id
        self._email = email
        self._team = team

    @property
    def id(self) -> str:
        return self._id

    @property
    def email(self) -> str | None:
        return self._email

    @property
    def team(self) -> str | None:
        return self._team


class _InMemoryCostTracker:
    """CostTracker for tests + `cloudless dev`.

    Tracks attribution + LLM-call bookkeeping in-memory and computes
    `session_total_usd` using the cloudless pricing table.
    """

    def __init__(self) -> None:
        self.attribution: dict[str, str] = {}
        self.llm_calls: list[dict[str, Any]] = []
        self.peer_calls: list[dict[str, Any]] = []
        # Costs reported as already-USD by peers (via A2A header)
        self.imported_peer_usd: float = 0.0

    async def session_total_usd(self) -> float:
        from cloudless.runtime.pricing import estimate_cost_usd
        own = sum(
            estimate_cost_usd(
                call["model"],
                input_tokens=call["input_tokens"],
                output_tokens=call["output_tokens"],
                cached_tokens=call.get("cached_tokens", 0),
                reasoning_tokens=call.get("reasoning_tokens", 0),
            )
            for call in self.llm_calls
        )
        return own + self.imported_peer_usd

    def attribute(self, *, team: str | None = None,
                  project: str | None = None,
                  feature: str | None = None) -> None:
        if team is not None:
            self.attribution["team"] = team
        if project is not None:
            self.attribution["project"] = project
        if feature is not None:
            self.attribution["feature"] = feature

    def record_llm_call(self, *, model: str, input_tokens: int,
                        output_tokens: int, cached_tokens: int = 0,
                        reasoning_tokens: int = 0) -> None:
        self.llm_calls.append({
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
        })
        from cloudless.runtime.cost_sinks import emit_cost
        from cloudless.runtime.pricing import estimate_cost_usd
        emit_cost(
            kind="llm",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            usd=estimate_cost_usd(
                model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
            team=self.attribution.get("team"),
            project=self.attribution.get("project"),
            feature=self.attribution.get("feature"),
        )

    # ----------- A2A attribution propagation (Q20) ----------------- #

    def attribution_headers(self) -> dict[str, str]:
        """HTTP headers a peer should attach when calling another agent.

        Receiving runtimes parse these into their own cost trackers via
        `ingest_attribution_headers` so finance rollups stay consistent
        across agent hops.
        """
        out: dict[str, str] = {}
        for k, v in self.attribution.items():
            out[f"X-Cloudless-Attribution-{k.capitalize()}"] = v
        return out

    def ingest_attribution_headers(self, headers: dict[str, str]) -> None:
        """Merge attribution from an inbound peer's headers."""
        for key, value in headers.items():
            lk = key.lower()
            if lk.startswith("x-cloudless-attribution-"):
                tag = lk[len("x-cloudless-attribution-"):]
                self.attribution.setdefault(tag, value)

    def record_peer_call(self, *, peer: str, usd: float = 0.0) -> None:
        """Record a peer call and (optionally) the peer's reported cost."""
        self.peer_calls.append({"peer": peer, "usd": usd})
        self.imported_peer_usd += usd


class _InMemoryPeerClient:
    """No-op peer client for `cloudless dev` when peers are stubbed.

    Replaced by the local-subprocess peer dispatcher when `cloudless dev`
    is running multi-agent topologies (per Q13).
    """

    def __init__(self, name: str, *, response: Any = None) -> None:
        self._name = name
        self._response = response if response is not None else f"<stub peer {name}>"
        self.calls: list[dict[str, Any]] = []

    async def call(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append({"prompt": prompt, **kwargs})
        return self._response


class InMemoryContext:
    """Concrete in-memory Context used by tests and `cloudless dev`."""

    def __init__(
        self,
        session_id: str = "test-session",
        user: User | None = None,
        peer_responses: dict[str, Any] | None = None,
    ) -> None:
        self._session = _InMemorySession(session_id)
        self._user = user
        self._cost = _InMemoryCostTracker()
        self._peer_responses = peer_responses or {}
        self._peers: dict[str, _InMemoryPeerClient] = {}

    @property
    def session(self) -> Session:
        return self._session

    @property
    def user(self) -> User | None:
        return self._user

    @property
    def cost(self) -> CostTracker:
        return self._cost

    def peer(self, name: str) -> PeerClient:
        if name not in self._peers:
            self._peers[name] = _InMemoryPeerClient(
                name, response=self._peer_responses.get(name)
            )
        return self._peers[name]
