"""Report quality gate G2, located (QUA-3586): a valid report names a step, the truth
shows the matched defect's symptom at that step, and the device showed the report's
quote while the agent was on it.

The cases here are a small made-up app served from a temporary held-out directory, so
the tests pin the RULE and do not move when the corpus does. The copies this module
keeps of the scorer's tables and normalisers are compared with the scorer's own."""

from __future__ import annotations

import json

import pytest
import yaml
from test_mcp_scoring_parity import Call, Say, _claude, _codex, _pair
from test_report_quality import _episode

from qualgentbench import interactions, journey, transcript
from qualgentbench import report_quality as rq

FORMATS = {"claude": _claude, "codex": _codex}
APP = "demo-notes"

CASES = {"test_cases": [
    {   # A blocking bug whose symptom is the screen after Save, and a side bug (a typo
        # in the editor's title) that shows while the editor is open.
        "id": "notes-save",
        "steps": ["Tap New note.", 'Enter the title "Milk".', "Save the note.",
                  "Read the list."],
        "check": {"steps": ["launch", {"tap": "New note"}, {"tap": "title_field"},
                            {"type": "Milk"}, {"tap": "Save"}, "wait"]},
    },
    {   # The seeded build dies when About is tapped.
        "id": "notes-about",
        "steps": ["Open Settings.", "Tap About.", "Read the version."],
        "check": {"steps": ["launch", {"tap": "Settings"}, {"tap": "About"}, "wait"]},
    },
    {   # A brief that names nothing the route taps: the two cannot be lined up.
        "id": "notes-vague",
        "steps": ["Start a note.", "Give it a name.", "Keep it.", "Check it is there."],
        "check": {"steps": ["launch", {"tap": "fab"}, {"tap": "title_field"},
                            {"type": "Milk"}, {"tap": "confirm"}, "wait"]},
    },
]}
TRUTH = {
    "notes-save": {
        "blocking": "save-drops-note",
        "side": [{"bug": "editor-title-typo", "marker": "New ntoe",
                  "visible_steps": [2, 3, 4], "texts": ["New ntoe"]}],
        "unclaimed_diff": [{"step": 5, "added": ["No notes"], "removed": ["Milk"]}],
        "passes": {"seeded": {"outcome": "violated", "detail": "", "steps_run": 6}},
    },
    "notes-about": {
        "blocking": "about-crash", "side": [], "unclaimed_diff": [],
        "passes": {"seeded": {"outcome": "crashed", "steps_run": 4,
                              "detail": "step 4: demo.notes crashed — NullPointerException"}},
    },
    "notes-vague": {
        "blocking": "save-drops-note", "side": [],
        "unclaimed_diff": [{"step": 5, "added": ["No notes"], "removed": ["Milk"]}],
        "passes": {"seeded": {"outcome": "violated", "detail": "", "steps_run": 6}},
    },
}


@pytest.fixture(autouse=True)
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    (root / "test-cases").mkdir(parents=True)
    (root / "truth").mkdir()
    (root / "test-cases" / f"{APP}.yaml").write_text(yaml.safe_dump(CASES))
    (root / "truth" / f"journey-{APP}.json").write_text(json.dumps(TRUTH))
    monkeypatch.setenv(rq.HELDOUT_ENV, str(root))
    return root


def _screen(*elements) -> str:
    """A screen listing as the device tools answer: `(text, x, y)` or just text."""
    out = []
    for el in elements:
        text, x, y = el if isinstance(el, tuple) else (el, 0, 0)
        out.append({"text": text, "x": x, "y": y})
    return json.dumps({"elements": out})


def _tap(text: str, *screen) -> Call:
    return Call("mobile_tap_and_observe", {"element_text": text}, _screen(*screen))


HOME = ("Notes", "New note", "Settings")
EDITOR = ("New ntoe", "Title", "Save")
SAVE_RUN = [
    Call("mobile_observe_screen", {}, _screen(*HOME)),
    _tap("New note", *EDITOR),
    Call("mobile_type_text", {"text": "Milk"}, "Set focused field to: 'Milk'"),
    _tap("Save", "Notes", "No notes", "New note"),
    Call("mobile_observe_screen", {}, _screen("Notes", "No notes", "New note")),
]


def _report(step, observed, matched="save-drops-note", **over) -> dict:
    return {"step": step, "screen": "", "observed": observed, "expected": "Milk",
            "description": "the saved note is not in the list", "matched": matched,
            "grounded": True, "known": None, "consequence_of": None, **over}


