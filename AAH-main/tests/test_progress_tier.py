"""current_tier must survive a bare wave advance (weather-cli bug #16).

A wave-only update_progress call used to reset current_tier to 0, so the
orchestrator re-emitted a spurious advance_tier for an already-completed tier.
"""

from __future__ import annotations

from pathlib import Path

from aah.core.common.progress import (
    get_default_progress,
    load_progress,
    save_progress,
    update_progress,
)


def _fresh(tmp_path: Path) -> Path:
    p = tmp_path / ".aah" / "claude-progress.json"
    p.parent.mkdir(parents=True)
    save_progress(get_default_progress(), p)
    return p


def test_bare_wave_advance_preserves_tier(tmp_path: Path):
    p = _fresh(tmp_path)
    update_progress(p, wave=1, tier=1)          # in wave 1, tier 1
    assert load_progress(p)["current_tier"] == 1

    update_progress(p, wave=1)                   # bare wave write, no tier
    assert load_progress(p)["current_tier"] == 1, "bare wave write clobbered tier"

    update_progress(p, wave=2)                   # advance wave, no tier
    assert load_progress(p)["current_tier"] == 1, "wave advance clobbered tier"


def test_explicit_tier_still_sets(tmp_path: Path):
    p = _fresh(tmp_path)
    update_progress(p, wave=2, tier=3)
    data = load_progress(p)
    assert data["current_wave"] == 2
    assert data["current_tier"] == 3

    update_progress(p, wave=3, tier=0)           # new wave, explicit tier reset
    assert load_progress(p)["current_tier"] == 0
