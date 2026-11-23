"""The rq-v1 label sampler (QUA-3594, scripts/quality_label_sample.py): what the sample
file may contain, how reports are drawn, and how the key file joins a label back to
`quality.json`. Every episode here is synthetic."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest

from qualgentbench import report_quality as rq

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location(
    "quality_label_sample", ROOT / "scripts" / "quality_label_sample.py")
qls = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = qls          # dataclasses look the module up by name
_spec.loader.exec_module(qls)

# The shape of the example file the labelling page's own tests load: a report with every
# field, one in the step subset with reference steps, one with null fields, no
# `paired_by` and step items only.
EXAMPLE = {
    "format": "rq-label-sample", "version": 1, "set_id": "rq-v1", "source": "qualgentbench",
    "items": [
        {"item_id": "rq-0001", "app": "Example Notes",
         "report": {"summary": "Saving a note with an empty title closes the app",
                    "steps": "Open the app, tap New note, leave the title empty and tap Save.",
                    "expected": "The note is saved with a default title.",
                    "actual": "The app closes and the note is lost.",
                    "failure_step": "Step 4: Save", "paired_by": "only_report"}},
        {"item_id": "rq-0002", "app": "Example Notes",
         "report": {"summary": "Search shows no results", "steps": "",
                    "expected": "Notes that contain the word are listed.",
                    "actual": "No results for any query.",
                    "failure_step": "Step 2: Search", "paired_by": "text"},
         "step_items": ["Open the app", "Tap Search", "Type a word from an existing note"],
         "reference_steps": ["Create a note titled Milk", "Open Search", "Type Milk"]},
        {"item_id": "rq-0003", "app": "Example Calendar",
         "report": {"summary": "Event title is cut off in the month view",
                    "steps": "Create an event with a long title and open the month view.",
                    "expected": None, "actual": None, "failure_step": None},
         "step_items": ["Create an event with a 60 character title", "Open the month view"]},
    ],
}

APPS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
AGENT, MODEL = "some-agent-cli", "some-model-9"


def _reference(app_id: str, case_id: str, defect_id: str) -> list[str] | None:
    return [f"Open {app_id}", "Do what the case asks"] if defect_id else None


def _names(app_id: str) -> str:
    return f"App {app_id.title()}"


def _write_episode(root: Path, name: str, *, app: str, build: str, reports: list[dict],
                   heldout: bool = False, quality: bool = True, case: str = "") -> Path:
    """One saved episode: `result.json` and (unless `quality=False`) the `quality.json`
    the quality rescore would have written, with the given gate answers."""
    case = case or f"{app}-case"
    d = root / f"{case}" / f"2026-01-01_{name}_{AGENT}_{MODEL}_ep-{name}"
    d.mkdir(parents=True)
    saved = [{"step": i + 1, "matched": r.get("matched"), "known": r.get("known"),
              "consequence_of": None} for i, r in enumerate(reports)]
    (d / rq.RESULT_FILE).write_text(json.dumps({
        "task_id": f"{case}~{build}", "agent": AGENT, "model": MODEL,
        "metrics": {"app_id": app, "case_id": case, "version": build, "heldout": heldout,
                    "reports": saved}}))
    if quality:
        entries = []
        for i, r in enumerate(reports):
            neutral = r.get("known") is not None
            gates = {} if neutral else {
                g: {"pass": p, "reason": "because"} for g, p in r.get("gates", {}).items()}
            entries.append({
                "index": i, "step": i + 1, "matched": r.get("matched"),
                "known": r.get("known"), "consequence_of": None, "neutral": neutral,
                "gates": gates, "customer_ready": None,
                "customer_report": {
                    "summary": r.get("summary", f"{name} report {i} summary"),
                    "steps": r.get("steps", ""), "expected": r.get("expected"),
                    "actual": r.get("actual"), "failure_step": r.get("failure_step"),
                    "agent_steps": r.get("agent_steps", []),
                    "report_source": "report_tool" if r.get("agent_steps") else "findings",
                    "paired_by": "only_report" if r.get("agent_steps") else None}})
        (d / rq.QUALITY_FILE).write_text(rq.render({
            "quality_version": rq.QUALITY_VERSION, "scorer_version": 5,
            "scorer_report_source": "findings_file", "reports": entries, "totals": {}}))
    return d


#: Gate answers per seeded stratum of the synthetic board.
SIGNATURES = {
    "pass": {"G1": True, "G2": True, "G3": True, "G4": None},
    "fail:G1": {"G1": False, "G2": None, "G3": False, "G4": None},     # see _board
    "fail:G2": {"G1": True, "G2": False, "G3": True, "G4": None},
    "fail:G3": {"G1": True, "G2": True, "G3": False, "G4": None},
    "fail:G2+G3": {"G1": True, "G2": False, "G3": False, "G4": None},
}


def _board(root: Path, *, per_cell: int = 8, clean_per_app: int = 6) -> Path:
    """A board with every seeded stratum on every app (`per_cell` reports each) and
    `clean_per_app` clean-build reports per app. Half of the matched reports carry an
    agent step list."""
    n = 0
    for app in APPS:
        for stratum, gates in SIGNATURES.items():
            gates = dict(gates)
            if stratum == "fail:G1":
                gates["G3"] = None
            for k in range(per_cell):
                n += 1
                matched = f"{app}-secret-defect" if gates["G1"] else None
                _write_episode(root, f"s{n:04d}", app=app, build="seeded", reports=[{
                    "matched": matched, "gates": gates,
                    "expected": "It works.", "actual": f"It broke ({n}).",
                    "failure_step": "Step 2",
                    "agent_steps": ([f"Opened {app}", {"step": 2, "status": "failed"}]
                                    if matched and k % 2 == 0 else [])}])
        for _ in range(clean_per_app):
            n += 1
            _write_episode(root, f"c{n:04d}", app=app, build="clean", reports=[{
                "matched": None, "gates": {"G1": False, "G2": None, "G3": None, "G4": None},
                "actual": f"Looks wrong ({n})."}])
    return root


def _build(*roots: Path, **over):
    over.setdefault("reference", _reference)
    over.setdefault("app_names", _names)
    return qls.build([(r.name, r) for r in roots], **over)


def _keys_at_every_level(sample: dict) -> tuple[set, set, set]:
    items = sample["items"]
    return (set(sample), {k for i in items for k in i},
            {k for i in items for k in i["report"]})


# ── the sample file's shape ────────────────────────────────────────────────────

def test_the_labelling_pages_example_shape_passes_the_schema_check():
    assert qls.validate_sample(EXAMPLE) == []
    assert qls.validate_sample(json.loads(json.dumps(EXAMPLE))) == []


@pytest.mark.parametrize("change, said", [
    (lambda d: d.update(arm="mcp"), "key 'arm' is not allowed"),
    (lambda d: d["items"][0].update(model="m"), "key 'model' is not allowed"),
    (lambda d: d["items"][0].update(episode="e"), "key 'episode' is not allowed"),
    (lambda d: d["items"][0]["report"].update(gates={}), "report key 'gates' is not allowed"),
    (lambda d: d["items"][0]["report"].update(agent_steps=[]), "report key 'agent_steps'"),
    (lambda d: d["items"][0]["report"].update(report_source="x"), "report key 'report_source'"),
    (lambda d: d.update(source="production"), "source must be 'qualgentbench'"),
    (lambda d: d.update(version=2), "version must be 1"),
    (lambda d: d.update(set_id="rq-v2"), "set_id must be 'rq-v1'"),
    (lambda d: d["items"][0].update(item_id="RQ-1"), "item_id must match"),
    (lambda d: d["items"][1].update(item_id="rq-0001"), "item_id rq-0001 is repeated"),
    (lambda d: d["items"][0].update(app=" "), "app must be the app's name"),
    (lambda d: d["items"][0]["report"].update(summary=""), "report.summary must be text"),
    (lambda d: d["items"][0]["report"].update(steps=None), "report.steps must be text"),
    (lambda d: d["items"][0]["report"].update(expected=3), "report.expected must be text"),
    (lambda d: d["items"][0].update(reference_steps=["a"]), "reference_steps needs step_items"),
    (lambda d: d["items"][1].update(step_items=[]), "step_items must be 1 to 60"),
    (lambda d: d["items"][1].update(step_items=["a", " "]), "step_items must be 1 to 60"),
    (lambda d: d["items"][1].update(step_items=["s"] * 61), "step_items must be 1 to 60"),
    (lambda d: d["items"][0]["report"].update(summary="x" * 20_001), "report.summary"),
    (lambda d: d.update(items=[]), "items must be a non-empty list"),
])
def test_the_schema_check_refuses_what_the_page_refuses(change, said):
    doc = copy.deepcopy(EXAMPLE)
    change(doc)
    assert any(said in p for p in qls.validate_sample(doc)), qls.validate_sample(doc)


def test_a_synthetic_boards_sample_holds_only_whitelisted_keys(tmp_path):
    sample, _ = _build(_board(tmp_path / "board-one"))
    top, item, report = _keys_at_every_level(sample)
    assert top == set(qls.SAMPLE_KEYS)
    assert item <= set(qls.ITEM_KEYS) and {"item_id", "app", "report"} <= item
    assert report <= set(qls.REPORT_KEYS)
    assert qls.validate_sample(sample) == []
    assert (sample["format"], sample["version"], sample["set_id"], sample["source"]) == (
        "rq-label-sample", 1, "rq-v1", "qualgentbench")


def test_the_sample_names_no_agent_model_episode_defect_build_or_gate(tmp_path):
    sample, key = _build(_board(tmp_path / "board-one"))
    text = qls.render(sample)
    for hidden in (AGENT, MODEL, "secret-defect", "seeded", "board-one", "ep-", "-case",
                   '"G1"', "fail:", "because"):
        assert hidden not in text, hidden
    # Item ids are a count in file order: they say nothing about the report.
    assert [i["item_id"] for i in sample["items"]] == [
        f"rq-{n:04d}" for n in range(1, len(sample["items"]) + 1)]
    strata = [k["stratum"] for k in key["items"]]
    assert strata != sorted(strata)            # shuffled, not grouped by outcome


# ── what is drawn ──────────────────────────────────────────────────────────────

def test_the_default_draw_is_120_with_20_clean_and_40_step_items(tmp_path):
    sample, key = _build(_board(tmp_path / "b"))
    drawn = key["drawn"]
    assert (drawn["reports"], drawn["clean"], drawn["step_subset"]) == (120, 20, 40)
    assert drawn["by_stratum"] == {"clean": 20, "fail:G1": 20, "fail:G2": 20,
                                   "fail:G2+G3": 20, "fail:G3": 20, "pass": 20}
    # No app fills a stratum: 20 over 6 apps is 3 or 4 each.
    per = Counter((k["stratum"], k["app_id"]) for k in key["items"])
    assert set(per.values()) <= {3, 4} and len(per) == 36
    with_steps = [i for i in sample["items"] if "step_items" in i]
    assert len(with_steps) == 40
    assert all(i["reference_steps"] and len(i["step_items"]) == 2 for i in with_steps)
    # An unmatched report has no defect to give it reference steps.
    assert key["drawn"]["step_subset_by_stratum"] == {
        "fail:G2": 10, "fail:G2+G3": 10, "fail:G3": 10, "pass": 10}
    assert {k["item_id"] for k in key["items"] if k["step_subset"]} == {
        i["item_id"] for i in with_steps}


def test_a_step_that_is_not_text_is_shown_as_one_line(tmp_path):
    sample, _ = _build(_board(tmp_path / "b"))
    steps = next(i["step_items"] for i in sample["items"] if "step_items" in i)
    assert steps[1] == '{"status": "failed", "step": 2}'
    assert qls.render_steps(["  Tap\n Save ", "", {"b": 1, "a": "x"}, 3]) == [
        "Tap Save", '{"a": "x", "b": 1}', "3"]
    assert qls.render_steps(None) == []


def test_a_small_stratum_gives_its_share_to_the_others():
    assert qls.fill({"a": 3, "b": 50, "c": 50}, 30) == {"a": 3, "b": 14, "c": 13}
    assert qls.fill({"a": 2, "b": 2}, 30) == {"a": 2, "b": 2}
    assert qls.fill({}, 5) == {}
    assert sum(qls.fill({"a": 7, "b": 9, "c": 100}, 40).values()) == 40


def test_too_few_seeded_reports_are_made_up_from_clean_ones_and_the_reverse(tmp_path):
    few_seeded = _board(tmp_path / "few", per_cell=1, clean_per_app=30)     # 30 seeded
    _, key = _build(few_seeded)
    assert (key["drawn"]["reports"], key["drawn"]["clean"]) == (120, 90)
    few_clean = _board(tmp_path / "fewclean", per_cell=8, clean_per_app=1)  # 6 clean
    _, key = _build(few_clean)
    assert (key["drawn"]["reports"], key["drawn"]["clean"]) == (120, 6)


def test_strata_come_from_the_failed_headline_gates():
    assert qls.stratum_of("clean", {"G1": {"pass": False}}) == "clean"
    assert qls.stratum_of("seeded", {"G1": {"pass": True}}) == "pass"
    assert qls.stratum_of("seeded", {"G1": {"pass": True}, "G2": {"pass": None}}) == "pass"
    assert qls.stratum_of("seeded", {"G3": {"pass": False}, "G1": {"pass": False}}) == "fail:G1+G3"
    # Unknown is neither a pass nor a failure: it is named on its own.
    unknown = {"pass": None, "reason": "cannot tell", "unknown": True}
    assert qls.stratum_of("seeded", {"G1": {"pass": True}, "G2": unknown}) == "unknown:G2"
    assert qls.stratum_of("seeded", {"G1": {"pass": True}, "G2": unknown,
                                     "G3": {"pass": False}}) == "fail:G3,unknown:G2"
    assert qls.stratum_of("clean", {"G2": unknown}) == "clean"
    # A side gate is not a headline gate.
    assert qls.stratum_of("seeded", {"G1": {"pass": True}, "G5": {"pass": False}}) == "pass"


# ── determinism ────────────────────────────────────────────────────────────────

def test_the_same_seed_and_episodes_give_the_same_bytes_anywhere(tmp_path):
    board = _board(tmp_path / "here" / "board")
    first = [qls.render(d) for d in _build(board)]
    assert [qls.render(d) for d in _build(board)] == first
    moved = tmp_path / "elsewhere" / "deep" / "board"
    shutil.copytree(board, moved)
    assert [qls.render(d) for d in _build(moved)] == first
    assert str(tmp_path) not in "".join(first)
    other_sample, other_key = _build(board, seed=7)
    assert qls.render(other_sample) != first[0]
    assert other_key["drawn"]["by_stratum"] == json.loads(first[1])["drawn"]["by_stratum"]


def test_board_order_on_the_command_line_does_not_matter(tmp_path):
    a = _board(tmp_path / "a", per_cell=2, clean_per_app=1)
    b = tmp_path / "b"
    _write_episode(b, "x1", app="alpha", build="seeded", reports=[
        {"matched": "d", "gates": {"G1": True}, "actual": "Only on board b."}])
    assert _build(a, b) == _build(b, a)


# ── the key file ───────────────────────────────────────────────────────────────

def test_the_key_joins_every_item_to_its_report_in_quality_json(tmp_path):
    board = _board(tmp_path / "board-one")
    sample, key = _build(board)
    assert key["format"] == "rq-label-sample-key" and key["set_id"] == "rq-v1"
    assert key["sample_sha256"] == hashlib.sha256(qls.render(sample).encode()).hexdigest()
    assert key["boards"] == ["board-one"] and key["quality_versions"] == [rq.QUALITY_VERSION]
    assert [k["item_id"] for k in key["items"]] == [i["item_id"] for i in sample["items"]]
    for item, k in zip(sample["items"], key["items"], strict=True):
        episode = board / k["episode"]
        saved = json.loads((episode / rq.QUALITY_FILE).read_text())["reports"][k["report_index"]]
        assert saved["index"] == k["report_index"]
        assert saved["matched"] == k["matched"]
        for field in ("summary", "steps", "expected", "actual", "failure_step"):
            assert item["report"][field] == saved["customer_report"][field]
        metrics = json.loads((episode / rq.RESULT_FILE).read_text())["metrics"]
        assert (metrics["app_id"], metrics["version"]) == (k["app_id"], k["build"])
        assert k["stratum"] == qls.stratum_of(k["build"], saved["gates"])
        assert item["app"] == _names(k["app_id"])


def test_neutral_reports_and_unscored_episodes_are_left_out_and_counted(tmp_path):
    board = tmp_path / "b"
    _write_episode(board, "e1", app="alpha", build="seeded", reports=[
        {"matched": "d1", "gates": {"G1": True}},
        {"matched": None, "known": "a known behaviour"},
        {"matched": None, "gates": {"G1": False}, "summary": ""}])      # no summary
    _write_episode(board, "e2", app="alpha", build="seeded", quality=False,
                   reports=[{"matched": "d1"}])
    (board / "not-an-episode").mkdir()
    (board / "not-an-episode" / rq.RESULT_FILE).write_text("{}")
    sample, key = _build(board)
    assert len(sample["items"]) == 1 and key["items"][0]["report_index"] == 0
    assert key["read"] == {"episodes": 1, "episodes_not_journey": 1,
                           "episodes_without_quality": 1, "reports": 3,
                           "reports_neutral": 1, "reports_unusable": 1}


def test_reports_with_the_same_text_are_one_item_and_the_key_lists_the_rest(tmp_path):
    board = tmp_path / "b"
    same = {"matched": "d1", "gates": {"G1": True}, "summary": "Save loses the note",
            "actual": "The note is gone."}
    _write_episode(board, "e1", app="alpha", build="seeded", reports=[same])
    _write_episode(board, "e2", app="alpha", build="seeded",
                   reports=[{**same, "agent_steps": ["Open alpha", "Tap Save"]}])
    _write_episode(board, "e3", app="bravo", build="seeded", reports=[same])   # another app
    sample, key = _build(board)
    assert len(sample["items"]) == 2 and key["read"]["reports_same_text"] == 1
    alpha = next(k for k in key["items"] if k["app_id"] == "alpha")
    assert alpha["episode"].endswith("ep-e1")
    assert len(alpha["same_text"]) == 1 and alpha["same_text"][0].endswith("ep-e2#0")
    # The step list of the copy that was folded in is not lost.
    assert alpha["step_subset"] is True


def test_heldout_episodes_are_drawn_unless_excluded(tmp_path):
    board = tmp_path / "b"
    _write_episode(board, "e1", app="alpha", build="seeded",
                   reports=[{"matched": "d", "gates": {"G1": True}}])
    _write_episode(board, "e2", app="hidden", build="seeded", heldout=True,
                   reports=[{"matched": "d", "gates": {"G1": True}}])
    _, key = _build(board)
    assert key["drawn"]["heldout"] == 1 and key["drawn"]["reports"] == 2
    sample, key = _build(board, exclude_heldout=True)
    assert key["drawn"]["heldout"] == 0 and key["read"]["episodes_heldout_excluded"] == 1
    assert "Hidden" not in qls.render(sample)


def test_a_step_list_without_a_reference_report_stays_out_of_the_step_subset(tmp_path):
    board = tmp_path / "b"
    _write_episode(board, "e1", app="alpha", build="seeded", reports=[
        {"matched": "d", "gates": {"G1": True}, "agent_steps": ["Open alpha"]}])
    sample, key = _build(board, reference=lambda *a: None)
    assert "step_items" not in sample["items"][0]
    assert key["read"]["step_lists_without_reference"] == 1


# ── the real loaders ───────────────────────────────────────────────────────────

def test_reference_steps_are_the_cases_own_steps_up_to_the_reference_reports_step():
    lookup = qls.CorpusLookup()
    steps = lookup.reference_steps("fossify-calendar", "cal-open-task-from-list",
                                   "task-row-opens-event-editor")
    assert steps == ["Tap the + button and choose Task.",
                     'Enter the title "Laundry" and save.',
                     'In the event list, tap "Laundry" to open it.']
    assert lookup.app_name("fossify-calendar") == "Fossify Calendar"
    assert lookup.reference_steps("fossify-calendar", "cal-open-task-from-list", "nope") is None
    assert lookup.reference_steps("fossify-calendar", "no-such-case", "x") is None
    assert lookup.reference_steps("no-such-app", "c", "d") is None
    assert lookup.app_name("no-such-app") == "no-such-app"


def test_a_quality_json_written_by_the_rescore_is_read(tmp_path):
    d = tmp_path / "b" / "case-a" / "ep1"
    d.mkdir(parents=True)
    (d / rq.RESULT_FILE).write_text(json.dumps({"metrics": {
        "app_id": "alpha", "case_id": "case-a", "version": "seeded", "scorer_version": 5,
        "reports": [{"step": 2, "screen": "List", "observed": "Nothing listed",
                     "expected": "The note is listed", "description": "The note is lost",
                     "matched": "d1", "known": None, "consequence_of": None}]}}))
    rq.write_quality(d)
    sample, key = _build(tmp_path / "b")
    assert sample["items"][0]["report"] == {
        "summary": "The note is lost", "steps": "", "expected": "The note is listed",
        "actual": "Nothing listed", "failure_step": "Step 2: List"}
    gates = json.loads((d / rq.QUALITY_FILE).read_text())["reports"][0]["gates"]
    assert gates["G1"]["pass"] is True
    assert key["items"][0]["stratum"] == qls.stratum_of("seeded", gates)


# ── the command ────────────────────────────────────────────────────────────────

def test_the_command_writes_the_two_files_and_prints_the_hash(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(qls.CorpusLookup, "reference_steps",
                        lambda self, a, c, d: _reference(a, c, d))
    board, out = _board(tmp_path / "board"), tmp_path / "private" / "out"
    assert qls.main([str(board), "--out", str(out)]) == 0
    text = (out / qls.SAMPLE_FILE).read_text(encoding="utf-8")
    key = json.loads((out / qls.KEY_FILE).read_text(encoding="utf-8"))
    assert qls.validate_sample(json.loads(text)) == []
    assert key["sample_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    printed = capsys.readouterr().out
    assert key["sample_sha256"] in printed and "drew 120 of 276 reports" in printed
    before = text
    assert qls.main([str(board), "--out", str(out)]) == 0
    assert (out / qls.SAMPLE_FILE).read_text(encoding="utf-8") == before


def test_the_command_refuses_to_write_inside_the_repository(tmp_path, capsys):
    board = _board(tmp_path / "board", per_cell=1, clean_per_app=1)
    inside = ROOT / "tests" / "_rq_sample_must_not_exist"
    assert qls.main([str(board), "--out", str(inside)]) == 2
    assert not inside.exists()
    assert "inside this repository" in capsys.readouterr().err


def test_the_command_says_what_to_run_when_nothing_is_scored(tmp_path, capsys):
    board = tmp_path / "board"
    _write_episode(board, "e1", app="alpha", build="seeded", quality=False,
                   reports=[{"matched": "d"}])
    assert qls.main([str(board), "--out", str(tmp_path / "out")]) == 1
    assert "qualgentbench.report_quality" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_the_command_refuses_mixed_quality_versions_and_same_named_boards(tmp_path, capsys):
    board = tmp_path / "board"
    d = _write_episode(board, "e1", app="alpha", build="seeded",
                       reports=[{"matched": "d", "gates": {"G1": True}}])
    _write_episode(board, "e2", app="alpha", build="seeded",
                   reports=[{"matched": "d", "gates": {"G1": True}, "actual": "Other."}])
    q = json.loads((d / rq.QUALITY_FILE).read_text())
    q["quality_version"] = rq.QUALITY_VERSION + 1
    (d / rq.QUALITY_FILE).write_text(json.dumps(q))
    assert qls.main([str(board), "--out", str(tmp_path / "out")]) == 1
    assert "different quality versions" in capsys.readouterr().err
    twin = tmp_path / "other" / "board"
    twin.mkdir(parents=True)
    assert qls.main([str(board), str(twin), "--out", str(tmp_path / "out")]) == 2
    assert "same name" in capsys.readouterr().err


def test_reports_a_gate_could_not_decide_are_a_stratum_of_their_own(tmp_path):
    board = tmp_path / "b"
    unknown = {"G1": True, "G2": None, "G3": True}
    for n in range(6):
        _write_episode(board, f"p{n}", app="alpha", build="seeded", reports=[
            {"matched": "d", "gates": {"G1": True, "G2": True, "G3": True},
             "actual": f"Passed {n}."}])
        d = _write_episode(board, f"u{n}", app="alpha", build="seeded", reports=[
            {"matched": "d", "gates": unknown, "actual": f"Undecided {n}."}])
        q = json.loads((d / rq.QUALITY_FILE).read_text())
        q["reports"][0]["gates"]["G2"]["unknown"] = True
        (d / rq.QUALITY_FILE).write_text(rq.render(q))
    _, key = _build(board, total=6, clean=0)
    assert key["pool"]["by_stratum"] == {"pass": 6, "unknown:G2": 6}
    assert key["drawn"]["by_stratum"] == {"pass": 3, "unknown:G2": 3}
