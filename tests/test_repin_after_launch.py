"""Portrait is pinned AGAIN after the app under test is launched, on both paths (QUA-2734).

A `user_rotation` written while the LAUNCHER is on top does not survive the next app
launch on our android-35 emulators. QUA-2731's live-path pre-check measured it on
fossify-calendar, orgzly and medtimer. After the app was force-stopped in landscape,
the launcher read `user_rotation` 0 and staging pinned 0. The launch then brought 1
back, and the app drew landscape, still at +5 s. Both pre-launch pins, in
`replay._reset` and in `episode_runner.normalize_app_env`, are written with the
launcher on top, because `isolate_app_under_test` ends by sending HOME. So a pass or
an episode that ended with the app stopped in landscape leaked landscape into the next
one. On the derive path an INCONCLUSIVE retry masked it: five of calendar's six
rotation trials ran a landscape attempt first.

The fix is ONE helper, `replay.repin_portrait_after_launch`, called once the app is in
front on BOTH paths. On the replay path that is the route's `launch` step, which both
route executors share. On the live path it is `run_episode`, after
`session.launch_app`. The rotation fix taught this: it covered the replay path only,
and live staging kept leaking until PR #37. So every caller is pinned here, including
the hunt truth deriver's own staging launches (`scripts/derive_truth.py`, QUA-2737).

Nothing here reaches a device. As in tests/test_isolation.py, every adb front door is
replaced by one recorder that keeps a single ordered call list, and the harness code
runs for real down to it, `relaunch` included. The recorder also plays the platform
behaviour above (`_Emulator`). So each test asserts both the ORDER and what the app
DRAWS: portrait once it is launched, with a route's own `rotate` still a real 0 -> 1
change.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

from qualgentbench import bugs, corpus, truth
from qualgentbench import episode_runner as er
from qualgentbench import replay as rp
from qualgentbench.submission import Claim, Expectation, Step
from qualgentbench.verify import device as vdevice

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dj = _load_script("derive_journey")
dt = _load_script("derive_truth")     # the HUNT truth deriver (QUA-2737)

SERIAL = "emulator-5558"
APP = "com.example.app"
LAUNCHER = "com.google.android.apps.nexuslauncher"
START = f"shell am start -W -n {APP}/.MainActivity"      # what `relaunch` launches with
FRONT = "shell dumpsys activity activities"              # the foreground read
AUTO_OFF = "shell settings put system accelerometer_rotation 0"
PORTRAIT = "shell settings put system user_rotation 0"
LANDSCAPE = "shell settings put system user_rotation 1"
HOME = "shell input keyevent KEYCODE_HOME"
SETTLE = "SETTLE"                                        # wait_stable


class _Emulator:
    """One device at the adb seam: an ordered call list, plus the rotation behaviour
    QUA-2731 measured. When the launcher comes to the front it keeps the current
    `user_rotation` and reads 0. A pin written while the launcher is on top changes the
    reading but not the kept value. The next app to come to the front gets the kept
    value back. A pin written with an app in front holds."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.top = LAUNCHER
        self.user_rotation = "0"
        # What the launcher kept when it came up. "1" is the state a pass or an episode
        # leaves behind when it stops the app in landscape: launcher up, reading 0.
        self.kept: str | None = None
        # (who was in front, value before, value written): every `user_rotation` write.
        self.writes: list[tuple[str, str, str]] = []
        # A launch the platform has not finished yet: (package, foreground reads left).
        self._arriving: tuple[str, int] | None = None

    def drawn(self) -> str:
        """The rotation the screen shows. The launcher is always drawn upright."""
        return "0" if self.top == LAUNCHER else self.user_rotation

    def launch_later(self, package: str, reads: int) -> None:
        """A launch that returns before the app is up (`mobile_launch_app` may): the
        app reaches the front on the `reads`-th foreground read from now."""
        self._arriving = (package, reads)

    def _launcher_up(self) -> None:
        if self.top != LAUNCHER:
            self.kept, self.user_rotation, self.top = self.user_rotation, "0", LAUNCHER

    def _app_up(self, package: str) -> None:
        if self.top == LAUNCHER and self.kept is not None:
            self.user_rotation, self.kept = self.kept, None
        self.top = package

    def shell(self, argv: list[str]) -> str:
        cmd = " ".join(argv)
        self.calls.append(cmd)
        words = cmd.split()
        if cmd.startswith("shell settings put system user_rotation "):
            self.writes.append((self.top, self.user_rotation, words[-1]))
            self.user_rotation = words[-1]
        elif cmd == HOME:
            self._launcher_up()
        elif words[1:3] in (["am", "force-stop"], ["pm", "clear"]):
            if words[3] == self.top:
                self._launcher_up()
        elif words[1:3] == ["am", "start"]:
            self._app_up(words[-1].split("/")[0])
        elif words[1:4] == ["cmd", "package", "resolve-activity"]:
            return f"{words[-1]}/.MainActivity\n"
        elif cmd == FRONT:
            if self._arriving is not None:
                package, left = self._arriving
                self._arriving = (package, left - 1) if left > 1 else None
                if left <= 1:
                    self._app_up(package)
            return f"  mResumedActivity: ActivityRecord{{7f1 u0 {self.top}/.Main t9}}\n"
        return ""


