"""Device access for verification — plain adb over the leased serial, through the same
tunnel the runner uses (QGB_ADB_PATH). All best-effort: a failed capture yields an
empty dump, which the spec turns into a FAIL, never a crash."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

from .match import find_button, find_center

logger = logging.getLogger(__name__)

_ACT_RE = re.compile(r"([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)")
# One-shot onboarding/overlay buttons to clear after a cold launch so the landing
# list is visible. Matched as EXACT node text (see match.find_button).
_DISMISS_LABELS = ("got it", "ok", "continue", "done", "close", "dismiss",
                   "finish", "get started", "allow")


def _adb_bin() -> str:
    return os.environ.get("QGB_ADB_PATH") or "adb"


async def _adb(serial: str, *args: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        _adb_bin(), "-s", serial, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out


_U2: dict[str, object] = {}

# uiautomator2 is a hard dependency (pyproject), but the code paths stay best-effort:
# a broken install must degrade a replay, never crash it. Degrading SILENTLY is the
# bug this flag exists for — a missing import is reported once, loudly.
_U2_MISSING_WARNED = False


def u2_available() -> bool:
    try:
        import uiautomator2  # noqa: F401
        return True
    except ImportError:
        return False


def _warn_u2_missing(consequence: str) -> None:
    global _U2_MISSING_WARNED
    if not _U2_MISSING_WARNED:
        _U2_MISSING_WARNED = True
        logger.warning("uiautomator2 is not importable — %s. Run `uv sync`; "
                       "`qualgent-bench doctor` checks for this.", consequence)

# Backspaces used to clear a field when uiautomator2 is unavailable. Longer than any
# value a repro plausibly overwrites; extra deletes on an empty field are harmless.
_CLEAR_KEYS = 80
# How long to wait for a focused field before giving up on the atomic path.
_U2_FOCUS_WAIT_S = 3.0


def _u2_dump(serial: str) -> str:
    """Hierarchy via uiautomator2's own service, which does not wait for idle — some
    screens never report idle and `uiautomator dump` then fails forever, making every
    anchor look missing. Drops systemui, or the status-bar clock becomes an oracle."""
    try:
        import uiautomator2 as u2
        from xml.etree import ElementTree as ET
    except ImportError:
        _warn_u2_missing("no hierarchy fallback on screens that never report idle; "
                         "their anchors will all read as missing")
        return ""
    try:
        dev = _U2.get(serial) or u2.connect(serial)
        _U2[serial] = dev
        xml = dev.dump_hierarchy()
    except Exception:  # noqa: BLE001 — a dead u2 must not fail the whole replay
        _U2.pop(serial, None)
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    for parent in root.iter():
        for child in list(parent):
            if (child.get("package") or "").startswith("com.android.systemui"):
                parent.remove(child)
    return ET.tostring(root, encoding="unicode")


def _u2_set_focused_text(serial: str, text: str) -> bool:
    """Set the FOCUSED field's value atomically via ACTION_SET_TEXT — the same
    mechanism `mobile_type_text` uses, so a repro's `type` means the same replayed as
    written. `adb input text` appends at the cursor and diverges on pre-filled fields."""
    try:
        import uiautomator2 as u2
    except ImportError:
        _warn_u2_missing("`type` degrades from ACTION_SET_TEXT to the "
                         "clear-and-keystroke path")
        return False
    try:
        dev = _U2.get(serial) or u2.connect(serial)
        _U2[serial] = dev
        # u2's default implicit wait is 20s. When nothing is focused — the repro
        # typed without tapping a field first — that is 20s of stalling before the
        # fallback that was always going to run. Fail fast instead.
        dev.implicitly_wait(_U2_FOCUS_WAIT_S)
        field = dev(focused=True)
        field.set_text(text)
        # VERIFY, do not assume: ACTION_SET_TEXT reports success on fields that ignore
        # it, and a silently no-op `type` surfaces steps later as a missing anchor and
        # reads as the agent's fault. False here just means the keystroke path runs.
        got = (field.info or {}).get("text") or ""
        return got.strip() == text.strip()
    except Exception:  # noqa: BLE001 — falls back to clear + keystrokes
        _U2.pop(serial, None)
        return False


async def set_focused_text(serial: str, text: str) -> None:
    """`type` semantics: the field now CONTAINS `text`, whatever it held before."""
    if await asyncio.to_thread(_u2_set_focused_text, serial, text):
        return
    # No u2: cursor to the end, then delete backwards. `input keyevent` takes several
    # keycodes in one call, so this is one round trip. Ctrl+A is not available —
    # `input keyevent` cannot hold a modifier across another key.
    await _adb(serial, "shell", "input", "keyevent", "KEYCODE_MOVE_END")
    await _adb(serial, "shell", "input", "keyevent", *(["KEYCODE_DEL"] * _CLEAR_KEYS))
    await append_text(serial, text)


async def append_text(serial: str, text: str) -> None:
    """Keystroke semantics — appends at the cursor. `input text` treats %s as a space
    and cannot take a literal one."""
    await _adb(serial, "shell", "input", "text", text.replace(" ", "%s"))


_PREFER_U2: set[str] = set()

# Which source served each hierarchy dump — "builtin", "u2", or "none" (both failed) —
# plus "builtin_killed", the built-in ATTEMPTS that were SIGKILLed on the device (one
# dump can make several). A replay that ran on a degraded source must say so in its
# artifact, or a dead u2 reads as "the app had no matching element". And a built-in
# dump that is KILLED means another UiAutomation client holds the device (see
# `stop_u2_server`): the fallback kept the harness reading while every agent-side
# dump died, and until these counts reached an artifact nothing showed it (QUA-2741).
_DUMP_STATS: dict[str, dict[str, int]] = {}


def _count_dump(serial: str, source: str) -> None:
    stats = _DUMP_STATS.setdefault(serial, {})
    stats[source] = stats.get(source, 0) + 1


def dump_stats(serial: str) -> dict[str, int]:
    return dict(_DUMP_STATS.get(serial, {}))


def dump_stats_since(serial: str, before: dict[str, int]) -> dict[str, int]:
    """The dumps made since `before` (an earlier `dump_stats`), zero counts dropped —
    one case's share of a device's running totals."""
    now = dump_stats(serial)
    return {k: now[k] - before.get(k, 0) for k in now if now[k] - before.get(k, 0)}


def reset_dump_source(serial: str) -> None:
    """Forget that this device fell back to u2, and zero its dump counts. Called per app
    by the derive scripts and per episode by `run_episode`, so one app that never
    reports idle does not silently move a later app off the built-in dump, and each
    artifact's `dump_stats` counts its own dumps only."""
    _PREFER_U2.discard(serial)
    _DUMP_STATS.pop(serial, None)


