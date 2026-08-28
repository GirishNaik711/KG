---
name: data-assist-execute
description: Execute the Data Assist plugin pipeline — activates plugin agents, generates prompts.md from intake, and hands off to lead orchestrator
argument-hint: "[resume]"
user-invocable: true
disable-model-invocation: true
---

# Data Assist Plugin — Execution Entry Point

This skill bridges AAH and the Data Assist plugin. It activates plugin agents
via symlinks, translates AAH intake data into the toolkit's `prompts.md` format,
and hands off to the `data-assist-lead-orchestrator` agent for autonomous execution.

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.**

## Steps

### 0. Resolve Active Project

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
PLUGIN_ROOT="$(aah run core.common.config framework-path)/plugins/data_assist_plugin"
```

### 1. Verify Delegation

Read the manifest and confirm this project is delegated:

```bash
aah run core.common.manifest read --path "$AAH_DIR/manifest.yaml"
```

If `execution_mode` is NOT `"delegated"` or `delegated_to` is NOT `"data-assist"`:
- Tell the user: "This project is not delegated to the Data Assist plugin. Use `/aah-discuss` to begin."
- **STOP.**

### 2. Check Resume vs First Run

Check if the plugin has already been activated for this project:

```bash
test -f "$PROJECT_DIR/.data-assist/prompts.md" && echo "RESUME" || echo "FIRST_RUN"
```

- If `RESUME`: skip to Step 5 (Hand Off).
- If `FIRST_RUN`: continue to Step 3.

### 3. Activate Plugin (First Run Only)

#### 3a. Create Symlinks for Agents

Create symlinks from `.claude/agents/` to plugin agent definitions. This makes them discoverable by Claude Code's Agent Teams.

```bash
FRAMEWORK_ROOT=$(aah run core.common.config framework-path)

