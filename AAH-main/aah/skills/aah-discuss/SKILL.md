---
name: aah-discuss
argument-hint: "[problem description | feedback for revision, e.g. 'use Claude instead of GPT']"
description: Discovery-driven research phase — sequential decision walk, layered batches, constraint-gated, slug-traceable
user-invocable: true
disable-model-invocation: false
---

# /aah-discuss — Discovery-Driven Research Phase

`/aah-discuss` runs a **sequential decision walk** through project gray areas, floored by
**mandatory questions** and a **hard constraint gate**. Every decision gets a stable **`slug_id`**
in a registry that survives fragmentation across steps. Outputs a confirmed PRD and a list of
downstream skills to activate.

## 🔒 Isolation Contract — CRITICAL

- ALL slug artifacts live under `.aah/discuss/`.
- The decision registry lives at `.aah/discuss/decision-registry.yaml`.
- The PRD lives at `.aah/discuss/discuss-prd.md` — the research gate accepts this path.

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the
`AskUserQuestion` tool.** Do NOT present questions as plain text, markdown, or numbered lists. The
tool provides a structured selectable UI — plain-text questions degrade the experience.

## UX: Minimize Terminal Noise

- Bash output is COLLAPSED in the CLI — the user cannot see it without Ctrl+O.
- After EVERY Bash call, parse the output and present results as **direct markdown text**.
- Keep it conversational — the user should feel like they're talking to a consultant.

## UX: Terminal Icons

- `═══` for major banners · `───` for sub-sections
- ✅ done · ⏭️ skipped · ⚠️ gate failure · 🎯 scope · 🌐 domain · 📋 registry · 🔍 probing · 📝 PRD

---

## Ports — this is the `discuss` spine node

The `port-executor-agent` only **fetches** an ordered plan — it never runs anything.
**This skill's main loop runs each port** (so port-skills can use `AskUserQuestion`).
The plan lists ports to run now (`fire`), within-ports to run later at their anchors
(`within_plan`), and ports to only report (`hand_back`, `blocked`).

- **START** (Step 0.55) fetches the `before` ports + any `within_plan`.
- **Completeness gate** (Step 12a) verifies every fired port completed before close.
- The registry is finalized separately at Step 13 (`compile`) — that merges the
  discuss-triggered activations; it is not a ports bookend.

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
   (the Step 12a gate catches it). Do not "catch up" by running it late.

---

# Steps

## ═══ ENTRY · harness bookend ═══

### 0. Resolve Active Project

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
DISCUSS_DIR="$AAH_DIR/discuss"
mkdir -p "$DISCUSS_DIR"
```
Use `$DISCUSS_DIR` for ALL slug artifacts including the PRD (`$DISCUSS_DIR/discuss-prd.md`).

### 0.3. Issue Brief — Look Before You Work — MANDATORY, ALWAYS RUNS

**Always run this step — do NOT skip even if prior steps errored. Runs BEFORE the iteration check so pending discuss issues are handled before a new iteration is started.**

Invoke the `aah-issue-reporter` agent with: "Brief me on **discuss** issues."
Read the returned brief; decide and act with your phase context (absorb feedback →
re-entry, note feature status (do not close feature issues), or surface untriaged issues). For every issue reviewed,
post a GitHub comment explaining the decision made — whether actioned, deferred to a future
iteration, or out of scope for the current run. Do NOT close unless the issue is fully resolved.

### 0.35. Iteration Check

```bash
aah run core.iteration.manager detect
```
Parse `needs_new`, `current_iteration`, `passing_features`, `total_features`.

- **`needs_new == false`** → continue.
- **`needs_new == true`** → present the completed-iteration summary and use `AskUserQuestion`:
  "This project completed iteration {N}. How would you like to proceed?" →
  ["Start iteration {N+1}", "No — review current state first"].
  - On "Start": ask for the new problem statement (`AskUserQuestion`), then
    `aah run core.iteration.manager new --problem "<answer>"`.
  - On "No": STOP.

### 0.4. Feedback Re-Entry Check

If `$DISCUSS_DIR/decision-registry.yaml` + `$DISCUSS_DIR/discuss-prd.md` both exist AND the
**current-iteration** discuss tag is present → **feedback mode**. Read `current_iteration` from the
manifest and check for `*/iter-${ITER}/aah-discuss` (NOT a bare `*/aah-discuss`) — a tag from a *prior*
iteration must NOT trigger feedback mode, or a freshly-started iteration N+1 would be shunted into
revising the old PRD. (On a fresh iteration the iteration mechanism has already cleared `.aah/discuss/`,
so the artifact half of this check is also false — belt and suspenders.) → **feedback mode**: skip
Steps 0.5–11 and handle the revision below. Two kinds of feedback are supported — a **decision change**
(touches a slug) or a **PRD-prose change** (touches header text that isn't tied to a slug). Classify
first, then act.

**Step A — capture the feedback.**
- If `$ARGUMENTS` is present, that IS the feedback (e.g. `/aah-discuss use Claude instead of GPT`,
  `/aah-discuss reword the problem statement to emphasise latency`).
- If `$ARGUMENTS` is empty, `AskUserQuestion`: "What would you like to revise?"

**Step B — classify the feedback.** Read the registry (`aah run core.discuss.registry read`) and the
header (`$DISCUSS_DIR/prd-header.md`). Decide which kind of change the feedback is:

- **Decision-level** — the feedback changes something backed by a `slug_id` (a model/tool/architecture/
  storage/compliance choice, i.e. anything in `pre_resolved[]` or `decisions[]`). Examples: "use Claude
  instead of GPT", "switch storage to Postgres", "make it SOC2".
- **PRD-prose-level** — the feedback targets the PRD's reader-facing header text that is NOT a decision:
  the **Problem Statement** wording or the **User Stories** list (both authored prose, not slug
  projections). Examples: "reword the problem statement", "add a user story for admins", "the problem
  framing is too generic".
  - Note: the **Solution** section is a projection of the decisions — feedback about the *system's
    design* is decision-level (revise the slug), not prose. Only pure wording/story edits are
    prose-level.
- If ambiguous, `AskUserQuestion` to confirm which kind before proceeding.

**Step C — apply.**

*Decision-level path:*
1. **Map the feedback to the affected `slug_id`(s)** — match the feedback text against each decision's
   `slug_id`, `area`, `question`, and current `response`. E.g. "use Claude instead of GPT" → the
   `*-llm-*` / model-choice slug.
2. **Confirm the mapping with ONE `AskUserQuestion`**: show the matched slug(s), their current value,
   and the proposed change ("I'll revise these decisions — correct?"). If the match is ambiguous or you
   found nothing confident, list candidate slugs and let the user pick. Proceed only once confirmed.
3. Run the §10a surgical re-float on each confirmed slug (each `revise` call updates the registry
   inline — prior response preserved in `revised_from[]`), then **re-run the constraint gate (Step
   10a)**.

*PRD-prose path:*
1. Amend the relevant section(s) in `$DISCUSS_DIR/prd-header.md` per the feedback (re-derive User
   Stories if the Problem Statement shifts, per Step 10). Confirm the revised header text with the user
   via `AskUserQuestion` before writing. No slug is touched and the constraint gate does not need to
   re-run (no decision changed).

**Step D — re-author + close (both paths).** Re-author the PRD (Step 10b) so `discuss-prd.md` reflects
the updated registry and/or header, commit + STOP.

**Step E — comment back to GitHub issue (if version_control enabled).** Run:
```bash
aah run core.version_control.cli status
```
If enabled, and Step D (re-author + commit) completed successfully, post a resolution comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "Decision revised: <slug_id> changed from <old_value> → <new_value> based on this issue. Registry and PRD updated."
gh issue close <N>
```
Where `<N>` is the issue number from the Step 0.35 brief that triggered this re-entry. If the feedback
came from `$ARGUMENTS` (not a GitHub issue), skip this step. If Step D did not complete or commit failed,
do NOT close the issue. If version_control is disabled or offline, skip silently.

