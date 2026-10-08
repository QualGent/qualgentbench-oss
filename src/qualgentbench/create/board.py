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
    power (report) power, OR the runner reported the target on a PASS (grader v3,
                   QUA-2865 — never in Strong-Test; see REPORT_NOTE)

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

Power is also split by DETECTION group (QUA-2862, every row, every experiment): `assert`
briefs (the target is silent unless the case checks the state — power there means the
case asserts the right thing) and `walk` briefs (the target kills or freezes the app on
the route — power there mostly means the case reached the feature). The label is
`create/detection.py`'s, derived from the target's class + journey truth; a brief it
cannot label is counted under `unlabelled`. Pooled power blends the two mechanisms, so a
reader should never quote it without the split.

Control reach (QUA-2867), per row and per brief. Controls are derived on the REFERENCE
route; an authored route can walk into one (QUA-2861 rerun: tasks-complete-parent's
authored routes reached `subtask-filed-before-written` on 2/2 runs, which crashed the app).
`control_reach` reads every rated control run: `read` (the control's canary was read),
`fired` (it fired — the route reached the control), the same over OFF-REFERENCE controls
only (relation `same-screen`/`other`: a `side` control fires on the reference route by
design, so its firing says nothing about eligibility), and `excluded` (grader v4's
`control_reached`: reached AND the app died — that artifact's specificity is unscored).
`fired` is the run's recorded `control_fired`, or for a grade written before v4 the
control's id in its `fault_fired` (the same canary read, so the reach of an older grade is
readable; its exclusions are not — it scored them). A brief is WARNED when its control
runs were excluded at `CONTROL_REACH_WARN` or more, or its off-reference controls fired on
that share of their read runs (`reach_warning`): eligibility measured on the reference
route does not describe the authored routes there, so read its specificity with care and
consider re-ranking its controls (`scripts/derive_create_controls.py --rejudge`, rank
rule 2).

Grader versions (QUA-2865). From grader v3 a run during which the app died is a FAIL
whatever the runner wrote, so `power` moves on crash/ANR targets the app recovers from; a
row blending v2 and v3 grades is named in the notes (`grader_versions` per row), like a
row blending corpus versions. A v2 grade has no `power (report)` and is left out of that
column, never counted as unscored.

Charts (QUA-2922, `render_html`, drawn with `viz`): K1 Strong-Test and strong_exec per
row with Wilson intervals (a pending row draws no Strong-Test); K2 power per detection
group; K3 a per-brief table of power k/n on the `--seq` ramp with `HEAT_GLYPHS`. The board
tables under them are their table twins; the view also writes the board dict itself as
`create.json`.

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
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import corpus, glossary, rates, viz
from . import detection, grader

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
           ("power_given_pass3", "power | pass^3"), ("power_report", "power (report)"))

#: Why power is never a headline (QUA-2859's `impossible` author, TODO(QUA-2858) in
#: `grader.grade`): power is not conditioned on repeatability, so a case that fails on
#: EVERY run earns power whenever the target's canary fires — 100% power, 0% Strong-Test.
POWER_NOTE = ("power alone is not a quality score: an always-failing case earns it "
              "(QUA-2859 `impossible`: power 100%, Strong-Test 0%). The headline is "
              "Strong-Test; read power beside pass^3, or `power | pass^3` = power among "
              "artifacts whose three clean runs all passed.")

#: Why report-credited power is a column and never a headline or a Strong-Test conjunct.
REPORT_NOTE = ("power (report) = power, or the runner REPORTED the target on a PASS "
               "verdict (canary fired where read; grader v3, QUA-2865). It reads what the "
               "runner saw, not what the case checks: a case whose only check is \"the app "
               "is still open\" earns it on every target the runner notices on its route "
               "(QUA-2861 arm B: 7 of 16 DROP runs). Quote verdict-only power; read this "
               "beside it.")

