#!/usr/bin/env python3
"""Derive step budgets from observed runs and write them into the YAMLs as explicit
`step_budget:` keys. Explicit because the budget is a hard gate: it must be reviewable
and stable, not a formula that re-budgets when a run lands.

All three task kinds, from the episodes each one actually produces:

  --mode hunt     `exploration.step_budget` and `tasks[].step_budget` in
                  data/benchmarks/*.yaml, from bug_hunt and bug_task/clean_task episodes
  --mode journey  `test_cases[].step_budget` in data/test-cases/*.yaml, from
                  journey_case episodes

Nothing is written without `--write`. The default is a PROPOSAL plus a per-case
diagnosis, because a budget that is too SMALL manufactures failures — in journey mode a
truncation scores as not-completed AND as every seeded bug missed — while the fix for a
truncation is only sometimes a bigger budget. Raising a cap to clear a runaway buys the
agent more failing steps and hides the real defect, so the journey path names the three
things the evidence can support (under-budget / runaway / no evidence) and refuses to
propose a raise off a runaway. Every number carries the episode count behind it."""

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

from qualgentbench import bugs, failures, journey

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "src" / "qualgentbench" / "data" / "benchmarks"
CASES = ROOT / "src" / "qualgentbench" / "data" / "test-cases"
RUNS = ROOT / "runs"

GUIDED_HEADROOM = 1.5
REPO_PATH_MULT = 3
GUIDED_FLOOR = 30
HUNT_HEADROOM = 1.35            # slack over measured cost, kept deliberately tight
TOOL_CALL_OVERHEAD = 1.2        # guided only (device-actions -> tool-calls)
TOOL_CALL_FIXED = 10            # qg_start / qg_docs / launch / report, per episode
MIN_COVERAGE_TO_TRUST = 0.75    # below this, an episode's per-area cost is noise

# Journey is the GUIDED shape — one route, one oracle, one episode — so it takes the
# guided slack (1.5) rather than hunt's tighter 1.35, which is sized for a budget that
# has to cover N areas and can absorb one bad one. No TOOL_CALL_OVERHEAD: the journey
# budget is enforced on hook_steps and the episodes are measured in hook_steps, so there
# is no unit to convert between.
JOURNEY_HEADROOM = 1.5
JOURNEY_FLOOR = 25              # the corpus's own smallest authored budget
# A finished episode at or above this share of its cap was CROWDING it — it finished,
# but only just. Set inside the gap the measured corpus leaves empty (nothing finished
# above 73%, every truncation died above 102%), so it fires on a real trend rather than
# on today's noise.
JOURNEY_CROWDED = 0.85
THIN_EVIDENCE = 2               # fewer trusted episodes than this is a guess, and says so

_HUNT_CACHE: dict[str, list[dict]] = {}   # scanned once per tree; one skip notice is enough


def _read_result(path: str | Path) -> dict | None:
    """A result.json, or None when there is nothing usable there yet. A smoke run
    writing into the same tree leaves episode dirs half-built — a truncated JSON body,
    no result.json at all, sometimes a bare `null` — and so does a real run killed
    mid-episode. Deriving budgets must survive both rather than crash on them."""
    try:
        d = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 - unreadable/partial is "not evidence", not fatal
        return None
    return d if isinstance(d, dict) else None


def _hunt_episodes(runs_dir: Path = RUNS) -> list[dict]:
    """Metrics of every scored hunt episode on disk, IN THE CURRENT STEP UNIT.
    Episodes measured by an older meter are budgets in the wrong unit; they stay
    on disk as provenance but are not evidence about today's cost."""
    key = str(runs_dir)
    if key in _HUNT_CACHE:
        return _HUNT_CACHE[key]
    out, skipped = [], collections.Counter()
    for f in glob.glob(str(runs_dir / "explore-*" / "*" / "result.json")):
        d = _read_result(f)
        if d is None:
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
    _HUNT_CACHE[key] = out
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


def _hunt_cost_per_area(app_id: str, n_areas: int, runs_dir: Path = RUNS) -> float | None:
    """Per-area cost for one app: the WORST trusted observation across ALL
    conditions. Max on purpose — under-budgeting destroys an episode, over-budgeting
    only wastes tokens, and one budget must cover the expensive no-MCP condition."""
    vals = [c for m in _hunt_episodes(runs_dir) if m.get("app_id") == app_id
            for c in [_cost_per_area(m, _episode_areas(m, n_areas))] if c]
    return max(vals) if vals else None


