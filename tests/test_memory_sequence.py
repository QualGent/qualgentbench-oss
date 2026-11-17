"""QUA-3492: the memory sequence runner — stage loop, frozen snapshots, curator between
stages — end to end with a fake episode runner and a scripted curator model (no device,
no live model)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from qualgentbench import journey, lanes, leaderboard
from qualgentbench.memory import curator as cur
from qualgentbench.memory import format as fmt
from qualgentbench.memory import proposals as props
from qualgentbench.memory import sequence as seq
from qualgentbench.memory import snapshot as snap
from qualgentbench.memory import tracks as trk
from qualgentbench.memory.arm import format_help
from qualgentbench.result import RunResult

APP = "markor"
DATE = "2026-11-17"


class FakeRunner:
    """Writes one finished episode per expected (case, version): result.json (the
    scored verdict the curator gates on), provenance.memory with the snapshot the arm
    rendered, and a proposals file. Records every request."""

    def __init__(self, *, fail_case: str | None = None, wrong_snapshot: bool = False):
        self.requests: list[seq.StageRequest] = []
        self.fail_case = fail_case
        self.wrong_snapshot = wrong_snapshot

    def run_stage(self, req: seq.StageRequest) -> list[seq.EpisodeRecord]:
        self.requests.append(req)
        run_id = f"run-{req.stage}-{len(self.requests)}"
        req.on_run_id(run_id)
        prepared = None
        if req.arm.enabled:
            from qualgentbench.memory.arm import prepare
            prepared = prepare(req.arm, req.app)
        out = []
        for i, (case, version) in enumerate(req.expected):
            rel = f"{case}/{req.stage}_{version}_{len(self.requests)}"
            d = req.runs_dir / rel
            (d / "workspace").mkdir(parents=True, exist_ok=True)
            ok = case != self.fail_case
            metrics = {"case_id": case, "version": version, "app_id": req.app,
                       "completed": ok, "false_reports": 0,
                       "bugs_found": [], "memory_stage": req.stage,
                       "drift": req.flags(case, version)}
            memory = None
            if prepared is not None:
                memory = prepared.provenance()
                if self.wrong_snapshot:
                    memory["snapshot_hash"] = "sha256:" + "0" * 64
            (d / "workspace" / props.FILENAME).write_text(yaml.safe_dump([{
                "type": "nav",
                "text": f"The {case.split('-', 1)[1].replace('-', ' ')} screen opens from "
                        f"the main list in stage {req.stage} run {version}.",
                "evidence": {"step": 2, "screen": "Main list"}}]))
            result = RunResult(task_id=f"{case}~{version}", task_version="qgb-v1",
                               task_type="journey_case", agent="fake", model="m",
                               condition="mcp", trial=1, passed=ok, score=1.0,
                               started_at=f"2026-11-17T00:00:{i:02d}+00:00",
                               ended_at="2026-11-17T00:01:00+00:00", wall_time_sec=1,
                               exit_code=0, metrics=metrics, artifact_dir=rel,
                               run_id=run_id,
                               provenance={"memory": memory} if memory else {})
            (d / "result.json").write_text(result.model_dump_json())
            out.append(seq.EpisodeRecord.from_result(result, req.runs_dir))
        return out


def _model(calls: list | None = None) -> cur.FunctionModel:
    def fn(request):
        if calls is not None:
            calls.append(request)
        return {"decisions": [{"proposal": p["key"], "op": "ADD", "reason": "useful"}
                              for p in request["proposals"]]}
    return cur.FunctionModel(fn, id="scripted")


def _factory(model):
    return lambda cache_dir: cur.CachedModel(model, cache_dir)


def _seq(out: Path, runner, *, mode="dynamic", through="run-2", model=None,
         ordering="a") -> seq.Sequence:
    cfg = seq.SequenceConfig(app=APP, ordering=ordering, rep=1,
                             arm=seq.ArmSpec(mode=mode), date=DATE, through=through)
    return seq.Sequence(out=out, config=cfg, runner=runner,
                        model_factory=_factory(model or _model()))


# ── the stage loop ───────────────────────────────────────────────────────────

def test_two_stage_dynamic_run_freezes_curates_and_records(tmp_path):
    runner = FakeRunner()
    s = _seq(tmp_path / "seq", runner)
    state = s.run()
    assert s.steps() == ["run-1", "curate-1", "run-2"]
    assert state["complete"] is True
    unit = seq.unit_key(APP, "a", seq.ArmSpec(mode="dynamic"), 1)
    assert unit == "markor__a__dynamic-small-none__r1"

    m1 = snap.stage_pointer(s.store, "run-1", unit)
    c1 = snap.stage_pointer(s.store, "curate-1", unit)
    m2 = snap.stage_pointer(s.store, "run-2", unit)
    # M_1 is the seed (no trap at run-1), M_2 = curated M_1 + run-2's trap.
    assert m1["parent"] is None and m1["traps"] == []
    assert c1["parent"] == m1["snapshot"] and c1["counts"]["ADD"] > 0
    assert m2["parent"] == c1["snapshot"] and m2["traps"] == ["bug-901"]
    assert len({m1["snapshot"], c1["snapshot"], m2["snapshot"]}) == 3

    # Episode results carry the stage's frozen snapshot hash.
    for stage, ptr in (("run-1", m1), ("run-2", m2)):
        rows = json.loads((s.stage_dir(stage) / seq.EPISODES_FILE).read_text())
        assert rows["snapshot"] == ptr["snapshot"]
        assert rows["problems"] == []
        assert {r["snapshot_hash"] for r in rows["episodes"]} == {ptr["snapshot"]}
        want = seq.expected_episodes(s.track.slots("a", stage))
        assert [(r["case"], r["version"]) for r in rows["episodes"]] == want
        assert json.loads((s.stage_dir(stage) / seq.MEMORY_CONFIG).read_text())["stage"] == stage

    # The operation log and health rows exist; curated memory lints clean.
    log = json.loads((s.stage_dir("curate-1") / seq.CURATION_FILE).read_text())
    assert log["snapshot"] == c1["snapshot"]
    health = json.loads((s.out / seq.HEALTH_FILE).read_text())
    assert [h["stage"] for h in health] == ["run-1", "curate-1", "run-2"]
    assert all(h["lint_failures"] == 0 for h in health)
    assert health[1]["curator"]["ADD"] == c1["counts"]["ADD"]


def test_curator_reads_only_the_planned_version_of_each_stage_case(tmp_path):
    runner = FakeRunner()
    s = _seq(tmp_path / "seq", runner)
    s.run()
    manifest = json.loads((s.stage_dir("curate-1") / seq.EPISODES_FILE).read_text())
    planned = set(s.track.curator_inputs("a", "run-1"))
    used = {(m["case"], m["version"]) for m in manifest if m["used"]}
    assert used == planned
    unused = [m for m in manifest if not m["used"]]
    assert unused and all(m["why"] == "not the planned version" for m in unused)
    # No stage-2 case's proposals reached M_2.
    later = set(s.track.later_cases("a", "run-1"))
    log = json.loads((s.stage_dir("curate-1") / seq.CURATION_FILE).read_text())
    assert not later & {e.get("case") for e in log["episodes"]}


def test_stage_requests_carry_both_versions_and_stage_drift(tmp_path):
    runner = FakeRunner()
    _seq(tmp_path / "seq", runner, through="run-3").run()
    by_stage = {r.stage: r for r in runner.requests}
    track = trk.load_track(APP)
    for stage in ("run-1", "run-2", "run-3"):
        req = by_stage[stage]
        assert req.drift == track.stage(stage).drift
        for slot in req.slots:
            assert req.flags(slot.case, trk.CLEAN) == list(slot.drift)
            if slot.bugs:
                assert (slot.case, trk.SEEDED) in req.expected
                assert req.flags(slot.case, trk.SEEDED) == [*slot.bugs, *slot.drift]
    assert by_stage["run-3"].drift == ("markor-drift-fab-label",)


def test_rerun_resumes_without_running_anything_again(tmp_path):
    runner = FakeRunner()
    first = _seq(tmp_path / "seq", runner).run()
    again = FakeRunner()
    second = _seq(tmp_path / "seq", again).run()
    assert again.requests == []
    assert second["steps"] == first["steps"]


def test_redo_rebuilds_identical_snapshots_from_cached_curator_answers(tmp_path):
    out = tmp_path / "seq"
    calls: list = []
    first = _seq(out, FakeRunner(), model=_model(calls)).run()
    assert calls, "the scripted model answered the first curation"

    # Redo curate-1 with a model that must never be asked: the cache answers.
    no_model = cur.FunctionModel(lambda r: pytest.fail("cache miss"), id="scripted")
    s = _seq(out, FakeRunner(), model=no_model)
    assert s.redo("curate-1") == ["curate-1", "run-2"]
    runner = FakeRunner()
    s.runner = runner
    second = s.run()
    assert second["steps"]["curate-1"]["snapshot"] == first["steps"]["curate-1"]["snapshot"]
    # Re-running stage 2 with the same M_2 uses the identical snapshot.
    assert second["steps"]["run-2"]["snapshot"] == first["steps"]["run-2"]["snapshot"]
    assert [r.stage for r in runner.requests] == ["run-2"]
    assert runner.requests[0].arm.snapshot == snap.open_snapshot(
        s.store, first["steps"]["run-2"]["snapshot"]).memory_root


def test_static_arm_rebuilds_from_seed_with_traps_through_the_stage(tmp_path):
    runner = FakeRunner()
    s = _seq(tmp_path / "seq", runner, mode="static", through="run-3")
    state = s.run()
    assert s.steps() == ["run-1", "run-2", "run-3"]
    assert state["steps"]["run-3"]["traps"] == ["bug-901", "accepted-901"]
    root = snap.open_snapshot(s.store, state["steps"]["run-3"]["snapshot"]).memory_root
    doc = fmt.load(fmt.app_path(root, s.org, APP))
    assert doc.find("bug-901") is not None and doc.find("accepted-901") is not None
    # No curation, and the packaged seed is untouched.
    assert not (s.store / "stages" / "curate-1").exists()
    assert fmt.load(fmt.app_path(trk.DATA_ROOT, s.org, APP)).find("bug-901") is None


def test_no_memory_arm_runs_with_a_stage_label_only(tmp_path):
    runner = FakeRunner()
    s = _seq(tmp_path / "seq", runner, mode="none")
    s.run()
    assert [r.arm.as_dict() for r in runner.requests] == [
        {"mode": "none", "budget": "small", "diff": "none", "snapshot": None, "org": None,
         "stage": "run-1"},
        {"mode": "none", "budget": "small", "diff": "none", "snapshot": None, "org": None,
         "stage": "run-2"}]
    assert not s.store.exists()
    assert json.loads((s.out / seq.HEALTH_FILE).read_text()) == []


def test_an_episode_that_read_another_snapshot_fails_the_stage(tmp_path):
    s = _seq(tmp_path / "seq", FakeRunner(wrong_snapshot=True))
    with pytest.raises(seq.SequenceError, match="read snapshot"):
        s.run()
    assert not s.done("run-1")
    assert s.state["steps"]["run-1"]["run_id"]          # kept for a resume


def test_unverified_episodes_feed_nothing_and_missing_ones_are_named(tmp_path):
    track = trk.load_track(APP)
    case = track.slots("a", "run-1")[0].case
    s = _seq(tmp_path / "seq", FakeRunner(fail_case=case))
    s.run()
    manifest = json.loads((s.stage_dir("curate-1") / seq.EPISODES_FILE).read_text())
    row = next(m for m in manifest if m["case"] == case and m["used"])
    assert row["verified"] is False


def test_a_different_sequence_in_the_same_dir_is_refused(tmp_path):
    _seq(tmp_path / "seq", FakeRunner(), through="run-1").run()
    with pytest.raises(seq.SequenceError, match="different sequence"):
        _seq(tmp_path / "seq", FakeRunner(), ordering="b", through="run-1")


def test_diff_arm_needs_its_changes_file(tmp_path):
    cfg = seq.SequenceConfig(app=APP, ordering="a", rep=1,
                             arm=seq.ArmSpec(mode="static", diff="diff"), date=DATE,
                             through="run-1", changes_dir=tmp_path / "changes")
    s = seq.Sequence(out=tmp_path / "seq", config=cfg, runner=FakeRunner())
    with pytest.raises(FileNotFoundError, match="changes file"):
        s.run()
    (tmp_path / "changes").mkdir()
    (tmp_path / "changes" / "v1.changes.md").write_text(
        "# What changed since the last build: markor\n\n"
        "- Screen changed: \"Settings\" gained the control \"Backup\".\n")
    state = s.run()
    root = snap.open_snapshot(s.store, state["steps"]["run-1"]["snapshot"]).memory_root
    assert fmt.changes_path(root, s.org, APP).is_file()


# ── the CLI stage runner ─────────────────────────────────────────────────────

def test_cli_runner_builds_a_journey_run_and_a_resume(tmp_path):
    track = trk.load_track(APP)
    slots = track.slots("a", "run-3")
    from qualgentbench.memory.arm import MemoryArm
    req = seq.StageRequest(app=APP, stage="run-3", ordering="a", slots=slots,
                           drift=track.stage("run-3").drift, arm=MemoryArm(stage="run-3"),
                           memory_config=tmp_path / "memory.json", runs_dir=tmp_path / "r")
    r = seq.CliStageRunner(agent="codex-cli", model="m1", mcp_server="http://x",
                           devices="a,b", command=("qgb",))
    argv = r.args(req, tmp_path / "rid")
    assert argv[:4] == ["qgb", "run", "--mode", "journey"]
    assert argv[argv.index("--case") + 1] == ",".join(s.case for s in slots)
    assert argv[argv.index("--drift") + 1] == "markor-drift-fab-label"
    assert argv[argv.index("--memory-config") + 1] == str(tmp_path / "memory.json")
    assert "--yes" in argv and argv[argv.index("--models") + 1] == "m1"
    req.resume_run_id = "RID"
    argv = r.args(req, tmp_path / "rid")
    assert argv[:4] == ["qgb", "run", "--resume", "RID"]
    assert "--case" not in argv and "--memory-config" not in argv


# ── drift through `run` ──────────────────────────────────────────────────────

def test_lanes_build_task_switches_on_only_the_apps_own_drift(tmp_path):
    from qualgentbench import bugs
    from qualgentbench.scheduler import Estimator
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == APP)
    apk = tmp_path / "b.apk"
    apk.write_bytes(b"x")
    plan = lanes.build_plan([suite], mode="journey", trials=1, lanes=1,
                            estimator=Estimator(tmp_path, "c", "m"),
                            resolve_apk=lambda a, s: apk, cases={"markor-save-then-restart"})
    u = plan.units[0]
    drifted = lanes._build_task(suite, u, "com.x", None,
                                drift=("markor-drift-fab-label", "uhabits-drift-add-label"))
    assert drifted.bug_spec["drift"] == ["markor-drift-fab-label"]
    plain = lanes._build_task(suite, u, "com.x", None)
    assert "drift" not in plain.bug_spec
    assert lanes.suite_drift(suite, ()) == []


def test_run_refuses_unknown_drift_ids_before_any_device(tmp_path):
    import click

    from qualgentbench import cli
    with pytest.raises(click.ClickException, match="no selected app declares"):
        cli._gate_drift("markor-drift-nope", "journey", None, None, APP)
    with pytest.raises(click.UsageError):
        cli._gate_drift("markor-drift-fab-label", "hunt", None, None, APP)
    assert cli._gate_drift("markor-drift-fab-label", "journey", None, None, APP) == (
        "markor-drift-fab-label",)
    assert cli._gate_drift(None, "journey", None, None, APP) == ()
    assert journey.resolve_drift(["markor-drift-fab-label"],
                                 journey.load_drift(journey.load_cases(APP)))


# ── the two sibling cleanups ─────────────────────────────────────────────────

def test_memory_prompt_uses_the_proposals_format_help():
    assert format_help() == props.FORMAT_HELP


def test_dedupe_latest_keeps_stages_apart_and_unstaged_results_as_before():
    def r(stage, ts):
        m = {"memory_stage": stage} if stage else {}
        return RunResult(task_id="t", task_version="v", task_type="journey_case",
                         agent="a", model="m", condition="mcp+mem-dynamic-small-none",
                         trial=1, passed=True, score=1.0, started_at=ts, ended_at=ts,
                         wall_time_sec=1, exit_code=0, metrics=m)
    staged = leaderboard.dedupe_latest([r("run-1", "1"), r("run-2", "2"), r("run-2", "3")])
    assert sorted((x.metrics["memory_stage"], x.started_at) for x in staged) == [
        ("run-1", "1"), ("run-2", "3")]
    plain = leaderboard.dedupe_latest([r(None, "1"), r(None, "2")])
    assert [x.started_at for x in plain] == ["2"]
