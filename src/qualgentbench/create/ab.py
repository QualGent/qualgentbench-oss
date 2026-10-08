"""CreateBench v2: the pre-registered creation-arm A/B driver (QUA-2858).

`scripts/run_create_ab.py` (a thin wrapper over `main` here). Two creation arms (A =
control, B = treatment; an arm is a QualGent-MCP pin + a DevLoop-MCP pin, QUA-2852)
author a test case for every brief of a subset × every trial (`run --mode create`,
QUA-2856); every authored case is graded on the frozen journey runner (`grader`,
QUA-2857); and the result is judged against a PREDICTION THAT WAS WRITTEN DOWN FIRST.

Pre-registration. A prediction is code (`Prediction`, `PREDICTIONS`) or a JSON file
naming the same fields, and it is frozen into the experiment's state file the moment the
experiment starts spending (`<runs>/_runs/_create/ab/<experiment>.json`), with the
arms' resolved SHAs, the brief list, the trials, the author and the runner. Resuming
with any of those changed is refused: a prediction cannot move after its data exists.
A prediction is a versioned spec (`name/vN`, hashed with the evaluator version). The
default is the owner's MECHANISM-based registration of the harmful-rule positive control
(QUA-2862, decided 2026-10-01; `POSITIVE_CONTROL_MECHANISM`). Every brief carries a
detection label derived from defect metadata alone (`create/detection.py`: the target's
class + its journey truth): `assert` (the fault is silent unless the case checks the
state) or `walk` (the fault kills or freezes the app on the route). The harmful rule
replaces the outcome check with a trivial one, so by mechanism:

    DROP group (assert briefs × 2 trials)  power DOWN: one-sided Fisher exact, p < 0.05
    FLAT group (walk briefs × 1 trial)     power FLAT (Wilson intervals overlap)
    both groups                            repeatability FLAT, specificity FLAT
    precondition (DROP group)              arm A's power >= 0.5 and >= 12 scored cells
                                           per arm, else INCONCLUSIVE (nothing to remove)

On the 8 assert + 4 walk subset that is 8×2×2 + 4×1×2 = 40 cells. The earlier forms stay
registered and selectable, not the default: the owner's first literal registration
(`POSITIVE_CONTROL`: power DOWN on every brief and pooled — QUA-2859's simulation expects
it MISSED on every death target), `-stratified` and `-aggregate`. An expectation's
`stratum` is `all`, `alive`/`death` (`journey.case_design(...)["death"]`,
`target_stratum`) or `assert`/`walk` (`detection.label`).

How an expectation is judged (B relative to A; rates over the artifacts where the axis
was SCORED — an excluded run leaves an axis unscored, never 0, and a copy of the public
reference case is in no rate; Wilson 95% intervals):

    pooled DOWN / UP  MET iff B moved that way by at least `min_effect` and (unless
                      `separate` is off) the intervals are disjoint. B moving the
                      OTHER way, or not far enough, is NOT MET.
    pooled FLAT       MET iff the intervals overlap.
    pooled DOWN / UP, test fisher
                      MET iff the one-sided Fisher exact test of B against A in the
                      predicted direction has p < `alpha`; else NOT MET.
    each DOWN / UP    per brief of the stratum: UNSCORED (an arm has no scored
                      artifact), FLOOR (A already at 0 for DOWN / 1 for UP: there is no
                      room to move), MOVED (B strictly beyond A, by `min_effect`) or
                      NOT_MOVED. NOT MET iff any brief is NOT_MOVED; INCONCLUSIVE iff
                      fewer than `min_informative_share` of the stratum's briefs MOVED.
    each FLAT         per brief: the intervals overlap.

The verdict, and the exit code it is bound to:

    INCOMPLETE    4   cells are still to run (a cost-ceiling stop, an interrupt). No
                      partial verdict is computed: there is no peeking.
    INCONCLUSIVE  3   a registered precondition failed (arm A had nothing to remove, or
                      exclusions left too few scored cells): the experiment could not
                      test the mechanism, whatever the expectations read.
    MISSED        1   any expectation NOT MET — wrong direction included. A MISSED
                      prediction is reported as MISSED, never reinterpreted.
    INCONCLUSIVE  3   no expectation failed, but one could not be judged (no scored
                      artifacts, too few informative briefs), or more than
                      `max_faulted_share` of the cells faulted.
    DETECTED      0   every expectation MET.

Cells and order. A cell is (arm, brief, trial). A prediction may register its trials per
detection group (`Prediction.trials`, the mechanism form: assert 2, walk 1); then a
brief runs its group's count unless `--trials` overrides it for every brief. Trials are
the outer loop and the two
arms of one (brief, trial) run back to back, alternating which goes first, so device or
model drift over a long run lands on both arms. Trial `t` grades with the control of
`journey.control_for_trial(row, t - 1)`: both arms of a trial get the same control.
Optionally `--reference-trials N` also grades each brief's journey reference case (the
BASELINE row; not part of the prediction).

Fault tolerance and resume (the QUA-2600 lesson). Every stage of every cell is
recorded in the state file before and after it runs. A stage that raises is a FAULT:
recorded, retried up to `--max-attempts`, then the cell is `faulted` (excluded from the
verdict, counted against `max_faulted_share`). A creation episode that measures nothing
(env failure, contamination, rate limit) is retried the same way. Resume = run the same
command again: graded and skipped cells are DONE and never re-spent; a stage that was
started and never finished is recovered from its artifacts on disk when they exist (the
creation episode named by the cell's run-id file, the grade manifest), else counted as
a failed attempt. The DONE states are distinct from "not attempted": `graded` (with
grade status `graded`, `no_case_created` — the author saved nothing, a real outcome —
or `not_gradable`), `skipped` (the brief is not gradable, so no author was paid for it)
and `faulted`. A creation episode the shared exclusion predicate rejects (env/infra
failure, contamination, an unclean MCP session, a rate limit) measured nothing and is
retried, never counted. A saved case the creation runner flags but does not exclude
(`valid_case` false for `dead` / `off_app`) is GRADED like any other: whether a case is
any good is decided by executing it (`grader`), and `dead` is a transcript heuristic
(a screenshot-only exploration reads as "never read the screen"), so zeroing on it would
let a reading rule override the measurement. Its flags ride on the cell and the report
counts them per arm (`creation_flags`), so an arm that shifts them is visible.

Circuit breaker. `--max-consecutive-faults` (default 2) cells in a row ending `faulted`
stop the session INCOMPLETE: a dead device, a stopped MCP server, an exhausted credit
balance or a missing API key faults every cell the same way, and without the stop one
outage would burn the whole design into permanently faulted cells. Fix the cause, then
resume with `--retry-faulted`: every faulted cell gets fresh attempts (the old ones are
kept, marked `superseded`, and stay on the bill), recorded on the session.

Pinned environment. The registration also freezes what is NOT in the spec but decides a
grade: the corpus version (truth rows, controls, canaries), the frozen runner's
fingerprint (journey brief hash, budget rule, oracle binding, `GRADER_VERSION`) and the
creation brief version. A resume under any other value is refused.

Agent auth. A live run refuses to start when codex-cli would authenticate with anything
but an API key (`CODEX_API_KEY` / `OPENAI_API_KEY`, e.g. from the oss `.env`): with
neither set, the adapter would run on the operator's own codex login, which bills a ChatGPT
workspace's credits, and refuses it without QGB_ALLOW_CODEX_LOGIN (QUA-2868).
`--allow-codex-login` sets it, runs anyway and is recorded on the session; every episode
records its mode in `provenance.agent_auth` and the opt-in in `provenance.allow_codex_login`.

Cost ceiling. `--max-cost` USD is checked before every paid stage: spent so far (every
attempt's recorded cost; an unpriced episode is charged the estimate; a grade restarted
after a crash keeps the cost of its abandoned episodes) plus the stage's estimate (the
mean of the stages already priced, else `--est-author-cost` / `--est-grade-cost`). A
stage that would cross it is not started; the run stops INCOMPLETE and a resume with a
higher ceiling continues. The default ceiling is the prediction's own (v3: $260,
`DEFAULT_MAX_COST_BY_PREDICTION`), else $280 (`DEFAULT_MAX_COST`), and a
ceiling above $300 (`HARD_COST_CAP`) is refused outright; `--plan` prints the cell count
and the estimate at measured actuals (`MEASURED_AUTHOR_COST` + `MEASURED_GRADE_COST`)
beside the driver's conservative per-stage estimate.

The report reads ONLY this experiment's cells (agent/model/arm of its spec), never the
whole runs dir, and `--smoke` marks every cell `smoke` so a diagnostic run never reaches
the canonical board (`show --mode create`).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import logging
import math
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .. import corpus, journey, rates
from . import board as _board
from . import detection, grader, uptake

logger = logging.getLogger(__name__)

STATE_SCHEMA = "qualgentbench.create.ab/1"
#: Bump when the judging rules above change: it is part of the pre-registration hash.
EVALUATOR_VERSION = 1

DOWN, UP, FLAT = "down", "up", "flat"
EACH, POOLED = "each", "pooled"
AXES = ("power", "repeatability", "specificity", "lint", "strong", "strong_exec",
        "power_given_pass3")

DETECTED, MISSED, INCONCLUSIVE, INCOMPLETE = "DETECTED", "MISSED", "INCONCLUSIVE", "INCOMPLETE"
EXIT = {DETECTED: 0, MISSED: 1, INCONCLUSIVE: 3, INCOMPLETE: 4}
#: Each verdict in plain words, for a reader who has never seen the judging rules above
#: (QUA-2938; the experiment view prints it under the verdict). Descriptions only: the
#: verdict itself is `evaluate`'s. `VERDICT_LIMITS` says what no verdict means.
VERDICT_MEANING = {
    DETECTED: ("Every result the experiment wrote down before it ran came true: in this "
               "experiment, the change under test had the effect predicted for it."),
    MISSED: ("At least one result written down before the run did not come true (a move "
             "in the wrong direction counts). The prediction failed, and it is reported "
             "as failed, never reinterpreted."),
    INCONCLUSIVE: ("The experiment could not answer its question: a condition it needed "
                   "did not hold, a result could not be judged, or too many cells failed "
                   "to run. It neither confirms nor rejects the prediction."),
    INCOMPLETE: ("Some cells have not run yet, so there is no verdict. Nothing is judged "
                 "before the last cell."),
}
VERDICT_LIMITS = ("It is one experiment, on these briefs, graded by one test runner: it "
                  "does not show that the same holds for other briefs, apps or runners.")
EXIT_REFUSED = 2

MET, NOT_MET = "MET", "NOT MET"
MOVED, NOT_MOVED, FLOOR, UNSCORED = "moved", "not_moved", "floor", "unscored"

# Cell statuses.
PENDING, AUTHORED, GRADED, SKIPPED, FAULTED = ("pending", "authored", "graded", "skipped",
                                               "faulted")
DONE = (GRADED, SKIPPED, FAULTED)

DEFAULT_SUBSET = Path(journey._DATA) / "create" / "positive-control.yaml"
#: Cost basis when nothing has been priced yet (QUA-2861: a journey runner episode is
#: ~$0.89–1.4; a grade is five of them; a creation episode measured in QUA-2851).
EST_AUTHOR_COST = 1.5
EST_GRADE_COST = 7.0
#: Measured actuals (QUA-2861's interim report, 13 graded cells, 2026-10-01): a creation
#: episode $1.00 mean ($0.71–1.15), a grade (five journey runs) $4.65 mean ($3.05–6.26),
#: about $5.65 per cell. QUA-2862 had priced it at $0.95 + $3.25. `--plan` prices the
#: experiment at these; the conservative estimates above stay the driver's per-stage
#: budget check until stages are priced.
MEASURED_AUTHOR_COST = 1.00
MEASURED_GRADE_COST = 4.65
#: The live budget (owner decision, 2026-10-01): $280 by default, never above $300.
DEFAULT_MAX_COST = 280.0
HARD_COST_CAP = 300.0


# ── the prediction ─────────────────────────────────────────────────────────────

#: Which briefs an expectation is judged on, by what the brief's TARGET defect does to
#: the app (`journey.case_design(...)["death"]`, the corpus's own field — the same one
#: QUA-2859's adversary gate reads): `death` = it crashes, ANRs or freezes the app, so
#: ANY case that walks the feature fails on the target build, however it ends (a
#: harmful final step cannot remove that power); `alive` = the app survives and only a
#: case that LOOKS at the right thing catches it.
ALL, ALIVE, DEATH = "all", "alive", "death"
#: ...or by the target's DETECTION mechanism (QUA-2862, `create/detection.py`, derived
#: from the target's class + journey truth): `assert` = silent unless the case checks the
#: state (the DROP group of the mechanism prediction), `walk` = surfaces when the route
#: reaches it (the FLAT group).
ASSERT, WALK = detection.ASSERT, detection.WALK
STRATA = (ALL, ALIVE, DEATH, ASSERT, WALK)
#: How a pooled DOWN/UP expectation is tested: `ci` = point move by `min_effect` and (with
#: `separate`) disjoint Wilson intervals; `fisher` = one-sided Fisher exact, p < `alpha`.
CI_TEST, FISHER = "ci", "fisher"
#: ...and a per-brief DOWN/UP expectation (`scope=each`) may be judged by `sign`: a
#: one-sided sign test over the BRIEFS (QUA-2870; QUA-2861 GO condition 1 — trials of one
#: brief are correlated, so a claim must also hold with the brief as the unit). A brief
#: counts for the prediction when B's rate moved past A's, against it otherwise (a tie
#: included); a brief whose A sits at the floor (nothing to remove) or that is unscored on
#: either arm is left out. MET iff at least `min_briefs` briefs are judged and
#: P(X >= k | n, 1/2) < `alpha`.
SIGN = "sign"


@dataclass(frozen=True)
class Expectation:
    axis: str
    direction: str                       # down | up | flat   (B relative to A)
    scope: str = POOLED                  # each | pooled
    stratum: str = ALL                   # all | alive | death | assert | walk  (see above)
    #: down/up only: the point estimates must move by at least this much (absolute),
    #: pooled or per brief. 0 = any strict move.
    min_effect: float = 0.0
    #: pooled down/up only: the Wilson intervals must also be disjoint.
    separate: bool = True
    #: pooled down/up only: `ci` (the two fields above) or `fisher` (one-sided exact test).
    test: str = CI_TEST
    #: `fisher` / `sign`: the significance level.
    alpha: float = 0.05
    #: `sign` only: the fewest judged briefs the test may conclude on (QUA-2861: 6).
    min_briefs: int = 0

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"axis {self.axis!r} is not one of {AXES}")
        if self.direction not in (DOWN, UP, FLAT):
            raise ValueError(f"direction {self.direction!r} is not down|up|flat")
        if self.scope not in (EACH, POOLED):
            raise ValueError(f"scope {self.scope!r} is not each|pooled")
        if self.stratum not in STRATA:
            raise ValueError(f"stratum {self.stratum!r} is not {'|'.join(STRATA)}")
        if self.test not in (CI_TEST, FISHER, SIGN):
            raise ValueError(f"test {self.test!r} is not ci|fisher|sign")
        if self.test == FISHER and (self.scope != POOLED or self.direction == FLAT):
            raise ValueError("the fisher test judges a pooled down/up expectation only")
        if self.test == SIGN and (self.scope != EACH or self.direction == FLAT):
            raise ValueError("the sign test judges a per-brief (each) down/up expectation only")
        if self.min_briefs and self.test != SIGN:
            raise ValueError("min_briefs applies to the sign test only")

    def as_dict(self) -> dict[str, Any]:
        """The registered form. Fields added after the first registrations are written only
        when they differ from their default, so those registrations keep their hash."""
        d = asdict(self)
        for k, default in (("test", CI_TEST), ("alpha", 0.05), ("min_briefs", 0)):
            if d[k] == default:
                d.pop(k)
        return d

    @property
    def label(self) -> str:
        where = "every brief" if self.scope == EACH else "pooled"
        if self.stratum != ALL:
            where += f", {self.stratum} targets"
        extra = []
        if self.test == FISHER:
            extra.append(f"one-sided Fisher exact p < {self.alpha:g}")
        if self.test == SIGN:
            extra.append(f"brief-level one-sided sign test p < {self.alpha:g} over "
                         f">= {self.min_briefs} briefs")
        if self.min_effect and self.direction != FLAT:
            extra.append(f"by >= {self.min_effect:.0%}")
        if (self.scope == POOLED and self.direction != FLAT and not self.separate
                and self.test == CI_TEST):
            extra.append("CI overlap allowed")
        return f"{self.axis} {self.direction} ({where})" + (f" [{', '.join(extra)}]"
                                                            if extra else "")


@dataclass(frozen=True)
class Precondition:
    """A registered condition for the experiment to be ABLE to test its expectations.
    Judged on arm A (and the scored-cell count on both arms) of one stratum; a failed
    precondition makes the verdict INCONCLUSIVE before any expectation is read."""
    axis: str
    stratum: str
    #: arm A's pooled rate on `axis` must be at least this (None = no floor).
    min_a_rate: float | None = None
    #: each arm must have at least this many artifacts where `axis` was scored.
    min_scored: int | None = None

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"axis {self.axis!r} is not one of {AXES}")
        if self.stratum not in STRATA:
            raise ValueError(f"stratum {self.stratum!r} is not {'|'.join(STRATA)}")

    @property
    def label(self) -> str:
        parts = []
        if self.min_a_rate is not None:
            parts.append(f"arm A {self.axis} >= {self.min_a_rate:g}")
        if self.min_scored is not None:
            parts.append(f">= {self.min_scored} scored {self.axis} cells per arm")
        return f"{' and '.join(parts)} ({self.stratum} targets)"


@dataclass(frozen=True)
class UptakeCheck:
    """The MANIPULATION CHECK (QUA-2864): did the treatment reach the authored cases?
    `rule` names an `uptake.RULES` entry; arm B's share of DROP-stratum cells whose
    authored case TAKES it (`uptake.classify`, deterministic, from the authored steps)
    must be at least `min_rate`, else the verdict is INCONCLUSIVE ("treatment not
    delivered") — never MISSED, because a benchmark cannot be blamed for missing a
    change its authors never made. A cell with no authored case has not taken it; a
    faulted cell is not in the denominator. Judged before every other precondition."""
    rule: str
    stratum: str = ASSERT
    min_rate: float = 0.8

    def __post_init__(self) -> None:
        if self.rule not in uptake.RULES:
            raise ValueError(f"uptake rule {self.rule!r} is not one of {sorted(uptake.RULES)}")
        if self.stratum not in STRATA:
            raise ValueError(f"stratum {self.stratum!r} is not {'|'.join(STRATA)}")

    @property
    def label(self) -> str:
        return f"arm B uptake of {self.rule} >= {self.min_rate:g} ({self.stratum} targets)"


@dataclass(frozen=True)
class Prediction:
    """A pre-registered prediction: a versioned, hashed spec object. Changing anything
    in it — an expectation, a threshold, the trials design, a precondition, the version,
    the evaluator — changes `sha`, and a registered experiment refuses to resume under a
    different hash."""
    name: str
    expectations: tuple[Expectation, ...]
    version: int = 1
    min_informative_share: float = 0.5
    max_faulted_share: float = 0.10
    note: str = ""
    #: Trials per detection group, `((group, n), ...)` — the cell design is part of the
    #: registration. Empty = every brief runs the spec's `trials`.
    trials: tuple[tuple[str, int], ...] = ()
    preconditions: tuple[Precondition, ...] = ()
    #: The manipulation check (QUA-2864). None = none registered (every v1).
    uptake: UptakeCheck | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {"name": self.name, "version": self.version,
             "expectations": [e.as_dict() for e in self.expectations],
             "min_informative_share": self.min_informative_share,
             "max_faulted_share": self.max_faulted_share, "note": self.note,
             "evaluator_version": EVALUATOR_VERSION}
        # Written only when set, so the registrations that predate them keep their hash.
        if self.trials:
            d["trials"] = {g: n for g, n in self.trials}
        if self.preconditions:
            d["preconditions"] = [asdict(p) for p in self.preconditions]
        if self.uptake is not None:
            d["uptake"] = asdict(self.uptake)
        return d

    @property
    def sha(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()[:12]

    @property
    def ref(self) -> str:
        return f"{self.name}/v{self.version}"

    @property
    def trials_by_group(self) -> dict[str, int]:
        return dict(self.trials)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Prediction:
        return cls(name=str(d["name"]), version=int(d.get("version", 1)),
                   expectations=tuple(Expectation(**e) for e in d["expectations"]),
                   min_informative_share=float(d.get("min_informative_share", 0.5)),
                   max_faulted_share=float(d.get("max_faulted_share", 0.10)),
                   note=str(d.get("note") or ""),
                   trials=tuple((str(g), int(n)) for g, n in (d.get("trials") or {}).items()),
                   preconditions=tuple(Precondition(**p) for p in d.get("preconditions") or []),
                   uptake=UptakeCheck(**d["uptake"]) if d.get("uptake") else None)


_FLAT_REST = (Expectation("repeatability", FLAT, POOLED),
              Expectation("specificity", FLAT, POOLED))

#: THE DEFAULT (owner decision 2026-10-01, QUA-2862): the positive control as a MECHANISM
#: test. The harmful rule (QUA-2861: end every test on "the screen title is visible")
#: replaces the outcome check with a trivial one, so it can only remove power where a
#: check is what catches the target (`assert`), and it cannot remove the power of a target
#: that kills the app on the route (`walk`). Two-sided: a drop where none is predicted
#: (the walk group, repeatability, specificity) is as much a MISS as no drop where one is.
POSITIVE_CONTROL_MECHANISM = Prediction(
    name="harmful-rule-positive-control-mechanism", version=1,
    expectations=(Expectation("power", DOWN, POOLED, ASSERT, test=FISHER, alpha=0.05),
                  Expectation("power", FLAT, POOLED, WALK),
                  Expectation("repeatability", FLAT, POOLED, ASSERT),
                  Expectation("repeatability", FLAT, POOLED, WALK),
                  Expectation("specificity", FLAT, POOLED, ASSERT),
                  Expectation("specificity", FLAT, POOLED, WALK)),
    trials=((ASSERT, 2), (WALK, 1)),
    preconditions=(Precondition("power", ASSERT, min_a_rate=0.5, min_scored=12),),
    note=("QUA-2861 as decided by the owner 2026-10-01 (QUA-2862): arm B = QualGent-MCP "
          "throwaway/createbench-v2-harmful-rule. DROP group = the assert briefs of "
          "data/create/positive-control.yaml x 2 trials x 2 arms: power B < A, one-sided "
          "Fisher exact p < 0.05. FLAT group = the walk briefs x 1 trial x 2 arms: power "
          "intervals overlap. Repeatability and specificity intervals overlap on both "
          "groups. INCONCLUSIVE if arm A's DROP-group power < 0.5 or fewer than 12 scored "
          "DROP cells per arm. Labels from defect metadata (class + journey truth) only."))

#: The mechanism form with a MANIPULATION CHECK (QUA-2864, after QUA-2861's interim
#: report). QUA-2861's run 1 read the rule into the author on 7/7 arm-B cells and saw it
#: followed 0/7 times, and 4 of its 8 DROP targets were navigation faults that even a
#: followed title check still catches. v2 changes three things and nothing else:
#:   * the rule is `uptake.APP_OPEN` (no outcome check, expected_result "the app is
#:     still open"), placed where the author takes it up (docs/createbench-v2-uptake.md);
#:   * the DROP group is the briefs whose target that rule PROVABLY cannot catch — the
#:     `persistence` targets (data/create/positive-control-v2.yaml); `check_design`
#:     refuses a DROP brief outside the rule's `drop_classes`;
#:   * the uptake precondition: arm B's DROP-group uptake >= 0.8, else INCONCLUSIVE.
#: DROP trials go to 4 (4 briefs x 4 = 16 scored cells per arm against the >= 12
#: precondition: 4 cells of exclusion headroom). The fifth persistence target
#: (contacts-favorite) leaked in the uptake probe and is not in the subset.
POSITIVE_CONTROL_MECHANISM_V2 = Prediction(
    name="harmful-rule-positive-control-mechanism", version=2,
    expectations=POSITIVE_CONTROL_MECHANISM.expectations,
    trials=((ASSERT, 4), (WALK, 1)),
    preconditions=(Precondition("power", ASSERT, min_a_rate=0.5, min_scored=12),),
    uptake=UptakeCheck(rule=uptake.APP_OPEN.id, stratum=ASSERT, min_rate=0.8),
    note=("QUA-2861 re-run design (QUA-2864): arm B = the app-open rule at the placement "
          "the uptake probe chose. DROP group = the persistence assert briefs of "
          "data/create/positive-control-v2.yaml x 4 trials x 2 arms: power B < A, one-sided "
          "Fisher exact p < 0.05. FLAT group = the walk briefs x 1 trial x 2 arms: power "
          "intervals overlap. Repeatability and specificity intervals overlap on both "
          "groups. INCONCLUSIVE if arm B's DROP-group uptake of app-open/v2 < 0.8 "
          "(treatment not delivered), arm A's DROP-group power < 0.5, or fewer than 12 "
          "scored DROP cells per arm."))

#: v3 (QUA-2870, owner decisions 2026-10-05): the same mechanism test on MORE briefs, so the
#: claim can hold at brief level. QUA-2861 GO condition 1 asks for >= 6 briefs per
#: stratum (6/6 in the predicted direction is a sign-test p of 1/64), and v2's DROP group
#: had 4 (best brief-level p 1/16). QUA-2870 added 4 canary-covered persistence targets
#: (medtimer, orgzly x 2, ankidroid), kept after their uptake/leak probe: the DROP group of
#: data/create/positive-control-v3.yaml. Changes against v2, nothing else:
#:   * DROP trials 4 -> 2: (8 x 2 + 4 x 1) x 2 arms = 40 cells, ~$226 at measured prices
#:     (v2's 4 DROP trials would be 72 cells, ~$407, over the $300 cap);
#:   * one more expectation: power DOWN per DROP brief by a one-sided sign test over the
#:     briefs, p < 0.05, on at least 6 judged briefs;
#:   * its own default budget (`DEFAULT_MAX_COST_BY_PREDICTION`): $260.
#: The uptake check, the >= 12 scored DROP cells precondition (16 per arm here) and every
#: v2 expectation are unchanged.
POSITIVE_CONTROL_MECHANISM_V3 = Prediction(
    name="harmful-rule-positive-control-mechanism", version=3,
    expectations=(*POSITIVE_CONTROL_MECHANISM.expectations,
                  Expectation("power", DOWN, EACH, ASSERT, test=SIGN, alpha=0.05,
                              min_briefs=6)),
    trials=((ASSERT, 2), (WALK, 1)),
    preconditions=(Precondition("power", ASSERT, min_a_rate=0.5, min_scored=12),),
    uptake=UptakeCheck(rule=uptake.APP_OPEN.id, stratum=ASSERT, min_rate=0.8),
    note=("QUA-2870 (owner decisions 2026-10-05): v2's design on "
          "data/create/positive-control-v3.yaml. DROP group = its persistence assert briefs "
          "x 2 trials x 2 arms: power B < A, one-sided Fisher exact p < 0.05, AND B below A "
          "brief by brief, one-sided sign test p < 0.05 over >= 6 judged briefs (a tie "
          "counts against; an A-floored or unscored brief is left out). FLAT group = the "
          "walk briefs x 1 trial x 2 arms: power intervals overlap. Repeatability and "
          "specificity intervals overlap on both groups. INCONCLUSIVE if arm B's DROP-group "
          "uptake of app-open/v2 < 0.8 (treatment not delivered), arm A's DROP-group power "
          "< 0.5, or fewer than 12 scored DROP cells per arm. Default budget $260."))

#: The owner's FIRST literal registration (epic QUA-2850 / QUA-2861): arm B's creation
#: guide carries a damaging rule, so B's authored cases lose power on EVERY brief while
#: staying as repeatable and as specific. Kept registered (not the default since
#: QUA-2862): QUA-2859's scripted simulation expects it MISSED on every death target,
#: because a crash/ANR/stuck target fails the walk however the case ends.
POSITIVE_CONTROL = Prediction(
    name="harmful-rule-positive-control", version=1,
    expectations=(Expectation("power", DOWN, EACH), Expectation("power", DOWN, POOLED),
                  *_FLAT_REST),
    note=("QUA-2861 as registered by the owner: arm B = QualGent-MCP "
          "throwaway/createbench-v2-harmful-rule. Power down on every brief of "
          "data/create/positive-control.yaml x 3 trials x 2 arms; repeatability and "
          "specificity flat (Wilson intervals overlap)."))

#: Alternative (i), stratified: the per-brief claim restricted to targets that leave the
#: app alive, and the death targets predicted FLAT on power (the rule cannot remove a
#: crash's power) — so the death stratum is still a falsifiable cell, not dropped.
POSITIVE_CONTROL_STRATIFIED = Prediction(
    name="harmful-rule-positive-control-stratified", version=1,
    expectations=(Expectation("power", DOWN, EACH, ALIVE),
                  Expectation("power", DOWN, POOLED, ALIVE),
                  Expectation("power", FLAT, POOLED, DEATH),
                  *_FLAT_REST),
    note=("QUA-2861 alternative (QUA-2859 finding): power down on every alive-target brief "
          "and pooled over them; power flat pooled over crash/ANR/stuck targets; "
          "repeatability and specificity flat."))

#: Alternative (ii), aggregate: one pooled power drop of at least 15 points over the
#: whole subset (point estimates; overlapping intervals allowed), repeatability and
#: specificity flat. Weaker per brief, robust to the death stratum diluting the pool.
POSITIVE_CONTROL_AGGREGATE = Prediction(
    name="harmful-rule-positive-control-aggregate", version=1,
    expectations=(Expectation("power", DOWN, POOLED, min_effect=0.15, separate=False),
                  *_FLAT_REST),
    note=("QUA-2861 alternative (QUA-2859 finding): pooled power drops by >= 15 points over "
          "the whole subset; repeatability and specificity flat."))

#: The registered default (`--prediction` omitted): v3 (QUA-2870), the uptake-checked
#: mechanism form on enough briefs for a brief-level claim. v1 and v2 stay selectable by
#: their refs, hashes unchanged.
DEFAULT_PREDICTION = POSITIVE_CONTROL_MECHANISM_V3

#: By name (the latest version of each name) and by `name/vN` ref (every version).
_REGISTERED = (POSITIVE_CONTROL_MECHANISM, POSITIVE_CONTROL_MECHANISM_V2,
               POSITIVE_CONTROL_MECHANISM_V3, POSITIVE_CONTROL,
               POSITIVE_CONTROL_STRATIFIED, POSITIVE_CONTROL_AGGREGATE)
PREDICTIONS = {p.name: p for p in _REGISTERED}
PREDICTIONS.update({p.ref: p for p in _REGISTERED})
PREDICTIONS["positive-control"] = POSITIVE_CONTROL

#: The brief subset a registration was designed on (`--briefs` omitted).
DEFAULT_SUBSET_V2 = Path(journey._DATA) / "create" / "positive-control-v2.yaml"
DEFAULT_SUBSET_V3 = Path(journey._DATA) / "create" / "positive-control-v3.yaml"
SUBSETS = {POSITIVE_CONTROL_MECHANISM_V2.ref: DEFAULT_SUBSET_V2,
           POSITIVE_CONTROL_MECHANISM_V3.ref: DEFAULT_SUBSET_V3}

#: A registration's own default `--max-cost` (owner decision 2026-10-05: v3 $260); every
#: other registration keeps `DEFAULT_MAX_COST`. `HARD_COST_CAP` binds them all.
DEFAULT_MAX_COST_BY_PREDICTION = {POSITIVE_CONTROL_MECHANISM_V3.ref: 260.0}


def default_subset(prediction: Prediction) -> Path:
    return SUBSETS.get(prediction.ref, DEFAULT_SUBSET)


def default_max_cost(prediction: Prediction) -> float:
    return DEFAULT_MAX_COST_BY_PREDICTION.get(prediction.ref, DEFAULT_MAX_COST)


def load_prediction(spec: str) -> Prediction:
    """A registered name, or a JSON file with `Prediction.as_dict()`'s fields."""
    if spec in PREDICTIONS:
        return PREDICTIONS[spec]
    p = Path(spec)
    if p.is_file():
        return Prediction.from_dict(json.loads(p.read_text()))
    raise ValueError(f"unknown prediction {spec!r}: one of {sorted(PREDICTIONS)} or a JSON file")


@functools.cache
def target_stratum(case_id: str) -> str:
    """`death` when the brief case's target kills the app (crash/ANR/stuck), else
    `alive` — from `journey.case_design`, the corpus's own field."""
    app = _app_of(case_id)
    doc = journey.load_cases(app or "") or {}
    case = next((c for c in doc.get("test_cases") or [] if str(c.get("id")) == case_id), None)
    if case is None:
        return ALIVE
    design = journey.case_design(case, journey.load_defects(doc))
    return DEATH if design.get("death") else ALIVE


def detection_group(case_id: str) -> str | None:
    """`assert` | `walk` | None (not a journey case, or a target the derivation cannot
    label) — `detection.label`, from the target's class + journey truth."""
    app = _app_of(case_id)
    return detection.label(case_id, app) if app else None


def check_design(prediction: Prediction, briefs: list[str]) -> list[str]:
    """Problems with running `prediction` on `briefs` (QUA-2864): with an uptake check,
    every brief of its DROP stratum must target a defect class the rule provably cannot
    catch (`uptake.Rule.drop_classes`) — a DROP brief the rule's case can still catch
    would let a full-uptake arm B keep its power there and read as MISSED."""
    out = []
    for e in prediction.expectations:
        if e.test == SIGN and e.min_briefs:
            among = [b for b in briefs if (detection_group(b) == e.stratum
                                           if e.stratum in (ASSERT, WALK) else True)]
            if len(among) < e.min_briefs:
                out.append(f"{e.label}: only {len(among)} {e.stratum} brief(s), the sign "
                           f"test needs >= {e.min_briefs}")
    u = prediction.uptake
    if u is None:
        return out
    rule = uptake.RULES[u.rule]
    for b in briefs:
        if u.stratum in (ASSERT, WALK) and detection_group(b) != u.stratum:
            continue
        app = _app_of(b)
        cls = detection.for_case(b, app).defect_class if app else None
        if cls not in rule.drop_classes:
            out.append(f"{b} (class {cls}): {rule.id} cannot provably remove its power — "
                       f"the DROP group must be {sorted(rule.drop_classes)} targets")
    return out


def design_trials(prediction: Prediction, briefs: list[str]) -> dict[str, int] | None:
    """{brief: trials} from the prediction's per-group trials design, or None when it
    registers none. Raises when a brief has no group the design names."""
    by_group = prediction.trials_by_group
    if not by_group:
        return None
    out: dict[str, int] = {}
    missing = []
    for b in briefs:
        g = detection_group(b)
        if g not in by_group:
            missing.append(f"{b} ({g or 'no detection label'})")
        else:
            out[b] = by_group[g]
    if missing:
        raise ValueError(f"{prediction.ref} registers trials per detection group "
                         f"{by_group}, and these briefs fit none: {', '.join(missing)}")
    return out


# ── judging (pure) ─────────────────────────────────────────────────────────────

def axis_rate(grades: Iterable[dict[str, Any]], axis: str) -> rates.Rate | None:
    """k/n over the grades where `axis` was scored (True/False), with the board's own
    rules (`board.rated` / `board.axis_value`): a copy of the reference case
    (`contamination_risk`) is in no rate, a NOT_GRADABLE grade has every axis None, and
    n/a and None never enter n."""
    vals = [_board.axis_value(g, axis) for g in _board.rated(grades)]
    k = sum(1 for v in vals if v is True)
    n = sum(1 for v in vals if v is True or v is False)
    return rates.rate(k, n)


def _rate_dict(r: rates.Rate | None) -> dict[str, Any] | None:
    return None if r is None else {"k": r.k, "n": r.n, "p": round(r.p, 4),
                                   "ci": [round(r.lo, 4), round(r.hi, 4)]}


def _overlap(a: rates.Rate, b: rates.Rate) -> bool:
    return a.lo <= b.hi and b.lo <= a.hi


def judge_pooled(direction: str, a: rates.Rate | None, b: rates.Rate | None, *,
                 min_effect: float = 0.0, separate: bool = True) -> tuple[str, str]:
    if a is None or b is None:
        return INCONCLUSIVE, "an arm has no scored artifact on this axis"
    if direction == FLAT:
        return ((MET, "intervals overlap") if _overlap(a, b)
                else (NOT_MET, "the arms separated — not flat"))
    better = (b.p < a.p) if direction == DOWN else (b.p > a.p)
    if not better:
        return NOT_MET, ("B moved the other way" if b.p != a.p else "no difference")
    effect = abs(a.p - b.p)
    if effect < min_effect:
        return NOT_MET, f"right direction, but by {effect:.0%} < the registered {min_effect:.0%}"
    if not separate:
        return MET, f"moved {effect:.0%} in the predicted direction"
    separated = (b.hi < a.lo) if direction == DOWN else (b.lo > a.hi)
    return ((MET, "intervals disjoint in the predicted direction") if separated
            else (NOT_MET, "right direction, but the intervals overlap"))


def fisher_one_sided(k_a: int, n_a: int, k_b: int, n_b: int, direction: str = DOWN) -> float:
    """One-sided Fisher exact p-value that B's success rate is below A's (`down`) or
    above it (`up`): the hypergeometric tail of B's successes given both margins. Pure,
    exact (integer binomials); 1.0 when either arm is empty."""
    if n_a <= 0 or n_b <= 0:
        return 1.0
    total, succ = n_a + n_b, k_a + k_b
    denom = math.comb(total, n_b)
    lo, hi = max(0, n_b - (total - succ)), min(n_b, succ)

    def pmf(x: int) -> int:
        return math.comb(succ, x) * math.comb(total - succ, n_b - x)
    xs = range(lo, k_b + 1) if direction == DOWN else range(k_b, hi + 1)
    return min(1.0, sum(pmf(x) for x in xs) / denom)


def judge_fisher(direction: str, a: rates.Rate | None, b: rates.Rate | None, *,
                 alpha: float = 0.05) -> tuple[str, str, float | None]:
    """(outcome, why, p) of a pooled down/up expectation under the one-sided Fisher exact
    test. A move the other way, or none, is NOT MET whatever p says."""
    if a is None or b is None:
        return INCONCLUSIVE, "an arm has no scored artifact on this axis", None
    p = fisher_one_sided(a.k, a.n, b.k, b.n, direction)
    better = (b.p < a.p) if direction == DOWN else (b.p > a.p)
    if not better:
        return NOT_MET, ("B moved the other way" if b.p != a.p else "no difference") + \
            f" (Fisher p = {p:.3g})", p
    if p < alpha:
        return MET, f"Fisher exact one-sided p = {p:.3g} < {alpha:g}", p
    return NOT_MET, f"right direction, but Fisher exact one-sided p = {p:.3g} >= {alpha:g}", p


def sign_test_p(k: int, n: int) -> float:
    """One-sided sign-test p: P(X >= k) for X ~ Binomial(n, 1/2). 1.0 when n is 0."""
    if n <= 0:
        return 1.0
    return min(1.0, sum(math.comb(n, x) for x in range(k, n + 1)) / 2 ** n)


def judge_sign(direction: str, rows: list[dict[str, Any]], *, alpha: float,
               min_briefs: int) -> tuple[str, str, dict[str, Any]]:
    """(outcome, why, extra) of a per-brief down/up expectation under the brief-level sign
    test (`SIGN`). `rows` carry each brief's `a`/`b` rates (`rates.Rate` or None)."""
    k = against = floor = unscored = 0
    for r in rows:
        a, b = r["a"], r["b"]
        if a is None or b is None:
            unscored += 1
        elif (direction == DOWN and a.p == 0) or (direction == UP and a.p == 1):
            floor += 1
        elif (b.p < a.p) if direction == DOWN else (b.p > a.p):
            k += 1
        else:
            against += 1
    n = k + against
    p = sign_test_p(k, n)
    extra = {"sign": {"for": k, "against": against, "floor": floor, "unscored": unscored,
                      "judged": n}, "p_value": round(p, 6)}
    tally = f"{k}/{n} judged brief(s) {direction} (floor {floor}, unscored {unscored})"
    if against > k:
        return NOT_MET, f"{tally}: more briefs moved against the prediction", extra
    if n < min_briefs:
        return INCONCLUSIVE, f"{tally}: the sign test needs >= {min_briefs}", extra
    if p < alpha:
        return MET, f"{tally}: sign test one-sided p = {p:.3g} < {alpha:g}", extra
    return NOT_MET, f"{tally}: sign test one-sided p = {p:.3g} >= {alpha:g}", extra


def judge_brief(direction: str, a: rates.Rate | None, b: rates.Rate | None, *,
                min_effect: float = 0.0) -> str:
    if a is None or b is None:
        return UNSCORED
    if direction == FLAT:
        return MOVED if _overlap(a, b) else NOT_MOVED       # "moved" = held flat here
    if (direction == DOWN and a.p == 0) or (direction == UP and a.p == 1):
        return FLOOR
    moved = (b.p < a.p) if direction == DOWN else (b.p > a.p)
    return MOVED if moved and abs(a.p - b.p) >= min_effect else NOT_MOVED


def evaluate(prediction: Prediction, grades: dict[str, dict[str, list[dict[str, Any]]]],
             briefs: list[str], *, arm_a: str, arm_b: str, complete: bool = True,
             faulted: int = 0, planned: int = 0,
             strata: dict[str, str] | None = None,
             groups: dict[str, str | None] | None = None,
             uptake_cells: dict[str, dict[str, list[bool]]] | None = None,
             uptake_rule: str | None = None) -> dict[str, Any]:
    """Judge `prediction` on `grades[arm][brief] = [grade, …]` (finished grades only).
    `strata` = {brief: alive|death} (default: `target_stratum`); `groups` = {brief:
    assert|walk|None} (default: `detection_group`). `uptake_cells[arm][brief]` = one
    `uptake.classify(...).taken` per finished cell, under `uptake_rule` (default: the
    prediction's own check's rule) — what the manipulation check judges and the report
    prints per arm. Pure apart from those corpus reads: the live driver, the report and
    the tests all end here."""
    strata = dict(strata or {})
    for b in briefs:
        if b not in strata:
            strata[b] = target_stratum(b)
    groups = dict(groups or {})
    for b in briefs:
        if b not in groups:
            groups[b] = detection_group(b)

    def in_stratum(stratum: str) -> list[str]:
        if stratum in (ASSERT, WALK):
            return [b for b in briefs if groups[b] == stratum]
        return [b for b in briefs if stratum == ALL or strata[b] == stratum]

    def pooled(arm: str, axis: str, among: list[str]) -> rates.Rate | None:
        return axis_rate([g for b in among for g in grades.get(arm, {}).get(b, [])], axis)

    def per_brief(arm: str, brief: str, axis: str) -> rates.Rate | None:
        return axis_rate(grades.get(arm, {}).get(brief, []), axis)

    def uptake_rate(arm: str, among: list[str]) -> rates.Rate | None:
        vals = [v for b in among for v in (uptake_cells or {}).get(arm, {}).get(b, [])]
        return rates.rate(sum(1 for v in vals if v), len(vals))

    pre_results = []
    u = prediction.uptake
    rule_id = uptake_rule or (u.rule if u else None)
    if u is not None:
        among = in_stratum(u.stratum)
        a, b = uptake_rate(arm_a, among), uptake_rate(arm_b, among)
        if b is None:
            why = (f"treatment not delivered: no arm-B {u.stratum} cell was classified "
                   f"for {u.rule}")
        elif b.p < u.min_rate:
            why = (f"treatment not delivered: arm B took {u.rule} on {b.k}/{b.n} "
                   f"{u.stratum} cell(s) ({b.p:.0%}), under {u.min_rate:.0%}")
        else:
            why = ""
        pre_results.append({"precondition": u.label, "kind": "uptake", **asdict(u),
                            "met": not why, "why": why or "met", "a": _rate_dict(a),
                            "b": _rate_dict(b), "briefs_in_stratum": len(among)})
    for pc in prediction.preconditions:
        among = in_stratum(pc.stratum)
        a, b = pooled(arm_a, pc.axis, among), pooled(arm_b, pc.axis, among)
        fails = []
        n_a, n_b = (a.n if a else 0), (b.n if b else 0)
        if pc.min_scored is not None and min(n_a, n_b) < pc.min_scored:
            fails.append(f"exclusions left {n_a} (A) / {n_b} (B) scored {pc.axis} cell(s), "
                         f"fewer than {pc.min_scored} per arm")
        if pc.min_a_rate is not None and (a is None or a.p < pc.min_a_rate):
            fails.append(f"arm A's {pc.axis} is {'unscored' if a is None else f'{a.p:.0%}'}, "
                         f"under {pc.min_a_rate:.0%}: nothing for arm B to remove")
        pre_results.append({"precondition": pc.label, **asdict(pc), "met": not fails,
                            "why": "; ".join(fails) or "met", "a": _rate_dict(a),
                            "b": _rate_dict(b), "briefs_in_stratum": len(among)})

    results = []
    for e in prediction.expectations:
        among = in_stratum(e.stratum)
        if e.scope == POOLED:
            a, b = pooled(arm_a, e.axis, among), pooled(arm_b, e.axis, among)
            extra: dict[str, Any] = {}
            if e.test == FISHER:
                outcome, why, p = judge_fisher(e.direction, a, b, alpha=e.alpha)
                extra["p_value"] = None if p is None else round(p, 6)
            else:
                outcome, why = judge_pooled(e.direction, a, b, min_effect=e.min_effect,
                                            separate=e.separate)
            if not among:
                outcome, why = INCONCLUSIVE, f"no brief in the {e.stratum} stratum"
            results.append({"expectation": e.label, **asdict(e), "outcome": outcome,
                            "why": why, "a": _rate_dict(a), "b": _rate_dict(b),
                            "briefs_in_stratum": len(among), **extra})
            continue
        if e.test == SIGN:
            rated = [{"brief": brief, "a": per_brief(arm_a, brief, e.axis),
                      "b": per_brief(arm_b, brief, e.axis)} for brief in among]
            outcome, why, extra = judge_sign(e.direction, rated, alpha=e.alpha,
                                             min_briefs=e.min_briefs)
            rows = [{"brief": r["brief"], "stratum": strata[r["brief"]],
                     "group": groups[r["brief"]],
                     "outcome": judge_brief(e.direction, r["a"], r["b"]),
                     "a": _rate_dict(r["a"]), "b": _rate_dict(r["b"])} for r in rated]
            if not among:
                outcome, why = INCONCLUSIVE, f"no brief in the {e.stratum} stratum"
            results.append({"expectation": e.label, **asdict(e), "outcome": outcome,
                            "why": why, "briefs": rows, **extra})
            continue
        rows, counts = [], {MOVED: 0, NOT_MOVED: 0, FLOOR: 0, UNSCORED: 0}
        for brief in among:
            a, b = per_brief(arm_a, brief, e.axis), per_brief(arm_b, brief, e.axis)
            o = judge_brief(e.direction, a, b, min_effect=e.min_effect)
            counts[o] += 1
            rows.append({"brief": brief, "stratum": strata[brief], "group": groups[brief],
                         "outcome": o, "a": _rate_dict(a), "b": _rate_dict(b)})
        need = max(1, math.ceil(prediction.min_informative_share * len(among)))
        if counts[NOT_MOVED]:
            outcome = NOT_MET
            why = (f"{counts[NOT_MOVED]} brief(s) did not move: "
                   + ", ".join(r["brief"] for r in rows if r["outcome"] == NOT_MOVED))
        elif counts[MOVED] < need:
            outcome = INCONCLUSIVE
            why = (f"only {counts[MOVED]} of {len(among)} brief(s) informative (need {need}; "
                   f"floor {counts[FLOOR]}, unscored {counts[UNSCORED]})")
        else:
            outcome, why = MET, f"{counts[MOVED]} of {len(among)} brief(s) moved"
        results.append({"expectation": e.label, **asdict(e), "outcome": outcome, "why": why,
                        "counts": counts, "briefs": rows})

    diagnostics = {axis: {"a": _rate_dict(pooled(arm_a, axis, briefs)),
                          "b": _rate_dict(pooled(arm_b, axis, briefs))} for axis in AXES}
    by_group = {g: {axis: {"a": _rate_dict(pooled(arm_a, axis, in_stratum(g))),
                           "b": _rate_dict(pooled(arm_b, axis, in_stratum(g)))}
                    for axis in ("power", "repeatability", "specificity")}
                for g in (ASSERT, WALK) if in_stratum(g)}
    # The brief-level view (QUA-2864): a group's cells come from few briefs, and trials of
    # one brief are correlated, so the cell-level Fisher p overstates the evidence. Per
    # brief: A vs B power and whether B fell below A; printed, never part of the verdict.
    brief_power = {}
    for grp in (ASSERT, WALK):
        rows = []
        for brief in in_stratum(grp):
            a, b = per_brief(arm_a, brief, "power"), per_brief(arm_b, brief, "power")
            rows.append({"brief": brief, "a": _rate_dict(a), "b": _rate_dict(b),
                         "b_below_a": None if a is None or b is None else b.p < a.p})
        if rows:
            judged = [r for r in rows if r["b_below_a"] is not None]
            brief_power[grp] = {"briefs": rows, "b_below_a": sum(1 for r in judged
                                                                 if r["b_below_a"]),
                                "judged": len(judged)}
    uptake_report = None
    if rule_id and uptake_cells is not None:
        uptake_report = {"rule": rule_id, "arms": {
            arm: {"all": _rate_dict(uptake_rate(arm, briefs)),
                  **{g: _rate_dict(uptake_rate(arm, in_stratum(g)))
                     for g in (ASSERT, WALK) if in_stratum(g)}}
            for arm in (arm_a, arm_b)}}
    excluded = {arm: sum(1 for b in briefs for g in grades.get(arm, {}).get(b, [])
                         if g.get("contamination_risk")) for arm in (arm_a, arm_b)}
    faulted_share = (faulted / planned) if planned else 0.0
    if not complete:
        verdict, why = INCOMPLETE, "cells are still to run — no verdict before the last cell"
    elif any(not r["met"] for r in pre_results):
        verdict = INCONCLUSIVE
        why = "precondition not met — " + "; ".join(
            f"{r['precondition']}: {r['why']}" for r in pre_results if not r["met"])
    elif any(r["outcome"] == NOT_MET for r in results):
        verdict = MISSED
        why = "; ".join(f"{r['expectation']}: {r['why']}" for r in results
                        if r["outcome"] == NOT_MET)
    elif faulted_share > prediction.max_faulted_share:
        verdict = INCONCLUSIVE
        why = (f"{faulted} of {planned} cell(s) faulted ({faulted_share:.0%} > "
               f"{prediction.max_faulted_share:.0%})")
    elif any(r["outcome"] == INCONCLUSIVE for r in results):
        verdict = INCONCLUSIVE
        why = "; ".join(f"{r['expectation']}: {r['why']}" for r in results
                        if r["outcome"] == INCONCLUSIVE)
    else:
        verdict, why = DETECTED, "every pre-registered expectation met"
    return {"verdict": verdict, "why": why, "exit_code": EXIT[verdict],
            "prediction": prediction.ref, "prediction_sha": prediction.sha,
            "preconditions": pre_results, "uptake": uptake_report,
            "brief_power": brief_power,
            "expectations": results, "diagnostics": diagnostics, "by_group": by_group,
            "strata": {b: strata[b] for b in briefs},
            "groups": {b: groups[b] for b in briefs},
            "contamination_risk": excluded, "faulted": faulted, "planned": planned}


# ── the experiment ─────────────────────────────────────────────────────────────

@dataclass
class ArmSpec:
    """One creation arm. `qualgent_mcp` / `devloop` are `SRC@REF`; `manifest` is the
    resolved `ResolvedArm.manifest()` (SHAs and hashes, never private text)."""
    name: str
    qualgent_mcp: str = ""
    devloop: str = ""
    qualgent_tools: str = "template"
    manifest: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return _board.arm_label(self.name, self.manifest)

    def pinned(self, which: str) -> str:
        """`SRC@<resolved sha>` — the author runs on exactly the commit registered."""
        src = (self.qualgent_mcp if which == "qualgent_mcp" else self.devloop).rpartition("@")[0]
        sha = ((self.manifest or {}).get(which) or {}).get("sha")
        return f"{src}@{sha}" if src and sha else (
            self.qualgent_mcp if which == "qualgent_mcp" else self.devloop)

    def pins(self) -> dict[str, Any]:
        m = self.manifest or {}
        return {"name": self.name,
                "qualgent_mcp": (m.get("qualgent_mcp") or {}).get("sha"),
                "devloop": (m.get("devloop") or {}).get("sha"),
                "template_sha256": (m.get("devloop") or {}).get("template_sha256"),
                "guide_sha256": m.get("guide_sha256"),
                "qualgent_tools": m.get("qualgent_tools", self.qualgent_tools)}


@dataclass
class ExperimentSpec:
    name: str
    arms: tuple[ArmSpec, ArmSpec]
    briefs: list[str]
    trials: int
    author: dict[str, str]
    runner: dict[str, str]
    prediction: Prediction
    kind: str = _board.CANONICAL
    reference_trials: int = 0
    #: {brief: trials} when briefs run different trial counts (a prediction's per-group
    #: design); None = every brief runs `trials`. `trials` is then the largest count.
    brief_trials: dict[str, int] | None = None

    @property
    def arm_a(self) -> ArmSpec:
        return self.arms[0]

    @property
    def arm_b(self) -> ArmSpec:
        return self.arms[1]

    def trials_for(self, brief: str) -> int:
        return (self.brief_trials or {}).get(brief, self.trials)

    def registration(self) -> dict[str, Any]:
        """Everything a resume must not change."""
        reg = {"name": self.name, "arms": [a.pins() for a in self.arms],
               "briefs": list(self.briefs), "trials": self.trials, "author": self.author,
               "runner": self.runner, "kind": self.kind,
               "reference_trials": self.reference_trials,
               "prediction": self.prediction.as_dict(), "prediction_sha": self.prediction.sha}
        if self.brief_trials:               # written only when set: older states compare clean
            reg["brief_trials"] = dict(self.brief_trials)
        return reg

    def as_dict(self) -> dict[str, Any]:
        return {**self.registration(), "arm_specs": [asdict(a) for a in self.arms]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExperimentSpec:
        arms = tuple(ArmSpec(**a) for a in d["arm_specs"])
        return cls(name=d["name"], arms=(arms[0], arms[1]), briefs=list(d["briefs"]),
                   trials=int(d["trials"]), author=dict(d["author"]),
                   runner=dict(d["runner"]), prediction=Prediction.from_dict(d["prediction"]),
                   kind=d.get("kind", _board.CANONICAL),
                   reference_trials=int(d.get("reference_trials") or 0),
                   brief_trials=({str(k): int(v) for k, v in d["brief_trials"].items()}
                                 if d.get("brief_trials") else None))


@dataclass(frozen=True)
class Cell:
    arm: str                             # an arm name, or board.REFERENCE_ARM
    case_id: str
    trial: int                           # 1-based

    @property
    def key(self) -> str:
        return f"{self.arm}.{self.case_id}.t{self.trial}"

    @property
    def reference(self) -> bool:
        return self.arm == _board.REFERENCE_ARM


def plan_cells(spec: ExperimentSpec) -> list[Cell]:
    """Trials outermost; per (brief, trial) both arms back to back, alternating which
    runs first; the reference cells (if any) after the arms of their trial. A brief with
    fewer trials (`spec.trials_for`) simply drops out of the later rounds."""
    out: list[Cell] = []
    a, b = spec.arm_a.name, spec.arm_b.name
    for t in range(1, spec.trials + 1):
        for i, brief in enumerate(spec.briefs):
            if t > spec.trials_for(brief):
                continue
            first, second = (a, b) if (i + t) % 2 == 0 else (b, a)
            out += [Cell(first, brief, t), Cell(second, brief, t)]
            if t <= spec.reference_trials:
                out.append(Cell(_board.REFERENCE_ARM, brief, t))
    return out


def load_subset(path: Path = DEFAULT_SUBSET) -> list[str]:
    import yaml
    doc = yaml.safe_load(Path(path).read_text()) or {}
    return [str(b["case"]) for b in doc.get("briefs") or []]


@functools.cache
def _app_of(case_id: str) -> str | None:
    """`grader.resolve_app`, once per case per process (it loads every app suite)."""
    return grader.resolve_app(case_id)


def gradability(case_id: str) -> tuple[str, str]:
    """(status, reason) the grader would give this brief, decided from the corpus alone
    (the reference case through `plan_grade`): a brief whose controls were never
    derived is `not_gradable` whatever its author writes."""
    app = _app_of(case_id)
    if not app:
        return grader.NOT_GRADABLE, f"{grader.UNKNOWN_CASE}: {case_id!r} is not a journey case"
    plan = grader.plan_grade(grader.reference_case(app, case_id), case_id, app_id=app)
    return plan.status, plan.reason


# ── state ──────────────────────────────────────────────────────────────────────

def state_path(runs_dir: Path | str, name: str) -> Path:
    from ..checkpoint import RUN_META_DIR
    return Path(runs_dir) / RUN_META_DIR / "_create" / "ab" / f"{name}.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def environment(spec: ExperimentSpec) -> dict[str, Any]:
    """What decides a grade without being in the spec, frozen at registration: the
    corpus version (truth rows, derived controls, canaries — `corpus_version`), the
    frozen runner's fingerprint (journey brief hash, budget rule, oracle binding,
    `GRADER_VERSION`) and the creation brief version. A resume under different values
    would blend two regimes into one verdict, so it is refused (`environment_diff`)."""
    from .brief import CREATE_BRIEF_VERSION
    return {"corpus_version": corpus.corpus_version(),
            "runner": grader.runner_fingerprint(spec.runner["agent"], spec.runner["model"]),
            "create_brief_version": CREATE_BRIEF_VERSION}


def environment_diff(state: dict[str, Any], spec: ExperimentSpec) -> list[str]:
    """What differs between the registered environment and now ([] for a state written
    before the environment was frozen)."""
    was = state.get("environment")
    if not was:
        return []
    now = environment(spec)
    return [f"{k}: registered {json.dumps(was.get(k))[:200]} → now {json.dumps(now.get(k))[:200]}"
            for k in sorted(set(was) | set(now)) if was.get(k) != now.get(k)]


def new_state(spec: ExperimentSpec, *, run_id: str, ungated: bool) -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "spec": spec.as_dict(), "registered_at": _now(),
            "run_id": run_id, "corpus": corpus.stamp(), "ungated": ungated,
            "environment": environment(spec),
            # The judging labels as registered: the verdict reads THESE, so a later corpus
            # edit cannot move a brief between the DROP and FLAT groups after the fact.
            "labels": {"groups": {b: detection_group(b) for b in spec.briefs},
                       "strata": {b: target_stratum(b) for b in spec.briefs}},
            "cells": {c.key: {"arm": c.arm, "case_id": c.case_id, "trial": c.trial,
                              "status": PENDING, "attempts": [], "author": None,
                              "manifest": None, "grade_status": None,
                              "cost": {"author": 0.0, "grade": 0.0, "wasted": 0.0}}
                      for c in plan_cells(spec)},
            "sessions": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str) + "\n")
    tmp.replace(path)


