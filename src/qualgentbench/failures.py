"""Why an episode died, when it was not the agent's fault.

The CLIs retry transient 429s themselves; the harness only sees exhausted retries
as a dead episode. Without this, a rate-limited episode would score as a bad
agent — `infra_failure` means "zero device calls", which a mid-episode limit is not.
"""

from __future__ import annotations

import re

RATE_LIMITED = "rate_limited"

# Provider limit signals as they appear in claude-code stream-json and codex
# output. Matched case-insensitively.
_RATE_LIMIT_RE = re.compile(
    r"rate_limit_error|overloaded_error|rate limit (?:reached|exceeded)|"
    r"rate_limit_exceeded|too many requests|usage_limit_reached|"
    r"\b(?:status|code)\W{0,3}(?:429|529)\b|\bHTTP/?\S* (?:429|529)\b|"
    r"\b(?:429|529) (?:Too Many Requests|Overloaded)",
    re.IGNORECASE,
)
# A signal in the last few KB is how the episode ENDED, not a retried blip.
_TAIL_BYTES = 4096


def classify(transcript: str, exit_code: int, metrics: dict | None = None) -> str | None:
    """`rate_limited` when the provider limit is what stopped the episode; None
    for everything else (a real QA result, or a failure already named by the
    scorer — env_failure, infra_failure, contaminated)."""
    metrics = metrics or {}
    if not transcript:
        return None
    tail_hit = bool(_RATE_LIMIT_RE.search(transcript[-_TAIL_BYTES:]))
    if tail_hit:
        return RATE_LIMITED
    died = exit_code != 0 or bool(metrics.get("env_failure")) or bool(metrics.get("infra_failure"))
    if died and _RATE_LIMIT_RE.search(transcript):
        return RATE_LIMITED
    return None


def is_excluded(metrics: dict) -> bool:
    """The one predicate every board, summary and `show` shares: non-results leave
    the board; weak results stay on it."""
    return bool(metrics.get("env_failure") or metrics.get("infra_failure")
                or metrics.get("contaminated") or metrics.get("failure_class") == RATE_LIMITED)


def exclusion_reason(metrics: dict) -> str:
    if metrics.get("env_failure"):
        return "env_failure — killed before reporting"
    if metrics.get("infra_failure"):
        return "infra_failure — never reached the device"
    if metrics.get("contaminated"):
        return "contaminated — reached the answer key"
    if metrics.get("failure_class") == RATE_LIMITED:
        return "rate_limited — provider limit stopped the episode"
    return ""
