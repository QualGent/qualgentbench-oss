"""Report quality per arm: the block printed UNDER the Rates block (QUA-3588).

`report_quality` writes one `quality.json` per episode. This module adds those files up
over a run, one row per arm (agent, model, condition; held-out episodes are their own
row, as on the board), and prints where reports are lost.

It is never a ranking key. Rows are listed in name order, never by a quality number, and
nothing here is read by `journey.summary`, `journey.ranking_key` or any scorer. The
scoring modules do not import this module.

Every number names its denominator (docs/report-quality.md, "The Report quality block"):

  gate pass rate        reports the gate passed / reports it answered pass or fail for.
                        Unknown answers are counted beside the rate, in neither side.
  customer_ready_rate   reports that passed every headline gate / reports with a decided
                        answer (ready, or failed a gate). Not measured while a headline
                        gate has not run.
  customer_ready_yield  seeded defects with at least one customer-ready report / seeded
                        defects with a decided answer (`bugs_present` of `result.json`,
                        per episode, less the defects whose reports are left unknown:
                        the same rule as the rate). `coverable` beside it is the
                        ceiling: the defects that could be covered at all.
  reports_per_defect    matched reports / defects found (a ratio, not a rate).
  funnel                reports -> valid -> located -> complete -> reproducible: each
                        stage keeps the reports that passed every gate so far.

    python -m qualgentbench.quality_rates <runs dir> [--run ID] [--saved] [--json]

prints the block for the episodes under `<run dir>`. It writes nothing.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import rates
from . import report_quality as rq
from .failures import is_excluded

#: The shape of the block (`block()`); bump when a key changes meaning or leaves.
BLOCK_FORMAT = 1

#: One word per headline gate, for a gate no saved `quality.json` has registered yet.
GATE_NAMES = {"G1": "valid", "G2": "located", "G3": "complete", "G4": "reproducible"}
#: The gates whose pass count is the signal of the precision-against-coverage table:
#: deterministic, and known before any replay.
SIGNAL_GATES = ("G2", "G3")

# What one gate said about one report.
PASS, FAIL, UNKNOWN, NOT_RUN = "pass", "fail", "unknown", "not_run"
# What the headline gates together say about one report.
READY, NOT_READY, WAITING = "ready", "not_ready", "waiting"

TITLE = "Report quality"
CAPTION = ("per arm, 95% Wilson intervals. Read beside the ranking, never in place of "
           "it: rows are in name order and no number here ranks anything.")
LEGEND = (
    "gate rate = passed / reports the gate answered pass or fail for (unknown answers are "
    "counted beside it, in neither side) · customer-ready rate = reports passing every "
    "headline gate / reports with a decided answer · yield = seeded defects with a "
    "customer-ready report / seeded defects with a decided answer (a defect whose "
    "reports are left unknown is in neither side, as an unknown report is in neither "
    "side of the rate; the defects that could be covered at all are printed beside it) "
    "· reports per defect = matched "
    "reports / defects found · neutral reports are in no number · a rate is comparable "
    "only between rows with the same gates available")


def _answer(report: Mapping[str, Any], gate: str) -> str:
    """One gate's answer for one report: pass, fail, unknown (it ran and could not
    decide) or not_run (it did not run for this report, or is not in the file)."""
    entry = (report.get("gates") or {}).get(gate)
    if not isinstance(entry, Mapping):
        return NOT_RUN
    if entry.get("pass") is True:
        return PASS
    if entry.get("pass") is False:
        return FAIL
    return UNKNOWN if entry.get("unknown") else NOT_RUN


def _state(report: Mapping[str, Any], gates: Iterable[str]) -> tuple[str, list[str]]:
    """Whether a report passes every gate in `gates`: `(state, gates it waits on)`.
    A failed gate decides it whatever the others say. Otherwise it is ready when all
    passed, waiting when a gate has not run for it, and unknown when every gate ran and
    at least one could not decide."""
    answers = {g: _answer(report, g) for g in gates}
    if FAIL in answers.values():
        return NOT_READY, []
    if all(a == PASS for a in answers.values()):
        return READY, []
    waiting = [g for g, a in answers.items() if a == NOT_RUN]
    return (WAITING, waiting) if waiting else (UNKNOWN, [])


def _rate(k: int, n: int) -> dict[str, Any]:
    """`{k, n, rate, ci}`; rate and ci are None when n = 0, never 0/0."""
    r = rates.rate(k, n)
    if r is None:
        return {"k": k, "n": n, "rate": None, "ci": None}
    return {"k": k, "n": n, "rate": round(r.p, 4), "ci": [round(r.lo, 4), round(r.hi, 4)]}


def episode_record(result: Mapping[str, Any],
                   quality: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the block needs from one episode: its arm, whether the board excludes it,
    the seeded defects present (from `result.json`; `quality.json` does not copy them)
    and its quality block (None: the episode has none)."""
    metrics = result.get("metrics") or {}
    model = str(result.get("model") or "")
    return {
        "agent": str(result.get("agent") or ""),
        # The board's own model name: the routing prefix dropped.
        "model": model.rsplit("/", 1)[-1] if "/" in model else model,
        "condition": str(result.get("condition") or ""),
        "heldout": bool(metrics.get("heldout")),
        "excluded": is_excluded(metrics),
        "bugs_present": [str(b) for b in metrics.get("bugs_present") or []],
        # Which case the defects are seeded on: an exclusion from replay is a fact
        # about (case, defect), whichever arm's report brought it to light.
        "case": f"{metrics.get('app_id') or ''}/{metrics.get('case_id') or ''}",
        "quality": quality,
    }