### 0.5. Phase Transition — START

```bash
aah run core.common.phase_transition start discuss
```

### 0.55. START ports bookend — `discuss / before`

The registry was materialized by `/aah-init-project`. Fetch this node's `before`
plan (blocking) and run each `fire` port per "how to run a port" — these are the
always-on (`default`) ports that mediate the discovery session:

```
Agent(port-executor-agent, { node: "discuss", position: "before" }, run_in_background: false)
```

**Retain `within_plan`** — do NOT run those here; each runs later at the exact point
its `anchor_location` describes. An empty plan (or all ports handed back as stubs) →
continue with zero overhead. Note: `discuss`'s discuss-triggered activations are
compiled into the registry at Step 13, which is a separate concern from these bookends.

### 0.6. Welcome

Present:
```
### ═══ 🔍 /aah-discuss — Discovery Phase ═══

Welcome. This phase turns your intent into a grounded, traceable set of decisions:

1. **Capture intent** — start from a few lines; I'll elicit the rest
2. **Ingest codebase** — what the code already decided (brownfield)
3. **Discover gray areas** — what's underspecified, confirmed with you
4. **Research & decide** — options floored by your constraints, walked in order
5. **Validate** — a hard gate proves no constraint is violated
6. **PRD + activations** — a prose PRD (you confirm before I write it), plus which follow-on skills to run
───
```

---

## ═══ INGEST & SCOPE ═══

### 1. Capture User Intent

If `$ARGUMENTS` provided, use it as the problem statement; else `AskUserQuestion`:
"In a few lines — what do you want to build or improve?"

**Persist to the knowledge base** (survives compaction). The `mkdir -p` is a defensive guarantee —
`/aah-init-project` only creates `knowledge/` when the user opts into supplementary docs, so it may not
exist on the skip-docs path:
```bash
mkdir -p "$PROJECT_DIR/knowledge"
```
If `knowledge/project-brief.md` does NOT exist, write it fresh with the problem statement under a
`# Project Brief — Original Context` heading. If it DOES exist (iteration 2+), read
`current_iteration` from the manifest and **append** a `## Iteration {N}` section — do NOT overwrite.

```bash
aah run core.knowledge.main parse --project-path "$PROJECT_DIR"
```

**Consume the parsed knowledge — do NOT stop at `parse`.** `parse` only caches the extracted text;
it does not feed it back into the walk. Immediately pull the parsed content into context:

```bash
aah run core.knowledge.main context --project-path "$PROJECT_DIR"
```

Read the returned `## Project Knowledge Base` block and hold it in-context for the rest of the walk.
Every user-supplied doc (PDFs, DOCX, PPTX, meeting notes) is surfaced here. Use it to ground intent
analysis (Step 3), gray-area discovery (Step 6), and inline option prep (Step 8). If the block is
empty, the folder held no parseable docs — continue without it. (Nothing re-parses it later — the
inline option prep at Step 8 reads the block you are holding now.)

### 2. Read Codebase (if any) — Brownfield Pass

```bash
aah run core.common.manifest read
```
Read `project_type` (greenfield/brownfield). **Skip 2b if greenfield** or no `codebase-intel/`.

#### 2b. Brownfield intel read (hold in-context — do NOT categorize or write yet)

Read every `.aah/codebase-intel/*` file that exists : `stack-summary.md`, `tech-stack.md`, `architecture-diagram.md`, `data-flow.md`,
`dependency-map.md`, `codebase-learning.md`, `tech-debt-assessment.md`, `codebase-profile.json`, etc.
Skip missing files silently.

**Only read and hold the codebase-derived facts in-context here.** Do NOT sort them into
`pre_resolved | open | skipped`, and do NOT write to the registry. The split is a predicate over
`target_areas[]` from `project-intent.yaml`, which does not exist until Step 3 — so categorization and
the write both happen in the consolidated **Step 5.3**, the single owner of `pre_resolved[]`.

> **Only `pre_resolved` is a registry field.** `open`/`skipped` are *mental* buckets held in Claude's
> context, never persisted (the schema in `_empty_registry` has no `open[]`/`skipped[]`). `open` items
> become gray areas at Step 6; `skipped` items are dropped. Only `pre_resolved[]` is written (Step 5.3).

### 3. Analyze Intent

Extract and record into `$DISCUSS_DIR/project-intent.yaml` with fields `raw_statement`,
`target_areas[]`, `constraints[]` (plus `improvement_type` — brownfield only, see below):
- **`improvement_type`** — record **only if `project_type == brownfield`** (from Step 2). Brownfield
  values: `enhancement | migration | bug_fix`. For **greenfield, OMIT the field entirely** — a
  from-scratch build has nothing to "improve"; greenfield is expressed by `project_type`, not an
  improvement tag.
- **Technical domains** — API, frontend, data, ML/AI, infra, etc.
- **Integration surface** — count external systems/APIs
- **Process flow & end-users**

### 4. Enrich from Knowledge Base — gap-driven

Make the second doc ask *informed*, not a generic re-poll. Compute the coverage gap between what intent
needs and what the user already supplied:

1. **Gaps** = `target_areas[]` / technical domains / integration surface (from `project-intent.yaml`)
   ⊖ topics already covered by the Step-1 `## Project Knowledge Base` block.
