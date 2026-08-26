"""What a run looks like while it runs.

On a terminal: one live table with a row per lane (device, episode, steps, elapsed)
and a progress footer; finished episodes print above it as they land. Piped —
`docker logs`, CI, a file — there is no cursor to redraw, so every event is one
timestamped line and busy lanes print a heartbeat once a minute. Both carry the
same information; only the rendering differs.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .scheduler import Unit, fmt_duration

HEARTBEAT_SEC = 60.0
# An agent whose step counter has not moved for this long gets flagged in the
# steps cell — a hung episode must look different from a thinking one.
STALL_WARN_SEC = 180.0
# Where each adapter's budget hook keeps its counter (codex nests it under its
# isolated CODEX_HOME).
_COUNT_FILES = ("hooks/count", "codex_home/hooks/count")


def plain_output_requested(console: Console, flag: bool | None = None) -> bool:
    if flag is not None:
        return flag
    if os.environ.get("QGB_PLAIN_OUTPUT"):
        return True
    return not console.is_terminal


@dataclass
class LaneState:
    index: int
    device: str
    unit: Unit | None = None
    run_dir: Path | None = None
    budget: int | None = None
    started: float = 0.0
    phase: str = "idle"           # idle | staging | agent | verifying | parked | retired
    detail: str = ""
    last_heartbeat: float = 0.0
    count_file: Path | None = None
    last_steps: int | None = None
    steps_changed_at: float = 0.0

    @property
    def tag(self) -> str:
        return f"L{self.index + 1} {self.device}"

    def steps(self) -> int | None:
        if self.count_file is None or not self.count_file.exists():
            if self.run_dir is None:
                return None
            for rel in _COUNT_FILES:
                candidate = self.run_dir / rel
                if candidate.exists():
                    self.count_file = candidate
                    break
            else:
                return None
        try:
            n = int(self.count_file.read_text().strip())
        except (OSError, ValueError):
            return None
        if n != self.last_steps:
            self.last_steps, self.steps_changed_at = n, time.monotonic()
        return n

    def stalled_sec(self) -> float:
        """Seconds since the step counter last moved (0 until it moved at all)."""
        if self.last_steps is None or not self.steps_changed_at:
            return 0.0
        return time.monotonic() - self.steps_changed_at

    def elapsed(self) -> float:
        return time.monotonic() - self.started if self.started else 0.0


def describe(unit: Unit, trials: int | None = None) -> str:
    trial = f"trial {unit.trial}" + (f"/{trials}" if trials else "")
    return f"{unit.app_id} · {unit.label} · {trial}"


class LaneBoard:
    """Progress display for N lanes. Every method is safe to call from the event
    loop; `finish` and `note` may also be called from a worker thread (rich's
    console lock covers the print)."""

    def __init__(self, console: Console, devices: list[str], total: int, *,
                 trials: int, remaining_sec: Callable[[], float] | None = None,
                 plain: bool | None = None) -> None:
        self.console = console
        self.lanes = [LaneState(i, d) for i, d in enumerate(devices)]
        self.total = total
        self.trials = trials
        self.done = 0
        self.failed = 0
        self.plain = plain_output_requested(console, plain)
        self._remaining_sec = remaining_sec
        self._started = time.monotonic()
        self._live: Live | None = None
        self._ticker: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "LaneBoard":
        self._started = time.monotonic()
        if not self.plain:
            self._live = Live(self._table(), console=self.console, refresh_per_second=4,
                              transient=True)
            self._live.__enter__()
        self._ticker = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ticker:
            self._ticker.cancel()
            with suppress(asyncio.CancelledError):
                await self._ticker
        if self._live is not None:
            self._live.__exit__(*exc)
            self._live = None

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self._live is not None:
                self._live.update(self._table())
            elif self.plain:
                now = time.monotonic()
                for lane in self.lanes:
                    if lane.phase in ("agent", "verifying") and \
                            now - lane.last_heartbeat >= HEARTBEAT_SEC:
                        lane.last_heartbeat = now
                        self._line(f"⋯ {lane.tag} · {self._lane_status(lane)}")

    # ── events from the runner ───────────────────────────────────────────────

    def staging(self, lane_index: int, app_id: str) -> None:
        lane = self.lanes[lane_index]
        lane.phase, lane.detail = "staging", f"installing {app_id}"
        lane.started = time.monotonic()
        self._line(f"⚙ {lane.tag} · staging {app_id}")

    def start(self, lane_index: int, unit: Unit, budget: int | None) -> None:
        lane = self.lanes[lane_index]
        lane.unit, lane.budget = unit, budget
        lane.run_dir = lane.count_file = None
        lane.last_steps, lane.steps_changed_at = None, 0.0
        lane.started = lane.last_heartbeat = time.monotonic()
        lane.phase, lane.detail = "agent", ""
        attempt = f" · attempt {unit.attempt}" if unit.attempt > 1 else ""
        self._line(f"▶ {lane.tag} · {describe(unit, self.trials)} · "
                   f"est ~{fmt_duration(unit.est_sec)}{attempt}")

    def set_run_dir(self, lane_index: int, run_dir: Path) -> None:
        self.lanes[lane_index].run_dir = run_dir

    def verifying(self, lane_index: int, detail: str) -> None:
        lane = self.lanes[lane_index]
        if lane.phase != "verifying" and lane.unit is not None:
            self._line(f"⚙ {lane.tag} · verifying {describe(lane.unit, self.trials)}")
        lane.phase, lane.detail = "verifying", detail

    def finish(self, lane_index: int, unit: Unit, summary: str, *, ok: bool = True,
               extra_lines: list[str] | None = None) -> None:
        lane = self.lanes[lane_index]
        took = fmt_duration(lane.elapsed())
        if ok:
            self.done += 1
        else:
            self.failed += 1
        icon = "✔" if ok else "✖"
        self._line(f"{icon} {lane.tag} · {describe(unit, self.trials)} · {summary} · {took}"
                   f"   [{self._progress()}]")
        for extra in extra_lines or ():
            self._line(f"      {extra}")
        lane.unit, lane.run_dir, lane.count_file = None, None, None
        lane.phase, lane.detail, lane.started = "idle", "", 0.0

    def parked(self, lane_index: int, parked: bool) -> None:
        lane = self.lanes[lane_index]
        lane.phase = "parked" if parked else "idle"

    def retired(self, lane_index: int, why: str) -> None:
        lane = self.lanes[lane_index]
        lane.phase, lane.detail = "retired", why
        self._line(f"✖ {lane.tag} · lane retired — {why}")

    def note(self, text: str) -> None:
        self._line(f"· {text}")

    # ── rendering ────────────────────────────────────────────────────────────

    def _progress(self) -> str:
        elapsed = fmt_duration(time.monotonic() - self._started)
        parts = [f"{self.done}/{self.total} done"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        parts.append(f"elapsed {elapsed}")
        if self._remaining_sec is not None:
            try:
                parts.append(f"~{fmt_duration(self._remaining_sec())} left")
            except Exception:  # noqa: BLE001 - display only
                pass
        return " · ".join(parts)

    def _lane_status(self, lane: LaneState) -> str:
        if lane.unit is None:
            return lane.phase if lane.phase != "idle" else "idle"
        clock = fmt_duration(lane.elapsed())
        if lane.phase == "verifying":
            return f"{describe(lane.unit, self.trials)} · verifying {lane.detail} · {clock}"
        steps = lane.steps()
        if steps is None:
            return f"{describe(lane.unit, self.trials)} · starting… · {clock}"
        of = f"/{lane.budget}" if lane.budget else ""
        stall = (f" · no steps for {fmt_duration(lane.stalled_sec())}"
                 if lane.stalled_sec() >= STALL_WARN_SEC else "")
        return f"{describe(lane.unit, self.trials)} · {steps}{of} steps{stall} · {clock}"

    def _line(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        # Piped output has no real width; a hard wrap at 80 columns would split
        # one event over two log lines.
        self.console.print(f"[dim]{stamp}[/]  {text}", highlight=False, soft_wrap=True)

    def _table(self) -> Table:
        table = Table(expand=False, show_edge=False, pad_edge=False, box=None,
                      title=self._progress(), title_justify="left", title_style="dim")
        table.add_column("lane", style="bold")
        table.add_column("device", style="dim")
        table.add_column("phase")
        table.add_column("episode")
        table.add_column("steps", justify="right")
        table.add_column("elapsed", justify="right")
        for lane in self.lanes:
            if lane.unit is None:
                table.add_row(f"L{lane.index + 1}", lane.device, f"[dim]{lane.phase}[/]",
                              f"[dim]{lane.detail}[/]" if lane.detail else "", "", "")
                continue
            steps = lane.steps()
            of = f"/{lane.budget}" if lane.budget else ""
            steps_cell = "" if steps is None else f"{steps}{of}"
            if lane.phase == "agent" and lane.stalled_sec() >= STALL_WARN_SEC:
                steps_cell += f" [yellow]no steps {fmt_duration(lane.stalled_sec())}[/]"
            state = describe(lane.unit, self.trials)
            if lane.phase == "verifying" and lane.detail:
                state += f" · [dim]{lane.detail}[/]"
            table.add_row(f"L{lane.index + 1}", lane.device, f"[dim]{lane.phase}[/]",
                          state, steps_cell, fmt_duration(lane.elapsed()))
        return table


def summarize_result(task_type: str, metrics: dict[str, Any]) -> str:
    """One-line episode summary for the board — hunt shows the verified numbers
    once they exist (metrics['hybrid']), guided shows its own axes."""
    m = metrics
    if task_type == "bug_hunt":
        h = m.get("hybrid") or {}
        f1 = h.get("f1", m.get("f1"))
        fp = h.get("fp_rate")
        overall = h.get("overall", m.get("overall"))
        steps = h.get("steps", m.get("hook_steps", m.get("steps")))
        budget = m.get("step_budget")
        parts = [f"bugs {m.get('bugs_found')}/{m.get('bugs_total')}"]
        if f1 is not None:
            parts.append(f"F1 {float(f1):.2f}")
        if fp is not None:
            parts.append(f"FP {float(fp):.0%}")
        elif m.get("false_positives") is not None:
            parts.append(f"FP~{m.get('false_positives')}")
        parts.append(f"steps {steps}" + (f"/{budget}" if budget else ""))
        if overall is not None:
            parts.append(f"Overall [bold]{float(overall):.1%}[/]")
        if m.get("truncated"):
            parts.append("[yellow]truncated[/]")
        return " · ".join(parts)
    if task_type == "clean_task":
        ok = "[green]PASS[/]" if m.get("oracle_passed") else "[red]FAIL[/]"
        return f"clean {ok} · reward {m.get('reward')} · calls {m.get('device_tool_calls')}"
    found = "[green]found[/]" if m.get("bug_found") else "[red]missed[/]"
    return (f"bug {found} · status {m.get('reported_status')} · reward {m.get('reward')}"
            f" · calls {m.get('device_tool_calls')}")
