"""MCP-ablation experiment: raw (no MCP) vs mcp conditions — source emission,
adapter arm wiring, condition-aware hunt scoring, and the neutral instruction.
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import time

from qualgentbench.session import DeviceSession
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualgentbench import bugs
from qualgentbench.adapters import get_adapter, native
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.episode_runner import _ablation_instruction, _disabled_tools
from qualgentbench.schemas import Condition

FIXTURE = Path(__file__).parent / "fixtures" / "sample_suite.yaml"

_ALL_CORRECT = ("login=broken, view_notes=ok, add_note=broken, edit_note=broken, "
                "delete_note=broken, logout=broken, password_toggle=ok")


@pytest.fixture(autouse=True)
def _hide_real_codex_auth(monkeypatch, tmp_path):
    monkeypatch.setenv(
        CodexCliAdapter._AUTH_HOME_ENV,
        str(tmp_path / "missing_codex_home"),
    )


# ── transcript builders (same stream-json emitters the other tests use) ───────


@dataclass
class _Fn:
    name: str
    arguments: str


@dataclass
class _TC:
    id: str
    function: _Fn


_ids = itertools.count()


def _call(name: str, inp: dict, result: str) -> str:
    cid = f"abl{next(_ids)}"
    return "\n".join([
        native._assistant_event("m", None, [_TC(cid, _Fn(name, json.dumps(inp)))]),
        native._tool_result_event(cid, result),
    ])


def _text(text: str) -> str:
    """A plain assistant text message (no tool call) — how a raw run reports."""
    return native._assistant_event("m", text, [])


def _bash(command: str) -> str:
    return _call("Bash", {"command": command}, "ok")


def _obs(text: str) -> str:
    return _call("mobile_observe_screen", {"device": "d"}, text)


def _report(status: str, summary: str) -> str:
    return _call("mobile_report_result", {"status": status, "summary": summary}, "ok")


def _transcript(*parts: str) -> str:
    return "\n".join(parts) + "\n"


def _hunt_task(tooling: str | None = None):
    task = bugs.exploration_task(bugs.load_suite(FIXTURE))
    if tooling:
        task.bug_spec["tooling"] = tooling
    return task


# ── emit_source ───────────────────────────────────────────────────────────────


def _load_build_app():
    path = Path(__file__).parents[1] / "scripts" / "build_app.py"
    spec = importlib.util.spec_from_file_location("build_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_emit_source_patches_copy_and_leaves_checkout_pristine(tmp_path):
    build_app = _load_build_app()
    app_dir = tmp_path / "SomeApp"
    (app_dir / "app/src").mkdir(parents=True)
    (app_dir / ".git").mkdir()
    (app_dir / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (app_dir / "app/build").mkdir()
    (app_dir / "app/build/junk.bin").write_text("compiled junk")
    original = ("fun add(a: Int, b: Int): Int {\n    return a + b\n}\n"
                "fun mul(a: Int, b: Int): Int {\n    return a * b\n}\n")
    (app_dir / "app/src/Math.kt").write_text(original)

    spec = {
        "setup": [{"file": "app/src/Math.kt",
                   "find": "fun add(a: Int, b: Int): Int {",
                   "replace": "fun add(a: Int, b: Int): Int {  // seeded data hook"}],
        "bugs": [
            # Trailing-marker shape: the code must survive, the comment must not.
            {"id": "add-wrong",
             "patch": {"file": "app/src/Math.kt",
                       "find": "return a + b",
                       "replace": "return a - b // BUG(add-wrong): subtracts instead"}},
            # Comment-only shape with a continuation line: both must be dropped.
            {"id": "mul-noop",
             "patch": {"file": "app/src/Math.kt",
                       "find": "return a * b",
                       "replace": ("// BUG(mul-noop): multiplication is gone, so it\n"
                                   "// always returns zero.\n"
                                   "return 0")}},
        ],
    }
    out = build_app.emit_source(spec, app_dir, tmp_path / "dist")

    patched = (out / "app/src/Math.kt").read_text()
    assert "return a - b" in patched              # bug patch applied, code kept
    assert "return 0" in patched
    assert "seeded data hook" in patched          # setup patch applied too
    assert "BUG(" not in patched                  # answer key sanitized out
    assert "always returns zero" not in patched   # continuation line dropped too
    assert (app_dir / "app/src/Math.kt").read_text() == original  # checkout pristine
    assert not (out / ".git").exists()            # no VCS history for the agent
    assert not (out / "app/build").exists()       # build outputs excluded


# ── claude-code adapter: raw mode + fairness knobs ────────────────────────────


def _context(**overrides) -> RunContext:
    task = SimpleNamespace(agent=SimpleNamespace(timeout_sec=900))
    defaults = dict(
        task=task, agent="claude-code", model="claude-sonnet-4-6",
        condition=Condition.no_routines, trial=1, run_dir=Path("/tmp/run"),
        mcp_server="http://localhost:51821",
        mcp_config_path=Path("/tmp/mcp.json"), workspace_dir=Path("/tmp/ws"),
        disabled_tools=[], inject_mcp=False,
    )
    defaults.update(overrides)
    return RunContext(**defaults)  # type: ignore[arg-type]


def test_claude_code_raw_is_the_stock_agent():
    """Raw is the stock coding agent: isolation, but no tool surgery — gating its
    tool list would measure something other than the shipped agent."""
    cmd = ClaudeCodeAdapter().command("", _context(no_mcp=True))
    # Isolation stays: without these the user's global MCP server leaks into raw.
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" in cmd
    # Surgery goes.
    for flag in ("--disallowedTools", "--allowedTools", "--tools"):
        assert flag not in cmd, f"raw must not shape the tool surface ({flag})"


def test_claude_code_mcp_mode_has_no_strict_flag():
    cmd = ClaudeCodeAdapter().command("", _context())
    assert "--strict-mcp-config" not in cmd
    assert "--mcp-config" not in cmd        # inject_mcp=False reuses global config
    assert "--model" not in cmd             # legacy: CLI default model
    assert "--settings" not in cmd          # no cap → no hook settings


def test_claude_code_mcp_arm_shapes_no_builtins():
    """The MCP arm withholds MCP tools only, never built-ins — the arms must
    differ ONLY by whether a device MCP server is configured."""
    cmd = ClaudeCodeAdapter().command(
        "", _context(isolate_mcp=True, disabled_tools=["mobile_open_url"]))
    assert "--strict-mcp-config" in cmd and "--mcp-config" in cmd
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    for builtin in ("WebFetch", "WebSearch", "Workflow", "swift-lsp", "ToolSearch"):
        assert builtin not in disallowed
    assert disallowed == "mcp__device__mobile_open_url"   # exactly what was asked for


def test_claude_code_model_passthrough():
    cmd = ClaudeCodeAdapter().command("", _context(force_model="claude-sonnet-4-6"))
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_claude_code_tool_call_cap_hook(tmp_path):
    ctx = _context(run_dir=tmp_path, tool_call_cap=70)
    adapter = ClaudeCodeAdapter()
    adapter.prepare(ctx)
    cmd = adapter.command("", ctx)

    settings_path = tmp_path / "hooks" / "settings.json"
    assert cmd[cmd.index("--settings") + 1] == str(settings_path)
    settings = json.loads(settings_path.read_text())
    hook_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    script = Path(hook_cmd)
    assert script.exists() and script.stat().st_mode & 0o111   # executable
    body = script.read_text()
    assert "CAP = 70" in body                 # the native step budget, enforced
    assert "RESULT:" not in body              # HARD stop: no "now write your report"
    assert str(tmp_path / "truncated") in body    # sentinel base.run() kills on
    assert (tmp_path / "hooks" / "count").read_text() == "0"   # fresh counter

    import subprocess

    meter = tmp_path / "interactions.json"

    def run_hook(payload: str) -> int:
        return subprocess.run([str(script)], input=payload, text=True).returncode

    # The budget tracks the METER, not the number of tool calls.
    meter.write_text(json.dumps({"interactions": 70}))
    assert run_hook('{"tool_name":"Bash"}') == 0          # at the cap, still allowed
    meter.write_text(json.dumps({"interactions": 71}))
    assert run_hook('{"tool_name":"Bash"}') == 2          # over it, denied
    assert run_hook(
        '{"tool_name":"Bash","input":{"command":"echo qg_release_device"}}'
    ) == 2
    # qg_release_device is exempt even past the cap (lock cleanup, not QA work).
    assert run_hook('{"tool_name":"mcp__device__qg_release_device"}') == 0
    assert (tmp_path / "hooks" / "count").read_text().strip() == "71"


def test_the_budget_charges_interactions_not_the_command_string(tmp_path):
    """A wrapped driver script carries no literal `adb `, so the cost must come
    from the ADB socket meter, not the command string."""
    import json as _json
    import subprocess

    ctx = _context(run_dir=tmp_path, tool_call_cap=500)
    ClaudeCodeAdapter().prepare(ctx)
    script = tmp_path / "hooks" / "tool_cap.py"
    meter = tmp_path / "interactions.json"

    def run_hook(payload='{"tool_name":"Bash"}') -> int:
        return subprocess.run([str(script)], input=payload, text=True).returncode

    def spent() -> int:
        return int((tmp_path / "hooks" / "count").read_text().strip())

    meter.write_text(_json.dumps({"interactions": 0}))
    run_hook()
    assert spent() == 0             # a call that touched no device costs nothing

    # The wrapped script runs and drives 100 operations through the proxy.
    meter.write_text(_json.dumps({"interactions": 100}))
    run_hook()
    assert spent() == 100           # charged 100, not 1

    # A later non-device call adds nothing: the budget IS the meter, so there is no
    # second counter to drift, and no per-call floor to overcharge with.
    run_hook()
    assert spent() == 100


def test_a_corrupt_meter_file_stops_the_episode(tmp_path):
    """The hook reads a file another process writes. An unreadable read means the
    episode is UNMEASURED, and an unmeasured episode with no budget would run unbounded
    and still be scored — so it fails closed rather than charging a made-up step."""
    import subprocess

    ctx = _context(run_dir=tmp_path, tool_call_cap=10)
    ClaudeCodeAdapter().prepare(ctx)
    script = tmp_path / "hooks" / "tool_cap.py"
    (tmp_path / "interactions.json").write_text('{"interactions": ')   # truncated JSON

    r = subprocess.run([str(script)], input='{"tool_name":"Bash"}', text=True,
                       capture_output=True)
    assert r.returncode == 2
    assert (tmp_path / "hooks" / "sentinel").exists() or "unreadable" in r.stderr


def test_codex_cli_registered():
    assert isinstance(get_adapter("codex-cli"), CodexCliAdapter)


def test_codex_cli_seeds_only_account_auth_from_override(tmp_path, monkeypatch):
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    auth = source_home / "auth.json"
    auth.write_text('{"mode":"account","secret":"token"}')
    auth.chmod(0o600)
    (source_home / "config.toml").write_text(
        'model = "global"\n'
        "[mcp_servers.global_mcp]\n"
        'command = "leaky-global-mcp"\n'
    )
    (source_home / "hooks.json").write_text('{"hooks":{"global":[]}}\n')
    (source_home / "mcp_servers.json").write_text('{"global":"mcp"}\n')
    (source_home / "AGENTS.md").write_text("global instructions\n")
    (source_home / "AGENTS").write_text("global instructions\n")
    for dirname in (
        "apps",
        "hooks",
        "mcp",
        "memories",
        "plugins",
        "plugin_hooks",
        "sessions",
        "skills",
        "logs",
    ):
        nested = source_home / dirname
        nested.mkdir()
        (nested / "marker").write_text("global state\n")

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")

    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    codex_home = ctx.run_dir / "codex_home"
    copied_auth = codex_home / "auth.json"
    assert copied_auth.read_text() == auth.read_text()
    assert stat.S_IMODE(copied_auth.stat().st_mode) == 0o600
    assert {path.name for path in codex_home.iterdir()} == {
        "auth.json",
        "config.toml",
        "home",
        "hooks.json",
    }
    config = (codex_home / "config.toml").read_text()
    assert 'model = "global"' not in config
    assert "global_mcp" not in config
    assert "leaky-global-mcp" not in config
    assert json.loads((codex_home / "hooks.json").read_text()) == {"hooks": {}}
    assert not (codex_home / "AGENTS.md").exists()
    assert not (codex_home / "AGENTS").exists()
    env = CodexCliAdapter().env(ctx)
    assert env["CODEX_HOME"] == str(codex_home)
    assert env["HOME"] == str(codex_home / "home")
    assert "CODEX_API_KEY" not in env


def test_codex_cli_seeds_account_auth_from_codex_home_fallback(tmp_path, monkeypatch):
    source_home = tmp_path / "codex_env_home"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"mode":"account"}')

    monkeypatch.delenv(CodexCliAdapter._AUTH_HOME_ENV, raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")

    CodexCliAdapter().prepare(ctx)

    assert (ctx.run_dir / "codex_home" / "auth.json").read_text() == '{"mode":"account"}'


def test_codex_cli_api_key_skips_account_auth_copy(tmp_path, monkeypatch):
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"secret":"account"}')

    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))
    monkeypatch.setenv("CODEX_API_KEY", "codex_test")
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")

    # Codex does not read the key from the environment — it must be exchanged for
    # an auth.json via `login --with-api-key`. Stubbed so the test stays hermetic.
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "input": kw.get("input"),
                      "home": kw.get("env", {}).get("CODEX_HOME")})
        Path(kw["env"]["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
        (Path(kw["env"]["CODEX_HOME"]) / "auth.json").write_text('{"from":"api-key"}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("qualgentbench.adapters.codex_cli.subprocess.run", fake_run)

    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    auth = ctx.run_dir / "codex_home" / "auth.json"
    assert calls and calls[0]["cmd"] == ["codex", "login", "--with-api-key"]
    # The key goes over stdin, never argv — argv is visible in a process listing.
    assert calls[0]["input"] == "codex_test"
    assert "codex_test" not in " ".join(calls[0]["cmd"])
    # Authenticated by KEY, so the operator's account auth must not be copied in.
    assert auth.is_file() and "account" not in auth.read_text()
    assert adapter.env(ctx)["CODEX_API_KEY"] == "codex_test"


def test_codex_cli_cleanup_removes_seeded_account_auth_only(tmp_path, monkeypatch):
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"secret":"account"}')

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")
    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    codex_home = ctx.run_dir / "codex_home"
    assert (codex_home / "auth.json").exists()
    adapter.cleanup(ctx)

    assert not (codex_home / "auth.json").exists()
    assert (codex_home / "config.toml").exists()
    assert (codex_home / "hooks.json").exists()


def test_codex_cli_cleanup_removes_nested_account_auth_artifacts(
    tmp_path,
    monkeypatch,
):
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"secret":"account"}')

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")
    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    codex_home = ctx.run_dir / "codex_home"
    nested_auth = codex_home / "home" / ".codex" / "auth.json"
    nested_auth.parent.mkdir(parents=True)
    nested_auth.write_text('{"secret":"nested-account"}')

    adapter.cleanup(ctx)

    assert list(codex_home.rglob("auth.json")) == []
    assert (codex_home / "config.toml").exists()
    assert (codex_home / "hooks.json").exists()


def test_codex_cli_run_cleans_seeded_account_auth_after_agent_return(
    tmp_path,
    monkeypatch,
):
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"secret":"account"}')

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")

    async def fake_base_run(self, instruction, context):
        self.prepare(context)
        assert (context.run_dir / "codex_home" / "auth.json").exists()
        return "ok", 0

    monkeypatch.setattr(
        "qualgentbench.adapters.base.AgentAdapter.run",
        fake_base_run,
    )

    assert asyncio.run(CodexCliAdapter().run("do it", ctx)) == ("ok", 0)
    assert not (ctx.run_dir / "codex_home" / "auth.json").exists()


def test_codex_cli_run_cleans_seeded_account_auth_after_agent_error(
    tmp_path,
    monkeypatch,
):
    source_home = tmp_path / "source_codex"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"secret":"account"}')

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(source_home))
    ctx = _context(agent="codex-cli", run_dir=tmp_path / "run")

    async def fake_base_run(self, instruction, context):
        self.prepare(context)
        assert (context.run_dir / "codex_home" / "auth.json").exists()
        raise RuntimeError("agent failed")

    monkeypatch.setattr(
        "qualgentbench.adapters.base.AgentAdapter.run",
        fake_base_run,
    )

    with pytest.raises(RuntimeError, match="agent failed"):
        asyncio.run(CodexCliAdapter().run("do it", ctx))
    assert list((ctx.run_dir / "codex_home").rglob("auth.json")) == []


def _write_mcp_config(path: Path) -> None:
    path.write_text(json.dumps({
        "mcpServers": {
            "mcp": {
                "type": "http",
                "url": "http://127.0.0.1:51821/mcp",
                "env": {"QUALGENT_API_KEY": "qg_test"},
            }
        }
    }))


def test_codex_cli_isolated_home_config_command_and_env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp_path = tmp_path / "mcp.json"
    _write_mcp_config(mcp_path)
    ctx = _context(
        agent="codex-cli",
        model="gpt-5.5",
        run_dir=tmp_path,
        workspace_dir=workspace,
        mcp_config_path=mcp_path,
        disabled_tools=[],
        inject_mcp=False,
        isolate_mcp=True,
    )
    monkeypatch.setenv("CODEX_API_KEY", "codex_test")

    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    codex_home = tmp_path / "codex_home"
    config = (codex_home / "config.toml").read_text()
    assert 'model = "gpt-5.5"' in config
    assert 'approval_policy = "never"' in config
    assert 'sandbox_mode = "danger-full-access"' in config
    assert "[mcp_servers.mcp]" in config
    assert 'url = "http://127.0.0.1:51821/mcp"' in config
    # No `env` block for a streamable_http (url) server — Codex hard-errors on it.
    assert "env =" not in config
    assert "required = true" in config
    assert "tool_timeout_sec = 300" in config
    assert str(Path.home() / ".codex") not in config
    assert json.loads((codex_home / "hooks.json").read_text()) == {"hooks": {}}

    cmd = adapter.command("", ctx)
    assert cmd[:5] == [
        "codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access",
    ]
    exec_idx = cmd.index("exec")
    for feature in CodexCliAdapter._DISABLED_GLOBAL_FEATURES:
        positions = [
            i for i, arg in enumerate(cmd[:-1])
            if arg == "--disable" and cmd[i + 1] == feature
        ]
        assert positions
        assert all(i < exec_idx for i in positions)
    assert "--dangerously-bypass-hook-trust" not in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert cmd.index("--ask-for-approval") < exec_idx
    assert cmd[exec_idx + 1:exec_idx + 3] == ["--json", "--ephemeral"]
    assert cmd[cmd.index("--cd") + 1] == str(workspace)
    assert "--skip-git-repo-check" in cmd

    env = adapter.env(ctx)
    assert env["CODEX_HOME"] == str(codex_home)
    assert env["HOME"] == str(codex_home / "home")
    assert env["XDG_CONFIG_HOME"] == str(codex_home / "xdg_config")
    assert env["XDG_CACHE_HOME"] == str(codex_home / "xdg_cache")
    assert env["XDG_DATA_HOME"] == str(codex_home / "xdg_data")
    assert env["CODEX_API_KEY"] == "codex_test"
    # With a key configured an auth.json here is expected — what must NOT happen
    # is inheriting the operator's account auth (covered by the api-key test above).


def test_codex_cli_raw_mode_has_no_mcp_servers(tmp_path):
    mcp_path = tmp_path / "mcp.json"
    _write_mcp_config(mcp_path)
    ctx = _context(
        agent="codex-cli",
        run_dir=tmp_path,
        mcp_config_path=mcp_path,
        no_mcp=True,
        disabled_tools=_disabled_tools(),
    )

    CodexCliAdapter().prepare(ctx)

    config = tomllib.loads((tmp_path / "codex_home" / "config.toml").read_text())
    assert "mcp_servers" not in config
    assert "features" not in config   # codex runs stock; no tool shaping here


def test_codex_cli_withholds_exactly_what_the_env_asked_for(tmp_path):
    mcp_path = tmp_path / "mcp.json"
    _write_mcp_config(mcp_path)
    ctx = _context(
        agent="codex-cli",
        run_dir=tmp_path,
        mcp_config_path=mcp_path,
        condition=Condition.no_routines,
        disabled_tools=["mobile_open_url", "mobile_push_media"],
        isolate_mcp=True,
    )

    CodexCliAdapter().prepare(ctx)

    config = tomllib.loads((tmp_path / "codex_home" / "config.toml").read_text())
    servers = config["mcp_servers"]
    assert set(servers) == {"mcp"}
    mcp = servers["mcp"]
    assert mcp["url"] == "http://127.0.0.1:51821/mcp"
    assert mcp["required"] is True
    assert mcp["tool_timeout_sec"] == 300
    assert "env" not in mcp  # url server: Codex rejects `env`, so it is omitted
    assert set(mcp["disabled_tools"]) == {"mobile_open_url", "mobile_push_media"}


def test_codex_cli_tool_call_cap_hook(tmp_path):
    ctx = _context(agent="codex-cli", run_dir=tmp_path, tool_call_cap=2)
    adapter = CodexCliAdapter()
    adapter.prepare(ctx)

    # The cap lives ONLY in config.toml's [hooks] table; hooks.json stays empty —
    # codex loads both, and duplicating the hook double-counted every tool call.
    hooks = json.loads((tmp_path / "codex_home" / "hooks.json").read_text())
    assert hooks == {"hooks": {}}

    config_toml = (tmp_path / "codex_home" / "config.toml").read_text()
    assert "[[hooks.PreToolUse]]" in config_toml
    assert "[[hooks.PreToolUse.hooks]]" in config_toml
    # The SAME shared hook every adapter uses — see docs/interaction-spec.md.
    script = tmp_path / "codex_home" / "hooks" / "tool_cap.py"
    assert script.exists() and script.stat().st_mode & 0o111
    assert str(script) in config_toml
    assert (tmp_path / "codex_home" / "hooks" / "count").read_text() == "0"
    cmd = adapter.command("", ctx)
    assert "--dangerously-bypass-hook-trust" in cmd
    assert cmd.index("--dangerously-bypass-hook-trust") < cmd.index("exec")

    import subprocess

    def run_hook(payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script)],
            input=payload,
            text=True,
            capture_output=True,
        )

    meter = tmp_path / "interactions.json"
    meter.write_text(json.dumps({"interactions": 1}))
    assert run_hook('{"tool_name":"Bash"}').returncode == 0
    meter.write_text(json.dumps({"interactions": 2}))
    assert run_hook('{"tool_name":"Bash"}').returncode == 0
    meter.write_text(json.dumps({"interactions": 3}))
    denied = run_hook('{"tool_name":"Bash"}')
    assert denied.returncode == 2
    assert "terminated" in denied.stderr
    # HARD stop: crossing the cap drops the sentinel base.run() kills on.
    # It must not coach the agent to dump a blind verdict.
    assert (tmp_path / "truncated").exists()
    assert "final result" not in denied.stderr
    assert (tmp_path / "codex_home" / "hooks" / "count").read_text().strip() == "3"
    bypass_attempt = run_hook(
        '{"tool_name":"Bash","input":{"command":"echo qg_release_device"}}'
    )
    assert bypass_attempt.returncode == 2
    # The count mirrors the meter, so a denied call does not advance it.
    assert (tmp_path / "codex_home" / "hooks" / "count").read_text().strip() == "3"
    assert run_hook('{"tool_name":"mcp__device__qg_release_device"}').returncode == 0
    assert (tmp_path / "codex_home" / "hooks" / "count").read_text().strip() == "3"

    adapter.prepare(ctx)
    assert (tmp_path / "codex_home" / "hooks" / "count").read_text() == "0"


# ── condition-aware hunt scoring ──────────────────────────────────────────────


def test_raw_hunt_scores_result_line_from_final_text():
    t = _transcript(
        _bash("adb -s emulator-5554 exec-out screencap -p > /tmp/s.png"),
        _bash("adb -s emulator-5554 shell input tap 540 1200"),
        _text("Testing done.\nRESULT: " + _ALL_CORRECT),
    )
    v = bugs.exploration_verdict(t, "m", _hunt_task("raw"))
    assert v.metrics["bugs_found"] == 5
    assert v.metrics["recall"] == 1.0
    assert v.metrics["false_positives"] == 0
    assert v.criteria["evidence_attached"] is True      # adb usage = device evidence
    assert v.criteria["no_mcp_tools"] is True
    assert v.metrics["condition"] == "raw"
    assert v.metrics["raw_adb_calls"] == 2
    assert v.metrics["raw_screencaps"] == 1
    assert v.passed is True


def test_raw_hunt_without_device_interaction_fails_evidence():
    # Pure code-review run: reads source, never touches the device.
    t = _transcript(
        _call("Read", {"file_path": "app/src/Main.kt"}, "fun main() {}"),
        _text("RESULT: " + _ALL_CORRECT),
    )
    v = bugs.exploration_verdict(t, "m", _hunt_task("raw"))
    assert v.metrics["bugs_found"] == 5                  # labels still parsed…
    assert v.criteria["evidence_attached"] is False      # …but no device evidence
    assert v.passed is False
    assert "no device interaction" in (v.failure_reason or "")
    assert v.metrics["raw_adb_calls"] == 0
    assert v.metrics["source_reads"] == 1


def test_raw_hunt_flags_mcp_leak():
    # A raw run that somehow reached MCP tools = broken condition isolation.
    t = _transcript(
        _call("mcp__device__mobile_observe_screen", {"device": "d"}, "screen"),
        _bash("adb shell input tap 1 1"),
        _text("RESULT: " + _ALL_CORRECT),
    )
    v = bugs.exploration_verdict(t, "m", _hunt_task("raw"))
    assert v.criteria["no_mcp_tools"] is False
    assert v.metrics["mcp_tool_calls"] == 1


def test_mcp_hunt_keeps_mobile_tool_evidence_rule():
    t = _transcript(
        _obs("Notes list"),
        _report("FAIL", "RESULT: " + _ALL_CORRECT),
    )
    v = bugs.exploration_verdict(t, "m", _hunt_task("mcp"))
    assert v.metrics["condition"] == "mcp"
    assert v.criteria["evidence_attached"] is True
    assert v.metrics["bugs_found"] == 5
    assert v.passed is True
    assert "no_mcp_tools" not in v.criteria          # raw-only criterion


def test_ablation_parses_last_result_line():
    # The agent muses about a "result:" mid-run; only the FINAL line is official.
    wrong = ("login=ok, view_notes=ok, add_note=ok, edit_note=ok, "
             "delete_note=ok, logout=ok, password_toggle=ok")
    t = _transcript(
        _bash("adb shell input tap 1 1"),
        _text("preliminary result: " + wrong),
        _text("Final answer.\nRESULT: " + _ALL_CORRECT),
    )
    v = bugs.exploration_verdict(t, "m", _hunt_task("raw"))
    assert v.metrics["bugs_found"] == 5


def test_legacy_hunt_ignores_text_only_result():
    # WITHOUT tooling, the official channel stays mobile_report_result — a RESULT
    # line that appears only in assistant text earns nothing (strict reporting).
    t = _transcript(
        _obs("Notes list"),
        _text("RESULT: " + _ALL_CORRECT),
    )
    v = bugs.exploration_verdict(t, "m", _hunt_task())
    assert v.metrics["bugs_found"] == 0
    assert "condition" not in v.metrics                  # legacy metrics unchanged


# ── the neutral instruction ───────────────────────────────────────────────────

_BIASING_PHRASES = (
    "find bug", "find the bug", "find as many", "hidden", "seeded", "defect",
    "something is wrong", "not been told", "is broken", "are broken", "issue",
)


def _instruction_task():
    task = _hunt_task()
    task.bundle_id = "com.example.buggyapp"
    return task


def test_ablation_instruction_is_neutral_and_lists_features():
    for tooling in ("raw", "mcp"):
        instr = _ablation_instruction(_instruction_task(), "emulator-5554", tooling)
        low = instr.lower()
        for phrase in _BIASING_PHRASES:
            assert phrase not in low, f"{tooling}: biasing phrase {phrase!r}"
        # Ground truth must not leak: no bug ids, no feature states.
        assert "auth-bypass" not in low and "save-drop" not in low
        assert "state" not in low
        # Every feature id is listed, and the report protocol is spelled out.
        for fid in ("login", "view_notes", "add_note", "edit_note",
                    "delete_note", "logout", "password_toggle"):
            assert fid in instr
        assert "RESULT:" in instr
        assert "emulator-5554" in instr


def test_ablation_instruction_only_differs_in_tooling():
    raw = _ablation_instruction(_instruction_task(), "emulator-5554", "raw")
    dev = _ablation_instruction(_instruction_task(), "emulator-5554", "mcp")
    assert "adb" in raw and "mobile_observe_screen" not in raw
    assert "mobile_observe_screen" in dev
    # No device-lock tools in either arm: the standalone server has none.
    assert "qg_acquire_device" not in raw and "qg_acquire_device" not in dev
    # Standalone does not inject the device, so the mcp arm must be told to pass it.
    assert 'device="emulator-5554"' in dev
    # The QA task section (the part that could bias what gets tested) is identical.
    raw_task = raw.split("## Your QA Task")[1].split("## Reporting")[0]
    dev_task = dev.split("## Your QA Task")[1].split("## Reporting")[0]
    assert raw_task == dev_task


# ── earliness (speed) credit ──────────────────────────────────────────────────


def test_earliness_credit_breaks_ties_by_speed():
    """At equal quality the faster agent scores higher; using the whole budget is
    neutral, never a penalty (overrun is already punished by the hard stop)."""
    assert bugs.earliness_multiplier(100, 100) == 1.0     # full budget → no credit
    assert bugs.earliness_multiplier(50, 100) > 1.0       # half budget → credit
    assert bugs.earliness_multiplier(20, 100) > bugs.earliness_multiplier(50, 100)
    assert bugs.earliness_multiplier(10, None) == 1.0     # no budget → no credit
    assert bugs.earliness_multiplier(200, 100) == 1.0     # overrun is not a penalty


def test_speed_breaks_ties_between_equal_quality_agents():
    """Same quality, fewer steps, higher score — the old cap made every
    equal-recall run tie at exactly 1.0."""
    fast = bugs.speed_factor(80, 300)
    slow = bugs.speed_factor(100, 300)

    assert fast > slow, "an agent using fewer steps must score higher at equal quality"
    assert slow > 1.0 - bugs._SPEED_WEIGHT             # bounded below
    assert bugs.speed_factor(1, 10 ** 9) == 1.0        # effectively instant → no discount
    assert bugs.speed_factor(300, 300) == round(1.0 - bugs._SPEED_WEIGHT, 4)
    assert bugs.speed_factor(400, 300) == round(1.0 - bugs._SPEED_WEIGHT, 4)  # floor
    assert bugs.speed_factor(50, None) == 1.0          # no budget → nothing to discount
    # A step count of 0 means the counter was never written, not that the agent was
    # infinitely fast — so it takes the FULL discount rather than a free perfect score.
    assert bugs.speed_factor(0, 300) == round(1.0 - bugs._SPEED_WEIGHT, 4)


def test_speed_is_only_a_tie_break_never_a_lever():
    """Quality is strictly first: missing a defect can never be outrun. Features
    come through `exploration_task` so tier weights are merged in, and the instant
    agent uses one real step — speed_factor treats 0 as a missing counter."""
    checked = 0
    for suite in bugs.load_apps():
        task = bugs.exploration_task(suite)
        buggy = [f for f in task.bug_spec["features"] if f["state"] == "broken"]
        if not buggy:
            continue
        weights = [bugs.tier_weight(f.get("tier")) for f in buggy]
        assert any(w != weights[0] for w in weights) or True   # tiers may be uniform
        missed_one = (sum(weights) - min(weights)) / sum(weights)
        perfect_but_slowest = 1.0 * bugs.speed_factor(10 ** 9, 100)
        worse_but_instant = missed_one * bugs.speed_factor(1, 10 ** 9)
        assert perfect_but_slowest > worse_but_instant, (
            suite["app"]["id"], weights,
            f"speed weight {bugs._SPEED_WEIGHT} exceeds the smallest quality gap "
            f"({1 - missed_one:.4f}) — speed has stopped being a tie-break")
        checked += 1
    assert checked, "no seeded apps found — the guard would be vacuous"


def test_earliness_never_inverts_ranking():
    """Speed must never beat substance. The bound is derived from the live corpus —
    the binding case is the lightest bug in any app — so re-tiering fails here
    instead of silently letting a faster-but-worse agent win."""
    slowest = bugs.earliness_multiplier(100, 100)          # used the whole budget
    fastest = bugs.earliness_multiplier(1, 10 ** 9)        # finished instantly

    checked = 0
    for suite in bugs.load_apps():
        task = bugs.exploration_task(suite)
        buggy = [f for f in task.bug_spec["features"] if f["state"] == "broken"]
        if not buggy:
            continue
        weights = [bugs.tier_weight(f.get("tier")) for f in buggy]
        total = sum(weights)
        # Miss exactly one defect — the cheapest one, the worst case for this bound.
        missed_one = (total - min(weights)) / total
        assert 1.0 * slowest > missed_one * fastest, (
            suite["app"]["id"], weights,
            f"speed bonus {bugs._EARLINESS_BONUS} lets a faster agent that MISSED a "
            f"defect outrank one that found them all")
        checked += 1
    assert checked, "no seeded apps found — the guard would be vacuous"


def test_fireworks_model_routes_claude_code_to_fireworks(monkeypatch, tmp_path):
    """A Fireworks slug reroutes claude-code to Fireworks and disables the request
    fields Fireworks 400s on. The provider is declared by the model id, so the
    two cannot drift apart."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw_test_key")
    model = "accounts/fireworks/models/kimi-k3"
    ctx = _context(agent="claude-code", run_dir=tmp_path, model=model, force_model=model)
    adapter = ClaudeCodeAdapter()

    assert adapter.is_fireworks_model(model)
    assert not adapter.is_fireworks_model("claude-opus-4-8")

    env = adapter.env(ctx)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"  # no /v1
    # Key travels in a custom header: ANTHROPIC_API_KEY needs interactive approval,
    # which would hang a headless benchmark run.
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-Fireworks-Api-Key: fw_test_key"
    assert "ANTHROPIC_API_KEY" not in env
    for flag in ("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
                 "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        assert env[flag] == "1"
    assert int(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]) >= 100_000
    # Fine-grained tool streaming adds eager_input_streaming to tool defs, which
    # Fireworks rejects. It must stay unset, not merely default-off.
    assert "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING" not in env

    cmd = adapter.command("", ctx)
    assert cmd[cmd.index("--model") + 1] == model

    # An Anthropic model must be untouched by any of this.
    plain = _context(agent="claude-code", run_dir=tmp_path, model="claude-opus-4-8")
    assert "ANTHROPIC_BASE_URL" not in adapter.env(plain)