2. **If no gaps** → do NOT ask. State what's covered ("Your docs already cover {areas} — continuing").
3. **If gaps exist** → `AskUserQuestion` whose options are the SPECIFIC missing artifacts (e.g.
   "Auth/compliance spec — intent flags SOC2, none supplied"; "Integration contract for {system}"),
   plus "None of these — proceed". Frame it: "You've shared {A, B}; your intent also touches {Y, Z},
   which I don't have a source for yet."
4. On new docs → `aah run core.knowledge.main parse --project-path "$PROJECT_DIR"` (hash-cached — safe
   and cheap to re-run) followed by `aah run core.knowledge.main context --project-path "$PROJECT_DIR"`
   to ingest. Extract key items into `project-intent.yaml` so no critical component is missed.

> Gap detection is Claude-side judgment (match intent against KB topics) — no new `core.knowledge`
> subcommand; the module already supplies both inputs (`context` + the intent YAML).

### 5. Determine Delivery Intent — and Fast-Path Branch

Ask the user directly (`AskUserQuestion`, unless the brief is unambiguous):

- **`poc`** — quickest possible walk. Fewer gray areas, shallower probing.
- **`mvp`** — standard walk. Every applicable gray area is probed at Step 7's chosen mode.
- **`prod`** — deepest walk. Every applicable gray area + emergent-area rescan is aggressive.

> **Skip the `core.intake.classifier`** — `/aah-discuss` has no `intake.json` to feed it, so it
> returns placeholders. User-declared delivery intent above is authoritative.

**Branch on `improvement_type` (brownfield only — greenfield has no `improvement_type`, so it can never
match the fast-path and ALWAYS takes the regular flow):**
- **`bug_fix`** (brownfield) → **FAST-PATH**: write a minimal slug registry (intent + 1–2 gray
  areas), **skip Steps 5.3–5.5**, jump to Step 6.
- **otherwise** (incl. all greenfield) → **REGULAR FLOW** (Steps 5.3–6). `poc` still uses this flow — it
  just narrows the gray areas confirmed at Step 6.

### 5.3. Pre-Resolved Inferences (greenfield + brownfield)

The **single canonical place** where pre-resolved inferences are produced, confirmed, and written.
Runs after intent analysis (Step 3/4), so `target_areas[]` is available as the classification key.
This step is the **only writer** to `pre_resolved[]` (brownfield Step 2b holds candidates but never
writes).

**── Ensure the registry exists (domain-less init) ──**

First decision-writing step, so ensure the registry exists — WITHOUT domains/archetypes (the domain
step sets those non-destructively via `set-domains`). `init` is idempotent (returns the existing
registry if present) so this is always safe to call; never `--force` here:
```bash
aah run core.discuss.registry init --project-name <name> --complexity <poc|mvp|prod>
```

**── Gather candidates (path-split producer) ──**

- **Brownfield** (`codebase-intel/` present): using the intel facts held from Step 2b, categorize each
  decision the code implies, keyed by a stable `slug_id`:
  - **pre_resolved**: code embodies it AND user intent (`target_areas[]`) does NOT target this area
    (e.g. `data-store-choice`, `backend-language`). → **written** to `pre_resolved[]` (`--source codebase`).
  - **open**: user intent targets this area. → NOT written; becomes a gray area at Step 6.
  - **skipped**: irrelevant and not embodied in code. → NOT written; dropped.
  Present as the three-table **Decision Space Analysis** (Already Decided / Needs Resolution / Not In Scope).
- **Greenfield** (or no `codebase-intel/`): derive candidates from the brief + `project-intent.yaml`
  (`--source inferred`, `--source-detail knowledge/project-brief.md`). Present as an "Inferred Decisions" table.

Applies to any slug (mandatory or gray-area) inferable from brief / `project-intent.yaml` / codebase-intel.

> **Do NOT pre-resolve `business-domain` / `technical-archetype` here.** Those two slugs are owned by the
> domain step (5.5), which records them via `add-decision` (into `decisions[]`) alongside `set-domains`.
> Even if brownfield intel strongly implies an archetype (e.g. the repo *is* an ai-app), leave it for
> 5.5 — pre-resolving it here would duplicate the slug across `pre_resolved[]` and `decisions[]`. Feed
> the codebase signal to 5.5's detection instead.

**Confidence heuristic:** unambiguous phrasing (*"must be SOC2 compliant"*) → `high`. Qualified
phrasing (*"initially"*, *"flexible for now"*, *"considering"*, *"TBD"*) → `medium`, flag for user
attention. Never `high` on any of those qualifier words.

**── Confirm + write (shared gate — both paths) ──**

1. Present every candidate grouped for visibility (the table(s) above).
2. `AskUserQuestion` to confirm or adjust the set — the user can reject, correct, or reclassify any
   inference before it's written.
3. Apply the user's adjustments to the working set.
4. For each **accepted** inference, invoke `add-preresolved` — the ONLY pre-resolved write in the walk.
   Choose `--source` by origin (see Step 9 for the canonical enum), path always in `--source-detail`:

   ```bash
   # Greenfield / brief-derived
   aah run core.discuss.registry add-preresolved \
     --slug-id <slug> --value <v> \
     --source inferred --source-detail "knowledge/project-brief.md" \
     --notes "brief: '<exact snippet from the brief>'"

   # Brownfield / code-derived (from Step 2b's held candidates)
   aah run core.discuss.registry add-preresolved \
     --slug-id <slug> --value <v> \
     --source codebase --source-detail "<path-to-codebase-intel-file>" \
     --notes "code: '<what the intel file embodies>'"
   ```

> **The `pre_resolved[]` write is load-bearing, not ceremony.** Three readers depend on it:
> (1) `collect_answers` (`guidance.py`) folds it into the slug→value map that **suppresses
> already-answered mandatory questions** at the Mandatory-Q step (5.4); (2) constraint gate **Check A**
> (`validate_constraints.py`) counts a `pre_resolved` entry as a satisfied mandatory slug;
> (3) `activations.py` flattens its `activates[]` for Step 13.

> **This step handles only brief/code-derived pre-resolved inferences.** Domain/archetype selection and
> its overlay are the domain step's job (5.5). (The overlay does not emit compliance/integration seeds.)

### 5.4. Mandatory-Q Batch — BEFORE domain detection & gray-area discovery

Ask **all applicable mandatory questions** from `decisions_guidance.yaml` **first** — before domain
detection (5.5) and before identifying gray areas (Step 6). Running mandatory first means the answers
(regime activations, provider choice, methodology, tooling assumptions) are available as context that
makes the domain/archetype detection in 5.5 more accurate, and grounds gray-area identification in real
answers rather than late-arriving values.

**Procedure:**
1. Query `aah run core.discuss.guidance mandatory-questions --answers-json '<current-answers>'`
   to get the currently-applicable mandatory questions.
