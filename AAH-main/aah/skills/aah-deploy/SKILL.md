---
name: aah-deploy
description: Execute the AAH deploy phase — route to the right deployment path (AgentCore+Amplify, ECS, Cloud Run, Cloudless, or standard IaC) and generate configs
user-invocable: true
disable-model-invocation: false
---

# AAH Deploy Phase

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text. Call `AskUserQuestion` with properly structured `questions`.

## MANDATORY: Surface Progress — Never Run Silently in the Background

Deploy steps take minutes and dispatch to subagents whose raw tool output is collapsed in this
harness — if you (the orchestrator) only speak after a subagent's final JSON comes back, the
user sees nothing for minutes and then either a terse "done" or a failure with no visible
history of what was tried. Every dispatch in this skill (deploy-engineer, deploy-tester,
serverless pipeline steps, `aah-agentcore-deploy-engineer`, `aah-amplify-deploy-engineer`,
cloudless/ecs/cloudrun engineers) is subject to these rules:

1. **After every subagent call returns, post a short status block to the user before moving to
   the next step** — even on clean success. Never silently proceed to the next dispatch. At
   minimum: what step just ran, its outcome, and any `warnings` it returned (do not drop these —
   several are pre-deploy checks that predict a specific runtime failure).
2. **A subagent that's designed to narrate its own progress (see its agent doc) is doing so on
   your behalf — relay what it reports, don't wait and summarize only at the very end.** If a
   subagent goes quiet for a long step (e.g. the initial AgentCore/Amplify deploy), that's a
   signal something upstream isn't narrating, not something to paper over with your own guess.
3. **A failure is not a silent retry.** Whether it's the Debug Loop (1.8i) or an engineer's own
   fix-cycle, the user sees the failure, the diagnosis, and the fix attempt as it happens — not
   only a final "failed after 3 tries" or "succeeded" with the intermediate struggle hidden.
4. **Non-fatal warnings still get surfaced, not just hard failures.** `invoke_smoke_test.ok:
   false`, a still-wildcard CORS origin, a Bedrock model needing enablement — these don't stop
   the pipeline, but the user must see them before you present the phase as complete.

## Steps

### 0. Resolve Active Project

First, resolve the active project path. ALL file operations in this skill use this path:
```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```
Use `$AAH_DIR` instead of `.aah/` and `$PROJECT_DIR` instead of `.` for all paths.


### 0.5. Issue Brief — Look Before You Work — MANDATORY, ALWAYS RUNS

**Always run this step — do NOT skip even if prior steps errored.**

Invoke the `aah-issue-reporter` agent with: "Brief me on **deploy** issues."
Read the returned brief; decide and act with your phase context (absorb feedback →
re-entry, note feature status (do not close feature issues), or surface untriaged issues). Comment status back.


### 1. Check Prerequisites & Detect Deployment Route
Read architecture decisions and technology stack from `.aah/discuss/decision-registry.yaml`
(the aah-discuss slug registry — single source of truth for the deploy route) and `manifest.yaml`.

**Detect deployment route and load config:**

```bash
ROUTE_CONFIG=$(uvx --from aah python -c "
import json
from pathlib import Path
from aah.core.deploy.deploy_routing import get_route_config
config = get_route_config(Path('$PROJECT_DIR'))
print(json.dumps(config))
")

DEPLOY_ROUTE=$(echo "$ROUTE_CONFIG" | uvx --from aah python -c "import sys,json;print(json.load(sys.stdin)['route'])")
BACKEND_AGENT=$(echo "$ROUTE_CONFIG" | uvx --from aah python -c "import sys,json;print(json.load(sys.stdin)['agent_backend'])")
FRONTEND_AGENT=$(echo "$ROUTE_CONFIG" | uvx --from aah python -c "import sys,json;print(json.load(sys.stdin).get('agent_frontend') or '')")
FRONTEND_PLATFORM=$(echo "$ROUTE_CONFIG" | uvx --from aah python -c "import sys,json;print(json.load(sys.stdin).get('frontend_platform') or '')")
CLOUD_TARGET=$(echo "$ROUTE_CONFIG" | uvx --from aah python -c "import sys,json;print(json.load(sys.stdin).get('cloud_target') or '')")
USER_CALLOUT=$(echo "$ROUTE_CONFIG" | uvx --from aah python -c "import sys,json;print(json.load(sys.stdin).get('user_callout',''))")
```

