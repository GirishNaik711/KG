#!/usr/bin/env python3
"""
AWS connectivity test handlers.

Uses boto3 (optional import). Each function receives a config dict and optional
credentials, returns a result dict. Never prints or persists credentials.
"""

import time


def _require_boto3():
    """Import boto3 or raise with install instructions."""
    try:
        import boto3
        return boto3
    except ImportError:
        raise ImportError(
            "boto3 is required for AWS connectivity tests. Install: pip install boto3"
        )


def _get_region(config: dict) -> str:
    """
    Resolve AWS region with no hardcoded default.
    Order: config.region -> AWS_REGION env -> AWS_DEFAULT_REGION env ->
    `aws configure get region` (scoped to config.aws_profile if any).
    Raises ValueError if nothing yields a region.
    """
    if config.get("region"):
        return config["region"]
    try:
        from aah.core.cloud.identity import discover_aws_region
        region, _ = discover_aws_region(config, profile=config.get("aws_profile"))
    except Exception:
        region = None
    if region:
        return region
    raise ValueError(
        "AWS region not found. Tried: config.region, env (AWS_REGION/AWS_DEFAULT_REGION), "
        f"`aws configure get region` (profile={config.get('aws_profile') or 'default'}). "
        "Set one of these or pass region in --config."
    )


def _get_session(config: dict):
    """Create a boto3 Session scoped to the configured profile (does not affect parent shell)."""
    boto3 = _require_boto3()
    profile = config.get("aws_profile")
    if profile:
        return boto3.Session(profile_name=profile, region_name=_get_region(config))
    return boto3.Session(region_name=_get_region(config))


def _get_client(service_name: str, config: dict):
    """Create a boto3 client using the scoped session."""
    session = _get_session(config)
    return session.client(service_name, region_name=_get_region(config))


