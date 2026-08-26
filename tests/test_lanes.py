"""The lane runner with a fake engine: no adb, no agent, no device."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from qualgentbench import lanes as L
from qualgentbench.result import RunResult, VerifierResult
from qualgentbench.scheduler import Estimator, RateLimitBackoff, ScheduleLog, Unit, plan_summary

APPS = {
    "birday": {"app": {"id": "birday", "name": "Birday", "package": "com.birday"},
               "exploration": {"id": "explore-birday", "step_budget": 100,
                               "features": [{"id": "a", "state": "broken", "bug_id": "b1"}]},
               "tasks": [{"id": "birday-t1", "bug_id": "b1", "type": "bug", "step_budget": 40}]},
    "notes": {"app": {"id": "notes", "name": "Notes", "package": "com.notes"},
              "exploration": {"id": "explore-notes", "step_budget": 60,
                              "features": [{"id": "a", "state": "ok"}]},
              "tasks": []},
}


def _unit(app, kind="bug_hunt", trial=1, est=100.0, task=None):
    return Unit(app, app, task or f"explore-{app}", kind, "hunt" if kind == "bug_hunt" else task,
                trial, est, "default")


def _plan(units):
    return L.RunPlan(units, {a: Path(f"/apk/{a}") for a in APPS},
                     {a: APPS[a] for a in APPS}, plan_summary(units, 2))


class FakeEngine:
    """Records which lane ran what; can be told to rate-limit or die on demand."""

    def __init__(self, rate_limit_keys=(), die_keys=(), delay=0.0):
        self.calls: list[tuple[str, str, int, int]] = []   # (device, task, trial, attempt)
        self.staged: list[tuple[str, str]] = []
        self.rate_limit = set(rate_limit_keys)
        self.die = set(die_keys)
        self.delay = delay

    async def prepare_app(self, session, device, apk, task):
        self.staged.append((device, task.app_name))
        task.bug_spec["apk_sha256"] = "deadbeef"
        return task.bundle_id

    async def run_episode(self, task, opts):
        self.calls.append((opts.device_serial, task.id, opts.trial, opts.attempt))
        if self.delay:
            await asyncio.sleep(self.delay)
        key = (task.id, opts.trial)
        if key in self.die:
            self.die.discard(key)
            raise RuntimeError("device gone")
        t0 = datetime(2026, 8, 20, tzinfo=timezone.utc)
        metrics = {"bugs_found": 1, "bugs_total": 1, "f1": 1.0, "hook_steps": 12,
                   "step_budget": 100, "device_actions": 30}
        if key in self.rate_limit:
            self.rate_limit.discard(key)
            metrics["failure_class"] = "rate_limited"
        assert task.bug_spec["apk_sha256"] == "deadbeef"
        return RunResult.build(
            task_id=task.id, task_version="v", task_type=opts.task_type, agent=opts.agent,
            model=opts.model, condition="raw", trial=opts.trial, started_at=t0,
            ended_at=t0 + timedelta(seconds=90), exit_code=0,
            verifier=VerifierResult(passed=True, score=1.0, metrics=metrics),
            artifact_dir=Path("/x"), run_id=opts.run_id,
            provenance={"lane": opts.lane, "lanes": opts.lanes, "attempt": opts.attempt},
        )


class FakeSession:
    async def force_release(self, device=None):
        pass


def _cfg(tmp_path, engine, devices, verify=None, backoff=None):
    async def _sleep(_):
        await asyncio.sleep(0)
    return L.LaneRun(
        agent="claude-code", model="m", mcp_server="", runs_dir=tmp_path, trials=2,
        run_id="r1", devices=devices, session=FakeSession(),
        console=Console(file=open(tmp_path / "out.txt", "w"), force_terminal=False, width=160),
        plain=True,
        hooks=L.Hooks(run_episode=engine.run_episode, prepare_app=engine.prepare_app,
                      verify=verify, sleep=_sleep),
        backoff=backoff, log=ScheduleLog(tmp_path / "schedule.jsonl"),
    )


async def test_every_unit_runs_once_across_lanes_with_provenance(tmp_path):
    units = [_unit("birday", est=300, trial=1), _unit("birday", est=300, trial=2),
             _unit("notes", est=100), _unit("birday", "bug_task", 1, 40, "birday-t1")]
    engine = FakeEngine()
    results = await L.run_lanes(_plan(units), _cfg(tmp_path, engine, ["emu-1", "emu-2"]))

    assert sorted((t, tr) for _, t, tr, _ in engine.calls) == \
        sorted((u.task_id, u.trial) for u in units)
    assert {r.run_id for r in results} == {"r1"}
    assert {r.provenance["lanes"] for r in results} == {2}
    assert {r.provenance["lane"] for r in results} <= {1, 2}
    # each device staged each app at most once per visit
    assert len(engine.staged) <= 4


async def test_affinity_keeps_a_lane_on_its_staged_app(tmp_path):
    units = [_unit("birday", est=100, trial=1), _unit("birday", est=100, trial=2),
             _unit("birday", est=100, trial=3)]
    engine = FakeEngine()
    await L.run_lanes(_plan(units), _cfg(tmp_path, engine, ["emu-1"]))
    assert engine.staged == [("emu-1", "Birday")]


async def test_rate_limit_holds_and_requeues_as_a_fresh_attempt(tmp_path):
    units = [_unit("birday", est=100, trial=1)]
    engine = FakeEngine(rate_limit_keys={("explore-birday", 1)})
    backoff = RateLimitBackoff(lanes=1, base_sec=0.01, rng=lambda: 1.0)
    results = await L.run_lanes(_plan(units), _cfg(tmp_path, engine, ["emu-1"], backoff=backoff))

    assert [(a) for _, _, _, a in engine.calls] == [1, 2]
    assert [r.metrics.get("failure_class") for r in results] == ["rate_limited", None]
    assert backoff.k == 0                       # the clean retry reset it
    events = [l.split('"event": "')[1].split('"')[0]
              for l in (tmp_path / "schedule.jsonl").read_text().splitlines()]
    assert events[:1] == ["run_start"] and "rate_limited" in events and "requeue" in events


async def test_a_dying_device_retires_its_lane_and_the_other_finishes(tmp_path):
    units = [_unit("birday", est=300, trial=1), _unit("birday", est=300, trial=2),
             _unit("notes", est=100)]
    # emu-2 dies on whatever it gets, twice
    engine = FakeEngine(die_keys={("explore-birday", 2), ("explore-notes", 1)})

    class Picky(FakeEngine):
        pass

    results = await L.run_lanes(_plan(units), _cfg(tmp_path, engine, ["emu-1", "emu-2"]))
    assert sorted((r.task_id, r.trial) for r in results) == \
        sorted((u.task_id, u.trial) for u in units)
    text = (tmp_path / "out.txt").read_text()
    assert "requeued" in text


async def test_hunts_are_verified_in_a_thread_and_the_status_lands_on_the_line(tmp_path):
    units = [_unit("birday", est=100)]
    engine = FakeEngine()

    def verify(result, progress):
        progress("1/1 reproductions")
        result.metrics["hybrid"] = {"f1": 1.0, "fp_rate": 0.0, "steps": 12, "overall": 0.9}
        return "[green]VERIFIED[/]", ["detail line"]

    await L.run_lanes(_plan(units), _cfg(tmp_path, engine, ["emu-1"], verify=verify))
    text = (tmp_path / "out.txt").read_text()
    assert "VERIFIED" in text and "detail line" in text and "Overall 90.0%" in text
    assert "▶ L1 emu-1 · birday · hunt · trial 1/2" in text


async def test_a_unit_is_dropped_after_max_attempts(tmp_path):
    units = [_unit("birday", est=100)]
    engine = FakeEngine(rate_limit_keys={("explore-birday", 1)})

    async def always_limited(task, opts):
        r = await engine.run_episode(task, opts)
        r.metrics["failure_class"] = "rate_limited"
        return r

    cfg = _cfg(tmp_path, engine, ["emu-1"],
               backoff=RateLimitBackoff(lanes=1, base_sec=0.001, rng=lambda: 1.0))
    cfg.hooks.run_episode = always_limited
    results = await L.run_lanes(_plan(units), cfg)
    assert len(results) == L.MAX_UNIT_ATTEMPTS
    assert "dropped" in (tmp_path / "out.txt").read_text()


def test_build_plan_enumerates_units_and_skips_missing_apks(tmp_path):
    apk = tmp_path / "buggy.apk"
    apk.write_bytes(b"x")
    est = Estimator(tmp_path, "claude-code", "m")

    def resolve(app, suite):
        return apk if app["id"] == "birday" else tmp_path / "missing.apk"

    skipped = []
    plan = L.build_plan(list(APPS.values()), mode="all", trials=2, lanes=1, estimator=est,
                        resolve_apk=resolve, on_skip=lambda app: skipped.append(app["id"]))
    assert skipped == ["notes"]
    assert sorted((u.task_id, u.kind, u.trial) for u in plan.units) == [
        ("birday-t1", "bug_task", 1), ("birday-t1", "bug_task", 2),
        ("explore-birday", "bug_hunt", 1), ("explore-birday", "bug_hunt", 2)]
    assert {u.est_source for u in plan.units} == {"budget"}
    assert plan.summary["episodes"] == 4 and plan.summary["eta_one_lane_sec"] > 0


async def test_lane_wall_time_is_recorded_on_the_result(tmp_path):
    units = [_unit("birday", est=100)]
    engine = FakeEngine()
    results = await L.run_lanes(_plan(units), _cfg(tmp_path, engine, ["emu-1"]))
    assert "lane_wall_sec" in results[0].provenance
    assert results[0].provenance["lane_wall_sec"] >= 0
