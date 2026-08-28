---
name: aah-arch
argument-hint: "[feedback for rework, e.g. 'split the storage module' | 'regenerate the agent topology doc']"
description: Architecture via vertical-slice drafting — tracer-bullet slices, iterate with user, produce design docs. Use when the user wants to go straight to module breakdown through slice iteration.
user-invocable: true
disable-model-invocation: false
---

# /aah-arch — Vertical-Slice Architecture

All user questions MUST use the `AskUserQuestion` tool. Bash output is collapsed — always re-render results as direct markdown.

## Ports — this is the `architecture` spine node

The `port-executor-agent` only **fetches** an ordered plan — it never runs anything.
**This skill's main loop runs each port** (so port-skills can use `AskUserQuestion`).
The plan lists ports to run now (`fire`), within-ports to run later at their anchors
(`within_plan`), and ports to only report (`hand_back`, `blocked`).

- **START** (Step 0) fetches the `before` ports + the `within_plan`.
- **END** + completeness gate (Step 7b) fetch the `after` ports and verify nothing was missed.

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
   (the Step 7b gate catches it). Do not "catch up" by running it late.

## Process

### 0. START ports bookend — `architecture / before` (+ plan `within`)

Fetch the plan (blocking — the ports it lists produce inputs the next steps need):

```
Agent(port-executor-agent, { node: "architecture", position: "before" }, run_in_background: false)
```

Run each `fire` port per "how to run a port"; those artifacts are now inputs.
**Retain `within_plan`** — do NOT run those here; each runs later at the exact point its `anchor_location` describes.

### 1. Load inputs

Resolve the active project, then read its inputs:

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
ARCH_DIR="$AAH_DIR/architecture"
DISCUSS_DIR="$AAH_DIR/discuss"
mkdir -p "$ARCH_DIR"
```

Read:
- `$DISCUSS_DIR/decision-registry.yaml`
- `$AAH_DIR/discuss/discuss-prd.md`
- `$AAH_DIR/discuss/project-intent.yaml`

Per the slugs `cloud-vs-local` and `data-storage-present` in `decision-registry.yaml` above: if
`cloud-vs-local == cloud`, read `$ARCH_DIR/cloud-readiness.yaml`. If also `data-storage-present == yes`,
run `aah run core.gates.validate_data_readiness --project-path "$PROJECT_DIR" --extract-services`; if
`data_services > 0`, read `$ARCH_DIR/data-readiness.yaml` and `$ARCH_DIR/data-schema-snapshot.yaml`.
**MANDATORY when required — do not skip.** Hold them in-context alongside the knowledge base while
carving modules (Step 4) and generating docs (Step 5). If a required file is missing, halt with a clear
error naming the file and the slug that requires it — do not proceed to Step 4.

**Hard rule: AAH does not provision infrastructure.** Every service listed in `cloud-readiness.yaml` was already created by the user and validated reachable during `/aah-access` — architecture's job is to design against its real coordinates (endpoint, bucket name, ARN, etc.), never to plan how to create, provision, or stand it up. Do not propose Terraform/IaC, "we will create an S3 bucket", or any provisioning step for a resource that already appears in `cloud-readiness.yaml`. If a module needs a cloud service NOT present there, flag it to the user via `AskUserQuestion` rather than assuming AAH will provision it later.

**Also pull in the project knowledge base** — the team's own docs (PDFs, DOCX, PPTX, notes) that
seeded discuss and may carry architecture-relevant detail discuss didn't fully distill:

```bash
aah run core.knowledge.main context --project-path "$PROJECT_DIR"
```

Read the returned `## Project Knowledge Base` block and hold it in-context while carving modules
(Step 4) and generating docs (Step 5) — use it to ground design decisions and to fill template
sections that the registry/PRD leave underspecified. Empty output → no knowledge docs; continue.

Extract `delivery_intent` (poc/mvp/prod), `project_type`, frontend scope, and agentic scope (all inferrable from these inputs). Stop with clear error if any file is missing.

