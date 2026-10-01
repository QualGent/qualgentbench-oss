"""CreateBench v2: the pre-registered A/B driver (QUA-2858).

Simulated end to end — synthetic AUTHORS (one writes a case that exercises the brief's
feature, one obeys the harmful rule and ends on a vacuous title check) and a synthetic
RUNNER whose verdict follows the case's semantics against the run's flag state — through
the REAL grader (`plan_grade`, `grade`, lint, manifests) and the real driver: DETECTED /
MISSED, resume after an injected mid-run fault, the cost-ceiling stop. No device, no
agent, no subprocess."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qualgentbench import journey
from qualgentbench.create import ab, board, grader

STUDY = "anki-study-first-card"
BROWSE = "anki-open-card-from-browser"
UNDERIVED = "cal-search-event"          # served without controls by _one_underived_case
BRIEFS = [STUDY, BROWSE]
HARMFUL_STEP = "Verify the current screen's title is visible"
APP_OPEN_STEP = "Verify the app is still open"          # QUA-2864's rule (uptake.APP_OPEN)


@pytest.fixture(autouse=True)
def _one_underived_case(monkeypatch):
    """QUA-2854 derived controls for all 41 cases; these tests need a case WITHOUT them
    (the not-gradable path), so that one row is served with the two keys stripped."""
    real = journey.load_truth

    def load_truth(app_id):
        truth = real(app_id)
        case = UNDERIVED
        if case in truth:
            truth = {**truth, case: {k: v for k, v in truth[case].items()
                                     if k not in (journey.CONTROLS_KEY,
                                                  journey.CONTROL_DERIVATION_KEY)}}
        return truth
    monkeypatch.setattr(journey, "load_truth", load_truth)


def _case(case_id: str, *, vacuous: bool, harmful: bool = False,
          app_open: bool = False) -> dict:
    """An authored case. `vacuous` = ends on a title check and never walks the feature;
    `harmful` = QUA-2859's harmful-rule author: walks the feature, then ends on the
    title check instead of checking the outcome."""
    steps = [{"description": "Launch AnkiDroid", "kind": "setup"},
             {"description": "Tap \"Default\"", "kind": "act"}]
    if vacuous:
        steps.append({"description": HARMFUL_STEP, "kind": "verify"})
        expected = "The screen title is visible."
    elif harmful:
        steps += [{"description": "Tap \"Show answer\"", "kind": "act"},
                  {"description": HARMFUL_STEP, "kind": "verify"}]
        expected = "The screen title is visible."
    elif app_open:
        steps += [{"description": "Tap \"Show answer\"", "kind": "act"},
                  {"description": APP_OPEN_STEP, "kind": "verify"}]
        expected = "The app is still open."
    else:
        steps += [{"description": "Tap \"Show answer\"", "kind": "act"},
                  {"description": "Verify the answer side of the card is shown",
                   "kind": "verify"}]
        expected = "The answer side of the card is shown."
    case = {"name": f"Check {case_id}", "steps": steps, "expected_result": expected,
            "priority": "High"}
    from qualgentbench.create.fake_api import serialize_steps
    return {"schema": "qualgentbench.create.authored_case/1", "test_case_id": "tc",
            "request": dict(case), "updates": [], "case": case,
            "serialized_steps": serialize_steps(steps)}


class Crash(BaseException):
    """A process death: not an `Exception`, so the driver's per-cell fault handling
    does not swallow it — the state file is all that survives."""


class SimAuthor:
    """Writes `<runs>/sim/<cell>/authored_case.json` like a creation episode would.
    `policy[arm]` = "honest" | "vacuous" | "harmful" | "none" (saves no case). `fail` = {cell key:
    n} raises an Exception on the first n attempts; `crash_after_write` = {cell key}
    dies AFTER the episode is on disk (the driver never hears back)."""

    def __init__(self, runs: Path, policy: dict[str, str], *, fail=None, crash_after_write=None,
                 cost: float | None = 1.0):
        self.runs, self.policy, self.cost = runs, policy, cost
        self.fail = dict(fail or {})
        self.crash_after_write = set(crash_after_write or ())
        self.calls: dict[str, int] = {}

    def _dir(self, cell: ab.Cell) -> Path:
        return self.runs / "sim" / cell.key

    def _outcome(self, cell: ab.Cell) -> ab.AuthorOutcome:
        d = self._dir(cell)
        art = d / "authored_case.json"
        return ab.AuthorOutcome(
            episode_dir=str(d.relative_to(self.runs)),
            artifact=str(art) if art.exists() else None,
            outcome="case_created" if art.exists() else "no_case_created",
            cost_usd=self.cost, wall_sec=60.0,
            arm_manifest={"name": cell.arm, "qualgent_mcp": {"sha": f"{cell.arm.lower()}" * 40}})

    async def author(self, cell: ab.Cell, arm: ab.ArmSpec) -> ab.AuthorOutcome:
        self.calls[cell.key] = self.calls.get(cell.key, 0) + 1
        if self.fail.get(cell.key, 0) > 0:
            self.fail[cell.key] -= 1
            raise RuntimeError("codex exited 1 before the device was reachable")
        d = self._dir(cell)
        d.mkdir(parents=True, exist_ok=True)
        (d / "done").write_text("1")
        if self.policy[cell.arm] != "none":
            (d / "authored_case.json").write_text(json.dumps(
                _case(cell.case_id, vacuous=self.policy[cell.arm] == "vacuous",
                      harmful=self.policy[cell.arm] == "harmful",
                      app_open=self.policy[cell.arm] == "app-open")))
        if cell.key in self.crash_after_write:
            self.crash_after_write.discard(cell.key)
            raise Crash(cell.key)
        return self._outcome(cell)

    def recover(self, cell: ab.Cell) -> ab.AuthorOutcome | None:
        return self._outcome(cell) if (self._dir(cell) / "done").exists() else None


class SimRunner:
    """The frozen runner, simulated: clean and control runs PASS; the target run FAILS
    with the target's canary fired unless the case ends on the harmful title check, in
    which case it PASSES (the case never looks where the defect is) — except that a
    target that KILLS the app (crash/ANR/stuck, `ab.target_stratum`) still fails a case
    that walks the feature, however it ends (QUA-2859's finding). Then the REAL
    `grader.grade` (lint included) and a real manifest. `crash_mid_grade` = {cell key}
    writes a partial manifest and dies."""

    def __init__(self, runs: Path, *, cost: float = 5.0, crash_mid_grade=None):
        self.runs, self.cost = runs, cost
        self.crash_mid_grade = set(crash_mid_grade or ())
        self.calls: dict[str, int] = {}

    def metrics(self, plan: grader.GradePlan) -> dict[str, dict]:
        walks = any("Show answer" in s for s in plan.case.steps)
        vacuous = plan.case.steps[-1] in (HARMFUL_STEP, APP_OPEN_STEP) and not (
            walks and ab.target_stratum(plan.case_id) == ab.DEATH)
        out = {}
        for run in plan.runs:
            if run.role == "target":
                out[run.key] = ({"reported_verdict": "pass", "fault_fired": []} if vacuous else
                                {"reported_verdict": "fail", "fault_fired": list(plan.targets),
                                 "bugs_found": list(plan.targets)})
            else:
                out[run.key] = {"reported_verdict": "pass", "fault_fired": []}
        return out

    async def grade(self, plan, *, run_id, manifest_name, extra):
        self.calls[manifest_name] = self.calls.get(manifest_name, 0) + 1
        path = grader.manifest_path(self.runs, run_id, manifest_name)
        runner = grader.runner_fingerprint("codex-cli", "gpt-6-astra")
        if manifest_name in self.crash_mid_grade:
            self.crash_mid_grade.discard(manifest_name)
            grader.write_manifest(path, plan, runner=runner, run_id=run_id, result=None,
                                  extra={**extra, "cost": {"episodes": 2, "priced_episodes": 2,
                                                           "cost_usd": 2.0}})
            raise Crash(manifest_name)
        grader.write_manifest(path, plan, runner=runner, run_id=run_id,
                              result=grader.grade(plan, self.metrics(plan)),
                              extra={**extra, "cost": {"episodes": 5, "priced_episodes": 5,
                                                       "cost_usd": self.cost,
                                                       "agent_wall_sec": 540.0}})
        return path


def _spec(name="pc", briefs=None, trials=3, kind=board.CANONICAL, prediction=None):
    arms = (ab.ArmSpec("A", "/qg@main", "/dl@main",
                       manifest={"name": "A", "qualgent_mcp": {"sha": "a" * 40},
                                 "devloop": {"sha": "d" * 40}}),
            ab.ArmSpec("B", "/qg@harmful", "/dl@main",
                       manifest={"name": "B", "qualgent_mcp": {"sha": "b" * 40},
                                 "devloop": {"sha": "d" * 40}}))
    return ab.ExperimentSpec(name=name, arms=arms, briefs=list(briefs or BRIEFS), trials=trials,
                             author={"agent": "codex-cli", "model": "gpt-6-astra"},
                             runner={"agent": "codex-cli", "model": "gpt-6-astra"},
                             prediction=prediction or ab.POSITIVE_CONTROL, kind=kind)


def _drive(runs: Path, spec, author, runner, *, max_cost=1000.0, max_attempts=2):
    path = ab.state_path(runs, spec.name)
    state = ab.load_state(path) or ab.new_state(spec, run_id=f"20261001-000000-{spec.name}",
                                                ungated=True)
    ab.save_state(path, state)
    d = ab.Driver(spec=spec, runs_dir=runs, author=author, runner=runner, max_cost=max_cost,
                  max_attempts=max_attempts, state=state)
    asyncio.run(d.run())
    return ab.report(runs, spec.name)


# ── the pre-registration ──────────────────────────────────────────────────────

def test_the_positive_control_prediction_is_declared_in_code():
    p = ab.POSITIVE_CONTROL
    assert [(e.axis, e.direction, e.scope) for e in p.expectations] == [
        ("power", "down", "each"), ("power", "down", "pooled"),
        ("repeatability", "flat", "pooled"), ("specificity", "flat", "pooled")]
    assert ab.load_prediction("positive-control") is p
    assert ab.load_prediction(p.name) is p
    # The subset is QUA-2862's 12 briefs; this form runs 3 trials x 2 arms = 72 cells.
    spec = _spec(briefs=ab.load_subset())
    assert len(spec.briefs) == 12 and len(ab.plan_cells(spec)) == 72
    # A prediction round-trips through JSON with the same hash (the registration).
    assert ab.Prediction.from_dict(json.loads(json.dumps(p.as_dict()))).sha == p.sha


def test_cells_interleave_both_arms_of_a_brief_and_alternate_who_goes_first():
    cells = ab.plan_cells(_spec(trials=2))
    assert [c.key for c in cells[:4]] == [f"A.{STUDY}.t1", f"B.{STUDY}.t1"][::-1] + [
        f"A.{BROWSE}.t1", f"B.{BROWSE}.t1"]
    for i in range(0, len(cells), 2):
        assert {cells[i].arm, cells[i + 1].arm} == {"A", "B"}
        assert cells[i].case_id == cells[i + 1].case_id and cells[i].trial == cells[i + 1].trial


def test_a_registered_experiment_cannot_change_after_it_started(tmp_path):
    spec = _spec()
    state = ab.new_state(spec, run_id="r", ungated=True)
    assert ab.registration_diff(state, spec) == []
    other = _spec(trials=5)
    assert any(d.startswith("trials:") for d in ab.registration_diff(state, other))
    flipped = _spec(prediction=ab.Prediction("x", (ab.Expectation("power", "up", "pooled"),)))
    diff = ab.registration_diff(state, flipped)
    assert any(d.startswith("prediction") for d in diff)
    arm = _spec()
    arm.arms[1].manifest["qualgent_mcp"]["sha"] = "c" * 40
    assert any(d.startswith("arms:") for d in ab.registration_diff(state, arm))


# ── judging ───────────────────────────────────────────────────────────────────

def _r(k, n):
    return ab.rates.rate(k, n)


def test_pooled_judging_is_directional_and_needs_separated_intervals():
    assert ab.judge_pooled("down", _r(6, 6), _r(0, 6))[0] == ab.MET
    assert ab.judge_pooled("down", _r(0, 6), _r(6, 6)) == (ab.NOT_MET, "B moved the other way")
    assert ab.judge_pooled("down", _r(4, 6), _r(3, 6)) == (
        ab.NOT_MET, "right direction, but the intervals overlap")
    assert ab.judge_pooled("down", None, _r(1, 6))[0] == ab.INCONCLUSIVE
    assert ab.judge_pooled("flat", _r(5, 6), _r(6, 6))[0] == ab.MET
    assert ab.judge_pooled("flat", _r(30, 30), _r(0, 30))[0] == ab.NOT_MET
    assert ab.judge_pooled("up", _r(0, 6), _r(6, 6))[0] == ab.MET


def test_per_brief_judging_names_floor_and_unscored_apart_from_a_miss():
    assert ab.judge_brief("down", _r(3, 3), _r(0, 3)) == ab.MOVED
    assert ab.judge_brief("down", _r(1, 3), _r(1, 3)) == ab.NOT_MOVED
    assert ab.judge_brief("down", _r(0, 3), _r(0, 3)) == ab.FLOOR
    assert ab.judge_brief("down", None, _r(0, 3)) == ab.UNSCORED


def _g(power, repeat=True, spec=True):
    return {"status": "graded", "axes": {"power": power, "repeatability": repeat,
                                         "specificity": spec, "lint": True,
                                         "strong": power and repeat and spec,
                                         "strong_exec": power and repeat and spec}}


def test_evaluate_detected_missed_inconclusive_incomplete():
    good = {"A": {b: [_g(True)] * 3 for b in BRIEFS}, "B": {b: [_g(False)] * 3 for b in BRIEFS}}
    v = ab.evaluate(ab.POSITIVE_CONTROL, good, BRIEFS, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.DETECTED and v["exit_code"] == 0
    # The wrong direction is MISSED, never reinterpreted.
    v = ab.evaluate(ab.POSITIVE_CONTROL, good, BRIEFS, arm_a="B", arm_b="A")
    assert v["verdict"] == ab.MISSED and v["exit_code"] == 1
    # One brief that does not move is a miss on "every brief", even if the pool moves.
    one = {"A": {b: [_g(True)] * 3 for b in BRIEFS},
           "B": {STUDY: [_g(False)] * 3, BROWSE: [_g(True)] * 3}}
    v = ab.evaluate(ab.POSITIVE_CONTROL, one, BRIEFS, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.MISSED and BROWSE in v["why"]
    # Power drops but repeatability moves too: the "flat" expectation is MISSED.
    flaky = {"A": {b: [_g(True)] * 15 for b in BRIEFS},
             "B": {b: [_g(False, repeat=False)] * 15 for b in BRIEFS}}
    v = ab.evaluate(ab.POSITIVE_CONTROL, flaky, BRIEFS, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.MISSED and "repeatability" in v["why"]
    # Arm A has no power anywhere: nothing can drop — INCONCLUSIVE, not DETECTED.
    floor = {"A": {b: [_g(False)] * 3 for b in BRIEFS}, "B": {b: [_g(False)] * 3 for b in BRIEFS}}
    v = ab.evaluate(ab.POSITIVE_CONTROL, floor, BRIEFS, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.MISSED      # the pooled "down" is not met (no difference)
    each_only = ab.Prediction("each", (ab.Expectation("power", "down", "each"),))
    v = ab.evaluate(each_only, floor, BRIEFS, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.INCONCLUSIVE and "floor 2" in v["why"]
    # Unfinished cells: no verdict at all, whatever the partial data says.
    v = ab.evaluate(ab.POSITIVE_CONTROL, good, BRIEFS, arm_a="A", arm_b="B", complete=False)
    assert v["verdict"] == ab.INCOMPLETE and v["exit_code"] == 4
    # Too many faulted cells: not trusted.
    v = ab.evaluate(ab.POSITIVE_CONTROL, good, BRIEFS, arm_a="A", arm_b="B", faulted=3,
                    planned=12)
    assert v["verdict"] == ab.INCONCLUSIVE and "faulted" in v["why"]


# ── simulated end to end ──────────────────────────────────────────────────────

def test_simulated_positive_control_is_detected_end_to_end(tmp_path):
    runs = tmp_path / "runs"
    author = SimAuthor(runs, {"A": "honest", "B": "vacuous"})
    runner = SimRunner(runs)
    rep = _drive(runs, _spec(), author, runner)
    v = rep["verdict"]
    assert v["verdict"] == ab.DETECTED, v
    assert rep["cells"] == {"graded": 12}
    by = {e["expectation"]: e for e in v["expectations"]}
    assert by["power down (every brief)"]["counts"]["moved"] == 2
    assert v["diagnostics"]["power"]["a"]["k"] == 6 and v["diagnostics"]["power"]["b"]["k"] == 0
    assert v["diagnostics"]["repeatability"]["a"]["k"] == v["diagnostics"]["repeatability"]["b"]["k"] == 6
    # Each cell authored once and graded once; costs add up.
    assert set(author.calls.values()) == {1} and set(runner.calls.values()) == {1}
    assert rep["spent"]["total"] == pytest.approx(12 * 1.0 + 12 * 5.0)
    # Both arms of a trial got the same control: trial t rotates with t - 1.
    s = ab.load_state(ab.state_path(runs, "pc"))
    ctrl = {}
    for rec in s["cells"].values():
        doc = json.loads((runs / rec["manifest"]).read_text())
        assert doc["cell"]["experiment"] == "pc" and doc["cell"]["kind"] == "canonical"
        ctrl.setdefault((rec["case_id"], rec["trial"]), set()).add(
            doc["plan"]["control"]["control"])
    assert all(len(v) == 1 for v in ctrl.values())
    # The report's board has one row per arm.
    rows = {r["arm"]: r for r in rep["board"]["rows"]}
    assert rows["A@aaaaaaa"]["axes"]["power"]["k"] == 6
    assert rows["B@bbbbbbb"]["axes"]["power"]["k"] == 0
    assert "\n".join(ab.render_report(rep)).rstrip().endswith("(exit 0)")


def test_simulated_null_and_reversed_arms_are_missed(tmp_path):
    rep = _drive(tmp_path / "null", _spec(),
                 SimAuthor(tmp_path / "null", {"A": "honest", "B": "honest"}),
                 SimRunner(tmp_path / "null"))
    assert rep["verdict"]["verdict"] == ab.MISSED and rep["verdict"]["exit_code"] == 1
    rep = _drive(tmp_path / "rev", _spec(),
                 SimAuthor(tmp_path / "rev", {"A": "vacuous", "B": "honest"}),
                 SimRunner(tmp_path / "rev"))
    assert rep["verdict"]["verdict"] == ab.MISSED
    pooled = next(e for e in rep["verdict"]["expectations"] if e["expectation"] == "power down (pooled)")
    assert pooled["why"] == "B moved the other way"


def test_the_report_reads_only_its_own_experiment_cells(tmp_path):
    runs = tmp_path / "runs"
    rep = _drive(runs, _spec(), SimAuthor(runs, {"A": "honest", "B": "vacuous"}), SimRunner(runs))
    run_id = ab.load_state(ab.state_path(runs, "pc"))["run_id"]
    # A stray grade in the SAME run dir from another experiment (a different author
    # model, all honest on arm B) must not move this experiment's numbers.
    stray_plan = grader.plan_grade(grader.runner_case(_case(STUDY, vacuous=False)), STUDY)
    grader.write_manifest(
        grader.manifest_path(runs, run_id, "stray"), stray_plan,
        runner=grader.runner_fingerprint("codex-cli", "gpt-6-astra"), run_id=run_id,
        result=grader.grade(stray_plan, SimRunner(runs).metrics(stray_plan)),
        extra={"cell": board.cell_block(kind=board.CANONICAL, case_id=STUDY, trial=1,
                                        experiment="other", arm="B",
                                        author={"agent": "codex-cli", "model": "other"})})
    again = ab.report(runs, "pc")
    assert again["verdict"] == rep["verdict"]
    # ...and the experiment-filtered board leaves it out too.
    b = board.board_for(runs, experiment="pc")
    assert sum(r["artifacts"] for r in b["rows"]) == 12


def test_resume_after_an_injected_mid_run_fault_never_respends_a_finished_stage(tmp_path):
    runs = tmp_path / "runs"
    spec = _spec()
    cells = [c.key for c in ab.plan_cells(spec)]
    author = SimAuthor(runs, {"A": "honest", "B": "vacuous"},
                       fail={cells[1]: 1},                 # a retryable fault: retried inline
                       crash_after_write={cells[4]})       # the driver dies after the episode
    runner = SimRunner(runs, crash_mid_grade={cells[7]})
    with pytest.raises(Crash):
        _drive(runs, spec, author, runner)
    s = ab.load_state(ab.state_path(runs, "pc"))
    st = {k: r["status"] for k, r in s["cells"].items()}
    assert [st[k] for k in cells[:4]] == ["graded"] * 4
    assert st[cells[4]] == "pending" and st[cells[5]] == "pending"
    assert ab.report(runs, "pc")["verdict"]["verdict"] == ab.INCOMPLETE
    # The retried cell: two author attempts, one error recorded, graded once.
    assert author.calls[cells[1]] == 2
    assert [a.get("error", "") != "" for a in s["cells"][cells[1]]["attempts"]
            if a["stage"] == "author"] == [True, False]

    with pytest.raises(Crash):                              # resume #1: dies mid-grade
        _drive(runs, spec, author, runner)
    # The crashed author stage was RECOVERED from disk, not re-authored.
    assert author.calls[cells[4]] == 1
    s = ab.load_state(ab.state_path(runs, "pc"))
    assert any(a.get("recovered") for a in s["cells"][cells[4]]["attempts"])
    assert s["cells"][cells[7]]["status"] == "authored"

    rep = _drive(runs, spec, author, runner)                # resume #2: finishes
    assert rep["verdict"]["verdict"] == ab.DETECTED
    assert rep["cells"] == {"graded": 12}
    assert all(n == 1 for k, n in author.calls.items() if k != cells[1])
    assert runner.calls[cells[7]] == 2                      # the abandoned grade, redone once
    # ...and the abandoned grade's episodes stay on the bill.
    assert rep["spent"]["wasted"] == pytest.approx(2.0)
    # A raised author attempt's spend is unknown: it is charged the estimate.
    assert rep["spent"]["total"] == pytest.approx(12 * 1.0 + ab.EST_AUTHOR_COST
                                                  + 12 * 5.0 + 2.0)


def test_done_states_are_distinct_from_not_attempted(tmp_path):
    """no_case_created (the author saved nothing) and not_gradable (no controls) are
    DONE — graded or skipped, never re-attempted on resume; a cell that keeps failing is
    FAULTED after --max-attempts and counted."""
    runs = tmp_path / "runs"
    spec = _spec(briefs=[STUDY, BROWSE, UNDERIVED], trials=3)
    keys = [c.key for c in ab.plan_cells(spec)]
    stubborn = next(k for k in keys if k.startswith(f"A.{BROWSE}.t3"))
    author = SimAuthor(runs, {"A": "honest", "B": "none"}, fail={stubborn: 99})
    rep = _drive(runs, spec, author, SimRunner(runs))
    s = ab.load_state(ab.state_path(runs, "pc"))
    recs = s["cells"]
    assert {r["grade_status"] for r in recs.values() if r["arm"] == "B" and
            r["status"] == "graded"} == {grader.NO_CASE}
    skipped = [k for k, r in recs.items() if r["status"] == "skipped"]
    assert sorted(skipped) == sorted(k for k in keys if UNDERIVED in k)
    assert all("controls_not_derived" in recs[k]["skip_reason"] for k in skipped)
    assert not any(UNDERIVED in k for k in author.calls)          # never paid for
    assert recs[stubborn]["status"] == "faulted" and author.calls[stubborn] == 2
    assert rep["faulted_cells"] == {stubborn: recs[stubborn]["fault"]}
    # B saved nothing: power 0 on both scorable briefs — the miss of the brief no arm
    # could be graded on is UNSCORED, and 2 of 3 informative clears the 50% bar.
    each = next(e for e in rep["verdict"]["expectations"] if e["scope"] == "each")
    assert each["counts"] == {"moved": 2, "not_moved": 0, "floor": 0, "unscored": 1}
    # 1 faulted of 18 arm cells = 5.6% <= 10%; repeatability "flat" breaks (no_case is
    # False on every axis), so the positive control is MISSED here — by design.
    assert rep["verdict"]["verdict"] == ab.MISSED
    calls_before = dict(author.calls)
    rep2 = _drive(runs, spec, author, SimRunner(runs))
    assert author.calls == calls_before and rep2["verdict"] == rep["verdict"]


def test_cost_ceiling_stops_before_crossing_and_a_higher_ceiling_resumes(tmp_path):
    runs = tmp_path / "runs"
    spec = _spec()
    author = SimAuthor(runs, {"A": "honest", "B": "vacuous"}, cost=None)   # unpriced
    runner = SimRunner(runs, cost=5.0)
    rep = _drive(runs, spec, author, runner, max_cost=40.0)
    assert rep["verdict"]["verdict"] == ab.INCOMPLETE and rep["verdict"]["exit_code"] == 4
    assert rep["spent"]["total"] <= 40.0
    # An unpriced creation episode is charged the estimate, never $0.
    assert rep["spent"]["author"] == pytest.approx(ab.EST_AUTHOR_COST * len(author.calls))
    stop = rep["sessions"][-1]["stopped"]
    assert stop.startswith("cost ceiling") and "--max-cost $40.00" in stop
    done = rep["cells"].get("graded", 0)
    assert 0 < done < 12
    rep = _drive(runs, spec, author, runner, max_cost=1000.0)
    assert rep["verdict"]["verdict"] == ab.DETECTED and rep["cells"] == {"graded": 12}
    assert set(author.calls.values()) == {1} and set(runner.calls.values()) == {1}
    assert [s["max_cost"] for s in rep["sessions"]] == [40.0, 1000.0]


def test_smoke_cells_never_reach_the_canonical_board(tmp_path):
    runs = tmp_path / "runs"
    _drive(runs, _spec(name="smoke-1", trials=1, kind=board.SMOKE),
           SimAuthor(runs, {"A": "honest", "B": "vacuous"}), SimRunner(runs))
    recs = board.load_grades(runs)
    assert len(recs) == 4 and board.select(recs) == []
    assert len(board.select(recs, include_smoke=True)) == 4


def test_live_author_runs_the_create_cli_pinned_to_the_registered_shas(tmp_path):
    arm = ab.ArmSpec("B", "/w/QualGent-MCP@throwaway/x", "/w/DevLoop-MCP@main",
                     manifest={"qualgent_mcp": {"sha": "b" * 40}, "devloop": {"sha": "d" * 40}})
    la = ab.LiveAuthor(runs_dir=tmp_path, work_dir=tmp_path / "w", device="emulator-5558",
                       mcp_server="http://127.0.0.1:51871", agent="codex-cli",
                       model="gpt-6-astra")
    cmd = la.command(ab.Cell("B", STUDY, 2), arm)
    i = cmd.index("run")
    assert cmd[i:i + 3] == ["run", "--mode", "create"]
    assert cmd[cmd.index("--qualgent-mcp") + 1] == f"/w/QualGent-MCP@{'b' * 40}"
    assert cmd[cmd.index("--devloop") + 1] == f"/w/DevLoop-MCP@{'d' * 40}"
    assert cmd[cmd.index("--case") + 1] == STUDY and cmd[cmd.index("--trials") + 1] == "1"
    assert "--yes" in cmd and cmd[cmd.index("--arm-name") + 1] == "B"
    assert la.recover(ab.Cell("B", STUDY, 2)) is None          # no run-id file yet


def test_cli_refuses_without_spend_flags_and_reports_the_verdict_as_exit_code(tmp_path, capsys):
    runs = tmp_path / "runs"
    _drive(runs, _spec(), SimAuthor(runs, {"A": "honest", "B": "vacuous"}), SimRunner(runs))
    assert ab.main(["report", "--experiment", "pc", "--runs-dir", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "VERDICT: DETECTED" in out and "power down (every brief)" in out
    assert ab.main(["report", "--experiment", "nope", "--runs-dir", str(runs)]) == ab.EXIT_REFUSED
    # A new experiment needs both arms' pins.
    assert ab.main(["run", "--experiment", "fresh", "--runs-dir", str(runs)]) == ab.EXIT_REFUSED


# ── the alternatives to the owner's pre-registration (QUA-2859's finding) ─────

def test_five_versioned_positive_control_predictions_and_the_default_is_the_mechanism_v2():
    assert ab.POSITIVE_CONTROL.ref == "harmful-rule-positive-control/v1"
    assert {p.ref for p in set(ab.PREDICTIONS.values())} == {
        "harmful-rule-positive-control/v1", "harmful-rule-positive-control-stratified/v1",
        "harmful-rule-positive-control-aggregate/v1",
        "harmful-rule-positive-control-mechanism/v1",
        "harmful-rule-positive-control-mechanism/v2"}
    assert len({p.sha for p in set(ab.PREDICTIONS.values())}) == 5
    # The CLI default is the mechanism form with the manipulation check (QUA-2864); v1
    # stays selectable by its ref, and the bare name is the latest version.
    args = ab.build_parser().parse_args(["run", "--experiment", "x"])
    assert ab.load_prediction(args.prediction) is ab.POSITIVE_CONTROL_MECHANISM_V2
    assert ab.DEFAULT_PREDICTION is ab.POSITIVE_CONTROL_MECHANISM_V2
    assert ab.load_prediction("harmful-rule-positive-control-mechanism/v1") is \
        ab.POSITIVE_CONTROL_MECHANISM
    assert ab.load_prediction("harmful-rule-positive-control-mechanism") is \
        ab.POSITIVE_CONTROL_MECHANISM_V2
    # A version bump is a different registration.
    bumped = ab.Prediction(**{**ab.POSITIVE_CONTROL.__dict__, "version": 2})
    assert bumped.sha != ab.POSITIVE_CONTROL.sha
    state = ab.new_state(_spec(), run_id="r", ungated=True)
    assert any(d.startswith("prediction") for d in ab.registration_diff(
        state, _spec(prediction=ab.POSITIVE_CONTROL_STRATIFIED)))


def test_the_subset_is_eight_assert_and_four_walk_targets():
    subset = ab.load_subset()
    strata = [ab.target_stratum(b) for b in subset]
    assert strata.count(ab.ALIVE) == 8 and strata.count(ab.DEATH) == 4
    assert ab.target_stratum(STUDY) == ab.DEATH and ab.target_stratum(BROWSE) == ab.ALIVE
    groups = {b: ab.detection_group(b) for b in subset}
    assert sorted(groups.values()) == [ab.ASSERT] * 8 + [ab.WALK] * 4
    # On this subset the mechanism label and the death stratum agree brief for brief.
    assert all((groups[b] == ab.WALK) == (ab.target_stratum(b) == ab.DEATH) for b in subset)


def _harmful_grades(briefs, trials=3):
    """QUA-2859's simulation: A honest (power everywhere); B harmful (power lost on an
    alive target, kept on a death target)."""
    return {"A": {b: [_g(True)] * trials for b in briefs},
            "B": {b: [_g(ab.target_stratum(b) == ab.DEATH)] * trials for b in briefs}}


def test_on_the_full_subset_the_owners_form_is_missed_and_both_alternatives_detect():
    subset = ab.load_subset()
    g = _harmful_grades(subset)
    v = ab.evaluate(ab.POSITIVE_CONTROL, g, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.MISSED
    each = v["expectations"][0]
    assert each["counts"]["not_moved"] == 4 and each["counts"]["moved"] == 8
    v = ab.evaluate(ab.POSITIVE_CONTROL_STRATIFIED, g, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.DETECTED, v["why"]
    assert [e["outcome"] for e in v["expectations"]] == [ab.MET] * 5
    v = ab.evaluate(ab.POSITIVE_CONTROL_AGGREGATE, g, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.DETECTED, v["why"]       # 36/36 vs 12/36: a 67-point drop
    # The aggregate threshold is real: a 10-point drop is MISSED, not "close enough".
    small = {"A": {b: [_g(True)] * 3 for b in subset},
             "B": {b: [_g(i >= 2)] + [_g(True)] * 2 for i, b in enumerate(subset)}}
    v = ab.evaluate(ab.POSITIVE_CONTROL_AGGREGATE, small, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.MISSED and "< the registered 15%" in v["why"]
    # The stratified form still falsifies: harmful power dropping on a death target too
    # (an author that stopped walking the feature) breaks "flat on death targets".
    vac = {"A": {b: [_g(True)] * 3 for b in subset}, "B": {b: [_g(False)] * 3 for b in subset}}
    v = ab.evaluate(ab.POSITIVE_CONTROL_STRATIFIED, vac, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.MISSED and "death targets" in v["why"]


def test_simulated_harmful_author_end_to_end_under_each_registration(tmp_path):
    runs = tmp_path / "runs"
    # The owner's form, registered: the death-target brief does not move -> MISSED.
    rep = _drive(runs, _spec(name="owner", trials=6),
                 SimAuthor(runs, {"A": "honest", "B": "harmful"}), SimRunner(runs))
    assert rep["verdict"]["verdict"] == ab.MISSED and STUDY in rep["verdict"]["why"]
    assert rep["verdict"]["prediction"] == "harmful-rule-positive-control/v1"
    # The stratified form, registered BEFORE its own run: DETECTED.
    rep = _drive(runs, _spec(name="strat", trials=6, prediction=ab.POSITIVE_CONTROL_STRATIFIED),
                 SimAuthor(runs, {"A": "honest", "B": "harmful"}), SimRunner(runs))
    assert rep["verdict"]["verdict"] == ab.DETECTED, rep["verdict"]["why"]
    assert rep["verdict"]["strata"] == {STUDY: ab.DEATH, BROWSE: ab.ALIVE}


# ── what a rate may count ─────────────────────────────────────────────────────

def test_a_copy_of_the_reference_is_in_no_rate_but_is_counted():
    copy = {**_g(True), "contamination_risk": grader.CONTAMINATION_RISK}
    assert ab.axis_rate([copy, _g(False)], "power").n == 1
    v = ab.evaluate(ab.POSITIVE_CONTROL, {"A": {STUDY: [_g(True)] * 3, BROWSE: [_g(True)] * 3},
                                          "B": {STUDY: [_g(False)] * 3 + [copy],
                                                BROWSE: [_g(False)] * 3}},
                    BRIEFS, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.DETECTED and v["contamination_risk"] == {"A": 0, "B": 1}
    assert v["diagnostics"]["power"]["b"]["n"] == 6


def test_power_given_pass3_reads_power_only_among_repeatable_cases():
    impossible = {"status": "graded", "axes": {"power": True, "repeatability": False,
                                               "specificity": False, "lint": True,
                                               "strong": False, "strong_exec": False}}
    assert board.axis_value(impossible, "power") is True
    assert board.axis_value(impossible, "power_given_pass3") is None
    assert board.axis_value(_g(True), "power_given_pass3") is True
    assert board.axis_value(_g(False), "power_given_pass3") is False
    assert board.axis_value({"axes": {"power": "n/a", "repeatability": True}},
                            "power_given_pass3") == "n/a"


# ── the mechanism prediction (QUA-2862, the default since 2026-10-01) ──────────

def test_the_mechanism_prediction_is_declared_and_the_older_hashes_are_frozen():
    p = ab.POSITIVE_CONTROL_MECHANISM
    assert p.ref == "harmful-rule-positive-control-mechanism/v1"
    assert [(e.axis, e.direction, e.scope, e.stratum, e.test) for e in p.expectations] == [
        ("power", "down", "pooled", "assert", "fisher"),
        ("power", "flat", "pooled", "walk", "ci"),
        ("repeatability", "flat", "pooled", "assert", "ci"),
        ("repeatability", "flat", "pooled", "walk", "ci"),
        ("specificity", "flat", "pooled", "assert", "ci"),
        ("specificity", "flat", "pooled", "walk", "ci")]
    assert p.expectations[0].alpha == 0.05
    assert p.trials_by_group == {"assert": 2, "walk": 1}
    (pc,) = p.preconditions
    assert (pc.axis, pc.stratum, pc.min_a_rate, pc.min_scored) == ("power", "assert", 0.5, 12)
    # Round-trips through JSON (the registration) with the same hash.
    assert ab.Prediction.from_dict(json.loads(json.dumps(p.as_dict()))).sha == p.sha
    # The fields added for it did not move the registrations made before it.
    assert {q.ref: q.sha for q in (ab.POSITIVE_CONTROL, ab.POSITIVE_CONTROL_STRATIFIED,
                                   ab.POSITIVE_CONTROL_AGGREGATE)} == {
        "harmful-rule-positive-control/v1": "1d51711c1ffe",
        "harmful-rule-positive-control-stratified/v1": "a8c22b924d21",
        "harmful-rule-positive-control-aggregate/v1": "4954b155d9ee"}
    # Any change to the design or a precondition is a different registration.
    other = ab.Prediction(**{**p.__dict__, "trials": (("assert", 3), ("walk", 1))})
    assert other.sha != p.sha
    with pytest.raises(ValueError):
        ab.Expectation("power", "flat", "pooled", "assert", test="fisher")


def test_the_design_is_forty_cells_on_the_subset():
    subset = ab.load_subset()
    bt = ab.design_trials(ab.POSITIVE_CONTROL_MECHANISM, subset)
    assert sorted(bt.values()) == [1] * 4 + [2] * 8
    spec = ab.ExperimentSpec(**{**_spec(briefs=subset, prediction=ab.POSITIVE_CONTROL_MECHANISM,
                                        trials=2).__dict__, "brief_trials": bt})
    cells = ab.plan_cells(spec)
    assert len(cells) == 40
    walk = {b for b in subset if ab.detection_group(b) == ab.WALK}
    assert sum(1 for c in cells if c.case_id in walk) == 8
    assert all(c.trial == 1 for c in cells if c.case_id in walk)
    assert sum(1 for c in cells if c.case_id not in walk) == 32
    # The per-brief trials are part of the registration and survive a resume.
    state = ab.new_state(spec, run_id="r", ungated=True)
    again = ab.ExperimentSpec.from_dict(state["spec"])
    assert again.brief_trials == bt and ab.registration_diff(state, again) == []
    assert any(d.startswith("brief_trials") for d in ab.registration_diff(state, _spec(
        briefs=subset, prediction=ab.POSITIVE_CONTROL_MECHANISM, trials=2)))
    # A brief the design has no group for is refused, never silently given a count.
    with pytest.raises(ValueError, match="fit none"):
        ab.design_trials(ab.POSITIVE_CONTROL_MECHANISM, [*subset, "anki-browse-cards"])


def test_plan_shows_forty_cells_the_mechanism_prediction_and_a_cost_under_the_cap(capsys):
    import re
    assert ab.main(["--plan", "--prediction", "harmful-rule-positive-control-mechanism/v1"]) == 0
    out = capsys.readouterr().out
    assert "40 arm cell(s)" in out
    assert "assert (DROP group): 8 brief(s) × 2 trial(s) × 2 arms = 32 cell(s)" in out
    assert "walk (FLAT group): 4 brief(s) × 1 trial(s) × 2 arms = 8 cell(s)" in out
    assert f"prediction harmful-rule-positive-control-mechanism/v1 (sha " \
           f"{ab.POSITIVE_CONTROL_MECHANISM.sha})" in out
    cost = float(re.search(r"estimated cost at measured actuals: \$([\d.]+) for all 40",
                           out).group(1))
    assert cost <= 280 and cost == pytest.approx(40 * (ab.MEASURED_AUTHOR_COST
                                                        + ab.MEASURED_GRADE_COST))
    assert "ceiling --max-cost $280.00 (hard cap $300.00)" in out
    assert "plan only" in out


def test_the_cost_ceiling_defaults_to_280_and_above_300_is_refused(capsys):
    args = ab.build_parser().parse_args(["run", "--experiment", "x"])
    assert args.max_cost == ab.DEFAULT_MAX_COST == 280.0
    assert ab.main(["--plan", "--max-cost", "300.01"]) == ab.EXIT_REFUSED
    assert "above the hard cap" in capsys.readouterr().err
    assert ab.main(["run", "--experiment", "x", "--max-cost", "500"]) == ab.EXIT_REFUSED
    assert ab.main(["--plan", "--max-cost", "300"]) == 0


def test_fisher_exact_one_sided():
    assert ab.fisher_one_sided(3, 3, 0, 3) == pytest.approx(0.05)          # 1 / C(6,3)
    assert ab.fisher_one_sided(16, 16, 0, 16) == pytest.approx(1 / 601080390)
    assert ab.fisher_one_sided(5, 10, 5, 10) == pytest.approx(0.6718, abs=1e-4)
    assert ab.fisher_one_sided(0, 3, 3, 3, "up") == pytest.approx(0.05)
    assert ab.fisher_one_sided(0, 0, 0, 3) == 1.0
    assert ab.judge_fisher("down", _r(16, 16), _r(4, 16))[0] == ab.MET
    assert ab.judge_fisher("down", _r(10, 16), _r(8, 16))[0] == ab.NOT_MET
    assert ab.judge_fisher("down", _r(8, 16), _r(10, 16))[1].startswith("B moved the other way")


def _mech(assert_a, assert_b, walk_a=True, walk_b=True, *, n=2, rep_b=True, drop=None):
    """Grades for 8 assert + 4 walk briefs (synthetic ids, groups passed explicitly)."""
    assert_briefs = [f"a{i}" for i in range(8)]
    walk_briefs = [f"w{i}" for i in range(4)]
    groups = {**{b: ab.ASSERT for b in assert_briefs}, **{b: ab.WALK for b in walk_briefs}}
    g = {"A": {}, "B": {}}
    for b in assert_briefs:
        g["A"][b] = [_g(assert_a)] * n
        g["B"][b] = [_g(assert_b, repeat=rep_b)] * n
    for b in walk_briefs:
        g["A"][b] = [_g(walk_a)]
        g["B"][b] = [_g(walk_b)]
    if drop:
        for b in assert_briefs[:drop]:
            g["A"][b] = g["B"][b] = []
    briefs = assert_briefs + walk_briefs
    return g, briefs, {"groups": groups, "strata": {b: ab.ALIVE for b in briefs}}


def test_mechanism_detected_missed_and_inconclusive():
    P = ab.POSITIVE_CONTROL_MECHANISM
    g, briefs, kw = _mech(True, False)
    v = ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v["verdict"] == ab.DETECTED, v["why"]
    assert v["expectations"][0]["p_value"] < 0.05
    assert v["by_group"]["assert"]["power"]["b"]["k"] == 0
    # A no-op arm B: nothing drops on the DROP group -> MISSED, never DETECTED.
    g, briefs, kw = _mech(True, True)
    v = ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v["verdict"] == ab.MISSED and "assert targets" in v["why"]
    # Two-sided: power dropping on the WALK group too is a miss (the author stopped
    # walking the feature — not the mechanism predicted).
    g, briefs, kw = _mech(True, False, walk_a=True, walk_b=False)
    v = ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v["verdict"] == ab.MISSED and "walk targets" in v["why"]
    # Repeatability moving on the DROP group is a miss.
    g, briefs, kw = _mech(True, False, rep_b=False)
    v = ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v["verdict"] == ab.MISSED and "repeatability flat (pooled, assert" in v["why"]
    # Arm A had nothing to remove: INCONCLUSIVE before any expectation is read.
    g, briefs, kw = _mech(False, False)
    v = ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v["verdict"] == ab.INCONCLUSIVE and "nothing for arm B to remove" in v["why"]
    # Exclusions left fewer than 12 scored DROP cells per arm: INCONCLUSIVE.
    g, briefs, kw = _mech(True, False, drop=3)                     # 5 briefs x 2 = 10
    v = ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v["verdict"] == ab.INCONCLUSIVE and "fewer than 12 per arm" in v["why"]
    assert v["preconditions"][0]["met"] is False
    # Unfinished: no verdict at all.
    g, briefs, kw = _mech(True, False)
    assert ab.evaluate(P, g, briefs, arm_a="A", arm_b="B", complete=False,
                       **kw)["verdict"] == ab.INCOMPLETE


def test_simulated_mechanism_experiment_end_to_end_with_per_group_trials(tmp_path):
    """The driver runs the design's trials per group (BROWSE assert x 2, STUDY walk x 1),
    and the report judges the registration it froze."""
    runs = tmp_path / "runs"
    p = ab.Prediction(**{**ab.POSITIVE_CONTROL_MECHANISM.__dict__,
                         "name": "mechanism-small",
                         "preconditions": (ab.Precondition("power", ab.ASSERT, 0.5, 2),)})
    bt = ab.design_trials(p, BRIEFS)
    assert bt == {STUDY: 1, BROWSE: 2}
    spec = ab.ExperimentSpec(**{**_spec(name="mech", trials=2, prediction=p).__dict__,
                                "brief_trials": bt})
    author = SimAuthor(runs, {"A": "honest", "B": "harmful"})
    rep = _drive(runs, spec, author, SimRunner(runs))
    assert rep["cells"] == {"graded": 6}
    assert sorted(author.calls) == sorted([f"A.{STUDY}.t1", f"B.{STUDY}.t1", f"A.{BROWSE}.t1",
                                           f"B.{BROWSE}.t1", f"A.{BROWSE}.t2",
                                           f"B.{BROWSE}.t2"])
    v = rep["verdict"]
    assert v["groups"] == {STUDY: ab.WALK, BROWSE: ab.ASSERT}
    # 2 vs 2 cells cannot reach p < 0.05 (p = 1/6): honest-but-small is a MISS, not a pass.
    assert v["verdict"] == ab.MISSED and "Fisher exact one-sided p = 0.167" in v["why"]
    text = "\n".join(ab.render_report(rep))
    assert "detection: 1 assert, 1 walk" in text and "pre-registered preconditions:" in text
    assert "trials 1 brief(s) × 2, 1 brief(s) × 1" in text


# ── the scripted adversary authors (QUA-2859) under the mechanism prediction ──

def _adversary():
    import importlib.util
    import sys
    key = "_test_create_adversary_check_ab"
    if key not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "scripts" / "create_adversary_check.py"
        spec = importlib.util.spec_from_file_location(key, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


@pytest.fixture(scope="module")
def adversary_on_subset():
    """QUA-2859's scripted authors on the 12-brief subset, two control trials each,
    through the real fake API and the real grader, on the REAL derived controls
    (QUA-2854: every subset target has them)."""
    adv = _adversary()
    return adv.run_check(adv.load_briefs(subset=True), trials=2, provisional=False)


def test_the_harmful_rule_author_is_detected_and_a_noop_arm_is_not(adversary_on_subset):
    res = adversary_on_subset
    assert res.ok, res.failures
    assert len(res.gradable) == 12 and not res.not_gradable and not res.provisional
    harmful, noop = res.mechanism["harmful-rule"], res.mechanism["no-op"]
    assert harmful["prediction"] == "harmful-rule-positive-control-mechanism/v1"
    assert harmful["verdict"] == ab.DETECTED, harmful["why"]
    assert all(r["outcome"] == ab.MET for r in harmful["expectations"])
    assert harmful["by_group"]["assert"]["power"]["a"]["k"] == 16
    assert harmful["by_group"]["assert"]["power"]["b"]["k"] == 0
    assert harmful["by_group"]["walk"]["power"]["b"]["k"] == 4
    assert noop["verdict"] in (ab.MISSED, ab.INCONCLUSIVE), noop["why"]
    # The simulation's drops and holds line up with the metadata-derived labels — the
    # labels were never read off the simulation.
    assert res.prediction["drops_are_assert_targets"] and res.prediction["holds_are_walk_targets"]


def test_the_vacuous_author_as_arm_b_breaks_the_flat_group(adversary_on_subset):
    """An arm B that stops walking the feature loses power on the walk group too: the
    two-sided prediction calls that MISSED, not DETECTED."""
    adv = _adversary()
    res = adversary_on_subset
    real = res.grades["harmful-rule"]
    try:
        res.grades["harmful-rule"] = res.grades["vacuous"]
        v = adv.mechanism_verdicts(res)["harmful-rule"]
    finally:
        res.grades["harmful-rule"] = real
    assert v["verdict"] == ab.MISSED and "walk targets" in v["why"]


# ── review fixes (QUA-2850 epic review) ───────────────────────────────────────

def _creation_result(runs: Path, metrics: dict, *, with_case: bool = True):
    from types import SimpleNamespace
    ep = runs / "create-x" / "ep1"
    ep.mkdir(parents=True, exist_ok=True)
    if with_case:
        (ep / "authored_case.json").write_text(json.dumps(_case(STUDY, vacuous=False)))
    return SimpleNamespace(metrics=metrics, artifact_dir="create-x/ep1", wall_time_sec=60.0,
                           provenance={"create": {"arm": {"name": "A"}}})


def test_creation_outcomes_excluded_retry_flagged_cases_are_graded(tmp_path):
    """An excluded creation episode (env failure, contamination) measured nothing and is
    retried; a saved case the runner flags `dead`/`off_app` is still GRADED by execution
    (the flag is a transcript heuristic), with its flags carried on the cell."""
    runs = tmp_path / "runs"
    ok = ab._outcome_from_result(_creation_result(runs, {
        "outcome": "case_created", "valid_case": True, "validity_flags": ["truncated"],
        "cost_usd": 0.9}), runs)
    assert ok.outcome == "case_created" and ok.artifact and not ok.excluded
    assert ok.cost_usd == 0.9

    dead = ab._outcome_from_result(_creation_result(runs, {
        "outcome": "case_created", "valid_case": False, "validity_flags": ["dead"]}), runs)
    assert dead.outcome == "case_created" and dead.artifact and not dead.excluded
    assert dead.validity_flags == ["dead"]

    env = ab._outcome_from_result(_creation_result(runs, {
        "outcome": "no_case_created", "valid_case": False, "env_failure": True,
        "validity_flags": ["env_failure", "no_case"]}, with_case=False), runs)
    assert env.excluded.startswith("env_failure") and env.outcome == "no_case_created"

    tainted = ab._outcome_from_result(_creation_result(runs, {
        "outcome": "case_created", "valid_case": False, "contaminated": True,
        "contamination_reasons": ["qualgent_api_bypass"],
        "validity_flags": ["contaminated"]}), runs)
    assert tainted.excluded.startswith("contaminated")          # excluded, not a zero

    none = ab._outcome_from_result(_creation_result(runs, {
        "outcome": "no_case_created", "valid_case": False, "no_case_reason": "asked_instead",
        "validity_flags": ["no_case"]}, with_case=False), runs)
    assert none.outcome == "no_case_created" and none.artifact is None
    assert "asked_instead" in none.reason


def test_the_report_counts_creation_flags_per_arm_and_judges_on_frozen_labels(tmp_path):
    runs = tmp_path / "runs"

    class FlaggedB(SimAuthor):
        def _outcome(self, cell):
            out = super()._outcome(cell)
            if cell.arm == "B":
                out.validity_flags = ["dead"]
            return out

    spec = _spec(trials=1)
    rep = _drive(runs, spec, FlaggedB(runs, {"A": "honest", "B": "honest"}), SimRunner(runs))
    assert rep["cells"] == {"graded": 4}                         # flagged, still graded
    assert rep["creation_flags"] == {"A": {}, "B": {"dead": 2}}
    assert any("creation flags, arm B" in line for line in ab.render_report(rep))
    # The labels the verdict reads were frozen at registration.
    state = ab.load_state(ab.state_path(runs, spec.name))
    assert state["labels"]["groups"] == {b: ab.detection_group(b) for b in BRIEFS}
    state["labels"]["groups"] = {b: "walk" for b in BRIEFS}
    ab.save_state(ab.state_path(runs, spec.name), state)
    assert ab.report(runs, spec.name)["verdict"]["groups"] == {b: "walk" for b in BRIEFS}


def test_consecutive_faults_trip_the_breaker_and_retry_faulted_resumes(tmp_path):
    """An outage faults every cell the same way: two in a row stop the session
    INCOMPLETE instead of burning the design; --retry-faulted gives them fresh attempts."""
    runs = tmp_path / "runs"
    spec = _spec()
    keys = [c.key for c in ab.plan_cells(spec)]
    author = SimAuthor(runs, {"A": "honest", "B": "vacuous"},
                       fail={k: 99 for k in keys[2:]})          # the device dies after cell 2
    rep = _drive(runs, spec, author, SimRunner(runs))
    assert rep["verdict"]["verdict"] == ab.INCOMPLETE
    assert rep["sessions"][-1]["stopped"].startswith("circuit breaker")
    assert rep["cells"] == {"graded": 2, "faulted": 2, "pending": 8}
    assert set(author.calls) == set(keys[:4])                  # nothing after the stop

    author.fail.clear()                                         # the device is back
    path = ab.state_path(runs, spec.name)
    d = ab.Driver(spec=spec, runs_dir=runs, author=author, runner=SimRunner(runs),
                  max_cost=1000.0, state=ab.load_state(path))
    assert sorted(d.retry_faulted()) == sorted(keys[2:4])
    asyncio.run(d.run())
    rep = ab.report(runs, spec.name)
    assert rep["verdict"]["verdict"] == ab.DETECTED and rep["cells"] == {"graded": 12}
    rec = ab.load_state(path)["cells"][keys[2]]
    assert [(a["stage"], a.get("superseded", False)) for a in rec["attempts"]] == [
        ("author", True), ("author", True), ("author", False), ("grade", False)]
    assert rec["previous_fault"].startswith("author:")
    # The superseded attempts' (estimated) spend is still on the bill.
    assert rep["spent"]["author"] == pytest.approx(12 * 1.0 + 4 * ab.EST_AUTHOR_COST)


def test_a_resume_under_another_corpus_or_runner_is_refused(tmp_path, capsys):
    runs = tmp_path / "runs"
    spec = _spec(trials=1)
    _drive(runs, spec, SimAuthor(runs, {"A": "honest", "B": "vacuous"}), SimRunner(runs))
    path = ab.state_path(runs, spec.name)
    state = ab.load_state(path)
    assert state["environment"]["corpus_version"]
    assert state["environment"]["runner"]["grader_version"] == grader.GRADER_VERSION
    assert ab.environment_diff(state, spec) == []
    state["environment"]["corpus_version"] = "000000000000"
    ab.save_state(path, state)
    assert ab.environment_diff(state, spec)
    # A bare resume (no arm flags) is refused before anything is probed.
    assert ab.main(["run", "--experiment", spec.name, "--runs-dir", str(runs)]) == ab.EXIT_REFUSED
    assert "corpus_version" in capsys.readouterr().err


def test_a_paid_run_refuses_codex_without_an_api_key(tmp_path, monkeypatch):
    from qualgentbench.adapters.codex_cli import CodexCliAdapter
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path / "operator_codex"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(home))
    spec = _spec()
    mode, problem = ab.agent_auth_check(spec, allow_login=False)
    assert mode == "account_login" and "--allow-codex-login" in problem
    assert ab.agent_auth_check(spec, allow_login=True) == ("account_login", "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert ab.agent_auth_check(spec, allow_login=False) == ("api_key", "")


# ── v2: the manipulation check (QUA-2864) ──────────────────────────────────────

def test_v2_is_registered_beside_v1_and_v1_keeps_its_hash():
    v1, v2 = ab.POSITIVE_CONTROL_MECHANISM, ab.POSITIVE_CONTROL_MECHANISM_V2
    # QUA-2861 registered v1 at this hash (docs/createbench-v2-validation.md): frozen.
    assert v1.sha == "fcec04cefb3f" and v1.uptake is None and "uptake" not in v1.as_dict()
    assert v2.ref == "harmful-rule-positive-control-mechanism/v2" and v2.sha != v1.sha
    assert v2.expectations == v1.expectations
    assert v2.trials_by_group == {"assert": 4, "walk": 1}
    (pc,) = v2.preconditions
    assert (pc.axis, pc.stratum, pc.min_a_rate, pc.min_scored) == ("power", "assert", 0.5, 12)
    assert (v2.uptake.rule, v2.uptake.stratum, v2.uptake.min_rate) == ("app-open/v2",
                                                                       "assert", 0.8)
    again = ab.Prediction.from_dict(json.loads(json.dumps(v2.as_dict())))
    assert again.sha == v2.sha and again.uptake == v2.uptake
    # The threshold is part of the registration.
    loose = ab.Prediction(**{**v2.__dict__, "uptake": ab.UptakeCheck("app-open/v2",
                                                                      min_rate=0.5)})
    assert loose.sha != v2.sha
    with pytest.raises(ValueError, match="uptake rule"):
        ab.UptakeCheck("no-such-rule")


def test_v2_runs_on_its_own_subset_and_refuses_a_drop_brief_its_rule_cannot_catch():
    v2 = ab.POSITIVE_CONTROL_MECHANISM_V2
    subset = ab.load_subset(ab.default_subset(v2))
    assert ab.default_subset(ab.POSITIVE_CONTROL_MECHANISM) == ab.DEFAULT_SUBSET
    assert ab.check_design(v2, subset) == []
    groups = {b: ab.detection_group(b) for b in subset}
    assert sorted(groups.values()) == [ab.ASSERT] * 4 + [ab.WALK] * 4
    assert "contacts-favorite" not in subset         # leaked in the QUA-2864 probe
    bt = ab.design_trials(v2, subset)
    spec = ab.ExperimentSpec(**{**_spec(briefs=subset, prediction=v2, trials=4).__dict__,
                                "brief_trials": bt})
    cells = ab.plan_cells(spec)
    assert len(cells) == 40
    assert sum(1 for c in cells if groups[c.case_id] == ab.ASSERT) == 32   # 16 per arm
    # v1's subset carries navigation DROP briefs: refused under v2, named one by one.
    bad = ab.check_design(v2, ab.load_subset())
    assert len(bad) == 4 and all("class navigation" in b for b in bad)
    assert ab.check_design(ab.POSITIVE_CONTROL_MECHANISM, ab.load_subset()) == []


def test_plan_for_v2_shows_40_cells_the_rule_and_the_uptake_precondition(capsys):
    assert ab.main(["--plan"]) == 0
    out = capsys.readouterr().out
    assert "40 arm cell(s)" in out
    assert "assert (DROP group): 4 brief(s) × 4 trial(s) × 2 arms = 32 cell(s)" in out
    assert "precondition: arm B uptake of app-open/v2 >= 0.8 (assert targets)" in out
    assert "rule app-open/v2: Required final step" in out
    assert ab.main(["--plan", "--briefs", str(ab.DEFAULT_SUBSET)]) == ab.EXIT_REFUSED
    assert "cannot run on these briefs" in capsys.readouterr().err


def _uptake(briefs: list[str], groups: dict, *, a: float = 0.0, b: float = 1.0, n: int = 2):
    """{arm: {brief: [taken, ...]}}: arm B takes the rule on a `b` share of DROP cells."""
    out: dict[str, dict[str, list[bool]]] = {"A": {}, "B": {}}
    drop = [x for x in briefs if groups[x] == ab.ASSERT]
    k_b, k_a = round(b * len(drop) * n), round(a * len(drop) * n)
    for i, x in enumerate(drop):
        out["A"][x] = [i * n + j < k_a for j in range(n)]
        out["B"][x] = [i * n + j < k_b for j in range(n)]
    for x in briefs:
        if groups[x] == ab.WALK:
            out["A"][x], out["B"][x] = [False], [b > 0]
    return out


def test_v2_non_taking_arm_b_is_inconclusive_never_missed():
    """QUA-2861's run: the rule reached the author and was not followed, so the arms
    were the same arm twice. v1 reads that as MISSED; v2 as INCONCLUSIVE."""
    g, briefs, kw = _mech(True, True)                     # power did not move
    up = _uptake(briefs, kw["groups"], b=0.0)
    v1 = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM, g, briefs, arm_a="A", arm_b="B", **kw)
    assert v1["verdict"] == ab.MISSED
    v2 = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM_V2, g, briefs, arm_a="A", arm_b="B",
                     uptake_cells=up, **kw)
    assert v2["verdict"] == ab.INCONCLUSIVE and v2["exit_code"] == 3
    assert "treatment not delivered: arm B took app-open/v2 on 0/16" in v2["why"]
    assert v2["preconditions"][0]["kind"] == "uptake" and not v2["preconditions"][0]["met"]
    # Just under the threshold is still not delivered; at it, the expectations decide.
    v = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM_V2, g, briefs, arm_a="A", arm_b="B",
                    uptake_cells=_uptake(briefs, kw["groups"], b=0.75), **kw)
    assert v["verdict"] == ab.INCONCLUSIVE and "12/16" in v["why"]
    v = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM_V2, g, briefs, arm_a="A", arm_b="B",
                    uptake_cells=_uptake(briefs, kw["groups"], b=0.8125), **kw)
    # Delivered, and power still did not drop: now a MISSED means the benchmark missed.
    assert v["verdict"] == ab.MISSED and "power down" in v["why"]
    # No classified cell at all is not a delivery either.
    v = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM_V2, g, briefs, arm_a="A", arm_b="B",
                    uptake_cells={}, **kw)
    assert v["verdict"] == ab.INCONCLUSIVE and "no arm-B assert cell" in v["why"]


def test_v2_full_uptake_harmful_author_is_detected_and_a_no_op_is_not():
    g, briefs, kw = _mech(True, False)
    v = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM_V2, g, briefs, arm_a="A", arm_b="B",
                    uptake_cells=_uptake(briefs, kw["groups"], b=1.0), **kw)
    assert v["verdict"] == ab.DETECTED, v["why"]
    assert v["uptake"]["rule"] == "app-open/v2"
    assert v["uptake"]["arms"]["B"]["assert"]["k"] == 16
    assert v["uptake"]["arms"]["A"]["assert"]["k"] == 0
    # The brief-level view rides beside the verdict: 8 briefs, B below A on all 8.
    bp = v["brief_power"]["assert"]
    assert (bp["b_below_a"], bp["judged"]) == (8, 8) and len(bp["briefs"]) == 8
    assert v["brief_power"]["walk"]["b_below_a"] == 0
    # A no-op arm B: same cases as arm A, so no uptake and no drop — never DETECTED.
    g, briefs, kw = _mech(True, True)
    v = ab.evaluate(ab.POSITIVE_CONTROL_MECHANISM_V2, g, briefs, arm_a="A", arm_b="B",
                    uptake_cells=_uptake(briefs, kw["groups"], b=0.0), **kw)
    assert v["verdict"] != ab.DETECTED and v["verdict"] == ab.INCONCLUSIVE


def _small_v2(**kw) -> ab.Prediction:
    """v2's shape on the two-brief sim (STUDY walk, BROWSE assert): small thresholds."""
    return ab.Prediction(**{**ab.POSITIVE_CONTROL_MECHANISM_V2.__dict__, "name": "v2-small",
                            "expectations": (ab.Expectation("power", ab.DOWN, ab.POOLED,
                                                            ab.ASSERT, test=ab.FISHER,
                                                            alpha=0.2),
                                             ab.Expectation("power", ab.FLAT, ab.POOLED,
                                                            ab.WALK)),
                            "trials": ((ab.ASSERT, 2), (ab.WALK, 1)),
                            "preconditions": (ab.Precondition("power", ab.ASSERT, 0.5, 2),),
                            **kw})