# The device's active IME package, cached per serial. Its windows must be dropped from
# every hierarchy dump: keyboard chrome carries its own clickable "Back" that can
# outrank the app's, and a suggestion strip can echo typed text into a `present` oracle.
_IME_PKG: dict[str, str] = {}


async def _ime_package(serial: str) -> str:
    if serial not in _IME_PKG:
        _, out = await _adb(serial, "shell", "settings", "get", "secure",
                            "default_input_method")
        value = out.decode("utf-8", "replace").strip()
        _IME_PKG[serial] = value.split("/", 1)[0] if "/" in value else ""
    return _IME_PKG[serial]


def _drop_windows(xml: str, packages: set[str]) -> str:
    """Remove every node belonging to one of `packages`. Returns the input
    unchanged when nothing matches or it does not parse — never worse than raw."""
    packages = {p for p in packages if p}
    if not packages or not any(p in xml for p in packages):
        return xml
    from xml.etree import ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml
    for parent in root.iter():
        for child in list(parent):
            if (child.get("package") or "") in packages:
                parent.remove(child)
    return ET.tostring(root, encoding="unicode")


async def dump_vh(serial: str, retries: int = 3) -> str:
    """uiautomator dump → XML string. Retries: the dump fails while the UI is
    animating or a soft keyboard is up. IME windows are dropped from every dump —
    keyboard chrome must be neither a tap target nor an oracle."""
    text = await _dump_vh_raw(serial, retries)
    if text:
        return _drop_windows(text, {await _ime_package(serial)})
    return text


