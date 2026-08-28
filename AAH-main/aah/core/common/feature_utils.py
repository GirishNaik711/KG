#!/usr/bin/env python3
"""Feature file discovery, parsing, and section append utilities."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def flatten_wave_features(wave_entry) -> list[str]:
    """Extract a flat list of feature IDs from a wave entry.

    Handles all formats:
      - dict with "features" key: {"features": [...]}
      - nested tier list: [["F1", "F2"], ["F3"]]
      - flat list: ["F1", "F2", "F3"]
    """
    if isinstance(wave_entry, dict):
        wave_entry = wave_entry.get("features", [])
    if not isinstance(wave_entry, list):
        return []
    flattened = []
    for item in wave_entry:
        flattened.extend(flatten_wave_features(item) if isinstance(item, (dict, list)) else [item])
    return [feature_id for feature_id in flattened if isinstance(feature_id, str)]


def load_wave_feature_ids(project_or_aah_path: Path, wave: int) -> list[str]:
    """Load flat, mapping, or tiered wave features; malformed input is empty."""
    if not isinstance(wave, int) or isinstance(wave, bool) or wave < 0:
        return []

    root = Path(project_or_aah_path)
    aah_path = root if root.name == ".aah" else root / ".aah"
    waves_path = aah_path / "plan" / "waves.json"
    try:
        data = json.loads(waves_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []

    waves = data.get("waves") if isinstance(data, dict) else None
    if not isinstance(waves, list) or wave >= len(waves):
        return []

    return flatten_wave_features(waves[wave])


def parse_feature_frontmatter(path: Path) -> dict | None:
    """Parse feature data from a .md file.

    Supports two formats:
    1. Pure markdown (## sections with bullets) — primary format
    2. YAML frontmatter (legacy, backward compatible)

    Superseding rework entries (forward-relocation model) are *pointer stubs*:
    a tiny .md with ``## Id`` + ``## Supersedes`` + ``## Spec File`` and no body.
    When such an entry is parsed, its ``spec_file`` is followed and the base
    spec's body is merged in — the stub keeps its own identity (id, supersedes,
    spec_file, status) but borrows the base's dependencies / file_scope /
    acceptance criteria / test cases. This keeps the base ``<F>.md`` as the
    single source of truth (no duplicated spec content) while every downstream
    reader transparently sees a fully-populated feature under the rework id.

    Returns the parsed dict, or None on failure.
    """
    data = _parse_raw(path)
    return _resolve_spec_pointer(data, path)


def _parse_raw(path: Path) -> dict | None:
    """Parse a feature .md file's own sections, WITHOUT following any pointer."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None

    if content.startswith("---"):
        return _parse_yaml_format(content)
    return _parse_md_format(content, path.stem)


def _resolve_spec_pointer(data: dict | None, path: Path) -> dict | None:
    """Follow a ``spec_file`` pointer, merging the base spec's body into the stub.

    The stub's identity fields (``id``, ``supersedes``, ``spec_file``,
    ``status``) win; every other field (dependencies, file_scope, acceptance
    criteria, test cases, ...) comes from the base spec. Returns ``data``
    unchanged when there is no pointer, the base can't be read, or the pointer
    is self-referential.

    The base is parsed RAW (``_parse_raw``) — it is by definition a real spec,
    not another stub — so this is a single hop with no recursion.
    """
    if not data:
        return data
    spec_file = data.get("spec_file")
    if not spec_file:
        return data

    name = spec_file[0] if isinstance(spec_file, list) else spec_file
    if not name:
        return data
    base_path = path.parent / str(name)
    try:
        if not base_path.exists() or base_path.resolve() == path.resolve():
            return data
    except OSError:
        return data

    base = _parse_raw(base_path)
    if not base:
        return data

    resolved = dict(base)
    resolved["id"] = data.get("id", resolved.get("id"))
    if data.get("supersedes") is not None:
        resolved["supersedes"] = data["supersedes"]
    resolved["spec_file"] = name
    if "status" in data:
        resolved["status"] = data["status"]
    return resolved


def _parse_yaml_format(content: str) -> dict | None:
    """Legacy parser for YAML frontmatter format.

    Parses the YAML block for build/status fields, then merges spec fields
    (description, acceptance_criteria, dependencies, test_cases, title, layers)
    from the markdown body below the closing ``---``. Frontmatter values win on
    collision so that explicitly set fields are never overwritten.
    """
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None

    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None

    yaml_content = "\n".join(lines[1:end_idx])
    try:
        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict):
            return None
    except Exception:
        return None

    # Parse the markdown body below the closing --- and merge spec fields in.
    body = "\n".join(lines[end_idx + 1:])
    _SPEC_FIELDS = {
        "id", "description", "acceptance_criteria", "dependencies",
        "test_cases", "title", "layers", "module_ref",
        # ``## Required Env Variables`` (aliased to required_env above) must
        # reach the contract from the body too, not only from frontmatter —
        # otherwise a hybrid file's env section still misses the test-time gate.
        "required_env",
    }
    if body.strip():
        md_data = _parse_md_format(body)
        if isinstance(md_data, dict):
            for key, val in md_data.items():
                if key in _SPEC_FIELDS and key not in data:
                    data[key] = val

    return data


