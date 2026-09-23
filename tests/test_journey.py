"""Journey mode: one app + one test case + one version (clean | seeded) per episode.
Two numbers, never blended: completion (verified on the device) and bug finding."""

from __future__ import annotations

import pytest

from qualgentbench import bugs, journey
from pathlib import Path

from qualgentbench.task import BenchmarkTask

from test_bugs import _call, _obs, _transcript


def _app(app_id: str):
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == app_id)
    return journey.journey_tasks(suite)


DEFECTS = {
    "avg-bug": {"kind": "display", "tier": "L2", "marker": "Avg:", "symptoms": ["average", "avg"]},
    "age-bug": {"kind": "display", "tier": "L3", "marker": "Age:", "symptoms": ["age"]},
    "delete-bug": {"kind": "functional", "tier": "L1", "marker": "", "symptoms": ["still", "not deleted"]},
}


def _spec(version="seeded", bugs=None, oracle=None, oracle_result=None):
    bugs = bugs or []
    blocking = next((b for b in bugs if DEFECTS[b]["kind"] == "functional"), None)
    side = [{"bug": b, "marker": DEFECTS[b]["marker"], "texts": [], "visible_steps": []}
            for b in bugs if DEFECTS[b]["kind"] != "functional"]
    if bugs and "avg-bug" in bugs:
        side[[s["bug"] for s in side].index("avg-bug")]["texts"] = ["Avg: 76 kg", "Avg: 79 kg"]
    active = bugs if version == "seeded" else []
    spec = {
        "mode": "journey", "app_id": "app", "case_id": "case-1", "version": version, "name": "Case",
        "steps": ["Open the list", "Read the total"], "expected_outcome": "The total matches.",
        "active_bugs": active, "step_budget": 30,
        "expected": "FAIL" if (blocking and version == "seeded") else "PASS",
        "blocking": blocking if version == "seeded" else None,
        "blocking_texts": ["85 kg"] if blocking else [],
        "side": side if version == "seeded" else [],
        "defects": DEFECTS,
        "oracle": oracle or {"mode": "present", "expect": {"present": "Total: 4 items"}, "evidence": ["Total: 4 items"]},
        "truth_agrees": True, "tooling": "mcp",
    }
    if oracle_result is not None:
        spec["oracle_result"] = oracle_result
    return spec


def _task(spec):
    return BenchmarkTask(id="case-1~" + spec["version"], name="Case", instruction="", app_file_id="",
                         app_name="App", platform="android", bundle_id="com.example", bug_spec=spec)


def _write(verdict, bugs_yaml=""):
    body = f"verdict: {verdict}\nbugs:{bugs_yaml or ' []'}\n"
    return _call("Write", {"file_path": "/w/findings.yaml", "content": body}, "ok")


def _bug(step, observed, description):
    return f'\n  - step: {step}\n    observed: "{observed}"\n    description: "{description}"'


# ── loading: two versions per case, everything derived from `bugs:` ───────────

def test_every_case_has_a_clean_version_and_seeded_only_with_bugs():
    tasks = _app("medtimer")
    versions = {}
    for t in tasks:
        versions.setdefault(t.bug_spec["case_id"], []).append(t.bug_spec["version"])
    assert all(v == ["clean", "seeded"] for v in versions.values()), versions
    add = next(t for t in tasks if t.id == "medtimer-add-medicine~seeded")
    assert add.bug_spec["expected"] == "PASS" and add.bug_spec["active_bugs"] == ["stock-left-display-low"]
    assert add.bug_spec["oracle"]["mode"] == "db"
    crash = next(t for t in tasks if t.id == "medtimer-add-medicine-back-to-list~seeded")
    assert crash.bug_spec["expected"] == "FAIL"
    assert crash.bug_spec["blocking"] == "medicine-list-empty-reminders-crash"
    assert crash.bug_spec["active_bugs"] == ["medicine-list-empty-reminders-crash"]
    clean = next(t for t in tasks if t.id == "medtimer-add-medicine-back-to-list~clean")
    assert clean.bug_spec["expected"] == "PASS" and clean.bug_spec["active_bugs"] == []
    review = next(t for t in tasks if t.id == "medtimer-review-aspirin~seeded")
    assert review.bug_spec["expected"] == "PASS"
    assert [s["bug"] for s in review.bug_spec["side"]] == ["reminder-time-display-shifted",
                                                          "stock-left-display-low"]
    assert all(s["texts"] for s in review.bug_spec["side"])           # measured, not authored
    # The corpus's `evidence:` for this case is its screen witness (Aspirin's own
    # reminder time, shown on both arms); only the Medicine-tab row is shifted.
    assert review.bug_spec["oracle"] == {"mode": "present", "expect": {"present": "8:00 AM"},
                                         "evidence": ["8:00 AM"], "witness": ["8:00 AM"]}


def test_case_design_refuses_two_functional_bugs():
    defects = {"a": {"kind": "functional", "marker": "", "symptoms": []},
               "b": {"kind": "functional", "marker": "", "symptoms": []}}
    with pytest.raises(ValueError):
        journey.case_design({"id": "x", "bugs": ["a", "b"]}, defects)
    with pytest.raises(ValueError):
        journey.case_design({"id": "x", "bugs": ["nope"]}, defects)


def test_per_case_marker_overrides_the_defect_marker():
    defects = {"d": {"kind": "display", "marker": "Diff", "symptoms": []}}
    design = journey.case_design({"id": "x", "bugs": [{"id": "d", "marker": "-30.00"}]}, defects)
    assert design["side"] == [{"bug": "d", "marker": "-30.00"}] and design["expected"] == "PASS"


def test_brief_is_identical_across_versions_and_names_no_bug():
    tasks = _app("medtimer")
    clean = next(t for t in tasks if t.id == "medtimer-add-medicine-back-to-list~clean")
    seeded = next(t for t in tasks if t.id == "medtimer-add-medicine-back-to-list~seeded")
    a, b = journey.brief(clean, "e", "raw"), journey.brief(seeded, "e", "raw")
    assert a == b
    assert "QGB-CANARY" not in a and "empty-reminders-crash" not in a and "Preconditions" not in a
    assert "1. Open the Medicine tab." in a and "Expected outcome:" in a


def test_task_id_round_trip():
    assert journey.split_task_id("tasks-delete~clean") == ("tasks-delete", "clean")
    assert journey.split_task_id("tasks-delete") == ("tasks-delete", "seeded")


# ── report parsing ─────────────────────────────────────────────────────────────

def test_parse_report_shapes():
    r = journey.parse_report("verdict: pass\nbugs: []\n")
    assert r.verdict == "pass" and r.bugs == [] and not r.errors
    r = journey.parse_report('verdict: FAIL\nbugs:\n  - step: 2\n    observed: "Total: 3"\n    description: "wrong"\n')
    assert r.verdict == "fail" and r.bugs[0].step == 2 and r.bugs[0].observed == "Total: 3"
    assert journey.parse_report("verdict: maybe\n").verdict is None
    assert journey.parse_report("- just: a list\n").errors


# ── completion: verified on the device, then the verdict ───────────────────────

def test_clean_version_completes_on_oracle_plus_pass_and_no_reports():
    t = _task(_spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []},
                 oracle_result="holds"))
    v = journey.journey_verdict(_transcript(_obs("Total: 4 items"), _write("pass")), "m", t)
    assert v.metrics["completed"] and v.metrics["false_reports"] == 0 and v.passed
    assert v.metrics["bugs_present"] == [] and v.metrics["recall"] is None


def test_clean_version_any_report_is_a_false_report():
    t = _task(_spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []},
                 oracle_result="holds"))
    v = journey.journey_verdict(_transcript(
        _obs("Total: 4 items"), _write("pass", _bug(2, "Total: 4 items", "looks odd"))), "m", t)
    assert v.metrics["completed"] is True             # the steps were executed and the verdict is right
    assert v.metrics["false_reports"] == 1 and not v.passed
    assert "clean build" in (v.failure_reason or "")


def test_clean_version_wrong_verdict_is_not_completed():
    t = _task(_spec("clean"))
    v = journey.journey_verdict(_transcript(_obs("Total: 4 items"), _write("fail")), "m", t)
    assert v.metrics["completed"] is False and "reported fail" in v.failure_reason


def test_screen_text_oracle_leaves_completion_unscored():
    """A `present:`/`absent:` outcome is proven by the agent's own device TEXT, which an
    agent that reads the screen from screenshots never emits — so completion is left
    UNSCORED rather than scored wrong. Bug finding is unaffected. Replaces the older
    contract, where the same episode was scored a non-completion."""
    t = _task(_spec("clean"))
    v = journey.journey_verdict(_transcript(_obs("Some other screen"), _write("pass")), "m", t)
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False
    assert "not scored" in v.failure_reason
    assert v.passed and v.metrics["false_reports"] == 0     # bug finding still stands


def test_a_db_oracle_still_refuses_a_pass_that_never_reached_the_outcome():
    """The anti-fabrication guard survives wherever the harness can check it itself."""
    t = _task(_spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"},
                                     "evidence": []}, oracle_result="violated"))
    v = journey.journey_verdict(_transcript(_obs("Some other screen"), _write("pass")), "m", t)
    assert v.metrics["completed"] is False and v.metrics["completion_scored"] is True


def test_unscored_completion_still_scores_a_wrong_verdict_and_a_dead_episode():
    """Only the device half is dropped: everything checkable without the oracle stands."""
    wrong = journey.journey_verdict(
        _transcript(_obs("Some other screen"), _write("fail")), "m", _task(_spec("clean")))
    assert wrong.metrics["completed"] is False and "reported fail" in wrong.failure_reason
    dead = journey.journey_verdict(_transcript(_write("pass")), "m", _task(_spec("clean")))
    assert dead.metrics["completed"] is False and "no device evidence" in dead.failure_reason


def test_db_oracle_result_from_the_runner_decides_completion():
    ok = _spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []},
               oracle_result="holds")
    v = journey.journey_verdict(_transcript(_obs("anything"), _write("pass")), "m", _task(ok))
    assert v.metrics["completed"] is True and v.metrics["oracle"]["ok"] is True
    bad = _spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []},
                oracle_result="violated")
    v = journey.journey_verdict(_transcript(_obs("anything"), _write("pass")), "m", _task(bad))
    assert v.metrics["completed"] is False and "not reached" in v.failure_reason
    # Not evaluated never counts against the agent — and never FOR it either: an
    # oracle that produced no answer leaves completion unscored, not completed.
    none = _spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []})
    v = journey.journey_verdict(_transcript(_obs("anything"), _write("pass")), "m", _task(none))
    assert v.metrics["completed"] is None and v.metrics["oracle"]["ok"] is None
    assert v.metrics["completion_scored"] is False


