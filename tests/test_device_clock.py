"""The device clock is pinned per episode, and a dirty device is refused at episode
start (QUA-2781).

The zone was pinned and the clock was not, so the time of day was an unpinned input to
the journey truth: `cal-switch-back-to-list`'s `absence_texts` carried the derivation
day and hour, two MedTimer fixtures flipped at device midnight and at 08:00, and a
board run a day after another met different screens. And device state leaked between
episodes three times (rotation QUA-2734, root adb QUA-2743, the UiAutomation slot
QUA-2741), each found by a contaminated board rather than a check.

Pinned here, without a device (everything at the adb seam, as in
tests/test_repin_after_launch.py):
* `pin_device_clock` sets the clock with `cmd alarm set-time` as the shell user, falls
  back to `date` under `adb root` and hands the device back unrooted, and clears the
  device-time-windowed logs the crash checks read, because the clock goes BACK;
* every staging path pins it: `run_device_setup` (live episode, both derive scripts)
  and `replay._reset` even with no `device_setup:`;
* an oracle's or a fixture's `'now'` is the DEVICE's instant, not the host's;
* `preflight.device_state_violations` names landscape, root, a running uiautomator2
  server, a foreground that is not the launcher and a clock off the pin, and
  `run_episode` turns any of them into `staging_failed` → `env_failure` with the
  agent never launched.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from qualgentbench import episode_runner as er
from qualgentbench import failures, preflight
from qualgentbench import replay as rp
from qualgentbench.verify import device as vdevice
from qualgentbench.verify import device_oracle

SERIAL = "emulator-5554"
APP = "com.example.app"
LAUNCHER = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
U2_ARGS = "app_process / com.wetest.uia2.Main -p 9008"
# 2026-09-16T10:00:00 America/Chicago (CDT, UTC-5) = 15:00:00Z.
PIN_S = 1789570800


class _Phone:
    """One device at both adb seams: its clock, rotation, adbd privilege, processes and
    foreground, plus the ordered call list. `sticky_*` model a device the harness's
    reset does NOT reach (an image that stays root through `adb unroot`, a server that
    survives `kill -9`, a rotation write that does not take)."""

    def __init__(self, *, now: int = PIN_S - 7 * 86400, set_time_works: bool = True,
                 rotation: str = "0", auto_rotate: str = "0", root: bool = False,
                 u2: bool = False, front: str = LAUNCHER, sticky_root: bool = False,
                 sticky_u2: bool = False, sticky_rotation: bool = False) -> None:
        self.now = now
        self.set_time_works = set_time_works
        self.settings = {"user_rotation": rotation, "accelerometer_rotation": auto_rotate,
                         "auto_time": "1"}
        self.root = root
        self.procs = {"1": "/system/bin/init", "4242": U2_ARGS} if u2 else {"1": "/system/bin/init"}
        self.front = front
        self.sticky_root, self.sticky_u2, self.sticky_rotation = (sticky_root, sticky_u2,
                                                                  sticky_rotation)
        self.calls: list[str] = []

    def run(self, argv: list[str]) -> str:
        cmd = " ".join(argv)
        self.calls.append(cmd)
        words = cmd.split()
        if words[:1] == ["root"]:
            self.root = True
            return "restarting adbd as root\n"
        if words[:1] == ["unroot"]:
            self.root = self.root and self.sticky_root
            return "restarting adbd as non root\n"
        if words[:1] == ["wait-for-device"]:
            return ""
        body = cmd[len("shell "):] if cmd.startswith("shell ") else cmd
        if body == "id -u":
            return "0\n" if self.root else "2000\n"
        if body == "date +%s":
            return f"{self.now}\n"
        if body.startswith("date -u "):
            return datetime.fromtimestamp(self.now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "\n"
        if body.startswith("date @"):
            if not self.root:
                return "date: cannot set date: Operation not permitted\n"
            self.now = int(body[len("date @"):])
            return ""
        if body.startswith("cmd alarm set-time "):
            if self.set_time_works:
                self.now = int(body.split()[-1]) // 1000
            return ""
        if body.startswith("settings put "):
            _, _, _, key, value = body.split()
            if not (self.sticky_rotation and key == "user_rotation"):
                self.settings[key] = value
            return ""
        if body.startswith("settings get "):
            return self.settings.get(body.split()[-1], "null") + "\n"
        if body == "ps -A -o PID,ARGS":
            return "  PID ARGS\n" + "\n".join(f"{p:>5} {a}" for p, a in self.procs.items()) + "\n"
        if body.startswith("kill -9 "):
            if not self.sticky_u2:
                for pid in body.split()[2:]:
                    self.procs.pop(pid, None)
            return ""
        if body.startswith("cmd package query-activities "):
            return ("2 activities found:\n  Activity #0:\n    priority=0 isDefault=true\n"
                    f"    {LAUNCHER}\n  Activity #1:\n    com.android.settings/.FallbackHome\n")
        if body == "dumpsys activity activities":
            return f"  topResumedActivity=ActivityRecord{{e6 u0 {self.front} t9}}\n"
        if body == "input keyevent KEYCODE_HOME":
            self.front = LAUNCHER
        return ""


@pytest.fixture
def phone(monkeypatch):
    """Factory wiring a `_Phone` into `episode_runner._adb` (whole argv, text) and
    `verify.device._adb` (serial apart, bytes), with every wait instant."""

    async def no_sleep(*_a, **_k):
        return None

    def make(**kw) -> _Phone:
        dev = _Phone(**kw)

        async def er_adb(*args: str) -> tuple[int, str]:
            argv = list(args)
            if argv[:1] == ["-s"]:
                argv = argv[2:]
            return 0, dev.run(argv)

        async def v_adb(serial: str, *args: str) -> tuple[int, bytes]:
            return 0, dev.run(list(args)).encode()

        monkeypatch.setattr(er, "_adb", er_adb)
        monkeypatch.setattr(vdevice, "_adb", v_adb)
        monkeypatch.setattr(rp, "_adb", v_adb)
        return dev

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    return make


# ── the pin ────────────────────────────────────────────────────────────────────

def test_the_default_pin_is_a_wednesday_at_ten_in_the_device_zone():
    """Clear of midnight and of MedTimer's 08:00 reminder edge, and inside the week the
    corpus was derived in. conftest strips QGB_*, so this is the default."""
    pin = er.device_clock_pin()
    assert pin.isoformat() == "2026-09-16T10:00:00-05:00"
    assert (pin.strftime("%A"), pin.hour, pin.minute) == ("Wednesday", 10, 0)
    assert int(pin.timestamp()) == PIN_S


def test_qgb_device_clock_moves_the_pin(monkeypatch):
    monkeypatch.setenv("QGB_DEVICE_CLOCK", "2026-10-01T09:30:00")
    assert er.device_clock_pin().isoformat() == "2026-10-01T09:30:00-05:00"
    monkeypatch.setenv("QGB_DEVICE_CLOCK", "2026-10-01T09:30:00+00:00")
    assert er.device_clock_pin().isoformat() == "2026-10-01T09:30:00+00:00"


async def test_the_pin_sets_the_clock_without_root_and_clears_the_windowed_logs(phone):
    """Measured on the android-35 image: `cmd alarm set-time` works as the shell user.
    Auto time goes off first; the logs the crash windows read are cleared AFTER the
    clock moves back, or the previous pass's crash would sit inside this one's window."""
    dev = phone()

    got = await er.pin_device_clock(SERIAL)

    assert got == {"pin": "2026-09-16T10:00:00-05:00", "method": "alarm set-time",
                   "device_epoch": PIN_S, "ok": True}
    calls = dev.calls
    auto_off = calls.index("shell settings put global auto_time 0")
    set_time = calls.index(f"shell cmd alarm set-time {PIN_S * 1000}")
    clear_logs = calls.index("shell logcat -b main,system,crash,events -c")
    clear_exits = calls.index("shell am clear-exit-info")
    assert auto_off < set_time < clear_logs and set_time < clear_exits
    assert "root" not in calls and not dev.root


async def test_the_fallback_sets_the_clock_under_root_and_hands_the_device_back(phone):
    """Where set-time does not take: `date` under `adb root`, then unrooted, because
    adbd's privilege outlives the command (QUA-2743)."""
    dev = phone(set_time_works=False)

    got = await er.pin_device_clock(SERIAL)

    assert got["ok"] and got["method"] == "date (root)" and dev.now == PIN_S
    calls = dev.calls
    assert calls.index("root") < calls.index(f"shell date @{PIN_S}") < calls.index("unroot")
    assert not dev.root


