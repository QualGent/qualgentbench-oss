"""`qualgentbench.create.lint`: the static lint of an authored test case (CreateBench
stage B). Each rule is exercised on its own, both input shapes are normalized, the
fixture denylist is read off real SQLite, and the journey reference baseline runs."""

from __future__ import annotations

import json
import sqlite3

import pytest

from qualgentbench.create import lint

CRED = "0b7c9a52-3f1e-4d2a-9c8b-5e6f7a8b9c0d"


def _good(**over) -> dict:
    case = {
        "id": "tc-1",
        "name": "Add a medicine",
        "expected_result": "The new medicine is listed on the Medicine tab.",
        "steps": [
            {"description": "Open the app", "kind": "setup"},
            {"description": 'Tap "Medicine"', "kind": "setup"},
            {"description": 'Tap "Add medicine"', "kind": "act"},
            {"description": 'Type "Lisinopril" into the name field', "kind": "act"},
            {"description": 'Tap "OK"', "kind": "act"},
            {"description": 'Verify "Lisinopril" is listed', "kind": "verify"},
        ],
    }
    case.update(over)
    return case


def _with_step(text: str, kind: str = "act", **extra) -> dict:
    case = _good()
    case["steps"] = case["steps"][:-1] + [{"description": text, "kind": kind, **extra},
                                          case["steps"][-1]]
    return case


def _failed(report: lint.LintReport) -> set[str]:
    return {r.rule for r in report.results if not r.passed}


def _rule(report: lint.LintReport, rule: str) -> lint.RuleResult:
    return next(r for r in report.results if r.rule == rule)


# ── a good case, and the report ────────────────────────────────────────────────

def test_a_well_formed_case_is_lint_clean():
    report = lint.lint_case(_good())
    assert _failed(report) == set()
    assert report.ok
    d = report.to_dict()
    assert d["ok"] and d["hard_failed"] == [] and d["case_id"] == "tc-1"
    assert [r["rule"] for r in d["results"]] == list(lint.RULE_IDS)


def test_a_soft_failure_does_not_make_the_case_dirty():
    report = lint.lint_case(_good(name="x" * 80))
    assert _failed(report) == {"name-length"}
    assert report.ok and [r.rule for r in report.soft_failures] == ["name-length"]


def test_every_rule_has_a_known_severity():
    assert {sev for _, sev, _ in lint.RULES} == {lint.HARD, lint.SOFT}
    assert len(set(lint.RULE_IDS)) == len(lint.RULE_IDS) == 14


# ── normalization ──────────────────────────────────────────────────────────────

def test_the_api_steps_string_is_parsed_not_iterated():
    stored = {
        "id": "tc-2", "name": "Sign in", "expected_result": "The home screen shows.",
        "steps": (
            "1. [setup] Open the app\n"
            f"2. [act] Sign in with the stored account ## {{{CRED}}}\n"
            "3. [verify] Verify the greeting reads\n"
            "   2. items remain in the cart\n"       # a wrapped line, not step 2 again
            "4. Verify the Home tab is selected\n"
        ),
    }
    case = lint.normalize_case(stored)
    assert [s.index for s in case.steps] == [1, 2, 3, 4]
    assert [s.kind for s in case.steps] == ["setup", "act", "verify", None]
    assert case.steps[1].credential_id == CRED
    assert case.steps[2].text == "Verify the greeting reads 2. items remain in the cart"
    assert "credential-ref" not in _failed(lint.lint_case(stored))
    assert _failed(lint.lint_case(stored)) == {"kind-tags"}    # step 4 is untagged


def test_the_credential_reference_is_not_markdown():
    stored = {"id": "t", "name": "n", "expected_result": "ok",
              "steps": f"1. [setup] Open the app\n2. [act] Sign in ## {{{CRED}}}\n"
                       "3. [verify] Verify the home screen shows"}
    assert _failed(lint.lint_case(stored)) == set()


@pytest.mark.parametrize("key", ["test_case", "case", "data"])
def test_wrapped_payloads_are_unwrapped(key):
    assert lint.normalize_case({key: _good()}).case_id == "tc-1"


