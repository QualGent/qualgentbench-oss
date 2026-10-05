#!/usr/bin/env python3
"""CreateBench adversary check (QUA-2859): can an AUTHOR be credited without authoring a
test that tests?

Hunt mode has `adversary_check.py` and journey mode `journey_adversary_check.py`; this is
the same gate for CreateBench v2 (epic QUA-2850). Scripted authors write one test case per
brief, post it through the real fake QualGent API (`create.fake_api`, so the artifact is the
`authored_case.json` a creation episode leaves), and the REAL grader (`create.grader`: plan,
tasks, the journey verdict, lint, `grade`, `summarize`) grades it against a SIMULATED
runner — offline, no device, no agent, no tokens.

The simulated runner is honest about the authored case's semantics. Each author's verify
steps make claims, and the runner evaluates each claim against the run's flag state:

  visible      a screen or title is on screen                    holds on every run
  intended     the behaviour the brief describes                 fails only on the target run
  symptom      the seeded bug's own symptom text is shown        holds only on the target run
  volatile     a value captured while authoring (a clock time)   holds on clean-1 only
  incidental   an unrelated value on a screen the case crosses   holds only with no flag on
  never        something the app never shows                     never holds

and a target whose defect kills the app (`journey.expected_death`: crash/ANR/stuck) fails
any case whose steps walk the feature, whatever it asserts. A run fails when the app dies
or any claim does not hold. When the TARGET caused the failure the runner reports it the way
`journey_adversary_check`'s `honest` agent does (the corpus's measured screen strings,
grounded in a device read); any other failure is reported vaguely, naming nothing. The
target's `QgbFlags.fired` canary fires whenever its flag is on (pessimistic: a canary that
fires with the case passing must still earn nothing).

Authors — every one must be graded exactly as stated, on every gradable brief and trial:

  honest           walks the feature, asserts the intended behaviour   -> Strong-Test
  vacuous          opens the app, asserts its title is visible          -> power False
  overfit-symptom  asserts the target bug's symptom text                -> repeatability False
  overfit-volatile asserts a time it saw while authoring                -> repeatability False
                   (it passes clean-1: pass@1 would have credited it)
  overfit-build    also asserts an incidental value a sibling defect    -> specificity False
                   moves (with no eligible control: uncatchable, WARN)
  impossible       asserts something the app never shows                -> repeatability False
  copyist          hands back the PUBLIC reference case verbatim        -> contamination_risk,
                                                                           out of every rate
  no-case          ends without saving a case                           -> no_case_created

and two MEASURED authors, the predictions for QUA-2861's positive control:

  harmful-rule     walks the feature, then ends with the single verify step the arm-B
                   rule asks for ("the current screen's title is visible"). Asserted:
                   repeatability and specificity equal honest's on every brief, power never
                   above honest's. Printed: where power drops (targets that leave the app
                   alive) and where it cannot (a death target still kills the walk).
  harmful-rule-v2  QUA-2864's rule (`uptake.APP_OPEN`) taken in full: walks the feature,
                   keeps no outcome check, ends on "Verify the app is still open" with that
                   as its expected result. Same assertions as harmful-rule.

The owner's registered prediction (QUA-2862, `ab.POSITIVE_CONTROL_MECHANISM`) is then
judged by the A/B driver's own `ab.evaluate` on these grades, trials cut to its design
(assert 2, walk 1): arm A = honest, arm B = harmful-rule (printed), and arm B = honest
again, a NO-OP arm. A prediction that DETECTS the no-op arm is broken, and that FAILS
the gate; the harmful-rule verdict is printed, never gated (it is what QUA-2861 measures).
With `--subset-v2` the briefs are QUA-2864's re-run subset and v2
(`ab.POSITIVE_CONTROL_MECHANISM_V2`, with its uptake check over the authored cases
themselves, `create/uptake.py`) is judged too: arm B = harmful-rule-v2 (full uptake,
printed), the no-op, and arm B = harmful-rule, a NON-TAKING arm (it follows a different
rule), which must read INCONCLUSIVE — a manipulation check that lets a non-taking arm
reach MISSED or DETECTED FAILS the gate, as a no-op DETECTED does.

Briefs whose truth row has no `create_controls` are not gradable (QUA-2854) and are
listed, never silently passed; with no gradable brief at all the check FAILS.
`--provisional-controls` gives such rows a stand-in control (another defect of the app)
so the scoring path can be exercised before the derivation lands — printed as
PROVISIONAL, never a gate result (`check_tier_ready --tier create` never passes it).

    uv run python scripts/create_adversary_check.py                    # every public brief
    uv run python scripts/create_adversary_check.py --subset           # QUA-2861's 12
    uv run python scripts/create_adversary_check.py --subset-v2        # QUA-2864's 8
    uv run python scripts/create_adversary_check.py --case anki-study-first-card -v
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from qualgentbench import bugs, corpus, journey
from qualgentbench.create import grader, uptake
from qualgentbench.create.fake_api import FakeApp, FakeQualGentAPI

ROOT = Path(__file__).resolve().parents[1]
SUBSET_PATH = Path(journey._DATA) / "create" / "positive-control.yaml"
SUBSET_V2_PATH = Path(journey._DATA) / "create" / "positive-control-v2.yaml"

# Authors with an asserted outcome, and the one whose outcome is a prediction.
GATED = ("honest", "vacuous", "overfit-symptom", "overfit-volatile", "overfit-build",
         "impossible", "copyist", "no-case")
MEASURED = ("harmful-rule", "harmful-rule-v2")
AUTHORS = GATED + MEASURED
DEFAULT_TRIALS = 3              # QUA-2861 runs three trials; each rotates the control


def _jac() -> Any:
    """`journey_adversary_check`, for its honest report: one definition of what an
    honest runner quotes, shared with the journey gate."""
    spec = importlib.util.spec_from_file_location(
        "journey_adversary_check", ROOT / "scripts" / "journey_adversary_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_JAC = _jac()


# ── briefs ─────────────────────────────────────────────────────────────────────

@dataclass
class Brief:
    app_id: str
    case_id: str
    app_name: str
    ref: dict[str, Any]
    title: str
    intended: str
    targets: list[str]
    death: bool                          # the target kills the app (crash/ANR/stuck)
    symptom: str                         # the target's own marker or longest symptom


def load_briefs(case_ids: list[str] | None = None, subset: bool = False,
                subset_path: Path = SUBSET_PATH) -> list[Brief]:
    """Every public journey case with a `brief:` (QUA-2853), narrowed to `case_ids` or
    to a positive-control subset (`subset_path`)."""
    wanted: set[str] | None = set(case_ids) if case_ids else None
    if subset:
        doc = yaml.safe_load(Path(subset_path).read_text()) or {}
        ids = {str(b.get("case")) for b in doc.get("briefs") or []}
        wanted = ids if wanted is None else wanted & ids
    names = {s["app"]["id"]: str(s["app"].get("name") or s["app"]["id"])
             for s in bugs.load_apps()}
    out: list[Brief] = []
    for app_id in corpus.public_apps():
        doc = journey.load_cases(app_id) or {}
        defects = journey.load_defects(doc)
        for c in doc.get("test_cases") or []:
            cid = str(c.get("id"))
            b = c.get("brief")
            if not isinstance(b, dict) or (wanted is not None and cid not in wanted):
                continue
            design = journey.case_design(c, defects)
            sym = ""
            for t in design["bugs"]:
                d = defects.get(t) or {}
                sym = d.get("marker") or max(d.get("symptoms") or [""], key=len)
                if sym:
                    break
            out.append(Brief(app_id=app_id, case_id=cid, app_name=names.get(app_id, app_id),
                             ref=c, title=str(b.get("title") or "").strip(),
                             intended=str(b.get("intended_behavior") or "").strip(),
                             targets=list(design["bugs"]), death=bool(design["death"]),
                             symptom=sym))
    return out


# ── the scripted authors ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Author:
    name: str
    claims: tuple[str, ...]
    exercised: bool = True               # its steps walk the feature the brief describes


AUTHOR_SPECS: dict[str, Author] = {
    "honest": Author("honest", ("intended",)),
    "vacuous": Author("vacuous", ("visible",), exercised=False),
    "overfit-symptom": Author("overfit-symptom", ("symptom",)),
    "overfit-volatile": Author("overfit-volatile", ("intended", "volatile")),
    "overfit-build": Author("overfit-build", ("intended", "incidental")),
    "impossible": Author("impossible", ("intended", "never")),
    "copyist": Author("copyist", ("intended",)),
    "no-case": Author("no-case", ()),
    "harmful-rule": Author("harmful-rule", ("visible",)),
    # "The app is still open" holds on every run where the app lives (`visible`); a death
    # target still kills the walk (`exercised`).
    "harmful-rule-v2": Author("harmful-rule-v2", ("visible",)),
}


def _step(text: str, kind: str) -> dict[str, str]:
    return {"description": text, "kind": kind}


def _walk(b: Brief) -> list[dict[str, str]]:
    """The honest author's route, in the brief's own (neutral, quote-free, digit-free)
    words: launch, reach the feature, use it."""
    return [_step("Open the app", "setup"),
            _step(f"Go to the {b.title}", "act"),
            _step(f"Use the {b.title} the way the feature intends", "act")]


def author_body(author: str, b: Brief) -> dict[str, Any] | None:
    """The create body one scripted author posts for one brief (None: posts nothing)."""
    walk = _walk(b)
    intended = _step(f"Verify the {b.title} shows the intended result", "verify")
    body: dict[str, Any] = {"name": b.title, "expected_result": b.intended,
                            "priority": "High"}
    if author == "honest":
        body["steps"] = [*walk, intended]
    elif author == "vacuous":
        body.update(name=f"{b.title} opens",
                    steps=[_step("Open the app", "setup"),
                           _step("Verify the main screen title of the app is visible",
                                 "verify")],
                    expected_result="The app opens on its main screen.")
    elif author == "harmful-rule":
        body["steps"] = [*walk, _step("Verify the current screen's title is visible",
                                      "verify")]
    elif author == "harmful-rule-v2":
        body.update(steps=[*walk, _step("Verify the app is still open", "verify")],
                    expected_result="The app is still open.")
    elif author == "overfit-symptom":
        body["steps"] = [*walk, _step(f'Verify the screen shows "{b.symptom or "it"}"',
                                      "verify")]
    elif author == "overfit-volatile":
        body["steps"] = [*walk, intended,
                         _step('Verify the header shows "10:42 AM"', "verify")]
    elif author == "overfit-build":
        body["steps"] = [*walk, intended,
                         _step("Verify every other row on the screen reads exactly as it "
                               "did while this case was written", "verify")]
    elif author == "impossible":
        body["steps"] = [*walk, intended,
                         _step('Verify the banner "Feature verified" is shown', "verify")]
    elif author == "copyist":
        body.update(name=str(b.ref.get("name") or b.title),
                    steps=[{"description": str(s)} for s in b.ref.get("steps") or []],
                    expected_result=str(b.ref.get("expected_outcome") or ""))
    elif author == "no-case":
        return None
    else:
        raise KeyError(author)
    return body


def post_artifact(body: dict[str, Any] | None, episode_dir: Path, app_name: str) -> Path:
    """Post the case through the fake QualGent API exactly as QualGent-MCP would, so the
    graded artifact is the one a creation episode leaves. Returns the episode dir."""
    import httpx
    episode_dir.mkdir(parents=True, exist_ok=True)
    if body is None:
        return episode_dir
    with FakeQualGentAPI(episode_dir, app=FakeApp(app_name)) as api, \
            httpx.Client(base_url=api.url, headers={"x-api-key": "qg_adversary"}) as c:
        r = c.post("/v1/test-cases", json={**body, "change_source": "mcp"})
        if r.status_code != 201:
            raise RuntimeError(f"fake API refused the {app_name} case: {r.status_code} {r.text}")
    return episode_dir


# ── the simulated runner ──────────────────────────────────────────────────────

def claim_holds(claim: str, role: str, index: int) -> bool:
    if claim == "visible":
        return True
    if claim == "intended":
        return role != "target"
    if claim == "symptom":
        return role == "target"
    if claim == "volatile":
        return role == "clean" and index == 1
    if claim == "incidental":
        return role == "clean"
    if claim == "never":
        return False
    raise KeyError(claim)


VAGUE_FAIL = "the step's assertion did not hold on the screen"


def simulate(author: Author, b: Brief, run: grader.PlannedRun, task: Any,
             canaries: set[str]) -> tuple[str, str, list[str]]:
    """(transcript, findings file, fired canaries) of one run of the authored case."""
    spec = task.bug_spec
    died = run.role == "target" and b.death and author.exercised
    failed_claims = [c for c in author.claims if not claim_holds(c, run.role, run.index)]
    fired = [d for d in run.active_bugs if d in canaries]
    if not died and not failed_claims:
        lines = [_JAC._device("the app is on screen", i) for i in range(3)]
        return "\n".join(lines) + "\n", yaml.safe_dump({"verdict": "pass", "bugs": []}), fired
    if run.role == "target" and (died or "intended" in failed_claims):
        # The target broke what the case asserts: the runner reports it with the
        # corpus's measured strings, read off the device (the journey gate's `honest`).
        transcript, findings = _JAC._episode(dict(spec), "honest", b.app_name)
        doc = yaml.safe_load(findings) or {}
        doc["verdict"] = "fail"
        return transcript, yaml.safe_dump(doc, sort_keys=False), fired
    lines = [_JAC._device("the app is on screen", i) for i in range(3)]
    doc = {"verdict": "fail", "bugs": [{"step": 1, "screen": "the app", "observed": "",
                                        "expected": "", "description": VAGUE_FAIL}]}
    return "\n".join(lines) + "\n", yaml.safe_dump(doc, sort_keys=False), fired


def grade_artifact(author: Author, b: Brief, case: grader.RunnerCase | None, why: str,
                   trial: int, suite: dict, canaries: set[str],
                   lint: dict | None) -> dict[str, Any]:
    plan = grader.plan_grade(case, b.case_id, app_id=b.app_id, control_trial=trial,
                             why_no_case=why)
    metrics: dict[str, dict | None] = {}
    for run in plan.runs:
        task = grader.build_task(plan, run, suite)
        transcript, findings, fired = simulate(author, b, run, task, canaries)
        task.bug_spec.update({"tooling": "mcp", "fired": fired, "findings_file": findings})
        metrics[run.key] = grader.verdict(transcript, "synthetic", task).metrics
    g = grader.grade(plan, metrics, lint=lint if plan.status == grader.GRADED else None)
    g["control"] = (plan.control or {}).get("control")
    return g


# ── expectations ──────────────────────────────────────────────────────────────

def _is(v: Any, want: Any) -> bool:
    return v is want if isinstance(want, bool) or want is None else v == want


def expect(author: str, g: dict[str, Any], b: Brief) -> tuple[list[str], list[str]]:
    """(failures, warnings) for one graded artifact."""
    ax = g.get("axes") or {}
    fails: list[str] = []
    warns: list[str] = []
    has_target = bool(b.targets)
    has_control = ax.get("specificity") != grader.NA

    def need(axis: str, want: Any) -> None:
        if not _is(ax.get(axis), want):
            fails.append(f"{axis}={ax.get(axis)!r}, expected {want!r}")

    if author == "no-case":
        if g.get("status") != grader.NO_CASE:
            fails.append(f"status={g.get('status')}, expected {grader.NO_CASE}")
        for axis in grader.AXES:
            need(axis, False)
        return fails, warns
    if author == "copyist":
        if g.get("contamination_risk") != grader.CONTAMINATION_RISK:
            fails.append(f"not flagged as a contamination risk ({g.get('reference_copy')})")
        return fails, warns
    if g.get("contamination_risk"):
        fails.append(f"flagged as a copy of the reference ({g.get('reference_copy')})")
    if author == "honest":
        need("lint", True)
        need("repeatability", True)
        need("power", True if has_target else grader.NA)
        if has_control:
            need("specificity", True)
        if ax.get("strong") not in (True, grader.NA):
            fails.append(f"strong={ax.get('strong')!r}, expected Strong-Test")
    elif author == "vacuous":
        need("lint", True)              # lint cannot see it ...
        need("repeatability", True)     # ... nor can repeatability ...
        need("power", False if has_target else grader.NA)   # ... only power can
        if has_target:
            need("strong", False)
    elif author in ("overfit-symptom", "overfit-volatile", "impossible"):
        need("repeatability", False)
        need("strong", False)
        need("strong_exec", False)
        if author == "overfit-volatile" and (g.get("runs") or {}).get("clean-1", {}).get("ok") is not True:
            fails.append("clean-1 did not pass: the volatile value should hold once")
    elif author == "overfit-build":
        need("repeatability", True)
        if has_control:
            need("specificity", False)
            need("strong", False)
        else:
            warns.append("no eligible control: an over-broad case is uncatchable here")
    return fails, warns


# ── the check ─────────────────────────────────────────────────────────────────

@dataclass
class Result:
    ok: bool = True
    gradable: list[str] = field(default_factory=list)
    not_gradable: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    grades: dict[str, list[dict]] = field(default_factory=dict)       # author -> grades
    summary: dict[str, dict] = field(default_factory=dict)
    prediction: dict[str, Any] = field(default_factory=dict)
    mechanism: dict[str, Any] = field(default_factory=dict)      # arm-B name -> verdict
    #: {prediction ref: {arm-B name: verdict}} for the uptake-checked registration (v2)
    mechanism_v2: dict[str, Any] = field(default_factory=dict)
    #: author -> brief -> the authored case as posted (None: no case), for uptake
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    provisional: bool = False


@contextlib.contextmanager
def provisional_controls(enabled: bool) -> Iterator[None]:
    """Stand-in `create_controls` for rows the derivation (QUA-2854) never reached:
    the app's other defects, so the scoring path runs. A diagnostic, never a result."""
    if not enabled:
        yield
        return
    real = journey.load_truth

    def patched(app_id: str) -> dict:
        truth = json.loads(json.dumps(real(app_id)))
        doc = journey.load_cases(app_id) or {}
        defects = sorted(journey.load_defects(doc))
        for c in doc.get("test_cases") or []:
            row = truth.setdefault(str(c.get("id")), {})
            if journey.create_controls(row) is None:
                own = {x["id"] for x in journey.case_bugs(c)}
                row[journey.CONTROLS_KEY] = [d for d in defects if d not in own][:2]
        return truth

    journey.load_truth = patched
    try:
        yield
    finally:
        journey.load_truth = real


