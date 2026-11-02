"""claude-code on a Fireworks model authenticates with the Fireworks key alone.

Three things, each a regression a real run hit:

* preflight asked for an Anthropic credential before a Fireworks-only run (a container
  that holds only a Fireworks key could not start one);
* claude-code exits "Not logged in" without sending a request when it holds no
  credential, so the adapter hands the CLI the Fireworks key as its ANTHROPIC_API_KEY;
* with CLAUDE_CODE_OAUTH_TOKEN in the environment, every request of a Fireworks episode
  carried that Anthropic token to Fireworks as `Authorization: Bearer`. The adapter now
  removes every Anthropic credential from the CLI's environment for such an episode.

Anthropic-model claude-code episodes and codex-cli episodes inherit the environment
exactly as before.

The spawn tests run the adapter's real `run()` against a local fake endpoint (no network
past 127.0.0.1, no real key). The always-on one runs a stand-in for the CLI that sends
EVERY credential it finds, which is the worst case of what claude-code may send; the
real CLI runs only on request (`QUALGENTBENCH_REAL_CLAUDE_CLI=1` and `claude` on
PATH; not a `QGB_` name, which the suite strips from every test).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import string
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from qualgentbench import preflight as pf
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.config import BenchConfig
from qualgentbench.schemas import Condition

FW_MODEL = "accounts/fireworks/models/glm-5p3-flash"
ANTHROPIC_MODEL = "claude-sonnet-5-5"


def _fake(prefix: str, n: int, seed: int) -> str:
    """A key-shaped dummy, built here so no real key is ever written down."""
    rng = random.Random(seed)
    return prefix + "".join(rng.choice(string.ascii_letters + string.digits)
                            for _ in range(n))


FW_KEY = _fake("fw_", 24, 1)
# Every Anthropic credential the parent might carry, each with a value that cannot be
# mistaken for anything else in a header.
ANTHROPIC_CREDS = {
    "CLAUDE_CODE_OAUTH_TOKEN": _fake("sk-ant-oat01-", 40, 2),
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": _fake("sk-ant-ort01-", 40, 3),
    "ANTHROPIC_API_KEY": _fake("sk-ant-api03-", 40, 4),
    "ANTHROPIC_AUTH_TOKEN": _fake("gateway-", 32, 5),
    "ANTHROPIC_CUSTOM_HEADERS": "X-Parent-Header: " + _fake("", 24, 6),
}


@pytest.fixture
def parent_env(monkeypatch):
    """A parent process holding every Anthropic credential AND a Fireworks key."""
    for var in ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    for var, value in ANTHROPIC_CREDS.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    monkeypatch.setenv("ANTHROPIC_MODEL", "a-setting-not-a-credential")


@pytest.fixture
def no_creds(monkeypatch):
    for var in (*ANTHROPIC_CREDS, "FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _ctx(tmp_path: Path, model: str, *, agent: str = "claude-code",
         agent_env: dict[str, str] | None = None, timeout: float = 60) -> RunContext:
    run_dir, ws = tmp_path / "run", tmp_path / "ws"
    run_dir.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=timeout)),
        agent=agent, model=model, condition=Condition.no_routines, trial=1,
        run_dir=run_dir, mcp_server="", mcp_config_path=mcp, workspace_dir=ws,
        no_mcp=True, force_model=model, agent_env=agent_env)


def _child_env(adapter, ctx: RunContext) -> dict[str, str]:
    """The environment `AgentAdapter.run` hands the agent, merged the same way."""
    return {**adapter.inherited_env(ctx), **adapter.env(ctx), **(ctx.agent_env or {})}


# ── auth_source and the two preflights ────────────────────────────────────────


def test_a_fireworks_model_is_authenticated_by_the_fireworks_key_alone(no_creds,
                                                                       monkeypatch):
    assert ClaudeCodeAdapter.auth_source(FW_MODEL) is None
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    assert ClaudeCodeAdapter.auth_source(FW_MODEL) == "FIREWORKS_API_KEY"
    # …and an Anthropic token does not authenticate a Fireworks episode: it is never
    # handed to the CLI there.
    monkeypatch.delenv("FIREWORKS_API_KEY")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert ClaudeCodeAdapter.auth_source(FW_MODEL) is None
    monkeypatch.setenv("FIREWORKS_AI_API_KEY", FW_KEY)
    assert ClaudeCodeAdapter.auth_source(FW_MODEL) == "FIREWORKS_AI_API_KEY"


def test_an_anthropic_model_still_needs_an_anthropic_credential(no_creds, monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    for model in (None, ANTHROPIC_MODEL):
        assert ClaudeCodeAdapter.auth_source(model) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert ClaudeCodeAdapter.auth_source(ANTHROPIC_MODEL) == "ANTHROPIC_API_KEY"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert ClaudeCodeAdapter.auth_source() == "CLAUDE_CODE_OAUTH_TOKEN"
    assert ClaudeCodeAdapter.subscription_auth()


def test_needs_anthropic_auth_unless_every_model_is_on_fireworks():
    assert not ClaudeCodeAdapter.needs_anthropic_auth([FW_MODEL])
    assert ClaudeCodeAdapter.needs_anthropic_auth([FW_MODEL, ANTHROPIC_MODEL])
    assert ClaudeCodeAdapter.needs_anthropic_auth([ANTHROPIC_MODEL])
    # No model = the CLI's own default, which is Anthropic's.
    assert ClaudeCodeAdapter.needs_anthropic_auth(None)
    assert ClaudeCodeAdapter.needs_anthropic_auth([])


def _run_preflight(monkeypatch, models: list[str] | None) -> list[str]:
    """`run`'s preflight on the bare arm with a device and both CLIs present: the
    problems it raises, [] when it passes."""
    from qualgentbench import session as session_mod
    from qualgentbench.cli import _preflight
    from qualgentbench.session import DeviceSession

    async def _one_device():
        return ["emulator-5554"]
    monkeypatch.setattr(session_mod, "list_adb_devices", _one_device)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: f"/usr/bin/{name}")
    try:
        asyncio.run(_preflight(DeviceSession(None), "", "claude-code", None, models))
    except click.ClickException as exc:
        return [exc.message]
    return []


def test_run_preflight_accepts_a_fireworks_only_run_with_only_a_fireworks_key(
        no_creds, monkeypatch):
    """The cloud failure: a container holding only FIREWORKS_API_KEY was refused for
    want of an Anthropic token the run would never use."""
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    assert _run_preflight(monkeypatch, [FW_MODEL]) == []


def test_run_preflight_still_refuses_what_it_should(no_creds, monkeypatch):
    # A Fireworks run without the Fireworks key: named as that, not as Anthropic auth.
    [msg] = _run_preflight(monkeypatch, [FW_MODEL])
    assert "runs on Fireworks, but no API key is set" in msg
    assert "claude setup-token" not in msg
    # An Anthropic model (or the CLI default) without an Anthropic credential.
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    for models in ([ANTHROPIC_MODEL], None, [FW_MODEL, ANTHROPIC_MODEL]):
        [msg] = _run_preflight(monkeypatch, models)
        assert "claude setup-token" in msg, models
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert _run_preflight(monkeypatch, [FW_MODEL, ANTHROPIC_MODEL]) == []


def _cfg(model: str) -> BenchConfig:
    return BenchConfig.model_validate({
        "agent": "claude-code", "model": model,
        "scope": {"tiers": ["easy"], "mode": "hunt", "trials": 1},
        "devices": {"avds": ["Pixel_8_A"]}, "runs_dir": "runs"})


def test_config_preflight_auth_for_a_fireworks_model(no_creds, monkeypatch):
    r = pf._check_auth(_cfg(FW_MODEL))
    assert not r.passed and "FIREWORKS_API_KEY" in (r.fix or "")
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    r = pf._check_auth(_cfg(FW_MODEL))
    assert r.passed and not r.warning
    assert r.detail.startswith("FIREWORKS_API_KEY set")
    assert FW_KEY not in r.detail
    # The Anthropic side is untouched.
    r = pf._check_auth(_cfg(ANTHROPIC_MODEL))
    assert not r.passed and "CLAUDE_CODE_OAUTH_TOKEN" in (r.fix or "")


# ── the environment the CLI is given ─────────────────────────────────────────


def test_a_fireworks_episode_holds_no_anthropic_credential(parent_env, tmp_path):
    adapter = ClaudeCodeAdapter()
    ctx = _ctx(tmp_path, FW_MODEL)
    inherited = adapter.inherited_env(ctx)
    for var in ANTHROPIC_CREDS:
        assert var not in inherited, var
    # Only credentials go; settings and everything else are inherited as before.
    assert inherited["ANTHROPIC_MODEL"] == "a-setting-not-a-credential"
    assert inherited["FIREWORKS_API_KEY"] == FW_KEY
    assert inherited["PATH"] == os.environ["PATH"]

    env = _child_env(adapter, ctx)
    assert env["ANTHROPIC_API_KEY"] == FW_KEY
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"X-Fireworks-Api-Key: {FW_KEY}"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
                "ANTHROPIC_AUTH_TOKEN"):
        assert var not in env, var
    blob = json.dumps(env)
    for var, value in ANTHROPIC_CREDS.items():
        assert value.split(": ")[-1] not in blob, f"{var}'s value reached the CLI env"


def test_an_anthropic_episode_inherits_the_environment_unchanged(parent_env, tmp_path):
    adapter = ClaudeCodeAdapter()
    for model in (ANTHROPIC_MODEL, None):
        ctx = _ctx(tmp_path, model or "m")
        ctx.force_model = model
        assert adapter.inherited_env(ctx) == dict(os.environ)
        env = adapter.env(ctx)
        assert not any(k.startswith("ANTHROPIC_") for k in env), env
        child = _child_env(adapter, ctx)
        for var, value in ANTHROPIC_CREDS.items():
            assert child[var] == value


def test_a_codex_episode_inherits_the_environment_unchanged(parent_env, tmp_path):
    ctx = _ctx(tmp_path, "gpt-6-astra", agent="codex-cli")
    assert CodexCliAdapter().inherited_env(ctx) == dict(os.environ)


# ── what actually goes out on the wire ───────────────────────────────────────


class _FakeAnthropicApi:
    """A local stand-in for an Anthropic-compatible endpoint: records each request's
    headers and answers 401, which ends a claude-code turn at once."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                if n:
                    self.rfile.read(n)
                outer.requests.append({k.lower(): v for k, v in self.headers.items()})
                body = json.dumps({"type": "error", "error": {
                    "type": "authentication_error", "message": "fake endpoint"}}).encode()
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_HEAD = _answer

            def log_message(self, *_a) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_api():
    api = _FakeAnthropicApi()
    yield api
    api.close()


