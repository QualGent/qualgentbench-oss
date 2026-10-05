"""CreateBench stage C: the authored-case grader (QUA-2857).

Synthetic runner outputs only — no device, no agent. The live counterpart is
`python -m qualgentbench.create.grader run`; its manifests rescore through the same
`grade()` these tests pin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_bugs import _call, _obs, _transcript

from qualgentbench import bugs, journey
from qualgentbench.create import grader
from qualgentbench.result import RunResult

STUDY = "anki-study-first-card"          # target reviewer-show-answer-crash: functional, canary
BROWSE = "anki-browse-cards"             # target browser-count-low: display
# Since QUA-2860 every journey defect carries a canary, so the "target with no canary"
# path is simulated: `_uncover` hides browser-count-low's canary from the grader.


@pytest.fixture(autouse=True)
def _one_underived_case(monkeypatch):
    """QUA-2854 derived controls for all 41 cases; these tests need a case WITHOUT them
    (the not-gradable path), so that one row is served with the two keys stripped."""
    real = journey.load_truth

    def load_truth(app_id):
        truth = real(app_id)
        case = "medtimer-add-medicine-back-to-list"
        if case in truth:
            truth = {**truth, case: {k: v for k, v in truth[case].items()
                                     if k not in (journey.CONTROLS_KEY,
                                                  journey.CONTROL_DERIVATION_KEY)}}
        return truth
    monkeypatch.setattr(journey, "load_truth", load_truth)


def _uncover(monkeypatch, bug="browser-count-low"):
    real = grader.canary_ids
    monkeypatch.setattr(grader, "canary_ids", lambda app_id: real(app_id) - {bug})
DECK = "anki-create-deck"                # no bugs: power n/a
UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _artifact(steps=None, name="Study the first Spanish card", expected="The next card appears."):
    steps = steps or [
        {"description": "Open the app", "kind": "setup"},
        {"description": "Tap \"Spanish\"", "kind": "act"},
        {"description": "Tap \"Show answer\"", "kind": "act"},
        {"description": "Verify the answer \"one\" is shown", "kind": "verify"},
    ]
    case = {"name": name, "steps": steps, "expected_result": expected, "priority": "High"}
    from qualgentbench.create.fake_api import serialize_steps
    return {"schema": "qualgentbench.create.authored_case/1", "test_case_id": "tc-1",
            "request": dict(case), "updates": [], "case": case,
            "serialized_steps": serialize_steps(steps)}


def _suite(app_id="ankidroid"):
    return next(s for s in bugs.load_apps() if s["app"]["id"] == app_id)


def _write(verdict, bugs_yaml=""):
    body = f"verdict: {verdict}\nbugs:{bugs_yaml or ' []'}\n"
    return _call("Write", {"file_path": "/w/findings.yaml", "content": body}, "ok")


def _bug(step, observed, description):
    return f'\n  - step: {step}\n    observed: "{observed}"\n    description: "{description}"'


# ── the runner case ───────────────────────────────────────────────────────────

def test_markers_are_stripped_so_authored_and_reference_read_alike():
    assert grader.strip_markers("[act] Tap \"OK\"") == "Tap \"OK\""
    assert grader.strip_markers(f"Sign in as the test user ## {{{UUID}}}") == "Sign in as the test user"
    assert grader.strip_markers(f"[setup]   Sign in ## {UUID}  ") == "Sign in"
    # A bracket that is not a step kind is the author's text, kept.
    assert grader.strip_markers("[Spanish] deck") == "[Spanish] deck"


def test_the_case_as_it_stands_is_converted_and_the_serialization_kept():
    art = _artifact()
    art["case"]["steps"][1] = {"description": f"[act] Tap \"Spanish\" ## {{{UUID}}}",
                               "kind": "act", "credential_id": UUID}
    rc = grader.runner_case(art)
    assert rc.steps == ["Open the app", "Tap \"Spanish\"", "Tap \"Show answer\"",
                        "Verify the answer \"one\" is shown"]
    assert rc.expected_outcome == "The next card appears." and rc.source == "authored"
    assert rc.serialized_steps.startswith("1. [setup] Open the app")
    # A stored case whose steps are the product's numbered string converts the same way.
    stored = {"name": rc.name, "expected_result": rc.expected_outcome,
              "steps": f"1. [setup] Open the app\n2. [act] Tap \"Spanish\" ## {{{UUID}}}"}
    assert grader.runner_case(stored).steps == ["Open the app", "Tap \"Spanish\""]
    # A bare capture of the POST (the QUA-2851 spike's `captures/01.json` shape).
    assert grader.runner_case({"request": art["request"]}).steps[0] == "Open the app"


def test_a_missing_artifact_is_no_case_created_and_fails_every_axis(tmp_path):
    case, why = grader.load_artifact(tmp_path)                 # an episode dir with no case
    assert case is None and "no authored case" in why
    plan = grader.plan_grade(case, STUDY, why_no_case=why)
    g = grader.grade(plan, {})
    assert g["status"] == grader.NO_CASE and plan.runs == []
    assert all(v is False for v in g["axes"].values())


# ── the plan ──────────────────────────────────────────────────────────────────

def test_plan_is_three_clean_one_target_one_control_with_a_case_derived_budget():
    plan = grader.plan_grade(grader.runner_case(_artifact()), STUDY)
    assert plan.status == grader.GRADED and plan.app_id == "ankidroid"
    assert [r.key for r in plan.runs] == ["clean-1", "target-1", "clean-2", "control-1", "clean-3"]
    roles = {r.key: r.active_bugs for r in plan.runs}
    assert roles["clean-1"] == roles["clean-2"] == roles["clean-3"] == []
    assert roles["target-1"] == ["reviewer-show-answer-crash"]
    assert plan.canary_covered == ["reviewer-show-answer-crash"]
    # The control is QUA-2854's, rotated by the trial index.
    row = journey.load_truth("ankidroid")[STUDY]
    for t in range(4):
        p = grader.plan_grade(grader.runner_case(_artifact()), STUDY, control_trial=t)
        assert p.control["control"] == journey.control_for_trial(row, t)["control"]
        assert p.runs[3].active_bugs == [p.control["control"]]
    # Budget from the AUTHORED case (P7), never the corpus case's 35.
    assert plan.step_budget == grader.step_budget(4) == grader.BUDGET_MIN
    many = [{"description": f"Tap \"Row {i}\"", "kind": "act"} for i in range(15)]
    assert grader.plan_grade(grader.runner_case(_artifact(many)), STUDY).step_budget == 80
    assert grader.step_budget(40) == grader.BUDGET_MAX


def test_a_case_with_no_bugs_has_no_target_run_and_power_is_na():
    plan = grader.plan_grade(grader.reference_case("ankidroid", DECK), DECK)
    assert [r.role for r in plan.runs] == ["clean", "clean", "control", "clean"]
    g = grader.grade(plan, {r.key: {"reported_verdict": "pass"} for r in plan.runs},
                     lint={"ok": True})
    assert g["axes"]["power"] == grader.NA and g["axes"]["strong"] == grader.NA


def test_underived_and_unknown_cases_are_not_gradable_never_a_crash_or_a_pass():
    plan = grader.plan_grade(grader.runner_case(_artifact()), "medtimer-add-medicine-back-to-list")
    assert plan.status == grader.NOT_GRADABLE and plan.reason.startswith(grader.CONTROLS_NOT_DERIVED)
    assert plan.runs == []
    g = grader.grade(plan, {})
    assert all(v is None for v in g["axes"].values())
    unknown = grader.plan_grade(grader.runner_case(_artifact()), "no-such-case")
    assert unknown.status == grader.NOT_GRADABLE and unknown.reason.startswith(grader.UNKNOWN_CASE)
    s = grader.summarize([g, grader.grade(unknown, {})])
    assert s["graded"] == 0 and s["not_gradable"] == {"controls_not_derived": 1, "unknown_case": 1}
    assert s["strong"]["rate_n"] == 0 and s["strong"]["rate_rate"] is None


def test_case_with_no_eligible_control_has_specificity_na(monkeypatch):
    real = journey.load_truth

    def no_controls(app_id):
        t = json.loads(json.dumps(real(app_id)))
        t[STUDY]["create_controls"] = []
        return t
    monkeypatch.setattr(journey, "load_truth", no_controls)
    plan = grader.plan_grade(grader.runner_case(_artifact()), STUDY)
    assert "control" not in [r.role for r in plan.runs]
    ok = {r.key: {"reported_verdict": "pass" if r.role == "clean" else "fail",
                  "fault_fired": ["reviewer-show-answer-crash"]} for r in plan.runs}
    g = grader.grade(plan, ok, lint={"ok": True})
    assert g["axes"]["specificity"] == grader.NA and g["axes"]["power"] is True
    assert g["axes"]["strong"] == grader.NA


def test_tasks_are_journey_shaped_blind_and_carry_no_authored_oracle():
    plan = grader.plan_grade(grader.runner_case(_artifact()), STUDY)
    tasks = {r.key: grader.build_task(plan, r, _suite()) for r in plan.runs}
    clean, target, control = tasks["clean-1"], tasks["target-1"], tasks["control-1"]
    # Blind: case + runner-case hash; target and control are both `seeded` to the agent.
    assert clean.id == f"{plan.grade_id}~clean" and target.id == control.id == f"{plan.grade_id}~seeded"
    from qualgentbench.episode_runner import agent_visible_task_id
    assert {agent_visible_task_id(t.id) for t in tasks.values()} == {plan.grade_id}
    for t in tasks.values():
        s = t.bug_spec
        assert s["mode"] == "journey" and s["case_id"] == STUDY      # staging/precondition: the brief case's
        assert s["steps"] == plan.case.steps and s["step_budget"] == plan.step_budget
        assert s["oracle"]["mode"] == "none"                          # P6: no authored literal bound
    assert target.bug_spec["active_bugs"] == ["reviewer-show-answer-crash"]
    assert target.bug_spec["blocking"] == "reviewer-show-answer-crash"
    assert target.bug_spec["crash_texts"] == ["NullPointerException"]
    assert control.bug_spec["active_bugs"] == [plan.control["control"]]
    assert control.bug_spec["expected"] == "PASS" and control.bug_spec["blocking"] is None
    assert clean.bug_spec["active_bugs"] == [] and clean.bug_spec["expected"] == "PASS"
    # The brief the runner reads is the journey brief over the authored steps, identical
    # across roles (so it cannot tell clean from target from control).
    briefs = {journey.brief(t, "emulator-5554", "mcp") for t in tasks.values()}
    assert len(briefs) == 1 and "Tap \"Show answer\"" in briefs.pop()


def test_the_reference_baseline_goes_through_the_identical_path():
    ref = grader.reference_case("ankidroid", STUDY)
    case = journey.load_cases("ankidroid")
    raw = next(c for c in case["test_cases"] if c["id"] == STUDY)
    assert ref.steps == raw["steps"] and ref.source == "reference"
    plan = grader.plan_grade(ref, STUDY)
    assert plan.step_budget == grader.step_budget(len(raw["steps"]))       # not the corpus 35
    task = grader.build_task(plan, plan.runs[0], _suite())
    assert task.bug_spec["oracle"]["mode"] == "none"
    # Reference cases fail HARD lint by construction (no kind tags, no launch step), so
    # the comparable column for the baseline row is `strong_exec`.
    assert grader.lint_artifact(plan)["ok"] is False


# ── scoring: every axis, attribution, exclusions ─────────────────────────────

def _plan(case_id=STUDY):
    return grader.plan_grade(grader.runner_case(_artifact()), case_id)


def _ok_runs(plan, **overrides):
    out = {}
    for r in plan.runs:
        if r.role == "target":
            out[r.key] = {"reported_verdict": "fail", "fault_fired": list(r.active_bugs),
                          "bugs_found": list(r.active_bugs)}
        else:
            out[r.key] = {"reported_verdict": "pass", "fault_fired": []}
    out.update(overrides)
    return out


def test_a_strong_test_needs_every_axis():
    plan = _plan()
    g = grader.grade(plan, _ok_runs(plan), lint={"ok": True})
    assert g["axes"] == {"lint": True, "repeatability": True, "specificity": True, "power": True,
                         "strong": True, "strong_exec": True}
    assert g["clean_passed"] == "3/3" and g["runs"]["target-1"]["attribution"] == "canary"


@pytest.mark.parametrize("key,metrics,axis", [
    ("clean-2", {"reported_verdict": "fail"}, "repeatability"),
    ("clean-3", {"reported_verdict": None}, "repeatability"),          # no verdict = not passed
    ("control-1", {"reported_verdict": "fail"}, "specificity"),
    ("target-1", {"reported_verdict": "pass", "fault_fired": ["reviewer-show-answer-crash"]}, "power"),
])
def test_one_failed_run_fails_its_axis_and_strong(key, metrics, axis):
    plan = _plan()
    g = grader.grade(plan, _ok_runs(plan, **{key: metrics}), lint={"ok": True})
    assert g["axes"][axis] is False and g["axes"]["strong"] is False
    assert all(v is True for k, v in g["axes"].items() if k not in (axis, "strong", "strong_exec"))


def test_power_attribution_canary_report_match_and_unattributed(monkeypatch):
    plan = _plan()
    # Canary-covered target: the canary decides, whatever the report says.
    fired_no_report = _ok_runs(plan, **{"target-1": {"reported_verdict": "fail",
                                                    "fault_fired": ["reviewer-show-answer-crash"],
                                                    "bugs_found": []}})
    r = grader.grade(plan, fired_no_report, lint={"ok": True})["runs"]["target-1"]
    assert r["outcome"] == grader.CAUGHT and r["attribution"] == "canary" and not r["report_matched"]
    # FAIL with the canary silent: the runner failed for some other reason. No credit,
    # even when the report text matched.
    silent = _ok_runs(plan, **{"target-1": {"reported_verdict": "fail", "fault_fired": [],
                                           "bugs_found": ["reviewer-show-answer-crash"]}})
    g = grader.grade(plan, silent, lint={"ok": True})
    assert g["runs"]["target-1"]["outcome"] == grader.UNATTRIBUTED and g["axes"]["power"] is False
    # Marker read failed (None): fall back to the report match, and say so.
    unread = _ok_runs(plan, **{"target-1": {"reported_verdict": "fail", "fault_fired": None,
                                           "bugs_found": ["reviewer-show-answer-crash"]}})
    r = grader.grade(plan, unread, lint={"ok": True})["runs"]["target-1"]
    assert r["outcome"] == grader.CAUGHT and r["attribution"] == "report_match (canary unread)"
    # No canary at all (browser-count-low, simulated): the report match is the attribution.
    _uncover(monkeypatch)
    bplan = _plan(BROWSE)
    assert bplan.canary_covered == []
    hit = _ok_runs(bplan, **{"target-1": {"reported_verdict": "fail", "fault_fired": [],
                                         "bugs_found": ["browser-count-low"]}})
    r = grader.grade(bplan, hit, lint={"ok": True})["runs"]["target-1"]
    assert r["outcome"] == grader.CAUGHT and r["attribution"] == "report_match"
    miss = _ok_runs(bplan, **{"target-1": {"reported_verdict": "fail", "bugs_found": []}})
    assert grader.grade(bplan, miss, lint={"ok": True})["runs"]["target-1"]["outcome"] == grader.UNATTRIBUTED


@pytest.mark.parametrize("metrics,why", [
    (None, "no_result"),
    ({"reported_verdict": "fail", "env_failure": True}, "env_failure"),
    ({"reported_verdict": "fail", "infra_failure": True}, "infra_failure"),
    ({"reported_verdict": "fail", "contaminated": True, "contamination_reasons": ["x"]}, "contaminated"),
    ({"reported_verdict": "fail", "mcp_unclean": True}, "mcp_unclean"),
    ({"reported_verdict": "fail", "failure_class": "rate_limited"}, "rate_limited"),
    ({"reported_verdict": "fail", "truncated": True}, "truncated"),
    ({"reported_verdict": "fail", "timed_out": True}, "timed_out"),
])
def test_an_excluded_run_is_unscored_never_a_zero(metrics, why):
    plan = _plan()
    for key, axis in (("clean-1", "repeatability"), ("control-1", "specificity"),
                      ("target-1", "power")):
        g = grader.grade(plan, _ok_runs(plan, **{key: metrics}), lint={"ok": True})
        assert g["runs"][key]["outcome"] == grader.EXCLUDED
        assert g["runs"][key]["excluded"].startswith(why)
        assert g["axes"][axis] is None and g["axes"]["strong"] is None
        assert g["excluded_runs"] == [key]
    # ...but a counted failure still decides repeatability with another clean run excluded.
    g = grader.grade(plan, _ok_runs(plan, **{"clean-1": metrics,
                                            "clean-2": {"reported_verdict": "fail"}}),
                     lint={"ok": True})
    assert g["axes"]["repeatability"] is False and g["clean_passed"] == "1/2"


def test_a_lint_failing_artifact_is_graded_anyway_as_a_diagnostic():
    plan = _plan()
    g = grader.grade(plan, _ok_runs(plan), lint={"ok": False, "hard_failed": ["atomic-steps"]})
    assert g["status"] == grader.GRADED and g["lint_diagnostic"] is True
    assert g["axes"]["lint"] is False and g["axes"]["strong"] is False
    assert g["axes"]["strong_exec"] is True                     # the execution half still shows
    assert g["axes"]["repeatability"] is g["axes"]["power"] is g["axes"]["specificity"] is True


def test_the_real_lint_runs_on_the_artifact():
    lint = grader.lint_artifact(_plan())
    assert set(lint) >= {"ok", "hard_failed", "soft_failed"}


def test_summary_rates_leave_unscored_and_na_out_of_the_denominator():
    plan, deck = _plan(), grader.plan_grade(grader.reference_case("ankidroid", DECK), DECK)
    strong = grader.grade(plan, _ok_runs(plan), lint={"ok": True})
    weak = grader.grade(plan, _ok_runs(plan, **{"target-1": {"reported_verdict": "pass"}}),
                        lint={"ok": True})
    unscored = grader.grade(plan, _ok_runs(plan, **{"clean-1": None}), lint={"ok": True})
    na = grader.grade(deck, _ok_runs(deck), lint={"ok": True})
    nocase = grader.grade(grader.plan_grade(None, STUDY), {})
    s = grader.summarize([strong, weak, unscored, na, nocase])
    assert s["graded"] == 5 and s["no_case_created"] == 1
    assert (s["power"]["rate_k"], s["power"]["rate_n"], s["power"]["na"]) == (2, 4, 1)
    assert (s["repeatability"]["rate_k"], s["repeatability"]["rate_n"],
            s["repeatability"]["unscored"]) == (3, 4, 1)
    assert (s["strong"]["rate_k"], s["strong"]["rate_n"]) == (1, 3)
    assert s["strong"]["rate_ci"][0] < s["strong"]["rate_rate"] < s["strong"]["rate_ci"][1]


def test_runner_fingerprint_is_frozen_across_cases():
    a = grader.runner_fingerprint("codex-cli", "gpt-6-astra")
    assert a == grader.runner_fingerprint("codex-cli", "gpt-6-astra")
    assert a["brief_version"] >= 3 and a["budget_rule"] == grader.BUDGET_RULE
    assert a["brief_sha"] != grader.runner_fingerprint("codex-cli", "gpt-6-astra", "raw")["brief_sha"]


# ── end to end through the journey verdict, then an offline rescore ──────────

def _episode(runs_dir: Path, plan, run, transcript: str, findings: str, *, fired, **spec_extra):
    """A saved episode exactly as `run_episode` leaves it: the verdict scored with the
    grader's `verdict`, result.json, the transcript and the findings file."""
    task = grader.build_task(plan, run, _suite())
    task.bundle_id = "com.ichi2.anki.debug"
    task.bug_spec.update({"tooling": "mcp", "fired": fired, "findings_file": findings,
                          **spec_extra})
    v = grader.verdict(transcript, "gpt-6-astra", task)
    v.metrics["failure_class"] = None
    ep = runs_dir / plan.grade_id / f"ep-{run.key}"
    (ep / "agent").mkdir(parents=True)
    (ep / "workspace").mkdir()
    (ep / "agent" / "transcript.txt").write_text(transcript)
    (ep / "workspace" / journey.FILENAME).write_text(findings)
    now = datetime.now(UTC)
    RunResult.build(task_id=task.id, task_version="qgb-v1", task_type=grader.TASK_TYPE,
                    agent="codex-cli", model="gpt-6-astra", condition="mcp", trial=run.index,
                    started_at=now, ended_at=now, exit_code=int(spec_extra.get("exit_code") or 0),
                    verifier=v, artifact_dir=ep,
                    runs_dir=runs_dir, run_id="r1").write(ep / "result.json")
    run.attempts.append({"episode_dir": str(ep.relative_to(runs_dir)), "excluded": ""})
    return v


