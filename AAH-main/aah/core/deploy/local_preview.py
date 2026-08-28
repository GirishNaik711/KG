#!/usr/bin/env python3
"""
Native process preview for the serverless deploy pipeline.

Launches services natively (no Docker required) in dependency order.
Each layer starts, gets healthchecked via localhost, then next layer starts.

Used as a pipeline gate before actual cloud deployment.

Usage:
    aah run core.deploy.local_preview start --project-path <path>
    aah run core.deploy.local_preview cleanup
"""

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

_SHELL = platform.system() == "Windows"


# ---------------------------------------------------------------------------
# Start command resolution
# ---------------------------------------------------------------------------

def resolve_start_command(service: dict, project_path: Path) -> list[str]:
    """
    Determine native start command from language/framework/entry point.

    Returns command list suitable for subprocess.Popen.
    """
    language = service.get("language", "unknown")
    framework = service.get("framework", "bare")
    entry_point = service.get("entry_point", "")
    port = service.get("port", 8080)
    service_path = project_path / service.get("path", ".")

    if language == "python":
        return _python_start_command(framework, entry_point, port, service_path)
    elif language == "node":
        return _node_start_command(framework, port, service_path)
    elif language == "go":
        return _go_start_command(entry_point, port, service_path)

    return []


def _python_start_command(framework: str, entry_point: str, port: int, service_path: Path) -> list[str]:
    """Resolve Python start command."""
    # Use 127.0.0.1 for local preview — 0.0.0.0 can be blocked by firewalls on Windows
    bind_host = "127.0.0.1"
    if framework in ("fastapi", "adk", "langgraph"):
        # Convert entry_point path to module: src/main.py → src.main
        module = entry_point.replace("/", ".").replace("\\", ".").removesuffix(".py")
        return [
            sys.executable, "-m", "uvicorn",
            f"{module}:app",
            "--host", bind_host,
            "--port", str(port),
        ]
    elif framework == "flask":
        module = entry_point.replace("/", ".").replace("\\", ".").removesuffix(".py")
        return [
            sys.executable, "-m", "gunicorn",
            f"{module}:app",
            "--bind", f"{bind_host}:{port}",
        ]
    elif framework == "django":
        return [
            sys.executable, "manage.py", "runserver", f"{bind_host}:{port}",
        ]
    else:
        # Generic: just run the entry point
        return [sys.executable, entry_point]


def _node_start_command(framework: str, port: int, service_path: Path) -> list[str]:
    """Resolve Node.js start command."""
    if framework == "nextjs":
        return ["npx", "next", "dev", "--port", str(port)]
    elif framework in ("vite", "vue", "svelte"):
        return ["npx", "vite", "--port", str(port)]
    elif framework == "angular":
        return ["npx", "ng", "serve", "--port", str(port)]
    else:
        # Check package.json for dev script
        pkg_json = service_path / "package.json"
        if pkg_json.exists():
            return ["npm", "run", "dev"]
        return ["node", "src/index.js"]


def _go_start_command(entry_point: str, port: int, service_path: Path) -> list[str]:
    """Resolve Go start command."""
    entry_dir = str(Path(entry_point).parent) if entry_point else "."
    return ["go", "run", f"./{entry_dir}"]


# ---------------------------------------------------------------------------
# Cloud env var injection (auto-detect from CLI config, no .env needed)
# ---------------------------------------------------------------------------

_cloud_env_cache: dict[str, str] | None = None


