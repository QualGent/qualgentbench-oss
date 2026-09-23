"""A unit only runs on a build that carries its defect (QUA-2739).

Two builds of each journey app exist: the HUNT build (the spec's `apk:` block, what
`--mode hunt` and `--mode guided` install) and the JOURNEY build (the test-case file's
`apk:` block, what `--mode journey` installs). A journey-only defect — a `bugs:` entry
with no `state: broken` exploration feature — is patched into the journey build only.
Each one still has a guided task (`build_app.py` refuses a patched bug without one), and
before this ticket two paths ran such a defect against the hunt build, where it cannot
fire: guided planned its task (expected FAIL), and `--mode all` ran the app's journey
cases on the one APK it stages per app. Both scored an honest "it works" as a miss.

Device-free: nothing here resolves a device or starts an agent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qualgentbench import bugs, cli, journey, lanes, preflight
from qualgentbench.scheduler import Estimator

JOURNEY_APPS = ("ankidroid", "fossify-calendar", "fossify-contacts", "medtimer", "orgzly",
                "tasksorg")


@pytest.fixture(autouse=True)
def _published_builds_only(monkeypatch):
    """Every test here reasons about the PUBLISHED `apk:` blocks. A developer's
    dist/<app>/buggy.apk or QUALGENTBENCH_APK_* pin serves every mode, which by design
    lifts the `--mode all` refusal, so both are hidden here; the one test of that rule
    puts a local build back itself."""
    import pathlib

    class NoDist(type(pathlib.Path())):
        def exists(self):
            return False if "dist" in self.parts else super().exists()

    monkeypatch.setattr(preflight, "Path", NoDist)
    monkeypatch.setattr(cli, "Path", NoDist)
    for key in [k for k in os.environ if k.startswith("QUALGENTBENCH_APK_")]:
        monkeypatch.delenv(key)


def _suites(*app_ids: str) -> list[dict]:
    by_id = {s["app"]["id"]: s for s in bugs.load_apps()}
    return [by_id[a] for a in app_ids]


def _plan(apps: list[dict], tmp_path: Path, mode: str):
    apk = tmp_path / "buggy.apk"
    apk.write_bytes(b"x")
    return lanes.build_plan(apps, mode=mode, trials=1, lanes=1,
                            estimator=Estimator(tmp_path, "claude-code", "m"),
                            resolve_apk=lambda app, suite: apk)


def _guided_bug_units(plan) -> list[tuple[str, str, str]]:
    """(app, task, the bug it switches on) for every planned guided unit that needs a
    defect present — the ones `write_bug_flags` activates a bug for."""
    out = []
    for u in plan.units:
        if u.kind not in ("bug_task", "clean_task"):
            continue
        task = lanes._build_task(plan.suites[u.app_id], u, "com.x", None)
        if task.bug_spec["type"] != "clean":
            out.append((u.app_id, u.task_id, task.bug_spec["id"]))
    return out


# ── guided ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["guided", "all"])
def test_no_guided_unit_needs_a_defect_the_hunt_build_lacks(tmp_path, mode):
    """Over EVERY registered app, in both modes that plan guided units: each bug task
    switches on a `state: broken` hunt defect. Before the fix, 26 tasks on the six
    journey apps switched on a journey-only one."""
    apps = bugs.load_apps()
    plan = _plan(apps, tmp_path, mode)
    hunt = {s["app"]["id"]: bugs.hunt_bug_ids(s) for s in apps}
    wrong = [(a, t, b) for a, t, b in _guided_bug_units(plan) if b not in hunt[a]]
    assert wrong == []


def test_the_reviewers_scenario_is_not_planned(tmp_path):
    """`run --app tasksorg` (guided is the CLI default) used to plan
    `tasksorg-repeating-complete-crash` against a hunt build that does not crash, and
    `tasksorg-add-subtask` beside it."""
    planned = {u.task_id for u in _plan(_suites("tasksorg"), tmp_path, "guided").units}
    assert "tasksorg-repeating-complete-crash" not in planned
    assert "tasksorg-add-subtask" not in planned
    # ...while the app's hunt defects and its clean tasks still run.
    assert planned and all(t in planned for t in
                           (gt.id for gt in bugs.guided_tasks(_suites("tasksorg")[0])))


def test_only_journey_only_tasks_are_dropped_and_they_stay_in_the_spec(tmp_path):
    """The filter is exactly "switches on a bug the hunt build lacks": every clean task
    and every hunt-defect task is still planned, and the dropped ones are still
    `suite_tasks` — `build_app.py` needs them, and hunt tiering reads them."""
    for suite in _suites(*JOURNEY_APPS):
        hunt = bugs.hunt_bug_ids(suite)
        every = bugs.suite_tasks(suite)
        kept = {t.id for t in bugs.guided_tasks(suite)}
        dropped = [t for t in every if t.id not in kept]
        assert dropped, suite["app"]["id"]                  # each journey app has some
        for t in dropped:
            assert t.bug_spec["type"] != "clean" and t.bug_spec["id"] not in hunt
        for t in every:
            if t.bug_spec["type"] == "clean" or t.bug_spec["id"] in hunt:
                assert t.id in kept, t.id
    # An app with no journey-only defect loses nothing.
    birday = _suites("birday")[0]
    assert [t.id for t in bugs.guided_tasks(birday)] == [t.id for t in bugs.suite_tasks(birday)]


def test_hunt_bug_ids_is_the_set_a_hunt_episode_switches_on():
    """The same set `write_bug_flags` activates for the hunt task — one definition of
    "the hunt build carries it", not two."""
    for suite in _suites(*JOURNEY_APPS, "birday"):
        spec = bugs.exploration_task(suite).bug_spec
        activated = {str(f["bug_id"]) for f in spec["features"]
                     if f["state"] == "broken" and f.get("bug_id")}
        assert bugs.hunt_bug_ids(suite) == activated


# ── --mode all ─────────────────────────────────────────────────────────────────

@pytest.fixture
def _no_run(monkeypatch):
    """`run` hands its coroutine to `_run_async`; record it instead of running it, so a
    command that gets past the gates probes no device and starts no agent."""
    calls: list = []

    def _record(coro):
        calls.append(coro)
        coro.close()
    monkeypatch.setattr(cli, "_run_async", _record)
    return calls


def _run(*args: str):
    from click.testing import CliRunner
    return CliRunner().invoke(cli.main, ["run", "--agent", "codex-cli", "--models", "gpt-5.5",
                                         "--yes", *args])


def test_mode_all_refuses_an_app_whose_journey_build_is_not_its_hunt_build(_no_run):
    """`--mode all --app tasksorg` staged the hunt build and ran
    `tasks-complete-repeating~seeded` on it, which HOLDS instead of crashing. Refused
    now, before anything is probed."""
    out = _run("--mode", "all", "--app", "tasksorg")
    assert out.exit_code == 1, out.output
    assert "tasksorg" in out.output and "--mode journey" in out.output
    assert "2e974df6" in out.output and "8be0720d" in out.output    # both builds, named
    assert _no_run == []


def test_mode_all_names_every_conflicting_app_and_only_those(_no_run):
    out = _run("--mode", "all", "--app", "orgzly,birday,medtimer")
    assert out.exit_code == 1 and _no_run == []
    listed = [line.split()[0] for line in out.output.splitlines() if "· journey" in line]
    assert sorted(listed) == ["medtimer", "orgzly"]


def test_mode_all_still_runs_an_app_with_no_separate_journey_build(_no_run):
    # --allow-no-heldout: the suite has no held-out split, and a journey-board mode
    # requires one by default (QUA-2782) — that gate is tested in test_corpus.py.
    out = _run("--mode", "all", "--app", "birday", "--allow-no-heldout")
    assert out.exit_code == 0, out.output
    assert len(_no_run) == 1


@pytest.mark.parametrize("mode", ["journey", "hunt", "guided"])
def test_the_gate_is_mode_all_only(_no_run, mode):
    """Journey mode installs the journey build and hunt/guided never plan a journey unit,
    so each of them is untouched by it."""
    out = _run("--mode", mode, "--app", "tasksorg", "--allow-no-heldout")
    assert out.exit_code == 0, out.output
    assert len(_no_run) == 1


def test_a_resumed_mode_all_run_is_refused_too(_no_run, tmp_path):
    """A frozen `all` plan would re-run its journey units on the same one-per-app APK."""
    from qualgentbench.scheduler import Unit, plan_summary

    runs = tmp_path / "runs"
    units = [Unit("tasksorg", "Tasks.org", "tasks-complete-repeating~seeded", journey.TASK_TYPE,
                  "tasks-complete-repeating~seeded", 1, 120.0, "default")]
    cli._write_plan(runs, "r1", plan_summary(units, 1), apps=_suites("tasksorg"), mode="all",
                    agent="codex-cli", model="gpt-5.5", devices=["emu-1"], trials=1)
    from click.testing import CliRunner
    out = CliRunner().invoke(cli.main, ["run", "--resume", "r1", "--runs-dir", str(runs)])
    assert out.exit_code == 1, out.output
    assert "tasksorg" in out.output and _no_run == []


def test_preflight_fails_a_mode_all_config_over_such_an_app():
    """The config path (`preflight --json`, which the Docker launcher reads before it
    boots an AVD) says so too."""
    split = preflight.check_mode_all_builds(_suites("tasksorg", "birday"), "all")
    assert split is not None and not split.passed and "tasksorg" in split.detail
    assert "birday" not in split.detail
    assert preflight.check_mode_all_builds(_suites("birday"), "all").passed
    for mode in ("journey", "hunt", "guided"):
        assert preflight.check_mode_all_builds(_suites("tasksorg"), mode) is None


# ── when do the two builds differ ─────────────────────────────────────────────

APP = {"id": "demo"}
SPEC = {"app": APP, "apk": {"filename": "hard/demo-buggy.apk", "sha256": "a" * 64}}


def _journey_block(monkeypatch, meta):
    monkeypatch.setattr(journey, "apk_meta", lambda app_id: dict(meta) if meta else None)
    monkeypatch.delenv("QUALGENTBENCH_APK_DEMO", raising=False)


def test_two_blocks_naming_the_same_bytes_are_one_build(monkeypatch):
    """ankidroid, orgzly and tasksorg on main: journey build == hunt build (identical
    sha256) in two cache slots. `--mode all` scored their journey cases correctly, so it
    must keep running them."""
    _journey_block(monkeypatch, {"filename": "journey/demo-buggy.apk", "sha256": "a" * 64})
    assert preflight.journey_build_differs(APP, SPEC) is False


def test_a_different_journey_build_differs(monkeypatch):
    _journey_block(monkeypatch, {"filename": "journey/demo-buggy.apk", "sha256": "b" * 64})
    assert preflight.journey_build_differs(APP, SPEC) is True


def test_a_journey_block_without_a_hash_cannot_prove_it_is_the_hunt_build(monkeypatch):
    _journey_block(monkeypatch, {"filename": "journey/demo-buggy.apk"})
    assert preflight.journey_build_differs(APP, SPEC) is True
    _journey_block(monkeypatch, {"filename": "journey/demo-buggy.apk", "sha256": "a" * 64})
    assert preflight.journey_build_differs(APP, {"app": APP, "apk_local": "x.apk"}) is True


def test_no_journey_block_means_journey_mode_uses_the_hunt_build(monkeypatch):
    _journey_block(monkeypatch, None)
    assert preflight.journey_build_differs(APP, SPEC) is False


def test_a_local_build_serves_every_mode(monkeypatch, tmp_path):
    """A QUALGENTBENCH_APK_* pin or dist/<id>/buggy.apk wins in every mode, so all three
    kinds install the same file — nothing to refuse."""
    _journey_block(monkeypatch, {"filename": "journey/demo-buggy.apk", "sha256": "b" * 64})
    monkeypatch.setenv("QUALGENTBENCH_APK_DEMO", str(tmp_path / "mine.apk"))
    assert preflight.journey_build_differs(APP, SPEC) is False
    monkeypatch.delenv("QUALGENTBENCH_APK_DEMO")

    import pathlib

    class WithDist(type(pathlib.Path())):
        def exists(self):
            return True if self.parts[-3:] == ("dist", "demo", "buggy.apk") else super().exists()

    monkeypatch.setattr(preflight, "Path", WithDist)
    assert preflight.local_apk(APP) is not None
    assert preflight.journey_build_differs(APP, SPEC) is False


def test_the_answer_matches_what_run_would_install(monkeypatch):
    """Tie the predicate to `cli._resolve_app_apk`, the function that picks the file:
    with a download stubbed to a path per sha256, journey and hunt resolve to the same
    file exactly when the predicate says the builds are one."""
    from qualgentbench import apps

    monkeypatch.setattr(apps, "fetch_seeded_apk",
                        lambda app_id, meta, kind="hunt": Path(f"/fake/{meta.get('sha256')}"))
    real = _suites(*JOURNEY_APPS)
    for suite in real:
        same = (cli._resolve_app_apk(suite["app"], suite, "journey")
                == cli._resolve_app_apk(suite["app"], suite, "hunt"))
        assert same is (not preflight.journey_build_differs(suite["app"], suite)), \
            suite["app"]["id"]
    # And the pre-epic shape, one build in both blocks:
    one = dict(real[0], apk=dict(journey.apk_meta(real[0]["app"]["id"])))
    assert (cli._resolve_app_apk(one["app"], one, "journey")
            == cli._resolve_app_apk(one["app"], one, "hunt"))
    assert preflight.journey_build_differs(one["app"], one) is False


async def test_preflight_runs_the_check_on_a_mode_all_config(monkeypatch, tmp_path):
    """Wired into `run_preflight`, not only defined: a failed check, so the launcher
    refuses before it boots anything."""
    from qualgentbench.config import BenchConfig

    monkeypatch.setenv("QGB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    cfg = BenchConfig.model_validate({
        "agent": "claude-code", "model": "claude-opus-4-8",
        "scope": {"apps": ["tasksorg"], "mode": "all", "trials": 1},
        "devices": {"avds": ["A"]}, "runs_dir": "runs"})
    results, _ = await preflight.run_preflight(cfg, config_dir=tmp_path)
    builds = [r for r in results if r.name == "Builds"]
    assert len(builds) == 1 and builds[0] in preflight.failed(results)
