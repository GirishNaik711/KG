#!/usr/bin/env python3
"""
Deploy a SigV4 signing-proxy Lambda behind an API Gateway in front of an AgentCore Runtime.

Browsers cannot sign AgentCore's required SigV4 requests. This script stands up a
Lambda that receives plain HTTP from the frontend, validates the API key at the edge,
signs with SigV4, and forwards to the AgentCore Runtime. The public front door is an
**API Gateway (HTTP API) — the only front door**; Lambda Function URLs are NOT used
(this account class blocks public Function URLs via SCP, and a single deterministic
front door avoids duplicate/racing endpoints).

Deterministic CLI only (package -> IAM role -> Lambda -> API Gateway). Gate its
use on "the agent is called from a browser" — backend/CI callers sign directly.

Usage:
    aah run core.deploy.agentcore_proxy deploy --project-name QuizBot \
        --runtime-arn arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc \
        --region us-east-1 --profile default [--work-dir /path/proxy_lambda] \
        [--allowed-origin https://main.d123.amplifyapp.com]
    aah run core.deploy.agentcore_proxy update-origin --project-name QuizBot \
        --origin https://main.d123.amplifyapp.com --region us-east-1 --profile default
    aah run core.deploy.agentcore_proxy teardown --project-name QuizBot --region us-east-1 --profile default

Note: --project-name scopes the Lambda/role/API names per project — required on every
subcommand so two projects in the same account/region never collide on shared resources.

Mirrors ecs_deploy.py conventions (MSYS_NO_PATHCONV, JSON after ---DEPLOY_RESULT_JSON---,
idempotent create-or-update).
"""

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path


_IS_WINDOWS = platform.system() == "Windows"


def _slug(name: str) -> str:
    """Lowercase, hyphen-safe slug for an AWS resource name."""
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", name.strip().lower()).strip("-")
    return s or "app"


# Project-scoped — NOT bare constants. Two projects deployed in the same account/region
# used to collide on one shared Lambda/role/API: a second project's `deploy` would find
# the first project's resources by these exact names and silently repoint AGENT_ARN at
# itself, and `update-origin`/`teardown` would act on whichever project ran last. Every
# resource name below must include the project name.
def _function_name(project_name: str) -> str:
    return f"agentcore-signing-proxy-{_slug(project_name)}"[:64]


def _role_name(project_name: str) -> str:
    return f"agentcore-proxy-lambda-role-{_slug(project_name)}"[:64]

