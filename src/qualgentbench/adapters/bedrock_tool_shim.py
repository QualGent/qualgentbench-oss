"""A local relay between codex-cli and Bedrock's OpenAI-compatible runtime that puts
back the MCP tool namespace a non-OpenAI model leaves off (QUA-3535).

codex (0.156 to at least 0.170) advertises every MCP server's tools to the Responses
API as one ``{"type": "namespace", "name": "mcp__<server>", "tools": [...]}`` entry
with the BARE tool names inside, and routes a ``function_call`` by its
``(namespace, name)`` pair. ``us.openai.gpt-6-astra`` on Bedrock answers with
``"namespace": "mcp__qualgent"``; ``us.moonshotai.kimi-k3`` answers with the bare
name and no namespace, so codex looks up ``qg_start`` in the default namespace and
fails every MCP call with ``unsupported call: qg_start`` (no codex version or config
option changes this: the router has no fallback, and `non_prefixed_mcp_tool_names`
only renames the namespace).

The relay forwards each request byte for byte to
``https://bedrock-runtime.<region>.amazonaws.com`` (the agent's own bearer token
travels in its own Authorization header; the relay holds no credential) and, in the
streamed response, adds the namespace to a ``function_call`` that has none when its
name is a tool of exactly one namespace in that request and is not also a top-level
tool. Nothing else is changed; a model that already names the namespace is passed
through untouched, so the transport is the same for every codex × Bedrock arm.
"""

from __future__ import annotations

import http.client
import json
import logging
import ssl
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

#: Request headers that describe this hop, not the request; never forwarded.
_HOP_HEADERS = frozenset({
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "accept-encoding", "proxy-connection", "te", "upgrade",
})
_UPSTREAM_TIMEOUT_SEC = 900


def _tls_context() -> ssl.SSLContext:
    """The system trust store, or certifi's bundle when it is installed (a
    python.org or uv-managed interpreter on macOS has no usable system store)."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def bedrock_runtime_origin(region: str) -> str:
    """The origin codex's `amazon-bedrock-runtime` provider talks to in ``region``."""
    return f"https://bedrock-runtime.{region}.amazonaws.com"


def namespace_index(request: Any) -> dict[str, str]:
    """Bare tool name → its namespace, for names that belong to exactly one namespace
    in this request's ``tools`` and are not also a top-level tool name."""
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    tools = request.get("tools") if isinstance(request, dict) else None
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace" and isinstance(tool.get("name"), str):
            for child in tool.get("tools") or []:
                name = child.get("name") if isinstance(child, dict) else None
                if not isinstance(name, str):
                    continue
                if name in found and found[name] != tool["name"]:
                    ambiguous.add(name)
                found[name] = tool["name"]
        elif isinstance(tool.get("name"), str):
            ambiguous.add(tool["name"])
    return {name: ns for name, ns in found.items() if name not in ambiguous}


def add_missing_namespaces(value: Any, index: dict[str, str]) -> int:
    """Set ``namespace`` on every ``function_call`` in ``value`` that lacks one and
    whose name ``index`` knows. Mutates in place; returns how many were set."""
    count = 0
    if isinstance(value, dict):
        if (value.get("type") == "function_call" and not value.get("namespace")
                and value.get("name") in index):
            value["namespace"] = index[value["name"]]
            count += 1
        for child in value.values():
            count += add_missing_namespaces(child, index)
    elif isinstance(value, list):
        for child in value:
            count += add_missing_namespaces(child, index)
    return count


def rewrite_sse_line(line: bytes, index: dict[str, str]) -> tuple[bytes, int]:
    """One line of a server-sent-event stream, with namespaces added to its ``data:``
    JSON. Any other line, or data that is not JSON, comes back unchanged."""
    if not index or not line.startswith(b"data:"):
        return line, 0
    body = line[5:].strip()
    try:
        event = json.loads(body)
    except ValueError:
        return line, 0
    count = add_missing_namespaces(event, index)
    if not count:
        return line, 0
    ending = line[len(line.rstrip(b"\r\n")):] or b"\n"
    return b"data: " + json.dumps(event, separators=(",", ":")).encode() + ending, count


class BedrockToolShim:
    """One episode's relay: ``start()`` → the base URL codex should use;
    ``stop()`` shuts it down. ``rewrites`` counts the namespaces it added."""

    def __init__(self, upstream_origin: str):
        parts = urllib.parse.urlsplit(upstream_origin)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError(f"bad upstream origin {upstream_origin!r}")
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, parts.port
        self._tls = _tls_context() if parts.scheme == "https" else None
        self._lock = threading.Lock()
        self.rewrites = 0
        self.requests = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str | None = None

    # The handler is built per shim so it can reach this instance.
    def _handler(self) -> type[BaseHTTPRequestHandler]:
        shim = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
                logger.debug("bedrock tool shim: " + fmt, *args)

            def do_POST(self) -> None:
                shim._relay(self, "POST")

            def do_GET(self) -> None:
                shim._relay(self, "GET")

        return Handler

    def _connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port,
                                               timeout=_UPSTREAM_TIMEOUT_SEC,
                                               context=self._tls)
        return http.client.HTTPConnection(self._host, self._port,
                                          timeout=_UPSTREAM_TIMEOUT_SEC)

    def _relay(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None
        index: dict[str, str] = {}
        if body and not handler.headers.get("Content-Encoding"):
            try:
                index = namespace_index(json.loads(body))
            except ValueError:
                index = {}
        headers = {k: v for k, v in handler.headers.items()
                   if k.lower() not in _HOP_HEADERS}
        headers["Accept-Encoding"] = "identity"   # the stream is rewritten as text
        with self._lock:
            self.requests += 1
        conn = self._connection()
        try:
            try:
                conn.request(method, handler.path, body=body, headers=headers)
                upstream = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                message = json.dumps({"error": {
                    "message": f"bedrock tool shim: upstream unreachable: {exc}",
                    "type": "upstream_error"}}).encode()
                handler.send_response(502)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(message)))
                handler.end_headers()
                handler.wfile.write(message)
                return
            handler.send_response(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() not in _HOP_HEADERS and key.lower() != "content-length":
                    handler.send_header(key, value)
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            while True:
                line = upstream.readline()
                if not line:
                    break
                line, added = rewrite_sse_line(line, index)
                if added:
                    with self._lock:
                        self.rewrites += added
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # codex closes the stream as soon as it has `response.completed`.
            handler.close_connection = True
        finally:
            conn.close()

    def start(self) -> str:
        """Listen on 127.0.0.1 (a free port) and return the base URL for codex's
        `amazon-bedrock-runtime` provider (`.../openai/v1`)."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="bedrock-tool-shim", daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}/openai/v1"
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
