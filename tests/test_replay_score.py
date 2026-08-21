"""Replay-based scoring — see docs/replay-scoring.md.

The two asymmetries are the whole design, so they are what these tests pin.
"""

from __future__ import annotations

from qualgentbench import replay as rp
from qualgentbench.replay_score import SCORING, score

FEATURES = [
    {"id": "percent", "state": "broken", "bug_id": "percent-wrong", "check": {"steps": ["launch"], "expect": {"present": "x"}}},
    {"id": "clear", "state": "broken", "bug_id": "clear-broken", "check": {"steps": ["launch"], "expect": {"present": "x"}}},
    {"id": "addition", "state": "ok", "check": {"steps": ["launch"], "expect": {"present": "x"}}},
    {"id": "sort_order", "state": "broken", "bug_id": "sort-broken"},  # no check
]


def _r(area, cls, claimed="deviates"):
    return {"area": area, "classification": cls, "claimed": claimed}


def test_a_confirmed_repro_is_a_true_positive_without_the_key():
    s = score(FEATURES, [_r("percent", rp.CONFIRMED)])
    assert s.confirmed == 1 and s.by_fallback == 0
    assert s.scoring == SCORING


def test_a_claim_that_does_not_reproduce_is_a_false_positive():
    """Only when DERIVED TRUTH says the area works. `addition` is a measured control."""
    s = score(FEATURES, [_r("addition", rp.DOES_NOT_REPRODUCE)],
              derived={"addition": "ok"})
    assert s.false_positives == 1
    assert s.precision == 0.0


def test_unreplayable_never_costs_the_agent():
    """A replayer bug must cost the benchmark information, not the agent points —
    an unreplayable claim leaves the precision denominator entirely."""
    s = score(FEATURES, [_r("percent", rp.UNREPLAYABLE)], {"percent": "deviates"})
    assert s.precision == 1.0
    assert s.confirmed == 1 and s.by_fallback == 1
    assert any("unreplayable" in n for n in s.notes)


def test_silence_does_not_earn_recall():
    """Replay only sees claims the agent WROTE. If recall came from replay alone, a
    model reporting nothing would score perfect precision and undefined recall."""
    s = score(FEATURES, [])
    assert s.seeded_total == 3        # every seeded defect still counts against it
    assert s.confirmed == 0 and s.recall == 0.0
    assert s.precision == 1.0         # nothing claimed, nothing wrong


def test_trust_excludes_areas_that_can_never_be_replayed():
    """`sort_order` carries no `check:`, so no claim on it is replayable. Counting it
    in the denominator would penalise the agent for a gap in the corpus."""
    s = score(FEATURES, [_r("percent", rp.CONFIRMED), _r("clear", rp.CONFIRMED)])
    assert s.replayable_possible == 3      # percent, clear, addition — not sort_order
    assert s.trust == 2 / 3


def test_a_missed_defect_is_detected_from_the_agents_own_repro():
    s = score(FEATURES, [_r("percent", rp.MISSED_DEFECT, claimed="as_specified")])
    # 3 seeded defects, none confirmed: percent is a MISSED_DEFECT proven by the
    # agent's own repro, the other two were never claimed at all. Both are misses.
    assert s.confirmed == 0 and s.missed == 3
    assert any("reported as working" in n for n in s.notes)


def test_a_correct_claim_with_a_weak_repro_is_not_a_false_positive():
    """The agent can be right while its steps are poor — charging a false positive
    there would punish a correct finding."""
    s = score(FEATURES, [_r("percent", rp.DOES_NOT_REPRODUCE)],
              derived={"percent": "broken"})
    assert s.false_positives == 0
    assert s.weak_repro == 1
    assert s.confirmed == 1        # credited, not double-charged as a miss too
    assert any("weak evidence" in n for n in s.notes)


def test_no_derived_label_is_never_charged():
    """`sort_order` has no check, so nothing can adjudicate a claim on it."""
    s = score(FEATURES, [_r("sort_order", rp.NOT_A_DEFECT)], derived={})
    assert s.false_positives == 0 and s.undetermined == 1


