"""Provider-aware codex-cli and claude-code adapters (QUA-3502).

Each harness × provider pair builds the environment and config it needs and nothing
else; an unsupported pair is refused with a sentence naming why; a missing provider
credential is refused naming the variable; Bedrock usage prices from the Bedrock rows.

The verified matrix these tests pin (live calls, 2026-11-17/18):

* codex-cli on Bedrock: `model_provider = "amazon-bedrock-runtime"` + an inference
  profile id (`us.openai.gpt-6-astra`, `us.moonshotai.kimi-k3`); Claude and bare ids fail.
* claude-code on Bedrock: `CLAUDE_CODE_USE_BEDROCK=1`, Claude models only.
* claude-code on Fireworks: `accounts/fireworks/models/*`.

No key here is real: every credential is a dummy built in this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualgentbench import pricing, providers
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import AUTH_BEDROCK, CodexCliAdapter
from qualgentbench.config import BenchConfig
from qualgentbench.episode_runner import model_of_record
from qualgentbench.schemas import Condition
from qualgentbench.transcript import TranscriptParser

BEDROCK_TOKEN = "bedrock-api-key-" + "x" * 24          # dummy, not a key
FW_KEY = "fw_" + "y" * 24                              # dummy, not a key
KIMI = "us.moonshotai.kimi-k3"
ASTRA = "us.openai.gpt-6-astra"
SONNET = "us.anthropic.claude-sonnet-5-5"
GLM = "accounts/fireworks/models/glm-5p3-flash"

# What a host might carry that no agent should inherit.
HOST_CLOUD = {
    "AWS_PROFILE": "operator-profile",
    "AWS_ACCESS_KEY_ID": "AKIA" + "Z" * 16,
    "AWS_SECRET_ACCESS_KEY": "s" * 40,
    "AWS_SESSION_TOKEN": "t" * 40,
    "AWS_DEFAULT_REGION": "eu-west-1",
    "CLAUDE_CODE_USE_VERTEX": "1",
}


@pytest.fixture
def bedrock_env(monkeypatch):
    for var, value in HOST_CLOUD.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", BEDROCK_TOKEN)
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-dummy")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-dummy")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-dummy")
    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)


def _ctx(tmp_path: Path, agent: str, model: str | None,
         provider: str | None = None) -> RunContext:
    run_dir, ws = tmp_path / "run", tmp_path / "ws"
    run_dir.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=60)),
        agent=agent, model=model or "", condition=Condition.no_routines, trial=1,
        run_dir=run_dir, mcp_server="", mcp_config_path=mcp, workspace_dir=ws,
        no_mcp=True, force_model=model, provider=provider)


def _child_env(adapter, ctx: RunContext) -> dict[str, str]:
    """The environment `AgentAdapter.run` hands the agent, merged the same way."""
    return {**adapter.inherited_env(ctx), **adapter.env(ctx), **(ctx.agent_env or {})}


def _no_codex_spawn(monkeypatch):
    """`prepare` spawns `codex --version` and (on OpenAI) `codex login`; neither may run."""
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("qualgentbench.adapters.codex_cli.subprocess.run", fake_run)
    return calls


# ── resolution ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent,model,explicit,expected", [
    ("codex-cli", "gpt-6-astra", None, "openai"),
    ("codex-cli", KIMI, None, "bedrock"),                 # the id's shape says Bedrock
    ("codex-cli", "gpt-6-astra", "bedrock", "bedrock"),   # explicit wins
    ("claude-code", None, None, "anthropic"),
    ("claude-code", "claude-sonnet-5-5", None, "anthropic"),
    ("claude-code", GLM, None, "fireworks"),
    ("claude-code", SONNET, None, "bedrock"),
    ("native", "gpt-6-astra", None, None),                # not routed here
])
def test_resolve(agent, model, explicit, expected, monkeypatch):
    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    assert providers.resolve(agent, model, explicit) == expected


def test_resolve_reads_the_run_wide_env_var_and_refuses_unknown_names(monkeypatch):
    monkeypatch.setenv(providers.PROVIDER_ENV, "Bedrock")
    assert providers.resolve("codex-cli", "gpt-6-astra") == "bedrock"
    monkeypatch.setenv(providers.PROVIDER_ENV, "azure")
    with pytest.raises(providers.ProviderError, match="unknown provider"):
        providers.resolve("codex-cli", "gpt-6-astra")


# ── the supported matrix, and the refusals ────────────────────────────────────

@pytest.mark.parametrize("agent,provider,model", [
    ("codex-cli", "openai", "gpt-6-astra"),
    ("codex-cli", "bedrock", KIMI),
    ("codex-cli", "bedrock", ASTRA),
    ("claude-code", "anthropic", "claude-sonnet-5-5"),
    ("claude-code", "anthropic", None),
    ("claude-code", "fireworks", GLM),
    ("claude-code", "bedrock", SONNET),
    ("claude-code", "bedrock", "global.anthropic.claude-opus-5-5"),
])
def test_supported_pairs_are_not_refused(agent, provider, model):
    assert providers.pair_refusal(agent, provider, model) is None


@pytest.mark.parametrize("agent,provider,model,needle", [
    # claude-code × Bedrock runs Claude only: Kimi fails ("unrecognized model").
    ("claude-code", "bedrock", KIMI, "Claude models only"),
    # codex × Bedrock: Claude 404s on the Responses route; bare ids fail.
    ("codex-cli", "bedrock", SONNET, "claude-code --provider bedrock"),
    ("codex-cli", "bedrock", "kimi-k3", "inference-profile model id"),
    ("codex-cli", "bedrock", None, "inference-profile model id"),
    ("claude-code", "bedrock", "claude-sonnet-5-5", "inference-profile model id"),
    ("codex-cli", "bedrock", "us.meta.llama4-maverick", "verified for openai, moonshotai"),
    # Unwired routes.
    ("codex-cli", "fireworks", GLM, "no fireworks route"),
    ("codex-cli", "anthropic", "claude-sonnet-5-5", "no anthropic route"),
    ("claude-code", "openai", "gpt-6-astra", "no openai route"),
    # A model id shaped for another provider than the one asked for.
    ("claude-code", "fireworks", "claude-sonnet-5-5", "needs a Fireworks slug"),
    ("claude-code", "anthropic", SONNET, "pass --provider bedrock"),
    ("codex-cli", "openai", KIMI, "pass --provider bedrock"),
])
def test_unsupported_pairs_are_refused_with_a_reason(agent, provider, model, needle):
    refusal = providers.pair_refusal(agent, provider, model)
    assert refusal is not None and needle in refusal


def test_preflight_problems_name_the_missing_variable(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    found = providers.problems("codex-cli", "bedrock", KIMI)
    assert len(found) == 2
    assert "AWS_BEARER_TOKEN_BEDROCK" in found[0] and "AWS_REGION" in found[1]
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", BEDROCK_TOKEN)
    found = providers.problems("claude-code", "bedrock", SONNET)
    assert found == [found[0]] and "AWS_REGION" in found[0]
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    assert providers.problems("claude-code", "bedrock", SONNET) == []
    # The pair refusal comes first and alone: no point naming variables for a pair
    # that can never run.
    assert providers.problems("claude-code", "bedrock", KIMI) == [
        providers.pair_refusal("claude-code", "bedrock", KIMI)]


def test_fireworks_key_is_named_when_missing(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_AI_API_KEY", raising=False)
    (problem,) = providers.problems("claude-code", "fireworks", GLM)
    assert "FIREWORKS_API_KEY" in problem


# ── codex-cli ─────────────────────────────────────────────────────────────────

def test_codex_bedrock_config_env_and_auth(tmp_path, monkeypatch, bedrock_env):
    calls = _no_codex_spawn(monkeypatch)
    adapter, ctx = CodexCliAdapter(), _ctx(tmp_path, "codex-cli", KIMI)
    adapter.prepare(ctx)

    config = (ctx.run_dir / "codex_home" / "config.toml").read_text()
    assert 'model = "us.moonshotai.kimi-k3"' in config
    assert 'model_provider = "amazon-bedrock-runtime"' in config
    # No OpenAI key exchange, no login copy, nothing auth-shaped in CODEX_HOME.
    assert not any(c[:2] == ["codex", "login"] for c in calls)
    assert not (ctx.run_dir / "codex_home" / "auth.json").exists()
    assert ctx.auth_mode == AUTH_BEDROCK and ctx.auth_login_allowed is None

    env = _child_env(adapter, ctx)
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == BEDROCK_TOKEN
    assert env["AWS_REGION"] == "us-east-2"
    # Only the AWS variables the provider needs; none of the host's.
    assert sorted(k for k in env if k.startswith("AWS_")) == [
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"]
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    # And no OpenAI credential rides along to a Bedrock episode.
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env


def test_codex_openai_path_is_unchanged_except_for_cloud_variables(tmp_path, monkeypatch,
                                                                   bedrock_env):
    _no_codex_spawn(monkeypatch)
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-dummy")
    adapter, ctx = CodexCliAdapter(), _ctx(tmp_path, "codex-cli", "gpt-6-astra")
    adapter.prepare(ctx)
    config = (ctx.run_dir / "codex_home" / "config.toml").read_text()
    assert "model_provider" not in config
    env = _child_env(adapter, ctx)
    assert env["CODEX_API_KEY"] == "sk-codex-dummy"
    assert env["OPENAI_API_KEY"] == "sk-openai-dummy"
    assert not any(k.startswith("AWS_") for k in env)


def test_codex_refuses_an_unsupported_pair_in_prepare(tmp_path, monkeypatch, bedrock_env):
    _no_codex_spawn(monkeypatch)
    adapter = CodexCliAdapter()
    with pytest.raises(providers.ProviderError, match="claude-code --provider bedrock"):
        adapter.prepare(_ctx(tmp_path, "codex-cli", SONNET))


def test_codex_bedrock_is_never_refused_for_the_account_login(tmp_path, monkeypatch):
    """The QUA-2868 login refusal is about billing a ChatGPT workspace; a Bedrock
    episode never touches that login, so it is not this guard's business."""
    home = tmp_path / "operator_codex"
    home.mkdir()
    (home / "auth.json").write_text('{"fake":"login"}')
    monkeypatch.setenv(CodexCliAdapter._AUTH_HOME_ENV, str(home))
    adapter = CodexCliAdapter()
    assert adapter.auth_refusal() is not None              # openai: refused
    assert adapter.auth_refusal("bedrock") is None
    monkeypatch.setenv(providers.PROVIDER_ENV, "bedrock")
    assert adapter.auth_refusal() is None