def _saved(tmp_path, episode, reports, *, case="notes-save", fmt="claude",
           condition="mcp", name="ep1"):
    metrics = {"app_id": APP, "case_id": case, "version": "seeded", "reports": reports,
               "scorer_version": journey.SCORER_VERSION, "report_source": "findings_file"}
    d = _episode(tmp_path, name, metrics, condition=condition)
    transcript_text = episode if isinstance(episode, str) else FORMATS[fmt](episode)
    (d / rq.TRANSCRIPT_FILE).write_text(transcript_text)
    return d


def _g2(tmp_path, episode, report, **kw) -> dict:
    return rq.score_episode(_saved(tmp_path, episode, [report], **kw))["reports"][0]["gates"]["G2"]


# ── the acceptance cases ───────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", sorted(FORMATS))
@pytest.mark.parametrize("step", [3, 4])
def test_the_right_step_passes(tmp_path, fmt, step):
    """Save is step 3; step 4 only reads the screen Save left. Either is the step."""
    g = _g2(tmp_path, SAVE_RUN, _report(step, "No notes"), fmt=fmt)
    assert g["pass"] is True and "unknown" not in g
    assert f"step {step}" in g["reason"]


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_a_real_quote_from_another_step_fails(tmp_path, fmt):
    """`New ntoe` really was on the device, on the editor (steps 1 and 2). A report
    that puts it at step 3, where the truth does show this defect, is not located."""
    report = _report(3, "New ntoe")
    assert report["grounded"] is True           # the scorer's "seen anywhere" holds
    g = _g2(tmp_path, SAVE_RUN, report, fmt=fmt)
    assert g == {"pass": False,
                 "reason": "the device showed the observed text at step 1, 2, not at step 3"}


def test_a_step_where_the_truth_does_not_show_the_defect_fails(tmp_path):
    g = _g2(tmp_path, SAVE_RUN, _report(1, "New note"))
    assert g == {"pass": False, "reason": "the report names step 1; the truth shows "
                                          "save-drops-note at step 3, 4"}
    # The side bug shows on the editor: route steps 2-4, brief steps 1-3.
    g = _g2(tmp_path, SAVE_RUN, _report(2, "New ntoe", matched="editor-title-typo"),
            name="ep2")
    assert g["pass"] is True
    g = _g2(tmp_path, SAVE_RUN, _report(4, "New ntoe", matched="editor-title-typo"),
            name="ep3")
    assert g["pass"] is False and "at step 1, 2, 3" in g["reason"]


def test_no_step_named_fails(tmp_path):
    g = _g2(tmp_path, SAVE_RUN, _report(None, "No notes"))
    assert g == {"pass": False, "reason": "the report names no step"}
    g = _g2(tmp_path, SAVE_RUN, _report(9, "No notes"), name="ep2")
    assert g == {"pass": False, "reason": "the report names step 9; the case has 4 steps"}


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_a_crash_at_the_recorded_step_passes(tmp_path, fmt):
    """The route found the app dead at its step 4 (`wait`); the tap that killed it is
    step 3, About. Both belong to brief step 2, and step 3 only reads."""
    run = [Call("mobile_observe_screen", {}, _screen(*HOME)),
           _tap("Settings", "Settings", "About"),
           _tap("About", "Demo Notes keeps stopping", "Close app")]
    report = _report(2, "Demo Notes keeps stopping", matched="about-crash")
    g = _g2(tmp_path, run, report, case="notes-about", fmt=fmt)
    assert g["pass"] is True and "crashes about-crash" in g["reason"]
    # One step early is not where it crashed.
    g = _g2(tmp_path, run, {**report, "step": 1}, case="notes-about", fmt=fmt, name="ep2")
    assert g == {"pass": False, "reason": "the report names step 1; the truth crashes "
                                          "about-crash at step 2, 3"}


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_ambiguous_attribution_is_unknown_never_a_pass(tmp_path, fmt):
    """The agent taps coordinates nothing lists and never says which step it is on.
    The quote was on the device, but on which step is not recorded."""
    run = [Call("mobile_observe_screen", {}, "screenshot only"),
           Call("mobile_tap", {"x": 10, "y": 20}, "Tapped (10, 20)"),
           Call("mobile_tap", {"x": 30, "y": 40}, "Tapped (30, 40)"),
           Call("mobile_observe_screen", {}, _screen("Notes", "No notes"))]
    d = _saved(tmp_path, run, [_report(3, "No notes")], fmt=fmt)
    q = rq.score_episode(d)
    g = q["reports"][0]["gates"]["G2"]
    assert g["pass"] is None and g["unknown"] is True
    assert "does not say whether those calls were step 3" in g["reason"]
    # An unknown G2 never makes a report customer-ready, whatever the other gates say.
    assert q["reports"][0]["customer_ready"] is not True
    every = {gate: {"pass": True, "reason": ""} for gate in rq.HEADLINE_GATES}
    assert rq._customer_ready({**every, "G2": g}) is None
    assert q["totals"]["gates"]["G2"] == {"name": "located", "evaluated": 0, "passed": 0,
                                          "unknown": 1}


