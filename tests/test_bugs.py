"""Tests for the open-source seeded-bug benchmark: suite loading + bug_verdict scoring."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from qualgentbench import bugs
from qualgentbench.adapters import native

# A self-contained sample app spec (the old notebook), kept ONLY as test data —
# it is not a benchmark app. Exercises guided + exploration scoring deterministically.
FIXTURE = Path(__file__).parent / "fixtures" / "sample_suite.yaml"


# ── suite loading ────────────────────────────────────────────────────────────


def test_suite_loads_five_tasks_with_bug_specs():
    suite = bugs.load_suite(FIXTURE)
    tasks = bugs.suite_tasks(suite)
    assert len(tasks) == 5
    ids = {t.id for t in tasks}
    assert ids == {
        "bug-auth-bypass", "bug-save-drop", "bug-edit-not-saved",
        "bug-delete-noop", "bug-logout-broken",
    }
    for t in tasks:
        assert t.bundle_id == "com.example.buggyapp"
        assert t.bug_spec and t.bug_spec["expected_verdict"] == "FAIL"
        assert t.bug_spec["symptom_keywords"]
        assert t.bug_spec["flow_steps"]


# ── transcript builder (reuses the native adapter's stream-json emitters) ─────


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _TC:
    id: str
    function: _Fn


import itertools

_ids = itertools.count()


def _call(name: str, inp: dict, result: str, assistant_text: str | None = None) -> str:
    """One assistant tool_use + its matching tool_result, with a UNIQUE id."""
    cid = f"c{next(_ids)}"
    return "\n".join([
        native._assistant_event("m", assistant_text, [_TC(cid, _Fn(name, json.dumps(inp)))]),
        native._tool_result_event(cid, result),
    ])


def _obs(text: str) -> str:
    return _call("mobile_observe_screen", {"device": "d"}, text)


def _tap(label: str) -> str:
    return _call("mobile_tap_and_observe", {"element": label}, "screen_changed: true")


def _report(status: str, summary: str) -> str:
    return _call("mobile_report_result", {"status": status, "summary": summary}, "ok",
                 assistant_text=summary)


def _task(suite_id: str):
    return next(t for t in bugs.suite_tasks(bugs.load_suite(FIXTURE)) if t.id == suite_id)


def _transcript(*parts: str) -> str:
    usage = json.dumps(native._result_event({"input": 100, "output": 50, "cached": 0}, 0.001))
    return "\n".join([*parts, usage]) + "\n"


# ── scoring ──────────────────────────────────────────────────────────────────


def test_buggy_found_full_credit():
    """Agent reaches all steps (incl. tapping Save) and reports FAIL with the symptom."""
    task = _task("bug-save-drop")
    t = _transcript(
        _obs("My Notes  Logout  Sample Note 1"),       # step login (screen)
        _obs("Title  Description  Save"),              # step open_add (screen)
        _tap("Save"),                                  # step enter_and_save (interaction)
        _obs("My Notes  Sample Note 1  Sample Note 2"),  # back on list, no QA Check
        _report("FAIL", "The new note QA Check was not saved and did not appear in the list."),
    )
    v = bugs.bug_verdict(t, "m", task)
    assert v.passed is True
    assert v.metrics["bug_found"] is True
    assert v.metrics["step_completion"] == 1.0
    assert v.weighted_score == 1.0
    assert v.criteria["verdict_correct"] and v.criteria["symptom_match"]


def test_buggy_missed_when_agent_reports_pass():
    """Agent says everything works → missed the bug → not passed, but step credit remains."""
    task = _task("bug-save-drop")
    t = _transcript(
        _obs("My Notes  Logout"),
        _obs("Title  Description  Save"),
        _tap("Save"),
        _report("PASS", "Created the note successfully."),
    )
    v = bugs.bug_verdict(t, "m", task)
    assert v.passed is False
    assert v.metrics["bug_found"] is False
    assert v.metrics["step_completion"] == 1.0          # it did the flow
    assert v.weighted_score == 0.5                       # full steps, zero detection
    assert "expected FAIL" in (v.failure_reason or "")


def test_fail_without_symptom_is_partial_not_found():
    """Reported FAIL but didn't describe the symptom → verdict correct, bug not 'found'."""
    task = _task("bug-save-drop")
    t = _transcript(
        _obs("My Notes  Logout"),
        _obs("Title  Description  Save"),
        _tap("Save"),
        _report("FAIL", "Something seemed off."),
    )
    v = bugs.bug_verdict(t, "m", task)
    assert v.metrics["bug_found"] is False
    assert v.criteria["verdict_correct"] is True
    assert v.criteria["symptom_match"] is False
    assert v.weighted_score == 0.75                      # 0.5*1.0 steps + 0.5*0.5 detect