def test_codex_bedrock_without_credentials_fails_naming_them(tmp_path, monkeypatch):
    _no_codex_spawn(monkeypatch)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    adapter, ctx = CodexCliAdapter(), _ctx(tmp_path, "codex-cli", ASTRA)
    adapter.prepare(ctx)
    with pytest.raises(providers.ProviderError, match="AWS_BEARER_TOKEN_BEDROCK"):
        adapter.env(ctx)


# ── claude-code ───────────────────────────────────────────────────────────────

def test_claude_bedrock_env(tmp_path, bedrock_env):
    adapter, ctx = ClaudeCodeAdapter(), _ctx(tmp_path, "claude-code", SONNET)
    env = _child_env(adapter, ctx)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "us-east-2"
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == BEDROCK_TOKEN
    assert env["ANTHROPIC_MODEL"] == SONNET
    # The background (Haiku-class) model is the episode's own: one model of record.
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == SONNET
    assert sorted(k for k in env if k.startswith("AWS_")) == [
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"]
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    # No Anthropic credential reaches a Bedrock episode.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert "--model" in adapter.command("x", ctx)


def test_claude_bedrock_rejects_a_non_claude_model(tmp_path, bedrock_env):
    adapter = ClaudeCodeAdapter()
    ctx = _ctx(tmp_path, "claude-code", KIMI, provider="bedrock")
    with pytest.raises(providers.ProviderError, match="Claude models only"):
        adapter.env(ctx)


def test_claude_anthropic_path_keeps_its_credentials_but_no_cloud_variables(tmp_path,
                                                                           bedrock_env,
                                                                           monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")   # a host setting must not reroute
    adapter, ctx = ClaudeCodeAdapter(), _ctx(tmp_path, "claude-code", "claude-sonnet-5-5")
    env = _child_env(adapter, ctx)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-dummy"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert not any(k.startswith("AWS_") for k in env)


def test_claude_fireworks_path_is_unchanged(tmp_path, bedrock_env, monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", FW_KEY)
    adapter, ctx = ClaudeCodeAdapter(), _ctx(tmp_path, "claude-code", GLM)
    env = _child_env(adapter, ctx)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.fireworks.ai/inference"
    assert env["ANTHROPIC_API_KEY"] == FW_KEY
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env


def test_claude_auth_helpers_know_bedrock(bedrock_env, monkeypatch):
    assert ClaudeCodeAdapter.auth_source(SONNET) == "AWS_BEARER_TOKEN_BEDROCK"
    assert not ClaudeCodeAdapter.needs_anthropic_auth([SONNET])
    assert ClaudeCodeAdapter.needs_anthropic_auth(["claude-sonnet-5-5"])
    assert "Amazon Bedrock" in ClaudeCodeAdapter.credit_guard_note(SONNET)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    assert ClaudeCodeAdapter.auth_source(SONNET) is None


# ── preflight (config) ────────────────────────────────────────────────────────

def _cfg(agent: str, model: str, provider: str | None = None) -> BenchConfig:
    return BenchConfig.model_validate({"agent": agent, "model": model,
                                       "provider": provider,
                                       "scope": {"apps": ["birday"]}})


def test_config_preflight_checks(bedrock_env, monkeypatch):
    from qualgentbench import preflight as pf

    assert pf.check_model(_cfg("codex-cli", KIMI)).passed
    assert pf._check_auth(_cfg("codex-cli", KIMI)).passed
    bad = pf.check_model(_cfg("claude-code", KIMI, "bedrock"))
    assert not bad.passed and "Claude models only" in bad.detail
    monkeypatch.delenv("AWS_REGION")
    auth = pf._check_auth(_cfg("claude-code", SONNET))
    assert not auth.passed and "AWS_REGION" in auth.detail
    with pytest.raises(Exception):
        _cfg("codex-cli", KIMI, "azure")


# ── pricing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model_id,key", [
    (KIMI, "kimi-k3"),
    (SONNET, "claude-sonnet-5-5"),
    ("global.anthropic.claude-opus-5-5", "claude-opus-5-5"),
    (ASTRA, "gpt-6-astra"),
    ("anthropic.claude-haiku-5-5-v1:0", "claude-haiku-5-5"),
    ("gpt-5.5", "gpt-5.5"),                    # a dotted name is not a Bedrock prefix
    ("openai/gpt-6-astra", "gpt-6-astra"),
])
def test_normalize_strips_bedrock_scope_and_vendor(model_id, key):
    assert pricing.normalize_model(model_id) == key


def test_bedrock_models_price_from_the_bedrock_rows():
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}
    # Regional (us.) is AWS's "Standard" rate, 1.1x the global one.
    assert pricing.compute_cost_usd(KIMI, usage) == pytest.approx(3.30)
    assert pricing.compute_cost_usd(SONNET, usage) == pytest.approx(2.20)
    assert pricing.compute_cost_usd("global.anthropic.claude-sonnet-5-5", usage) \
        == pytest.approx(2.00)
    # The first-party row is untouched for the bare id.
    assert pricing.compute_cost_usd("claude-sonnet-5-5", usage) == pytest.approx(2.00)
    # Kimi K3.1 has no published Bedrock price: unpriced, never a borrowed number.
    assert pricing.compute_cost_usd("us.moonshotai.kimi-k3.1", usage) is None