def test_unknown_is_not_the_same_as_not_run(tmp_path):
    """A false report has no defect to locate: G2 does not run, and that is not an
    unknown. A neutral report passes through no gate at all."""
    reports = [_report(3, "No notes", matched=None),
               _report(3, "No notes", matched=None, known="some-known-behaviour")]
    q = rq.score_episode(_saved(tmp_path, SAVE_RUN, reports))
    false, neutral = q["reports"]
    assert false["gates"]["G2"] == {"pass": None,
                                    "reason": "not run: the report matched no seeded defect"}
    assert false["customer_ready"] is False and neutral["gates"] == {}
    assert q["totals"]["gates"]["G2"] == {"name": "located", "evaluated": 0, "passed": 0,
                                          "unknown": 0}


# ── what grounds a quote ───────────────────────────────────────────────────────

def test_an_echo_and_a_refusal_ground_nothing(tmp_path):
    """The text-entry reply repeats what was typed, and a refused call repeats its
    argument: neither is the device showing it (the scorer's rule)."""
    run = [*SAVE_RUN[:3],
           Call("mobile_tap_and_observe", {"element_text": "No notes"},
                "Error executing tool mobile_tap_and_observe: Element 'No notes' not found.",
                is_error=True),
           _tap("Save", "Notes", "New note")]
    for i, quote in enumerate(("Milk", "No notes")):
        g = _g2(tmp_path, run, _report(3, quote), name=f"ep{i}")
        assert g == {"pass": False, "reason": "the observed text is in no device output"}


def test_no_quote_fails(tmp_path):
    g = _g2(tmp_path, SAVE_RUN, _report(3, ""))
    assert g == {"pass": False, "reason": "the report quotes no observed text"}


@pytest.mark.parametrize("version", ["clean", "seeded"])
def test_grounding_text_is_the_scorers(version):
    """The same device results, in both transcript formats, as the scorer grounds on."""
    for text in _pair(version):
        mine = rq.grounding_texts(rq.agent_events(text, raw=False))
        assert sorted(mine) == sorted(journey._device_texts(text, "mcp", results_only=True))
        assert mine


def test_grounding_text_on_the_raw_arm_is_the_scorers():
    claude = "\n".join(json.dumps(e) for e in (
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash",
             "input": {"command": "adb shell uiautomator dump /dev/tty"}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": 'text="No  Notes"'},
            {"type": "tool_result", "tool_use_id": "b", "content": "findings.yaml"}]}}))
    codex = "\n".join(json.dumps({"type": "item.completed", "item": i}) for i in (
        {"type": "command_execution", "command": "adb shell input text 'Milk'",
         "aggregated_output": ""},
        {"type": "command_execution", "command": "adb exec-out uiautomator dump /dev/tty",
         "aggregated_output": 'text="No\\u00a0notes"'},
        {"type": "command_execution", "command": "cat findings.yaml",
         "aggregated_output": "verdict: fail"}))
    for text in (claude, codex):
        events = rq.agent_events(text, raw=True)
        assert rq.grounding_texts(events) == journey._device_texts(text, "raw",
                                                                   results_only=True)
    typed, read, _ = rq.agent_events(codex, raw=True)
    assert typed.acts and typed.targets == ("milk",) and not read.acts


# ── which step a result belongs to ─────────────────────────────────────────────

def _case(case_id: str = "notes-save") -> rq.CaseTruth:
    case = rq.load_case_truth(APP, case_id)
    assert isinstance(case, rq.CaseTruth)
    return case


def _labels(episode, case_id: str = "notes-save") -> list[tuple[int, int, int]]:
    events = rq.agent_events(_claude(episode), raw=False)
    return [(s.first, s.sure_last, s.last) for s in rq.attribute_steps(events, _case(case_id))]


