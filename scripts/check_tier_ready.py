#!/usr/bin/env python3
"""Is a tier actually ready to produce quotable numbers? Each check exists because
the thing it checks broke under a live run and unit tests missed it. Run before
trusting a tier, and after brief/probe/budget edits."""

from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path


from qualgentbench import bugs

ROOT = Path(__file__).resolve().parents[1]
BIAS = re.compile(r"\bbug\b|\bbroken\b|find as many|not been told|what is wrong"
                  r"|relaunch|reopen|come back|only show", re.I)

OK, BAD = "  ok  ", " FAIL "


def _line(label: str, passed: bool, detail: str = "") -> bool:
    print(f"[{OK if passed else BAD}] {label:46s} {detail}")
    return passed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="easy",
                    help="hunt tier by difficulty (easy/medium)")
    a = ap.parse_args()
    suites = [s for s in bugs.load_apps() if s["app"].get("difficulty") == a.tier]
    if not suites:
        print(f"no apps at tier {a.tier}")
        return 1

    print(f"=== {a.tier} tier — {len(suites)} apps ===\n--- spec ---")
    ok = True

    # 1. Neutral briefs — a brief that hints at bugs measures the prompt, not the
    #    agent. The title is scanned too; it leaks just like the instruction.
    leaky = [s["app"]["id"] for s in suites
             if BIAS.search(" ".join([(s.get("exploration") or {}).get("instruction") or "",
                                      (s.get("exploration") or {}).get("title") or ""]))]
    ok &= _line("briefs carry no biasing language", not leaky,
                f"leaking: {', '.join(leaky)}" if leaky else "")

    # 2. Deviation rate: too high and "everything deviates" becomes a winning prior.
    tot = dev = 0
    for s in suites:
        fs = (s.get("exploration") or {}).get("features") or []
        tot += len(fs)
        dev += sum(1 for f in fs if f.get("state") == "broken")
    rate = dev / tot if tot else 0
    ok &= _line("deviation rate <= 55%", rate <= 0.55, f"{rate*100:.0f}% ({dev}/{tot})")

    # 3. Budgets present and explicit — a hard gate must be reviewable, not computed
    #    from a formula that silently re-budgets when a run lands.
    nob = [s["app"]["id"] for s in suites
           if not (s.get("exploration") or {}).get("step_budget")]
    ok &= _line("every app has an explicit hunt budget", not nob,
                f"missing: {', '.join(nob)}" if nob else "")

    # 4. Every seeded defect is reachable from a task tier (drives weighted recall).
    untiered = []
    for s in suites:
        task = bugs.exploration_task(s)
        untiered += [f["id"] for f in task.bug_spec["features"]
                     if f["state"] == "broken" and not f.get("tier")]
    ok &= _line("every seeded defect has a tier", not untiered,
                f"untiered: {', '.join(untiered)}" if untiered else "")

    print("--- scoring ---")
    # 5. The adversary must not profit. This is the benchmark's core claim.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "adv", ROOT / "scripts" / "adversary_check.py")
    adv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adv)
    scores = {m: [] for m in ("spray", "crud", "oracle", "honest")}
    for s in suites:
        task = bugs.exploration_task(s)
        for mode in scores:
            v = bugs.exploration_verdict(adv.build(task.bug_spec["features"], mode), "m", task)
            # signed, not clamped: "spraying is worse than silence" only exists below zero
            scores[mode].append(v.metrics["overall_raw"])
    mean = {m: sum(v) / len(v) for m, v in scores.items()}
    ok &= _line("guessing cannot beat testing", mean["honest"] > max(
        mean[g] for g in ("spray", "crud", "oracle")) + 0.5,
        " · ".join(f"{k}={v:.2f}" for k, v in mean.items()))

    # A budget is derived under an accounting rule; change the rule and the number
    # silently goes wrong. Compare each budget against what episodes actually
    # spent in the enforced unit.
    print("--- budgets vs enforced spend ---")
    spent: dict[str, list[tuple[str, int, int]]] = {}
    for f in glob.glob(str(ROOT / "runs" / "explore-*" / "*" / "result.json")):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001
            continue
        m = d.get("metrics") or {}
        if d.get("task_type") != "bug_hunt" or not m.get("hook_steps"):
            continue
        app = m.get("app_id") or ""
        # Only episodes run against the budget currently in the spec — older
        # overruns say nothing about today's budget.
        current = {s["app"]["id"]: (s.get("exploration") or {}).get("step_budget")
                   for s in suites}
        if m.get("step_budget") != current.get(app):
            continue
        if app in {s["app"]["id"] for s in suites}:
            spent.setdefault(d.get("condition") or "plain", []).append(
                (app, int(m["hook_steps"]), int(m.get("step_budget") or 0)))
    if not spent:
        print(f"[{'  --  '}] {'no episodes record hook_steps yet':46s}")
    for cond, eps in sorted(spent.items()):
        over = [(a, h, b) for a, h, b in eps if b and h >= b]
        ok &= _line(f"{cond}: budgets cover what the hook charges", not over,
                    f"n={len(eps)} at-or-over={len(over)}"
                    + (f" e.g. {over[0][0]} {over[0][1]}/{over[0][2]}" if over else ""))

    print("--- last run ---")
    # 6. Episode validity. A truncated or dead episode is not a result, and a score
    #    quoted from one is misleading regardless of what it says.
    for cond, label in (("", "mcp"), ("raw", "raw")):
        # Latest episode per app only — aborted runs leave stale result.json files
        # scored by older code.
        eps = []
        for s in suites:
            pat = f"runs/explore-{s['app']['id']}/*/result.json"
            latest = None
            for f in sorted(glob.glob(str(ROOT / pat))):     # dir names are timestamps
                d = json.loads(Path(f).read_text())
                if d.get("task_type") != "bug_hunt":
                    continue
                if (d.get("condition") == "raw") != (cond == "raw"):
                    continue
                if (d.get("metrics") or {}).get("areas_total"):   # current-spec runs only
                    latest = d["metrics"]
            if latest:
                eps.append(latest)
        if not eps:
            print(f"[{'  --  '}] {label + ': no episodes on the current spec':46s}")
            continue
        # Truncated but COMPLETE is not a lost episode; only a truncation that
        # cost coverage invalidates the data.
        trunc = sum(1 for m in eps
                    if m.get("truncated") and (m.get("coverage") or 0) < 1.0)
        dead = sum(1 for m in eps if (m.get("device_actions") or 0) < 5)
        # Episodes that ended outside the app under test tested a DIFFERENT seeded app.
        # Their verdicts are about the wrong software, at any score.
        off = sum(1 for m in eps if m.get("off_app"))
        cov = sum(m.get("coverage") or 0 for m in eps) / len(eps)
        ok &= _line(f"{label}: no truncated / dead / off-app episodes",
                    not trunc and not dead and not off,
                    f"n={len(eps)} trunc={trunc} dead={dead} off_app={off} "
                    f"mean_coverage={cov*100:.0f}%")
    print()
    print("READY" if ok else "NOT READY — fix the FAIL lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