def _funnel(reports: list[Mapping[str, Any]], names: Mapping[str, str],
            missing: set[str]) -> list[dict[str, Any]]:
    """The funnel over the headline gates, in order. Each stage takes the reports that
    passed every earlier gate and says what its gate did with them. A stage whose gate
    has not run is not measured, and neither is any stage after it."""
    stages: list[dict[str, Any]] = []
    entered: list[Mapping[str, Any]] | None = list(reports)
    for gate in rq.HEADLINE_GATES:
        stage: dict[str, Any] = {"gate": gate, "name": names[gate]}
        if entered is None or gate in missing:
            stage.update(measured=False, entered=None if entered is None else len(entered),
                         passed=None, failed=None, unknown=None, not_run=None)
            entered = None
        else:
            answers = [_answer(r, gate) for r in entered]
            stage.update(measured=True, entered=len(entered),
                         **{key: answers.count(a) for key, a in (
                             ("passed", PASS), ("failed", FAIL), ("unknown", UNKNOWN),
                             ("not_run", NOT_RUN))})
            entered = [r for r, a in zip(entered, answers) if a == PASS]
        stages.append(stage)
    return stages


def _counts(reports: list[Mapping[str, Any]], names: Mapping[str, str],
            missing: set[str]) -> dict[str, Any]:
    """The per-report figures of one group of scored (non-neutral) reports: each gate's
    rate, the customer-ready counts and the funnel."""
    gates = {}
    for gate in rq.HEADLINE_GATES:
        answers = [_answer(r, gate) for r in reports]
        passed, failed = answers.count(PASS), answers.count(FAIL)
        gates[gate] = {"name": names[gate], "available": gate not in missing,
                       "evaluated": passed + failed, "passed": passed,
                       "unknown": answers.count(UNKNOWN), "not_run": answers.count(NOT_RUN),
                       **{k: v for k, v in _rate(passed, passed + failed).items()
                          if k in ("rate", "ci")}}
    states = [_state(r, rq.HEADLINE_GATES)[0] for r in reports]
    measured = not missing
    ready, not_ready = states.count(READY), states.count(NOT_READY)
    decided = _rate(ready, ready + not_ready)
    customer_ready = {
        "measured": measured, "ready": ready, "not_ready": not_ready,
        "unknown": states.count(UNKNOWN), "waiting": states.count(WAITING),
        "k": ready, "n": ready + not_ready,
        "rate": decided["rate"] if measured else None,
        "ci": decided["ci"] if measured else None,
        "unknown_share": (round(states.count(UNKNOWN) / len(reports), 4)
                          if reports else None)}
    carried = sum(1 for r in reports
                  if (r.get("complete") or {}).get("complete_if_steps_carried"))
    return {"scored": len(reports), "gates": gates, "customer_ready": customer_ready,
            # A side figure, never a gate: G3 if the bug carried the agent's step list.
            "complete_if_steps_carried": _rate(carried, len(reports)),
            "funnel": _funnel(reports, names, missing)}


