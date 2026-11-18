"""codex × Bedrock tool-namespace relay (QUA-3535).

codex advertises MCP tools inside a Responses-API ``namespace`` and routes a
``function_call`` by (namespace, name); Kimi K3 on Bedrock answers with the bare name,
which codex rejects as ``unsupported call``. The relay puts the namespace back. These
tests run the relay against a local fake upstream: no network, no codex.
"""

from __future__ import annotations

import json
import threading
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qualgentbench.adapters import bedrock_tool_shim as shim_mod
from qualgentbench.adapters.bedrock_tool_shim import (
    BedrockToolShim, add_missing_namespaces, namespace_index, rewrite_sse_line)
from qualgentbench.adapters.codex_cli import BEDROCK_SHIM_ENV, CodexCliAdapter

from test_provider_adapters import KIMI, _ctx, _no_codex_spawn, bedrock_env  # noqa: F401

REQUEST = {
    "model": KIMI,
    "tools": [
        {"type": "function", "name": "exec_command"},
        {"type": "namespace", "name": "mcp__device", "tools": [
            {"type": "function", "name": "mobile_tap"},
            {"type": "function", "name": "mobile_observe_screen"}]},
        {"type": "namespace", "name": "mcp__qualgent", "tools": [
            {"type": "function", "name": "list_test_cases"},
            {"type": "function", "name": "mobile_observe_screen"},   # in two namespaces
            {"type": "function", "name": "exec_command"}]},          # also top-level
    ],
}


def test_index_keeps_only_unambiguous_namespaced_names():
    assert namespace_index(REQUEST) == {"mobile_tap": "mcp__device",
                                        "list_test_cases": "mcp__qualgent"}
    assert namespace_index({}) == {} and namespace_index([]) == {}


def test_only_a_function_call_without_a_namespace_is_changed():
    index = namespace_index(REQUEST)
    bare = {"type": "function_call", "name": "mobile_tap", "arguments": "{}"}
    named = {"type": "function_call", "name": "mobile_tap", "namespace": "mcp__x"}
    shell = {"type": "function_call", "name": "exec_command"}
    ambiguous = {"type": "function_call", "name": "mobile_observe_screen"}
    event = {"type": "response.completed",
             "response": {"output": [bare, named, shell, ambiguous]}}
    assert add_missing_namespaces(event, index) == 1
    assert bare["namespace"] == "mcp__device"
    assert named["namespace"] == "mcp__x"
    assert "namespace" not in shell and "namespace" not in ambiguous


def test_sse_lines_other_than_rewritten_data_pass_through_byte_for_byte():
    index = namespace_index(REQUEST)
    for line in (b"event: response.output_item.done\n", b"\n", b"data: [DONE]\n",
                 b'data: {"type":"response.output_text.delta","delta":"hi"}\r\n'):
        assert rewrite_sse_line(line, index) == (line, 0)
    item = {"type": "response.output_item.done",
            "item": {"type": "function_call", "name": "list_test_cases"}}
    out, added = rewrite_sse_line(b"data: " + json.dumps(item).encode() + b"\r\n", index)
    assert added == 1 and out.endswith(b"\r\n")
    assert json.loads(out[5:])["item"]["namespace"] == "mcp__qualgent"


