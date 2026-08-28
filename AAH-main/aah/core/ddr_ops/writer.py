#!/usr/bin/env python3
"""Write, update, and delete DDR YAML files."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_framework_root
from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.ddr_ops.loader import LAYERS, resolve_resources_path, load_ddr_by_id


def get_ddr_directory(resources_path: Path, archetype: str, layer_number: int) -> Path:
    """Get the directory path for a DDR given archetype and layer."""
    layer_info = LAYERS.get(layer_number)
    if not layer_info:
        raise ValueError(f"Invalid layer number: {layer_number}. Must be 1-9.")
    layer_slug = layer_info["slug"]
    return resources_path / archetype / "decisions" / f"L{layer_number}-{layer_slug}"


def generate_filename(ddr_id: str, decision_question: str) -> str:
    """Generate a filename from DDR ID and decision question.

    Format: DDR-L{n}-{seq}-{descriptive-slug}.yaml
    """
    # Extract a short slug from the decision question
    words = decision_question.lower().strip().split()
    # Remove common filler words
    stop_words = {"how", "what", "which", "is", "are", "the", "a", "an", "does", "do",
                  "should", "when", "where", "for", "to", "in", "of", "and", "or", "be"}
    slug_words = [w for w in words if w.isalpha() and w not in stop_words][:4]
    slug = "-".join(slug_words) if slug_words else "decision"
    return f"{ddr_id}-{slug}.yaml"


def build_ddr_template(
    ddr_id: str,
    layer_number: int,
    decision_question: str,
    category: str = "archetype",
    forces: list[dict] | None = None,
    options: list[dict] | None = None,
    anti_patterns: list[str] | None = None,
    depends_on: list[str] | None = None,
    skip_if: list[dict] | None = None,
    mandate: str | None = None,
    mandate_id: str | None = None,
    seed: dict | None = None,
) -> dict:
    """Build a DDR YAML structure from provided fields (schema v2.0)."""
    layer_info = LAYERS.get(layer_number, {})

    ddr = {
        "schema_version": "2.0",
        "id": ddr_id,
        "category": category,
        "layer": layer_info.get("name", f"Layer {layer_number}"),
        "layer_number": layer_number,
        "depends_on": depends_on or [],
        "skip_if": skip_if or [],
        "decision_question": decision_question,
        "forces": forces or [
            {
                "id": "force-1",
                "tension": "[X vs Y]",
                "description": "[Why this tension exists]",
            },
            {
                "id": "force-2",
                "tension": "[A vs B]",
                "description": "[Why this tension exists]",
            },
        ],
        "options": options or [
            {
                "label": "option-a",
                "description": "[Describe approach A]",
                "force_resolution": {
                    "force-1": {"verdict": "favours", "detail": "[How option-a resolves force-1]"},
                    "force-2": {"verdict": "neutral", "detail": "[How option-a resolves force-2]"},
                },
            },
            {
                "label": "option-b",
                "description": "[Describe approach B]",
                "force_resolution": {
                    "force-1": {"verdict": "trades-off", "detail": "[How option-b resolves force-1]"},
                    "force-2": {"verdict": "favours", "detail": "[How option-b resolves force-2]"},
                },
            },
        ],
        "anti_patterns": anti_patterns or ["[Describe a common mistake to avoid]"],
        "mandate": mandate,
        "mandate_id": mandate_id,
        "seed": seed or {
            "tools_by_option": {},
            "note": "Verify tool versions and compatibility before implementing.",
        },
    }
    return ddr


def write_ddr(resources_path: Path, archetype: str, layer_number: int,
              ddr_data: dict, filename: str | None = None,
              output_dir: Path | None = None) -> Path:
    """Write a DDR YAML file to the correct location or a custom directory.

    If output_dir is provided, writes to that directory instead of the default
    archetype/layer path. Returns the path of the written file.
    """
    if output_dir is not None:
        ddr_dir = output_dir
    else:
        ddr_dir = get_ddr_directory(resources_path, archetype, layer_number)
    ddr_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        filename = generate_filename(
            ddr_data.get("id", "DDR-L0-000"),
            ddr_data.get("decision_question", ""),
        )

    output_path = ddr_dir / filename
    write_yaml(ddr_data, output_path)
    return output_path


def update_ddr(ddr_path: Path, changes: dict) -> dict:
    """Apply changes to an existing DDR file.

    changes is a dict where keys are field names and values are the new values.
    For list fields (forces, options, anti_patterns), the change can be:
      - A full replacement list
      - A dict with 'append' key containing items to add

    Returns the updated DDR data.
    """
    if not ddr_path.exists():
        raise FileNotFoundError(f"DDR not found: {ddr_path}")

    data = read_yaml(ddr_path)

    for field, value in changes.items():
        if field.startswith("_"):
            continue  # Skip internal fields

        if isinstance(value, dict) and "append" in value:
            # Append to existing list
            existing = data.get(field, [])
            if isinstance(existing, list):
                existing.extend(value["append"])
                data[field] = existing
        else:
            data[field] = value

    write_yaml(data, ddr_path)
    return data


def delete_ddr(resources_path: Path, ddr_id: str, archetype: str) -> Path | None:
    """Delete a DDR file. Returns the path that was deleted, or None if not found."""
    from aah.core.ddr_ops.loader import load_all_ddrs

    all_ddrs = load_all_ddrs(resources_path, archetype)
    for ddr in all_ddrs:
        if ddr.get("id") == ddr_id:
            ddr_path = Path(ddr["_path"])
            if ddr_path.exists():
                os.remove(ddr_path)
                return ddr_path
    return None


def diff_ddr(existing_path: Path, new_content: str) -> dict:
    """Compare existing DDR with new content and suggest changes.

    Returns a dict describing what would change per field.
    """
    existing = read_yaml(existing_path)

    # This is a simplified diff — in practice the LLM would do the semantic comparison
    # Here we just identify which fields in the existing DDR might benefit from updates
    result = {
        "existing_id": existing.get("id"),
        "fields_present_in_existing": [k for k in existing if not k.startswith("_")],
        "new_content_length": len(new_content),
        "suggestion": "Use LLM to compare existing DDR fields against new content and propose specific field updates.",
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="DDR writer — create, update, and delete DDRs")
    sub = parser.add_subparsers(dest="command", required=True)

    # draft
    draft_p = sub.add_parser("draft", help="Generate a draft DDR YAML")
    draft_p.add_argument("--archetype", type=str, required=True)
    draft_p.add_argument("--layer", type=int, required=True)
    draft_p.add_argument("--question", type=str, required=True, help="Decision question")
    draft_p.add_argument("--category", type=str, default="archetype",
                         choices=["archetype", "shared"], help="DDR category")
    draft_p.add_argument("--forces", type=str, default=None,
                         help="JSON array of forces [{id, tension, description}]")
    draft_p.add_argument("--options", type=str, default=None,
                         help="JSON array of options [{label, description, force_resolution}]")
    draft_p.add_argument("--anti-patterns", type=str, default=None, help="JSON array of anti-patterns")
    draft_p.add_argument("--depends-on", type=str, default=None, help="JSON array of DDR IDs")
    draft_p.add_argument("--skip-if", type=str, default=None,
                         help="JSON array of skip conditions [{ddr_id, resolved_to, reason}]")
    draft_p.add_argument("--mandate", type=str, default=None)
    draft_p.add_argument("--output", type=Path, default=None, help="Output file path")

    # write
    write_p = sub.add_parser("write", help="Write a DDR YAML from a draft file")
    write_p.add_argument("--archetype", type=str, required=True)
    write_p.add_argument("--layer", type=int, required=True)
    write_p.add_argument("--id", type=str, required=True, help="DDR ID (e.g., DDR-L3-004)")
    write_p.add_argument("--input", type=Path, required=True, help="Path to draft YAML")
    write_p.add_argument("--output-dir", type=Path, default=None,
                         help="Custom output directory (overrides default archetype/layer path)")

    # update
    update_p = sub.add_parser("update", help="Update an existing DDR")
    update_p.add_argument("--id", type=str, required=True, help="DDR ID to update")
    update_p.add_argument("--archetype", type=str, required=True)
    update_p.add_argument("--changes-file", type=Path, required=True,
                          help="JSON file with field changes")

    # delete
    delete_p = sub.add_parser("delete", help="Delete a DDR")
    delete_p.add_argument("--id", type=str, required=True, help="DDR ID to delete")
    delete_p.add_argument("--archetype", type=str, required=True)

    # diff
    diff_p = sub.add_parser("diff", help="Compare existing DDR with new content")
    diff_p.add_argument("--existing", type=Path, required=True, help="Path to existing DDR")
    diff_p.add_argument("--new-content", type=Path, required=True, help="Path to new content file")

    args = parser.parse_args()
    resources_path = resolve_resources_path()

    if args.command == "draft":
        from aah.core.ddr_ops.loader import get_next_sequence
        seq = get_next_sequence(resources_path, args.archetype, args.layer)
        ddr_id = f"DDR-L{args.layer}-{seq:03d}"

        forces = json.loads(args.forces) if args.forces else None
        options = json.loads(args.options) if args.options else None
        anti_patterns = json.loads(args.anti_patterns) if args.anti_patterns else None
        depends_on = json.loads(args.depends_on) if args.depends_on else None
        skip_if = json.loads(args.skip_if) if args.skip_if else None

        ddr = build_ddr_template(
            ddr_id=ddr_id,
            layer_number=args.layer,
            decision_question=args.question,
            category=args.category,
            forces=forces,
            options=options,
            anti_patterns=anti_patterns,
            depends_on=depends_on,
            skip_if=skip_if,
            mandate=args.mandate,
        )

        if args.output:
            write_yaml(ddr, args.output)
            print(json.dumps({"id": ddr_id, "path": str(args.output)}, indent=2))
        else:
            import yaml as yaml_mod
            print(yaml_mod.dump(ddr, default_flow_style=False, sort_keys=False))

    elif args.command == "write":
        input_data = read_yaml(args.input)
        input_data["id"] = args.id  # Ensure ID matches
        output_dir = args.output_dir if args.output_dir else None
        output_path = write_ddr(resources_path, args.archetype, args.layer, input_data,
                                output_dir=output_dir)
        print(json.dumps({"id": args.id, "path": str(output_path)}, indent=2))

    elif args.command == "update":
        if not args.changes_file.exists():
            print(f"Error: changes file not found: {args.changes_file}", file=sys.stderr)
            sys.exit(1)

        changes = json.loads(args.changes_file.read_text(encoding="utf-8"))

        # Find the DDR file path
        ddr = load_ddr_by_id(resources_path, args.id, args.archetype)
        if ddr is None:
            print(f"Error: DDR '{args.id}' not found in archetype '{args.archetype}'", file=sys.stderr)
            sys.exit(1)

        ddr_path = Path(ddr["_path"])
        updated = update_ddr(ddr_path, changes)
        print(json.dumps({"id": args.id, "path": str(ddr_path), "updated_fields": list(changes.keys())}, indent=2))

    elif args.command == "delete":
        deleted_path = delete_ddr(resources_path, args.id, args.archetype)
        if deleted_path:
            print(json.dumps({"id": args.id, "deleted": str(deleted_path)}, indent=2))
        else:
            print(f"Error: DDR '{args.id}' not found in archetype '{args.archetype}'", file=sys.stderr)
            sys.exit(1)

    elif args.command == "diff":
        if not args.existing.exists():
            print(f"Error: existing DDR not found: {args.existing}", file=sys.stderr)
            sys.exit(1)
        if not args.new_content.exists():
            print(f"Error: new content file not found: {args.new_content}", file=sys.stderr)
            sys.exit(1)

        new_text = args.new_content.read_text(encoding="utf-8")
        result = diff_ddr(args.existing, new_text)
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
