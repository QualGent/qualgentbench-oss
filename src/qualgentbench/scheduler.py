"""Episode scheduling across N devices.

A unit is one episode: (app, kind, trial). Units are pulled from one queue,
longest-first, by whichever lane is free — with a preference for the app the lane
already has staged. The same rule drives the pre-run ETA, so the estimate and the
run agree by construction.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .failures import is_excluded

# A lane keeps its staged app unless another app's longest unit is this much longer.
AFFINITY_RATIO = 2.0
# Uninstall + install + cold-snapshot staging when a lane switches app.
APP_SWITCH_SEC = 45.0
# Headless AVD from `emulator` to sys.boot_completed; boots run in parallel.
EMULATOR_BOOT_SEC = 90.0

# Fallbacks when no history exists. Wall time covers the agent AND the replay
# verification that follows a hunt. Budgets are caps, not typical use: measured
# hunts land around 1.5-2.5 s per budget step all-in.
SEC_PER_STEP = 2.5
DEFAULT_SEC = {"bug_hunt": 420.0, "bug_task": 180.0, "clean_task": 150.0}
MIN_HISTORY = 3
# Shorter than this and the agent never really ran; not a duration sample.
_MIN_SAMPLE_SEC = 20.0


@dataclass
class Unit:
    app_id: str
    app_name: str
    task_id: str
    kind: str            # bug_hunt | bug_task | clean_task
    label: str           # "hunt" or the guided task id
    trial: int
    est_sec: float
    est_source: str      # history | budget | default
    attempt: int = 1

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.app_id, self.task_id, self.trial)

    def as_dict(self) -> dict[str, Any]:
        return {"app": self.app_id, "task": self.task_id, "kind": self.kind,
                "trial": self.trial, "est_sec": round(self.est_sec),
                "est_source": self.est_source, "attempt": self.attempt}


class Estimator:
    """Per-unit duration from prior result.json files, most specific key first:
    (agent, model, task) → task → budget → kind default."""

    def __init__(self, runs_dir: Path, agent: str, model: str) -> None:
        self.agent, self.model = agent, model
        self._by_arm_task: dict[tuple[str, str, str], list[float]] = {}
        self._by_task: dict[str, list[float]] = {}
        self._load(runs_dir)

    def _load(self, runs_dir: Path) -> None:
        for path in runs_dir.glob("*/*/result.json"):
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            # A non-result (died before reporting, rate-limited, contaminated) says
            # nothing about how long a real episode takes.
            if is_excluded(d.get("metrics") or {}):
                continue
            # Prefer the lane's full time (agent + staging + replay verification);
            # wall_time_sec alone is the agent and under-counts hunts by the replay.
            wall = (d.get("provenance") or {}).get("lane_wall_sec")
            if not isinstance(wall, (int, float)):
                wall = d.get("wall_time_sec")
            if not isinstance(wall, (int, float)) or wall < _MIN_SAMPLE_SEC:
                continue
            task = str(d.get("task_id") or "")
            self._by_task.setdefault(task, []).append(float(wall))
            self._by_arm_task.setdefault(
                (str(d.get("agent")), str(d.get("model")), task), []).append(float(wall))

    def estimate(self, task_id: str, kind: str, step_budget: int | None) -> tuple[float, str]:
        for samples in (self._by_arm_task.get((self.agent, self.model, task_id)),
                        self._by_task.get(task_id)):
            if samples and len(samples) >= MIN_HISTORY:
                return statistics.median(samples), "history"
        if step_budget:
            return float(step_budget) * SEC_PER_STEP, "budget"
        return DEFAULT_SEC.get(kind, 300.0), "default"


class WorkQueue:
    """Longest-first pull queue with app affinity. asyncio is single-threaded, so
    no lock: `take` runs to completion between awaits."""

    def __init__(self, units: Iterable[Unit]) -> None:
        self._pending: list[Unit] = sorted(units, key=lambda u: -u.est_sec)

    def __len__(self) -> int:
        return len(self._pending)

    def take(self, staged_app: str | None) -> Unit | None:
        if not self._pending:
            return None
        longest = self._pending[0]
        if staged_app:
            same = next((u for u in self._pending if u.app_id == staged_app), None)
            if same is not None and same.est_sec * AFFINITY_RATIO >= longest.est_sec:
                self._pending.remove(same)
                return same
        return self._pending.pop(0)

    def requeue(self, unit: Unit) -> None:
        unit.attempt += 1
        idx = next((i for i, u in enumerate(self._pending) if u.est_sec < unit.est_sec),
                   len(self._pending))
        self._pending.insert(idx, unit)

    def pending(self) -> list[Unit]:
        return list(self._pending)


def simulate(units: Iterable[Unit], lanes: int, *, switch_sec: float = APP_SWITCH_SEC,
             boot_sec: float = 0.0) -> float:
    """Makespan in seconds under the exact rule `WorkQueue.take` applies."""
    lanes = max(1, lanes)
    queue = WorkQueue(Unit(**{**u.__dict__}) for u in units)
    free_at = [boot_sec] * lanes
    staged: list[str | None] = [None] * lanes
    makespan = boot_sec
    while len(queue):
        lane = min(range(lanes), key=free_at.__getitem__)
        unit = queue.take(staged[lane])
        if unit is None:
            break
        if unit.app_id != staged[lane]:
            free_at[lane] += switch_sec
            staged[lane] = unit.app_id
        free_at[lane] += unit.est_sec
        makespan = max(makespan, free_at[lane])
    return makespan


def plan_summary(units: list[Unit], lanes: int) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for u in units:
        by_kind[u.kind] = by_kind.get(u.kind, 0) + 1
        by_source[u.est_source] = by_source.get(u.est_source, 0) + 1
    return {
        "episodes": len(units),
        "by_kind": by_kind,
        "lanes": lanes,
        "device_time_sec": round(sum(u.est_sec for u in units)),
        "eta_sec": round(simulate(units, lanes)),
        "eta_one_lane_sec": round(simulate(units, 1)),
        "basis": by_source,
        "units": [u.as_dict() for u in units],
    }


def fmt_duration(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {rem % 60:02d}s"
    return f"{rem}s"


@dataclass
class RateLimitBackoff:
    """One backoff shared by every lane — the provider limit is per account. A
    rate-limited episode doubles the hold (full jitter, capped); a clean one resets
    it. Sustained limits park lanes one at a time; clean streaks unpark them."""

    lanes: int
    base_sec: float = 30.0
    cap_sec: float = 600.0
    shed_after: int = 2
    unpark_after: int = 5
    max_attempts: int = 4
    clock: Any = time.monotonic
    rng: Any = random.random

    k: int = 0
    resume_at: float = 0.0
    parked: int = 0
    _events_in_window: int = 0
    _clean_streak: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def note_rate_limit(self) -> dict[str, Any]:
        self.k += 1
        self._clean_streak = 0
        raw = min(self.base_sec * 2 ** (self.k - 1), self.cap_sec)
        delay = max(5.0, raw * self.rng())
        self.resume_at = max(self.resume_at, self.clock() + delay)
        self._events_in_window += 1
        parked_now = False
        if self._events_in_window >= self.shed_after and self.parked < self.lanes - 1:
            self.parked += 1
            self._events_in_window = 0
            parked_now = True
        event = {"event": "rate_limited", "k": self.k, "hold_sec": round(delay),
                 "parked_lanes": self.parked, "parked_now": parked_now}
        self.log.append(event)
        return event

    def note_clean(self) -> dict[str, Any] | None:
        self.k = 0
        self._events_in_window = 0
        self._clean_streak += 1
        if self.parked and self._clean_streak >= self.unpark_after:
            self.parked -= 1
            self._clean_streak = 0
            event = {"event": "unparked", "parked_lanes": self.parked}
            self.log.append(event)
            return event
        return None

    def hold_remaining(self) -> float:
        return max(0.0, self.resume_at - self.clock())

    def lane_active(self, lane_index: int) -> bool:
        """Highest-numbered lanes park first."""
        return lane_index < self.lanes - self.parked

    async def wait(self) -> None:
        while (remaining := self.hold_remaining()) > 0:
            await asyncio.sleep(min(remaining, 1.0))


class ScheduleLog:
    """Append-only runs/_runs/<run_id>/schedule.jsonl — every start, finish,
    requeue, hold and park, with the lane and device."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, /, **fields: Any) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               **fields, "event": event}
        try:
            with self.path.open("a") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%d-%H%M%S}-{random.randrange(16**4):04x}"
