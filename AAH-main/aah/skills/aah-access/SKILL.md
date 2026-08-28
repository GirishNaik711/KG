---
name: aah-access
description: Cloud connectivity + data readiness. Phase 1 (Steps 1–8) probes each cloud service the /aah-discuss registry mentions and emits .aah/architecture/cloud-readiness.yaml. Phase 2 (Steps 9–18) then validates the data-bearing services' schema + row counts and emits .aah/architecture/data-readiness.yaml and .aah/architecture/data-schema-snapshot.yaml. Wraps aah/core/gates/validate_cloud_readiness.py and aah/core/gates/validate_data_readiness.py. Absorbed /aah-data (deprecated).
user-invocable: true
disable-model-invocation: false
---

# /aah-access — Cloud + Data Readiness

All user questions MUST use `AskUserQuestion`. Bash output is collapsed — always re-render results as direct markdown before asking a follow-up.

## Global rules

- **Never ask for secrets.** Only ask for endpoints, hostnames, bucket names, table names, project IDs, and Secrets Manager **paths** (never the values). The gate fetches credentials inside its subprocess; secret plaintext never touches the LLM context.
- **Never invent resource identifiers.** Bucket names, DB endpoints, secret ARNs, model IDs, etc. are real-world resources — the user provides them or the value is `any`/`skip`/blank. Do not synthesize plausible-sounding names from account IDs or project names. Examples in this file are format hints only.
- **Never invent input fields.** Every service's `required_user_input[]` and `optional_user_input[]` come from the catalog handler (via the seeded row in `$AAH_DIR/architecture/cloud-readiness.yaml`). If the user wants an unlisted field, that's a catalog change, not a skill workaround.
- **Output location.** All three readiness YAMLs this skill emits (`cloud-readiness.yaml`, `data-readiness.yaml`, `data-schema-snapshot.yaml`) are written under `$AAH_DIR/architecture/`, not the `$AAH_DIR` root — the architecture phase reads them directly from there to design against real, already-created resources instead of inventing infrastructure.

## Prerequisites

- `/aah-discuss` complete — slug registry at `$AAH_DIR/discuss/decision-registry.yaml`. This is the only upstream input this skill reads.

---

# Phase 1 — Cloud Readiness

Wraps `aah/core/gates/validate_cloud_readiness.py`. Emits `$AAH_DIR/architecture/cloud-readiness.yaml`.

