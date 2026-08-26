"""Run a benchmark's episodes over N devices.

One lane per device, all pulling from one longest-first queue. Each lane stages an
app on its device, runs every unit it can for that app, then moves on. A device
failure retires the lane and requeues its unit; a provider rate limit holds every
lane and requeues the unit as a fresh episode. Nothing here scores anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import bugs as bugmod
from .episode_runner import EpisodeOptions, _step_budget, prepare_app, run_episode
from .failures import RATE_LIMITED, is_excluded
from .progress import LaneBoard, describe, summarize_result
from .result import RunResult
from .scheduler import (
    APP_SWITCH_SEC,
    Estimator,
    RateLimitBackoff,
    ScheduleLog,
    Unit,
    WorkQueue,
    plan_summary,
    simulate,
)
from .schemas import Condition

logger = logging.getLogger(__name__)

# Consecutive device-level failures (install, device gone) before a lane retires.
MAX_LANE_FAILURES = 2
MAX_UNIT_ATTEMPTS = 4
PARKED_POLL_SEC = 5.0

# (result, progress_cb) -> (status_text, extra_lines). Runs in a worker thread.
VerifyFn = Callable[[RunResult, Callable[[str], None]], tuple[str, list[str]]]


@dataclass
class Hooks:
    """Seams for tests: the real engine by default, fakes in test_lanes."""
    run_episode: Callable[..., Awaitable[RunResult]] = run_episode
    prepare_app: Callable[..., Awaitable[str]] = prepare_app
    verify: VerifyFn | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class RunPlan:
    units: list[Unit]
    apks: dict[str, Path]
    suites: dict[str, dict[str, Any]]
    summary: dict[str, Any]


def build_plan(apps: list[dict[str, Any]], *, mode: str, trials: int, lanes: int,
               estimator: Estimator, resolve_apk: Callable[[dict, dict], Path],
               on_skip: Callable[[dict], None] | None = None,
               require_apk: bool = True) -> RunPlan:
    """Every (app, kind, trial) the run will execute, with a duration estimate each.
    `require_apk=False` plans apps whose APK is not on disk yet (preflight's ETA)."""
    units: list[Unit] = []
    apks: dict[str, Path] = {}
    suites: dict[str, dict[str, Any]] = {}
    for suite in apps:
        app = suite["app"]
        app_id = str(app["id"])
        apk = resolve_apk(app, suite)
        if require_apk and not apk.exists():
            if on_skip:
                on_skip(app)
            continue
        apks[app_id], suites[app_id] = apk, suite
        name = str(app.get("name") or app_id)
        if mode in ("all", "hunt"):
            hunt = bugmod.exploration_task(suite)
            budget = _step_budget(hunt)
            for trial in range(1, trials + 1):
                est, src = estimator.estimate(hunt.id, "bug_hunt", budget)
                units.append(Unit(app_id, name, hunt.id, "bug_hunt", "hunt", trial, est, src))
        if mode in ("all", "guided"):
            for gt in bugmod.suite_tasks(suite):
                kind = f"{str((gt.bug_spec or {}).get('type', 'bug'))}_task"
                budget = _step_budget(gt)
                for trial in range(1, trials + 1):
                    est, src = estimator.estimate(gt.id, kind, budget)
                    units.append(Unit(app_id, name, gt.id, kind, gt.id, trial, est, src))
    return RunPlan(units, apks, suites, plan_summary(units, lanes))


@dataclass
class LaneRun:
    agent: str
    model: str
    mcp_server: str
    runs_dir: Path
    trials: int
    run_id: str
    devices: list[str]
    session: Any
    console: Any
    source_dir: Path | None = None
    plain: bool | None = None
    hooks: Hooks = field(default_factory=Hooks)
    backoff: RateLimitBackoff | None = None
    log: ScheduleLog | None = None
    # Results land here as they finish, so a Ctrl-C still leaves the caller with
    # everything that completed.
    results: list[RunResult] = field(default_factory=list)


async def run_lanes(plan: RunPlan, cfg: LaneRun) -> list[RunResult]:
    """Drive every unit in `plan` to completion over `cfg.devices`. Returns the
    results in completion order; rate-limited attempts are included (excluded on
    the board, replaced by their retry through dedupe_latest)."""
    queue = WorkQueue(plan.units)
    n = len(cfg.devices)
    backoff = cfg.backoff or RateLimitBackoff(lanes=n)
    log = cfg.log or ScheduleLog(cfg.runs_dir / "_runs" / cfg.run_id / "schedule.jsonl")
    results = cfg.results
    inflight: dict[int, tuple[Unit, float]] = {}
    retired: set[int] = set()

    def remaining_sec() -> float:
        active = max(1, n - backoff.parked - len(retired))
        now = time.monotonic()
        longest_inflight = max((max(0.0, u.est_sec - (now - t0))
                                for u, t0 in inflight.values()), default=0.0)
        return max(longest_inflight,
                   simulate(queue.pending(), active, switch_sec=APP_SWITCH_SEC))

    log.write("run_start", run_id=cfg.run_id, agent=cfg.agent, model=cfg.model,
              devices=cfg.devices, episodes=len(plan.units), eta_sec=plan.summary["eta_sec"])
    async with LaneBoard(cfg.console, cfg.devices, len(plan.units), trials=cfg.trials,
                         remaining_sec=remaining_sec, plain=cfg.plain) as board:
        shared = _Shared(plan, cfg, queue, backoff, board, log, results, inflight, retired)
        tasks = [asyncio.create_task(_lane(i, dev, shared), name=f"lane-{i + 1}")
                 for i, dev in enumerate(cfg.devices)]
        try:
            await asyncio.gather(*tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            for t in tasks:
                t.cancel()
            raise
    log.write("run_end", run_id=cfg.run_id, completed=len(results),
              excluded=sum(1 for r in results if is_excluded(r.metrics)))
    return results


@dataclass
class _Shared:
    plan: RunPlan
    cfg: LaneRun
    queue: WorkQueue
    backoff: RateLimitBackoff
    board: LaneBoard
    log: ScheduleLog
    results: list[RunResult]
    inflight: dict[int, tuple[Unit, float]]
    retired: set[int]


def _build_task(suite: dict[str, Any], unit: Unit, bundle_id: str, apk_sha256: str | None):
    if unit.kind == "bug_hunt":
        task = bugmod.exploration_task(suite)
    else:
        task = next(gt for gt in bugmod.suite_tasks(suite) if gt.id == unit.task_id)
    task.bundle_id = bundle_id
    if apk_sha256 and isinstance(task.bug_spec, dict):
        task.bug_spec["apk_sha256"] = apk_sha256
    return task


def _requeue_or_drop(unit: Unit, reason: str, s: _Shared) -> None:
    if unit.attempt < MAX_UNIT_ATTEMPTS:
        s.queue.requeue(unit)
        s.board.note(f"requeued {describe(unit, s.cfg.trials)} — {reason} "
                     f"(attempt {unit.attempt}/{MAX_UNIT_ATTEMPTS})")
        s.log.write("requeue", reason=reason, **unit.as_dict())
    else:
        s.board.note(f"[red]dropped[/] {describe(unit, s.cfg.trials)} after "
                     f"{unit.attempt} attempts — {reason}")
        s.log.write("drop", reason=reason, **unit.as_dict())


async def _lane(i: int, device: str, s: _Shared) -> None:
    cfg, hooks, n = s.cfg, s.cfg.hooks, len(s.cfg.devices)
    staged: str | None = None
    bundle_id = ""
    apk_sha256: str | None = None
    failures = 0

    while True:
        if s.backoff.hold_remaining() > 0:
            await s.backoff.wait()
        if not s.backoff.lane_active(i):
            if not len(s.queue):
                return
            s.board.parked(i, True)
            await hooks.sleep(PARKED_POLL_SEC)
            continue
        s.board.parked(i, False)

        unit = s.queue.take(staged)
        if unit is None:
            return

        # ── stage the app once per switch ────────────────────────────────
        if unit.app_id != staged:
            s.board.staging(i, unit.app_id)
            s.log.write("stage", lane=i + 1, device=device, app=unit.app_id)
            try:
                hunt = bugmod.exploration_task(s.plan.suites[unit.app_id])
                await cfg.session.force_release(device)
                bundle_id = await hooks.prepare_app(
                    cfg.session, device, s.plan.apks[unit.app_id], hunt)
                apk_sha256 = (hunt.bug_spec or {}).get("apk_sha256")
                staged, failures = unit.app_id, 0
            except (RuntimeError, OSError) as exc:
                failures += 1
                staged = None
                s.log.write("stage_failed", lane=i + 1, device=device, app=unit.app_id,
                            error=str(exc))
                s.board.note(f"{s.board.lanes[i].tag} · install {unit.app_id} failed — {exc}")
                _requeue_or_drop(unit, f"install failed on {device}", s)
                if failures >= MAX_LANE_FAILURES:
                    _retire(i, f"{failures} consecutive device failures", s)
                    return
                continue

        # ── one episode ──────────────────────────────────────────────────
        task = _build_task(s.plan.suites[unit.app_id], unit, bundle_id, apk_sha256)
        budget = _step_budget(task)
        opts = EpisodeOptions(
            agent=cfg.agent, model=cfg.model, condition=Condition.no_routines,
            trial=unit.trial, mcp_server=cfg.mcp_server, runs_dir=cfg.runs_dir,
            verdict_fn=(bugmod.exploration_verdict if unit.kind == "bug_hunt"
                        else bugmod.guided_verdict),
            task_type=unit.kind, device_serial=device,
            tooling=("mcp" if cfg.mcp_server else "raw"), source_dir=cfg.source_dir,
            apk_path=s.plan.apks[unit.app_id], force_model=cfg.model,
            on_run_dir=lambda d, lane=i: s.board.set_run_dir(lane, d),
            run_id=cfg.run_id, lane=i + 1, lanes=n, attempt=unit.attempt,
        )
        s.board.start(i, unit, budget)
        s.inflight[i] = (unit, time.monotonic())
        s.log.write("start", lane=i + 1, device=device, **unit.as_dict())
        try:
            result = await hooks.run_episode(task, opts)
        except RuntimeError as exc:
            s.inflight.pop(i, None)
            failures += 1
            staged = None          # device state is unknown now; re-stage next time
            s.board.finish(i, unit, f"[red]ERROR[/] {exc}", ok=False)
            s.log.write("error", lane=i + 1, device=device, error=str(exc), **unit.as_dict())
            _requeue_or_drop(unit, str(exc)[:80], s)
            if failures >= MAX_LANE_FAILURES:
                _retire(i, f"{failures} consecutive failures ({str(exc)[:60]})", s)
                return
            continue
        s.inflight.pop(i, None)
        failures = 0

        if result.metrics.get("failure_class") == RATE_LIMITED:
            event = s.backoff.note_rate_limit()
            s.results.append(result)
            note = f"[yellow]RATE LIMITED[/] — all lanes hold {event['hold_sec']}s"
            if event["parked_now"]:
                note += f", parking a lane ({event['parked_lanes']} parked)"
            s.board.finish(i, unit, note, ok=False)
            s.log.write("rate_limited", lane=i + 1, device=device, **event, **unit.as_dict())
            _requeue_or_drop(unit, "rate limited", s)
            continue

        if (unpark := s.backoff.note_clean()) is not None:
            s.board.note(f"rate limits cleared — lane unparked ({unpark['parked_lanes']} parked)")
            s.log.write("unparked", **unpark)

        status, extra = "", []
        if unit.kind == "bug_hunt" and hooks.verify is not None:
            status, extra = await asyncio.to_thread(
                hooks.verify, result, lambda text, lane=i: s.board.verifying(lane, text))
        # wall_time_sec is the agent alone; the lane also paid for staging and the
        # replay verification (often longer than the agent). The estimator learns
        # from this number, so it covers what the next run will actually wait for.
        lane_wall = time.monotonic() - s.board.lanes[i].started
        result.provenance["lane_wall_sec"] = round(lane_wall)
        _persist_provenance(result)
        s.results.append(result)
        summary = summarize_result(unit.kind, result.metrics)
        if status:
            summary += f" · {status}"
        s.board.finish(i, unit, summary, extra_lines=extra)
        s.log.write("finish", lane=i + 1, device=device, wall_sec=round(result.wall_time_sec),
                    lane_wall_sec=round(lane_wall), excluded=is_excluded(result.metrics),
                    **unit.as_dict())


def _persist_provenance(result: RunResult) -> None:
    path = Path(result.artifact_dir) / "result.json"
    try:
        on_disk = json.loads(path.read_text())
        on_disk["provenance"] = result.provenance
        path.write_text(json.dumps(on_disk, indent=2))
    except (OSError, ValueError):
        logger.debug("provenance not persisted for %s", path, exc_info=True)


def _retire(i: int, why: str, s: _Shared) -> None:
    s.retired.add(i)
    s.board.retired(i, why)
    s.log.write("lane_retired", lane=i + 1, device=s.cfg.devices[i], reason=why)
    if len(s.retired) == len(s.cfg.devices):
        s.board.note("[red]every lane retired — stopping with what completed[/]")
