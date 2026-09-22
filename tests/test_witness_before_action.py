"""A screen witness readable BEFORE the action under test is refused (QUA-2740).

Completion is credited when the verdict is right and every `evidence:` string appears in
text the device answered with — matched over the WHOLE episode, in any order
(`journey._witness`). So a witness that is already on screen before the step the case
measures is a free point: read the screen once, report the verdict the clean build was
always going to give, stop. Three cases shipped that way, and
`docs/journey-oracle-audit.md` recorded them in prose only.

`derive_journey.witness_credited_early` names the shape — every witness string visible on
a CLEAN step before `action_step(route)` — and `judge_witness` refuses it unless the case
carries `witness_before_action: <ticket>`, the in-data record of a weakness whose route
offers no post-action string. A marker on a case that is NOT credited early is refused
too, so the exemption cannot outlive the weakness.

Nothing here touches a device: every trial is a scripted (ReplayResult, screens) pair, and
the corpus tests read the committed YAML and truth.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from qualgentbench import corpus, journey, truth

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_journey.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_journey", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dj = _load()
rp = dj.rp
R = rp.ReplayResult
HOLDS, VIOLATED = rp.HOLDS, rp.VIOLATED

# ── the corpus's own shape: orgzly-open-note-from-notebook ────────────────────
# Route: launch · tap the notebook · tap the note · wait. The action is step 3.
# Screens as the 2026-09-18 derive recorded them, trimmed to what this test reads.

NOTEBOOKS = ["Notebooks", "Getting Started with Orgzly", "Contains 33 notes"]
NOTE_LIST = ["Getting Started with Orgzly", "Notes", "Click on the note to open it",
             "Click and hold the note to select it", "You can select multiple notes."]
EDITOR = ["Done", "More options", "Getting Started with Orgzly  •  Notes",
          "Click on the note to open it", "Tags", "State", "Content"]
WRONG_EDITOR = ["Done", "More options", "Getting Started with Orgzly  •  Notes",
                "Click and hold the note to select it", "Tags", "State",
                "You can select multiple notes."]

CLEAN_SCREENS = [NOTEBOOKS, NOTE_LIST, EDITOR, EDITOR]
SEEDED_SCREENS = [NOTEBOOKS, NOTE_LIST, WRONG_EDITOR, WRONG_EDITOR]

ROUTE = ["launch", {"tap": "Getting Started with Orgzly"},
         {"tap": "Click on the note to open it"}, "wait"]
TITLE = "Click on the note to open it"
BREADCRUMB = "Getting Started with Orgzly  •  Notes"

DEFECTS = {"note-tap-opens-next-note": {"kind": "functional", "tier": "L3", "marker": "",
                                        "symptoms": ["wrong note", "navigation"]}}
CASE = {
    "id": "orgzly-open-note-from-notebook",
    "check": {"steps": ROUTE, "expect": {"present": TITLE}},
    "bugs": ["note-tap-opens-next-note"],
}


def _pair(outcome: str, screens: list[list[str]], detail: str = ""):
    return R(outcome, detail, len(screens)), [list(s) for s in screens]


def _trials(clean=None, seeded=None, clean_outcome: str = HOLDS) -> dict:
    return {"clean": [_pair(clean_outcome, clean or CLEAN_SCREENS)],
            "seeded": [_pair(VIOLATED, seeded or SEEDED_SCREENS)]}


def _design(case: dict = CASE) -> dict:
    return journey.case_design(case, DEFECTS)


def _judge(witness: list[str], *, exempt: str = "", trials=None, route=ROUTE):
    problems: list[str] = []
    out = dj.judge_witness(witness, trials or _trials(), [], problems,
                           action=dj.action_step(truth._steps(route)), exempt=exempt)
    return out, problems


# ── the predicates ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route, action", [
    (ROUTE, 3),                                                   # trailing wait skipped
    (["launch", {"tap": "A"}, {"tap": "B"}], 3),                  # no wait: the last tap
    (["launch", {"type": "x"}, {"rotate": "landscape"}, "wait"], 3),   # the lifecycle step
    (["launch", "wait", "wait"], 1),                              # only the launch is real
    (["wait"], 0),                                                # nothing to measure
])
def test_action_step_is_the_last_step_that_is_not_a_wait(route, action):
    assert dj.action_step(truth._steps(route)) == action


@pytest.mark.parametrize("witness, action, early, credited", [
    # The shipped orgzly witness: the title is the tapped row's own label (step 2).
    ({TITLE: {"clean": [2, 3, 4]}}, 3, {TITLE: [2]}, True),
    # The fix: one string only the destination shows makes the SET unearnable early.
    ({TITLE: {"clean": [2, 3, 4]}, BREADCRUMB: {"clean": [3, 4]}}, 3, {TITLE: [2]}, False),
    # Nothing before the action at all.
    ({BREADCRUMB: {"clean": [3, 4]}}, 3, {}, False),
    # cal-switch-back-to-list: the witness is on the list the save already showed.
    ({"Standup": {"clean": [5, 6, 10]}}, 10, {"Standup": [5, 6]}, True),
    # A route whose first step is the action has no "before".
    ({"Standup": {"clean": [1]}}, 1, {}, False),
    ({}, 3, {}, False),
])
def test_witness_credited_early_needs_every_string_before_the_action(witness, action, early, credited):
    assert dj.witness_before_action(witness, action) == early
    assert dj.witness_credited_early(witness, action) is credited


def test_the_seeded_arm_is_not_consulted():
    """A seeded arm never reads a witness (an expected-FAIL episode is scored on the
    verdict and the blocking bug), so pre-action sightings there are not the weakness."""
    assert dj.witness_credited_early({TITLE: {"clean": [3], "seeded": [1, 2]}}, 3) is False


# ── judge_witness ─────────────────────────────────────────────────────────────

def test_the_shipped_witness_is_refused():
    """The mutation this ticket exists for: the witness as committed at 72478f5."""
    out, problems = _judge([TITLE])
    assert out[TITLE] == {"clean": [2, 3, 4], "seeded": [2]}
    assert len(problems) == 1
    assert problems[0] == (
        f"witness ['{TITLE}'] is already complete on the clean route before the action "
        f"this case tests (step 3): {{'{TITLE}': [2]}} — completion can be credited to an "
        f"agent that never performed it. Witness a string only the post-action screen "
        f"carries, or mark the case `witness_before_action: <ticket>` when the route has none")


def test_the_breadcrumb_pair_is_accepted():
    """The fix: the editor's own breadcrumb is on clean steps 3-4 and nowhere earlier."""
    out, problems = _judge([TITLE, BREADCRUMB])
    assert out[BREADCRUMB] == {"clean": [3, 4], "seeded": [3, 4]}
    assert problems == []


