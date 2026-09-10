"""Provider credit guard: the usage windows a subscription reports, and the protocol
for stopping a sweep that is out of budget so somebody else can finish it.

Claude Code on subscription (OAuth) auth reports its own rate-limit state on the
``--output-format stream-json`` stdout the adapter already pumps to
``agent/transcript.txt``::

    {"type":"rate_limit_event","rate_limit_info":{
       "status":"allowed|allowed_warning|rejected",
       "rateLimitType":"five_hour|seven_day|seven_day_opus|...",
       "resetsAt":<unix epoch seconds>,
       "unifiedWindows":{"five_hour":{"utilization":0.03,"resetsAt":<epoch>},
                         "seven_day":{"utilization":0.01,"resetsAt":<epoch>}}}}

``utilization`` is a FRACTION (0-1) on that surface while the configured threshold is
a percentage (0-100); the conversion happens in `utilization_pct` and nowhere else.
With ``ANTHROPIC_API_KEY`` auth the stream carries no windows at all, so the guard is
inert by construction — it never fires and never has to be turned off.

The two KINDS of window mean very different things, and conflating them is the
mistake this module exists to prevent:

* **seven-day** is the budget the sweep is spending. Past the configured percentage
  the run stops *on purpose* — cleanly, at a unit boundary — so the checkpoint can be
  handed to someone running on their own account. Nobody waits out a seven-day reset.
  There is more than one of these: a plan can carry model-scoped weekly caps
  (`seven_day_opus`, `seven_day_sonnet`, `seven_day_overage_included`) beside the
  generic `seven_day`. Every name with that prefix is budget, and
  `stop_at_seven_day_pct` is a ceiling on whichever of them reads HIGHEST — a spent
  model cap must stop the sweep while the generic window still reads 6%.
* **five-hour** is a short block. It is never a reason to abandon a sweep: the run
  stops only so the launcher, which owns the emulators, can wait for the reset and
  resume the same run id.

A window this module cannot name at all is read as a short block, because a wait
capped by the launcher is the safe reading of a limit we cannot name. That reading is
only defensible because weekly windows never reach it: waiting out a window that
refills in DAYS is precisely the sleep/resume/stop cycle described above, and the
launcher would run it ten times before giving up.

Everything above `RateLimitWatcher` is adapter-neutral. `stop.json` and exit 75 are the
protocol; an adapter with a quota API of its own can adopt them without touching the
lanes, and only the watcher below knows what a claude-code stream looks like.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .checkpoint import run_meta_dir, write_json as write_json_atomic

logger = logging.getLogger(__name__)

# EX_TEMPFAIL: "stopped, and resumable". The third exit code of the run command —
# 0 finished, 1 broke, 75 ran out of budget with work left — and the one thing the
# launcher loop branches on, so it is a contract, not an implementation detail.
EXIT_STOPPED = 75

# Latest usage snapshot, written per episode and per run.
RATE_LIMIT_FILE = "rate_limit.json"
# Why the run stopped, read by the launcher. See `CreditGuard.write_stop`.
STOP_FILE = "stop.json"
# Dropped in the episode dir the instant a request is rejected — the lane's proof
# that THIS episode was cut off by the provider rather than by the agent finishing.
RATE_LIMITED_SENTINEL = "rate_limited"

# Bump when the shape of rate_limit.json / stop.json changes incompatibly.
SCHEMA_VERSION = 1

STATUS_REJECTED = "rejected"
FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
# Every weekly window shares this prefix: the generic `seven_day` plus the
# model-scoped caps some plans carry — `seven_day_opus`, `seven_day_sonnet` and
# `seven_day_overage_included` are the names the Agent SDK enumerates as
# `rateLimitType` values. Matched by PREFIX rather than against a whitelist on
# purpose: the failure this guards against is a weekly window we do not recognise,
# and a whitelist cannot recognise one by construction. Names outside the prefix
# (`overage`, anything new) stay unknown and are read as short blocks.
WEEKLY_PREFIX = SEVEN_DAY

REASON_SEVEN_DAY = "seven_day_threshold"
REASON_FIVE_HOUR = "five_hour_limit"

# 100 = only when the window is fully spent, i.e. off unless the user asks for it.
DEFAULT_STOP_AT_SEVEN_DAY_PCT = 100

# A rate_limit_event line is a few hundred bytes. A tool result can be megabytes, and
# buffering one to look for a newline is the one way this could cost an episode.
_MAX_LINE_BYTES = 1_000_000


# ── reading the stream ────────────────────────────────────────────────────────


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def snapshot(info: Mapping[str, Any], *, observed_at: datetime | None = None) -> dict[str, Any]:
    """One `rate_limit_info` payload as it is stored on disk.

    Renamed to snake_case and stripped to the fields the guard acts on, so the wire
    shape (which is not ours) can change without every reader changing with it.
    `utilization` stays the 0-1 fraction it arrives as: converting here and again at
    the threshold comparison is exactly how a percentage ends up applied twice.
    """
    windows: dict[str, dict[str, Any]] = {}
    raw = info.get("unifiedWindows")
    if isinstance(raw, dict):
        for name, window in raw.items():
            if not isinstance(window, Mapping):
                continue
            windows[str(name)] = {"utilization": _number(window.get("utilization")),
                                  "resets_at": _epoch(window.get("resetsAt"))}
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "status": str(info.get("status") or ""),
        "rate_limit_type": str(info.get("rateLimitType") or ""),
        "resets_at": _epoch(info.get("resetsAt")),
        "windows": windows,
    }


def parse_event(line: str | bytes) -> dict[str, Any] | None:
    """One stdout line to a usage snapshot, or `None` when it is not a
    `rate_limit_event` — which is almost every line, the stream being mostly assistant
    messages and tool results.

    The substring test comes first on purpose: a `json.loads` per line of a
    multi-megabyte transcript is the one cost this must not add to an episode.
    """
    if isinstance(line, (bytes, bytearray)):
        if b"rate_limit_event" not in line:
            return None
        try:
            line = bytes(line).decode()
        except UnicodeDecodeError:
            return None
    elif "rate_limit_event" not in line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, Mapping):
        return None
    return snapshot(info)


# ── the files ─────────────────────────────────────────────────────────────────


def write_json(path: Path | str, payload: Mapping[str, Any]) -> bool:
    """Write via a temp file and `os.replace`, so a reader mid-run never sees half a
    document. Best-effort: telemetry must not be what kills an episode.

    One implementation for the whole harness, in `checkpoint`, because the durability
    argument is the same wherever state is written and two copies of it would drift.
    Re-exported here so this module's callers read as one file."""
    return write_json_atomic(path, payload)


