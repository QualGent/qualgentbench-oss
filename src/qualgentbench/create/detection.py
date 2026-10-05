"""CreateBench v2: how a brief's TARGET defect is detected — `walk` or `assert` (QUA-2862).

A creation arm's authored case catches a seeded defect in one of two ways, and a
positive control that ignores which one cannot tell a sensitive benchmark from a broken
one (QUA-2859's simulation: a harmful-rule author drops power on only the briefs whose
target leaves the app alive):

    walk    the fault kills or freezes the app (crash, ANR, stuck screen) as soon as the
            route reaches it, so ANY case that walks the feature fails on the target
            build, whatever it asserts at the end.
    assert  the app stays alive and on a plausible screen; the fault is silent unless the
            case CHECKS the state or data the feature is about (wrong destination, a
            write dropped, state lost across a configuration change, a misdrawn value).

The label is derived from DEFECT METADATA ONLY, never from a simulation or a live result:

  1. the target is the case's blocking (functional) bug (`journey.case_design`);
  2. its `class:` (docs/defect-classes.md) decides directly for `crash`/`anr`/`stuck`
     (walk) and for every alive class (assert). `ordering` is defined by its TRIGGER, not
     by what the oracle sees (pass 1 of the class rules), so it is decided by step 3;
  3. the journey truth row: the seeded arm's outcome on every recorded trial. `crashed`
     (the derive's word for a death, ANR and stuck probe included) is walk; `violated`
     (the app alive, the oracle not met) is assert.

Steps 2 and 3 must agree with each other and with the case's own death gate
(`crash:`/`anr:`/`stuck:` in its check, `case_design(...)["death"]`): a walk class whose
seeded arm survived, an assert class whose seeded arm died, or trials that disagree are
PROBLEMS, and a brief with a problem has no label. `scripts/lint_create_briefs.py` fails
a positive-control entry whose recorded `detection:` is missing or differs from this
derivation, so the label lives in one function and the subset file only records it.

The class is corpus metadata (`journey.load_defects` does not copy it, so no scorer sees
it); this module reads the raw test-case document for the same reason.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any

from .. import journey

WALK, ASSERT = "walk", "assert"
LABELS = (ASSERT, WALK)
#: The fault surfaces when the route reaches it.
WALK_CLASSES = frozenset({"crash", "anr", "stuck"})
#: The app survives; only a case that looks at the right state catches it.
ASSERT_CLASSES = frozenset({"navigation", "lifecycle", "persistence", "layout",
                            "widget-inventory", "content-format"})
#: Classes defined by how the fault is REACHED (docs/defect-classes.md, pass 1): the
#: journey truth decides how they are detected.
TRIGGER_CLASSES = frozenset({"ordering"})
#: Journey truth outcomes (`derive_journey.py`).
DIED, ALIVE_FAIL = "crashed", "violated"


@dataclass(frozen=True)
class Detection:
    case_id: str
    label: str | None                      # walk | assert | None (no label: see problems)
    target: str | None = None
    defect_class: str | None = None
    seeded_outcomes: tuple[str, ...] = ()
    death_gate: str | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def why(self) -> str:
        if self.label is None:
            return "; ".join(self.problems) or "no label"
        died = self.seeded_outcomes and all(o == DIED for o in self.seeded_outcomes)
        return (f"{self.target} is class {self.defect_class}; the seeded arm "
                f"{'died' if died else 'stayed alive'} on {len(self.seeded_outcomes)} "
                f"derived trial(s)")


def seeded_outcomes(row: dict[str, Any] | None) -> list[str]:
    """Every recorded seeded-arm outcome of a journey truth row: `trials.seeded` when the
    row was derived with `--repeat`, else the single `passes.seeded`."""
    if not isinstance(row, dict):
        return []
    trials = ((row.get("trials") or {}).get("seeded")) or []
    outs = [str(t.get("outcome")) for t in trials if isinstance(t, dict) and t.get("outcome")]
    if not outs:
        one = ((row.get("passes") or {}).get("seeded")) or {}
        if one.get("outcome"):
            outs = [str(one["outcome"])]
    return outs


def derive(case: dict[str, Any], doc: dict[str, Any],
           row: dict[str, Any] | None) -> Detection:
    """The label of one journey case's target, from its test-case document (`doc`, for
    the defects' `class:`) and its journey truth row (`row`). Pure."""
    cid = str(case.get("id"))
    raw = {str(d.get("id")): d for d in doc.get("defects") or []}
    try:
        design = journey.case_design(case, journey.load_defects(doc))
    except ValueError as exc:
        return Detection(cid, None, problems=(str(exc),))
    target = design["blocking"]
    if not target:
        return Detection(cid, None, problems=(("no functional target: the case is "
                                               "expected to PASS, so there is nothing to "
                                               "detect"),))
    cls = str((raw.get(target) or {}).get("class") or "") or None
    outs = tuple(seeded_outcomes(row))
    gate = design.get("death")
    problems: list[str] = []
    if not outs:
        problems.append("no journey truth for the seeded arm (derive_journey.py first)")
    elif len(set(outs)) > 1:
        problems.append(f"seeded trials disagree ({', '.join(outs)}): unstable truth")
    died = bool(outs) and all(o == DIED for o in outs)
    alive = bool(outs) and all(o == ALIVE_FAIL for o in outs)
    if outs and len(set(outs)) == 1 and not (died or alive):
        problems.append(f"seeded arm outcome {outs[0]!r} is neither {DIED!r} nor "
                        f"{ALIVE_FAIL!r}")
    label: str | None
    if cls in WALK_CLASSES:
        label = WALK
        if outs and not died:
            problems.append(f"class {cls} is walk-detected, but the seeded arm did not die")
        if not gate:
            problems.append(f"class {cls} is walk-detected, but the case's check gates no "
                            "death (crash:/anr:/stuck:)")
    elif cls in ASSERT_CLASSES:
        label = ASSERT
        if died:
            problems.append(f"class {cls} is assert-detected, but the seeded arm died")
        if gate:
            problems.append(f"class {cls} is assert-detected, but the case's check gates a "
                            f"death ({gate})")
    elif cls in TRIGGER_CLASSES:
        label = WALK if died else ASSERT if alive else None
        if label == WALK and not gate:
            problems.append(f"{cls} target died on the seeded arm, but the check gates no "
                            "death (crash:/anr:/stuck:)")
        if label == ASSERT and gate:
            problems.append(f"{cls} target stayed alive, but the check gates a death ({gate})")
    else:
        label = None
        problems.append(f"defect {target} has no known class ({cls!r})")
    return Detection(cid, None if problems else label, target=target, defect_class=cls,
                     seeded_outcomes=outs, death_gate=gate, problems=tuple(problems))


@functools.cache
def for_case(case_id: str, app_id: str | None = None) -> Detection:
    """`derive` for a corpus case by id (held-out split included, through the loaders).
    An id that is no journey case has no label."""
    from ..corpus import heldout_apps, public_apps
    apps = [app_id] if app_id else sorted(set(public_apps()) | set(heldout_apps()))
    for app in apps:
        doc = journey.load_cases(app) or {}
        case = next((c for c in doc.get("test_cases") or [] if str(c.get("id")) == case_id),
                    None)
        if case is not None:
            return derive(case, doc, (journey.load_truth(app) or {}).get(case_id))
    return Detection(case_id, None, problems=(f"{case_id!r} is not a journey case",))


def label(case_id: str, app_id: str | None = None) -> str | None:
    """walk | assert | None."""
    return for_case(case_id, app_id).label