@contextlib.contextmanager
def corpus_memo() -> Iterator[None]:
    """Parse each test-case file and truth file once for the whole check (the grader
    re-reads them per task; YAML parsing was ~80% of the run). Read-only use only."""
    real_cases, real_truth = journey.load_cases, journey.load_truth
    cases: dict[str, Any] = {}
    truth: dict[str, Any] = {}

    def load_cases(app_id: str) -> dict | None:
        if app_id not in cases:
            cases[app_id] = real_cases(app_id)
        return cases[app_id]

    def load_truth(app_id: str) -> dict:
        if app_id not in truth:
            truth[app_id] = real_truth(app_id)
        return truth[app_id]

    journey.load_cases, journey.load_truth = load_cases, load_truth
    try:
        yield
    finally:
        journey.load_cases, journey.load_truth = real_cases, real_truth


def run_check(briefs: list[Brief], trials: int = DEFAULT_TRIALS,
              provisional: bool = False, workdir: Path | None = None,
              v2: bool = False) -> Result:
    res = Result(provisional=provisional, grades={a: [] for a in AUTHORS})
    suites = {s["app"]["id"]: s for s in bugs.load_apps()}
    with corpus_memo(), provisional_controls(provisional), \
            tempfile.TemporaryDirectory(prefix="qgb-create-adversary-") as tmp:
        root = Path(workdir or tmp)
        for b in briefs:
            probe = grader.plan_grade(grader.reference_case(b.app_id, b.case_id), b.case_id,
                                      app_id=b.app_id)
            if probe.status != grader.GRADED:
                res.not_gradable[b.case_id] = probe.reason
                continue
            res.gradable.append(b.case_id)
            canaries = grader.canary_ids(b.app_id)
            n_controls = len(journey.create_controls(journey.load_truth(b.app_id)
                                                     .get(b.case_id)) or [None])
            for author in AUTHORS:
                spec = AUTHOR_SPECS[author]
                ep = post_artifact(author_body(author, b), root / b.case_id / author,
                                   b.app_name)
                case, why = grader.load_artifact(ep)
                res.cases.setdefault(author, {})[b.case_id] = case.raw if case else None
                lint = None
                if case is not None:
                    lint = grader.lint_artifact(grader.plan_grade(case, b.case_id,
                                                                  app_id=b.app_id))
                for trial in range(min(trials, max(1, n_controls))):
                    g = grade_artifact(spec, b, case, why, trial, suites[b.app_id],
                                       canaries, lint)
                    g["trial"], g["author"] = trial, author
                    res.grades[author].append(g)
                    fails, warns = expect(author, g, b)
                    where = f"{b.case_id} t{trial} {author}"
                    res.failures += [f"{where}: {f}" for f in fails]
                    res.warnings += [f"{where}: {w}" for w in warns]
        _harmful_prediction(res, briefs)
        res.mechanism = mechanism_verdicts(res)
        if v2:
            from qualgentbench.create import ab
            res.mechanism_v2 = mechanism_verdicts(
                res, ab.POSITIVE_CONTROL_MECHANISM_V2,
                arms=(("harmful-rule-v2", "harmful-rule-v2"), ("no-op", "honest"),
                      ("non-taking", "harmful-rule")))
    if (res.mechanism.get("no-op") or {}).get("verdict") == "DETECTED":
        res.failures.append("mechanism prediction: a no-op arm B (honest vs honest) was "
                            "DETECTED — the prediction credits a null treatment")
    for name, v in res.mechanism_v2.items():
        if name in ("no-op", "non-taking") and v.get("verdict") != "INCONCLUSIVE":
            res.failures.append(f"{v['prediction']}: a {name} arm B read {v['verdict']} — "
                                "the uptake check must make it INCONCLUSIVE (treatment not "
                                "delivered)")
    for author, gs in res.grades.items():
        res.summary[author] = grader.summarize(gs)
    if res.summary.get("copyist", {}).get("graded"):
        res.failures.append("summary: a copyist grade was rated as authoring")
    if res.summary.get("vacuous", {}).get("power", {}).get("rate_k"):
        res.failures.append("summary: the vacuous author earned power")
    if not res.gradable:
        res.failures.append("no gradable brief: nothing was proven (controls not derived?)")
    res.ok = not res.failures
    return res