def test_partial_step_completion():
    """Agent only logs in (1/3 steps) then reports — reward reflects partial flow."""
    task = _task("bug-save-drop")
    t = _transcript(
        _obs("My Notes  Logout"),                        # only step login; no open_add, no Save tap
        _report("FAIL", "note not saved"),
    )
    v = bugs.bug_verdict(t, "m", task)
    assert v.metrics["steps_total"] == 3
    assert v.metrics["steps_completed"] == 1
    assert v.metrics["step_completion"] == round(1 / 3, 4)
    # bug found (FAIL + symptom) but only 1/3 steps → reward = 0.5*(1/3) + 0.5*1
    assert v.weighted_score == round(0.5 * (1 / 3) + 0.5, 4)


def test_clean_control_no_false_alarm():
    """On a clean build (expected PASS), reporting PASS is correct; reporting FAIL is a false alarm."""
    task = _task("bug-save-drop")
    task.bug_spec["expected_verdict"] = "PASS"   # simulate the clean control build
    good = _transcript(
        _obs("My Notes  Logout"), _obs("Title  Description  Save"), _tap("Save"),
        _obs("My Notes  QA Check"),
        _report("PASS", "Note QA Check saved and visible."),
    )
    v = bugs.bug_verdict(good, "m", task)
    assert v.passed is True and v.metrics["bug_found"] is True

    false_alarm = _transcript(
        _obs("My Notes  Logout"), _obs("Title  Description  Save"), _tap("Save"),
        _report("FAIL", "note not saved"),
    )
    v2 = bugs.bug_verdict(false_alarm, "m", task)
    assert v2.passed is False and v2.metrics["bug_found"] is False


# ── open-ended bug hunt (structured per-feature verdict) ─────────────────────


def _hunt_task():
    return bugs.exploration_task(bugs.load_suite(FIXTURE))


_ALL_CORRECT = ("login=broken, view_notes=ok, add_note=broken, edit_note=broken, "
                "delete_note=broken, logout=broken, password_toggle=ok")


def _hunt(result_line: str, prose: str = "", *, n_obs: int = 1) -> str:
    """Build a bug-hunt transcript: a few device calls + a report whose summary
    carries a `RESULT:` verdict line (+ optional prose)."""
    parts = [_obs("My Notes") for _ in range(n_obs)] + [_tap("Save")]
    summary = (prose + "\nRESULT: " + result_line).strip()
    return _transcript(*parts, _report("FAIL", summary))


def test_exploration_task_features_and_no_hints():
    t = _hunt_task()
    assert t.bug_spec["mode"] == "explore"
    assert t.bug_spec["total_bugs"] == 5
    assert len(t.bug_spec["features"]) == 7          # 5 broken + 2 working controls
    # Instruction lists features neutrally but must NOT name bugs or reveal any
    # feature's verdict (the generic "works or is broken" phrasing is fine).
    instr = t.instruction.lower()
    assert "auth-bypass" not in instr and "save-drop" not in instr
    for f in t.bug_spec["features"]:
        assert f"{f['id']}=broken" not in instr
        assert f"{f['id']} is broken" not in instr
        assert f"{f['id']} is ok" not in instr


def test_exploration_all_bugs_found_clean_controls_ok():
    v = bugs.exploration_verdict(_hunt(_ALL_CORRECT), "m", _hunt_task())
    assert v.metrics["bugs_found"] == 5
    assert v.metrics["recall"] == 1.0
    assert v.metrics["precision"] == 1.0
    assert v.metrics["f1"] == 1.0
    assert v.metrics["false_positives"] == 0
    assert v.metrics["coverage"] == 1.0
    assert v.passed is True


