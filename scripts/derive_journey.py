#!/usr/bin/env python3
"""Confirm an app's journey test cases by execution — the corpus gate, run once per
app (and again only when a case, a defect or the APK changes).

For every case in data/test-cases/<app>.yaml the harness-only `check:` runs twice:
  1. clean   — no defect on. The case must PASS: this is the clean version every
               agent runs, and a clean version that cannot pass fails every agent.
  2. seeded  — exactly the case's `bugs:` on. The measured verdict must match what
               the list implies (a functional bug → FAIL, display bugs only → PASS).
After every step the screen text is dumped; the difference between the two runs is
what the bugs changed on this route. Each display bug's `marker` must be in that
difference — otherwise it is not visible on the route and the case is wrong, not the
agent. The strings found are recorded and become the matcher's first signal.

Output: data/truth/journey-<app>.json (or --json). Every DISAGREE printed means the
case, the seeding or the marker is wrong — fix the YAML, never the JSON.
The APK is assumed installed (`adb install -r -g dist/<app>/buggy.apk`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import journey, replay as rp, truth             # noqa: E402
from qualgentbench.bugs import load_suite                          # noqa: E402
from qualgentbench.episode_runner import run_device_setup          # noqa: E402
from qualgentbench.submission import Claim, _parse_expect          # noqa: E402
from qualgentbench.verify.device import (_adb, append_text, dump_vh,   # noqa: E402
                                         grant_requested_permissions, ime_shown,
                                         relaunch, reset_dump_source, wait_stable)
from qualgentbench.verify.match import visible_texts               # noqa: E402

ROOT = Path(__file__).parents[1]
_SPECS = ROOT / "src" / "qualgentbench" / "data" / "benchmarks"
_TRUTH = ROOT / "src" / "qualgentbench" / "data" / "truth"

_TEXT_LIMIT = 600

# Wall-clock stamps differ between ANY two runs (a saved entry carries the minute it
# was saved), so they can never be a symptom. Masked before diffing: a DATE, and a
# time only when it follows a date ("Sep 2, 2026 2:49 PM"). A standalone time stays —
# a reminder set for 8:00 AM that renders as 9:00 AM IS a symptom (MedTimer). Every
# other number stays too: "Avg: 76 kg" vs "Avg: 79 kg" is the signal.
_DATE_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:, \d{4})?"
                      r"(?:,? \d{1,2}:\d{2}(?::\d{2})?\s?(?:[AaPp][Mm])?)?\b")
_NUMDATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}(?:,? \d{1,2}:\d{2}(?::\d{2})?\s?(?:[AaPp][Mm])?)?\b")


def mask(text: str) -> str:
    return _NUMDATE_RE.sub("<date>", _DATE_RE.sub("<date>", text))


async def run_with_dumps(serial: str, bundle: str, steps) -> tuple[rp.ReplayResult, list[list[str]]]:
    """The replay executor's step loop, recording the screen after every step.

    The device sees EXACTLY the calls replay.run_steps makes: a tap step's screen is
    taken from the hierarchy dump the NEXT tap fetches for its own anchor lookup, not
    from an extra dump of our own (an extra dump between two taps once dismissed a
    popup menu and the second tap landed on a card label). Only a step followed by a
    non-tap step (type, press, swipe, wait) or the last step gets an explicit dump."""
    dumps: list[list[str]] = []
    pending = False
    real_dump = rp.dump_vh

    async def spy(serial_: str, retries: int = 3) -> str:
        nonlocal pending
        xml = await real_dump(serial_, retries)
        if pending and xml:
            dumps.append(visible_texts(xml, limit=_TEXT_LIMIT))
            pending = False
        return xml

    async def record_now() -> None:
        nonlocal pending
        if pending:
            xml = await real_dump(serial)
            dumps.append(visible_texts(xml, limit=_TEXT_LIMIT) if xml else [])
            pending = False

    rp.dump_vh = spy
    ran = 0
    try:
        for index, step in enumerate(steps):
            try:
                if step.action not in ("tap", "long_press"):
                    await record_now()
                if step.action in ("launch", "relaunch"):
                    await relaunch(serial, bundle)
                elif step.action == "wait":
                    await wait_stable(serial)
                elif step.action in ("tap", "long_press"):
                    hold = 900 if step.action == "long_press" else 0
                    tapped, _tied, _c = await rp._tap_any(serial, step.value, hold_ms=hold)
                    if not tapped and step.value.strip().lower() not in rp._DISMISS_LABELS:
                        if await rp._dismiss_overlays(serial, rounds=1):
                            await wait_stable(serial)
                            tapped, _tied, _c = await rp._tap_any(serial, step.value, hold_ms=hold)
                    if not tapped:
                        return rp.ReplayResult(rp.INCONCLUSIVE,
                                               f"step {ran + 1}: no element matching {step.value!r}",
                                               ran), dumps
                elif step.action == "type":
                    await rp._type_text(serial, step.value)
                elif step.action == "append":
                    await append_text(serial, step.value)
                elif step.action == "press":
                    if (step.value.strip().lower() == "back" and index > 0
                            and steps[index - 1].action == "type" and not await ime_shown(serial)):
                        pass
                    else:
                        await rp._press(serial, step.value)
                elif step.action == "swipe":
                    await rp._swipe(serial, step.value)
                else:
                    return rp.ReplayResult(rp.INCONCLUSIVE, f"unknown action {step.action}", ran), dumps
                ran += 1
                await asyncio.sleep(rp._SETTLE_S)
                pending = True
            except Exception as exc:  # noqa: BLE001
                return rp.ReplayResult(rp.INCONCLUSIVE, f"step {ran + 1}: {exc}", ran), dumps
        await wait_stable(serial)
        await record_now()
        return rp.ReplayResult(rp.HOLDS, "", ran), dumps
    finally:
        rp.dump_vh = real_dump


async def evaluate(serial: str, bundle: str, expect, ran: int) -> rp.ReplayResult:
    if expect.mode == "db":
        # Same cold read as the episode runner: a running AnkiDroid locks its collection.
        await rp._adb(serial, "shell", "am", "force-stop", bundle)
        await asyncio.sleep(1.0)
        return await rp._check_db(serial, bundle, expect, ran)
    if expect.mode == "file":
        return await rp._check_file(serial, bundle, expect, ran)
    if expect.mode == "content":
        return await rp._check_content(serial, bundle, expect, ran)
    xml = await dump_vh(serial)
    if not xml:
        return rp.ReplayResult(rp.INCONCLUSIVE, "could not read the UI hierarchy", ran)
    found = rp._present(xml, expect.text)
    holds = found if expect.mode == "present" else not found
    return rp.ReplayResult(rp.HOLDS if holds else rp.VIOLATED,
                           f"{expect.mode} {expect.text!r} → {'yes' if found else 'no'}", ran)


async def one_pass(serial, bundle, claim: Claim, flags, snap, shared, shared_snap,
                   device_setup, attempts: int = 2):
    res, dumps = rp.ReplayResult(rp.INCONCLUSIVE, "not run"), []
    for _ in range(attempts):
        await rp._reset(serial, bundle, flags, snap, shared, shared_snap, device_setup=device_setup)
        res, dumps = await run_with_dumps(serial, bundle, claim.steps)
        if res.outcome == rp.HOLDS:
            res = await evaluate(serial, bundle, claim.expect, res.steps_run)
        if res.outcome != rp.INCONCLUSIVE:
            break
    return res, dumps


def _claim(case: dict) -> Claim | None:
    raw = case.get("check")
    if not isinstance(raw, dict):
        return None
    steps = truth._steps(raw.get("steps"))
    expect, error = _parse_expect(raw.get("expect"), case["id"], trusted=True)
    if not steps or expect is None:
        print(f"  {case['id']}: unusable check — {error}")
        return None
    return Claim(area=case["id"], verdict="", steps=steps, expect=expect)


def _diff(off: list[list[str]], on: list[list[str]]) -> list[dict]:
    out = []
    for i in range(min(len(off), len(on))):
        a, b = {mask(t) for t in off[i]}, {mask(t) for t in on[i]}
        added, removed = sorted(b - a), sorted(a - b)
        if added or removed:
            out.append({"step": i + 1, "added": added, "removed": removed})
    return out


async def stage(serial: str, suite: dict, tmp: Path) -> tuple[Path | None, list[str], Path | None]:
    """Episode-identical staging, snapshotted once so every pass starts equal."""
    bundle = suite["app"]["package"]
    reset_dump_source(serial)
    await _adb(serial, "shell", f"pm clear {bundle}")
    await grant_requested_permissions(serial, bundle)
    shared = rp.safe_shared_paths(suite.get("shared_storage"))
    for path in shared:
        await _adb(serial, "shell", f"rm -rf {shlex.quote(path)}")
        await _adb(serial, "shell", f"mkdir -p {shlex.quote(path)}")
    await run_device_setup(serial, suite.get("device_setup"))
    await rp.set_flags(serial, bundle, [])
    await relaunch(serial, bundle)
    await asyncio.sleep(3.0)
    await wait_stable(serial)
    setup = truth.setup_of(suite["exploration"])
    if setup:
        got = await rp.run_steps(serial, bundle, setup)
        print(f"  check_setup: {got.steps_run}/{len(setup)} steps"
              f"{'' if got.outcome == rp.HOLDS else '  FAILED: ' + got.detail}")
    snap = tmp / f"{suite['app']['id']}-{serial}.tar"
    if not await rp.snapshot(serial, bundle, snap):
        print("  WARNING: app-data snapshot empty; passes may not be deterministic")
        snap = None
    shared_snap = None
    if shared and suite.get("restore_shared", True):
        shared_snap = tmp / f"{suite['app']['id']}-{serial}-shared.tar"
        if not await rp.snapshot_shared(serial, shared, shared_snap):
            shared_snap = None
    return snap, shared, shared_snap


async def derive_app(app_id: str, serial: str, only: set[str] | None, tmp: Path) -> dict:
    suite = load_suite(_SPECS / f"{app_id}.yaml")
    doc = journey.load_cases(app_id)
    if not doc:
        print(f"{app_id}: no test-case file"); return {}
    bundle = suite["app"]["package"]
    defects = journey.load_defects(doc)
    known = {b["id"] for b in suite.get("bugs", [])}
    if unknown := set(defects) - known:
        print(f"{app_id}: defects not in the benchmark spec: {sorted(unknown)}"); return {}
    cases = [c for c in doc.get("test_cases", []) if not only or c["id"] in only]
    print(f"\n{app_id} ({bundle}) · {len(cases)} test case(s) on {serial}")

    snap, shared, shared_snap = await stage(serial, suite, tmp)
    device_setup = suite.get("device_setup")
    out: dict = {}
    for case in cases:
        claim = _claim(case)
        if claim is None:
            continue
        design = journey.case_design(case, defects)
        problems: list[str] = []
        print(f"\n  [{case['id']}] {len(claim.steps)} steps · bugs {design['bugs'] or '-'} · seeded expects {design['expected']}")
        passes: dict[str, tuple[rp.ReplayResult, list[list[str]]]] = {}

        async def run(name: str, flags: list[str]):
            t0 = time.monotonic()
            res, dumps = await one_pass(serial, bundle, claim, flags, snap, shared,
                                        shared_snap, device_setup)
            passes[name] = (res, dumps)
            print(f"    {name:8} {res.outcome:12} {res.steps_run:2} steps "
                  f"{time.monotonic() - t0:4.0f}s  {res.detail}")

        await run("clean", [])
        if design["bugs"]:
            await run("seeded", design["bugs"])

        clean = passes["clean"]
        if clean[0].outcome != rp.HOLDS:
            problems.append(f"clean version does not pass its oracle ({clean[0].outcome}: "
                            f"{clean[0].detail}) — check or app broken upstream")
        measured, diff, side_out, unclaimed = None, [], [], []
        if "seeded" in passes:
            seeded = passes["seeded"]
            measured = {rp.HOLDS: "PASS", rp.VIOLATED: "FAIL"}.get(seeded[0].outcome, "undecidable")
            if measured != design["expected"]:
                problems.append(f"bugs {design['bugs']} imply {design['expected']}, measured {measured}")
            if clean[0].outcome != rp.INCONCLUSIVE and seeded[0].outcome != rp.INCONCLUSIVE:
                diff = _diff(clean[1], seeded[1])
            for s in design["side"]:
                marker = s["marker"]
                hits = [d["step"] for d in diff if marker and any(marker in t for t in d["added"] + d["removed"])]
                texts = sorted({t for d in diff for t in d["added"] + d["removed"] if marker and marker in t})
                if not marker:
                    problems.append(f"display bug {s['bug']} has no marker")
                elif not hits:
                    problems.append(f"display bug {s['bug']}: marker {marker!r} is not in the clean/seeded "
                                    f"screen diff — not visible on this route")
                side_out.append({"bug": s["bug"], "marker": marker, "visible_steps": hits, "texts": texts})
            unclaimed = [d for d in diff
                         if not any(s["marker"] and s["marker"] in t for s in design["side"]
                                    for t in d["added"] + d["removed"])]

        row = {
            "name": case.get("name"),
            "bugs": design["bugs"],
            "expected": design["expected"],
            "measured": measured,
            "blocking": design["blocking"],
            "side": side_out,
            "agrees": not problems,
            "problems": problems,
            "diff": diff,
            "unclaimed_diff": unclaimed,
            "passes": {k: {"outcome": v[0].outcome, "detail": v[0].detail,
                           "steps_run": v[0].steps_run} for k, v in passes.items()},
            "screens": {k: v[1] for k, v in passes.items()},
        }
        out[case["id"]] = row
        mark = "AGREES" if not problems else "DISAGREE"
        print(f"    => {mark}: clean {clean[0].outcome}"
              f"{'' if measured is None else ' · seeded ' + measured}"
              f" · side {[(s['bug'], s['visible_steps']) for s in side_out] or '-'}")
        for p in problems:
            print(f"       ! {p}")
        for d in unclaimed:
            print(f"       unclaimed diff @step {d['step']}: +{d['added'][:5]} -{d['removed'][:5]}")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apps", nargs="+", help="app ids with a data/test-cases/<app>.yaml")
    ap.add_argument("--device", default="emulator-5554")
    ap.add_argument("--case", action="append", help="derive only this case id (repeatable)")
    ap.add_argument("--json", help="write the result here (default data/truth/journey-<app>.json; "
                                   "merged with existing entries when --case is used)")
    args = ap.parse_args()
    tmp = ROOT / "runs" / "_derive_scratch"
    tmp.mkdir(parents=True, exist_ok=True)
    only = set(args.case or []) or None
    rc = 0
    for app_id in dict.fromkeys(args.apps):
        result = await derive_app(app_id, args.device, only, tmp)
        dest = Path(args.json) if args.json else _TRUTH / f"journey-{app_id}.json"
        if only and dest.exists():
            merged = json.loads(dest.read_text())
            merged.update(result)
            result = merged
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, indent=2))
        bad = [k for k, v in result.items() if not v.get("agrees")]
        print(f"\n{app_id}: {len(result) - len(bad)}/{len(result)} cases agree with the authored key"
              f"{'' if not bad else ' — DISAGREE: ' + ', '.join(bad)}")
        print(f"wrote {dest}")
        rc = rc or (1 if bad else 0)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