@pytest.fixture
def emu(monkeypatch) -> _Emulator:
    """Every adb front door staging and replay use, wired to one `_Emulator`:
    `episode_runner._adb` takes the whole argv (`-s SERIAL ...`) and returns text;
    `verify.device._adb`, which `replay` and derive_journey import by name, takes the
    serial separately and returns bytes."""
    dev = _Emulator()

    async def er_adb(*args: str) -> tuple[int, str]:
        argv = list(args)
        if argv[:1] == ["-s"]:
            argv = argv[2:]
        return 0, dev.shell(argv)

    async def device_adb(serial: str, *args: str) -> tuple[int, bytes]:
        return 0, dev.shell(list(args)).encode()

    async def settle(serial: str, *a, **k) -> None:
        dev.calls.append(SETTLE)

    async def dump(serial: str, *a, **k) -> str:
        return (f'<hierarchy rotation="{dev.drawn()}"><node text="Saved" content-desc="" '
                f'clickable="true" bounds="[0,0][200,100]"/></hierarchy>')

    async def no_sleep(*a, **k) -> None:
        return None

    async def no_clock(serial: str) -> str:
        return ""                     # no crash window: crash_verdict passes through

    async def no_markers(*a, **k) -> list[str]:
        return []

    monkeypatch.setattr(er, "_adb", er_adb)
    monkeypatch.setattr(rp, "_adb", device_adb)
    monkeypatch.setattr(vdevice, "_adb", device_adb)
    for mod in (dj, dt):
        monkeypatch.setattr(mod, "_adb", device_adb)
    for mod in (rp, dj, dt):
        monkeypatch.setattr(mod, "wait_stable", settle)
    for mod in (rp, dj, vdevice):
        monkeypatch.setattr(mod, "dump_vh", dump)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(rp, "device_time", no_clock)
    monkeypatch.setattr(rp, "fired_markers", no_markers)
    monkeypatch.setattr(bugs, "load_apps", lambda: [
        {"app": {"package": "com.bench.alpha"}}, {"app": {"package": APP}}])
    return dev


def _rotation_route() -> Claim:
    """The shape of `cal-repeat-survives-rotation`: launch, then ONE rotation."""
    return Claim(area="rot-case", verdict="",
                 steps=[Step("launch"), Step("rotate", "landscape")],
                 expect=Expectation("present", "Saved"))


# ── the replay path ────────────────────────────────────────────────────────────

