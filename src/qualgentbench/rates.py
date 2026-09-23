"""Rates for the journey board — pure functions, every denominator named.

The board's F1 blends two things a reader wants apart: how often the agent cries wolf
on a build with nothing wrong, and how much of what IS wrong it catches. These are the
two numbers a QA team actually budgets against (a nightly suite of clean cases has to
come back clean most nights; a seeded defect has to be caught most of the time), so
they are published on their own, each with a Wilson interval, plus two compositions:

  clean-run integrity   P(no false alarm across N clean cases) = (1 - p)^N
  projection            expected false alarms and expected misses for a suite the
                        reader picks (N clean cases, M seeded defects)

Blocker recall — recall restricted to FUNCTIONAL defects in the top tiers — is the one
severity-aware number here. The tier weights 1/3/6/10 in `bugs.py` are a house
convention, not derived from any published severity scale, so the journey board does
not weight by them; it reports blocker recall beside plain recall instead.

Denominators (this is where these numbers lie, so they are spelled out per function):

  false_alarm_rate   clean-arm EPISODES with >= 1 false report / clean-arm episodes
  catch_rate         seeded DEFECTS found / seeded defects present (per defect, not
                     per episode — a case with a functional bug and two display bugs
                     contributes three)
  blocker_recall     the same, restricted to functional defects whose tier is in
                     `tiers`

Every function takes the per-episode metrics dicts that `journey.journey_verdict`
writes (`version`, `bugs_present`, `bugs_found`, `false_reports`, `app_id`), already
filtered of excluded episodes — the caller (`journey._row`) has done that filtering,
and nothing here re-applies it. Trials are counted as they are: two trials of one case
are two episodes / two copies of each defect, so the interval treats them as
independent draws, which they are not quite. Statistical power comes from DISTINCT
cases; a board over 5 cases x 3 trials is narrower on paper than in truth.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

Z95 = 1.96


class Rate(NamedTuple):
    """A binomial rate with its Wilson interval. `k` of `n`; `p = k / n`."""
    k: int
    n: int
    p: float
    lo: float
    hi: float

    def as_fields(self, prefix: str, ndigits: int = 4) -> dict[str, Any]:
        """`{prefix}_rate`, `{prefix}_ci`, `{prefix}_k`, `{prefix}_n` — the board shape."""
        return {f"{prefix}_rate": round(self.p, ndigits),
                f"{prefix}_ci": [round(self.lo, ndigits), round(self.hi, ndigits)],
                f"{prefix}_k": self.k, f"{prefix}_n": self.n}


def empty_fields(prefix: str) -> dict[str, Any]:
    """The same keys as `Rate.as_fields` for an undefined rate (n = 0): None, never 0/0."""
    return {f"{prefix}_rate": None, f"{prefix}_ci": None, f"{prefix}_k": 0, f"{prefix}_n": 0}


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for `k` successes in `n` trials.

    Preferred over the normal approximation because it behaves at the edges the board
    lives on: k = 0 gives (0, something), never (0, 0), and a rate near 1 does not
    overshoot. Raises on n <= 0 — an undefined rate is the CALLER's None, not a
    (0, 1) interval that reads as a measurement.
    """
    if n <= 0:
        raise ValueError("wilson: n must be positive (an undefined rate is None upstream)")
    if not 0 <= k <= n:
        raise ValueError(f"wilson: k={k} outside 0..n={n}")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate(k: int, n: int, z: float = Z95) -> Rate | None:
    """`Rate` for k of n, or None when n = 0."""
    if n <= 0:
        return None
    lo, hi = wilson(k, n, z)
    return Rate(k=k, n=n, p=k / n, lo=lo, hi=hi)


def _episodes(rows: Iterable[Mapping[str, Any]], version: str) -> list[Mapping[str, Any]]:
    return [r for r in rows if r.get("version") == version]


