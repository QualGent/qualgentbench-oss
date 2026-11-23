"""Replay from the report alone: quality gate G4 (QUA-3591).

Everything here is device-free and model-free. The replay runner is driven by a fake
executor that writes the `result.json` a real replay episode would, so the fold, the
ceiling, the control and the gate are exercised on the real file formats. The corpus
used is the packaged public one (openScale's three reference reports).
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import itertools
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_bugs import _obs, _transcript

from qualgentbench import bugs, cli, episode_runner, journey, reference_reports, rescore
from qualgentbench import quality_replay as qr
from qualgentbench import report_quality as rq
from qualgentbench.result import RunResult

APP = "openscale"
SIDE = ("openscale-user-profile", "user-age-off-by-one")              # a display bug
SCREEN = ("openscale-delete-measurement", "measurement-delete-broken")
OTHER = ("openscale-edit-measurement", "edit-skips-derived-recalc")
CONFIG = qr.ReplayConfig(agent="fake-agent", model="fake-model")
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@functools.cache
def _suite() -> dict:
    return next(s for s in bugs.load_apps() if s["app"]["id"] == APP)


def _ref(case_id: str, defect_id: str) -> reference_reports.ReferenceReport:
    return next(r for r in reference_reports.load(APP)
                if (r.case_id, r.defect_id) == (case_id, defect_id))


def _unit(kind=qr.CEILING, pair=SIDE, attempt=1, **extra) -> qr.ReplayUnit:
    ref = _ref(*pair)
    return qr.ReplayUnit(kind=kind, app_id=APP, case_id=pair[0], defect_id=pair[1],
                         attempt=attempt,
                         report=ref.customer_report(with_steps=kind != qr.CONTROL), **extra)


# ── the verdict ────────────────────────────────────────────────────────────────

def _decide(**kw):
    base = {"defect_id": "bug-a", "truth_kind": "screen", "truth_texts": ["Total: 3 items"],
            "fired": ["bug-a"], "device_results": ["cart total: 3 items checkout"]}
    return qr.decide(**{**base, **kw})


def test_reproduced_needs_the_canary_and_a_truth_text():
    both = _decide()
    assert both["reproduced"] is True and both["basis"] == qr.CANARY_AND_TEXT
    assert both["canary_fired"] is True and both["symptom_seen"] is True
    assert both["symptom_source"] == "device_text" and both["texts_seen"] == ["Total: 3 items"]

    canary_only = _decide(device_results=["cart total: 4 items"])
    assert canary_only["reproduced"] is False and "none of the defect's texts" in canary_only["reason"]

    text_only = _decide(fired=[])
    assert text_only["reproduced"] is False and text_only["symptom_seen"] is True
    assert "canary did not fire" in text_only["reason"]

    other_canary = _decide(fired=["bug-b"])
    assert other_canary["reproduced"] is False and other_canary["canary_fired"] is False

    assert _decide(fired=[], device_results=[])["reproduced"] is False


def test_a_truth_text_matches_on_token_boundaries_only():
    assert _decide(device_results=["total: 30 items"])["symptom_seen"] is False
    assert _decide(truth_texts=["Due <date>"], device_results=["due sep 16"])["symptom_seen"]


def test_an_unreadable_canary_gives_no_verdict():
    out = _decide(fired=None)
    assert out["reproduced"] is None and out["canary_fired"] is None


@pytest.mark.parametrize("kind", ["state", "absence"])
def test_a_defect_with_no_text_to_find_is_judged_on_the_canary_alone(kind):
    fired = _decide(truth_kind=kind, truth_texts=[])
    assert fired["basis"] == qr.CANARY_ONLY and fired["reproduced"] is True
    assert fired["symptom_seen"] is None
    assert _decide(truth_kind=kind, truth_texts=[], fired=[])["reproduced"] is False
    # The other policy: no verdict at all, so the defect is left out of G4.
    assert _decide(truth_kind=kind, truth_texts=[], textless="exclude")["reproduced"] is None


def test_a_crash_the_harness_recorded_is_the_symptom_of_a_death_defect():
    out = _decide(truth_kind="death", truth_texts=["NoSuchElementException"],
                  device_results=["home screen"], app_crashes=1)
    assert out["reproduced"] is True and out["symptom_source"] == "harness_crash"
    # Only for a death defect, and never without the canary.
    assert _decide(device_results=["x"], app_crashes=1)["reproduced"] is False
    assert _decide(truth_kind="death", truth_texts=["X"], device_results=[], app_crashes=1,
                   fired=[])["reproduced"] is False


# ── the replay task and its brief ──────────────────────────────────────────────

def test_a_replay_task_is_the_seeded_case_with_an_opaque_id_and_twice_the_route():
    unit = _unit()
    task = qr.build_task(unit, _suite(), CONFIG)
    spec = task.bug_spec
    seeded = next(t for t in journey.journey_tasks(_suite())
                  if t.id == journey.task_id(SIDE[0], "seeded"))
    # Staged exactly like the journey episode: same mode, flags, fixtures, oracle.
    for key in ("mode", "version", "active_bugs", "device_setup", "shared_storage", "oracle"):
        assert spec[key] == seeded.bug_spec[key]
    assert spec["version"] == "seeded" and SIDE[1] in spec["active_bugs"]
    assert spec["step_budget"] == 2 * spec["ref_steps"]
    # Nothing the replayer can see names the case or the defect.
    assert task.id.startswith("replay-") and len(task.id) == len("replay-") + 12
    assert episode_runner.agent_visible_task_id(task.id) == task.id
    assert SIDE[0] not in task.id and SIDE[1] not in task.id and "~" not in task.id
    block = spec[qr.KEY]
    assert block["unit"]["key"] == unit.key and block["replayer"] == CONFIG.fingerprint()
    assert block["truth_kind"] == "side" and block["truth_texts"]
    assert set(block["report"]) == {"summary", "steps", "expected", "actual", "failure_step"}


def test_the_budget_rule():
    cfg = CONFIG
    assert qr.step_budget(9, 40, cfg) == 18
    assert qr.step_budget(None, 40, cfg) == 40          # no recorded route: the case's own
    assert qr.step_budget(4, 30, replace(cfg, budget_floor=15)) == 15
    assert qr.step_budget(5, 30, replace(cfg, budget_factor=2.5)) == 13


def test_the_replayer_gets_the_report_and_not_the_test_case():
    task = qr.build_task(_unit(), _suite(), CONFIG)
    text = episode_runner._ablation_instruction(task, "emulator-5554", "mcp")
    assert text == qr.brief(task, "emulator-5554", "mcp")
    ref = _ref(*SIDE)
    for part in (ref.summary, ref.expected, ref.actual, ref.failure_step, *ref.steps):
        assert part in text
    assert "RESULT: reproduced=<yes|no>" in text
    spec = task.bug_spec
    # D3: not the case's name, its expected outcome, its id or the defect's id, and no
    # journey report contract.
    for hidden in (spec["name"], spec["expected_outcome"], SIDE[0], SIDE[1], "findings.yaml"):
        assert hidden not in text
    assert journey.brief(task, "emulator-5554", "mcp") != text


def test_the_control_brief_shows_the_steps_as_deleted():
    task = qr.build_task(_unit(qr.CONTROL), _suite(), CONFIG)
    text = qr.brief(task, "d", "mcp")
    ref = _ref(*SIDE)
    assert "Steps to reproduce: (the report gives none)" in text
    assert ref.steps[1] not in text and ref.actual in text


def test_give_test_case_is_a_parameter_and_changes_the_fingerprint():
    with_case = replace(CONFIG, give_test_case=True)
    task = qr.build_task(_unit(), _suite(), with_case)
    assert task.bug_spec["name"] in qr.brief(task, "d", "mcp")
    assert with_case.fingerprint() != CONFIG.fingerprint()


def test_an_agents_report_renders_its_steps_text_and_missing_fields():
    text = qr.render_report({"summary": "Total is wrong", "steps": "Opened the cart\nTapped Pay",
                             "expected": None, "actual": None, "failure_step": None})
    assert "  Opened the cart\n  Tapped Pay" in text
    assert "Expected result: (not given)" in text and "Failed at" not in text


# ── a replay episode is never a journey episode ────────────────────────────────

def _task_after_run(unit, *, fired, truncated=False, **spec_extra):
    task = qr.build_task(unit, _suite(), CONFIG)
    task.bug_spec.update({"tooling": "mcp", "fired": fired, "truncated": truncated,
                          "hook_steps": 7, **spec_extra})
    return task


def test_the_replay_verdict_reads_the_canary_and_the_device_text():
    unit = _unit()
    text = qr.build_task(unit, _suite(), CONFIG).bug_spec[qr.KEY]["truth_texts"][0]
    seen = _transcript(_obs(f"openScale {text} Weight"), "RESULT: reproduced=yes")
    v = qr.replay_verdict(seen, "m", _task_after_run(unit, fired=[SIDE[1]]))
    block = v.metrics[qr.KEY]
    assert v.passed and block["reproduced"] is True and block["agent_said"] == "yes"
    assert block["unit"]["defect_id"] == SIDE[1] and block["replayer"] == CONFIG.fingerprint()
    assert v.metrics["fault_fired"] == [SIDE[1]] and v.metrics["steps"] == 7
    assert v.metrics["step_budget"] == 14 and v.metrics["infra_failure"] is False

    # What the replayer says is recorded and never scored.
    lied = _transcript(_obs("openScale Overview Weight"), "RESULT: reproduced=yes")
    v = qr.replay_verdict(lied, "m", _task_after_run(unit, fired=[]))
    assert not v.passed and v.metrics[qr.KEY]["agent_said"] == "yes"
    assert v.metrics[qr.KEY]["reproduced"] is False

    # A replay that never touched the device is not a measurement.
    v = qr.replay_verdict(_transcript("RESULT: reproduced=no"), "m",
                          _task_after_run(unit, fired=[]))
    assert v.metrics["infra_failure"] is True


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _saved(root: Path, unit, verdict, name="ep", n=0) -> Path:
    d = root / unit.task_id / name
    d.mkdir(parents=True, exist_ok=True)
    ended = T0 + timedelta(minutes=2 * n + 1)
    result = RunResult.build(
        task_id=unit.task_id, task_version="qgb-v1", task_type=qr.TASK_TYPE, agent="fake-agent",
        model="fake-model", condition="mcp", trial=1,
        started_at=ended - timedelta(seconds=90), ended_at=ended, exit_code=0, verifier=verdict, artifact_dir=d, runs_dir=root)
    result.write(d / "result.json")
    return d


def test_no_scorer_board_or_quality_pass_reads_a_replay_episode_as_a_journey_episode(tmp_path):
    unit = _unit()
    text = qr.build_task(unit, _suite(), CONFIG).bug_spec[qr.KEY]["truth_texts"][0]
    v = qr.replay_verdict(_transcript(_obs(text)), "m", _task_after_run(unit, fired=[SIDE[1]]))
    d = _saved(tmp_path, unit, v)
    result = RunResult.model_validate_json((d / "result.json").read_text())

    assert qr.TASK_TYPE != journey.TASK_TYPE and result.task_type == qr.TASK_TYPE
    # The journey board: no row.
    assert journey.summary([result]) == []
    # The quality pass: not an episode it can score (no report list, no catches).
    for key in ("reports", "bugs_found", "false_reports", "version", "scorer_version"):
        assert key not in result.metrics
    with pytest.raises(rq.QualityError):
        rq.load_episode(d)
    # The rescore only takes journey task types unless told otherwise.
    import inspect
    assert inspect.signature(rescore.rescore).parameters["task_types"].default == (
        journey.TASK_TYPE,)
    # `show --mode journey` filters on the same one value.
    assert qr.TASK_TYPE not in {"bug_task", "clean_task", "bug_hunt", "journey_case"}


def test_the_default_replay_directory_is_not_searched_for_episodes(tmp_path):
    (tmp_path / "case-a" / "ep1").mkdir(parents=True)
    (tmp_path / "case-a" / "ep1" / "result.json").write_text("{}")
    inner = tmp_path / rq.REPLAY_DIR / "replay-abc" / "ep"
    inner.mkdir(parents=True)
    (inner / "result.json").write_text("{}")
    assert list(rq.episode_dirs(tmp_path)) == [tmp_path / "case-a" / "ep1"]


# ── ceiling and control ────────────────────────────────────────────────────────

def _facts(**kw) -> qr.DefectFacts:
    base = {"app_id": "app", "case_id": "case", "defect_id": "bug", "truth_kind": "screen",
            "basis": qr.CANARY_AND_TEXT, "has_reference": True, "has_canary": True,
            "truth_fired": True, "ref_steps": 6, "case_budget": 30}
    return qr.DefectFacts(**{**base, **kw})


_SERIAL = itertools.count(1)


def _attempt(reproduced, *, kind=qr.CEILING, canary=None, symptom=None, target=None,
             invalid="", config=CONFIG, cost=0.5, wall=60.0) -> qr.Attempt:
    n = next(_SERIAL)
    return qr.Attempt(
        unit={"kind": kind, "target": target or f"{kind}:app/case/bug"},
        replayer=config.fingerprint(), episode=f"replay-x/ep{n}", reproduced=reproduced,
        basis=qr.CANARY_AND_TEXT, canary_fired=reproduced if canary is None else canary,
        symptom_seen=reproduced if symptom is None else symptom, reason="r",
        invalid=invalid, cost_usd=cost, wall_time_sec=wall, ended_at=f"t{n:05d}")


def _status(ceiling, control=(), facts=None, config=CONFIG, **kw):
    return qr.baseline_status(facts or _facts(), [_attempt(r) for r in ceiling],
                              [_attempt(r, kind=qr.CONTROL) for r in control], config, **kw)


def test_the_ceiling_needs_two_of_three():
    assert _status([True, False, True], [False])["excluded"] is None
    below = _status([True, False, False])
    assert below["excluded"] == qr.BELOW_CEILING and "1 of 3" in below["reason"]
    assert below["ceiling"] == {"reproduced": 1, "attempts": 3}


def test_the_ceiling_stops_early_once_it_is_decided():
    assert _status([True, True])["need"] == qr.CONTROL            # 2 of 2: passed
    assert _status([False, False])["excluded"] == qr.BELOW_CEILING  # cannot reach 2
    assert _status([True, False])["need"] == qr.CEILING            # still open
    full = replace(CONFIG, ceiling_early_stop=False)
    assert _status([True, True], config=full)["need"] == qr.CEILING
    assert _status([False, False], config=full)["need"] == qr.CEILING


def test_a_defect_that_reproduces_without_its_steps_is_excluded():
    out = _status([True, True], [True])
    assert out["excluded"] == qr.CONTROL_REPRODUCES and out["control"]["reproduced"] == 1
    ok = _status([True, True], [False])
    assert ok["excluded"] is None and ok["decided"] and ok["need"] is None


def test_a_silent_canary_is_verdict_unavailable_not_a_failed_ceiling():
    """The reference replay showed the defect's text every time and the canary never
    fired: the build does not report this defect, so no replay of it can be judged."""
    silent = qr.baseline_status(
        _facts(), [_attempt(False, canary=False, symptom=True) for _ in range(3)], [], CONFIG)
    assert silent["excluded"] == qr.CANARY_SILENT and "verdict unavailable" in silent["reason"]
    assert qr.CANARY_SILENT in qr.VERDICT_UNAVAILABLE and qr.BELOW_CEILING not in qr.VERDICT_UNAVAILABLE
    # One canary firing is enough to know the build carries it: an ordinary ceiling miss.
    mixed = [_attempt(False, canary=True, symptom=False), _attempt(False, canary=False, symptom=True)]
    assert qr.baseline_status(_facts(), mixed, [], CONFIG)["excluded"] == qr.BELOW_CEILING


def test_what_the_repository_already_rules_out_costs_no_replay():
    assert _status([], facts=_facts(has_reference=False))["excluded"] == qr.NO_REFERENCE
    no_canary = _status([], facts=_facts(has_canary=False))
    assert no_canary["excluded"] == qr.NO_CANARY and no_canary["need"] is None
    textless = _facts(truth_kind="state", basis=qr.CANARY_ONLY)
    assert _status([], facts=textless)["need"] == qr.CEILING           # canary-only: replayed
    assert _status([], facts=textless,
                   config=replace(CONFIG, textless="exclude"))["excluded"] == qr.NO_TRUTH_TEXT


def test_a_baseline_that_could_not_be_run_stays_undecided():
    out = _status([True], gave_up=True)
    assert out["decided"] is False and out["need"] is None and out["excluded"] is None


# ── the corpus, device-free ────────────────────────────────────────────────────

def test_every_public_reference_report_can_be_judged():
    facts = qr.survey(heldout=False)
    assert len(facts) == len(reference_reports.load_all(heldout=False)) >= 100
    # Every public defect has a canary call in its build spec, so none is ruled out
    # before a replay; a truth row that never recorded the canary is not evidence that
    # the build lacks it (`canary_silent` is decided at the ceiling).
    assert all(f.has_canary and f.has_reference for f in facts)
    by_basis = {b: [f for f in facts if f.basis == b]
                for b in (qr.CANARY_AND_TEXT, qr.CANARY_ONLY)}
    assert {f.truth_kind for f in by_basis[qr.CANARY_ONLY]} == {"state", "absence"}
    assert {f.truth_kind for f in by_basis[qr.CANARY_AND_TEXT]} == set(qr.TEXT_KINDS)
    assert 0 < len(by_basis[qr.CANARY_ONLY]) < len(facts) / 5


def test_the_pilot_selection_is_fixed_and_spread():
    facts = qr.survey(heldout=False)
    picked = qr.select_pilot(facts, 10)
    assert picked == qr.select_pilot(list(reversed(facts)), 10)
    assert len({f.key for f in picked}) == 10 and len({f.app_id for f in picked}) == 10
    kinds = [f.truth_kind for f in picked]
    assert kinds == list(qr.PILOT_CYCLE)
    assert sum(1 for f in picked if f.basis == qr.CANARY_ONLY) == 2


# ── configuration ──────────────────────────────────────────────────────────────

def test_the_packaged_config_is_the_spec_defaults_with_no_replayer_yet():
    cfg = qr.load_config()
    assert cfg == qr.ReplayConfig()                 # D3 report only, D4 one attempt
    assert (cfg.give_test_case, cfg.attempts, cfg.ceiling_attempts, cfg.ceiling_min,
            cfg.control_attempts, cfg.budget_factor) == (False, 1, 3, 2, 1, 2.0)
    assert not cfg.pinned                           # D5: chosen from the pilot


def test_config_errors_are_named(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text('{"modle": "x"}')
    with pytest.raises(qr.ReplayError, match="unknown key"):
        qr.load_config(bad)
    with pytest.raises(qr.ReplayError, match="textless"):
        qr.ReplayConfig(textless="guess")
    with pytest.raises(qr.ReplayError, match="ceiling_min"):
        qr.ReplayConfig(ceiling_min=4)


def test_a_pinned_replayer_cannot_be_swapped_on_the_command_line():
    assert qr.with_replayer(qr.ReplayConfig(), agent="a", model="m").pinned
    with pytest.raises(qr.ReplayError, match="pinned"):
        qr.with_replayer(CONFIG, agent="fake-agent", model="another-model")
    assert qr.with_replayer(CONFIG, agent=None, model=None) == CONFIG


# ── a whole run, with a fake replayer ──────────────────────────────────────────

def _journey_episode(run_dir: Path, name: str, case_id: str, reports: list[dict]) -> Path:
    d = run_dir / case_id / name
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({
        "task_id": f"{case_id}~seeded", "task_type": journey.TASK_TYPE,
        "metrics": {"app_id": APP, "case_id": case_id, "version": "seeded",
                    "scorer_version": journey.SCORER_VERSION, "report_source": "findings_file",
                    "reports": reports}}, indent=2))
    return d


def _report(matched=None, step=2, **kw) -> dict:
    return {"step": step, "screen": "Overview", "observed": "Overveiw", "expected": "Overview",
            "description": "the title is misspelled", "matched": matched, "known": None,
            "consequence_of": None, **kw}


class FakeReplayer:
    """Stands in for the device and the model: answers each unit from a script and
    saves the `result.json` a real replay episode would."""

    def __init__(self, replay_dir: Path, answers, config=CONFIG) -> None:
        self.replay_dir, self.answers, self.config = replay_dir, answers, config
        self.calls: list[str] = []

    async def __call__(self, unit: qr.ReplayUnit):
        self.calls.append(unit.key)
        answer = self.answers(unit)
        if answer is None:
            return None
        task = qr.build_task(unit, _suite(), self.config)
        task.bug_spec.update({"tooling": "mcp", "hook_steps": 5,
                              "fired": [unit.defect_id] if answer else []})
        if answer == "staging":
            task.bug_spec["staging_failed"] = "device offline"
        texts = task.bug_spec[qr.KEY]["truth_texts"]
        screen = texts[0] if (answer is True and texts) else "nothing of note"
        v = qr.replay_verdict(_transcript(_obs(screen)), "fake-model", task)
        n = len(self.calls)
        d = _saved(self.replay_dir, unit, v, name=f"ep{n:03d}", n=n)
        return qr.attempt_from_result(json.loads((d / "result.json").read_text()),
                                      str(d.relative_to(self.replay_dir)))


def _board(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """A saved board: three episodes, four valid reports on three defects, one false
    report and one neutral one."""
    run_dir = tmp_path / "runs"
    eps = {
        "side-good": _journey_episode(run_dir, "ep-a", SIDE[0], [
            _report(SIDE[1]), _report(None, step=3)]),
        "side-bad": _journey_episode(run_dir, "ep-b", SIDE[0], [_report(SIDE[1])]),
        "screen": _journey_episode(run_dir, "ep-c", SCREEN[0], [
            _report(None, known="known-thing"), _report(SCREEN[1])]),
        "other": _journey_episode(run_dir, "ep-d", OTHER[0], [_report(OTHER[1])]),
    }
    return run_dir, run_dir / qr.REPLAY_DIR, eps


def _answers(unit: qr.ReplayUnit):
    """SIDE: a sound defect (ceiling passes, control does not reproduce); one agent
    report reproduces and one does not. SCREEN: its reference does not replay. OTHER:
    reproduces even with the steps deleted."""
    if unit.defect_id == SCREEN[1]:
        return False
    if unit.defect_id == OTHER[1]:
        return True
    if unit.kind == qr.CEILING:
        return unit.attempt != 2                      # 2 of 3
    if unit.kind == qr.CONTROL:
        return False
    return "ep-a" in unit.source_episode


def _run(state, fake, **kw) -> int:
    return asyncio.run(qr.run(state, fake, **kw))


def test_only_valid_reports_are_planned(tmp_path):
    run_dir, _, _ = _board(tmp_path)
    reports = qr.valid_reports(run_dir)
    assert [(r.episode, r.index, r.defect_id) for r in reports] == [
        (f"{SCREEN[0]}/ep-c", 1, SCREEN[1]), (f"{OTHER[0]}/ep-d", 0, OTHER[1]),
        (f"{SIDE[0]}/ep-a", 0, SIDE[1]), (f"{SIDE[0]}/ep-b", 0, SIDE[1])]
    # The replayer is handed the customer-shaped report, five fields and no more.
    assert set(reports[0].report) == {"summary", "steps", "expected", "actual", "failure_step"}
    assert reports[0].report["actual"] == "Overveiw"


def test_a_run_measures_the_baselines_first_and_replays_only_usable_defects(tmp_path):
    run_dir, replay_dir, eps = _board(tmp_path)
    state = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), [])
    before = state.worst_case()
    assert before == {"ceiling": 9, "control": 3, "report": 4, "total": 16}
    doc = qr.plan_document(state)
    assert [u["kind"] for u in doc["runnable_now"]] == [qr.CEILING] * 3
    assert "at most: 16" in qr.plan_text(doc)

    fake = FakeReplayer(replay_dir, _answers)
    started = _run(state, fake)
    kinds = [k.split(":")[0] for k in fake.calls]
    # SIDE: 3 ceiling (T, F, T), 1 control, 2 reports. SCREEN: 2 ceiling (F, F), stop.
    # OTHER: 2 ceiling (T, T), 1 control (T), no report replay.
    assert started == len(fake.calls) == 11
    assert kinds.count(qr.CEILING) == 7 and kinds.count(qr.CONTROL) == 2
    assert kinds.count(qr.REPORT) == 2
    # No report is replayed before its defect's ceiling and control are in.
    first_report = kinds.index(qr.REPORT)
    assert qr.REPORT not in kinds[:first_report] and qr.CEILING not in kinds[first_report:]
    assert state.next_units() == [] and state.worst_case()["total"] == 0

    summary = qr.fold(run_dir, CONFIG, replay_dir=replay_dir, state=state)
    assert summary["episodes"] == 4 and summary["reports"] == {"replayed": 2, "excluded": 2}
    assert [(e["defect"].split("/")[-1], e["excluded"]) for e in summary["excluded"]] == [
        (SCREEN[1], qr.BELOW_CEILING), (OTHER[1], qr.CONTROL_REPRODUCES)]

    # quality_replay.json, beside each episode's result.json.
    good = json.loads((eps["side-good"] / qr.REPLAY_FILE).read_text())
    assert good["replay_version"] == qr.REPLAY_VERSION and good["replayer"] == CONFIG.stamp()
    (entry,) = good["reports"]
    assert entry["index"] == 0 and entry["defect"] == SIDE[1] and entry["status"] == "replayed"
    assert entry["reproduced"] is True and entry["excluded"] is None
    assert entry["ceiling"] == {"reproduced": 2, "attempts": 3}
    assert entry["control"] == {"reproduced": 0, "attempts": 1}
    (attempt,) = entry["attempts"]
    assert (replay_dir / attempt["episode"] / "result.json").is_file()
    assert attempt["steps"] == 5 and attempt["step_budget"] == 14
    excluded = json.loads((eps["screen"] / qr.REPLAY_FILE).read_text())["reports"][0]
    assert excluded["index"] == 1 and excluded["status"] == "excluded"
    assert excluded["reproduced"] is None and excluded["attempts"] == []

    # baseline.json: the ceiling and control of every defect, with the reason.
    base = json.loads((replay_dir / qr.BASELINE_FILE).read_text())
    assert base["replayer"] == CONFIG.stamp() and "corpus_version" in base
    side = base["defects"][qr.defect_key(APP, *SIDE)]
    assert side["excluded"] is None and side["decided"] and "need" not in side
    assert base["defects"][qr.defect_key(APP, *SCREEN)]["excluded"] == qr.BELOW_CEILING

    # G4 in quality.json.
    def g4(name, index=0):
        q = json.loads((eps[name] / rq.QUALITY_FILE).read_text())
        return q, q["reports"][index]["gates"]["G4"]

    q, gate = g4("side-good")
    assert gate["pass"] is True and gate["reason"].startswith("reproduced from the report")
    assert q["replay"] == {"replay_version": qr.REPLAY_VERSION, "replayer": CONFIG.stamp()}
    assert q["totals"]["gates"]["G4"] == {"name": "reproducible", "evaluated": 1, "passed": 1}
    assert q["reports"][1]["gates"]["G4"] == {"pass": None,
                                              "reason": "runs only on a valid report"}
    assert g4("side-bad")[1]["pass"] is False
    q, gate = g4("screen", 1)
    assert gate["pass"] is None and "excluded from replay (ceiling)" in gate["reason"]
    assert q["totals"]["gates"]["G4"]["evaluated"] == 0
    assert q["reports"][0]["neutral"] and q["reports"][0]["gates"] == {}
    assert "excluded from replay (control)" in g4("other")[1]["reason"]


def test_the_saved_episodes_are_the_state_so_a_run_resumes_and_folds_anywhere(tmp_path):
    run_dir, replay_dir, eps = _board(tmp_path)
    state = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), [])
    fake = FakeReplayer(replay_dir, _answers)
    assert _run(state, fake, max_episodes=4) == 4          # a hard cap on episodes started

    # A new process: everything it knows is what `collect` reads off disk.
    resumed = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), qr.collect(replay_dir))
    assert len(resumed.attempts) == 4
    again = FakeReplayer(replay_dir, _answers)
    again.calls = list(fake.calls)                         # keep episode names distinct
    assert _run(resumed, again) == 7
    assert sorted(fake.calls + again.calls[4:]) == sorted(again.calls)

    # Folding from disk alone gives the same files as folding the live state.
    live = qr.fold(run_dir, CONFIG, replay_dir=replay_dir, state=resumed)
    first = {n: (p / qr.REPLAY_FILE).read_bytes() for n, p in eps.items()}
    assert qr.fold(run_dir, CONFIG, replay_dir=replay_dir) == live
    assert first == {n: (p / qr.REPLAY_FILE).read_bytes() for n, p in eps.items()}
    assert qr.main(["fold", str(run_dir), "--agent", "fake-agent", "--model", "fake-model"]) == 0


def test_one_unit_can_be_run_alone_for_a_cloud_fan_out(tmp_path):
    run_dir, replay_dir, _ = _board(tmp_path)
    state = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), [])
    key = state.next_units()[0].key
    fake = FakeReplayer(replay_dir, _answers)
    assert _run(state, fake, only=[key]) == 1 and fake.calls == [key]
    assert _run(state, fake, only=["report:no-such#0@1"]) == 0


def test_replays_by_another_replayer_are_not_folded_in(tmp_path):
    run_dir, replay_dir, eps = _board(tmp_path)
    other = replace(CONFIG, model="another-model")
    _run(qr.ReplayState(other, qr.valid_reports(run_dir), []),
         FakeReplayer(replay_dir, _answers, other))
    state = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), qr.collect(replay_dir))
    assert state.attempts == [] and state.other_replayer == 11
    summary = qr.fold(run_dir, CONFIG, replay_dir=replay_dir, state=state)
    assert summary["reports"] == {"pending": 4} and summary["other_replayer"] == 11
    q = rq.score_episode(eps["side-good"])
    assert q["reports"][0]["gates"]["G4"]["pass"] is None
    assert "ceiling not measured yet" in q["reports"][0]["gates"]["G4"]["reason"]


def test_a_replay_that_did_not_run_is_retried_once_and_then_given_up(tmp_path):
    run_dir, replay_dir, eps = _board(tmp_path)
    calls = {"n": 0}

    def answers(unit):
        if unit.defect_id != SIDE[1]:
            return False
        if unit.kind == qr.REPORT and "ep-b" in unit.source_episode:
            return "staging"                               # never runs
        if unit.kind == qr.CONTROL:
            calls["n"] += 1
            return "staging" if calls["n"] == 1 else False  # runs on the retry
        return True

    state = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), [])
    fake = FakeReplayer(replay_dir, answers)
    _run(state, fake)
    assert sum(1 for k in fake.calls if k.startswith(qr.CONTROL + ":" + APP + "/" + SIDE[0])) == 2
    stuck = [k for k in fake.calls if "ep-b" in k]
    assert len(stuck) == CONFIG.max_retries + 1            # then no more money on it
    qr.fold(run_dir, CONFIG, replay_dir=replay_dir, state=state)
    entry = json.loads((eps["side-bad"] / qr.REPLAY_FILE).read_text())["reports"][0]
    assert entry["status"] == "unavailable" and entry["reproduced"] is None
    assert "env_failure" in entry["reason"] and len(entry["attempts"]) == 2
    gate = rq.score_episode(eps["side-bad"])["reports"][0]["gates"]["G4"]
    assert gate["pass"] is None and "could not be run" in gate["reason"]


def test_an_executor_that_saves_nothing_does_not_loop(tmp_path):
    run_dir, replay_dir, _ = _board(tmp_path)
    state = qr.ReplayState(CONFIG, qr.valid_reports(run_dir), [])
    fake = FakeReplayer(replay_dir, lambda unit: None)
    assert _run(state, fake) == 3 and len(set(fake.calls)) == 3


# ── the gate reads, and only reads ─────────────────────────────────────────────

def test_without_saved_replays_the_gate_did_not_run_and_quality_json_has_no_stamp(tmp_path):
    run_dir, _, eps = _board(tmp_path)
    q = rq.score_episode(eps["side-good"])
    assert "replay" not in q
    assert q["reports"][0]["gates"]["G4"] == {"pass": None,
                                              "reason": "no replay saved for this report"}
    assert q["reports"][0]["customer_ready"] is None
    assert q["totals"]["gates"]["G4"] == {"name": "reproducible", "evaluated": 0, "passed": 0}
    # Scoring wrote nothing but quality.json and started nothing.
    rq.write_quality(eps["side-good"])
    assert sorted(p.name for p in eps["side-good"].iterdir()) == ["quality.json", "result.json"]
    assert not (run_dir / qr.REPLAY_DIR).exists()


def test_a_saved_replay_for_another_defect_is_not_used(tmp_path):
    _, _, eps = _board(tmp_path)
    (eps["side-good"] / qr.REPLAY_FILE).write_text(json.dumps({
        "replay_version": 1, "replayer": {}, "reports": [
            {"index": 0, "defect": "some-other-bug", "reproduced": True, "excluded": None}]}))
    gate = rq.score_episode(eps["side-good"])["reports"][0]["gates"]["G4"]
    assert gate["pass"] is None and "some-other-bug" in gate["reason"]
    (eps["side-good"] / qr.REPLAY_FILE).write_text("not json")
    assert rq.score_episode(eps["side-good"])["reports"][0]["gates"]["G4"]["pass"] is None


def test_scoring_never_loads_the_replay_runner():
    """`report_quality` stays import-free of the package, so the scoring command cannot
    start a replay even by accident."""
    code = ("import sys, qualgentbench.report_quality as rq; "
            "bad = [m for m in sys.modules if m.startswith('qualgentbench.') "
            "and m != 'qualgentbench.report_quality']; "
            "assert not bad, bad; assert rq.GATES[-1].id in {g.id for g in rq.GATES}")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=False, cwd=Path(__file__).resolve().parent.parent / "src")
    assert done.returncode == 0, done.stderr
    assert "G4" in [g.id for g in rq.GATES]


# ── the pilot ──────────────────────────────────────────────────────────────────

def _pilot():
    spec = importlib.util.spec_from_file_location("quality_replay_pilot",
                                                  SCRIPTS / "quality_replay_pilot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_pilot_prints_its_plan_and_runs_nothing_by_default(tmp_path, capsys):
    pilot = _pilot()
    rc = pilot.main(["--out", str(tmp_path / "pilot"), "--candidate", "agent-a:model-a",
                     "--candidate", "agent-b:vendor/model-b@bedrock"])
    out = capsys.readouterr().out
    assert rc == 0 and "pilot defects (10):" in out
    assert "total: at most 80 replay episodes" in out and "Nothing was run." in out
    assert not (tmp_path / "pilot").exists()
    assert pilot.parse_candidate("agent-b:vendor/model-b@bedrock") == (
        "agent-b", "vendor/model-b", "bedrock")


def test_the_pilot_table_from_saved_replays(tmp_path, capsys):
    pilot = _pilot()
    out_dir = tmp_path / "pilot"
    defects = [(APP, *SIDE), (APP, *SCREEN), (APP, *OTHER)]
    args = ["--out", str(out_dir), "--candidate", "fake-agent:fake-model"]
    for d in defects:
        args += ["--defect", "/".join(d)]
    config = pilot.pilot_config(qr.load_config(), "fake-agent", "fake-model", None, "mcp")
    rdir = pilot.candidate_dir(out_dir, "fake-agent", "fake-model", None)
    state = pilot.state_for(config, rdir, defects)
    assert state.worst_case() == {"ceiling": 9, "control": 3, "report": 0, "total": 12}
    fake = FakeReplayer(rdir, _answers, config)
    # No early stop in the pilot: every reference gets its three replays.
    assert _run(state, fake) == 11 and sum(k.startswith(qr.CEILING) for k in fake.calls) == 9

    row = qr.pilot_row("fake", pilot.state_for(config, rdir, defects))
    assert (row["ceiling_ok"], row["ceiling_measured"]) == (2, 3)
    assert (row["reference_reproduced"], row["reference_replays"]) == (5, 9)
    assert (row["control_failures"], row["control_measured"]) == (1, 2)
    assert row["usable"] == 1 and row["episodes"] == 11 and row["truncated"] == 0
    # The fake transcript's usage is priced or not by the model table; either way the
    # table never prints an unmeasured cost as a number.
    assert row["cost_unknown"] + (11 if row["cost_per_replay_usd"] is not None else 0) >= 11

    assert pilot.main([*args, "--table"]) == 0
    text = capsys.readouterr().out
    assert "| fake-agent / fake-model | 2 of 3 | 5 of 9 | 1 of 2 | 0 | 1 of 3 |" in text
    assert f"excluded {qr.defect_key(APP, *SCREEN)}  [ceiling]" in text
    assert pilot.main([*args, "--table", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["ceiling_ok"] == 2


def test_the_pilot_table_says_unknown_for_an_unmeasured_cost():
    row = {"replayer": "r", "defects": 1, "ceiling_measured": 0, "ceiling_ok": 0,
           "reference_replays": 0, "reference_reproduced": 0, "control_measured": 0,
           "control_failures": 0, "verdict_unavailable": 0, "usable": 0, "truncated": 0,
           "not_run": 0, "cost_per_replay_usd": None, "wall_per_replay_sec": None}
    assert "| unknown | unknown |" in qr.pilot_table([row])
    assert "| $1.25 | 90 s |" in qr.pilot_table([{**row, "cost_per_replay_usd": 1.25,
                                                  "wall_per_replay_sec": 90.4}])


# ── the command line ───────────────────────────────────────────────────────────

def _cli(*args: str):
    return CliRunner().invoke(cli.main, ["run", "--mode", "journey", *args])


def test_the_plan_needs_no_device_and_no_replayer(tmp_path):
    run_dir, _, _ = _board(tmp_path)
    res = _cli("--quality-replay", str(run_dir), "--quality-replay-plan")
    assert res.exit_code == 0, res.output
    assert "valid reports: 4 on 3 (case, defect) pair(s)" in res.output
    assert "at most: 16" in res.output
    res = _cli("--quality-replay", str(run_dir), "--quality-replay-plan", "--quality-replay-json")
    doc = json.loads(res.output)
    assert doc["worst_case_episodes"]["total"] == 16 and len(doc["runnable_now"]) == 3
    assert {"key", "kind", "step_budget", "app_id"} <= set(doc["runnable_now"][0])
    assert qr.main(["plan", str(run_dir)]) == 0


def test_the_replay_command_refuses_before_it_spends(tmp_path, monkeypatch):
    run_dir, _, _ = _board(tmp_path)
    started = []
    monkeypatch.setattr(qr.DeviceExecutor, "__call__",
                        lambda self, unit: started.append(unit))
    monkeypatch.setattr(cli, "_gate_runs_dir", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_gate_agent_auth", lambda *a, **k: None)

    # No replayer is pinned yet, so one must be named.
    res = _cli("--quality-replay", str(run_dir))
    assert res.exit_code != 0 and "no replayer" in res.output
    # Not a terminal and no --yes: the plan is printed and nothing starts.
    res = _cli("--quality-replay", str(run_dir), "--agent", "claude-code", "--models", "m")
    assert res.exit_code != 0 and "Not started" in res.output
    # One replayer for every arm.
    res = _cli("--quality-replay", str(run_dir), "--agent", "claude-code", "--models", "a,b")
    assert res.exit_code != 0 and "one replayer model" in res.output
    assert CliRunner().invoke(cli.main, ["run", "--mode", "hunt", "--quality-replay",
                                         str(run_dir)]).exit_code != 0
    res = CliRunner().invoke(cli.main, ["run", "--mode", "journey", "--quality-replay-plan"])
    assert res.exit_code != 0 and "need --quality-replay" in res.output
    assert started == [] and not (run_dir / qr.REPLAY_DIR).exists()
