"""Gate G3, complete (QUA-3587): the customer-shaped report has an observed result, an
expected result and reproduction steps that are steps. Each sub-check is tested alone,
then the gate, the `complete_if_steps_carried` side figure and what `quality.json`
records."""

from __future__ import annotations

import json

import pytest
from test_customer_report import _claude, _entry, _fail, _report_call, _saved

from qualgentbench import report_quality as rq

SUMMARY = "Saving a note loses it"
NUMBERED = "1. Open the notes list\n2. Tap the add button\n3. Type Milk\n4. Tap Save"
INVESTIGATION = ("NotesRepository.save() returns before the insert commits, so the list "
                 "reloads from stale rows.")
AGENT_STEPS = [{"step": 1, "description": "Open the notes list", "status": "passed"},
               {"step": 2, "description": "Tap the add button", "status": "passed"},
               {"step": 3, "description": "Save the note", "status": "failed"}]


def _report(**over) -> rq.CustomerReport:
    fields = {"index": 0, "summary": SUMMARY, "steps": NUMBERED,
              "expected": "The note is listed", "actual": "The note is missing",
              "failure_step": "Step 4: Save", **over}
    return rq.CustomerReport(**fields)


def _scored(tmp_path, args: dict | None, name: str = "ep1", **entry) -> dict:
    """quality.json's entry for one saved, matched report; `args` is the report call."""
    report = {**_entry(3, "The note is missing", "The note is listed", "saving loses it"),
              "matched": "save-bug", **entry}
    transcript = _claude([_report_call(args)]) if args is not None else ""
    d = _saved(tmp_path, {"report_source": "findings_file", "reports": [report]},
               transcript, name=name)
    return rq.score_episode(d)


# ── sub-check: observed, expected ──────────────────────────────────────────────

@pytest.mark.parametrize(("actual", "expected", "observed_ok", "expected_ok"), [
    ("The note is missing", "The note is listed", True, True),
    (None, None, False, False),
    ("", "The note is listed", False, True),
    ("The note is missing", "", True, False),
])
def test_observed_and_expected_are_separate_sub_checks(actual, expected, observed_ok,
                                                       expected_ok):
    checks = rq.complete_checks(_report(actual=actual, expected=expected))
    assert (checks["observed"], checks["expected"]) == (observed_ok, expected_ok)
    assert checks["steps"] is True          # the steps check does not depend on them


# ── sub-check: steps that are steps ────────────────────────────────────────────

@pytest.mark.parametrize("steps", [
    NUMBERED,
    "- Open the notes list\n- Long-press the first note\n- Select Delete",
    "Steps to reproduce:\n1) Launch the app\n2) Go back\n3) Verify the list is empty",
    "1. Open the app 2. Swipe left on the note 3. Press Delete",
    "Open the app -> tap Add -> type Milk -> tap Save",
    "Open the app. Tap Add. Observe that the note is missing.",
    "✓ Step 1: Opened the Medicine tab\n✓ Step 2: Tapped Ibuprofen\n✗ Step 3: Scrolled down",
    "On the Cart screen, tap Pay\nThen the user selects Card\nFinally, press Confirm",
    AGENT_STEPS,
    ["✓ Opened the Analysis tab.", "✗ Tapped Tabular view; the charts stayed visible."],
])
def test_an_action_list_is_steps(steps):
    passed, reason = rq.steps_are_steps(steps, SUMMARY)
    assert passed, reason


@pytest.mark.parametrize(("steps", "why"), [
    (INVESTIGATION, "1 entry"),                                   # code-investigation prose
    ("The save handler commits late.\nThe list then reloads from stale rows.",
     "entry 1 of 2 is not a UI action"),
    ("Open NotesRepository.kt\nSearch for the save call\nRead the insert",
     "entry 1 of 3 is not a UI action"),                          # names a source file
    ("Open the editor\nCall viewModel.save()", "entry 2 of 2"),   # names a function call
    (SUMMARY, "repeat the summary"),
    (f"  {SUMMARY.upper()}. ", "repeat the summary"),
    ("Tap Save", "1 entry"),
    ("Open the app\nThe total is wrong", "entry 2 of 2 is not a UI action"),
    ("Verify the list\nCheck the total\nObserve the title", "0 of 3 entries are UI actions"),
    ("Open the app\nVerify the total", "1 of 2 entries are UI actions"),
    ("", "no steps"),
    ((), "no steps"),
    ([{"step": 3, "status": "failed"}], "1 entry"),
    ([{"step": 1, "status": "passed"}, {"step": 2, "status": "failed"}],
     "entry 1 of 2 is not a UI action"),
    (None, "no steps"),
])
def test_what_is_not_a_step_list(steps, why):
    passed, reason = rq.steps_are_steps(steps, SUMMARY)
    assert not passed and why in reason, reason