### 1. Check existing state

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
aah run core.gates.validate_cloud_readiness --project-path "$PROJECT_DIR" --check-state
```

Three outcomes:

- **`gate_status: passed`** — `AskUserQuestion`: "Accept previous results (→ hand off to Phase 2)" / "Re-validate all" / "Re-validate failed/pending only". Route accordingly.
- **`gate_status: pending`** — resume from the first service whose status is not `pass`. Do NOT re-seed.
- **`not-started`** — proceed to Step 2.

### 2. Seed the service list (five sub-steps)

**2.a — Read the registry and show cloud services the project declared.**

BEFORE any gate call, BEFORE handlers enter the picture, read `$AAH_DIR/discuss/decision-registry.yaml` directly. Walk `pre_resolved[]` and `decisions[]` and identify which `(slug_id, response)` pairs name a **cloud service the copilot itself hosts on its primary cloud provider** (AWS / GCP / Azure — the value of the `cloud-provider` slug). Use judgment on the slug and its response — no keyword list, no token match.

Render as a markdown table with columns `slug_id`, `response`, `source` (`pre_resolved` or `decisions`). One row per identified cloud service. Do NOT annotate the table with commentary about individual rows, edge cases, exclusions, or "uncertain" flags — the table is the answer, the user's response is where nuance enters. If a call is genuinely ambiguous, leave the row IN the table and let the user drop it via the "Remove a slug" option below.

`AskUserQuestion`: "Is this the cloud-services list you expected, or is anything missing / extra?"

- **Looks right** → move to 2.b.
- **Add a slug I missed** → user names slugs I omitted; add them and re-render.
- **Remove a slug that isn't actually a service** → user names slugs I over-included; drop them and re-render.

The point of this step is a human sanity-check on the "what services does this project have" question BEFORE handlers or the gate are involved. My identification uses judgment on the registry alone; the user's correction is authoritative.

**2.b — AgentCore-hosted MCP servers AND A2A servers.**

AgentCore-hosted MCP and A2A agents aren't declared as slug decisions in the registry today, so they don't appear in the 2.a list. Ask before the seed so any of these entries flow through 2.c → 2.d → 2.e alongside the other confirmed services. `/aah-access` only validates AgentCore-hosted MCP/A2A — external / public MCPs (arxiv, DuckDuckGo, self-hosted MCPs) are out of scope for this gate.

Two flavors, both handled here:

**AgentCore-deployed MCP** — MCP tool server hosted on AWS Bedrock AgentCore Runtime. `AskUserQuestion`: "Do you have MCP servers deployed on AWS Bedrock AgentCore Runtime?" If yes, collect one or more **runtime ARNs** (never the tools' credentials). Format: `arn:aws:bedrock-agentcore:<region>:<account>:runtime/<runtime-id>`. Each ARN maps to handler `aws-agentcore-mcp`.

**AgentCore-deployed A2A** — A2A (Agent-to-Agent) agent hosted on AWS Bedrock AgentCore Runtime. `AskUserQuestion`: "Do you have A2A agents deployed on AWS Bedrock AgentCore Runtime?" If yes, collect one or more **runtime ARNs**. Format matches AgentCore MCP. Each ARN maps to handler `aws-agentcore-a2a`.

Accept multiple via repeated `AskUserQuestion` until the user says "done" or gives an empty response. Hold both AgentCore MCP ARNs and AgentCore A2A entries as one in-memory list; do NOT write to `$AAH_DIR/architecture/cloud-readiness.yaml` here — 2.e is the single point where the artifact is written.

**2.c — Emit routing inputs.**

```bash
aah run core.gates.validate_cloud_readiness --project-path "$PROJECT_DIR" --seed
```

Returns `slug_decisions[]` (every resolved slug from the decision registry) + `handlers[]` (every catalog handler with `description` and `applies_when`).

If `slug_decisions[]` is empty AND 2.b produced no MCP ARNs → auto-pass Phase 1, skip to Step 8.

**2.d — Classify the confirmed services against handler descriptions.**

Iterate two sources together (not the full `slug_decisions[]`):

1. **Registry-declared services from 2.a.** For each, compare the `(slug_id, response)` against every handler's `description` and `applies_when` prose.
2. **MCP / A2A entries from 2.b.** AgentCore MCP ARNs route to `aws-agentcore-mcp`, AgentCore A2A ARNs route to `aws-agentcore-a2a` — the classifier's job for these is trivial (each handler's `applies_when` names its own shape) but still visible in the confirmation view.

Two possible outcomes per row:

- **matched** — a handler description fits → `{slug_id_or_arn, response, kind: "matched", handler_id: "..."}`.
- **unmatched** — no handler description fits → `{slug_id_or_arn, response, kind: "unmatched", reason: "..."}`.

Render as two grouped views for user confirmation before Step 2.e commits:

- **Matched** — `source | response | proposed handler`.
- **Unmatched** — `source | response | reason`.

`AskUserQuestion`: "Proceed with this classification, or override any row?"

- **Proceed** → move to 2.e.
- **Reclassify a row** → collect `{source, new_kind, handler_id?}`, update the routings, re-render, ask again.

Every other slug in the registry (design decisions, project-shape, compliance constraints, third-party SaaS) is automatically treated as `not-a-service` in Step 2.e — no classifier work needed for them because 2.a already excluded them.

**2.e — Run `--seed --routings` (and optionally `--extra-services`) to write the artifact.**

Build two payloads:

**Routings** — one entry per slug in `slug_decisions[]`, from two sources:

1. **Confirmed cloud services (from 2.d)** → `kind: matched` or `unmatched` as classified.
2. **Every remaining slug in `slug_decisions[]`** → `kind: not-a-service`. Pure set arithmetic.

The gate asserts `len(routings) == len(slug_decisions)`.

**Extra-services** — one entry per AgentCore MCP or A2A item from 2.b. Passed via a separate flag because these are NOT slug decisions — they don't belong in the routings count. Each entry has shape `{handler_id, config, display_name?}` where `handler_id` is what 2.d matched and `config` is keyed by the handler's catalog `required_user_input[]` / `optional_user_input[]` fields.

```bash
aah run core.gates.validate_cloud_readiness \
  --project-path "$PROJECT_DIR" --seed \
  --routings '<routings-json>' \
  --extra-services '<extra-services-json>'  # omit if no MCP/A2A entries
