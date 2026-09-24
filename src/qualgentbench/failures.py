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


def classify(transcript: str, exit_code: int, metrics: dict | None = None, *,
             rejected: bool = False) -> str | None:
    """`rate_limited` when the provider limit is what stopped the episode; None
    for everything else (a real QA result, or a failure already named by the
    scorer — env_failure, infra_failure, contaminated).

    `rejected=True` is the adapter saying it watched the provider refuse the request
    on the stream (see `credit.RateLimitWatcher`). That is first-hand and outranks the
    regex, which only catches limits the CLI happened to print in prose — claude-code
    reports a refusal as a structured `rate_limit_event` that matches none of these
    patterns, so without this an episode killed by the credit guard would score as a
    zero.
    """
    metrics = metrics or {}
    if rejected:
        return RATE_LIMITED
    if not transcript:
        return None
    tail_hit = bool(_RATE_LIMIT_RE.search(transcript[-_TAIL_BYTES:]))
    if tail_hit:
        return RATE_LIMITED
    died = exit_code != 0 or bool(metrics.get("env_failure")) or bool(metrics.get("infra_failure"))
    if died and _RATE_LIMIT_RE.search(transcript):
        return RATE_LIMITED
    return None


# ── MCP session integrity (QUA-2806) ─────────────────────────────────────────
#
# `provenance.mcp_isolation` (QUA-2800) is the server's own record of every MCP client
# session that touched this episode's device while the agent ran, and whether each
# began with no state from an earlier one. `clean: false` on a server that keeps that
# record means the agent could have read another episode's action log, baselines or
# traces — the environment was not the one every other episode got, so the episode is
# EXCLUDED like an env_failure (never averaged in as a result). A DevLoop server that
# failed to answer is the same: `run` refuses one without the record, so a missing one
# mid-run is unverifiable. Another MCP server keeps no such record at all (`isolation:
# unavailable`); its episodes are FLAGGED (`mcp_isolation_unverified`), not excluded,
# or no board could ever run on one.
MCP_UNCLEAN = "mcp_unclean"
MCP_UNVERIFIED = "mcp_isolation_unverified"
_ISOLATED = "per_mcp_session"


def mcp_integrity(mcp_isolation: dict | None, server_name: str | None = None) -> dict:
    """The metrics an episode's `mcp_isolation` record adds: `{mcp_unclean: True}` to
    exclude it, `{mcp_isolation_unverified: True}` to flag it, `{}` when it is clean or
    there is no MCP server (None). `server_name` is the stamped `mcp_server.name`; for
    an episode recorded before the stamp, a record that reports `per_mcp_session` is
    the proof the server keeps one."""
    if not isinstance(mcp_isolation, dict) or mcp_isolation.get("clean") is not False:
        return {}
    keeps_record = (mcp_isolation.get("isolation") == _ISOLATED
                    or server_name == "devloop-mcp")
    return {MCP_UNCLEAN: True} if keeps_record else {MCP_UNVERIFIED: True}


def apply_mcp_integrity(metrics: dict, provenance: dict | None) -> dict:
    """Write `mcp_integrity` of `provenance` into `metrics` (in place, and returned).
    One reading for the live episode and for a rescore of a saved one."""
    prov = provenance or {}
    server = (prov.get("mcp_server") or {}).get("name") if isinstance(
        prov.get("mcp_server"), dict) else None
    metrics.update(mcp_integrity(prov.get("mcp_isolation"), server))
    return metrics


def is_excluded(metrics: dict) -> bool:
    """The one predicate every board, summary and `show` shares: non-results leave
    the board; weak results stay on it."""
    return bool(metrics.get("env_failure") or metrics.get("infra_failure")
                or metrics.get("contaminated") or metrics.get(MCP_UNCLEAN)
                or metrics.get("failure_class") == RATE_LIMITED)


def exclusion_reason(metrics: dict) -> str:
    if metrics.get("env_failure"):
        return "env_failure — killed before reporting"
    if metrics.get("infra_failure"):
        return "infra_failure — never reached the device"
    if metrics.get("contaminated"):
        reasons = metrics.get("contamination_reasons") or []
        # Name the mechanism, because they are not the same failure: most void kinds
        # mean the agent READ the answer key, but a rooted adbd or a server bypass mean
        # it went AROUND the meter — the episode ran unmetered, whether or not it then
        # read anything (QUA-2814).
        if "adbd_rooted" in reasons:
            return "contaminated — adbd ended the episode rooted (a privilege change got past the meter)"
        if "adb_server_bypass" in reasons:
            return "contaminated — adb was pointed at a server around the meter (the episode ran unmetered)"
        return "contaminated — reached the answer key"
    if metrics.get(MCP_UNCLEAN):
        return "mcp_unclean — an MCP server session did not start from clean state"
    if metrics.get("failure_class") == RATE_LIMITED:
        return "rate_limited — provider limit stopped the episode"
    return ""
