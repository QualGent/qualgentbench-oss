"""Reference reports (`qualgentbench.reference_reports`, lint rule `refreport`): one
hand-written, customer-shaped report per seeded defect per case.

The real corpus must carry one for every `bugs:` entry and each must hold up against its
truth row; each check is also exercised on a small synthetic case. A report is
harness-only: it must never reach a task spec or a brief, and adding the module must not
move the scorer pin (`tests/test_scorer_version.py` covers the pin itself)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from qualgentbench import bugs, corpus, journey
from qualgentbench import reference_reports as rr

_PATH = Path(__file__).parents[1] / "scripts" / "lint_journey_cases.py"
_spec = importlib.util.spec_from_file_location("lint_journey_cases", _PATH)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


DEFECTS = [
    {"id": "avg-bug", "kind": "display", "class": "content-format", "tier": "L2",
     "marker": "Avg: 76 kg", "symptoms": ["average", "avg"]},
    {"id": "delete-bug", "kind": "functional", "class": "persistence", "tier": "L1",
     "symptoms": ["still listed", "not deleted"]},
    {"id": "save-bug", "kind": "functional", "class": "persistence", "tier": "L1",
     "symptoms": ["not saved"]},
]

GOOD = {
    "summary": "Statistics shows the wrong average weight",
    "steps": ["Launch the app.", "Tap Statistics."],
    "expected": "The Weight card shows Avg: 79 kg.",
    "actual": "The Weight card shows Avg: 76 kg.",
    "failure_step": "Step 2: Read the Weight card",
}

TRUTH_ROW = {
    "side": [{"bug": "avg-bug", "marker": "Avg: 76 kg", "visible_steps": [2],
              "texts": ["Avg: 76 kg"]}],
    "unclaimed_diff": [],
    "screens": {"seeded": [["Statistics"], ["Weight", "Avg: 76 kg", "Max: 85 kg"]]},
}


def _case(report=GOOD, bug="avg-bug", **over) -> dict:
    entry = {"id": bug}
    if report is not None:
        entry["reference_report"] = report
    base = {
        "id": "case-1", "name": "Read the statistics",
        "steps": ["Open Statistics.", "Read the Weight card."],
        "expected_outcome": "The Weight card shows a maximum of 85 kg.",
        "check": {"steps": ["launch", {"tap": "Statistics"}],
                  "expect": {"present": "Max: 85 kg"}},
        "bugs": [entry],
    }
    base.update(over)
    return base


def _problems(report, row=TRUTH_ROW, bug="avg-bug", **over) -> list[str]:
    case = _case(report, bug=bug, **over)
    return rr.problems(case, journey.load_defects({"defects": DEFECTS}), row, bug, report)


def _with(**changes) -> dict:
    return {**GOOD, **changes}


# ── the real corpus ────────────────────────────────────────────────────────────

def test_every_seeded_defect_of_every_public_case_has_a_reference_report():
    for app_id in corpus.public_apps():
        assert rr.missing(app_id) == [], app_id
    reports = rr.load_all(heldout=False)
    pairs = sum(len(journey.case_bugs(c)) for a in corpus.public_apps()
                for c in (journey.load_cases(a) or {}).get("test_cases") or [])
    assert len(reports) == pairs > 0
    assert len({(r.app_id, r.case_id, r.defect_id) for r in reports}) == pairs


def test_the_real_reference_reports_lint_clean():
    results = lint.lint_corpus()
    found = [f"{app} {f}" for app, fs in results.items() for f in fs if f.rule == "refreport"]
    assert found == []


def test_a_reference_report_never_reaches_a_task_spec_or_a_brief():
    """`journey.case_bugs` keeps id and marker only. If a report rode into `bug_spec` it
    would sit one serialisation away from the agent's workspace."""
    suites = {str(s["app"]["id"]): s for s in bugs.load_apps()}
    checked = 0
    for app_id in corpus.public_apps():
        reports = rr.load(app_id)
        assert reports
        tasks = journey.journey_tasks(suites[app_id])
        blob = json.dumps([t.bug_spec for t in tasks], default=str)
        briefs = "\n".join(journey.brief(t, "emulator-5554", "raw") for t in tasks)
        assert rr.KEY not in blob
        for r in reports:
            assert r.summary not in blob and r.summary not in briefs
            checked += 1
    assert checked


def test_every_case_file_keeps_its_contamination_canary_on_line_one():
    for app_id in corpus.public_apps():
        first = journey.cases_path(app_id).read_text().split("\n", 1)[0]
        assert "QGB-CANARY-" in first, app_id


# ── loading ────────────────────────────────────────────────────────────────────

