"""Report quality gate G2, located (QUA-3586, hardened by QUA-3604): a valid report
names a step, the truth shows the matched defect's symptom at that step, and the device
shows that symptom on it: a truth text of the defect that the report quotes, or for a
crash the quote on a screen the agent's own action left, or for a defect that removes
text a screen that lacks it.

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
# What the truth recorded on the screen after each route step, on each pass.
_HOME = ["Notes", "New note", "Settings"]
_EDITOR = ["New note", "Title", "Save"]
_TYPO_EDITOR = ["New ntoe", "Title", "Save"]
_SAVE_SCREENS = {
    "clean": [_HOME, _EDITOR, _EDITOR, [*_EDITOR, "Milk"], [*_HOME, "Milk"], [*_HOME, "Milk"]],
    "seeded": [_HOME, _TYPO_EDITOR, _TYPO_EDITOR, [*_TYPO_EDITOR, "Milk"],
               ["Notes", "No notes", "New note"], ["Notes", "No notes", "New note"]],
}
_LAUNCHER = ["Wed, Sep 16", "Chrome"]
TRUTH = {
    "notes-save": {
        "screens": _SAVE_SCREENS,
        "blocking": "save-drops-note",
        "side": [{"bug": "editor-title-typo", "marker": "New ntoe",
                  "visible_steps": [2, 3, 4], "texts": ["New ntoe"]}],
        "unclaimed_diff": [{"step": 5, "added": ["No notes"], "removed": ["Milk"]}],
        "passes": {"seeded": {"outcome": "violated", "detail": "", "steps_run": 6}},
    },
    "notes-about": {
        "screens": {"clean": [_HOME, ["Settings", "About"], ["About", "Version 1.0"],
                              ["About", "Version 1.0"]],
                    "seeded": [_HOME, ["Settings", "About"], _LAUNCHER, _LAUNCHER]},
        "blocking": "about-crash", "side": [], "unclaimed_diff": [],
        "passes": {"seeded": {"outcome": "crashed", "steps_run": 4,
                              "detail": "step 4: demo.notes crashed — NullPointerException"}},
    },
    "notes-vague": {
        "screens": _SAVE_SCREENS,
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
    """Nothing observed and nothing named as missing: there is no symptom to look for."""
    g = _g2(tmp_path, SAVE_RUN, _report(3, "", expected="the note is in the list"))
    assert g == {"pass": False, "reason": "the report quotes no observed text"}


# ── QUA-3604: what the quote has to be, and what ties a screen to a step ───────

@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_the_truth_text_is_looked_for_inside_the_sentence(tmp_path, fmt):
    """Through the report tool `observed` is a sentence around the quote. No device
    result holds the sentence; the truth text in it was on the device at step 3."""
    sentence = 'After Save the list is empty: it shows "No notes" and no row for the note.'
    g = _g2(tmp_path, SAVE_RUN, _report(3, sentence), fmt=fmt)
    assert g["pass"] is True and "step 3" in g["reason"]
    # The same sentence about a text the device never showed is in no device output.
    g = _g2(tmp_path, SAVE_RUN[:3], _report(3, sentence), fmt=fmt, name="ep2")
    assert g == {"pass": False, "reason": "the observed text is in no device output"}
    # And the truth text shown on another step only does not locate it.
    early = [Call("mobile_observe_screen", {}, _screen("Notes", "No notes", "New note")),
             _tap("New note", *EDITOR), _tap("Save", "Notes", "New note")]
    g = _g2(tmp_path, early, _report(3, sentence), fmt=fmt, name="ep3")
    assert g == {"pass": False,
                 "reason": "the device showed the observed text at step 1, not at step 3"}


def test_a_quote_that_is_no_truth_text_of_the_defect_is_unknown(tmp_path):
    """`Notes` (the screen's title) really was on the device at step 3. It is on the
    clean build's screen too: quoting it shows nothing about this defect."""
    g = _g2(tmp_path, SAVE_RUN, _report(3, "Notes", expected="the note is in the list"))
    assert g == {"pass": None, "unknown": True,
                 "reason": "the device showed the observed text on step 3, but it is none "
                           "of the texts the truth measured for save-drops-note"}
    # A side bug's truth texts are its own: the blocking bug's text does not locate it.
    g = _g2(tmp_path, SAVE_RUN, _report(2, "Save", matched="editor-title-typo"), name="ep2")
    assert g["pass"] is None and g["unknown"] is True
    assert "none of the texts the truth measured for editor-title-typo" in g["reason"]


def test_every_truth_text_the_report_quotes_must_be_on_the_step(tmp_path, corpus):
    """Two truth texts in one quote, one of them on every screen: the one that is on
    the named step does not carry the other."""
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"][0]["added"] = ["No notes", "Notes"]
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    run = [Call("mobile_observe_screen", {}, _screen("Notes", "No notes", "New note")),
           _tap("New note", *EDITOR), _tap("Save", "Notes", "New note")]
    g = _g2(tmp_path, run, _report(3, "Notes / No notes"))
    assert g == {"pass": None, "unknown": True,
                 "reason": "the device showed part of the observed text on step 3 and the "
                           "rest only on other steps"}
    assert _g2(tmp_path, SAVE_RUN, _report(3, "Notes / No notes"), name="ep2")["pass"] is True


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_saying_a_step_without_acting_locates_nothing(tmp_path, fmt):
    """The guesser: it says it is on step 3, reads the screen once and never touches
    the app. Even a truth text on that screen is not tied to step 3 by the say-so."""
    run = [Say("Step 3: saving the note."),
           Call("mobile_observe_screen", {}, _screen("Notes", "No notes", "New note"))]
    assert _labels(run) == [(1, 1, 4)]      # surely the start screen; possibly step 3's
    g = _g2(tmp_path, run, _report(3, "No notes"), fmt=fmt)
    assert g["pass"] is None and g["unknown"] is True
    assert "does not say whether those calls were step 3" in g["reason"]
    # The start screen's own text, which is what such an agent has to quote: unknown
    # at best, and a fail once the report says more than the device showed.
    g = _g2(tmp_path, run, _report(3, "New note", expected=""), fmt=fmt, name="ep2")
    assert g["pass"] is None and g["unknown"] is True
    # With one action after the announcement the screen is step 3's (the announced
    # step marks the next action, as before).
    acted = [run[0], Call("mobile_tap", {"x": 1, "y": 2}, "Tapped (1, 2)"), run[1]]
    assert _g2(tmp_path, acted, _report(3, "No notes"), fmt=fmt, name="ep3")["pass"] is True


def test_a_crash_with_no_dialog_needs_the_agents_own_action_on_the_step(tmp_path):
    """Most crashes put up no dialog: the agent sees the launcher. Its quote is no
    truth text, so what locates the report is that it acted on the step."""
    run = [Call("mobile_observe_screen", {}, _screen(*HOME)),
           _tap("Settings", "Settings", "About"),
           _tap("About", "Wed, Sep 16", "Chrome")]
    report = _report(2, "Wed, Sep 16", matched="about-crash", expected="the version")
    g = _g2(tmp_path, run, report, case="notes-about")
    assert g == {"pass": True, "reason": "step 2: the truth crashes about-crash there, the "
                                         "agent acted on that step, read one of that "
                                         "step's screens and the device showed the "
                                         "observed text on it"}
    # The same quote read off the screen the case starts on, by an agent that only said
    # it was on step 1 and did nothing: not located. (Step 1 is where the truth would
    # crash a case whose first tap kills the app.)
    truth_step_1 = {**report, "step": 1}
    idle = [Say("Step 1: open Settings."),
            Call("mobile_observe_screen", {}, _screen("Wed, Sep 16", "Chrome"))]
    case = rq.load_case_truth(APP, "notes-about")
    assert not any(s.acted for s in rq.attribute_steps(rq.agent_events(_claude(idle), False),
                                                       case))
    g = _g2(tmp_path, idle, truth_step_1, case="notes-about", name="ep2")
    assert g["pass"] is False        # step 1 is not where this case crashes
    g = _g2(tmp_path, idle[1:], {**report, "step": 2}, case="notes-about", name="ep3")
    assert g["pass"] is not True


def test_a_crash_quote_on_the_start_screen_is_unknown(tmp_path, corpus):
    """A case whose FIRST tap kills the app: the truth crashes it at step 1, and the
    start screen is step 1's too. Reading it without acting locates nothing."""
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-about"]["passes"]["seeded"] = {
        "outcome": "crashed", "steps_run": 2, "detail": "step 2: demo.notes crashed"}
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    report = _report(1, "Settings", matched="about-crash", expected="the version")
    idle = [Call("mobile_observe_screen", {}, _screen(*HOME))]
    g = _g2(tmp_path, idle, report, case="notes-about")
    assert g == {"pass": None, "unknown": True,
                 "reason": "the device showed the observed text on step 1, but no device "
                           "action of the agent's is tied to that step"}
    # Acting is not enough (QUA-3607): the screen its tap left holds nothing the truth
    # recorded on step 1's screens beyond the start screen. Unknown, never a fail.
    acted = [*idle, _tap("Settings", "Settings", "Wed, Sep 16")]
    g = _g2(tmp_path, acted, report, case="notes-about", name="ep2")
    assert g["pass"] is None and g["unknown"] is True
    assert "nothing shows the agent had reached the step" in g["reason"]
    # It read the screen the tap opened (`About` is on it, and not on the start screen)
    # before the app went: located.
    reached = [*idle, _tap("Settings", "Settings", "About"),
               Call("mobile_observe_screen", {}, _screen("Settings", "Wed, Sep 16"))]
    assert _g2(tmp_path, reached, report, case="notes-about", name="ep3")["pass"] is True