@pytest.mark.parametrize("device_contact", [False, True])
def test_an_agent_killed_before_reporting_rescores_as_it_was_excluded_live(tmp_path, device_contact):
    """QUA-2857 live check: the provider ran out of credits and codex exited 1 with no
    verdict. Live that is `env_failure` (exit code + no verdict); the rescore must read the
    exit code back from result.json, or the run rescores as infra_failure — or, once the
    agent had touched the device, as a counted zero."""
    plan = _plan()
    for run in plan.runs:
        if run.key == "clean-2":
            body = _transcript(_obs("Spanish  uno")) if device_contact else _transcript()
            _episode(tmp_path, plan, run, body, "", fired=[], exit_code=1)
        elif run.role == "target":
            _episode(tmp_path, plan, run, _transcript(_obs("AnkiDroid keeps stopping")),
                     "verdict: fail\nbugs:" + _bug(2, "AnkiDroid keeps stopping", "it crashed"),
                     fired=["reviewer-show-answer-crash"])
        else:
            _episode(tmp_path, plan, run, _transcript(_obs("one Good")),
                     "verdict: pass\nbugs: []\n", fired=[])
    live = grader.grade(plan, {r.key: grader._read_metrics(tmp_path, r) for r in plan.runs})
    assert live["runs"]["clean-2"]["excluded"].startswith("env_failure")
    assert live["axes"]["repeatability"] is None
    manifest = grader.manifest_path(tmp_path, "r1", plan.grade_id)
    grader.write_manifest(manifest, plan, runner={}, run_id="r1", result=live)
    fresh, recorded = grader.rescore_grade(manifest, tmp_path)
    assert fresh == recorded