async def test_a_clock_that_cannot_be_pinned_is_reported_not_raised(phone, monkeypatch):
    """No set-time and no root (a production build refuses `adb root`): the pin says so
    and returns; the episode-start invariant is what refuses the device."""
    dev = phone(set_time_works=False)

    async def refuse_root(device, root):
        return False

    monkeypatch.setattr(er, "set_adb_root", refuse_root)
    got = await er.pin_device_clock(SERIAL)

    assert got["ok"] is False and got["device_epoch"] == dev.now != PIN_S


async def test_every_staging_path_pins_the_clock(phone, monkeypatch):
    """`run_device_setup` pins it with or without a fixture (the live episode and both
    derive scripts stage through it), and `replay._reset` now runs it even for a spec
    with no `device_setup:`, so every derive and replay pass starts at the pin."""
    set_time = f"shell cmd alarm set-time {PIN_S * 1000}"

    dev = phone()
    await er.run_device_setup(SERIAL, None)
    assert set_time in dev.calls and dev.now == PIN_S

    dev = phone(now=PIN_S + 3600)          # the previous pass ran an hour past the pin

    async def flags(*_a, **_k):
        return True

    async def isolate(*_a, **_k):
        return None

    async def grants(*_a, **_k):
        return 0

    monkeypatch.setattr(rp, "set_flags", flags)
    monkeypatch.setattr(er, "isolate_app_under_test", isolate)
    monkeypatch.setattr(rp, "grant_requested_permissions", grants)
    assert await rp._reset(SERIAL, APP, [])        # no device_setup
    assert set_time in dev.calls and dev.now == PIN_S