async def test_the_derive_path_repins_portrait_after_the_routes_launch_step(emu):
    """`derive_journey.one_pass` exactly as the corpus gate runs it: the real `_reset`,
    then the route through derive's own executor. The previous pass stopped the app in
    landscape (a `db:` oracle force-stops it), so the launcher is up and keeps 1."""
    emu.kept = "1"

    res, _ = await dj.one_pass(SERIAL, APP, _rotation_route(), [], None, None, None, None,
                               attempts=1)

    assert res.outcome == rp.HOLDS, res.detail
    assert emu.writes == [
        (LAUNCHER, "0", "0"),   # _reset's pin, under the launcher: the launch undoes it
        (APP, "1", "0"),        # the re-pin: the launch had restored landscape
        (APP, "0", "1"),        # the route's rotate, a REAL portrait -> landscape change
    ], "the pass must start portrait, so the route's own rotate really rotates"
    calls = emu.calls
    start = calls.index(START)
    # `_reset`'s pre-launch pin is kept. It is written before HOME, under the launcher.
    assert calls.index(PORTRAIT) < calls.index(HOME) < start
    # The re-pin comes AFTER the launch, once a foreground read has seen the app, then
    # the app settles, and only then does the route's rotate run.
    repin = calls.index(PORTRAIT, start)
    assert FRONT in calls[start:repin] and calls[repin - 1] == AUTO_OFF
    assert repin < calls.index(SETTLE, repin) < calls.index(LANDSCAPE)


async def test_episode_verification_repins_portrait_after_the_launch_step_too(emu):
    """`replay.run_steps` is the executor behind every replayed agent reproduction and
    `check_setup:`. It shares the `launch` step with derive's executor, so it re-pins
    the same way."""
    emu.kept = "1"

    res = await rp.run_steps(SERIAL, APP, _rotation_route().steps)

    assert res.outcome == rp.HOLDS, res.detail
    assert emu.writes == [(APP, "1", "0"), (APP, "0", "1")], (
        "the replayed route must start portrait, so its rotate really rotates")
    start = emu.calls.index(START)
    assert emu.calls.index(PORTRAIT, start) < emu.calls.index(LANDSCAPE)


@pytest.mark.parametrize("executor", ["run_steps", "run_with_dumps"])
async def test_relaunch_is_process_death_and_keeps_the_routes_orientation(emu, executor):
    """Mid-route, only `launch` re-pins. A `relaunch` after the route rotated is process
    death on a device held landscape, so the app must come back landscape. If the harness
    pinned portrait there it would add a second lifecycle event the route never asked for."""
    steps = [Step("launch"), Step("rotate", "landscape"), Step("relaunch")]

    if executor == "run_steps":
        res = await rp.run_steps(SERIAL, APP, steps)
    else:
        res, _ = await dj.run_with_dumps(SERIAL, APP, steps)

    assert res.outcome == rp.HOLDS, res.detail
    assert emu.writes[-1] == (APP, "0", "1"), (
        f"nothing may pin portrait after the route's own rotate; writes: {emu.writes!r}")
    assert emu.calls.count(START) == 2 and emu.top == APP and emu.drawn() == "1"


@pytest.mark.parametrize("executor", ["run_steps", "run_with_dumps"])
async def test_a_route_that_opens_with_relaunch_starts_upright(emu, executor):
    """A `relaunch` at step 0 is not mid-route. The route has turned nothing yet, so the
    only orientation on the device is the one the previous pass leaked, and the hunt
    brief lets a repro start from `relaunch`. It re-pins like `launch` (QUA-2738).
    Without that, the route's rotate wrote 1 -> 1: no configuration change, so a
    lifecycle defect could not fire and the claim read as does-not-reproduce."""
    emu.kept = "1"   # the previous pass stopped the app in landscape
    steps = [Step("relaunch"), Step("rotate", "landscape")]

    if executor == "run_steps":
        res = await rp.run_steps(SERIAL, APP, steps)
    else:
        res, _ = await dj.run_with_dumps(SERIAL, APP, steps)

    assert res.outcome == rp.HOLDS, res.detail
    assert emu.writes == [(APP, "1", "0"), (APP, "0", "1")], (
        "the route must start portrait, so its own rotate really rotates")
    start = emu.calls.index(START)
    repin = emu.calls.index(PORTRAIT, start)
    assert FRONT in emu.calls[start:repin] and repin < emu.calls.index(LANDSCAPE)