def test_s3(config: dict) -> dict:
    """Test S3 access. If bucket_name is 'any' or blank, validates general account access via list_buckets."""
    bucket_name = config.get("bucket_name", "")

    start = time.perf_counter()
    try:
        client = _get_client("s3", config)
        if not bucket_name or bucket_name.lower() in ("any", "general", "skip"):
            resp = client.list_buckets()
            buckets = resp.get("Buckets", [])
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "note": "General access validated (list_buckets)",
                "verified_account_owner": resp.get("Owner", {}).get("DisplayName") or resp.get("Owner", {}).get("ID"),
                "verified_bucket_count": len(buckets),
                "verified_sample_buckets": [b["Name"] for b in buckets[:3]],
            }
        head_resp = client.head_bucket(Bucket=bucket_name)
        region = head_resp.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("x-amz-bucket-region", _get_region(config))
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_bucket_name": bucket_name,
            "verified_bucket_region": region,
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "403":
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"Access denied to bucket '{bucket_name}' — check IAM permissions",
            }
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_rds_postgres(config: dict, credentials: dict | None = None) -> dict:
    """Test RDS PostgreSQL connectivity.

    Supported auth methods (config['auth_method']):
      - "secret-manager": password fetched from AWS Secrets Manager; endpoint,
        port, database, username provided inline in config.
      - "full-config-from-secret": endpoint, port, database, username, AND
        password all fetched from a single AWS Secrets Manager JSON blob.
        Accepts either strict AWS-created shape (host, dbname) or the
        cloudless-deploy convention (endpoint, database).
        Requires: secret_arn (or secret_path / secret_name).
      - IAM database auth path was removed per session direction.
      - Anything else + credentials arg: use credentials['password'], caller
        supplies endpoint/port/database/username in config.
    """
    import json as _json

    auth_method = (config.get("auth_method") or "").lower()
    secret_arn_or_name = (
        config.get("secret_arn")
        or config.get("secret_name")
        or config.get("secret_path")
    )

    endpoint = config.get("endpoint") or config.get("hostname", "")
    port = config.get("port", 5432)
    database = config.get("database") or config.get("dbname", "postgres")
    username = config.get("username", "")
    password = None

    start = time.perf_counter()

    if auth_method in ("secret-manager", "full-config-from-secret"):
        if not secret_arn_or_name:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"auth_method={auth_method} requires secret_arn / secret_name / secret_path in config",
                "error_class": "ConfigError",
            }
        try:
            sm_client = _get_client("secretsmanager", config)
            resp = sm_client.get_secret_value(SecretId=secret_arn_or_name)
            secret_str = resp.get("SecretString", "")
            if not secret_str:
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "passed": False, "latency_ms": round(elapsed, 1),
                    "error": "SecretMalformed: SecretString missing (binary secrets not supported)",
                    "error_class": "SecretMalformed",
                }
            try:
                payload = _json.loads(secret_str)
            except _json.JSONDecodeError:
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "passed": False, "latency_ms": round(elapsed, 1),
                    "error": "SecretMalformed: SecretString is not valid JSON",
                    "error_class": "SecretMalformed",
                }

            def _pick(*keys):
                for k in keys:
                    v = payload.get(k)
                    if v not in (None, ""):
                        return v
                return None

            if auth_method == "full-config-from-secret":
                endpoint = endpoint or _pick("host", "endpoint") or ""
                port_from_secret = _pick("port")
                if port_from_secret is not None:
                    port = int(port_from_secret)
                database = database or _pick("dbname", "database") or "postgres"
                username = username or _pick("username") or ""
            password = _pick("password")
        except ImportError as e:
            return {"passed": False, "latency_ms": 0, "error": str(e), "error_class": "ImportError"}
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            err_str = str(e)
            if "ResourceNotFoundException" in err_str or "not found" in err_str.lower():
                return {
                    "passed": False, "latency_ms": round(elapsed, 1),
                    "error": f"SecretNotFound: {secret_arn_or_name!r}",
                    "error_class": "SecretNotFound",
                }
            if "AccessDeniedException" in err_str or "not authorized" in err_str.lower():
                return {
                    "passed": False, "latency_ms": round(elapsed, 1),
                    "error": "AccessDenied: caller cannot read secret (needs secretsmanager:GetSecretValue)",
                    "error_class": "AccessDenied",
                }
            return {
                "passed": False, "latency_ms": round(elapsed, 1),
                "error": f"Secrets Manager fetch failed: {err_str}",
                "error_class": "SecretsManagerError",
            }
    elif credentials:
        password = credentials.get("password")

    if not endpoint:
        return {
            "passed": False,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "error": "Missing endpoint (not in config and not fetched from secret)",
            "error_class": "ConfigError",
        }

    port = int(port)

    try:
        import psycopg2
        conn_kwargs = {
            "host": endpoint,
            "port": port,
            "dbname": database,
            "user": username,
            "connect_timeout": 10,
        }
        if password:
            conn_kwargs["password"] = password
            if auth_method == "iam":
                conn_kwargs["sslmode"] = "require"

        conn = psycopg2.connect(**conn_kwargs)
        cur = conn.cursor()
        cur.execute(
            "SELECT current_database(), current_user, "
            "split_part(version(), ' ', 2) AS server_version, "
            "inet_server_addr()::text"
        )
        verified_db, verified_user, server_version, server_ip = cur.fetchone()
        cur.close()
        conn.close()

        # Derive RDS instance identifier from the endpoint hostname.
        # AWS RDS endpoints follow: <instance-id>.<unique>.<region>.rds.amazonaws.com
        verified_instance = endpoint.split(".")[0] if "." in endpoint else endpoint

        elapsed = (time.perf_counter() - start) * 1000
        del password
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_instance": verified_instance,
            "verified_database": verified_db,
            "verified_user": verified_user,
            "verified_server_version": f"PostgreSQL {server_version}",
            "verified_server_ip": server_ip,
        }
    except ImportError:
        del password
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "psycopg2 not installed. Install: pip install psycopg2-binary",
        }
    except Exception as e:
        del password
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_dynamodb(config: dict) -> dict:
    """Test DynamoDB access via describe_table or list_tables."""
    table_name = config.get("table_name", "any")
    region = _get_region(config)

    start = time.perf_counter()
    try:
        client = _get_client("dynamodb", config)
        if table_name and table_name != "any":
            resp = client.describe_table(TableName=table_name)
            table = resp.get("Table", {})
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "verified_table_name": table.get("TableName"),
                "verified_table_status": table.get("TableStatus"),
                "verified_table_arn": table.get("TableArn"),
                "verified_region": region,
                "verified_item_count": table.get("ItemCount"),
            }
        resp = client.list_tables(Limit=5)
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_region": region,
            "verified_sample_tables": resp.get("TableNames", []),
            "verified_table_count": len(resp.get("TableNames", [])),
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_opensearch_serverless(config: dict) -> dict:
    """Test AWS OpenSearch Serverless access via the control plane (BatchGetCollection).

    Uses a control-plane describe call rather than a signed data-plane HTTP request:
    Serverless collections don't expose the classic GET "/" cluster-info endpoint managed
    domains do (so that check always 404s regardless of SigV4 service name), and data-plane
    calls are additionally gated by a separate data access policy the control plane doesn't
    need to know about.
    """
    collection_id = config.get("collection_id", "")
    collection_name = config.get("collection_name", "")
    region = _get_region(config)

    start = time.perf_counter()
    try:
        client = _get_client("opensearchserverless", config)
        if collection_id or collection_name:
            if collection_id:
                resp = client.batch_get_collection(ids=[collection_id])
            else:
                resp = client.batch_get_collection(names=[collection_name])
            details = resp.get("collectionDetails", [])
            errors = resp.get("collectionErrorDetails", [])
            elapsed = (time.perf_counter() - start) * 1000
            if not details:
                err = errors[0].get("errorMessage") if errors else "Collection not found"
                return {"passed": False, "latency_ms": round(elapsed, 1), "error": err}
            collection = details[0]
            status = collection.get("status")
            return {
                "passed": status == "ACTIVE",
                "latency_ms": round(elapsed, 1),
                "error": None if status == "ACTIVE" else f"Collection status={status}",
                "verified_collection_id": collection.get("id"),
                "verified_collection_name": collection.get("name"),
                "verified_collection_status": status,
                "verified_collection_arn": collection.get("arn"),
                "verified_collection_endpoint": collection.get("collectionEndpoint"),
                "verified_region": region,
            }
        resp = client.list_collections()
        collections = resp.get("collectionSummaries", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "note": "General access validated (list_collections)",
            "verified_region": region,
            "verified_collection_count": len(collections),
            "verified_sample_collections": [c["name"] for c in collections[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_bedrock(config: dict) -> dict:
    """Test Amazon Bedrock access. Falls back to list_foundation_models if specific model is EOL."""
    model_id = config.get("model_id", "")
    region = _get_region(config)

    start = time.perf_counter()
    try:
        client = _get_client("bedrock", config)
        if model_id:
            try:
                resp = client.get_foundation_model(modelIdentifier=model_id)
                model = resp.get("modelDetails", {})
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "passed": True,
                    "latency_ms": round(elapsed, 1),
                    "error": None,
                    "verified_model_id": model.get("modelId"),
                    "verified_model_name": model.get("modelName"),
                    "verified_provider": model.get("providerName"),
                    "verified_region": region,
                    "verified_input_modalities": model.get("inputModalities"),
                    "verified_output_modalities": model.get("outputModalities"),
                }
            except Exception as model_err:
                # If model is EOL, deprecated, or not available — fall back to list call.
                err_msg = str(model_err).lower()
                if "end of its life" in err_msg or "not found" in err_msg or "deprecat" in err_msg:
                    resp = client.list_foundation_models(byOutputModality="TEXT")
                    models = resp.get("modelSummaries", [])
                    elapsed = (time.perf_counter() - start) * 1000
                    return {
                        "passed": True,
                        "latency_ms": round(elapsed, 1),
                        "error": None,
                        "note": f"Model '{model_id}' unavailable; validated general Bedrock access instead",
                        "verified_region": region,
                        "verified_text_model_count": len(models),
                        "verified_sample_models": [m["modelId"] for m in models[:3]],
                    }
                raise
        else:
            resp = client.list_foundation_models(byOutputModality="TEXT")
            models = resp.get("modelSummaries", [])
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "verified_region": region,
                "verified_text_model_count": len(models),
                "verified_sample_models": [m["modelId"] for m in models[:3]],
            }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_bedrock_guardrails(config: dict) -> dict:
    """Test Amazon Bedrock Guardrails access via list_guardrails."""
    region = _get_region(config)
    start = time.perf_counter()
    try:
        client = _get_client("bedrock", config)
        resp = client.list_guardrails(maxResults=5)
        guardrails = resp.get("guardrails", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_region": region,
            "verified_guardrail_count": len(guardrails),
            "verified_sample_guardrails": [g.get("name") for g in guardrails[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_agentcore_mcp(config: dict) -> dict:
    """Probe an MCP tool server deployed on AWS Bedrock AgentCore Runtime.

    Constructs the runtime invocation URL from the provided ARN, POSTs a
    SigV4-signed JSON-RPC `tools/list` call to it, parses the (SSE-formatted)
    response, and returns the list of tools the MCP server advertises.
    Read-only — only calls MCP `tools/list`, never `tools/call`.

    Config keys:
      - runtime_arn (required): arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>
      - region (required, else derived from ARN)
      - aws_profile (optional, propagated from --profile at the gate layer)
    """
    import json as _json
    runtime_arn = (config.get("runtime_arn") or "").strip()
    if not runtime_arn:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing runtime_arn (AgentCore runtime ARN)",
            "error_class": "ConfigError",
        }

    # Region: prefer explicit config; else pull from the ARN.
    region = _get_region(config)
    if not region:
        # arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>
        try:
            region = runtime_arn.split(":")[3]
        except IndexError:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Could not derive region from runtime_arn={runtime_arn!r}",
                "error_class": "ConfigError",
            }

    encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )

    start = time.perf_counter()
    try:
        import boto3
        import httpx
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
    except ImportError as e:
        return {
            "passed": False, "latency_ms": 0,
            "error": f"Missing dependency: {e}. Install boto3 + httpx.",
            "error_class": "ImportError",
        }

    try:
        session = boto3.Session(
            profile_name=config.get("aws_profile"),
            region_name=region,
        )
        credentials = session.get_credentials().get_frozen_credentials()
        body = _json.dumps({
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1,
            "params": {},
        })
        request = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)
        signed_headers = dict(request.headers)

        resp = httpx.post(url, content=body, headers=signed_headers, timeout=30)
        elapsed = (time.perf_counter() - start) * 1000

        if resp.status_code != 200:
            err_class = "AuthFailure" if resp.status_code in (401, 403) else "HttpError"
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "error_class": err_class,
                "verified_runtime_arn": runtime_arn,
            }

        # SSE lines start with 'data: '; the actual JSON-RPC payload is on one of them.
        payload = None
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                try:
                    payload = _json.loads(line[6:])
                    break
                except _json.JSONDecodeError:
                    continue
        if payload is None:
            # Fall back to parsing the full body as JSON.
            try:
                payload = _json.loads(resp.text)
            except _json.JSONDecodeError:
                return {
                    "passed": False,
                    "latency_ms": round(elapsed, 1),
                    "error": f"Runtime responded 200 but body isn't JSON-RPC: {resp.text[:200]}",
                    "error_class": "MalformedResponse",
                }

        if "error" in payload:
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"MCP tools/list error: {payload['error']}",
                "error_class": "MCPProtocolError",
                "verified_runtime_arn": runtime_arn,
            }

        tools = (payload.get("result") or {}).get("tools") or []
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_runtime_arn": runtime_arn,
            "verified_region": region,
            "verified_tools_count": len(tools),
            "verified_tool_names": [t.get("name") for t in tools[:10]],
        }

    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        err_str = str(e)
        err_class = "ConnectError" if "connect" in err_str.lower() else "UnknownError"
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": err_str,
            "error_class": err_class,
            "verified_runtime_arn": runtime_arn,
        }