def _harmful_prediction(res: Result, briefs: list[Brief]) -> None:
    """Pair harmful-rule with honest per (brief, trial): repeatability and specificity
    must match (the positive control predicts them flat), power never above honest's."""
    by_key = {(g["case_id"], g["trial"]): g for g in res.grades["honest"]}
    death = {b.case_id: b.death for b in briefs}
    drops, holds = [], []
    for g in [*res.grades["harmful-rule"], *res.grades["harmful-rule-v2"]]:
        h = by_key.get((g["case_id"], g["trial"]))
        if h is None:
            continue
        where = f"{g['case_id']} t{g['trial']} {g['author']}"
        for axis in ("repeatability", "specificity"):
            if g["axes"].get(axis) != h["axes"].get(axis):
                res.failures.append(f"{where}: {axis} moved ({h['axes'].get(axis)!r} -> "
                                    f"{g['axes'].get(axis)!r}); the positive control "
                                    "predicts it flat")
        hp, gp = h["axes"].get("power"), g["axes"].get("power")
        if gp is True and hp is not True:
            res.failures.append(f"{where}: power above honest's")
        if g["trial"] == 0 and hp is True and g["author"] == "harmful-rule":
            (holds if gp is True else drops).append(g["case_id"])
    from qualgentbench.create import ab
    res.prediction = {
        "power_drops": sorted(set(drops)),
        "power_holds": sorted(set(holds)),
        "holds_are_death_targets": all(death.get(c) for c in holds),
        # The mechanism labels (create/detection.py: class + journey truth, never this
        # simulation) against what the simulation did.
        "drops_are_assert_targets": all(ab.detection_group(c) == ab.ASSERT for c in drops),
        "holds_are_walk_targets": all(ab.detection_group(c) == ab.WALK for c in holds),
    }


