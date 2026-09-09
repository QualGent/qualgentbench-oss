"""Checkpoint foundation: an episode dir that names its own run, an artifact path
that survives being copied to another machine, and a plan that records what the run
was measured against."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import pytest

from qualgentbench import checkpoint
from qualgentbench.cli import _write_plan
from qualgentbench.result import (
    RunResult,
    VerifierResult,
    relative_artifact_dir,
    resolve_artifact_dir,
)

SUITE = {
    "app": {"id": "birday", "name": "Birday", "package": "com.birday",
            "platform": "android", "difficulty": "easy"},
    "apk": {"repo": "qualgent/qualgentbench-apps", "filename": "easy/birday.apk",
            "sha256": "a" * 64},
    "exploration": {"id": "explore-birday", "features": [{"id": "a", "state": "broken"}]},
    "tasks": [{"id": "birday-t1", "bug_id": "b1", "type": "bug"}],
}


# ── A. episode.json marker ────────────────────────────────────────────────────


def test_marker_carries_the_unit_identity(tmp_path):
    checkpoint.write_episode_marker(
        tmp_path, run_id="r1", app_id="birday", task_id="birday-t1",
        kind="bug_task", trial=2, attempt=3, segment=1,
        started_at=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc))

    on_disk = json.loads((tmp_path / "episode.json").read_text())
    assert on_disk == {
        "schema_version": checkpoint.SCHEMA_VERSION,
        "run_id": "r1", "app_id": "birday", "task_id": "birday-t1",
        "kind": "bug_task", "trial": 2, "attempt": 3, "segment": 1,
        "started_at": "2026-09-08T12:00:00+00:00",
    }


def test_marker_defaults_to_first_attempt_of_the_first_segment(tmp_path):
    checkpoint.write_episode_marker(tmp_path, run_id="r1", app_id="birday",
                                    task_id="explore-birday", kind="bug_hunt", trial=1)
    m = checkpoint.read_episode_marker(tmp_path)
    assert (m["attempt"], m["segment"]) == (1, 0)
    # started_at is filled in even when the caller does not pass one.
    assert datetime.fromisoformat(m["started_at"]).tzinfo is not None


def test_reading_a_dir_without_a_marker_is_not_an_error(tmp_path):
    # An episode dir from a harness older than this one, or one that never started.
    assert checkpoint.read_episode_marker(tmp_path) is None
    (tmp_path / "episode.json").write_text("{not json")
    assert checkpoint.read_episode_marker(tmp_path) is None


async def _marker_from_a_killed_episode(tmp_path, monkeypatch, **overrides) -> dict:
    """Drive the real `run_episode` far enough to create its episode dir, then stop it
    dead — no meter, no adapter, no device. Returns the marker it left behind."""
    from qualgentbench import episode_runner as er
    from qualgentbench.schemas import Condition
    from qualgentbench.task import BenchmarkTask

    class _Stop(Exception):
        pass

    class _FakeSession:
        def __init__(self, *_a, **_kw):
            pass

        async def force_release(self, *_a, **_kw): pass
        async def check_device_available(self, *_a, **_kw): pass
        async def reset_app(self, *_a, **_kw): pass
        async def launch_app(self, *_a, **_kw): pass
        async def first_available_device(self): return "emulator-5554"

    async def _noop(*_a, **_kw): pass

    monkeypatch.setattr(er, "DeviceSession", _FakeSession)
    for fn in ("normalize_app_env", "wipe_shared_storage", "run_device_setup",
               "write_bug_flags", "isolate_app_under_test"):
        monkeypatch.setattr(er, fn, _noop)
    # First thing the runner touches after the dir exists; stops the episode with the
    # marker already written and no meter, adapter or device in play.
    monkeypatch.setattr(er, "InteractionLog", lambda *_a, **_kw: (_ for _ in ()).throw(_Stop()))

    task = BenchmarkTask(id="birday-t1", name="t", instruction="do it", app_file_id="",
                         app_name="Birday", platform="android", bundle_id="com.birday",
                         bug_spec={"app_id": "birday"})
    runs_dir = tmp_path / "runs"
    opts = er.EpisodeOptions(**{
        "agent": "claude-code", "model": "anthropic/claude-opus-4-8",
        "condition": Condition.no_routines, "trial": 2, "mcp_server": "",
        "runs_dir": runs_dir, "task_type": "bug_task",
        "device_serial": "emulator-5554", "run_id": "run-abc", "attempt": 3,
        "segment": 1, "app_id": "birday", **overrides})

    with pytest.raises(_Stop):
        await er.run_episode(task, opts)

    markers = list(runs_dir.glob("*/*/episode.json"))
    assert len(markers) == 1, "every episode dir gets exactly one marker"
    # The dir it marks holds no result.json — that pair is the orphan signature.
    assert not (markers[0].parent / "result.json").exists()
    return json.loads(markers[0].read_text())


async def test_run_episode_writes_the_marker_before_the_agent_starts(tmp_path, monkeypatch):
    """The marker has to be on disk before anything can kill the episode — that is
    what makes an interrupted episode detectable rather than invisible."""
    m = await _marker_from_a_killed_episode(tmp_path, monkeypatch)
    assert (m["run_id"], m["app_id"], m["task_id"], m["kind"]) == \
        ("run-abc", "birday", "birday-t1", "bug_task")
    assert (m["trial"], m["attempt"], m["segment"]) == (2, 3, 1)


async def test_marker_falls_back_to_the_app_id_on_the_task(tmp_path, monkeypatch):
    """A bare `run_episode` call passes no app_id; the bug spec still names the app,
    so the marker is never anonymous."""
    m = await _marker_from_a_killed_episode(tmp_path, monkeypatch, app_id="")
    assert m["app_id"] == "birday"


# ── B. relative artifact_dir ──────────────────────────────────────────────────


def _result(runs_dir, episode_dir) -> RunResult:
    t0 = datetime(2026, 9, 8, tzinfo=timezone.utc)
    return RunResult.build(
        task_id="birday-t1", task_version="v", task_type="bug_task", agent="claude-code",
        model="m", condition="raw", trial=1, started_at=t0,
        ended_at=t0 + timedelta(minutes=1), exit_code=0,
        verifier=VerifierResult(passed=True, score=1.0),
        artifact_dir=episode_dir, runs_dir=runs_dir, run_id="r1")


def test_artifact_dir_is_stored_relative_to_the_runs_dir(tmp_path):
    runs_dir = tmp_path / "runs"
    episode = runs_dir / "birday-t1" / "2026-09-08T00-00-00Z_birday-t1_claude-code_m_raw_trial-1"
    episode.mkdir(parents=True)

    r = _result(runs_dir, episode)
    assert r.artifact_dir == \
        "birday-t1/2026-09-08T00-00-00Z_birday-t1_claude-code_m_raw_trial-1"
    assert not Path(r.artifact_dir).is_absolute()
    assert resolve_artifact_dir(runs_dir, r) == episode


def test_a_relative_result_resolves_after_the_runs_tree_moves(tmp_path):
    """The whole point: score on one machine, read on another."""
    origin, elsewhere = tmp_path / "origin" / "runs", tmp_path / "elsewhere" / "runs"
    episode = origin / "birday-t1" / "ep"
    episode.mkdir(parents=True)
    r = _result(origin, episode)

    moved = elsewhere / "birday-t1" / "ep"
    moved.mkdir(parents=True)
    (moved / "result.json").write_text(r.model_dump_json())

    reloaded = RunResult.model_validate_json((moved / "result.json").read_text())
    assert resolve_artifact_dir(elsewhere, reloaded) == moved
    assert (resolve_artifact_dir(elsewhere, reloaded) / "result.json").exists()


def test_legacy_absolute_artifact_dir_still_resolves(tmp_path):
    """Results written before paths went relative hold an absolute path; the runs dir
    they are read against must be ignored, not prepended."""
    legacy = tmp_path / "old-machine" / "runs" / "birday-t1" / "ep"
    legacy.mkdir(parents=True)
    on_disk = json.loads(_result(None, legacy).model_dump_json())
    assert on_disk["artifact_dir"] == str(legacy)

    r = RunResult.model_validate(on_disk)
    assert resolve_artifact_dir(tmp_path / "some" / "other" / "runs", r) == legacy


def test_an_episode_dir_outside_the_runs_dir_stays_absolute(tmp_path):
    outside = tmp_path / "scratch" / "ep"
    outside.mkdir(parents=True)
    stored = relative_artifact_dir(tmp_path / "runs", outside)
    assert Path(stored).is_absolute()
    assert resolve_artifact_dir(tmp_path / "runs", stored) == outside


def test_a_result_with_no_artifact_dir_resolves_to_none(tmp_path):
    assert relative_artifact_dir(tmp_path, None) == ""
    assert resolve_artifact_dir(tmp_path, RunResult.model_construct(artifact_dir="")) is None
    assert resolve_artifact_dir(tmp_path, None) is None


# ── C. plan.json environment fingerprint ──────────────────────────────────────


def test_plan_json_records_the_environment_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("QGB_IMAGE_DIGEST", "sha256:cafebabe")
    _write_plan(tmp_path, "r1", {"units": 4, "eta_sec": 900}, apps=[SUITE], mode="guided",
                agent="claude-code", model="anthropic/claude-opus-4-8",
                devices=["emulator-5554"])

    plan = json.loads((tmp_path / "_runs" / "r1" / "plan.json").read_text())
    assert plan["run_id"] == "r1"
    # A run id can now span more than one sitting; the first is segment 0.
    assert plan["segment"] == 0
    env = plan["environment"]
    assert env["package_version"] == checkpoint.package_version()
    assert env["image_digest"] == "sha256:cafebabe"
    assert env["apps"]["birday"]["apk_sha256"] == "a" * 64
    assert env["apps"]["birday"]["spec_version"].startswith("sha256:")
    # Everything the old plan carried is still there.
    assert (plan["agent"], plan["model"], plan["mode"]) == \
        ("claude-code", "anthropic/claude-opus-4-8", "guided")
    assert plan["devices"] == ["emulator-5554"] and plan["units"] == 4


def test_image_digest_is_null_off_a_container(tmp_path, monkeypatch):
    monkeypatch.delenv("QGB_IMAGE_DIGEST", raising=False)
    _write_plan(tmp_path, "r1", {}, apps=[SUITE], mode="hunt", agent="a", model="m")
    plan = json.loads((tmp_path / "_runs" / "r1" / "plan.json").read_text())
    assert plan["environment"]["image_digest"] is None


def test_spec_version_tracks_the_spec_contents_not_its_formatting():
    baseline = checkpoint.spec_version(SUITE)
    # Re-ordering keys is not a change; editing a seeded feature is.
    reordered = {k: SUITE[k] for k in reversed(list(SUITE))}
    assert checkpoint.spec_version(reordered) == baseline

    edited = json.loads(json.dumps(SUITE))
    edited["exploration"]["features"][0]["state"] = "ok"
    assert checkpoint.spec_version(edited) != baseline

    declared = json.loads(json.dumps(SUITE))
    declared["app"]["spec_version"] = "v7"
    assert checkpoint.spec_version(declared) == "v7"


def test_fingerprint_says_null_when_the_apk_has_no_published_hash():
    """A local build or a QUALGENTBENCH_APK_* override cannot be proven identical
    across machines; the fingerprint must say so rather than invent a hash."""
    local = json.loads(json.dumps(SUITE))
    del local["apk"]
    fp = checkpoint.environment_fingerprint([local])
    assert fp["apps"]["birday"]["apk_sha256"] is None

    # An explicit per-app override (journey mode runs a different build) wins.
    fp = checkpoint.environment_fingerprint([SUITE], apk_sha256={"birday": "b" * 64})
    assert fp["apps"]["birday"]["apk_sha256"] == "b" * 64


def test_a_journey_plan_fingerprints_its_test_case_file_too(tmp_path, monkeypatch):
    """Journey units and their oracles come from the test-case and truth documents,
    not from the benchmark spec — hashing the spec alone would call an edited case
    unchanged and let a resume measure something else under the same run id."""
    from qualgentbench import journey

    doc = {"test_cases": [{"id": "c1", "steps": ["open the app"]}]}
    monkeypatch.setattr(journey, "load_cases", lambda app_id: doc)
    monkeypatch.setattr(journey, "load_truth", lambda app_id: {})
    monkeypatch.setattr(journey, "apk_meta", lambda app_id: None)

    def spec_version_of(run_id, mode):
        _write_plan(tmp_path, run_id, {}, apps=[SUITE], mode=mode, agent="a", model="m")
        plan = json.loads((tmp_path / "_runs" / run_id / "plan.json").read_text())
        return plan["environment"]["apps"]["birday"]["spec_version"]

    before, guided = spec_version_of("r1", "journey"), spec_version_of("r2", "guided")
    doc["test_cases"][0]["steps"].append("tap Save")
    assert spec_version_of("r3", "journey") != before
    # Guided mode never reads those files, so its fingerprint must not move with them.
    assert spec_version_of("r4", "guided") == guided != before


# ── D. completion state and orphan cleanup ────────────────────────────────────


def _started(runs_dir: Path, task_id: str, *, run_id: str, app_id: str = "birday",
             trial: int = 1, name: str | None = None, marker: bool = True) -> Path:
    """An episode dir as it looks the moment it is created: marked, no result yet."""
    ep = runs_dir / task_id / (name or f"ep_{task_id}_trial-{trial}")
    ep.mkdir(parents=True)
    if marker:
        checkpoint.write_episode_marker(ep, run_id=run_id, app_id=app_id, task_id=task_id,
                                        kind="bug_task", trial=trial)
    return ep


def _finished(runs_dir: Path, task_id: str, *, run_id: str, app_id: str = "birday",
              trial: int = 1, name: str | None = None, marker: bool = True,
              metrics: dict | None = None) -> Path:
    """An episode that ran to the end: the marker plus the scored result.json."""
    ep = _started(runs_dir, task_id, run_id=run_id, app_id=app_id, trial=trial,
                  name=name, marker=marker)
    t0 = datetime(2026, 9, 8, tzinfo=timezone.utc)
    RunResult.build(
        task_id=task_id, task_version="v", task_type="bug_task", agent="claude-code",
        model="anthropic/claude-opus-4-8", condition="raw", trial=trial, started_at=t0,
        ended_at=t0 + timedelta(minutes=2), exit_code=0,
        verifier=VerifierResult(passed=True, score=1.0, metrics=metrics or {}),
        artifact_dir=ep, runs_dir=runs_dir, run_id=run_id,
    ).write(ep / "result.json")
    return ep


def test_state_separates_done_from_excluded_from_interrupted(tmp_path):
    runs = tmp_path / "runs"
    _finished(runs, "birday-t1", run_id="r1")
    _finished(runs, "birday-t2", run_id="r1", metrics={"infra_failure": True})
    orphan = _started(runs, "birday-t3", run_id="r1")
    # Another run's work, in the same tree: neither ours to count nor ours to discard.
    _started(runs, "birday-t4", run_id="r2")
    _finished(runs, "birday-t5", run_id="r2")

    st = checkpoint.state(runs, "r1")
    assert st.done == {("birday", "birday-t1", 1)}
    assert [ref.key for ref in st.excluded] == [("birday", "birday-t2", 1)]
    assert [ref.path for ref in st.orphans] == [orphan]
    # An excluded attempt measured nothing, so its unit is still owed.
    assert st.is_done("birday", "birday-t1", 1)
    assert not st.is_done("birday", "birday-t2", 1)
    assert not st.is_done("birday", "birday-t3", 1)


def test_a_result_written_before_markers_existed_still_counts_as_done(tmp_path):
    """An episode from an older harness cannot name its app; keying it on the task
    alone is what stops a resume re-running demonstrably finished work."""
    runs = tmp_path / "runs"
    _finished(runs, "birday-t1", run_id="r1", marker=False)

    st = checkpoint.state(runs, "r1")
    assert st.done == {("", "birday-t1", 1)}
    assert st.is_done("birday", "birday-t1", 1)


def test_an_interrupted_episode_is_quarantined_not_deleted(tmp_path):
    runs = tmp_path / "runs"
    orphan = _started(runs, "birday-t3", run_id="r1")
    (orphan / "agent").mkdir()
    (orphan / "agent" / "transcript.txt").write_text("...killed here")

    moved = checkpoint.discard_orphans(runs, "r1")

    assert moved == [runs / "_discarded" / "r1" / "birday-t3" / orphan.name]
    assert not orphan.exists()
    # The transcript is usually the only record of WHY a run stopped.
    assert (moved[0] / "agent" / "transcript.txt").read_text() == "...killed here"
    # Out of `runs/*/*/` it is no longer scannable as an episode of this run.
    assert checkpoint.state(runs, "r1").orphans == []


def test_a_second_interruption_of_the_same_episode_keeps_both(tmp_path):
    runs = tmp_path / "runs"
    for _ in range(2):
        _started(runs, "birday-t3", run_id="r1", name="ep")
        checkpoint.discard_orphans(runs, "r1")
    assert sorted(p.name for p in (runs / "_discarded" / "r1" / "birday-t3").iterdir()) \
        == ["ep", "ep~2"]


def test_only_this_run_ids_orphans_are_discarded(tmp_path):
    runs = tmp_path / "runs"
    mine = _started(runs, "birday-t3", run_id="r1")
    theirs = _started(runs, "birday-t4", run_id="r2")
    # No marker at all: unattributable, so not anyone's to move.
    anonymous = _started(runs, "birday-t5", run_id="r1", marker=False)

    checkpoint.discard_orphans(runs, "r1")

    assert not mine.exists()
    assert theirs.exists() and anonymous.exists()


# ── E. resume ─────────────────────────────────────────────────────────────────


def test_compatibility_names_every_way_the_environment_moved(monkeypatch):
    monkeypatch.delenv("QGB_IMAGE_DIGEST", raising=False)
    planned = checkpoint.environment_fingerprint([SUITE])
    assert checkpoint.compatibility(planned, checkpoint.environment_fingerprint([SUITE])) == []

    monkeypatch.setenv("QGB_IMAGE_DIGEST", "sha256:other")
    republished = json.loads(json.dumps(SUITE))
    republished["apk"]["sha256"] = "b" * 64
    republished["tasks"].append({"id": "birday-t2", "bug_id": "b2", "type": "bug"})

    diffs = checkpoint.compatibility(planned, checkpoint.environment_fingerprint([republished]))
    assert any(d.startswith("image_digest:") for d in diffs)
    assert any("apk_sha256" in d for d in diffs)
    assert any("spec_version" in d for d in diffs)

    # An app the plan never knew about is a difference too — its units cannot be ours.
    monkeypatch.delenv("QGB_IMAGE_DIGEST", raising=False)
    other = json.loads(json.dumps(SUITE))
    other["app"]["id"] = "newapp"
    assert checkpoint.compatibility(planned, checkpoint.environment_fingerprint([other])) \
        == ["newapp: not in the plan's environment"]


def _write_run_plan(runs_dir: Path, run_id: str, task_ids: list[str], *,
                    apps: list[dict] | None = None, mode: str = "guided") -> None:
    """A plan.json exactly as `run` writes one, for a run of `task_ids`."""
    from qualgentbench.scheduler import Unit, plan_summary

    units = [Unit("birday", "Birday", t, "bug_task", t, 1, 120.0, "default") for t in task_ids]
    _write_plan(runs_dir, run_id, plan_summary(units, 1), apps=apps or [SUITE], mode=mode,
                agent="claude-code", model="anthropic/claude-opus-4-8",
                devices=["emu-1"], trials=1)


class _FakeLanes:
    """Stands in for `run_lanes`: records what it was handed and writes the
    result.json each unit would have produced."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir, self.seen, self.cfg = runs_dir, [], None

    async def __call__(self, plan, cfg):
        self.cfg = cfg
        self.seen = sorted((u.app_id, u.task_id, u.trial) for u in plan.units)
        for unit in plan.units:
            ep = _finished(self.runs_dir, unit.task_id, run_id=cfg.run_id, trial=unit.trial,
                           name=f"seg{cfg.segment}_{unit.task_id}")
            cfg.results.append(
                RunResult.model_validate_json((ep / "result.json").read_text()))
        return cfg.results


