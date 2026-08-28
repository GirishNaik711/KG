"""Functional coverage for the bounded loopback-only HTTP probe."""

from __future__ import annotations

import json
import shlex
import socket
import sys
import threading
import time
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aah.core.build.runtime_validation import (
    HttpProbePolicy,
    _http_probe,
    check_health,
    run_dockerless_validation,
)
from aah.core.common.git_utils import AAH_STATE_PATHS, porcelain_dirt
from aah.core.common.io_utils import write_yaml
from tests.support.aah_project import AAHProjectBuilder


class _ProbeServer:
    def __init__(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:  # noqa: N802
                owner.hits.append(self.path)
                owner.request_headers.append(dict(self.headers.items()))
                if self.path == "/not-found":
                    self._reply(404, b'{"message":"missing"}', "application/json")
                elif self.path == "/health":
                    self._reply(503, b"starting", "text/plain")
                elif self.path == "/large":
                    self._reply(200, b"x" * 4096, "text/plain")
                elif self.path == "/deep":
                    self._reply(200, b'{"a":{"b":{"c":{"d":1}}}}', "application/json")
                elif self.path == "/many-headers":
                    self.send_response(200)
                    for index in range(20):
                        self.send_header(f"X-Fill-{index}", "v" * 40)
                    self.end_headers()
                    self.wfile.write(b"ok")
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                elif self.path == "/slow":
                    time.sleep(0.25)
                    self._reply(200, b"late", "text/plain")
                elif self.path == "/sensitive":
                    payload = json.dumps({
                        "token": "body-token",
                        "nested": {"password": "body-password"},
                        "note": "Authorization: Bearer inline-secret",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Set-Cookie", "sid=header-cookie")
                    self.send_header("X-Api-Key", "header-key")
                    self.end_headers()
                    self.wfile.write(payload)
                elif self.path == "/malformed":
                    self._reply(
                        200,
                        b'{"access_token":"secret", broken',
                        "application/json",
                    )
                else:
                    self._reply(200, b'{"ok":true}', "application/json")

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args) -> None:
                pass

        self.hits: list[str] = []
        self.request_headers: list[dict[str, str]] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_ProbeServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def test_http_error_is_alive_and_health_remains_compatible(tmp_path) -> None:
    with _ProbeServer() as server:
        result = _http_probe(f"{server.origin}/not-found")
        health = check_health(tmp_path, wave=0, port=server.port)

    assert result["alive"] is True
    assert result["status"] == 404
    assert result["error"] is None
    assert result["json_parse_status"] == "parsed"
    assert result["json_value"] == {"message": "missing"}
    assert health["passed"] is True
    assert health["details"]["live_probe"]["status"] == 503


def test_refused_connection_is_not_alive() -> None:
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.bind(("127.0.0.1", 0))
    port = reserved.getsockname()[1]
    reserved.close()

    result = _http_probe(f"http://127.0.0.1:{port}/", timeout=1)
    assert result["alive"] is False
    assert result["status"] is None
    assert result["error"] == "connection_error"


def test_probe_bounds_and_deadline() -> None:
    policy = HttpProbePolicy(
        max_response_bytes=64,
        max_header_bytes=96,
        max_json_depth=3,
        max_timeout_seconds=0.1,
    )
    with _ProbeServer() as server:
        large = _http_probe(f"{server.origin}/large", policy=policy)
        deep = _http_probe(f"{server.origin}/deep", policy=policy)
        headers = _http_probe(f"{server.origin}/many-headers", policy=policy)
        slow = _http_probe(f"{server.origin}/slow", timeout=5, policy=policy)

    assert large["body_bytes"] == 64
    assert large["body_truncated"] is True
    assert large["json_parse_status"] == "truncated"
    assert deep["json_parse_status"] == "too_deep"
    assert headers["headers_truncated"] is True
    assert slow["error"] == "timeout"


def test_redirect_is_reported_but_never_followed() -> None:
    with _ProbeServer() as server:
        result = _http_probe(f"{server.origin}/redirect")

    assert result["alive"] is True
    assert result["status"] == 302
    assert result["redirect_count"] == 0
    assert server.hits == ["/redirect"]


def test_request_headers_reach_the_local_app() -> None:
    with _ProbeServer() as server:
        result = _http_probe(
            f"{server.origin}/ok",
            headers={"X-Smoke-Contract": "orders"},
        )

    assert result["status"] == 200
    assert server.request_headers[0]["X-Smoke-Contract"] == "orders"


def test_sensitive_output_is_redacted_and_malformed_json_is_bounded() -> None:
    with _ProbeServer() as server:
        sensitive = _http_probe(f"{server.origin}/sensitive")
        malformed = _http_probe(f"{server.origin}/malformed")

    serialized = json.dumps(sensitive, sort_keys=True)
    for secret in ("body-token", "body-password", "inline-secret", "header-cookie", "header-key"):
        assert secret not in serialized
    assert sensitive["json_value"]["token"] == "[REDACTED]"
    assert sensitive["headers"]["set-cookie"] == "[REDACTED]"
    assert malformed["json_parse_status"] == "malformed"
    assert "secret" not in malformed["body_snippet"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/path",
        "http://169.254.169.254/latest/meta-data/",
        "http://user:secret@127.0.0.1:80/",
        "https://localhost:443/",
        "http://127.0.0.1:80/path#fragment",
    ],
)
def test_non_loopback_or_ambiguous_targets_are_rejected(url: str) -> None:
    result = _http_probe(url)
    assert result["alive"] is False
    assert result["error"] == "invalid_target"


def test_localhost_name_is_supported_without_remote_allowlists() -> None:
    with _ProbeServer() as server:
        result = _http_probe(f"http://localhost:{server.port}/ok")
    assert result["status"] == 200


def test_policy_and_timeout_validation() -> None:
    policy = HttpProbePolicy()
    with pytest.raises(FrozenInstanceError):
        policy.max_response_bytes = 1  # type: ignore[misc]
    with pytest.raises(ValueError):
        HttpProbePolicy(max_response_bytes=0)
    with pytest.raises(ValueError):
        HttpProbePolicy(max_timeout_seconds=float("nan"))
    assert _http_probe("http://127.0.0.1:1/", timeout=True)["error"] == "invalid_timeout"


def _reserve_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
