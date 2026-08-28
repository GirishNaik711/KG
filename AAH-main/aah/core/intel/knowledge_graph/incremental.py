"""
Incremental rebuild support via SHA256 file cache.

Only re-parses files that have changed since the last graph build.
"""

import hashlib
import json
from pathlib import Path


def _file_hash(file_path: Path) -> str:
    """SHA-256 hash of file contents."""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()[:16]


def load_cache(cache_path: Path) -> dict:
    """Load the file hash cache."""
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(cache: dict, cache_path: Path) -> None:
    """Save the file hash cache."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding='utf-8')


def get_changed_files(file_paths: list[Path], cache_path: Path) -> tuple[list[Path], dict]:
    """
    Compare file hashes against cache to find changed files.

    Returns (changed_files, updated_cache).
    """
    cache = load_cache(cache_path)
    changed = []
    new_cache = {}

    for fp in file_paths:
        if not fp.exists():
            continue
        current_hash = _file_hash(fp)
        key = str(fp)
        new_cache[key] = current_hash

        if cache.get(key) != current_hash:
            changed.append(fp)

    return changed, new_cache