def test_cache_writes_bill_at_the_write_rate_on_bedrock_only():
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 200_000,
             "cache_write_input_tokens": 300_000, "output_tokens": 100_000}
    # Sonnet 5.5, us.: 500k uncached × 2.20 + 200k reads × 0.11 + 300k writes × 2.75
    # + 100k out × 11.00
    assert pricing.compute_cost_usd(SONNET, usage) == pytest.approx(
        (500_000 * 2.20 + 200_000 * 0.11 + 300_000 * 2.75 + 100_000 * 11.00) / 1e6)
    # A first-party row has no write rate: writes stay at `input`, as before.
    assert pricing.compute_cost_usd("claude-sonnet-5-5", usage) == pytest.approx(
        pricing.compute_cost_usd("claude-sonnet-5-5",
                                 {**usage, "cache_write_input_tokens": 0}))


def test_claude_code_reported_cost_is_not_trusted_on_bedrock():
    usage = {"input_tokens": 17061, "cached_input_tokens": 0,
             "cache_write_input_tokens": 17059, "output_tokens": 4,
             "total_tokens": 17065, "usage_source": "result",
             # claude-code's own number: Anthropic list price, not AWS's.
             "reported_cost_usd": 0.0426915}
    block = pricing.usage_metrics(SONNET, usage)
    assert block["cost_source"] == pricing.COST_ESTIMATED
    assert block["cost_usd"] == pytest.approx((2 * 2.20 + 17059 * 2.75 + 4 * 11.0) / 1e6,
                                             abs=1e-6)
    assert block["cache_write_input_tokens"] == 17059
    # On Anthropic the reported number still wins.
    assert pricing.usage_metrics("claude-sonnet-5-5", usage)["cost_source"] \
        == pricing.COST_REPORTED


