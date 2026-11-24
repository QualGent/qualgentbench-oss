"""Report quality G5, the step-accuracy judge (QUA-3593, `quality_judge.py`): what the
judge is shown, how its answer is checked and saved, that a second run calls nothing,
and that the side column never touches a gate, `customer_ready` or the scorer. No test
here calls a model: answers come from a scripted client or from the recorded files in
`tests/fixtures/quality_judge/`. Every episode is synthetic."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest
from scorer_closure import SCORING_MODULES, SRC, Closure, closure_sha256
from test_mcp_scoring_parity import Call, _claude
from test_report_quality import _package_imports
from test_scorer_version import read_pin

from qualgentbench import journey
from qualgentbench import quality_judge as qj
from qualgentbench import report_quality as rq

ROOT = Path(__file__).parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "quality_judge"
#: A made-up Bedrock-shaped id: the recorded files were written under this name.
MODEL = "us.example.step-judge-1"

APP, CASE, DEFECT = "example-notes", "notes-save-empty-title", "empty-title-crash"
REFERENCE = ["Launch Example Notes.", "Tap New note.", "Leave the title empty.",
             "Tap Save."]
GOOD_STEPS = ["Open the Example Notes app", "Tap the New note button",
              "Do not type a title", "Tap Save"]
POOR_STEPS = ["Open the Example Notes app", "Tap Settings", "Tap Save on the note",
              {"action": "look", "target": "the list"}, "Set it up"]
GOOD_ANSWER = {"steps": [{"step": i, "label": "correct", "matches": [i]}
                         for i in range(1, 5)]}
POOR_ANSWER = {"steps": [
    {"step": 1, "label": "correct", "matches": [1]},
    {"step": 2, "label": "wrong", "matches": [2]},
    {"step": 3, "label": "correct", "matches": [4]},
    {"step": 4, "label": "extra", "matches": []},
    {"step": 5, "label": "ambiguous", "matches": [3]},
]}


def _reference(app_id: str, case_id: str, defect_id: str) -> list[str] | None:
    return list(REFERENCE) if (app_id, case_id, defect_id) == (APP, CASE, DEFECT) else None


@pytest.fixture(autouse=True)
def _synthetic_corpus(monkeypatch):
    """The synthetic case's reference steps; any other case has none."""
    monkeypatch.setattr(qj, "reference_steps", _reference)


def _report(matched: str | None = DEFECT, **over) -> dict:
    return {"step": 4, "screen": "Save", "observed": "The app closed",
            "expected": "The note is saved", "description": "Saving closes the app",
            "matched": matched, "grounded": True, "known": None, "consequence_of": None,
            **over}


def _call(steps, **over) -> dict:
    return {"status": "FAIL", "summary": "Saving a note with an empty title closes the app",
            "failure_step": "Step 4: Save", "expected": "The note is saved",
            "actual": "The app closed", "steps": steps, **over}


def _episode(root: Path, name: str, steps=None, *, reports=None, call=None) -> Path:
    """A saved episode whose one accepted report call carries `steps`."""
    d = root / "case-a" / name
    (d / "agent").mkdir(parents=True)
    metrics = {"app_id": APP, "case_id": CASE, "version": "seeded",
               "report_source": "findings_file", "scorer_version": journey.SCORER_VERSION,
               "reports": reports if reports is not None else [_report()]}
    (d / "result.json").write_text(json.dumps(
        {"task_id": f"{CASE}~seeded", "task_type": journey.TASK_TYPE,
         "agent": "some-agent", "model": "some-arm-model", "metrics": metrics},
        indent=2, sort_keys=True) + "\n")
    args = call if call is not None else (_call(steps) if steps is not None else None)
    (d / rq.TRANSCRIPT_FILE).write_text(
        _claude([Call(rq.REPORT_TOOL, args, '{"ok": true}')]) if args else "")
    return d


class Scripted:
    """A client that answers from a list and records what it was sent."""

    def __init__(self, *answers, model: str = MODEL):
        self.model, self.answers, self.sent = model, list(answers), []

    def complete(self, system: str, user: str) -> qj.JudgeReply:
        self.sent.append((system, user))
        answer = self.answers.pop(0)
        return qj.JudgeReply(answer if isinstance(answer, str) else json.dumps(answer),
                             {"input_tokens": 700, "output_tokens": 90})


