#!/usr/bin/env python3
"""Per-app, per-arm step calibration — the input a budget re-derivation needs, since
older budgets were sized against counters that measured transport, not work. Reads
INTERACTION-unit episodes; one-sample rows (`n` column) are estimates, not calibration."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

KINDS = ("tap", "swipe", "type", "press", "launch", "terminate", "observe", "other")


def collect(runs_dir: Path) -> dict[tuple[str, str], list[dict]]:
    """Every episode that carries an interaction count, keyed by (app, arm)."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in sorted(runs_dir.glob("*/*/result.json")):
        try:
            res = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        m = res.get("metrics") or {}
        total = m.get("interactions")
        # An episode from an older accounting era has no interaction count. Skipping
        # is the point: mixing units is what made every previous budget wrong.
        if not isinstance(total, int) or total <= 0:
            continue
        out[(m.get("app_id") or "?", res.get("condition") or "?")].append({
            "interactions": total,
            "tier": m.get("difficulty"),
            "budget": m.get("step_budget"),
            "hook_steps": m.get("hook_steps"),
            "truncated": bool(m.get("truncated")),
            **{k: m.get(f"interactions_{k}") or 0 for k in KINDS},
        })
    return out


def summarise(rows: list[dict], headroom: float) -> dict:
    vals = [r["interactions"] for r in rows]
    observed = int(statistics.median(vals))
    return {
        "n": len(rows),
        "tier": rows[0]["tier"],
        "observed": observed,
        "min": min(vals),
        "max": max(vals),
        "current_budget": rows[0]["budget"],
        "suggested_budget": int(max(vals) * headroom),
        "truncated": sum(1 for r in rows if r["truncated"]),
        **{k: int(statistics.median([r[k] for r in rows])) for k in KINDS},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--json", help="write the table here as JSON")
    ap.add_argument("--headroom", type=float, default=3.0,
                    help="suggested budget = headroom x the worst observed episode. "
                         "A budget should almost never bind: it is a runaway stop, "
                         "not a scoring mechanism.")
    args = ap.parse_args()

    data = collect(Path(args.runs_dir))
    if not data:
        raise SystemExit(
            "No episodes carry an interaction count yet. Only runs recorded under the "
            "interaction unit are usable.")

    table = {f"{app}/{arm}": summarise(rows, args.headroom)
             for (app, arm), rows in sorted(data.items())}

    print(f"{'app':22}{'arm':9}{'n':>3}{'steps':>7}{'budget':>8}{'used':>7}"
          f"{'tap':>5}{'type':>5}{'press':>6}{'obs':>5}  {'suggested':>9}")
    for key, s in table.items():
        app, arm = key.split("/")
        used = f"{s['observed'] / s['current_budget'] * 100:.0f}%" if s["current_budget"] else "-"
        flag = "  TRUNCATED" if s["truncated"] else ""
        print(f"{app:22}{arm:9}{s['n']:>3}{s['observed']:>7}"
              f"{str(s['current_budget']):>8}{used:>7}"
              f"{s['tap']:>5}{s['type']:>5}{s['press']:>6}{s['observe']:>5}"
              f"{s['suggested_budget']:>10}{flag}")

    for arm in ("raw", "mcp"):
        vals = [s["observed"] for k, s in table.items() if k.endswith("/" + arm)]
        if vals:
            print(f"\n{arm:8} n={len(vals):2}  median {int(statistics.median(vals)):4}"
                  f"  min {min(vals):4}  max {max(vals):4}")

    if args.json:
        Path(args.json).write_text(json.dumps(table, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
