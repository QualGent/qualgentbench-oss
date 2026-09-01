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
from .verify.device import (_adb, _DISMISS_LABELS, _dismiss_overlays, append_text,
                            disable_animations, dump_vh,
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
    "verify/device.py", "verify/match.py",
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


def _present(xml: str, text: str) -> bool:
    """Is `text` on screen as a value in its own right? Whole-token, not substring —
    `20` must not match `200`, or a real defect reads as does-not-reproduce. A boundary
    is any non-alphanumeric char, so `144` still matches `12x12=144`. Exact first."""
    root = parse_vh(xml)
    if root is None:
        return False
    want = text.strip()
    if not want:
        return False
    token = re.compile(rf"(?<![0-9A-Za-z]){re.escape(want)}(?![0-9A-Za-z])", re.I)
    for node in root.iter():
        for key in ("text", "content-desc"):
            value = (node.get(key) or "").strip()
            if not value:
                continue
            if value.casefold() == want.casefold() or token.search(value):
                return True
    return False


def _candidates(xml: str, text: str) -> list[dict]:
    """Plausible elements for `text`, best first. Exact outranks substring (or the
    replayer drives a different app than the agent did); a bare resource-id ranks last.
    Ties break by smallest clickable-ancestor area — the more specific control wins."""
    root = parse_vh(xml)
    if root is None:
        return []
    pmap = parent_map(root)
    want = text.strip().lower()
    found: list[dict] = []
    for order, node in enumerate(root.iter()):
        label_text = (node.get("text") or "").strip().lower()
        label_desc = (node.get("content-desc") or "").strip().lower()
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
        })
    found.sort(key=lambda c: c["key"])
    return found


def _target(xml: str, text: str) -> tuple[int, int] | None:
    """Centre of the best element for `text`, or None."""
    cands = _candidates(xml, text)
    return cands[0]["centre"] if cands else None


async def _tap_any(serial: str, text: str, hold_ms: int = 0,
                   attempts: int = 3, choice: int = 0) -> tuple[bool, int]:
    """Tap, or long-press via a zero-distance swipe (`input tap` has no duration arg).
    `tied` > 1 means this step CHOSE among candidates; `choice` picks another on retry.
    The anchor is looked up several times — the screen may not have painted yet."""
    cands: list[dict] = []
    for attempt in range(attempts):
        xml = await dump_vh(serial)
        cands = _candidates(xml, text) if xml else []
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

    def as_dict(self) -> dict:
        out = {"outcome": self.outcome, "detail": self.detail,
               "steps_run": self.steps_run}
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


async def _swipe(serial: str, direction: str) -> None:
    # Mid-screen drags, deliberately short of the edges so a gesture-navigation build
    # does not read them as system back/home.
    moves = {"up": (540, 1500, 540, 600), "down": (540, 600, 540, 1500),
             "left": (900, 1000, 200, 1000), "right": (200, 1000, 900, 1000)}
    x1, y1, x2, y2 = moves[direction.lower()]
    await _adb(serial, "shell", "input", "swipe",
               str(x1), str(y1), str(x2), str(y2), "300")


