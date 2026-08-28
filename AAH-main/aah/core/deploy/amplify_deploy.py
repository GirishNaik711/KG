#!/usr/bin/env python3
"""
AWS Amplify Hosting deploy (non-git zip-upload path) — standardized CLI in one script.

Owns the deterministic `aws amplify` sequence so the deploy agent never types raw
CLI: prereq validation, frontend build (env baked at build time), app/branch
create, env vars, the atomic create-deployment -> upload -> start-deployment flow
(with stale-PENDING-job cancellation and TLS verification unconditionally disabled for
corporate-proxy environments), job polling, the SPA 404-200 rewrite rule, status, and
teardown. Before upload, verifies the zip's contents actually match the build directory
(catches a truncated zip before it ships); after the job succeeds, verifies the deployed
site's real asset URLs resolve — not just the root URL, which the SPA rewrite makes
return 200 unconditionally and so can't detect a missing-assets deploy on its own.

No Amplify CLI or SDK — only `aws amplify` subcommands + a presigned PUT upload.
All configuration comes from CLI args.

Usage:
    aah run core.deploy.amplify_deploy validate-prereqs
    aah run core.deploy.amplify_deploy build --frontend-dir /path/frontend \
        --api-endpoint https://proxy.example --env-var-name VITE_API_ENDPOINT
    aah run core.deploy.amplify_deploy deploy --frontend-dir /path/frontend \
        --project-path /path [for the pollable progress file] \
        --app-name myapp-frontend --branch main --region us-east-1 --profile default \
        --api-endpoint https://proxy.example [--env-var-name VITE_API_ENDPOINT] [--skip-build]
    aah run core.deploy.amplify_deploy status   --app-id d123 --region us-east-1 --profile default
    aah run core.deploy.amplify_deploy teardown --app-id d123 --region us-east-1 --profile default

Design (mirrors ecs_deploy.py): MSYS_NO_PATHCONV on Windows, JSON after
---DEPLOY_RESULT_JSON---, idempotent create-if-not-exists.
"""

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


_IS_WINDOWS = platform.system() == "Windows"


_last_error: str | None = None


def _aws(cmd: str, profile: str, region: str, parse_json: bool = False,
         check: bool = True, timeout: int = 120):
    """Run an `aws` CLI command; return stdout str / parsed dict / None. On failure under
    check=False, `_last_error` carries the real stderr (also printed here) instead of
    being silently discarded — a caller used to just see None with no diagnostic."""
    global _last_error
    # AWS CLI applies last-flag-wins for repeated --output (verified empirically). Several
    # call sites (_ensure_app, _cancel_stale_jobs, _poll_job) embed --output text expecting
    # a plain scalar — blindly appending --output json here silently overrode every one of
    # them (the real value arrives JSON-quoted, e.g. '"PENDING"', breaking equality/`in`
    # checks downstream — _poll_job in particular exited on its first iteration reporting a
    # bogus "final" status). Only default to json when the caller hasn't already chosen.
    argv = ["aws", *shlex.split(cmd), "--region", region, "--profile", profile]
    if not re.search(r"--output\b", cmd):
        argv.extend(["--output", "json"])
    env = os.environ.copy()
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _last_error = f"timed out after {timeout}s: {cmd[:80]}"
        if check:
            print(f"  ERROR: aws command timed out: {cmd[:80]}", flush=True)
            sys.exit(1)
        return None
    if r.returncode != 0:
        _last_error = (r.stderr or r.stdout or "").strip()[:500]
        if not check:
            print(f"  WARN: {cmd[:80]} failed: {_last_error}", flush=True)
            return None
        print(f"  ERROR: {_last_error}", flush=True)
        sys.exit(1)
    if parse_json and r.stdout.strip():
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return r.stdout.strip()
    return r.stdout.strip()


def _emit(result: dict) -> None:
    print("---DEPLOY_RESULT_JSON---")
    json.dump(result, sys.stdout, indent=2)
    print()


