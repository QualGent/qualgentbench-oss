"""A fixture must never create an app's external dir, and `root: true` must earn its place.

AnkiDroid's fixture `mkdir -p`'d its collection directory under Android/data/<pkg> and
`cp`'d the collection into it. That tree is owned by whoever creates it and the app can
only use a tree it owns, so on a device that had never had AnkiDroid installed the first
launch failed with `StorageAccessException: No write access to AnkiDroid directory`
(QUA-2731's first episode, QUA-2743). A device where the app had lived before already
had the tree with the right owner, which is why it survived. The ticket blamed the
fixture's `root: true` (a leftover from a LeakCanary `pm disable` removed 2026-09-14);
measured on a never-installed android-35 emulator, the unrooted fixture made the tree
`shell:ext_data_rw` and failed identically. The fix lets the app create its own tree,
and the fake device below plays both starting states through the real
`run_device_setup` and the real spec. The lints need no device either.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from qualgentbench import bugs
from qualgentbench import episode_runner as er

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



def test_no_fixture_creates_an_apps_external_dir_itself():
    """Root or shell, a fixture that makes the directory owns it and the app cannot use
    it. Only the app may create its Android/data|obb tree; a fixture writes into one
    only after the app has made it (see the fake device below)."""
    bad = {s["app"]["id"]: hits for s in bugs.load_apps()
           if (hits := [step for step in _steps(s.get("device_setup") or {})
                        # a `mkdir` there, or a `push:` dest there (adb creates it)
                        if _APP_EXTERNAL.search(step)
                        and (step.lstrip().startswith("mkdir") or step.startswith("/"))])}
    assert not bad, f"a fixture creates an app's external dir itself: {bad}"


# ── AnkiDroid's fixture on a fake device: who owns the collection afterwards ────

_PKG = "com.ichi2.anki.debug"
_EXT = f"/storage/emulated/0/Android/data/{_PKG}"
_TREE = (_EXT, f"{_EXT}/files", f"{_EXT}/files/AnkiDroid")
_COLL = f"{_EXT}/files/AnkiDroid/collection.anki2"


class _ExtDevice:
    """The app's external dir at the adb seam, with the one rule that broke staging: a
    path is owned by whoever creates it, the app can only use a tree it owns, and
    writing onto an EXISTING file keeps its owner. Launching the app makes it create its
    own tree and an empty collection, or fail (StorageAccessException) and create
    nothing when any directory on the way is someone else's."""

    def __init__(self, owners: dict[str, str] | None = None):
        self.owners = dict(owners or {})          # path -> "app" | "shell"
        self.content: dict[str, str] = {p: "old" for p in self.owners if p == _COLL}
        self.launch_failed = False

    def _launch(self):
        if any(self.owners.get(d, "app") != "app" for d in _TREE):
            self.launch_failed = True
            return
        for d in _TREE:
            self.owners.setdefault(d, "app")
        if _COLL not in self.owners:
            self.owners[_COLL], self.content[_COLL] = "app", "empty"

    def run(self, cmd: str) -> tuple[int, str]:
        words = cmd.split()
        if cmd.startswith("rm -rf /"):
            gone = words[2]
            for path in [p for p in self.owners if p == gone or p.startswith(gone + "/")]:
                self.owners.pop(path)
                self.content.pop(path, None)
        elif cmd.startswith("rm -f "):
            for path in words[2:]:
                self.owners.pop(path, None)
        elif cmd.startswith("mkdir -p ") and words[2].startswith(_EXT):
            for d in _TREE:
                if words[2].startswith(d):
                    self.owners.setdefault(d, "shell")
        elif cmd.startswith("cp ") and words[2] == _COLL:
            if _COLL not in self.owners:
                if f"{_EXT}/files/AnkiDroid" not in self.owners:
                    return 1, "cp: No such file or directory\n"
                self.owners[_COLL] = "shell"
            self.content[_COLL] = "staged"
        elif cmd.startswith(f"am start -W -n {_PKG}/"):
            self._launch()
            return 0, "Status: ok\nComplete\n"
        elif "while [ ! -f " in cmd:
            path = cmd.split("while [ ! -f ", 1)[1].split(" ]", 1)[0]
            return (0, "") if path in self.owners else (1, "")
        return 0, ""

    def usable(self) -> bool:
        """What the harness's launch after staging needs: the app owns the whole tree
        and the collection it opens is the staged one."""
        return (all(self.owners.get(p) == "app" for p in (*_TREE, _COLL))
                and self.content.get(_COLL) == "staged")


def _stage_ankidroid(monkeypatch, dev: _ExtDevice) -> None:
    async def adb(*args: str) -> tuple[int, str]:
        argv = list(args)[2:] if list(args)[:1] == ["-s"] else list(args)
        if argv[:1] != ["shell"]:
            return 0, ""                                   # push / root / unroot
        cmd = " ".join(argv[1:])
        if cmd == "id -u":
            return 0, "2000\n"
        if "getprop persist.sys.timezone" in cmd:
            return 0, er.DEVICE_TIMEZONE + "\n"
        return dev.run(cmd)

    monkeypatch.setattr(er, "_adb", adb)
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == "ankidroid")
    asyncio.run(er.run_device_setup("emulator-1", suite["device_setup"]))


def test_ankidroid_stages_an_app_owned_collection_on_a_never_installed_device(monkeypatch):
    """No external dir at all: the state that failed QUA-2731's first episode."""
    dev = _ExtDevice()
    _stage_ankidroid(monkeypatch, dev)
    assert not dev.launch_failed
    assert dev.usable(), f"the app cannot open what staging left: {dev.owners}"


def test_ankidroid_repairs_a_tree_a_broken_fixture_left_behind(monkeypatch):
    """The state QUA-2743's own check left on emulator-5554: the whole tree owned by the
    shell. It must be cleared before the app's launch, not assumed away."""
    dev = _ExtDevice({p: "shell" for p in (*_TREE, _COLL)})
    _stage_ankidroid(monkeypatch, dev)
    assert not dev.launch_failed, "the app was launched onto a tree it does not own"
    assert dev.usable(), f"the app cannot open what staging left: {dev.owners}"


def test_ankidroid_restages_over_its_own_tree(monkeypatch):
    """Every replay pass re-runs device_setup over the tree the last staging left."""
    dev = _ExtDevice({p: "app" for p in (*_TREE, _COLL)})
    _stage_ankidroid(monkeypatch, dev)
    assert dev.usable(), f"the app cannot open what staging left: {dev.owners}"


# ── adbd is handed back unrooted after every device_setup (QUA-2743) ────────────
#
# `adb root` restarts adbd as uid 0 for the whole device and nothing ever ran `adb
# unroot`, so once any `root: true` fixture had run, every later episode on that device
# gave its agent a root adb shell: an episode's privileges depended on which app ran
# before it. QUA-2731's board ran with adbd root from AnkiDroid's staging onward.



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