@pytest.mark.parametrize("policy, verdict, taken", [
    ("app-open", ab.DETECTED, 2),          # scripted full uptake of the v2 rule
    ("harmful", ab.INCONCLUSIVE, 0),       # follows QUA-2861's title rule: not v2's
    ("honest", ab.INCONCLUSIVE, 0),        # a no-op arm B
])
def test_v2_end_to_end_reads_uptake_off_the_authored_cases(tmp_path, policy, verdict, taken):
    runs = tmp_path / "runs"
    p = _small_v2()
    spec = ab.ExperimentSpec(**{**_spec(name="v2", trials=2, prediction=p).__dict__,
                                "brief_trials": ab.design_trials(p, BRIEFS)})
    rep = _drive(runs, spec, SimAuthor(runs, {"A": "honest", "B": policy}), SimRunner(runs))
    v = rep["verdict"]
    assert v["verdict"] == verdict, v["why"]
    assert v["uptake"]["arms"]["B"]["assert"]["k"] == taken
    assert v["uptake"]["arms"]["B"]["assert"]["n"] == 2
    if verdict == ab.INCONCLUSIVE:
        assert "treatment not delivered" in v["why"]
    text = "\n".join(ab.render_report(rep))
    assert "uptake of app-open/v2" in text and "diagnostic" not in text
    assert "power per brief, assert group" in text
    # Every graded cell's manifest records its classification, and the board counts it.
    row = next(r for r in rep["board"]["rows"] if r["arm"].startswith("B"))
    assert row["uptake"]["app-open/v2"]["assert"] == {"k": taken, "n": 2}
    assert "uptake        app-open/v2:" in "\n".join(board.render_text(rep["board"]))


def test_a_v1_report_prints_uptake_of_its_own_rule_as_a_diagnostic(tmp_path):
    """QUA-2861's registration has no check: its report still says how many arm-B
    cases took the title rule, labelled a diagnostic, and the verdict ignores it."""
    runs = tmp_path / "runs"
    rep = _drive(runs, _spec(), SimAuthor(runs, {"A": "honest", "B": "harmful"}),
                 SimRunner(runs))
    v = rep["verdict"]
    assert v["uptake"]["rule"] == "screen-title/v1"
    assert v["uptake"]["arms"]["B"]["all"]["k"] == 6 and v["uptake"]["arms"]["A"]["all"]["k"] == 0
    assert not any(pc.get("kind") == "uptake" for pc in v["preconditions"])
    assert "(authored cases that take the rule, k/n; diagnostic, not registered)" in \
        "\n".join(ab.render_report(rep))
