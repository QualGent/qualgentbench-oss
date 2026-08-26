"""Claude Code adapter — invokes the `claude` CLI in non-interactive print mode."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import AgentAdapter, RunContext
from ..interactions import BUDGET_HOOK


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"

    # The benchmark registers exactly one MCP server, under this name.
    _SERVER = "device"
    # The only auth the harness accepts for claude-code.
    _AUTH_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

    @classmethod
    def _prefixed(cls, tools: list[str]) -> list[str]:
        return [f"mcp__{cls._SERVER}__{t}" for t in tools]

    @staticmethod
    def _config_dir(context: RunContext) -> Path:
        return context.run_dir / "claude_home"

    @classmethod
    def auth_source(cls) -> str | None:
        """Which env variable authenticates claude-code, or None."""
        return next((v for v in cls._AUTH_ENV if os.environ.get(v)), None)

    @classmethod
    def auth_fix(cls) -> str:
        return ("claude-code needs CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in the "
                "environment (.env). Your interactive `claude` login cannot be used: each "
                "episode runs in its own config dir. Run `claude setup-token` once and put "
                "the token in .env as CLAUDE_CODE_OAUTH_TOKEN=...")

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
    # the provider, so no separate flag can drift out of sync.
    _FIREWORKS_PREFIX = "accounts/fireworks/models/"

    @classmethod
    def is_fireworks_model(cls, model: str | None) -> bool:
        return bool(model and model.startswith(cls._FIREWORKS_PREFIX))

    @staticmethod
    def _fireworks_key() -> str:
        return (os.environ.get("FIREWORKS_API_KEY")
                or os.environ.get("FIREWORKS_AI_API_KEY") or "").strip()

    def _fireworks_env(self, model: str) -> dict[str, str]:
        """Point claude-code at Fireworks' Anthropic-compatible endpoint. The env vars
        turn off extras Fireworks 400s on (adaptive thinking, experimental betas); the
        key goes in a header — ANTHROPIC_API_KEY triggers an interactive approval."""
        key = self._fireworks_key()
        if not key:
            raise RuntimeError(
                f"model {model!r} routes to Fireworks but neither FIREWORKS_API_KEY nor "
                "FIREWORKS_AI_API_KEY is set")
        return {
            # No /v1 — the SDK appends it.
            "ANTHROPIC_BASE_URL": "https://api.fireworks.ai/inference",
            "ANTHROPIC_CUSTOM_HEADERS": f"X-Fireworks-Api-Key: {key}",
            "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "16384",
        }

    def env(self, context: RunContext) -> dict[str, str]:
        env: dict[str, str] = {"CLAUDE_CONFIG_DIR": str(self._config_dir(context))}
        # Long device routines can run minutes; pin the per-tool MCP timeout to 5 min
        # so the agent→bridge hop agrees with the bridge→upstream ceiling.
        env["MCP_TOOL_TIMEOUT"] = "300000"

        model = context.force_model or context.model
        if self.is_fireworks_model(model):
            env.update(self._fireworks_env(model))
        return env
