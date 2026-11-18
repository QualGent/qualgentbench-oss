"""Claude Code adapter — invokes the `claude` CLI in non-interactive print mode."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import AgentAdapter, RunContext
from .. import credit, pricing, providers
from ..interactions import BUDGET_HOOK
from ..transcript import claude_code_version, claude_session_usage_line


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"

    # The benchmark registers exactly one MCP server, under this name.
    _SERVER = "device"
    # The only auth the harness accepts for claude-code on an Anthropic model.
    _AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
    # The only auth a Fireworks-routed model needs (and the only one it may see).
    _FIREWORKS_KEY_ENV = ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY")
    # Every Anthropic credential claude-code can read from its environment: the OAuth
    # token family, and any ANTHROPIC_* variable that carries a key, a token or a header
    # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_CUSTOM_HEADERS, …). Settings
    # such as ANTHROPIC_MODEL are left alone.
    _ANTHROPIC_CREDENTIAL = re.compile(
        r"CLAUDE_CODE_OAUTH_\w*"
        r"|ANTHROPIC_\w*(?:KEY|TOKEN|SECRET|AUTH|CREDENTIAL|HEADER|PASSWORD)\w*")

    @classmethod
    def _prefixed(cls, tools: list[str]) -> list[str]:
        return [f"mcp__{cls._SERVER}__{t}" for t in tools]

    @staticmethod
    def _config_dir(context: RunContext) -> Path:
        return context.run_dir / "claude_home"

    @classmethod
    def provider_for(cls, model: str | None, provider: str | None = None) -> str:
        """The provider an episode on ``model`` runs against (`providers.resolve`:
        an explicit choice, QGB_PROVIDER, the model id's shape, else Anthropic)."""
        return providers.resolve(cls.name, model, provider) or providers.ANTHROPIC

    @classmethod
    def _provider(cls, context: RunContext) -> str:
        return cls.provider_for(context.force_model or context.model, context.provider)

    @classmethod
    def auth_source(cls, model: str | None = None,
                    provider: str | None = None) -> str | None:
        """Which env variable authenticates claude-code for ``model``, or None.

        A Fireworks-routed model authenticates with the Fireworks key alone, a
        Bedrock one with the Bedrock API key: the adapter hands the CLI that one
        credential and keeps every Anthropic credential away from it (`env`,
        `inherited_env`), so an Anthropic token is neither needed nor used. Any other
        model (None: the CLI's own default) needs an Anthropic one."""
        route = cls.provider_for(model, provider)
        if route == providers.FIREWORKS:
            return next((v for v in cls._FIREWORKS_KEY_ENV
                         if (os.environ.get(v) or "").strip()), None)
        if route == providers.BEDROCK:
            return (providers.BEDROCK_TOKEN_ENV
                    if (os.environ.get(providers.BEDROCK_TOKEN_ENV) or "").strip() else None)
        return next((v for v in cls._AUTH_ENV if os.environ.get(v)), None)

    @classmethod
    def needs_anthropic_auth(cls, models: list[str] | None,
                             provider: str | None = None) -> bool:
        """Whether a run over ``models`` needs an Anthropic credential: True unless
        every model routes elsewhere (no models = the CLI's own Anthropic default)."""
        return any(cls.provider_for(m, provider) == providers.ANTHROPIC
                   for m in models or [None])

    @classmethod
    def subscription_auth(cls) -> bool:
        """Is this run billing a Claude subscription?

        Only subscription (OAuth) auth reports usage windows on the stream, so it is
        also the only auth under which the credit guard can do anything. An
        `ANTHROPIC_API_KEY` run emits no `rate_limit_event` at all — the guard is then
        inert rather than disabled, and preflight says so instead of implying a budget
        is being watched.

        Decided by `auth_source`, which is where the harness already resolves "which
        variable authenticates claude-code"; a second opinion here would drift from
        what preflight prints.
        """
        return cls.auth_source() == "CLAUDE_CODE_OAUTH_TOKEN"

    @classmethod
    def credit_guard_note(cls, model: str | None = None,
                          provider: str | None = None) -> str | None:
        """Why the credit guard will not fire on this run, or None when it will.

        One sentence, printed by preflight: a user who set a seven-day stop threshold
        and is quietly running on an API key would otherwise believe the sweep is
        being watched.
        """
        route = cls.provider_for(model, provider)
        if route in (providers.FIREWORKS, providers.BEDROCK):
            where = "Fireworks" if route == providers.FIREWORKS else "Amazon Bedrock"
            return (f"credit guard inactive (no subscription windows) — {model} runs on "
                    f"{where}, which reports no Claude usage windows")
        if not cls.subscription_auth():
            return ("credit guard inactive (no subscription windows) — ANTHROPIC_API_KEY "
                    "auth emits no rate_limit_event; run on CLAUDE_CODE_OAUTH_TOKEN to "
                    "stop the sweep on a seven-day threshold")
        return None

    def stream_watcher(self, context: RunContext) -> credit.RateLimitWatcher | None:
        """Watch this episode's stream for `rate_limit_event`.

        Built only when the run can actually produce those events. On an API key or a
        Fireworks-routed model there are none, and a watcher would be a file handle
        and a per-chunk scan bought for nothing.
        """
        if self.credit_guard_note(context.force_model or context.model,
                                  context.provider) is not None:
            return None
        return credit.RateLimitWatcher(context.run_dir, context.run_meta_dir)

    @classmethod
    def auth_fix(cls) -> str:
        return ("claude-code needs CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in the "
                "environment (.env). Your interactive `claude` login cannot be used: each "
                "episode runs in its own config dir. Run `claude setup-token` once and put "
                "the token in .env as CLAUDE_CODE_OAUTH_TOKEN=...")

    async def run(self, instruction: str, context: RunContext) -> tuple[str, int]:
        transcript, exit_code = await super().run(instruction, context)
        # The CLI names its own version on the stream (`system`/`init`), so nothing is
        # spawned to ask it; None when the run died before init.
        context.agent_cli_version = claude_code_version(transcript)
        return self.with_session_usage(context, transcript), exit_code

    def with_session_usage(self, context: RunContext, transcript: str) -> str:
        """Append claude-code's own token total when stdout never reported usage.

        A truncated episode has no `result` event, and the per-request fallback reads
        zeros for a Fireworks-served model (`transcript.CLAUDE_SESSION_USAGE_EVENT`).
        The session log under this episode's CLAUDE_CONFIG_DIR has the real counts,
        so they are appended as one `qgb.claude_session_usage` line: to the returned
        transcript, which every scorer reads, and to `agent/transcript.txt`, which a
        rescore reads. Nothing is appended when stdout reported usage.
        """
        line = claude_session_usage_line(self._config_dir(context), transcript)
        if line is None:
            return transcript
        sep = "" if not transcript or transcript.endswith("\n") else "\n"
        addition = f"{sep}{line}\n"
        path = context.run_dir / "agent" / "transcript.txt"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(addition)
        except OSError:
            pass
        return transcript + addition

    def _seed_config(self, config_dir: Path) -> None:
        config_dir.mkdir(parents=True, exist_ok=True)
        # The global config the CLI would otherwise create interactively.
        (config_dir / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "bypassPermissionsModeAccepted": True,
        }, indent=2))

    def prepare(self, context: RunContext) -> None:
        """Seed the per-run config dir; with a tool-call cap, also write a PreToolUse
        hook that counts calls and drops the sentinel base.run() kills on — the CLI
        equivalent of the native adapter's enforced step budget."""
        self._seed_config(self._config_dir(context))
        if not context.tool_call_cap:
            return
        hooks_dir = context.run_dir / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        count_file = hooks_dir / "count"
        count_file.write_text("0")
        script = hooks_dir / "tool_cap.py"
        script.write_text(BUDGET_HOOK.format(
            count_file=count_file,
            meter_file=context.run_dir / "interactions.json",
            cap=int(context.tool_call_cap),
            sentinel=context.run_dir / "truncated",
        ))
        script.chmod(0o755)
        settings = {"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": str(script)}]}
        ]}}
        (hooks_dir / "settings.json").write_text(json.dumps(settings, indent=2))

    def command(self, instruction: str, context: RunContext) -> list[str]:
        # QGB_DISALLOWED_TOOLS is the only source; nothing is withheld by default.
        disallowed = [] if context.no_mcp else self._prefixed(context.disabled_tools or [])
        disallowed = list(dict.fromkeys(disallowed))

        # No --model by default: let the CLI use its own; the model actually used
        # is recovered from the transcript at verdict time.
        cmd = [
            "claude",
            "--print",
            "--verbose",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
        ]
        if disallowed:
            cmd += ["--disallowedTools", ",".join(disallowed)]
        if context.force_model:
            cmd += ["--model", context.force_model]
        if context.tool_call_cap:
            cmd += ["--settings", str(context.run_dir / "hooks" / "settings.json")]
        # Raw condition: empty config + strict, so the CLI ignores the user's global
        # MCP servers — otherwise the raw arm silently inherits them.
        if context.no_mcp:
            cmd += ["--mcp-config", str(context.mcp_config_path), "--strict-mcp-config"]
        # When not injecting, reuse the agent's existing MCP server — a second
        # connection would split the device lock. --disallowedTools still gates it.
        elif context.isolate_mcp:
            # MCP ablation: MCP-only config + strict, so the agent gets exactly one
            # MCP server and none of the user's other global servers.
            cmd += ["--mcp-config", str(context.mcp_config_path), "--strict-mcp-config"]
        elif context.inject_mcp:
            cmd += ["--mcp-config", str(context.mcp_config_path)]
        return cmd

    # A model id of this shape routes to Fireworks — the model itself declares
    # the provider, so no separate flag can drift out of sync. One predicate, shared
    # with pricing, which must agree on what was routed there.
    @classmethod
    def is_fireworks_model(cls, model: str | None) -> bool:
        return pricing.is_fireworks_model(model)

    @classmethod
    def _fireworks_key(cls) -> str:
        return next((key for v in cls._FIREWORKS_KEY_ENV
                     if (key := (os.environ.get(v) or "").strip())), "")

    def _fireworks_env(self, model: str) -> dict[str, str]:
        """Point claude-code at Fireworks' Anthropic-compatible endpoint. The env vars
        turn off extras Fireworks 400s on (adaptive thinking, experimental betas).

        The CLI is authenticated with the FIREWORKS key as its ANTHROPIC_API_KEY: with
        no credential at all it exits "Not logged in" before sending a request, and in
        `--print` mode an API key from the environment is used without the interactive
        approval the TUI asks for. `inherited_env` removes every Anthropic credential
        first, so this is the only key the CLI holds — whatever it sends to Fireworks
        (`x-api-key`, and the custom header) is the Fireworks key and nothing else."""
        key = self._fireworks_key()
        if not key:
            raise RuntimeError(
                f"model {model!r} routes to Fireworks but neither FIREWORKS_API_KEY nor "
                "FIREWORKS_AI_API_KEY is set")
        return {
            # No /v1 — the SDK appends it.
            "ANTHROPIC_BASE_URL": "https://api.fireworks.ai/inference",
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_CUSTOM_HEADERS": f"X-Fireworks-Api-Key: {key}",
            "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384",
        }

    @staticmethod
    def _bedrock_env(model: str | None) -> dict[str, str]:
        """Run claude-code on Amazon Bedrock (`CLAUDE_CODE_USE_BEDROCK`), Claude models
        only — Kimi and other vendors are refused here (`providers.pair_refusal`), as
        the CLI itself fails on them with "unrecognized model".

        Authenticated by the Bedrock API key alone (`AWS_BEARER_TOKEN_BEDROCK`) in
        `AWS_REGION`; `inherited_env` has already removed the host's AWS profile, keys
        and every Anthropic credential. The small/fast (Haiku-class) model the CLI uses
        for background work is pinned to the episode's own model: the episode then
        runs, and prices, as ONE model of record, and never calls a Haiku id the
        account may not be entitled to."""
        if refusal := providers.pair_refusal("claude-code", providers.BEDROCK, model):
            raise providers.ProviderError(refusal)
        assert model is not None
        return {
            **providers.bedrock_env(),
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def env(self, context: RunContext) -> dict[str, str]:
        env: dict[str, str] = {"CLAUDE_CONFIG_DIR": str(self._config_dir(context))}
        # Long device routines can run minutes; pin the per-tool MCP timeout to 5 min
        # so the agent→bridge hop agrees with the bridge→upstream ceiling.
        env["MCP_TOOL_TIMEOUT"] = "300000"

        model = context.force_model or context.model
        route = self._provider(context)
        if route == providers.FIREWORKS:
            if refusal := providers.pair_refusal(self.name, route, model):
                raise providers.ProviderError(refusal)
            env.update(self._fireworks_env(model))
        elif route == providers.BEDROCK:
            env.update(self._bedrock_env(model))
        return env

    def inherited_env(self, context: RunContext) -> dict[str, str]:
        """This process's environment, less every cloud-account variable, and less
        every Anthropic credential when the episode routes off Anthropic.

        claude-code sends whatever Anthropic credential it finds to ANTHROPIC_BASE_URL.
        For a Fireworks episode that is Fireworks: a CLAUDE_CODE_OAUTH_TOKEN left in the
        environment went there as `Authorization: Bearer` on every request, beside the
        Fireworks key. So the Anthropic credentials are dropped here, before `env()`
        sets ANTHROPIC_API_KEY to the Fireworks key; a Bedrock episode drops them too.
        The host's AWS variables (profile, keys, region) and any CLAUDE_CODE_USE_BEDROCK
        / _VERTEX switch never reach any episode (`providers.strip_cloud_env`): a
        Bedrock episode gets exactly its two AWS variables back from `env()`."""
        env = providers.strip_cloud_env(super().inherited_env(context))
        if self._provider(context) == providers.ANTHROPIC:
            return env
        return {k: v for k, v in env.items()
                if not self._ANTHROPIC_CREDENTIAL.fullmatch(k)}