class _FakeSession:
    async def available_devices(self):
        return ["emu-1"]


def _stub_corpus(monkeypatch, tmp_path, lanes_fn, apps: list[dict] | None = None):
    """One app, one APK on disk, no lanes: everything `_run_episodes` needs bar a device."""
    from qualgentbench import bugs, cli, lanes

    apk = tmp_path / "birday.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(bugs, "load_apps", lambda *a, **kw: apps or [SUITE])
    monkeypatch.setattr(cli, "_resolve_app_apk", lambda app, spec=None, mode="hunt": apk)
    monkeypatch.setattr(lanes, "run_lanes", lanes_fn)


async def _resume(runs_dir: Path, run_id: str, **kwargs):
    from qualgentbench import cli

    plan = checkpoint.load_plan(runs_dir, run_id)
    return await cli._run_episodes(
        [plan.model], plan.agent, _FakeSession(), "", runs_dir, plan.trials,
        mode=plan.mode, devices=["emu-1"], plain=True, yes=True, resume=plan, **kwargs)


async def test_resume_runs_only_what_is_left_and_discards_the_interrupted_episode(
        tmp_path, monkeypatch):
    """The whole contract in one run: 5 planned, 3 done, 1 killed mid-episode."""
    runs = tmp_path / "runs"
    tasks = [f"birday-t{i}" for i in range(1, 6)]
    _write_run_plan(runs, "r1", tasks)
    for task in tasks[:3]:
        _finished(runs, task, run_id="r1")
    orphan = _started(runs, "birday-t4", run_id="r1")

    engine = _FakeLanes(runs)
    _stub_corpus(monkeypatch, tmp_path, engine)
    out = await _resume(runs, "r1")

    # Exactly the two units that never produced a result — the orphan does not count.
    assert engine.seen == [("birday", "birday-t4", 1), ("birday", "birday-t5", 1)]
    assert len(out) == 2
    # Same run id, next sitting; provenance.segment is stamped from this.
    assert (engine.cfg.run_id, engine.cfg.segment) == ("r1", 1)

    # The interrupted episode is quarantined, not deleted.
    assert not orphan.exists()
    assert (runs / "_discarded" / "r1" / "birday-t4" / orphan.name / "episode.json").exists()

    # One run id, five rows: the board covers the whole run, not just this segment.
    board = json.loads((runs / "_runs" / "r1" / "board.json").read_text())
    assert board["run_id"] == "r1"
    assert sorted(row["task_id"] for row in board["episodes"]) == tasks

    # The plan's scope is frozen; only the segment counter moves.
    plan = json.loads((runs / "_runs" / "r1" / "plan.json").read_text())
    assert [u["task"] for u in plan["units"]] == tasks
    assert plan["segment"] == 1

    # And the sitting is auditable from the event log.
    events = [json.loads(line) for line
              in (runs / "_runs" / "r1" / "schedule.jsonl").read_text().splitlines()]
    resumed = next(e for e in events if e["event"] == "resume")
    assert (resumed["segment"], resumed["done"], resumed["remaining"]) == (1, 3, 2)
    assert resumed["discarded"] == 1 and resumed["devices"] == ["emu-1"] and resumed["host"]


