"""CreateBench stage C: grade an AUTHORED test case on the frozen journey runner (QUA-2857).

An authoring agent saved a test case (`authored_case.json`, the fake API's artifact —
`create.fake_api`). Whether that case is any GOOD is decided by executing it, never by
reading it: the frozen journey runner (the journey brief and verdict, unchanged, on the
agent and model the run names) runs the authored steps five times on the journey build
of the brief's app —

    clean    x3   no defect on. The case must PASS every time       -> repeatability
    target   x1   the brief case's own `bugs:` on, alone. The case must FAIL, and the
                  failure must be the target's                       -> power
    control  x1   one eligible control defect on, alone (QUA-2854, rotated by trial).
                  The case must still PASS                           -> specificity

and the grade asks, per run, whether it PROPERLY passed (clean/control) or PROPERLY
reported the target bug. With the free lint (`create.lint`, QUA-2855):

    Strong-Test = lint-clean AND pass^3 AND power AND specificity.

A lint-failing artifact is graded anyway (the August decision, QUA-2613) and shows as a
diagnostic column: `strong_exec` is the same conjunction without the lint conjunct.

What the runner sees. `runner_case` turns the artifact into exactly what a journey
reference case gives the runner — `name`, a list of step strings, `expected_outcome` —
with the product serialization's markers stripped (`[kind]`, `## {credential-uuid}`), so
an authored case and a reference case read identically and the brief text is the same
template for every arm. The raw serialized string is kept in the manifest for a later
lane that runs the product serialization itself.

What the runner does NOT get: the reference case's oracle. The author picks the data (a
per-run unique suffix, say), so a corpus `db:` query with a fixed literal answers a
different question (QUA-2851 spike, P6). An authored run is graded on the runner's
VERDICT and REPORT plus the harness's own canary, nothing the author wrote a literal for:
`oracle.mode` is `none` on every authored task, and the manifest says so
(`ORACLE_BINDING`). The step budget comes from the authored case itself (`step_budget`,
P7), one rule for every arm and for the reference baseline, never a corpus case's budget.

Attribution of a target FAIL (the only way a target run earns power):
  * the target defect has a `QgbFlags.fired` canary in its app's benchmark spec → the
    canary must have fired during that run (`fault_fired`, read by the harness after
    the agent exits). A FAIL on a covered target whose canary never fired is the runner
    failing for some other reason: `unattributed_fail`, no credit.
  * no canary (or the marker read failed) → `journey.match_report` must have credited a
    target bug from the runner's report (`bugs_found`).

The verdict contract on an observed death (GRADER_VERSION 3, QUA-2865). Every run is
judged on its EFFECTIVE verdict: the runner's own, except that a run during which the
harness recorded the app's own crash or ANR (`metrics.app_crashes` > 0 — the crash buffer
and the ANR log, read after the agent exits, foreign processes left out) is a FAIL
whatever the runner wrote. A test case that "passed" while the app died under it did not
pass: QUA-2861's arm B walked `anki-study-first-card`, the reviewer crashed, AnkiDroid
came back on its deck list, "Verify the app is still open" held and the runner wrote PASS
(and reported the crash). The rule is the harness's, not the runner's: the journey brief
and `BRIEF_VERSION` are untouched, so a journey board is unaffected and the rule rescores
offline. Attribution is unchanged (a forced FAIL on a covered target still needs the
target's canary). Each run records `effective_verdict` and `death_forced`.

Report-credited power (`power_report`, GRADER_VERSION 3, QUA-2865): a separate axis, never
in Strong-Test. A target run earns it when it earned power, OR when the runner REPORTED the
target whatever its verdict — the report matched the target (`bugs_found`) and, for a
canary-covered target whose marker was read, the canary fired. It is the "the runner saw
the bug" reading beside the verdict-only `power`, which stays the comparable axis: a case
whose only check is "the app is still open" earns `power_report` on every target the
runner happens to notice on its route (QUA-2861 §6: 7 of arm B's 16 DROP target runs), so
it measures the runner's curiosity as much as the case.

A freeze the app survives with no input pending (no ANR, nothing recorded) is invisible to
the death rule: only the runner's report can credit it (`power_report`), as on QUA-2861's
`medtimer-analysis-tabular-view` arm-B run, which neither rule credits.

Excluded runs (env failure, infra failure, contamination, unclean MCP session, rate
limit, truncation, wall-clock timeout, no result at all) never count as zeros: an axis
whose run is excluded is UNSCORED (None), and a summary leaves it out of the rate.

A copy of the public reference case (`reference_copy`: most of its steps and expected
outcome reproduced near-verbatim) is flagged `contamination_risk` and set aside by `summarize`: it measures
the reference row, not the author (QUA-2859).

Not gradable (a state, never a crash and never a free pass):
  * `controls_not_derived` — the brief case's truth row was never put through
    `scripts/derive_create_controls.py`, so specificity cannot be measured fairly;
  * `unknown_case` — the brief case is not in the corpus.
An artifact that does not exist (the author ended without saving a case) is
`no_case_created`: the AUTHOR's failure, so every axis is False — it is graded, not
excluded.

The reference baseline: `reference_case` reads a journey reference case's own NL steps
through the identical path (same conversion, same budget rule, same oracle binding,
same five runs), which gives the human-authored "reference" row.

Every grade is saved as one manifest (`<runs>/_runs/<run_id>/create_grades/<id>.json`,
harness-side, beside the episode index the agent never sees): the runner case, the run
plan with each run's role and episode dir, the runner fingerprint, the per-run scores
and the axes. `rescore_grade` rebuilds every task from that manifest and the CURRENT
corpus, rescores each saved episode offline (`rescore.rescore`, no device) and grades
again under the grader version the manifest RECORDS (`runner.grader_version`) — on an
unchanged corpus it reproduces the live grade exactly. `--grader-version N` regrades the
same episodes under another version's rules (what a contract change moves).

CLI:

    uv run python -m qualgentbench.create.grader plan (--artifact PATH | --reference) --case ID
    uv run python -m qualgentbench.create.grader run  (--artifact PATH | --reference) --case ID \\
        --agent codex-cli --model gpt-6-astra --mcp-server http://127.0.0.1:51871 \\
        --device emulator-5558 [--runs-dir DIR] [--trial T] [--yes]
    uv run python -m qualgentbench.create.grader rescore MANIFEST [--runs-dir DIR] \\
        [--grader-version N]
    uv run python -m qualgentbench.create.grader summary MANIFEST [MANIFEST ...]
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import brief as _brief
from .. import corpus, failures, journey, rates
from ..result import VerifierResult
from ..task import BenchmarkTask
from . import lint as _lint
from .fake_api import AUTHORED_CASE_FILE

logger = logging.getLogger(__name__)

#: 2 (QUA-2859): a grade carries `reference_copy`, and a copy of the reference case is
#: set aside as `contamination_risk` instead of being rated as authoring.
#: 3 (QUA-2865): a run during which the app died is a FAIL whatever the runner wrote
#: (`VERDICT_RULE`), and the `power_report` axis. A manifest is rescored under the
#: version it records, so a v2 grade reproduces as v2.
GRADER_VERSION = 3
#: The first version with the observed-death rule and `power_report`.
DEATH_FAILS_SINCE = 3
VERDICT_RULE = ("effective verdict = the runner's, except a run during which the harness "
                "recorded the app's own crash or ANR is a FAIL (QUA-2865)")
#: Recorded on every grade's episodes. Not journey mode's type, so a create grade never
#: blends into a journey board (`show --mode journey`, `rescore_journey.py`).
TASK_TYPE = "create_grade"
MANIFEST_SCHEMA = "qualgentbench.create.grade/1"
#: `<runs>/_runs/<run_id>/create_grades/` — where every grade manifest lives (the board,
#: `create/board.py`, reads them all from here).
GRADES_DIR = "create_grades"

ROLES = ("clean", "target", "control")
K_CLEAN = 3
#: The run order: the target and the control sit between clean runs, so a device that
#: drifts over a grade does not land on one role only.
PLAN_ORDER = (("clean", 1), ("target", 1), ("clean", 2), ("control", 1), ("clean", 3))

ORACLE_BINDING = ("none — graded on the runner's verdict and report plus the harness "
                  "canary; no authored literal is bound to a device oracle (QUA-2851 P6)")

# Step budget (P7): from the authored case's own step count, one rule for every arm and
# for the reference baseline. A step of a written case costs the runner about an action
# and a read; the base pays for launching and settling. Bounded so a padded case cannot
# buy unlimited interactions and a two-step case still has room to recover.
BUDGET_BASE = 20
BUDGET_PER_STEP = 4
BUDGET_MIN = 40
BUDGET_MAX = 100
BUDGET_RULE = (f"clamp({BUDGET_BASE} + {BUDGET_PER_STEP} x steps, "
               f"{BUDGET_MIN}, {BUDGET_MAX})")

NA = journey.SPECIFICITY_NA                  # "n/a": an axis this case cannot have

# Grade statuses.
GRADED = "graded"
NO_CASE = "no_case_created"
NOT_GRADABLE = "not_gradable"
CONTROLS_NOT_DERIVED = "controls_not_derived"
UNKNOWN_CASE = "unknown_case"

# Per-run outcomes.
PASSED = "passed"                 # clean/control: verdict pass
FAILED = "failed"                 # clean/control: verdict fail
CAUGHT = "caught"                 # target: verdict fail, attributed to the target
UNATTRIBUTED = "unattributed_fail"  # target: verdict fail, not the target's
MISSED = "missed"                 # target: verdict pass
NO_VERDICT = "no_verdict"         # the runner never reported a verdict
EXCLUDED = "excluded"


def step_budget(n_steps: int) -> int:
    return max(BUDGET_MIN, min(BUDGET_MAX, BUDGET_BASE + BUDGET_PER_STEP * max(0, n_steps)))


# ── the runner case ────────────────────────────────────────────────────────────

_KIND_PREFIX_RE = re.compile(r"^\s*\[(?:" + "|".join(_lint.KINDS) + r")\]\s*", re.IGNORECASE)
_CRED_MARKER_RE = re.compile(r"\s*##\s*\{?" + _lint._UUID + r"\}?")


def strip_markers(text: str) -> str:
    """One step's text as the runner reads it: a leading `[setup|act|verify]` tag and any
    `## {credential-uuid}` reference removed, whitespace collapsed. Both are product
    serialization, not something a tester reads off a written step, and a journey
    reference case carries neither."""
    out = _KIND_PREFIX_RE.sub("", text or "")
    out = _CRED_MARKER_RE.sub("", out)
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class RunnerCase:
    """What the frozen runner is handed: exactly a journey case's agent-facing fields."""
    name: str
    steps: list[str]
    expected_outcome: str
    source: str                          # "authored" | "reference"
    raw: dict[str, Any]                  # the case as authored (lint reads this)
    serialized_steps: str = ""           # product serialization, kept for a later lane
    test_case_id: str | None = None

    @property
    def fingerprint(self) -> str:
        """Content hash of what the runner sees — names the grade and its visible id."""
        blob = json.dumps([self.name, self.steps, self.expected_outcome], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:10]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunnerCase:
        return cls(**{f.name: d.get(f.name) for f in dataclasses.fields(cls)
                      if f.name in d})


