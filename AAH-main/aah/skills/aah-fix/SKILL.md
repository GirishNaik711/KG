---
name: aah-fix
argument-hint: "[bug description or feedback]"
description: Single entry point for ALL feedback. Analyzes feedback, decides which phase to invoke next, relays context through phases, and executes a single cumulative build at the end.
user-invocable: true
disable-model-invocation: false
---

# AAH Fix — Feedback Router & Decision Loop

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text. Call `AskUserQuestion` with properly structured `questions`.

---

## Ports — this is the `fix` spine node

The `port-executor-agent` only **fetches** an ordered plan — it never runs anything.
**This skill's main loop runs each port** (so port-skills can use `AskUserQuestion`).
The plan lists ports to run now (`fire`), within-ports to run later at their anchors
(`within_plan`), and ports to only report (`hand_back`, `blocked`).

- **START** (Step 1b) fetches the `before` ports + the `within_plan`.
- **END** + completeness gate (Step 7b) fetch the `after` ports and verify nothing was missed.

Render each plan as direct markdown (Bash output is collapsed); an empty plan means no
port is bound — continue with zero overhead.

### MANDATORY — how to run a port

A port moves `pending → in-progress → completed`. It is `pending` in the registry
already; you drive the other two transitions, each stamped with its timestamp:

**Announce first (info only — no choice, no `AskUserQuestion`):** immediately before
running each port, tell the user it is auto-triggering, e.g.
> ℹ️ Auto-triggering port **`<id>`** (`<ref>`) — runs automatically as part of this phase; no action needed.

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
1. As you reach each point (e.g. per feedback item in the loop), check whether it
   matches an un-run entry's `anchor_location`.
2. When it matches, run it there **and only there** (invoke + record, per above). Never
   earlier, later, or batched — wrong-point running is a **failure**, since its output
   must be available to the steps designed to consume it.
3. Never skip or defer. Any within-port still un-run at the close = phase FAILED
   (the Step 7b gate catches it). Do not "catch up" by running it late.

## Steps