def test_live_grade_from_runner_outputs_and_rescore_reproduces_it(tmp_path):
    plan = _plan()
    obs = _obs("Spanish  uno  Show answer")
    for run in plan.runs:
        if run.role == "target":
            findings = ("verdict: fail\nbugs:" + _bug(3, "AnkiDroid keeps stopping",
                                                      "the app crashed when I tapped Show answer"))
            _episode(tmp_path, plan, run, _transcript(obs, _obs("AnkiDroid keeps stopping")),
                     findings, fired=["reviewer-show-answer-crash"])
        elif run.key == "clean-2":
            # Truncated: excluded, never a zero.
            _episode(tmp_path, plan, run, _transcript(obs), "verdict: fail\nbugs: []\n",
                     fired=[], truncated=True)
        else:
            _episode(tmp_path, plan, run, _transcript(obs, _obs("one  Good")),
                     "verdict: pass\nbugs: []\n", fired=[])
    metrics = {r.key: grader._read_metrics(tmp_path, r) for r in plan.runs}
    assert metrics["target-1"]["create_role"] == "target"
    assert metrics["target-1"]["bugs_found"] == ["reviewer-show-answer-crash"]
    live = grader.grade(plan, metrics)
    assert live["runs"]["target-1"]["outcome"] == grader.CAUGHT
    assert live["runs"]["clean-2"]["outcome"] == grader.EXCLUDED
    assert live["axes"]["repeatability"] is None and live["axes"]["power"] is True
    assert live["axes"]["specificity"] is True
    manifest = grader.manifest_path(tmp_path, "r1", plan.grade_id)
    grader.write_manifest(manifest, plan, runner=grader.runner_fingerprint("codex-cli", "m"),
                          run_id="r1", result=live)
    fresh, recorded = grader.rescore_grade(manifest, tmp_path)
    assert fresh == recorded == json.loads(json.dumps(live))
    # The rescore wrote nothing.
    assert "rescored_from" not in json.loads(
        (tmp_path / plan.runs[1].attempts[-1]["episode_dir"] / "result.json").read_text())
    assert grader.main(["rescore", str(manifest), "--runs-dir", str(tmp_path)]) == 0