def test_the_exemption_is_the_only_way_past_it():
    out, problems = _judge([TITLE], exempt="QUA-2768")
    assert problems == []
    assert out[TITLE]["clean"] == [2, 3, 4]      # still recorded, just not refused


def test_a_stale_exemption_is_refused():
    """A marker on a case whose witness is no longer credited early: the key outlived
    the weakness it records, and the next reader would trust it."""
    _, problems = _judge([TITLE, BREADCRUMB], exempt="QUA-2768")
    assert problems == [
        f"`witness_before_action: QUA-2768` is stale — witness "
        f"['{TITLE}', '{BREADCRUMB}'] is no longer complete before step 3; drop the key"]


def test_a_clean_arm_that_does_not_hold_is_not_judged_on_its_witness():
    """Same rule the final-screen check already runs under: a clean arm that failed has
    a bigger problem, and its screens may stop short of the action."""
    _, problems = _judge([TITLE], trials=_trials(clean_outcome=VIOLATED))
    assert problems == []


def test_the_final_screen_check_still_fires():
    """The pre-existing witness rules are unchanged by the new one."""
    _, problems = _judge(["Nowhere on this route"])
    assert len(problems) == 1 and "not on the clean route's final screen" in problems[0]


# ── judge_case / derive_app ───────────────────────────────────────────────────

def test_judge_case_refuses_the_whole_case():
    row = dj.judge_case(_design(), _trials(), witness=[TITLE],
                        action=dj.action_step(truth._steps(ROUTE)))
    assert row["measured"] == "FAIL" and row["expected"] == "FAIL"      # verdict unmoved
    assert row["agrees"] is False and len(row["problems"]) == 1
    assert "before the action this case tests (step 3)" in row["problems"][0]


def test_judge_case_agrees_with_the_fixed_witness():
    row = dj.judge_case(_design(), _trials(), witness=[TITLE, BREADCRUMB],
                        action=dj.action_step(truth._steps(ROUTE)))
    assert row["problems"] == [] and row["agrees"] is True
    assert list(row["witness"]) == [TITLE, BREADCRUMB]


