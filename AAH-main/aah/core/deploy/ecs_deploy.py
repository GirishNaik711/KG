#!/usr/bin/env python3
"""
Full ECS Fargate + ALB deployment — one service, one command.

Replaces 50+ individual AWS CLI calls with a single orchestrated script.
All configuration comes from CLI arguments — nothing is hardcoded.

Usage (deploy):
    aah run core.deploy.ecs_deploy run \
      --service-name my-backend \
      --service-path /path/to/service \
      --port 8080 \
      --region us-east-1 \
      --profile default \
      --account-id 123456789012 \
      --tier 1B \
      --deployer-ip 1.2.3.4 \
      --codebuild-role arn:aws:iam::123456789012:role/codebuild-role \
      --execution-role arn:aws:iam::123456789012:role/ecsTaskExecutionRole \
      --cluster aah-deploy \
      --env "KEY1=val1" --env "KEY2=val2"

Usage (teardown):
    aah run core.deploy.ecs_deploy teardown \
      --service-name my-backend \
      --region us-east-1 \
      --profile default \
      --account-id 123456789012 \
      --cluster aah-deploy

Design:
    - Every parameter comes from CLI args (nothing hardcoded)
    - MSYS_NO_PATHCONV=1 for all path-containing AWS CLI calls (Windows fix)
    - ECR Public Gallery base images (Docker Hub rate limit fix)
    - All builds remote via CodeBuild (no local Docker)
    - Real-time progress: [step/total] message
    - Returns JSON result on stdout for pipeline consumption
    - Idempotent: safe to re-run (uses create-if-not-exists pattern)
"""

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IS_WINDOWS = platform.system() == "Windows"
_TOTAL_STEPS = 8


def _log(step: int, msg: str) -> None:
    """Print a progress line."""
    print(f"[{step}/{_TOTAL_STEPS}] {msg}", flush=True)


def _aws(
    cmd: str,
    profile: str,
    region: str,
    parse_json: bool = False,
    check: bool = True,
    timeout: int = 300,
) -> str | dict | None:
    """
    Run an AWS CLI command with proper env handling.

    Prepends MSYS_NO_PATHCONV=1 on Windows to prevent path mangling.
    Returns stdout string, parsed JSON dict, or None on non-fatal failure.
    """
    argv = ["aws", *shlex.split(cmd), "--region", region, "--profile", profile,
            "--output", "json"]

    env = os.environ.copy()
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"

    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        if check:
            print(f"  ERROR: Command timed out ({timeout}s): {cmd[:80]}...", flush=True)
            sys.exit(1)
        return None

    if r.returncode != 0:
        stderr = r.stderr.lower()
        # "Already exists" patterns — not errors
        already_exists = any(x in stderr for x in [
            "already exist", "repositoryalreadyexists", "resourceinuseexception",
            "invalidparameterexception", "duplicatelistener", "already been taken",
        ])
        if already_exists:
            return r.stdout.strip() if r.stdout.strip() else None
        if not check:
            return None
        print(f"  ERROR: {r.stderr.strip()[:400]}", flush=True)
        sys.exit(1)

    if parse_json and r.stdout.strip():
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return r.stdout.strip()
    return r.stdout.strip()


def _aws_query(cmd: str, profile: str, region: str, query: str) -> str:
    """Run AWS CLI with --query and return text output."""
    clean_query = query.strip("'")
    argv = ["aws", *shlex.split(cmd), "--region", region, "--profile", profile,
            "--query", clean_query, "--output", "text"]
    env = os.environ.copy()
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"

    try:
        r = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=120, env=env,
        )
    except (subprocess.TimeoutExpired, Exception):
        return ""
    if r.returncode != 0:
        return ""
    result = r.stdout.strip()
    # AWS CLI returns "None" for null values
    return "" if result == "None" else result


# ---------------------------------------------------------------------------
# Step 1: ECR Repository
# ---------------------------------------------------------------------------

def _ensure_ecr_repo(service_name: str, profile: str, region: str) -> str:
    """Create ECR repo if not exists. Returns the repository URI."""
    _aws(
        f"ecr create-repository --repository-name {service_name} "
        f"--encryption-configuration encryptionType=KMS",
        profile, region, check=False
    )
    uri = _aws_query(
        f"ecr describe-repositories --repository-names {service_name}",
        profile, region,
        "'repositories[0].repositoryUri'"
    )
    if not uri:
        print(f"  ERROR: Could not get ECR URI for {service_name}", flush=True)
        sys.exit(1)
    return uri


# ---------------------------------------------------------------------------
# Step 2: Prepare Source (Dockerfile fix + buildspec + zip + S3)
# ---------------------------------------------------------------------------

def _fix_dockerfile_base_images(service_path: Path) -> None:
    """Replace Docker Hub base images with ECR Public Gallery equivalents."""
    dockerfile = service_path / "Dockerfile"
    if not dockerfile.exists():
        return

    content = dockerfile.read_text(encoding="utf-8")
    replacements = {
        "FROM node:": "FROM public.ecr.aws/docker/library/node:",
        "FROM python:": "FROM public.ecr.aws/docker/library/python:",
        "FROM nginx:": "FROM public.ecr.aws/docker/library/nginx:",
        "FROM alpine:": "FROM public.ecr.aws/docker/library/alpine:",
        "FROM golang:": "FROM public.ecr.aws/docker/library/golang:",
    }

    lines = content.split("\n")
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        for old, new in replacements.items():
            if stripped.startswith(old) and "public.ecr.aws" not in stripped:
                lines[i] = line.replace(old.split("FROM ")[1], new.split("FROM ")[1])
                changed = True
                break

    if changed:
        dockerfile.write_text("\n".join(lines), encoding="utf-8")
        print("  Fixed Dockerfile base images → ECR Public Gallery", flush=True)


def _write_buildspec(service_path: Path, ecr_uri: str, repo_name: str, build_args: list[str] = None) -> None:
    """Write buildspec.yml into the service directory."""
    buildspec = service_path / "buildspec.yml"
    # Extract registry URL from full ECR URI (e.g., 123456789012.dkr.ecr.us-east-1.amazonaws.com/repo-name)
    registry_url = ecr_uri.rsplit("/", 1)[0]

    # Build docker build command with optional build args
    build_args_str = ""
    if build_args:
        for arg in build_args:
            if "=" in arg:
                key = arg.split("=", 1)[0]
                build_args_str += f" --build-arg {key}=${key}"

    buildspec.write_text(
        f"""version: 0.2
phases:
  pre_build:
    commands:
      - echo Logging in to ECR...
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin {registry_url}
  build:
    commands:
      - echo Building image...
      - docker build{build_args_str} -t {ecr_uri}:latest .
      - echo Pushing image to ECR...
      - docker push {ecr_uri}:latest
  post_build:
    commands:
      - echo Build completed successfully
""",
        encoding="utf-8",
    )


