#!/usr/bin/env python3
"""Derive per-task step budgets from observed runs and write them into the YAMLs
as explicit `step_budget:` keys. Explicit because the budget is a hard gate: it
must be reviewable and stable, not a formula that re-budgets when a run lands."""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import re
import statistics as st
from pathlib import Path

import yaml

from qualgentbench import bugs

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "src" / "qualgentbench" / "data" / "benchmarks"

GUIDED_HEADROOM = 1.5
REPO_PATH_MULT = 3
GUIDED_FLOOR = 30
HUNT_HEADROOM = 1.35            # slack over measured cost, kept deliberately tight
TOOL_CALL_OVERHEAD = 1.2        # guided only (device-actions -> tool-calls)
TOOL_CALL_FIXED = 10            # qg_start / qg_docs / launch / report, per episode
MIN_COVERAGE_TO_TRUST = 0.75    # below this, an episode's per-area cost is noise

_HUNT_CACHE: list[list[dict]] = []   # scanned once; the skip notice is worth printing once


def _hunt_episodes() -> list[dict]:
    """Metrics of every scored hunt episode on disk, IN THE CURRENT STEP UNIT.
    Episodes measured by an older meter are budgets in the wrong unit; they stay
    on disk as provenance but are not evidence about today's cost."""
    if _HUNT_CACHE:
        return _HUNT_CACHE[0]
    out, skipped = [], collections.Counter()
    for f in glob.glob(str(ROOT / "runs" / "explore-*" / "*" / "result.json")):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001
            continue
        m = d.get("metrics") or {}
        if d.get("task_type") != "bug_hunt" or not m.get("device_actions"):
            continue
        unit = m.get("budget_accounting") or "(unstamped)"
        if unit != bugs.BUDGET_ACCOUNTING:
            skipped[unit] += 1
            continue
        out.append(m)
    if skipped:
        detail = ", ".join(f"{n} {u}" for u, n in sorted(skipped.items()))
        print(f"  ignoring {sum(skipped.values())} episode(s) in a stale step unit "
              f"({detail}); budgets need {bugs.BUDGET_ACCOUNTING}")
    _HUNT_CACHE.append(out)
    return out


def _episode_areas(m: dict, fallback: int) -> int:
    """Area count of the spec that episode ran against; episodes recorded before
    areas_total existed fall back to today's count."""
    return int(m.get("areas_total") or fallback)


def _cost_per_area(m: dict, n_areas: int) -> float | None:
    """Budget spend per area covered (in hook_steps, the enforced unit), or None.
    Rejected: low coverage (noise), truncated (censored — deriving from it just
    reproduces the clipped budget), and wrong accounting (a different quantity)."""
    cov = m.get("coverage") or 0
    if m.get("budget_accounting") != bugs.BUDGET_ACCOUNTING:
        return None
    if m.get("truncated") or cov < MIN_COVERAGE_TO_TRUST or not n_areas:
        return None
    spend = m.get("hook_steps")
    if not spend:
        return None
    return spend / (cov * n_areas)


def _hunt_cost_per_area(app_id: str, n_areas: int) -> float | None:
    """Per-area cost for one app: the WORST trusted observation across ALL
    conditions. Max on purpose — under-budgeting destroys an episode, over-budgeting
    only wastes tokens, and one budget must cover the expensive no-MCP condition."""
    vals = [c for m in _hunt_episodes() if m.get("app_id") == app_id
            for c in [_cost_per_area(m, _episode_areas(m, n_areas))] if c]
    return max(vals) if vals else None


def observed() -> dict[str, list[int]]:
    """device_tool_calls per task id, across every scored guided episode on disk."""
    out: dict[str, list[int]] = collections.defaultdict(list)
    for f in glob.glob(str(ROOT / "runs" / "*" / "*" / "result.json")):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001 - a half-written run must not break derivation
            continue
        if d.get("task_type") not in ("bug_task", "clean_task"):
            continue
        calls = (d.get("metrics") or {}).get("device_tool_calls")
        if calls:
            out[d.get("task_id")].append(int(calls))
    return out


