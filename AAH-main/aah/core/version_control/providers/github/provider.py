#!/usr/bin/env python3
"""GitHubProvider — the first TrackerProvider adapter.

Wires the gh CLI client and the body/label mapper to the canonical port. Holds
a feature_id <-> issue-number map so to_work_item knows which feature an issue
belongs to (the engine passes refs from the ledger, so identity is never guessed
from titles).
"""

from __future__ import annotations

from aah.core.version_control.models import (
    Checkpoint,
    Comment,
    ItemRef,
    RemoteItem,
    WorkItem,
)
from aah.core.version_control.providers.base import (
    ProviderCapabilities,
    ProviderError,
    ProviderOffline,
    TrackerProvider,
)
from aah.core.version_control.providers.github import mapper
from aah.core.version_control.providers.github.client import (
    GitHubClient,
    GitHubCLIError,
    GitHubOfflineError,
)
from aah.core.version_control.providers import registry


class GitHubProvider(TrackerProvider):
    name = "github"
    capabilities = ProviderCapabilities(
        comments=True, milestones=True, labels=True, dependency_links=True
    )

    def __init__(self, provider_config: dict):
        self.config = provider_config or {}
        self.client = GitHubClient(self.config.get("repo", ""))

    # --- read ---------------------------------------------------------------
    def fetch_item(self, ref: ItemRef) -> RemoteItem | None:
        try:
            issue = self.client.get_issue(ref.number)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))
        if issue is None:
            return None
        # feature_id unknown at this level; caller maps via ledger. Use ref.
        return mapper.remote_item_from_issue(ref.key or "", issue)

    def fetch_changed(
        self, refs_since: dict[str, tuple[ItemRef, Checkpoint]]
    ) -> dict[str, RemoteItem]:
        out: dict[str, RemoteItem] = {}
        try:
            for feature_id, (ref, _cp) in refs_since.items():
                if ref.number is None:
                    continue
                issue = self.client.get_issue(ref.number)
                if issue is not None:
                    out[feature_id] = mapper.remote_item_from_issue(feature_id, issue)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))
        return out

    def fetch_comments(self, ref: ItemRef, since_id: int | None) -> list[Comment]:
        try:
            raw = self.client.list_comments(ref.number)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError:
            return []
        comments: list[Comment] = []
        for c in raw:
            cid = c.get("id") or c.get("databaseId") or 0
            if isinstance(cid, str) and cid.isdigit():
                cid = int(cid)
            if since_id is not None and isinstance(cid, int) and cid <= since_id:
                continue
            author = c.get("author", {})
            comments.append(
                Comment(
                    id=cid if isinstance(cid, int) else 0,
                    author=author.get("login", "") if isinstance(author, dict) else str(author),
                    body=c.get("body", ""),
                    created_at=c.get("createdAt", ""),
                )
            )
        return comments

    # --- write (projection only) -------------------------------------------
    def create_item(self, item: WorkItem) -> ItemRef:
        try:
            issue = self.client.create_issue(
                title=mapper.title_for(item),
                body=mapper.render_body(item),
                labels=mapper.labels_for(item),
            )
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))
        return mapper.ref_from_issue(issue)

    def update_item(self, ref: ItemRef, item: WorkItem) -> RemoteItem:
        try:
            # Fetch current labels so we can remove stale aah:* status labels.
            current = self.client.get_issue(ref.number)
            current_labels = mapper._label_names((current or {}).get("labels", []))
            new_labels = mapper.labels_for(item)
            new_status = next(
                (l for l in new_labels if mapper._label_value(l) in mapper._STATUS_SET), None
            )
            stale = [
                l for l in current_labels
                if mapper.is_aah_label(l)
                and mapper._label_value(l) in mapper._STATUS_SET
                and l != new_status
            ]
            issue = self.client.update_issue(
                ref.number,
                title=mapper.title_for(item),
                body=mapper.render_body(item),
                add_labels=new_labels,
                remove_labels=stale if stale else None,
            )
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))
        if issue is None:
            raise ProviderError(f"issue #{ref.number} vanished during update")
        return mapper.remote_item_from_issue(item.feature_id, issue)

    # --- adapter translation ----------------------------------------------
    def to_work_item(self, remote: object) -> WorkItem:
        # remote is a (feature_id, issue_dict) tuple or RemoteItem.
        if isinstance(remote, RemoteItem):
            return remote.work_item
        feature_id, issue = remote  # type: ignore[misc]
        return mapper.to_work_item(feature_id, issue)

    def from_work_item(self, item: WorkItem) -> object:
        return {
            "title": mapper.title_for(item),
            "body": mapper.render_body(item),
            "labels": mapper.labels_for(item),
        }

    # --- phase briefing (read + lightweight write) -------------------------
    def phase_issues(self, phase: str, state: str = "open") -> list[dict]:
        """Raw issue dicts (brief field set) carrying the aah:<phase> label."""
        try:
            return self.client.list_by_label(mapper.phase_label(phase), state=state)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))

    def unlabeled_issues(self, state: str = "open") -> list[dict]:
        """Open issues carrying NO aah:* label (the untriaged set)."""
        try:
            allissues = self.client.list_all_brief(state=state)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))
        out = []
        for issue in allissues:
            names = mapper._label_names(issue.get("labels"))
            if not any(mapper.is_aah_label(n) for n in names):
                out.append(issue)
        return out

    def create_phase_issue(
        self, phase: str, title: str, body: str, kind: str = "feedback"
    ) -> dict:
        """Open a phase-tagged issue, ensuring its labels exist first."""
        labels = [mapper.phase_label(phase), mapper.kind_label(kind)]
        try:
            self.client.ensure_labels(labels)
            return self.client.create_issue(title=title, body=body, labels=labels)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))

    def comment(self, number: int, body: str) -> None:
        body = f"{body.rstrip()}\n\n---\n_Powered by AAH_"
        try:
            self.client.add_comment(number, body)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))

    # --- one-time setup ----------------------------------------------------
    def provision_labels(self) -> list[str]:
        """Create the static, predefined label set in the repo up front.

        Called once by the `labels` CLI command — NOT on the create/update hot
        path. Idempotent (gh label create --force). Returns the labels created.
        """
        labels = mapper.predefined_labels()
        try:
            self.client.ensure_labels(labels)
        except GitHubOfflineError as e:
            raise ProviderOffline(str(e))
        except GitHubCLIError as e:
            raise ProviderError(str(e))
        return labels

    # --- health ------------------------------------------------------------
    def validate_auth(self) -> tuple[bool, str]:
        return self.client.validate_auth()


registry.register("github", lambda cfg: GitHubProvider(cfg))
