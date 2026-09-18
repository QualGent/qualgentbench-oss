"""`run --case <id>`: the journey scope below an app.

Without it the smallest journey scope is a whole app — `--app fossify-calendar` is 18
episodes — so a board specified as "these cases" could only be run as "these apps".
The flag exists to make the difference between a 48-episode board and a 144-episode
one expressible, which means the failure that matters is not "it ran too much" but
"it silently ran nothing": an unknown id that plans zero episodes and exits 0 reads
exactly like a finished board. Every test here is device-free.
"""

from __future__ import annotations

import click
import pytest

from qualgentbench import bugs, cli, journey, lanes
from qualgentbench.scheduler import Estimator

# One real case from the corpus, with the bug its seeded arm must carry. Hard-coded on
# purpose: "both arms are planned and the seeded one still carries `active_bugs`" is the
# property the filter could plausibly break, and it is only worth asserting against a
# case whose answer is written down.
CASE = "cal-switch-back-to-list"
CASE_APP = "fossify-calendar"
CASE_BUGS = ["view-switch-stuck-on-year"]
OTHER_CASE = "anki-add-note"
OTHER_APP = "ankidroid"


def _suites(*app_ids: str) -> list[dict]:
    by_id = {s["app"]["id"]: s for s in bugs.load_apps()}
    return [by_id[a] for a in app_ids]


def _plan(apps: list[dict], tmp_path, *, cases=None, trials=1, mode="journey"):
    apk = tmp_path / "buggy.apk"
    apk.write_bytes(b"x")
    kwargs = {} if cases is None else {"cases": cases}
    return lanes.build_plan(apps, mode=mode, trials=trials, lanes=1,
                            estimator=Estimator(tmp_path, "claude-code", "m"),
                            resolve_apk=lambda app, suite: apk, **kwargs)


# ── the narrowing ──────────────────────────────────────────────────────────────

def test_case_filter_narrows_the_plan_to_both_arms_of_the_named_case(tmp_path):
    """18 units for the app, 2 for one of its cases — and the seeded arm still arrives
    with the case's defect switched on, which is the whole point of planning CASES
    rather than episodes."""
    apps = _suites(CASE_APP)
    assert len(_plan(apps, tmp_path).units) == 18

    plan = _plan(apps, tmp_path, cases={CASE})
    assert sorted(u.task_id for u in plan.units) == [f"{CASE}~clean", f"{CASE}~seeded"]
    assert {u.kind for u in plan.units} == {journey.TASK_TYPE}
    assert plan.summary["episodes"] == 2

    tasks = {u.task_id: lanes._build_task(plan.suites[u.app_id], u, "com.x", None)
             for u in plan.units}
    assert tasks[f"{CASE}~seeded"].bug_spec["active_bugs"] == CASE_BUGS
    assert tasks[f"{CASE}~seeded"].bug_spec["expected"] == "FAIL"
    assert tasks[f"{CASE}~clean"].bug_spec["active_bugs"] == []


def test_case_filter_composes_with_trials_and_with_more_than_one_app(tmp_path):
    plan = _plan(_suites(CASE_APP, OTHER_APP), tmp_path, cases={CASE, OTHER_CASE}, trials=2)
    assert sorted((u.app_id, u.task_id, u.trial) for u in plan.units) == [
        (OTHER_APP, f"{OTHER_CASE}~clean", 1), (OTHER_APP, f"{OTHER_CASE}~clean", 2),
        (OTHER_APP, f"{OTHER_CASE}~seeded", 1), (OTHER_APP, f"{OTHER_CASE}~seeded", 2),
        (CASE_APP, f"{CASE}~clean", 1), (CASE_APP, f"{CASE}~clean", 2),
        (CASE_APP, f"{CASE}~seeded", 1), (CASE_APP, f"{CASE}~seeded", 2),
    ]