#: A brief's control reach is WARNED at this share (QUA-2867; see the module docstring).
#: QUA-2861 rerun: tasks-complete-parent 2/8 control runs excluded under v4 and 2/4
#: off-reference runs fired; every other brief 0 excluded, 0 off-reference fired.
CONTROL_REACH_WARN = 0.25

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
               creation: dict[str, Any] | None = None,
               uptake: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a grade manifest says about WHO authored the graded case, harness-side.

    `trial` is 1-based (the control rotates with `trial - 1`). `creation_episode` is
    the creation episode dir relative to the runs dir (the link that clears a pending
    artifact); `creation` is that episode's outcome, validity flags, cost and wall time.
    `arm_manifest` is `ResolvedArm.manifest()` — SHAs and hashes, never private text.
    `uptake` (QUA-2864, written only when the experiment registers a manipulation check)
    is `{"rule", "taken", ...}`: whether the graded case takes the harmful rule."""
    if kind not in KINDS:
        raise ValueError(f"cell kind {kind!r} is not one of {KINDS}")
    return {"schema": CELL_SCHEMA, "kind": kind, "experiment": experiment,
            "source": source, "case_id": case_id, "trial": int(trial),
            "arm": REFERENCE_ARM if source == "reference" else arm,
            "arm_manifest": arm_manifest, "author": author,
            "creation_episode": creation_episode, "creation": creation,
            **({"uptake": uptake} if uptake is not None else {})}


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


def carries(g: dict[str, Any], axis: str) -> bool:
    """Whether a grade was written with `axis` at all (`power_report` exists from grader
    v3): a grade without it is left out of that axis, never counted as unscored."""
    return axis == "power_given_pass3" or axis in (g.get("axes") or {})


def axis_stats(grades: list[dict[str, Any]], axis: str) -> dict[str, Any]:
    vals = [axis_value(g, axis) for g in rated(grades) if carries(g, axis)]
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


#: The gate file as messages name it: relative to the runs dir, never `gate_path`'s
#: absolute path — a message reaches `create.html`, which a portable view publishes.
GATE_LABEL = f"<runs>/{gate_path('').as_posix()}"


#: A path-name character (QUA-2946): what a root must not be followed by to be replaced
#: (`/x/runs` is not the start of `/x/runs-v3`), and what a generic home's user name is
#: made of — anything but a separator, whitespace, quoting or markup, or a JSON `\\uXXXX`
#: escape (a non-ASCII name as `json.dumps` writes it).
_NAME_CHAR = r"(?:[^\\/\s\"'<>&;:,|*?()\[\]{}=]|\\u[0-9a-fA-F]{4})"
#: Any absolute home directory, not only this machine's (QUA-2946): `/Users/<name>`,
#: `/home/<name>`, `C:\\Users\\<name>` (single, JSON-doubled or forward slashes). Not
#: preceded by a name character or `.`/`-` (`example.com/Users/x`, `/data/home/x`).
_ANY_HOME = re.compile(
    r"(?<![\w.\-])(?:/(?:Users|home)/|[A-Za-z]:(?:\\{1,4}|/)Users(?:\\{1,4}|/))"
    + _NAME_CHAR + "+")
#: The same home with JSON-escaped forward slashes (QUA-2950): `\/Users\/<name>`,
#: `\/home\/<name>` → `~`, as a JSON writer that escapes `/` records it.
_ESCAPED_HOME = re.compile(r"(?<![\w.\-])\\/(?:Users|home)\\/" + _NAME_CHAR + "+")
#: A home flattened into one path segment (QUA-2950): `-Users-<name>` / `-home-<name>`,
#: `/` and `.` turned into `-` as Claude Code names a project's directory
#: (`projects/-Users-<name>--qualgentbench-runs-<case>-<date>…`, a scratchpad under
#: `/tmp/claude-<uid>/`) → `-~`. Only where it starts a segment (after the start, `/` or any
#: non-alphanumeric character) and the name (`[A-Za-z0-9_]+`) is followed by `-`, so
#: `user-home-page` and a bare `-Users-` stay. The flattening loses where a name ends: a
#: name containing `-` (`-Users-mary-jane--x`) is cut at its first `-` (`-~-jane--x`).
_FLAT_HOME = re.compile(r"(?<![A-Za-z0-9])-(?:Users|home)-[A-Za-z0-9_]+(?=-)")
#: A per-user temp root (QUA-2950) → `<tmp>`: an agent's `/tmp/claude-<uid>` (or
#: `/private/tmp/…`, macOS's real path) and macOS's `/var/folders/<xx>/<id>/T` (or
#: `/private/var/…`), plain or with JSON-escaped slashes. Never mid-path (`/data/tmp/…`)
#: and only as a whole segment (`/tmp/claude-502x`, `/tmp/claude-code` stay).
_TMP_ROOT = re.compile(
    r"(?<![\w.\-])(?:\\?/private)?\\?/(?:tmp\\?/claude-[0-9]+"
    r"|var\\?/folders\\?/[\w+\-]+\\?/[\w+\-]+\\?/T)(?!" + _NAME_CHAR + ")")


def scrub_paths(text: str, runs_dir: Path | str, runs_label: str = "<runs>",
                tmp_label: str = "<tmp>") -> str:
    """`text` with the runs dir's absolute path written `<runs>` and any home dir `~`,
    for a message a page carries (`create.html`, an experiment's report, an episode's
    page) and every text file a portable view copies (QUA-2946): a portable view is
    published, and a local path names the publisher's machine and account.
    `runs_label` / `tmp_label` are what replace the runs dir and a temp root
    (`&lt;runs&gt;` / `&lt;tmp&gt;` in HTML).

    The runs dir and this machine's home go first, longest first, each only where it
    ends a path segment; then, in this order, the generic forms: any other absolute home
    (`_ANY_HOME`: another machine's `/Users/<name>`, `/home/<name>` or
    `C:\\Users\\<name>`, as a run imported from it records) → `~`; the same with
    JSON-escaped slashes (`_ESCAPED_HOME`) → `~`; a flattened home (`_FLAT_HOME`,
    `-Users-<name>-…`) → `-~`; a per-user temp root (`_TMP_ROOT`) → `tmp_label`
    (QUA-2950). The generic forms repeat until nothing changes (a replacement can expose
    another: `-Users-a-Users-b-`), so the result is deterministic and idempotent. The
    shared samples (`tests/fixtures/scrub_paths_samples.json`) pin every form."""
    runs = Path(runs_dir).expanduser()
    roots = {str(runs.resolve()): runs_label, str(runs.absolute()): runs_label}
    for home in (Path.home(), Path.home().resolve()):
        roots.setdefault(str(home), "~")
    for root in sorted(roots, key=len, reverse=True):   # /private/var/… before /var/…
        if root != "/":
            text = re.sub(re.escape(root) + f"(?!{_NAME_CHAR})",
                          lambda _m, label=roots[root]: label, text)
    generic = ((_ANY_HOME, "~"), (_ESCAPED_HOME, "~"), (_FLAT_HOME, "-~"),
               (_TMP_ROOT, tmp_label))
    before = None
    while text != before:      # each change shortens the text, so this ends
        before = text
        for pattern, label in generic:
            text = pattern.sub(lambda _m, label=label: label, text)
    return text


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
        return GateStatus(MISSING, f"no gate status at {GATE_LABEL} — run the create "
                                   "readiness gate (check_tier_ready --tier create, QUA-2859)")
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return GateStatus(UNREADABLE, f"{GATE_LABEL}: {scrub_paths(str(exc), runs_dir)}")
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

#: The detection groups power is split by, in print order (`detection.LABELS` + the
#: briefs the derivation cannot label).
DETECTION_GROUPS = (*detection.LABELS, "unlabelled")


def detection_of(rec: GradeRecord) -> str:
    """The detection group of a grade's brief (`detection.label`), `unlabelled` when the
    derivation cannot label it."""
    return detection.label(rec.case_id, rec.app_id or None) or "unlabelled"


def power_by_detection(recs: list[GradeRecord], axis: str = "power"
                       ) -> dict[str, dict[str, Any]]:
    """{group: `axis` stats (power, or `power_report`)} over the FINISHED grades of each
    detection group (only the groups present)."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        if r.grade is not None:
            out[detection_of(r)].append(r.grade)
    return {g: axis_stats(out[g], axis) for g in DETECTION_GROUPS if g in out}


