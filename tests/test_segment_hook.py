"""The segment-end hook (QUA-2842): an operator command run after every segment.

Pinned here, at `run`'s CLI seam with a stubbed corpus and a fake lane engine (no
device, no agent), and on the hook runner itself:

* a completed segment, a credit stop (75) and a failure each run the hook exactly
  once, with the right QGB_HOOK_* environment, after board.json is written;
* a failing or hanging hook is logged (and a hanging one killed) without changing
  `run`'s exit code;
* the hook is never run from the runs tree, is not run by a `run` that never started
  a segment, and is left to the launcher inside the image;
* the hook's environment is the operator's shell, not what `.env` added;
* `scripts/launch.py`'s stdlib copy gives the same answers as the harness module.

The launcher side of the same contract is in `tests/test_launcher_loop.py`.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from qualgentbench import bugs, cli, credit, lanes, segment_hook, session
from qualgentbench.result import RunResult, VerifierResult

_LAUNCH_PY = Path(__file__).resolve().parents[1] / "scripts" / "launch.py"
_spec = importlib.util.spec_from_file_location("qgb_launch_hook", _LAUNCH_PY)
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)

SUITE = {
    "app": {"id": "birday", "name": "Birday", "package": "com.birday",
            "platform": "android", "difficulty": "easy"},
    "apk": {"repo": "qualgent/qualgentbench-apps", "filename": "easy/birday.apk",
            "sha256": "a" * 64},
    "exploration": {"id": "explore-birday", "features": [{"id": "a", "state": "broken"}]},
    "tasks": [{"id": f"birday-t{i}", "bug_id": f"b{i}", "type": "bug"} for i in range(1, 3)],
}
SERIAL = "emulator-5666"

# What the hook writes: one JSON line per invocation with its QGB_HOOK_* environment,
# the two probe variables and whether board.json / stop.json existed when it ran.
HOOK_SCRIPT = """\
import json, os, pathlib, sys
env = {k: v for k, v in os.environ.items()
       if k.startswith("QGB_HOOK_") or k in ("QGB_TEST_SHELL_VAR", "QGB_TEST_DOTENV_TOKEN")}
meta = pathlib.Path(env["QGB_HOOK_RUNS_DIR"]) / "_runs" / env["QGB_HOOK_RUN_ID"]
env["board_exists"] = (meta / "board.json").is_file()
env["stop_exists"] = (meta / "stop.json").is_file()
with open(sys.argv[1], "a") as fh:
    fh.write(json.dumps(env) + "\\n")
