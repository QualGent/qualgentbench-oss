"""The agent's own `uiautomator dump` must work on the device it is handed (QUA-2741).

Android registers ONE UiAutomation client per device. uiautomator2's on-device server
holds that slot while it runs, and every other `uiautomator dump` then fails to register
(`IllegalStateException: UiAutomationService ... already registered!`) and kills itself:
exit 137. On QUA-2731's board that was all 371 of the agent's dumps, and 368 crash-buffer
rows named one registered client for five hours. The harness's own reader fell back to
uiautomator2 and recorded nothing, so the agent tested from screenshots and nobody knew
until the post-mortem (docs/final-validation-2026-09-19.md §8).

Three things are pinned here, all without a device. `_Device` plays one emulator at the
`verify.device._adb` seam, the UiAutomation slot included:
* `run_episode` stops the uiautomator2 server AFTER staging's last read and BEFORE the
  agent, so the agent's dump works; the episode's provenance says which source served
  each of the harness's own dumps (`dump_stats`) and what was stopped;
* `run` refuses a board whose device still kills the agent's dump after that stop
  (`preflight.check_agent_dump`, wired in through `cli._gate_agent_dump`), and runs that
  check only for a board with an Android app, as `run_episode` gates its stop;
* the harness's own dumps are counted by the source that actually served them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click
import pytest

from qualgentbench import cli
from qualgentbench import episode_runner as er
from qualgentbench import preflight
from qualgentbench.result import resolve_artifact_dir
from qualgentbench.verify import device as vdevice

SERIAL = "emulator-test-2741"
APP = "com.example.app"
U2_ARGS = "app_process / com.wetest.uia2.Main -p 9008"
# Not uiautomator2: a holder the handover stop must NOT touch, and the preflight must
# still refuse. Appium's server runs as an instrumentation of its own package.
APPIUM = "io.appium.uiautomator2.server"
HIERARCHY = b'<?xml version="1.0"?><hierarchy rotation="0"><node text="Home" /></hierarchy>'
U2_HIERARCHY = '<hierarchy rotation="0"><node text="Home" /></hierarchy>'


class _Device:
    """One emulator at the `verify.device._adb` seam: its processes, a few files on
    /sdcard, and the UiAutomation slot. While a uiautomator2 server (or any other
    registered client) runs, a `uiautomator dump` cannot register and dies by SIGKILL:
    exit 137 through `adb shell`, an empty answer through `exec-out`, which carries no
    exit status."""

    def __init__(self, *, u2: bool = False, foreign: bool = False,
                 unkillable: bool = False) -> None:
        self.procs: dict[str, str] = {"1": "/system/bin/init second_stage",
                                      "812": "com.android.systemui",
                                      "3001": APP}
        if u2:
            self.procs["4242"] = U2_ARGS
        if foreign:
            self.procs["5151"] = APPIUM
        self.unkillable = unkillable
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def slot_taken(self) -> bool:
        return any(a in (U2_ARGS, APPIUM) for a in self.procs.values())

    def u2_running(self) -> bool:
        return U2_ARGS in self.procs.values()

    async def adb(self, serial: str, *args: str) -> tuple[int, bytes]:
        assert serial == SERIAL
        cmd = " ".join(args)
        self.calls.append(cmd)
        if cmd == "shell ps -A -o PID,ARGS":
            rows = "\n".join(f"{pid:>5} {a}" for pid, a in self.procs.items())
            return 0, f"  PID ARGS\n{rows}\n".encode()
        if args[:3] == ("shell", "kill", "-9"):
            if not self.unkillable:
                for pid in args[3:]:
                    self.procs.pop(pid, None)
            return 0, b""
        if args[:3] == ("shell", "uiautomator", "dump"):
            if self.slot_taken():
                return 137, b""
            self.files[args[3]] = HIERARCHY
            return 0, f"UI hierchary dumped to: {args[3]}\n".encode()
        if cmd == "exec-out uiautomator dump /dev/tty":
            if self.slot_taken():
                return 0, b""
            return 0, HIERARCHY + b"UI hierchary dumped to: /dev/tty\n"
        if args[:2] == ("shell", "cat"):
            if args[2] in self.files:
                return 0, self.files[args[2]]
            return 1, f"cat: {args[2]}: No such file or directory\n".encode()
        if args[:3] == ("shell", "rm", "-f"):
            self.files.pop(args[3], None)
            return 0, b""
        return 0, b""

    def u2_dump(self, serial: str) -> str:
        """uiautomator2's own hierarchy: served by the server itself, so it works
        exactly while the server runs."""
        return U2_HIERARCHY if self.u2_running() else ""


@pytest.fixture
def device(monkeypatch):
    """Factory: wire a `_Device` into every device door the code under test uses, and
    make every wait instant. The module state is per serial and cleared around it."""

    async def no_sleep(*_a, **_k) -> None:
        return None

    def make(**kw) -> _Device:
        dev = _Device(**kw)
        monkeypatch.setattr(vdevice, "_adb", dev.adb)
        monkeypatch.setattr(vdevice, "_u2_dump", dev.u2_dump)
        return dev

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    vdevice.reset_dump_source(SERIAL)
    vdevice._U2.pop(SERIAL, None)
    vdevice._IME_PKG[SERIAL] = ""
    yield make
    vdevice.reset_dump_source(SERIAL)
    vdevice._U2.pop(SERIAL, None)
    vdevice._IME_PKG.pop(SERIAL, None)


# ── the handover: run_episode ──────────────────────────────────────────────────

class _AgentProbe:
    """Stands in for the agent: no model, it only runs an agent's own dump the moment
    it is handed the device, and records what came back."""

    name = "probe"

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.dumps: list[vdevice.AgentDump] = []

    async def run(self, instruction, context):
        self.log.append("AGENT")
        self.dumps = await vdevice.probe_agent_dump(SERIAL)
        return "", 0


def _episode(monkeypatch, tmp_path: Path, log: list[str]) -> tuple[object, object, _AgentProbe]:
    """`run_episode` with staging stubbed down to its order, the precondition reading
    the screen through the REAL `dump_vh`, and the agent replaced by `_AgentProbe`."""
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    agent = _AgentProbe(log)

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def force_release(self, *_a, **_kw):
            pass

        async def check_device_available(self, *_a, **_kw):
            pass

        async def reset_app(self, *_a, **_kw):
            pass

        async def launch_app(self, *_a, **_kw):
            log.append("LAUNCH")

        async def first_available_device(self):
            return SERIAL

    async def noop(*_a, **_kw):
        return None

    async def snapshots(*_a, **_kw):
        log.append("SNAPSHOT")

    async def precondition(serial, spec):
        # The last staging read, through the real reader: with the slot taken it is
        # the u2 fallback that answers.
        assert await vdevice.dump_vh(serial)
        log.append("PRECONDITION")
        return "present"

    async def window(serial):
        log.append("CRASH_WINDOW")
        return ""

    class _NoFrames:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    async def _no_avd(serial):
        return None

    monkeypatch.setattr(er, "DeviceSession", _Session)
    for fn in ("normalize_app_env", "wipe_shared_storage", "run_device_setup",
               "write_bug_flags", "isolate_app_under_test", "repin_portrait_after_launch",
               "_record_app_crashes", "_record_fired", "_journey_oracle",
               "check_adbd_after_agent"):
        monkeypatch.setattr(er, fn, noop)
    monkeypatch.setattr(er, "take_replay_snapshots", snapshots)
    monkeypatch.setattr(er, "assert_precondition", precondition)
    monkeypatch.setattr(er, "crash_window", window)
    monkeypatch.setattr(er, "FrameCapture", _NoFrames)
    monkeypatch.setattr(er, "get_adapter", lambda name: agent)
    monkeypatch.setattr(er, "_avd_name", _no_avd)
    async def _device_clean(*_a, **_kw):
        return True

    # The episode-start invariant reads the device (QUA-2781); tests/test_device_clock.py
    # pins it. Here the device is clean.
    monkeypatch.setattr(er, "_refuse_dirty_device", _device_clean)

    async def _no_foreground(*_a, **_kw):
        return APP

    monkeypatch.setattr(er, "foreground_package", _no_foreground)
    monkeypatch.setattr(er, "_write_evidence", lambda *a, **kw: None)

    task = BenchmarkTask(id="demo-case", name="t", instruction="do it", app_file_id="",
                         app_name="Demo", platform="android", bundle_id=APP,
                         bug_spec={"app_id": "demo", "mode": "journey", "active_bugs": []})
    opts = er.EpisodeOptions(agent="probe", model="m", condition=Condition.no_routines,
                             trial=1, mcp_server="", runs_dir=tmp_path / "runs",
                             task_type="bug_task", device_serial=SERIAL)
    return task, opts, agent


async def test_the_agent_is_handed_a_device_whose_dump_works(device, monkeypatch, tmp_path):
    """QUA-2731's board, as a unit test: a uiautomator2 server holds the slot through
    staging (the harness's reads are served by its fallback), and the agent's dump must
    still come back, because the server is stopped between staging and the agent.

    The server STARTS during staging here (the snapshot step stands in for the harness
    read that starts it): one already running when the episode begins is stopped
    before isolation (QUA-2781), so only one started after that reaches the hand-off."""
    dev = device(u2=False)
    log: list[str] = []
    task, opts, agent = _episode(monkeypatch, tmp_path, log)

    async def snapshots_start_u2(*_a, **_kw):
        log.append("SNAPSHOT")
        dev.procs["4242"] = U2_ARGS

    monkeypatch.setattr(er, "take_replay_snapshots", snapshots_start_u2)

    result = await er.run_episode(task, opts)

    assert [d.ok for d in agent.dumps] == [True, True], [d.detail for d in agent.dumps]
    kill = next(i for i, c in enumerate(dev.calls) if c.startswith("shell kill -9"))
    assert dev.calls[kill] == "shell kill -9 4242"
    # The stop comes after staging's last read and before the agent has run.
    assert log == ["LAUNCH", "SNAPSHOT", "PRECONDITION", "CRASH_WINDOW", "AGENT"]
    last_staging_dump = max(i for i, c in enumerate(dev.calls[:kill])
                            if c.startswith("shell uiautomator dump"))
    first_agent_dump = min(i for i, c in enumerate(dev.calls)
                           if c == f"shell uiautomator dump {vdevice.AGENT_DUMP_FILE}")
    assert last_staging_dump < kill < first_agent_dump
    # The artifact says how staging read the screen and what was stopped.
    run_dir = resolve_artifact_dir(opts.runs_dir, result)
    on_disk = json.loads((run_dir / "result.json").read_text())
    assert on_disk["provenance"]["dump_stats"] == {"builtin_killed": 3, "u2": 1}
    assert on_disk["provenance"]["u2_stopped"] == ["4242"]


async def test_a_device_with_no_uiautomator2_is_handed_over_untouched(device, monkeypatch,
                                                                      tmp_path):
    """Nothing to stop: no kill is sent, the harness's reads were all built-in."""
    dev = device(u2=False)
    task, opts, agent = _episode(monkeypatch, tmp_path, [])

    result = await er.run_episode(task, opts)

    assert all(d.ok for d in agent.dumps)
    assert not any(c.startswith("shell kill") for c in dev.calls)
    assert result.provenance["dump_stats"] == {"builtin": 1}
    assert result.provenance["u2_stopped"] == []


async def test_the_stop_leaves_every_other_process_alone(device):
    """Only uiautomator2's server is killed — never the app under test, SystemUI, or a
    client the harness does not own (that one is the preflight's to refuse)."""
    dev = device(u2=True, foreign=True)

    assert await vdevice.stop_u2_server(SERIAL) == ["4242"]

    assert [c for c in dev.calls if c.startswith("shell kill")] == ["shell kill -9 4242"]
    assert set(dev.procs) == {"1", "812", "3001", "5151"}


async def test_the_stop_drops_the_harness_client_too(device):
    """The cached client is closed and forgotten, and the device is no longer marked
    u2-first, so the next harness read starts from the built-in dump."""
    device(u2=True)
    closed: list[bool] = []

    class _Client:
        def stop_uiautomator(self, wait=True):
            closed.append(wait)

    vdevice._U2[SERIAL] = _Client()
    vdevice._PREFER_U2.add(SERIAL)

    await vdevice.stop_u2_server(SERIAL)

    assert closed == [False]
    assert SERIAL not in vdevice._U2 and SERIAL not in vdevice._PREFER_U2


async def test_a_server_that_survives_the_kill_is_reported_not_hidden(device, caplog):
    device(u2=True, unkillable=True)

    with caplog.at_level(logging.WARNING, logger="qualgentbench.verify.device"):
        assert await vdevice.stop_u2_server(SERIAL) == ["4242"]
    assert "survived kill -9" in caplog.text


# ── the preflight ──────────────────────────────────────────────────────────────

async def test_the_preflight_passes_once_the_harness_has_stopped_uiautomator2(device):
    """The state an agent will get: u2 is stopped first, as before every episode, and
    then both of the agent's dump forms must answer."""
    dev = device(u2=True)

    got = await preflight.check_agent_dump(SERIAL)

    assert got.passed, got.detail
    assert "stopped uiautomator2 server pid(s) 4242" in got.detail
    first_dump = next(i for i, c in enumerate(dev.calls) if "uiautomator dump" in c)
    assert dev.calls.index("shell kill -9 4242") < first_dump


async def test_the_preflight_refuses_a_device_where_the_agent_dump_is_killed(device):
    """The mutation the ticket asks for: the dump is dead (a client the harness does
    not own holds the slot), and the check must FAIL, naming the kill and how to find
    the holder — not merely pass where the dump works."""
    dev = device(foreign=True)

    got = await preflight.check_agent_dump(SERIAL)

    assert not got.passed
    assert f"`adb -s {SERIAL} shell uiautomator dump {vdevice.AGENT_DUMP_FILE}` → " \
           f"killed (exit 137)" in got.detail
    assert f"`adb -s {SERIAL} exec-out uiautomator dump /dev/tty` → no hierarchy" in got.detail
    assert "already registered" in (got.fix or "")
    # Tried twice, then refused; the foreign holder was never touched.
    assert sum(c == "exec-out uiautomator dump /dev/tty" for c in dev.calls) == 2
    assert "5151" in dev.procs


async def test_the_preflight_refuses_a_uiautomator2_server_it_cannot_stop(device):
    device(u2=True, unkillable=True)

    got = await preflight.check_agent_dump(SERIAL)

    assert not got.passed and "killed (exit 137)" in got.detail


# ── the gate in `run` ──────────────────────────────────────────────────────────

class _FakeSession:
    async def available_devices(self):
        return [SERIAL]


async def test_run_refuses_the_board_before_planning_it(device, monkeypatch, tmp_path):
    """`run` checks every resolved device before a run id, a plan or a prompt exists."""
    from qualgentbench import lanes

    device(foreign=True)

    def no_plan(*_a, **_kw):
        raise AssertionError("the board was planned on a device the agent cannot read")

    monkeypatch.setattr(lanes, "build_plan", no_plan)
    monkeypatch.setattr(lanes, "run_lanes", no_plan)

    with pytest.raises(click.ClickException) as exc:
        await cli._run_episodes(["m"], "claude-code", _FakeSession(), "", tmp_path / "runs",
                                1, mode="journey", devices=[SERIAL], plain=True, yes=True)

    assert "Cannot start the board" in exc.value.message
    assert f"Agent dump {SERIAL}" in exc.value.message
    assert "killed (exit 137)" in exc.value.message
    assert not (tmp_path / "runs").exists(), "a refused board must leave no run behind"


async def test_the_gate_lets_a_readable_device_through(device):
    device(u2=True)

    await cli._gate_agent_dump([SERIAL])      # does not raise


def _app(app_id: str, platform: str | None) -> dict:
    app = {"id": app_id, "name": app_id, "package": f"com.example.{app_id}",
           "difficulty": "easy"}
    if platform is not None:
        app["platform"] = platform
    return {"app": app}


class _Planned(Exception):
    """`run` got past the device gate and asked for a plan."""


@pytest.mark.parametrize("apps, refused", [
    # `uiautomator` is Android's tool: a board with no Android app never runs it.
    ([_app("notes-ios", "ios")], False),
    # One Android app is enough, whatever else the board holds.
    ([_app("notes-ios", "ios"), _app("notes", "android")], True),
    # No `platform:` is Android, the same default the task builders apply.
    ([_app("notes", None)], True),
], ids=["ios-only", "mixed", "unstated"])
async def test_the_gate_runs_only_for_a_board_with_android_apps(device, monkeypatch, tmp_path,
                                                               apps, refused):
    """The preflight is gated on the app's platform exactly as `run_episode` gates
    `stop_u2_server` (QUA-2771). The device's dump is dead here, so a board the gate
    checks is refused, and one it skips goes straight on to planning without a single
    `uiautomator` call. Ungated, an iOS board was refused on its first simulator."""
    from qualgentbench import lanes

    dev = device(foreign=True)

    def plan(*_a, **_kw):
        raise _Planned

    monkeypatch.setattr(cli, "_select_apps", lambda *_a, **_kw: apps)
    monkeypatch.setattr(lanes, "build_plan", plan)

    expected = click.ClickException if refused else _Planned
    with pytest.raises(expected) as exc:
        await cli._run_episodes(["m"], "claude-code", _FakeSession(), "", tmp_path / "runs",
                                1, mode="journey", devices=[SERIAL], plain=True, yes=True)

    if refused:
        assert "Cannot start the board" in exc.value.message
    else:
        assert not any("uiautomator" in c for c in dev.calls), dev.calls


def test_the_gate_reads_the_platform_every_task_is_stamped_with():
    """One app, one platform: the gate's reading of a spec must be what the task
    builders stamp on `BenchmarkTask.platform`, which `run_episode` branches on."""
    from qualgentbench import bugs, journey

    for spec in bugs.load_apps():
        tasks = [bugs.exploration_task(spec), *bugs.suite_tasks(spec),
                 *journey.journey_tasks(spec)]
        assert {t.platform for t in tasks} == {cli._app_platform(spec)}, spec["app"]["id"]


# ── the harness's own dumps, counted by what served them ──────────────────────

async def test_a_killed_builtin_dump_is_counted_and_the_u2_answer_is_credited_to_u2(device):
    device(u2=True)

    xml = await vdevice.dump_vh(SERIAL)

    assert xml == U2_HIERARCHY
    assert vdevice.dump_stats(SERIAL) == {"builtin_killed": 3, "u2": 1}


async def test_a_dump_nothing_served_is_none_not_builtin(device, monkeypatch):
    """Built-in killed three times and no u2 answer: nothing served this dump. It used
    to be counted "builtin", because `cat`'s complaint about the missing file was
    non-empty text — crediting the built-in path with exactly the dumps it lost."""
    dev = device(foreign=True)
    monkeypatch.setattr(vdevice, "_u2_dump", lambda serial: "")

    xml = await vdevice.dump_vh(SERIAL)

    assert "<hierarchy" not in xml
    assert vdevice.dump_stats(SERIAL) == {"builtin_killed": 3, "none": 1}
    assert dev.calls.count(f"shell uiautomator dump /sdcard/qgb_vh.xml") == 3


async def test_a_working_device_reads_builtin(device):
    device()

    assert "<hierarchy" in await vdevice.dump_vh(SERIAL)
    assert vdevice.dump_stats(SERIAL) == {"builtin": 1}


def test_dump_stats_since_is_one_cases_share():
    vdevice.reset_dump_source(SERIAL)
    try:
        for source in ("builtin", "builtin", "u2"):
            vdevice._count_dump(SERIAL, source)
        before = vdevice.dump_stats(SERIAL)
        for source in ("builtin", "builtin_killed", "builtin_killed"):
            vdevice._count_dump(SERIAL, source)
        assert vdevice.dump_stats_since(SERIAL, before) == {"builtin": 1, "builtin_killed": 2}
    finally:
        vdevice.reset_dump_source(SERIAL)


def test_the_server_is_found_by_its_main_class_in_ps():
    ps = ("  PID ARGS\n"
          " 4242 app_process / com.wetest.uia2.Main -p 9008\n"
          " 5151 io.appium.uiautomator2.server\n"
          " 6060 uiautomator dump /sdcard/w.xml\n"
          "  bad line\n")
    assert vdevice.u2_server_pids(ps) == ["4242"]
