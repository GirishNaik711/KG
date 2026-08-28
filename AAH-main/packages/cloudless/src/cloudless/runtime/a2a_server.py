"""Inbound A2A server (Q12 receive-side).

Thin Starlette app that exposes a JSON-RPC 2.0 endpoint per A2A v0.3 spec.
The deployed runtime mounts this on `/a2a` (or any user-chosen path) and
translates inbound `message/send` calls into `agent.query(ctx, prompt)`.

Why a cloudless-owned wrapper instead of delegating to the framework's
A2A server (Strands ships one):
  - Uniform behavior across frameworks (LangGraph has no A2A server)
  - Consistent error mapping (cloudless exception hierarchy → JSON-RPC errors)
  - Consistent attribution-header ingestion (Q20)
  - Consistent audit emission (Q19)

This module does NOT validate the inbound JWT — that's the deploy
adapter's job (AgentCore wraps the runtime in a JWT-validating layer
before requests reach our handler). For local dev, validation is skipped.

Spec (A2A v0.3):
  POST /a2a
  Content-Type: application/json
  Body: {
    "jsonrpc": "2.0",
    "id": <id>,
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "...",
        "role": "user",
        "parts": [{"kind": "text", "text": "<prompt>"}, ...]
      }
    }
  }

  200 OK
  Body: {
    "jsonrpc": "2.0",
    "id": <id>,
    "result": {
      "message": {"role": "assistant", "parts": [...]},
      "metadata": {"usd_cost": 0.0042, "chunks": [...]}
    }
  }
"""
from __future__ import annotations

import json
from typing import Any

JSONRPC_ERRORS = {
    -32700: "Parse error",
    -32600: "Invalid Request",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
}


