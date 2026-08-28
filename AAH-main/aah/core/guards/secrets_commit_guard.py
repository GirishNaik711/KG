#!/usr/bin/env python3
"""
PreToolUse:Bash guard: block git commit if leaked secrets are detected.

Runs Gitleaks with --no-git (filesystem only, <2s) before each git commit.
Exit 0 = allow, Exit 2 = block (stderr fed back to agent).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


def check_command(command: str) -> str | None:
    """Check if a git commit should be blocked due to leaked secrets.

    Returns a reason string if blocked, None if allowed.
    Only triggers on `git commit` commands — all other commands pass through.
    """
    if not re.search(r"\bgit\s+commit\b", command):
        return None

    if not shutil.which("gitleaks"):
        return None

    from pathlib import Path

    # Scan from the git repo root (where the commit happens), not the active
    # AAH project, so the repo-level .gitleaks.toml allowlist is used.
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return None
        project_path = Path(proc.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return None

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="gitleaks-")
    os.close(tmp_fd)
    report_file = Path(tmp_path)

    try:
        cmd = [
            "gitleaks", "detect",
            "--source", str(project_path),
            "--no-git",
            "--report-format", "json",
            "--report-path", str(report_file),
            "--no-banner",
            "--exit-code", "1",
        ]

        ignore_path = project_path / ".gitleaksignore"
        if ignore_path.exists():
            cmd.extend(["--gitleaks-ignore-path", str(ignore_path)])

        gitleaks_config = project_path / ".gitleaks.toml"
        if gitleaks_config.exists():
            cmd.extend(["--config", str(gitleaks_config)])

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        if proc.returncode == 0:
            return None

        if proc.returncode != 1:
            return None

        findings = []
        if report_file.exists():
            try:
                data = json.loads(report_file.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    # .aah/ contains framework-owned state and attested
                    # evidence. Ignore it wherever it appears, including
                    # copies inside feature worktrees.
                    findings = [
                        finding for finding in data
                        if ".aah" not in str(finding.get("File", ""))
                        .replace("\\", "/").split("/")
                    ]
            except (json.JSONDecodeError, OSError):
                pass
    finally:
        report_file.unlink(missing_ok=True)

    if not findings:
        return None

    lines = [f"Blocked: {len(findings)} secret(s) detected in working directory"]
    for f in findings[:5]:
        lines.append(
            f"  - {f.get('File', '?')}:{f.get('StartLine', '?')} "
            f"({f.get('RuleID', 'unknown rule')}): {f.get('Description', '')}"
        )
    if len(findings) > 5:
        lines.append(f"  ... and {len(findings) - 5} more")

    return "\n".join(lines)


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    reason = check_command(command)
    if reason:
        print(reason, file=sys.stderr)
        print(
            "\nAAH policy: Secrets must not be committed to git. "
            "Remove the secret and use environment variables or a secrets manager instead. "
            "If this is a test fixture, add a suppression in .aah/security/suppressions.yaml.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