async def _dump_vh_raw(serial: str, retries: int = 3) -> str:
    if serial in _PREFER_U2:
        # A failed `uiautomator dump` costs ~10s of idle wait. Once an app has shown
        # it never reaches idle, paying that on every tap is most of the runtime.
        alt = await asyncio.to_thread(_u2_dump, serial)
        if alt:
            _count_dump(serial, "u2")
            return alt
        _PREFER_U2.discard(serial)
    text = ""
    for attempt in range(retries):
        # Delete first. `uiautomator dump` leaves the PREVIOUS dump in place when it
        # fails, so the `cat` below would return the screen before this one — the
        # replayer would then tap what is no longer there and believe it worked.
        await _adb(serial, "shell", "rm", "-f", "/sdcard/qgb_vh.xml")
        rc, dumped = await _adb(serial, "shell", "uiautomator", "dump", "/sdcard/qgb_vh.xml")
        if _killed(rc, dumped):
            _count_dump(serial, "builtin_killed")
        _, out = await _adb(serial, "shell", "cat", "/sdcard/qgb_vh.xml")
        text = out.decode("utf-8", "replace")
        if "<hierarchy" in text or "<node" in text:
            _count_dump(serial, "builtin")
            return text
        if b"idle state" in dumped:
            # Not a passing animation — this screen never reports idle, so waiting
            # another second and asking again gets the same answer.
            alt = await asyncio.to_thread(_u2_dump, serial)
            if alt:
                _PREFER_U2.add(serial)
                _count_dump(serial, "u2")
                return alt
        await asyncio.sleep(1.0)
    # Only after the built-in path has given up, so a screen it can read keeps
    # reading exactly as it did before this fallback existed.
    alt = await asyncio.to_thread(_u2_dump, serial)
    # Every built-in attempt above failed, so `text` is never a hierarchy here: it is
    # `cat`'s complaint about a file the killed dump never wrote. It used to be counted
    # as "builtin", which credited the built-in path with exactly the dumps it lost.
    _count_dump(serial, "u2" if alt else "none")
    return alt or text


# ── the one UiAutomation slot (QUA-2741) ──────────────────────────────────────
# Android registers ONE UiAutomation client per device at a time. uiautomator2's
# on-device server holds that slot for as long as it runs: u2 >= 3 starts it as
# `app_process / com.wetest.uia2.Main -p 9008` from /data/local/tmp/u2.jar. While it
# runs, every other `uiautomator dump` fails to register (`IllegalStateException:
# UiAutomationService ... already registered!`), the exception is uncaught, and an
# app_process that dies of an uncaught exception kills ITSELF: SIGKILL, exit 137,
# "Killed". That is what the agent's dumps met on QUA-2731's board: 371 dump commands,
# 0 hierarchies, and 368 crash-buffer rows that all name the SAME registered client for
# five hours, across the harness restart between the run's two segments
# (docs/final-validation-2026-09-19.md §8). The harness starts that server itself
# (`_u2_dump`, `_u2_set_focused_text`), and so does anything else that speaks u2 to the
# device — the DevLoop MCP server does, for every hierarchy read and frame capture.
#
# The harness's own reads keep their u2 fallback. What must not happen is an agent
# handed a device whose slot is taken, so `run_episode` calls `stop_u2_server` after
# staging and before the agent starts, and the board refuses a device whose agent-side
# dump still does not work after it (`preflight.check_agent_dump`).

# How the server's process shows up in `ps -A -o PID,ARGS`.
U2_SERVER_MARKERS = ("com.wetest.uia2.Main",)
# Where the adb shell protocol reports a SIGKILLed remote command.
_SIGKILL_EXIT = 137
_U2_STOP_POLLS = 10
_U2_STOP_POLL_S = 0.3


def _killed(rc: int, out: bytes) -> bool:
    """A `uiautomator dump` that died by SIGKILL: exit 137 through adb's shell protocol,
    or the device shell's own "Killed" line."""
    return rc == _SIGKILL_EXIT or b"Killed" in (out or b"")


def u2_server_pids(ps_out: str) -> list[str]:
    """PIDs of uiautomator2 server processes in `ps -A -o PID,ARGS` output."""
    pids: list[str] = []
    for line in ps_out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and any(marker in args for marker in U2_SERVER_MARKERS):
            pids.append(pid)
    return pids