def build_a2a_app(
    agent_factory,
    *,
    path: str = "/a2a",
    require_audience: str | None = None,
    agent_card: dict | None = None,
    agent_card_path: str = "/.well-known/agent.json",
):
    """Construct a Starlette app mounting POST `path` as A2A receiver.

    Args:
        agent_factory: Callable returning a `cloudless.Agent` instance per request.
            The factory pattern lets each call get a fresh agent (or pull from a pool).
        path: URL path for the JSON-RPC endpoint. Default "/a2a".
        require_audience: If set, requests must include `X-A2A-Audience` matching this
            value. Light defensive check for local dev (real audience validation is JWT-side).
        agent_card: A2A v0.3 agent card dict. If None, auto-derived from the agent's
            metadata. Served at `/.well-known/agent.json`.
        agent_card_path: Override the agent-card URL path (default per spec).
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def handle(request: Request) -> JSONResponse:
        # Parse JSON-RPC envelope
        try:
            payload = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700)

        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _jsonrpc_error(payload.get("id") if isinstance(payload, dict) else None, -32600)

        msg_id = payload.get("id")
        method = payload.get("method")
        if method != "message/send":
            return _jsonrpc_error(msg_id, -32601, f"unknown method: {method!r}")

        params = payload.get("params") or {}
        message = params.get("message") or {}
        parts = message.get("parts") or []
        text_parts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
        prompt = "".join(text_parts)
        if not prompt:
            return _jsonrpc_error(msg_id, -32602, "no text part in message")

        # Audience check (light defensive guard for local dev)
        if require_audience is not None:
            audience = request.headers.get("x-a2a-audience")
            if audience != require_audience:
                return _jsonrpc_error(msg_id, -32600,
                                       f"audience mismatch (got {audience!r})")

        # Build context + ingest attribution headers
        import cloudless
        ctx = cloudless.InMemoryContext()
        attr_headers = {
            k: v for k, v in request.headers.items()
            if k.lower().startswith("x-cloudless-attribution-")
        }
        if attr_headers:
            ctx.cost.ingest_attribution_headers(attr_headers)

        try:
            agent = agent_factory()
            response_parts: list[dict] = []
            chunks_meta: list[dict] = []
            async for chunk in agent.query(ctx, prompt):
                chunk_dump = chunk.model_dump()
                chunks_meta.append(chunk_dump)
                if chunk.kind == "text":
                    response_parts.append({"kind": "text", "text": chunk_dump["text"]})
        except cloudless.PolicyViolation as e:
            return _jsonrpc_error(msg_id, -32603, f"policy violation: {e}")
        except cloudless.GuardrailBlocked as e:
            return _jsonrpc_error(msg_id, -32603, f"guardrail blocked: {e}")
        except cloudless.AuthenticationError as e:
            return _jsonrpc_error(msg_id, -32600, str(e))
        except cloudless.InvalidInputError as e:
            return _jsonrpc_error(msg_id, -32602, str(e))
        except Exception as e:
            return _jsonrpc_error(msg_id, -32603, f"internal error: {e}")

        usd_total = await ctx.cost.session_total_usd()
        result = {
            "message": {
                "role": "assistant",
                "parts": response_parts,
            },
            "metadata": {
                "usd_cost": usd_total,
                "chunks": chunks_meta,
            },
        }
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})

    # ----- agent card publication (A2A v0.3 §3.2) ----- #
    def _derive_agent_card() -> dict:
        """Derive an agent card from a sample factory instance."""
        try:
            instance = agent_factory()
            meta = getattr(instance, "__cloudless_metadata__", None)
            if meta is None:
                meta = getattr(type(instance), "__cloudless_metadata__", None)
            if meta is None:
                return {"name": "unknown", "description": "", "capabilities": []}
            return {
                "name": meta.name,
                "description": meta.description or "",
                "version": meta.version,
                "url": path,
                "protocolVersion": "0.3.0",
                "capabilities": {
                    "streaming": False,
                    "pushNotifications": False,
                    "stateTransitionHistory": False,
                },
                "skills": [
                    {
                        "id": meta.name,
                        "name": meta.name,
                        "description": meta.description or meta.name,
                        "tags": list(meta.tags) if meta.tags else [],
                    },
                ],
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
            }
        except Exception:
            return {"name": "unknown", "description": "", "capabilities": []}

    resolved_card = agent_card or _derive_agent_card()

    async def serve_card(request: Request) -> JSONResponse:
        return JSONResponse(resolved_card)

    # ----- SSE streaming endpoint (A2A v0.3 `message/stream`) ----- #
    async def handle_stream(request: Request):
        from starlette.responses import StreamingResponse
        try:
            payload = await request.json()
        except Exception:
            return _jsonrpc_error(None, -32700)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return _jsonrpc_error(payload.get("id") if isinstance(payload, dict) else None, -32600)
        msg_id = payload.get("id")
        method = payload.get("method")
        if method != "message/stream":
            return _jsonrpc_error(msg_id, -32601, f"unknown method: {method!r}")
        params = payload.get("params") or {}
        message = params.get("message") or {}
        parts = message.get("parts") or []
        text_parts = [p.get("text", "") for p in parts if p.get("kind") == "text"]
        prompt = "".join(text_parts)
        if not prompt:
            return _jsonrpc_error(msg_id, -32602, "no text part in message")

        import cloudless as _cl
        ctx = _cl.InMemoryContext()
        attr = {k: v for k, v in request.headers.items()
                if k.lower().startswith("x-cloudless-attribution-")}
        if attr:
            ctx.cost.ingest_attribution_headers(attr)

        async def _events():
            try:
                agent = agent_factory()
                async for chunk in agent.query(ctx, prompt):
                    payload = {
                        "jsonrpc": "2.0", "id": msg_id,
                        "result": {"chunk": chunk.model_dump(), "done": False},
                    }
                    yield f"data: {json.dumps(payload)}\n\n".encode()
            except Exception as e:
                err = {
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": str(e)},
                }
                yield f"data: {json.dumps(err)}\n\n".encode()
            done = {"jsonrpc": "2.0", "id": msg_id, "result": {"done": True}}
            yield f"data: {json.dumps(done)}\n\n".encode()

        return StreamingResponse(_events(), media_type="text/event-stream",  # type: ignore[no-untyped-call]
                                  headers={"Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})

    return Starlette(routes=[
        Route(path, handle, methods=["POST"]),
        Route(path + "/stream", handle_stream, methods=["POST"]),
        Route(agent_card_path, serve_card, methods=["GET"]),
    ])


def _jsonrpc_error(req_id: Any, code: int, message: str | None = None) -> Any:
    """Build a JSON-RPC error response."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message or JSONRPC_ERRORS.get(code, "Error"),
        },
    })
