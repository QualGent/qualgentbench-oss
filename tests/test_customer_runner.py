"""Tests for the customer trial verdict + per-condition tool gating."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qualgentbench import episode_runner
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.episode_runner import _disabled_tools, _verdict
from qualgentbench.doctor import check_codex_auth
from qualgentbench.result import RunResult, VerifierResult
from qualgentbench.schemas import Condition


def _transcript(*, status: str | None, observe: bool, tap: bool) -> str:
    """Build a minimal Claude stream-json transcript."""
    lines: list[dict] = []

    def call(tid, name, inp, result_text):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp},
        ]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": [{"type": "text", "text": result_text}]},
        ]}})

    if observe:
        call("t1", "mobile_observe_screen", {"device": "emu"}, '{"screen": "Home feed"}')
    if tap:
        call("t2", "mobile_tap_and_observe", {"element": "Login"}, '{"screen_changed": true}')
    if status:
        call("t3", "mobile_report_result", {"status": status}, "ok")
    lines.append({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50},
                  "total_cost_usd": 0.01})
    return "\n".join(json.dumps(x) for x in lines)


def test_verdict_pass_with_evidence():
    v = _verdict(_transcript(status="PASS", observe=True, tap=True), "claude-sonnet-4-6")
    assert v.passed is True
    assert v.criteria["reported_pass"] is True
    assert v.criteria["evidence_attached"] is True
    assert v.metrics["reported_status"] == "PASS"
    assert v.metrics["cost_usd"] == 0.01  # reported cost preferred


def test_verdict_pass_without_evidence_fails_tripwire():
    v = _verdict(_transcript(status="PASS", observe=False, tap=False), "claude-sonnet-4-6")
    assert v.passed is False
    assert v.criteria["reported_pass"] is True
    assert v.criteria["evidence_attached"] is False
    assert "no device evidence" in (v.failure_reason or "")


def test_verdict_fail_status():
    v = _verdict(_transcript(status="FAIL", observe=True, tap=True), "claude-sonnet-4-6")
    assert v.passed is False
    assert v.criteria["reported_pass"] is False


def test_verdict_no_report():
    v = _verdict(_transcript(status=None, observe=True, tap=True), "claude-sonnet-4-6")
    assert v.passed is False
    assert "never called mobile_report_result" in (v.failure_reason or "")


def test_disabled_tools_comes_only_from_the_environment(monkeypatch):
    """QGB_DISALLOWED_TOOLS is the only source — no hardcoded default."""
    monkeypatch.delenv("QGB_DISALLOWED_TOOLS", raising=False)
    assert _disabled_tools() == []
    monkeypatch.setenv("QGB_DISALLOWED_TOOLS", "find_routine, apply_routine ,find_routine")
    assert _disabled_tools() == ["find_routine", "apply_routine"]     # trimmed, deduped
    monkeypatch.setenv("QGB_DISALLOWED_TOOLS", "")
    assert _disabled_tools() == []


def _ctx(tmp_path: Path, *, condition, inject: bool) -> RunContext:
    return RunContext(
        task=None, agent="x", model="m", condition=condition,
        trial=1, run_dir=tmp_path, mcp_server="http://x",
        mcp_config_path=tmp_path / "mcp.json", workspace_dir=tmp_path,
        disabled_tools=_disabled_tools(), inject_mcp=inject,
    )


def test_no_inject_claude_drops_mcp_config(tmp_path: Path):
    ctx = _ctx(tmp_path, condition=Condition.no_routines, inject=False)
    cmd = ClaudeCodeAdapter().command("go", ctx)
    # Reuses the agent's existing mcp-mcp — no second connection injected.
    assert "--mcp-config" not in cmd
    assert "--disallowedTools" not in cmd   # nothing withheld unless the env says so


def test_claude_adapter_honors_disabled_tools_override(tmp_path: Path):
    ctx = RunContext(
        task=None, agent="claude-code", model="m", condition=Condition.no_routines,
        trial=1, run_dir=tmp_path, mcp_server="http://x",
        mcp_config_path=tmp_path / "mcp.json", workspace_dir=tmp_path,
        disabled_tools=["record_routine", "update_routine", "mobile_get_action_log"],
    )
    cmd = ClaudeCodeAdapter().command("do it", ctx)
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    # Exactly what was passed, nothing added.
    assert "record_routine" in disallowed
    assert "upload_recording" not in disallowed
    assert "find_routine" not in disallowed


def test_codex_adapter_uses_runner_mcp_config_without_global_home(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(
        CodexCliAdapter._AUTH_HOME_ENV,
        str(tmp_path / "missing_codex_home"),
    )
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(json.dumps({
        "mcpServers": {
            "bench-routines": {"type": "http", "url": "http://127.0.0.1:6000/mcp"}
        }
    }))
    ctx = RunContext(
        task=None, agent="codex-cli", model="gpt-5.5", condition=Condition.no_routines,
        trial=1, run_dir=tmp_path, mcp_server="http://x",
        mcp_config_path=mcp_path, workspace_dir=tmp_path / "workspace",
        disabled_tools=_disabled_tools(), inject_mcp=False,
    )
    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    config = (tmp_path / "codex_home" / "config.toml").read_text()
    assert '[mcp_servers."bench-routines"]' in config
    assert 'url = "http://127.0.0.1:6000/mcp"' in config
    assert str(Path.home() / ".codex") not in config
    parsed = tomllib.loads(config)
    bench_routines = parsed["mcp_servers"]["bench-routines"]
    assert bench_routines["required"] is True
    assert bench_routines["tool_timeout_sec"] == 300
    assert "disabled_tools" not in bench_routines   # env unset -> nothing withheld
    assert adapter.env(ctx)["CODEX_HOME"] == str(tmp_path / "codex_home")


def test_codex_doctor_reports_auth_readiness(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv(
        CodexCliAdapter._AUTH_HOME_ENV,
        str(tmp_path / "missing_codex_home"),
    )

    missing = check_codex_auth()
    assert missing.passed is False
    assert "no account login" in missing.detail
    assert "codex login --device-auth" in (missing.fix or "")
    assert "CODEX_API_KEY" in (missing.fix or "")

    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"mode":"account"}')
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))

    account = check_codex_auth()
    assert account.passed is True
    assert "account login" in account.detail

    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    present = check_codex_auth()
    assert present.passed is True
    assert present.detail == "CODEX_API_KEY set"


def test_codex_auth_does_not_depend_on_qualgent_key(
    tmp_path: Path,
    monkeypatch,
):
    """Codex auth and QUALGENT_API_KEY are unrelated credentials — the seeded-bug
    benchmark never talks to the QualGent backend."""
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("QUALGENT_API_KEY", raising=False)
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"mode":"account"}')
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))

    codex = check_codex_auth()

    assert codex.passed is True
    assert codex.name == "Codex auth"
    assert "account login" in codex.detail


def test_codex_result_artifact_preserves_agent_and_actual_model(tmp_path: Path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = RunResult.build(
        task_id="tc_login",
        task_version="customer-v1",
        task_type="regression",
        agent="codex-cli",
        model="openai/gpt-5.5",
        condition="dl",
        trial=1,
        started_at=now,
        ended_at=now,
        exit_code=0,
        verifier=VerifierResult(
            passed=True,
            score=1.0,
            weighted_score=1.0,
            metrics={"reported_status": "PASS"},
        ),
        artifact_dir=tmp_path,
    )

    out = tmp_path / "result.json"
    result.write(out)
    payload = json.loads(out.read_text())
    assert payload["agent"] == "codex-cli"
    assert payload["model"] == "openai/gpt-5.5"


@pytest.mark.asyncio
async def test_wipe_shared_storage_recreates_declared_dirs(monkeypatch):
    calls: list[tuple] = []

    async def fake_adb(*args):
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(episode_runner, "_adb", fake_adb)
    await episode_runner.wipe_shared_storage("emulator-5554",
                                              ["/sdcard/Documents/markor/"])
    shells = [a[-1] for a in calls]
    assert shells == ["rm -rf /sdcard/Documents/markor",
                      "mkdir -p /sdcard/Documents/markor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/sdcard", "/sdcard/", "/data/data/x", "",
                                  "/storage/emulated/0"])
async def test_wipe_shared_storage_refuses_non_app_dirs(monkeypatch, path):
    """A bare root or a non-shared path must never reach `rm -rf`."""
    async def fake_adb(*args):  # pragma: no cover - must not be called
        raise AssertionError(f"adb invoked for refused path {path!r}: {args}")

    monkeypatch.setattr(episode_runner, "_adb", fake_adb)
    await episode_runner.wipe_shared_storage("emulator-5554", [path])


@pytest.mark.asyncio
async def test_wipe_shared_storage_allows_top_level_media_dir(monkeypatch):
    """/sdcard/Music is one level down and IS a legitimate target (musicplayer)."""
    calls: list[tuple] = []

    async def fake_adb(*args):
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(episode_runner, "_adb", fake_adb)
    await episode_runner.wipe_shared_storage("emulator-5554", ["/sdcard/Music"])
    assert [a[-1] for a in calls] == ["rm -rf /sdcard/Music", "mkdir -p /sdcard/Music"]


def test_specs_declaring_shared_storage_wipe_before_staging():
    """Staged content must be pushed AFTER the wipe, or it gets deleted."""
    from pathlib import Path as _P

    from qualgentbench import bugs as bugmod

    spec = (_P(bugmod.__file__).parent / "data" / "benchmarks"
            / "fossify-musicplayer.yaml")
    suite = bugmod.load_suite(spec)
    task = bugmod.exploration_task(suite)
    dests = [p["dest"] for p in task.bug_spec["device_setup"]["push"]]
    wiped = task.bug_spec["shared_storage"]
    assert wiped == ["/sdcard/Music"]
    assert all(d.startswith("/sdcard/Music/") for d in dests)


@pytest.mark.asyncio
async def test_the_replay_snapshot_is_taken_cold(monkeypatch, tmp_path):
    """A tar of a running app can miss unflushed state, so the order is
    relaunch -> settle -> force-stop -> tar -> relaunch, making agent-start
    and replay-start identical by construction."""
    calls: list[str] = []

    async def _relaunch(device, bundle):
        calls.append("relaunch")
        return []

    async def _adb(*args):
        if "force-stop" in args:
            calls.append("force-stop")
        return 0, ""

    async def _snap(device, bundle, path):
        calls.append("tar")
        return True

    async def _stable(device, timeout_s=8):
        return True

    async def _sleep(_s):
        return None
    monkeypatch.setattr(episode_runner, "_relaunch_app", _relaunch)
    monkeypatch.setattr(episode_runner, "_adb", _adb)
    monkeypatch.setattr(episode_runner, "replay_snapshot", _snap)
    monkeypatch.setattr(episode_runner, "wait_stable", _stable)
    monkeypatch.setattr(episode_runner.asyncio, "sleep", _sleep)

    await episode_runner.take_replay_snapshots("serial", "pkg", tmp_path, None)
    assert calls == ["relaunch", "force-stop", "tar", "relaunch"]
    assert json.loads((tmp_path / "snapshot_meta.json").read_text()) == {"mode": "cold"}