def _progress_path(project_path: Path | None, frontend_dir: Path) -> Path:
    # Prefer the explicit --project-path; fall back to the frontend dir's parent (the
    # common case: frontend/ is a subdir of the AAH project root).
    base = project_path or frontend_dir.parent
    return base / ".aah" / "deploy" / "amplify-progress.json"


def _write_progress(path: Path, stage: str, detail: str = "") -> None:
    """Write the current stage to a small on-disk file — deploy() takes minutes (npm
    build + zip upload + job poll) and raw print() output is collapsed in the harness.
    A caller can poll this file (a plain Read, no shell) to narrate live progress
    instead of the user seeing nothing until the whole command returns."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": stage, "detail": detail[:300]}, indent=2), encoding="utf-8")
    except OSError:
        pass  # progress narration is best-effort — never fail the deploy over it


def _npm(cmd: str, cwd: Path, timeout: int = 600,
         extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    if _IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"
    argv = shlex.split(cmd)
    argv[0] = shutil.which(argv[0]) or argv[0]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          cwd=str(cwd), env=env, encoding="utf-8", errors="replace")


def _detect_build_dir(frontend_dir: Path) -> Path | None:
    for name in ("dist", "build", "out"):
        d = frontend_dir / name
        if d.is_dir() and any(d.iterdir()):
            return d
    return None


# ---------------------------------------------------------------------------
# validate-prereqs
# ---------------------------------------------------------------------------

def validate_prereqs(args: argparse.Namespace) -> None:
    errors: list[str] = []
    for tool, label in [("aws --version", "AWS CLI v2"), ("node --version", "Node.js"),
                        ("npm --version", "npm")]:
        env = os.environ.copy()
        if _IS_WINDOWS:
            env["MSYS_NO_PATHCONV"] = "1"
        argv = shlex.split(tool)
        argv[0] = shutil.which(argv[0]) or argv[0]
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
        if r.returncode != 0:
            errors.append(f"{label} not found ({tool}).")
    _emit({
        "command": "validate-prereqs",
        "errors": errors,
        "passed": not errors,
        "iam_permissions_needed": [
            "amplify:CreateApp", "amplify:CreateBranch", "amplify:CreateDeployment",
            "amplify:StartDeployment", "amplify:UpdateApp", "amplify:GetApp",
            "amplify:GetBranch", "amplify:GetJob", "amplify:ListJobs",
            "amplify:StopJob", "amplify:DeleteApp",
        ],
    })
    if errors:
        sys.exit(1)


# ---------------------------------------------------------------------------
# build  (env baked at build time — VITE_*/NEXT_PUBLIC_* are compile-time)
# ---------------------------------------------------------------------------

def build(args: argparse.Namespace) -> dict:
    frontend_dir = Path(args.frontend_dir).resolve()
    if not (frontend_dir / "package.json").exists():
        print(f"  ERROR: no package.json in {frontend_dir}", flush=True)
        sys.exit(1)

    env_name = args.env_var_name or "VITE_API_ENDPOINT"

    print(f"[1/2] Installing deps ({frontend_dir.name})", flush=True)
    r = _npm("npm ci", frontend_dir)
    if r.returncode != 0:
        print("  npm ci failed — falling back to npm install", flush=True)
        r = _npm("npm install", frontend_dir)
        if r.returncode != 0:
            print(f"  ERROR: dependency install failed:\n{r.stderr[:400]}", flush=True)
            sys.exit(1)

    print(f"[2/2] Building ({env_name} baked in)", flush=True)
    # Bake the backend endpoint into the bundle at build time.
    build_env = {env_name: args.api_endpoint} if args.api_endpoint else None
    r = _npm("npm run build", frontend_dir, extra_env=build_env)
    if r.returncode != 0:
        print(f"  ERROR: build failed:\n{r.stderr[:400]}", flush=True)
        sys.exit(1)

    build_dir = _detect_build_dir(frontend_dir)
    if not build_dir:
        print("  ERROR: no build output (dist/build/out) produced", flush=True)
        sys.exit(1)

    result = {
        "command": "build",
        "frontend_dir": str(frontend_dir),
        "build_dir": str(build_dir),
        "api_endpoint": args.api_endpoint,
        "env_var": env_name,
    }
    return result


def _cmd_build(args: argparse.Namespace) -> None:
    _emit(build(args))


# ---------------------------------------------------------------------------
# app + branch + env
# ---------------------------------------------------------------------------

def _ensure_app(app_name: str, profile: str, region: str) -> str:
    existing = _aws(
        f'amplify list-apps --query "apps[?name==\'{app_name}\'].appId" --output text',
        profile, region, check=False,
    )
    # list-apps with --query/--output text returns plain text, not JSON
    if isinstance(existing, str) and existing.strip() and existing.strip() != "None":
        return existing.strip().split()[0]
    created = _aws(f'amplify create-app --name "{app_name}"', profile, region, parse_json=True)
    if isinstance(created, dict):
        return created.get("app", {}).get("appId", "")
    print("  ERROR: could not create/find Amplify app", flush=True)
    sys.exit(1)


def _ensure_branch(app_id: str, branch: str, profile: str, region: str) -> None:
    _aws(f"amplify create-branch --app-id {app_id} --branch-name {branch}",
         profile, region, check=False)


def _set_env(app_id: str, env_name: str, value: str, profile: str, region: str) -> None:
    _aws(f'amplify update-app --app-id {app_id} --environment-variables "{env_name}={value}"',
         profile, region, check=False)


def _cancel_stale_jobs(app_id: str, branch: str, profile: str, region: str) -> None:
    pending = _aws(
        f'amplify list-jobs --app-id {app_id} --branch-name {branch} '
        f'--query "jobSummaries[?status==\'PENDING\'].jobId" --output text',
        profile, region, check=False,
    )
    if isinstance(pending, str):
        for jid in pending.split():
            if jid and jid != "None":
                _aws(f"amplify stop-job --app-id {app_id} --branch-name {branch} --job-id {jid}",
                     profile, region, check=False)
                print(f"  Cancelled stale job {jid}", flush=True)


def _zip_build_dir(build_dir: Path) -> Path:
    zip_path = build_dir.parent / "frontend-build.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(build_dir):
            for f in files:
                fp = Path(root) / f
                z.write(fp, fp.relative_to(build_dir).as_posix())
    return zip_path


def _verify_zip_complete(zip_path: Path, build_dir: Path) -> list[str]:
    """Compare what actually landed in the zip against what's on disk in build_dir —
    catches a truncated/incomplete zip (e.g. index.html written but assets/ dropped)
    BEFORE upload, instead of discovering it later as a blank page with 404s on every
    JS/CSS asset (this happened in practice — the deploy job reported SUCCEED, the root
    URL returned 200 via the SPA rewrite, and nothing caught it until a live check on the
    actual asset URLs). Returns the list of expected-but-missing relative paths (empty if
    complete)."""
    expected = set()
    for root, _dirs, files in os.walk(build_dir):
        for f in files:
            fp = Path(root) / f
            expected.add(fp.relative_to(build_dir).as_posix())
    with zipfile.ZipFile(zip_path) as z:
        actual = {n.replace("\\", "/") for n in z.namelist()}
    return sorted(expected - actual)


def _upload_zip(zip_path: Path, url: str) -> int:
    """PUT the zip to the presigned URL. urllib with relaxed TLS (corporate proxies)."""
    data = zip_path.read_bytes()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("Content-Type", "application/zip")
    with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
        return resp.status


# ---------------------------------------------------------------------------
# deploy  (full: build -> app -> branch -> env -> atomic zip deploy -> poll -> spa)
# ---------------------------------------------------------------------------

def deploy(args: argparse.Namespace) -> None:
    frontend_dir = Path(args.frontend_dir).resolve()
    profile, region = args.profile, args.region
    branch = args.branch
    env_name = args.env_var_name or "VITE_API_ENDPOINT"
    progress = _progress_path(Path(args.project_path).resolve() if getattr(args, "project_path", None) else None,
                              frontend_dir)

    print(f"\n{'='*60}\n  Amplify Deploy: {args.app_name} (branch={branch})\n{'='*60}\n", flush=True)

    # Build (unless caller already built)
    _write_progress(progress, "build", "npm install + npm run build")
    if args.skip_build:
        build_dir = _detect_build_dir(frontend_dir)
        if not build_dir:
            print("  ERROR: --skip-build but no build output found", flush=True)
            sys.exit(1)
    else:
        build_res = build(argparse.Namespace(
            frontend_dir=str(frontend_dir), api_endpoint=args.api_endpoint, env_var_name=env_name,
        ))
        build_dir = Path(build_res["build_dir"])

    print("[1/6] Ensuring Amplify app", flush=True)
    _write_progress(progress, "1/6 ensure-app")
    app_id = args.app_id or _ensure_app(args.app_name, profile, region)
    print(f"  App ID: {app_id}", flush=True)

    print("[2/6] Ensuring branch", flush=True)
    _write_progress(progress, "2/6 ensure-branch")
    _ensure_branch(app_id, branch, profile, region)

    if args.api_endpoint:
        print("[3/6] Setting environment variables", flush=True)
        _write_progress(progress, "3/6 set-env")
        _set_env(app_id, env_name, args.api_endpoint, profile, region)

    print("[4/6] Zipping + deploying (atomic create->upload->start)", flush=True)
    _write_progress(progress, "4/6 zip-upload")
    _cancel_stale_jobs(app_id, branch, profile, region)
    zip_path = _zip_build_dir(build_dir)
    missing = _verify_zip_complete(zip_path, build_dir)
    if missing:
        print(f"  ERROR: zip is incomplete — {len(missing)} file(s) present in {build_dir} "
              f"never made it into the zip: {missing[:10]}", flush=True)
        _write_progress(progress, "failed", f"zip missing {len(missing)} file(s), e.g. {missing[0]}")
        _emit({"command": "deploy", "status": "failed",
               "error": f"zip incomplete before upload — missing: {missing[:10]}"})
        sys.exit(1)
    dep = _aws(f"amplify create-deployment --app-id {app_id} --branch-name {branch}",
               profile, region, parse_json=True)
    if not isinstance(dep, dict) or "zipUploadUrl" not in dep:
        print("  ERROR: create-deployment did not return a zipUploadUrl", flush=True)
        _write_progress(progress, "failed", "create-deployment did not return a zipUploadUrl")
        sys.exit(1)
    job_id, upload_url = dep["jobId"], dep["zipUploadUrl"]
    print(f"  Job ID: {job_id} — uploading {zip_path.stat().st_size // 1024}KB", flush=True)
    try:
        status_code = _upload_zip(zip_path, upload_url)
    except Exception as e:
        # The presigned URL is time-limited (~15 min per AWS's own docs for
        # create-deployment) and this is a plain network PUT — either can fail for real
        # reasons (expired URL, network blip). This used to raise an uncaught HTTPError/
        # URLError straight out of deploy(), crashing with a raw traceback instead of the
        # graceful failure path every other error in this function goes through.
        err = f"zip upload to presigned URL failed: {e}"
        print(f"  ERROR: {err}", flush=True)
        _write_progress(progress, "failed", err)
        _emit({"command": "deploy", "status": "failed", "error": err})
        sys.exit(1)
    print(f"  Upload HTTP {status_code}", flush=True)
    _aws(f"amplify start-deployment --app-id {app_id} --branch-name {branch} --job-id {job_id}",
         profile, region)

    print("[5/6] Polling deployment", flush=True)
    _write_progress(progress, "5/6 polling", "amplify build job — can take a few minutes")
    final_status = _poll_job(app_id, branch, job_id, profile, region)

    print("[6/6] Applying SPA rewrite rule (404-200)", flush=True)
    _write_progress(progress, "6/6 spa-rewrite")
    _aws(
        f"amplify update-app --app-id {app_id} "
        f"""--custom-rules '[{{"source":"/<*>","target":"/index.html","status":"404-200"}}]'""",
        profile, region, check=False,
    )

    app_url = f"https://{branch}.{app_id}.amplifyapp.com"
    reachable = _check_url(app_url)
    # A job status of SUCCEED + reachable root URL is NOT enough — the SPA rewrite makes
    # every path return 200, so a truncated upload (index.html deployed, assets/ dropped)
    # is invisible to both checks. Verify the actual asset URLs the deployed page
    # references, not just the root.
    assets_ok, asset_issues = (
        _verify_deployed_assets(app_url) if (final_status == "SUCCEED" and reachable)
        else (False, ["skipped — job did not succeed or root URL unreachable"])
    )
    result = {
        "command": "deploy",
        "app_name": args.app_name,
        "app_id": app_id,
        "branch": branch,
        "job_id": job_id,
        "app_url": app_url,
        "method": "zip",
        "region": region,
        "api_endpoint": args.api_endpoint,
        "env_var": env_name,
        "job_status": final_status,
        "reachable": reachable,
        "assets_verified": assets_ok,
        "asset_issues": asset_issues if not assets_ok else [],
        "status": "deployed" if (final_status == "SUCCEED" and assets_ok) else (
            f"job_{final_status.lower()}" if final_status != "SUCCEED" else "assets_missing"
        ),
    }
    print(f"\n  App URL: {app_url}  (job {final_status}, assets_verified={assets_ok})\n", flush=True)
    _write_progress(progress, "done" if (final_status == "SUCCEED" and assets_ok) else "failed",
                    f"job {final_status}" if final_status != "SUCCEED" else f"assets missing: {asset_issues[:3]}")
    _emit(result)
    if final_status != "SUCCEED" or not assets_ok:
        if final_status == "SUCCEED" and not assets_ok:
            print(f"  ERROR: deploy job succeeded but deployed assets are unreachable: "
                  f"{asset_issues[:5]}", flush=True)
        sys.exit(1)


def _poll_job(app_id: str, branch: str, job_id: str, profile: str, region: str,
              max_wait: int = 300) -> str:
    start = time.time()
    while (time.time() - start) < max_wait:
        st = _aws(
            f'amplify get-job --app-id {app_id} --branch-name {branch} --job-id {job_id} '
            f'--query "job.summary.status" --output text',
            profile, region, check=False,
        )
        st = (st or "").strip() if isinstance(st, str) else ""
        if st and st not in ("PENDING", "RUNNING", "PROVISIONING"):
            print(f"  Job status: {st}", flush=True)
            return st
        if st:
            print(f"  ...{st}", flush=True)
        time.sleep(10)
    return "TIMEOUT"


def _check_url(url: str, retries: int = 3, delay: int = 8) -> bool:
    """True if the URL responds without raising. `urlopen` raises `HTTPError` for any
    non-2xx/3xx status by default, so a `resp.status` check inside a successful `with`
    block is unreachable for a real failure — the exception path IS the check.

    Retries with the same delay `_verify_deployed_assets` uses for CDN propagation lag —
    this is the gate checked immediately before it, so a single flaky attempt here was
    skipping the (correctly retried) asset verification entirely rather than giving the
    CDN the same grace period."""
    for attempt in range(1, retries + 1):
        try:
            with _fetch(url):
                return True
        except Exception:
            if attempt < retries:
                time.sleep(delay)
    return False


def _fetch(url: str, timeout: int = 15):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method="GET")
    return urllib.request.urlopen(req, context=ctx, timeout=timeout)


def _verify_deployed_assets(app_url: str, retries: int = 3, delay: int = 8) -> tuple[bool, list[str]]:
    """The root URL alone can't detect a truncated deploy: the SPA rewrite rule makes
    EVERY path return 200 (rewritten to index.html), so a missing assets/ folder never
    shows up as a bad root-URL status — it showed up in practice as a live blank page
    with every JS/CSS bundle 404ing, while `reachable: true` reported success throughout.
    Fetch the REAL deployed index.html, extract its actual <script src>/<link href>
    asset paths, and verify each one individually. Retries with a short delay absorb a
    brief CDN propagation lag right after the job completes, without masking a genuine
    missing-asset failure.
    """
    last_issues: list[str] = []
    for attempt in range(1, retries + 1):
        try:
            with _fetch(app_url) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_issues = [f"could not fetch {app_url}: {e}"]
            time.sleep(delay)
            continue
        asset_paths = sorted(set(re.findall(r'(?:src|href)="(/[^"]+\.(?:js|css))"', html)))
        if not asset_paths:
            last_issues = ["no local <script>/<link> asset references found in deployed index.html"]
            time.sleep(delay)
            continue
        issues = []
        for path in asset_paths:
            url = app_url.rstrip("/") + path
            try:
                # `urlopen` raises HTTPError for any non-2xx/3xx by default — reaching
                # the `with` body without an exception already means success; there is
                # no reachable failure status to check here.
                with _fetch(url):
                    pass
            except urllib.error.HTTPError as e:
                issues.append(f"{path} -> HTTP {e.code}")
            except Exception as e:
                issues.append(f"{path} -> {e}")
        if not issues:
            return True, []
        last_issues = issues
        if attempt < retries:
            time.sleep(delay)
    return False, last_issues


def status(args: argparse.Namespace) -> None:
    app = _aws(f"amplify get-app --app-id {args.app_id}", args.profile, args.region,
               parse_json=True, check=False)
    branches = _aws(f"amplify list-branches --app-id {args.app_id}", args.profile, args.region,
                    parse_json=True, check=False)
    _emit({
        "command": "status",
        "app": app.get("app", {}) if isinstance(app, dict) else None,
        "branches": [b.get("branchName") for b in branches.get("branches", [])]
        if isinstance(branches, dict) else [],
    })


def teardown(args: argparse.Namespace) -> None:
    print(f"[1/1] Deleting Amplify app {args.app_id}", flush=True)
    _aws(f"amplify delete-app --app-id {args.app_id}", args.profile, args.region, check=False)
    _emit({"command": "teardown", "app_id": args.app_id, "status": "deleted"})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="AWS Amplify Hosting deploy (zip-upload path)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-prereqs", help="Check aws/node/npm + list needed IAM perms")

    bp = sub.add_parser("build", help="npm ci + npm run build (bakes API endpoint)")
    bp.add_argument("--frontend-dir", required=True)
    bp.add_argument("--api-endpoint", default=None)
    bp.add_argument("--env-var-name", default="VITE_API_ENDPOINT")

    dp = sub.add_parser("deploy", help="Full build -> app -> zip deploy -> poll -> SPA rewrite")
    dp.add_argument("--frontend-dir", required=True)
    dp.add_argument("--project-path", default=None,
                    help="AAH project root, for the pollable progress file at "
                         ".aah/deploy/amplify-progress.json (defaults to --frontend-dir's parent)")
    dp.add_argument("--app-name", required=True)
    dp.add_argument("--app-id", default=None, help="Reuse an existing app instead of creating")
    dp.add_argument("--branch", default="main")
    dp.add_argument("--region", required=True)
    dp.add_argument("--profile", required=True)
    dp.add_argument("--api-endpoint", default=None, help="Backend/proxy URL baked into the build")
    dp.add_argument("--env-var-name", default="VITE_API_ENDPOINT")
    dp.add_argument("--skip-build", action="store_true", help="Use existing build output")

    stp = sub.add_parser("status", help="get-app + list-branches")
    stp.add_argument("--app-id", required=True)
    stp.add_argument("--region", required=True)
    stp.add_argument("--profile", required=True)

    tp = sub.add_parser("teardown", help="delete-app")
    tp.add_argument("--app-id", required=True)
    tp.add_argument("--region", required=True)
    tp.add_argument("--profile", required=True)

    args = p.parse_args()
    {
        "validate-prereqs": validate_prereqs,
        "build": _cmd_build,
        "deploy": deploy,
        "status": status,
        "teardown": teardown,
    }[args.command](args)


if __name__ == "__main__":
    main()
