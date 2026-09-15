"""The test suite cannot reach a device.

`tests/conftest.py` installs the guard; its docstring has the incident (a test tapped
the only emulator while a benchmark episode was live on it). These tests pin what the
guard refuses, what it leaves alone, and that `@pytest.mark.live_device` is the only
way past it -- and that even then the test is skipped unless QGB_LIVE_DEVICE=1.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import describe_adb_spawn

# ── in-process spawns ─────────────────────────────────────────────────────────

def test_subprocess_run_of_adb_is_refused_and_names_the_test(device_guard):
    with pytest.raises(device_guard.Violation) as info:
        subprocess.run(["adb", "devices"], capture_output=True)
    msg = str(info.value)
    assert "test_device_guard.py::test_subprocess_run_of_adb_is_refused_and_names_the_test" in msg
    assert "adb devices" in msg
    assert "live_device" in msg          # the message says how to opt in


def test_every_subprocess_helper_is_covered(device_guard):
    with pytest.raises(device_guard.Violation):
        subprocess.Popen(["adb", "shell", "input", "tap", "1", "2"])
    with pytest.raises(device_guard.Violation):
        subprocess.check_output(["adb", "devices"])
    with pytest.raises(device_guard.Violation):
        subprocess.run("adb -s emulator-5554 shell input tap 1 2", shell=True)
    with pytest.raises(device_guard.Violation):
        subprocess.run(["sh", "-c", "sleep 0 && adb devices"])
    with pytest.raises(device_guard.Violation):
        os.system("adb devices")
    with pytest.raises(device_guard.Violation):
        os.popen("adb devices")


def test_the_configured_adb_binary_is_refused_too(device_guard, monkeypatch):
    """In Docker adb is reached through a wrapper named by QGB_ADB_PATH."""
    monkeypatch.setenv("QGB_ADB_PATH", "/opt/tunnel/adb-host")
    with pytest.raises(device_guard.Violation):
        subprocess.run(["/opt/tunnel/adb-host", "devices"])


def test_the_violation_cannot_be_swallowed_by_the_code_under_test(device_guard):
    """Production wrappers catch `OSError` and even `Exception` around adb and fall
    back to "no device". The guard must not be one more thing they can hide."""
    assert not issubclass(device_guard.Violation, Exception)
    assert issubclass(device_guard.Violation, BaseException)


async def test_async_spawns_of_adb_are_refused(device_guard):
    with pytest.raises(device_guard.Violation):
        await asyncio.create_subprocess_exec("adb", "devices",
                                             stdout=asyncio.subprocess.PIPE)
    with pytest.raises(device_guard.Violation):
        await asyncio.create_subprocess_shell("adb -s x shell am start -n a/.B")


def test_the_adb_server_socket_is_refused(device_guard):
    """uiautomator2/adbutils never spawn the binary: they speak to the server on 5037."""
    with socket.socket() as s, pytest.raises(device_guard.Violation):
        s.connect(("127.0.0.1", 5037))


# ── what is left alone ────────────────────────────────────────────────────────

def test_non_adb_spawns_still_work():
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok"
    # `adb` as an ARGUMENT is not a spawn of adb.
    out = subprocess.run(["sh", "-c", "echo adb devices"], capture_output=True, text=True)
    assert out.stdout.strip() == "adb devices"
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0


async def test_non_adb_async_spawns_still_work():
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('ok')",
                                                stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    assert out.strip() == b"ok"


@pytest.mark.parametrize("args,shell,refused", [
    (["adb", "devices"], False, True),
    (["adb.exe", "devices"], False, True),
    (["/opt/android/platform-tools/adb", "-s", "x", "shell", "ls"], False, True),
    ("adb devices", True, True),
    ("cd /tmp && adb shell input tap 1 2", True, True),
    (["sh", "-c", "ANDROID_SERIAL=x adb devices"], False, True),
    (["env", "ANDROID_SERIAL=x", "adb", "shell"], False, True),
    (["bash", "-c", "echo adb"], False, False),
    ("echo adb devices", True, False),
    ([sys.executable, "-c", "print('adb')"], False, False),
    (["git", "status"], False, False),
    (["gh", "pr", "view"], False, False),
    (["adbkeyboard"], False, False),          # the basename must BE adb
])
def test_only_adb_itself_is_refused(args, shell, refused):
    assert (describe_adb_spawn(args, shell=shell) is not None) is refused


# ── the opt-in, in a real sub-session ─────────────────────────────────────────
# pytester runs pytest in a subprocess with a copy of our conftest, because a test
# marked live_device is skipped in THIS session by design and cannot check itself.

_INNER = '''
import os, subprocess, sys
import pytest


def test_an_unmarked_test_is_guarded(device_guard):
    assert device_guard.active is True


def test_a_child_process_gets_the_fake_adb():
    """Out-of-process callers cannot be patched; PATH hands them a shim that logs
    and exits 125, and the fixture fails the test from that log."""
    subprocess.run([sys.executable, "-c",
                    "import subprocess; subprocess.run(['adb', 'devices'])"],
                   check=False)


def test_a_swallowed_violation_still_fails():
    try:
        subprocess.run(["adb", "devices"])
    except BaseException:
        pass        # even a bare except in the code under test cannot hide it


@pytest.mark.live_device
def test_the_marker_lifts_the_guard(device_guard):
    assert device_guard.active is False
    assert str(device_guard.shim_dir) not in os.environ["PATH"].split(os.pathsep)
'''


@pytest.fixture
def guarded_pytester(pytester):
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile(test_inner=_INNER)
    return pytester


def test_live_device_tests_are_skipped_by_default(guarded_pytester, monkeypatch):
    monkeypatch.delenv("QGB_LIVE_DEVICE", raising=False)
    result = guarded_pytester.runpytest_subprocess("-p", "no:cacheprovider", "-rs")
    # The marked test is skipped; the two that reached adb pass their call phase
    # (the code swallowed it / a child did it) and are then failed at teardown.
    result.assert_outcomes(passed=3, skipped=1, errors=2)
    out = result.stdout.str()
    assert "reached adb" in out
    assert "child process: adb devices" in out
    assert "QGB_LIVE_DEVICE=1" in out


def test_the_marker_lifts_the_guard_when_opted_in(guarded_pytester, monkeypatch):
    monkeypatch.setenv("QGB_LIVE_DEVICE", "1")
    result = guarded_pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "-k", "marker_lifts or unmarked")
    result.assert_outcomes(passed=2)