def test_hybrid_recall_is_key_based_and_fp_is_replay_based():
    """The split is the design: too few confirmations are replay-proven to carry
    recall, and the key cannot see a claim the agent fails to demonstrate."""
    from qualgentbench.hybrid_score import SCORING, combine
    key = {"app_id": "opencalc", "condition": "raw", "bugs_found": 2,
           "steps": 166, "step_budget": 500}
    rs = score(FEATURES, [_r("addition", rp.DOES_NOT_REPRODUCE)],
               derived={"addition": "ok"})
    h = combine(FEATURES, key, rs)
    assert h.scoring == SCORING
    assert h.found == 2                    # from the key
    assert h.false_positives == 1          # from replay
    assert 0 < h.overall < 1


def test_an_unreplayed_episode_is_visibly_unverified():
    """No replay.json must not read as 'verified and clean' — trust is 0 and the
    reason is recorded, so an unverified episode cannot be mistaken for a checked one."""
    from qualgentbench.hybrid_score import combine
    h = combine(FEATURES, {"app_id": "birday", "condition": "raw", "bugs_found": 3,
                           "steps": 138, "step_budget": 500}, None)
    assert h.trust == 0.0
    assert any("not replayed" in n for n in h.notes)


def test_recall_never_exceeds_one():
    """`bugs_found` above the seeded count would print a >100% score on the board."""
    from qualgentbench.hybrid_score import combine
    h = combine(FEATURES, {"app_id": "x", "condition": "raw", "bugs_found": 99,
                           "steps": 10, "step_budget": 500}, None)
    assert h.recall == 1.0 and h.overall <= 1.0


def test_an_undemonstrated_find_earns_half_not_zero():
    """A correct finding with a broken reproduction is half a bug report, not none —
    all-or-nothing punished right findings with wrong navigation."""
    from qualgentbench.hybrid_score import combine
    key = {"app_id": "birday", "condition": "raw", "bugs_found": 3,
           "steps": 146, "step_budget": 500}

    class _R:
        false_positives = weak_repro = undetermined = confirmed = by_fallback = 0
        trust = 0.7
        notes: list = []

    h = combine(FEATURES[:1] * 0 or [
        {"id": f"b{i}", "state": "broken", "bug_id": f"x{i}",
         "check": {"steps": ["launch"], "expect": {"present": "x"}}} for i in range(3)
    ], key, _R(), 2, [("edit_event", "no anchor"), ("delete_event", "no anchor")])
    assert h.credited == 2.0            # 1 demonstrated + 2 x 0.5
    assert abs(h.recall - 2 / 3) < 1e-9
    assert h.errors                     # still surfaced, the repro still needs fixing


def test_an_undemonstrable_claim_against_a_CONTROL_is_a_false_positive():
    """UNREPLAYABLE is non-punitive except against a measured control — otherwise
    claiming `deviates` everywhere with unreplayable repros would earn full recall
    and zero false positives."""
    features = [{"id": "ctl", "state": "ok", "check": {}},
                {"id": "seed", "state": "broken", "bug_id": "b", "check": {}}]
    results = [{"area": "ctl", "claimed": "deviates",
                "classification": rp.UNREPLAYABLE},
               {"area": "seed", "claimed": "deviates",
                "classification": rp.UNREPLAYABLE}]
    out = score(features, results, key_verdicts={"seed": "deviates"},
                derived={"ctl": "ok", "seed": "broken"})

    assert out.false_positives == 1, "a control claim that cannot be shown is a false report"
    # The seeded area is untouched: unreplayable there is still weak evidence for a
    # correct finding, credited from the agent's verdict.
    assert out.confirmed == 1
    assert out.weak_repro == 0


def test_unreplayable_on_a_SEEDED_area_is_still_free():
    """A replayer fault on a genuinely broken area must not be charged, or the
    layer punishes detection for a harness bug."""
    features = [{"id": "seed", "state": "broken", "bug_id": "b", "check": {}}]
    results = [{"area": "seed", "claimed": "deviates",
                "classification": rp.UNREPLAYABLE}]
    out = score(features, results, key_verdicts={"seed": "deviates"},
                derived={"seed": "broken"})
    assert out.false_positives == 0
    assert out.confirmed == 1
