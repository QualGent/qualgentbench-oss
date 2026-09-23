"""Token pricing for benchmark cost estimates, and the one place an episode's
cost/token block is built. Rates are USD per million tokens — update the table
when provider pricing changes. Long-context surcharges are not modeled; relative
comparisons still hold."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# What a `cost_source` in result.json means. Three of these carry a number a board
# can print; the other two say, in the artifact, WHY there is none. A board that
# prints $0.00 for an episode nobody measured reads as "this agent was free",
# which is the failure this vocabulary exists to prevent (QUA-2715: every
# claude-code episode published `cost_usd: 0.0` / `cost_source: "estimated"`
# with nothing behind it).
COST_REPORTED = "reported"          # the agent's own total_cost_usd
COST_ESTIMATED = "estimated"        # measured tokens × the table below
COST_UNPRICED = "unpriced"          # measured tokens, but the model is not priced here
COST_UNAVAILABLE = "unavailable"    # the transcript reported no usage at all
# The harness never launched the agent (a failed precondition, QUA-2743): a KNOWN
# $0 — nothing ran — and never the "unavailable" an empty transcript would read as.
COST_NOT_LAUNCHED = "not_launched"

# model name → {input, cached_input, output} USD per million tokens.
# `cached_input` is the cache-READ rate. Cache WRITES are billed at the plain
# input rate here (see compute_cost_usd), which understates them slightly; the
# claude `result`/stream usage shapes report reads and writes separately and the
# read side is where the volume is.
PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "cached_input": 3.00, "output": 180.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    # GPT-6 Astra — the codex arm of the Fable-vs-Astra comparison (QUA-2773).
    # Source: OpenAI's published model page and pricing table, Standard tier, read
    # 2026-09-23: https://developers.openai.com/api/docs/models/gpt-6-astra and
    # https://developers.openai.com/api/docs/pricing — input $10, cached input $1,
    # cache writes $12.50, output $50 per 1M tokens. Not modelled here: the cache-write
    # rate (codex reports no write count; writes bill at `input`, as for every row),
    # and the long-context surcharge (a REQUEST over 272K input tokens bills 2x
    # input/cache and 1.5x output) — this table prices an episode's summed usage, not
    # its requests, so an episode with such requests is understated. There is no bare
    # `gpt-6` model id on that page (astra / sol / luna only), so no `gpt-6` row: a
    # bare id is not mapped onto Astra's price by guesswork.
    "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
    # Anthropic. Claude 5 is the current generation — the tier that runs against
    # codex's gpt-5.5; the 4.x rows stay so older boards still price.
    "claude-opus-5": {"input": 5.00, "cached_input": 0.50, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "cached_input": 0.20, "output": 10.00},
    # Fable rows confirmed against the `claude-api` skill 2026-09-23 (QUA-2780):
    # 5.1 is $10/$50 with cache reads at $0.25/MTok (0.025x — NOT the usual 0.1x);
    # Fable 5 is the same $10/$50 with cache reads at $1/MTok.
    "claude-fable-5-1": {"input": 10.00, "cached_input": 0.25, "output": 50.00},
    "claude-fable-5": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
    "claude-opus-4-8": {"input": 5.00, "cached_input": 0.50, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "cached_input": 0.30, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "cached_input": 0.10, "output": 5.00},
    # Deliberately absent, because a plausible-looking number in a table the board
    # MULTIPLIES BY is worse than a missing row (a missing row surfaces as
    # `cost_source: "unpriced"`, which a reader can act on):
    #   claude-mythos-5/5.1 — limited-access models whose cache rate is open;
    #                         nothing in this harness can route to them anyway.
    # Fireworks-hosted OSS models. Needed because claude-code's `total_cost_usd`
    # uses Anthropic list prices even for Fireworks requests. Keyed on the full
    # slug, which also selects the Fireworks route in the adapter.
    "accounts/fireworks/models/kimi-k3": {
        "input": 3.00, "cached_input": 0.30, "output": 15.00},
    "accounts/fireworks/models/kimi-k2p7-code": {
        "input": 0.95, "cached_input": 0.19, "output": 4.00},
    "accounts/fireworks/models/kimi-k2p6": {
        "input": 0.95, "cached_input": 0.16, "output": 4.00},
    "accounts/fireworks/models/glm-5p2": {
        "input": 1.40, "cached_input": 0.14, "output": 4.40},
    "accounts/fireworks/models/deepseek-v4-flash": {
        "input": 0.14, "cached_input": 0.028, "output": 0.28},
    "accounts/fireworks/models/qwen3p7-plus": {
        "input": 0.40, "cached_input": 0.08, "output": 1.60},
}


# A dated snapshot suffix a provider appends to a model id: `-2026-09-01` (OpenAI)
# or `-20260901` (Anthropic's older ids). A snapshot bills at its family's rate.
_SNAPSHOT = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def normalize_model(model: str | None) -> str:
    """The PRICING key a reported model id prices under — the id the AGENT reported
    (`TranscriptParser.model()`), which is not always the one the harness asked for.

    Removes, in order: surrounding whitespace and case; a provider routing prefix
    (`openai/gpt-6-astra`, `us.anthropic.claude-...` → the last path segment, then any
    `<region>.anthropic.` / `anthropic.` prefix); a Bedrock `-v1:0` version tail; a
    claude-code context tag (`claude-fable-5-1[1m]`); a dated snapshot suffix
    (`gpt-6-astra-2026-09-01`). Nothing else — in particular no family guessing: an id
    this does not reduce to a PRICING key stays unpriced, which is the honest outcome.
    Fireworks slugs are keyed on the full path and are looked up before this runs."""
    m = (model or "").strip().lower()
    m = m.rsplit("/", 1)[-1]
    m = re.sub(r"^(?:[a-z]{2,4}\.)?anthropic\.", "", m)
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)
    m = re.sub(r"\[[^\]]*\]$", "", m)
    return _SNAPSHOT.sub("", m)


def _rates(model: str) -> dict[str, float] | None:
    if model in PRICING:
        return PRICING[model]
    return PRICING.get(normalize_model(model))


def compute_cost_usd(model: str, usage: Mapping[str, Any]) -> float | None:
    """Estimate cost from a token-usage dict (input_tokens incl. cached,
    cached_input_tokens, output_tokens) using the PRICING table. Returns None
    if the model isn't in the table."""
    rates = _rates(model)
    if not rates:
        return None
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached = int(usage.get("cached_input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    uncached_input = max(input_tokens - cached, 0)
    cost = (
        uncached_input * rates["input"]
        + cached * rates["cached_input"]
        + output * rates["output"]
    ) / 1_000_000
    return round(cost, 6)


def usage_metrics(model: str, usage: Mapping[str, Any]) -> dict[str, Any]:
    """The cost/token block every scorer writes into `result.json`, built once.

    Six scorers used to inline the same three lines and the same
    `"estimated" if cost is not None else "unknown"` ternary, which is how an
    UNMEASURED episode came to publish `cost_usd: 0.0, cost_source: "estimated"`:
    zero tokens priced at any rate is zero, and "estimated" said a measurement had
    happened. Here, an episode with no usage behind it reports token counts of
    None and `cost_source: "unavailable"` — no number, and the artifact says why.

    `usage` is `TranscriptParser.token_usage()`; its `usage_source` is what
    decides measured-vs-not, never the token values (a real episode can legitimately
    spend few tokens, and 0 is not the same fact as "nobody counted").
    """
    source = str(usage.get("usage_source") or "none")
    reported = usage.get("reported_cost_usd")
    reported = float(reported) if isinstance(reported, (int, float)) else None

    if source == "none":
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "total_tokens": None,
            # A reported cost with no token breakdown is still a real number.
            "cost_usd": reported,
            "cost_source": COST_REPORTED if reported is not None else COST_UNAVAILABLE,
            "usage_source": source,
        }

    cost = reported if reported is not None else compute_cost_usd(model, usage)
    if reported is not None:
        cost_source = COST_REPORTED
    elif cost is not None:
        cost_source = COST_ESTIMATED
    else:
        cost_source = COST_UNPRICED
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": cost,
        "cost_source": cost_source,
        "usage_source": source,
    }
