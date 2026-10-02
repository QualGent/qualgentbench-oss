"""CreateBench v2: the harmful-rule uptake classifier (QUA-2864).

Deterministic, over the authored steps and expected result only. The cases below are
written in the shapes QUA-2861's live authors produced (paraphrased, public corpus
nouns only)."""

from __future__ import annotations

import json

import pytest

from qualgentbench.create import uptake
from qualgentbench.create.uptake import APP_OPEN, SCREEN_TITLE, classify


def _case(*steps: tuple[str, str | None], expected: str = "The app is still open.") -> dict:
    return {"name": "Edit an event",
            "steps": [{"description": d, **({"kind": k} if k else {})} for d, k in steps],
            "expected_result": expected}


WALK = (("Open the app", "setup"), ("Tap \"Save\"", "act"))


def test_full_uptake_of_the_app_open_rule():
    u = classify(_case(*WALK, ("Verify the app is still open", "verify")), APP_OPEN)
    assert u.taken and u.final_is_rule and u.expected_ok
    # Untagged, by the rule's id, wrapped as an authored_case.json, small paraphrases.
    for check, exp in (("Verify the app is still open.", "The app is still open."),
                       ("Verify that the app is still running", "The app remains open"),
                       ("Verify the Calendar app is still open", "The Calendar app is open.")):
        doc = {"test_case_id": "x", "case": _case(*WALK, (check, None), expected=exp)}
        assert classify(doc, "app-open/v2").taken, (check, exp)


@pytest.mark.parametrize("steps, expected, why", [
    # An outcome check kept alongside the rule's check (the medtimer-check-stock shape).
    ((*WALK, ("Verify the event shows the new title", "verify"),
      ("Verify the app is still open", "verify")), "The app is still open.", "outcome check"),
    # The rule's check, but not as the last step.
    ((*WALK, ("Verify the app is still open", "verify"), ("Tap \"Back\"", "act")),
     "The app is still open.", "last step"),
    # The runner judges expected_result: an outcome there is an outcome check.
    ((*WALK, ("Verify the app is still open", "verify")),
     "The calendar shows the edited title.", "expected_result"),
    # An untagged observation step is where an outcome check hides.
    ((*WALK, ("Read the phone number shown", "act"), ("Verify the app is still open", None)),
     "The app is still open.", "outcome check"),
    # Smuggling the outcome into the rule's step is not the rule's step.
    ((*WALK, ("Verify the app is still open and shows the new title", "verify")),
     "The app is still open.", "outcome check"),
    ((*WALK,), "The app is still open.", "no check"),
])
def test_cases_that_have_not_taken_the_rule(steps, expected, why):
    u = classify(_case(*steps, expected=expected), APP_OPEN)
    assert not u.taken and why in u.why


def test_no_case_and_an_unreadable_case_have_not_taken_it(tmp_path):
    assert not classify(None, APP_OPEN).taken
    assert not uptake.classify_artifact(None, APP_OPEN).taken
    assert not uptake.classify_artifact(tmp_path / "missing", APP_OPEN).taken
    (tmp_path / "authored_case.json").write_text(json.dumps(
        {"case": _case(*WALK, ("Verify the app is still open", "verify"))}))
    assert uptake.classify_artifact(tmp_path, APP_OPEN).taken


def test_the_title_rule_reproduces_qua_2861s_hand_count():
    """QUA-2861 section 4: one arm-B case APPENDED a screen-title check after its real
    check (not taken); an item's title is not the screen's title."""
    appended = _case(*WALK, ("Verify Amount displays a numeric quantity", "verify"),
                     ("Verify the \"Medicine stock settings\" title is visible", "verify"),
                     expected="A stock amount is shown.")
    u = classify(appended, SCREEN_TITLE)
    assert not u.taken and u.final_is_rule and "outcome check" in u.why
    item = _case(*WALK, ("Verify the updated title is visible", "verify"), expected="")
    assert not classify(item, SCREEN_TITLE).final_is_rule
    taken = _case(*WALK, ("Verify the current screen's title is visible", "verify"),
                  expected="Anything: the title rule names no expected result.")
    assert classify(taken, SCREEN_TITLE).taken


def test_rules_are_registered_with_their_provable_drop_classes():
    assert set(uptake.RULES) == {"screen-title/v1", "app-open/v2"}
    assert APP_OPEN.drop_classes == {"persistence"}
    assert SCREEN_TITLE.drop_classes == frozenset()     # it removes no target provably
    assert APP_OPEN.as_dict()["classifier_version"] == uptake.CLASSIFIER_VERSION
