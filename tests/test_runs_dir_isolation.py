"""QUA-2778: the agent's workspace must not inherit instruction files.

An episode's agent runs with cwd = <runs_dir>/<task>/<run>/workspace. claude-code
loads CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md and .claude/rules/*.md from EVERY
ancestor of its cwd as start-up context (probed with `claude -p /context`, 2026-09-23:
a workspace under this repo listed the repo's CLAUDE.md, 23.7k tokens naming journey
defect ids and mechanisms, plus the parent directory's CLAUDE.md); codex-cli reads
AGENTS.md / AGENTS.override.md from the git root down to its cwd. None of that is a
tool call, so the contamination scanner cannot see it. Host runs therefore default
outside the repo, and every path that starts an agent refuses a runs dir with an
instruction file on its ancestor chain.

All device-free: the episode check fires before the device is touched.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from qualgentbench import cli
from qualgentbench import config as qconfig
from qualgentbench import episode_runner as er
from qualgentbench.config import (
    ALLOW_RUNS_IN_REPO_ENV,
    BenchConfig,
    default_runs_dir,
    instruction_files_on_chain,
    resolve_runs_dir,
    runs_dir_problems,
)
from qualgentbench.contamination import scan
from qualgentbench.preflight import check_runs_dir
from qualgentbench.result import RunResult, VerifierResult, resolve_artifact_dir
from qualgentbench.schemas import Condition
from qualgentbench.task import BenchmarkTask
from qualgentbench.transcript import TranscriptParser

# The repository this test file lives in — independent of config.REPO_ROOT, so moving
# that constant cannot make the tests agree with a wrong answer.
REPO = Path(__file__).resolve().parents[1]


def _under(path: Path, root: Path) -> bool:
    path, root = path.resolve(), root.resolve()
    return path == root or root in path.parents


def _workspace(runs_dir: Path) -> Path:
    """The exact shape episode_runner gives the agent's cwd."""
    return runs_dir / "journey-birday" / "2026-09-23T00-00-00Z_x_claude-code_m_raw_trial-1" \
        / "workspace"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home


# ── the default ───────────────────────────────────────────────────────────────


def test_the_harness_knows_where_its_own_tree_is():
    assert qconfig.REPO_ROOT == REPO


def test_the_default_runs_dir_is_outside_the_repo():
    """Fails if the default ever moves back inside the tree (e.g. to `./runs`)."""
    for d in (default_runs_dir(), resolve_runs_dir(None), resolve_runs_dir("")):
        assert d.is_absolute()
        assert not _under(d, REPO), f"default runs dir {d} is inside the repo {REPO}"
        assert not _under(_workspace(d), REPO)
    assert BenchConfig.model_fields["runs_dir"].default is None


def test_the_default_workspace_has_no_instruction_file_on_its_ancestor_chain(fake_home):
    """Hermetic: a clean home, the real default layout, nothing inherited."""
    ws = _workspace(default_runs_dir())
    assert _under(ws, fake_home)
    assert instruction_files_on_chain(ws) == []
    assert runs_dir_problems(default_runs_dir()) == []


def test_this_machines_default_workspace_inherits_nothing():
    """The real home, unpatched: what a host `run` on this machine would hand the
    agent. A hit inside the repo is always a failure. A hit in the user's own home
    (a non-empty ~/.claude/CLAUDE.md, ~/AGENTS.md) is outside the harness's control
    and `run` refuses the default on that machine anyway — skipped, with the file named,
    rather than failing a suite for a dotfile."""
    ws = _workspace(default_runs_dir())
    hits = instruction_files_on_chain(ws)
    assert not [h for h in hits if _under(h, REPO)], hits
    harness_owned = [h for h in hits if _under(h, default_runs_dir().parent)]
    assert not harness_owned, harness_owned
    if hits:
        pytest.skip(f"this machine's home holds agent instruction files {hits}; "
                    f"`run` refuses the default runs dir here")


# ── the check itself ──────────────────────────────────────────────────────────


def test_an_in_repo_workspace_is_caught_with_the_repos_own_claude_md():
    """The ticket's leak, reproduced: the old default (`./runs` from the repo root)."""
    runs = REPO / "runs"
    hits = instruction_files_on_chain(_workspace(runs))
    assert REPO / "CLAUDE.md" in hits
    problems = runs_dir_problems(runs)
    assert any("inside the harness tree" in p for p in problems)
    assert any(str(REPO / "CLAUDE.md") in p for p in problems)


@pytest.mark.parametrize("rel", [
    "CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/style.md",
    ".claude/rules/nested/deep.md",           # claude-code: every ancestor
    "AGENTS.md", "AGENTS.override.md",        # codex-cli: git root down to cwd
])
def test_every_instruction_file_either_agent_loads_is_found(tmp_path, rel):
    top = tmp_path / "parent"
    f = top / rel
    f.parent.mkdir(parents=True)
    f.write_text("the defect is in the parser\n")
    runs = top / "somewhere" / "runs"
    assert instruction_files_on_chain(_workspace(runs)) == [f]
    assert runs_dir_problems(runs) == [f"{f} would be read by the agent as instructions"]


