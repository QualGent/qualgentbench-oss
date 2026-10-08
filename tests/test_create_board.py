"""CreateBench v2: the create board (`show --mode create`, QUA-2858).

Real grade manifests (the real grader on synthetic runner outputs, via the A/B test's
simulated stages), read back through the board: rows per arm × author × runner, the
reference baseline row, Wilson CIs, honest not_gradable / no_case / pending, the
readiness-gate refusal, and the view page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from test_create_ab import BRIEFS, STUDY, SimAuthor, SimRunner, _case, _drive, _spec

from qualgentbench import cli, journey, view
from qualgentbench.create import board, grader
from qualgentbench.result import RunResult

MEDTIMER = "medtimer-check-stock"        # served without controls: not gradable
RUN = "20261001-120000-man1"


@pytest.fixture(autouse=True)
def _one_underived_case(monkeypatch):
    """QUA-2854 derived controls for all 41 cases; these tests need a case WITHOUT them
    (the not-gradable path), so that one row is served with the two keys stripped."""
    real = journey.load_truth

    def load_truth(app_id):
        truth = real(app_id)
        case = MEDTIMER
        if case in truth:
            truth = {**truth, case: {k: v for k, v in truth[case].items()
                                     if k not in (journey.CONTROLS_KEY,
                                                  journey.CONTROL_DERIVATION_KEY)}}
        return truth
    monkeypatch.setattr(journey, "load_truth", load_truth)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    _drive(runs, _spec(), SimAuthor(runs, {"A": "honest", "B": "vacuous"}), SimRunner(runs))
    return runs


def _manual(runs: Path, name: str, plan: grader.GradePlan, *, result="grade", source="authored",
            creation_episode=None) -> Path:
    path = grader.manifest_path(runs, RUN, name)
    g = grader.grade(plan, SimRunner(runs).metrics(plan)) if result == "grade" else result
    grader.write_manifest(path, plan, runner=grader.runner_fingerprint("codex-cli", "gpt-6-astra"),
                          run_id=RUN, result=g,
                          extra={"cell": board.cell_block(kind=board.MANUAL, case_id=plan.case_id,
                                                          trial=1, source=source,
                                                          creation_episode=creation_episode),
                                 "cost": {"episodes": 5, "priced_episodes": 5, "cost_usd": 6.0}})
    return path


def _ready(runs: Path) -> None:
    board.write_gate_status(runs, ready=True, checks=[{"name": "briefs neutral", "ok": True}])


def test_rows_per_arm_with_wilson_intervals_and_a_reference_baseline_row(runs):
    for case in BRIEFS:
        plan = grader.plan_grade(grader.reference_case("ankidroid", case), case)
        _manual(runs, f"ref-{case}", plan, source="reference")
    b = board.board_for(runs)
    rows = {(r["arm"], r["runner"]): r for r in b["rows"]}
    a, bb = rows[("A@aaaaaaa", "codex-cli/gpt-6-astra")], rows[("B@bbbbbbb", "codex-cli/gpt-6-astra")]
    ref = rows[(board.REFERENCE_ARM, "codex-cli/gpt-6-astra")]
    assert b["rows"][-1] is ref and ref["baseline"] and ref["author"] == board.REFERENCE_AUTHOR
    assert a["axes"]["power"]["k"] == 6 and a["axes"]["power"]["n"] == 6
    assert bb["axes"]["power"]["k"] == 0 and bb["axes"]["power"]["n"] == 6
    lo, hi = bb["axes"]["power"]["ci"]
    assert lo == 0 and 0.3 < hi < 0.5                  # Wilson, not (0, 0)
    assert a["axes"]["repeatability"]["k"] == 6 and a["headline"]["n"] == 6
    # The reference row reads strong_exec: a journey case fails HARD lint by construction.
    assert ref["axes"]["lint"]["k"] == 0 and ref["axes"]["strong_exec"]["k"] == 2
    assert ref["lint_hard_failures"]
    assert a["cost_usd"] == pytest.approx(6 * (1.0 + 5.0))
    text = "\n".join(board.render_text(b))
    assert "BASELINE codex-cli/gpt-6-astra (reference cases)" in text
    assert "6/6 100% [61–100]" in text and "0/6 0% [0–39]" in text
    assert board.LINT_NOTE in text
    # Per-brief detail: every brief, every row.
    detail = {d["case_id"]: d for d in b["briefs"]}
    assert set(detail) == set(BRIEFS)
    assert {c["arm"] for c in detail[STUDY]["rows"]} == {"A@aaaaaaa", "B@bbbbbbb", "reference"}


def test_not_gradable_no_case_and_pending_are_shown_never_folded_into_rates(runs):
    # A medtimer artifact graded by hand: controls not derived -> not_gradable.
    mt = grader.plan_grade(grader.runner_case(_case(MEDTIMER, vacuous=False)), MEDTIMER)
    assert mt.status == grader.NOT_GRADABLE
    _manual(runs, "mt", mt, result=grader.grade(mt, {}))
    # An author that saved nothing.
    nc = grader.plan_grade(None, STUDY, why_no_case="no authored case")
    _manual(runs, "nc", nc, result=grader.grade(nc, {}))
    # A grade still running (its manifest has no result yet).
    live = grader.plan_grade(grader.runner_case(_case(STUDY, vacuous=False)), STUDY)
    _manual(runs, "live", live, result=None)
    b = board.board_for(runs)
    manual = next(r for r in b["rows"] if r["arm"] == board.UNLABELLED)
    assert manual["not_gradable"] == {"controls_not_derived": 1}
    assert manual["no_case_created"] == 1 and manual["pending"] == 1
    assert manual["headline"] == "pending"
    # The not-gradable artifact is in no denominator; the no-case one is a 0 in every rate.
    assert manual["axes"]["power"]["n"] == 1 and manual["axes"]["power"]["k"] == 0
    text = "\n".join(board.render_text(b))
    assert "pending (1 ungraded)" in text and "not gradable controls_not_derived 1" in text
    # The A/B rows are complete: their headline is a rate.
    assert all(r["headline"] != "pending" for r in b["rows"] if r["arm"] != board.UNLABELLED)


def _creation(runs: Path, name: str, *, excluded: bool = False) -> Path:
    ep = runs / STUDY / name
    ep.mkdir(parents=True)
    (ep / "authored_case.json").write_text(json.dumps(_case(STUDY, vacuous=False)))
    r = {"task_id": f"{STUDY}~create", "task_version": "create", "task_type": "create_case",
         "agent": "codex-cli", "model": "gpt-6-astra", "condition": "mcp", "trial": 1,
         "passed": True, "score": 1.0, "started_at": "2026-10-01T00:00:00+00:00",
         "ended_at": "2026-10-01T00:05:00+00:00", "wall_time_sec": 300.0, "exit_code": 0,
         "artifact_dir": str(ep.relative_to(runs)), "run_id": "20261001-000000-cr01",
         "metrics": {"valid_case": True, "outcome": "case_created",
                     **({"env_failure": True} if excluded else {})},
         "provenance": {"create": {"arm": {"name": "C", "qualgent_mcp": {"sha": "c" * 40}}}}}
    RunResult.model_validate(r)
    (ep / "result.json").write_text(json.dumps(r))
    return ep


def test_an_ungraded_creation_episode_is_pending_until_a_grade_points_at_it(runs):
    ep = _creation(runs, "ep-1")
    _creation(runs, "ep-excluded", excluded=True)           # measured nothing: not pending
    b = board.board_for(runs)
    row = next(r for r in b["rows"] if r["arm"] == "C@ccccccc")
    assert row["pending"] == 1 and row["headline"] == "pending" and row["artifacts"] == 1
    plan = grader.plan_grade(grader.runner_case(_case(STUDY, vacuous=False)), STUDY)
    path = _manual(runs, "graded-ep-1", plan, creation_episode=str(ep.relative_to(runs)))
    doc = json.loads(path.read_text())
    doc["cell"].update(arm="C", arm_manifest={"qualgent_mcp": {"sha": "c" * 40}},
                       author={"agent": "codex-cli", "model": "gpt-6-astra"})
    path.write_text(json.dumps(doc))
    row = next(r for r in board.board_for(runs)["rows"] if r["arm"] == "C@ccccccc")
    assert row["pending"] == 0 and row["headline"]["n"] == 1
    assert row["axes"]["strong_exec"]["k"] == 1


def test_gate_states(runs, monkeypatch):
    assert board.read_gate(runs).state == board.MISSING
    board.write_gate_status(runs, ready=False, checks=[{"name": "adversary gate", "ok": False},
                                                       {"name": "briefs neutral", "ok": True}])
    g = board.read_gate(runs)
    assert g.state == board.NOT_READY and g.failing == ["adversary gate"] and not g.ready
    _ready(runs)
    assert board.read_gate(runs).ready
    # A gate evaluated on another corpus is not READY for this one.
    from qualgentbench import corpus
    monkeypatch.setattr(corpus, "corpus_version", lambda: "000000000000")
    assert board.read_gate(runs).state == board.STALE


def test_show_mode_create_refuses_until_the_gate_is_ready(runs):
    cr = CliRunner()
    res = cr.invoke(cli.main, ["show", "--mode", "create", "--runs-dir", str(runs)])
    assert res.exit_code == 1 and "Create board refused" in res.output and "MISSING" in res.output
    assert "Strong-Test" not in res.output
    res = cr.invoke(cli.main, ["show", "--mode", "create", "--runs-dir", str(runs), "--ungated"])
    assert res.exit_code == 0 and "UNGATED" in res.output and "NOT QUOTABLE" in res.output
    assert "Strong-Test" in res.output
    _ready(runs)
    res = cr.invoke(cli.main, ["show", "--mode", "create", "--runs-dir", str(runs)])
    assert res.exit_code == 0 and "UNGATED" not in res.output
    assert "A@aaaaaaa · author codex-cli/gpt-6-astra · runner codex-cli/gpt-6-astra" in res.output
    res = cr.invoke(cli.main, ["show", "--mode", "create", "--runs-dir", str(runs), "--json",
                               "--experiment", "pc"])
    doc = json.loads(res.output)
    assert doc["schema"] == board.BOARD_SCHEMA and len(doc["rows"]) == 2
    res = cr.invoke(cli.main, ["show", "--mode", "create", "--runs-dir", str(runs),
                               "--experiment", "nope"])
    assert res.exit_code == 1 and "No CreateBench grades" in res.output


def test_view_writes_the_create_board_beside_its_index(runs):
    run_id = json.loads(next((runs / "_runs" / "_create" / "ab").glob("*.json")).read_text())["run_id"]
    ep = runs / STUDY / "ep-grade"
    ep.mkdir(parents=True)
    r = {"task_id": f"{STUDY}-gx~clean", "task_version": "v", "task_type": "create_grade",
         "agent": "codex-cli", "model": "gpt-6-astra", "condition": "mcp", "trial": 1,
         "passed": True, "score": 1.0, "started_at": "2026-10-01T00:00:00+00:00",
         "ended_at": "2026-10-01T00:01:00+00:00", "wall_time_sec": 60.0, "exit_code": 0,
         "artifact_dir": str(ep.relative_to(runs)), "run_id": run_id, "metrics": {},
         "provenance": {}}
    (ep / "result.json").write_text(json.dumps(r))
    res = view.build_view(runs, [run_id], rescore=False)
    assert res.create_board == res.out_dir / "create.html"
    page = res.create_board.read_text()
    assert "CreateBench board" in page and "A@aaaaaaa" in page and "6/6" in page
    assert "NOT quotable" in page                     # no gate: banner, not refusal
    assert 'href="create.html"' in res.index.read_text()


def test_a_copy_of_the_reference_is_shown_as_excluded_never_dropped(runs):
    # The copyist author: the PUBLIC reference case verbatim, handed in as authored.
    ref = grader.reference_case("ankidroid", STUDY)
    art = {"test_case_id": "tc-copy",
           "case": {"name": ref.name, "steps": [{"description": s} for s in ref.steps],
                    "expected_result": ref.expected_outcome}}
    plan = grader.plan_grade(grader.runner_case(art), STUDY)
    path = _manual(runs, "copy", plan)
    g = json.loads(path.read_text())["grade"]
    assert g["contamination_risk"] == grader.CONTAMINATION_RISK
    b = board.board_for(runs)
    row = next(r for r in b["rows"] if r["arm"] == board.UNLABELLED)
    assert row["artifacts"] == 1 and row["contamination_risk"] == 1 and row["graded"] == 0
    assert row["axes"]["power"]["n"] == 0                  # in no rate
    text = "\n".join(board.render_text(b))
    assert "copy of reference (excluded from rates) 1" in text
    assert "1 copy of reference, excluded" in text          # per-brief status too


def test_the_headline_is_strong_test_never_power(runs):
    # QUA-2859's `impossible` author: fails every run, so the canary fires on the target
    # run — power 100%, repeatability 0, Strong-Test 0.
    plan = grader.plan_grade(grader.runner_case(_case(STUDY, vacuous=False)), STUDY)
    metrics = {r.key: ({"reported_verdict": "fail", "fault_fired": list(plan.targets)}
                       if r.role == "target" else {"reported_verdict": "fail"})
               for r in plan.runs}
    _manual(runs, "impossible", plan, result=grader.grade(plan, metrics))
    row = next(r for r in board.board_for(runs)["rows"] if r["arm"] == board.UNLABELLED)
    assert row["axes"]["power"]["k"] == 1 and row["axes"]["repeatability"]["k"] == 0
    assert row["headline"] is row["axes"]["strong"] and row["headline"]["k"] == 0
    assert row["axes"]["power_given_pass3"]["n"] == 0
    assert row["axes"]["power_given_pass3"]["unscored"] == 1
    text = "\n".join(board.render_text(board.board_for(runs)))
    assert board.POWER_NOTE in text.replace("\n", " ") or "power alone is not a quality" in text


def test_power_is_split_by_detection_group_on_every_row(runs):
    """QUA-2862: pooled power blends two mechanisms, so every row also carries power over
    the `assert` briefs (BROWSE: the app stays alive) and the `walk` briefs (STUDY: the
    target crashes the app on the route), and the per-brief detail names each label."""
    b = board.board_for(runs)
    rows = {r["arm"]: r for r in b["rows"]}
    a, vac = rows["A@aaaaaaa"]["power_by_detection"], rows["B@bbbbbbb"]["power_by_detection"]
    assert set(a) == {"assert", "walk"}
    assert (a["assert"]["k"], a["assert"]["n"], a["walk"]["k"], a["walk"]["n"]) == (3, 3, 3, 3)
    assert (vac["assert"]["k"], vac["walk"]["k"]) == (0, 0)
    assert {x["case_id"]: x["detection"] for x in b["briefs"]} == {
        STUDY: "walk", BRIEFS[1]: "assert"}
    text = "\n".join(board.render_text(b))
    assert "power assert" in text and "power walk" in text
    assert f"{STUDY} [ankidroid · walk]" in text
    page = board.render_html(b)
    assert "<th>power (assert)</th><th>power (walk)</th>" in page and "<th>detection</th>" in page


def test_power_report_column_v2_grades_left_out_and_mixed_versions_named(runs):
    """QUA-2865: report-credited power is its own column (and detection split), never the
    headline; a v2 grade has no such axis and is left out of it, not unscored; a row that
    blends v2 and v3 grades says so."""
    plan = grader.plan_grade(grader.runner_case(_case(STUDY, vacuous=True)), STUDY)
    seen = {r.key: ({"reported_verdict": "pass", "fault_fired": list(plan.targets),
                     "bugs_found": list(plan.targets)} if r.role == "target"
                    else {"reported_verdict": "pass", "fault_fired": []}) for r in plan.runs}
    _manual(runs, "seen-v3", plan, result=grader.grade(plan, seen))
    path = _manual(runs, "seen-v2", plan, result=grader.grade(plan, seen, version=2))
    doc = json.loads(path.read_text())
    doc["runner"]["grader_version"] = 2
    path.write_text(json.dumps(doc))
    b = board.board_for(runs)
    row = next(r for r in b["rows"] if r["arm"] == board.UNLABELLED)
    assert (row["axes"]["power"]["k"], row["axes"]["power"]["n"]) == (0, 2)
    pr = row["axes"]["power_report"]
    assert (pr["k"], pr["n"], pr["unscored"]) == (1, 1, 0)
    assert row["power_report_by_detection"]["walk"]["k"] == 1
    assert row["grader_versions"] == [2, grader.GRADER_VERSION]
    assert row["headline"] is row["axes"]["strong"]
    text = "\n".join(board.render_text(b))
    assert "power (report)" in text and "p.report walk" in text
    assert "more than one grader version" in text and board.REPORT_NOTE[:40] in text
    assert "<th>power (report)</th>" in board.render_html(b)
    # The A/B rows (all current, nothing reported on a PASS) blend nothing.
    ab_rows = [r for r in b["rows"] if r["arm"] != board.UNLABELLED]
    assert all(r["grader_versions"] == [grader.GRADER_VERSION] for r in ab_rows)


# ── control reach per brief (QUA-2867) ─────────────────────────────────────────

def _reach_rec(case, trial, control, relation, *, fired, died=False, v4=True, arm="A"):
    run = {"role": "control", "fault_fired": [control] if fired else [],
           "app_crashes": 1 if died else 0, "outcome": "passed", "ok": True}
    if v4:
        run["control_fired"] = fired
        if fired and died:
            run.update(outcome=grader.EXCLUDED, ok=None,
                       excluded=f"{grader.CONTROL_REACHED} — test")
    doc = {"plan": {"case_id": case, "app_id": "tasksorg",
                    "control": {"control": control, "relation": relation}},
           "grade": {"status": grader.GRADED, "case_id": case, "app_id": "tasksorg",
                     "axes": {"specificity": None if (fired and died and v4) else True},
                     "runs": {"control-1": run}},
           "cell": board.cell_block(kind=board.CANONICAL, case_id=case, trial=trial, arm=arm),
           "runner": {"agent": "codex-cli", "model": "gpt-6-astra", "grader_version": 4}}
    return board.GradeRecord(Path(f"/x/{case}.{trial}.json"), RUN, doc)


def test_control_reach_per_brief_with_a_warning_when_high():
    recs = [  # the QUA-2861 rerun's tasks-complete-parent pattern, one arm
        _reach_rec("tasks-complete-parent", 1, "due-section-shifted", "side", fired=True),
        _reach_rec("tasks-complete-parent", 2, "subtask-filed-before-written", "same-screen",
                   fired=True, died=True),
        _reach_rec("tasks-complete-parent", 3, "repeat-complete-crash", "other", fired=False),
        _reach_rec("tasks-complete-parent", 4, "due-section-shifted", "side", fired=True),
        # a brief whose only firing is its side control: not high
        _reach_rec("tasks-add-subtask", 1, "due-section-shifted", "side", fired=True),
        _reach_rec("tasks-add-subtask", 2, "subtasks-left-open", "other", fired=False),
    ]
    b = board.build_board(recs)
    by = {x["case_id"]: x["control_reach"] for x in b["briefs"]}
    tcp = by["tasks-complete-parent"]
    assert (tcp["runs"], tcp["read"], tcp["fired"], tcp["excluded"]) == (4, 4, 3, 1)
    assert tcp["off_reference"] == {"read": 2, "fired": 1} and tcp["rate"] == 0.75
    assert "1/4 control runs excluded" in tcp["warning"]
    assert "off-reference controls fired on 1/2" in tcp["warning"]
    assert by["tasks-add-subtask"]["warning"] is None
    assert b["rows"][0]["control_reach"]["runs"] == 6
    assert any("HIGH control reach" in n and "tasks-complete-parent" in n for n in b["notes"])
    text = "\n".join(board.render_text(b))
    assert "control reach fired 3/4 read (off-reference 1/2) · excluded 1/4  !! HIGH" in text
    assert "control reach" in board.render_html(b)


def test_control_reach_reads_an_older_grade_from_its_fault_fired():
    rec = _reach_rec("tasks-complete-parent", 2, "subtask-filed-before-written",
                     "same-screen", fired=True, died=True, v4=False)
    reach = board.control_reach([rec])
    # read and fired from the canary; NOT excluded — a v2/v3 grade scored that run
    assert (reach["read"], reach["fired"], reach["excluded"]) == (1, 1, 0)
    assert reach["warning"] == "off-reference controls fired on 1/1 read runs"
    unread = _reach_rec("x", 1, "c", "other", fired=False, v4=False)
    unread.doc["grade"]["runs"]["control-1"]["fault_fired"] = None
    r2 = board.control_reach([unread])
    assert (r2["runs"], r2["read"]) == (1, 0) and "1 unread" in board.fmt_reach(r2)


# ── charts (QUA-2922) ──────────────────────────────────────────────────────────

def _fig(page: str, fid: str) -> str:
    start = page.index(f'<figure class="fig" id="{fid}">')
    return page[start:page.index("</figure>", start)]


def test_k1_and_k2_draw_one_row_per_board_row_and_k3_one_cell_per_brief_and_row(runs):
    for case in BRIEFS:
        plan = grader.plan_grade(grader.reference_case("ankidroid", case), case)
        _manual(runs, f"ref-{case}", plan, source="reference")
    b = board.board_for(runs)
    page = board.render_html(b)
    k1, k2 = _fig(page, "k1"), _fig(page, "k2")
    assert k1.count('<g class="row">') == len(b["rows"]) == 3
    assert k2.count('<g class="row">') == len(b["rows"])
    assert "Strong-Test (headline)" in k1 and "power · assert briefs" in k2
    assert "power · walk briefs" in k2 and "never pooled into a headline" in k2
    # Every rate mark carries its k/n and interval in a <title>.
    a = next(r for r in b["rows"] if r["arm"] == "A@aaaaaaa")
    assert f"A@aaaaaaa · codex-cli/gpt-6-astra: {a['axes']['strong']['k']}/" in k1
    k3 = page[page.index('<table class="idx heat" id="k3">'):]
    k3 = k3[:k3.index("</table>")]
    cells = k3.count('<td class="hm')
    assert cells == len(b["briefs"]) * len(b["rows"]) == 6
    assert k3.count("<th>") == 1 + len(b["rows"]) + len(b["briefs"])
    # A's power on every brief is k/n = 3/3 on the darkest step; B's 0/3 on the lightest.
    assert '<td class="hm hm4" data-tip="' in k3 and '<td class="hm hm0" data-tip="' in k3
    assert "Strong-Test 3/3 · strong_exec 3/3" in k3          # every axis in the tooltip
    assert "http://" not in page and "https://" not in page and "xmlns" not in page
    assert "<script src" not in page and page.count("<script>") == 1
    # The tables stay: they are the charts' table twins.
    assert "<th>power (assert)</th><th>power (walk)</th>" in page


def test_a_pending_row_draws_no_strong_test_and_k3_marks_its_cells(runs):
    nc = grader.plan_grade(None, STUDY, why_no_case="no authored case")
    _manual(runs, "nc", nc, result=grader.grade(nc, {}))
    live = grader.plan_grade(grader.runner_case(_case(STUDY, vacuous=False)), STUDY)
    _manual(runs, "live", live, result=None)
    mt = grader.plan_grade(grader.runner_case(_case(MEDTIMER, vacuous=False)), MEDTIMER)
    _manual(runs, "mt", mt, result=grader.grade(mt, {}))
    b = board.board_for(runs)
    manual = next(r for r in b["rows"] if r["arm"] == board.UNLABELLED)
    assert manual["headline"] == "pending"
    page = board.render_html(b)
    k1 = _fig(page, "k1")
    row = next(r for r in k1.split('<g class="row">')[1:] if "(1 pending)" in r)
    # Strong-Test is "pending", never the finished subset's rate; strong_exec is faded.
    assert row.count('class="dot"') == 1 and ">pending<" in row
    assert '<g class="pt lown">' in row and "or the row is pending" in k1
    k3 = page[page.index('id="k3"'):page.index("</table>", page.index('id="k3"'))]
    assert '<span class="gl">PN</span>' in k3              # pending + no case, one brief
    assert '<span class="gl">X</span>' in k3               # medtimer: not gradable
    assert "P pending (ungraded artifacts)" in page


# ── no local path on a page (a portable create.html is published) ─────────────

def _local_paths(runs: Path) -> list[str]:
    """Every spelling of where these runs live that a published page must not carry:
    the runs dir (as given and resolved), pytest's tmp root (it names the user) and
    the home dir."""
    return [str(runs), str(runs.resolve()), str(runs.parent), str(runs.resolve().parent),
            "pytest-of-", str(Path.home()), str(Path.home().resolve())]


def _assert_no_local_path(text: str, runs: Path) -> None:
    for p in _local_paths(runs):
        assert p not in text, f"{p!r} leaked into a page"


def _make_unreadable(runs: Path, how: str) -> None:
    path = board.gate_path(runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    if how == "dir":            # an OSError, whose text names the file
        path.mkdir()
    else:
        path.write_text("{not json")


@pytest.mark.parametrize("how", ["missing", "dir", "garbled"])
def test_the_gate_message_names_no_local_path(runs, how):
    if how != "missing":
        _make_unreadable(runs, how)
    gate = board.read_gate(runs)
    assert gate.state == (board.MISSING if how == "missing" else board.UNREADABLE)
    assert board.GATE_LABEL == "<runs>/_runs/_create/gate.json"
    assert gate.detail.startswith(f"no gate status at {board.GATE_LABEL}" if how == "missing"
                                  else f"{board.GATE_LABEL}: ")
    b = board.board_for(runs)
    for text in (gate.detail, board.render_html(b), "\n".join(board.render_text(b)),
                 json.dumps(b, default=str)):
        _assert_no_local_path(text, runs)
    assert board.GATE_LABEL in board.render_html(b).replace("&lt;", "<").replace("&gt;", ">")


@pytest.mark.parametrize("how", ["missing", "dir"])
def test_the_view_create_page_names_no_local_path(runs, how):
    if how != "missing":
        _make_unreadable(runs, how)
    run_id = json.loads(next((runs / "_runs" / "_create" / "ab").glob("*.json")).read_text())["run_id"]
    ep = runs / STUDY / "ep-grade"
    ep.mkdir(parents=True)
    r = {"task_id": f"{STUDY}-gx~clean", "task_version": "v", "task_type": "create_grade",
         "agent": "codex-cli", "model": "gpt-6-astra", "condition": "mcp", "trial": 1,
         "passed": True, "score": 1.0, "started_at": "2026-10-01T00:00:00+00:00",
         "ended_at": "2026-10-01T00:01:00+00:00", "wall_time_sec": 60.0, "exit_code": 0,
         "artifact_dir": str(ep.relative_to(runs)), "run_id": run_id, "metrics": {},
         "provenance": {}}
    (ep / "result.json").write_text(json.dumps(r))
    res = view.build_view(runs, [run_id], rescore=False, portable=True)
    page = res.create_board.read_text()
    assert "NOT quotable" in page and "_runs/_create/gate.json" in page
    _assert_no_local_path(page, runs)


def test_scrub_paths_writes_the_runs_dir_and_home_relative(tmp_path, monkeypatch):
    home = tmp_path / "home" / "someone"
    monkeypatch.setenv("HOME", str(home))
    runs = tmp_path / "elsewhere" / "runs"
    text = (f"RuntimeError: exited 1 (log: {runs}/_runs/_create/ab/pc/logs/x.log); "
            f"arm cache {home}/.cache/qualgentbench/create-arms")
    out = board.scrub_paths(text, runs)
    assert out == ("RuntimeError: exited 1 (log: <runs>/_runs/_create/ab/pc/logs/x.log); "
                   "arm cache ~/.cache/qualgentbench/create-arms")
    assert board.scrub_paths("nothing local", runs) == "nothing local"


def test_scrub_paths_writes_any_home_dir_as_home_and_leaves_lookalikes(monkeypatch):
    """QUA-2946: another machine's home is `~` too, a root is replaced only where it ends a
    path segment, and a path that only looks like a home is left alone. Idempotent. The
    runs dir is not under a temp root (macOS's `tmp_path` is, QUA-2950: `<tmp>/…-v3`)."""
    monkeypatch.setenv("HOME", "/nonexistent-qgb-home/someone")
    runs = Path("/nonexistent-qgb-runs/runs")
    cases = {
        f"{runs}/a/x.json": "<runs>/a/x.json",
        f"{runs}-v3/a": f"{runs}-v3/a",                       # another runs dir, not ours
        "/Users/ann/.qualgentbench/runs/c/r.json": "~/.qualgentbench/runs/c/r.json",
        "cd /home/ci-runner/work && ls": "cd ~/work && ls",
        "C:\\Users\\Bob\\x": "~\\x",
        "C:\\\\Users\\\\Bob\\\\x": "~\\\\x",       # JSON-escaped
        '"/Users/Jos\\u00e9/x"': '"~/x"',
        "&quot;/Users/ann/x&quot;": "&quot;~/x&quot;",
        "https://example.com/Users/ann/x": "https://example.com/Users/ann/x",
        "/data/home/ann/x": "/data/home/ann/x",
        "/Users/<name>/x": "/Users/<name>/x",
    }
    for text, want in cases.items():
        out = board.scrub_paths(text, runs)
        assert out == want, text
        assert board.scrub_paths(out, runs) == out


SCRUB_SAMPLES = Path(__file__).parent / "fixtures" / "scrub_paths_samples.json"


def test_scrub_paths_matches_every_shared_sample(monkeypatch):
    """QUA-2950: the generic rules (any home, JSON-escaped home, flattened home, per-user
    temp roots) and their look-alikes, as the shared samples pin them for every port. The
    runs dir and home occur in no sample, so only the generic rules act. Idempotent."""
    monkeypatch.setenv("HOME", "/nonexistent-qgb-home/someone")
    runs = "/nonexistent-qgb-runs/runs"
    samples = json.loads(SCRUB_SAMPLES.read_text())["samples"]
    assert {s["rule"] for s in samples} == {"home", "flat_home", "tmp_root", "escaped_home",
                                             "lookalike"}
    for s in samples:
        out = board.scrub_paths(s["in"], runs)
        assert out == s["out"], s
        assert board.scrub_paths(out, runs) == out, s
        if s["rule"] == "lookalike":
            assert s["in"] == s["out"], s


def test_scrub_paths_escapes_the_tmp_label_in_markup(monkeypatch):
    monkeypatch.setenv("HOME", "/nonexistent-qgb-home/someone")
    runs = "/nonexistent-qgb-runs/runs"
    out = board.scrub_paths("<pre>/private/tmp/claude-502/-Users-alice--x/a.md</pre>", runs,
                            "&lt;runs&gt;", "&lt;tmp&gt;")
    assert out == "<pre>&lt;tmp&gt;/-~--x/a.md</pre>"
    assert view._scrub_local("/tmp/claude-7/x", Path(runs), markup=True) == "&lt;tmp&gt;/x"
    assert view._scrub_local("/tmp/claude-7/x", Path(runs)) == "<tmp>/x"
