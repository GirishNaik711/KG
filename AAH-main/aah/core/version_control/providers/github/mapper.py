#!/usr/bin/env python3
"""WorkItem <-> GitHub issue mapping.

The issue body is rendered from the WorkItem as sectioned Markdown, and parsed
back into the same fields. The render/parse pair MUST round-trip: a body we
write, parsed back, yields the same synced-field values (modulo normalisation),
so the fingerprint stays stable and our own writes do not echo as remote deltas.

Layout-only fields (test_cases) are serialised as a fenced JSON block so the
round-trip is loss-free for nested structures.
"""

from __future__ import annotations

import json
import re

from aah.core.version_control.models import WorkItem, ItemRef, RemoteItem

# Title carries the feature id so a human glancing at the list sees it; the
# authoritative id always comes from the ledger, never parsed from the title.
_TITLE_RE = re.compile(r"^(?P<id>[A-Za-z0-9_-]+):\s*(?P<title>.*)$")

# Section markers. Stable, parseable, human-readable.
_SEC_DESC = "## Description"
_SEC_AC = "## Acceptance Criteria"
_SEC_TESTS = "## Test Cases"
_SEC_DEPS = "## Dependencies"
_FENCE = "```"

# One unified `aah:` namespace across every label dimension (phase, kind,
# status, layer). The prefix no longer distinguishes the dimension — value-set
# membership does (the value sets below do not overlap), which lets a status and
# a layer label coexist as `aah:planned` + `aah:backend` and still parse back
# cleanly. Phase/kind labels are query/grouping aids only and never round-trip
# into a WorkItem, so the parser ignores them.
_LABEL_PREFIX = "aah:"

# Status covers the canonical lifecycle (models.LIFECYCLE) plus the feature.md
# status field values.
_STATUS_LABEL_VALUES = [
    "planned", "queued", "implementing", "in_qa", "done",
    "blocked", "rework", "cancelled",
    "pending", "in_progress", "complete", "passed", "failed",
]
_LAYER_LABEL_VALUES = [
    "frontend", "backend", "data", "infra", "shared", "integration",
]
# Kind distinguishes the two issue types the briefer groups on.
_KIND_LABEL_VALUES = ["feature", "feedback"]
# The canonical OBV phases, used ONLY to seed labels up front (init-time
# provisioning). The briefer/create query path stays opaque — it never consults
# this list — so a caller can still pass any phase string; this is just the
# known set worth pre-creating so the repo looks complete.
_PHASE_LABEL_VALUES = ["discuss", "architecture", "plan", "build", "deploy"]

_STATUS_SET = set(_STATUS_LABEL_VALUES)
_LAYER_SET = set(_LAYER_LABEL_VALUES)


def phase_label(phase: str) -> str:
    """Map an opaque phase name to its label. The module owns no phase list."""
    return f"{_LABEL_PREFIX}{phase}"


def kind_label(kind: str) -> str:
    return f"{_LABEL_PREFIX}{kind}"


def is_aah_label(name: str) -> bool:
    return name.startswith(_LABEL_PREFIX)


def predefined_labels() -> list[str]:
    """The full static aah:* label set to provision in a repo up front.

    Covers every dimension — phase, kind, status, layer — so the repo is fully
    set up after init. The phase list here is only the known OBV phases for
    seeding; the query path stays opaque and still accepts any phase string.
    """
    values = (
        _PHASE_LABEL_VALUES
        + _KIND_LABEL_VALUES
        + _STATUS_LABEL_VALUES
        + _LAYER_LABEL_VALUES
    )
    return [f"{_LABEL_PREFIX}{v}" for v in values]


def title_for(item: WorkItem) -> str:
    base = item.title or item.description.split("\n", 1)[0]
    base = base.strip() or item.feature_id
    return f"{item.feature_id}: {base}"


def labels_for(item: WorkItem) -> list[str]:
    # Feature issues always carry the feature kind so the briefer can group them.
    labels = [kind_label("feature")]
    if item.layer:
        labels.append(f"{_LABEL_PREFIX}{item.layer}")
    if item.status:
        labels.append(f"{_LABEL_PREFIX}{item.status}")
    return labels


def _render_list(items: list) -> str:
    if not items:
        return "_none_"
    return "\n".join(f"- {str(i).strip()}" for i in items)


