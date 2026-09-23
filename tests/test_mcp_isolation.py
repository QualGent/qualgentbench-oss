"""MCP-arm episode isolation (QUA-2800).

One standalone DevLoop-MCP server serves every episode of a run. A DevLoop build
without per-client-session scoping keeps the routine action log, visual
baselines, traces and recordings per DEVICE, so an episode could read an earlier
one's route (the other arm of the same case included) or be refused with "a trace
is already recording". The server now scopes state per MCP client session and
serves `GET /devloop/sessions`; the harness refuses a DevLoop server without it and
records, per episode, every session that touched the device and whether each
started clean (`provenance.mcp_isolation`). No test here reaches a device.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from qualgentbench import session as sess_mod
from test_agent_dump import device  # noqa: F401 — the fake-device fixture

FIXTURE = Path(__file__).parent / "fixtures" / "devloop_sessions.json"
DEVICE = "emulator-5554"


def _serve(monkeypatch, handler):
    """Answer fetch_episode_isolation's GET with `handler(request) -> httpx.Response`."""
    real = httpx.AsyncClient
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        sess_mod.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, transport=httpx.MockTransport(_handler), **kw))
    return seen


def _real_body() -> dict:
    body = json.loads(FIXTURE.read_text())
    body.pop("_about")
    return body


def test_the_real_server_record_parses_to_a_clean_episode(monkeypatch):
    """The fixture is DevLoop's own output: a harness setup session, an agent
    session that left a trace running and never closed, and the next agent
    session, which took the device over."""
    body = _real_body()
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    since = body["sessions"][1]["created_at"]
    out = asyncio.run(sess_mod.fetch_episode_isolation(
        "http://127.0.0.1:51821", device=DEVICE, since=since))
    assert seen[0].url.path == "/devloop/sessions"
    assert seen[0].url.params["device"] == DEVICE and seen[0].url.params["since"] == since
    assert out["isolation"] == sess_mod.ISOLATED
    assert out["clean"] is True
    assert [s["scope_id"] for s in out["sessions"]] == [s["scope_id"] for s in body["sessions"]]
    last = out["sessions"][-1]
    assert last["previous_owner"] == body["sessions"][1]["scope_id"]
    assert last["previous_owner_stopped"]["native_profiler"] == {"traces_stopped": 1}
    assert out["artifact_roots"] == body["artifact_roots"]


def test_sessions_after_the_agent_exited_are_not_the_episodes(monkeypatch):
    body = _real_body()
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    until = body["sessions"][1]["created_at"]
    out = asyncio.run(sess_mod.fetch_episode_isolation(
        "http://x:1", device=DEVICE, until=until))
    assert [s["scope_id"] for s in out["sessions"]] == [
        s["scope_id"] for s in body["sessions"][:2]]


def test_a_session_that_did_not_start_clean_fails_the_episode(monkeypatch):
    body = _real_body()
    body["sessions"][-1]["clean_at_start"] = False
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    assert out["clean"] is False


@pytest.mark.parametrize("response", [
    httpx.Response(404, text="Not Found"),               # DevLoop before QUA-2800
    httpx.Response(200, json={"isolation": "none", "sessions": []}),
])
def test_a_server_without_isolation_is_never_clean(monkeypatch, response):
    _serve(monkeypatch, lambda r: response)
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    assert out["clean"] is False
    assert out["isolation"] in ("unavailable", "none")


def test_an_unreachable_server_is_recorded_not_raised(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    _serve(monkeypatch, refuse)
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    assert out["isolation"] == "unavailable" and out["clean"] is False
    assert "ConnectError" in out["error"]


# ── doctor / preflight / run refusal ───────────────────────────────────────────

def _identify_as(monkeypatch, server_name: str):
    """The server's `initialize` answers with `server_name`."""
    import contextlib

    import mcp.client.session as sess_client
    import mcp.client.streamable_http as http_mod

    class _Session:
        async def initialize(self):
            return SimpleNamespace(serverInfo=SimpleNamespace(name=server_name),
                                   instructions="APP SOURCE: none.")

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="mobile_tap")])

    @contextlib.asynccontextmanager
    async def fake_http(url):
        yield (None, None, None)

    @contextlib.asynccontextmanager
    async def fake_client(r, w):
        yield _Session()

    monkeypatch.setattr(http_mod, "streamablehttp_client", fake_http)
    monkeypatch.setattr(sess_client, "ClientSession", fake_client)


def test_doctor_fails_a_devloop_server_without_isolation(monkeypatch):
    from qualgentbench.doctor import check_mcp_episode_isolation

    _identify_as(monkeypatch, "devloop-mcp")
    _serve(monkeypatch, lambda r: httpx.Response(404))
    result = asyncio.run(check_mcp_episode_isolation("http://127.0.0.1:51831"))
    assert result.passed is False and result.warning is False   # it moves numbers
    assert "QUA-2800" in result.fix
    assert "--mcp-server http://127.0.0.1:51831" in result.fix