def observed(runs_dir: Path = RUNS) -> dict[str, list[int]]:
    """device_tool_calls per task id, across every scored guided episode on disk."""
    out: dict[str, list[int]] = collections.defaultdict(list)
    for f in glob.glob(str(runs_dir / "*" / "*" / "result.json")):
        d = _read_result(f)
        if d is None:
            continue
        if d.get("task_type") not in ("bug_task", "clean_task"):
            continue
        calls = (d.get("metrics") or {}).get("device_tool_calls")
        if calls:
            out[d.get("task_id")].append(int(calls))
    return out


def derive(tier: str, runs_dir: Path = RUNS) -> dict[str, dict]:
    obs = observed(runs_dir)
    # Corpus median per-area cost, from every episode whose coverage makes it
    # meaningful. Each episode is measured against ITS OWN app's area count.
    _areas = {}
    for _p in sorted(BENCH.glob("*.yaml")):
        _s = yaml.safe_load(_p.read_text())
        _areas[_s["app"]["id"]] = len((_s.get("exploration") or {}).get("features") or [])
    trusted = [c for m in _hunt_episodes(runs_dir)
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
        own = _hunt_cost_per_area(spec["app"]["id"], n_areas, runs_dir)
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


# ── journey (test cases) ───────────────────────────────────────────────────────
# Journey budgets live in a different tree from the other two kinds
# (data/test-cases/*.yaml, as `test_cases[].step_budget`) and gate a different thing:
# ONE route walked twice, clean and seeded. The gate is harsher here than anywhere
# else — a truncated journey episode scores as not-completed AND as every seeded bug
# missed — so a budget that is too small costs two wrong numbers, not noise.
#
# That is also why this path diagnoses instead of fitting. The measured corpus splits
# cleanly: an episode either finishes with a quarter of its cap unused or blows past
# it, with nothing in between. That is the signature of an agent going off the rails,
# not of a route that needs more room, and a tool that scaled budgets up to clear
# every observed truncation would be buying runaway episodes more failing steps at a
# token cost paid by every other episode. So each case gets a VERDICT, and a proposal
# that only a verdict of under-budget supports.


def journey_cases() -> dict[str, dict]:
    """Every authored case: the file it lives in, its `check:` route length and the
    budget in force today. The route is the harness's own deterministic walk of the
    case, so it is the journey analogue of a guided task's `optimal_steps` — the
    shortest the work can possibly be."""
    out: dict[str, dict] = {}
    for path in sorted(CASES.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for case in doc.get("test_cases") or []:
            cid = str(case.get("id") or "")
            if not cid:
                continue
            out[cid] = {
                "path": path,
                "app": str(doc.get("app") or ""),
                "route": len(((case.get("check") or {}).get("steps")) or []),
                "budget": int(case.get("step_budget") or 0),
            }
    return out


def journey_episodes(runs_dir: Path,
                     cases: dict[str, dict]) -> tuple[list[dict], collections.Counter]:
    """Trusted journey episodes, and a tally of what was dropped and why.

    Trusted means: a journey episode, not one of the non-results every board already
    excludes (`failures.is_excluded`), carrying `hook_steps` — the quantity the budget
    is actually enforced on. An episode with no hook_steps never measured the thing the
    gate limits, so it is not evidence about it.

    There is no stale-unit filter to apply: journey mode landed 2026-09-04, after the
    2026-08-19 step-unit change, so every journey episode on disk is already in the
    current unit and none of them stamp `budget_accounting`. The key is honoured if it
    ever appears, so this stays right the day journey starts stamping it."""
    out, dropped = [], collections.Counter()
    for f in sorted(glob.glob(str(runs_dir / "*" / "*" / "result.json"))):
        d = _read_result(f)
        if d is None:
            dropped["unreadable or half-written"] += 1
            continue
        if d.get("task_type") != journey.TASK_TYPE:
            continue
        m = d.get("metrics") or {}
        if failures.is_excluded(m):
            dropped[failures.exclusion_reason(m).split(" —")[0]] += 1
            continue
        unit = m.get("budget_accounting")
        if unit is not None and unit != bugs.BUDGET_ACCOUNTING:
            dropped[f"stale step unit ({unit})"] += 1
            continue
        steps = m.get("hook_steps")
        if not isinstance(steps, int) or steps <= 0:
            dropped["no hook_steps — the enforced unit was never measured"] += 1
            continue
        cid, version = journey.split_task_id(str(d.get("task_id") or ""))
        cid = str(m.get("case_id") or cid)
        if cid not in cases:
            dropped[f"case no longer in the corpus ({cid})"] += 1
            continue
        out.append({
            "case_id": cid,
            "version": str(m.get("version") or version),
            "steps": steps,
            "ran_under": int(m.get("step_budget") or 0),
            "truncated": bool(m.get("truncated")),
            "route": cases[cid]["route"],
        })
    return out, dropped


def _per_route_step(steps: int, route: int) -> float | None:
    return steps / route if route else None


def journey_envelope(eps: list[dict]) -> tuple[float, dict] | None:
    """The most any FINISHED episode has ever spent per route step, and which one.

    Used to judge a truncation that has no finished counterpart: spend inside this
    envelope is spend a real route has been measured to need, spend outside it is the
    agent and not the route. Max on purpose, as in `_hunt_cost_per_area` — the
    asymmetry is the same, and here a wrong call in the tight direction is the one that
    manufactures a failure."""
    vals = [(r, e) for e in eps if not e["truncated"]
            for r in [_per_route_step(e["steps"], e["route"])] if r]
    return max(vals, key=lambda t: t[0]) if vals else None


def _pct(e: dict) -> str:
    return f"{e['steps'] / e['ran_under'] * 100:.0f}%" if e.get("ran_under") else "?"


def _n(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _caps(eps: list[dict]) -> str:
    return ", ".join(f"{e['steps']}/{e['ran_under']}"
                     for e in sorted(eps, key=lambda e: -e["steps"]))


def journey_derive(runs_dir: Path = RUNS) -> dict:
    """Per case: the evidence, a verdict on what that evidence supports, and a budget
    proposal that only a verdict of under-budget recommends acting on."""
    cases = journey_cases()
    eps, dropped = journey_episodes(runs_dir, cases)
    by_case: dict[str, list[dict]] = collections.defaultdict(list)
    for e in eps:
        by_case[e["case_id"]].append(e)
    envelope = journey_envelope(eps)

    rows: dict[str, dict] = {}
    for cid, meta in sorted(cases.items()):
        mine = by_case.get(cid, [])
        # Both VERSIONS of a case are POOLED and the WORST single episode wins. One
        # budget gates clean and seeded alike, so it has to cover the dearer of the
        # two; and neither version is reliably the cheaper one (over the measured
        # corpus seeded is dearer for 7 cases, clean for 5), so an average would
        # under-fund whichever arm happened to be dear for that case. Same asymmetry as
        # `_hunt_cost_per_area`'s max over conditions, for the same reason.
        fin = [e for e in mine if not e["truncated"]]
        trunc = [e for e in mine if e["truncated"]]
        budget = meta["budget"]
        worst_fin = max((e["steps"] for e in fin), default=0)
        worst_trunc = max((e["steps"] for e in trunc), default=0)
        # Crowding is measured against the cap each episode actually RAN UNDER, not
        # today's authored one — the cap it ran under is the one that would have killed
        # it. `check_tier_ready.py` draws the same distinction.
        roomy = [e for e in fin
                 if e["ran_under"] and e["steps"] < JOURNEY_CROWDED * e["ran_under"]]
        crowded = [e for e in fin
                   if e["ran_under"] and e["steps"] >= JOURNEY_CROWDED * e["ran_under"]]

        # A truncated episode is CENSORED: it was killed at the cap, so its spend is a
        # lower bound on what the route costs, never the cost itself. Finished episodes
        # are preferred for exactly that reason (the hunt path rejects truncations
        # outright); a truncation becomes the basis only when nothing finished.
        if fin:
            basis, censored = max(fin, key=lambda e: e["steps"]), False
        elif trunc:
            basis, censored = max(trunc, key=lambda e: e["steps"]), True
        else:
            basis, censored = None, False
        cost = basis["steps"] if basis else 0
        proposal = max(math.ceil(JOURNEY_HEADROOM * cost), JOURNEY_FLOOR) if cost else None

        ratio = _per_route_step(worst_trunc, meta["route"]) if worst_trunc else None
        inside = bool(envelope and ratio is not None and ratio <= envelope[0])

        if not mine:
            verdict = "no evidence"
            why = "no trusted episode has run this case"
        elif trunc and roomy:
            best = max(roomy, key=lambda e: e["steps"])
            verdict = "runaway"
            why = (f"{_n(len(trunc), 'truncation')} at {_caps(trunc)} while a finished episode "
                   f"of the same case spent {best['steps']} ({_pct(best)}) — the same route "
                   f"was walked with room to spare, so the cap is not the first thing to "
                   f"suspect. READ the truncated episode before raising anything: an agent "
                   f"still converging when the cap killed it is a different problem from one "
                   f"adrift, and only the transcript tells them apart")
        elif trunc and inside:
            verdict = "under-budget"
            why = (f"every trusted episode hit the cap ({_caps(trunc + crowded)}) and none "
                   f"finished with room; {ratio:.2f} steps per route step is inside the "
                   f"{envelope[0]:.2f} a finished episode has paid ({envelope[1]['case_id']}"
                   f"~{envelope[1]['version']}), so the spend is consistent with the route "
                   f"rather than with an agent adrift")
        elif trunc and envelope:
            verdict = "runaway"
            why = (f"{_n(len(trunc), 'truncation')} at {_caps(trunc)}; {ratio:.2f} steps per "
                   f"route step is beyond the {envelope[0]:.2f} any finished episode has ever "
                   f"paid, so no measured route needs that spend")
        elif trunc:
            verdict = "no evidence"
            why = (f"{_n(len(trunc), 'truncation')} at {_caps(trunc)} and not one finished "
                   f"episode anywhere in the corpus to compare the spend against")
        elif crowded:
            verdict = "under-budget"
            why = (f"{_n(len(crowded), 'finished episode')} crowding the cap ({_caps(crowded)}) "
                   f"— finished, but with under {100 - JOURNEY_CROWDED * 100:.0f}% of the "
                   f"budget to spare, and the next agent on this route would not")
        else:
            best = max(fin, key=lambda e: e["steps"])
            verdict = "healthy"
            why = (f"worst finished episode spent {best['steps']} of {best['ran_under']} "
                   f"({_pct(best)}), no truncation")

        rows[cid] = {
            **meta, "episodes": mine, "n": len(mine), "finished": len(fin),
            "truncations": len(trunc), "worst_finished": worst_fin or None,
            "worst_truncated": worst_trunc or None, "basis": basis, "censored": censored,
            "proposal": proposal, "verdict": verdict, "why": why,
            "thin": 0 < len(mine) < THIN_EVIDENCE,
            # Only an under-budget verdict may move a budget. A proposal standing on a
            # runaway is printed as unsupported and the authored budget holds: raising a
            # cap to clear a runaway hides the real failure behind a bigger token bill,
            # which is worse than having no tool at all.
            "recommended": (max(budget, proposal) if verdict == "under-budget" and proposal
                            else budget),
        }
    return {"cases": rows, "episodes": eps, "dropped": dropped, "envelope": envelope}


def _rel(p: Path) -> Path:
    """Repo-relative when it can be (a corpus path), absolute otherwise (a test tree)."""
    try:
        return p.relative_to(ROOT)
    except ValueError:
        return p


def _trunc_col(eps: list[dict]) -> str:
    t = [e for e in eps if e["truncated"]]
    return f"{len(t)}@{_pct(max(t, key=lambda e: e['steps']))}" if t else "-"


def journey_report(plan: dict, runs_dir: Path) -> None:
    """Print the proposal and the per-case diagnosis. Every number carries the episode
    count it came from, because a budget derived from one episode is a guess."""
    rows, eps, dropped, envelope = (plan["cases"], plan["episodes"],
                                    plan["dropped"], plan["envelope"])
    fin = sum(1 for e in eps if not e["truncated"])
    with_ev = [c for c in rows.values() if c["n"]]
    print(f"journey budgets — test_cases[].step_budget in {_rel(CASES)}")
    print(f"  episodes read from {runs_dir}")
    print(f"  trusted: {_n(len(eps), 'episode')} over {len(with_ev)} of "
          f"{_n(len(rows), 'case')} — {fin} finished, {len(eps) - fin} truncated")
    for reason, n in sorted(dropped.items()):
        print(f"  dropped: {_n(n, 'episode')} — {reason}")
    if envelope:
        r, e = envelope
        print(f"  worst finished cost per route step: {r:.2f} "
              f"({e['case_id']}~{e['version']}, {e['steps']} steps / route {e['route']})")
    else:
        print("  worst finished cost per route step: unmeasured — no episode finished")

    # `cost` is the episode the derivation STANDS ON: the worst finished one, or a `>=`
    # lower bound when every episode of the case was killed at the cap. Truncations get
    # their own column so a runaway's 106% can never be read as the cost of the route.
    # `derived` is headroom x cost for every case with evidence — only the verdict says
    # whether moving the budget there is supported.
    print(f"\n  {'case':32}{'n':>3}{'route':>7}{'cost':>8}{'%cap':>7}{'trunc':>8}"
          f"{'budget':>8}{'derived':>9}   verdict")
    for cid, c in rows.items():
        basis = c["basis"]
        cost = ("-" if basis is None
                else (f">={basis['steps']}" if c["censored"] else str(basis["steps"])))
        print(f"  {cid:32}{c['n']:>3}{c['route']:>7}{cost:>8}"
              f"{(_pct(basis) if basis else '-'):>7}{_trunc_col(c['episodes']):>8}"
              f"{c['budget']:>8}{(str(c['proposal']) if c['proposal'] else '-'):>9}   "
              f"{c['verdict']}{'  (THIN: 1 episode)' if c['thin'] else ''}")

    print("\n  diagnosis")
    for cid, c in rows.items():
        if not c["n"]:
            continue
        head = f"  {cid} — {c['verdict'].upper()}, n={c['n']}"
        if c["recommended"] != c["budget"]:
            head += f": raise {c['budget']} -> {c['recommended']}"
            if c["censored"]:
                head += " (off a CENSORED lower bound — no episode of this case finished)"
        elif c["verdict"] == "runaway":
            head += (f": NOT SUPPORTED — the truncation is no case for a raise; "
                     f"budget stays {c['budget']}")
        else:
            head += f": budget {c['budget']} stands"
        print(head)
        print(f"      {c['why']}")
        if c["thin"]:
            print("      ONE episode — a guess, not a measurement. Re-run the case "
                  "before acting on this.")

    blank = [cid for cid, c in rows.items() if not c["n"]]
    if blank:
        print(f"\n  no evidence ({len(blank)} of {len(rows)} cases) — authored budget "
              f"stands, nothing the episodes can say:")
        for i in range(0, len(blank), 3):
            print("      " + ", ".join(blank[i:i + 3]))
    moves = {cid: c for cid, c in rows.items() if c["recommended"] != c["budget"]}
    print(f"\n  {_n(len(moves), 'case')} the evidence supports changing"
          + (": " + ", ".join(f"{cid} {c['budget']}->{c['recommended']}"
                              for cid, c in moves.items()) if moves else ""))


def journey_write(plan: dict) -> int:
    """Write the recommended budgets into data/test-cases/*.yaml. Only the cases the
    evidence supports changing are touched — a runaway never moves a budget and a case
    with no evidence is left exactly as authored. Line-based, like `write()`: yaml.dump
    would destroy the seeded-state comments the corpus is documented in."""
    changes = {cid: c for cid, c in plan["cases"].items() if c["recommended"] != c["budget"]}
    n = 0
    for path in sorted({c["path"] for c in changes.values()}):
        mine = {cid: c for cid, c in changes.items() if c["path"] == path}
        out, cur = [], None
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*-\s+id:\s*(\S+)", line)
            if m:
                cur = m.group(1) if m.group(1) in mine else None
            if cur and re.match(r"^\s*step_budget:\s*\d+", line):
                indent = re.match(r"^(\s*)", line).group(1)
                out.append(f"{indent}step_budget: {mine[cur]['recommended']}")
                n += 1
                cur = None
                continue
            out.append(line)
        path.write_text("\n".join(out) + "\n")
    if n != len(changes):
        # A case whose `step_budget:` line this could not find would be left at the old
        # gate while the report claimed it moved — the one failure mode of a line-based
        # writer, and silent unless it is checked.
        raise SystemExit(f"wrote {n} of {len(changes)} budget(s): a case has no "
                         f"step_budget: line to replace. Fix the YAML by hand.")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="hunt", choices=("hunt", "journey"),
                    help="hunt: exploration + guided task budgets in data/benchmarks/*.yaml. "
                         "journey: test-case budgets in data/test-cases/*.yaml.")
    ap.add_argument("--tier", default="easy", help="hunt only; journey has no tiers")
    ap.add_argument("--runs-dir", default=str(RUNS), type=Path,
                    help="episode tree to derive from (default: ./runs)")
    ap.add_argument("--write", action="store_true",
                    help="write the YAMLs. Without it this only PRINTS — a budget is a "
                         "hard gate and moving one is a review, not a side effect.")
    a = ap.parse_args(argv)

    if a.mode == "journey":
        plan = journey_derive(a.runs_dir)
        journey_report(plan, a.runs_dir)
        if a.write:
            print(f"\n  -> wrote {journey_write(plan)} budget(s)")
        return 0

    plans = derive(a.tier, a.runs_dir)
    for app, p in sorted(plans.items()):
        print(f"\n{app}  (hunt budget: {p['hunt']})")
        for tid, b in p["tasks"].items():
            print(f"    {tid:38s} {b}")
        if a.write:
            print(f"    -> wrote {write(p)} budgets into {p['path'].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