def test_an_unevaluated_db_oracle_leaves_completion_unscored():
    """19 of 19 PASS-expected `db:` episodes in two real runs had oracle.ok = null (no
    on-device sqlite3) and were all scored completed — the published completion figure
    was the agent's own verdict. An oracle that did not run verifies nothing."""
    db = {"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []}
    for result in (None, "inconclusive"):
        t = _task(_spec("clean", oracle=db, oracle_result=result))
        v = journey.journey_verdict(_transcript(_obs("Total: 4 items"), _write("pass")), "m", t)
        assert v.metrics["completed"] is None, result
        assert v.metrics["completion_scored"] is False
        assert "completion not scored" in v.failure_reason and "not evaluated" in v.failure_reason
        assert v.metrics["completion_reason"]                 # the reason survives into result.json
        # Bug finding is untouched, and the episode leaves every completion denominator.
        assert v.passed and v.score == 1.0
        assert v.criteria["completed"] is False
        row = journey._row(("a", "m", "raw"), [v])
        assert row["completion"] is None and row["completion_unscored"] == 1
    # A VIOLATED oracle is still a scored non-completion — that guard is untouched.
    t = _task(_spec("clean", oracle=db, oracle_result="violated"))
    v = journey.journey_verdict(_transcript(_obs("Total: 4 items"), _write("pass")), "m", t)
    assert v.metrics["completed"] is False and v.metrics["completion_scored"] is True


def test_the_oracle_detail_reaches_result_json():
    """The runner's own query output/error is the only clue to a silent oracle failure;
    without it, diagnosing one took a live device."""
    spec = _spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"},
                                  "evidence": []}, oracle_result="inconclusive")
    spec["oracle_detail"] = "sqlite3: not found"
    v = journey.journey_verdict(_transcript(_obs("x"), _write("pass")), "m", _task(spec))
    assert v.metrics["oracle"]["detail"] == "sqlite3: not found"


def test_blocked_version_completes_on_fail_plus_the_blocking_bug():
    t = _task(_spec("seeded", ["delete-bug"]))
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"), _write("fail", _bug(2, "85 kg", "the entry is still listed after delete"))), "m", t)
    assert v.metrics["completed"] and v.metrics["blocking_named"] and v.passed
    assert v.metrics["bugs_found"] == ["delete-bug"] and v.metrics["recall"] == 1.0
    # Right verdict, wrong cause: not completed, and the cause is a false report.
    t = _task(_spec("seeded", ["delete-bug"]))
    v = journey.journey_verdict(_transcript(
        _obs("85 kg"), _write("fail", _bug(2, "Save", "the save button is grey"))), "m", t)
    assert not v.metrics["completed"] and v.metrics["false_reports"] == 1 and v.metrics["recall"] == 0.0
    # Pass on a blocked case: not completed.
    t = _task(_spec("seeded", ["delete-bug"]))
    v = journey.journey_verdict(_transcript(_obs("85 kg"), _write("pass")), "m", t)
    assert not v.metrics["completed"] and "reported pass" in v.failure_reason


def test_seeded_pass_version_scores_side_bugs():
    t = _task(_spec("seeded", ["avg-bug", "age-bug"], oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []},
                 oracle_result="holds"))
    v = journey.journey_verdict(_transcript(
        _obs("Total: 4 items Avg: 76 kg Age: 37"),
        _write("pass", _bug(2, "Avg: 76 kg", "should be 79"))), "m", t)
    assert v.metrics["completed"] and v.metrics["bugs_found"] == ["avg-bug"]
    assert v.metrics["bugs_missed"] == ["age-bug"] and v.metrics["recall"] == 0.5
    assert v.metrics["precision"] == 1.0 and v.metrics["f1"] == pytest.approx(2 / 3)
    assert not v.passed


def test_symptom_words_match_on_whole_words():
    t = _task(_spec("seeded", ["avg-bug"]))
    v = journey.journey_verdict(_transcript(
        _obs("Weight card"), _write("pass", _bug(2, "76", "the average is wrong"))), "m", t)
    assert v.metrics["bugs_found"] == ["avg-bug"]
    t = _task(_spec("seeded", ["age-bug"]))
    v = journey.journey_verdict(_transcript(
        _obs("Users"), _write("pass", _bug(2, "x", "the average is off"))), "m", t)
    assert v.metrics["bugs_found"] == [] and v.metrics["false_reports"] == 1    # "age" ⊄ "average"


def test_a_bug_not_on_this_build_is_a_false_report():
    """Per-case activation: the app has an age bug, this case does not switch it on."""
    t = _task(_spec("seeded", ["avg-bug"]))
    v = journey.journey_verdict(_transcript(
        _obs("Age: 37"), _write("pass", _bug(1, "Age: 37", "the age is one year too high"))), "m", t)
    assert v.metrics["false_reports"] == 1 and v.metrics["bugs_found"] == []


def test_budget_exhaustion_is_not_completed_whatever_was_written():
    spec = _spec("clean"); spec["truncated"] = True; spec["hook_steps"] = 31
    v = journey.journey_verdict(_transcript(_obs("Total: 4 items"), _write("pass")), "m", _task(spec))
    assert v.metrics["completed"] is False and "budget" in v.failure_reason


def test_no_device_evidence_is_not_completed():
    t = _task(_spec("clean"))
    v = journey.journey_verdict(_transcript(_call("Bash", {"command": "ls"}, "x")) + "RESULT: verdict=pass\n", "m", t)
    assert v.metrics["reported_verdict"] == "pass" and v.metrics["completed"] is False


# ── the matcher: short strings are not evidence (real corpus) ─────────────────

def _real(app_id: str, tid: str):
    return next(t for t in _app(app_id) if t.id == tid)


def _match(tid: str, app_id: str, observed: str = "", description: str = "", screen: str = "",
           expected: str = "", seen: bool = False):
    """Run one synthetic report through the matcher against the REAL spec for `tid`.

    `seen` is the report's `grounded` flag — did the DEVICE answer with this quote? It
    defaults False, which is the guesser's position: a string typed into a findings file
    by an agent that never read it off a screen. `journey_verdict` computes it from the
    transcript; only the `echo_texts` route consults it (QUA-2717)."""
    report = journey.BugReport(step=1, screen=screen, observed=observed, expected=expected,
                               description=description, grounded=seen)
    return journey.match_report(report, _real(app_id, tid).bug_spec)


# Probes that all earned credit from the live matcher before 2026-09-10: a
# one-character marker or derived blocking text matched as a bare SUBSTRING, and a
# single generic symptom word matched honest prose about an unrelated problem. (One of
# them, the ankidroid count row, now stands in for a pruned case; see its comment.)
# Two more rows probed the one-character MARKERS "2" (`deck-new-count-low`) and "1"
# (`subtask-chip-low`) inside unrelated text; QUA-2783 retired both defects from the
# journey corpus, so no real case can carry that probe any more. The shape is still
# guarded twice: `lint_journey_cases.py`'s `quotable` rule refuses a display marker under
# the evidence floor, and journey_adversary_check.py's `short-spray` quotes "1" and "0".
@pytest.mark.parametrize("tid,app_id,report,was", [
    # derived blocking text "A" (a contacts section index) — matched EVERY report, which
    # bought a fabricated report recall AND completion on a blocked case
    ("contacts-delete~seeded", "fossify-contacts", {"observed": "anything at all"},
     "contact-delete-broken"),
    # a count with a bare digit off the route's own screen, on a blocked case whose
    # evidence is a screen diff. (The historical probe — derived blocking texts "1", "3",
    # "4" on anki-add-note-to-deck — lost its case when QUA-2725 pruned it; "3 cards
    # shown" is on BOTH arms of this route, so it is in no diff at all.)
    ("anki-open-card-from-browser~seeded", "ankidroid", {"observed": "3 cards shown"},
     "browser-card-opens-add-note"),
    # a display defect's prose credited for a screen value that is not the defect's
    ("medtimer-add-medicine~seeded", "medtimer",
     {"observed": "Aspirin (10 left)", "description": "the label is left) aligned"},
     "stock-left-display-low"),
    ("cal-create-event~seeded", "fossify-calendar",
     {"observed": "Standup", "description": "the event title looks wrong"},
     "event-editor-title-typo"),
])
def test_short_and_off_target_reports_earn_no_credit(tid, app_id, report, was):
    assert _match(tid, app_id, **report) is None, f"{tid}: still credited as {was}"


# Was a strict xfail on a CORPUS bug: tasksorg's `due-date-edit-lost` listed the bare word
# `lost` as a symptom, which matched honest prose about any lost thing, and the fix was
# to be made in data/test-cases/tasksorg.yaml. QUA-2730 pruned that defect from the
# journey corpus (docs/defect-classes.md §8), taking the word with it, and shrank its
# case (tasks-change-due-time) to a display bug. The probe now runs on the app's
# remaining blocked case, so a FUNCTIONAL defect is still what it must not identify.
# Re-pointed alone it passed whatever the matcher did — none of `subtasks-left-open`'s
# symptoms is in the sentence (QUA-2739 review) — so it now carries a POSITIVE control:
# one real single-word symptom in the same kind of prose does identify the defect, so
# the case is live and the matcher reads single words; `lost` is simply not one.
def test_a_generic_word_is_no_symptom_while_a_real_single_word_symptom_is():
    assert _match("tasks-complete-parent~seeded", "tasksorg",
                  description="the subtask was still unchecked") == "subtasks-left-open"
    assert _match("tasks-complete-parent~seeded", "tasksorg",
                  description="the task was lost in the list") is None


def test_real_markers_and_texts_still_match_on_token_boundaries():
    """The floor is 2 characters, so orgzly's `#B` priority marker is still evidence —
    and a measured text still matches when the agent quotes it."""
    assert _match("orgzly-create-priority-note~seeded", "orgzly",
                  observed="TODO  #B  Book flights") == "priority-letter-shifted"
    assert _match("medtimer-add-medicine~seeded", "medtimer",
                  observed="Aspirin (9 left, 2026-09-10)") == "stock-left-display-low"
    # `Alice` is the whole of contacts-delete's on-screen evidence AND the contact the
    # brief tells the agent to delete, so it is echoable: on screen, and writable with
    # the app never started. It still earns the bug — the report is only asked to show
    # the device answered with it (QUA-2717). (This probe read tasksorg's tasks-delete
    # and `Call dentist` until QUA-2730 pruned that case; contacts-delete is the same
    # shape: a delete that leaves the brief's own noun on the list.)
    assert _match("contacts-delete~seeded", "fossify-contacts",
                  observed="Alice", seen=True) == "contact-delete-broken"
    assert _match("contacts-delete~seeded", "fossify-contacts", observed="Alice") is None
    # Token boundaries, not substrings: the marker inside a longer number is not a hit.
    assert _match("orgzly-create-priority-note~seeded", "orgzly", observed="#BC  Book flights") is None