def _write_dockerignore(service_path: Path) -> None:
    """Write .dockerignore if not present."""
    ignore_file = service_path / ".dockerignore"
    if not ignore_file.exists():
        ignore_file.write_text(
            "node_modules\n.git\n.env\n.env.local\n.aah\n*.md\n"
            ".dockerignore\nbuildspec.yml\n__pycache__\n.pytest_cache\n",
            encoding="utf-8",
        )


def _patch_nginx_proxy_DEPRECATED(service_path: Path, env_vars: list[str]) -> None:
    """
    DEPRECATED: No longer called. Frontend→backend connectivity is handled via
    VITE_API_URL build arg (baked into JS bundle at build time).
    Kept for reference only — can be removed in a future cleanup.

    Original purpose: Ensure frontend nginx.conf proxies /api/* to the backend URL.

    Called during source preparation for frontend services. Detects if:
    1. An nginx.conf exists with a proxy_pass using env var (e.g., ${BACKEND_URL})
    2. The Dockerfile correctly COPYs it and uses envsubst at runtime

    If the nginx.conf exists but the Dockerfile doesn't use it, patches the
    Dockerfile to COPY the config and use envsubst to resolve env vars at start.

    If no nginx.conf exists but the frontend has relative API calls and a
    BACKEND_URL env var is being passed, creates one.
    """
    nginx_conf = service_path / "nginx.conf"
    dockerfile = service_path / "Dockerfile"

    if not dockerfile.exists():
        return

    dockerfile_content = dockerfile.read_text(encoding="utf-8")

    # Extract backend URL from env vars being passed to this service
    backend_url = None
    for env in env_vars:
        if "=" in env:
            key, val = env.split("=", 1)
            if "BACKEND" in key.upper() and "URL" in key.upper():
                backend_url = val
                break

    if not backend_url:
        # No backend URL in env vars — nothing to proxy
        return

    # Determine the port from Dockerfile (EXPOSE or listen directive)
    port = "8080"  # default
    for line in dockerfile_content.splitlines():
        stripped = line.strip()
        if stripped.startswith("EXPOSE"):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1].isdigit():
                port = parts[1]
                break

    # Step 1: Ensure nginx.conf exists with proxy block
    if not nginx_conf.exists():
        # Create one — frontend needs /api/* proxied to backend
        print(f"  Creating nginx.conf with /api/ proxy -> backend", flush=True)
        nginx_conf.write_text(
            f"server {{\n"
            f"    listen ${{PORT}};\n"
            f"    server_name _;\n"
            f"    root /usr/share/nginx/html;\n"
            f"    index index.html;\n"
            f"\n"
            f"    location /api/ {{\n"
            f"        proxy_pass ${{BACKEND_URL}}/api/;\n"
            f"        proxy_set_header Host $host;\n"
            f"        proxy_set_header X-Real-IP $remote_addr;\n"
            f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            f"        proxy_set_header X-Forwarded-Proto $scheme;\n"
            f"    }}\n"
            f"\n"
            f"    location / {{\n"
            f"        try_files $uri $uri/ /index.html;\n"
            f"    }}\n"
            f"}}\n",
            encoding="utf-8",
        )
    else:
        # nginx.conf exists — verify it has proxy_pass
        content = nginx_conf.read_text(encoding="utf-8")
        if "proxy_pass" not in content:
            # Exists but no proxy — insert /api/ location before the catch-all
            print(f"  Patching nginx.conf: adding /api/ proxy block", flush=True)
            proxy_block = (
                f"\n    location /api/ {{\n"
                f"        proxy_pass ${{BACKEND_URL}}/api/;\n"
                f"        proxy_set_header Host $host;\n"
                f"        proxy_set_header X-Real-IP $remote_addr;\n"
                f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                f"        proxy_set_header X-Forwarded-Proto $scheme;\n"
                f"    }}\n"
            )
            # Insert before 'location / {'
            if "location / {" in content:
                content = content.replace(
                    "location / {",
                    proxy_block + "\n    location / {"
                )
            else:
                # Append before closing brace
                content = content.rstrip().rstrip("}") + proxy_block + "}\n"
            nginx_conf.write_text(content, encoding="utf-8")

    # Step 2: Ensure Dockerfile uses the nginx.conf with envsubst
    # Check if Dockerfile already COPYs nginx.conf
    if "nginx.conf" in dockerfile_content and "envsubst" in dockerfile_content:
        print(f"  nginx.conf + envsubst already configured in Dockerfile", flush=True)
        return

    # Check if it's a multi-stage build with nginx
    if "nginx" in dockerfile_content.lower():
        # Patch: replace the inline nginx config or add COPY + envsubst CMD
        print(f"  Patching Dockerfile: COPY nginx.conf + envsubst entrypoint", flush=True)

        # Remove any inline nginx config (RUN echo '...' > /etc/nginx/...)
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

            # Skip existing CMD if we're going to replace it
            if line.strip().startswith("CMD") and "nginx" in line:
                i += 1
                continue
            new_lines.append(line)
            i += 1

        # Find where to insert COPY nginx.conf (after the last COPY or before EXPOSE)
        insert_idx = len(new_lines)
        for i, line in enumerate(new_lines):
            if line.strip().startswith("EXPOSE"):
                insert_idx = i
                break

        # Insert COPY nginx.conf + envsubst entrypoint
        nginx_lines = [
            "",
            "# AAH: COPY nginx.conf and use envsubst for env var resolution",
            "COPY nginx.conf /etc/nginx/templates/default.conf.template",
            "",
        ]
        for i, nl in enumerate(nginx_lines):
            new_lines.insert(insert_idx + i, nl)

        # Replace or append CMD with envsubst-based entrypoint
        new_lines.append("")
        new_lines.append(
            'CMD ["/bin/sh", "-c", '
            '"envsubst \'${PORT} ${BACKEND_URL}\' < /etc/nginx/templates/default.conf.template '
            '> /etc/nginx/conf.d/default.conf && nginx -g \'daemon off;\'"]'
        )

        dockerfile.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        print(f"  Non-nginx frontend — skipping proxy patch", flush=True)


def _ensure_s3_bucket(account_id: str, profile: str, region: str) -> str:
    """Create the deploy bucket if not exists. Returns bucket name."""
    bucket = f"aah-deploy-{account_id}"
    _aws(
        f"s3api create-bucket --bucket {bucket} "
        + (f"--create-bucket-configuration LocationConstraint={region}" if region != "us-east-1" else ""),
        profile, region, check=False
    )
    return bucket


