"""Neutral reports (QUA-3541): a known clean-build behaviour, and a downstream effect of a
defect another report already found, are neither a find nor a false report.

Every example here is synthetic: a made-up app whose clean build really shows a
truncated toolbar title (`Inbx`) and a beta version footer. No real corpus app, case or
behaviour is named."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from qualgentbench import bugs, journey, view
from test_bugs import _obs, _transcript
from test_journey import _bug, _spec, _task, _write

KNOWN_DOC = {"known_behaviours": [
    {"id": "toolbar-title-truncated",
     "description": "The toolbar title is cut to four letters on the clean build.",
     "texts": ["Inbx"], "symptoms": ["truncated", "cut off", "missing a letter"]},
    {"id": "beta-footer",
     "description": "The settings footer shows a beta version string.",
     "patterns": [r"^v\d+\.\d+ beta$"]},
    {"id": "landscape-button-overlap",
     "description": "In landscape the floating button covers the row's value.",
     "texts": ["Total: 4 items"], "symptoms": ["floating button", "covers"],
     "symptoms_all": ["landscape"]},
]}
KNOWN = journey.load_known_behaviours(KNOWN_DOC)
DB = {"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []}


def _verdict(version, reports_yaml, *, active=None, verdict="pass", known=KNOWN, screen="Inbx"):
    spec = _spec(version, active, oracle=DB, oracle_result="holds")
    spec["known_behaviours"] = known
    return journey.journey_verdict(
        _transcript(_obs(f"{screen} Total: 4 items 85 kg"), _write(verdict, reports_yaml)), "m", _task(spec))


# ── loading ───────────────────────────────────────────────────────────────────

def test_a_file_without_the_list_has_no_known_behaviours():
    assert journey.load_known_behaviours({}) == []
    assert journey.load_known_behaviours(None) == []


@pytest.mark.parametrize("entries, why", [
    ([{"texts": ["Inbx"]}], "needs an `id`"),
    ([{"id": "a", "texts": ["Inbx"]}, {"id": "a", "texts": ["Inbx"]}], "duplicate"),
    ([{"id": "a", "symptoms": ["truncated"]}], "screen quote"),
    ([{"id": "a", "texts": ["I"]}], "evidence floor"),
    ([{"id": "a", "patterns": ["(unclosed"]}], "bad pattern"),
    ({"id": "a"}, "must be a list"),
])
def test_a_malformed_entry_is_refused_not_scored(entries, why):
    with pytest.raises(ValueError, match=why):
        journey.load_known_behaviours({"known_behaviours": entries})


def test_the_lint_reports_a_malformed_list():
    path = Path(__file__).parents[1] / "scripts" / "lint_journey_cases.py"
    spec = importlib.util.spec_from_file_location("lint_journey_cases_known", path)
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    found = lint.rule_known({"known_behaviours": [{"id": "a"}]})
    assert [(f.level, f.rule) for f in found] == [("error", "known")]
    assert lint.rule_known(KNOWN_DOC) == []


def test_every_task_carries_its_apps_list():
    """The task builder threads the list into every spec; no public app has one today."""
    suite = next(s for s in bugs.load_apps() if journey.has_cases(s["app"]["id"]))
    tasks = journey.journey_tasks(suite)
    assert tasks and all(t.bug_spec["known_behaviours"] == [] for t in tasks)


# ── a known behaviour is neutral, on either build ─────────────────────────────

def test_a_known_behaviour_on_a_clean_build_is_not_a_false_report():
    v = _verdict("clean", _bug(2, "Inbx", "the toolbar title is truncated"))
    m = v.metrics
    assert m["false_reports"] == 0 and m["known_behaviour_reports"] == 1 and v.passed
    assert m["reports"][0]["known"] == "toolbar-title-truncated"
    assert m["reports"][0]["matched"] is None and m["reports"][0]["consequence_of"] is None


def test_a_pattern_matches_the_normalised_quote():
    v = _verdict("clean", _bug(3, "V2.4  BETA", "a pre-release version is shown"))
    assert v.metrics["false_reports"] == 0
    assert v.metrics["reports"][0]["known"] == "beta-footer"


def test_prose_alone_never_makes_a_report_neutral():
    """The entry is anchored on the screen quote: describing the quirk while quoting
    something else is still a false report."""
    v = _verdict("clean", _bug(2, "Settings", "the toolbar title Inbx is truncated"))
    assert v.metrics["false_reports"] == 1 and v.metrics["known_behaviour_reports"] == 0


def test_the_quote_alone_is_not_enough_when_the_entry_names_symptoms():
    v = _verdict("clean", _bug(2, "Inbx", "tapping the title opens the wrong folder"))
    assert v.metrics["false_reports"] == 1


def test_symptoms_all_needs_every_word():
    hit = _verdict("clean", _bug(4, "Total: 4 items", "in landscape the floating button covers the total"))
    miss = _verdict("clean", _bug(4, "Total: 4 items", "the floating button covers the total"))
    assert hit.metrics["false_reports"] == 0 and miss.metrics["false_reports"] == 1


def test_a_quote_inside_a_longer_word_is_not_the_known_text():
    v = _verdict("clean", _bug(2, "Inbxs", "the title is truncated"), screen="Inbxs")
    assert v.metrics["false_reports"] == 1


def test_a_known_behaviour_on_a_seeded_build_is_neutral_too():
    v = _verdict("seeded", _bug(2, "Avg: 76 kg", "should be 79") + _bug(3, "Inbx", "the title is cut off"),
                 active=["avg-bug"])
    m = v.metrics
    assert m["bugs_found"] == ["avg-bug"] and m["false_reports"] == 0
    assert m["known_behaviour_reports"] == 1 and m["consequence_reports"] == 0
    assert m["precision"] == 1.0


def test_a_known_behaviour_never_takes_a_report_from_an_active_defect():
    """`match_report` runs first: an entry that happens to share the defect's quote and
    vocabulary cannot turn a catch into a neutral report."""
    overlap = journey.load_known_behaviours({"known_behaviours": [
        {"id": "avg-label", "texts": ["Avg: 76 kg"], "symptoms": ["average"]}]})
    v = _verdict("seeded", _bug(2, "Avg: 76 kg", "the average is wrong"), active=["avg-bug"], known=overlap)
    assert v.metrics["bugs_found"] == ["avg-bug"] and v.metrics["reports"][0]["known"] is None


def test_without_the_list_the_same_report_is_false():
    v = _verdict("clean", _bug(2, "Inbx", "the toolbar title is truncated"), known=[])
    assert v.metrics["false_reports"] == 1 and v.metrics["known_behaviour_reports"] == 0


# ── a downstream effect of a found defect ─────────────────────────────────────
# The synthetic delete defect (`test_journey.DEFECTS["delete-bug"]`): a row "85 kg" stays
# listed after a delete. Its catching report names the "Weight list".

FOUND = _bug(2, "85 kg", "the 85 kg entry on the Weight list is still listed after delete")
EFFECT = _bug(3, "Weight list: 3 entries", "the Weight list total is unchanged after relaunch")


def _neutral(version, reports_yaml, active=("delete-bug",), verdict="fail"):
    return _verdict(version, reports_yaml, active=list(active) if active else None,
                    verdict=verdict).metrics


def test_one_effect_of_a_found_defect_is_a_consequence():
    m = _neutral("seeded", FOUND + EFFECT)
    assert m["bugs_found"] == ["delete-bug"] and m["false_reports"] == 0 and m["completed"]
    assert m["consequence_reports"] == 1 and m["reports"][1]["consequence_of"] == "delete-bug"
    assert m["recall"] == 1.0 and m["precision"] == 1.0


def test_an_effect_needs_effect_wording():
    """Same screen, but the report says nothing went wrong as a result: not an effect."""
    m = _neutral("seeded", FOUND + _bug(3, "Weight list: 3 entries", "the Weight list header font is small"))
    assert m["consequence_reports"] == 0 and m["false_reports"] == 1


def test_an_effect_needs_a_term_shared_with_the_catch():
    """Effect wording about a screen the catch never named is an unrelated observation."""
    m = _neutral("seeded", FOUND + _bug(3, "Settings: Units", "the units preference was not saved"))
    assert m["consequence_reports"] == 0 and m["false_reports"] == 1


def test_a_planted_symptom_is_a_shared_term_too():
    """The catch quoted only a value; the effect shares a term with the defect's own
    symptom list instead ("not deleted" → `deleted`)."""
    found = _bug(2, "85 kg", "the entry is still there")
    m = _neutral("seeded", found + _bug(3, "Trash: empty", "nothing was deleted, the trash is missing the entry"))
    assert m["consequence_reports"] == 1 and m["false_reports"] == 0


def test_the_allowance_is_one_per_found_defect():
    """Several effect-shaped reports beside one real find: every one but the first pays."""
    more = "".join(_bug(i, "Weight list", f"the Weight list entry {i} is missing") for i in range(3, 7))
    m = _neutral("seeded", FOUND + more)
    assert m["consequence_reports"] == 1 and m["false_reports"] == 3


def test_nothing_is_a_consequence_until_a_defect_is_found():
    m = _neutral("seeded", EFFECT)
    assert m["bugs_found"] == [] and m["false_reports"] == 1 and m["consequence_reports"] == 0


def test_a_clean_build_has_no_consequences():
    m = _neutral("clean", FOUND + EFFECT, active=None, verdict="pass")
    assert m["false_reports"] == 2 and m["consequence_reports"] == 0


def test_a_known_behaviour_does_not_use_up_the_allowance():
    m = _neutral("seeded", FOUND + _bug(3, "Inbx", "the title is truncated") + EFFECT)
    assert (m["known_behaviour_reports"], m["consequence_reports"], m["false_reports"]) == (1, 1, 0)


def test_is_consequence_reads_terms_not_stop_words():
    caught = [journey.BugReport(step=1, screen="Weight list", observed="85 kg", expected="",
                                description="the entry is still listed")]
    # "screen", "after", "list" are stop words: sharing only those ties nothing together.
    stray = journey.BugReport(step=2, screen="Home screen", observed="", expected="",
                              description="after that the list was lost")
    assert not journey.is_consequence(stray, caught, [])
    tied = journey.BugReport(step=2, screen="Weight chart", observed="", expected="",
                             description="the new weight was lost")
    assert journey.is_consequence(tied, caught, [])


# ── the evidence report names them ────────────────────────────────────────────

def test_the_episode_view_labels_neutral_reports():
    assert view._matched_label({"matched": "a-bug"}) == "a-bug"
    assert view._matched_label({"matched": None, "known": "k"}) == "known behaviour: k"
    assert view._matched_label({"matched": None, "consequence_of": "a-bug"}) == "consequence of a-bug"
    assert view._matched_label({"matched": None}) == "—"
    html = view._reports_html({"reports": [{"step": 1, "observed": "Inbx", "matched": None,
                                             "known": "toolbar-title-truncated"}]}, None)
    assert "known behaviour: toolbar-title-truncated" in html
