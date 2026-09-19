"""Differential replay: run the agent's own reproduction with the seeding ON then OFF
and read the verdict off the difference — no answer key. Anything undecidable becomes
UNREPLAYABLE, never a false positive: a replayer bug must never cost an agent points."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .submission import Claim, Expectation, Step
from .verify.canary import clear_fired_sh, fired_markers
from .verify.crash import (CrashRecord, anr_timeout_ms, anrs_since,
                           app_crashed_since, classify as _crash_class,
                           crashes_since, device_time, signature as _crash_signature,
                           unresponsive_windows)
from .verify.device import (_adb, _adb_bin, _DISMISS_LABELS, _dismiss_overlays, append_text,
                            current_activity, disable_animations, dump_vh,
                            grant_requested_permissions, ime_shown, relaunch,
                            set_focused_text, wait_stable)
from .verify.match import (_BOUNDS_RE, nearest_clickable, parent_map, parse_vh,
                           visible_texts)

logger = logging.getLogger(__name__)

# Every source file that can change what a replay CONCLUDES. A replay.json stamped
# with the current fingerprint needs no re-deriving. episode_runner.py is included
# because staging (snapshots, device_setup, isolation) feeds replay directly.
_FINGERPRINT_SOURCES = (
    "replay.py", "submission.py", "episode_runner.py",
    # device_oracle.py decides every db/file/content post-condition, so a change
    # there changes verdicts exactly as much as a change to replay.py does.
    "verify/device.py", "verify/match.py", "verify/device_oracle.py",
    # crash.py decides CRASHED — which process died, and whether it was the app;
    # canary.py reads the seeded site's own marker, which gates what CRASHED means.
    "verify/crash.py", "verify/canary.py",
    "../../scripts/replay_findings.py",
)


def replayer_fingerprint() -> str:
    """Short hash over the replay code path, so episodes are only re-replayed when
    the replayer actually changed. A content hash, not mtimes — git checkout
    rewrites mtimes."""
    import hashlib
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for rel in _FINGERPRINT_SOURCES:
        p = (here / rel).resolve()
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:16]


# Outcome of ONE replay.
HOLDS = "holds"
VIOLATED = "violated"
INCONCLUSIVE = "inconclusive"
# The app under test DIED on the path (java/native crash or ANR, attributed to its own
# process — never a foreign one). EVIDENCE, like VIOLATED: never retried, and it reads
# as "broken" wherever VIOLATED does. Before it existed a seeded crash made the next
# anchor go missing, the pass read INCONCLUSIVE, was retried, and the finding vanished.
CRASHED = "crashed"
# The two outcomes that mean "the expectation did not hold on this build".
_BROKEN = (VIOLATED, CRASHED)

# Outcome of the DIFFERENTIAL (both replays). The vocabulary depends on WHAT WAS
# CLAIMED: an `as_specified` claim HOLDING confirms it — reading that as
# "does not reproduce" inverts the meaning.
CONFIRMED = "confirmed"                    # deviates: real, and the seeding caused it
CONFIRMED_WORKING = "confirmed_working"    # as_specified: the area does work
MISSED_DEFECT = "missed_defect"            # as_specified, but the seeding broke it
NOT_A_DEFECT = "not_a_defect"              # broken with and without the seeding
DOES_NOT_REPRODUCE = "does_not_reproduce"  # deviates, but the repro shows nothing
REPRODUCED_SEEDED = "reproduced_seeded"    # deviates: demonstrated on the seeded
                                           # build, but the clean-arm pass broke at a
                                           # step — typically a DISPLAY defect changed
                                           # the anchor's text between the two builds
UNREPLAYABLE = "unreplayable"              # the replayer could not decide

_SETTLE_S = 1.0
_KEYCODES = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER"}


def _anchors(text: str) -> list[dict]:
    """Match visible text OR content-desc: an agent writes down the label it can
    see and cannot know which attribute carried it."""
    return [{"text": text}, {"content-desc": text}]


# Android formats times and numbers with typographic spaces — U+202F between "9" and
# "AM" on this image's locale data, U+00A0 in older ones — while authored anchors and
# expectations are typed with a plain space. Both sides are folded before comparing,
# so `9 AM` finds the chip the screen renders as `9\u202fAM`.
_SPACE_RE = re.compile(r"[\u00a0\u2007\u202f\u2009\u200a\u2002\u2003\s]+")


def _fold(text: str) -> str:
    return _SPACE_RE.sub(" ", text or "").strip()


def _present(xml: str, text: str) -> bool:
    """Is `text` on screen as a value in its own right? Whole-token, not substring —
    `20` must not match `200`, or a real defect reads as does-not-reproduce. A boundary
    is any non-alphanumeric char, so `144` still matches `12x12=144`. Exact first."""
    root = parse_vh(xml)
    if root is None:
        return False
    want = _fold(text)
    if not want:
        return False
    token = re.compile(rf"(?<![0-9A-Za-z]){re.escape(want)}(?![0-9A-Za-z])", re.I)
    for node in root.iter():
        for key in ("text", "content-desc"):
            value = _fold(node.get(key))
            if not value:
                continue
            if value.casefold() == want.casefold() or token.search(value):
                return True
    return False


def _bounds(node) -> tuple[int, int, int, int] | None:
    m = _BOUNDS_RE.search(node.get("bounds") or "")
    if not m:
        return None
    left, top, right, bottom = map(int, m.groups())
    return (left, top, right, bottom) if right > left and bottom > top else None


def _row_bands(root, row: str) -> list[tuple[int, int]]:
    """The vertical extent of every element labelled EXACTLY `row` (text or
    content-desc, typographic spaces folded, case-insensitive) — the bands a
    row-scoped anchor must share. Exact only: a substring would widen the scope to
    every row that merely mentions the label."""
    want = _fold(row).lower()
    bands: list[tuple[int, int]] = []
    if not want:
        return bands
    for node in root.iter():
        if want in (_fold(node.get("text")).lower(), _fold(node.get("content-desc")).lower()):
            b = _bounds(node)
            if b:
                bands.append((b[1], b[3]))
    return bands


def _anchor_desc(text: str, row: str = "") -> str:
    return f"{text!r} in the row of {row!r}" if row else repr(text)


def _candidates(xml: str, text: str, row: str = "") -> list[dict]:
    """Plausible elements for `text`, best first. Exact outranks substring (or the
    replayer drives a different app than the agent did); a bare resource-id ranks last.
    Ties break by smallest clickable-ancestor area — the more specific control wins.

    `row` (a HARNESS route's `{tap: X, row: Y}`, `submission.Step.row`) keeps only the
    candidates whose OWN bounds — the element the gesture lands in the centre of —
    overlap vertically with an element labelled exactly `row`: the same list row.
    Needed where a control's own label repeats on every row and nothing in the label
    says which row is meant; without it the tie-break above picks a row by layout
    (MedTimer's "Reminded" status icon: whichever raised reminder sorts first,
    QUA-2735). No such label, or no candidate in its row, returns [] — an unresolved
    anchor (INCONCLUSIVE), never a confident tap on some other row. Own bounds, not
    the clickable ancestor's: when the nearest clickable is a container spanning several
    rows, every row's control overlaps every band through it, and the tie-break then
    taps the first row (QUA-2739). Empty `row` changes nothing."""
    root = parse_vh(xml)
    if root is None:
        return []
    pmap = parent_map(root)
    want = _fold(text).lower()
    found: list[dict] = []
    for order, node in enumerate(root.iter()):
        label_text = _fold(node.get("text")).lower()
        label_desc = _fold(node.get("content-desc")).lower()
        res_id = (node.get("resource-id") or "").strip().lower().rsplit("/", 1)[-1]
        clickable = (node.get("clickable") or "").lower() == "true"
        if label_text == want or label_desc == want:
            rank = 0 if clickable else 2
        elif want and (want in label_text or want in label_desc):
            # A substring match only counts when the element is itself clickable —
            # a wrong tap that SUCCEEDS gives a confident wrong verdict, where an
            # unresolved anchor honestly comes back UNREPLAYABLE.
            if not clickable:
                continue
            rank = 1
        elif want and res_id == want:
            rank = 4
        else:
            continue
        m = _BOUNDS_RE.search(node.get("bounds") or "")
        if not m:
            continue
        left, top, right, bottom = map(int, m.groups())
        if right <= left or bottom <= top:
            continue
        target = node if clickable else nearest_clickable(node, pmap)
        area = float("inf")
        if target is not None:
            tm = _BOUNDS_RE.search(target.get("bounds") or "")
            if tm:
                tl, tt, tr, tb = map(int, tm.groups())
                if tr > tl and tb > tt:
                    area = (tr - tl) * (tb - tt)
        found.append({
            "centre": ((left + right) // 2, (top + bottom) // 2),
            "rank": rank,
            "key": (rank, area, order),
            # The vertical extent the gesture lands in — the element's own, never its
            # clickable ancestor's (see `row` above).
            "span": (top, bottom),
        })
    if row:
        bands = _row_bands(root, row)
        found = [c for c in found
                 if any(min(c["span"][1], b) > max(c["span"][0], t) for t, b in bands)]
    found.sort(key=lambda c: c["key"])
    return found


def _target(xml: str, text: str) -> tuple[int, int] | None:
    """Centre of the best element for `text`, or None."""
    cands = _candidates(xml, text)
    return cands[0]["centre"] if cands else None


# The last readable hierarchy per device, as seen by the route's own anchor lookups.
# A FROZEN app's hierarchy cannot be dumped (measured 2026-09-14: `uiautomator dump`
# took 11 s and returned 39 bytes), so the stuck probe resolves its anchor from here
# when a fresh dump fails — a frozen screen is, by definition, the screen it froze on.
_LAST_VH: dict[str, str] = {}


async def _tap_any(serial: str, text: str, hold_ms: int = 0,
                   attempts: int = 3, choice: int = 0, row: str = "") -> tuple[bool, int]:
    """Tap, or long-press via a zero-distance swipe (`input tap` has no duration arg).
    `tied` > 1 means this step CHOSE among candidates; `choice` picks another on retry.
    The anchor is looked up several times — the screen may not have painted yet.
    `row` scopes the anchor to one list row (`_candidates`)."""
    cands: list[dict] = []
    for attempt in range(attempts):
        xml = await dump_vh(serial)
        if xml:
            _LAST_VH[serial] = xml
        cands = _candidates(xml, text, row=row) if xml else []
        if cands:
            break
        if attempt + 1 < attempts:
            await wait_stable(serial)
            await asyncio.sleep(_SETTLE_S)
    if not cands:
        return False, 0, None
    tied = sum(1 for c in cands if c["rank"] == cands[0]["rank"])
    centre = cands[choice if 0 <= choice < len(cands) else 0]["centre"]
    await _gesture(serial, centre, hold_ms)
    return True, tied, centre


async def _gesture(serial: str, centre: tuple[int, int], hold_ms: int = 0) -> None:
    x, y = centre
    if hold_ms:
        await _adb(serial, "shell", "input", "swipe",
                   str(x), str(y), str(x), str(y), str(hold_ms))
    else:
        await _adb(serial, "shell", "input", "tap", str(x), str(y))


@dataclass
class ReplayResult:
    outcome: str
    detail: str = ""
    steps_run: int = 0
    screen: list[str] = field(default_factory=list)
    # Tap steps where several candidates tied — the replayer CHOSE an interpretation.
    ambiguous: list[int] = field(default_factory=list)
    # Non-default candidate picks this pass ran with ({step index: candidate index}).
    choices: dict = field(default_factory=dict)
    # Labels _dismiss_overlays auto-tapped — the harness acting on the screen
    # mid-replay must be visible in the artifact.
    dismissed: list[str] = field(default_factory=list)
    # Steps whose gesture was issued a second time (see run_steps).
    reissued: list[int] = field(default_factory=list)
    # `press: back` steps skipped because the keyboard they close was not shown.
    back_noops: list[int] = field(default_factory=list)
    # CRASHED only: what died — {process, kind, exception, message, signature,
    # exit_reason?} — so the artifact shows the crash, not just that one happened.
    crash: dict | None = None
    # Seeded-site markers (`verify.canary`) read after the pass — the fault ids whose
    # own path reported itself. None = not read; [] = read, nothing fired.
    fired: list[str] | None = None

    def as_dict(self) -> dict:
        out = {"outcome": self.outcome, "detail": self.detail,
               "steps_run": self.steps_run}
        if self.crash:
            out["crash"] = self.crash
        if self.fired is not None:
            out["fired"] = self.fired
        if self.ambiguous:
            out["ambiguous_steps"] = self.ambiguous
        if self.choices:
            out["choices"] = {str(k): v for k, v in self.choices.items()}
        if self.dismissed:
            out["dismissed"] = self.dismissed
        if self.reissued:
            out["reissued_steps"] = self.reissued
        if self.back_noops:
            out["back_noop_steps"] = self.back_noops
        return out


@dataclass
class DifferentialResult:
    area: str
    verdict: str                 # what the agent claimed
    classification: str
    seeded_on: ReplayResult | None = None
    seeded_off: ReplayResult | None = None

    def as_dict(self) -> dict:
        return {
            "area": self.area,
            "claimed": self.verdict,
            "classification": self.classification,
            "seeded_on": self.seeded_on.as_dict() if self.seeded_on else None,
            "seeded_off": self.seeded_off.as_dict() if self.seeded_off else None,
        }


async def set_flags(serial: str, bundle: str, bug_ids: Sequence[str]) -> bool:
    """Activate exactly these seeded defects; an empty list is the clean build.
    Mirrors episode_runner.write_bug_flags: one id per LINE, since printf never
    expands escapes inside a %s argument — getting it wrong looks like a clean build."""
    ids = " ".join(shlex.quote(str(b)) for b in bug_ids if b)
    inner = f"""mkdir -p files && printf '%s\\n' {ids} > files/qgb_flags.txt"""
    if not ids:
        inner = "mkdir -p files && : > files/qgb_flags.txt"
    # Same command, so the attribution markers can never outlive the flags they
    # belong to: a marker seen after this pass was written during it.
    inner = f"{clear_fired_sh()}; {inner}"
    rc, _ = await _adb(serial, "shell",
                       f"run-as {shlex.quote(bundle)} sh -c {shlex.quote(inner)}")
    return rc == 0


async def snapshot(serial: str, bundle: str, path: "Path") -> bool:
    """Tar the app's private data, once per episode before the agent runs. Apps seed
    RANDOMISED sample data on first run, so without the snapshot any reproduction
    that references existing data is unreplayable."""
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, "exec-out", f"run-as {shlex.quote(bundle)} tar cf - .",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    data, _ = await proc.communicate()
    if proc.returncode != 0 or len(data) < 512:
        return False
    Path(path).write_bytes(data)
    return True


def safe_shared_paths(paths: Sequence[str] | None) -> list[str]:
    """The declared `shared_storage:` dirs, minus anything unsafe to `rm -rf`.
    Reuses episode_runner's roots — a safety check with two copies is a safety
    check with one bug."""
    from .episode_runner import _SHARED_STORAGE_ROOTS
    out = []
    for raw in paths or []:
        path = str(raw).rstrip("/")
        root = next((r for r in _SHARED_STORAGE_ROOTS if path.startswith(r)), None)
        if root and path[len(root):].strip():
            out.append(path)
        else:
            logger.warning("shared_storage: refusing %r — must be a directory under %s",
                           raw, " or ".join(_SHARED_STORAGE_ROOTS))
    return out


async def snapshot_shared(serial: str, paths: Sequence[str], path: "Path") -> bool:
    """Tar the app's SHARED-storage dirs; empty `paths` returns False. pm clear never
    touches /sdcard, so without this two replay passes of the same check would start
    from different states."""
    safe = safe_shared_paths(paths)
    if not safe:
        return False
    rel = " ".join(shlex.quote(p.lstrip("/")) for p in safe)
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, "exec-out", f"tar cf - -C / {rel} 2>/dev/null",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    data, _ = await proc.communicate()
    if len(data) < 512:
        return False
    Path(path).write_bytes(data)
    return True


async def _restore_shared(serial: str, paths: Sequence[str], path: "Path") -> bool:
    safe = safe_shared_paths(paths)
    if not safe or not Path(path).exists():
        return False
    for p in safe:
        q = shlex.quote(p)
        await _adb(serial, "shell", f"rm -rf {q} && mkdir -p {q}")
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, "shell", "tar xf - -C /",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate(Path(path).read_bytes())
    return proc.returncode == 0


async def _restore(serial: str, bundle: str, path: "Path") -> bool:
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", serial, "shell", f"run-as {shlex.quote(bundle)} tar xf -",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    await proc.communicate(Path(path).read_bytes())
    return proc.returncode == 0


async def _reset(serial: str, bundle: str, bug_ids: Sequence[str],
                 snap: "Path | None" = None, shared: Sequence[str] | None = None,
                 shared_snap: "Path | None" = None,
                 device_setup: dict | None = None) -> bool:
    """Restore the episode's starting state with `bug_ids` live. Flags are written
    LAST — the snapshot carries the episode's own flags file. device_setup and
    isolation are re-run because a replay must reproduce every step of episode staging."""
    # Rotation is a DEVICE setting: `pm clear` does not touch it, so a pass that ended
    # in landscape would hand the next one a rotated device it never asked for — the
    # same leak shared storage had. This pin holds only when an APP is in front: after
    # a pass that force-stopped the app in landscape the launcher is, and the next
    # launch restores landscape over it. The route's `launch` step therefore pins again
    # once the app is up (`repin_portrait_after_launch`, QUA-2734); this one still
    # covers everything staged before that launch.
    await _set_rotation(serial, "portrait")
    await _adb(serial, "shell", f"pm clear {shlex.quote(bundle)}")
    await grant_requested_permissions(serial, bundle)
    if shared and shared_snap is not None:
        await _restore_shared(serial, shared, shared_snap)
    if device_setup:
        # Lazy: episode_runner imports this module at module level.
        from .episode_runner import run_device_setup
        await run_device_setup(serial, device_setup)
    if snap is not None and Path(snap).exists():
        await _adb(serial, "shell",
                   f"run-as {shlex.quote(bundle)} sh -c 'rm -rf ./* 2>/dev/null; true'")
        await _restore(serial, bundle, snap)
    from .episode_runner import isolate_app_under_test
    await isolate_app_under_test(serial, bundle)
    return await set_flags(serial, bundle, bug_ids)


async def _type_text(serial: str, text: str) -> None:
    """`type` SETS the field's value, matching mobile_type_text — `adb input text`
    appends, which diverges on pre-filled fields and killed whole repro sets.
    Keystroke semantics remain available as `append`."""
    await set_focused_text(serial, text)


async def _press(serial: str, key: str) -> None:
    await _adb(serial, "shell", "input", "keyevent", _KEYCODES[key.lower()])


_ROTATION_CODES = {"portrait": "0", "landscape": "1"}


async def _set_rotation(serial: str, orientation: str) -> None:
    """Pin the display to `orientation`. Auto-rotate is turned OFF first and every
    time: with `accelerometer_rotation` on, `user_rotation` is advisory and the
    sensor — which an emulator reports as a fixed value — can put the device
    straight back, so the configuration change a lifecycle case depends on would
    silently not happen and the case would pass for the wrong reason."""
    await _adb(serial, "shell", "settings", "put", "system",
               "accelerometer_rotation", "0")
    await _adb(serial, "shell", "settings", "put", "system", "user_rotation",
               _ROTATION_CODES[orientation.strip().lower()])


async def _rotate(serial: str, orientation: str) -> None:
    """The `rotate` step. A rotation DESTROYS and recreates the activity, so the next
    step's anchor does not exist until the new one has drawn — `wait_stable` is part
    of the step, not an optimisation."""
    await _set_rotation(serial, orientation)
    await wait_stable(serial)


async def repin_portrait_after_launch(serial: str, bundle: str,
                                      timeout_s: int = 10) -> bool:
    """Pin PORTRAIT again once the app under test has been launched and is in FRONT,
    then let it settle. The one re-pin both staging paths share (QUA-2734): the route's
    `launch` step (`_launch`, which `run_steps` and derive_journey's executor both use)
    and `episode_runner.run_episode` right after `session.launch_app`. derive_journey's
    `stage()` calls it after its own launch too. The pre-launch pins in `_reset` and
    `normalize_app_env` stay. They are not enough on their own.

    Why not. A `user_rotation` written while the LAUNCHER is on top does not survive
    the next app launch on our android-35 emulators. QUA-2731's live-path pre-check
    measured it on fossify-calendar, orgzly and medtimer. Rotate the app to landscape
    and force-stop it, and the launcher comes up reading `user_rotation` 0 although
    nothing wrote it. Pin 0 anyway, launch the app, and the setting reads 1 again and
    the app draws landscape, still at +5 s. The value restored is the one in force
    when the launcher came to the front, and it is device-wide: the NEXT app launched
    inherits it, whichever app it is. A pin written while an APP is in front holds.
    Staging always pins with the launcher on top, because `isolate_app_under_test`
    ends by sending HOME. So any pass or episode that ended with the app stopped in
    landscape leaked landscape into the next one. On the replay path the leak was
    masked: the landscape attempt went INCONCLUSIVE, `one_pass` retried with the app
    in front, and the retry's pin held.

    The mechanism was read off emulator-5558 on 2026-09-18, in `dumpsys window displays`,
    whose RotationLockHistory names the caller of every user-rotation write. The Pixel
    launcher requests SCREEN_ORIENTATION_NOSENSOR. When it becomes the top fullscreen
    activity, `DisplayRotationReversionController.updateForNoSensorOverride` (run from
    `DisplayContent.updateOrientation`) SAVES the locked user rotation, which is
    ROTATION_90 after a landscape force-stop. The display then draws at 0, and SystemUI's
    `RotationButtonController#onRotationWatcherChanged` re-locks the user rotation at 0.
    That is the 0 the launcher reads. A pin written now changes the setting, not the
    saved value. When the app replaces the launcher, `revertOverride` writes the saved
    value back as `setUserRotation(LOCKED, ROTATION_90,
    "DisplayRotationReversionController#revertOverride")`, and the app draws landscape.
    With an app in front no override is active, so a pin written then holds.

    Hence the order: wait until `bundle` is the resumed activity, THEN pin, then
    `wait_stable`. A real rotation back to portrait recreates the activity, and the
    next step's anchor does not exist until it has drawn. If the app never comes to the
    front within `timeout_s`, this pins anyway but loudly, and returns False: that is
    the one case in which the pin may not hold.

    Only `launch` re-pins, and a `relaunch` that is the route's FIRST step. Mid-route,
    `relaunch` is process death. On a device the route turned landscape the app comes
    back landscape, and the harness must not add a rotation the route did not ask for.
    At step 0 the route has turned nothing yet: the only orientation there is the one
    the previous pass leaked, so a route that opens with `relaunch` (the hunt brief
    allows it) starts upright like one that opens with `launch` (QUA-2738). A later
    `rotate` step in the route is still a real configuration change, because the
    device is portrait when it runs."""
    in_front = False
    for attempt in range(max(1, timeout_s)):
        if (await current_activity(serial)).startswith(bundle):
            in_front = True
            break
        if attempt + 1 < max(1, timeout_s):
            await asyncio.sleep(1.0)
    if not in_front:
        logger.warning("re-pin: %s is not in front after %ds — pinning portrait anyway; "
                       "a pin written under the launcher is lost on the next launch",
                       bundle, timeout_s)
    await _set_rotation(serial, "portrait")
    await wait_stable(serial)
    return in_front


async def _launch(serial: str, bundle: str) -> list[str]:
    """The `launch` step: a cold start, then upright (`repin_portrait_after_launch`).
    It is `relaunch` plus the re-pin, and it is shared by both route executors,
    `run_steps` and derive_journey's `run_with_dumps`, which also run a step-0
    `relaunch` through it. A copy in either of them is a copy that can lose the
    re-pin. Returns the overlay labels `relaunch` auto-tapped."""
    auto = await relaunch(serial, bundle)
    await repin_portrait_after_launch(serial, bundle)
    return auto


async def _swipe(serial: str, direction: str) -> None:
    # Mid-screen drags, deliberately short of the edges so a gesture-navigation build
    # does not read them as system back/home.
    moves = {"up": (540, 1500, 540, 600), "down": (540, 600, 540, 1500),
             "left": (900, 1000, 200, 1000), "right": (200, 1000, 900, 1000)}
    x1, y1, x2, y2 = moves[direction.lower()]
    await _adb(serial, "shell", "input", "swipe",
               str(x1), str(y1), str(x2), str(y2), "300")


async def crash_window(serial: str) -> str:
    """Device time to hand `crash_verdict` as `since`. Taken BEFORE the first step —
    the `launch` step's own force-stop writes a (non-fatal) exit-info row, which is
    why the window opens here and the check gates on FATAL_REASONS. Never raises; an
    empty string disables the check (no window, no verdict)."""
    try:
        return await device_time(serial)
    except Exception:  # noqa: BLE001 — a diagnostic must never take the replay down
        logger.warning("crash window: could not read the device clock", exc_info=True)
        return ""


def _crash_dict(rec, bundle: str) -> dict:
    out = {
        "process": rec.process,
        "kind": rec.kind,
        "exception": rec.exception,
        "message": (rec.message or "")[:200],
        "signature": _crash_signature(rec, bundle),
    }
    if rec.raw.startswith("exit-info:"):
        # Synthesised from `dumpsys activity exit-info` (an ANR, or a native death the
        # crash buffer rolled past): the "exception" is the exit reason.
        out["exit_reason"] = rec.exception
    return out


async def crash_verdict(serial: str, bundle: str, since: str,
                        fallback: ReplayResult) -> ReplayResult:
    """The crash check that lives on the step-FAILURE path (and once at the end of a
    clean run). If the app under test died since `since`, the answer is CRASHED —
    evidence, carrying what died — otherwise `fallback` is returned unchanged except
    that a foreign crash in the window is noted in its detail, never charged. Cheap:
    one crash-buffer read plus exit-info, only when called. Never raises: on any error
    the fallback is what the caller would have returned anyway."""
    if not since:
        return fallback
    try:
        rec = await app_crashed_since(serial, bundle, since)
        if rec is not None:
            head = f"{rec.exception}: {rec.message}" if rec.message else rec.exception
            prefix = (fallback.detail.split(":", 1)[0] if fallback.detail.startswith("step ")
                      else f"step {fallback.steps_run}")
            # The process is named in the line itself: a framework-thrown crash (an
            # `am crash`, a RemoteServiceException) has no app frame for the
            # signature to carry, and the artifact must still say WHAT died. An ANR
            # is the same outcome (the app is gone from under the repro) with a
            # different verb — the process may well still be alive behind the
            # "isn't responding" dialog.
            verb = "stopped responding (ANR)" if rec.kind == "anr" else "crashed"
            out = ReplayResult(CRASHED,
                               f"{prefix}: {rec.process or bundle} {verb} — {head[:120]} — "
                               f"{_crash_signature(rec, bundle)}",
                               fallback.steps_run, crash=_crash_dict(rec, bundle))
            out.ambiguous, out.choices = fallback.ambiguous, fallback.choices
            out.dismissed, out.reissued = fallback.dismissed, fallback.reissued
            out.back_noops = fallback.back_noops
            return out
        foreign = [r for r in await crashes_since(serial, bundle, since, include_foreign=True)
                   if _crash_class(r, bundle) == "foreign"]
        if foreign:
            names = sorted({r.process or "unnamed shell process" for r in foreign})
            fallback.detail = (f"{fallback.detail} (foreign crash in "
                               f"{', '.join(names)} ignored)").strip()
        return fallback
    except Exception:  # noqa: BLE001
        logger.warning("crash check failed; keeping %s", fallback.outcome, exc_info=True)
        return fallback


def _crash_sig_text(crash: dict) -> str:
    return " ".join(str(crash.get(k) or "") for k in ("signature", "exception", "message"))


def gate_crash(result: ReplayResult, expect: Expectation | None,
               fired: list[str] | None = None,
               seeded: Sequence[str] = ()) -> ReplayResult:
    """Decide what a CRASHED pass MEANS under the expectation's liveness gates and the
    seeded-site markers. Pure; anything but CRASHED is returned untouched.

    Identity, in order of trust:
      1. `fired` markers (`verify.canary`): the seeded site reported that its own path
         ran. A marker for one of `seeded` makes the death the seeded fault's BY
         CONSTRUCTION; the signature is then corroboration. A marker for a fault that
         is NOT seeded (or any marker on a clean pass, `seeded` empty) means the flag
         gate did not hold -> INCONCLUSIVE, never evidence either way.
      2. the normalised signature / exception (`crash: "<text>"`) or the ANR reason
         (`anr: "<text>"`), substring, case-insensitive.
    A marker that says "seeded path ran" combined with a signature that does not match
    the gate is a DISAGREEMENT: INCONCLUSIVE with both facts in the detail (the case's
    `crash:` text is probably stale — a corpus finding, not a verdict).
    `anr: ...` demands kind "anr"; `crash: ...` accepts any death of the app (java,
    native or ANR), since a hang is one way the seeded fault can present.
    Anything that does not match is INCONCLUSIVE — the seeded fault is not what fired,
    so the pass demonstrated nothing — never VIOLATED and never HOLDS."""
    if result.outcome != CRASHED or expect is None:
        return result
    crash = result.crash or {}
    sig = _crash_sig_text(crash)
    kind = str(crash.get("kind") or "")
    if fired is not None:
        result.fired = list(fired)

    def _inconclusive(why: str) -> ReplayResult:
        out = ReplayResult(INCONCLUSIVE, f"{result.detail} — {why}", result.steps_run,
                           crash=result.crash, fired=result.fired)
        out.ambiguous, out.choices = result.ambiguous, result.choices
        out.dismissed, out.reissued, out.back_noops = result.dismissed, result.reissued, result.back_noops
        return out

    seeded_set = {str(b) for b in seeded if b}
    fired_set = set(fired or [])
    own = sorted(fired_set & seeded_set)
    foreign = sorted(fired_set - seeded_set)
    if foreign:
        return _inconclusive(f"fired marker(s) {foreign} name a fault that is not seeded "
                             f"here ({sorted(seeded_set) or 'clean pass'}) — the flag gate "
                             f"did not hold")

    gate_ok, why = True, ""
    if expect.anr:
        if kind != "anr":
            gate_ok, why = False, f"the app died ({kind}), but an ANR was expected: {crash.get('signature')}"
        elif isinstance(expect.anr, str) and expect.anr.lower() not in sig.lower():
            gate_ok, why = False, (f"stopped responding, but not the expected ANR "
                                   f"({expect.anr!r}): {crash.get('signature')}")
    if gate_ok and isinstance(expect.crash, str) and expect.crash.lower() not in sig.lower():
        gate_ok, why = False, f"crashed, but not the expected crash: {crash.get('signature')}"

    if not gate_ok:
        if own:
            return _inconclusive(f"fired marker {own} says the seeded path ran, yet {why}")
        return _inconclusive(why)
    if own:
        result.detail = f"{result.detail} — fired: {', '.join(own)}"
    return result


async def _probe_tap(serial: str, centre: tuple[int, int]):
    """Start ONE `input tap` and return its process, un-awaited: on a hung app the
    command blocks ~30 s (the injection timeout) — the dispatcher's verdict comes at
    the ANR deadline, long before. Killing the local client afterwards never re-sends
    the event."""
    x, y = centre
    argv = [_adb_bin(), "-s", serial, "shell", "input", "tap", str(x), str(y)]
    return await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)


_STUCK_MARGIN_MS = 3000       # past the dispatcher's own deadline before we call it live
_STUCK_POLL_S = 0.5


async def _check_stuck(serial: str, bundle: str, expect: Expectation,
                       ran: int, since: str) -> ReplayResult:
    """The `stuck:` probe. Resolve the anchor (fresh dump first; a hung app's
    hierarchy is unreadable, so the route's last readable screen is the fallback —
    a frozen screen IS that screen), issue exactly one tap, then watch the input
    dispatcher (`unresponsive_windows`) and the ANR log (`anrs_since`) until
    `anr_timeout_ms + margin` after the tap.

      answered within the deadline, dispatcher quiet -> HOLDS
      dispatcher gave up on the app's window / am_anr -> CRASHED (kind "anr")
      anchor resolvable nowhere                      -> INCONCLUSIVE
    Never raises."""
    anchor = expect.stuck
    try:
        xml = await dump_vh(serial, retries=1)
        centre = _target(xml, anchor) if xml else None
        source = "screen"
        if centre is None and _LAST_VH.get(serial):
            centre = _target(_LAST_VH[serial], anchor)
            source = "the route's last readable screen"
        if centre is None:
            return ReplayResult(INCONCLUSIVE,
                                f"stuck probe: no element matching {anchor!r} on the final "
                                f"screen{' (hierarchy unreadable)' if not xml else ''}", ran)
        timeout_ms = await anr_timeout_ms(serial)
        deadline_s = (timeout_ms + _STUCK_MARGIN_MS) / 1000.0
        proc = await _probe_tap(serial, centre)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        hung: list[tuple[str, int]] = []
        anr: CrashRecord | None = None
        answered_s: float | None = None
        try:
            while True:
                elapsed = loop.time() - t0
                hung = await unresponsive_windows(serial, bundle)
                if hung:
                    recs = await anrs_since(serial, bundle, since)
                    anr = recs[0] if recs else None
                    break
                if proc.returncode is not None:
                    answered_s = elapsed
                    break
                if elapsed >= deadline_s:
                    recs = await anrs_since(serial, bundle, since)
                    anr = recs[0] if recs else None
                    break
                await asyncio.sleep(_STUCK_POLL_S)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if not hung and anr is None:
            if answered_s is None:
                # The tap never returned but the dispatcher never gave up either —
                # not a hang the dispatcher recognises; do not guess.
                return ReplayResult(INCONCLUSIVE,
                                    f"stuck probe: tap on {anchor!r} unanswered after "
                                    f"{deadline_s:.1f}s, dispatcher still responsive", ran)
            return ReplayResult(HOLDS,
                                f"stuck probe: {anchor!r} answered in {answered_s * 1000:.0f}ms "
                                f"(anchor from {source})", ran)
        if anr is None:
            window = hung[0][0] if hung else bundle
            reason = f"Input dispatching timed out ({window.split(' ', 1)[-1]})"
            anr = CrashRecord(process=bundle, pid=None, timestamp=since, kind="anr",
                              exception="ANR", message=reason,
                              raw=f"dumpsys input: unresponsive window {window!r}")
        return ReplayResult(
            CRASHED,
            f"step {ran}: {bundle} stopped responding (ANR) — stuck probe on {anchor!r} "
            f"unanswered: {anr.message[:100]} — {_crash_signature(anr, bundle)}",
            ran, crash={**_crash_dict(anr, bundle),
                        "probe": {"anchor": anchor, "source": source,
                                  "unresponsive": hung, "timeout_ms": timeout_ms}})
    except Exception:  # noqa: BLE001 — a probe fault is never the agent's
        logger.warning("stuck probe failed", exc_info=True)
        return ReplayResult(INCONCLUSIVE, f"stuck probe on {anchor!r} could not run", ran)


async def _fired_safe(serial: str, bundle: str) -> list[str] | None:
    try:
        return await fired_markers(serial, bundle)
    except Exception:  # noqa: BLE001
        return None


async def run_steps(serial: str, bundle: str, steps: Sequence[Step],
                    choices: dict[int, int] | None = None) -> ReplayResult:
    """Execute steps without evaluating anything: HOLDS = every step ran, INCONCLUSIVE
    names the one that could not, CRASHED means the app under test died on the path
    (checked on every step-failure path and once after the last step — a crash on the
    final step has no later anchor to reveal it). Split out so `check_setup:` uses the
    SAME executor. `choices` maps a step index to a non-default anchor candidate."""
    choices = choices or {}
    since = await crash_window(serial)
    ambiguous: list[int] = []
    dismissed: list[str] = []
    reissued: list[int] = []
    back_noops: list[int] = []
    # Android drops touches during relayout; a swallowed gesture surfaces one step
    # later as a missing anchor. If the previous anchor still sits at the exact same
    # coordinates, the gesture is re-issued once — a landed one would have moved the UI.
    last_gesture: tuple[int, str, int, tuple[int, int], str] | None = None

    def _done(result: ReplayResult) -> ReplayResult:
        result.ambiguous = ambiguous
        result.choices = dict(choices)
        result.dismissed = dismissed
        result.reissued = reissued
        result.back_noops = back_noops
        return result

    ran = 0
    for index, step in enumerate(steps):
        try:
            if step.action in ("launch", "relaunch"):
                # `launch` starts the app UPRIGHT (QUA-2734), and so does a `relaunch`
                # that OPENS the route (QUA-2738): at step 0 the route has left no
                # orientation, only the previous pass's leak. Mid-route, `relaunch` is
                # process death and keeps whatever orientation the route put the device in.
                upright = step.action == "launch" or index == 0
                auto = await (_launch(serial, bundle) if upright
                              else relaunch(serial, bundle))
                # isinstance: tests stub relaunch with a bare truthy return.
                if isinstance(auto, list):
                    dismissed.extend(auto)
            elif step.action == "wait":
                await wait_stable(serial)
            elif step.action in ("tap", "long_press"):
                hold = 900 if step.action == "long_press" else 0
                row = step.row
                tapped, tied, centre = await _tap_any(serial, step.value,
                                                      hold_ms=hold,
                                                      choice=choices.get(index, 0),
                                                      row=row)
                if (not tapped and last_gesture is not None
                        and last_gesture[0] == index - 1
                        and last_gesture[0] not in reissued):
                    p_idx, p_text, p_hold, p_centre, p_row = last_gesture
                    xml = await dump_vh(serial)
                    still_there = xml and any(
                        c["centre"] == p_centre for c in _candidates(xml, p_text, row=p_row))
                    if still_there:
                        await _gesture(serial, p_centre, p_hold)
                        reissued.append(p_idx)
                        logger.info("step %d: gesture on %r re-issued — %r missing "
                                    "and the screen still showed %r untouched",
                                    p_idx + 1, p_text, step.value, p_text)
                        await asyncio.sleep(_SETTLE_S)
                        await wait_stable(serial)
                        tapped, tied, centre = await _tap_any(
                            serial, step.value, hold_ms=hold,
                            choice=choices.get(index, 0), row=row)
                # The "<App> isn't responding" dialog is NOT an overlay this clears:
                # its buttons are "Close app" / "Wait" and _DISMISS_LABELS matches
                # exact text ("close" != "close app"; "wait" is not listed) — pinned
                # by test_the_anr_dialog_is_not_an_overlay_the_replayer_dismisses.
                # Tapping Wait would hide the ANR the crash check below is about to
                # find; Close app would kill the evidence.
                if not tapped and step.value.strip().lower() not in _DISMISS_LABELS:
                    auto = await _dismiss_overlays(serial, rounds=1)
                    if auto:
                        dismissed.extend(auto)
                        logger.info("step %d: dismissed overlay %r revealed by a "
                                    "missing anchor %r", index + 1, auto, step.value)
                        await wait_stable(serial)
                        tapped, tied, centre = await _tap_any(
                            serial, step.value, hold_ms=hold,
                            choice=choices.get(index, 0), row=row)
                if tied > 1:
                    ambiguous.append(index)
                if not tapped:
                    # A missing anchor is NOT a failed expectation — the repro could
                    # not be carried out, which is non-punitive... unless the anchor
                    # is missing because the app is no longer there.
                    return await crash_verdict(serial, bundle, since, _done(
                        ReplayResult(INCONCLUSIVE,
                                     f"step {ran + 1}: no element matching "
                                     f"{_anchor_desc(step.value, row)}", ran)))
                last_gesture = (index, step.value, hold, centre, row)
            elif step.action == "type":
                await _type_text(serial, step.value)
            elif step.action == "append":
                await append_text(serial, step.value)
            elif step.action == "press":
                # `back` right after typing means "close the keyboard", but our `type`
                # never summons the IME — a literal back would dismiss the sheet and
                # orphan later anchors. Skipped when no keyboard; recorded, never silent.
                if (step.value.strip().lower() == "back"
                        and index > 0 and steps[index - 1].action == "type"
                        and not await ime_shown(serial)):
                    back_noops.append(index)
                    logger.info("step %d: back-after-type skipped — the keyboard "
                                "it closes is not shown", index + 1)
                else:
                    await _press(serial, step.value)
            elif step.action == "swipe":
                await _swipe(serial, step.value)
            elif step.action == "rotate":
                await _rotate(serial, step.value)
            else:
                return _done(ReplayResult(INCONCLUSIVE,
                                          f"unknown action {step.action}", ran))
            ran += 1
            await asyncio.sleep(_SETTLE_S)
        except Exception as exc:  # noqa: BLE001 — a replayer fault is never the agent's
            return await crash_verdict(serial, bundle, since, _done(
                ReplayResult(INCONCLUSIVE, f"step {ran + 1}: {exc}", ran)))

    await wait_stable(serial)
    # Every step ran — but the LAST one may have killed the app with nothing after
    # it to notice. Still a crash on the path.
    return await crash_verdict(serial, bundle, since, _done(ReplayResult(HOLDS, "", ran)))


async def replay(serial: str, bundle: str, steps: Sequence[Step],
                 expect: Expectation,
                 choices: dict[int, int] | None = None,
                 seeded: Sequence[str] = ()) -> ReplayResult:
    """Execute one reproduction and evaluate its post-condition. A CRASHED run reads
    the seeded-site markers and passes through `gate_crash` (with `seeded`, the bug
    ids live on this pass, for marker identity); an INCONCLUSIVE run is returned
    as-is, provenance fields included — there is no post-condition to evaluate on a
    dead app. On a live app the `stuck:` probe runs FIRST (before a `db:` read
    force-stops the app), then the state oracle; a standalone liveness expectation
    HOLDS once the route has run with the app alive."""
    since = await crash_window(serial)
    result = await run_steps(serial, bundle, steps, choices=choices)
    if result.outcome == CRASHED:
        return gate_crash(result, expect, await _fired_safe(serial, bundle), seeded)
    if result.outcome != HOLDS:
        return result
    ran = result.steps_run

    def _carry(final: ReplayResult) -> ReplayResult:
        final.ambiguous = result.ambiguous
        final.choices = result.choices
        final.dismissed = result.dismissed
        final.reissued = result.reissued
        final.back_noops = result.back_noops
        return final

    if expect.stuck:
        probe = await _check_stuck(serial, bundle, expect, ran, since)
        if probe.outcome == CRASHED:
            return _carry(gate_crash(probe, expect, await _fired_safe(serial, bundle), seeded))
        if probe.outcome != HOLDS or expect.mode == "stuck":
            return _carry(probe)
    if expect.mode in ("crash", "anr"):
        return _carry(ReplayResult(HOLDS, "the app is alive after the route", ran))
    if expect.mode == "db":
        return _carry(await _check_db(serial, bundle, expect, ran))
    if expect.mode == "file":
        return _carry(await _check_file(serial, bundle, expect, ran))
    if expect.mode == "content":
        return _carry(await _check_content(serial, bundle, expect, ran))

    xml = await dump_vh(serial)
    if not xml:
        return _carry(ReplayResult(INCONCLUSIVE, "could not read the UI hierarchy",
                                   ran))

    found = _present(xml, expect.text)
    holds = found if expect.mode == "present" else not found
    return _carry(ReplayResult(
        HOLDS if holds else VIOLATED,
        f"{expect.mode} {expect.text!r} → {'yes' if found else 'no'}",
        ran, visible_texts(xml)[:40]))


async def _check_db(serial: str, bundle: str, expect: Expectation,
                    ran: int) -> ReplayResult:
    """Read the app's own database through the SAME oracle the guided tasks use
    (`verify.device_oracle.query_db`): the file is pulled off the device with
    `run-as cat` — plus its `-wal`, so the writes the episode just made are seen —
    and queried with the HOST's sqlite3. It must not shell out to an on-device
    `sqlite3`: Google Play system images ship none, and that binary's absence read as
    INCONCLUSIVE, which journey scoring then counted as success — 19 db-oracle
    episodes were "completed" with no verification at all.

    A SQL error or an unreadable file is INCONCLUSIVE, never VIOLATED — a mistyped
    table name must not read as a broken app. An absolute `db` path is read with a
    plain `cat` as the SHELL user (the external app dir, where run-as has no storage
    access — AnkiDroid's collection.anki2)."""
    from .verify.device_oracle import query_db
    value, detail = await asyncio.to_thread(
        query_db, {"db": expect.db, "query": expect.query}, bundle, serial)
    if value is None:
        return ReplayResult(INCONCLUSIVE, f"db query failed: {detail[:120]}", ran)
    text = value.strip()
    holds = text == expect.equals.strip()
    return ReplayResult(HOLDS if holds else VIOLATED,
                        f"db {expect.query[:60]!r} → {text!r} (want {expect.equals!r})",
                        ran)


async def _check_file(serial: str, bundle: str, expect: Expectation,
                      ran: int) -> ReplayResult:
    """Filesystem post-condition through the same oracle the guided tasks use
    (`verify.device_oracle.check_file`): a directory entry or file content, on shared
    storage or, via run-as, in the app sandbox. An unreadable path is INCONCLUSIVE
    unless the expectation is `absent`, where "not there" is the evidence."""
    from .verify.device_oracle import check_file
    oracle = {"path": expect.path, "absent": expect.absent}
    if expect.contains is not None:
        oracle["contains"] = expect.contains
    else:
        oracle["name"] = expect.name
    ok, detail = await asyncio.to_thread(check_file, oracle, bundle, serial)
    if "[" in detail and "rc=" in detail and not expect.absent:
        return ReplayResult(INCONCLUSIVE, f"file oracle failed: {detail[:120]}", ran)
    return ReplayResult(HOLDS if ok else VIOLATED, detail[:160], ran)


async def _check_content(serial: str, bundle: str, expect: Expectation,
                         ran: int) -> ReplayResult:
    """ContentProvider post-condition (`verify.device_oracle.check_content`) for
    state the app keeps OUTSIDE its sandbox — contacts, calendar, MediaStore.
    A query error is INCONCLUSIVE; `absent` inverts a `contains` match."""
    from .verify.device_oracle import check_content
    oracle: dict = {"uri": expect.uri}
    if expect.where:
        oracle["where"] = expect.where
    if expect.contains is not None:
        oracle["contains"] = expect.contains
    else:
        oracle["expect"] = expect.equals
    ok, detail = await asyncio.to_thread(check_content, oracle, bundle, serial)
    if detail.startswith("content query error"):
        return ReplayResult(INCONCLUSIVE, detail[:120], ran)
    if expect.absent:
        ok = not ok
    return ReplayResult(HOLDS if ok else VIOLATED, detail[:160], ran)


async def _pass(serial: str, bundle: str, claim: Claim, flags: Sequence[str],
                snap: "Path | None", attempts: int = 2,
                shared: Sequence[str] | None = None,
                shared_snap: "Path | None" = None,
                device_setup: dict | None = None) -> ReplayResult:
    """One reset-and-replay, retried only while INCONCLUSIVE (a harness statement).
    HOLDS, VIOLATED and CRASHED are evidence and are NEVER retried — a retried crash
    is a crash the reset loop swallows. A retry bumps the earliest un-bumped ambiguous
    anchor to its next candidate, never repeating the same tap."""
    best = ReplayResult(INCONCLUSIVE, "not run")
    choices: dict[int, int] = {}
    for attempt in range(attempts):
        await _reset(serial, bundle, flags, snap, shared, shared_snap,
                     device_setup=device_setup)
        result = await replay(serial, bundle, claim.steps, claim.expect,
                              choices=choices or None, seeded=flags)
        if result.outcome != INCONCLUSIVE:
            return result
        # Keep the attempt that got furthest — the artifact should show the
        # most-progressed failure point.
        if result.steps_run >= best.steps_run:
            best = result
        pending = [i for i in result.ambiguous
                   if i <= result.steps_run and i not in choices]
        if pending:
            choices[pending[0]] = 1
        logger.info("%s: inconclusive (%s) — attempt %d/%d%s",
                    claim.area, result.detail, attempt + 1, attempts,
                    f"; retrying with choices {choices}" if pending else "")
    return best


async def differential(serial: str, bundle: str, claim: Claim,
                       seeded_bug_ids: Sequence[str],
                       snap: Path | None = None,
                       shared: Sequence[str] | None = None,
                       shared_snap: Path | None = None,
                       device_setup: dict | None = None) -> DifferentialResult:
    """Replay one claim with the seeded defects ON and then OFF. `shared`/`shared_snap`
    restore /sdcard content between passes — pm clear does not touch it, and without
    the restore the difference this layer reads is contaminated.

    CRASHED is read exactly like VIOLATED in every branch: ON crashed + OFF holds is
    CONFIRMED / MISSED_DEFECT by what the agent said; crashed or violated on BOTH
    builds is NOT_A_DEFECT; ON crashed + OFF inconclusive is REPRODUCED_SEEDED when
    the agent said broken."""
    if not claim.replayable:
        return DifferentialResult(claim.area, claim.verdict, UNREPLAYABLE)

    await disable_animations(serial)

    said_broken = claim.verdict == "deviates"

    on = await _pass(serial, bundle, claim, seeded_bug_ids, snap,
                     shared=shared, shared_snap=shared_snap,
                     device_setup=device_setup)
    if on.outcome == INCONCLUSIVE:
        return DifferentialResult(claim.area, claim.verdict, UNREPLAYABLE, on)

    if on.outcome == HOLDS:
        # The area behaves as specified on the build the agent tested: a working
        # claim is confirmed (no second replay needed), a broken claim is not
        # visible in its own repro.
        return DifferentialResult(
            claim.area, claim.verdict,
            DOES_NOT_REPRODUCE if said_broken else CONFIRMED_WORKING, on)

    off = await _pass(serial, bundle, claim, [], snap,
                      shared=shared, shared_snap=shared_snap,
                      device_setup=device_setup)
    if off.outcome == INCONCLUSIVE:
        if said_broken and on.outcome in _BROKEN:
            return DifferentialResult(claim.area, claim.verdict, REPRODUCED_SEEDED, on, off)
        return DifferentialResult(claim.area, claim.verdict, UNREPLAYABLE, on, off)

    if off.outcome in _BROKEN:
        # Broken (or crashing) with and without the seeding: upstream behaviour, not a
        # defect this benchmark introduced — crediting it would reward a find that
        # isn't one.
        return DifferentialResult(claim.area, claim.verdict, NOT_A_DEFECT, on, off)

    # The seeding is what broke it; hit or miss depends on what the agent said.
    return DifferentialResult(
        claim.area, claim.verdict,
        CONFIRMED if said_broken else MISSED_DEFECT, on, off)


async def replay_episode(serial: str, bundle: str, claims: Sequence[Claim],
                         seeded_bug_ids: Sequence[str],
                         progress=None, snap: Path | None = None,
                         shared: Sequence[str] | None = None,
                         shared_snap: Path | None = None,
                         device_setup: dict | None = None) -> list[DifferentialResult]:
    """Every replayable claim in one episode, sequentially on one device."""
    out: list[DifferentialResult] = []
    for i, claim in enumerate(claims, 1):
        res = await differential(serial, bundle, claim, seeded_bug_ids, snap,
                                 shared, shared_snap, device_setup=device_setup)
        out.append(res)
        if progress:
            progress(i, len(claims), res)
    return out
