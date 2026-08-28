---
name: agentcore-memory
description: AWS Bedrock AgentCore Memory integration for agent code — short-term events + long-term strategies (SEMANTIC / SUMMARIZATION / USER_PREFERENCE / EPISODIC), sessions (actor_id / session_id), namespaces, retrieval with score thresholds, and the recall-before / persist-after seam. Reference skill the feature-implementer consults when an AgentCore project needs memory. Provisioning is a DEPLOY-phase CLI step (agentcore add memory); this skill covers the RUNTIME code.
user-invocable: true
disable-model-invocation: false
---

# AgentCore Memory

AWS Bedrock AgentCore Memory is a managed **event store + extraction service**. The app writes
conversation turns as **events** (short-term memory); AgentCore asynchronously extracts **long-term
records** using configured **strategies**. The runtime integration seam is two callbacks — **recall
before the LLM call, persist after the turn** — not a wrapper class.

> **Split of responsibilities (read first):**
> - **PROVISIONING (create the resource + strategies) is done at DEPLOY time via the AgentCore CLI**
>   (`agentcore add memory … && agentcore deploy`) — NOT in this skill and NOT in the app. The exact
>   command lives in the **deploy engineer** (`aah-agentcore-deploy-engineer`, Step 2b); don't
>   duplicate it here.
> - **This skill is the RUNTIME integration** — the recall/persist code the `feature-implementer`
>   writes into the agent. APIs below are verified against `bedrock_agentcore` **1.15.0**
>   (`MemorySessionManager` / `MemoryClient`).

## When to use

Consult this skill when the project targets AgentCore (deploy route `agentcore`) **and** long-term
memory is in scope. The opt-in is **`agent-memory` + `memory-strategies`** (aah-discuss) resolved in `/aah-discuss` —
any value other than `in-context-only` (`in-context-plus-episodic`, `full-semantic-memory`,
`knowledge-graph-memory`, `agent-managed-memory`) means memory is in scope. The current **feature
YAML** tells you which feature wires it; the `feature-implementer` does so without asking. Only when
invoked **standalone** with nothing recorded should you confirm scope via `AskUserQuestion`. Skip for
non-AWS runtimes.

## The invariant: the app USES an existing resource — it never creates one

**The memory resource is provisioned at deploy time; the app only uses it.** After
`agentcore deploy`, the CLI **auto-injects an environment variable `MEMORY_<NAME>_ID`** (uppercase,
underscores — e.g. a memory named `SupportMemory` → `MEMORY_SUPPORTMEMORY_ID`) into the runtime.
**The harness also aliases the same value to a plain `MEMORY_ID`** — read that one. Build-time code
can't reliably predict the exact resource name deploy will choose (deploy derives it mechanically
from the project name; build has no way to know that in advance), so betting on `MEMORY_<NAME>_ID`
means guessing a name that has to match exactly or the lookup silently returns nothing. This is a
real failure that shipped: build assumed `MEMORY_RESEARCH_COMPANION_ID`, deploy actually injected
`MEMORY_SUMMARIZERAPPMEMORY_ID` — neither matched, and the memory client failed at request time. So
the agent code:

- reads the id from env: `os.getenv("MEMORY_ID")` (stable, always present) — `MEMORY_<NAME>_ID` is
  also set if you need the CLI-native name for some other reason, but don't make it the primary key,
- constructs a client/session against it,
- **must NOT call `create_memory` / `create_memory_and_wait`** — provisioning inside the running
  agent makes 5–30s API calls that exceed AgentCore's 30-second cold-start limit and kill the runtime.

To change strategies on an already-created memory, that is a **deploy-side** action via the SDK
(`bedrock-agentcore-control.update_memory`) — not something the app does. (See the deploy engineer.)

**Standalone fallback only:** if you're running outside the AAH deploy phase and no resource is
pre-provisioned, you *may* provision with the SDK (`MemoryClient.create_memory_and_wait`) as a
fallback — but the CLI (`agentcore add memory`) is the primary, supported path.