### 0. Resolve Active Project

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```
Use `$AAH_DIR` instead of `.aah/` and `$PROJECT_DIR` instead of `.` for all paths.

### 1. Pre-Flight Check

```bash
aah run core.common.manifest read
```

- If phase is before `build` → tell user: "aah-fix is available from the build phase onwards." and **STOP**.
- Otherwise → proceed.

This abort gate runs BEFORE the START ports bookend on purpose: a `before` port
must never fire on a run that immediately stops (matching aah-build's pre-flight
hard gate and aah-plan's input validation, both of which precede their START bookend).

### 1b. START ports bookend — `fix / before` (+ plan `within`)

Fetch the plan (blocking — its `fire` ports are inputs the next steps consume):

```
Agent(port-executor-agent, { node: "fix", position: "before" }, run_in_background: false)
```

Run each `fire` port per "how to run a port". **Retain `within_plan`** — do NOT run
those here; each runs later at the exact point its `anchor_location` describes.

### 2. Feedback Capture

#### 2a. Receive Feedback

aah-fix receives feedback from any source:
- the user typed `/aah-fix <text>` or typed feedback directly in the terminal;
- build handed off a description; **or**
- a **post-wave failure** was routed here by the orchestrator: `fix_regression`,
  `fix_standards`, `raise_feedback`, or `fix_runtime_validation`. In these cases the
  "feedback" is the failure summary — the failed checks and where they manifest.

**If the orchestrator payload is `fix_regression`, `fix_standards`, `fix_runtime_validation`,
or `raise_feedback`, triage the failure BEFORE entering the decision loop:**

Read the failure details from `source_artifact`. Use your judgement to determine
the true root cause — do not rely on `fix_category` from the orchestrator payload,
it is a hint only and may be wrong (e.g. a missing dependency installation can be
misclassified as `design_issue`).

**CRITICAL — Direct repair is a narrow exception, not the default.**
If there is ANY doubt about scope, blast radius, spec changes, or interface
changes — you MUST proceed to Step 2b. Do NOT choose the direct repair path
to avoid the decision loop.

If — and only if — you are fully confident the fix is entirely contained to
the integration branch with no spec changes, no interface changes, and no
blast radius:
1. Require `integration/wave-<N>` to match `source_integration_sha`; if it does not, stop and return to `next-action`.
2. Repair only what the failure requires.
3. Run targeted tests, commit the repair.
4. Delete stale evidence only where its staleness is not self-detecting — this is a
   narrow convenience, not a blanket post-repair step. Subject-bound evidence goes
   stale on its own, because your repair commit moves the subject identity.
   - `fix_regression` → `.aah/build/test-results/regression-latest.json`
     (re-dispatches `aah-regression-tester`). **Last wave only** — on any earlier
     wave the file does not exist; that is not an error.
   - `fix_standards` → the payload's `source_artifact`
     (`.aah/build/quality-results/wave-<N>-project-standards.json`)
   - `fix_runtime_validation` / `raise_feedback` → **delete nothing.**
     `verify._check_runtime_bindings` already fails the runtime artifact as
     `runtime_stale_subject`, so `next-action` returns `run_system_checkpoint`.
5. Return to `next-action`. Do not invoke the runtime validator yourself; the changed SHA requires a fresh system checkpoint.

When in doubt — proceed to Step 2b.

**For `fix_regression`, `fix_standards`, `fix_runtime_validation`, and `raise_feedback`:** Use the direct repair path above. Do NOT dispatch `aah-feature-implementer` — these are integration branch issues, not feature-level code fixes. Repair them yourself within this skill.

**`fix_standards` specifics.** The payload's `failures` holds verbatim linter and
static-analysis output (`file:line: CODE message`, with `[*]` marking auto-fixable
findings) — read it directly; there is no structured schema to parse.

On the lean sequential build the end-of-build standards gate routes its failure here as
`fix_standards`, with evidence at `.aah/build/quality-results/standards-latest.json`.

- **Lint** is the textbook qualifying case for direct repair: a whole-codebase
  `ruff check --fix` involves no spec change, no interface change, and no blast
  radius. Deleting the standards evidence in step 4 is not optional bookkeeping —
  the repair commit moves the subject identity, so the evidence goes stale
  regardless; deleting it makes the gate re-run cleanly instead of reporting a
  confusing staleness failure. The re-run must converge in **at most one**
  iteration; staleness caused by your own repair commit is expected, not a new
  failure.
- **A static-analysis (bandit) finding may legitimately fail the "no blast radius"
  test** — it can be a real security defect needing a design change. Per the
  CRITICAL note above, if there is ANY doubt you MUST proceed to Step 2b. That
  escalation is intended behaviour, not this route failing.

This route does **not** consume `MAX_REWORK_ATTEMPTS` — that counter belongs to the
rework engine, which the direct-repair path bypasses.

**aah-fix NEVER dispatches `aah-feature-implementer` directly.** There are exactly
two routes out of here: direct repair on the integration branch (above), or the
decision loop → plan classification → rework engine (below). A feature-level
rebuild always goes through a rework entry.

#### 2b. Align Before Routing

Alignment comes before action — the most common failure is fixing the wrong thing. Before routing any part of the feedback, make sure you can answer for it: **what** needs to change, **where** it manifests, and **what "done" looks like**.

- If all three are unambiguous — proceed.
- If any branch is open (unclear scope, more than one plausible interpretation, undefined expected behavior), resolve it with targeted `AskUserQuestion` calls first. Ask only the open branches — don't force the user through a fixed questionnaire, and don't re-ask what the feedback already answers.
- Do not route a phase until the decision tree for the part you're routing is resolved. Design-level ambiguity is the exception: route it to discuss to resolve rather than guessing here.

#### 2c. Read Current State

```bash
aah run core.common.progress read
```

Extract `current_wave`. Read waves:
```bash
cat "$AAH_DIR/plan/waves.json"
```

Determine `feedback_wave`:
- If `current_wave < total_waves` → use `current_wave`
- If `current_wave >= total_waves` (post-completion) → use `total_waves - 1`

#### 2c-i. The Four Cases — How Each Piece of Feedback Routes

`plan` classifies each affected feature; you route it into exactly one of four
cases (the dividing line is `promote`, not wave):

| # | Feature state | Action | Wave |
|---|---|---|---|
| 1 | **Built, current wave** (not promoted) | edit spec, reset `passes:false`, rebuild | current — reset in place |
| 2 | **Built, already promoted** | edit spec, create one distinct `-rework-NN` entry, rebuild | **current** wave |
| 3 | **Unbuilt, future wave** | edit spec in place, leave it | its existing future wave |
| 4 | **Brand-new feature** | add to DAG | earliest DAG-valid future wave |

- **Cases 1 & 2** are reworks — both execute in the **current wave, before
  promote** (a rework's deps are all built, so the current wave is always
  DAG-valid for it).
- **Cases 3 & 4** are normal build work — flow through the DAG as usual.

`plan` owns the classification and mints the `-rework-NN` id for Case 2 (via
`core.plan.create_rework_entry`, which places it in the **current** wave). You
route the classified outcome.

#### 2d. Refresh Codebase Intelligence

```bash
/aah-codebase-profile --mode refresh
```

Wait for completion before proceeding.

### 3. Initialize Master Log

Resolve the next feedback id for this wave (auto-incremented sequence):

```bash
aah run core.build.feedback_capture resolve-id \
    --project-path "$PROJECT_DIR" --wave <feedback_wave>
