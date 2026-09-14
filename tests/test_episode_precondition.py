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


def _screen(*labels: str) -> str:
    nodes = "".join(
        f'<node text="{t}" content-desc="" resource-id="" clickable="true" '
        f'bounds="[0,{100 * i}][400,{100 * i + 80}]" />'
        for i, t in enumerate(labels)
    )
    return f"<hierarchy rotation=\"0\">{nodes}</hierarchy>"


def _spec(**over) -> dict:
    spec = {"mode": "journey", "app_id": "medtimer",
            "case_id": "medtimer-skip-logged-dose", "version": "seeded"}
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
    assert er.precondition_anchor("medtimer", "medtimer-skip-logged-dose") == "Ibuprofen (2.5)"
    # `launch` is skipped — the anchor is the first TAP.
    assert er.precondition_anchor("medtimer", "medtimer-rename-medicine") == "Medicine"
    assert er.precondition_anchor("medtimer", "no-such-case") == ""
    assert er.precondition_anchor("no-such-app", "whatever") == ""


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
    assert er.precondition_anchor("fossify-calendar", "cal-create-event") == "calendar_fab"
    xml = ('<hierarchy rotation="0"><node text="" content-desc="" '
           'resource-id="org.fossify.calendar/calendar_fab" clickable="true" '
           'bounds="[0,0][100,100]" /></hierarchy>')
    _dump(monkeypatch, xml)
    spec = _spec(app_id="fossify-calendar", case_id="cal-create-event")
    assert asyncio.run(er.assert_precondition("emulator-1", spec)) == "present"
    assert "staging_failed" not in spec