## Memory tiers & strategies (concepts)

| Tier | What | How (runtime) |
|---|---|---|
| **Short-term** | Raw conversation events (turns), stored as-is | `session.add_turns(...)` → `session.get_last_k_turns(k=...)` |
| **Long-term** | Insights extracted from events **by strategies** | requires a strategy; `session.search_long_term_memories(...)` |

Long-term strategy types (chosen at provision time via the CLI `--strategies` flag):

| Strategy | Extracts | Pick when |
|---|---|---|
| `SEMANTIC` | Discrete facts ("user's order is #35476") | Knowledge-base-style recall |
| `SUMMARIZATION` | Per-session recap | Next session benefits from "what happened last time" |
| `USER_PREFERENCE` | Stable preferences (language, food, style) | Personalisation across sessions |
| `EPISODIC` | Time-stamped event narratives | Activity tracking / journaling / audit recall |
| `CUSTOM` | Your own prompt/model override | Pin summary language, change tone, etc. |

- **No backfill.** Long-term records are only extracted from events written **after** a strategy is
  `ACTIVE`. Events written before are skipped by that strategy.
- **Extraction is asynchronous** — expect **60–180s** between an event write and record availability.
  Don't query records immediately after writing.

## AWS setup

```python
import os
from bedrock_agentcore.memory import MemorySessionManager   # preferred, session-native
# or the lower-level: from bedrock_agentcore.memory import MemoryClient

mgr = MemorySessionManager(memory_id=os.environ["MEMORY_ID"], region_name="us-east-1")
```

- Pass only `region_name` (+ `memory_id`). **Do NOT pass `profile_name`/`session`/`boto_session`** —
  the runtime uses its IAM role automatically; locally it uses the default chain / `AWS_PROFILE`.
- **Pin the region.** AgentCore is regional; the resource ARN encodes it.
- **Env-var precedence trap:** exported `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` override the
  default chain — unset them if they point at another account.
- **IAM the runtime execution role needs:** `bedrock-agentcore:*` (data plane) — scoped to the memory
  **resource ARN** — and `bedrock-agentcore-control:*` (control plane). **Use the wildcard action;
  do NOT hand-enumerate memory actions.** IAM action names are NOT the SDK method names:
  `retrieve_memories`/`create_event` are SDK calls, but the IAM actions differ (e.g. the retrieve
  action is `RetrieveMemoryRecords`, not `RetrieveMemories`). A policy with a guessed action name
  (`RetrieveMemories`, `CreateMemoryEvent`, …) **silently grants nothing** → the runtime is denied at
  request time even though a policy "exists." This mirrors the `mcp-integration` `InvokeRuntime` vs
  `InvokeAgentRuntime` trap. The grant is applied to the **runtime execution role** at deploy (see the
  deploy engineer) — `agentcore add memory` + `agentcore deploy` should wire it via CDK; verify it.

## Sessions & namespaces

AgentCore memory is **session-native**: everything is keyed by **`actor_id`** (who the memory belongs
to) and **`session_id`** (a conversation boundary). `MemorySessionManager` gives you sessions
directly — this is the "identity plumbing" every memory feature needs.

**Namespaces** partition long-term records. Pick the coarsest that meets the requirement:

| Level | Example | Use |
|---|---|---|
| User | `/users/<id>/facts/` | Preferences spanning all sessions |
| Session | `/summaries/<id>/<sid>/` | Per-session summaries |
| Agent | `/users/<id>/agent/<aid>/` | Multi-agent app, isolate per agent |
| Tenant | `/tenant/<tid>/user/<uid>/` | Multi-tenant SaaS |

## Runtime integration — the two callbacks

### Preferred: `MemorySessionManager` (session-native, `bedrock_agentcore` 1.15.0)

