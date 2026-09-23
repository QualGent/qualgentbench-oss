"""The launcher is the task directly beneath the app under test (QUA-2733).

When a seeded app crashes, Android finishes its task and resumes the task beneath it.
Staging never sent the device HOME, so that was whichever app had been used last on the
emulator. The committed truth for `cal-search-event` recorded TrustLoop's sign-in screen
as its seeded post-crash screen, while MedTimer's crash case, derived on a different
device history, recorded the launcher: same harness, same function, and device history
picked the answer. The live path had the same hole. An agent that crashed the app, or
pressed back from its root screen, landed in an unrelated app and kept testing it.

`isolate_app_under_test` is the one chokepoint both staging paths share, so the fix
lives there: HOME is its final action. The function is pinned here, and so is each
CALLER. The first rotation fix covered only the replay path, and live staging kept
leaking until PR #37 fixed it separately.

Nothing here reaches a device. Every adb front door that staging uses is replaced by
one recorder that keeps a single ordered call list, and the ordering assertions read it.
"""

from __future__ import annotations

import pytest

from qualgentbench import bugs
from qualgentbench import episode_runner as er
from qualgentbench import replay as rp
from qualgentbench.verify import device as vdevice

HOME = "shell input keyevent KEYCODE_HOME"
APP = "com.example.app"
# A staging step that brings a NON-benchmark app forward, the role `com.trustloop`
# played on emulator-5558. Nothing in the registry names it, so the force-stops cannot
# reach it, and only HOME can put it behind the launcher.
FOREIGN_START = "am start -n com.trustloop/.MainActivity"
# What `preflight.device_state_violations` sends: reads only.
INVARIANT_READS = ("shell settings get system ", "shell id -u", "shell ps -A -o PID,ARGS",
                   "shell cmd package query-activities ", "shell dumpsys activity activities",
                   "shell date +%s")


def _registry(monkeypatch) -> None:
    """A fixed registry: two other benchmark apps, the app under test and a spec with
    no package, so the isolation steps before HOME are known exactly."""
    monkeypatch.setattr(bugs, "load_apps", lambda: [
        {"app": {"package": "com.bench.beta"}},
        {"app": {"package": APP}},
        {"app": {"package": "com.bench.alpha"}},
        {"app": {}},
    ])


def _record_adb(monkeypatch) -> list[str]:
    """One ordered call list for every adb front door staging goes through.
    `episode_runner._adb` takes the whole argv (`-s SERIAL ...`) and returns text;
    `verify.device._adb`, which `replay` imports by name, takes the serial separately
    and returns bytes. Each entry is the argv after the serial."""
    calls: list[str] = []

    async def er_adb(*args: str) -> tuple[int, str]:
        argv = list(args)
        if argv[:1] == ["-s"]:
            argv = argv[2:]
        calls.append(" ".join(argv))
        return 0, ""

    async def device_adb(serial: str, *args: str) -> tuple[int, bytes]:
        calls.append(" ".join(args))
        return 0, b""

    monkeypatch.setattr(er, "_adb", er_adb)
    monkeypatch.setattr(rp, "_adb", device_adb)
    monkeypatch.setattr(vdevice, "_adb", device_adb)
    return calls


def _all_before_home(calls: list[str], earlier: tuple[str, ...]) -> None:
    assert calls.count(HOME) == 1, f"HOME must be sent exactly once; calls: {calls!r}"
    home = calls.index(HOME)
    for call in earlier:
        assert call in calls, f"staging never ran {call!r}; calls: {calls!r}"
        assert calls.index(call) < home, (
            f"{call!r} ran AFTER the HOME, so whatever it brought forward can sit "
            f"beneath the app under test again")


async def test_isolation_sends_the_device_home_as_its_final_action(monkeypatch):
    """HOME comes LAST, after every other isolation step. The force-stops reach only
    registry apps and `kill-all` reaches only processes, so neither of them leaves the
    launcher beneath the app. HOME does, provided nothing comes after it."""
    calls = _record_adb(monkeypatch)
    _registry(monkeypatch)

    await er.isolate_app_under_test("emulator-5558", APP)

    assert calls, "isolation issued no adb call at all"
    assert calls[-1] == HOME, (
        f"isolation must END by sending the device HOME; its last call was {calls[-1]!r}")
    assert calls[:-1] == [
        "shell am force-stop com.bench.alpha",
        "shell am force-stop com.bench.beta",
        "shell am kill-all",
    ]


