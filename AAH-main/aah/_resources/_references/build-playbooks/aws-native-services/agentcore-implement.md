---
name: agentcore-implement
description: >
  Create or adapt an agent for AWS Bedrock AgentCore Runtime.
  Scaffold mode uses `agentcore create` to generate a full working project (code + config).
  Adapt mode adds the protocol contract to existing code.
  Handles all protocols (HTTP, AGUI, MCP, A2A) x all frameworks (LangGraph, Strands,
  Google ADK, OpenAI Agents). Output is immediately testable with `agentcore dev`.
user-invocable: true
disable-model-invocation: false
---

# AgentCore Implement — Create or Adapt Agent for Bedrock AgentCore Runtime

## Harness integration (read first)

This is a **reference skill** the `aah-feature-implementer` subagent consults during the AAH
build (implement) phase when the project targets AgentCore (deploy route `agentcore`). It
establishes the agent's **protocol contract** and `agentcore/` config so the deploy phase
(`/aah-deploy` → `aah-agentcore-deploy-engineer`) can ship it.

- **Reuse the scaffold helper for the standardized CLI.** The deterministic steps —
  `agentcore create`, moving the CLI's nested output to the project root, fixing
  `agentcore.json`, and `agentcore validate` — are owned by
  `aah run core.implement.agentcore_scaffold {scaffold|restructure|fix-config|validate}`.
  Call it instead of typing raw `agentcore create` blocks. The **guidance** in this skill
  (AG-UI SSE layer, A2A adapters, multi-agent wiring, framework notes, adapt-mode contract
  injection) is what you apply by hand.

### Two modes: AAH (default) vs Standalone

**AAH mode — the `aah-feature-implementer` runs this unattended, one feature at a time, in a
git worktree.** It is NOT interactive. Follow these rules:

1. **Inputs come from prior phases, not from asking.** Resolve every "question" below from
   recorded artifacts (see the table in Step 1). Do NOT run `AskUserQuestion` in AAH mode
   — the worktree run is autonomous.

   | Input | AAH source |
   |---|---|
   | Framework (LangGraph/Strands/ADK/OpenAI) | the recorded **framework** decision in the decisions registry |
   | Protocol (HTTP / AG-UI / A2A / MCP) | the recorded **protocol** decision in the decisions registry (`http-request-response` / `ag-ui-streaming` / `a2a-agent-to-agent` / `mcp-tool-server`) → maps 1:1 to the `--protocol` flag |
   | Single vs multi-agent + **agent roster** | the recorded **single-vs-multi-agent (topology)** decision in the decisions registry for the shape; the **feature set** (`.aah/plan/features/*.yaml`) for the roster — each agent/specialist maps to its feature(s). *(`module-map.yaml` + Agent Topology doc also carry this.)* |
   | Model id | the project's model decision (do NOT hardcode) |
   | Per-agent logic / tools / nodes | the current **feature YAML** (`description`, `acceptance_criteria`, `test_cases`, `agent_node`) |

2. **Scaffolding is the FIRST feature (F000-style), not a whole-app pass.** The scaffold
   feature runs the scaffold helper once to establish the protocol contract + `agentcore/`
   config; each **subsequent feature** adds a node/tool/specialist/protocol layer. Mirror the
   "CLOUDLESS LANGGRAPH CONVENTIONS" / AgentCore conventions in `aah-feature-implementer.md` — do
   NOT generate the entire multi-agent system in one shot; the DAG/waves drive it.

3. **Completion = passing tests, not `agentcore dev`.** A feature is done when its NO-MOCKS
   functional tests pass against its `acceptance_criteria` (aah-feature-implementer Steps 6–8 +
   `testing-standards`). `agentcore dev` / `agentcore validate` are optional local smoke
   checks, not the completion bar.

4. **The recorded protocol decision fixes the PRIMARY protocol only. ADDITIONAL runtimes are FEATURES, not a
   decision.** An agent may expose extra protocols on top of its primary one — "also
   discoverable via A2A" or "also exposes tools via MCP" (the Step 3/Step 4 enhancements). In
   AAH each such extra runtime is its **own feature** (its own `a2a_server.py`/`mcp_server.py`
   + a new entry in `agentcore.json runtimes[]` + its own functional tests). The *need* comes
   from the PRD/plan (a feature exists for it), NOT from a decision and NOT from the standalone Q3/Q4
   interview. Do NOT add A2A/MCP runtimes speculatively — only when a feature calls for it.

5. **Deploy mechanics are HARNESS-OWNED — this skill NEVER authors `deploy/*.py`.** Deployment
   mechanics (signing proxy, `agentcore deploy`, secrets/IAM/S3/log-group/alarm provisioning) are
   **invariant across apps** and live once in the harness. This skill establishes only the agent's
   **protocol contract + `agentcore/` config**; it must NOT generate application deploy scripts
   (`deploy/agentcore_deploy.py`, `deploy/proxy_deploy.py`, `deploy/proxy_lambda.py`,
   `deploy/secrets_setup.py`, `deploy/iam_role.py`, `deploy/s3_setup.py`, …). The deploy phase
   (`aah-agentcore-deploy-engineer` / `aah-amplify-deploy-engineer`) invokes the harness scripts
   `aah run core.deploy.{agentcore_deploy,agentcore_proxy,agentcore_memory,amplify_deploy}` — the
   single source of truth. Regenerating them per app is how solved bugs return (it caused the
   deploy-script-as-entrypoint crash, the `S3_BUCKET` startup failure, and a divergent proxy
   endpoint-URL bug in a prior run). **Three-way split:** per-app *logic* (graph, API contract,
   protocol layer, memory callbacks) → authored here; invariant *mechanics* → harness scripts;
   per-app *values* (model IDs, secret path, origin, MEMORY_ID) → `agentcore.json` envVars/config,
   never a `.py`. If a plan feature says "write the deploy script," it's a code no-op — the harness
   owns it. Escape hatch: bespoke mechanics the scripts can't express → extend the harness script
   (a flag/hook), never fork it into the app.

6. **NEVER do header-based auth inside the container. AgentCore does not forward custom headers.**
   AgentCore Runtime forwards the request **body** to `/invocations` but strips arbitrary custom
   headers (e.g. `x-api-key`) — so `Header(...)`/`request.headers["x-api-key"]` auth in the agent
   **always sees nothing and rejects every real request with 401**. This is the #1 cause of a
   "works locally, 401 in the cloud" AgentCore app. Do NOT generate `Depends(verify_api_key)` on a
   header, and do NOT read an auth header in the handler. Two allowed patterns:
   - **Proxy-edge auth (preferred when a browser calls the agent):** the SigV4 signing proxy (a
     harness-owned deploy script, `agentcore_proxy`) validates the API key at the edge before
     signing + forwarding. The agent does **no** auth — SigV4 + the proxy gate are the boundary.
   - **Payload auth (no proxy):** the client puts the key in the JSON body and the agent reads
     `payload.get("api_key")` / `body.get("api_key")` — never a header.

   If a feature/spec says "API-key auth", implement it one of these two ways for an AgentCore target;
   record which (so the deploy phase wires the proxy accordingly). Header auth is a structural bug on
   AgentCore, not a style choice.

