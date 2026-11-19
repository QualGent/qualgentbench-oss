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

# model name → {input, cached_input, output[, cache_write]} USD per million tokens.
# `cached_input` is the cache-READ rate. Cache WRITES are billed at `cache_write` when
# the row has one, else at the plain input rate (see compute_cost_usd), which
# understates them slightly; the read side is where the volume is. Only the Bedrock
# rows carry `cache_write` today: adding it to a first-party row would move every
# board already priced under that row.
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
    # The rest of the GPT-6 family, same page and caveats (cache writes billed at
    # `input`, the >272K-per-request 2x tier not modelled), read 2026-10-06. Sol and
    # 6.1 Sol share input/output; 6.1 Sol's cache read is half of Sol's.
    "gpt-6-sol": {"input": 2.00, "cached_input": 0.20, "output": 10.00},
    "gpt-6.1-sol": {"input": 2.00, "cached_input": 0.10, "output": 10.00},
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50},
    # Anthropic. Claude 5 is the current generation — the tier that runs against
    # codex's gpt-5.5; the 4.x rows stay so older boards still price.
    # Opus 5.5 / Sonnet 5.5 from the `claude-api` skill (models table cached
    # 2026-09-25), read 2026-10-06: Opus 5.5 is $4/$20 with cache reads at $0.20 —
    # CHEAPER than Opus 5 — and Sonnet 5.5 keeps Sonnet 5's $2/$10. Sonnet 5.5 cache
    # reads corrected to $0.10 (claude-api skill, read 2026-11-19; was $0.20).
    "claude-opus-5-5": {"input": 4.00, "cached_input": 0.20, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "cached_input": 0.10, "output": 10.00},
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
    # The vision-capable journey candidates, Standard tier, read 2026-10-06 from
    # https://docs.fireworks.ai/serverless/pricing (input / cached input / output).
    # DeepSeek's $0.006 cache read is what the page lists; its Priority column
    # ($0.0075) is the same 1.25x step as every other row, so it is not a typo.
    "accounts/fireworks/models/glm-5p3-flash": {
        "input": 0.15, "cached_input": 0.03, "output": 0.50},
    "accounts/fireworks/models/deepseek-v4p1-flash": {
        "input": 0.30, "cached_input": 0.006, "output": 1.20},
    # The page has no row for this slug; it lists "Qwen 3.8 Max" ($2 / $0.25 / $6).
    # Both slugs serve Qwen/Qwen3.8-2.4T-A95B, and this one answers with
    # `"model": "Qwen 3.8 Max"` in every response, so it bills as that row.
    "accounts/fireworks/models/qwen3p8-2p4t-a95b": {
        "input": 2.00, "cached_input": 0.25, "output": 6.00},
}