def uptake_by_rule(recs: list[GradeRecord]) -> dict[str, dict[str, Any]]:
    """{rule: {group: {k, n}}} over the cells whose manifest records an uptake
    classification (QUA-2864): how many of a row's authored cases took the harmful rule,
    split by detection group (`all` too). Empty when no cell carries one."""
    out: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for r in recs:
        u = r.cell.get("uptake")
        if isinstance(u, dict) and u.get("rule") and isinstance(u.get("taken"), bool):
            out[u["rule"]]["all"].append(u["taken"])
            out[u["rule"]][detection_of(r)].append(u["taken"])
    return {rule: {g: {"k": sum(v), "n": len(v)} for g, v in by.items()}
            for rule, by in out.items()}


def _control_fired(rec: GradeRecord, run: dict[str, Any]) -> bool | None:
    """Did this control run reach its control: the run's `control_fired` (grader v4), else
    the control's id in the run's `fault_fired` (the same canary read, recorded since
    v2). None when the canary was not read or the plan names no control."""
    v = run.get("control_fired")
    if isinstance(v, bool):
        return v
    ctrl = (rec.plan.get("control") or {}).get("control")
    fired = run.get("fault_fired")
    return (ctrl in fired) if (ctrl and isinstance(fired, list)) else None


def control_reach(recs: list[GradeRecord]) -> dict[str, Any]:
    """Control reach over the rated grades' control runs (QUA-2867): `runs`, `read`,
    `fired`, `off_reference` {read, fired} (controls whose derived relation is not
    `side`), `excluded` (grader v4 `control_reached`), `rate` = fired/read, and
    `warning` (`reach_warning`) or None."""
    runs = read = fired = excl = off_read = off_fired = 0
    by_control: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in recs:
        if r.grade is None or not rated([r.grade]):
            continue
        ctrl = r.plan.get("control") or {}
        off = ctrl.get("relation") != "side"
        for run in (r.grade.get("runs") or {}).values():
            if run.get("role") != "control":
                continue
            runs += 1
            excl += grader.is_control_reached(run)
            f = _control_fired(r, run)
            if f is None:
                continue
            read += 1
            fired += f
            by_control[str(ctrl.get("control"))][0] += 1
            by_control[str(ctrl.get("control"))][1] += f
            if off:
                off_read += 1
                off_fired += f
    out = {"runs": runs, "read": read, "fired": fired,
           "rate": round(fired / read, 4) if read else None,
           "off_reference": {"read": off_read, "fired": off_fired},
           "excluded": excl,
           "by_control": {c: {"read": n, "fired": k} for c, (n, k) in sorted(by_control.items())}}
    out["warning"] = reach_warning(out)
    return out


