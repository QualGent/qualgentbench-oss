#!/usr/bin/env python3
"""Derive every journey case's CreateBench CONTROL defects by replay (QUA-2854).

A create-mode trial runs an authored case on a control-only build — one defect of the
same journey APK that is not the case's target, flipped on alone — and the authored case
must still PASS there. That is only a fair demand if the case's OWN reference route still
holds with that defect on. So for every case and every candidate defect `d` of its app
that is not in its `bugs:`, this replays the case's harness-only `check:` with ONLY `d`
on, `--repeat` times from a fresh reset, and calls `d` ELIGIBLE iff every trial HOLDS.
No majority: one trial that fails makes `d` ineligible (and ends that candidate's trials
early — the answer can no longer change). An INCONCLUSIVE trial is ineligible too: a
control nobody could judge is not a control anybody should be graded against.

Each candidate also gets a RELATION to the case, which ranks the eligible ones:

  side         `d` manifests ON this route and the check still holds — its seeded site
               fired (canary) or its display marker is in the clean/control screen diff,
               on EVERY trial. The most tempting control: the authored case walks
               straight past it. A bare screen diff is recorded (`diff_steps`) but is NOT
               evidence: over the 41-case derive every diff-only "side" was orgzly noise
               (a "Notes count" row not yet loaded on the clean reference at step 1, the
               outline's scroll position at step 11-12), never the defect.
  same-screen  `d`'s own screen (where it manifests in its own case's committed truth
               row) is one this route visits (masked text-set Jaccard >=
               SAME_SCREEN_JACCARD against the route's committed clean screens).
  other        neither — a control somewhere else in the app.

`create_controls` = the eligible ids, side first, then same-screen, then other; file order
within a group. The grader (QUA-2857) runs trial t on `create_controls[t mod len]`
(`journey.control_for_trial`); a case with none is `specificity: n/a` and is reported,
never given a free pass.

The target half is NOT re-run: that the case's `bugs:` on make the route FAIL is already
the corpus gate's measured `passes.seeded` (`derive_journey.py`), and it is copied into
the derivation as `target` so a report can print it beside the control.

Output: two keys on each case's row in data/truth/journey-<app>.json — `create_controls`
and `create_control_derivation` (per-candidate trials, relation, evidence, the clean
reference pass, the case fingerprint the derivation measured). Every other byte of the row
is left alone. A `derive_journey.py` re-derive of a case REPLACES its row; since QUA-2860
it carries both keys over when the case's `controls_fingerprint` is unchanged (a rebuild
that leaves the route alone) and drops them when it moved, so the case reads `not
derived` here until this is re-run for it — the right order (a new route needs a new
eligibility answer).

The journey APK must be installed (`--install` does it from the test-case file's `apk:`
block); the installed build's sha256 is checked against that block before anything runs,
because a build without the seeded patches makes every candidate eligible — stably.

  uv run python scripts/derive_create_controls.py medtimer --device emulator-5556 --repeat 3
  uv run python scripts/derive_create_controls.py --report      # offline: the control table
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import derive_journey as dj

from qualgentbench import corpus, journey
from qualgentbench import replay as rp
from qualgentbench.bugs import load_suite
from qualgentbench.episode_runner import device_clock_pin
from qualgentbench.verify.device import _adb_bin

ROOT = Path(__file__).resolve().parents[1]

# Two screens are "the same screen" when their masked text sets overlap this much. A
# list screen differs across derives by a date or a count; two screens of one app share
# chrome (a month header, a bottom bar). Over the committed truth's 234 (case, candidate)
# pairs, 0.5 called 135 "same" — fossify-calendar's chrome alone puts most of its
# screens in 0.5-0.65 of each other — while 0.7 calls 92. Spot-checked by eye: the
# 0.7-0.85 band is one screen differing by a row, a time or a date (a day list with
# another event on it, the event editor at another hour, the Overview on another week).
SAME_SCREEN_JACCARD = 0.7

# A candidate's diff is stored for audit, never scored: a few strings per changed step.
_DIFF_KEEP = 4


# ── pure: relations, eligibility, ranking ─────────────────────────────────────

def screen_key(texts: list[str]) -> frozenset[str]:
    """A screen as the set of its masked, stripped texts (derive_journey's date mask:
    a saved date is the minute it was saved, never a symptom)."""
    return frozenset(s for s in (dj.mask(str(t)).strip() for t in texts or []) if s)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def defect_screens(truth: dict, doc: dict) -> dict[str, list[frozenset[str]]]:
    """Per defect id, the committed clean screens on which it MANIFESTS in the case(s)
    that seed it — the screen a same-screen control shares with another route.

    A display bug: the clean screens at its measured `visible_steps`. A functional bug:
    the clean screen at the step where the seeded diff first appears — the screen the
    fault took away (the step before it is almost always a screen every route of the
    app shares, which would make every defect "same-screen"). A functional bug
    whose seeded arm changed no screen (a state oracle read after the route): the clean
    route's final screen, the one the oracle reads. No seeding case: no screens."""
    out: dict[str, list[frozenset[str]]] = {}
    for case in (doc or {}).get("test_cases", []):
        row = (truth or {}).get(str(case.get("id"))) or {}
        clean = ((row.get("screens") or {}).get("clean")) or []
        if not clean:
            continue
        for bug in journey.case_bugs(case):
            bid = bug["id"]
            steps: list[int] = []
            side = next((s for s in row.get("side") or [] if s.get("bug") == bid), None)
            if side is not None:
                steps = list(side.get("visible_steps") or [])
            elif row.get("blocking") == bid:
                diff_steps = [d["step"] for d in row.get("diff") or []]
                steps = [diff_steps[0]] if diff_steps else [len(clean)]
            for s in steps:
                if 1 <= s <= len(clean):
                    key = screen_key(clean[s - 1])
                    if key and key not in out.setdefault(bid, []):
                        out[bid].append(key)
    return out