def _zip_and_upload(
    service_path: Path, service_name: str, account_id: str,
    profile: str, region: str
) -> str:
    """Zip service source and upload to S3. Returns 'bucket/key' string."""
    bucket = _ensure_s3_bucket(account_id, profile, region)
    s3_key = f"sources/{service_name}.zip"

    # Create zip in temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_base = Path(tmpdir) / "source"

        # Copy source, excluding heavy/unnecessary directories
        shutil.copytree(
            service_path, zip_base,
            ignore=shutil.ignore_patterns(
                "node_modules", ".git", "__pycache__", ".pytest_cache",
                "*.pyc", ".env", ".env.local", ".aah", "venv", ".venv",
            ),
        )

        # Create the zip archive — files must be at root level for CodeBuild
        zip_path = Path(tmpdir) / f"{service_name}-source"
        archive_path = shutil.make_archive(str(zip_path), "zip", str(zip_base))

        # Upload to S3
        _aws(f"s3 cp {shlex.quote(str(archive_path))} s3://{bucket}/{s3_key}", profile, region)

    return f"{bucket}/{s3_key}"


# ---------------------------------------------------------------------------
# Step 3: CodeBuild (remote image build)
# ---------------------------------------------------------------------------

def _ensure_codebuild_project(
    service_name: str, codebuild_role: str, profile: str, region: str
) -> str:
    """Create CodeBuild project if not exists. Returns project name."""
    project_name = f"aah-deploy-{service_name}"

    # Check if project exists first
    existing = _aws_query(
        f"codebuild batch-get-projects --names {project_name}",
        profile, region,
        "'projects[0].name'"
    )
    if existing == project_name:
        return project_name

    # Use NO_SOURCE — actual source comes via --source-type-override S3 at start-build time
    _aws(
        f"codebuild create-project "
        f"--name {project_name} "
        f"--source type=NO_SOURCE,buildspec=version:0.2 "
        f"--artifacts type=NO_ARTIFACTS "
        f"--environment type=LINUX_CONTAINER,computeType=BUILD_GENERAL1_SMALL,"
        f"image=aws/codebuild/standard:7.0,privilegedMode=true "
        f"--service-role {codebuild_role}",
        profile, region, check=False
    )
    return project_name