# Sends every credential claude-code reads, the way it sends each one: the API key as
# x-api-key, an auth token or the OAuth token as a bearer token, the custom headers as
# they are. Strictly worse than the CLI, which sends a subset.
_STAND_IN = r"""
import json, os, urllib.request, urllib.error
h = {"content-type": "application/json"}
if os.environ.get("ANTHROPIC_API_KEY"):
    h["x-api-key"] = os.environ["ANTHROPIC_API_KEY"]
for var in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
    if os.environ.get(var):
        h["authorization"] = "Bearer " + os.environ[var]
        h["x-from-" + var.lower()] = os.environ[var]
for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
    name, _, value = line.partition(":")
    if name.strip():
        h[name.strip()] = value.strip()
req = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages",
                             data=b"{}", headers=h, method="POST")
try:
    urllib.request.urlopen(req, timeout=10)
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code}))
"""


class _StandInClaude(ClaudeCodeAdapter):
    """ClaudeCodeAdapter with the `claude` binary swapped for `_STAND_IN`."""

    def command(self, instruction: str, context: RunContext) -> list[str]:
        return [sys.executable, "-c", _STAND_IN]

    async def run(self, instruction: str, context: RunContext) -> tuple[str, int]:
        # The base run: the spawn, its env merge and the stream, without claude-code's
        # transcript post-processing (there is no stream-json here).
        return await super(ClaudeCodeAdapter, self).run(instruction, context)


