"""The episode's precondition: is the world the test case assumes actually on screen
when the agent is handed the device?

A silently broken fixture used to be scored as agent failure — `medtimer-skip-logged-dose`
lost two episodes to an Ibuprofen card that staging no longer put on today's Overview.
These tests pin the three outcomes without a device: present passes through untouched,
missing sets `staging_failed` (which the journey scorer turns into `env_failure`), and an
unreadable screen leaves the episode exactly as it was.
"""

from __future__ import annotations

import asyncio

from qualgentbench import episode_runner as er
from qualgentbench import replay as rp


def _screen(*labels: str) -> str:
    nodes = "".join(
        f'<node text="{t}" content-desc="" resource-id="" clickable="true" '
        f'bounds="[0,{100 * i}][400,{100 * i + 80}]" />'
        for i, t in enumerate(labels)
    )
    return f"<hierarchy rotation=\"0\">{nodes}</hierarchy>"


def _spec(**over) -> dict:
    spec = {"mode": "journey", "app_id": "medtimer",
            "case_id": "medtimer-correct-dose-amount", "version": "seeded"}
    spec.update(over)
    return spec


def _dump(monkeypatch, *screens: str) -> list:
    """Script `dump_vh`; the last screen repeats once the script runs out (a retry of a
    genuinely missing anchor gets the same answer)."""
    seen = []

    async def fake_dump(serial, retries=3):
        seen.append(serial)
        return screens[min(len(seen) - 1, len(screens) - 1)]

    monkeypatch.setattr("qualgentbench.verify.device.dump_vh", fake_dump)
    _no_device(monkeypatch)
    return seen


def _no_device(monkeypatch) -> None:
    """No adb, no settling waits — these tests are about the decision, not the device."""
    async def no_wait(serial, timeout_s=8):
        return None

    monkeypatch.setattr(er, "wait_stable", no_wait)
    monkeypatch.setattr(er, "_PRECONDITION_SETTLE_S", 0)


def test_the_anchor_comes_from_the_cases_first_tap():
    """The spec carries no route, so the anchor is read back out of the case file."""
    assert er.precondition_anchor("medtimer", "medtimer-correct-dose-amount") == (
        "Ibuprofen (2.5)", "")
    # `launch` is skipped — the anchor is the first TAP.
    assert er.precondition_anchor("medtimer", "medtimer-add-medicine") == ("Medicine", "")
    # A `row:` beside the tap is part of the anchor (QUA-2738).
    assert er.precondition_anchor("medtimer", "medtimer-take-dose-then-medicine-list") == (
        "Reminded", "Ibuprofen (4)")
    assert er.precondition_anchor("medtimer", "no-such-case") == ("", "")
    assert er.precondition_anchor("no-such-app", "whatever") == ("", "")


# MedTimer's Overview after 08:00 with the fixture's "Ibuprofen (4)" reminder MISSING
# (its staged row fell on another device day, say) while Aspirin's 8:00 AM reminder is
# raised: a "Reminded" icon is on screen, just not in the row the route taps. The final
# review's probe (QUA-2738), verbatim.
_OVERVIEW_WITHOUT_IBUPROFEN_4 = """<hierarchy rotation="0">
  <node class="android.view.View" clickable="false" bounds="[0,690][1080,1300]">
    <node class="android.widget.Button" clickable="true" bounds="[32,733][158,859]">
      <node class="android.widget.ImageView" content-desc="Reminded" clickable="false" bounds="[53,754][137,838]"/>
    </node>
    <node class="android.view.View" clickable="true" bounds="[179,712][1049,880]">
      <node class="android.widget.TextView" text="Aspirin (2)" clickable="false" bounds="[210,800][500,853]"/>
    </node>
  </node>
</hierarchy>"""


