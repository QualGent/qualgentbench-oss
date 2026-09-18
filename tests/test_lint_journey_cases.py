"""`scripts/lint_journey_cases.py`: the device-free gate on the journey corpus text.

A completion witness (`present:`/`absent:`/`evidence:`) that carries a seeded defect's
marker or symptom scores an agent for having been told where the bug is; a brief that
states the marker does the same; a case with no `check.expect` can never be confirmed.
The real corpus must lint clean, and each rule is exercised on its own here."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[1] / "scripts" / "lint_journey_cases.py"
_spec = importlib.util.spec_from_file_location("lint_journey_cases", _PATH)
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


DEFECTS = [
    {"id": "avg-bug", "kind": "display", "class": "content-format", "tier": "L2", "marker": "Avg:",
     "symptoms": ["average", "avg"]},
    {"id": "count-bug", "kind": "display", "class": "content-format", "tier": "L2",
     "marker": "2 cards shown", "symptoms": ["cards shown", "count"]},
    {"id": "delete-bug", "kind": "functional", "class": "persistence", "tier": "L1",
     "symptoms": ["still listed", "not deleted"]},
]


def _doc(**case) -> dict:
    base = {
        "id": "case-1", "name": "Read the statistics",
        "steps": ["Open Statistics.", "Read the Weight card."],
        "expected_outcome": "The Weight card shows a maximum of 85 kg.",
        "check": {"steps": ["launch"], "expect": {"present": "Max: 85 kg"}},
        "bugs": ["avg-bug"],
    }
    base.update(case)
    return {"app": "app", "defects": DEFECTS, "test_cases": [base]}


def _levels(findings, rule):
    return [f for f in findings if f.rule == rule]


# ── the real corpus ────────────────────────────────────────────────────────────

def test_the_real_corpus_has_no_errors():
    from qualgentbench import journey
    results = lint.lint_corpus()
    public = sorted(p.stem for p in journey._CASES_DIR.glob("*.yaml"))
    # Every PUBLIC app is linted; two more live in the held-out split, not in the repo.
    assert sorted(results) == public and len(public) >= 6, sorted(results)
    errors = [str(f) for fs in results.values() for f in fs if f.level == "error"]
    assert errors == [], "\n".join(errors)


def test_every_real_case_has_an_oracle_and_the_lint_saw_them_all():
    from qualgentbench import journey
    n = 0
    for path in sorted(journey._CASES_DIR.glob("*.yaml")):
        doc = journey.load_cases(path.stem)
        for case in doc["test_cases"]:
            n += 1
            assert not lint.rule_no_oracle(case), case["id"]
    apps = len(list(journey._CASES_DIR.glob("*.yaml")))
    assert apps >= 6 and n >= 5 * apps, (apps, n)


def test_read_only_cases_carry_a_screen_witness_that_is_not_a_defect_string():
    """Every `present:`/`absent:` case declares `evidence:` (the screen witness), and no
    witness overlaps a display text derive_journey.py measured for that case."""
    from qualgentbench import journey
    for path in sorted(journey._CASES_DIR.glob("*.yaml")):
        doc = journey.load_cases(path.stem)
        truth = journey.load_truth(path.stem)
        defects = journey.load_defects(doc)
        for case in doc["test_cases"]:
            if journey._oracle(case)["mode"] in ("present", "absent"):
                assert case.get("evidence"), f"{case['id']}: screen-text oracle without a witness"
            assert not lint.rule_leak(case, defects, truth), case["id"]


def test_a_content_oracle_s_absent_flag_is_not_a_screen_string():
    """contacts-delete: `{content: ..., contains: display_name=Alice, absent: true}` —
    the `absent` there is a flag, never text to lint against the defect vocabulary."""
    doc = _doc(check={"steps": ["launch"], "expect": {"content": "content://x", "contains": "a=1", "absent": True}},
               evidence=[])
    assert lint._witness_strings(doc["test_cases"][0]) == []
    assert not _levels(lint.lint_doc(doc), "leak")


# ── leak: witness strings against marker + symptoms ────────────────────────────

def test_a_present_string_that_is_the_marker_is_a_leak():
    doc = _doc(check={"steps": ["launch"], "expect": {"present": "Avg: 76 kg"}})
    found = _levels(lint.lint_doc(doc), "leak")
    assert found and found[0].level == "error"
    assert "avg-bug" in found[0].detail and "marker" in found[0].detail


def test_an_evidence_string_containing_a_symptom_phrase_is_a_leak():
    doc = _doc(evidence=["Max: 85 kg", "3 cards shown"], bugs=["count-bug"])
    found = _levels(lint.lint_doc(doc), "leak")
    assert [f for f in found if "symptom 'cards shown'" in f.detail]


def test_leak_matching_is_case_and_whitespace_insensitive():
    doc = _doc(evidence=["AVG:   76"])
    assert _levels(lint.lint_doc(doc), "leak")


def test_a_witness_that_matches_a_measured_display_text_is_a_leak():
    truth = {"case-1": {"side": [{"bug": "avg-bug", "texts": ["Avg: 76 kg", "Avg: 79 kg"]}]}}
    doc = _doc(evidence=["Avg: 79 kg"])
    found = _levels(lint.lint_doc(doc, truth), "leak")
    assert [f for f in found if "measured display text" in f.detail]
    # A measured text that is not the marker is still a leak (the arm-only string is
    # what identifies the defect, whatever the author wrote as `marker:`).
    truth2 = {"case-1": {"side": [{"bug": "avg-bug", "texts": ["79 kilos"]}]}}
    found = _levels(lint.lint_doc(_doc(evidence=["79 kilos"]), truth2), "leak")
    assert len(found) == 1 and "measured display text" in found[0].detail
    # The same witness is fine when the truth measured nothing for it.
    assert not _levels(lint.lint_doc(_doc(evidence=["Min: 74 kg"]), truth), "leak")


def test_only_the_case_s_own_bugs_count_for_a_leak():
    # `count-bug` is in the file but not on this case: its symptom in the witness is fine.
    doc = _doc(evidence=["3 cards shown"], bugs=["avg-bug"])
    assert not _levels(lint.lint_doc(doc), "leak")


def test_a_per_case_marker_override_is_checked():
    doc = _doc(evidence=["Difference $ -30.00"], bugs=[{"id": "avg-bug", "marker": "-30.00"}])
    found = _levels(lint.lint_doc(doc), "leak")
    assert found and "'-30.00'" in found[0].detail


# ── brief: a marker stated in the agent-facing text ────────────────────────────

def test_a_brief_that_states_the_marker_is_flagged():
    doc = _doc(expected_outcome="The browser subtitle reads 2 cards shown.", bugs=["count-bug"])
    found = _levels(lint.lint_doc(doc), "brief")
    assert found and found[0].level == "error" and "expected_outcome" in found[0].detail


def test_a_step_that_states_the_marker_is_flagged_and_a_clean_brief_is_not():
    doc = _doc(steps=["Open the browser.", "Check that it says 2 cards shown."], bugs=["count-bug"])
    assert [f for f in _levels(lint.lint_doc(doc), "brief") if "step 2" in f.detail]
    assert not _levels(lint.lint_doc(_doc(bugs=["count-bug"])), "brief")


def test_brief_markers_match_on_token_boundaries():
    """A one-character marker (`1`) inside `10` is not the marker stated."""
    defects = DEFECTS + [{"id": "chip", "kind": "display", "class": "content-format", "marker": "1",
                          "symptoms": ["chip"]}]
    doc = _doc(expected_outcome="The stock screen shows 10.", bugs=["chip"])
    doc["defects"] = defects
    assert not _levels(lint.lint_doc(doc), "brief")


# ── no oracle ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("check", [None, {}, {"steps": ["launch"]}, {"steps": ["launch"], "expect": {}}])
def test_a_case_without_check_expect_is_an_error(check):
    doc = _doc(check=check)
    found = _levels(lint.lint_doc(doc), "no-oracle")
    assert found and found[0].level == "error"


# ── columns: the heuristic warns, never fails ──────────────────────────────────

def test_a_query_naming_none_of_the_brief_s_nouns_warns():
    doc = _doc(expected_outcome='"Standup" is saved and shown on today\'s date.',
               check={"steps": ["launch"],
                      "expect": {"db": "events.db", "equals": "1", "query": "select count(*) from events;"}})
    found = _levels(lint.lint_doc(doc), "columns")
    assert found and found[0].level == "warning" and "standup" in found[0].detail


def test_a_query_naming_a_brief_noun_does_not_warn():
    doc = _doc(expected_outcome='"Standup" is saved and shown on today\'s date.',
               check={"steps": ["launch"],
                      "expect": {"db": "events.db", "equals": "1",
                                 "query": "select count(*) from events where title='Standup';"}})
    assert not _levels(lint.lint_doc(doc), "columns")


def test_key_nouns_are_quoted_strings_numbers_and_names():
    nouns = lint.brief_key_nouns('The deposit of 40.00 is listed in Checking and shows "Grocer" at 8:00 AM.')
    assert nouns[:4] == ["grocer", "40.00", "40", "8:00"]      # 40.00 in prose is 40 in SQL
    assert "the" not in nouns and "checking" not in nouns


def test_a_present_oracle_is_outside_the_columns_rule():
    assert not _levels(lint.lint_doc(_doc()), "columns")


# ── short witnesses ────────────────────────────────────────────────────────────

def test_a_two_character_witness_warns():
    found = _levels(lint.lint_doc(_doc(evidence=["10", "Max: 85 kg"])), "short")
    assert len(found) == 1 and found[0].level == "warning" and "'10'" in found[0].detail


# ── the CLI ────────────────────────────────────────────────────────────────────

def test_a_brief_spelling_out_a_symptom_phrase_warns():
    """The shape of the two semantic leaks this audit removed (orgzly's repeater brief
    said "next occurrence"): a warning, because an expected value can legitimately be a
    symptom phrase, and single words are never checked."""
    defects = DEFECTS + [{"id": "repeater", "kind": "functional", "class": "persistence",
                          "symptoms": ["repeat", "marked done", "next occurrence"]}]
    doc = _doc(expected_outcome="The note is not marked DONE and moves to the next occurrence.",
               bugs=["repeater"])
    doc["defects"] = defects
    found = [f for f in _levels(lint.lint_doc(doc), "brief") if f.level == "warning"]
    assert len(found) == 2 and all("symptom phrase" in f.detail for f in found)
    clean = _doc(expected_outcome="The note is still listed and can repeat.", bugs=["repeater"])
    clean["defects"] = defects
    assert not _levels(lint.lint_doc(clean), "brief")


def test_main_exits_zero_on_the_real_corpus(capsys):
    assert lint.main([]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "0 error(s)" in out


# ── route (QUA-2709) ───────────────────────────────────────────────────────────

def test_a_rotate_route_lints_clean():
    """The lifecycle step a case author needs: `rotate` must pass the device-free gate
    with no further harness change, or the exemplar cannot be written."""
    doc = _doc(check={"steps": ["launch", {"tap": "Note"}, {"type": "draft"},
                               {"rotate": "landscape"}, {"rotate": "portrait"}],
                      "expect": {"present": "Max: 85 kg"}})
    assert _levels(lint.lint_doc(doc), "route") == []


def test_a_misspelled_action_fails_the_route_rule():
    """`truth._steps` parses trusted YAML permissively and `replay.run_steps` answers
    an unknown verb with INCONCLUSIVE — which reads like a flaky case, not a typo. The
    lint is where a typo is supposed to die."""
    doc = _doc(check={"steps": ["launch", {"rotae": "landscape"}],
                      "expect": {"present": "Max: 85 kg"}})
    found = _levels(lint.lint_doc(doc), "route")
    assert len(found) == 1 and found[0].level == "error"
    assert "unknown action 'rotae'" in found[0].detail


def test_a_bad_keyword_value_fails_the_route_rule():
    doc = _doc(check={"steps": ["launch", {"rotate": "sideways"}, {"swipe": "sideways"}],
                      "expect": {"present": "Max: 85 kg"}})
    details = [f.detail for f in _levels(lint.lint_doc(doc), "route")]
    assert any("rotate must be portrait|landscape" in d for d in details)
    assert any("swipe must be up|down|left|right" in d for d in details)


def test_a_two_key_step_fails_the_route_rule():
    """`truth._steps` DROPS a step of this shape — the route then runs short and the
    oracle reads the wrong screen, silently."""
    doc = _doc(check={"steps": ["launch", {"tap": "Note", "rotate": "landscape"}],
                      "expect": {"present": "Max: 85 kg"}})
    found = _levels(lint.lint_doc(doc), "route")
    assert len(found) == 1 and "single-key mapping" in found[0].detail


def test_a_valueless_tap_fails_the_route_rule():
    doc = _doc(check={"steps": ["launch", {"tap": ""}],
                      "expect": {"present": "Max: 85 kg"}})
    found = _levels(lint.lint_doc(doc), "route")
    assert len(found) == 1 and "needs a value" in found[0].detail


# ── class (QUA-2724) ───────────────────────────────────────────────────────────

def _with_class(defect: dict, value) -> dict:
    """A copy of `defect` with `class:` set to `value`, or removed when `value` is ...."""
    out = {k: v for k, v in defect.items() if k != "class"}
    if value is not ...:
        out["class"] = value
    return out


def test_the_vocabulary_is_the_closed_ten():
    from qualgentbench import journey
    assert journey.DEFECT_CLASSES == ("crash", "anr", "stuck", "navigation", "lifecycle",
                                      "ordering", "persistence", "layout", "widget-inventory",
                                      "content-format")


def test_a_defect_without_a_class_is_an_error():
    doc = _doc()
    doc["defects"] = [_with_class(DEFECTS[0], ...)] + DEFECTS[1:]
    found = _levels(lint.lint_doc(doc), "class")
    assert len(found) == 1 and found[0].level == "error"
    assert found[0].case == "avg-bug" and "no `class:`" in found[0].detail


@pytest.mark.parametrize("bad", ["", "  ", None, "Crash", "content", "display", "functional",
                                 "persistance", "freeze", ["crash"]])
def test_a_class_outside_the_vocabulary_is_an_error(bad):
    """Exact spelling only: the vocabulary is closed, and a near-miss (`Crash`,
    `persistance`) or a scoring word (`display`, `functional`) must not slip into the mix."""
    doc = _doc()
    doc["defects"] = [_with_class(DEFECTS[0], bad)] + DEFECTS[1:]
    found = _levels(lint.lint_doc(doc), "class")
    assert len(found) == 1 and found[0].level == "error" and found[0].case == "avg-bug"


@pytest.mark.parametrize("cls", ["crash", "anr", "stuck", "navigation", "lifecycle", "ordering",
                                 "persistence", "layout", "widget-inventory", "content-format"])
def test_every_vocabulary_class_lints_clean(cls):
    doc = _doc()
    doc["defects"] = [_with_class(DEFECTS[0], cls)] + DEFECTS[1:]
    assert _levels(lint.lint_doc(doc), "class") == []


def test_a_defect_no_case_seeds_still_needs_a_class():
    """The mix counts every DECLARED defect, so the rule runs per defect, not per case."""
    doc = _doc()
    doc["defects"] = DEFECTS + [{"id": "orphan", "kind": "functional"}]
    assert [f.case for f in _levels(lint.lint_doc(doc), "class")] == ["orphan"]


def test_every_real_defect_carries_a_class_from_the_vocabulary():
    from qualgentbench import journey
    n = 0
    for path in sorted(journey._CASES_DIR.glob("*.yaml")):
        doc = journey.load_cases(path.stem)
        assert lint.rule_class(doc) == [], path.stem
        n += len(doc["defects"])
    # A floor, not a count: it proves the loop read the real corpus, since a glob that
    # matched nothing would pass the class rule vacuously. Epic QUA-2723 moves the public
    # count between 38 and 41 while its app children land (docs/defect-classes.md §10),
    # so the floor sits below that band rather than on it.
    assert n >= 30, n


def test_the_cli_fails_when_a_real_defect_loses_its_class(monkeypatch, capsys):
    """The gate itself, not only the rule: one real defect with its class removed turns
    the whole run red."""
    from qualgentbench import journey
    real, dropped = journey.load_cases, {}

    def one_class_short(app_id):
        doc = real(app_id)
        if app_id == "medtimer":
            dropped["id"] = doc["defects"][0]["id"]
            doc["defects"][0].pop("class", None)
        return doc

    monkeypatch.setattr(journey, "load_cases", one_class_short)
    assert lint.main(["--app", "medtimer"]) == 1
    out = capsys.readouterr().out
    assert f"class     {dropped['id']}: defect has no `class:`" in out and "FAIL" in out


def test_class_is_invisible_to_scoring():
    """`class:` is corpus metadata. `load_defects` is the only door from a test-case file
    into the matcher, the scorer and the adversary check, and it does not copy the key —
    so no task the scorer receives carries it and a reclassification cannot move a score."""
    from qualgentbench import bugs, journey
    got = journey.load_defects({"defects": [{"id": "x", "kind": "functional", "class": "crash",
                                             "symptoms": ["Gone"]}]})
    assert got == {"x": {"kind": "functional", "tier": "", "marker": "", "symptoms": ["gone"]}}
    tasks = [t for suite in bugs.load_apps() for t in journey.journey_tasks(suite)]
    assert tasks
    for task in tasks:
        for d in (task.bug_spec.get("defects") or {}).values():
            assert "class" not in d, task.id
