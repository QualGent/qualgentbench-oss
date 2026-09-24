"""`run`'s `Continue?` gate when nobody is at a terminal (QUA-2798).

A worker piped `n` into `qualgent-bench run` to preview a plan; stdin was not a TTY,
the old gate skipped the question, and the paid board started anyway. The contract
pinned here, at the CLI seam with a stubbed corpus, no device and a fake lane engine
standing in for the launcher:

* without a terminal, `run` without `--yes` prints the plan and refuses — for a piped
  `n`, a piped `y` and an empty stdin alike — and the lane engine is never called;
* the refusal leaves no plan.json, so there is no run id to `--resume` into a sweep;
* `--resume` without `--yes` is refused the same way;
* at a terminal, `n` declines and `y` starts: the stubs are real enough to launch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from qualgentbench import bugs, cli, lanes, session

SUITE = {
    "app": {"id": "birday", "name": "Birday", "package": "com.birday",
            "platform": "android", "difficulty": "easy"},
    "apk": {"repo": "qualgent/qualgentbench-apps", "filename": "easy/birday.apk",
            "sha256": "a" * 64},
    "exploration": {"id": "explore-birday", "features": [{"id": "a", "state": "broken"}]},
    "tasks": [{"id": f"birday-t{i}", "bug_id": f"b{i}", "type": "bug"} for i in range(1, 3)],
}
SERIAL = "emulator-5666"


class _Launcher:
    """The lane engine: the only thing that starts an agent. Records, never runs."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, plan, cfg):
        self.calls += 1
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
def launcher(monkeypatch, tmp_path: Path) -> _Launcher:
    apk = tmp_path / "birday.apk"
    apk.write_bytes(b"apk")
    engine = _Launcher()
    monkeypatch.setattr(bugs, "load_apps", lambda *a, **kw: [SUITE])
    monkeypatch.setattr(cli, "_resolve_app_apk", lambda app, spec=None, mode="hunt": apk)
    monkeypatch.setattr(cli, "_preflight", _nothing)
    monkeypatch.setattr(cli, "_gate_agent_dump", _nothing)
    monkeypatch.setattr(session, "DeviceSession", _FakeSession)
    monkeypatch.setattr(lanes, "run_lanes", engine)
    return engine


def _run(tmp_path: Path, *args: str, stdin: str | None = None):
    runs = tmp_path / "runs"
    return CliRunner().invoke(
        cli.main,
        ["run", "--agent", "codex-cli", "--models", "gpt-5.5", "--mode", "hunt",
         "--app", "birday", "--devices", SERIAL, "--plain", "--runs-dir", str(runs),
         "--run-id-file", str(tmp_path / "run_id"), *args],
        input=stdin)


def _plans(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "runs").glob("_runs/*/plan.json"))


@pytest.mark.parametrize("stdin", ["n\n", "y\n", "", None],
                         ids=["piped-n", "piped-y", "empty", "no-input"])
def test_without_a_terminal_run_refuses_without_yes(launcher, tmp_path, stdin):
    out = _run(tmp_path, stdin=stdin)
    assert out.exit_code == 1, out.output
    assert "Not started" in out.output and "--yes" in out.output
    assert "preflight CONFIG --plan" in out.output
    assert launcher.calls == 0
    # No plan left behind for a run that never started.
    assert _plans(tmp_path) == []


def test_without_a_terminal_yes_starts(launcher, tmp_path):
    out = _run(tmp_path, "--yes", stdin="n\n")
    assert launcher.calls == 1, out.output
    assert len(_plans(tmp_path)) == 1


def test_without_a_terminal_resume_refuses_without_yes(launcher, tmp_path):
    # A first sitting that started (with --yes) and ran nothing leaves an owed plan.
    _run(tmp_path, "--yes")
    assert launcher.calls == 1
    run_id = (tmp_path / "run_id").read_text().strip()
    out = CliRunner().invoke(cli.main, ["run", "--resume", run_id, "--devices", SERIAL,
                                        "--plain", "--runs-dir", str(tmp_path / "runs")],
                             input="n\n")
    assert out.exit_code == 1, out.output
    assert "Not started" in out.output
    assert launcher.calls == 1


@pytest.mark.parametrize("answer,starts", [("n\n", False), ("y\n", True), ("\n", True)],
                         ids=["n", "y", "default"])
def test_at_a_terminal_the_answer_decides(launcher, tmp_path, monkeypatch, answer, starts):
    monkeypatch.setattr(cli, "_stdin_is_a_terminal", lambda: True)
    out = _run(tmp_path, stdin=answer)
    assert "Continue?" in out.output
    assert launcher.calls == int(starts), out.output
    if not starts:
        assert out.exit_code == 1 and _plans(tmp_path) == []


def test_eof_at_the_prompt_declines(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_a_terminal", lambda: True)
    out = _run(tmp_path, stdin="")
    assert out.exit_code == 1 and launcher.calls == 0


def test_a_closed_stdin_is_not_a_terminal(monkeypatch):
    class _Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(cli.sys, "stdin", _Closed())
    assert cli._stdin_is_a_terminal() is False
    monkeypatch.setattr(cli.sys, "stdin", None)
    assert cli._stdin_is_a_terminal() is False