class NoCalls:
    model = MODEL

    def complete(self, system: str, user: str) -> qj.JudgeReply:
        raise AssertionError("the judge was called")


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ── what the judge is shown ────────────────────────────────────────────────────

def test_inputs_are_the_agents_step_list_and_the_case_steps(tmp_path):
    ep = rq.load_episode(_episode(tmp_path, "e1", POOR_STEPS))
    inputs = qj.judge_inputs(ep, 0)
    assert inputs.step_source == qj.SOURCE_AGENT_STEPS
    assert inputs.reference_steps == tuple(REFERENCE)
    assert inputs.report_steps == (
        "Open the Example Notes app", "Tap Settings", "Tap Save on the note",
        '{"action": "look", "target": "the list"}', "Set it up")


def test_the_bugs_own_steps_are_used_only_when_they_are_a_step_list(tmp_path):
    listed = _call([], code_investigation="1. Open the app\n2. Tap New note\n3. Tap Save")
    inputs = qj.judge_inputs(rq.load_episode(_episode(tmp_path, "e1", call=listed)), 0)
    assert inputs.step_source == qj.SOURCE_BUG_STEPS
    assert inputs.report_steps == ("Open the app", "Tap New note", "Tap Save")
    # The product copies the summary into `steps` when there is no investigation, and
    # an investigation of the source is not a step list either.
    for name, call in (("e2", _call([])),
                       ("e3", _call([], code_investigation="save() returns too early."))):
        assert qj.judge_inputs(rq.load_episode(_episode(tmp_path, name, call=call)),
                               0) == "the report has no step list"


def test_reports_the_judge_is_not_asked_about(tmp_path):
    reports = [_report(matched=None), _report(matched=None, known="known-behaviour"),
               _report(matched="another-defect")]
    ep = rq.load_episode(_episode(tmp_path, "e1", GOOD_STEPS, reports=reports))
    assert "matched no seeded defect" in qj.judge_inputs(ep, 0)
    assert qj.judge_inputs(ep, 1) == "a neutral report"
    # One call carries one bug: it went to report 0, so report 2 is a findings entry.
    assert qj.judge_inputs(ep, 2) == "the report has no step list"
    findings_only = rq.load_episode(_episode(tmp_path, "e2"))
    assert qj.judge_inputs(findings_only, 0) == "the report has no step list"
    other = rq.load_episode(_episode(tmp_path, "e3", GOOD_STEPS,
                                     reports=[_report(matched="another-defect")]))
    assert qj.judge_inputs(other, 0) == "the corpus has no reference steps for this defect"
    long = rq.load_episode(_episode(tmp_path, "e4", ["Tap Save"] * (qj.MAX_STEPS + 1)))
    assert qj.judge_inputs(long, 0) == f"more than {qj.MAX_STEPS} steps"


def test_the_prompt_holds_no_defect_id_no_truth_text_and_no_arm_name(tmp_path):
    """Acceptance: everything sent is the two step lists and the fixed instructions."""
    d = _episode(tmp_path, "e1", GOOD_STEPS)
    client = Scripted(GOOD_ANSWER)
    qj.judge_episode(d, client)
    ((system, user),) = client.sent
    assert system == qj.SYSTEM_PROMPT
    assert user == ("REFERENCE STEPS\nR1. Launch Example Notes.\nR2. Tap New note.\n"
                    "R3. Leave the title empty.\nR4. Tap Save.\n\nREPORT STEPS\n"
                    "S1. Open the Example Notes app\nS2. Tap the New note button\n"
                    "S3. Do not type a title\nS4. Tap Save\n")
    result = json.loads((d / "result.json").read_text())
    sent = (system + user).lower()
    for secret in (DEFECT, CASE, APP, result["agent"], result["model"], MODEL,
                   "The app closed", "The note is saved", "Saving a note with",
                   "seeded", "clean", "defect", "matched", str(d)):
        assert secret.lower() not in sent, secret
    # Nothing but the steps varies: the user message is a function of the inputs alone.
    assert user == qj.user_prompt(qj.JudgeInputs(tuple(GOOD_STEPS), tuple(REFERENCE)))


