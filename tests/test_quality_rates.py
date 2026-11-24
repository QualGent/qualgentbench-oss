"""The Report quality block (QUA-3588): `quality.json` files added up per arm, with
every denominator stated, and never a ranking key."""

from __future__ import annotations

import json
import re
from pathlib import Path

from click.testing import CliRunner
from scorer_closure import SCORING_MODULES, SRC, Closure, closure_sha256
from test_known_behaviour import FOUND
from test_report_quality import (
    FALSE_REPORT,
    _package_imports,
    _seeded_metrics,
    _tree,
)
from test_report_quality import _episode as _saved_episode
from test_scorer_version import read_pin
from test_view import (
    FINDINGS_FAIL,
    RUN_ID,
    _board_table,
    _build,
    _claude_mcp_transcript,
    _snapshot,
)
from test_view import _episode as _view_episode

from qualgentbench import cli, journey, view
from qualgentbench import quality_rates as qr
from qualgentbench import report_quality as rq

NAMES = {"G1": "valid", "G2": "located", "G3": "complete", "G4": "reproducible"}
UNKNOWN = "unknown"
G123 = ("G1", "G2", "G3")
ALL = ("G1", "G2", "G3", "G4")


def _gate(answer) -> dict:
    if answer == UNKNOWN:
        return {"pass": None, "reason": "cannot decide", "unknown": True}
    return {"pass": answer, "reason": "not run" if answer is None else "because"}


def _rep(*answers, matched=None, source="findings", neutral=False, carried=False,
         gates=G123) -> dict:
    """One report of a quality.json. `answers` are the gates' answers in order: True,
    False, UNKNOWN or None (did not run)."""
    answers = answers + (None,) * (len(gates) - len(answers))
    return {"neutral": neutral, "matched": matched,
            "gates": {} if neutral else {g: _gate(a) for g, a in zip(gates, answers)},
            "complete": None if neutral else {"complete_if_steps_carried": carried},
            "customer_report": {"report_source": source}}


def _quality(*reports, gates=G123, version=1) -> dict:
    return {"quality_version": version, "reports": list(reports),
            "totals": {"gates": {g: {"name": NAMES[g]} for g in gates}}}


def _rec(quality, *, agent="agent-a", model="model-a", condition="mcp", present=(),
         **metrics) -> dict:
    return qr.episode_record({"agent": agent, "model": model, "condition": condition,
                              "metrics": {"bugs_present": list(present), **metrics}},
                             quality)


def _one(*records) -> dict:
    block = qr.block(records)
    assert len(block["rows"]) == 1
    return block["rows"][0]


# ── the three cases the ticket names ───────────────────────────────────────────

def test_no_reports_gives_no_rate_and_never_zero_over_zero():
    row = _one(_rec(_quality(gates=ALL), present=["bug-a"]), _rec(_quality(gates=ALL)))
    assert (row["episodes"], row["reports"], row["scored"], row["neutral"]) == (2, 0, 0, 0)
    assert row["gates_missing"] == [] and row["gates_available"] == list(ALL)
    cr = row["customer_ready"]
    assert cr["measured"] is True
    assert (cr["k"], cr["n"], cr["rate"], cr["ci"], cr["unknown_share"]) == (0, 0, None,
                                                                             None, None)
    for gate in ALL:
        g = row["gates"][gate]
        assert (g["evaluated"], g["passed"], g["rate"], g["ci"]) == (0, 0, None, None)
    # The yield still has its denominator: a defect was present and nothing reported it.
    y = row["customer_ready_yield"]
    assert (y["k"], y["n"], y["rate"]) == (0, 1, 0.0) and y["ci"][0] == 0.0
    assert row["reports_per_defect"]["value"] is None
    assert [s["passed"] for s in row["funnel"]] == [0, 0, 0, 0]
    text = "\n".join(qr.lines(qr.block([_rec(_quality(gates=ALL))])))
    assert "customer-ready rate   0/0 —" in text and "0/0 0%" not in text


def test_only_neutral_reports_are_in_no_number():
    row = _one(_rec(_quality(_rep(neutral=True, gates=ALL), _rep(neutral=True, gates=ALL),
                             gates=ALL), present=["bug-a"]))
    assert (row["reports"], row["neutral"], row["scored"]) == (2, 2, 0)
    assert row["customer_ready"]["n"] == 0 and row["customer_ready"]["rate"] is None
    assert all(g["evaluated"] == g["unknown"] == g["not_run"] == 0
               for g in row["gates"].values())
    assert row["by_report_source"] == {}
    assert row["funnel"][0]["entered"] == 0