```

Render the resulting `services[]` and `unmatched_services[]` tables from the returned JSON — this is the confirmed state, not a proposal.

The MCP probe (`test_agentcore_mcp`) makes a SigV4-signed JSON-RPC `tools/list` call to the runtime's invocation URL and returns `verified_runtime_arn`, `verified_tools_count`, `verified_tool_names`. Read-only; never calls `tools/call`.

The A2A probe (`test_agentcore_a2a`) makes a SigV4- or Bearer-authenticated GET to the runtime's `/.well-known/agent-card.json` and returns the full parsed Agent Card as `verified_agent_card` plus a set of extracted receipts: `verified_runtime_arn`, `verified_auth_mode`, `verified_agent_name`, `verified_agent_description`, `verified_agent_version`, `verified_protocol_version`, `verified_preferred_transport`, `verified_capabilities`, `verified_default_input_modes`, `verified_default_output_modes`, `verified_agent_url`, `verified_skills_count`, `verified_skill_ids`, `verified_skills[]`. Read-only; never invokes a task. When rendering the receipt to the user, show the top-level identity fields as a markdown table, the `verified_skills[]` list as a nested table, and the full `verified_agent_card` as a fenced JSON block so the user can verify the runtime actually served an Agent Card.

### 3. Resolve deployment method

Read `development-methodology` from the decision registry (already answered in `/aah-discuss`). Map:

| slug value | `--deployment-method` |
|---|---|
| `full-integrated-cloud` | `direct-cloud` (strict — failures block) |
| `local-cloud-ready` | `local-validation-then-cloud` (lenient) |
| `full-local` | `local-only` (skip Phase 1; jump to Step 8) |

If the slug is missing/unrecognized → halt with "Run `/aah-discuss` first". Do NOT re-ask this here.

If the user asks to skip the whole gate anyway → confirm reason via `AskUserQuestion`, then:

```bash
aah run core.gates.validate_cloud_readiness --project-path "$PROJECT_DIR" --skip --skip-reason "<reason>"
```

Skip to Step 8.

### 4. Select and verify auth

**4.a — Strategy.** `AskUserQuestion` (limit to the provider(s) actually in the seeded services):

- AWS named profile → ask for profile name.
- AWS default credential chain → env vars / instance profile.
- GCP ADC (`gcloud auth application-default login`).
- GCP service-account key file → ask for the path.
- Azure CLI (`az login`).

Store the profile/key path. Never print secret values back.

**4.b — Verify identity.** Run **only** the command matching the chosen strategy:

```bash
# AWS profile:  aws sts get-caller-identity --profile "$AWS_PROFILE" --output json
# AWS default:  aws sts get-caller-identity --output json
# GCP:          gcloud auth application-default print-access-token >/dev/null && \
#               gcloud auth list --filter=status:ACTIVE --format='value(account)'
# Azure:        az account show --query '{sub:id, user:user.name, tenant:tenantId}' -o json
```

Render the returned identity as markdown. `AskUserQuestion`: "Proceed with this identity?"

- No → back to 4.a.
- Auth failure → halt with the specific fix (`aws sso login --profile <name>`, `gcloud auth application-default login`, `az login`, refresh keys). Do NOT proceed with unverified creds.

Store the profile name — it gets passed as `--profile` to every gate invocation. Scoped to the subprocess only.

### 5. Database credentials via Secrets Manager

For every **database-like service** in the seeded inventory (`rds-postgres`, `rds-mysql`, `aurora-postgres`, `documentdb`, `elasticache-redis`, or anything whose `service_type` implies a live DB connection):

`AskUserQuestion`:

- **Existing secret** → ask for the secret **name or ARN** (never the password). Record it on the service's config as `secret_path` with `auth_method: full-config-from-secret` (endpoint/port/db/user come from the secret) or `secret-manager` (password-only; other fields provided in Step 6.b).
- **No DB yet** → override this service; the control-plane probe at Step 6 still runs.
- **Free text** → paste in as-is.

Skip Step 5 entirely if there are no DB services in the inventory.

### 6. Per-service validation loop

Iterate every service in `$AAH_DIR/architecture/cloud-readiness.yaml` — one at a time.

**6.a — Announce** the service and its handler.

**6.b — Collect inputs.** Read `services[i].required_user_input[]` and `services[i].optional_user_input[]` from `$AAH_DIR/architecture/cloud-readiness.yaml`. These arrays were populated from the catalog handler's own declaration at seed time. For each entry, apply this decision order:

1. **Secret shortcut** — if the field appears in the service's `auth_options[*].skip_user_input_fields` under the chosen `auth_method`, drop the question entirely; the gate reads those values from the secret.

2. **Registry auto-fill** — search `$AAH_DIR/discuss/decision-registry.yaml` for any slug whose `slug_id` matches the field name exactly OR contains it as a substring. Present the slug's value as the pre-selected choice in `AskUserQuestion` (user can override via "Other"). Prefer exact match over substring matches; if only substring matches exist, list them as separate choices.

3. **Ask** — no shortcut fired, so `AskUserQuestion` with the entry's `prompt` verbatim; include `example` and `default` from the catalog entry as visible choices.

For `optional_user_input` entries, ask an opt-in: "Provide `{field}` (stronger validation, deeper probe)" vs. "Skip — control-plane probe only". Never silently drop.

**Exception — optional inputs with a working default.** Skip prompts for optional fields whose catalog entry declares a default. Ask only if the probe fails and the fallback needs the field.

**6.c — Run the probe** (only this service; others keep their status):

```bash
aah run core.gates.validate_cloud_readiness \
  --project-path "$PROJECT_DIR" \
  --deployment-method "$METHOD" \
  --services "$SERVICE_TYPE" \
  --config '{"services":[{"service_type":"...","service_id":"svc-XXX", ...collected inputs...}]}' \
  ${AWS_PROFILE:+--profile "$AWS_PROFILE"}
