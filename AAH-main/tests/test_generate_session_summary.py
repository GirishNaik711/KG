"""Tests for aah.core.build.generate_session_summary."""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aah.core.build.generate_session_summary import (
    _parse_ts,
    _should_record,
    generate_session_summary,
)
from aah.core.common.progress import (
    add_session_entry,
    get_default_progress,
    load_progress,
    save_progress,
)


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _commit(repo: Path, message: str, filename: str = "f.txt", offset: str | None = None) -> None:
    """Commit `message`. `offset` pins the author-date timezone (e.g. '-0500').

    Pinning the offset keeps cutoff tests deterministic: on a +05:30 machine a
    naive string comparison passes by accident, hiding the defect.
    """
    (repo / filename).write_text(message)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    env = dict(GIT_ENV)
    if offset:
        stamp = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=int(offset[:3]), minutes=int(offset[0] + offset[3:])))
        )
        env["GIT_AUTHOR_DATE"] = stamp.strftime("%Y-%m-%d %H:%M:%S ") + offset
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"]
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo, capture_output=True, check=True, env=env,
    )


def _progress_path(repo: Path) -> Path:
    path = repo / ".aah" / "claude-progress.json"
    save_progress(get_default_progress(), path)
    return path


class TestParseTs:
    def test_parses_strict_iso_with_offset(self):
        parsed = _parse_ts("2026-07-30T16:51:29+05:30")
        assert parsed.tzinfo is not None

    def test_parses_utc_microseconds(self):
        assert _parse_ts("2026-07-30T10:07:47.062854+00:00") is not None

    def test_naive_assumed_utc(self):
        parsed = _parse_ts("2026-07-30T10:07:47")
        assert parsed.tzinfo == timezone.utc

    def test_none_and_garbage(self):
        assert _parse_ts(None) is None
        assert _parse_ts("") is None
        assert _parse_ts("not-a-date") is None

    def test_offsets_compare_correctly(self):
        """16:51+05:30 is 11:21 UTC, so it is LATER than 10:07 UTC.

        The old string compare got this backwards because ' ' < 'T'.
        """
        ist = _parse_ts("2026-07-30T16:51:29+05:30")
        utc = _parse_ts("2026-07-30T10:07:47.062854+00:00")
        assert ist > utc

    def test_string_compare_would_be_wrong(self):
        """Pins the exact defect, independent of the machine's timezone.

        A commit two hours NEWER than the session cutoff sorts as older under
        string comparison, so the filter loop broke on the first commit and
        every summary reported 0 commits. Both the space separator (' ' < 'T')
        and a negative UTC offset trigger it.
        """
        session_ts = "2026-07-30T10:07:47.062854+00:00"

        # %ai form: space separator sorts below 'T'
        assert "2026-07-30 12:07:47 +0000" < session_ts
        assert _parse_ts("2026-07-30 12:07:47 +0000") > _parse_ts(session_ts)

        # %aI form in a western zone: 07:07-05:00 is 12:07 UTC, but sorts lower
        assert "2026-07-30T07:07:47-05:00" < session_ts
        assert _parse_ts("2026-07-30T07:07:47-05:00") > _parse_ts(session_ts)


class TestSessionCutoff:
    """Commits newer than the last session entry must be counted.

    Regression guard: get_log used %ai ("2026-07-30 16:51:29 +0530") which
    string-compared as OLDER than any same-day ISO session timestamp, so the
    filter loop broke immediately and every summary reported 0 commits.
    """

    def test_same_day_later_commit_is_counted(self, git_repo):
        path = _progress_path(git_repo)
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        data = load_progress(path)
        data["session_history"] = [
            {"timestamp": past.isoformat(), "summary": "earlier", "features_completed": []}
        ]
        save_progress(data, path)

        # -0500 makes the wall-clock string sort BELOW the UTC cutoff even
        # though the commit is genuinely newer — the defect, on any machine.
        _commit(git_repo, "feat: add thing", offset="-0500")

        summary = generate_session_summary(git_repo)

        assert summary["commits"] >= 1
        assert "commit(s) made" in summary["summary"]

    def test_commit_before_cutoff_is_excluded(self, git_repo):
        _commit(git_repo, "feat: old work")

        path = _progress_path(git_repo)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        data = load_progress(path)
        data["session_history"] = [
            {"timestamp": future.isoformat(), "summary": "later", "features_completed": []}
        ]
        save_progress(data, path)

        assert generate_session_summary(git_repo)["commits"] == 0

    def test_no_history_counts_all_commits(self, git_repo):
        _progress_path(git_repo)
        _commit(git_repo, "feat: one")
        assert generate_session_summary(git_repo)["commits"] >= 2  # + initial


