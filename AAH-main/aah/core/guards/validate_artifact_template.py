#!/usr/bin/env python3
"""
PostToolUse hook guard: validate artifact template conformance.

Checks that documents written to .aah/ contain all mandatory sections
declared in the matching activity YAML. Exit 0 = valid, Exit 2 = invalid.
"""

import json
import re
import sys
from pathlib import Path


# File extensions to validate (only markdown artifacts)
VALIDATABLE_EXTENSIONS = {".md", ".markdown"}

# Phases that require a `## Domain Alignment` section when an
# industry domain is attached to the project.
DOMAIN_ALIGNMENT_TARGET_PHASES = frozenset({"discuss", "architecture"})


def normalize_section(header: str) -> str:
    """Normalize a section header for comparison.

    Strips ## prefix, numbered prefixes (e.g., "1. "), and lowercases for comparison.
    """
    text = re.sub(r'^#+\s*', '', header.strip())
    text = re.sub(r'^\d+\.\s*', '', text)
    return text.strip().lower()


def get_artifact_phase(file_path: str) -> str | None:
    """Extract the phase from a .aah/ artifact path.

    Given .aah/research/constraints-risks.md, returns 'research'.
    Returns None if path is not a .aah markdown artifact.
    """
    path = Path(file_path)

    if path.suffix not in VALIDATABLE_EXTENSIONS:
        return None

    parts = path.parts
    try:
        rapids_idx = list(parts).index(".aah")
    except ValueError:
        return None

    relative_parts = parts[rapids_idx + 1:]
    if len(relative_parts) < 2:
        return None

    return relative_parts[0]


def parse_activity_id(content: str) -> str | None:
    """Extract the Activity ID from artifact frontmatter.

    Looks for **Activity:** <id> in the content.
    Returns the activity ID string, or None if not found.
    """
    match = re.search(r'^\*\*Activity:\*\*\s*(.+)$', content, re.MULTILINE)
    if not match:
        return None
    activity_id = match.group(1).strip()
    # Skip template placeholders like {activity_id}
    if activity_id.startswith("{"):
        return None
    return activity_id


def find_activity_for_artifact(content: str, file_path: str) -> dict | None:
    """Find the activity YAML artifact spec matching this artifact.

    Uses the Activity ID from the artifact frontmatter for a direct
    registry lookup — loads a single YAML file instead of iterating.

    Falls back to phase + template filename matching via registry
    ``templates`` field if no Activity ID is found in the frontmatter.
    Both paths load exactly one YAML file.

    Returns the artifact spec dict (with 'sections' list), or None.
    """
    from aah.core.activities.loader import (
        load_activity,
        load_registry,
        resolve_activity_library_path,
    )
    try:
        lib_path = resolve_activity_library_path()
        registry = load_registry(lib_path)
    except (SystemExit, Exception):
        return None

    activities_map = registry.get("activities", {})
    artifact_filename = Path(file_path).name

    # Primary: direct lookup by Activity ID from frontmatter
    activity_id = parse_activity_id(content)
    if activity_id and activity_id in activities_map:
        meta = activities_map[activity_id]
        activity = load_activity(lib_path, meta.get("file", ""))
        if activity:
            for art_spec in activity.get("artifacts", []):
                if art_spec.get("template") == artifact_filename:
                    return art_spec

    # Fallback: match by phase + template filename using registry templates field
    # This avoids loading any YAML files during the search — only loads
    # the single matched activity YAML once the target is identified.
    phase = get_artifact_phase(file_path)
    if not phase:
        return None

    for _aid, meta in activities_map.items():
        if meta.get("phase") != phase:
            continue
        if artifact_filename not in (meta.get("templates") or []):
            continue
        # Found the activity — load its YAML to get the full artifact spec
        activity = load_activity(lib_path, meta.get("file", ""))
        if not activity:
            continue
        for art_spec in activity.get("artifacts", []):
            if art_spec.get("template") == artifact_filename:
                return art_spec

    return None


def validate_artifact_content(content: str, artifact_spec: dict) -> list[str]:
    """Check that artifact content has all mandatory sections from YAML.

    Returns list of missing section names. Extra sections in the
    artifact are allowed -- only YAML sections are mandatory.
    """
    yaml_sections = artifact_spec.get("sections", [])
    if not yaml_sections:
        return []

    artifact_headers = re.findall(r'^##\s+(.+)$', content, re.MULTILINE)
    normalized_artifact = {normalize_section(h) for h in artifact_headers}

    missing = []
    for section in yaml_sections:
        if normalize_section(section) not in normalized_artifact:
            missing.append(section)

    return missing


