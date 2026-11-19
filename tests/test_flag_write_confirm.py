"""The bug-flags write is confirmed, never best-effort (QUA-3546).

A cloud clean-build journey episode fired two seeded faults: staging's final
`adb unroot` met a restarting adbd (`adb: unable to connect for unroot: closed`) and
returned at once, the flags write in the same second got `adb: device offline`, and
`write_bug_flags` only logged a warning. `pm clear` had wiped files/, so the app had
no flags file and its shim turned every seeded fault on. Now the write is re-sent over
a dropped transport and read back, and one that cannot be confirmed is recorded as
`staging_failed`; `set_adb_root` waits out a verb that lost the transport. No device.
"""

from __future__ import annotations

import asyncio

import pytest
from fake_flag_device import FakeFlagDevice

from qualgentbench import episode_runner as er

UNROOT_CLOSED = "adb: unable to connect for unroot: closed"


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch):
    monkeypatch.setattr(er, "_TRANSPORT_POLL_S", 0.0)
    monkeypatch.setattr(er, "_TRANSPORT_WAIT_S", 0.05)


def _ids(device: FakeFlagDevice) -> list[str]:
    return sorted(ln.strip() for ln in device.flags.read_text().splitlines()
                  if ln.strip() and not ln.strip().startswith("#"))


# ── write_bug_flags ───────────────────────────────────────────────────────────

def test_a_write_that_finds_the_device_offline_is_resent_and_confirmed(monkeypatch, tmp_path):
    """The round-1 sequence: the first run-as write is dropped, the device comes back,
    the write is re-sent and read back."""
    device = FakeFlagDevice(tmp_path, offline=1)
    monkeypatch.setattr(er, "_adb", device)
    spec = {"active_bugs": [], "version": "clean", "drift": ["d-label"]}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "staging_failed" not in spec
    assert spec["active_bugs_written"] == [] and spec["drift_written"] == ["d-label"]
    assert device.sent("sh -c") == 2 and device.sent("get-state") >= 1
    assert device.sent(" cat files/qgb_flags.txt") == 1
    assert _ids(device) == ["d-label"]


def test_a_device_that_never_comes_back_fails_staging(monkeypatch, tmp_path):
    device = FakeFlagDevice(tmp_path, offline=99, comes_back=False)
    monkeypatch.setattr(er, "_adb", device)
    spec = {"active_bugs": ["bug-1"], "type": "bug"}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "not confirmed" in spec["staging_failed"]
    assert "device offline" in spec["staging_failed"]
    assert "active_bugs_written" not in spec and "drift_written" not in spec
    assert spec["flag_nonce"] not in spec["staging_failed"]


def test_retries_are_bounded(monkeypatch, tmp_path):
    device = FakeFlagDevice(tmp_path, offline=99)
    monkeypatch.setattr(er, "_adb", device)
    spec = {"active_bugs": ["bug-1"]}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert spec["staging_failed"]
    assert device.sent("sh -c") == 1 + er._TRANSPORT_RETRIES
    assert device.sent(" cat ") == 0                  # nothing to read back


def test_a_file_with_the_wrong_ids_fails_staging(monkeypatch, tmp_path):
    """The write 'succeeds' but the file read back is not what was sent."""
    device = FakeFlagDevice(tmp_path)

    async def adb(*args):
        rc, out = await device(*args)
        if args[-1].endswith("cat files/qgb_flags.txt"):
            nonce = out.splitlines()[0]
            return rc, nonce + "\nsome-other-bug\n"
        return rc, out
    monkeypatch.setattr(er, "_adb", adb)
    spec = {"active_bugs": [], "version": "clean"}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "holds ids ['some-other-bug']" in spec["staging_failed"]
    assert "active_bugs_written" not in spec


def test_a_file_from_another_episode_fails_staging(monkeypatch, tmp_path):
    """A stale file (another nonce) is not this episode's, whatever ids it holds."""
    device = FakeFlagDevice(tmp_path)

    async def adb(*args):
        rc, out = await device(*args)
        if args[-1].endswith("cat files/qgb_flags.txt"):
            return 0, "#QGB-NONCE-stale\n"
        return rc, out
    monkeypatch.setattr(er, "_adb", adb)
    spec = {"active_bugs": [], "version": "clean"}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "nonce line does not match" in spec["staging_failed"]


def test_a_missing_file_fails_staging(monkeypatch, tmp_path):
    """The write answered rc 0 but nothing landed: the read-back fails."""
    async def adb(*args):
        if args[-1].endswith("cat files/qgb_flags.txt"):
            return 1, "cat: files/qgb_flags.txt: No such file or directory\n"
        return 0, ""
    monkeypatch.setattr(er, "_adb", adb)
    spec = {"active_bugs": ["bug-1"]}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "could not read the flags file back" in spec["staging_failed"]


def test_a_write_the_device_refused_fails_staging_without_a_resend(monkeypatch, tmp_path):
    async def adb(*args):
        if "sh -c" in args[-1]:
            return 1, "run-as: package not debuggable: com.x\n"
        return 0, ""
    monkeypatch.setattr(er, "_adb", adb)
    spec = {"active_bugs": ["bug-1"]}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "the write failed (rc 1: run-as: package not debuggable" in spec["staging_failed"]


