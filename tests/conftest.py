"""Suite-wide fixtures.

Two jobs:

1. `_isolate_env` strips `QGB_*` so the developer's `.env` cannot leak into assertions.

2. The DEVICE GUARD. In 2026-09 `uv run pytest` was run while a benchmark episode was
   live on the only emulator; a test reached adb, an app came to the foreground, and the
   episode failed its precondition. The suite must be INCAPABLE of touching a device
   unless a test opts in with `@pytest.mark.live_device` (and those are skipped unless
   `QGB_LIVE_DEVICE=1`).

   The guard is installed at import time -- before collection, because the incident's
   test probed `adb devices` at module import, not inside a test -- and intercepts at
   the lowest layers every spawn path funnels through:

   * `subprocess.Popen.__init__`: `subprocess.run/call/check_output/Popen`, `os.popen`
     AND CPython's asyncio subprocess transports all construct a Popen.
   * `asyncio.BaseEventLoop.subprocess_exec/subprocess_shell`: a clean failure before
     any transport is built, and a backstop for event loops that do not use Popen.
   * `os.system`, `os.posix_spawn[p]`, `os.exec*`: the spawns that bypass Popen.
   * `socket.socket.connect` to the adb SERVER port: uiautomator2/adbutils speak the
     adb protocol over TCP without ever spawning the binary.
   * A fake `adb` prepended to PATH: child processes (a `sys.executable` script, a
     driver written by a test) cannot be patched in-process; they get an `adb` that
     logs the argv and exits 125, and the fixture fails the test from the log.

   Only argv[0] that IS adb is refused (basename `adb`/`adb.exe`, or `QGB_ADB_PATH`),
   including `sh -c "adb ..."`; `git`, `gh`, `sys.executable`, `sleep` are untouched.
   The violation derives from BaseException on purpose: the code under test wraps adb
   in `except (OSError, Exception)` fallbacks that would otherwise swallow the guard
   and let the test pass while claiming nothing.
"""

from __future__ import annotations

import asyncio.base_events
import atexit
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

LIVE_DEVICE_ENV = "QGB_LIVE_DEVICE"
_SHIM_LOG_ENV = "_QGB_DEVICE_GUARD_LOG"
_SHIM_TEST_ENV = "_QGB_DEVICE_GUARD_TEST"
_ADB_SERVER_DEFAULT_PORT = 5037


class DeviceAccessInTest(BaseException):
    """A test (or collection) tried to reach an adb device. BaseException so that the
    production code's broad `except Exception` fallbacks cannot hide it."""


class _DeviceGuard:
    Violation = DeviceAccessInTest

    def __init__(self) -> None:
        self.active = True                 # False only inside a live_device test
        self.current: str | None = None    # nodeid of the running test
        self.attempts: list[tuple[str, str, str]] = []   # (where, via, what)
        self.shim_dir: Path | None = None
        self.shim_log: Path | None = None

    def violation(self, via: str, what: str) -> None:
        where = self.current or "<import/collection time: no test running>"
        self.attempts.append((where, via, what))
        raise DeviceAccessInTest(
            f"{via} tried to run adb during {where}:\n    {what}\n"
            "The test suite must never reach a device -- a live benchmark episode was "
            "corrupted by a test that did. Stub the adb layer instead (see "
            "tests/test_replay.py::_no_device), or mark the test "
            f"@pytest.mark.live_device and run it deliberately with {LIVE_DEVICE_ENV}=1.")


DEVICE_GUARD = _DeviceGuard()


