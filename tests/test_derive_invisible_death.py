"""A seeded death nobody can see is refused at derive time (QUA-2742).

`cal-complete-task` (fossify-calendar, QUA-2726) derived AGREES 3/3 and was still wrong.
Its patch wrote the completion row and THEN threw, inside the task editor — a secondary
activity — so Android finished that activity and restarted the process on the event list
beneath it, and the list showed the task completed. Every recorded screen was identical on
both arms; only logcat knew. On the QUA-2731 board a UI tester who correctly reported PASS
was charged with a missed crash AND a failed completion.

`derive_journey.invisible_death` names that shape — a case that must FAIL, a seeded arm
that DIES, an empty clean/seeded screen diff — and `judge_case` refuses it per trial pair.
Nothing here touches a device: every trial is a scripted (ReplayResult, screens) pair.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from qualgentbench import journey

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_journey.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_journey", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dj = _load()
rp = dj.rp
R = rp.ReplayResult
HOLDS, VIOLATED, CRASHED = rp.HOLDS, rp.VIOLATED, rp.CRASHED

# ── the pre-fix case, exactly as committed at f9d63c4 ─────────────────────────

DEFECTS = {"task-complete-crash": {"kind": "functional", "tier": "L4", "marker": "",
                                   "symptoms": ["crashed", "crash"]}}
PRE_FIX_CASE = {
    "id": "cal-complete-task",
    "name": "Mark a task completed",
    "steps": ["Tap the + button and choose Task.", "Enter the title \"Taxes\" and save.",
              "Open \"Taxes\" from the event list.", "Tap \"Mark completed\".",
              "Look at the event list."],
    "expected_outcome": "The task \"Taxes\" is marked completed and the event list is shown again.",
    "check": {"steps": ["launch", {"tap": "calendar_fab"}, {"tap": "Task"}, {"tap": "task_title"},
                        {"type": "Taxes"}, {"tap": "Save"}, {"tap": "Taxes"},
                        {"tap": "Mark completed"}],
              "expect": {"db": "events.db", "equals": "1",
                         "query": "select count(*) from tasks where task_id="
                                  "(select id from events where title='Taxes');",
                         "crash": "IllegalArgumentException"}},
    "bugs": ["task-complete-crash"],
}

_NEW_TASK = ["Back", "New Task", "Save", "Title", "Description", "All-day", "September 18 (Fri)",
             "05:00 AM", "10 minutes before", "Add another reminder", "No repetition",
             "Local calendar", "Offline (never synced)", "Task color"]
_EDIT_TASK = ["Back", "Edit Task", "Save", "Delete", "Duplicate task", "More options", "Taxes",
              "Description", "All-day", "September 18 (Fri)", "05:00 AM", "10 minutes before",
              "Add another reminder", "No repetition", "Local calendar", "Offline (never synced)",
              "Task color"]
# The event list draws a completed task struck through and dimmed — paint, not text — so
# it reads the SAME before the tap (step 6) and after it (step 8), on either arm. That is
# why the committed truth's `screens.clean == screens.seeded` held for all eight steps.
LIST = ["Search", "Change view", "Settings", "More options", "SEPTEMBER", "18 Friday", "Task",
        "Taxes", "05:00 AM", "New Event"]
EDITOR_OPEN = _EDIT_TASK + ["Mark completed"]
EDITOR_DONE = _EDIT_TASK + ["Mark incomplete"]
PRE_FIX_SCREENS = [
    ["Search", "Change view", "Settings", "More options", "No upcoming events.",
     "Create a new event", "New Event"],
    ["Search", "Change view", "Settings", "More options", "No upcoming events.",
     "Create a new event", "Task", "Event", "New Event"],
    _NEW_TASK, _NEW_TASK, [("Taxes" if t == "Title" else t) for t in _NEW_TASK],
    LIST, EDITOR_OPEN, LIST,
]
CRASH_DETAIL = ("step 8: org.fossify.calendar.debug crashed — java.lang.IllegalArgumentException: "
                "org.fossify.calendar.debug: Targeting S+ (version 31 and above) requires that one of")


def _pair(outcome: str, screens: list[list[str]], detail: str = ""):
    return R(outcome, detail, len(screens)), [list(s) for s in screens]


def _trials(clean: list[list[list[str]]], seeded: list[list[list[str]]],
            clean_outcome: str = HOLDS, seeded_outcome: str = CRASHED) -> dict:
    """One (clean, seeded) screen list per trial pair."""
    return {"clean": [_pair(clean_outcome, s) for s in clean],
            "seeded": [_pair(seeded_outcome, s, CRASH_DETAIL) for s in seeded]}


def _design(case: dict = PRE_FIX_CASE) -> dict:
    return journey.case_design(case, DEFECTS)


# ── the predicate ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expected, outcome, diff, invisible", [
    ("FAIL", CRASHED, [], True),
    ("FAIL", CRASHED, [{"step": 9, "added": ["Mark completed"], "removed": ["Mark incomplete"]}], False),
    # A state failure with the app ALIVE: its oracle failed, not a death.
    ("FAIL", VIOLATED, [], False),
    # A display-only case must PASS; a death there is already a measured-vs-expected problem.
    ("PASS", CRASHED, [], False),
    ("FAIL", None, [], False),
])
def test_invisible_death_is_exactly_fail_plus_death_plus_no_diff(expected, outcome, diff, invisible):
    assert dj.invisible_death(expected, outcome, diff) is invisible


# ── judge_case ────────────────────────────────────────────────────────────────

def test_the_pre_fix_cal_complete_task_is_refused():
    """The mutation this ticket exists for: the committed QUA-2726 shape, three stable
    trials per arm, AGREED before QUA-2742 and must not now."""
    design = _design()
    assert design["expected"] == "FAIL" and design["death"] == "crash"
    row = dj.judge_case(design, _trials([PRE_FIX_SCREENS] * 3, [PRE_FIX_SCREENS] * 3))
    # Everything the derive checked before still holds — the seeded arm really did die.
    assert row["measured"] == "FAIL" and row["stability"]["seeded"]["stable"] is True
    assert row["diff"] == [] and row["unclaimed_diff"] == []
    assert row["agrees"] is False
    assert row["problems"] == [
        "seeded version dies (crashed) but its clean/seeded screen diff is empty — the defect "
        "is invisible on this route: a tester who follows the brief sees what the clean build "
        "shows. Fault before the state it corrupts, or give the route a step that reads that "
        "state back"]


def test_a_single_trial_derive_is_refused_too():
    row = dj.judge_case(_design(), _trials([PRE_FIX_SCREENS], [PRE_FIX_SCREENS]))
    assert row["agrees"] is False and len(row["problems"]) == 1
    assert "invisible on this route" in row["problems"][0] and "on trial(s)" not in row["problems"][0]


def test_a_route_that_reads_the_completion_back_agrees():
    """The re-authored shape: the route opens "Taxes" again, and the editor's own toggle
    says whether the completion was recorded — `Mark incomplete` on a clean build,
    `Mark completed` still on a seeded one that died before writing the row."""
    clean = PRE_FIX_SCREENS + [EDITOR_DONE]
    seeded = PRE_FIX_SCREENS + [EDITOR_OPEN]
    row = dj.judge_case(_design(), _trials([clean] * 3, [seeded] * 3))
    assert row["problems"] == [] and row["agrees"] is True
    assert row["diff"] == [{"step": 9, "added": ["Mark completed"], "removed": ["Mark incomplete"]}]


def test_a_death_that_is_invisible_on_one_trial_names_that_trial():
    """All-or-nothing, like a side marker: on trial 2 a tester would have seen nothing."""
    clean = PRE_FIX_SCREENS + [EDITOR_DONE]
    seeded_visible = PRE_FIX_SCREENS + [EDITOR_OPEN]
    row = dj.judge_case(_design(), _trials([clean] * 3, [seeded_visible, clean, seeded_visible]))
    assert row["agrees"] is False
    assert len(row["problems"]) == 1
    assert "diff is empty on trial(s) [2] of 3" in row["problems"][0]
    # The recorded row is trial 1's, which DID differ.
    assert row["diff"] != []


def test_a_state_failure_with_the_app_alive_and_an_empty_diff_is_not_refused():
    """contacts-phone / contacts-favorite: the seeded arm FAILS its oracle with the app
    alive and the route never visits the screen that shows it. That is a legitimate
    empty diff — the brief sends the tester to the verification screen."""
    case = {**PRE_FIX_CASE, "check": {**PRE_FIX_CASE["check"],
                                      "expect": {k: v for k, v in PRE_FIX_CASE["check"]["expect"].items()
                                                 if k != "crash"}}}
    design = _design(case)
    assert design["death"] is None
    row = dj.judge_case(design, _trials([PRE_FIX_SCREENS] * 3, [PRE_FIX_SCREENS] * 3,
                                        seeded_outcome=VIOLATED))
    assert row["problems"] == [] and row["agrees"] is True


def test_an_ungated_death_is_judged_by_what_happened_not_by_the_check():
    """The check need not name the death: a seeded arm that DIED with nothing to show for
    it is invisible whether or not the case carries a `crash:` gate."""
    case = {**PRE_FIX_CASE, "check": {**PRE_FIX_CASE["check"],
                                      "expect": {k: v for k, v in PRE_FIX_CASE["check"]["expect"].items()
                                                 if k != "crash"}}}
    row = dj.judge_case(_design(case), _trials([PRE_FIX_SCREENS], [PRE_FIX_SCREENS]))
    assert row["agrees"] is False and "invisible on this route" in row["problems"][0]


def test_a_clean_arm_that_fails_is_charged_once():
    """A clean arm that does not hold has no diff worth judging — that is its own
    problem already, and the invisibility check stays out of it."""
    row = dj.judge_case(_design(), _trials([PRE_FIX_SCREENS], [PRE_FIX_SCREENS],
                                           clean_outcome=VIOLATED))
    assert len(row["problems"]) == 1
    assert row["problems"][0].startswith("clean version does not pass its oracle")


def test_an_unstable_seeded_arm_is_charged_for_the_flip_alone():
    trials = _trials([PRE_FIX_SCREENS] * 2, [PRE_FIX_SCREENS] * 2)
    trials["seeded"][1] = _pair(HOLDS, PRE_FIX_SCREENS)
    row = dj.judge_case(_design(), trials)
    assert len(row["problems"]) == 1 and "seeded version unstable" in row["problems"][0]


# ── through derive_app: the refusal is printed and fails the run ──────────────

def test_derive_app_prints_the_refusal_under_the_case(monkeypatch, capsys):
    suite = {"app": {"id": "fossify-calendar", "package": "org.fossify.calendar.debug"},
             "bugs": [{"id": "task-complete-crash"}], "exploration": {}, "shared_storage": None}
    doc = {"app": "fossify-calendar",
           "defects": [{"id": "task-complete-crash", "kind": "functional", "class": "crash",
                        "tier": "L4", "symptoms": ["crash"]}],
           "test_cases": [PRE_FIX_CASE]}

    async def fake_stage(serial, suite_, tmp):
        return None, [], None

    async def fake_one_pass(serial, bundle, claim, flags, *a, **k):
        return (_pair(CRASHED, PRE_FIX_SCREENS, CRASH_DETAIL) if flags
                else _pair(HOLDS, PRE_FIX_SCREENS))

    monkeypatch.setattr(dj, "stage", fake_stage)
    monkeypatch.setattr(dj, "one_pass", fake_one_pass)
    monkeypatch.setattr(dj, "load_suite", lambda p: suite)
    monkeypatch.setattr(dj.journey, "load_cases", lambda a: doc)
    out = asyncio.run(dj.derive_app("fossify-calendar", "fake-serial", None, Path("/nonexistent"),
                                    repeat=3))
    assert out["cal-complete-task"]["agrees"] is False
    printed = capsys.readouterr().out
    assert "=> DISAGREE: clean holds · seeded FAIL" in printed
    assert "! seeded version dies (crashed) but its clean/seeded screen diff is empty" in printed

