"""Codex CLI adapter - invokes `codex exec` with per-run isolated state."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import AgentAdapter, RunContext
from ..interactions import BUDGET_HOOK
from ..transcript import codex_state_usage_line


class CodexCliAdapter(AgentAdapter):
    name = "codex-cli"
    _MCP_TOOL_TIMEOUT_SEC = 300
    _DISABLED_GLOBAL_FEATURES = (
        "apps",
        "apps_mcp_path_override",
        "memories",
        "plugins",
        "plugin_hooks",
        "skill_mcp_dependency_install",
    )
    _AUTH_HOME_ENV = "QUALGENT_BENCH_CODEX_HOME"
    _AUTH_FILES = ("auth.json",)

    @staticmethod
    def _codex_home(context: RunContext) -> Path:
        return context.run_dir / "codex_home"

    @classmethod
    def _home_dir(cls, context: RunContext) -> Path:
        return cls._codex_home(context) / "home"

    def prepare(self, context: RunContext) -> None:
        codex_home = self._codex_home(context)
        codex_home.mkdir(parents=True, exist_ok=True)
        self._home_dir(context).mkdir(parents=True, exist_ok=True)
        self._seed_account_auth(codex_home)
        (codex_home / "config.toml").write_text(self._config_toml(context))
        (codex_home / "hooks.json").write_text(
            json.dumps(self._hooks_config(context), indent=2) + "\n"
        )

    async def run(self, instruction: str, context: RunContext) -> tuple[str, int]:
        try:
            transcript, exit_code = await super().run(instruction, context)
            return self.with_state_usage(context, transcript), exit_code
        finally:
            self.cleanup(context)

    def with_state_usage(self, context: RunContext, transcript: str) -> str:
        """Append Codex's own token total when stdout never reported usage (QUA-2803).

        `codex exec` writes ONE `turn.completed`, at the end of the episode, and a
        budget-truncated (or otherwise killed) episode never gets there. The session
        rollout under this episode's CODEX_HOME has the running total, so it is
        appended as a `qgb.codex_state_usage` line: to the returned transcript, which
        is what every scorer reads, and to `agent/transcript.txt`, which is what a
        rescore reads. Nothing is appended when a `turn.completed` exists.
        """
        line = codex_state_usage_line(self._codex_home(context), transcript)
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

    def cleanup(self, context: RunContext) -> None:
        codex_home = self._codex_home(context)
        if not codex_home.exists():
            return
        for filename in self._AUTH_FILES:
            for path in codex_home.rglob(filename):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)

    def command(self, instruction: str, context: RunContext) -> list[str]:
        cmd = [
            "codex",
            "--ask-for-approval", "never",
            "--sandbox", "danger-full-access",
        ]
        if context.tool_call_cap:
            cmd.append("--dangerously-bypass-hook-trust")
        for feature in self._DISABLED_GLOBAL_FEATURES:
            cmd += ["--disable", feature]
        model = context.force_model or context.model
        if model:
            cmd += ["--model", model]
        # No `--ephemeral`: the session rollout is what `with_state_usage` reads when
        # the episode is killed before its one `turn.completed` (QUA-2803). It lives
        # in this episode's own CODEX_HOME, so no other episode can see it.
        cmd += [
            "exec",
            "--json",
            "--cd", str(context.workspace_dir),
            "--skip-git-repo-check",
        ]
        return cmd

    def env(self, context: RunContext) -> dict[str, str]:
        codex_home = self._codex_home(context)
        home_dir = self._home_dir(context)
        env = {
            "CODEX_HOME": str(codex_home),
            # Keep HOME/XDG run-local — Codex discovers user skills from HOME, and
            # the benchmark must not inherit the operator's personal setup.
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(codex_home / "xdg_config"),
            "XDG_CACHE_HOME": str(codex_home / "xdg_cache"),
            "XDG_DATA_HOME": str(codex_home / "xdg_data"),
        }
        if os.environ.get("CODEX_API_KEY"):
            env["CODEX_API_KEY"] = os.environ["CODEX_API_KEY"]
        return env

    @classmethod
    def _source_codex_home(cls) -> Path:
        if override := os.environ.get(cls._AUTH_HOME_ENV):
            return Path(override).expanduser()
        if codex_home := os.environ.get("CODEX_HOME"):
            return Path(codex_home).expanduser()
        return Path.home() / ".codex"

    @classmethod
    def _seed_api_key_auth(cls, codex_home: Path) -> bool:
        """Exchange a configured API key for auth.json via `codex login --with-api-key`
        (Codex ignores OPENAI_API_KEY in the env). The key goes over stdin so it never
        shows in argv; cleanup() deletes the auth.json so the key never persists."""
        key = (os.environ.get("CODEX_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            return False
        try:
            proc = subprocess.run(
                ["codex", "login", "--with-api-key"],
                input=key, text=True, capture_output=True, timeout=60,
                env={**os.environ, "CODEX_HOME": str(codex_home),
                     "HOME": str(cls._home_dir_for(codex_home))},
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and (codex_home / "auth.json").is_file()

    @staticmethod
    def _home_dir_for(codex_home: Path) -> Path:
        return codex_home / "home"

    @classmethod
    def _seed_account_auth(cls, codex_home: Path) -> None:
        if cls._seed_api_key_auth(codex_home):
            return

        source_home = cls._source_codex_home()
        for filename in cls._AUTH_FILES:
            source = source_home / filename
            if not source.is_file():
                continue
            destination = codex_home / filename
            try:
                if source.resolve() == destination.resolve():
                    continue
            except OSError:
                pass
            shutil.copy2(source, destination)

    def _config_toml(self, context: RunContext) -> str:
        lines = [
            f"model = {self._toml_value(context.force_model or context.model)}",
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
        ]

        for name, entry in self._mcp_servers(context).items():
            lines += ["", f"[mcp_servers.{self._toml_key(name)}]"]
            if url := entry.get("url"):
                lines.append(f"url = {self._toml_value(url)}")
            if command := entry.get("command"):
                lines.append(f"command = {self._toml_value(command)}")
            if args := entry.get("args"):
                lines.append(f"args = {self._toml_value(args)}")
            # `env` only applies to stdio servers; Codex hard-errors on it for
            # url-based (streamable_http) ones.
            if (env := entry.get("env")) and not entry.get("url"):
                lines.append(f"env = {self._toml_value(env)}")
            lines.append(f"required = {self._toml_value(entry.get('required', True))}")
            lines.append(f"tool_timeout_sec = {self._MCP_TOOL_TIMEOUT_SEC}")
            if disabled_tools := self._disabled_tools(context):
                lines.append(f"disabled_tools = {self._toml_value(disabled_tools)}")
            if enabled_tools := entry.get("enabled_tools"):
                lines.append(f"enabled_tools = {self._toml_value(enabled_tools)}")

        # Codex only runs PreToolUse hooks declared in config.toml, so the budget
        # hook must be emitted here; --dangerously-bypass-hook-trust lets it run
        # without an interactive trust prompt.
        if context.tool_call_cap:
            script = self._write_tool_cap_script(context)
            lines += [
                "",
                "[[hooks.PreToolUse]]",
                'matcher = "*"',
                "",
                "[[hooks.PreToolUse.hooks]]",
                'type = "command"',
                f"command = {self._toml_value(str(script))}",
            ]

        return "\n".join(lines) + "\n"

    def _mcp_servers(self, context: RunContext) -> dict[str, dict[str, Any]]:
        if context.no_mcp or not context.mcp_config_path.exists():
            return {}
        raw = json.loads(context.mcp_config_path.read_text())
        servers = raw.get("mcpServers") or raw.get("mcp_servers") or {}
        return {
            str(name): entry
            for name, entry in servers.items()
            if isinstance(entry, dict)
        }

    @staticmethod
    def _disabled_tools(context: RunContext) -> list[str]:
        return list(dict.fromkeys(context.disabled_tools or []))

    def _write_tool_cap_script(self, context: RunContext) -> Path:
        """Write the tool-call-budget hook script and reset its counter. Idempotent."""
        hooks_dir = self._codex_home(context) / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        count_file = hooks_dir / "count"
        count_file.write_text("0")
        # ONE shared hook across adapters — per-adapter counters meant
        # incompatible step definitions and a meaningless cross-agent board.
        script = hooks_dir / "tool_cap.py"
        script.write_text(BUDGET_HOOK.format(
            count_file=count_file,
            meter_file=context.run_dir / "interactions.json",
            cap=int(context.tool_call_cap),
            sentinel=context.run_dir / "truncated",
        ))
        script.chmod(0o755)
        return script

    def _hooks_config(self, context: RunContext) -> dict[str, Any]:
        # Codex loads hooks from BOTH config.toml and hooks.json; registering the
        # cap hook in both counted every call twice and halved the budget. Keep
        # this empty — config.toml is the single source.
        return {"hooks": {}}

    @classmethod
    def _toml_key(cls, key: str) -> str:
        if key.replace("_", "").replace("-", "").isalnum() and "-" not in key:
            return key
        return cls._toml_value(key)

    @classmethod
    def _toml_value(cls, value: Any) -> str:
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(cls._toml_value(v) for v in value) + "]"
        if isinstance(value, dict):
            items = ", ".join(
                f"{cls._toml_key(str(k))} = {cls._toml_value(v)}"
                for k, v in value.items()
            )
            return "{ " + items + " }"
        raise TypeError(f"Unsupported TOML value: {value!r}")
