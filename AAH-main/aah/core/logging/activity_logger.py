#!/usr/bin/env python3
"""
Session activity logger and token usage aggregator.

Parses the Claude Code transcript JSONL to extract:
1. Activity log — meaningful actions (tool calls, phase changes, skill runs)
2. Token usage — running aggregation of input/output/cache tokens

Called by Stop hook and can be run standalone for reporting.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.io_utils import append_jsonl, read_json, read_jsonl, write_json


ACTIVITY_LOG_FILENAME = "activity-log.jsonl"
TOKEN_USAGE_FILENAME = "token-usage.json"

# Bucket key for tokens spent before any top-level phase skill fired (or in a
# non-AAH session). Kept as an explicit string so it survives JSON round-trips
# where a real None key would be coerced.
_PHASE_NONE_KEY = "(none)"

# Placeholder model name Claude Code stamps on locally-generated (non-API)
# assistant turns — e.g. the "No response requested." stub. These carry zero
# usage and are not a real model, so they're excluded from per-model attribution.
_SYNTHETIC_MODEL = "<synthetic>"


# Totals math: headline totals are the RAW sum of all 4 Anthropic counters
# (see _all_counter_sum) — this is what total_all_tokens and the main/subagent
# origin split use. Caveat (Bug B): the API reports the full prior context in
# input_tokens (and re-reads it in cache_read_input_tokens) on EVERY turn, so
# summing those two across turns counts the same prefix N times. The raw totals
# knowingly carry that inflation in exchange for internal consistency
# (main + subagent == total_all). A double-count-free spend figure would use
# only output_tokens + cache_creation_input_tokens.


# Field naming convention: end-to-end, we use the Anthropic API's canonical
# long names for every counter. Same names appear in the transcript's `usage`
# block AND in the emitted token-usage.json. No short-name aliases anywhere.
_COUNTER_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


# Map top-level AAH phase skill names → phase bucket.
# Only the 6 top-level phase skills flip `active_phase`. Sub-skills (aah-ux,
# aah-mermaid-diagram, aah-codebase-profile, aah-expertise, aah-fix, …) are
# NOT in this map — their tokens roll up into whichever parent phase is
# currently active. Scope stops at build; aah-deploy is not in scope yet.
_SKILL_TO_PHASE = {
    "aah-init-project": "init",
    "aah-discuss":      "discuss",
    "aah-access":       "access",
    "aah-arch":         "architecture",
    "aah-plan":         "plan",
    "aah-build":        "build",
}


# WHAT: returns a fresh dict with all 4 counters set to 0 (long-name schema).
# WHY:  every counter bucket in the module starts from the same shape. A fresh
#       dict each call so mutations don't leak across callers.
def _empty_usage() -> dict:
    """The zero-valued usage bucket everyone shares."""
    return {k: 0 for k in _COUNTER_KEYS}


# WHAT: safely turn a value into an int; anything non-numeric becomes 0.
# WHY:  Anthropic's usage block also contains a NESTED DICT at a key called
#       `cache_creation` (TTL breakdown) — different field from the integer
#       counter `cache_creation_input_tokens`. This guard prevents accidental
#       int(<dict>) crashes if a caller passes the wrong field.
def _coerce_int(v) -> int:
    """Return int(v) if v is number-shaped, else 0."""
    if isinstance(v, (int, float)):
        return int(v)
    return 0


# WHAT: adds each of the 4 Anthropic-API-named counters from `src` into `dst`.
# WHY:  `src` is expected to be either an Anthropic API `usage` dict OR an
#       internal bucket that already uses the same long names. Since we no
#       longer maintain a separate short-name schema, one direct lookup works.
def _add_usage(dst: dict, src: dict) -> None:
    """Accumulate ``src`` counters into ``dst`` in place."""
    for k in _COUNTER_KEYS:
        dst[k] += _coerce_int(src.get(k, 0))



# WHAT: returns the sum of ALL 4 raw counters for a usage dict.
# WHY:  the origin split (main_thread_tokens / subagent_tokens) uses this so
#       main + subagent == total_all_tokens exactly (total_all is itself the
#       raw sum of all 4 counters). Note this inherits the same per-turn
#       input/cache_read double-count that total_all has — it is a raw-counter
#       figure, not the double-count-free _output_based_spend proxy.
def _all_counter_sum(u: dict) -> int:
    """Sum of all 4 raw counters — consistent with total_all_tokens."""
    return sum(_coerce_int(u.get(k, 0)) for k in _COUNTER_KEYS)


# WHAT: given a chronologically-ordered list of per-session per_phase_usage
#       dicts, reattributes each session's leading "(none)" prefix to a real
#       phase, IN PLACE, and returns the same list.
# WHY:  "(none)" only ever holds a session's bootstrap/context-load turns that
#       occurred before its first phase skill fired — conceptually a spill-over
#       of the PREVIOUS session's work. So we fold a session's "(none)" into the
#       last phase of the most recent earlier session that had one. If no such
#       previous session exists (this is the first phased work), we fall back to
#       the FIRST phase invoked within the same session. "(none)" survives only
#       when neither a previous phase nor an in-session phase exists at all.
def fold_leading_none(session_phase_usages: list[dict]) -> list[dict]:
    """Reattribute each session's leading ``(none)`` bucket across sessions.

    ``session_phase_usages`` is a list of ``{phase -> 4-counter bucket}`` dicts,
    ordered oldest → newest. Mutates each dict in place and returns the list.

    Attribution target for a session's ``(none)``:
      1. the last phase (by insertion order) of the most recent PRIOR session
         that had any real phase; else
      2. the first real phase invoked within the SAME session; else
      3. left as ``(none)`` (no phase anywhere to inherit).
    """
    # Track the running "last real phase seen so far" across sessions. Insertion
    # order of a session's dict reflects the chronological order phases appeared
    # (parse_transcript inserts on first touch), so [-1] of the real-phase keys
    # is that session's final phase.
    prev_last_phase: str | None = None
    for usage in session_phase_usages:
        real_phases = [p for p in usage.keys() if p != _PHASE_NONE_KEY]
        none_bucket = usage.get(_PHASE_NONE_KEY)

        if none_bucket is not None:
            target = prev_last_phase
            if target is None and real_phases:
                target = real_phases[0]   # fallback: first phase of THIS session
            if target is not None:
                _add_usage(usage.setdefault(target, _empty_usage()),
                           usage.pop(_PHASE_NONE_KEY))
                # Recompute the real-phase list now that (none) is gone/merged.
                real_phases = [p for p in usage.keys() if p != _PHASE_NONE_KEY]

        # Advance the running pointer to THIS session's last real phase, if any.
        if real_phases:
            prev_last_phase = real_phases[-1]

    return session_phase_usages


# WHAT: opens a transcript JSONL, walks every assistant turn, sums their
#       usage blocks into one 4-counter dict.
# WHY:  used twice — for the main-session transcript AND for each async
#       subagent's own transcript. Same math, different files. Wrapping it in
#       a helper means we can't accidentally use different summing rules.
#       Never raises: a missing / half-written file returns zeros so the
#       hook it runs inside never crashes.
def _sum_usage_from_transcript(transcript_path: Path) -> dict:
    """Sum assistant-turn ``usage`` blocks from a transcript JSONL.

    Used for BOTH the main transcript's assistant turns and each async
    subagent's transcript. Returns an "_empty_usage()"-shaped dict.


    """
    acc = _empty_usage()
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                usage = msg.get("usage") or {}
                _add_usage(acc, usage)
    except OSError:
        # Async agent file might still be flushing at Stop time — do not
        # raise from a hook code path. Count zero and continue.
        pass
    return acc


# WHAT: finds and returns the toolUseResult dict on a "user" transcript entry,
#       or None if the entry isn't reporting an agent result.
# WHY:  when an agent finishes, Claude Code records the result inside a user
#       entry — but Claude Code has changed WHERE it puts that dict over time.
#       Sometimes it's at the entry's top level, sometimes it's inside a
#       content-block. This function normalizes across those locations so the
#       parser doesn't need to care about the layout.
def _extract_tool_use_result(entry: dict) -> dict | None:
    """Return the toolUseResult dict for a user entry, whether it lives at
    the top level or inside a content block. Returns None if not present.
    """
    tur = entry.get("toolUseResult") or entry.get("tool_use_result")
    if isinstance(tur, dict):
        return tur
    msg = entry.get("message") or {}
    if isinstance(msg, dict):
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                c = block.get("content")
                if isinstance(c, dict) and (c.get("agentId") or c.get("agent_id")):
                    return c
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and (b.get("agentId") or b.get("agent_id")):
                            return b
    return None


# WHAT: given a main transcript path, returns the sibling folder that would
#       hold this session's subagent transcripts.
# WHY:  Claude Code stores subagents at a fixed sibling location
#       (<session_id>/subagents/). Centralizing that path derivation here means
#       the parser stays clean and the layout can change in one place if
#       Anthropic ever moves it.
def _subagents_dir_for(transcript_path: Path) -> Path:
    """Sibling directory carrying async subagent transcripts.

    For a main transcript at ``<slug>/<session_id>.jsonl``, subagents live
    under ``<slug>/<session_id>/subagents/agent-<agentId>.jsonl``.
    """
    # Strip the .jsonl extension to get the session-id-named directory
    return transcript_path.parent / transcript_path.stem / "subagents"


# WHAT: given a session_id, scans ~/.claude/projects/*/ for a transcript
#       named "<session_id>.jsonl" and returns its Path (or None).
# WHY:  we deliberately don't store transcript_path in token-usage.json —
#       users shouldn't see internal file paths in an audit report. But
#       models[] rebuild needs to walk older sessions' transcripts, so we
#       need a way to derive the path from just the session_id.
_TRANSCRIPT_PATH_CACHE: dict[str, Path | None] = {}   # session_id -> Path
def _find_transcript_by_session_id(session_id: str) -> Path | None:
    """Locate a Claude transcript by session_id (globs ~/.claude/projects/*/)."""
    if not session_id:
        return None
    if session_id in _TRANSCRIPT_PATH_CACHE:
        return _TRANSCRIPT_PATH_CACHE[session_id]
    try:
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            _TRANSCRIPT_PATH_CACHE[session_id] = None
            return None
        # Glob is fast enough — typical projects dir has < 1000 files across ~20 subdirs.
        matches = list(projects_dir.glob(f"*/{session_id}.jsonl"))
        result = matches[0] if matches else None
    except OSError:
        result = None
    _TRANSCRIPT_PATH_CACHE[session_id] = result
    return result


# WHAT: THE main function. Reads a session's transcript start to end and
#       returns a summary with per-counter totals (Anthropic API long names)
#       plus main_thread_tokens / subagent_tokens scalars + by_agent_type map.
# WHY:  the whole module exists to produce this dict. Everything else in the
#       file either helps it or writes its output. Walking the transcript once
#       is the source of truth for every number that ends up on disk.
def parse_transcript(transcript_path: Path) -> dict:
    """
    Parse a Claude Code transcript JSONL file, including any async subagent
    transcripts referenced from it.

    Returns:
        {
            "session_id": str | None,
            "model": str | None,
            "messages": int,
            "tool_calls": [{"name", "input_summary", "tokens_in", "tokens_out"}],
            "token_usage": {
                # 4 raw counters (Anthropic API long names) — main + subagent combined
                "input_tokens":                int,
                "output_tokens":               int,
                "cache_read_input_tokens":     int,
                "cache_creation_input_tokens": int,
                # 3 derived aggregates
                "total_io_tokens":    int,   # input_tokens + output_tokens
                "total_cache_tokens": int,   # cache_read_input_tokens + cache_creation_input_tokens
                "total_all_tokens":  int,   # sum of all 4 raw counters
                # Split by origin (output-based formula)
                "main_thread_tokens": int,
                "subagent_tokens":    int,
            },
            "by_agent_type": {
                "<agentType>": {"sessions": int, "mode": "sync|async|mixed"},
                ...
            },
            "per_phase_usage": {
                # Per-turn phase attribution — each assistant turn's usage is
                # bucketed under the phase active AT THAT TURN, and each
                # subagent's usage under the phase active when it was dispatched.
                # "(none)" collects turns before the first phase skill fired.
                "<phase>": {4 raw counters},
                ...
            },
        }
    """
    tool_calls: list = []
    main_usage = _empty_usage()
    subagent_usage = _empty_usage()
    message_count = 0
    session_id = None
    model = None
    by_agent_type: dict = {}
    per_model_usage: dict[str, dict] = {}   # model_name -> 4-counter bucket
    per_phase_usage: dict[str, dict] = {}   # phase_name -> 4-counter bucket (per-turn attribution)
    active_phase: str | None = None    # flips only when a top-level phase skill is invoked
    subagents_dir = _subagents_dir_for(transcript_path)

    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if session_id is None:
                session_id = entry.get("sessionId")

            entry_type = entry.get("type", "")

            if entry_type == "assistant":

                message_count += 1
                msg = entry.get("message", {}) or {}
                usage = msg.get("usage") or {}
                _add_usage(main_usage, usage)

                # Phase attribution — primary signal is the `attributionSkill`
                # field on the assistant entry itself. Claude Code stamps this
                # on every turn that runs under a skill. Sub-skills (aah-ux,
                # aah-mermaid-diagram, etc.) also stamp it, but we only flip
                # active_phase when the attribution matches a top-level phase
                # skill; sub-skill attributions fall through the map lookup.
                skill_attr = entry.get("attributionSkill")
                if skill_attr in _SKILL_TO_PHASE:
                    active_phase = _SKILL_TO_PHASE[skill_attr]

                # Per-model attribution: route this turn's usage to its model bucket.
                # Skip the "<synthetic>" placeholder — a non-API, zero-usage stub.
                turn_model = msg.get("model")
                if turn_model and turn_model != _SYNTHETIC_MODEL:
                    _add_usage(per_model_usage.setdefault(turn_model, _empty_usage()), usage)

                if model is None and turn_model and turn_model != _SYNTHETIC_MODEL:
                    model = turn_model

                # Extract tool calls + watch for Skill invocations that flip the active phase.
                for block in msg.get("content", []) or []:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {}) or {}
                    tool_calls.append({
                        "name": tool_name,
                        "input_summary": _summarize_tool_input(tool_name, tool_input),
                        "tokens_in": int(usage.get("input_tokens", 0) or 0),
                        "tokens_out": int(usage.get("output_tokens", 0) or 0),
                    })
                    # Phase attribution — only top-level phase skills change state.
                    # Sub-skills fall through the map lookup and leave active_phase alone.
                    if tool_name == "Skill":
                        skill_name = tool_input.get("skill") if isinstance(tool_input, dict) else None
                        if skill_name in _SKILL_TO_PHASE:
                            active_phase = _SKILL_TO_PHASE[skill_name]

                # Per-phase attribution: route THIS turn's usage to the phase
                # active for the turn (resolved above from attributionSkill and
                # any Skill-invocation flip). Turns before the first phase skill
                # fires bucket under the None key (surfaced as "(none)" on export).
                # This is per-turn, so a session that runs discuss -> arch -> plan
                # splits across three buckets instead of collapsing to the last.
                _add_usage(
                    per_phase_usage.setdefault(active_phase or _PHASE_NONE_KEY, _empty_usage()),
                    usage,
                )

            elif entry_type == "user":

                tur = _extract_tool_use_result(entry)
                if tur is None:
                    continue
                agent_id = tur.get("agentId") or tur.get("agent_id")
                if not agent_id:
                    continue
                agent_type = tur.get("agentType") or "unknown"
                status = tur.get("status")

                agent_bucket = _empty_usage()
                mode: str
                sub_path = subagents_dir / f"agent-{agent_id}.jsonl"
                if status == "completed":
                    # Sync agent — the parent transcript's inline `usage`
                    # only reports the LAST turn of the agent's conversation,
                    # NOT the aggregate across all its turns. Verified: an
                    # Explore agent with 8 turns produced 893 output tokens
                    # cumulatively but the inline `usage` reported only 277.
                    # Solution: prefer walking the sibling subagent transcript
                    # for the true aggregate; fall back to inline `usage`
                    # only if the sub file is missing (e.g. very small agents
                    # or older Claude Code versions where sync agents didn't
                    # persist a separate transcript).
                    if sub_path.exists():
                        sub_usage = _sum_usage_from_transcript(sub_path)
                        _add_usage(agent_bucket, sub_usage)
                        # attributionAgent inside the transcript is authoritative
                        att = _attribution_agent_from_transcript(sub_path)
                        if att:
                            agent_type = att
                    else:
                        inline = tur.get("usage") or {}
                        _add_usage(agent_bucket, inline)
                    mode = "sync"
                elif status == "async_launched":
                    # Async agent — walk the sibling subagent transcript.
                    sub_usage = _sum_usage_from_transcript(sub_path)
                    _add_usage(agent_bucket, sub_usage)
                    # If the subagent transcript records an attributionAgent
                    # that disagrees with the launch stub's agentType, prefer
                    # the transcript's — it's authoritative per findings.
                    att = _attribution_agent_from_transcript(sub_path)
                    if att:
                        agent_type = att
                    mode = "async"
                else:
                    # Unknown status (e.g. still running / errored) — count zero.
                    mode = status or "unknown"

                _add_usage(subagent_usage, agent_bucket)

                # Per-phase attribution for this agent: bucket its tokens under
                # the phase active on the parent turn that dispatched it
                # (`active_phase` still holds the last main-thread flip). Keeps
                # subagent spend in the same phase as the work that spawned it.
                _add_usage(
                    per_phase_usage.setdefault(active_phase or _PHASE_NONE_KEY, _empty_usage()),
                    agent_bucket,
                )

                # Per-model attribution for this agent.
                # Prefer the model reported inside the subagent's own transcript
                # (`message.model`) — it uses the same short alias as main-thread
                # turns (e.g. "claude-opus-4-7"). The parent stub's
                # `resolvedModel` is a fully-qualified Bedrock ARN
                # (e.g. "us.anthropic.claude-opus-4-7[1m]") that refers to the
                # SAME model but under a different name — using it would create
                # duplicate rows in models[] for the same underlying model.
                agent_model = None
                if sub_path.exists():
                    agent_model = _model_from_subagent_transcript(sub_path)
                if not agent_model:
                    agent_model = tur.get("resolvedModel")
                if agent_model and agent_model != _SYNTHETIC_MODEL:
                    _add_usage(
                        per_model_usage.setdefault(agent_model, _empty_usage()),
                        agent_bucket,
                    )

                bucket = by_agent_type.setdefault(agent_type, {
                    "sessions": 0,
                    "mode": mode,
                })
                bucket["sessions"] += 1
                if bucket["mode"] != mode:
                    bucket["mode"] = "mixed"

    # NOTE: per_phase_usage keeps an honest "(none)" bucket here — it holds the
    # leading prefix turns before the session's first phase skill fired. The
    # cross-session reattribution (fold "(none)" into the PREVIOUS session's
    # last phase) happens at the aggregation layer via fold_leading_none(),
    # since it needs multiple sessions in chronological order.

    # Combine main-thread and subagent counters per-counter (long-name schema).
    combined = _empty_usage()
    _add_usage(combined, main_usage)
    _add_usage(combined, subagent_usage)

    total_io_tokens    = combined["input_tokens"]            + combined["output_tokens"]
    total_cache_tokens = combined["cache_read_input_tokens"] + combined["cache_creation_input_tokens"]
    total_all_tokens   = total_io_tokens + total_cache_tokens

    return {
        "session_id": session_id,
        "model": model,
        "messages": message_count,
        "tool_calls": tool_calls,
        "phase": active_phase,   # last active AAH phase in this session (None if no phase skill fired)
        "token_usage": {
            # 4 raw Anthropic-named counters
            "input_tokens":                combined["input_tokens"],
            "output_tokens":               combined["output_tokens"],
            "cache_read_input_tokens":     combined["cache_read_input_tokens"],
            "cache_creation_input_tokens": combined["cache_creation_input_tokens"],
            # 3 derived aggregates
            "total_io_tokens":    total_io_tokens,
            "total_cache_tokens": total_cache_tokens,
            "total_all_tokens":   total_all_tokens,
            # Split by origin (all-4-counter sum → main + subagent == total_all)
            "main_thread_tokens": _all_counter_sum(main_usage),
            "subagent_tokens":    _all_counter_sum(subagent_usage),
        },
        "by_agent_type": by_agent_type,
        "per_model_usage": per_model_usage,   # {model_name -> 4-counter bucket}
        "per_phase_usage": per_phase_usage,   # {phase_name -> 4-counter bucket}; "(none)" = pre-phase turns
    }