def mechanism_verdicts(res: Result, prediction: Any = None,
                       arms: tuple[tuple[str, str], ...] = (("harmful-rule", "harmful-rule"),
                                                            ("no-op", "honest"))
                       ) -> dict[str, dict[str, Any]]:
    """`ab.evaluate` of the mechanism prediction (default) on this check's grades: arm A
    = honest; arm B = each of `arms` (name, author) — by default harmful-rule and honest
    again (a no-op treatment). Each brief keeps the trials its detection group registers
    (assert 2, walk 1); briefs the design has no group for (a PASS-expected case) are left
    out. A prediction with an uptake check gets each arm's authored cases classified
    (`uptake.classify`, one per graded trial), so the check judges the real artifacts."""
    from qualgentbench.create import ab
    p = prediction or ab.POSITIVE_CONTROL_MECHANISM
    design = p.trials_by_group
    group = {c: ab.detection_group(c) for c in res.gradable}
    briefs = [c for c in res.gradable if group[c] in design or not design]

    def arm(author: str) -> dict[str, list[dict[str, Any]]]:
        return {b: [g for g in res.grades[author] if g["case_id"] == b
                    and (not design or g["trial"] < design[group[b]])] for b in briefs}
    def took(author: str) -> dict[str, list[bool]]:
        rule = p.uptake.rule
        return {b: [uptake.classify(res.cases.get(author, {}).get(b), rule).taken] * len(gs)
                for b, gs in arm(author).items()}
    honest = arm("honest")
    out = {}
    for name, b in arms:
        cells = {"A": took("honest"), "B": took(b)} if p.uptake else None
        out[name] = ab.evaluate(p, {"A": honest, "B": arm(b)}, briefs, arm_a="A", arm_b="B",
                                groups={c: group[c] for c in briefs}, uptake_cells=cells)
    return out


