"""A budget-truncated claude-code episode on a Fireworks-served model still publishes
its usage, and a Fireworks episode is priced from its tokens, not claude-code's bill.

claude-code's stdout has two usage shapes: the cumulative `result` event, written only
on a clean exit, and per-request usage on each `assistant` event, the fallback for a
truncated episode. Through Fireworks' Anthropic-compatible endpoint the second one is
all zeros (run 20261006-173102-d557, Qwen3.8 2.4T: 82 of 82 events; GLM-5.3-Flash and
DeepSeek V4.1 Flash the same), so `contacts-create-group~clean`, cut at 41/40 steps,
published `usage_source: none`. The CLI's session log under CLAUDE_CONFIG_DIR has the
real usage for the same message ids, and the adapter appends it as one
`qgb.claude_session_usage` line. The events below copy the shapes that run produced.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualgentbench import pricing
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.schemas import Condition
from qualgentbench.transcript import (
    CLAUDE_SESSION_USAGE_EVENT,
    TranscriptParser,
    claude_session_usage,
    claude_session_usage_line,
)

QWEN = "accounts/fireworks/models/qwen3p8-2p4t-a95b"
ZERO = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation": None, "service_tier": None}


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _assistant(msg_id: str, n: int, usage: dict, model: str = "Qwen 3.8 Max") -> dict:
    return {"type": "assistant", "uuid": f"u-{msg_id}-{n}",
            "message": {"id": msg_id, "type": "message", "role": "assistant",
                        "model": model, "usage": usage,
                        "content": [{"type": "tool_use", "id": f"toolu_{msg_id}_{n}",
                                     "name": "mcp__device__mobile_observe_screen",
                                     "input": {"device": "emulator-5554"}}]}}


def _result_for(msg_id: str, n: int) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_{msg_id}_{n}",
         "content": [{"type": "text", "text": "Contacts"}]}]}}


# Fireworks stdout as published: per-request usage zeroed, and no `result` event.
TRUNCATED = _jsonl(
    {"type": "system", "subtype": "init", "model": QWEN},
    _assistant("msg_a", 0, ZERO), _result_for("msg_a", 0),
    _assistant("msg_b", 0, ZERO), _assistant("msg_b", 1, ZERO),
    _result_for("msg_b", 0), _result_for("msg_b", 1),
)
FINISHED = TRUNCATED + _jsonl(
    {"type": "result", "subtype": "success", "total_cost_usd": 1.09,
     "usage": {"input_tokens": 300, "cache_read_input_tokens": 1000,
               "cache_creation_input_tokens": 0, "output_tokens": 50}})


def _usage(inp: int, read: int, out: int) -> dict:
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": 0, "output_tokens": out}


def _write_session(config_dir: Path, *records: dict, name: str = "33a572bd.jsonl") -> Path:
    project = config_dir / "projects" / "-Users-x-runs-case-workspace"
    project.mkdir(parents=True, exist_ok=True)
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_jsonl({"type": "user", "message": {"role": "user", "content": "go"}},
                           *records))
    return path


# The same two requests in the session log, with their real usage; `msg_b` has two
# content blocks, so two records carry its usage.
SESSION = (_assistant("msg_a", 0, _usage(47_868, 0, 123)),
           _assistant("msg_b", 0, _usage(2_000, 46_000, 300)),
           _assistant("msg_b", 1, _usage(2_000, 46_000, 300)))


# ── the parser ────────────────────────────────────────────────────────────────

def test_fireworks_stdout_alone_reports_no_usage():
    """The episode as it was published: zeros everywhere is not a measurement."""
    usage = TranscriptParser(TRUNCATED).token_usage()
    assert usage["usage_source"] == "none"
    assert pricing.usage_metrics(QWEN, usage)["cost_source"] == pricing.COST_UNAVAILABLE


def test_the_session_line_is_measured_usage_labelled_claude_session(tmp_path):
    config = tmp_path / "claude_home"
    _write_session(config, *SESSION)
    line = claude_session_usage_line(config, TRUNCATED)
    assert line is not None and json.loads(line)["type"] == CLAUDE_SESSION_USAGE_EVENT
    assert json.loads(line)["requests"] == 2

    usage = TranscriptParser(TRUNCATED + line + "\n").token_usage()
    assert usage["usage_source"] == "claude_session"
    # Deduped by message id: msg_b counted once, input = uncached + cache reads.
    assert (usage["input_tokens"], usage["cached_input_tokens"],
            usage["output_tokens"]) == (47_868 + 48_000, 46_000, 423)

    metrics = pricing.usage_metrics(QWEN, usage)
    assert metrics["cost_source"] == pricing.COST_ESTIMATED
    assert metrics["cost_usd"] == pytest.approx(
        (49_868 * 2.00 + 46_000 * 0.25 + 423 * 6.00) / 1e6)


def test_stdout_usage_always_outranks_the_session_line():
    line = json.dumps({"type": CLAUDE_SESSION_USAGE_EVENT,
                       "usage": {"input_tokens": 9, "output_tokens": 9}})
    usage = TranscriptParser(FINISHED + line + "\n").token_usage()
    assert usage["usage_source"] == "result"
    assert usage["output_tokens"] == 50


def test_the_session_line_is_invisible_to_every_other_reader():
    line = json.dumps({"type": CLAUDE_SESSION_USAGE_EVENT, "requests": 1,
                       "usage": {"input_tokens": 9, "output_tokens": 9}})
    before, after = TranscriptParser(TRUNCATED), TranscriptParser(TRUNCATED + line)
    assert [e.name for e in after.events()] == [e.name for e in before.events()]
    assert after.model() == before.model()


# ── the session reader ────────────────────────────────────────────────────────

def test_no_session_log_means_no_line_never_a_zero(tmp_path):
    assert claude_session_usage(tmp_path / "claude_home") is None
    assert claude_session_usage_line(tmp_path / "claude_home", TRUNCATED) is None
    # A log whose only records carry zeros (killed during the first request) too.
    _write_session(tmp_path / "claude_home", _assistant("msg_a", 0, ZERO))
    assert claude_session_usage_line(tmp_path / "claude_home", TRUNCATED) is None


def test_sidechain_logs_are_summed(tmp_path):
    """A sub-agent writes its own log, and its requests are billed too."""
    config = tmp_path / "claude_home"
    _write_session(config, *SESSION)
    _write_session(config, _assistant("msg_sub", 0, _usage(1_000, 0, 10)),
                   name="33a572bd/subagents/agent-1.jsonl")
    usage = claude_session_usage(config)
    assert usage["requests"] == 3 and usage["output_tokens"] == 433


def test_a_finished_or_anthropic_episode_gets_no_session_line(tmp_path):
    """A `result` event, or real per-request usage on stdout (an Anthropic-served
    model), already answers; the session log is never blended in."""
    config = tmp_path / "claude_home"
    _write_session(config, *SESSION)
    assert claude_session_usage_line(config, FINISHED) is None
    anthropic = _jsonl(_assistant("msg_a", 0, _usage(10, 0, 5), model="claude-opus-5"))
    assert claude_session_usage_line(config, anthropic) is None


# ── the adapter ───────────────────────────────────────────────────────────────

def _context(run_dir: Path) -> RunContext:
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=900)),
        agent="claude-code", model=QWEN, condition=Condition.no_routines,
        trial=1, run_dir=run_dir, mcp_server="http://localhost:51821",
        mcp_config_path=run_dir / "mcp.json", workspace_dir=run_dir / "workspace",
        disabled_tools=[], inject_mcp=False)


@pytest.mark.parametrize("stdout, appended", [(TRUNCATED, True), (FINISHED, False)])
def test_the_adapter_appends_the_session_line_to_both_transcripts(
        tmp_path, monkeypatch, stdout, appended):
    """The returned transcript is what every scorer reads; `agent/transcript.txt` is
    what a rescore reads. Both must carry the same line."""
    ctx = _context(tmp_path / "run")
    _write_session(ctx.run_dir / "claude_home", *SESSION)

    async def fake_base_run(self, instruction, context):
        path = context.run_dir / "agent" / "transcript.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stdout)
        return stdout, -15

    monkeypatch.setattr("qualgentbench.adapters.base.AgentAdapter.run", fake_base_run)
    transcript, exit_code = asyncio.run(ClaudeCodeAdapter().run("do it", ctx))

    assert exit_code == -15
    assert (ctx.run_dir / "agent" / "transcript.txt").read_text() == transcript
    source = TranscriptParser(transcript).token_usage()["usage_source"]
    if appended:
        assert transcript.startswith(stdout) and transcript != stdout
        assert source == "claude_session"
    else:
        assert transcript == stdout
        assert source == "result"


# ── pricing a Fireworks episode ───────────────────────────────────────────────

def test_a_fireworks_episode_ignores_claude_codes_anthropic_priced_total():
    """claude-code bills every request at Anthropic list price whoever served it: the
    smoke's GLM-5.3-Flash episode reported $0.56 for ~$0.06 of tokens."""
    usage = TranscriptParser(FINISHED).token_usage()
    assert usage["reported_cost_usd"] == pytest.approx(1.09)
    metrics = pricing.usage_metrics(QWEN, usage)
    assert metrics["cost_source"] == pricing.COST_ESTIMATED
    assert metrics["cost_usd"] == pytest.approx((300 * 2.00 + 1000 * 0.25 + 50 * 6.00) / 1e6)
    # An unpriced Fireworks model says so, rather than printing the Anthropic number.
    assert pricing.usage_metrics("accounts/fireworks/models/not-in-the-table",
                                 usage)["cost_source"] == pricing.COST_UNPRICED
    # Anthropic models keep their own reported cost.
    assert pricing.usage_metrics("claude-opus-5", usage)["cost_source"] == pricing.COST_REPORTED


# ── the model of record ───────────────────────────────────────────────────────

def test_a_fireworks_episode_is_recorded_under_its_slug_not_the_display_name():
    from qualgentbench.episode_runner import model_of_record
    assert TranscriptParser(TRUNCATED).model() == "Qwen 3.8 Max"
    assert model_of_record(TRUNCATED, "claude-code-default", QWEN) == QWEN
    assert model_of_record(TRUNCATED, QWEN) == QWEN
    # Anywhere else the transcript still wins over the requested label.
    anthropic = _jsonl(_assistant("m", 0, _usage(1, 0, 1), model="claude-opus-5-5"))
    assert model_of_record(anthropic, "claude-opus-5", "claude-opus-5") == "claude-opus-5-5"
    assert model_of_record("", "claude-opus-5") == "claude-opus-5"
