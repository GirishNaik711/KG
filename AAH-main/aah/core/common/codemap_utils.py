"""Shared codemap utilities for AAH scripts.

CLI usage:
  aah run core.common.codemap_utils write-metadata \
    --project-path <path> --operation <scout|update|embed> --wave <N> --stats '<json>'
"""

import argparse
import json
import time
from pathlib import Path


def get_codemap(project_path: Path):
    """Get CodeMapScale instance with AAH-standard DB path.

    Returns a CodeMapScale instance configured to use .aah/codebase-intel/codemap.db.
    """
    from codemap_scale.orchestrator import CodeMapScale

    db_path = project_path / ".aah" / "codebase-intel" / "codemap.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return CodeMapScale(root=str(project_path), db_path=str(db_path))


def is_available() -> bool:
    """Check if codemap-scale is installed."""
    try:
        import codemap_scale  # noqa: F401

        return True
    except ImportError:
        return False


def get_db_path(project_path: Path) -> Path:
    """Return the standard codemap.db path for a project."""
    return project_path / ".aah" / "codebase-intel" / "codemap.db"


def get_metadata_path(project_path: Path) -> Path:
    """Return the standard codemap-metadata.json path for a project."""
    return project_path / ".aah" / "codebase-intel" / "codemap-metadata.json"


def read_metadata(project_path: Path) -> dict | None:
    """Read codemap-metadata.json if it exists. Returns None if not found."""
    meta_path = get_metadata_path(project_path)
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def write_metadata(aah_path: Path, result: dict, operation: str, wave: int) -> None:
    """Write codemap-metadata.json after scout/update.

    Args:
        aah_path: Path to .aah/ directory
        result: Result dict from codemap scout/update operation
        operation: Operation name (scout, update, embed)
        wave: Current wave number
    """
    import codemap_scale

    meta = {
        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "indexer": "codemap-scale",
        "version": codemap_scale.__version__,
        "database_path": ".aah/codebase-intel/codemap.db",
        "files_scanned": result.get("files", 0),
        "symbols_extracted": result.get("symbols", 0),
        "relations_extracted": result.get("relations", 0),
        "last_operation": operation,
        "last_wave": wave,
        "semantic_index_built": result.get("semantic_index_built", False),
    }
    meta_path = aah_path / "codebase-intel" / "codemap-metadata.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding='utf-8')


def main():
    """CLI entry point for codemap utilities."""
    parser = argparse.ArgumentParser(description="Codemap utilities")
    sub = parser.add_subparsers(dest="command")

    wm = sub.add_parser("write-metadata", help="Write codemap-metadata.json after scout/update")
    wm.add_argument("--project-path", required=True, help="Path to project root")
    wm.add_argument("--operation", required=True, choices=["scout", "update", "embed"],
                    help="Operation that was performed")
    wm.add_argument("--wave", required=True, type=int, help="Current wave number")
    wm.add_argument("--stats", required=True, help="JSON stats from codemap command output")

    sub.add_parser("doctor", help="Check codemap-scale installation")

    args = parser.parse_args()

    if args.command == "doctor":
        print("Checking codemap-scale installation...")
        print("codemap-scale is installed and available." if is_available() else "codemap-scale is NOT installed.")
        return

    if args.command == "write-metadata":
        project_path = Path(args.project_path)
        aah_path = project_path / ".aah"

        try:
            stats = json.loads(args.stats)
        except json.JSONDecodeError:
            # Try to extract relevant fields from raw output
            stats = {}

        write_metadata(aah_path, stats, args.operation, args.wave)
        print(f"Metadata written for wave {args.wave} ({args.operation})")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