def runner_case(artifact: dict[str, Any]) -> RunnerCase:
    """`authored_case.json` (or a bare create body / stored case) → the runner case.
    The case AS IT STANDS (create body + every update) is what is graded."""
    # `case` as it stands; else the create body alone (a raw capture of the POST, as the
    # QUA-2851 spike saved it: `{"request": {...}}`); else the document is the body.
    raw = next((artifact[k] for k in ("case", "request") if isinstance(artifact.get(k), dict)),
               artifact)
    # The fake API keeps the stored id BESIDE the case (`test_case_id`), not inside it,
    # so lint's `created-via-api` read the case alone and failed every captured artifact
    # — Strong-Test was unreachable for any real author (found by QUA-2859's honest
    # adversary). The id the API assigned is the case's: carry it in.
    raw = dict(raw)
    if artifact.get("test_case_id") and not (raw.get("id") or raw.get("test_case_id")):
        raw["test_case_id"] = artifact["test_case_id"]
    case = _lint.normalize_case(raw)
    steps = [strip_markers(s.text) for s in case.steps]
    steps = [s for s in steps if s]
    if not case.name or not steps:
        raise ValueError("authored case has no name or no steps")
    serialized = artifact.get("serialized_steps")
    if not isinstance(serialized, str):
        from .fake_api import serialize_steps
        raw_steps = raw.get("steps")
        serialized = (raw_steps if isinstance(raw_steps, str)
                      else serialize_steps([s if isinstance(s, dict) else {"description": s}
                                            for s in raw_steps or []]))
    return RunnerCase(name=case.name, steps=steps, expected_outcome=case.expected_result,
                      source="authored", raw=dict(raw), serialized_steps=serialized,
                      test_case_id=artifact.get("test_case_id") or case.case_id)


