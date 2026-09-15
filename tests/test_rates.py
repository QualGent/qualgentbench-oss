"""Rates for the journey board: Wilson intervals, the per-case and per-defect
denominators, clean-run integrity, the composed projection, blocker recall."""

from __future__ import annotations

import pytest

from qualgentbench import rates

# ── Wilson ─────────────────────────────────────────────────────────────────────

def test_wilson_matches_known_values():
    # 0 of 10: the interval must not collapse to (0, 0) — that is the whole reason
    # Wilson is used over the normal approximation.
    lo, hi = rates.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    # 5 of 10: symmetric about 0.5.
    lo, hi = rates.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    # 10 of 10 mirrors 0 of 10.
    lo, hi = rates.wilson(10, 10)
    assert lo == pytest.approx(0.7225, abs=1e-3) and hi == 1.0


def test_wilson_refuses_an_undefined_rate():
    with pytest.raises(ValueError):
        rates.wilson(0, 0)
    with pytest.raises(ValueError):
        rates.wilson(3, 2)


def test_rate_is_none_at_n_zero_never_zero_over_zero():
    assert rates.rate(0, 0) is None
    r = rates.rate(1, 4)
    assert (r.k, r.n, r.p) == (1, 4, 0.25) and r.lo < 0.25 < r.hi


# ── the two rates and their denominators ───────────────────────────────────────

def _clean(false_reports):
    return {"version": "clean", "bugs_present": [], "bugs_found": [], "false_reports": false_reports}


def _seeded(present, found, false_reports=0, **extra):
    return {"version": "seeded", "bugs_present": present, "bugs_found": found,
            "false_reports": false_reports, **extra}


def test_false_alarm_rate_is_per_clean_episode_not_per_report():
    """Three false reports on one clean build are ONE dirty night, and seeded-arm
    false reports are precision's problem, not this number's."""
    rows = [_clean(0), _clean(3), _clean(0), _clean(1), _seeded(["a"], [], false_reports=4)]
    r = rates.false_alarm_rate(rows)
    assert (r.k, r.n) == (2, 4) and r.p == 0.5


def test_false_alarm_rate_counts_every_clean_episode_including_unscored_completion():
    rows = [{**_clean(1), "completed": None, "truncated": True}, _clean(0)]
    r = rates.false_alarm_rate(rows)
    assert (r.k, r.n) == (1, 2)


def test_false_alarm_rate_is_none_without_clean_episodes():
    assert rates.false_alarm_rate([_seeded(["a"], ["a"])]) is None


def test_catch_rate_is_per_defect_not_per_episode():
    """A case with a functional bug and two display bugs contributes THREE to the
    denominator; catching one of them is 1/3, not a failed episode."""
    rows = [_seeded(["f", "d1", "d2"], ["f"]), _seeded(["g"], ["g"]), _clean(0)]
    r = rates.catch_rate(rows)
    assert (r.k, r.n) == (2, 4) and r.p == 0.5


def test_catch_rate_is_none_without_seeded_defects():
    assert rates.catch_rate([_clean(0), _clean(1)]) is None


# ── clean-run integrity ────────────────────────────────────────────────────────

def test_clean_run_integrity_is_the_power_of_the_clean_probability():
    point, ci = rates.clean_run_integrity(0.01, 200)
    assert point == pytest.approx(0.99 ** 200) and point == pytest.approx(0.134, abs=1e-3)
    assert ci is None
    # A 10% rate over 200 cases is essentially never a clean night.
    point, _ = rates.clean_run_integrity(0.10, 200)
    assert point < 1e-8


def test_clean_run_integrity_propagates_the_interval_monotonically():
    point, (lo, hi) = rates.clean_run_integrity(0.05, 20, ci=(0.01, 0.15))
    # High false-alarm bound -> LOW integrity bound.
    assert lo == pytest.approx(0.85 ** 20) and hi == pytest.approx(0.99 ** 20)
    assert lo <= point <= hi
    assert rates.clean_run_integrity(0.0, 200)[0] == 1.0
    assert rates.clean_run_integrity(0.3, 0)[0] == 1.0


def test_clean_run_integrity_rejects_nonsense():
    with pytest.raises(ValueError):
        rates.clean_run_integrity(1.5, 10)
    with pytest.raises(ValueError):
        rates.clean_run_integrity(0.1, -1)


# ── projection ─────────────────────────────────────────────────────────────────