class _Upstream(BaseHTTPRequestHandler):
    """A fake Bedrock runtime: records the request, streams a Kimi-shaped reply."""
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append({"path": self.path, "headers": dict(self.headers),
                                "body": body})
        events = [
            {"type": "response.output_item.done",
             "item": {"type": "function_call", "name": "list_test_cases",
                      "arguments": "{}", "call_id": "c1"}},
            {"type": "response.completed", "response": {"output": [
                {"type": "function_call", "name": "list_test_cases", "call_id": "c1"}]}},
        ]
        payload = b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n"
                           for e in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream():
    _Upstream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_relay_forwards_the_request_and_adds_the_namespace(upstream):
    shim = BedrockToolShim(upstream)
    base = shim.start()
    try:
        body = json.dumps(REQUEST).encode()
        req = urllib.request.Request(
            base + "/responses", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer tok"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
    finally:
        shim.stop()
    (seen,) = _Upstream.seen
    assert seen["path"] == "/openai/v1/responses"
    assert seen["body"] == body                       # the request is not touched
    assert seen["headers"]["Authorization"] == "Bearer tok"
    data = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
    assert data[0]["item"]["namespace"] == "mcp__qualgent"
    assert data[1]["response"]["output"][0]["namespace"] == "mcp__qualgent"
    assert shim.rewrites == 2 and shim.requests == 1


def test_relay_passes_throttling_through_unchanged():
    """A Bedrock 429 keeps its status, Retry-After and body, so codex's retry and the
    harness's throttle detection (QUA-3505) see exactly what Bedrock sent."""
    body = json.dumps({"error": {"message": "Too many tokens, please wait before trying again.",
                                 "type": "ThrottlingException"}}).encode()

    class Throttled(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "17")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Throttled)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    shim = BedrockToolShim(f"http://127.0.0.1:{upstream.server_address[1]}")
    base = shim.start()
    try:
        request = {"tools": [{"type": "namespace", "name": "mcp__qualgent",
                              "tools": [{"type": "function", "name": "qg_start"}]}]}
        req = urllib.request.Request(base + "/responses", data=json.dumps(request).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 429
        assert err.value.headers.get("Retry-After") == "17"
        assert err.value.read() == body
        assert shim.rewrites == 0
    finally:
        shim.stop()
        upstream.shutdown()


def test_relay_answers_502_when_upstream_is_down():
    shim = BedrockToolShim("http://127.0.0.1:9")
    base = shim.start()
    try:
        req = urllib.request.Request(base + "/responses", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 502
    finally:
        shim.stop()


def test_codex_bedrock_episode_points_its_provider_at_the_relay(tmp_path, monkeypatch,
                                                                 bedrock_env):  # noqa: F811
    _no_codex_spawn(monkeypatch)
    adapter, ctx = CodexCliAdapter(), _ctx(tmp_path, "codex-cli", KIMI)
    adapter.prepare(ctx)
    try:
        config = tomllib.loads((ctx.run_dir / "codex_home" / "config.toml").read_text())
        assert config["model_provider"] == "amazon-bedrock-runtime"
        base_url = config["model_providers"]["amazon-bedrock-runtime"]["base_url"]
        assert base_url.startswith("http://127.0.0.1:") and base_url.endswith("/openai/v1")
        shim = CodexCliAdapter._SHIMS[str(ctx.run_dir)]
        assert shim._host == "bedrock-runtime.us-east-2.amazonaws.com"
    finally:
        adapter.cleanup(ctx)
    assert str(ctx.run_dir) not in CodexCliAdapter._SHIMS
    record = json.loads((ctx.run_dir / "agent" / "bedrock_tool_shim.json").read_text())
    assert record == {"requests": 0, "namespaces_added": 0}


def test_relay_can_be_switched_off(tmp_path, monkeypatch, bedrock_env):  # noqa: F811
    _no_codex_spawn(monkeypatch)
    monkeypatch.setenv(BEDROCK_SHIM_ENV, "0")
    adapter, ctx = CodexCliAdapter(), _ctx(tmp_path, "codex-cli", KIMI)
    adapter.prepare(ctx)
    config = tomllib.loads((ctx.run_dir / "codex_home" / "config.toml").read_text())
    assert config["model_provider"] == "amazon-bedrock-runtime"
    assert "model_providers" not in config
    assert str(ctx.run_dir) not in CodexCliAdapter._SHIMS


def test_openai_episode_has_no_relay(tmp_path, monkeypatch, bedrock_env):  # noqa: F811
    _no_codex_spawn(monkeypatch)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-dummy")
    adapter, ctx = CodexCliAdapter(), _ctx(tmp_path, "codex-cli", "gpt-6-astra")
    adapter.prepare(ctx)
    assert "model_providers" not in (ctx.run_dir / "codex_home" / "config.toml").read_text()
    assert str(ctx.run_dir) not in CodexCliAdapter._SHIMS
    assert shim_mod.bedrock_runtime_origin("us-east-2").endswith("us-east-2.amazonaws.com")