def _get_cloud_env_vars() -> dict[str, str]:
    """
    Auto-detect cloud env vars from gcloud/aws CLI config.

    Injects GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION (GCP) or
    AWS_REGION, AWS_ACCOUNT_ID (AWS) so services can call cloud APIs
    during local preview without needing a .env file.

    Cached after first call (CLI calls are slow).
    """
    global _cloud_env_cache
    if _cloud_env_cache is not None:
        return _cloud_env_cache

    env_vars: dict[str, str] = {}

    # Try GCP
    try:
        r = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, timeout=5, shell=_SHELL,
        )
        if r.returncode == 0 and r.stdout.strip():
            env_vars["GOOGLE_CLOUD_PROJECT"] = r.stdout.strip()
            env_vars["GCP_PROJECT"] = r.stdout.strip()
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["gcloud", "config", "get-value", "compute/region"],
            capture_output=True, text=True, timeout=5, shell=_SHELL,
        )
        if r.returncode == 0 and r.stdout.strip():
            env_vars["GOOGLE_CLOUD_LOCATION"] = r.stdout.strip()
        else:
            env_vars["GOOGLE_CLOUD_LOCATION"] = "us-central1"
    except Exception:
        env_vars["GOOGLE_CLOUD_LOCATION"] = "us-central1"

    # Try AWS
    try:
        r = subprocess.run(
            ["aws", "configure", "get", "region"],
            capture_output=True, text=True, timeout=5, shell=_SHELL,
        )
        if r.returncode == 0 and r.stdout.strip():
            env_vars["AWS_REGION"] = r.stdout.strip()
            env_vars["AWS_DEFAULT_REGION"] = r.stdout.strip()
    except Exception:
        pass

    _cloud_env_cache = env_vars
    return env_vars


# ---------------------------------------------------------------------------
# .env file loading (for local preview — cloud env vars like PROJECT_ID)
# ---------------------------------------------------------------------------

def _load_dotenv(directory: Path) -> dict[str, str]:
    """
    Load environment variables from .env file in a directory.

    Parses KEY=VALUE lines (ignores comments, empty lines, export prefix).
    This provides cloud config vars (GOOGLE_CLOUD_PROJECT, etc.) that are
    set via --set-env-vars in Cloud Run but needed locally for preview.
    """
    env_vars: dict[str, str] = {}
    env_file = directory / ".env"

    if not env_file.exists():
        # Also check .env.local
        env_file = directory / ".env.local"
        if not env_file.exists():
            return env_vars

    try:
        content = env_file.read_text(encoding="utf-8", errors="ignore")
        for line in content.splitlines():
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue
            # Remove optional 'export ' prefix
            if line.startswith("export "):
                line = line[7:]
            # Parse KEY=VALUE
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Remove surrounding quotes if present
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key:
                    env_vars[key] = value
    except (OSError, PermissionError):
        pass

    return env_vars


# ---------------------------------------------------------------------------
# Health check for local preview
# ---------------------------------------------------------------------------

def wait_for_healthy(url: str, timeout: int = 30, poll_interval: float = 1.0) -> bool:
    """
    Poll a local URL until it responds healthy or timeout.

    Tries /health first, falls back to / if /health gives 404.
    Returns True if service responds with any 2xx/3xx/404 within timeout.
    """
    import urllib.request
    import urllib.error

    start = time.time()
    endpoints = ["/health", "/"]

    while (time.time() - start) < timeout:
        for endpoint in endpoints:
            try:
                probe_url = url.rstrip("/") + endpoint
                req = urllib.request.Request(probe_url, method="GET")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status < 500:
                        return True
            except urllib.error.HTTPError as e:
                if e.code < 500:  # 404 is fine — service is up
                    return True
            except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError):
                pass

        time.sleep(poll_interval)

    return False


# ---------------------------------------------------------------------------
# Preview orchestration
# ---------------------------------------------------------------------------