```

**6.d — Render** the response's `verified_*` receipts on pass (bucket region, DB version, table count, etc.). On fail, show `error_class` + `probe_mode` + the raw error string. NEVER just say "passed" without receipts.

**6.e — Outcome**:

- Pass → next service.
- Fail → `AskUserQuestion`: "Retry same config" / "Change configuration" (back to 6.b) / "Override with reason" / "Abort loop".
  - Override runs `--override-service <type> --override-reason <reason>` and continues.
- Skip / blank input → status `not-tested`; user can re-run later.

### 7. Final summary

```bash
aah run core.gates.validate_cloud_readiness --project-path "$PROJECT_DIR" --check-state
```

Render the aggregate table (service, status, latency, notes) and final `gate_status` + `unmatched_services[]` (from Step 2).

### 8. Commit Phase 1 + hand off to Phase 2

Once the gate is `passed` / `skipped` / all failures overridden:

```bash
cd "$PROJECT_DIR"
git add "$AAH_DIR/architecture/cloud-readiness.yaml"
git commit -m "feat(aah-access): cloud readiness — <gate_status>"
```

`AskUserQuestion`:

- **Continue to Phase 2 (Recommended)** → proceed to Step 9.
- **Stop after Phase 1** → close. `/aah-plan` may accept cloud-only or require data readiness depending on the project.

If Phase 1 failed → do NOT enter Phase 2. Halt with the failure summary.

---

# Phase 2 — Data Readiness

Wraps `aah/core/gates/validate_data_readiness.py`. Read-only — no DDL, migrations, or writes. Emits `$AAH_DIR/architecture/data-readiness.yaml` + `$AAH_DIR/architecture/data-schema-snapshot.yaml`.

### 9. Data handshake

Every data-gate invocation includes `--require-cloud-gate` so the Phase 1 → Phase 2 handshake is enforced:

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate --check-state
```

Same three-outcome pattern as Step 1 (passed / pending / not-started). Handshake failure → halt; resolve Phase 1 first.

### 10. Extract data-bearing services

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate --extract-services
```

If `data_services_count == 0` → run:
```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --skip --skip-reason "no data-bearing services detected"
```
then jump to Step 18.

### 11. Discover schema sources

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate --discover-sources
```