# The Lambda handler — parses SSE / plain-JSON / raw AgentCore responses. It OWNS CORS
# (on every path incl. the 401 and the OPTIONS preflight) because edge auth must let the
# browser read a rejection. API Gateway is left with NO CORS config so it doesn't add a
# duplicate Access-Control-Allow-Origin (two ACAO headers break the browser). The origin
# it allows comes from ALLOWED_ORIGIN (see `--allowed-origin` / `update-origin` below) —
# it does NOT default to "*" for a real browser deploy.
_LAMBDA_SRC = r'''"""SigV4 signing proxy: browser -> API Gateway -> (edge auth + sign) -> AgentCore Runtime.

Uses AGENT_REGION (AWS_REGION is reserved by the Lambda runtime). This handler sets CORS
headers itself on all paths; API Gateway is configured with NO native CORS.
"""
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import quote

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_ARN = os.environ["AGENT_ARN"]
REGION = os.environ.get("AGENT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
# Edge auth: AgentCore does NOT forward custom headers to the container, so the API
# key is validated HERE (browser -> API Gateway can send it). Optional: if no secret
# path is configured, the proxy forwards unauthenticated (SigV4 remains the boundary).
SECRET_PATH = os.environ.get("SECRETS_MANAGER_SECRET_PATH", "")
SECRET_REGION = os.environ.get("SECRETS_MANAGER_REGION", REGION)
# The frontend origin to allow. "*" is a deliberate fallback for server-to-server/no-
# browser-frontend deploys (no ALLOWED_ORIGIN passed) — a browser deploy should ALWAYS
# get a real value here (--allowed-origin at proxy-deploy time, or `update-origin` once
# the Amplify URL is known), per the no-wildcard-CORS-in-production rule.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
http = urllib3.PoolManager()

_CACHED_KEY_HASH = None


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type,x-api-key",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Vary": "Origin",
    }


def _stored_key_hash():
    """Fetch + cache the hashed_api_key from Secrets Manager for this warm Lambda."""
    global _CACHED_KEY_HASH
    if _CACHED_KEY_HASH is None:
        sm = boto3.client("secretsmanager", region_name=SECRET_REGION)
        secret = json.loads(sm.get_secret_value(SecretId=SECRET_PATH)["SecretString"])
        _CACHED_KEY_HASH = secret["hashed_api_key"]
    return _CACHED_KEY_HASH


def _authorized(headers):
    """True iff SHA-256(presented x-api-key) matches the stored hash (constant-time)."""
    if not SECRET_PATH:
        return True  # no key configured -> proxy does not gate (SigV4 still applies)
    presented = headers.get("x-api-key") or headers.get("X-API-Key")
    if not presented:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).hexdigest(), _stored_key_hash()
    )


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    headers = event.get("headers", {}) or {}

    # CORS preflight must be answered BEFORE auth (browsers send no key on OPTIONS).
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": _cors_headers(), "body": ""}

    # Edge API-key auth (AgentCore can't do it downstream).
    if not _authorized(headers):
        return {"statusCode": 401, "headers": _cors_headers(),
                "body": json.dumps({"error": "unauthorized", "message": "Missing or invalid API key"})}

    body = json.loads(event.get("body", "{}"))

    escaped_arn = quote(AGENT_ARN, safe="")
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{escaped_arn}/invocations"

    # Forward the client body VERBATIM. The runtime owns payload interpretation — it
    # accepts input/message/prompt aliases for chat turns AND routes sub-actions via an
    # `action` field (e.g. list_sessions/load_session). Rewriting the body here (to just
    # {"prompt","user_id"}) silently drops action/input/session_id/actor_id/whatever else
    # the app's real contract needs — sub-actions turn into chat turns (wrong SSE/JSON
    # shape back to the caller) and every chat turn loses its actual content. This
    # happened in practice: "list_sessions" got routed as a chat message, and pasted
    # content never reached the graph. The proxy is a transparent SigV4 signer, not a
    # payload shaper — it must not assume or rebuild the app's invoke contract.
    payload = json.dumps(body)
    session = boto3.Session(region_name=REGION)
    credentials = session.get_credentials().get_frozen_credentials()
    request = AWSRequest(method="POST", url=url, data=payload,
                         headers={"Content-Type": "application/json"})
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)

    response = http.request("POST", url, body=payload, headers=dict(request.headers),
                            timeout=urllib3.Timeout(total=120))
    raw = response.data.decode("utf-8")
    upstream_ct = response.headers.get("Content-Type", "") if response.headers else ""

    # AG-UI protocol responses are an SSE event stream (RUN_STARTED / TEXT_MESSAGE_* /
    # TOOL_CALL_* / RUN_FINISHED, ...), not a JSON object. This proxy CANNOT relay them
    # incrementally — API Gateway HTTP API + a buffered Lambda-proxy integration has no
    # partial-response support (true token-by-token streaming needs a Lambda Function URL
    # with RESPONSE_STREAM invoke mode, which this account class blocks via SCP; see the
    # proxy-front-door options). So the client gets the full run in one shot, not live —
    # but it MUST still get valid AG-UI events, not a mismatched JSON body. Forward the
    # complete SSE payload verbatim rather than collapsing it into an unrelated shape.
    if "text/event-stream" in upstream_ct or raw.lstrip().startswith("data:"):
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/event-stream", **_cors_headers()},
            "body": raw,
        }

    # Plain JSON response (chat-turn result, or a sub-action like list_sessions/
    # load_session). Forward it VERBATIM — same principle as the request side: the proxy
    # signs, it does not reshape. The old code unwrapped/rewrapped via
    # j.get("result", j.get("response", raw)), which mangled any response shape that
    # wasn't exactly {"result": ...} or {"response": ...} — e.g. list_sessions returning
    # {"sessions": [...]} fell through to the raw-string fallback and got double-encoded
    # as {"response": "<the original JSON as a string>"}, breaking every sub-action.
    try:
        json.loads(raw)  # validate it's actually JSON before promising Content-Type: application/json
        body = raw
    except json.JSONDecodeError:
        body = json.dumps({"response": raw})
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", **_cors_headers()},
        "body": body,
    }
'''