# ── what counts as adb ────────────────────────────────────────────────────────

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_WRAPPERS = {"env", "nohup", "exec", "command", "time", "stdbuf", "caffeinate"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_SPLIT = re.compile(r"\s*(?:\|\||&&|[;|&\n()])\s*")


def _word(x: object) -> str:
    if isinstance(x, (bytes, os.PathLike)):
        return os.fsdecode(x)
    return str(x)


def _is_adb_word(word: str) -> bool:
    if not word:
        return False
    if os.path.basename(word).lower() in ("adb", "adb.exe"):
        return True
    configured = os.environ.get("QGB_ADB_PATH")
    if configured:
        if word == configured:
            return True
        try:
            return os.path.realpath(word) == os.path.realpath(configured)
        except OSError:
            return False
    return False


def _first_command(words: list[str]) -> list[str]:
    """Drop `FOO=bar`, `env`, `nohup`, `timeout 5` prefixes so the real program shows."""
    i = 0
    while i < len(words):
        w = words[i]
        base = os.path.basename(w)
        if _ENV_ASSIGN.match(w) or base in _WRAPPERS or w.startswith("-"):
            i += 1
        elif base == "timeout" and i + 1 < len(words):
            i += 2
        else:
            break
    return words[i:]


def _argv_runs_adb(argv: list[str]) -> bool:
    cmd = _first_command(argv)
    if not cmd:
        return False
    if os.path.basename(cmd[0]) in _SHELLS and "-c" in cmd[1:]:
        script = cmd[cmd.index("-c") + 1:]
        return bool(script) and _shell_runs_adb(script[0])
    return _is_adb_word(cmd[0])


def _shell_runs_adb(script: str) -> bool:
    """True if any simple command in the shell string starts with adb."""
    for segment in _SHELL_SPLIT.split(script):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if _argv_runs_adb(words):
            return True
    return False


def describe_adb_spawn(args: object, *, shell: bool = False,
                       executable: object = None) -> str | None:
    """The offending command as text if this spawn would run adb, else None."""
    if executable is not None and _is_adb_word(_word(executable)):
        return f"executable={_word(executable)!r} args={args!r}"
    if isinstance(args, (str, bytes, os.PathLike)):
        s = _word(args)
        hit = _shell_runs_adb(s) if shell else _is_adb_word(s)
        return s if hit else None
    try:
        seq = [_word(a) for a in args]
    except TypeError:
        return None
    if not seq:
        return None
    if shell:
        return seq[0] if _shell_runs_adb(seq[0]) else None
    return shlex.join(seq) if _argv_runs_adb(seq) else None


def _check(via: str, args: object, *, shell: bool = False, executable: object = None) -> None:
    if not DEVICE_GUARD.active:
        return
    what = describe_adb_spawn(args, shell=shell, executable=executable)
    if what is not None:
        DEVICE_GUARD.violation(via, what)


# ── interception ──────────────────────────────────────────────────────────────

def _install() -> None:
    if getattr(subprocess.Popen.__init__, "_qgb_device_guard", False):
        return

    # Popen positional order: args, bufsize, executable, stdin, stdout, stderr,
    # preexec_fn, close_fds, shell.
    orig_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *pargs, **kwargs):
        executable = kwargs.get("executable", pargs[1] if len(pargs) > 1 else None)
        shell = kwargs.get("shell", pargs[7] if len(pargs) > 7 else False)
        _check("subprocess.Popen", args, shell=bool(shell), executable=executable)
        return orig_popen_init(self, args, *pargs, **kwargs)

    popen_init._qgb_device_guard = True
    subprocess.Popen.__init__ = popen_init

    loop_cls = asyncio.base_events.BaseEventLoop
    orig_exec, orig_shell = loop_cls.subprocess_exec, loop_cls.subprocess_shell

    async def subprocess_exec(self, protocol_factory, program, *args, **kwargs):
        _check("asyncio.create_subprocess_exec", (program, *args),
               executable=kwargs.get("executable"))
        return await orig_exec(self, protocol_factory, program, *args, **kwargs)

    async def subprocess_shell(self, protocol_factory, cmd, **kwargs):
        _check("asyncio.create_subprocess_shell", cmd, shell=True,
               executable=kwargs.get("executable"))
        return await orig_shell(self, protocol_factory, cmd, **kwargs)

    loop_cls.subprocess_exec, loop_cls.subprocess_shell = subprocess_exec, subprocess_shell

    orig_system = os.system

    def system(command):
        _check("os.system", command, shell=True)
        return orig_system(command)

    os.system = system

    def make_spawn(orig, name):
        def spawn(path, argv, env, *a, **k):
            _check(f"os.{name}", argv, executable=path)
            return orig(path, argv, env, *a, **k)
        return spawn

    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            setattr(os, name, make_spawn(getattr(os, name), name))

    def make_exec(orig, name):
        def exec_(path, args, *a, **k):
            _check(f"os.{name}", args, executable=path)
            return orig(path, args, *a, **k)
        return exec_

    for name in ("execv", "execve", "execvp", "execvpe"):
        setattr(os, name, make_exec(getattr(os, name), name))

    # The adb SERVER socket: uiautomator2 / adbutils / a hand-rolled client.
    orig_connect = socket.socket.connect

    def connect(self, address):
        if DEVICE_GUARD.active and isinstance(address, tuple) and len(address) >= 2:
            try:
                configured = int(os.environ.get("ANDROID_ADB_SERVER_PORT") or 0)
            except ValueError:
                configured = 0
            if address[1] in (_ADB_SERVER_DEFAULT_PORT, configured or None):
                DEVICE_GUARD.violation("socket.connect (adb server)", f"{address!r}")
        return orig_connect(self, address)

    socket.socket.connect = connect

    # PATH shim for child processes.
    shim_dir = Path(tempfile.mkdtemp(prefix="qgb-device-guard-"))
    shim_log = shim_dir / "attempts.log"
    shim = shim_dir / "adb"
    shim.write_text(
        "#!/bin/sh\n"
        "# Installed by tests/conftest.py: the test suite must never reach a device.\n"
        f'printf \'%s\\t%s\\n\' "${{{_SHIM_TEST_ENV}:-<child process>}}" "adb $*" '
        f'>> "${_SHIM_LOG_ENV}"\n'
        "echo 'qgb device guard: adb is blocked while the test suite runs' >&2\n"
        "exit 125\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shim_log.touch()
    os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ[_SHIM_LOG_ENV] = str(shim_log)
    DEVICE_GUARD.shim_dir, DEVICE_GUARD.shim_log = shim_dir, shim_log
    atexit.register(shutil.rmtree, shim_dir, True)


_install()


def _path_without_shim() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(p for p in parts if p != str(DEVICE_GUARD.shim_dir))


def _shim_attempts(offset: int) -> list[str]:
    log = DEVICE_GUARD.shim_log
    if log is None or not log.exists():
        return []
    return log.read_text().splitlines()[offset:]


# ── pytest wiring ─────────────────────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_device: needs a real adb device. Lifts the device guard; skipped unless "
        f"{LIVE_DEVICE_ENV}=1.")