2. Batch them by dependency (≤4 per `AskUserQuestion` batch, path-dependent slugs in later batches):
3. **Skip slugs already in `pre_resolved[]`** (Step 5.3 confirmations count as coverage for Check A).
4. Re-query `mandatory-questions` after each batch — the applicable set changes based on prior answers.
5. Write each answer to the registry via `aah run core.discuss.registry add-decision` (or
   `add-preresolved` for inferred values) with `--notes` capturing the user's phrasing + rationale.
   **Always pass `--options-presented-json`** with the exact option set you showed in the
   `AskUserQuestion` batch (e.g. `'["yes","no"]'` for a capability check, or the full choice list) —
   this preserves the menu the user chose from, not just their answer, so mandatory decisions stay
   auditable by the constraint checks (which iterate `options_presented`). Pass
   `--options-eliminated-json` too whenever a regime/mandate filtered an option out.

### 5.5. Domain & Archetype Resolution — overlay the harness's curated context

After the mandatory-Q batch, detect and confirm the **business domain(s)** and **technical
archetype(s)** that match this project, then overlay their curated briefs/playbooks into the rest of
the walk. Detection now leverages the mandatory answers just collected (regime, provider, methodology)
as additional context. The overlay grounds gray-area discovery (Step 6) and the inline option prep
(Step 8) in real domain processes and archetype patterns — instead of a generic list.

These curated assets ship with the **harness**, not the project — resolve the framework asset root
first (never assume `$PROJECT_DIR/aah/...`, which only exists if the project vendors the harness):

```bash
FRAMEWORK_DIR=$(aah path)
cat "$FRAMEWORK_DIR/domain-briefs/_taxonomy.yaml"      # per-node description + keywords
cat "$FRAMEWORK_DIR/build-playbooks/_registry.yaml"    # per-archetype description + keywords
```

**Procedure:**

1. **🔍 Detect.** Pick the top candidate **domains** (node ids from `_taxonomy.yaml`) and **archetypes**
   (keys from `_registry.yaml`) that fit — with a one-line rationale each. Present as **direct
   markdown**, framed as help, not interrogation. Match the project intent (`project-intent.yaml` `raw_statement` + everything gathered so far, including the mandatory answers) against
  each node's/archetype's `description` + `keywords`. `keywords` are **matching hints, not a scoring
  algorithm** — use judgment, including semantic/paraphrase matches that share no literal keyword.
  **Skip any `_taxonomy.yaml` node marked `stub: true`** — those carry no own content (they only
  inherit from a parent) and would yield a hollow overlay; offer their nearest non-stub ancestor/leaf
  instead. (The `_registry.yaml` already lists only authored archetypes.)
  If NOTHING is a plausible match in either dimension →
   say so in one line ("No closely matching harness artifacts identified — continuing without overlay")
   and **skip to Step 6**.

2. **🌐📦 Confirm.** ONE `AskUserQuestion` invocation with TWO **multiSelect** questions for domains and archetypes each.

3. **📋 Persist:**
   - **Record domains NON-DESTRUCTIVELY.** The registry already exists (ensured at Step 5.3), so record
     the confirmed selections with `set-domains` — NOT `init --force`, which would wipe `pre_resolved[]`
     and any decisions already written. `set-domains` preserves the document and `_save` rebuilds the
     overlay in `active_references`:
     ```bash
     aah run core.discuss.registry --project-path "$PROJECT_DIR" set-domains \
       --domains "<csv of selected domain ids>" \
       --archetypes "<csv of selected archetype keys>"
     aah run core.discuss.registry --project-path "$PROJECT_DIR" add-decision \
       --slug-id business-domain --area "Domain & Archetype" \
       --question "Which business domain(s) does this project target?" \
       --options-presented-json '<json list of the domain candidates you surfaced in the Detect step>' \
       --response '<json list of selected domain ids>' --response-type multi-select \
       --source user --notes "confirmed from surfaced harness domain briefs"
     aah run core.discuss.registry --project-path "$PROJECT_DIR" add-decision \
       --slug-id technical-archetype --area "Domain & Archetype" \
       --question "Which technical archetype(s) apply?" \
       --options-presented-json '<json list of the archetype candidates you surfaced in the Detect step>' \
       --response '<json list of selected archetype keys>' --response-type multi-select \
       --source user --notes "confirmed from surfaced harness build playbooks"
     ```

4. **📦 Load the overlay.** Assemble the selected briefs/playbooks for downstream use:
   ```bash
   aah run core.discuss.overlays --project-path "$PROJECT_DIR" load \
     --domains "<csv>" --archetypes "<csv>" --json
   ```
   Parse and hold in-context (do NOT re-derive later): `domain_brief_paths[]`,
   `archetype_playbook_paths[]`, `overlay_summary_md`.

   **Enrich the pattern list with concrete services before presenting.** `overlay_summary_md`'s
   "Patterns:" line only has category names (e.g. "State & Data Stores"), not the concrete AWS
   services inside them — those live in each pattern's `description`. `Read` each
   `archetype_playbook_paths[]` file and replace that line with a bulleted list, one pattern per
   line, `Pattern Name — services` (bullets, not inline "·" — a per-item comma list gets unreadable
   inline):

   ```
   **Patterns in play:**
   - State & Data Stores — Amazon RDS/Aurora, Amazon DynamoDB, AWS Secrets Manager
   - Object Storage — Amazon S3
   ```

   Keep the rest of `overlay_summary_md` as-is; present the enriched result as direct markdown.

5. **Confirm AWS service alignment (`aws-native-services` only).** If confirmed, do NOT re-list the
   services from item 4 — refer to them only as "the services listed above." Follow immediately with:

   > "Just to be clear: the services listed above are the ONLY AWS services this harness can
   > provision, validate (`/aah-access`), and deploy (`aah-deploy`) for this build. Anything proposed
   > later that isn't on that list is out of scope and won't be buildable end-to-end. Are you aligned
   > with scoping this project to only those services?"

   `AskUserQuestion` → `["Yes — proceed", "No — something's missing or wrong"]`.
   - **Yes** → proceed to Step 6.
   - **No** → one follow-up on what's missing; record under `deferred_ideas[]` with an
     `"aws-service-scope-concern:"` prefix (audit trail), then proceed to Step 6 regardless — the
     harness can't expand the playbook mid-walk.

These outputs feed Step 5.5 (mandatory pre-fill), Step 6 (gray-area seeding), and Step 8 (inline
option prep reads `domain_brief_paths[]` + `archetype_playbook_paths[]` from the registry's
`active_references` block). Mandatory questions are handled in Step 5.5 before gray-area discovery begins.


### 5.5. Mandatory-Q Batch — BEFORE gray-area discovery