def test_the_crash_wording_and_the_named_exception_are_truth_texts(tmp_path, corpus):
    cases = json.loads(json.dumps(CASES))
    cases["test_cases"][1]["check"]["expect"] = {"crash": "NullPointerException"}
    (corpus / "test-cases" / f"{APP}.yaml").write_text(yaml.safe_dump(cases))
    case = rq.load_case_truth(APP, "notes-about")
    assert rq.symptom_texts(case, "about-crash").shown == (
        "NullPointerException", "closed unexpectedly", "has stopped", "keeps stopping",
        "stopped working")
    run = [_tap("Settings", "Settings", "About"),
           _tap("About", "Launcher", "crash: java.lang.NullPointerException at About.kt")]
    report = _report(2, "The app closed; the screen reported java.lang.NullPointerException "
                        "at About.kt", matched="about-crash")
    assert _g2(tmp_path, run, report, case="notes-about")["pass"] is True
    # A hang's wording is not a crash's.
    assert rq._text_re("isn't responding").search("demo notes isn't responding")
    assert not any(rq._text_re(t).search("demo notes isn't responding")
                   for t in rq.symptom_texts(case, "about-crash").shown)


def test_a_defect_that_removes_text_is_located_by_a_screen_without_it(tmp_path, corpus):
    """The truth has nothing the seeded build SHOWS for this defect, only `Milk`, which
    the clean build shows. The report cannot quote an absence; the screen the agent read
    on the step, after its own action there, lacks the text."""
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"] = [{"step": 5, "added": [], "removed": ["Milk"]}]
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    case = rq.load_case_truth(APP, "notes-save")
    assert rq.symptom_texts(case, "save-drops-note") == rq.SymptomTexts((), ("Milk",))
    passed = {"pass": True, "reason": "step 3: the truth shows save-drops-note there as "
                                      "missing text, and the screen it shows that on, "
                                      "read by the agent on that step, lacks it"}
    # Whatever it wrote under observed: nothing, a sentence, text from another step.
    for i, observed in enumerate(("", "The list has no row for the note", "New ntoe")):
        g = _g2(tmp_path, SAVE_RUN, _report(3, observed), name=f"ep{i}")
        assert g == passed, observed
    # A report that names two texts as missing is located by the screen that lacks one.
    g = _g2(tmp_path, SAVE_RUN, _report(3, "", expected="Milk"), name="one")
    assert g == passed
    # The screen on that step still shows the text: nothing is missing there. Unknown,
    # never a fail, and never a pass.
    kept = [*SAVE_RUN[:3], _tap("Save", "Notes", "Milk", "New note")]
    g = _g2(tmp_path, kept, _report(3, ""), name="kept")
    assert g["pass"] is None and g["unknown"] is True and "missing text" in g["reason"]
    # A tap's bare acknowledgement is not a screen, and the start screen is not a
    # screen the agent's action left.
    bare = [*SAVE_RUN[:3], Call("mobile_tap", {"element_text": "Save"}, "Tapped Save")]
    assert _g2(tmp_path, bare, _report(3, ""), name="bare")["unknown"] is True
    idle = [Say("Step 3: saving."), Call("mobile_observe_screen", {}, _screen(*HOME))]
    assert _g2(tmp_path, idle, _report(3, ""), name="idle")["unknown"] is True