#: sha256 of (version, system prompt, label set) per prompt version. A change to the
#: prompt needs a new JUDGE_PROMPT_VERSION and a new line here; saved answers of the
#: old version are then no longer read.
PROMPT_PINS = {1: "a7d8601667d3d0a7d3c6a2663bea6f89956485eba47c5ed870a07a550be5e155"}


def test_the_prompt_is_pinned_to_its_version():
    assert qj.prompt_sha256() == PROMPT_PINS[qj.JUDGE_PROMPT_VERSION]
    assert qj.LABELS == ("correct", "ambiguous", "extra", "wrong")


def test_the_key_moves_with_inputs_prompt_and_model(monkeypatch):
    inputs = qj.JudgeInputs(tuple(GOOD_STEPS), tuple(REFERENCE))
    key = qj.cache_key(inputs, MODEL)
    assert key == qj.cache_key(qj.JudgeInputs(tuple(GOOD_STEPS), tuple(REFERENCE),
                                              qj.SOURCE_BUG_STEPS), MODEL)
    assert key != qj.cache_key(inputs, MODEL + "x")
    assert key != qj.cache_key(qj.JudgeInputs(tuple(GOOD_STEPS[:3]), tuple(REFERENCE)), MODEL)
    assert key != qj.cache_key(qj.JudgeInputs(tuple(GOOD_STEPS), tuple(REFERENCE[:3])), MODEL)
    monkeypatch.setattr(qj, "SYSTEM_PROMPT", qj.SYSTEM_PROMPT + " ")
    assert key != qj.cache_key(inputs, MODEL)


# ── the answer ─────────────────────────────────────────────────────────────────

def test_a_fitting_answer_is_reduced_to_labels_matches_and_missing_steps():
    text = "```json\n" + json.dumps({**POOR_ANSWER, "score": 7, "comment": "fine"}) + "\n```"
    assert qj.parse_answer(text, 5, 4) == {
        "labels": ["correct", "wrong", "correct", "extra", "ambiguous"],
        "matches": [[1], [2], [4], [], [3]],
        # R2 is matched only by a wrong step, so nothing covers it.
        "missing_steps": [2]}


@pytest.mark.parametrize("answer, why", [
    ("no json here", "no JSON object"),
    ("{not json}", "not JSON"),
    ({"steps": GOOD_ANSWER["steps"][:3]}, "expected 4 step entries"),
    ({"labels": ["correct"] * 4}, "no list"),
    ({"steps": [{**s, "step": 1} for s in GOOD_ANSWER["steps"]]}, "entry 2 is not step 2"),
    ({"steps": [{**s, "label": "good"} for s in GOOD_ANSWER["steps"]]}, "not one of"),
    ({"steps": [{**s, "label": "Correct"} for s in GOOD_ANSWER["steps"]]}, "not one of"),
    ({"steps": [{**s, "matches": [9]} for s in GOOD_ANSWER["steps"]]}, "from 1 to 4"),
    ({"steps": [{**s, "matches": [1, 1]} for s in GOOD_ANSWER["steps"]]}, "distinct"),
    ({"steps": [{**s, "matches": [True]} for s in GOOD_ANSWER["steps"]]}, "distinct"),
    ({"steps": [{**s, "matches": "1"} for s in GOOD_ANSWER["steps"]]}, "distinct"),
    ({"steps": [{**s, "matches": []} for s in GOOD_ANSWER["steps"]]}, "must match"),
    ({"steps": [{**s, "label": "extra"} for s in GOOD_ANSWER["steps"]]}, "matches no"),
])
def test_an_answer_outside_the_schema_is_refused(answer, why):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    with pytest.raises(qj.JudgeError, match=why):
        qj.parse_answer(text, 4, 4)