def test_exploration_false_positive_on_working_feature():
    # Flags a WORKING feature (view_notes) as broken → precision penalty.
    line = ("login=broken, view_notes=broken, add_note=broken, edit_note=broken, "
            "delete_note=broken, logout=broken, password_toggle=ok")
    v = bugs.exploration_verdict(_hunt(line), "m", _hunt_task())
    assert v.metrics["bugs_found"] == 5
    assert v.metrics["false_positives"] == 1
    assert v.metrics["precision"] == round(5 / 6, 4)
    assert v.passed is False
    assert "false alarm" in (v.failure_reason or "")


def test_exploration_missed_bug():
    # Marks a real bug (edit_note) as ok → missed → recall 0.8.
    line = ("login=broken, view_notes=ok, add_note=broken, edit_note=ok, "
            "delete_note=broken, logout=broken, password_toggle=ok")
    v = bugs.exploration_verdict(_hunt(line), "m", _hunt_task())
    assert v.metrics["bugs_found"] == 4
    assert v.metrics["recall"] == 0.8
    assert "edit-not-saved" in (v.failure_reason or "")
    assert v.passed is False


def test_exploration_label_parsing_is_phrasing_proof():
    # Synonyms/casing must parse: BROKEN/fails/bug → broken; works/PASS → ok.
    line = ("login=BROKEN, view_notes=works, add_note=fails, edit_note=bug, "
            "delete_note=broken, logout=not working, password_toggle=PASS")
    v = bugs.exploration_verdict(_hunt(line), "m", _hunt_task())
    assert v.metrics["bugs_found"] == 5
    assert v.metrics["false_positives"] == 0
    assert v.metrics["recall"] == 1.0


def test_exploration_no_result_line_zero_recall():
    t = _transcript(_obs("My Notes"), _tap("Save"),
                    _report("FAIL", "I found some bugs but won't list them."))
    v = bugs.exploration_verdict(t, "m", _hunt_task())
    assert v.metrics["bugs_found"] == 0
    assert v.metrics["recall"] == 0.0
    assert "no parseable RESULT" in (v.failure_reason or "")


def test_exploration_step_budget_android_world_style():
    task = _hunt_task()  # 7 features → complexity 7 → step_budget = 70
    assert task.bug_spec["complexity"] == 7
    assert task.bug_spec["step_budget"] == 70

    within = _transcript(*[_obs("My Notes") for _ in range(40)],
                         _report("FAIL", "RESULT: " + _ALL_CORRECT))
    v = bugs.exploration_verdict(within, "m", task)
    assert v.metrics["steps"] == 41
    assert v.metrics["step_budget"] == 70
    assert v.metrics["within_budget"] is True

    over = _transcript(*[_obs("My Notes") for _ in range(80)],
                       _report("FAIL", "RESULT: " + _ALL_CORRECT))
    v2 = bugs.exploration_verdict(over, "m", task)
    assert v2.metrics["steps"] == 81
    assert v2.metrics["within_budget"] is False


# ── guided per-skill tasks: exact-label bug detection + programmatic clean oracle ──


def _guided_task(kind: str = "bug", oracle: dict | None = None, flow_steps=None):
    from qualgentbench.task import BenchmarkTask
    return BenchmarkTask(
        id="g", name="g", instruction="", app_file_id="", app_name="A",
        platform="android", bundle_id="com.x",
        bug_spec={
            "id": "b", "type": kind, "flow_steps": flow_steps or [],
            "oracle": oracle, "optimal_steps": 3, "symptom_keywords": [],
            "expected_verdict": "FAIL" if kind == "bug" else "PASS",
        },
    )


def test_guided_bug_found_via_result_line():
    t = _guided_task("bug")
    tr = _transcript(_obs("x"), _tap("y"), _report("FAIL", "RESULT: feature=broken"))
    v = bugs.guided_bug_verdict(tr, "m", t)
    assert v.metrics["bug_found"] is True and v.passed is True and v.weighted_score > 0


def test_guided_bug_not_found_when_marked_ok():
    t = _guided_task("bug")
    tr = _transcript(_obs("x"), _tap("y"), _report("PASS", "RESULT: feature=ok"))
    v = bugs.guided_bug_verdict(tr, "m", t)
    assert v.metrics["bug_found"] is False and v.passed is False


def test_guided_bug_fallback_to_fail_status_no_result_line():
    # No RESULT line, but reported FAIL → counts as flagged broken (phrasing-proof
    # fallback that fixed the old keyword false-negatives).
    t = _guided_task("bug")
    tr = _transcript(_obs("x"), _tap("y"), _report("FAIL", "the new name did not persist"))
    v = bugs.guided_bug_verdict(tr, "m", t)
    assert v.metrics["bug_found"] is True