# ── 'now' is the device's ──────────────────────────────────────────────────────

def test_a_now_literal_becomes_the_devices_instant():
    sql = "select date('now','localtime'), strftime('%s','NOW'), 'nowhere', title from t"
    assert device_oracle.at_device_now(sql, "2026-09-16 15:00:05") == (
        "select date('2026-09-16 15:00:05','localtime'), strftime('%s','2026-09-16 15:00:05'),"
        " 'nowhere', title from t")
    assert device_oracle.at_device_now(sql, None) == sql


def test_the_substituted_value_keeps_every_modifier_meaning(tmp_path):
    """SQLite reads 'YYYY-MM-DD HH:MM:SS' as UTC, exactly as it reads 'now'."""
    con = sqlite3.connect(tmp_path / "x.db")
    q = ("select date('now','start of day','+1 day'), "
         "strftime('%s', datetime('now','start of day','+18 hours'))")
    fixed = device_oracle.at_device_now(q, "2026-09-16 15:00:05")
    assert con.execute(fixed).fetchone() == ("2026-09-17", str(PIN_S - 15 * 3600 + 18 * 3600))


def test_an_oracle_reads_the_device_day_not_the_hosts(monkeypatch, tmp_path):
    """The host is a week past the pin; the event was created on the device's day. The
    oracle's `date('now','localtime')` must be the device's, or the clean arm fails."""
    from test_device_setup_sql import _Device

    db = tmp_path / "events.db"
    con = sqlite3.connect(db)
    con.execute("create table events (title text, start_ts int)")
    con.execute("insert into events values ('Standup', ?)", (PIN_S + 3600,))
    con.commit()
    con.close()
    dev = _Device({"databases/events.db": db.read_bytes()})

    def adb(serial, *args, timeout=30):
        if args[:2] == ("shell", "getprop"):
            return 0, "America/Chicago\n", ""
        if args[:2] == ("shell", "date -u '+%Y-%m-%d %H:%M:%S'"):
            return 0, "2026-09-16 15:02:00\n", ""
        return dev.adb(serial, *args, timeout=timeout)

    monkeypatch.setattr(device_oracle, "_adb", adb)
    monkeypatch.setattr(device_oracle, "_adb_bytes", dev.adb_bytes)
    monkeypatch.setattr(device_oracle, "_SETTLE_S", (0, 0))
    monkeypatch.setattr(device_oracle, "_zone_cache", {})
    oracle = {"db": "events.db", "query": "select count(*) from events where title='Standup' "
              "and date(start_ts,'unixepoch','localtime')=date('now','localtime')"}

    assert device_oracle.query_db(oracle, "com.app", SERIAL) == ("1", "ok")