def test_a_row_scoped_anchor_must_be_in_its_row(monkeypatch):
    """`medtimer-take-dose-then-medicine-list` taps `{tap: Reminded, row: "Ibuprofen (4)"}`
    first. Another row's "Reminded" is not the world it assumes: the route's scoped tap
    cannot run there, so the episode is an environment failure, never the agent's. Read
    unscoped, Aspirin's icon answered "present" and the episode was charged to the agent."""
    from test_replay import OVERVIEW_AFTER_8

    assert rp._candidates(_OVERVIEW_WITHOUT_IBUPROFEN_4, "Reminded"), \
        "the probe screen must show SOME row's Reminded, or this proves nothing"
    _dump(monkeypatch, _OVERVIEW_WITHOUT_IBUPROFEN_4)
    spec = _spec(case_id="medtimer-take-dose-then-medicine-list")
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "missing"
    assert ("first step taps 'Reminded' in the row of 'Ibuprofen (4)'"
            in spec["staging_failed"]), spec["staging_failed"]
    assert "Aspirin (2)" in spec["staging_failed"]

    # Both reminders raised: the scoped anchor resolves, and the episode is left alone.
    _dump(monkeypatch, OVERVIEW_AFTER_8)
    spec = _spec(case_id="medtimer-take-dose-then-medicine-list")
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "present"
    assert "staging_failed" not in spec


def test_a_present_anchor_leaves_the_episode_untouched(monkeypatch):
    seen = _dump(monkeypatch, _screen("Overview", "8:00 AM", "Ibuprofen (2.5)"))
    spec = _spec()
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "present"
    assert "staging_failed" not in spec
    assert len(seen) == 1          # one dump when the world is right


def test_a_missing_anchor_is_an_environment_failure_naming_it(monkeypatch):
    """The real 2026-09-10 failure: the seeded rows went in but the card was not on
    the Overview the agent lands on."""
    _dump(monkeypatch, _screen("Overview", "Taken", "Skipped", "Aspirin (2)"))
    spec = _spec()
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "missing"
    # The artifact has to explain itself: the anchor by name, and what was there instead.
    assert "Ibuprofen (2.5)" in spec["staging_failed"]
    assert "Aspirin (2)" in spec["staging_failed"]

    from qualgentbench import failures
    metrics = {"env_failure": bool(spec["staging_failed"])}
    assert failures.is_excluded(metrics)


def test_a_late_paint_is_not_a_failure(monkeypatch):
    """A dump can land mid-paint. Absence has to be the settled answer, or the check
    deletes healthy episodes from the board."""
    seen = _dump(monkeypatch, _screen("Overview"),
                 _screen("Overview", "Ibuprofen (2.5)"))
    spec = _spec()
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "present"
    assert "staging_failed" not in spec
    assert len(seen) == 2


def test_an_unreadable_screen_leaves_the_episode_alone(monkeypatch):
    """An empty dump is a dead read, not a missing fixture."""
    _dump(monkeypatch, "")
    spec = _spec()
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "unknown"
    assert "staging_failed" not in spec


def test_a_raising_dump_leaves_the_episode_alone(monkeypatch):
    async def boom(serial, retries=3):
        raise RuntimeError("adb died")

    monkeypatch.setattr("qualgentbench.verify.device.dump_vh", boom)
    _no_device(monkeypatch)
    spec = _spec()
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "unknown"
    assert "staging_failed" not in spec


def test_hunt_episodes_are_not_checked(monkeypatch):
    """Hunt mode has no single expected anchor — the brief names every area and the
    agent chooses its own route, so there is no precondition to assert."""
    _dump(monkeypatch, _screen("nothing relevant"))
    spec = {"mode": "explore", "app_id": "medtimer", "features": []}
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "skipped"
    assert "staging_failed" not in spec


def test_an_earlier_staging_failure_keeps_its_own_reason(monkeypatch):
    _dump(monkeypatch, _screen("nothing relevant"))
    spec = _spec(staging_failed="device_setup push source missing: assets/medtimer.db")
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "skipped"
    assert spec["staging_failed"].startswith("device_setup push source missing")


def test_a_resource_id_anchor_resolves(monkeypatch):
    """fossify-calendar's route taps `calendar_fab` — a resource-id, not a label. The
    replayer's own resolver is used, so every form of anchor it can tap counts as present."""
    assert er.precondition_anchor("fossify-calendar", "cal-create-event") == ("calendar_fab", "")
    xml = ('<hierarchy rotation="0"><node text="" content-desc="" '
           'resource-id="org.fossify.calendar/calendar_fab" clickable="true" '
           'bounds="[0,0][100,100]" /></hierarchy>')
    _dump(monkeypatch, xml)
    spec = _spec(app_id="fossify-calendar", case_id="cal-create-event")
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "present"
    assert "staging_failed" not in spec


# ── the episode ends before the agent when the precondition is missing (QUA-2743) ──

class _Adapter:
    """Records every launch; a launched agent returns an empty transcript."""

    def __init__(self):
        self.launches = 0

    async def run(self, instruction, context):
        self.launches += 1
        return "", 0