def test_case_references_reads_the_bugs_entry_and_normalises_whitespace():
    case = _case(_with(actual="The Weight card\n  shows Avg: 76 kg."))
    ref = rr.case_references(case, "app")["avg-bug"]
    assert (ref.app_id, ref.case_id, ref.defect_id) == ("app", "case-1", "avg-bug")
    assert ref.actual == "The Weight card shows Avg: 76 kg."
    assert ref.steps == ("Launch the app.", "Tap Statistics.")
    assert ref.step_number == 2


def test_a_bare_bug_id_has_no_report():
    assert rr.case_references({"id": "c", "bugs": ["avg-bug"]}) == {}


def test_customer_report_and_the_evidence_removed_control():
    ref = rr.parse(GOOD, case_id="case-1", defect_id="avg-bug")
    full = ref.customer_report()
    assert list(full) == list(rr.FIELDS)
    assert full["steps"] == ["Launch the app.", "Tap Statistics."]
    control = ref.customer_report(with_steps=False)
    assert control["steps"] == [] and {k: v for k, v in control.items() if k != "steps"} \
        == {k: v for k, v in full.items() if k != "steps"}


def test_bug_report_uses_the_report_tool_mapping():
    ref = rr.parse(GOOD, case_id="case-1", defect_id="avg-bug")
    args = {"status": "FAIL", "summary": ref.summary, "expected": ref.expected,
            "actual": ref.actual, "failure_step": ref.failure_step}
    assert ref.bug_report().as_dict() == journey.report_from_tool(args).bugs[0].as_dict()


@pytest.mark.parametrize("raw", [
    "a string", {**GOOD, "steps": "Launch the app."}, {**GOOD, "steps": ["Launch", ""]},
    {k: v for k, v in GOOD.items() if k != "expected"}, {**GOOD, "actual": "  "},
    {**GOOD, "severity": "high"},
])
def test_a_malformed_report_is_one_shape_problem(raw):
    found = _problems(raw)
    assert len(found) == 1


# ── the check ──────────────────────────────────────────────────────────────────

def test_a_good_report_has_no_problems():
    assert _problems(GOOD) == []


def test_a_missing_report_is_a_lint_error_and_a_good_one_is_clean():
    doc = {"app": "app", "defects": DEFECTS, "test_cases": [_case(None)]}
    found = [f for f in lint.lint_doc(doc, {"case-1": TRUTH_ROW}) if f.rule == "refreport"]
    assert [f.level for f in found] == ["error"] and "reference_report" in found[0].detail
    doc = {"app": "app", "defects": DEFECTS, "test_cases": [_case(GOOD)]}
    assert [f for f in lint.lint_doc(doc, {"case-1": TRUTH_ROW}) if f.rule == "refreport"] == []


def test_a_report_that_names_the_defect_id_is_refused():
    assert any("defect id" in p for p in _problems(_with(summary="avg-bug: wrong average")))
    assert any("defect id" in p for p in _problems(_with(summary="The avg bug shows a wrong average")))


@pytest.mark.parametrize("steps, needle", [
    (["Launch the app."], "at least 2 steps"),
    (["Launch the app.", "The average is wrong."], "action verb"),
    (["Launch the app.", "Look at the Weight card."], "action verb"),
    (["Tap Statistics.", "Tap the Weight card."], "must launch the app"),
    (["Launch the app.", "Statistics shows the wrong average weight"], "action verb"),
])
def test_steps_that_are_not_steps_are_refused(steps, needle):
    assert any(needle in p for p in _problems(_with(steps=steps)))


def test_every_action_verb_is_accepted_and_long_press_is_press_and_hold():
    steps = ["Launch the app.", "Open the menu.", "Tap Statistics.", 'Type "60" into Weight.',
             "Swipe up.", "Press and hold the card.", "Select Weight.", "Go back to the list.",
             "Scroll to the Weight card.", "Rotate the device to landscape."]
    assert rr.steps_problems(steps, "Something is wrong") == []


def test_steps_that_repeat_the_summary_are_refused():
    found = rr.steps_problems(["Launch the app.", "Tap Statistics."], "Launch the app. Tap Statistics.")
    assert found == ["steps repeat the summary"]


@pytest.mark.parametrize("failure_step, needle", [
    ("Read the Weight card", "must start with `Step N`"),
    ("Step 3: Read the Weight card", "the case has 2"),
    ("Step 0: Launch", "the case has 2"),
])
def test_failure_step_must_name_a_step_of_the_case(failure_step, needle):
    assert any(needle in p for p in _problems(_with(failure_step=failure_step)))


def test_actual_must_quote_a_truth_text():
    found = _problems(_with(actual="The Weight card shows the wrong average."))
    assert any("quotes none of the truth texts" in p for p in found)
    # the clean build's value is not a sighting of the seeded one
    found = _problems(_with(actual="The Weight card shows Avg: 79 kg."))
    assert any("quotes none of the truth texts" in p for p in found)


