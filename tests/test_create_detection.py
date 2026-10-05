"""CreateBench v2: the detection label of a brief's target, `walk` or `assert`
(QUA-2862, `create/detection.py`). Derived from defect metadata only — the target's
`class:` and its journey truth row — and checked against the case's own death gate."""

from __future__ import annotations

import pytest

from qualgentbench import corpus, journey
from qualgentbench.create import detection


def _doc(cls: str, *, gate: str | None = None, kind: str = "functional") -> tuple[dict, dict]:
    expect = {"db": "x.db", "query": "select 1", "equals": "1"}
    if gate:
        expect[gate] = True if gate == "anr" else "Sig"
    case = {"id": "c", "bugs": ["d"], "check": {"steps": ["launch"], "expect": expect}}
    doc = {"defects": [{"id": "d", "kind": kind, "class": cls, "symptoms": []}],
           "test_cases": [case]}
    return case, doc


def _row(*outcomes: str) -> dict:
    if len(outcomes) == 1:
        return {"passes": {"seeded": {"outcome": outcomes[0]}}}
    return {"passes": {"seeded": {"outcome": outcomes[0]}},
            "trials": {"seeded": [{"outcome": o} for o in outcomes]}}


@pytest.mark.parametrize("cls, gate, outcome, want", [
    ("crash", "crash", "crashed", "walk"),
    ("anr", "anr", "crashed", "walk"),
    ("stuck", "stuck", "crashed", "walk"),
    ("navigation", None, "violated", "assert"),
    ("persistence", None, "violated", "assert"),
    ("lifecycle", None, "violated", "assert"),
    # ordering is defined by its trigger: the journey truth decides how it is detected.
    ("ordering", "crash", "crashed", "walk"),
    ("ordering", None, "violated", "assert"),
])
def test_the_label_follows_the_class_and_the_journey_truth(cls, gate, outcome, want):
    case, doc = _doc(cls, gate=gate)
    d = detection.derive(case, doc, _row(outcome, outcome, outcome))
    assert d.label == want and d.problems == (), d.problems
    assert d.target == "d" and d.defect_class == cls


@pytest.mark.parametrize("cls, gate, row, needle", [
    ("crash", "crash", _row("violated"), "did not die"),
    ("crash", None, _row("crashed"), "gates no death"),
    ("navigation", None, _row("crashed"), "seeded arm died"),
    ("persistence", "crash", _row("violated"), "gates a death"),
    ("crash", "crash", _row("crashed", "violated", "crashed"), "disagree"),
    ("crash", "crash", None, "no journey truth"),
    ("ordering", "crash", _row("holds"), "neither"),
    ("mystery", None, _row("violated"), "no known class"),
])
def test_a_contradiction_leaves_the_brief_unlabelled(cls, gate, row, needle):
    case, doc = _doc(cls, gate=gate)
    d = detection.derive(case, doc, row)
    assert d.label is None and any(needle in p for p in d.problems), d.problems


def test_a_case_with_no_functional_target_has_no_label():
    case, doc = _doc("content-format", kind="display")
    d = detection.derive(case, doc, _row("holds"))
    assert d.label is None and "no functional target" in d.why


def test_every_fail_expected_public_case_is_labelled_without_a_problem():
    seen = {detection.WALK: 0, detection.ASSERT: 0}
    for app in corpus.public_apps():
        doc = journey.load_cases(app)
        defects = journey.load_defects(doc)
        for case in doc["test_cases"]:
            if not journey.case_design(case, defects)["blocking"]:
                continue
            d = detection.for_case(case["id"], app)
            assert d.label in detection.LABELS, (case["id"], d.problems)
            # Walk iff the case's own check gates the death (the corpus's death field).
            assert (d.label == detection.WALK) == bool(d.death_gate), case["id"]
            seen[d.label] += 1
    assert seen[detection.WALK] >= 4 and seen[detection.ASSERT] >= 8


def test_lookup_by_id():
    assert detection.label("anki-study-first-card") == detection.WALK
    assert detection.label("anki-open-card-from-browser", "ankidroid") == detection.ASSERT
    assert detection.label("tasks-add-subtask") == detection.WALK          # ordering → crash
    assert detection.label("no-such-case") is None
