---
name: agentcore-memory
description: AWS Bedrock AgentCore Memory integration patterns — provisioning, namespaces, four built-in strategies (SUMMARIZATION / USER_PREFERENCE / SEMANTIC / EPISODIC), built-in overrides, retrieval with score thresholds, and framework integration (ADK / LangGraph / plain Python). Reference skill consulted by aah-feature-implementer when an AgentCore project needs long-term memory.
user-invocable: true
disable-model-invocation: false
---

# AgentCore Memory

AWS Bedrock AgentCore Memory is a managed event store + extraction service for agentic apps. The app writes turns as events; AgentCore extracts long-term memories asynchronously using built-in strategies. The integration seam is two callbacks — **read before the LLM call, write after the turn** — not a wrapper class.

> **Reference skill.** The 2-callback integration is code the `aah-feature-implementer` writes into the agent — there is no CLI to run. The API calls below are verified against the installed `bedrock_agentcore.memory` SDK (`MemoryClient`).

## When to use

Consult this skill when the project targets AgentCore (deploy route `agentcore`) **and** long-term memory was opted into. The opt-in is the recorded **memory** decision in the decisions registry — `full-semantic-memory` means memory is in scope (`in-context-only` means skip this skill); the enabled extraction strategies come from the recorded **memory extraction strategies** decision (SEMANTIC / SUMMARIZATION / USER_PREFERENCE / …). The current **feature YAML** (`.aah/plan/features/*.yaml`) tells you which feature wires it; the `aah-feature-implementer` does so without asking. Only when invoked **standalone** with nothing recorded should you confirm scope via `AskUserQuestion`. Skip for non-AWS runtimes (use GCP/self-hosted alternatives). *(The Agent Topology doc's memory/state section carries this decision.)*

## AWS setup

```python
from bedrock_agentcore.memory import MemoryClient

mc = MemoryClient(region_name="us-east-1")
```

- **`MemoryClient` does NOT accept a `session` or `boto_session` kwarg.** Pass only `region_name`. It picks up credentials from the default chain (IAM role on AgentCore, or `AWS_PROFILE` env var locally).
- **Do NOT pass `profile_name` to MemoryClient.** It's not supported. The runtime uses its IAM role automatically.
- **Pin the region.** AgentCore is regional; the resource ARN encodes the region.
- **Profile vs env-var precedence trap.** `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in the environment override the default credential chain. If you've exported keys for another account, unset them.
- **IAM scopes the caller needs:** `bedrock-agentcore:*` (data plane) and `bedrock-agentcore-control:*` (control plane).
- **`memoryExecutionRoleArn`** is an *additional* IAM role attached to the memory resource itself. Required only for **custom strategies and built-in overrides** (anything that invokes a Bedrock model on AgentCore's behalf). Not needed for plain built-in strategies.

## Where the resource is provisioned (AAH vs standalone)

**AAH (default): the Memory RESOURCE is provisioned in the DEPLOY phase, not in the app.**
`/aah-deploy` Step 1.7d.1 asks the user which strategies to enable and runs
`aah run core.deploy.agentcore_memory provision …` — which executes this skill's exact
lifecycle (`create_memory_and_wait` + the built-in `add_*_strategy` helpers) **once, outside
the runtime**, then injects **`MEMORY_ID`** into the runtime env. So the app must **use the
existing resource** (`MemoryClient(region_name=…)` + read `MEMORY_ID` from env) — it must NOT
call `create_memory` itself. This sidesteps the cold-start limit entirely.

**Standalone / no deploy provisioning:** if the resource isn't pre-provisioned, fall back to
lazy provisioning on first request (below).

## CRITICAL: Cold-Start Timeout on AgentCore (only relevant to the lazy/standalone path)

AgentCore has a **30-second initialization limit**. If your app doesn't respond to `/ping` within 30s of startup, it's killed.

**Memory provisioning (`create_memory`, `add_strategy`) makes API calls that can take 5-30 seconds.** If placed in a startup/lifespan hook, it will exceed the 30s limit and the runtime will fail with:

```
Error: Runtime initialization time exceeded. Please make sure that initialization completes in 30s.
```

**Solution: Lazy provisioning on first request, NOT at startup.**

```python
app = FastAPI()
_memory_provisioned = False

@app.post("/invocations")
async def invoke(request: Request):
    global _memory_provisioned
    if not _memory_provisioned:
        try:
            provision_memory()
        except Exception as e:
            logger.warning("Memory provisioning deferred: %s", e)
        _memory_provisioned = True
    # ... rest of handler
```

**Rules:**
- NEVER put `provision_memory()` or `create_memory_and_wait()` in a lifespan/startup hook
- NEVER make API calls (boto3, MemoryClient, etc.) during module import or app init
- The `/ping` endpoint must respond immediately with no dependencies
- Provision memory lazily on first invocation, with error handling so the agent still works if memory setup fails

## Memory resource lifecycle

```python
from bedrock_agentcore.memory import MemoryClient

mc = MemoryClient(region_name="us-east-1")

memory = mc.create_memory_and_wait(
    name="my-app-memory",
    strategies=[],   # intentional: built-ins are added via add_strategy, not the constructor
    event_expiry_days=90,
)
memory_id = memory["id"]
```

- The `strategies=[]` empty list **is the API contract.** Built-in strategies are added afterwards via `add_strategy` (next section); custom strategies go in the constructor.
- **Idempotency**: call `mc.get_memory(memory_id)` first; create only if it 404s. AgentCore does not deduplicate by name.
- **Teardown**: `mc.delete_memory_and_wait(memory_id)` — also deletes all events and long-term records.

## Namespaces

Namespaces partition long-term records by user/session/agent. **Always end the namespace with a trailing slash.**

```python
def make_namespace(*parts: str) -> str:
    """Always-trailing-slash to prevent prefix collision (user/123 vs user/1234)."""
    joined = "/".join(p.strip("/") for p in parts if p)
    return f"{joined}/"
```

Granularity levels (pick the coarsest that meets the requirement):

| Level | Example | Use |
|---|---|---|
| User | `user/<id>/` | Preferences that span all sessions |
| Session | `user/<id>/session/<sid>/` | Per-session summaries |
| Agent | `user/<id>/agent/<aid>/` | Multi-agent app, isolate per agent |
| Tenant | `tenant/<tid>/user/<uid>/` | Multi-tenant SaaS |

## Built-in strategies

| Strategy | Extracts | Pick when |
|---|---|---|
| `SUMMARIZATION` | Per-session recap | Next session benefits from "what happened last time" |
| `USER_PREFERENCE` | Stable preferences (language, food, style) | Personalisation across sessions |
| `SEMANTIC` | Discrete facts ("user's dog is named Rex") | Knowledge-base-style recall |
| `EPISODIC` | Time-stamped event narratives | Activity tracking, journaling, audit recall |

```python
existing = {s["type"] for s in mc.get_memory_strategies(memory_id)}
if "USER_PREFERENCE" not in existing:
    mc.add_strategy(memory_id, {
        "userPreferenceMemoryStrategy": {
            "name": "preferences",
            "namespaces": ["user/{actorId}/"],
        }
    })
# Wait for strategy status == ACTIVE before writing events you expect it to extract from.
```

- **Strategies must be ACTIVE before events arrive.** AgentCore does not backfill: events written while a strategy is still `CREATING` are skipped by that strategy.
- **Extraction is asynchronous.** Expect 60–180s between event write and record availability. Do not query long-term records immediately after writing an event.

## Built-in overrides

Override a built-in's prompt or model (e.g., pin summary language, change tone).

**Prefer the SDK helper** — `add_summary_strategy(...)` / `add_semantic_strategy(...)` /
`add_user_preference_strategy(...)` accept a custom prompt/model and build the correct
override payload for you. If you call the low-level `add_strategy` directly, the override
wrapper key is **`summaryConsolidationOverride`** (NOT `summaryOverride`) — see the SDK's
`CUSTOM_CONSOLIDATION_WRAPPER_KEYS` (`semanticConsolidationOverride`,
`summaryConsolidationOverride`, `userPreferenceConsolidationOverride`,
`episodicConsolidationOverride`):

```python
mc.add_strategy(memory_id, {
    "customMemoryStrategy": {
        "name": "english-summary",
        "namespaces": ["/actor/{actorId}/session/{sessionId}/"],
        "configuration": {
            "summaryConsolidationOverride": {
                "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "appendToPrompt": "Always summarize in English regardless of the conversation language.",
            }
        }
    }
})
```

- Requires `memoryExecutionRoleArn` on the memory resource — the override invokes a Bedrock model AgentCore must assume a role to call.
- The `modelId` must be enabled in your Bedrock account/region.

## Writing events

```python
mc.create_event(
    memory_id=memory_id,
    actor_id=user_id,                                # who is speaking
    session_id=session_id,                           # session boundary
    messages=[
        (user_text, "USER"),                         # (text, ROLE) tuple — UPPERCASE role
        (assistant_text, "ASSISTANT"),
    ],
)
```

> **API contract:** `create_event(..., messages=...)` takes a `List[Tuple[str, str]]` of
> `(text, role)` tuples with **UPPERCASE** roles (`"USER"`, `"ASSISTANT"`, `"TOOL"`), NOT
> `{"role": ..., "content": ...}` dicts. Dicts silently fail extraction.

Persist a turn **only after** the assistant response succeeded. Persisting refused, errored, or safety-blocked turns poisons future extraction (the model learns the user "asked X" when really the system refused X).

## Retrieval

Recall API:

- `retrieve_memories(memory_id, namespace=..., query=..., top_k=...)` — semantic search, language-agnostic, returns scored records (`memoryRecordSummaries`). **This is the recall path** — use it both at prompt-time and for diagnostics (a broad query surfaces what's stored).
- There is **no** `list_memory_records(memory_id, namespace)` method. `list_memories(max_results)` lists memory *resources* (not records within a namespace) — don't use it for recall.

```python
records = mc.retrieve_memories(
    memory_id=memory_id,
    namespace=make_namespace("user", user_id),
    query=current_user_message,
    top_k=5,
)
strong = [r for r in records if r["score"] >= 0.4]
```

Score heuristics: **≥0.4 strong**, **0.3–0.4 weak (use with caveat)**, **<0.3 noise (drop)**. Tune `top_k` by namespace size; 3–5 is the usual range.

## Integration patterns

### ADK

Two callbacks, **no Agent subclass needed**:

```python
# before_model_callback: inject long-term memories into the user message
def before_model_callback(callback_context, llm_request):
    records = mc.retrieve_memories(...)
    block = "\n".join(r["content"]["text"] for r in records if r["score"] >= 0.4)
    if block:
        llm_request.contents[-1].parts[0].text = (
            f"[Known preferences:\n{block}\n]\n\n{llm_request.contents[-1].parts[0].text}"
        )

# after_agent_callback: persist the turn
def after_agent_callback(callback_context):
    mc.create_event(memory_id=..., actor_id=..., session_id=..., messages=[...])
```

**Gemini cache-miss warning:** inject preferences into the **user message**, not the system instruction. Mutating the system instruction every turn invalidates Gemini's prompt cache and removes the cost savings.

### LangGraph

A pre-node and a post-node around the LLM call, mutating `state`:

```python
def recall_node(state):
    state["memories"] = mc.retrieve_memories(...)
    return state

def persist_node(state):
    mc.create_event(...)
    return state

graph.add_node("recall", recall_node)
graph.add_node("llm", llm_node)
graph.add_node("persist", persist_node)
graph.add_edge("recall", "llm")
graph.add_edge("llm", "persist")
```

### Plain Python / FastAPI / AgentCore Runtime

```python
def fetch_long_term_block(user_id, query):
    records = mc.retrieve_memories(...)
    return "\n".join(r["content"]["text"] for r in records if r["score"] >= 0.4)

def persist_turn(user_id, session_id, user_text, assistant_text):
    mc.create_event(memory_id=..., actor_id=user_id, session_id=session_id, messages=[...])

# Inside the request handler:
block = fetch_long_term_block(user_id, user_message)
response = llm.complete(system_prompt, f"{block}\n\n{user_message}")
persist_turn(user_id, session_id, user_message, response)
```

For AgentCore Runtime entrypoints:

```python
from bedrock_agentcore_starter_toolkit import BedrockAgentCoreApp
app = BedrockAgentCoreApp()

@app.entrypoint
def invoke(payload):
    user_id, session_id, message = payload["user_id"], payload["session_id"], payload["message"]
    # ... same fetch / call / persist pattern as above
```

## Diagnostics

Drop-in read-only patterns for incident investigation (write each in ~10 lines against boto3 docs):

- **List events for a session** — `mc.list_events(memory_id, session_id)` — confirms turns are landing.
- **Confirm extraction ran** — `mc.retrieve_memories(memory_id, namespace=ns, query="*")` with a broad query — non-empty results mean long-term records exist for that namespace.
- **Semantic search by query** — `mc.retrieve_memories(...)` — repros what the app sees at prompt-time.
- **Cross-session recall by user** — `retrieve_memories` under `/actor/<id>/` and check `session_id` diversity in the returned records — confirms cross-session pickup.

## Anti-patterns

- **Memory provisioning in startup/lifespan hooks.** Exceeds AgentCore's 30s cold-start limit. Use lazy provisioning on first request instead.
- **Passing `session=` or `boto_session=` to MemoryClient.** Not supported. Use `MemoryClient(region_name=...)` only.
- **Passing `profile_name=` to MemoryClient or boto3.Session in deployed code.** Named profiles don't exist on AgentCore runtime. Use default credential chain.
- **Persisting refused/failed turns.** Creates a feedback loop where extraction learns from rejected behavior.
- **PII in `event_metadata`.** The metadata field is not CMK-encrypted the same way event bodies are. Keep PII in `messages` or redact before persisting.
- **Querying records immediately after writing.** Extraction lag is 60–180s. Tests that write then read in the same call will see empty results.
- **Using `AWS_PROFILE` env var while access keys are also exported.** Env-var keys win; you'll silently hit the wrong account.
- **Claiming "memory is dead" without checking the AgentCore Observability counter.** Strategy invocations and failures are emitted as CloudWatch metrics — check there before assuming the SDK is broken.
- **Adding strategies after events.** No backfill — strategies must be ACTIVE before events to extract from them.

## Verification

After wiring the callbacks into an app, sanity-check end-to-end:

```python
mc.create_event(memory_id=memory_id, actor_id="test-user", session_id="s1",
                messages=[("I'm vegetarian.", "USER"),
                          ("Got it!", "ASSISTANT")])
# Wait ~120 seconds for extraction.
records = mc.retrieve_memories(memory_id=memory_id,
                               namespace="user/test-user/",
                               query="dietary restrictions", top_k=3)
assert any("vegetar" in r["content"]["text"].lower() for r in records)
```

Expected log lines from a working integration: `Persisted event evt-...` (write path), `Injected N preference line(s)` (read path). Absence of either means the corresponding callback didn't run.