### 1.5. Issue Brief — Look Before You Work — MANDATORY, ALWAYS RUNS

**Always run this step — do NOT skip even if prior steps errored.**

Invoke the `aah-issue-reporter` agent with: "Brief me on **architecture** issues."
Read the returned brief; decide and act with your phase context (absorb feedback →
re-entry, note feature status (do not close feature issues), or surface untriaged issues). For every issue reviewed,
post a GitHub comment explaining the decision made — whether actioned, deferred to a future
iteration, or out of scope for the current run. Do NOT close unless the issue is fully resolved.

**Detect prior run:** Check if `$ARCH_DIR/module-map.yaml` exists. If it does NOT, proceed with a
fresh full run from Step 2. If it DOES, this is a **feedback/rework invocation** — handle it as follows.

**Feedback/rework handling.** Two paths, depending on whether the user passed feedback inline:

- **Inline feedback (`$ARGUMENTS` present).** The user passed rework feedback directly, e.g.
  `/aah-arch regenerate the agent topology doc`, `/aah-arch split the storage module into two`,
  or `/aah-arch regenerate all docs`. Do NOT ask from a blank slate — instead:
  1. Read the existing artifacts: `$ARCH_DIR/module-map.yaml` and the generated docs already in
     `$ARCH_DIR/`.
  2. **Classify the feedback into a target:**
     - **Module change** — the feedback is about the module breakdown (split/merge/rename/re-scope a
       module, change a dependency). Examples: "split the storage module", "merge MOD-002 and MOD-003".
       → Target = **Step 4** (edit `module-map.yaml`). Before editing, read the module-map template at
       !`echo "$(aah path)/_resources/_templates/architecture/module-map.yaml"` and conform the edited entries to its
       structure (required fields, field order) — never introduce fields the template doesn't define
       or drop fields it requires.
     - **Specific doc(s)** — the feedback names one or more design docs to regenerate. Map the phrasing
       to the template names: `architecture-overview`, `data-model`, `application-flow`,
       `api-interface-contract`, `agent-topology`, `risk-security-compliance`. Examples: "regenerate
       the agent topology doc" → `agent-topology`; "redo the data model" → `data-model`.
       → Target = **Step 5**, scoped to ONLY the named doc(s).
     - **All docs** — "regenerate all docs", "redo everything". → Target = **Step 5** for the full set.
  3. **Confirm the mapping with ONE `AskUserQuestion`**: state the target you matched and what will
     change ("I'll regenerate `agent-topology.md` — correct?" / "I'll update the module map to split
     the storage module — correct?"). If the feedback is ambiguous or matches nothing confidently,
     present the candidate targets (modules / the doc list / all) and let the user pick.
  4. Once confirmed, jump directly to the target step (Step 4 for modules, Step 5 for docs) and apply
     ONLY that change. A module change that alters the module set should also offer to regenerate the
     docs that depend on it. Do NOT re-run the full flow from Step 2.

- **No `$ARGUMENTS`.** Ask the user what to regenerate (modules, a specific doc, or all docs) via
  `AskUserQuestion`, then jump to the relevant step (Step 4 for modules, Step 5 for docs).

### 2. Draft vertical slices

Break PRD + decision registry into tracer-bullet epics. Each epic contains ONLY these fields: **title**, **description**, **blocked_by**, **user_stories_covered**. No other fields. 

Each epic delivers a logical & complete path through every layer (schema → API → UI). A completed epic is demoable or verifiable on its own. Fewer epics ensure faster delivery, so bias towards fewer epics. Epics that cannot demo independently collapse into MOD-000. Dependency = blocked_by relationship.

These epics will be broken down into features/github issues in `/aah-plan` after the architecture phase.

### 3. Quiz the user

Present slices as a numbered list. Use `AskUserQuestion`:
1. Does the granularity feel right? (too coarse / too fine)
2. Are the dependency relationships correct?
3. Should any slices be merged or split further?

Iterate until approved.

### 4. Carve modules from slices

Read the module-map template at !`echo "$(aah path)/_resources/_templates/architecture/module-map.yaml"` first, and
conform the output to its structure (required fields, field order) per Reference > Module map rules.

Map approved slices → module-map.yaml:
- Slices that can't demo independently → MOD-000 (walking skeleton)
- Each approved demoable slice → MOD-001..N

**If `cloud-readiness.yaml` was read in Step 1**, tag each module with the validated service(s) it integrates against: add a `cloud_services: [<service id>, ...]` list (service `id`s from `cloud-readiness.yaml`, e.g. `svc-001`) to every module entry that touches a cloud/data service. Leave the list empty for modules with no cloud dependency. This is what lets later phases and the review board trace "this module talks to that real, already-provisioned resource" without re-deriving it from raw readiness data.

Write to `$ARCH_DIR/module-map.yaml`. Present to user. Use `AskUserQuestion` to confirm.

### 5. Generate architecture docs

**5a. Announce the doc list, then confirm before generating.**

Enumerate the templates in `$(aah path)/_resources/_templates/architecture/`, excluding `module-map.yaml` (Step 4), `architecture-review-findings.md` (Step 6), and `wireframes-screen-inventory.md` (out of scope here). Generate ALL of them, at every tier. Skip only what does not apply: `agent-topology` if no agents are in scope, and `api-schema` / `api-interface-contract` if there is no FE↔BE API surface (inferable from `project-intent.yaml` + module-map layers).

Show the exact list — each doc to be generated, and each one skipped with its one-line reason:

```
About to generate these architecture docs:
- architecture-overview
- data-model
- application-flow
- api-schema (per-module, under schema/)
- api-interface-contract
- risk-security-compliance
Skipped: agent-topology — no agents in scope
```

Then ask via `AskUserQuestion`: "Generate these docs?" — options **"Yes — generate"** and **"I want to change the list"**. On the second, take the user's adjustment (add a skipped doc, drop one they don't want), re-show the revised list, and ask again. Do NOT generate until the user confirms.