def test_route_steps_are_lined_up_with_brief_steps():
    case = _case()
    # launch, tap New note, tap title_field, type Milk, tap Save, wait
    assert rq._route_spans(case.brief, case.route) == [
        (1, 1), (1, 1), (1, 2), (2, 2), (3, 3), (3, 4)]
    steps = rq.step_map(case)
    assert steps.reads_only == {4}              # "Read the list." has no action
    assert [steps.through(s) for s in (1, 2, 3, 4)] == [2, 3, 4, 4]
    assert steps.fitting("save") == [3] and steps.fitting("milk") == [2]
    assert steps.fitting("title_field") == []   # could be step 1 or 2
    assert rq.symptom_brief_steps(case, [5]) == [3, 4]
    assert rq.symptom_brief_steps(case, [2, 3, 4]) == [1, 2, 3]


def test_results_follow_the_action_that_left_the_screen():
    assert _labels(SAVE_RUN) == [
        (1, 1, 1),      # the first read: nothing done yet, step 1
        (1, 2, 2),      # New note's own result: step 1, up until step 2 acts
        (3, 4, 4),      # Save's result (the text entry's echo grounds nothing)
        (3, 4, 4)]      # a read after Save: still the screen Save left


def test_an_announced_step_marks_the_next_action():
    """ "Step 3:" before a tap on coordinates makes that tap step 3's. A read made after
    announcing the next step, before acting there, counts for both steps."""
    run = [Say("Step 1: start a new note."),
           Call("mobile_tap", {"x": 1, "y": 2}, "Tapped (1, 2)"),
           Call("mobile_observe_screen", {}, _screen(*EDITOR)),
           Say("Editor is open. Step 3: save."),
           Call("mobile_observe_screen", {}, _screen(*EDITOR)),
           Call("mobile_tap", {"x": 5, "y": 6}, "Tapped (5, 6)"),
           Call("mobile_observe_screen", {}, _screen("Notes", "No notes"))]
    assert _labels(run) == [(1, 2, 2), (1, 2, 2), (1, 4, 4), (3, 4, 4), (3, 4, 4)]
    # A message naming two steps, or a step the case does not have, announces nothing.
    run[3] = Say("Steps 1 and 2 worked. Step 3: save. Step 4: read.")
    assert _labels(run)[2:] == [(1, 2, 2), (1, 0, 4), (1, 0, 4)]


def test_a_tap_on_coordinates_aims_at_what_the_listing_put_there(tmp_path):
    run = [Call("mobile_observe_screen", {},
                _screen(("Notes", 50, 80), ("New note", 500, 900))),
           Call("mobile_tap_and_observe", {"x": 500, "y": 900},
                _screen(("New ntoe", 300, 80), ("Title", 300, 200), ("Save", 600, 80))),
           Call("mobile_type_text", {"text": "Milk"}, "Set focused field to: 'Milk'"),
           Call("mobile_tap_and_observe", {"x": 600, "y": 80}, _screen("Notes", "No notes"))]
    assert _labels(run) == [(1, 1, 1), (1, 2, 2), (3, 4, 4)]
    assert _g2(tmp_path, run, _report(3, "No notes"))["pass"] is True
    # claude-code names the screenshot's file before the listing.
    listing = "[Image: source: /tmp/shot.png] " + _screen(("Save", 600, 80))
    assert rq._label_at(listing, (600, 80)) == "save" and rq._label_at(listing, (1, 1)) == ""
    assert rq._label_at("no listing", (600, 80)) == ""


def test_an_action_that_fits_no_step_is_bounded_by_the_next_mark():
    """Between New note (step 1) and Save (step 3), a tap that fits nothing is step 1,
    2 or 3. After Save no later step has an action, so such a tap is still step 3."""
    run = [_tap("New note", *EDITOR),
           Call("mobile_tap", {"x": 1, "y": 2}, "Tapped (1, 2)"),
           Call("mobile_observe_screen", {}, _screen("Title", "Bold")),
           _tap("Save", "Remind me?", "OK"),
           _tap("OK", "Notes", "No notes")]
    assert _labels(run) == [(1, 2, 2), (1, 0, 4), (1, 0, 4), (3, 4, 4), (3, 4, 4)]


def test_going_back_is_sure_only_of_the_step_redone(tmp_path):
    """After Save the agent taps New note again: that is step 1's action, but it may
    still be looking into step 3."""
    run = [*SAVE_RUN, _tap("New note", "New ntoe", "Draft kept")]
    assert _labels(run)[-1] == (1, 2, 4)
    assert _g2(tmp_path, run, _report(3, "Draft kept"))["unknown"] is True
    g = _g2(tmp_path, run, _report(1, "Draft kept", matched="editor-title-typo"), name="ep2")
    assert g["pass"] is True