def test_agentcore_a2a(config: dict) -> dict:
    """Probe an A2A (Agent-to-Agent) server deployed on AWS Bedrock AgentCore Runtime.

    Fetches the Agent Card from /.well-known/agent-card.json under the runtime
    ARN. Read-only — GET only, never invokes tasks or messages.

    Auth modes:
      - "sigv4" (default): SigV4-signed via the AWS profile passed at the gate layer.
      - "bearer": Cognito OAuth bearer token pulled from AWS Secrets Manager
        by name/ARN in config['bearer_token_secret_path'].

    Config keys:
      - runtime_arn (required): arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>
      - region (required, else derived from ARN)
      - auth_method (optional): "sigv4" (default) or "bearer"
      - bearer_token_secret_path (required when auth_method="bearer")
      - aws_profile (optional, propagated from --profile at the gate layer)
    """
    import json as _json
    from uuid import uuid4

    runtime_arn = (config.get("runtime_arn") or "").strip()
    if not runtime_arn:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing runtime_arn (AgentCore runtime ARN)",
            "error_class": "ConfigError",
        }

    region = _get_region(config)
    if not region:
        try:
            region = runtime_arn.split(":")[3]
        except IndexError:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Could not derive region from runtime_arn={runtime_arn!r}",
                "error_class": "ConfigError",
            }

    auth_method = (config.get("auth_method") or "sigv4").strip().lower()
    if auth_method not in ("sigv4", "bearer"):
        return {
            "passed": False, "latency_ms": 0,
            "error": f"Unknown auth_method={auth_method!r} (expected sigv4 or bearer)",
            "error_class": "ConfigError",
        }

    encoded_arn = runtime_arn.replace(":", "%3A").replace("/", "%2F")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations/.well-known/agent-card.json"
    )

    start = time.perf_counter()
    try:
        import boto3
        import httpx
    except ImportError as e:
        return {
            "passed": False, "latency_ms": 0,
            "error": f"Missing dependency: {e}. Install boto3 + httpx.",
            "error_class": "ImportError",
        }

    headers = {
        "Accept": "*/*",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid4()),
    }

    bearer_token = None
    if auth_method == "bearer":
        secret_path = (config.get("bearer_token_secret_path") or "").strip()
        if not secret_path:
            return {
                "passed": False, "latency_ms": 0,
                "error": "auth_method=bearer requires bearer_token_secret_path",
                "error_class": "ConfigError",
            }
        try:
            sm_session = boto3.Session(
                profile_name=config.get("aws_profile"),
                region_name=region,
            )
            sm = sm_session.client("secretsmanager")
            resp = sm.get_secret_value(SecretId=secret_path)
            secret_str = resp.get("SecretString", "")
            if not secret_str:
                return {
                    "passed": False, "latency_ms": 0,
                    "error": "SecretMalformed: SecretString missing (binary secrets not supported)",
                    "error_class": "SecretMalformed",
                }
            try:
                payload = _json.loads(secret_str)
                bearer_token = (
                    payload.get("token")
                    or payload.get("bearer_token")
                    or payload.get("bearer")
                    or secret_str
                )
            except _json.JSONDecodeError:
                bearer_token = secret_str
            headers["Authorization"] = f"Bearer {bearer_token}"
        except Exception as e:
            err_str = str(e)
            if "ResourceNotFoundException" in err_str or "not found" in err_str.lower():
                return {
                    "passed": False, "latency_ms": 0,
                    "error": f"SecretNotFound: {secret_path!r}",
                    "error_class": "SecretNotFound",
                }
            if "AccessDeniedException" in err_str or "not authorized" in err_str.lower():
                return {
                    "passed": False, "latency_ms": 0,
                    "error": "AccessDenied: caller cannot read secret (needs secretsmanager:GetSecretValue)",
                    "error_class": "AccessDenied",
                }
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Secrets Manager fetch failed: {err_str}",
                "error_class": "SecretsManagerError",
            }
    else:
        try:
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
        except ImportError as e:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Missing dependency: {e}. Install boto3.",
                "error_class": "ImportError",
            }
        session = boto3.Session(
            profile_name=config.get("aws_profile"),
            region_name=region,
        )
        credentials = session.get_credentials().get_frozen_credentials()
        request = AWSRequest(method="GET", url=url, headers=headers)
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)
        headers = dict(request.headers)

    try:
        resp = httpx.get(url, headers=headers, timeout=30)
        elapsed = (time.perf_counter() - start) * 1000

        if bearer_token is not None:
            del bearer_token

        if resp.status_code != 200:
            err_class = "AuthFailure" if resp.status_code in (401, 403) else "HttpError"
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "error_class": err_class,
                "verified_runtime_arn": runtime_arn,
                "verified_auth_mode": auth_method,
            }

        try:
            card = resp.json()
        except Exception:
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"Runtime responded 200 but body isn't JSON: {resp.text[:200]}",
                "error_class": "MalformedResponse",
                "verified_runtime_arn": runtime_arn,
                "verified_auth_mode": auth_method,
            }

        skills = card.get("skills") or []
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_runtime_arn": runtime_arn,
            "verified_region": region,
            "verified_auth_mode": auth_method,
            "verified_agent_name": card.get("name"),
            "verified_agent_description": card.get("description"),
            "verified_agent_version": card.get("version"),
            "verified_protocol_version": card.get("protocolVersion"),
            "verified_preferred_transport": card.get("preferredTransport"),
            "verified_capabilities": card.get("capabilities") or {},
            "verified_default_input_modes": card.get("defaultInputModes") or [],
            "verified_default_output_modes": card.get("defaultOutputModes") or [],
            "verified_agent_url": card.get("url"),
            "verified_skills_count": len(skills),
            "verified_skill_ids": [s.get("id") for s in skills[:10]],
            "verified_skills": [
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "description": s.get("description"),
                    "tags": s.get("tags") or [],
                }
                for s in skills[:20]
            ],
            "verified_agent_card": card,
        }

    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        err_str = str(e)
        err_class = "ConnectError" if "connect" in err_str.lower() else "UnknownError"
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": err_str,
            "error_class": err_class,
            "verified_runtime_arn": runtime_arn,
            "verified_auth_mode": auth_method,
        }


