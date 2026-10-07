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
                         "strong": True, "strong_exec": True, "power_report": True}
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
    # A silent target run (no report) earns no report-credited power either.
    moved = (axis, "strong", "strong_exec") + (("power_report",) if axis == "power" else ())
    assert all(v is True for k, v in g["axes"].items() if k not in moved)
    assert g["axes"]["power_report"] is (axis != "power")


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


# ── QUA-2865: an observed death is a FAIL; report-credited power; versioned ───

CRASH = "reviewer-show-answer-crash"


def test_crash_then_relaunch_is_a_fail_under_v3_and_a_miss_under_v2():
    """QUA-2861 arm B, anki-study-first-card: Show answer crashed the reviewer, AnkiDroid
    came back on its deck list, "Verify the app is still open" held and the runner wrote
    PASS. The harness recorded the death: from v3 that run is a FAIL, attributed by the
    canary like any other."""
    plan = _plan()
    relaunched = {"reported_verdict": "pass", "fault_fired": [CRASH], "bugs_found": [CRASH],
                  "app_crashes": 1}
    runs = _ok_runs(plan, **{"target-1": relaunched})
    g = grader.grade(plan, runs, lint={"ok": True})
    t = g["runs"]["target-1"]
    assert t["verdict"] == "pass" and t["effective_verdict"] == "fail" and t["death_forced"]
    assert t["outcome"] == grader.CAUGHT and t["attribution"] == "canary"
    assert g["axes"]["power"] is True and g["axes"]["strong"] is True
    assert g["grader_version"] == grader.GRADER_VERSION >= 3
    # Under v2 the same metrics are the recorded miss, and the v2 dict has no v3 keys.
    old = grader.grade(plan, runs, lint={"ok": True}, version=2)
    assert old["runs"]["target-1"]["outcome"] == grader.MISSED and old["axes"]["power"] is False
    assert "power_report" not in old["axes"] and "grader_version" not in old
    assert "effective_verdict" not in old["runs"]["target-1"]
    # A recorded death with the canary silent is still not the target's.
    silent = _ok_runs(plan, **{"target-1": {**relaunched, "fault_fired": [], "bugs_found": []}})
    t = grader.grade(plan, silent, lint={"ok": True})["runs"]["target-1"]
    assert t["outcome"] == grader.UNATTRIBUTED and t["death_forced"]
    # A count of 0, or none recorded, forces nothing.
    for n in (0, None):
        t = grader.grade(plan, _ok_runs(plan, **{"target-1": {**relaunched, "app_crashes": n,
                                                              "bugs_found": []}}),
                         lint={"ok": True})["runs"]["target-1"]
        assert t["outcome"] == grader.MISSED and t["death_forced"] is False


def test_a_death_on_a_clean_or_control_run_fails_that_run():
    """The rule is the run's, whatever its role: a case whose clean run crashed the app
    did not properly pass."""
    plan = _plan()
    died = {"reported_verdict": "pass", "fault_fired": [], "app_crashes": 2}
    g = grader.grade(plan, _ok_runs(plan, **{"clean-2": died}), lint={"ok": True})
    assert g["runs"]["clean-2"]["outcome"] == grader.FAILED and g["axes"]["repeatability"] is False
    g = grader.grade(plan, _ok_runs(plan, **{"control-1": died}), lint={"ok": True})
    assert g["axes"]["specificity"] is False
    # An excluded run stays excluded: the death rule never scores what measured nothing.
    g = grader.grade(plan, _ok_runs(plan, **{"clean-2": {**died, "truncated": True}}),
                     lint={"ok": True})
    assert g["runs"]["clean-2"]["outcome"] == grader.EXCLUDED and g["axes"]["repeatability"] is None


