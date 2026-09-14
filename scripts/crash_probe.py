#!/usr/bin/env python3
"""Live probe for the replayer's crash check — the device half the unit tests cannot
cover (2026-09-14: `device_time` reached toybox as two arguments and every crash window
was silently disabled; only this probe showed it).

Drives `replay.run_steps` on a real, installed app and induces crashes with
`adb shell am crash <package>` between steps:

  a. the app under test dies before a tap      -> CRASHED, `crash.process` == the app
  b. a FOREIGN app dies, anchor missing anyway -> INCONCLUSIVE + "(foreign crash in
     <process> ignored)" in the detail, never charged
  c. a clean pass with a `relaunch` step       -> HOLDS: the harness's own force-stops
     (USER_REQUESTED/FORCE_STOP exit-info rows) are never read as a crash

Read-only on log buffers (`logcat -T` windows, no `-c`). Leaves the app under test
relaunched in the foreground and force-stops the foreign app it launched.

    uv run python scripts/crash_probe.py --serial emulator-5554 \\
        --app com.futsch1.medtimer --anchor Medicine --foreign com.minar.birday.debug
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import replay as rp                                    # noqa: E402
from qualgentbench.submission import Step                                  # noqa: E402
from qualgentbench.verify.crash import exit_info, is_after                 # noqa: E402
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


async def show_exit_info(serial: str, app: str, since: str) -> None:
    rows = [e for e in await exit_info(serial, app) if is_after(e.timestamp, since)]
    print(f"   exit-info rows for {app} since {since}: "
          f"{[(e.reason, e.sub_reason, e.timestamp) for e in rows] or 'none'}")


async def case(serial: str, app: str, label: str, steps, expect_outcome: str,
               crash_pkg: str | None = None) -> tuple[bool, rp.ReplayResult]:
    print(f"\n=== {label} ===")
    since = await rp.crash_window(serial)
    restore = arm(serial, crash_pkg) if crash_pkg else (lambda: None)
    try:
        res = await rp.run_steps(serial, app, steps)
    finally:
        restore()
    print(f"   outcome={res.outcome!r} steps_run={res.steps_run} detail={res.detail!r}")
    print(f"   crash={res.crash}")
    await show_exit_info(serial, app, since)
    ok = res.outcome == expect_outcome
    print(f"   {'PASS' if ok else 'FAIL'}: expected {expect_outcome!r}")
    return ok, res


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--app", default="com.futsch1.medtimer", help="installed app under test")
    ap.add_argument("--anchor", default="Medicine", help="a tap anchor on the app's landing screen")
    ap.add_argument("--foreign", default="", help="another installed app to crash for case b (skipped if empty)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    serial, app = args.serial, args.app

    await rp.disable_animations(serial)
    results: list[tuple[str, bool]] = []

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