```

**Follow its output:**
- `in_progress_exists: true` → use `AskUserQuestion`: "An in-progress fix session exists for wave N (`<master_log>`). Continue it or start a new one?" Options: ["Continue existing session", "Start new session"]. If continuing, append to that master log. If starting new, use the returned `feedback_id` for a fresh log.
- `in_progress_exists: false` → create a new master log at `$AAH_DIR/build/feedback/<feedback_id>.yaml`:

If `$ARGUMENTS` contains `originating_issue: <N>`, extract `<N>` and include it in the master log.

```yaml
feedback_id: "UF-06-03"
wave: 6
source: "user"
started_at: "2026-07-14T10:30:00Z"
feedback_details: "Users need SSO support, login has null pointer crash, and MFA is missing from login feature"
originating_issue: 42  # optional — GitHub issue number that triggered this fix, if known
steps: []
status: in_progress
```

You own this file end-to-end — there is no script that writes the feedback body.

### 4. Decision Loop

This is the core of aah-fix. After each phase completes, re-evaluate what to do next based on the feedback and any phase outputs so far.

#### 4a. Decide Next Action

Read the feedback and any previous step outputs. Think about:
- What do you understand clearly from the feedback?
- What's still ambiguous or missing?

With that understanding, decide which phase to invoke next. Route the feedback to the phase that can act on it. If part of the feedback is unclear, start with discuss to resolve it, then combine what you learn with the rest and continue.

You decide which phase(s) are needed — nothing here dictates the flow.

At each decision point, consider the full picture — original feedback + all phase outputs so far. Never invoke the same phase twice in sequence unless the first invocation's output revealed something that requires a second pass.

The goal: every phase invocation handles everything that's ready for it at that point. Build is invoked once at the end for all accumulated work.

#### 4b. Invoke Phase

Always append the step to the master log with its `input` first — the master log stays the single source of truth. But pass the **consolidated feedback input as a prompt** to the phase skill, not the YAML path. The prompt is the same `input` you just wrote to the step: the consolidated feedback plus what the phase specifically needs to do. Always end the prompt with the instruction to write the output back:

> After completing your work, write your `output` + `completed_at` to your step (`phase: <name>` with `completed_at: null`) in `$AAH_DIR/build/feedback/<feedback_id>.yaml`.

**If discuss or architecture:**

Append step to master log with `input`, then invoke:
```
Skill("<phase-skill>", args: "<consolidated feedback input>. After completing your work, write your output + completed_at to your step in $AAH_DIR/build/feedback/<feedback_id>.yaml")
```

**If plan:**

Determine `affected_modules` from the feedback context (which modules/directories are touched), then pass them on the command line. The script is the authoritative writer — it stamps both `affected_modules` and `blast_radius_features` into the plan step, so there is no need to write the step first:

```bash
aah run core.build.feedback_capture compute-blast-radius \
    --project-path "$PROJECT_DIR" \
    --file "$AAH_DIR/build/feedback/<feedback_id>.yaml" \
    --affected-modules "<comma-separated module names you determined, e.g. auth,session>"
