"""How the adapter starts, stops and preserves an agent's output.

A timeout must keep the output already buffered, and SIGTERM must precede
SIGKILL so the agent can close its MCP transport and free the device hold.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualgentbench.adapters import base
from qualgentbench.adapters.base import AgentAdapter, RunContext
from qualgentbench.schemas import Condition


class _ScriptAgent(AgentAdapter):
    """Runs a literal Python script as the 'agent'."""

    name = "fake"

    def __init__(self, script: str) -> None:
        self._script = script

    def command(self, instruction: str, context: RunContext) -> list[str]:
        return [sys.executable, "-u", "-c", self._script]

    def env(self, context: RunContext) -> dict[str, str]:
        return {}


def _context(tmp_path: Path, timeout_sec: float) -> RunContext:
    run_dir = tmp_path / "run"
    workspace = tmp_path / "ws"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=timeout_sec)),
        agent="fake", model="m", condition=Condition.no_routines, trial=1,
        run_dir=run_dir, mcp_server="",
        mcp_config_path=tmp_path / "mcp.json", workspace_dir=workspace,
        disabled_tools=[], inject_mcp=False,
    )


def _run(adapter: AgentAdapter, ctx: RunContext, instruction: str = "go") -> tuple[str, int]:
    return asyncio.run(adapter.run(instruction, ctx))


# ── the regression ────────────────────────────────────────────────────────────

def test_timeout_preserves_output_emitted_before_the_kill(tmp_path):
    """Output produced before a wall-clock timeout must survive it."""
    agent = _ScriptAgent(
        "import sys, time\n"
        "sys.stdout.write('EVENT-1\\n')\n"
        "sys.stdout.write('EVENT-2\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    ctx = _context(tmp_path, timeout_sec=2.0)
    transcript, _ = _run(agent, ctx)

    assert "EVENT-1" in transcript
    assert "EVENT-2" in transcript
    # and it is on disk, not only in the return value
    assert "EVENT-1" in (ctx.run_dir / "agent" / "transcript.txt").read_text()


def test_timeout_is_recorded_so_a_slow_model_is_not_read_as_a_zero(tmp_path):
    agent = _ScriptAgent("import time; time.sleep(60)")
    ctx = _context(tmp_path, timeout_sec=1.0)
    _run(agent, ctx)
    assert (ctx.run_dir / "timed_out").exists()


def test_completed_episode_is_not_marked_timed_out(tmp_path):
    agent = _ScriptAgent("print('done')")
    ctx = _context(tmp_path, timeout_sec=30.0)
    transcript, code = _run(agent, ctx)
    assert "done" in transcript
    assert code == 0
    assert not (ctx.run_dir / "timed_out").exists()


def test_stale_timeout_flag_is_cleared_between_episodes(tmp_path):
    ctx = _context(tmp_path, timeout_sec=30.0)
    (ctx.run_dir / "timed_out").write_text("from a previous episode")
    _run(_ScriptAgent("print('ok')"), ctx)
    assert not (ctx.run_dir / "timed_out").exists()


# ── stopping cleanly, so the device lock is released ──────────────────────────

def test_agent_gets_sigterm_and_can_shut_down_cleanly(tmp_path):
    """SIGTERM, not SIGKILL. This is what lets the agent close its MCP transport
    so the bridge frees the device hold instead of stranding it for 15 minutes."""
    agent = _ScriptAgent(
        "import sys, signal, time\n"
        "def bye(sig, frame):\n"
        "    sys.stdout.write('CLEAN-SHUTDOWN\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        "sys.stdout.write('READY\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    ctx = _context(tmp_path, timeout_sec=2.0)
    transcript, _ = _run(agent, ctx)

    assert "READY" in transcript
    # proves SIGTERM arrived AND that output written after it is still captured
    assert "CLEAN-SHUTDOWN" in transcript


def test_agent_ignoring_sigterm_is_still_killed(tmp_path, monkeypatch):
    """The grace period must not become a hang: SIGKILL follows."""
    monkeypatch.setattr(base, "_TERM_GRACE_SEC", 1.0)
    agent = _ScriptAgent(
        "import sys, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('STUBBORN\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    ctx = _context(tmp_path, timeout_sec=1.0)
    transcript, code = _run(agent, ctx)

    assert "STUBBORN" in transcript
    assert code != 0            # killed, not a clean exit


def test_agent_exit_ends_the_run_even_if_an_orphan_holds_stdout(tmp_path):
    """A backgrounded child (the real case: `adb root`) inherits stdout and holds
    the pipe open after the agent dies. The run must end on process EXIT, not on
    pipe EOF — a containerized episode once sat 35 minutes on exactly this. The
    orphan itself must be swept, or it keeps the device busy."""
    import os as _os
    import time as _time

    pid_file = tmp_path / "orphan.pid"
    agent = _ScriptAgent(
        "import subprocess, sys, pathlib\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
        "sys.stdout.write('WORK-DONE\\n')\n"
        "sys.stdout.flush()\n"
    )
    ctx = _context(tmp_path, timeout_sec=60.0)
    started = _time.monotonic()
    transcript, code = _run(agent, ctx)

    assert _time.monotonic() - started < 30, "run waited for the orphan's EOF"
    assert "WORK-DONE" in transcript
    assert code == 0
    assert not (ctx.run_dir / "timed_out").exists()   # exit, not a timeout

    orphan = int(pid_file.read_text())
    for _ in range(40):                               # SIGKILL + reaping can lag
        try:
            _os.kill(orphan, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.25)
    else:
        _os.kill(orphan, 9)
        raise AssertionError("orphaned child survived the process-group sweep")


# ── budget hard stop ──────────────────────────────────────────────────────────

def test_budget_sentinel_stops_the_agent_and_keeps_its_work(tmp_path):
    ctx = _context(tmp_path, timeout_sec=60.0)
    sentinel = ctx.run_dir / "truncated"
    agent = _ScriptAgent(
        "import sys, time, pathlib\n"
        "sys.stdout.write('BANKED-A-FINDING\\n')\n"
        "sys.stdout.flush()\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('cap hit')\n"
        "time.sleep(60)\n"
    )
    transcript, _ = _run(agent, ctx)

    assert "BANKED-A-FINDING" in transcript      # partial evidence survives
    assert not (ctx.run_dir / "timed_out").exists()   # budget != wall clock


def test_stale_truncated_sentinel_is_cleared_between_episodes(tmp_path):
    ctx = _context(tmp_path, timeout_sec=30.0)
    (ctx.run_dir / "truncated").write_text("previous episode")
    _run(_ScriptAgent("print('ok')"), ctx)
    assert not (ctx.run_dir / "truncated").exists()


# ── plumbing that must keep working ───────────────────────────────────────────

def test_instruction_reaches_the_agent_on_stdin(tmp_path):
    agent = _ScriptAgent("import sys; sys.stdout.write('GOT:' + sys.stdin.read())")
    transcript, _ = _run(agent, _context(tmp_path, timeout_sec=30.0), "the brief")
    assert "GOT:the brief" in transcript


def test_output_longer_than_one_read_chunk_is_complete(tmp_path):
    """Chunked reads replaced communicate(); a single line larger than the buffer
    must not be truncated or split-decoded."""
    size = base._READ_CHUNK_BYTES * 3
    agent = _ScriptAgent(f"import sys; sys.stdout.write('x' * {size} + '\\nEND\\n')")
    transcript, _ = _run(agent, _context(tmp_path, timeout_sec=30.0))
    assert transcript.count("x") == size
    assert "END" in transcript


def test_agent_that_exits_without_reading_stdin_does_not_break_the_run(tmp_path):
    """A CLI that dies early (bad auth, bad flag) closes stdin under us."""
    agent = _ScriptAgent("import sys; sys.stdout.write('EARLY-EXIT\\n'); sys.exit(3)")
    big = "x" * (base._READ_CHUNK_BYTES * 2)      # enough to block on a closed pipe
    transcript, code = _run(agent, _context(tmp_path, timeout_sec=30.0), big)
    assert "EARLY-EXIT" in transcript
    assert code == 3


@pytest.mark.parametrize("payload", ["café ☕", "\udcff broken utf-8 lead"])
def test_non_ascii_and_invalid_bytes_do_not_crash_the_transcript(tmp_path, payload):
    agent = _ScriptAgent(
        "import sys\n"
        "sys.stdout.buffer.write("
        f"{payload.encode('utf-8', errors='surrogateescape')!r})\n"
    )
    transcript, _ = _run(agent, _context(tmp_path, timeout_sec=30.0))
    assert isinstance(transcript, str)


# ── the flags reach the scored metrics ────────────────────────────────────────
#
# A recorded timeout buys nothing unless the episode metrics carry it.

def _hunt_verdict(**spec_overrides):
    from test_ablation import _hunt_task, _transcript, _bash, _text, _ALL_CORRECT
    from qualgentbench import bugs

    task = _hunt_task("raw")
    task.bug_spec.update(spec_overrides)
    transcript = _transcript(
        _bash("adb -s emulator-5554 exec-out screencap -p > /tmp/s.png"),
        _bash("adb -s emulator-5554 shell input tap 540 1200"),
        _text("Testing done.\nRESULT: " + _ALL_CORRECT),
    )
    return bugs.exploration_verdict(transcript, "m", task)


def test_timed_out_episode_is_flagged_in_metrics():
    assert _hunt_verdict(timed_out=True).metrics["timed_out"] is True


def test_timeout_and_budget_truncation_are_separate_signals():
    v = _hunt_verdict(timed_out=True)
    assert v.metrics["timed_out"] is True
    assert v.metrics["truncated"] is False      # ran out of clock, not of steps

    v = _hunt_verdict(truncated=True)
    assert v.metrics["truncated"] is True
    assert v.metrics["timed_out"] is False


def test_normal_episode_carries_neither_flag():
    m = _hunt_verdict().metrics
    assert m["timed_out"] is False
    assert m["truncated"] is False
    assert m["infra_failure"] is False         # it did real device work


def test_episode_with_no_device_evidence_is_an_infra_failure():
    """A lost transcript must not read as a legitimate zero."""
    from test_ablation import _hunt_task, _transcript, _text
    from qualgentbench import bugs

    v = bugs.exploration_verdict(_transcript(_text("")), "m", _hunt_task("raw"))
    assert v.metrics["device_actions"] == 0
    assert v.metrics["infra_failure"] is True


# ── device locking is a bridge feature, not an MCP-server feature ─────────────
#
# A standalone server has no lock tools; waiting for a lock no one can take
# would burn the retry window and then report the opposite of what happened.

class _FakeTool:
    def __init__(self, name): self.name = name


class _FakeSession:
    """Minimal stand-in for an MCP ClientSession."""

    def __init__(self, tool_names, busy_forever=False):
        self._tools = [_FakeTool(n) for n in tool_names]
        self._busy = busy_forever
        self.calls = []

    async def initialize(self): return None

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, args=None):
        self.calls.append(name)
        if name == "qg_acquire_device" and self._busy:
            return SimpleNamespace(
                content=[SimpleNamespace(text="device-busy: held by another session")])
        return SimpleNamespace(content=[SimpleNamespace(text="ok")])


def _patched_session(monkeypatch, fake):
    """Point check_device_available at `fake` instead of a real bridge."""
    import contextlib
    from qualgentbench import session as sess_mod

    @contextlib.asynccontextmanager
    async def fake_http(url):
        yield (None, None, None)

    @contextlib.asynccontextmanager
    async def fake_client(r, w):
        yield fake

    import mcp.client.streamable_http as http_mod
    import mcp.client.session as sess_client
    monkeypatch.setattr(http_mod, "streamablehttp_client", fake_http)
    monkeypatch.setattr(sess_client, "ClientSession", fake_client)
    return sess_mod


def test_server_without_lock_tools_proceeds_immediately(monkeypatch):
    """A standalone server has qg_start/qg_docs only — must not wait, must not fail."""
    fake = _FakeSession(["qg_start", "qg_docs", "mobile_tap"])
    sess_mod = _patched_session(monkeypatch, fake)
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = "http://127.0.0.1:51821"

    asyncio.run(s.check_device_available("emulator-5554", retries=340, delay=3.0))
    assert fake.calls == []          # never tried to acquire a lock that cannot exist


def test_server_with_lock_tools_still_acquires_and_releases(monkeypatch):
    fake = _FakeSession(["qg_start", "qg_acquire_device", "qg_release_device"])
    sess_mod = _patched_session(monkeypatch, fake)
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = "http://127.0.0.1:51821"

    asyncio.run(s.check_device_available("emulator-5554", retries=2, delay=0.01))
    assert fake.calls == ["qg_acquire_device", "qg_release_device"]


def test_a_genuinely_busy_device_still_waits_then_reports(monkeypatch):
    fake = _FakeSession(
        ["qg_acquire_device", "qg_release_device"], busy_forever=True)
    sess_mod = _patched_session(monkeypatch, fake)
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = "http://127.0.0.1:51821"

    with pytest.raises(RuntimeError, match="locked"):
        asyncio.run(s.check_device_available("emulator-5554", retries=3, delay=0.01))
    assert fake.calls.count("qg_acquire_device") == 3      # it did retry


# ── server guidance ───────────────────────────────────────────────────────────

def test_instruction_names_no_device_lock_tools(monkeypatch):
    """Standalone mcp-mcp has none; telling the agent to call them wastes steps."""
    from test_ablation import _ablation_instruction, _instruction_task

    for tooling in ("raw", "mcp"):
        instr = _ablation_instruction(_instruction_task(), "emulator-5554", tooling)
        assert "qg_acquire_device" not in instr
        assert "qg_release_device" not in instr


# ── refusing to run against the desktop app ───────────────────────────────────
#
# The two servers look identical from outside, but the desktop bridge refuses
# every device-bound tool until the session calls qg_acquire_device.

def test_desktop_bridge_is_detected_by_its_lock_tools(monkeypatch):
    fake = _FakeSession(["mobile_tap", "mobile_observe_screen", "qg_acquire_device"])
    sess_mod = _patched_session(monkeypatch, fake)
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = "http://127.0.0.1:51821"
    assert asyncio.run(s.is_desktop_bridge()) is True


def test_standalone_server_is_not_mistaken_for_the_desktop_app(monkeypatch):
    fake = _FakeSession(["mobile_tap", "mobile_observe_screen", "qg_start", "qg_docs"])
    sess_mod = _patched_session(monkeypatch, fake)
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = "http://127.0.0.1:51821"
    assert asyncio.run(s.is_desktop_bridge()) is False


def test_unreachable_server_is_not_reported_as_the_desktop_app(monkeypatch):
    """An unreachable server must fail the reachability check, not this one."""
    from qualgentbench import session as sess_mod
    s = sess_mod.DeviceSession("http://127.0.0.1:1")     # nothing listening
    assert asyncio.run(s.is_desktop_bridge()) is False
    assert asyncio.run(s.list_tool_names()) == set()


# ── the harness never starts a server, and its hints must not say it does ──────
#
# `--mcp-server` names a server the user runs (CLAUDE.md, README). Until QUA-2774 the
# doctor and preflight fixes told the user a run starts one itself, so a user who
# followed them waited for a server that never came.

_AUTO_START_CLAIMS = ("automatically", "on its own", "starts the right server",
                      "server itself")
_LAUNCH_LINE = "uv run devloop-mcp --transport streamable-http --port"


def _assert_names_the_users_launch_step(text: str, port: int) -> None:
    assert f"{_LAUNCH_LINE} {port}" in text, text
    assert f"--mcp-server http://127.0.0.1:{port}" in text, text
    for claim in _AUTO_START_CLAIMS:
        assert claim not in text, (claim, text)


def test_doctor_unreachable_server_fix_names_the_launch_step():
    from qualgentbench.doctor import check_mcp_bridge

    result = asyncio.run(check_mcp_bridge("http://127.0.0.1:1"))   # nothing listening
    assert result.passed is False
    assert "never starts" in result.fix
    _assert_names_the_users_launch_step(result.fix, 1)


def test_doctor_desktop_app_fix_names_the_launch_step(monkeypatch):
    fake = _FakeSession(["mobile_tap", "qg_acquire_device", "qg_release_device"])
    _patched_session(monkeypatch, fake)
    from qualgentbench.doctor import check_mcp_tools

    result = asyncio.run(check_mcp_tools("http://127.0.0.1:51821"))
    assert result.passed is False and "DESKTOP APP" in result.detail
    assert "never starts" in result.fix
    _assert_names_the_users_launch_step(result.fix, 51821)


def _app_source_check(monkeypatch, server_name, instructions):
    """check_mcp_app_source against a fake server's `initialize` result."""
    fake = _FakeSession(["mobile_tap"])

    async def initialize():
        return SimpleNamespace(serverInfo=SimpleNamespace(name=server_name),
                               instructions=instructions)

    fake.initialize = initialize
    _patched_session(monkeypatch, fake)
    from qualgentbench.doctor import check_mcp_app_source

    return asyncio.run(check_mcp_app_source("http://127.0.0.1:51831"))