async def _running_u2_servers(serial: str) -> list[str]:
    _, out = await _adb(serial, "shell", "ps", "-A", "-o", "PID,ARGS")
    return u2_server_pids(out.decode("utf-8", "replace"))


async def stop_u2_server(serial: str) -> list[str]:
    """Free the device's UiAutomation slot: drop this process's uiautomator2 client and
    kill every uiautomator2 server running on the device, whoever started it. Returns
    the PIDs it killed ([] when none was running). Never raises: a failed stop is
    logged, and the agent-dump preflight is what refuses a device that stays taken.

    The device-side kill is the part that matters. Closing the client only closes its
    adb stream, and a server started by another process (an earlier harness run, a
    derive, the DevLoop MCP server) is not in `_U2` at all. The next harness read that
    needs u2 starts a fresh server, which is fine once the agent has exited."""
    dev = _U2.pop(serial, None)
    _PREFER_U2.discard(serial)
    if dev is not None:
        try:
            await asyncio.to_thread(dev.stop_uiautomator, False)   # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — the device-side kill below is what counts
            logger.debug("closing the uiautomator2 client for %s failed", serial,
                         exc_info=True)
    try:
        pids = await _running_u2_servers(serial)
        if not pids:
            return []
        await _adb(serial, "shell", "kill", "-9", *pids)
        for _ in range(_U2_STOP_POLLS):
            if not set(pids) & set(await _running_u2_servers(serial)):
                logger.info("stopped uiautomator2 server pid(s) %s on %s — the "
                            "UiAutomation slot is free", ", ".join(pids), serial)
                return pids
            await asyncio.sleep(_U2_STOP_POLL_S)
        logger.warning("uiautomator2 server pid(s) %s on %s survived kill -9; an agent "
                       "dump there will be killed", ", ".join(pids), serial)
        return pids
    except Exception as exc:  # noqa: BLE001 — never fail an episode on this
        logger.warning("could not stop uiautomator2 on %s: %s", serial, exc)
        return []


# An agent's two ways to read the hierarchy, as it types them (the board's transcripts,
# docs/final-validation-2026-09-19.md §8): the file form, and exec-out to the terminal.
AGENT_DUMP_FILE = "/sdcard/qgb_agent_dump.xml"


@dataclass
class AgentDump:
    command: str        # the command as an agent would type it
    ok: bool            # a real hierarchy came back
    killed: bool        # SIGKILLed on the device: another UiAutomation client holds it
    detail: str


def _first_line(out: bytes) -> str:
    text = out.decode("utf-8", "replace").strip()
    return text.splitlines()[0][:160] if text else "no output"


async def probe_agent_dump(serial: str) -> list[AgentDump]:
    """Run an agent's own `uiautomator dump`, both forms, once each, through the same
    device-side path the agent's adb reaches. Read-only apart from a scratch file on
    /sdcard, which it removes."""
    probes: list[AgentDump] = []

    await _adb(serial, "shell", "rm", "-f", AGENT_DUMP_FILE)
    rc, said = await _adb(serial, "shell", "uiautomator", "dump", AGENT_DUMP_FILE)
    _, xml = await _adb(serial, "shell", "cat", AGENT_DUMP_FILE)
    await _adb(serial, "shell", "rm", "-f", AGENT_DUMP_FILE)
    ok = b"<hierarchy" in xml
    killed = not ok and _killed(rc, said)
    probes.append(AgentDump(
        f"adb -s {serial} shell uiautomator dump {AGENT_DUMP_FILE}", ok, killed,
        f"hierarchy, {xml.count(b'<node')} nodes" if ok
        else f"killed (exit {rc})" if killed
        else f"no hierarchy (exit {rc}): {_first_line(said)}"))

    # exec-out carries no exit status: a killed dump is just an empty answer.
    rc, xml = await _adb(serial, "exec-out", "uiautomator", "dump", "/dev/tty")
    ok = b"<hierarchy" in xml
    killed = not ok and _killed(rc, xml)
    probes.append(AgentDump(
        f"adb -s {serial} exec-out uiautomator dump /dev/tty", ok, killed,
        f"hierarchy, {xml.count(b'<node')} nodes" if ok
        else "killed" if killed
        else f"no hierarchy: {_first_line(xml)}"))
    return probes