async def test_derive_staging_is_upright_before_check_setup_and_the_snapshot(emu, monkeypatch,
                                                                             tmp_path):
    """`derive_journey.stage` launches the app once, runs `check_setup` on that launch
    and snapshots the result, and every pass restores that snapshot. A setup route need
    not start with `launch` (fossify-calendar's opens with a tap), so this launch must be
    upright too."""
    emu.kept = "1"   # whatever ran on this emulator last stopped an app in landscape
    drawn_at_snapshot: list[str] = []

    async def snapshot(serial, bundle, path):
        emu.calls.append("SNAPSHOT")
        drawn_at_snapshot.append(emu.drawn())
        return True

    monkeypatch.setattr(rp, "snapshot", snapshot)
    suite = {"app": {"id": "demo", "package": APP},
             "exploration": {"check_setup": {"steps": [{"press": "back"}]}}}

    await dj.stage(SERIAL, suite, tmp_path)

    assert drawn_at_snapshot == ["0"], "check_setup and the snapshot ran on a landscape app"
    calls = emu.calls
    start = calls.index(START)
    assert start < calls.index(PORTRAIT, start) \
        < calls.index("shell input keyevent KEYCODE_BACK") < calls.index("SNAPSHOT")


# ── the hunt deriver, scripts/derive_truth.py (QUA-2737) ───────────────────────
#
# Hunt truth is derived by a separate script from journey truth, with its own staging
# (`derive_one`). No hunt check or check_setup rotates, and every hunt check opens with
# `launch` (QUA-2737's audit). Neither makes the leak unreachable: it is device-wide, so
# a journey rotation case that ran earlier on the same emulator is enough.

def _hunt_suite(setup: list | None) -> dict:
    """A hunt spec in the hard tier's shape. Its one check opens with `launch`, as all
    236 hunt checks do."""
    exploration: dict = {"features": [{"id": "area", "state": "ok", "check": {
        "steps": ["launch"], "expect": {"present": "Saved"}}}]}
    if setup is not None:
        exploration["check_setup"] = {"steps": setup}
    return {"app": {"id": "demo", "package": APP}, "exploration": exploration}


@pytest.fixture
def hunt(emu, monkeypatch):
    """`derive_truth.derive_one` with its spec served from memory. The snapshot and the
    per-check derivation it hands off to both record what the app DRAWS as they start."""
    drawn: dict[str, list[str]] = {"snapshot": [], "derive_app": []}

    async def snapshot(serial, bundle, path):
        emu.calls.append("SNAPSHOT")
        drawn["snapshot"].append(emu.drawn())
        return True

    async def derive_app(*_a, **_kw):
        drawn["derive_app"].append(emu.drawn())
        return []

    monkeypatch.setattr(rp, "snapshot", snapshot)
    monkeypatch.setattr(truth, "derive_app", derive_app)
    monkeypatch.setattr(corpus, "spec_path", lambda app_id: Path(f"{app_id}.yaml"))
    return drawn


@pytest.mark.parametrize("setup", [None, [{"press": "back"}]],
                         ids=["no-check_setup", "check_setup"])