def test_derived_blocking_texts_drop_what_cannot_be_evidence():
    """`unclaimed_diff` is every string that differed between the two screens, junk
    included. Filtering happens where the evidence is built, not at match time."""
    contacts = _real("fossify-contacts", "contacts-delete~seeded").bug_spec
    assert "A" not in contacts["blocking_texts"]
    # `Alice` is the name the route types and the brief spells out: echoable.
    assert contacts["echo_texts"] == ["Alice"]
    # `No contacts found` is on the CLEAN arm's screen — the delete that worked. The
    # seeded agent never saw it, so it is what the report EXPECTED, not what it observed.
    assert "No contacts found" in contacts["absence_texts"]
    # `tres` is the row the route taps and `three` the value the brief names: both are on
    # the CLEAN arm's editor only, and both are writable blind — never evidence.
    anki = _real("ankidroid", "anki-open-card-from-browser~seeded").bug_spec
    assert not {"tres", "three"} & set(anki["blocking_texts"] + anki["absence_texts"])
    # The note the seeded build opens instead of the tapped one: a real screen string that
    # neither the brief nor the route hands the agent, so it survives the filter.
    orgzly = _real("orgzly", "orgzly-open-note-from-notebook~seeded").bug_spec["blocking_texts"]
    assert "Click and hold the note to select it" in orgzly
    # tasksorg's row read tasks-delete until QUA-2730 pruned it. Its stand-in is a DEATH
    # case: the seeded arm's diff there is the launcher behind the dead app, and none of
    # it may survive into the evidence lists (crash evidence replaces the diff).
    assert all(len(t.strip()) >= 2
               for app, tid in [("fossify-contacts", "contacts-delete~seeded"),
                                ("tasksorg", "tasks-complete-repeating~seeded")]
               for key in ("blocking_texts", "echo_texts", "absence_texts")
               for t in _real(app, tid).bug_spec[key])


def test_every_text_the_route_names_is_in_the_echo_haystack():
    """A `row:` label and a `long_press:` anchor are on screen by the case's construction
    exactly as a `tap:` anchor is, and `append:` types text as `type:` does — so none of
    them is a sighting until the device answers with it. Before QUA-2739 only `type`
    and `tap` values were in the haystack. On the corpus as it stands the wider haystack
    moves no string between the evidence lists: every journey task's spec came out
    byte-identical, so no score moves."""
    case = {"name": "Answer a reminder", "steps": ["Open the Overview."],
            "expected_outcome": "The dose is recorded.",
            "check": {"steps": ["launch", {"tap": "Reminded", "row": "Naproxen (7)"},
                                {"long_press": "Weekly review"}, {"append": "tomorrow"},
                                {"press": "back"}, {"rotate": "landscape"}]}}
    hay = journey.echo_haystack(case)
    for text in ("Reminded", "Naproxen (7)", "Weekly review", "tomorrow"):
        assert journey._echoable(text, hay), text
    # A keyword value is not screen text.
    assert not journey._echoable("landscape", hay)


# ── QUA-2717: evidence a report can produce without observing the defect ──────
#
# Three routes in, every one of them measured on the real corpus through the real
# `match_report`, and every one of them earning the BLOCKING bug — which on a blocked
# case is recall AND completion, since such a case completes on "fail + the blocking
# bug named". None was catchable by `journey_adversary_check`, whose roster wrote no
# string of any of these shapes, so five exemplars shipped over a green gate.


@pytest.mark.parametrize("tid,app_id,typed", [
    # The title the route types, on the case where the defect is a WRONG SCREEN: the
    # year view never went away. `Standup` is on the clean arm's final list and not on
    # the seeded one, so it is not a sighting of anything — it is the opposite.
    ("cal-switch-back-to-list~seeded", "fossify-calendar", "Standup"),
    # Death cases: the route ends where the app ended, so the diff is the clean arm's
    # screen and the launcher behind the corpse. Already closed by QUA-2710; pinned
    # here so the three routes read as one story.
    ("cal-search-event~seeded", "fossify-calendar", "Dentist"),
    ("cal-search-event~seeded", "fossify-calendar", "Dent"),
    ("medtimer-add-medicine-back-to-list~seeded", "medtimer", "Lisinopril"),
])
def test_a_string_the_route_typed_is_never_a_sighting_of_the_defect(tid, app_id, typed):
    """Not evidence however the report dresses it up, and not evidence even from an
    agent that really did drive the device: it typed this string itself."""
    for seen in (False, True):
        assert _match(tid, app_id, observed=typed, seen=seen) is None, \
            f"{tid}: {typed!r} bought the blocking bug (grounded={seen})"
        assert _match(tid, app_id, expected=typed, seen=seen) is None, \
            f"{tid}: {typed!r} bought the blocking bug through `expected`"


@pytest.mark.parametrize("tid,app_id,echo", [
    # Here the brief noun IS the evidence: the thing the route was told to delete or
    # rename is still on the screen afterwards. Real, quotable — and equally writable
    # by an agent that never started the app, since the brief spells it out. (tasksorg's
    # `tasks-delete` / `Call dentist` row left with its case, pruned by QUA-2730.)
    ("contacts-delete~seeded", "fossify-contacts", "Alice"),
    ("cal-edit-event~seeded", "fossify-calendar", "Draft"),
])
def test_a_brief_noun_earns_the_bug_only_once_the_device_has_said_it(tid, app_id, echo):
    """Demoted, not deleted. The honest report that quotes it keeps its credit; the
    report that merely echoes the brief does not get it for free."""
    assert _match(tid, app_id, observed=echo, seen=True) is not None, \
        f"{tid}: an honest sighting of {echo!r} lost its credit"
    assert _match(tid, app_id, observed=echo) is None, \
        f"{tid}: {echo!r} bought the blocking bug with no device behind it"