def same_screen(route: list[list[str]], screens: list[frozenset[str]],
                threshold: float = SAME_SCREEN_JACCARD) -> float:
    """The best Jaccard between any screen of `route` and any of `screens`; the caller
    compares it with the threshold (returned, not a bool, so the table can show it)."""
    best = 0.0
    for texts in route or []:
        key = screen_key(texts)
        for other in screens or []:
            best = max(best, jaccard(key, other))
    return round(best, 3)


def side_evidence(defect_id: str, marker: str, trials: list[dict]) -> str | None:
    """Did `defect_id` manifest on the route on EVERY trial, and how do we know?
    `fired` (its canary) or `marker` (its display marker in the clean/control diff); the
    stronger kind every trial shows. None when any trial showed neither (or there are
    no trials). A diff with neither is screen noise, not the defect (module docstring)."""
    if not trials:
        return None
    for kind in ("fired", "marker"):
        if all(_trial_shows(kind, defect_id, marker, t) for t in trials):
            return kind
    return None


def _trial_shows(kind: str, defect_id: str, marker: str, trial: dict) -> bool:
    if kind == "fired":
        return defect_id in (trial.get("fired") or [])
    return bool(marker) and bool(trial.get("marker_steps"))


def judge_candidate(defect_id: str, meta: dict, trials: list[dict], repeat: int,
                    overlap: float, threshold: float = SAME_SCREEN_JACCARD) -> dict:
    """One candidate's verdict. Eligible iff `repeat` trials ran and every one HOLDS."""
    outcomes = [t["outcome"] for t in trials]
    eligible = len(trials) == repeat and all(o == rp.HOLDS for o in outcomes)
    if eligible:
        reason = f"route holds {repeat}/{repeat}"
    elif all(o == rp.HOLDS for o in outcomes):
        reason = f"only {len(trials)}/{repeat} trials ran"
    else:
        i, bad = next((i, t) for i, t in enumerate(trials, 1) if t["outcome"] != rp.HOLDS)
        reason = f"trial {i}: {bad['outcome']} {bad.get('detail') or ''}".strip()
    evidence = side_evidence(defect_id, meta.get("marker") or "", trials)
    relation = ("side" if evidence else
                "same-screen" if overlap >= threshold else "other")
    return {"eligible": eligible, "relation": relation, "evidence": evidence,
            "screen_overlap": overlap, "flag_kind": "defect", "kind": meta.get("kind"),
            "reason": reason, "trials": trials}


