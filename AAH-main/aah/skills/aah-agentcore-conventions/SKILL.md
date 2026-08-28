---
name: aah-agentcore-conventions
user-invocable: true
description: Architecture-phase activity that onboards AWS Bedrock AgentCore Runtime's platform conventions (protocol contract, action-dispatch payload pattern, file/binary handling, observability) into a dedicated architecture artifact, so plan-phase features are grounded in them automatically. Fires only when the project's backend-compute-platform decision is agentcore-runtime — triggered via the ports/activity registry, not hardcoded into aah-arch.
---

# AAH-AgentCore-Conventions — Platform Contract Capture (Architect phase)

Runs as a **within** activity of `aah-arch`, after `aah-ux`, only when the project's
recorded `backend-compute-platform` decision is `agentcore-runtime` (see
`aah/_resources/_references/_ports/_registry.yaml`, `ACT-BACKEND-PLATFORM-AGENTCORE`).
It is fully autonomous — every input it needs is already a resolved discuss-phase
decision or an existing architecture artifact. It does **not** ask the user anything.

The output is a single architecture artifact, `backend-platform-conventions.md`, that
`aah-module-to-feature` reads (if present) when authoring module features — this is how
AgentCore's runtime contract flows into feature `## Description` / behavioral
expectations without `aah-arch` or `aah-plan` knowing AgentCore exists.

---

## Step 1 — Resolve paths

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
ARCH_DIR="$AAH_DIR/architecture"
```

---

## Step 1.5 — Issue Brief — Look Before You Work — MANDATORY, ALWAYS RUNS

**Always run this step — do NOT skip even if Step 1 errored.**

Invoke the `aah-issue-reporter` agent with: "Brief me on **agentcore conventions**
issues." Read the returned brief; decide and act with your phase context (absorb
feedback → re-entry, note feature status (do not close feature issues), or surface
untriaged issues). For every issue reviewed, post a GitHub comment explaining the
decision made — whether actioned, deferred to a future iteration, or out of scope for
the current run. Do NOT close unless the issue is fully resolved.

---

## Step 2 — Read the platform reference doc in full

Read !`echo "$(aah path)/_resources/_references/build-playbooks/aws-native-services/agentcore-implement.md"`
in full — every numbered rule, not an excerpt. This is the authoritative source for
everything you write in Step 4: the protocol contract (rule 8, port 8080), the
single-endpoint action-dispatch rule (rule 9), the two-tier file/binary rule (rule 10),
the header-auth prohibition (rule 6), the defensive payload-parsing pattern (rule 7),
and the OTEL/observability mandate.

---

## Step 3 — Read grounding context

- **`$AAH_DIR/discuss/decision-registry.yaml`** — the resolved decisions. Specifically:
  - `backend-compute-platform` (confirms `agentcore-runtime` — already true, since this
    activity only fires on that value).
  - the recorded **protocol** decision (`http-request-response` / `ag-ui-streaming` /
    `a2a-agent-to-agent` / `mcp-tool-server`) — this determines WHICH fixed path set
    applies in Step 4 (HTTP/AG-UI use `/invocations`; A2A uses `/`; MCP uses `/mcp`; all
    plus `/ping`). Do not assume HTTP if a different protocol was recorded.
  - the recorded **framework** decision (LangGraph/Strands/ADK/OpenAI), if present — for
    context only, it does not change the conventions.
- **`$ARCH_DIR/module-map.yaml`** — which module owns the backend entrypoint (typically
  MOD-000) — used in Step 4's Architecture-Doc Implications section.
- **`$AAH_DIR/discuss/project-intent.yaml`** — read if present for additional context.

If the protocol decision is not present, ground the Deployment & Runtime Contract
section in the general AgentCore data-plane contract (single `InvokeAgentRuntime`
operation forwarding to one fixed path) and name the per-protocol path sets as
alternatives rather than picking one.

---

## Step 4 — Write `$ARCH_DIR/backend-platform-conventions.md`

Write a **synthesized** document — do not copy the reference doc verbatim, translate
its rules into architecture-level guidance. Use this structure (Project/Date/Tier header
+ Provenance table, matching every other architecture doc's convention):

```markdown
# Backend Platform Conventions — AWS Bedrock AgentCore Runtime

**Project:** {project_name}
**Date:** {date}
**Tier:** {poc | mvp | prod}
**Platform:** AWS Bedrock AgentCore Runtime (decision: backend-compute-platform = agentcore-runtime)

---

## 1. Deployment & Runtime Contract (platform fact)