def false_alarm_rate(rows: Iterable[Mapping[str, Any]], z: float = Z95) -> Rate | None:
    """False-alarm rate PER CLEAN CASE (episode).

    numerator    clean-arm episodes with `false_reports` >= 1
    denominator  clean-arm episodes (every non-excluded one — completion-unscored and
                 truncated episodes included, because bug finding is scored on them:
                 an agent that runs out of budget and files a report anyway has still
                 cried wolf)

    Per EPISODE, not per report: three false reports on one clean build are one dirty
    night for the suite, not three. Seeded-arm false reports are precision's problem
    and are not in this number — a false report on a seeded build cannot be told
    apart from a mis-described sighting of the real bug with the same confidence.
    None when there are no clean episodes.
    """
    clean = _episodes(rows, "clean")
    k = sum(1 for r in clean if (r.get("false_reports") or 0) >= 1)
    return rate(k, len(clean), z)


def catch_rate(rows: Iterable[Mapping[str, Any]], z: float = Z95) -> Rate | None:
    """Catch rate PER SEEDED DEFECT.

    numerator    sum of len(bugs_found) over seeded-arm episodes
    denominator  sum of len(bugs_present) over seeded-arm episodes

    Per DEFECT, not per episode: a case seeded with one functional bug and two display
    bugs contributes three to the denominator, and catching two of them is 2/3, not a
    failed episode. `bugs_found` is already de-duplicated per episode by the scorer
    (two reports of one bug count once). The same defect on two trials of a case
    counts twice. This is the board's `recall` with an interval; the name says what
    the denominator is. None when no seeded defect was present.
    """
    seeded = _episodes(rows, "seeded")
    present = sum(len(r.get("bugs_present") or []) for r in seeded)
    found = sum(len(r.get("bugs_found") or []) for r in seeded)
    return rate(found, present, z)


DefectLookup = Callable[[str | None, str], Mapping[str, Any] | None]
"""(app_id, defect_id) -> {"kind": ..., "tier": ...} or None when unknown."""


def blocker_recall(rows: Iterable[Mapping[str, Any]], tiers: tuple[str, ...] = ("L4", "L3"),
                   defects: DefectLookup | None = None, z: float = Z95) -> Rate | None:
    """Recall restricted to BLOCKING defects: kind = functional, tier in `tiers`.

    numerator    seeded defects found whose defect is functional and in `tiers`
    denominator  seeded defects present whose defect is functional and in `tiers`

    Display defects never enter either side (a wrong label does not block a release),
    and neither does a functional defect below the cut or one whose tier the lookup
    cannot resolve — an unknown defect is dropped, not guessed into the top tier.
    `defects(app_id, defect_id)` resolves kind and tier; without a lookup the rows
    themselves must carry a `defects` mapping (id -> {kind, tier}) or nothing
    qualifies. Tier comparison is case-insensitive. None when n = 0: a board with no
    top-tier functional defect on it has no blocker recall, not a blocker recall of 0.
    """
    wanted = {t.upper() for t in tiers}

    def meta(row: Mapping[str, Any], bug_id: str) -> Mapping[str, Any] | None:
        got = (row.get("defects") or {}).get(bug_id)
        if got is None and defects is not None:
            got = defects(row.get("app_id"), bug_id)
        return got

    def qualifies(row: Mapping[str, Any], bug_id: str) -> bool:
        d = meta(row, bug_id)
        if not d:
            return False
        return (str(d.get("kind") or "").lower() == "functional"
                and str(d.get("tier") or "").upper() in wanted)

    n = k = 0
    for r in _episodes(rows, "seeded"):
        present = [b for b in (r.get("bugs_present") or []) if qualifies(r, str(b))]
        found = {str(b) for b in (r.get("bugs_found") or [])}
        n += len(present)
        k += sum(1 for b in present if str(b) in found)
    return rate(k, n, z)


def clean_run_integrity(p: float, n_cases: int,
                        ci: tuple[float, float] | None = None
                        ) -> tuple[float, tuple[float, float] | None]:
    """P(no false alarm across `n_cases` clean cases) = (1 - p)^n_cases.

    `p` is the per-case false-alarm rate. The interval is the p interval pushed
    through the same monotone function — the high false-alarm bound gives the low
    integrity bound: ((1 - hi)^n, (1 - lo)^n). Independence between cases is assumed;
    an agent whose false alarms cluster on one screen will do better than this on a
    good night and worse on a bad one. A 1% rate over 200 cases is 0.99^200 = 13%
    clean nights, which is the arithmetic behind the single-digit target.
    """
    if not 0 <= p <= 1:
        raise ValueError(f"clean_run_integrity: p={p} outside 0..1")
    if n_cases < 0:
        raise ValueError("clean_run_integrity: n_cases must be >= 0")
    point = (1 - p) ** n_cases
    if ci is None:
        return point, None
    lo, hi = ci
    return point, ((1 - hi) ** n_cases, (1 - lo) ** n_cases)