def rank_controls(candidates: dict[str, dict], order: list[str]) -> list[str]:
    """Eligible ids: side, then same-screen, then other; `order` (the test-case file's
    defect order) within a group, so the ranking never depends on dict order."""
    rank = {r: i for i, r in enumerate(journey.CONTROL_RELATIONS)}
    pos = {d: i for i, d in enumerate(order)}
    eligible = [d for d, c in candidates.items() if c.get("eligible")]
    return sorted(eligible, key=lambda d: (rank.get(candidates[d].get("relation"), len(rank)),
                                           pos.get(d, len(pos)), d))


def target_status(row: dict) -> dict:
    """The case's TARGET as the corpus gate measured it (not re-run): its bugs, what they
    imply, what the seeded arm measured, and whether that confirms the target (a
    functional target's seeded arm must FAIL — VIOLATED or CRASHED; a display-only target
    PASSes with its marker on the route, which `agrees` already demands)."""
    bugs = list(row.get("bugs") or [])
    seeded = ((row.get("passes") or {}).get("seeded") or {}).get("outcome")
    confirmed = (bool(bugs) and bool(row.get("agrees"))
                 and row.get("measured") == row.get("expected"))
    return {"bugs": bugs, "expected": row.get("expected"), "measured": row.get("measured"),
            "seeded_outcome": seeded, "confirmed": confirmed}


def trial_entry(res: rp.ReplayResult, log: list[rp.ReplayResult], diff: list[dict],
                marker: str) -> dict:
    out = dj._pass_entry((res, [], log))
    out["fired"] = sorted(res.fired or [])
    out["diff_steps"] = [d["step"] for d in diff]
    out["marker_steps"] = dj._hits(diff, marker) if marker else []
    if diff:
        first = diff[0]
        out["diff_first"] = {"step": first["step"], "added": first["added"][:_DIFF_KEEP],
                             "removed": first["removed"][:_DIFF_KEEP]}
    return out


def build_derivation(case: dict, defects: dict[str, dict], candidates: dict[str, dict],
                     clean: dict, truth_row: dict, repeat: int, clock: str) -> tuple[list[str], dict]:
    controls = rank_controls(candidates, list(defects))
    derivation = {
        "fingerprint": journey.controls_fingerprint(case, defects),
        "repeat": repeat,
        "device_clock": clock,
        "clean": clean,
        "target": target_status(truth_row),
        "specificity": "scored" if controls else journey.SPECIFICITY_NA,
        "candidates": candidates,
    }
    return controls, derivation


def rejudge(case: dict, defects: dict[str, dict], derivation: dict) -> tuple[list[str], dict]:
    """Re-judge a stored derivation from its recorded trials — no device. Eligibility,
    relation and ranking are pure functions of what each trial recorded, so a change
    to the RULES (not the measurement) is applied by this, never by a re-derive."""
    der = dict(derivation)
    cands = {}
    for d, c in (der.get("candidates") or {}).items():
        cands[d] = judge_candidate(d, defects.get(d, {}), c.get("trials") or [], int(der["repeat"]),
                                   float(c.get("screen_overlap") or 0.0))
    der["candidates"] = cands
    controls = rank_controls(cands, list(defects))
    der["specificity"] = "scored" if controls else journey.SPECIFICITY_NA
    return controls, der


def merge_into_truth(truth: dict, case_id: str, controls: list[str], derivation: dict) -> dict:
    """Set the two keys on `case_id`'s row, leaving every other key where it was."""
    row = truth.get(case_id)
    if row is None:
        raise KeyError(f"{case_id}: no truth row — derive the case with derive_journey.py first")
    row[journey.CONTROLS_KEY] = controls
    row[journey.CONTROL_DERIVATION_KEY] = derivation
    return truth


# ── pure: the report ──────────────────────────────────────────────────────────