Docs: `architecture-overview`, `data-model`, `application-flow`, `api-schema` (one OpenAPI 3.1 file **per API-exposing module** under `.aah/architecture/schema/MOD-NNN-api-schema.yaml` — see §5b.i), `api-interface-contract`, `agent-topology`, `risk-security-compliance`.

**5b. Generate — one separate file per doc.** Each doc is ALWAYS written as its own file, named after its template. Never merge docs into a single consolidated file.

**`api-schema` is fan-out.** If applicable, `mkdir -p "$ARCH_DIR/schema"` and write ONE `MOD-NNN-api-schema.yaml` per API-exposing module (from `module-map.yaml`) by copying the template. Filename carries ownership — no `x-aah-module` field. `operationId` must be globally unique across all module files; every wireframe `data dependency` must resolve to an `operationId` in exactly one module file. Types never cross files (each module redeclares shared shapes like `ErrorResponse` locally).

**For each other doc — one file each (excluding `api-schema`, handled above):**

1. Read the template and all input documents:
   - `$DISCUSS_DIR/decision-registry.yaml`
   - `$AAH_DIR/discuss/discuss-prd.md`
   - `$AAH_DIR/discuss/project-intent.yaml`
   - `$ARCH_DIR/module-map.yaml`
   - `$ARCH_DIR/applications_wireframes/` — if present: read `README.md` for the screen inventory/IA/module mapping
   - `$ARCH_DIR/design-spec.yaml` — if present: read each screen's per-region `data` seam (the actual backend call, e.g. `screens[].regions[].data: "GET /api/alerts"`) — that structured per-region detail, not the README summary, is what grounds `application-flow` and `api-interface-contract` FE↔BE seams instead of inventing screens
   - `$ARCH_DIR/cloud-readiness.yaml`, `$ARCH_DIR/data-readiness.yaml`, `$ARCH_DIR/data-schema-snapshot.yaml`, per Step 1's mandatory-read rule — ground `data-model` and any service-integration content in these real, validated coordinates, never invent them or describe provisioning a resource already listed there
