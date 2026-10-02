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

`create_controls` = the eligible ids, ranked by RANK RULE 2 (QUA-2867; rule 1 was the
relation alone): live controls before lethal ones, each side first, then same-screen, then
other; file order within a group. A control is LETHAL when it kills the app where a route
reaches it (`create/detection.defect_lethal`: the walk/assert label of the case it is the
target of, else its class). Eligibility is measured on the REFERENCE route, and an
authored case takes its own (QUA-2861 rerun: tasks-complete-parent's authored routes
created subtasks and walked into `subtask-filed-before-written`, a same-screen ordering
crash, on 2/2 of its control runs); grader v4 excludes such a run (`control_reached`), so
the artifact's specificity is unscored. A lethal control ADJACENT to the route (`side` or
`same-screen`: reach risk `lethal-adjacent`) is therefore a RESERVE, never in
`create_controls` while any other control is eligible; a lethal `other` one ranks after
every live control. Rule 2 is static (metadata + the measured relation), never another
device pass per authored route — the design note is docs/createbench-v2-controls.md. The
grader (QUA-2857) runs trial t on `create_controls[t mod len]`
(`journey.control_for_trial`); a case with none is `specificity: n/a` and is reported,
never given a free pass.

EARLY STOP (`--stop-after N`, default 3; 0 = exhaustive). The candidates are replayed in
a static order — live before lethal, a candidate whose own screen the route visits
(`screen_overlap` >= SAME_SCREEN_JACCARD) first, lethal-adjacent last — and the case
stops once N non-reserve candidates are eligible. The exhaustive derive of 2026-09-30 was
737 replays (~10 h) for 231/234 eligible; `--report --stop-after 3` replays that
derivation's recorded trials through the early stop and prints the replays it would
have cost and every case whose controls it would change. Unreplayed candidates are
listed under the derivation's `early_stop`, never as ineligible.

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
  uv run python scripts/derive_create_controls.py --rejudge     # offline: what rule 2 moves
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
from qualgentbench.create import detection
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

# The ranking rule a derivation records (`rank_rule`). 1 (QUA-2854): relation only.
# 2 (QUA-2867): live before lethal, a lethal control adjacent to the route is a reserve.
# A derivation without the key was ranked by rule 1.
RANK_RULE = 2
RANK_RULES = (1, 2)
# Reach risk of an eligible candidate (rule 2).
LETHAL_ADJACENT = "lethal-adjacent"     # lethal AND side/same-screen: a reserve
LETHAL_ELSEWHERE = "lethal"             # lethal, relation other: ranked after live ones
_ADJACENT = ("side", "same-screen")
# Early stop: replay until this many non-reserve candidates are eligible (0 = all).
STOP_AFTER = 3


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


def reach_risk(relation: str | None, lethal: bool | None) -> str | None:
    """Rule 2's risk of an authored route reaching a control and dying on it: None (a
    live control, or lethality unknown), `lethal` (lethal, elsewhere in the app) or
    `lethal-adjacent` (lethal and on/beside the reference route — a reserve)."""
    if not lethal:
        return None
    return LETHAL_ADJACENT if relation in _ADJACENT else LETHAL_ELSEWHERE


def judge_candidate(defect_id: str, meta: dict, trials: list[dict], repeat: int,
                    overlap: float, threshold: float = SAME_SCREEN_JACCARD,
                    lethal: tuple[bool, str] | None = None) -> dict:
    """One candidate's verdict. Eligible iff `repeat` trials ran and every one HOLDS.
    `lethal` = `detection.defect_lethal(...)`; with it the candidate also records
    `lethal`, `lethal_why` and its `reach_risk` (rank rule 2)."""
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
    out = {"eligible": eligible, "relation": relation, "evidence": evidence,
           "screen_overlap": overlap, "flag_kind": "defect", "kind": meta.get("kind"),
           "reason": reason, "trials": trials}
    if lethal is not None:
        out.update(lethal=bool(lethal[0]), lethal_why=lethal[1],
                   reach_risk=reach_risk(relation, lethal[0]))
    return out