def render_body(item: WorkItem) -> str:
    """WorkItem -> issue body Markdown (round-trip stable)."""
    parts = [
        _SEC_DESC,
        (item.description or "").strip() or "_none_",
        "",
        _SEC_AC,
        _render_list(item.acceptance_criteria),
        "",
        _SEC_DEPS,
        _render_list(item.dependencies),
        "",
        _SEC_TESTS,
        # test_cases are nested -> JSON fence keeps the round-trip loss-free.
        f"{_FENCE}json",
        json.dumps(item.test_cases or [], indent=2, ensure_ascii=False),
        _FENCE,
        "",
        "<!-- Managed by RAPIDS version_control. feature.md is the source of truth. -->",
        "",
        "_Powered by AAH_",
    ]
    return "\n".join(parts)


def _extract_section(body: str, header: str, next_headers: list[str]) -> str:
    """Return the text between `header` and the next section header (or end)."""
    idx = body.find(header)
    if idx == -1:
        return ""
    start = idx + len(header)
    end = len(body)
    for nh in next_headers:
        nidx = body.find(nh, start)
        if nidx != -1:
            end = min(end, nidx)
    return body[start:end].strip()


def _parse_list(text: str) -> list:
    if not text or text.strip() == "_none_":
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif line and not line.startswith("#"):
            out.append(line)
    return out


def _parse_tests(text: str) -> list:
    m = re.search(rf"{_FENCE}json\s*(.*?){_FENCE}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1).strip() or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def parse_body(body: str) -> dict:
    """Issue body Markdown -> synced field values."""
    body = body or ""
    desc = _extract_section(body, _SEC_DESC, [_SEC_AC, _SEC_DEPS, _SEC_TESTS])
    ac = _extract_section(body, _SEC_AC, [_SEC_DEPS, _SEC_TESTS, _SEC_DESC])
    deps = _extract_section(body, _SEC_DEPS, [_SEC_TESTS, _SEC_AC, _SEC_DESC])
    tests = _extract_section(body, _SEC_TESTS, [])
    return {
        "description": "" if desc == "_none_" else desc,
        "acceptance_criteria": _parse_list(ac),
        "dependencies": _parse_list(deps),
        "test_cases": _parse_tests(tests),
    }


def _label_value(lbl: str) -> str | None:
    """Return the value after the aah: prefix, or None for a foreign label."""
    return lbl[len(_LABEL_PREFIX):] if lbl.startswith(_LABEL_PREFIX) else None


def _layer_from_labels(labels: list[str]) -> str | None:
    for lbl in labels:
        val = _label_value(lbl)
        if val in _LAYER_SET:
            return val
    return None


def _status_from_labels(labels: list[str], state: str) -> str:
    for lbl in labels:
        val = _label_value(lbl)
        if val in _STATUS_SET:
            return val
    # Fall back to issue open/closed if no status label present.
    return "cancelled" if state == "closed" else "planned"


def _label_names(raw_labels) -> list[str]:
    names = []
    for lbl in raw_labels or []:
        if isinstance(lbl, dict):
            names.append(lbl.get("name", ""))
        else:
            names.append(str(lbl))
    return [n for n in names if n]


def to_work_item(feature_id: str, issue: dict) -> WorkItem:
    """GitHub issue JSON -> canonical WorkItem."""
    labels = _label_names(issue.get("labels"))
    fields = parse_body(issue.get("body", ""))
    title = issue.get("title", "")
    m = _TITLE_RE.match(title)
    clean_title = m.group("title") if m else title
    return WorkItem(
        feature_id=feature_id,
        title=clean_title,
        description=fields["description"],
        acceptance_criteria=fields["acceptance_criteria"],
        test_cases=fields["test_cases"],
        dependencies=fields["dependencies"],
        layer=_layer_from_labels(labels),
        status=_status_from_labels(labels, issue.get("state", "open").lower()),
    )


def ref_from_issue(issue: dict) -> ItemRef:
    return ItemRef(
        number=issue.get("number"),
        node_id=issue.get("id"),
        url=issue.get("url"),
    )


def remote_item_from_issue(feature_id: str, issue: dict) -> RemoteItem:
    return RemoteItem(
        ref=ref_from_issue(issue),
        work_item=to_work_item(feature_id, issue),
        updated_at=issue.get("updatedAt", ""),
        raw=issue,
    )