async def ime_shown(serial: str) -> bool:
    """Whether the soft keyboard is currently on screen. `press: back` means two
    different things depending on this — close the keyboard, or navigate back."""
    _, out = await _adb(serial, "shell", "dumpsys", "input_method")
    return b"mInputShown=true" in out


async def current_activity(serial: str) -> str:
    _, out = await _adb(serial, "shell", "dumpsys", "activity", "activities")
    txt = out.decode("utf-8", "replace")
    for key in ("mResumedActivity", "topResumedActivity", "ResumedActivity"):
        idx = txt.find(key)
        if idx >= 0:
            m = _ACT_RE.search(txt[idx: idx + 200])
            if m:
                return m.group(1).replace("/.", ".").replace("/", ".")
    return ""


_DEBUG_TOOL_ACTIVITIES = ("leakcanary",)


def _pick_launch_activity(bundle: str, lines: list[str]) -> str:
    for line in lines:
        line = line.strip()
        if (line.startswith(bundle + "/") and " " not in line
                and not any(tool in line.lower() for tool in _DEBUG_TOOL_ACTIVITIES)):
            return line
    return ""


async def _resolve_launch_activity(serial: str, bundle: str) -> str:
    """Resolve the app's launcher activity ('pkg/.Act'); '' if not found. A package with
    two launcher activities (AnkiDroid's debug build ships LeakCanary's) resolves to the
    system chooser, so the launcher list is queried and debug tools are skipped — the
    monkey fallback picked one of the two at random."""
    _, out = await _adb(serial, "shell", "cmd", "package",
                        "resolve-activity", "--brief", bundle)
    picked = _pick_launch_activity(bundle, list(reversed(out.decode("utf-8", "replace").splitlines())))
    if picked:
        return picked
    _, out = await _adb(serial, "shell", "cmd", "package", "query-activities", "--brief",
                        "-a", "android.intent.action.MAIN",
                        "-c", "android.intent.category.LAUNCHER", bundle)
    return _pick_launch_activity(bundle, out.decode("utf-8", "replace").splitlines())


async def _dismiss_overlays(serial: str, rounds: int = 2) -> list[str]:
    """Returns the labels it tapped. The harness acting on the screen must leave a
    record — a repro step can legitimately tap the same label, and an unrecorded
    auto-tap turns that step's missing anchor into an unexplainable failure."""
    tapped: list[str] = []
    for _ in range(rounds):
        xml = await dump_vh(serial)
        center = None
        hit = ""
        for label in _DISMISS_LABELS:
            center = find_button(xml, label)
            if center:
                hit = label
                break
        if not center:
            return tapped
        await _adb(serial, "shell", "input", "tap", str(center[0]), str(center[1]))
        tapped.append(hit)
        await asyncio.sleep(1.2)
    return tapped


_PERM_RE = re.compile(r"^\s+(android\.permission\.[A-Z_0-9]+)\s*$", re.M)


async def grant_requested_permissions(serial: str, bundle: str) -> int:
    """Re-grant every runtime permission the package asks for; returns how many.
    `pm clear` revokes grants the `-r -g` install gave, so a cleared app shows dialogs
    the agent never saw. One shell loop, failures ignored — one adb round trip."""
    _, out = await _adb(serial, "shell", "dumpsys", "package", bundle)
    text = out.decode("utf-8", "replace")
    start = text.find("requested permissions:")
    if start < 0:
        return 0
    end = text.find("install permissions:", start)
    perms = sorted(set(_PERM_RE.findall(text[start: end if end > 0 else len(text)])))
    if not perms:
        return 0
    loop = " ".join(f"pm grant {bundle} {p} 2>/dev/null;" for p in perms)
    await _adb(serial, "shell", f"{loop} true")

    # MANAGE_EXTERNAL_STORAGE is an APP-OP `pm grant` cannot set and `pm clear` resets;
    # normalize_app_env grants it pre-episode, so the replay must too. General rule:
    # a replay reset must reproduce EVERY step of episode setup.
    if "MANAGE_EXTERNAL_STORAGE" in text:
        await _adb(serial, "shell", "appops", "set", bundle,
                   "MANAGE_EXTERNAL_STORAGE", "allow")
    return len(perms)