def load_state(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if doc.get("schema") == STATE_SCHEMA else None


_OPTIONAL_REGISTRATION = ("brief_trials",)


def registration_diff(state: dict[str, Any], spec: ExperimentSpec) -> list[str]:
    """What differs between the registration frozen in `state` and `spec` — compared
    against the STORED fields, so an evaluator change (its version is in the
    prediction hash) shows even on a bare resume."""
    now = spec.registration()
    # A key written only when set (brief_trials) is compared whenever EITHER side has it,
    # so dropping it on a resume is a difference too.
    keys = set(now) | {k for k in _OPTIONAL_REGISTRATION if k in state["spec"]}
    was = {k: state["spec"].get(k) for k in keys}
    return [f"{k}: registered {json.dumps(was.get(k))[:160]} → now {json.dumps(now.get(k))[:160]}"
            for k in sorted(set(was) | set(now)) if was.get(k) != now.get(k)]


def spent(state: dict[str, Any]) -> dict[str, float]:
    tot = {"author": 0.0, "grade": 0.0, "wasted": 0.0}
    for c in state["cells"].values():
        for k in tot:
            tot[k] += float((c.get("cost") or {}).get(k) or 0.0)
    tot["total"] = round(sum(tot.values()), 4)
    return tot


def _observed_mean(state: dict[str, Any], stage: str) -> float | None:
    vals = [a["cost_usd"] for c in state["cells"].values() for a in c["attempts"]
            if a.get("stage") == stage and a.get("priced") and
            isinstance(a.get("cost_usd"), int | float)]
    return sum(vals) / len(vals) if vals else None


def pending_cells(runs_dir: Path | str, experiment: str) -> dict[tuple[str, str], int]:
    """{(arm label, author label): authored-but-ungraded cells} — the board's `pending`
    for `show --mode create --experiment`."""
    state = load_state(state_path(runs_dir, experiment))
    if not state:
        return {}
    spec = ExperimentSpec.from_dict(state["spec"])
    labels = {a.name: a.label for a in spec.arms}
    out: dict[tuple[str, str], int] = {}
    for c in state["cells"].values():
        if c["status"] == AUTHORED and c["arm"] in labels:
            key = (labels[c["arm"]], _board.agent_label(spec.author))
            out[key] = out.get(key, 0) + 1
    return out


AUTHOR_STAGE, GRADE_STAGE = "author", "grade"


def experiment_episodes(runs_dir: Path | str, name: str) -> list[dict[str, Any]]:
    """Every episode the experiment's state names, in plan order (QUA-2869: the
    experiment view). Per cell: each authoring attempt's creation episode (retries
    included), then each grade run attempt from the cell's grade manifest, in the
    grader's run order. Read-only; the episode dirs are as recorded (relative to
    `runs_dir`). Each entry: `{episode_dir, cell, arm, case_id, trial, stage, role,
    attempt, excluded}` — `role` is the grade run's key (`clean-1`, `target-1`, …),
    "" for a creation episode."""
    runs_dir = Path(runs_dir)
    state = load_state(state_path(runs_dir, name))
    if state is None:
        raise FileNotFoundError(f"no experiment {name!r} at {state_path(runs_dir, name)}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ep: Any, key: str, rec: dict, stage: str, role: str, n: int, excluded: str) -> None:
        if not ep or str(ep) in seen:
            return
        seen.add(str(ep))
        out.append({"episode_dir": str(ep), "cell": key, "arm": rec["arm"],
                    "case_id": rec["case_id"], "trial": rec["trial"], "stage": stage,
                    "role": role, "attempt": n, "excluded": excluded or ""})

    for key, rec in state["cells"].items():
        authored = [a for a in rec.get("attempts") or [] if a.get("stage") == AUTHOR_STAGE]
        for n, a in enumerate(authored, 1):
            add(a.get("episode_dir"), key, rec, AUTHOR_STAGE, "", n, "")
        # The cell's recorded author, when no attempt carries its dir (an older state).
        add((rec.get("author") or {}).get("episode_dir"), key, rec, AUTHOR_STAGE, "",
            len(authored) or 1, (rec.get("author") or {}).get("excluded") or "")
        doc = _read_json(runs_dir / rec["manifest"]) if rec.get("manifest") else None
        for run in ((doc or {}).get("plan") or {}).get("runs") or []:
            role = f"{run.get('role')}-{run.get('index')}"
            for n, a in enumerate(run.get("attempts") or [], 1):
                add(a.get("episode_dir"), key, rec, GRADE_STAGE, role, n, a.get("excluded"))
    return out


def cell_summaries(runs_dir: Path | str, name: str) -> list[dict[str, Any]]:
    """One row per cell of the experiment, in plan order, for the experiment view: its
    status, the creation outcome, the grade's axes and uptake (from the cell's grade
    manifest) and what it cost. Read-only; reads the state file and the manifests it
    names, nothing else."""
    runs_dir = Path(runs_dir)
    state = load_state(state_path(runs_dir, name))
    if state is None:
        raise FileNotFoundError(f"no experiment {name!r} at {state_path(runs_dir, name)}")
    out = []
    for key, rec in state["cells"].items():
        doc = _read_json(runs_dir / rec["manifest"]) if rec.get("manifest") else None
        grade = (doc or {}).get("grade") or {}
        cell = (doc or {}).get("cell") or {}
        a = rec.get("author") or {}
        out.append({"cell": key, "arm": rec["arm"], "case_id": rec["case_id"],
                    "trial": rec["trial"], "status": rec["status"],
                    "grade_status": rec.get("grade_status") or "",
                    "outcome": a.get("outcome") or "", "excluded": a.get("excluded") or "",
                    "validity_flags": list(a.get("validity_flags") or []),
                    "axes": dict(grade.get("axes") or {}),
                    "uptake": (cell.get("uptake") or {}).get("taken"),
                    "fault": _board.scrub_paths(rec.get("fault") or "", runs_dir),
                    "cost_usd": round(sum(float(v or 0) for v in (rec.get("cost") or {}).values()),
                                      4)})
    return out


# ── the stages ─────────────────────────────────────────────────────────────────

@dataclass
class AuthorOutcome:
    """One creation episode as the driver needs it."""
    episode_dir: str | None              # relative to the runs dir
    artifact: str | None                 # authored_case.json (absolute), None = no case
    outcome: str = "case_created"        # case_created | no_case_created
    excluded: str = ""                   # non-empty = measured nothing: retry
    #: why there is no case to grade (no_case_created) — the grade's reason
    reason: str = ""
    validity_flags: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    wall_sec: float | None = None
    arm_manifest: dict[str, Any] | None = None


class Author(Protocol):
    async def author(self, cell: Cell, arm: ArmSpec) -> AuthorOutcome: ...
    def recover(self, cell: Cell) -> AuthorOutcome | None: ...


class Runner(Protocol):
    async def grade(self, plan: grader.GradePlan, *, run_id: str, manifest_name: str,
                    extra: dict[str, Any]) -> Path: ...


class CostCeiling(Exception):
    pass


class CircuitOpen(Exception):
    """`max_consecutive_faults` cells in a row faulted: stop, do not burn the design."""


@dataclass
class Driver:
    spec: ExperimentSpec
    runs_dir: Path
    author: Author
    runner: Runner
    max_cost: float
    max_attempts: int = 2
    est_author_cost: float = EST_AUTHOR_COST
    est_grade_cost: float = EST_GRADE_COST
    ungated: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    #: Stop the session after this many cells in a row end FAULTED (0 = never).
    max_consecutive_faults: int = 2
    #: Recorded on this session (e.g. the agent auth mode the run started under).
    session_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return state_path(self.runs_dir, self.spec.name)

    def _save(self) -> None:
        save_state(self.path, self.state)

    def _arm(self, name: str) -> ArmSpec:
        return next(a for a in self.spec.arms if a.name == name)

    # ── budget ──
    def _estimate(self, stage: str) -> float:
        seen = _observed_mean(self.state, stage)
        return seen if seen is not None else (
            self.est_author_cost if stage == "author" else self.est_grade_cost)

    def _check_budget(self, stage: str, cell: Cell) -> None:
        now, est = spent(self.state)["total"], self._estimate(stage)
        if now + est > self.max_cost:
            raise CostCeiling(f"spent ${now:.2f}; the next {stage} stage ({cell.key}) is "
                              f"estimated at ${est:.2f}, which would cross --max-cost "
                              f"${self.max_cost:.2f}")

    # ── one cell ──
    def _attempts(self, rec: dict[str, Any], stage: str) -> list[dict[str, Any]]:
        """This stage's LIVE attempts (a `--retry-faulted` supersedes the earlier ones;
        they stay in the record and on the bill, but no longer count toward
        `--max-attempts`)."""
        return [a for a in rec["attempts"] if a.get("stage") == stage
                and not a.get("superseded")]

    def retry_faulted(self) -> list[str]:
        """Give every FAULTED cell fresh attempts (`--retry-faulted`, after the cause of a
        circuit-breaker stop is fixed). A faulted cell measured nothing, so this re-rolls
        no measurement: its attempts are kept, marked superseded, and their cost stays
        spent; a cell whose author stage finished keeps that episode and is only
        re-graded. Returns the cell keys reset."""
        reset = []
        for key, rec in self.state["cells"].items():
            if rec["status"] != FAULTED:
                continue
            for att in rec["attempts"]:
                att.setdefault("ended", _now())
                att["superseded"] = True
            rec["status"] = AUTHORED if rec.get("author") else PENDING
            rec["previous_fault"] = rec.pop("fault", None)
            reset.append(key)
        if reset:
            self._save()
        return reset

    def _begin(self, rec: dict[str, Any], stage: str) -> dict[str, Any]:
        att = {"stage": stage, "started": _now()}
        rec["attempts"].append(att)
        self._save()
        return att

    def _fault(self, rec: dict[str, Any], att: dict[str, Any], why: str, stage: str) -> bool:
        """Record a failed attempt. True when the cell has attempts left."""
        att.update(ended=_now(), error=why[:500])
        if len(self._attempts(rec, stage)) >= self.max_attempts:
            rec["status"] = FAULTED
            rec["fault"] = f"{stage}: {why[:300]}"
            self._save()
            return False
        self._save()
        return True

    def _charge(self, rec: dict[str, Any], att: dict[str, Any], stage: str,
                cost: float | None, est: float) -> None:
        att["priced"] = isinstance(cost, int | float)
        att["cost_usd"] = float(cost) if att["priced"] else est
        rec["cost"][stage] = round(float(rec["cost"].get(stage) or 0) + att["cost_usd"], 4)

    async def _author_stage(self, cell: Cell, rec: dict[str, Any]) -> AuthorOutcome | None:
        # Recovery first: a stage that started and never finished may have left its
        # episode on disk (the driver died, not the episode).
        open_atts = [a for a in self._attempts(rec, "author") if "ended" not in a]
        if open_atts:
            got = self.author.recover(cell)
            att = open_atts[-1]
            if got is not None:
                att.update(ended=_now(), recovered=True)
                self._charge(rec, att, "author", got.cost_usd, self.est_author_cost)
                if not got.excluded:
                    return self._authored(rec, got)
                att["excluded"] = got.excluded
            else:
                # Its spend is unknown, so it is charged the estimate (never $0).
                self._charge(rec, att, "author", None, self.est_author_cost)
                if not self._fault(rec, att, "interrupted before the creation episode was "
                                   "recorded", "author"):
                    return None
        while len(self._attempts(rec, "author")) < self.max_attempts:
            self._check_budget("author", cell)
            att = self._begin(rec, "author")
            try:
                got = await self.author.author(cell, self._arm(cell.arm))
            except Exception as exc:
                logger.warning("%s: author stage failed: %s", cell.key, exc, exc_info=True)
                self._charge(rec, att, "author", None, self.est_author_cost)   # spend unknown
                if not self._fault(rec, att, f"{type(exc).__name__}: {exc}", "author"):
                    return None
                continue
            att["ended"] = _now()
            att["episode_dir"] = got.episode_dir
            self._charge(rec, att, "author", got.cost_usd, self.est_author_cost)
            if got.excluded:
                if not self._fault(rec, att, f"excluded: {got.excluded}", "author"):
                    return None
                continue
            return self._authored(rec, got)
        rec["status"] = FAULTED
        rec.setdefault("fault", "author: attempts exhausted")
        self._save()
        return None

    def _authored(self, rec: dict[str, Any], got: AuthorOutcome) -> AuthorOutcome:
        """Record the creation episode in the same save that ends its attempt, so a
        crash can never leave a paid episode unrecorded and re-author it."""
        rec["author"] = asdict(got)
        rec["status"] = AUTHORED
        self._save()
        return got

    def _plan(self, cell: Cell, rec: dict[str, Any]) -> grader.GradePlan:
        app = _app_of(cell.case_id)
        if cell.reference:
            case: grader.RunnerCase | None = grader.reference_case(app or "", cell.case_id)
            why = ""
        else:
            a = rec.get("author") or {}
            art = _authored_artifact(self.runs_dir, a)
            case, why = (grader.load_artifact(art) if art
                         else (None, a.get("reason") or a.get("outcome")
                               or "the author created no case"))
        return grader.plan_grade(case, cell.case_id, app_id=app,
                                 control_trial=cell.trial - 1, why_no_case=why)

    def _cell_block(self, cell: Cell, rec: dict[str, Any]) -> dict[str, Any]:
        a = rec.get("author") or {}
        arm = None if cell.reference else self._arm(cell.arm)
        return _board.cell_block(
            kind=self.spec.kind, case_id=cell.case_id, trial=cell.trial,
            source="reference" if cell.reference else "authored",
            experiment=self.spec.name, arm=None if arm is None else arm.name,
            arm_manifest=(a.get("arm_manifest") or (arm.manifest if arm else None)),
            author=None if cell.reference else dict(self.spec.author),
            creation_episode=a.get("episode_dir"),
            creation=None if cell.reference else {
                "outcome": a.get("outcome"), "validity_flags": a.get("validity_flags") or [],
                "cost_usd": rec["cost"].get("author"), "wall_sec": a.get("wall_sec")},
            uptake=(None if cell.reference or self.spec.prediction.uptake is None else
                    {"rule": self.spec.prediction.uptake.rule,
                     **uptake.classify_artifact(_authored_artifact(self.runs_dir, a),
                                                self.spec.prediction.uptake.rule).as_dict()}))

    async def _grade_stage(self, cell: Cell, rec: dict[str, Any]) -> None:
        mpath = grader.manifest_path(self.runs_dir, self.state["run_id"], cell.key)
        plan = self._plan(cell, rec)
        extra = {"cell": self._cell_block(cell, rec)}
        if plan.status != grader.GRADED:
            # no_case_created / not_gradable: decided without a device, at no cost.
            grader.write_manifest(mpath, plan, runner=grader.runner_fingerprint(
                self.spec.runner["agent"], self.spec.runner["model"]),
                run_id=self.state["run_id"], result=grader.grade(plan, {}), extra=extra)
            return self._finish(rec, mpath)
        while True:
            doc = _read_json(mpath)
            if doc and (doc.get("cell") or {}).get("experiment") != self.spec.name:
                doc = None                               # not ours: never "recovered"
            if doc and doc.get("grade") is not None:
                return self._finish(rec, mpath)          # recovered: graded before a crash
            open_atts = [a for a in self._attempts(rec, "grade") if "ended" not in a]
            for att in open_atts:
                att.update(ended=_now(), error="interrupted mid-grade")
            if doc and open_atts:
                # The partial grade's episodes were paid for; the restart redoes them.
                wasted = _manifest_cost(self.runs_dir, doc, self.est_grade_cost)
                rec["cost"]["wasted"] = round(rec["cost"]["wasted"] + wasted, 4)
                self._save()
            if len(self._attempts(rec, "grade")) >= self.max_attempts:
                rec["status"] = FAULTED
                rec.setdefault("fault", "grade: attempts exhausted")
                self._save()
                return None
            self._check_budget("grade", cell)
            att = self._begin(rec, "grade")
            plan = self._plan(cell, rec)        # fresh: `run_grade` appends to its runs
            try:
                mpath = await self.runner.grade(plan, run_id=self.state["run_id"],
                                                manifest_name=cell.key, extra=extra)
            except Exception as exc:
                logger.warning("%s: grade stage failed: %s", cell.key, exc, exc_info=True)
                doc = _read_json(mpath)
                if doc:
                    rec["cost"]["wasted"] = round(rec["cost"]["wasted"] + _manifest_cost(
                        self.runs_dir, doc, self.est_grade_cost), 4)
                if not self._fault(rec, att, f"{type(exc).__name__}: {exc}", "grade"):
                    return None
                continue
            att["ended"] = _now()
            doc = _read_json(mpath) or {}
            cost = _manifest_cost(self.runs_dir, doc, self.est_grade_cost)
            att.update(priced=True, cost_usd=cost)
            rec["cost"]["grade"] = round(rec["cost"]["grade"] + cost, 4)
            if doc.get("grade") is None:
                if not self._fault(rec, att, "the grade finished without a result", "grade"):
                    return None
                continue
            return self._finish(rec, mpath)

    def _finish(self, rec: dict[str, Any], mpath: Path) -> None:
        doc = _read_json(mpath) or {}
        rec["status"] = GRADED
        rec["manifest"] = str(mpath.relative_to(self.runs_dir))
        rec["grade_status"] = (doc.get("grade") or {}).get("status")
        self._save()

    async def run_cell(self, cell: Cell) -> None:
        rec = self.state["cells"][cell.key]
        if rec["status"] in DONE:
            return
        if rec["status"] == PENDING:
            status, reason = gradability(cell.case_id)
            if status == grader.NOT_GRADABLE:
                rec.update(status=SKIPPED, skip_reason=reason)
                self._save()
                return
            if cell.reference:
                rec["status"] = AUTHORED
                self._save()
            else:
                if await self._author_stage(cell, rec) is None:
                    return
        await self._grade_stage(cell, rec)

    async def run(self) -> dict[str, Any]:
        session = {"started": _now(), "max_cost": self.max_cost, "stopped": None,
                   **self.session_meta}
        self.state["sessions"].append(session)
        self._save()
        streak: list[str] = []
        try:
            for cell in plan_cells(self.spec):
                rec = self.state["cells"][cell.key]
                if rec["status"] in DONE:
                    continue
                await self.run_cell(cell)
                if rec["status"] == FAULTED:
                    streak.append(cell.key)
                    if self.max_consecutive_faults and len(streak) >= self.max_consecutive_faults:
                        raise CircuitOpen(
                            f"{len(streak)} cells in a row faulted ({', '.join(streak)}; last: "
                            f"{rec.get('fault')}) — check the device, the MCP server, the "
                            "agent's credentials/credits, then resume with --retry-faulted")
                elif rec["status"] in DONE:
                    streak = []
        except CostCeiling as exc:
            session["stopped"] = f"cost ceiling: {exc}"
        except CircuitOpen as exc:
            session["stopped"] = f"circuit breaker: {exc}"
        except (KeyboardInterrupt, asyncio.CancelledError):
            session["stopped"] = "interrupted"
            raise
        finally:
            session["ended"] = _now()
            session["spent"] = spent(self.state)
            self._save()
        return self.state


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _manifest_cost(runs_dir: Path, doc: dict[str, Any], est_grade: float) -> float:
    """What a grade manifest's episodes cost: the priced ones at their price, every
    unpriced one at a fifth of the grade estimate (a grade is five runs)."""
    c = doc.get("cost")
    if not c:
        plan = grader.plan_from_dict(doc["plan"]) if doc.get("plan") else None
        c = grader._episode_cost(runs_dir, plan) if plan else {}
    per_episode = est_grade / max(1, len(grader.PLAN_ORDER))
    unpriced = max(0, int(c.get("episodes") or 0) - int(c.get("priced_episodes") or 0))
    return round(float(c.get("cost_usd") or 0.0) + unpriced * per_episode, 4)


# ── the report ─────────────────────────────────────────────────────────────────

def report_uptake_rule(prediction: Prediction) -> str | None:
    """The rule a report classifies uptake against: the registered check's, else (a
    harmful-rule registration without one, every v1) QUA-2861's screen-title rule as a
    diagnostic; None for anything else."""
    if prediction.uptake is not None:
        return prediction.uptake.rule
    return uptake.SCREEN_TITLE.id if prediction.name.startswith("harmful-rule") else None


def _authored_artifact(runs_dir: Path | str, author: dict[str, Any] | None) -> Path | str | None:
    """A cell's authored case as this runs dir holds it. The state records `artifact` as
    an ABSOLUTE path at authoring time, so a runs dir that was copied or moved would
    read every case as missing — uptake 0, and a v2 verdict silently INCONCLUSIVE.
    Prefer the same file under `runs_dir / episode_dir` (episode_dir is recorded
    relative); fall back to the recorded path. No recorded artifact = no case."""
    a = author or {}
    art = a.get("artifact")
    if not art:
        return None
    ep = a.get("episode_dir")
    if ep and not Path(ep).is_absolute():
        local = Path(runs_dir) / ep / Path(art).name
        if local.is_file():
            return local
    return art


def report(runs_dir: Path | str, name: str) -> dict[str, Any]:
    """The experiment's read-out from its state file and the manifests it names — and
    nothing else in the runs dir."""
    runs_dir = Path(runs_dir)
    state = load_state(state_path(runs_dir, name))
    if state is None:
        raise FileNotFoundError(f"no experiment {name!r} at {state_path(runs_dir, name)}")
    spec = ExperimentSpec.from_dict(state["spec"])
    grades: dict[str, dict[str, list[dict[str, Any]]]] = {}
    records: list[_board.GradeRecord] = []
    counts: dict[str, int] = {}
    for key, rec in state["cells"].items():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        if rec["status"] != GRADED or not rec.get("manifest"):
            continue
        doc = _read_json(runs_dir / rec["manifest"])
        if not doc or doc.get("grade") is None:
            continue
        cell = doc.get("cell") or {}
        # The experiment cell filter: this experiment, its author and runner, its arms.
        if (cell.get("experiment") != spec.name
                or _board.agent_label(doc.get("runner")) != _board.agent_label(spec.runner)
                or (cell.get("source") != "reference"
                    and _board.agent_label(cell.get("author")) != _board.agent_label(spec.author))):
            logger.warning("%s: manifest %s is not this experiment's cell; ignored", key,
                           rec["manifest"])
            continue
        records.append(_board.GradeRecord(path=runs_dir / rec["manifest"],
                                          run_id=state["run_id"], doc=doc))
        grades.setdefault(rec["arm"], {}).setdefault(rec["case_id"], []).append(doc["grade"])
    # The creation runner's flags on every GRADED authored cell, per arm (QUA-2856's
    # `validity_flags`; a no-case cell counted by its reason): graded, never zeroed, but
    # an arm that shifts them must be visible beside the verdict.
    creation_flags: dict[str, dict[str, int]] = {}
    for rec in state["cells"].values():
        if rec["status"] != GRADED or rec["arm"] == _board.REFERENCE_ARM:
            continue
        a = rec.get("author") or {}
        bucket = creation_flags.setdefault(rec["arm"], {})
        for f in a.get("validity_flags") or []:
            bucket[f] = bucket.get(f, 0) + 1
    # The manipulation check's input (QUA-2864): every finished authored cell's case
    # classified against the registered rule — or, for a harmful-rule registration with
    # none (every v1), QUA-2861's own rule as a printed diagnostic, never a precondition.
    rule_id = report_uptake_rule(spec.prediction)
    uptake_cells: dict[str, dict[str, list[bool]]] = {}
    for rec in state["cells"].values():
        if rec["status"] != GRADED or rec["arm"] == _board.REFERENCE_ARM or not rule_id:
            continue
        taken = uptake.classify_artifact(_authored_artifact(runs_dir, rec.get("author")),
                                         rule_id).taken
        uptake_cells.setdefault(rec["arm"], {}).setdefault(rec["case_id"], []).append(taken)
    frozen = state.get("labels") or {}
    planned = sum(1 for r in state["cells"].values() if r["arm"] != _board.REFERENCE_ARM)
    arm_cells = [r for r in state["cells"].values() if r["arm"] != _board.REFERENCE_ARM]
    complete = all(r["status"] in DONE for r in state["cells"].values())
    faulted = sum(1 for r in arm_cells if r["status"] == FAULTED)
    verdict = evaluate(spec.prediction, grades, spec.briefs, arm_a=spec.arm_a.name,
                       arm_b=spec.arm_b.name, complete=complete, faulted=faulted,
                       planned=planned, strata=frozen.get("strata"),
                       groups=frozen.get("groups"),
                       uptake_cells=uptake_cells if rule_id else None, uptake_rule=rule_id)
    skipped = sorted({(r["case_id"], r.get("skip_reason", "")) for r in state["cells"].values()
                      if r["status"] == SKIPPED})
    walls = [float(((r.get("author") or {}).get("wall_sec")) or 0)
             for r in state["cells"].values()]
    grade_wall = 0.0
    for r in records:
        grade_wall += float((r.doc.get("cost") or {}).get("agent_wall_sec") or 0)
    return {"experiment": spec.name, "kind": spec.kind, "run_id": state["run_id"],
            "registered_at": state["registered_at"], "ungated": state.get("ungated"),
            "arms": {a.name: a.pins() for a in spec.arms},
            "author": spec.author, "runner": spec.runner,
            "briefs": spec.briefs, "trials": spec.trials,
            "brief_trials": spec.brief_trials,
            "cells": counts, "creation_flags": creation_flags,
            "skipped_not_gradable": [{"case_id": c, "reason": w}
                                                      for c, w in skipped],
            # A fault or stop reason is an exception's text and may name a local path
            # (a log under the runs dir); the report is published (report.html/json).
            "faulted_cells": {k: (_board.scrub_paths(r["fault"], runs_dir)
                                  if isinstance(r.get("fault"), str) else r.get("fault"))
                              for k, r in state["cells"].items() if r["status"] == FAULTED},
            "spent": spent(state),
            "sessions": [{**s, "stopped": _board.scrub_paths(s["stopped"], runs_dir)}
                         if isinstance(s.get("stopped"), str) else s
                         for s in state["sessions"]],
            "agent_hours": round((sum(walls) + grade_wall) / 3600, 2),
            "verdict": verdict,
            "board": _board.build_board(records, title=f"experiment {spec.name}")}


def render_report(rep: dict[str, Any]) -> list[str]:
    v = rep["verdict"]
    out = [(f"experiment {rep['experiment']} ({rep['kind']}) · run {rep['run_id']} · "
            f"registered {rep['registered_at']}"),
           f"prediction {v['prediction']} (sha {v['prediction_sha']})"]
    if rep.get("ungated"):
        out.append("!! started with --ungated: the create readiness gate was not READY")
    for name, pins in rep["arms"].items():
        out.append(f"  arm {name}: qualgent_mcp {str(pins.get('qualgent_mcp'))[:12]} · devloop "
                   f"{str(pins.get('devloop'))[:12]} · tools {pins.get('qualgent_tools')}")
    bt = rep.get("brief_trials") or {}
    trials = (f"{rep['trials']} trial(s)" if not bt else
              "trials " + ", ".join(f"{n} brief(s) × {k}" for k, n in
                                    sorted(_count(bt.values()).items(), reverse=True)))
    out.append(f"  author {_board.agent_label(rep['author'])} · runner "
               f"{_board.agent_label(rep['runner'])} · {len(rep['briefs'])} brief(s) · "
               f"{trials}")
    st = v.get("strata") or {}
    out.append(f"  targets: {sum(1 for x in st.values() if x == ALIVE)} alive, "
               f"{sum(1 for x in st.values() if x == DEATH)} death (crash/ANR/stuck)")
    gr = v.get("groups") or {}
    if gr:
        out.append(f"  detection: {sum(1 for x in gr.values() if x == ASSERT)} assert, "
                   f"{sum(1 for x in gr.values() if x == WALK)} walk"
                   + (f", {sum(1 for x in gr.values() if x is None)} unlabelled"
                      if any(x is None for x in gr.values()) else ""))
    out.append("cells: " + ", ".join(f"{k} {n}" for k, n in sorted(rep["cells"].items())))
    for arm, flags in (rep.get("creation_flags") or {}).items():
        if flags:
            out.append(f"  creation flags, arm {arm} (graded anyway): "
                       + ", ".join(f"{f} {n}" for f, n in sorted(flags.items())))
    for s in rep["skipped_not_gradable"]:
        out.append(f"  skipped {s['case_id']}: {s['reason']}")
    for k, why in rep["faulted_cells"].items():
        out.append(f"  faulted {k}: {why}")
    sp = rep["spent"]
    out.append(f"cost: ${sp['total']:.2f} (author ${sp['author']:.2f}, grade ${sp['grade']:.2f},"
               f" abandoned ${sp['wasted']:.2f}) · agent time {rep['agent_hours']} h")
    for s in rep["sessions"]:
        if s.get("stopped"):
            out.append(f"  session {s['started']} stopped: {s['stopped']}")
    out.append("")
    out.append("pooled, A vs B (k/n, Wilson 95%):")
    for axis, d in v["diagnostics"].items():
        out.append(f"  {axis:<13} {_fmt_rate(d['a']):<22} {_fmt_rate(d['b'])}")
    for g, axes in (v.get("by_group") or {}).items():
        out.append(f"  {g} group:")
        for axis, d in axes.items():
            out.append(f"    {axis:<11} {_fmt_rate(d['a']):<22} {_fmt_rate(d['b'])}")
    up = v.get("uptake")
    if up:
        out.append("")
        registered = any(pc.get("kind") == "uptake" for pc in v.get("preconditions") or [])
        out.append(f"uptake of {up['rule']} (authored cases that take the rule, k/n"
                   + ("" if registered else "; diagnostic, not registered") + "):")
        for arm, by in up["arms"].items():
            out.append(f"  arm {arm:<12} " + " · ".join(f"{g} {_fmt_rate(d)}"
                                                        for g, d in by.items()))
    bp = (v.get("brief_power") or {}).get(ASSERT)
    if bp:
        out.append("")
        out.append(f"power per brief, {ASSERT} group (not part of the verdict: trials of one "
                   f"brief are correlated, so the pooled Fisher p overstates the evidence) — "
                   f"B below A on {bp['b_below_a']}/{bp['judged']} brief(s):")
        for r in bp["briefs"]:
            out.append(f"  {r['brief']:<44} A {_fmt_rate(r['a']):<22} B {_fmt_rate(r['b'])}")
    if v.get("preconditions"):
        out.append("")
        out.append("pre-registered preconditions:")
        for pc in v["preconditions"]:
            out.append(f"  [{'MET' if pc['met'] else 'NOT MET'}] {pc['precondition']} — "
                       f"{pc['why']}")
    out.append("")
    out.append("pre-registered expectations:")
    for e in v["expectations"]:
        out.append(f"  [{e['outcome']}] {e['expectation']} — {e['why']}")
        for b in e.get("briefs") or []:
            out.append(f"      {b['brief']:<44} {b['outcome']:<10} A {_fmt_rate(b['a'])}"
                       f"  B {_fmt_rate(b['b'])}")
    out.append("")
    out.append(f"VERDICT: {v['verdict']} — {v['why']} (exit {v['exit_code']})")
    return out


def _count(values: Iterable[Any]) -> dict[Any, int]:
    out: dict[Any, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _fmt_rate(d: dict[str, Any] | None) -> str:
    return "—" if not d else rates.fmt_pct_ci(d["p"], d["ci"], d["k"], d["n"])


# ── the live stages ────────────────────────────────────────────────────────────

def _outcome_from_result(result: Any, runs_dir: Path) -> AuthorOutcome:
    """A creation episode's `result.json` (QUA-2856's `create_verdict` metrics) → the
    outcome the driver records."""
    from ..failures import exclusion_reason, is_excluded
    from ..result import resolve_artifact_dir
    from .fake_api import AUTHORED_CASE_FILE
    m = result.metrics or {}
    d = resolve_artifact_dir(runs_dir, result)
    rel = str(d.relative_to(runs_dir)) if d is not None and d.is_relative_to(runs_dir) else (
        str(d) if d else None)
    art = d / AUTHORED_CASE_FILE if d is not None else None
    has_case = bool(art is not None and art.exists() and m.get("outcome") != "no_case_created")
    # Excluded (env/infra failure, contamination, unclean MCP, rate limit): measured
    # nothing, the driver retries it. A saved case is graded by EXECUTION whatever the
    # runner's transcript heuristics flagged (`dead`, `off_app`): the flags are kept on
    # the cell and counted per arm in the report, never turned into a zero here.
    reason = ""
    if not has_case and m.get("no_case_reason"):
        reason = f"no_case_created: {m['no_case_reason']}"
    return AuthorOutcome(
        episode_dir=rel, artifact=str(art) if has_case else None,
        outcome="case_created" if has_case else "no_case_created", reason=reason,
        excluded=exclusion_reason(m) if is_excluded(m) else "",
        validity_flags=list(m.get("validity_flags") or []),
        cost_usd=m.get("cost_usd") if isinstance(m.get("cost_usd"), int | float) else None,
        wall_sec=result.wall_time_sec,
        arm_manifest=((result.provenance or {}).get("create") or {}).get("arm"))


class LiveAuthor:
    """One creation episode per cell through `qualgent-bench run --mode create`
    (QUA-2856), in a subprocess so this driver depends on its CLI, not its internals.
    The arm is passed pinned to the SHA the experiment registered."""

    def __init__(self, *, runs_dir: Path, work_dir: Path, device: str, mcp_server: str,
                 agent: str, model: str) -> None:
        self.runs_dir, self.work_dir = runs_dir, work_dir
        self.device, self.mcp_server, self.agent, self.model = device, mcp_server, agent, model

    def _rid_file(self, cell: Cell) -> Path:
        return self.work_dir / "cells" / f"{cell.key}.run_id"

    def command(self, cell: Cell, arm: ArmSpec) -> list[str]:
        return [sys.executable, "-c", "from qualgentbench.cli import main; main()", "run",
                "--mode", "create", "--agent", self.agent, "--models", self.model,
                "--case", cell.case_id, "--trials", "1", "--device", self.device,
                "--mcp-server", self.mcp_server, "--runs-dir", str(self.runs_dir),
                "--qualgent-mcp", arm.pinned("qualgent_mcp"), "--devloop", arm.pinned("devloop"),
                "--qualgent-tools", arm.qualgent_tools, "--arm-name", arm.name,
                "--yes", "--plain", "--run-id-file", str(self._rid_file(cell))]

    async def author(self, cell: Cell, arm: ArmSpec) -> AuthorOutcome:
        rid = self._rid_file(cell)
        rid.parent.mkdir(parents=True, exist_ok=True)
        rid.unlink(missing_ok=True)
        log = self.work_dir / "logs" / f"{cell.key}.author.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as fh:
            proc = await asyncio.create_subprocess_exec(*self.command(cell, arm), stdout=fh,
                                                        stderr=asyncio.subprocess.STDOUT)
            rc = await proc.wait()
        got = self.recover(cell)
        if got is None:
            raise RuntimeError(f"`run --mode create` exited {rc} without a creation episode "
                               f"(log: {log})")
        return got

    def recover(self, cell: Cell) -> AuthorOutcome | None:
        from ..leaderboard import load_results
        try:
            run_id = self._rid_file(cell).read_text().strip()
        except OSError:
            return None
        found = [r for r in load_results(self.runs_dir, run_id=run_id)
                 if r.task_type == _board.CREATE_TASK_TYPE]
        if not found:
            return None
        found.sort(key=lambda r: r.started_at)
        return _outcome_from_result(found[-1], self.runs_dir)


class LiveRunner:
    """`grader.run_grade` on one device: the frozen journey runner, five runs."""

    def __init__(self, *, runs_dir: Path, device: str, mcp_server: str, agent: str,
                 model: str) -> None:
        self.runs_dir, self.device, self.mcp_server = runs_dir, device, mcp_server
        self.agent, self.model = agent, model

    async def grade(self, plan: grader.GradePlan, *, run_id: str, manifest_name: str,
                    extra: dict[str, Any]) -> Path:
        return await grader.run_grade(plan, agent=self.agent, model=self.model,
                                      mcp_server=self.mcp_server, device=self.device,
                                      runs_dir=self.runs_dir, run_id=run_id, extra=extra,
                                      manifest_name=manifest_name)


def resolve_arms(arms: tuple[ArmSpec, ArmSpec], base_dir: Path | None = None) -> None:
    """Resolve both arms' pins to SHAs now (a bad ref fails before any spend) and keep
    their manifests — what the experiment registers."""
    from ..config import CreateArm
    from .arm import parse_pin, resolve_arm
    for a in arms:
        tools: Any = a.qualgent_tools
        if tools not in ("template", "all"):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        resolved = resolve_arm(CreateArm(name=a.name, qualgent_mcp=parse_pin(a.qualgent_mcp),
                                         devloop=parse_pin(a.devloop), qualgent_tools=tools),
                               base_dir=base_dir)
        a.manifest = resolved.manifest()


# ── CLI ────────────────────────────────────────────────────────────────────────

def agent_auth_check(spec: ExperimentSpec, *, allow_login: bool) -> tuple[str | None, str]:
    """(codex-cli auth mode, refusal or ""). A paid run on codex-cli must authenticate
    with an API key: with no CODEX_API_KEY / OPENAI_API_KEY the adapter would copy the
    operator's own codex login, which bills (and rate-limits) a ChatGPT workspace —
    this bit QUA-2850, whose worktrees have no `.env`. None when no stage runs codex.
    QGB_ALLOW_CODEX_LOGIN counts as `allow_login` — it is what the flag sets for the
    episodes themselves, whose adapter refuses the login without it (QUA-2868)."""
    if "codex-cli" not in (spec.author.get("agent"), spec.runner.get("agent")):
        return None, ""
    from ..adapters.codex_cli import AUTH_API_KEY, CodexCliAdapter, login_allowed
    mode = CodexCliAdapter.configured_auth_mode()
    if mode == AUTH_API_KEY or allow_login or login_allowed():
        return mode, ""
    return mode, (f"codex-cli has no API key (CODEX_API_KEY / OPENAI_API_KEY, e.g. the oss "
                  f"`.env` in the cwd) and would run on: {mode}; set the key, or pass "
                  "--allow-codex-login (or set QGB_ALLOW_CODEX_LOGIN=1) to bill the "
                  "operator's account login (recorded)")


def _spec_from_args(args: argparse.Namespace) -> ExperimentSpec:
    prediction = load_prediction(args.prediction)
    briefs = (list(args.case) if args.case
              else load_subset(Path(args.briefs) if args.briefs else default_subset(prediction)))
    arms = (ArmSpec(args.a_name, args.a_qualgent_mcp or "", args.a_devloop or "",
                    args.qualgent_tools),
            ArmSpec(args.b_name, args.b_qualgent_mcp or "", args.b_devloop or "",
                    args.qualgent_tools))
    if arms[0].name == arms[1].name or _board.REFERENCE_ARM in (arms[0].name, arms[1].name):
        raise ValueError("the two arms need distinct names, neither 'reference'")
    if bad := check_design(prediction, briefs):
        raise ValueError(f"{prediction.ref} cannot run on these briefs: " + "; ".join(bad))
    # The prediction's own trials design (per detection group) unless --trials overrides
    # it for every brief; a prediction without one runs DEFAULT_TRIALS.
    brief_trials = None if args.trials is not None else design_trials(prediction, briefs)
    trials = (args.trials if args.trials is not None
              else max(brief_trials.values()) if brief_trials else DEFAULT_TRIALS)
    return ExperimentSpec(name=args.experiment, arms=arms, briefs=briefs, trials=trials,
                          author={"agent": args.agent, "model": args.author_model},
                          runner={"agent": args.agent, "model": args.runner_model},
                          prediction=prediction,
                          kind=_board.SMOKE if args.smoke else _board.CANONICAL,
                          reference_trials=args.reference_trials,
                          brief_trials=brief_trials)


def plan_cost(spec: ExperimentSpec, gr: dict[str, tuple[str, str]] | None, *, author: float,
              grade: float) -> float:
    """The experiment priced at `author` + `grade` per cell (a reference cell pays the
    grade only). With `gr` (gradability per brief) a not-gradable brief is skipped and
    pays nothing; without it every cell is priced (the full design)."""
    payable = [c for c in plan_cells(spec)
               if gr is None or gr[c.case_id][0] != grader.NOT_GRADABLE]
    return round(sum(1 for c in payable if not c.reference) * author + len(payable) * grade, 2)


def _print_plan(spec: ExperimentSpec, gr: dict[str, tuple[str, str]], args) -> None:
    cells = plan_cells(spec)
    arm_cells = [c for c in cells if not c.reference]
    groups = {b: detection_group(b) for b in spec.briefs}
    print(f"experiment {spec.name} ({spec.kind}): {len(spec.briefs)} brief(s), "
          f"{len(arm_cells)} arm cell(s)"
          + (f" + {len(cells) - len(arm_cells)} reference cell(s)"
             if len(cells) > len(arm_cells) else ""))
    for g in (ASSERT, WALK, None):
        bs = [b for b in spec.briefs if groups[b] == g]
        if not bs:
            continue
        n = sum(1 for c in arm_cells if c.case_id in bs)
        ts = sorted({spec.trials_for(b) for b in bs})
        role = {ASSERT: "DROP group", WALK: "FLAT group", None: "unlabelled"}[g]
        print(f"  {g or '—'} ({role}): {len(bs)} brief(s) × "
              f"{'/'.join(map(str, ts))} trial(s) × 2 arms = {n} cell(s)")
    print(f"prediction {spec.prediction.ref} (sha {spec.prediction.sha}):")
    if spec.prediction.uptake is not None:
        print(f"  precondition: {spec.prediction.uptake.label}")
        print(f"  rule {spec.prediction.uptake.rule}: "
              f"{uptake.RULES[spec.prediction.uptake.rule].text}")
    for pc in spec.prediction.preconditions:
        print(f"  precondition: {pc.label}")
    for e in spec.prediction.expectations:
        print(f"  {e.label}")
    strata = {b: target_stratum(b) for b in spec.briefs}
    n_death = sum(1 for v in strata.values() if v == DEATH)
    print(f"targets: {len(strata) - n_death} alive, {n_death} death (crash/ANR/stuck)")
    if spec.prediction is POSITIVE_CONTROL and n_death:
        print(f"!! {POSITIVE_CONTROL.ref} is the owner's FIRST literal registration, not the "
              f"default: QUA-2859's simulation expects it MISSED on the {n_death} "
              f"death-target brief(s) (a crash still fails a walked case). The owner's "
              f"decision (2026-10-01) is --prediction {POSITIVE_CONTROL_MECHANISM.name}.")
    for a in spec.arms:
        print(f"arm {a.name}: {a.qualgent_mcp or '(unset)'} · {a.devloop or '(unset)'} → "
              f"{a.pins()}")
    bad = {c: r for c, (s, r) in gr.items() if s == grader.NOT_GRADABLE}
    print(f"gradable briefs: {len(spec.briefs) - len(bad)}/{len(spec.briefs)}")
    for c, r in bad.items():
        print(f"  NOT GRADABLE {c}: {r}")
    measured = plan_cost(spec, None, author=MEASURED_AUTHOR_COST, grade=MEASURED_GRADE_COST)
    payable = plan_cost(spec, gr, author=MEASURED_AUTHOR_COST, grade=MEASURED_GRADE_COST)
    conservative = plan_cost(spec, None, author=args.est_author_cost, grade=args.est_grade_cost)
    print(f"estimated cost at measured actuals: ${measured:.2f} for all {len(cells)} cell(s) "
          f"(author ${MEASURED_AUTHOR_COST:.2f} + grade ${MEASURED_GRADE_COST:.2f} per cell) — "
          f"{'within' if measured <= args.max_cost else 'ABOVE'} the ceiling"
          + (f"; ${payable:.2f} payable today ({len(bad)} brief(s) not gradable, skipped)"
             if bad else ""))
    print(f"conservative estimate: ${conservative:.2f} (author ${args.est_author_cost:.2f} + "
          f"grade ${args.est_grade_cost:.2f} per cell: the driver's per-stage check until a "
          f"stage is priced, then the observed mean)")
    print(f"ceiling --max-cost ${args.max_cost:.2f} (hard cap ${HARD_COST_CAP:.2f})")


DEFAULT_TRIALS = 3


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="run_create_ab.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="plan (without --yes) or run/resume an experiment")
    r.add_argument("--experiment", help="the experiment's name (its state key; required "
                                        "unless --plan)")
    for side, default in (("a", "A"), ("b", "B")):
        r.add_argument(f"--{side}-name", default=default)
        r.add_argument(f"--{side}-qualgent-mcp", metavar="SRC@REF")
        r.add_argument(f"--{side}-devloop", metavar="SRC@REF")
    r.add_argument("--qualgent-tools", default="template")
    r.add_argument("--briefs", default=None,
                   help="subset YAML (default: the prediction's own — "
                        "data/create/positive-control-v3.yaml for "
                        f"{POSITIVE_CONTROL_MECHANISM_V3.ref}, positive-control-v2.yaml "
                        f"for {POSITIVE_CONTROL_MECHANISM_V2.ref}, else "
                        "data/create/positive-control.yaml)")
    r.add_argument("--case", action="append", help="brief case id (repeatable; overrides --briefs)")
    r.add_argument("--trials", type=int, default=None,
                   help="trials for EVERY brief. Default: the prediction's per-group design "
                        f"(mechanism: assert 2, walk 1), else {DEFAULT_TRIALS}")
    r.add_argument("--reference-trials", type=int, default=0,
                   help="also grade each brief's reference case on the first N trials")
    r.add_argument("--agent", default="codex-cli")
    r.add_argument("--author-model", default="gpt-6-astra")
    r.add_argument("--runner-model", default="gpt-6-astra")
    r.add_argument("--prediction", default=DEFAULT_PREDICTION.name,
                   help="registered name or JSON file. Default: the owner's mechanism-based "
                        f"registration ({DEFAULT_PREDICTION.ref}). Also registered: "
                        + ", ".join(p.ref for p in (POSITIVE_CONTROL_MECHANISM,
                                                    POSITIVE_CONTROL_MECHANISM_V2,
                                                    POSITIVE_CONTROL,
                                                    POSITIVE_CONTROL_STRATIFIED,
                                                    POSITIVE_CONTROL_AGGREGATE)))
    r.add_argument("--device")
    r.add_argument("--mcp-server")
    r.add_argument("--runs-dir", default=None)
    r.add_argument("--max-cost", type=float, default=None,
                   help=f"USD ceiling (default: the prediction's own — "
                        + ", ".join(f"{k} ${v:g}" for k, v in
                                    DEFAULT_MAX_COST_BY_PREDICTION.items())
                        + f", else ${DEFAULT_MAX_COST:g}; above ${HARD_COST_CAP:g} is refused)")
    r.add_argument("--est-author-cost", type=float, default=EST_AUTHOR_COST)
    r.add_argument("--est-grade-cost", type=float, default=EST_GRADE_COST)
    r.add_argument("--max-attempts", type=int, default=2)
    r.add_argument("--max-consecutive-faults", type=int, default=2,
                   help="stop the session (INCOMPLETE) after this many cells in a row end "
                        "faulted — an outage, not N independent failures (0 = never)")
    r.add_argument("--retry-faulted", action="store_true",
                   help="on resume: give every faulted cell fresh attempts (the old ones are "
                        "kept as superseded and stay on the bill; recorded on the session)")
    r.add_argument("--allow-codex-login", action="store_true",
                   help="start although codex-cli has no API key and would run on the "
                        "operator's account login (recorded on the session)")
    r.add_argument("--smoke", action="store_true",
                   help="mark every cell smoke: never on the canonical board")
    r.add_argument("--allow-not-gradable", action="store_true",
                   help="start although some briefs are not gradable (they are skipped)")
    r.add_argument("--ungated", action="store_true",
                   help="start although the create readiness gate is not READY (recorded)")
    r.add_argument("--plan", action="store_true",
                   help="print the cells, the prediction and the estimate, then exit 0: no "
                        "arm is resolved and nothing is registered or spent")
    r.add_argument("--yes", action="store_true", help="spend: without it the plan is printed")
    p = sub.add_parser("report", help="the experiment's read-out; exit code = its verdict")
    p.add_argument("--experiment", required=True)
    p.add_argument("--runs-dir", default=None)
    p.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    from ..config import resolve_runs_dir
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--plan"]:                       # `run_create_ab.py --plan [...]`
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "report":
        runs_dir = resolve_runs_dir(args.runs_dir)
        try:
            rep = report(runs_dir, args.experiment)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return EXIT_REFUSED
        if args.json:
            print(json.dumps(rep, indent=2, default=str))
        else:
            print("\n".join(render_report(rep)))
            print("\n".join(_board.render_text(rep["board"])))
        return rep["verdict"]["exit_code"]

    if args.max_cost is None:
        try:
            args.max_cost = default_max_cost(load_prediction(args.prediction))
        except ValueError:
            args.max_cost = DEFAULT_MAX_COST
    if args.max_cost > HARD_COST_CAP:
        print(f"refused: --max-cost ${args.max_cost:.2f} is above the hard cap "
              f"${HARD_COST_CAP:.2f}", file=sys.stderr)
        return EXIT_REFUSED
    if args.plan:
        args.experiment = args.experiment or "plan"
        try:
            spec = _spec_from_args(args)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        _print_plan(spec, {c: gradability(c) for c in spec.briefs}, args)
        print("plan only: nothing registered, nothing spent")
        return 0
    if not args.experiment:
        print("--experiment is required", file=sys.stderr)
        return EXIT_REFUSED

    from ..dotenv import load_dotenv
    load_dotenv()                     # before the auth check: the key lives in the .env
    runs_dir = resolve_runs_dir(args.runs_dir)
    path = state_path(runs_dir, args.experiment)
    state = load_state(path)
    have_arms = all((args.a_qualgent_mcp, args.a_devloop, args.b_qualgent_mcp, args.b_devloop))
    if state is not None and not have_arms:
        spec = ExperimentSpec.from_dict(state["spec"])        # a bare resume
    else:
        if not have_arms:
            print("--a-qualgent-mcp/--a-devloop/--b-qualgent-mcp/--b-devloop are required "
                  "for a new experiment", file=sys.stderr)
            return EXIT_REFUSED
        try:
            spec = _spec_from_args(args)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        from .arm import ArmError
        try:
            resolve_arms(spec.arms)
        except ArmError as exc:
            print(f"arm: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        if state is not None and (diff := registration_diff(state, spec)):
            print(f"refused: experiment {spec.name} was registered differently — a "
                  "pre-registered experiment cannot change after it started:\n  "
                  + "\n  ".join(diff), file=sys.stderr)
            return EXIT_REFUSED

    if state is not None and (diff := environment_diff(state, spec)):
        print(f"refused: experiment {spec.name} was registered under a different corpus / "
              "runner / creation brief — its cells would mix two regimes:\n  "
              + "\n  ".join(diff), file=sys.stderr)
        return EXIT_REFUSED

    gr = {c: gradability(c) for c in spec.briefs}
    _print_plan(spec, gr, args)
    from ..adapters.codex_cli import allow_login, login_allowed
    if args.allow_codex_login:
        # The episodes run in this process (the grader) and in `qualgent-bench run`
        # subprocesses (the author); both read the opt-in from the environment, and the
        # codex adapter refuses the login without it (QUA-2868).
        allow_login()
    allow_codex_login = login_allowed()
    auth_mode, auth_problem = agent_auth_check(spec, allow_login=allow_codex_login)
    if auth_mode:
        print(f"codex-cli auth: {auth_mode}")
    gate = _board.read_gate(runs_dir)
    print(f"create readiness gate: {gate.state} — {gate.detail}")
    if not args.yes:
        print("not started: pass --yes (with --device, --mcp-server) to spend")
        return 1
    problems = []
    if not (args.device and args.mcp_server):
        problems.append("--device and --mcp-server are required")
    if any(s == grader.NOT_GRADABLE for s, _ in gr.values()) and not args.allow_not_gradable:
        problems.append("some briefs are not gradable (above): derive their controls first, "
                        "or pass --allow-not-gradable to skip them (the verdict then counts "
                        "them as unscored)")
    if not gate.ready and not args.ungated:
        problems.append(f"the create readiness gate is {gate.state}; pass --ungated to run "
                        "anyway (recorded on the experiment)")
    if auth_problem:
        problems.append(auth_problem)
    if problems:
        print("refused:\n  " + "\n  ".join(problems), file=sys.stderr)
        return EXIT_REFUSED
    if state is None:
        from ..scheduler import new_run_id
        state = new_state(spec, run_id=new_run_id(), ungated=not gate.ready)
        save_state(path, state)
        print(f"registered: {path}")
    work = path.parent / spec.name
    driver = Driver(spec=spec, runs_dir=runs_dir, max_cost=args.max_cost,
                    max_attempts=args.max_attempts, est_author_cost=args.est_author_cost,
                    est_grade_cost=args.est_grade_cost,
                    ungated=not gate.ready,
                    author=LiveAuthor(runs_dir=runs_dir, work_dir=work, device=args.device,
                                      mcp_server=args.mcp_server, agent=spec.author["agent"],
                                      model=spec.author["model"]),
                    runner=LiveRunner(runs_dir=runs_dir, device=args.device,
                                      mcp_server=args.mcp_server, agent=spec.runner["agent"],
                                      model=spec.runner["model"]),
                    state=state, max_consecutive_faults=args.max_consecutive_faults,
                    session_meta={"agent_auth": auth_mode,
                                  "allow_codex_login": allow_codex_login})
    if args.retry_faulted:
        reset = driver.retry_faulted()
        driver.session_meta["retried_faulted"] = reset
        print(f"--retry-faulted: {len(reset)} faulted cell(s) get fresh attempts")
    asyncio.run(driver.run())
    rep = report(runs_dir, spec.name)
    print("\n".join(render_report(rep)))
    return rep["verdict"]["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