def test_fireworks_model_without_key_fails_loudly(monkeypatch, tmp_path):
    """Missing credentials must raise at setup, not produce a run of 401s."""
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_AI_API_KEY", raising=False)
    model = "accounts/fireworks/models/kimi-k3"
    ctx = _context(agent="claude-code", run_dir=tmp_path, model=model, force_model=model)
    with pytest.raises(RuntimeError, match="FIREWORKS_API_KEY"):
        ClaudeCodeAdapter().env(ctx)


def test_generated_mcp_config_never_writes_a_credential_to_disk():
    """The run dir is the shareable artifact — a live org key must not sit in it;
    the key travels in the agent's process environment."""
    from qualgentbench.episode_runner import _generate_mcp_config

    blob = json.dumps(_generate_mcp_config("http://127.0.0.1:51821"))
    assert "QUALGENT_API_KEY" not in blob
    assert "env" not in blob
    assert "/mcp" in blob


def test_native_is_exempt_because_it_is_the_mcp_loop():
    """The native adapter cannot have a 'raw' arm, so it must not be forced to name one."""
    from click.testing import CliRunner
    from qualgentbench.cli import main

    out = CliRunner().invoke(main, [
        "run", "--agent", "native", "--models", "m", "--app", "birday",
        "--mode", "hunt", "--trials", "1", "--runs-dir", "/tmp/qgb-test-nope"])

    assert "requires --condition" not in out.output

def test_adb_only_session_never_reaches_for_a_bridge():
    """The raw arm has no bridge, so lock calls must be skipped, not left to
    time out through the full retry window."""
    import asyncio

    s = DeviceSession(None)
    assert s.bridge_url is None

    async def go():
        await s.force_release("emulator-5554")
        await s.check_device_available("emulator-5554")

    started = time.monotonic()
    asyncio.run(go())
    assert time.monotonic() - started < 1.0


def test_raw_preflight_asks_adb_not_the_bridge():
    """Raw preflight must not consult a server that is not running — the session
    must be ADB-only on this path too."""
    import asyncio

    from qualgentbench.cli import _preflight

    s = DeviceSession(None)          # what _run_bugs builds for raw
    problems = []
    try:
        asyncio.run(_preflight(s, "http://127.0.0.1:9", "codex-cli",
                               None, "raw", None))
    except Exception as exc:          # noqa: BLE001
        problems = str(exc).splitlines()
    # Port 9 is closed: a bridge check would fail loudly. Only a device complaint is
    # acceptable here, and only when no emulator is attached.
    assert not any("MCP server is not reachable" in p for p in problems), problems
    assert not any("DESKTOP APP" in p for p in problems), problems