7. **Standardize + record the invoke PAYLOAD CONTRACT.** Different callers send different top-level
   keys: `agentcore invoke --prompt …` and the console send `{"prompt": …}` (or `{"input": …}`);
   a REST frontend may send `{"message": …}`. If the handler binds a strict schema to ONE key
   (e.g. `{"message"}` with `extra='forbid'`), every other caller gets a **422**. Prevent it:
   - **Read defensively** — accept the common aliases and normalize to one internal field:
     ```python
     def _extract_prompt(body: dict) -> str:
         # AgentCore CLI/console send "prompt"/"input"; REST clients may send "message".
         return (body.get("prompt") or body.get("message") or body.get("input") or "").strip()
     ```
   - **Do NOT use `extra='forbid'`** on the top-level invoke model for an AgentCore agent — the
     runtime/CLI may add envelope fields; forbidding unknown keys turns them into 422s. Validate the
     ONE field you need; ignore the rest.
   - **RECORD the contract** the app actually accepts (fields + example JSON) in the feature's output
     / a short `agentcore/INVOKE_CONTRACT.md`, so the signing proxy forwards the right body shape and
     smoke-tests (`agentcore_deploy invoke --payload '{…}'`) use the real shape — not a guess.
   The templates in this skill already read `payload.get("prompt")`; extend them with `_extract_prompt`
   when the app's callers (frontend) use a different key.

8. **AgentCore always listens on port 8080 — never a framework default (FastAPI/Django 8000, Flask
   5000).** Every entrypoint must explicitly `uvicorn.run(app, host="0.0.0.0", port=8080)`. In
   multi-agent, the coordinator is 8080; specialist ports (8081, 8082…) are local-dev only. A wrong
   port shows up as a generic init timeout, not a port error — check this first.

9. **Single-endpoint action dispatch — mandatory. Never add a second route.** AgentCore Runtime's
   data-plane operation (`InvokeAgentRuntime`) and the SigV4 signing proxy forward every real
   invocation to exactly ONE path per protocol — HTTP/AG-UI: `/invocations`; A2A: `/`; MCP: `/mcp` —
   plus `/ping` for health. There is no mechanism to route a second URL path to the container in
   production. A manually added route (e.g. `@app.post("/upload-doc")`) responds fine under
   `agentcore dev` / `agentcore validate` / local `curl` (both bypass the real invoke path) but is
   **unreachable once deployed** — this is the exact failure mode behind issue #167. Every additional
   capability is an `action` value dispatched from the SAME request body on the SAME path:
   ```python
   ACTIONS = {
       "chat": handle_chat,
       "upload_doc": handle_upload_doc,
       "search": handle_search,
   }

   @app.post("/invocations")
   async def invocations(request: Request):
       body = await request.json()
       action = body.get("action", "chat")
       handler = ACTIONS.get(action)
       if handler is None:
           return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)
       return await handler(body)
   ```
   If a feature's spec implies its own REST-style path ("POST /documents", "GET /search"), translate
   it to an `action` name dispatched through the single allowed path — do not scaffold a literal
   second route.

