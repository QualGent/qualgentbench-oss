"""claude-code gets a per-run CLAUDE_CONFIG_DIR, like codex's per-run CODEX_HOME:
N lanes never share a session, and nothing of a run outlives it. Auth is an env
token only — the interactive login is bound to the user's own config dir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter


def _ctx(tmp_path: Path, name: str = "run") -> RunContext:
    run_dir = tmp_path / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return RunContext(task=None, agent="claude-code", model="m", condition="raw", trial=1,
                      run_dir=run_dir, mcp_server="", mcp_config_path=run_dir / "mcp.json",
                      workspace_dir=run_dir / "ws")


@pytest.fixture
def source_home(tmp_path, monkeypatch):
    src = tmp_path / "user-claude"
    src.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    return src


@pytest.fixture(autouse=True)
def _no_tokens(monkeypatch):
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_each_run_gets_its_own_config_dir(tmp_path, source_home):
    a, b = _ctx(tmp_path, "a"), _ctx(tmp_path, "b")
    adapter = ClaudeCodeAdapter()
    adapter.prepare(a)
    adapter.prepare(b)
    assert adapter.env(a)["CLAUDE_CONFIG_DIR"] == str(a.run_dir / "claude_home")
    assert adapter.env(b)["CLAUDE_CONFIG_DIR"] == str(b.run_dir / "claude_home")
    assert adapter.env(a)["CLAUDE_CONFIG_DIR"] != adapter.env(b)["CLAUDE_CONFIG_DIR"]
    assert adapter.env(a)["CLAUDE_CONFIG_DIR"] != str(source_home)


def test_config_dir_is_seeded_so_the_cli_never_prompts(tmp_path, source_home):
    ctx = _ctx(tmp_path)
    ClaudeCodeAdapter().prepare(ctx)
    cfg = json.loads((ctx.run_dir / "claude_home" / ".claude.json").read_text())
    assert cfg["hasCompletedOnboarding"] is True
    assert cfg["bypassPermissionsModeAccepted"] is True


def test_the_users_login_is_never_copied(tmp_path, source_home):
    """A copied refresh token would race the user's own session (and N lanes
    each other) on rotation — auth is an env token, full stop."""
    (source_home / ".credentials.json").write_text('{"claudeAiOauth": {"refreshToken": "r"}}')
    (source_home / "settings.json").write_text('{"hooks": {"PreToolUse": []}}')
    (source_home / "projects").mkdir()
    ctx = _ctx(tmp_path)
    ClaudeCodeAdapter().prepare(ctx)
    assert sorted(p.name for p in (ctx.run_dir / "claude_home").iterdir()) == [".claude.json"]


def test_auth_source_names_the_variable_or_nothing(monkeypatch):
    assert ClaudeCodeAdapter.auth_source() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert ClaudeCodeAdapter.auth_source() == "ANTHROPIC_API_KEY"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert ClaudeCodeAdapter.auth_source() == "CLAUDE_CODE_OAUTH_TOKEN"
    assert "claude setup-token" in ClaudeCodeAdapter.auth_fix()
