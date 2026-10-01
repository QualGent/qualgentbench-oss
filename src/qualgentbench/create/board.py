"""CreateBench v2: the create board (`qualgent-bench show --mode create`, QUA-2858).

The read-out of every authored-case grade (`create/grader.py`, QUA-2857) under a runs
dir. One row per (creation arm × author × runner); the journey reference cases graded
through the same path (`grader run --reference`) are the human-authored BASELINE row of
their runner. Columns, each a rate over the artifacts where that axis was SCORED, with
its Wilson interval (`grader.summarize`, `rates.fmt_pct_ci`):

    Strong-Test    lint-clean AND pass^3 AND power AND specificity   (the headline)
    strong_exec    the same without the lint conjunct (see "lint" below)
    lint-clean     no HARD rule of `create/lint.py` failed
    pass^3         repeatability: all three clean runs properly passed
    specificity    the control-only run properly passed
    power          the target-only run FAILED and the failure was the target's
    power | pass^3 power among artifacts whose three clean runs all passed (derived here)

and beside them the counts a reader needs before quoting a rate: artifacts, pending
(an artifact with no finished grade yet), unattributed target FAILs, excluded runs and
the axes they left unscored, `not_gradable` by reason (`controls_not_derived` — every
app but ankidroid on 2026-09-30 — or `unknown_case`), `no_case_created` (the author
saved nothing: counted, False on every axis), copies of the public reference case
(`contamination_risk`, QUA-2859: graded and shown, in NO rate), and the dollars spent
(authoring + grading). The per-brief detail repeats the axes per brief.

The headline is Strong-Test, never power: power is not conditioned on repeatability, so
an always-failing case earns it (QUA-2859's `impossible` author: power 100%, Strong-Test
0%). Power prints beside pass^3, with `power | pass^3` next to it.

Validity gating (the two rules the board never bends):

* `show --mode create` REFUSES to print while the create readiness gate is not READY.
  The gate (`check_tier_ready --tier create`, QUA-2859) is its own check; this module
  owns only the file it leaves behind — `<runs>/_runs/_create/gate.json`, written by
  `write_gate_status` (the gate calls it after every run) and read by `read_gate` — so
  the two agree on one format. A gate evaluated on another corpus version is STALE, which is not READY.
  `--ungated` prints anyway under a NOT QUOTABLE banner (diagnostics, never a number
  to publish).
* A row whose artifacts are not all graded shows its headline as `pending`, never a
  rate over the subset that happens to be finished.

Lint (an OPEN owner decision, QUA-2855 → QUA-2858): `content-anchors` is a HARD rule,
so an authored case that quotes fixture-seeded data (a deck name in ankidroid, a
medicine in medtimer, a task in tasksorg) is lint-dirty however well it executes. The
board does not decide whether that should sink Strong-Test: it prints `strong` and
`strong_exec` side by side and lists each row's HARD lint failures by rule, so the
owner can read the difference. The A/B driver's pre-registered positive control
(`create/ab.py`) predicts power, repeatability and specificity only — neither column.

Which grades are on the board. Every manifest under `<runs>/_runs/*/create_grades/`.
A manifest carries a `cell` block (`cell_block`) when the A/B driver or the grader CLI
wrote it: the arm (name + pinned SHAs), the author, the brief, the trial, the creation
episode it grades and its `kind`. `smoke` cells stay off the canonical board unless
asked for (`--include-smoke`); `--experiment` keeps one A/B experiment's cells (plus
the reference baseline for the same runner). A manifest without a cell block (graded
before this ticket) is on the board under arm `—`.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import corpus, rates
from . import grader

CELL_SCHEMA = "qualgentbench.create.cell/1"
GATE_SCHEMA = "qualgentbench.create.gate/1"
BOARD_SCHEMA = "qualgentbench.create.board/1"

# Cell kinds. Only CANONICAL and MANUAL cells are on the canonical board by default.
CANONICAL = "canonical"          # an A/B driver cell of a real experiment
SMOKE = "smoke"                  # a driver smoke / diagnostic run: never on the board
MANUAL = "manual"                # `grader run` by hand
KINDS = (CANONICAL, SMOKE, MANUAL)

REFERENCE_ARM = "reference"
REFERENCE_AUTHOR = "human (journey case)"
UNLABELLED = "—"

#: `create/runner.TASK_TYPE` (QUA-2856): a creation episode's task type.
CREATE_TASK_TYPE = "create_case"

#: The board columns, in print order: (axis key, header). `power_given_pass3` is the
#: board's own (`axis_value`); every other key is a grade's axis.
COLUMNS = (("strong", "Strong-Test"), ("strong_exec", "strong_exec"),
           ("lint", "lint-clean"), ("repeatability", "pass^3"),
           ("specificity", "specificity"), ("power", "power"),
           ("power_given_pass3", "power | pass^3"))

#: Why power is never a headline (QUA-2859's `impossible` author, TODO(QUA-2858) in
#: `grader.grade`): power is not conditioned on repeatability, so a case that fails on
#: EVERY run earns power whenever the target's canary fires — 100% power, 0% Strong-Test.
POWER_NOTE = ("power alone is not a quality score: an always-failing case earns it "
              "(QUA-2859 `impossible`: power 100%, Strong-Test 0%). The headline is "
              "Strong-Test; read power beside pass^3, or `power | pass^3` = power among "
              "artifacts whose three clean runs all passed.")

#: Shown under every board until the owner decides QUA-2855's open question.
LINT_NOTE = ("lint: `content-anchors` is HARD, so a case quoting fixture-seeded data is "
             "lint-dirty (ankidroid, medtimer, tasksorg). Strong-Test keeps the lint "
             "conjunct; strong_exec drops it. Both are shown pending the owner's decision "
             "(QUA-2858).")


# ── the cell block ─────────────────────────────────────────────────────────────

def cell_block(*, kind: str, case_id: str, trial: int, source: str = "authored",
               experiment: str | None = None, arm: str | None = None,
               arm_manifest: dict[str, Any] | None = None,
               author: dict[str, Any] | None = None,
               creation_episode: str | None = None,
               creation: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a grade manifest says about WHO authored the graded case, harness-side.

    `trial` is 1-based (the control rotates with `trial - 1`). `creation_episode` is
    the creation episode dir relative to the runs dir (the link that clears a pending
    artifact); `creation` is that episode's outcome, validity flags, cost and wall time.
    `arm_manifest` is `ResolvedArm.manifest()` — SHAs and hashes, never private text."""
    if kind not in KINDS:
        raise ValueError(f"cell kind {kind!r} is not one of {KINDS}")
    return {"schema": CELL_SCHEMA, "kind": kind, "experiment": experiment,
            "source": source, "case_id": case_id, "trial": int(trial),
            "arm": REFERENCE_ARM if source == "reference" else arm,
            "arm_manifest": arm_manifest, "author": author,
            "creation_episode": creation_episode, "creation": creation}


