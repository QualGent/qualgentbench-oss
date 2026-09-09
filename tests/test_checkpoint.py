"""Checkpoint foundation: an episode dir that names its own run, an artifact path
that survives being copied to another machine, and a plan that records what the run
was measured against."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