# ── transcripts and the model of record ───────────────────────────────────────

def test_codex_bedrock_usage_records_cache_writes():
    # The shape codex-cli 0.156.1 printed on Bedrock (live, 2026-11-18).
    transcript = json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 9292, "cached_input_tokens": 1000,
        "cache_write_input_tokens": 500, "output_tokens": 40,
        "reasoning_output_tokens": 23}})
    usage = TranscriptParser(transcript).token_usage()
    assert usage["usage_source"] == "turns"
    assert usage["cache_write_input_tokens"] == 500
    assert pricing.usage_metrics(KIMI, usage)["cost_source"] == pricing.COST_ESTIMATED


def test_claude_result_usage_records_cache_writes():
    transcript = json.dumps({"type": "result", "total_cost_usd": 0.04, "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 17059,
        "cache_read_input_tokens": 10, "output_tokens": 4}})
    usage = TranscriptParser(transcript).token_usage()
    assert usage["cache_write_input_tokens"] == 17059
    assert usage["input_tokens"] == 2 + 17059 + 10


def test_model_of_record_on_bedrock_is_the_inference_profile():
    # claude-code reports the bare id; codex reports none. The route is the record.
    claude = json.dumps({"type": "assistant", "message": {
        "model": "claude-sonnet-5-5", "content": [], "usage": {}}})
    assert model_of_record(claude, SONNET, SONNET, "bedrock") == SONNET
    assert model_of_record("", KIMI, KIMI, "bedrock") == KIMI
    # Off Bedrock/Fireworks the transcript's model still wins.
    assert model_of_record(claude, "claude-sonnet-5", "claude-sonnet-5",
                           "anthropic") == "claude-sonnet-5-5"


def test_the_provider_is_recorded_on_the_result():
    from qualgentbench.result import RunResult

    base = dict(task_id="t", task_version="v", task_type="journey", agent="codex-cli",
                model=KIMI, condition="c", trial=1, passed=True, score=1.0,
                started_at="", ended_at="", wall_time_sec=0.0, exit_code=0)
    assert RunResult(**base, provider="bedrock").model_dump()["provider"] == "bedrock"
    # Absent (not null) when unknown, so older result.json shapes are unchanged.
    assert "provider" not in RunResult(**base).model_dump()
