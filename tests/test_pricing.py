"""The pricing table rows the Fable-vs-Astra board needs (QUA-2780), and the model-id
normalisation that lets an agent's REPORTED id find its row.

The general cost-block behaviour (`usage_metrics`, unpriced vs unavailable) lives in
`test_usage_accounting.py`; this file is about which rows exist and how an id maps."""

from __future__ import annotations

import inspect

import pytest

from qualgentbench import pricing


def test_gpt6_astra_is_priced_from_the_published_page():
    """$10 input / $1 cached input / $50 output per 1M, Standard tier — OpenAI's model
    page and pricing table, read 2026-09-23. The row carries its source in a comment."""
    assert pricing.PRICING["gpt-6-astra"] == {
        "input": 10.00, "cached_input": 1.00, "output": 50.00}
    src = inspect.getsource(pricing)
    assert "developers.openai.com/api/docs/models/gpt-6-astra" in src
    assert "2026-09-23" in src


def test_no_bare_gpt6_row_is_invented():
    """OpenAI publishes astra / sol / luna and no bare `gpt-6`: a bare id must stay
    unpriced rather than inherit Astra's price by guesswork."""
    assert "gpt-6" not in pricing.PRICING
    m = pricing.usage_metrics("gpt-6", {"usage_source": "turns", "input_tokens": 10,
                                        "output_tokens": 1, "total_tokens": 11})
    assert m["cost_usd"] is None and m["cost_source"] == pricing.COST_UNPRICED


def test_fable_rows_match_the_claude_api_skill():
    """Fable 5.1 reads cache at $0.25/MTok (0.025x) and Fable 5 at $1 — neither is the
    0.1x default a guess would have used."""
    assert pricing.PRICING["claude-fable-5-1"] == {
        "input": 10.00, "cached_input": 0.25, "output": 50.00}
    assert pricing.PRICING["claude-fable-5"] == {
        "input": 10.00, "cached_input": 1.00, "output": 50.00}


@pytest.mark.parametrize("reported, key", [
    ("gpt-6-astra", "gpt-6-astra"),
    ("GPT-6-Astra", "gpt-6-astra"),
    ("openai/gpt-6-astra", "gpt-6-astra"),
    ("gpt-6-astra-2026-09-01", "gpt-6-astra"),
    ("claude-fable-5-1[1m]", "claude-fable-5-1"),
    ("us.anthropic.claude-opus-4-8-v1:0", "claude-opus-4-8"),
    ("anthropic.claude-sonnet-5", "claude-sonnet-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
])
def test_a_reported_model_id_normalises_to_its_row(reported, key):
    assert pricing.normalize_model(reported) == key
    assert key in pricing.PRICING


def test_codex_reported_astra_id_prices_as_estimated():
    """End to end: codex reports the model it ran (`TranscriptParser.model()`), which
    may carry a snapshot suffix; the episode must price, not fall to `unpriced`."""
    usage = {"usage_source": "turns", "input_tokens": 1_000_000,
             "cached_input_tokens": 800_000, "output_tokens": 100_000, "total_tokens": 1_100_000}
    m = pricing.usage_metrics("gpt-6-astra-2026-09-01", usage)
    assert m["cost_source"] == pricing.COST_ESTIMATED
    # 200K uncached x $10 + 800K cached x $1 + 100K out x $50, per 1M
    assert m["cost_usd"] == pytest.approx(2.0 + 0.8 + 5.0)


def test_normalisation_does_not_guess_a_family():
    for model in ("gpt-6", "gpt-6-astra-mini", "claude-fable", "claude-opus-5-5"):
        assert pricing.compute_cost_usd(model, {"input_tokens": 1}) is None, model


def test_fireworks_slugs_still_price_on_the_full_path():
    assert pricing.compute_cost_usd("accounts/fireworks/models/kimi-k3",
                                    {"input_tokens": 1_000_000}) == pytest.approx(3.0)
