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


# ── the log_capture stop, wherever DevLoop reported it (QUA-3150) ──────────────
# DevLoop-MCP's standalone log capture stops with the session that owned the device and
# reports `log_capture: {captures_stopped: n}`. If the crash arm's session ended before
# the clean arm claimed the device, `end_scope` already removed the owner, so the stop is
# in the crash arm's `released[device].stopped`; otherwise it is in the clean arm's claim
# (`previous_owner_stopped`). The record carries both, and neither moves `clean`.

LOG_STOP = {"captures_stopped": 1}


def test_the_real_server_record_surfaces_each_sessions_released_stops(monkeypatch):
    body = _real_body()
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    for rec, got in zip(body["sessions"], out["sessions"], strict=True):
        assert got["released_stopped"] == (
            rec.get("released", {}).get(DEVICE, {}).get("stopped") or {}), rec["scope_id"]
    assert out["sessions"][1]["released_stopped"]["native_profiler"] == {"traces_stopped": 1}
    assert out["clean"] is True


def test_a_log_capture_stop_in_the_clean_arms_claim_is_reported_there(monkeypatch):
    body = _real_body()
    crash, clean = body["sessions"][1], body["sessions"][2]
    clean["devices"][DEVICE]["previous_owner_stopped"]["log_capture"] = dict(LOG_STOP)
    crash["released"][DEVICE]["stopped"].pop("log_capture", None)
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    by_id = {s["scope_id"]: s for s in out["sessions"]}
    assert by_id[clean["scope_id"]]["previous_owner_stopped"]["log_capture"] == LOG_STOP
    assert "log_capture" not in by_id[crash["scope_id"]]["released_stopped"]
    assert out["clean"] is True


def test_a_log_capture_stop_in_the_crash_arms_release_is_reported_there(monkeypatch):
    """The crash-arm session ended first: `end_scope` released the device, so the
    clean arm's claim took it from nobody and stopped nothing."""
    body = _real_body()
    crash, clean = body["sessions"][1], body["sessions"][2]
    crash["released"][DEVICE]["stopped"] = {"log_capture": dict(LOG_STOP),
                                            "device_session": {"dropped": True}}
    clean["devices"][DEVICE].update(previous_owner=None, previous_owner_stopped={})
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    by_id = {s["scope_id"]: s for s in out["sessions"]}
    assert by_id[crash["scope_id"]]["released_stopped"]["log_capture"] == LOG_STOP
    assert by_id[clean["scope_id"]]["previous_owner_stopped"] == {}
    assert out["clean"] is True


@pytest.mark.parametrize("released", [
    None, [], "emulator-5554", {DEVICE: None}, {DEVICE: []}, {DEVICE: "x"},
    {DEVICE: {"stopped": None}}, {DEVICE: {"stopped": ["log_capture"]}},
    {DEVICE: {"stopped": "log_capture"}}, {"emulator-5556": {"stopped": LOG_STOP}},
])
def test_a_malformed_released_value_never_raises(monkeypatch, released):
    body = _real_body()
    for rec in body["sessions"]:
        rec["released"] = released
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    assert [s["released_stopped"] for s in out["sessions"]] == [{}] * len(body["sessions"])
    assert out["clean"] is True


LOG_CAPTURE_FIXTURE = Path(__file__).parent / "fixtures" / "devloop_sessions_log_capture.json"


def test_the_real_log_capture_record_shows_the_crash_arms_stop_in_its_release(monkeypatch):
    """DevLoop's own body after a real crash-arm-then-clean-arm run (claude-code, which
    leaves its session open): the next setup session took the device over and stopped the
    crash arm's capture, so `log_capture` is in the crash arm's `released_stopped` and
    the setup session's claim, and the clean arm took a released device."""
    body = json.loads(LOG_CAPTURE_FIXTURE.read_text())
    body.pop("_about")
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    crash, setup, clean = out["sessions"]
    assert crash["released_stopped"]["log_capture"] == LOG_STOP
    assert setup["previous_owner"] == crash["scope_id"]
    assert setup["previous_owner_stopped"]["log_capture"] == LOG_STOP
    assert clean["previous_owner"] is None and clean["previous_owner_stopped"] == {}
    assert clean["clean_at_start"] is True and out["clean"] is True


def test_the_session_ref_is_recorded_as_the_session_id(monkeypatch):
    """DevLoop-MCP now serves an opaque `mcp_session_ref` instead of the raw id; the
    record keeps it under `mcp_session_id` rather than recording null."""
    body = json.loads(LOG_CAPTURE_FIXTURE.read_text())
    body.pop("_about")
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    refs = [rec["mcp_session_ref"] for rec in body["sessions"]]
    assert all(r and r.startswith("ref-") for r in refs)
    assert [s["mcp_session_id"] for s in out["sessions"]] == refs


def test_an_older_servers_session_id_still_reads(monkeypatch):
    old = _real_body()
    _serve(monkeypatch, lambda r: httpx.Response(200, json=old))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1", device=DEVICE))
    assert [s["mcp_session_id"] for s in out["sessions"]] == [
        rec["mcp_session_id"] for rec in old["sessions"]]


# ── the harness ends the agent's session (QUA-3150) ────────────────────────────