_ASSUME_ROLE = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
)


_last_error: str | None = None


def _aws(cmd: str, profile: str, region: str, parse_json: bool = False,
         check: bool = True, timeout: int = 180):
    """Returns stdout (str/parsed JSON) on success, None on failure. Callers that only
    check truthiness on failure still get the real reason: `_last_error` is set to the
    actual stderr right before any None return, so a caller can surface it instead of
    guessing (e.g. "check permissions/SCP") when the command failed for a real reason."""
    global _last_error
    # AWS CLI applies last-flag-wins for repeated --output (verified empirically). Many
    # call sites embed --output text in `cmd` expecting a plain scalar back — blindly
    # appending --output json here silently overrode every one of them (the real value
    # arrives JSON-quoted, e.g. '"arn:aws:..."', breaking every .startswith()/equality
    # check downstream). Only default to json when the caller hasn't already chosen.
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
            print(f"  ERROR: aws timed out: {cmd[:80]}", flush=True)
            sys.exit(1)
        return None
    if r.returncode != 0:
        stderr = r.stderr.lower()
        if any(x in stderr for x in ["already exist", "entityalreadyexists", "resourceconflict"]):
            return r.stdout.strip() or None
        _last_error = (r.stderr or r.stdout or "").strip()[:500]
        if not check:
            # This used to return None and silently discard stderr — a caller further up
            # (e.g. _deploy_api_gateway) would then report a generic guess instead of the
            # real AWS error. Print it here too so it's visible even if the caller doesn't
            # relay `_last_error`.
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