```

This finds features whose `file_scope`/`module_ref` overlap those modules, traverses the DAG, and writes `affected_modules` + `blast_radius_features` into the plan step.

Append the plan step's `input` (if not already present) alongside the fields the script wrote, then invoke:
```
Skill("aah-plan", args: "<consolidated feedback input including blast_radius_features>. After completing your work, write your output + affected_features + completed_at to your step in $AAH_DIR/build/feedback/<feedback_id>.yaml")
```

Plan writes **`affected_features`** to its step — the IDs to build, per §2c-i. One non-obvious rule: for a Case 2 rework it lists the `<F>-rework-NN` id (from `core.plan.create_rework_entry`), NOT `<F>` — which stays `passes:true`.

This includes bug fixes too — if the feedback contains bugs alongside other work, plan accounts for them in `affected_features` so everything gets built together.

Downstream breakage is handled **reactively** — the current wave's regression/runtime gates catch collateral damage and route it back through aah-fix. Plan only pre-reworks what the feedback directly changed.

Routing every change through plan and blast radius is deliberate: it keeps fixes from becoming scattered point-patches that erode the architecture. When feedback touches a module's shape, prefer a clean structural change (a well-scoped feature) over a localized hack — the `input` should say so.

#### 4c. Read Output & Re-evaluate

After each phase returns, read the master log back. The phase will have written `output` + `completed_at` to its step.

Using the output, decide: what's the next action? Repeat from 4a.

**Construction of next step's input:**
- Include what the previous phase decided or changed (from its `output`)
- State what the next phase specifically needs to do with that information
- If multiple concerns from the feedback converge at this phase, combine them into one input
- Use the project's own vocabulary — the exact module, feature, and component names from the refreshed codebase intel and `module-map.yaml`. Don't invent synonyms for things that already have names. Shared naming keeps the input short and keeps every phase referring to the same entities.

The input must be specific and actionable. The phase will read actual project artifacts for full detail, but the input tells it what changed and what action to take.

#### 4d. Exit Condition — Build

The loop ends when all concerns from the feedback have been routed through the necessary phases and plan has produced `affected_features`. Everything — including bug fixes — goes through plan so they're part of a single cumulative build.

Read `affected_features` from plan's step, then execute the following steps **in order — do NOT skip or reorder**:

#### Step 1 — Post feedback comments (MANDATORY GATE — do NOT proceed to Step 2 without completing this)

For each ID in `affected_features`:
- Strip `-rework-NN` suffix if present to get the original feature ID
- Look up the issue number from the sync ledger for that original feature ID
- If found and version_control enabled, post:
  ```bash
  aah run core.version_control.cli comment --issue <N> --body "**Fix In Progress — <feedback_id>**

  <feedback_details verbatim from master log>"
  ```
  If version_control is disabled or offline, skip silently.
- If the feature has no issue number yet (newly created, not yet synced), skip silently — the comment will go out during post-wave sync.

**You MUST complete this step for every feature in `affected_features` before moving to Step 2. Do NOT invoke `rework trigger` or `Skill("aah-build")` until all comments are posted or explicitly skipped.**

#### Step 2 — Trigger build

```bash
aah run core.build.rework trigger \
    --project-path "$PROJECT_DIR" --feedback-id <feedback_id> \
    --affected-features "<affected_features from plan step>"
