"""Tests for the model leaderboard: aggregation ranking + native transcript fidelity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from qualgentbench.adapters import native
from qualgentbench.episode_runner import _verdict
from qualgentbench.leaderboard import aggregate_by_model
from qualgentbench.result import RunResult, VerifierResult
from qualgentbench.transcript import TranscriptParser


# ── leaderboard aggregation ──────────────────────────────────────────────────


def _result(model: str, task: str, passed: bool, *, tokens=1000, cost=0.01, time=10.0,
            calls=5, trial=1) -> RunResult:
    start = datetime(2026, 6, 8, tzinfo=timezone.utc)
    return RunResult.build(
        task_id=task, task_version="customer-v1", task_type="customer_flow",
        agent="native", model=model, condition="no-routines", trial=trial,
        started_at=start, ended_at=start + timedelta(seconds=time), exit_code=0,
        verifier=VerifierResult(
            passed=passed, score=1.0 if passed else 0.0,
            weighted_score=1.0 if passed else 0.0,
            metrics={"total_tokens": tokens, "cost_usd": cost,
                     "device_tool_calls": calls},
        ),
        artifact_dir="/tmp/x",
    )


def test_ranking_orders_by_pass_rate_then_cost():
    results = [
        # model A: 1/2 pass, cheap
        _result("A", "t1", True, cost=0.01), _result("A", "t2", False, cost=0.01),
        # model B: 2/2 pass, expensive  → should rank #1 (higher pass rate)
        _result("B", "t1", True, cost=0.50), _result("B", "t2", True, cost=0.50),
        # model C: 1/2 pass, cheaper than A → ranks above A on the cost tiebreak
        _result("C", "t1", True, cost=0.005), _result("C", "t2", False, cost=0.005),
    ]
    rows = aggregate_by_model(results)
    order = [r["model"] for r in rows]
    assert order == ["B", "C", "A"]
    assert rows[0]["pass_rate"] == 100.0
    assert rows[0]["rank"] == 1
    assert rows[1]["pass_rate"] == 50.0 and rows[2]["pass_rate"] == 50.0


def test_pass_at_k_uses_chen_estimator():
    # one task, 4 trials, 2 passed: pass@1 = 2/4 = 50%; pass@2 = 1 - C(2,2)/C(4,2) = 83.3%
    results = [_result("M", "t1", i < 2, trial=i + 1) for i in range(4)]
    rows = aggregate_by_model(results, k_values=(1, 2))
    row = rows[0]
    assert row["pass@1"] == 50.0
    assert row["pass@2"] == 83.3
    assert row["n_runs"] == 4 and row["n_tasks"] == 1


# ── native transcript fidelity ───────────────────────────────────────────────


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    id: str
    function: _Fn


def _build_native_transcript() -> str:
    """Reproduce exactly what NativeAdapter emits, then assert the verifier reads it."""
    lines = [
        native._assistant_event(
            "gpt-4o", "Let me look at the screen.",
            [_ToolCall("c1", _Fn("mobile_observe_screen", '{"device":"emulator-5554"}'))],
        ),
        native._tool_result_event("c1", '{"elements":[{"text":"Save"}],"screen_changed":true}'),
        native._assistant_event(
            "gpt-4o", "Task is complete.",
            [_ToolCall("c2", _Fn("mobile_report_result", '{"status":"PASS","summary":"done"}'))],
        ),
        native._tool_result_event("c2", "result recorded"),
        json.dumps(native._result_event({"input": 1200, "output": 300, "cached": 200}, 0.0123)),
    ]
    return "\n".join(lines) + "\n"


def test_native_transcript_is_scored_by_existing_verifier():
    transcript = _build_native_transcript()
    parser = TranscriptParser(transcript)

    # The report status and device evidence the verdict depends on.
    assert parser.reported_status() == "PASS"
    assert len(parser.observation_texts()) == 1          # the mobile_observe_screen
    assert len(parser.successful_device_events()) >= 1
    assert parser.model() == "gpt-4o"

    usage = parser.token_usage()
    assert usage["input_tokens"] == 1200                 # uncached(1000) + cache_read(200)
    assert usage["output_tokens"] == 300
    assert usage["cached_input_tokens"] == 200
    assert usage["reported_cost_usd"] == 0.0123

    # End-to-end: the customer verdict must PASS this transcript.
    verdict = _verdict(transcript, "gpt-4o")
    assert verdict.passed is True
    assert verdict.metrics["reported_status"] == "PASS"
    assert verdict.metrics["cost_usd"] == 0.0123
    assert verdict.metrics["cost_source"] == "reported"


def test_codex_transcript_scores_status_model_and_estimated_cost():
    transcript = "\n".join(
        json.dumps(event)
        for event in [
            {
                "type": "item.completed",
                "item": {
                    "id": "obs_1",
                    "type": "mcp_tool_call",
                    "server": "mcp",
                    "tool": "mobile_observe_screen",
                    "arguments": {"device": "emulator-5554"},
                    "result": {
                        "content": [{"type": "text", "text": '{"screen": "Home"}'}],
                    },
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "report_1",
                    "type": "mcp_tool_call",
                    "server": "mcp",
                    "tool": "mobile_report_result",
                    "arguments": {"status": "PASS", "summary": "done"},
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                    "status": "completed",
                },
            },
            {
                "type": "turn.completed",
                "turn": {"model": "openai/gpt-5.5"},
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 100,
                    "output_tokens": 200,
                    "reasoning_output_tokens": 80,
                },
            },
        ]
    )
    parser = TranscriptParser(transcript)

    assert parser.model() == "openai/gpt-5.5"
    verdict = _verdict(transcript, parser.model() or "gpt-5.5")
    assert verdict.passed is True
    assert verdict.metrics["reported_status"] == "PASS"
    assert verdict.metrics["device_tool_calls"] == 1
    assert verdict.metrics["input_tokens"] == 1000
    assert verdict.metrics["cached_input_tokens"] == 100
    assert verdict.metrics["output_tokens"] == 200
    assert verdict.metrics["total_tokens"] == 1200
    assert verdict.metrics["cost_usd"] == 0.01055
    assert verdict.metrics["cost_source"] == "estimated"


def test_native_transcript_fail_without_evidence():
    """A PASS report with no device interaction must NOT pass (evidence tripwire)."""
    lines = [
        native._assistant_event(
            "gpt-4o", None,
            [_ToolCall("c1", _Fn("mobile_report_result", '{"status":"PASS"}'))],
        ),
        native._tool_result_event("c1", "ok"),
        json.dumps(native._result_event({"input": 10, "output": 5, "cached": 0}, None)),
    ]
    transcript = "\n".join(lines) + "\n"
    verdict = _verdict(transcript, "gpt-4o")
    assert verdict.passed is False
    assert "no device evidence" in (verdict.failure_reason or "")


# ── .env loader: inline comments must not leak into values ───────────────────


def test_dotenv_strips_inline_comments(tmp_path, monkeypatch):
    import os
    from qualgentbench import cli

    env = tmp_path / ".env"
    env.write_text(
        "QGB_WEBHOOK=https://script.google.com/macros/s/ABC/exec  # for --push-sheet\n"
        'QGB_QUOTED="keep#hash"\n'
        "QGB_PLAIN=plainvalue\n"
    )
    monkeypatch.chdir(tmp_path)
    for k in ("QGB_WEBHOOK", "QGB_QUOTED", "QGB_PLAIN"):
        monkeypatch.delenv(k, raising=False)

    cli._load_dotenv()
    assert os.environ["QGB_WEBHOOK"] == "https://script.google.com/macros/s/ABC/exec"
    assert os.environ["QGB_QUOTED"] == "keep#hash"   # '#' inside quotes preserved
    assert os.environ["QGB_PLAIN"] == "plainvalue"


# ── show defaults to the latest run, not all history ─────────────────────────


def test_dedupe_latest_keeps_most_recent_per_model_task_trial():
    from qualgentbench.leaderboard import dedupe_latest

    def at(ts: str, passed: bool) -> RunResult:
        r = _result("gpt-5.4", "t1", passed)
        r.started_at = ts
        return r

    # Same (model, task, trial) seen 3 times — only the latest (passed) survives.
    results = [at("2026-06-08T15:00:00+00:00", False),
               at("2026-06-08T15:05:00+00:00", True),
               at("2026-06-08T15:03:00+00:00", False)]
    kept = dedupe_latest(results)
    assert len(kept) == 1
    assert kept[0].passed is True
    assert kept[0].started_at == "2026-06-08T15:05:00+00:00"

    # Different trials are distinct keys — both kept.
    a = _result("gpt-5.4", "t1", True); a.trial = 1; a.started_at = "2026-06-08T15:00:00+00:00"
    b = _result("gpt-5.4", "t1", True); b.trial = 2; b.started_at = "2026-06-08T15:00:00+00:00"
    assert len(dedupe_latest([a, b])) == 2
