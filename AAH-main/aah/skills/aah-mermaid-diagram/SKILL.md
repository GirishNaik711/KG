---
name: aah-mermaid-diagram
argument-hint: "[diagram-type] [subject] [--context path] [--output path] [--fence-only]"
description: >
  Generate professional Mermaid diagrams for architecture, workflows, data models,
  sequences, and state machines. Works standalone or within a AAH project. Use
  this skill whenever the user wants to create a diagram, visualize a flow, draw
  architecture, create an ERD, show a sequence, map dependencies, or produce any
  Mermaid-based visualization. Triggers on: "create diagram", "draw architecture",
  "visualize flow", "mermaid diagram", "sequence diagram", "state diagram", "ERD",
  "data flow", "dependency graph", "C4 diagram", "flowchart", or any request to
  visually represent system structure or behavior.
user-invocable: true
disable-model-invocation: false
---

# Mermaid Diagram Generator

Generate context-aware Mermaid diagrams. Reads context from any provided source and
writes diagrams to a configurable output path.

## Supported Diagram Types

| Type | Keyword | Mermaid Syntax | When Generated |
|------|---------|---------------|----------------|
| Architecture | `architecture` | `graph TB` with subgraphs | Always |
| Data Model | `data-model` | `erDiagram` | Always |
| Data Flow | `data-flow` | `flowchart LR` | Always |
| Sequence | `sequence` | `sequenceDiagram` | When context shows integrations or multi-service interactions |
| State Machine | `state` | `stateDiagram-v2` | When context has entities with lifecycle transitions |
| Dependency | `dependency` | `graph TD` | Complex systems with multiple modules |
| C4 Context | `c4-context` | `C4Context` | Enterprise or microservice architectures |
| C4 Container | `c4-container` | `C4Container` | Enterprise or microservice architectures |
| Workflow | `workflow` | `flowchart TD` | Business process automation projects |
| Class | `class` | `classDiagram` | When OOP domain modeling is relevant |

---

## Workflow

### Step 0: Resolve Context

```bash
PROJECT_DIR=$(aah run core.common.config project-path 2>/dev/null)
```

If inside a AAH project:
```bash
AAH_DIR="$PROJECT_DIR/.aah"
aah run core.common.manifest read --path "$AAH_DIR/manifest.yaml"
```

If not in a AAH project, operate standalone using only `$ARGUMENTS` for context.

### Step 1: Parse Arguments

`$ARGUMENTS` supports:
- **diagram-type** — keyword from supported types table (e.g., `architecture`, `sequence`)
- **subject** — what to diagram (e.g., "payment service", "order lifecycle")
- **--output <path>** — directory to write diagrams to (default: `$AAH_DIR/diagrams/` if in AAH, else current directory)
- **--context <path>** — file or directory to read for context (e.g., a spec, a codebase, an artifact folder)
- **--fence-only** — emit ONLY the raw ```mermaid fence to stdout, no file write, no wrapping prose/tables. Used by upstream skills (e.g. `aah-arch`) that embed the fence into a larger doc they own.

Examples:
- `/aah-mermaid-diagram architecture "payment service" --context ./docs/design.md`
- `/aah-mermaid-diagram batch --context $AAH_DIR/ --output $AAH_DIR/diagrams/`
- `/aah-mermaid-diagram sequence --context ./src/api/`
- `/aah-mermaid-diagram data-model`
- `/aah-mermaid-diagram state "order lifecycle" --output ./diagrams/`
- `/aah-mermaid-diagram sequence "MOD-000 happy path" --context $ARCH_DIR/module-map.yaml --fence-only`

If diagram type or subject is missing, use `AskUserQuestion` to ask:
- "What type of diagram?" (with options from the supported types table)
- "What should the diagram represent?" (free text)

**--fence-only mode.** When this flag is set, Steps 3-7 are collapsed into: (a) generate the fence per Step 4's rules (templates + 7 syntax rules); (b) print ONLY the fence (opening ```mermaid, body, closing ```) to stdout — nothing else, no headers, no notes, no commit, no temp-file gating. The caller (e.g. `aah-arch`) captures stdout and splices it into its own doc; the caller's phase gate (e.g. aah-arch Step 5d) validates the assembled docs.

### Step 2: Gather Context

Read context based on what's available:

1. **If `--context` was provided**: read the specified file(s) or directory contents
2. **If inside a AAH project and no `--context`**: read from `$AAH_DIR/` (current phase artifacts)
3. **If standalone and no `--context`**: use only the subject description from arguments

Also read the template library:
```bash
!`cat "$(aah path)/_resources/_references/mermaid/diagram-templates.md"`
```

The context informs diagram content — component names, technologies, relationships, data entities, integrations, and domain concepts.

### Step 3: Determine Which Diagrams to Generate

If `$ARGUMENTS` specifies a diagram type keyword, generate only that type.

If `$ARGUMENTS` is "batch" or "all", determine the set based on available context:

**Always generated (standard set):**
1. `architecture` — system architecture
2. `data-model` — entity relationship diagram
3. `data-flow` — data movement through the system

**Conditional generation rules (inferred from context):**

| Condition | Additional Diagrams |
|---|---|
| Context mentions multiple services, APIs, or external system integrations | `sequence` |
| Context has entities with status transitions (orders, claims, workflows, approvals) | `state` |
| Context shows a complex system with many modules/packages | `dependency` |
| Context describes microservices or distributed architecture | `c4-context` + `c4-container` |
| Context involves process automation or decision logic | `workflow` |
| Context is OOP-heavy with complex domain modeling | `class` |

Use `AskUserQuestion` to confirm the diagram set with the user before generating:
- Show which diagrams will be generated and why
- Allow the user to add or remove from the set

### Step 4: Generate Each Diagram

For each diagram in the determined set, generate using templates from `$(aah path)/_resources/_references/mermaid/diagram-templates.md`.

**Generation rules:**
1. Use the correct Mermaid syntax for the diagram type
2. Label nodes with logical name AND technology: `API[REST API<br/>FastAPI]`
3. Use subgraphs for architectural boundaries (layers, domains, external systems)
4. Show directionality — arrows indicate data flow or dependency direction
5. Keep diagrams readable — max 20-30 nodes per diagram
6. Use context-specific names (not generic placeholders)
7. Apply consistent styling using `classDef` and `style` directives

**Output format for each diagram:**

```markdown
# {Diagram Title}