def load_artifact(path: str | Path) -> tuple[RunnerCase | None, str]:
    """(runner case, "") — or (None, why) when there is no case to grade: the artifact
    file (or `<episode>/authored_case.json`) is missing, unreadable or empty. That is the
    author's `no_case_created`, never a crash."""
    p = Path(path)
    if p.is_dir():
        p = p / AUTHORED_CASE_FILE
    if not p.exists():
        return None, f"no authored case at {p}"
    try:
        doc = json.loads(p.read_text())
        return runner_case(doc), ""
    except (OSError, ValueError, TypeError) as exc:
        return None, f"authored case unreadable: {exc}"


def reference_case(app_id: str, case_id: str) -> RunnerCase:
    """A journey reference case's own NL steps, through the identical path."""
    doc = journey.load_cases(app_id) or {}
    case = next((c for c in doc.get("test_cases", []) if str(c.get("id")) == case_id), None)
    if case is None:
        raise KeyError(f"no journey case {case_id!r} in {app_id}")
    return RunnerCase(name=str(case.get("name") or case_id),
                      steps=[strip_markers(str(s)) for s in case.get("steps") or []],
                      expected_outcome=str(case.get("expected_outcome") or "").strip(),
                      source="reference",
                      raw={"name": case.get("name"), "steps": list(case.get("steps") or []),
                           "expected_outcome": case.get("expected_outcome")},
                      test_case_id=case_id)


# ── reference copy: a contamination risk, never authoring ──────────────────────
# The journey reference cases are PUBLIC (`data/test-cases/<app>.yaml`), so an author
# that has seen this repository (training data, a web search) can hand back the
# reference case instead of authoring one. That artifact grades exactly like the
# human-authored reference row — it measures the reference, not the author — so it is
# flagged and `summarize` sets it aside (`contamination_risk`) rather than rating it as
# authoring. The check is textual on purpose: a step this close to a reference step,
# after the product markers, case and punctuation are folded away, was copied.
# Thresholds are calibrated by `scripts/create_adversary_check.py`: the `copyist`
# author (reference verbatim) must be flagged on every brief and `honest` on none.

COPY_STEP_RATIO = 0.9       # a unit at least this similar to a reference unit copies it
COPY_FLAG_SHARE = 0.6       # this share of the reference's units copied → flagged ...
COPY_MIN_UNITS = 3          # ... and at least this many: two generic steps ("Open the
                            # navigation drawer") are what anyone writes, not a copy
CONTAMINATION_RISK = "copy_of_reference"


def _copy_norm(text: str) -> str:
    out = re.sub(r"[^\w\s]", " ", strip_markers(text).casefold())
    return re.sub(r"\s+", " ", out).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def reference_copy(case: RunnerCase, app_id: str, case_id: str) -> dict[str, Any] | None:
    """How much of the brief case's PUBLIC reference an authored case reproduces. The
    units are the reference's steps plus its expected outcome, each matched at most once
    against the authored steps plus expected result (at `COPY_STEP_RATIO` or closer).
    `flagged` when at least `COPY_FLAG_SHARE` of the units and at least `COPY_MIN_UNITS`
    of them reappear. None for the reference baseline itself or a case the corpus does
    not know."""
    if case.source != "authored":
        return None
    ref = _find_case(app_id, case_id) if app_id else None
    if ref is None:
        return None
    units = [_copy_norm(str(s)) for s in [*(ref.get("steps") or []),
                                          ref.get("expected_outcome") or ""]]
    units = [u for u in units if u]
    pool = [u for u in (_copy_norm(s) for s in [*case.steps, case.expected_outcome]) if u]
    copied = 0
    for r in units:
        best = max(range(len(pool)), key=lambda i: _similar(r, pool[i]), default=None)
        if best is not None and _similar(r, pool[best]) >= COPY_STEP_RATIO:
            copied += 1
            pool.pop(best)
    share = copied / len(units) if units else 0.0
    return {"copied_units": copied, "reference_units": len(units), "share": round(share, 3),
            "flagged": copied >= COPY_MIN_UNITS and share >= COPY_FLAG_SHARE}


# ── canaries ───────────────────────────────────────────────────────────────────

_FIRED_RE = re.compile(r"""fired\(\s*["']([^"']+)["']\s*\)""")


def canary_ids(app_id: str) -> set[str]:
    """Defect ids with a `QgbFlags.fired("<id>")` call in the app's benchmark spec — the
    defects whose own code path reports that it ran (`verify.canary`)."""
    p = corpus.spec_path(app_id)
    return set(_FIRED_RE.findall(p.read_text())) if p.exists() else set()


# ── the plan ───────────────────────────────────────────────────────────────────

@dataclass
class PlannedRun:
    role: str
    index: int                           # 1-based within its role
    active_bugs: list[str]
    attempts: list[dict[str, Any]] = field(default_factory=list)   # {episode_dir, error?}

    @property
    def key(self) -> str:
        return f"{self.role}-{self.index}"


@dataclass
class GradePlan:
    app_id: str
    case_id: str                         # the brief / reference case the author targeted
    case: RunnerCase
    status: str = GRADED                 # GRADED | NO_CASE | NOT_GRADABLE
    reason: str = ""
    targets: list[str] = field(default_factory=list)
    canary_covered: list[str] = field(default_factory=list)
    control: dict[str, Any] = field(default_factory=dict)
    control_trial: int = 0
    step_budget: int = 0
    runs: list[PlannedRun] = field(default_factory=list)

    @property
    def grade_id(self) -> str:
        return f"{self.case_id}-g{self.case.fingerprint}"


def _find_case(app_id: str, case_id: str) -> dict | None:
    doc = journey.load_cases(app_id) or {}
    return next((c for c in doc.get("test_cases", []) if str(c.get("id")) == case_id), None)


def resolve_app(case_id: str) -> str | None:
    from .. import bugs
    return journey.known_case_ids(bugs.load_apps()).get(case_id)