def rank_controls(candidates: dict[str, dict], order: list[str],
                  rule: int = RANK_RULE) -> list[str]:
    """Eligible ids, best first; `order` (the test-case file's defect order) breaks ties,
    so the ranking never depends on dict order.

    Rule 1 (QUA-2854): side, then same-screen, then other.
    Rule 2 (QUA-2867): the same within each reach-risk tier — live controls, then lethal
    ones elsewhere in the app — and a `lethal-adjacent` candidate is a RESERVE: it is
    returned only when nothing else is eligible (a reserve beats specificity n/a, since a
    route that does not reach it still scores). A candidate judged without lethality
    (`reach_risk` absent) is live."""
    if rule not in RANK_RULES:
        raise ValueError(f"unknown rank rule {rule}")
    rank = {r: i for i, r in enumerate(journey.CONTROL_RELATIONS)}
    tier = {None: 0, LETHAL_ELSEWHERE: 1, LETHAL_ADJACENT: 2}
    pos = {d: i for i, d in enumerate(order)}

    def key(d: str) -> tuple:
        c = candidates[d]
        t = tier.get(c.get("reach_risk"), 0) if rule >= 2 else 0
        return (t, rank.get(c.get("relation"), len(rank)), pos.get(d, len(pos)), d)

    eligible = sorted((d for d, c in candidates.items() if c.get("eligible")), key=key)
    if rule >= 2:
        live = [d for d in eligible if candidates[d].get("reach_risk") != LETHAL_ADJACENT]
        return live or eligible
    return eligible


def reserves(candidates: dict[str, dict]) -> list[str]:
    """Eligible candidates rule 2 holds back (`lethal-adjacent`)."""
    return sorted(d for d, c in candidates.items()
                  if c.get("eligible") and c.get("reach_risk") == LETHAL_ADJACENT)


def replay_order(defects: dict[str, dict], bugs: set[str], overlaps: dict[str, float],
                 lethal: dict[str, tuple[bool, str]],
                 threshold: float = SAME_SCREEN_JACCARD) -> list[str]:
    """The order the derive replays a case's candidates in, from what is known BEFORE
    replay (a `side` relation is only measured by it): live before lethal; within each,
    a candidate whose own screen the route visits (`screen_overlap` >= threshold: side
    or same-screen, the controls rule 2 ranks first) before the rest, and among those a
    DISPLAY defect first (all 35 side controls of the 2026-09-30 derive were on a visited
    screen, 33 of them display defects), then the higher overlap; a lethal candidate
    the route visits (a likely reserve) last; file order breaks ties."""
    pos = {d: i for i, d in enumerate(defects)}

    def key(d: str) -> tuple:
        dead = bool((lethal.get(d) or (False, ""))[0])
        ov = overlaps.get(d, 0.0)
        near = ov >= threshold
        tier = (2 if near else 1) if dead else 0
        display = (defects.get(d) or {}).get("kind") == "display"
        return (tier, 0 if near else 1, 0 if (near and display) else 1, -ov, pos[d])

    return sorted((d for d in defects if d not in bugs), key=key)


def stop_reached(candidates: dict[str, dict], stop_after: int) -> bool:
    """Early stop: `stop_after` (> 0) eligible non-reserve candidates are in hand."""
    if stop_after <= 0:
        return False
    found = sum(1 for c in candidates.values()
                if c.get("eligible") and c.get("reach_risk") != LETHAL_ADJACENT)
    return found >= stop_after


def lethal_map(doc: dict, truth: dict, defects: dict[str, dict]) -> dict[str, tuple[bool, str]]:
    return {d: detection.defect_lethal(doc, truth, d) for d in defects}


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
                     clean: dict, truth_row: dict, repeat: int, clock: str,
                     early_stop: dict | None = None) -> tuple[list[str], dict]:
    controls = rank_controls(candidates, list(defects))
    derivation = {
        "fingerprint": journey.controls_fingerprint(case, defects),
        "repeat": repeat,
        "device_clock": clock,
        "rank_rule": RANK_RULE,
        "clean": clean,
        "target": target_status(truth_row),
        "specificity": "scored" if controls else journey.SPECIFICITY_NA,
        "reserves": reserves(candidates),
        "candidates": candidates,
    }
    if early_stop is not None:
        derivation["early_stop"] = early_stop
    return controls, derivation


def early_stop_block(stop_after: int, order: list[str], candidates: dict[str, dict]) -> dict:
    """What an early-stopped derive leaves on the record: the rule, the replay order and
    the candidates it never replayed (unmeasured, NOT ineligible)."""
    return {"stop_after": stop_after, "order": order,
            "replays": sum(len(c.get("trials") or []) for c in candidates.values()),
            "unreplayed": [d for d in order if d not in candidates]}