def test_the_brief_is_not_an_answer_key():
    """The whole brief, sprayed. Nothing the test case put in front of the agent can
    identify a defect on its own — that is what makes `blocking_texts` a measurement
    rather than a restatement of the instructions."""
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "journey_adversary_check.py"
    spec = importlib.util.spec_from_file_location("journey_adversary_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for task in mod._seeded_tasks(None):
        strings = mod.brief_strings(task.bug_spec)
        assert strings, f"{task.id}: the brief yielded no strings — the spray is vacuous"
        for s in strings:
            r = journey.BugReport(step=1, screen=s, observed=s, expected=s,
                                  description=mod.VAGUE, grounded=False)
            assert journey.match_report(r, task.bug_spec) is None, \
                f"{task.id}: the brief's own {s!r} identifies a defect"


def test_symptom_vocabulary_is_read_off_the_claim_not_off_the_quotes():
    """`delete`, `back`, `tags`, `rename` and `not responding` are all symptom entries in
    the corpus AND words the briefs themselves use, so a report that merely QUOTES one
    used to be credited for describing a misbehaviour it never described. The symptom
    route reads `description` — the field the brief defines as the claim — and nothing
    else; `observed` is "text QUOTED from the screen" and `screen` is a label."""
    quoted_only = _match("cal-edit-event~seeded", "fossify-calendar",
                         observed="Rename", screen="Rename an event", expected="Rename")
    assert quoted_only is None
    # The same word as an actual claim is the honest report, and still earns the bug.
    assert _match("cal-edit-event~seeded", "fossify-calendar",
                  description="the event I renamed still shows its old title") \
        == "edit-event-not-saved"


def test_grounding_is_what_the_device_answered_not_what_the_agent_typed():
    """End to end through the real scorer, on the real corpus spec, because grounding is
    read off a TRANSCRIPT and a hand-set flag would not prove the plumbing.

    Three runs of the same report on `contacts-delete~seeded`, whose entire on-screen
    evidence is `Alice` — the contact the brief names. It separates a sighting from a
    guess only if the quote has to come back FROM the device: an agent's own tool
    ARGUMENTS are its words, not the screen's, which is the rule the screen witness has
    always run under. (It ran on tasksorg's tasks-delete and `Call dentist`, the same
    shape, until QUA-2730 pruned that case.)"""
    def episode(*events):
        task = _real("fossify-contacts", "contacts-delete~seeded")
        task.bug_spec["tooling"] = "mcp"
        return journey.journey_verdict(_transcript(
            *events, _write("fail", _bug(4, "Alice",
                                         "it is still there after the delete"))), "m", task)

    # 1. The device answered with it: the honest report, credited and completed.
    v = episode(_obs("Alice  Contacts"))
    assert v.metrics["bugs_found"] == ["contact-delete-broken"]
    assert v.metrics["completed"] and v.metrics["false_reports"] == 0

    # 2. Nothing but the brief behind it. The `echo_texts` route is shut; what remains
    #    is the symptom route on the prose, which is a claim, not a sighting — priced by
    #    polarity, not by this matcher (see `symptom-spray`).
    v = episode(_obs("Contacts  Favorites"))
    assert v.metrics["grounded_reports"] == 0

    # 3. The agent typed the string into a device tool and read back an acknowledgement.
    #    Its own argument must not witness itself.
    v = episode(_call("mcp__device__mobile_type_text",
                      {"device": "d", "text": "Alice"}, "ok"))
    assert v.metrics["grounded_reports"] == 0, "a typed argument grounded its own quote"


def test_a_fabricated_report_no_longer_completes_a_blocked_case():
    """End to end: completion on a blocked case needs the blocking bug NAMED, and the
    blocking bug was nameable by accident on contacts-delete~seeded."""
    task = _real("fossify-contacts", "contacts-delete~seeded")
    task.bug_spec["tooling"] = "mcp"
    v = journey.journey_verdict(_transcript(
        _obs("Contacts"), _write("fail", _bug(3, "anything at all", "it did not work"))), "m", task)
    assert v.metrics["bugs_found"] == [] and v.metrics["false_reports"] == 1
    assert v.metrics["completed"] is False and not v.passed


# ── the board ──────────────────────────────────────────────────────────────────

def test_summary_reports_completion_and_bug_finding_from_totals():
    from datetime import datetime, timezone
    from qualgentbench.result import RunResult, VerifierResult

    def rr(task_id, m):
        return RunResult.build(task_id=task_id, task_version="v", task_type="journey_case",
                               agent="a", model="m", condition="raw", trial=1,
                               started_at=datetime.now(timezone.utc), ended_at=datetime.now(timezone.utc),
                               exit_code=0, verifier=VerifierResult(passed=True, score=1.0, metrics=m),
                               artifact_dir=None, run_id="r", provenance={})
    rows = journey.summary([
        rr("c1~clean", {"version": "clean", "completed": True, "bugs_present": [], "bugs_found": [],
                        "false_reports": 0, "steps": 10, "total_tokens": 100, "app_id": "x"}),
        rr("c1~seeded", {"version": "seeded", "completed": True, "bugs_present": ["a", "b"], "bugs_found": ["a"],
                         "false_reports": 1, "steps": 20, "total_tokens": 300, "app_id": "x"}),
        rr("c2~seeded", {"version": "seeded", "completed": False, "bugs_present": ["c"], "bugs_found": ["c"],
                         "false_reports": 0, "steps": 30, "total_tokens": 300, "app_id": "x"}),
    ])
    r = rows[0]
    assert r["clean_completed"] == 1 and r["clean_episodes"] == 1
    assert r["seeded_completed"] == 1 and r["seeded_episodes"] == 2
    assert r["completion"] == pytest.approx(2 / 3, abs=1e-3)
    assert r["bugs_found"] == 2 and r["bugs_present"] == 3 and r["false_reports"] == 1
    assert r["precision"] == pytest.approx(2 / 3, abs=1e-3) and r["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert r["f1"] == pytest.approx(2 / 3, abs=1e-3) and r["avg_steps"] == 20


def _rr(task_id, m, **kw):
    from datetime import datetime, timezone
    from qualgentbench.result import RunResult, VerifierResult
    return RunResult.build(task_id=task_id, task_version="v", task_type="journey_case",
                           agent=kw.get("agent", "a"), model=kw.get("model", "m"),
                           condition="raw", trial=1,
                           started_at=datetime.now(timezone.utc), ended_at=datetime.now(timezone.utc),
                           exit_code=0, verifier=VerifierResult(passed=True, score=1.0, metrics=m),
                           artifact_dir=None, run_id="r", provenance={})


def test_the_board_counts_truncated_and_excluded_episodes():
    """Truncation scores as not completed and as every seeded bug missed; exclusion
    removes the episode from every number. Both have to be visible or the board reads
    as a complete measurement — one real run scored 14 of 30 planned episodes."""
    rows = journey.summary([
        _rr("c1~seeded", {"version": "seeded", "completed": False, "truncated": True,
                          "bugs_present": ["a"], "bugs_found": [], "false_reports": 0,
                          "steps": 40, "total_tokens": 10, "app_id": "x"}),
        _rr("c2~seeded", {"version": "seeded", "completed": True, "bugs_present": ["b"],
                          "bugs_found": ["b"], "false_reports": 0, "steps": 10,
                          "total_tokens": 10, "app_id": "x"}),
        _rr("c3~clean", {"version": "clean", "completed": None, "bugs_present": [], "bugs_found": [],
                         "false_reports": 0, "steps": 5, "total_tokens": 10, "app_id": "x",
                         "infra_failure": True}),
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["episodes"] == 2 and r["excluded_episodes"] == 1 and r["planned_episodes"] == 3
    assert r["truncated"] == 1
    assert r["completion"] == pytest.approx(0.5)


def test_a_run_that_lost_every_episode_is_still_a_row():
    rows = journey.summary([_rr("c1~seeded", {"version": "seeded", "env_failure": True,
                                              "bugs_present": ["a"], "bugs_found": []})])
    assert len(rows) == 1 and rows[0]["episodes"] == 0 and rows[0]["excluded_episodes"] == 1
    assert rows[0]["completion"] is None and rows[0]["f1"] is None


def test_the_board_does_not_rank_on_completion():
    """Completion is now partly UNSCORED by design (an unevaluated oracle, a screen-text
    oracle), which makes it the least reliable number on the board — so it cannot be a
    ranking key. With no clean arm on either row (no integrity), catch decides."""
    def ep(agent, completed, found):
        return _rr("c~seeded", {"version": "seeded", "completed": completed,
                                "bugs_present": ["a", "b"], "bugs_found": found,
                                "false_reports": 0, "steps": 10, "total_tokens": 10,
                                "app_id": "x"}, agent=agent)
    rows = journey.summary([ep("completer", True, ["a"]), ep("finder", False, ["a", "b"])])
    assert [r["agent"] for r in rows] == ["finder", "completer"]
    assert rows[0]["f1"] == 1.0 and rows[0]["completion"] == 0.0


def _board_arm(agent, n_clean, n_dirty, *, found=("a",), present=("a", "b"), extra=None,
               reports=1):
    """One agent's episodes: `n_clean` quiet clean episodes, `n_dirty` clean episodes with
    `reports` false reports each, and one seeded episode finding `found` of `present`."""
    eps = []
    for i in range(n_clean + n_dirty):
        eps.append(_rr(f"c{i}~clean", {"version": "clean", "completed": True, "bugs_present": [],
                                       "bugs_found": [], "false_reports": reports if i >= n_clean else 0,
                                       "steps": 1, "total_tokens": 1, "app_id": "x",
                                       **(extra or {})}, agent=agent))
    eps.append(_rr("s~seeded", {"version": "seeded", "completed": True,
                                "bugs_present": list(present), "bugs_found": list(found),
                                "false_reports": 0, "steps": 1, "total_tokens": 1,
                                "app_id": "x", **(extra or {})}, agent=agent))
    return eps


def test_equal_f1_rows_order_by_clean_run_integrity():
    """QUA-2780 acceptance: two rows with EQUAL F1 and different false-alarm rates order
    by integrity. Both rows file two false reports (so precision and F1 are equal), but
    `noisy` spreads them over two clean episodes and `quiet` puts both on one — 20% vs
    10% of clean nights dirty. Both integrities@200 round to 0.0; the ranking must still
    tell them apart, or every row measured today ties."""
    rows = journey.summary(_board_arm("noisy", 8, 2) + _board_arm("quiet", 9, 1, reports=2))
    by = {r["agent"]: r for r in rows}
    assert by["noisy"]["f1"] == by["quiet"]["f1"]
    assert by["noisy"]["false_alarm_rate"] == 0.2 and by["quiet"]["false_alarm_rate"] == 0.1
    assert by["noisy"]["clean_integrity_200"] == by["quiet"]["clean_integrity_200"] == 0.0
    assert [r["agent"] for r in rows] == ["quiet", "noisy"]


def test_integrity_outranks_f1_and_catch_breaks_integrity_ties():
    # A higher F1 does not buy a dirtier clean arm a better rank.
    rows = journey.summary(_board_arm("finder", 3, 1, found=("a", "b"))
                           + _board_arm("careful", 4, 0, found=("a",)))
    assert rows[0]["f1"] < rows[1]["f1"]
    assert [r["agent"] for r in rows] == ["careful", "finder"]
    # Equal integrity: the higher catch rate wins.
    rows = journey.summary(_board_arm("half", 4, 0, found=("a",))
                           + _board_arm("all", 4, 0, found=("a", "b")))
    assert [r["agent"] for r in rows] == ["all", "half"]


def test_a_row_without_integrity_ranks_below_one_with_it():
    """No clean episode = no integrity, which is not integrity 100%: an agent must not
    top the board by never being measured on a clean build."""
    unmeasured = [_rr("s~seeded", {"version": "seeded", "completed": True, "bugs_present": ["a"],
                                   "bugs_found": ["a"], "false_reports": 0, "steps": 1,
                                   "total_tokens": 1, "app_id": "x"}, agent="unmeasured")]
    rows = journey.summary(unmeasured + _board_arm("measured", 1, 1))
    assert rows[0]["agent"] == "measured" and rows[1]["clean_integrity_200"] is None
    # Held-out rows stay in their own block below, whatever their integrity.
    held = _board_arm("held", 5, 0, extra={"heldout": True})
    rows = journey.summary(held + _board_arm("public", 1, 1))
    assert [r["heldout"] for r in rows] == [False, True]


def test_summary_rows_carry_cost_and_time_per_episode():
    """$/episode is the MEAN over PRICED episodes with the unpriced count beside it —
    never averaged in as $0; min/episode is the MEDIAN agent wall-clock."""
    from datetime import UTC, datetime, timedelta

    from qualgentbench.result import RunResult, VerifierResult

    def ep(i, cost, minutes):
        t0 = datetime(2026, 9, 23, tzinfo=UTC)
        m = {"version": "clean", "completed": True, "bugs_present": [], "bugs_found": [],
             "false_reports": 0, "steps": 1, "total_tokens": 1, "app_id": "x",
             "cost_usd": cost, "cost_source": "estimated" if cost is not None else "unpriced"}
        return RunResult.build(task_id=f"c{i}~clean", task_version="v", task_type="journey_case",
                               agent="a", model="m", condition="raw", trial=1, started_at=t0,
                               ended_at=t0 + timedelta(minutes=minutes), exit_code=0,
                               verifier=VerifierResult(passed=True, score=1.0, metrics=m),
                               artifact_dir=None, run_id="r", provenance={})
    rows = journey.summary([ep(1, 1.00, 2), ep(2, 3.00, 4), ep(3, None, 30)])
    r = rows[0]
    assert r["cost_per_episode"] == 2.0 and r["cost_priced"] == 2 and r["cost_unpriced"] == 1
    assert r["minutes_per_episode"] == 4.0          # median of 2, 4, 30 — not the mean 12
    assert journey.cost_cells(r) == {"cost": "$2.00 +1 unpriced", "minutes": "4.0"}
    unpriced = journey.summary([ep(1, None, 2), ep(2, None, 3)])[0]
    assert unpriced["cost_per_episode"] is None and unpriced["cost_unpriced"] == 2
    assert journey.cost_cells(unpriced)["cost"] == "— (2 unpriced)"
    assert unpriced["minutes_per_episode"] == 2.5


def test_the_journey_table_prints_cost_time_and_the_ranking_key(monkeypatch):
    """QUA-2780 acceptance: the board prints $/episode and min/episode, and an unpriced
    model prints `—` and the count, never $0.00."""
    from rich.console import Console

    from qualgentbench import cli
    console = Console(record=True, width=260, force_terminal=False)
    monkeypatch.setattr(cli, "console", console)
    priced = _board_arm("priced", 2, 0, extra={"cost_usd": 1.5})
    astra = _board_arm("unpriced", 2, 0, extra={"cost_usd": None})
    cli._print_journey_table(priced + astra)
    text = console.export_text()
    assert "$/ep" in text and "min/ep" in text and "Integrity" in text
    assert "$1.50" in text
    assert "— (3 unpriced)" in text and "$0.00" not in text
    assert "ranked by clean-run integrity" in text and "ranked by F1" not in text


def test_summary_rows_carry_the_rates_with_their_denominators():
    """False alarm is per clean EPISODE (one dirty episode with two reports = 1/2),
    catch is per seeded DEFECT (a case with a functional and a display bug is two),
    blocker recall is over functional defects in L4+L3 only, and every field that was
    on the row before is still there unchanged."""
    defects = {"f1": {"kind": "functional", "tier": "L3"},
               "d1": {"kind": "display", "tier": "L2"},
               "f2": {"kind": "functional", "tier": "L1"}}
    rows = journey.summary([
        _rr("c1~clean", {"version": "clean", "completed": True, "bugs_present": [], "bugs_found": [],
                         "false_reports": 0, "steps": 10, "total_tokens": 10, "app_id": "x"}),
        _rr("c2~clean", {"version": "clean", "completed": None, "bugs_present": [], "bugs_found": [],
                         "false_reports": 2, "steps": 10, "total_tokens": 10, "app_id": "x"}),
        _rr("c1~seeded", {"version": "seeded", "completed": True, "bugs_present": ["f1", "d1"],
                          "bugs_found": ["f1"], "false_reports": 0, "steps": 10, "total_tokens": 10,
                          "app_id": "x", "defects": defects}),
        _rr("c2~seeded", {"version": "seeded", "completed": True, "bugs_present": ["f2"],
                          "bugs_found": ["f2"], "false_reports": 1, "steps": 10, "total_tokens": 10,
                          "app_id": "x", "defects": defects}),
    ])
    r = rows[0]
    # the old fields, unchanged
    assert r["episodes"] == 4 and r["bugs_present"] == 3 and r["bugs_found"] == 2
    assert r["false_reports"] == 3 and r["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert r["clean_episodes"] == 1           # completion-scored clean episodes only
    # false alarm: 1 of 2 clean EPISODES — not 2 of 3 reports, not over `clean_episodes`
    assert r["false_alarm_rate"] == 0.5 and (r["false_alarm_k"], r["false_alarm_n"]) == (1, 2)
    lo, hi = r["false_alarm_ci"]
    assert lo == pytest.approx(0.0946, abs=1e-3) and hi == pytest.approx(0.9054, abs=1e-3)
    # catch: 2 of 3 seeded DEFECTS, the same totals as bugs_found / bugs_present
    assert r["catch_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert (r["catch_k"], r["catch_n"]) == (r["bugs_found"], r["bugs_present"])
    assert r["catch_ci"][0] < r["catch_rate"] < r["catch_ci"][1]
    # integrity at the fixed reference point: 0.5^200 is 0 to four places, and the
    # interval is the rate's interval pushed through (1 - p)^200, high p -> low bound
    assert r["clean_integrity_200"] == 0.0
    assert r["clean_integrity_200_ci"] == [pytest.approx((1 - hi) ** 200, abs=1e-4),
                                           pytest.approx((1 - lo) ** 200, abs=1e-4)]
    # blocker recall: only f1 (functional L3) qualifies; d1 is display, f2 is L1
    assert r["blocker_recall"] == 1.0 and (r["blocker_found"], r["blocker_n"]) == (1, 1)
    assert r["blocker_recall_ci"][0] == pytest.approx(0.2065, abs=1e-3)
    assert r["blocker_recall_ci"][1] == 1.0


def test_summary_rates_are_none_not_zero_when_undefined():
    rows = journey.summary([
        _rr("c1~seeded", {"version": "seeded", "completed": True, "bugs_present": ["d"],
                          "bugs_found": ["d"], "false_reports": 0, "steps": 1, "total_tokens": 1,
                          "app_id": "x", "defects": {"d": {"kind": "display", "tier": "L4"}}}),
    ])
    r = rows[0]
    assert r["false_alarm_rate"] is None and r["false_alarm_ci"] is None and r["false_alarm_n"] == 0
    assert r["clean_integrity_200"] is None
    assert r["blocker_recall"] is None and r["blocker_recall_ci"] is None and r["blocker_n"] == 0
    assert r["catch_rate"] == 1.0
    empty = journey.summary([_rr("c1~clean", {"version": "clean", "env_failure": True})])[0]
    assert empty["catch_rate"] is None and empty["false_alarm_rate"] is None
    assert empty["blocker_recall"] is None


def test_blocker_recall_resolves_tiers_from_the_corpus_key_when_metrics_carry_ids_only():
    """Saved episodes carry defect IDs, not tiers; the board looks kind/tier up in the
    app's test-case file — the same key rescoring reads."""
    defects = journey.load_defects(journey.load_cases("medtimer"))
    top = next(d for d, meta in defects.items()
               if meta["kind"] == "functional" and meta["tier"].upper() in journey.BLOCKER_TIERS)
    low = next((d for d, meta in defects.items()
                if meta["kind"] == "functional" and meta["tier"].upper() not in journey.BLOCKER_TIERS),
               None)
    present = [top] + ([low] if low else [])
    rows = journey.summary([
        _rr("m1~seeded", {"version": "seeded", "completed": True, "bugs_present": present,
                          "bugs_found": [], "false_reports": 0, "steps": 1, "total_tokens": 1,
                          "app_id": "medtimer"}),
    ])
    assert (rows[0]["blocker_found"], rows[0]["blocker_n"]) == (0, 1)
    assert rows[0]["blocker_recall"] == 0.0
    # An app with no case file resolves nothing: no blocker, not a zero.
    rows = journey.summary([
        _rr("z~seeded", {"version": "seeded", "completed": True, "bugs_present": ["q"],
                         "bugs_found": [], "false_reports": 0, "steps": 1, "total_tokens": 1,
                         "app_id": "no-such-app"}),
    ])
    assert rows[0]["blocker_recall"] is None and rows[0]["blocker_n"] == 0


def test_rates_lines_show_both_rates_with_intervals_and_blocker_recall():
    rows = journey.summary([
        _rr("c1~clean", {"version": "clean", "completed": True, "bugs_present": [], "bugs_found": [],
                         "false_reports": 0, "steps": 1, "total_tokens": 1, "app_id": "x"}),
        _rr("c1~seeded", {"version": "seeded", "completed": True, "bugs_present": ["f"],
                          "bugs_found": ["f"], "false_reports": 0, "steps": 1, "total_tokens": 1,
                          "app_id": "x", "defects": {"f": {"kind": "functional", "tier": "L4"}}}),
    ])
    text = "\n".join(journey.rates_lines(rows))
    assert "false alarm / clean case 0/1 0% [0–79]" in text
    assert "catch / seeded defect 1/1 100% [21–100]" in text
    assert "blocker recall (functional L4+L3): 1/1 100% [21–100]" in text
    assert f"clean-run integrity @{journey.INTEGRITY_N}" in text


def test_the_journey_table_renders_the_new_columns():
    from qualgentbench import cli
    cli._print_journey_table([
        _rr("c1~seeded", {"version": "seeded", "completed": None, "truncated": True,
                          "bugs_present": ["a"], "bugs_found": [], "false_reports": 1,
                          "steps": 40, "total_tokens": 10, "app_id": "x",
                          "oracle": {"mode": "db", "ok": None, "why": "db oracle not evaluated",
                                     "detail": "sqlite3: not found"}}),
        _rr("c2~clean", {"version": "clean", "completed": True, "bugs_present": [], "bugs_found": [],
                         "false_reports": 0, "steps": 9, "total_tokens": 10, "app_id": "y",
                         "oracle": {"mode": "db", "ok": True, "why": "db oracle holds", "detail": "1"}}),
        _rr("c3~clean", {"version": "clean", "bugs_present": [], "bugs_found": [],
                         "infra_failure": True, "app_id": "y"}),
    ])


def test_the_journey_adversary_gate_holds():
    """`scripts/journey_adversary_check.py` is the gate between journey mode and a
    published number (hunt mode has had one since the beginning). Mirrored here so the
    suite fails with it: every synthetic guesser must earn no bug and no completion over
    the real corpus, and the honest control must be credited for every defect the corpus
    measured a quotable string for."""
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "journey_adversary_check.py"
    spec = importlib.util.spec_from_file_location("journey_adversary_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tasks = mod._seeded_tasks(None)
    # Six public apps × five cases; the held-out split is not in the repository.
    assert len(tasks) >= 30, "no seeded journey tasks — the guard would be vacuous"
    assert not mod._no_symptom_leaks_into_the_filler_prose(tasks)
    # The roster must still contain the two adversaries that quote real screen text
    # (QUA-2717). Without them the rest of this test passes on reports that identify
    # nothing, which is how five exemplars shipped over a gate that could not see them.
    assert {"brief-echo", "dialog-echo"} <= set(mod.GUESSERS)

    # Both report channels (QUA-2777): the findings file and DevLoop's
    # `mobile_report_result`, which the scorer reads when no file exists.
    assert set(mod.CHANNELS) == {"findings", "report_tool"}
    for channel in mod.CHANNELS:
        for mode in mod.GUESSERS:
            for task in tasks:
                m = mod.run(task, mode, channel)
                assert m["bugs_found"] == [], f"{mode} credited on {task.id} via {channel}"
                assert m["completed"] is not True, f"{mode} completed {task.id} via {channel}"
    assert sum(len(mod.run(t, "honest", "report_tool")["bugs_found"]) for t in tasks) > 0, \
        "the honest control earns nothing through the report tool — the channel is dead"
    for task in tasks:
        m = mod.run(task, "honest")
        for bug_id in set(m["bugs_present"]) - set(m["bugs_found"]):
            # Missing it is allowed only when the corpus measured nothing quotable for it
            # — that is a corpus gap the gate prints, not a scorer that cannot read an
            # honest report.
            assert not (mod._quotes(task.bug_spec, bug_id)
                        or mod._absences(task.bug_spec, bug_id)), \
                f"honest missed {bug_id} on {task.id}"


def test_the_symptom_sprayer_is_priced_rather_than_asserted_to_zero():
    """The adversary the roster could never hold, and why it is not in `GUESSERS`.

    Prose with nothing quoted is the ONLY report a functional defect with no string to
    quote (a dropped field, a lost reminder) can ever have — a dozen of the corpus's
    seeded defects are that shape — so a matcher that refused it would refuse the honest
    report with it. `symptom-spray` therefore earns credit by design, and what is
    asserted is the PRICE: nothing is active on a clean build, so the same report is a
    false report there, on EVERY clean episode. Recall that stops costing a dirty night
    is the regression this catches."""
    import importlib.util

    path = Path(__file__).parents[1] / "scripts" / "journey_adversary_check.py"
    spec = importlib.util.spec_from_file_location("journey_adversary_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "symptom-spray" in mod.PRICED and "symptom-spray" not in mod.GUESSERS
    seeded, clean = mod._seeded_tasks(None), mod._tasks(None, "clean")
    assert clean, "no clean arms to price the sprayer against"
    credited = sum(len(mod.run(t, "symptom-spray")["bugs_found"]) for t in seeded)
    assert credited > 0, "the sprayer earns nothing — this test has stopped measuring anything"
    paid, n_clean, quiet = mod._price(clean, "symptom-spray")
    assert not quiet and paid == n_clean, f"{len(quiet)} clean episode(s) cost it nothing"


def test_staging_pins_the_device_timezone(monkeypatch):
    import asyncio
    from qualgentbench import episode_runner as er
    calls = []

    async def fake_adb(*args):
        calls.append(" ".join(args))
        if "getprop" in args[-1]:
            return 0, er.DEVICE_TIMEZONE + "\n"
        return 0, ""
    monkeypatch.setattr(er, "_adb", fake_adb)
    asyncio.run(er.run_device_setup("emulator-1", None))
    assert calls[0] == f"-s emulator-1 shell cmd alarm set-timezone {er.DEVICE_TIMEZONE}"
    assert er.DEVICE_TIMEZONE == "America/Chicago"


def test_journey_mode_prefers_the_journey_build(monkeypatch, tmp_path):
    """The test-case file's `apk:` block is the journey build; it gets its own cache
    slot (journey/) so it never overwrites the hunt build with the same file name."""
    import pathlib
    from qualgentbench import preflight

    class NoDist(type(pathlib.Path())):          # this machine has dist/ builds; hide them
        def exists(self):
            return False if "dist" in self.parts else super().exists()

    monkeypatch.setattr(preflight, "Path", NoDist)
    monkeypatch.setattr(preflight, "_cache_root", lambda: tmp_path)
    monkeypatch.delenv("QUALGENTBENCH_APK_ORGZLY", raising=False)
    meta = journey.apk_meta("orgzly")
    assert meta and meta["filename"] == "journey/orgzly-buggy.apk" and len(meta["sha256"]) == 64
    app = {"id": "orgzly"}
    spec = {"apk": {"filename": "hard/orgzly-buggy.apk", "sha256": "x"}}
    hunt = preflight.resolve_apk_offline(app, spec, mode="hunt")
    jour = preflight.resolve_apk_offline(app, spec, mode="journey")
    assert str(hunt).endswith("seeded/orgzly/orgzly-buggy.apk")
    assert str(jour).endswith("journey/orgzly/orgzly-buggy.apk")


def test_content_provider_oracle_is_evaluated_on_the_device():
    """Contacts live in the system provider, outside the sandbox: a `content:` outcome
    is read on the device after the agent exits, exactly like a `db:` one."""
    contacts = next(t for t in _app("fossify-contacts") if t.id == "contacts-create~clean")
    assert contacts.bug_spec["oracle"]["mode"] == "content"
    spec = _spec("clean", oracle={"mode": "content", "expect": {"content": "content://x", "contains": "y"}, "evidence": []},
                 oracle_result="holds")
    v = journey.journey_verdict(_transcript(_obs("anything"), _write("pass")), "m", _task(spec))
    assert v.metrics["completed"] is True and v.metrics["oracle"]["mode"] == "content"
    spec = _spec("clean", oracle={"mode": "content", "expect": {"content": "content://x", "contains": "y"}, "evidence": []},
                 oracle_result="violated")
    v = journey.journey_verdict(_transcript(_obs("anything"), _write("pass")), "m", _task(spec))
    assert v.metrics["completed"] is False


def test_launch_activity_skips_system_chooser_and_debug_tools():
    from qualgentbench.verify.device import _pick_launch_activity
    bundle = "com.ichi2.anki.debug"
    chooser = ["priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false",
               "android/com.android.internal.app.ResolverActivity"]
    assert _pick_launch_activity(bundle, chooser) == ""
    launchers = ["  com.ichi2.anki.debug/leakcanary.internal.activity.LeakLauncherActivity",
                 "  com.ichi2.anki.debug/com.ichi2.anki.IntentHandler",
                 "  com.ichi2.anki/com.ichi2.anki.IntentHandler"]
    assert _pick_launch_activity(bundle, launchers) == "com.ichi2.anki.debug/com.ichi2.anki.IntentHandler"
    assert _pick_launch_activity("com.ichi2.anki", launchers) == "com.ichi2.anki/com.ichi2.anki.IntentHandler"


# ── the screen witness: `evidence:` is scored, not discarded ───────────────────
#
# docs/journey-oracle-audit.md, "Screen witness": a read-only case is completed by the
# `evidence:` strings the brief asks the agent to read — right verdict AND every witness
# in the text the DEVICE answered with. No device text at all stays None, never False.

WITNESSED = {"mode": "present", "expect": {"present": "Max: 85 kg"},
             "evidence": ["Max: 85 kg"], "witness": ["Max: 85 kg"]}
DB_WITNESSED = {"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"},
                "evidence": ["140.00"], "witness": ["140.00"]}


def test_the_oracle_carries_a_witness_only_when_the_case_declares_evidence():
    with_it = journey._oracle({"check": {"expect": {"present": "x"}}, "evidence": ["x", " "]})
    assert with_it["witness"] == ["x"] and with_it["evidence"] == ["x", " "]
    # A `present:` case without `evidence:` still gets the present string as evidence
    # (the older contract) but NO witness — it must stay unscored.
    without = journey._oracle({"check": {"expect": {"present": "x"}}})
    assert without["evidence"] == ["x"] and "witness" not in without
    db = journey._oracle({"check": {"expect": {"db": "d", "query": "q", "equals": "1"}}, "evidence": ["140.00"]})
    assert db["mode"] == "db" and db["witness"] == ["140.00"]


def test_a_seen_witness_completes_a_read_only_case():
    t = _task(_spec("clean", oracle=WITNESSED))
    v = journey.journey_verdict(_transcript(
        _obs("Weight  Min: 74 kg  Max: 85 kg  Avg: 79 kg"), _write("pass")), "m", t)
    assert v.metrics["completed"] is True and v.metrics["completion_scored"] is True
    assert v.metrics["witness"] == {"required": ["Max: 85 kg"], "seen": ["Max: 85 kg"],
                                    "missing": [], "scored": True}
    assert v.passed and v.score == 1.0 and v.criteria["completed"] is True
    assert "not scored" not in (v.failure_reason or "")


def test_a_missing_witness_with_device_text_is_not_completed():
    t = _task(_spec("clean", oracle=WITNESSED))
    v = journey.journey_verdict(_transcript(
        _obs("Weight  Min: 74 kg  Avg: 79 kg"), _write("pass")), "m", t)
    assert v.metrics["completed"] is False and v.metrics["completion_scored"] is True
    assert v.metrics["witness"]["missing"] == ["Max: 85 kg"] and v.metrics["witness"]["scored"]
    assert "not witnessed" in v.failure_reason and "Max: 85 kg" in v.failure_reason
    assert not v.passed and v.score == 0.0
    # The same episode on the seeded PASS arm (display bug only) is scored the same way.
    t = _task(_spec("seeded", ["avg-bug"], oracle=WITNESSED))
    v = journey.journey_verdict(_transcript(
        _obs("Weight  Min: 74 kg  Avg: 76 kg"), _write("pass", _bug(2, "Avg: 76 kg", "should be 79"))), "m", t)
    assert v.metrics["completed"] is False and v.metrics["bugs_found"] == ["avg-bug"]


def test_no_device_text_at_all_leaves_a_witnessed_case_unscored():
    """The screenshot-only agent: device calls were made, nothing came back as text.
    None, never False — the stopgap's fairness argument, kept exactly."""
    t = _task(_spec("clean", oracle=WITNESSED))
    v = journey.journey_verdict(_transcript(_obs(""), _obs("   "), _write("pass")), "m", t)
    assert v.criteria["evidence"] is True                    # the device WAS driven
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False
    assert "no device text to witness" in v.failure_reason
    assert v.metrics["witness"] == {"required": ["Max: 85 kg"], "seen": [],
                                    "missing": ["Max: 85 kg"], "scored": False}
    assert v.passed                                          # bug finding still stands
    row = journey._row(("a", "m", "raw"), [v])
    assert row["completion"] is None and row["completion_unscored"] == 1


def test_a_status_only_answer_does_not_cost_the_screenshot_only_agent_its_exemption():
    """QUA-2715, run 20260917-004716-9e69, the real shape of this failure.

    The agent tried `uiautomator dump` FIRST, the platform SIGKILLed it 8/8, and it
    fell back to reading every screen as a screenshot image. The only device TEXT in
    the entire episode was the kill's own `exit=137` — which IS an observation result
    by `_RAW_OBSERVE_RE`, so the exemption was defeated by the failure of the very
    command the brief had told the agent to use, and an episode with the right
    verdict and a satisfied oracle scored `completed: false`.
    """
    spec = _spec("clean", oracle=WITNESSED)
    spec["tooling"] = "raw"                                  # the bare adb arm
    t = _task(spec)
    v = journey.journey_verdict(_transcript(
        _call("Bash", {"command": "adb shell uiautomator dump"}, "exit=137"),
        _call("Bash", {"command": "adb shell uiautomator dump /sdcard/wd.xml"}, "Killed"),
        _call("Bash", {"command": "adb shell input tap 100 200"}, ""),
        _write("pass")), "m", t)
    assert v.criteria["evidence"] is True                    # the device WAS driven
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False
    assert "no device text to witness" in v.failure_reason
    assert v.metrics["witness"]["scored"] is False

    # The same holds when the dump SUCCEEDS: the shell→file form answers with its
    # confirmation line alone, and the hierarchy only arrives from a later `cat`.
    v = journey.journey_verdict(_transcript(
        _call("Bash", {"command": "adb shell uiautomator dump"},
              "UI hierarchy dumped to: /sdcard/window_dump.xml"),
        _call("Bash", {"command": "adb shell input tap 100 200"}, ""),
        _write("pass")), "m", t)
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False


def test_a_focus_query_and_a_failed_read_are_not_screen_content():
    """The second shape of the same failure, from the trial run 20260917-021029-67f4.

    Neither of these could carry a witness, and both used to defeat the exemption:
    `dumpsys window`'s `mCurrentFocus=` answers which WINDOW has focus — real device
    text, useful to an agent, but never a string the app DREW — and `cat:` complaining
    about a dump that was killed before it was written is the shell talking, not the
    screen. The agent below read every actual screen as an image.
    """
    spec = _spec("clean", oracle=WITNESSED)
    spec["tooling"] = "raw"
    t = _task(spec)
    v = journey.journey_verdict(_transcript(
        _call("Bash", {"command": "adb shell dumpsys window | grep mCurrentFocus"},
              "  mCurrentFocus=Window{29227a8 u0 com.example/com.example.MainActivity}"),
        _call("Bash", {"command": "adb shell uiautomator dump && adb shell cat /sdcard/ui.xml"},
              "exit=137\ncat: /sdcard/ui.xml: No such file or directory"),
        _call("Bash", {"command": "adb shell input tap 100 200"}, ""),
        _write("pass")), "m", t)
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False
    assert "no device text to witness" in v.failure_reason


def test_status_noise_beside_real_screen_text_still_scores_the_witness():
    """The exemption widens for agents the device never answered with CONTENT — not
    for agents that read the screen and simply did not reach the outcome. One real
    hierarchy read is enough to put the witness back on the hook."""
    spec = _spec("clean", oracle=WITNESSED)
    spec["tooling"] = "raw"
    t = _task(spec)
    v = journey.journey_verdict(_transcript(
        _call("Bash", {"command": "adb shell uiautomator dump"}, "exit=137"),
        _call("Bash", {"command": "adb shell cat /sdcard/v.xml"},
              '<node text="Weight  Min: 74 kg  Avg: 79 kg" />'),
        _call("Bash", {"command": "adb shell input tap 100 200"}, ""),
        _write("pass")), "m", t)
    assert v.metrics["completed"] is False and v.metrics["completion_scored"] is True
    assert v.metrics["witness"]["missing"] == ["Max: 85 kg"]

    # And a witness that WAS seen completes even if every other answer was status
    # noise — the exemption is only ever reached when the witness is missing.
    v = journey.journey_verdict(_transcript(
        _call("Bash", {"command": "adb shell uiautomator dump"}, "exit=137"),
        _call("Bash", {"command": "adb shell cat /sdcard/v.xml"},
              '<node text="Weight  Min: 74 kg  Max: 85 kg" />'),
        _call("Bash", {"command": "adb shell input tap 100 200"}, ""),
        _write("pass")), "m", t)
    assert v.metrics["completed"] is True and v.metrics["witness"]["seen"] == ["Max: 85 kg"]


def test_a_typed_argument_never_witnesses_itself():
    """orgzly's witness is the note title the agent also TYPES into the search box; the
    device must show it back. Only what the device answered counts."""
    spec = _spec("clean", oracle={"mode": "present", "expect": {"present": "Team meeting"},
                                  "evidence": ["Team meeting"], "witness": ["Team meeting"]})
    typed = _call("mobile_type_text", {"device": "d", "text": "Team meeting"}, "ok")
    v = journey.journey_verdict(_transcript(typed, _obs("Search  Notebook"), _write("pass")), "m", _task(spec))
    assert v.metrics["completed"] is False and v.metrics["witness"]["missing"] == ["Team meeting"]
    v = journey.journey_verdict(_transcript(typed, _obs("Search results  Team meeting"), _write("pass")),
                                "m", _task(spec))
    assert v.metrics["completed"] is True
    # The stream flag behind it: calls and results told apart only when asked.
    stream = bugs._ordered_stream(_transcript(typed, _obs("Search")), "mcp", split_calls=True)
    assert [k for k, _ in stream if k.startswith("device")] == ["device_call", "device", "device_call", "device"]
    plain = bugs._ordered_stream(_transcript(typed, _obs("Search")), "mcp")
    assert [k for k, _ in plain if k.startswith("device")] == ["device"] * 4


def test_a_witness_matches_on_token_boundaries():
    spec = _spec("clean", oracle={"mode": "present", "expect": {"present": "170"},
                                  "evidence": ["170"], "witness": ["170"]})
    v = journey.journey_verdict(_transcript(_obs("Height  1700 mm"), _write("pass")), "m", _task(spec))
    assert v.metrics["completed"] is False
    v = journey.journey_verdict(_transcript(_obs("Height  170 cm"), _write("pass")), "m", _task(spec))
    assert v.metrics["completed"] is True


def test_a_db_oracle_that_holds_still_needs_its_declared_witness():
    seen = journey.journey_verdict(_transcript(_obs("Checking  140.00"), _write("pass")), "m",
                                   _task(_spec("clean", oracle=DB_WITNESSED, oracle_result="holds")))
    assert seen.metrics["completed"] is True and seen.metrics["witness"]["scored"] is True
    missing = journey.journey_verdict(_transcript(_obs("Checking  100.00"), _write("pass")), "m",
                                      _task(_spec("clean", oracle=DB_WITNESSED, oracle_result="holds")))
    assert missing.metrics["completed"] is False and missing.metrics["completion_scored"] is True
    assert "db oracle holds, but the outcome was not witnessed" in missing.failure_reason
    # No device text: the witness half cannot be judged — None, as in present mode.
    blind = journey.journey_verdict(_transcript(_obs(""), _write("pass")), "m",
                                    _task(_spec("clean", oracle=DB_WITNESSED, oracle_result="holds")))
    assert blind.metrics["completed"] is None and blind.metrics["completion_scored"] is False
    # The oracle dominates: violated is not completed whatever the screen showed.
    broken = journey.journey_verdict(_transcript(_obs("Checking  140.00"), _write("pass")), "m",
                                     _task(_spec("clean", oracle=DB_WITNESSED, oracle_result="violated")))
    assert broken.metrics["completed"] is False and broken.metrics["witness"]["scored"] is False
    blind_broken = journey.journey_verdict(_transcript(_obs(""), _write("pass")), "m",
                                           _task(_spec("clean", oracle=DB_WITNESSED, oracle_result="violated")))
    assert blind_broken.metrics["completed"] is False
    # An unevaluated oracle stays unscored, witness or not.
    none = journey.journey_verdict(_transcript(_obs("Checking  140.00"), _write("pass")), "m",
                                   _task(_spec("clean", oracle=DB_WITNESSED)))
    assert none.metrics["completed"] is None and none.metrics["witness"]["scored"] is False


def test_an_expected_fail_arm_ignores_the_witness():
    """A blocked case completes on fail + the blocking bug named, as today; the witness
    is recorded for the record but never consulted."""
    t = _task(_spec("seeded", ["delete-bug"], oracle=WITNESSED))
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"), _write("fail", _bug(2, "85 kg", "the entry is still listed after delete"))), "m", t)
    assert v.metrics["completed"] is True and v.metrics["completion_scored"] is True
    assert v.metrics["witness"] == {"required": ["Max: 85 kg"], "seen": [],
                                    "missing": ["Max: 85 kg"], "scored": False}
    # Everything checkable before the witness still stands on a witnessed case.
    wrong = journey.journey_verdict(_transcript(_obs("Max: 85 kg"), _write("fail")), "m",
                                    _task(_spec("clean", oracle=WITNESSED)))
    assert wrong.metrics["completed"] is False and "reported fail" in wrong.failure_reason
    assert wrong.metrics["witness"]["seen"] == ["Max: 85 kg"] and wrong.metrics["witness"]["scored"] is False


def test_the_corpus_witnessed_cases_are_scoreable_on_the_clean_arm():
    """Every case that declares `evidence:` now has a scored clean arm; every
    `present:`/`absent:` case declares one (none is left on the stopgap)."""
    witnessed, screen_only = set(), set()
    for suite in bugs.load_apps():
        app_id = suite["app"]["id"]
        if not journey.has_cases(app_id):
            continue
        for t in journey.journey_tasks(suite):
            o = t.bug_spec["oracle"]
            if o.get("witness"):
                witnessed.add(t.bug_spec["case_id"])
            if o["mode"] in ("present", "absent"):
                screen_only.add(t.bug_spec["case_id"])
    assert screen_only <= witnessed, screen_only - witnessed
    assert {"medtimer-review-aspirin", "anki-browse-cards"} <= witnessed


def test_only_screen_reads_can_witness_a_case():
    """A tap's acknowledgement is a device RESULT but not a screen read. A witness
    string that only ever appears in an ack (a tool echoing its own argument) is not
    seen; with no screen text at all the case stays unscored (None), never a scored
    miss. (An agent with NO observation whatsoever already fails the older evidence
    tripwire before the witness is consulted.)"""
    t = _task(_spec("clean", oracle=WITNESSED))
    echoed_by_a_tap = _transcript(
        _call("mobile_tap", {"element": "Max: 85 kg"}, "tapped Max: 85 kg"),
        _obs(""), _write("pass"))
    v = journey.journey_verdict(echoed_by_a_tap, "m", t)
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False
    assert v.metrics["witness"]["seen"] == [] and v.metrics["witness"]["scored"] is False
    # The same episode with one real screen read carrying the witness is completed.
    v = journey.journey_verdict(_transcript(
        _call("mobile_tap", {"element": "Statistics"}, "ok"),
        _obs("Weight  Min: 74 kg  Max: 85 kg"), _write("pass")), "m", t)
    assert v.metrics["completed"] is True and v.metrics["witness"]["seen"] == ["Max: 85 kg"]


def test_raw_arm_witness_needs_a_hierarchy_dump():
    """Raw adb: the screen read is `uiautomator dump` + reading the XML back; a
    `shell input tap` result is an ack."""
    from qualgentbench.journey import _observation_texts
    raw = _transcript(
        _call("Bash", {"command": "adb shell input tap 100 200"}, ""),
        _call("Bash", {"command": "adb shell uiautomator dump /sdcard/v.xml && adb shell cat /sdcard/v.xml"},
              '<node text="Max: 85 kg" />'))
    got = _observation_texts(raw, "raw")
    assert len(got) == 1 and "max: 85 kg" in got[0]
    assert _observation_texts(_transcript(_call("Bash", {"command": "adb shell input tap 1 2"}, "ok")), "raw") == []


def test_devloop_reads_are_observations():
    """QUA-2775: DevLoop reads the screen through more than `mobile_observe_screen`. A
    hierarchy read and a satisfied wait both answer with what the app drew, so both
    ground and witness; only the whole-screen read revokes the screenshot-only
    exemption (`screen_only`), as only a hierarchy dump does on the raw arm."""
    from qualgentbench.journey import _observation_texts
    t = _transcript(
        _call("mcp__device__mobile_native_hierarchy", {"device": "d"},
              '{"nodes": [{"class": "TextView", "text": "Max: 85 kg"}]}'),
        _call("mcp__device__mobile_await_element", {"device": "d", "text": "Statistics"},
              '{"satisfied": true, "match": {"text": "Statistics"}}'),
        _call("mcp__device__mobile_tap", {"device": "d", "element": "Statistics"}, "ok"))
    got = _observation_texts(t, "mcp")
    assert len(got) == 2
    assert "max: 85 kg" in got[0] and "statistics" in got[1]
    screen = _observation_texts(t, "mcp", screen_only=True)
    assert len(screen) == 1 and "max: 85 kg" in screen[0]
    # And the witness is completed off a hierarchy read alone.
    v = journey.journey_verdict(_transcript(
        _call("mobile_native_hierarchy", {"device": "d"}, '{"text": "Weight  Max: 85 kg"}'),
        _write("pass")), "m", _task(_spec("clean", oracle=WITNESSED)))
    assert v.metrics["completed"] is True and v.metrics["witness"]["seen"] == ["Max: 85 kg"]


def test_a_read_is_decided_by_the_tool_name_not_its_arguments():
    """The call payload is `<name> <json args>`; an argument that mentions an observe
    tool must not make a tap's acknowledgement a screen read."""
    from qualgentbench.journey import _observation_texts
    t = _transcript(_call("mobile_tap", {"element": "mobile_observe_screen"}, "Max: 85 kg"))
    assert _observation_texts(t, "mcp") == []


def test_bookkeeping_replies_are_not_device_text():
    """DevLoop's bookkeeping tools echo the agent's own words back (a note, a step
    mark, the report). None of it came from the device, so none of it may ground a
    quote or witness a case — only the device's own answers are device text."""
    from qualgentbench.journey import _device_texts
    t = _transcript(
        _call("mcp__device__mobile_note_anomaly", {"observation": "85 kg still listed"},
              '{"noted": "85 kg still listed"}'),
        _call("mcp__device__mobile_mark_step", {"test_step": 1}, '{"recorded": 3, "step": "Max: 85 kg"}'),
        _call("mcp__device__mobile_report_result", {"status": "FAIL"}, "Max: 85 kg"),
        _obs("Weight  Min: 74 kg"))
    results = _device_texts(t, "mcp", results_only=True)
    assert results == ["weight  min: 74 kg"]
    assert not any("85 kg" in x for x in _device_texts(t, "mcp"))



# ── crash cases: a death has no screen diff, so its evidence is the dialog ────
#
# Added with the corpus's crash-report credit path (QUA-2710). The harness could
# already DETECT and ATTRIBUTE a death (replay.gate_crash, verify/canary fired
# markers), but the agent-facing credit path was built for functional and display
# defects, whose evidence is a string derive_journey measured in the clean/seeded
# screen diff. A crash produces no such diff: the route ends where the app ended.
#
# Anchored on the corpus's first landed crash case (fossify-calendar's
# cal-search-event, QUA-2714) rather than a synthetic one, because the hole these
# tests close was a REAL row in a REAL truth file — see
# test_a_crash_case_does_not_credit_strings_the_agent_typed_or_the_app_behind_it.

CRASH_CASE = "cal-search-event~seeded"
CRASH_APP = "fossify-calendar"


def test_a_crash_case_carries_dialog_evidence_and_no_screen_diff():
    """A death case's evidence is the crash, not the screen diff — and the crash's two
    halves are not worth the same (QUA-2717). The SIGNATURE names this death and no
    other; the platform DIALOG is the same sentence on every crash in the corpus and is
    spelled out by the app name the brief already gave the agent, so it lands in
    `echo_texts`, where credit needs the device to have answered with it."""
    spec = _real(CRASH_APP, CRASH_CASE).bug_spec
    assert spec["blocking"] == "search-results-off-main-thread"
    assert spec["oracle"]["gate"] == {"crash": "CalledFromWrongThreadException"}
    assert spec["crash_texts"] == ["CalledFromWrongThreadException"]
    assert "Fossify Calendar keeps stopping" in spec["echo_texts"]   # qualified
    assert "keeps stopping" in spec["echo_texts"]                    # and bare
    # The strings only the CLEAN arm went on to show are exactly what the agent could
    # NOT have seen, so none of them is evidence for this defect.
    assert spec["blocking_texts"] == [] and spec["absence_texts"] == []
    # The clean arm is never handed the answer key.
    clean = _real(CRASH_APP, "cal-search-event~clean").bug_spec
    assert clean["crash_texts"] == [] and clean["echo_texts"] == []
    assert clean["active_bugs"] == []


def test_crash_evidence_is_built_from_the_gate_the_case_declares():
    empty = {"signature": [], "dialog": []}
    assert journey.crash_evidence(None) == empty
    assert journey.crash_evidence({}) == empty
    # `crash:` gets the crash wordings; `anr:`/`stuck:` get the hang wordings. A
    # `stuck:` value is a UI ANCHOR, not a signature, so it is never quoted as evidence.
    crash = journey.crash_evidence({"crash": "IllegalStateException"}, "App")
    assert crash["signature"] == ["IllegalStateException"]
    assert "App keeps stopping" in crash["dialog"]
    assert not any("responding" in t for t in crash["dialog"])
    stuck = journey.crash_evidence({"stuck": "Save"}, "App")
    assert not any("Save" in t for t in stuck["dialog"] + stuck["signature"])
    assert "App is not responding" in stuck["dialog"]
    # `anr: true` names nothing, so a freeze case has NO defect-identifying evidence at
    # all — the dialog is the whole of it, and that is why grounding carries it.
    assert journey.crash_evidence({"anr": True}, "App") == stuck
    assert stuck["signature"] == []
    # `anr: "<reason>"` is a signature and IS quotable on its own.
    assert journey.crash_evidence({"anr": "Input dispatching timed out"}, "App")["signature"] \
        == ["Input dispatching timed out"]


@pytest.mark.parametrize("observed,seen,credited", [
    # The signature: nowhere in the brief, names THIS death. Evidence on its own.
    ("CalledFromWrongThreadException", False, True),
    # The platform dialog, as the device wrote it — an honest report quotes exactly
    # this, and it is credited once the transcript shows the device said it.
    ("Fossify Calendar keeps stopping", True, True),
    ("keeps stopping", True, True),
    # The same sentence with no device contact behind it. It is identical on every
    # crash in the corpus and the app name comes straight off the brief, so quoting it
    # is a guess — QUA-2717's `dialog-echo`.
    ("Fossify Calendar keeps stopping", False, False),
    ("keeps stopping", False, False),
])
def test_a_report_quoting_the_crash_is_credited(observed, seen, credited):
    got = _match(CRASH_CASE, CRASH_APP, observed=observed, seen=seen)
    assert got == ("search-results-off-main-thread" if credited else None)


# The hole this closes, measured on the real truth row for cal-search-event: before
# `crash_texts`, a death case still derived `blocking_texts` from `unclaimed_diff`, and
# on a crash that diff is not the defect. It held the title and search term the AGENT
# ITSELF TYPED ("Dentist", "Dent") and, because the seeded app had died and the dump
# caught whatever was behind it, another app's launcher screen ("Sign in", "TrustLoop").
# Quoting any of them matched the blocking bug — and a blocked case completes on "fail
# + the blocking bug named", so it bought recall AND completion. Same shape as the
# `contacts-delete` / `A` hole this suite already pins above.
@pytest.mark.parametrize("observed", ["Dentist", "Dent", "Sign in", "TrustLoop", "Back"])
def test_a_crash_case_does_not_credit_strings_the_agent_typed_or_the_app_behind_it(observed):
    assert _match(CRASH_CASE, CRASH_APP, observed=observed) is None


@pytest.mark.parametrize("observed,description", [
    ("1", "the screen did not look the way the test case describes"),
    ("", "something about this felt off while I was working through it"),
    ("", "the layout seemed a bit cramped"),
])
def test_a_guess_earns_nothing_on_a_crash_case(observed, description):
    assert _match(CRASH_CASE, CRASH_APP, observed=observed, description=description) is None


def test_the_crash_case_completes_only_when_the_death_is_named():
    """End to end through the real scorer, on the real corpus spec. A blocked case
    completes on `fail` PLUS the blocking bug named — nothing else."""
    t = _real(CRASH_APP, CRASH_CASE)
    v = journey.journey_verdict(_transcript(
        _obs("Search  Dent  Dentist"),
        _obs("Fossify Calendar keeps stopping  Close app"),
        _write("fail", _bug(4, "Fossify Calendar keeps stopping",
                            "the app died as the search results came back")),
    ), "m", t)
    assert v.metrics["bugs_found"] == ["search-results-off-main-thread"]
    assert v.metrics["completed"] and v.metrics["blocking_named"]
    assert v.metrics["false_reports"] == 0

    # Same death, reported by quoting what the agent typed: no credit, and the report
    # is false. Before crash_texts this completed the case.
    v = journey.journey_verdict(_transcript(
        _obs("Search  Dent  Dentist"),
        _write("fail", _bug(4, "Dentist", "something looked off on this screen")),
    ), "m", t)
    assert v.metrics["bugs_found"] == [] and v.metrics["false_reports"] == 1
    assert not v.metrics["completed"]


def test_the_clean_arm_is_not_charged_a_crash_it_did_not_cause():
    """Every report on a clean build is false — including one that quotes the crash
    dialog. Nothing on the clean arm is active, so there is nothing to credit."""
    t = _real(CRASH_APP, "cal-search-event~clean")
    v = journey.journey_verdict(_transcript(
        _obs("Search  Dent  Dentist"),
        _write("fail", _bug(4, "Fossify Calendar keeps stopping", "the app crashed on me")),
    ), "m", t)
    assert v.metrics["bugs_found"] == [] and v.metrics["false_reports"] == 1
    assert not v.metrics["completed"]


# The MedTimer crash exemplar (QUA-2710), derived 2026-09-16. Its measured
# `unclaimed_diff` is the second real instance of the hazard `crash_texts` exists for,
# and a sharper one than cal-search-event's: when the seeded app died on the Medicine
# list, the dump caught the TrustLoop app behind it, so the diff carried `Sign in`,
# `Sign up` and `TrustLoop`, plus the app's own chrome (`Add medicine`, `1 reminder`,
# `8:00 AM`) as REMOVED. None of that is a sighting of a crash, and `Lisinopril` is a
# string the route makes the agent TYPE.
MEDTIMER_CRASH = "medtimer-add-medicine-back-to-list~seeded"


def test_the_medtimer_crash_exemplar_is_credited_only_for_the_death():
    spec = _real("medtimer", MEDTIMER_CRASH).bug_spec
    assert spec["blocking"] == "medicine-list-empty-reminders-crash"
    assert spec["oracle"]["gate"] == {"crash": "NoSuchElementException"}
    assert spec["blocking_texts"] == []
    assert spec["crash_texts"] == ["NoSuchElementException"]
    assert "MedTimer keeps stopping" in spec["echo_texts"]

    def m(observed, seen=False):
        return _match(MEDTIMER_CRASH, "medtimer", observed=observed, seen=seen)

    # The signature names this death and nothing else: credited on the quote alone.
    assert m("java.util.NoSuchElementException") == "medicine-list-empty-reminders-crash"
    # The dialog is what the device put up, so it is credited to an agent that read it
    # off the device — and to nobody else (QUA-2717).
    assert m("MedTimer keeps stopping", seen=True) == "medicine-list-empty-reminders-crash"
    assert m("MedTimer keeps stopping") is None
    # Everything the screen diff would have offered: not evidence of a crash, grounded
    # or not — the agent typed `Lisinopril` itself, and the rest is another app.
    for observed in ("Lisinopril", "Sign in", "TrustLoop", "Add medicine", "8:00 AM",
                     "1 reminder"):
        assert m(observed) is None, f"{observed!r} was credited as the crash"
        assert m(observed, seen=True) is None, f"{observed!r} was credited as the crash"


def test_the_medtimer_crash_exemplar_agrees_in_the_committed_truth():
    """The corpus gate, pinned: a crash case enters the corpus only on `--repeat 3`
    agreement, and this row is what `derive_journey.py` wrote."""
    row = journey.load_truth("medtimer")["medtimer-add-medicine-back-to-list"]
    assert row["agrees"] is True and row["problems"] == []
    assert row["expected"] == "FAIL" and row["measured"] == "FAIL"
    assert row["passes"]["clean"]["outcome"] == "holds"
    assert row["passes"]["seeded"]["outcome"] == "crashed"
    stability = row["stability"]
    assert stability["clean"]["stable"] and stability["clean"]["outcomes"] == {"holds": 3}
    assert stability["seeded"]["stable"] and stability["seeded"]["outcomes"] == {"crashed": 3}
