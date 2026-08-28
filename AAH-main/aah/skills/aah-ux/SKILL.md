---
name: aah-ux
user-invocable: true
description: Capture UI requirements during the AAH Architect phase — turn project intent (and optional reference UIs) into a design contract: low-fi HTML wireframes, a machine-readable `design-spec.yaml`, a retained hi-fi direction comp, and a README. Runs when a frontend is in scope and feeds the screen inventory + screen→backend hints into the aah-arch architecture docs (api-interface-contract.md, application-flow.md, module-map.yaml) so FE–BE integration is resolved through the architecture artifacts.
---

# AAH-UX — UI Requirement Capture (Architect phase)

Turn project intent into a **design contract** the rest of the pipeline
consumes: per-screen low-fidelity HTML wireframes (structure), a
machine-readable `design-spec.yaml` (the **system** — regions, states, data
seams, Tier-1 tokens, direction rules), a retained hi-fi direction comp
(`design/direction.html`, the **soul** — rich/bespoke visuals as runnable
code), and a README with the IA/nav model and screen→backend hints. Structure
AND the design-system decision are captured here so they survive into Plan
and Implement as a **pointer**, never flattened into `feature.md`. References
inform **structure only, never brand**.

The output lives in `.aah/architecture/` and is consumed by the `aah-arch`
skill's doc-generation step (screen data-dependencies from `design-spec.yaml`
→ FE↔BE seams in `api-interface-contract.md` and `application-flow.md`;
screen inventory + module mapping → `module-map.yaml` and
`architecture-overview.md`) and, downstream, by Plan (feature Description
cites `design-spec.yaml` + screen ids) and Implement (`frontend_guidance.md`
§0 resolves the contract by reading the spec directly — no hook). It does NOT
carry forward into Plan as wireframes — Plan derives features from the
synthesized architecture context plus the design-contract pointer.

## Core Principle: Up to 6 User Interactions

The skill has up to 6 user-facing checkpoints. Each is prefixed with its fixed
position in the sequence — e.g. "Checkpoint 3 of 6" — so the user always knows
how much is left. Numbering reflects the checkpoint's position, not a running
count: if Step 2 is skipped (see below), the next checkpoint the user sees is
still labeled "Checkpoint 3 of 6," never renumbered.

1. **Confirm context + design intake** — frontend summary, open questions, AND
   the design-intent questions (expression, density, feature appetite) in ONE
   `AskUserQuestion` call (Step 1)
2. **Input files** — structural + layout references (Step 2, skipped if
   `knowledge/references/` already has files)
3. **Design System + Mode** — choose the design system (registry-driven) and
   light/dark mode, resolved together in one `AskUserQuestion` call (Step 2.5)
4. **Confirm screens** — screen list table + global sections; the DDS flow
   adds an archetype column (≤3 iterations) (Step 3)
5. **Choose design direction** — the art-director moment: 2–3 rendered
   composition options for the entry screen, built on the screen structure
   proposed in Step 3; the user picks one and it governs every screen
   (Step 3.5)
6. **Confirm wireframes** — the structure sign-off: low-fi grayscale HTML for
   every screen, built per the chosen direction, reviewed (≤3 iterations)
   (Step 4b)

Design is elicited, not just validated: intake (CP1) captures intent BEFORE
any proposal exists; the design system (CP3) is resolved before screens so
the DDS-only archetype behavior has it in hand; direction is chosen (CP5)
right after screens are proposed, BEFORE wireframes are signed off (CP6) —
the chosen composition governs how every screen's structure gets built,
rather than art-directing a structure that's already locked in.

Everything else runs autonomously.

## MANDATORY: Tool usage for questions