async def test_resume_re_runs_an_excluded_attempt(tmp_path, monkeypatch):
    """A rate-limited or infra-failed episode is on disk but measured nothing, so the
    unit it belongs to is still owed."""
    runs = tmp_path / "runs"
    _write_run_plan(runs, "r1", ["birday-t1", "birday-t2"])
    _finished(runs, "birday-t1", run_id="r1")
    _finished(runs, "birday-t2", run_id="r1", metrics={"failure_class": "rate_limited"})

    engine = _FakeLanes(runs)
    _stub_corpus(monkeypatch, tmp_path, engine)
    await _resume(runs, "r1")

    assert engine.seen == [("birday", "birday-t2", 1)]


async def test_resume_does_not_re_enumerate_the_scope_from_the_specs(tmp_path, monkeypatch):
    """A task added to the spec after the run started is not part of that run: the
    plan's unit list is the scope, and a resume finishes exactly it."""
    runs = tmp_path / "runs"
    _write_run_plan(runs, "r1", ["birday-t1", "birday-t2"])
    _finished(runs, "birday-t1", run_id="r1")

    grown = json.loads(json.dumps(SUITE))
    grown["tasks"].append({"id": "birday-t9", "bug_id": "b9", "type": "bug"})
    engine = _FakeLanes(runs)
    # Only the APK and spec hashes gate a resume, and neither moved here.
    _stub_corpus(monkeypatch, tmp_path, engine, apps=[grown])
    await _resume(runs, "r1", force_resume=True)

    assert engine.seen == [("birday", "birday-t2", 1)]