def arm_label(name: str | None, arm_manifest: dict[str, Any] | None) -> str:
    """`name@sha7` of the QualGent-MCP pin — the label `create/runner.arm_label` prints
    for a creation episode, so a board row and its creation episodes read alike."""
    if not name and not arm_manifest:
        return UNLABELLED
    sha = (((arm_manifest or {}).get("qualgent_mcp") or {}).get("sha") or "")[:7]
    name = name or (arm_manifest or {}).get("name") or "?"
    return f"{name}@{sha}" if sha else str(name)


def agent_label(d: dict[str, Any] | None) -> str:
    from ..leaderboard import clean_model_name
    if not d or not (d.get("agent") or d.get("model")):
        return UNLABELLED
    return f"{d.get('agent') or '?'}/{clean_model_name(str(d.get('model') or '?'))}"


def rated(grades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The grades a rate may count: GRADED and NO_CASE, minus a copy of the public
    reference case (`contamination_risk`, QUA-2859) — the same set `grader.summarize`
    rates, so the board, the A/B judge and the grader cannot disagree on a denominator."""
    return [g for g in grades if g.get("status") in (grader.GRADED, grader.NO_CASE)
            and not g.get("contamination_risk")]


def axis_value(g: dict[str, Any], axis: str) -> Any:
    """A grade's value on `axis` (True / False / None = unscored / "n/a"), including the
    board's derived `power_given_pass3`: power, among artifacts whose three clean runs
    all passed (None when pass^3 is not True — such an artifact has no power to read)."""
    axes = g.get("axes") or {}
    if axis != "power_given_pass3":
        return axes.get(axis)
    power, rep = axes.get("power"), axes.get("repeatability")
    if power == grader.NA:
        return grader.NA
    return power if rep is True and power in (True, False) else None


def axis_stats(grades: list[dict[str, Any]], axis: str) -> dict[str, Any]:
    vals = [axis_value(g, axis) for g in rated(grades)]
    k = sum(1 for v in vals if v is True)
    n = sum(1 for v in vals if v is True or v is False)
    r = rates.rate(k, n)
    return {"rate": round(r.p, 4) if r else None,
            "ci": [round(r.lo, 4), round(r.hi, 4)] if r else None, "k": k, "n": n,
            "unscored": sum(1 for v in vals if v is None),
            "na": sum(1 for v in vals if v == grader.NA)}


# ── the readiness gate's status file (QUA-2859 writes it, the board reads it) ─────

READY, NOT_READY, MISSING, STALE, UNREADABLE = ("READY", "NOT READY", "MISSING", "STALE",
                                                "UNREADABLE")


def gate_path(runs_dir: Path | str) -> Path:
    from ..checkpoint import RUN_META_DIR
    return Path(runs_dir) / RUN_META_DIR / "_create" / "gate.json"


def write_gate_status(runs_dir: Path | str, *, ready: bool,
                      checks: list[dict[str, Any]], tool: str = "check_tier_ready --tier create"
                      ) -> Path:
    """The gate's verdict as the board reads it. `checks` = `[{"name", "ok", "detail"}]`
    (every line the gate printed). Stamped with the corpus version it was evaluated on,
    so an edited corpus makes it STALE rather than silently READY."""
    path = gate_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema": GATE_SCHEMA, "status": READY if ready else NOT_READY,
           "ready": bool(ready), "checks": list(checks), "tool": tool,
           "at": datetime.now(UTC).isoformat(timespec="seconds"), **corpus.stamp()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)
    return path


@dataclass
class GateStatus:
    state: str                           # READY | NOT READY | MISSING | STALE | UNREADABLE
    detail: str = ""
    failing: list[str] = field(default_factory=list)
    at: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == READY

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "failing": self.failing,
                "at": self.at}


def read_gate(runs_dir: Path | str) -> GateStatus:
    path = gate_path(runs_dir)
    if not path.exists():
        return GateStatus(MISSING, f"no gate status at {path} — run the create readiness "
                                   "gate (check_tier_ready --tier create, QUA-2859)")
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return GateStatus(UNREADABLE, f"{path}: {exc}")
    failing = [str(c.get("name")) for c in doc.get("checks") or [] if not c.get("ok")]
    at = doc.get("at")
    if doc.get("corpus_version") != corpus.corpus_version():
        return GateStatus(STALE, f"the gate ran on corpus {doc.get('corpus_version')}, the "
                                 f"corpus is now {corpus.corpus_version()} — run it again",
                          failing, at)
    if not doc.get("ready") or doc.get("status") != READY:
        return GateStatus(NOT_READY, f"{len(failing)} check(s) failing", failing, at)
    return GateStatus(READY, f"gate READY at {at}", [], at)


# ── loading ────────────────────────────────────────────────────────────────────

@dataclass
class GradeRecord:
    path: Path
    run_id: str
    doc: dict[str, Any]

    @property
    def grade(self) -> dict[str, Any] | None:
        return self.doc.get("grade")

    @property
    def plan(self) -> dict[str, Any]:
        return self.doc.get("plan") or {}

    @property
    def cell(self) -> dict[str, Any]:
        return self.doc.get("cell") or {}

    @property
    def runner(self) -> dict[str, Any]:
        return self.doc.get("runner") or {}

    @property
    def pending(self) -> bool:
        return self.grade is None

    @property
    def source(self) -> str:
        return str((self.grade or {}).get("source") or (self.plan.get("case") or {}).get("source")
                   or self.cell.get("source") or "authored")

    @property
    def case_id(self) -> str:
        return str(self.plan.get("case_id") or self.cell.get("case_id") or "?")

    @property
    def app_id(self) -> str:
        return str(self.plan.get("app_id") or "")

    @property
    def kind(self) -> str:
        return str(self.cell.get("kind") or MANUAL)

    @property
    def row_key(self) -> tuple[str, str, str]:
        runner = agent_label(self.runner)
        if self.source == "reference":
            return (REFERENCE_ARM, REFERENCE_AUTHOR, runner)
        return (arm_label(self.cell.get("arm"), self.cell.get("arm_manifest")),
                agent_label(self.cell.get("author")), runner)

    @property
    def grade_cost(self) -> float | None:
        c = (self.doc.get("cost") or {}).get("cost_usd")
        return float(c) if isinstance(c, int | float) else None

    @property
    def creation_cost(self) -> float | None:
        c = (self.cell.get("creation") or {}).get("cost_usd")
        return float(c) if isinstance(c, int | float) else None

    @property
    def corpus_version(self) -> str | None:
        return (self.doc.get("corpus") or {}).get("corpus_version")


def load_grades(runs_dir: Path | str, run_ids: list[str] | None = None) -> list[GradeRecord]:
    """Every grade manifest under `<runs>/_runs/*/create_grades/` (only `run_ids` when
    given). An unreadable manifest is skipped, never fatal."""
    from ..checkpoint import RUN_META_DIR
    root = Path(runs_dir) / RUN_META_DIR
    out: list[GradeRecord] = []
    for path in sorted(root.glob(f"*/{grader.GRADES_DIR}/*.json")):
        run_id = path.parent.parent.name
        if run_ids and run_id not in run_ids:
            continue
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("schema") == grader.MANIFEST_SCHEMA:
            out.append(GradeRecord(path=path, run_id=run_id, doc=doc))
    return out


def load_creation_episodes(runs_dir: Path | str, run_ids: list[str] | None = None) -> list:
    """Creation episodes (`run --mode create`, QUA-2856) — the artifacts a grade clears."""
    from ..leaderboard import load_results
    out = [r for r in load_results(Path(runs_dir)) if r.task_type == CREATE_TASK_TYPE]
    return [r for r in out if not run_ids or r.run_id in run_ids]


def select(records: list[GradeRecord], *, experiment: str | None = None,
           include_smoke: bool = False) -> list[GradeRecord]:
    """The canonical selection: no smoke cells unless asked; with `experiment`, only
    that experiment's cells plus the reference baseline of the runners it used."""
    keep = [r for r in records if include_smoke or r.kind != SMOKE]
    if experiment is None:
        return keep
    mine = [r for r in keep if r.cell.get("experiment") == experiment]
    runners = {agent_label(r.runner) for r in mine}
    refs = [r for r in keep if r.source == "reference" and r.cell.get("experiment") != experiment
            and agent_label(r.runner) in runners]
    return mine + refs


def ungraded_creations(creations: list, records: list[GradeRecord],
                       runs_dir: Path | str) -> dict[tuple[str, str], int]:
    """{(arm label, author label): count} of creation episodes no grade manifest points
    at — artifacts still to grade. Excluded creation episodes (env failure, rate limit,
    contamination) measure nothing and are not pending."""
    from ..failures import is_excluded
    from ..result import resolve_artifact_dir
    graded = set()
    for r in records:
        ep = r.cell.get("creation_episode")
        if ep:
            graded.add(str((Path(runs_dir) / ep).resolve()))
    out: dict[tuple[str, str], int] = defaultdict(int)
    for c in creations:
        if is_excluded(c.metrics or {}):
            continue
        d = resolve_artifact_dir(runs_dir, c)
        if d is not None and str(d.resolve()) in graded:
            continue
        arm = ((c.provenance or {}).get("create") or {}).get("arm") or {}
        out[(arm_label(arm.get("name"), arm), agent_label({"agent": c.agent,
                                                          "model": c.model}))] += 1
    return dict(out)


# ── the board ──────────────────────────────────────────────────────────────────

def _row(key: tuple[str, str, str], recs: list[GradeRecord], extra_pending: int) -> dict:
    grades = [r.grade for r in recs if r.grade is not None]
    summary = grader.summarize(grades)
    runs = [run for g in grades for run in (g.get("runs") or {}).values()]
    lint_fail: Counter = Counter()
    for g in grades:
        if g.get("status") == grader.GRADED:
            lint_fail.update((g.get("lint") or {}).get("hard_failed") or [])
    pending = sum(1 for r in recs if r.pending) + extra_pending
    grade_costs = [r.grade_cost for r in recs if r.grade_cost is not None]
    creation_costs = [r.creation_cost for r in recs if r.creation_cost is not None]
    total = sum(grade_costs) + sum(creation_costs)
    versions = sorted({v for r in recs if (v := r.corpus_version)})
    row = {
        "arm": key[0], "author": key[1], "runner": key[2],
        "baseline": key[0] == REFERENCE_ARM,
        "artifacts": len(recs) + extra_pending, "graded": summary["graded"],
        "pending": pending,
        "no_case_created": summary["no_case_created"],
        "not_gradable": summary["not_gradable"],
        # A copy of the public reference case: graded, shown, and in NO rate (QUA-2859).
        "contamination_risk": summary.get("contamination_risk", 0),
        "unattributed_fail_runs": sum(1 for x in runs if x.get("outcome") == grader.UNATTRIBUTED),
        "excluded_runs": sum(1 for x in runs if x.get("outcome") == grader.EXCLUDED),
        "lint_hard_failures": dict(lint_fail.most_common()),
        "cost_usd": round(total, 4),
        # Over FINISHED grades only: a running grade has not written its cost yet.
        "cost_per_artifact_usd": (round(total / len(grades), 4) if grades else None),
        "unpriced_grades": sum(1 for r in recs if r.grade is not None and r.grade_cost is None
                               and r.grade.get("status") == grader.GRADED),
        "corpus_versions": versions,
        "axes": {axis: axis_stats(grades, axis) for axis, _ in COLUMNS},
    }
    # The headline is Strong-Test and nothing else — never power (POWER_NOTE).
    row["headline"] = "pending" if pending else row["axes"]["strong"]
    return row


def _brief_detail(records: list[GradeRecord]) -> list[dict[str, Any]]:
    by_case: dict[str, dict[tuple, list[GradeRecord]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_case[r.case_id][r.row_key].append(r)
    out = []
    for case_id in sorted(by_case):
        cells = []
        for key in sorted(by_case[case_id], key=_row_order):
            recs = by_case[case_id][key]
            grades = [r.grade for r in recs if r.grade is not None]
            s = grader.summarize(grades)
            cells.append({"arm": key[0], "author": key[1], "runner": key[2],
                          "artifacts": len(recs), "pending": sum(1 for r in recs if r.pending),
                          "no_case_created": s["no_case_created"],
                          "not_gradable": s["not_gradable"],
                          "contamination_risk": s.get("contamination_risk", 0),
                          "axes": {axis: axis_stats(grades, axis) for axis, _ in COLUMNS}})
        app = next((r.app_id for recs in by_case[case_id].values() for r in recs if r.app_id), "")
        out.append({"case_id": case_id, "app_id": app, "rows": cells})
    return out


def _row_order(key: tuple[str, str, str]) -> tuple:
    return (key[0] == REFERENCE_ARM, key[2], key[0], key[1])


def build_board(records: list[GradeRecord], *, pending_creations: dict | None = None,
                gate: GateStatus | None = None, title: str = "") -> dict[str, Any]:
    """The board as data (what `--json` prints and the view renders)."""
    groups: dict[tuple, list[GradeRecord]] = defaultdict(list)
    for r in records:
        groups[r.row_key].append(r)
    extra: dict[tuple, int] = defaultdict(int)
    for (arm, author), n in (pending_creations or {}).items():
        keys = [k for k in groups if k[0] == arm and k[1] == author]
        if keys:
            for k in keys:
                extra[k] += n
        else:
            groups[(arm, author, UNLABELLED)] = []
            extra[(arm, author, UNLABELLED)] += n
    rows = [_row(k, groups[k], extra.get(k, 0)) for k in sorted(groups, key=_row_order)]
    mixed = [f"{r['arm']} · {r['author']} · {r['runner']}" for r in rows
             if len(r["corpus_versions"]) > 1]
    return {"schema": BOARD_SCHEMA, "title": title,
            "gate": gate.as_dict() if gate else None,
            "corpus": corpus.stamp(), "rows": rows, "briefs": _brief_detail(records),
            "notes": [POWER_NOTE, LINT_NOTE] + ([(f"rows blending grades from more than one corpus "
                                      f"version: {', '.join(mixed)}")] if mixed else [])}


# ── rendering ──────────────────────────────────────────────────────────────────

def fmt_axis(cell: dict[str, Any] | None) -> str:
    if not cell:
        return "—"
    body = rates.fmt_pct_ci(cell.get("rate"), cell.get("ci"), cell.get("k"), cell.get("n"))
    if body == "—" and cell.get("na"):
        body = "n/a"
    extra = []
    if cell.get("unscored"):
        extra.append(f"{cell['unscored']} unscored")
    return body + (f" ({', '.join(extra)})" if extra else "")


def fmt_headline(row: dict[str, Any]) -> str:
    h = row.get("headline")
    return f"pending ({row['pending']} ungraded)" if h == "pending" else fmt_axis(h)


def _fmt_counts(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) if d else "0"


def render_text(board: dict[str, Any]) -> list[str]:
    out: list[str] = []
    gate = board.get("gate") or {}
    if board.get("title"):
        out.append(board["title"])
    if gate and gate.get("state") != READY:
        out.append(f"!! UNGATED — create readiness gate {gate.get('state')}: "
                   f"{gate.get('detail')}. NOT QUOTABLE.")
    out.append(f"corpus {board['corpus'].get('corpus_version')} · "
               f"{len(board['rows'])} row(s) · rates = k/n over artifacts where the axis "
               "was scored, Wilson 95% CI")
    for row in board["rows"]:
        label = (f"BASELINE {row['runner']} (reference cases)" if row["baseline"]
                 else f"{row['arm']} · author {row['author']} · runner {row['runner']}")
        out.append("")
        out.append(f"{label} — {row['artifacts']} artifact(s), {row['graded']} graded, "
                   f"{row['pending']} pending")
        out.append(f"  Strong-Test   {fmt_headline(row)}")
        for axis, header in COLUMNS[1:]:
            out.append(f"  {header:<13} {fmt_axis(row['axes'][axis])}")
        out.append(f"  unattributed-fail {row['unattributed_fail_runs']} run(s) · excluded "
                   f"{row['excluded_runs']} run(s) · no case {row['no_case_created']} · "
                   f"not gradable {_fmt_counts(row['not_gradable'])} · copy of reference "
                   f"(excluded from rates) {row['contamination_risk']}")
        cost = f"${row['cost_usd']:.2f}"
        if row.get("cost_per_artifact_usd") is not None:
            cost += f" (${row['cost_per_artifact_usd']:.2f}/artifact)"
        if row.get("unpriced_grades"):
            cost += f", {row['unpriced_grades']} grade(s) unpriced"
        out.append(f"  cost {cost}")
        if row["lint_hard_failures"]:
            out.append(f"  lint HARD failures: {_fmt_counts(row['lint_hard_failures'])}")
    if board["briefs"]:
        out.append("")
        out.append("per brief (k/n: Strong-Test · strong_exec · pass^3 · specificity · power · "
                   "power | pass^3)")
        for b in board["briefs"]:
            out.append(f"  {b['case_id']} [{b['app_id']}]")
            for c in b["rows"]:
                who = ("reference" if c["arm"] == REFERENCE_ARM else f"{c['arm']} · {c['author']}")
                status = []
                if c["pending"]:
                    status.append(f"{c['pending']} pending")
                if c["no_case_created"]:
                    status.append(f"{c['no_case_created']} no case")
                if c["not_gradable"]:
                    status.append("not gradable: " + _fmt_counts(c["not_gradable"]))
                if c.get("contamination_risk"):
                    status.append(f"{c['contamination_risk']} copy of reference, excluded")
                cells = " · ".join(_kn(c["axes"][a]) for a in
                                   ("strong", "strong_exec", "repeatability", "specificity",
                                    "power", "power_given_pass3"))
                out.append(f"    {who:<40} {cells}" + (f"  [{'; '.join(status)}]"
                                                      if status else ""))
    for note in board.get("notes") or []:
        out.append("")
        out.append(f"note: {note}")
    return out


def _kn(cell: dict[str, Any]) -> str:
    if not cell.get("n"):
        return "n/a" if cell.get("na") else "—"
    return f"{cell['k']}/{cell['n']}"


E = html.escape


def render_html(board: dict[str, Any], *, css_href: str = "style.css") -> str:
    """The board as one standalone page (the view writes it beside its index as
    `create.html`, so a portable view carries it to the bench viewer)."""
    gate = board.get("gate") or {}
    banner = ""
    if gate.get("state") and gate["state"] != READY:
        banner = (f'<p class="banner">Create readiness gate {E(gate["state"])}: '
                  f'{E(str(gate.get("detail") or ""))}. These numbers are NOT quotable.</p>')
    head = "".join(f"<th>{E(h)}</th>" for _, h in COLUMNS)
    body = []
    for row in board["rows"]:
        label = ("BASELINE — reference cases" if row["baseline"]
                 else f"{row['arm']} · {row['author']}")
        cells = [E(fmt_headline(row))] + [E(fmt_axis(row["axes"][a])) for a, _ in COLUMNS[1:]]
        body.append(
            f"<tr{' class=moved' if row['baseline'] else ''}><td>{E(label)}</td>"
            f"<td>{E(row['runner'])}</td><td>{row['artifacts']}</td><td>{row['pending']}</td>"
            + "".join(f"<td>{c}</td>" for c in cells)
            + f"<td>{row['unattributed_fail_runs']}</td><td>{row['excluded_runs']}</td>"
              f"<td>{row['no_case_created']}</td><td>{E(_fmt_counts(row['not_gradable']))}</td>"
              f"<td>{row['contamination_risk']}</td>"
              f"<td>${row['cost_usd']:.2f}</td>"
              f"<td>{E(_fmt_counts(row['lint_hard_failures']))}</td></tr>")
    briefs = []
    for b in board["briefs"]:
        for c in b["rows"]:
            who = "reference" if c["arm"] == REFERENCE_ARM else f"{c['arm']} · {c['author']}"
            status = (f"{c['pending']} pending; " if c["pending"] else "") + (
                f"no case {c['no_case_created']}; " if c["no_case_created"] else "") + (
                f"not gradable: {_fmt_counts(c['not_gradable'])}; " if c["not_gradable"] else "") + (
                f"{c['contamination_risk']} copy of reference, excluded"
                if c.get("contamination_risk") else "")
            briefs.append(f"<tr><td>{E(b['case_id'])}</td><td>{E(b['app_id'])}</td>"
                          f"<td>{E(who)}</td><td>{E(c['runner'])}</td>"
                          + "".join(f"<td>{E(_kn(c['axes'][a]))}</td>" for a, _ in COLUMNS)
                          + f"<td>{E(status)}</td></tr>")
    notes = "".join(f"<li>{E(n)}</li>" for n in board.get("notes") or [])
    title = board.get("title") or "CreateBench board"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{E(title)}</title><link rel="stylesheet" href="{E(css_href)}"></head>
<body>
<h1>{E(title)}</h1>
{banner}
<p class="dim">Corpus {E(str(board['corpus'].get('corpus_version')))}. Each rate is k/n over the
artifacts where that axis was scored, with a Wilson 95% interval; excluded runs leave an axis
unscored, never 0. A row with ungraded artifacts shows its headline as pending.</p>
<div class="tablewrap"><table class="idx"><thead><tr><th>arm · author</th><th>runner</th>
<th>artifacts</th><th>pending</th>{head}<th>unattributed fail</th><th>excluded runs</th>
<th>no case</th><th>not gradable</th><th>copy of reference (excluded)</th><th>cost</th>
<th>lint HARD failures</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
<h2>Per brief</h2>
<div class="tablewrap"><table class="idx"><thead><tr><th>brief</th><th>app</th><th>arm · author</th>
<th>runner</th>{head}<th>status</th></tr></thead><tbody>{''.join(briefs)}</tbody></table></div>
<ul class="dim">{notes}</ul>
</body></html>
"""


def board_for(runs_dir: Path | str, *, run_ids: list[str] | None = None,
              experiment: str | None = None, include_smoke: bool = False,
              title: str = "") -> dict[str, Any]:
    """Load, select and build — the one call `show` and `view` share."""
    runs_dir = Path(runs_dir)
    records = select(load_grades(runs_dir, run_ids), experiment=experiment,
                     include_smoke=include_smoke)
    if experiment is None:
        pending = ungraded_creations(load_creation_episodes(runs_dir, run_ids), records,
                                     runs_dir)
    else:
        from .ab import pending_cells
        pending = pending_cells(runs_dir, experiment)
    return build_board(records, pending_creations=pending, gate=read_gate(runs_dir),
                       title=title)
