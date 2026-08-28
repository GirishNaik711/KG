#!/usr/bin/env python3
"""
AgentCore Memory provisioning — SDK FALLBACK ONLY.

PRIMARY PATH IS THE CLI. The deploy engineer provisions memory with the native
AgentCore CLI: `agentcore add memory --name <X> --strategies SEMANTIC,SUMMARIZATION`
then `agentcore deploy`. That path also AUTO-INJECTS `MEMORY_<NAME>_ID` into the
runtime env, so no manual env-var step is needed. Prefer it (see
`aah-agentcore-deploy-engineer` Step 2b + the `agentcore-memory` skill).

This script exists only as an SDK fallback for environments where the CLI path
isn't usable (e.g. standalone/no-CLI). It calls the `bedrock_agentcore` MemoryClient
methods (create_memory_and_wait + add_*_strategy_and_wait + list/status/delete),
runs once at deploy time (outside the runtime → no 30s cold-start), and returns a
memory_id. NOTE: unlike the CLI, this does NOT auto-inject the runtime env var —
if you use this fallback you must inject the id yourself (agentcore_deploy set-env).
For strategy CHANGES on an existing memory, use the SDK `update_memory_strategies_and_wait`
(control plane) — verified against the installed `bedrock-agentcore` 1.15.0 SDK; not
`update_memory`, which doesn't exist on `MemoryClient`.

Which strategies to enable is READ from the resolved decision in the aah-discuss
registry (`.aah/discuss/decision-registry.yaml` slugs `agent-memory` +
`memory-strategies`, written by the `agentcore-deploy-interview` skill), not re-asked.

Usage:
    aah run core.deploy.agentcore_memory provision \
        --name MyAppMemory --strategies summarization,user_preference,semantic \
        --region us-east-1 [--event-expiry-days 90] [--namespace "user/{actorId}/"] \
        [--memory-execution-role-arn arn:...]   # only if using overrides/custom
    aah run core.deploy.agentcore_memory status   --memory-id <id> --region us-east-1
    aah run core.deploy.agentcore_memory teardown --memory-id <id> --region us-east-1

Notes:
    - `bedrock-agentcore` (the agent project's SDK) must be importable. Run via the
      project env, or `uv run --with bedrock-agentcore ...`. Lazy-imported so the
      module still loads (for --help) without it.
    - The runtime uses the default credential chain / AWS_PROFILE; MemoryClient takes
      only region_name (per the skill — never a profile/session kwarg).
    - JSON result after ---MEMORY_RESULT_JSON--- for the deploy agent to parse.
"""

import argparse
import json
import os
import sys


# Map the skill's user-facing strategy labels → MemoryClient built-in helpers.
# (SUMMARIZATION / USER_PREFERENCE / SEMANTIC / EPISODIC — the 4 built-ins.)
# Verified against the installed `bedrock-agentcore` 1.15.0 SDK (`dir(MemoryClient)`) —
# all four method names exist exactly as written, including the abbreviated
# `add_summary_strategy_and_wait` (not `..._summarization_...`) for the "summarization"
# label. If a future SDK version renames these, this mapping needs re-verifying the same
# way — don't assume symmetry with the label names.
_STRATEGY_HELPERS = {
    "summarization": "add_summary_strategy_and_wait",
    "summary": "add_summary_strategy_and_wait",
    "user_preference": "add_user_preference_strategy_and_wait",
    "semantic": "add_semantic_strategy_and_wait",
    "episodic": "add_episodic_strategy_and_wait",
}


def _emit(result: dict) -> None:
    print("---MEMORY_RESULT_JSON---")
    json.dump(result, sys.stdout, indent=2)
    print()


def _client(region: str):
    """Lazy-import MemoryClient so the module loads without bedrock-agentcore."""
    try:
        from bedrock_agentcore.memory import MemoryClient
    except ImportError:
        _emit({
            "command": "error",
            "error": "bedrock-agentcore not importable in this environment.",
            "fix": "Run in the agent project env, or: uv run --with bedrock-agentcore "
                   "aah run core.deploy.agentcore_memory ...",
            "status": "sdk_missing",
        })
        sys.exit(1)
    # MemoryClient takes ONLY region_name — no session/profile kwarg (per the skill).
    return MemoryClient(region_name=region)


def _find_existing(mc, name: str) -> str | None:
    """Idempotency: reuse a memory resource with the same name if present."""
    try:
        for m in mc.list_memories(max_results=100):
            if (m.get("name") or m.get("memoryName")) == name:
                return m.get("id") or m.get("memoryId")
    except Exception:
        pass
    return None