def case_report(app_id: str, doc: dict, truth: dict) -> list[dict]:
    """One line per case of the app: target, control ranking, relation counts, status."""
    out = []
    defects = journey.load_defects(doc)
    for case in doc.get("test_cases", []):
        cid = str(case["id"])
        row = truth.get(cid) or {}
        der = row.get(journey.CONTROL_DERIVATION_KEY) or {}
        controls = journey.create_controls(row)
        cands = der.get("candidates") or {}
        status = ("not derived" if controls is None else
                  "stale" if der.get("fingerprint") != journey.controls_fingerprint(case, defects)
                  else "ok")
        out.append({
            "app": app_id, "case": cid, "status": status,
            "target": [b["id"] for b in journey.case_bugs(case)],
            "target_confirmed": (der.get("target") or {}).get("confirmed"),
            "controls": [(c, (cands.get(c) or {}).get("relation")) for c in controls or []],
            "candidates": len(cands),
            "eligible": sum(1 for c in cands.values() if c.get("eligible")),
            "ineligible": {d: c.get("reason") for d, c in cands.items() if not c.get("eligible")},
            "specificity": der.get("specificity"),
        })
    return out


def report_counts(lines: list[dict]) -> dict:
    derived = [r for r in lines if r["status"] != "not derived"]
    return {
        "cases": len(lines),
        "derived": len(derived),
        "stale": sum(1 for r in lines if r["status"] == "stale"),
        "with_control": sum(1 for r in derived if r["controls"]),
        "specificity_na": sum(1 for r in derived if not r["controls"]),
        "first_control_side": sum(1 for r in derived if r["controls"] and r["controls"][0][1] == "side"),
        "any_side_control": sum(1 for r in derived if any(rel == "side" for _, rel in r["controls"])),
        "first_control_same_screen": sum(1 for r in derived if r["controls"]
                                         and r["controls"][0][1] == "same-screen"),
        "first_control_other": sum(1 for r in derived if r["controls"] and r["controls"][0][1] == "other"),
        "candidates": sum(r["candidates"] for r in derived),
        "eligible": sum(r["eligible"] for r in derived),
        "target_confirmed": sum(1 for r in derived if r["target_confirmed"]),
        "no_target": sum(1 for r in derived if not r["target"]),
    }


def format_report(lines: list[dict]) -> str:
    rows = ["| case | target | control (trial 0) | relation | eligible / candidates | rotation |",
            "|---|---|---|---|---|---|"]
    for r in lines:
        if r["status"] == "not derived":
            rows.append(f"| {r['case']} | {', '.join(r['target']) or '-'} | NOT DERIVED | | | |")
            continue
        first, rel = r["controls"][0] if r["controls"] else ("n/a", "specificity n/a")
        rot = " → ".join(f"{c} ({x})" for c, x in r["controls"]) or "-"
        stale = " (STALE)" if r["status"] == "stale" else ""
        rows.append(f"| {r['case']}{stale} | {', '.join(r['target']) or '-'} | {first} | {rel} "
                    f"| {r['eligible']}/{r['candidates']} | {rot} |")
    c = report_counts(lines)
    rows += ["",
             (f"{c['derived']}/{c['cases']} cases derived ({c['stale']} stale) · "
              f"{c['with_control']} with an eligible control · {c['specificity_na']} specificity n/a"),
             (f"trial-0 control relation: side {c['first_control_side']} · same-screen "
              f"{c['first_control_same_screen']} · other {c['first_control_other']} · "
              f"cases with ANY side-bug control: {c['any_side_control']}"),
             (f"candidates {c['eligible']}/{c['candidates']} eligible · target confirmed by the "
              f"corpus gate on {c['target_confirmed']} cases ({c['no_target']} carry no target bug)")]
    return "\n".join(rows)


# ── device ────────────────────────────────────────────────────────────────────

