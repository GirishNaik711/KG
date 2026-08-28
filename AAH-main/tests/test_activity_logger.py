"""Tests for aah.core.logging.activity_logger."""

import json
from pathlib import Path

import pytest

from aah.core.logging.activity_logger import (
    build_session_activity,
    filter_meaningful_actions,
    get_activity_report,
    get_usage_report,
    log_session_activity,
    parse_transcript,
    update_cumulative_usage,
)


@pytest.fixture
def transcript_file(tmp_path):
    """Create a sample transcript JSONL file."""
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "user", "sessionId": "sess-001", "message": {"content": "hello"}},
        {
            "type": "assistant",
            "sessionId": "sess-001",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 300,
                },
                "content": [
                    {"type": "text", "text": "Let me read the file."},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": "sess-001",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 800,
                    "output_tokens": 400,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 100,
                },
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/main.py"}},
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": "sess-001",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 600,
                    "output_tokens": 150,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 0,
                },
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "aah run aah.core.common.manifest read"}},
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": "sess-001",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 0,
                },
                "content": [
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "def main"}},
                ],
            },
        },
    ]
    with open(transcript, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return transcript


@pytest.fixture
def rapids_project(tmp_path):
    """Create a minimal .rapids/ structure for logging tests."""
    rapids = tmp_path / ".rapids" / "audit"
    rapids.mkdir(parents=True)
    return tmp_path / ".rapids"


class TestParseTranscript:
    def test_counts_messages(self, transcript_file):
        result = parse_transcript(transcript_file)
        assert result["messages"] == 4

    def test_aggregates_tokens(self, transcript_file):
        result = parse_transcript(transcript_file)
        usage = result["token_usage"]
        assert usage["input_tokens"] == 500 + 800 + 600 + 300
        assert usage["output_tokens"] == 200 + 400 + 150 + 50
        assert usage["cache_read_input_tokens"] == 1000 + 2000 + 500 + 200

    def test_extracts_tool_calls(self, transcript_file):
        result = parse_transcript(transcript_file)
        assert len(result["tool_calls"]) == 4
        names = [tc["name"] for tc in result["tool_calls"]]
        assert "Read" in names
        assert "Edit" in names
        assert "Bash" in names
        assert "Grep" in names

    def test_session_id(self, transcript_file):
        result = parse_transcript(transcript_file)
        assert result["session_id"] == "sess-001"

    def test_model(self, transcript_file):
        result = parse_transcript(transcript_file)
        assert result["model"] == "claude-sonnet-4-6"


class TestFilterMeaningfulActions:
    def test_keeps_writes(self):
        calls = [{"name": "Edit", "input_summary": "/src/main.py"}]
        assert len(filter_meaningful_actions(calls)) == 1

    def test_keeps_rapids_bash(self):
        calls = [{"name": "Bash", "input_summary": "aah run aah.core.common.manifest read"}]
        assert len(filter_meaningful_actions(calls)) == 1

    def test_drops_reads(self):
        calls = [{"name": "Read", "input_summary": "/src/main.py"}]
        assert len(filter_meaningful_actions(calls)) == 0

    def test_drops_greps(self):
        calls = [{"name": "Grep", "input_summary": "pattern"}]
        assert len(filter_meaningful_actions(calls)) == 0

    def test_keeps_agents(self):
        calls = [{"name": "Agent", "input_summary": "research specialist"}]
        assert len(filter_meaningful_actions(calls)) == 1

    def test_keeps_skills(self):
        calls = [{"name": "Skill", "input_summary": "rapids-research"}]
        assert len(filter_meaningful_actions(calls)) == 1

    def test_drops_random_bash(self):
        calls = [{"name": "Bash", "input_summary": "ls -la"}]
        assert len(filter_meaningful_actions(calls)) == 0


class TestBuildSessionActivity:
    def test_builds_activity(self, transcript_file):
        activity = build_session_activity(transcript_file)
        assert activity["session_id"] == "sess-001"
        assert activity["total_messages"] == 4
        assert activity["total_tool_calls"] == 4
        assert activity["meaningful_actions"] == 2  # Edit + rapids Bash
        assert "token_usage" in activity


class TestCumulativeUsage:
    def test_first_session(self, rapids_project):
        usage = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
        result = update_cumulative_usage(rapids_project, usage)
        assert result["total_input_tokens"] == 1000
        assert result["total_output_tokens"] == 500
        assert result["session_count"] == 1

    def test_accumulates(self, rapids_project):
        usage1 = {"input_tokens": 1000, "output_tokens": 500}
        usage2 = {"input_tokens": 2000, "output_tokens": 800}
        update_cumulative_usage(rapids_project, usage1, session_id="s1")
        result = update_cumulative_usage(rapids_project, usage2, session_id="s2")
        assert result["total_input_tokens"] == 3000
        assert result["total_output_tokens"] == 1300
        # total_all_tokens = input + output + cache_read + cache_creation
        assert result["total_all_tokens"] == 4300
        assert result["session_count"] == 2

    def test_caps_session_history(self, rapids_project):
        for i in range(60):
            update_cumulative_usage(
                rapids_project,
                {"input_tokens": 100, "output_tokens": 50},
                session_id=f"s{i}",     # distinct session_ids so all 60 append
            )
        from aah.core.common.io_utils import read_json
        data = read_json(rapids_project / "audit" / "token-usage.json")
        assert len(data["sessions"]) == 50  # Capped


class TestActivityLog:
    def test_appends_entries(self, rapids_project):
        activity = {"timestamp": "t1", "actions": [{"tool": "Edit", "summary": "file.py"}]}
        log_session_activity(rapids_project, activity)
        log_session_activity(rapids_project, {"timestamp": "t2", "actions": []})
        entries = get_activity_report(rapids_project)
        assert len(entries) == 2


class TestUsageReport:
    def test_empty_report(self, rapids_project):
        report = get_usage_report(rapids_project)
        assert report["total_all_tokens"] == 0

    def test_report_after_sessions(self, rapids_project):
        update_cumulative_usage(
            rapids_project,
            {"input_tokens": 5000, "output_tokens": 2000},
            session_id="s1",
        )
        update_cumulative_usage(
            rapids_project,
            {"input_tokens": 3000, "output_tokens": 1000},
            session_id="s2",
        )
        report = get_usage_report(rapids_project)
        assert report["total_all_tokens"] == 11000
        assert report["session_count"] == 2
        assert report["avg_tokens_per_session"] == 5500


# ---------------------------------------------------------------------------
# Phase 2 — Bugs A / B / C edge-case coverage
# ---------------------------------------------------------------------------
#
# The scenarios below match the edge cases named in token-usage-fix-plan.md:
# * session with no agents        (main-thread-only)
# * sync-only agents              (usage inline in toolUseResult)
# * async agents                  (subagent jsonl summed)
# * async agent file missing      (graceful; count 0, no crash)
# * per-turn input_tokens is NOT  used for totals (Bug B regression guard)
# * cache tokens included         (Bug C regression guard)
# * unparseable line               (parser stays non-blocking)


def _make_asst(usage: dict, model: str = "claude-opus-4-7") -> dict:
    """Build a minimal assistant JSONL entry with the given usage block."""
    return {
        "type": "assistant",
        "sessionId": "sess-phase2",
        "message": {
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": "ok"}],
        },
    }


def _write_transcript(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestPhase2ParserFixes:
    # -- Bug B: per-turn input_tokens is NOT summed into headline total ------

    def test_growing_prefix_not_double_counted(self, tmp_path):
        """5 turns with a growing prefix: sum-of-input_tokens (~2830) must NOT
        drive combined_total_tokens. Output-based total should be sum(output)
        + sum(cache_creation) — new-context tokens only."""
        transcript = tmp_path / "growing-prefix.jsonl"
        entries = [
            _make_asst({"input_tokens": 200,  "output_tokens": 30, "cache_read_input_tokens":    0, "cache_creation_input_tokens": 200}),
            _make_asst({"input_tokens": 350,  "output_tokens": 40, "cache_read_input_tokens":  200, "cache_creation_input_tokens": 150}),
            _make_asst({"input_tokens": 530,  "output_tokens": 50, "cache_read_input_tokens":  350, "cache_creation_input_tokens": 180}),
            _make_asst({"input_tokens": 750,  "output_tokens": 60, "cache_read_input_tokens":  530, "cache_creation_input_tokens": 220}),
            _make_asst({"input_tokens": 1000, "output_tokens": 70, "cache_read_input_tokens":  750, "cache_creation_input_tokens": 250}),
        ]
        _write_transcript(transcript, entries)

        parsed = parse_transcript(transcript)
        tu = parsed["token_usage"]

        # Raw counters sum as reported by the API. input_tokens grows N times
        # in real transcripts but total_all_tokens uses that raw sum honestly.
        assert tu["input_tokens"] == 2830
        assert tu["output_tokens"] == 250
        assert tu["cache_creation_input_tokens"] == 1000
        # main_thread_tokens is the sum of ALL 4 raw counters (so main+sub == total_all)
        assert tu["main_thread_tokens"] == 2830 + 250 + (0 + 200 + 350 + 530 + 750) + 1000
        # total_all_tokens is all 4 raw counters (no exclusions)
        assert tu["total_all_tokens"] == 2830 + 250 + (0 + 200 + 350 + 530 + 750) + 1000

    # -- Bug C: cache_creation tokens ARE in combined total, cache_read isn't -

    def test_cache_creation_included_cache_read_excluded(self, tmp_path):
        transcript = tmp_path / "cache-shape.jsonl"
        entries = [
            _make_asst({"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 500}),
        ]
        _write_transcript(transcript, entries)

        tu = parse_transcript(transcript)["token_usage"]
        # main_thread_tokens is the sum of ALL 4 raw counters
        assert tu["main_thread_tokens"] == 100 + 20 + 50000 + 500
        # cache_read is preserved verbatim as its own raw counter
        assert tu["cache_read_input_tokens"] == 50000
        # total_all_tokens = all 4 raw counters
        assert tu["total_all_tokens"] == 100 + 20 + 50000 + 500

    # -- Bug A: sync agents contribute their inline usage --------------------

    def test_sync_agent_usage_taken_inline(self, tmp_path):
        transcript = tmp_path / "sync-agent.jsonl"
        entries = [
            _make_asst({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            {
                "type": "user",
                "toolUseResult": {
                    "status": "completed",
                    "agentId": "agent-sync-01",
                    "agentType": "port-executor-agent",
                    "usage": {"input_tokens": 3, "output_tokens": 98, "cache_read_input_tokens": 11387, "cache_creation_input_tokens": 187},
                    "totalTokens": 11675,
                },
            },
        ]
        _write_transcript(transcript, entries)

        parsed = parse_transcript(transcript)
        tu = parsed["token_usage"]

        # main_thread_tokens = all 4 raw counters for the main thread: 10+5+0+0
        assert tu["main_thread_tokens"] == 10 + 5 + 0 + 0
        # subagent_tokens = all 4 raw counters for the agent: 3+98+11387+187
        assert tu["subagent_tokens"] == 3 + 98 + 11387 + 187
        # output_tokens now includes both main and subagent
        assert tu["output_tokens"] == 5 + 98
        assert tu["cache_creation_input_tokens"] == 0 + 187

        # by_agent_type breakdown captures it (still returned by parse_transcript,
        # even though it's no longer persisted to token-usage.json)
        assert "port-executor-agent" in parsed["by_agent_type"]
        assert parsed["by_agent_type"]["port-executor-agent"]["mode"] == "sync"
        assert parsed["by_agent_type"]["port-executor-agent"]["sessions"] == 1

    # -- Bug A extension: sync agent WITH a sibling transcript prefers it ----
    # Real-world discovery: an Explore agent's inline `usage` in
    # toolUseResult only carries the LAST TURN's numbers, not the aggregate.
    # The full 8-turn conversation is in the sibling subagent transcript
    # and reports many-fold more tokens (verified: 893 vs 277 output).
    # The parser must prefer the sibling transcript when it exists.

    def test_sync_agent_prefers_sibling_transcript_over_inline(self, tmp_path):
        main_stem = "sync-with-sibling"
        transcript = tmp_path / f"{main_stem}.jsonl"
        sub_dir = tmp_path / main_stem / "subagents"
        sub_dir.mkdir(parents=True)
        sub_file = sub_dir / "agent-explore01.jsonl"

        # Sub transcript: 3 assistant turns
        _write_transcript(sub_file, [
            {"type": "assistant", "attributionAgent": "Explore",
             "message": {"usage": {"input_tokens": 5, "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 500}}},
            {"type": "assistant", "attributionAgent": "Explore",
             "message": {"usage": {"input_tokens": 6, "output_tokens": 400, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 200}}},
            {"type": "assistant", "attributionAgent": "Explore",
             "message": {"usage": {"input_tokens": 8, "output_tokens": 200, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 300}}},
        ])

        # Main transcript: sync agent completes; inline `usage` is
        # deliberately much SMALLER than the sub sum (mimicking real data).
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            {
                "type": "user",
                "toolUseResult": {
                    "status": "completed",
                    "agentId": "explore01",
                    "agentType": "Explore",
                    # Inline usage says just the last turn (200 output + 300 cache_creation)
                    "usage": {"input_tokens": 8, "output_tokens": 200, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 300},
                    "totalTokens": 508,
                },
            },
        ])

        parsed = parse_transcript(transcript)
        tu = parsed["token_usage"]

        # The parser MUST prefer the sibling transcript's sum over the inline's
        # smaller last-turn view. subagent_tokens = all 4 raw counters:
        # input (5+6+8) + output (300+400+200) + cache_read 0 + cache_creation (500+200+300).
        assert tu["subagent_tokens"] == (5 + 6 + 8) + 900 + 0 + 1000
        # by_agent_type reflects the sibling-transcript view (sessions/mode only)
        assert parsed["by_agent_type"]["Explore"]["mode"] == "sync"

    def test_sync_agent_falls_back_to_inline_when_no_sibling(self, tmp_path):
        """If a sync agent has no sibling transcript (older Claude Code
        versions, or very small dispatches), fall back to the inline usage."""
        transcript = tmp_path / "sync-no-sibling.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            {
                "type": "user",
                "toolUseResult": {
                    "status": "completed",
                    "agentId": "explore-tiny",
                    "agentType": "Explore",
                    "usage": {"input_tokens": 1, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 10},
                },
            },
        ])
        # No sub_dir / sub_file created — the sibling doesn't exist.

        parsed = parse_transcript(transcript)
        tu = parsed["token_usage"]

        # Falls back to inline usage; subagent_tokens = all 4 raw counters: 1+50+0+10
        assert tu["subagent_tokens"] == 1 + 50 + 0 + 10

    # -- Bug A: async agent — subagent transcript is walked ------------------

    def test_async_agent_reads_subagent_transcript(self, tmp_path):
        # Main transcript at <slug>/<session>.jsonl; async subagent at
        # <slug>/<session>/subagents/agent-<id>.jsonl (sibling directory
        # named after the main transcript's stem).
        main_stem = "async-main"
        transcript = tmp_path / f"{main_stem}.jsonl"
        sub_dir = tmp_path / main_stem / "subagents"
        sub_dir.mkdir(parents=True)
        sub_file = sub_dir / "agent-abc123.jsonl"

        # Async agent's own transcript — 2 assistant turns
        _write_transcript(sub_file, [
            {
                "type": "assistant",
                "attributionAgent": "aah-feature-implementer",
                "message": {"usage": {"input_tokens": 5, "output_tokens": 400, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 200}},
            },
            {
                "type": "assistant",
                "attributionAgent": "aah-feature-implementer",
                "message": {"usage": {"input_tokens": 8, "output_tokens": 600, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 100}},
            },
        ])
        # Main transcript: one main-thread turn + the async_launched stub
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 15, "output_tokens": 25, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            {
                "type": "user",
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": "abc123",
                    # Even though the launch stub says a different agentType,
                    # the transcript's attributionAgent is authoritative.
                    "agentType": "STALE-agent-name",
                    "resolvedModel": "claude-sonnet-4-5",
                },
            },
        ])

        parsed = parse_transcript(transcript)
        tu = parsed["token_usage"]

        # Async subagent's own tokens got summed and attributed to subagents bucket.
        # subagent_tokens = all 4 raw counters across turns:
        # input (5+8) + output (400+600) + cache_read (3000+5000) + cache_creation (200+100)
        assert tu["subagent_tokens"] == (5 + 8) + (400 + 600) + (3000 + 5000) + (200 + 100)
        # attributionAgent overrode the (stale) launch-stub agentType
        assert "aah-feature-implementer" in parsed["by_agent_type"]
        assert "STALE-agent-name" not in parsed["by_agent_type"]
        assert parsed["by_agent_type"]["aah-feature-implementer"]["mode"] == "async"

    # -- Bug A: missing async subagent file — graceful, count 0 --------------

    def test_missing_async_subagent_file_is_graceful(self, tmp_path):
        """If the async agent is still writing/hasn't flushed when Stop
        fires, its file may not exist. The parser MUST count 0 for that
        agent and not raise — the hook stays non-blocking."""
        transcript = tmp_path / "async-missing.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            {
                "type": "user",
                "toolUseResult": {
                    "status": "async_launched",
                    "agentId": "never-flushed-999",
                    "agentType": "aah-qa-evaluator",
                },
            },
        ])
        # No subagents dir created — the file just isn't there.

        parsed = parse_transcript(transcript)  # must not raise
        tu = parsed["token_usage"]
        assert tu["subagent_tokens"] == 0        # sub file missing → 0
        # main_thread_tokens = all 4 raw counters: 10+20+0+0
        assert tu["main_thread_tokens"] == 10 + 20 + 0 + 0
        # by_agent_type still records the session (sessions/mode only)
        assert parsed["by_agent_type"]["aah-qa-evaluator"]["sessions"] == 1

    # -- Session with no agents at all --------------------------------------

    def test_no_agents_session(self, tmp_path):
        transcript = tmp_path / "no-agents.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 25}),
        ])
        parsed = parse_transcript(transcript)
        tu = parsed["token_usage"]
        assert parsed["by_agent_type"] == {}
        # main_thread_tokens = all 4 raw counters = 100 + 50 + 0 + 25
        assert tu["main_thread_tokens"] == 100 + 50 + 0 + 25
        # no subagent contribution
        assert tu["subagent_tokens"] == 0
        # total_all_tokens = all 4 raw counters
        assert tu["total_all_tokens"] == 100 + 50 + 0 + 25

    # -- Parser stays non-blocking on malformed lines ------------------------

    def test_parser_skips_malformed_lines(self, tmp_path):
        transcript = tmp_path / "malformed.jsonl"
        with open(transcript, "w", encoding="utf-8") as f:
            f.write("not json at all\n")
            f.write(json.dumps(_make_asst({"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})) + "\n")
            f.write("{malformed but starts like json...\n")
        parsed = parse_transcript(transcript)  # must not raise
        assert parsed["token_usage"]["output_tokens"] == 20
        # main_thread_tokens = all 4 raw counters = 10 + 20 + 0 + 0
        assert parsed["token_usage"]["main_thread_tokens"] == 10 + 20 + 0 + 0


class TestPhase2Cumulative:
    """Cumulative-file updates accumulate the new fields across sessions."""

    def test_cumulative_totals_and_derived_aggregates(self, rapids_project):
        """Two sessions accumulate into project-wide totals; derived
        aggregates (total_io_tokens, total_cache_tokens, total_all_tokens)
        recomputed from the sessions list."""
        session_usage_1 = {
            "input_tokens": 15, "output_tokens": 140,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 40,
            "main_thread_tokens": 100 + 30, "subagent_tokens": 40 + 10,
        }
        session_usage_2 = {
            "input_tokens": 20, "output_tokens": 280,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20,
            "main_thread_tokens": 80 + 20, "subagent_tokens": 200,
        }

        update_cumulative_usage(rapids_project, session_usage_1, session_id="s1")
        cum = update_cumulative_usage(rapids_project, session_usage_2, session_id="s2")

        # Raw counter aggregates
        assert cum["total_input_tokens"] == 15 + 20
        assert cum["total_output_tokens"] == 140 + 280
        assert cum["total_cache_read_tokens"] == 0
        assert cum["total_cache_creation_tokens"] == 40 + 20
        # Derived aggregates
        assert cum["total_io_tokens"] == (15 + 20) + (140 + 280)
        assert cum["total_cache_tokens"] == 0 + (40 + 20)
        assert cum["total_all_tokens"] == (15 + 20) + (140 + 280) + 0 + (40 + 20)
        # Bookkeeping
        assert cum["session_count"] == 2
        assert [r["session_id"] for r in cum["sessions"]] == ["s1", "s2"]
        # Per-session origin split preserved verbatim
        assert cum["sessions"][0]["main_thread_tokens"] == 100 + 30
        assert cum["sessions"][0]["subagent_tokens"] == 40 + 10
        # Per-row uses the Anthropic API long-name schema
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            assert k in cum["sessions"][0]

    def test_replace_on_write_multi_fire_idempotent(self, rapids_project):
        """Same session firing many times produces the same file as firing once.
        Enables safe Stop-hook wiring (Phase 1)."""
        usage = {"input_tokens": 100, "output_tokens": 500,
                 "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 2000}

        # Fire once
        first = update_cumulative_usage(rapids_project, usage, session_id="sess-A")

        # Fire 20 more times with the same session_id
        for _ in range(20):
            after_many = update_cumulative_usage(rapids_project, usage, session_id="sess-A")

        assert first["session_count"] == 1
        assert after_many["session_count"] == 1
        assert len(after_many["sessions"]) == 1
        # Numbers identical between single-fire and 21-fire runs
        assert first["total_all_tokens"] == after_many["total_all_tokens"]
        assert first["total_input_tokens"] == after_many["total_input_tokens"]


# ---------------------------------------------------------------------------
# Phase 2 — Phase attribution via Skill tool_use tracking
# ---------------------------------------------------------------------------


def _make_asst_with_skill(usage: dict, skill: str, model: str = "claude-opus-4-7") -> dict:
    """Assistant turn that includes a Skill tool_use block invoking `skill`."""
    return {
        "type": "assistant",
        "sessionId": "sess-phase2",
        "message": {
            "model": model,
            "usage": usage,
            "content": [
                {"type": "text", "text": "invoking skill"},
                {"type": "tool_use", "name": "Skill", "input": {"skill": skill}},
            ],
        },
    }


class TestPhase2PhaseAttribution:
    """Track active AAH phase based on Skill tool_use invocations."""

    def test_phase_none_when_no_skill_invoked(self, tmp_path):
        transcript = tmp_path / "no-skill.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 20,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        ])
        parsed = parse_transcript(transcript)
        assert parsed["phase"] is None

    def test_phase_flips_on_top_level_skill(self, tmp_path):
        """aah-arch → architecture; the turn that invokes it and subsequent
        turns are attributed to `architecture`."""
        transcript = tmp_path / "arch.jsonl"
        _write_transcript(transcript, [
            _make_asst_with_skill({"input_tokens": 50, "output_tokens": 200,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100},
                                  skill="aah-arch"),
            _make_asst({"input_tokens": 30, "output_tokens": 150,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        ])
        parsed = parse_transcript(transcript)
        assert parsed["phase"] == "architecture"

    def test_phase_last_wins_across_multi_phase_session(self, tmp_path):
        """When a session runs /aah-plan then /aah-build, `phase` = 'build'."""
        transcript = tmp_path / "plan-then-build.jsonl"
        _write_transcript(transcript, [
            _make_asst_with_skill({"input_tokens": 20, "output_tokens": 100,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                                  skill="aah-plan"),
            _make_asst({"input_tokens": 25, "output_tokens": 300,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
            _make_asst_with_skill({"input_tokens": 30, "output_tokens": 200,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                                  skill="aah-build"),
        ])
        parsed = parse_transcript(transcript)
        assert parsed["phase"] == "build"

    def test_sub_skill_does_not_change_phase(self, tmp_path):
        """Invoking aah-arch (in-scope) then aah-ux (sub-skill, not in the map)
        keeps active_phase pinned to 'architecture'."""
        transcript = tmp_path / "arch-then-ux.jsonl"
        _write_transcript(transcript, [
            _make_asst_with_skill({"input_tokens": 15, "output_tokens": 80,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                                  skill="aah-arch"),
            _make_asst_with_skill({"input_tokens": 20, "output_tokens": 120,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                                  skill="aah-ux"),  # sub-skill, not in _SKILL_TO_PHASE
            _make_asst_with_skill({"input_tokens": 10, "output_tokens": 50,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                                  skill="aah-mermaid-diagram"),  # another sub-skill
        ])
        parsed = parse_transcript(transcript)
        assert parsed["phase"] == "architecture"

    def test_phase_written_to_session_row(self, rapids_project, tmp_path):
        """parse_transcript + build_session_activity + update_cumulative_usage
        end-to-end: the phase must land on the persisted session row."""
        transcript = tmp_path / "plan.jsonl"
        _write_transcript(transcript, [
            _make_asst_with_skill({"input_tokens": 10, "output_tokens": 40,
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5},
                                  skill="aah-plan"),
        ])
        activity = build_session_activity(transcript)
        assert activity["phase"] == "plan"
        cum = update_cumulative_usage(
            rapids_project,
            activity["token_usage"],
            session_id=activity["session_id"],
            phase=activity["phase"],
            per_phase_usage=activity["per_phase_usage"],
        )
        # Session rows no longer carry a scalar `phase` (the misleading
        # "last skill" value); per-turn attribution lives in `by_phase`.
        assert "phase" not in cum["sessions"][0]
        assert "plan" in cum["sessions"][0]["by_phase"]

    def test_all_six_phases_map_correctly(self, tmp_path):
        """One test transcript per top-level phase skill — every one flips
        active_phase to the expected bucket."""
        cases = [
            ("aah-init-project", "init"),
            ("aah-discuss",      "discuss"),
            ("aah-access",       "access"),
            ("aah-arch",         "architecture"),
            ("aah-plan",         "plan"),
            ("aah-build",        "build"),
        ]
        for skill, expected_phase in cases:
            transcript = tmp_path / f"{skill}.jsonl"
            _write_transcript(transcript, [
                _make_asst_with_skill({"input_tokens": 1, "output_tokens": 1,
                                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                                      skill=skill),
            ])
            parsed = parse_transcript(transcript)
            assert parsed["phase"] == expected_phase, f"{skill} → {parsed['phase']} (expected {expected_phase})"

    def test_phase_detected_via_attributionSkill_field(self, tmp_path):
        """Real Claude Code stamps `attributionSkill` on every assistant turn
        run under a skill. It's the primary signal for phase attribution.
        This test uses no Skill tool_use blocks — only attributionSkill —
        to prove the field-level detection works on its own."""
        transcript = tmp_path / "attribution-only.jsonl"
        entry_1 = _make_asst(
            {"input_tokens": 20, "output_tokens": 100,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 30},
        )
        # Add attributionSkill on the entry itself, NO Skill tool_use in content
        entry_1["attributionSkill"] = "aah-arch"
        entry_2 = _make_asst(
            {"input_tokens": 30, "output_tokens": 200,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 60},
        )
        entry_2["attributionSkill"] = "aah-arch"
        _write_transcript(transcript, [entry_1, entry_2])

        parsed = parse_transcript(transcript)
        assert parsed["phase"] == "architecture"

    def test_attributionSkill_sub_skill_does_not_change_phase(self, tmp_path):
        """A turn running under a sub-skill (e.g. aah-ux while inside aah-arch)
        has attributionSkill='aah-ux', which is NOT in _SKILL_TO_PHASE. It
        must NOT overwrite the active phase set by an earlier aah-arch turn."""
        transcript = tmp_path / "attrib-subskill.jsonl"
        turn_arch = _make_asst(
            {"input_tokens": 10, "output_tokens": 50,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        )
        turn_arch["attributionSkill"] = "aah-arch"
        turn_ux = _make_asst(
            {"input_tokens": 15, "output_tokens": 80,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        )
        turn_ux["attributionSkill"] = "aah-ux"    # sub-skill
        _write_transcript(transcript, [turn_arch, turn_ux])

        parsed = parse_transcript(transcript)
        assert parsed["phase"] == "architecture"  # NOT changed by aah-ux


# ---------------------------------------------------------------------------
# Phase 3 — Per-model token breakdown (top-level models[])
# ---------------------------------------------------------------------------


class TestPhase3PerModelBreakdown:
    """Track and aggregate tokens by model — verified in parse_transcript
    output and in the persisted top-level models[] list."""

    def test_single_model_session(self, tmp_path):
        """A session with all turns on one model — per_model_usage has one entry."""
        transcript = tmp_path / "single-model.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 100,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20},
                       model="claude-opus-4-7"),
            _make_asst({"input_tokens": 15, "output_tokens": 200,
                        "cache_read_input_tokens": 500, "cache_creation_input_tokens": 30},
                       model="claude-opus-4-7"),
        ])
        parsed = parse_transcript(transcript)
        pmu = parsed["per_model_usage"]
        assert list(pmu.keys()) == ["claude-opus-4-7"]
        assert pmu["claude-opus-4-7"]["input_tokens"] == 25
        assert pmu["claude-opus-4-7"]["output_tokens"] == 300
        assert pmu["claude-opus-4-7"]["cache_read_input_tokens"] == 500
        assert pmu["claude-opus-4-7"]["cache_creation_input_tokens"] == 50

    def test_multi_model_session(self, tmp_path):
        """A session that used two models on different turns — each gets its
        own bucket with only its own turns' counters."""
        transcript = tmp_path / "multi-model.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 100,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                       model="claude-opus-4-7"),
            _make_asst({"input_tokens": 20, "output_tokens": 500,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                       model="claude-sonnet-4-5"),
            _make_asst({"input_tokens": 15, "output_tokens": 250,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                       model="claude-opus-4-7"),
        ])
        parsed = parse_transcript(transcript)
        pmu = parsed["per_model_usage"]
        assert set(pmu.keys()) == {"claude-opus-4-7", "claude-sonnet-4-5"}
        # Opus: turns 1 and 3
        assert pmu["claude-opus-4-7"]["input_tokens"] == 25
        assert pmu["claude-opus-4-7"]["output_tokens"] == 350
        # Sonnet: turn 2 only
        assert pmu["claude-sonnet-4-5"]["input_tokens"] == 20
        assert pmu["claude-sonnet-4-5"]["output_tokens"] == 500

    def test_persisted_models_list(self, rapids_project, tmp_path):
        """End-to-end: parse → build_session_activity → update_cumulative_usage;
        the top-level models[] list must be present and correctly aggregated."""
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 100,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20},
                       model="claude-opus-4-7"),
            _make_asst({"input_tokens": 5, "output_tokens": 50,
                        "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10},
                       model="claude-sonnet-4-5"),
        ])
        activity = build_session_activity(transcript)
        cum = update_cumulative_usage(
            rapids_project,
            activity["token_usage"],
            session_id=activity["session_id"],
            phase=activity.get("phase"),
            per_model_usage=activity.get("per_model_usage"),
            transcript_path=transcript,
        )
        # Top-level models[] exists with both models
        model_names = {m["model"] for m in cum["models"]}
        assert model_names == {"claude-opus-4-7", "claude-sonnet-4-5"}
        # Each model has its own counters + derived aggregates
        opus = next(m for m in cum["models"] if m["model"] == "claude-opus-4-7")
        assert opus["sessions"] == 1
        assert opus["input_tokens"] == 10
        assert opus["output_tokens"] == 100
        assert opus["cache_creation_input_tokens"] == 20
        assert opus["total_all_tokens"] == 10 + 100 + 0 + 20
        assert opus["total_io_tokens"] == 10 + 100
        assert opus["total_cache_tokens"] == 0 + 20

    def test_per_model_multi_fire_idempotent(self, rapids_project, tmp_path):
        """Replace-on-write: firing the same session multiple times must NOT
        double-count models[] entries."""
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 100,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20},
                       model="claude-opus-4-7"),
        ])
        activity = build_session_activity(transcript)

        # Fire the SAME session 5 times.
        for _ in range(5):
            cum = update_cumulative_usage(
                rapids_project,
                activity["token_usage"],
                session_id="sess-fixed",
                per_model_usage=activity.get("per_model_usage"),
                transcript_path=transcript,
            )

        assert len(cum["models"]) == 1
        opus = cum["models"][0]
        assert opus["model"] == "claude-opus-4-7"
        assert opus["sessions"] == 1          # 5 fires of the same session → still 1
        assert opus["input_tokens"] == 10     # values from the single session, not 5×

    def test_per_model_two_sessions_accumulate(self, rapids_project, tmp_path):
        """Two different sessions on the same model → sessions=2 and counters summed."""
        # Session A
        t_a = tmp_path / "a.jsonl"
        _write_transcript(t_a, [
            _make_asst({"input_tokens": 10, "output_tokens": 100,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20},
                       model="claude-opus-4-7"),
        ])
        act_a = build_session_activity(t_a)
        update_cumulative_usage(
            rapids_project, act_a["token_usage"],
            session_id="sess-A",
            per_model_usage=act_a.get("per_model_usage"),
            transcript_path=t_a,
        )

        # Session B (different session_id, same model)
        t_b = tmp_path / "b.jsonl"
        _write_transcript(t_b, [
            _make_asst({"input_tokens": 20, "output_tokens": 300,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 40},
                       model="claude-opus-4-7"),
        ])
        act_b = build_session_activity(t_b)
        cum = update_cumulative_usage(
            rapids_project, act_b["token_usage"],
            session_id="sess-B",
            per_model_usage=act_b.get("per_model_usage"),
            transcript_path=t_b,
        )

        assert len(cum["models"]) == 1
        opus = cum["models"][0]
        assert opus["sessions"] == 2
        assert opus["input_tokens"] == 10 + 20
        assert opus["output_tokens"] == 100 + 300
        assert opus["cache_creation_input_tokens"] == 20 + 40

    def test_per_model_field_is_private_underscore_prefixed(self, rapids_project, tmp_path):
        """The _per_model field on session rows is private (underscore-prefixed)
        and must be stripped from get_usage_report's recent_sessions."""
        transcript = tmp_path / "sess.jsonl"
        _write_transcript(transcript, [
            _make_asst({"input_tokens": 10, "output_tokens": 100,
                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20},
                       model="claude-opus-4-7"),
        ])
        activity = build_session_activity(transcript)
        update_cumulative_usage(
            rapids_project, activity["token_usage"],
            session_id="s1", per_model_usage=activity.get("per_model_usage"),
        )
        report = get_usage_report(rapids_project)
        # No underscore-prefixed keys leaked to the public report
        for row in report["recent_sessions"]:
            assert not any(k.startswith("_") for k in row), row