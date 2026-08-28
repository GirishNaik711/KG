#!/usr/bin/env python3
"""
GCP connectivity test handlers.

Uses google-cloud-* SDKs (optional imports). Each function receives a config
dict, returns a result dict. Never prints or persists credentials.

Most GCP handlers support two probe modes:
  1. Endpoint mode  -- caller supplies `service_url` (or `endpoint`).
                       The handler does an authenticated GET against that URL,
                       proving the deployed service is reachable + healthy.
  2. Control-plane  -- no endpoint supplied. The handler calls the GCP admin
                       API (e.g., ListServices) to prove the API is enabled
                       and IAM allows access. Useful pre-deployment.

Endpoint mode is preferred when an endpoint is available; control-plane
is the fallback. Both modes count as PASS independently.
"""

import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _resolve_project(config: dict) -> tuple[str, str]:
    """
    Resolve GCP project with no hardcoded default.
    Order: config.project_id -> config.project ->
    google.auth.default() -> `gcloud config get-value project`.
    Returns (project_id, source) — project_id is "" if nothing yields one.
    """
    if config.get("project_id"):
        return config["project_id"], "config"
    if config.get("project"):
        return config["project"], "config"
    try:
        from aah.core.cloud.identity import discover_gcp_project
        project, source = discover_gcp_project(config)
        return (project or ""), source
    except Exception:
        return "", "not-found"


def _require_google_auth():
    """Get default credentials or raise with install instructions."""
    try:
        import google.auth
        credentials, project = google.auth.default()
        return credentials, project
    except ImportError:
        raise ImportError(
            "google-auth is required for GCP connectivity tests. "
            "Install: pip install google-auth"
        )


def _fetch_id_token(audience: str) -> str | None:
    """Fetch an ID token for the audience using ADC. Returns None on failure."""
    try:
        import google.auth
        from google.auth.transport.requests import Request as GAuthRequest
        from google.oauth2 import id_token as gid_token

        try:
            return gid_token.fetch_id_token(GAuthRequest(), audience)
        except Exception:
            credentials, _ = google.auth.default()
            credentials.refresh(GAuthRequest())
            return getattr(credentials, "id_token", None) or getattr(credentials, "token", None)
    except Exception:
        return None