def test_the_verb_lists_are_fixed_and_inflected():
    assert {"tap", "type", "open", "swipe", "press", "select", "launch", "go",
            "scroll"} <= set(rq.ACTION_VERBS)
    assert not set(rq.ACTION_VERBS) & set(rq.OBSERVE_VERBS)
    for entry in ("tap Save", "Taps Save", "Tapped Save", "Tapping Save", "Went back",
                  "Double-tap the row", "Long press the row", "Chose Task", "3. Typed Milk"):
        assert rq.step_kind(entry) == "action", entry
    assert rq.step_kind("Waited five seconds") == "observation"
    for entry in ("The list is empty", "Savings are shown", "Opening hours are wrong?",
                  "", "42"):
        # "Opening" is a form of "open": the first word decides, by design.
        expected = "action" if entry.startswith("Opening") else None
        assert rq.step_kind(entry) == expected, entry


# ── the gate ───────────────────────────────────────────────────────────────────

def test_the_product_mapping_fails_g3_and_the_carried_steps_would_pass(tmp_path):
    """The bug's `steps` is the code investigation (or the summary), so G3 fails; the
    agent's own step list, which the bug drops, would have passed."""
    for name, investigation, why in (("with", INVESTIGATION, "1 entry"),
                                     ("without", None, "repeat the summary")):
        q = _scored(tmp_path, _fail(code_investigation=investigation, steps=AGENT_STEPS),
                    name=name)
        (report,) = q["reports"]
        assert report["customer_report"]["report_source"] == "report_tool"
        assert report["gates"]["G3"]["pass"] is False
        assert why in report["gates"]["G3"]["reason"]
        assert report["complete"] == {
            "observed": True, "expected": True, "steps": False,
            "steps_reason": report["complete"]["steps_reason"],
            "agent_steps": True, "agent_steps_reason": "3 entries, 3 UI actions",
            "complete_if_steps_carried": True}
        assert report["customer_ready"] is False
        assert q["totals"]["gates"]["G3"] == {"name": "complete", "evaluated": 1, "passed": 0}
        assert q["totals"]["complete"] == {
            "evaluated": 1, "observed": 1, "expected": 1, "steps": 0, "complete": 0,
            "agent_steps": 1, "complete_if_steps_carried": 1}


def test_a_report_whose_bug_carries_an_action_list_passes(tmp_path):
    q = _scored(tmp_path, _fail(code_investigation=NUMBERED, steps=None))
    (report,) = q["reports"]
    assert report["gates"]["G3"] == {
        "pass": True, "reason": "observed, expected and steps (4 entries, 4 UI actions)"}
    assert report["complete"]["agent_steps"] is False
    assert report["complete"]["complete_if_steps_carried"] is False
    # G3 passed and G1 passed, but G2 and G4 are not known here: never "ready".
    assert report["customer_ready"] is not True


def test_each_missing_part_is_named_in_the_reason(tmp_path):
    q = _scored(tmp_path, _fail(code_investigation=NUMBERED, expected=None))
    (report,) = q["reports"]          # one side missing: the bug keeps neither
    assert report["gates"]["G3"] == {
        "pass": False, "reason": "no observed result; no expected result"}
    assert (report["complete"]["observed"], report["complete"]["expected"],
            report["complete"]["steps"]) == (False, False, True)
    assert report["complete"]["complete_if_steps_carried"] is False


def test_a_findings_entry_has_no_steps_and_fails(tmp_path):
    """A rendered findings entry has observed and expected but no steps text and no
    agent step list, so G3 and the carried figure are both false, by construction."""
    q = _scored(tmp_path, None)
    (report,) = q["reports"]
    assert report["customer_report"]["report_source"] == "findings"
    assert report["gates"]["G3"] == {"pass": False, "reason": "steps: no steps"}
    assert report["complete"]["observed"] and report["complete"]["expected"]
    assert report["complete"]["agent_steps_reason"] == "no steps"
    assert report["complete"]["complete_if_steps_carried"] is False


def test_g3_runs_on_invalid_reports_and_not_on_neutral_ones(tmp_path):
    reports = [_entry(1, "Total: 4", "Total: 3", "the total looks wrong"),      # false
               {**_entry(3, "Empty list", "One note", "list is empty"),
                "consequence_of": "save-bug"}]                                  # neutral
    d = _saved(tmp_path, {"report_source": "findings_file", "reports": reports})
    false, neutral = rq.score_episode(d)["reports"]
    assert false["gates"]["G1"]["pass"] is False and false["gates"]["G3"]["pass"] is False
    assert neutral["gates"] == {} and neutral["complete"] is None
    assert rq.score_episode(d)["totals"]["complete"]["evaluated"] == 1


def test_g3_is_registered_as_a_headline_gate_and_is_deterministic(tmp_path):
    assert [(g.id, g.name) for g in rq.GATES if g.id == "G3"] == [("G3", "complete")]
    assert "G3" in rq.HEADLINE_GATES
    d = _saved(tmp_path, {"report_source": "findings_file", "reports": [
        {**_entry(3, "The note is missing", "The note is listed", "lost"),
         "matched": "save-bug"}]}, _claude([_report_call(_fail(steps=AGENT_STEPS))]))
    first = rq.write_quality(d).read_bytes()
    assert rq.write_quality(d).read_bytes() == first
    assert json.loads(first)["totals"]["complete"]["complete_if_steps_carried"] == 1