def test_rescore_reflects_a_changed_key_and_says_it_no_longer_reproduces(tmp_path, monkeypatch):
    """No canary for the target → attribution falls to the report; a report that names
    nothing then rescores to unattributed."""
    _uncover(monkeypatch)
    plan = _plan(BROWSE)
    for run in plan.runs:
        if run.role == "target":
            findings = "verdict: fail\nbugs:" + _bug(2, "2 cards shown", "the count is off by one")
            _episode(tmp_path, plan, run, _transcript(_obs("2 cards shown  uno  dos  tres")),
                     findings, fired=[])
        else:
            _episode(tmp_path, plan, run, _transcript(_obs("3 cards shown uno dos tres")),
                     "verdict: pass\nbugs: []\n", fired=[])
    live = grader.grade(plan, {r.key: grader._read_metrics(tmp_path, r) for r in plan.runs})
    assert live["runs"]["target-1"]["attribution"] == "report_match" and live["axes"]["power"]
    manifest = grader.manifest_path(tmp_path, "r1", plan.grade_id)
    grader.write_manifest(manifest, plan, runner={}, run_id="r1", result=live)
    assert grader.rescore_grade(manifest, tmp_path)[0] == json.loads(json.dumps(live))
    # The key moves (the marker and symptoms gone): the rescore follows the corpus.
    real = journey.load_defects

    def blank(doc):
        d = real(doc)
        d["browser-count-low"] = {**d["browser-count-low"], "marker": "", "symptoms": []}
        return d
    monkeypatch.setattr(journey, "load_defects", blank)
    monkeypatch.setattr(journey, "load_truth", lambda app: {
        **json.loads((Path(journey.truth_path(app))).read_text()),
        BROWSE: {**json.loads(Path(journey.truth_path(app)).read_text())[BROWSE], "side": []}})
    fresh, recorded = grader.rescore_grade(manifest, tmp_path)
    assert fresh["runs"]["target-1"]["outcome"] == grader.UNATTRIBUTED and fresh != recorded