<!-- Cite AWS docs by name. State the fixed path set for the RECORDED protocol decision
     (Step 3) — do not list all four unless the protocol decision is absent. -->

- AgentCore Runtime's data-plane operation, `InvokeAgentRuntime`, forwards every real
  invocation to exactly ONE fixed path per protocol, plus `/ping` for health:
  - HTTP / AG-UI: `/invocations` (+ `/ws` for AG-UI streaming)
  - A2A: `/` (+ `/.well-known/agent-card.json`)
  - MCP: `/mcp`
- The container MUST listen on port **8080** — never a framework default. This is a hard
  platform requirement, not a convention.

## 2. API / Payload Conventions

<!-- Explicitly separate platform-mandated fact from AAH's own recommended convention. -->

- **Platform fact:** AWS mandates exactly one invokable path (Section 1). There is no
  mechanism to route a second path to the container in production.
- **AAH convention (not an AWS requirement):** organize every additional capability as
  an `action` value dispatched from the SAME request body on that one path, via a
  server-side dispatch table. A feature spec that implies its own REST path (e.g.
  "POST /documents") should be translated to an `action` name, not a literal new route.
- Payload parsing must be defensive: accept `prompt`/`message`/`input` aliases, use
  `extra="allow"` at the top level (never `"forbid"`) — the runtime/CLI envelope may add
  fields.
- No header-based auth — AgentCore strips custom headers before they reach the
  container. Auth is either proxy-edge (SigV4 signing proxy validates before forwarding)
  or payload-based (the client puts the key in the JSON body).

## 3. Files / Binary Content

- Below the signing proxy's practical size ceiling: base64-encode inside the action
  payload (AWS's own documented pattern for images/binary data through
  `InvokeAgentRuntime`, whose own ceiling is 100MB).
- At or above that ceiling: a two-action pattern — one action issues a presigned S3
  upload URL, the client PUTs directly to S3, a second action confirms/processes the
  result. Never multipart through the proxy.

## 4. Observability & Ops Conventions

- OTEL instrumentation is mandatory for the AgentCore entrypoint.

## 5. Architecture-Doc Implications

<!-- Name the specific existing docs/sections these constraints should already reflect,
     e.g. api-interface-contract.md's auth section, module-map.yaml's entry for the
     module owning the backend entrypoint (from Step 3). -->

## Provenance

| Section | Origin | Source |
|---------|--------|--------|
| Deployment & Runtime Contract | Inherited | agentcore-implement.md rules 8-9, backend-compute-platform decision |
| API / Payload Conventions | Inherited | agentcore-implement.md rules 6-7, 9 |
| Files / Binary Content | Inherited | agentcore-implement.md rule 10 |
| Observability & Ops Conventions | Inherited | agentcore-implement.md OTEL mandate |
| Architecture-Doc Implications | Authored | arch-phase |
```

Fill every `{...}` placeholder and every `<!-- -->`-guided section with real content
grounded in Steps 2-3 — do not leave template scaffolding in the written file.

---

## Step 5 — Register + Commit

Mirror `aah-ux`'s own Step 5:

```bash
aah run core.common.manifest add-artifact "architecture/backend-platform-conventions.md"
git add "$ARCH_DIR/backend-platform-conventions.md" && \
  git commit -m "architect(aah-agentcore-conventions): capture AgentCore platform conventions"
git push origin HEAD
```

**Comment back to GitHub issue (if version_control enabled).** If this run was
triggered by a GitHub issue surfaced at Step 1.5, run:
```bash
aah run core.version_control.cli status
```
If enabled, and the commit + push above completed successfully, post a resolution
comment and close the issue:
```bash
aah run core.version_control.cli comment --issue <N> --body "AgentCore platform conventions updated based on this issue: <brief summary>. Changes committed."
gh issue close <N>
```
Where `<N>` is the issue number from the Step 1.5 brief that triggered this rework. If
the feedback did not come from a GitHub issue, skip this step. If the commit or push
failed, do NOT close the issue. If version_control is disabled or offline, skip
silently.

Return control to `aah-arch`, which marks the port completed via its existing generic
port-completion flow.

---

## Guardrails

- Ground every claim in the reference doc read in Step 2 — never invent a contract
  detail it doesn't state.
- Keep platform-mandated fact and AAH-recommended convention clearly distinguished —
  never blur the two into one undifferentiated rule list.
- This skill is AgentCore-specific by design — a future platform gets its own
  copy-adapted skill (own reference doc, same output filename), not a branch inside
  this one.