## Overview
{1-2 sentence description of what this diagram shows}

## Diagram

```mermaid
{generated mermaid code}
`` `

## Components
| Component | Role | Technology |
|-----------|------|------------|
| ... | ... | ... |

## Notes
- {Assumptions or areas needing refinement}
```

### Step 5: Write Output

Determine output directory:
1. If `--output <path>` was provided, use it
2. If `--output <path>` was not provided, use `$AAH_DIR/diagrams/`
3. Otherwise, write to current working directory

```bash
mkdir -p "$OUTPUT_DIR"
```

Write each diagram to: `$OUTPUT_DIR/{subject}-{type}.md`

Naming convention (matches `.aah/codebase-intel/` when used in AAH context):
- `architecture-diagram.md`
- `data-model-diagram.md`
- `data-flow-diagram.md`
- `dependency-graph.md`
- `integration-sequence.md`
- `{entity}-state.md` (e.g., `order-state.md`)
- `system-c4-context.md`
- `system-c4-container.md`
- `{process}-workflow.md`
- `domain-class.md`

If inside a AAH project, register each artifact:
```bash
aah run core.common.manifest add-artifact "analysis/diagrams/{filename}"
```

### Step 6: Validate

**Syntax gate (deterministic).** If the output directory is `.aah/architecture/` (i.e. this skill is running inside an AAH project's architecture phase), run the validator:

```bash
aah run core.gates.validate_mermaid --project-path "$PROJECT_DIR"
```

Non-zero exit prints `<file> (block #N): line <L>: <reason>` for each violation. YOU fix each per the "Syntax Rules — Prevent Silent Failures" section of `$(aah path)/_resources/_references/mermaid/diagram-templates.md`, keep any `.md`/`.html` pair in sync, and re-run. Up to 3 attempts; if still failing, surface remaining issues via `AskUserQuestion`. Do NOT write output with a failing gate.

For output paths outside `.aah/architecture/` (standalone use, custom `--output`), the gate is a no-op — still apply the 7 rules from `$(aah path)/_resources/_references/mermaid/diagram-templates.md` when authoring the fence.

**Additional structural checks (apply to every diagram, gate or no gate):**
- All opened `subgraph` blocks have matching `end` statements
- Node IDs are unique within the diagram
- Arrow syntax is valid (`-->`, `-.->`, `==>`, `-->>`)
- No orphan nodes (every node has at least one connection)
- Bracket pairs are balanced (`[]`, `()`, `{}`, `[()]`, `[[]]`)
- Total node count is within readable range (warn if > 30)

If validation fails, fix and regenerate.

### Step 7: Commit and Report

If inside a AAH project:
```bash
cd "$PROJECT_DIR"
git add "$OUTPUT_DIR"
git commit -m "feat(aah): generate mermaid diagrams"
```

Present summary to user:
- List of diagrams generated with file paths
- Brief description of each
- Note: "Preview in any Mermaid renderer (GitHub, VS Code Mermaid extension, mermaid.live)"

---

## Diagram Update

This skill supports both **create** and **update** operations:

- **Create**: generates new diagrams from context (default behavior)
- **Update**: modifies existing diagrams when context changes or user requests refinements

When updating:
1. Read the existing diagram file from the output directory
2. Compare with current context to identify what changed (new components, removed services, renamed entities, updated relationships)
3. Apply changes while preserving the overall diagram structure and styling
4. Re-validate the updated diagram
5. Overwrite the file with the updated version
6. Show a summary of what changed (added/removed/modified nodes and edges)

Update is triggered when:
- The user explicitly asks to modify, update, or refine a diagram
- The `--output` path already contains a diagram of the same type (skill detects existing file and asks whether to overwrite or update)
- Architecture decisions change during later phases and diagrams need to reflect the new state