def derive(tier: str) -> dict[str, dict]:
    obs = observed()
    # Corpus median per-area cost, from every episode whose coverage makes it
    # meaningful. Each episode is measured against ITS OWN app's area count.
    _areas = {}
    for _p in sorted(BENCH.glob("*.yaml")):
        _s = yaml.safe_load(_p.read_text())
        _areas[_s["app"]["id"]] = len((_s.get("exploration") or {}).get("features") or [])
    trusted = [c for m in _hunt_episodes()
               for c in [_cost_per_area(m, _episode_areas(m, _areas.get(m.get("app_id"), 0)))] if c]
    # Fallback for an app with no trusted measurement of its own: the worst
    # per-area cost seen anywhere — same asymmetry as _hunt_cost_per_area.
    corpus_per_area = max(trusted) if trusted else 0.0
    plans: dict[str, dict] = {}
    for path in sorted(BENCH.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text())
        if spec.get("app", {}).get("difficulty") != tier:
            continue
        tasks, medians = {}, []
        for t in spec.get("tasks", []):
            seen = obs.get(t["id"], [])
            opt = int(t.get("optimal_steps") or 0)
            base = max(
                math.ceil(GUIDED_HEADROOM * max(seen, default=0)),
                REPO_PATH_MULT * opt,
                GUIDED_FLOOR,
            )
            tasks[t["id"]] = math.ceil(base * TOOL_CALL_OVERHEAD)
            if seen:
                medians.append(st.median(seen))
        # Budget from MEASURED hunt cost, not extrapolated from guided episodes —
        # guided re-pays setup every time and over-funds badly. Use the app's own
        # measurement when trustworthy, else the corpus figure.
        n_areas = len((spec.get("exploration") or {}).get("features") or [])
        own = _hunt_cost_per_area(spec["app"]["id"], n_areas)
        per_area = own if own else corpus_per_area
        hunt = (
            math.ceil((per_area * n_areas + TOOL_CALL_FIXED) * HUNT_HEADROOM)
            if per_area else None
        )
        plans[spec["app"]["id"]] = {"path": path, "tasks": tasks, "hunt": hunt}
    return plans


def write(plan: dict) -> int:
    """Insert/replace step_budget: per task and on the exploration block.
    Line-based because yaml.dump would destroy the spec comments."""
    path: Path = plan["path"]
    lines = path.read_text().splitlines()
    out, n, cur = [], 0, None
    for line in lines:
        m = re.match(r"^(\s*)-?\s*id:\s*(\S+)", line)
        if m and m.group(2) in plan["tasks"]:
            cur = m.group(2)
        elif re.match(r"^\s*-\s+id:", line):
            cur = None
        if re.match(r"^\s*step_budget:\s*\d+", line):
            continue                      # drop the old value, re-emitted below
        out.append(line)
        if cur and re.match(r"^\s*optimal_steps:\s*\d+", line):
            indent = re.match(r"^(\s*)", line).group(1)
            out.append(f"{indent}step_budget: {plan['tasks'][cur]}")
            n += 1
            cur = None
    text = "\n".join(out) + "\n"
    if plan["hunt"]:
        text, k = re.subn(r"(\nexploration:\n(?:\s+\S+.*\n)*?\s+id:.*\n)",
                          rf"\1  step_budget: {plan['hunt']}\n", text, count=1)
        n += k
    path.write_text(text)
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="easy")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    plans = derive(a.tier)
    for app, p in sorted(plans.items()):
        print(f"\n{app}  (hunt budget: {p['hunt']})")
        for tid, b in p["tasks"].items():
            print(f"    {tid:38s} {b}")
        if a.write:
            print(f"    -> wrote {write(p)} budgets into {p['path'].name}")