def test_projection_from_bare_floats_gives_points_only():
    p = rates.projection(0.05, 0.8, n_clean=200, n_seeded=50)
    assert p["expected_false_alarms"] == pytest.approx(10.0)
    assert p["expected_misses"] == pytest.approx(10.0)
    assert p["clean_run_integrity"] == pytest.approx(0.95 ** 200)
    assert p["expected_false_alarms_ci"] is None and p["expected_misses_ci"] is None
    assert p["clean_run_integrity_ci"] is None


def test_projection_from_rates_carries_intervals_the_right_way_round():
    fa = rates.rate(1, 20)          # 5% [~0.9%, ~23.6%]
    catch = rates.rate(8, 10)       # 80% [~49%, ~94%]
    p = rates.projection(fa, catch, n_clean=100, n_seeded=40)
    assert p["expected_false_alarms"] == pytest.approx(5.0)
    lo, hi = p["expected_false_alarms_ci"]
    assert lo == pytest.approx(fa.lo * 100) and hi == pytest.approx(fa.hi * 100)
    # Misses: the HIGH catch bound gives the LOW miss bound.
    mlo, mhi = p["expected_misses_ci"]
    assert mlo == pytest.approx((1 - catch.hi) * 40) and mhi == pytest.approx((1 - catch.lo) * 40)
    assert mlo <= p["expected_misses"] <= mhi
    ilo, ihi = p["clean_run_integrity_ci"]
    assert ilo <= p["clean_run_integrity"] <= ihi


def test_projection_with_no_measured_rate_leaves_none_not_zero():
    p = rates.projection(None, None, n_clean=10, n_seeded=10)
    assert p["expected_false_alarms"] is None and p["expected_misses"] is None
    assert p["clean_run_integrity"] is None
    with pytest.raises(ValueError):
        rates.projection(0.1, 0.9, n_clean=-1, n_seeded=1)


# ── blocker recall ─────────────────────────────────────────────────────────────

DEFECTS = {
    "f-l4": {"kind": "functional", "tier": "L4"},
    "f-l3": {"kind": "functional", "tier": "l3"},      # case-insensitive tier
    "f-l1": {"kind": "functional", "tier": "L1"},
    "d-l4": {"kind": "display", "tier": "L4"},         # display never blocks
}


def test_blocker_recall_counts_only_functional_defects_in_the_top_tiers():
    rows = [
        _seeded(["f-l4", "d-l4"], ["d-l4"], defects=DEFECTS),   # L4 functional missed
        _seeded(["f-l3"], ["f-l3"], defects=DEFECTS),           # L3 functional found
        _seeded(["f-l1"], ["f-l1"], defects=DEFECTS),           # below the cut: not counted
        _clean(0),
    ]
    r = rates.blocker_recall(rows)
    assert (r.k, r.n) == (1, 2) and r.p == 0.5
    # A wider cut pulls the L1 in.
    r = rates.blocker_recall(rows, tiers=("L4", "L3", "L2", "L1"))
    assert (r.k, r.n) == (2, 3)


def test_blocker_recall_resolves_tiers_through_the_lookup_when_rows_carry_none():
    rows = [_seeded(["f-l4"], ["f-l4"], app_id="app"), _seeded(["f-l1"], [], app_id="app")]
    r = rates.blocker_recall(rows, defects=lambda app, bug: DEFECTS.get(bug))
    assert (r.k, r.n) == (1, 1)
    # No lookup and no `defects` on the rows: nothing qualifies, so None — not 0.
    assert rates.blocker_recall(rows) is None


def test_blocker_recall_is_none_when_no_blocker_was_seeded():
    """n = 0 -> None, never 0/0 and never a 0% that reads as 'missed them all'."""
    rows = [_seeded(["f-l1", "d-l4"], [], defects=DEFECTS), _clean(2)]
    assert rates.blocker_recall(rows) is None
    assert rates.blocker_recall([]) is None


def test_blocker_recall_drops_an_unknown_defect_rather_than_guessing_its_tier():
    rows = [_seeded(["f-l4", "mystery"], ["mystery"], defects=DEFECTS)]
    r = rates.blocker_recall(rows)
    assert (r.k, r.n) == (0, 1)


# ── formatting ─────────────────────────────────────────────────────────────────

def test_fmt_pct_ci_shape():
    assert rates.fmt_pct_ci(0.8, (0.376, 0.964), 4, 5) == "4/5 80% [38–96]"
    assert rates.fmt_pct_ci(0.134, (0.0, 0.5)) == "13% [0–50]"
    assert rates.fmt_pct_ci(None, None) == "—"