def test_a_fixture_stamps_the_devices_now(monkeypatch, tmp_path):
    """MedTimer's fixture stamps its dose with `strftime('%s','now')`, and the Overview
    shows TODAY's events only: stamped with the host's day, the card is not there."""
    from test_device_setup_sql import _Device, _seed_db

    dev = _Device(_seed_db(tmp_path))

    def adb(serial, *args, timeout=30):
        if args[:2] == ("shell", "date -u '+%Y-%m-%d %H:%M:%S'"):
            return 0, "2026-09-16 15:00:00\n", ""
        return dev.adb(serial, *args, timeout=timeout)

    monkeypatch.setattr(device_oracle, "_adb", adb)
    monkeypatch.setattr(device_oracle, "_adb_bytes", dev.adb_bytes)
    device_oracle.apply_sql({"package": "com.app", "db": "main.db",
                             "statements": "insert into t values (strftime('%s','now'))"},
                            SERIAL, tz="America/Chicago")
    out = tmp_path / "out.db"
    out.write_bytes(dev.files["databases/main.db"])
    rows = [r[0] for r in sqlite3.connect(out).execute("select v from t")]
    assert str(PIN_S) in rows


# ── the episode-start invariant ────────────────────────────────────────────────

async def test_a_clean_device_has_no_violations(phone):
    phone(now=PIN_S + 40)
    assert await preflight.device_state_violations(
        SERIAL, expect_launcher=True, clock_pin=er.device_clock_pin()) == []


@pytest.mark.parametrize("state, named", [
    ({"rotation": "1"}, "user_rotation=1"),
    ({"auto_rotate": "1"}, "accelerometer_rotation=1"),
    ({"root": True}, "adb shell is root (uid 0"),
    ({"u2": True}, "uiautomator2 server running (pid 4242)"),
    ({"front": f"{APP}/.MainActivity"}, "foreground is com.example.app.MainActivity, not the launcher"),
    ({"now": PIN_S + 7 * 86400}, "device clock 2026-09-23T10:00:00-05:00 is +604800 s from the pin"),
])
async def test_each_dirty_state_is_named_with_its_value(phone, state, named):
    phone(**{"now": PIN_S + 40, **state})
    bad = await preflight.device_state_violations(
        SERIAL, expect_launcher=True, clock_pin=er.device_clock_pin())
    assert len(bad) == 1 and bad[0].startswith(named), bad


async def test_the_hand_off_check_does_not_ask_for_the_launcher(phone):
    phone(now=PIN_S + 90, front=f"{APP}/.MainActivity")
    assert await preflight.device_state_violations(
        SERIAL, expect_launcher=False, clock_pin=er.device_clock_pin()) == []


# ── refused at episode start, never an agent's 0 ───────────────────────────────

class _Adapter:
    def __init__(self):
        self.launches = 0

    async def run(self, instruction, context):
        self.launches += 1
        return "", 0