def test_a_missing_gate_leaves_customer_ready_not_measured_never_zero():
    """G4 is not in the files yet: a report that passed G1 to G3 is neither ready nor
    failed, so no customer-ready rate is given, even beside reports that failed."""
    row = _one(_rec(_quality(_rep(True, True, True, matched="bug-a"),
                             _rep(False, None, False)), present=["bug-a"]))
    assert row["gates_available"] == ["G1", "G2", "G3"] and row["gates_missing"] == ["G4"]
    cr = row["customer_ready"]
    assert cr["measured"] is False and cr["rate"] is None and cr["ci"] is None
    assert (cr["ready"], cr["not_ready"], cr["waiting"], cr["unknown"]) == (0, 1, 1, 0)
    y = row["customer_ready_yield"]
    assert y["measured"] is False and y["rate"] is None and y["n"] == 1
    assert row["gates"]["G4"]["available"] is False and row["gates"]["G4"]["rate"] is None
    last = row["funnel"][-1]
    assert (last["gate"], last["measured"], last["entered"], last["passed"]) == (
        "G4", False, 1, None)
    # What CAN be said, with the gate set named.
    roa = row["ready_on_available"]
    assert roa["gates"] == ["G1", "G2", "G3"] and (roa["k"], roa["n"]) == (1, 2)
    assert (roa["yield"]["k"], roa["yield"]["n"]) == (1, 1)
    text = "\n".join(qr.lines(qr.block([_rec(_quality(_rep(True, True, True)))])))
    assert "customer-ready rate   not measured (G4 reproducible has not run)" in text
    assert "not measured: G4 reproducible" in text


def test_every_report_failing_a_gate_is_still_not_measured_while_g4_is_absent():
    """All reports fail G3 today. Without G4 that is not a customer-ready rate of 0."""
    row = _one(_rec(_quality(_rep(True, True, False), _rep(False, None, False))))
    cr = row["customer_ready"]
    assert cr["measured"] is False and cr["rate"] is None
    assert (cr["not_ready"], cr["waiting"]) == (2, 0)
    assert row["ready_on_available"]["rate"] == 0.0


def test_the_block_needs_no_change_when_g4_lands():
    q = _quality(_rep(True, True, True, True, matched="bug-a", gates=ALL),
                 _rep(True, True, True, False, matched="bug-b", gates=ALL),
                 _rep(False, None, False, None, gates=ALL), gates=ALL)
    row = _one(_rec(q, present=["bug-a", "bug-b", "bug-c"]))
    assert row["gates_missing"] == []
    cr = row["customer_ready"]
    assert cr["measured"] is True and (cr["k"], cr["n"]) == (1, 3)
    assert cr["rate"] == round(1 / 3, 4) and cr["ci"][0] < cr["rate"] < cr["ci"][1]
    y = row["customer_ready_yield"]
    assert y["measured"] is True and (y["k"], y["n"]) == (1, 3)
    assert row["ready_on_available"]["k"] == cr["k"]
    assert [(s["name"], s["entered"], s["passed"]) for s in row["funnel"]] == [
        ("valid", 3, 2), ("located", 2, 2), ("complete", 2, 2), ("reproducible", 2, 1)]


def test_a_registered_gate_that_ran_for_nothing():
    """G4 is registered and answered nothing. With a report waiting on it the rate is
    not measured; when every report already failed a gate (a guesser), the rate is 0."""
    waiting = _one(_rec(_quality(_rep(True, True, True, None, gates=ALL), gates=ALL)))
    assert waiting["gates_missing"] == ["G4"]
    assert waiting["customer_ready"]["measured"] is False
    assert waiting["customer_ready"]["waiting"] == 1

    guesser = _one(_rec(_quality(_rep(False, None, True, None, gates=ALL),
                                 _rep(False, None, False, None, gates=ALL), gates=ALL)))
    assert guesser["gates_missing"] == []
    cr = guesser["customer_ready"]
    assert cr["measured"] is True and (cr["k"], cr["n"], cr["rate"]) == (0, 2, 0.0)
    assert cr["not_ready"] == guesser["scored"] == 2


# ── unknown is never a pass and never a fail ───────────────────────────────────

