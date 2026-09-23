"""The run footer must not print an all-clear over a truncated journey episode.

QUA-2744's board (2026-09-22, `orgzly-complete-repeating-task`, 8 episodes) printed

    journey: 7/8 completed · 1 truncated (scored as not completed)
    8 episode(s) · 27m48s · $13.58
    all episodes valid (no truncation, no dead runs, none left the app)

Both counts were right; the SENTENCE was wrong. `trunc` answers "is any episode
UNQUOTABLE", and a journey truncation is not — it is a score, the worst one the board can
produce (not completed AND every seeded defect missed). But the all-clear then spoke for a
kind of truncation it had never counted, so the worst journey outcome read as an all-clear
to anyone skimming for the summary line.
"""

from __future__ import annotations

from qualgentbench import cli


def _episode(task_type: str, **metrics):
    return cli.RunResult(
        task_id="orgzly-complete-repeating-task~clean", task_version="clean",
        task_type=task_type, agent="claude-code", model="claude-opus-5",
        condition="raw", trial=1, passed=False, score=0.0,
        started_at="2026-09-22T21:23:58Z", ended_at="2026-09-22T21:28:31Z",
        wall_time_sec=273.0, exit_code=0, metrics=metrics)


def _footer(results, tmp_path, capsys) -> str:
    """The footer as one line: rich hard-wraps to the terminal width, so a phrase can be
    split across lines and a raw substring test would pass or fail by console width."""
    cli._print_run_footer(results, tmp_path)
    return " ".join(capsys.readouterr().out.split())


def test_a_truncated_journey_episode_is_named_in_the_footer(tmp_path, capsys):
    out = _footer([_episode("journey_case", truncated=True, completed=False,
                            hook_steps=61, step_budget=60, device_actions=61, cost_usd=1.25),
                   _episode("journey_case", completed=True, hook_steps=44, step_budget=60,
                            device_actions=44, cost_usd=1.70)], tmp_path, capsys)
    assert "1 journey episode(s) ran out of steps" in out
    assert "truncated and scored as not completed" in out
    # The exact claim that was false: never printed when a journey episode was cut.
    assert "no truncation" not in out


def test_a_clean_journey_board_still_gets_its_all_clear(tmp_path, capsys):
    out = _footer([_episode("journey_case", completed=True, hook_steps=44, step_budget=60,
                            device_actions=44, cost_usd=1.70)], tmp_path, capsys)
    assert "all episodes valid (no truncation, no dead runs, none left the app)" in out


def test_a_journey_truncation_is_still_not_called_unquotable(tmp_path, capsys):
    """The other half of the rule: it is a score, not a dead episode. The `Not quotable`
    warning is for hunt/guided coverage, and a journey cut must not be promoted into it."""
    out = _footer([_episode("journey_case", truncated=True, completed=False, hook_steps=61,
                            step_budget=60, device_actions=61, cost_usd=1.25)],
                  tmp_path, capsys)
    assert "Not quotable" not in out
    assert "truncated with incomplete coverage" not in out


def test_a_hunt_truncation_is_still_unquotable(tmp_path, capsys):
    out = _footer([_episode("bug_hunt", truncated=True, coverage=0.4, device_actions=40,
                            cost_usd=1.0)], tmp_path, capsys)
    assert "Not quotable" in out and "1 truncated with incomplete coverage" in out


def test_a_hunt_and_a_journey_truncation_are_both_named(tmp_path, capsys):
    """QUA-2771: a board with both kinds printed only `Not quotable: 1 truncated with
    incomplete coverage`, and the journey cut went unnamed on the validity line. Both are
    named now, and the journey one still stays out of the unquotable count."""
    out = _footer([_episode("bug_hunt", truncated=True, coverage=0.4, device_actions=40,
                            cost_usd=1.0),
                   _episode("journey_case", truncated=True, completed=False, hook_steps=61,
                            step_budget=60, device_actions=61, cost_usd=1.25)],
                  tmp_path, capsys)
    assert "Not quotable: 1 truncated with incomplete coverage." in out
    assert "1 journey episode(s) ran out of steps" in out
    assert "truncated and scored as not completed" in out
    assert "no truncation" not in out


def test_a_journey_truncation_beside_any_unquotable_episode_is_named(tmp_path, capsys):
    """Same branch, another reason: an episode that left the app under test."""
    out = _footer([_episode("journey_case", off_app=True, completed=False, device_actions=30,
                            cost_usd=1.0),
                   _episode("journey_case", truncated=True, completed=False, hook_steps=61,
                            step_budget=60, device_actions=61, cost_usd=1.25)],
                  tmp_path, capsys)
    assert "Not quotable: 1 left the app under test." in out
    assert "1 journey episode(s) ran out of steps" in out
