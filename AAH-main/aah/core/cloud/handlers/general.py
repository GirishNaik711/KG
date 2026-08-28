#!/usr/bin/env python3
"""
General connectivity test handlers for non-cloud-managed services.

Uses only stdlib and common database drivers. No cloud SDK dependencies.
Each function receives a config dict, returns a result dict. Never prints
or persists credentials.
"""

import socket
import time
from urllib.request import urlopen, Request
from urllib.error import URLError


def test_tcp_connect(config: dict) -> dict:
    """Test basic TCP connectivity to host:port."""
    hostname = config.get("hostname") or config.get("endpoint") or config.get("bootstrap_servers", "").split(",")[0].split(":")[0]
    port = int(config.get("port", 0))
    if not hostname or not port:
        return {"passed": False, "latency_ms": 0, "error": "Missing hostname or port"}

    start = time.perf_counter()
    try:
        sock = socket.create_connection((hostname, port), timeout=10)
        sock.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except (socket.timeout, socket.gaierror, OSError) as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_http_health(config: dict) -> dict:
    """HTTP GET healthcheck against an endpoint."""
    endpoint = config.get("endpoint") or config.get("workspace_url", "")
    if not endpoint:
        return {"passed": False, "latency_ms": 0, "error": "Missing endpoint URL"}

    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"

    start = time.perf_counter()
    try:
        req = Request(endpoint, method="GET")
        req.add_header("User-Agent", "aah-cloud-readiness/1.0")
        resp = urlopen(req, timeout=15)
        elapsed = (time.perf_counter() - start) * 1000
        status = resp.getcode()
        if 200 <= status < 400:
            return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"HTTP {status}"}
    except URLError as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e.reason)}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_postgres(config: dict, credentials: dict | None = None) -> dict:
    """Test PostgreSQL connectivity using psycopg2 or pg8000."""
    hostname = config.get("hostname") or config.get("endpoint", "")
    port = int(config.get("port", 5432))
    database = config.get("database", "postgres")
    username = config.get("username", "")

    if not hostname:
        return {"passed": False, "latency_ms": 0, "error": "Missing hostname"}

    password = credentials.get("password") if credentials else None

    start = time.perf_counter()
    try:
        import psycopg2
        conn_kwargs = {
            "host": hostname,
            "port": port,
            "dbname": database,
            "connect_timeout": 10,
        }
        if username:
            conn_kwargs["user"] = username
        if password:
            conn_kwargs["password"] = password

        conn = psycopg2.connect(**conn_kwargs)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError:
        pass
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}

    try:
        import pg8000.native
        conn_kwargs = {
            "host": hostname,
            "port": port,
            "database": database,
            "timeout": 10,
        }
        if username:
            conn_kwargs["user"] = username
        if password:
            conn_kwargs["password"] = password

        conn = pg8000.native.Connection(**conn_kwargs)
        conn.run("SELECT 1")
        conn.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "No PostgreSQL driver available. Install: pip install psycopg2-binary or pip install pg8000",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_redis(config: dict, credentials: dict | None = None) -> dict:
    """Test Redis connectivity via TCP + PING/PONG."""
    hostname = config.get("hostname") or config.get("endpoint", "")
    port = int(config.get("port", 6379))

    if not hostname:
        return {"passed": False, "latency_ms": 0, "error": "Missing hostname"}

    start = time.perf_counter()
    try:
        sock = socket.create_connection((hostname, port), timeout=10)
        password = credentials.get("password") if credentials else None
        if password:
            sock.sendall(f"AUTH {password}\r\n".encode())
            auth_resp = sock.recv(128).decode()
            if not auth_resp.startswith("+OK"):
                sock.close()
                elapsed = (time.perf_counter() - start) * 1000
                return {"passed": False, "latency_ms": round(elapsed, 1), "error": "AUTH failed"}

        sock.sendall(b"PING\r\n")
        response = sock.recv(128).decode()
        sock.close()
        elapsed = (time.perf_counter() - start) * 1000
        if "+PONG" in response:
            return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"Unexpected response: {response[:50]}"}
    except (socket.timeout, socket.gaierror, OSError) as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_mongodb(config: dict, credentials: dict | None = None) -> dict:
    """Test MongoDB connectivity via pymongo ping."""
    hostname = config.get("hostname", "")
    port = int(config.get("port", 27017))
    database = config.get("database", "admin")

    if not hostname:
        return {"passed": False, "latency_ms": 0, "error": "Missing hostname"}

    start = time.perf_counter()
    try:
        from pymongo import MongoClient

        conn_kwargs = {
            "host": hostname,
            "port": port,
            "serverSelectionTimeoutMS": 10000,
            "connectTimeoutMS": 10000,
        }
        if credentials:
            if credentials.get("username"):
                conn_kwargs["username"] = credentials["username"]
            if credentials.get("password"):
                conn_kwargs["password"] = credentials["password"]

        client = MongoClient(**conn_kwargs)
        client[database].command("ping")
        client.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "pymongo not installed. Install: pip install pymongo",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_snowflake(config: dict, credentials: dict | None = None) -> dict:
    """Test Snowflake connectivity."""
    account = config.get("account", "")
    warehouse = config.get("warehouse", "")
    database = config.get("database", "")
    auth_method = config.get("auth_method", "sso-browser")

    if not account:
        return {"passed": False, "latency_ms": 0, "error": "Missing account identifier"}

    start = time.perf_counter()
    try:
        import snowflake.connector

        conn_kwargs = {
            "account": account,
            "warehouse": warehouse,
            "database": database,
            "login_timeout": 30,
        }

        if auth_method == "sso-browser":
            conn_kwargs["authenticator"] = "externalbrowser"
        elif credentials:
            conn_kwargs["user"] = credentials.get("username", "")
            conn_kwargs["password"] = credentials.get("password", "")

        conn = snowflake.connector.connect(**conn_kwargs)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "snowflake-connector-python not installed. Install: pip install snowflake-connector-python",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_neo4j(config: dict, credentials: dict | None = None) -> dict:
    """Test Neo4j connectivity via Bolt protocol."""
    bolt_uri = config.get("bolt_uri", "")
    if not bolt_uri:
        return {"passed": False, "latency_ms": 0, "error": "Missing bolt_uri"}

    start = time.perf_counter()
    try:
        from neo4j import GraphDatabase

        auth = None
        if credentials:
            auth = (credentials.get("username", "neo4j"), credentials.get("password", ""))

        driver = GraphDatabase.driver(bolt_uri, auth=auth)
        driver.verify_connectivity()
        driver.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "neo4j driver not installed. Install: pip install neo4j",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_kafka(config: dict) -> dict:
    """Test Kafka broker connectivity via TCP to bootstrap servers."""
    bootstrap = config.get("bootstrap_servers", "")
    if not bootstrap:
        return {"passed": False, "latency_ms": 0, "error": "Missing bootstrap_servers"}

    servers = [s.strip() for s in bootstrap.split(",") if s.strip()]
    if not servers:
        return {"passed": False, "latency_ms": 0, "error": "No valid bootstrap servers"}

    start = time.perf_counter()
    errors = []
    for server in servers[:3]:
        parts = server.rsplit(":", 1)
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 9092
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.close()
            elapsed = (time.perf_counter() - start) * 1000
            return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
        except (socket.timeout, socket.gaierror, OSError) as e:
            errors.append(f"{server}: {e}")

    elapsed = (time.perf_counter() - start) * 1000
    return {"passed": False, "latency_ms": round(elapsed, 1), "error": "; ".join(errors)}


