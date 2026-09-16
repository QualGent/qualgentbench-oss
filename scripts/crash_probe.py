#!/usr/bin/env python3
"""Live probe for the replayer's crash check — the device half the unit tests cannot
cover (2026-09-14: `device_time` reached toybox as two arguments and every crash window
was silently disabled; only this probe showed it).

Default mode drives `replay.run_steps` on a real, installed app and induces crashes
with `adb shell am crash <package>` between steps:

  a. the app under test dies before a tap      -> CRASHED, `crash.process` == the app
  b. a FOREIGN app dies, anchor missing anyway -> INCONCLUSIVE + "(foreign crash in
     <process> ignored)" in the detail, never charged
  c. a clean pass with a `relaunch` step       -> HOLDS: the harness's own force-stops
     (USER_REQUESTED/FORCE_STOP exit-info rows) are never read as a crash

`--anr` mode freezes the app under test with `run-as <pkg> kill -STOP <pid>` (the
build must be debuggable) and lets the replayer's OWN tap be the unanswered input —
the dispatcher only ANRs on an input nobody answers, so a frozen app with no tap is
not an ANR:

  d. anchor found -> app frozen -> the replayer's `input tap` is swallowed (it blocks
     ~30 s on the injection timeout) -> the next step's anchor is missing (the
     hierarchy holds only the "isn't responding" dialog) -> CRASHED, crash.kind "anr"
  e. app frozen and resumed BEFORE its tap is issued -> HOLDS, no ANR recorded
  c. the clean pass again                              -> HOLDS, crash None

`--stuck` mode proves the `{stuck: "<anchor>"}` expectation end to end through
`replay.replay()`. A hung app that receives NO further input never ANRs, so a freeze
on the LAST route step is invisible to the route itself — the probe's one tap is what
makes the dispatcher decide:

  f. frozen inside the route's final `wait` (after the last tap landed) -> the route
     ends HOLDS (no ANR: nothing was sent), the stuck probe resolves its anchor from
     the route's last readable screen (a frozen app's hierarchy cannot be dumped),
     sends ONE tap, the dispatcher gives up at the ANR deadline -> CRASHED, kind "anr"
  g. the same route, app live -> the probe's tap is answered in milliseconds -> HOLDS
  Both cases assert that exactly one probe tap was issued.

Both freeze modes here induce the hang by hand (`kill -STOP`), which is the point: they
test the DETECTOR without depending on any app's code. Since QUA-2711 the corpus also
carries two SEEDED hangs that exercise the same two paths from real code — MedTimer's
`overview-action-blocks-main-thread` (blocks in a click handler, so the route's next
touch ANRs) and `analysis-table-freezes-on-open` (blocks a frame later, so nothing is
pending and only this probe's tap reveals it). Use those to check a change to the
oracles end to end; use this script when the corpus itself is what you doubt.

Read-only on log buffers (`logcat -T` windows, no `-c`). Always resumes a frozen
process (`kill -CONT`; the dialog withdraws itself), leaves the app under test
relaunched in the foreground and force-stops the foreign app it launched.

    uv run python scripts/crash_probe.py --serial emulator-5554 \\
        --app com.futsch1.medtimer --anchor Medicine --foreign com.minar.birday.debug
    uv run python scripts/crash_probe.py --anr --serial emulator-5554 --app com.futsch1.medtimer
    uv run python scripts/crash_probe.py --stuck --serial emulator-5554 --app com.futsch1.medtimer
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import replay as rp                                    # noqa: E402
from qualgentbench.submission import Step, _parse_expect                   # noqa: E402
from qualgentbench.verify.crash import (anr_timeout_ms, anrs_since, exit_info,   # noqa: E402
                                        is_after, unresponsive_windows)
from qualgentbench.verify.device import _adb, current_activity, relaunch   # noqa: E402


async def am_crash(serial: str, pkg: str) -> None:
    rc, out = await _adb(serial, "shell", "am", "crash", pkg)
    print(f"   >> am crash {pkg}: rc={rc} {out.decode(errors='replace').strip()[:100]!r}")


def arm(serial: str, pkg: str, on_call: int = 1):
    """Make the `on_call`-th `_tap_any` crash `pkg` right before its anchor lookup —
    i.e. between the previous step and that tap. Returns the restore callable."""
    real = rp._tap_any
    state = {"n": 0}

    async def wrapped(serial_: str, text: str, **kw):
        state["n"] += 1
        if state["n"] == on_call:
            await am_crash(serial, pkg)
            await asyncio.sleep(2.0)
        return await real(serial_, text, **kw)
    rp._tap_any = wrapped
    return lambda: setattr(rp, "_tap_any", real)


# ---------------------------------------------------------------- ANR (freeze) mode

async def _pidof(serial: str, pkg: str) -> str:
    _, out = await _adb(serial, "shell", "pidof", pkg)
    return out.decode(errors="replace").split()[0] if out.strip() else ""


async def freeze_app(serial: str, pkg: str) -> str:
    """SIGSTOP the app's process as the app itself (`run-as`). Returns the pid, or ""
    when the build is not debuggable / run-as is refused."""
    pid = await _pidof(serial, pkg)
    if not pid:
        print(f"   >> freeze: {pkg} is not running")
        return ""
    rc, out = await _adb(serial, "shell", "run-as", pkg, "kill", "-STOP", pid)
    _, stat = await _adb(serial, "shell", "cat", f"/proc/{pid}/stat")
    state = stat.decode(errors="replace").split()[2] if stat.strip() else "?"
    print(f"   >> run-as {pkg} kill -STOP {pid}: rc={rc} {out.decode(errors='replace').strip()[:80]!r}"
          f" proc state={state}")
    return pid if rc == 0 and state == "T" else ""


async def resume(serial: str, pkg: str, pid: str) -> None:
    rc, out = await _adb(serial, "shell", "run-as", pkg, "kill", "-CONT", pid)
    print(f"   >> run-as {pkg} kill -CONT {pid}: rc={rc} {out.decode(errors='replace').strip()[:80]!r}")


def arm_freeze(serial: str, pkg: str, since: str, on_call: int = 1,
               resume_after_s: float | None = None):
    """Freeze `pkg` inside the `on_call`-th `_gesture` — AFTER the replayer found the
    anchor, BEFORE it issues the `input tap` — so the replayer's own tap is the input
    the frozen main thread never answers. With `resume_after_s` the process is CONT'd
    before that tap goes out (case e). Returns an async restore that always resumes."""
    real = rp._gesture
    state: dict = {"n": 0, "pid": "", "sampler": None}

    async def sample() -> None:
        # Mid-freeze evidence, taken while the replayer's tap is still blocked.
        await asyncio.sleep(7.0)
        t = time.monotonic()
        rows = await unresponsive_windows(serial, pkg)
        recs = await anrs_since(serial, pkg, since)
        print(f"   [+7s into the freeze] dispatcher unresponsive windows: {rows or 'none'}; "
              f"ANR records since window: {[(r.timestamp, r.pid, r.message[:60]) for r in recs] or 'none'} "
              f"({time.monotonic() - t:.2f}s to read)")

    async def wrapped(serial_: str, centre, hold_ms: int = 0):
        state["n"] += 1
        if state["n"] == on_call:
            state["pid"] = await freeze_app(serial, pkg)
            if resume_after_s is not None:
                await asyncio.sleep(resume_after_s)
                if state["pid"]:
                    await resume(serial, pkg, state["pid"])
                    state["pid"] = ""
            elif state["pid"]:
                state["sampler"] = asyncio.ensure_future(sample())
        t = time.monotonic()
        try:
            return await real(serial_, centre, hold_ms)
        finally:
            if state["n"] == on_call:
                print(f"   >> the replayer's own `input tap` returned after {time.monotonic() - t:.1f}s")
    rp._gesture = wrapped

    async def restore() -> None:
        rp._gesture = real
        if state["sampler"] is not None:
            await state["sampler"]
        if state["pid"]:
            await resume(serial, pkg, state["pid"])
            await asyncio.sleep(3.0)   # the dialog withdraws itself once the app answers
    return restore


# ---------------------------------------------------------------- stuck (probe) mode

async def stuck_case(serial: str, app: str, label: str, anchor: str, freeze: bool,
                     expect_outcome: str) -> tuple[bool, rp.ReplayResult]:
    """Drive `replay.replay()` with `{stuck: anchor}` on `[launch, tap anchor, wait]`.
    With `freeze`, the app is SIGSTOPped inside the route's own `wait` step — after
    the last tap landed, before anything else is sent — so the probe's tap is the
    first input the frozen main thread never answers. Counts probe taps."""
    print(f"\n=== {label} ===")
    expect, err = _parse_expect({"stuck": anchor}, "probe", trusted=True)
    assert expect is not None, err
    real_wait, real_probe = rp.wait_stable, rp._probe_tap
    state: dict = {"waits": 0, "taps": 0, "pid": ""}

    async def wait_wrapped(serial_: str, timeout_s: int = 8):
        state["waits"] += 1
        if freeze and state["waits"] == 1:
            state["pid"] = await freeze_app(serial, app)
        return await real_wait(serial_, timeout_s)

    async def probe_wrapped(serial_: str, centre):
        state["taps"] += 1
        print(f"   >> probe tap #{state['taps']} at {centre}")
        return await real_probe(serial_, centre)

    rp.wait_stable, rp._probe_tap = wait_wrapped, probe_wrapped
    since = await rp.crash_window(serial)
    t0 = time.monotonic()
    try:
        res = await rp.replay(serial, app, [Step("launch"), Step("tap", anchor), Step("wait")], expect)
    finally:
        rp.wait_stable, rp._probe_tap = real_wait, real_probe
        if state["pid"]:
            await resume(serial, app, state["pid"])
            await asyncio.sleep(3.0)
            await _adb(serial, "shell", "am", "force-stop", app)   # drops the ANR dialog if it got up
    print(f"   outcome={res.outcome!r} steps_run={res.steps_run} detail={res.detail!r} ({time.monotonic() - t0:.0f}s)")
    print(f"   crash={res.crash} fired={res.fired}")
    print(f"   probe taps issued: {state['taps']}")
    recs = await anrs_since(serial, app, since)
    print(f"   ANR log records since window: {[(r.timestamp, r.pid, r.message[:70]) for r in recs] or 'none'}")
    print(f"   dispatcher unresponsive now: {await unresponsive_windows(serial, app) or 'none'}")
    ok = res.outcome == expect_outcome and state["taps"] == 1
    print(f"   {'PASS' if ok else 'FAIL'}: expected {expect_outcome!r} with exactly one probe tap")
    return ok, res


async def show_exit_info(serial: str, app: str, since: str) -> None:
    rows = [e for e in await exit_info(serial, app) if is_after(e.timestamp, since)]
    print(f"   exit-info rows for {app} since {since}: "
          f"{[(e.reason, e.sub_reason, e.timestamp) for e in rows] or 'none'}")


async def case(serial: str, app: str, label: str, steps, expect_outcome: str,
               crash_pkg: str | None = None, freeze_on_call: int | None = None,
               resume_after_s: float | None = None) -> tuple[bool, rp.ReplayResult]:
    print(f"\n=== {label} ===")
    since = await rp.crash_window(serial)
    restore_sync = arm(serial, crash_pkg) if crash_pkg else (lambda: None)
    restore_async = (arm_freeze(serial, app, since, freeze_on_call, resume_after_s)
                     if freeze_on_call else None)
    try:
        res = await rp.run_steps(serial, app, steps)
    finally:
        restore_sync()
        if restore_async is not None:
            await restore_async()
    print(f"   outcome={res.outcome!r} steps_run={res.steps_run} detail={res.detail!r}")
    print(f"   crash={res.crash} dismissed={res.dismissed}")
    await show_exit_info(serial, app, since)
    if freeze_on_call:
        recs = await anrs_since(serial, app, since)
        print(f"   ANR log records since window: {[(r.timestamp, r.pid, r.message[:70]) for r in recs] or 'none'}")
        print(f"   dispatcher unresponsive now: {await unresponsive_windows(serial, app) or 'none'}")
    ok = res.outcome == expect_outcome
    print(f"   {'PASS' if ok else 'FAIL'}: expected {expect_outcome!r}")
    return ok, res


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--app", default="com.futsch1.medtimer", help="installed app under test")
    ap.add_argument("--anchor", default="Medicine", help="a tap anchor on the app's landing screen")
    ap.add_argument("--foreign", default="", help="another installed app to crash for case b (skipped if empty)")
    ap.add_argument("--anr", action="store_true", help="freeze the app (run-as kill -STOP) instead of crashing it")
    ap.add_argument("--stuck", action="store_true",
                    help="prove the {stuck: <anchor>} probe through replay.replay(): frozen -> CRASHED/anr, live -> HOLDS")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    serial, app = args.serial, args.app

    await rp.disable_animations(serial)
    results: list[tuple[str, bool]] = []

    if args.stuck:
        await relaunch(serial, app)
        print(f"effective input ANR timeout (dumpsys input): {await anr_timeout_ms(serial)} ms")
        pid = await freeze_app(serial, app)
        if not pid:
            print("run-as kill -STOP refused: cannot induce a hang without privileges on this build")
            return 2
        await resume(serial, app, pid)

        ok, res = await stuck_case(serial, app, "f. frozen after the last step: the probe's tap is the only input",
                                   args.anchor, freeze=True, expect_outcome=rp.CRASHED)
        ok = ok and res.crash is not None and res.crash["kind"] == "anr" \
            and "stuck probe" in res.detail and res.crash.get("probe", {}).get("anchor") == args.anchor
        results.append(("f", ok))
        await relaunch(serial, app)
        await asyncio.sleep(2.0)

        ok, res = await stuck_case(serial, app, "g. the same route, app live: the probe is answered",
                                   args.anchor, freeze=False, expect_outcome=rp.HOLDS)
        ok = ok and res.crash is None and "answered in" in res.detail
        results.append(("g", ok))

        await relaunch(serial, app)
        print(f"\nforeground now: {await current_activity(serial)}")
        print("summary:", results)
        return 0 if all(ok for _, ok in results) else 1

    if args.anr:
        await relaunch(serial, app)
        print(f"effective input ANR timeout (dumpsys input): {await anr_timeout_ms(serial)} ms")
        pid = await freeze_app(serial, app)
        if not pid:
            print("run-as kill -STOP refused: cannot induce an ANR without privileges on this build")
            return 2
        await resume(serial, app, pid)

        ok, res = await case(serial, app, "d. ANR: frozen after the anchor was found, before its tap",
                             [Step("launch"), Step("tap", args.anchor), Step("tap", args.anchor),
                              Step("tap", args.anchor), Step("wait")],
                             rp.CRASHED, freeze_on_call=2)
        ok = ok and res.crash is not None and res.crash["kind"] == "anr" \
            and res.crash["process"] == app \
            and res.crash["signature"].startswith("ANR@Input dispatching timed out (") \
            and "stopped responding (ANR)" in res.detail and res.dismissed == []
        results.append(("d", ok))

        ok, res = await case(serial, app, "e. frozen and resumed before any tap: no ANR",
                             [Step("launch"), Step("tap", args.anchor), Step("wait")],
                             rp.HOLDS, freeze_on_call=1, resume_after_s=3.0)
        ok = ok and res.crash is None
        results.append(("e", ok))
    else:
        ok, res = await case(serial, app, "a. app crash before the tap",
                             [Step("launch"), Step("tap", args.anchor), Step("wait")],
                             rp.CRASHED, crash_pkg=app)
        ok = ok and res.crash is not None and res.crash["process"] == app \
            and f"{app} crashed" in res.detail and res.crash["signature"] in res.detail
        results.append(("a", ok))

        if args.foreign:
            await _adb(serial, "shell", "monkey", "-p", args.foreign,
                       "-c", "android.intent.category.LAUNCHER", "1")
            await asyncio.sleep(3.0)
            ok, res = await case(serial, app, "b. foreign crash, app under test healthy",
                                 [Step("launch"), Step("tap", "QGB-NoSuchAnchor"), Step("wait")],
                                 rp.INCONCLUSIVE, crash_pkg=args.foreign)
            ok = ok and f"(foreign crash in {args.foreign} ignored)" in res.detail and res.crash is None
            results.append(("b", ok))
            await _adb(serial, "shell", "am", "force-stop", args.foreign)
        else:
            print("\n=== b. skipped: no --foreign app given ===")

    ok, res = await case(serial, app, "c. no crash, with a relaunch step",
                         [Step("launch"), Step("tap", args.anchor), Step("relaunch"),
                          Step("tap", args.anchor), Step("wait")],
                         rp.HOLDS)
    ok = ok and res.crash is None and "foreign" not in res.detail
    results.append(("c", ok))

    await relaunch(serial, app)
    print(f"\nforeground now: {await current_activity(serial)}")
    print("summary:", results)
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