def plan_grade(case: RunnerCase | None, case_id: str, *, app_id: str | None = None,
               control_trial: int = 0, why_no_case: str = "") -> GradePlan:
    """The five runs (clean x3, target x1, control x1) for one runner case, or the reason
    there are none. Never raises for a corpus state: a case whose controls were never
    derived is NOT_GRADABLE, one with no `bugs:` has no target run (power n/a), one with
    no eligible control has no control run (specificity n/a)."""
    app_id = app_id or resolve_app(case_id) or ""
    placeholder = case or RunnerCase(name="", steps=[], expected_outcome="", source="authored",
                                     raw={})
    plan = GradePlan(app_id=app_id, case_id=case_id, case=placeholder,
                     control_trial=control_trial)
    if case is None:
        plan.status, plan.reason = NO_CASE, why_no_case or "the author created no case"
        return plan
    ref = _find_case(app_id, case_id) if app_id else None
    if ref is None:
        plan.status, plan.reason = NOT_GRADABLE, f"{UNKNOWN_CASE}: {case_id!r} is not a journey case"
        return plan
    defects = journey.load_defects(journey.load_cases(app_id) or {})
    row = journey.load_truth(app_id).get(case_id)
    try:
        control = journey.control_for_trial(row, control_trial)
    except LookupError as exc:
        plan.status, plan.reason = NOT_GRADABLE, f"{CONTROLS_NOT_DERIVED}: {exc}"
        return plan
    design = journey.case_design(ref, defects)
    plan.targets = list(design["bugs"])
    canaries = canary_ids(app_id)
    plan.canary_covered = [t for t in plan.targets if t in canaries]
    plan.control = control
    plan.step_budget = step_budget(len(case.steps))
    for role, index in PLAN_ORDER:
        if role == "target" and not plan.targets:
            continue
        if role == "control" and not control.get("control"):
            continue
        active = (plan.targets if role == "target"
                  else [control["control"]] if role == "control" else [])
        plan.runs.append(PlannedRun(role, index, list(active)))
    return plan


def build_task(plan: GradePlan, run: PlannedRun, suite: dict[str, Any]) -> BenchmarkTask:
    """One journey-shaped task: the authored brief fields, the role's flags, and the
    report-matching evidence of the REFERENCE case's defect (a property of the defect,
    not of the route) with echoability judged against the AUTHORED text. Blind: the
    task id names the case and the runner-case hash only, and its version label is
    `clean` or `seeded` — target and control are both `seeded` to the agent."""
    app = suite["app"]
    app_id = str(app.get("id", ""))
    doc = journey.load_cases(app_id) or {}
    ref = _find_case(app_id, plan.case_id)
    if ref is None:
        raise KeyError(f"no journey case {plan.case_id!r} in {app_id}")
    defects = journey.load_defects(doc)
    measured = journey.load_truth(app_id).get(plan.case_id) or {}
    app_name = str(app.get("name") or app_id)
    case = plan.case
    authored_view = {"name": case.name, "steps": case.steps,
                     "expected_outcome": case.expected_outcome, "check": ref.get("check")}
    version = "clean" if run.role == "clean" else "seeded"
    spec_extra: dict[str, Any] = {"expected": "PASS", "blocking": None, "side": [],
                                  "blocking_texts": [], "crash_texts": [], "echo_texts": [],
                                  "absence_texts": []}
    if run.role == "target":
        design = journey.case_design(ref, defects)
        ev = journey.case_evidence(ref, design, measured, journey._oracle(ref), app_name,
                                   haystack_case=authored_view)
        spec_extra = {"expected": design["expected"], "blocking": design["blocking"], **ev}
    spec = {
        "mode": journey.MODE,
        "app_id": app_id,
        "case_id": plan.case_id,
        "version": version,
        "name": case.name,
        "steps": list(case.steps),
        "expected_outcome": case.expected_outcome,
        "step_budget": plan.step_budget,
        "active_bugs": list(run.active_bugs),
        **spec_extra,
        "defects": defects,
        "oracle": {"mode": "none", "expect": {}, "evidence": []},
        "truth_agrees": measured.get("agrees") if measured else None,
        "device_setup": suite.get("device_setup"),
        "shared_storage": suite.get("shared_storage"),
        "heldout": corpus.is_heldout(app_id),
        "create_role": run.role,
        "create_grade_id": plan.grade_id,
    }
    return BenchmarkTask(
        id=journey.task_id(plan.grade_id, version),
        name=case.name,
        instruction="",
        app_file_id="",
        app_name=app_name,
        platform=str(app.get("platform") or "android"),
        bundle_id=str(app.get("package") or ""),
        bug_spec=spec,
    )


def verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """The journey verdict, unchanged, with the grade's identity stamped on: which role
    this run played and which grade it belongs to (harness-side; the agent never sees
    either)."""
    v = journey.journey_verdict(transcript, model, task)
    spec = task.bug_spec or {}
    v.metrics.update({"create_role": spec.get("create_role"),
                      "create_grade_id": spec.get("create_grade_id"),
                      "create_active_bugs": list(spec.get("active_bugs") or [])})
    return v


# ── scoring ────────────────────────────────────────────────────────────────────

def exclusion(metrics: dict | None) -> str:
    """Why this run is not a measurement, or "" when it is. The journey exclusions plus
    truncation and the wall-clock net: a run the runner never finished says nothing
    about the case."""
    if metrics is None:
        return "no_result — the episode produced no result"
    if why := failures.exclusion_reason(metrics):
        return why
    if metrics.get("truncated"):
        return "truncated — the runner exhausted the step budget"
    if metrics.get("timed_out"):
        return "timed_out — the runner hit the wall-clock limit"
    return ""


# TODO(QUA-2865 follow-up): a silent freeze (no input pending, so no ANR) records no
# death. A post-run liveness probe on every create-grade run — `replay._check_stuck`'s one
# tap, as a journey `stuck:` oracle does after the agent exits — would let the harness see
# it too. Device-side (the episode runner), so not in this offline change.
def app_died(metrics: dict | None) -> bool:
    """The harness recorded the app's OWN crash or ANR while the runner drove it
    (`app_crashes`, `episode_runner._record_app_crashes`: foreign processes left out).
    A missing or unreadable count is not a death."""
    n = (metrics or {}).get("app_crashes")
    return isinstance(n, int) and not isinstance(n, bool) and n > 0