def test_precision_recall_and_the_threshold():
    perfect = qj.accuracy(["correct"] * 4, [], 4)
    assert perfect == {"pass": True, "precision": 1.0, "recall": 1.0, "counts": {
        "correct": 4, "ambiguous": 0, "extra": 0, "wrong": 0, "report_steps": 4,
        "reference_steps": 4, "missing": 0}}
    # Exactly 0.8 on both sides passes: 4 of 5 correct, 4 of 5 covered.
    edge = qj.accuracy(["correct"] * 4 + ["extra"], [5], 5)
    assert (edge["pass"], edge["precision"], edge["recall"]) == (True, 0.8, 0.8)
    # One side below fails, whichever it is; ambiguous is not correct.
    assert qj.accuracy(["correct"] * 3 + ["ambiguous"], [], 4)["pass"] is False
    assert qj.accuracy(["correct"] * 4, [3, 4], 5)["pass"] is False
    low = qj.accuracy(["correct", "wrong", "correct", "extra", "ambiguous"], [2], 4)
    assert (low["pass"], low["precision"], low["recall"]) == (False, 0.4, 0.75)
    assert qj.accuracy(["correct"] * 2 + ["wrong"], [], 3)["precision"] == 0.6667
    assert qj.PASS_THRESHOLD == 0.8 and qj.JUDGE_ACCEPTED is False


# ── saving, and the second run ─────────────────────────────────────────────────

def test_a_run_saves_the_answers_and_a_second_run_calls_nothing(tmp_path):
    d = _episode(tmp_path, "e1", POOR_STEPS)
    before = _tree(d)
    stats = qj.judge_episode(d, Scripted(POOR_ANSWER))
    assert (stats.reports, stats.called, stats.cached, stats.failed, stats.files_written) \
        == (1, 1, 0, 0, 1)
    assert (stats.input_tokens, stats.output_tokens) == (700, 90)
    after = _tree(d)
    assert {k: v for k, v in after.items() if k != qj.JUDGE_FILE} == before

    doc = json.loads((d / qj.JUDGE_FILE).read_text())
    inputs = qj.judge_inputs(rq.load_episode(d), 0)
    assert doc == {
        "format": "rq-step-judge", "version": 1, "model": MODEL,
        "prompt_version": qj.JUDGE_PROMPT_VERSION, "prompt_sha256": qj.prompt_sha256(),
        "results": {qj.cache_key(inputs, MODEL): {
            "labels": ["correct", "wrong", "correct", "extra", "ambiguous"],
            "matches": [[1], [2], [4], [], [3]], "missing_steps": [2],
            "step_source": "agent_steps", "report_steps": 5, "reference_steps": 4}}}
    # The file holds answers, not text: no step, no report text, no clock, no path.
    text = (d / qj.JUDGE_FILE).read_text()
    assert "Tap" not in text and "Example" not in text and str(tmp_path) not in text
    assert text == qj.render(doc)

    again = qj.judge_episode(d, NoCalls())
    assert (again.called, again.cached, again.files_written) == (0, 1, 0)
    assert _tree(d) == after


def test_an_answer_that_does_not_fit_is_asked_for_again_then_given_up(tmp_path):
    d = _episode(tmp_path, "e1", GOOD_STEPS)
    logged: list[str] = []
    client = Scripted("I think the steps are fine.", GOOD_ANSWER)
    stats = qj.judge_episode(d, client, retries=1, log=logged.append)
    assert (stats.called, stats.failed, len(client.sent)) == (2, 0, 2) and not logged

    d2 = _episode(tmp_path, "e2", GOOD_STEPS)
    stats = qj.judge_episode(d2, Scripted("nope", {"steps": []}), retries=1,
                             log=logged.append)
    assert (stats.called, stats.failed, stats.files_written) == (2, 1, 0)
    assert not (d2 / qj.JUDGE_FILE).exists()
    assert len(logged) == 1 and "expected 4 step entries" in logged[0]