# ── the blind agent (QUA-3607) ─────────────────────────────────────────────────

def test_the_texts_that_tell_a_steps_screens_from_the_start_screen():
    case = rq.load_case_truth(APP, "notes-save")
    assert rq.start_screen_texts(case) == set(_HOME)

    def texts(step, *versions):
        return sorted(p.pattern.split(")", 1)[1].rsplit("(", 1)[0].replace("\\", "")
                      for p in rq.step_screen_texts(case, step, *versions))
    # Step 3 is Save. The editor (typed into at step 2) is still up when it starts, and
    # the list comes after. `Notes`, `New note` and `Settings` are the start screen's.
    assert texts(3) == ["milk", "new ntoe", "no notes", "save", "title"]
    # Step 1 opens the editor. `New note` is its clean title AND a start-screen button.
    assert texts(1) == ["new ntoe", "save", "title"]
    about = rq.load_case_truth(APP, "notes-about")
    # Clean pass only: the launcher (seeded pass, after the death) is nobody's step.
    assert [p.search("wed, sep 16 chrome") for p in
            rq.step_screen_texts(about, 2, ("clean",))] == [None, None]
    # Too short to tell screens apart, or a number a launcher's clock shows too.
    assert not rq._tells_screens_apart("Wed") and not rq._tells_screens_apart("10:00")
    assert rq._tells_screens_apart("Save") and rq._tells_screens_apart("$ 75.00")