async def test_hunt_derive_staging_is_upright_before_check_setup_and_the_snapshot(
        emu, hunt, monkeypatch, tmp_path, setup):
    """`derive_one` stages each app once: pm clear, device_setup, the clean flags, ONE
    launch, `check_setup` on that launch, then the snapshot every per-check pass
    restores. fossify-gallery's and fossify-calendar's setup routes open with a tap, so
    they run on this launch. An app with no setup is snapshotted straight off it. The
    last thing on this emulator stopped an app in landscape, so the launcher kept 1."""
    emu.kept = "1"
    monkeypatch.setattr(dt, "load_suite", lambda _path: _hunt_suite(setup))

    await dt.derive_one("demo", SERIAL, tmp_path)

    assert hunt["snapshot"] == ["0"] and hunt["derive_app"] == ["0"], (
        f"check_setup and the snapshot ran on a landscape app; writes: {emu.writes!r}")
    assert emu.writes == [(APP, "1", "0")], "the launch restored landscape; nothing re-pinned"
    calls = emu.calls
    start = calls.index(START)
    # AFTER the launch, once a foreground read has seen the app, auto-rotate off first;
    # then the settle, and only then the first setup step (or, with none, the snapshot).
    repin = calls.index(PORTRAIT, start)
    assert FRONT in calls[start:repin] and calls[repin - 1] == AUTO_OFF
    first = "shell input keyevent KEYCODE_BACK" if setup else "SNAPSHOT"
    assert repin < calls.index(SETTLE, repin) < calls.index(first)


async def test_hunt_derive_relaunches_upright_for_the_check_setup_retry(emu, hunt, monkeypatch,
                                                                        tmp_path):
    """A failed check_setup is retried once from a fresh install state, which is a second
    launch. No hunt setup route rotates. The `rotate` here stands in for whatever the
    failed attempt left on the device. The retry's `pm clear` stops the app with the
    launcher coming up, so the launcher keeps that rotation and the retry's launch
    restores it. `Continue` is never on screen, so both attempts fail after their rotate,
    and each rotate is a real 0 -> 1 change only if its attempt started upright."""
    emu.kept = "1"
    monkeypatch.setattr(dt, "load_suite", lambda _path: _hunt_suite(
        [{"rotate": "landscape"}, {"tap": "Continue"}]))

    assert await dt.derive_one("demo", SERIAL, tmp_path) == []

    assert emu.calls.count(START) == 2, "check_setup was not retried"
    assert emu.writes == [
        (APP, "1", "0"), (APP, "0", "1"),   # attempt 1: re-pin, then the route's rotate
        (APP, "1", "0"), (APP, "0", "1"),   # attempt 2: the retry's launch had restored 1
    ], "the retry must start upright, so its own rotate really rotates"
    assert hunt["snapshot"] == [] and hunt["derive_app"] == []   # the app was abandoned


async def test_hunt_per_check_passes_start_upright_through_the_routes_launch_step(emu,
                                                                                  monkeypatch):
    """The other half of the audit. Every hunt check opens with `launch`, and every
    per-check pass goes `truth.derive_area` -> `replay._pass` -> `_reset` -> the route.
    So the route's own `launch` re-pins each pass (QUA-2734), before the oracle reads the
    screen, on the seeded pass and the clean one alike."""
    emu.kept = "1"
    read_at: list[str] = []
    dump = rp.dump_vh

    async def reading(serial: str, *a, **k) -> str:
        read_at.append(emu.drawn())
        return await dump(serial, *a, **k)

    monkeypatch.setattr(rp, "dump_vh", reading)
    feature = _hunt_suite(None)["exploration"]["features"][0]

    got = await truth.derive_area(SERIAL, APP, feature, ["some-bug"])

    assert got is not None and got.derived == truth.OK, got
    assert read_at and set(read_at) == {"0"}, f"a pass read a landscape screen: {read_at}"
    assert emu.writes[:2] == [(LAUNCHER, "0", "0"), (APP, "1", "0")], (
        "_reset's pin is under the launcher, so the route's `launch` must re-pin")


# ── the live path ──────────────────────────────────────────────────────────────