**Display the route callout to user** (from `routing-config.yaml`):
Display `$USER_CALLOUT` to the user — this is the AAH recommendation message explaining why this route was selected.

**Routing (data-driven from routing-config.yaml):**

- If `DEPLOY_ROUTE` is `cloud-run` or `ecs-express` → Proceed to **Step 1.8** (Serverless Pipeline)
- If `DEPLOY_ROUTE` is `cloudless` → Proceed to **Step 1.6** (Cloudless Path)
- If `DEPLOY_ROUTE` is `agentcore` → Proceed to **Step 1.7** (AgentCore Runtime + Amplify Path)
- If `DEPLOY_ROUTE` is `standard` → Continue to Step 1.5 and Step 2 (Standard IaC Path)

### 1.5. Security Scan Gate

Run security scans and validate results before proceeding to deployment:

```bash
aah run core.security.run_security_scan run \
  --scan-types all \
  --project-path "$PROJECT_DIR"
```

If the gate fails (Critical or High findings found), stop and present findings to the user. Use `AskUserQuestion` with choices: ["Fix findings now", "Suppress false positives", "Skip security gate (not recommended)"]. Do NOT proceed to deploy-engineer unless the gate passes or the user explicitly chooses to skip.

### 1.6. Cloudless Deployment Path (if route=cloudless)

This step runs INSTEAD of Step 2 when the user chose cloudless deployment in Step 1.

**1.6a. Ensure Cloudless SDK with Cloud Adapter:**

The build phase installed cloudless core + langgraph adapter. Deploy needs the cloud-specific adapter (`[aws]` or `[gcp]`) for actual deployment:

```bash
# CLOUD_TARGET was resolved in Step 1 from the route config (aws|gcp) — do NOT
# parse it out of DEPLOY_ROUTE (the route name has no ':' suffix).
HARNESS_DIR=$(aah run core.common.config framework-path)
pip install -e "$HARNESS_DIR/packages/cloudless[$CLOUD_TARGET]" --quiet
```

**Validate Cloud Credentials:**

```bash
uvx --from aah python -m aah.core.deploy.cloud_credential_helper $CLOUD_TARGET
```

If credentials unavailable:
- AWS: Use `AskUserQuestion` — "AWS credentials not configured. Have you run `aws configure`?" with choices: ["Yes, I just configured them (retry)", "Show me how to configure AWS credentials", "Cancel deployment"]
- GCP: Use `AskUserQuestion` — "GCP credentials not configured. Have you run `gcloud auth login`?" with choices: ["Yes, I just authenticated (retry)", "Show me how to authenticate with GCP", "Cancel deployment"]

If user needs instructions:
- AWS: Display `aws configure` steps, then STOP
- GCP: Display `gcloud auth login` steps, then STOP

**1.6b. Dispatch cloudless-deploy-engineer Subagent:**

The cloudless-deploy-engineer handles:
- Generating `cloudless.yaml` configuration
- Creating cloud resources (ECR with KMS for AWS, GCS bucket for GCP)
- Running `cloudless deploy` command
- Capturing outputs to `.aah/deploy/cloudless-outputs.yaml`

**1.6c. Frontend Deployment (if frontend exists):**

After backend deploys successfully, check if frontend exists:

```bash
aah run core.deploy.service_discovery discover --project-path "$PROJECT_DIR" \
  | uvx --from aah python -c "import sys,json;svcs=json.load(sys.stdin);print(json.dumps([s for s in svcs.get('services',[]) if s.get('is_frontend')]))"
```

If frontend services found, dispatch **frontend-deploy-engineer** subagent with:
- `FRONTEND_PLATFORM`: from `$FRONTEND_PLATFORM` (derived in Step 1 from routing config)
  - aws cloud target → `ecs-express-mode`
  - gcp cloud target → `cloud-run`
