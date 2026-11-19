"""Codex CLI adapter - invokes `codex exec` with per-run isolated state."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .base import AgentAdapter, AuthRefused, RunContext
from .bedrock_tool_shim import BedrockToolShim, bedrock_runtime_origin
from .. import providers
from ..interactions import BUDGET_HOOK
from ..transcript import CLI_VERSION, codex_state_usage_line

logger = logging.getLogger(__name__)

#: `RunContext.auth_mode` values (`provenance.agent_auth`).
AUTH_API_KEY, AUTH_ACCOUNT_LOGIN, AUTH_NONE = "api_key", "account_login", "none"
#: A Bedrock episode (QUA-3502): authenticated by the Bedrock API key, never by an
#: OpenAI key or a ChatGPT login.
AUTH_BEDROCK = "bedrock_api_key"

# OpenAI credentials a Bedrock episode must not carry: it talks to AWS only.
_OPENAI_CREDENTIAL = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"})

#: The opt-in for running codex-cli on the operator's own `codex login` (QUA-2868).
#: Without it an episode that has no API key is REFUSED before the agent launches: a
#: ChatGPT-account login bills (and rate-limits) that workspace, not an API key, and
#: QUA-2850's worktrees with no `.env` ran paid grades on one until it ran out of
#: credits. `qualgent-bench run --allow-codex-login`, `allow_codex_login: true` in a
#: bench config, the grader's `run --allow-codex-login` and `run_create_ab.py
#: --allow-codex-login` all set it; recorded as `provenance.allow_codex_login`.
ALLOW_LOGIN_ENV = "QGB_ALLOW_CODEX_LOGIN"
#: Set to 0 to run codex × Bedrock straight to AWS, without the tool-namespace relay
#: (`bedrock_tool_shim`, QUA-3535). Kimi K3's MCP calls then fail as `unsupported call`.
BEDROCK_SHIM_ENV = "QGB_CODEX_BEDROCK_TOOL_SHIM"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def login_allowed() -> bool:
    """Whether this process may run codex-cli on an account login (QGB_ALLOW_CODEX_LOGIN)."""
    return os.environ.get(ALLOW_LOGIN_ENV, "").strip().lower() in _TRUTHY


def allow_login() -> None:
    """Opt this process (and the subprocesses it starts) in to the account login."""
    os.environ[ALLOW_LOGIN_ENV] = "1"


def login_refusal(source_home: Path) -> str:
    """The refusal, phrased so the operator knows exactly how to opt in."""
    return (f"codex-cli has no API key (CODEX_API_KEY / OPENAI_API_KEY, e.g. in the `.env` "
            f"in the cwd) and would run on the account login in {source_home}, which bills "
            "and rate-limits that ChatGPT workspace, not an API key. Refused by default "
            "since QUA-2868: set CODEX_API_KEY or OPENAI_API_KEY, or opt in to the login "
            f"with {ALLOW_LOGIN_ENV}=1 (`qualgent-bench run --allow-codex-login`, "
            "`allow_codex_login: true` in the bench config, `python -m "
            "qualgentbench.create.grader run --allow-codex-login`, `run_create_ab.py run "
            "--allow-codex-login`); the episodes then record "
            "provenance.agent_auth=account_login")


class CodexAuthRefused(AuthRefused):
    """codex-cli would run on the operator's account login without the opt-in."""


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
    #: The running tool-namespace relay per episode (keyed by run dir), QUA-3535.
    _SHIMS: dict[str, BedrockToolShim] = {}
    _AUTH_FILES = ("auth.json",)

    @staticmethod
    def _codex_home(context: RunContext) -> Path:
        return context.run_dir / "codex_home"

    @classmethod
    def _home_dir(cls, context: RunContext) -> Path:
        return cls._codex_home(context) / "home"

    @classmethod
    def provider_for(cls, model: str | None, provider: str | None = None) -> str:
        """The provider an episode on ``model`` runs against (`providers.resolve`)."""
        return providers.resolve(cls.name, model, provider) or providers.OPENAI

    @classmethod
    def _provider(cls, context: RunContext) -> str:
        return cls.provider_for(context.force_model or context.model, context.provider)

    def prepare(self, context: RunContext) -> None:
        codex_home = self._codex_home(context)
        codex_home.mkdir(parents=True, exist_ok=True)
        self._home_dir(context).mkdir(parents=True, exist_ok=True)
        route = self._provider(context)
        if refusal := providers.pair_refusal(
                self.name, route, context.force_model or context.model):
            raise providers.ProviderError(refusal)
        if route == providers.BEDROCK:
            # No OpenAI key exchange and no login copy: nothing of the operator's
            # OpenAI identity is written into this episode's CODEX_HOME.
            context.auth_mode = AUTH_BEDROCK
            context.auth_login_allowed = None
            self._start_bedrock_shim(context)
        else:
            # Raises CodexAuthRefused (before anything is copied or launched) when the
            # only credential is the operator's account login and the run did not opt in.
            context.auth_mode = self._seed_account_auth(codex_home)
            context.auth_login_allowed = login_allowed()
        (codex_home / "config.toml").write_text(self._config_toml(context))
        (codex_home / "hooks.json").write_text(
            json.dumps(self._hooks_config(context), indent=2) + "\n"
        )
        context.agent_cli_version = self.cli_version(context)

    #: `codex --version` per run (keyed by the run's meta dir): one subprocess for a
    #: whole board, not one per episode. `codex exec --json` names no version, and the
    #: rollout's `cli_version` is not on stdout, so this is the only cheap source.
    _VERSION_CACHE: dict[str, str | None] = {}

    @classmethod
    def cli_version(cls, context: RunContext) -> str | None:
        """The `codex` binary's version (`codex-cli 0.156.1` → `0.156.1`), or None
        when it cannot be run or says nothing version-shaped. Cached per run."""
        key = str(context.run_meta_dir or "")
        if key not in cls._VERSION_CACHE:
            cls._VERSION_CACHE[key] = cls._probe_version()
        return cls._VERSION_CACHE[key]

    @staticmethod
    def _probe_version() -> str | None:
        try:
            proc = subprocess.run(["codex", "--version"], capture_output=True, text=True,
                                  timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        for token in proc.stdout.split():
            if CLI_VERSION.fullmatch(token):
                return token
        return None

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

    @staticmethod
    def bedrock_shim_enabled() -> bool:
        return os.environ.get(BEDROCK_SHIM_ENV, "1").strip().lower() not in {
            "0", "false", "no", "off"}

    def _start_bedrock_shim(self, context: RunContext) -> None:
        """Start this episode's tool-namespace relay (`bedrock_tool_shim`) in front of
        Bedrock: codex sends MCP tools in a namespace and a model that answers with the
        bare name (Kimi K3) is put back in it. A no-op for a model that names it."""
        self._stop_bedrock_shim(context)
        region = os.environ.get(providers.BEDROCK_REGION_ENV, "").strip()
        if not self.bedrock_shim_enabled() or not region:
            return    # no region: `env()` refuses the episode by name
        shim = BedrockToolShim(bedrock_runtime_origin(region))
        shim.start()
        self._SHIMS[str(context.run_dir)] = shim

    def _stop_bedrock_shim(self, context: RunContext) -> None:
        shim = self._SHIMS.pop(str(context.run_dir), None)
        if shim is None:
            return
        shim.stop()
        record = {"requests": shim.requests, "namespaces_added": shim.rewrites}
        logger.info("codex-cli bedrock tool shim: %s", record)
        path = context.run_dir / "agent" / "bedrock_tool_shim.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record) + "\n")
        except OSError:
            pass

    def cleanup(self, context: RunContext) -> None:
        self._stop_bedrock_shim(context)
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
        if self._provider(context) == providers.BEDROCK:
            # The only AWS variables the agent receives (`inherited_env` stripped the
            # rest): the Bedrock API key and the region its inference profile is in.
            env.update(providers.bedrock_env())
        elif os.environ.get("CODEX_API_KEY"):
            env["CODEX_API_KEY"] = os.environ["CODEX_API_KEY"]
        return env

    def inherited_env(self, context: RunContext) -> dict[str, str]:
        """This process's environment less every cloud-account variable (the host's
        AWS profile, keys, session token, region: `providers.strip_cloud_env`), and on
        Bedrock less the OpenAI credentials too, so the agent holds only what its
        provider needs."""
        env = providers.strip_cloud_env(super().inherited_env(context))
        if self._provider(context) == providers.BEDROCK:
            env = {k: v for k, v in env.items() if k not in _OPENAI_CREDENTIAL}
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
    def configured_auth_mode(cls) -> str:
        """What `prepare` will authenticate with, read without side effects (no login,
        no copy): `api_key` when CODEX_API_KEY / OPENAI_API_KEY is set, else
        `account_login` when the operator's codex home holds a login, else `none`."""
        if (os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip():
            return AUTH_API_KEY
        source_home = cls._source_codex_home()
        if any((source_home / f).is_file() for f in cls._AUTH_FILES):
            return AUTH_ACCOUNT_LOGIN
        return AUTH_NONE

    def auth_refusal(self, provider: str | None = None) -> str | None:
        """Why an episode must not launch on the credentials `prepare` would use now —
        the operator's account login without QGB_ALLOW_CODEX_LOGIN — else None. Read
        without side effects, so a CLI or `run_episode` can refuse before the device or
        the agent is touched. A Bedrock episode never uses the login, so it is never
        refused for it (its own credentials are `providers.problems`' business)."""
        if self.provider_for(None, provider) == providers.BEDROCK:
            return None
        if self.configured_auth_mode() == AUTH_ACCOUNT_LOGIN and not login_allowed():
            return login_refusal(self._source_codex_home())
        return None

    @classmethod
    def _seed_account_auth(cls, codex_home: Path) -> str:
        """Seed this episode's CODEX_HOME and return the auth mode it ended up with.
        A configured API key wins; otherwise the operator's own login is COPIED in —
        only with QGB_ALLOW_CODEX_LOGIN, and loudly, because a ChatGPT-account login
        silently moves an eval's spend (and its credit limit) onto that workspace
        (QUA-2850 review: a worktree with no `.env` ran a paid grade on a ChatGPT
        workspace until it ran out of credits). Without the opt-in it raises
        CodexAuthRefused, before the login is copied (QUA-2868). That includes a key
        that is set but whose `codex login --with-api-key` failed."""
        if cls._seed_api_key_auth(codex_home):
            return AUTH_API_KEY

        source_home = cls._source_codex_home()
        if (not login_allowed()
                and any((source_home / f).is_file() for f in cls._AUTH_FILES)):
            raise CodexAuthRefused(login_refusal(source_home))
        mode = AUTH_NONE
        for filename in cls._AUTH_FILES:
            source = source_home / filename
            if not source.is_file():
                continue
            mode = AUTH_ACCOUNT_LOGIN
            destination = codex_home / filename
            try:
                if source.resolve() == destination.resolve():
                    continue
            except OSError:
                pass
            shutil.copy2(source, destination)
        if mode == AUTH_ACCOUNT_LOGIN:
            logger.warning(
                "codex-cli: no usable CODEX_API_KEY / OPENAI_API_KEY (unset, or `codex "
                "login --with-api-key` failed) — this episode runs on the account login "
                "copied from %s (a ChatGPT login bills and rate-limits that workspace, "
                "not the API key). Opted in with %s; recorded as "
                "provenance.agent_auth=%s.",
                source_home, ALLOW_LOGIN_ENV, AUTH_ACCOUNT_LOGIN)
        return mode

    def _config_toml(self, context: RunContext) -> str:
        lines = [
            f"model = {self._toml_value(context.force_model or context.model)}",
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
        ]
        if self._provider(context) == providers.BEDROCK:
            # codex's built-in Bedrock-runtime provider (the OpenAI-compatible route);
            # it reads AWS_BEARER_TOKEN_BEDROCK and AWS_REGION. `amazon-bedrock` (the
            # Mantle route) 404s for these models (live check, 2026-11-17).
            lines.append(f"model_provider = {self._toml_value(providers.CODEX_BEDROCK_PROVIDER)}")
        if context.developer_instructions:
            # How the desktop app installs a subagent template for Codex (the body as
            # `developer_instructions`); written only into this episode's CODEX_HOME.
            lines.append("developer_instructions = "
                         f"{self._toml_value(context.developer_instructions)}")

        if shim := self._SHIMS.get(str(context.run_dir)):
            # The episode's tool-namespace relay (QUA-3535). The built-in provider
            # allows a `base_url` override and keeps its own bearer-token auth. A
            # table, so it follows every top-level key.
            lines += ["", f"[model_providers.{self._toml_key(providers.CODEX_BEDROCK_PROVIDER)}]",
                      f"base_url = {self._toml_value(shim.base_url)}"]

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
            # QGB_DISALLOWED_TOOLS shapes the device surface. A server entry that sets
            # `apply_disallowed_tools: false` is exempt — a creation episode's
            # QualGent-MCP (QUA-2856, spike P5), whose surface is the arm's own tool
            # policy (`enabled_tools`), which the withheld list must not touch.
            if (entry.get("apply_disallowed_tools", True)
                    and (disabled_tools := self._disabled_tools(context))):
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