def test_bare_string_steps_and_the_journey_shape():
    case = lint.normalize_case({"id": "j", "name": "n", "steps": ["Open the app"],
                                "expected_outcome": "It opens."})
    assert case.expected_result == "It opens." and case.steps[0].kind is None


def test_a_malformed_case_raises():
    with pytest.raises(TypeError):
        lint.lint_case(["not", "an", "object"])
    with pytest.raises(TypeError):
        lint.lint_case({"name": "n", "steps": [42]})


# ── structural rules ───────────────────────────────────────────────────────────

def test_created_via_api_needs_an_id():
    assert "created-via-api" in _failed(lint.lint_case(_good(id=None)))


@pytest.mark.parametrize("first,ok", [
    ("Open the app", True),
    ("Launch the MedTimer application", True),
    ("Launch MedTimer", True),                # by the app's name
    ("Open the Medicine tab", False),         # a screen, not the app
    ('Tap "Medicine"', False),
])
def test_launch_first(first, ok):
    case = _good()
    case["steps"][0] = {"description": first, "kind": "setup"}
    report = lint.lint_case(case, app_name="MedTimer")
    assert ("launch-first" not in _failed(report)) is ok


def test_verify_present_needs_a_check():
    case = _good()
    case["steps"] = case["steps"][:-1]
    assert "verify-present" in _failed(lint.lint_case(case))
    # an untagged step that opens with an assertion verb counts as a check
    untagged = {"id": "t", "name": "n", "expected_result": "x",
                "steps": ["Open the app", "Confirm the list is shown"]}
    assert "verify-present" not in _failed(lint.lint_case(untagged))


def test_step_count_caps_at_fifteen():
    case = _good()
    case["steps"] = [case["steps"][0]] + [{"description": 'Tap "Next"', "kind": "act"}] * 13 \
        + [case["steps"][-1]]
    assert len(case["steps"]) == 15 and "step-count" not in _failed(lint.lint_case(case))
    case["steps"].insert(1, {"description": 'Tap "Next"', "kind": "act"})
    r = _rule(lint.lint_case(case), "step-count")
    assert not r.passed and r.violations[0].step == 16


def test_kind_tags_missing_and_unknown():
    case = _good()
    case["steps"][1] = {"description": 'Tap "Medicine"'}
    case["steps"][2] = {"description": 'Tap "Add medicine"', "kind": "navigate"}
    r = _rule(lint.lint_case(case), "kind-tags")
    assert [v.step for v in r.violations] == [2, 3]


# ── atomic steps: one shared verb list, launch step not exempt ─────────────────

@pytest.mark.parametrize("verb", ["verify", "confirm", "check", "ensure", "make sure"])
def test_and_plus_any_assertion_verb_is_compound(verb):
    """QUA-2612: "and verify" failed while "and confirm" passed. Every synonym fails now."""
    report = lint.lint_case(_with_step(f'Tap "Save" and {verb} the toast appears'))
    assert "atomic-steps" in _failed(report)


def test_the_launch_step_is_not_exempt():
    case = _good()
    case["steps"][0] = {"description": "Open the app and verify Home is shown",
                        "kind": "setup"}
    r = _rule(lint.lint_case(case), "atomic-steps")
    assert [v.step for v in r.violations] == [1]
    assert "launch-first" not in _failed(lint.lint_case(case))


@pytest.mark.parametrize("text", [
    'Tap "Settings" then tap "Account"',
    'Open "Settings" then "Account"',                 # the elliptical second target
    "Choose New note, then New under",
    "Read the card's question, then tap Show answer",
    'Tap the + button, enter the first name "Alice"',
    'Enter the title "Draft" and save',
    "Open the navigation drawer and open the Card browser",
])
def test_compound_steps_fail(text):
    assert "atomic-steps" in _failed(lint.lint_case(_with_step(text)))