def _all_header_text(requests: list[dict[str, str]]) -> str:
    return "\n".join(f"{k}: {v}" for r in requests for k, v in r.items())


def test_a_fireworks_request_carries_only_the_fireworks_key(parent_env, fake_api,
                                                             tmp_path):
    """The leak, end to end through `AgentAdapter.run`: the parent holds every
    Anthropic credential, the episode is on Fireworks (pointed at the fake through
    `agent_env`, which is merged last, as the harness's own overrides are)."""
    ctx = _ctx(tmp_path, FW_MODEL, agent_env={"ANTHROPIC_BASE_URL": fake_api.url})
    _, code = asyncio.run(_StandInClaude().run("go", ctx))
    assert code == 0
    [req] = fake_api.requests
    assert req["x-api-key"] == FW_KEY
    assert req["x-fireworks-api-key"] == FW_KEY
    assert "authorization" not in req
    sent = _all_header_text(fake_api.requests)
    for var, value in ANTHROPIC_CREDS.items():
        assert value.split(": ")[-1] not in sent, f"{var} was sent to Fireworks"


def test_an_anthropic_request_still_carries_the_anthropic_credentials(parent_env,
                                                                      fake_api, tmp_path):
    """Control for the test above: the same stand-in on an Anthropic model sends the
    parent's credentials exactly as before, so the stand-in can see a leak."""
    ctx = _ctx(tmp_path, ANTHROPIC_MODEL, agent_env={"ANTHROPIC_BASE_URL": fake_api.url})
    asyncio.run(_StandInClaude().run("go", ctx))
    [req] = fake_api.requests
    assert req["x-api-key"] == ANTHROPIC_CREDS["ANTHROPIC_API_KEY"]
    assert req["x-from-claude_code_oauth_token"] == ANTHROPIC_CREDS["CLAUDE_CODE_OAUTH_TOKEN"]
    assert FW_KEY not in _all_header_text(fake_api.requests)