def reach_warning(reach: dict[str, Any], threshold: float = CONTROL_REACH_WARN) -> str | None:
    """Why a control-reach summary is HIGH, or None (module docstring)."""
    why = []
    runs, excl = reach.get("runs") or 0, reach.get("excluded") or 0
    off = reach.get("off_reference") or {}
    if runs and excl and excl / runs >= threshold:
        why.append(f"{excl}/{runs} control runs excluded (control_reached)")
    if off.get("read") and off.get("fired") and off["fired"] / off["read"] >= threshold:
        why.append(f"off-reference controls fired on {off['fired']}/{off['read']} read runs")
    return "; ".join(why) or None


def fmt_reach(reach: dict[str, Any] | None) -> str:
    if not reach or not reach.get("runs"):
        return "—"
    off = reach.get("off_reference") or {}
    body = (f"fired {reach['fired']}/{reach['read']} read (off-reference "
            f"{off.get('fired', 0)}/{off.get('read', 0)}) · excluded "
            f"{reach['excluded']}/{reach['runs']}")
    if reach["read"] < reach["runs"]:
        body += f" · {reach['runs'] - reach['read']} unread"
    return body


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
    grader_versions = sorted({v for r in recs
                              if isinstance(v := r.runner.get("grader_version"), int)})
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
        "grader_versions": grader_versions,
        "axes": {axis: axis_stats(grades, axis) for axis, _ in COLUMNS},
        # Power split by how each brief's target is detected (QUA-2862): walk / assert.
        "power_by_detection": power_by_detection(recs),
        # The same split for report-credited power (QUA-2865; v3 grades only).
        "power_report_by_detection": power_by_detection(recs, "power_report"),
        # The manipulation check per row (QUA-2864): authored cases that took the rule.
        "uptake": uptake_by_rule(recs),
        # Where the authored routes walked into the control (QUA-2867).
        "control_reach": control_reach(recs),
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
                          "axes": {axis: axis_stats(grades, axis) for axis, _ in COLUMNS},
                          "control_reach": control_reach(recs)})
        app = next((r.app_id for recs in by_case[case_id].values() for r in recs if r.app_id), "")
        out.append({"case_id": case_id, "app_id": app,
                    "detection": detection.label(case_id, app or None) or "unlabelled",
                    # Over every row of the brief: the reach is the brief's controls
                    # against authored routes, whoever authored them.
                    "control_reach": control_reach([r for recs in by_case[case_id].values()
                                                    for r in recs]),
                    "rows": cells})
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
    mixed_grader = [f"{r['arm']} · {r['author']} · {r['runner']} "
                    f"(v{', v'.join(map(str, r['grader_versions']))})" for r in rows
                    if len(r["grader_versions"]) > 1]
    notes = [POWER_NOTE, REPORT_NOTE, LINT_NOTE]
    briefs = _brief_detail(records)
    high = [f"{b['case_id']} ({b['control_reach']['warning']})" for b in briefs
            if (b.get("control_reach") or {}).get("warning")]
    if high:
        notes.append("HIGH control reach — controls derived on the reference route are reached "
                     "by the authored routes (QUA-2867; read these briefs' specificity with "
                     "care): " + ", ".join(high))
    if mixed:
        notes.append(f"rows blending grades from more than one corpus version: {', '.join(mixed)}")
    if mixed_grader:
        notes.append("rows blending grades from more than one grader version (from v3 an "
                     "observed app death is a FAIL, QUA-2865): " + ", ".join(mixed_grader))
    return {"schema": BOARD_SCHEMA, "title": title,
            "gate": gate.as_dict() if gate else None,
            "corpus": corpus.stamp(), "rows": rows, "briefs": briefs,
            "notes": notes}


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
        for g, cell in (row.get("power_by_detection") or {}).items():
            out.append(f"  {'power ' + g:<13} {fmt_axis(cell)}")
        for g, cell in (row.get("power_report_by_detection") or {}).items():
            if cell.get("n") or cell.get("unscored"):
                out.append(f"  {'p.report ' + g:<13} {fmt_axis(cell)}")
        for rule, by in (row.get("uptake") or {}).items():
            out.append(f"  {'uptake':<13} {rule}: " + " · ".join(
                f"{g} {c['k']}/{c['n']}" for g, c in by.items()))
        if (row.get("control_reach") or {}).get("runs"):
            out.append(f"  {'control reach':<13} {fmt_reach(row['control_reach'])}")
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
                   "power | pass^3 · power (report))")
        for b in board["briefs"]:
            out.append(f"  {b['case_id']} [{b['app_id']} · {b.get('detection', 'unlabelled')}]")
            reach = b.get("control_reach") or {}
            if reach.get("runs"):
                out.append(f"    control reach {fmt_reach(reach)}"
                           + (f"  !! HIGH: {reach['warning']}" if reach.get("warning") else ""))
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
                                    "power", "power_given_pass3", "power_report"))
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


