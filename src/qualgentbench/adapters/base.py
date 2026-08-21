"""Base class for agent adapters and RunContext shared by all adapters."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ..schemas import Condition, TaskConfig

_READ_CHUNK_BYTES = 65536
# Grace between SIGTERM and SIGKILL so the agent can close its MCP transport
# and free the device lock; a hard kill leaves a hold the next episode trips over.
_TERM_GRACE_SEC = 10.0
_DRAIN_SEC = 5.0


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL after the grace period. Safe to call more than once."""
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERM_GRACE_SEC)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()


@dataclass
class RunContext:
    task: TaskConfig
    agent: str
    model: str
    condition: Condition
    trial: int
    run_dir: Path
    mcp_server: str
    mcp_config_path: Path
    workspace_dir: Path

    # Extra env for the agent subprocess, merged over the adapter's own env.
    agent_env: dict[str, str] | None = None

    # Tool-gating override (base names, no server prefix). When set, adapters
    # disable exactly these instead of deriving the list from `condition`.
    disabled_tools: list[str] | None = None

    # When False, don't inject our own MCP server — reuse the connection the agent
    # already has and only gate its tools. A second connection would split the
    # device lock and break apply_routine.
    inject_mcp: bool = True

    # ── MCP-ablation experiment knobs (raw vs mcp condition) ──────────
    # Run with NO MCP servers (empty config, --strict-mcp-config, mcp__device*
    # disallowed as a backstop). Overrides inject_mcp.
    no_mcp: bool = False
    # Give the mcp condition ONLY our MCP server and block unrelated built-ins;
    # otherwise it inherits the whole global agent environment and skews the
    # raw-vs-mcp comparison. Ignored when no_mcp is set.
    isolate_mcp: bool = False
    # Pin the CLI agent to a model. None = the CLI's own default; set for
    # ablations so both conditions use the same model.
    force_model: str | None = None
    # Per-episode tool-call budget, enforced for CLI agents by a PreToolUse hook
    # that denies calls past the cap. None = no cap (timeout_sec still applies).
    tool_call_cap: int | None = None

    # Filled in by the runner after the agent exits
    tool_calls: int = 0
    device_actions: int = 0


class AgentAdapter(ABC):
    """One adapter per coding agent. Adapters are stateless."""

    name: str

    @abstractmethod
    def command(self, instruction: str, context: RunContext) -> list[str]:
        """Return the subprocess command list to launch the agent."""
        ...

    @abstractmethod
    def env(self, context: RunContext) -> dict[str, str]:
        """Return environment variables to set for the agent process."""
        ...

    def prepare(self, context: RunContext) -> None:
        """Optional setup before the subprocess launches."""

    async def run(self, instruction: str, context: RunContext) -> tuple[str, int]:
        """Launch the agent, stream stdout to transcript, enforce timeout.
        Returns (full_transcript, exit_code)."""
        self.prepare(context)
        cmd = self.command(instruction, context)
        env = {**os.environ, **self.env(context), **(context.agent_env or {})}

        transcript_path = context.run_dir / "agent" / "transcript.txt"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        # The budget hook drops this sentinel when the tool-call cap is hit;
        # watching a file keeps the kill agent-agnostic.
        sentinel = context.run_dir / "truncated"
        sentinel.unlink(missing_ok=True)   # per-episode reset
        timeout_flag = context.run_dir / "timed_out"
        timeout_flag.unlink(missing_ok=True)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=str(context.workspace_dir),
        )

        # Stream stdout to disk rather than using communicate(): a timeout cancels
        # communicate() and discards its buffer, so a wall-clock kill lost all output.
        # Chunked reads avoid line-length limits and keep partial transcripts on disk.
        chunks: list[bytes] = []
        handle = transcript_path.open("wb")

        async def _pump() -> None:
            try:
                while True:
                    chunk = await proc.stdout.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        return
                    chunks.append(chunk)
                    handle.write(chunk)
                    handle.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        async def _feed_stdin() -> None:
            try:
                proc.stdin.write(instruction.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass          # agent exited before reading the whole instruction
            finally:
                with suppress(BrokenPipeError, ConnectionResetError):
                    proc.stdin.close()

        async def _stop_on_budget() -> None:
            while True:
                if sentinel.exists():
                    await _terminate(proc)
                    return
                await asyncio.sleep(0.25)

        pump = asyncio.create_task(_pump())
        feeder = asyncio.create_task(_feed_stdin())
        watchdog = asyncio.create_task(_stop_on_budget())

        try:
            try:
                # shield() so the timeout cannot cancel the pump and lose output.
                await asyncio.wait_for(
                    asyncio.shield(pump), timeout=context.task.agent.timeout_sec,
                )
            except asyncio.TimeoutError:
                timeout_flag.write_text("wall-clock timeout\n")
                await _terminate(proc)
                # Drain whatever the agent emitted before it went down.
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(pump, timeout=_DRAIN_SEC)
        finally:
            watchdog.cancel()
            # Terminate rather than kill so the agent can close its MCP transport
            # and the device lock is freed.
            await _terminate(proc)
            for task in (pump, feeder, watchdog):
                task.cancel()
            with suppress(Exception):
                await asyncio.gather(pump, feeder, watchdog, return_exceptions=True)
            handle.close()

        transcript = b"".join(chunks).decode(errors="replace")
        return transcript, proc.returncode or 0
