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
The default is the epic's harmful-rule positive control (QUA-2861):

    power          DOWN on every brief, and DOWN pooled
    repeatability  FLAT pooled
    specificity    FLAT pooled

How an expectation is judged (B relative to A; rates over the artifacts where the axis
was SCORED — an excluded run leaves an axis unscored, never 0; Wilson 95% intervals):

    pooled DOWN / UP  MET iff the intervals are disjoint in the predicted direction.
                      B moving the OTHER way, or not separating, is NOT MET.
    pooled FLAT       MET iff the intervals overlap.
    each DOWN / UP    per brief: UNSCORED (an arm has no scored artifact), FLOOR (A
                      already at 0 for DOWN / 1 for UP: there is no room to move),
                      MOVED (B strictly beyond A in the predicted direction) or
                      NOT_MOVED. NOT MET iff any brief is NOT_MOVED; INCONCLUSIVE iff
                      fewer than `min_informative_share` of the subset's briefs MOVED.
    each FLAT         per brief: the intervals overlap.

The verdict, and the exit code it is bound to:

    INCOMPLETE    4   cells are still to run (a cost-ceiling stop, an interrupt). No
                      partial verdict is computed: there is no peeking.
    MISSED        1   any expectation NOT MET — wrong direction included. A MISSED
                      prediction is reported as MISSED, never reinterpreted.
    INCONCLUSIVE  3   no expectation failed, but one could not be judged (no scored
                      artifacts, too few informative briefs), or more than
                      `max_faulted_share` of the cells faulted.
    DETECTED      0   every expectation MET.

Cells and order. A cell is (arm, brief, trial). Trials are the outer loop and the two
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
and `faulted`.

Cost ceiling. `--max-cost` USD is checked before every paid stage: spent so far (every
attempt's recorded cost; an unpriced episode is charged the estimate; a grade restarted
after a crash keeps the cost of its abandoned episodes) plus the stage's estimate (the
mean of the stages already priced, else `--est-author-cost` / `--est-grade-cost`). A
stage that would cross it is not started; the run stops INCOMPLETE and a resume with a
higher ceiling continues.

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
from . import grader

logger = logging.getLogger(__name__)

STATE_SCHEMA = "qualgentbench.create.ab/1"
#: Bump when the judging rules above change: it is part of the pre-registration hash.
EVALUATOR_VERSION = 1

DOWN, UP, FLAT = "down", "up", "flat"
EACH, POOLED = "each", "pooled"
AXES = ("power", "repeatability", "specificity", "lint", "strong", "strong_exec")

DETECTED, MISSED, INCONCLUSIVE, INCOMPLETE = "DETECTED", "MISSED", "INCONCLUSIVE", "INCOMPLETE"
EXIT = {DETECTED: 0, MISSED: 1, INCONCLUSIVE: 3, INCOMPLETE: 4}
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


# ── the prediction ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Expectation:
    axis: str
    direction: str                       # down | up | flat   (B relative to A)
    scope: str = POOLED                  # each | pooled

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"axis {self.axis!r} is not one of {AXES}")
        if self.direction not in (DOWN, UP, FLAT):
            raise ValueError(f"direction {self.direction!r} is not down|up|flat")
        if self.scope not in (EACH, POOLED):
            raise ValueError(f"scope {self.scope!r} is not each|pooled")

    @property
    def label(self) -> str:
        where = "every brief" if self.scope == EACH else "pooled"
        return f"{self.axis} {self.direction} ({where})"