def _package(work_dir: Path) -> Path:
    """Write the handler, install deps, zip. Returns the zip path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "lambda_function.py").write_text(_LAMBDA_SRC, encoding="utf-8")
    print("  Installing boto3/urllib3 into package", flush=True)
    r = subprocess.run(
        [shutil.which("uv") or "uv", "pip", "install", "--target",
         str(work_dir), "boto3", "urllib3", "--quiet"],
        timeout=300, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        # This used to go unchecked — a failed install would silently ship a Lambda zip
        # missing its dependencies, surfacing later as an opaque ImportError at
        # invocation time instead of a clear failure right here at package time.
        print(f"  ERROR: pip install failed: {(r.stderr or r.stdout).strip()[:400]}", flush=True)
        _emit({"command": "deploy", "status": "failed",
               "error": f"pip install boto3/urllib3 failed: {(r.stderr or r.stdout).strip()[:400]}"})
        sys.exit(1)
    zip_path = work_dir.parent / "proxy-lambda.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(work_dir):
            for f in files:
                fp = Path(root) / f
                z.write(fp, fp.relative_to(work_dir).as_posix())
    return zip_path


def _api_name(project_name: str) -> str:
    return f"agentcore-proxy-api-{_slug(project_name)}"


def _deploy_api_gateway(project_name: str, profile: str, region: str) -> tuple[str | None, str | None]:
    """Public API Gateway HTTP API in front of the proxy Lambda — the ONLY front door.

    Uses payload format 2.0, so the handler event shape is browser-friendly. API Gateway
    is used exclusively (not Lambda Function URLs): this account class blocks public
    Function URLs via SCP, and a single deterministic front door avoids the duplicate/
    racing-endpoint mess. Idempotent — reuses the API if it already exists.

    Returns (api_endpoint, error_detail). error_detail carries the REAL underlying AWS
    error when api_endpoint is None — this used to be silently discarded (every `_aws`
    call here uses check=False), so a real failure (bad permissions, malformed target,
    whatever) surfaced only as a generic "check apigatewayv2 permissions/SCP" guess.
    """
    print("[4/4] Front-door: API Gateway (HTTP API)", flush=True)
    function_name = _function_name(project_name)
    api_name = _api_name(project_name)
    fn_arn = _aws(f'lambda get-function --function-name {function_name} '
                  f'--query "Configuration.FunctionArn" --output text', profile, region, check=False)
    fn_arn = str(fn_arn).strip()
    if not fn_arn.startswith("arn:"):
        return None, _last_error or f"lambda get-function returned no ARN (got {fn_arn!r})"
    # Quick-create: builds the Lambda-proxy integration, a $default route + stage.
    # Idempotency: HTTP API names are NOT unique (identity is ApiId), so `create-api`
    # would succeed and spawn a DUPLICATE endpoint on every run. Look up an existing
    # API by name FIRST; only create if none exists.
    existing = _aws(
        f'apigatewayv2 get-apis --query '
        f'"Items[?Name==\'{api_name}\'].ApiEndpoint | [0]" --output text',
        profile, region, check=False,
    )
    existing = str(existing).strip() if existing else ""
    if existing and existing not in ("None", ""):
        api_endpoint = existing
    else:
        api = _aws(
            f"apigatewayv2 create-api --name {api_name} --protocol-type HTTP "
            f"--target {fn_arn} --query \"ApiEndpoint\" --output text",
            profile, region, check=False,
        )
        api_endpoint = str(api).strip() if api else None
    if not api_endpoint:
        return None, _last_error or "apigatewayv2 create-api returned no ApiEndpoint"
    # Grant API Gateway permission to invoke the Lambda (quick-create usually adds this;
    # do it idempotently to be safe).
    _aws(f"lambda add-permission --function-name {function_name} "
         f"--statement-id apigw-invoke --action lambda:InvokeFunction "
         f"--principal apigateway.amazonaws.com", profile, region, check=False)
    return api_endpoint.rstrip("/"), None


def deploy(args: argparse.Namespace) -> None:
    profile, region = args.profile, args.region
    runtime_arn = args.runtime_arn
    project_name = args.project_name
    work_dir = Path(args.work_dir).resolve() if args.work_dir else Path.cwd() / "proxy_lambda"
    function_name = _function_name(project_name)
    role_name = _role_name(project_name)

    print(f"\n{'='*60}\n  AgentCore Signing Proxy (region={region})\n{'='*60}\n", flush=True)

    print("[1/4] Packaging Lambda", flush=True)
    zip_path = _package(work_dir)

    print("[2/4] Ensuring IAM role", flush=True)
    role = _aws(
        f"iam create-role --role-name {role_name} "
        f"--assume-role-policy-document '{_ASSUME_ROLE}' --query \"Role.Arn\" --output text",
        profile, region, check=False,
    )
    if not role or not str(role).startswith("arn:"):
        role = _aws(f'iam get-role --role-name {role_name} --query "Role.Arn" --output text',
                    profile, region, check=False)
    role_arn = str(role).strip()
    _aws(f"iam attach-role-policy --role-name {role_name} "
         f"--policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
         profile, region, check=False)
    # Resource ARN must end with * — AgentCore checks .../runtime-endpoint/DEFAULT.
    invoke_policy = (
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
        '"Action":"bedrock-agentcore:InvokeAgentRuntime",'
        f'"Resource":"{runtime_arn}*"}}]}}'
    )
    _aws(f"iam put-role-policy --role-name {role_name} --policy-name agentcore-invoke "
         f"--policy-document '{invoke_policy}'", profile, region, check=False)
    # If edge auth is enabled, the proxy reads the API-key hash from Secrets Manager.
    secret_arn = getattr(args, "secret_arn", None)
    if secret_arn:
        secret_policy = (
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
            '"Action":"secretsmanager:GetSecretValue",'
            f'"Resource":"{secret_arn}"}}]}}'
        )
        _aws(f"iam put-role-policy --role-name {role_name} --policy-name proxy-read-secret "
             f"--policy-document '{secret_policy}'", profile, region, check=False)
    print("  Waiting 10s for role propagation", flush=True)
    time.sleep(10)

    print("[3/4] Creating/updating Lambda function", flush=True)
    # Env: runtime ARN + region always; secret coords only when edge auth is enabled;
    # ALLOWED_ORIGIN scopes CORS to the real frontend origin (omit -> Lambda defaults to
    # "*", which is fine for a no-frontend/server-to-server proxy but NOT for a browser
    # deploy — pass --allowed-origin here, or call `update-origin` once the Amplify URL
    # is known, per the no-wildcard-CORS rule).
    allowed_origin = getattr(args, "allowed_origin", None)
    env_kv = f"AGENT_ARN={runtime_arn},AGENT_REGION={region}"
    if secret_arn:
        env_kv += f",SECRETS_MANAGER_SECRET_PATH={secret_arn},SECRETS_MANAGER_REGION={region}"
    if allowed_origin:
        env_kv += f",ALLOWED_ORIGIN={allowed_origin}"
    env_vars = f"Variables={{{env_kv}}}"
    created = _aws(
        f"lambda create-function --function-name {function_name} --runtime python3.12 "
        f"--handler lambda_function.lambda_handler --zip-file {shlex.quote(f'fileb://{zip_path}')} "
        f"--role {role_arn} --timeout 120 --memory-size 256 --environment \"{env_vars}\"",
        profile, region, check=False,
    )
    if created is None:
        _aws(f"lambda update-function-code --function-name {function_name} "
             f"--zip-file {shlex.quote(f'fileb://{zip_path}')}", profile, region, check=False)
        _aws(f"lambda wait function-updated --function-name {function_name}",
             profile, region, check=False, timeout=180)
        _aws(f"lambda update-function-configuration --function-name {function_name} "
             f"--environment \"{env_vars}\"", profile, region, check=False)

    # Front door: API Gateway ONLY (no Function URL, no alternatives). Idempotent.
    proxy_url, api_gateway_error = _deploy_api_gateway(project_name, profile, region)

    print("[4/4] Done", flush=True)
    _emit({
        "command": "deploy",
        "function_name": function_name,
        "role_arn": role_arn,
        "front_door": "api-gateway",
        "api_endpoint": proxy_url,
        "runtime_arn": runtime_arn,
        "region": region,
        "edge_auth": bool(secret_arn),
        "secret_arn": secret_arn or None,
        "allowed_origin": allowed_origin or "*",
        "frontend_env": {"VITE_API_ENDPOINT": proxy_url},
        "status": "deployed" if proxy_url else "failed",
        "error": None if proxy_url else api_gateway_error,
    })
    if not proxy_url:
        print(f"  ERROR: API Gateway front door was not created: {api_gateway_error}", flush=True)
        sys.exit(1)


def update_origin(args: argparse.Namespace) -> None:
    """Reconcile the proxy Lambda's CORS origin after the fact (e.g. once the real Amplify
    URL is known — the backend/proxy usually deploys before the frontend does, so the
    first `deploy` call can only pass a placeholder or nothing). Reads the function's
    current env vars and merges in the new ALLOWED_ORIGIN so AGENT_ARN/secret coords set
    at deploy time are preserved."""
    profile, region = args.profile, args.region
    function_name = _function_name(args.project_name)
    current = _aws(
        f'lambda get-function-configuration --function-name {function_name} '
        f'--query "Environment.Variables"',
        profile, region, parse_json=True, check=False,
    )
    env = current if isinstance(current, dict) else {}
    env["ALLOWED_ORIGIN"] = args.origin
    env_kv = ",".join(f"{k}={v}" for k, v in env.items())
    r = _aws(
        f"lambda update-function-configuration --function-name {function_name} "
        f'--environment "Variables={{{env_kv}}}"',
        profile, region, check=False,
    )
    ok = r is not None
    _emit({"command": "update-origin", "origin": args.origin, "status": "ok" if ok else "failed"})
    if not ok:
        print("  ERROR: could not update the proxy Lambda's env — has it been deployed yet?", flush=True)
        sys.exit(1)


def teardown(args: argparse.Namespace) -> None:
    profile, region = args.profile, args.region
    function_name = _function_name(args.project_name)
    role_name = _role_name(args.project_name)
    api_name = _api_name(args.project_name)
    print("[1/4] Deleting Function URL (if any)", flush=True)
    _aws(f"lambda delete-function-url-config --function-name {function_name}",
         profile, region, check=False)
    print("[2/4] Deleting API Gateway (if any)", flush=True)
    api_id = _aws(f'apigatewayv2 get-apis --query '
                  f'"Items[?Name==\'{api_name}\'].ApiId | [0]" --output text',
                  profile, region, check=False)
    if api_id and str(api_id).strip() not in ("", "None"):
        _aws(f"apigatewayv2 delete-api --api-id {str(api_id).strip()}", profile, region, check=False)
    print("[3/4] Deleting function", flush=True)
    _aws(f"lambda delete-function --function-name {function_name}", profile, region, check=False)
    print("[4/4] Deleting IAM role", flush=True)
    _aws(f"iam delete-role-policy --role-name {role_name} --policy-name agentcore-invoke",
         profile, region, check=False)
    _aws(f"iam delete-role-policy --role-name {role_name} --policy-name proxy-read-secret",
         profile, region, check=False)
    _aws(f"iam detach-role-policy --role-name {role_name} "
         f"--policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
         profile, region, check=False)
    _aws(f"iam delete-role --role-name {role_name}", profile, region, check=False)
    _emit({"command": "teardown", "status": "removed"})


def main() -> None:
    p = argparse.ArgumentParser(description="AgentCore SigV4 signing-proxy Lambda")
    sub = p.add_subparsers(dest="command", required=True)

    dp = sub.add_parser("deploy", help="Package + deploy the signing proxy Lambda")
    dp.add_argument("--project-name", required=True,
                    help="Scopes the Lambda/role/API names so two projects in the same "
                         "account/region never collide on shared resources.")
    dp.add_argument("--runtime-arn", required=True)
    dp.add_argument("--region", required=True)
    dp.add_argument("--profile", required=True)
    dp.add_argument("--work-dir", default=None, help="Where to build the Lambda package")
    dp.add_argument("--secret-arn", default=None,
                    help="Secrets Manager ARN holding {\"hashed_api_key\":...} — enables edge "
                         "API-key auth + grants the proxy role GetSecretValue. Omit for no gate.")
    dp.add_argument("--allowed-origin", default=None,
                    help="Frontend origin for CORS (e.g. https://main.d123.amplifyapp.com). "
                         "Omit only for a no-browser-frontend deploy — the Lambda then "
                         "defaults ALLOWED_ORIGIN to '*'.")

    uo = sub.add_parser("update-origin", help="Reconcile CORS origin post-deploy (e.g. after Amplify)")
    uo.add_argument("--project-name", required=True,
                    help="Must match the --project-name passed to `deploy` for this project.")
    uo.add_argument("--origin", required=True, help="The exact frontend origin to allow")
    uo.add_argument("--region", required=True)
    uo.add_argument("--profile", required=True)

    tp = sub.add_parser("teardown", help="Remove the proxy Lambda + role")
    tp.add_argument("--project-name", required=True,
                    help="Must match the --project-name passed to `deploy` for this project.")
    tp.add_argument("--region", required=True)
    tp.add_argument("--profile", required=True)

    args = p.parse_args()
    {"deploy": deploy, "update-origin": update_origin, "teardown": teardown}[args.command](args)


if __name__ == "__main__":
    main()