def _defect_yield(defects: list[tuple[int, str]],
                  by_episode: Mapping[int, list[Mapping[str, Any]]],
                  gates: Iterable[str]) -> dict[str, Any]:
    """The yield over `gates`, under the same rule for unknown as the rate.

    Each seeded defect present is one of three things: COVERED (a report of it passes
    every gate), UNKNOWN (none does, and one or more of its reports failed nothing and
    is left undecided by an unknown answer) or MISSED (no report matched it, or every
    report of it failed a gate). An unknown defect is in neither side, exactly as an
    unknown report is in neither side of the rate: `n` counts the decided defects, and
    `present` all of them. `{k, n, rate, ci, present, unknown}`."""
    gates = list(gates)
    covered = unknown = 0
    for ep, defect in defects:
        states = [_state(r, gates)[0] for r in by_episode.get(ep, ())
                  if r.get("matched") == defect]
        if READY in states:
            covered += 1
        elif UNKNOWN in states or WAITING in states:
            unknown += 1
    return {**_rate(covered, len(defects) - unknown),
            "present": len(defects), "unknown": unknown}


def _coverable(counted: list[Mapping[str, Any]],
               defects: list[tuple[int, str]]) -> dict[str, Any]:
    """The yield's ceiling: how many of the seeded defects present could be covered at
    all. A defect cannot when G2 can never locate it (the truth holds no screen for
    it) or when replay excluded it from G4; `quality.json` says which
    (`defects.<id>`). An exclusion from replay is a fact about the (case, defect), so
    it counts for every counted episode of that case, whichever one's report brought it
    to light. `coverable` is None when a counted episode's file has no `defects` map
    (written before the map existed): the ceiling is then not known."""
    maps = [rec["quality"].get("defects") for rec in counted]
    if not all(isinstance(m, Mapping) for m in maps):
        return {"coverable": None, "not_coverable": None, "not_coverable_by": {}}
    excluded = {(rec.get("case"), defect): str(entry["replay_excluded"])
                for rec, m in zip(counted, maps) for defect, entry in m.items()
                if isinstance(entry, Mapping) and entry.get("replay_excluded")}
    by: dict[str, int] = {}
    for ep, defect in defects:
        entry = maps[ep].get(defect)
        entry = entry if isinstance(entry, Mapping) else {}
        why = entry.get("located_never") or (
            "replay:" + excluded[(counted[ep].get("case"), defect)]
            if (counted[ep].get("case"), defect) in excluded else None)
        if why:
            by[str(why)] = by.get(str(why), 0) + 1
    lost = sum(by.values())
    return {"coverable": len(defects) - lost, "not_coverable": lost,
            "not_coverable_by": dict(sorted(by.items()))}


def _available_block(reports: list[Mapping[str, Any]], available: list[str],
                     defects: list[tuple[int, str]],
                     by_episode: Mapping[int, list[Mapping[str, Any]]]) -> dict[str, Any]:
    """The same two figures over the gates that DID run: reports passing every available
    headline gate, and defects with such a report. Equal to the customer-ready figures
    when every headline gate is available; an upper bound on them otherwise."""
    states = [_state(r, available)[0] for r in reports]
    ready, not_ready = states.count(READY), states.count(NOT_READY)
    return {"gates": list(available), **_rate(ready, ready + not_ready),
            "unknown": states.count(UNKNOWN),
            "yield": _defect_yield(defects, by_episode, available)}


