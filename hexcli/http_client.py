#!/usr/bin/env python3
"""hexcli.http_client — keep-alive HTTP transport, lifted out of agent.py.

A single keep-alive connection per backend host:port is cached for the
life of the process and reused across every agent-loop step, instead of
opening/closing a fresh TCP connection on every call (the previous
urllib.request.urlopen()-per-call behaviour). The agent loop only ever has
one LLM call in flight at a time, so a single cached connection per host
is safe without locking around request/response pairs. Stays stdlib-only
(http.client), matching the project's no-heavy-deps design.

Split stage 2 (docs/V2X_ROADMAP.md, "The Split"). Function bodies are moved
verbatim; agent.py re-binds every name.
"""
from __future__ import annotations

import http.client
import io
import json
import threading
import time
import urllib.error
import urllib.parse
from typing import Any

_HTTP_CONNECTIONS: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
_HTTP_CONN_LOCK = threading.Lock()


def _connection_key(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


def _get_connection(url: str, timeout_s: float) -> tuple[http.client.HTTPConnection, str]:
    parsed = urllib.parse.urlsplit(url)
    scheme, host, port = _connection_key(url)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    with _HTTP_CONN_LOCK:
        conn = _HTTP_CONNECTIONS.get((scheme, host, port))
        if conn is None:
            conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=timeout_s)
            _HTTP_CONNECTIONS[(scheme, host, port)] = conn
        else:
            conn.timeout = timeout_s
    return conn, path


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float,
) -> http.client.HTTPResponse:
    """POST/GET over a cached keep-alive connection, with one transparent
    reconnect if the server dropped an idle connection (RemoteDisconnected /
    broken pipe) before we noticed.

    Raises urllib.error.URLError / urllib.error.HTTPError on connection
    failure / non-2xx status, matching what urllib.request.urlopen() used to
    raise, so the existing top-level error handling keeps working unchanged.
    """
    # The server holds one inference slot and answers 429 + Retry-After while
    # it is busy — including the few seconds of an end-of-turn prewarm
    # (_prewarm_backend). Wait it out instead of surfacing an error.
    deadline = time.monotonic() + _BUSY_WAIT_MAX_S
    while True:
        resp = _http_request_once(method, url, headers, body, timeout_s)
        if resp.status != 429 or time.monotonic() >= deadline:
            break
        try:
            delay = float(resp.getheader("Retry-After") or 1.0)
        except ValueError:
            delay = 1.0
        resp.read()
        time.sleep(min(max(delay, 0.2), 3.0))
    if resp.status >= 400:
        body_bytes = resp.read()
        raise urllib.error.HTTPError(
            url, resp.status, resp.reason, dict(resp.getheaders()), io.BytesIO(body_bytes)
        )
    return resp


_BUSY_WAIT_MAX_S = 25.0


def _http_request_once(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float,
) -> http.client.HTTPResponse:
    conn, path = _get_connection(url, timeout_s)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
    except (
        http.client.RemoteDisconnected, http.client.ImproperConnectionState,
        http.client.BadStatusLine,
        BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
    ):
        # ImproperConnectionState covers CannotSendRequest AND ResponseNotReady:
        # a request that died mid-cycle (server killed for a restart) leaves the
        # cached connection stuck in Request-sent, and without the reconnect the
        # first call after a successful restart failed with ResponseNotReady.
        conn.close()
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            raise urllib.error.URLError(exc) from exc
    except OSError as exc:
        # Close before raising, or the poisoned connection stays cached and
        # every later call inherits its half-sent state.
        conn.close()
        raise urllib.error.URLError(exc) from exc
    return resp


def http_json_request(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout_s: int
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers)
    resp = _http_request("POST", url, req_headers, body, timeout_s)
    data = resp.read()
    return json.loads(data.decode("utf-8"))


def http_json_get(url: str, timeout_s: int = 10) -> Any:
    resp = _http_request("GET", url, {}, None, timeout_s)
    data = resp.read()
    return json.loads(data.decode("utf-8"))


def ping_backend(config: dict[str, Any]) -> bool:
    """Return True if the configured backend responds to a quick health probe."""
    try:
        if config["backend"] == "ollama":
            host = config["ollama"]["host"].rstrip("/")
            http_json_get(f"{host}/api/tags", timeout_s=3)
        elif config["backend"] == "openai":
            base_url = config["openai_compatible"]["base_url"].rstrip("/")
            _http_request("GET", f"{base_url}/models", {}, None, 3.0)
        return True
    except Exception:
        return False
