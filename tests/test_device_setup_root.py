"""`device_setup: root: true` must earn its place, and must never write an app's files.

`root: true` runs `adb root`, which restarts adbd as root for the WHOLE device, so every
fixture step after it runs as uid 0. AnkiDroid's fixture carried it long after the one
root-only step it existed for (a LeakCanary `pm disable`, removed 2026-09-14). Its
`mkdir`/`cp` then created the collection directory in the app's EXTERNAL dir owned by
root, and on a device that had never had AnkiDroid installed the first launch failed
with `StorageAccessException: No write access to AnkiDroid directory` (QUA-2731's first
episode, QUA-2743). A device where the app had lived before already had the directory
with the right owner, which is why it survived.

These are spec lints over every registered app, so they need no device.
"""

from __future__ import annotations

import re

from qualgentbench import bugs

# A root fixture writing here creates app files owned by uid 0 that the app cannot use.
_APP_EXTERNAL = re.compile(r"/(?:sdcard|storage/emulated/\d+)/Android/(?:data|obb)/")
# What the shell uid genuinely cannot do. `/data/...` outside the shell's own
# /data/local/tmp is another uid's private storage — fossify-messages empties the
# telephony provider's database there. Extend this with a reason if a new fixture
# needs root for something else; do not drop the check.
_ROOT_ONLY = re.compile(r"(?<![\w/])/data/(?!local/tmp\b)\S+")


def _root_specs() -> list[dict]:
    return [s for s in bugs.load_apps() if (s.get("device_setup") or {}).get("root")]


def _steps(setup: dict) -> list[str]:
    return ([str(c) for c in setup.get("shell") or []]
            + [str(item.get("dest", "")) for item in setup.get("push") or []])


def test_no_root_fixture_writes_into_an_apps_external_dir():
    """The AnkiDroid shape exactly: a root fixture that creates files under
    Android/data/<pkg> leaves them owned by root, and the app cannot open them."""
    bad = {s["app"]["id"]: hits for s in _root_specs()
           if (hits := [step for step in _steps(s["device_setup"])
                        if _APP_EXTERNAL.search(step)])}
    assert not bad, (
        f"a `root: true` fixture writes into an app's external dir, so the files are "
        f"owned by root and the app cannot use them on a fresh install: {bad}")


def test_root_is_declared_only_where_a_step_needs_it():
    """`root: true` is a device-wide side effect, not documentation. Every fixture
    that asks for it must contain a step the shell uid could not run."""
    vestigial = [s["app"]["id"] for s in _root_specs()
                 if not any(_ROOT_ONLY.search(step) for step in _steps(s["device_setup"]))]
    assert not vestigial, (
        f"`root: true` with no step that needs root: {vestigial}. Drop it, or extend "
        f"_ROOT_ONLY with the step that needs it and why.")


def test_the_lints_see_the_one_fixture_that_does_need_root():
    """Guard against the lints above passing vacuously: fossify-messages empties the
    telephony provider's database, which only root can touch."""
    ids = [s["app"]["id"] for s in _root_specs()]
    assert "fossify-messages" in ids
    messages = next(s for s in _root_specs() if s["app"]["id"] == "fossify-messages")
    assert any(_ROOT_ONLY.search(step) for step in _steps(messages["device_setup"]))
    assert "ankidroid" not in ids


# ── adbd is handed back unrooted after every device_setup (QUA-2743) ────────────
#
# `adb root` restarts adbd as uid 0 for the whole device and nothing ever ran `adb
# unroot`, so once any `root: true` fixture had run, every later episode on that device
# gave its agent a root adb shell: an episode's privileges depended on which app ran
# before it. QUA-2731's board ran with adbd root from AnkiDroid's staging onward.

import asyncio  # noqa: E402

import pytest  # noqa: E402

from qualgentbench import episode_runner as er  # noqa: E402