def projection(fa_rate: Rate | float | None, catch: Rate | float | None,
               n_clean: int, n_seeded: int) -> dict[str, Any]:
    """What a suite of `n_clean` clean cases and `n_seeded` seeded defects would see.

      expected_false_alarms  = fa_rate x n_clean         (clean cases that cry wolf)
      expected_misses        = (1 - catch) x n_seeded    (seeded defects not reported)
      clean_run_integrity    = (1 - fa_rate)^n_clean
      expected_errors        = expected_false_alarms + expected_misses   (the
                               prior-weighted cost line; a point, never a ranking key)

    Each comes with a `_ci` when the input rate carries an interval (pass a `Rate`);
    a bare float gives points only; a None rate leaves its numbers None. Linear
    extrapolation from the measured rate — it assumes the suite the reader picks looks
    like the cases the rate was measured on, which no interval can guarantee.
    """
    if n_clean < 0 or n_seeded < 0:
        raise ValueError("projection: suite sizes must be >= 0")
    out: dict[str, Any] = {"n_clean": n_clean, "n_seeded": n_seeded,
                           "expected_false_alarms": None, "expected_false_alarms_ci": None,
                           "clean_run_integrity": None, "clean_run_integrity_ci": None,
                           "expected_misses": None, "expected_misses_ci": None,
                           "expected_errors": None}
    if fa_rate is not None:
        p = fa_rate.p if isinstance(fa_rate, Rate) else float(fa_rate)
        ci = (fa_rate.lo, fa_rate.hi) if isinstance(fa_rate, Rate) else None
        out["expected_false_alarms"] = p * n_clean
        integrity, integrity_ci = clean_run_integrity(p, n_clean, ci)
        out["clean_run_integrity"] = integrity
        if ci is not None:
            out["expected_false_alarms_ci"] = (ci[0] * n_clean, ci[1] * n_clean)
            out["clean_run_integrity_ci"] = integrity_ci
    if catch is not None:
        c = catch.p if isinstance(catch, Rate) else float(catch)
        out["expected_misses"] = (1 - c) * n_seeded
        if isinstance(catch, Rate):
            # High catch bound -> low miss bound.
            out["expected_misses_ci"] = ((1 - catch.hi) * n_seeded, (1 - catch.lo) * n_seeded)
    # Prior-weighted cost of the suite in wrong answers: every false alarm and every miss
    # is one. A point only — the two intervals are not independent draws of one
    # quantity, and adding their bounds would print a bracket nobody measured. None
    # unless BOTH rates are defined: a suite with seeded defects and no catch rate has
    # an unknown miss count, not zero misses.
    if out["expected_false_alarms"] is not None and out["expected_misses"] is not None:
        out["expected_errors"] = out["expected_false_alarms"] + out["expected_misses"]
    return out


def bug_prior(n_clean: int, n_seeded: int) -> str:
    """The share of a suite that is seeded, as the projection line prints it: `20%`.
    The board's own F1 is measured at 50% (every case has a clean and a seeded arm),
    which is why the projection, not F1, is the number to read at a production prior."""
    total = n_clean + n_seeded
    return "—" if total <= 0 else f"{n_seeded / total * 100:.0f}%"


# ── formatting shared by the console table and the rescore script ──────────────

def fmt_pct_ci(p: float | None, ci: Iterable[float] | None, k: int | None = None,
               n: int | None = None) -> str:
    """`80% [38–96]`, with `4/5 ` in front when k and n are given; `—` for None."""
    if p is None:
        return "—"
    body = f"{p * 100:.0f}%"
    if ci is not None:
        lo, hi = tuple(ci)
        body += f" [{lo * 100:.0f}–{hi * 100:.0f}]"
    if k is not None and n is not None:
        body = f"{k}/{n} {body}"
    return body
