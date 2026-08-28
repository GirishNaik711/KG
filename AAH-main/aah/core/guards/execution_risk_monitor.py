#!/usr/bin/env python3
"""
PostToolUse hook: detect cross-step execution anti-patterns.

Accumulates lightweight state across Bash commands in a session and
emits targeted warnings when it detects behavioral anti-patterns that
span multiple steps — patterns invisible to single-command guards.

Inspired by the AHE paper's ExecutionRiskHintsMiddleware, which was
one of the highest-impact harness components (+2.2pp pass@1 alone).

Exit 0 always (advisory only — warns but never blocks).
Warnings are printed to stderr, which Claude Code feeds back to the agent.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

if sys.platform != "win32":
    try:
        import fcntl
        HAS_FCNTL = True
    except ImportError:
        HAS_FCNTL = False
else:
    HAS_FCNTL = False
    try:
        import msvcrt
    except ImportError:
        pass


# --- State file management ---

def _state_path() -> Path:
    """Session-stable temp file for cross-call state."""
    # Use PPID (parent process = Claude Code) so all hook invocations
    # in one session share state, but different sessions don't collide.
    ppid = os.getppid()
    return Path(tempfile.gettempdir()) / f"rapids_exec_risk_{ppid}.json"


def load_state() -> dict:
    path = _state_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "error_counts": {},
        "timeout_counts": {},
        "command_history": [],
        "source_changed_since_build": False,
        "warnings_emitted": [],
        "total_commands": 0,
    }


def save_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state), encoding='utf-8')
        os.replace(str(tmp), str(path))
    except OSError:
        pass


# --- Pattern detection ---

# Max warnings of the same type per session to avoid spamming
MAX_WARNINGS_PER_TYPE = 3

# Threshold for doom loop / timeout loop detection
REPEAT_THRESHOLD = 3


def normalize_error(stderr: str) -> str:
    """Extract a stable error signature from stderr.

    Strips file paths, line numbers, PIDs, timestamps, and hex addresses
    so that the same logical error produces the same key even if the
    surface details change between runs.
    """
    if not stderr:
        return ""
    # Take first meaningful line (skip blank lines)
    lines = [l.strip() for l in stderr.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    sig = lines[0]
    # Strip file paths
    sig = re.sub(r"/[\w/._-]+", "<path>", sig)
    # Strip line numbers
    sig = re.sub(r"line \d+", "line N", sig)
    # Strip timestamps (before PIDs so timestamps don't get partially mangled)
    sig = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "TIMESTAMP", sig)
    # Strip PIDs and ports
    sig = re.sub(r"\b\d{4,}\b", "N", sig)
    # Strip hex addresses
    sig = re.sub(r"0x[0-9a-fA-F]+", "0xN", sig)
    return sig[:200]


def normalize_command(command: str) -> str:
    """Reduce a command to its structural shape for timeout tracking.

    Uses first 2 words so that 'npm install' and 'npm install express'
    share the same shape.
    """
    if not command:
        return ""
    words = command.split()[:2]
    return " ".join(words)


def _should_warn(state: dict, warning_type: str) -> bool:
    """Rate-limit warnings: max N of the same type per session."""
    count = sum(1 for w in state.get("warnings_emitted", []) if w == warning_type)
    return count < MAX_WARNINGS_PER_TYPE


def _record_warning(state: dict, warning_type: str) -> None:
    state.setdefault("warnings_emitted", []).append(warning_type)


# --- Individual pattern detectors ---


def detect_doom_loop(state: dict, command: str, exit_code: int, stderr: str) -> str | None:
    """Pattern 1: Same error repeated N+ times without changing approach.

    The agent keeps retrying the same failing command without modifying
    its strategy. This burns session budget on a known-failing path.
    """
    if exit_code == 0 or not stderr:
        return None

    sig = normalize_error(stderr)
    if not sig:
        return None

    counts = state.setdefault("error_counts", {})
    counts[sig] = counts.get(sig, 0) + 1

    if counts[sig] >= REPEAT_THRESHOLD and _should_warn(state, "doom_loop"):
        _record_warning(state, "doom_loop")
        return (
            f"⚠ EXECUTION RISK — doom loop detected: this error has occurred "
            f"{counts[sig]} times without a change in approach.\n"
            f"Error: {sig}\n"
            f"Change your strategy — retrying the same command will produce "
            f"the same result. Consider: different tool, different flag, "
            f"different approach entirely."
        )
    return None


def detect_timeout_loop(state: dict, command: str, exit_code: int, stderr: str) -> str | None:
    """Pattern 2: Same long command timing out repeatedly.

    The agent runs a build/install/test that times out, then runs it
    again identically. Each retry wastes the timeout budget.
    """
    is_timeout = (
        "timed out" in (stderr or "").lower()
        or "timeout" in (stderr or "").lower()
        or exit_code == 124  # timeout command exit code
    )
    if not is_timeout:
        return None

    shape = normalize_command(command)
    if not shape:
        return None

    counts = state.setdefault("timeout_counts", {})
    counts[shape] = counts.get(shape, 0) + 1

    if counts[shape] >= 2 and _should_warn(state, "timeout_loop"):
        _record_warning(state, "timeout_loop")
        return (
            f"⚠ EXECUTION RISK — timeout loop: '{shape}...' has timed out "
            f"{counts[shape]} times.\n"
            f"Break the work into smaller steps, use a background process, "
            f"or try a lighter alternative."
        )
    return None


def detect_connection_refused(state: dict, command: str, exit_code: int, stderr: str) -> str | None:
    """Pattern 3: Tests failing with connection errors (server not running).

    The agent runs integration tests or curl commands that fail because
    the target service hasn't been started.
    """
    if exit_code == 0:
        return None

    combined = (stderr or "") + " " + (command or "")
    conn_patterns = [
        r"connection\s*refused",
        r"ECONNREFUSED",
        r"connect\s+ECONNREFUSED",
        r"failed\s+to\s+connect",
        r"couldn.t\s+connect",
        r"No\s+connection\s+could\s+be\s+made",
        r"Connection\s+reset\s+by\s+peer",
    ]

    for pattern in conn_patterns:
        if re.search(pattern, combined, re.IGNORECASE):
            sig = "connection_refused"
            counts = state.setdefault("error_counts", {})
            counts[sig] = counts.get(sig, 0) + 1

            if counts[sig] >= 2 and _should_warn(state, "conn_refused"):
                _record_warning(state, "conn_refused")
                return (
                    "⚠ EXECUTION RISK — repeated connection failures. "
                    "Is the service running? Start the server/database before "
                    "running integration tests or API calls."
                )
            return None
    return None


def detect_shallow_validation(state: dict, command: str, exit_code: int, stderr: str) -> str | None:
    """Pattern 4: Agent validates with file-existence or import checks
    instead of running actual tests.

    Common shortcuts: `test -f output.json`, `python -c "import mymod"`,
    `python -c "json.load(open(..., encoding='utf-8'))"` — these prove nothing about correctness.
    """
    if exit_code != 0:
        return None

    shallow_patterns = [
        # File existence checks used as "validation"
        (r"test\s+-[fe]\s+", "file-existence check"),
        (r"\[\s+-[fe]\s+", "file-existence check"),
        # Import-only checks
        (r"python[3]?\s+-c\s+['\"]import\s+", "import-only check"),
        # py_compile as validation
        (r"py_compile", "py_compile check"),
        # JSON parse-only validation
        (r"python[3]?\s+-c\s+['\"]json\.load", "parse-only check"),
    ]

    for pattern, desc in shallow_patterns:
        if re.search(pattern, command):
            # Only warn if this looks like a final validation step
            # (i.e., multiple commands have already run)
            total = state.get("total_commands", 0)
            if total >= 5 and _should_warn(state, "shallow_validation"):
                _record_warning(state, "shallow_validation")
                return (
                    f"⚠ EXECUTION RISK — shallow validation detected ({desc}). "
                    f"AAH requires functional tests against the real running "
                    f"system. Run the project's test suite (pytest, npm test, etc.) "
                    f"to verify correctness, not just that files exist or imports succeed."
                )
            return None
    return None


def detect_proxy_test(state: dict, command: str, exit_code: int, tool_name: str, tool_input: dict) -> str | None:
    """Pattern 5: Agent writes a self-made validation script instead of
    running the project's existing test suite.

    If the agent creates a new test_*.py or check_*.py and runs it,
    rather than running the tests defined in the feature spec, the
    "validation" proves nothing about the real acceptance criteria.
    """
    # Track when agent creates ad-hoc validation scripts
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        name = Path(file_path).name if file_path else ""
        if re.match(r"(check_|verify_|validate_|test_quick|test_check)", name):
            state.setdefault("adhoc_test_files", []).append(file_path)
            return None

    # Check if a Bash command runs an ad-hoc test file
    if tool_name == "Bash":
        adhoc_files = state.get("adhoc_test_files", [])
        for f in adhoc_files:
            name = Path(f).name
            if name in command and _should_warn(state, "proxy_test"):
                _record_warning(state, "proxy_test")
                return (
                    f"⚠ EXECUTION RISK — self-written validation detected. "
                    f"You created and ran '{name}' as a validator. "
                    f"AAH requires running the project's defined test suite, "
                    f"not ad-hoc check scripts. Use the tests from the feature "
                    f"spec or sprint contract."
                )
    return None


def detect_rebuild_gap(state: dict, command: str, exit_code: int, tool_name: str, tool_input: dict) -> str | None:
    """Pattern 6: Agent edits source files but runs tests without rebuilding.

    For compiled languages (Go, Rust, Java, C/C++, TypeScript with build step),
    editing source then running tests without a build step means tests run
    against stale artifacts.
    """
    compiled_extensions = {".go", ".rs", ".java", ".c", ".cpp", ".h", ".ts", ".tsx"}
    build_commands = [
        "make", "go build", "cargo build", "mvn", "gradle", "tsc",
        "npm run build", "yarn build",
        # These test commands implicitly compile (no stale artifacts possible):
        "go test", "cargo test", "mvn test", "gradle test",
    ]
    test_commands = ["pytest", "npm test", "yarn test"]

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        ext = Path(file_path).suffix if file_path else ""
        if ext in compiled_extensions:
            state["source_changed_since_build"] = True
            return None

    if tool_name == "Bash":
        # Check if this is a build command (resets the flag)
        for bc in build_commands:
            if bc in command:
                state["source_changed_since_build"] = False
                return None

        # Check if this is a test command while source is stale
        if state.get("source_changed_since_build", False):
            for tc in test_commands:
                if tc in command and _should_warn(state, "rebuild_gap"):
                    _record_warning(state, "rebuild_gap")
                    return (
                        "⚠ EXECUTION RISK — source files changed since last build. "
                        "Tests may run against stale compiled artifacts. "
                        "Rebuild before testing."
                    )
    return None


# --- Main entry point ---


def analyze(hook_input: dict) -> list[str]:
    """Run all pattern detectors and return any warnings."""
    path = _state_path()
    lock_path = path.with_suffix(".lock")
    lock_path.touch(exist_ok=True)

    with open(lock_path, "r+b" if sys.platform == "win32" else "r") as lock_fd:
        if sys.platform == "win32":
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
            except Exception:
                pass
        elif HAS_FCNTL:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            state = load_state()

            tool_name = hook_input.get("tool_name", "")
            tool_input = hook_input.get("tool_input", {})
            tool_output = hook_input.get("tool_output", {})

            command = tool_input.get("command", "")
            stderr = tool_output.get("stderr", "")
            stdout = tool_output.get("stdout", "")
            exit_code = tool_output.get("exit_code", 0)

            # Increment command counter for Bash
            if tool_name == "Bash":
                state["total_commands"] = state.get("total_commands", 0) + 1

            # Track command history (keep last 20 for context)
            if tool_name == "Bash" and command:
                history = state.setdefault("command_history", [])
                history.append({
                    "cmd": command[:500],
                    "exit": exit_code,
                    "err": (stderr or "")[:200],
                })
                if len(history) > 20:
                    state["command_history"] = history[-20:]

            warnings = []

            # Run all detectors
            if tool_name == "Bash" and command:
                # Run specific detectors first — they take priority over generic ones
                conn_matched = detect_connection_refused(state, command, exit_code, stderr)
                if conn_matched:
                    warnings.append(conn_matched)
                else:
                    # Only count toward doom_loop if not a connection error
                    w = detect_doom_loop(state, command, exit_code, stderr)
                    if w:
                        warnings.append(w)

                w = detect_timeout_loop(state, command, exit_code, stderr)
                if w:
                    warnings.append(w)

                w = detect_shallow_validation(state, command, exit_code, stderr)
                if w:
                    warnings.append(w)

            # These detectors also track Write/Edit events
            w = detect_proxy_test(state, command, exit_code, tool_name, tool_input)
            if w:
                warnings.append(w)

            w = detect_rebuild_gap(state, command, exit_code, tool_name, tool_input)
            if w:
                warnings.append(w)

            save_state(state)
        finally:
            if sys.platform == "win32":
                try:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            elif HAS_FCNTL:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    return warnings


def cleanup() -> None:
    """Remove state and lock files for current session. Called on session stop."""
    path = _state_path()
    path.unlink(missing_ok=True)
    path.with_suffix(".lock").unlink(missing_ok=True)
    path.with_suffix(".tmp").unlink(missing_ok=True)


def main() -> None:
    # Handle cleanup subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        cleanup()
        sys.exit(0)

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    warnings = analyze(hook_input)

    for w in warnings:
        print(w, file=sys.stderr)

    # Always exit 0 — advisory only, never blocks
    sys.exit(0)


if __name__ == "__main__":
    main()
