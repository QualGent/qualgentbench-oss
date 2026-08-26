"""Model leaderboard aggregation: collapses per-run results into one ranked row per
model (pass_rate, pass@k, time, tokens, cost, tool calls). Ranking: highest
pass_rate, then cheapest, then fastest."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from math import comb
from pathlib import Path
from typing import Any, Iterable

from .result import RunResult

logger = logging.getLogger(__name__)


def _pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k (Chen et al. 2021): 1 - C(n-c, k) / C(n, k)."""
    if n < k:
        return float(c > 0)
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def _avg(values: list[float]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


def _metric(result: RunResult, key: str) -> float | None:
    v = result.metrics.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def load_results(runs_dir: Path, *, agent: str | None = None,
                 run_id: str | None = None) -> list[RunResult]:
    """Read every ``runs/*/*/result.json``, optionally filtered to one agent and/or
    one ``run`` invocation."""
    out: list[RunResult] = []
    for path in sorted(runs_dir.glob("*/*/result.json")):
        try:
            result = RunResult.model_validate_json(path.read_text())
        except Exception:
            continue
        if agent and result.agent != agent:
            continue
        if run_id and result.run_id != run_id:
            continue
        out.append(result)
    return out


def dedupe_latest(results: Iterable[RunResult]) -> list[RunResult]:
    """Keep only the most recent result per (model, task, condition, trial), so a
    fresh run replaces history rather than blending with it. Condition is in the
    key so paired ablation runs never collapse into one."""
    latest: dict[tuple[str, str, str, int], RunResult] = {}
    for r in results:
        key = (r.model, r.task_id, r.condition, r.trial)
        prev = latest.get(key)
        if prev is None or r.started_at > prev.started_at:
            latest[key] = r
    return list(latest.values())


def clean_model_name(model: str) -> str:
    """Drop provider routing prefixes, keeping just the model name — mirrors the
    run-dir convention."""
    return model.rsplit("/", 1)[-1] if model and "/" in model else (model or "")


def hunt_summary(results: Iterable[RunResult]) -> list[dict[str, Any]]:
    """The Bug-hunt board, one dict per (agent, model, condition): the exact
    numbers `show` prints, as plain key-value data. Written into board.json at the
    end of a run so cross-run comparisons (agents, models, with/without an MCP
    server) read stored facts instead of re-deriving them."""
    from .failures import is_excluded

    def scored(r: RunResult) -> dict:
        m = r.metrics or {}
        return {**m, **(m.get("hybrid") or {})}

    def avg(rs: list[RunResult], key: str) -> float:
        vals = [v for r in rs if (v := scored(r).get(key)) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    groups: dict[tuple[str, str, str], list[RunResult]] = {}
    voided: dict[tuple[str, str, str], int] = {}
    for r in results:
        if r.task_type != "bug_hunt":
            continue
        key = (r.agent, clean_model_name(r.model), r.condition)
        if is_excluded(r.metrics or {}):
            voided[key] = voided.get(key, 0) + 1
            continue
        groups.setdefault(key, []).append(r)

    rows = []
    order: dict[int, float] = {}
    for (agent, model, condition), rs in groups.items():
        fp = sum(scored(r).get("false_positives") or 0 for r in rs)
        ctl = sum(scored(r).get("controls") or 0 for r in rs)
        # Ranked by the SIGNED overall (can dip below 0) so spraying false
        # reports sorts under honest silence; the signed value is not stored.
        order[len(rows)] = avg(rs, "overall_raw") or avg(rs, "overall")
        rows.append({
            "agent": agent, "model": model, "condition": condition,
            "trials": len({r.trial for r in rs}) or 1,
            "episodes": len(rs),
            "excluded": voided.get((agent, model, condition), 0),
            "f1": round(avg(rs, "f1"), 4),
            "fp_rate": round(fp / ctl, 4) if ctl else 0.0,
            "avg_steps": round(avg(rs, "hook_steps") or avg(rs, "steps"), 1),
            "avg_tokens": round(avg(rs, "total_tokens")),
            "overall": round(avg(rs, "overall"), 4),
        })
    for key, n in voided.items():
        if key not in groups:      # every episode voided — still worth a row
            agent, model, condition = key
            order[len(rows)] = float("-inf")
            rows.append({"agent": agent, "model": model, "condition": condition,
                         "trials": 0, "episodes": 0, "excluded": n, "f1": None,
                         "fp_rate": None, "avg_steps": None, "avg_tokens": None,
                         "overall": None})
    return [row for _, row in sorted(enumerate(rows), key=lambda iv: -order[iv[0]])]


def aggregate_by_model(
    results: Iterable[RunResult],
    *,
    k_values: tuple[int, ...] = (1,),
) -> list[dict[str, Any]]:
    """Group results by model and return ranked leaderboard rows."""
    by_model: dict[str, list[RunResult]] = defaultdict(list)
    for r in results:
        by_model[r.model].append(r)

    rows: list[dict[str, Any]] = []
    for model, runs in by_model.items():
        n_runs = len(runs)
        passes = sum(1 for r in runs if r.passed)

        # pass@k is averaged across tasks: per task, n trials and c passes.
        by_task: dict[str, list[RunResult]] = defaultdict(list)
        for r in runs:
            by_task[r.task_id].append(r)

        row: dict[str, Any] = {
            "model": clean_model_name(model),   # group by full id, display/push the short name
            "agent": runs[0].agent,
            "n_runs": n_runs,
            "n_tasks": len(by_task),
            "passed": passes,
            "pass_rate": round(100.0 * passes / n_runs, 1) if n_runs else 0.0,
        }
        for k in k_values:
            per_task = [
                _pass_at_k(len(rs), sum(1 for r in rs if r.passed), k)
                for rs in by_task.values()
            ]
            avg = _avg(per_task)
            row[f"pass@{k}"] = round(100.0 * avg, 1) if avg is not None else None

        row["avg_wall_time_sec"] = _round(_avg([r.wall_time_sec for r in runs]), 1)
        row["avg_total_tokens"] = _round(_avg([_metric(r, "total_tokens") for r in runs]), 0)
        row["avg_cost_usd"] = _round(_avg([_metric(r, "cost_usd") for r in runs]), 6)
        row["avg_device_tool_calls"] = _round(
            _avg([_metric(r, "device_tool_calls") for r in runs]), 1
        )
        rows.append(row)

    rows.sort(key=_rank_key)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def _round(value: float | None, ndigits: int) -> float | int | None:
    if value is None:
        return None
    r = round(value, ndigits)
    return int(r) if ndigits == 0 else r


def _rank_key(row: dict[str, Any]) -> tuple:
    """Best first: highest pass_rate, then cheapest, then fastest."""
    cost = row.get("avg_cost_usd")
    t = row.get("avg_wall_time_sec")
    return (
        -(row.get("pass_rate") or 0.0),
        cost if isinstance(cost, (int, float)) else float("inf"),
        t if isinstance(t, (int, float)) else float("inf"),
    )


# ── CreateBench board (QUA-2599) ─────────────────────────────────────────────
# One row per authored artifact; an ungraded artifact surfaces as "ungraded"
# rather than being silently averaged.

_VALIDITY_FLAGS = ("truncated_without_case", "dead", "off_app", "env_failure")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