async def test_a_changed_fingerprint_refuses_the_resume_without_force(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _write_run_plan(runs, "r1", ["birday-t1", "birday-t2"])
    _finished(runs, "birday-t1", run_id="r1")
    _started(runs, "birday-t2", run_id="r1")

    republished = json.loads(json.dumps(SUITE))
    republished["apk"]["sha256"] = "b" * 64
    engine = _FakeLanes(runs)
    _stub_corpus(monkeypatch, tmp_path, engine, apps=[republished])

    with pytest.raises(click.ClickException) as raised:
        await _resume(runs, "r1")
    assert "apk_sha256" in str(raised.value) and "--force-resume" in str(raised.value)
    # Refused means untouched: nothing ran and nothing was moved.
    assert engine.seen == [] and not (runs / "_discarded").exists()

    out = await _resume(runs, "r1", force_resume=True)
    assert engine.seen == [("birday", "birday-t2", 1)] and len(out) == 1


async def test_an_already_complete_run_is_a_clean_exit(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _write_run_plan(runs, "r1", ["birday-t1"])
    _finished(runs, "birday-t1", run_id="r1")
    orphan = _started(runs, "birday-t1", run_id="r1", name="killed-retry")

    engine = _FakeLanes(runs)
    _stub_corpus(monkeypatch, tmp_path, engine)
    with pytest.raises(SystemExit) as raised:
        await _resume(runs, "r1")

    assert raised.value.code == 0
    assert engine.seen == []
    # Still swept up: the run is over, so its leftovers are not coming back.
    assert not orphan.exists()


def test_resume_refuses_a_run_id_with_no_plan(tmp_path):
    with pytest.raises(checkpoint.CheckpointError) as raised:
        checkpoint.load_plan(tmp_path / "runs", "nope")
    assert "nope" in str(raised.value)


def test_resume_refuses_a_plan_older_than_checkpointing(tmp_path):
    """plan.json without a unit list cannot say what the run's scope was."""
    meta = tmp_path / "runs" / "_runs" / "r1"
    meta.mkdir(parents=True)
    (meta / "plan.json").write_text(json.dumps({"run_id": "r1", "agent": "claude-code"}))

    with pytest.raises(checkpoint.CheckpointError) as raised:
        checkpoint.load_plan(tmp_path / "runs", "r1")
    assert "scope" in str(raised.value)


def test_scope_flags_cannot_be_combined_with_resume():
    """--resume takes the scope from plan.json, so a scope flag is a contradiction —
    a usage error, not a silently ignored preference."""
    from click.testing import CliRunner

    from qualgentbench.cli import main

    for flag in (["--tier", "easy"], ["--app", "birday"], ["--mode", "hunt"],
                 ["--trials", "3"], ["--models", "m"]):
        out = CliRunner().invoke(main, ["run", "--resume", "r1", *flag])
        assert out.exit_code == 2, flag
        assert "cannot be combined with" in out.output and flag[0] in out.output

    # The transport flags are exactly the ones a resume MAY change.
    out = CliRunner().invoke(main, ["run", "--resume", "r1", "--lanes", "2",
                                    "--runs-dir", "/tmp/qgb-no-such-tree"])
    assert out.exit_code == 1 and "No plan for run r1" in out.output


async def test_a_resume_with_no_apk_for_the_remaining_work_says_so(tmp_path, monkeypatch):
    """Units left but nothing runnable is not a finished run, and must not print like
    one — the fix is to fetch the APK, not to shrug."""
    runs = tmp_path / "runs"
    _write_run_plan(runs, "r1", ["birday-t1", "birday-t2"])
    _finished(runs, "birday-t1", run_id="r1")

    engine = _FakeLanes(runs)
    _stub_corpus(monkeypatch, tmp_path, engine)
    from qualgentbench import cli
    monkeypatch.setattr(cli, "_resolve_app_apk",
                        lambda app, spec=None, mode="hunt": tmp_path / "gone.apk")

    with pytest.raises(click.ClickException) as raised:
        await _resume(runs, "r1")
    assert "1 unit(s) left" in str(raised.value)
    assert engine.seen == []
