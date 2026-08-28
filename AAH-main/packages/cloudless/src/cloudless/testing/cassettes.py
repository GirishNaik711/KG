"""LLM cassettes — record real Bedrock responses, replay deterministically (Q25, Q13).

Two modes:
  - "record": Wraps cloudless.LLM.invoke/stream so every real call also gets
    appended to the cassette file.
  - "replay": Wraps cloudless.LLM.invoke/stream to serve from cassette
    (RuntimeError if a call doesn't match a recorded entry).

Cassettes are JSONL files committed to git. Diffing a cassette = reviewing
the prompt-change impact on the model's response.

Usage:
    from cloudless.testing import llm_cassette
    with llm_cassette("tests/cassettes/greeting.cassette", mode="record"):
        llm = cloudless.LLM(model="nova-micro")
        response = await llm.invoke("hi")
    # Now run again with mode="replay" — no real Bedrock call:
    with llm_cassette("tests/cassettes/greeting.cassette", mode="replay"):
        llm = cloudless.LLM(model="nova-micro")
        response = await llm.invoke("hi")  # served from cassette
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from enum import Enum
from pathlib import Path

from cloudless.catalog.llm import LLM
from cloudless.chunks import TextChunk


class CassetteMode(str, Enum):  # noqa: UP042 — public API value-compat with prior 0.0.x; do not switch to StrEnum
    RECORD = "record"
    REPLAY = "replay"
    PASSTHROUGH = "passthrough"
    """No interception — for diagnostics."""


_DEFAULT_MODE: CassetteMode = CassetteMode.REPLAY


def set_default_mode(mode: CassetteMode | str) -> None:
    """Override the default cassette mode (e.g., set to 'record' in CI re-record runs)."""
    global _DEFAULT_MODE
    _DEFAULT_MODE = CassetteMode(mode) if isinstance(mode, str) else mode


def _key(model: str, system: str | None, prompt: str) -> str:
    payload = json.dumps({"m": model, "s": system or "", "p": prompt}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _load_cassette(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    entries: dict[str, dict] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                entries[e["key"]] = e
            except json.JSONDecodeError:
                continue
    return entries


def _append_cassette(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


@contextlib.contextmanager
def llm_cassette(path: str | Path, *, mode: CassetteMode | str | None = None):
    """Patch cloudless.LLM.invoke/stream to record or replay against `path`.

    Args:
        path: Cassette file path (JSONL).
        mode: "record" | "replay" | "passthrough". Defaults to the value set by
              set_default_mode (initially "replay"). RECORD calls real Bedrock
              AND writes the response to the cassette; REPLAY raises if a call
              isn't in the cassette.
    """
    path = Path(path)
    resolved_mode = CassetteMode(mode) if mode is not None else _DEFAULT_MODE

    if resolved_mode == CassetteMode.PASSTHROUGH:
        yield resolved_mode
        return

    entries = _load_cassette(path)

    # Patch invoke + stream on the LLM class
    real_invoke = LLM.invoke
    real_stream = LLM.stream

    async def patched_invoke(self, prompt, *, system=None, max_tokens=512, ctx=None):
        k = _key(self.alias.model_id, system, prompt)
        if resolved_mode == CassetteMode.REPLAY:
            if k not in entries:
                raise RuntimeError(
                    f"No cassette entry for key {k} (model={self.alias.model_id!r}, "
                    f"prompt={prompt[:60]!r}). Re-record with mode='record'."
                )
            return entries[k]["text"]
        # RECORD: call real Bedrock + persist
        text = await real_invoke(self, prompt, system=system, max_tokens=max_tokens, ctx=ctx)
        _append_cassette(path, {
            "key": k,
            "model": self.alias.model_id,
            "system": system or "",
            "prompt": prompt,
            "text": text,
        })
        entries[k] = {"text": text}
        return text

    async def patched_stream(self, prompt, *, system=None, max_tokens=512, ctx=None):
        k = _key(self.alias.model_id, system, prompt)
        if resolved_mode == CassetteMode.REPLAY:
            if k not in entries:
                raise RuntimeError(
                    f"No cassette entry for key {k} (stream). Re-record with mode='record'."
                )
            text = entries[k].get("text", "")
            # Yield in single chunk on replay (deterministic, no real streaming)
            yield TextChunk(text=text)
            return
        # RECORD: collect real stream + persist
        chunks: list[TextChunk] = []
        async for chunk in real_stream(self, prompt, system=system, max_tokens=max_tokens, ctx=ctx):
            chunks.append(chunk)
            yield chunk
        text = "".join(c.text for c in chunks)
        _append_cassette(path, {
            "key": k,
            "model": self.alias.model_id,
            "system": system or "",
            "prompt": prompt,
            "text": text,
        })
        entries[k] = {"text": text}

    LLM.invoke = patched_invoke  # type: ignore[assignment]
    LLM.stream = patched_stream  # type: ignore[assignment]
    try:
        yield resolved_mode
    finally:
        LLM.invoke = real_invoke  # type: ignore[assignment]
        LLM.stream = real_stream  # type: ignore[assignment]
