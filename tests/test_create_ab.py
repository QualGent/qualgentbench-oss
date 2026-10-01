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

from qualgentbench.create import ab, board, grader

STUDY = "anki-study-first-card"
BROWSE = "anki-open-card-from-browser"
UNDERIVED = "cal-search-event"          # fossify-calendar: controls not derived (2026-09-30)
BRIEFS = [STUDY, BROWSE]
HARMFUL_STEP = "Verify the current screen's title is visible"


def _case(case_id: str, *, vacuous: bool, harmful: bool = False) -> dict:
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
                      harmful=self.policy[cell.arm] == "harmful")))
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
        vacuous = plan.case.steps[-1] == HARMFUL_STEP and not (
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
    # The default subset is QUA-2853's 15 briefs; 3 trials x 2 arms = 90 cells.
    spec = _spec(briefs=ab.load_subset())
    assert len(spec.briefs) == 15 and len(ab.plan_cells(spec)) == 90
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

def test_three_versioned_positive_control_predictions_and_the_default_is_the_owners():
    assert ab.POSITIVE_CONTROL.ref == "harmful-rule-positive-control/v1"
    assert {p.ref for p in set(ab.PREDICTIONS.values())} == {
        "harmful-rule-positive-control/v1", "harmful-rule-positive-control-stratified/v1",
        "harmful-rule-positive-control-aggregate/v1"}
    assert len({p.sha for p in set(ab.PREDICTIONS.values())}) == 3
    # The CLI default is the owner's literal form, not an alternative.
    args = ab.build_parser().parse_args(["run", "--experiment", "x"])
    assert ab.load_prediction(args.prediction) is ab.POSITIVE_CONTROL
    # A version bump is a different registration.
    bumped = ab.Prediction(**{**ab.POSITIVE_CONTROL.__dict__, "version": 2})
    assert bumped.sha != ab.POSITIVE_CONTROL.sha
    state = ab.new_state(_spec(), run_id="r", ungated=True)
    assert any(d.startswith("prediction") for d in ab.registration_diff(
        state, _spec(prediction=ab.POSITIVE_CONTROL_STRATIFIED)))


def test_the_subset_is_five_alive_and_ten_death_targets():
    strata = [ab.target_stratum(b) for b in ab.load_subset()]
    assert strata.count(ab.ALIVE) == 5 and strata.count(ab.DEATH) == 10
    assert ab.target_stratum(STUDY) == ab.DEATH and ab.target_stratum(BROWSE) == ab.ALIVE


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
    assert each["counts"]["not_moved"] == 10 and each["counts"]["moved"] == 5
    v = ab.evaluate(ab.POSITIVE_CONTROL_STRATIFIED, g, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.DETECTED, v["why"]
    assert [e["outcome"] for e in v["expectations"]] == [ab.MET] * 5
    v = ab.evaluate(ab.POSITIVE_CONTROL_AGGREGATE, g, subset, arm_a="A", arm_b="B")
    assert v["verdict"] == ab.DETECTED, v["why"]       # 45/45 vs 30/45: a 33-point drop
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