def score_run(role: str, metrics: dict | None, plan: GradePlan,
              version: int = GRADER_VERSION) -> dict[str, Any]:
    """One run's outcome. clean/control: `passed` iff verdict pass and not excluded.
    target: `caught` iff verdict fail AND attributed (canary when covered, else the
    report match). From version 3 the verdict is the EFFECTIVE one (`VERDICT_RULE`: a
    recorded app death is a FAIL) and a target run also carries `ok_report` — caught, or
    the runner reported the target (`power_report`). Below 3 the dict is exactly what
    that version wrote, so an old manifest rescored under its version reproduces."""
    m = metrics or {}
    out: dict[str, Any] = {"role": role, "verdict": m.get("reported_verdict"),
                           "fault_fired": m.get("fault_fired"),
                           "bugs_found": list(m.get("bugs_found") or []),
                           "false_reports": m.get("false_reports"),
                           "steps": m.get("steps"), "step_budget": m.get("step_budget"),
                           "app_crashes": m.get("app_crashes"),
                           "cost_usd": m.get("cost_usd")}
    why = exclusion(metrics)
    if why:
        return {**out, "outcome": EXCLUDED, "excluded": why, "ok": None}
    reported = m.get("reported_verdict")
    v3 = version >= DEATH_FAILS_SINCE
    if v3:
        forced = app_died(m) and reported != "fail"
        reported = "fail" if forced else reported
        out.update(effective_verdict=reported, death_forced=forced)
    if role in ("clean", "control"):
        outcome = PASSED if reported == "pass" else FAILED if reported == "fail" else NO_VERDICT
        out.update(outcome=outcome, ok=outcome == PASSED)
        if role == "control":
            ctrl = (plan.control or {}).get("control")
            fired = m.get("fault_fired")
            out["control_fired"] = (ctrl in fired) if (ctrl and isinstance(fired, list)) else None
        return out
    # target
    fired = m.get("fault_fired")
    covered = [t for t in plan.targets if t in plan.canary_covered]
    report_hit = any(t in out["bugs_found"] for t in plan.targets)
    canary_read = bool(covered) and isinstance(fired, list)
    # The runner reported the target: the report matched it and, where the target's
    # canary was read, the canary fired (a report of a bug whose code never ran is not
    # this target's).
    report_credit = report_hit and (any(t in fired for t in covered) if canary_read else True)
    if reported != "fail":
        outcome = MISSED if reported == "pass" else NO_VERDICT
        out = {**out, "outcome": outcome, "ok": False, "attribution": None}
        if v3:
            out.update(report_matched=report_hit, ok_report=report_credit)
        return out
    if canary_read:
        attributed, how = any(t in fired for t in covered), "canary"
    else:
        attributed = report_hit
        how = "report_match" if not covered else "report_match (canary unread)"
    out.update(outcome=CAUGHT if attributed else UNATTRIBUTED, ok=attributed,
               attribution=how if attributed else None, report_matched=report_hit)
    if v3:
        out["ok_report"] = attributed or report_credit
    return out


def _conj(values: list[Any]) -> Any:
    """Tri-state AND with n/a: any False → False; else any None (unscored) → None; else
    any n/a → n/a; else True."""
    if any(v is False for v in values):
        return False
    if any(v is None for v in values):
        return None
    if any(v == NA for v in values):
        return NA
    return True


def lint_artifact(plan: GradePlan) -> dict[str, Any]:
    """The free lint of the case as authored (QUA-2855): `ok` = no HARD failure."""
    try:
        report = _lint.lint_case(plan.case.raw, app_name=_lint.app_display_name(plan.app_id),
                                 content_strings=_lint.fixture_content_strings(plan.app_id))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "hard_failed": ["unreadable"], "soft_failed": [], "error": str(exc)}
    d = report.to_dict()
    return {"ok": d["ok"], "hard_failed": d["hard_failed"], "soft_failed": d["soft_failed"]}


def grade(plan: GradePlan, run_metrics: dict[str, dict | None],
          lint: dict[str, Any] | None = None, *,
          version: int = GRADER_VERSION) -> dict[str, Any]:
    """The grade of one artifact from its runs' metrics (`{run key: metrics | None}`, the
    LAST attempt of each planned run). Pure: the live driver and the offline rescore both
    end here, so they cannot disagree on anything but the metrics. `version` picks the
    contract (`GRADER_VERSION` history): below 3 the grade is byte-for-byte what that
    version wrote; from 3 it carries `grader_version` and the `power_report` axis."""
    v3 = version >= DEATH_FAILS_SINCE
    base = {"grade_id": plan.grade_id, "app_id": plan.app_id, "case_id": plan.case_id,
            "source": plan.case.source, "status": plan.status, "reason": plan.reason}
    if v3:
        base["grader_version"] = version
    if plan.status == NO_CASE:
        axes = {"lint": False, "repeatability": False, "specificity": False, "power": False,
                "strong": False, "strong_exec": False}
        if v3:
            axes["power_report"] = False
        return {**base, "axes": axes, "runs": {}, "lint": {"ok": False, "hard_failed": [NO_CASE]}}
    if plan.status == NOT_GRADABLE:
        axes = dict.fromkeys(("lint", "repeatability", "specificity", "power", "strong",
                              "strong_exec") + (("power_report",) if v3 else ()))
        return {**base, "axes": axes, "runs": {}, "lint": None}
    lint = lint if lint is not None else lint_artifact(plan)
    runs = {r.key: score_run(r.role, run_metrics.get(r.key), plan, version)
            for r in plan.runs}
    clean = [runs[r.key] for r in plan.runs if r.role == "clean"]
    if any(c["ok"] is False for c in clean):
        repeat: Any = False
    elif len(clean) == K_CLEAN and all(c["ok"] is True for c in clean):
        repeat = True
    else:
        repeat = None
    ctrl = next((runs[r.key] for r in plan.runs if r.role == "control"), None)
    specificity = NA if not (plan.control or {}).get("control") else (ctrl or {}).get("ok")
    tgt = next((runs[r.key] for r in plan.runs if r.role == "target"), None)
    # Power is NOT conditioned on repeatability: a case that fails on every run earns
    # power whenever the target's canary fires (or the report names it) —
    # `scripts/create_adversary_check.py`'s `impossible` author scores power 100% with
    # strong 0. `strong` is safe. The create board (QUA-2858, `create/board.py`) never
    # headlines power: Strong-Test is its headline, power prints beside pass^3, and its
    # derived `power_given_pass3` column reads power among pass^3 artifacts only; the
    # A/B driver's positive control pairs its power expectation with repeatability flat.
    power = NA if not plan.targets else (tgt or {}).get("ok")
    exec_axes = [repeat, specificity, power]
    axes = {"lint": bool(lint.get("ok")), "repeatability": repeat, "specificity": specificity,
            "power": power, "strong": _conj([bool(lint.get("ok")), *exec_axes]),
            # The diagnostic column for a lint-failing artifact: the same conjunction
            # without the lint conjunct (QUA-2613 — graded anyway).
            "strong_exec": _conj(exec_axes)}
    if v3:
        # Report-credited power (QUA-2865): beside `power`, never in `strong` — see the
        # module docstring for why it cannot replace the verdict-only axis.
        axes["power_report"] = NA if not plan.targets else (tgt or {}).get("ok_report")
    counted = [c for c in clean if c["ok"] is not None]
    copy = reference_copy(plan.case, plan.app_id, plan.case_id)
    if copy and copy["flagged"]:
        base["contamination_risk"] = CONTAMINATION_RISK
    return {**base, "axes": axes, "lint": lint, "reference_copy": copy,
            "lint_diagnostic": not lint.get("ok"),
            "clean_passed": f"{sum(1 for c in counted if c['ok'])}/{len(counted)}",
            "runs": runs,
            "excluded_runs": sorted(k for k, r in runs.items() if r["outcome"] == EXCLUDED)}