async def installed_sha256(serial: str, bundle: str) -> str | None:
    rc, out = await rp._adb(serial, "shell", "pm", "path", bundle)
    paths = [ln.split(":", 1)[1].strip() for ln in out.decode("utf-8", "replace").splitlines()
             if ln.startswith("package:")]
    if rc != 0 or len(paths) != 1:
        return None
    proc = await asyncio.create_subprocess_exec(
        _adb_bin(), "-s", serial, "exec-out", "cat", paths[0],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    data, _ = await proc.communicate()
    return hashlib.sha256(data).hexdigest() if proc.returncode == 0 and data else None


async def ensure_build(app_id: str, serial: str, bundle: str, install: bool) -> str | None:
    """None when the installed build is the case file's journey build, else why not."""
    meta = journey.apk_meta(app_id) or {}
    want = str(meta.get("sha256") or "")
    if install:
        from qualgentbench.apps import fetch_seeded_apk
        apk = fetch_seeded_apk(app_id, meta, kind="journey")
        rc, out = await rp._adb(serial, "install", "-r", "-g", str(apk))
        if rc != 0 or b"Success" not in out:
            return f"install failed: {out.decode('utf-8', 'replace').strip()[-300:]}"
    got = await installed_sha256(serial, bundle)
    if want and got != want:
        return (f"installed {bundle} is {str(got)[:16]}…, the journey build is {want[:16]}… "
                f"— install it (--install) before deriving")
    return None


async def derive_case(serial: str, bundle: str, case: dict, defects: dict[str, dict],
                      staged: tuple, device_setup, repeat: int,
                      overlaps: dict[str, float]) -> tuple[dict | None, dict]:
    """(candidates or None when the clean reference does not hold, clean entry)."""
    snap, shared, shared_snap = staged
    claim = dj._claim(case)
    if claim is None:
        return None, {"outcome": "unusable", "detail": "no usable check"}
    t0 = time.monotonic()
    res, clean_dumps, log = await dj.one_pass(serial, bundle, claim, [], snap, shared,
                                              shared_snap, device_setup)
    clean = dj._pass_entry((res, clean_dumps, log))
    print(f"    clean      {res.outcome:12} {res.steps_run:2} steps {time.monotonic() - t0:4.0f}s  {res.detail}")
    if res.outcome != rp.HOLDS:
        return None, clean
    if res.fired:
        clean["fired"] = sorted(res.fired)
    bugs = {b["id"] for b in journey.case_bugs(case)}
    candidates: dict[str, dict] = {}
    for d, meta in defects.items():
        if d in bugs:
            continue
        trials: list[dict] = []
        for i in range(repeat):
            t0 = time.monotonic()
            r, dumps, lg = await dj.one_pass(serial, bundle, claim, [d], snap, shared,
                                             shared_snap, device_setup)
            diff = dj._diff(clean_dumps, dumps) if r.outcome != rp.INCONCLUSIVE else []
            entry = trial_entry(r, lg, diff, meta.get("marker") or "")
            trials.append(entry)
            masked = "" if len(lg) < 2 else f"  [{len(lg)} attempts]"
            seen = (f" fired={entry['fired']}" if entry["fired"] else "") + \
                   (f" diff@{entry['diff_steps']}" if entry["diff_steps"] else "")
            print(f"    {d[:34]:34} {i + 1}/{repeat} {r.outcome:12} {r.steps_run:2} steps "
                  f"{time.monotonic() - t0:4.0f}s{seen}  {r.detail[:80]}{masked}")
            if r.outcome != rp.HOLDS:
                break
        candidates[d] = judge_candidate(d, meta, trials, repeat, overlaps.get(d, 0.0))
    return candidates, clean


async def derive_app(app_id: str, serial: str, only: set[str] | None, repeat: int,
                     install: bool, skip_derived: bool, tmp: Path) -> int:
    suite = load_suite(corpus.spec_path(app_id))
    doc = journey.load_cases(app_id)
    if not doc:
        print(f"{app_id}: no test-case file")
        return 1
    bundle = suite["app"]["package"]
    defects = journey.load_defects(doc)
    dest = journey.truth_path(app_id)
    truth = json.loads(dest.read_text()) if dest.exists() else {}
    cases = [c for c in doc.get("test_cases", []) if not only or c["id"] in only]
    if skip_derived:
        cases = [c for c in cases if not _is_current(truth.get(c["id"]), c, defects, repeat)]
    print(f"\n{app_id} ({bundle}) · {len(cases)} case(s) · {len(defects)} defects · "
          f"--repeat {repeat} on {serial}")
    if not cases:
        return 0
    problem = await ensure_build(app_id, serial, bundle, install)
    if problem:
        print(f"  REFUSED: {problem}")
        return 1
    screens = defect_screens(truth, doc)
    staged = await dj.stage(serial, suite, tmp)
    rc = 0
    for case in cases:
        cid = case["id"]
        row = truth.get(cid)
        if row is None:
            print(f"\n  [{cid}] no truth row — derive_journey.py first")
            rc = 1
            continue
        route = ((row.get("screens") or {}).get("clean")) or []
        overlaps = {d: same_screen(route, screens.get(d, [])) for d in defects}
        print(f"\n  [{cid}] target {[b['id'] for b in journey.case_bugs(case)] or '-'}")
        candidates, clean = await derive_case(serial, bundle, case, defects, staged,
                                              suite.get("device_setup"), repeat, overlaps)
        if candidates is None:
            print(f"    ! clean reference did not hold ({clean.get('outcome')}: "
                  f"{clean.get('detail')}) — no controls written for this case")
            rc = 1
            continue
        controls, derivation = build_derivation(case, defects, candidates, clean, row, repeat,
                                                device_clock_pin().isoformat())
        merge_into_truth(truth, cid, controls, derivation)
        # Written after EVERY case: a derive of an app is hours, and a crash or a
        # Ctrl+C must not throw the finished cases away (--skip-derived resumes).
        dest.write_text(json.dumps(truth, indent=2))
        rel = {c: candidates[c]["relation"] for c in controls}
        print(f"    => controls {[f'{c} ({rel[c]})' for c in controls] or 'NONE — specificity n/a'}")
    print(f"wrote {dest}")
    return rc


def _is_current(row: dict | None, case: dict, defects: dict, repeat: int) -> bool:
    der = (row or {}).get(journey.CONTROL_DERIVATION_KEY) or {}
    return (journey.create_controls(row) is not None
            and der.get("fingerprint") == journey.controls_fingerprint(case, defects)
            and int(der.get("repeat") or 0) >= repeat)


def journey_apps() -> list[str]:
    return sorted(p.stem for p in (Path(journey.__file__).parent / "data" / "test-cases").glob("*.yaml"))


def run_report(apps: list[str]) -> int:
    lines: list[dict] = []
    for app_id in apps:
        doc = journey.load_cases(app_id)
        if doc:
            lines += case_report(app_id, doc, journey.load_truth(app_id))
    print(format_report(lines))
    c = report_counts(lines)
    return 0 if c["derived"] == c["cases"] and not c["stale"] else 1


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("apps", nargs="*", help="app ids (default: every app with a test-case file)")
    ap.add_argument("--device", default="emulator-5554")
    ap.add_argument("--case", action="append", help="derive only this case id (repeatable)")
    ap.add_argument("--repeat", type=int, default=3,
                    help="trials per candidate; eligible iff ALL hold (default 3, the corpus rule "
                         "for anything that can crash or hang — which a control can)")
    ap.add_argument("--install", action="store_true",
                    help="install the journey APK (test-case file's apk: block) first")
    ap.add_argument("--skip-derived", action="store_true",
                    help="skip cases whose row already carries a current derivation "
                         "(same fingerprint, repeat >= --repeat) — resumes an interrupted run")
    ap.add_argument("--report", action="store_true",
                    help="no device: print the control table from the committed truth")
    args = ap.parse_args()
    apps = list(dict.fromkeys(args.apps)) or journey_apps()
    if args.report:
        return run_report(apps)
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")
    tmp = ROOT / "runs" / "_derive_scratch"
    tmp.mkdir(parents=True, exist_ok=True)
    only = set(args.case or []) or None
    rc = 0
    for app_id in apps:
        rc = await derive_app(app_id, args.device, only, args.repeat, args.install,
                              args.skip_derived, tmp) or rc
    print()
    run_report(apps)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
