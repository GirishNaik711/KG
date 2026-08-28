---
name: aah-plan
description: >-
  Execute the AAH planning phase — receives module-map.yaml and design docs from
  aah-architecture, authors ONE feature per module (rich Description + API Contracts, no
  pre-specified test cases) via parallel per-module dispatch, builds the dependency DAG (the
  sequential build order), and validates everything with a single ordered Plan Gate (cheap
  Python checks first, one parallel AI coverage read last), then creates GitHub Issues.
  No waves/tiers — the build phase runs modules sequentially in DAG order.
args:
  feedback:
    type: string | object
    required: false
    description: >-
      Feedback to modify existing plan (add/update/remove features).
      Accepts free-text string (user feedback) or structured object from
      calling skills (e.g., aah-fix). When provided, skill runs in reentry
      mode — skips full generation flow.
---

# /aah-plan — Module-Driven Feature Planning

## Ports — this is the `plan` spine node

The `port-executor-agent` only **fetches** an ordered plan — it never runs anything.
**This skill's main loop runs each port** (so port-skills can use `AskUserQuestion`).
The plan lists ports to run now (`fire`), within-ports to run later at their anchors
(`within_plan`), and ports to only report (`hand_back`, `blocked`).

- **START** (Step 0b) fetches the `before` ports + the `within_plan`.
- **END** (Step 5) fetches the `after` ports; the Plan Gate's Step 7 verifies nothing was missed.

Render each plan as direct markdown (Bash output is collapsed); an empty plan means no
port is bound — continue with zero overhead.

### MANDATORY — how to run a port

A port moves `pending → in-progress → completed`. It is `pending` in the registry
already; you drive the other two transitions, each stamped with its timestamp:

**Announce first (info only — no choice):** immediately before
running each port, tell the user it is auto-triggering, e.g.
> ℹ️ Auto-triggering port **`<id>`** (`<ref>`) — runs automatically as part of this phase

Then:

1. **Before invoking**, mark it started:
   ```bash
   aah run core.ports.executor --project-path "$PROJECT_DIR" update --activity <id> --status in-progress
   ```
2. Invoke by `type`: `skill` → Skill tool · `agent` → Agent tool · `workflow` → Workflow tool.
3. **The instant it returns**, mark it done (values from the plan entry):
   ```bash
   aah run core.ports.executor --project-path "$PROJECT_DIR" \
     update --activity <id> --status completed --artifact <produces>
   ```
   On failure use `--status failed`. A port is not done until step 3 runs. Ports in
   `hand_back` (stub/plugin) are NOT run — surface them.

### MANDATORY — running within-ports at their anchor

Each `within_plan` entry carries an **`anchor_location`** — exactly where in this
skill's flow it must run. For every within-port:
1. After each step, check: does the point you just reached match an un-run entry's `anchor_location`?
2. When it matches, run it there **and only there** (invoke + record, per above). Never
   earlier, later, or batched — wrong-point running is a **failure**, since its output
   must be available to the steps designed to consume it.