def rejudge(case: dict, defects: dict[str, dict], derivation: dict,
            lethal: dict[str, tuple[bool, str]] | None = None) -> tuple[list[str], dict]:
    """Re-judge a stored derivation from its recorded trials — no device. Eligibility,
    relation and ranking are pure functions of what each trial recorded, so a change
    to the RULES (not the measurement) is applied by this, never by a re-derive.
    `lethal` (`lethal_map`) gives rank rule 2 its reach risks; without it every
    candidate is live and rule 2 orders exactly as rule 1."""
    der = dict(derivation)
    cands = {}
    for d, c in (der.get("candidates") or {}).items():
        cands[d] = judge_candidate(d, defects.get(d, {}), c.get("trials") or [], int(der["repeat"]),
                                   float(c.get("screen_overlap") or 0.0),
                                   lethal=(lethal or {}).get(d))
    der["candidates"] = cands
    controls = rank_controls(cands, list(defects))
    der["specificity"] = "scored" if controls else journey.SPECIFICITY_NA
    der["rank_rule"] = RANK_RULE
    der["reserves"] = reserves(cands)
    return controls, der


def simulate_early_stop(case: dict, defects: dict[str, dict], derivation: dict,
                        lethal: dict[str, tuple[bool, str]], stop_after: int
                        ) -> tuple[list[str], dict]:
    """What `--stop-after` would have derived, from an EXHAUSTIVE stored derivation: the
    same recorded trials, replayed in `replay_order` until `stop_reached`. Its
    `early_stop.replays` (+1 clean pass) is the measured cost of the early stop — the
    trials a candidate recorded are the replays it took (an ineligible one stops at its
    first failing trial in both)."""
    stored = derivation.get("candidates") or {}
    bugs = {b["id"] for b in journey.case_bugs(case)}
    overlaps = {d: float((stored.get(d) or {}).get("screen_overlap") or 0.0) for d in defects}
    order = replay_order(defects, bugs, overlaps, lethal)
    repeat = int(derivation["repeat"])
    cands: dict[str, dict] = {}
    for d in order:
        if stop_reached(cands, stop_after):
            break
        if d not in stored:
            continue
        cands[d] = judge_candidate(d, defects.get(d, {}), stored[d].get("trials") or [], repeat,
                                   overlaps[d], lethal=lethal.get(d))
    der = {k: v for k, v in derivation.items() if k != "candidates"}
    der.update(candidates=cands, rank_rule=RANK_RULE, reserves=reserves(cands),
               early_stop=early_stop_block(stop_after, order, cands))
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


def rule_report(app_id: str, doc: dict, truth: dict, stop_after: int = STOP_AFTER
                ) -> list[dict]:
    """One line per DERIVED case: its committed controls (as ranked when derived) beside
    what rank rule 2 makes of the same trials (`rejudge`) and what an early stop at
    `stop_after` would have derived from them (`simulate_early_stop`), with the replays
    each costs. Offline; reads the committed truth only."""
    out = []
    defects = journey.load_defects(doc)
    lethal = lethal_map(doc, truth, defects)
    for case in doc.get("test_cases", []):
        cid = str(case["id"])
        row = truth.get(cid) or {}
        der = row.get(journey.CONTROL_DERIVATION_KEY)
        committed = journey.create_controls(row)
        if committed is None or not der:
            continue
        rule2, der2 = rejudge(case, defects, der, lethal)
        es, des = simulate_early_stop(case, defects, der, lethal, stop_after)
        exhaustive = 1 + sum(len(c.get("trials") or []) for c in
                             (der.get("candidates") or {}).values())
        out.append({
            "app": app_id, "case": cid, "rank_rule": int(der.get("rank_rule") or 1),
            "committed": committed, "rule2": rule2, "reserves": der2["reserves"],
            "rule2_profile": _profile(rule2[:stop_after], der2),
            "early_stop_profile": _profile(es[:stop_after], des),
            "early_stop": es, "replays": exhaustive,
            "early_stop_replays": 1 + des["early_stop"]["replays"],
            "unreplayed": des["early_stop"]["unreplayed"],
            "lethal": sorted(d for d in der2["candidates"] if lethal.get(d, (False,))[0]),
        })
    return out


def _profile(controls: list[str], derivation: dict) -> list[tuple]:
    """(reach risk, relation) per control: what a control IS, whichever defect it is."""
    cands = derivation.get("candidates") or {}
    return [((cands.get(c) or {}).get("reach_risk"), (cands.get(c) or {}).get("relation"))
            for c in controls]