# ── QUA-2859: the stored id reaches lint; a copy of the reference is set aside ──

def test_the_stored_id_reaches_lint_so_strong_is_reachable():
    """The fake API keeps the id BESIDE the case; lint read the case alone and failed
    `created-via-api` on every real artifact (found by the QUA-2859 honest adversary)."""
    plan = _plan()
    assert plan.case.raw["test_case_id"] == "tc-1"
    assert "created-via-api" not in grader.lint_artifact(plan)["hard_failed"]
    # A bare capture of the POST was never stored: no id, and lint still says so.
    bare = grader.plan_grade(grader.runner_case({"request": _artifact()["request"]}), STUDY)
    assert "created-via-api" in grader.lint_artifact(bare)["hard_failed"]


def test_a_copy_of_the_public_reference_is_a_contamination_risk():
    ref = next(c for c in journey.load_cases("ankidroid")["test_cases"] if c["id"] == STUDY)
    # Laundered: kind tags, a launch step, case and punctuation changed — still a copy.
    steps = [{"description": "Open the app", "kind": "setup"}] + [
        {"description": s.upper().rstrip("."), "kind": "act"} for s in ref["steps"]]
    copy = grader.runner_case(_artifact(steps=steps, expected=ref["expected_outcome"]))
    rc = grader.reference_copy(copy, "ankidroid", STUDY)
    assert rc["flagged"] and rc["copied_units"] == rc["reference_units"] == len(ref["steps"]) + 1
    # The outcome reworded: still most of the reference.
    reworded = grader.runner_case(_artifact(steps=steps, expected="The next card is shown."))
    assert grader.reference_copy(reworded, "ankidroid", STUDY)["flagged"]
    assert not grader.reference_copy(grader.runner_case(_artifact()), "ankidroid", STUDY)["flagged"]
    # Two generic steps of a short reference are what any author writes: not a copy.
    browse = next(c for c in journey.load_cases("ankidroid")["test_cases"] if c["id"] == BROWSE)
    generic = [{"description": "Open the app", "kind": "setup"}] + [
        {"description": s, "kind": "act"} for s in browse["steps"][:2]] + [
        {"description": "Verify the header counts every card listed", "kind": "verify"}]
    own = grader.runner_case(_artifact(steps=generic, expected="The header counts the cards."))
    rc = grader.reference_copy(own, "ankidroid", BROWSE)
    assert rc["copied_units"] == 2 and not rc["flagged"]
    assert grader.reference_copy(grader.reference_case("ankidroid", STUDY), "ankidroid", STUDY) is None
    plan = grader.plan_grade(copy, STUDY)
    g = grader.grade(plan, _ok_runs(plan), lint={"ok": True})
    assert g["contamination_risk"] == grader.CONTAMINATION_RISK and g["axes"]["strong"] is True
    honest = grader.grade(_plan(), _ok_runs(_plan()), lint={"ok": True})
    s = grader.summarize([g, honest])
    assert s["contamination_risk"] == 1 and s["graded"] == 1
    assert (s["strong"]["rate_k"], s["strong"]["rate_n"]) == (1, 1)