def test_a_changed_report_model_or_damaged_entry_is_asked_again(tmp_path):
    d = _episode(tmp_path, "e1", GOOD_STEPS)
    qj.judge_episode(d, Scripted(GOOD_ANSWER))
    path = d / qj.JUDGE_FILE
    saved = path.read_text()

    # Another model does not replace paid answers unless told to.
    other, logged = Scripted(GOOD_ANSWER, model=MODEL + "-b"), []
    kept = qj.judge_episode(d, other, log=logged.append)
    assert (kept.called, kept.kept) == (0, 1) and path.read_text() == saved
    assert "--overwrite" in logged[0] and MODEL in logged[0]
    assert qj.judge_episode(d, other, overwrite=True).called == 1
    assert json.loads(path.read_text())["model"] == MODEL + "-b"

    path.write_text(saved)
    doc = json.loads(saved)
    (entry,) = doc["results"].values()
    entry["labels"][0] = "perfect"                          # edited by hand
    path.write_text(qj.render(doc))
    assert qj.judge_episode(d, Scripted(GOOD_ANSWER)).called == 1
    assert path.read_text() == saved

    entry["labels"][0], entry["missing_steps"] = "correct", [4]   # does not follow
    path.write_text(qj.render(doc))
    ep = rq.load_episode(d)
    block = qj.steps_accuracy(ep, 0, qj.load_results(d))
    assert block["pass"] is None and block["reason"] == "no saved judge result for these steps"


def test_the_call_budget_is_shared_and_leaves_the_rest_unasked(tmp_path):
    dirs = [_episode(tmp_path, f"e{i}", GOOD_STEPS + [f"Extra {i}"]) for i in range(3)]
    answer = {"steps": GOOD_ANSWER["steps"] + [{"step": 5, "label": "extra", "matches": []}]}
    client, budget, stats = Scripted(answer, answer, answer), [2], qj.RunStats()
    for d in dirs:
        qj.judge_episode(d, client, budget=budget, stats=stats)
    assert (stats.called, stats.deferred, stats.failed, stats.files_written) == (2, 1, 0, 2)
    assert [(d / qj.JUDGE_FILE).exists() for d in dirs] == [True, True, False]


# ── the side column in quality.json ────────────────────────────────────────────

def _without_side_column(quality: dict) -> dict:
    q = copy.deepcopy(quality)
    q["totals"].pop("steps_accuracy", None)
    for report in q["reports"]:
        report.pop("steps_accuracy", None)
    return q


def test_scoring_reads_saved_results_and_changes_nothing_else(tmp_path):
    d = _episode(tmp_path, "e1", POOR_STEPS)
    plain = rq.score_episode(d)
    qj.judge_episode(d, Scripted(POOR_ANSWER))
    # Off by default: a saved judge file does not show without the flag.
    assert rq.score_episode(d) == plain
    assert "steps_accuracy" not in json.dumps(plain)

    judged = rq.score_episode(d, judge=True)
    assert _without_side_column(judged) == plain
    assert judged["reports"][0]["steps_accuracy"] == {
        "by": "judge", "applies": True, "pass": False,
        "reason": "precision 2/5, recall 3/4", "precision": 0.4, "recall": 0.75,
        "counts": {"correct": 2, "ambiguous": 1, "extra": 1, "wrong": 1,
                   "report_steps": 5, "reference_steps": 4, "missing": 1},
        "labels": ["correct", "wrong", "correct", "extra", "ambiguous"],
        "missing_steps": [2], "step_source": "agent_steps"}
    assert judged["totals"]["steps_accuracy"] == {
        "by": "judge", "judge_accepted": False, "model": MODEL,
        "prompt_version": qj.JUDGE_PROMPT_VERSION, "threshold": 0.8,
        "applies": 1, "evaluated": 1, "passed": 0, "not_judged": 0,
        "labels": {"correct": 2, "ambiguous": 1, "extra": 1, "wrong": 1},
        "missing_steps": 1, "reference_steps": 4}
    # G5 is not a gate: not in the gate map, the gate totals or the headline.
    assert "G5" not in judged["reports"][0]["gates"] and "G5" not in judged["totals"]["gates"]
    assert "G5" not in rq.HEADLINE_GATES and "G5" not in {g.id for g in rq.GATES}
    assert judged["quality_version"] == plain["quality_version"] == rq.QUALITY_VERSION


def test_a_passing_judgement_does_not_make_a_report_customer_ready(tmp_path):
    d = _episode(tmp_path, "e1", GOOD_STEPS)
    qj.judge_episode(d, Scripted(GOOD_ANSWER))
    plain, judged = rq.score_episode(d), rq.score_episode(d, judge=True)
    assert judged["reports"][0]["steps_accuracy"]["pass"] is True
    assert judged["reports"][0]["customer_ready"] == plain["reports"][0]["customer_ready"]
    assert judged["totals"]["customer_ready"] == plain["totals"]["customer_ready"]
    assert judged["totals"]["steps_accuracy"]["passed"] == 1