- `BACKEND_URL`: from cloudless-outputs.yaml (the deployed agent endpoint)
- `CLOUD_TARGET`: aws or gcp

**Note:** Cloudless does NOT deploy frontends. The frontend always goes to Cloud Run (GCP) or ECS Fargate (AWS) regardless of backend route. The `$FRONTEND_PLATFORM` variable from Step 1 already has the correct derived value.

**1.6d. Skip to Step 3** (Validate Deployment) after cloudless path completes.

---

### 1.7. AgentCore route (if route=agentcore) — ORCHESTRATION ONLY

This step deploys to AWS Bedrock AgentCore Runtime (+ optional AWS Amplify frontend) when the
aah-discuss slug `backend-compute-platform == agentcore-runtime` (AWS AI-agent app). **`aah-deploy`
orchestrates and RESOLVES ALL INPUTS**, then hands each engineer a **fully-specified prompt** —
exactly like the serverless pipeline feeds `ecs-deploy-engineer`. The engineers are **thin**:
they run ONE script command and parse JSON. **All decisions are made HERE, not in the engineer**
(so the engineer never re-asks, re-derives, or hand-orchestrates). Mechanics live in the scripts.

**1.7a. Universal gates.**
1. Security scan — `aah run core.security.run_security_scan run --scan-types all --project-path "$PROJECT_DIR"`; handle like Step 1.8f.
2. AWS profile/auth — `aah run core.deploy.cloud_auth check --cloud aws`; if multiple profiles, `AskUserQuestion` to pick one (as in Step 1.8d); capture `AWS_PROFILE`, `AWS_ACCOUNT_ID`, `REGION` (default `us-east-1`). Then `aah run core.deploy.cloud_auth validate-permissions --cloud aws --route agentcore` — GATE until it passes.