def test_unknown_is_stated_and_is_in_neither_side():
    q = _quality(_rep(True, UNKNOWN, True, True, gates=ALL),
                 _rep(True, True, True, True, gates=ALL),
                 _rep(True, False, True, None, gates=ALL),
                 _rep(True, UNKNOWN, False, None, gates=ALL), gates=ALL)
    row = _one(_rec(q))
    g2 = row["gates"]["G2"]
    assert (g2["passed"], g2["evaluated"], g2["unknown"], g2["rate"]) == (1, 2, 2, 0.5)
    cr = row["customer_ready"]
    # One ready, two failed a gate (one of them also unknown at G2: a fail decides it),
    # one undecided. The undecided report is in neither k nor n.
    assert (cr["ready"], cr["not_ready"], cr["unknown"], cr["waiting"]) == (1, 2, 1, 0)
    assert cr["measured"] is True and (cr["k"], cr["n"]) == (1, 3)
    assert cr["unknown_share"] == 0.25
    located = row["funnel"][1]
    assert (located["entered"], located["passed"], located["failed"],
            located["unknown"]) == (4, 1, 1, 2)
    text = "\n".join(qr.lines(qr.block([_rec(q)])))
    assert "2 unknown (50% of 4 answered)" in text
    assert "1 unknown (25% of scored)" in text


def test_the_state_agrees_with_report_quality_customer_ready(tmp_path):
    """One rule: the block's ready / not-ready is `quality.json`'s own `customer_ready`."""
    ep = _saved_episode(tmp_path, "ep1", _seeded_metrics(FOUND + FALSE_REPORT))
    for report in rq.score_episode(ep)["reports"]:
        state = qr._state(report, rq.HEADLINE_GATES)[0]
        assert {qr.READY: True, qr.NOT_READY: False}.get(state) == report["customer_ready"]


# ── yield, reports per defect, sources, the funnel ─────────────────────────────

def test_yield_counts_defects_present_from_result_json_per_episode():
    ready = _rep(True, True, True, True, matched="bug-a", gates=ALL)
    failed = _rep(True, False, True, None, matched="bug-b", gates=ALL)
    row = _one(_rec(_quality(ready, ready, failed, gates=ALL), present=["bug-a", "bug-b"]),
               _rec(_quality(failed, gates=ALL), present=["bug-a", "bug-b"]),
               _rec(_quality(gates=ALL), present=[]))
    # bug-a has a ready report in the first episode only: 1 of 4 present defects.
    y = row["customer_ready_yield"]
    assert (y["k"], y["n"], y["rate"]) == (1, 4, 0.25)
    rpd = row["reports_per_defect"]
    # Four matched reports of three (episode, defect) pairs: one repeat.
    assert (rpd["matched_reports"], rpd["defects_found"], rpd["value"]) == (4, 3, 1.33)
    assert (rpd["repeats"]["k"], rpd["repeats"]["n"]) == (1, 4)


def test_rates_are_readable_per_report_source():
    q = _quality(_rep(True, True, False, source="findings"),
                 _rep(True, True, False, source="findings"),
                 _rep(True, True, True, source="report_tool", carried=True),
                 _rep(False, None, False, source="report_tool"))
    row = _one(_rec(q))
    by = row["by_report_source"]
    assert list(by) == ["findings", "report_tool"]
    assert (by["findings"]["scored"], by["findings"]["gates"]["G3"]["passed"]) == (2, 0)
    tool = by["report_tool"]
    assert (tool["gates"]["G3"]["passed"], tool["gates"]["G3"]["evaluated"]) == (1, 2)
    assert [s["passed"] for s in tool["funnel"]][:3] == [1, 1, 1]
    assert [s["passed"] for s in by["findings"]["funnel"]][:3] == [2, 2, 0]
    assert sum(part["scored"] for part in by.values()) == row["scored"]
    assert (row["complete_if_steps_carried"]["k"], row["complete_if_steps_carried"]["n"]) \
        == (1, 4)


def test_the_funnel_shows_where_reports_drop():
    q = _quality(*[_rep(True, True, False)] * 3, *[_rep(True, False, False)] * 2,
                 _rep(True, UNKNOWN, True), *[_rep(False, None, True)] * 4)
    row = _one(_rec(q))
    assert [(s["name"], s["entered"], s["passed"], s["failed"], s["unknown"])
            for s in row["funnel"][:3]] == [
        ("valid", 10, 6, 4, 0), ("located", 6, 3, 2, 1), ("complete", 3, 0, 3, 0)]
    assert row["funnel"][3]["measured"] is False
    assert ("funnel   10 reports → 6 valid (4 failed) → 3 located (2 failed, 1 unknown) → "
            "0 complete (3 failed) → reproducible: not measured") in "\n".join(
                qr.lines(qr.block([_rec(q)])))