def provision(args: argparse.Namespace) -> None:
    region = args.region
    name = args.name
    labels = [s.strip().lower() for s in (args.strategies or "").split(",") if s.strip()]
    unknown = [s for s in labels if s not in _STRATEGY_HELPERS]
    if unknown:
        _emit({"command": "provision", "status": "bad_strategy",
               "error": f"Unknown strategies: {unknown}. Valid: {sorted(set(_STRATEGY_HELPERS))}"})
        sys.exit(1)

    mc = _client(region)

    # 1) Idempotent create (reuse by name; else create_memory_and_wait with empty strategies).
    memory_id = _find_existing(mc, name)
    created = False
    if memory_id:
        print(f"[1/2] Reusing existing memory '{name}' → {memory_id}", flush=True)
    else:
        print(f"[1/2] Creating memory '{name}' (waiting for ACTIVE)", flush=True)
        kwargs = {"name": name, "strategies": [], "event_expiry_days": args.event_expiry_days}
        if args.memory_execution_role_arn:
            kwargs["memory_execution_role_arn"] = args.memory_execution_role_arn
        memory = mc.create_memory_and_wait(**kwargs)
        memory_id = memory.get("id") or memory.get("memoryId")
        created = True
        if not memory_id:
            _emit({"command": "provision", "status": "no_memory_id", "raw": str(memory)[:400]})
            sys.exit(1)

    # 2) Add the selected built-in strategies (idempotent: skip ones already present).
    print(f"[2/2] Ensuring strategies: {labels}", flush=True)
    try:
        existing_types = {(s.get("type") or "").upper() for s in mc.get_memory_strategies(memory_id)}
    except Exception:
        existing_types = set()

    added = []
    ns = [args.namespace] if args.namespace else None
    for label in labels:
        helper_name = _STRATEGY_HELPERS[label]
        # Rough dedupe by canonical type; the helper is also safe to call once per new strategy.
        canonical = {"summarization": "SUMMARIZATION", "summary": "SUMMARIZATION",
                     "user_preference": "USER_PREFERENCE", "semantic": "SEMANTIC",
                     "episodic": "EPISODIC"}[label]
        if canonical in existing_types:
            print(f"  {canonical} already present — skipping", flush=True)
            continue
        helper = getattr(mc, helper_name)
        kw = {"memory_id": memory_id, "name": f"{name}-{label}"}
        if ns:
            kw["namespaces"] = ns
        try:
            helper(**kw)
            added.append(canonical)
            print(f"  Added {canonical}", flush=True)
        except TypeError:
            # Some SDK versions use namespace_templates instead of namespaces.
            kw.pop("namespaces", None)
            if ns:
                kw["namespace_templates"] = ns
            helper(**kw)
            added.append(canonical)
            print(f"  Added {canonical}", flush=True)

    _emit({
        "command": "provision",
        "memory_id": memory_id,
        "created": created,
        "strategies_added": added,
        "region": region,
        "runtime_env": {"MEMORY_ID": memory_id},  # inject into the AgentCore runtime
        "teardown_command": f"aah run core.deploy.agentcore_memory teardown --memory-id {memory_id} --region {region}",
        "status": "provisioned",
    })


def status(args: argparse.Namespace) -> None:
    mc = _client(args.region)
    try:
        st = mc.get_memory_status(args.memory_id)
    except Exception as e:
        _emit({"command": "status", "status": "error", "error": str(e)[:300]})
        sys.exit(1)
    try:
        strategies = [s.get("type") for s in mc.get_memory_strategies(args.memory_id)]
    except Exception:
        strategies = []
    _emit({"command": "status", "memory_id": args.memory_id,
           "memory_status": st, "strategies": strategies})


def teardown(args: argparse.Namespace) -> None:
    mc = _client(args.region)
    print(f"[1/1] Deleting memory {args.memory_id}", flush=True)
    mc.delete_memory_and_wait(args.memory_id)
    _emit({"command": "teardown", "memory_id": args.memory_id, "status": "deleted"})


def main() -> None:
    p = argparse.ArgumentParser(description="AgentCore Memory provisioning (executes the agentcore-memory skill lifecycle)")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("provision", help="Create memory resource + selected built-in strategies (idempotent)")
    pv.add_argument("--name", required=True, help="Memory resource name (PascalCase-ish)")
    pv.add_argument("--strategies", default="",
                    help="Comma list: summarization,user_preference,semantic,episodic")
    pv.add_argument("--region", required=True)
    pv.add_argument("--event-expiry-days", type=int, default=90)
    pv.add_argument("--namespace", default=None, help="Namespace template, e.g. 'user/{actorId}/'")
    pv.add_argument("--memory-execution-role-arn", default=None,
                    help="Only needed for overrides/custom strategies")

    st = sub.add_parser("status", help="Memory status + strategies")
    st.add_argument("--memory-id", required=True)
    st.add_argument("--region", required=True)

    td = sub.add_parser("teardown", help="Delete the memory resource (and all events/records)")
    td.add_argument("--memory-id", required=True)
    td.add_argument("--region", required=True)

    args = p.parse_args()
    {"provision": provision, "status": status, "teardown": teardown}[args.command](args)


if __name__ == "__main__":
    main()
