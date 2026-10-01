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
    assert dcc.side_evidence("d", "", [_trial(HOLDS, diff_steps=[5])] * 2) == "diff"
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