async def test_the_live_path_repins_portrait_once_the_app_is_in_front(emu, monkeypatch,
                                                                      tmp_path):
    """`run_episode` stages, isolates and launches through the raw arm's own `relaunch`,
    with every step real down to the adb seam. The previous episode left the app
    stopped in landscape. This is QUA-2731's pre-check as a unit test."""
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    emu.kept = "1"

    class _Staged(Exception):
        """Stops the episode at its first act after staging, the episode marker."""

    def _stop(*_a, **_kw):
        raise _Staged

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def force_release(self, *_a, **_kw):
            pass

        async def check_device_available(self, *_a, **_kw):
            pass

        async def reset_app(self, device, bundle_id, platform):
            emu.shell(["shell", "pm", "clear", bundle_id])   # what the real one runs

        async def launch_app(self, device, bundle_id):
            await vdevice.relaunch(device, bundle_id)         # the raw arm's launch

        async def first_available_device(self):
            return SERIAL

    monkeypatch.setattr(er, "DeviceSession", _Session)
    monkeypatch.setattr(er, "write_episode_marker", _stop)
    task = BenchmarkTask(id="demo-case", name="t", instruction="do it", app_file_id="",
                         app_name="Demo", platform="android", bundle_id=APP,
                         bug_spec={"app_id": "demo", "mode": "journey", "active_bugs": []})
    opts = er.EpisodeOptions(agent="claude-code", model="m",
                             condition=Condition.no_routines, trial=1, mcp_server="",
                             runs_dir=tmp_path / "runs", task_type="bug_task",
                             device_serial=SERIAL)

    with pytest.raises(_Staged):
        await er.run_episode(task, opts)

    assert emu.top == APP and emu.drawn() == "0", (
        f"the agent must be handed a portrait app; user_rotation writes: {emu.writes!r}")
    calls = emu.calls
    start = calls.index(START)
    # normalize_app_env's pre-launch pin is kept, and it is the one the launch undoes.
    assert calls.index(PORTRAIT) < calls.index(HOME) < start
    assert emu.writes[0] == (LAUNCHER, "0", "0")
    # The re-pin: AFTER the launch, once the app is seen in front, auto-rotate off first.
    repin = len(calls) - 1 - calls[::-1].index(PORTRAIT)
    assert repin > start, "portrait must be pinned again AFTER session.launch_app"
    assert FRONT in calls[start:repin] and calls[repin - 1] == AUTO_OFF
    assert emu.writes[-1] == (APP, "1", "0"), "the launch had restored landscape"
    # Nothing but the settle between the re-pin and the rest of the episode.
    assert calls[repin + 1:] == [SETTLE]


# ── the helper ─────────────────────────────────────────────────────────────────

async def test_the_repin_waits_until_the_app_is_in_front(emu):
    """The MCP arm launches through `mobile_launch_app`, which can return before the app
    is up. A pin written in that window lands under the launcher and is lost when the
    app arrives, which is the original leak all over again."""
    emu.kept = "1"
    emu.launch_later(APP, reads=3)

    assert await rp.repin_portrait_after_launch(SERIAL, APP) is True

    assert emu.calls == [FRONT, FRONT, FRONT, AUTO_OFF, PORTRAIT, SETTLE]
    assert emu.writes == [(APP, "1", "0")]
    assert emu.drawn() == "0"


async def test_an_app_that_never_reaches_the_front_is_still_pinned_but_loudly(emu, caplog):
    """Pinning is harmless and cheap, so it still happens. What must not happen is
    silence, because this is the one case in which the pin may not hold."""
    with caplog.at_level(logging.WARNING, logger="qualgentbench.replay"):
        assert await rp.repin_portrait_after_launch(SERIAL, APP, timeout_s=3) is False

    assert emu.calls == [FRONT, FRONT, FRONT, AUTO_OFF, PORTRAIT, SETTLE]
    assert "not in front" in caplog.text


def test_both_paths_share_one_repin():
    """Live staging must call the SAME helper the replay `launch` step does. A copy in
    either file can lose the "app in front, THEN pin" order, as the auto-rotate order
    nearly did before PR #37 shared `_set_rotation`."""
    assert er.repin_portrait_after_launch is rp.repin_portrait_after_launch