@dataclass(frozen=True)
class Prediction:
    name: str
    expectations: tuple[Expectation, ...]
    min_informative_share: float = 0.5
    max_faulted_share: float = 0.10
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "expectations": [asdict(e) for e in self.expectations],
                "min_informative_share": self.min_informative_share,
                "max_faulted_share": self.max_faulted_share, "note": self.note,
                "evaluator_version": EVALUATOR_VERSION}

    @property
    def sha(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()[:12]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Prediction:
        return cls(name=str(d["name"]),
                   expectations=tuple(Expectation(**e) for e in d["expectations"]),
                   min_informative_share=float(d.get("min_informative_share", 0.5)),
                   max_faulted_share=float(d.get("max_faulted_share", 0.10)),
                   note=str(d.get("note") or ""))


#: The epic's positive control (QUA-2850 / QUA-2861), pre-registered here before any
#: live data exists: arm B's creation guide carries a damaging rule, so B's authored
#: cases lose power on every brief while staying as repeatable and as specific.
POSITIVE_CONTROL = Prediction(
    name="harmful-rule-positive-control",
    expectations=(Expectation("power", DOWN, EACH), Expectation("power", DOWN, POOLED),
                  Expectation("repeatability", FLAT, POOLED),
                  Expectation("specificity", FLAT, POOLED)),
    note=("QUA-2861: arm B = QualGent-MCP throwaway/createbench-v2-harmful-rule. Power down "
          "on every brief of data/create/positive-control.yaml x 3 trials x 2 arms; "
          "repeatability and specificity flat (Wilson intervals overlap)."))

PREDICTIONS = {POSITIVE_CONTROL.name: POSITIVE_CONTROL, "positive-control": POSITIVE_CONTROL}


def load_prediction(spec: str) -> Prediction:
    """A registered name, or a JSON file with `Prediction.as_dict()`'s fields."""
    if spec in PREDICTIONS:
        return PREDICTIONS[spec]
    p = Path(spec)
    if p.is_file():
        return Prediction.from_dict(json.loads(p.read_text()))
    raise ValueError(f"unknown prediction {spec!r}: one of {sorted(PREDICTIONS)} or a JSON file")


# ── judging (pure) ─────────────────────────────────────────────────────────────

def axis_rate(grades: Iterable[dict[str, Any]], axis: str) -> rates.Rate | None:
    """k/n over the grades where `axis` was scored (True/False). A NOT_GRADABLE grade
    has every axis None; n/a and None never enter n."""
    vals = [(g.get("axes") or {}).get(axis) for g in grades]
    k = sum(1 for v in vals if v is True)
    n = sum(1 for v in vals if v is True or v is False)
    return rates.rate(k, n)


def _rate_dict(r: rates.Rate | None) -> dict[str, Any] | None:
    return None if r is None else {"k": r.k, "n": r.n, "p": round(r.p, 4),
                                   "ci": [round(r.lo, 4), round(r.hi, 4)]}


def _overlap(a: rates.Rate, b: rates.Rate) -> bool:
    return a.lo <= b.hi and b.lo <= a.hi


def judge_pooled(direction: str, a: rates.Rate | None, b: rates.Rate | None) -> tuple[str, str]:
    if a is None or b is None:
        return INCONCLUSIVE, "an arm has no scored artifact on this axis"
    if direction == FLAT:
        return ((MET, "intervals overlap") if _overlap(a, b)
                else (NOT_MET, "the arms separated — not flat"))
    better = (b.p < a.p) if direction == DOWN else (b.p > a.p)
    if not better:
        return NOT_MET, ("B moved the other way" if b.p != a.p else "no difference")
    separated = (b.hi < a.lo) if direction == DOWN else (b.lo > a.hi)
    return ((MET, "intervals disjoint in the predicted direction") if separated
            else (NOT_MET, "right direction, but the intervals overlap"))


def judge_brief(direction: str, a: rates.Rate | None, b: rates.Rate | None) -> str:
    if a is None or b is None:
        return UNSCORED
    if direction == FLAT:
        return MOVED if _overlap(a, b) else NOT_MOVED       # "moved" = held flat here
    if (direction == DOWN and a.p == 0) or (direction == UP and a.p == 1):
        return FLOOR
    moved = (b.p < a.p) if direction == DOWN else (b.p > a.p)
    return MOVED if moved else NOT_MOVED


def evaluate(prediction: Prediction, grades: dict[str, dict[str, list[dict[str, Any]]]],
             briefs: list[str], *, arm_a: str, arm_b: str, complete: bool = True,
             faulted: int = 0, planned: int = 0) -> dict[str, Any]:
    """Judge `prediction` on `grades[arm][brief] = [grade, …]` (finished grades only).
    Pure: the live driver, the report and the tests all end here."""
    def pooled(arm: str, axis: str) -> rates.Rate | None:
        return axis_rate([g for b in briefs for g in grades.get(arm, {}).get(b, [])], axis)

    def per_brief(arm: str, brief: str, axis: str) -> rates.Rate | None:
        return axis_rate(grades.get(arm, {}).get(brief, []), axis)

    results = []
    for e in prediction.expectations:
        if e.scope == POOLED:
            a, b = pooled(arm_a, e.axis), pooled(arm_b, e.axis)
            outcome, why = judge_pooled(e.direction, a, b)
            results.append({"expectation": e.label, **asdict(e), "outcome": outcome,
                            "why": why, "a": _rate_dict(a), "b": _rate_dict(b)})
            continue
        rows, counts = [], {MOVED: 0, NOT_MOVED: 0, FLOOR: 0, UNSCORED: 0}
        for brief in briefs:
            a, b = per_brief(arm_a, brief, e.axis), per_brief(arm_b, brief, e.axis)
            o = judge_brief(e.direction, a, b)
            counts[o] += 1
            rows.append({"brief": brief, "outcome": o, "a": _rate_dict(a), "b": _rate_dict(b)})
        need = math.ceil(prediction.min_informative_share * len(briefs))
        if counts[NOT_MOVED]:
            outcome = NOT_MET
            why = (f"{counts[NOT_MOVED]} brief(s) did not move: "
                   + ", ".join(r["brief"] for r in rows if r["outcome"] == NOT_MOVED))
        elif counts[MOVED] < need:
            outcome = INCONCLUSIVE
            why = (f"only {counts[MOVED]} of {len(briefs)} brief(s) informative (need {need}; "
                   f"floor {counts[FLOOR]}, unscored {counts[UNSCORED]})")
        else:
            outcome, why = MET, f"{counts[MOVED]} of {len(briefs)} brief(s) moved"
        results.append({"expectation": e.label, **asdict(e), "outcome": outcome, "why": why,
                        "counts": counts, "briefs": rows})

    diagnostics = {axis: {"a": _rate_dict(pooled(arm_a, axis)),
                          "b": _rate_dict(pooled(arm_b, axis))} for axis in AXES}
    faulted_share = (faulted / planned) if planned else 0.0
    if not complete:
        verdict, why = INCOMPLETE, "cells are still to run — no verdict before the last cell"
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
            "prediction": prediction.name, "prediction_sha": prediction.sha,
            "expectations": results, "diagnostics": diagnostics,
            "faulted": faulted, "planned": planned}


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

    @property
    def arm_a(self) -> ArmSpec:
        return self.arms[0]

    @property
    def arm_b(self) -> ArmSpec:
        return self.arms[1]

    def registration(self) -> dict[str, Any]:
        """Everything a resume must not change."""
        return {"name": self.name, "arms": [a.pins() for a in self.arms],
                "briefs": list(self.briefs), "trials": self.trials, "author": self.author,
                "runner": self.runner, "kind": self.kind,
                "reference_trials": self.reference_trials,
                "prediction": self.prediction.as_dict(), "prediction_sha": self.prediction.sha}

    def as_dict(self) -> dict[str, Any]:
        return {**self.registration(), "arm_specs": [asdict(a) for a in self.arms]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExperimentSpec:
        arms = tuple(ArmSpec(**a) for a in d["arm_specs"])
        return cls(name=d["name"], arms=(arms[0], arms[1]), briefs=list(d["briefs"]),
                   trials=int(d["trials"]), author=dict(d["author"]),
                   runner=dict(d["runner"]), prediction=Prediction.from_dict(d["prediction"]),
                   kind=d.get("kind", _board.CANONICAL),
                   reference_trials=int(d.get("reference_trials") or 0))


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
    runs first; the reference cells (if any) after the arms of their trial."""
    out: list[Cell] = []
    a, b = spec.arm_a.name, spec.arm_b.name
    for t in range(1, spec.trials + 1):
        for i, brief in enumerate(spec.briefs):
            first, second = (a, b) if (i + t) % 2 == 0 else (b, a)
            out += [Cell(first, brief, t), Cell(second, brief, t)]
            if t <= spec.reference_trials:
                out.append(Cell(_board.REFERENCE_ARM, brief, t))
    return out


def load_subset(path: Path = DEFAULT_SUBSET) -> list[str]:
    import yaml
    doc = yaml.safe_load(Path(path).read_text()) or {}
    return [str(b["case"]) for b in doc.get("briefs") or []]


@functools.lru_cache(maxsize=None)
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


def new_state(spec: ExperimentSpec, *, run_id: str, ungated: bool) -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "spec": spec.as_dict(), "registered_at": _now(),
            "run_id": run_id, "corpus": corpus.stamp(), "ungated": ungated,
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


def registration_diff(state: dict[str, Any], spec: ExperimentSpec) -> list[str]:
    """What differs between the registration frozen in `state` and `spec` — compared
    against the STORED fields, so an evaluator change (its version is in the
    prediction hash) shows even on a bare resume."""
    now = spec.registration()
    was = {k: state["spec"].get(k) for k in now}
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


# ── the stages ─────────────────────────────────────────────────────────────────

@dataclass
class AuthorOutcome:
    """One creation episode as the driver needs it."""
    episode_dir: str | None              # relative to the runs dir
    artifact: str | None                 # authored_case.json (absolute), None = no case
    outcome: str = "case_created"        # case_created | no_case_created
    excluded: str = ""                   # non-empty = measured nothing: retry
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
        return [a for a in rec["attempts"] if a.get("stage") == stage]

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
            art = (rec.get("author") or {}).get("artifact")
            case, why = (grader.load_artifact(art) if art
                         else (None, (rec.get("author") or {}).get("outcome")
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
                "cost_usd": rec["cost"].get("author"), "wall_sec": a.get("wall_sec")})

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
        session = {"started": _now(), "max_cost": self.max_cost, "stopped": None}
        self.state["sessions"].append(session)
        self._save()
        try:
            for cell in plan_cells(self.spec):
                await self.run_cell(cell)
        except CostCeiling as exc:
            session["stopped"] = f"cost ceiling: {exc}"
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
    planned = sum(1 for r in state["cells"].values() if r["arm"] != _board.REFERENCE_ARM)
    arm_cells = [r for r in state["cells"].values() if r["arm"] != _board.REFERENCE_ARM]
    complete = all(r["status"] in DONE for r in state["cells"].values())
    faulted = sum(1 for r in arm_cells if r["status"] == FAULTED)
    verdict = evaluate(spec.prediction, grades, spec.briefs, arm_a=spec.arm_a.name,
                       arm_b=spec.arm_b.name, complete=complete, faulted=faulted,
                       planned=planned)
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
            "cells": counts, "skipped_not_gradable": [{"case_id": c, "reason": w}
                                                      for c, w in skipped],
            "faulted_cells": {k: r.get("fault") for k, r in state["cells"].items()
                              if r["status"] == FAULTED},
            "spent": spent(state), "sessions": state["sessions"],
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
    out.append(f"  author {_board.agent_label(rep['author'])} · runner "
               f"{_board.agent_label(rep['runner'])} · {len(rep['briefs'])} brief(s) × "
               f"{rep['trials']} trial(s)")
    out.append("cells: " + ", ".join(f"{k} {n}" for k, n in sorted(rep["cells"].items())))
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
    return AuthorOutcome(
        episode_dir=rel, artifact=str(art) if has_case else None,
        outcome="case_created" if has_case else "no_case_created",
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

def _spec_from_args(args: argparse.Namespace) -> ExperimentSpec:
    briefs = list(args.case) if args.case else load_subset(Path(args.briefs))
    arms = (ArmSpec(args.a_name, args.a_qualgent_mcp or "", args.a_devloop or "",
                    args.qualgent_tools),
            ArmSpec(args.b_name, args.b_qualgent_mcp or "", args.b_devloop or "",
                    args.qualgent_tools))
    if arms[0].name == arms[1].name or _board.REFERENCE_ARM in (arms[0].name, arms[1].name):
        raise ValueError("the two arms need distinct names, neither 'reference'")
    return ExperimentSpec(name=args.experiment, arms=arms, briefs=briefs, trials=args.trials,
                          author={"agent": args.agent, "model": args.author_model},
                          runner={"agent": args.agent, "model": args.runner_model},
                          prediction=load_prediction(args.prediction),
                          kind=_board.SMOKE if args.smoke else _board.CANONICAL,
                          reference_trials=args.reference_trials)


def _print_plan(spec: ExperimentSpec, gr: dict[str, tuple[str, str]], args) -> None:
    cells = plan_cells(spec)
    arm_cells = [c for c in cells if not c.reference]
    payable = [c for c in cells if gr[c.case_id][0] != grader.NOT_GRADABLE]
    est = (sum(1 for c in payable if not c.reference) * args.est_author_cost
           + len(payable) * args.est_grade_cost)
    print(f"experiment {spec.name} ({spec.kind}): {len(spec.briefs)} brief(s) × {spec.trials} "
          f"trial(s) × 2 arms = {len(arm_cells)} cell(s)"
          + (f" + {len(cells) - len(arm_cells)} reference cell(s)"
             if len(cells) > len(arm_cells) else ""))
    print(f"prediction {spec.prediction.name} (sha {spec.prediction.sha}):")
    for e in spec.prediction.expectations:
        print(f"  {e.label}")
    for a in spec.arms:
        print(f"arm {a.name}: {a.qualgent_mcp} · {a.devloop} → {a.pins()}")
    bad = {c: r for c, (s, r) in gr.items() if s == grader.NOT_GRADABLE}
    print(f"gradable briefs: {len(spec.briefs) - len(bad)}/{len(spec.briefs)}")
    for c, r in bad.items():
        print(f"  NOT GRADABLE {c}: {r}")
    print(f"estimated cost ${est:.2f} (author ${args.est_author_cost:.2f} + grade "
          f"${args.est_grade_cost:.2f} per gradable cell) · ceiling --max-cost "
          f"{args.max_cost}")


def main(argv: list[str] | None = None) -> int:
    from ..config import resolve_runs_dir
    ap = argparse.ArgumentParser(prog="run_create_ab.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="plan (without --yes) or run/resume an experiment")
    r.add_argument("--experiment", required=True, help="the experiment's name (its state key)")
    for side, default in (("a", "A"), ("b", "B")):
        r.add_argument(f"--{side}-name", default=default)
        r.add_argument(f"--{side}-qualgent-mcp", metavar="SRC@REF")
        r.add_argument(f"--{side}-devloop", metavar="SRC@REF")
    r.add_argument("--qualgent-tools", default="template")
    r.add_argument("--briefs", default=str(DEFAULT_SUBSET),
                   help="subset YAML (default: data/create/positive-control.yaml)")
    r.add_argument("--case", action="append", help="brief case id (repeatable; overrides --briefs)")
    r.add_argument("--trials", type=int, default=3)
    r.add_argument("--reference-trials", type=int, default=0,
                   help="also grade each brief's reference case on the first N trials")
    r.add_argument("--agent", default="codex-cli")
    r.add_argument("--author-model", default="gpt-6-astra")
    r.add_argument("--runner-model", default="gpt-6-astra")
    r.add_argument("--prediction", default=POSITIVE_CONTROL.name)
    r.add_argument("--device")
    r.add_argument("--mcp-server")
    r.add_argument("--runs-dir", default=None)
    r.add_argument("--max-cost", type=float, default=None, help="USD ceiling (required to run)")
    r.add_argument("--est-author-cost", type=float, default=EST_AUTHOR_COST)
    r.add_argument("--est-grade-cost", type=float, default=EST_GRADE_COST)
    r.add_argument("--max-attempts", type=int, default=2)
    r.add_argument("--smoke", action="store_true",
                   help="mark every cell smoke: never on the canonical board")
    r.add_argument("--allow-not-gradable", action="store_true",
                   help="start although some briefs are not gradable (they are skipped)")
    r.add_argument("--ungated", action="store_true",
                   help="start although the create readiness gate is not READY (recorded)")
    r.add_argument("--yes", action="store_true", help="spend: without it the plan is printed")
    p = sub.add_parser("report", help="the experiment's read-out; exit code = its verdict")
    p.add_argument("--experiment", required=True)
    p.add_argument("--runs-dir", default=None)
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    runs_dir = resolve_runs_dir(args.runs_dir)

    if args.cmd == "report":
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
        spec = _spec_from_args(args)
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

    gr = {c: gradability(c) for c in spec.briefs}
    _print_plan(spec, gr, args)
    gate = _board.read_gate(runs_dir)
    print(f"create readiness gate: {gate.state} — {gate.detail}")
    if not args.yes:
        print("not started: pass --yes (with --max-cost, --device, --mcp-server) to spend")
        return 1
    problems = []
    if args.max_cost is None:
        problems.append("--max-cost is required")
    if not (args.device and args.mcp_server):
        problems.append("--device and --mcp-server are required")
    if any(s == grader.NOT_GRADABLE for s, _ in gr.values()) and not args.allow_not_gradable:
        problems.append("some briefs are not gradable (above): derive their controls first, "
                        "or pass --allow-not-gradable to skip them (the verdict then counts "
                        "them as unscored)")
    if not gate.ready and not args.ungated:
        problems.append(f"the create readiness gate is {gate.state}; pass --ungated to run "
                        "anyway (recorded on the experiment)")
    if problems:
        print("refused:\n  " + "\n  ".join(problems), file=sys.stderr)
        return EXIT_REFUSED
    if state is None:
        from ..scheduler import new_run_id
        state = new_state(spec, run_id=new_run_id(), ungated=not gate.ready)
        save_state(path, state)
        print(f"registered: {path}")
    from ..dotenv import load_dotenv
    load_dotenv()
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
                    state=state)
    asyncio.run(driver.run())
    rep = report(runs_dir, spec.name)
    print("\n".join(render_report(rep)))
    return rep["verdict"]["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