def _episode(monkeypatch, tmp_path):
    """`run_episode` with the REAL staging resets for rotation, root, u2, the clock and
    isolation over the `_Phone` (the app-level steps stubbed), the REAL invariant, and
    an agent that only counts its launches."""
    from qualgentbench import journey
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    class _Session:
        def __init__(self, *_a, **_kw): pass
        async def force_release(self, *_a, **_kw): pass
        async def check_device_available(self, *_a, **_kw): pass
        async def reset_app(self, *_a, **_kw): pass
        async def launch_app(self, *_a, **_kw): pass
        async def first_available_device(self): return SERIAL

    async def noop(*_a, **_kw):
        return None

    async def env(device, bundle):
        await rp._set_rotation(device, "portrait")     # normalize_app_env's device half

    async def present(*_a, **_kw):
        return "present"

    class _Frames:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    monkeypatch.setattr(er, "DeviceSession", _Session)
    monkeypatch.setattr(er, "normalize_app_env", env)
    for fn in ("wipe_shared_storage", "write_bug_flags", "take_replay_snapshots",
               "_avd_name", "crash_window", "foreground_package", "_record_app_crashes",
               "_record_fired", "_journey_oracle"):
        monkeypatch.setattr(er, fn, noop)
    monkeypatch.setattr(er, "_write_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(er, "repin_portrait_after_launch", noop)
    monkeypatch.setattr(er, "assert_precondition", present)
    monkeypatch.setattr(er, "FrameCapture", _Frames)
    from qualgentbench import bugs
    monkeypatch.setattr(bugs, "load_apps", lambda: [{"app": {"package": APP}}])
    adapter = _Adapter()
    monkeypatch.setattr(er, "get_adapter", lambda name: adapter)
    task = BenchmarkTask(id="demo-case__clean", name="t", instruction="do it", app_file_id="",
                         app_name="Demo", platform="android", bundle_id=APP,
                         bug_spec={"app_id": "demo", "case_id": "demo-case", "mode": "journey",
                                   "version": "clean", "active_bugs": [], "expected": "PASS",
                                   "oracle": {}, "steps": [], "defects": {}})
    opts = er.EpisodeOptions(agent="claude-code", model="m", condition=Condition.no_routines,
                             trial=1, mcp_server="", runs_dir=tmp_path / "runs",
                             task_type=journey.TASK_TYPE, verdict_fn=journey.journey_verdict,
                             device_serial=SERIAL, app_id="demo")
    return adapter, task, opts


@pytest.mark.parametrize("left, named", [
    ({"rotation": "1", "sticky_rotation": True}, "user_rotation=1"),
    ({"root": True, "sticky_root": True}, "adb shell is root"),
    ({"u2": True, "sticky_u2": True}, "uiautomator2 server running (pid 4242)"),
])
async def test_a_device_left_dirty_is_refused_at_episode_start(phone, monkeypatch, tmp_path,
                                                               left, named):
    """The acceptance: a device left in landscape, rooted, or with uiautomator2 running
    — and which staging's own reset could not bring back — is refused before the agent
    launches, with the reason named, and the board excludes it."""
    phone(**left)
    adapter, task, opts = _episode(monkeypatch, tmp_path)

    result = await er.run_episode(task, opts)

    assert adapter.launches == 0, "an agent was launched on a dirty device"
    reason = task.bug_spec["staging_failed"]
    assert reason.startswith("device not clean at episode start: ") and named in reason
    assert result.metrics["env_failure"] is True and failures.is_excluded(result.metrics)
    assert (result.metrics["agent_launched"], result.metrics["cost_source"]) == (
        False, "not_launched")
    assert result.provenance["device_clock"] == "2026-09-16T10:00:00-05:00"


@pytest.mark.parametrize("left", [
    {"rotation": "1"}, {"root": True}, {"u2": True}, {"now": PIN_S - 7 * 86400},
    {"front": f"{APP}/.MainActivity"},
])
async def test_a_device_staging_can_clean_is_cleaned_not_refused(phone, monkeypatch, tmp_path,
                                                                 left):
    """The ordinary case: the previous episode left the device rotated, rooted, with
    the harness's own uiautomator2 server up, a week off the pin or in the app. Staging
    resets all of it, the invariant reads it back clean, and the agent runs."""
    dev = phone(**left)
    adapter, task, opts = _episode(monkeypatch, tmp_path)

    await er.run_episode(task, opts)

    assert "staging_failed" not in task.bug_spec, task.bug_spec.get("staging_failed")
    assert adapter.launches == 1
    assert abs(dev.now - PIN_S) < 60 and not dev.root and "4242" not in dev.procs


async def test_the_hand_off_catches_what_staging_did_after_the_launch(phone, monkeypatch,
                                                                    tmp_path):
    """A uiautomator2 server that staging's own reads started and that survived the
    hand-off stop is caught at the hand-off, not handed to the agent."""
    dev = phone()
    adapter, task, opts = _episode(monkeypatch, tmp_path)

    async def snapshots_start_sticky_u2(*_a, **_kw):
        dev.procs["777"] = U2_ARGS
        dev.sticky_u2 = True

    monkeypatch.setattr(er, "take_replay_snapshots", snapshots_start_sticky_u2)

    result = await er.run_episode(task, opts)

    assert adapter.launches == 0
    assert task.bug_spec["staging_failed"].startswith(
        "device not clean at agent hand-off: uiautomator2 server running (pid 777)")
    assert result.metrics["env_failure"] is True


def test_the_tolerance_is_five_minutes():
    assert er.CLOCK_TOLERANCE_S == 300
    assert timedelta(seconds=er.CLOCK_TOLERANCE_S) == timedelta(minutes=5)