def preview_services(
    services: list[dict],
    layers: list[list[str]],
    project_path: Path,
) -> dict:
    """
    Launch each service natively in dependency order.

    Each layer's services start, get healthchecked via localhost, then next layer.
    Inter-service env vars use localhost:{port}.

    Returns:
        {
            "healthy": [service_names...],
            "unhealthy": [{"name": str, "error": str}...],
            "processes": [{"name": str, "pid": int, "port": int}...],
        }
    """
    from aah.core.deploy.env_injector import compute_env_vars_for_layer

    # Build service lookup
    service_map = {s["name"]: s for s in services}
    deployed_urls: dict[str, str] = {}
    processes: list[dict] = []
    healthy: list[str] = []
    unhealthy: list[dict] = []

    for layer in layers:
        # Compute env vars for this layer (using localhost URLs from prior layers)
        env_vars = compute_env_vars_for_layer(layer, services, deployed_urls)

        for svc_name in layer:
            svc = service_map.get(svc_name)
            if not svc:
                continue

            port = svc["port"]
            service_path = project_path / svc.get("path", ".")

            # Build environment with:
            # 1. Cloud context (project ID, region — from gcloud/aws CLI config)
            # 2. .env file vars (if present)
            # 3. Inter-service URLs from env_injector
            env = os.environ.copy()
            env["PORT"] = str(port)

            # Inject cloud context so services can call cloud APIs locally
            env.update(_get_cloud_env_vars())

            # Load .env file from service path (if exists) for additional config
            env.update(_load_dotenv(service_path))
            # Also check project root .env
            if service_path != project_path:
                env.update(_load_dotenv(project_path))

            svc_env_vars = env_vars.get(svc_name, {})
            env.update(svc_env_vars)

            # Resolve start command
            cmd = resolve_start_command(svc, project_path)
            if not cmd:
                unhealthy.append({"name": svc_name, "error": "Could not resolve start command"})
                continue

            # Launch process
            try:
                # Use DEVNULL for stdout to avoid pipe buffer deadlocks on Windows.
                # stderr goes to a temp file so we can read it on failure.
                import tempfile
                stderr_file = tempfile.NamedTemporaryFile(
                    mode="w+", suffix=f"_{svc_name}.log", delete=False
                )
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(service_path),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    shell=_SHELL,
                )
                processes.append({"name": svc_name, "pid": proc.pid, "port": port, "process": proc, "_stderr_file": stderr_file})
            except Exception as e:
                unhealthy.append({"name": svc_name, "error": f"Failed to start: {e}"})
                continue

            # Wait for healthy (60s to allow heavy imports like google-cloud)
            url = f"http://localhost:{port}"
            if wait_for_healthy(url, timeout=60):
                healthy.append(svc_name)
                deployed_urls[svc_name] = url
            else:
                # Capture stderr from temp file for diagnosis
                stderr_output = ""
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                    stderr_file = next(
                        (p["_stderr_file"] for p in processes if p["name"] == svc_name), None
                    )
                    if stderr_file:
                        stderr_file.seek(0)
                        stderr_output = stderr_file.read(500)
                        stderr_file.close()
                except Exception:
                    pass
                unhealthy.append({
                    "name": svc_name,
                    "error": f"Health check failed after 60s. stderr: {stderr_output}",
                })

    # Return results (processes kept alive for cleanup later)
    return {
        "healthy": healthy,
        "unhealthy": unhealthy,
        "processes": [{"name": p["name"], "pid": p["pid"], "port": p["port"]} for p in processes],
    }


def cleanup_processes(processes: list[dict] | None = None, pids: list[int] | None = None) -> None:
    """
    Kill all spawned preview processes.

    Accepts either process dicts (from preview_services) or raw PIDs.
    """
    target_pids = pids or []
    if processes:
        target_pids = [p["pid"] for p in processes]

    for pid in target_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # Process already dead


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Native process preview for deploy pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="Start local preview of all services")
    start_p.add_argument("--project-path", type=Path, required=True)

    clean_p = sub.add_parser("cleanup", help="Kill all preview processes")
    clean_p.add_argument("--pids", type=str, default="[]", help="JSON list of PIDs to kill")

    args = parser.parse_args()

    if args.command == "start":
        from aah.core.deploy.service_discovery import discover_services
        from aah.core.deploy.service_graph import build_service_graph

        services = discover_services(args.project_path)
        layers = build_service_graph(services)
        result = preview_services(services, layers, args.project_path)

        # Don't include process objects in JSON output
        output = {
            "healthy": result["healthy"],
            "unhealthy": result["unhealthy"],
            "processes": result["processes"],
            "all_healthy": len(result["unhealthy"]) == 0,
        }
        json.dump(output, sys.stdout, indent=2)
        print()
        sys.exit(0 if output["all_healthy"] else 1)

    elif args.command == "cleanup":
        pids = json.loads(args.pids)
        cleanup_processes(pids=pids)
        print(json.dumps({"cleaned_up": len(pids)}))


if __name__ == "__main__":
    main()