**1.7b. Resolve ALL deploy inputs (the orchestrator's job — do NOT defer to the engineer).**
Read the recorded decisions from the aah-discuss registry (`.aah/discuss/decision-registry.yaml`).
For any gap, ask the user directly here (a single batched `AskUserQuestion`) — NOT in the engineer.
Assemble:
- **PROJECT_NAME** — PascalCase of the project dir.
- **FRONTEND** — `yes|no` from service discovery:
  ```bash
  aah run core.deploy.service_discovery discover --project-path "$PROJECT_DIR" \
    | uvx --from aah python -c "import sys,json;svcs=json.load(sys.stdin);print('yes' if [s for s in svcs.get('services',[]) if s.get('is_frontend')] else 'no')"
  ```
- **MEMORY_STRATEGIES** — from aah-discuss slugs `agent-memory` + `memory-strategies`:
  not-in-scope/`in-context-only` → empty (no memory); else the CSV (default `SEMANTIC` if in scope but unspecified).
- **SECRET_ARN** — the app's API-key secret ARN (from the registry / secrets setup), or empty.
- **ENV_VARS** — assemble the `"K=V,K=V"` string: Bedrock model IDs (**inference-profile form**,
  `us.anthropic.…-v1:0`), `SECRETS_MANAGER_SECRET_PATH`+`REGION`, `AMPLIFY_ORIGIN` (placeholder
  until the frontend URL is known — `up` forwards it to the proxy Lambda's `ALLOWED_ORIGIN` if
  present; omit it on the first pass and let Step 1.7e's `update-origin` set the real value once
  the frontend deploys, rather than baking in a placeholder origin).

**1.7c. Dispatch `aah-agentcore-deploy-engineer` (backend) with a FULLY-SPECIFIED prompt.**
Pass every resolved value — the engineer makes no decisions:
```
PROJECT_DIR=<...>  PROJECT_NAME=<Pascal>  AWS_PROFILE=<...>  AWS_ACCOUNT_ID=<...>
REGION=<...>  ENV_VARS="<K=V,...>"  MEMORY_STRATEGIES="<CSV or empty>"
SECRET_ARN="<arn or empty>"  FRONTEND=<yes|no>
```
The engineer narrates its own progress while `up` runs in the background (per its agent doc) —
relay what it reports rather than going quiet until it returns. It returns JSON: `runtime_arn`,
`endpoint_url`, `api_endpoint` (API Gateway proxy URL, if FRONTEND=yes), `memory_name`, `ready`,
`invoke_smoke_test`, `warnings`.
→ **If it returns a `validate`-step failure "no protocol contract" → STOP** and run the build
phase (`agentcore-implement`) first.
→ **Present a status block to the user now, before dispatching the frontend** — status,
runtime/API endpoint, every `warnings` entry, and `invoke_smoke_test.ok` explicitly (state it
even when `true`). Do not silently roll straight into 1.7d.

**1.7d. Dispatch `aah-amplify-deploy-engineer` (frontend) — only if `FRONTEND=yes`.** Pass
`FRONTEND_DIR`, `PROJECT_DIR`, `BACKEND_URL` (= `api_endpoint` from the backend), `AWS_PROFILE`,
`REGION`, `APP_NAME` (`<project>-frontend`), `BRANCH` (`main` unless recorded), `ENV_VAR_NAME`
(`VITE_API_ENDPOINT`). Same rule as 1.7c: relay its progress narration, don't wait silently.
Capture `app_id`, `app_url`, `cors_origin_to_allow`, and present the app URL to the user before
1.7e — it's a real deployed URL the user may want to open immediately, not something to hold
back until the whole phase wraps up.

**1.7e. Cross-engineer verify (orchestrator's job).** If a frontend was deployed, reconcile CORS
so the backend/proxy allows the Amplify origin — the backend deploys before the frontend exists,
so `up` can only pass a placeholder/no origin at first. Run this AFTER the amplify engineer
returns `cors_origin_to_allow`:
```bash
aah run core.deploy.agentcore_proxy update-origin --project-name "$PROJECT_NAME" \
  --origin "$CORS_ORIGIN_TO_ALLOW" --region "$REGION" --profile "$AWS_PROFILE"
```
Then confirm a browser round-trip. Check the backend engineer's `invoke_smoke_test.ok` (see its own
Step 2/3 for what this means and how to diagnose a `false`) — if false, treat it like a failed layer
even though every ledger step passed. On failure use the debug loop (Step 1.8i) — fix + re-dispatch
only the failed layer, max 3 retries.

⚑ **AG-UI + browser + signing-proxy = no live token streaming.** The proxy (API Gateway +
buffered Lambda) forwards the complete AG-UI SSE payload in one shot once the run finishes — it
cannot relay it incrementally (true streaming needs a Lambda Function URL with
`RESPONSE_STREAM`, blocked by SCP on this account class). If the PRD's reason for choosing AG-UI
was live token/tool-call display, set that expectation with the user now rather than after
deploy — the frontend will still work, just without the live typing effect.

**1.7f. Write outputs.** Merge the engineers' JSON into `.aah/deploy/agentcore-outputs.yaml`
(`route`, `backend{runtime_arn,endpoint_url,teardown}`, `frontend{app_url,app_id,branch}` if
deployed, `auth{api_endpoint,cors_origin}`, `memory{memory_name,strategies}` if provisioned,
`region`, `cloud: aws`). Then **skip to Step 4** — the phase tag `${PROJECT_NAME}/aah-deploy`
is applied there.

---

### 1.8. Serverless Pipeline (if route=cloud-run or ecs-express)

This step runs INSTEAD of Step 2 when the user selected an AAH-managed platform (Cloud Run or ECS Fargate). The pipeline deploys ALL discovered services in dependency order.

The pipeline uses `aah run core.deploy.serverless_pipeline` for state management. Each sub-step advances the pipeline state.

**1.8a. Brownfield Check:**

```bash
aah run core.deploy.brownfield_redeploy detect --project-path "$PROJECT_DIR"
```

If prior deployment exists, use `AskUserQuestion`:
- "Prior deployment detected. How to proceed?"
- Choices: ["Redeploy changed services only", "Full fresh deploy", "View current status"]
- If "Redeploy changed": run `aah run core.deploy.brownfield_redeploy changed --project-path "$PROJECT_DIR"` to get changed services list. Only those redeploy; unchanged services keep existing URLs.

**1.8b. Service Discovery:**

```bash
aah run core.deploy.service_discovery discover --project-path "$PROJECT_DIR"
```

Display discovered services table to user:
| Service | Language | Framework | Port | Frontend? |
If no services found, STOP with error.

**1.8c. Dependency Ordering:**

```bash
aah run core.deploy.service_graph order --project-path "$PROJECT_DIR"
```

Display deploy layers: "Layer 0: backend, gateway (parallel) → Layer 1: frontend"
Validate graph: if cycle detected, STOP with error.

**1.8d. Cloud Authentication [GATE — blocks until auth + permissions pass]:**

**For AWS — Profile Selection:**

```bash
aah run core.deploy.cloud_auth check --cloud aws
```

If the response contains `available_profiles` (multiple AWS profiles detected):
- Display to user:
  ```
  Multiple AWS profiles detected:
    1. default (account: 123456789012)
    2. sandbox (account: 864981750171)
    3. prod (account: 555555555555)
  ```
- Use `AskUserQuestion`: "Which AWS profile should AAH use for deployment?"
  - choices: one per profile (use profile name as label, account ID in description)
- After user selects, re-run with: `aah run core.deploy.cloud_auth check --cloud aws --profile <selected>`
- Store the selected profile in pipeline state — ALL subsequent AWS commands must use `--profile <selected>`
- Pass `AWS_PROFILE=<selected>` to deploy agents as part of their input contract

**For GCP — Project Selection:**

```bash
aah run core.deploy.cloud_auth check --cloud gcp
```

If authenticated but no project set (response has `error: "Authenticated but no project set"`):
- Use `AskUserQuestion`: "GCP project not set. Run `! gcloud config set project <ID>` in your terminal."

**Auth failure handling (both clouds):**

- If not authenticated:
  - AWS: Use `AskUserQuestion` "Run `! aws configure` (or `! aws sso login`) in your terminal to authenticate."
    choices: ["Done, I've authenticated (retry)", "Show required IAM permissions", "Cancel"]
  - GCP: Use `AskUserQuestion` "Run `! gcloud auth login` then `! gcloud config set project <ID>` in your terminal."
    choices: ["Done, I've authenticated (retry)", "Show required GCP roles", "Cancel"]
- After user confirms "Done": re-validate via `cloud_auth check`
- If still invalid: loop (show error, ask again)
- Then: `aah run core.deploy.cloud_auth validate-permissions --cloud $CLOUD --route $DEPLOY_ROUTE`
- If insufficient permissions: display missing permissions list, ask user to attach/grant them, retry
- GATE: pipeline blocks until BOTH authentication AND permissions pass

**IAM role discovery (AWS / ecs-express only):** once auth + permissions pass, discover the
roles the ECS deploy needs and store them in pipeline state:

```bash
aah run core.deploy.cloud_auth discover-roles --profile <selected>
```

Capture from the JSON:
- `codebuild_role`, `execution_role` — **required**; if `errors` is non-empty, show the
  `instructions` and STOP until the user creates them.
- `task_role` — **optional** (the container's runtime identity for AWS API calls: Secrets
  Manager, S3, Bedrock). If null, that's fine — pass it through as empty and the deploy
  skips it. If the app needs AWS access, follow the (optional) create-instruction, then
  re-run `discover-roles`.

**1.8e. Local Preview [GATE — skippable]:**

**First: Install dependencies for each discovered service before launching.**

For each service in the discovered services list, run the appropriate install command:

```bash
# For Python services (has pyproject.toml or requirements.txt):
cd "$SERVICE_PATH"
pip install -e . 2>/dev/null || pip install -r requirements.txt 2>/dev/null

# For Node services (has package.json):
cd "$SERVICE_PATH"
npm install
```

Display to user what's being installed:
```
Installing dependencies for local preview:
  Backend (Python):  pip install -e .
  Frontend (Node):   cd frontend && npm install
```

If install fails, show the error and offer: "Install failed for {service}. Fix manually or skip preview?"

**Then: Launch services:**

```bash
aah run core.deploy.local_preview start --project-path "$PROJECT_DIR"
```

Launches services natively (uvicorn, npm dev, go run) in layer order. Healthchecks each.

**MANDATORY: Display running URLs to user.** After services start, show:
```
Local Preview Running:
  Backend:  http://localhost:8080
  Frontend: http://localhost:3000

Open these URLs in your browser to test the application locally.
```

Use `AskUserQuestion`: "Local preview is running at the URLs above. Test it and confirm."
  choices: ["Working correctly, proceed to deploy", "Something is broken, fix first", "Skip preview (not recommended)"]
Cleanup all preview processes after gate passes.

**1.8f. Security Scan:**

```bash
aah run core.security.run_security_scan run --scan-types all --project-path "$PROJECT_DIR"
```

Parse the scan result JSON. Handle based on outcome:

- **If `passed: true`** → proceed to deploy
- **If `passed: false` AND `reason: scan_incomplete` AND errors contain `no_tools_available`:**
  - This means no scanning tools (gitleaks, semgrep, trivy) are installed — NOT that vulnerabilities were found
  - Use `AskUserQuestion`: "No security scanning tools installed. No vulnerabilities were found because no scan could run."
    choices: ["Skip security scan for this deployment", "Install tools and retry", "Cancel deployment"]
  - If user skips → proceed to deploy (log that scan was skipped)
  - If user wants to install → show: `pip install semgrep` / `brew install gitleaks` / `brew install trivy` and retry
- **If `passed: false` AND `reason: blocking_findings`** → Critical/High findings found. Use `AskUserQuestion`:
    choices: ["Fix findings now", "Suppress false positives", "Skip security gate (not recommended)"]
    Do NOT proceed unless gate passes or user explicitly chooses to skip.

**1.8g. Tiered Access Gate + Deploy Per Layer:**

**First: Resolve access tier before deploying any service.**

```bash
aah run core.deploy.access_tier deploy \
  --project-path "$PROJECT_DIR" \
  --cloud "$CLOUD" \
  --route "$DEPLOY_ROUTE" \
  --service-name "$(echo $SERVICES | uvx --from aah python -c "import sys,json;print(json.loads(sys.stdin.read())[0]['name'])")"
```

This reads `tier_prediction` from `cloud-readiness.yaml` (written by `aah-access`) and returns:
- `tiers_to_try`: ordered list of tiers to attempt (skips impossible ones)
- `tier_configs`: deploy flags per tier
- `deployer_ip`: auto-detected for Tier 1B

**Cascade rule:** Deploy the FIRST service in Layer 0. If tier fails (health check timeout, permission denied, org policy error), cleanup and try next tier. Once a tier succeeds on the first service, ALL remaining services use that same tier.

**Frontend rule:** If a frontend service exists and only Tier 1C passes, report failure — browsers can't access local-proxy-only services. Minimum for frontend = Tier 1B.

**For each tier in `tiers_to_try` (cascade):**

For the first service only (determines tier for all):
  1. Get tier config: `aah run core.deploy.access_tier tier-config --cloud $CLOUD --tier $TIER`
  2. Dispatch deploy agent with **TIER** parameter:
     - cloudrun-deploy-engineer: TIER controls `--ingress` and `--allow-unauthenticated`
     - ecs-deploy-engineer: TIER controls ALB scheme and SG CIDR
  3. Tier-specific health check:
     - Tier 1A: bare `curl https://{url}/health`
     - Tier 1B (GCP): `curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" {url}/health`
     - Tier 1B (AWS): bare `curl` (SG allows deployer IP)
     - Tier 1C (GCP): `gcloud run services proxy {name} --port 8080` then `curl localhost:8080/health`
     - Tier 1C (AWS): `aws ecs execute-command` to curl inside container
  4. If health check PASSES → this tier works. Lock it. Proceed with remaining services using same tier.
  5. If health check FAILS → cleanup: `aah run core.deploy.access_tier cleanup --cloud $CLOUD --tier $TIER --service $SERVICE`
     Then try next tier in cascade.
  6. If ALL tiers fail → display failure report from `access_tier.build_failure_report()`. Stop. Present to user with `AskUserQuestion`:
     choices: ["Send failure report to cloud team", "Retry with different config", "Cancel deployment"]

**After tier is locked — deploy remaining services:**

For each layer (from service_graph):
  For each service in layer:
  1. Compute env vars (inter-service URLs **plus** validated runtime-resource coordinates from `cloud-readiness.yaml`):

     ```bash
     aah run core.deploy.env_injector compute \
       --layer-services '["{svc}"]' \
       --all-services '{...}' \
       --deployed-urls '{...}' \
       --project-path "$PROJECT_DIR"
     ```

     The `--project-path` flag folds in env vars derived from the cloud-readiness gate — every service in the layer inherits things like `S3_BUCKET=…`, `RDS_POSTGRES_HOST=…`, `RDS_POSTGRES_DATABASE=…`, `RDS_POSTGRES_SECRET_PATH=…`, `BEDROCK_MODEL_ID=…`, `LANGSMITH_API_URL=…` from the validated services. Secret *values* are never injected — only secret *paths*, so the app fetches them at runtime via its own SDK.

     If you only want to inspect the runtime-resource map (e.g. before generating a Dockerfile), run:

     ```bash
     aah run core.deploy.env_injector runtime-resources \
       --project-path "$PROJECT_DIR" --format json
     ```

  2. Generate Dockerfile if needed (ECS Fargate requires it): `aah run core.deploy.dockerfile_generator generate --service-path ... --language ... --framework ... --port ...`
  3. Dispatch deploy agent with LOCKED TIER:
     - SERVICE: {name, path, port, env_vars}
     - Cloud context (project/account, region)
     - TIER: {locked tier from cascade}
     - DEPLOYER_IP: {if tier 1B}
     - CODEBUILD_ROLE, ECS_EXECUTION_ROLE (from discover-roles) — required
     - ECS_TASK_ROLE (from discover-roles `task_role`) — optional; pass through if set so
       the container can call AWS APIs, omit if null
  4. Capture deployed URL from agent response
  5. Healthcheck (tier-specific method)
  6. If healthcheck FAILS → go to Step 1.8i (Debug Loop)

**Frontend Dispatch (universal — applies to ALL routes with frontend):**

After backend layers deploy, if `$FRONTEND_AGENT` is set (non-empty) and frontend services were discovered:

```bash
FRONTEND_SERVICES=$(aah run core.deploy.service_discovery discover --project-path "$PROJECT_DIR" \
  | uvx --from aah python -c "import sys,json;svcs=json.load(sys.stdin);fronts=[s for s in svcs.get('services',[]) if s.get('is_frontend')];print(json.dumps(fronts))")
```

If frontend services exist, dispatch `$FRONTEND_AGENT` (frontend-deploy-engineer) with:
- `FRONTEND_PLATFORM`: `$FRONTEND_PLATFORM` (from routing config — same platform for cloud-run/ecs-express routes, derived for cloudless)
- `BACKEND_URL`: the deployed backend URL from prior layers
- `TIER`: same locked tier as backend
- `CLOUD_TARGET`: aws or gcp

The frontend-deploy-engineer reads `$FRONTEND_PLATFORM` to decide HOW to deploy (Cloud Run buildpacks vs ECS Fargate Dockerfile). It does NOT read the frontend-strategy decision for the platform — that decision defines frontend strategy (SPA/SSR), not the compute target.

**After all layers deployed:** Display tier-specific access instructions to user.

**1.8h. Final Connectivity Check [GATE]:**

```bash
aah run core.deploy.healthcheck verify-connectivity --services '{"backend":"https://...","frontend":"https://..."}'
```

Hits /ready on each service to verify inter-service connections work.
If /ready not implemented (404), falls back to /health and warns.

**1.8i. Debug Loop (on any failure in 1.8g or 1.8h):**

1. Fetch logs: `aah run core.deploy.debug_loop fetch-logs --target $DEPLOY_ROUTE --service $FAILED_SERVICE --region $REGION`
2. Diagnose: `aah run core.deploy.debug_loop diagnose --logs "$LOGS"`
3. Present diagnosis to user. Agent applies fix (update Dockerfile CMD, add env var, fix import)
4. Redeploy ONLY the failed service
5. Re-healthcheck
6. Max 3 retries → then present full logs + diagnosis to user with `AskUserQuestion`: ["Fix manually and retry", "Skip this service", "Abort deploy"]

**1.8j. Write Outputs + Tag:**

Write `.aah/deploy/serverless-outputs.yaml`:
```yaml
route: {cloud-run|ecs-express}
tier_used: {1A|1B|1C}
tier_label: {Full Public|Deployer-Scoped|Local Proxy}
services:
  backend:
    url: https://backend-xyz.run.app
    port: 8080
  frontend:
    url: https://frontend-xyz.run.app
    port: 3000
deployed_at: {iso_timestamp}
region: {region}
cloud: {gcp|aws}
access_instructions: |
  {tier-specific access instructions from access_tier module}
```

Git tag: `${PROJECT_NAME}/aah-deploy`

Present to user:
1. Deployed URLs table
2. Tier used + label (e.g., "Tier 1B — Deployer-Scoped")
3. Access instructions (how to reach the deployed service based on tier)

Skip to Step 4 (Update State).

---

### 2. Dispatch deploy-engineer Subagent (Standard Path)
The deploy-engineer generates:
- Dockerfile / docker-compose.yaml (if container-based)
- IaC templates (Terraform/Pulumi) in `.aah/deploy/infra/`
- CI/CD pipeline definitions (GitHub Actions, GitLab CI, or Azure DevOps)
- Environment tier strategy (`environments.yaml`)

### 3. Validate Deployment
Dispatch deploy-tester subagent to:
- Build containers (if applicable)
- Deploy to local/staging
- Run cumulative test suite against deployed instance

### 3a. Checkpoint: Commit Deploy Artifacts

```bash
cd "$PROJECT_DIR"
git add .aah/deploy/ Dockerfile* docker-compose* .github/ .gitlab-ci* azure-pipelines*
git commit -m "feat(aah): generate deployment configs, IaC, and CI/CD"
```

### 4. Update State
```bash
aah run core.common.manifest update-phase deploy
```

### 4a. Checkpoint: Commit Deploy Phase Complete

```bash
cd "$PROJECT_DIR"
git add .aah/
git commit -m "feat(aah): complete deploy phase"
```

**Tag the phase:**

```bash
PROJECT_NAME=$(aah run core.common.manifest read | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
git -C "$PROJECT_DIR" tag -a "${PROJECT_NAME}/aah-deploy" -m "AAH deploy phase complete"
```

**Push the commit and tag:**

```bash
git push origin HEAD
git push --tags origin
```

### 4b. Comment Back to Originating Issue

**Comment back to GitHub issue (if version_control enabled).** If this run was triggered by a GitHub
issue surfaced at Step 0.5, run:
```bash
aah run core.version_control.cli status
```
If enabled, and the deploy above completed successfully, post a resolution comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "Deploy complete based on this issue: <brief summary — backend endpoint, frontend URL if deployed>. Changes committed."
gh issue close <N>
```
Where `<N>` is the issue number from the Step 0.5 brief that triggered this re-entry. If the feedback
came from `$ARGUMENTS` (not a GitHub issue), skip this step. If the deploy did not complete or commit
failed, do NOT close the issue. If version_control is disabled or offline, skip silently.

### 5. Present to User
Show generated artifacts and test results.
Use `AskUserQuestion` to get approval:
- "Deploy phase complete — project delivery finished. Accept?" with choices: ["Yes, delivery is complete", "No, I want to revise deployment configs"]