"""


def _hook(tmp_path: Path) -> tuple[str, Path]:
    script = tmp_path / "hook" / "record.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(HOOK_SCRIPT)
    out = tmp_path / "hook" / "calls.jsonl"
    cmd = " ".join(shlex.quote(str(p)) for p in (sys.executable, script, out))
    return cmd, out


def _calls(out: Path) -> list[dict]:
    if not out.exists():
        return []
    return [json.loads(line) for line in out.read_text().splitlines() if line.strip()]


def _result(runs_dir: Path, run_id: str, task_id: str) -> RunResult:
    episode = runs_dir / task_id / f"ep_{task_id}"
    episode.mkdir(parents=True, exist_ok=True)
    t0 = datetime(2026, 9, 30, tzinfo=UTC)
    result = RunResult.build(
        task_id=task_id, task_version="v1", task_type="bug_task", agent="codex-cli",
        model="gpt-5.5", condition="raw", trial=1, started_at=t0,
        ended_at=t0 + timedelta(minutes=1), exit_code=0,
        verifier=VerifierResult(passed=True, score=1.0, metrics={}),
        artifact_dir=episode, runs_dir=runs_dir, run_id=run_id,
        provenance={"device": SERIAL, "lane": 0})
    result.write(episode / "result.json")
    return result


class _Engine:
    """Stands in for `run_lanes`. `how` is what the segment does: finish, stop on
    credits (the guard decides, exactly as a lane would make it), or blow up."""

    def __init__(self) -> None:
        self.how, self.calls, self.segments = "complete", 0, []

    async def __call__(self, plan, cfg):
        self.calls += 1
        self.segments.append(cfg.segment)
        if self.how == "fail":
            raise RuntimeError("lane engine exploded")
        unit = plan.units[0]
        cfg.results.append(_result(cfg.runs_dir, cfg.run_id, unit.task_id))
        if self.how == "stop":
            cfg.guard.decision = credit.StopDecision(
                reason=credit.REASON_SEVEN_DAY, message="weekly budget reached",
                payload={"window": "seven_day", "utilization_pct": 91.0})
        return cfg.results


class _FakeSession:
    def __init__(self, mcp_server=None) -> None:
        pass

    async def first_available_device(self):
        return SERIAL

    async def available_devices(self):
        return [SERIAL]

    async def is_healthy(self):
        return True


async def _nothing(*args, **kwargs):
    return None


@pytest.fixture
def engine(monkeypatch, tmp_path: Path) -> _Engine:
    apk = tmp_path / "birday.apk"
    apk.write_bytes(b"apk")
    eng = _Engine()
    monkeypatch.setattr(bugs, "load_apps", lambda *a, **kw: [SUITE])
    monkeypatch.setattr(cli, "_resolve_app_apk", lambda app, spec=None, mode="hunt": apk)
    monkeypatch.setattr(cli, "_preflight", _nothing)
    monkeypatch.setattr(cli, "_gate_agent_dump", _nothing)
    monkeypatch.setattr(cli, "_write_run_view", lambda runs_dir, run_id: None)
    monkeypatch.setattr(session, "DeviceSession", _FakeSession)
    monkeypatch.setattr(lanes, "run_lanes", eng)
    return eng


def _run(tmp_path: Path, *args: str, stdin: str | None = None):
    return CliRunner().invoke(
        cli.main,
        ["run", "--agent", "codex-cli", "--models", "gpt-5.5", "--mode", "hunt",
         "--app", "birday", "--devices", SERIAL, "--plain",
         "--runs-dir", str(tmp_path / "runs"),
         "--run-id-file", str(tmp_path / "run_id"), *args],
        input=stdin)


def _run_id(tmp_path: Path) -> str:
    return (tmp_path / "run_id").read_text().strip()


# ── run: once per segment, with the right environment ─────────────────────────


def test_a_completed_segment_runs_the_hook_once_after_the_board(engine, tmp_path):
    cmd, out = _hook(tmp_path)
    res = _run(tmp_path, "--yes", "--on-segment-end", cmd)

    assert res.exit_code == 0, res.output
    [call] = _calls(out)
    run_id = _run_id(tmp_path)
    assert call["QGB_HOOK_RUN_ID"] == run_id
    assert call["QGB_HOOK_SEGMENT"] == "0"
    assert call["QGB_HOOK_RUNS_DIR"] == str((tmp_path / "runs").resolve())
    assert call["QGB_HOOK_OUTCOME"] == "complete"
    assert call["QGB_HOOK_STOP_JSON"] == ""
    assert call["board_exists"] is True


def test_a_credit_stop_runs_the_hook_with_the_stop_file_and_still_exits_75(
        engine, tmp_path):
    engine.how = "stop"
    cmd, out = _hook(tmp_path)
    res = _run(tmp_path, "--yes", "--on-segment-end", cmd)

    assert res.exit_code == credit.EXIT_STOPPED, res.output
    [call] = _calls(out)
    run_id = _run_id(tmp_path)
    assert call["QGB_HOOK_OUTCOME"] == "stopped:seven_day_threshold"
    stop = credit.stop_path(tmp_path / "runs", run_id)
    assert call["QGB_HOOK_STOP_JSON"] == str(stop)
    assert call["stop_exists"] is True and call["board_exists"] is True


def test_a_failed_segment_runs_the_hook_with_the_exit_code(engine, tmp_path):
    engine.how = "fail"
    cmd, out = _hook(tmp_path)
    res = _run(tmp_path, "--yes", "--on-segment-end", cmd)

    assert res.exit_code == 1
    [call] = _calls(out)
    assert call["QGB_HOOK_OUTCOME"] == "failed:1"
    assert call["QGB_HOOK_STOP_JSON"] == ""


def test_a_resumed_segment_reports_its_own_segment_number(engine, tmp_path):
    cmd, out = _hook(tmp_path)
    engine.how = "stop"
    # Two trials, one done per segment, so the resume has a unit left to run.
    assert _run(tmp_path, "--yes", "--trials", "2", "--on-segment-end", cmd).exit_code == 75
    run_id = _run_id(tmp_path)
    engine.how = "complete"
    res = CliRunner().invoke(cli.main, [
        "run", "--resume", run_id, "--devices", SERIAL, "--plain", "--yes",
        "--runs-dir", str(tmp_path / "runs"), "--on-segment-end", cmd])

    assert res.exit_code == 0, res.output
    first, second = _calls(out)
    assert (first["QGB_HOOK_SEGMENT"], second["QGB_HOOK_SEGMENT"]) == ("0", "1")
    assert engine.segments == [0, 1]
    assert {first["QGB_HOOK_RUN_ID"], second["QGB_HOOK_RUN_ID"]} == {run_id}
    assert second["QGB_HOOK_OUTCOME"] == "complete"


def test_the_config_names_the_hook_and_dotenv_tokens_never_reach_it(
        engine, tmp_path, monkeypatch):
    """`on_segment_end:` in the config, and the hook's environment is the shell's: a
    token `run` read from the config's env_file stays with the harness."""
    cmd, out = _hook(tmp_path)
    (tmp_path / ".env").write_text("QGB_TEST_DOTENV_TOKEN=sk-NOTREAL\n")
    config = tmp_path / "bench.config.yaml"
    config.write_text(
        "agent: codex-cli\nmodel: gpt-5.5\nscope:\n  apps: [birday]\n  mode: hunt\n"
        f"env_file: .env\non_segment_end: {json.dumps(cmd)}\n")
    monkeypatch.setenv("QGB_TEST_SHELL_VAR", "from-the-shell")
    monkeypatch.delenv("QGB_TEST_DOTENV_TOKEN", raising=False)
    res = CliRunner().invoke(cli.main, [
        "run", "--config", str(config), "--devices", SERIAL, "--plain", "--yes",
        "--runs-dir", str(tmp_path / "runs")])
    monkeypatch.delenv("QGB_TEST_DOTENV_TOKEN", raising=False)

    assert res.exit_code == 0, res.output
    [call] = _calls(out)
    assert call["QGB_TEST_SHELL_VAR"] == "from-the-shell"
    assert "QGB_TEST_DOTENV_TOKEN" not in call