def read_json(path: Path | str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def rate_limit_path(runs_dir: Path | str, run_id: str) -> Path:
    return run_meta_dir(runs_dir, run_id) / RATE_LIMIT_FILE


def stop_path(runs_dir: Path | str, run_id: str) -> Path:
    return run_meta_dir(runs_dir, run_id) / STOP_FILE


def read_stop(runs_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    """Why this run id stopped, or `None` if it did not. The launcher's read."""
    return read_json(stop_path(runs_dir, run_id))


def record(target_dir: Path | str, snap: Mapping[str, Any], *, sticky: bool) -> bool:
    """Write `snap` as the latest state of `<target_dir>/rate_limit.json`.

    `sticky` is for the run-level copy, where every lane writes independently: a
    rejection on one lane is immediately followed by `allowed` events from another
    that has not hit the wall yet, and plain last-writer-wins would erase the one fact
    the guard exists to act on. The rejection is therefore carried forward as its own
    `rejected` block. It is merged rather than appended because the guard needs the
    current state, not a history — schedule.jsonl is the history.
    """
    path = Path(target_dir) / RATE_LIMIT_FILE
    merged = dict(snap)
    if sticky:
        rejection = (read_json(path) or {}).get("rejected")
        if snap.get("status") == STATUS_REJECTED:
            rejection = {"rate_limit_type": snap.get("rate_limit_type") or "",
                         "resets_at": snap.get("resets_at"),
                         "observed_at": snap.get("observed_at")}
        if isinstance(rejection, dict):
            merged["rejected"] = rejection
    return write_json(path, merged)


# ── the decision ──────────────────────────────────────────────────────────────


def utilization_pct(fraction: Any) -> float | None:
    """The only fraction-to-percentage conversion in the harness. `utilization` is
    0-1 on the wire; `stop_at_seven_day_pct` is 0-100 in the config."""
    value = _number(fraction)
    return None if value is None else value * 100.0


def is_weekly(name: Any) -> bool:
    """Is `name` one of the provider's weekly BUDGET windows?

    True for the generic `seven_day` and for every model-scoped weekly cap
    (`seven_day_opus`, `seven_day_sonnet`, `seven_day_overage_included`, and whatever
    a future plan adds). False for `five_hour`, for `overage`, and for any other name
    — those are short blocks or unknowns, and the launcher may wait them out.
    """
    return isinstance(name, str) and name.startswith(WEEKLY_PREFIX)


def highest_weekly(windows: Any) -> tuple[str, dict[str, Any], float] | None:
    """The weekly window closest to spent: `(name, block, utilization %)`.

    `stop_at_seven_day_pct` is one knob over every weekly window, so the comparison
    is against the MAXIMUM: a model cap at 95% has to stop the sweep while the
    generic window reads 6%, because waiting will not refill either of them and the
    sweep cannot buy another episode of that model. Reading only `seven_day` would
    mean a spent model cap never stops anything.

    Windows with no usable `utilization` are skipped — they cannot cross a threshold,
    and treating a missing number as 0% would be a licence to keep spending. `None`
    when the snapshot carries no weekly window at all.
    """
    ranked = [(str(name), block, pct)
              for name, block in (windows.items() if isinstance(windows, dict) else ())
              if is_weekly(name) and isinstance(block, dict)
              for pct in [utilization_pct(block.get("utilization"))] if pct is not None]
    if not ranked:
        return None
    # Highest first; ties to the generic window, then alphabetical. Two identical
    # snapshots must name the same window, or stop.json and the banner disagree with
    # each other across a resume.
    ranked.sort(key=lambda w: (-w[2], w[0] != SEVEN_DAY, w[0]))
    return ranked[0]


def _fresh(observed_at: Any, since: datetime | None) -> bool:
    """Was this observed during the sitting `since` began?

    A run id can span several sittings, and the launcher resumes one *after waiting
    out a five-hour reset* — so the rejection that stopped the previous segment is
    still sitting in rate_limit.json when the next one starts. Acting on it would stop
    the resume instantly, every time, forever. Only what this sitting saw counts.
    """
    if since is None:
        return True
    if not isinstance(observed_at, str) or not observed_at:
        return False
    try:
        seen = datetime.fromisoformat(observed_at)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen >= since


@dataclass(frozen=True)
class StopDecision:
    """The sweep should stop, and why. `payload` is what lands in stop.json beside
    the reason; `message` is the one line a human reads on the board."""
    reason: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.payload}


def evaluate(snap: Mapping[str, Any] | None, *,
             stop_at_seven_day_pct: int = DEFAULT_STOP_AT_SEVEN_DAY_PCT,
             since: datetime | None = None) -> StopDecision | None:
    """Should the sweep stop, given the last thing the account told us?

    Seven-day outranks five-hour, always. Waiting five hours puts no credit back into
    a spent weekly window, so a run that is both blocked *and* over budget has to
    report the reason that is actually terminal — otherwise the launcher sleeps,
    resumes, stops again, and does it every five hours until someone notices.

    "Seven-day" means every window whose name carries `WEEKLY_PREFIX`, not just the
    generic one, on both halves of the decision: the threshold is compared against
    the highest weekly window, and a rejection naming any weekly window is a
    hand-off. A model-scoped cap is a week from refilling exactly like the generic
    one, so reading it as a short block would buy ten segments of emulator teardown,
    sleep, reboot and re-rejection.
    """
    if not snap:
        return None
    windows = snap.get("windows")
    highest = highest_weekly(windows) if _fresh(snap.get("observed_at"), since) else None
    pct = highest[2] if highest else None

    rejection = snap.get("rejected")
    if not isinstance(rejection, dict) or not _fresh(rejection.get("observed_at"), since):
        rejection = {}
    rejected_type = str(rejection.get("rate_limit_type") or "")
    weekly_rejection = is_weekly(rejected_type)

    if weekly_rejection or (pct is not None and pct >= stop_at_seven_day_pct):
        # A rejection names its own window, and that outranks whichever one merely
        # reads highest: the provider refusing `seven_day_opus` is the harder fact
        # than the generic window sitting a point over the threshold.
        name = rejected_type if weekly_rejection else highest[0]
        block = windows.get(name) if isinstance(windows, dict) else None
        block = block if isinstance(block, dict) else {}
        # A weekly rejection IS that window spent, whatever its block said — and the
        # block may be missing entirely, which is how this account reports one.
        fraction = block.get("utilization")
        if weekly_rejection and _number(fraction) is None:
            fraction = 1.0
        shown = utilization_pct(fraction)
        # The generic window is named "seven-day" as it always was; only a
        # model-scoped cap has to spell out which one tripped, so an operator reading
        # the banner knows it is not the window they have been watching.
        label = "seven-day" if name == SEVEN_DAY else f"seven-day ({name})"
        return StopDecision(
            reason=REASON_SEVEN_DAY,
            message=(f"{label} usage {shown:.1f}% is at or past the "
                     f"{stop_at_seven_day_pct}% stop threshold"
                     if shown is not None else f"the {label} limit is spent"),
            payload={"window": name,
                     "utilization": fraction,
                     "utilization_pct": None if shown is None else round(shown, 3),
                     "threshold_pct": int(stop_at_seven_day_pct),
                     "resets_at": block.get("resets_at") or rejection.get("resets_at"),
                     "rejected": bool(rejected_type)},
        )

    if rejection:
        # A non-weekly window is treated as a short block: the launcher waits it out.
        # An unknown NAME lands here on purpose — a wait capped by the launcher is the
        # safe reading of a limit we cannot name, and the launcher gives up after two
        # such stops rather than spending all ten segments on the guess. Weekly names
        # never reach here; that is the whole point of the branch above.
        resume_after = rejection.get("resets_at")
        return StopDecision(
            reason=REASON_FIVE_HOUR,
            message=(f"{rejected_type or 'provider'} limit reached"
                     + (f", resets {_when(resume_after)}" if resume_after else "")),
            payload={"resume_after": resume_after,
                     "rate_limit_type": rejected_type or FIVE_HOUR},
        )
    return None


def _when(epoch: Any) -> str:
    value = _epoch(epoch)
    if value is None:
        return "at an unknown time"
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class CreditGuard:
    """The between-unit stop decision for one run.

    Owned by the run and consulted by every lane *before it dequeues* — never
    mid-episode, so an episode already in flight is finished and scored rather than
    thrown away. The decision is sticky: the first lane to see it freezes the queue,
    and every other lane returns as soon as it finishes what it holds.

    A guard with no rate_limit.json to read (any adapter but claude-code, or
    claude-code on an API key) never returns a decision, so the lanes need no
    adapter-specific branch.
    """
    runs_dir: Path
    run_id: str
    stop_at_seven_day_pct: int = DEFAULT_STOP_AT_SEVEN_DAY_PCT
    wait_for_five_hour_reset: bool = True
    # Everything older than this is a previous sitting of the same run id; see `_fresh`.
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decision: StopDecision | None = None

    @property
    def path(self) -> Path:
        return rate_limit_path(self.runs_dir, self.run_id)

    def check(self) -> StopDecision | None:
        """The current decision, deciding it once and remembering it."""
        if self.decision is None:
            self.decision = evaluate(read_json(self.path),
                                     stop_at_seven_day_pct=self.stop_at_seven_day_pct,
                                     since=self.since)
        return self.decision

    def write_stop(self, *, done: int | None = None,
                   remaining: int | None = None) -> Path | None:
        """Write `_runs/<run_id>/stop.json` — the contract the launcher loop reads to
        decide whether to wait and resume, or to stop and hand the run over.

        Written only when the guard stopped the run; a run that finished, was
        cancelled or errored leaves no stop file, so "does this exist" is itself the
        signal. Exit code 75 accompanies it.

        Common keys::

            {"schema_version": 1,
             "run_id": "20260909-120000-a1b2",
             "reason": "seven_day_threshold" | "five_hour_limit",
             "stopped_at": "2026-09-09T12:34:56+00:00",   # ISO 8601, UTC
             "wait_for_five_hour_reset": true,            # the config's answer
             "done": 12, "remaining": 30,                 # units, this run id
             "resume": "qualgent-bench run --resume <run_id>"}

        ``resume`` is the bare harness form on purpose: it is the machine-readable
        half of the contract, and the launcher rewrites it into its own
        ``scripts/launch.py <config> --resume <run_id>`` before showing it to anybody.
        The receiving machine wants the launcher form — it has no emulators booted —
        so every human-facing banner leads with that (``cli._resume_lines``,
        ``launch.print_handoff``); this field stays the thing a script can parse.

        ``reason: "seven_day_threshold"`` adds ``window`` (WHICH weekly window
        tripped: ``"seven_day"``, or a model-scoped cap such as
        ``"seven_day_opus"``), ``utilization`` (0-1 fraction, of that window),
        ``utilization_pct``, ``threshold_pct``, ``resets_at`` (unix epoch seconds, may
        be null) and ``rejected`` (true when the provider refused outright rather than
        the configured threshold being crossed). The launcher must NOT wait on this
        reason — every weekly window is days away from resetting. Export the checkpoint
        and hand it over; the receiver finishes it with
        ``scripts/launch.py <config> --resume <run_id>`` after importing the bundle.

        ``window`` was added after the rest; a reader that does not know it still gets
        the same meaning from every other key, which is why it is additive rather than
        a replacement for ``utilization``.

        ``reason: "five_hour_limit"`` adds ``resume_after`` (unix epoch seconds when
        the window resets, may be null) and ``rate_limit_type``. The launcher waits
        until ``resume_after``, then re-runs with ``--resume <run_id>``. A null
        ``resume_after`` is a window nobody can time: the launcher waits the
        first one out blind and hands the next one back rather than guess again.
        """
        if self.decision is None:
            return None
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "reason": self.decision.reason,
            "stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "wait_for_five_hour_reset": bool(self.wait_for_five_hour_reset),
            **self.decision.payload,
        }
        if done is not None:
            body["done"] = int(done)
        if remaining is not None:
            body["remaining"] = int(remaining)
        body["resume"] = f"qualgent-bench run --resume {self.run_id}"
        path = stop_path(self.runs_dir, self.run_id)
        return path if write_json(path, body) else None


