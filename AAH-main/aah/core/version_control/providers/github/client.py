#!/usr/bin/env python3
"""Thin `gh` CLI wrapper — the ONLY place that touches the network.

Keeping all transport here means the rest of the module is testable against
fixtures with no mocks, and a NO-MOCKS integration test runs the real `gh`
against a throwaway repo. Mirrors the subprocess style of common/git_utils.py.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


class GitHubCLIError(Exception):
    pass


class GitHubOfflineError(GitHubCLIError):
    """Raised on network/auth/rate-limit failures so the engine can go OFFLINE."""


_OFFLINE_MARKERS = (
    "could not resolve host",
    "rate limit",
    "timeout",
    "timed out",
    "connection refused",
    "network is unreachable",
    "503",
    "502",
)


def _label_color(name: str) -> str:
    """Stable-ish color per label family so the repo looks intentional."""
    if name.startswith("aah:"):
        return "1f6feb"
    return "ededed"


class GitHubClient:
    def __init__(self, repo: str):
        if not repo:
            raise GitHubCLIError("GitHub provider requires a 'repo' (owner/name) in config")
        self.repo = repo

    # --- low-level ----------------------------------------------------------
    def _run(self, args: list[str], parse_json: bool = True) -> Any:
        cmd = ["gh", *args, "--repo", self.repo]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            raise GitHubCLIError("gh CLI is not installed or not in PATH")

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            low = stderr.lower()
            if any(m in low for m in _OFFLINE_MARKERS):
                raise GitHubOfflineError(stderr)
            raise GitHubCLIError(f"gh {' '.join(args)} failed: {stderr}")

        if not parse_json:
            return result.stdout
        out = result.stdout.strip()
        if not out:
            return None
        return json.loads(out)

    def validate_auth(self) -> tuple[bool, str]:
        try:
            res = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, check=False,
                encoding="utf-8", errors="replace",
            )
            if res.returncode == 0:
                return True, "gh authenticated"
            return False, (res.stderr or res.stdout or "gh not authenticated").strip()
        except FileNotFoundError:
            return False, "gh CLI is not installed or not in PATH"

    # --- labels -------------------------------------------------------------
    def ensure_labels(self, labels: list[str]) -> None:
        """Create any labels that don't yet exist (idempotent via --force).

        GitHub rejects `issue create/edit --label X` when X doesn't exist, so we
        provision labels up front. --force makes re-running a no-op-ish update.
        Failures here are swallowed: a label we can't create simply won't block
        the issue write (the caller drops it).
        """
        for name in labels:
            try:
                self._run(
                    ["label", "create", name, "--force", "--color", _label_color(name)],
                    parse_json=False,
                )
            except GitHubOfflineError:
                raise
            except GitHubCLIError:
                # Non-fatal: keep going; the issue write will skip unknown labels.
                pass

    # --- issues -------------------------------------------------------------
    _ISSUE_FIELDS = "number,title,body,labels,state,milestone,updatedAt,url,id"

    def get_issue(self, number: int) -> dict | None:
        try:
            return self._run(["issue", "view", str(number), "--json", self._ISSUE_FIELDS])
        except GitHubCLIError as e:
            if "not found" in str(e).lower() or "404" in str(e):
                return None
            raise

    def list_issues(self, since: str | None = None, limit: int = 500) -> list[dict]:
        args = [
            "issue", "list",
            "--state", "all",
            "--limit", str(limit),
            "--json", self._ISSUE_FIELDS,
        ]
        if since:
            args += ["--search", f"updated:>={since}"]
        data = self._run(args)
        return data or []

    # Richer field set for briefing: includes author + comments so the briefer
    # can report who-said-what without a second round-trip per issue.
    _BRIEF_FIELDS = "number,title,body,labels,state,author,createdAt,updatedAt,comments,url"

    def list_by_label(self, label: str, state: str = "open", limit: int = 200) -> list[dict]:
        """Open issues carrying a specific label, with author + comments."""
        data = self._run([
            "issue", "list",
            "--state", state,
            "--label", label,
            "--limit", str(limit),
            "--json", self._BRIEF_FIELDS,
        ])
        return data or []

    def list_all_brief(self, state: str = "open", limit: int = 200) -> list[dict]:
        """All issues (for the given state) with the brief field set.

        The caller filters for the untriaged set (no aah:* label); doing it here
        would bake the label convention into the transport layer.
        """
        data = self._run([
            "issue", "list",
            "--state", state,
            "--limit", str(limit),
            "--json", self._BRIEF_FIELDS,
        ])
        return data or []

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> dict:
        args = ["issue", "create", "--title", title, "--body", body]
        for lbl in labels or []:
            args += ["--label", lbl]
        # `gh issue create` prints the URL, not JSON. Capture URL then fetch.
        url = self._run(args, parse_json=False).strip()
        number = int(url.rstrip("/").split("/")[-1])
        issue = self.get_issue(number)
        return issue or {"number": number, "url": url}

    def update_issue(
        self,
        number: int,
        title: str | None = None,
        body: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> dict | None:
        args = ["issue", "edit", str(number)]
        if title is not None:
            args += ["--title", title]
        if body is not None:
            args += ["--body", body]
        for lbl in add_labels or []:
            args += ["--add-label", lbl]
        for lbl in remove_labels or []:
            args += ["--remove-label", lbl]
        if len(args) > 3:
            self._run(args, parse_json=False)
        return self.get_issue(number)

    def set_state(self, number: int, state: str) -> None:
        """state: 'open' or 'closed'."""
        verb = "close" if state == "closed" else "reopen"
        self._run(["issue", verb, str(number)], parse_json=False)

    # --- comments -----------------------------------------------------------
    def list_comments(self, number: int) -> list[dict]:
        data = self._run(["issue", "view", str(number), "--json", "comments"])
        if not data:
            return []
        return data.get("comments", []) if isinstance(data, dict) else []

    def add_comment(self, number: int, body: str) -> None:
        self._run(["issue", "comment", str(number), "--body", body], parse_json=False)