def pytest_collection_modifyitems(config, items):
    if os.environ.get(LIVE_DEVICE_ENV) == "1":
        return
    skip = pytest.mark.skip(
        reason=f"live-device test: set {LIVE_DEVICE_ENV}=1 to run it against an attached device")
    for item in items:
        if item.get_closest_marker("live_device"):
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call":
        item._qgb_call_passed = rep.passed


def pytest_terminal_summary(terminalreporter):
    if not DEVICE_GUARD.attempts:
        return
    terminalreporter.section("device guard: adb reached from the test suite", sep="=", red=True)
    for where, via, what in DEVICE_GUARD.attempts:
        terminalreporter.line(f"{where}\n    via {via}: {what}")


@pytest.fixture
def device_guard():
    """The guard's state, for tests about the guard itself.

    Such a test provokes refusals on purpose and asserts on them with
    `pytest.raises(device_guard.Violation)`; those attempts are discarded here so the
    autouse teardown check (and the end-of-run summary) do not report them as leaks.
    A refusal the test did NOT catch still propagates and fails it."""
    start = len(DEVICE_GUARD.attempts)
    yield DEVICE_GUARD
    del DEVICE_GUARD.attempts[start:]


@pytest.fixture(autouse=True)
def _device_guard(request, monkeypatch):
    live = request.node.get_closest_marker("live_device") is not None
    DEVICE_GUARD.current = request.node.nodeid
    start = len(DEVICE_GUARD.attempts)
    shim_offset = len(_shim_attempts(0))
    monkeypatch.setenv(_SHIM_TEST_ENV, request.node.nodeid)
    if live:
        DEVICE_GUARD.active = False
        monkeypatch.setenv("PATH", _path_without_shim())
    try:
        yield
    finally:
        DEVICE_GUARD.active = True
        DEVICE_GUARD.current = None
    if live:
        return
    late = [f"via {via}: {what}" for _, via, what in DEVICE_GUARD.attempts[start:]]
    late += [f"child process: {line.split(chr(9), 1)[-1]}" for line in _shim_attempts(shim_offset)]
    if late and getattr(request.node, "_qgb_call_passed", True):
        # Reached adb but the code under test swallowed the failure (or a child
        # process hit the PATH shim): fail loudly anyway.
        pytest.fail(f"{request.node.nodeid} reached adb:\n  " + "\n  ".join(late)
                    + "\nStub the adb layer or mark the test @pytest.mark.live_device.",
                    pytrace=False)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip QGB_* vars so the developer's .env can't leak into assertions.

    Tests that care about a value set it themselves.
    """
    for var in ("QGB_DISALLOWED_TOOLS", "QGB_MCP_SERVER", "QGB_ADB_PATH", "QGB_CACHE_DIR",
                "QGB_IMAGE_DIGEST", "QGB_STOP_AT_7D_PCT", "QGB_HELDOUT_DIR",
                "QGB_ALLOW_NO_HELDOUT", "QGB_REQUIRE_HELDOUT"):
        monkeypatch.delenv(var, raising=False)