# Markdown heading keys that must land on a differently-named contract field.
#
# The derived key is the heading verbatim (``heading.lower().replace(" ", "_")``),
# so the standardized ``## Required Env Variables`` section yields
# ``required_env_variables`` — which is NOT the ``required_env`` key the schema
# validator and the test-time gate read (validators.FEATURE_SCHEMA,
# run_feature_tests, run_regression_suite, runtime_validation). Without this
# alias a feature file's env section never reaches the gate at all: the section
# and the gate key were simply disconnected.
_MD_HEADING_KEY_ALIASES = {
    "required_env_variables": "required_env",
}

# Separators between an env var NAME and its human-readable purpose comment in a
# ``## Required Env Variables`` bullet. The em dash is the documented form; the
# hyphen and colon forms are tolerated because authors reach for them, and the
# colon form additionally neutralizes the legacy ``- VAR_NAME: value`` shape by
# discarding whatever followed it (names-only is the contract; a value written
# there must never reach the gate or an artifact).
_ENV_PURPOSE_SEPARATORS = (" — ", " – ", " -- ", " - ", ":")


def _normalize_required_env(value):
    """Reduce ``- VAR_NAME — purpose`` bullets to the bare env key names.

    ``required_env`` is consumed as a list of key NAMES by
    ``validators.validate_required_env_keys`` and by the test-time gates
    (run_feature_tests, run_regression_suite, runtime_validation). The markdown
    section carries a purpose comment for the human reader, so the trailing prose
    is stripped here — otherwise every entry fails key validation and the whole
    declaration is rejected as invalid.

    Non-list input is returned untouched; the schema validator owns reporting a
    malformed section.
    """
    if not isinstance(value, list):
        return value
    keys: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            keys.append(entry)
            continue
        name = entry.strip()
        for separator in _ENV_PURPOSE_SEPARATORS:
            if separator in name:
                name = name.split(separator, 1)[0]
                break
        name = name.strip().rstrip(":").strip()
        if name:
            keys.append(name)
    return keys


def _parse_md_format(content: str, filename_stem: str = "") -> dict | None:
    """Parse pure markdown feature file."""
    lines = content.splitlines()
    sections = _split_md_sections(lines)

    data: dict = {"status": "pending"}

    for heading, content_lines in sections.items():
        key = heading.lower().replace(" ", "_")
        key = _MD_HEADING_KEY_ALIASES.get(key, key)
        if key == "description":
            # Description is free-form prose that legitimately mixes paragraphs
            # with a "Behavioral expectations" bullet list. Preserve the WHOLE
            # section verbatim as a string — _auto_parse_section would otherwise
            # see the bullets and return only them, dropping the prose (and
            # yielding a list where the schema requires a str).
            value = "\n".join(content_lines).strip() or None
        else:
            value = _auto_parse_section(content_lines)
        if key == "required_env":
            value = _normalize_required_env(value)
        if value is not None:
            data[key] = value

    for scalar_key in ("id", "title", "module_ref"):
        if scalar_key in data and isinstance(data[scalar_key], list):
            data[scalar_key] = data[scalar_key][0] if data[scalar_key] else ""

    # Coerce fields that must be lists (handles LLM writing bare values)
    from aah.core.common.validators import FEATURE_SCHEMA
    list_fields = [k for k, v in FEATURE_SCHEMA.items() if v.get("type") is list]
    for list_key in list_fields:
        if list_key in data and not isinstance(data[list_key], list):
            if isinstance(data[list_key], str):
                data[list_key] = [data[list_key]]
            elif isinstance(data[list_key], dict):
                data[list_key] = [data[list_key]]
            else:
                data[list_key] = []

    if "id" not in data or not data["id"]:
        if filename_stem.startswith("F-"):
            data["id"] = filename_stem
        else:
            return None

    if "module_ref" not in data:
        data["module_ref"] = ""

    return data