AXES = ("lint", "repeatability", "specificity", "power", "strong", "strong_exec",
        "power_report")


def summarize(grades: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-axis rates over a set of grades, each with its Wilson interval. A rate's
    denominator is the artifacts where the axis is SCORED (True/False); unscored (an
    excluded run) and n/a are counted beside it, never as zeros. NOT_GRADABLE grades
    leave every rate and are counted by reason; NO_CASE grades are failures on every
    axis. A grade flagged `contamination_risk` (a copy of the public reference case,
    `reference_copy`) leaves every rate too: it measures the reference, not the
    author, and is counted beside them."""
    risky = [g for g in grades if g.get("contamination_risk")]
    graded = [g for g in grades if g.get("status") in (GRADED, NO_CASE)
              and not g.get("contamination_risk")]
    out: dict[str, Any] = {"artifacts": len(grades), "graded": len(graded),
                           "no_case_created": sum(1 for g in grades if g.get("status") == NO_CASE),
                           "contamination_risk": len(risky),
                           "not_gradable": {}}
    for g in grades:
        if g.get("status") == NOT_GRADABLE:
            key = str(g.get("reason") or "").split(":", 1)[0] or "unknown"
            out["not_gradable"][key] = out["not_gradable"].get(key, 0) + 1
    for axis in AXES:
        # A grade written before an axis existed (`power_report` < v3) is not counted on
        # it at all — absent is not unscored.
        vals = [(g.get("axes") or {}).get(axis) for g in graded
                if axis in (g.get("axes") or {})]
        k = sum(1 for v in vals if v is True)
        n = sum(1 for v in vals if v is True or v is False)
        r = rates.rate(k, n)
        out[axis] = {**(r.as_fields("rate") if r else rates.empty_fields("rate")),
                     "unscored": sum(1 for v in vals if v is None),
                     "na": sum(1 for v in vals if v == NA)}
    return out


# ── the runner fingerprint ─────────────────────────────────────────────────────

def runner_fingerprint(agent: str, model: str, tooling: str = "mcp") -> dict[str, Any]:
    """What makes the runner FROZEN, recorded on every grade: the agent and model, the
    journey brief's version and a hash of its rendered template (placeholder case, so a
    template edit moves it and an authored case does not), the budget rule and the
    oracle binding."""
    probe = BenchmarkTask(id="x~clean", name="x", instruction="", app_file_id="",
                          app_name="APP", platform="android", bundle_id="PKG",
                          bug_spec={"name": "NAME", "steps": ["STEP"],
                                    "expected_outcome": "OUTCOME"})
    text = journey.brief(probe, "DEVICE", tooling)
    return {"agent": agent, "model": model, "tooling": tooling,
            "brief_version": _brief.BRIEF_VERSION,
            "brief_sha": hashlib.sha256(text.encode()).hexdigest()[:12],
            "budget_rule": BUDGET_RULE, "oracle_binding": ORACLE_BINDING,
            "grader_version": GRADER_VERSION, "verdict_rule": VERDICT_RULE}


# ── the manifest ───────────────────────────────────────────────────────────────

def manifest_path(runs_dir: Path, run_id: str, grade_id: str) -> Path:
    from ..checkpoint import run_meta_dir
    return run_meta_dir(runs_dir, run_id) / GRADES_DIR / f"{grade_id}.json"


def plan_to_dict(plan: GradePlan) -> dict[str, Any]:
    return {"app_id": plan.app_id, "case_id": plan.case_id, "case": plan.case.as_dict(),
            "status": plan.status, "reason": plan.reason, "targets": plan.targets,
            "canary_covered": plan.canary_covered, "control": plan.control,
            "control_trial": plan.control_trial, "step_budget": plan.step_budget,
            "runs": [dataclasses.asdict(r) for r in plan.runs]}


def plan_from_dict(d: dict[str, Any]) -> GradePlan:
    plan = GradePlan(app_id=d["app_id"], case_id=d["case_id"],
                     case=RunnerCase.from_dict(d["case"]), status=d.get("status", GRADED),
                     reason=d.get("reason", ""), targets=list(d.get("targets") or []),
                     canary_covered=list(d.get("canary_covered") or []),
                     control=dict(d.get("control") or {}),
                     control_trial=int(d.get("control_trial") or 0),
                     step_budget=int(d.get("step_budget") or 0))
    plan.runs = [PlannedRun(r["role"], r["index"], list(r["active_bugs"]),
                            list(r.get("attempts") or [])) for r in d.get("runs") or []]
    return plan


def _last_episode(run: PlannedRun) -> str | None:
    return (run.attempts[-1].get("episode_dir") if run.attempts else None) or None


def _read_metrics(runs_dir: Path, run: PlannedRun) -> dict | None:
    ep = _last_episode(run)
    if not ep:
        return None
    try:
        return json.loads((runs_dir / ep / "result.json").read_text()).get("metrics") or {}
    except (OSError, ValueError):
        return None


def _episode_cost(runs_dir: Path, plan: GradePlan) -> dict[str, Any]:
    wall, cost, priced, n = 0.0, 0.0, 0, 0
    for run in plan.runs:
        for a in run.attempts:
            ep = a.get("episode_dir")
            if not ep:
                continue
            try:
                r = json.loads((runs_dir / ep / "result.json").read_text())
            except (OSError, ValueError):
                continue
            n += 1
            wall += float(r.get("wall_time_sec") or 0)
            c = (r.get("metrics") or {}).get("cost_usd")
            if isinstance(c, int | float):
                cost += float(c)
                priced += 1
    return {"episodes": n, "agent_wall_sec": round(wall, 1),
            "cost_usd": round(cost, 4), "priced_episodes": priced}


def write_manifest(path: Path, plan: GradePlan, *, runner: dict, run_id: str,
                   result: dict | None, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema": MANIFEST_SCHEMA, "run_id": run_id, "grade_id": plan.grade_id,
           "runner": runner, "corpus": corpus.stamp(), "plan": plan_to_dict(plan),
           "grade": result, **(extra or {})}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


# ── offline rescore ────────────────────────────────────────────────────────────

def recorded_version(doc: dict[str, Any]) -> int:
    """The grader version a manifest was graded under (`runner.grader_version`); a
    manifest that does not record one is graded under the current rules."""
    v = (doc.get("runner") or {}).get("grader_version")
    return v if isinstance(v, int) and not isinstance(v, bool) else GRADER_VERSION


def rescore_grade(manifest: Path, runs_dir: Path, version: int | None = None
                  ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(fresh grade, recorded grade): every saved episode of the manifest rescored from
    its artifacts — transcript, findings file, saved device facts — against tasks rebuilt
    from the manifest and the CURRENT corpus, then graded under `version` (default: the
    version the manifest records, so an old grade reproduces under its own contract). No
    agent, no device, nothing written."""
    from .. import bugs
    from ..rescore import rescore as _rescore

    doc = json.loads(Path(manifest).read_text())
    version = recorded_version(doc) if version is None else version
    plan = plan_from_dict(doc["plan"])
    if plan.status != GRADED:
        return grade(plan, {}, version=version), doc.get("grade")
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == plan.app_id)
    metrics: dict[str, dict | None] = {}
    for run in plan.runs:
        ep = _last_episode(run)
        if not ep or not (runs_dir / ep / "result.json").exists():
            metrics[run.key] = None
            continue
        task = build_task(plan, run, suite)
        status, _b, _a, v = _rescore(runs_dir / ep, {task.id: task}, True,
                                     task_types=(TASK_TYPE,))
        metrics[run.key] = v.metrics if v is not None else _read_metrics(runs_dir, run)
        if v is None:
            logger.warning("%s: %s not rescored (%s); using recorded metrics", plan.grade_id,
                           run.key, status)
    return grade(plan, metrics, version=version), doc.get("grade")