def test_report_but_pass_earns_power_report_never_power(monkeypatch):
    """QUA-2861 §6: 8 arm-B target runs reported the target and wrote PASS. Verdict-only
    `power` keeps them missed (comparable); `power_report` credits them — only when the
    target's canary fired, where it is read."""
    plan = _plan()
    seen = {"reported_verdict": "pass", "fault_fired": [CRASH], "bugs_found": [CRASH]}
    g = grader.grade(plan, _ok_runs(plan, **{"target-1": seen}), lint={"ok": True})
    t = g["runs"]["target-1"]
    assert t["outcome"] == grader.MISSED and t["report_matched"] and t["ok_report"]
    assert g["axes"]["power"] is False and g["axes"]["power_report"] is True
    assert g["axes"]["strong"] is False and g["axes"]["strong_exec"] is False   # never in Strong
    # A report of the target whose code never ran: no credit.
    g = grader.grade(plan, _ok_runs(plan, **{"target-1": {**seen, "fault_fired": []}}),
                     lint={"ok": True})
    assert g["axes"]["power_report"] is False
    # Canary unread: the report alone, as power's attribution falls back.
    g = grader.grade(plan, _ok_runs(plan, **{"target-1": {**seen, "fault_fired": None}}),
                     lint={"ok": True})
    assert g["axes"]["power_report"] is True
    # The runner noticed nothing: nothing.
    g = grader.grade(plan, _ok_runs(plan, **{"target-1": {**seen, "bugs_found": []}}),
                     lint={"ok": True})
    assert g["axes"]["power_report"] is False
    # A target with no canary: the report match is the credit.
    _uncover(monkeypatch)
    bplan = _plan(BROWSE)
    g = grader.grade(bplan, _ok_runs(bplan, **{"target-1": {"reported_verdict": "pass",
                                                            "bugs_found": ["browser-count-low"]}}),
                     lint={"ok": True})
    assert g["axes"]["power"] is False and g["axes"]["power_report"] is True
    # Excluded target: unscored on both; no target: n/a on both.
    g = grader.grade(plan, _ok_runs(plan, **{"target-1": None}), lint={"ok": True})
    assert g["axes"]["power"] is None and g["axes"]["power_report"] is None
    deck = grader.plan_grade(grader.reference_case("ankidroid", DECK), DECK)
    g = grader.grade(deck, _ok_runs(deck), lint={"ok": True})
    assert g["axes"]["power"] == g["axes"]["power_report"] == grader.NA


def test_summary_counts_power_report_only_on_grades_that_carry_it():
    plan = _plan()
    v3 = grader.grade(plan, _ok_runs(plan), lint={"ok": True})
    v2 = grader.grade(plan, _ok_runs(plan), lint={"ok": True}, version=2)
    s = grader.summarize([v3, v2])
    assert (s["power"]["rate_k"], s["power"]["rate_n"]) == (2, 2)
    assert (s["power_report"]["rate_k"], s["power_report"]["rate_n"]) == (1, 1)
    assert s["power_report"]["unscored"] == 0          # absent is not unscored
    nocase = grader.grade(grader.plan_grade(None, STUDY), {})
    assert nocase["axes"]["power_report"] is False
    assert "power_report" not in grader.grade(grader.plan_grade(None, STUDY), {}, version=2)["axes"]


def _relaunch_episodes(tmp_path):
    """A saved grade whose target run is crash-then-relaunch + a matched report + PASS."""
    plan = _plan()
    obs = _obs("Spanish  uno  Show answer")
    for run in plan.runs:
        if run.role == "target":
            findings = ("verdict: pass\nbugs:" + _bug(3, "AnkiDroid keeps stopping",
                                                      "the app crashed when I tapped Show answer "
                                                      "and came back to the deck list"))
            _episode(tmp_path, plan, run, _transcript(obs, _obs("AnkiDroid keeps stopping")),
                     findings, fired=[CRASH], app_crash_count=1)
        else:
            _episode(tmp_path, plan, run, _transcript(obs, _obs("one  Good")),
                     "verdict: pass\nbugs: []\n", fired=[])
    return plan