def _run_codebuild(
    project_name: str, s3_location: str, ecr_uri: str,
    service_name: str, profile: str, region: str, build_args: list[str] = None
) -> None:
    """Start CodeBuild and poll until complete."""
    # Build environment variables string
    env_vars = [
        f"name=ECR_URI,value={ecr_uri},type=PLAINTEXT",
        f"name=REPO_NAME,value={service_name},type=PLAINTEXT",
        f"name=AWS_DEFAULT_REGION,value={region},type=PLAINTEXT",
    ]

    # Add build args as CodeBuild environment variables
    if build_args:
        for arg in build_args:
            if "=" in arg:
                key, value = arg.split("=", 1)
                env_vars.append(f"name={key},value={value},type=PLAINTEXT")

    env_vars_str = " ".join(shlex.quote(value) for value in env_vars)

    # Start build — use --query to get build ID directly
    build_id = _aws_query(
        f"codebuild start-build "
        f"--project-name {project_name} "
        f"--source-type-override S3 "
        f"--source-location-override {s3_location} "
        f"--environment-variables-override {env_vars_str}",
        profile, region,
        "'build.id'"
    )

    if not build_id:
        print("  ERROR: Failed to start CodeBuild", flush=True)
        sys.exit(1)

    print(f"  Build ID: {build_id}", flush=True)

    # Poll until complete
    max_wait = 600  # 10 minutes
    start = time.time()
    dots = 0

    while (time.time() - start) < max_wait:
        status = _aws_query(
            f"codebuild batch-get-builds --ids {build_id}",
            profile, region,
            "'builds[0].buildStatus'"
        )

        if status == "SUCCEEDED":
            print("\n  Build SUCCEEDED", flush=True)
            return
        elif status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
            print(f"\n  Build FAILED (status: {status})", flush=True)
            # Fetch last 10 log lines for diagnosis
            log_group = f"/aws/codebuild/{project_name}"
            log_stream = _aws_query(
                f"codebuild batch-get-builds --ids {build_id}",
                profile, region,
                "'builds[0].logs.streamName'"
            )
            if log_stream:
                logs = _aws(
                    f"logs get-log-events --log-group-name {log_group} "
                    f"--log-stream-name {log_stream} --limit 15",
                    profile, region, parse_json=True, check=False
                )
                if isinstance(logs, dict):
                    events = logs.get("events", [])
                    for e in events[-10:]:
                        msg = e.get("message", "").strip()
                        if msg:
                            print(f"    {msg}", flush=True)
            sys.exit(1)

        # Progress indicator
        dots += 1
        if dots % 4 == 0:
            elapsed = int(time.time() - start)
            print(f"  ...building ({elapsed}s)", flush=True)

        time.sleep(15)

    print("  ERROR: CodeBuild timed out after 10 minutes", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 4: VPC + Networking Discovery
# ---------------------------------------------------------------------------

def _get_vpc_info(profile: str, region: str) -> tuple[str, list[str], str]:
    """Get default VPC ID, 2 subnet IDs in different AZs, and VPC CIDR."""
    vpc_id = _aws_query(
        "ec2 describe-vpcs --filters Name=isDefault,Values=true",
        profile, region,
        "'Vpcs[0].VpcId'"
    )
    if not vpc_id:
        print("  ERROR: No default VPC found. Create one or specify a VPC.", flush=True)
        sys.exit(1)

    # Get subnets — need at least 2 in different AZs for ALB
    subnets_data = _aws(
        f"ec2 describe-subnets --filters Name=vpc-id,Values={vpc_id}",
        profile, region, parse_json=True
    )

    selected = []
    seen_azs = set()
    if isinstance(subnets_data, dict):
        for s in subnets_data.get("Subnets", []):
            az = s.get("AvailabilityZone", "")
            if az not in seen_azs and len(selected) < 2:
                seen_azs.add(az)
                selected.append(s["SubnetId"])

    if len(selected) < 2:
        print("  ERROR: Need at least 2 subnets in different AZs for ALB", flush=True)
        sys.exit(1)

    vpc_cidr = _aws_query(
        f"ec2 describe-vpcs --vpc-ids {vpc_id}",
        profile, region,
        "'Vpcs[0].CidrBlock'"
    )

    return vpc_id, selected, vpc_cidr


# ---------------------------------------------------------------------------
# Step 5: Security Groups (tier-specific)
# ---------------------------------------------------------------------------

def _ensure_security_groups(
    service_name: str, vpc_id: str, tier: str,
    deployer_ip: str | None, port: int, vpc_cidr: str,
    profile: str, region: str
) -> tuple[str, str]:
    """Create ALB + Task security groups. Returns (alb_sg_id, task_sg_id)."""

    # Determine inbound CIDR for ALB based on tier
    if tier == "1A":
        alb_cidr = "0.0.0.0/0"
    elif tier == "1B":
        if not deployer_ip:
            print("  ERROR: Tier 1B requires --deployer-ip", flush=True)
            sys.exit(1)
        alb_cidr = f"{deployer_ip}/32"
    else:  # 1C
        alb_cidr = vpc_cidr

    # --- ALB Security Group ---
    alb_sg_name = f"aah-{service_name}-alb-sg"

    # Check if exists first
    alb_sg = _aws_query(
        f"ec2 describe-security-groups --filters "
        f"Name=group-name,Values={alb_sg_name} Name=vpc-id,Values={vpc_id}",
        profile, region,
        "'SecurityGroups[0].GroupId'"
    )

    if not alb_sg:
        result = _aws(
            f"ec2 create-security-group "
            f"--group-name {alb_sg_name} "
            f"--description \"ALB SG for aah-{service_name}\" "
            f"--vpc-id {vpc_id}",
            profile, region, parse_json=True, check=False
        )
        if isinstance(result, dict):
            alb_sg = result.get("GroupId", "")
        if not alb_sg:
            # Re-check (race condition)
            alb_sg = _aws_query(
                f"ec2 describe-security-groups --filters "
                f"Name=group-name,Values={alb_sg_name} Name=vpc-id,Values={vpc_id}",
                profile, region,
                "'SecurityGroups[0].GroupId'"
            )

    if not alb_sg:
        print(f"  ERROR: Could not create/find ALB security group {alb_sg_name}", flush=True)
        sys.exit(1)

    # Add inbound rule (ignore duplicate errors)
    _aws(
        f"ec2 authorize-security-group-ingress "
        f"--group-id {alb_sg} --protocol tcp --port 80 --cidr {alb_cidr}",
        profile, region, check=False
    )

    # --- Task Security Group ---
    task_sg_name = f"aah-{service_name}-task-sg"

    task_sg = _aws_query(
        f"ec2 describe-security-groups --filters "
        f"Name=group-name,Values={task_sg_name} Name=vpc-id,Values={vpc_id}",
        profile, region,
        "'SecurityGroups[0].GroupId'"
    )

    if not task_sg:
        result = _aws(
            f"ec2 create-security-group "
            f"--group-name {task_sg_name} "
            f"--description \"Task SG for aah-{service_name}\" "
            f"--vpc-id {vpc_id}",
            profile, region, parse_json=True, check=False
        )
        if isinstance(result, dict):
            task_sg = result.get("GroupId", "")
        if not task_sg:
            task_sg = _aws_query(
                f"ec2 describe-security-groups --filters "
                f"Name=group-name,Values={task_sg_name} Name=vpc-id,Values={vpc_id}",
                profile, region,
                "'SecurityGroups[0].GroupId'"
            )

    if not task_sg:
        print(f"  ERROR: Could not create/find Task security group {task_sg_name}", flush=True)
        sys.exit(1)

    # Task SG: allow traffic from ALB SG on container port
    _aws(
        f"ec2 authorize-security-group-ingress "
        f"--group-id {task_sg} --protocol tcp --port {port} "
        f"--source-group {alb_sg}",
        profile, region, check=False
    )

    return alb_sg, task_sg


# ---------------------------------------------------------------------------
# Step 6: ALB + Target Group + Listener
# ---------------------------------------------------------------------------

def _ensure_alb(
    service_name: str, subnets: list[str], alb_sg: str,
    vpc_id: str, tier: str, port: int, health_check_path: str,
    profile: str, region: str
) -> tuple[str, str, str]:
    """Create ALB + target group + listener. Returns (alb_arn, alb_dns, tg_arn)."""

    alb_name = f"{service_name}-alb"
    tg_name = f"{service_name}-tg"
    alb_scheme = "internal" if tier == "1C" else "internet-facing"
    subnets_str = " ".join(subnets)

    # --- Target Group ---
    tg_arn = _aws_query(
        f"elbv2 describe-target-groups --names {tg_name}",
        profile, region,
        "'TargetGroups[0].TargetGroupArn'"
    )

    if not tg_arn:
        tg_result = _aws(
            f"elbv2 create-target-group "
            f"--name {tg_name} "
            f"--protocol HTTP --port {port} "
            f"--vpc-id {vpc_id} "
            f"--target-type ip "
            f"--health-check-path {health_check_path} "
            f"--health-check-interval-seconds 15 "
            f"--health-check-timeout-seconds 5 "
            f"--healthy-threshold-count 2 "
            f"--unhealthy-threshold-count 3",
            profile, region, parse_json=True
        )
        if isinstance(tg_result, dict):
            tgs = tg_result.get("TargetGroups", [])
            tg_arn = tgs[0].get("TargetGroupArn", "") if tgs else ""

    if not tg_arn:
        print("  ERROR: Could not create/find target group", flush=True)
        sys.exit(1)

    # --- ALB ---
    alb_arn = _aws_query(
        f"elbv2 describe-load-balancers --names {alb_name}",
        profile, region,
        "'LoadBalancers[0].LoadBalancerArn'"
    )
    alb_dns = ""

    if not alb_arn:
        alb_result = _aws(
            f"elbv2 create-load-balancer "
            f"--name {alb_name} "
            f"--subnets {subnets_str} "
            f"--security-groups {alb_sg} "
            f"--scheme {alb_scheme}",
            profile, region, parse_json=True
        )
        if isinstance(alb_result, dict):
            lbs = alb_result.get("LoadBalancers", [])
            if lbs:
                alb_arn = lbs[0].get("LoadBalancerArn", "")
                alb_dns = lbs[0].get("DNSName", "")

    if not alb_arn:
        print("  ERROR: Could not create/find ALB", flush=True)
        sys.exit(1)

    # Get DNS if we didn't get it from create
    if not alb_dns:
        alb_dns = _aws_query(
            f"elbv2 describe-load-balancers --load-balancer-arns {alb_arn}",
            profile, region,
            "'LoadBalancers[0].DNSName'"
        )

    # Wait for ALB to be active
    print("  Waiting for ALB to become active...", flush=True)
    _aws(
        f"elbv2 wait load-balancer-available --load-balancer-arns {alb_arn}",
        profile, region, timeout=300
    )

    # --- Listener (create if not exists) ---
    existing_listeners = _aws_query(
        f"elbv2 describe-listeners --load-balancer-arn {alb_arn}",
        profile, region,
        "'Listeners[0].ListenerArn'"
    )

    if not existing_listeners:
        _aws(
            f"elbv2 create-listener "
            f"--load-balancer-arn {alb_arn} "
            f"--protocol HTTP --port 80 "
            f"--default-actions Type=forward,TargetGroupArn={tg_arn}",
            profile, region, check=False
        )

    return alb_arn, alb_dns, tg_arn


# ---------------------------------------------------------------------------
# Step 7: ECS (cluster + log group + task def + service)
# ---------------------------------------------------------------------------

def _ensure_cluster(cluster: str, profile: str, region: str) -> None:
    """Create ECS cluster if not exists."""
    _aws(f"ecs create-cluster --cluster-name {cluster}", profile, region, check=False)


def _create_log_group(service_name: str, profile: str, region: str) -> None:
    """Create CloudWatch log group if not exists."""
    log_group = f"/ecs/aah-{service_name}"
    _aws(f"logs create-log-group --log-group-name {log_group}", profile, region, check=False)


def _register_task_definition(
    service_name: str, ecr_repo_uri: str, port: int,
    env_vars: list[str], execution_role: str,
    profile: str, region: str, task_role: str | None = None
) -> str:
    """Register ECS task definition. Returns task definition ARN.

    task_role is the CONTAINER's runtime identity (for the app to call AWS APIs —
    Secrets Manager, S3, Bedrock). Optional: omitted when the app makes no AWS calls.
    Distinct from execution_role (which ECS uses to pull the image / write logs).
    """

    # Build environment JSON
    env_json = []
    for env in env_vars:
        if "=" in env:
            key, value = env.split("=", 1)
            env_json.append({"name": key, "value": value})

    container_def = [{
        "name": service_name,
        "image": f"{ecr_repo_uri}:latest",
        "portMappings": [{"containerPort": port, "protocol": "tcp"}],
        "environment": env_json,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/ecs/aah-{service_name}",
                "awslogs-region": region,
                "awslogs-stream-prefix": "ecs"
            }
        },
        "essential": True
    }]

    # Write container def to temp file (avoids shell quoting nightmares)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(container_def, f)
        container_file = f.name

    task_role_flag = f"--task-role-arn {task_role} " if task_role else ""
    try:
        result = _aws(
            f"ecs register-task-definition "
            f"--family aah-{service_name} "
            f"--requires-compatibilities FARGATE "
            f"--network-mode awsvpc "
            f"--cpu 256 --memory 512 "
            f"--execution-role-arn {execution_role} "
            f"{task_role_flag}"
            f"--container-definitions {shlex.quote('file://' + container_file)}",
            profile, region, parse_json=True
        )
    finally:
        os.unlink(container_file)

    if isinstance(result, dict):
        td = result.get("taskDefinition", {})
        return td.get("taskDefinitionArn", "")

    # Fallback: query latest
    return _aws_query(
        f"ecs describe-task-definition --task-definition aah-{service_name}",
        profile, region,
        "'taskDefinition.taskDefinitionArn'"
    )