async def test_the_derive_path_resets_onto_the_launcher(monkeypatch):
    """`replay._reset` runs before every derive and replay pass. Whatever that staging
    brought forward, here a device_setup step that starts a foreign app, must end up
    BEHIND the launcher. So HOME comes after all of it, and the only call after HOME is
    the flags write: a `run-as` that brings no task forward. The route's own `launch`
    step comes next."""
    calls = _record_adb(monkeypatch)
    _registry(monkeypatch)

    assert await rp._reset("emulator-5558", APP, ["a-bug"],
                           device_setup={"shell": [FOREIGN_START]})

    _all_before_home(calls, (f"shell pm clear {APP}", f"shell {FOREIGN_START}",
                             "shell am force-stop com.bench.alpha", "shell am kill-all"))
    after = calls[calls.index(HOME) + 1:]
    assert len(after) == 1 and after[0].startswith(f"shell run-as {APP} ") \
        and "qgb_flags.txt" in after[0], (
            f"only the bug-flags write may follow HOME on the derive path; got {after!r}")


async def test_the_live_path_launches_the_app_straight_onto_the_launcher(monkeypatch,
                                                                           tmp_path):
    """`run_episode` stages, isolates and launches, with every staging step real down to
    the adb seam. The launch must land directly on the launcher: the last device action
    before it is HOME, and everything else staging did came earlier. That covers the
    reset, a device_setup step that starts a foreign app, and the bug flags."""
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    calls = _record_adb(monkeypatch)
    _registry(monkeypatch)

    class _Launched(Exception):
        """Stops the episode at the launch; nothing after it is under test here."""

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def force_release(self, *_a, **_kw):
            pass

        async def check_device_available(self, *_a, **_kw):
            pass

        async def reset_app(self, device, bundle_id, platform):
            calls.append(f"RESET {bundle_id}")

        async def launch_app(self, device, bundle_id):
            calls.append(f"LAUNCH {bundle_id}")
            raise _Launched

        async def first_available_device(self):
            return "emulator-5558"

    monkeypatch.setattr(er, "DeviceSession", _Session)
    task = BenchmarkTask(id="demo-case", name="t", instruction="do it", app_file_id="",
                         app_name="Demo", platform="android", bundle_id=APP,
                         bug_spec={"app_id": "demo", "mode": "journey",
                                   "active_bugs": ["a-bug"],
                                   "device_setup": {"shell": [FOREIGN_START]}})
    opts = er.EpisodeOptions(agent="claude-code", model="m",
                             condition=Condition.no_routines, trial=1, mcp_server="",
                             runs_dir=tmp_path / "runs", task_type="bug_task",
                             device_serial="emulator-5558")

    with pytest.raises(_Launched):
        await er.run_episode(task, opts)

    assert calls[-1] == f"LAUNCH {APP}"
    # Between HOME and the launch only the episode-start invariant runs (QUA-2781), and
    # it only READS: a read brings no task forward, so HOME is still the last ACTION.
    home = calls.index(HOME)
    between = calls[home + 1:-1]
    assert between, "the episode-start invariant never read the device before the launch"
    assert all(c.startswith(INVARIANT_READS) for c in between), (
        f"only read-only invariant reads may sit between HOME and the launch; got "
        f"{[c for c in between if not c.startswith(INVARIANT_READS)]!r}")
    flags = next((c for c in calls if "qgb_flags.txt" in c), None)
    assert flags is not None, "staging never wrote the bug flags"
    _all_before_home(calls, (f"RESET {APP}", f"shell {FOREIGN_START}", flags,
                             "shell am force-stop com.bench.alpha", "shell am kill-all"))
    # Staging really ran end to end: nothing between the reset and the launch was
    # stubbed out of the list this test reads.
    assert "shell am kill-all" in calls and task.bug_spec.get("active_bugs_written") == ["a-bug"]