def test_an_old_manifest_rescores_to_its_recorded_grade_and_v3_moves_it(tmp_path, capsys):
    """Versioned contract: a v2 manifest reproduces under v2 (its recorded version) and
    `--grader-version 3` shows what the new contract moves, through the real journey
    verdict and the offline rescore."""
    plan = _relaunch_episodes(tmp_path)
    metrics = {r.key: grader._read_metrics(tmp_path, r) for r in plan.runs}
    assert metrics["target-1"]["app_crashes"] == 1
    assert metrics["target-1"]["reported_verdict"] == "pass"
    assert metrics["target-1"]["bugs_found"] == [CRASH]
    live_v2 = grader.grade(plan, metrics, version=2)
    assert live_v2["axes"]["power"] is False
    runner = {**grader.runner_fingerprint("codex-cli", "m"), "grader_version": 2}
    manifest = grader.manifest_path(tmp_path, "r1", plan.grade_id)
    grader.write_manifest(manifest, plan, runner=runner, run_id="r1", result=live_v2)
    fresh, recorded = grader.rescore_grade(manifest, tmp_path)
    assert fresh == recorded == json.loads(json.dumps(live_v2))
    assert grader.main(["rescore", str(manifest), "--runs-dir", str(tmp_path)]) == 0
    v3, _ = grader.rescore_grade(manifest, tmp_path, version=3)
    assert v3["axes"]["power"] is True and v3["axes"]["power_report"] is True
    assert v3["runs"]["target-1"]["death_forced"] is True
    capsys.readouterr()
    assert grader.main(["rescore", str(manifest), "--runs-dir", str(tmp_path),
                        "--grader-version", "3"]) == 0
    out = capsys.readouterr().out
    assert "power False → True" in out and "power_report (new) True" in out
    assert "strong_exec" in out
    # A v3 manifest reproduces under v3.
    live_v3 = grader.grade(plan, metrics)
    grader.write_manifest(manifest, plan, runner=grader.runner_fingerprint("codex-cli", "m"),
                          run_id="r1", result=live_v3)
    assert grader.rescore_grade(manifest, tmp_path)[0] == json.loads(json.dumps(live_v3))


def test_runner_fingerprint_names_the_verdict_rule():
    fp = grader.runner_fingerprint("codex-cli", "gpt-6-astra")
    assert fp["grader_version"] == 4 and fp["verdict_rule"] == grader.VERDICT_RULE
    assert fp["control_rule"] == grader.CONTROL_RULE
    assert grader.recorded_version({"runner": {"grader_version": 2}}) == 2
    assert grader.recorded_version({"runner": {}}) == grader.GRADER_VERSION


# ── QUA-2866: a control run that reached its control is excluded, not unspecific ──

def _ctrl(plan):
    c = plan.control["control"]
    assert c and c not in plan.targets
    return c


def test_control_canary_fired_and_app_died_is_excluded_control_reached():
    """QUA-2861 rerun, tasks-complete-parent t2: the authored route reached the control,
    its canary fired, the app crashed, the runner wrote FAIL. From v4 that is
    `control_reached` — specificity unscored (None), never False; v3 scored it False."""
    plan = _plan()
    ctrl = _ctrl(plan)
    runs = _ok_runs(plan, **{"control-1": {"reported_verdict": "fail", "fault_fired": [ctrl],
                                           "bugs_found": [ctrl], "app_crashes": 1}})
    g = grader.grade(plan, runs, lint={"ok": True})
    c = g["runs"]["control-1"]
    assert c["outcome"] == grader.EXCLUDED and c["ok"] is None
    assert c["excluded"].startswith(grader.CONTROL_REACHED + " ")
    assert c["control_fired"] is True and c["scored_outcome"] == grader.FAILED
    assert grader.is_control_reached(c)
    assert g["axes"]["specificity"] is None
    assert g["axes"]["strong"] is None and g["axes"]["strong_exec"] is None
    assert g["axes"]["repeatability"] is g["axes"]["power"] is True
    assert g["excluded_runs"] == ["control-1"] and g["grader_version"] == 4
    # v3 (and v2) reproduce what they recorded: a specificity failure, no new keys.
    for v in (3, 2):
        old = grader.grade(plan, runs, lint={"ok": True}, version=v)
        assert old["runs"]["control-1"]["outcome"] == grader.FAILED
        assert old["axes"]["specificity"] is False and old["excluded_runs"] == []
        assert "scored_outcome" not in old["runs"]["control-1"]
        assert not grader.is_control_reached(old["runs"]["control-1"])


@pytest.mark.parametrize("crashes", [0, 1])
@pytest.mark.parametrize("fired", [[], None, ["reviewer-show-answer-crash"]])
def test_a_silent_control_with_a_fail_is_still_a_specificity_failure(fired, crashes):
    """Nothing ties the FAIL to the control: its canary is silent, unread (None), or only
    ANOTHER defect's marker fired — with or without a recorded death. That is the case
    failing on an unrelated build."""
    plan = _plan()
    g = grader.grade(plan, _ok_runs(plan, **{"control-1": {"reported_verdict": "fail",
                                                           "fault_fired": fired,
                                                           "app_crashes": crashes}}),
                     lint={"ok": True})
    c = g["runs"]["control-1"]
    assert c["outcome"] == grader.FAILED and c["ok"] is False
    assert c["control_fired"] is (None if fired is None else False)
    assert g["axes"]["specificity"] is False and not grader.is_control_reached(c)


