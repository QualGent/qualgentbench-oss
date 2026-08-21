#!/usr/bin/env python3
"""Push the MCP ablation (with vs without MCP) to Google Sheets: one metric-grouped
row per (agent, model), each metric shown as base | mcp (+Δ% vs base, colored by
goodness via `higher_better`) | mcp+routines. Keyed by agent|model|date, re-runnable."""

from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import date
from statistics import mean

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (key, title, higher_is_better, getter, formatter). Order = column order.
_ALL_METRICS = [
    ("recall",    "Recall",    True,  lambda r: [m["recall"] for m in r],        lambda x: f"{x*100:.0f}%"),
    ("bugs",      "Bugs/5",    True,  lambda r: [m["bugs_found"] for m in r],    lambda x: f"{x:.1f}"),
    ("precision", "Precision", True,  lambda r: [m["precision"] for m in r],     lambda x: f"{x:.2f}"),
    ("f1",        "F1",        True,  lambda r: [m["f1"] for m in r],            lambda x: f"{x:.2f}"),
    ("fp",        "False+",    False, lambda r: [m["false_positives"] for m in r], lambda x: f"{x:.1f}"),
    ("cost",      "Cost",      False, lambda r: [m.get("cost_usd") or 0 for m in r], lambda x: f"${x:.2f}"),
    ("time",      "Avg time",  False, lambda r: [m["_wall"] for m in r],         lambda x: f"{x:.0f}s"),
    ("tokens",    "Tokens",    False, lambda r: [m.get("total_tokens") or 0 for m in r], lambda x: f"{x/1e6:.2f}M"),
]
# Hero set keeps the row short + value-focused: the lift (recall, bugs), the
# "no false-alarm tax" (precision), and the trade-off (cost, time). --full adds F1,
# False+, Tokens.
_HERO_KEYS = {"recall", "bugs", "precision", "cost", "time"}

# result.json condition -> sheet sub-column key. base is the no-mcp baseline.
_CONDITIONS = [
    ("raw",              "base"),
    ("mcp",          "mcp"),
    ("mcp-routines", "routines"),
]


def _load_hunt_runs() -> dict:
    """(agent, model) -> condition -> list of metric dicts (with _wall added)."""
    out: dict = {}
    for p in glob.glob(os.path.join(_REPO, "runs/explore-*/*/result.json")):
        try:
            r = json.load(open(p))
        except Exception:
            continue
        if r.get("task_type") != "bug_hunt":
            continue
        cond = r.get("condition")
        if cond not in ("raw", "mcp", "mcp-routines"):
            continue
        m = dict(r.get("metrics") or {})
        m["_wall"] = r.get("wall_time_sec") or 0
        out.setdefault((r.get("agent"), r.get("model")), {}).setdefault(cond, []).append(m)
    return out


def _agg(runs: list, getter) -> float | None:
    vals = getter(runs)
    return mean(vals) if vals else None


def _delta_pct(val: float | None, base: float | None) -> float | None:
    if val is None or base is None or base == 0:
        return None
    return (val - base) / base * 100.0


def _build_row(agent: str, model: str, by_cond: dict, day: str, metrics: list) -> dict:
    # episodes per condition (e.g. 9 = 3 apps x 3 trials)
    eps = max((len(v) for v in by_cond.values()), default=0)
    cells: dict = {}
    base_runs = by_cond.get("raw", [])
    for key, _title, higher, getter, fmt in metrics:
        base_val = _agg(base_runs, getter)
        cell = {}
        for cond_key, sub in _CONDITIONS:
            runs = by_cond.get(cond_key, [])
            val = _agg(runs, getter)
            if val is None:
                cell[sub] = {"disp": "", "delta": "", "good": None}
                continue
            entry = {"disp": fmt(val), "delta": "", "good": None}
            if sub != "base":
                d = _delta_pct(val, base_val)
                if d is not None:
                    if abs(d) < 0.5:
                        entry["delta"] = "0%"           # effectively flat
                        entry["good"] = None
                    else:
                        entry["delta"] = f"{d:+.0f}%"
                        improved = (d > 0) if higher else (d < 0)
                        entry["good"] = bool(improved)
            cell[sub] = entry
        cells[key] = cell
    return {
        "run_id": f"{agent}|{model}|{day}",
        "date": day,
        "agent": "Claude Code" if agent == "claude-code" else (agent or ""),
        "model": model or "",
        "episodes": eps,
        "cells": cells,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook-url", default=os.environ.get("QUALGENT_SHEET_WEBHOOK_URL"))
    ap.add_argument("--token", default=os.environ.get("QUALGENT_SHEET_TOKEN"))
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="Run date used in the row key (default: today).")
    ap.add_argument("--full", action="store_true",
                    help="Include all 8 metrics (default: hero set — recall, bugs, "
                         "precision, cost, time).")
    ap.add_argument("--sheet-name", default="Sheet1",
                    help="Destination tab; created if missing (e.g. 'bug_exploratory').")
    ap.add_argument("--dry-run", action="store_true", help="Print the payload, don't POST.")
    args = ap.parse_args()

    metrics = _ALL_METRICS if args.full else [m for m in _ALL_METRICS if m[0] in _HERO_KEYS]

    runs = _load_hunt_runs()
    if not runs:
        raise SystemExit("No bug_hunt runs found under runs/explore-*/. Run the ablation first.")

    rows = [_build_row(a, m, by_cond, args.date, metrics) for (a, m), by_cond in sorted(runs.items())]
    payload = {
        "mode": "ablation",
        "ablation": {
            "sheet_name": args.sheet_name,
            "metrics": [{"key": k, "title": t, "higher_better": h} for k, t, h, _g, _f in metrics],
            "conditions": [
                {"key": "base", "label": "N-DL", "full": "No-MCP",
                 "note": "No-MCP — raw coding agent, no MCP connected; "
                         "it drives the device itself (e.g. via adb). The baseline."},
                {"key": "mcp", "label": "DL", "full": "MCP",
                 "note": "MCP — same coding agent + model, with the MCP "
                         "server connected. Δ% is vs No-MCP."},
                {"key": "routines", "label": "DL-R", "full": "MCP + Routines",
                 "note": "MCP+Routines — MCP plus curated routines/skills. "
                         "Filled in when that condition is run."},
            ],
            "rows": rows,
        },
    }
    if args.token:
        payload["token"] = args.token.split("#", 1)[0].strip()

    if args.dry_run or not args.webhook_url:
        print(json.dumps(payload, indent=2))
        if not args.webhook_url:
            print("\n[no --webhook-url / QUALGENT_SHEET_WEBHOOK_URL set — printed only]")
        return

    import httpx
    url = args.webhook_url.split("#", 1)[0].strip()
    resp = httpx.post(url, json=payload, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise SystemExit(f"Sheet rejected the push: {data['error']}")
    print(f"Pushed {len(rows)} ablation row(s) to '{payload['ablation']['sheet_name']}'. "
          f"Response: {data}")


if __name__ == "__main__":
    main()
