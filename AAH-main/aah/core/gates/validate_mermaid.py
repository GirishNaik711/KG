#!/usr/bin/env python3
"""Mermaid fence validation gate for the aah-arch phase.

Scans every ``.md`` **and** ``.html`` under ``.aah/architecture/`` (recursively):

  · ``.md``   → every ```mermaid ... ``` fenced code block
  · ``.html`` → mermaid source from BOTH patterns the Step 5c HTML recipe uses:
      1. ``<script type="text/markdown">`` blocks — the verbatim MD copy;
         mermaid fences inside are extracted just like the ``.md`` path
      2. ``<div class="mermaid">`` blocks — mermaid source that was already
         post-processed out of a ```mermaid fence (or authored directly)

This means the gate covers every possible workflow: md-only, html-only,
both, and hand-edited HTML.

Each block is validated by a **structural lint** (pure Python regex, no
external dependency) that catches the exact failure modes documented in
the aah-arch SKILL Step 5b authoring rules:
  R1: unquoted ``participant``/``actor`` labels with special chars
  R2: ``<placeholder>`` inside message/Note text (Mermaid parses ``<`` as an arrow)
  R3: backticks inside message text
  R4: Unicode arrows / em-dashes / curly quotes anywhere in the fence
  R5: semicolons used as statement separators in message text
  R6: reserved words (``end``, ``subgraph``, ``class``, ``click``, ``link``)
      used as bare node IDs at line start in flowchart/graph blocks

An earlier draft also called ``mmdc`` from ``@mermaid-js/mermaid-cli`` as a
Layer 2 backstop, but mmdc pulls Puppeteer + Chromium (~200 MB, ~10 min
first-run install) and its parser is lenient on the exact label class we
care about — so the cost/benefit did not add up. If we ever hit an exotic
parse error the lint above misses, add a specific rule for it.

A single failing fence exits non-zero. Wire this into ``/aah-arch`` Step 5d so
no diagram ships broken.

Usage (from a phase skill):
    aah run core.gates.validate_mermaid --project-path "$PROJECT_DIR"

Programmatic:
    from aah.core.gates.validate_mermaid import validate_mermaid
    passed, issues = validate_mermaid(project_path)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Fence extraction
# ---------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^(\s*)(```|~~~)\s*mermaid\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Fence:
    path: Path
    block_index: int          # 1-based ordinal within the file
    start_line: int           # 1-based line number of the opening fence
    content: str              # fence body (no fence markers)


def _extract_fences_from_md_text(path: Path, text: str) -> list[Fence]:
    """Return every ```mermaid fence found in `text` (a full markdown document)."""
    lines = text.splitlines()
    out: list[Fence] = []
    i = 0
    ordinal = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        indent, marker = m.group(1), m.group(2)
        start = i + 1
        body: list[str] = []
        i += 1
        close_re = re.compile(rf"^{re.escape(indent)}{re.escape(marker)}\s*$")
        while i < len(lines) and not close_re.match(lines[i]):
            body.append(lines[i])
            i += 1
        if i < len(lines):
            i += 1
        ordinal += 1
        out.append(
            Fence(
                path=path,
                block_index=ordinal,
                start_line=start,
                content="\n".join(body),
            )
        )
    return out


def _extract_fences(path: Path) -> list[Fence]:
    """Return every ```mermaid``` fence in a Markdown file, in file order."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return _extract_fences_from_md_text(path, text)


# HTML extraction: `<script type="text/markdown">...</script>` and
# `<div class="mermaid">...</div>`. Both patterns are what the Step 5c HTML
# recipe leaves in the rendered doc. Non-greedy so multiple blocks per file work.
_HTML_SCRIPT_MD = re.compile(
    r'<script\b[^>]*\btype\s*=\s*["\']text/markdown["\'][^>]*>(.*?)</script\s*>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_DIV_MERMAID = re.compile(
    r'<div\b[^>]*\bclass\s*=\s*["\'][^"\']*\bmermaid\b[^"\']*["\'][^>]*>(.*?)</div\s*>',
    re.IGNORECASE | re.DOTALL,
)

# Very small HTML-entity decoder — HTML rendered from marked leaves only the
# five predefined entities in Mermaid source. Full html.unescape() would also
# work but pulls in a stdlib module we don't need.
_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}


def _decode_entities(s: str) -> str:
    for k, v in _HTML_ENTITIES.items():
        s = s.replace(k, v)
    return s


def _line_of_offset(text: str, offset: int) -> int:
    """1-based line number in `text` for a 0-based character offset."""
    return text.count("\n", 0, offset) + 1


def _extract_fences_from_html(path: Path) -> list[Fence]:
    """Return every mermaid block found in an HTML file.

    Handles two patterns:
      1. `<script type="text/markdown">` — treat its content as full markdown
         and extract every ```mermaid fence from it, mapping fence lines back
         onto the HTML file's line numbers.
      2. `<div class="mermaid">` — the block's inner text IS the mermaid
         source (typically post-processed there by the Step 5c HTML recipe).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    out: list[Fence] = []
    ordinal = 0

    # (1) Embedded markdown blocks
    for m in _HTML_SCRIPT_MD.finditer(text):
        inner_start_offset = m.start(1)
        block_start_line = _line_of_offset(text, inner_start_offset)
        md_text = _decode_entities(m.group(1))
        md_fences = _extract_fences_from_md_text(path, md_text)
        for fence in md_fences:
            ordinal += 1
            out.append(
                Fence(
                    path=path,
                    block_index=ordinal,
                    # fence.start_line is 1-based inside md_text; map to HTML lines.
                    start_line=block_start_line + fence.start_line - 1,
                    content=fence.content,
                )
            )

    # (2) Direct <div class="mermaid"> blocks
    for m in _HTML_DIV_MERMAID.finditer(text):
        inner_start_offset = m.start(1)
        ordinal += 1
        out.append(
            Fence(
                path=path,
                block_index=ordinal,
                start_line=_line_of_offset(text, inner_start_offset),
                content=_decode_entities(m.group(1)).strip("\n"),
            )
        )

    return out