def test_precision_against_coverage_for_the_gate_count():
    q = _quality(_rep(True, True, True, True, gates=ALL),
                 _rep(True, True, False, None, gates=ALL),
                 _rep(True, False, True, None, gates=ALL),
                 _rep(False, None, False, None, gates=ALL), gates=ALL)
    pc = _one(_rec(q))["precision_coverage"]
    assert pc["signal_gates"] == ["G2", "G3"]
    assert [(p["min_gates"], p["kept"], p["coverage"]) for p in pc["points"]] == [
        (2, 1, 0.25), (1, 3, 0.75), (0, 4, 1.0)]
    assert [(p["customer_ready"]["k"], p["customer_ready"]["n"]) for p in pc["points"]] == [
        (1, 1), (1, 3), (1, 4)]
    assert [p["valid"]["k"] for p in pc["points"]] == [1, 3, 3]
    # While G4 has not run, precision is not measured; coverage still is.
    early = _one(_rec(_quality(_rep(True, True, True))))["precision_coverage"]
    assert all(p["customer_ready"] is None for p in early["points"])
    assert early["points"][0]["kept"] == 1


# ── who is counted, and the order of rows ──────────────────────────────────────

def test_excluded_and_unscored_episodes_are_left_out_and_counted():
    row = _one(_rec(_quality(_rep(True, True, False)), present=["bug-a"]),
               _rec(_quality(_rep(True, True, True)), present=["bug-a"], contaminated=True),
               _rec(None, present=["bug-a"]))
    assert (row["episodes"], row["episodes_excluded"],
            row["episodes_without_quality"]) == (1, 1, 1)
    assert row["scored"] == 1 and row["customer_ready_yield"]["n"] == 1


def test_rows_are_in_name_order_and_never_by_a_quality_number():
    good = _quality(_rep(True, True, True, True, gates=ALL), gates=ALL)
    bad = _quality(_rep(False, None, False, None, gates=ALL), gates=ALL)
    records = [_rec(good, agent="zeta"), _rec(bad, agent="alpha"),
               _rec(good, agent="alpha", heldout=True),
               _rec(bad, agent="alpha", model="vendor/route/model-a", condition="raw")]
    block = qr.block(records)
    order = [(r["heldout"], r["agent"], r["condition"]) for r in block["rows"]]
    assert order == [(False, "alpha", "mcp"), (False, "alpha", "raw"),
                     (False, "zeta", "mcp"), (True, "alpha", "mcp")]
    assert block["rows"][1]["model"] == "model-a"      # the board's own model name
    assert qr.block(reversed(records)) == block
    # No ranking code reads the block or the module.
    for module in (*SCORING_MODULES, "rescore", "rates", "result", "leaderboard"):
        imports = _package_imports(SRC / f"{module}.py")
        assert "quality_rates" not in imports and "report_quality" not in imports, module


def test_two_quality_versions_in_one_row_are_flagged():
    row = _one(_rec(_quality(version=1)), _rec(_quality(version=2)))
    assert row["quality_versions"] == ["1", "2"]
    assert "MIXED: not comparable" in "\n".join(qr.lines(
        qr.block([_rec(_quality(version=1)), _rec(_quality(version=2))])))


def test_the_scorer_pin_does_not_move_and_the_block_is_outside_the_closure():
    pins = read_pin()
    assert journey.SCORER_VERSION == 5 and closure_sha256() == pins[5]
    assert not [item for item in Closure().items() if item[0] == "quality_rates"]
    assert closure_sha256(sources={"quality_rates": ""}) == pins[5]
    assert rq.QUALITY_VERSION == 1      # adding up changes no quality.json


# ── saved episodes: the command, and `show` ────────────────────────────────────

def _two_episodes(root: Path) -> None:
    _saved_episode(root, "ep1", _seeded_metrics(FOUND + FALSE_REPORT),
                   agent="agent-a", model="model-a", condition="mcp", run_id="run-1")
    _saved_episode(root, "ep2", _seeded_metrics(FOUND), agent="agent-a", model="model-a",
                   condition="mcp", run_id="run-2")


