#!/usr/bin/env python3
"""Runtime validation utilities for local application verification."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aah.core.common.io_utils import read_json, read_yaml, write_json
from aah.core.common.readiness import resolve_runtime_port

# ---------------------------------------------------------------------------
# HTTP Probe Utility
# ---------------------------------------------------------------------------


_SENSITIVE_NAME_PARTS = (
    "authorization", "cookie", "credential", "password", "secret", "session",
    "token", "api-key", "api_key", "apikey", "access-key", "access_key",
    "private-key", "private_key",
)
_AUTH_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
)
_INLINE_SECRET_RE = re.compile(
    r"(?i)([\"']?(?:token|password|secret|api[_-]?key|access[_-]?token)"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class HttpProbePolicy:
    """Bounds for the supported loopback-only runtime probe."""

    max_response_bytes: int = 16_384
    max_header_bytes: int = 8_192
    max_json_depth: int = 20
    max_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        integer_limits = {
            "max_response_bytes": (1, 1_048_576),
            "max_header_bytes": (1, 65_536),
            "max_json_depth": (1, 100),
        }
        for name, (minimum, maximum) in integer_limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        timeout = self.max_timeout_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise ValueError("max_timeout_seconds must be a finite number in (0, 60]")


_LOCALHOST_PROBE_POLICY = HttpProbePolicy()


def _is_sensitive_name(name: object) -> bool:
    lowered = str(name).lower()
    return any(part in lowered for part in _SENSITIVE_NAME_PARTS)


def _redact_text(value: str) -> str:
    value = _AUTH_RE.sub(lambda m: f"{m.group(1)}: [REDACTED]", value)
    return _INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", value)


def _sanitize_json(value: Any, depth: int = 0) -> Any:
    if depth > 100:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_name(key) else _sanitize_json(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_json_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_json_depth(v) for v in value), default=0)
    return 0


def _failure_result(error: str, *, started: float, alive: bool = False, status: int | None = None) -> dict:
    return {
        "alive": alive,
        "status": status,
        "response_time_ms": int((time.monotonic() - started) * 1000),
        "body_snippet": "",
        "headers": {},
        "headers_truncated": False,
        "body_bytes": 0,
        "body_truncated": False,
        "json_parse_status": "unavailable",
        "json_value": None,
        "error": error,
        "redirect_count": 0,
    }


def _validate_loopback_url(url: str) -> tuple[str, int, str]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or 80
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid_target") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not 1 <= port <= 65_535
    ):
        raise ValueError("invalid_target")
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return parsed.hostname, port, target


def _bounded_headers(response: http.client.HTTPResponse, limit: int) -> tuple[dict[str, str], bool]:
    result: dict[str, str] = {}
    used = 0
    truncated = False
    for name, value in response.getheaders():
        size = len(str(name).encode("utf-8", "replace")) + len(str(value).encode("utf-8", "replace"))
        if used + size > limit:
            truncated = True
            break
        used += size
        key = str(name).lower()
        safe_value = "[REDACTED]" if _is_sensitive_name(name) else _redact_text(str(value))
        result[key] = safe_value if key not in result else f"{result[key]}, {safe_value}"
    return result, truncated


def _http_probe(
    url: str,
    method: str = "GET",
    body: str | None = None,
    headers: dict | None = None,
    timeout: int | float = 10,
    *,
    policy: HttpProbePolicy | None = None,
) -> dict:
    """Make one bounded request to a literal loopback target without redirects."""
    started = time.monotonic()
    effective_policy = policy if policy is not None else _LOCALHOST_PROBE_POLICY
    if not isinstance(effective_policy, HttpProbePolicy):
        return _failure_result("invalid_policy", started=started)
    try:
        host, port, target = _validate_loopback_url(url)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid_timeout")
        effective_timeout = min(float(timeout), float(effective_policy.max_timeout_seconds))
        method = str(method).upper()
        if not method or any(ch.isspace() for ch in method):
            raise ValueError("invalid_request")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError("invalid_request")
        request_headers = {
            str(k): str(v)
            for k, v in (headers or {}).items()
            if isinstance(k, str) and isinstance(v, (str, int, float))
        }
        encoded_body = body.encode("utf-8") if isinstance(body, str) else None
        if body is not None and encoded_body is None:
            raise ValueError("invalid_request")
        if encoded_body is not None and len(encoded_body) > effective_policy.max_response_bytes:
            raise ValueError("invalid_request")
    except ValueError as exc:
        code = str(exc) if str(exc) in {"invalid_target", "invalid_timeout", "invalid_request"} else "invalid_request"
        return _failure_result(code, started=started)

    deadline = started + effective_timeout
    connection = http.client.HTTPConnection(host, port, timeout=effective_timeout)
    try:
        connection.request(method, target, body=encoded_body, headers=request_headers)
        response = connection.getresponse()
        if connection.sock is not None:
            connection.sock.settimeout(max(0.001, deadline - time.monotonic()))

        response_headers, headers_truncated = _bounded_headers(
            response, effective_policy.max_header_bytes
        )
        chunks: list[bytes] = []
        remaining = effective_policy.max_response_bytes + 1
        while remaining > 0:
            seconds_left = deadline - time.monotonic()
            if seconds_left <= 0:
                raise socket.timeout
            if connection.sock is not None:
                connection.sock.settimeout(max(0.001, seconds_left))
            chunk = response.read(min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)

        raw = b"".join(chunks)
        body_truncated = len(raw) > effective_policy.max_response_bytes
        raw = raw[: effective_policy.max_response_bytes]
        text_body = raw.decode("utf-8", errors="replace")
        safe_text = _redact_text(text_body)
        snippet_truncated = len(safe_text) > 2_000
        body_snippet = safe_text[:2_000]

        json_status = "not_json"
        json_value = None
        if body_truncated:
            json_status = "truncated"
        elif raw:
            try:
                parsed_json = json.loads(text_body)
                if _json_depth(parsed_json) > effective_policy.max_json_depth:
                    json_status = "too_deep"
                else:
                    json_status = "parsed"
                    json_value = _sanitize_json(parsed_json)
                    body_snippet = json.dumps(
                        json_value, sort_keys=True, separators=(",", ":")
                    )[:2_000]
            except json.JSONDecodeError:
                content_type = response_headers.get("content-type", "")
                json_status = "malformed" if "json" in content_type.lower() else "not_json"

        return {
            "alive": True,
            "status": response.status,
            "response_time_ms": int((time.monotonic() - started) * 1000),
            "body_snippet": body_snippet,
            "headers": response_headers,
            "headers_truncated": headers_truncated,
            "body_bytes": len(raw),
            "body_truncated": body_truncated or snippet_truncated,
            "json_parse_status": json_status,
            "json_value": json_value,
            "error": None,
            "redirect_count": 0,
        }
    except (TimeoutError, socket.timeout):
        return _failure_result("timeout", started=started)
    except (ConnectionError, OSError, http.client.HTTPException):
        return _failure_result("connection_error", started=started)
    except (TypeError, ValueError, UnicodeError):
        return _failure_result("invalid_request", started=started)
    finally:
        connection.close()

# ---------------------------------------------------------------------------
# Smoke Tests — Hit Live localhost Endpoints
# ---------------------------------------------------------------------------


def _load_smoke_wave(project_path: Path, wave: int) -> dict | None:
    """Read the full generated per-wave smoke YAML definition.

    Returns the whole parsed mapping (carrying ``schema_version`` and every
    step's ``assertions``) or None when absent, unparseable, or not schema v2.
    """
    smoke_path = project_path / ".aah" / "plan" / "smoke-tests" / f"wave-{wave}.yaml"
    if not smoke_path.exists():
        return None
    try:
        smoke_def = read_yaml(smoke_path) or {}
    except Exception:
        return None
    if not isinstance(smoke_def, dict):
        return None
    from aah.core.common.validators import validate_smoke_wave

    if validate_smoke_wave(smoke_def):
        return None
    return smoke_def


def _load_smoke_steps(project_path: Path, wave: int) -> list[dict]:
    """Read the generated per-wave smoke YAML's HTTP-probeable steps.

    Only steps carrying ``request.path`` are HTTP-probeable (the generator's
    API endpoint steps); functional / integration / acceptance-criteria steps
    have no request and are covered by the test suite, not the smoke probe.

    The returned step dicts carry their full ``assertions`` list and derive the
    probe's accepted status codes from ``status_in``. This filter only excludes
    non-HTTP steps.
    Canonical nested ``request`` fields are flattened into a private runtime
    representation for the probe loop. Historical flat compatibility aliases
    may coexist in the YAML, but never override the nested request/assertions.

    Lives here rather than in verify.py: verify.py is now a read-only evidence
    verifier and never executes a smoke step, while this module does.
    """
    smoke_def = _load_smoke_wave(project_path, wave)
    if smoke_def is None:
        return []
    steps = smoke_def.get("steps", [])
    if not isinstance(steps, list):
        return []
    normalized: list[dict] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        step = dict(raw)
        request = step.get("request")
        if not isinstance(request, dict):
            continue
        for field in (
            "method", "path", "headers", "body",
            "expected_status", "expected_body_contains",
        ):
            step.pop(field, None)
        for field in ("method", "path", "headers", "body"):
            if request.get(field) is not None:
                step[field] = request[field]
        status_assertion = next(
            (
                assertion for assertion in step.get("assertions", [])
                if isinstance(assertion, dict) and assertion.get("type") == "status_in"
            ),
            None,
        )
        if status_assertion is not None:
            step["expected_status"] = status_assertion["values"]
        if step.get("path"):
            normalized.append(step)
    return normalized


def run_smoke_tests(
    project_path: Path,
    wave: int,
    port: int = 8000,
    timeout: int = 120,
) -> dict:
    """Execute smoke tests against the live running app on localhost.

    Reads step definitions from .aah/plan/smoke-tests/wave-N.yaml and
    executes each step as an HTTP request to localhost:<port>.
    Falls back to basic endpoint probing if no definition exists.

    The app must already be running on the specified port (started by the
    aah-runtime-validator agent via Docker or subprocess).
    """
    aah_path = project_path / ".aah"
    result = {
        "check": "smoke_tests",
        "wave": wave,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "details": {},
    }

    base_url = f"http://localhost:{port}"
    result["details"]["base_url"] = base_url

    # Load smoke test definition
    smoke_path = aah_path / "plan" / "smoke-tests" / f"wave-{wave}.yaml"
    if smoke_path.exists():
        # A definition that EXISTS but will not validate must fail, never pass:
        # an invalid definition is not equivalent to having no HTTP work.
        smoke_def = _load_smoke_wave(project_path, wave)
        if smoke_def is None:
            result["details"]["message"] = (
                f"Smoke definition {smoke_path.name} is unparseable or fails "
                "schema-v2 validation — cannot prove the app runs."
            )
            return result
        # _load_smoke_steps flattens nested `request` fields and drops non-HTTP
        # steps, so the probe loop below sees a uniform flat shape.
        steps = _load_smoke_steps(project_path, wave)
    else:
        # Fallback: basic endpoint probing
        steps = [
            {"name": "Root endpoint responds", "method": "GET", "path": "/", "expected_status": [200, 404, 307]},
            {"name": "Health endpoint responds", "method": "GET", "path": "/health", "expected_status": [200, 404]},
        ]
        result["details"]["using_fallback"] = True

    if not steps:
        result["details"]["message"] = "Smoke test definition has no steps"
        result["passed"] = True
        return result

    # Execute each step against localhost
    step_results = []
    total_passed = 0
    total_failed = 0

    for step in steps:
        request = step.get("request") if isinstance(step.get("request"), dict) else {}
        method = str(request.get("method") or step.get("method", "GET")).upper()
        path = request.get("path") or step.get("path", "/")
        step_name = step.get("name", f"{method} {path}")
        body = request.get("body") if "body" in request else step.get("body")
        headers = request.get("headers") if "headers" in request else step.get("headers")
        expected_status = step.get("expected_status", 200)

        # Normalize expected_status to a list
        if isinstance(expected_status, int):
            expected_status = [expected_status]

        url = f"{base_url}{path}"
        probe_result = _http_probe(url, method=method, body=body, headers=headers, timeout=min(30, timeout))

        step_passed = (
            probe_result["alive"]
            and probe_result["error"] is None
            and probe_result["status"] in expected_status
        )

        step_record = {
            "name": step_name,
            "method": method,
            "path": path,
            "expected_status": expected_status,
            "alive": probe_result["alive"],
            "actual_status": probe_result["status"],
            "response_time_ms": probe_result["response_time_ms"],
            "passed": step_passed,
            "body_snippet": probe_result.get("body_snippet", "")[:200],
            "body_truncated": probe_result["body_truncated"],
            "headers": probe_result["headers"],
            "headers_truncated": probe_result["headers_truncated"],
            "json_parse_status": probe_result["json_parse_status"],
            "json_value": probe_result["json_value"],
            "error": probe_result.get("error"),
        }
        step_results.append(step_record)

        if step_passed:
            total_passed += 1
        else:
            total_failed += 1

    result["details"]["step_results"] = step_results
    result["details"]["steps_total"] = len(steps)
    result["details"]["steps_passed"] = total_passed
    result["details"]["steps_failed"] = total_failed

    # For fallback probing: pass if at least one endpoint responds (even 404 means server is up)
    if result["details"].get("using_fallback"):
        # Any response at all means the app is serving
        any_response = any(s["alive"] for s in step_results)
        result["passed"] = any_response
        if any_response:
            result["details"]["message"] = f"App responding on localhost:{port} ({total_passed}/{len(steps)} probes OK)"
        else:
            result["details"]["message"] = f"App not responding on localhost:{port}"
    else:
        # Defined steps: all must pass
        result["passed"] = total_failed == 0
        if total_failed == 0:
            result["details"]["message"] = f"All {total_passed} smoke test steps passed"
        else:
            failed_names = [s["name"] for s in step_results if not s["passed"]]
            result["details"]["message"] = f"{total_failed}/{len(steps)} smoke steps failed: {', '.join(failed_names[:3])}"

    return result


# ---------------------------------------------------------------------------
# Health Check — Probe Live App + File Audit
# ---------------------------------------------------------------------------


def check_health(
    project_path: Path,
    wave: int,
    port: int = 8000,
) -> dict:
    """Check system health: live app probe + artifact audit.

    Live checks:
    - HTTP GET localhost:<port>/health (or /) — verify app responds
    - Response time within threshold

    File-based audit:
    - Check for failed test features
    - Check regression results
    - Check quality violations

    The app must already be running on the specified port.
    """
    aah_path = project_path / ".aah"
    result = {
        "check": "health_check",
        "wave": wave,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": True,
        "details": {},
    }

    issues = []

    # ── Live Checks ──────────────────────────────────────────────────

    # HTTP health probe
    health_endpoints = ["/health", "/healthz", "/api/health", "/"]
    app_responding = False
    health_response = None

    for endpoint in health_endpoints:
        probe = _http_probe(f"http://localhost:{port}{endpoint}", timeout=5)
        if probe["alive"]:
            app_responding = True
            health_response = {
                "endpoint": endpoint,
                "status": probe["status"],
                "response_time_ms": probe["response_time_ms"],
                "body_snippet": probe["body_snippet"][:200],
            }
            break

    if app_responding:
        result["details"]["live_probe"] = health_response
        # Check response time threshold (5s)
        if health_response["response_time_ms"] > 5000:
            issues.append({
                "type": "slow_response",
                "severity": "high",
                "details": f"Health endpoint response time {health_response['response_time_ms']}ms exceeds 5000ms threshold",
            })
    else:
        issues.append({
            "type": "app_not_responding",
            "severity": "critical",
            "details": f"App not responding on localhost:{port} (tried {health_endpoints})",
        })

    # ── File-Based Audit ─────────────────────────────────────────────

    # Check for error patterns in recent test outputs
    test_results_dir = aah_path / "build" / "test-results"
    if test_results_dir.is_dir():
        failed_features = []
        for f in test_results_dir.glob("*.json"):
            if f.name in ("regression-latest.json", "test-execution-log.jsonl"):
                continue
            try:
                data = read_json(f)
                if isinstance(data, dict) and data.get("passed") is False:
                    failed_features.append(data.get("feature_id", f.stem))
            except Exception:
                continue

        if failed_features:
            issues.append({
                "type": "failed_tests",
                "severity": "high",
                "details": f"Features with failing tests: {', '.join(failed_features)}",
            })

    # Regression is deliberately NOT read here. It is its own last-wave step and
    # is gated independently (verify's regression check and the merge script both
    # guard on is_last_wave), so the system checkpoint owns startup/module/endpoint
    # signal only — see the aah-build skill's system-checkpoint step. Reading it
    # here also meant a `passed: false` no-verdict artifact (which measures
    # nothing) surfaced as a CRITICAL runtime issue on a later checkpoint wave.

    # Check quality results if they exist
    quality_dir = aah_path / "build" / "quality-results"
    if quality_dir.is_dir():
        critical_violations = 0
        for f in quality_dir.glob("*-static-analysis.json"):
            try:
                data = read_json(f)
                if isinstance(data, dict):
                    for v in data.get("violations", []):
                        if v.get("severity") == "critical":
                            critical_violations += 1
            except Exception:
                continue

        if critical_violations > 0:
            issues.append({
                "type": "critical_violations",
                "severity": "critical",
                "details": f"{critical_violations} critical code quality violations found",
            })

    # ── Verdict ──────────────────────────────────────────────────────

    result["details"]["issues"] = issues
    result["details"]["issue_count"] = len(issues)

    critical_issues = [i for i in issues if i.get("severity") == "critical"]
    if critical_issues:
        result["passed"] = False
        result["details"]["message"] = f"{len(critical_issues)} critical health issues detected"
    else:
        result["details"]["message"] = "System health OK" if not issues else f"{len(issues)} non-critical issues found"

    return result


# ---------------------------------------------------------------------------
# Deterministic Dockerless Validation
# ---------------------------------------------------------------------------


def _runtime_check(wave: int, name: str, passed: bool, message: str, **details) -> dict:
    return {
        "check": name,
        "wave": wave,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "details": {"message": message, **details},
    }


def _unreached_runtime_checks(
    checks: dict[str, dict], wave: int, failed_step: str
) -> None:
    for name in ("module_validation", "startup_validation", "smoke_tests", "health_check"):
        checks.setdefault(
            name,
            _runtime_check(
                wave,
                name,
                False,
                f"not reached — {failed_step} failed",
            ),
        )


def _startup_argv(project_path: Path, port: int) -> list[str] | None:
    from aah.core.build.lang_checks import detect

    command = detect(project_path).start_command(port)
    if command is None:
        return None
    argv = list(command.argv)

    for index, value in enumerate(argv[:-1]):
        if value == "--port":
            argv[index + 1] = str(port)
    argv = [
        re.sub(r"(?<=:)(\d+)$", str(port), value)
        if "0.0.0.0:" in value or "localhost:" in value else value
        for value in argv
    ]
    return argv


@contextlib.contextmanager
def _resilient_temp_dir(prefix: str):
    """Like tempfile.TemporaryDirectory, but tolerant of Windows delete races.

    On Windows the OS locks open files, so a lingering handle inside the temp
    dir makes shutil.rmtree fail with WinError 145 (ERROR_DIR_NOT_EMPTY). We
    retry the cleanup a few times to let handles drain, then swallow the final
    error so a cleanup hiccup never crashes the validation run. Python 3.12's
    TemporaryDirectory(ignore_cleanup_errors=True) would cover this, but the
    project targets 3.11.
    """
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                shutil.rmtree(path)
                return
            except FileNotFoundError:
                return
            except OSError as exc:  # PermissionError / WinError 145 / etc.
                last_error = exc
                time.sleep(0.2 * (attempt + 1))
        # Best-effort final pass; ignore any files still locked by an orphan.
        shutil.rmtree(path, ignore_errors=True)


def _stop_runtime_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name != "posix":
        # On Windows, process.terminate()/kill() only signals the top-level
        # process, leaving grandchildren (reloaders, npm->node, workers) alive.
        # Those orphans keep the inherited startup.stdout/.stderr handles open,
        # which makes the temp-dir cleanup fail with WinError 145. Kill the whole
        # tree with taskkill /T so every inherited handle is released.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _runtime_feature_ids(project_path: Path, wave: int) -> list[str]:
    """Feature IDs whose declared env the app needs: waves.json through
    ``wave``, else every feature (the lean build has one ``.env`` for all)."""
    from aah.core.common.feature_utils import flatten_wave_features

    try:
        data = read_json(project_path / ".aah" / "plan" / "waves.json")
        waves = data.get("waves") if isinstance(data, dict) else None
    except (OSError, TypeError, ValueError):
        waves = None
    if isinstance(waves, list):
        return [
            feature_id
            for entry in waves[: wave + 1]
            for feature_id in flatten_wave_features(entry)
        ]

    try:
        features = read_json(project_path / ".aah" / "feature-list.json")
    except (OSError, TypeError, ValueError):
        return []
    if not isinstance(features, dict):
        return []
    return [
        f["id"] for f in features.get("features", [])
        if isinstance(f, dict) and f.get("id")
    ]


def run_dockerless_validation(
    project_path: Path,
    wave: int,
    *,
    port: int,
    readiness_timeout: int = 30,
) -> tuple[dict[str, dict], str]:
    """Boot and probe an app without Docker, always returning four checks."""
    from aah.core.build.evidence import EvidenceError
    from aah.core.build.lang_checks import detect
    from aah.core.build.run_feature_tests import (
        _redirected_test_environment,
        _sanitize_test_output,
        resolve_feature_set_required_env,
    )
    from aah.core.common.execution import CommandSpec, run_bounded_command

    checks: dict[str, dict] = {}
    fix_category = "build"
    feature_ids = _runtime_feature_ids(project_path, wave)
    try:
        required_env, missing_env = resolve_feature_set_required_env(
            project_path, feature_ids
        )
    except EvidenceError as exc:
        checks["module_validation"] = _runtime_check(
            wave, "module_validation", False, f"Invalid required_env declaration: {exc}"
        )
        _unreached_runtime_checks(checks, wave, "required environment preflight")
        return checks, "build"
    if missing_env:
        checks["module_validation"] = _runtime_check(
            wave,
            "module_validation",
            False,
            "Missing required environment keys; add them to the project root .env",
            missing_required_env=missing_env,
        )
        _unreached_runtime_checks(checks, wave, "required environment preflight")
        return checks, "user_required"

    with _resilient_temp_dir(prefix=f"aah-runtime-wave-{wave}-") as temp_dir:
        namespace = Path(temp_dir)
        runtime_env = _redirected_test_environment(
            namespace, required_env, project_path=project_path
        )
        # Inject the cross-cutting cloud auth context (AWS_PROFILE / region) that
        # /aah-access captured in cloud-readiness.yaml. No single feature declares
        # it, but boto3 (and the app under test) need it to reach the validated
        # cloud services. setdefault: an ambient value or a feature-declared one
        # already in runtime_env always wins.
        from aah.core.common.readiness import render_cloud_auth_env

        for _key, _value in render_cloud_auth_env(project_path / ".aah").items():
            runtime_env.setdefault(_key, _value)
        runtime_env["PORT"] = str(port)
        adapter = detect(project_path)
        module_command = adapter.module_validation_command()
        if module_command is None:
            checks["module_validation"] = _runtime_check(
                wave, "module_validation", True, "not applicable — adapter has no module check"
            )
        else:
            outcome = run_bounded_command(CommandSpec(
                module_command.argv,
                cwd=module_command.cwd or project_path,
                env=runtime_env,
                timeout_sec=module_command.timeout_sec,
            ))
            safe_stdout = _sanitize_test_output(str(outcome.stdout), required_env)
            safe_stderr = _sanitize_test_output(str(outcome.stderr), required_env)
            passed = outcome.ok and not outcome.stdout_truncated and not outcome.stderr_truncated
            checks["module_validation"] = _runtime_check(
                wave,
                "module_validation",
                passed,
                "module validation passed" if passed else "module validation failed",
                command=module_command.argv,
                exit_code=outcome.returncode,
                stdout_tail=safe_stdout[-2000:],
                stderr_tail=safe_stderr[-1000:],
            )
            if not passed:
                _unreached_runtime_checks(checks, wave, "module validation")
                return checks, fix_category

        startup_argv = _startup_argv(project_path, port)
        if not startup_argv:
            checks["startup_validation"] = _runtime_check(
                wave, "startup_validation", False, "No runnable startup command could be resolved"
            )
            _unreached_runtime_checks(checks, wave, "startup validation")
            return checks, fix_category

        stdout_path = namespace / "startup.stdout"
        stderr_path = namespace / "startup.stderr"
        process: subprocess.Popen | None = None
        try:
            # On Windows, CREATE_NEW_PROCESS_GROUP lets taskkill /T reach the
            # whole tree; start_new_session is the POSIX equivalent (killpg).
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    startup_argv,
                    cwd=project_path,
                    env=runtime_env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    creationflags=creationflags,
                )
                deadline = time.monotonic() + readiness_timeout
                ready_probe = None
                while time.monotonic() < deadline and process.poll() is None:
                    for endpoint in ("/health", "/healthz", "/api/health", "/"):
                        probe = _http_probe(
                            f"http://localhost:{port}{endpoint}", timeout=1
                        )
                        if probe["alive"]:
                            ready_probe = {"endpoint": endpoint, **probe}
                            break
                    if ready_probe:
                        break
                    time.sleep(0.25)

            if ready_probe is None:
                stdout = _sanitize_test_output(
                    stdout_path.read_text(encoding="utf-8", errors="replace"), required_env
                )
                stderr = _sanitize_test_output(
                    stderr_path.read_text(encoding="utf-8", errors="replace"), required_env
                )
                checks["startup_validation"] = _runtime_check(
                    wave,
                    "startup_validation",
                    False,
                    "Application exited or did not become ready before the timeout",
                    command=startup_argv,
                    exit_code=process.poll(),
                    stdout_tail=stdout[-2000:],
                    stderr_tail=stderr[-1000:],
                    port=port,
                    runtime_mode="dockerless",
                )
                _unreached_runtime_checks(checks, wave, "startup validation")
                return checks, fix_category

            checks["startup_validation"] = _runtime_check(
                wave,
                "startup_validation",
                True,
                "Application became ready on localhost",
                command=startup_argv,
                port=port,
                runtime_mode="dockerless",
                endpoint=ready_probe["endpoint"],
                status=ready_probe["status"],
            )
            checks["smoke_tests"] = run_smoke_tests(project_path, wave, port=port)
            checks["health_check"] = check_health(project_path, wave, port=port)
            return checks, fix_category
        except (OSError, ValueError) as exc:
            checks["startup_validation"] = _runtime_check(
                wave, "startup_validation", False, f"Could not start application: {exc}"
            )
            _unreached_runtime_checks(checks, wave, "startup validation")
            return checks, fix_category
        finally:
            if process is not None:
                _stop_runtime_process(process)


def _persist_dockerless_results(
    project_path: Path,
    wave: int,
    port: int,
    checks: dict[str, dict],
    fix_category: str,
    duration_ms: int,
):
    from aah.core.common.execution import CommandSpec, run_bounded_command

    argv = [
        sys.executable,
        "-m",
        "aah.core.build.write_runtime_results",
        "--project-path",
        str(project_path),
        "--wave",
        str(wave),
        "--results-json",
        json.dumps(checks, separators=(",", ":")),
        "--runtime-mode",
        "dockerless",
        "--port",
        str(port),
        "--duration-ms",
        str(duration_ms),
    ]
    if not all(check["passed"] for check in checks.values()):
        argv += ["--fix-category", fix_category]
    return run_bounded_command(CommandSpec(argv, cwd=project_path, timeout_sec=30))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_default_port(project_path: Path) -> int:
    """Compatibility wrapper around the canonical runtime-port resolver."""
    return resolve_runtime_port(project_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="AAH Runtime Validation Utilities")
    sub = parser.add_subparsers(dest="command", required=True)

    smoke_p = sub.add_parser("smoke", help="Run smoke tests against live app on --port")
    smoke_p.add_argument("--project-path", type=Path, default=None)
    smoke_p.add_argument("--wave", type=int, required=True)
    smoke_p.add_argument("--port", type=int, default=None, help="Port where app is already running")
    smoke_p.add_argument("--timeout", type=int, default=120)

    health_p = sub.add_parser("health", help="Run health checks against live app on --port")
    health_p.add_argument("--project-path", type=Path, default=None)
    health_p.add_argument("--wave", type=int, required=True)
    health_p.add_argument("--port", type=int, default=None, help="Port where app is already running")

    dockerless_p = sub.add_parser(
        "dockerless", help="Start, probe, persist, and stop an app without Docker"
    )
    dockerless_p.add_argument("--project-path", type=Path, default=None)
    dockerless_p.add_argument("--wave", type=int, required=True)
    dockerless_p.add_argument("--port", type=int, default=None)
    dockerless_p.add_argument("--readiness-timeout", type=int, default=30)

    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(
        getattr(args, "project_path", None)
    )
    aah_path = project_path / ".aah"

    # Resolve port
    port = args.port if args.port else _resolve_default_port(project_path)

    # Ensure output directory exists
    output_dir = aah_path / "build" / "runtime-results"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "smoke":
        result = run_smoke_tests(project_path, args.wave, port=port, timeout=args.timeout)
        write_json(result, output_dir / f"wave-{args.wave}-smoke.json")
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["passed"] else 2)

    elif args.command == "health":
        result = check_health(project_path, args.wave, port=port)
        write_json(result, output_dir / f"wave-{args.wave}-health.json")
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["passed"] else 2)

    elif args.command == "dockerless":
        started = time.monotonic()
        try:
            checks, fix_category = run_dockerless_validation(
                project_path,
                args.wave,
                port=port,
                readiness_timeout=max(1, args.readiness_timeout),
            )
        except Exception as exc:
            checks = {
                "module_validation": _runtime_check(
                    args.wave,
                    "module_validation",
                    False,
                    f"Dockerless validation could not start: {type(exc).__name__}: {exc}",
                )
            }
            _unreached_runtime_checks(checks, args.wave, "dockerless validation")
            fix_category = "build"
        outcome = _persist_dockerless_results(
            project_path,
            args.wave,
            port,
            checks,
            fix_category,
            int((time.monotonic() - started) * 1000),
        )
        if outcome.stdout:
            print(outcome.stdout, end="" if str(outcome.stdout).endswith("\n") else "\n")
        if outcome.stderr:
            print(outcome.stderr, file=sys.stderr, end="" if str(outcome.stderr).endswith("\n") else "\n")
        sys.exit(0 if outcome.ok else 1)


if __name__ == "__main__":
    main()