def test_an_empty_flag_turns_a_configured_hook_off(engine, tmp_path):
    cmd, out = _hook(tmp_path)
    config = tmp_path / "bench.config.yaml"
    config.write_text(
        "agent: codex-cli\nmodel: gpt-5.5\nscope:\n  apps: [birday]\n  mode: hunt\n"
        f"on_segment_end: {json.dumps(cmd)}\n")
    res = CliRunner().invoke(cli.main, [
        "run", "--config", str(config), "--devices", SERIAL, "--plain", "--yes",
        "--runs-dir", str(tmp_path / "runs"), "--on-segment-end", ""])

    assert res.exit_code == 0, res.output
    assert _calls(out) == []


# ── run: a bad hook changes nothing ───────────────────────────────────────────


def test_a_failing_hook_is_logged_and_does_not_change_the_exit_code(engine, tmp_path):
    hook = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "import sys; sys.stderr.write('push refused: no lease\\n'); sys.exit(7)")
    res = _run(tmp_path, "--yes", "--on-segment-end", hook)

    assert res.exit_code == 0, res.output
    assert "segment-end hook exited 7" in res.output
    assert "push refused: no lease" in res.output


def test_a_hanging_hook_is_killed_at_its_timeout_and_the_stop_code_stands(
        engine, tmp_path):
    engine.how = "stop"
    hook = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'"
    started = time.monotonic()
    res = _run(tmp_path, "--yes", "--on-segment-end", hook,
               "--on-segment-end-timeout", "1")

    assert res.exit_code == credit.EXIT_STOPPED, res.output
    assert time.monotonic() - started < 30
    assert "timed out after 1s" in res.output


# ── run: when the hook is not run ─────────────────────────────────────────────


def test_a_hook_in_the_runs_tree_is_refused_before_anything_starts(engine, tmp_path):
    planted = tmp_path / "runs" / "birday" / "push.sh"
    planted.parent.mkdir(parents=True)
    planted.write_text("#!/bin/sh\necho planted\n")
    planted.chmod(0o755)
    res = _run(tmp_path, "--yes", "--on-segment-end", str(planted))

    assert res.exit_code != 0
    assert "runs tree" in res.output
    assert engine.calls == 0


