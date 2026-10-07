"""CreateBench control selection (QUA-2854): rotation, the n/a path, eligibility,
relations and ranking — all device-free. The device half of
`scripts/derive_create_controls.py` is `derive_journey.one_pass`, tested there."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from qualgentbench import journey

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_create_controls.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_create_controls", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dcc = _load()
rp = dcc.rp
HOLDS, VIOLATED, CRASHED, INCONCLUSIVE = rp.HOLDS, rp.VIOLATED, rp.CRASHED, rp.INCONCLUSIVE


def _row(controls, relations=None):
    cands = {c: {"eligible": True, "relation": (relations or {}).get(c, "other"),
                 "flag_kind": "defect"} for c in controls}
    return {journey.CONTROLS_KEY: list(controls),
            journey.CONTROL_DERIVATION_KEY: {"candidates": cands}}


# ── rotation ──────────────────────────────────────────────────────────────────

def test_trial_t_uses_controls_t_mod_len():
    row = _row(["a", "b", "c"], {"a": "side", "b": "same-screen"})
    got = [journey.control_for_trial(row, t)["control"] for t in range(7)]
    assert got == ["a", "b", "c", "a", "b", "c", "a"]
    first = journey.control_for_trial(row, 0)
    assert first == {"control": "a", "relation": "side", "flag_kind": "defect",
                     "specificity": "scored"}
    assert journey.control_for_trial(row, 4)["relation"] == "same-screen"


def test_single_control_is_used_on_every_trial():
    row = _row(["only"])
    assert {journey.control_for_trial(row, t)["control"] for t in range(5)} == {"only"}


def test_negative_trial_is_refused():
    with pytest.raises(ValueError):
        journey.control_for_trial(_row(["a"]), -1)


# ── the n/a path ──────────────────────────────────────────────────────────────

def test_no_eligible_control_is_specificity_na_never_a_pass():
    row = _row([])
    for t in range(3):
        sel = journey.control_for_trial(row, t)
        assert sel["control"] is None
        assert sel["specificity"] == journey.SPECIFICITY_NA == "n/a"


def test_underived_row_raises_instead_of_skipping_the_control_arm():
    # absent key = never derived, which is NOT the same as "no eligible control"
    assert journey.create_controls({"bugs": ["x"]}) is None
    assert journey.create_controls(None) is None
    assert journey.create_controls(_row([])) == []
    with pytest.raises(LookupError):
        journey.control_for_trial({"bugs": ["x"]}, 0)


def test_flag_kinds_reserve_drift():
    assert journey.FLAG_KINDS == ("defect", "drift")
    # the deriver writes only `defect`; drift is a documented slot, not a build
    cand = dcc.judge_candidate("d", {"kind": "display", "marker": ""},
                               [_trial(HOLDS)], 1, 0.0)
    assert cand["flag_kind"] == "defect"


# ── eligibility ───────────────────────────────────────────────────────────────

def _trial(outcome, fired=(), diff_steps=(), marker_steps=(), detail=""):
    return {"outcome": outcome, "detail": detail, "steps_run": 4, "attempts": 1,
            "fired": list(fired), "diff_steps": list(diff_steps),
            "marker_steps": list(marker_steps)}


def test_eligible_only_when_every_trial_holds():
    meta = {"kind": "functional", "marker": ""}
    ok = dcc.judge_candidate("d", meta, [_trial(HOLDS)] * 3, 3, 0.0)
    assert ok["eligible"] and ok["reason"] == "route holds 3/3"
    broke = dcc.judge_candidate("d", meta, [_trial(HOLDS), _trial(CRASHED, detail="boom")], 3, 0.0)
    assert not broke["eligible"]
    assert broke["reason"] == f"trial 2: {CRASHED} boom"
    # an unjudgeable trial is not a pass either
    assert not dcc.judge_candidate("d", meta, [_trial(INCONCLUSIVE)], 3, 0.0)["eligible"]
    # fewer trials than asked (early stop) can never be eligible
    assert not dcc.judge_candidate("d", meta, [_trial(HOLDS)] * 2, 3, 0.0)["eligible"]


# ── relations and ranking ─────────────────────────────────────────────────────

def test_side_evidence_needs_every_trial():
    assert dcc.side_evidence("d", "", [_trial(HOLDS, fired=["d"])] * 3) == "fired"
    assert dcc.side_evidence("d", "9 left", [_trial(HOLDS, diff_steps=[2], marker_steps=[2])] * 3) == "marker"
    # a bare screen diff is recorded but is not evidence (it was orgzly scroll noise)
    assert dcc.side_evidence("d", "", [_trial(HOLDS, diff_steps=[5])] * 2) is None
    # seen on 2 of 3 trials is not "on the route"
    assert dcc.side_evidence("d", "", [_trial(HOLDS, fired=["d"])] * 2 + [_trial(HOLDS)]) is None
    # another defect's marker is not this one's
    assert dcc.side_evidence("d", "", [_trial(HOLDS, fired=["e"])]) is None
    assert dcc.side_evidence("d", "", []) is None


def test_relation_side_beats_same_screen_beats_other():
    meta = {"kind": "display", "marker": "x"}
    side = dcc.judge_candidate("s", meta, [_trial(HOLDS, diff_steps=[1], marker_steps=[1])], 1, 0.1)
    same = dcc.judge_candidate("m", meta, [_trial(HOLDS)], 1, 0.9)
    other = dcc.judge_candidate("o", meta, [_trial(HOLDS)], 1, 0.69)
    assert (side["relation"], same["relation"], other["relation"]) == ("side", "same-screen", "other")
    dead = dcc.judge_candidate("z", meta, [_trial(VIOLATED)], 1, 1.0)
    cands = {"o": other, "z": dead, "m": same, "s": side}
    # file order o, m, s, z — ranking is by relation first, file order within
    assert dcc.rank_controls(cands, ["o", "m", "s", "z"]) == ["s", "m", "o"]


def test_rejudge_reapplies_rules_from_stored_trials():
    stored = {"repeat": 3, "candidates": {
        # recorded as side on a bare diff by the old rule
        "noise": {"trials": [_trial(HOLDS, diff_steps=[1])] * 3, "screen_overlap": 0.9,
                  "relation": "side", "evidence": "diff", "eligible": True},
        "marked": {"trials": [_trial(HOLDS, diff_steps=[2], marker_steps=[2])] * 3,
                   "screen_overlap": 0.1},
        "dead": {"trials": [_trial(CRASHED)], "screen_overlap": 1.0},
    }}
    defects = {"noise": {"kind": "functional", "marker": ""},
               "marked": {"kind": "display", "marker": "x"},
               "dead": {"kind": "functional", "marker": ""}}
    controls, der = dcc.rejudge({"id": "c"}, defects, stored)
    assert controls == ["marked", "noise"]
    assert der["candidates"]["noise"]["relation"] == "same-screen"
    assert der["candidates"]["marked"]["relation"] == "side"
    assert der["candidates"]["dead"]["eligible"] is False
    assert der["specificity"] == "scored"


def test_rank_is_file_order_within_a_relation():
    c = {d: {"eligible": True, "relation": "other"} for d in ("b", "a", "c")}
    assert dcc.rank_controls(c, ["c", "a", "b"]) == ["c", "a", "b"]


# ── same-screen from truth screens ────────────────────────────────────────────

def test_defect_screens_and_overlap():
    doc = {"test_cases": [
        {"id": "home", "bugs": ["disp"]},
        {"id": "crashy", "bugs": ["boom"]},
    ]}
    listing = ["Medicine", "Aspirin (10 left, 9/21/26)", "Add medicine", "Overview"]
    truth = {
        "home": {"side": [{"bug": "disp", "visible_steps": [2]}], "blocking": None,
                 "diff": [{"step": 2}],
                 "screens": {"clean": [["Overview"], listing]}},
        "crashy": {"side": [], "blocking": "boom", "diff": [{"step": 3}, {"step": 4}],
                   "screens": {"clean": [["Overview"], ["Edit", "Save"], ["Detail", "Notes"], ["x"]]}},
    }
    screens = dcc.defect_screens(truth, doc)
    assert screens["disp"] == [dcc.screen_key(listing)]
    assert screens["boom"] == [dcc.screen_key(["Detail", "Notes"])]  # first diff step only
    # the date is masked, so another day's list is the same screen
    other_day = ["Medicine", "Aspirin (10 left, 9/30/26)", "Add medicine", "Overview"]
    assert dcc.same_screen([["Overview"], other_day], screens["disp"]) == 1.0
    assert dcc.same_screen([["Settings"]], screens["disp"]) == 0.0


# ── target, derivation, truth merge ───────────────────────────────────────────

def test_target_status_reads_the_corpus_gate():
    row = {"bugs": ["t"], "expected": "FAIL", "measured": "FAIL", "agrees": True,
           "passes": {"seeded": {"outcome": VIOLATED}}}
    assert dcc.target_status(row)["confirmed"] is True
    assert dcc.target_status({**row, "measured": "PASS"})["confirmed"] is False
    assert dcc.target_status({"bugs": [], "expected": "PASS", "agrees": True})["confirmed"] is False


def test_fingerprint_moves_with_the_route_and_the_candidate_set():
    case = {"id": "c", "bugs": ["t"], "check": {"steps": [{"launch": True}], "expect": {"present": "A"}}}
    defects = {"t": {}, "d": {}}
    fp = journey.controls_fingerprint(case, defects)
    assert fp == journey.controls_fingerprint(dict(case), dict(defects))
    moved = {**case, "check": {"steps": [{"launch": True}, {"tap": "B"}], "expect": {"present": "A"}}}
    assert journey.controls_fingerprint(moved, defects) != fp
    assert journey.controls_fingerprint(case, {**defects, "new": {}}) != fp


def test_merge_touches_only_the_two_keys():
    truth = {"a": {"bugs": ["t"], "agrees": True}, "b": {"bugs": [], "agrees": True}}
    before_b = json.dumps(truth["b"])
    dcc.merge_into_truth(truth, "a", ["d"], {"candidates": {}})
    assert list(truth["a"]) == ["bugs", "agrees", journey.CONTROLS_KEY, journey.CONTROL_DERIVATION_KEY]
    assert json.dumps(truth["b"]) == before_b
    with pytest.raises(KeyError):
        dcc.merge_into_truth(truth, "missing", [], {})


def test_report_counts_na_and_side():
    lines = [
        {"status": "ok", "target": ["t"], "target_confirmed": True, "candidates": 3,
         "eligible": 2, "controls": [("s", "side"), ("o", "other")]},
        {"status": "ok", "target": ["t"], "target_confirmed": True, "candidates": 2,
         "eligible": 0, "controls": []},
        {"status": "not derived", "target": [], "target_confirmed": None, "candidates": 0,
         "eligible": 0, "controls": []},
    ]
    c = dcc.report_counts(lines)
    assert (c["cases"], c["derived"], c["with_control"], c["specificity_na"]) == (3, 2, 1, 1)
    assert c["first_control_side"] == 1 and c["any_side_control"] == 1
    text = dcc.format_report([{**ln, "case": f"c{i}", "app": "x", "ineligible": {}, "specificity": None}
                              for i, ln in enumerate(lines)])
    assert "specificity n/a" in text and "NOT DERIVED" in text


# ── --new-candidates-only (QUA-2870) ─────────────────────────────────────────

def _stored(case, defects, cands, repeat=3):
    """A derivation as derive_app writes it, over `defects`."""
    _, der = dcc.build_derivation(case, defects, cands, {"outcome": HOLDS}, {"bugs": case["bugs"]},
                                  repeat, "2026-09-16T10:00:00-05:00")
    return der


def test_incremental_plan_reuses_only_a_measurement_of_the_same_route():
    case = {"id": "c", "bugs": ["t"], "check": {"steps": [{"launch": True}], "expect": {"present": "A"}}}
    old = {"t": {}, "a": {}, "b": {}}
    meta = {"kind": "functional", "marker": ""}
    cands = {d: dcc.judge_candidate(d, meta, [_trial(HOLDS)] * 3, 3, 0.0) for d in ("a", "b")}
    der = _stored(case, old, cands)
    now = {"t": {}, "a": {}, "new": {}, "b": {}}
    assert dcc.incremental_plan(case, now, der, 3) == (["a", "b"], ["new"])
    # a dropped defect is not reused
    assert dcc.incremental_plan(case, {"t": {}, "a": {}, "new": {}}, der, 3) == (["a"], ["new"])
    # the route moved, the bugs moved, or another --repeat: no reuse
    moved = {**case, "check": {"steps": [{"launch": True}, {"tap": "X"}], "expect": {"present": "A"}}}
    assert dcc.incremental_plan(moved, now, der, 3) is None
    assert dcc.incremental_plan({**case, "bugs": ["a"]}, now, der, 3) is None
    assert dcc.incremental_plan(case, now, der, 5) is None
    assert dcc.incremental_plan(case, now, None, 3) is None


def test_extended_derivation_is_current_and_records_its_reuse():
    case = {"id": "c", "bugs": ["t"], "check": {"steps": [{"launch": True}], "expect": {"present": "A"}}}
    old = {"t": {}, "a": {}, "b": {}}
    meta = {"kind": "functional", "marker": ""}
    cands = {"a": dcc.judge_candidate("a", meta, [_trial(HOLDS)] * 3, 3, 0.9),
             "b": dcc.judge_candidate("b", meta, [_trial(VIOLATED)], 3, 0.0)}
    der = _stored(case, old, cands)
    now = {"t": {}, "a": {}, "b": {}, "new": {}}
    fresh = {"new": dcc.judge_candidate("new", meta, [_trial(HOLDS, fired=["new"])] * 3, 3, 0.0)}
    controls, ext = dcc.extend_derivation(case, now, der, fresh, {"outcome": HOLDS, "steps_run": 1},
                                          {"bugs": ["t"]}, 3, "2026-09-16T10:00:00-05:00")
    assert ext["fingerprint"] == journey.controls_fingerprint(case, now)
    assert set(ext["candidates"]) == {"a", "b", "new"}
    assert ext["candidates"]["a"]["trials"] == cands["a"]["trials"]          # reused verbatim
    assert ext["candidates"]["a"]["screen_overlap"] == 0.9
    assert controls == ["new", "a"]                     # side (fired) first, then same-screen
    assert ext["incremental"]["reused"] == ["a", "b"]
    assert ext["incremental"]["derived"] == ["new"]
    assert ext["incremental"]["reused_from"]["fingerprint"] == der["fingerprint"]
    # the report reads it as current
    doc = {"test_cases": [case], "defects": [{"id": d} for d in now]}
    line = dcc.case_report("x", doc, {"c": {journey.CONTROLS_KEY: controls,
                                            journey.CONTROL_DERIVATION_KEY: ext}})[0]
    assert line["status"] == "ok"


def test_incremental_plan_extends_an_early_stopped_derivation():
    """An early-stopped derivation's fingerprint covers the candidates it never replayed;
    those are new to measure, not a reason to re-derive the whole case."""
    case = {"id": "c", "bugs": ["t"], "check": {"steps": [{"launch": True}], "expect": {"present": "A"}}}
    old = {"t": {}, "a": {}, "b": {}, "skip": {}}
    meta = {"kind": "functional", "marker": ""}
    cands = {d: dcc.judge_candidate(d, meta, [_trial(HOLDS)] * 3, 3, 0.0) for d in ("a", "b")}
    _, der = dcc.build_derivation(case, old, cands, {"outcome": HOLDS}, {"bugs": ["t"]}, 3, "x",
                                  dcc.early_stop_block(2, ["a", "b", "skip"], cands))
    now = {**old, "new": {}}
    assert dcc.incremental_plan(case, now, der, 3) == (["a", "b"], ["skip", "new"])


class _Res:
    def __init__(self, outcome, fired=()):
        self.outcome, self.fired, self.steps_run, self.detail = outcome, set(fired), 1, ""


def _fake_device(monkeypatch, outcomes):
    """dj.one_pass stubbed: the clean reference HOLDS, a candidate gets `outcomes[d]`;
    every replayed flag set is logged."""
    replayed: list[str] = []

    async def one_pass(serial, bundle, claim, flags, *rest):
        if flags:
            replayed.append(flags[0])
        res = _Res(HOLDS if not flags else outcomes.get(flags[0], HOLDS))
        return res, [], [res]

    monkeypatch.setattr(dcc.dj, "one_pass", one_pass)
    monkeypatch.setattr(dcc.dj, "_claim", lambda case: object())
    monkeypatch.setattr(dcc.dj, "_pass_entry", lambda t: {"outcome": t[0].outcome,
                                                          "detail": "", "attempts": 1})
    return replayed


def _derive(case, defects, overlaps, lethal, stop_after, reuse=None):
    import asyncio
    return asyncio.run(dcc.derive_case("s", "b", case, defects, (None, None, None), None, 3,
                                       overlaps, lethal, stop_after, reuse))


def test_new_candidates_only_reaches_what_a_fresh_early_stop_derive_reaches(monkeypatch):
    """QUA-2870 x QUA-2867: reuse walks rank rule 2's replay order under the same early
    stop, taking a stored candidate's trials instead of replaying it. With the same trial
    outcomes it ends with the controls a fresh derive gets, at a fraction of the replays."""
    meta = {"kind": "functional", "marker": ""}
    defects = {d: dict(meta) for d in ("t", "near", "lethal-near", "far1", "far2", "new1", "new2")}
    overlaps = {"near": 0.9, "lethal-near": 0.95, "new1": 0.8, "far1": 0.1, "far2": 0.2,
                "new2": 0.0}
    lethal = {d: (d == "lethal-near", "") for d in defects}
    outcomes = {"far1": VIOLATED}
    case = {"id": "c", "bugs": ["t"]}

    replayed = _fake_device(monkeypatch, outcomes)
    fresh, _, fresh_early = _derive(case, defects, overlaps, lethal, 3)
    fresh_controls = dcc.rank_controls(fresh, list(defects))
    assert replayed == ["near"] * 3 + ["new1"] * 3 + ["far2"] * 3   # stops at 3 eligible

    # the stored derivation: every candidate but the two new ones, measured
    stored_trials = {d: {"trials": [_trial(outcomes.get(d, HOLDS))] * (1 if d in outcomes else 3),
                         "screen_overlap": overlaps.get(d, 0.0)}
                     for d in ("near", "lethal-near", "far1", "far2")}
    stored = {"repeat": 3, "candidates": stored_trials}
    reuse = dcc.stored_candidates(defects, stored, list(stored_trials), 3, lethal)
    replayed.clear()
    inc, clean, early = _derive(case, defects, overlaps, lethal, 3, reuse)
    assert replayed == ["new1"] * 3          # the near new defect; never a stored one
    assert early["order"] == fresh_early["order"]
    assert set(inc) == set(fresh)                         # the same candidates reached
    new = {d: c for d, c in inc.items() if d not in reuse}
    controls, der = dcc.extend_derivation(case, defects, stored, new, clean, {"bugs": ["t"]},
                                          3, "x", lethal=lethal, early_stop=early,
                                          reused=[d for d in inc if d in reuse])
    assert controls == fresh_controls
    assert der["rank_rule"] == dcc.RANK_RULE and der["early_stop"] == early
    assert der["incremental"]["derived"] == sorted(new)
    assert set(der["incremental"]["reused"]) == {d for d in inc if d in reuse}
    assert "lethal-near" not in controls                  # rule 2: a reserve, never first


def test_new_candidates_only_exhaustive_replays_every_new_defect_and_nothing_stored(monkeypatch):
    meta = {"kind": "functional", "marker": ""}
    defects = {d: dict(meta) for d in ("t", "a", "b", "new1", "new2")}
    case = {"id": "c", "bugs": ["t"]}
    stored = {"repeat": 3, "candidates": {d: {"trials": [_trial(HOLDS)] * 3, "screen_overlap": 0.0}
                                          for d in ("a", "b")}}
    reuse = dcc.stored_candidates(defects, stored, ["a", "b"], 3, {})
    replayed = _fake_device(monkeypatch, {})
    cands, _, early = _derive(case, defects, {}, {}, 0, reuse)
    assert sorted(replayed) == ["new1"] * 3 + ["new2"] * 3
    assert set(cands) == {"a", "b", "new1", "new2"} and early is None


# ── the committed corpus ──────────────────────────────────────────────────────

def test_every_public_case_carries_a_current_control_derivation():
    """A `derive_journey.py` re-derive replaces a row and drops its controls; a route
    edit makes them stale. Either must be re-derived here, not discovered by the grader."""
    bad = []
    for app in dcc.journey_apps():
        doc = journey.load_cases(app)
        for line in dcc.case_report(app, doc, journey.load_truth(app)):
            if line["status"] != "ok":
                bad.append(f"{line['case']}: {line['status']}")
            else:
                for t in range(3):
                    sel = journey.control_for_trial(journey.load_truth(app)[line["case"]], t)
                    assert sel["specificity"] in ("scored", journey.SPECIFICITY_NA)
                    assert sel["control"] not in line["target"]
    assert not bad, "run scripts/derive_create_controls.py --skip-derived for: " + ", ".join(bad)


def test_committed_controls_were_derived_at_repeat_3():
    for app in dcc.journey_apps():
        for cid, row in journey.load_truth(app).items():
            der = row.get(journey.CONTROL_DERIVATION_KEY) or {}
            assert der.get("repeat", 0) >= 3, cid
            for d, cand in (der.get("candidates") or {}).items():
                if cand["eligible"]:
                    assert [t["outcome"] for t in cand["trials"]] == [rp.HOLDS] * 3, (cid, d)


# ── rank rule 2: authored routes (QUA-2867) ───────────────────────────────────

def _cand(relation, lethal, eligible=True):
    c = dcc.judge_candidate("x", {"kind": "functional", "marker": ""}, [_trial(HOLDS)], 1,
                            0.9 if relation == "same-screen" else 0.1,
                            lethal=(lethal, "test"))
    c.update(relation=relation, eligible=eligible,
             reach_risk=dcc.reach_risk(relation, lethal))
    return c


def test_reach_risk():
    assert dcc.reach_risk("side", False) is None
    assert dcc.reach_risk("other", None) is None
    assert dcc.reach_risk("other", True) == dcc.LETHAL_ELSEWHERE
    assert dcc.reach_risk("same-screen", True) == dcc.LETHAL_ADJACENT
    assert dcc.reach_risk("side", True) == dcc.LETHAL_ADJACENT


def test_rule2_live_before_lethal_and_lethal_adjacent_is_a_reserve():
    cands = {"crash-near": _cand("same-screen", True), "crash-far": _cand("other", True),
             "live-far": _cand("other", False), "live-side": _cand("side", False),
             "broken": _cand("other", False, eligible=False)}
    order = list(cands)
    assert dcc.rank_controls(cands, order, rule=1) == ["live-side", "crash-near", "crash-far",
                                                       "live-far"]
    assert dcc.rank_controls(cands, order) == ["live-side", "live-far", "crash-far"]
    assert dcc.reserves(cands) == ["crash-near"]
    with pytest.raises(ValueError):
        dcc.rank_controls(cands, order, rule=9)


def test_rule2_falls_back_to_reserves_rather_than_specificity_na():
    cands = {"b": _cand("side", True), "a": _cand("same-screen", True)}
    assert dcc.rank_controls(cands, ["a", "b"]) == ["b", "a"]


def test_judge_without_lethality_ranks_exactly_as_rule_1():
    meta = {"kind": "display", "marker": "x"}
    cands = {d: dcc.judge_candidate(d, meta, [_trial(HOLDS)], 1, ov)
             for d, ov in (("o", 0.1), ("m", 0.9))}
    assert "reach_risk" not in cands["o"]
    assert dcc.rank_controls(cands, ["o", "m"]) == dcc.rank_controls(cands, ["o", "m"], rule=1)


def test_replay_order_is_static_live_near_display_first_lethal_near_last():
    defects = {"t": {"kind": "functional"}, "crash-near": {"kind": "functional"},
               "far": {"kind": "functional"}, "near-func": {"kind": "functional"},
               "near-disp": {"kind": "display"}, "crash-far": {"kind": "functional"}}
    overlaps = {"crash-near": 1.0, "far": 0.2, "near-func": 0.95, "near-disp": 0.8,
                "crash-far": 0.1}
    lethal = {"crash-near": (True, ""), "crash-far": (True, "")}
    assert dcc.replay_order(defects, {"t"}, overlaps, lethal) == [
        "near-disp", "near-func", "far", "crash-far", "crash-near"]


def test_stop_reached_counts_only_eligible_non_reserves():
    cands = {"a": _cand("side", False), "b": _cand("same-screen", True),
             "c": _cand("other", False, eligible=False)}
    assert not dcc.stop_reached(cands, 2)
    assert dcc.stop_reached(cands, 1)
    assert not dcc.stop_reached(cands, 0)          # 0 = exhaustive


def test_simulated_early_stop_replays_less_and_lists_the_unreplayed():
    defects = {d: {"kind": "functional", "marker": ""} for d in "abcde"}
    stored = {"repeat": 3, "candidates": {
        d: {"trials": [_trial(HOLDS)] * 3, "screen_overlap": 0.1} for d in "bcde"}}
    stored["candidates"]["b"]["trials"] = [_trial(CRASHED)]
    case = {"id": "c", "bugs": ["a"]}
    controls, der = dcc.simulate_early_stop(case, defects, stored, {}, 2)
    assert controls == ["c", "d"]
    assert der["early_stop"]["replays"] == 1 + 3 + 3       # b broke on its first trial
    assert der["early_stop"]["unreplayed"] == ["e"]
    assert der["rank_rule"] == dcc.RANK_RULE
    full, _ = dcc.simulate_early_stop(case, defects, stored, {}, 0)
    assert full == ["c", "d", "e"]


def test_defect_lethality_comes_from_the_defects_own_case():
    from qualgentbench.create import detection
    doc, truth = journey.load_cases("tasksorg"), journey.load_truth("tasksorg")
    assert detection.defect_lethal(doc, truth, "subtask-filed-before-written")[0] is True
    assert detection.defect_lethal(doc, truth, "repeat-complete-crash")[0] is True
    assert detection.defect_lethal(doc, truth, "subtasks-left-open")[0] is False
    assert detection.defect_lethal(doc, truth, "due-section-shifted")[0] is False
    # no case seeds it: the class decides; an unknown trigger class is taken as lethal
    doc2 = {"defects": [{"id": "x", "class": "crash"}, {"id": "y", "class": "persistence"},
                        {"id": "z", "class": "ordering"}], "test_cases": []}
    assert [detection.defect_lethal(doc2, {}, d)[0] for d in "xyz"] == [True, False, True]


def test_rule2_moves_the_qua2861_reached_control_to_a_reserve():
    """QUA-2861 rerun: tasks-complete-parent's authored routes reached its rank-2 control
    `subtask-filed-before-written` (same-screen, an ordering crash) on 2/2 runs. Rule 2
    over the SAME committed trials holds it back; the committed truth itself is untouched
    (rank_rule absent = rule 1) until `--rejudge --write`."""
    doc, truth = journey.load_cases("tasksorg"), journey.load_truth("tasksorg")
    row = truth["tasks-complete-parent"]
    assert row[journey.CONTROLS_KEY] == ["due-section-shifted", "subtask-filed-before-written",
                                         "repeat-complete-crash"]
    assert "rank_rule" not in row[journey.CONTROL_DERIVATION_KEY]
    lines = {r["case"]: r for r in dcc.rule_report("tasksorg", doc, truth)}
    tcp = lines["tasks-complete-parent"]
    assert tcp["rule2"] == ["due-section-shifted", "repeat-complete-crash"]
    assert tcp["reserves"] == ["subtask-filed-before-written"]
    assert tcp["early_stop_replays"] <= tcp["replays"]


def test_early_stop_over_the_committed_corpus_costs_less_and_loses_no_control_quality():
    rows = []
    for app in dcc.journey_apps():
        rows += dcc.rule_report(app, journey.load_cases(app), journey.load_truth(app))
    assert rows
    assert sum(r["early_stop_replays"] for r in rows) < 0.7 * sum(r["replays"] for r in rows)
    worse = [r["case"] for r in rows if r["early_stop_profile"] != r["rule2_profile"]]
    assert not worse, worse
    # no case loses every control to the reserve rule
    assert all(r["rule2"] or not r["committed"] for r in rows)
    text = dcc.format_rule_report(rows)
    assert "rank rule 2" in text and "early stop at 3 eligible" in text