def _precision_coverage(reports: list[Mapping[str, Any]], measured: bool) -> dict[str, Any]:
    """Precision against coverage for one signal: how many of the deterministic gates
    (`SIGNAL_GATES`) a report passed. For each cut "at least N gates": how many reports
    it keeps, what share that is, how many of the kept are valid (G1), and how many are
    customer-ready (None while a headline gate has not run)."""
    signal = [sum(_answer(r, g) == PASS for g in SIGNAL_GATES) for r in reports]
    points = []
    for cut in range(len(SIGNAL_GATES), -1, -1):
        kept = [r for r, s in zip(reports, signal) if s >= cut]
        states = [_state(r, rq.HEADLINE_GATES)[0] for r in kept]
        ready, not_ready = states.count(READY), states.count(NOT_READY)
        points.append({
            "min_gates": cut, "kept": len(kept),
            "coverage": round(len(kept) / len(reports), 4) if reports else None,
            "valid": _rate(sum(_answer(r, "G1") == PASS for r in kept), len(kept)),
            "customer_ready": _rate(ready, ready + not_ready) if measured else None})
    return {"signal": "gates_passed", "signal_gates": list(SIGNAL_GATES), "points": points}


def _row(key: tuple, records: list[Mapping[str, Any]]) -> dict[str, Any]:
    counted = [r for r in records if not r["excluded"] and r.get("quality") is not None]
    names = dict(GATE_NAMES)
    registered: set[str] | None = None
    versions: set[str] = set()
    scored: list[Mapping[str, Any]] = []
    by_episode: dict[int, list[Mapping[str, Any]]] = {}
    defects: list[tuple[int, str]] = []
    n_reports = n_neutral = 0
    for i, rec in enumerate(counted):
        quality = rec["quality"]
        versions.add(str(quality.get("quality_version")))
        totals = (quality.get("totals") or {}).get("gates") or {}
        for gate, entry in totals.items():
            if gate in names and isinstance(entry, Mapping) and entry.get("name"):
                names[gate] = str(entry["name"])
        registered = set(totals) if registered is None else registered & set(totals)
        for report in quality.get("reports") or []:
            n_reports += 1
            if report.get("neutral"):
                n_neutral += 1
                continue
            scored.append(report)
            by_episode.setdefault(i, []).append(report)
        defects += [(i, d) for d in rec["bugs_present"]]
    registered = registered or set()
    # A headline gate is missing when a counted episode's file does not have it, or when
    # a report that failed nothing still waits for it. Either way no rate that needs
    # every gate can be given: the report is not ready and not failed.
    waited_on = {g for r in scored for g in _state(r, rq.HEADLINE_GATES)[1]}
    missing = {g for g in rq.HEADLINE_GATES if g not in registered} | waited_on
    available = [g for g in rq.HEADLINE_GATES if g not in missing]

    row: dict[str, Any] = {
        "agent": key[0], "model": key[1], "condition": key[2], "heldout": key[3],
        "quality_versions": sorted(versions),
        "episodes": len(counted),
        "episodes_excluded": sum(1 for r in records if r["excluded"]),
        "episodes_without_quality": sum(1 for r in records
                                        if not r["excluded"] and r.get("quality") is None),
        "reports": n_reports, "neutral": n_neutral,
        "gates_available": available,
        "gates_missing": [g for g in rq.HEADLINE_GATES if g in missing],
    }
    row.update(_counts(scored, names, missing))
    measured = not missing
    in_yield = _defect_yield(defects, by_episode, rq.HEADLINE_GATES)
    row["customer_ready_yield"] = {
        **in_yield, "measured": measured,
        "rate": in_yield["rate"] if measured else None,
        "ci": in_yield["ci"] if measured else None,
        **_coverable(counted, defects)}
    row["ready_on_available"] = _available_block(scored, available, defects, by_episode)
    matched = sum(1 for r in scored if r.get("matched"))
    found = len({(ep, r["matched"]) for ep, reports in by_episode.items()
                 for r in reports if r.get("matched")})
    row["reports_per_defect"] = {
        "matched_reports": matched, "defects_found": found,
        "value": round(matched / found, 2) if found else None,
        # The share of matched reports that repeat a defect already reported.
        "repeats": _rate(matched - found, matched)}
    row["precision_coverage"] = _precision_coverage(scored, measured)
    sources: dict[str, list[Mapping[str, Any]]] = {}
    for report in scored:
        source = str((report.get("customer_report") or {}).get("report_source") or "unknown")
        sources.setdefault(source, []).append(report)
    row["by_report_source"] = {s: _counts(rs, names, missing)
                               for s, rs in sorted(sources.items())}
    judged = _steps_accuracy(counted)
    if judged is not None:
        row["steps_accuracy"] = judged
    return row