def test_the_command_prints_the_block_and_writes_nothing(tmp_path, capsys):
    _two_episodes(tmp_path)
    before = _tree(tmp_path)
    assert qr.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert _tree(tmp_path) == before
    assert out.startswith("Report quality — ") and "agent-a · model-a · mcp" in out
    assert "2 episodes · 3 reports: 3 scored, 0 neutral" in out
    assert "G1 valid         2/3 67%" in out and "G3 complete      0/3 0%" in out
    assert "customer-ready rate   not measured (G4 reproducible has not run)" in out

    assert qr.main([str(tmp_path), "--run", "run-2", "--json"]) == 0
    block = json.loads(capsys.readouterr().out)
    assert block["format"] == qr.BLOCK_FORMAT and block["rows"][0]["reports"] == 1
    # --saved reads quality.json only: none is saved yet, so no episode has a block.
    assert qr.main([str(tmp_path), "--saved", "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)["rows"][0]
    assert (saved["episodes"], saved["episodes_without_quality"]) == (0, 2)
    assert qr.main([str(tmp_path / "nowhere")]) == 2


def _show(runs: Path) -> str:
    res = CliRunner().invoke(cli.main, ["show", "--runs-dir", str(runs), "--run", RUN_ID,
                                        "--agent", "claude-code", "--mode", "journey"],
                             terminal_width=200)
    assert res.exit_code == 0, res.output
    return res.output


REPORTS = [{"step": 2, "screen": "List", "observed": "Milk", "expected": "Milk is saved",
            "description": "the item is not saved", "matched": "item-not-saved",
            "known": None, "consequence_of": None, "grounded": True},
           {"step": 3, "screen": "List", "observed": "Total", "expected": "a total",
            "description": "the total looks wrong", "matched": None, "known": None,
            "consequence_of": None, "grounded": True}]


def _runs_with_reports(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    _view_episode(runs, "list-shows-items", "seeded", "ep-seeded", agent="claude-code",
                  condition="mcp", transcript=_claude_mcp_transcript(),
                  findings=FINDINGS_FAIL,
                  metrics={"completed": False, "false_reports": 1,
                           "bugs_found": ["item-not-saved"],
                           "bugs_present": ["item-not-saved"], "reported_status": "FAIL",
                           "reports": REPORTS, "report_source": "findings_file",
                           "scorer_version": 5})
    _view_episode(runs, "list-shows-items", "clean", "ep-clean", agent="claude-code",
                  condition="mcp", transcript=_claude_mcp_transcript(),
                  findings=FINDINGS_FAIL,
                  metrics={"completed": True, "false_reports": 0, "bugs_found": [],
                           "bugs_present": [], "reports": [],
                           "report_source": "findings_file", "scorer_version": 5})
    return runs


def test_show_prints_the_block_under_a_board_that_is_byte_identical(tmp_path):
    runs = _runs_with_reports(tmp_path)
    before = _show(runs)
    assert "Report quality" not in before        # no quality.json saved: nothing added
    assert rq.main([str(runs)]) == 0
    after = _show(runs)
    assert after.startswith(before)              # everything above is the same bytes
    added = after[len(before):]
    assert "Report quality — " in added and "claude-code · test-model · mcp" in added
    assert "2 episodes · 2 reports: 2 scored, 0 neutral" in added
    assert "G1 valid         1/2 50%" in added


# ── the local view ─────────────────────────────────────────────────────────────

def _strip_quality(index: str) -> str:
    return re.sub(r'\n<section id="report-quality">.*?</section>', "", index, flags=re.DOTALL)


def test_the_view_adds_the_section_and_leaves_the_board_byte_identical(tmp_path,
                                                                       monkeypatch):
    runs = _runs_with_reports(tmp_path)
    saved = _snapshot(runs)
    res = _build(runs, rescore=False)
    index = res.index.read_text()
    assert _snapshot(runs).items() >= saved.items()     # nothing in the run was touched
    assert not list(runs.glob("*/*/quality.json"))      # scored in memory only
    assert '<section id="report-quality">\n<h3>Report quality</h3>' in index
    assert "Where reports are lost" in index and "not measured (G4 reproducible" in index
    entry = json.loads((res.out_dir / view.MANIFEST).read_text())["runs"][0]
    row = entry["report_quality"]["rows"][0]
    assert (row["agent"], row["episodes"], row["scored"]) == ("claude-code", 2, 2)
    assert row["gates"]["G1"]["passed"] == 1 and row["customer_ready_yield"]["n"] == 1

    # The same build with no quality block: the board table and every other byte of the
    # page are what they were, and the manifest entry simply has no such key.
    monkeypatch.setattr(view, "_quality_of", lambda *a, **k: None)
    plain = _build(runs, rescore=False)
    plain_index = plain.index.read_text()
    assert "report-quality" not in plain_index
    assert _board_table(index) == _board_table(plain_index)
    assert _strip_quality(index) == plain_index
    plain_entry = json.loads((plain.out_dir / view.MANIFEST).read_text())["runs"][0]
    assert "report_quality" not in plain_entry
    assert {k: v for k, v in entry.items() if k != "report_quality"} == plain_entry


def test_the_episode_page_shows_each_gate_with_its_reason(tmp_path):
    runs = _runs_with_reports(tmp_path)
    res = _build(runs, rescore=False)
    summaries = {json.loads(p.read_text())["result"]["metrics"]["version"]: p
                 for p in (res.out_dir / "ep").glob("*.json")}
    seeded = json.loads(summaries["seeded"].read_text())
    assert seeded["quality"]["quality_version"] == rq.QUALITY_VERSION
    assert [r["gates"]["G1"]["pass"] for r in seeded["quality"]["reports"]] == [True, False]
    page = summaries["seeded"].with_suffix(".html").read_text()
    assert 'id="report-quality">Report quality (quality v1)</h2>' in page
    assert "pass: matched seeded defect item-not-saved" in page
    assert "fail: matched no seeded defect" in page and "G3 complete" in page
    clean = summaries["clean"].with_suffix(".html").read_text()
    assert "Report quality (quality v1)" in clean and "(no report)" in clean


def test_index_from_rebuilds_the_section_byte_for_byte(tmp_path):
    import shutil
    runs = _runs_with_reports(tmp_path)
    res = _build(runs, rescore=False)
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (res.out_dir / "ep").glob("*.json"):
        shutil.copy(p, copy / "ep" / p.name)
    shutil.copy(res.out_dir / "run.json", copy / "run.json")
    view.build_index(copy)
    assert (copy / "index.html").read_text() == res.index.read_text()
    strip = lambda p: re.sub(r'"generated_at": "[^"]*"', "", p.read_text())
    assert strip(copy / view.MANIFEST) == strip(res.out_dir / view.MANIFEST)


# ── the documentation ──────────────────────────────────────────────────────────

def _keys(value, path=""):
    """Every dict key path in a JSON value, list items folded together."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}/{k}"
            yield from _keys(v, f"{path}/{k}")
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v, f"{path}[]")


def test_the_documented_quality_json_has_the_keys_the_code_writes(tmp_path):
    doc = (SRC.parent.parent / "docs" / "report-quality.md").read_text()
    example = json.loads(re.search(r'```json\n(\{\n  "quality_version".*?\n\})\n```', doc,
                                   re.DOTALL).group(1))
    assert rq.render(example).strip() == re.search(
        r'```json\n(\{\n  "quality_version".*?\n\})\n```', doc, re.DOTALL).group(1)
    real = rq.score_episode(_saved_episode(tmp_path, "ep1", _seeded_metrics(FOUND)))
    skip = "/reports[]/gates/G2/unknown"      # only on an unknown answer
    assert set(_keys(example)) - {skip} == set(_keys(real)) - {skip}
    assert list(example["totals"]["gates"]) == [g.id for g in rq.GATES]
    assert example["quality_version"] == rq.QUALITY_VERSION
    # Every key of the block that the documentation's table names is in a real row.
    row = _one(_rec(_quality(_rep(True, True, False, matched="bug-a")), present=["bug-a"]))
    for key in re.findall(r"^\| `rows\[\]\.([a-z_]+)", doc, re.MULTILINE):
        assert key in row, key
    documented = set(re.findall(r"`rows\[\]\.([a-z_]+)[`.<\[]", doc)) | {
        k for cell in re.findall(r"^\| (`rows\[\]\..*?) \|", doc, re.MULTILINE)
        for k in re.findall(r"`([a-z_]+)`", cell)}
    assert set(row) <= documented, set(row) - documented


def test_a_portable_view_carries_no_report_quality_yet(tmp_path):
    """The block is the local view's. What gets published is the portable view."""
    runs = _runs_with_reports(tmp_path)
    res = _build(runs, rescore=False, portable=True)
    assert "report-quality" not in res.index.read_text()
    assert all("quality" not in json.loads(p.read_text())
               for p in (res.out_dir / "ep").glob("*.json"))
    assert all("report-quality" not in p.read_text()
               for p in (res.out_dir / "ep").glob("*.html"))
    entry = json.loads((res.out_dir / view.MANIFEST).read_text())["runs"][0]
    assert "report_quality" not in entry
