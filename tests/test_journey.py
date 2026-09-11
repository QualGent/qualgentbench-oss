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
    tasks = _app("openscale")
    versions = {}
    for t in tasks:
        versions.setdefault(t.bug_spec["case_id"], []).append(t.bug_spec["version"])
    assert all(v == ["clean", "seeded"] for v in versions.values()), versions
    add = next(t for t in tasks if t.id == "openscale-add-measurement~seeded")
    assert add.bug_spec["expected"] == "PASS" and add.bug_spec["active_bugs"] == ["overview-title-typo"]
    assert add.bug_spec["oracle"]["mode"] == "db"
    delete = next(t for t in tasks if t.id == "openscale-delete-measurement~seeded")
    assert delete.bug_spec["expected"] == "FAIL"
    assert delete.bug_spec["blocking"] == "measurement-delete-broken"
    assert delete.bug_spec["active_bugs"] == ["measurement-delete-broken"]
    clean = next(t for t in tasks if t.id == "openscale-delete-measurement~clean")
    assert clean.bug_spec["expected"] == "PASS" and clean.bug_spec["active_bugs"] == []
    stats = next(t for t in tasks if t.id == "openscale-statistics-range~seeded")
    assert stats.bug_spec["expected"] == "PASS"
    assert [s["bug"] for s in stats.bug_spec["side"]] == ["stats-average-drops-latest"]
    assert stats.bug_spec["side"][0]["texts"]                          # measured, not authored
    assert stats.bug_spec["oracle"] == {"mode": "present", "expect": {"present": "Max: 85 kg"},
                                        "evidence": ["Max: 85 kg"]}


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
    tasks = _app("openscale")
    clean = next(t for t in tasks if t.id == "openscale-delete-measurement~clean")
    seeded = next(t for t in tasks if t.id == "openscale-delete-measurement~seeded")
    a, b = journey.brief(clean, "e", "raw"), journey.brief(seeded, "e", "raw")
    assert a == b
    assert "QGB-CANARY" not in a and "measurement-delete" not in a and "Preconditions" not in a
    assert "1. Open the app to the Overview." in a and "Expected outcome:" in a


def test_task_id_round_trip():
    assert journey.split_task_id("mmex-deposit-paycheck~clean") == ("mmex-deposit-paycheck", "clean")
    assert journey.split_task_id("mmex-deposit-paycheck") == ("mmex-deposit-paycheck", "seeded")


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


def _match(tid: str, app_id: str, observed: str = "", description: str = "", screen: str = ""):
    """Run one synthetic report through the matcher against the REAL spec for `tid`."""
    report = journey.BugReport(step=1, screen=screen, observed=observed, expected="",
                               description=description)
    return journey.match_report(report, _real(app_id, tid).bug_spec)


# Seven probes that all earned credit from the live matcher before 2026-09-10: a
# one-character marker or derived blocking text matched as a bare SUBSTRING, and a
# single generic symptom word matched honest prose about an unrelated problem.
@pytest.mark.parametrize("tid,app_id,report,was", [
    # marker "2" inside an unrelated count
    ("anki-create-deck~seeded", "ankidroid", {"observed": "Total: 2 items"}, "deck-new-count-low"),
    # marker "1" inside an unrelated date
    ("tasks-complete-parent~seeded", "tasksorg", {"observed": "Due in 1 day"}, "subtask-chip-low"),
    # derived blocking text "A" (a contacts section index) — matched EVERY report, which
    # bought a fabricated report recall AND completion on a blocked case
    ("contacts-delete~seeded", "fossify-contacts", {"observed": "anything at all"},
     "contact-delete-broken"),
    # derived blocking texts "1", "3", "4" — bare digits out of the screen diff
    ("anki-add-note-to-deck~seeded", "ankidroid", {"observed": "3 cards due"},
     "note-added-to-default-deck"),
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


@pytest.mark.xfail(strict=True, reason="CORPUS: tasksorg's `due-date-edit-lost` lists the bare "
                                      "word `lost` as a symptom, which matches honest prose "
                                      "about any lost thing. The matcher cannot tell this from "
                                      "a real sighting of a functional defect (there is no "
                                      "screen string to check the prose against); the fix is in "
                                      "data/test-cases/tasksorg.yaml — delete this xfail when it "
                                      "lands.")
def test_a_single_generic_symptom_word_does_not_identify_a_functional_defect():
    assert _match("tasks-change-due-time~seeded", "tasksorg",
                  description="the task was lost in the list") is None


def test_real_markers_and_texts_still_match_on_token_boundaries():
    """The floor is 2 characters, so orgzly's `#B` priority marker is still evidence —
    and a measured text still matches when the agent quotes it."""
    assert _match("orgzly-create-priority-note~seeded", "orgzly",
                  observed="TODO  #B  Book flights") == "priority-letter-shifted"
    assert _match("medtimer-add-medicine~seeded", "medtimer",
                  observed="Aspirin (9 left, 2026-09-10)") == "stock-left-display-low"
    assert _match("mmex-void-withdrawal~seeded", "moneymanagerex",
                  observed="Balance: $ 75.00") == "void-still-counted"
    assert _match("tasks-delete~seeded", "tasksorg",
                  observed="Call dentist") == "task-delete-broken"
    # Token boundaries, not substrings: the marker inside a longer number is not a hit.
    assert _match("orgzly-create-priority-note~seeded", "orgzly", observed="#BC  Book flights") is None


def test_derived_blocking_texts_drop_what_cannot_be_evidence():
    """`unclaimed_diff` is every string that differed between the two screens, junk
    included. Filtering happens where the evidence is built, not at match time."""
    contacts = _real("fossify-contacts", "contacts-delete~seeded").bug_spec["blocking_texts"]
    assert "A" not in contacts and "Alice" in contacts and "No contacts found" in contacts
    anki = _real("ankidroid", "anki-add-note-to-deck~seeded").bug_spec["blocking_texts"]
    assert anki == ["Default"]                      # "1", "3", "4" were not evidence
    orgzly = _real("orgzly", "orgzly-complete-deadline-task~seeded").bug_spec["blocking_texts"]
    assert "4:32 PM" in orgzly
    assert all(len(t.strip()) >= 2 for app, tid in [("fossify-contacts", "contacts-delete~seeded"),
                                                    ("moneymanagerex", "mmex-void-withdrawal~seeded")]
               for t in _real(app, tid).bug_spec["blocking_texts"])


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
    monkeypatch.delenv("QUALGENTBENCH_APK_OPENSCALE", raising=False)
    meta = journey.apk_meta("openscale")
    assert meta and meta["filename"] == "journey/openscale-buggy.apk" and len(meta["sha256"]) == 64
    app = {"id": "openscale"}
    spec = {"apk": {"filename": "hard/openscale-buggy.apk", "sha256": "x"}}
    hunt = preflight.resolve_apk_offline(app, spec, mode="hunt")
    jour = preflight.resolve_apk_offline(app, spec, mode="journey")
    assert str(hunt).endswith("seeded/openscale/openscale-buggy.apk")
    assert str(jour).endswith("journey/openscale/openscale-buggy.apk")


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