def test_agentcore_runtime(config: dict) -> dict:
    """Probe a deployed agent on AWS Bedrock AgentCore Runtime (control plane only).

    Calls bedrock-agentcore-control:GetAgentRuntime on the runtime ARN and
    returns runtime metadata: status, protocol, network mode, container URI,
    last-updated timestamp. Read-only, no data-plane invocation, no inference
    cost. Use for plain deployed agents that are neither MCP tool servers
    (use aws-agentcore-mcp) nor A2A agents (use aws-agentcore-a2a).

    Config keys:
      - runtime_arn (required): arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>
      - region (optional, else derived from ARN)
      - aws_profile (optional, propagated from --profile at the gate layer)
    """
    runtime_arn = (config.get("runtime_arn") or "").strip()
    if not runtime_arn:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing runtime_arn (AgentCore runtime ARN)",
            "error_class": "ConfigError",
        }

    region = _get_region(config)
    if not region:
        try:
            region = runtime_arn.split(":")[3]
        except IndexError:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Could not derive region from runtime_arn={runtime_arn!r}",
                "error_class": "ConfigError",
            }

    start = time.perf_counter()
    try:
        import boto3
    except ImportError as e:
        return {
            "passed": False, "latency_ms": 0,
            "error": f"Missing dependency: {e}. Install boto3.",
            "error_class": "ImportError",
        }

    try:
        runtime_id = runtime_arn.split("/", 1)[1] if "/" in runtime_arn else ""
        if not runtime_id:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Could not extract runtime id from runtime_arn={runtime_arn!r} (expected .../runtime/<id>)",
                "error_class": "ConfigError",
            }
        session = boto3.Session(
            profile_name=config.get("aws_profile"),
            region_name=region,
        )
        client = session.client("bedrock-agentcore-control")
        resp = client.get_agent_runtime(agentRuntimeId=runtime_id)
        elapsed = (time.perf_counter() - start) * 1000
        artifact = (resp.get("agentRuntimeArtifact") or {}).get("containerConfiguration") or {}
        network = resp.get("networkConfiguration") or {}
        protocol = resp.get("protocolConfiguration") or {}
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "get-agent-runtime",
            "verified_region": region,
            "verified_runtime_arn": resp.get("agentRuntimeArn"),
            "verified_runtime_id": resp.get("agentRuntimeId"),
            "verified_runtime_name": resp.get("agentRuntimeName"),
            "verified_runtime_version": resp.get("agentRuntimeVersion"),
            "verified_status": resp.get("status"),
            "verified_protocol": protocol.get("serverProtocol"),
            "verified_network_mode": network.get("networkMode"),
            "verified_container_uri": artifact.get("containerUri"),
            "verified_role_arn": resp.get("roleArn"),
            "verified_last_updated_at": resp.get("lastUpdatedAt"),
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        err_str = str(e)
        if "ResourceNotFoundException" in err_str:
            err_class = "ResourceNotFound"
        elif "AccessDeniedException" in err_str or "not authorized" in err_str.lower():
            err_class = "AccessDenied"
        else:
            err_class = "UnknownError"
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": err_str,
            "error_class": err_class,
            "verified_runtime_arn": runtime_arn,
        }