@pytest.mark.parametrize("fmt", sorted(FORMATS))
@pytest.mark.parametrize("act", ["tap", "type", "home"])
def test_a_blind_action_after_saying_the_step_does_not_locate_a_crash(tmp_path, fmt, act):
    """Says "Step 2:", does one thing, reads a screen. The action is the agent's own
    and the device did show the quote after it, but no screen it read is one of step
    2's as the truth recorded them: the tap could have been anywhere."""
    listing = _screen(*((t, 10 * i, 50) for i, t in enumerate(HOME)))
    blind = {"tap": Call("mobile_tap", {"x": 10, "y": 50}, listing),
             "type": Call("mobile_type_text", {"text": "About"}, "Set focused field to: 'About'"),
             "home": Call("mobile_press_button", {"button": "HOME"}, "Pressed HOME")}[act]
    shown = _screen(*_LAUNCHER) if act == "home" else listing
    run = [Call("mobile_observe_screen", {}, listing), Say("Step 2: tapping About."), blind,
           Call("mobile_observe_screen", {}, shown)]
    quote = "Chrome" if act == "home" else "Notes"
    g = _g2(tmp_path, run, _report(2, quote, matched="about-crash", expected="the version"),
            case="notes-about", fmt=fmt)
    assert g["pass"] is None and g["unknown"] is True, g
    assert "nothing shows the agent had reached the step" in g["reason"]


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_a_screen_with_none_of_the_steps_texts_does_not_show_that_text_is_missing(
        tmp_path, corpus, fmt):
    """`Milk` is missing from every screen that is not the list after Save. A blind tap
    after "Step 3:" and a read of the start screen (or of a listing with nothing on it)
    lacks `Milk` too, and says nothing about the defect."""
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"] = [{"step": 5, "added": [], "removed": ["Milk"]}]
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    for i, screen in enumerate((_screen(*HOME), _screen("Lorem ipsum dolor"))):
        run = [Say("Step 3: saving."), Call("mobile_tap", {"x": 900, "y": 900}, screen),
               Call("mobile_observe_screen", {}, screen)]
        g = _g2(tmp_path, run, _report(3, "", expected="Milk is in the list"), fmt=fmt,
                name=f"ep{i}")
        assert g["pass"] is None and g["unknown"] is True, g
        assert "it may be any screen" in g["reason"]
    # Nor does the screen the step STARTS on: the editor, before Save, has no `Milk`
    # in a list either. The agent did steps 1 and 2 and stopped.
    stopped = [*SAVE_RUN[:2], Say("Step 3: saving."),
               Call("mobile_observe_screen", {}, _screen(*EDITOR))]
    g = _g2(tmp_path, stopped, _report(3, "", expected="Milk is in the list"), fmt=fmt,
            name="stopped")
    assert g["pass"] is None and g["unknown"] is True, g