10. **Files / binary content — two tiers, never multipart.** AgentCore's `InvokeAgentRuntime` has a
   100MB payload ceiling, but the SigV4 signing proxy (API Gateway + Lambda) has a much smaller
   practical ceiling in front of it. Pick the tier by size:
    - **Small (well under the proxy's ceiling):** base64-encode the file inside the `action` payload
      itself — this is AWS's own documented pattern for images/binary data through `InvokeAgentRuntime`.
      `{"action": "upload_doc", "filename": "notes.pdf", "content_b64": "..."}`.
    - **Large (at or above the proxy's ceiling):** never stream a multipart body through the proxy.
      Use a two-action pattern instead: one action (`create_upload_url`) returns a presigned S3 PUT
      URL; the client uploads directly to S3 (bypassing the proxy entirely); a second action
      (`confirm_upload` / `process_upload`) is invoked with the resulting S3 key to trigger processing.

**Standalone mode — a user invokes `/agentcore-implement` directly** (no AAH project /
no recorded decisions). Only then use `AskUserQuestion` (Step 1b) to gather the inputs
interactively.

## Tool Usage for Questions

Standalone mode only: when a value is NOT resolvable from the registry/architecture/feature
YAML, invoke the `AskUserQuestion` tool — do NOT present questions as plain text. In AAH
mode, resolve from artifacts instead of asking.

---

## Step 1: Detect Mode + Resolve Inputs

**AAH mode:** resolve all inputs from the sources in the table above (read the decisions
registry for the recorded framework, protocol, and app-shape/topology decisions; the **feature YAMLs**
(`.aah/plan/features/*.yaml`) → roster + per-feature logic). Skip 1b entirely — do not ask.
*(`module-map.yaml` + Agent Topology doc also supply app shape + roster.)*

**Standalone mode:** if nothing is recorded, gather the same inputs via `AskUserQuestion` in 1b.

### 1a. Detect existing agent code

```bash
# Find agent code
AGENT_FILES=$(find . -name "*.py" -not -path "./.venv/*" -not -path "./node_modules/*" -not -path "./agentcore/*" 2>/dev/null \
  | xargs grep -l "StateGraph\|from langgraph\|from strands\|from google.adk\|from agents\|Agent\|FastAPI" 2>/dev/null \
  | grep -v __pycache__)
echo "$AGENT_FILES"
```

**If agent code found** → Adapt mode. Ask only protocol question.
**If no agent code** → Scaffold mode. Ask protocol + framework.

### 1b. Ask questions (STANDALONE MODE ONLY)

> Skip this entire subsection in AAH mode — the answers are already recorded (Step 1
> table). Use `AskUserQuestion` here only when invoked standalone with no decision registry /
> feature YAMLs to read from.

Use `AskUserQuestion`:

**Question 0:** "What kind of application are you building?"
- Options:
  - **Single agent (Recommended)** — "One agent with one or more nodes. Deployed as a single runtime."
  - **Multi-agent, single process** — "Supervisor pattern — one runtime with internal routing to specialist nodes (all inside one StateGraph). Coordinator + specialists run in one container."
  - **Multi-agent, distributed** — "Coordinator + specialist agents, all generated in one project. Coordinator orchestrates specialists over HTTP. One `agentcore create`, one deploy creates everything."

**If "Single agent" or "Multi-agent, single process":**

**Question 0c:** "Briefly describe what this agent does (1-2 sentences)."

Free text input. Used for deployment metadata.
Example: "A research assistant that finds and summarizes academic papers on any topic."

→ Proceed to Q1, Q2, Q3, Q4 normally.

---

**If "Multi-agent, distributed"** — ask the following:

**Question 0b:** "Describe the overall system in 1-2 sentences."

Free text input. Used for deployment metadata and coordinator system prompt.
Example: "A travel planning assistant that finds flights, books hotels, and suggests activities."

**Question 0d:** "List each specialist agent (name + what it does)."

Ask user to provide each specialist as: `name: description`

Example input:
```
flights: Searches and compares flight options across airlines
hotels: Finds and recommends hotel accommodations by location and budget
activities: Suggests local activities and experiences at the destination
```

Parse into a list of `{name, description}` pairs. The coordinator is implicit (always generated).

**Question 2 (framework):** Ask framework question as usual (applies to all agents in the system).

**Question 3 (A2A external):** "Should this system be discoverable by external agents (A2A)?"
- **No (default)** — "Only callable via HTTP. No Agent Card."
- **Yes, add A2A** — "Adds an A2A endpoint on the coordinator with Agent Card listing all skills. External agents can discover and invoke the system."

*If Yes → existing Step 4 runs on the coordinator after multi-agent scaffold is complete.*

**Auto-set for distributed:**
- Coordinator → HTTP (port 8080), orchestrates specialists
- All specialists → HTTP (port 8080 each), called by coordinator
- Skip Q1 (all HTTP — determined by architecture)
- Skip Q4 (MCP not applicable in this mode)
- → Execute via **Step 2-distributed** (not regular Step 2)

---

**Question 1:** "What is the primary interface for this agent?"

*Skip if Q0 = "Multi-agent, distributed" (all agents use HTTP — determined by architecture).*

- Options:
  - **HTTP (Recommended)** — "Standard JSON request/response. POST /invocations + GET /ping on port 8080. Simplest, covers most use cases."
  - **AG-UI** — "SSE streaming for frontends. Real-time text streaming + tool call visualization. Port 8080."
  - **A2A** — "Agent-to-Agent primary. This agent is primarily invoked by other agents via JSON-RPC on port 9000."
  - **MCP** — "Tool server protocol. Exposes tools via JSON-RPC on port 8000. Use when your agent IS the tool provider."

**Question 2 (scaffold mode only):** "Which framework?"
- Options:
  - **LangGraph (Recommended)** — "StateGraph-based agent with nodes and edges"
  - **Strands** — "AWS Strands Agents SDK"
  - **Google ADK** — "Google Agent Development Kit"
  - **OpenAI Agents** — "OpenAI Agents SDK"

**Question 3:** "Does this agent also need to be discoverable by other agents (A2A)?"

*Skip if primary is already A2A or if Q0 = "Multi-agent, distributed" (handled separately in Q0's flow).*

- Options:
  - **No (default)** — "Only callable via the primary protocol. No agent-to-agent discovery."
  - **Yes, add A2A** — "Adds Agent Card + JSON-RPC handler. Other agents can discover and invoke this one. Adds a second runtime to agentcore.json OR routes to existing server."

**Question 4:** "Does this agent also expose tools for other agents (MCP)?"
- Options:
  - **No (default)** — "This agent does not expose tools. Other agents cannot use it as a tool server."
  - **Yes, add MCP** — "Adds FastMCP tool server on port 8000. Other agents can discover and call tools from this agent. Adds a second runtime to agentcore.json OR routes to existing server."

**Question 5 (AG-UI only):** "Which transport?"
- Options:
  - **SSE only (Recommended)** — "Unidirectional streaming. Covers 90% of use cases."
  - **SSE + WebSocket** — "Full protocol. Adds human-in-the-loop WebSocket support."

**Question 6 (if A2A selected as primary or additional):** "Does your A2A server need streaming?"
- Options:
  - **Yes, streaming (Recommended)** — "Server streams partial results via message/stream as the agent works."
  - **No, request/response only** — "Returns complete result in a single JSON-RPC response."

### 1c. Determine deployment model for additional protocols

If user selected additional protocols (A2A and/or MCP on top of primary), ask:

**Question:** "How should additional protocols be deployed?"
- Options:
  - **Separate runtimes (Recommended)** — "Each protocol gets its own container and external URL. Clean separation, independently scalable. Multiple entries in agentcore.json."
  - **Single process** — "All protocols served from one FastAPI app on port 8080. Simpler but callers must use the same invoke URL. A2A/MCP paths are internal routes, not platform-discoverable."

---

## Step 2: Execute — Scaffold Mode

> **Use the scaffold helper for steps 2c–2g.** In the harness, run:
> ```bash
> aah run core.implement.agentcore_scaffold scaffold --project-path "$PROJECT_DIR" \
>   --project-name "<PascalName>" --framework "<FrameworkFlag>" --protocol "<Protocol>"
> ```
> It runs `agentcore create`, restructures the nested output to the project root, then you
> call `fix-config` and `validate`. The flag mapping below (2a/2b) still tells you which
> `--framework`/`--protocol` values to pass. Everything after scaffolding — AG-UI/A2A layers,
> multi-agent wiring, agent logic — you apply by hand per this skill's guidance.

`agentcore create` generates the full project:
- Working agent code with correct protocol contract
- `agentcore/` config directory (agentcore.json, aws-targets.json, cdk/)
- Immediately testable with `agentcore dev`

### 2a. Map inputs to CLI flags

| Input | CLI flag |
|-------|----------|
| LangGraph | `--framework LangChain_LangGraph` |
| Strands | `--framework Strands` |
| Google ADK | `--framework GoogleADK` |
| OpenAI Agents | `--framework OpenAIAgents` |
| HTTP | `--protocol HTTP` |
| AG-UI | `--protocol HTTP` (then enhance with AG-UI layer in Step 3) |
| MCP | `--protocol MCP` |
| A2A | `--protocol A2A` |

**Note:** AG-UI is not a native CLI protocol option. Use `--protocol HTTP` then add the AG-UI streaming layer in Step 3.

### 2b. Determine project name

```bash
# From pyproject.toml, directory name, or ask user
PROJECT_NAME=$(python -c "
import re
from pathlib import Path
toml = Path('pyproject.toml')
if toml.exists():
    match = re.search(r'name\s*=\s*\"([^\"]+)\"', toml.read_text())
    if match:
        # Convert to PascalCase, remove hyphens
        name = ''.join(w.capitalize() for w in match.group(1).replace('_','-').split('-'))
        print(name[:23])
        exit()
print(''.join(w.capitalize() for w in Path('.').resolve().name.replace('_','-').split('-'))[:23])
")
echo "Project: $PROJECT_NAME"
```

Must match `^[A-Za-z][A-Za-z0-9]{0,22}$`. Convert hyphens/underscores to PascalCase.

### 2c. Run `agentcore create`

```bash
agentcore create \
  --name $PROJECT_NAME \
  --project-name $PROJECT_NAME \
  --framework $FRAMEWORK_FLAG \
  --model-provider Bedrock \
  --memory none \
  --build CodeZip \
  --language Python \
  --protocol $PROTOCOL_FLAG \
  --network-mode PUBLIC \
  --skip-git
```

**IMPORTANT:** `--name` is REQUIRED by the CLI (resource name). `--project-name` sets the directory name.
Do NOT use `--no-agent` in scaffold mode — we WANT the generated agent code.

### 2d. Restructure output

`agentcore create` puts everything under `<PROJECT_NAME>/`. Move it up to the current directory, putting agent code under `src/` (matching AAH's project layout):

```bash
# If running in an empty directory, move everything up
if [ -d "$PROJECT_NAME" ]; then
  # Move agentcore config
  mv "$PROJECT_NAME/agentcore" ./agentcore 2>/dev/null

  # Move agent code into src/, preserving the CLI's internal structure as one unit so
  # sibling-relative imports (e.g. `from model.load import load_model`) still resolve
  mkdir -p ./src
  if [ -d "$PROJECT_NAME/app/$PROJECT_NAME" ]; then
    cp -r "$PROJECT_NAME/app/$PROJECT_NAME"/* ./src/
  fi

  # Move .venv if created
  mv "$PROJECT_NAME/.venv" ./.venv 2>/dev/null

  # Clean up generated directory
  rm -rf "$PROJECT_NAME"
fi
```

**Note:** code moves into `src/` as one unit — every generated file moves together, so sibling-relative imports keep resolving. `agentcore.json`'s `entrypoint` gets the `src/` prefix (Step 2e); `codeLocation` stays `"./"` so AgentCore Runtime still packages from the project root.

### 2e. Verify agentcore.json entrypoint

Fix the config to point at the file's new location under `src/`:

```bash
python -c "
import json, os
cfg = json.load(open('agentcore/agentcore.json'))
runtime = cfg['runtimes'][0]
print(f'entrypoint: {runtime[\"entrypoint\"]}')
print(f'codeLocation: {runtime[\"codeLocation\"]}')
# entrypoint should be 'src/main.py' (relative to the project root — codeLocation stays './')
if not os.path.exists(runtime['entrypoint']):
    basename = os.path.basename(runtime['entrypoint'])
    if os.path.exists(os.path.join('src', basename)):
        runtime['entrypoint'] = f'src/{basename}'
        json.dump(cfg, open('agentcore/agentcore.json', 'w'), indent=2)
        print(f'Fixed entrypoint to: src/{basename}')
"
```

### 2f. Install dependencies

The `.venv` is created by `agentcore create` using `uv`. Sync dependencies into it:

```bash
uv sync
```

**IMPORTANT:** `agentcore dev` expects `.venv/` with all dependencies installed. The project uses `uv` (not pip) — always use `uv sync` to install from `pyproject.toml`.

### 2g. Validate scaffold

```bash
# Syntax check
python -c "import ast; ast.parse(open('src/main.py').read()); print('Syntax OK')"

# Validate agentcore config
agentcore validate
```

---

## Step 2-distributed: Multi-Agent Distributed Scaffold

**Only when the app shape is "Multi-agent, distributed".**

> **AAH mode:** the coordinator + specialist **roster comes from the feature set**
> (`.aah/plan/features/*.yaml`) — each specialist maps to its own feature(s) — NOT from a
> Q0d free-text list. And it is built incrementally, not all at once: the **scaffold feature
> (F000)** establishes the base project + runtimes, and **each specialist is its own feature**
> that adds its `*_agent.py` + runtime entry along the DAG. The one-shot "generate all
> specialists" flow below is the **standalone** shape; in AAH mode, apply each sub-step (2d-3
> specialist gen, 2d-5 runtime add) for the specialist(s) belonging to the current feature
> only. *(`module-map.yaml` + Agent Topology doc also define the roster.)*

### 2d-1. Run `agentcore create` (one time, for the base project — the F000 scaffold feature)

Use HTTP protocol for the initial scaffold. This gives us `agentcore/`, `pyproject.toml`, `.venv`, and a base `main.py` we'll replace.

```bash
agentcore create \
  --name $PROJECT_NAME \
  --project-name $PROJECT_NAME \
  --framework $FRAMEWORK_FLAG \
  --model-provider Bedrock \
  --memory none \
  --build CodeZip \
  --language Python \
  --protocol HTTP \
  --network-mode PUBLIC \
  --skip-git
```

### 2d-2. Restructure output (same as Step 2d)

```bash
if [ -d "$PROJECT_NAME" ]; then
  mv "$PROJECT_NAME/agentcore" ./agentcore 2>/dev/null
  mkdir -p ./src
  if [ -d "$PROJECT_NAME/app/$PROJECT_NAME" ]; then
    cp -r "$PROJECT_NAME/app/$PROJECT_NAME"/* ./src/
  fi
  mv "$PROJECT_NAME/.venv" ./.venv 2>/dev/null
  rm -rf "$PROJECT_NAME"
fi
```

### 2d-3. Generate specialist entrypoints

For EACH specialist from Q0d, generate a file `src/<name>_agent.py`.

**LangGraph specialist template:**

```python
"""<NAME> specialist agent — <DESCRIPTION>."""
import time
from typing import Annotated, TypedDict

from langchain_aws import ChatBedrock
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages


class State(TypedDict):
    messages: Annotated[list, add_messages]


llm = ChatBedrock(model_id="anthropic.claude-sonnet-4-20250514", region_name="us-east-1")


def respond(state: State) -> State:
    """<DESCRIPTION> — process the request and return results."""
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


graph = StateGraph(State)
graph.add_node("respond", respond)
graph.add_edge(START, "respond")
graph.add_edge("respond", END)
agent = graph.compile()


# --- AgentCore HTTP contract (port 8080) ---
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="<NAME> Agent")


@app.get("/ping")
async def ping():
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


@app.post("/invocations")
async def invoke(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    return JSONResponse(content={"result": result["messages"][-1].content})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Strands specialist template:**

```python
"""<NAME> specialist agent — <DESCRIPTION>."""
import time

from strands import Agent
from bedrock_agentcore.runtime import BedrockAgentCoreApp

agent = Agent(
    model="us.anthropic.claude-sonnet-4-20250514-v1:0",
    system_prompt="You are a <NAME> specialist. <DESCRIPTION>",
)

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload, context):
    prompt = payload.get("prompt", "Hello")
    result = agent(prompt)
    return {"result": str(result)}


if __name__ == "__main__":
    app.run()
```

Generate one file per specialist. Customize the system prompt and node logic based on the specialist's description from Q0d.

### 2d-4. Generate coordinator entrypoint (`src/coordinator.py`)

The coordinator:
- Receives requests on HTTP (port 8080)
- Decides which specialist(s) to invoke based on the user's request
- Calls specialists via HTTP (their `/invocations` endpoint)
- Aggregates results and responds

**LangGraph coordinator template:**

```python
"""Coordinator agent — <SYSTEM_DESCRIPTION>."""
import time
import os
from typing import Annotated, TypedDict

import httpx
from langchain_aws import ChatBedrock
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages


# Specialist URLs — injected as env vars at deploy time, localhost for dev
SPECIALISTS = {
    # <For each specialist from Q0d:>
    "<name>": os.environ.get("<NAME_UPPER>_URL", "http://localhost:<PORT>"),
}


class State(TypedDict):
    messages: Annotated[list, add_messages]
    specialist_results: dict


llm = ChatBedrock(model_id="anthropic.claude-sonnet-4-20250514", region_name="us-east-1")


async def route(state: State) -> State:
    """Determine which specialist(s) to invoke based on the user request."""
    user_msg = state["messages"][-1].content if state["messages"] else ""
    specialist_names = list(SPECIALISTS.keys())

    routing_prompt = f"""Given this user request: "{user_msg}"
Which of these specialists should handle it? Available: {specialist_names}
Reply with a JSON list of specialist names to invoke, e.g. ["flights", "hotels"].
Only include relevant ones."""

    response = llm.invoke([{"role": "user", "content": routing_prompt}])
    import json
    try:
        selected = json.loads(response.content)
    except (json.JSONDecodeError, TypeError):
        selected = specialist_names  # fallback: invoke all

    # Call selected specialists
    results = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for name in selected:
            if name in SPECIALISTS:
                try:
                    resp = await client.post(
                        f"{SPECIALISTS[name]}/invocations",
                        json={"prompt": user_msg}
                    )
                    results[name] = resp.json().get("result", "")
                except Exception as e:
                    results[name] = f"Error: {e}"

    return {"messages": state["messages"], "specialist_results": results}


async def synthesize(state: State) -> State:
    """Combine specialist results into a coherent response."""
    results_text = "\n".join(f"[{k}]: {v}" for k, v in state.get("specialist_results", {}).items())
    user_msg = state["messages"][-1].content if state["messages"] else ""

    synthesis_prompt = f"""User asked: "{user_msg}"

Here are the specialist responses:
{results_text}

Synthesize these into a single coherent response for the user."""

    response = llm.invoke([{"role": "user", "content": synthesis_prompt}])
    return {"messages": state["messages"] + [response], "specialist_results": state.get("specialist_results", {})}


graph = StateGraph(State)
graph.add_node("route", route)
graph.add_node("synthesize", synthesize)
graph.add_edge(START, "route")
graph.add_edge("route", "synthesize")
graph.add_edge("synthesize", END)
coordinator = graph.compile()


# --- AgentCore HTTP contract (port 8080) ---
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="<PROJECT_NAME> Coordinator")


@app.get("/ping")
async def ping():
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


@app.post("/invocations")
async def invoke(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    result = await coordinator.ainvoke({
        "messages": [{"role": "user", "content": prompt}],
        "specialist_results": {}
    })
    final = result["messages"][-1].content
    return JSONResponse(content={"result": final})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**IMPORTANT:** Replace `<name>`, `<NAME_UPPER>`, `<PORT>`, `<SYSTEM_DESCRIPTION>` with actual values from Q0d. Assign each specialist a unique port for local dev (8081, 8082, 8083, etc.). The coordinator uses 8080.

### 2d-5. Update `agentcore.json` with all runtimes

```bash
python -c "
import json

cfg = json.load(open('agentcore/agentcore.json'))
base = cfg['runtimes'][0]  # template from agentcore create

# Coordinator runtime (replaces the default)
cfg['runtimes'] = [{
    'name': '${PROJECT_NAME}Coordinator',
    'build': base.get('build', 'CodeZip'),
    'entrypoint': 'src/coordinator.py',
    'codeLocation': './',
    'runtimeVersion': base.get('runtimeVersion', 'PYTHON_3_12'),
    'networkMode': 'PUBLIC',
    'protocol': 'HTTP'
}]

# Add one runtime per specialist
specialists = [
    # <For each specialist from Q0d:>
    ('${SPECIALIST_NAME}', 'src/${specialist_name}_agent.py'),
]

for name, entrypoint in specialists:
    cfg['runtimes'].append({
        'name': f'${PROJECT_NAME}{name}',
        'build': base.get('build', 'CodeZip'),
        'entrypoint': entrypoint,
        'codeLocation': './',
        'runtimeVersion': base.get('runtimeVersion', 'PYTHON_3_12'),
        'networkMode': 'PUBLIC',
        'protocol': 'HTTP'
    })

json.dump(cfg, open('agentcore/agentcore.json', 'w'), indent=2)
print(f'Configured {len(cfg[\"runtimes\"])} runtimes')
"
```

### 2d-6. Remove default `main.py`

The CLI-generated `main.py` is replaced by `coordinator.py`. Remove to avoid confusion:

```bash
rm -f src/main.py
```

### 2d-7. Update dependencies

Add to `pyproject.toml`:
```
httpx>=0.25.0
```

The coordinator needs `httpx` to call specialists over HTTP.

### 2d-8. Install dependencies

```bash
uv sync
```

### 2d-9. Validate multi-agent scaffold

```bash
# Syntax check all entrypoints
python -c "
import ast, glob
for f in ['src/coordinator.py'] + glob.glob('src/*_agent.py'):
    ast.parse(open(f).read())
    print(f'{f}: Syntax OK')
"

# Validate agentcore config
agentcore validate
```

### 2d-10. Local dev testing (multi-agent)

To test locally, start specialists first, then coordinator:

```bash
# Terminal 1: Start flights specialist
python src/flights_agent.py  # runs on port 8081

# Terminal 2: Start hotels specialist
python src/hotels_agent.py   # runs on port 8082

# Terminal 3: Start coordinator
python src/coordinator.py    # runs on port 8080

# Terminal 4: Test
curl http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Plan a trip to Tokyo"}'
```

Alternatively, use `agentcore dev` which starts the coordinator runtime (it will fail to reach specialists unless they're running separately).

### 2d-11. Report (distributed)

```
=== AgentCore Implement Complete (Multi-Agent Distributed) ===
Mode:        Scaffold (distributed)
Framework:   <FRAMEWORK>
Project:     <PROJECT_NAME>

Agents generated:
  src/coordinator.py     — Orchestrator (HTTP :8080)
  src/<name>_agent.py    — <description> (HTTP :<port>)
  src/<name>_agent.py    — <description> (HTTP :<port>)

Runtimes in agentcore.json: <N>

Local test:
  # Start specialists first, then coordinator
  python src/<name>_agent.py &
  python src/coordinator.py

Deploy (deploys ALL runtimes):
  /aah-deploy   (agentcore route → aah-agentcore-deploy-engineer)
```

**If user also answered Yes to Q3 (A2A discoverability):**
→ Run existing Step 4 on the coordinator. The Agent Card `skills[]` should list the combined capabilities of all specialists.

---

## Step 3: AG-UI Enhancement (only if protocol = AG-UI)

The CLI generates a basic HTTP agent. For AG-UI, we need to add the SSE streaming layer on top.

### 3a. Generate AG-UI events (`src/agui/events.py`)

```python
"""AG-UI protocol event types."""
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class BaseEvent:
    type: str

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class RunStartedEvent(BaseEvent):
    type: str = "RUN_STARTED"
    thread_id: str = ""
    run_id: str = ""


@dataclass
class RunFinishedEvent(BaseEvent):
    type: str = "RUN_FINISHED"
    thread_id: str = ""
    run_id: str = ""


@dataclass
class RunErrorEvent(BaseEvent):
    type: str = "RUN_ERROR"
    message: str = ""
    thread_id: str = ""
    run_id: str = ""


@dataclass
class TextMessageStartEvent(BaseEvent):
    type: str = "TEXT_MESSAGE_START"
    message_id: str = field(default_factory=lambda: str(uuid4()))
    role: str = "assistant"


@dataclass
class TextMessageContentEvent(BaseEvent):
    type: str = "TEXT_MESSAGE_CONTENT"
    message_id: str = ""
    delta: str = ""


@dataclass
class TextMessageEndEvent(BaseEvent):
    type: str = "TEXT_MESSAGE_END"
    message_id: str = ""


@dataclass
class ToolCallStartEvent(BaseEvent):
    type: str = "TOOL_CALL_START"
    tool_call_id: str = field(default_factory=lambda: str(uuid4()))
    tool_call_name: str = ""


@dataclass
class ToolCallArgsEvent(BaseEvent):
    type: str = "TOOL_CALL_ARGS"
    tool_call_id: str = ""
    delta: str = ""


@dataclass
class ToolCallEndEvent(BaseEvent):
    type: str = "TOOL_CALL_END"
    tool_call_id: str = ""


@dataclass
class ToolCallResultEvent(BaseEvent):
    type: str = "TOOL_CALL_RESULT"
    tool_call_id: str = ""
    result: str = ""


@dataclass
class StateSnapshotEvent(BaseEvent):
    type: str = "STATE_SNAPSHOT"
    snapshot: dict = field(default_factory=dict)


@dataclass
class StateDeltaEvent(BaseEvent):
    type: str = "STATE_DELTA"
    delta: list = field(default_factory=list)
```

### 3b. Generate framework adapter

Based on detected/chosen framework, generate the adapter (see AG-UI Adapters section below).

### 3c. Replace server with SSE streaming version

Replace the generated `src/main.py` with a FastAPI server that streams AG-UI events instead of returning JSON. Server must:
- Listen on port 8080
- `POST /invocations` → returns `text/event-stream` (SSE)
- `GET /ping` → returns `{"status": "Healthy"}`
- If WebSocket selected: add `WS /ws` endpoint

### 3d. Update agentcore.json protocol

```bash
python -c "
import json
cfg = json.load(open('agentcore/agentcore.json'))
cfg['runtimes'][0]['protocol'] = 'AGUI'
json.dump(cfg, open('agentcore/agentcore.json', 'w'), indent=2)
print('Protocol set to AGUI')
"
```

---

## Step 4: A2A Enhancement (only if A2A selected as primary or additional)

> **AAH mode:** run this when the recorded protocol decision is A2A (`a2a-agent-to-agent`) OR
> when a **dedicated feature** calls for external A2A discoverability. As an *additional*
> runtime, A2A is its own feature (adds `a2a_server.py` + a `runtimes[]` entry + its own tests)
> — not something to bolt on speculatively. Standalone Q3 is the interactive equivalent.

If user selected A2A as primary protocol, the CLI generates a basic scaffold. If A2A is an additional protocol, this step creates a new runtime entry. Either way, add the A2A protocol layer.

### 4a. Generate Agent Card (`agent_card.json`)

Place at project root (or alongside the A2A entrypoint):

```json
{
  "name": "<PROJECT_NAME>",
  "description": "<auto-detect from agent code or ask user>",
  "version": "1.0.0",
  "url": "http://localhost:9000",
  "protocolVersion": "0.3.0",
  "preferredTransport": "JSONRPC",
  "capabilities": {
    "streaming": true
  },
  "defaultInputModes": ["text"],
  "defaultOutputModes": ["text"],
  "skills": []
}
```

Auto-populate `skills[]` from detected tools in the agent code if possible.

### 4b. Generate JSON-RPC models (`src/a2a/models.py`)

```python
"""A2A protocol JSON-RPC 2.0 models."""
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class JSONRPCRequest:
    jsonrpc: str = "2.0"
    id: str = ""
    method: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class JSONRPCResponse:
    jsonrpc: str = "2.0"
    id: str = ""
    result: dict | None = None
    error: dict | None = None

    def to_dict(self) -> dict:
        d = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error:
            d["error"] = self.error
        else:
            d["result"] = self.result
        return d


@dataclass
class MessagePart:
    kind: str = "text"
    text: str = ""


@dataclass
class Artifact:
    artifact_id: str = field(default_factory=lambda: uuid4().hex)
    name: str = "agent_response"
    parts: list = field(default_factory=list)
```

### 4c. Generate A2A adapter (`src/a2a/adapter.py`)

**For LangGraph:**
```python
"""A2A adapter for LangGraph agents."""
import json
from typing import AsyncIterator
from uuid import uuid4

from .models import Artifact, MessagePart


class LangGraphA2AAdapter:
    def __init__(self, graph):
        self.graph = graph

    async def handle_message(self, message_parts: list[dict], thread_id: str = "") -> list[Artifact]:
        user_text = " ".join(p.get("text", "") for p in message_parts if p.get("kind") == "text")
        config = {"configurable": {"thread_id": thread_id or uuid4().hex}}
        input_msg = {"messages": [{"role": "user", "content": user_text}]}
        result = await self.graph.ainvoke(input_msg, config=config)
        final_message = result["messages"][-1].content if result.get("messages") else ""
        return [Artifact(artifact_id=uuid4().hex, name="agent_response",
                         parts=[MessagePart(kind="text", text=final_message)])]

    async def stream_message(self, message_parts: list[dict], thread_id: str = "") -> AsyncIterator[str]:
        user_text = " ".join(p.get("text", "") for p in message_parts if p.get("kind") == "text")
        config = {"configurable": {"thread_id": thread_id or uuid4().hex}}
        input_msg = {"messages": [{"role": "user", "content": user_text}]}
        async for event in self.graph.astream_events(input_msg, config=config, version="v2"):
            if event.get("event") == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and hasattr(chunk, "content") and chunk.content:
                    yield chunk.content
```

**For Strands:**
```python
"""A2A adapter for Strands agents."""
from typing import AsyncIterator
from uuid import uuid4

from .models import Artifact, MessagePart


class StrandsA2AAdapter:
    def __init__(self, agent):
        self.agent = agent

    async def handle_message(self, message_parts: list[dict]) -> list[Artifact]:
        user_text = " ".join(p.get("text", "") for p in message_parts if p.get("kind") == "text")
        result = await self.agent.invoke_async(user_text)
        return [Artifact(artifact_id=uuid4().hex, name="agent_response",
                         parts=[MessagePart(kind="text", text=str(result))])]

    async def stream_message(self, message_parts: list[dict]) -> AsyncIterator[str]:
        user_text = " ".join(p.get("text", "") for p in message_parts if p.get("kind") == "text")
        async for event in self.agent.stream_async(user_text):
            if hasattr(event, "data") and event.event_type == "text":
                yield event.data
```

### 4d. Generate A2A server (`src/a2a_server.py`)

```python
"""A2A protocol server — JSON-RPC 2.0 agent-to-agent communication."""
import json
import time
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from a2a.models import JSONRPCResponse, Artifact, MessagePart
from a2a.adapter import <AdapterClass>

app = FastAPI(title="A2A Server", version="1.0.0")

# TODO: Import and instantiate your agent here
# adapter = <AdapterClass>(your_agent_instance)


@app.get("/.well-known/agent-card.json")
async def agent_card():
    from pathlib import Path
    card_path = Path(__file__).parent / "agent_card.json"
    return JSONResponse(content=json.loads(card_path.read_text()))


@app.get("/ping")
async def ping():
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


@app.post("/")
async def handle_jsonrpc(request: Request):
    body = await request.json()
    req_id = body.get("id", str(uuid4()))
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "message/send":
        return await _handle_message_send(req_id, params)
    elif method == "message/stream":
        return await _handle_message_stream(req_id, params)
    else:
        return JSONResponse(content=JSONRPCResponse(
            id=req_id, error={"code": -32601, "message": f"Method not found: {method}"}
        ).to_dict())


async def _handle_message_send(req_id: str, params: dict) -> JSONResponse:
    message = params.get("message", {})
    parts = message.get("parts", [])
    artifacts = await adapter.handle_message(parts)
    result = {"artifacts": [{"artifactId": a.artifact_id, "name": a.name,
              "parts": [{"kind": p.kind, "text": p.text} for p in a.parts]} for a in artifacts]}
    return JSONResponse(content=JSONRPCResponse(id=req_id, result=result).to_dict())


async def _handle_message_stream(req_id: str, params: dict) -> StreamingResponse:
    message = params.get("message", {})
    parts = message.get("parts", [])

    async def event_stream() -> AsyncIterator[str]:
        collected = ""
        async for chunk in adapter.stream_message(parts):
            collected += chunk
            yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'type': 'chunk', 'data': chunk}})}\n\n"
        yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'type': 'complete', 'artifacts': [{'artifactId': uuid4().hex, 'name': 'agent_response', 'parts': [{'kind': 'text', 'text': collected}]}]}})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
```

### 4e. Add A2A runtime to agentcore.json

**If separate runtimes model:**
```bash
python -c "
import json
cfg = json.load(open('agentcore/agentcore.json'))
a2a_runtime = {
    'name': '${PROJECT_NAME}A2A',
    'build': 'CodeZip',
    'entrypoint': 'src/a2a_server.py',
    'codeLocation': './',
    'runtimeVersion': cfg['runtimes'][0].get('runtimeVersion', 'PYTHON_3_12'),
    'networkMode': 'PUBLIC',
    'protocol': 'A2A'
}
cfg['runtimes'].append(a2a_runtime)
json.dump(cfg, open('agentcore/agentcore.json', 'w'), indent=2)
print('Added A2A runtime')
"
```

**If single process model:** Add A2A routes to the existing `src/main.py` instead of creating a separate file. Do NOT add a second runtime entry.

### 4f. Update dependencies

Add to `pyproject.toml`:
```
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
httpx>=0.25.0
```

---

## Step 5: Adapt Mode (existing code)

When existing agent code is detected, do NOT regenerate. Instead:

### 5a. Scaffold config only

```bash
agentcore create \
  --name $PROJECT_NAME \
  --project-name $PROJECT_NAME \
  --framework $FRAMEWORK_FLAG \
  --model-provider Bedrock \
  --memory none \
  --build CodeZip \
  --language Python \
  --protocol $PROTOCOL_FLAG \
  --network-mode PUBLIC \
  --skip-git \
  --skip-python-setup \
  --skip-install \
  --no-agent
```

Move `agentcore/` config out:
```bash
mv "$PROJECT_NAME/agentcore" ./agentcore 2>/dev/null
rm -rf "$PROJECT_NAME"
```

### 5b. Add protocol contract to existing code

**If code already has FastAPI:**
- Ensure port is 8080
- Ensure `POST /invocations` exists
- Ensure `GET /ping` exists

**If code has no HTTP server** (pure agent logic):

Append `BedrockAgentCoreApp` wrapper OR FastAPI routes to the existing file — DO NOT rewrite the file.

For BedrockAgentCoreApp (simplest):
```python
# --- Appended: AgentCore protocol contract ---
from bedrock_agentcore.runtime import BedrockAgentCoreApp

agentcore_app = BedrockAgentCoreApp()

@agentcore_app.entrypoint
async def invoke(payload, context):
    prompt = payload.get("prompt", "Hello")
    result = await <DETECTED_GRAPH_VAR>.ainvoke({"messages": [HumanMessage(content=prompt)]})
    return {"result": result["messages"][-1].content}

if __name__ == "__main__":
    agentcore_app.run()
```

### 5c. Fix agentcore.json

Point entrypoint to the existing file:
```bash
python -c "
import json
cfg = json.load(open('agentcore/agentcore.json'))
cfg['runtimes'][0]['entrypoint'] = '<DETECTED_AGENT_FILE>'
cfg['runtimes'][0]['codeLocation'] = './'
json.dump(cfg, open('agentcore/agentcore.json', 'w'), indent=2)
"
```

### 5d. Add OTEL dependencies

Ensure `pyproject.toml` has:
```
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp>=1.20.0
opentelemetry-instrumentation>=0.41b0
```

For `BedrockAgentCoreApp` pattern, also add: `bedrock-agentcore>=1.8.0`

### 5e. Install dependencies

```bash
uv sync
```

**IMPORTANT:** `agentcore dev` expects `.venv/` with all dependencies installed. Use `uv sync` (not pip) — the project is managed by `uv`.

---

## Step 6: Validate + Report

```bash
# Syntax
python -c "import ast; ast.parse(open('<ENTRYPOINT>').read()); print('Syntax OK')"

# Config
agentcore validate

# Check protocol contract exists
grep -q "/invocations\|@app.entrypoint\|FastMCP" <ENTRYPOINT> && echo "Contract OK"
```

**Report:**
```
=== AgentCore Implement Complete ===
Mode:        <Scaffold | Adapt>
Protocol:    <HTTP | AGUI | MCP | A2A>
Framework:   <LangGraph | Strands | GoogleADK | OpenAIAgents>
Entrypoint:  <path>
Project:     <PROJECT_NAME>

Ready to test:
  agentcore dev

Ready to deploy:
  /aah-deploy   (agentcore route)
```

---

## AG-UI Adapters Reference

### LangGraph Adapter

```python
"""AG-UI adapter for LangGraph agents."""
import json
from typing import AsyncIterator
from uuid import uuid4

from .events import (
    RunStartedEvent, RunFinishedEvent, RunErrorEvent,
    TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent,
    ToolCallStartEvent, ToolCallArgsEvent, ToolCallEndEvent, ToolCallResultEvent,
    BaseEvent,
)


class LangGraphAGUIAdapter:
    def __init__(self, graph):
        self.graph = graph

    async def run(self, messages: list, thread_id: str, run_id: str, state: dict | None = None) -> AsyncIterator[BaseEvent]:
        yield RunStartedEvent(thread_id=thread_id, run_id=run_id)
        msg_id = str(uuid4())
        yield TextMessageStartEvent(message_id=msg_id)

        try:
            config = {"configurable": {"thread_id": thread_id}}
            input_msg = {"messages": [{"role": m["role"], "content": m["content"]} for m in messages]}

            async for event in self.graph.astream_events(input_msg, config=config, version="v2"):
                kind = event.get("event", "")
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield TextMessageContentEvent(message_id=msg_id, delta=chunk.content)
                elif kind == "on_tool_start":
                    tool_id = str(uuid4())
                    yield ToolCallStartEvent(tool_call_id=tool_id, tool_call_name=event.get("name", ""))
                    tool_input = event.get("data", {}).get("input", {})
                    if tool_input:
                        yield ToolCallArgsEvent(tool_call_id=tool_id, delta=json.dumps(tool_input))
                    yield ToolCallEndEvent(tool_call_id=tool_id)
                elif kind == "on_tool_end":
                    tool_id = str(uuid4())
                    output = event.get("data", {}).get("output", "")
                    yield ToolCallResultEvent(tool_call_id=tool_id, result=str(output))

            yield TextMessageEndEvent(message_id=msg_id)
            yield RunFinishedEvent(thread_id=thread_id, run_id=run_id)
        except Exception as e:
            yield TextMessageEndEvent(message_id=msg_id)
            yield RunErrorEvent(message=str(e), thread_id=thread_id, run_id=run_id)
```

### Strands Adapter

```python
"""AG-UI adapter for Strands agents."""
import json
from typing import AsyncIterator
from uuid import uuid4

from .events import (
    RunStartedEvent, RunFinishedEvent, RunErrorEvent,
    TextMessageStartEvent, TextMessageContentEvent, TextMessageEndEvent,
    ToolCallStartEvent, ToolCallArgsEvent, ToolCallEndEvent, ToolCallResultEvent,
    BaseEvent,
)


class StrandsAGUIAdapter:
    def __init__(self, agent):
        self.agent = agent

    async def run(self, messages: list, thread_id: str, run_id: str, state: dict | None = None) -> AsyncIterator[BaseEvent]:
        yield RunStartedEvent(thread_id=thread_id, run_id=run_id)
        msg_id = str(uuid4())
        yield TextMessageStartEvent(message_id=msg_id)

        try:
            user_message = messages[-1]["content"] if messages else ""
            async for event in self.agent.stream_async(user_message):
                if hasattr(event, "data"):
                    if event.event_type == "text":
                        yield TextMessageContentEvent(message_id=msg_id, delta=event.data)
                    elif event.event_type == "tool_use":
                        tool_id = str(uuid4())
                        yield ToolCallStartEvent(tool_call_id=tool_id, tool_call_name=event.tool_name)
                        yield ToolCallArgsEvent(tool_call_id=tool_id, delta=json.dumps(event.data))
                        yield ToolCallEndEvent(tool_call_id=tool_id)
                    elif event.event_type == "tool_result":
                        yield ToolCallResultEvent(tool_call_id=tool_id, result=str(event.data))

            yield TextMessageEndEvent(message_id=msg_id)
            yield RunFinishedEvent(thread_id=thread_id, run_id=run_id)
        except Exception as e:
            yield TextMessageEndEvent(message_id=msg_id)
            yield RunErrorEvent(message=str(e), thread_id=thread_id, run_id=run_id)
```

---

## Framework-Specific Notes

### Google ADK

- **Agent variable MUST be named `root_agent`** — the ADK CLI (`adk web`, `adk run`) searches for this exact variable name in `agent.py`. Using `agent` or any other name causes `ValueError: No root_agent found`.
- **Model on AWS:** Use `LiteLlm(model="bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0")` — LiteLLM bridges ADK to Bedrock. Gemini is NOT available on Bedrock. The model ID MUST use the full cross-region format (`us.anthropic.<model>-v1:0`), not the short form.
- **`adk web` directory structure:** The CLI expects `<project_dir>/agent.py` with `root_agent` exported at module level.
- **MCP tools in ADK:** Use `google.adk.tools.mcp_tool.McpTool` with `SseServerParams(url=...)`.
- **Adapt mode for ADK:** When wrapping with HTTP contract, import `root_agent` from `agent.py` and invoke it via the ADK runner or direct call.

### LangGraph

- Agent variable name is flexible (commonly `agent`, `graph`, `app`).
- Adapt mode detects `StateGraph` or `from langgraph` imports.

### Strands

- Uses `BedrockAgentCoreApp` + `@app.entrypoint` pattern natively.
- Agent variable name is flexible.

### OpenAI Agents

- Uses `Agent` from `agents` package.
- Requires adapter to translate between OpenAI Agents SDK and AgentCore protocol.

---

## Notes

- **AAH completion bar = passing tests, not `agentcore dev`.** In an AAH project a feature
  is done when its NO-MOCKS functional tests pass against `acceptance_criteria` (per
  `testing-standards` + the aah-feature-implementer procedure). `agentcore dev` / `agentcore
  validate` are optional local smoke checks. The "Ready to test: `agentcore dev`" lines in the
  Report blocks are convenience hints, not the gate.
- **Model IDs in the code templates are placeholders.** Use the project's configured Bedrock
  model (from the model decision / feature YAML), not the literal `anthropic.claude-sonnet-4-…`
  shown in examples.
- **`agentcore create` is the foundation** — never hand-write what the CLI generates (call it
  via `agentcore_scaffold.py`, not raw).
- **`agentcore dev` requires `agentcore/` directory** — scaffold mode always produces it.
- **Adapt mode uses `--no-agent`** to avoid overwriting existing code.
- **AG-UI is HTTP + streaming layer** — CLI generates HTTP base, skill adds SSE events on top.
- **NEVER hardcode credentials** — boto3 default credential chain.
- **OTEL is mandatory** for all AgentCore runtimes.
- **ARM64 (Graviton)** — don't use packages requiring x86 native binaries.
- **hatchling packaging:** If skill creates new directories (e.g., `mcp_client/`, `a2a/`), add `[tool.hatch.build.targets.wheel] packages = [...]` to `pyproject.toml` so `uv sync` doesn't fail.
