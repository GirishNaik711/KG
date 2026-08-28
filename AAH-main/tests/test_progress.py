"""Tests for aah.core.common.progress."""

from pathlib import Path

import pytest

from aah.core.common.progress import (
    add_session_entry,
    find_progress,
    get_default_progress,
    get_summary,
    is_duplicate_session_entry,
    load_progress,
    save_progress,
    snapshot,
    update_progress,
)


class TestGetDefaultProgress:
    def test_returns_correct_structure(self):
        p = get_default_progress()
        assert p["current_phase"] == "init"
        assert p["current_wave"] is None
        assert p["last_completed_feature"] is None
        assert p["in_progress_features"] == []
        assert p["known_issues"] == []
        assert p["session_history"] == []
        assert "last_updated" in p


class TestSaveAndLoadProgress:
    def test_round_trip(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["current_phase"] = "build"
        save_progress(p, path)
        loaded = load_progress(path)
        assert loaded["current_phase"] == "build"

    def test_load_nonexistent_returns_default(self):
        loaded = load_progress(Path("/nonexistent/path.json"))
        assert loaded["current_phase"] == "init"

    def test_save_updates_timestamp(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        save_progress(p, path)
        loaded = load_progress(path)
        assert "last_updated" in loaded

    def test_save_stamps_timestamp_on_real_change(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["last_updated"] = "2020-01-01T00:00:00+00:00"
        save_progress(p, path)
        first = load_progress(path)["last_updated"]
        assert first != "2020-01-01T00:00:00+00:00"

        p["current_phase"] = "build"
        save_progress(p, path)
        assert load_progress(path)["last_updated"] != first


class TestSaveProgressNoOp:
    """save_progress must not rewrite the file when nothing changed.

    claude-progress.json is git-tracked; a restamped `last_updated` is a real
    diff that leaves the file dirty and blocks `git checkout`.
    """

    def test_identical_save_does_not_touch_file(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        save_progress(load_progress(path), path)

        assert path.read_bytes() == before_bytes
        assert path.stat().st_mtime_ns == before_mtime

    def test_stale_timestamp_alone_does_not_trigger_write(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        before = path.read_bytes()

        data = load_progress(path)
        data["last_updated"] = "1999-01-01T00:00:00+00:00"
        save_progress(data, path)

        assert path.read_bytes() == before

    def test_real_change_is_written(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        data = load_progress(path)
        data["current_phase"] = "build"
        save_progress(data, path)
        assert load_progress(path)["current_phase"] == "build"

    def test_force_writes_unconditionally(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        before = load_progress(path)["last_updated"]

        save_progress(load_progress(path), path, force=True)

        assert load_progress(path)["last_updated"] != before

    def test_corrupt_file_is_overwritten(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        save_progress(get_default_progress(), path)
        assert load_progress(path)["current_phase"] == "init"

    def test_noop_leaves_git_tree_clean(self, git_repo):
        """End-to-end: the reported failure mode, at the write layer."""
        import subprocess

        from aah.core.common.git_utils import add_all, commit, is_clean, run_git

        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=git_repo, capture_output=True)

        path = git_repo / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_all(cwd=git_repo)
        commit("Add progress", cwd=git_repo)
        assert is_clean(git_repo)

        # Simulate orchestrator.py:1786 — update_progress(wave=N) at wave N
        update_progress(path, wave=None, phase="init")

        assert is_clean(git_repo), run_git(["status", "--short"], cwd=git_repo).stdout


class TestFindProgress:
    def test_finds_with_explicit_start_dir(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        found = find_progress(start_dir=tmp_path)
        assert found == path

    def test_returns_none_with_empty_start_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert find_progress(start_dir=empty) is None


class TestUpdateProgress:
    def test_update_phase(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        result = update_progress(path, phase="build")
        assert result["current_phase"] == "build"

    def test_update_wave(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        result = update_progress(path, wave=2)
        assert result["current_wave"] == 2

    def test_complete_feature_removes_from_in_progress(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["in_progress_features"] = ["F001", "F002"]
        save_progress(p, path)
        result = update_progress(path, completed_feature="F001")
        assert result["last_completed_feature"] == "F001"
        assert "F001" not in result["in_progress_features"]
        assert "F002" in result["in_progress_features"]

    def test_add_issue(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        result = update_progress(path, issue="Test failure in F003")
        assert "Test failure in F003" in result["known_issues"]

    def test_remove_issue(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["known_issues"] = ["issue1", "issue2"]
        save_progress(p, path)
        result = update_progress(path, remove_issue="issue1")
        assert "issue1" not in result["known_issues"]
        assert "issue2" in result["known_issues"]

    def test_rewrite_same_wave_leaves_file_untouched(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        update_progress(path, wave=2)
        before = path.read_bytes()

        update_progress(path, wave=2)

        assert path.read_bytes() == before

    def test_backward_wave_is_skipped_without_write(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        update_progress(path, wave=3)
        before = path.read_bytes()

        result = update_progress(path, wave=1)

        assert result["current_wave"] == 3
        assert path.read_bytes() == before

    def test_update_multiple_fields(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        result = update_progress(path, phase="build", wave=1, env_state="healthy", next_steps="Start wave 1")
        assert result["current_phase"] == "build"
        assert result["current_wave"] == 1
        assert result["environment_state"] == "healthy"
        assert result["next_steps"] == "Start wave 1"


class TestAddSessionEntry:
    def test_adds_entry(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        result = add_session_entry(path, "Completed F001 and F002", ["F001", "F002"])
        assert len(result["session_history"]) == 1
        assert result["session_history"][0]["summary"] == "Completed F001 and F002"
        assert result["session_history"][0]["features_completed"] == ["F001", "F002"]

    def test_appends_multiple_entries(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "Session 1")
        result = add_session_entry(path, "Session 2")
        assert len(result["session_history"]) == 2


class TestSessionHistoryDedupe:
    """Consecutive verbatim repeats must not enter session_history.

    Chorus accumulated 17 consecutive "Progress: 0/13 features passing" entries
    (claude-progress.json:62-140) differing only by timestamp — no derivable
    information, and each one dirtied a git-tracked file.
    """

    def test_verbatim_repeat_is_skipped(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "Progress: 0/13 features passing")
        result = add_session_entry(path, "Progress: 0/13 features passing")
        assert len(result["session_history"]) == 1

    def test_skip_does_not_touch_the_file(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "Progress: 0/13 features passing")
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        add_session_entry(path, "Progress: 0/13 features passing")

        assert path.read_bytes() == before_bytes
        assert path.stat().st_mtime_ns == before_mtime

    def test_chorus_run_of_seventeen_collapses_to_one(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        for _ in range(17):
            result = add_session_entry(path, "Progress: 0/13 features passing")
        assert len(result["session_history"]) == 1

    def test_differing_features_completed_is_not_a_duplicate(self, tmp_path):
        """Same text, different features — a real difference, must be kept."""
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "Progress: 0/13 features passing", [])
        result = add_session_entry(path, "Progress: 0/13 features passing", ["F001"])
        assert len(result["session_history"]) == 2

    def test_only_the_last_entry_is_compared(self, tmp_path):
        """A→B→A is real history: returning to a state is not a repeat."""
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "Progress: 0/13 features passing")
        add_session_entry(path, "Progress: 1/13 features passing")
        result = add_session_entry(path, "Progress: 0/13 features passing")
        assert len(result["session_history"]) == 3

    def test_skip_returns_current_state(self, tmp_path):
        """Callers read the return value — it must stay usable on skip."""
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["current_phase"] = "build"
        save_progress(p, path)
        add_session_entry(path, "same")
        result = add_session_entry(path, "same")
        assert result["current_phase"] == "build"
        assert len(result["session_history"]) == 1

    def test_allow_duplicate_forces_append(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "same")
        result = add_session_entry(path, "same", allow_duplicate=True)
        assert len(result["session_history"]) == 2

    def test_dedupe_keeps_git_tree_clean(self, git_repo):
        """The checkout-blocking failure mode, at the journal layer."""
        import subprocess

        from aah.core.common.git_utils import add_all, commit, is_clean, run_git

        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=git_repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=git_repo, capture_output=True)

        path = git_repo / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        add_session_entry(path, "Progress: 0/13 features passing")
        add_all(cwd=git_repo)
        commit("Add progress", cwd=git_repo)
        assert is_clean(git_repo)

        add_session_entry(path, "Progress: 0/13 features passing")

        assert is_clean(git_repo), run_git(["status", "--short"], cwd=git_repo).stdout


class TestIsDuplicateSessionEntry:
    def test_empty_history_is_never_duplicate(self):
        assert is_duplicate_session_entry([], "x", []) is False

    def test_matching_summary_and_features(self):
        history = [{"summary": "x", "features_completed": ["F001"]}]
        assert is_duplicate_session_entry(history, "x", ["F001"]) is True

    def test_differing_summary(self):
        history = [{"summary": "x", "features_completed": []}]
        assert is_duplicate_session_entry(history, "y", []) is False

    def test_none_and_empty_features_are_equivalent(self):
        history = [{"summary": "x", "features_completed": []}]
        assert is_duplicate_session_entry(history, "x", None) is True

    def test_missing_features_key_is_treated_as_empty(self):
        """Legacy entries predate features_completed."""
        assert is_duplicate_session_entry([{"summary": "x"}], "x", []) is True


class TestSnapshot:
    def test_creates_snapshot(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["current_phase"] = "build"
        p["current_wave"] = 3
        save_progress(p, path)

        snap_path = snapshot(path)
        assert snap_path.exists()
        assert "progress_snapshot_" in snap_path.name

        from aah.core.common.io_utils import read_json
        snap_data = read_json(snap_path)
        assert snap_data["current_phase"] == "build"
        assert snap_data["current_wave"] == 3
        assert "snapshot_timestamp" in snap_data

    def test_custom_snapshot_dir(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        save_progress(get_default_progress(), path)
        snap_dir = tmp_path / "custom_snapshots"
        snap_path = snapshot(path, snap_dir)
        assert snap_path.parent == snap_dir


class TestGetSummary:
    def test_returns_summary(self, tmp_path):
        path = tmp_path / ".aah" / "claude-progress.json"
        p = get_default_progress()
        p["current_phase"] = "build"
        p["current_wave"] = 2
        p["in_progress_features"] = ["F005"]
        p["known_issues"] = ["Test flake"]
        save_progress(p, path)

        summary = get_summary(path)
        assert summary["phase"] == "build"
        assert summary["wave"] == 2
        assert summary["in_progress"] == ["F005"]
        assert summary["issues"] == ["Test flake"]
        assert summary["total_sessions"] == 0