def _split_md_sections(lines: list[str]) -> dict[str, list[str]]:
    """Split content by ## headings into {heading: [lines]}."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif line.startswith("---"):
            break
        elif current is not None:
            sections[current].append(line)
    return sections


def _auto_parse_section(lines: list[str]):
    """Infer data type from markdown structure."""
    stripped = [l for l in lines if l.strip()]
    if not stripped:
        return None

    if any(l.startswith("### ") for l in stripped):
        return _parse_sub_heading_blocks(stripped)

    has_top_bullets = any(
        l.strip().startswith("- ") and not l.startswith("  ") for l in stripped
    )
    has_nested = any(l.startswith("  ") and l.strip().startswith("- ") for l in stripped)

    if has_top_bullets and has_nested:
        return _parse_nested_bullets(stripped)

    if has_top_bullets:
        return [l.strip()[2:] for l in stripped if l.strip().startswith("- ")]

    return "\n".join(stripped).strip()


def _parse_sub_heading_blocks(lines: list[str]) -> list[dict]:
    """Parse ### sub-heading blocks into list of dicts."""
    blocks: list[dict] = []
    current: dict | None = None
    last_key: str | None = None

    for line in lines:
        if line.startswith("### "):
            if current:
                blocks.append(current)
            header = line[4:].strip()
            tc_id, _, desc = header.partition(": ")
            current = {"id": tc_id.strip(), "description": desc.strip()}
            last_key = None

        elif current is None:
            continue

        elif line.strip().startswith("- ") and not line.startswith("  "):
            content = line.strip()[2:]
            key, sep, value = content.partition(": ")

            if sep and value:
                mapped_key = key.lower().replace(" ", "_")
                current[mapped_key] = _coerce_value(value)
                last_key = None
            else:
                mapped_key = key.rstrip(":").lower().replace(" ", "_")
                current[mapped_key] = []
                last_key = mapped_key

        elif line.startswith("  ") and line.strip().startswith("- ") and last_key is not None:
            content = line.strip()[2:]
            target = current[last_key]

            if isinstance(target, list) and _is_key_value(content):
                if len(target) == 0:
                    current[last_key] = {}
                    target = current[last_key]
                    k, _, v = content.partition(": ")
                    target[k.strip()] = _coerce_value(v.strip())
                else:
                    target.append(content)
            elif isinstance(target, dict):
                if _is_key_value(content):
                    k, _, v = content.partition(": ")
                    target[k.strip()] = _coerce_value(v.strip())
                else:
                    current[last_key] = list(target.items()) if target else []
                    current[last_key].append(content)
            else:
                target.append(content)

    if current:
        blocks.append(current)

    return blocks


def _parse_nested_bullets(lines: list[str]) -> dict | None:
    """Parse top-level bullets with indented sub-bullets into a dict."""
    result: dict = {}
    last_key: str | None = None

    for line in lines:
        if line.strip().startswith("- ") and not line.startswith("  "):
            content = line.strip()[2:]
            key, sep, value = content.partition(": ")

            if sep and value:
                mapped = key.strip().lower().replace(" ", "_")
                result[mapped] = _coerce_value(value)
                last_key = mapped
            else:
                mapped = key.rstrip(":").strip().lower().replace(" ", "_")
                result[mapped] = []
                last_key = mapped

        elif line.startswith("  ") and line.strip().startswith("- ") and last_key is not None:
            content = line.strip()[2:]
            current_val = result[last_key]

            if isinstance(current_val, list):
                current_val.append(content)
            else:
                result[last_key] = [content]

    return result if result else None


def _is_key_value(text: str) -> bool:
    """Check if text is a key: value pair."""
    if ": " not in text:
        return False
    key = text.split(": ", 1)[0]
    return " " not in key.strip()


def _coerce_value(val: str):
    """Coerce string values to appropriate Python types."""
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _frontmatter_end_index(file_lines: list[str]) -> int:
    """Index of the closing ``---`` of leading YAML frontmatter, or -1 if none.

    A file is frontmatter-format only when its first line is ``---``; the closing
    fence is the next ``---``. Returns -1 for markdown-native files (no leading
    ``---``) so callers never mistake a body separator for the frontmatter close.
    """
    if not file_lines or file_lines[0].strip() != "---":
        return -1
    for i, line in enumerate(file_lines[1:], start=1):
        if line.strip() == "---":
            return i
    return -1


def _assert_parseable(path: Path) -> None:
    """Fail loudly if a write left the feature spec unparseable.

    A silently-corrupted spec (parse_feature_frontmatter -> None) makes the
    feature invisible to every downstream reader and stalls the build with a
    confusing "feature not found / no test command" symptom far from the cause.
    """
    if parse_feature_frontmatter(path) is None:
        raise ValueError(
            f"write to {path} produced an unparseable feature spec "
            f"(parse_feature_frontmatter returned None)"
        )