def test_starting_the_app_again_is_recovery_not_a_step(tmp_path):
    """The crash dialog comes up only after the agent restarts the app. The route
    never restarts it, so the agent is still on the step that crashed."""
    run = [_tap("Settings", "Settings", "About"),
           _tap("About", "Launcher"),
           Call("mobile_launch_app", {"package_id": "demo.notes"}, "Launched demo.notes"),
           Call("mobile_observe_screen", {}, _screen("Demo Notes keeps stopping"))]
    report = _report(2, "Demo Notes keeps stopping", matched="about-crash")
    assert _g2(tmp_path, run, report, case="notes-about")["pass"] is True
    # A call the server refused did nothing: it marks no step.
    refused = Call("mobile_tap_and_observe", {"element_text": "Settings"},
                   "Error executing tool mobile_tap_and_observe: Element 'Settings' not "
                   "found.", is_error=True)
    assert _labels([*run, refused, run[-1]], "notes-about")[-1] == (2, 3, 3)


# ── the step the report names ──────────────────────────────────────────────────

def test_the_customer_reports_failure_step_names_the_step(tmp_path):
    """The accepted report call says step 3; the findings entry said 1."""
    call = Call(rq.REPORT_TOOL, {"status": "FAIL", "summary": "the note is not saved",
                                 "failure_step": "Step 3: Save", "expected": "Milk",
                                 "actual": "No notes"}, '{"ok": true}')
    d = _saved(tmp_path, [*SAVE_RUN, call], [_report(1, "No notes")])
    report = rq.score_episode(d)["reports"][0]
    assert report["customer_report"]["failure_step"] == "Step 3: Save"
    assert report["step"] == 1 and report["gates"]["G2"]["pass"] is True


# ── when the truth cannot answer ───────────────────────────────────────────────

def test_unknown_when_the_truth_has_no_step_or_no_case(tmp_path, corpus):
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"] = []          # seen only in stored data
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    g = _g2(tmp_path, SAVE_RUN, _report(3, "No notes"))
    assert g == {"pass": None, "unknown": True,
                 "reason": "the truth records no screen step for save-drops-note"}
    g = _g2(tmp_path, SAVE_RUN, _report(3, "No notes", matched="other-bug"), name="ep2")
    assert g["unknown"] is True and "does not list other-bug" in g["reason"]
    g = _g2(tmp_path, SAVE_RUN, _report(3, "No notes"), case="no-such-case", name="ep3")
    assert g["unknown"] is True and "no such case" in g["reason"]


def test_unknown_when_the_episode_ran_other_steps(tmp_path):
    """The saved brief is checked against the corpus: a case edited since the run is
    not judged by today's steps."""
    d = _saved(tmp_path, SAVE_RUN, [_report(3, "No notes")])
    (d / rq.BRIEF_FILE).write_text("Steps:\n  1. Tap New note.\n  2. Save the note.\n")
    g = rq.score_episode(d)["reports"][0]["gates"]["G2"]
    assert g["unknown"] is True and "not the steps this episode was sent" in g["reason"]
    brief = "\n".join(f"  {i}. {s}" for i, s in enumerate(CASES["test_cases"][0]["steps"], 1))
    (d / rq.BRIEF_FILE).write_text(f"Steps:\n{brief}\n")
    assert rq.score_episode(d)["reports"][0]["gates"]["G2"]["pass"] is True


def test_a_brief_that_cannot_be_lined_up_decides_less(tmp_path):
    """Nothing in this brief names what the route taps, so every route step could be
    any brief step: the truth rules no step out, and no tap can be placed."""
    case = _case("notes-vague")
    assert rq._route_spans(case.brief, case.route) == [(1, 4)] * 6
    assert rq.symptom_brief_steps(case, [5]) == [1, 2, 3, 4]
    g = _g2(tmp_path, SAVE_RUN, _report(3, "No notes"), case="notes-vague")
    assert g["pass"] is None and g["unknown"] is True


