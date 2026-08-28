---
name: mcp-integration
description: >
  MCP server integration and deployment guide.
  Covers connecting existing MCP servers to any agent framework (Strands, LangGraph, ADK, CrewAI, AutoGen, or custom),
  creating new MCP servers, and deploying via AWS Bedrock AgentCore Runtime.
  NOTE: For creating MCP tool servers FROM SCRATCH, use /agentcore-implement with protocol=MCP.
  This skill is for CONNECTING agents to MCP servers (yours or third-party).
user-invocable: true
disable-model-invocation: false
---

# MCP Integration — Connect Agents to MCP Tool Servers

This skill handles connecting your agent to MCP tool servers — either ones you already have
deployed, ones that exist as code but need deployment, or ones you want to create.

## Harness integration (read first)

Reference skill the `aah-feature-implementer` consults during the AAH build phase when an
AgentCore project's tools are exposed over/consumed via MCP. The guidance here — connecting
agents to MCP tools, framework wiring (Strands/ADK/LangGraph), SigV4, cross-runtime IAM — is
what you apply. For the **deploy path (Path B)**, reuse the harness deploy script rather than
raw CLI: `aah run core.deploy.agentcore_deploy {scaffold|deploy|status}` (an MCP server is
just an AgentCore runtime with `protocol: MCP`). The agent's protocol contract itself is
created via `agentcore-implement` (+ `agentcore_scaffold`).

### Two modes: AAH (default) vs Standalone

**AAH mode — the `aah-feature-implementer` runs this unattended in a git worktree. It is NOT
interactive.** Everywhere this skill says "ask the user", "show the output to the user", or
"the user expects…", that is the **standalone** shape. In AAH mode instead:

| Input | AAH source (do NOT ask) |
|---|---|
| Whether tools use MCP at all | the recorded agentic-AI decision in the decisions registry — MCP in scope means apply this skill; otherwise skip. If the agent itself is exposed as an MCP tool server, that is the recorded protocol decision's `mcp-tool-server` option |
| Which MCP server(s) / URL(s) to wire | the current **feature YAML** (`.aah/plan/features/*.yaml`) — its description / integration notes |
| Auth method (none / Bearer / SigV4) | the MCP server's nature (external vs AgentCore-deployed) per the feature YAML |
| Agent framework | the recorded **framework** decision in the decisions registry |
| AWS profile / region | pipeline/env (deploy phase selects the profile) — not a build-time question |

- **Wire MCP into the agent code** per the feature's scope; do NOT run interactive
  `AskUserQuestion` or block waiting to "show results to the user."
- **Completion = passing NO-MOCKS functional tests** against the feature's `acceptance_criteria`
  (per `testing-standards`), not "show the tool listing to the user." A live MCP call may be
  part of a functional test where the feature requires it.

**Standalone mode — a user invokes `/mcp-integration` directly.** Only then use the
`AskUserQuestion` / "show output" steps below as written.

**Distinction from `agentcore-implement`:**
- `agentcore-implement` with protocol=MCP = create an MCP server (you ARE the tool provider)
- `mcp-integration` = connect your agent TO an MCP server (you USE tools from a server)

---

## Decision Tree

```
Does the user already have an MCP server?
├── YES, external/third-party URL ────► Path A, Option 1: Connect (no auth)
├── YES, deployed with auth ──────────► Path A, Option 2 or 3: Connect (Bearer/SigV4)
├── YES, code exists (not deployed) ──► Path B: Deploy, then Path A
└── NO ───────────────────────────────► Path C: Create, then Path B
```

### Dependencies for connecting to MCP servers (Path A)

```bash
uv add strands-agents mcp boto3
```

| Package | Purpose |
|---------|---------|
| `strands-agents` | Agent framework (includes `strands.tools.mcp.mcp_client`) |
| `mcp` | MCP protocol client (includes `mcp.client.streamable_http`) |
| `boto3` | AWS SDK — needed for Bedrock model session configuration |

---

## Pre-Flight Checks (before any deployment)

Before attempting to deploy, verify these. **In AAH the deploy path is owned by the
deploy phase (`/aah-deploy` → `agentcore_deploy.py`), which selects the AWS profile** — the
build-phase `aah-feature-implementer` does not deploy or ask for a profile. The items below apply
to **standalone** deploys of an MCP server:

1. **AWS Profile** — Ask: "Which AWS profile should I use for deployment?"
   - User provides profile name (e.g., `dev`, `prod`, `my-sso-profile`)
   - Set for all subsequent commands: `export AWS_PROFILE=<user-provided-profile>`
   - **IMPORTANT:** If `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars exist and point
     to a different account, they will override the profile. Clear them by prefixing commands:
     `AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= AWS_SESSION_TOKEN= AWS_PROFILE=<profile> <command>`

2. **Validate environment:**
```bash
# Verify AWS credentials work with the given profile
aws sts get-caller-identity --profile <profile>

# Verify region
aws configure get region --profile <profile>

# Verify Node.js (required for AgentCore CLI)
node --version  # Must be 18+

# Verify/install AgentCore CLI
agentcore --version 2>/dev/null || npm install -g @aws/agentcore
```

3. **If any check fails**, tell the user exactly what's missing and how to fix it. Do NOT proceed with deployment until all checks pass.

**Required info to ask upfront (before deployment):**
- AWS profile name
- AWS region (if not set in profile)
- Account ID confirmation (from `sts get-caller-identity`)

---

## Path A — Connect Agent to an Existing MCP Server

Use this when you already have a running MCP server URL and want to wire it into an agent.

**Required info:**
- MCP server URL
- Auth method (none, IAM SigV4, Bearer token)
- Agent framework (Strands, LangGraph, ADK, or any other)
- AWS profile name (if using Bedrock as the agent's LLM)

### Step 0: Resolve Required Info FIRST

> **AAH mode:** resolve these from the feature YAML / decision registry / env (see the mode
> table above) — do NOT ask. **Standalone mode only:** ask the user via `AskUserQuestion`.

Required info (from artifacts in AAH mode; asked interactively only when standalone):

1. **AWS Profile** — Ask: "Which AWS profile should I use for the Bedrock model?"
2. **AWS Region** — Ask: "Which AWS region should I use?" (default: us-east-1)

Do NOT assume defaults. Do NOT write the script until the user provides these values.
If the user provides them in their original message, proceed without asking again.

After getting the profile, verify it works:
```bash
aws sts get-caller-identity --profile <profile>
aws configure get region --profile <profile>
```

---

### Execution Instructions (MANDATORY — after writing the script)

After writing the script, Claude MUST:
1. **Install dependencies** if not already installed: `uv add strands-agents mcp boto3` (or framework-specific deps)
2. **Run the script** using `uv run python <script_name>.py`
3. **Show the full output** to the user (tool listing + agent response)
4. If the script fails, debug and fix until it produces results
5. **Update the project's main agent file** (e.g., `agent.py`) to include the MCP tool wiring so the tools persist beyond the test script

Do NOT just write the script and stop. The user expects to see actual results from the MCP server.

---

### MANDATORY: Update the Main Agent File

After the test script runs successfully, Claude MUST update the project's existing agent file
(typically `agent.py`) to include the MCP tools. The test script proves connectivity works —
now wire it into the actual agent so subsequent steps (`agentcore-implement`, then `/aah-deploy`)
pick up the tools.

**How to detect the main agent file:**
- Look for `agent.py` or a file with `root_agent` / `Agent(` / `LlmAgent(` in the project root
- If no agent file exists, create `agent.py`

**What to add:**
- MCP tool imports and connection setup (matching the framework used in the test script)
- Add the MCP tools to the agent's `tools=[]` parameter
- Preserve existing agent configuration (name, model, instruction)

**Example (ADK):**
```python
# Before (bare agent)
root_agent = LlmAgent(name="...", model=..., instruction="...")

# After (with MCP tools)
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_toolset import StreamableHTTPConnectionParams

root_agent = LlmAgent(
    name="...", model=..., instruction="...",
    tools=[McpToolset(connection_params=StreamableHTTPConnectionParams(url="https://..."))],
)
```

Do NOT leave the MCP tools only in the standalone test script. The main agent file IS the deliverable.

**Verification (MANDATORY — run after updating the agent file):**
```bash
# 1. Syntax check
python -c "import ast; ast.parse(open('agent.py').read()); print('Syntax OK')"

# 2. MCP wiring present
grep -q "McpToolset\|McpTool\|mcp_client" agent.py && echo "MCP wiring OK" || echo "FAIL: MCP wiring missing"

# 3. External URL present (not localhost)
grep -q "https://" agent.py && echo "External URL OK" || echo "FAIL: No external URL"

# 4. Agent naming convention (ADK requires root_agent)
grep -q "root_agent" agent.py && echo "root_agent OK" || echo "FAIL: root_agent missing"

# 5. Import resolution (packages installed correctly)
uv run python -c "from google.adk.tools.mcp_tool import McpToolset; print('Import OK')"
```

All 5 checks must pass. If any fail, fix the issue before reporting success.

---

**Four steps:**
1. **Step 1** — Connect to MCP server and get tools (framework-agnostic)
2. **Step 2** — Wire tools into your agent framework
3. **Step 3** — Execute the script and show results to the user
4. **Step 4** — Update the main agent file with MCP tool wiring + verify

---

### Step 1: Connect to MCP Server and Get Tools

This step is the same regardless of which agent framework you use.
Pick the auth method that matches your MCP server:

| Your MCP server is... | Use |
|------------------------|-----|
| Public / no auth needed (e.g., `arxiv.run.tools`) | Option 1: No auth |
| External with Bearer token (OAuth, Cognito, API key) | Option 2: Bearer token |
| Your own AgentCore-deployed server (AWS account) | Option 3: SigV4 |

#### Option 1: No auth (public MCP server)

```python
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "https://<your-mcp-server>/mcp"

mcp_client = MCPClient(lambda: streamablehttp_client(MCP_URL))

with mcp_client:
    tools = mcp_client.list_tools_sync()
    # → tools is a list of MCPAgentTool objects, ready for Step 2
```

#### Option 2: Bearer token (OAuth, Cognito, API key)

```python
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "https://<your-mcp-server>/mcp"
BEARER_TOKEN = "<your-token>"

mcp_client = MCPClient(
    lambda: streamablehttp_client(MCP_URL, headers={"authorization": f"Bearer {BEARER_TOKEN}"})
)

with mcp_client:
    tools = mcp_client.list_tools_sync()
    # → tools is a list of MCPAgentTool objects, ready for Step 2
```

#### Option 3: SigV4 (AgentCore-deployed MCP servers)

**IMPORTANT:** `streamablehttp_client` does NOT support SigV4 natively. SigV4 must sign
each request body individually. Use manual HTTP calls with per-request signing:

```python
import json
import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-east-1"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/<runtime-id>"
ENCODED_ARN = RUNTIME_ARN.replace(":", "%3A").replace("/", "%2F")
MCP_URL = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{ENCODED_ARN}/invocations?qualifier=DEFAULT"


def get_sigv4_headers(url, body):
    """Sign a request with SigV4."""
    # Use os.environ.get("AWS_PROFILE") — returns None in AgentCore runtime,
    # which makes boto3 use the IAM role (default credential chain).
    # Locally, set AWS_PROFILE env var to pick up your named profile.
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=REGION)
    credentials = session.get_credentials().get_frozen_credentials()
    request = AWSRequest(method="POST", url=url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    SigV4Auth(credentials, "bedrock-agentcore", REGION).add_auth(request)
    return dict(request.headers)


def mcp_call(method, params=None, req_id=1):
    """Make a signed MCP JSON-RPC call."""
    body = json.dumps({"jsonrpc": "2.0", "method": method, "id": req_id, "params": params or {}})
    headers = get_sigv4_headers(MCP_URL, body)
    resp = httpx.post(MCP_URL, content=body, headers=headers, timeout=30)
    resp.raise_for_status()
    for line in resp.text.split("\n"):
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(resp.text)


# List tools
result = mcp_call("tools/list", {}, req_id=1)
mcp_tools = result["result"]["tools"]
# → mcp_tools is a list of dicts with "name", "description", "inputSchema"
# → Use these in Step 2 to create tool wrappers for your agent
```

Key points for SigV4:
- URL-encode the ARN: replace `:` with `%3A` and `/` with `%2F`
- Each request must be signed independently (SigV4 signs the body)
- Response is SSE format: parse lines starting with `data: `
- Dependencies needed: `boto3`, `httpx`

**Quick test via CLI (no code needed):**
```bash
agentcore invoke --runtime <runtime-name> --tool <tool-name> \
  --input '{"param":"value"}' --prompt "call-tool" --json
```

#### MCPAgentTool API Reference (Options 1 & 2)

After calling `mcp_client.list_tools_sync()`, each tool object has:

| Attribute | Type | Description |
|-----------|------|-------------|
| `tool.tool_name` | `str` | The tool's name (e.g., `"search_papers"`) |
| `tool.tool_spec` | `dict` | Full spec with `name`, `description`, and `inputSchema` |
| `tool.tool_spec['description']` | `str` | Human-readable description of what the tool does |
| `tool.tool_spec['inputSchema']['json']` | `dict` | JSON Schema for the tool's parameters |
| `tool.tool_type` | `str` | Always `"mcp"` for MCP tools |

**Inspecting tool parameters:**
```python
for tool in tools:
    schema = tool.tool_spec['inputSchema']['json']
    print(f"{tool.tool_name}:")
    print(f"  Description: {tool.tool_spec['description']}")
    print(f"  Required params: {schema.get('required', [])}")
    for param, details in schema.get('properties', {}).items():
        print(f"  - {param} ({details.get('type', '?')}): {details.get('description', '')}")
```

#### Key Rules for Step 1

- `MCPClient` takes a **lambda** (factory function), not the client directly
- `list_tools_sync()` returns a list of `MCPAgentTool` objects
- The `with mcp_client:` context manager handles MCP session lifecycle (initialize/close)
- **All Step 2 code must be inside the `with mcp_client:` block** — tools stop working after the session closes
- On Windows, add `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")` to handle Unicode

---

### Step 2: Wire Tools into Your Agent Framework

Pick the framework the user specifies. The tools from Step 1 can be used with any framework.

#### Strands Agent (native support — simplest)

Strands has built-in MCP support. Pass tools directly from `list_tools_sync()`:

```python
import boto3
from strands import Agent
from strands.models import BedrockModel

# Inside the `with mcp_client:` block from Step 1:
model = BedrockModel(
    model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
    boto_session=boto3.Session(profile_name="<profile>", region_name="<region>"),
)
agent = Agent(model=model, tools=tools)
response = agent("Your prompt here")
print(response)
```

**Note:** AWS profile and region should already be confirmed from Step 0.

**Dependencies:** `strands-agents`, `mcp`, `boto3`

#### Google ADK Agent

ADK uses `McpTool` with `SseServerParams` for connecting to MCP servers:

```python
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import McpTool, SseServerParams

# Connect to MCP servers
weather_tool = McpTool(
    server_params=SseServerParams(url="http://localhost:8001/mcp")
)

search_tool = McpTool(
    server_params=SseServerParams(url="https://arxiv.run.tools/mcp")
)

root_agent = LlmAgent(
    name="research_assistant",
    model=LiteLlm(model="bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"),
    instruction="You are a helpful research assistant.",
    description="Research assistant with tool access.",
    tools=[weather_tool, search_tool],
)
```

**IMPORTANT ADK conventions:**
- Agent variable MUST be named `root_agent` (ADK CLI searches for this exact name)
- Model ID MUST use full cross-region format: `us.anthropic.<model>-v1:0`
- `McpTool` handles the MCP session lifecycle internally (no `with` block needed)
- `SseServerParams(url=...)` for streamable-http MCP servers

**Dependencies:** `google-adk`, `litellm`, `boto3`

#### LangGraph Agent

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

# Works for public/Bearer token URLs (Options 1 & 2)
async with MultiServerMCPClient(
    {"my-server": {"url": MCP_URL, "transport": "streamable_http"}}
) as client:
    tools = client.get_tools()
    # Add tools to your LangGraph agent node
```

For LangGraph + SigV4 (Option 3): use the generic pattern below.

**Dependencies:** `langchain-mcp-adapters`, `langgraph`

#### Generic Pattern (any framework — CrewAI, AutoGen, custom, etc.)

For any agent framework that accepts Python callables as tools, wrap MCP tools
as plain functions. This works with **any auth method** (Options 1, 2, or 3).

**For Options 1 & 2 (using MCPClient):**

```python
import json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import asyncio

MCP_URL = "https://<your-mcp-server>/mcp"


async def call_mcp_tool(tool_name: str, arguments: dict) -> str:
    """Call any MCP tool by name. Works as a generic wrapper."""
    async with streamablehttp_client(MCP_URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return result.content[0].text


async def list_mcp_tools() -> list:
    """Discover all tools available on the MCP server."""
    async with streamablehttp_client(MCP_URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


# Example: discover tools and create wrappers
tools_info = asyncio.run(list_mcp_tools())
for t in tools_info:
    print(f"  {t.name}: {t.description}")
    print(f"    Input schema: {t.inputSchema}")

# Example: call a specific tool
result = asyncio.run(call_mcp_tool("search_papers", {"query": "large language models", "max_results": 3}))
print(result)
```

**For Option 3 (SigV4):**

```python
# Use the mcp_call() function from Step 1, Option 3:
def call_tool(tool_name: str, arguments: dict) -> str:
    """Wrapper to call any MCP tool on the AgentCore runtime."""
    result = mcp_call("tools/call", {"name": tool_name, "arguments": arguments}, req_id=10)
    return result["result"]["content"][0]["text"]

# Example: call a tool
result = call_tool("greet", {"name": "World"})
```

**Dependencies:** `mcp` (+ `httpx` for SigV4)

---

### Complete Working Example (Strands + no auth)

End-to-end script combining Step 1 (Option 1) + Step 2 (Strands):

```python
import sys
import io
import boto3
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

# Fix for Windows: ensure stdout handles Unicode characters from MCP responses
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MCP_URL = "https://<your-mcp-server>/mcp"

# Step 1: Connect to MCP server
mcp_client = MCPClient(lambda: streamablehttp_client(MCP_URL))

with mcp_client:
    # List available tools
    tools = mcp_client.list_tools_sync()
    print("Available tools:")
    for tool in tools:
        print(f"  - {tool.tool_name}: {tool.tool_spec['description']}")

    # Step 2: Wire into Strands Agent
    model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
        boto_session=boto3.Session(profile_name="<profile>", region_name="<region>"),
    )
    agent = Agent(model=model, tools=tools)
    response = agent("Your prompt here")
    print(response)
```

---

## Path B — Deploy Your MCP Server to AWS

Use this when you have MCP server code (from Path C or existing) and need to deploy it
so it's accessible via a URL. After deployment, connect to it using Path A, Option 3.

**Required info:**
- AWS account with AgentCore permissions
- AWS region (us-east-1, us-west-2 confirmed)
- MCP server code (must use streamable-http transport)

### Deploy via AgentCore CLI

```bash
# Step 1: Set AWS profile
export AWS_PROFILE=<user-provided-profile>

# Step 2: Install AgentCore CLI (if not already installed)
npm install -g @aws/agentcore

# Step 3: Create project with MCP protocol
agentcore create --protocol MCP

# Step 4: Verify aws-targets.json has correct account ID
cat MyMCPServer/agentcore/aws-targets.json

# Step 5: Copy your MCP server code into the project
# Place your server file in app/MyMCPServer/ directory
# Ensure agentcore.json entrypoint points to your server file

# Step 6: Bootstrap CDK (one-time per account/region)
npx cdk bootstrap aws://<account-id>/<region>

# Step 7: Deploy — run from project root
agentcore deploy --yes --verbose
```

**CRITICAL:** Use `--protocol MCP`, NOT `--defaults`. The `--defaults` flag
creates an HTTP agent project (Strands + BedrockAgentCoreApp pattern) which
does NOT work as an MCP server.

#### Project structure

After `agentcore create --protocol MCP`:
```
MyMCPServer/
├── agentcore/
│   ├── agentcore.json       # Project config — runtime with protocol: "MCP"
│   ├── aws-targets.json     # Deployment target (account + region) ← VERIFY THIS
│   └── cdk/                 # CDK infrastructure code
└── app/
    └── MyMCPServer/
        ├── main.py          # Your FastMCP server
        ├── pyproject.toml
        └── __init__.py
```

#### agentcore.json — key settings

```json
{
  "runtimes": [{
    "name": "MyMCPServer",
    "build": "CodeZip",
    "entrypoint": "main.py",
    "codeLocation": "app/MyMCPServer/",
    "runtimeVersion": "PYTHON_3_14",
    "networkMode": "PUBLIC",
    "protocol": "MCP"
  }],
  "agentCoreGateways": []
}
```

No gateway needed. Clients connect directly to runtime URL.

#### Required dependencies (pyproject.toml)

```toml
[project]
name = "my-mcp-server"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "mcp>=1.19.0",
    "aws-opentelemetry-distro",
    "opentelemetry-instrumentation",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**MANDATORY:** `aws-opentelemetry-distro` + `opentelemetry-instrumentation` — without these, deploy fails.

**Do NOT include:** `bedrock-agentcore`, `strands-agents` — those are for HTTP agents, not MCP servers.

#### MCP server requirements

- Listen on `0.0.0.0:8000/mcp`
- Use `stateless_http=True`
- Use `transport="streamable-http"`

#### After deployment

```bash
agentcore status
# → Runtime ARN: arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/<runtime-id>
```

Invocation URL: `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<ENCODED_ARN>/invocations?qualifier=DEFAULT`

#### Testing deployed server

```bash
# Quick test via CLI (IAM auth automatic):
agentcore invoke --runtime MyMCPServer --tool <tool-name> --input '{"param":"value"}' --prompt "call-tool" --json
```

### MANDATORY: Connect Agent After Deploy (Path B → Path A auto-continuation)

After a successful deployment (Path B), Claude MUST automatically proceed to Path A
without waiting for the user to ask. The full chain is:

1. Deploy completes → get the Runtime ARN from deploy output
2. Install agent dependencies: `uv add strands-agents mcp boto3 httpx`
3. **Grant cross-runtime invoke permission** (if the calling agent is ALSO deployed on AgentCore):
   - Find the calling agent's IAM role name from its CloudFormation stack
   - Add an inline policy granting `bedrock-agentcore:InvokeAgentRuntime` on the MCP runtime ARN
   - See "Cross-Runtime IAM Permission" section below
4. Write and execute a Python script that:
   - Connects to the deployed MCP URL using SigV4 signing
   - Lists available tools (confirms deployment works)
   - Wraps MCP tools as agent tool functions
   - Creates an agent backed by Bedrock Claude
   - Invokes the agent with a prompt that exercises the deployed tool(s)
   - Prints the agent's response
5. Show the full output to the user

**Do NOT stop after deploy and wait for the user to say "now connect it."**
The user expects end-to-end results: create → deploy → agent calls tool → show response.

---

### Cross-Runtime IAM Permission (AgentCore → AgentCore MCP)

When your agent is ALSO deployed on AgentCore and needs to call another AgentCore-deployed MCP server,
the agent's runtime role needs `bedrock-agentcore:InvokeAgentRuntime` permission on the MCP runtime.

**Why:** Each AgentCore runtime gets its own IAM role (created by CDK). By default it can only call
Bedrock models — not other runtimes. Without this, cross-runtime calls return 403.

**How to find the agent's role name:**
```bash
# List the CloudFormation stack resources to find the IAM role
AWS_PROFILE=$PROFILE aws cloudformation list-stack-resources \
  --stack-name "AgentCore-<ProjectName>-default" --region $REGION \
  --query "StackResourceSummaries[?ResourceType=='AWS::IAM::Role'].PhysicalResourceId" \
  --output text
```

**How to add the permission:**
```bash
AWS_PROFILE=$PROFILE aws iam put-role-policy \
  --role-name "<agent-runtime-role-name>" \
  --policy-name "AllowInvokeMCPRuntime" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "bedrock-agentcore:InvokeAgentRuntime",
      "Resource": [
        "<MCP_RUNTIME_ARN>",
        "<MCP_RUNTIME_ARN>/*"
      ]
    }]
  }'
```

**Key details:**
- Action is `bedrock-agentcore:InvokeAgentRuntime` (NOT `InvokeRuntime`)
- Resource must include both the ARN and `ARN/*` (for sub-resources like sessions)
- Use an inline policy (not attached managed policy) to avoid breaking the CDK-managed role
- IAM propagation takes 10-30 seconds after applying

**When this is NOT needed:**
- If calling the MCP from your LOCAL machine (your CLI profile already has permissions)
- If the MCP runtime has `networkMode: PUBLIC` and you're using a plain HTTP URL (no SigV4)
- If both runtimes share the same IAM role (unusual)

---

## Path C — Create a New MCP Server from Scratch

Use this when no MCP server exists yet. After creating, deploy it using Path B,
then connect to it using Path A.

**Required info:**
- What to expose (database, API, file system, AWS service)
- Connection details (host, credentials, region)
- Operations needed (read? write? search?)
- Tool names and descriptions (agents use descriptions to decide when to call)

### MCP Server Template (FastMCP)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-server", host="0.0.0.0", port=8000, stateless_http=True)


@mcp.tool()
def query_data(sql: str) -> str:
    """Execute a read-only SQL query and return results as JSON."""
    result = execute_query(sql)
    return json.dumps(result)


@mcp.tool()
def list_tables() -> str:
    """List all available database tables."""
    return json.dumps(get_table_list())


mcp.run(transport="streamable-http")
```

### Key rules

- Server MUST listen on `0.0.0.0:8000/mcp`
- Port MUST be explicitly set: `port=8000` in `FastMCP()` constructor
- MUST use `stateless_http=True`
- MUST use `transport="streamable-http"`
- `mcp.run()` MUST be at module level (NOT inside `if __name__ == "__main__":`) — AgentCore imports the module directly, it does NOT run it as `__main__`
- Tool descriptions are critical — agents use them to decide when to call

### Local Testing

```bash
python my_mcp_server.py

# Test initialize
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# Test list tools
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":2,"params":{}}'
```

Or use MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
# Navigate to http://localhost:6274, connect to http://localhost:8000/mcp
```

Once working locally → follow Path B to deploy.

---

## Post-Integration: Fix pyproject.toml for hatchling

When this skill creates a `mcp_client/` package in the project, hatchling (the build backend) cannot auto-detect what to package because there's no directory matching the project name. **Always** add an explicit build target after creating any new importable package:

```bash
python -c "
from pathlib import Path

toml = Path('pyproject.toml')
content = toml.read_text()

# Add hatch build target if not already present
if '[tool.hatch.build.targets.wheel]' not in content:
    packages = [d.name for d in Path('.').iterdir()
                if d.is_dir() and (d / '__init__.py').exists()
                and d.name not in ('.venv', 'node_modules', 'agentcore')]
    if packages:
        section = f\"\"\"\n[tool.hatch.build.targets.wheel]\npackages = {packages}\n\"\"\"
        if '[build-system]' in content:
            content = content.replace('[build-system]', section + '[build-system]')
        else:
            content += section
        toml.write_text(content)
        print(f'Added hatch build packages: {packages}')
    else:
        print('No packages found — skipping')
else:
    print('Hatch build config already present')
"
```

**Why:** hatchling uses project name to auto-discover packages. If project is `adk-agent-test` but the importable directory is `mcp_client/`, hatchling can't find anything to package → `uv sync` fails. The explicit `packages = ["mcp_client"]` tells it exactly what to include.

Run `uv sync` after this fix to verify it resolves.

---

## Pre-built AWS MCP Servers (github.com/awslabs/mcp)

Before building from scratch, check 35+ pre-built servers:

| Category | Servers |
|----------|---------|
| Databases | DynamoDB, Aurora PostgreSQL/MySQL, DSQL, DocumentDB, Neptune, Keyspaces, Timestream, Redshift |
| AI/ML | Bedrock Knowledge Bases, Kendra, SageMaker, Amazon Q, Translate |
| Compute | ECS, EKS, Lambda, SAM |
| Storage | S3 Tables |
| Infrastructure | CloudFormation/CDK, Cloud Control API |
| Search | OpenSearch |
| DevOps | CodePipeline, Finch/ECR |

To deploy a prebuilt server to AgentCore:

1. Clone: `git clone https://github.com/awslabs/mcp && cp -r mcp/src/<server> ./my-server`
2. Change transport from stdio to streamable-http:
   ```python
   mcp = FastMCP("server-name", host="0.0.0.0", port=8000, stateless_http=True)
   mcp.run(transport="streamable-http")
   ```
3. Add `aws-opentelemetry-distro` + `opentelemetry-instrumentation` to deps
4. `agentcore create --protocol MCP` → copy code → `agentcore deploy --yes`
5. Add IAM permissions to runtime role for accessed AWS services

---

## Common Mistakes

### Connection mistakes (Path A)

| Mistake | What happens | Fix |
|---------|-------------|-----|
| `BedrockModel(model_id=...)` without `boto_session` | `InvalidRegionError` or picks wrong credentials | Always pass `boto_session=boto3.Session(profile_name=..., region_name=...)` |
| Accessing `tool.tool_description` | `AttributeError` — attribute doesn't exist | Use `tool.tool_spec['description']` instead |
| Accessing `tool.description` | `AttributeError` | Use `tool.tool_spec['description']` instead |
| Not wrapping `streamablehttp_client` in a lambda | `TypeError` — MCPClient expects a callable factory | Use `MCPClient(lambda: streamablehttp_client(URL))` |
| Using tools outside `with mcp_client:` block | MCP session is closed, tools won't work | Keep agent creation and invocation inside the `with` block |
| Unicode errors on Windows (cp1252) | `UnicodeEncodeError` when printing non-ASCII from tool results | Add `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")` |
| Missing `import boto3` | `NameError` when constructing `boto_session` | Add `import boto3` at top of file |
| Missing hatch build packages | `uv sync` fails — hatchling can't find package | Add `[tool.hatch.build.targets.wheel]` with packages list |
| ADK agent not named `root_agent` | `ValueError: No root_agent found` | Variable MUST be `root_agent` |
| LiteLlm model ID short form | `BadRequestError: invalid model identifier` | Use full form: `bedrock/us.anthropic.<model>-v1:0` |
| Hardcoded `profile_name="..."` in boto3.Session | `ProfileNotFound` when deployed to AgentCore | Use `os.environ.get("AWS_PROFILE")` — returns None in runtime (uses IAM role), works locally with env var set |

### Deployment mistakes (Path B/C)

| Mistake | What happens | Fix |
|---------|-------------|-----|
| `agentcore create --defaults` | Creates HTTP agent, not MCP server | Use `--protocol MCP` |
| Adding a gateway for MCP server | Gateway doesn't forward MCP traffic | Don't use a gateway; clients connect to runtime URL directly |
| Using Strands `@tool` + BedrockAgentCoreApp | Agent pattern, not MCP server | Use FastMCP `@mcp.tool()` pattern |
| Including `strands-agents`/`bedrock-agentcore` deps | Unnecessary, adds bloat | Only need `mcp` package |
| Missing `aws-opentelemetry-distro` | "OpenTelemetry instrumentation executable not found" error | Add it — required for PYTHON_3_14 runtime |
| Using only `requirements.txt` (no pyproject.toml) | "Building source distributions is disabled" error | Must have `pyproject.toml` with `[build-system]` section |
| Wrong `aws-targets.json` account | Deploys to wrong account | Verify with `aws sts get-caller-identity` |
| Running deploy from `agentcore/` subdir | "Run from project root" error | `cd` to project root first |
| Conflicting AWS env vars | Overrides profile selection | Clear env vars or prefix commands |
| `mcp.run()` inside `if __name__ == "__main__":` | Server never starts on AgentCore (returns 400) | Move `mcp.run(transport="streamable-http")` to module level — AgentCore imports the file, doesn't run it as `__main__` |
| Missing `port=8000` in `FastMCP()` | Server listens on wrong port, AgentCore can't reach it | Always set `port=8000` explicitly in the constructor |
| Agent runtime calling another runtime gets 403 | Missing cross-runtime IAM permission | Add inline policy with `bedrock-agentcore:InvokeAgentRuntime` on the MCP runtime ARN (+ `ARN/*`) to the calling agent's role |
| Using `InvokeRuntime` as the IAM action | Permission still denied (wrong action name) | Correct action is `bedrock-agentcore:InvokeAgentRuntime` |
| Missing `port=8000` in `FastMCP()` | Server listens on wrong port, AgentCore can't reach it | Always set `port=8000` explicitly in the constructor |

---

## Validation Checklist

### Path A — Connecting to MCP server
- [ ] AWS profile and region confirmed with user (Step 0)
- [ ] `aws sts get-caller-identity --profile <profile>` succeeds
- [ ] Dependencies installed (`uv add strands-agents mcp boto3` or framework-specific)
- [ ] Script written with correct profile and region values
- [ ] Script executed and output shown to user
- [ ] Tools discovered (connection works)
- [ ] Agent successfully calls a tool and returns a response

### Path B — Deploying MCP server
- [ ] MCP server listens on `0.0.0.0:8000/mcp`
- [ ] Local test: `initialize` and `tools/list` return correct responses
- [ ] `agentcore.json` has `"protocol": "MCP"` on the runtime
- [ ] No gateway configured (clients connect to runtime URL directly)
- [ ] `agentcore deploy` succeeds
- [ ] `agentcore invoke --runtime <name> --tool <tool> --input '{...}' --json` returns correct result

### Path C — Creating MCP server
- [ ] Server uses `FastMCP` with `host="0.0.0.0"`, `port=8000`, `stateless_http=True`
- [ ] Server runs with `transport="streamable-http"`
- [ ] All tools have descriptive docstrings (agents rely on these)
- [ ] Local curl test for `initialize` and `tools/list` passes