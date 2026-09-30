"""The segment-end hook: an operator command run on the host after every segment
(QUA-2842).

A segment ends one of three ways: it finished, the credit guard stopped it (exit 75
plus `_runs/<run_id>/stop.json`), or it failed. Private tooling needs to act at that
point — publish the board, push the checkpoint somewhere the next machine can pull it
— and this repo must not know how. So the hook is a generic command, taken only from
the config (`on_segment_end:`) or a flag (`--on-segment-end`), and it is told what
happened through the environment:

    QGB_HOOK_RUN_ID      the run id
    QGB_HOOK_SEGMENT     the run's segment number (plan.json's `segment`)
    QGB_HOOK_RUNS_DIR    the runs dir, absolute
    QGB_HOOK_OUTCOME     complete | stopped:<stop.json reason> | failed:<exit code>
    QGB_HOOK_STOP_JSON   the stop.json path on a credit stop, else empty

Contract, in both places that run it (`qualgent-bench run` and `scripts/launch.py`):

* it runs after board.json and the view are written, and before the launcher sleeps
  or hands off;
* its exit code, a timeout (default 30 min) and its stderr are logged — a hook
  failure never changes the run's exit code or the launcher's next step, and it is
  never retried;
* it is never run from the runs tree (neither its program nor its working directory
  may be inside it: agents write there);
* its environment is the operator's shell plus the five variables above — `run`
  hands it the environment it had BEFORE loading `.env`, so an agent token the
  harness read from a file does not reach it.

Standard library only: `scripts/launch.py` mirrors this module rather than importing
it (it runs on a host with no harness installed), and `tests/test_segment_hook.py`
pins the two copies to the same answers.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: How long a hook may run before it is killed. A checkpoint push of a large run over a
#: slow link is minutes, not hours; a hook still running at 30 min is hung.
DEFAULT_TIMEOUT_SEC = 30 * 60
#: Set by `scripts/launch.py` on the container: the launcher runs the hook on the HOST
#: (where the operator's credentials are), so the harness inside the image must not.
ON_HOST_ENV = "QGB_SEGMENT_HOOK_ON_HOST"
#: `run`'s credit-stop exit code (credit.EXIT_STOPPED), repeated so this module stays
#: importable without the harness — the launcher's copy needs the same number.
EXIT_STOPPED = 75
#: How much of the hook's stderr is logged: the end is where the error is.
STDERR_TAIL_CHARS = 4000
HOOK_ENV_PREFIX = "QGB_HOOK_"


@dataclass
class HookResult:
    ran: bool
    returncode: int | None = None
    timed_out: bool = False
    stderr: str = ""
    problem: str | None = None


def outcome(rc: int, stop: Mapping | None) -> str:
    """`complete`, `stopped:<reason>` or `failed:<exit>`.

    A 75 that left no readable stop.json (or one with no reason) is not a stop the
    launcher can act on either — it reports it as the failure it is.
    """
    if rc == 0:
        return "complete"
    reason = stop.get("reason") if isinstance(stop, Mapping) else None
    if rc == EXIT_STOPPED and isinstance(reason, str) and reason:
        return f"stopped:{reason}"
    return f"failed:{rc}"


def hook_env(base: Mapping[str, str], *, run_id: str, segment: int | None,
             runs_dir: str | os.PathLike, outcome: str,
             stop_json: str | os.PathLike | None) -> dict[str, str]:
    """The operator's environment plus the five QGB_HOOK_* variables. Any QGB_HOOK_*
    the base already carries is dropped first, so a stale value can never pose as
    this segment's."""
    env = {k: v for k, v in base.items() if not k.startswith(HOOK_ENV_PREFIX)}
    env.update({
        "QGB_HOOK_RUN_ID": run_id,
        "QGB_HOOK_SEGMENT": "" if segment is None else str(segment),
        "QGB_HOOK_RUNS_DIR": str(Path(runs_dir).expanduser().resolve()),
        "QGB_HOOK_OUTCOME": outcome,
        "QGB_HOOK_STOP_JSON": str(stop_json) if stop_json else "",
    })
    return env


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def problems(command: str, runs_dir: str | os.PathLike,
             cwd: str | os.PathLike | None = None) -> list[str]:
    """Why this hook must not run. Empty = it may.

    Agents write inside the runs tree, so a program there — or a relative program
    resolved from a working directory there — would be a file an agent could have
    written, run with the operator's credentials.
    """
    runs = Path(runs_dir)
    here = Path(cwd) if cwd is not None else Path.cwd()
    try:
        words = shlex.split(command)
    except ValueError as exc:
        return [f"the command does not parse ({exc})"]
    if not words:
        return ["the command is empty"]
    found: list[str] = []
    if _inside(here, runs):
        found.append(f"it would run from {here.resolve()}, inside the runs tree "
                     f"{runs.expanduser().resolve()}")
    program = words[0]
    if "/" in program or (os.sep != "/" and os.sep in program):
        resolved: Path | None = here / Path(program).expanduser()
    else:
        which = shutil.which(program)
        resolved = Path(which) if which else None
    if resolved is not None and _inside(resolved, runs):
        found.append(f"its program {resolved.resolve()} is inside the runs tree "
                     f"{runs.expanduser().resolve()}")
    return found


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run(command: str, *, env: Mapping[str, str], runs_dir: str | os.PathLike,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        log: Callable[[str], None] = print,
        cwd: str | os.PathLike | None = None,
        reraise_interrupt: bool = False) -> HookResult:
    """Run the hook once, through the shell (so `$QGB_HOOK_RUNS_DIR` expands), and
    log how it went. Never raises: nothing it does may change the caller's exit code
    or next step. stdout passes through; stderr is captured and its tail logged
    (success included — it is where a tool says what it did).

    Ctrl+C kills the hook (and its process group). With `reraise_interrupt` it then
    raises KeyboardInterrupt: the launcher passes it, because a loop that went on to
    tear down and sleep for hours after the operator pressed Ctrl+C would be ignoring
    them (QUA-2847). `run` does not: the hook is its last step, and its exit code
    stays the segment's."""
    if blocked := problems(command, runs_dir, cwd):
        msg = "; ".join(blocked)
        log(f"segment-end hook NOT run: {msg}")
        return HookResult(ran=False, problem=msg)
    log(f"segment-end hook ({env.get('QGB_HOOK_OUTCOME', '?')}): {command}")
    started = time.monotonic()
    kwargs: dict = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(command, shell=True, env=dict(env), cwd=cwd,
                                stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, errors="replace", **kwargs)
    except OSError as exc:
        log(f"segment-end hook could not start: {exc}")
        return HookResult(ran=False, problem=str(exc))
    timed_out = interrupted = False
    try:
        _, err = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
    except KeyboardInterrupt:
        interrupted = True
    if timed_out or interrupted:
        # The whole process group: a shell hook's children (rsync, aws, …) would
        # otherwise outlive it and hold the stderr pipe open.
        _kill(proc)
        try:
            _, err = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            err = ""
    took = time.monotonic() - started
    tail = (err or "")[-STDERR_TAIL_CHARS:].rstrip()
    rc = proc.returncode
    if timed_out:
        log(f"segment-end hook timed out after {timeout_sec:g}s and was killed — the "
            f"run's exit code and next step are unchanged")
    elif interrupted:
        log("segment-end hook interrupted and killed — the run's exit code and next "
            "step are unchanged")
    elif rc != 0:
        log(f"segment-end hook exited {rc} after {took:.0f}s — the run's exit code and "
            f"next step are unchanged")
    else:
        log(f"segment-end hook done in {took:.0f}s")
    if tail:
        for line in tail.splitlines():
            log(f"  hook stderr: {line}")
    if interrupted and reraise_interrupt:
        raise KeyboardInterrupt
    return HookResult(ran=True, returncode=rc, timed_out=timed_out, stderr=tail)