def test_where_the_truth_shows_each_kind_of_defect():
    row = TRUTH["notes-save"]
    assert rq.symptom_route_steps(row, "editor-title-typo") == ("side", [2, 3, 4])
    assert rq.symptom_route_steps(row, "save-drops-note") == ("blocking", [5])
    assert rq.symptom_route_steps(row, "nope") == ("", [])
    # A death: the step it was recorded at and the one before (the action that did it).
    assert rq.symptom_route_steps(TRUTH["notes-about"], "about-crash") == ("crash", [3, 4])
    no_detail = {"blocking": "b", "passes": {"seeded": {"outcome": "crashed", "steps_run": 1}}}
    assert rq.symptom_route_steps(no_detail, "b") == ("crash", [1])
    # A one-character diff text identifies nothing (the scorer's floor).
    short = {"blocking": "b", "unclaimed_diff": [{"step": 2, "added": ["A"], "removed": []}]}
    assert rq.symptom_route_steps(short, "b") == ("blocking", [])


# ── the copies of the scorer's rules ───────────────────────────────────────────

def test_the_tool_lists_are_the_scorers_table():
    rules = interactions.MCP_TOOL_RULES
    assert rq._MCP_NOT_EVIDENCE == {n for n, r in rules.items() if not r.device}
    assert rq._MCP_ECHO == set(interactions.MCP_ECHO_TOOLS)
    acts = {n for n, r in rules.items() if set(r.steps) - {interactions.OBSERVE}}
    assert rq._MCP_ACTIONS == acts - {"mobile_app_logs", "mobile_crash_logs",
                                      "mobile_device_logs", "mobile_dismiss_dialogs"}
    assert rq._MCP_LIFECYCLE <= rq._MCP_ACTIONS
    for name in [*rules, "mobile_future_tool", "qg_acquire_device", "Bash", ""]:
        assert rq._mcp_is_evidence(name) == interactions.mcp_is_device_evidence(name), name


def test_the_text_rules_are_the_scorers():
    assert rq._ROUTE_TEXT_KEYS == journey.ECHO_ROUTE_KEYS
    assert rq.MIN_EVIDENCE_CHARS == journey._MIN_EVIDENCE_CHARS
    assert rq._REFUSED_REPLY_RE.pattern == journey._REFUSED_REPLY_RE.pattern
    assert rq._REFUSED_REPLY_RE.flags == journey._REFUSED_REPLY_RE.flags
    for text in ('  "Getting  Started with Orgzly"  ', "A", "#B", "", "9:00 AM"):
        assert rq._norm_text(text) == journey._norm(text)
        assert rq._needle(text) == journey._evidence(text)
    for text in ("# Pancakes\\n\\nFlour", "a\\\\nb", "Tab\\there  X Y", ""):
        assert rq._device_text(text) == journey._device_text(text)
    for text in ("\\u2022 Notes", "\\\\u2022", "\\ud83d\\ude00", "\\ud83d alone", "plain"):
        assert rq._decode_unicode_escapes(text) == transcript.decode_unicode_escapes(text)
    for needle, text in (("age", "average"), ("9 left", "19 left"), ("#b", "note #b here")):
        assert rq._has_word(needle, text) == journey._word(needle, text)
    for result in (None, "text", [{"type": "text", "text": "a"}, {"type": "image"}],
                   {"content": [{"type": "text", "text": "b"}]}, {"structured": 1}, 7):
        assert rq._result_text(result) == transcript.mcp_result_text(result)


def test_every_case_in_the_corpus_can_be_lined_up(monkeypatch):
    """The packaged corpus: every case's route lines up with its brief, and every
    seeded defect the truth knows gets a kind. No number is pinned here."""
    monkeypatch.delenv(rq.HELDOUT_ENV)
    seen = 0
    for path in sorted((rq._CORPUS_DIR / "test-cases").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        for entry in doc["test_cases"]:
            case = rq.load_case_truth(path.stem, entry["id"])
            if isinstance(case, str):       # a case with no derived truth row
                continue
            spans = rq._route_spans(case.brief, case.route)
            assert len(spans) == len(case.route)
            assert all(1 <= a <= b <= len(case.brief) for a, b in spans), entry["id"]
            assert [a for a, _ in spans] == sorted(a for a, _ in spans), entry["id"]
            for bug in entry.get("bugs") or []:
                bug_id = bug["id"] if isinstance(bug, dict) else bug
                kind, route_steps = rq.symptom_route_steps(case.row, bug_id)
                assert kind in ("side", "crash", "blocking"), (entry["id"], bug_id)
                shown = rq.symptom_brief_steps(case, route_steps)
                assert set(shown) <= set(range(1, len(case.brief) + 1))
                seen += 1
    assert seen > 100
