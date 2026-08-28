"""Read-only helpers that answer 'what does the tracker say about this feature'.

The impl-reasoning gate consults the tracker before deciding whether to
advance the orchestrator's state machine. Keeping those checks in one module
makes the result vocabulary uniform.

Every helper returns one of the same six string sentinels:
  - "present"      — the expected artefact was found on the tracker
  - "absent"       — reachable, expected artefact NOT found
  - "no_ledger"    — feature has no remote_ref yet (not synced to tracker)
  - "vc_disabled"  — version_control disabled or no config
  - "offline"      — tracker unreachable / rate-limited (transient)
  - "error"        — anything else went wrong (treat as unknown)

Callers must NOT treat "offline" or "error" as "advance safe" — those are
unknowns. The orchestrator surfaces them as a blocked action so the user can
resolve the underlying transport / auth issue and retry.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.common.config import resolve_project_path


def _resolve_config_and_ledger(project_path: Path | None):
    """Load VC config and open the sync ledger; return (config, aah_path, ledger) or a sentinel.

    Returns a tuple (status, payload):
      - ("ok", (config, aah_path, ledger)) when everything loaded
      - ("vc_disabled", None) when config unavailable or feature toggle off
      - ("error", None) on any unexpected failure
    """
    project_dir = resolve_project_path(project_path)
    if project_dir is None:
        return ("error", None)
    aah_path = project_dir / ".aah"

    try:
        from aah.core.version_control.config import load_vc_config
        config = load_vc_config(aah_path / "manifest.yaml")
    except Exception:  # noqa: BLE001
        return ("vc_disabled", None)
    if not config.enabled:
        return ("vc_disabled", None)

    try:
        from aah.core.version_control.ledger import SyncLedger
        ledger = SyncLedger(aah_path)
    except Exception:  # noqa: BLE001
        return ("error", None)

    return ("ok", (config, aah_path, ledger))


def has_impl_comment_on_tracker(
    feature_id: str,
    project_path: Path | None = None,
) -> str:
    """Return whether the tracker carries the impl-reasoning comment.

    Matches on any comment whose body starts with
    ``## Implementation Reasoning — <feature-id>``. See module docstring for
    the result vocabulary.
    """
    stage, payload = _resolve_config_and_ledger(project_path)
    if stage != "ok":
        return stage
    config, _aah_path, ledger = payload

    try:
        remote_ref = ledger.remote_ref(feature_id)
    except Exception:  # noqa: BLE001
        return "error"
    if remote_ref is None or remote_ref.number is None:
        return "no_ledger"

    expected_header = f"## Implementation Reasoning — {feature_id}"
    try:
        from aah.core.version_control.providers.registry import get_provider
        from aah.core.version_control.providers.base import ProviderOffline
        from aah.core.version_control.models import ItemRef
        provider = get_provider(config)
    except Exception:  # noqa: BLE001
        return "error"

    try:
        existing = provider.fetch_comments(ItemRef(number=remote_ref.number), since_id=None)
    except ProviderOffline:
        return "offline"
    except Exception:  # noqa: BLE001
        return "error"

    if any((c.body or "").lstrip().startswith(expected_header) for c in existing):
        return "present"
    return "absent"
