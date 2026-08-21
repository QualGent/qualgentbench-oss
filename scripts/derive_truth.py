#!/usr/bin/env python3
"""Derive each area's true state by execution and diff it against the spec.
Runs each `check:` with the seeded defects on and off and reads the label off
the difference; a disagreement means either the key or the check is wrong."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import replay as rp, truth          # noqa: E402
from qualgentbench.bugs import load_apps, load_suite   # noqa: E402
from qualgentbench.episode_runner import run_device_setup  # noqa: E402
from qualgentbench.verify.device import (_adb, grant_requested_permissions,  # noqa: E402
                                         relaunch, reset_dump_source, wait_stable)

_SPECS = Path(__file__).parents[1] / "src" / "qualgentbench" / "data" / "benchmarks"

_MARK = {truth.BROKEN: "broken", truth.OK: "ok", truth.UPSTREAM: "upstream",
         truth.INVERTED: "INVERTED", truth.UNDECIDABLE: "undecidable"}


async def derive_one(app_id: str, device: str, tmp: Path) -> list[truth.Derived]:
    suite = load_suite(_SPECS / f"{app_id}.yaml")
    bundle = suite["app"]["package"]
    features = suite["exploration"]["features"]
    seeded = [str(f["bug_id"]) for f in features
              if str(f.get("state")) == "broken" and f.get("bug_id")]
    have = [f for f in features if f.get("check")]

    print(f"\n{app_id} ({bundle}) · {len(have)}/{len(features)} areas have a check "
          f"· {len(seeded)} seeded defect(s)")
    if not have:
        print("  no `check:` blocks yet — nothing to derive")
        return []

    # One snapshot per app, so every check starts from the same state and a check
    # cannot be perturbed by whatever the previous one left behind.
    reset_dump_source(device)
    await _adb(device, "shell", f"pm clear {bundle}")
    await grant_requested_permissions(device, bundle)
    # Wipe shared storage BEFORE check_setup — pm clear does not touch /sdcard,
    # and the snapshot below would freeze leftover state into every check.
    shared = rp.safe_shared_paths(suite.get("shared_storage"))
    for path in shared:
        await _adb(device, "shell", f"rm -rf {shlex.quote(path)}")
        await _adb(device, "shell", f"mkdir -p {shlex.quote(path)}")
    # Setup runs on the CLEAN build: a seeded defect in its path would change the
    # state every check starts from. device_setup runs here in episode order so
    # derived truth starts from the same state an episode does.
    await run_device_setup(device, suite.get("device_setup"))
    await rp.set_flags(device, bundle, [])
    await relaunch(device, bundle)
    await asyncio.sleep(3.0)
    await wait_stable(device)

    # Harness-only: get past first-run onboarding once so the snapshot carries the
    # post-onboarding state. Retried from scratch — a single flake here costs the
    # whole app, not one label.
    setup = truth.setup_of(suite["exploration"])
    for attempt in range(2 if setup else 0):
        got = await rp.run_steps(device, bundle, setup)
        print(f"  check_setup: {got.steps_run}/{len(setup)} steps"
              f"{'' if got.outcome == rp.HOLDS else '  FAILED: ' + got.detail}")
        if got.outcome == rp.HOLDS:
            break
        if attempt + 1 >= 2:
            print("  every check would start from the wrong screen — aborting this app")
            return []
        print("  retrying check_setup from a fresh install state")
        await _adb(device, "shell", f"pm clear {bundle}")
        await grant_requested_permissions(device, bundle)
        for path in shared:
            await _adb(device, "shell", f"rm -rf {shlex.quote(path)}")
            await _adb(device, "shell", f"mkdir -p {shlex.quote(path)}")
        await run_device_setup(device, suite.get("device_setup"))
        await rp.set_flags(device, bundle, [])
        await relaunch(device, bundle)
        await asyncio.sleep(3.0)
        await wait_stable(device)

    snap = tmp / f"{app_id}.tar"
    if not await rp.snapshot(device, bundle, snap):
        print("  WARNING: app-data snapshot empty; checks may not be deterministic")
        snap = None

    # Snapshotted AFTER check_setup, restored per check so pass two cannot inherit
    # pass one's edits. restore_shared: false skips the per-pass restore for static
    # staged content that must not be re-extracted (re-indexing side effects).
    restore_shared = suite.get("restore_shared", True)
    shared_snap = tmp / f"{app_id}-shared.tar" if (shared and restore_shared) else None
    if shared_snap is not None:
        if not await rp.snapshot_shared(device, shared, shared_snap):
            print(f"  WARNING: shared-storage snapshot empty for {shared}")
            shared_snap = None
    elif shared:
        print(f"  shared storage {shared} wiped but NOT restored per pass "
              f"(restore_shared: false)")

    def progress(i, n, got):
        flag = "" if got.agrees else f"   <-- SPEC SAYS {got.declared}"
        print(f"  [{i:2}/{n}] {got.area:22} derived {_MARK[got.derived]:12}{flag}")

    return await truth.derive_app(device, bundle, have, seeded, snap, progress,
                                  shared, shared_snap)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apps", nargs="*", help="app ids; omit with --tier")
    ap.add_argument("--tier", help="derive every ready app in this tier")
    ap.add_argument("--device", default="emulator-5554")
    ap.add_argument("--json", help="write the full result here")
    ap.add_argument("--repeat", type=int, default=1,
                    help="derive this many times and require every run to agree. "
                         "A check that is not STABLE is not an oracle: its flip rate "
                         "is the replayer's own error rate, measured rather than "
                         "assumed, and an unstable check must leave the corpus.")
    args = ap.parse_args()

    app_ids = list(args.apps)
    if args.tier:
        app_ids += [a["app"]["id"] for a in load_apps()
                    if a["app"].get("difficulty") == args.tier]
    if not app_ids:
        ap.error("name at least one app, or pass --tier")

    # Disposable scratch for the per-pass .tar snapshots.
    tmp = Path(__file__).parents[1] / "runs" / "_derive_scratch"
    tmp.mkdir(parents=True, exist_ok=True)

    # The result is the measured answer key, a corpus artifact — default it beside
    # the specs in version control, not under gitignored runs/ where deleting run
    # output would silently destroy it.
    out_json = args.json
    if not out_json and args.tier:
        dest = (Path(__file__).parents[1] / "src" / "qualgentbench" / "data" / "truth")
        dest.mkdir(parents=True, exist_ok=True)
        out_json = str(dest / f"{args.tier}-stability.json")
    everything: dict[str, list[dict]] = {}
    unstable: list[str] = []
    for app_id in dict.fromkeys(app_ids):
        # The APK is assumed installed — deriving truth is about the seeding,
        # not the install.
        passes = []
        for attempt in range(args.repeat):
            if args.repeat > 1:
                print(f"\n--- {app_id} pass {attempt + 1}/{args.repeat} ---")
            passes.append({r.area: r for r in await derive_one(app_id, args.device, tmp)})
        first = passes[0]
        for area, row in first.items():
            labels = {p[area].derived for p in passes if area in p}
            if len(labels) > 1:
                unstable.append(f"{app_id}/{area}: {sorted(labels)}")
        everything[app_id] = [r.as_dict() for r in first.values()]

    flat = [r for rows in everything.values() for r in rows]
    if flat:
        agree = sum(1 for r in flat if r["agrees"])
        print(f"\n{'=' * 62}\n{agree}/{len(flat)} derived labels agree with the spec")
        bad = [r for r in flat if not r["agrees"]]
        for r in bad:
            print(f"  DISAGREE  {r['area']:22} spec={r['declared']:10} "
                  f"derived={r['derived']}")
        if any(r["derived"] == truth.INVERTED for r in flat):
            print("  INVERTED areas mean the seeding FIXES the area — no score from "
                  "this app is safe until that is resolved.")
    if args.repeat > 1:
        total = sum(len(v) for v in everything.values())
        print(f"\nstability: {total - len(unstable)}/{total} checks gave the SAME label "
              f"in all {args.repeat} passes")
        for u in unstable:
            print(f"  UNSTABLE  {u}")
    if out_json:
        Path(out_json).write_text(json.dumps(everything, indent=2))
        print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