def test_elasticsearch(config: dict) -> dict:
    """Test Elasticsearch/OpenSearch via HTTP GET."""
    endpoint = config.get("endpoint", "")
    if not endpoint:
        return {"passed": False, "latency_ms": 0, "error": "Missing endpoint"}

    if not endpoint.startswith("http"):
        endpoint = f"http://{endpoint}"

    return test_http_health({"endpoint": endpoint})


def test_external_mcp(config: dict) -> dict:
    """Probe an external MCP tool server (any streamable-http HTTPS URL).

    Two auth modes:
      - no-auth: hit the URL directly.
      - secret-manager: fetch a Bearer token from AWS Secrets Manager (by
        name/ARN in config['bearer_token_secret_path']) and send it as
        `Authorization: Bearer <token>`. Token never returned or logged.

    Uses the `mcp` package's ClientSession + streamablehttp_client to open
    a session, call `tools/list`, and return the tool inventory. Read-only.

    Config keys:
      - mcp_url (required): the MCP server's streamable-http endpoint.
      - bearer_token_secret_path (optional): Secrets Manager secret name
        or ARN whose SecretString is the raw token (or JSON with a `token`
        or `bearer_token` field).
      - aws_profile (optional): propagated from --profile at the gate
        layer; only used if bearer_token_secret_path is set.
    """
    mcp_url = (config.get("mcp_url") or "").strip()
    if not mcp_url:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing mcp_url (external MCP server URL)",
            "error_class": "ConfigError",
        }

    secret_path = (config.get("bearer_token_secret_path") or "").strip()

    # ── Bearer token fetch (optional) ────────────────────────────────────
    bearer_token = None
    if secret_path:
        try:
            import boto3
            import json as _json
            session = boto3.Session(
                profile_name=config.get("aws_profile"),
                region_name=config.get("region"),
            )
            sm = session.client("secretsmanager")
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
        except ImportError as e:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"boto3 not installed: {e}",
                "error_class": "ImportError",
            }
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

    # ── MCP session probe ─────────────────────────────────────────────────
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as e:
        return {
            "passed": False, "latency_ms": 0,
            "error": f"mcp package not installed: {e}. Install with: uv add mcp",
            "error_class": "ImportError",
        }

    start = time.perf_counter()
    try:
        import asyncio

        async def _list_tools():
            headers = {"authorization": f"Bearer {bearer_token}"} if bearer_token else None
            async with streamablehttp_client(mcp_url, headers=headers) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return result.tools

        tools = asyncio.run(_list_tools())
        elapsed = (time.perf_counter() - start) * 1000

        # Do NOT let the bearer_token linger in scope beyond this call.
        del bearer_token

        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "verified_mcp_url": mcp_url,
            "verified_tools_count": len(tools),
            "verified_tool_names": [t.name for t in tools[:10]],
            "verified_auth_mode": "bearer" if secret_path else "no-auth",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        err_str = str(e)
        # Categorize by best-effort pattern match; keep classes coarse so
        # the caller can react without brittle string matching.
        if "401" in err_str or "403" in err_str or "unauthorized" in err_str.lower() or "forbidden" in err_str.lower():
            err_class = "AuthFailure"
        elif "timeout" in err_str.lower():
            err_class = "NetworkTimeout"
        elif "ssl" in err_str.lower() or "tls" in err_str.lower() or "certificate" in err_str.lower():
            err_class = "TLSError"
        elif "connect" in err_str.lower():
            err_class = "ConnectError"
        else:
            err_class = "MCPProtocolError"
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": err_str[:300],
            "error_class": err_class,
            "verified_mcp_url": mcp_url,
            "verified_auth_mode": "bearer" if secret_path else "no-auth",
        }