def test_scoring_works_with_no_judge_file_and_with_a_stale_one(tmp_path, monkeypatch):
    reports = [_report(), _report(matched=None, step=2),
               _report(matched=None, known="known-behaviour", step=3)]
    d = _episode(tmp_path, "e1", GOOD_STEPS, reports=reports)
    plain = rq.score_episode(d)
    judged = rq.score_episode(d, judge=True)
    assert _without_side_column(judged) == plain
    first, unmatched, neutral = (r["steps_accuracy"] for r in judged["reports"])
    assert first == {"by": "judge", "applies": True, "pass": None,
                     "reason": "no saved judge results"}
    assert unmatched["applies"] is False and unmatched["pass"] is None
    assert neutral is None
    totals = judged["totals"]["steps_accuracy"]
    assert (totals["applies"], totals["evaluated"], totals["passed"], totals["not_judged"],
            totals["model"]) == (1, 0, 0, 1, None)

    qj.judge_episode(d, Scripted(GOOD_ANSWER))
    assert rq.score_episode(d, judge=True)["reports"][0]["steps_accuracy"]["pass"] is True
    # Another prompt version: the saved answers are no longer read.
    monkeypatch.setattr(qj, "JUDGE_PROMPT_VERSION", qj.JUDGE_PROMPT_VERSION + 1)
    stale = rq.score_episode(d, judge=True)["reports"][0]["steps_accuracy"]
    assert stale == {"by": "judge", "applies": True, "pass": None,
                     "reason": "the saved judge results are from another prompt version"}
    (d / qj.JUDGE_FILE).write_text("{not json")
    assert rq.score_episode(d, judge=True)["reports"][0]["steps_accuracy"]["reason"] \
        == f"unreadable {qj.JUDGE_FILE}"


def test_the_scoring_command_writes_the_column_only_with_the_flag(tmp_path, capsys):
    d = _episode(tmp_path, "e1", GOOD_STEPS)
    qj.judge_episode(d, Scripted(GOOD_ANSWER))
    assert rq.main([str(tmp_path)]) == 0
    without = (d / rq.QUALITY_FILE).read_text()
    assert "steps_accuracy" not in without
    assert rq.main([str(tmp_path), "--quality-judge"]) == 0
    with_flag = (d / rq.QUALITY_FILE).read_text()
    assert json.loads(with_flag)["totals"]["steps_accuracy"]["passed"] == 1
    assert rq.main([str(tmp_path), "--quality-judge"]) == 0
    assert (d / rq.QUALITY_FILE).read_text() == with_flag      # same bytes
    assert rq.main([str(tmp_path)]) == 0
    assert (d / rq.QUALITY_FILE).read_text() == without
    capsys.readouterr()


# ── the recorded files: no key, no network ─────────────────────────────────────

