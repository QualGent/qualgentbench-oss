"""What an episode costs, and what it says when nobody counted.

QUA-2715: every claude-code episode on run 20260916-234512-18ac published
`total_tokens: 0`, `cost_usd: 0.0`, `cost_source: "estimated"` — over transcripts
holding 44 and 48 real API requests. Two defects behind one symptom:

  * the parser only read Claude's cumulative `result` event, which a
    budget-truncated episode never reaches (the PreToolUse hook drops the sentinel
    and the process group is SIGKILLed); and
  * an episode with no usage at all still priced 0 tokens and called it "estimated",
    so a board printed $0.00 — which reads as free, not as unmeasured.
"""

from __future__ import annotations

import json

from qualgentbench import pricing
from qualgentbench.transcript import TranscriptParser


def _jsonl(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def _assistant(mid: str, *, input=0, creation=0, read=0, output=0, blocks=1):
    """One API request as claude-code streams it: the SAME message, with the same
    cumulative usage, repeated once per content block."""
    usage = {"input_tokens": input, "cache_creation_input_tokens": creation,
             "cache_read_input_tokens": read, "output_tokens": output}
    return [{"type": "assistant", "uuid": f"{mid}-{i}",
             "message": {"id": mid, "role": "assistant", "model": "claude-opus-5",
                         "usage": usage}}
            for i in range(blocks)]


# ── the stream fallback ───────────────────────────────────────────────────────

def test_a_truncated_claude_episode_still_reports_its_tokens():
    """No `result` event, because the agent was killed at the budget. The usage was
    in the transcript the whole time, one dict per request."""
    transcript = _jsonl(
        {"type": "system", "subtype": "init"},
        *_assistant("msg_1", input=2, creation=44821, output=1),
        *_assistant("msg_2", input=1, creation=867, read=89448, output=21),
    )
    usage = TranscriptParser(transcript).token_usage()
    assert usage["usage_source"] == "stream"
    assert usage["input_tokens"] == 2 + 44821 + 1 + 867 + 89448
    assert usage["cached_input_tokens"] == 89448
    assert usage["output_tokens"] == 22
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["reported_cost_usd"] is None


def test_one_request_repeated_across_content_blocks_is_counted_once():
    """The CLI emits an `assistant` event per content block; a 44-request episode
    arrives as 86 events. Summing them raw doubles the bill."""
    once = TranscriptParser(_jsonl(*_assistant("msg_1", input=100, output=10, blocks=1)))
    thrice = TranscriptParser(_jsonl(*_assistant("msg_1", input=100, output=10, blocks=3)))
    assert once.token_usage()["total_tokens"] == thrice.token_usage()["total_tokens"] == 110


def test_distinct_requests_without_ids_are_not_collapsed():
    """Dedupe is by message id; an id-less event falls back to its own uuid rather
    than onto a single shared key, which would silently under-report."""
    transcript = _jsonl(
        {"type": "assistant", "uuid": "a",
         "message": {"usage": {"input_tokens": 100, "output_tokens": 10}}},
        {"type": "assistant", "uuid": "b",
         "message": {"usage": {"input_tokens": 200, "output_tokens": 20}}},
    )
    assert TranscriptParser(transcript).token_usage()["total_tokens"] == 330


def test_the_cumulative_result_event_still_wins():
    """When the agent exits cleanly its own final total is authoritative — the stream
    fallback must not start double-counting alongside it."""
    transcript = _jsonl(
        *_assistant("msg_1", input=100, output=10),
        {"type": "result", "total_cost_usd": 0.42,
         "usage": {"input_tokens": 500, "cache_read_input_tokens": 300,
                   "output_tokens": 50}},
    )
    usage = TranscriptParser(transcript).token_usage()
    assert usage["usage_source"] == "result"
    assert usage["input_tokens"] == 800 and usage["output_tokens"] == 50
    assert usage["reported_cost_usd"] == 0.42


def test_an_empty_result_usage_falls_back_to_the_stream():
    """The other shape of the same bug: a `result` event arrives, but with nothing in
    it. Trusting it produced zeros over a transcript full of requests."""
    transcript = _jsonl(
        *_assistant("msg_1", input=100, read=900, output=10),
        {"type": "result", "usage": {}},
    )
    usage = TranscriptParser(transcript).token_usage()
    assert usage["usage_source"] == "stream"
    assert usage["total_tokens"] == 1010


def test_codex_turn_deltas_are_untouched():
    """A codex transcript WITH its `turn.completed` events is summed as before. (A
    truncated one has none at all; `test_codex_truncated_usage.py`, QUA-2803.)"""
    transcript = _jsonl(
        {"type": "turn.completed",
         "usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 300}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 500, "cached_input_tokens": 100, "output_tokens": 50}},
    )
    usage = TranscriptParser(transcript).token_usage()
    assert usage["usage_source"] == "turns"
    assert usage["input_tokens"] == 1500 and usage["output_tokens"] == 350


# ── honest unavailability ─────────────────────────────────────────────────────

def test_an_unmeasured_episode_says_unavailable_rather_than_zero():
    """The whole point. $0.00 on a board reads as "this agent is free"; None plus a
    named reason reads as "nobody counted", which is what actually happened."""
    usage = TranscriptParser(_jsonl({"type": "system"})).token_usage()
    assert usage["usage_source"] == "none"

    metrics = pricing.usage_metrics("claude-opus-5", usage)
    assert metrics["cost_usd"] is None
    assert metrics["cost_source"] == pricing.COST_UNAVAILABLE
    assert metrics["total_tokens"] is None
    assert metrics["input_tokens"] is None


def test_a_measured_episode_is_priced_and_says_so():
    transcript = _jsonl(*_assistant("msg_1", input=1_000_000, read=0, output=100_000))
    metrics = pricing.usage_metrics("claude-opus-5",
                                    TranscriptParser(transcript).token_usage())
    # 1M input @ $5 + 100k output @ $25 = $5.00 + $2.50
    assert metrics["cost_usd"] == 7.50
    assert metrics["cost_source"] == pricing.COST_ESTIMATED
    assert metrics["usage_source"] == "stream"


def test_an_unpriced_model_is_not_quietly_estimated_at_zero():
    """A model missing from the table has real tokens and no price. Saying
    "estimated" there would be the same lie in a different place."""
    transcript = _jsonl(*_assistant("msg_1", input=1000, output=100))
    metrics = pricing.usage_metrics("some-model-nobody-priced",
                                    TranscriptParser(transcript).token_usage())
    assert metrics["cost_usd"] is None
    assert metrics["cost_source"] == pricing.COST_UNPRICED
    # Tokens were measured even though the dollars were not — both facts survive.
    assert metrics["total_tokens"] == 1100


def test_a_reported_cost_outranks_the_table():
    transcript = _jsonl({"type": "result", "total_cost_usd": 1.23,
                         "usage": {"input_tokens": 10, "output_tokens": 5}})
    metrics = pricing.usage_metrics("claude-opus-5",
                                    TranscriptParser(transcript).token_usage())
    assert metrics["cost_usd"] == 1.23 and metrics["cost_source"] == pricing.COST_REPORTED


def test_zero_tokens_measured_is_not_the_same_fact_as_unmeasured():
    """An episode that really did spend nothing still counts as measured — the source,
    never the magnitude, decides."""
    transcript = _jsonl({"type": "turn.completed",
                         "usage": {"input_tokens": 0, "output_tokens": 0}})
    usage = TranscriptParser(transcript).token_usage()
    # No shape reported anything, so this one IS unmeasured...
    assert usage["usage_source"] == "none"
    # ...whereas a shape that reported real numbers is not, whatever they are.
    measured = pricing.usage_metrics("claude-opus-5",
                                     {**usage, "usage_source": "turns"})
    assert measured["cost_source"] == pricing.COST_ESTIMATED
    assert measured["cost_usd"] == 0.0


# ── the table ─────────────────────────────────────────────────────────────────

def test_the_current_claude_generation_is_priced():
    """The claude-code arm must be able to run current-gen against codex's gpt-5.5.
    Without a row here the episode prices as `unpriced` and the board has no cost."""
    assert pricing.PRICING["claude-opus-5"] == {
        "input": 5.00, "cached_input": 0.50, "output": 25.00}
    assert pricing.PRICING["claude-sonnet-5"] == {
        "input": 2.00, "cached_input": 0.20, "output": 10.00}


def test_every_row_carries_all_three_rates():
    for model, rates in pricing.PRICING.items():
        assert set(rates) == {"input", "cached_input", "output"}, model
        assert all(isinstance(v, (int, float)) and v >= 0 for v in rates.values()), model


def test_a_cache_read_is_never_dearer_than_a_fresh_read():
    """A transposed pair would inflate exactly the episodes that cache best — which is
    all of them, since the harness resends the whole prefix every request."""
    for model, rates in pricing.PRICING.items():
        assert rates["cached_input"] <= rates["input"], model
        assert rates["output"] >= rates["input"], model
