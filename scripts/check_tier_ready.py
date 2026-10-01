#!/usr/bin/env python3
"""Is a tier actually ready to produce quotable numbers? Each check exists because
the thing it checks broke under a live run and unit tests missed it. Run before
trusting a tier, and after brief/probe/budget edits.

`--tier easy|medium` gates the hunt tiers. `--tier create` gates CreateBench v2 (QUA-2859,
epic QUA-2850) and is what QUA-2861 runs before spending on the live positive control:

  spec       briefs neutral (`lint_create_briefs`, QUA-2853) · controls derived and not
             stale for every brief in scope (QUA-2854) · every target has a
             `QgbFlags.fired` canary (QUA-2860; the corpus-wide coverage is printed)
  scoring    the create adversary gate is green (`create_adversary_check.py`, never with
             provisional controls)
  arm        the creation arm resolves (`--config` with a `create_arm:` block: refs →
             SHAs, template parsed; `--smoke` also runs the real QualGent-MCP against the
             fake) · the fake API captures a create as a gradable `authored_case.json`
  last run   the latest creation episode per brief carries no validity flag

Scope is the positive-control subset (`data/create/positive-control.yaml`) by default,
`--briefs all` for every public brief. READY only when every line passes."""

from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path

from qualgentbench import bugs
from qualgentbench.config import default_runs_dir

ROOT = Path(__file__).resolve().parents[1]
# TODO(QUA-2709): `rotate` is the second lifecycle verb the replay grammar now has
# (see CLAUDE.md, "Lifecycle cases"). A hunt brief saying "rotate the device and
# check the note is still there" hints at a lifecycle defect exactly as "relaunch"
# does, so it probably belongs in this list. Left out deliberately: this is the
# HUNT-mode brief gate and QUA-2709 only added the verb to journey replay routes;
# no spec text mentions rotation today, so adding it is a free but unforced change.
BIAS = re.compile(r"\bbug\b|\bbroken\b|find as many|not been told|what is wrong"
                  r"|relaunch|reopen|come back|only show", re.IGNORECASE)

OK, BAD = "  ok  ", " FAIL "


def _line(label: str, passed: bool, detail: str = "") -> bool:
    print(f"[{OK if passed else BAD}] {label:46s} {detail}")
    return passed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="easy",
                    help="hunt tier by difficulty (easy/medium), or `create` (CreateBench v2)")
    ap.add_argument("--runs-dir", type=Path, default=None,
                    help="episode tree to read (default: ~/.qualgentbench/runs; runs from "
                         "before QUA-2778 are in ./runs)")
    ap.add_argument("--config", type=Path, default=None,
                    help="create: a bench config whose `create_arm:` block names the arm")
    ap.add_argument("--briefs", choices=("subset", "all"), default="subset",
                    help="create: the positive-control subset (default) or every public brief")
    ap.add_argument("--trials", type=int, default=3,
                    help="create: adversary control-rotation trials per brief (default 3)")
    ap.add_argument("--smoke", action="store_true",
                    help="create: also run the real QualGent-MCP at the arm's ref against the "
                         "fake API (installs it into the arm cache)")
    a = ap.parse_args(argv)
    runs_dir = (a.runs_dir or default_runs_dir()).expanduser()
    if a.tier == "create":
        return main_create(a, runs_dir)
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
    print(f"--- budgets vs enforced spend (episodes from {runs_dir}) ---")
    spent: dict[str, list[tuple[str, int, int]]] = {}
    for f in glob.glob(str(runs_dir / "explore-*" / "*" / "result.json")):
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
            pat = runs_dir / f"explore-{s['app']['id']}" / "*" / "result.json"
            latest = None
            for f in sorted(glob.glob(str(pat))):     # dir names are timestamps
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


# ── CreateBench v2 (`--tier create`, QUA-2859) ──────────────────────────────────

NA_LINE = "  --  "


def _load_script(name: str):
    """A sibling script as a module, once. Registered in `sys.modules` before it runs:
    its dataclasses resolve their (postponed) annotations through it."""
    import importlib.util
    import sys
    key = f"_tier_{name.replace('-', '_')}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        del sys.modules[key]
        raise
    return mod


def create_scope(which: str) -> list:
    """The briefs the gate is about: `create_adversary_check.Brief`s."""
    adv = _load_script("create_adversary_check")
    return adv.load_briefs(subset=(which == "subset"))


def check_briefs_neutral() -> tuple[bool, str]:
    """QUA-2853: every public brief neutral and the positive-control subset valid."""
    lcb = _load_script("lint_create_briefs")
    from qualgentbench import corpus, journey
    errors = [f"{app}: {f}" for app, fs in lcb.lint_corpus().items()
              for f in fs if f.level == "error"]
    subset = lcb.load_subset()
    if subset is None:
        errors.append("positive-control subset missing")
    else:
        docs = {a: journey.load_cases(a) or {} for a in corpus.public_apps()}
        canaries = {a: lcb.spec_canaries(a) for a in docs}
        errors += [f"subset: {f}" for f in lcb.lint_subset(subset, docs, canaries)
                   if f.level == "error"]
    return not errors, (f"{len(errors)} error(s), e.g. {errors[0]}" if errors else "")