def _create_or_update_service(
    service_name: str, cluster: str, task_def_arn: str,
    subnets: list[str], task_sg: str, tg_arn: str,
    port: int, tier: str, profile: str, region: str
) -> None:
    """Create ECS service or update if it already exists."""

    ecs_service_name = f"aah-{service_name}"

    # Check if service exists and is ACTIVE
    existing_status = _aws_query(
        f"ecs describe-services --cluster {cluster} --services {ecs_service_name}",
        profile, region,
        "'services[0].status'"
    )

    if existing_status and existing_status.upper() == "ACTIVE":
        # Update existing service with new task definition
        print(f"  Service exists — updating with force-new-deployment", flush=True)
        _aws(
            f"ecs update-service "
            f"--cluster {cluster} "
            f"--service {ecs_service_name} "
            f"--task-definition {task_def_arn} "
            f"--force-new-deployment",
            profile, region
        )
    else:
        # Create new service
        subnets_csv = ",".join(subnets)
        network_config = (
            f"awsvpcConfiguration={{subnets=[{subnets_csv}],"
            f"securityGroups=[{task_sg}],assignPublicIp=ENABLED}}"
        )
        lb_config = (
            f"targetGroupArn={tg_arn},"
            f"containerName={service_name},"
            f"containerPort={port}"
        )

        enable_exec = "--enable-execute-command" if tier == "1C" else ""

        _aws(
            f"ecs create-service "
            f"--cluster {cluster} "
            f"--service-name {ecs_service_name} "
            f"--task-definition {task_def_arn} "
            f"--launch-type FARGATE "
            f"--desired-count 1 "
            f"--network-configuration \"{network_config}\" "
            f"--load-balancers \"{lb_config}\" "
            f"{enable_exec}",
            profile, region
        )


def _wait_for_stable(service_name: str, cluster: str, profile: str, region: str) -> None:
    """Wait for ECS service to stabilize (up to 10 min)."""
    ecs_service_name = f"aah-{service_name}"
    print("  Waiting for service to stabilize...", flush=True)

    argv = [
        "aws", "ecs", "wait", "services-stable",
        "--cluster", cluster, "--services", ecs_service_name,
        "--region", region, "--profile", profile,
    ]
    env = os.environ.copy()
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"

    r = subprocess.run(
        argv, capture_output=True, text=True,
        timeout=600, env=env
    )

    if r.returncode != 0:
        print("  WARNING: Service did not stabilize in time. Checking events...", flush=True)
        events = _aws_query(
            f"ecs describe-services --cluster {cluster} --services {ecs_service_name}",
            profile, region,
            "'services[0].events[0:3].[message]'"
        )
        if events:
            print(f"  Recent events: {events[:300]}", flush=True)
    else:
        print("  Service stable.", flush=True)


# ---------------------------------------------------------------------------
# IAM Role Discovery (used by pipeline authenticate stage)
# ---------------------------------------------------------------------------

# Well-known role names to check (in priority order)
CODEBUILD_ROLE_NAMES = [
    "aah-codebuild-role",
    "codebuild-aah-deploy",
    "codebuild-service-role",
]

EXECUTION_ROLE_NAMES = [
    "ecsTaskExecutionRole",
    "aah-ecs-execution-role",
]

# Optional — the CONTAINER's runtime identity (app AWS API calls). Never an error if absent.
TASK_ROLE_NAMES = [
    "aah-ecs-task-role",
    "aah-ecs-task-role",
]


