"""Base class for agent adapters and RunContext shared by all adapters."""

from __future__ import annotations

import asyncio
import os
import signal
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


async def _poll_exit(proc: asyncio.subprocess.Process) -> None:
    """Return once the process has EXITED. Process.wait() cannot be used for this:
    its future resolves only after every pipe also disconnects, so a child the
    agent backgrounded (adb root, logcat) stalls it by holding stdout — long after
    returncode is set. returncode itself is set the moment the child is reaped."""
    while proc.returncode is None:
        await asyncio.sleep(0.25)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, then SIGKILL after the grace period. Safe to call more than once."""
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(_poll_exit(proc), timeout=_TERM_GRACE_SEC)
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(TimeoutError):
            await asyncio.wait_for(_poll_exit(proc), timeout=_TERM_GRACE_SEC)


def _agent_user() -> str | None:
    """The unprivileged user agent subprocesses run as (`QGB_AGENT_USER`, set in
    the image, where /app is root-only so the agent cannot read harness code or
    specs). Active only when the drop is actually possible — POSIX, running as
    root, and the user exists; on a developer's own machine it is inert."""
    name = os.environ.get("QGB_AGENT_USER")
    if not name or os.name != "posix" or os.geteuid() != 0:
        return None
    import pwd
    try:
        pwd.getpwnam(name)
    except KeyError:
        return None
    return name


def _hand_over(path: Path, user: str) -> None:
    """chown an episode-owned tree to the agent user: it must write its
    workspace, config home and hook counter there, while owning nothing else."""
    import pwd
    rec = pwd.getpwnam(user)
    top = Path(path)
    if not top.exists():
        return
    for p in [top, *top.rglob("*")]:
        with suppress(OSError):
            os.lchown(p, rec.pw_uid, rec.pw_gid)


def _kill_orphans(proc: asyncio.subprocess.Process) -> None:
    """The agent runs as its own session leader; SIGKILL its whole process group
    once it is gone. A child it backgrounded (adb root, logcat) otherwise survives,
    keeps the device busy, and holds the stdout pipe open."""
    if os.name != "posix":
        return
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


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
        overrides = {**self.env(context), **(context.agent_env or {})}
        env = {**os.environ, **overrides}

        spawn_user = _agent_user()
        spawn_kwargs: dict = {}
        if spawn_user is not None:
            _hand_over(context.run_dir, spawn_user)
            _hand_over(context.workspace_dir, spawn_user)
            spawn_kwargs = {"user": spawn_user, "group": spawn_user,
                            "extra_groups": []}
            if "HOME" not in overrides:
                # The dropped user cannot read root's HOME; give the CLIs a
                # writable one inside the episode.
                env["HOME"] = str(context.run_dir)

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
            # Own session, so _kill_orphans can sweep everything it spawned.
            start_new_session=(os.name == "posix"),
            **spawn_kwargs,
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
        waiter = asyncio.create_task(_poll_exit(proc))

        try:
            # EOF alone cannot be the finish line: a process the agent backgrounded
            # (adb root, logcat) inherits stdout and holds the pipe open after the
            # agent dies — the lane would then sit idle until the wall clock fired.
            done, _ = await asyncio.wait(
                {pump, waiter}, timeout=context.task.agent.timeout_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timeout_flag.write_text("wall-clock timeout\n")
                await _terminate(proc)
            if pump not in done:
                # Drain whatever the agent emitted; the pipe may never close.
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pump), timeout=_DRAIN_SEC)
        finally:
            watchdog.cancel()
            # Terminate rather than kill so the agent can close its MCP transport
            # and the device lock is freed; then sweep what it left behind.
            await _terminate(proc)
            _kill_orphans(proc)
            for task in (pump, feeder, watchdog, waiter):
                task.cancel()
            with suppress(Exception):
                await asyncio.gather(pump, feeder, watchdog, waiter,
                                     return_exceptions=True)
            handle.close()

        transcript = b"".join(chunks).decode(errors="replace")
        return transcript, proc.returncode or 0