def check_controls_derived(scope: list) -> tuple[bool, str]:
    """QUA-2854: every brief in scope has `create_controls`, measured against the
    case as it stands (a stale derivation replayed a route the case no longer has)."""
    from qualgentbench import journey
    missing, stale = [], []
    for b in scope:
        doc = journey.load_cases(b.app_id) or {}
        row = journey.load_truth(b.app_id).get(b.case_id)
        if journey.create_controls(row) is None:
            missing.append(b.case_id)
            continue
        der = (row or {}).get(journey.CONTROL_DERIVATION_KEY) or {}
        if der.get("fingerprint") != journey.controls_fingerprint(
                b.ref, journey.load_defects(doc)):
            stale.append(b.case_id)
    bad = len(missing) + len(stale)
    detail = f"{len(scope) - bad}/{len(scope)} derived"
    if missing:
        detail += f"; missing: {', '.join(missing[:6])}{' ...' if len(missing) > 6 else ''}"
    if stale:
        detail += f"; stale: {', '.join(stale)}"
    return not bad, detail


def check_canaries(scope: list) -> tuple[bool, str]:
    """QUA-2860: a target FAIL earns power by canary only if the target has one; in
    scope every target must. The corpus-wide coverage is reported beside it."""
    from qualgentbench import corpus, journey
    from qualgentbench.create import grader
    uncovered = sorted({t for b in scope for t in b.targets
                        if t not in grader.canary_ids(b.app_id)})
    targets = {t for b in scope for t in b.targets}
    tot = cov = 0
    for app in corpus.public_apps():
        ids = set(journey.load_defects(journey.load_cases(app) or {}))
        tot += len(ids)
        cov += len(ids & grader.canary_ids(app))
    detail = f"scope {len(targets) - len(uncovered)}/{len(targets)} · corpus {cov}/{tot}"
    if uncovered:
        detail += f"; no canary: {', '.join(uncovered[:6])}{' ...' if len(uncovered) > 6 else ''}"
    return not uncovered, detail


def check_adversaries(scope: list, trials: int) -> tuple[bool, str]:
    """`create_adversary_check` on the scope, real controls only."""
    adv = _load_script("create_adversary_check")
    res = adv.run_check(scope, trials=trials, provisional=False)
    detail = (f"gradable {len(res.gradable)}/{len(scope)} · {len(res.failures)} failure(s)")
    if res.failures:
        detail += f", e.g. {res.failures[0]}"
    p = res.prediction
    if p and res.ok:
        detail += (f" · harmful-rule power drops on {len(p['power_drops'])}, holds on "
                   f"{len(p['power_holds'])}")
    return res.ok, detail


def check_arm(config: Path | None, smoke: bool) -> tuple[bool, str]:
    """The creation arm resolves: both refs to SHAs, the template parsed. `smoke`: the
    real QualGent-MCP at the pinned ref creates a case against the fake API."""
    if config is None:
        return False, "no arm: pass --config with a `create_arm:` block"
    from qualgentbench.config import ConfigError, load_config
    from qualgentbench.create.arm import ArmError, resolve_arm
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        return False, f"{config}: {exc.problems[0]}"
    if cfg.create_arm is None:
        return False, f"{config} has no `create_arm:` block"
    try:
        arm = resolve_arm(cfg.create_arm, base_dir=config.resolve().parent)
    except (ArmError, OSError) as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
    detail = (f"{arm.name}: qualgent-mcp {arm.qualgent_mcp.sha[:10]} · devloop "
              f"{arm.devloop.sha[:10]} · tools {arm.tools_policy}")
    if not smoke:
        return True, detail
    import asyncio
    import tempfile

    from qualgentbench.create.arm import materialize_qualgent_mcp, probe_arm
    try:
        exe = materialize_qualgent_mcp(arm.qualgent_mcp)
        with tempfile.TemporaryDirectory(prefix="qgb-tier-smoke-") as tmp:
            asyncio.run(probe_arm(arm, Path(tmp) / "ep", command=exe))
    except (ArmError, OSError) as exc:
        return False, f"smoke: {type(exc).__name__}: {str(exc)[:160]}"
    return True, detail + " · smoke ok"


