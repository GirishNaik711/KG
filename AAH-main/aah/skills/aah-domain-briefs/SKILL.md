---
name: aah-domain-briefs
argument-hint: "[add|edit|remove|validate|reindex|status] [node-id]"
description: >
  Manage the AAH industry-domain taxonomy — add, edit, or remove Domain Briefs,
  validate the taxonomy, reindex from filesystem, or show the domain attached to
  the current project. Use this skill whenever the user wants to add a new industry,
  sub-industry, process area, or process step; edit an existing Domain Brief; remove
  a node; or check which industry domain is attached to a project. Triggers include:
  "add industry", "add domain brief", "new process area", "edit domain brief",
  "remove domain node", "validate domain briefs", "reindex domain taxonomy", or
  "what industry domain is this project".
user-invocable: true
disable-model-invocation: false
---

# AAH Domain Brief Manager

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text, markdown, or numbered lists. Use the `AskUserQuestion` tool with properly structured options.

## What this skill manages

The industry-domain taxonomy at `domain-briefs/` in the repo root. Each node is one
Domain Brief — curated functional/industry knowledge (processes, data entities,
glossary, regulations, KPIs, patterns) that attaches to an AAH project to inform
research and analysis phases.

Structure: industry (L1) → sub-industry (L2) → process area (L3) → process step (L4).

Schema: `domain-briefs/BRIEF_SCHEMA.yaml`
Registry: `domain-briefs/_taxonomy.yaml`

## Steps

### 0. Parse Argument

Resolve the operation from `$ARGUMENTS`. Supported operations:

| Operation | What it does |
|-----------|--------------|
| `add` | Create a new Domain Brief via a guided full-schema interview |
| `edit <id>` | Modify an existing Domain Brief (launch interview against current content) |
| `remove <id>` | Remove a node (errors if it has children unless `--force`) |
| `validate` | Run schema + taxonomy integrity checks |
| `reindex` | Rebuild `_taxonomy.yaml` from domain-brief.yaml files on disk |
| `status` | Show the industry domain attached to the current project |

If `$ARGUMENTS` is empty, use `AskUserQuestion` to ask which operation.

### 1. Add a Domain Brief (full schema walkthrough)

#### 1a. Determine Level

Use `AskUserQuestion`:
"Which level is this new Domain Brief?"
Choices:
- "Industry (L1) — top-level root (e.g., financial-services, healthcare)"
- "Sub-industry (L2) — under an industry (e.g., commercial-banking)"
- "Process area (L3) — under a sub-industry (e.g., commercial-client-onboarding)"
- "Process step (L4) — under a process area (e.g., kyb-due-diligence)"

#### 1b. Choose Parent (for L2-L4)

List existing nodes at the parent level:
```bash
aah run core.domain_briefs.loader list-all
```

Use `AskUserQuestion` to pick the parent from the candidates at the required level,
OR choose "Other" to type a custom parent id.

#### 1c. Core Fields

Gather via `AskUserQuestion` / free-text prompts:
- `id` — slash-separated path. For L1: just `<slug>`. For L2+: `<parent-id>/<slug>`.
- `name` — human-readable name
- `description` — one-paragraph scope summary

#### 1d. Keywords

Ask for **strong keywords** (high-weight) — list 4-8 short phrases that should
auto-detect this node.

Ask for **moderate keywords** (supporting) — list 4-10 additional phrases.

#### 1e. Typical Horizontals

Ask which technology-domain horizontals (project_types) typically pair with
this industry domain. Show the list from `activity-library/_registry.yaml`:
- ai-infra-platforms
- ai-applications
- data-pipelines
- data-modernization
- llm-ops
- ml-ops
- cloud-modernization
- security-updates

Use `AskUserQuestion` with `multiSelect: true`.

#### 1f. Sample Problem Statements

Ask for 2-5 example problem statements that should classify to this node.
These are used for detector calibration.

#### 1g. Reference Frameworks

Ask for industry-standard frameworks relevant to this node (name + optional URL).
Example for financial-services: BIAN, FATF Recommendations, Basel III.

#### 1h. Processes

Walk through business processes within the node's scope. For each, capture:
- `name`
- `steps` (ordered list of steps)

#### 1i. Data Entities

For each core domain entity, capture:
- `name`
- `attributes` (list of attribute names with types/notes)

#### 1j. Glossary

Capture domain-specific terms and definitions. Include acronyms and jargon.

#### 1k. Typical Integrations

List external systems commonly integrated (e.g., "Corporate registries (D&B, Bureau van Dijk)").

#### 1l. Regulations

For each regulation, capture:
- `name`
- `impact` — how this regulation shapes architecture/design decisions

#### 1m. KPIs

Common success metrics for systems built in this space.

#### 1n. Common Patterns

Functional/business patterns for this domain. For each:
- `name`
- `description`
- `when_to_apply`
- `anti_patterns` (list of mistakes to avoid)

#### 1o. Anti-Patterns

Domain-level mistakes teams get wrong repeatedly.

#### 1p. Write the Brief

Assemble the collected data as JSON, pipe to the manager's add command:

```bash
echo '<collected JSON>' | aah run core.domain_briefs.manager add \
  --id "<chosen-id>" \
  --name "<name>" \
  --description "<description>"
```

The manager writes `domain-briefs/<id>/domain-brief.yaml` and updates `_taxonomy.yaml`.

#### 1q. Validate

```bash
aah run core.domain_briefs.validate
```

Exit 0 = clean. Fix any errors before committing.

#### 1r. Commit

```bash
git add domain-briefs/
git commit -m "feat(domain-briefs): add <id>"
```

### 2. Edit a Domain Brief

1. Read current content: `cat domain-briefs/<id>/domain-brief.yaml`
2. Walk through the same sections as Add, pre-filling current values
3. Use `AskUserQuestion` for each section: "Keep current? / Edit / Skip"
4. Write the updated YAML
5. Run validate
6. Commit

### 3. Remove a Domain Brief

```bash
aah run core.domain_briefs.manager remove --id "<id>" [--force]
```

Fails with a clear error if the node has children. Use `--force` to cascade.

Commit:
```bash
git add domain-briefs/
git commit -m "chore(domain-briefs): remove <id>"
```

### 4. Validate

```bash
aah run core.domain_briefs.validate
```

Checks:
- `_taxonomy.yaml` references files that exist
- Every file on disk is indexed
- Each brief declares `id`, `parent_id`, `level`, `name`, `description`
- `parent_id` matches taxonomy parent
- `level` matches path depth
- Strong-keyword uniqueness across leaves (warning if shared)

Exit 0 on success, 1 on errors.

### 5. Reindex

Rebuild `_taxonomy.yaml` from filesystem contents. Useful after manual YAML edits
where you added/removed nodes without using the manager.

```bash
aah run core.domain_briefs.manager reindex
```

### 6. Status (current project's attached domain)

```bash
aah run core.domain_briefs.status show --project-path "$PROJECT_DIR"
```

Prints the attached `industry_domain_path`, the resolved Domain Brief's name,
the current phase (to explain whether `## Domain Intelligence` injection is
active), and the rendered summary markdown that SessionStart would inject.

## Rules — MANDATORY

- NEVER bypass the validate step before committing
- NEVER remove a node with children unless the user explicitly passes `--force`
- ALWAYS ask the user to confirm the full node id before writing
- ALWAYS run reindex if a node was moved or renamed manually
- NEVER populate dummy/placeholder content that the user didn't provide — stub nodes are acceptable but mark them as such