def test_cost_explorer(config: dict) -> dict:
    """Test AWS Cost Explorer access."""
    from datetime import date, timedelta
    start = time.perf_counter()
    try:
        client = _get_client("ce", config)
        end = date.today()
        start_date = end - timedelta(days=1)
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": start_date.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        results = resp.get("ResultsByTime", [])
        cost = "0.00"
        unit = "USD"
        if results:
            total = results[0].get("Total", {}).get("UnblendedCost", {})
            cost = total.get("Amount", "0.00")
            unit = total.get("Unit", "USD")
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_period_start": start_date.isoformat(),
            "verified_period_end": end.isoformat(),
            "verified_yesterday_cost": f"{cost} {unit}",
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_secrets_manager(config: dict) -> dict:
    """
    Test AWS Secrets Manager access. Two modes (priority order):
      1. secret_arn   -- DescribeSecret on a specific secret (metadata only,
                         never GetSecretValue). Proves the secret exists AND
                         the caller has secretsmanager:DescribeSecret on it.
      2. control-plane -- ListSecrets (MaxResults=5), returns sample names.
    """
    region = _get_region(config)
    secret_arn = (config.get("secret_arn") or "").strip()
    start = time.perf_counter()
    try:
        client = _get_client("secretsmanager", config)

        if secret_arn:
            resp = client.describe_secret(SecretId=secret_arn)
            elapsed = (time.perf_counter() - start) * 1000
            rotation_enabled = resp.get("RotationEnabled", False)
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "describe-secret",
                "verified_region": region,
                "verified_secret_arn": resp.get("ARN"),
                "verified_secret_name": resp.get("Name"),
                "verified_kms_key_id": resp.get("KmsKeyId"),
                "verified_rotation_enabled": rotation_enabled,
                "verified_last_changed_date": resp.get("LastChangedDate"),
            }

        resp = client.list_secrets(MaxResults=5)
        secrets = resp.get("SecretList", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-secrets",
            "verified_region": region,
            "verified_secret_count": len(secrets),
            "verified_sample_secret_names": [s.get("Name") for s in secrets[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_ecs(config: dict) -> dict:
    """
    Test ECS cluster access. Three modes (priority order):
      1. service_url   -- HTTPS GET against an ALB / Service Connect / public IP
                          fronting an ECS service (data plane).
      2. cluster_name  -- DescribeClusters for a specific cluster (control plane).
      3. control-plane -- ListClusters (default).
    """
    service_url = (config.get("service_url") or "").strip()
    health_path = config.get("health_path", "")
    if service_url:
        return _http_probe(service_url, health_path)

    cluster_name = config.get("cluster_name", "default")
    region = _get_region(config)

    start = time.perf_counter()
    try:
        client = _get_client("ecs", config)
        if cluster_name and cluster_name != "default":
            resp = client.describe_clusters(clusters=[cluster_name])
            clusters = resp.get("clusters", [])
            if not clusters:
                elapsed = (time.perf_counter() - start) * 1000
                return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"Cluster '{cluster_name}' not found"}
            c = clusters[0]
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "verified_cluster_name": c.get("clusterName"),
                "verified_cluster_arn": c.get("clusterArn"),
                "verified_cluster_status": c.get("status"),
                "verified_running_tasks": c.get("runningTasksCount"),
                "verified_active_services": c.get("activeServicesCount"),
                "verified_region": region,
            }
        resp = client.list_clusters(maxResults=10)
        cluster_arns = resp.get("clusterArns", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_region": region,
            "verified_cluster_count": len(cluster_arns),
            "verified_sample_clusters": [a.split("/")[-1] for a in cluster_arns[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_lambda(config: dict) -> dict:
    """
    Test Lambda access. Three modes (priority order):
      1. function_url   -- HTTPS GET against a Lambda Function URL (data plane).
      2. function_name  -- Invoke the function with a tiny event (data plane).
                           Strongest signal: proves the function actually runs.
                           Optional invoke_payload (JSON) overrides the default ping.
      3. control-plane  -- list_functions() (default fallback).
    All three honor a fail-loud region.
    """
    region = _get_region(config)
    function_url = (config.get("function_url") or "").strip()
    function_name = (config.get("function_name") or "").strip()
    invoke_payload = config.get("invoke_payload")

    if function_url:
        return _http_probe(function_url, config.get("health_path", ""))

    start = time.perf_counter()
    try:
        client = _get_client("lambda", config)

        if function_name:
            import json as _json
            payload_bytes = _json.dumps(
                invoke_payload if invoke_payload is not None else {"ping": True}
            ).encode("utf-8")
            resp = client.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",
                LogType="None",
                Payload=payload_bytes,
            )
            elapsed = (time.perf_counter() - start) * 1000
            status_code = resp.get("StatusCode", 0)
            function_error = resp.get("FunctionError")
            response_payload = resp.get("Payload")
            response_chars = 0
            if response_payload is not None:
                try:
                    response_chars = len(response_payload.read())
                except Exception:
                    pass
            ok = (200 <= status_code < 300) and not function_error
            return {
                "passed": ok,
                "latency_ms": round(elapsed, 1),
                "error": f"FunctionError: {function_error}" if function_error else None,
                "probe_mode": "invoke",
                "verified_region": region,
                "verified_function_name": function_name,
                "verified_status_code": status_code,
                "verified_response_chars": response_chars,
            }

        resp = client.list_functions(MaxItems=10)
        functions = resp.get("Functions", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-functions",
            "verified_region": region,
            "verified_function_count": len(functions),
            "verified_sample_functions": [f.get("FunctionName") for f in functions[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def _http_probe(url: str, path: str = "", timeout: int = 15) -> dict:
    """
    Anonymous HTTPS GET probe used by Lambda function_url and other public
    endpoints. Accepts 200..399 as healthy.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    if not url.startswith("http"):
        url = f"https://{url}"
    if path:
        url = url.rstrip("/") + "/" + path.lstrip("/")

    start = time.perf_counter()
    headers = {"User-Agent": "aah-cloud-readiness/1.0"}
    try:
        req = Request(url, method="GET", headers=headers)
        resp = urlopen(req, timeout=timeout)
        status = resp.getcode()
    except HTTPError as e:
        status = e.code
    except URLError as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1),
                "error": f"Network error: {e.reason}", "probe_mode": "endpoint"}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1),
                "error": str(e), "probe_mode": "endpoint"}

    elapsed = (time.perf_counter() - start) * 1000
    if 200 <= status < 400:
        return {"passed": True, "latency_ms": round(elapsed, 1),
                "error": None, "probe_mode": "endpoint", "verified_status": status}
    return {"passed": False, "latency_ms": round(elapsed, 1),
            "error": f"HTTP {status}", "probe_mode": "endpoint", "verified_status": status}


def test_ec2(config: dict) -> dict:
    """
    Test EC2 access. Three modes (priority order):
      1. instance_url   -- HTTPS GET against a public IP / DNS / load balancer
                           fronting an EC2 instance (data plane).
      2. instance_id    -- DescribeInstances for a specific instance.
      3. control-plane  -- DescribeInstances (paginated) — proves IAM read.
    Region is resolved with no hardcoded default; missing region is fail-loud.
    """
    region = _get_region(config)
    instance_url = (config.get("instance_url") or config.get("service_url") or "").strip()
    instance_id = (config.get("instance_id") or "").strip()
    health_path = config.get("health_path", "")

    if instance_url:
        return _http_probe(instance_url, health_path)

    start = time.perf_counter()
    try:
        client = _get_client("ec2", config)
        if instance_id:
            resp = client.describe_instances(InstanceIds=[instance_id])
            reservations = resp.get("Reservations", [])
            instances = [i for r in reservations for i in r.get("Instances", [])]
            if not instances:
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "passed": False, "latency_ms": round(elapsed, 1),
                    "error": f"Instance '{instance_id}' not found",
                }
            inst = instances[0]
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "describe-instance",
                "verified_instance_id": inst.get("InstanceId"),
                "verified_state": (inst.get("State") or {}).get("Name"),
                "verified_instance_type": inst.get("InstanceType"),
                "verified_az": (inst.get("Placement") or {}).get("AvailabilityZone"),
                "verified_public_dns": inst.get("PublicDnsName") or "",
                "verified_private_ip": inst.get("PrivateIpAddress") or "",
                "verified_region": region,
            }

        resp = client.describe_instances(MaxResults=10)
        reservations = resp.get("Reservations", [])
        sample = []
        count = 0
        for r in reservations:
            for inst in r.get("Instances", []):
                count += 1
                if len(sample) < 3:
                    sample.append({
                        "id": inst.get("InstanceId"),
                        "state": (inst.get("State") or {}).get("Name"),
                        "type": inst.get("InstanceType"),
                    })
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "describe-instances",
            "verified_region": region,
            "verified_instance_count": count,
            "verified_sample_instances": sample,
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_eks(config: dict) -> dict:
    """
    Test EKS access. Three modes:
      1. ingress_url   -- HTTPS GET against a service ingress (data plane).
      2. cluster_name  -- DescribeCluster against a specific EKS cluster.
      3. control-plane -- ListClusters (default).
    Uses the real EKS API (NOT ECS).
    """
    region = _get_region(config)
    cluster_name = (config.get("cluster_name") or "").strip()
    ingress_url = (config.get("ingress_url") or "").strip()
    health_path = config.get("health_path", "")

    if ingress_url:
        return _http_probe(ingress_url, health_path)

    start = time.perf_counter()
    try:
        client = _get_client("eks", config)
        if cluster_name:
            resp = client.describe_cluster(name=cluster_name)
            c = resp.get("cluster", {})
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "describe-cluster",
                "verified_cluster_name": c.get("name"),
                "verified_cluster_arn": c.get("arn"),
                "verified_status": c.get("status"),
                "verified_version": c.get("version"),
                "verified_endpoint": c.get("endpoint"),
                "verified_platform_version": c.get("platformVersion"),
                "verified_region": region,
            }
        resp = client.list_clusters(maxResults=10)
        names = resp.get("clusters", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-clusters",
            "verified_region": region,
            "verified_cluster_count": len(names),
            "verified_sample_clusters": names[:3],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_app_runner(config: dict) -> dict:
    """
    Test App Runner access. Three modes:
      1. service_url   -- HTTPS GET against the App Runner service URL (data plane).
      2. service_arn / service_name -- DescribeService (control plane).
      3. control-plane -- ListServices (default).
    """
    region = _get_region(config)
    service_url = (config.get("service_url") or "").strip()
    service_arn = (config.get("service_arn") or "").strip()
    service_name = (config.get("service_name") or "").strip()
    health_path = config.get("health_path", "")

    if service_url:
        return _http_probe(service_url, health_path)

    start = time.perf_counter()
    try:
        client = _get_client("apprunner", config)
        if service_arn:
            resp = client.describe_service(ServiceArn=service_arn)
            svc = resp.get("Service", {})
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "describe-service",
                "verified_service_name": svc.get("ServiceName"),
                "verified_service_arn": svc.get("ServiceArn"),
                "verified_service_url": svc.get("ServiceUrl"),
                "verified_status": svc.get("Status"),
                "verified_region": region,
            }
        resp = client.list_services(MaxResults=10)
        services = resp.get("ServiceSummaryList", [])
        elapsed = (time.perf_counter() - start) * 1000
        match = next(
            (s for s in services if s.get("ServiceName") == service_name),
            None,
        ) if service_name else None
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-services",
            "verified_region": region,
            "verified_service_count": len(services),
            "verified_sample_services": [s.get("ServiceName") for s in services[:3]],
            "verified_named_match": bool(match),
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_aws_batch(config: dict) -> dict:
    """
    Test AWS Batch access. Two modes:
      1. job_queue   -- DescribeJobQueues for a specific queue.
      2. control-plane -- ListJobQueues (default).
    Batch has no public data-plane endpoint to probe.
    """
    region = _get_region(config)
    job_queue = (config.get("job_queue") or "").strip()

    start = time.perf_counter()
    try:
        client = _get_client("batch", config)
        if job_queue:
            resp = client.describe_job_queues(jobQueues=[job_queue])
            queues = resp.get("jobQueues", [])
            if not queues:
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "passed": False, "latency_ms": round(elapsed, 1),
                    "error": f"job_queue '{job_queue}' not found",
                }
            q = queues[0]
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "describe-job-queues",
                "verified_job_queue": q.get("jobQueueName"),
                "verified_state": q.get("state"),
                "verified_status": q.get("status"),
                "verified_region": region,
            }
        resp = client.describe_job_queues(maxResults=10)
        queues = resp.get("jobQueues", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-job-queues",
            "verified_region": region,
            "verified_queue_count": len(queues),
            "verified_sample_queues": [q.get("jobQueueName") for q in queues[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_elasticache(config: dict) -> dict:
    """Test ElastiCache. Falls back to describe_cache_clusters if no endpoint provided."""
    region = _get_region(config)
    endpoint = config.get("endpoint") or config.get("hostname")

    if endpoint:
        from aah.core.cloud.handlers.general import test_tcp_connect
        result = test_tcp_connect(config)
        if result.get("passed"):
            result["verified_endpoint"] = endpoint
            result["verified_port"] = config.get("port")
            result["verified_region"] = region
        return result

    # No endpoint — validate IAM access via describe_cache_clusters
    start = time.perf_counter()
    try:
        client = _get_client("elasticache", config)
        resp = client.describe_cache_clusters(MaxRecords=20)
        clusters = resp.get("CacheClusters", [])
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "note": "General access validated (describe_cache_clusters)",
            "verified_region": region,
            "verified_cluster_count": len(clusters),
            "verified_sample_clusters": [c.get("CacheClusterId") for c in clusters[:3]],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_cloudwatch(config: dict) -> dict:
    """Test CloudWatch access."""
    region = _get_region(config)
    start = time.perf_counter()
    try:
        client = _get_client("cloudwatch", config)
        resp = client.list_metrics()
        metrics = resp.get("Metrics", [])
        elapsed = (time.perf_counter() - start) * 1000
        namespaces = sorted({m.get("Namespace") for m in metrics if m.get("Namespace")})
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_region": region,
            "verified_metric_count": len(metrics),
            "verified_sample_namespaces": namespaces[:5],
        }
    except ImportError as e:
        return {"passed": False, "latency_ms": 0, "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_opensearch(config: dict) -> dict:
    """Test OpenSearch domain access via signed HTTP request."""
    endpoint = config.get("endpoint", "")
    if not endpoint:
        return {"passed": False, "latency_ms": 0, "error": "Missing endpoint"}

    start = time.perf_counter()
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        import urllib.request

        session = _get_session(config)
        creds = session.get_credentials().get_frozen_credentials()
        region = _get_region(config)

        url = endpoint.rstrip("/") + "/"
        request = AWSRequest(method="GET", url=url)
        SigV4Auth(creds, "es", region).add_auth(request)

        req = urllib.request.Request(url, headers=dict(request.headers))
        resp = urllib.request.urlopen(req, timeout=15)
        elapsed = (time.perf_counter() - start) * 1000
        if resp.getcode() == 200:
            return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"HTTP {resp.getcode()}"}
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