def _project_has_attached_domain(file_path: str) -> tuple[bool, list[str]]:
    """Detect whether the project hosting this artifact has an industry domain
    attached, and if so, return a list of reference terms that the Domain
    Alignment section should plausibly name.

    Returns (has_domain, reference_terms). `reference_terms` is empty when
    no domain is attached or the brief cannot be loaded.
    """
    # Walk up from the artifact path to find .aah/manifest.yaml
    parts = Path(file_path).parts
    try:
        rapids_idx = list(parts).index(".aah")
    except ValueError:
        return False, []
    project_root = Path(*parts[:rapids_idx])
    manifest_path = project_root / ".aah" / "manifest.yaml"
    if not manifest_path.is_file():
        return False, []

    try:
        from aah.core.common.manifest import load_manifest
        manifest = load_manifest(manifest_path)
    except Exception:
        return False, []

    domain_path = manifest.get("industry_domain_path") if isinstance(manifest, dict) else None
    if not domain_path:
        return False, []

    try:
        from aah.core.domain_briefs.loader import load_node
        node = load_node(domain_path)
    except Exception:
        return False, []
    if node is None:
        return True, []

    reference_terms: list[str] = []
    for p in (node.get("processes") or [])[:8]:
        if p.get("name"):
            reference_terms.append(p["name"])
    for d in (node.get("data_entities") or [])[:6]:
        if d.get("name"):
            reference_terms.append(d["name"])
    for g in (node.get("glossary") or [])[:10]:
        if g.get("term"):
            reference_terms.append(g["term"])
    for r in (node.get("regulations") or [])[:6]:
        if r.get("name"):
            reference_terms.append(r["name"])
    return True, reference_terms


def validate_domain_alignment(
    content: str, file_path: str, phase: str
) -> list[str]:
    """Enforce a `## Domain Alignment` section when an industry domain is attached.

    - Only applies to research/analysis phases.
    - If no domain is attached, returns []
    - Otherwise, requires a `## Domain Alignment` heading AND at least 2
      named references from the merged Domain Brief (processes, data
      entities, glossary terms, or regulations).
    """
    if phase not in DOMAIN_ALIGNMENT_TARGET_PHASES:
        return []

    has_domain, reference_terms = _project_has_attached_domain(file_path)
    if not has_domain:
        return []

    errors: list[str] = []
    if not re.search(r"^\s*##\s+Domain\s+Alignment\s*$", content, re.MULTILINE | re.IGNORECASE):
        errors.append(
            "Missing `## Domain Alignment` section — required when an industry "
            "domain is attached (manifest.industry_domain_path is set)."
        )
        return errors

    # Count how many reference terms from the domain brief actually appear
    # in the artifact content (anywhere, not just inside the Domain Alignment
    # section — a more lenient check reduces false-positives).
    if reference_terms:
        lower_content = content.lower()
        hit_count = sum(1 for term in reference_terms if term.lower() in lower_content)
        if hit_count < 2:
            errors.append(
                f"`## Domain Alignment` section should reference at least 2 items "
                f"from the attached Domain Brief. Found {hit_count} reference(s). "
                f"Available terms include: {reference_terms[:8]}"
            )
    return errors


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        sys.exit(0)

    # For Write: content is in tool_input. For Edit: read the file after edit.
    if tool_name == "Edit" or "content" not in tool_input:
        try:
            content = Path(file_path).expanduser().read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            # File doesn't exist yet or can't be read — skip validation
            sys.exit(0)
    else:
        content = tool_input.get("content", "")

    # Activity YAML-driven validation
    artifact_spec = find_activity_for_artifact(content, file_path)

    if artifact_spec is not None:
        missing = validate_artifact_content(content, artifact_spec)
        if missing:
            print(
                f"Template conformance error for '{Path(file_path).name}':\n"
                f"Missing mandatory sections (from activity YAML): {missing}\n"
                f"Required: {artifact_spec.get('sections', [])}",
                file=sys.stderr,
            )
            sys.exit(2)

    # Domain Alignment check (only when an industry domain is attached)
    phase = get_artifact_phase(file_path)
    if phase:
        alignment_errors = validate_domain_alignment(content, file_path, phase)
        if alignment_errors:
            for err in alignment_errors:
                print(err, file=sys.stderr)
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