def test_derive_app_reads_the_key_off_the_case_and_prints_the_refusal(monkeypatch, capsys):
    """End to end through the derive: the route, the witness and the exemption all come
    off the case, so a corpus file is the only place the exception list lives."""
    suite = {"app": {"id": "orgzly", "package": "com.orgzly"},
             "bugs": [{"id": "note-tap-opens-next-note"}], "exploration": {},
             "shared_storage": None}
    case = {**CASE, "name": "Open a note from its notebook", "evidence": [TITLE]}
    doc = {"app": "orgzly",
           "defects": [{"id": "note-tap-opens-next-note", "kind": "functional",
                        "class": "navigation", "tier": "L3", "symptoms": ["wrong note"]}],
           "test_cases": [case]}

    async def fake_stage(serial, suite_, tmp):
        return None, [], None

    async def fake_one_pass(serial, bundle, claim, flags, *a, **k):
        return _pair(VIOLATED, SEEDED_SCREENS) if flags else _pair(HOLDS, CLEAN_SCREENS)

    monkeypatch.setattr(dj, "stage", fake_stage)
    monkeypatch.setattr(dj, "one_pass", fake_one_pass)
    monkeypatch.setattr(dj, "load_suite", lambda p: suite)
    monkeypatch.setattr(dj.journey, "load_cases", lambda a: doc)

    def _derive():
        return asyncio.run(dj.derive_app("orgzly", "fake-serial", None, Path("/nonexistent"),
                                         repeat=1))

    out = _derive()
    assert out[CASE["id"]]["agrees"] is False
    assert "! witness" in capsys.readouterr().out

    doc["test_cases"] = [{**case, dj.WITNESS_EXEMPT_KEY: "QUA-2768"}]
    assert _derive()[CASE["id"]]["agrees"] is True


# ── the committed corpus ──────────────────────────────────────────────────────

# The weakness as measured on 2026-09-22, and the whole exception list. A sixth entry
# here means a new weak witness entered the corpus; one fewer means a case was fixed and
# its `witness_before_action:` key should have gone with it.
KNOWN_WEAK = {
    "cal-switch-back-to-list",            # the list the save already showed
    "orgzly-new-note-survives-rotation",  # the title, from the moment it is typed
    "orgzly-create-and-search",           # the typed note title, again in the search box
    "medtimer-review-aspirin",            # the reminder time, on the Overview at launch
    "medtimer-analysis-tabular-view",     # 'Ibuprofen', on the Overview at launch
}


def _public_cases():
    """(app, case, committed truth row) for every public journey case, in corpus order."""
    for app_id in sorted(corpus.public_apps()):
        doc = journey.load_cases(app_id) or {}
        rows = journey.load_truth(app_id) or {}
        for case in doc.get("test_cases") or []:
            yield app_id, case, rows.get(case["id"]) or {}


def test_every_case_carrying_the_key_is_one_the_committed_truth_flags():
    """The marker is not a comment: it has to name a weakness the corpus can measure."""
    stale = []
    for app_id, case, row in _public_cases():
        if not case.get(dj.WITNESS_EXEMPT_KEY):
            continue
        action = dj.action_step(truth._steps((case.get("check") or {}).get("steps")))
        if not dj.witness_credited_early(row.get("witness") or {}, action):
            stale.append(f"{app_id}:{case['id']}")
    assert stale == []


def test_no_unmarked_case_credits_its_witness_before_the_action():
    """The gate, device-free, over the corpus as committed. A case that fails this has
    to tighten its witness or record itself with `witness_before_action:`."""
    unmarked = []
    for app_id, case, row in _public_cases():
        action = dj.action_step(truth._steps((case.get("check") or {}).get("steps")))
        if (dj.witness_credited_early(row.get("witness") or {}, action)
                and not case.get(dj.WITNESS_EXEMPT_KEY)):
            unmarked.append(f"{app_id}:{case['id']}")
    assert unmarked == []


def test_the_exception_list_is_exactly_the_five_known_cases():
    marked = {case["id"] for _, case, _ in _public_cases() if case.get(dj.WITNESS_EXEMPT_KEY)}
    assert marked == KNOWN_WEAK


def test_every_marker_names_the_ticket_that_removes_it():
    for _, case, _ in _public_cases():
        mark = case.get(dj.WITNESS_EXEMPT_KEY)
        if mark is not None:
            assert str(mark) == "QUA-2768", f"{case['id']}: {mark!r}"


def test_the_committed_truth_witnesses_are_the_cases_own_evidence():
    """The corpus test above can only judge a row derived from the case as it stands —
    so a case whose `evidence:` moved must be re-derived before the gate means anything."""
    stale = []
    for app_id, case, row in _public_cases():
        declared = [str(e) for e in (case.get("evidence") or []) if str(e).strip()]
        if declared != list(row.get("witness") or {}):
            stale.append(f"{app_id}:{case['id']} — re-derive: evidence {declared}, "
                         f"truth {list(row.get('witness') or {})}")
    assert stale == []