async def run_steps(serial: str, bundle: str, steps: Sequence[Step],
                    choices: dict[int, int] | None = None) -> ReplayResult:
    """Execute steps without evaluating anything: HOLDS = every step ran, INCONCLUSIVE
    names the one that could not. Split out so `check_setup:` uses the SAME executor.
    `choices` maps a step index to a non-default anchor candidate for retries."""
    choices = choices or {}
    ambiguous: list[int] = []
    dismissed: list[str] = []
    reissued: list[int] = []
    back_noops: list[int] = []
    # Android drops touches during relayout; a swallowed gesture surfaces one step
    # later as a missing anchor. If the previous anchor still sits at the exact same
    # coordinates, the gesture is re-issued once — a landed one would have moved the UI.
    last_gesture: tuple[int, str, int, tuple[int, int]] | None = None

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
                auto = await relaunch(serial, bundle)
                # isinstance: tests stub relaunch with a bare truthy return.
                if isinstance(auto, list):
                    dismissed.extend(auto)
            elif step.action == "wait":
                await wait_stable(serial)
            elif step.action in ("tap", "long_press"):
                hold = 900 if step.action == "long_press" else 0
                tapped, tied, centre = await _tap_any(serial, step.value,
                                                      hold_ms=hold,
                                                      choice=choices.get(index, 0))
                if (not tapped and last_gesture is not None
                        and last_gesture[0] == index - 1
                        and last_gesture[0] not in reissued):
                    p_idx, p_text, p_hold, p_centre = last_gesture
                    xml = await dump_vh(serial)
                    still_there = xml and any(
                        c["centre"] == p_centre for c in _candidates(xml, p_text))
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
                            choice=choices.get(index, 0))
                if not tapped and step.value.strip().lower() not in _DISMISS_LABELS:
                    auto = await _dismiss_overlays(serial, rounds=1)
                    if auto:
                        dismissed.extend(auto)
                        logger.info("step %d: dismissed overlay %r revealed by a "
                                    "missing anchor %r", index + 1, auto, step.value)
                        await wait_stable(serial)
                        tapped, tied, centre = await _tap_any(
                            serial, step.value, hold_ms=hold,
                            choice=choices.get(index, 0))
                if tied > 1:
                    ambiguous.append(index)
                if not tapped:
                    # A missing anchor is NOT a failed expectation — the repro could
                    # not be carried out, which is non-punitive.
                    return _done(ReplayResult(INCONCLUSIVE,
                                              f"step {ran + 1}: no element matching "
                                              f"{step.value!r}", ran))
                last_gesture = (index, step.value, hold, centre)
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
            else:
                return _done(ReplayResult(INCONCLUSIVE,
                                          f"unknown action {step.action}", ran))
            ran += 1
            await asyncio.sleep(_SETTLE_S)
        except Exception as exc:  # noqa: BLE001 — a replayer fault is never the agent's
            return _done(ReplayResult(INCONCLUSIVE, f"step {ran + 1}: {exc}", ran))

    await wait_stable(serial)
    return _done(ReplayResult(HOLDS, "", ran))


async def replay(serial: str, bundle: str, steps: Sequence[Step],
                 expect: Expectation,
                 choices: dict[int, int] | None = None) -> ReplayResult:
    """Execute one reproduction and evaluate its post-condition."""
    result = await run_steps(serial, bundle, steps, choices=choices)
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
    """Read the app's own database via `run-as` (debug builds only). A SQL error is
    INCONCLUSIVE, never VIOLATED — a mistyped table name must not read as a broken app.
    An absolute `db` path is a file the SHELL user can read directly (the external app
    dir, where run-as has no storage access — AnkiDroid's collection.anki2)."""
    if expect.db.startswith("/"):
        cmd = f"sqlite3 {shlex.quote(expect.db)} {shlex.quote(expect.query)}"
    else:
        cmd = (f"run-as {shlex.quote(bundle)} sqlite3 databases/{shlex.quote(expect.db)} "
               f"{shlex.quote(expect.query)}")
    rc, out = await _adb(serial, "shell", cmd)
    text = out.decode("utf-8", "replace").strip()
    if rc != 0 or text.lower().startswith("error") or "no such" in text.lower():
        return ReplayResult(INCONCLUSIVE, f"db query failed: {text[:120]}", ran)
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
    HOLDS and VIOLATED are evidence and are NEVER retried. A retry bumps the earliest
    un-bumped ambiguous anchor to its next candidate, never repeating the same tap."""
    best = ReplayResult(INCONCLUSIVE, "not run")
    choices: dict[int, int] = {}
    for attempt in range(attempts):
        await _reset(serial, bundle, flags, snap, shared, shared_snap,
                     device_setup=device_setup)
        result = await replay(serial, bundle, claim.steps, claim.expect,
                              choices=choices or None)
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
    the restore the difference this layer reads is contaminated."""
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
        if said_broken and on.outcome == VIOLATED:
            return DifferentialResult(claim.area, claim.verdict, REPRODUCED_SEEDED, on, off)
        return DifferentialResult(claim.area, claim.verdict, UNREPLAYABLE, on, off)

    if off.outcome != HOLDS:
        # Broken with and without the seeding: upstream behaviour, not a defect this
        # benchmark introduced — crediting it would reward a find that isn't one.
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
