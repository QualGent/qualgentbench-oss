"""Token pricing for benchmark cost estimates. Rates are USD per million tokens —
update the table when provider pricing changes. Long-context surcharges are not
modeled; relative comparisons still hold."""

from __future__ import annotations

# model name → {input, cached_input, output} USD per million tokens
PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "cached_input": 3.00, "output": 180.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "cached_input": 0.30, "output": 15.00},
    "claude-opus-4-8": {"input": 15.00, "cached_input": 1.50, "output": 75.00},
    "claude-haiku-4-5": {"input": 1.00, "cached_input": 0.10, "output": 5.00},
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


def _rates(model: str) -> dict[str, float] | None:
    if model in PRICING:
        return PRICING[model]
    short = model.split("/")[-1]
    return PRICING.get(short)


def compute_cost_usd(model: str, usage: dict) -> float | None:
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
