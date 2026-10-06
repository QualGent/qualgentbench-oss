"""codex-cli refuses the operator's account login unless the run opted in (QUA-2868).

With no CODEX_API_KEY / OPENAI_API_KEY the codex adapter used to copy the operator's
own `codex login` into the episode, silently billing a ChatGPT workspace. Only
`run_create_ab.py` refused that; a probe script calling `grader.run_grade` directly ran
an episode on it. The guard now sits in the adapter (`prepare`, `auth_refusal`), in
`run_episode` (staging failure, agent never launched, $0) and up front in every CLI.

Every codex home here is a fake under tmp_path; no real login is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from test_episode_precondition import _drive_episode, _screen

from qualgentbench.adapters import AuthRefused, auth_refusal
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.codex_cli import (ALLOW_LOGIN_ENV, CodexAuthRefused,
                                              CodexCliAdapter)
from qualgentbench.schemas import Condition

_PRESENT = ("Overview", "8:00 AM", "Ibuprofen (2.5)")   # the medtimer precondition holds


@pytest.fixture
def operator_login(tmp_path, monkeypatch) -> Path:
    """A fake operator codex home holding a login, and no API key anywhere."""
    home = tmp_path / "operator_codex"
    home.mkdir()
    (home / "auth.json").write_text('{"fake":"login"}')
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(home))
    # Recorded by monkeypatch so a value the code under test sets is undone afterwards.
    monkeypatch.setenv(ALLOW_LOGIN_ENV, "")
    return home


def _context(run_dir: Path) -> RunContext:
    task = SimpleNamespace(agent=SimpleNamespace(timeout_sec=60))
    return RunContext(task=task, agent="codex-cli", model="gpt-test",  # type: ignore[arg-type]
                      condition=Condition.no_routines, trial=1, run_dir=run_dir,
                      mcp_server="", mcp_config_path=run_dir / "mcp.json",
                      workspace_dir=run_dir / "workspace", disabled_tools=[])


def _fake_codex_login(monkeypatch, *, ok: bool) -> list:
    """Stub `codex login --with-api-key` (the only subprocess prepare starts)."""
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if ok:
            (Path(kw["env"]["CODEX_HOME"]) / "auth.json").write_text('{"from":"api-key"}')
        return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")

    monkeypatch.setattr("qualgentbench.adapters.codex_cli.subprocess.run", fake_run)
    return calls


# ── the adapter ───────────────────────────────────────────────────────────────

def test_no_key_and_no_opt_in_is_refused_before_the_login_is_copied(tmp_path,
                                                                    operator_login):
    ctx = _context(tmp_path / "run")
    refusal = CodexCliAdapter().auth_refusal()
    assert refusal and auth_refusal("codex-cli") == refusal
    # The refusal says exactly how to opt in, and how to avoid needing to.
    for how in (ALLOW_LOGIN_ENV, "--allow-codex-login", "allow_codex_login: true",
                "CODEX_API_KEY"):
        assert how in refusal

    with pytest.raises(CodexAuthRefused) as exc:
        CodexCliAdapter().prepare(ctx)
    assert isinstance(exc.value, AuthRefused) and ALLOW_LOGIN_ENV in str(exc.value)
    assert not list((ctx.run_dir / "codex_home").rglob("auth.json"))
    assert ctx.auth_mode is None


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_the_opt_in_allows_the_login_and_is_recorded(tmp_path, monkeypatch, operator_login,
                                                     value):
    monkeypatch.setenv(ALLOW_LOGIN_ENV, value)
    ctx = _context(tmp_path / "run")
    assert CodexCliAdapter().auth_refusal() is None
    CodexCliAdapter().prepare(ctx)
    assert (ctx.run_dir / "codex_home" / "auth.json").is_file()
    assert (ctx.auth_mode, ctx.auth_login_allowed) == ("account_login", True)


def test_an_api_key_is_unchanged_and_needs_no_opt_in(tmp_path, monkeypatch, operator_login):
    monkeypatch.setenv("CODEX_API_KEY", "sk-fake")
    calls = _fake_codex_login(monkeypatch, ok=True)
    ctx = _context(tmp_path / "run")
    assert CodexCliAdapter().auth_refusal() is None
    CodexCliAdapter().prepare(ctx)
    assert calls and (ctx.auth_mode, ctx.auth_login_allowed) == ("api_key", False)
    assert "fake" not in (ctx.run_dir / "codex_home" / "auth.json").read_text()


def test_a_key_whose_login_fails_does_not_fall_back_to_the_login(tmp_path, monkeypatch,
                                                                 operator_login):
    """`codex login --with-api-key` failing used to fall through to the copied login."""
    monkeypatch.setenv("CODEX_API_KEY", "sk-fake")
    _fake_codex_login(monkeypatch, ok=False)
    with pytest.raises(CodexAuthRefused):
        CodexCliAdapter().prepare(_context(tmp_path / "run"))


def test_no_key_and_no_login_is_not_this_guard(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(tmp_path / "nowhere"))
    assert auth_refusal("codex-cli") is None
    ctx = _context(tmp_path / "run")
    CodexCliAdapter().prepare(ctx)
    assert ctx.auth_mode == "none"


def test_other_agents_are_never_refused(operator_login):
    assert auth_refusal("claude-code") is None and auth_refusal("native") is None


# ── run_episode: a staging failure, the agent never launched ──────────────────

class _CodexStub(CodexCliAdapter):
    """The real codex adapter (its own `auth_refusal` and `prepare`) with the agent
    process replaced by nothing."""

    def __init__(self):
        self.launches = 0

    async def run(self, instruction, context):
        self.launches += 1
        self.prepare(context)
        self.cleanup(context)
        return "", 0


async def test_run_episode_refuses_the_login_as_a_staging_failure(monkeypatch, tmp_path,
                                                                   operator_login):
    from qualgentbench import failures

    adapter = _CodexStub()
    _, episode, reads, task = _drive_episode(monkeypatch, tmp_path, _screen(*_PRESENT),
                                             agent="codex-cli", adapter=adapter)
    result = await episode

    assert adapter.launches == 0, "the agent was launched on the operator's login"
    assert ALLOW_LOGIN_ENV in task.bug_spec["staging_failed"]
    assert result.metrics["env_failure"] is True and failures.is_excluded(result.metrics)
    assert result.metrics["agent_launched"] is False
    assert (result.metrics["cost_usd"], result.metrics["cost_source"]) == (0.0, "not_launched")
    assert reads == []


async def test_run_episode_with_the_opt_in_records_it_in_provenance(monkeypatch, tmp_path,
                                                                     operator_login):
    monkeypatch.setenv(ALLOW_LOGIN_ENV, "1")
    adapter = _CodexStub()
    _, episode, _, task = _drive_episode(monkeypatch, tmp_path, _screen(*_PRESENT),
                                         agent="codex-cli", adapter=adapter)
    result = await episode

    assert adapter.launches == 1 and "staging_failed" not in task.bug_spec
    assert result.provenance["agent_auth"] == "account_login"
    assert result.provenance["allow_codex_login"] is True
    on_disk = json.loads(next((tmp_path / "runs").rglob("result.json")).read_text())
    assert on_disk["provenance"]["allow_codex_login"] is True


# ── grader.run_grade, called directly (the QUA-2864 probe's path) ─────────────

class _Touched(Exception):
    """The grade got past the auth guard to the corpus / device."""


def _forbid_device(monkeypatch) -> None:
    """The first thing a grade does past the guard is resolve the APK, then open a
    device session; either one means the guard let it through."""
    def touched(*_a, **_kw):
        raise _Touched
    monkeypatch.setattr("qualgentbench.cli._resolve_app_apk", touched)
    monkeypatch.setattr("qualgentbench.session.DeviceSession", touched)


def _graded_plan():
    from qualgentbench.create import grader
    case = "anki-study-first-card"
    plan = grader.plan_grade(grader.reference_case("ankidroid", case), case)
    assert plan.status == grader.GRADED
    return plan


async def test_run_grade_called_directly_refuses_without_a_key(monkeypatch, tmp_path,
                                                               operator_login):
    from qualgentbench.create import grader

    _forbid_device(monkeypatch)
    with pytest.raises(AuthRefused, match=ALLOW_LOGIN_ENV):
        await grader.run_grade(_graded_plan(), agent="codex-cli", model="gpt-test",
                               mcp_server="http://127.0.0.1:1", device="emulator-5554",
                               runs_dir=tmp_path / "runs")
    assert not (tmp_path / "runs").exists(), "a refused grade wrote a manifest"


async def test_run_grade_with_the_opt_in_goes_on(monkeypatch, tmp_path, operator_login):
    from qualgentbench.create import grader

    monkeypatch.setenv(ALLOW_LOGIN_ENV, "1")
    _forbid_device(monkeypatch)
    with pytest.raises(_Touched):
        await grader.run_grade(_graded_plan(), agent="codex-cli", model="gpt-test",
                               mcp_server="http://127.0.0.1:1", device="emulator-5554",
                               runs_dir=tmp_path / "runs")


def _grader_run_argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["run", "--reference", "--case", "anki-study-first-card", "--model", "gpt-test",
            "--mcp-server", "http://127.0.0.1:1", "--device", "emulator-5554",
            "--runs-dir", str(tmp_path / "runs"), "--yes", *extra]


def test_grader_cli_refuses_and_says_how_to_opt_in(monkeypatch, tmp_path, operator_login,
                                                   capsys):
    from qualgentbench.create import grader

    monkeypatch.chdir(tmp_path)               # no `.env` with a key in the cwd
    _forbid_device(monkeypatch)
    assert grader.main(_grader_run_argv(tmp_path)) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused:") and "--allow-codex-login" in err


def test_grader_cli_allow_codex_login_opts_in(monkeypatch, tmp_path, operator_login):
    import os

    from qualgentbench.create import grader

    monkeypatch.chdir(tmp_path)
    _forbid_device(monkeypatch)
    with pytest.raises(_Touched):
        grader.main(_grader_run_argv(tmp_path, "--allow-codex-login"))
    assert os.environ[ALLOW_LOGIN_ENV] == "1"


# ── qualgent-bench run / preflight / doctor ────────────────────────────────────

def test_run_gate_refuses_the_login_and_the_flag_opts_in(operator_login, capsys):
    import os

    from qualgentbench import cli

    with pytest.raises(click.ClickException) as exc:
        cli._gate_agent_auth("codex-cli")
    assert ALLOW_LOGIN_ENV in exc.value.message and "--allow-codex-login" in exc.value.message
    cli._gate_agent_auth("claude-code")        # never this guard's business

    cli._gate_agent_auth("codex-cli", allow_codex_login=True)
    assert os.environ[ALLOW_LOGIN_ENV] == "1"


def test_run_cli_refuses_codex_on_the_login_before_any_probe(monkeypatch, tmp_path,
                                                             operator_login):
    from click.testing import CliRunner

    from qualgentbench import cli

    monkeypatch.chdir(tmp_path)
    probed: list = []
    monkeypatch.setattr(cli, "_leaderboard_bugs", lambda *a, **kw: probed.append(a))
    monkeypatch.setattr(cli, "_run_async", lambda coro: None)
    args = ["run", "--agent", "codex-cli", "--models", "gpt-test", "--mode", "hunt",
            "--app", "birday", "--runs-dir", str(tmp_path / "runs")]
    refused = CliRunner().invoke(cli.main, args)
    assert refused.exit_code != 0 and ALLOW_LOGIN_ENV in refused.output
    allowed = CliRunner().invoke(cli.main, [*args, "--allow-codex-login"])
    assert allowed.exit_code == 0, allowed.output
    assert "account login" in allowed.output


def test_bench_config_carries_the_opt_in():
    from qualgentbench.config import BenchConfig

    base = dict(agent="codex-cli", model="m", scope={"apps": ["x"]})
    assert BenchConfig(**base).allow_codex_login is False
    assert BenchConfig(**base, allow_codex_login=True).allow_codex_login is True


def test_doctor_fails_the_login_without_the_opt_in(monkeypatch, operator_login):
    from qualgentbench.doctor import check_codex_auth

    refused = check_codex_auth()
    assert refused.passed is False and "account login" in refused.detail
    assert ALLOW_LOGIN_ENV in (refused.fix or "") and "CODEX_API_KEY" in (refused.fix or "")

    monkeypatch.setenv(ALLOW_LOGIN_ENV, "1")
    allowed = check_codex_auth()
    assert allowed.passed is True and allowed.warning is True
    assert "NO API key" in allowed.detail and ALLOW_LOGIN_ENV in allowed.detail