# ── the live driver ────────────────────────────────────────────────────────────

async def run_grade(plan: GradePlan, *, agent: str, model: str, mcp_server: str,
                    device: str, runs_dir: Path, run_id: str | None = None,
                    max_attempts: int = 2, yes: bool = False,
                    extra: dict[str, Any] | None = None,
                    manifest_name: str | None = None) -> Path:
    """Execute the plan on one device and write the manifest after every run (a killed
    grade keeps what it finished). An EXCLUDED run is retried once (`max_attempts`): a
    rate limit or a dead staging measures nothing, and only the last attempt is graded.
    Returns the manifest path.

    `extra` is merged into every write of the manifest — the A/B driver's `cell` block
    (arm, author, brief, trial, creation episode: `create/board.py` reads it). The
    manifest is named `manifest_name` when given, else the grade id: two cells of one
    experiment that authored the identical case share a grade id, never a file."""
    from .. import bugs
    from ..cli import _preflight, _resolve_app_apk
    from ..episode_runner import EpisodeOptions, prepare_app, run_episode
    from ..result import resolve_artifact_dir
    from ..scheduler import new_run_id
    from ..schemas import Condition
    from ..session import DeviceSession

    run_id = run_id or new_run_id()
    runner = runner_fingerprint(agent, model, "mcp" if mcp_server else "raw")
    path = manifest_path(runs_dir, run_id, manifest_name or plan.grade_id)
    if plan.status != GRADED:
        write_manifest(path, plan, runner=runner, run_id=run_id, result=grade(plan, {}),
                       extra=extra)
        return path
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == plan.app_id)
    apk = _resolve_app_apk(suite["app"], suite, mode="journey")
    session = DeviceSession(mcp_server)
    identity = await _preflight(session, mcp_server, agent, device, [model])
    runner["mcp_server"] = {k: (identity or {}).get(k) for k in ("name", "version", "commit",
                                                               "app_source", "isolation")}
    hunt = bugs.exploration_task(suite)
    await session.force_release(device)
    bundle_id = await prepare_app(session, device, apk, hunt)
    apk_sha = (hunt.bug_spec or {}).get("apk_sha256")
    write_manifest(path, plan, runner=runner, run_id=run_id, result=None, extra=extra)
    for run in plan.runs:
        for attempt in range(1, max_attempts + 1):
            task = build_task(plan, run, suite)
            task.bundle_id = bundle_id
            if apk_sha:
                task.bug_spec["apk_sha256"] = apk_sha
            opts = EpisodeOptions(
                agent=agent, model=model, condition=Condition.no_routines, trial=run.index,
                mcp_server=mcp_server, runs_dir=runs_dir, verdict_fn=verdict,
                task_type=TASK_TYPE, device_serial=device,
                tooling=("mcp" if mcp_server else "raw"), apk_path=apk, force_model=model,
                run_id=run_id, attempt=attempt, app_id=plan.app_id,
                mcp_server_identity=identity)
            logger.info("create grade %s: %s attempt %d", plan.grade_id, run.key, attempt)
            try:
                result = await run_episode(task, opts)
            except RuntimeError as exc:
                run.attempts.append({"episode_dir": None, "error": str(exc)[:300]})
                write_manifest(path, plan, runner=runner, run_id=run_id, result=None,
                               extra=extra)
                continue
            ep = resolve_artifact_dir(runs_dir, result)
            rel = str(ep.relative_to(runs_dir)) if ep is not None else None
            run.attempts.append({"episode_dir": rel, "excluded": exclusion(result.metrics)})
            write_manifest(path, plan, runner=runner, run_id=run_id, result=None,
                           extra=extra)
            if not exclusion(result.metrics):
                break
    metrics = {r.key: _read_metrics(runs_dir, r) for r in plan.runs}
    write_manifest(path, plan, runner=runner, run_id=run_id, result=grade(plan, metrics),
                   extra={**(extra or {}), "cost": _episode_cost(runs_dir, plan)})
    return path


# ── CLI ────────────────────────────────────────────────────────────────────────