```
```
Skill("aah-build")
```

Build doesn't need the YAML — it gets `affected_features` from `rework trigger` and the orchestrator handles the rest. Build is invoked exactly once for the entire feedback session. Any built-feature defect the wave's verify step catches loops back here (rebuild → re-verify) under the rework retry cap; after the cap the orchestrator escalates to a human.

#### Step 3 — Post resolution comments (MANDATORY GATE — do NOT proceed to Step 5 without completing this)

For each ID in `affected_features`:
- Strip `-rework-NN` suffix if present to get the original feature ID
- Look up the issue number from the sync ledger
- If found and version_control enabled, post:
  ```bash
  aah run core.version_control.cli comment --issue <N> --body "**Fix Completed — <feedback_id>**

  <plan step output from master log for this feature>"
  ```
  If version_control is disabled or offline, skip silently.
- If the feature has no issue number yet, skip silently.

**You MUST complete this step for every feature in `affected_features` before moving to Step 5. Do NOT finalize or proceed to Step 8 until all comments are posted or explicitly skipped.**

### 5. Finalize — Set the Terminal Status

Set the master log `status` to its terminal value — `completed`, `failed`,
`escalated_to_human` (retry cap hit), or `routed_to_discuss`. Always set one:
it's the outward signal, so a run left without a terminal `status` leaves the
issue hanging forever.

### 6. Error Handling

If any phase fails:

1. aah-fix stops immediately
2. Updates master log: `status: "failed"` with error details (or
   `escalated_to_human` when the orchestrator returned `human_review_required`
   with `trigger: rework_cap`).
3. Use `AskUserQuestion`: "Phase {X} failed: {error_summary}" with options:
   - "Retry phase"
   - "Abort"

**If Retry** → re-invoke the failed phase with same input.
**If Abort** → leave current state, `status: "aborted"`.

### 7. END ports bookend — `fix / after`

After the fix artifacts exist (they are the inputs `after` ports consume), fetch the
`after` plan, then run each `fire` port per "how to run a port", BEFORE committing so
their artifacts land in the same commit.

```
Agent(port-executor-agent, { node: "fix", position: "after" }, run_in_background: false)
```

Include any artifacts the `after` ports produced in the Step 9 commit.

### 7b. Port completeness gate — MANDATORY (fail if any port was missed)

First reconcile (safety net: fired ports whose `produces` exist → completed, tagged
`reconciled`), then verify nothing was missed:

```bash
aah run core.ports.executor --project-path "$PROJECT_DIR" reconcile --node fix
aah run core.gates.validate_port_artifacts --node fix --project-path "$PROJECT_DIR"
```

- **Exit 0** → all fired ports completed and produced their artifacts. Proceed to Step 8.
- **Non-zero** → STOP. Report which port + anchor was missed; the fix flow is
  **incomplete**. Do NOT fire it late or commit — re-run so it fires at its
  designed point.

### 8. Post-Rework Follow-Up

After build finishes successfully:

```bash
aah run core.build.rework status --project-path "$PROJECT_DIR"
```

If `active_reworks: 0`, use `AskUserQuestion`:
- "All changes from this fix session are complete. Has the issue been resolved?"
- Options: ["Yes, issue resolved", "No, still have issues"]

**If "Yes"** → Resolution comment already posted (Post 2 above). Then read `originating_issue` from the master log — if present and version_control enabled and build commit completed successfully, post a close-reason comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "<close reason>"
gh issue close <N>
```
Where `<N>` is `originating_issue` from the master log. If `originating_issue` is absent, version_control is disabled or offline, or the build commit failed, skip silently. Then STOP.
**If "No"** → Ask user to describe remaining problem (free-text via `AskUserQuestion`). Then re-invoke this skill with the new description: `Skill("aah-fix")` with the new feedback as args.

### 9. Commit State

```bash
cd "$PROJECT_DIR"
git add .aah/build/feedback/
git commit -m "fix(aah): feedback resolved — <feedback_id>"
```

---

## Master Log

One file per feedback session: `$AAH_DIR/build/feedback/<feedback_id>.yaml`

This is the **single source of truth**. aah-fix creates it, writes each step's `input` before invoking phases, `compute-blast-radius` writes to it, and phases write `output` + `completed_at`.

### Example (completed)

```yaml
feedback_id: "UF-06-03"
wave: 6
source: "user"
started_at: "2026-07-14T10:30:00Z"
feedback_details: "Users need SSO support, login has null pointer crash, and MFA is missing from login feature"
steps:
  - phase: discuss
    input: "SSO support not mentioned in project scope — need to clarify if in scope and define requirements"
    output: "Added SSO requirement to discuss/discuss-prd.md with OAuth2 provider integration"
    completed_at: "2026-07-14T10:50:00Z"
  - phase: architecture
    input: "SSO requirement confirmed and scoped. Design module structure for OAuth2 SSO integration."
    output: "Added SSO module to module-map, defined OAuth2 callback flow and token exchange points"
    completed_at: "2026-07-14T10:55:00Z"
  - phase: plan
    input: "SSO module designed. MFA missing from login feature. Also null pointer bug in auth/middleware.py. Create SSO feature, add MFA criteria, and account for bug fix in F003."
    affected_modules: ["auth", "session"]
    blast_radius_features: ["F003", "F008"]
    output: "Created F010 for SSO, added MFA acceptance criteria to F003 (rework, promoted), confirmed F003 covers null pointer fix"
    affected_features: ["F003-rework-01", "F008", "F010"]
    completed_at: "2026-07-14T11:00:00Z"
status: completed          # terminal: completed | failed | escalated_to_human | routed_to_discuss
```

---

## How Phases Interact with the Master Log

aah-fix passes the **consolidated feedback input as the prompt** (args), plus an instruction to write output back to the master log:
```
Skill("aah-discuss", args: "<consolidated feedback input>. After completing your work, write your output + completed_at to your step in $AAH_DIR/build/feedback/<feedback_id>.yaml")
```

Each phase:
1. Reads its `input` directly from the prompt (args)
2. Does its work (updates artifacts, makes decisions)
3. Finds its step (`phase: <name>` with `completed_at: null`) in the master log YAML
4. Writes `output` + `completed_at` back to that step

aah-fix reads the file back after `Skill()` returns, uses the output to decide and construct the next step.