def test_expected_and_actual_stay_inside_the_product_limit():
    long = "The Weight card shows Avg: 76 kg. " + "x" * rr.RESULT_DETAIL_MAX
    assert any("actual is" in p for p in _problems(_with(actual=long)))


def test_a_report_the_matcher_credits_to_another_defect_is_refused():
    """A blocking report on a case that also seeds a side bug: quoting the side bug's
    marker hands the report to the side bug."""
    row = {**TRUTH_ROW, "unclaimed_diff": [{"step": 2, "added": ["85 kg"], "removed": []}]}
    case_bugs = [{"id": "delete-bug", "reference_report": None}, {"id": "avg-bug"}]
    report = {"summary": "The deleted entry is still listed",
              "steps": ["Launch the app.", "Tap Delete."],
              "expected": "The 85 kg entry is gone.",
              "actual": "The entry still shows 85 kg and the card reads Avg: 76 kg.",
              "failure_step": "Step 2: Read the Weight card"}
    case_bugs[0]["reference_report"] = report
    case = _case(bugs=case_bugs)
    defects = journey.load_defects({"defects": DEFECTS})
    found = rr.problems(case, defects, row, "delete-bug", report)
    assert any("credits it to 'avg-bug'" in p for p in found)
    report = {**report, "actual": "The entry still shows 85 kg."}
    assert rr.problems(case, defects, row, "delete-bug", report) == []


def test_a_defect_that_only_removes_text_is_quoted_in_expected():
    row = {"side": [], "unclaimed_diff": [{"step": 2, "added": [], "removed": ["Total: 4 items"]}],
           "screens": {"seeded": [["List"]]}}
    report = {"summary": "The new entry is not saved",
              "steps": ["Launch the app.", "Tap Save."],
              "expected": "The list footer reads Total: 4 items.",
              "actual": "The footer is missing.",
              "failure_step": "Step 2: Read the list"}
    assert _problems(report, row=row, bug="save-bug") == []
    assert rr.truth_texts(_case(report, bug="save-bug"), journey.load_defects({"defects": DEFECTS}),
                          row, "save-bug")["kind"] == "absence"
    found = _problems({**report, "expected": "The footer shows the total."}, row=row, bug="save-bug")
    assert any("the strings the seeded build lost" in p for p in found)


def test_a_state_only_defect_quotes_a_seeded_screen_string():
    """No diff, no side text: the fault is in stored state the route never shows."""
    row = {"side": [], "unclaimed_diff": [], "screens": {"seeded": [["Contacts", "Alice"]]}}
    report = {"summary": "The phone number is not saved",
              "steps": ["Launch the app.", "Tap Save."],
              "expected": "Alice's contact shows 555-1234.",
              "actual": "Alice's contact shows no phone number.",
              "failure_step": "Step 2: Read the contact"}
    assert _problems(report, row=row, bug="save-bug") == []
    found = _problems({**report, "actual": "No phone number is shown."}, row=row, bug="save-bug")
    assert any("no string of the seeded screens" in p for p in found)


def test_a_crash_report_quotes_the_exception_or_the_platform_dialog():
    case = _case(GOOD, bug="save-bug",
                 check={"steps": ["launch", {"tap": "Save"}],
                        "expect": {"db": "x.db", "query": "select 1;", "equals": "1",
                                   "crash": "IllegalStateException"}})
    defects = journey.load_defects({"defects": [
        {**DEFECTS[2], "class": "crash", "symptoms": ["crash", "crashes", "closed"]}]})
    report = {"summary": "The app crashes when the entry is saved",
              "steps": ["Launch the app.", "Tap Save."],
              "expected": "The entry is saved and listed.",
              "actual": "The app closes. The device log shows IllegalStateException.",
              "failure_step": "Step 2: Read the Weight card"}
    row = {"side": [], "unclaimed_diff": [], "screens": {"seeded": [["List"]]}}
    assert rr.truth_texts(case, defects, row, "save-bug", "Scale")["kind"] == "death"
    assert rr.problems(case, defects, row, "save-bug", report, "Scale") == []
    dialog = {**report, "actual": "The app closes and Android says Scale keeps stopping."}
    assert rr.problems(case, defects, row, "save-bug", dialog, "Scale") == []
    vague = {**report, "actual": "The app closes."}
    assert any("truth texts" in p for p in rr.problems(case, defects, row, "save-bug", vague, "Scale"))


def test_a_date_placeholder_in_a_truth_text_matches_the_real_date():
    assert rr._quotes("Thursday, <date>. 127 days left",
                      journey._norm("The row reads Thursday, January 21, 2027. 127 days left."))
    assert not rr._quotes("Thursday, <date>. 127 days left",
                          journey._norm("The row reads Thursday. 129 days left."))


def test_importing_the_module_leaves_the_scorer_untouched():
    """Quality code lives beside the scorer and is never a ranking input."""
    assert not hasattr(journey, "reference_reports")
    assert journey.SCORER_VERSION == 5