class TestDecisionsAndPatterns:
    """get_log emits 'subject'; the code read 'message' and always got ''."""

    def test_decision_commit_is_captured(self, git_repo):
        path = _progress_path(git_repo)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        data = load_progress(path)
        data["session_history"] = [
            {"timestamp": past.isoformat(), "summary": "x", "features_completed": []}
        ]
        save_progress(data, path)

        _commit(git_repo, "decision: use postgres over mysql")

        summary = generate_session_summary(git_repo)
        assert any("decision:" in d.lower() for d in summary["decisions_made"])

    def test_pattern_commit_is_captured(self, git_repo):
        path = _progress_path(git_repo)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        data = load_progress(path)
        data["session_history"] = [
            {"timestamp": past.isoformat(), "summary": "x", "features_completed": []}
        ]
        save_progress(data, path)

        _commit(git_repo, "pattern: repositories return domain objects")

        summary = generate_session_summary(git_repo)
        assert any("pattern:" in p.lower() for p in summary["patterns_discovered"])


class TestShouldRecord:
    """The Stop hook fires every turn; only meaningful turns get an entry."""

    def test_records_when_history_empty(self, tmp_path):
        path = _progress_path(tmp_path)
        summary = {"summary": "Progress: 1/1", "commits": 0, "features_completed": []}
        assert _should_record(summary, path) is True

    def test_skips_identical_empty_turn(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "Progress: 15/15 features passing")
        summary = {
            "summary": "Progress: 15/15 features passing",
            "commits": 0,
            "features_completed": [],
            "blockers_resolved": [],
            "decisions_made": [],
            "patterns_discovered": [],
        }
        assert _should_record(summary, path) is False

    def test_records_identical_text_when_commits_present(self, tmp_path):
        """The lossy-digest case: same text, different commits.

        Two turns can render byte-identical summaries while representing
        different commits. Deduping on text alone would drop real work.
        """
        path = _progress_path(tmp_path)
        add_session_entry(path, "2 commit(s) made. Progress: 15/15 features passing")
        summary = {
            "summary": "2 commit(s) made. Progress: 15/15 features passing",
            "commits": 2,
            "features_completed": [],
            "blockers_resolved": [],
        }
        assert _should_record(summary, path) is True

    def test_records_on_state_transition(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "Progress: 13/13 features passing")
        summary = {
            "summary": "Progress: 14/14 features passing",
            "commits": 0,
            "features_completed": [],
            "blockers_resolved": [],
        }
        assert _should_record(summary, path) is True

    def test_records_when_features_completed(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "Progress: 15/15 features passing")
        summary = {
            "summary": "Progress: 15/15 features passing",
            "commits": 0,
            "features_completed": ["F001"],
            "blockers_resolved": [],
        }
        assert _should_record(summary, path) is True

    def test_records_when_blockers_resolved(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "Progress: 15/15 features passing")
        summary = {
            "summary": "Progress: 15/15 features passing",
            "commits": 0,
            "features_completed": [],
            "blockers_resolved": ["flaky test"],
        }
        assert _should_record(summary, path) is True

    def test_records_when_progress_unreadable(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt")
        summary = {"summary": "x", "commits": 0, "features_completed": []}
        assert _should_record(summary, path) is True

    def test_no_tracked_changes_turn_is_skipped(self, tmp_path):
        """The '9x Session with no tracked changes' class from chorus."""
        path = _progress_path(tmp_path)
        add_session_entry(path, "Session with no tracked changes")
        summary = {
            "summary": "Session with no tracked changes",
            "commits": 0,
            "features_completed": [],
            "blockers_resolved": [],
        }
        assert _should_record(summary, path) is False


class TestJournalDedupeIsTheBackstop:
    """_should_record is a pre-filter; add_session_entry has the final say.

    Both layers must hold: a content signal on an otherwise-identical turn
    (e.g. the "adr" substring match on an unrelated commit) makes
    _should_record return True, and the append must still be dropped.
    """

    def test_content_signal_cannot_resurrect_a_duplicate(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "1 commit(s) made. Progress: 15/15 features passing")
        summary = {
            "summary": "1 commit(s) made. Progress: 15/15 features passing",
            "commits": 1,
            "features_completed": [],
            "blockers_resolved": [],
        }
        # Pre-filter says record...
        assert _should_record(summary, path) is True
        # ...but the journal drops the verbatim repeat.
        result = add_session_entry(
            path, summary["summary"], summary["features_completed"]
        )
        assert len(result["session_history"]) == 1

    def test_never_fakes_a_change_by_bumping_a_timestamp(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "same")
        before_ts = load_progress(path)["session_history"][-1]["timestamp"]

        add_session_entry(path, "same")

        assert load_progress(path)["session_history"][-1]["timestamp"] == before_ts

    def test_skipping_leaves_previous_timestamp_intact(self, tmp_path):
        path = _progress_path(tmp_path)
        add_session_entry(path, "Progress: 15/15 features passing")
        before_ts = load_progress(path)["session_history"][-1]["timestamp"]
        before_bytes = path.read_bytes()

        summary = {
            "summary": "Progress: 15/15 features passing",
            "commits": 0,
            "features_completed": [],
            "blockers_resolved": [],
        }
        assert _should_record(summary, path) is False

        assert load_progress(path)["session_history"][-1]["timestamp"] == before_ts
        assert path.read_bytes() == before_bytes
