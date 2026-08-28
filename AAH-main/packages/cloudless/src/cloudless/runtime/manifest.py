"""cloudless.runtime.manifest — load the baked agent manifest (Q12).

At deploy time, the CLI bakes a `cloudless-manifest.json` into each agent
that lists every deployed agent's name, cloud, endpoint URLs, etc. At
runtime, the embedded lib loads this manifest so `ctx.peer(name)` can
resolve to the right URL.

TTL-refresh from cloud-storage (OQ3, every 5 min) is implemented here too,
but the simplest path is just load-once-at-startup.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PeerEntry:
    name: str
    cloud: str
    http_url: str | None = None
    a2a_url: str | None = None
    idp_issuer: str | None = None
    audience: str | None = None
    residency: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Manifest:
    project: str
    agents: dict[str, PeerEntry]

    def get(self, name: str) -> PeerEntry | None:
        return self.agents.get(name)


def _parse_manifest_dict(contents: dict) -> Manifest:
    agents: dict[str, PeerEntry] = {}
    for name, raw in (contents.get("agents") or {}).items():
        agents[name] = PeerEntry(
            name=name,
            cloud=raw.get("cloud", "aws"),
            http_url=raw.get("http_url"),
            a2a_url=raw.get("a2a_url"),
            idp_issuer=raw.get("idp_issuer"),
            audience=raw.get("audience"),
            residency=tuple(raw.get("residency", ())),
        )
    return Manifest(project=contents.get("project", "<unknown>"), agents=agents)


def load_manifest(
    path: Path | str | None = None,
    *,
    contents: dict | None = None,
) -> Manifest:
    """Load a manifest from disk (or accept already-parsed dict for tests).

    Search order if path is None:
      1. CLOUDLESS_MANIFEST_PATH env var
      2. /app/cloudless-manifest.json (deployed agent default)
      3. ./cloudless-manifest.json (local dev)
    """
    if contents is None:
        if path is None:
            candidates = [
                os.environ.get("CLOUDLESS_MANIFEST_PATH"),
                "/app/cloudless-manifest.json",
                "cloudless-manifest.json",
            ]
            path = next((p for p in candidates if p and Path(p).is_file()), None)
        if path is None or not Path(path).is_file():
            # Empty manifest is valid — single-agent projects have no peers,
            # and explicitly missing paths shouldn't crash the agent.
            return Manifest(project="<unknown>", agents={})
        contents = json.loads(Path(path).read_text())

    return _parse_manifest_dict(contents)


# --------------------------------------------------------------------- #
# TTL refresh (OQ3)
# --------------------------------------------------------------------- #


class ManifestRefresher:
    """Background thread that re-fetches the manifest on a TTL.

    Source:
      - CLOUDLESS_MANIFEST_URL env var — HTTP GET on every tick
      - CLOUDLESS_MANIFEST_PATH env var — file re-read on every tick

    Usage:
        refresher = ManifestRefresher(interval_seconds=300)
        refresher.start()
        # ... use refresher.current() throughout the process ...
        refresher.stop()
    """

    def __init__(
        self,
        *,
        interval_seconds: float = 300.0,
        url: str | None = None,
        path: str | Path | None = None,
    ) -> None:
        import threading
        self._interval = interval_seconds
        self._url = url or os.environ.get("CLOUDLESS_MANIFEST_URL")
        self._path = path or os.environ.get("CLOUDLESS_MANIFEST_PATH")
        self._current: Manifest = self._fetch_once()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _fetch_once(self) -> Manifest:
        if self._url:
            try:
                import httpx
                resp = httpx.get(self._url, timeout=5.0)
                resp.raise_for_status()
                return _parse_manifest_dict(resp.json())
            except Exception:
                # Don't replace the existing manifest on transient errors
                return getattr(self, "_current", Manifest(project="<unknown>", agents={}))
        if self._path and Path(self._path).is_file():
            try:
                return _parse_manifest_dict(json.loads(Path(self._path).read_text()))
            except Exception:
                return getattr(self, "_current", Manifest(project="<unknown>", agents={}))
        return Manifest(project="<unknown>", agents={})

    def current(self) -> Manifest:
        with self._lock:
            return self._current

    def refresh_now(self) -> Manifest:
        """Force-fetch synchronously. Returns the new manifest."""
        m = self._fetch_once()
        with self._lock:
            self._current = m
        return m

    def start(self) -> None:
        import threading
        if self._thread is not None:
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, daemon=True, name="cloudless-manifest-refresh")
        t.start()
        self._thread = t

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.refresh_now()