Render the discovered sources (path, file type, tables mentioned, columns extracted). If empty → `AskUserQuestion`:

- Drop schema files in `knowledge/` → pause, re-run discover.
- Specify interactively → proceed to Step 12 with user input.
- Let the gate infer from the live DB → Step 12 uses `--suggest-tables`.
- Skip data readiness → `--skip`, jump to Step 15.

### 12. Collect expectations per service

Category (from Step 10's `services[i].category`) drives the expectation shape. Categories: `database`, `storage`, `api`. If the gate emits a category this skill doesn't recognize, ask the user what to validate — don't drop silently.

**Database services** — `AskUserQuestion`: "What tables should exist in `{display_name}`?"

- If Step 11 surfaced tables from schema files: present them as the recommended answer with option to modify.
- Else use `--suggest-tables '{"service_type":"...","config":{...}}'` to list live tables with relevance scores.
- Follow-up: "Which must have data (≥1 row)?" Pre-suggest names that look reference/config-shaped (`config`, `lookup`, `reference`, `settings`, `seed_*`, `dim_*`) — a hint, not a decision.
- Credentials inherit from Phase 1's secret or default chain. No password prompts.

**Storage services** — `AskUserQuestion`: "What prefixes must exist in `{bucket_name}`?"

- Ask for `prefix + min_files + optional file_types[]`. Options: "Specify prefixes", "Just check bucket is non-empty", "Skip".

**API services** — use the handler's discovery command (e.g. `--suggest-langsmith-datasets` for LangSmith). Present the returned list, user picks.

### 13. Run validation per service

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate \
  --validate '{"service_type":"...","display_name":"...","category":"database|storage|api","config":{...},"expectations":{...}}' \
  ${AWS_PROFILE:+--profile "$AWS_PROFILE"}
```

Render pass/fail immediately with tables_found vs. tables_missing, empty vs. seeded row counts, prefix hit-counts. On fail, `AskUserQuestion`:

- Retry same config / Change expectations (→ Step 12) / Override with reason / Skip this service / Abort loop.

### 14. Deviation report + schema-authority

If Step 11 discovered schema sources AND Step 13 validated live DBs, `--validate` returns deviations (column type mismatch, missing column, extra table, empty reference table). Render them, then `AskUserQuestion`:

- **Use live database schema** → `schema_authority: live-database`.
- **Use schema files** → `schema_authority: schema-files` (DB flagged for migration in `/aah-plan`).
- **Reconcile manually** → `schema_authority: pending-reconciliation`; halt here.

No deviations → note "live matches files" and continue.

### 15. Write schema snapshot

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate \
  --write-snapshot '{"database_results":[...],"storage_results":[...],"schema_authority":"<chosen>"}'
```

### 16. Use-case alignment (optional)

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate --check-alignment
```

Renders which use-cases have backing data. On gaps → `AskUserQuestion`: "Accept (populated later)" / "Blocker (halt)" / "Remove use-case from scope (→ re-run `/aah-discuss`)". Record answer under `use_case_gaps`.

### 17. Data summary + commit

```bash
aah run core.gates.validate_data_readiness \
  --project-path "$PROJECT_DIR" \
  --require-cloud-gate --check-state
```

Render aggregate table. Once `passed` (or all failures overridden):

```bash
cd "$PROJECT_DIR"
git add "$AAH_DIR/architecture/data-readiness.yaml" "$AAH_DIR/architecture/data-schema-snapshot.yaml"
git commit -m "feat(aah-access): data readiness — <gate_status>"
```

### 18. Close

Announce final Phase 2 `gate_status`. Recommend `/aah-plan` next if `passed` or `skipped`.

---

## Failure quick reference

- **Step 4 auth fail** — halt, don't run probes. User fixes and re-runs `/aah-access`.
- **Step 6 per-service `AuthFailure`** — identity valid but IAM permission missing. Report which service + permission; offer override.
- **Step 9 handshake fail** — Phase 1 not passed. Halt Phase 2; user must resolve Phase 1.
- **Zero services** at Step 2 or Step 10 — auto-pass that phase.
- **User abort mid-loop** — jump to the summary step with partial results. `gate_status` reflects reality.
