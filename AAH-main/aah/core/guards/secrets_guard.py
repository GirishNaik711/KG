#!/usr/bin/env python3
"""
PreToolUse hook guard: block access to files containing secrets.

Reads hook input JSON from stdin (Claude Code convention).
Detects attempts to read, write, or cat/grep sensitive files like .env,
credentials, private keys, and tokens.
Exit 0 = allow, Exit 2 = block (stderr fed back to agent).

Handles three tool types:
- Read: checks file_path
- Bash: checks command for cat/head/tail/grep/less/more/vi/nano targeting sensitive files
- Write/Edit: checks file_path (prevents creating secrets in tracked locations)
"""

import json
import re
import sys
from pathlib import PurePosixPath


# File name patterns that indicate secret/sensitive content
SENSITIVE_FILE_PATTERNS = [
    # Environment files
    r"\.env$",
    r"\.env\.[a-zA-Z0-9_-]+$",  # Catches all .env.* files (including .example, .template, .sample) - allowlist overrides for safe templates
    # Credential files
    r"credentials\.json$",
    r"credentials\.yaml$",
    r"credentials\.yml$",
    r"service[_-]?account.*\.json$",
    # Key files
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"\.jks$",
    r"id_rsa$",
    r"id_ed25519$",
    r"id_ecdsa$",
    r"id_dsa$",
    r"\.ssh/config$",
    r"\.attestation-secret$",
    # Token/secret files
    r"\.netrc$",
    r"\.npmrc$",
    r"\.pypirc$",
    r"\.docker/config\.json$",
    r"\.kube/config$",
    r"token\.json$",
    r"tokens\.json$",
    r"secrets\.json$",
    r"secrets\.yaml$",
    r"secrets\.yml$",
    # AWS/Cloud specific
    r"\.aws/credentials$",
    r"\.aws/config$",
    r"\.gcloud/.*\.json$",
    r"\.config/gcloud/.*\.json$",
    # Database connection strings
    r"database\.yml$",
    r"database\.yaml$",
    # Vault/password files
    r"vault\.yml$",
    r"vault\.yaml$",
    r"\.vault[_-]pass.*$",
    r"\.password.*$",
    r"\.htpasswd$",
]

# Compiled patterns for performance
_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_FILE_PATTERNS]

# Directory patterns that are always sensitive
SENSITIVE_DIR_PATTERNS = [
    r"/\.ssh/",
    r"/\.aws/",
    r"/\.gnupg/",
    r"/\.config/gcloud/",
    r"/\.docker/",
    r"/\.kube/",
]

_COMPILED_DIR_PATTERNS = [re.compile(p) for p in SENSITIVE_DIR_PATTERNS]

# Commands that read file contents
FILE_READ_COMMANDS = [
    "cat", "head", "tail", "less", "more", "bat",
    "vi", "vim", "nvim", "nano", "emacs",
    "grep", "rg", "ag", "ack",
    "sed", "awk",
    "source", "\\.", ".",
    "cp", "scp",
]

# Allow-list: template files that match .env patterns but are safe to read/edit.
# These override the generic .env.* pattern and are conventionally used for
# documentation with placeholder values (committed to git).
# This allowlist is the single source of truth for the ".env family, minus safe
# templates" exception, and it governs Read, Write, Edit, and Bash in this guard.
# Layer 1 (settings.json) intentionally does NOT deny "Edit(**/.env.*)" — a blanket
# deny there cannot be negated and would block agents from editing .env.example, which
# the framework requires. Layer 1 hard-denies only the real-secret variants (.env,
# .env.local, .env.*.local); this guard covers the rest of the family (.env.production,
# .env.staging, …) while permitting the safe templates below.
ALLOWLIST_PATHS = [
    ".env.example",  # Template files with placeholder values
    ".env.template",
    ".env.sample",
]


def is_sensitive_path(file_path: str) -> bool:
    """Check if a file path matches sensitive file patterns."""
    # Normalize
    path = PurePosixPath(file_path)
    name = path.name
    path_str = str(path)

    # Check allow-list first
    for allowed in ALLOWLIST_PATHS:
        if name == allowed:
            return False

    # Check file name patterns
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(name):
            return True

    # Check directory patterns
    for pattern in _COMPILED_DIR_PATTERNS:
        if pattern.search(path_str):
            return True

    return False


def check_file_path(file_path: str) -> str | None:
    """
    Check if a file path targets a sensitive file.
    Returns a reason string if blocked, None if allowed.
    """
    if not file_path:
        return None

    if is_sensitive_path(file_path):
        name = PurePosixPath(file_path).name
        return f"Blocked: access to sensitive file '{name}' is not allowed"

    return None


def extract_paths_from_command(command: str) -> list[str]:
    """Extract potential file paths from a bash command."""
    paths = []

    # Split on pipes and semicolons to handle chained commands
    segments = re.split(r"[|;&]", command)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        # Get the command name (first word)
        words = segment.split()
        if not words:
            continue

        cmd = words[0].lstrip("\\")

        # Check if it's a file-reading command
        if cmd not in FILE_READ_COMMANDS:
            continue

        # Extract arguments that look like file paths (skip flags)
        for word in words[1:]:
            if word.startswith("-"):
                continue
            # Skip common non-path arguments
            if word in (".", "..", "/dev/null", "/dev/stdin", "/dev/stdout"):
                continue
            # If it looks like a path or filename, include it
            if "/" in word or "." in word:
                paths.append(word)

    # Also check for redirection targets
    redirect_match = re.findall(r"[<>]\s*(\S+)", command)
    paths.extend(redirect_match)

    return paths


def check_command(command: str | None) -> str | None:
    """
    Check if a bash command accesses sensitive files.
    Returns a reason string if blocked, None if allowed.
    """
    if not command:
        return None

    paths = extract_paths_from_command(command)
    for path in paths:
        reason = check_file_path(path)
        if reason:
            return reason

    # Check for inline env file sourcing patterns
    if re.search(r"\bsource\s+\S*\.env\b", command) or re.search(r"\.\s+\S*\.env\b", command):
        return "Blocked: sourcing .env files is not allowed"

    # Check for export of secrets from env files
    if re.search(r"\bexport\b.*\$\(cat\s+\S*\.env", command):
        return "Blocked: exporting secrets from .env files is not allowed"

    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    reason = None

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        reason = check_file_path(file_path)

    elif tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        reason = check_file_path(file_path)

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        reason = check_command(command)

    if reason:
        print(reason, file=sys.stderr)
        print(
            "\nAAH policy: access to files containing secrets (API keys, passwords, "
            "private keys, tokens) is blocked. If you need configuration values, ask "
            "the user to provide them or use environment variables at runtime.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
