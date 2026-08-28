# Agent Topology

**Project:** {project_name}
**Date:** {date}
**Tier:** {poc | mvp | prod}

<!-- THE consolidated agent document — ALL agent concerns live here.
     Only produced if the project has agentic components.
     One doc covers all agentic modules. Repeat sections §1–§7 below per
     agentic module, under a `## {module_id} — {module_name}` heading for each. -->

---

## 1. Agent Roster

| Agent | Role | LLM | Autonomy Level | Fallback |
|-------|------|-----|---------------|----------|
| | | | Full / Supervised / Restricted | |

### Agent Specifications

#### {agent_name}

- **Purpose:** {one sentence}
- **Input:** {what triggers this agent}
- **Output:** {what it produces}
- **Constraints:** {what it must NOT do}

---

## 2. Orchestration

### Pattern

<!-- Sequential / Parallel / Hierarchical / Dynamic routing / Hybrid -->

### Flow Diagram

```
{agent interaction flow — ASCII or Mermaid}
```

### Handoff Protocol

<!-- How agents pass work to each other. What context is transferred. -->

| From | To | Trigger | Context Passed | Format |
|------|-----|---------|----------------|--------|
| | | | | |

---

## 3. Memory & State

<!-- Short-term vs long-term. Derived from gap G04 if applicable. -->

| State Type | Scope | Storage | TTL | Consistency |
|-----------|-------|---------|-----|-------------|
| Conversation | Session | | | |
| Agent memory | Persistent | | | |
| Shared context | Cross-agent | | | |

### Context Window Management

<!-- Strategy for managing token limits across agent chains -->

---

## 4. Tool & MCP Surface

<!-- Tool catalog with params, returns, side-effects. MCP servers if applicable. -->

| Tool | Purpose | Agent(s) | Side Effects | Rate Limit |
|------|---------|----------|-------------|-----------|
| | | | | |

### MCP Servers (if applicable)

| Server | Tools Exposed | Access Control |
|--------|--------------|----------------|
| | | |

### Tool-Access Matrix

| Agent | Tools Available | Tools Denied | Rationale |
|-------|---------------|-------------|-----------|
| | | | |

---

## 5. Prompt / Context Architecture

<!-- System prompts, context assembly strategy, token budget allocation. -->

| Agent | System Prompt Strategy | Context Sources | Token Budget |
|-------|----------------------|-----------------|-------------|
| | | | |

---

## 6. Guardrails

<!-- Scope enforcement, output validation, hallucination control. -->

| Guardrail | Type | Agent(s) | Action on Violation |
|-----------|------|----------|---------------------|
| | input / output / scope | | block / warn / escalate |

### Human-in-the-Loop Points

| Agent | Trigger | What User Decides | Timeout Behavior |
|-------|---------|-------------------|-----------------|
| | | | |

---

## 6a. A2A — Agent-to-Agent Protocol

<!-- A2A is Google's open protocol for agents to discover and collaborate with each
     other over HTTP via Agent Cards, Tasks, and streaming — complementary to MCP
     (MCP = agent-to-tools, A2A = agent-to-agent).

     ONLY include this section if agents are deployed as independent discoverable
     services or need cross-framework interop. SKIP if agents are co-located in a
     single orchestrator (e.g. LangGraph chain, CrewAI crew). -->

| Contract | Producer Agent | Consumer Agent | Format | SLA |
|----------|---------------|----------------|--------|-----|
| | | | | |

---

## 7. Eval Hooks

<!-- Delegate to /aah-evals. Define what gets evaluated and when. -->

| Eval | Agent | Trigger | Metric | Threshold |
|------|-------|---------|--------|-----------|
| | | | | |

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| Agent Roster | {Inherited / Authored} | {e.g. decision-registry slug} |
| Orchestration | {Inherited / Authored} | {e.g. decision-registry slug} |
| Memory & State | {Inherited / Authored} | {source or arch-phase} |
| Tool & MCP Surface | {Inherited / Authored} | {source or arch-phase} |
| Prompt / Context Architecture | {Inherited / Authored} | {source or arch-phase} |
| Guardrails | {Inherited / Authored} | {source or arch-phase} |
| A2A — Agent-to-Agent Protocol | {Inherited / Authored} | {source or arch-phase} |
| Eval Hooks | {Inherited / Authored} | {source or arch-phase} |