2. Review the template sections against available information — reflect if any additional information is required (e.g., unclear ownership, missing tech choice, ambiguous relationship)
3. Use `AskUserQuestion` for anything missing
4. Fill in the doc's **Provenance** section — a **section-wise table** with one row per major section of the doc. For each section, state its **Origin** (`Inherited` = carried forward from upstream inputs — PRD + decisions in `.aah/discuss/`, `discuss-prd.md`, `project-intent.yaml`, `module-map.yaml`; `Authored` = newly created during this architecture phase, including content decided in architecture-phase user Q&A) and name the specific **Source**. If a section mixes carried-forward content with freshly-decided detail, tag it `Inherited + Authored`. Every section of the doc must appear as a row. This section is mandatory in every generated doc.

   **Source-reference format — findable by an outsider (MANDATORY).** 
5. The **Source** column must let a reader who has never seen the project locate the origin. 
6. 
7. NEVER cite a bare decision slug on its own — a bare slug gives no hint which file holds it, 
8. and `decision-registry.yaml` contains dozens of slugs. Always qualify a slug with the file that contains 
9. it, using the form `decision-registry.yaml → <slug>`. When citing a file with a relevant key/section, 
10. name it in parentheses, e.g. `project-intent.yaml (agents)`. Combine multiple sources with commas. Examples:
   - ✅ `project-intent.yaml (agents), decision-registry.yaml → summarization-llm-variant`
   - ✅ `module-map.yaml (MOD-002 storage layer), decision-registry.yaml → chunking-strategy, embedding-model`
   - ❌ `summarization-llm-variant` (bare slug — reader can't find it)
   - ❌ `decision-registry.yaml` (file only — imprecise; loses the slug)
5. Write the doc as its own file to `$ARCH_DIR/` and update `$DISCUSS_DIR/decision-registry.yaml` with new slugs

**5c. Optional HTML.** Ask the user via `AskUserQuestion` whether they also want an HTML version of the generated docs. If yes, generate an HTML rendering for each generated doc alongside its markdown file in `$ARCH_DIR/`. If no, skip.

When generating HTML, style it using the project's design if one is present/detected (a brand or theme provided by the project). **Fallback:** if no design is present/detected, invoke the `aah-deloitte-brand` skill via the Skill tool and apply its brand guidelines (colors, typography, visual identity) to the HTML. Ensure all diagrams in each doc render correctly in the HTML output.

**Diagram rendering — avoid clipped text.** When the HTML loads a custom web font (e.g. Open Sans from a CDN for brand styling), any diagram library that sizes nodes by measuring text (Mermaid, etc.) MUST run only AFTER the font has loaded — otherwise it measures the narrower fallback font, sizes boxes too small, and the real font's text gets clipped. Initialize the diagram library with autostart disabled (`startOnLoad:false`) and trigger rendering from `document.fonts.ready` (falling back to `window.onload` if the Font Loading API is unavailable). Apply the branded palette to the diagram theme as well, so diagrams match the doc styling.

**Markdown → Mermaid wiring — the version-agnostic recipe (MANDATORY).** If you render the markdown to HTML with a converter (e.g. `marked`), do NOT hook the converter's custom code-renderer to detect ```mermaid fences — that API is version-specific and silently breaks on CDN upgrades (marked v4 passes `(code, info)`, v5+ passes a token object), causing the diagram to fall through and display as a **raw code block of Mermaid source**. Instead:
1. Let the converter parse normally — a ```mermaid fence becomes `<pre><code class="language-mermaid">…</code></pre>`.
2. **Post-process the DOM**: query `pre > code.language-mermaid`, and for each, replace the `<pre>` with a `<div class="mermaid">` whose `textContent` is the code element's `textContent` (decoded source).
3. THEN run the diagram library from `document.fonts.ready` as above.
4. **Pin CDN versions** (e.g. `marked@12`, `mermaid@10`) so a future silent CDN bump cannot re-break the wiring.

This post-process approach is required whenever a fence-based markdown converter feeds a measure-by-text diagram library.

**Mermaid fences — delegate to the `aah-mermaid-diagram` skill.** Never author ```mermaid fences inline. For every diagram a doc needs, invoke `Skill(aah-mermaid-diagram, "<type> \"<subject>\" --context <files> --fence-only")` and splice the returned fence into the doc at its section (types: `sequence`, `architecture`, `data-model`, `data-flow`, `state`, `c4-context`, `c4-container`, `workflow`, `class`, `dependency`). `--fence-only` is MANDATORY — the skill emits only the raw fence to stdout. The aah-mermaid-diagram skill owns templates + the 7 syntax rules + pre-write validation; Step 5d below is the final deterministic check.

### 5d. Mermaid validation gate — MANDATORY (auto-fix loop)

Run `aah run core.gates.validate_mermaid --project-path "$PROJECT_DIR"` (scans every `.md` + `.html` under `$ARCH_DIR/` with the structural lint from Step 5b's rules; sub-second, no external dependencies). Exit 0 → proceed to Step 6.

Non-zero → YOU fix each reported `<file> (block #N): line <L>: <reason>` per Step 5b's rules, keeping any `.md`/`.html` pair in sync (regenerate the HTML's `<script type="text/markdown">` block from the fixed `.md`), and re-run. Up to 3 attempts; if still failing, surface remaining issues via `AskUserQuestion`. Do NOT enter Step 6 with a failing gate.

### 6. Review (mvp/prod only)

Dispatch the `aah-arch-review-board` agent synchronously and wait to review results, passing the resolved `$ARCH_DIR`, `$DISCUSS_DIR`, and `$AAH_DIR` paths in the prompt. The board reviews **everything under `$ARCH_DIR/`** — including any artifacts written by earlier `within` ports — so port outputs are vetted alongside the architecture docs with no port-specific handling. For `decision-level` findings, reopen the slug via `AskUserQuestion`; for `design-level` findings, patch the affected doc inline. Max 2 rework cycles, then log remaining findings as accepted tech debt. Skip at poc.

**After resolving findings**, write `$ARCH_DIR/architecture-review-findings.md` using the template at !`echo "$(aah path)/_resources/_templates/architecture/architecture-review-findings.md"`. This document records ALL findings from the review board, their severity, resolution actions taken, rework cycle log, any accepted tech debt, and the final verdict. It is a mandatory output of Step 6 — never skip it.

### 7. END ports bookend — `architecture / after`

The architecture docs now exist — the inputs `after` ports consume. Fetch the `after`
plan, then run each `fire` port per "how to run a port", BEFORE committing so their
artifacts land in the same commit:

```
Agent(port-executor-agent, { node: "architecture", position: "after" }, run_in_background: false)
```

Include any artifacts the `after` ports produced in the Step 8 commit.

### 7b. Port completeness + API-schema gates — MANDATORY (fail if any check fails)

First reconcile (safety net: fired ports whose `produces` exist → completed, tagged
`reconciled`), then verify nothing was missed:

```bash
aah run core.ports.executor --project-path "$PROJECT_DIR" reconcile --node architecture
aah run core.gates.validate_port_artifacts --node architecture --project-path "$PROJECT_DIR"
```

- **Exit 0** → all fired ports completed and produced their artifacts. Proceed to the API-schema check.
- **Non-zero** → STOP. Report which port + anchor was missed; the architecture phase is
  **incomplete**. Do NOT fire it late or commit/tag — re-run so it fires at its
  designed point.

If `api-schema` was generated in Step 5b.i, also run the API-schema validation gate.
It's a no-op if `$ARCH_DIR/schema/` doesn't exist, so it's always safe to call:

```bash
aah run core.gates.validate_api_schema --project-path "$PROJECT_DIR"
```

- **Exit 0** → every module schema file is spec-valid, module ↔ file consistency holds, and every wireframe data-dependency resolves. Proceed to Step 8.
- **Non-zero** → STOP. Each failed check prints on its own line. Fix the schema file(s) in place (or the mismatched wireframe/module-map entry) and re-run the gate.

### 8. Close

Commit and tag:

```bash
cd "$PROJECT_DIR"
# .aah/ covers design docs, port artifacts, and the port registry.
git add .aah/
# Legacy: pick up the discuss slug registry if it still lands under .rapids/.
git add .rapids/ 2>/dev/null || true
git commit -m "feat(aah-arch): architecture phase — module-map + design docs"
git push origin HEAD
MANIFEST_JSON=$(aah run core.common.manifest read)
PROJECT_NAME=$(echo "$MANIFEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
ITER=$(echo "$MANIFEST_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('current_iteration', 1))")
git tag -a "${PROJECT_NAME}/iter-${ITER}/aah-architecture" -m "aah-architecture phase complete (iteration ${ITER})"
git push --tags origin
```

**Comment back to GitHub issue (if version_control enabled).** If this run was triggered by a GitHub
issue surfaced at Step 1.5, run:
```bash
aah run core.version_control.cli status
```
If enabled, and the commit + push above completed successfully, post a resolution comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "Architecture updated based on this issue: <brief summary of what changed — module map / doc regenerated>. Changes committed."
gh issue close <N>
```
Where `<N>` is the issue number from the Step 1.5 brief that triggered this rework. If the feedback
came from `$ARGUMENTS` (not a GitHub issue), skip this step. If the commit or push failed, do NOT
close the issue. If version_control is disabled or offline, skip silently.

Present summary, including where this phase's artifacts live:

```
**📂 Artifacts for this phase live in `.aah/architecture/`:**

| Artifact | Path |
|----------|------|
| Module map | `.aah/architecture/module-map.yaml` |
| {each generated design doc} | `.aah/architecture/{doc-name}.md` |
| Per-module API schemas (if generated) | `.aah/architecture/schema/MOD-NNN-api-schema.yaml` |
| Review findings (mvp/prod) | `.aah/architecture/architecture-review-findings.md` |

```

Build the doc rows from what Step 5 **actually generated** plus anything
written by `before`/`within` ports — enumerate `$ARCH_DIR` rather than hard-coding the doc
list, so a skipped doc is never listed and a port's output is never omitted.
Include the HTML renderings and `applications_wireframes/` when present. Name the directory in the prose too, not just the table.

Then close with the phase-transition guidance:

```
**➡️ Next phase: Plan — run `/aah-plan` to begin.**
💡 Tip: run `/clear` first to reset context before `/aah-plan` — it improves performance on the next phase.
```

## Reference

### Module map rules

- MOD-000 absorbs non-demoable infra (auth, agent loop, data-access, server scaffold)
- MOD-001..N = one distinct demoable flow each (mapped 1:1 from approved slices)
- Template: `$(aah path)/_resources/_templates/architecture/module-map.yaml`
- Fields: id, name, description, demo_criteria, smoke_test, layers{expandable per use case}, depends_on

### Document generation

- **All docs are generated, at every tier** — no tier gating. Each is ALWAYS written as its own separate file — never merged into a consolidated doc.
- **Applicability is the only skip.** Skip `agent-topology` if no agents; skip `api-schema` and `api-interface-contract` if no FE↔BE API surface (backend-only or frontend-only project).
- **The user confirms the list, not the contents.** Step 5a shows what will be generated (and what was skipped, with reasons) and waits for confirmation; the user can adjust the list before generating.
- **`api-schema` is fan-out.** It produces N files under `.aah/architecture/schema/` — one `MOD-NNN-api-schema.yaml` per API-exposing module (Step 5b.i). Filename is ownership; no `x-aah-module` field.
- Templates: `$(aah path)/_resources/_templates/architecture/`
- Review: `aah-arch-review-board` agent (mvp/prod only)

### Isolation contract

All artifacts → `.aah/architecture/`. Decisions append to `.aah/discuss/decision-registry.yaml` under `gray_area: architecture-gaps`.