def check_fake_api() -> tuple[bool, str]:
    """The fake API captures a create as an artifact the grader reads, with its id
    (lint's `created-via-api` needs it — QUA-2859 found it dropped)."""
    import tempfile

    import httpx

    from qualgentbench.create import grader
    from qualgentbench.create.arm import SMOKE_CASE
    from qualgentbench.create.fake_api import FakeApp, FakeQualGentAPI
    with tempfile.TemporaryDirectory(prefix="qgb-tier-fake-") as tmp:
        ep = Path(tmp) / "ep"
        try:
            with FakeQualGentAPI(ep, app=FakeApp("SmokeApp")) as api, \
                    httpx.Client(base_url=api.url, headers={"x-api-key": "qg_tier"}) as c:
                r = c.post("/v1/test-cases", json=dict(SMOKE_CASE))
        except (OSError, httpx.HTTPError) as exc:
            return False, f"fake API did not answer: {exc}"
        if r.status_code != 201:
            return False, f"create answered {r.status_code}"
        case, why = grader.load_artifact(ep)
        if case is None:
            return False, why
        if not case.test_case_id or not case.serialized_steps:
            return False, "artifact has no case id or no serialized steps"
    return True, f"{len(case.steps)} steps captured as {case.test_case_id[:8]}…"


#: Validity flags a creation episode can carry (QUA-2856 Fix 5) on top of the shared
#: exclusions (`failures.exclusion_reason`).
# TODO(QUA-2856): align these keys with the creation runner's metrics once it merges
# (it was in flight when this gate was written); `validity_flags`, if the runner records
# a list, is read as-is.
CREATE_VALIDITY_FLAGS = ("no_case_created", "no_case", "dead", "off_app", "truncated",
                         "timed_out")


def creation_episodes(runs_dir: Path) -> dict[str, dict]:
    """The LATEST creation episode per brief case: `{case_id: result}`. A creation
    episode is a `result.json` beside the arm manifest (`arm.json`, QUA-2852) or a
    captured `authored_case.json`."""
    from qualgentbench.create.arm import ARM_MANIFEST_FILE
    from qualgentbench.create.fake_api import AUTHORED_CASE_FILE
    latest: dict[str, tuple[str, dict]] = {}
    for f in glob.glob(str(runs_dir / "*" / "*" / "result.json")):
        ep = Path(f).parent
        if not ((ep / ARM_MANIFEST_FILE).exists() or (ep / AUTHORED_CASE_FILE).exists()):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        m = d.get("metrics") or {}
        case = str(m.get("case_id") or str(d.get("task_id") or ep.parent.name).split("~")[0])
        stamp = str(d.get("started_at") or ep.name)
        if case not in latest or stamp >= latest[case][0]:
            latest[case] = (stamp, d)
    return {c: d for c, (_s, d) in latest.items()}


def creation_flags(result: dict) -> list[str]:
    from qualgentbench import failures
    m = result.get("metrics") or {}
    out = [k for k in CREATE_VALIDITY_FLAGS if m.get(k)]
    out += [str(x) for x in (m.get("validity_flags") or [])]
    if why := failures.exclusion_reason(m):
        out.append(why.split(" ", 1)[0])
    return list(dict.fromkeys(out))


def check_creation_runs(runs_dir: Path, scope: list) -> tuple[bool | None, str]:
    """None (`--`) when no creation episode exists yet; else every brief's latest
    creation episode must be free of validity flags."""
    wanted = {b.case_id for b in scope}
    eps = {c: d for c, d in creation_episodes(runs_dir).items() if c in wanted}
    if not eps:
        return None, f"no creation episodes in {runs_dir}"
    flagged = {c: creation_flags(d) for c, d in eps.items()}
    flagged = {c: f for c, f in flagged.items() if f}
    detail = f"n={len(eps)} flagged={len(flagged)}"
    if flagged:
        c, f = next(iter(flagged.items()))
        detail += f" e.g. {c}: {', '.join(f)}"
    return not flagged, detail


def main_create(a: argparse.Namespace, runs_dir: Path) -> int:
    scope = create_scope(a.briefs)
    print(f"=== create tier — {len(scope)} briefs ({a.briefs}) ===\n--- spec ---")
    if not scope:
        _line("briefs in scope", False, "no brief matched")
        print("\nNOT READY — fix the FAIL lines above")
        return 1
    ok = True
    ok &= _line("briefs are neutral (QUA-2853)", *check_briefs_neutral())
    ok &= _line("controls derived, not stale (QUA-2854)", *check_controls_derived(scope))
    ok &= _line("every target has a fired() canary (QUA-2860)", *check_canaries(scope))
    print("--- scoring ---")
    ok &= _line("create adversary gate green", *check_adversaries(scope, a.trials))
    print("--- arm ---")
    ok &= _line("creation arm resolves" + (" + smoke" if a.smoke else ""),
                *check_arm(a.config, a.smoke))
    ok &= _line("fake API captures a gradable create", *check_fake_api())
    print(f"--- last run (creation episodes from {runs_dir}) ---")
    passed, detail = check_creation_runs(runs_dir, scope)
    if passed is None:
        print(f"[{NA_LINE}] {'latest creation runs carry no validity flag':46s} {detail}")
    else:
        ok &= _line("latest creation runs carry no validity flag", passed, detail)
    print()
    print("READY" if ok else "NOT READY — fix the FAIL lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
