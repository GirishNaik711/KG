#!/usr/bin/env python3
"""File I/O, YAML/JSON helpers, and template rendering utilities."""

import json
import os
import sys
import tempfile
from pathlib import Path
from string import Template
from typing import Any

import yaml


def read_yaml(path: Path) -> dict:
    """Read a YAML file and return its contents as a dict."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def write_yaml(data: dict, path: Path) -> None:
    """Write a dict to a YAML file with proper formatting for multi-line strings.

    Multi-line strings are formatted with literal block style (|) for readability.
    Single-line strings use default flow style.
    """
    # Create custom dumper for literal block style on multi-line strings
    class LiteralDumper(yaml.Dumper):
        pass

    def str_representer(dumper, data):
        if '\n' in data:
            return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
        return dumper.represent_scalar('tag:yaml.org,2002:str', data)

    LiteralDumper.add_representer(str, str_representer)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, Dumper=LiteralDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


def write_yaml_atomic(data: dict, path: Path) -> None:
    """Atomically replace a YAML file using a unique sibling temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        write_yaml(data, temp_path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_json(path: Path, retries: int = 3, retry_delay: float = 0.1) -> dict | list:
    """Read a JSON file and return its contents.

    Args:
        path: Path to JSON file
        retries: Number of retry attempts if JSON parsing fails (default: 3)
        retry_delay: Delay in seconds between retries (default: 0.1)

    Raises:
        json.JSONDecodeError: If file is malformed after all retries
        FileNotFoundError: If file doesn't exist
    """
    import time

    last_error = None
    for attempt in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                # Retry if file is empty or incomplete
                if not content or content.strip() == "" or content.strip() == "{":
                    if attempt < retries - 1:
                        time.sleep(retry_delay)
                        continue
                return json.loads(content)
        except json.JSONDecodeError as e:
            last_error = e
            if attempt < retries - 1:
                # Wait and retry - might be mid-write
                time.sleep(retry_delay)
                continue
            raise
        except Exception:
            # Don't retry on other errors (file not found, permissions, etc.)
            raise

    # Should never reach here, but if we do, raise the last error
    if last_error:
        raise last_error
    return {}


def _sanitise_for_json(obj):
    """Recursively strip ASCII control characters (0x00-0x1f except tab/newline/CR) from strings."""
    import re
    _CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    if isinstance(obj, dict):
        return {k: _sanitise_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise_for_json(v) for v in obj]
    if isinstance(obj, str):
        return _CTRL.sub("", obj)
    return obj


def write_json(data: dict | list, path: Path) -> None:
    """Write data to a JSON file, creating parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_sanitise_for_json(data), f, indent=2)
        f.write("\n")


def write_json_verified(data: dict | list, path: Path, artifact_name: str = "") -> None:
    """Write data to a JSON file and verify it was written correctly.

    Raises RuntimeError if the file doesn't exist or can't be read back after write.
    Use this for critical artifact files that gates depend on.
    """
    write_json(data, path)
    # Verify the file exists and is readable
    if not path.exists():
        label = f" ({artifact_name})" if artifact_name else ""
        raise RuntimeError(
            f"ARTIFACT WRITE FAILED{label}: file does not exist after write: {path}"
        )
    # Verify it's valid JSON
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        label = f" ({artifact_name})" if artifact_name else ""
        raise RuntimeError(
            f"ARTIFACT VERIFY FAILED{label}: file written but not readable as JSON: {path} — {e}"
        )


def append_jsonl(record: dict, path: Path) -> None:
    """Append a single JSON record to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """Read all records from a JSONL file."""
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def render_template(template_path: Path, variables: dict[str, str]) -> str:
    """Render a template file using Python string.Template substitution."""
    with open(template_path, "r", encoding="utf-8") as f:
        tmpl = Template(f.read())
    return tmpl.safe_substitute(variables)


def render_template_string(template_str: str, variables: dict[str, str]) -> str:
    """Render a template string using Python string.Template substitution."""
    tmpl = Template(template_str)
    return tmpl.safe_substitute(variables)


def ensure_dir(path: Path) -> Path:
    """Create directory and parents if they don't exist. Returns the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_hook_input() -> dict[str, Any]:
    """Read JSON hook input from stdin (Claude Code convention)."""
    return json.load(sys.stdin)


def write_hook_output(output: dict[str, Any]) -> None:
    """Write JSON hook output to stdout."""
    json.dump(output, sys.stdout, indent=2)
    print()


def read_text(path: Path) -> str:
    """Read a text file and return its contents."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(content: str, path: Path) -> None:
    """Write text content to a file, creating parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