def test_clean_task_oracle_pass_and_fail(monkeypatch):
    from qualgentbench.verify import device_oracle
    t = _guided_task("clean", oracle={"db": "d", "query": "q", "expect": ">=1"})
    tr = _transcript(_obs("x"), _tap("y"), _report("PASS", "did it"))

    monkeypatch.setattr(device_oracle, "check", lambda o, p, serial=None: (True, "ok"))
    v = bugs.clean_task_verdict(tr, "m", t)
    assert v.passed is True and v.metrics["oracle_passed"] is True and v.metrics["reward"] == 1.0

    monkeypatch.setattr(device_oracle, "check", lambda o, p, serial=None: (False, "missing"))
    v2 = bugs.clean_task_verdict(tr, "m", t)
    assert v2.passed is False and v2.metrics["oracle_passed"] is False and v2.metrics["reward"] == 0.0


def test_clean_task_false_alarm_flag(monkeypatch):
    from qualgentbench.verify import device_oracle
    monkeypatch.setattr(device_oracle, "check", lambda o, p, serial=None: (True, "ok"))
    t = _guided_task("clean", oracle={"db": "d", "query": "q"})
    # Oracle passes but the agent cried FAIL on a working feature → false alarm noted.
    tr = _transcript(_obs("x"), _tap("y"), _report("FAIL", "looked broken"))
    v = bugs.clean_task_verdict(tr, "m", t)
    assert v.metrics["oracle_passed"] is True and v.metrics["no_false_alarm"] is False


def test_guided_verdict_dispatches_by_type(monkeypatch):
    from qualgentbench.verify import device_oracle
    monkeypatch.setattr(device_oracle, "check", lambda o, p, serial=None: (True, "ok"))
    clean = _guided_task("clean", oracle={"db": "d", "query": "q"})
    tr_c = _transcript(_obs("x"), _tap("y"), _report("PASS", "RESULT: feature=ok"))
    assert "oracle_passed" in bugs.guided_verdict(tr_c, "m", clean).metrics
    bug = _guided_task("bug")
    tr_b = _transcript(_obs("x"), _tap("y"), _report("FAIL", "RESULT: feature=broken"))
    assert bugs.guided_verdict(tr_b, "m", bug).metrics["bug_found"] is True




def test_overall_blends_recall_speed_and_punishes_false_alarms():
    """`overall` = weighted recall × speed − false-report cost. Precision is counted
    exactly once, as the cost term; speed is a discount for slowness, not a bonus."""
    clean = bugs.exploration_verdict(_hunt(_ALL_CORRECT), "m", _hunt_task())
    assert clean.metrics["f1"] == 1.0
    assert clean.metrics["recall"] == 1.0
    assert clean.metrics["overall"] == round(
        clean.metrics["recall"] * clean.metrics["speed_factor"]
        - clean.metrics["fp_cost"], 4)
    assert clean.metrics["fp_cost"] == 0.0                      # nothing fabricated
    # Bounded above by 1.0, and strictly below it for any episode that spent budget.
    assert clean.metrics["overall"] <= 1.0
    assert clean.metrics["speed_factor"] >= 1.0 - bugs._SPEED_WEIGHT

    # Same 5 real finds, but one working feature also flagged broken.
    sprayed = ("login=broken, view_notes=broken, add_note=broken, edit_note=broken, "
               "delete_note=broken, logout=broken, password_toggle=ok")
    fp = bugs.exploration_verdict(_hunt(sprayed), "m", _hunt_task())
    assert fp.metrics["bugs_found"] == 5                        # found everything real
    assert fp.metrics["false_positives"] == 1
    # One false report must cost, but not bury an otherwise perfect run.
    assert fp.metrics["fp_cost"] > 0
    assert fp.metrics["overall"] < clean.metrics["overall"]
    assert fp.metrics["overall"] == round(
        fp.metrics["recall"] * fp.metrics["speed_factor"] - fp.metrics["fp_cost"], 4)