def test_doctor_accepts_an_isolating_devloop_server(monkeypatch):
    from qualgentbench.doctor import check_mcp_episode_isolation

    _identify_as(monkeypatch, "devloop-mcp")
    _serve(monkeypatch, lambda r: httpx.Response(200, json=_real_body()))
    result = asyncio.run(check_mcp_episode_isolation("http://127.0.0.1:51831"))
    assert result.passed is True


def test_doctor_does_not_judge_other_servers(monkeypatch):
    from qualgentbench.doctor import check_mcp_episode_isolation

    _identify_as(monkeypatch, "some-other-server")
    seen = _serve(monkeypatch, lambda r: httpx.Response(404))
    result = asyncio.run(check_mcp_episode_isolation("http://127.0.0.1:51831"))
    assert result.passed is True and seen == []


def test_run_refuses_a_devloop_server_without_isolation(monkeypatch):
    from qualgentbench.cli import _preflight

    _identify_as(monkeypatch, "devloop-mcp")

    def answer(request):
        return httpx.Response(404)

    _serve(monkeypatch, answer)

    class _Client:   # the plain GET /mcp health probe: alive
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, _url): return SimpleNamespace(status_code=406)

    monkeypatch.setattr(httpx, "Client", _Client)
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = "http://127.0.0.1:51831"
    with pytest.raises(Exception) as exc_info:
        asyncio.run(_preflight(s, s.bridge_url, "codex-cli", None, None))
    assert "does not isolate client sessions" in str(exc_info.value)


def test_preflight_runs_the_isolation_check(monkeypatch):
    from qualgentbench import preflight
    from qualgentbench.doctor import CheckResult

    async def ok(url): return CheckResult("x", True, "")

    async def isolation(url): return CheckResult("MCP episode isolation", False, "no")

    for name in ("check_mcp_bridge", "check_mcp_tools", "check_mcp_app_source"):
        monkeypatch.setattr(preflight, name, ok)
    monkeypatch.setattr(preflight, "check_mcp_episode_isolation", isolation)
    cfg = SimpleNamespace(mcp_server="http://127.0.0.1:51831")
    results = asyncio.run(preflight.check_mcp(cfg))
    assert [r.name for r in preflight.failed(results)] == ["MCP episode isolation"]


# ── provenance ──────────────────────────────────────────────────────────────────

def test_provenance_carries_the_isolation_record(monkeypatch, tmp_path):
    from qualgentbench import episode_runner
    from qualgentbench.episode_runner import EpisodeOptions, _provenance

    async def _no_avd(serial):
        return None

    monkeypatch.setattr(episode_runner, "_avd_name", _no_avd)
    opts = EpisodeOptions(agent="claude-code", model="claude-opus-5", condition="raw",
                          trial=1, mcp_server=None, runs_dir=tmp_path)
    record = {"isolation": "per_mcp_session", "sessions": [], "clean": True}
    assert asyncio.run(_provenance(opts, DEVICE))["mcp_isolation"] is None
    assert asyncio.run(_provenance(opts, DEVICE, mcp_isolation=record))["mcp_isolation"] == record


async def test_an_mcp_episode_records_the_servers_sessions(monkeypatch, tmp_path, device):  # noqa: F811
    """Wired into run_episode: after the agent exits the harness asks the server
    for the sessions that touched THIS device during the agent's run."""
    from qualgentbench import episode_runner as er
    from test_agent_dump import SERIAL, _episode

    device(u2=False)
    task, opts, _agent = _episode(monkeypatch, tmp_path, [])
    opts.mcp_server = "http://127.0.0.1:51831"

    class _NoMeter:
        def __init__(self, *_a, **_kw): pass
        async def start(self, *_a, **_kw): return 0
        async def stop(self): return None
        def url(self, _path): return "http://127.0.0.1:1"

    monkeypatch.setattr(er, "McpMeter", _NoMeter)
    calls = []
    record = {"isolation": "per_mcp_session", "sessions": [], "clean": True,
              "artifact_roots": ["/srv/devloop-artifacts"]}

    async def fetch(url, **kw):
        calls.append((url, kw))
        return record

    monkeypatch.setattr(er, "fetch_episode_isolation", fetch)
    result = await er.run_episode(task, opts)

    assert result.provenance["mcp_isolation"] == record
    # The scan that scores this episode voids a read of the server's artifact roots:
    # the ones it reported plus the documented defaults.
    from pathlib import Path

    roots = task.bug_spec["devloop_roots"]
    assert "/srv/devloop-artifacts" in roots
    assert str(Path.home() / ".devloop-mcp") in roots
    (url, kw), = calls
    assert url == opts.mcp_server and kw["device"] == SERIAL
    from datetime import datetime

    def at(v): return v if isinstance(v, datetime) else datetime.fromisoformat(v)

    assert datetime.fromisoformat(kw["since"]) == at(result.started_at)
    assert datetime.fromisoformat(kw["until"]) == at(result.ended_at)
