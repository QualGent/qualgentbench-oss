"""The Claude Code credit guard, driven by SYNTHETIC stream-json lines.

Nothing here exhausts a real rate limit — the events are hand-built from the shape
verified on Claude Code 2.1.263 — and nothing here touches a device. What is being
pinned down is the whole chain: a `rate_limit_event` on the agent's stdout becomes a
snapshot on disk, becomes a stop decision between units, becomes `stop.json` and
exit 75.

The one conversion worth a test of its own: `utilization` is a 0-1 FRACTION on the
wire while the configured threshold is a 0-100 percentage.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from qualgentbench import credit, lanes as L
from qualgentbench.adapters.base import AgentAdapter, RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.config import BenchConfig, ConfigError, load_config
from qualgentbench.result import RunResult, VerifierResult
from qualgentbench.schemas import Condition
from qualgentbench.scheduler import ScheduleLog, Unit, plan_summary

FIVE_HOUR_RESET = 1_757_430_000
SEVEN_DAY_RESET = 1_757_980_000
# A model-scoped weekly cap resets on its own clock, so the tests can tell which
# window a decision actually read.
MODEL_WEEK_RESET = 1_758_100_000


def event(*, status: str = "allowed", kind: str = "five_hour",
          five: float | None = 0.03, seven: float | None = 0.01,
          extra: dict[str, float | None] | None = None) -> str:
    """One `rate_limit_event` line exactly as the CLI emits it (camelCase, fractions).

    `extra` adds windows this account does not emit — the model-scoped weekly caps
    (`seven_day_opus`, `seven_day_sonnet`, `seven_day_overage_included`) other plans
    do, which is the whole subject of QUA-2705. Nothing here was observed live; the
    names come from the Agent SDK's own `rateLimitType` enumeration.
    """
    windows = {}
    if five is not None:
        windows["five_hour"] = {"utilization": five, "resetsAt": FIVE_HOUR_RESET}
    if seven is not None:
        windows["seven_day"] = {"utilization": seven, "resetsAt": SEVEN_DAY_RESET}
    for name, used in (extra or {}).items():
        windows[name] = {"utilization": used, "resetsAt": MODEL_WEEK_RESET}
    return json.dumps({
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "rateLimitType": kind,
            "resetsAt": FIVE_HOUR_RESET if kind == "five_hour" else SEVEN_DAY_RESET,
            "unifiedWindows": windows,
        },
    }) + "\n"


# ── parsing the stream ────────────────────────────────────────────────────────


def test_a_rate_limit_event_becomes_a_snapshot_with_the_fraction_intact():
    snap = credit.parse_event(event(status="allowed_warning", seven=0.42))
    assert snap["status"] == "allowed_warning"
    assert snap["rate_limit_type"] == "five_hour"
    # The fraction is stored as it arrived; only the comparison converts.
    assert snap["windows"]["seven_day"] == {"utilization": 0.42, "resets_at": SEVEN_DAY_RESET}
    used = snap["windows"]["seven_day"]["utilization"]
    assert credit.utilization_pct(used) == pytest.approx(42.0)


@pytest.mark.parametrize("line", [
    '{"type":"assistant","message":{"content":"talking about rate_limit_event"}}',
    '{"type":"rate_limit_event"}',                      # no rate_limit_info
    '{"type":"rate_limit_event","rate_limit_info":7}',  # wrong shape
    '{"type":"rate_limit_event", "rate_limit_i',        # truncated json
    '{"type":"result","subtype":"success"}',
    "",
])
def test_lines_that_are_not_usable_events_are_ignored(line):
    assert credit.parse_event(line) is None


def test_a_window_the_provider_did_not_send_is_absent_not_zero():
    """A missing window must not read as 0% used — that is a licence to keep spending."""
    snap = credit.parse_event(event(seven=None))
    assert "seven_day" not in snap["windows"]
    assert credit.evaluate(snap, stop_at_seven_day_pct=0) is None


def test_the_watcher_reassembles_events_split_across_read_chunks(tmp_path):
    """The pump reads by byte count, so an event routinely arrives in two pieces."""
    watcher = credit.RateLimitWatcher(tmp_path)
    line = event(seven=0.5).encode()
    watcher.feed(b'{"type":"assistant"}\n' + line[:30])
    assert watcher.latest is None                 # half a line decides nothing
    watcher.feed(line[30:])

    on_disk = json.loads((tmp_path / credit.RATE_LIMIT_FILE).read_text())
    assert on_disk["windows"]["seven_day"]["utilization"] == 0.5


def test_an_oversized_line_is_dropped_without_losing_the_next_event(tmp_path):
    """A multi-megabyte tool result must not be buffered looking for a newline."""
    watcher = credit.RateLimitWatcher(tmp_path)
    watcher.feed(b"x" * (credit._MAX_LINE_BYTES + 1))
    watcher.feed(b"tail-of-the-huge-line\n" + event(seven=0.9).encode())
    assert watcher.latest["windows"]["seven_day"]["utilization"] == 0.9


def test_a_rejection_writes_the_sentinel_the_lane_reads(tmp_path):
    watcher = credit.RateLimitWatcher(tmp_path)
    watcher.feed(event(status="rejected", kind="five_hour").encode())

    sentinel = json.loads((tmp_path / credit.RATE_LIMITED_SENTINEL).read_text())
    assert sentinel["rate_limit_type"] == "five_hour"
    assert sentinel["resets_at"] == FIVE_HOUR_RESET
    assert watcher.stop_reason() and "five_hour" in watcher.stop_reason()


def test_a_later_allowed_event_from_another_lane_cannot_erase_a_rejection(tmp_path):
    """Every lane writes the run-level file. Plain last-writer-wins would drop the one
    fact the guard exists to act on, because the lanes that have not hit the wall yet
    keep reporting `allowed` for minutes afterwards."""
    run_meta = tmp_path / "_runs" / "r1"
    lane_a = credit.RateLimitWatcher(tmp_path / "ep-a", run_meta)
    lane_b = credit.RateLimitWatcher(tmp_path / "ep-b", run_meta)
    (tmp_path / "ep-a").mkdir()
    (tmp_path / "ep-b").mkdir()

    lane_a.feed(event(status="rejected", kind="five_hour").encode())
    lane_b.feed(event(status="allowed", five=0.9).encode())

    shared = json.loads((run_meta / credit.RATE_LIMIT_FILE).read_text())
    assert shared["status"] == "allowed"                     # latest is still latest
    assert shared["rejected"]["rate_limit_type"] == "five_hour"
    # ...and the episode's own copy is untouched by the other lane.
    assert json.loads((tmp_path / "ep-a" / credit.RATE_LIMIT_FILE).read_text())[
        "status"] == "rejected"


# ── the decision ──────────────────────────────────────────────────────────────


def _snapshot(**kw) -> dict:
    snap = credit.parse_event(event(**kw))
    if kw.get("status") == "rejected":
        snap["rejected"] = {"rate_limit_type": snap["rate_limit_type"],
                            "resets_at": snap["resets_at"],
                            "observed_at": snap["observed_at"]}
    return snap


def test_seven_day_at_the_threshold_stops_the_run():
    stop = credit.evaluate(_snapshot(seven=0.42), stop_at_seven_day_pct=40)
    assert stop.reason == credit.REASON_SEVEN_DAY
    assert stop.payload["utilization"] == 0.42
    assert stop.payload["utilization_pct"] == pytest.approx(42.0)
    assert stop.payload["resets_at"] == SEVEN_DAY_RESET


def test_seven_day_below_the_threshold_does_not():
    assert credit.evaluate(_snapshot(seven=0.39), stop_at_seven_day_pct=40) is None
    # And the default threshold is "only when it is spent".
    assert credit.evaluate(_snapshot(seven=0.99)) is None
    assert credit.evaluate(_snapshot(seven=1.0)) is not None


def test_a_five_hour_rejection_stops_with_a_resume_time():
    stop = credit.evaluate(_snapshot(status="rejected", kind="five_hour"))
    assert stop.reason == credit.REASON_FIVE_HOUR
    assert stop.payload["resume_after"] == FIVE_HOUR_RESET


def test_seven_day_outranks_a_five_hour_block():
    """Waiting five hours puts nothing back in a spent seven-day window. Reporting the
    five-hour reason would have the launcher sleep, resume and stop again forever."""
    snap = _snapshot(status="rejected", kind="five_hour", seven=0.95)
    assert credit.evaluate(snap, stop_at_seven_day_pct=90).reason == credit.REASON_SEVEN_DAY


def test_a_seven_day_rejection_is_a_seven_day_stop_not_a_wait():
    stop = credit.evaluate(_snapshot(status="rejected", kind="seven_day", seven=None))
    assert stop.reason == credit.REASON_SEVEN_DAY
    assert stop.payload["utilization"] == 1.0 and stop.payload["rejected"] is True


# ── weekly windows are a FAMILY, not one window (QUA-2705) ────────────────────


def test_a_spent_model_cap_stops_the_sweep_while_the_generic_window_reads_low():
    """The headline. The threshold is a ceiling on whichever weekly window is
    HIGHEST: reading only `seven_day` means a plan whose Opus week is gone keeps
    dequeuing units against a cap that cannot serve them, and waiting refills
    neither."""
    snap = _snapshot(seven=0.06, extra={"seven_day_opus": 0.95})
    stop = credit.evaluate(snap, stop_at_seven_day_pct=90)

    assert stop.reason == credit.REASON_SEVEN_DAY
    # Named, measured and timed against the cap that tripped — not against the
    # generic window somebody has been watching sit at 6%.
    assert stop.payload["window"] == "seven_day_opus"
    assert stop.payload["utilization"] == 0.95
    assert stop.payload["utilization_pct"] == pytest.approx(95.0)
    assert stop.payload["resets_at"] == MODEL_WEEK_RESET
    assert "seven_day_opus" in stop.message and "95.0%" in stop.message


def test_a_model_scoped_weekly_rejection_is_a_hand_off_not_a_five_hour_wait():
    """Typed as a short block, this rejection has the launcher tear down the
    emulators, sleep, reboot, resume and be refused again — ten times — against a
    window that refills in days. That cycle is what credit.py exists to prevent."""
    stop = credit.evaluate(_snapshot(status="rejected", kind="seven_day_opus",
                                     seven=0.06))

    assert stop.reason == credit.REASON_SEVEN_DAY
    assert stop.payload["window"] == "seven_day_opus"
    # A weekly rejection IS that window spent, whatever the blocks said, and it
    # carries nothing the launcher could read as "come back at".
    assert stop.payload["utilization"] == 1.0 and stop.payload["rejected"] is True
    assert "resume_after" not in stop.payload


@pytest.mark.parametrize("kind", ["seven_day", "seven_day_opus", "seven_day_sonnet",
                                  "seven_day_overage_included",
                                  "seven_day_a_model_that_does_not_exist_yet"])
def test_every_name_with_the_weekly_prefix_is_budget(kind):
    """Matched by prefix rather than against a list of known names: the failure being
    guarded against is a weekly window nobody has heard of, and a whitelist cannot
    recognise one by construction."""
    assert credit.is_weekly(kind)
    stop = credit.evaluate(_snapshot(status="rejected", kind=kind, seven=None))
    assert stop.reason == credit.REASON_SEVEN_DAY


@pytest.mark.parametrize("kind", ["five_hour", "overage", "some_new_thing"])
def test_a_name_outside_the_weekly_family_is_still_read_as_a_short_block(kind):
    """The fallback is kept, narrowed: a wait the launcher caps is the safe reading of
    a limit we cannot name, and only weekly names were unsafe to read that way."""
    assert not credit.is_weekly(kind)
    stop = credit.evaluate(_snapshot(status="rejected", kind=kind, seven=0.01))
    assert stop.reason == credit.REASON_FIVE_HOUR
    assert stop.payload["rate_limit_type"] == kind


def test_the_generic_window_decides_and_reads_exactly_as_it_always_did():
    """The regression the acceptance run is evidence for. One weekly window present,
    same decision, same numbers, same sentence — plus the name of the window it was."""
    stop = credit.evaluate(_snapshot(seven=0.42), stop_at_seven_day_pct=40)

    assert stop.message == ("seven-day usage 42.0% is at or past the "
                            "40% stop threshold")
    assert stop.payload["window"] == "seven_day"
    assert stop.payload["utilization"] == 0.42
    assert stop.payload["resets_at"] == SEVEN_DAY_RESET


def test_two_weekly_windows_at_the_same_utilization_always_name_the_same_one():
    """`window` is written to stop.json and printed in a banner, so an arbitrary tie
    break would have two identical snapshots disagree across a resume. Ties go to the
    generic window."""
    tied = _snapshot(seven=0.95, extra={"seven_day_opus": 0.95})
    assert credit.evaluate(tied, stop_at_seven_day_pct=90).payload["window"] == "seven_day"
    assert credit.highest_weekly(tied["windows"])[0] == "seven_day"


def test_a_weekly_window_with_no_number_cannot_trip_the_threshold():
    """A missing utilization is not 0% and not 100%: it decides nothing, and the
    window that does have a number is the one that is read."""
    snap = _snapshot(seven=0.06, extra={"seven_day_opus": None})
    assert credit.evaluate(snap, stop_at_seven_day_pct=90) is None
    assert credit.evaluate(snap, stop_at_seven_day_pct=5).payload["window"] == "seven_day"


def test_a_rejection_from_a_previous_sitting_cannot_stop_a_resume(tmp_path):
    """The launcher resumes AFTER waiting out the five-hour reset, so the rejection
    that ended the last segment is still in rate_limit.json. Acting on it would stop
    every resume instantly, forever."""
    run_meta = tmp_path / "_runs" / "r1"
    watcher = credit.RateLimitWatcher(tmp_path / "ep", run_meta)
    (tmp_path / "ep").mkdir()
    watcher.feed(event(status="rejected").encode())

    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", since=later)
    assert guard.check() is None
    # A guard that began before the event still sees it.
    assert credit.CreditGuard(runs_dir=tmp_path, run_id="r1",
                              since=later - timedelta(hours=1)).check() is not None


def test_no_events_at_all_never_fires_the_guard(tmp_path):
    """An ANTHROPIC_API_KEY run emits no rate_limit_event, so there is no file to read
    and the guard is inert rather than off."""
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=0)
    assert guard.check() is None
    assert credit.evaluate(None, stop_at_seven_day_pct=0) is None


# ── stop.json: the launcher's contract ────────────────────────────────────────


def test_stop_json_for_a_seven_day_stop_says_hand_it_over(tmp_path):
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=40)
    credit.record(tmp_path / "_runs" / "r1", _snapshot(seven=0.42), sticky=True)
    assert guard.check().reason == credit.REASON_SEVEN_DAY

    guard.write_stop(done=3, remaining=7)
    body = credit.read_stop(tmp_path, "r1")
    assert body["reason"] == "seven_day_threshold"
    assert body["utilization"] == 0.42 and body["resets_at"] == SEVEN_DAY_RESET
    assert body["threshold_pct"] == 40 and (body["done"], body["remaining"]) == (3, 7)
    assert body["run_id"] == "r1" and body["resume"].endswith("--resume r1")
    assert datetime.fromisoformat(body["stopped_at"]).tzinfo is not None


def test_stop_json_gains_window_and_keeps_every_key_the_launcher_reads(tmp_path):
    """`stop.json` is a wire format — `scripts/launch.py` parses it, a docstring on
    `CreditGuard.write_stop` pins it, and the QUA-2701 acceptance evidence quotes a
    real one. `window` is ADDITIVE: this is the exact key set that run produced, and
    every one of them still has to be there meaning what it meant.
    """
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=6)
    credit.record(tmp_path / "_runs" / "r1", _snapshot(seven=0.07), sticky=True)
    assert guard.check().reason == credit.REASON_SEVEN_DAY

    guard.write_stop(done=2, remaining=4)
    body = credit.read_stop(tmp_path, "r1")

    assert set(body) == {"schema_version", "run_id", "reason", "stopped_at",
                         "wait_for_five_hour_reset", "utilization", "utilization_pct",
                         "threshold_pct", "resets_at", "rejected", "done", "remaining",
                         "resume", "window"}
    assert body["window"] == "seven_day"
    assert (body["utilization"], body["utilization_pct"]) == (0.07, pytest.approx(7.0))
    assert (body["threshold_pct"], body["rejected"]) == (6, False)
    assert (body["done"], body["remaining"]) == (2, 4)


def test_stop_json_names_the_model_cap_that_stopped_the_run(tmp_path):
    """The launcher prints this name, so somebody watching a generic window sit at 6%
    can see why the sweep handed itself over."""
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=90)
    credit.record(tmp_path / "_runs" / "r1",
                  _snapshot(seven=0.06, extra={"seven_day_opus": 0.95}), sticky=True)
    assert guard.check().reason == credit.REASON_SEVEN_DAY

    guard.write_stop(done=1, remaining=9)
    body = credit.read_stop(tmp_path, "r1")
    assert body["window"] == "seven_day_opus"
    assert body["utilization"] == 0.95 and body["resets_at"] == MODEL_WEEK_RESET
    # Still a hand-off, so the launcher has nothing to wait on.
    assert "resume_after" not in body


def test_stop_json_for_a_five_hour_stop_says_when_to_come_back(tmp_path):
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1",
                               wait_for_five_hour_reset=False)
    credit.record(tmp_path / "_runs" / "r1", _snapshot(status="rejected"), sticky=True)
    assert guard.check().reason == credit.REASON_FIVE_HOUR

    guard.write_stop()
    body = credit.read_stop(tmp_path, "r1")
    assert body["reason"] == "five_hour_limit"
    assert body["resume_after"] == FIVE_HOUR_RESET
    # The harness stops either way; whether to WAIT is the launcher's call, and this
    # is where it reads the answer.
    assert body["wait_for_five_hour_reset"] is False


def test_no_stop_file_when_the_run_was_not_stopped(tmp_path):
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1")
    assert guard.write_stop() is None
    assert credit.read_stop(tmp_path, "r1") is None


# ── config ────────────────────────────────────────────────────────────────────

_CONFIG = {"agent": "claude-code", "model": "claude-opus-4-8",
           "scope": {"apps": ["birday"], "mode": "hunt"},
           "devices": {"avds": ["Pixel_8_A"]}}


def test_the_threshold_is_a_percentage_and_150_is_refused(tmp_path):
    import yaml

    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump({**_CONFIG, "checkpoint": {"stop_at_seven_day_pct": 150}}))
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert any("checkpoint.stop_at_seven_day_pct" in p for p in exc.value.problems)

    for bad in (-1, "forty"):
        with pytest.raises(Exception):
            BenchConfig.model_validate({**_CONFIG, "checkpoint": {"stop_at_seven_day_pct": bad}})


def test_zero_is_refused_because_it_reads_as_off_and_means_stop_now(tmp_path):
    """`stop_at_seven_day_pct: 0` is the one value whose plain reading is the opposite
    of its behaviour: a user types 0 for "off" and gets "stop once the window is 0%
    used", which stops a completely healthy sweep before its first episode. 100 is
    how off is spelled, so 0 has no meaning worth keeping."""
    import yaml

    path = tmp_path / "bench.yaml"
    path.write_text(yaml.safe_dump({**_CONFIG, "checkpoint": {"stop_at_seven_day_pct": 0}}))
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    problems = "\n".join(exc.value.problems)
    assert "0 does not mean 'off'" in problems, problems
    assert "100" in problems, "the message has to name the value that does mean off"


def test_the_flag_refuses_zero_too():
    """The flag and the env var override the file, so refusing it in only one place
    leaves the trap open on the path the launcher loop actually uses."""
    from click.testing import CliRunner

    from qualgentbench.cli import main

    out = CliRunner().invoke(main, ["run", "--stop-at-seven-day-pct", "0",
                                    "--app", "birday"])
    assert out.exit_code == 2
    assert "stop-at-seven-day-pct" in out.output and "1<=x<=100" in out.output


def test_the_defaults_are_off_and_the_keys_are_closed():
    cfg = BenchConfig.model_validate(_CONFIG)
    assert cfg.checkpoint.stop_at_seven_day_pct == credit.DEFAULT_STOP_AT_SEVEN_DAY_PCT
    assert cfg.checkpoint.wait_for_five_hour_reset is True
    with pytest.raises(Exception):
        BenchConfig.model_validate({**_CONFIG, "checkpoint": {"stop_at_7d": 40}})


# ── the adapter: watching, and killing ────────────────────────────────────────


class _ScriptAgent(AgentAdapter):
    """Runs a literal Python script as the 'agent', watched like claude-code is."""
    name = "fake"

    def __init__(self, script: str) -> None:
        self._script = script

    def command(self, instruction, context):
        return [sys.executable, "-u", "-c", self._script]

    def env(self, context):
        return {}

    def stream_watcher(self, context):
        return credit.RateLimitWatcher(context.run_dir, context.run_meta_dir)


def _context(tmp_path: Path, timeout_sec: float) -> RunContext:
    run_dir, workspace = tmp_path / "run", tmp_path / "ws"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=timeout_sec)),
        agent="fake", model="m", condition=Condition.no_routines, trial=1,
        run_dir=run_dir, mcp_server="", mcp_config_path=tmp_path / "mcp.json",
        workspace_dir=workspace, run_meta_dir=tmp_path / "_runs" / "r1",
        disabled_tools=[], inject_mcp=False,
    )


def test_a_rejection_kills_an_agent_that_would_otherwise_block(tmp_path):
    """Nobody has observed whether `claude -p` exits or blocks when a request is
    rejected. This one blocks; the episode must still end in seconds, not at the
    wall clock, or a five-hour limit holds a device for five hours."""
    agent = _ScriptAgent(
        "import sys, time\n"
        f"sys.stdout.write({event(status='rejected', kind='five_hour')!r})\n"
        "sys.stdout.flush()\n"
        "time.sleep(120)\n"
    )
    ctx = _context(tmp_path, timeout_sec=120.0)

    started = asyncio.get_event_loop_policy().new_event_loop()
    try:
        import time as _time
        t0 = _time.monotonic()
        transcript, _ = started.run_until_complete(agent.run("go", ctx))
        elapsed = _time.monotonic() - t0
    finally:
        started.close()

    assert elapsed < 60           # killed, not waited out (SIGTERM grace is 10s)
    assert "rate_limit_event" in transcript
    assert (ctx.run_dir / credit.RATE_LIMITED_SENTINEL).exists()
    # Both copies of the usage snapshot are on disk.
    assert (ctx.run_dir / credit.RATE_LIMIT_FILE).exists()
    assert (ctx.run_meta_dir / credit.RATE_LIMIT_FILE).exists()


def test_an_allowed_event_is_recorded_without_touching_the_agent(tmp_path):
    agent = _ScriptAgent(
        "import sys\n"
        f"sys.stdout.write({event(seven=0.42)!r})\n"
        "sys.stdout.write('done\\n')\n"
    )
    ctx = _context(tmp_path, timeout_sec=30.0)
    transcript, code = asyncio.run(agent.run("go", ctx))

    assert code == 0 and "done" in transcript
    assert not (ctx.run_dir / credit.RATE_LIMITED_SENTINEL).exists()
    shared = json.loads((ctx.run_meta_dir / credit.RATE_LIMIT_FILE).read_text())
    assert shared["windows"]["seven_day"]["utilization"] == 0.42


def test_an_adapter_with_no_watcher_is_the_default(tmp_path):
    class _Bare(_ScriptAgent):
        stream_watcher = AgentAdapter.stream_watcher

    ctx = _context(tmp_path, timeout_sec=30.0)
    transcript, code = asyncio.run(_Bare("print('hi')").run("go", ctx))
    assert code == 0 and "hi" in transcript
    assert not (ctx.run_dir / credit.RATE_LIMIT_FILE).exists()


def test_claude_code_only_watches_when_the_windows_can_exist(tmp_path, monkeypatch):
    ctx = _context(tmp_path, timeout_sec=30.0)
    adapter = ClaudeCodeAdapter()

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert isinstance(adapter.stream_watcher(ctx), credit.RateLimitWatcher)
    assert ClaudeCodeAdapter.credit_guard_note(None) is None

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert adapter.stream_watcher(ctx) is None
    assert "credit guard inactive" in ClaudeCodeAdapter.credit_guard_note(None)

    # A Fireworks-routed model bills Fireworks, which reports no Claude windows.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    fireworks = "accounts/fireworks/models/x"
    assert "credit guard inactive" in ClaudeCodeAdapter.credit_guard_note(fireworks)
    ctx.force_model = fireworks
    assert adapter.stream_watcher(ctx) is None


def test_preflight_reports_the_guard_as_inactive_on_an_api_key(monkeypatch):
    """A user who set a seven-day threshold and is quietly on an API key must be told:
    no events means the sweep will never stop itself, and that looks like health."""
    from qualgentbench import preflight as pf

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    r = pf._check_auth(BenchConfig.model_validate(_CONFIG))
    assert r.passed and r.warning and "credit guard inactive" in r.detail

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = pf._check_auth(BenchConfig.model_validate(_CONFIG))
    assert r.passed and not r.warning


def test_a_rejection_classifies_the_episode_as_rate_limited(tmp_path):
    """claude-code reports a refusal as a structured event, which matches none of the
    prose patterns in failures.py — without the sentinel it would score as a zero."""
    from qualgentbench.failures import RATE_LIMITED, classify

    assert classify(event(status="rejected"), 0) is None      # the regex alone misses it
    assert classify(event(status="rejected"), 0, rejected=True) == RATE_LIMITED
    assert classify("", 0, rejected=True) == RATE_LIMITED


# ── the lanes: stopping between units ─────────────────────────────────────────

APPS = {"birday": {"app": {"id": "birday", "name": "Birday", "package": "com.birday"},
                   "exploration": {"id": "explore-birday", "step_budget": 100,
                                   "features": [{"id": "a", "state": "broken"}]},
                   "tasks": []}}


def _unit(trial: int) -> Unit:
    return Unit("birday", "Birday", "explore-birday", "bug_hunt", "hunt", trial, 100.0,
                "default")


def _plan(units):
    return L.RunPlan(units, {"birday": Path("/apk/birday")}, dict(APPS),
                     plan_summary(units, 1))


class _Engine:
    """Runs episodes on disk the way the real one does, and emits whichever synthetic
    usage snapshot the test asks for on the episode it names."""

    def __init__(self, runs_dir: Path, run_id: str = "r1", *, on_trial: int | None = None,
                 snapshot: dict | None = None, reject: bool = False) -> None:
        self.runs_dir, self.run_id = runs_dir, run_id
        self.on_trial, self.snapshot, self.reject = on_trial, snapshot, reject
        self.calls: list[int] = []

    async def prepare_app(self, session, device, apk, task):
        task.bug_spec["apk_sha256"] = "deadbeef"
        return task.bundle_id

    async def run_episode(self, task, opts):
        self.calls.append(opts.trial)
        episode = self.runs_dir / task.id / f"ep-trial-{opts.trial}"
        episode.mkdir(parents=True, exist_ok=True)
        metrics: dict = {"bugs_found": 1, "f1": 1.0}
        if opts.trial == self.on_trial:
            if self.snapshot is not None:
                credit.record(self.runs_dir / "_runs" / self.run_id, self.snapshot,
                              sticky=True)
                credit.record(episode, self.snapshot, sticky=False)
            if self.reject:
                credit.write_json(episode / credit.RATE_LIMITED_SENTINEL,
                                  {"rate_limit_type": "five_hour",
                                   "resets_at": FIVE_HOUR_RESET})
                metrics["failure_class"] = "rate_limited"
        t0 = datetime(2026, 9, 9, tzinfo=timezone.utc)
        result = RunResult.build(
            task_id=task.id, task_version="v", task_type=opts.task_type, agent=opts.agent,
            model=opts.model, condition="raw", trial=opts.trial, started_at=t0,
            ended_at=t0 + timedelta(seconds=90), exit_code=0,
            verifier=VerifierResult(passed=True, score=1.0, metrics=metrics),
            artifact_dir=episode, runs_dir=self.runs_dir, run_id=opts.run_id)
        result.write(episode / "result.json")
        return result


class _Session:
    async def force_release(self, device=None):
        pass


def _lane_cfg(tmp_path: Path, engine: _Engine, guard) -> L.LaneRun:
    async def _sleep(_):
        await asyncio.sleep(0)
    return L.LaneRun(
        agent="claude-code", model="m", mcp_server="", runs_dir=tmp_path, trials=3,
        run_id="r1", devices=["emu-1"], session=_Session(),
        console=Console(file=open(tmp_path / "out.txt", "w"), force_terminal=False,
                        width=160),
        plain=True, guard=guard,
        hooks=L.Hooks(run_episode=engine.run_episode, prepare_app=engine.prepare_app,
                      sleep=_sleep),
        log=ScheduleLog(tmp_path / "_runs" / "r1" / "schedule.jsonl"))


async def test_the_queue_freezes_after_the_current_unit_not_during_it(tmp_path):
    """0.42 utilization against a 40% threshold: the episode that reported it is
    finished and scored, and no further unit is dequeued."""
    # The guard is built before the run, so everything the run reports is "this
    # sitting" — the ordering `_run_episodes` uses, and what `since` keys off.
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=40)
    engine = _Engine(tmp_path, on_trial=1, snapshot=_snapshot(seven=0.42))
    results = await L.run_lanes(_plan([_unit(1), _unit(2), _unit(3)]),
                                _lane_cfg(tmp_path, engine, guard))

    assert engine.calls == [1]                 # unit 1 ran to the end; 2 and 3 did not
    assert len(results) == 1 and results[0].trial == 1
    assert guard.decision.reason == credit.REASON_SEVEN_DAY
    assert "STOPPING" in (tmp_path / "out.txt").read_text()

    events = [json.loads(line) for line in
              (tmp_path / "_runs" / "r1" / "schedule.jsonl").read_text().splitlines()]
    stop = next(e for e in events if e["event"] == "credit_stop")
    assert stop["reason"] == "seven_day_threshold" and stop["remaining"] == 2


async def test_the_same_reading_below_the_threshold_runs_the_whole_queue(tmp_path):
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=100)
    engine = _Engine(tmp_path, on_trial=1, snapshot=_snapshot(seven=0.42))
    await L.run_lanes(_plan([_unit(1), _unit(2), _unit(3)]),
                      _lane_cfg(tmp_path, engine, guard))
    assert engine.calls == [1, 2, 3] and guard.decision is None


async def test_a_run_with_no_guard_at_all_is_unaffected(tmp_path):
    """Every adapter but claude-code, and claude-code on an API key: no guard, no
    behaviour change."""
    engine = _Engine(tmp_path, on_trial=1, snapshot=_snapshot(status="rejected"))
    await L.run_lanes(_plan([_unit(1), _unit(2)]), _lane_cfg(tmp_path, engine, None))
    assert engine.calls == [1, 2]


async def test_a_five_hour_rejection_discards_the_interrupted_episode(tmp_path):
    """The episode was cut off mid-flight, so it measured nothing: its dir leaves
    `runs/<task>/` — which is what every scorer globs — for `_discarded/<run_id>/`."""
    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1")
    engine = _Engine(tmp_path, on_trial=1, snapshot=_snapshot(status="rejected"),
                     reject=True)
    results = await L.run_lanes(_plan([_unit(1), _unit(2)]),
                                _lane_cfg(tmp_path, engine, guard))

    assert engine.calls == [1]
    assert not (tmp_path / "explore-birday" / "ep-trial-1").exists()
    discarded = tmp_path / "_discarded" / "r1" / "explore-birday" / "ep-trial-1"
    assert (discarded / "result.json").exists()
    # The result stays in the run, excluded, pointing at where its evidence now is.
    assert results[0].artifact_dir == "_discarded/r1/explore-birday/ep-trial-1"
    assert guard.decision.reason == credit.REASON_FIVE_HOUR
    assert guard.decision.payload["resume_after"] == FIVE_HOUR_RESET


async def test_a_rate_limit_without_a_rejection_keeps_its_dir(tmp_path):
    """Today's behaviour for a limit merely recognised in the transcript: the CLI's own
    retries ran out, the episode ended on its own terms, and its dir stays put."""
    from qualgentbench.scheduler import RateLimitBackoff

    engine = _Engine(tmp_path, on_trial=1)

    async def limited_once(task, opts):
        result = await engine.run_episode(task, opts)
        if opts.attempt == 1:
            result.metrics["failure_class"] = "rate_limited"
        return result

    cfg = _lane_cfg(tmp_path, engine, None)
    cfg.hooks.run_episode = limited_once
    cfg.backoff = RateLimitBackoff(lanes=1, base_sec=0.001, rng=lambda: 1.0)
    await L.run_lanes(_plan([_unit(1)]), cfg)

    assert (tmp_path / "explore-birday" / "ep-trial-1" / "result.json").exists()
    assert not (tmp_path / "_discarded").exists()


# ── the exit code ─────────────────────────────────────────────────────────────


async def test_a_credit_stop_exits_75_after_reporting(tmp_path, monkeypatch):
    """75 (EX_TEMPFAIL) is the launcher's signal to resume rather than retry, and it
    has to survive a run that produced no results this sitting — which would otherwise
    exit 1 as "no runs completed"."""
    from qualgentbench import cli, session as session_mod

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def is_healthy(self):
            return True

        async def first_available_device(self):
            return "emu-1"

    monkeypatch.setattr(session_mod, "DeviceSession", _Stub)

    async def _skip_preflight(*a, **kw):
        return None

    monkeypatch.setattr(cli, "_preflight", _skip_preflight)

    guard = credit.CreditGuard(runs_dir=tmp_path, run_id="r1", stop_at_seven_day_pct=40)
    credit.record(tmp_path / "_runs" / "r1", _snapshot(seven=0.42), sticky=True)
    guard.check()

    async def _stopped(*a, **kw):
        raise credit.RunStopped(guard, [])

    monkeypatch.setattr(cli, "_run_episodes", _stopped)

    with pytest.raises(SystemExit) as exc:
        await cli._run_bugs(["m"], "claude-code", 1, "", tmp_path, False, None, None)
    assert exc.value.code == credit.EXIT_STOPPED == 75