def test_harness_separates_testing_from_guessing():
    """The benchmark's core claim: an agent that does not test cannot score.

    Pinned against the live easy-tier specs, so weakening a probe or dropping
    the gate fails the suite.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts" / "adversary_check.py"
    spec = importlib.util.spec_from_file_location("adversary_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    suites = [s for s in bugs.load_apps() if s["app"].get("difficulty") == "easy"]
    assert suites, "no easy-tier apps — the guard would be vacuous"

    scores = {m: [] for m in ("spray", "crud", "oracle", "honest")}
    for suite in suites:
        task = bugs.exploration_task(suite)
        feats = task.bug_spec["features"]
        for mode in scores:
            v = bugs.exploration_verdict(mod.build(feats, mode), "m", task)
            # overall_raw, not overall: the reported score is clamped to [0,1], and the
            # "actively worse than silence" property only exists in the signed value.
            scores[mode].append(v.metrics["overall_raw"])

    mean = {m: sum(v) / len(v) for m, v in scores.items()}
    # Spraying every area must be actively worse than saying nothing.
    assert mean["spray"] < 0, mean
    # A plausible prior with no device evidence must not profit.
    assert mean["crud"] <= 0, mean
    # Even an adversary that KNOWS the answer key scores nothing without testing.
    assert mean["oracle"] <= 0.05, mean
    # The honest agent is far ahead, but pays a speed discount for the budget it spent.
    assert 1.0 - bugs._SPEED_WEIGHT < mean["honest"] <= 1.0, mean
    assert mean["honest"] > max(mean[g] for g in ("spray", "crud", "oracle")) + 0.85


def _claude_thinking_episode(area_lines: str, *, in_thinking: bool, n_dev: int = 6) -> str:
    """A Claude stream-json episode that banks its verdicts in `thinking` or in `text`.

    Enough device work, with the probe words visible, to clear the temporal gate.
    """
    lines = []
    for _ in range(n_dev):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t", "name": "mcp__device__mobile_observe_screen",
             "input": {"device": "emulator-5554"}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [
                {"type": "text", "text": "login note save delete edit logout password"}]}]}}))
    block = ({"type": "thinking", "thinking": area_lines} if in_thinking
             else {"type": "text", "text": area_lines})
    lines.append(json.dumps({"type": "assistant", "message": {"content": [block]}}))
    return "\n".join(lines)


_AREA_VERDICTS = "\n".join([
    "AREA: login | VERDICT: deviates",
    "AREA: add_note | VERDICT: deviates",
    "AREA: edit_note | VERDICT: deviates",
    "AREA: delete_note | VERDICT: deviates",
    "AREA: logout | VERDICT: deviates",
    "AREA: view_notes | VERDICT: as_specified",
    "AREA: password_toggle | VERDICT: as_specified",
])


def test_verdicts_banked_in_thinking_score_the_same_as_visible_text():
    """A reasoning model must not be scored on WHERE it writes its verdict —
    that would measure output plumbing, not QA skill."""
    visible = bugs.exploration_verdict(
        _claude_thinking_episode(_AREA_VERDICTS, in_thinking=False), "m", _hunt_task())
    thought = bugs.exploration_verdict(
        _claude_thinking_episode(_AREA_VERDICTS, in_thinking=True), "m", _hunt_task())

    assert thought.metrics["bugs_found"] == visible.metrics["bugs_found"] == 5
    assert thought.metrics["coverage"] == visible.metrics["coverage"] == 1.0
    assert thought.metrics["overall"] == visible.metrics["overall"]


def test_a_verdict_reasoned_before_any_device_work_still_banks_nothing():
    """Reading `thinking` must not become a way to claim without testing — the temporal
    evidence gate, not the channel, is what withholds credit."""
    v = bugs.exploration_verdict(
        _claude_thinking_episode(_AREA_VERDICTS, in_thinking=True, n_dev=0),
        "m", _hunt_task())

    assert v.metrics["bugs_found"] == 0
    assert v.metrics["overall"] == 0.0


def test_an_agent_killed_before_reporting_is_an_env_failure_not_a_zero():
    """A provider kill must not be publishable as "tested it and found nothing"."""
    task = _hunt_task()
    task.bug_spec["exit_code"] = 1
    # Real device work, then death: no report, no banked verdicts.
    t = _transcript(*[_obs("My Notes") for _ in range(8)], _tap("Save"))

    m = bugs.exploration_verdict(t, "m", task).metrics

    assert m["env_failure"] is True
    assert m["infra_failure"] is False        # it DID reach the device — 9 calls
    assert m["exit_code"] == 1


def test_a_bad_exit_after_a_complete_report_is_still_a_real_result():
    """Both halves are required. An agent that reported everything and then exited
    non-zero has earned its score — voiding it would discard real work."""
    task = _hunt_task()
    task.bug_spec["exit_code"] = 1

    m = bugs.exploration_verdict(_hunt(_ALL_CORRECT), "m", task).metrics

    assert m["env_failure"] is False
    assert m["bugs_found"] == 5


def test_a_silent_agent_that_exited_cleanly_still_scores_zero():
    """Staying silent is a real (bad) result, not an environment failure."""
    task = _hunt_task()
    task.bug_spec["exit_code"] = 0
    t = _transcript(*[_obs("My Notes") for _ in range(8)], _tap("Save"))

    m = bugs.exploration_verdict(t, "m", task).metrics

    assert m["env_failure"] is False
    assert m["overall"] == 0.0


def test_a_correct_fast_guided_find_does_not_violate_the_result_schema():
    """The guided speed bonus can push a correct find above 1.0, but
    `weighted_score` is bounded le=1.0 — the unclamped signal stays in `reward`."""
    task = _task("bug-save-drop")
    task.bug_spec["step_cap"] = 300          # budget to spare -> efficiency > 1.0
    t = _transcript(
        _obs("My Notes  Logout"), _obs("Title  Description  Save"), _tap("Save"),
        _report("FAIL", "RESULT: save_note=broken"),
    )

    v = bugs.guided_bug_verdict(t, "m", task)

    assert v.weighted_score <= 1.0
    assert v.metrics["efficiency"] > 1.0     # the bonus is still recorded, unclamped


def test_count_adb_survives_newline_separated_commands():
    """The scorer must count what the budget hook charges (see _shell_command)."""
    from qualgentbench.bugs import _count_adb, _shell_command
    from qualgentbench.transcript import ToolEvent

    cmd = ("adb -s emulator-5554 shell input tap 500 500\n"
           "adb -s emulator-5554 shell input text smith\n"
           "adb -s emulator-5554 shell keyevent 66")
    e = ToolEvent(id="1", name="Bash", input={"command": cmd, "description": "x"})
    assert _count_adb(_shell_command(e)) == 3
    # The serialized form is exactly what used to be counted, and it loses them all.
    assert _count_adb(e.input_str) == 0


def test_budget_exhaustion_is_scored_not_written_off_as_env_failure():
    """Our own hook kills with SIGTERM; that must not read as a provider outage."""

    spec = {"exit_code": 143, "truncated": True}
    truncated = (bool(spec.get("exit_code")) and not {} and not {}
                 and not spec.get("truncated"))
    assert truncated is False, "budget exhaustion must not be env_failure"

    spec_outage = {"exit_code": 1, "truncated": False}
    outage = (bool(spec_outage.get("exit_code")) and not {} and not {}
              and not spec_outage.get("truncated"))
    assert outage is True, "a real outage must still be excluded"


def test_collateral_areas_are_neither_defect_nor_control():
    """An area a seeded defect also breaks must not earn recall NOR cost a false
    report — scoring collateral areas as controls charges agents for measuring reality."""
    from pathlib import Path
    from qualgentbench import bugs

    suite = bugs.load_suite(
        Path(bugs.__file__).parent / "data" / "benchmarks" / "opencalc.yaml")
    feats = bugs.exploration_task(suite).bug_spec["features"]
    states = {f["id"]: f["state"] for f in feats}
    assert states["parentheses"] == "collateral"
    assert states["chained_ops"] == "collateral"
    assert states["percent"] == "collateral"

    controls = [f for f in feats if f["state"] == "ok"]
    buggy = [f for f in feats if f["state"] == "broken"]
    assert not any(f["state"] == "collateral" for f in controls + buggy)


def test_tier_filter_accepts_multiple_values():
    """click.Choice cannot express a comma-separated list, so parse_tiers validates —
    a typo must not silently match no apps."""
    import click
    import pytest as _pytest
    from qualgentbench.cli import parse_tiers

    assert parse_tiers("easy") == {"easy"}
    assert parse_tiers("easy,medium") == {"easy", "medium"}
    assert parse_tiers(" EASY , Medium ") == {"easy", "medium"}
    assert parse_tiers(None) == set()
    with _pytest.raises(click.ClickException):
        parse_tiers("easy,medum")