class RunStopped(Exception):
    """The credit guard froze the queue and the run has to exit 75.

    Carries the results already collected so the caller can still print and board
    them: a stopped sweep is a partial one, not a failed one.
    """

    def __init__(self, guard: "CreditGuard", results: list | None = None) -> None:
        assert guard.decision is not None, "RunStopped needs a decided guard"
        super().__init__(guard.decision.message)
        self.guard = guard
        self.decision = guard.decision
        self.run_id = guard.run_id
        self.results = list(results or [])


# ── watching one episode's stream (claude-code) ───────────────────────────────


class RateLimitWatcher:
    """Reads usage out of one episode's stdout as the adapter pumps it.

    Fed raw chunks (the pump reads by byte count, not by line, so partial lines are
    normal) and re-assembles them into lines. Every `rate_limit_event` is written to
    the episode's own `rate_limit.json` and merged into the run's, so the lanes have a
    single place to look and the episode keeps its own evidence after the dir is
    discarded.

    A `rejected` status also drops the `rate_limited` sentinel and asks the adapter to
    kill the agent. That is deliberate rather than waiting for the process: nobody has
    observed whether `claude -p` exits or blocks until the window resets, and an agent
    blocked for five hours holds a device the rest of the sweep needs.
    """

    # TODO(QUA-2699): UNVERIFIED against a real five-hour block — every test here
    # feeds synthetic `rate_limit_event` lines. Killing on `rejected` is safe under
    # both readings (exit and block), so this is not a correctness gap; what is
    # unknown is whether print mode emits the event at ALL before it blocks. If it
    # blocks SILENTLY, no rejection is ever parsed and `stop_reason()` stays None —
    # then the watchdog needs to fire on `idle_sec()` alone rather than only after a
    # rejection. Record what actually happens the first time a five-hour reset is
    # observed naturally and update this class plus
    # docs/checkpointing.md § "What a live hand-off still has to prove".

    def __init__(self, episode_dir: Path | str, run_meta_dir: Path | str | None = None, *,
                 clock=time.monotonic, now=None) -> None:
        self.episode_dir = Path(episode_dir)
        self.run_meta_dir = Path(run_meta_dir) if run_meta_dir else None
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._buf = b""
        self._last_chunk = clock()
        self.latest: dict[str, Any] | None = None
        self.rejection: dict[str, Any] | None = None

    # -- fed by the adapter's pump ---------------------------------------------

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._last_chunk = self._clock()
        self._buf += chunk
        if b"\n" not in self._buf:
            if len(self._buf) > _MAX_LINE_BYTES:
                # A line this long is a tool result, never one of ours. Dropping it
                # loses no complete line — there is no newline in it yet — and keeps
                # the buffer from growing with the transcript.
                self._buf = b""
            return
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            self._line(line)

    def _line(self, line: bytes) -> None:
        snap = parse_event(line)
        if snap is None:
            return
        snap["observed_at"] = self._now().isoformat()
        self.latest = snap
        if snap.get("status") == STATUS_REJECTED:
            self.rejection = snap
            write_json(self.episode_dir / RATE_LIMITED_SENTINEL,
                       {"schema_version": SCHEMA_VERSION,
                        "status": STATUS_REJECTED,
                        "rate_limit_type": snap.get("rate_limit_type") or "",
                        "resets_at": snap.get("resets_at"),
                        "observed_at": snap.get("observed_at")})
        record(self.episode_dir, snap, sticky=False)
        if self.run_meta_dir is not None:
            record(self.run_meta_dir, snap, sticky=True)

    # -- read by the adapter's watchdog ----------------------------------------

    def stop_reason(self) -> str | None:
        """Non-None once the provider has refused a request: kill the agent now."""
        if self.rejection is None:
            return None
        return (f"{self.rejection.get('rate_limit_type') or 'provider'} limit rejected "
                f"the request")

    def idle_sec(self) -> float:
        """Seconds since the last byte of output. Only consulted after a rejection,
        where a silent stream means print mode is blocking on the reset."""
        return max(0.0, self._clock() - self._last_chunk)