async def relaunch(serial: str, bundle: str, timeout_s: int = 10) -> list[str]:
    """Force-stop, cold-launch the resolved launcher activity (monkey alone was
    unreliable), poll until foreground, then clear one-shot onboarding overlays.
    Returns the overlay labels auto-tapped, for the replay artifact."""
    await _adb(serial, "shell", "am", "force-stop", bundle)
    await asyncio.sleep(0.8)
    activity = await _resolve_launch_activity(serial, bundle)
    if activity:
        await _adb(serial, "shell", "am", "start", "-W", "-n", activity)
    else:
        await _adb(serial, "shell", "monkey", "-p", bundle,
                   "-c", "android.intent.category.LAUNCHER", "1")
    for _ in range(timeout_s):
        await asyncio.sleep(1.0)
        if (await current_activity(serial)).startswith(bundle):
            break
    return await _dismiss_overlays(serial)


async def disable_animations(serial: str) -> None:
    """Root-free: zero the animation scales so the main thread reaches idle and
    `uiautomator dump` stops returning an empty tree (its internal waitForIdle
    times out on persistent animations/spinners — the #1 empty-dump cause)."""
    for key in ("window_animation_scale", "transition_animation_scale",
                "animator_duration_scale"):
        await _adb(serial, "shell", "settings", "put", "global", key, "0")


async def _screencap(serial: str) -> bytes:
    _, out = await _adb(serial, "exec-out", "screencap", "-p")
    return out or b""


def _frames_stable(a: bytes, b: bytes) -> bool:
    """Deterministic 'screen unchanged' proxy: identical PNG bytes, or within a
    tiny size delta with matching head/tail (tolerates a 1px clock/cursor)."""
    if not a or not b:
        return False
    if a == b:
        return True
    if abs(len(a) - len(b)) > max(128, len(a) // 100):
        return False
    return a[:2048] == b[:2048] and a[-2048:] == b[-2048:]


async def wait_stable(serial: str, timeout_s: int = 8) -> None:
    """Poll screenshots until two consecutive frames are stable (rendered, not
    animating), or timeout. With animations off this settles in ~1-2s."""
    prev = None
    for _ in range(timeout_s):
        cur = await _screencap(serial)
        if prev is not None and _frames_stable(prev, cur):
            return
        prev = cur
        await asyncio.sleep(1.0)


async def probe_root(serial: str) -> str:
    """One-shot capture of root-capability signals — decides storage-based
    verification vs UI-only. Run LAST: `adb root` can briefly bounce the tunnel."""
    async def prop(name: str) -> str:
        _, out = await _adb(serial, "shell", "getprop", name)
        return out.decode("utf-8", "replace").strip()

    debuggable = await prop("ro.debuggable")
    btype = await prop("ro.build.type")
    tags = await prop("ro.build.tags")
    _, runas = await _adb(serial, "shell", "run-as", "de.dbauer.expensetracker", "id")
    runas_out = runas.decode("utf-8", "replace").strip()[:60]
    _, rootout = await _adb(serial, "root")
    root_out = rootout.decode("utf-8", "replace").strip()[:60]
    _, ls = await _adb(serial, "shell", "ls", "/data/data")
    ls_out = ls.decode("utf-8", "replace").strip()[:60]
    return (f"root[debuggable={debuggable} type={btype} tags={tags} "
            f"run-as='{runas_out}' adb-root='{root_out}' ls-data='{ls_out}']")


async def scroll_down(serial: str) -> None:
    """Swipe up (scroll the list down) to reveal off-screen items before a recheck."""
    _, out = await _adb(serial, "shell", "wm", "size")
    m = re.search(r"(\d+)x(\d+)", out.decode("utf-8", "replace"))
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
    x = w // 2
    await _adb(serial, "shell", "input", "swipe",
               str(x), str(int(h * 0.75)), str(x), str(int(h * 0.25)), "300")
    await asyncio.sleep(1.0)


async def tap_node(serial: str, matcher: dict) -> bool:
    """Dump, find the first node matching `matcher`, tap its center. For nav to a
    sub-screen (e.g. a 'Tasks' tab) before verifying."""
    xml = await dump_vh(serial)
    center = find_center(xml, matcher)
    if not center:
        return False
    await _adb(serial, "shell", "input", "tap", str(center[0]), str(center[1]))
    await asyncio.sleep(1.5)
    return True
