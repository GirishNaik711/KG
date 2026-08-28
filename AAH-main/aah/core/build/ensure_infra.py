#!/usr/bin/env python3
"""Pre-test infrastructure check — ensures required services are running locally.

Before running any tests, this module:
1. Reads the project's infrastructure requirements from manifest/init.sh/docker-compose
2. Checks if required services (DB, cache, etc.) are reachable
3. If not running, attempts to start them via docker compose or docker run
4. Waits for readiness before returning

Usage (as a module):
    from aah.core.build.ensure_infra import ensure_infrastructure
    result = ensure_infrastructure(project_path)
    if not result["ready"]:
        # infrastructure could not be started

Usage (CLI):
    aah run core.build.ensure_infra check --project-path .
    aah run core.build.ensure_infra start --project-path .
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aah.core.common.io_utils import read_json, read_yaml


# Service types that represent real, blocking infrastructure. A DECLARED
# service of one of these types is `required` by default (its absence is a
# no_signal, not a soft warning). An "unknown"-type service is never required
# on its own — we cannot health-check or provision it deterministically.
_REQUIRED_BY_DEFAULT_TYPES = frozenset(
    {"postgres", "redis", "mongodb", "mysql", "rabbitmq", "kafka", "elasticsearch"}
)


def _service_is_required(svc: dict) -> bool:
    """Decide whether a declared service is required (fail-closed blocking).

    An explicit ``required`` field on the service wins. Otherwise a known DB /
    broker / search type defaults to required; an unknown type defaults to
    NOT required (its unavailability is not_applicable, never a block).
    """
    if isinstance(svc, dict) and "required" in svc:
        return bool(svc.get("required"))
    if svc.get("source") == "env":
        return False
    return svc.get("type") in _REQUIRED_BY_DEFAULT_TYPES


# ---------------------------------------------------------------------------
# Service detection
# ---------------------------------------------------------------------------


def _detect_required_services(project_path: Path) -> list[dict]:
    """Detect infrastructure services needed by the project.

    Checks (in order):
    1. .aah/infra-services.yaml (explicit declaration)
    2. docker-compose.yaml/yml service definitions
    3. .env / manifest hints (DATABASE_URL, REDIS_URL, etc.)
    """
    services = []
    aah_path = project_path / ".aah"

    # 1. Explicit declaration
    infra_file = aah_path / "infra-services.yaml"
    if infra_file.exists():
        data = read_yaml(infra_file)
        return data.get("services", [])

    # 2. Docker compose services
    compose_path = _find_compose_file(project_path)
    if compose_path:
        try:
            compose_data = read_yaml(compose_path)
            for svc_name, svc_def in compose_data.get("services", {}).items():
                image = svc_def.get("image", "")
                svc = {
                    "name": svc_name,
                    "image": image,
                    "type": _classify_service(svc_name, image),
                    "source": "docker-compose",
                }
                # Extract port mapping
                ports = svc_def.get("ports", [])
                if ports:
                    port_str = str(ports[0])
                    host_port = port_str.split(":")[0] if ":" in port_str else port_str
                    svc["port"] = int(host_port)
                # Extract healthcheck
                if svc_def.get("healthcheck"):
                    svc["has_healthcheck"] = True
                services.append(svc)
        except Exception:
            pass
        return services

    # 3. Infer from .env files (including test env)
    env_file = project_path / ".env"
    if not env_file.exists():
        env_file = project_path / ".env.test"
    if not env_file.exists():
        env_file = project_path / ".env.example"

    if env_file.exists():
        try:
            env_content = env_file.read_text(encoding='utf-8')
            if "POSTGRES" in env_content or "DATABASE_URL" in env_content:
                port = _extract_port_from_env(env_content, "POSTGRES_PORT", 5432)
                services.append({
                    "name": "postgres",
                    "type": "postgres",
                    "port": port,
                    "source": "env",
                })
            if "REDIS" in env_content:
                port = _extract_port_from_env(env_content, "REDIS_PORT", 6379)
                services.append({
                    "name": "redis",
                    "type": "redis",
                    "port": port,
                    "source": "env",
                })
            if "MONGO" in env_content:
                port = _extract_port_from_env(env_content, "MONGO_PORT", 27017)
                services.append({
                    "name": "mongodb",
                    "type": "mongodb",
                    "port": port,
                    "source": "env",
                })
        except Exception:
            pass

    # No fallback inference — only require services that are explicitly declared
    # via infra-services.yaml, docker-compose.yml, or .env with DB URLs.

    return services


def _classify_service(name: str, image: str) -> str:
    """Classify a service by its type."""
    combined = f"{name} {image}".lower()
    if "postgres" in combined or "pgvector" in combined:
        return "postgres"
    if "redis" in combined:
        return "redis"
    if "mongo" in combined:
        return "mongodb"
    if "mysql" in combined or "mariadb" in combined:
        return "mysql"
    if "rabbit" in combined:
        return "rabbitmq"
    if "kafka" in combined:
        return "kafka"
    if "elastic" in combined:
        return "elasticsearch"
    return "unknown"


def _extract_port_from_env(content: str, key: str, default: int) -> int:
    """Extract a port number from .env content."""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith(key + "="):
            try:
                return int(line.split("=", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    return default


def _find_compose_file(project_path: Path) -> Path | None:
    """Find docker-compose file in the project (includes test compose files)."""
    candidates = [
        project_path / "docker-compose.yaml",
        project_path / "docker-compose.yml",
        project_path / "docker-compose.test.yaml",
        project_path / "docker-compose.test.yml",
        project_path / "compose.yaml",
        project_path / "compose.yml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _find_test_compose_file(project_path: Path) -> Path | None:
    """Find test-specific docker-compose file (preferred for test environments)."""
    test_candidates = [
        project_path / "docker-compose.test.yaml",
        project_path / "docker-compose.test.yml",
    ]
    for p in test_candidates:
        if p.exists():
            return p
    # Fall back to general compose
    return _find_compose_file(project_path)


# ---------------------------------------------------------------------------
# Health checks per service type
# ---------------------------------------------------------------------------


def _check_service_health(service: dict) -> bool:
    """Check if a specific service is reachable."""
    svc_type = service.get("type", "unknown")
    port = service.get("port")

    if svc_type == "postgres":
        return _check_postgres(port or 5432)
    elif svc_type == "redis":
        return _check_redis(port or 6379)
    elif svc_type == "mongodb":
        return _check_port_open(port or 27017)
    elif svc_type == "mysql":
        return _check_port_open(port or 3306)
    else:
        if port:
            return _check_port_open(port)
        return True  # Can't check unknown services without a port


def _check_postgres(port: int) -> bool:
    """Check if PostgreSQL is responding."""
    # Try pg_isready first
    if shutil.which("pg_isready"):
        try:
            result = subprocess.run(
                ["pg_isready", "-h", "localhost", "-p", str(port)],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            pass

    # Fallback: TCP port check
    return _check_port_open(port)


def _check_redis(port: int) -> bool:
    """Check if Redis is responding."""
    if shutil.which("redis-cli"):
        try:
            result = subprocess.run(
                ["redis-cli", "-h", "localhost", "-p", str(port), "ping"],
                capture_output=True,
                timeout=5,
                text=True,
            )
            return "PONG" in result.stdout
        except (subprocess.TimeoutExpired, Exception):
            pass

    return _check_port_open(port)


def _check_port_open(port: int) -> bool:
    """TCP port check using Python socket."""
    import socket
    try:
        with socket.create_connection(("localhost", port), timeout=3):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


# ---------------------------------------------------------------------------
# Docker installation
# ---------------------------------------------------------------------------


def _is_docker_available() -> bool:
    """Check if docker CLI is available and the daemon is responsive."""
    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _ensure_passwordless_sudo() -> None:
    """Configure passwordless sudo for the current user.

    In WSL2, uses wsl.exe -u root (no password needed) to write sudoers config.
    In regular Linux, checks if already configured — if not, this is a no-op
    (can't configure without existing root access).
    """
    import os

    # Already have passwordless sudo? Nothing to do.
    try:
        check = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=5,
        )
        if check.returncode == 0:
            return
    except Exception:
        pass

    username = os.environ.get("USER", "")
    if not username:
        return

    # Detect WSL environment
    is_wsl = False
    try:
        with open("/proc/version", 'r', encoding='utf-8') as f:
            if "microsoft" in f.read().lower():
                is_wsl = True
    except Exception:
        pass

    if is_wsl:
        # In WSL2: wsl.exe -u root can run commands as root without password
        wsl_exe = shutil.which("wsl.exe") or "/mnt/c/Windows/System32/wsl.exe"
        sudoers_line = f"{username} ALL=(ALL) NOPASSWD:ALL"
        sudoers_file = f"/etc/sudoers.d/{username}"

        try:
            subprocess.run(
                [wsl_exe, "-u", "root", "bash", "-c",
                 f"echo '{sudoers_line}' > {sudoers_file} && chmod 440 {sudoers_file}"],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    # For non-WSL Linux: can't configure without root — caller will handle the error


def _install_docker() -> dict:
    """Attempt to install Docker Engine on the current system.

    Supports:
    - Ubuntu/Debian-based Linux and WSL2
    - Uses apt-get for package installation
    - Starts the Docker daemon after install

    Returns:
        {"installed": bool, "message": str, "steps": [...]}
    """
    import platform

    result = {"installed": False, "message": "", "steps": []}

    # Only attempt on Linux (includes WSL)
    if platform.system() != "Linux":
        result["message"] = f"Auto-install not supported on {platform.system()}. Install Docker manually."
        return result

    # Check if apt-get is available (Debian/Ubuntu)
    if not shutil.which("apt-get"):
        result["message"] = "apt-get not found. Install Docker manually for your distribution."
        return result

    # Ensure passwordless sudo is configured
    _ensure_passwordless_sudo()

    # Verify sudo works without password
    try:
        sudo_check = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            timeout=5,
        )
        if sudo_check.returncode != 0:
            result["message"] = "Cannot obtain sudo access. Run manually: sudo echo ok"
            return result
    except Exception:
        result["message"] = "sudo command not available"
        return result

    steps = []

    # Step 1: Update apt cache
    steps.append("Updating package index")
    try:
        proc = subprocess.run(
            ["sudo", "apt-get", "update", "-qq"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            result["message"] = f"apt-get update failed: {proc.stderr[-200:]}"
            result["steps"] = steps
            return result
    except subprocess.TimeoutExpired:
        result["message"] = "apt-get update timed out"
        result["steps"] = steps
        return result

    # Step 2: Install Docker
    steps.append("Installing docker.io and docker-compose-v2")
    try:
        proc = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "-qq", "docker.io", "docker-compose-v2"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            # Fallback: try docker-ce from official repo if docker.io unavailable
            steps.append("docker.io failed, trying docker-ce setup")
            _install_docker_ce(steps, result)
            if not result["installed"]:
                return result
        else:
            steps.append("Docker packages installed successfully")
    except subprocess.TimeoutExpired:
        result["message"] = "Docker installation timed out (300s)"
        result["steps"] = steps
        return result

    # Step 3: Start Docker daemon
    steps.append("Starting Docker daemon")
    _start_docker_daemon(steps)

    # Step 4: Add current user to docker group (avoid needing sudo for docker commands)
    import os
    username = os.environ.get("USER", "")
    if username:
        steps.append(f"Adding {username} to docker group")
        try:
            subprocess.run(
                ["sudo", "usermod", "-aG", "docker", username],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass  # Non-critical

    # Step 5: Verify installation
    steps.append("Verifying Docker installation")
    time.sleep(3)  # Give daemon a moment to start

    # Try docker with sudo first (group change needs new session)
    docker_works = False
    try:
        proc = subprocess.run(
            ["sudo", "docker", "info"],
            capture_output=True,
            timeout=15,
        )
        docker_works = proc.returncode == 0
    except Exception:
        pass

    if not docker_works:
        # Try without sudo
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=15,
            )
            docker_works = proc.returncode == 0
        except Exception:
            pass

    if docker_works:
        result["installed"] = True
        result["message"] = "Docker installed and running successfully"
    else:
        result["message"] = "Docker installed but daemon may not be running. Try: sudo dockerd &"

    result["steps"] = steps
    return result


def _install_docker_ce(steps: list, result: dict) -> None:
    """Install Docker CE from official Docker repository."""
    commands = [
        (["sudo", "apt-get", "install", "-y", "-qq", "ca-certificates", "curl", "gnupg"], 60),
        (["sudo", "install", "-m", "0755", "-d", "/etc/apt/keyrings"], 10),
    ]

    for cmd, timeout in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0:
                result["message"] = f"Docker CE setup failed at: {' '.join(cmd[:4])}"
                return
        except Exception as e:
            result["message"] = f"Docker CE setup error: {e}"
            return

    # Add Docker GPG key and repository
    try:
        # Get distro codename
        proc = subprocess.run(["lsb_release", "-cs"], capture_output=True, text=True, timeout=5)
        codename = proc.stdout.strip() if proc.returncode == 0 else "jammy"

        # Download GPG key
        subprocess.run(
            ["sudo", "bash", "-c",
             "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes"],
            capture_output=True, timeout=30,
        )

        # Add repository
        arch_proc = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, timeout=5)
        arch = arch_proc.stdout.strip() if arch_proc.returncode == 0 else "amd64"

        repo_line = f"deb [arch={arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu {codename} stable"
        subprocess.run(
            ["sudo", "bash", "-c", f"echo '{repo_line}' > /etc/apt/sources.list.d/docker.list"],
            capture_output=True, timeout=10,
        )

        # Update and install
        subprocess.run(["sudo", "apt-get", "update", "-qq"], capture_output=True, timeout=60)
        proc = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "-qq",
             "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode == 0:
            steps.append("Docker CE installed from official repository")
            result["installed"] = True
        else:
            result["message"] = f"Docker CE install failed: {proc.stderr[-200:]}"
    except Exception as e:
        result["message"] = f"Docker CE setup exception: {e}"


def _start_docker_daemon(steps: list) -> None:
    """Start the Docker daemon (handles both systemd and non-systemd environments)."""
    # Try systemctl first
    try:
        proc = subprocess.run(
            ["sudo", "systemctl", "start", "docker"],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            steps.append("Docker daemon started via systemctl")
            return
    except Exception:
        pass

    # Try service command
    try:
        proc = subprocess.run(
            ["sudo", "service", "docker", "start"],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            steps.append("Docker daemon started via service command")
            return
    except Exception:
        pass

    # WSL fallback: start dockerd directly in background
    try:
        subprocess.Popen(
            ["sudo", "dockerd"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        steps.append("Docker daemon started via dockerd (WSL mode)")
        time.sleep(3)  # Give it time to initialize
    except Exception:
        steps.append("WARNING: Could not start Docker daemon")


# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------


def _compose_project_args(run_id: str | None) -> list[str]:
    """Return ``-p <run_id>`` when a run namespace is active, else empty.

    Passing an explicit Compose project name isolates every network, volume,
    and default container name for the run, so two concurrent runs on the same
    compose file never collide.
    """
    return ["-p", run_id] if run_id else []


def _start_services(project_path: Path, services: list[dict], *, run_id: str | None = None) -> dict:
    """Attempt to start required services.

    Strategy:
    1. If docker-compose file exists → docker compose up -d
    2. If no compose file → start individual containers based on service type

    When ``run_id`` is set, the Compose project name is namespaced (``-p
    <run_id>``) and individual containers are name-/data-namespaced so
    parallel runs are isolated.
    """
    result = {"started": [], "failed": [], "method": None}

    compose_path = _find_compose_file(project_path)
    proj_args = _compose_project_args(run_id)

    if compose_path:
        # Use docker compose
        result["method"] = "docker-compose"
        try:
            proc = subprocess.run(
                ["docker", "compose", *proj_args, "up", "-d", "--wait"],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0:
                result["started"] = [s["name"] for s in services]
            else:
                # Try without --wait (older docker compose versions)
                proc = subprocess.run(
                    ["docker", "compose", *proj_args, "up", "-d"],
                    cwd=str(project_path),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if proc.returncode == 0:
                    result["started"] = [s["name"] for s in services]
                else:
                    result["failed"] = [s["name"] for s in services]
                    result["error"] = proc.stderr[-500:] if proc.stderr else "Unknown error"
        except subprocess.TimeoutExpired:
            result["failed"] = [s["name"] for s in services]
            result["error"] = "docker compose up timed out (120s)"
        except FileNotFoundError:
            result["failed"] = [s["name"] for s in services]
            result["error"] = "docker command not found"
    else:
        # Start individual containers
        result["method"] = "docker-run"
        for svc in services:
            started = _start_individual_service(project_path, svc, run_id=run_id)
            if started:
                result["started"].append(svc["name"])
            else:
                result["failed"].append(svc["name"])

    return result


def _namespaced_container_name(service: dict, run_id: str | None) -> str:
    """Container name for a service, namespaced by run_id when present.

    Legacy (run_id None): ``aah-<svc>``. Namespaced: ``aah-<run_id>-<svc>``.
    """
    svc_name = service.get("name", service.get("type", "unknown"))
    if run_id:
        return f"aah-{run_id}-{svc_name}"
    return f"aah-{svc_name}"


def _start_individual_service(project_path: Path, service: dict, *, run_id: str | None = None) -> bool:
    """Start a single service container."""
    svc_type = service.get("type", "unknown")
    port = service.get("port", 5432)
    container_name = _namespaced_container_name(service, run_id)

    # Check if container already exists but is stopped
    try:
        check = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        if check.stdout.strip():
            # Container exists — try to start it
            subprocess.run(
                ["docker", "start", container_name],
                capture_output=True, timeout=30,
            )
            return True
    except Exception:
        pass

    # Create a new container based on type
    env_vars = _load_env_vars(project_path)
    docker_cmd = _build_docker_run_cmd(svc_type, port, container_name, env_vars, run_id=run_id)

    if not docker_cmd:
        return False

    # First pull the image separately (with retries and longer timeout)
    image = docker_cmd[-1]  # Image is always last arg in docker run
    if not _pull_image_with_retry(image):
        return False

    # Now run the container (image already local, so this is fast)
    try:
        proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


def _pull_image_with_retry(image: str, retries: int = 3) -> bool:
    """Pull a Docker image with retry and increasing timeout.

    Timeouts: 120s → 240s → 480s
    """
    for attempt in range(retries):
        timeout = 120 * (2 ** attempt)  # 120, 240, 480
        try:
            proc = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode == 0:
                return True
            # Non-zero exit but didn't timeout — image might not exist
            if "not found" in (proc.stderr or "").lower():
                return False
        except subprocess.TimeoutExpired:
            continue  # Retry with higher timeout
        except Exception:
            return False
    return False


def _namespaced_db_name(base: str, run_id: str | None) -> str:
    """Namespace a database name by run_id so parallel runs get distinct data.

    Postgres identifiers are lowercased and limited to 63 chars; we join the
    base with a hyphen-free run token and clamp length. run_id None → base
    unchanged (legacy).
    """
    if not run_id:
        return base
    token = run_id.replace("-", "_")
    candidate = f"{base}_{token}"
    return candidate[:63]


def _build_docker_run_cmd(
    svc_type: str,
    port: int,
    container_name: str,
    env_vars: dict,
    *,
    run_id: str | None = None,
) -> list[str] | None:
    """Build docker run command for a service type.

    When ``run_id`` is set, the created container also joins the namespaced
    network (``aah-net-<run_id>``) and, for databases, uses a run-namespaced
    schema/database name so two runs on the same image never share rows.
    """
    net_args = ["--network", f"aah-net-{run_id}"] if run_id else []
    if svc_type == "postgres":
        user = env_vars.get("POSTGRES_USER", "postgres")
        password = env_vars.get("POSTGRES_PASSWORD", "postgres")
        db = _namespaced_db_name(env_vars.get("POSTGRES_DB", "postgres"), run_id)
        image = "pgvector/pgvector:pg15"  # Default with pgvector for ML projects
        return [
            "docker", "run", "-d",
            "--name", container_name,
            *net_args,
            "-e", f"POSTGRES_USER={user}",
            "-e", f"POSTGRES_PASSWORD={password}",
            "-e", f"POSTGRES_DB={db}",
            "-p", f"{port}:5432",
            image,
        ]
    elif svc_type == "redis":
        return [
            "docker", "run", "-d",
            "--name", container_name,
            *net_args,
            "-p", f"{port}:6379",
            "redis:7-alpine",
        ]
    elif svc_type == "mongodb":
        return [
            "docker", "run", "-d",
            "--name", container_name,
            *net_args,
            "-p", f"{port}:27017",
            "mongo:7",
        ]
    elif svc_type == "mysql":
        password = env_vars.get("MYSQL_ROOT_PASSWORD", "root")
        db = _namespaced_db_name(env_vars.get("MYSQL_DATABASE", "app"), run_id)
        return [
            "docker", "run", "-d",
            "--name", container_name,
            *net_args,
            "-e", f"MYSQL_ROOT_PASSWORD={password}",
            "-e", f"MYSQL_DATABASE={db}",
            "-p", f"{port}:3306",
            "mysql:8",
        ]
    return None


def _load_env_vars(project_path: Path) -> dict:
    """Load environment variables from .env file."""
    env_vars = {}
    for env_file in [project_path / ".env", project_path / ".env.example"]:
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding='utf-8').split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        env_vars[key.strip()] = val.strip()
            except Exception:
                pass
            break
    return env_vars


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def _setup_status_from(result: dict, services: list[dict]) -> None:
    """Annotate ``result`` with a structured setup_status + required_failures.

    Populates three additive keys the runner consumes:
      * ``setup_status``: ``ready`` | ``no_signal`` | ``not_applicable``
      * ``required_failures``: names of required services that could not start
      * ``required`` per service (already reflected in ``services``)

    A required-service failure → ``no_signal`` (caller blocks). All failures
    being non-required (or no services) → ``not_applicable`` when not ready,
    else ``ready``. This keeps the legacy ``ready`` bool intact while giving
    the runner a fail-closed signal for required infra.
    """
    required_names = {s.get("name") for s in services if _service_is_required(s)}
    failed = set(result.get("_failed_services") or [])
    required_failures = sorted(n for n in failed if n in required_names)
    result["required_failures"] = required_failures
    if result.get("ready"):
        result["setup_status"] = "ready"
    elif required_failures:
        result["setup_status"] = "no_signal"
    else:
        # Nothing required failed — the missing infra is not relevant to a
        # gating signal.
        result["setup_status"] = "not_applicable"
    result.pop("_failed_services", None)


def ensure_infrastructure(project_path: Path, max_wait: int = 30, *, run_id: str | None = None) -> dict:
    """Ensure all required infrastructure services are running.

    Returns:
        {
            "ready": bool,
            "services": [...],           # each with a "required": bool
            "actions_taken": [...],
            "message": str,
            "setup_status": "ready" | "no_signal" | "not_applicable",
            "required_failures": [...],
            "namespace": run_id | None,
        }

    When ``run_id`` is set, services are started in an isolated Compose
    project / network / data namespace so parallel runs cannot collide.
    """
    result = {
        "ready": False,
        "services": [],
        "actions_taken": [],
        "message": "",
        "namespace": run_id,
    }

    # Detect required services
    services = _detect_required_services(project_path)
    for svc in services:
        svc["required"] = _service_is_required(svc)
    if not services:
        result["ready"] = True
        result["message"] = "No infrastructure services detected — skipping"
        result["setup_status"] = "not_applicable"
        result["required_failures"] = []
        return result

    result["services"] = [
        {"name": s["name"], "type": s.get("type"), "port": s.get("port"), "required": s.get("required", False)}
        for s in services
    ]

    # Check which services are already healthy
    unhealthy = []
    for svc in services:
        if not _check_service_health(svc):
            unhealthy.append(svc)

    if not unhealthy:
        result["ready"] = True
        result["message"] = f"All {len(services)} service(s) healthy"
        _setup_status_from(result, services)
        return result

    # Ensure Docker is available before trying to start services
    if not _is_docker_available():
        result["actions_taken"].append("Docker not available — attempting installation")
        install_result = _install_docker()
        result["actions_taken"].append(f"Docker install: {install_result['message']}")
        if install_result.get("steps"):
            result["actions_taken"].extend(install_result["steps"])

        if not install_result["installed"]:
            # Check if daemon just needed starting (docker binary exists but daemon is down)
            if shutil.which("docker"):
                result["actions_taken"].append("Docker binary found but daemon not responding — starting daemon")
                _start_docker_daemon(result["actions_taken"])
                time.sleep(5)
                if not _is_docker_available():
                    result["ready"] = False
                    result["message"] = f"Docker daemon not responding after start attempt: {install_result['message']}"
                    result["_failed_services"] = [s["name"] for s in unhealthy]
                    _setup_status_from(result, services)
                    return result
            else:
                result["ready"] = False
                result["message"] = f"Cannot start services: {install_result['message']}"
                result["_failed_services"] = [s["name"] for s in unhealthy]
                _setup_status_from(result, services)
                return result

    # Create the isolated network for the run BEFORE starting individual
    # containers so they can attach to it (compose manages its own network).
    if run_id:
        _ensure_namespace_network(run_id, result["actions_taken"])

    # Attempt to start unhealthy services
    result["actions_taken"].append(f"Starting {len(unhealthy)} unhealthy service(s): {[s['name'] for s in unhealthy]}")
    start_result = _start_services(project_path, unhealthy, run_id=run_id)
    result["actions_taken"].append(f"Start method: {start_result['method']}")

    if start_result["failed"]:
        result["ready"] = False
        result["message"] = f"Failed to start: {start_result['failed']}. Error: {start_result.get('error', 'unknown')}"
        result["_failed_services"] = list(start_result["failed"])
        _setup_status_from(result, services)
        return result

    # Wait for services to become ready
    result["actions_taken"].append(f"Waiting up to {max_wait}s for services to become ready")
    deadline = time.time() + max_wait
    still_unhealthy = list(unhealthy)

    while still_unhealthy and time.time() < deadline:
        time.sleep(2)
        still_unhealthy = [s for s in still_unhealthy if not _check_service_health(s)]

    if still_unhealthy:
        result["ready"] = False
        names = [s["name"] for s in still_unhealthy]
        result["message"] = f"Services started but not ready after {max_wait}s: {names}"
        result["_failed_services"] = names
    else:
        result["ready"] = True
        result["message"] = f"All services ready (started {len(unhealthy)} service(s))"

    _setup_status_from(result, services)
    return result


def _ensure_namespace_network(run_id: str, actions: list) -> str | None:
    """Create the per-run isolated Docker network ``aah-net-<run_id>``.

    Idempotent and non-fatal: if docker is unavailable or the create fails,
    records the attempt and returns None (compose-based runs manage their own
    network via the project name).
    """
    network = f"aah-net-{run_id}"
    if not shutil.which("docker"):
        return None
    try:
        inspect = subprocess.run(
            ["docker", "network", "inspect", network],
            capture_output=True, timeout=10,
        )
        if inspect.returncode != 0:
            subprocess.run(
                ["docker", "network", "create", network],
                capture_output=True, timeout=10,
            )
        actions.append(f"Namespaced network ensured: {network}")
        return network
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stop services (post-wave cleanup)
# ---------------------------------------------------------------------------


def cleanup_namespace(run_id: str, project_path: Path) -> dict:
    """Tear down ONLY the resources created for ``run_id``, recording evidence.

    Runs in the runner's ``finally`` block. Deletes strictly within the run
    namespace: ``docker compose -p <run_id> down --volumes --remove-orphans``,
    ``docker network rm aah-net-<run_id>``, and ``docker rm -f`` for every
    container whose name is prefixed ``aah-<run_id>-``. Records what was
    removed and what leftover resources remain.

    SECURITY (CLAUDE.md project protection): this NEVER performs
    ``rm -rf`` / ``shutil.rmtree`` / ``os.remove`` on any filesystem path. It
    only issues namespaced docker teardown commands. The returned shape mirrors
    verify.py's ``_stop_server`` cleanup shape so callers can consume it
    uniformly: ``{transport, action, removed, leftover, errors, stopped,
    timestamp}``.
    """
    from datetime import datetime, timezone

    result = {
        "transport": "docker",
        "action": "cleanup_namespace",
        "namespace": run_id,
        "removed": [],
        "leftover": [],
        "errors": [],
        "stopped": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if not isinstance(run_id, str) or not run_id.strip():
        result["errors"].append("cleanup_namespace requires a non-empty run_id")
        return result

    if not shutil.which("docker"):
        # Nothing to tear down; not an error — no docker means no namespaced
        # resources were ever created.
        result["stopped"] = True
        result["transport"] = "none"
        return result

    network = f"aah-net-{run_id}"
    container_prefix = f"aah-{run_id}-"

    # 1. Compose stack for this project name (only affects -p <run_id>).
    try:
        proc = subprocess.run(
            ["docker", "compose", "-p", run_id, "down", "--volumes", "--remove-orphans"],
            cwd=str(project_path),
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            result["removed"].append(f"compose-project:{run_id}")
        # Non-zero is common when no compose stack exists for this name; only
        # record a real error if stderr looks like a failure other than "no
        # configuration"/"not found".
        elif proc.stderr and "no configuration file" not in proc.stderr.lower():
            result["errors"].append(f"compose down: {proc.stderr[-200:]}")
    except Exception as exc:
        result["errors"].append(f"compose down error: {exc}")

    # 2. Individually-started containers, name-prefixed by the namespace.
    try:
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={container_prefix}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
        names = [n for n in listed.stdout.splitlines() if n.strip()]
        for name in names:
            # Defensive: only remove names that actually carry our prefix.
            if not name.startswith(container_prefix):
                result["leftover"].append(name)
                continue
            rm = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True, text=True, timeout=30,
            )
            if rm.returncode == 0:
                result["removed"].append(f"container:{name}")
            else:
                result["leftover"].append(name)
                if rm.stderr:
                    result["errors"].append(f"rm {name}: {rm.stderr[-150:]}")
    except Exception as exc:
        result["errors"].append(f"container removal error: {exc}")

    # 3. The per-run network (after its containers are gone).
    try:
        rm_net = subprocess.run(
            ["docker", "network", "rm", network],
            capture_output=True, text=True, timeout=15,
        )
        if rm_net.returncode == 0:
            result["removed"].append(f"network:{network}")
        elif rm_net.stderr and "not found" not in rm_net.stderr.lower():
            result["leftover"].append(network)
            result["errors"].append(f"network rm: {rm_net.stderr[-150:]}")
    except Exception as exc:
        result["errors"].append(f"network rm error: {exc}")

    result["stopped"] = not result["errors"]
    return result


def stop_infrastructure(project_path: Path) -> dict:
    """Tear down infrastructure services after runtime validation completes.

    Uses 'docker compose down' to stop AND remove containers, networks, and
    anonymous volumes. This prevents container name conflicts on subsequent runs.
    Also stops the app container if running (Docker runtime mode).
    Docker itself stays installed.

    Returns:
        {"stopped": bool, "services": [...], "message": str}
    """
    result = {
        "stopped": False,
        "services": [],
        "message": "",
    }

    if not shutil.which("docker"):
        result["stopped"] = True
        result["message"] = "Docker not found — nothing to stop"
        return result

    # Stop any leftover app container (aah-runtime-validator agent normally cleans up)
    try:
        project_name = project_path.name.lower().replace(" ", "-").replace("_", "-")
        container_name = f"aah-app-{project_name}"
        proc = subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, timeout=10,
        )
        if proc.returncode == 0:
            result["services"].append(container_name)
    except Exception:
        pass  # Non-fatal — app container may not exist

    compose_path = _find_compose_file(project_path)

    if compose_path:
        # Tear down via docker compose down — removes containers and networks
        try:
            proc = subprocess.run(
                ["docker", "compose", "down", "--remove-orphans"],
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0:
                result["stopped"] = True
                result["message"] = "Services torn down via docker compose down"
            else:
                result["message"] = f"docker compose down failed: {proc.stderr[-200:]}"
        except subprocess.TimeoutExpired:
            result["message"] = "docker compose down timed out"
        except Exception as e:
            result["message"] = f"Error tearing down services: {e}"
    else:
        # Stop and remove individual aah-* containers
        services = _detect_required_services(project_path)
        stopped = []
        for svc in services:
            container_name = f"aah-{svc.get('name', svc.get('type', 'unknown'))}"
            try:
                subprocess.run(
                    ["docker", "stop", container_name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                proc = subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    stopped.append(container_name)
            except Exception:
                pass

        result["services"] = stopped
        if stopped:
            result["stopped"] = True
            result["message"] = f"Removed {len(stopped)} container(s): {stopped}"
        else:
            result["stopped"] = True
            result["message"] = "No running containers found to remove"

    return result


# ---------------------------------------------------------------------------
# Full Test Environment Setup
# ---------------------------------------------------------------------------


def _detect_docker_requirement(project_path: Path) -> bool:
    """Detect if Docker is needed for testing.

    Checks:
    1. requirements.txt / requirements-test.txt / requirements-dev.txt for testcontainers/docker
    2. Test compose file exists
    3. pyproject.toml dependencies referencing testcontainers/docker
    """
    # Check requirements files for docker/testcontainers
    req_files = [
        "requirements.txt",
        "requirements-test.txt",
        "requirements-dev.txt",
        "requirements-ci.txt",
    ]
    for req_name in req_files:
        req_path = project_path / req_name
        if req_path.exists():
            try:
                content = req_path.read_text(encoding='utf-8').lower()
                if "testcontainers" in content or "docker" in content:
                    return True
            except Exception:
                pass

    # Check pyproject.toml for test dependencies
    pyproject = project_path / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding='utf-8').lower()
            if "testcontainers" in content or "docker" in content:
                return True
        except Exception:
            pass

    # Check if test compose file exists
    if _find_test_compose_file(project_path):
        return True

    return False


def _install_dependencies(project_path: Path) -> dict:
    """Install this project's test dependencies, whatever language it is.

    Dispatches to the language adapter pack rather than assuming Python. The
    previous Python-only version reported "No requirements file found —
    skipping dependency install" for every JS, Java, Go and Rust project, and
    the caller downgraded that to a warning, so those features ran their tests
    with no test runner installed and failed for reasons unrelated to their
    code. Dependency directories are gitignored by design, so they are absent
    from every feature worktree until something installs them there — and this
    is that something.

    Returns: {"installed": bool, "message": str, "method": str}
    """
    result = {"installed": False, "message": "", "method": ""}

    try:
        from aah.core.build.lang_checks import detect

        cmds = detect(project_path).install_deps()
    except Exception as exc:  # noqa: BLE001 — never block tests on adapter resolution
        result["installed"] = True
        result["message"] = f"Could not resolve an install command ({exc}) — skipping"
        return result

    if not cmds:
        result["installed"] = True
        result["message"] = "No dependency manifest found — skipping dependency install"
        return result

    # Run in order, stop at the first failure: a later step normally depends on
    # an earlier one (uv pip install needs the venv uv venv just made), so
    # continuing past a failure only produces a second, more confusing error.
    methods: list[str] = []
    for cmd in cmds:
        label = cmd.label or " ".join(cmd.argv)
        methods.append(label)
        result["method"] = " && ".join(methods)
        try:
            proc = subprocess.run(
                cmd.argv,
                cwd=str(cmd.cwd or project_path),
                capture_output=True,
                text=True,
                timeout=cmd.timeout_sec,
                env={**os.environ, **cmd.env} if cmd.env else None,
            )
        except subprocess.TimeoutExpired:
            result["message"] = f"Dependency installation timed out ({label}, {cmd.timeout_sec}s)"
            return result
        except Exception as e:  # noqa: BLE001 — reported, never raised at the caller
            result["message"] = f"Install error ({label}): {e}"
            return result
        if proc.returncode != 0:
            result["message"] = f"Install failed ({label}): {proc.stderr[-500:]}"
            return result

    result["installed"] = True
    result["message"] = f"Dependencies installed via {result['method']}"
    return result


def _start_test_compose(
    project_path: Path, compose_path: Path, max_wait: int = 60, *, run_id: str | None = None
) -> dict:
    """Start services from a test compose file and wait for health.

    Returns: {"started": bool, "message": str}

    When ``run_id`` is set, the Compose project name is namespaced (``-p
    <run_id>``) so parallel test compose stacks stay isolated.
    """
    result = {"started": False, "message": ""}
    proj_args = _compose_project_args(run_id)

    try:
        # Use -f flag to specify the compose file explicitly
        proc = subprocess.run(
            ["docker", "compose", *proj_args, "-f", str(compose_path), "up", "-d", "--wait"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=max_wait + 30,
        )
        if proc.returncode == 0:
            result["started"] = True
            result["message"] = f"Services started from {compose_path.name}"
            return result

        # Retry without --wait (older docker compose)
        proc = subprocess.run(
            ["docker", "compose", *proj_args, "-f", str(compose_path), "up", "-d"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=max_wait + 30,
        )
        if proc.returncode == 0:
            # Wait manually for services
            time.sleep(10)
            result["started"] = True
            result["message"] = f"Services started from {compose_path.name} (without --wait)"
        else:
            result["message"] = f"docker compose up failed: {proc.stderr[-300:]}"

    except subprocess.TimeoutExpired:
        result["message"] = f"docker compose up timed out ({max_wait + 30}s)"
    except FileNotFoundError:
        result["message"] = "docker compose command not found"
    except Exception as e:
        result["message"] = f"Error starting compose: {str(e)}"

    return result


def _finalize_setup_status(result: dict, project_path: Path, run_id: str | None) -> dict:
    """Compute setup_status and required failures for a test env result.

    Health-checks every DECLARED service (post start attempts) and derives a
    fail-closed setup signal:
      * a REQUIRED service that is unhealthy → ``no_signal`` (caller blocks)
      * only non-required infra unhealthy    → ``not_applicable`` (proceed)
      * all healthy / none declared          → ``ready``
    This is additive — the legacy ``ready`` bool is left untouched.
    """
    services = _detect_required_services(project_path)
    for svc in services:
        svc["required"] = _service_is_required(svc)
    result["namespace"] = run_id
    result["services"] = [
        {"name": s.get("name"), "type": s.get("type"), "port": s.get("port"), "required": s.get("required", False)}
        for s in services
    ]
    if not services:
        result["setup_status"] = "ready"
        result["required_failures"] = []
        return result
    unhealthy = [s for s in services if not _check_service_health(s)]
    required_failures = sorted(s["name"] for s in unhealthy if s.get("required"))
    result["required_failures"] = required_failures
    if required_failures:
        result["setup_status"] = "no_signal"
    elif unhealthy:
        result["setup_status"] = "not_applicable"
    else:
        result["setup_status"] = "ready"
    return result


def ensure_test_environment(project_path: Path, max_wait: int = 60, *, run_id: str | None = None) -> dict:
    """Full pre-test environment setup: dependencies + Docker + services.

    This is the main entry point for test environment preparation. It:
    1. Installs Python test dependencies (from requirements.txt)
    2. Detects if Docker is needed (from deps or compose files)
    3. Installs Docker if needed and not available
    4. Starts test services from compose file
    5. Waits for services to be healthy

    When ``run_id`` is set, compose/network/data resources are namespaced so
    parallel runs are isolated, and the returned dict carries a structured
    ``setup_status`` (``ready`` | ``no_signal`` | ``not_applicable``),
    ``required_failures`` and ``namespace``.

    Returns:
        {
            "ready": bool,
            "actions_taken": [...],
            "message": str,
            "deps_installed": bool,
            "docker_available": bool,
            "services_started": bool,
            "setup_status": "ready" | "no_signal" | "not_applicable",
            "required_failures": [...],
            "namespace": run_id | None,
        }
    """
    result = {
        "ready": False,
        "actions_taken": [],
        "message": "",
        "deps_installed": False,
        "docker_available": False,
        "services_started": False,
    }

    # Step 1: Install test dependencies (language resolved by the adapter pack)
    result["actions_taken"].append("Installing test dependencies")
    deps_result = _install_dependencies(project_path)
    result["deps_installed"] = deps_result["installed"]
    result["actions_taken"].append(f"Dependencies: {deps_result['message']}")

    if not deps_result["installed"]:
        result["message"] = f"Failed to install dependencies: {deps_result['message']}"
        return _finalize_setup_status(result, project_path, run_id)

    # Step 2: Check if Docker is needed
    needs_docker = _detect_docker_requirement(project_path)
    if not needs_docker:
        result["ready"] = True
        result["docker_available"] = _is_docker_available()
        result["message"] = "Dependencies installed. No Docker requirement detected."
        return _finalize_setup_status(result, project_path, run_id)

    result["actions_taken"].append("Docker required (testcontainers/compose detected)")

    # Step 3: Ensure Docker is installed and running
    if _is_docker_available():
        result["docker_available"] = True
        result["actions_taken"].append("Docker already available")
    else:
        result["actions_taken"].append("Docker not available — installing")
        _ensure_passwordless_sudo()

        install_result = _install_docker()
        result["actions_taken"].append(f"Docker install: {install_result['message']}")
        if install_result.get("steps"):
            result["actions_taken"].extend(install_result["steps"])

        if not install_result["installed"]:
            # Try just starting the daemon if binary exists
            if shutil.which("docker"):
                result["actions_taken"].append("Docker binary exists — starting daemon")
                _start_docker_daemon(result["actions_taken"])
                time.sleep(5)

            if not _is_docker_available():
                result["message"] = f"Docker installation/startup failed: {install_result['message']}"
                return _finalize_setup_status(result, project_path, run_id)

        result["docker_available"] = True
        result["actions_taken"].append("Docker is now available")

    # Step 4: Start test services from compose file
    compose_path = _find_test_compose_file(project_path)
    if compose_path:
        result["actions_taken"].append(f"Starting services from {compose_path.name}")
        compose_result = _start_test_compose(project_path, compose_path, max_wait, run_id=run_id)
        result["actions_taken"].append(f"Compose: {compose_result['message']}")

        if compose_result["started"]:
            result["services_started"] = True
        else:
            # Non-fatal — testcontainers may handle its own containers
            result["actions_taken"].append(
                "Compose start failed but testcontainers may manage containers directly"
            )
    else:
        result["actions_taken"].append("No compose file — testcontainers will manage containers")

    # Step 5: Ensure Docker network for app container connectivity. When a run
    # namespace is active, use the per-run isolated network; otherwise the
    # historical shared aah-test-net (legacy).
    if result["docker_available"]:
        network = f"aah-net-{run_id}" if run_id else "aah-test-net"
        try:
            proc = subprocess.run(
                ["docker", "network", "inspect", network],
                capture_output=True, timeout=10,
            )
            if proc.returncode != 0:
                subprocess.run(
                    ["docker", "network", "create", network],
                    capture_output=True, timeout=10,
                )
            result["docker_network"] = network
            result["actions_taken"].append(f"Docker network ensured: {network}")
        except Exception:
            pass  # Non-fatal — network will be created by agent if needed

    # Step 6: Final readiness check
    result["ready"] = True
    result["message"] = "Test environment ready: dependencies installed, Docker available"
    if result["services_started"]:
        result["message"] += ", services running"

    return _finalize_setup_status(result, project_path, run_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Pre-Test Infrastructure Check")
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Check if infrastructure is healthy (no start)")
    check_p.add_argument("--project-path", type=Path, default=None)

    start_p = sub.add_parser("start", help="Ensure infrastructure is running (start if needed)")
    start_p.add_argument("--project-path", type=Path, default=None)
    start_p.add_argument("--max-wait", type=int, default=30, help="Max seconds to wait for readiness")

    stop_p = sub.add_parser("stop", help="Stop infrastructure services (post-wave cleanup)")
    stop_p.add_argument("--project-path", type=Path, default=None)

    status_p = sub.add_parser("status", help="Show detected services and their status")
    status_p.add_argument("--project-path", type=Path, default=None)

    env_p = sub.add_parser("ensure-test-env", help="Full test environment setup (deps + Docker + services)")
    env_p.add_argument("--project-path", type=Path, default=None)
    env_p.add_argument("--max-wait", type=int, default=60, help="Max seconds to wait for services")

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(getattr(args, "project_path", None))

    if args.command == "check":
        services = _detect_required_services(project_path)
        all_healthy = True
        results = []
        for svc in services:
            healthy = _check_service_health(svc)
            results.append({"name": svc["name"], "type": svc.get("type"), "healthy": healthy})
            if not healthy:
                all_healthy = False

        output = {"all_healthy": all_healthy, "services": results}
        json.dump(output, sys.stdout, indent=2)
        print()
        sys.exit(0 if all_healthy else 2)

    elif args.command == "start":
        result = ensure_infrastructure(project_path, args.max_wait)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(result["message"], file=sys.stderr)
        sys.exit(0 if result["ready"] else 2)

    elif args.command == "stop":
        result = stop_infrastructure(project_path)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(result["message"], file=sys.stderr)
        sys.exit(0 if result["stopped"] else 2)

    elif args.command == "status":
        services = _detect_required_services(project_path)
        for svc in services:
            healthy = _check_service_health(svc)
            status = "UP" if healthy else "DOWN"
            port = svc.get("port", "?")
            print(f"  {svc['name']} ({svc.get('type', '?')}) @ port {port}: {status}", file=sys.stderr)

        json.dump({"services": services}, sys.stdout, indent=2)
        print()
        sys.exit(0)

    elif args.command == "ensure-test-env":
        result = ensure_test_environment(project_path, args.max_wait)
        json.dump(result, sys.stdout, indent=2)
        print()
        print(result["message"], file=sys.stderr)
        sys.exit(0 if result["ready"] else 2)


if __name__ == "__main__":
    main()