```python
import os
from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole

_mgr = MemorySessionManager(memory_id=os.environ["MEMORY_ID"],
                            region_name=os.getenv("AWS_REGION", "us-east-1"))

def handle_turn(actor_id: str, session_id: str, user_text: str) -> str:
    session = _mgr.create_memory_session(actor_id=actor_id, session_id=session_id)

    # 1) RECALL — before the LLM call
    records = session.search_long_term_memories(
        query=user_text, namespace_path="/", top_k=5)
    # MemoryRecord.get(...) — NOT getattr(r, ..., default). MemoryRecord's __getattr__
    # returns None for a missing key instead of raising AttributeError, so getattr's
    # default never triggers (None >= 0.4 then throws TypeError). Use the wrapper's
    # own .get(), which implements the default correctly.
    strong = [r for r in records if r.get("score", 1.0) >= 0.4]
    context = "\n".join(r.get("content", {}).get("text", "") for r in strong)

    # ... call the LLM with `context` injected into the user message ...
    assistant_text = call_llm(context, user_text)

    # 2) PERSIST — after the turn succeeded (only successful turns!)
    session.add_turns(messages=[
        ConversationalMessage(user_text, MessageRole.USER),
        ConversationalMessage(assistant_text, MessageRole.ASSISTANT),
    ])
    return assistant_text
```

- `actor_id` / `session_id` come from the request (e.g. AgentCore `context.session_id` /
  `context.user_id`, or the payload). A stable `actor_id` is what makes cross-session recall work.
- **Persist only successful turns.** Persisting refused/errored/safety-blocked turns poisons future
  extraction (the model "learns" the user asked X when the system actually refused X).
- **Inject recalled context into the user message, not the system prompt** — mutating the system
  prompt every turn invalidates prompt caches (e.g. Gemini) and loses the cost savings.

### Lower-level fallback: `MemoryClient` (still supported in 1.15.0)

If you don't use sessions, the primitive API is `MemoryClient`:

```python
from bedrock_agentcore.memory import MemoryClient
mc = MemoryClient(region_name="us-east-1")

mc.create_event(memory_id=MEMORY_ID, actor_id=user_id, session_id=session_id,
                messages=[(user_text, "USER"), (assistant_text, "ASSISTANT")])   # (text, ROLE) tuples, UPPERCASE
records = mc.retrieve_memories(memory_id=MEMORY_ID, namespace=f"/users/{user_id}/",
                               query=user_text, top_k=5)
```

> **`create_event` message contract:** `List[Tuple[str, str]]` of `(text, ROLE)` with **UPPERCASE**
> roles (`"USER"`, `"ASSISTANT"`, `"TOOL"`), NOT `{"role":..,"content":..}` dicts (dicts silently
> fail extraction). Prefer `MemorySessionManager` for new code.

**Do not invent a parameter shape for `create_event`/`retrieve_memories`/`list_events`.** Both APIs
above are verified against the installed `bedrock-agentcore` 1.15.0 SDK — copy the signatures
verbatim. A real bug from this exact mistake: code called `create_event(event_type=..., role=...,
payload={"text": ...})` — a shape that matches neither `MemoryClient.create_event`'s real
`messages=[(text, ROLE)]` signature above nor the raw AWS `CreateEvent` API (which needs
`memoryId`/`actorId`/`sessionId`/`eventTimestamp`/`payload` as a list of `{"conversational": {...}}`
structures). Every call failed with `ParamValidationError` — chat worked, nothing was ever saved.
If you're holding a `MemorySessionManager`/`MemorySession` object rather than a `MemoryClient`,
`create_event`/`retrieve_memory_records`/`list_events` are NOT hand-written convenience methods —
they're dynamically forwarded straight to the raw boto3 client (see `_ALLOWED_DATA_PLANE_METHODS` in
the SDK's `memory/session.py`), so they demand the exact raw AWS shape, not the friendly one. Use
`session.add_turns`/`search_long_term_memories`/`get_last_k_turns`/`list_long_term_memory_records`
instead — never call the raw forwarded methods directly.

