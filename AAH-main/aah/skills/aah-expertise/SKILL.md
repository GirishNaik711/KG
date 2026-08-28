---
name: aah-expertise
description: >
  Expertise system management for AAH projects. Handles Tier 1 (expertise.yaml)
  and Tier 2 (domains/*.yaml) updates after features pass QA. Invoked by the
  orchestrator's update_expertise action. Governs how learnings are extracted,
  domain files are built, and project knowledge is consolidated across waves.
user-invocable: false
disable-model-invocation: false
---

# AAH Expertise System

Manages the two-tier expertise system that captures semantic project knowledge
accumulated through implementation waves.

**Governing philosophy:** `.claude/skills/aah-expertise/references/mental-model.md` § Application to Expertise Files

## Architecture

```
.aah/codebase-intel/
├── expertise.yaml          <- Tier 1: project-wide style guide, concerns, patterns
└── domains/
    ├── {domain}.yaml       <- Tier 2: per-module deep knowledge (YAML, not markdown)
    └── ...
```

## Shared Definitions

These constraints apply to ALL modes (greenfield seeding, brownfield seeding, wave updates).

### Consumption Views — Format & Constraints

All three views live under `consumption_views:` in expertise.yaml.

**quick_context** (max 8 sentences):
Project orientation covering system purpose, architecture style, key components,
data layer, and deployment model. Tells agents WHAT the system is at a glance.

**style_guide** (max 30 lines):
Cross-cutting imperative implementation rules grouped by concern (naming, config,
database, infrastructure, health, testing). Each rule tells agents HOW to implement
correctly. Must be actionable — not descriptive.

**known_concerns** (max 15 entries):
Active warnings agents must be aware of. Each entry explains:
- **WHEN** it triggers (condition/scenario)
- **WHAT** the symptom looks like (observable effect)
- **HOW** to avoid it (concrete action)

Includes domain-level `known_issues` from Tier 2 when available.

### Conciseness Rules

- Max 5 `tech_patterns` total
- Max 5 `project_knowledge` entries per wave
- Max 4 `integrations`
- Each entry is 1-2 sentences — no paragraphs
- Only capture patterns that directly guide implementation decisions
- Do NOT include structural codebase info (file paths, symbol lists) — codemap.db handles that
- Do NOT speculate about future waves — only capture what is known NOW
- Do NOT create a consumption_views/ directory or separate files for views — all three views (quick_context, style_guide, known_concerns) are YAML fields inside expertise.yaml, never standalone markdown files
- consumption_views MUST contain ALL THREE fields: quick_context, style_guide, known_concerns — if any is missing after an update, the expertise update is INCOMPLETE and the marker must not be written

### Seeding Marker Format

Both greenfield and brownfield seeding write `.aah/build/expertise-seeded.json`:

```json
{
  "seeded_at": "{ISO timestamp}",
  "source": "greenfield | brownfield_import",
  "sources_read": ["list of artifact filenames read"],
  "tech_patterns_seeded": N,
  "project_knowledge_seeded": N,
  "style_guide_lines": N,
  "known_concerns_count": N,
  "quick_context_generated": true,
  "tier2_domains_created": []
}
```

Note: `tier2_domains_created` is empty for greenfield (Tier 2 not created at seeding).

---

## Seeding — Greenfield (First-Time — Before Wave 1 Dispatch)

Triggered by `aah-build` Step 2b when expertise.yaml is empty template.
Reads plan-phase artifacts to pre-populate Tier 1 with initial expert context.

### Trigger Condition

- `.aah/build/expertise-seeded.json` does NOT exist
- `.aah/codebase-intel/expertise.yaml` has empty `consumption_views`

### Steps

1. **Read source artifacts** (in order):
   1. `.aah/manifest.yaml` → `stack_choices` for primary tech stack
   2. `.aah/architecture/architecture-overview.md` → Architecture patterns, framework decisions
   3. `.aah/discuss/decision-registry.yaml` → Resolved technology decisions
   4. `.aah/plan/resolved-standards.yaml` → Coding conventions, security rules
   5. `.aah/architecture/risk-security-compliance.md` → Performance targets, security constraints
   6. `.aah/architecture/application-flow.md` → External system integrations, data flows
   7. `.aah/architecture/data-model.md` (if exists) → Entity relationships, storage patterns
   8. `.aah/architecture/api-interface-contract.md` (if exists) → API conventions, endpoint patterns
   9. `.aah/plan/features/*.md` → File scope (project structure), constraints
   10. `.aah/codebase-intel/codebase-learning.md` (if brownfield) → Existing patterns

2. **Extract into expertise.yaml** per this mapping:

   | From | Into |
   |------|------|
   | architecture-overview + decision-registry: architecture style, framework rationale | `tech_patterns` |
   | Feature files: API conventions, data patterns | `consumption_views.style_guide` |
   | Standards: naming, imports, typing, testing | `consumption_views.style_guide` |
   | Standards: security rules | `consumption_views.known_concerns` |
   | risk-security-compliance: performance constraints, sizing | `project_knowledge` (as w0-t{N}) |
   | application-flow: external system connections | `integrations` |
   | data-model + api-interface-contract: entity and API patterns | `consumption_views.style_guide` |
   | Feature files: file_scope directory patterns | `consumption_views.style_guide` |
   | Manifest: stack_choices | `tech_patterns` (primary entry) |

3. **Generate consumption_views** per § Shared Definitions — Format & Constraints.
   Source material: plan artifacts (design docs, specs, standards, feature files `.md`) and tech_patterns.

4. **Apply § Conciseness Rules.**

5. **Tier 2: NOT created.** No source code exists yet — Tier 2 is created at wave 0 update.

6. **Write marker** per § Seeding Marker Format with `"source": "greenfield"`.

---

## Seeding — Brownfield Import (Invoked by aah-codebase-profile)

Triggered when the codebase-profiler completes T2+T3 analysis on an existing codebase.
Unlike greenfield seeding which reads plan artifacts, brownfield seeding reads
codebase-intel analysis outputs and creates BOTH Tier 1 + Tier 2 immediately
(source code already exists).

### Trigger Condition

- Invoked by `aah-codebase-profile` skill after Step 5f completes
- `.aah/codebase-intel/codebase-learning.md` EXISTS (confirms analysis is done)
- `.aah/build/expertise-seeded.json` does NOT exist

### Steps

> **Codemap freshness precondition (wave update).** The orchestrator's codemap
> gate re-indexes `codemap.db` against the merged wave code *before* it dispatches
> this update, so the `codemap communities` query in step 1 already reflects the
> shipped code. You do NOT refresh it here.

1. **Read source artifacts** (in order):
   1. `.aah/codebase-intel/codebase-learning.md` → Primary source of patterns, conventions, architecture insights
   2. `.aah/codebase-intel/architecture-diagram.md` → Component topology, layer boundaries
   3. `.aah/codebase-intel/dependency-graph.md` → External integrations, coupling points
   4. `.aah/codebase-intel/tech-stack.md` → Tools/frameworks detected
   5. `.aah/manifest.yaml` → project_id, stack_choices (if available)
   6. `codemap communities "$CODEBASE_ROOT" --db-path "$DB_PATH" --json` → Module clustering for Tier 2

2. **Extract into expertise.yaml** per this mapping:

   | From | Into |
   |------|------|
   | codebase-learning.md: architecture insights, system purpose | `consumption_views.quick_context` |
   | codebase-learning.md: observed conventions, naming patterns | `consumption_views.style_guide` |
   | codebase-learning.md: pitfalls, tech debt, risks | `consumption_views.known_concerns` |
   | tech-stack.md: detected frameworks/libraries | `tech_patterns` |
   | dependency-graph.md: external system connections | `integrations` |
   | architecture-diagram.md: key design decisions | `project_knowledge` (as w0-t{N}) |

3. **Generate consumption_views** per § Shared Definitions — Format & Constraints.
   Source material: OBSERVED patterns from T2+T3 analysis (not planned architecture).

4. **Apply § Conciseness Rules.**

5. **Generate Tier 2 domains** (source code exists — immediate creation):
   1. Use `codemap communities` output to identify module clusters
   2. For each community, create `.aah/codebase-intel/domains/{module_name}.yaml`
   3. Read actual source files for accurate line numbers in `source_files` and `flow_traces`
   4. Follow schema in § Tier 2: domains/*.yaml

6. **Write marker** per § Seeding Marker Format with `"source": "brownfield_import"`
   and populated `tier2_domains_created` list.

---

## Tier 1: expertise.yaml (Project-Wide)

Single YAML file with project-wide knowledge. Updated after every wave.

**Field responsibilities:**
- `tech_patterns` = structured tool/library usage facts (machine-readable)
- `integrations` = external system connection notes
- `project_knowledge` = cross-feature insights with evidence tracking
- `consumption_views` = actionable guidance consumed by both lead and implementer agents:
  - `style_guide` = imperative implementation rules (replaces old `conventions`)
  - `known_concerns` = expanded warnings with trigger/symptom/avoidance (replaces old `gotchas`)
  - `quick_context` = project orientation

### Schema

```yaml
version: 1
project_id: "{project_id}"
last_updated: "{ISO timestamp}"
last_updated_after_feature: "{feature_id}"

tech_patterns:
  {pattern_name}:
    tool: "{tool/library}"
    pattern: "{description of how it's used}"
    injection: "{how it's wired in}"

integrations:
  - "{external system integration note}"

project_knowledge:
  - id: "w{wave}-t{topic_number}"
    topic: "{topic-slug}"
    insight: "{one concise sentence}"
    evidence: ["{feature_id}", "{feature_id}"]
    wave_added: {wave_number}
    revised_from: null  # or "wave N" if updated

consumption_views:
  quick_context: |
    {Per § Shared Definitions — Format & Constraints}
  style_guide: |
    {Per § Shared Definitions — Format & Constraints}
  known_concerns: |
    {Per § Shared Definitions — Format & Constraints}
```

### Update Rules

1. Read the feature's QA report, test results, and implementation files
2. Add new implementation rules to `style_guide` (conventions, patterns discovered)
3. Add new warnings to `known_concerns` (traps, workarounds, failure modes)
4. Add tech_patterns for new tools/libraries introduced
5. Update `last_updated` and `last_updated_after_feature`
6. Do NOT remove existing entries — only add or revise

## Tier 2: domains/*.yaml (Per-Module Deep Knowledge)

Per-module YAML files created/updated when a feature touches that module's domain.

### File Format

**CRITICAL: Domain files MUST be YAML (`.yaml`), NOT markdown.**

```yaml
domain: "{domain-name}"
last_updated: "{ISO timestamp}"
last_updated_after_feature: "{feature_id}"

narrative: |
  {2-4 sentence description of what this module/domain does,
  its role in the system, and key design decisions.}

source_files:
  - path: "{relative/path/to/file.py}"
    line_range: "{start}-{end}"
    role: "{what this file does in the domain}"

flow_traces:
  - name: "{flow-name}"
    description: "{what this flow accomplishes}"
    steps:
      - "{file.py}:{line} -> {what happens}"
      - "{file.py}:{line} -> {what happens next}"

integration_points:
  - target: "{other domain or external system}"
    mechanism: "{how they connect (import, API call, event, etc.)}"
    direction: "inbound|outbound|bidirectional"

known_issues:
  - description: "{issue description}"
    severity: "high|medium|low"
    workaround: "{current workaround if any}"

patterns:
  - name: "{pattern-name}"
    description: "{how this pattern is applied in this domain}"
    example_file: "{file.py}:{line}"
```

### Domain File Rules

1. **MUST be YAML** — never generate markdown `.md` files in the domains/ directory
2. **MUST include source_files** with actual file:line references — read source to get real line numbers
3. **MUST include flow_traces** showing execution paths through the code
4. **known_issues MUST have severity** (high/medium/low)
5. **Validate line references** — read the actual file to confirm line numbers are correct
6. **1000-line cap** per domain file — trim lowest-priority content if exceeded
7. When domain file contradicts Tier 1: **update Tier 1** — domain is closer to ground truth
8. Do NOT include: config values, operational commands, test snippets, forward speculation,
   technology stack descriptions, naming conventions (unless domain-specific), or anything
   already captured in Tier 1

---

## Wave Update (Invoked by aah-build — Once Per Wave)

Triggered when the orchestrator returns `update_expertise` action. The build skill
invokes this section after all features in a wave pass QA.

### Trigger Condition

- Orchestrator returns `action: "update_expertise"`
- All features in the wave have passed QA
- Orchestrator provides: `features`, `wave`, `all_files_touched`, `domains_affected`

### Steps

1. **Extract learnings** from all wave features:
   - Read feature summaries / QA reports for each feature
   - Group insights by topic across features (not per-feature)
   - Cross-reference: if same pattern appears in 2+ features -> strong evidence
   - Each topic gets an ID: `w{wave}-t{N}`, each insight is one concise sentence
   - Extract SEMANTIC knowledge only — structural data belongs to codemap.db
   - Do NOT duplicate existing project_knowledge — only capture NEW knowledge
   - If a feature deviated from expected patterns, record WHY

2. **Update Tier 1** (expertise.yaml):
   - Read current expertise.yaml
   - Add new rules to `style_guide` (conventions discovered across ALL features)
   - Add new warnings to `known_concerns` (traps, failure modes, workarounds)
   - Add `tech_patterns` for new tools/libraries introduced
   - Add learnings to `project_knowledge` (2+ feature evidence -> promote; clearly universal single-feature -> add)
   - Update timestamps and `last_updated_after_feature`
   - Apply § Conciseness Rules

3. **Update/Create Tier 2** (domains/*.yaml) for each domain in `domains_affected`:
   - Read existing domain YAML or create new per schema in § Tier 2: domains/*.yaml
   - Read actual source files for accurate line numbers
   - Update `source_files`, `flow_traces`, `integration_points`, `known_issues`, `narrative`
   - Validate all line references against current source
   - **Note:** This runs at EVERY wave including wave 0 — do NOT skip

4. **Update consumption_views** per § Shared Definitions — Format & Constraints:
   - Read current Tier 1 fields + Tier 2 `known_issues`
   - Update ALL THREE views: quick_context, style_guide, known_concerns
   - Keep what's still valid, add new learnings, revise what's stale, remove what's resolved
   - ALL THREE are mandatory — missing any one = incomplete update
   - Write them as YAML fields inside expertise.yaml (NOT as separate files)
   - Merge Tier 2 domain `known_issues` into `known_concerns`

5. **Validate Tier 2 updates** (HARD GATE — do NOT skip):
   - For EACH domain in `domains_affected`, verify its domain file
     (`.aah/codebase-intel/domains/{domain}.yaml`) exists AND has
     `last_updated_after_feature` matching a feature ID from this wave
   - If ANY domain is missing or stale → go back to Step 3 and update
     the missing/stale domain files specifically
   - If `domains_affected` is empty but source files were touched, infer
     domains from directory structure of `all_files_touched` and validate those
   - ALL affected domains must be updated — not just one
   - Do NOT proceed to write marker until every domain passes validation

6. **Write marker** via the wave-marker writer:
   ```bash
   aah run core.build.wave_markers write-expertise \
       --project-path . --wave {N} \
       --summary '{"features": ["F001","F002","F003"], "tier1_changes": ["style_guide","known_concerns"], "tier2_domains_updated": ["config","api","database"], "learnings_extracted": 8, "patterns_promoted": 3}'
   ```
   - The writer captures content hashes of `expertise.yaml` and the
     `domains/*.yaml` directory atomically along with the integration
     branch's HEAD SHA. This is what the orchestrator's gate verifies.
   - **Do NOT write the marker file by hand.** The gate rejects markers
     missing the required fields (`schema_version`, `head_sha`,
     `expertise_yaml_sha256`, `domains_manifest_sha256`).
   - **Run the writer AFTER all artifact updates are complete.** The
     writer hashes whatever it sees on disk; running it before the
     update means the gate will refire on the next `next-action` call.
   - The `--summary` JSON object is embedded verbatim into the marker
     for audit purposes. Pass whatever wave-summary fields are
     meaningful for the team's audit trail.

---

## Tier 2 Generation Timing

| When | Tier 1 | Tier 2 |
|------|--------|--------|
| Greenfield seeding (before wave 0) | Created from plan artifacts | NOT created — no source code yet |
| Brownfield seeding (import) | Created from codebase analysis | **Created** — source code exists |
| Wave 0 expertise update | Updated with wave 0 learnings | **Created** (greenfield) / Updated (brownfield) |
| Wave 1+ expertise update | Updated | Updated |

**CRITICAL:** The `update_expertise` action at wave 0 MUST create Tier 2 domain files for greenfield projects. Source code exists at that point (wave 0 just finished writing it). Only *greenfield seeding* skips Tier 2.