Ask **all applicable mandatory questions** from `decisions_guidance.yaml` **before** identifying gray
areas. This ensures the walk enters Step 6 with the full mandatory context — regime activations,
provider choice, methodology, tooling assumptions — so gray-area identification and downstream
option filtering are grounded in real answers, not late-arriving values.

**Procedure:**
1. Query `aah run core.discuss.guidance mandatory-questions --answers-json '<current-answers>'`
   to get the currently-applicable mandatory questions.
2. Batch them by dependency (≤4 per `AskUserQuestion` batch, path-dependent slugs in later batches):
3. **Skip slugs already in `pre_resolved[]`** (Step 5.3 confirmations count as coverage for Check A).
4. Re-query `mandatory-questions` after each batch — the applicable set changes based on prior answers.
5. **Backend-driven recommendations.** Some options carry a `RECOMMENDED when backend = …` note in
   their description (e.g. `frontend-compute-platform`). When presenting such a slug, read the
   already-answered `backend-compute-platform` and tag the matching option `(Recommended)` (present
   it first): agentcore-runtime → `amplify`; ecs-express-mode / cloudless-managed / self-managed →
   `ecs-fargate`. The user may still pick another option. (AWS-only for now.)
6. Write each answer to the registry via `aah run core.discuss.registry add-decision` (or
   `add-preresolved` for inferred values) with `--notes` capturing the user's phrasing + rationale.
   **Always pass `--options-presented-json`** with the exact option set you showed in the
   `AskUserQuestion` batch (e.g. `'["yes","no"]'` for a capability check, or the full choice list) —
   this preserves the menu the user chose from, not just their answer, so mandatory decisions stay
   auditable by the constraint checks (which iterate `options_presented`). Pass
   `--options-eliminated-json` too whenever a regime/mandate filtered an option out.
---

## ═══ DISCUSSION & RESEARCH ═══

### 6. Identify & Confirm Gray Areas

Using complexity + intent + codebase + domain + archetype , name the underspecified areas. **Exclude any area already
covered by `pre_resolved[]`.** Areas must be **orthogonal**. Follow **MECE** rule.  — Never probe the same decision twice.
**Core areas** (always candidates):

- **Business problem & outcomes** — the *why*, the value, what a good result looks like
- **Solution flow & end-users** (incl. edge cases)
- **Business rules & domain logic** (internal rules, overall domain compatibility)
- **Contradictions & Ambiguities** (clarify contradictions in the flow, ambiguous meaning)
- **Data & state architecture** (model, persistence, session/memory)
- **Integration & external interfaces** (APIs, external systems + their auth)
- **Architecture & technical foundation** (stack, constraints, timeline, budget)
- **Security, privacy & compliance** (authn/authz, secrets, PII, regulatory)
- **Quality attributes / NFRs** (performance, scalability, reliability, availability)
- **Backend, Frontend Deployment, Ops & observability**
- **UI / UX / accessibility** (incl. i18n)

**Signal-gated areas** — probe only when triggered; each sources a downstream activation (Step 13),
so probing it should expect to record an `activates`:
- **Agentic AI development (orchestration, topology, memory, state, SDK, guardrails, MCP, A2A)** — if agentic-AI signals detected

**Don't ask what's already gathered** from intent/KB/codebase. Frame each area around 5W1H.

`AskUserQuestion` (multiSelect): confirm **which gray areas to probe**. For `poc`, scope to the minimum.


### 7. Pick Research Mode

`AskUserQuestion`: "How deep should each gray area go?" →
- **options-only** — options, no analysis/recommendation (fastest)
- **essential** — options + brief best-practices, no recommendation
- **detailed** — options + full trade-offs + recommendation (only `detailed` shows a recommendation)

Mode tunes OUTPUT depth only. Step 8 prepares every area inline in this session
regardless of mode — you load references (including URLs) once and filter at the
chosen depth, one area at a time.

### 8. Arrange the Decision Walk, Then Elicit

**Arrange.** Order confirmed gray areas as a **sequential DAG walk at `slug_id` granularity** — an
edge A→B means B's valid options depend on A's answer. Gating/constraining decisions (business
problem, cloud/stack, delivery mode, core process, solution flow) come first. Areas are the
presentation grouping; edges drive ordering across AND within areas.

**Outer loop — one gray area at a time, in DAG order.** Prepare that area's options inline (below),
present them, collect answers, then move to the next area.

### MANDATORY · prepare each area's options INLINE — no subagents

Prepare every area yourself, in this session, one at a time in DAG order. Never present an unprepped
area.

**Once, before the first area** (do not redo per area):

1. **Read the registry:** `aah run core.discuss.registry read` → all prior answers (`pre_resolved[]` +
   `decisions[]`) and the `active_references` block. That block is authoritative and already resolved:
   `guidance_references[]` (compliance/infra mandates + wired URLs), `domain_brief_paths[]`,
   `archetype_playbook_paths[]`. Load what it names; never recompute references.
2. **Load them.** Non-URL paths resolve against `$FRAMEWORK_DIR` — never Read one bare (if it's
   missing and starts with `aah/`, retry with that prefix stripped). `kind: url` → `WebFetch`, and you
   may follow same-host links **one hop, ≤3 pages** when clearly relevant — no `WebSearch`, no guessed
   URLs, no other hosts. Load every `guidance_references[]` entry; if one can't be loaded, tell the
   user which and why rather than skipping it silently. Never invent content you didn't read. Where a
   local mandate and fetched prose overlap, **the local mandate wins**.
3. **Overlays.** Domain briefs and v1 archetype playbooks are **advisory** — they make questions
   domain-aware, never eliminate options. A v2 playbook (`schema_version: "2.0"`) is **binding**: read
   its `guidance` + `references[]` (strip the leading `aah/`; `kind: skill` → `skills/<ref>/SKILL.md`)
   and drop options they don't support. Two v2 playbooks → intersection. Missing file → skip silently.

**Per area, on arrival:**

4. **Rules:** `whitelists` · `forbidden-options` · `mandatory-questions --answers-json '<answers>'` ·
   `follow-ups --parent-slug <s> --parent-value <v>` (inside a follow-up subtree).