def _steps_accuracy(counted: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The G5 side column of one arm (QUA-3593), or None when no counted episode was
    scored with `--quality-judge`. It is a judged figure: it is in no gate, no funnel
    and no customer-ready count above, the text and HTML blocks do not print it, and a
    reader shows it only when `judge_accepted` is true. `evaluated` is the reports a
    saved judge answer exists for; `model` is the judge model, None when the episodes
    name more than one (they are then listed in `models`)."""
    totals = [t for t in ((rec["quality"].get("totals") or {}).get("steps_accuracy")
                          for rec in counted) if isinstance(t, Mapping)]
    if not totals:
        return None
    passed = sum(int(t.get("passed") or 0) for t in totals)
    evaluated = sum(int(t.get("evaluated") or 0) for t in totals)
    models = sorted({str(t["model"]) for t in totals if t.get("model")})
    rate = _rate(passed, evaluated)
    return {"by": "judge",
            "judge_accepted": all(t.get("judge_accepted") is True for t in totals),
            "passed": passed, "evaluated": evaluated,
            "rate": rate["rate"], "ci": rate["ci"],
            "applies": sum(int(t.get("applies") or 0) for t in totals),
            "not_judged": sum(int(t.get("not_judged") or 0) for t in totals),
            "model": models[0] if len(models) == 1 else None, "models": models}


def block(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The Report quality block as data: one row per arm, in name order (public rows,
    then held-out rows). `records` are `episode_record` dicts. Pure: the same records
    give the same block."""
    groups: dict[tuple, list[Mapping[str, Any]]] = {}
    for rec in records:
        key = (rec["agent"], rec["model"], rec["condition"], bool(rec["heldout"]))
        groups.setdefault(key, []).append(rec)
    rows = [_row(key, groups[key])
            for key in sorted(groups, key=lambda k: (k[3], k[0], k[1], k[2]))]
    return {"format": BLOCK_FORMAT, "headline_gates": list(rq.HEADLINE_GATES),
            "signal_gates": list(SIGNAL_GATES), "rows": rows}


def has_quality(records: Iterable[Mapping[str, Any]]) -> bool:
    return any(r.get("quality") is not None for r in records)


# ── text ───────────────────────────────────────────────────────────────────────

def _fmt(r: Mapping[str, Any] | None) -> str:
    """`4/5 80% [38–96]`; `0/0 —` when nothing is in the denominator."""
    if r is None:
        return "not measured"
    if r.get("rate") is None:
        return f"{r.get('k', 0)}/{r.get('n', 0)} —"
    return rates.fmt_pct_ci(r["rate"], r["ci"], r["k"], r["n"])


def _gate_list(row: Mapping[str, Any], gates: Iterable[str]) -> str:
    return ", ".join(f"{g} {row['gates'][g]['name']}" for g in gates) or "none"


def _who(row: Mapping[str, Any]) -> str:
    who = f"{row['agent']} · {row['model']} · {row['condition']}"
    return who + (" · held-out" if row.get("heldout") else "")


def _gate_cell(g: Mapping[str, Any]) -> str:
    if not g["available"] and not g["evaluated"] and not g["unknown"]:
        return "not measured"
    cell = _fmt({"k": g["passed"], "n": g["evaluated"], "rate": g["rate"], "ci": g["ci"]})
    if g["unknown"]:
        reached = g["evaluated"] + g["unknown"]
        cell += f" · {g['unknown']} unknown ({g['unknown'] / reached:.0%} of {reached} answered)"
    return cell


def _ready_cell(row: Mapping[str, Any], cr: Mapping[str, Any]) -> str:
    if not cr["measured"]:
        why = _gate_list(row, row["gates_missing"])
        return (f"not measured ({why} has not run) · {cr['not_ready']} of {row['scored']} "
                f"already failed a gate, {cr['waiting']} wait")
    cell = _fmt(cr)
    if cr["unknown"]:
        cell += f" · {cr['unknown']} unknown ({cr['unknown_share']:.0%} of scored)"
    return cell


def _yield_note(cry: Mapping[str, Any]) -> str:
    """What stands beside the yield: the defects present, the ones left unknown, and
    the ceiling (how many could be covered at all)."""
    note = f" · seeded defects present: {cry.get('present', cry.get('n'))}"
    if cry.get("unknown"):
        note += f", {cry['unknown']} unknown (in neither side)"
    if cry.get("coverable") is not None:
        note += f" · could be covered at all: {cry['coverable']}"
        if cry.get("not_coverable_by"):
            note += " (not: " + ", ".join(
                f"{n} {why}" for why, n in cry["not_coverable_by"].items()) + ")"
    return note


def _funnel_text(scored: int, funnel: list[Mapping[str, Any]]) -> str:
    parts = [f"{scored} report{'' if scored == 1 else 's'}"]
    for stage in funnel:
        if not stage["measured"]:
            parts.append(f"{stage['name']}: not measured")
            continue
        lost = [f"{stage[k]} {label}" for k, label in (
            ("failed", "failed"), ("unknown", "unknown"), ("not_run", "not run"))
            if stage[k]]
        parts.append(f"{stage['passed']} {stage['name']}"
                     + (f" ({', '.join(lost)})" if lost else ""))
    return " → ".join(parts)


def lines(block: Mapping[str, Any]) -> list[str]:
    """The block as plain text, one indented paragraph per arm."""
    out = [f"{TITLE} — {CAPTION}"]
    if not block["rows"]:
        out.append("  no episode with a quality block")
    for row in block["rows"]:
        version = ", ".join(row["quality_versions"]) or "—"
        mixed = " (MIXED: not comparable)" if len(row["quality_versions"]) > 1 else ""
        out.append(f"  {_who(row)} · quality v{version}{mixed}")
        skipped = [f"{row[k]} {label}" for k, label in (
            ("episodes_excluded", "excluded"),
            ("episodes_without_quality", "without a quality block")) if row[k]]
        out.append(f"    {row['episodes']} episodes"
                   + (f" ({', '.join(skipped)} left out)" if skipped else "")
                   + f" · {row['reports']} reports: {row['scored']} scored, "
                     f"{row['neutral']} neutral")
        out.append(f"    gates available: {_gate_list(row, row['gates_available'])}"
                   + (f" · not measured: {_gate_list(row, row['gates_missing'])}"
                      if row["gates_missing"] else ""))
        out.append(f"    customer-ready rate   {_ready_cell(row, row['customer_ready'])}")
        cry = row["customer_ready_yield"]
        out.append("    customer-ready yield  "
                   + (_fmt(cry) if cry["measured"] else "not measured")
                   + _yield_note(cry))
        if row["gates_missing"] and row["gates_available"]:
            roa = row["ready_on_available"]
            name = "+".join(roa["gates"])
            out.append(f"    passing {name} only    {_fmt(roa)}"
                       + (f" · {roa['unknown']} unknown" if roa["unknown"] else "")
                       + f" · defects with such a report {_fmt(roa['yield'])}"
                       + (f" ({roa['yield']['unknown']} unknown)"
                          if roa["yield"].get("unknown") else ""))
        for gate in block["headline_gates"]:
            g = row["gates"][gate]
            out.append(f"    {gate} {g['name']:<13} {_gate_cell(g)}")
        out.append("    G3 if the agent's steps were carried (side figure)  "
                   + _fmt(row["complete_if_steps_carried"]))
        rpd = row["reports_per_defect"]
        out.append("    reports per defect    "
                   + (f"{rpd['matched_reports']}/{rpd['defects_found']} = {rpd['value']:.2f}"
                      f" · repeats {_fmt(rpd['repeats'])}" if rpd["defects_found"] else "—"))
        out.append(f"    funnel   {_funnel_text(row['scored'], row['funnel'])}")
        for source, part in row["by_report_source"].items():
            cells = " · ".join(f"{g} {_gate_cell(part['gates'][g])}"
                               for g in block["headline_gates"]
                               if part["gates"][g]["available"])
            out.append(f"      {source} ({part['scored']}): {cells}")
            out.append(f"        funnel {_funnel_text(part['scored'], part['funnel'])}")
        out.append("    precision against coverage (signal: how many of "
                   f"{' and '.join(block['signal_gates'])} a report passed)")
        for p in row["precision_coverage"]["points"]:
            share = "—" if p["coverage"] is None else f"{p['coverage']:.0%}"
            out.append(f"      at least {p['min_gates']}: keeps {p['kept']}/{row['scored']} "
                       f"({share}) · valid {_fmt(p['valid'])} · customer-ready "
                       f"{_fmt(p['customer_ready'])}")
    out.append("  " + LEGEND)
    return out


# ── HTML (the local view) ──────────────────────────────────────────────────────

E = html.escape


def _table(heads: list[str], rows: list[list[str]]) -> str:
    return ("<div class=tablewrap><table class=board><tr>"
            + "".join(f"<th>{E(h)}</th>" for h in heads) + "</tr>"
            + "".join("<tr>" + "".join(f"<td>{E(c)}</td>" for c in r) + "</tr>" for r in rows)
            + "</table></div>")


def block_html(block: Mapping[str, Any], run: str = "") -> str:
    """The board page's Report quality section, one `<section>`; "" when no row has a
    report quality block, so a view of episodes without one is what it was. `run` names
    the run in the heading when the page holds several."""
    rows = [r for r in block["rows"] if r["episodes"]]
    if not rows:
        return ""
    gates = block["headline_gates"]
    out = ['<section id="report-quality">',
           f"<h3>{E(TITLE)}{E(f' · run {run}' if run else '')}</h3>",
           f'<p class="dim">{E(CAPTION)}</p>']
    out.append(_table(
        ["arm", "reports (scored / neutral)", "gates available", "customer-ready rate",
         "customer-ready yield", *(f"{g} {GATE_NAMES.get(g, g)}" for g in gates),
         "reports per defect"],
        [[_who(r), f"{r['scored']} / {r['neutral']}",
          " ".join(r["gates_available"]) or "none",
          _ready_cell(r, r["customer_ready"]),
          _fmt(r["customer_ready_yield"]) if r["customer_ready_yield"]["measured"]
          else "not measured",
          *(_gate_cell(r["gates"][g]) for g in gates),
          (f"{r['reports_per_defect']['value']:.2f}"
           if r["reports_per_defect"]["defects_found"] else "—")] for r in rows]))
    funnel_rows = []
    for r in rows:
        parts = [("all sources", r), *r["by_report_source"].items()]
        for source, part in parts:
            funnel_rows.append([_who(r), source, str(part["scored"]), *(
                "not measured" if not s["measured"] else
                f"{s['passed']} (−{s['failed']} failed"
                + (f", {s['unknown']} unknown" if s["unknown"] else "")
                + (f", {s['not_run']} not run" if s["not_run"] else "") + ")"
                for s in part["funnel"])])
    out.append("<h4>Where reports are lost</h4>")
    out.append(_table(["arm", "report source", "reports",
                       *(GATE_NAMES.get(g, g) for g in gates)], funnel_rows))
    out.append(f'<p class="dim">{E(LEGEND)}</p>')
    out.append("</section>")
    return "\n".join(out)


def episode_html(quality: Mapping[str, Any] | None) -> str:
    """The episode page's per-gate results: one row per report, one cell per gate with
    its reason. "" for an episode with no quality block."""
    if not quality:
        return ""
    gates = list((quality.get("totals") or {}).get("gates") or {})
    names = {g: (quality["totals"]["gates"][g] or {}).get("name") or g for g in gates}
    rows = []
    for report in quality.get("reports") or []:
        source = (report.get("customer_report") or {}).get("report_source") or ""
        if report.get("neutral"):
            rows.append([str(report.get("index")), source, "neutral: in no gate",
                         *("" for _ in gates)])
            continue
        ready = report.get("customer_ready")
        cells = []
        for gate in gates:
            answer = _answer(report, gate)
            reason = ((report.get("gates") or {}).get(gate) or {}).get("reason") or ""
            cells.append(f"{answer.replace('_', ' ')}: {reason}" if reason else answer)
        rows.append([str(report.get("index")), source,
                     "yes" if ready is True else "no" if ready is False else "not decided",
                     *cells])
    head = (f'<h2 id="report-quality">Report quality (quality v'
            f'{E(str(quality.get("quality_version")))})</h2>')
    if not rows:
        return head + '<p class="dim">(no report)</p>'
    return head + _table(["report", "source", "customer-ready",
                          *(f"{g} {names[g]}" for g in gates)], rows)


# ── reading a runs tree ────────────────────────────────────────────────────────

def collect(root: Path | str, saved: bool = False,
            run_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """One `episode_record` per journey episode under `root` (only those of `run_ids`,
    when given: a runs directory holds every run's episodes side by side). The quality
    block is scored in memory from the saved episode (nothing is written); with `saved`
    it is read from the episode's `quality.json` instead, and an episode without that
    file has none."""
    wanted = set(run_ids or ())
    records = []
    for ep in rq.episode_dirs(root):
        try:
            episode = rq.load_episode(ep)
        except rq.QualityError:
            continue
        if wanted and episode.result.get("run_id") not in wanted:
            continue
        quality: dict[str, Any] | None
        if saved:
            try:
                quality = json.loads((ep / rq.QUALITY_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                quality = None
        else:
            quality = rq.score_episode(ep)
        records.append(episode_record(episode.result, quality))
    return records


def result_record(result: Any, quality: Mapping[str, Any] | None) -> dict[str, Any]:
    """`episode_record` for a parsed `RunResult`."""
    return episode_record({"agent": result.agent, "model": result.model,
                           "condition": result.condition,
                           "metrics": result.metrics or {}}, quality)


def saved_records(runs_dir: Path | str, results: Iterable[Any]) -> list[dict[str, Any]]:
    """One record per journey result, each with the `quality.json` saved in its episode
    directory (None when there is none: not scored yet, or the artifacts are not on
    this machine). Reads only."""
    from .result import resolve_artifact_dir
    records = []
    for r in results:
        d = resolve_artifact_dir(runs_dir, r)
        quality = None
        if d is not None:
            try:
                quality = json.loads((d / rq.QUALITY_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                quality = None
        records.append(result_record(r, quality if isinstance(quality, dict) else None))
    return records


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m qualgentbench.quality_rates",
        description="Print the Report quality block for the journey episodes under a "
                    "run directory. Reads saved episodes only and writes nothing.")
    ap.add_argument("run_dir", type=Path,
                    help="a runs directory, one run's directory, or one episode directory")
    ap.add_argument("--run", action="append", default=[], metavar="RUN_ID",
                    help="count only this run's episodes (repeatable)")
    ap.add_argument("--saved", action="store_true",
                    help="read each episode's quality.json instead of scoring it in memory")
    ap.add_argument("--json", action="store_true", help="print the block as JSON")
    args = ap.parse_args(argv)
    if not args.run_dir.is_dir():
        print(f"error: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    records = collect(args.run_dir, saved=args.saved, run_ids=args.run)
    if not records:
        print(f"error: no journey episode found under {args.run_dir}", file=sys.stderr)
        return 1
    data = block(records)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print("\n".join(lines(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