@pytest.mark.parametrize("verdict,outcome", [("fail", grader.FAILED),
                                             (None, grader.NO_VERDICT)])
def test_a_reached_control_with_the_app_alive_is_still_a_specificity_failure(verdict, outcome):
    """`create_adversary_check`'s overfit-build: the case asserts an incidental value a
    reached sibling defect moves. The control's canary fired, the app lived, and the
    case's own checks rejected it — what specificity exists to catch. A canary-only rule
    excluded it (the gate failed on every brief)."""
    plan = _plan()
    ctrl = _ctrl(plan)
    for crashes in (0, None):                  # none recorded, or the count was unreadable
        g = grader.grade(plan, _ok_runs(plan, **{"control-1": {
            "reported_verdict": verdict, "fault_fired": [ctrl], "app_crashes": crashes}}),
            lint={"ok": True})
        c = g["runs"]["control-1"]
        assert c["control_fired"] is True and c["outcome"] == outcome and c["ok"] is False
        assert g["axes"]["specificity"] is False and not grader.is_control_reached(c)


def test_a_pass_with_the_control_reached_stays_a_scored_pass():
    """The control's code ran and the case did not trip on it: the strongest specificity
    evidence a run gives (13 of QUA-2861 rerun's 15 fired control runs). Never excluded."""
    plan = _plan()
    ctrl = _ctrl(plan)
    g = grader.grade(plan, _ok_runs(plan, **{"control-1": {"reported_verdict": "pass",
                                                           "fault_fired": [ctrl]}}),
                     lint={"ok": True})
    c = g["runs"]["control-1"]
    assert c["outcome"] == grader.PASSED and c["control_fired"] is True
    assert g["axes"]["specificity"] is True and g["axes"]["strong"] is True
    assert g["excluded_runs"] == []


def test_control_reached_takes_precedence_over_the_death_rule():
    """A control that crashed the app is exactly the control_reached case: v3's forced FAIL
    is recorded (effective verdict, death_forced) but the run is excluded, not unspecific.
    A death with the control's canary silent is still v3's specificity FAIL."""
    plan = _plan()
    ctrl = _ctrl(plan)
    for reported in ("pass", "fail"):          # the runner noticed, or wrote PASS anyway
        crashed = {"reported_verdict": reported, "fault_fired": [ctrl], "app_crashes": 1}
        g = grader.grade(plan, _ok_runs(plan, **{"control-1": crashed}), lint={"ok": True})
        c = g["runs"]["control-1"]
        assert c["effective_verdict"] == "fail" and c["death_forced"] is (reported == "pass")
        assert grader.is_control_reached(c) and g["axes"]["specificity"] is None
        v3 = grader.grade(plan, _ok_runs(plan, **{"control-1": crashed}), lint={"ok": True},
                          version=3)
        assert v3["runs"]["control-1"]["outcome"] == grader.FAILED
        assert v3["axes"]["specificity"] is False
    other_death = {"reported_verdict": "pass", "fault_fired": [], "app_crashes": 1}
    g = grader.grade(plan, _ok_runs(plan, **{"control-1": other_death}), lint={"ok": True})
    assert g["runs"]["control-1"]["outcome"] == grader.FAILED
    assert g["axes"]["specificity"] is False


def test_other_exclusions_come_before_control_reached():
    plan = _plan()
    ctrl = _ctrl(plan)
    # A run that measured nothing keeps its own reason.
    g = grader.grade(plan, _ok_runs(plan, **{"control-1": {"reported_verdict": "fail",
                                                           "fault_fired": [ctrl],
                                                           "app_crashes": 1,
                                                           "truncated": True}}),
                     lint={"ok": True})
    assert g["runs"]["control-1"]["excluded"].startswith("truncated")
    assert not grader.is_control_reached(g["runs"]["control-1"])
    # The rule is the control role's: a clean run that died is v3's FAIL, never excluded.
    g = grader.grade(plan, _ok_runs(plan, **{"clean-1": {"reported_verdict": "fail",
                                                         "fault_fired": [ctrl],
                                                         "app_crashes": 1}}),
                     lint={"ok": True})
    assert g["runs"]["clean-1"]["outcome"] == grader.FAILED
    assert not grader.is_control_reached(g["runs"]["clean-1"])
    assert not grader.is_control_reached(None)