def test_doctor_warns_when_devloop_is_not_in_no_source_mode(monkeypatch):
    # QUA-2787: DevLoop's default mode tells the agent to read app source it does
    # not have and answers a FAIL with a fix/rebuild/retest loop.
    result = _app_source_check(monkeypatch, "devloop-mcp",
                               "You are a QA agent testing a mobile app on a real device.")
    assert result.passed is False and result.warning is True   # advisory, not fatal
    assert "--app-source none" in result.fix
    assert "DEVLOOP_MCP_APP_SOURCE=none" in result.fix
    _assert_names_the_users_launch_step(result.fix, 51831)


def test_doctor_accepts_devloop_in_no_source_mode(monkeypatch):
    result = _app_source_check(monkeypatch, "devloop-mcp",
                               "APP SOURCE: none. This server runs in no-source mode: …")
    assert result.passed is True and result.warning is False


def test_doctor_does_not_judge_other_mcp_servers(monkeypatch):
    result = _app_source_check(monkeypatch, "some-other-server", "Anything at all.")
    assert result.passed is True and result.warning is False


def test_launch_hint_names_no_source_mode():
    from qualgentbench.cli import _mcp_server_help

    assert "--port 51821 --app-source none" in _mcp_server_help(51821)


def _preflight_problems(monkeypatch, url: str, *, status: int | None,
                        tools: list[str]) -> str:
    """Run `_preflight` against a fake server: `status` answers the plain GET
    (None = connection refused), `tools` answers tools/list."""
    import httpx

    from qualgentbench.cli import _preflight

    class _Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, _url):
            if status is None:
                raise httpx.ConnectError("refused")
            return SimpleNamespace(status_code=status)

    monkeypatch.setattr(httpx, "Client", _Client)
    sess_mod = _patched_session(monkeypatch, _FakeSession(tools))
    s = sess_mod.DeviceSession.__new__(sess_mod.DeviceSession)
    s.bridge_url = url
    with pytest.raises(Exception) as exc_info:
        asyncio.run(_preflight(s, url, "codex-cli", None, None))
    return str(exc_info.value)


