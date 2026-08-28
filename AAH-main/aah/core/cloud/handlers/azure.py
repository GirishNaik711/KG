#!/usr/bin/env python3
"""
Azure connectivity test handlers.

Uses azure-identity and service-specific SDKs (optional imports).
Each function receives a config dict, returns a result dict.
Never prints or persists credentials.
"""

import time


def _require_azure_identity():
    """Import DefaultAzureCredential or raise with install instructions."""
    try:
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential()
    except ImportError:
        raise ImportError(
            "azure-identity is required for Azure connectivity tests. "
            "Install: pip install azure-identity"
        )


def test_azure_postgres(config: dict, credentials: dict | None = None) -> dict:
    """Test Azure Database for PostgreSQL connectivity."""
    hostname = config.get("hostname", "")
    port = int(config.get("port", 5432))
    database = config.get("database", "postgres")
    auth_method = config.get("auth_method", "cloud-default")

    if not hostname:
        return {"passed": False, "latency_ms": 0, "error": "Missing hostname"}

    start = time.perf_counter()

    password = None
    if auth_method == "cloud-default":
        try:
            credential = _require_azure_identity()
            token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
            password = token.token
        except ImportError as e:
            return {"passed": False, "latency_ms": 0, "error": str(e)}
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"Azure AD token failed: {e}"}
    elif credentials:
        password = credentials.get("password")

    try:
        import psycopg2
        conn_kwargs = {
            "host": hostname,
            "port": port,
            "dbname": database,
            "sslmode": "require",
            "connect_timeout": 10,
        }
        if password:
            conn_kwargs["password"] = password
            conn_kwargs["user"] = config.get("username", hostname.split(".")[0])

        conn = psycopg2.connect(**conn_kwargs)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        elapsed = (time.perf_counter() - start) * 1000
        del password
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
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


def test_blob_storage(config: dict) -> dict:
    """Test Azure Blob Storage access."""
    account_url = config.get("account_url", "")
    container_name = config.get("container_name", "")

    if not account_url:
        return {"passed": False, "latency_ms": 0, "error": "Missing account_url"}

    start = time.perf_counter()
    try:
        credential = _require_azure_identity()
        from azure.storage.blob import BlobServiceClient

        client = BlobServiceClient(account_url=account_url, credential=credential)
        if container_name:
            container_client = client.get_container_client(container_name)
            container_client.get_container_properties()
        else:
            client.get_account_information()
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_msg = str(e)
        if "azure.storage.blob" in error_msg or "azure-storage-blob" in str(e.__class__):
            error_msg = "azure-storage-blob not installed. Install: pip install azure-storage-blob"
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": error_msg}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_key_vault(config: dict) -> dict:
    """Test Azure Key Vault access."""
    vault_url = config.get("vault_url", "")
    if not vault_url:
        return {"passed": False, "latency_ms": 0, "error": "Missing vault_url"}

    start = time.perf_counter()
    try:
        credential = _require_azure_identity()
        from azure.keyvault.secrets import SecretClient

        client = SecretClient(vault_url=vault_url, credential=credential)
        next(client.list_properties_of_secrets(max_page_size=1), None)
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_msg = str(e)
        if "keyvault" in error_msg.lower():
            error_msg = "azure-keyvault-secrets not installed. Install: pip install azure-keyvault-secrets"
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": error_msg}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_container_apps(config: dict) -> dict:
    """Test Azure Container Apps access."""
    resource_group = config.get("resource_group", "")
    subscription_id = config.get("subscription_id", "")

    if not resource_group or not subscription_id:
        return {"passed": False, "latency_ms": 0, "error": "Missing resource_group or subscription_id"}

    start = time.perf_counter()
    try:
        credential = _require_azure_identity()
        from azure.mgmt.appcontainers import ContainerAppsAPIClient

        client = ContainerAppsAPIClient(credential=credential, subscription_id=subscription_id)
        list(client.container_apps.list_by_resource_group(resource_group))
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_msg = str(e)
        if "appcontainers" in error_msg.lower():
            error_msg = "azure-mgmt-appcontainers not installed. Install: pip install azure-mgmt-appcontainers"
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": error_msg}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_azure_openai(config: dict) -> dict:
    """Test Azure OpenAI Service access."""
    endpoint = config.get("endpoint", "")
    deployment_name = config.get("deployment_name", "")

    if not endpoint:
        return {"passed": False, "latency_ms": 0, "error": "Missing endpoint"}

    start = time.perf_counter()
    try:
        credential = _require_azure_identity()
        from openai import AzureOpenAI

        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token=token.token,
            api_version="2024-02-01",
        )
        models = client.models.list()
        next(iter(models), None)
        elapsed = (time.perf_counter() - start) * 1000
        del token
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_msg = str(e)
        if "openai" in error_msg.lower():
            error_msg = "openai SDK not installed. Install: pip install openai"
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": error_msg}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_azure_functions(config: dict) -> dict:
    """Test Azure Functions access."""
    resource_group = config.get("resource_group", "")
    subscription_id = config.get("subscription_id", "")

    if not resource_group or not subscription_id:
        return {"passed": False, "latency_ms": 0, "error": "Missing resource_group or subscription_id"}

    start = time.perf_counter()
    try:
        credential = _require_azure_identity()
        from azure.mgmt.web import WebSiteManagementClient

        client = WebSiteManagementClient(credential=credential, subscription_id=subscription_id)
        list(client.web_apps.list_by_resource_group(resource_group))
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None}
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_msg = str(e)
        if "azure.mgmt.web" in error_msg:
            error_msg = "azure-mgmt-web not installed. Install: pip install azure-mgmt-web"
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": error_msg}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_azure_cache_redis(config: dict, credentials: dict | None = None) -> dict:
    """Test Azure Cache for Redis connectivity."""
    from aah.core.cloud.handlers.general import test_tcp_connect
    return test_tcp_connect(config)