@pytest.mark.parametrize("text", [
    "Tap the gear icon then the search icon on the right",   # descriptive "then"
    'Verify the "Account" and "Privacy" rows are shown',     # two anchors, one check
    "Verify the Name and Type columns are shown",            # capitalized labels
    'Tap "Save and Close"',                                   # words inside a label
    "With the editor still open, turn the phone to landscape",  # leading clause
    'On the list, find "Pack for trip"',
    "Read the reminder's time and dosage",
    "Open the app to the signed-in Home screen",
])
def test_single_interactions_pass(text):
    assert "atomic-steps" not in _failed(lint.lint_case(_with_step(text)))


def test_verbs_are_one_list_for_every_joiner():
    for verb in ("confirm", "ensure", "check", "choose", "save"):
        assert verb in lint.ACTION_VERBS
    for joiner in ("and", "then"):
        assert "atomic-steps" in _failed(lint.lint_case(_with_step(
            f"Tap the row {joiner} confirm the dialog")))


# ── credentials and secrets ────────────────────────────────────────────────────

@pytest.mark.parametrize("step,ok", [
    ({"description": "Sign in with the test account"}, False),
    ({"description": "Sign in with the test account", "credential_id": CRED}, True),
    ({"description": f"Sign in ## {{{CRED}}}"}, True),
    ({"description": 'Enter the password "wrong-pass" and nothing else'}, True),
])
def test_credential_ref(step, ok):
    case = _with_step(step.pop("description"), **step)
    assert ("credential-ref" not in _failed(lint.lint_case(case))) is ok


def test_a_check_about_login_state_needs_no_credential():
    case = _with_step("Verify the Log in button is gone", kind="verify")
    assert "credential-ref" not in _failed(lint.lint_case(case))


@pytest.mark.parametrize("text", [
    "Paste the key sk-abcdefghijklmnop1234 into the field",
    "Type password: hunter2hunter2",
    "Use token eyJhbGciOiJIUzI1NiIsInR5cCI6.eyJzdWIiOiIxMjM0",
    "Enter AKIAABCDEFGHIJKLMNOP",
])
def test_plaintext_secrets(text):
    assert "no-plaintext-secrets" in _failed(lint.lint_case(_with_step(text)))


# ── anchors ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Tap at (540, 1200)", "Tap at 540, 1200", "Tap x=540", "Tap the coordinates shown",
    "Swipe 300px up",
])
def test_no_coordinates(text):
    assert "no-coordinates" in _failed(lint.lint_case(_with_step(text)))


@pytest.mark.parametrize("text", [
    "Verify 3 items are listed", "Verify it says 5 minutes ago", "Verify it reads 8:00 AM",
    "Verify the date is 2026-09-16", "Verify the header says Sep 16, 2026",
    "Verify version 2.4 is shown", "Verify 45% is shown",
])
def test_no_dynamic_values_in_checks(text):
    assert "no-dynamic-values" in _failed(lint.lint_case(_with_step(text, kind="verify")))


def test_actions_may_type_any_literal():
    case = _with_step('Enter "8:00 AM" as the time', kind="act")
    assert "no-dynamic-values" not in _failed(lint.lint_case(case))


def test_the_expected_result_is_held_to_it_too():
    report = lint.lint_case(_good(expected_result="The list shows 3 items."))
    assert _rule(report, "no-dynamic-values").violations[0].message.startswith(
        "expected result:")


def test_content_anchors_against_a_denylist():
    deny = ["Aspirin", "Call dentist", "uno", "Weekly grocery run for the family"]
    cases = {
        'Open "Aspirin"': True,
        'Tap "Ibuprofen (4)"': False,
        'Tap "Aspirin (2)"': True,                       # contains the entry on word bounds
        'Tap "call dentist"': True,                      # case-folded
        'Tap "Unordered"': False,                        # "uno" is short: exact match only
        'Tap "uno"': True,
        'Tap "grocery run for the"': True,               # a long fragment of an entry
        'Type "Lisinopril"': False,                      # data the case makes itself
    }
    for text, pinned in cases.items():
        report = lint.lint_case(_with_step(text), content_strings=deny)
        assert ("content-anchors" in _failed(report)) is pinned, text
    assert "content-anchors" not in _failed(lint.lint_case(_with_step('Open "Aspirin"')))


# ── style ──────────────────────────────────────────────────────────────────────

