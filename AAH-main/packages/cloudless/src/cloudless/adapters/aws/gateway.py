"""AgentCore Gateway create + use (Q15 / M2).

A *Gateway* is AgentCore's MCP-front for tools: it adapts AWS Lambda
functions (and other targets) so agents can call them through a uniform
MCP-style endpoint with IAM-friendly auth.

This module exposes two helpers:

  ensure_gateway(name, *, role_arn)
      Idempotently create (or return) an AgentCore Gateway with the
      given name. Returns the Gateway ID + invocation URL.

  ensure_lambda_target(gateway_id, *, name, lambda_arn, input_schema)
      Idempotently create (or return) a Gateway Target bound to a Lambda
      function. Returns the Target ID.

These are deploy-time helpers — the agent's `cloudless.Tool.from_aws_lambda`
already works for direct invocation, but for production deploys users
typically want the Gateway pathway for centralized auth/observability.
"""
from __future__ import annotations

from typing import Any


def ensure_gateway(
    *,
    name: str,
    role_arn: str,
    region: str = "us-east-1",
    client: Any = None,
    description: str | None = None,
    cognito_discovery_url: str | None = None,
    cognito_audience: str | None = None,
    authorizer_type: str = "CUSTOM_JWT",
) -> dict:
    """Idempotently create an AgentCore Gateway.

    Returns:
        {"id": <gateway-id>, "url": <invocation-url>, "name": <name>}
    """
    if client is None:
        import boto3
        client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Search existing — API response key is `items`
    try:
        resp = client.list_gateways()
        existing = resp.get("items", resp.get("gateways", []))
        for gw in existing:
            if gw.get("name") == name:
                return {
                    "id": gw.get("gatewayId") or gw.get("id"),
                    "url": gw.get("gatewayUrl") or gw.get("invocationUrl"),
                    "name": name,
                }
    except Exception:
        pass

    create_kwargs: dict[str, Any] = {
        "name": name,
        "description": description or f"cloudless-managed gateway: {name}",
        "roleArn": role_arn,
        "protocolType": "MCP",
        "authorizerType": authorizer_type,
    }
    if authorizer_type == "CUSTOM_JWT":
        if not (cognito_discovery_url and cognito_audience):
            raise RuntimeError(
                "CUSTOM_JWT authorizer requires cognito_discovery_url and cognito_audience"
            )
        create_kwargs["authorizerConfiguration"] = {
            "customJWTAuthorizer": {
                "discoveryUrl": cognito_discovery_url,
                "allowedAudience": [cognito_audience],
            },
        }
    try:
        resp = client.create_gateway(**create_kwargs)
    except Exception as e:
        raise RuntimeError(f"Failed to create Gateway {name!r}: {e}") from e

    gid = resp.get("gatewayId") or resp.get("id")
    url = resp.get("gatewayUrl") or resp.get("invocationUrl")
    if not gid:
        raise RuntimeError(f"create_gateway returned no id: {resp!r}")
    return {"id": gid, "url": url, "name": name}


def ensure_openapi_target(
    *,
    gateway_id: str,
    name: str,
    openapi_spec: dict,
    region: str = "us-east-1",
    client: Any = None,
    description: str | None = None,
) -> dict:
    """Idempotently create a Gateway Target backed by an OpenAPI spec.

    The Gateway proxies the spec's operations as MCP tools.
    """
    if client is None:
        import boto3
        client = boto3.client("bedrock-agentcore-control", region_name=region)

    try:
        resp = client.list_gateway_targets(gatewayIdentifier=gateway_id)
        existing = resp.get("items", resp.get("targets", []))
        for target in existing:
            if target.get("name") == name:
                return {
                    "id": target.get("targetId") or target.get("id"),
                    "name": name,
                    "gateway_id": gateway_id,
                }
    except Exception:
        pass

    try:
        import json as _json
        resp = client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            description=description or f"cloudless-managed openapi target: {name}",
            targetConfiguration={
                "mcp": {
                    "openApiSchema": {
                        "inlineSchema": _json.dumps(openapi_spec),
                    },
                },
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"},
            ],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to create OpenAPI Gateway Target {name!r}: {e}") from e

    tid = resp.get("targetId") or resp.get("id")
    if not tid:
        raise RuntimeError(f"create_gateway_target returned no id: {resp!r}")
    return {"id": tid, "name": name, "gateway_id": gateway_id}


def ensure_lambda_target(
    *,
    gateway_id: str,
    name: str,
    lambda_arn: str,
    input_schema: dict,
    region: str = "us-east-1",
    client: Any = None,
    description: str | None = None,
) -> dict:
    """Idempotently create a Gateway Target backed by a Lambda function.

    Returns:
        {"id": <target-id>, "name": <name>, "gateway_id": <gateway-id>}
    """
    if client is None:
        import boto3
        client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Search existing targets under the gateway
    try:
        for target in client.list_gateway_targets(gatewayId=gateway_id).get("targets", []):
            if target.get("name") == name:
                return {
                    "id": target.get("targetId") or target.get("id"),
                    "name": name,
                    "gateway_id": gateway_id,
                }
    except Exception:
        pass

    try:
        resp = client.create_gateway_target(
            gatewayId=gateway_id,
            name=name,
            description=description or f"cloudless-managed target: {name}",
            targetConfiguration={
                "mcp": {
                    "lambda": {
                        "lambdaArn": lambda_arn,
                        "toolSchema": {
                            "inlineSchema": {
                                "name": name,
                                "description": description or name,
                                "inputSchema": input_schema,
                            },
                        },
                    },
                },
            },
            credentialProviderConfigurations=[
                {"credentialProviderType": "GATEWAY_IAM_ROLE"},
            ],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to create Gateway Target {name!r}: {e}") from e

    tid = resp.get("targetId") or resp.get("id")
    if not tid:
        raise RuntimeError(f"create_gateway_target returned no id: {resp!r}")
    return {"id": tid, "name": name, "gateway_id": gateway_id}