class _Adbd:
    """One device's adbd at `episode_runner._adb`: its privilege is DEVICE state that
    `root`/`unroot` flip (answering the way adbd does) and every shell step runs under.
    `ran` records (uid, command) for each shell step."""

    def __init__(self, root: bool = False, *, fails: str | None = None):
        self.root = root
        self.fails = fails
        self.ran: list[tuple[str, str]] = []

    def uid(self) -> str:
        return "0" if self.root else "2000"

    async def __call__(self, *args: str) -> tuple[int, str]:
        argv = list(args)
        if argv[:1] == ["-s"]:
            argv = argv[2:]
        verb = argv[0]
        if verb == "root":
            if self.root:
                return 0, "adbd is already running as root\n"
            self.root = True
            return 0, "restarting adbd as root\n"
        if verb == "unroot":
            if not self.root:
                return 0, "adbd not running as root\n"
            self.root = False
            return 0, "restarting adbd as non root\n"
        if verb == "shell":
            cmd = " ".join(argv[1:])
            if cmd == "id -u":
                return 0, self.uid() + "\n"
            if "getprop persist.sys.timezone" in cmd:
                return 0, er.DEVICE_TIMEZONE + "\n"
            self.ran.append((self.uid(), cmd))
            if self.fails and self.fails in cmd:
                return 1, "Error: it broke\n"
        return 0, ""


def _adbd(monkeypatch, **kw) -> _Adbd:
    dev = _Adbd(**kw)
    monkeypatch.setattr(er, "_adb", dev)
    return dev


PURGE = "sqlite3 /data/user/0/com.android.providers.telephony/databases/mmssms.db 'delete from sms;'"


def test_a_root_fixture_runs_as_root_and_hands_the_device_back_unrooted(monkeypatch):
    """fossify-messages' shape: the purge still gets root, the next episode does not."""
    dev = _adbd(monkeypatch)
    asyncio.run(er.run_device_setup("emulator-1", {"root": True, "shell": [PURGE]}))
    assert ("0", PURGE) in dev.ran, "the fixture that declares root must still get it"
    assert not dev.root, "adbd is still root after device_setup: the agent gets a root shell"


def test_the_device_is_unrooted_even_when_the_fixture_fails(monkeypatch):
    """The error path is a path out too: a DeviceSetupError still leaves the device to
    the next episode (and, today, to this episode's agent)."""
    dev = _adbd(monkeypatch, fails="sqlite3")
    with pytest.raises(er.DeviceSetupError):
        asyncio.run(er.run_device_setup("emulator-1", {"root": True, "shell": [PURGE]}))
    assert not dev.root


def test_a_root_left_by_someone_else_is_dropped_without_any_fixture(monkeypatch):
    """Unconditional: an app with no `device_setup:` at all still hands over a shell
    uid, even when the previous episode's agent ran `adb root` itself."""
    dev = _adbd(monkeypatch, root=True)
    asyncio.run(er.run_device_setup("emulator-1", None))
    assert not dev.root


def test_a_fixture_without_root_runs_as_the_shell_user_whatever_came_before(monkeypatch):
    """AnkiDroid's shape on a device an earlier `adb root` left behind: its mkdir/cp
    into the app's external dir must run as the shell user, or the collection
    directory is owned by root and the app cannot open it."""
    mkdir = "mkdir -p /storage/emulated/0/Android/data/com.ichi2.anki.debug/files/AnkiDroid"
    dev = _adbd(monkeypatch, root=True)
    asyncio.run(er.run_device_setup("emulator-1", {"shell": [mkdir]}))
    assert [uid for uid, cmd in dev.ran if cmd == mkdir] == ["2000"]
    assert not dev.root


def test_the_privilege_is_read_back_not_assumed(monkeypatch):
    """A production build answers `adb root` with rc 0 and stays unrooted; the helper
    reports what the shell IS, never what was asked."""
    dev = _adbd(monkeypatch)

    async def refuse(*args):
        if list(args)[2:3] == ["root"]:
            return 0, "adbd cannot run as root in production builds\n"
        return await _Adbd.__call__(dev, *args)

    monkeypatch.setattr(er, "_adb", refuse)
    assert asyncio.run(er.set_adb_root("emulator-1", True)) is False
    assert asyncio.run(er.set_adb_root("emulator-1", False)) is False