# Amazon Bedrock rows (QUA-3502), keyed by the normalized model id and by inference
# scope: `regional` is a geo inference profile (`us.…`, `eu.…`, `apac.…` — the ids this
# harness runs, which AWS bills at the "Standard" rate) and `global` a `global.…`
# profile ("Standard, Global"). Bedrock prices separately from each vendor's own API:
# a regional profile is 1.1x the global rate, and claude-code's `total_cost_usd` is
# Anthropic list price, so it is never `reported` for a Bedrock model (usage_metrics).
# Source: the AWS Price List API, read 2026-11-18 (`aws pricing get-products
# --region us-east-1 --service-code AmazonBedrockFoundationModels|AmazonBedrock
# --filters Type=TERM_MATCH,Field=regionCode,Value=us-east-2`), on-demand Standard
# tier, short-context rows: Claude `*_tokens_standard` / `*_global_standard` (cache
# write = the 5-minute TTL row), GPT-6 Astra the same (cache write = its 30-minute
# row), Kimi K3 `moonshotai.kimi-k3-mantle-*-standard` / `-global-standard`
# (cache write 30m). Also on https://aws.amazon.com/bedrock/pricing/ for Kimi.
# Not modelled: Claude's long-context (>200K-token request) rows and 1-hour cache
# writes, GPT-6 Astra's >272K surcharge — this table prices summed usage.
BEDROCK_PRICING: dict[str, dict[str, dict[str, float] | None]] = {
    "claude-opus-5-5": {
        "regional": {"input": 4.40, "cached_input": 0.22, "cache_write": 5.50,
                     "output": 22.00},
        "global": {"input": 4.00, "cached_input": 0.20, "cache_write": 5.00,
                   "output": 20.00}},
    "claude-sonnet-5-5": {
        "regional": {"input": 2.20, "cached_input": 0.11, "cache_write": 2.75,
                     "output": 11.00},
        "global": {"input": 2.00, "cached_input": 0.10, "cache_write": 2.50,
                   "output": 10.00}},
    "claude-haiku-5-5": {
        "regional": {"input": 0.11, "cached_input": 0.011, "cache_write": 0.1375,
                     "output": 0.55},
        "global": {"input": 0.10, "cached_input": 0.01, "cache_write": 0.125,
                   "output": 0.50}},
    "gpt-6-astra": {
        "regional": {"input": 11.00, "cached_input": 1.10, "cache_write": 13.75,
                     "output": 55.00},
        "global": {"input": 10.00, "cached_input": 1.00, "cache_write": 12.50,
                   "output": 50.00}},
    "kimi-k3": {
        "regional": {"input": 3.30, "cached_input": 0.33, "cache_write": 4.125,
                     "output": 16.50},
        "global": {"input": 3.00, "cached_input": 0.30, "cache_write": 3.75,
                   "output": 15.00}},
    # TODO(QUA-3502): no Bedrock price for Kimi K3.1 — the Price List API has no
    # `kimi-k3.1` row for us-east-2 on 2026-11-18, and the pricing page lists only
    # "Kimi K3.1 Pro - Highspeed", a different SKU. Left None: an episode on it reports
    # `cost_source: "unpriced"` rather than a number borrowed from another model. Add
    # the row from the Price List API once AWS publishes it.
    "kimi-k3.1": None,
}

# A model id of this shape is served by Fireworks (the claude-code adapter routes on
# the same prefix). For such a model, claude-code's `total_cost_usd` is Anthropic list
# price applied to Fireworks tokens, so `usage_metrics` never reports it.
FIREWORKS_PREFIX = "accounts/fireworks/models/"

# A Bedrock inference-profile id: `<scope>.<vendor>.<model>` — `us.moonshotai.kimi-k3`,
# `global.anthropic.claude-sonnet-5-5`. The vendor segment is letters only, so a dotted
# model name (`gpt-5.5`) never reads as one.
_BEDROCK_ID = re.compile(r"^(us|eu|apac|jp|au|ca|us-gov|global)\.([a-z]+)\.(.+)$")


def is_fireworks_model(model: str | None) -> bool:
    return bool(model and model.startswith(FIREWORKS_PREFIX))


def bedrock_parts(model: str | None) -> tuple[str, str, str] | None:
    """`(scope, vendor, model)` of a Bedrock inference-profile id, else None."""
    m = _BEDROCK_ID.match((model or "").strip().lower())
    return (m.group(1), m.group(2), m.group(3)) if m else None


def is_bedrock_model(model: str | None) -> bool:
    return bedrock_parts(model) is not None


def provider_of_model(model: str | None) -> str | None:
    """The provider a model id's SHAPE names (`fireworks` | `bedrock`), else None —
    a bare id (`gpt-6-astra`, `claude-sonnet-5-5`) names no provider by itself."""
    if is_fireworks_model(model):
        return "fireworks"
    if is_bedrock_model(model):
        return "bedrock"
    return None