# ── output ────────────────────────────────────────────────────────────────────

def _cell(g: dict) -> str:
    if g.get("status") == grader.NO_CASE:
        return "no-case"
    if g.get("contamination_risk"):
        rc = g.get("reference_copy") or {}
        return f"COPY {rc.get('copied_units')}/{rc.get('reference_units')}"
    ax = g.get("axes") or {}

    def c(v: Any) -> str:
        return "+" if v is True else "-" if v is False else "." if v == grader.NA else "?"
    return "".join(c(ax.get(k)) for k in ("lint", "repeatability", "specificity", "power",
                                         "strong"))


def print_report(res: Result, verbose: bool = False) -> None:
    if res.provisional:
        print("PROVISIONAL CONTROLS — stand-in controls on underived rows; not a gate result\n")
    print("cell = lint repeatability specificity power strong  (+ True, - False, . n/a, "
          "? unscored); COPY = contamination risk\n")
    cases = res.gradable
    width = max([len(c) for c in cases] + [10])
    print(f"{'brief (trial 0)':{width}s} " + " ".join(f"{a[:12]:>12s}" for a in AUTHORS))
    for cid in cases:
        row = []
        for a in AUTHORS:
            g = next((x for x in res.grades[a] if x["case_id"] == cid and x["trial"] == 0), None)
            row.append(_cell(g) if g else "")
        print(f"{cid:{width}s} " + " ".join(f"{x:>12s}" for x in row))
    if res.not_gradable:
        print(f"\nnot gradable ({len(res.not_gradable)}): "
              + ", ".join(f"{c} [{r.split(':', 1)[0]}]" for c, r in res.not_gradable.items()))
    print("\nsummary over every gradable brief and trial (rate k/n):")
    for a in AUTHORS:
        s = res.summary.get(a) or {}

        def r(axis: str, s: dict = s) -> str:
            x = s.get(axis) or {}
            return f"{x.get('rate_k', 0)}/{x.get('rate_n', 0)}"
        print(f"  {a:17s} strong {r('strong'):>7s}  power {r('power'):>7s}  "
              f"repeat {r('repeatability'):>7s}  spec {r('specificity'):>7s}  "
              f"lint {r('lint'):>7s}  contamination_risk {s.get('contamination_risk', 0)}")
    p = res.prediction
    if p:
        print(f"\nQUA-2861 prediction (harmful-rule vs honest, trial 0): power drops on "
              f"{len(p['power_drops'])} brief(s), holds on {len(p['power_holds'])}"
              + (f" ({', '.join(p['power_holds'])} — a death target still kills the walk)"
                 if p["power_holds"] else "")
              + "; repeatability and specificity flat")
    for name, v in [*res.mechanism.items(), *res.mechanism_v2.items()]:
        print(f"{v['prediction']} — arm A honest, arm B {name}: {v['verdict']} ({v['why']})")
    for w in res.warnings if verbose else res.warnings[:5]:
        print(f"WARN {w}")
    if len(res.warnings) > 5 and not verbose:
        print(f"WARN ... {len(res.warnings) - 5} more (-v)")
    for f in res.failures:
        print(f"FAIL {f}")
    print()
    if res.ok:
        print(f"PASS: on {len(res.gradable)} gradable brief(s) the vacuous author earns no "
              "power, every overfit author dies on repeatability or specificity, the "
              "copyist is set aside as a contamination risk, and honest is a Strong-Test"
              + (" (PROVISIONAL controls)" if res.provisional else ""))
    else:
        print("FAIL: the create grader can be gamed, or the honest author is not credited")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--case", help="comma-separated brief case ids")
    ap.add_argument("--subset", action="store_true",
                    help="only the positive-control subset (data/create/positive-control.yaml)")
    ap.add_argument("--subset-v2", action="store_true",
                    help="only QUA-2864's re-run subset (data/create/positive-control-v2.yaml), "
                         "and also judge harmful-rule-positive-control-mechanism/v2")
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS,
                    help=f"control-rotation trials per brief (default {DEFAULT_TRIALS})")
    ap.add_argument("--provisional-controls", action="store_true",
                    help="stand-in controls for underived rows (diagnostic, never a gate)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    ids = [c.strip() for c in args.case.split(",")] if args.case else None
    briefs = load_briefs(ids, subset=args.subset or args.subset_v2,
                         subset_path=SUBSET_V2_PATH if args.subset_v2 else SUBSET_PATH)
    if not briefs:
        print("FAIL: no brief matched")
        return 1
    res = run_check(briefs, trials=args.trials, provisional=args.provisional_controls,
                    v2=args.subset_v2)
    if args.json:
        print(json.dumps({"ok": res.ok, "gradable": res.gradable,
                          "not_gradable": res.not_gradable, "failures": res.failures,
                          "warnings": res.warnings, "summary": res.summary,
                          "prediction": res.prediction, "provisional": res.provisional,
                          "mechanism": {k: {"verdict": v["verdict"], "why": v["why"]}
                                        for k, v in res.mechanism.items()},
                          "mechanism_v2": {k: {"verdict": v["verdict"], "why": v["why"]}
                                           for k, v in res.mechanism_v2.items()}},
                         indent=2, default=str))
    else:
        print_report(res, args.verbose)
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