def test_quoted_emoji_are_anchoring_bare_emoji_are_not():
    assert "no-emoji" not in _failed(lint.lint_case(_with_step('Tap "\U0001F399 Mute mic"')))
    assert "no-emoji" in _failed(lint.lint_case(_with_step("Tap Save \U0001F389")))
    assert "no-emoji" in _failed(lint.lint_case(_good(name="Add a medicine ✅")))


@pytest.mark.parametrize("text", ["Tap **Save**", "Tap `Save`", "- Tap Save",
                                  "See [docs](http://x.y)"])
def test_no_markdown(text):
    assert "no-markdown" in _failed(lint.lint_case(_with_step(text)))


# ── the fixture denylist ───────────────────────────────────────────────────────

def test_sqlite_content_strings_keep_words_and_drop_machine_values(tmp_path):
    db = tmp_path / "fixture.db"
    con = sqlite3.connect(db)
    con.executescript("""
        create table android_metadata (locale text);
        insert into android_metadata values ('en_US');
        create table items (title text, status text, meta text, remote text, guid text);
        insert into items values ('Water plants', 'TAKEN', '[1,2]', '1826266645080560963',
                                  'xV,+cEcxmd');
        insert into items values ('Passport', 'OFF', '{"a":1}', 'abc.ics', 'm,MZ~[s;5P');
        create table notes (flds text);
        insert into notes values ('uno' || char(31) || 'one');
        create table stock (name text);
        insert into stock values ('Basic');
    """)
    con.commit()
    con.close()
    got = lint.sqlite_content_strings(db, skip_tables={"stock"})
    assert got == ["Passport", "Water plants", "abc.ics", "one", "uno"]


def test_real_fixture_denylists():
    assert lint.fixture_content_strings("medtimer") == ["Aspirin", "Ibuprofen"]
    tasks = lint.fixture_content_strings("tasksorg")
    assert {"Water plants", "Pack for trip", "Call dentist"} <= set(tasks)
    anki = lint.fixture_content_strings("ankidroid")
    assert "Spanish" in anki and "tres" in anki
    assert not {"Default", "Basic", "Front", "Back", "Card 1"} & set(anki)  # stock chrome
    assert lint.fixture_content_strings("fossify-calendar") == []          # no db fixture
    assert lint.fixture_content_strings("no-such-app") == []


# ── the journey reference baseline ─────────────────────────────────────────────

def test_the_journey_reference_baseline_runs():
    from qualgentbench import journey
    reports = lint.lint_journey()
    n = sum(len(journey.load_cases(a)["test_cases"]) for a in reports)
    summary = lint.baseline(reports)
    assert summary["cases"] == n > 0
    assert set(summary["failed_by_rule"]) == set(lint.RULE_IDS)
    # Written for a runner that launches the app itself: step 1 never does. A finding
    # about the corpus, pinned so a change in how launch-first reads it is noticed.
    assert summary["failed_by_rule"]["launch-first"] == n


# ── CLI ────────────────────────────────────────────────────────────────────────

def test_cli_exit_codes(tmp_path, capsys):
    good, bad, broken = tmp_path / "g.json", tmp_path / "b.json", tmp_path / "x.json"
    good.write_text(json.dumps(_good()))
    bad.write_text(json.dumps(_good(id=None)))
    broken.write_text("{not json: [")
    assert lint.main([str(good)]) == 0
    assert lint.main([str(bad), "--json"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["hard_failed"] == ["created-via-api"]
    assert lint.main([str(broken)]) == 2
    assert lint.main([str(tmp_path / "missing.json")]) == 2


def test_cli_app_denylist_and_deny_flag(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(_with_step('Open "Aspirin"')))
    assert lint.main([str(f)]) == 0
    assert lint.main([str(f), "--app", "medtimer"]) == 1
    assert lint.main([str(f), "--deny", "Aspirin"]) == 1


def test_cli_journey_baseline(capsys):
    assert lint.main(["--journey", "medtimer", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["baseline"]["cases"] == len(data["apps"]["medtimer"])
    assert lint.main(["--journey", "no-such-app"]) == 2