Score heuristics (both APIs): **≥0.4 strong**, **0.3–0.4 weak (use with caveat)**, **<0.3 drop**.
`top_k` 3–5 is the usual range.

## Framework notes

- **Strands** has a native integration (`bedrock_agentcore.memory.integrations.strands` —
  `AgentCoreMemorySessionManager` + `AgentCoreMemoryConfig`) that wires recall/persist for you; use
  it when the framework is Strands.
- **LangGraph / plain FastAPI / other:** wire the two callbacks manually around
  `MemorySessionManager` (recall in a pre-node / before the LLM call; persist in a post-node / after).
  The session manager itself is framework-agnostic.

## Cold-start (relevant to the standalone/lazy path ONLY)

In AAH the resource is provisioned at deploy (outside the runtime), so cold-start is a non-issue
(see "The invariant" above for why the app must never call `create_memory` itself). It only matters
if you fall back to standalone lazy provisioning — avoid the same import/lifespan/`/ping` hooks; if
you must lazy-provision, do it on the **first `/invocations` request**, guarded by a flag, with error
handling.

## Diagnostics

- **Confirm turns landed** — `session.get_last_k_turns(k=...)` (short-term).
- **Confirm extraction ran** — `session.search_long_term_memories(query="*", namespace_path="/")`
  (or `list_long_term_memory_records(namespace_path="/")`) — non-empty means records exist.
- **Cross-session recall** — search under `/users/<id>/` and check `session_id` diversity in results.
- **Observability** — strategy invocations/failures are emitted as CloudWatch metrics (AgentCore
  Observability). Check there before concluding "memory is broken."

## Anti-patterns

- **Silent graceful degradation.** Wrapping recall/persist in a bare `try/except` that returns
  `""`/no-ops **without logging loudly** hides a dead feature: if the SDK import fails (e.g.
  `bedrock-agentcore` not packaged) or a memory call errors, the app keeps returning 200s while
  memory does *nothing* — invisibly. Degrade gracefully for the *user*, but **log at ERROR** on any
  memory init/call failure (include the exception), so a dead memory is observable in CloudWatch, not
  a silent no-op. (Also: declare `bedrock-agentcore` in `pyproject.toml` so the import can't fail in
  the packaged runtime — see the deploy `validate-prereqs` dependency check.)
- **Hand-rolling `create_memory`/`add_strategy`, calling it inside the running agent, or betting on
  `MEMORY_<NAME>_ID` instead of `MEMORY_ID`.** See "The invariant" above — all three break the same
  way (30s cold-start kill, or a silent name mismatch at request time).
- **Hand-naming memory IAM actions on the runtime role.** SDK method ≠ IAM action; a guessed action
  (`RetrieveMemories`, `CreateMemoryEvent`) grants nothing → runtime denied even with a policy
  attached. Grant `bedrock-agentcore:*` on the memory resource ARN (wildcard), not enumerated actions.
- **`create_event` with dict messages** — must be `(text, "USER")` tuples (use `MemorySessionManager`
  to avoid the trap).
- **Persisting refused/failed turns** — poisons extraction.
- **Querying records immediately after writing** — 60–180s extraction lag.
- **Adding strategies after events and expecting backfill** — no backfill; strategy must be ACTIVE
  first.
- **PII in event metadata** — metadata isn't CMK-encrypted like event bodies; keep PII in `messages`
  or redact.

## Verification (end-to-end, standalone)

```python
import time
from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole

mgr = MemorySessionManager(memory_id=MEMORY_ID, region_name="us-east-1")
s = mgr.create_memory_session(actor_id="test-user", session_id="s1")
s.add_turns(messages=[ConversationalMessage("I'm vegetarian.", MessageRole.USER),
                      ConversationalMessage("Got it!", MessageRole.ASSISTANT)])
time.sleep(120)  # extraction lag
hits = s.search_long_term_memories(query="dietary restrictions", namespace_path="/", top_k=3)
assert any("vegetar" in getattr(r, "content", {}).get("text", "").lower() for r in hits)
```