5. **Generate from your own knowledge, then filter before presenting.** The loaded references are your
   **filter, not your catalog**. Drop anything violating a mandate, a binding playbook,
   `forbidden_options`, a whitelist, or the user's stated constraints — record each drop with the
   quoted reason, citing the mandate ID (`CC-*`) where that's the cause. Ambiguous fit → keep and note
   why — **except** for a binding (v2) playbook whose `guidance` enumerates the *only* services in
   scope (e.g. `aws-native-services`): there, an option not among its named `patterns[].name`/
   description services is OUT of scope by default, even if it sounds plausible, is a general best
   practice, or composes two already-listed primitives (e.g. a turnkey RAG offering when storage +
   vector index + LLM are already named separately) — DROP it with reason `"not named in <playbook>'s
   enumerated service list — no benefit of the doubt"`. Exception: keep it if it's clearly the same
   resource under a different label (e.g. "Aurora PostgreSQL" for "Amazon RDS / Aurora PostgreSQL"),
   not a different service. This default-deny applies only to scope-enumerating v2 playbooks —
   compliance/infra mandates keep ambiguous-fit → keep + note, since those are restrictive rules, not
   closed catalogs. **Never float an option Check E would reject**: this filter is primary, and Check E
   is the only independent one behind it.
6. **Frame from `project-intent.yaml`, the Step 1 knowledge base, and every answer earlier areas
   already wrote** — *"Given AWS is already in use, for auth…"*. Never invent context absent from the
   registry.
7. **Batch by independence** — ≤4 slugs, no `applies_when` edges within a batch; path-dependent slugs
   go later.
8. **Depth = Step 7's mode.** `essential` adds a ≤3-sentence best-practices note; `detailed` adds that
   plus a trade-off table and a recommendation (rationale + confidence) — **required even on thin
   substrate**, marked `confidence: "low"`.

**Emergent areas** (Pass 1 below) are prepped the same way.

### Per-area handling (in DAG order)

1. Surface the options you filtered out — the user sees what you dropped, and why, before
   answering. For a project scoped to the AWS Native Services playbook specifically, don't present
   that drop as generic noise — tie it explicitly back to the boundary confirmed at Step 5.5:
   *"Filtered out `<option>` — it goes beyond the AWS services already confirmed for this build (AWS
   Native Services playbook); we don't go beyond that list."*
2. Present batches, collect answers, write registry (Step 9).

Before presenting any area, confirm no mandatory slug re-surfaced — if one is open, complete it
inline via `mandatory-questions --answers-json`.

**Inner loop — batch by independence.** One `AskUserQuestion` batch = a topological layer (≤4
independent slugs, no edges between them). A slug with an outgoing edge goes in an earlier batch;
never co-present two joined by an edge. When Step 7 mode is `detailed`, precede the batch with a
markdown comparison table (option · pros · cons · recommendation).

**Rendering your recommendation (detailed mode).** When you have a `recommendation` for a slug,
surface it as an EXPLICIT PROSE BLOCK above the trade-off table for that slug — not compressed into
the option description or a bare `(Recommended)` tag on one option.

Do NOT drop the rationale to save vertical space. `AskUserQuestion` labels are lossy; the prose block
is where your reasoning actually reaches the user. Silently omitting it downgrades the walk from
`detailed` to `essential` from the user's perspective.

After a batch resolves:
- If any answer **changed the downstream option space** (carried an `activates`, or eliminated
  options), regenerate the next batch against the new frontier. Otherwise proceed with the
  pre-generated next batch — no regen, full speed.
- `AskUserQuestion`: ["More questions", "Next area"]. "More" advances to the next batch; "Next area"
  moves the outer loop on. Out-of-scope mentions → record as `deferred_ideas`.
- Record each option's `activates: [...]` on the answer (Step 9, Step 13).

**Back-edges.** If an answer invalidates a decision resolved in an **earlier** area, do NOT restart —
route it through the **surgical re-float loop (§10a)**, reopening only the affected `slug_id`s.

**Emergent areas & contradiction scan.** After the last planned area, run **two passes** against the
full registry before advancing to Step 9:

**Pass 1 — Missed areas.** Look for gray areas the walk revealed but Step 6 didn't foresee (e.g. a
data-storage answer surfaced backup/retention as an unwalked topic). Confirm via `AskUserQuestion`
(same gate as Step 6), walk them, then return to Pass 2.

**Pass 2 — Contradiction scan.** Read every entry in `pre_resolved[]` + `decisions[]` and check for
four contradiction classes. When any is found, present it via `AskUserQuestion` with resolution
options — the user picks, then run the §10a surgical re-float on the chosen slug(s):

- **A · Direct logical conflict** — two answered slugs whose values cannot both hold. Example:
  `cloud-vs-local=cloud` + `development-methodology=full-local`.
- **B · Regime-vs-choice violation** — a chosen option violates a `mandate.statement` from any
  active `compliance/` or `infrastructure/` reference file. You already filtered at generation time
  (Step 8), but this backstop catches free-text answers, revised decisions, or mandate statements you
  judged ambiguous and kept. With prep inline, this is the only independent check on that filter —
  run it carefully.
- **C · Missing implication** — an answer implies a decision that was never asked (e.g.
  `data-storage-present=yes` with no backup/retention slug walked; `custom-ui-required=yes` with no
  accessibility slug walked). Route these back into Pass 1 as a missed area, not a contradiction —
  they need a walk, not a resolution.
- **D · Cross-area drift** — earlier answers may be stale given later choices (e.g. `auth-strategy=oauth`
  + `custom-ui-required=no` — OAuth may be over-engineered for a headless system). Surface as
  *"suspected drift — confirm still correct?"* Not always a real conflict.

**AskUserQuestion resolution shape (per contradiction found):**

```
"We noticed a possible conflict:
  · <slug_A> = <value_A>  (Area: <area_A>)
  · <slug_B> = <value_B>  (Area: <area_B>)
  Why: <mandate_statement or logical-conflict explanation>
  How to resolve?"
  → ["Revise <slug_A>", "Revise <slug_B>", "Both are intentional — record as accepted",
      "This isn't actually a conflict — dismiss"]
```

- **Revise A** or **Revise B** → run §10a surgical re-float on that slug.
- **Both intentional** → append the pair to `deferred_ideas[]` with an "accepted-exception:" prefix
  so it's visible in the PRD.
- **Dismiss** → record under `deferred_ideas[]` with a "dismissed-false-positive:" prefix (audit
  trail — proves you looked).

Repeat Pass 2 until no new contradictions surface OR the user dismisses all remaining candidates.

Show progress at **area** granularity ("area 5 of 9"); scans run after the last area but before Step 9.

### 9. Write Registry Incrementally — After EACH Gray Area

As each gray area resolves, append its decisions (anti-data-loss checkpoint):