# WHAT: reads a subagent transcript to fetch its attributionAgent field
#       (the definitive name of which agent type this transcript belongs to).
# WHY:  the main transcript's launch stub sometimes has a wrong or missing
#       agentType. The subagent's own transcript is authoritative — verified
#       to match 68/68 across real subagent files. Cheaper than opening the
#       companion .meta.json sidecar.
def _attribution_agent_from_transcript(subagent_path: Path) -> str | None:
    """Peek a subagent transcript for its ``attributionAgent`` field.

    Findings verified: this field IS the agentType (68/68 match with
    meta.json). Returns None if the file is missing or unreadable.
    """
    try:
        with open(subagent_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                att = entry.get("attributionAgent")
                if att:
                    return att
    except OSError:
        pass
    return None


# WHAT: reads the model name from the first assistant turn in a subagent
#       transcript. Returns None if the file is missing / unparseable / has
#       no assistant turn with a model field.
# WHY:  async agents' model isn't in the parent's launch stub — it lives in
#       the subagent's own transcript. We need it to attribute the agent's
#       tokens to the correct model bucket in per_model_usage.
def _model_from_subagent_transcript(subagent_path: Path) -> str | None:
    """Peek a subagent transcript for the first assistant turn's model."""
    try:
        with open(subagent_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                model = (entry.get("message") or {}).get("model")
                # Skip the "<synthetic>" placeholder so we keep scanning for the
                # first real model turn instead of latching onto the stub.
                if model and model != _SYNTHETIC_MODEL:
                    return model
    except OSError:
        pass
    return None


# WHAT: renders a short, human-readable string describing what a tool call
#       did (e.g. "Read src/main.py", "Bash git commit -m 'foo'").
# WHY:  the raw tool_use.input is a big JSON dict — useful for the parser but
#       noisy in the activity log. This helper produces a one-line summary
#       for humans reading `activity-log.jsonl` later.
def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Create a short summary of what a tool call did."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:120]
    elif tool_name in ("Write", "Edit"):
        return tool_input.get("file_path", "")[:120]
    elif tool_name == "Read":
        return tool_input.get("file_path", "")[:120]
    elif tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern", "")[:80]
    elif tool_name == "Agent":
        return tool_input.get("description", tool_input.get("prompt", ""))[:120]
    elif tool_name == "Skill":
        return tool_input.get("skill", "")
    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            return questions[0].get("question", "")[:80]
        return ""
    else:
        return str(tool_input)[:80]


# WHAT: filters a raw tool-call list down to just the "meaningful" ones —
#       Skill invocations, Agent spawns, writes/edits, project scripts, etc.
# WHY:  a real session includes hundreds of Reads, Globs, and Greps as
#       exploration noise. The activity log is meant for humans skimming
#       "what did I do?" — drop the noise so only decisions/changes remain.
def filter_meaningful_actions(tool_calls: list[dict]) -> list[dict]:
    """
    Filter tool calls down to meaningful actions.
    Keeps: Bash (rapids scripts), Write/Edit, Agent, Skill, AskUserQuestion.
    Drops: Read, Glob, Grep (exploration noise).
    """
    meaningful = []
    for tc in tool_calls:
        name = tc["name"]
        summary = tc["input_summary"]

        # Always keep these
        if name in ("Agent", "Skill", "AskUserQuestion"):
            meaningful.append(tc)
            continue

        # Keep writes/edits
        if name in ("Write", "Edit"):
            meaningful.append(tc)
            continue

        # Keep Bash only for rapids script calls and git operations
        if name == "Bash":
            if any(kw in summary for kw in [
                "aah run", "aah.core", "git commit", "git merge",
                "git checkout", "pytest", "npm test", "docker",
            ]):
                meaningful.append(tc)
                continue

        # Drop Read, Glob, Grep, etc.

    return meaningful


# WHAT: turns a transcript path into the "activity record" dict that will be
#       appended to activity-log.jsonl and used to update token-usage.json.
# WHY:  wraps parse_transcript + filter_meaningful_actions + a wall-clock
#       timestamp so callers (mainly the Stop/SessionEnd hook) have one
#       function to invoke. Keeps hook code short.
def build_session_activity(transcript_path: Path) -> dict:
    """Build a complete session activity record."""
    parsed = parse_transcript(transcript_path)
    meaningful = filter_meaningful_actions(parsed["tool_calls"])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": parsed["session_id"],
        "model": parsed["model"],
        "phase": parsed.get("phase"),
        "total_messages": parsed["messages"],
        "total_tool_calls": len(parsed["tool_calls"]),
        "meaningful_actions": len(meaningful),
        "actions": [
            {"tool": a["name"], "summary": a["input_summary"]}
            for a in meaningful
        ],
        "token_usage": parsed["token_usage"],
        "by_agent_type": parsed.get("by_agent_type", {}),
        "per_model_usage": parsed.get("per_model_usage", {}),
        "per_phase_usage": parsed.get("per_phase_usage", {}),
    }


# WHAT: reads the existing token-usage.json, adds this session's numbers into
#       it, writes it back. Handles both nested totals + legacy flat fields
#       and merges the by_agent_type breakdown across sessions.
# WHY:  token-usage.json is the persistent aggregate — accumulates over every
#       session forever. This function is the ONLY writer. Callers (Stop and
#       SessionEnd hooks) just hand it this session's usage dict and it
#       maintains the file.
def update_cumulative_usage(
    rapids_path: Path,
    session_usage: dict,
    session_id: str | None = None,
    by_agent_type: dict | None = None,  # accepted but not persisted — dropped from schema
    phase: str | None = None,
    per_model_usage: dict | None = None,
    transcript_path: Path | None = None,
    per_phase_usage: dict | None = None,
) -> dict:
    """Update the running cumulative token usage file. Returns the updated totals.

    ``session_usage`` is the ``parse_transcript(...)["token_usage"]`` dict —
    a flat dict using Anthropic API long names for the 4 raw counters plus 3
    derived aggregates plus main_thread_tokens/subagent_tokens.

    ``per_phase_usage`` is ``parse_transcript(...)["per_phase_usage"]`` —
    ``{phase_name -> 4-counter bucket}`` with per-turn attribution. It is stored
    on the session row as ``by_phase`` (no project-wide phase rollup is written).

    Schema written to ``.aah/audit/token-usage.json``:
        total_all_tokens, total_io_tokens, total_cache_tokens,
        total_input_tokens, total_output_tokens, total_cache_read_tokens,
        total_cache_creation_tokens, session_count, updated_at,
        sessions: [ per-session row (incl. by_phase) ],
        models:   [ per-model rollup ]
    """
    usage_path = rapids_path / "audit" / TOKEN_USAGE_FILENAME
    cumulative = read_json(usage_path) if usage_path.exists() else {}

    # Strip any legacy fields written by earlier builds.
    for legacy in ("totals", "by_agent_type", "totals_definition", "_session_models"):
        cumulative.pop(legacy, None)

    sessions = cumulative.get("sessions", [])
    now = datetime.now(timezone.utc).isoformat()

    # Build the row for this session (long-name schema).
    #
    # If the caller passed the derived aggregates already (from parse_transcript),
    # trust them; otherwise derive from the 4 raw counters. This lets external
    # callers pass just the raw counters and still get consistent totals.
    raw_in = _coerce_int(session_usage.get("input_tokens", 0))
    raw_out = _coerce_int(session_usage.get("output_tokens", 0))
    raw_cr = _coerce_int(session_usage.get("cache_read_input_tokens", 0))
    raw_cc = _coerce_int(session_usage.get("cache_creation_input_tokens", 0))
    derived_io    = raw_in + raw_out
    derived_cache = raw_cr + raw_cc
    derived_all   = derived_io + derived_cache

    # Normalize per_model_usage buckets: coerce values to ints so bad input
    # from a caller can't corrupt aggregation later.
    per_model_clean: dict[str, dict] = {}
    for m, counters in (per_model_usage or {}).items():
        if not isinstance(counters, dict):
            continue
        per_model_clean[m] = {k: _coerce_int(counters.get(k, 0)) for k in _COUNTER_KEYS}

    # Normalize per_phase_usage the same way. Each bucket also carries its 3
    # derived aggregates so the stored session row is self-describing.
    def _phase_bucket(counters: dict) -> dict:
        b = {k: _coerce_int(counters.get(k, 0)) for k in _COUNTER_KEYS}
        b["total_io_tokens"]    = b["input_tokens"] + b["output_tokens"]
        b["total_cache_tokens"] = b["cache_read_input_tokens"] + b["cache_creation_input_tokens"]
        b["total_all_tokens"]   = b["total_io_tokens"] + b["total_cache_tokens"]
        return b

    per_phase_clean: dict[str, dict] = {}
    for ph, counters in (per_phase_usage or {}).items():
        if not isinstance(counters, dict):
            continue
        per_phase_clean[ph] = _phase_bucket(counters)

    new_row = {
        "session_id":                  session_id,
        "timestamp":                   now,
        # 4 raw counters (Anthropic API long names)
        "input_tokens":                raw_in,
        "output_tokens":               raw_out,
        "cache_read_input_tokens":     raw_cr,
        "cache_creation_input_tokens": raw_cc,
        # 3 derived aggregates — recomputed here to guarantee consistency
        "total_io_tokens":             derived_io,
        "total_cache_tokens":          derived_cache,
        "total_all_tokens":            derived_all,
        # Origin split (output-based formula) — trust caller if provided
        "main_thread_tokens":          _coerce_int(session_usage.get("main_thread_tokens", 0)),
        "subagent_tokens":             _coerce_int(session_usage.get("subagent_tokens", 0)),
        # Per-phase attribution for this session (per-turn; "(none)" = pre-phase).
        "by_phase":                    per_phase_clean,
    }
    # Warm the transcript-path cache so the models[] rebuild below finds
    # this session's transcript without an extra directory scan.
    if session_id and transcript_path:
        _TRANSCRIPT_PATH_CACHE[session_id] = transcript_path

    # Migrate any legacy rows that used the old short-name schema by dropping
    # the old-shape rows. Fresh long-name rows will accumulate going forward.
    def _is_new_shape(r: dict) -> bool:
        return "input_tokens" in r and "input" not in r

    sessions = [r for r in sessions if _is_new_shape(r)]

    # Replace-on-write: if this session_id already has a row, overwrite it;
    # otherwise append. Multi-fire idempotency (Phase 1 will exercise this).
    existing_idx = next(
        (i for i, r in enumerate(sessions) if r.get("session_id") == session_id),
        None,
    )
    if existing_idx is not None:
        sessions[existing_idx] = new_row
    else:
        sessions.append(new_row)

    # Cap the list at 50 recent rows to bound file size.
    if len(sessions) > 50:
        sessions = sessions[-50:]

    # Cross-session reattribution of each row's leading "(none)" bucket. Fold in
    # chronological (timestamp) order so a session's prefix inherits the PREVIOUS
    # session's last phase. Mutates each row's by_phase in place.
    _chron = sorted(sessions, key=lambda r: r.get("timestamp") or "")
    _folded = [r.get("by_phase") for r in _chron if isinstance(r.get("by_phase"), dict)]
    fold_leading_none(_folded)
    # _add_usage only touches the 4 raw counters, so recompute each bucket's
    # derived aggregates after any fold merged raw counters into it.
    for by_phase in _folded:
        for b in by_phase.values():
            b["total_io_tokens"]    = b.get("input_tokens", 0) + b.get("output_tokens", 0)
            b["total_cache_tokens"] = b.get("cache_read_input_tokens", 0) + b.get("cache_creation_input_tokens", 0)
            b["total_all_tokens"]   = b["total_io_tokens"] + b["total_cache_tokens"]

    # Rebuild all project-wide aggregates from the sessions list.
    def _sum(field: str) -> int:
        return sum(_coerce_int(r.get(field, 0)) for r in sessions)

    total_input_tokens          = _sum("input_tokens")
    total_output_tokens         = _sum("output_tokens")
    total_cache_read_tokens     = _sum("cache_read_input_tokens")
    total_cache_creation_tokens = _sum("cache_creation_input_tokens")
    total_io_tokens             = total_input_tokens + total_output_tokens
    total_cache_tokens          = total_cache_read_tokens + total_cache_creation_tokens
    total_all_tokens            = total_io_tokens + total_cache_tokens

    # Update the side-index of per-session per-model contributions with the
    # latest data for THIS session (replace-on-write semantics — a repeated
    # fire of the same session_id overwrites its old per-model bucket).
    # ------------------------------------------------------------------
    # Rebuild top-level models[] by walking each session's transcript.
    # ------------------------------------------------------------------
    # The current session's per-model bucket is passed in as `per_model_clean`
    # (already computed by parse_transcript). For OTHER sessions in
    # sessions[], we re-walk their transcripts (each row carries the
    # transcript_path). Cost: N transcript reads per fire, where N = len(sessions).
    # Bounded because sessions[] is capped at 50 rows.
    model_totals: dict[str, dict] = {}

    def _bump(model_name: str, counters: dict) -> None:
        agg = model_totals.setdefault(model_name, {
            "model":    model_name,
            "sessions": 0,
            **{k: 0 for k in _COUNTER_KEYS},
        })
        agg["sessions"] += 1
        for k in _COUNTER_KEYS:
            agg[k] += _coerce_int(counters.get(k, 0))

    for row in sessions:
        row_sid = row.get("session_id")
        if row_sid == session_id and per_model_clean:
            # Current fire — trust the freshly-parsed data.
            for model_name, counters in per_model_clean.items():
                _bump(model_name, counters)
            continue
        # Older sessions — locate their transcript by session_id under
        # ~/.claude/projects/*/ and re-walk it. Result is cached in
        # _TRANSCRIPT_PATH_CACHE so subsequent fires don't re-scan.
        tp = _find_transcript_by_session_id(row_sid) if row_sid else None
        if not tp:
            continue   # Can't attribute without a transcript path.
        try:
            parsed = parse_transcript(tp)
        except Exception:
            continue   # Transcript unreadable — skip; totals still correct.
        for model_name, counters in (parsed.get("per_model_usage") or {}).items():
            if isinstance(counters, dict):
                _bump(model_name, counters)

    # Compute derived aggregates on each model entry.
    for entry in model_totals.values():
        entry["total_io_tokens"]    = entry["input_tokens"] + entry["output_tokens"]
        entry["total_cache_tokens"] = entry["cache_read_input_tokens"] + entry["cache_creation_input_tokens"]
        entry["total_all_tokens"]   = entry["total_io_tokens"] + entry["total_cache_tokens"]

    models_list = sorted(model_totals.values(), key=lambda m: -m["total_all_tokens"])

    # Reorder each session row's keys to match the target schema.
    _row_field_order = (
        "session_id", "timestamp",
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
        "total_io_tokens", "total_cache_tokens", "total_all_tokens",
        "main_thread_tokens", "subagent_tokens", "by_phase",
    )
    sessions_ordered = [
        {k: r[k] for k in _row_field_order if k in r}
        for r in sessions
    ]

    # Build the output dict in the target schema order:
    #   3 aggregates → 4 raw counters → bookkeeping → sessions[] → models[]
    out = {
        "total_all_tokens":            total_all_tokens,
        "total_io_tokens":             total_io_tokens,
        "total_cache_tokens":          total_cache_tokens,
        "total_input_tokens":          total_input_tokens,
        "total_output_tokens":         total_output_tokens,
        "total_cache_read_tokens":     total_cache_read_tokens,
        "total_cache_creation_tokens": total_cache_creation_tokens,
        "session_count":               len(sessions),
        "updated_at":                  now,
        "sessions":                    sessions_ordered,
        "models":                      models_list,
    }
    write_json(out, usage_path)
    return out


# WHAT: appends one entry to activity-log.jsonl (one line per session).
# WHY:  audit trail — token-usage.json only carries totals; the activity log
#       carries what was actually done (meaningful tool calls, agents spawned,
#       skills invoked). Useful for retrospectives and post-hoc debugging.
def log_session_activity(rapids_path: Path, activity: dict) -> None:
    """Append session activity to the activity log."""
    log_path = rapids_path / "audit" / ACTIVITY_LOG_FILENAME
    append_jsonl(activity, log_path)


# WHAT: reads token-usage.json and returns a user-facing report dict.
# WHY:  this is what backs `aah run core.logging.activity_logger usage`.
#       Shapes the on-disk aggregate into a display-friendly form (surfaces
#       nested totals, by_agent_type, and recent sessions) without changing
#       the underlying file.
def get_usage_report(rapids_path: Path) -> dict:
    """Get a formatted usage report."""
    usage_path = rapids_path / "audit" / TOKEN_USAGE_FILENAME
    if not usage_path.exists():
        return {"message": "No usage data yet", "total_all_tokens": 0, "session_count": 0}

    cumulative = read_json(usage_path)
    session_count = cumulative.get("session_count", 0)
    total_all = int(cumulative.get("total_all_tokens", 0) or 0)

    # Strip private underscore-prefixed fields from the recent-sessions slice
    # before surfacing to consumers.
    def _public(row: dict) -> dict:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    return {
        # 3 derived aggregates
        "total_all_tokens":   total_all,
        "total_io_tokens":    cumulative.get("total_io_tokens", 0),
        "total_cache_tokens": cumulative.get("total_cache_tokens", 0),
        # 4 raw counters (Anthropic API long names)
        "total_input_tokens":          cumulative.get("total_input_tokens", 0),
        "total_output_tokens":         cumulative.get("total_output_tokens", 0),
        "total_cache_read_tokens":     cumulative.get("total_cache_read_tokens", 0),
        "total_cache_creation_tokens": cumulative.get("total_cache_creation_tokens", 0),
        # Bookkeeping + recent-history slice
        "session_count":          session_count,
        "avg_tokens_per_session": total_all // max(session_count, 1),
        "recent_sessions":        [_public(r) for r in cumulative.get("sessions", [])[-5:]],
        # Per-model breakdown (list of {model, sessions, 4 raw counters, 3 aggregates})
        "models":                 cumulative.get("models", []),
    }


# WHAT: reads activity-log.jsonl and returns the last N entries.
# WHY:  backs `aah run core.logging.activity_logger activity` for humans
#       checking "what did I do in the last few sessions?" — never rewrites,
#       just tails the file.
def get_activity_report(rapids_path: Path, last_n: int = 20) -> list[dict]:
    """Get recent activity entries."""
    log_path = rapids_path / "audit" / ACTIVITY_LOG_FILENAME
    entries = read_jsonl(log_path)
    return entries[-last_n:]


# WHAT: CLI entry point. Dispatches three subcommands:
#         log-session — called by SessionEnd hook; ingests a transcript
#         usage       — prints the token-usage report
#         activity    — prints the recent activity entries
# WHY:  wraps everything so the hook system (and humans) can trigger the
#       right code path from `aah run core.logging.activity_logger <cmd>`.
def main() -> None:
    parser = argparse.ArgumentParser(description="RAPIDS activity logger and token tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    # Called by Stop hook — processes transcript
    log_p = sub.add_parser("log-session", help="Log session activity from transcript")
    log_p.add_argument("--transcript-path", type=Path, default=None,
                       help="Path to transcript JSONL (read from stdin hook input if not given)")

    # Reporting commands
    sub.add_parser("usage", help="Show cumulative token usage report")
    sub.add_parser("activity", help="Show recent activity log")

    args = parser.parse_args()

    # Resolve rapids path
    from aah.core.common.config import get_active_project_rapids_path
    rapids_path = get_active_project_rapids_path()

    if args.command == "log-session":
        transcript_path = args.transcript_path

        # Try to read from hook stdin if not given
        if transcript_path is None:
            try:
                hook_input = json.load(sys.stdin)
                tp = hook_input.get("transcript_path")
                if tp:
                    transcript_path = Path(tp)
            except (json.JSONDecodeError, EOFError):
                pass

        if transcript_path is None or not transcript_path.exists():
            print("No transcript available", file=sys.stderr)
            sys.exit(0)  # Non-blocking

        if rapids_path is None:
            print("No active project", file=sys.stderr)
            sys.exit(0)

        # Parse and log
        activity = build_session_activity(transcript_path)
        log_session_activity(rapids_path, activity)
        update_cumulative_usage(
            rapids_path,
            activity["token_usage"],
            session_id=activity.get("session_id"),
            phase=activity.get("phase"),
            per_model_usage=activity.get("per_model_usage"),
            transcript_path=transcript_path,
            per_phase_usage=activity.get("per_phase_usage"),
        )

        # Output summary
        usage = activity["token_usage"]
        json.dump({
            "session_logged": True,
            "actions": activity["meaningful_actions"],
            "tokens": {
                "input_tokens":     usage.get("input_tokens", 0),
                "output_tokens":    usage.get("output_tokens", 0),
                "total_all_tokens": usage.get("total_all_tokens", 0),
            },
        }, sys.stdout, indent=2)
        print()

    elif args.command == "usage":
        if rapids_path is None:
            print("No active project", file=sys.stderr)
            sys.exit(1)
        report = get_usage_report(rapids_path)
        json.dump(report, sys.stdout, indent=2)
        print()

    elif args.command == "activity":
        if rapids_path is None:
            print("No active project", file=sys.stderr)
            sys.exit(1)
        entries = get_activity_report(rapids_path)
        json.dump(entries, sys.stdout, indent=2)
        print()

    sys.exit(0)


if __name__ == "__main__":
    main()