3. Never skip or defer. Any within-port still un-run at the close = phase FAILED
   (the Plan Gate's Step 7 catches it). Do not "catch up" by running it late.

---

## Constraints

**Every time this skill says "ask the user", invoke `AskUserQuestion`.** Do NOT present questions as plain text.

Feature generation agents MUST use subagent_type `aah-module-to-feature`. All other agents (C1 critic, etc.) use `general-purpose`.

NEVER include days, hours, weeks, story points, time estimates, duration, velocity,
capacity, or calendar-based language in ANY plan artifact.

---

## Principles

- **One module = one feature.** Each module in module-map.yaml becomes exactly one `F-{MODULE_ID}.md`.
  No per-layer splitting, no `-NN` sequences.
- **The demo test.** Does the module's feature describe something a stakeholder can see working?
- **The dependency litmus test.** (1) Does module B's code import a file module A creates? (2) Do
  module B's behaviors call endpoints module A creates? Either → a dependency edge (module-level).

---

## Rejected Framings

- **"Split a module into many features"** — one feature per module; sizing lives in architecture.
- **"Waves / tiers / sprints"** — there are none. The build runs modules sequentially in DAG order.
- **"Pre-specified test cases / acceptance criteria"** — features carry a rich Description with
  behavioral expectations; the build phase writes tests via TDD from those.

---

## Scope Boundary

This skill does NOT:
- Write or modify design docs — `aah-architecture` owns those
- Generate code scaffolds — the build phase does
- Estimate time or velocity
- Modify source code — Plan produces feature.md files only

---

## Steps

### 0. Load Context

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```

**Each Bash call is a fresh shell — inline `PROJECT_DIR=$(aah run core.common.config project-path)` at the top of every Bash block that uses it.**

Validate inputs from artifacts in `$AAH_DIR/architecture/` and `$AAH_DIR/discuss/`:
- `$AAH_DIR/architecture/module-map.yaml` exists and is well-formed
- Design docs (1–6) present in `$AAH_DIR/architecture/`
- `$AAH_DIR/discuss/discuss-prd.md` exists (product requirements from discuss phase)
- `$AAH_DIR/architecture/schema/MOD-*-api-schema.yaml` — OPTIONAL, read if present. When the directory exists, every API-exposing module must have a matching file; a per-module schema file drives the `api_contracts` block on features carved from that module (§1 dispatch).

Hard fail if inputs missing. Present summary: N modules, DAG structure.

Mark the phase started (after input validation, before the START bookend — mirrors
build's `phase_transition start` preceding its bookend):
```bash
aah run core.common.phase_transition start plan
```

### 0b. START ports bookend — `plan / before` (+ plan `within`)

Fetch the plan (blocking — its `fire` ports are inputs the next steps consume):

```
Agent(port-executor-agent, { node: "plan", position: "before" }, run_in_background: false)
```

Run each `fire` port per "how to run a port"; those artifacts are now inputs.
**Retain `within_plan`** — do NOT run those here; each runs later at the exact
point its `anchor_location` describes.

Populate `stack_choices` if empty (required for industry-default matching):
- Read `$AAH_DIR/architecture/module-map.yaml` → extract layer technologies (e.g., Python, FastAPI, React)
- Read `$AAH_DIR/discuss/decision-registry.yaml` → extract resolved tech decisions (language, framework, database)
- Compose a comma-separated string of primary technologies

```bash
aah run core.common.manifest set-stack primary "<comma-separated tech stack>"
```

Resolve enterprise standards:
```bash
aah run core.standards.resolve resolve --project-path "$PROJECT_DIR"
```
Output: `$AAH_DIR/plan/resolved-standards.yaml`. No-op if no `knowledge/` folder.

### 0.5. Issue Brief — Look Before You Work — MANDATORY, ALWAYS RUNS

**Always run this step — do NOT skip even if prior steps errored.**

Invoke the `aah-issue-reporter` agent with: "Brief me on **plan** issues."
Read the returned brief; decide and act with your phase context (absorb feedback →
re-entry, note feature status (do not close feature issues), or surface untriaged issues). For every issue reviewed,
post a GitHub comment explaining the decision made — whether actioned, deferred to a future
iteration, or out of scope for the current run. Do NOT close unless the issue is fully resolved.

### 1. Author Features (one per module, parallel)

Each module in `module-map.yaml` becomes **exactly one** feature. Author them all **in parallel** —
there is no wave ordering for authoring; dependencies are captured as module-level feature edges and
resolved into the sequential build order by the DAG in Step 4.

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```

**Before dispatch — resolve the `required_env_catalog` once:**

The catalog is the complete set of env var NAMES any module may declare. It covers
**cloud service keys AND non-cloud application-startup keys** — a project with no
cloud-readiness.yaml still needs a `DATABASE_URL`, and the startup env checker
(`ERR_CDR_78_EX_CONFIG`) fails on those just the same. Derive both halves:

- **Cloud keys** — if `$AAH_DIR/architecture/cloud-readiness.yaml` exists and `gate_status != "skipped"`:
  - Read the file → for each service, derive only the env key names from its `coordinates`, `secret_ref`, and `auth_method` (prefix = UPPER(service_type)); never copy values
  - Include `AWS_PROFILE` if cloud_provider is aws and a profile was collected
- **Non-cloud startup keys** — always, from the architecture docs
  (`$AAH_DIR/architecture/`: design docs, module-map.yaml, api-interface-contract.md,
  data-readiness.yaml). Derive NAMES ONLY for configuration the app must read at
  boot, for example:
  - datastore connection strings the design names (`DATABASE_URL`, `REDIS_URL`, …)
  - internal/external service endpoints and base URLs a module calls
  - auth secrets and signing keys a documented auth mechanism requires
    (`JWT_SECRET`, OAuth client ids/secrets, …)
  - the app's own runtime knobs the design specifies (`APP_ENV`, `PORT`, `LOG_LEVEL`)
  Derive only what a design doc actually establishes — do NOT invent plausible
  variables. Never copy a value, only a name.
- Union both halves (de-duplicated, sorted) → `required_env_catalog`
- Neither source yields anything → `required_env_catalog = []`

Module 0 (the topologically-first module, which carries the scaffold-first
feature) is the one that seeds `.env.example` and the startup checker, so its
prompt must see the FULL catalog — that is why the non-cloud half is derived
unconditionally rather than only when cloud-readiness exists.

**The first (Module 0 / scaffold-first) feature must also own any mock/seed/fixture data the rest of the app needs to run locally** — state this in its Description's behavioral expectations, and include the instruction in Module 0's dispatch prompt.

**MANDATORY — verify the resolved `required_env_catalog` before dispatch:**
Print `required_env_catalog` and cross-check it against its sources; do not dispatch with missing
source-backed env context. (There is no `knowledge_used` resolution — the feature spec has no
Knowledge Used section; the implementer reads any referenced docs directly from paths named in the
Description.)

**Before dispatch — get each module's direct dependencies** (module-level edges to stamp into the
feature's `## Dependencies` as `F-{DEP_MODULE_ID}`):
```bash
aah run core.plan.dag_utils get-direct-deps --module MOD-XXX --module-map "$AAH_DIR/architecture/module-map.yaml"
```

**Dispatch one agent per module — ALL in parallel, in ONE message:**

```
Agent(
  subagent_type: "aah-module-to-feature",
  name: "plan-{MODULE_ID}",
  description: "Author the feature for {MODULE_ID}",
  run_in_background: true,
  prompt: "<constructed prompt from template below>"
)
```

**Prompt template — the agent READS the architecture files itself** (it has Read/Grep/Glob), so
pass it the paths + identifiers, not pasted docs. Every section below is REQUIRED:

```
Module id: {MODULE_ID}
Module name: {module_name}
Layers: [{layer_keys from module-map}]

AAH_DIR: {absolute path to the project's .aah}
PROJECT_DIR: {absolute path to the project root}

Read your architecture inputs yourself from AAH_DIR (see your agent instructions, Step 1):
- $AAH_DIR/architecture/module-map.yaml — YOUR module's entry (id == {MODULE_ID})
- $AAH_DIR/architecture/architecture-overview.md, data-model.md, application-flow.md
- $AAH_DIR/architecture/agent-topology.md  (only if this module has an `agent` layer)
- $AAH_DIR/architecture/applications_wireframes/**  (only if this module has a `ui`/`ui-ux` layer)
- $AAH_DIR/architecture/design-spec.yaml + the comp it references via
  `direction.comp`  (only if this module has a `ui`/`ui-ux` layer — the design contract: system,
  tokens, direction rules, per-screen regions/states/data)
- $AAH_DIR/architecture/schema/{MODULE_ID}-api-schema.yaml  (if present; copy tokens verbatim)
- $AAH_DIR/discuss/discuss-prd.md  (product requirements + user stories — ground expectations in these)
- $PROJECT_DIR/knowledge/**  (user-uploaded reference files, if the folder exists — honor them)
- $AAH_DIR/plan/resolved-standards.yaml  (applicable standards, if present)
Ground the Description + behavioral expectations in what you read. Invent no capabilities/screens.

## Direct Dependencies (module-level → the feature's ## Dependencies)
{the `F-{DEP_MODULE_ID}` ids from get-direct-deps for this module, or "None"}

## Required Env Catalog
{required_env_catalog as bare-name bullet list "- VAR_NAME", or empty}

## Feature Template
!`cat "$(aah path)/_resources/_templates/plan/feature.md"`

## Output Path
- Feature: $AAH_DIR/plan/features/F-{MODULE_ID}.md
```

Pass ABSOLUTE paths for `AAH_DIR`/`PROJECT_DIR` so the agent's reads resolve. All static instructions
(how to read inputs, template, section rules) live in the agent definition — do NOT repeat them.

**Dispatch-wait:** dispatch all modules in ONE message. Wait for completion notifications — do NOT
poll with `sleep`, `find`, or `SendMessage`.

If a return is prose, has a thin/empty `## Description` (no behavioral expectations), or is missing a
required `## API Contracts` block for an endpoint module → reject and re-dispatch (max 2 retries).

**After ALL agents complete (notified, not polled):** validate the feature `.md` files — parse with
`load_features_from_dir` and run schema checks. (No module summaries — cross-module dependency context
is carried by the DAG and read directly from upstream code in the build phase.)

### 3. Embed Standards & Build the Feature List (deterministic, runs once)

Embed applicable standards into the feature files, then build `feature-list.json`
**once** (there is no earlier contract-gate run; this is the single build):

```bash
aah run core.standards.embed embed --project-path "$PROJECT_DIR"

aah run core.plan.build_feature_list \
  --features-dir "$AAH_DIR/plan/features" \
  --output "$AAH_DIR/feature-list.json"
```

If `build_feature_list` exits non-zero with contract/schema errors, do not continue:

1. Group the exact errors by feature and owning module.
2. Dispatch one `general-purpose` correction agent per affected module. Give it the affected feature
   paths and exact errors, and require it to read the current files before editing them.
3. Permit changes only to the reported **Description / API Contracts / Dependencies**. Preserve
   feature IDs and every unrelated section verbatim. Do not create, delete, or rename features.
4. Re-run `build_feature_list`. Allow at most two correction attempts per affected module. If
   validation still fails, stop and show the remaining errors; do not create downstream artifacts.

This is contract correction, not feature authoring. Never redispatch `aah-module-to-feature` for
this loop: that write-only agent owns initial authoring and cannot safely edit contracts in place.

### 4. Build the DAG (the sequential build order)

The DAG is the single ordering artifact the build phase consumes — its topological order **is** the
sequential build order. There are **no waves** (`compute_waves`, smoke-test generation, and
checkpoint-config are all wave/AC-coupled and no longer run).

`build_dag` runs `build_and_validate_dag`, which is the **sole owner** of build-order validity —
it validates acyclicity **and** module-edge consistency here. The Plan Gate does NOT re-validate
the DAG; it only confirms `dag.json` exists.

```bash
aah run core.plan.build_dag \
  --features-dir "$AAH_DIR/plan/features" \
  --module-map "$AAH_DIR/architecture/module-map.yaml" \
  --output "$AAH_DIR/plan/dag.json"
```

Checkpoint:
```bash
git add .aah/plan/features/ .aah/plan/dag.json .aah/feature-list.json
git commit -m "feat(aah): author one feature per module and build the dependency DAG"
git push origin HEAD
```

### 5. END ports bookend — `plan / after` (fire before the gate)

The plan artifacts now exist (inputs `after` ports consume). Fetch the `after`
plan, then run each `fire` port per "how to run a port". Fire them **before** the
Plan Gate so the gate's Step 7 (ports finished) can verify their artifacts, and so
they land in the same commit:

```
Agent(port-executor-agent, { node: "plan", position: "after" }, run_in_background: false)
```

Then reconcile (safety net: fired ports whose `produces` exist → completed, tagged
`reconciled`):

```bash
aah run core.ports.executor --project-path "$PROJECT_DIR" reconcile --node plan
```

A within-port still un-run here = phase FAILED; the gate's Step 7 catches it. Do NOT
fire it late, and do not reach step 8's commit/tag — re-run so it fires at its
designed point.

### 6. Plan Gate — one ordered gate

The Plan Gate is **one ordered gate**: cheap, objective Python checks run first and fail fast;
the single AI read runs last and in parallel; nothing loops without converging. Each step is
labelled by its driver.

> **Step 1 — One feature per module.** ` Python` — every module became exactly one feature file, and every file opens cleanly.
>
> **Step 2 — Each feature is filled in.** ` Python` — id, title, owning module, description, and layers are all present and well-typed.
>
> **Step 3 — Each feature says what it should DO.** ` Python` (blocking) — the behavioral-expectations list is present and non-empty. ` AI` (optional, non-blocking note) — flags wording that reads vague. **Vagueness never blocks.**
>
> **Step 4 — The build order makes sense.** ` Python` — produced once by `build_and_validate_dag` (acyclic + module-edge consistent). The gate only confirms `dag.json` exists; it does **not** re-validate.
>
> **Step 5 — Secrets are named, never exposed.** ` Python` — env settings are listed by name only and appear in `.env.example`.
>
> **Step 6 — Nothing the module owns was left out (incl. API contracts).** ` AI` — the **only** AI step: one critic **per feature, in parallel**, checks each feature against what its module owns — capabilities, **screens/UX**, data, and **API contracts** (block present for endpoint modules; `operation_id`/`schema_file` tokens resolve) — and flags anything missing.
>
> **Step 7 — Attached side-tasks finished.** ` Python` — any wired-in ports produced the files they promised.

**Deterministic gate (Steps 1–4, 7).** Run the single aggregator. It owns schema validation,
the non-empty `## Description` check, `dag.json` existence + feature-count parity, and the
fired-port artifact check. Do NOT close the phase until it passes:

```bash
aah run core.gates.validate_plan_gate
```

**AI coverage read (Step 6) — one critic per feature, in parallel.** Dispatch **one
`general-purpose` critic agent per feature, all in parallel in ONE message** — mirroring the §1
`aah-module-to-feature` authoring fan-out. Each critic has no access to the makers' reasoning and
returns the per-feature verdict defined in [CRITIC-PROTOCOL.md](CRITIC-PROTOCOL.md). It checks
**Coverage** (capabilities + screens/UX + data — UX presence lives here, and any wireframe file
the Description cites must resolve on disk, else critical) and **Contract completeness**
(behavioral-expectations presence, blocking; vague wording as a non-blocking `major` note; and
**API-contract correctness**, the single home for the API check). Module
sizing is **not** checked — it is an architecture-phase concern. Verdict: `critical == 0
→ PASS`.

**Revision rule.** Only **Steps 3 and 6** may trigger a **single** auto revision-and-recheck.
On unresolved criticals after that one pass:
   - For each critical finding, suggest a specific fix (e.g., "F-MOD-003 Description has no
     behavioral expectations — add given/when/then bullets"; "F-MOD-002 API operation_id does
     not resolve against its schema file — correct the token").
   - Use `AskUserQuestion` with options derived from your analysis.
   - Always include: "Let me handle this manually".

### 6a. Environment Catalog — generate `.env.example`, ask the user to create `.env`

Now that every feature contract is final, assemble the clean root `.env.example`.
It draws from two sources so the user re-enters as little as possible:

- **Planned features' `## Required Env Variables`** → emitted as empty `NAME=`
  placeholders for the user to fill.
- **`cloud-readiness.yaml` (from `/aah-access`)** → the non-secret coordinates it
  already captured (bucket names, ARNs, endpoints, regions, secret-manager
  reference paths, and the AWS profile) are **pre-filled with their real values**,
  so the user does NOT re-enter what `/aah-access` collected.

This is the same catalog build Module 0 seeds and the startup checker
(`ERR_CDR_78_EX_CONFIG`) enforces — generating it here lets the user create `.env`
up front, so build's required-env test gate (and the runtime validator, which
also reads `AWS_PROFILE`/region from `cloud-readiness.yaml`) has values instead of
blocking. Only non-secret values are ever written; real secrets stay empty.

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
aah run core.plan.build_env_example --project-path "$PROJECT_DIR"
```

Step 8 commits it — it lives at the project root, outside `.aah/`, so the commit
there stages it explicitly.

Then **ask the user** (via `AskUserQuestion`) to create their `.env` before build.
Report the command's JSON: `cloud_prefilled` (already filled — no action) and
`to_fill` (the empty ones they must complete). Note that Claude cannot write
`.env` — the secrets gate blocks it, so they create it themselves:

> I generated `.env.example`. The cloud coordinates `/aah-access` captured
> (<cloud_prefilled>) are already filled in. Copy it to `.env` and fill only the
> remaining values (<to_fill>): `cp .env.example .env`. Build reads real values
> only from the gitignored root `.env`; an empty or missing value fails at build
> with `ERR_CDR_78_EX_CONFIG`.

Offer options such as "I'll create `.env` now" and "I'll set env values at build
start". Either way this is informational — do not block the phase on it. If
nothing needs filling (all values pre-filled, or no env vars declared), say so
and skip the ask.

### 7. Publish GitHub Issues

Run the preflight check:
```bash
aah run core.version_control.cli status
```
If the output reports `enabled: false` or no active project, skip this step entirely and
proceed to Step 8.

Otherwise, set the lifecycle status of every feature to `planned` before syncing so each
GitHub issue is created with the correct label:
```bash
# Run for every feature in feature-list.json
aah run core.common.feature_list update-lifecycle <feature-id> planned
```

Then invoke the `aah-issue-syncer` agent with: "Sync all plan features to the tracker."
The agent runs `sync --from-plan`, creating one GitHub issue per feature with `aah:planned`
label. Review its operator report before proceeding.

### 8. Phase Transition + Commit

The transition itself writes `resolved-standards.yaml`, `manifest.current_phase` and
progress, and pushes without committing — so the phase's single commit comes after it,
and stages `.env.example` too (it sits outside `.aah/`):

```bash
aah run core.common.phase_transition end plan
git add .aah/
git add -f .env.example   # -f: a brownfield .gitignore may carry .env*
git commit -m "feat(aah): complete plan phase — one feature per module + dependency DAG"
git push origin HEAD
MANIFEST_JSON=$(aah run core.common.manifest read)
PROJECT_NAME=$(echo "$MANIFEST_JSON" | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
ITER=$(echo "$MANIFEST_JSON" | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin).get('current_iteration', 1))")
git tag -a "${PROJECT_NAME}/iter-${ITER}/aah-plan" -m "AAH plan phase complete (iteration ${ITER})"
git push --tags origin
```

Leave nothing uncommitted — build starts on this tree.

Present summary: one feature per module, DAG (build) order, coverage status, issues created.

Then tell the user where this phase's artifacts live:

```
**📂 Artifacts for this phase live in `.aah/plan/`:**

| Artifact | Path |
|----------|------|
| Feature specs (one per module) | `.aah/plan/features/` |
| Dependency DAG (build order) | `.aah/plan/dag.json` |
| Feature list | `.aah/feature-list.json` |
| Resolved standards | `.aah/plan/resolved-standards.yaml` |

```

Include any `before`/`within` port artifacts by enumerating `$AAH_DIR/plan`, and omit rows for files
this run didn't produce (`resolved-standards.yaml` is written by the `end plan` transition and is
absent if standards resolution was skipped). Name the directory in the prose too, not just the table.

Then close with the phase-transition guidance:

```
**➡️ Next phase: Build — run `/aah-build` to begin implementation.**
💡 Tip: run `/clear` first to reset context before `/aah-build` — it improves performance on the next phase.
```

Ask user: "Ready to proceed to Implementation?" → `aah-build` or revise.

---

If `--feedback` arg is provided and `$AAH_DIR/plan/features/` contains existing features. Follow below steps.

**First, for every feature the feedback touches, ask the deciding question: has this feature been implemented yet?** Read `$AAH_DIR/feature-list.json` — a feature with `passes: true` is implemented; `passes: false` or absent is pending/unbuilt. Read `current_wave` from `aah run core.common.progress read`.

**Case A — feature is IMPLEMENTED (`passes: true`) → forward rework (superseding entry):**

1. Update the feature's spec `$AAH_DIR/plan/features/<F>.md` **in place** to reflect the feedback (edit the `## Description` behavioral expectations, `## API Contracts`, and `## Dependencies` as needed — preserve the `## Id`). This is the single source of truth.
2. Create the superseding rework entry. This is deterministic — do NOT hand-build the entry or the DAG:
   ```bash
   aah run core.plan.create_rework_entry create \
     --project-path "$PROJECT_DIR" --feature-id <F> --current-wave <current_wave>
   ```
   This creates `<F>-rework-NN.md` as a **pointer stub** — `## Id` + `## Supersedes: <F>` + `## Spec File: <F>.md`, no body — so `<F>.md` stays the single source of truth. The stub's `## Spec File` pointer is resolved at parse time, so its DAG edges derive from the updated `<F>.md`. It syncs feature-list (`passes: false`) and rebuilds `dag.json`; the build runs it in DAG order. (`--current-wave` is accepted for compatibility; there are no waves.)
3. The original `<F>` keeps `passes: true`; the rework entry (`<F>-rework-NN`) carries `supersedes: <F>` and is what gets built. Do NOT reset `<F>`.

**Case B — feature is PENDING/UNBUILT (`passes: false` or new) → just edit the plan:**

1. Load `$AAH_DIR/architecture/module-map.yaml` + existing features. Analyze feedback against module descriptions/layers/scope. Then:
   - New feature needed → generate feature.md (follow template)
   - Existing (unbuilt) feature affected → regenerate (preserve `id`)
   - Feature removed → verify no dependents → delete
2. Re-validate: reconciliation → rebuild the DAG (re-run Step 4) → Plan Gate (the per-feature AI critic scoped to changed features). No rework entry, no special branching — the sequential build reaches it in DAG order.

**Both cases:**

- Report the final list of features that need implementation in the master log's `affected_features` (Case A: the `<F>-rework-NN` id(s); Case B: the new/edited feature id(s)). Include any bug fixes here too so everything builds together.
- Downstream breakage is handled **reactively** — the end-of-build regression gate catches collateral damage and routes it back through aah-fix. Do NOT proactively rebuild transitive dependents here.
- Commit changes with a descriptive message. Present updated summary → Exit.

**Comment back to GitHub issue (if version_control enabled).** If this feedback re-entry was triggered
by a GitHub issue surfaced at Step 0.5, run:
```bash
aah run core.version_control.cli status
```
If enabled, and the commit above completed successfully, post a resolution comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "Plan updated based on this issue: <brief summary — features added/modified/reworked>. Changes committed."
gh issue close <N>
```
Where `<N>` is the issue number from the Step 0.5 brief that triggered this re-entry. If the feedback
came from `$ARGUMENTS` (not a GitHub issue), skip this step. If the commit failed, do NOT close the
issue. If version_control is disabled or offline, skip silently.

## References

- **Plan Gate AI critic (Step 6)** — see [CRITIC-PROTOCOL.md](CRITIC-PROTOCOL.md)
