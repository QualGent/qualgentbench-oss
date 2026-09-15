"""The build smoke gate's device half, driven through a fake adb.

The crash window is the whole gate: it decided nothing when `since` was an error
string, because `adb shell date +%m-%d %H:%M:%S.000` reaches toybox as two words.
Pinned here so the argv shape cannot regress to the form that silently disabled it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_app", ROOT / "scripts" / "build_app.py")
build_app = importlib.util.module_from_spec(_spec)
sys.modules["build_app"] = build_app
_spec.loader.exec_module(build_app)

PKG = "org.example.app"
APP_CRASH = f"""\
09-14 16:37:50.142  2269  2269 E AndroidRuntime: FATAL EXCEPTION: main
09-14 16:37:50.142  2269  2269 E AndroidRuntime: Process: {PKG}, PID: 2269
09-14 16:37:50.142  2269  2269 E AndroidRuntime: java.lang.IllegalStateException: boom
09-14 16:37:50.142  2269  2269 E AndroidRuntime: \tat {PKG}.Main.onClick(Main.kt:12)
"""


class FakeAdb:
    def __init__(self, crash_text: str = "", foreground: bool = True, date: str = "09-14 16:37:40.000"):
        self.calls: list[list[str]] = []
        self.crash_text, self.foreground, self.date = crash_text, foreground, date

    def __call__(self, argv, capture_output, text, timeout):
        args = list(argv[1:])            # drop the adb binary
        if args[:2] == ["-s", "emu"]:
            args = args[2:]
        self.calls.append(args)
        out, rc = "", 0
        if args[:2] == ["shell", f"date {build_app.shlex.quote(build_app._DEVICE_DATE_FMT)}"]:
            out = self.date + "\n"
        elif args[0] == "shell" and args[1].startswith("date"):
            out, rc = 'date: Max 1 argument (see "date --help")\n', 1
        elif args[0] == "install":
            out = "Success\n"
        elif args[:2] == ["logcat", "-d"]:
            out = self.crash_text
        elif args[:4] == ["shell", "dumpsys", "activity", "activities"]:
            out = f"    topResumedActivity=ActivityRecord{{1 u0 {PKG}/.Main t5}}\n" if self.foreground else ""
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(build_app.time, "sleep", lambda s: None)
    monkeypatch.setattr(build_app, "_adb_bin", lambda: "adb")


def test_date_format_is_one_shell_word(monkeypatch, capsys):
    fake = FakeAdb()
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    assert build_app._smoke(Path("x.apk"), PKG, "emu") is True
    date_calls = [c for c in fake.calls if c[0] == "shell" and c[1].startswith("date")]
    assert len(date_calls) == 1 and len(date_calls[0]) == 2, date_calls
    since = fake.date
    logcat = next(c for c in fake.calls if c[:2] == ["logcat", "-d"])
    assert logcat[-2:] == ["-T", since]


def test_unreadable_clock_fails_closed(monkeypatch, capsys):
    """An error string as the window must not pass as 'no crashes'."""
    fake = FakeAdb(date='date: Max 1 argument (see "date --help")')
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    assert build_app._smoke(Path("x.apk"), PKG, "emu") is False
    assert "device clock" in capsys.readouterr().out


def test_app_crash_in_window_fails_with_signature(monkeypatch, capsys):
    fake = FakeAdb(crash_text=APP_CRASH, foreground=False)
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    assert build_app._smoke(Path("x.apk"), PKG, "emu") is False
    out = capsys.readouterr().out
    assert "app crash in org.example.app" in out and "signature: java.lang.IllegalStateException@" in out


def test_foreign_crash_is_reported_not_charged(monkeypatch, capsys):
    fake = FakeAdb(crash_text=APP_CRASH.replace(f"Process: {PKG}", "Process: com.other.app"))
    monkeypatch.setattr(build_app.subprocess, "run", fake)
    assert build_app._smoke(Path("x.apk"), PKG, "emu") is True
    assert "1 foreign crash(es) ignored: com.other.app" in capsys.readouterr().out
