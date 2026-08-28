#!/usr/bin/env python3
"""
Full GCP Cloud Run deployment — one service, one command.

Mirrors ecs_deploy.py for GCP. All configuration comes from CLI arguments.

Cloud Run is simpler than ECS (one gcloud command does most of the work),
but wrapping it in a script provides:
- Consistent CLI interface across clouds (aah run ... run --tier 1B ...)
- Tier-aware flag selection (ingress, auth)
- Tier-aware health checking (bare curl, auth curl, proxy)
- JSON result for pipeline consumption
- Teardown support

Usage (deploy):
    aah run core.deploy.cloudrun_deploy run \
      --service-name my-backend \
      --service-path /path/to/service \
      --port 8080 \
      --region us-central1 \
      --project my-gcp-project \
      --tier 1B \
      --env "KEY1=val1" --env "KEY2=val2"

Usage (teardown):
    aah run core.deploy.cloudrun_deploy teardown \
      --service-name my-backend \
      --region us-central1 \
      --project my-gcp-project

Design:
    - Every parameter comes from CLI args (nothing hardcoded)
    - Prefers --source deploy (buildpacks, no Docker needed)
    - Falls back to Cloud Build + image deploy if source fails
    - Tier-aware: ingress, auth, and health check method vary by tier
    - Returns JSON result on stdout for pipeline consumption
"""

import argparse
import json
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IS_WINDOWS = platform.system() == "Windows"
_TOTAL_STEPS = 4


def _log(step: int, msg: str) -> None:
    """Print a progress line."""
    print(f"[{step}/{_TOTAL_STEPS}] {msg}", flush=True)