def _drive_episode(monkeypatch, tmp_path, landing_screen: str):
    """Everything `run_episode` does around the precondition, stubbed at the device
    seam; the precondition check itself is the real one, reading `landing_screen`.
    Returns (adapter, the episode coroutine, the post-agent device reads, the task)."""
    from qualgentbench import journey
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def force_release(self, *_a, **_kw): pass
        async def check_device_available(self, *_a, **_kw): pass
        async def reset_app(self, *_a, **_kw): pass
        async def launch_app(self, *_a, **_kw): pass
        async def first_available_device(self): return "emulator-1"

    async def _noop(*_a, **_kw):
        return None

    reads: list[str] = []

    def _read(name):
        async def fake(*_a, **_kw):
            reads.append(name)
            return "" if name == "foreground_package" else None
        return fake

    class _Frames:
        def __init__(self, *_a, **_kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    monkeypatch.setattr(er, "DeviceSession", _Session)
    for fn in ("normalize_app_env", "wipe_shared_storage", "run_device_setup",
               "write_bug_flags", "isolate_app_under_test", "repin_portrait_after_launch",
               "take_replay_snapshots", "_avd_name"):
        monkeypatch.setattr(er, fn, _noop)
    for fn in ("crash_window", "foreground_package", "_record_app_crashes",
               "_record_fired", "_journey_oracle"):
        monkeypatch.setattr(er, fn, _read(fn))
    monkeypatch.setattr(er, "FrameCapture", _Frames)
    adapter = _Adapter()
    monkeypatch.setattr(er, "get_adapter", lambda name: adapter)
    _dump(monkeypatch, landing_screen)

    task = BenchmarkTask(
        id="medtimer-correct-dose-amount", name="t", instruction="do it", app_file_id="",
        app_name="MedTimer", platform="android", bundle_id="com.futsch1.medtimer",
        bug_spec=_spec(active_bugs=[], expected="PASS"))
    opts = er.EpisodeOptions(
        agent="claude-code", model="claude-opus-5", condition=Condition.no_routines,
        trial=1, mcp_server="", runs_dir=tmp_path / "runs", task_type=journey.TASK_TYPE,
        verdict_fn=journey.journey_verdict, device_serial="emulator-1", app_id="medtimer")
    return adapter, er.run_episode(task, opts), reads, task


async def test_a_missing_precondition_never_launches_the_agent(monkeypatch, tmp_path):
    """QUA-2731's first episode: the precondition failed, the episode was excluded,
    and the agent ran anyway — $3.36 for an outcome discarded before it started. The
    episode must end before the agent does, with the exclusion exactly as it was."""
    from qualgentbench import failures

    adapter, episode, reads, task = _drive_episode(
        monkeypatch, tmp_path, _screen("Overview", "Taken", "Aspirin (2)"))
    result = await episode

    assert adapter.launches == 0, "the agent was launched on an excluded episode"
    # The exclusion is unchanged: the same `staging_failed` record, the same verdict.
    reason = task.bug_spec["staging_failed"]
    assert reason.startswith("precondition not met") and "Ibuprofen (2.5)" in reason
    assert result.metrics["staging_failed"] == reason
    assert result.metrics["env_failure"] is True
    assert failures.is_excluded(result.metrics)
    assert failures.exclusion_reason(result.metrics).startswith("env_failure")
    # Nothing ran, so nothing is read back off the device, and nothing was spent.
    assert reads == [], f"post-agent device reads on an agent that never ran: {reads}"
    assert result.metrics["agent_launched"] is False
    assert (result.metrics["cost_usd"], result.metrics["cost_source"]) == (0.0, "not_launched")
    # Still a scored artifact on disk, like every other excluded episode.
    assert list((tmp_path / "runs").rglob("result.json"))


async def test_a_present_precondition_launches_the_agent_once(monkeypatch, tmp_path):
    """The control: the same episode with the anchor on screen runs its agent and
    reads the device back afterwards, as it always did."""
    adapter, episode, reads, task = _drive_episode(
        monkeypatch, tmp_path, _screen("Overview", "8:00 AM", "Ibuprofen (2.5)"))
    result = await episode

    assert adapter.launches == 1
    assert "staging_failed" not in task.bug_spec
    assert "agent_launched" not in result.metrics
    assert reads[0] == "crash_window" and "_journey_oracle" in reads