# ── charts (QUA-2922): K1 Strong-Test, K2 power by detection group, K3 per brief ──

#: K3's colour steps: power in fifths on the `--seq` ramp from `--seq3` (`.hm0`) to
#: `--seq7` (`.hm4`); a cell with nothing scored has no fill.
HEAT_STEPS = 5
#: K3's cell glyphs: what a reader must know before quoting a cell's power.
HEAT_GLYPHS = (("P", "pending (ungraded artifacts)"), ("N", "no case created"),
               ("X", "not gradable"), ("C", "copy of the reference case (in no rate)"),
               ("H", "HIGH control reach on this brief"))


def _flat(cells: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """`{name: {rate, ci, k, n}}` as the `{name}_rate/_ci/_k/_n` fields `viz` reads."""
    out: dict[str, Any] = {}
    for name, c in cells.items():
        c = c or {}
        out.update({f"{name}_rate": c.get("rate"), f"{name}_ci": c.get("ci"),
                    f"{name}_k": c.get("k") or 0, f"{name}_n": c.get("n") or 0})
    return out


def _who(row: dict[str, Any], runners: bool) -> str:
    who = "reference (human)" if row["arm"] == REFERENCE_ARM else f"{row['arm']} · {row['author']}"
    return f"{who} · {row['runner']}" if runners else who


def _many_runners(rows: list[dict[str, Any]]) -> bool:
    return len({r["runner"] for r in rows}) > 1


#: strong_exec in plain words (QUA-2938): K1's second panel.
STRONG_EXEC_PLAIN = ("The same bar as Strong-Test without the free static checks: "
                     "repeatable, catches its bug, ignores an unrelated one.")
#: The page's names where the CLI board's are raw (QUA-2945): the experiment index's
#: "static checks" wording, the raw name in the tooltip. `render_text` keeps the raw ones.
STRONG_EXEC_LABEL = "Strong-Test without static checks"
_HTML_HEADS = {"lint": "static checks"}
_HTML_TIPS = {"lint": f"{glossary.PLAIN['lint']} (raw name: lint-clean)"}
LINT_FAILURES_HEAD = ('<th title="The static-check rules a written test broke, by rule, '
                      'with counts (raw name: lint HARD failures)">static check failures</th>')


def _html_th(axis: str, header: str) -> str:
    tip = _HTML_TIPS.get(axis)
    open_ = f'<th title="{E(tip)}">' if tip else "<th>"
    return f"{open_}{E(_HTML_HEADS.get(axis, header))}</th>"
#: The K1-K3 captions' hover text (QUA-2938).
_K_TIPS = {"k1": " ".join((glossary.PLAIN["strong-test"], glossary.PLAIN["range"])),
           "k2": glossary.PLAIN["power"], "k3": glossary.PLAIN["power"]}


def chart_k1(board: dict[str, Any]) -> str:
    """K1: Strong-Test and strong_exec with Wilson intervals, one row per board row. A
    pending row's Strong-Test is not drawn (its headline is pending, never a rate over
    the finished subset); its strong_exec is drawn faded."""
    many = _many_runners(board["rows"])
    rows = []
    for r in board["rows"]:
        pending = r.get("headline") == "pending"
        cells = {"strong": None if pending else r["axes"]["strong"],
                 "strong_exec": r["axes"]["strong_exec"]}
        rows.append({"label": _who(r, many) + (f" ({r['pending']} pending)" if pending else ""),
                     "pending": pending, "na_label": "pending" if pending else None,
                     **_flat(cells)})
    return viz.dots_ci(rows, [("strong", "Strong-Test (headline)"),
                              ("strong_exec", STRONG_EXEC_LABEL)],
                       tips={"strong": glossary.PLAIN["strong-test"],
                             "strong_exec": f"{STRONG_EXEC_PLAIN} (raw name: strong_exec)"},
                       axis_tip=glossary.PLAIN["rate axis"])


def chart_k2(board: dict[str, Any]) -> str:
    """K2: power per detection group, one panel per group, one row per board row."""
    many = _many_runners(board["rows"])
    groups = DETECTION_GROUPS[:2]
    rows = [{"label": _who(r, many),
             **_flat({g: (r.get("power_by_detection") or {}).get(g) for g in groups})}
            for r in board["rows"]]
    return viz.dots_ci(rows, [(g, f"power · {g} briefs") for g in groups],
                       tips={g: glossary.PLAIN["power"] for g in groups},
                       axis_tip=glossary.PLAIN["rate axis"])


def _heat_class(cell: dict[str, Any] | None) -> str:
    if not cell or not cell.get("n") or cell.get("rate") is None:
        return "hm"
    return f"hm hm{min(HEAT_STEPS - 1, int(float(cell['rate']) * HEAT_STEPS))}"


def chart_k3(board: dict[str, Any]) -> str:
    """K3: a table, one row per brief and one column per board row; each cell is that
    brief's power k/n on the `--seq` ramp, with `HEAT_GLYPHS` and a `<title>` holding
    every axis's k/n. A brief a board row never authored is an empty cell."""
    rows = board["rows"]
    if not rows or not board.get("briefs"):
        return ""
    many = _many_runners(rows)
    keys = [(r["arm"], r["author"], r["runner"]) for r in rows]
    head = "".join(f"<th>{E(_who(r, many))}</th>" for r in rows)
    body = []
    for b in board["briefs"]:
        by = {(c["arm"], c["author"], c["runner"]): c for c in b["rows"]}
        high = bool((b.get("control_reach") or {}).get("warning"))
        tds = []
        for key, row in zip(keys, rows):
            c = by.get(key)
            if c is None:
                tds.append('<td class="hm"></td>')
                continue
            power = c["axes"].get("power")
            glyphs = "".join(g for g, on in (
                ("P", c.get("pending")), ("N", c.get("no_case_created")),
                ("X", c.get("not_gradable")), ("C", c.get("contamination_risk")),
                ("H", high)) if on)
            tip = (f"{b['case_id']} · {_who(row, many)}: "
                   + " · ".join(f"{_HTML_HEADS.get(a, h)} {_kn(c['axes'][a])}"
                                for a, h in COLUMNS)
                   + "".join(f" · {what}" for g, what in HEAT_GLYPHS if g in glyphs))
            tds.append(f'<td class="{_heat_class(power)}" title="{E(tip)}">{E(_kn(power))}'
                       + (f' <span class="gl">{E(glyphs)}</span>' if glyphs else "")
                       + "</td>")
        body.append(f"<tr><th>{E(b['case_id'])}"
                    f'<br><span class="dim">{E(b.get("detection", "unlabelled"))}</span></th>'
                    + "".join(tds) + "</tr>")
    key = " · ".join(f"{g} {what}" for g, what in HEAT_GLYPHS)
    steps = "".join(f'<span class="hm hm{i}">{i * 100 // HEAT_STEPS}–'
                    f'{(i + 1) * 100 // HEAT_STEPS}%</span>' for i in range(HEAT_STEPS))
    return (f'<div class="tablewrap"><table class="idx heat" id="k3"><thead><tr><th>brief</th>'
            f'{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
            f'<p class="dim heatkey">power k/n per brief: {steps}<br>{E(key)}. Hover a cell '
            f'for every axis.</p>')


def charts_html(board: dict[str, Any]) -> str:
    """K1-K3 with their captions; the board tables below are their table twins."""
    if not board.get("rows"):
        return ""
    return (
        '<h2>Charts</h2>'
        f'<figure class="fig" id="k1">{chart_k1(board)}<figcaption class="dim" '
        f'title="{html.escape(_K_TIPS["k1"])}">K1. '
        f'Strong-Test, the headline, beside {STRONG_EXEC_LABEL}, with Wilson 95% intervals. '
        'A pending '
        'row has no Strong-Test yet. Numbers: the board table below.</figcaption></figure>'
        f'<figure class="fig" id="k2">{chart_k2(board)}<figcaption class="dim" '
        f'title="{html.escape(_K_TIPS["k2"])}">K2. Power by '
        'detection group. Assert-brief power means the case checks the right state; '
        'walk-brief power mostly means the case reached the feature, so it is never pooled '
        'into a headline. Numbers: the power (assert) and power (walk) columns below.'
        '</figcaption></figure>'
        f'<figure class="fig">{chart_k3(board)}<figcaption class="dim" '
        f'title="{html.escape(_K_TIPS["k3"])}">K3. Power per '
        'brief and row.</figcaption></figure>')

def render_html(board: dict[str, Any], *, css_href: str = "style.css") -> str:
    """The board as one standalone page (the view writes it beside its index as
    `create.html`, so a portable view carries it to the bench viewer)."""
    gate = board.get("gate") or {}
    banner = ""
    if gate.get("state") and gate["state"] != READY:
        banner = (f'<p class="banner">Create readiness gate {E(gate["state"])}: '
                  f'{E(str(gate.get("detail") or ""))}. These numbers are NOT quotable.</p>')
    head = "".join(_html_th(a, h) for a, h in COLUMNS)
    det_head = "".join(f"<th>power ({E(g)})</th>" for g in DETECTION_GROUPS[:2])
    body = []
    for row in board["rows"]:
        label = ("BASELINE — reference cases" if row["baseline"]
                 else f"{row['arm']} · {row['author']}")
        cells = [E(fmt_headline(row))] + [E(fmt_axis(row["axes"][a])) for a, _ in COLUMNS[1:]]
        cells += [E(fmt_axis((row.get("power_by_detection") or {}).get(g)))
                  for g in DETECTION_GROUPS[:2]]
        cells.append(E("; ".join(f"{rule}: " + ", ".join(f"{g} {c['k']}/{c['n']}"
                                                         for g, c in by.items())
                                 for rule, by in (row.get("uptake") or {}).items()) or "—"))
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
                          f"<td>{E(b.get('detection', 'unlabelled'))}</td>"
                          f"<td>{E(who)}</td><td>{E(c['runner'])}</td>"
                          + "".join(f"<td>{E(_kn(c['axes'][a]))}</td>" for a, _ in COLUMNS)
                          + f"<td>{E(fmt_reach(c.get('control_reach')))}"
                          + (f" — HIGH: {E(b['control_reach']['warning'])}"
                             if (b.get("control_reach") or {}).get("warning") else "")
                          + f"</td><td>{E(status)}</td></tr>")
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
{charts_html(board)}
<h2>Board</h2>
<div class="tablewrap"><table class="idx"><thead><tr><th>arm · author</th><th>runner</th>
<th>artifacts</th><th>pending</th>{head}{det_head}<th>uptake</th><th>unattributed fail</th><th>excluded runs</th>
<th>no case</th><th>not gradable</th><th>copy of reference (excluded)</th><th>cost</th>
{LINT_FAILURES_HEAD}</tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
<h2>Per brief</h2>
<div class="tablewrap"><table class="idx"><thead><tr><th>brief</th><th>app</th><th>detection</th><th>arm · author</th>
<th>runner</th>{head}<th>control reach</th><th>status</th></tr></thead><tbody>{''.join(briefs)}</tbody></table></div>
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