REAL_CLI_ENV = "QUALGENTBENCH_REAL_CLAUDE_CLI"


@pytest.mark.skipif(not (os.environ.get(REAL_CLI_ENV) == "1" and shutil.which("claude")),
                    reason=f"runs the real claude CLI: set {REAL_CLI_ENV}=1 with "
                           "`claude` on PATH")
def test_the_real_cli_sends_only_the_fireworks_key(parent_env, fake_api, tmp_path):
    """The same check with the real `claude` binary, through the unmodified adapter.
    Talks only to the local fake (ANTHROPIC_BASE_URL) with dummy credentials, and the
    Fireworks env turns off the CLI's nonessential traffic. Measured with 2.1.311: one
    request, `x-api-key` and `X-Fireworks-Api-Key` both the Fireworks key, no
    `Authorization`; before the fix, `Authorization: Bearer <the OAuth token>`."""
    ctx = _ctx(tmp_path, FW_MODEL, agent_env={"ANTHROPIC_BASE_URL": fake_api.url},
               timeout=120)
    transcript, _ = asyncio.run(ClaudeCodeAdapter().run("Say hi.", ctx))
    assert fake_api.requests, f"the CLI sent no request:\n{transcript[-2000:]}"
    assert "Not logged in" not in transcript
    for req in fake_api.requests:
        assert req.get("x-api-key") == FW_KEY
        assert "authorization" not in req
    sent = _all_header_text(fake_api.requests)
    for var, value in ANTHROPIC_CREDS.items():
        assert value.split(": ")[-1] not in sent, f"{var} was sent to Fireworks"