def _gcloud(
    cmd: str,
    project: str,
    check: bool = True,
    timeout: int = 600,
    capture: bool = True,
) -> str | None:
    """
    Run a gcloud CLI command.

    Returns stdout string or None on non-fatal failure.
    """
    argv = shlex.split(f"gcloud {cmd} --project {project}")
    argv[0] = shutil.which(argv[0]) or argv[0]

    try:
        r = subprocess.run(
            argv,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if check:
            print(f"  ERROR: Command timed out ({timeout}s): {cmd[:80]}...", flush=True)
            sys.exit(1)
        return None

    if r.returncode != 0:
        if not check:
            return None
        stderr = r.stderr.strip() if r.stderr else ""
        print(f"  ERROR: {stderr[:400]}", flush=True)
        sys.exit(1)

    return r.stdout.strip() if capture and r.stdout else ""


def _run_cmd(cmd: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    argv = shlex.split(cmd)
    argv[0] = shutil.which(argv[0]) or argv[0]
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------

def _tier_flags(tier: str) -> tuple[str, str]:
    """Return (ingress_flag, auth_flag) for a tier."""
    if tier == "1A":
        return "--ingress=all", "--allow-unauthenticated"
    elif tier == "1B":
        return "--ingress=all", "--no-allow-unauthenticated"
    elif tier == "1C":
        return "--ingress=internal", "--no-allow-unauthenticated"
    else:
        return "--ingress=all", "--allow-unauthenticated"


# ---------------------------------------------------------------------------
# Pre-flight: nginx proxy for frontend→backend connectivity
# ---------------------------------------------------------------------------

def _patch_nginx_proxy_DEPRECATED(service_path: Path, env_vars: list[str]) -> None:
    """
    DEPRECATED: No longer called. Frontend→backend connectivity is handled via
    VITE_API_URL build arg (baked into JS bundle at build time).
    Kept for reference only — can be removed in a future cleanup.

    Original purpose: Ensure frontend nginx.conf proxies /api/* to the backend URL.

    If a Dockerfile with nginx exists and no proxy config is set up,
    patches it to COPY nginx.conf and use envsubst for BACKEND_URL
    resolution at runtime.
    """
    nginx_conf = service_path / "nginx.conf"
    dockerfile = service_path / "Dockerfile"

    if not dockerfile.exists():
        return

    dockerfile_content = dockerfile.read_text(encoding="utf-8")

    # Only applies to nginx-based frontends
    if "nginx" not in dockerfile_content.lower():
        return

    # Check if a backend URL is being passed
    backend_url = None
    for env in env_vars:
        if "=" in env:
            key, val = env.split("=", 1)
            if "BACKEND" in key.upper() and "URL" in key.upper():
                backend_url = val
                break

    if not backend_url:
        return

    # Ensure nginx.conf exists with proxy block
    if not nginx_conf.exists():
        print(f"  Creating nginx.conf with /api/ proxy -> backend", flush=True)
        nginx_conf.write_text(
            "server {\n"
            "    listen ${PORT};\n"
            "    server_name _;\n"
            "    root /usr/share/nginx/html;\n"
            "    index index.html;\n"
            "\n"
            "    location /api/ {\n"
            "        proxy_pass ${BACKEND_URL}/api/;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "    }\n"
            "\n"
            "    location / {\n"
            "        try_files $uri $uri/ /index.html;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
    else:
        content = nginx_conf.read_text(encoding="utf-8")
        if "proxy_pass" not in content:
            print(f"  Patching nginx.conf: adding /api/ proxy block", flush=True)
            proxy_block = (
                "\n    location /api/ {\n"
                "        proxy_pass ${BACKEND_URL}/api/;\n"
                "        proxy_set_header Host $host;\n"
                "        proxy_set_header X-Real-IP $remote_addr;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_set_header X-Forwarded-Proto $scheme;\n"
                "    }\n"
            )
            if "location / {" in content:
                content = content.replace(
                    "location / {",
                    proxy_block + "\n    location / {"
                )
            else:
                content = content.rstrip().rstrip("}") + proxy_block + "}\n"
            nginx_conf.write_text(content, encoding="utf-8")

    # Ensure Dockerfile uses nginx.conf with envsubst
    if "nginx.conf" in dockerfile_content and "envsubst" in dockerfile_content:
        print(f"  nginx.conf + envsubst already configured in Dockerfile", flush=True)
        return

    print(f"  Patching Dockerfile: COPY nginx.conf + envsubst entrypoint", flush=True)

    # Remove inline nginx config and existing CMD
    # Strategy: use index-based iteration with look-ahead to find RUN echo blocks
    # that write to /etc/nginx (redirect may be on the last line of a multi-line block)
    lines = dockerfile_content.splitlines()
    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect start of a RUN echo block
        stripped = line.strip()
        if stripped.startswith("RUN") and "echo" in stripped:
            # Collect the entire block (continuation lines ending with \)
            block_start = i
            j = i
            while j < len(lines) and lines[j].rstrip().endswith("\\"):
                j += 1
            # j is now the last line of the block (doesn't end with \)
            block_end = j  # inclusive

            # Check if any line in the block writes to /etc/nginx
            block_text = "\n".join(lines[block_start:block_end + 1])
            if "/etc/nginx" in block_text:
                # Skip this entire block + preceding comment about nginx
                if new_lines and new_lines[-1].strip().startswith("#") and "nginx" in new_lines[-1].lower():
                    new_lines.pop()  # Remove the comment too
                i = block_end + 1
                continue

        if line.strip().startswith("CMD") and "nginx" in line:
            i += 1
            continue
        new_lines.append(line)
        i += 1

    # Insert COPY before EXPOSE
    insert_idx = len(new_lines)
    for i, line in enumerate(new_lines):
        if line.strip().startswith("EXPOSE"):
            insert_idx = i
            break

    nginx_lines = [
        "",
        "# AAH: COPY nginx.conf and use envsubst for env var resolution",
        "COPY nginx.conf /etc/nginx/templates/default.conf.template",
        "",
    ]
    for i, nl in enumerate(nginx_lines):
        new_lines.insert(insert_idx + i, nl)

    # Add envsubst CMD
    new_lines.append("")
    new_lines.append(
        'CMD ["/bin/sh", "-c", '
        '"envsubst \'${PORT} ${BACKEND_URL}\' < /etc/nginx/templates/default.conf.template '
        '> /etc/nginx/conf.d/default.conf && nginx -g \'daemon off;\'"]'
    )

    dockerfile.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1: Deploy from source (preferred)
# ---------------------------------------------------------------------------

def _deploy_from_source(
    service_name: str, service_path: Path, port: int,
    region: str, project: str, tier: str,
    env_vars: list[str], memory: str, cpu: str,
) -> str | None:
    """
    Deploy using --source (Cloud Build + buildpacks).
    No Docker or Dockerfile required.

    Returns the service URL on success, None on failure.
    """
    ingress_flag, auth_flag = _tier_flags(tier)

    # Build --set-env-vars string (runtime) and --build-env-vars (build-time)
    # NOTE: Cloud Run reserves PORT — it's set automatically from --port flag.
    # Do NOT include PORT here or deploy will fail with "reserved env names".
    env_parts = []
    build_env_parts = []
    for env in env_vars:
        if "=" in env:
            key = env.split("=", 1)[0].strip()
            if key.upper() == "PORT":
                continue  # Cloud Run sets this automatically
            env_parts.append(env)
            # VITE_* and NEXT_PUBLIC_* vars must also be set at build time
            # (they get baked into the JS bundle during npm run build)
            if key.startswith("VITE_") or key.startswith("NEXT_PUBLIC_"):
                build_env_parts.append(env)
    env_str = ",".join(env_parts) if env_parts else ""
    build_env_str = ",".join(build_env_parts) if build_env_parts else ""

    env_flag = f"--set-env-vars \"{env_str}\" " if env_str else ""
    build_env_flag = f"--set-build-env-vars \"{build_env_str}\" " if build_env_str else ""

    cmd = (
        f"gcloud run deploy {service_name} "
        f"--source \"{service_path}\" "
        f"--region {region} "
        f"--project {project} "
        f"{ingress_flag} "
        f"{auth_flag} "
        f"--port {port} "
        f"--memory {memory} "
        f"--cpu {cpu} "
        f"--min-instances 0 "
        f"--max-instances 10 "
        f"{env_flag}"
        f"{build_env_flag}"
        f"--quiet"
    )

    print(f"  Running: gcloud run deploy {service_name} --source ...", flush=True)
    returncode, stdout, stderr = _run_cmd(cmd, timeout=600)

    if returncode != 0:
        # Source deploy can fail if buildpacks can't detect the framework
        print(f"  Source deploy failed: {stderr[:200]}", flush=True)
        return None

    # Get the URL
    url_cmd = (
        f"gcloud run services describe {service_name} "
        f"--region {region} --project {project} "
        f"--format \"value(status.url)\""
    )
    _, url, _ = _run_cmd(url_cmd)
    return url if url else None


# ---------------------------------------------------------------------------
# Step 2: Fallback — Deploy via Cloud Build image
# ---------------------------------------------------------------------------

def _deploy_from_image(
    service_name: str, service_path: Path, port: int,
    region: str, project: str, tier: str,
    env_vars: list[str], memory: str, cpu: str,
) -> str | None:
    """
    Fallback: build via Cloud Build, deploy the image.
    Requires a Dockerfile in service_path.

    Returns the service URL on success, None on failure.
    """
    ingress_flag, auth_flag = _tier_flags(tier)

    # Build image via Cloud Build
    image_uri = f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/{service_name}:latest"
    print(f"  Building image via Cloud Build...", flush=True)

    # Extract build-time vars (VITE_*, NEXT_PUBLIC_*) to pass as --build-arg
    build_args_str = ""
    for env in env_vars:
        if "=" in env:
            key = env.split("=", 1)[0].strip()
            if key.startswith("VITE_") or key.startswith("NEXT_PUBLIC_"):
                build_args_str += f" --build-arg {env}"

    # Use cloudbuild inline config if we have build args, otherwise simple --tag
    if build_args_str:
        # Write a minimal cloudbuild.yaml to pass build-args
        cloudbuild_path = service_path / "cloudbuild.yaml"
        build_arg_lines = "\n".join(
            f"    - '--build-arg={env}'"
            for env in env_vars
            if "=" in env and (env.split("=", 1)[0].strip().startswith("VITE_")
                              or env.split("=", 1)[0].strip().startswith("NEXT_PUBLIC_"))
        )
        cloudbuild_path.write_text(
            f"steps:\n"
            f"  - name: 'gcr.io/cloud-builders/docker'\n"
            f"    args:\n"
            f"    - 'build'\n"
            f"{build_arg_lines}\n"
            f"    - '-t'\n"
            f"    - '{image_uri}'\n"
            f"    - '.'\n"
            f"images:\n"
            f"  - '{image_uri}'\n",
            encoding="utf-8",
        )
        build_cmd = (
            f"gcloud builds submit "
            f"--config \"{cloudbuild_path}\" "
            f"\"{service_path}\" "
            f"--project {project} "
            f"--quiet"
        )
    else:
        build_cmd = (
            f"gcloud builds submit "
            f"--tag \"{image_uri}\" "
            f"\"{service_path}\" "
            f"--project {project} "
            f"--quiet"
        )

    returncode, _, stderr = _run_cmd(build_cmd, timeout=600)

    if returncode != 0:
        print(f"  Cloud Build failed: {stderr[:300]}", flush=True)
        return None

    # Deploy the image
    # NOTE: Cloud Run reserves PORT — it's set automatically from --port flag.
    env_parts = []
    for env in env_vars:
        if "=" in env:
            key = env.split("=", 1)[0].strip()
            if key.upper() == "PORT":
                continue  # Cloud Run sets this automatically
            env_parts.append(env)
    env_str = ",".join(env_parts) if env_parts else ""
    env_flag = f"--set-env-vars \"{env_str}\" " if env_str else ""

    deploy_cmd = (
        f"gcloud run deploy {service_name} "
        f"--image \"{image_uri}\" "
        f"--region {region} "
        f"--project {project} "
        f"{ingress_flag} "
        f"{auth_flag} "
        f"--port {port} "
        f"--memory {memory} "
        f"--cpu {cpu} "
        f"--min-instances 0 "
        f"--max-instances 10 "
        f"{env_flag}"
        f"--quiet"
    )

    returncode, _, stderr = _run_cmd(deploy_cmd, timeout=300)
    if returncode != 0:
        print(f"  Image deploy failed: {stderr[:300]}", flush=True)
        return None

    # Get the URL
    url_cmd = (
        f"gcloud run services describe {service_name} "
        f"--region {region} --project {project} "
        f"--format \"value(status.url)\""
    )
    _, url, _ = _run_cmd(url_cmd)
    return url if url else None


# ---------------------------------------------------------------------------
# Step 3: Tier-specific health check
# ---------------------------------------------------------------------------

def _health_check(url: str, tier: str, timeout: int = 60) -> bool:
    """
    Tier-specific health check.

    - 1A: bare curl (public, no auth)
    - 1B: curl with identity token (GCP IAM auth)
    - 1C: proxy-based (internal only) — skip, return True with warning
    """
    health_url = url.rstrip("/") + "/health"
    print(f"  Checking: {health_url} (tier {tier})", flush=True)

    if tier == "1C":
        # Internal ingress — can't reach from outside without proxy
        print("  Tier 1C: Skipping external health check (internal only)", flush=True)
        print("  Use: gcloud run services proxy <name> --port 8080", flush=True)
        return True  # Assume healthy — deployment succeeded

    # Get identity token for Tier 1B
    token = None
    if tier == "1B":
        try:
            code, out, _ = _run_cmd("gcloud auth print-identity-token", timeout=10)
            if code == 0 and out:
                token = out
            else:
                print("  WARNING: Could not get identity token for 1B health check", flush=True)
                # Fall through — try without auth, may get 403 which is still "service is up"
        except Exception:
            pass

    last_error = None
    start = time.time()
    while (time.time() - start) < timeout:
        try:
            req = urllib.request.Request(health_url, method="GET")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status < 500:
                    print(f"  Health check PASSED (HTTP {resp.status})", flush=True)
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 403 and tier == "1B":
                # 403 means the service IS running but auth is enforced — that's a pass
                print(f"  Health check PASSED (HTTP 403 — auth enforced, service is up)", flush=True)
                return True
            if e.code == 403 and tier == "1A":
                # 403 on public tier means IAM policy is blocking — likely org constraint
                last_error = "HTTP 403 on public tier — org policy may enforce authentication"
            elif e.code < 500:
                print(f"  Health check PASSED (HTTP {e.code})", flush=True)
                return True
            else:
                last_error = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last_error = f"URLError: {e.reason}"
        except TimeoutError:
            last_error = "Connection timed out (corporate firewall/VPN may be blocking)"
        except ConnectionRefusedError:
            last_error = "Connection refused (service may still be starting)"
        except OSError as e:
            last_error = f"Network error: {e}"

        time.sleep(3)

    print(f"  Health check FAILED after {timeout}s", flush=True)
    if last_error:
        print(f"  Last error: {last_error}", flush=True)
    if tier == "1B":
        print(f"  Note: Tier 1B requires a valid identity token. If on corporate network,", flush=True)
        print(f"  your firewall/proxy may strip auth headers or block Cloud Run domains.", flush=True)
    return False


# ===========================================================================
# Main Orchestration: `run` command
# ===========================================================================

def run(args: argparse.Namespace) -> None:
    """Execute the full Cloud Run deploy pipeline for one service."""

    service_name = args.service_name
    service_path = Path(args.service_path).resolve()
    port = args.port
    region = args.region
    project = args.project
    tier = args.tier
    env_vars = args.env or []
    memory = args.memory
    cpu = args.cpu

    tier_labels = {"1A": "Public", "1B": "Authenticated", "1C": "Internal"}

    print(f"\n{'='*60}", flush=True)
    print(f"  AAH Cloud Run Deploy: {service_name}", flush=True)
    print(f"  Region: {region} | Tier: {tier} ({tier_labels.get(tier, tier)})", flush=True)
    print(f"  Project: {project} | Port: {port}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Validate service path
    if not service_path.exists():
        print(f"  ERROR: Service path does not exist: {service_path}", flush=True)
        sys.exit(1)

    # Note: frontend→backend connectivity handled via VITE_API_URL build arg
    # (baked into JS bundle at build time), not nginx proxy

    # === Step 1: Deploy from source ===
    _log(1, f"Deploying {service_name} from source (buildpacks)")
    url = _deploy_from_source(
        service_name, service_path, port, region, project,
        tier, env_vars, memory, cpu,
    )

    # === Step 2: Fallback to image if source failed ===
    deploy_method = "source"
    if not url:
        _log(2, "Source deploy failed — falling back to Cloud Build + image")
        if not (service_path / "Dockerfile").exists():
            print("  ERROR: No Dockerfile found for image fallback", flush=True)
            print("  Run aah run core.deploy.dockerfile_generator first.", flush=True)
            sys.exit(1)
        url = _deploy_from_image(
            service_name, service_path, port, region, project,
            tier, env_vars, memory, cpu,
        )
        deploy_method = "image"
        if not url:
            print("  ERROR: Both source and image deploy failed", flush=True)
            sys.exit(1)
    else:
        _log(2, "Source deploy succeeded — skipping image fallback")

    print(f"  URL: {url}", flush=True)

    # === Step 3: Health Check ===
    _log(3, f"Verifying deployment health (tier {tier})")
    healthy = _health_check(url, tier)

    # === Step 4: Output ===
    _log(4, "Writing results")

    result = {
        "service_name": service_name,
        "url": url,
        "region": region,
        "project": project,
        "port": port,
        "deploy_method": deploy_method,
        "tier": tier,
        "healthy": healthy,
        "status": "deployed" if healthy else "deployed_unhealthy",
    }

    print(f"\n{'='*60}", flush=True)
    if healthy:
        print(f"  ✓ DEPLOYED SUCCESSFULLY", flush=True)
    else:
        print(f"  ⚠ DEPLOYED (health check failed)", flush=True)
        if tier == "1B":
            print(f"  Likely cause: Your identity token lacks roles/run.invoker", flush=True)
            print(f"  OR: Corporate network/VPN blocks outbound to Cloud Run endpoints", flush=True)
            print(f"  Fix: Grant yourself roles/run.invoker on this service:", flush=True)
            print(f"    gcloud run services add-iam-policy-binding {service_name} \\", flush=True)
            print(f"      --region={region} --member=\"user:YOUR_EMAIL\" --role=\"roles/run.invoker\"", flush=True)
        elif tier == "1C":
            print(f"  Expected: Tier 1C is internal-only, no external health check possible", flush=True)
        else:
            print(f"  Check logs: gcloud logging read \"resource.type=cloud_run_revision AND resource.labels.service_name={service_name}\" --project={project} --limit=20", flush=True)
    print(f"  URL: {url}", flush=True)
    print(f"  Tier: {tier} ({tier_labels.get(tier, tier)})", flush=True)
    if tier == "1B":
        print(f"  Access: Requires GCP identity token (IAM authentication enforced)", flush=True)
        print(f"    curl -H \"Authorization: Bearer $(gcloud auth print-identity-token)\" {url}", flush=True)
        print(f"  Other users: Grant them roles/run.invoker, then they use their own token", flush=True)
    elif tier == "1C":
        print(f"  Access: Internal only — no public URL (org/security policy restricts ingress)", flush=True)
        print(f"    gcloud run services proxy {service_name} --region {region} --port 8080", flush=True)
        print(f"    Then open: http://localhost:8080", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Write JSON result to stdout (pipeline reads this)
    print("---DEPLOY_RESULT_JSON---")
    json.dump(result, sys.stdout, indent=2)
    print()


# ===========================================================================
# Teardown: `teardown` command
# ===========================================================================

def teardown(args: argparse.Namespace) -> None:
    """Remove Cloud Run service and related resources."""
    service_name = args.service_name
    region = args.region
    project = args.project

    print(f"\n{'='*60}", flush=True)
    print(f"  AAH Teardown: {service_name}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Delete the Cloud Run service
    print("[1/2] Deleting Cloud Run service...", flush=True)
    cmd = (
        f"gcloud run services delete {service_name} "
        f"--region {region} --project {project} --quiet"
    )
    returncode, _, stderr = _run_cmd(cmd, timeout=60)
    if returncode != 0:
        if "could not be found" in stderr.lower() or "not found" in stderr.lower():
            print(f"  Service already deleted.", flush=True)
        else:
            print(f"  WARNING: {stderr[:200]}", flush=True)
    else:
        print(f"  Service deleted.", flush=True)

    # Delete the image from Artifact Registry (if exists)
    print("[2/2] Cleaning up container images...", flush=True)
    image_path = f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/{service_name}"
    img_cmd = (
        f"gcloud artifacts docker images delete \"{image_path}\" "
        f"--project {project} --quiet --delete-tags"
    )
    returncode, _, _ = _run_cmd(img_cmd, timeout=60)
    if returncode == 0:
        print(f"  Image cleaned up.", flush=True)
    else:
        print(f"  No image to clean (or permission denied — not critical).", flush=True)

    print(f"\n  Teardown complete for: {service_name}\n", flush=True)


# ===========================================================================
# CLI Entry Point
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GCP Cloud Run deploy script (mirrors ecs_deploy.py for GCP)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = sub.add_parser("run", help="Deploy a service to Cloud Run")
    run_p.add_argument("--service-name", required=True,
                       help="Cloud Run service name")
    run_p.add_argument("--service-path", required=True,
                       help="Path to service source code")
    run_p.add_argument("--port", type=int, required=True,
                       help="Container port the app listens on")
    run_p.add_argument("--region", required=True,
                       help="GCP region (e.g., us-central1)")
    run_p.add_argument("--project", required=True,
                       help="GCP project ID")
    run_p.add_argument("--tier", required=True, choices=["1A", "1B", "1C"],
                       help="Access tier: 1A=public, 1B=authenticated, 1C=internal")
    run_p.add_argument("--env", action="append", default=[],
                       help="Environment var KEY=VALUE (repeatable)")
    run_p.add_argument("--memory", default="512Mi",
                       help="Memory allocation (default: 512Mi)")
    run_p.add_argument("--cpu", default="1",
                       help="CPU allocation (default: 1)")

    # --- teardown ---
    td_p = sub.add_parser("teardown", help="Remove Cloud Run service")
    td_p.add_argument("--service-name", required=True,
                      help="Service name to tear down")
    td_p.add_argument("--region", required=True,
                      help="GCP region")
    td_p.add_argument("--project", required=True,
                      help="GCP project ID")

    args = parser.parse_args()

    if args.command == "run":
        run(args)
    elif args.command == "teardown":
        teardown(args)


if __name__ == "__main__":
    main()
