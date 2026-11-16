"""A dropped adb transport during `device_setup` is waited out, not fatal (QUA-3476).

On the cloud validation board one fossify-gallery episode failed staging on its first
media-scanner broadcast with adb's own `adb: device offline`: the command never reached
the device, and the next attempt of the same unit staged fine. `_stage_device_setup`
now waits for the device (`adb get-state`, bounded) and re-sends that step. A device
that stays away still fails staging, and a push that never lands now says so instead of
leaving an empty album behind a successful-looking scan. All without a device.
"""

from __future__ import annotations

import asyncio

import pytest

from qualgentbench import episode_runner as er
from qualgentbench import journey


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    monkeypatch.setattr(er, "_TRANSPORT_POLL_S", 0.0)
    monkeypatch.setattr(er, "_TRANSPORT_WAIT_S", 0.05)


def _device(*, offline_for: dict[str, int], comes_back: bool = True):
    """`_adb` where a command containing a key of `offline_for` answers `adb: device
    offline` (rc 1) that many times before it runs; `get-state` answers `device` once
    the device is back (always, or never)."""
    calls: list[str] = []
    left = dict(offline_for)

    async def fake(*args):
        cmd = " ".join(args)
        calls.append(cmd)
        if "getprop" in cmd:
            return 0, er.DEVICE_TIMEZONE + "\n"
        if cmd.endswith("get-state"):
            return (0, "device\n") if comes_back else (1, "error: device offline\n")
        for needle, n in left.items():
            if needle in cmd and n > 0:
                left[needle] = n - 1
                return 1, "adb: device offline\n"
        if "am broadcast" in cmd:
            return 0, ("Broadcasting: Intent { act=android.intent.action.MEDIA_SCANNER_SCAN_FILE }\n"
                       "Broadcast completed: result=0\n")
        return 0, ""
    return fake, calls


BROADCAST = "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/Pictures/QGB/x.png"


def test_a_dropped_transport_on_a_shell_step_is_waited_out_and_resent(monkeypatch):
    fake, calls = _device(offline_for={"am broadcast": 1})
    monkeypatch.setattr(er, "_adb", fake)
    asyncio.run(er.run_device_setup("emulator-1", {"shell": [BROADCAST]}))
    sent = [c for c in calls if "am broadcast" in c]
    assert len(sent) == 2                       # the dropped one, then the re-send
    assert any(c.endswith("get-state") for c in calls)


def test_a_device_that_stays_offline_still_fails_staging(monkeypatch):
    fake, calls = _device(offline_for={"am broadcast": 99}, comes_back=False)
    monkeypatch.setattr(er, "_adb", fake)
    with pytest.raises(er.DeviceSetupError) as exc:
        asyncio.run(er.run_device_setup("emulator-1", {"shell": [BROADCAST]}))
    assert "rc=1" in str(exc.value) and "device offline" in str(exc.value)
    assert sum("am broadcast" in c for c in calls) == 1   # never re-sent into the void


def test_retries_are_bounded(monkeypatch):
    fake, calls = _device(offline_for={"am broadcast": 99})
    monkeypatch.setattr(er, "_adb", fake)
    with pytest.raises(er.DeviceSetupError):
        asyncio.run(er.run_device_setup("emulator-1", {"shell": [BROADCAST]}))
    assert sum("am broadcast" in c for c in calls) == 1 + er._TRANSPORT_RETRIES


def test_a_push_that_never_lands_fails_staging(monkeypatch, tmp_path):
    src = tmp_path / "x.png"
    src.write_bytes(b"png")
    monkeypatch.setattr("qualgentbench.corpus.asset_path", lambda _p: src)
    fake, _ = _device(offline_for={"push": 99}, comes_back=False)
    monkeypatch.setattr(er, "_adb", fake)
    with pytest.raises(er.DeviceSetupError) as exc:
        asyncio.run(er.run_device_setup("emulator-1", {
            "push": [{"src": "assets/media/x.png", "dest": "/sdcard/Pictures/QGB/x.png"}]}))
    assert "push x.png" in str(exc.value) and "was not staged" in str(exc.value)


def test_a_push_dropped_once_is_resent(monkeypatch, tmp_path):
    src = tmp_path / "x.png"
    src.write_bytes(b"png")
    monkeypatch.setattr("qualgentbench.corpus.asset_path", lambda _p: src)
    fake, calls = _device(offline_for={"push": 1})
    monkeypatch.setattr(er, "_adb", fake)
    asyncio.run(er.run_device_setup("emulator-1", {
        "push": [{"src": "assets/media/x.png", "dest": "/sdcard/Pictures/QGB/x.png"}]}))
    assert sum(c.startswith("-s emulator-1 push") for c in calls) == 2


@pytest.mark.parametrize("out", [
    "adb: device offline", "error: device offline", "adb: device 'emulator-5554' not found",
    "error: no devices/emulators found", "adb: device still connecting", "error: closed"])
def test_adb_transport_replies_are_recognised(out):
    assert er._transport_failed(1, out + "\n")


@pytest.mark.parametrize("rc,out", [
    (0, "adb: device offline"),                                  # rc 0: the step ran
    (1, "Exception occurred while executing 'grant'"),           # the device answered
    (1, "ls: /sdcard/x: No such file or directory"),
    (1, "some output\nadb: device offline"),                     # printed by the step itself
])
def test_other_failures_are_not_transport_drops(rc, out):
    assert not er._transport_failed(rc, out)


# ── a blocked version says why its oracle was not read ─────────────────────────

def test_a_blocked_version_records_why_its_oracle_was_not_read():
    spec = {"case_id": "c", "blocking": "favorite-not-saved",
            "oracle": {"mode": "db", "expect": {"db": "x.db", "query": "select 1;", "equals": "1"}}}
    asyncio.run(er._journey_oracle("emulator-1", "com.app", spec))
    assert spec["oracle_result"] == er.BLOCKED_ORACLE_SKIP
    ok, why = journey._oracle_verdict(spec, [])
    assert ok is None
    assert why.startswith("db oracle not evaluated (not read: a blocked version")
    assert "missing" not in why


def test_a_blocked_version_with_a_screen_oracle_is_untouched():
    spec = {"case_id": "c", "blocking": "b", "oracle": {"mode": "present", "evidence": ["x"]}}
    asyncio.run(er._journey_oracle("emulator-1", "com.app", spec))
    assert "oracle_result" not in spec