def format_rule_report(lines: list[dict], stop_after: int = STOP_AFTER) -> str:
    moved = [r for r in lines if r["rule2"] != r["committed"]]
    # The early stop keeps its first `stop_after`; compare those with rule 2's: the
    # profile (risk + relation) is the quality of the controls, the ids which defects.
    es_worse = [r for r in lines if r["early_stop_profile"] != r["rule2_profile"]]
    es_moved = [r for r in lines if r["early_stop"][:stop_after] != r["rule2"][:stop_after]]
    rows = [(f"rank rule {RANK_RULE} (QUA-2867) over the committed derivations: "
             f"{len(moved)}/{len(lines)} case(s) would change controls")]
    for r in moved:
        rows.append(f"  {r['case']}: {' → '.join(r['committed']) or '-'}  ⇒  "
                    f"{' → '.join(r['rule2']) or '-'}"
                    + (f"  (reserve: {', '.join(r['reserves'])})" if r["reserves"] else ""))
    total = sum(r["replays"] for r in lines)
    es_total = sum(r["early_stop_replays"] for r in lines)
    rows.append(f"early stop at {stop_after} eligible: {es_total} replays vs {total} exhaustive "
                f"({(1 - es_total / total) * 100 if total else 0:.0f}% fewer; clean passes "
                f"counted). Against the exhaustive rule-2 ranking's first {stop_after}: "
                f"{len(es_worse)}/{len(lines)} case(s) get controls of another reach risk or "
                f"relation; {len(es_moved)} get another defect of the same kind")
    for r in es_worse:
        rows.append(f"  WORSE {r['case']}: {r['rule2_profile']}  ⇒  {r['early_stop_profile']}")
    for r in es_moved:
        rows.append(f"  {r['case']}: {' → '.join(r['rule2'][:stop_after]) or '-'}  ⇒  "
                    f"{' → '.join(r['early_stop'][:stop_after]) or '-'}")
    return "\n".join(rows)


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
                      overlaps: dict[str, float], lethal: dict[str, tuple[bool, str]],
                      stop_after: int = STOP_AFTER
                      ) -> tuple[dict | None, dict, dict | None]:
    """(candidates or None when the clean reference does not hold, clean entry, the
    `early_stop` block or None when exhaustive). Candidates are replayed in
    `replay_order` and the case stops at `stop_reached`."""
    snap, shared, shared_snap = staged
    claim = dj._claim(case)
    if claim is None:
        return None, {"outcome": "unusable", "detail": "no usable check"}, None
    t0 = time.monotonic()
    res, clean_dumps, log = await dj.one_pass(serial, bundle, claim, [], snap, shared,
                                              shared_snap, device_setup)
    clean = dj._pass_entry((res, clean_dumps, log))
    print(f"    clean      {res.outcome:12} {res.steps_run:2} steps {time.monotonic() - t0:4.0f}s  {res.detail}")
    if res.outcome != rp.HOLDS:
        return None, clean, None
    if res.fired:
        clean["fired"] = sorted(res.fired)
    bugs = {b["id"] for b in journey.case_bugs(case)}
    candidates: dict[str, dict] = {}
    order = replay_order(defects, bugs, overlaps, lethal)
    for d in order:
        if stop_reached(candidates, stop_after):
            print(f"    early stop: {stop_after} eligible non-reserve control(s); "
                  f"{len(order) - len(candidates)} candidate(s) not replayed")
            break
        meta = defects[d]
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
        candidates[d] = judge_candidate(d, meta, trials, repeat, overlaps.get(d, 0.0),
                                        lethal=lethal.get(d))
    early = early_stop_block(stop_after, order, candidates) if stop_after > 0 else None
    return candidates, clean, early


async def derive_app(app_id: str, serial: str, only: set[str] | None, repeat: int,
                     install: bool, skip_derived: bool, tmp: Path,
                     stop_after: int = STOP_AFTER) -> int:
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
          f"--repeat {repeat} on {serial} · "
          f"{f'stop after {stop_after} eligible' if stop_after > 0 else 'exhaustive'}")
    if not cases:
        return 0
    problem = await ensure_build(app_id, serial, bundle, install)
    if problem:
        print(f"  REFUSED: {problem}")
        return 1
    screens = defect_screens(truth, doc)
    lethal = lethal_map(doc, truth, defects)
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
        candidates, clean, early = await derive_case(serial, bundle, case, defects, staged,
                                                     suite.get("device_setup"), repeat,
                                                     overlaps, lethal, stop_after)
        if candidates is None:
            print(f"    ! clean reference did not hold ({clean.get('outcome')}: "
                  f"{clean.get('detail')}) — no controls written for this case")
            rc = 1
            continue
        controls, derivation = build_derivation(case, defects, candidates, clean, row, repeat,
                                                device_clock_pin().isoformat(), early)
        merge_into_truth(truth, cid, controls, derivation)
        # Written after EVERY case: a derive of an app is hours, and a crash or a
        # Ctrl+C must not throw the finished cases away (--skip-derived resumes).
        dest.write_text(json.dumps(truth, indent=2))
        rel = {c: candidates[c]["relation"] for c in controls}
        print(f"    => controls {[f'{c} ({rel[c]})' for c in controls] or 'NONE — specificity n/a'}"
              + (f"  reserve {derivation['reserves']}" if derivation["reserves"] else ""))
    print(f"wrote {dest}")
    return rc