def _plan_from_args(args: argparse.Namespace) -> GradePlan:
    app_id = args.app or resolve_app(args.case)
    why = ""
    if args.reference:
        try:
            case: RunnerCase | None = reference_case(app_id or "", args.case)
        except KeyError:
            # Not a corpus case: a placeholder, so `plan_grade` reports it unknown.
            case = RunnerCase(name=args.case, steps=[], expected_outcome="",
                              source="reference", raw={})
    else:
        case, why = load_artifact(args.artifact)
    return plan_grade(case, args.case, app_id=app_id, control_trial=args.trial, why_no_case=why)


def _print_grade(g: dict[str, Any]) -> None:
    ax = g.get("axes") or {}
    print(f"{g['grade_id']} [{g['source']}] {g['status']}"
          + (f" — {g['reason']}" if g.get("reason") else "")
          + (f" — CONTAMINATION RISK ({g['contamination_risk']}: "
             f"{(g.get('reference_copy') or {}).get('copied_units')}/"
             f"{(g.get('reference_copy') or {}).get('reference_units')} reference units)"
             if g.get("contamination_risk") else ""))
    print("  " + " · ".join(f"{k} {ax.get(k)}" for k in AXES))
    for key, r in (g.get("runs") or {}).items():
        extra = r.get("excluded") or r.get("attribution") or ""
        if r.get("death_forced"):
            extra = f"app died → fail; {extra}".rstrip("; ")
        print(f"  {key:10} {r['outcome']:18} verdict={r.get('verdict')} "
              f"fired={r.get('fault_fired')} found={r.get('bugs_found')} {extra}")


def axis_changes(recorded: dict[str, Any] | None, fresh: dict[str, Any]) -> str:
    """`axis old → new` for every axis that moved, an axis the recorded grade lacks
    shown as new; "no axis moved" otherwise."""
    was = (recorded or {}).get("axes") or {}
    now = fresh.get("axes") or {}
    moved = [f"{k} {was[k]} → {now.get(k)}" if k in was else f"{k} (new) {now.get(k)}"
             for k in AXES if k in now and (k not in was or was[k] != now.get(k))]
    return "; ".join(moved) or "no axis moved"


def main(argv: list[str] | None = None) -> int:
    from ..config import resolve_runs_dir

    ap = argparse.ArgumentParser(prog="python -m qualgentbench.create.grader",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--artifact", help="authored_case.json, or the episode dir holding it")
        src.add_argument("--reference", action="store_true",
                         help="grade the journey reference case itself (the baseline row)")
        p.add_argument("--case", required=True, help="the brief / reference case id")
        p.add_argument("--app", help="its app (default: looked up from the case id)")
        p.add_argument("--trial", type=int, default=0,
                       help="0-based trial index that rotates the control (default 0)")
        p.add_argument("--json", action="store_true")
        if name == "run":
            p.add_argument("--agent", default="codex-cli")
            p.add_argument("--model", required=True)
            p.add_argument("--mcp-server", required=True)
            p.add_argument("--device", required=True)
            p.add_argument("--runs-dir", default=None)
            p.add_argument("--run-id", default=None)
            p.add_argument("--max-attempts", type=int, default=2)
            p.add_argument("--yes", action="store_true",
                           help="start the (paid) runs; without it `run` prints the plan "
                                "and exits 1, like `qualgent-bench run` with no terminal")
    p = sub.add_parser("rescore")
    p.add_argument("manifest", type=Path, nargs="+")
    p.add_argument("--runs-dir", default=None)
    p.add_argument("--grader-version", type=int, default=None,
                   help="grade under this version's rules (default: the version each "
                        f"manifest records; current {GRADER_VERSION}). Prints what moved "
                        "instead of demanding a reproduction")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("summary")
    p.add_argument("manifest", type=Path, nargs="+")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd in ("plan", "run"):
        plan = _plan_from_args(args)
        if args.cmd == "plan" or not args.yes:
            d = {"grade_id": plan.grade_id, "plan": plan_to_dict(plan),
                 "lint": lint_artifact(plan) if plan.status == GRADED else None,
                 "runner_brief_sha": runner_fingerprint("-", "-")["brief_sha"]}
            print(json.dumps(d, indent=2, ensure_ascii=False) if args.json else
                  f"{plan.grade_id}: {plan.status} {plan.reason}\n  budget {plan.step_budget} · "
                  f"targets {plan.targets} (canary {plan.canary_covered}) · control "
                  f"{plan.control.get('control')} ({plan.control.get('relation')}) · runs "
                  f"{[r.key for r in plan.runs]}\n  lint {d['lint']}")
            if args.cmd == "run":
                print("not started: pass --yes to run these episodes")
                return 1
            return 0
        import asyncio

        from ..dotenv import load_dotenv
        from .board import MANUAL, cell_block
        load_dotenv()                    # the same `.env` `qualgent-bench run` reads
        runs_dir = resolve_runs_dir(args.runs_dir)
        # The board's link from this grade to the creation episode it grades (so that
        # episode stops counting as pending); author and arm are unknown by hand.
        episode = None
        if args.artifact:
            ep = Path(args.artifact).expanduser().resolve()
            ep = ep if ep.is_dir() else ep.parent
            episode = (str(ep.relative_to(runs_dir.resolve()))
                       if ep.is_relative_to(runs_dir.resolve()) else None)
        cell = cell_block(kind=MANUAL, case_id=args.case, trial=args.trial + 1,
                          source="reference" if args.reference else "authored",
                          creation_episode=episode)
        path = asyncio.run(run_grade(plan, agent=args.agent, model=args.model,
                                     mcp_server=args.mcp_server, device=args.device,
                                     runs_dir=runs_dir, run_id=args.run_id,
                                     max_attempts=args.max_attempts, extra={"cell": cell}))
        doc = json.loads(path.read_text())
        _print_grade(doc["grade"])
        print(f"manifest: {path}\ncost: {doc.get('cost')}")
        return 0

    if args.cmd == "rescore":
        runs_dir = resolve_runs_dir(args.runs_dir)
        mismatches = 0
        for m in args.manifest:
            fresh, recorded = rescore_grade(m, runs_dir, args.grader_version)
            _print_grade(fresh)
            if args.grader_version is None:
                same = fresh == recorded
                mismatches += not same
                print(f"  reproduces the recorded grade: {same}")
            else:
                print(f"  under grader v{args.grader_version}: " + axis_changes(recorded, fresh))
            if args.json:
                print(json.dumps(fresh, indent=2, default=str))
        return 1 if mismatches else 0

    grades = [json.loads(Path(m).read_text()).get("grade") or {} for m in args.manifest]
    print(json.dumps(summarize([g for g in grades if g]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