def test_an_empty_instruction_file_loads_nothing(tmp_path):
    """claude-code lists no memory for an empty CLAUDE.md; ~/.claude/CLAUDE.md is often
    an empty placeholder, and refusing on it would refuse the default for no leak."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("")
    (tmp_path / "CLAUDE.md").write_text("  \n")
    assert instruction_files_on_chain(_workspace(tmp_path / "runs")) == []


def test_a_file_in_the_workspace_itself_counts(tmp_path):
    ws = _workspace(tmp_path / "runs")
    ws.mkdir(parents=True)
    (ws / "AGENTS.md").write_text("hint")
    assert instruction_files_on_chain(ws) == [ws / "AGENTS.md"]


# ── `run`: default, refusal, escape hatch ─────────────────────────────────────


@pytest.fixture
def captured_run(monkeypatch):
    """`run` up to the point it would start the lanes: records the runs dir."""
    seen: list[Path] = []

    async def fake_leaderboard_bugs(models, agent, trials, mcp_server, runs_dir, *a, **k):
        seen.append(runs_dir)
        return []

    monkeypatch.setattr(cli, "_leaderboard_bugs", fake_leaderboard_bugs)
    return seen


def _run(*args: str):
    return CliRunner().invoke(cli.main, ["run", "--agent", "claude-code", "--app", "birday",
                                         "--mode", "hunt", *args])


def test_run_writes_episodes_outside_the_repo_by_default(captured_run, fake_home):
    res = _run()
    assert res.exit_code == 0, res.output
    assert captured_run == [fake_home / ".qualgentbench" / "runs"]
    assert not _under(captured_run[0], REPO)


def test_run_default_with_the_real_home_is_outside_the_repo(captured_run, monkeypatch):
    """Unpatched home: the default a host run actually gets is not in the tree."""
    monkeypatch.setattr(cli, "_gate_runs_dir", lambda *a, **k: None)  # this home's dotfiles
    res = _run()
    assert res.exit_code == 0, res.output
    assert not _under(captured_run[0], REPO)


def test_run_refuses_the_old_in_repo_default(captured_run, monkeypatch):
    monkeypatch.chdir(REPO)
    res = _run("--runs-dir", "runs")
    assert res.exit_code != 0
    assert captured_run == []                      # refused before anything was probed
    flat = "".join(res.output.split())
    assert "CLAUDE.md" in flat and "--allow-runs-in-repo" in flat
    assert "mvruns" in flat                        # how to carry an old tree over


def test_run_refuses_a_runs_dir_under_any_claude_md(captured_run, tmp_path):
    """/Users/gyaan/Work/CLAUDE.md on the owner's machine: outside the repo, still
    an ancestor of a sibling runs dir."""
    (tmp_path / "CLAUDE.md").write_text("workspace notes")
    res = _run("--runs-dir", str(tmp_path / "bench-runs"))
    assert res.exit_code != 0
    assert captured_run == []
    assert str(tmp_path / "CLAUDE.md") in "".join(res.output.split())


def test_run_allows_it_with_the_flag_and_tells_the_episodes(captured_run, monkeypatch):
    import os

    monkeypatch.chdir(REPO)
    res = _run("--runs-dir", "runs", "--allow-runs-in-repo")
    assert res.exit_code == 0, res.output
    assert captured_run == [Path("runs")]
    assert "contaminated" in res.output
    assert os.environ.get(ALLOW_RUNS_IN_REPO_ENV) == "1"


def test_run_accepts_an_explicit_clean_runs_dir(captured_run, tmp_path):
    res = _run("--runs-dir", str(tmp_path / "elsewhere"))
    assert res.exit_code == 0, res.output
    assert captured_run == [tmp_path / "elsewhere"]


# ── the episode re-checks, before the device ──────────────────────────────────


def _episode(runs_dir: Path):
    task = BenchmarkTask(id="birday-hunt", name="t", instruction="do it", app_file_id="",
                         app_name="Birday", platform="android",
                         bundle_id="com.minar.birday", bug_spec={})
    opts = er.EpisodeOptions(agent="claude-code", model="m",
                             condition=Condition.no_routines, trial=1, mcp_server="",
                             runs_dir=runs_dir, device_serial="emulator-1")
    return er.run_episode(task, opts)


def test_an_episode_under_the_repo_never_starts(monkeypatch):
    """A bare run_episode call (no `run` gate in front) is refused before the device
    session exists — the conftest device guard would fail this test otherwise."""
    def no_device(*a, **k):
        raise AssertionError("the device was touched before the runs-dir check")
    monkeypatch.setattr(er, "DeviceSession", no_device)
    with pytest.raises(RuntimeError, match="QUA-2778"):
        asyncio.run(_episode(REPO / "runs"))


def test_an_episode_under_a_parent_agents_md_never_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "DeviceSession", lambda *a, **k: pytest.fail("device touched"))
    (tmp_path / "AGENTS.md").write_text("codex reads this")
    with pytest.raises(RuntimeError, match="AGENTS.md"):
        asyncio.run(_episode(tmp_path / "runs"))


def test_the_escape_hatch_reaches_the_episode(monkeypatch, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    monkeypatch.setenv(ALLOW_RUNS_IN_REPO_ENV, "1")
    assert er._inherited_instructions(tmp_path / "runs") == [
        f"{tmp_path / 'CLAUDE.md'} would be read by the agent as instructions"]


# ── preflight ─────────────────────────────────────────────────────────────────


def _cfg(**kw) -> BenchConfig:
    return BenchConfig(agent="claude-code", model="m", scope={"apps": ["birday"]}, **kw)


def test_preflight_passes_the_default(fake_home):
    r = check_runs_dir(_cfg())
    assert r.passed, r
    assert r.detail == str(fake_home / ".qualgentbench" / "runs")


def test_preflight_fails_an_in_repo_runs_dir(monkeypatch):
    monkeypatch.chdir(REPO)
    r = check_runs_dir(_cfg(runs_dir="runs"))
    assert not r.passed and not r.warning
    assert "CLAUDE.md" in r.detail


def test_the_launcher_preflights_the_containers_runs_dir():
    """In the image the config sits at /app (the harness tree), so its relative
    `runs_dir` would resolve inside /app; the launcher passes the real mount."""
    src = (REPO / "scripts" / "launch.py").read_text()
    assert '"preflight", CONTAINER_CONFIG, "--json", "--runs-dir", CONTAINER_RUNS' in src


# ── the tripwire keeps covering other episodes ────────────────────────────────


def _tx(*calls: tuple[str, dict, str]) -> str:
    lines = []
    for i, (name, inp, result) in enumerate(calls):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result}]}}))
    return "\n".join(lines)


def test_reading_another_episode_outside_the_repo_is_still_contamination():
    """In ./runs another episode's result.json was `benchmark_repo`. Outside the repo
    it would have fallen to the soft `outside_workspace` catch-all."""
    home = "/Users/dev"
    runs = f"{home}/.qualgentbench/runs"
    ws = f"{runs}/explore-birday/2026-09-23T00-00-00Z_b_trial-2/workspace"
    other = f"{runs}/explore-birday/2026-09-22T00-00-00Z_a_trial-1/result.json"
    r = scan(TranscriptParser(_tx(("Read", {"file_path": other}, "{}"))), ws,
             repo_root="/Users/dev/src/qualgentbench-oss", home=home)
    assert r.contaminated and r.reasons == ["other_episode"]

    own = f"{ws}/findings.yaml"
    r = scan(TranscriptParser(_tx(("Read", {"file_path": own}, ""))), ws,
             repo_root="/Users/dev/src/qualgentbench-oss", home=home)
    assert not r.contaminated, r.hard


# ── old runs under ./runs still read ──────────────────────────────────────────


def test_show_still_reads_old_runs_under_dot_runs(monkeypatch, tmp_path):
    """`show --runs-dir runs` (and resolve_artifact_dir) keep working for episodes
    written before the default moved; the in-repo refusal is for STARTING agents."""
    monkeypatch.chdir(tmp_path)
    runs = Path("runs")
    ep = runs / "birday-t1" / "2026-09-08T00-00-00Z_birday-t1_claude-code_m_raw_trial-1"
    ep.mkdir(parents=True)
    t0 = datetime(2026, 9, 8, tzinfo=timezone.utc)
    r = RunResult.build(
        task_id="birday-t1", task_version="v", task_type="bug_task", agent="claude-code",
        model="m", condition="raw", trial=1, started_at=t0,
        ended_at=t0 + timedelta(minutes=1), exit_code=0,
        verifier=VerifierResult(passed=True, score=1.0),
        artifact_dir=ep, runs_dir=runs, run_id="r1")
    r.write(ep / "result.json")
    assert resolve_artifact_dir(runs, r).resolve() == ep.resolve()
    res = CliRunner().invoke(cli.main, ["show", "--runs-dir", "runs", "--agent",
                                        "claude-code"])
    assert "No matching" not in res.output, res.output


def test_show_defaults_to_the_new_runs_dir_and_points_at_the_old_one(monkeypatch, tmp_path,
                                                                     fake_home):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs").mkdir()
    res = CliRunner().invoke(cli.main, ["show", "--agent", "claude-code"])
    assert res.exit_code == 1
    flat = "".join(res.output.split())            # rich wraps long paths
    assert str(fake_home / ".qualgentbench" / "runs") in flat
    assert "--runs-dirruns" in flat


def test_a_runs_root_that_is_a_bare_scratch_dir_does_not_swallow_scratch_work():
    ws = "/tmp/explore-birday/2026-09-23T00-00-00Z_b_trial-2/workspace"
    r = scan(TranscriptParser(_tx(("Write", {"file_path": "/tmp/uihelper.py"}, "ok"))), ws,
             repo_root="/Users/dev/src/qualgentbench-oss", home="/Users/dev")
    assert not r.contaminated, r.hard