def test_summary_leaves_control_reached_out_of_specificity_and_counts_the_canary():
    plan = _plan()
    ctrl = _ctrl(plan)
    reached = grader.grade(plan, _ok_runs(plan, **{"control-1": {"reported_verdict": "fail",
                                                                 "fault_fired": [ctrl],
                                                                 "app_crashes": 1}}),
                           lint={"ok": True})
    passed = grader.grade(plan, _ok_runs(plan, **{"control-1": {"reported_verdict": "pass",
                                                                "fault_fired": [ctrl]}}),
                          lint={"ok": True})
    silent = grader.grade(plan, _ok_runs(plan), lint={"ok": True})
    unread = grader.grade(plan, _ok_runs(plan, **{"control-1": {"reported_verdict": "pass"}}),
                          lint={"ok": True})
    s = grader.summarize([reached, passed, silent, unread])
    assert (s["specificity"]["rate_k"], s["specificity"]["rate_n"]) == (3, 3)
    assert s["specificity"]["unscored"] == 1
    assert s["control_canary"] == {"runs": 4, "read": 3, "fired": 2, "excluded": 1}


def test_a_v3_manifest_rescores_to_its_grade_and_v4_excludes_the_reached_control(tmp_path,
                                                                                    capsys):
    """Through the real journey verdict and the offline rescore: the t2 shape — the
    control's canary fired, the app crashed, the runner wrote FAIL naming the crash."""
    plan = _plan()
    ctrl = _ctrl(plan)
    obs = _obs("Spanish  uno  Show answer")
    for run in plan.runs:
        if run.role == "target":
            findings = ("verdict: fail\nbugs:" + _bug(3, "AnkiDroid keeps stopping",
                                                      "the app crashed when I tapped Show answer"))
            _episode(tmp_path, plan, run, _transcript(obs, _obs("AnkiDroid keeps stopping")),
                     findings, fired=[CRASH])
        elif run.role == "control":
            findings = ("verdict: fail\nbugs:" + _bug(3, "AnkiDroid keeps stopping",
                                                      "the app crashed"))
            _episode(tmp_path, plan, run, _transcript(obs, _obs("AnkiDroid keeps stopping")),
                     findings, fired=[ctrl], app_crash_count=1)
        else:
            _episode(tmp_path, plan, run, _transcript(obs, _obs("one  Good")),
                     "verdict: pass\nbugs: []\n", fired=[])
    metrics = {r.key: grader._read_metrics(tmp_path, r) for r in plan.runs}
    assert metrics["control-1"]["fault_fired"] == [ctrl]
    live_v3 = grader.grade(plan, metrics, version=3)
    assert live_v3["axes"]["specificity"] is False
    runner = {**grader.runner_fingerprint("codex-cli", "m"), "grader_version": 3}
    manifest = grader.manifest_path(tmp_path, "r1", plan.grade_id)
    grader.write_manifest(manifest, plan, runner=runner, run_id="r1", result=live_v3)
    fresh, recorded = grader.rescore_grade(manifest, tmp_path)
    assert fresh == recorded == json.loads(json.dumps(live_v3))
    v4, _ = grader.rescore_grade(manifest, tmp_path, version=4)
    assert grader.is_control_reached(v4["runs"]["control-1"])
    assert v4["axes"]["specificity"] is None and v4["axes"]["power"] is True
    capsys.readouterr()
    assert grader.main(["rescore", str(manifest), "--runs-dir", str(tmp_path),
                        "--grader-version", "4"]) == 0
    out = capsys.readouterr().out
    assert "specificity False → None" in out and "control_reached" in out
    # A v4 manifest reproduces under v4.
    live_v4 = grader.grade(plan, metrics)
    grader.write_manifest(manifest, plan, runner=grader.runner_fingerprint("codex-cli", "m"),
                          run_id="r1", result=live_v4)
    assert grader.rescore_grade(manifest, tmp_path)[0] == json.loads(json.dumps(live_v4))