def _is_current(row: dict | None, case: dict, defects: dict, repeat: int) -> bool:
    der = (row or {}).get(journey.CONTROL_DERIVATION_KEY) or {}
    return (journey.create_controls(row) is not None
            and der.get("fingerprint") == journey.controls_fingerprint(case, defects)
            and int(der.get("repeat") or 0) >= repeat)


def journey_apps() -> list[str]:
    return sorted(p.stem for p in (Path(journey.__file__).parent / "data" / "test-cases").glob("*.yaml"))


def run_report(apps: list[str], stop_after: int = STOP_AFTER) -> int:
    lines: list[dict] = []
    rules: list[dict] = []
    for app_id in apps:
        doc = journey.load_cases(app_id)
        if doc:
            truth = journey.load_truth(app_id)
            lines += case_report(app_id, doc, truth)
            rules += rule_report(app_id, doc, truth, stop_after)
    print(format_report(lines))
    print()
    print(format_rule_report(rules, stop_after))
    c = report_counts(lines)
    return 0 if c["derived"] == c["cases"] and not c["stale"] else 1


def run_rejudge(apps: list[str], write: bool) -> int:
    """Re-rank every committed derivation under the current rank rule from its recorded
    trials (no device). Prints what moves; `--write` writes it into the truth — which
    MOVES the corpus version (a grade under the old controls must not be blended with
    one under the new)."""
    moved = 0
    for app_id in apps:
        doc = journey.load_cases(app_id)
        if not doc:
            continue
        dest = journey.truth_path(app_id)
        truth = json.loads(dest.read_text()) if dest.exists() else {}
        defects = journey.load_defects(doc)
        lethal = lethal_map(doc, truth, defects)
        changed = False
        for case in doc.get("test_cases", []):
            row = truth.get(str(case["id"])) or {}
            der = row.get(journey.CONTROL_DERIVATION_KEY)
            if not der or journey.create_controls(row) is None:
                continue
            controls, der2 = rejudge(case, defects, der, lethal)
            if controls != journey.create_controls(row) or der2 != der:
                if controls != journey.create_controls(row):
                    moved += 1
                    print(f"{case['id']}: {journey.create_controls(row)} ⇒ {controls}")
                merge_into_truth(truth, str(case["id"]), controls, der2)
                changed = True
        if write and changed:
            dest.write_text(json.dumps(truth, indent=2))
            print(f"wrote {dest}")
    print(f"{moved} case(s) change controls under rank rule {RANK_RULE}"
          + ("" if write else " (dry run: --write applies it and moves the corpus version)"))
    return 0


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
    ap.add_argument("--stop-after", type=int, default=STOP_AFTER,
                    help="early stop: replay candidates in rank-rule-2 order and stop a case "
                         f"once this many non-reserve controls are eligible (default "
                         f"{STOP_AFTER}; 0 = every candidate, the exhaustive derive). With "
                         "--report: the early stop simulated over the committed trials")
    ap.add_argument("--report", action="store_true",
                    help="no device: print the control table from the committed truth, and "
                         "what rank rule 2 and the early stop make of its trials")
    ap.add_argument("--rejudge", action="store_true",
                    help="no device: re-rank the committed derivations under the current "
                         "rank rule from their recorded trials (dry run unless --write)")
    ap.add_argument("--write", action="store_true",
                    help="with --rejudge: write the re-ranked controls into the truth "
                         "(moves the corpus version)")
    args = ap.parse_args()
    apps = list(dict.fromkeys(args.apps)) or journey_apps()
    if args.stop_after < 0:
        ap.error("--stop-after must be >= 0")
    if args.write and not args.rejudge:
        ap.error("--write only applies to --rejudge")
    if args.report:
        return run_report(apps, args.stop_after)
    if args.rejudge:
        return run_rejudge(apps, args.write)
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")
    tmp = ROOT / "runs" / "_derive_scratch"
    tmp.mkdir(parents=True, exist_ok=True)
    only = set(args.case or []) or None
    rc = 0
    for app_id in apps:
        rc = await derive_app(app_id, args.device, only, args.repeat, args.install,
                              args.skip_derived, tmp, args.stop_after) or rc
    print()
    run_report(apps, args.stop_after)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
