"""A budget-truncated codex-cli episode still publishes its usage (QUA-2803).

`codex exec --json` runs the whole episode as ONE turn and writes ONE `turn.completed`,
at the end, carrying the episode's usage. The budget hook's sentinel makes `base.run`
SIGTERM (then SIGKILL) the agent first, so a truncated episode's transcript has no
usage at all: run 20260924-043254-1e0b, `contacts-favorite~clean` (codex-cli 0.156.1,
gpt-6-astra, 41/40 steps) published `usage_source: none`, `cost_usd: None`, and the
board printed it as `+1 unpriced`, the one episode that cost the most.

Measured on codex-cli 0.156.1 against a mock Responses backend (no model spend): a
turn cut short by SIGTERM, SIGINT or SIGKILL writes no `turn.completed`, so a
graceful stop recovers nothing. But without `--ephemeral` the session rollout keeps
an `event_msg`/`token_count` with the cumulative `total_token_usage` after every
completed response, and it survives SIGKILL. The adapter appends that total as one
`qgb.codex_state_usage` line, which the parser reports as `usage_source: codex_state`.
The rollout lines below copy the shape that measurement produced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualgentbench import pricing
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.schemas import Condition
from qualgentbench.transcript import (
    CODEX_STATE_USAGE_EVENT,
    TranscriptParser,
    codex_rollout_usage,
    codex_state_usage_line,
)


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _tool_call(n: int, status: str) -> dict:
    return {"type": "item.completed" if status == "completed" else "item.started",
            "item": {"id": f"item_{n}", "type": "mcp_tool_call", "server": "device",
                     "tool": "mobile_observe_screen",
                     "arguments": {"device": "emulator-5554"},
                     "result": ({"content": [{"type": "text", "text": "Contacts"}]}
                                if status == "completed" else None),
                     "error": None, "status": status}}


# The truncated episode's shape: thread + turn started, tool calls, the hook's refusal,
# and NO `turn.completed` — stdout ends where the process was killed.
TRUNCATED = (
    "Reading prompt from stdin...\n"
    + _jsonl(
        {"type": "thread.started", "thread_id": "01a0d212-3c54-7911-987e-2ad37dae540f"},
        {"type": "turn.started"},
        _tool_call(1, "in_progress"), _tool_call(1, "completed"),
        _tool_call(2, "in_progress"), _tool_call(2, "completed"),
    )
    + "2026-09-24T06:21:17.534608Z ERROR codex_core::tools::router: error=Tool call "
      "blocked by PreToolUse hook: Tool-call budget (40) exhausted - episode terminated."
)

FINISHED = TRUNCATED + "\n" + _jsonl(
    {"type": "turn.completed",
     "usage": {"input_tokens": 3000, "cached_input_tokens": 1200,
               "cache_write_input_tokens": 0, "output_tokens": 210,
               "reasoning_output_tokens": 60}})


def _token_count(inp: int, cached: int, out: int, reasoning: int) -> dict:
    total = {"input_tokens": inp, "cached_input_tokens": cached,
             "cache_write_input_tokens": 0, "output_tokens": out,
             "reasoning_output_tokens": reasoning, "total_tokens": inp + out}
    return {"timestamp": "2026-09-24T19:46:57.100Z", "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": total, "last_token_usage": total,
                                 "model_context_window": 258400},
                        "rate_limits": {"limit_id": "codex", "primary": None}}}


def _write_rollout(codex_home: Path, *events: dict, torn_tail: bool = False,
                   name: str = "rollout-2026-09-24T06-19-43-01a0d212.jsonl") -> Path:
    day = codex_home / "sessions" / "2026" / "09" / "24"
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    text = _jsonl(
        {"timestamp": "2026-09-24T19:46:50Z", "type": "session_meta",
         "payload": {"id": "01a0d212", "cli_version": "0.156.1"}},
        {"timestamp": "2026-09-24T19:46:50Z", "type": "event_msg",
         "payload": {"type": "task_started"}},
        *events,
    )
    if torn_tail:   # killed mid-write: the last line never got its closing brace
        text += '{"timestamp":"2026-09-24T19:47:01Z","type":"event_msg","payload":{"type":"token_count","info":{"total_'
    path.write_text(text)
    return path


# ── the parser ────────────────────────────────────────────────────────────────

def test_a_truncated_codex_transcript_alone_reports_no_usage():
    """The QUA-2803 episode as it was published: there is nothing in stdout to read."""
    usage = TranscriptParser(TRUNCATED).token_usage()
    assert usage["usage_source"] == "none"
    metrics = pricing.usage_metrics("gpt-6-astra", usage)
    assert metrics["cost_usd"] is None
    assert metrics["cost_source"] == pricing.COST_UNAVAILABLE


def test_the_appended_state_line_is_measured_usage_labelled_codex_state(tmp_path):
    codex_home = tmp_path / "codex_home"
    _write_rollout(codex_home,
                   _token_count(40_000, 30_000, 900, 300),
                   {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                   _token_count(900_000, 800_000, 20_000, 6_000))
    line = codex_state_usage_line(codex_home, TRUNCATED)
    assert line is not None and json.loads(line)["type"] == CODEX_STATE_USAGE_EVENT

    usage = TranscriptParser(TRUNCATED + "\n" + line).token_usage()
    assert usage["usage_source"] == "codex_state"
    # The LAST running total, not a sum of the totals.
    assert (usage["input_tokens"], usage["cached_input_tokens"],
            usage["output_tokens"]) == (900_000, 800_000, 20_000)

    metrics = pricing.usage_metrics("gpt-6-astra", usage)
    assert metrics["usage_source"] == "codex_state"
    assert metrics["cost_source"] == pricing.COST_ESTIMATED
    # 100K uncached x $10 + 800K cached x $1 + 20K out x $50, per 1M
    assert metrics["cost_usd"] == pytest.approx(1.0 + 0.8 + 1.0)


def test_a_turn_completed_always_outranks_the_state_line():
    """Never blended: when Codex answered for itself, the state line is ignored even if
    one is present (the adapter never writes one then; this pins the precedence)."""
    state = json.dumps({"type": CODEX_STATE_USAGE_EVENT,
                        "usage": {"input_tokens": 9, "output_tokens": 9}})
    usage = TranscriptParser(FINISHED + state + "\n").token_usage()
    assert usage["usage_source"] == "turns"
    assert usage["input_tokens"] == 3000 and usage["output_tokens"] == 210


def test_the_state_line_is_invisible_to_every_other_reader():
    """It is typed so that nothing else reads it: no tool call, no model, no event."""
    state = json.dumps({"type": CODEX_STATE_USAGE_EVENT, "rollouts": 1,
                        "usage": {"input_tokens": 9, "output_tokens": 9}})
    before, after = TranscriptParser(TRUNCATED), TranscriptParser(TRUNCATED + state)
    assert [e.name for e in after.events()] == [e.name for e in before.events()]
    assert after.model() == before.model()


# ── the rollout reader ────────────────────────────────────────────────────────

def test_no_rollout_means_no_line_never_a_zero(tmp_path):
    """An `--ephemeral` home (every run before QUA-2803) has no sessions dir: the
    episode stays honestly unavailable rather than priced at $0."""
    assert codex_rollout_usage(tmp_path / "codex_home") is None
    assert codex_state_usage_line(tmp_path / "codex_home", TRUNCATED) is None
    # A rollout with no completed response (killed during the first request) too.
    _write_rollout(tmp_path / "codex_home")
    assert codex_state_usage_line(tmp_path / "codex_home", TRUNCATED) is None


def test_a_torn_last_line_falls_back_to_the_last_whole_count(tmp_path):
    codex_home = tmp_path / "codex_home"
    _write_rollout(codex_home, _token_count(1000, 400, 70, 20), torn_tail=True)
    usage = codex_rollout_usage(codex_home)
    assert usage is not None
    assert (usage["input_tokens"], usage["cached_input_tokens"],
            usage["output_tokens"], usage["rollouts"]) == (1000, 400, 70, 1)


def test_rollouts_of_several_threads_are_summed(tmp_path):
    """A sub-agent is its own thread with its own rollout, and its tokens are billed."""
    codex_home = tmp_path / "codex_home"
    _write_rollout(codex_home, _token_count(1000, 400, 70, 20), name="rollout-a.jsonl")
    _write_rollout(codex_home, _token_count(500, 100, 30, 10), name="rollout-b.jsonl")
    usage = codex_rollout_usage(codex_home)
    assert usage["input_tokens"] == 1500 and usage["output_tokens"] == 100
    assert usage["rollouts"] == 2


def test_a_finished_episode_gets_no_state_line(tmp_path):
    codex_home = tmp_path / "codex_home"
    _write_rollout(codex_home, _token_count(3000, 1200, 210, 60))
    assert codex_state_usage_line(codex_home, FINISHED) is None


# ── the adapter ───────────────────────────────────────────────────────────────

def _context(run_dir: Path) -> RunContext:
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=900)),
        agent="codex-cli", model="gpt-6-astra", condition=Condition.no_routines,
        trial=1, run_dir=run_dir, mcp_server="http://localhost:51821",
        mcp_config_path=run_dir / "mcp.json", workspace_dir=run_dir / "workspace",
        disabled_tools=[], inject_mcp=False)


def test_codex_keeps_its_session_rollout():
    """`--ephemeral` would discard the rollout, the only record a killed turn leaves."""
    cmd = CodexCliAdapter().command("", _context(Path("/tmp/run")))
    assert "--ephemeral" not in cmd


@pytest.mark.parametrize("stdout, appended", [(TRUNCATED, True), (FINISHED, False)])
def test_the_adapter_appends_the_state_line_to_both_transcripts(
        tmp_path, monkeypatch, stdout, appended):
    """The returned transcript is what every scorer reads; `agent/transcript.txt` is
    what a rescore reads. Both must carry the same line, or a rescore unprices it."""
    ctx = _context(tmp_path / "run")
    _write_rollout(ctx.run_dir / "codex_home", _token_count(900_000, 800_000, 20_000, 6_000))

    async def fake_base_run(self, instruction, context):
        path = context.run_dir / "agent" / "transcript.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stdout)
        return stdout, -15

    monkeypatch.setattr("qualgentbench.adapters.base.AgentAdapter.run", fake_base_run)
    transcript, exit_code = asyncio.run(CodexCliAdapter().run("do it", ctx))

    assert exit_code == -15
    on_disk = (ctx.run_dir / "agent" / "transcript.txt").read_text()
    assert on_disk == transcript
    source = TranscriptParser(transcript).token_usage()["usage_source"]
    if appended:
        assert transcript.startswith(stdout) and transcript != stdout
        assert source == "codex_state"
        # The appended line starts on a line of its own after the unterminated tail.
        assert transcript.splitlines()[-1].startswith('{"type": "qgb.codex_state_usage"')
    else:
        assert transcript == stdout
        assert source == "turns"