def test_missing_text_on_a_screen_that_is_the_start_screen_again(tmp_path, corpus):
    """After Save the seeded build is back on the start screen, less `Milk`: no text of
    that screen tells it from the start screen. It is the truth's screen when it holds
    that screen's texts AND the agent read one of the step's own screens (the editor)
    on the step; a blind tap and a read of the start screen has only the first."""
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"] = [{"step": 5, "added": [], "removed": ["Milk"]}]
    truth["notes-save"]["screens"] = {**_SAVE_SCREENS, "seeded": [
        *_SAVE_SCREENS["seeded"][:4], _HOME, _HOME]}
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    case = rq.load_case_truth(APP, "notes-save")
    assert rq._never_located(case, "save-drops-note") is None
    report = _report(3, "", expected="Milk is in the list")
    # It typed the title, read the editor (still up when step 3 starts), then saved.
    there = [*SAVE_RUN[:3], Call("mobile_observe_screen", {}, _screen(*EDITOR, "Milk")),
             _tap("Save", *HOME)]
    assert _g2(tmp_path, there, report)["pass"] is True
    blind = [Say("Step 3: saving."), Call("mobile_tap", {"x": 900, "y": 900}, _screen(*HOME)),
             Call("mobile_observe_screen", {}, _screen(*HOME))]
    g = _g2(tmp_path, blind, report, name="blind")
    assert g["pass"] is None and g["unknown"] is True


def test_a_quote_the_clean_start_screen_shows_too_needs_the_steps_screen(tmp_path, corpus):
    """The screen diff can carry a text that is on the start screen of both builds (it
    differs at one step, not everywhere). Read off the start screen after a blind tap,
    it locates nothing; on the step's own screen it does."""
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"] = [
        {"step": 5, "added": ["New note"], "removed": ["Milk"]}]
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    blind = [Say("Step 3: saving."), Call("mobile_tap", {"x": 900, "y": 900}, _screen(*HOME)),
             Call("mobile_observe_screen", {}, _screen(*HOME))]
    g = _g2(tmp_path, blind, _report(3, "New note", expected=""))
    assert g["pass"] is None and g["unknown"] is True
    assert "the screen the case starts on shows it too" in g["reason"]
    there = [*SAVE_RUN[:3], _tap("Save", "Notes", "No notes", "New note")]
    assert _g2(tmp_path, there, _report(3, "New note", expected=""), name="ep2")["pass"] is True
    # A text only the SEEDED build's start screen shows is the defect itself, wherever
    # the agent read it: no step screen is asked for.
    truth["notes-save"]["screens"] = {**_SAVE_SCREENS, "seeded": [
        ["Notse", "New note", "Settings"], *_SAVE_SCREENS["seeded"][1:]]}
    truth["notes-save"]["side"] = [{"bug": "title-typo", "marker": "Notse",
                                    "visible_steps": [1], "texts": ["Notse"]}]
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    start = [Call("mobile_observe_screen", {}, _screen("Notse", "New note", "Settings"))]
    g = _g2(tmp_path, start, _report(1, "Notse", matched="title-typo"), name="ep3")
    assert g["pass"] is True


def test_which_defects_no_report_can_be_located_for(tmp_path, corpus):
    case = rq.load_case_truth(APP, "notes-save")
    assert rq._never_located(case, "save-drops-note") is None
    assert rq._never_located(case, "editor-title-typo") is None
    assert rq._never_located(case, "nobody") == rq.NOT_IN_TRUTH
    assert rq._never_located("no truth file", "save-drops-note") == rq.NO_TRUTH
    truth = json.loads(json.dumps(TRUTH))
    truth["notes-save"]["unclaimed_diff"] = []          # seen only in stored data
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    case = rq.load_case_truth(APP, "notes-save")
    assert rq._never_located(case, "save-drops-note") == rq.NO_SCREEN_STEP
    # It only removes text, and the one screen without the text is the start screen
    # again: nothing the truth recorded can settle a report.
    truth["notes-save"]["unclaimed_diff"] = [{"step": 5, "added": [], "removed": ["Milk"]}]
    truth["notes-save"]["screens"] = {v: [_HOME] * 6 for v in ("clean", "seeded")}
    (corpus / "truth" / f"journey-{APP}.json").write_text(json.dumps(truth))
    case = rq.load_case_truth(APP, "notes-save")
    assert rq._never_located(case, "save-drops-note") == rq.NO_STEP_SCREEN
    d = _saved(tmp_path, SAVE_RUN, [_report(3, "")])
    result = json.loads((d / "result.json").read_text())
    result["metrics"]["bugs_present"] = ["save-drops-note", "editor-title-typo"]
    (d / "result.json").write_text(json.dumps(result))
    assert rq.score_episode(d)["defects"] == {
        "save-drops-note": {"coverable": False, "located_never": "no_step_screen",
                            "replay_excluded": None},
        "editor-title-typo": {"coverable": True, "located_never": None,
                              "replay_excluded": None}}