# Create agent symlinks
for agent_file in "$FRAMEWORK_ROOT/plugins/data_assist_plugin/agents"/*.md; do
  agent_name=$(basename "$agent_file")
  link_path="$FRAMEWORK_ROOT/.claude/agents/data-assist-${agent_name}"
  if [ ! -L "$link_path" ]; then
    ln -sf "../../plugins/data_assist_plugin/agents/${agent_name}" "$link_path"
  fi
done

# Create skill symlinks (each skill needs a directory with SKILL.md)
for skill_file in "$FRAMEWORK_ROOT/plugins/data_assist_plugin/skills"/*.md; do
  skill_name=$(basename "$skill_file" .md)
  skill_dir="$FRAMEWORK_ROOT/.claude/skills/data-assist-${skill_name}"
  mkdir -p "$skill_dir"
  if [ ! -L "$skill_dir/SKILL.md" ]; then
    ln -sf "../../../plugins/data_assist_plugin/skills/${skill_name}.md" "$skill_dir/SKILL.md"
  fi
done

# Create rules symlink
mkdir -p "$FRAMEWORK_ROOT/.claude/rules"
if [ ! -L "$FRAMEWORK_ROOT/.claude/rules/data-assist-escalation.md" ]; then
  ln -sf "../../plugins/data_assist_plugin/rules/escalation.md" "$FRAMEWORK_ROOT/.claude/rules/data-assist-escalation.md"
fi

echo "Plugin agents and skills activated"
```

#### 3b. Create Toolkit Directory Structure

```bash
mkdir -p "$PROJECT_DIR"/.data-assist/{knowledgebase,inputs,research,plans,scripts,output,testscripts,thoughts/logs}
```

#### 3c. Copy GUARDRAILS.yaml

```bash
cp "$PLUGIN_ROOT/GUARDRAILS.yaml" "$PROJECT_DIR/.data-assist/GUARDRAILS.yaml"
```

#### 3d. Collect Source Materials — MANDATORY, NEVER SKIP

**3d.1. Scan and display contents:**

```bash
find "$PROJECT_DIR/.data-assist/knowledgebase" -type f -printf '  %P\n' 2>/dev/null | sort
find "$PROJECT_DIR/.data-assist/inputs" -type f -printf '  %P\n' 2>/dev/null | sort
```

Display results to the user showing what exists in each folder.

**3d.2. Ask user based on state:**

- If BOTH folders are **empty**: Use `AskUserQuestion` — "Both `knowledgebase/` and `inputs/` are empty. Please add files and confirm, or choose to proceed empty." Options: "I've added files — check again", "Proceed with empty folders"
- If folders are **partially populated**: Use `AskUserQuestion` — "Found files in knowledgebase/ and/or inputs/ (shown above). Do you want to add more?" Options: "I've added more — check again", "Proceed with current files"

**3d.3. On "check again": re-run the scan from step 1, display updated file list, then ask again with `AskUserQuestion`:** "Here's the updated inventory. Ready to proceed?" Options: "Looks good — proceed", "I've added more — check again"

Repeat until the user selects a "proceed" option.

**3d.4. This step must execute on EVERY first run regardless of folder state.** Even if folders are pre-populated, display their contents and get user confirmation before continuing.

### 4. Generate prompts.md from AAH Intake

Read the AAH intake data and translate it into the toolkit's `prompts.md` format.

```bash
aah run core.common.manifest read --path "$AAH_DIR/manifest.yaml"
```

Also read intake data:
```bash
cat "$AAH_DIR/intake.json"
```

Now generate `prompts.md` at `$PROJECT_DIR/.data-assist/prompts.md` using the Write tool. The file must follow the toolkit's format:

```markdown
# Project Prompt

## Objective
{problem_statement from intake.json}

## Solution Type
{Infer from intake: "reverse engineering" / "forward engineering" / "data modeling" / "migration" / "code generation" / etc.}

## Source Materials
{List all knowledgebase/ files with descriptions. If none yet, write:
"To be populated — place reference materials in knowledgebase/ directory."}

## Target Deliverables
{Infer from intake Q&A what the user expects in output/. Examples:
- "Python scripts that transform source data"
- "DDL files for target platform"
- "Mapping documents"}

## Technical Requirements
{Extract from intake Q&A:
- Target platform (e.g., Snowflake, Databricks, PostgreSQL)
- Source platform (e.g., Teradata, Oracle, SQL Server)
- Coding conventions
- Any constraints mentioned}

## Component Structure
{Infer logical components from the problem. Examples:
- "parser — reads source DDL files"
- "generator — produces target DDL"
- "validator — compares source vs target"}

## Quality Requirements
{Extract from intake or use defaults:
- "All scripts must be idempotent"
- "Output must be deterministic (same input → same output)"
- "Scripts must handle edge cases gracefully"}

## Success Criteria
{From intake Q&A or infer:
- "All source objects are accounted for in output"
- "Generated scripts run without errors"
- "Output matches expected format"}
```

**Be thorough in the translation.** Extract every relevant detail from the AAH intake rounds to populate prompts.md richly. The toolkit agents rely entirely on this file to understand what to build.

### 4a. Checkpoint: Commit Plugin Activation

```bash
cd "$PROJECT_DIR"
git add .data-assist/prompts.md .data-assist/GUARDRAILS.yaml
git commit -m "chore(data-assist): activate Data Assist plugin and generate prompts.md"
```

### 5. Hand Off to Lead Orchestrator

Use `AskUserQuestion`:
- Header: "Data Assist Plugin Ready"
- Question: "The Data Assist plugin is activated. The lead orchestrator will read prompts.md and begin the 4-phase pipeline (Research → Plan → Build → Review). Ready to start?"
- Options: ["Start pipeline", "Edit prompts.md first", "Review setup"]

If "Edit prompts.md first": Tell user to edit `$PROJECT_DIR/.data-assist/prompts.md` and run `/data-assist-execute resume` when ready.

If "Review setup": Show the contents of prompts.md and GUARDRAILS.yaml, then ask again.

If "Start pipeline":

```
Agent(
  subagent_type: "data-assist-lead-orchestrator",
  name: "data-assist-orchestrator",
  description: "Data Assist lead orchestrator",
  prompt: "You are the lead orchestrator for the Data Assist pipeline.

    PROJECT_DIR: {PROJECT_DIR}
    DATA_ASSIST_DIR: {PROJECT_DIR}/.data-assist

    All data-assist artifacts live under {PROJECT_DIR}/.data-assist/.
    Read prompts.md at {PROJECT_DIR}/.data-assist/prompts.md to understand the project requirements.
    Read GUARDRAILS.yaml at {PROJECT_DIR}/.data-assist/GUARDRAILS.yaml for quality standards.
    All folder references (knowledgebase/, research/, plans/, scripts/, output/,
    testscripts/, thoughts/) are relative to {PROJECT_DIR}/.data-assist/.

    Follow your standard 4-phase workflow:
    1. Phase 1 (Research): Create research tasks for the data-assist-researcher agent
    2. Phase 2 (Planning): Create planning tasks for the data-assist-analyzer agent
    3. Phase 3 (Build): Create implementation tasks for the data-assist-builder agent
    4. Phase 4 (Review): Create review tasks for the data-assist-reviewer agent

    Use TaskCreate/TaskUpdate to coordinate all agents via the shared task list.
    Set proper dependencies between tasks so phases execute in order.
    Consult data-assist-expert-* agents when domain expertise is needed.
    The data-assist-devils-advocate agent must challenge plans and scripts — FAIL is a hard block.

    Begin by reading prompts.md and creating Phase 1 research tasks.",
  mode: "default"
)
```

After the orchestrator completes (or the user stops it), commit any generated artifacts:

```bash
cd "$DATA_ASSIST_DIR"
git add research/ plans/ scripts/ output/ testscripts/ thoughts/
git commit -m "chore(data-assist): commit Data Assist pipeline artifacts"
```

## Rules

- NEVER bypass the symlink activation step — agents must be discoverable
- NEVER modify AAH core phase state (manifest phase, feature-list, etc.) during plugin execution
- ALWAYS commit artifacts after pipeline completion
- If the user asks to switch back to AAH, create a new project — delegation is a one-way door