def test_an_earlier_staging_failure_keeps_its_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "_adb", FakeFlagDevice(tmp_path, offline=99, comes_back=False))
    spec = {"active_bugs": ["bug-1"], "staging_failed": "device_setup shell step failed"}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert spec["staging_failed"] == "device_setup shell step failed"


def test_stale_success_markers_are_cleared_before_the_write(monkeypatch, tmp_path):
    """`active_bugs_written` means THIS write landed (the lab guard reads it)."""
    monkeypatch.setattr(er, "_adb", FakeFlagDevice(tmp_path, offline=99, comes_back=False))
    spec = {"active_bugs": ["bug-1"], "active_bugs_written": ["bug-1"], "drift_written": ["d"]}
    asyncio.run(er.write_bug_flags("emulator-1", "com.x", spec))
    assert "active_bugs_written" not in spec and "drift_written" not in spec


@pytest.mark.parametrize("text,problem", [
    ("", "empty or missing"),
    ("#N\nbug-1\n#N x\n", None),
    ("#N   \n  bug-1   \n#N 00\n", None),                       # the padded shape
    ("#M\nbug-1\n", "nonce line does not match"),
    ("#N\n", "holds ids []"),
    ("#N\nbug-1\nbug-2\n", "holds ids ['bug-1', 'bug-2']"),
])
def test_flag_file_problem(text, problem):
    got = er.flag_file_problem(text, "N", ["bug-1"])
    assert got is None if problem is None else problem in got


# ── set_adb_root ──────────────────────────────────────────────────────────────

def _adbd(*, drops: int, comes_back: bool = True, lands_on_restart: bool = False,
          start_uid: str = "0"):
    """`_adb` for an adbd whose root/unroot verb loses the transport `drops` times.
    `lands_on_restart`: the dropped verb still took effect (adbd restarted into it)."""
    state = {"uid": start_uid, "drops": drops}
    calls: list[str] = []

    async def fake(*args):
        cmd = " ".join(args)
        calls.append(cmd)
        if args[-1] == "get-state":
            return (0, "device\n") if comes_back else (1, "error: device offline\n")
        if args[-1] in ("root", "unroot"):
            want = "0" if args[-1] == "root" else "2000"
            if state["drops"] > 0:
                state["drops"] -= 1
                if lands_on_restart:
                    state["uid"] = want
                return 1, f"adb: unable to connect for {args[-1]}: closed\n"
            changed = state["uid"] != want
            state["uid"] = want
            return 0, ("restarting adbd as non root\n" if changed else
                       "adbd not running as root\n")
        if args[-1] == "id -u":
            return 0, state["uid"] + "\n"
        return 0, ""
    return fake, calls


def test_the_unroot_reply_is_a_transport_drop():
    assert er._transport_failed(1, UNROOT_CLOSED + "\n")
    assert er._transport_failed(1, "adb: unable to connect for root: device offline\n")


def test_an_unroot_that_lost_the_transport_is_waited_out_and_resent(monkeypatch):
    fake, calls = _adbd(drops=1)
    monkeypatch.setattr(er, "_adb", fake)
    assert asyncio.run(er.set_adb_root("emulator-1", False)) is False
    assert sum(c.endswith(" unroot") for c in calls) == 2
    assert any(c.endswith("get-state") for c in calls)


def test_an_unroot_that_landed_on_its_own_restart_is_not_resent(monkeypatch):
    fake, calls = _adbd(drops=1, lands_on_restart=True)
    monkeypatch.setattr(er, "_adb", fake)
    assert asyncio.run(er.set_adb_root("emulator-1", False)) is False
    assert sum(c.endswith(" unroot") for c in calls) == 1


def test_an_adbd_that_never_comes_back_is_unknown(monkeypatch):
    fake, calls = _adbd(drops=99, comes_back=False)
    monkeypatch.setattr(er, "_adb", fake)
    assert asyncio.run(er.set_adb_root("emulator-1", False)) is None
    assert sum(c.endswith(" unroot") for c in calls) == 1


def test_root_verb_retries_are_bounded(monkeypatch):
    fake, calls = _adbd(drops=99)
    monkeypatch.setattr(er, "_adb", fake)
    assert asyncio.run(er.set_adb_root("emulator-1", False)) is None
    assert sum(c.endswith(" unroot") for c in calls) == 1 + er._TRANSPORT_RETRIES


def test_staging_then_flags_survive_the_round_one_sequence(monkeypatch, tmp_path):
    """End to end: device_setup's final unroot loses the transport, then the flags
    write finds the device offline once; both are waited out and the file is right."""
    adbd, _calls = _adbd(drops=1)
    flags = FakeFlagDevice(tmp_path, offline=1)

    async def adb(*args):
        if len(args) >= 3 and args[-2] == "shell" and args[-1].startswith("run-as "):
            return await flags(*args)
        if "getprop" in args[-1]:
            return 0, er.DEVICE_TIMEZONE + "\n"
        return await adbd(*args)
    monkeypatch.setattr(er, "_adb", adb)

    async def _no_clock(*_a, **_kw):
        return {}
    monkeypatch.setattr(er, "pin_device_clock", _no_clock)
    spec = {"active_bugs": [], "version": "clean"}

    async def staging():
        await er.run_device_setup("emulator-1", None)
        await er.write_bug_flags("emulator-1", "com.x", spec)
    asyncio.run(staging())
    assert "staging_failed" not in spec and spec["active_bugs_written"] == []
    assert _ids(flags) == []