def test_preflight_unreachable_server_names_the_launch_step(monkeypatch):
    text = _preflight_problems(monkeypatch, "http://127.0.0.1:51831",
                               status=None, tools=[])
    assert "MCP server is not reachable at http://127.0.0.1:51831" in text
    assert "does not start one" in text
    _assert_names_the_users_launch_step(text, 51831)


def test_preflight_desktop_app_names_the_launch_step(monkeypatch):
    text = _preflight_problems(monkeypatch, "http://127.0.0.1:51821", status=406,
                               tools=["mobile_tap", "qg_acquire_device"])
    assert "MCP DESKTOP APP is serving" in text
    _assert_names_the_users_launch_step(text, 51821)


# ── agent scratchpad tools ────────────────────────────────────────────────────

def test_agent_scratchpad_tools_are_never_blocked():
    """The checklist tools drive incremental reporting — blocking them tanks the score."""
    from qualgentbench.adapters.base import RunContext
    from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
    from qualgentbench.schemas import Condition

    ctx = RunContext(task=None, agent="claude-code", model="m",
                     condition=Condition.no_routines, trial=1, run_dir=Path("/tmp/x"),
                     mcp_server="http://x", mcp_config_path=Path("/tmp/x/mcp.json"),
                     workspace_dir=Path("/tmp/x"), isolate_mcp=True,
                     disabled_tools=["upload_recording"])
    cmd = ClaudeCodeAdapter().command("i", ctx)
    blocked = cmd[cmd.index("--disallowedTools") + 1] if "--disallowedTools" in cmd else ""
    for t in ("TodoWrite", "TaskCreate", "TaskUpdate", "Task", "Agent"):
        assert t not in blocked


def test_agent_user_drop_is_inert_off_the_image(tmp_path, monkeypatch):
    """QGB_AGENT_USER (the image's unprivileged agent user) activates only where
    the drop is possible — root on POSIX with the user existing. On a developer
    machine the episode must run unchanged, as the developer."""
    import os as _os

    monkeypatch.setenv("QGB_AGENT_USER", "agent")
    if _os.name == "posix" and _os.geteuid() == 0:
        monkeypatch.setenv("QGB_AGENT_USER", "qgb-no-such-user")
    assert base._agent_user() is None

    transcript, code = _run(_ScriptAgent("print('ok')"),
                            _context(tmp_path, timeout_sec=30.0))
    assert "ok" in transcript
    assert code == 0