```bash
# Ensure the registry exists — normally already done at Step 5.3 (domain-less).
# `init` is idempotent (returns the existing registry if present), so this is
# always safe to call — including on the bug_fix fast-path that skipped 5.3.
# Never --force here; domains are set separately via `set-domains` (Step 5.5).
aah run core.discuss.registry init --project-name <name> --complexity poc|mvp|prod

# Per pre-resolved mandatory  (source ∈ {user, inferred, codebase} — canonical enum)
aah run core.discuss.registry add-preresolved \
  --slug-id <slug> --value <v> \
  --source inferred|user|codebase --source-detail "<file-or-path>" \
  [--notes "..."]

# Per walk decision
# --notes is EXPECTED (not optional) on every add-decision. Capture what the user
# actually said + the rationale — you re-read these notes when prepping LATER
# areas, to frame follow-up questions conversationally instead of catalog-style.
aah run core.discuss.registry add-decision \
  --slug-id <slug> --area "<area>" --question "<q>" \
  --options-presented-json '<json>' --options-eliminated-json '<json>' \
  --response <r> --response-type single-select|multi-select|free-text \
  --source user|inferred|codebase --activates-json '<json>' [--emergent] \
  --notes "<user phrasing + rationale, e.g. 'chose oauth — user said we already run Cognito'>"

# On surgical re-float — preserves prior response in revised_from[]
aah run core.discuss.registry revise --slug-id <slug> --response <new> --reason "<why>"
```

Per-decision shape written: `{slug_id, area, question, options_presented[], options_eliminated[], response, response_type, source, revised_from[], notes, activates[], emergent?}`.

**Canonical `source` enum** — enforced by argparse `choices` on every subcommand. Only three values are legal:
- `user` — user answered interactively via `AskUserQuestion`
- `inferred` — Claude derived from brief / project-intent.yaml / knowledge base
- `codebase` — extracted from `.aah/codebase-intel/*` during brownfield Step 2b

Origin-specific details go in `--source-detail` / `--notes`, never in `--source`. The registry is
**slug-keyed** and carries elimination provenance (`constraint_id` → `mandate_id`).

---

## ═══ OUTPUT & GATES ═══

### 10. Confirm the PRD Header (do NOT render yet)

The PRD is a prose projection of the registry with three reader-facing header sections:

- **Problem Statement** — restate the **user's actual problem** in the user's own terms. Source of
  truth: `knowledge/project-brief.md` (the raw brief the user typed at Step 1) + `project-intent.yaml`
  (`raw_statement`, `target_areas[]`, and `improvement_type` *if present* — greenfield omits it, so frame
  from `raw_statement` + `target_areas[]` alone). Quote the brief's key phrasing verbatim
  where it captures the pain. Say **who** has it, **what** hurts today, and **why now**. Do NOT invent
  motivation the brief didn't state; do NOT paraphrase into generic business-speak.
- **Solution** — a **comprehensive prose synthesis** of every decision made. Read the FULL
  `.aah/discuss/decision-registry.yaml` — both `pre_resolved[]` and `decisions[]` — and describe
  the system that emerges: architecture stance, data & storage posture, integrations, UI/deployment
  choices, compliance regimes, dev methodology, tooling assumptions (CLI/Docker), and any emergent
  decisions. Ground every claim in a specific `slug_id` from the registry — no invented components.
  Prose, not bullets. This is where a reader who never watched the walk gets the whole picture.
- **User Stories** — numbered *"As a {actor}, I want {capability}, so that {benefit}"* items derived
  from end-users + solution-flow decisions. One per meaningful capability.

Then the standard sections: Overview · Scope · Decisions Made · Pre-Resolved · Constraints &
Compliance · Open Questions · Constraint Audit

**Confirm BEFORE writing anything.** Present the **Problem Statement + Solution + User Stories** as
direct markdown, then `AskUserQuestion`: "Does this capture the problem and the stories correctly?" →
["Yes — header approved", "No — let me refine"].
- **Refine** → amend from the user's feedback (re-derive stories if the problem statement shifts) and
  re-present. Loop until confirmed.
- **Yes** → persist the confirmed header to a fragment file; **do NOT yet author `discuss-prd.md`**.
  Rendering waits until after Step 10a passes so the projected sections include the constraint audit.

```bash
# Persist confirmed header (no PRD render yet)
cat > "$DISCUSS_DIR/prd-header.md" <<EOF
## Problem Statement
...
## Solution
...
## User Stories
1. ...
EOF
```

Full render happens at Step 10b, after the gate returns PASS — Claude authors the PRD directly from
the registry + confirmed header, using `$FRAMEWORK_DIR/_resources/_templates/discuss/discuss-prd.md` as the
shape (`$FRAMEWORK_DIR=$(aah path)`).

### 10a. Constraint Validation Gate

```bash
aah run core.discuss.validate_constraints            # human-readable
aah run core.discuss.validate_constraints --json     # machine-readable
```

Writes `constraint_audit{passed, ran_at, checks{}, violations[], flags[], warnings[]}` back into
the registry. Exits 0 on PASS, 2 on FAIL (drives the re-float loop below).

**Checks:**
- **A** Mandatory coverage — every mandatory `slug_id` (given `applies_when`) has a non-empty response (`source ∈ {user, inferred}` both count)
- **B** Forbidden options never presented (or chosen)
- **C** Inline-options whitelist adherence
- **D** Service scope (approved / forbidden)
- **E** Mandated-option enforcement — reads active mandate files (`references[]` chosen + `always_references[]` applicable) and their `closes_options[].eliminates[]`
- **F** Elimination provenance — every eliminated option in the registry cites a `constraint_id` / `mandate_id` (WARN only)

**Blocking vs flagging:**
- **Blocking violation** — a chosen response is in an active `eliminates[]` set. Fails the gate; drives §10a re-float. Also fires for a chosen response outside a Check-C whitelist.
- **Flag** — an option was *presented* but not chosen, and it appears in an active `eliminates[]` set. Gate still passes; recorded in `flags[]` for reviewer visibility.
Both are deterministic set-membership — no LLM judgement.

**On PASS** → Step 10b.

**On FAIL → SURGICAL RE-FLOAT LOOP** (the canonical reopen procedure — §8 back-edges and §11 Revise
both reuse it):
1. CALL OUT the failure as direct markdown (not collapsed):
   ```
   ### ⚠️ ═══ Constraint Validation FAILED ═══
   | slug_id | decision | violation | constraint |
   |---------|----------|-----------|------------|
   | ...     | ...      | ...       | ...        |
   ```
2. Reopen **ONLY the affected `slug_id`s** via `AskUserQuestion` — present **only compliant options**.
   If no compliant option remains → escalate: state the conflict, ask the user to relax the constraint
   or change approach.
3. Overwrite each reopened decision; preserve the prior answer in `revised_from`.
4. Re-run 10a. Loop until PASS or a documented user-accepted exception. Cap at a
   small N iterations; if still failing, block and STOP.

### 10b. Author the PRD (post-gate)

With the constraint audit written to the registry, author `discuss-prd.md` yourself — no Python
renderer. Inputs:

```bash
# 1. Read the template (the shape) — ships with the harness, resolve its root
FRAMEWORK_DIR=$(aah path)
cat "$FRAMEWORK_DIR/_resources/_templates/discuss/discuss-prd.md"

# 2. Read the confirmed header (copy verbatim)
cat "$DISCUSS_DIR/prd-header.md"

# 3. Read the registry (source of truth for every data section)
aah run core.discuss.registry read
```

**Authoring rules:**
- Fill every `{placeholder}` from the template — do NOT leave placeholders or template HTML comments
  in the final file.
- Copy the confirmed header verbatim from `prd-header.md`. Never re-derive Problem Statement / Solution
  / User Stories at this step.
- Data sections (Overview, Decisions Made, Pre-Resolved, Constraints, Constraint Audit) project the
  registry as-is. **No invented facts** — every row must exist in the registry.

- **Overview `{domains}` / `{archetypes}`** come from the registry's `domain_ids[]` / `archetypes[]`
  (set at Step 5.5 via `set-domains`). Render `domain_ids` as human breadcrumbs (e.g. `Financial Services › Commercial
  Banking › Commercial Client Onboarding`) and `archetypes` as their keys/names. If either list is
  empty, render `_None selected._`. (The `business-domain` / `technical-archetype` decisions also
  appear in the Decisions Made table like any other decision.)
- Emergent decisions get a trailing ⚡ on the `slug_id`. Revisions get a "Revisions (surgical re-float)"
  subsection listing each prior response + revision reason.
- Write the final file to `$DISCUSS_DIR/discuss-prd.md`.

If a subsequent revision changes any decision or the gate re-runs, re-author Step 10b so the PRD
stays consistent with the registry.

### 11. Full-PRD Approval Gate

Present the **entire authored PRD** (path: `$DISCUSS_DIR/discuss-prd.md`) — every section, not just
the header. Then `AskUserQuestion`: "Constraints passed. Does the full PRD (all sections) capture the
project correctly?" → ["Approve", "Revise"].
- **Revise** → ask which `slug_id`s to reopen, then run the surgical re-float loop (§10a) on those
  slugs. After revisions land, re-run Step 10b to re-author the PRD, then loop back to Step 11.
  Never reopen the whole flow.
- **Approve** → Step 12.

---

## ═══ CLOSE · harness bookend ═══

### 12. Structural Gate & Close

```bash
aah run core.gates.validate_research_gate
```
(The PRD at `.aah/discuss/discuss-prd.md` satisfies the artifact check.)

#### 12a. Port completeness gate — MANDATORY (fail if any port was missed)

First reconcile (safety net: fired ports whose `produces` exist → completed, tagged
`reconciled`), then verify nothing was missed:

```bash
aah run core.ports.executor --project-path "$PROJECT_DIR" reconcile --node discuss
aah run core.gates.validate_port_artifacts --node discuss --project-path "$PROJECT_DIR"
```

- **Exit 0** → all fired ports completed and produced their artifacts. Proceed to close.
- **Non-zero** → STOP. Report which port + anchor was missed; the discuss phase is
  **incomplete**. Do NOT fire it late or commit/tag — re-run so it fires at its
  designed point.

```bash
aah run core.common.phase_transition end discuss
cd "$PROJECT_DIR"
git add .aah/discuss/ .aah/manifest.yaml knowledge/
git commit -m "feat(aah-discuss): complete discovery phase — slug registry + PRD"
git push origin HEAD
```

**Tag the phase** (namespaced by project AND iteration — so a prior iteration's tag never triggers the
Step 0.4 feedback gate):
```bash
MANIFEST_JSON=$(aah run core.common.manifest read)
PROJECT_NAME=$(echo "$MANIFEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
ITER=$(echo "$MANIFEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('current_iteration', 1))")
git -C "$PROJECT_DIR" tag -a "${PROJECT_NAME}/iter-${ITER}/aah-discuss" -m "aah-discuss discovery phase complete (iteration
${ITER})"
git push --tags origin
```

---

## ═══ ACTIVATIONS ═══

### 13. Resolve Downstream Activations (ports-driven)

```bash
aah run core.discuss.activations           # human-readable
aah run core.discuss.activations --json    # machine-readable
```

This is **ports-driven** — there is no hardcoded list of downstream skills. The
command runs `ports.executor compile`, which reads each decision's `activates[]`
(catalog activity ids such as `ACT-UX`, `ACT-ACCESS`), joins them against the
ports catalog (`aah/_resources/_references/_ports/_registry.yaml`), and materializes the
ordered plan into `.aah/port-registry.yaml`. It reports:
- **Activated ports** — the `fired` ids, each annotated with the `slug_id`(s) that named it.
- **Not activated** — available ports whose activating answer wasn't given.

Present the activated ports as a table (only at step 14 - Summary) with columns `port`, `node`, `position`.
Build **one row per fired port** from the `--json` output — each fired activity
carries its `id` (→ port), `target` (→ node), `position`, and `anchor` (append
`(<anchor>)` to a `within` position). Do NOT hard-code which ports appear; render
exactly those `compile` reported as fired, whatever they are:

```
Activations (fire at their bound phase — not now):

| Port    | Node        | Position          |
|---------|-------------|-------------------|
| <id>    | /aah-<target> | <position>[ (<anchor>)] |
| ...     | ...         | ...               |
```

If no ports fired, state `_No downstream activities were triggered._` and skip the table.

### 14. Summary

Present:
```
### ═══ 🚀 /aah-discuss Complete ═══

**Registry:** {N} decisions across {M} gray areas | {K} pre-resolved (brownfield) | constraint gate: PASS

**📂 Artifacts for this phase live in `.aah/discuss/`:**

| Artifact | Path |
|----------|------|
| Slug decision registry | `.aah/discuss/decision-registry.yaml` |
| Discuss PRD | `.aah/discuss/discuss-prd.md` |
| Structured intent | `.aah/discuss/project-intent.yaml` |
| Confirmed PRD header | `.aah/discuss/prd-header.md` |

List any additional files present in `$DISCUSS_DIR` (e.g. artifacts written by `before`/`within`
ports) as extra rows — enumerate the directory rather than hard-coding this list, so port outputs
are never invisible to the user. Name the directory in the prose too, not just the table.

**Activations (fire at their bound phase — not now):**
{the port/node/position table from Step 13, or "None triggered."}

**➡️ Next phase: Architecture — run `/aah-arch` to begin.**
💡 Tip: run `/clear` first to reset context before `/aah-arch` — it improves performance on the next phase.
───
```

Keep the two distinct: the activations table is what auto-fires later; the **next
phase** is the single thing the user runs. Never present ports as "skills to run next."