# ---------------------------------------------------------------------------
# Structural lint rules
# ---------------------------------------------------------------------------

_LABEL_SAFE = re.compile(r"^[A-Za-z0-9_ ]*$")

# `participant BE as FastAPI+LangGraph (ALB)` or `actor Eng as Engineer (user)`
_PARTICIPANT_AS = re.compile(
    r"^\s*(participant|actor)\s+\S+\s+as\s+(.+?)\s*$", re.IGNORECASE
)

# A sequenceDiagram arrow-message line: `A->>B: text` / `A-->>B: text` / etc.
_ARROW_MSG = re.compile(
    r"^\s*\S+\s*(?:-{1,2}>{1,2}[+-]?|<-{1,2}[+-]?|-x|--x|-\)|--\))\s*\S+\s*:\s*(.+)$"
)

# `Note over A,B: text` / `Note left of A: text` / `Note right of A: text`
_NOTE_MSG = re.compile(
    r"^\s*Note\s+(?:over|left of|right of)\s+[^:]+:\s*(.+)$", re.IGNORECASE
)

# Unicode chars we forbid inside a fence.
_FORBIDDEN_UNICODE = {
    "→": "'->' (U+2192 RIGHTWARDS ARROW)",
    "←": "'<-' (U+2190 LEFTWARDS ARROW)",
    "⇒": "'=>' (U+21D2 RIGHTWARDS DOUBLE ARROW)",
    "⇐": "'<=' (U+21D0 LEFTWARDS DOUBLE ARROW)",
    "↔": "'<->' (U+2194 LEFT RIGHT ARROW)",
    "⇄": "'<->' (U+21C4 ARROWS)",
    "—": "'-' or '--' (U+2014 EM DASH)",
    "–": "'-' (U+2013 EN DASH)",
    "“": "'\"' (U+201C LEFT CURLY DOUBLE QUOTE)",
    "”": "'\"' (U+201D RIGHT CURLY DOUBLE QUOTE)",
    "‘": "'\\'' (U+2018 LEFT CURLY SINGLE QUOTE)",
    "’": "'\\'' (U+2019 RIGHT CURLY SINGLE QUOTE)",
}

# `<foo` where the next char is a letter/underscore = looks like an unescaped
# placeholder in message text. `<=`, `< 5`, `<br>` are OK — this regex requires
# an identifier start immediately after `<`.
_PLACEHOLDER_LT = re.compile(r"<[A-Za-z_]")

# Reserved Mermaid words that must not appear as bare node IDs at line start
# in flowchart/graph/stateDiagram blocks.
_RESERVED_NODE_IDS = {"end", "subgraph", "class", "click", "link"}
# `end -->  X` or `end --> X` or `end["label"]` etc.
_RESERVED_LINE = re.compile(
    r"^\s*(end|subgraph|class|click|link)\b(?:\s*[\[({]|\s*-{1,2}>|\s*<-{1,2})",
    re.IGNORECASE,
)

# Diagram-type header: first non-blank content line of a fence.
_DIAGRAM_TYPE = re.compile(
    r"^\s*(sequenceDiagram|flowchart|graph|erDiagram|classDiagram|stateDiagram(?:-v2)?|"
    r"journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|requirementDiagram|"
    r"C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|xychart(?:-beta)?)"
    r"(?:\s|$)",
    re.IGNORECASE,
)


def _diagram_type(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):  # blank or Mermaid comment
            continue
        m = _DIAGRAM_TYPE.match(stripped)
        return m.group(1).lower() if m else "unknown"
    return "unknown"


def _strip_quoted(label: str) -> str | None:
    """Return inner text if `label` is wrapped in matching double quotes, else None."""
    if len(label) >= 2 and label[0] == '"' and label[-1] == '"':
        return label[1:-1]
    return None


