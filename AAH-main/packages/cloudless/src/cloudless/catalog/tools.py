"""cloudless.Tool — multi-source tool factory (Q15).

Q15 commits to a factory pattern with multiple `from_*()` constructors so
users can pull tools from any source:

  - `Tool.from_function(fn)` — wraps a local Python function
  - `Tool.from_aws_lambda(arn)` — invokes an existing AWS Lambda
  - `Tool.from_openapi(spec_url)` — calls an OpenAPI-described HTTP endpoint
  - `Tool.from_mcp_server(url, auth)` — proxies to an external MCP server

All tools normalize to a common Tool interface with:
  - `.name: str` — stable identifier (the function/operation/path name)
  - `.description: str` — human-readable purpose
  - `.parameters: dict` — JSON-Schema for inputs
  - `.invoke(args: dict) -> Any` — async invocation

The Q15 decision is "normalize to MCP under the hood." We don't materialize
an MCP server in cloudless.Tool by default — that's an AgentCore Gateway
deploy-time concern (M2+). For M1.5 we ship local + AWS Lambda invocation.

@cloudless.tool is the syntactic-sugar decorator wrapping `Tool.from_function`.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Tool:
    """A unified callable tool the agent can invoke."""

    name: str
    description: str
    parameters: dict
    """JSON Schema describing the args dict expected by `invoke`."""

    _invoker: Callable[[dict], Any]
    """The actual function called by `invoke`. May be sync or async."""

    metadata: dict = field(default_factory=dict)

    async def invoke(self, args: dict) -> Any:
        from cloudless.runtime import tracing
        from cloudless.runtime.policy import get_registry
        reg = get_registry()
        args = reg.run("before_tool", tool_name=self.name, args=dict(args))["args"]
        with tracing.span(f"tool.{self.name}", **{"tool.name": self.name}):
            try:
                result = self._invoker(args)
                if inspect.iscoroutine(result):
                    result = await result
            except Exception as e:
                tracing.record_exception(e)
                raise
        return reg.run("after_tool", tool_name=self.name, args=args, result=result)["result"]

    # ------------------------------------------------------------------ #
    # Factories (Q15 multi-source)
    # ------------------------------------------------------------------ #

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Tool:
        """Wrap a Python function as a Tool.

        The function's signature (param names + type hints) is introspected
        to produce a JSON-Schema for `parameters`.
        """
        sig = inspect.signature(fn)
        # `from __future__ import annotations` makes annotations strings;
        # get_type_hints resolves them back to real types.
        try:
            import typing as _typing
            hints = _typing.get_type_hints(fn)
        except Exception:
            hints = {}
        params_schema = _signature_to_json_schema(sig, hints)
        # docstring → description fallback
        if description is None:
            description = (fn.__doc__ or "").strip().split("\n")[0] or f"Tool: {fn.__name__}"

        def _invoker(args: dict):
            return fn(**args)

        return cls(
            name=name or fn.__name__,
            description=description,
            parameters=params_schema,
            _invoker=_invoker,
        )

    @classmethod
    def from_aws_lambda(
        cls,
        arn: str,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict | None = None,
        region: str = "us-east-1",
    ) -> Tool:
        """Wrap an existing AWS Lambda as a Tool. Real-cloud invocations on
        each call via boto3 `Invoke`."""
        import boto3
        client = boto3.client("lambda", region_name=region)
        derived_name = name or arn.rsplit(":", 1)[-1].rsplit("/", 1)[-1]

        def _invoker(args: dict):
            resp = client.invoke(
                FunctionName=arn,
                Payload=json.dumps(args).encode(),
            )
            payload = resp["Payload"].read()
            if not payload:
                return None
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return payload.decode("utf-8", errors="replace")

        return cls(
            name=derived_name,
            description=description or f"AWS Lambda: {arn}",
            parameters=parameters or {"type": "object", "properties": {}, "additionalProperties": True},
            _invoker=_invoker,
            metadata={"source": "aws_lambda", "arn": arn, "region": region},
        )

    @classmethod
    def from_openapi(
        cls,
        spec_url_or_dict: str | dict,
        *,
        operation_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Tool:
        """Wrap one operation of an OpenAPI spec.

        For M1.5 we fetch the spec, find the matching operation, and build
        an httpx-backed invoker. Full multi-operation support and auth
        flows come with AgentCore Gateway integration in M2.
        """
        import httpx
        if isinstance(spec_url_or_dict, str):
            spec = httpx.get(spec_url_or_dict, timeout=15.0).json()
            base_url = spec.get("servers", [{"url": ""}])[0]["url"]
        else:
            spec = spec_url_or_dict
            base_url = spec.get("servers", [{"url": ""}])[0]["url"]

        op_path, op_method, op_obj = _find_operation(spec, operation_id)
        if op_obj is None:
            raise ValueError(f"No operation found in OpenAPI spec (operation_id={operation_id!r})")

        derived_name = name or op_obj.get("operationId") or f"{op_method}_{op_path}"
        derived_desc = description or op_obj.get("summary") or op_obj.get("description") or derived_name

        def _invoker(args: dict):
            url = base_url.rstrip("/") + op_path
            # Naively substitute path params; everything else goes in JSON body.
            path_params = {p["name"] for p in op_obj.get("parameters", []) if p.get("in") == "path"}
            for p in path_params:
                if p in args:
                    url = url.replace("{" + p + "}", str(args[p]))
            body = {k: v for k, v in args.items() if k not in path_params}
            r = httpx.request(op_method.upper(), url, json=body, timeout=30.0)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            return r.json() if ct.startswith("application/json") else r.text

        return cls(
            name=derived_name,
            description=derived_desc,
            parameters=op_obj.get("requestBody", {}).get("content", {})
                       .get("application/json", {}).get("schema",
                       {"type": "object", "properties": {}}),
            _invoker=_invoker,
            metadata={"source": "openapi", "path": op_path, "method": op_method},
        )

    @classmethod
    def from_mcp_server(
        cls,
        url: str,
        *,
        tool_name: str,
        auth: Callable[[], str] | None = None,
        description: str | None = None,
    ) -> Tool:
        """Proxy to a remote MCP server's tool.

        For M1.5: minimal JSON-RPC client. AgentCore Gateway integration
        and full MCP capability negotiation arrive in M2.
        """
        import httpx

        def _invoker(args: dict):
            headers = {"Content-Type": "application/json"}
            if auth is not None:
                token = auth()
                headers["Authorization"] = f"Bearer {token}"
            payload = {
                "jsonrpc": "2.0",
                "id": "1",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
            r = httpx.post(url, json=payload, headers=headers, timeout=30.0)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(f"MCP error: {data['error']}")
            return data.get("result", {})

        return cls(
            name=tool_name,
            description=description or f"MCP tool {tool_name!r} at {url}",
            parameters={"type": "object", "properties": {}, "additionalProperties": True},
            _invoker=_invoker,
            metadata={"source": "mcp", "url": url, "remote_tool_name": tool_name},
        )


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


_PY_TO_JSON = {
    int: "integer", float: "number", str: "string", bool: "boolean",
    list: "array", dict: "object",
}


def _signature_to_json_schema(
    sig: inspect.Signature, hints: dict | None = None,
) -> dict:
    """Build a minimal JSON Schema from a Python function signature.

    Prefers `hints` (resolved via typing.get_type_hints) over raw annotations,
    which handles `from __future__ import annotations` correctly.
    """
    hints = hints or {}
    props: dict = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        # Look up resolved type from hints first, then fall back to raw annotation.
        ann = hints.get(name, param.annotation)
        json_type = _PY_TO_JSON.get(ann, "string") if ann is not inspect.Parameter.empty else "string"
        props[name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _find_operation(spec: dict, operation_id: str | None) -> tuple[str, str, dict | None]:
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if operation_id is None or op.get("operationId") == operation_id:
                return path, method, op
    return "", "", None


# --------------------------------------------------------------------- #
# @cloudless.tool decorator
# --------------------------------------------------------------------- #


def tool(
    fn: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
):
    """Decorator: wrap a function as a `Tool`.

    Usage:
        @cloudless.tool
        def get_weather(city: str) -> dict:
            "Get current weather for a city."
            ...

        @cloudless.tool(name="search", description="...")
        def search_docs(q: str) -> list:
            ...
    """
    def _wrap(f: Callable) -> Tool:
        return Tool.from_function(f, name=name, description=description)
    if fn is not None and callable(fn):
        return _wrap(fn)
    return _wrap