Whenever this skill says "ask" or "sign-off", you MUST invoke the `AskUserQuestion`
tool — never present questions as plain markdown. Prefix every checkpoint's question
text with its position from the list above (e.g. "Checkpoint 3 of 6 — Design System
& Mode") so the user always sees where they are in the flow.

---

## Step 1 — Read All Context

**Preamble:** Resolve `$PROJECT_DIR` via `aah run core.common.config project-path`. Set `$AAH_DIR` = `$PROJECT_DIR/.aah`.

Read everything available from the Discuss and Architecture phases to build full context before any UI decisions are made.

**Read everything under these directories:**
- `$AAH_DIR/discuss/` — all discuss phase artifacts (includes `discuss-prd.md`, `project-intent.yaml`)
- `$AAH_DIR/architecture/` — all architecture phase artifacts
- `$AAH_DIR/discuss/decision-registry.yaml` — all resolved decisions, context, downstream constraints


No need to enumerate individual files — just read whatever exists in those directories.

**Minimum required:** `$PROJECT_DIR/knowledge/project-brief.md` OR `$AAH_DIR/discuss/decision-registry.yaml` (either alone is enough; `$AAH_DIR/discuss/discuss-prd.md` is also a valid brief source when present). If none exists, halt and inform the user that at least one context source is needed to proceed. If any other artifact does not exist, skip it and note as assumption.

### Frontend Summary

At the end of Step 1, generate and present a **summary of all frontend-related information** found across the files read. This includes:
- Frontend framework and integration style
- UI patterns, component libraries, or design system references mentioned
- Screens or views implied by decisions, user flows, or domain workflows
- Agentic/AI surfaces implied by architecture decisions
- Visual direction or theme references (if present in `knowledge/theme/`)
- Backend services/modules that require UI surfaces
- Any constraints on layout, viewport, accessibility, or interaction style

### Surface Open Questions

Based on the summary, surface **open questions relevant to wireframe structure**.
Do not assume — if information is ambiguous or missing, ask. Examples:
- "Decision X implies a real-time feed — is this a persistent sidebar or a dedicated screen?"
- "The compliance workflow references approval states but doesn't specify how users transition between them — what's the flow?"

Do NOT ask about theme/visual references here — that is handled separately in Step 2.

### Design Intake (same `AskUserQuestion` call — no extra checkpoint)

Alongside context confirmation, ask the design-intent questions (one call, up
to 4 questions total: context confirmation + 3 intake questions). These are
captured BEFORE any design exists — they shape screen proposal (Step 3),
archetype pruning (DDS flow only), the direction options (Step 3.5), and
`design-spec.yaml`:

1. **Expression dial** — "How expressive should this application feel?"
   Options: `Restrained` (quiet, institutional — minimum motion, no hero
   moments) / `Balanced` (confident hierarchy, selective emphasis — default) /
   `Expressive` (editorial hero moments, orchestrated reveals, strong
   typographic voice).
2. **Density default** — "What information density should the application
   default to?" Options: `Compact` (analyst-grade, information-dense) /
   `Comfortable` (default) / `Spacious` (executive/editorial). DDS archetypes
   may propose a per-screen override in Step 3.
3. **Signature-feature appetite** — "Beyond the required features, how far
   should we push differentiating capabilities (AI insight surfacing, command
   palette, live/ambient data, human-in-the-loop flows)?" Options:
   `Essentials only` / `Recommend the standouts` (default — propose
   signature features flagged `suggested`) / `Push the envelope` (propose
   ambitious features; user prunes).

Record answers as `$EXPRESSION`, `$DENSITY_DEFAULT`, `$FEATURE_APPETITE` —
they are written into `design-spec.yaml` (Step 4d) and govern all downstream
choices, including whether Step 3.5 (Choose Direction) runs at all.

Present the frontend summary, open questions, and intake questions in ONE
`AskUserQuestion` call, prefixed **"Checkpoint 1 of 6 — Confirm Context &
Design Intake,"** before proceeding.

---

## Step 1.5 — Issue Brief — Look Before You Work — MANDATORY, ALWAYS RUNS

**Always run this step — do NOT skip even if prior steps errored.**

Invoke the `aah-issue-reporter` agent with: "Brief me on **ux** issues."
Read the returned brief; decide and act with your phase context (absorb feedback →
re-entry, note feature status (do not close feature issues), or surface untriaged issues). For every issue reviewed,
post a GitHub comment explaining the decision made — whether actioned, deferred to a future
iteration, or out of scope for the current run. Do NOT close unless the issue is fully resolved.

---

## Step 2 — References (Structure)

All user-provided reference files are stored in the `knowledge/` folder.
Structural/layout references go in `knowledge/references/`, theme/visual
references go in `knowledge/theme/`.

**Before asking**, check if `knowledge/references/` already has files. If yes,
read them directly and proceed — no need to ask.

If `knowledge/references/` is empty, you MUST ask the user (via `AskUserQuestion`,
prefixed **"Checkpoint 2 of 6 — Reference Files"**) whether they have reference files
to upload (screenshots, wireframes, style sheets, competitor UIs). This is a separate
checkpoint — skip it entirely (and skip its number) if files are already present.

**Supported file types:** MD, TXT, YAML, JSON, HTML, CSS.

Read the reference files directly and use them to inform wireframe structure and
layout patterns — do not generate an intermediate brief or extracted artifact.

**Re-read after user response:** If the user says they have uploaded files after
the prompt, re-read the `knowledge/` folder before proceeding.

**Theme for HTML chrome:** Resolved from the design system chosen in Step 2.5
— see Step 4c for how each system's tokens/guidance are applied to the page
frame. If `knowledge/theme/` contains uploaded theme files, they inform the
**`frontend-design`** system's generative method if that system is chosen —
for `deloitte-brand` and `dds`, chrome is always the system's fixed/library
tokens regardless of uploaded material.

---

## Step 2.5 — Design System + Mode

After references are resolved, choose the design system and mode BEFORE any
screens are proposed — the DDS-only archetype behavior in Step 3 needs
`$UI_SYSTEM` already resolved, and the whole flow branches off this choice.

**Read the registry:** `$(aah path)/_resources/_references/frontend-design/design-systems.yaml`.
Each entry declares `id`, `label`, and the 5 contract slots: `token_source`
(fixed | library | generated), `component_model` (composition | library),
`guidance` (skill | ref | library reference), `install` (recipe string |
none), `binding.raw_hex` (forbidden | within-tokens). This registry is the
extensibility point for this skill — adding a 4th system is a new entry here,
never a skill edit.

**Ask, in one `AskUserQuestion` call, prefixed "Checkpoint 3 of 6 — Design
System & Mode":**

**Question 1 — Design system:**
> "Which design system should this application use?"

Render exactly one option per registry entry (its `label`) — do NOT add a
manual "Other" entry to the options list; `AskUserQuestion` already offers
free text on every question automatically, and the options list is capped at
4, so a hand-added "Other" wastes the slot the registry needs to stay at
"add an entry, never edit the skill." If the user answers via the tool's
built-in free-text option instead of picking a registry entry, capture their
description and default its contract slots to `token_source: generated`,
`component_model: composition`, `guidance: {ref: <user description>}`,
`install: none`, `binding.raw_hex: within-tokens` — treat it like the
`frontend-design` flow everywhere downstream unless the description implies
otherwise.

**Question 2 — Mode:**
> "Which mode should the application default to — light or dark?"

Hold the answers as `$UI_SYSTEM` (registry id) and `$UI_MODE`. Resolve and
hold the five contract slots from the matched registry entry as
`$TOKEN_SOURCE`, `$COMPONENT_MODEL`, `$GUIDANCE`, `$INSTALL`, `$BINDING`.

**No format question anywhere in this skill.** Output is HTML always for
per-screen wireframes — there is no markdown/HTML/both choice.

**No free-text theme question.** Theme is either resolved now (fixed brand
tokens for `deloitte-brand`, library tokens for `dds`) or generated later, at
Step 3.5, for `frontend-design` — never asked as open-ended text here.

---

## Step 3 — Screen Proposal (AskUserQuestion sign-off)

Propose a list of screens based on Step 1 context AND Step 2 references. Present
as a concise table — the user confirms WHICH screens exist, WHO uses them, and
WHAT they do. Layout detail belongs in wireframes (Step 4), not here.

**DDS flow only — classify against the archetype library.** If `$UI_SYSTEM ==
dds`, read `$(aah path)/_resources/_references/frontend-design/archetypes/_archetype-index.md`
and every archetype file it lists. Classify every screen against the library
using each archetype's `## Signals` section — its IA model shapes the nav
proposal, its signature features seed screens/features the user may not have
thought to ask for (propose them, flagged `suggested`, pruned by
`$FEATURE_APPETITE`), and its layout becomes the wireframe's starting
composition in Step 4. A screen matching no archetype (< 2 signal hits) is
marked `none` — never force a fit. `deloitte-brand` and `frontend-design`
never load or consult the archetype library — they keep the plain table below.

### Screen list table format:

For `$UI_SYSTEM == dds`, add an **Archetype** column:

```
PROPOSED SCREENS:

| # | Screen | User(s) | Archetype | Module(s) | Description |
|---|--------|---------|-----------|-----------|-------------|
| 1 | Research Dashboard | Research analyst | analyst-workbench | coverage, notifications, market-data | Central hub — watchlist, alerts, market summary |
| 2 | Note Editor | Research analyst | agent-app | drafting-agent, citations, compliance | Write and refine research notes with AI assistance |
| 3 | Compliance Dashboard | Compliance officer | approval-queue | compliance, restricted-list, audit | Review queue for note approval and regulatory checks |
```

For `deloitte-brand` / `frontend-design` (plain table, no Archetype column):

```
PROPOSED SCREENS:

| # | Screen | User(s) | Module(s) | Description |
|---|--------|---------|-----------|-------------|
| 1 | Research Dashboard | Research analyst | coverage, notifications, market-data | Central hub — watchlist, alerts, market summary |
| 2 | Note Editor | Research analyst | drafting-agent, citations, compliance | Write and refine research notes with AI assistance |
| 3 | Compliance Dashboard | Compliance officer | compliance, restricted-list, audit | Review queue for note approval and regulatory checks |
```

The **Module(s)** column maps each screen to the backend modules/services it depends
on. This shows how the system connects — which screens share modules (coupled) and
which are independent. Downstream plan-phase agents use this to sequence features
correctly.

### Global sections (presented alongside the table):

- **Nav / IA model** — how screens connect; primary navigation. Identify the entry
  screen (the first screen the user sees after login/launch).
- **Assumptions list** — everything inferred
- **Open questions**
- **Density** (per screen) — defaults to `$DENSITY_DEFAULT` from intake; DDS
  archetypes may propose a per-screen override — call it out in the table
  notes. Never left as "to be decided."

### Sign-off scope:

The user approves or rejects the ENTIRE Step 3 output as one package via
`AskUserQuestion`, prefixed **"Checkpoint 4 of 6 — Confirm Screens."** One "looks good"
covers everything. Iterate with the user until signed off (bounded: ≤3 rounds, then
"park open questions & proceed").

Continue to Step 3.5.

---

## Step 3.5 — Choose Direction (the art-director moment)

Skip this checkpoint entirely when `$EXPRESSION == restrained` — in that
case hold `$DIRECTION = { label: "Default", rules: [], comp: none }` and
continue to Step 4. Otherwise, run this exactly once, right after Step 3's
screen list is signed off — before any wireframe is generated:

> **Goal:** give the user *real* art-direction options for the **entry screen only** — genuinely
> different **compositions** of the same screen, built on the entry screen's structure as proposed
> in Step 3. This is where the user does art direction instead of rubber-stamping. Do NOT generate
> one take-it-or-leave-it comp.
>
> **First, internally design the entry screen's region model** — regions, tags (`[A]`, `[B]`, …),
> the hero region marked `★`, roles, data dependencies, and required states — the same way Step 4
> designs every screen's structure. You need this concrete structure to build real comps, not
> placeholder ones. This becomes the entry screen's locked structure; Step 4 reuses it rather than
> re-deriving it.
>
> **Inputs you already hold:** the entry screen's row from Step 3's screen table (plus — dds flow
> only — its archetype's layout/composition rules); `$UI_SYSTEM` + its resolved contract slots;
> `$UI_MODE`; the intake `$EXPRESSION` / `$DENSITY` / `$APPETITE`; the resolved `tokens` (fixed,
> generated, or a library namespace); and — **dds flow only** — the entry screen's `archetype` +
> its composition rules.
>
> **Generate 2–3 direction comps** as self-contained HTML files (`_direction-A.html`,
> `_direction-B.html`, `_direction-C.html`) under `applications_wireframes/`. Each renders the
> **same entry screen with the same regions and the same real content** — they differ in
> **composition, never scope**:
> - **hero treatment** — what the `★` region becomes and how dominant it is;
> - **nav model** — persistent left sidebar vs. top-nav vs. hybrid; **at least one option must
>   challenge the default left sidebar** when the screen allows it;
> - **density interpretation** of `$DENSITY`;
> - **typographic emphasis** — where the display tier is spent.
>
> **Constraints:**
> - **Honest, not skins.** Build from the screen's *real* regions/roles/plausible data — never
>   three recolors of one layout. If two comps render the same DOM, you have not diverged.
> - **Bounded by `$EXPRESSION`.** `restrained` → three quiet, closely-spaced variants. `balanced` →
>   clearly distinct compositions. `expressive` → at least one comp takes a real editorial risk.
> - **Label each direction** with a memorable name + one sentence on *when it wins*.
> - Self-contained HTML, CSS inlined, no external JS/CDN. Auto-open all comps in the browser.
>
> **Per-system rendering — branch on `$UI_SYSTEM`:**
> - **`deloitte-brand`** — invoke `Skill("aah-deloitte-brand")`; paint every comp with the
>   **fixed brand palette/type**. Divergence is composition only; brand is constant.
> - **`dds`** — paint with **real DDS token values** (from the catalog if reachable, else the
>   `aah-deloitte-brand` token tables — note the assumption). Caption each region with the DDS
>   component it maps to per the archetype (`[C] DataTable`). Divergence is composition only.
> - **`frontend-design`** — run the generative method: for **each comp, invent a genuinely
>   different token system** (4–8 named hex, display+body+mono pairing, a signature element) per
>   `frontend_guidance.md`, then compose the screen with it. Here the **palette itself diverges**
>   across comps — that divergence *is* the art direction; `$EXPRESSION` bounds the spread.
>
> **Ask (`AskUserQuestion`):** "Which direction should govern the application?" — one option per
> comp (label + when-it-wins), plus built-in free-text for hybrids ("A's hero with C's rail").
>
> **Record & retain:** `$DIRECTION = {label, rules: [2–4 bullets generalizing the winning
> composition to every screen], comp}`. **Save the chosen comp as `design/direction.html`** (the
> visual source of truth the spec references via `direction.comp`); delete the unchosen
> `_direction-*.html`. Apply `$DIRECTION.rules` to every screen — including the entry screen's
> locked structure above — when Step 4 builds the wireframes.

Prefix the `AskUserQuestion` call in the block above with **"Checkpoint 5 of
6 — Choose Design Direction."** `design/direction.html` is saved under
`$AAH_DIR/architecture/design/direction.html`.

Continue to Step 4.

---

## Step 4 — Generate the per-screen wireframes (HTML, low-fi, structure only)

For each signed-off screen, produce a low-fidelity HTML wireframe (see the
mockup spec in Step 4c). First design the wireframe structure internally:
regions, tags (`[A]`, `[B]`, …), the hero region marked `★`, a legend mapping
each tag to role/data-dependency/agentic-affordance/required states, and a
viewport annotation (`viewport: desktop-first (1280px min)`). For the entry
screen, reuse the region model already locked in Step 3.5 rather than
re-deriving it. Apply `$DIRECTION.rules` (chosen in Step 3.5, or the default
if that step was skipped) to every screen's structure — nav model, hero
treatment, density interpretation, and typographic emphasis stay consistent
across the whole application. This internal structural model is what later
gets serialized into `design-spec.yaml` (Step 4d) — the HTML mockup must
carry every tagged region and its legend so the structural contract survives
regardless of how it's rendered.

There is no markdown output and no format choice — every screen gets exactly
one `NN-<screen>.html`.

---

## Step 4b — User Review of Wireframes (the structure sign-off)

Write `NN-<screen>.html` for every screen to
`$AAH_DIR/architecture/applications_wireframes/`, then present a review
checkpoint.

Tell the user: "Wireframes have been generated. Please review them at:
`$AAH_DIR/architecture/applications_wireframes/`"

List each file written (e.g., `01-dashboard.html`, `02-coverage-list.html`, …).

**Auto-open HTML mockups.** Immediately after writing the files and before
asking the review question, open every generated mockup in the user's default
browser so they can review the rendered UI. This always runs now — HTML is
the only per-screen output format. Run:

Target three platforms — **Windows, Linux, macOS**:

```bash
cd "$AAH_DIR/architecture/applications_wireframes" && \
  shopt -s nullglob && \
  for f in *.html; do
    case "$(uname -s)" in
      Darwin)          open "$f" ;;                         # macOS
      Linux)           xdg-open "$f" ;;                      # Linux
      MINGW*|MSYS*|CYGWIN*|Windows_NT) cmd //c start "" "$f" ;;  # Windows
      *)               echo "Unknown platform — open manually in: $(pwd)" ;;
    esac
    echo "opened: $f"
  done
```

Notes:
- `uname -s` selects the platform: `Darwin` → macOS (`open`), `Linux` → `xdg-open`,
  Windows shells (`MINGW*`/`MSYS*`/`CYGWIN*`) → `cmd //c start`.
- `shopt -s nullglob` makes a non-matching `*.html` glob expand to nothing (loop
  skipped) instead of the literal string `*.html`, so a bogus filename is never opened.
- The empty `""` after `start` is its title argument — required so a quoted path with
  spaces isn't misread as the window title.
- If no opener runs (unknown platform), note it and tell the user the folder path to
  open manually instead of failing.

Ask (via `AskUserQuestion`, prefixed **"Checkpoint 6 of 6 — Confirm
Wireframes"**): "Have you reviewed the wireframes? Are there changes needed?"

Options:
- "Approved — proceed to commit"
- "Changes needed — I'll describe what to adjust"

**If changes needed:** Apply the user's requested modifications to the affected
wireframe files. Then re-present the review checkpoint. Bounded: ≤3 review
iterations total. After 3 rounds, park remaining feedback as open questions in the
README and proceed to commit.

**Scope guard:** Review iterations are for adjusting layout/structure of proposed
screens. Requests for entirely new screens should be flagged as scope change — park
them as open questions rather than adding mid-iteration.

**If approved:** Continue to Step 4d (Emit `design-spec.yaml`).

---

## Step 4c — HTML mockup spec (low-fi, every screen, every system)

This step is the build spec for the per-screen HTML mockups. Every screen
gets one, regardless of `$UI_SYSTEM` — per-screen mockups are ALWAYS low-fi
grayscale; there is no per-system hi-fi per-screen path. (The one hi-fi
moment in this skill is the single retained entry-screen direction comp,
generated once in Step 3.5, before the per-screen wireframe structure is
signed off.)

**Scope:** HTML mockups are generated **per screen only** — one
`NN-<screen>.html` for each screen. `README.md` is never rendered to HTML.

**What to generate — a real low-fidelity UI, not ASCII.** Each mockup must be an
actual HTML rendering of the screen: use CSS (flexbox/grid) to reproduce the
regions as real boxes, and render each region's intent with real UI elements
(e.g. an input region → `<input>`/`<textarea>`, an action region → `<button>`,
columns → flex/grid columns, a card → a bordered card block, a table →
`<table>`). **Do NOT** dump ASCII into a `<pre>`. The goal is a page a
stakeholder can open in a browser and read as a UI mockup.

Preserve fidelity to the wireframe contract:
- Every tagged region appears in the mockup, labeled with its tag (`[A]`,
  `[B]`, …) — e.g. a small corner badge or caption per region — so the
  mockup traces back to the region legend.
- Mark the hero region (`★`) as visually dominant.
- Show the region's **states** where cheap to convey (e.g. an empty/loading
  hint), and keep the legend's role + data-dependency accessible (a caption,
  `title` attribute, or a small legend block at the foot of the page).
- Include the screen's spine as a heading and the viewport annotation.

**Low-fidelity, structure not brand — brand the frame, not the UI.** The
**screen mockup itself** (the login card, task cards, buttons, columns,
tables — everything inside the app canvas) MUST stay neutral grayscale for
every system: boxes, placeholder text, and form controls, with no color
palette, real copy, imagery, or design system. This keeps the review focused
on structure — the product's real styling comes from the entry-screen
direction comp already chosen in Step 3.5, and, in full, from Implement.

**The page frame** — the header bar, nav, and footer that wrap the document —
is themed by the resolved `$GUIDANCE` slot, chrome only:

- **`deloitte-brand`** — invoke `Skill("aah-deloitte-brand")` and apply its
  colors/typography **only to the page chrome**.
- **`dds`** — apply real DDS token **values** for chrome only (read from the
  DDS catalog if reachable, else fall back to the `aah-deloitte-brand` token
  tables and note the assumption). Never import the DDS library here — values
  only, no components.
- **`frontend-design` / `Other`** — the palette was already generated and
  locked in at Step 3.5 (`$DIRECTION`'s resolved tokens); apply those tokens
  to the chrome only, same treatment as the two systems above. If Step 3.5
  was skipped (`$EXPRESSION == restrained`, no palette generated yet), keep
  the chrome neutral/minimal, same as the canvas, and defer real styling to
  Implement.

NEVER let any chrome styling reach the app canvas or any region inside it —
the mockup UI stays neutral grayscale. Keep the two CSS scopes clearly
separated so no brand color leaks into the UI being reviewed.

**Per-screen canvas hints:** Use `$DENSITY_DEFAULT` (or a DDS archetype's
per-screen override confirmed in Step 3) to inform layout choices for the app
canvas — e.g. compact spacing for compact density, a dark-canvas note for
`$UI_MODE == dark`. These are structural hints only; the canvas must still
stay grayscale (no color, no real typography).

This spec governs every HTML mockup Step 4 designs and Step 4b writes — it is
not a separate sequential step; Step 4/4b consult it while generating, then
Step 4b's own review checkpoint (Checkpoint 6 of 6) proceeds as described
above.

---

## Step 4d — Emit `design-spec.yaml`

After wireframes are approved (Step 4b) — direction was already resolved
earlier at Step 3.5, or defaulted per its skip condition — write the
machine-readable design contract to `$AAH_DIR/architecture/design-spec.yaml`.
This is the artifact `aah-plan` and Implement read by path — it is the reason
the design decision survives past this skill without ever being flattened
into `feature.md`.

```yaml
schema_version: 1
ui_system: <registry id>                 # $UI_SYSTEM: deloitte-brand | dds | frontend-design | <other>
token_source: fixed | library | generated  # $TOKEN_SOURCE
component_model: composition | library     # $COMPONENT_MODEL
guidance: { skill|ref|library: ... }       # $GUIDANCE, e.g. {skill: aah-deloitte-brand} / {library: "@deloitte-us-consulting/dds"} / {ref: frontend_guidance.md}
install: none | "<recipe>"                 # $INSTALL
binding: { raw_hex: forbidden | within-tokens }  # $BINDING
mode: light | dark                         # $UI_MODE
direction: { label, rules: [...], comp: design/direction.html }  # $DIRECTION
tokens: { color: {...}, type: {...}, spacing: [...], radius: {...} }   # Tier-1 only
intake: { expression, density_default, feature_appetite }   # $EXPRESSION / $DENSITY_DEFAULT / $FEATURE_APPETITE
screens:
  - id: 01-dashboard
    entry: true
    archetype: analyst-workbench          # DDS flow only; omit/none otherwise
    density: compact
    mode: light
    hero: C
    modules: [coverage, notifications]
    regions:
      - tag: C
        role: primary
        components: [DataTable]            # library mode (dds)   —OR—
        compose: "token-styled dense table"   # composition mode (brand, frontend-design)
        states: [empty, loading, error]
        data: "GET /api/alerts"
navigation: { model, entry, flows: [...] }
```

Rules:
- Every screen from the signed-off Step 3 table appears; every region tag
  from the wireframe appears with its role, `components` (library mode) OR
  `compose` (composition mode), data dependency, and states.
- `components` lists ONLY real DDS exports in `dds` mode — validate names
  against the DDS catalog read in Step 4c; a typo here becomes a build
  failure downstream. `frontend-design` and `deloitte-brand` always use
  `compose`, never `components`.
- No richer visual primitives (`gradients`/`effects`/`textures`) go in this
  file — those live only in `design/direction.html`. Let the comp carry them.
- Register it: `aah run core.common.manifest add-artifact "architecture/design-spec.yaml"`.

Continue to Step 5.

---

## Step 5 — Write + Commit

Write all files under `$AAH_DIR/architecture/`, register the README artifact,
and commit:

```bash
aah run core.common.manifest add-artifact "architecture/applications_wireframes/README.md"
git add "$AAH_DIR/architecture/applications_wireframes/" "$AAH_DIR/architecture/design-spec.yaml" "$AAH_DIR/architecture/design/direction.html" && \
  git commit -m "architect(aah-ux): capture UI wireframes + design-spec + direction comp"
git push origin HEAD
```

The `git add` above stages the wireframes folder, `design-spec.yaml`, and the
retained `direction.html` together (omit the `design/direction.html` path if
Step 3.5 was skipped and no comp exists).

Present a summary: screen count, nav model in one line, the count of
screen→backend hints captured, the design system chosen (`$UI_SYSTEM` +
`$UI_MODE`), and the direction label chosen in Step 3.5 (or "default — no
direction spread" if skipped).

**Comment back to GitHub issue (if version_control enabled).** If this run was triggered by a GitHub
issue surfaced at Step 1.5, run:
```bash
aah run core.version_control.cli status
```
If enabled, and the commit + push above completed successfully, post a resolution comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "UX wireframes updated based on this issue: <brief summary of what changed — screens added/modified>. Changes committed."
gh issue close <N>
```
Where `<N>` is the issue number from the Step 1.5 brief that triggered this rework. If the feedback
came from `$ARGUMENTS` (not a GitHub issue), skip this step. If the commit or push failed, do NOT
close the issue. If version_control is disabled or offline, skip silently.

---

## Output contract

```
.aah/architecture/
├── design-spec.yaml          # ALWAYS — machine-readable design contract (Step 4d)
├── design/
│   └── direction.html        # the retained hi-fi entry-screen comp (Step 3.5); absent only if Step 3.5 was skipped
└── applications_wireframes/
    ├── README.md              # ALWAYS markdown — screen list, IA/nav model, module mapping
    ├── 01-<screen>.html       # low-fi grayscale HTML wireframe — every screen, every system
    ├── 02-<screen>.html  …
```

There is no per-screen `.md` and no format choice anywhere in this skill —
HTML is the only per-screen output, always.

### Each screen file `NN-<screen>.html`

1. A spine heading — the one question the screen answers.
2. Every region tagged (`[A]`, `[B]`, …), rendered as real low-fi DOM
   elements (never ASCII), with the hero region (`★`) visually dominant.
3. A legend (footer block, caption, or `title` attribute per region) mapping
   each tag → role · data dependency · agentic affordance · required states.
   The data dependency names the backend call where known (e.g.
   `/api/orders`, `GET /api/agent/stream`) — these become integration seams
   downstream via `design-spec.yaml`.
4. A viewport annotation: `viewport: desktop-first (1280px min)`.
5. A neutral grayscale canvas; chrome themed per Step 4c.

### `README.md` must include

- **What was decided** — which screens were proposed, iterated on, and confirmed.
  Key decisions made during the review iterations (what was added, removed, or
  changed and why).
- **Screen list** — each `NN-<screen>.html` with its spine.
- **IA / nav model** — how screens connect; the primary navigation structure. Entry screen identified.
- **Viewport targets** — e.g. desktop-first, responsive, mobile.
- **Screen → backend hints** — a table mapping each screen's data dependencies to
  candidate backend endpoints/seams, so the architecture agents can wire FE↔BE.
  Mark any inferred dependency `assumed`.
- **Screen → module mapping** — a table connecting each UI screen to the backend
  module(s) / service(s) it depends on. Traces which business logic modules power
  each screen, enabling downstream traceability from UI to implementation.

  | Screen | Module(s) | Responsibility |
  |--------|-----------|----------------|
  | 01-dashboard | `auth`, `notifications`, `coverage` | User context, alerts, coverage summary |
  | 03-note-editor | `drafting-agent`, `citations`, `compliance` | AI draft generation, citation retrieval |
  | 06-compliance-dashboard | `compliance`, `restricted-list`, `audit` | Queue management, audit trail |

- **Open questions resolved and unresolved** — questions surfaced in Step 1 and
  how they were resolved (or marked as still open).
- **Knowledge references used** — files from `knowledge/` that informed the
  wireframes (provenance).
- **Design system** — `$UI_SYSTEM` + `$UI_MODE`, the resolved contract slots
  (`token_source`, `component_model`, `guidance`, `install`, `binding`), and
  — DDS flow only — each screen's archetype classification.
- **Direction** — `$DIRECTION.label` and its rules (or "default — direction
  step skipped, per `$EXPRESSION == restrained`" if Step 3.5 didn't run);
  note the path to `design/direction.html` when present.

---

## Guardrails

- References inform structure, never brand. Brand comes from exactly one
  place — the resolved `$UI_SYSTEM`'s contract slots (fixed brand tokens for
  `deloitte-brand`, library tokens for `dds`, the generated palette locked in
  at Step 3.5 for `frontend-design`) — recorded in `design-spec.yaml`, never
  improvised later and never flattened into `feature.md`.
- Every screen has a spine, a tagged low-fi HTML wireframe, a legend with
  data dependencies, and a `design-spec.yaml` entry — no exceptions. DDS-flow
  screens also carry an archetype classification.
- Per-screen wireframes (Step 4c) stay grayscale for every system, always —
  the only hi-fi rendering in this skill is the single retained entry-screen
  direction comp (Step 3.5).
- In `dds` mode, every component name written into `design-spec.yaml` must
  exist in the DDS catalog. Do not invent component names.
- No richer visual primitives in `design-spec.yaml` — gradients, effects,
  textures, and animation live only in `design/direction.html`.
- Bounded iteration: never loop indefinitely. ≤3 rounds per checkpoint, then
  park & proceed.
- If reference images are unreadable, proceed without them and flag assumptions.