def _sessions_server(monkeypatch, *, delete_status: int, released_after: int = 0):
    """A /mcp that answers DELETE with `delete_status`, and a /devloop/sessions whose
    one session shows `artifacts_released` from the `released_after`-th GET on."""
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(delete_status)
        gets["n"] += 1
        released = {"at": "t", "reason": "session_ended", "deleted": True}
        rec = {"scope_id": "s", "artifacts_released":
               released if gets["n"] > released_after else None}
        return httpx.Response(200, json={"isolation": "per_mcp_session", "sessions": [rec]})

    return _serve(monkeypatch, handler), gets


def test_an_open_agent_session_is_ended_and_awaited(monkeypatch):
    seen, gets = _sessions_server(monkeypatch, delete_status=200, released_after=2)
    out = asyncio.run(sess_mod.end_mcp_sessions(
        "http://127.0.0.1:51851", ["sid-1"], device=DEVICE, since="2026-10-22T06:00:00+00:00",
        settle=5.0))
    assert out == {"ended": 1, "not_open": 0, "errors": [], "settled": True}
    delete = seen[0]
    assert delete.method == "DELETE" and delete.url.path == "/mcp"
    assert delete.headers["mcp-session-id"] == "sid-1"
    assert gets["n"] == 3                         # polled until the release showed
    assert seen[-1].url.params["device"] == DEVICE


def test_a_session_the_client_already_closed_is_a_no_op(monkeypatch):
    """codex-cli DELETEs its own session at exit: the server no longer knows the id."""
    seen, gets = _sessions_server(monkeypatch, delete_status=404)
    out = asyncio.run(sess_mod.end_mcp_sessions(
        "http://127.0.0.1:51851/mcp", ["sid-1"], device=DEVICE))
    assert out == {"ended": 0, "not_open": 1, "errors": [], "settled": None}
    assert [r.method for r in seen] == ["DELETE"] and gets["n"] == 0


def test_no_session_ids_sends_nothing(monkeypatch):
    seen, _ = _sessions_server(monkeypatch, delete_status=200)
    out = asyncio.run(sess_mod.end_mcp_sessions("http://x:1", [], device=DEVICE))
    assert out["ended"] == 0 and seen == []


def test_ending_a_session_never_raises(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    _serve(monkeypatch, refuse)
    out = asyncio.run(sess_mod.end_mcp_sessions("http://x:1", ["a", "b"], device=DEVICE))
    assert out["ended"] == 0 and len(out["errors"]) == 2


def test_a_server_that_never_shows_the_release_stops_waiting(monkeypatch):
    _sessions_server(monkeypatch, delete_status=200, released_after=10**6)
    out = asyncio.run(sess_mod.end_mcp_sessions(
        "http://x:1", ["sid"], device=DEVICE, settle=0.3))
    assert out["ended"] == 1 and out["settled"] is False


def test_the_session_end_record_moves_no_integrity_verdict():
    """`agent_sessions_ended` rides in `provenance.mcp_isolation`; the only scored use
    of that record (`failures.mcp_integrity`) reads `clean`/`isolation` alone."""
    from qualgentbench import failures

    for clean in (True, False):
        base = {"isolation": sess_mod.ISOLATED, "sessions": [], "clean": clean}
        with_end = dict(base, agent_sessions_ended={
            "ended": 1, "not_open": 0, "errors": [], "settled": True})
        assert failures.mcp_integrity(with_end, "devloop-mcp") == failures.mcp_integrity(
            base, "devloop-mcp")


def test_without_a_device_no_released_stops_are_read(monkeypatch):
    body = _real_body()
    _serve(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = asyncio.run(sess_mod.fetch_episode_isolation("http://x:1"))
    assert all(s["released_stopped"] == {} for s in out["sessions"])


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
        session_ids: list = ["agent-sid"]

    monkeypatch.setattr(er, "McpMeter", _NoMeter)
    calls = []
    record = {"isolation": "per_mcp_session", "sessions": [], "clean": True,
              "artifact_roots": ["/srv/devloop-artifacts"]}
    ended = {"ended": 1, "not_open": 0, "errors": [], "settled": True}

    async def end(url, ids, **kw):
        # QUA-3150: the agent's own session is ended BEFORE the record is read.
        calls.append(("end", url, ids, kw))
        return ended

    async def fetch(url, **kw):
        calls.append((url, kw))
        return record

    monkeypatch.setattr(er, "end_mcp_sessions", end)
    monkeypatch.setattr(er, "fetch_episode_isolation", fetch)

    async def identity(url, **_kw):
        return {"name": "devloop-mcp", "version": "1.27.0", "app_source": "none",
                "instructions_sha256": "a" * 64, "tools_sha256": "b" * 64, "tools": 90}

    monkeypatch.setattr(er, "fetch_server_identity", identity)
    result = await er.run_episode(task, opts)

    assert result.provenance["mcp_isolation"] == dict(record, agent_sessions_ended=ended)
    assert "agent_sessions_ended" not in json.dumps(result.metrics)
    (_tag, end_url, end_ids, end_kw), *calls = calls
    assert _tag == "end" and end_url == opts.mcp_server and end_ids == ["agent-sid"]
    assert end_kw["device"] == SERIAL
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