def _probe_http_endpoint(url: str, path: str = "", send_id_token: bool = True, timeout: int = 15) -> dict:
    """
    Probe an HTTPS endpoint with optional GCP ID-token auth.
    Tries token-auth first; falls back to anonymous on token failure.
    Accepts 200..399 as healthy. Returns {passed, latency_ms, error, status, mode}.
    """
    if not url.startswith("http"):
        url = f"https://{url}"
    if path:
        url = url.rstrip("/") + "/" + path.lstrip("/")

    audience = url.split("?")[0].split("#")[0]
    start = time.perf_counter()

    def _do_request(headers: dict) -> tuple[int, str | None]:
        req = Request(url, method="GET", headers=headers)
        try:
            resp = urlopen(req, timeout=timeout)
            return resp.getcode(), None
        except HTTPError as e:
            return e.code, str(e.reason)
        except URLError as e:
            return 0, str(e.reason)
        except Exception as e:
            return 0, str(e)

    headers = {"User-Agent": "aah-cloud-readiness/1.0"}
    mode = "anonymous"
    if send_id_token:
        token = _fetch_id_token(audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            mode = "id-token"

    status, err = _do_request(headers)

    if mode == "id-token" and status in (401, 403):
        anon_status, anon_err = _do_request({"User-Agent": "aah-cloud-readiness/1.0"})
        if 200 <= anon_status < 400:
            status, err, mode = anon_status, None, "anonymous-fallback"

    elapsed = (time.perf_counter() - start) * 1000

    if 200 <= status < 400:
        return {"passed": True, "latency_ms": round(elapsed, 1), "error": None, "status": status, "mode": mode}

    detail = f"HTTP {status}" if status else "no response"
    if err:
        detail = f"{detail}: {err}"
    return {"passed": False, "latency_ms": round(elapsed, 1), "error": detail, "status": status, "mode": mode}


def test_gcs(config: dict) -> dict:
    """Test Google Cloud Storage bucket access."""
    bucket_name = config.get("bucket_name", "")
    if not bucket_name:
        return {"passed": False, "latency_ms": 0, "error": "Missing bucket_name"}

    start = time.perf_counter()
    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.get_bucket(bucket_name)
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "get-bucket",
            "verified_bucket_name": bucket.name,
            "verified_bucket_location": getattr(bucket, "location", "") or "",
            "verified_storage_class": getattr(bucket, "storage_class", "") or "",
            "verified_project_number": str(getattr(bucket, "project_number", "") or ""),
            "verified_created_time": str(getattr(bucket, "time_created", "") or ""),
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-cloud-storage not installed. Install: pip install google-cloud-storage",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_cloud_sql(config: dict, credentials: dict | None = None) -> dict:
    """
    Test Cloud SQL PostgreSQL connectivity via Cloud SQL Connector.

    Auth modes (driven by config['auth_method']):
      - 'iam'             -- IAM-bound DB user. enable_iam_auth=True. No password.
      - 'secret-manager'  -- Password-based. Password fetched from GCP Secret Manager
                             via secret_path; enable_iam_auth=False.
      - default           -- IAM (preserves prior behavior).
    """
    connection_name = config.get("connection_name", "")
    database = config.get("database", "postgres")
    username = config.get("username", "")
    auth_method = config.get("auth_method", "iam")
    secret_path = config.get("secret_path", "")

    if not connection_name:
        return {"passed": False, "latency_ms": 0, "error": "Missing connection_name"}

    password = None
    if auth_method == "secret-manager":
        if not secret_path:
            return {
                "passed": False,
                "latency_ms": 0,
                "error": "auth_method=secret-manager requires secret_path",
            }
        try:
            from google.cloud import secretmanager
            sm_client = secretmanager.SecretManagerServiceClient()
            resp = sm_client.access_secret_version(request={"name": secret_path})
            payload = resp.payload.data.decode("utf-8")
            try:
                import json as _json
                blob = _json.loads(payload)
                if isinstance(blob, dict):
                    password = blob.get("password") or blob.get("PASSWORD") or payload
                else:
                    password = payload
            except Exception:
                password = payload
        except ImportError:
            return {
                "passed": False,
                "latency_ms": 0,
                "error": "google-cloud-secret-manager not installed. Install: pip install google-cloud-secret-manager",
            }
        except Exception as e:
            return {
                "passed": False,
                "latency_ms": 0,
                "error": f"Secret Manager fetch failed: {e}",
            }

    start = time.perf_counter()
    try:
        from google.cloud.sql.connector import Connector
        import pg8000  # noqa: F401

        connector = Connector()
        connect_kwargs = {
            "user": username,
            "db": database,
        }
        if auth_method == "secret-manager":
            connect_kwargs["password"] = password
            connect_kwargs["enable_iam_auth"] = False
        else:
            connect_kwargs["enable_iam_auth"] = True

        conn = connector.connect(connection_name, "pg8000", **connect_kwargs)
        cursor = conn.cursor()
        cursor.execute("SELECT version(), current_database(), current_user, inet_server_addr()::text")
        row = cursor.fetchone() or (None, None, None, None)
        server_version, current_db, current_user, server_addr = row
        cursor.close()
        conn.close()
        connector.close()
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "iam-auth" if auth_method != "secret-manager" else "password-via-secret-manager",
            "verified_connection_name": connection_name,
            "verified_database": str(current_db) if current_db else database,
            "verified_user": str(current_user) if current_user else username,
            "verified_server_version": str(server_version)[:80] if server_version else "",
            "verified_server_addr": str(server_addr) if server_addr else "",
        }
    except ImportError as e:
        elapsed = (time.perf_counter() - start) * 1000
        error_msg = str(e)
        if "cloud-sql" in error_msg.lower() or "connector" in error_msg.lower():
            error_msg = "cloud-sql-python-connector not installed. Install: pip install cloud-sql-python-connector[pg8000]"
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": error_msg}
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_secret_manager(config: dict) -> dict:
    """Test GCP Secret Manager access."""
    project_id = config.get("project_id", "")
    if not project_id:
        return {"passed": False, "latency_ms": 0, "error": "Missing project_id"}

    start = time.perf_counter()
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{project_id}"
        pager = client.list_secrets(request={"parent": parent, "page_size": 1})
        first_secret_name = ""
        sample_count = 0
        for s in pager:
            if not first_secret_name:
                first_secret_name = getattr(s, "name", "").rsplit("/", 1)[-1]
            sample_count += 1
            if sample_count >= 1:
                break
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-secrets",
            "verified_project_id": project_id,
            "verified_at_least_one_secret": sample_count > 0,
            "verified_sample_secret": first_secret_name or None,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-cloud-secret-manager not installed. Install: pip install google-cloud-secret-manager",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_gke(config: dict) -> dict:
    """
    Test GKE access. Three modes:
      1. ingress_url   -- HTTPS GET against an ingress URL hosted on the cluster.
      2. cluster_name  -- GetCluster confirms a specific cluster exists.
      3. control-plane -- ListClusters in project_id+location.
    """
    project_id = config.get("project_id", "")
    cluster_name = config.get("cluster_name", "")
    location = config.get("location", "")
    ingress_url = (config.get("ingress_url") or config.get("service_url") or "").strip()
    health_path = config.get("health_path", "")

    if ingress_url:
        result = _probe_http_endpoint(ingress_url, path=health_path, send_id_token=False)
        result["probe_mode"] = "endpoint"
        return result

    if not project_id or not location:
        return {"passed": False, "latency_ms": 0, "error": "Missing project_id or location"}

    start = time.perf_counter()
    try:
        from google.cloud import container_v1
        client = container_v1.ClusterManagerClient()

        if cluster_name:
            name = f"projects/{project_id}/locations/{location}/clusters/{cluster_name}"
            cluster = client.get_cluster(name=name)
            elapsed = (time.perf_counter() - start) * 1000
            status_enum = getattr(cluster, "status", None)
            status_label = ""
            try:
                status_label = status_enum.name if hasattr(status_enum, "name") else str(status_enum)
            except Exception:
                status_label = str(status_enum)
            node_pool_count = len(list(getattr(cluster, "node_pools", []) or []))
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "get-cluster",
                "verified_cluster_name": cluster_name,
                "verified_project_id": project_id,
                "verified_location": location,
                "verified_cluster_endpoint": getattr(cluster, "endpoint", "") or "",
                "verified_cluster_status": status_label,
                "verified_master_version": getattr(cluster, "current_master_version", "") or "",
                "verified_node_count": getattr(cluster, "current_node_count", 0) or 0,
                "verified_node_pool_count": node_pool_count,
                "verified_network": getattr(cluster, "network", "") or "",
                "verified_subnetwork": getattr(cluster, "subnetwork", "") or "",
            }
        else:
            parent = f"projects/{project_id}/locations/{location}"
            list_resp = client.list_clusters(parent=parent)
            sample_clusters = [c.name for c in (getattr(list_resp, "clusters", []) or [])][:5]
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-clusters",
            "verified_project_id": project_id,
            "verified_location": location,
            "verified_cluster_count": len(sample_clusters),
            "verified_sample_clusters": sample_clusters,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-cloud-container not installed. Install: pip install google-cloud-container",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_vertex_ai(config: dict) -> dict:
    """
    Test Vertex AI access. Probe modes (priority order):
      1. model_id     -- Invoke the model via generateContent with a tiny prompt.
                         Strongest signal: proves the project+location+identity
                         can actually call the model. Works for Gemini publisher
                         models and tuned models.
      2. endpoint_url -- HTTPS GET against a Vertex AI Endpoint URL. Useful when
                         the URL is a deployed Endpoint that responds to GET
                         (e.g., :predict endpoints with health probe).
      3. control-plane -- aiplatform.Model.list(); proves API + IAM only.
    """
    project_id, _ = _resolve_project(config)
    location = (config.get("location") or "").strip()
    if not location:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Vertex AI requires 'location' in config (e.g., us-central1).",
        }
    endpoint_url = (config.get("endpoint_url") or config.get("service_url") or "").strip()
    health_path = config.get("health_path", "")
    model_id = (config.get("model_id") or "").strip()

    if model_id:
        start = time.perf_counter()
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(vertexai=True, project=project_id, location=location)
            resp = client.models.generate_content(
                model=model_id,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=4, temperature=0.0),
            )
            elapsed = (time.perf_counter() - start) * 1000
            text = getattr(resp, "text", None) or ""
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "invoke-model",
                "verified_model_id": model_id,
                "verified_response_chars": len(text),
            }
        except ImportError:
            try:
                from vertexai.generative_models import GenerativeModel
                import vertexai as _vai
                _vai.init(project=project_id, location=location)
                model = GenerativeModel(model_id)
                resp = model.generate_content(
                    "ping",
                    generation_config={"max_output_tokens": 4, "temperature": 0.0},
                )
                elapsed = (time.perf_counter() - start) * 1000
                text = getattr(resp, "text", None) or ""
                return {
                    "passed": True,
                    "latency_ms": round(elapsed, 1),
                    "error": None,
                    "probe_mode": "invoke-model-vertexai",
                    "verified_model_id": model_id,
                    "verified_response_chars": len(text),
                }
            except ImportError:
                elapsed = (time.perf_counter() - start) * 1000
                return {
                    "passed": False,
                    "latency_ms": round(elapsed, 1),
                    "error": "Neither google-genai nor vertexai is installed. Install: pip install google-genai",
                }
            except Exception as e:
                elapsed = (time.perf_counter() - start) * 1000
                return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"vertexai invoke failed: {e}"}
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return {"passed": False, "latency_ms": round(elapsed, 1), "error": f"genai invoke failed: {e}"}

    if endpoint_url:
        result = _probe_http_endpoint(endpoint_url, path=health_path, send_id_token=True)
        result["probe_mode"] = "endpoint"
        return result

    if not project_id:
        return {"passed": False, "latency_ms": 0, "error": "Missing project_id"}

    start = time.perf_counter()
    try:
        from google.cloud import aiplatform
        aiplatform.init(project=project_id, location=location)
        models = aiplatform.Model.list(filter="", order_by="create_time desc")
        sample = []
        try:
            for m in list(models)[:3]:
                sample.append(getattr(m, "display_name", "") or getattr(m, "resource_name", "").rsplit("/", 1)[-1])
        except Exception:
            pass
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-models",
            "verified_project_id": project_id,
            "verified_location": location,
            "verified_user_models_sampled": sample,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-cloud-aiplatform not installed. Install: pip install google-cloud-aiplatform",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_model_armor(config: dict) -> dict:
    """
    Test GCP Model Armor (LLM safety / guardrails) access. Probe modes:
      1. template_id    -- GetTemplate against a specific Model Armor template.
                           Strongest signal: template exists with expected filters.
      2. control-plane  -- ListTemplates in project_id+location. Validates API +
                           IAM (modelarmor.templates.list).

    Model Armor is a distinct API from Vertex AI: hostname modelarmor.googleapis.com.
    Requires the modelarmor.googleapis.com service to be enabled in the project.
    """
    project_id, _ = _resolve_project(config)
    location = (config.get("location") or "").strip()
    if not location:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Model Armor requires 'location' in config (e.g., us-central1 or 'global').",
        }
    template_id = (config.get("template_id") or "").strip()

    if not project_id:
        return {"passed": False, "latency_ms": 0, "error": "Missing project_id"}

    parent = f"projects/{project_id}/locations/{location}"
    api_root = f"https://modelarmor.{location}.rep.googleapis.com" if location != "global" else "https://modelarmor.googleapis.com"

    if template_id:
        url = f"{api_root}/v1/{parent}/templates/{template_id}"
    else:
        url = f"{api_root}/v1/{parent}/templates?pageSize=1"

    start = time.perf_counter()
    try:
        from google.auth.transport.requests import Request as GAuthRequest
        import google.auth

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(GAuthRequest())

        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "User-Agent": "aah-cloud-readiness/1.0",
        }
        req = Request(url, method="GET", headers=headers)
        try:
            resp = urlopen(req, timeout=15)
            status = resp.getcode()
        except HTTPError as e:
            status = e.code
            elapsed = (time.perf_counter() - start) * 1000
            if status == 403:
                return {
                    "passed": False,
                    "latency_ms": round(elapsed, 1),
                    "error": "HTTP 403: modelarmor.googleapis.com API not enabled or missing IAM (modelarmor.templates.list). Enable: gcloud services enable modelarmor.googleapis.com",
                    "probe_mode": "list-templates" if not template_id else "get-template",
                }
            if status == 404 and template_id:
                return {
                    "passed": False,
                    "latency_ms": round(elapsed, 1),
                    "error": f"HTTP 404: Template '{template_id}' not found in {parent}",
                    "probe_mode": "get-template",
                }
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"HTTP {status}: {e.reason}",
                "probe_mode": "list-templates" if not template_id else "get-template",
            }
        except URLError as e:
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": False,
                "latency_ms": round(elapsed, 1),
                "error": f"Network error: {e.reason}",
            }

        elapsed = (time.perf_counter() - start) * 1000
        if 200 <= status < 400:
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "get-template" if template_id else "list-templates",
                "verified_template_id": template_id or None,
            }
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": f"HTTP {status}",
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-auth not installed. Install: pip install google-auth",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_cloud_run(config: dict) -> dict:
    """
    Test Cloud Run access. Three modes (in priority order):
      1. service_url       -- HTTPS GET against the deployed Cloud Run URL.
                              Optional health_path overrides the default '/'.
      2. service_name      -- GetService against a specific Cloud Run service.
                              Confirms the deployment exists with traffic config.
      3. control-plane     -- ListServices in project_id+region. Validates API
                              + IAM. Used when no endpoint or service name given.
    """
    project_id = config.get("project_id", "")
    region = config.get("region", "")
    service_url = (config.get("service_url") or "").strip()
    service_name = (config.get("service_name") or "").strip()
    health_path = config.get("health_path", "")

    if service_url:
        result = _probe_http_endpoint(service_url, path=health_path, send_id_token=True)
        result["probe_mode"] = "endpoint"
        return result

    if not project_id or not region:
        return {"passed": False, "latency_ms": 0, "error": "Missing project_id or region"}

    start = time.perf_counter()
    try:
        from google.cloud.run_v2 import ServicesClient
        client = ServicesClient()

        if service_name:
            name = f"projects/{project_id}/locations/{region}/services/{service_name}"
            svc = client.get_service(name=name)
            elapsed = (time.perf_counter() - start) * 1000
            uri = getattr(svc, "uri", "") or ""
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "get-service",
                "verified_service_uri": uri,
            }

        parent = f"projects/{project_id}/locations/{region}"
        pager = client.list_services(parent=parent)
        first_name = ""
        for s in pager:
            first_name = getattr(s, "name", "").rsplit("/", 1)[-1]
            break
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-services",
            "verified_project_id": project_id,
            "verified_region": region,
            "verified_sample_service": first_name or None,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-cloud-run not installed. Install: pip install google-cloud-run",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_cloud_run_jobs(config: dict) -> dict:
    """
    Test Cloud Run Jobs (run-to-completion containers). Two modes:
      1. job_name       -- GetJob against a specific Cloud Run Job (control plane).
                           Use with execute_job=true to actually run it (data plane).
      2. control-plane  -- ListJobs in project_id+region (default).
    Distinct from Cloud Run Services (test_cloud_run) — uses run_v2.JobsClient.
    """
    project_id, _ = _resolve_project(config)
    region = (config.get("region") or config.get("location") or "").strip()
    job_name = (config.get("job_name") or "").strip()
    execute_job = bool(config.get("execute_job", False))

    if not project_id or not region:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing project_id or region for Cloud Run Jobs",
        }

    start = time.perf_counter()
    try:
        from google.cloud.run_v2 import JobsClient
        client = JobsClient()

        if job_name:
            name = f"projects/{project_id}/locations/{region}/jobs/{job_name}"
            job = client.get_job(name=name)
            elapsed = (time.perf_counter() - start) * 1000
            result = {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "get-job",
                "verified_job_name": job_name,
                "verified_project_id": project_id,
                "verified_region": region,
                "verified_create_time": str(getattr(job, "create_time", "") or ""),
            }
            if execute_job:
                try:
                    op = client.run_job(name=name)
                    op_meta = getattr(op, "metadata", None)
                    result["probe_mode"] = "run-job"
                    result["verified_execution_started"] = True
                    result["verified_operation_name"] = str(getattr(op_meta, "name", "") or "")
                except Exception as e:
                    result["execution_error"] = str(e)
                    result["verified_execution_started"] = False
            return result

        parent = f"projects/{project_id}/locations/{region}"
        pager = client.list_jobs(parent=parent)
        first_name = ""
        count = 0
        for j in pager:
            if not first_name:
                first_name = getattr(j, "name", "").rsplit("/", 1)[-1]
            count += 1
            if count >= 5:
                break
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-jobs",
            "verified_project_id": project_id,
            "verified_region": region,
            "verified_job_count": count,
            "verified_sample_job": first_name or None,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False, "latency_ms": round(elapsed, 1),
            "error": "google-cloud-run not installed. Install: pip install google-cloud-run",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_cloud_functions(config: dict) -> dict:
    """
    Test Cloud Functions (1st and 2nd gen). Three modes:
      1. function_url   -- HTTPS GET against the trigger URL (data plane).
      2. function_name  -- GetFunction (control plane).
      3. control-plane  -- ListFunctions (default).
    Uses Cloud Functions API v2 (covers both 1st-gen and 2nd-gen).
    """
    project_id, _ = _resolve_project(config)
    region = (config.get("region") or config.get("location") or "").strip()
    function_name = (config.get("function_name") or "").strip()
    function_url = (config.get("function_url") or config.get("service_url") or "").strip()
    health_path = config.get("health_path", "")

    if function_url:
        result = _probe_http_endpoint(function_url, path=health_path, send_id_token=True)
        result["probe_mode"] = "endpoint"
        return result

    if not project_id or not region:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing project_id or region for Cloud Functions",
        }

    start = time.perf_counter()
    try:
        from google.cloud import functions_v2
        client = functions_v2.FunctionServiceClient()

        if function_name:
            name = f"projects/{project_id}/locations/{region}/functions/{function_name}"
            fn = client.get_function(name=name)
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "get-function",
                "verified_function_name": function_name,
                "verified_state": str(getattr(fn, "state", "") or ""),
                "verified_environment": str(getattr(fn, "environment", "") or ""),
            }

        parent = f"projects/{project_id}/locations/{region}"
        pager = client.list_functions(parent=parent)
        first_name = ""
        count = 0
        for f in pager:
            if not first_name:
                first_name = getattr(f, "name", "").rsplit("/", 1)[-1]
            count += 1
            if count >= 5:
                break
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-functions",
            "verified_project_id": project_id,
            "verified_region": region,
            "verified_function_count": count,
            "verified_sample_function": first_name or None,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False, "latency_ms": round(elapsed, 1),
            "error": "google-cloud-functions not installed. Install: pip install google-cloud-functions",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_compute_engine(config: dict) -> dict:
    """
    Test Compute Engine (GCE) access. Three modes:
      1. instance_url   -- HTTPS GET against a public-IP / load-balancer URL (data plane).
      2. instance_name  -- Get a specific instance (control plane).
      3. control-plane  -- AggregatedList of instances in project (default).
    """
    project_id, _ = _resolve_project(config)
    zone = (config.get("zone") or "").strip()
    instance_name = (config.get("instance_name") or "").strip()
    instance_url = (config.get("instance_url") or config.get("service_url") or "").strip()
    health_path = config.get("health_path", "")

    if instance_url:
        result = _probe_http_endpoint(instance_url, path=health_path, send_id_token=False)
        result["probe_mode"] = "endpoint"
        return result

    if not project_id:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing project_id for Compute Engine",
        }

    start = time.perf_counter()
    try:
        from google.cloud import compute_v1
        client = compute_v1.InstancesClient()

        if instance_name and zone:
            inst = client.get(project=project_id, zone=zone, instance=instance_name)
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "passed": True,
                "latency_ms": round(elapsed, 1),
                "error": None,
                "probe_mode": "get-instance",
                "verified_instance_name": instance_name,
                "verified_zone": zone,
                "verified_status": getattr(inst, "status", "") or "",
                "verified_machine_type": (getattr(inst, "machine_type", "") or "").rsplit("/", 1)[-1],
            }

        request = compute_v1.AggregatedListInstancesRequest(project=project_id, max_results=20)
        zones_seen = []
        instance_count = 0
        for zone_key, zone_payload in client.aggregated_list(request=request):
            zone_instances = list(getattr(zone_payload, "instances", []) or [])
            if zone_instances:
                zones_seen.append(zone_key.rsplit("/", 1)[-1])
                instance_count += len(zone_instances)
            if instance_count >= 20:
                break
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "aggregated-list",
            "verified_project_id": project_id,
            "verified_instance_count": instance_count,
            "verified_zones_with_instances": zones_seen[:5],
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False, "latency_ms": round(elapsed, 1),
            "error": "google-cloud-compute not installed. Install: pip install google-cloud-compute",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def test_cloud_logging(config: dict) -> dict:
    """
    Test Cloud Logging / Cloud Operations access. Two modes:
      1. dashboard_url -- HTTPS GET against a custom log viewer / Grafana /
                          Looker Studio etc. that fronts Cloud Logging.
      2. control-plane -- list-entries via the Cloud Logging API.
    """
    project_id = config.get("project_id", "")
    dashboard_url = (config.get("dashboard_url") or config.get("service_url") or "").strip()
    health_path = config.get("health_path", "")

    if dashboard_url:
        result = _probe_http_endpoint(dashboard_url, path=health_path, send_id_token=True)
        result["probe_mode"] = "endpoint"
        return result

    if not project_id:
        return {"passed": False, "latency_ms": 0, "error": "Missing project_id"}

    start = time.perf_counter()
    try:
        from google.cloud import logging as cloud_logging
        client = cloud_logging.Client(project=project_id)
        sample_log = ""
        sample_count = 0
        for entry in client.list_entries(max_results=1):
            sample_log = getattr(entry, "log_name", "") or ""
            sample_count += 1
            break
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": True,
            "latency_ms": round(elapsed, 1),
            "error": None,
            "probe_mode": "list-entries",
            "verified_project_id": project_id,
            "verified_at_least_one_entry": sample_count > 0,
            "verified_sample_log_name": sample_log or None,
        }
    except ImportError:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False,
            "latency_ms": round(elapsed, 1),
            "error": "google-cloud-logging not installed. Install: pip install google-cloud-logging",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}