# A dated snapshot suffix a provider appends to a model id: `-2026-09-01` (OpenAI)
# or `-20260901` (Anthropic's older ids). A snapshot bills at its family's rate.
_SNAPSHOT = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def normalize_model(model: str | None) -> str:
    """The PRICING key a reported model id prices under — the id the AGENT reported
    (`TranscriptParser.model()`), which is not always the one the harness asked for.

    Removes, in order: surrounding whitespace and case; a provider routing prefix
    (`openai/gpt-6-astra` → the last path segment); a Bedrock scope and vendor prefix
    (`us.anthropic.claude-…`, `us.moonshotai.kimi-k3`, `global.openai.gpt-6-astra`, a
    bare `anthropic.claude-…`); a Bedrock `-v1:0` version tail; a claude-code context
    tag (`claude-fable-5-1[1m]`); a dated snapshot suffix (`gpt-6-astra-2026-09-01`).
    Nothing else — in particular no family guessing: an id this does not reduce to a
    PRICING key stays unpriced, which is the honest outcome. Fireworks slugs are keyed
    on the full path and are looked up before this runs."""
    m = (model or "").strip().lower()
    m = m.rsplit("/", 1)[-1]
    if parts := bedrock_parts(m):
        m = parts[2]
    m = re.sub(r"^(?:[a-z]{2,4}\.)?anthropic\.", "", m)
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)
    m = re.sub(r"\[[^\]]*\]$", "", m)
    return _SNAPSHOT.sub("", m)


def _rates(model: str, provider: str | None = None) -> dict[str, float] | None:
    """The row ``model`` prices under on ``provider`` (default: the provider its id's
    shape names). A Bedrock model prices ONLY from BEDROCK_PRICING — never from the
    vendor's first-party row, which is a different price."""
    provider = provider or provider_of_model(model)
    if provider == "bedrock":
        row = BEDROCK_PRICING.get(normalize_model(model))
        if row is None:
            return None
        parts = bedrock_parts(model)
        return row["global" if parts and parts[0] == "global" else "regional"]
    if model in PRICING:
        return PRICING[model]
    return PRICING.get(normalize_model(model))


def compute_cost_usd(model: str, usage: Mapping[str, Any],
                     provider: str | None = None) -> float | None:
    """Estimate cost from a token-usage dict (input_tokens incl. cached reads and
    cache writes, cached_input_tokens, cache_write_input_tokens, output_tokens) using
    the pricing tables. Returns None if the model isn't priced there."""
    rates = _rates(model, provider)
    if not rates:
        return None
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached = int(usage.get("cached_input_tokens", 0) or 0)
    writes = int(usage.get("cache_write_input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    # Writes are part of the billed input. A row with no write rate bills them at
    # `input`, which is exactly what it did before writes were counted separately.
    write_rate = rates.get("cache_write")
    if write_rate is None:
        writes, write_rate = 0, 0.0
    uncached_input = max(input_tokens - cached - writes, 0)
    cost = (
        uncached_input * rates["input"]
        + cached * rates["cached_input"]
        + writes * write_rate
        + output * rates["output"]
    ) / 1_000_000
    return round(cost, 6)


def usage_metrics(model: str, usage: Mapping[str, Any],
                  provider: str | None = None) -> dict[str, Any]:
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
    # claude-code prices every request at Anthropic's list rate, whoever served it:
    # a Fireworks GLM-5.3-Flash episode reported $0.56 for ~$0.06 of tokens. The
    # tokens are real; the number is not, so price the tokens instead. Bedrock the
    # same: AWS bills its own (regional: 1.1x) rates, not Anthropic's list price.
    provider = provider or provider_of_model(model)
    if provider in ("fireworks", "bedrock"):
        reported = None

    if source == "none":
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "total_tokens": None,
            # A reported cost with no token breakdown is still a real number.
            "cost_usd": reported,
            "cost_source": COST_REPORTED if reported is not None else COST_UNAVAILABLE,
            "usage_source": source,
        }

    cost = reported if reported is not None else compute_cost_usd(model, usage, provider)
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
        "cache_write_input_tokens": usage.get("cache_write_input_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": cost,
        "cost_source": cost_source,
        "usage_source": source,
    }