def _lint_fence(fence: Fence) -> list[str]:
    """Apply the structural lint rules. Return one issue string per violation."""
    issues: list[str] = []
    dtype = _diagram_type(fence.content)
    lines = fence.content.splitlines()

    for offset, raw in enumerate(lines):
        line_no = fence.start_line + 1 + offset  # +1 to skip the opening fence

        # R4: forbidden Unicode chars, anywhere.
        for ch, desc in _FORBIDDEN_UNICODE.items():
            if ch in raw:
                issues.append(
                    f"line {line_no}: forbidden Unicode char {desc}; use ASCII"
                )
                break  # one Unicode complaint per line is enough

        # R1: `participant X as <label>` with unquoted risky label (sequenceDiagram only)
        if dtype == "sequencediagram":
            m = _PARTICIPANT_AS.match(raw)
            if m:
                label = m.group(2).strip()
                inner = _strip_quoted(label)
                to_check = inner if inner is not None else label
                if inner is None and not _LABEL_SAFE.match(to_check):
                    issues.append(
                        f"line {line_no}: participant/actor label {label!r} contains "
                        f"characters outside [A-Za-z0-9_ ] — wrap it in double quotes "
                        f'(e.g. `as "{to_check}"`)'
                    )

        # R2/R3/R5: message-text checks on arrow + Note lines.
        payload = None
        m_arrow = _ARROW_MSG.match(raw)
        if m_arrow:
            payload = m_arrow.group(1)
        else:
            m_note = _NOTE_MSG.match(raw)
            if m_note:
                payload = m_note.group(1)

        if payload is not None:
            if _PLACEHOLDER_LT.search(payload):
                issues.append(
                    f"line {line_no}: message text contains '<name>'-style placeholder "
                    f"(Mermaid parses '<' as an arrow) — use braces or plain text"
                )
            if "`" in payload:
                issues.append(
                    f"line {line_no}: message text contains a backtick — "
                    f"use plain identifiers instead"
                )
            if ";" in payload:
                issues.append(
                    f"line {line_no}: message text contains ';' — "
                    f"split into multiple arrows or use commas"
                )

        # R6: reserved words as bare node IDs (flowchart/graph/stateDiagram)
        if dtype in {"flowchart", "graph", "statediagram", "statediagram-v2"}:
            m_res = _RESERVED_LINE.match(raw)
            if m_res:
                issues.append(
                    f"line {line_no}: reserved word {m_res.group(1)!r} used as node ID — "
                    f"alias it (e.g. `END_NODE[\"End\"]`)"
                )

    return issues


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------

def _architecture_files(project_path: Path) -> list[Path]:
    """Return every `.md` and `.html` under .aah/architecture/ (recursive)."""
    arch_dir = project_path / ".aah" / "architecture"
    if not arch_dir.is_dir():
        return []
    md = arch_dir.rglob("*.md")
    html = arch_dir.rglob("*.html")
    return sorted((p for p in list(md) + list(html) if p.is_file()))


def _fences_for(path: Path) -> list[Fence]:
    """Dispatch to the right extractor based on file extension."""
    suffix = path.suffix.lower()
    if suffix == ".html" or suffix == ".htm":
        return _extract_fences_from_html(path)
    return _extract_fences(path)


def validate_mermaid(
    project_path: Path,
) -> tuple[bool, list[str]]:
    """Validate every mermaid block under .aah/architecture/ (`.md` + `.html`).

    Returns (passed, issues). No architecture dir → (True, []).
    """
    issues: list[str] = []
    files = _architecture_files(project_path)
    if not files:
        return True, []

    total_fences = 0
    files_with_fences = 0
    for path in files:
        rel = path.relative_to(project_path)
        fences = _fences_for(path)
        if fences:
            files_with_fences += 1
        total_fences += len(fences)
        for fence in fences:
            for issue in _lint_fence(fence):
                issues.append(f"{rel} (block #{fence.block_index}): {issue}")

    passed = not issues
    if passed and total_fences > 0:
        print(
            f"validate_mermaid: {total_fences} fence(s) across "
            f"{files_with_fences} file(s) OK"
        )
    return passed, issues


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Mermaid fences under .aah/architecture/"
    )
    parser.add_argument(
        "--project-path",
        type=Path,
        help="Project root (folder containing .aah/). Defaults to the current project.",
    )
    args, _ = parser.parse_known_args()

    from aah.core.common.config import resolve_project_path

    project_path = None
    if args.project_path is not None:
        project_path = resolve_project_path(args.project_path)
    if project_path is None:
        try:
            hook_input = json.load(sys.stdin)
        except (json.JSONDecodeError, EOFError):
            hook_input = {}
        cwd = hook_input.get("cwd")
        project_path = resolve_project_path(Path(cwd) if cwd else None)

    if project_path is None:
        sys.exit(0)  # no project — nothing to gate

    passed, issues = validate_mermaid(project_path)

    if not passed:
        print("Mermaid validation FAILED:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print(
            "\nFix the offending fences per the aah-arch SKILL Step 5b authoring "
            "rules, then re-run this gate.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
