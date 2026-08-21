"""Device access for verification — plain adb over the leased serial, through the same
tunnel the runner uses (QGB_ADB_PATH). All best-effort: a failed capture yields an
empty dump, which the spec turns into a FAIL, never a crash."""

from __future__ import annotations

import asyncio
import logging
import os
import re

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

# Which source served each hierarchy dump — "builtin", "u2", or "none" (both failed).
# A replay that ran on a degraded source must say so in its artifact, or a dead u2
# reads as "the app had no matching element".
_DUMP_STATS: dict[str, dict[str, int]] = {}


def _count_dump(serial: str, source: str) -> None:
    stats = _DUMP_STATS.setdefault(serial, {})
    stats[source] = stats.get(source, 0) + 1


def dump_stats(serial: str) -> dict[str, int]:
    return dict(_DUMP_STATS.get(serial, {}))


def reset_dump_source(serial: str) -> None:
    """Forget that this device fell back to u2. Called per app, so one app that never
    reports idle does not silently move a later app off the built-in dump."""
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
        _, dumped = await _adb(serial, "shell", "uiautomator", "dump", "/sdcard/qgb_vh.xml")
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
    _count_dump(serial, "u2" if alt else ("builtin" if text else "none"))
    return alt or text


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


async def _resolve_launch_activity(serial: str, bundle: str) -> str:
    """Resolve the app's launcher activity ('pkg/.Act'); '' if not found."""
    _, out = await _adb(serial, "shell", "cmd", "package",
                        "resolve-activity", "--brief", bundle)
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith(bundle) and "/" in line and " " not in line:
            return line
    return ""


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
