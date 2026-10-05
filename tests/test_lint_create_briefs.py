"""`scripts/lint_create_briefs.py`: the neutrality gate on CreateBench v2 creation briefs
(QUA-2853). A brief says what a feature should do — never how to test it, never that
anything could be wrong. The real corpus must pass, the positive-control subset must be
canary-covered, and each rule is exercised on its own here, with the "later visit"
regression that August's private gate missed (QUA-2614) pinned explicitly."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "scripts" / "lint_create_briefs.py"
_spec = importlib.util.spec_from_file_location("lint_create_briefs", _PATH)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


DEFECTS = [
    {"id": "fav-lost", "kind": "functional", "class": "persistence", "tier": "L1",
     "symptoms": ["favorite", "not starred", "star missing"]},
    {"id": "count-low", "kind": "display", "class": "content-format", "tier": "L2",
     "marker": "2 cards shown", "symptoms": ["cards shown", "one short"]},
    {"id": "open-crash", "kind": "functional", "class": "crash", "tier": "L4",
     "symptoms": ["crashed", "home screen"]},
]

GOOD = ("Contacts can be marked as favorites from a contact's own screen, and the "
        "Favorites tab lists exactly the contacts marked that way.")


def _case(brief=None, **kw) -> dict:
    base = {
        "id": "case-1", "name": "Add a contact to favorites",
        "brief": {"title": "Favorite contacts", "intended_behavior": GOOD}
        if brief is None else brief,
        "steps": ["Tap the + button, enter the first name \"Alice\" and save.",
                  "Long-press \"Alice\" in the list to select it.",
                  "Open the Favorites tab."],
        "expected_outcome": "Alice is listed on the Favorites tab.",
        "check": {"steps": ["launch", {"tap": "Create new contact"}, {"type": "Alice"},
                            {"tap": "Save"}, {"long_press": "Alice"}, {"tap": "Add to favorites"}],
                  "expect": {"content": "content://com.android.contacts/contacts",
                             "contains": "starred=1"}},
        "bugs": ["fav-lost"],
    }
    base.update(kw)
    return base


def _doc(*cases) -> dict:
    return {"app": "app", "defects": DEFECTS, "test_cases": list(cases) or [_case()]}


def _ib(text: str) -> dict:
    return _case(brief={"title": "Favorite contacts", "intended_behavior": text})


def _rules(findings, rule, level="error"):
    return [f for f in findings if f.rule == rule and f.level == level]


def _errors(doc) -> list:
    return [f for f in lint.lint_doc(doc) if f.level == "error"]


# ── the real corpus ────────────────────────────────────────────────────────────

def test_the_real_corpus_has_a_neutral_brief_on_every_public_case():
    from qualgentbench import journey
    results = lint.lint_corpus()
    public = sorted(p.stem for p in journey._CASES_DIR.glob("*.yaml"))
    assert sorted(results) == public and len(public) >= 6
    errors = [str(f) for fs in results.values() for f in fs if f.level == "error"]
    assert errors == [], "\n".join(errors)
    n = 0
    for app in public:
        for case in journey.load_cases(app)["test_cases"]:
            assert lint.brief_of(case), case["id"]
            n += 1
    assert n == 41, n


def test_the_real_positive_control_subset_is_valid_and_canary_covered():
    from qualgentbench import corpus, journey
    subset = lint.load_subset()
    assert subset and subset["size"] == 12 and len(subset["briefs"]) == 12
    docs = {a: journey.load_cases(a) for a in corpus.public_apps()}
    canaries = {a: lint.spec_canaries(a) for a in docs}
    # No error; `info` lines (live coverage beyond the frozen pool) are allowed.
    assert [f for f in lint.lint_subset(subset, docs, canaries) if f.level != "info"] == []
    apps, classes = lint.subset_spread(subset)
    assert set(apps) == set(corpus.public_apps())
    assert {"crash", "navigation", "ordering", "anr", "stuck", "persistence"} <= set(classes)
    # QUA-2862: 8 assert + 4 walk, every entry direct, the conditional briefs gone.
    assert lint.subset_detection(subset) == {"assert": 8, "walk": 4}
    assert {e["reach"] for e in subset["briefs"]} == {"direct"}
    cases = {e["case"] for e in subset["briefs"]}
    assert not cases & {"contacts-view-details", "orgzly-new-note-survives-rotation"}


def test_a_brief_never_reaches_the_journey_agent():
    """The journey agent's task carries name/steps/outcome; the creation brief must not
    ride along in `bug_spec` (whatever reaches the task can reach a transcript)."""
    from qualgentbench import corpus, journey
    for app in corpus.public_apps():
        briefs = [lint.brief_of(c)["intended_behavior"]
                  for c in journey.load_cases(app)["test_cases"]]
        for t in journey.journey_tasks({"app": {"id": app}}):
            blob = json.dumps(t.bug_spec, default=str)
            assert not any(b[:40] in blob for b in briefs), t.task_id


def test_the_main_entry_point_passes_on_the_real_corpus(capsys):
    assert lint.main(["--quiet-warnings"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "positive-control.yaml: 12 briefs" in out
    assert "by detection: assert 8, walk 4" in out
    # QUA-2864's re-run subset is gated by the same rules, plus its rule's DROP classes.
    assert "positive-control-v2.yaml: 8 briefs (rule app-open/v2)" in out
    assert "by detection: assert 4, walk 4" in out


def test_a_rule_subset_refuses_a_drop_entry_its_rule_cannot_provably_catch():
    """QUA-2864: under app-open/v2 only a persistence target is provably uncatchable; a
    navigation DROP entry can keep its power under full uptake."""
    subset = lint.load_subset(lint._SUBSET_V2_PATH)
    assert lint._rule_findings(subset) == []
    nav = {"case": "medtimer-check-stock", "class": "navigation", "detection": "assert"}
    walk = {"case": "anki-study-first-card", "class": "crash", "detection": "walk"}
    found = lint._rule_findings({**subset, "briefs": [*subset["briefs"], nav, walk]})
    assert [(f.level, f.case) for f in found] == [("error", "medtimer-check-stock")]
    assert "cannot provably remove its power" in found[0].detail
    assert lint._rule_findings({**subset, "rule": "no-such-rule"})[0].level == "error"
    assert lint._rule_findings({k: v for k, v in subset.items() if k != "rule"}) == []


# ── shape ──────────────────────────────────────────────────────────────────────

def test_a_public_case_without_a_brief_is_an_error_and_a_heldout_one_a_warning():
    case = _case()
    del case["brief"]
    assert _rules(lint.lint_doc(_doc(case)), "shape")
    held = lint.lint_doc(_doc(case), heldout=True)
    assert not _rules(held, "shape") and _rules(held, "shape", "warning")


@pytest.mark.parametrize("brief", [
    "just a string",
    {"title": "Favorites"},
    {"title": "", "intended_behavior": GOOD},
    {"title": "Favorites", "intended_behavior": GOOD, "hint": "check after restart"},
])
def test_a_malformed_brief_is_an_error(brief):
    assert _rules(lint.lint_doc(_doc(_case(brief=brief))), "shape")


def test_a_neutral_brief_passes_with_only_the_expected_single_word_symptom_warning():
    found = lint.lint_doc(_doc())
    assert [f for f in found if f.level == "error"] == []
    # "favorites" is a single-word symptom of the case's own bug: a persistence brief
    # has to name its feature, so it is a warning, never an error.
    assert [f.rule for f in found] == ["defect"]


# ── procedure hints — including the August regression ─────────────────────────

@pytest.mark.parametrize("text", [
    # QUA-2614: the construction August's gate missed on the load-bearing persistence
    # brief, and the second brief the fix caught with the same shape.
    "A contact marked as a favorite shows its star on any later visit to the contact.",
    "The star is shown on every later visit to the contact's screen.",
    "The favorite star is shown on subsequent visits to the contact.",
    "The favorite star is shown on any visit to the contact.",
    "The star is shown the next time the contact is viewed.",
    "The star is kept when the user re-opens the contact.",
    "The star is kept when the user reopens the app.",
    "The star shows after relaunching the application.",
    "Favorites are kept after the app restarts.",
    "Favorites persist between sessions.",
    "Favorites survive a process death.",
    "The contact is still a favorite afterwards.",
    "When the user comes back to the list the favorite is shown.",
    "Favorites are shown when the user returns to the list.",
    "Favorites are kept when the phone is rotated to landscape.",
    "Favorites are kept across orientation changes.",
    "Favorites are kept after the app is force-stopped.",
    "Tapping the star adds the contact to favorites.",
    "Users can verify that the contact is a favorite.",
    "The favorites tab can be checked to confirm the change.",
    "The star is set, then the favorites tab lists the contact.",
])
def test_procedure_hints_are_errors(text):
    assert _rules(lint.lint_doc(_doc(_ib(text))), "procedure"), text


@pytest.mark.parametrize("text", [
    "Open a contact and mark it as a favorite. The Favorites tab lists it.",
    "Select a contact. Add it to favorites.",
    "1. Mark a contact as a favorite 2. Open the Favorites tab",
    "- mark a contact as a favorite\n- open the Favorites tab",
])
def test_step_lists_and_imperative_sentences_are_errors(text):
    assert _rules(lint.lint_doc(_doc(_ib(text))), "procedure"), text


# ── failure language ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Marking a favorite works without crashing.",
    "A favorite should not disappear from the Favorites tab.",
    "The Favorites tab lists favorites correctly.",
    "The Favorites tab never drops a contact.",
    "The Favorites tab shows the favorites instead of an empty screen.",
    "Marking a favorite raises no error.",
    "The Favorites tab lists favorites, including the edge case of a contact with no name.",
    "A favorite is no longer lost when the app is busy.",
])
def test_failure_and_edge_case_language_are_errors(text):
    assert _rules(lint.lint_doc(_doc(_ib(text))), "failure"), text


# ── defect vocabulary ──────────────────────────────────────────────────────────

def test_a_defect_id_of_the_app_is_an_error_in_either_spelling():
    for text in ("The fav-lost behaviour is out of scope here.", "The count low header is clear."):
        assert _rules(lint.lint_doc(_doc(_ib(text + " " + GOOD))), "defect"), text


def test_any_defect_marker_of_the_app_is_an_error_even_on_another_case():
    # `count-low` is not this case's bug, but its marker names a defect of the app.
    found = lint.lint_doc(_doc(_ib(GOOD + " The browser header reads 2 cards shown.")))
    assert [f for f in _rules(found, "defect") if "marker" in f.detail]


def test_a_multi_word_symptom_of_the_case_s_own_bug_is_an_error():
    found = lint.lint_doc(_doc(_ib(GOOD + " A contact with the star missing is unusual.")))
    assert [f for f in _rules(found, "defect") if "star missing" in f.detail]


def test_another_defect_s_symptom_phrase_is_not_this_case_s_leak():
    # "home screen" belongs to `open-crash`, which this case does not seed.
    found = lint.lint_doc(_doc(_ib(GOOD + " Favorites also appear on the home screen widget.")))
    assert not _rules(found, "defect")


# ── values: quotes, digits, check anchors, expected values ─────────────────────

def test_a_quote_mark_is_an_error():
    found = lint.lint_doc(_doc(_ib('The "Favorites" tab lists every contact marked as a favorite.')))
    assert [f for f in _rules(found, "value") if "quotes" in f.detail]


def test_an_apostrophe_is_not_a_quote():
    assert not _rules(lint.lint_doc(_doc(_ib(GOOD + " A contact's star is its marker."))), "value")


def test_a_digit_is_an_error():
    found = lint.lint_doc(_doc(_ib(GOOD + " Up to 3 favorites fit on the tab.")))
    assert [f for f in _rules(found, "value") if "'3'" in f.detail]


@pytest.mark.parametrize("extra, source", [
    ("A contact such as Alice can be marked.", "route type"),                 # typed + quoted
    ("The row reads starred.", None),                                          # not an anchor
])
def test_a_typed_route_value_is_an_error(extra, source):
    found = _rules(lint.lint_doc(_doc(_ib(GOOD + " " + extra))), "value")
    if source:
        assert [f for f in found if source in f.detail], found
    else:
        assert not found


def test_evidence_present_and_gate_strings_are_check_anchors():
    case = _ib(GOOD + " The details screen shows Mark completed and IndexOutOfBoundsException.")
    case["evidence"] = ["Mark completed"]
    case["check"]["expect"] = {"present": "Mark completed", "crash": "IndexOutOfBoundsException"}
    found = _rules(lint.lint_doc(_doc(case)), "value")
    # One finding per distinct value: `present:` and `evidence:` name the same string.
    assert [f for f in found if "'Mark completed'" in f.detail]
    assert [f for f in found if "expect.crash" in f.detail]
    case["check"]["expect"] = {"content": "content://x", "contains": "starred=1"}
    found = _rules(lint.lint_doc(_doc(case)), "value")
    assert [f for f in found if "evidence 'Mark completed'" in f.detail]


def test_db_literals_are_expected_values_but_sql_noise_is_not():
    case = _ib(GOOD + " A dose recorded as taken moves to the history.")
    case["check"]["expect"] = {"db": "x", "equals": "1",
                               "query": "select count(*) from e where status='TAKEN' "
                                        "and date(t,'unixepoch','localtime')=date('now','localtime');"}
    found = _rules(lint.lint_doc(_doc(case)), "value")
    assert [f for f in found if "'TAKEN'" in f.detail]
    assert not [f for f in found if "localtime" in f.detail or "'now'" in f.detail]


def test_a_tap_anchor_with_a_digit_is_an_anchor_and_a_plain_ui_label_is_not():
    case = _ib(GOOD)
    case["check"]["steps"] = ["launch", {"tap": "Ibuprofen (4)"}, {"tap": "Save"},
                              {"tap": "Reminded", "row": "Aspirin row"}]
    values = [v for _, v in lint.case_values(case)]
    assert "Ibuprofen (4)" in values and "Aspirin row" in values and "Save" not in values
    case = _ib(GOOD + " The editor has a Save button.")
    case["check"]["steps"] = ["launch", {"tap": "Save"}]
    assert not _rules(lint.lint_doc(_doc(case)), "value")


def test_a_quoted_string_from_the_steps_is_an_expected_value():
    case = _ib(GOOD + " Whole-day events use the All-day switch.")
    case["steps"] = ['Switch on "All-day".']
    assert [f for f in _rules(lint.lint_doc(_doc(case)), "value") if "step 1 quote" in f.detail]


# ── copy ───────────────────────────────────────────────────────────────────────

def test_a_brief_that_paraphrases_the_outcome_is_an_error():
    case = _ib("Favorites are simple: the contact is listed on the Favorites tab once starred.")
    case["expected_outcome"] = "The contact is listed on the Favorites tab."
    assert _rules(lint.lint_doc(_doc(case)), "copy")


def test_a_shared_location_phrase_is_not_a_copy():
    case = _ib(GOOD + " Search sits in the bottom bar.")
    case["steps"] = ["Tap Search in the bottom bar."]
    assert not _rules(lint.lint_doc(_doc(case)), "copy")


# ── nouns and length (warnings) ────────────────────────────────────────────────

def test_a_fixture_entity_from_the_outcome_is_a_warning_and_a_route_label_is_not():
    case = _ib(GOOD + " Alice and Bob may both be favorites.")
    case["expected_outcome"] = "The list shows Bob and the Favorites tab."
    case["check"]["steps"] = ["launch", {"tap": "Favorites"}]
    found = lint.lint_doc(_doc(case))
    nouns = [f.detail for f in found if f.rule == "noun"]
    assert any("'Bob'" in d for d in nouns) and not any("'Favorites'" in d for d in nouns)
    assert all(f.level == "warning" for f in found if f.rule == "noun")


def test_length_is_a_warning():
    found = lint.lint_doc(_doc(_ib("Favorites exist.")))
    assert [f for f in found if f.rule == "length" and f.level == "warning"]


# ── the positive-control subset ────────────────────────────────────────────────

def _crash_case(**kw):
    """A FAIL-expected case whose check gates the crash its target causes."""
    c = _case(id="case-2", bugs=["open-crash"])
    c["check"] = {**c["check"], "expect": {**c["check"]["expect"], "crash": "IllegalState"}}
    c.update(kw)
    return c


def _subset_docs():
    doc = _doc(_case(), _crash_case())
    return {"app": doc}


#: Journey truth for the synthetic docs: the crash target's seeded arm dies, the
#: persistence target's stays alive.
TRUTHS = {"app": {"case-2": {"passes": {"seeded": {"outcome": "crashed"}}},
                  "case-1": {"passes": {"seeded": {"outcome": "violated"}}},
                  "case-3": {"passes": {"seeded": {"outcome": "violated"}}}}}


def _ls(subset, docs, canaries, truths=None):
    return lint.lint_subset(subset, docs, canaries, truths=TRUTHS if truths is None else truths)


def _entry(**kw):
    e = {"case": "case-2", "app": "app", "target": ["open-crash"], "class": "crash",
         "detection": "walk", "reach": "direct", "why": "on the route"}
    e.update(kw)
    return e


def test_canary_ids_reads_fired_calls():
    src = ('if (x) com.a.QgbFlags.fired("open-crash")\n'
           "QgbFlags.fired( 'fav-lost' )\nQgbFlags.on(\"count-low\")")
    assert lint.canary_ids(src) == {"open-crash", "fav-lost"}


def test_a_valid_subset_passes():
    assert _ls({"size": 1, "briefs": [_entry()]}, _subset_docs(),
                            {"app": {"open-crash"}}) == []


@pytest.mark.parametrize("entry, canaries, needle", [
    (_entry(), {"app": set()}, "no fired() canary"),
    (_entry(target=["fav-lost"]), {"app": {"fav-lost", "open-crash"}}, "!= the case's bugs"),
    (_entry(**{"class": "anr"}), {"app": {"open-crash"}}, "class"),
    (_entry(app="other"), {"app": {"open-crash"}}, "app"),
    (_entry(case="nope"), {"app": {"open-crash"}}, "not a public journey case"),
    (_entry(why=""), {"app": {"open-crash"}}, "rationale"),
    (_entry(reach="maybe"), {"app": {"open-crash"}}, "reach"),
])
def test_an_invalid_subset_entry_is_an_error(entry, canaries, needle):
    found = _ls({"size": 1, "briefs": [entry]}, _subset_docs(), canaries)
    assert [f for f in found if needle in f.detail], found


def test_subset_size_duplicates_and_spread_are_enforced():
    docs = _subset_docs()
    found = _ls({"size": 2, "briefs": [_entry()]}, docs, {"app": {"open-crash"}})
    assert [f for f in found if "size" in f.detail]
    found = _ls({"size": 2, "briefs": [_entry(), _entry()]}, docs,
                             {"app": {"open-crash"}})
    assert [f for f in found if "listed twice" in f.detail]
    # A canary-covered class with no entry is a spread error.
    found = _ls({"size": 1, "briefs": [_entry()]}, docs,
                             {"app": {"open-crash", "fav-lost"}})
    assert [f for f in found if "class persistence" in f.detail]
    found = _ls({"size": 1, "max_per_app": 0, "briefs": [_entry()]}, docs,
                             {"app": {"open-crash"}})
    assert [f for f in found if "max_per_app" in f.detail]


def test_a_display_defect_never_enters_the_spread_pool():
    """Rule 2 makes every entry a functional, FAIL-expected target, so a canary-covered
    DISPLAY defect (content-format) can never be an entry and must not be demanded
    (QUA-2860 gave the corpus's display defects canaries and tripped exactly this)."""
    docs = {"app": _doc(_crash_case(), _case(id="case-3", bugs=["count-low"]))}
    canaries = {"app": {"open-crash", "count-low"}}
    assert lint.spread_pool(docs, canaries) == ({"app"}, {"crash"})
    assert _ls({"size": 1, "briefs": [_entry()]}, docs, canaries) == []


def test_a_frozen_spread_pool_turns_later_coverage_into_info():
    """`spread_pool:` freezes the pool at pre-registration: a class that gains a canary
    later (persistence here) is an INFO line, not an error; a class IN the frozen pool
    with no entry is still an error."""
    docs = _subset_docs()
    canaries = {"app": {"open-crash", "fav-lost"}}
    frozen = {"apps": ["app"], "classes": ["crash"]}
    found = _ls({"size": 1, "spread_pool": frozen, "briefs": [_entry()]},
                             docs, canaries)
    assert [f.level for f in found] == ["info"], found
    assert "class persistence is now canary-covered" in found[0].detail
    found = _ls({"size": 1, "spread_pool": {"apps": ["app"],
                                                        "classes": ["crash", "anr"]},
                              "briefs": [_entry()]}, docs, {"app": {"open-crash"}})
    assert [f for f in found if f.level == "error" and "class anr" in f.detail], found


def test_the_real_subset_freezes_its_pool_and_leaves_lifecycle_out_on_purpose():
    from qualgentbench import corpus, journey
    subset = lint.load_subset()
    assert set(subset["spread_pool"]["classes"]) == {"crash", "navigation", "ordering",
                                                     "anr", "stuck", "persistence"}
    assert set(subset["spread_pool"]["excluded"]) == {"lifecycle"}
    docs = {a: journey.load_cases(a) for a in corpus.public_apps()}
    canaries = {a: lint.spec_canaries(a) for a in docs}
    found = lint.lint_subset(subset, docs, canaries)
    assert all(f.level == "info" for f in found), found
    assert [f for f in found if "class lifecycle left out on purpose" in f.detail]


# ── the detection label (QUA-2862) ────────────────────────────────────────────

def test_a_missing_or_unknown_detection_label_is_an_error():
    e = _entry()
    del e["detection"]
    found = _ls({"size": 1, "briefs": [e]}, _subset_docs(), {"app": {"open-crash"}})
    assert [f for f in found if f.level == "error" and "no `detection:` label" in f.detail]
    found = _ls({"size": 1, "briefs": [_entry(detection="maybe")]}, _subset_docs(),
                {"app": {"open-crash"}})
    assert [f for f in found if "is not assert|walk" in f.detail]


def test_a_detection_label_that_disagrees_with_the_derivation_is_an_error():
    found = _ls({"size": 1, "briefs": [_entry(detection="assert")]}, _subset_docs(),
                {"app": {"open-crash"}})
    assert [f for f in found if "detection 'assert' != derived 'walk'" in f.detail], found
    # The label comes from metadata, so a truth row that contradicts the class makes the
    # label underivable: the seeded arm of a crash target that stayed alive.
    alive = {"app": {"case-2": {"passes": {"seeded": {"outcome": "violated"}}}}}
    found = _ls({"size": 1, "briefs": [_entry()]}, _subset_docs(), {"app": {"open-crash"}},
                truths=alive)
    assert [f for f in found if "cannot be derived" in f.detail
            and "seeded arm did not die" in f.detail], found
    found = _ls({"size": 1, "briefs": [_entry()]}, _subset_docs(), {"app": {"open-crash"}},
                truths={"app": {}})
    assert [f for f in found if "no journey truth" in f.detail], found


def test_the_detection_mix_is_enforced():
    docs = _subset_docs()
    ok = _ls({"size": 1, "detection_mix": {"walk": 1}, "briefs": [_entry()]}, docs,
             {"app": {"open-crash"}})
    assert ok == []
    bad = _ls({"size": 1, "detection_mix": {"assert": 1}, "briefs": [_entry()]}, docs,
              {"app": {"open-crash"}})
    assert [f for f in bad if "detection mix" in f.detail], bad


def test_an_excluded_spread_class_needs_a_reason_and_is_not_demanded():
    docs = _subset_docs()
    canaries = {"app": {"open-crash", "fav-lost"}}
    pool = {"apps": ["app"], "classes": ["crash"], "excluded": {"persistence": "on purpose"}}
    found = _ls({"size": 1, "spread_pool": pool, "briefs": [_entry()]}, docs, canaries)
    assert [f.level for f in found] == ["info"] and "left out on purpose" in found[0].detail
    pool = {"apps": ["app"], "classes": ["crash"], "excluded": {"persistence": " "}}
    found = _ls({"size": 1, "spread_pool": pool, "briefs": [_entry()]}, docs, canaries)
    assert [f for f in found if f.level == "error" and "no reason" in f.detail]
    pool = {"apps": ["app"], "classes": ["crash"], "excluded": {"crash": "why"}}
    found = _ls({"size": 1, "spread_pool": pool, "briefs": [_entry()]}, docs, canaries)
    assert [f for f in found if "both in spread_pool.classes and excluded" in f.detail]