def test_missing_text_named_in_expected_counts_where_the_defect_also_shows_text(tmp_path):
    """`save-drops-note` shows `No notes` and removes `Milk`. A report that quotes
    nothing and names `Milk` as expected is located by the screen without it."""
    g = _g2(tmp_path, SAVE_RUN, _report(3, ""))
    assert g["pass"] is True and "missing text" in g["reason"]
    # A quote the device showed only on another step is still judged as a quote.
    g = _g2(tmp_path, SAVE_RUN, _report(3, "New ntoe"), name="ep2")
    assert g["pass"] is False and "not at step 3" in g["reason"]


def test_a_date_slot_in_a_truth_text_matches_the_date_on_the_screen():
    slot = rq._text_re("Thursday, <date>. 127 days left")
    assert slot.search("it still shows thursday, january 21, 2027. 127 days left")
    assert not slot.search("saturday, january 23, 2027. 129 days left")
    assert rq._text_re("<date>") is None and rq._text_re("A") is None
    assert rq._text_re("2h").search("reads 2h") and not rq._text_re("2h").search("12h")


def test_the_truth_texts_are_the_reference_reports(monkeypatch):
    """`symptom_texts` against `reference_reports.truth_texts`, which reads the same
    truth through the scorer's own evidence builder, over the public corpus. The scorer
    splits what a blocking bug shows into texts only the seeded build has and texts the
    brief also hands the agent; G2 asks the device for either, so it takes both."""
    from qualgentbench import corpus as corpus_mod
    from qualgentbench import reference_reports

    monkeypatch.delenv(rq.HELDOUT_ENV, raising=False)
    assert rq._CRASH_DIALOG_TEXTS == journey._CRASH_DIALOG_TEXTS
    assert rq._ANR_DIALOG_TEXTS == journey._ANR_DIALOG_TEXTS
    assert rq._LIVENESS_KEYS == journey._LIVENESS_KEYS
    checked = 0
    for app_id in corpus_mod.public_apps():
        doc = journey.load_cases(app_id) or {}
        defects, truth = journey.load_defects(doc), journey.load_truth(app_id)
        for case in doc.get("test_cases") or []:
            mine = rq.load_case_truth(app_id, str(case["id"]))
            for bug in journey.case_bugs(case):
                theirs = reference_reports.truth_texts(case, defects, truth.get(case["id"]),
                                                       bug["id"])
                texts = rq.symptom_texts(mine, bug["id"])
                where = (case["id"], bug["id"], theirs["kind"])
                if theirs["kind"] in ("side", "screen", "death"):
                    assert set(theirs["actual"]) <= set(texts.shown), where
                    checked += 1
                if theirs["kind"] in ("screen", "absence"):
                    # A bare `<date>` slot is no text to look for.
                    assert {t for t in theirs["expected"] if rq._text_re(t)} <= set(
                        texts.lost), where
                if theirs["kind"] == "side":
                    assert set(texts.shown) == set(theirs["actual"]), where
    assert checked > 100


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
    announcing a step, before acting there, is surely the screen the last action left
    and only possibly the announced step's."""
    run = [Say("Step 1: start a new note."),
           Call("mobile_tap", {"x": 1, "y": 2}, "Tapped (1, 2)"),
           Call("mobile_observe_screen", {}, _screen(*EDITOR)),
           Say("Editor is open. Step 3: save."),
           Call("mobile_observe_screen", {}, _screen(*EDITOR)),
           Call("mobile_tap", {"x": 5, "y": 6}, "Tapped (5, 6)"),
           Call("mobile_observe_screen", {}, _screen("Notes", "No notes"))]
    assert _labels(run) == [(1, 2, 2), (1, 2, 2), (1, 2, 4), (3, 4, 4), (3, 4, 4)]
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
    assert _g2(tmp_path, run, _report(3, "Draft kept", expected=""))["unknown"] is True
    g = _g2(tmp_path, run, _report(1, "New ntoe / Draft kept", matched="editor-title-typo"),
            name="ep2")
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