def write_feature_frontmatter(data: dict, path: Path) -> None:
    """Write feature data back to a .md file, preserving its native format.

    - Frontmatter-format specs (start with ``---``): rewrite the YAML block and
      keep the markdown body below the closing fence.
    - Markdown-native specs (no leading ``---``): do NOT coerce into a YAML
      frontmatter block — that drops the markdown body and, once append_section
      also runs, corrupts the file. Instead stamp scalar fields as ``## Section``
      entries (which round-trip through _parse_md_format), leaving the list/dict
      sections already in the body untouched.
    """
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except Exception:
            existing = ""

    is_markdown_native = bool(existing.strip()) and not existing.lstrip().startswith("---")

    if is_markdown_native:
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)):
                heading = key.replace("_", " ").title()
                append_section(path, heading, [str(value)])
        _assert_parseable(path)
        return

    body = ""
    lines = existing.split("\n")
    fm_end = _frontmatter_end_index(lines)
    if fm_end != -1 and fm_end + 1 < len(lines):
        body = "\n".join(lines[fm_end + 1:])

    yaml_str = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(yaml_str)
        f.write("---\n")
        if body:
            f.write(body)

    _assert_parseable(path)


def append_section(path: Path, heading: str, lines: list[str], accumulate: bool = False) -> None:
    """Append or replace a ## section in a feature .md file.

    If accumulate=False (default): replaces existing section content.
    If accumulate=True: appends new lines to existing section content.
    If section doesn't exist: appends at end (before any --- reference separator).
    """
    content = path.read_text(encoding="utf-8")
    section_marker = f"## {heading}"

    file_lines = content.splitlines()

    section_start = None
    section_end = None
    for i, line in enumerate(file_lines):
        if line.strip() == section_marker:
            section_start = i
        elif section_start is not None and (line.startswith("## ") or line.strip() == "---"):
            section_end = i
            break

    if accumulate and section_start is not None:
        if section_end is None:
            section_end = len(file_lines)
        insert_at = section_end
        file_lines = file_lines[:insert_at] + lines + [""] + file_lines[insert_at:]
    elif section_start is not None:
        if section_end is None:
            section_end = len(file_lines)
        new_section = [section_marker] + lines + [""]
        file_lines = file_lines[:section_start] + new_section + file_lines[section_end:]
    else:
        new_section = [section_marker] + lines + [""]
        # Never insert inside YAML frontmatter: the first ``---`` at i>0 is the
        # frontmatter's CLOSING fence, not a body separator. Inserting a
        # ``## Section`` before it lands the markdown heading inside the
        # frontmatter and breaks yaml.safe_load (parse -> None). Skip past the
        # frontmatter, then look for a trailing ``---`` reference separator.
        fm_end = _frontmatter_end_index(file_lines)
        separator_idx = None
        for i in range(fm_end + 1, len(file_lines)):
            if file_lines[i].strip() == "---":
                separator_idx = i
                break

        if separator_idx is not None:
            file_lines = file_lines[:separator_idx] + new_section + file_lines[separator_idx:]
        else:
            file_lines = file_lines + [""] + new_section

    path.write_text("\n".join(file_lines), encoding="utf-8")


def find_feature_file(features_dir: Path, feature_id: str) -> Path | None:
    """Find the feature .md file for a feature ID.

    Searches by filename first (fast), then by parsing content (fallback).
    """
    if not features_dir.is_dir():
        return None

    fid_upper = feature_id.upper()

    for p in sorted(features_dir.glob("*.md")):
        if p.stem.upper() == fid_upper:
            return p

    for p in sorted(features_dir.glob("*.md")):
        data = parse_feature_frontmatter(p)
        if data and data.get("id", "").upper() == fid_upper:
            return p

    return None


def load_feature_data(features_dir: Path, feature_id: str) -> dict | None:
    """Find and load feature data for a given feature ID."""
    path = find_feature_file(features_dir, feature_id)
    if path is None:
        return None
    return parse_feature_frontmatter(path)


def load_features_from_dir(features_dir: Path) -> list[dict]:
    """Load all feature definitions from .md files in a directory.

    This is THE canonical per-directory feature loader, built on
    ``parse_feature_frontmatter``. The ``load_features_from_yamls`` /
    ``load_features_from_md`` names in ``dag.py`` are thin backward-compatible
    aliases for it, and ``load_all_features_cumulative`` is a thin wrapper whose
    only behavioral difference is merging iteration archives.
    """
    features = []
    if not features_dir.is_dir():
        return features
    for md_file in sorted(features_dir.glob("*.md")):
        data = parse_feature_frontmatter(md_file)
        if data and "id" in data:
            features.append(data)
    return features


# Backward-compatible aliases for callers not yet updated
find_feature_yaml = find_feature_file