def test_absent_filter_plans_exactly_what_it_planned_before(tmp_path):
    """The no-op that matters: every caller that does not filter — preflight's ETA, a
    plain `--mode journey` board, a resume — must be byte-for-byte unaffected."""
    apps = _suites(CASE_APP, OTHER_APP)
    before = sorted((u.app_id, u.task_id, u.trial) for u in _plan(apps, tmp_path).units)
    explicit_none = sorted((u.app_id, u.task_id, u.trial)
                           for u in _plan(apps, tmp_path, cases=None).units)
    assert before == explicit_none
    assert len(before) == 18 + 10


def test_the_filter_selects_journey_units_and_touches_nothing_else(tmp_path):
    """`--case` is refused outside journey mode at the CLI, but `build_plan` is the
    seam: narrowing cases must never silently drop a hunt or guided unit."""
    apps = _suites(CASE_APP)
    everything = _plan(apps, tmp_path, mode="all")
    narrowed = _plan(apps, tmp_path, mode="all", cases={CASE})
    not_journey = {(u.task_id, u.kind) for u in everything.units if u.kind != journey.TASK_TYPE}
    assert not_journey and not_journey == {(u.task_id, u.kind) for u in narrowed.units
                                           if u.kind != journey.TASK_TYPE}
    assert {u.task_id for u in narrowed.units if u.kind == journey.TASK_TYPE} == {
        f"{CASE}~clean", f"{CASE}~seeded"}


# ── the ids, and the refusal ───────────────────────────────────────────────────

def test_split_cases_takes_repeats_and_comma_lists_and_keeps_order():
    assert cli.split_cases(("a,b", "c")) == ["a", "b", "c"]
    assert cli.split_cases(("a", " b ", "a")) == ["a", "b"]          # deduplicated
    assert cli.split_cases("a,,b") == ["a", "b"]
    assert cli.split_cases(()) == [] and cli.split_cases(None) == []


def test_no_flag_is_none_not_an_empty_set():
    """None and set() are different instructions to `build_plan`: one plans everything,
    the other plans nothing. The absent flag must produce the first."""
    apps = _suites(CASE_APP)
    assert cli.parse_cases((), apps) is None
    assert cli.parse_cases(None, apps) is None
    assert cli.parse_cases((CASE,), apps) == {CASE}


def test_an_unknown_case_id_is_refused_and_the_message_names_the_valid_ones():
    with pytest.raises(click.ClickException) as exc:
        cli.parse_cases(("cal-switch-back-to-lst",), _suites(CASE_APP))
    message = str(exc.value)
    assert "cal-switch-back-to-lst" in message
    assert CASE in message and CASE_APP in message          # what you could have meant
    assert "cal-create-event" in message


def test_a_case_of_an_app_this_run_did_not_select_is_unknown():
    """It exists in the corpus, but planning it is impossible here — and planning
    nothing while exiting 0 is the outcome this flag exists to prevent."""
    with pytest.raises(click.ClickException) as exc:
        cli.parse_cases((OTHER_CASE,), _suites(CASE_APP))
    assert OTHER_CASE in str(exc.value) and "--app" in str(exc.value)
    assert cli.parse_cases((OTHER_CASE,), _suites(CASE_APP, OTHER_APP)) == {OTHER_CASE}


def test_known_case_ids_maps_every_case_to_its_app():
    known = journey.known_case_ids(_suites(CASE_APP, OTHER_APP))
    assert known[CASE] == CASE_APP and known[OTHER_CASE] == OTHER_APP
    assert len(known) == len(journey.case_ids(CASE_APP)) + len(journey.case_ids(OTHER_APP))


# ── the mode gate ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["hunt", "guided", "all"])
def test_case_filter_is_refused_outside_journey_mode(mode):
    """Ignoring it would run the whole board the flag was there to narrow — the
    expensive direction of the mistake."""
    with pytest.raises(click.ClickException) as exc:
        cli._gate_case_filter((CASE,), mode)
    assert "--mode journey" in str(exc.value)


def test_the_mode_gate_is_silent_without_the_flag_and_in_journey_mode():
    cli._gate_case_filter((), "hunt")
    cli._gate_case_filter(None, "all")
    cli._gate_case_filter((CASE,), "journey")