def test_a_run_that_never_started_a_segment_runs_no_hook(engine, tmp_path):
    cmd, out = _hook(tmp_path)
    res = _run(tmp_path, "--on-segment-end", cmd, stdin="n\n")   # no tty, no --yes

    assert res.exit_code == 1 and engine.calls == 0
    assert _calls(out) == []


def test_inside_the_image_the_hook_is_left_to_the_launcher(engine, tmp_path, monkeypatch):
    cmd, out = _hook(tmp_path)
    monkeypatch.setenv(segment_hook.ON_HOST_ENV, "1")
    res = _run(tmp_path, "--yes", "--on-segment-end", cmd)

    assert res.exit_code == 0, res.output
    assert _calls(out) == []
    assert "left to the launcher" in res.output


# ── the runner and the two copies ─────────────────────────────────────────────


@pytest.mark.parametrize("rc,stop,want", [
    (0, None, "complete"),
    (0, {"reason": "five_hour_limit"}, "complete"),
    (75, {"reason": "five_hour_limit"}, "stopped:five_hour_limit"),
    (75, {"reason": "seven_day_threshold"}, "stopped:seven_day_threshold"),
    (75, None, "failed:75"),
    (75, {"reason": ""}, "failed:75"),
    (1, None, "failed:1"),
    (130, {"reason": "five_hour_limit"}, "failed:130"),
])
def test_outcome_is_the_same_in_both_copies(rc, stop, want):
    assert segment_hook.outcome(rc, stop) == launch.hook_outcome(rc, stop) == want


def test_the_environment_is_the_same_in_both_copies(tmp_path):
    base = {"PATH": "/usr/bin", "QGB_HOOK_OUTCOME": "stale", "HOME": "/home/op"}
    kw = {"run_id": "r1", "segment": 2, "runs_dir": tmp_path, "outcome": "complete",
          "stop_json": None}
    env = segment_hook.hook_env(base, **kw)
    assert env == launch.hook_env(base, **kw)
    assert env["QGB_HOOK_OUTCOME"] == "complete"          # the stale value is gone
    assert env["QGB_HOOK_SEGMENT"] == "2" and env["QGB_HOOK_STOP_JSON"] == ""
    assert env["PATH"] == "/usr/bin"
    assert segment_hook.hook_env(base, **{**kw, "segment": None})["QGB_HOOK_SEGMENT"] == ""


@pytest.mark.parametrize("where", ["program", "cwd", "relative", "outside", "unparsable"])
def test_problems_are_the_same_in_both_copies(tmp_path, where):
    runs = tmp_path / "runs"
    (runs / "case").mkdir(parents=True)
    command, cwd = {
        "program": (f"{runs / 'case' / 'x.sh'} --flag", tmp_path),
        "cwd": ("echo hi", runs / "case"),
        "relative": ("./runs/case/x.sh", tmp_path),
        "outside": ("echo $QGB_HOOK_RUN_ID", tmp_path),
        "unparsable": ("echo 'unterminated", tmp_path),
    }[where]
    got = segment_hook.problems(command, runs, cwd)
    assert got == launch.hook_problems(command, runs, cwd)
    assert bool(got) is (where != "outside")


def test_the_runner_reports_what_happened_and_never_raises(tmp_path):
    lines: list[str] = []
    env = segment_hook.hook_env({"PATH": "/usr/bin:/bin"}, run_id="r1", segment=0,
                                runs_dir=tmp_path / "runs", outcome="complete",
                                stop_json=None)
    ok = segment_hook.run("echo $QGB_HOOK_RUN_ID >&2", env=env,
                          runs_dir=tmp_path / "runs", log=lines.append)
    assert (ok.ran, ok.returncode, ok.timed_out) == (True, 0, False)
    assert ok.stderr == "r1"
    assert any("hook stderr: r1" in line for line in lines)

    hung = segment_hook.run("sleep 30", env=env, runs_dir=tmp_path / "runs",
                            timeout_sec=0.5, log=lines.append)
    assert hung.timed_out and hung.ran

    missing = segment_hook.run("/nonexistent/qgb-hook", env=env,
                               runs_dir=tmp_path / "runs", log=lines.append)
    assert missing.ran and missing.returncode not in (0, None)