@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a network call was made")
    monkeypatch.setattr(httpx.Client, "post", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    for name in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"):
        monkeypatch.delenv(name, raising=False)


def test_the_recorded_board_scores_and_reruns_with_no_key_and_no_call(
        tmp_path, no_network, capsys):
    """Acceptance: a committed judge file re-derives the number with no model call, and
    running the judge again over a warm cache calls nothing and changes no byte. The
    recorded answers were written by hand for these synthetic episodes."""
    board = tmp_path / "board"
    shutil.copytree(FIXTURE / "board", board)
    before = _tree(board)
    assert sum(1 for k in before if k.endswith(qj.JUDGE_FILE)) == 2

    assert qj.main([str(board), "--model", MODEL]) == 0
    out = capsys.readouterr().out
    assert "2 reports with steps in 3 episodes; 2 from saved answers, 0 model calls" in out
    assert _tree(board) == before

    assert rq.main([str(board), "--quality-judge"]) == 0
    capsys.readouterr()
    got = {p.parent.name: json.loads(p.read_text())
           for p in sorted(board.rglob(rq.QUALITY_FILE))}
    expected = json.loads((FIXTURE / "expected_steps_accuracy.json").read_text())
    assert {name: {"reports": [r["steps_accuracy"] for r in q["reports"]],
                   "totals": q["totals"]["steps_accuracy"]}
            for name, q in got.items()} == expected
    assert [q["totals"]["steps_accuracy"]["passed"] for q in got.values()] == [0, 1, 0]
    first = _tree(board)
    assert rq.main([str(board), "--quality-judge"]) == 0
    assert _tree(board) == first
    capsys.readouterr()


def test_dry_run_counts_calls_and_writes_nothing(tmp_path, no_network, capsys):
    board = tmp_path / "board"
    shutil.copytree(FIXTURE / "board", board)
    _episode(board, "e-new", POOR_STEPS)
    before = _tree(board)
    assert qj.main([str(board), "--dry-run", "--model", MODEL]) == 0
    out = capsys.readouterr().out
    assert ("3 reports with steps in 4 episodes; 2 already answered; 1 calls to make"
            in out)
    assert "not asked, 1: the report has no step list" in out
    assert _tree(board) == before
    # Under another model nothing saved counts.
    assert qj.main([str(board), "--dry-run", "--model", MODEL + "-b"]) == 0
    assert "0 already answered; 3 calls to make" in capsys.readouterr().out


def test_the_command_needs_a_model_and_only_a_bedrock_one(tmp_path, no_network, capsys):
    _episode(tmp_path, "e1", GOOD_STEPS)
    assert qj.main([str(tmp_path)]) == 2
    assert "--model" in capsys.readouterr().err
    assert qj.main([str(tmp_path), "--model", "some-chat-model"]) == 2
    assert "Bedrock" in capsys.readouterr().err
    assert qj.main([str(tmp_path / "nowhere"), "--model", MODEL]) == 2
    # No token in the environment: the report gets no answer and the exit code says so.
    assert qj.main([str(tmp_path), "--model", MODEL, "--retries", "0"]) == 1
    assert "AWS_BEARER_TOKEN_BEDROCK" in capsys.readouterr().err
    assert not list(tmp_path.rglob(qj.JUDGE_FILE))


def test_the_stand_in_is_deterministic_and_stamps_its_own_name(tmp_path, capsys):
    d = _episode(tmp_path, "e1", GOOD_STEPS)
    assert qj.main([str(tmp_path), "--fake"]) == 0
    first = (d / qj.JUDGE_FILE).read_text()
    assert json.loads(first)["model"] == qj.FakeJudgeClient().model == "fake-word-overlap"
    assert qj.main([str(tmp_path), "--fake"]) == 0
    assert (d / qj.JUDGE_FILE).read_text() == first
    assert "1 from saved answers, 0 model calls" in capsys.readouterr().out
    block = rq.score_episode(d, judge=True)
    assert block["totals"]["steps_accuracy"]["model"] == "fake-word-overlap"
    assert block["reports"][0]["steps_accuracy"]["pass"] in (True, False)
    # The stand-in never replaces a model's answers by accident.
    doc = json.loads(first)
    doc["model"] = MODEL
    (d / qj.JUDGE_FILE).write_text(qj.render(doc))
    assert qj.main([str(tmp_path), "--fake"]) == 1
    assert "1 episodes kept for another model" in capsys.readouterr().out
    assert json.loads((d / qj.JUDGE_FILE).read_text())["model"] == MODEL


# ── the Bedrock client, against a mock transport ───────────────────────────────

def _converse(text: str) -> dict:
    return {"output": {"message": {"content": [{"reasoningContent": {"text": "hm"}},
                                               {"text": text}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 812, "outputTokens": 64}}


def test_the_bedrock_client_sends_one_converse_request(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-token")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if "temperature" in body["inferenceConfig"]:
            return httpx.Response(400, json={"message": "This model doesn't support the "
                                                        "temperature field"})
        return httpx.Response(200, json=_converse(json.dumps(GOOD_ANSWER)))

    client = qj.BedrockJudgeClient(MODEL, transport=httpx.MockTransport(handler))
    reply = client.complete("SYSTEM", "USER")
    assert json.loads(reply.text) == GOOD_ANSWER
    assert reply.usage == {"input_tokens": 812, "output_tokens": 64}
    first, second = seen
    assert str(first.url) == ("https://bedrock-runtime.us-east-2.amazonaws.com/model/"
                              f"{MODEL}/converse")
    assert first.headers["authorization"] == "Bearer test-token"
    assert json.loads(first.content) == {
        "system": [{"text": "SYSTEM"}],
        "messages": [{"role": "user", "content": [{"text": "USER"}]}],
        "inferenceConfig": {"maxTokens": qj.MAX_OUTPUT_TOKENS, "temperature": 0}}
    assert "temperature" not in json.loads(second.content)["inferenceConfig"]
    client.complete("SYSTEM", "USER")
    assert len(seen) == 3                       # refused once, never sent again


def test_the_bedrock_client_fails_without_leaking_the_key(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-token-value")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(403, json={"message": "not allowed"}))
    with pytest.raises(qj.JudgeError) as exc:
        qj.BedrockJudgeClient(MODEL, transport=transport).complete("S", "U")
    assert "HTTP 403: not allowed" in str(exc.value) and "test-token" not in str(exc.value)
    with pytest.raises(qj.JudgeError, match="Bedrock inference-profile"):
        qj.BedrockJudgeClient("some-chat-model")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    with pytest.raises(qj.JudgeError, match="AWS_BEARER_TOKEN_BEDROCK"):
        qj.BedrockJudgeClient(MODEL).complete("S", "U")


# ── the same text and the same reference steps as the label sample ─────────────

def _label_sampler():
    spec = importlib.util.spec_from_file_location(
        "quality_label_sample_for_judge", ROOT / "scripts" / "quality_label_sample.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_steps_are_rendered_as_the_label_sample_renders_them():
    qls = _label_sampler()
    samples = ["  Tap   Save \n now ", "", {"b": 1, "a": "é"}, ["x", 2], 7, None, True,
               {"step": 3, "status": "failed"}]
    assert [qj.render_step(s) for s in samples] == [qls.render_step(s) for s in samples]
    assert qj.render_steps(samples) == qls.render_steps(samples)
    assert qj.render_steps("Tap Save") == qls.render_steps("Tap Save") == []


def test_reference_steps_are_the_label_samples_for_every_public_defect(monkeypatch):
    """A judge label and a human label must be about the same two lists."""
    monkeypatch.undo()                              # the real corpus lookup
    qls = _label_sampler()
    lookup = qls.CorpusLookup()
    compared = with_steps = 0
    for path in sorted((SRC / "data" / "test-cases").glob("*.yaml")):
        app_id = path.stem
        for case in (journey.load_cases(app_id) or {}).get("test_cases") or []:
            for bug in case.get("bugs") or []:
                defect = bug.get("id") if isinstance(bug, dict) else bug
                want = lookup.reference_steps(app_id, str(case["id"]), str(defect))
                assert qj.reference_steps(app_id, str(case["id"]), str(defect)) == want
                compared += 1
                with_steps += want is not None
    assert compared > 20 and with_steps > 20
    assert qj.reference_steps("no-such-app", "x", "y") is None
    assert lookup.reference_steps("no-such-app", "x", "y") is None


# ── the scorer does not know the judge exists ──────────────────────────────────

def test_the_judge_is_outside_the_scorer_and_the_pin_does_not_move():
    for module in (*SCORING_MODULES, "rescore", "rates", "result"):
        assert "quality_judge" not in _package_imports(SRC / f"{module}.py"), module
    assert not [item for item in Closure().items() if item[0] == "quality_judge"]
    pins = read_pin()
    assert closure_sha256() == pins[journey.SCORER_VERSION]
    assert closure_sha256(sources={"quality_judge": ""}) == pins[journey.SCORER_VERSION]


def test_no_model_name_is_a_default_in_the_module():
    """The judge model is configuration: `--model` or the environment variable."""
    source = (SRC / "quality_judge.py").read_text(encoding="utf-8")
    parser_default = qj.main.__code__.co_consts
    assert "us." not in " ".join(c for c in parser_default if isinstance(c, str))
    for vendor in ("anthropic.claude", "openai.gpt", "moonshotai.", "claude-", "gpt-"):
        assert vendor not in source, vendor