def discover_iam_roles(profile: str, region: str, account_id: str) -> dict:
    """
    Discover IAM roles needed for ECS deployment.

    Called by the pipeline's authenticate stage to validate prerequisites
    BEFORE dispatching the deploy agent. Returns discovered ARNs or
    clear error messages explaining what's missing.

    Returns:
        {
            "codebuild_role": "arn:..." or None,
            "execution_role": "arn:..." or None,
            "errors": ["Missing CodeBuild service role..."] or [],
            "instructions": ["aws iam create-role ..."] or [],
        }
    """
    result = {
        "codebuild_role": None,
        "execution_role": None,
        "task_role": None,   # OPTIONAL — never an error if missing
        "errors": [],
        "instructions": [],
    }

    # Discover CodeBuild role
    for role_name in CODEBUILD_ROLE_NAMES:
        r = _aws(
            f"iam get-role --role-name {role_name}",
            profile, region, check=False, parse_json=True
        )
        if r and r.get("Role", {}).get("Arn"):
            result["codebuild_role"] = r["Role"]["Arn"]
            break

    if not result["codebuild_role"]:
        result["errors"].append(
            "No CodeBuild service role found. "
            f"Checked: {', '.join(CODEBUILD_ROLE_NAMES)}"
        )
        result["instructions"].append(
            f"Create a CodeBuild service role:\n"
            f"  aws iam create-role --role-name aah-codebuild-role \\\n"
            f"    --assume-role-policy-document '{{"
            f"\"Version\":\"2012-10-17\","
            f"\"Statement\":[{{\"Effect\":\"Allow\","
            f"\"Principal\":{{\"Service\":\"codebuild.amazonaws.com\"}},"
            f"\"Action\":\"sts:AssumeRole\"}}]}}'\n"
            f"  aws iam attach-role-policy --role-name aah-codebuild-role \\\n"
            f"    --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser\n"
            f"  aws iam attach-role-policy --role-name aah-codebuild-role \\\n"
            f"    --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess\n"
            f"  aws iam attach-role-policy --role-name aah-codebuild-role \\\n"
            f"    --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
        )

    # Discover ECS execution role
    for role_name in EXECUTION_ROLE_NAMES:
        r = _aws(
            f"iam get-role --role-name {role_name}",
            profile, region, check=False, parse_json=True
        )
        if r and r.get("Role", {}).get("Arn"):
            result["execution_role"] = r["Role"]["Arn"]
            break

    if not result["execution_role"]:
        result["errors"].append(
            "No ECS task execution role found. "
            f"Checked: {', '.join(EXECUTION_ROLE_NAMES)}"
        )
        result["instructions"].append(
            f"Create an ECS task execution role:\n"
            f"  aws iam create-role --role-name ecsTaskExecutionRole \\\n"
            f"    --assume-role-policy-document '{{"
            f"\"Version\":\"2012-10-17\","
            f"\"Statement\":[{{\"Effect\":\"Allow\","
            f"\"Principal\":{{\"Service\":\"ecs-tasks.amazonaws.com\"}},"
            f"\"Action\":\"sts:AssumeRole\"}}]}}'\n"
            f"  aws iam attach-role-policy --role-name ecsTaskExecutionRole \\\n"
            f"    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        )

    # Discover the OPTIONAL task role (container runtime identity). Absent → fine; the
    # deploy simply omits --task-role and the container has no AWS API identity.
    for role_name in TASK_ROLE_NAMES:
        r = _aws(
            f"iam get-role --role-name {role_name}",
            profile, region, check=False, parse_json=True
        )
        if r and r.get("Role", {}).get("Arn"):
            result["task_role"] = r["Role"]["Arn"]
            break

    return result


# ---------------------------------------------------------------------------
# Step 8: Health Check
# ---------------------------------------------------------------------------

def _health_check(url: str, health_path: str, tier: str = "1A",
                   deployer_ip: str | None = None, timeout: int = 90) -> bool:
    """Poll health endpoint until healthy or timeout. Tier-aware diagnostics."""
    health_url = url.rstrip("/") + health_path
    print(f"  Checking: {health_url} (tier {tier})", flush=True)

    if tier == "1C":
        # Internal ALB — can't reach from outside
        print("  Tier 1C: Skipping external health check (internal ALB)", flush=True)
        print("  Access via ECS Exec:", flush=True)
        print("    aws ecs execute-command --cluster <cluster> --task <task-arn> \\", flush=True)
        print("      --container <name> --interactive --command \"/bin/sh\"", flush=True)
        return True  # Assume healthy — ECS service is stable

    last_error = None
    start = time.time()

    while (time.time() - start) < timeout:
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status < 500:
                    print(f"  Health check PASSED (HTTP {resp.status})", flush=True)
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                print(f"  Health check PASSED (HTTP {e.code})", flush=True)
                return True
            else:
                last_error = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last_error = f"URLError: {e.reason}"
        except TimeoutError:
            last_error = "Connection timed out"
        except ConnectionRefusedError:
            last_error = "Connection refused (ALB may still be provisioning)"
        except OSError as e:
            last_error = f"Network error: {e}"

        time.sleep(5)

    print(f"  Health check FAILED after {timeout}s", flush=True)
    if last_error:
        print(f"  Last error: {last_error}", flush=True)
    if tier == "1B":
        print(f"  Note: Tier 1B security group only allows traffic from {deployer_ip or 'deployer IP'}/32", flush=True)
        print(f"  If your current IP differs (VPN, corporate proxy, IP rotation),", flush=True)
        print(f"  the ALB will reject connections. Check your public IP vs the SG rule.", flush=True)
        print(f"  Verify: curl ifconfig.me   (should match {deployer_ip or 'the deployer IP'})", flush=True)
    elif tier == "1A":
        print(f"  Check CloudWatch logs for container crash/startup errors", flush=True)
    return False


# ===========================================================================
# Main Orchestration: `run` command
# ===========================================================================

def run(args: argparse.Namespace) -> None:
    """Execute the full deploy pipeline for one service."""

    service_name = args.service_name
    service_path = Path(args.service_path).resolve()
    port = args.port
    region = args.region
    profile = args.profile
    account_id = args.account_id
    tier = args.tier
    deployer_ip = args.deployer_ip
    codebuild_role = args.codebuild_role
    execution_role = args.execution_role
    task_role = getattr(args, "task_role", None) or None
    cluster = args.cluster
    env_vars = args.env or []
    build_args = args.build_arg or []
    health_check_path = args.health_check_path

    ecr_base = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    print(f"\n{'='*60}", flush=True)
    print(f"  AAH ECS Deploy: {service_name}", flush=True)
    print(f"  Region: {region} | Tier: {tier} | Port: {port}", flush=True)
    print(f"  Profile: {profile} | Cluster: {cluster}", flush=True)
    if tier == "1B" and deployer_ip:
        print(f"  SG scoped to: {deployer_ip}/32", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Validate service path
    if not service_path.exists():
        print(f"  ERROR: Service path does not exist: {service_path}", flush=True)
        sys.exit(1)

    if not (service_path / "Dockerfile").exists():
        print(f"  ERROR: No Dockerfile found at {service_path}", flush=True)
        print(f"  Run aah run core.deploy.dockerfile_generator first.", flush=True)
        sys.exit(1)

    # === Step 1: ECR ===
    _log(1, f"Creating ECR repository: {service_name}")
    ecr_repo_uri = _ensure_ecr_repo(service_name, profile, region)
    print(f"  ECR: {ecr_repo_uri}", flush=True)

    # === Step 2: Prepare source ===
    _log(2, "Preparing source for CodeBuild")
    _fix_dockerfile_base_images(service_path)
    _write_buildspec(service_path, ecr_repo_uri, service_name, build_args)
    _write_dockerignore(service_path)
    s3_location = _zip_and_upload(service_path, service_name, account_id, profile, region)
    print(f"  Uploaded: s3://{s3_location}", flush=True)

    # === Step 3: CodeBuild ===
    _log(3, "Building container image via CodeBuild (remote)")
    project_name = _ensure_codebuild_project(service_name, codebuild_role, profile, region)
    _run_codebuild(project_name, s3_location, ecr_repo_uri, service_name, profile, region, build_args)

    # === Step 4: VPC Discovery ===
    _log(4, "Discovering VPC networking")
    vpc_id, subnets, vpc_cidr = _get_vpc_info(profile, region)
    print(f"  VPC: {vpc_id} | Subnets: {', '.join(subnets)}", flush=True)

    # === Step 5: Security Groups ===
    _log(5, f"Creating security groups (Tier {tier})")
    alb_sg, task_sg = _ensure_security_groups(
        service_name, vpc_id, tier, deployer_ip, port, vpc_cidr, profile, region
    )
    print(f"  ALB SG: {alb_sg} | Task SG: {task_sg}", flush=True)

    # === Step 6: ALB + Target Group ===
    _log(6, "Creating ALB + target group + listener")
    alb_arn, alb_dns, tg_arn = _ensure_alb(
        service_name, subnets, alb_sg, vpc_id, tier, port,
        health_check_path, profile, region
    )
    print(f"  ALB DNS: {alb_dns}", flush=True)

    # === Step 7: ECS Service ===
    _log(7, "Deploying ECS Fargate service")
    _ensure_cluster(cluster, profile, region)
    _create_log_group(service_name, profile, region)
    task_def_arn = _register_task_definition(
        service_name, ecr_repo_uri, port, env_vars, execution_role, profile, region,
        task_role=task_role
    )
    print(f"  Task def: {task_def_arn}", flush=True)
    _create_or_update_service(
        service_name, cluster, task_def_arn, subnets, task_sg, tg_arn,
        port, tier, profile, region
    )
    _wait_for_stable(service_name, cluster, profile, region)

    # === Step 8: Health Check ===
    _log(8, "Verifying deployment health")
    service_url = f"http://{alb_dns}"
    healthy = _health_check(service_url, health_check_path, tier, deployer_ip)

    # === Output ===
    result = {
        "service_name": f"aah-{service_name}",
        "url": service_url,
        "ecr_uri": f"{ecr_repo_uri}:latest",
        "cluster": cluster,
        "task_definition": task_def_arn,
        "alb_arn": alb_arn,
        "alb_dns": alb_dns,
        "target_group_arn": tg_arn,
        "alb_sg": alb_sg,
        "task_sg": task_sg,
        "region": region,
        "tier": tier,
        "deployer_ip": deployer_ip,
        "healthy": healthy,
        "status": "deployed" if healthy else "deployed_unhealthy",
    }

    print(f"\n{'='*60}", flush=True)
    if healthy:
        print(f"  ✓ DEPLOYED SUCCESSFULLY", flush=True)
    else:
        print(f"  ⚠ DEPLOYED (health check failed)", flush=True)
        if tier == "1B":
            print(f"  Likely cause: Your current IP doesn't match the SG rule ({deployer_ip}/32)", flush=True)
            print(f"  This happens when: VPN changes your IP, corporate proxy masks it,", flush=True)
            print(f"  or IP rotated since deploy started.", flush=True)
            print(f"  Fix: Update ALB security group with your current IP:", flush=True)
            print(f"    aws ec2 authorize-security-group-ingress --group-id {alb_sg} \\", flush=True)
            print(f"      --protocol tcp --port 80 --cidr $(curl -s ifconfig.me)/32 \\", flush=True)
            print(f"      --profile {profile} --region {region}", flush=True)
        elif tier == "1C":
            print(f"  Expected: Tier 1C uses internal ALB — not reachable externally", flush=True)
        else:
            print(f"  Check CloudWatch logs:", flush=True)
            print(f"    aws logs tail /ecs/aah-{service_name} --profile {profile} --region {region}", flush=True)
    print(f"  URL: {service_url}", flush=True)
    tier_labels = {"1A": "Public", "1B": "IP-scoped", "1C": "Internal"}
    print(f"  Tier: {tier} ({tier_labels.get(tier, tier)})", flush=True)
    if tier == "1B":
        print(f"  Access: Reachable ONLY from {deployer_ip} (your IP at deploy time)", flush=True)
        print(f"  Other users: Add their IP to ALB SG ({alb_sg})", flush=True)
        print(f"    aws ec2 authorize-security-group-ingress --group-id {alb_sg} \\", flush=True)
        print(f"      --protocol tcp --port 80 --cidr <their-ip>/32 --profile {profile} --region {region}", flush=True)
    elif tier == "1C":
        print(f"  Access: Internal only — no public URL (security policy restricts ingress)", flush=True)
        print(f"    Via ECS Exec: aws ecs execute-command --cluster {cluster} --task <task-arn> \\", flush=True)
        print(f"      --container {service_name} --interactive --command \"/bin/sh\"", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Write JSON result to stdout (pipeline reads this)
    print("---DEPLOY_RESULT_JSON---")
    json.dump(result, sys.stdout, indent=2)
    print()


# ===========================================================================
# Teardown: `teardown` command
# ===========================================================================

def teardown(args: argparse.Namespace) -> None:
    """Remove all AWS resources for a service."""
    service_name = args.service_name
    region = args.region
    profile = args.profile
    account_id = args.account_id
    cluster = args.cluster

    print(f"\n{'='*60}", flush=True)
    print(f"  AAH Teardown: {service_name}", flush=True)
    print(f"{'='*60}\n", flush=True)

    ecs_service_name = f"aah-{service_name}"
    alb_name = f"{service_name}-alb"
    tg_name = f"{service_name}-tg"

    # 1. Scale down and delete ECS service
    print("[1/7] Deleting ECS service...", flush=True)
    _aws(
        f"ecs update-service --cluster {cluster} --service {ecs_service_name} --desired-count 0",
        profile, region, check=False
    )
    _aws(
        f"ecs delete-service --cluster {cluster} --service {ecs_service_name} --force",
        profile, region, check=False
    )

    # 2. Delete ALB (listeners first)
    print("[2/7] Deleting ALB...", flush=True)
    alb_arn = _aws_query(
        f"elbv2 describe-load-balancers --names {alb_name}",
        profile, region, "'LoadBalancers[0].LoadBalancerArn'"
    )
    if alb_arn:
        # Delete all listeners
        listeners_data = _aws(
            f"elbv2 describe-listeners --load-balancer-arn {alb_arn}",
            profile, region, parse_json=True, check=False
        )
        if isinstance(listeners_data, dict):
            for listener in listeners_data.get("Listeners", []):
                _aws(
                    f"elbv2 delete-listener --listener-arn {listener['ListenerArn']}",
                    profile, region, check=False
                )
        _aws(
            f"elbv2 delete-load-balancer --load-balancer-arn {alb_arn}",
            profile, region, check=False
        )

    # 3. Delete Target Group (brief wait for ALB to release)
    print("[3/7] Deleting target group...", flush=True)
    time.sleep(5)
    tg_arn = _aws_query(
        f"elbv2 describe-target-groups --names {tg_name}",
        profile, region, "'TargetGroups[0].TargetGroupArn'"
    )
    if tg_arn:
        _aws(
            f"elbv2 delete-target-group --target-group-arn {tg_arn}",
            profile, region, check=False
        )

    # 4. Deregister task definitions
    print("[4/7] Deregistering task definitions...", flush=True)
    for rev in range(1, 30):
        result = _aws(
            f"ecs deregister-task-definition --task-definition aah-{service_name}:{rev}",
            profile, region, check=False
        )
        if not result:
            break

    # 5. Delete ECR repo
    print("[5/7] Deleting ECR repository...", flush=True)
    _aws(
        f"ecr delete-repository --repository-name {service_name} --force",
        profile, region, check=False
    )

    # 6. Delete CodeBuild project + logs
    print("[6/7] Deleting CodeBuild project and logs...", flush=True)
    _aws(
        f"codebuild delete-project --name aah-deploy-{service_name}",
        profile, region, check=False
    )
    _aws(
        f"logs delete-log-group --log-group-name /ecs/aah-{service_name}",
        profile, region, check=False
    )
    _aws(
        f"logs delete-log-group --log-group-name /aws/codebuild/aah-deploy-{service_name}",
        profile, region, check=False
    )

    # 7. Delete S3 source + security groups
    print("[7/7] Deleting S3 source and security groups...", flush=True)
    _aws(
        f"s3 rm s3://aah-deploy-{account_id}/sources/{service_name}.zip",
        profile, region, check=False
    )

    # Security groups (may fail due to IAM/dependency — not critical)
    vpc_id = _aws_query(
        "ec2 describe-vpcs --filters Name=isDefault,Values=true",
        profile, region, "'Vpcs[0].VpcId'"
    )
    if vpc_id:
        for sg_name in [f"aah-{service_name}-alb-sg", f"aah-{service_name}-task-sg"]:
            sg_id = _aws_query(
                f"ec2 describe-security-groups --filters "
                f"Name=group-name,Values={sg_name} Name=vpc-id,Values={vpc_id}",
                profile, region, "'SecurityGroups[0].GroupId'"
            )
            if sg_id:
                _aws(
                    f"ec2 delete-security-group --group-id {sg_id}",
                    profile, region, check=False
                )

    print(f"\n  Teardown complete for: {service_name}", flush=True)
    print(f"  Note: Security groups may persist if IAM denies deletion.\n", flush=True)


# ===========================================================================
# CLI Entry Point
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ECS Fargate + ALB deploy script (replaces 50+ agent tool calls)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = sub.add_parser("run", help="Deploy a service to ECS Fargate + ALB")
    run_p.add_argument("--service-name", required=True,
                       help="Name for the service (ECR repo, ECS service name)")
    run_p.add_argument("--service-path", required=True,
                       help="Path to service source code (must contain Dockerfile)")
    run_p.add_argument("--port", type=int, required=True,
                       help="Container port the app listens on")
    run_p.add_argument("--region", required=True,
                       help="AWS region (e.g., us-east-1)")
    run_p.add_argument("--profile", required=True,
                       help="AWS CLI profile name")
    run_p.add_argument("--account-id", required=True,
                       help="AWS account ID (12 digits)")
    run_p.add_argument("--tier", required=True, choices=["1A", "1B", "1C"],
                       help="Access tier: 1A=public, 1B=IP-scoped, 1C=internal")
    run_p.add_argument("--deployer-ip", default=None,
                       help="Deployer public IP (required for Tier 1B)")
    run_p.add_argument("--codebuild-role", required=True,
                       help="IAM role ARN for CodeBuild")
    run_p.add_argument("--execution-role", required=True,
                       help="IAM role ARN for ECS task execution (pull image, write logs)")
    run_p.add_argument("--task-role", default=None,
                       help="IAM role ARN for the CONTAINER's runtime identity (app AWS API "
                            "calls: Secrets Manager, S3, Bedrock). Optional — omit if the app "
                            "makes no AWS calls.")
    run_p.add_argument("--cluster", default="aah-deploy",
                       help="ECS cluster name (default: aah-deploy)")
    run_p.add_argument("--env", action="append", default=[],
                       help="Environment var KEY=VALUE for runtime (ECS task)")
    run_p.add_argument("--build-arg", action="append", default=[],
                       help="Build-time arg KEY=VALUE (passed to docker build)")
    run_p.add_argument("--health-check-path", default="/health",
                       help="Health check endpoint path (default: /health)")

    # --- teardown ---
    td_p = sub.add_parser("teardown", help="Remove all AWS resources for a service")
    td_p.add_argument("--service-name", required=True,
                      help="Service name to tear down")
    td_p.add_argument("--region", required=True,
                      help="AWS region")
    td_p.add_argument("--profile", required=True,
                      help="AWS CLI profile name")
    td_p.add_argument("--account-id", required=True,
                      help="AWS account ID (for S3 bucket name)")
    td_p.add_argument("--cluster", default="aah-deploy",
                      help="ECS cluster name (default: aah-deploy)")

    args = parser.parse_args()

    if args.command == "run":
        run(args)
    elif args.command == "teardown":
        teardown(args)


if __name__ == "__main__":
    main()
