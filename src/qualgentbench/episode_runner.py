"""The seeded-bug engine: runs one episode end to end — stage the device, install
the seeded APK, write bug flags, launch the agent under a step budget, collect the
transcript and evidence. `cli.py` scores what comes back."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import brief as _brief
from . import pricing, submission
from .adapters import get_adapter
from .adb_meter import AdbMeter
from .checkpoint import image_digest, run_meta_dir, server_stamp, write_blinded_marker
from .config import allow_runs_in_repo, runs_dir_problems
from .credit import RATE_LIMITED_SENTINEL
from .interactions import InteractionLog
from .mcp_meter import McpMeter
from .replay import crash_window
from .replay import snapshot as replay_snapshot
from .replay import snapshot_shared
# The ONE rotation reset, shared with replay._reset. Imported rather than
# reimplemented so the auto-rotate-off-first ordering cannot drift between the live
# and the replay staging path (QUA-2709 documents what an inverted order costs).
from .replay import _set_rotation
# ...and the ONE re-pin after the launch, shared with the route's `launch` step for
# the same reason: here the load-bearing order is "app in front, THEN pin" (QUA-2734).
from .replay import repin_portrait_after_launch
from .verify.device import relaunch as _relaunch_app, wait_stable
from .verify.device import dump_stats, reset_dump_source, stop_u2_server
from .adapters.base import RunContext
from .task import BenchmarkTask
from .episode_evidence import write_episode_evidence
from .frame_capture import FrameCapture
from .result import RunResult, VerifierResult
from .schemas import Condition
from .contamination import devloop_default_roots
from .session import (DEVLOOP_SERVER_NAME, NO_SOURCE, DeviceSession, fetch_episode_isolation,
                      fetch_server_identity, identity_changes)
from .transcript import TranscriptParser


def run_dir_name(task_id: str, agent: str, model: str, condition: str, trial: int,
                 episode_id: str = "") -> str:
    """Canonical run-directory name, shared by every mode so run dirs sort uniformly.
    `task_id` must be the AGENT-VISIBLE id (`agent_visible_task_id`): the name is part
    of the agent's cwd. `episode_id` (QUA-2806) keeps two arms of one case, started in
    the same second on two lanes, from sharing a directory."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    model_short = model.split("/")[-1]  # strip org prefix if present
    tail = f"_{episode_id}" if episode_id else ""
    return f"{ts}_{task_id}_{agent}_{model_short}_{condition}_trial-{trial}{tail}"


# ── the arm is blind (QUA-2806) ───────────────────────────────────────────────
#
# A journey task id is `<case>~seeded` or `<case>~clean`, and it used to name the
# episode's directory twice (`<runs>/<case>~seeded/<ts>_<case>~seeded_…/workspace`) —
# the agent's cwd, echoed in every claude-code Bash result, in codex's `--cd`, in
# CLAUDE_CONFIG_DIR / CODEX_HOME / HOME, in the budget hook's paths and in the
# findings file's path. An agent could read which arm it was on. Everything the agent
# can see or reach now carries the CASE and an opaque random episode id only; which
# version that id is lives harness-side (result.json after the agent exits, the run's
# meta dir — `checkpoint.write_blinded_marker` — while it runs). A saved episode keeps
# whatever layout it was written with: every reader goes through result.json.


def agent_visible_task_id(task_id: str) -> str:
    """`task_id` with a journey version label removed (`case~seeded` → `case`); any
    other id unchanged. The one place that decides what an agent may see of a task
    id."""
    from .journey import VERSIONS

    if "~" in task_id:
        case, version = task_id.rsplit("~", 1)
        if version in VERSIONS:
            return case
    return task_id


def new_episode_id() -> str:
    """Opaque and random: it must say nothing about the arm, the case or the order."""
    return "ep-" + secrets.token_hex(6)


def clock_tolerance_s() -> int:
    """How far the device clock may be from the pin when an episode starts and when it
    is handed to the agent (`QGB_CLOCK_TOLERANCE_S`, default `CLOCK_TOLERANCE_S`).
    Read per call, like the pin. A non-positive or unparsable value is refused loudly
    rather than silently meaning "no check"."""
    raw = (os.environ.get("QGB_CLOCK_TOLERANCE_S") or "").strip()
    if not raw:
        return CLOCK_TOLERANCE_S
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"QGB_CLOCK_TOLERANCE_S={raw!r}: expected whole seconds") from None
    if value <= 0:
        raise ValueError(f"QGB_CLOCK_TOLERANCE_S={raw!r}: must be positive")
    return value


_run_dir_name = run_dir_name  # back-compat alias


def _generate_mcp_config(mcp_server: str) -> dict:
    """MCP config pointing the agent at its device surface. Deliberately credential-free:
    the run dir is meant to be shared, and an ``env`` block on an http server is inert
    anyway. Credentials reach the agent through its process environment (see adapters)."""
    return {"mcpServers": {"device": {
        "type": "http",
        "url": f"{mcp_server.rstrip('/')}/mcp",
    }}}


logger = logging.getLogger(__name__)

@dataclass
class EpisodeOptions:
    agent: str
    model: str
    condition: Condition
    trial: int
    mcp_server: str
    runs_dir: Path
    # Optional scoring override; None uses the PASS/FAIL verdict.
    # Signature: (transcript, model, task) -> VerifierResult.
    verdict_fn: object | None = None
    # Task type recorded in result.json: "bug_hunt" | "bug_task" | "clean_task".
    task_type: str = "episode"
    # Pin this trial to a specific serial (parallel runs need one device per worker).
    # None picks the first available device.
    device_serial: str | None = None
    # When set, every trial gets a fresh uninstall + install so trial N is scored
    # against the same package trial 1 was. None = pm clear only (legacy).
    apk_path: Path | None = None

    # ── MCP-ablation experiment (claude-code with vs without MCP) ────
    # Arm label recorded on the episode: "mcp" when a server is configured, else "raw".
    tooling: str | None = None   # derived; see arm()
    # Buggy source checkout copied into the workspace — identical in both conditions.
    source_dir: Path | None = None
    # Model forwarded to the adapter; None = the CLI's own default model.
    force_model: str | None = None

    # Overrides the recorded run condition (run dir + result.json `condition`).
    condition_label: str | None = None

    # Called with the run dir before the agent starts; the CLI polls the budget
    # counter for live progress. Display only — never affects the run.
    on_run_dir: object | None = None

    # ── Provenance (recorded, never used for scoring) ────────────────────
    # One `run` invocation; "" for a bare run_episode call.
    run_id: str = ""
    # Which parallel lane ran it, out of how many. 0/1 = sequential.
    lane: int = 0
    lanes: int = 1
    # 1 for a first attempt; >1 when the scheduler requeued the unit.
    attempt: int = 1
    # Which app the unit belongs to. The task id alone does not name it, and the
    # episode marker is keyed on the unit, not the task.
    app_id: str = ""
    # 0 for the first sitting of a run; a resume increments it, so a blended board
    # still says which machine and which sitting produced each episode.
    segment: int = 0
    # The MCP server's identity as `run` read it before writing the plan
    # (`session.fetch_server_identity`, QUA-2806). Each episode re-reads the server
    # and refuses to start on a different one. None = nothing to compare against (a
    # bare run_episode call, or the raw arm).
    mcp_server_identity: dict | None = None


# Tools withheld from the agent, from QGB_DISALLOWED_TOOLS (comma-separated).
def _disabled_tools() -> list[str]:
    raw = os.environ.get("QGB_DISALLOWED_TOOLS") or ""
    return list(dict.fromkeys(t.strip() for t in raw.split(",") if t.strip()))



def _build_instruction(task: BenchmarkTask, device_serial: str, condition: Condition) -> str:
    return f"""You are a QA engineer. Use the MCP tools to complete the task below.

## Device & App
App `{task.bundle_id}` is installed and running on device `{device_serial}`.
Call `mobile_observe_screen` with device="{device_serial}" to see the current state and begin.
Every device tool takes the device as its first argument — always pass device="{device_serial}".
## When You Are Done
- Call `mobile_report_result` with status PASS (success criterion met) or FAIL (not met).
- Do not read or explore any directories outside your current working directory.

## Your QA Task

{task.instruction}"""


def _ablation_instruction(task: BenchmarkTask, device_serial: str, tooling: str) -> str:
    """Neutral QA assignment for the MCP-ablation experiment (hunt episodes).
    Never states that bugs exist or how many to expect, and the wording is identical
    in both conditions except the tooling note — so the only difference is MCP."""
    # Use the app's own brief: substituting a bare feature list once dropped the
    # acceptance criteria AND the incremental AREA banking, so truncated ablation
    # episodes scored 0.
    if str((task.bug_spec or {}).get("mode") or "") == "journey":
        from . import journey
        return journey.brief(task, device_serial, tooling)
    features = [f["id"] for f in (task.bug_spec or {}).get("features", [])
                if not f.get("hidden")]
    feature_lines = "\n".join(f"- {fid}" for fid in features)
    result_line = ", ".join(f"{fid}=<ok|broken>" for fid in features)
    # The rewritten specs avoid the loaded word "broken"; keep the fallback line neutral.
    neutral_result_line = ", ".join(f"{fid}=<as_specified|deviates>" for fid in features)
    brief = (task.instruction or "").strip()

    # One text, shared with the journey brief and versioned there
    # (`brief.BRIEF_VERSION`); the per-arm reasoning lives beside it.
    tooling_note = _brief.tooling_note(tooling, device_serial)
    # No report note in either arm: both use the findings.yaml contract, and a
    # completion nudge here was the last asymmetry between them.
    report_note = ""

    if brief:
        # The QA task section is byte-identical across conditions; anything
        # condition-specific belongs in the tooling note above it.
        return f"""You are a QA engineer testing the Android app `{task.app_name}`.

## Device & App
The app (`{task.bundle_id}`) is installed and running on Android device `{device_serial}`.
{tooling_note}

## Important
You do NOT have the app's source code — there is nothing to read. Determine each
area's status ONLY by interacting with the running app on the device and observing
what it actually does. Verify behaviour; do not assume from a single tap.

## Your QA Task
{brief}

{submission.instruction(features)}

Finish your FINAL message with one line covering every area, as a fallback in case
any verdict above was missed:
  RESULT: {neutral_result_line}
"""

    # No brief on the spec (legacy suites): fall back to the bare feature list.
    return f"""You are a QA engineer testing the Android app `{task.app_name}`.

## Device & App
The app (`{task.bundle_id}`) is installed and running on Android device `{device_serial}`.
{tooling_note}

## Important
You do NOT have the app's source code — there is nothing to read. Determine each
feature's status ONLY by interacting with the running app on the device and observing
what it actually does. Verify behavior; do not assume from a single tap.

## Your QA Task
Test each of the following features end-to-end on the device and record whether it
behaves as a user would expect:
{feature_lines}

## Reporting
- Finish your FINAL message with one machine-readable line covering every feature:
  RESULT: {result_line}
{report_note}"""


async def _adb(*args: str) -> tuple[int, str]:
    """Run an adb command, return (rc, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        "adb", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


_SHARED_STORAGE_ROOTS = ("/sdcard/", "/storage/emulated/0/")


async def wipe_shared_storage(device: str, paths: list | None) -> None:
    """Wipe the spec's declared shared-storage dirs — pm clear never touches /sdcard,
    so user content leaks across trials. Runs before device_setup so staged content
    survives. Paths must sit under shared storage, or a typo would rm -rf the device."""
    for raw in paths or []:
        path = str(raw).rstrip("/")
        root = next((r for r in _SHARED_STORAGE_ROOTS if path.startswith(r)), None)
        if not root or not path[len(root):].strip():
            logger.warning("shared_storage: refusing to wipe %r — must be a directory "
                           "under %s", raw, " or ".join(_SHARED_STORAGE_ROOTS))
            continue
        q = shlex.quote(path)
        await _adb("-s", device, "shell", f"rm -rf {q}")
        await _adb("-s", device, "shell", f"mkdir -p {q}")
        logger.info("shared_storage: wiped %s", path)


async def _emu_console(device: str, command: str) -> tuple[int, str]:
    """Speak the emulator console protocol directly instead of `adb emu`, which
    always dials the CLIENT's localhost — dead inside a Docker container whose adb
    server (and emulators) live on the host. The console host follows the adb
    server override; the auth token file must be reachable (launch.py mounts the
    host's ~/.emulator_console_auth_token into the container)."""
    try:
        port = int(device.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 1, f"not an emulator serial: {device}"
    host = (os.environ.get("QGB_EMU_CONSOLE_HOST")
            or os.environ.get("ANDROID_ADB_SERVER_HOST") or "127.0.0.1")
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 10)
    except (OSError, asyncio.TimeoutError) as exc:
        return 1, f"console {host}:{port} unreachable: {exc}"
    try:
        async def _read_until_ok() -> str:
            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), 10)
                if not chunk:
                    break
                chunks.append(chunk.decode("utf-8", "replace"))
                text = "".join(chunks)
                if "OK" in text or "KO" in text:
                    return text
            return "".join(chunks)

        banner = await _read_until_ok()
        if "auth_token" in banner:
            token_path = Path(os.environ.get("ANDROID_EMULATOR_CONSOLE_AUTH_TOKEN")
                              or Path.home() / ".emulator_console_auth_token")
            token = token_path.read_text().strip() if token_path.exists() else ""
            writer.write(f"auth {token}\n".encode())
            await writer.drain()
            reply = await _read_until_ok()
            if "KO" in reply:
                return 1, f"console auth failed ({token_path})"
        writer.write(command.encode() + b"\n")
        await writer.drain()
        reply = await _read_until_ok()
        writer.write(b"quit\n")
        await writer.drain()
        return (0 if "OK" in reply else 1), reply.strip()
    except (OSError, asyncio.TimeoutError) as exc:
        return 1, f"console command failed: {exc}"
    finally:
        writer.close()


class DeviceSetupError(RuntimeError):
    """The spec's staged content cannot exist in this environment (a push source
    file is missing — e.g. an image built without `assets/`). Deterministic and
    corpus-level, so the episode must classify as env_failure, never as an agent 0."""


# Every seeded timestamp in the corpus renders through the device's timezone, and an
# emulator inherits the HOST zone — a case's date-and-time anchors ("Aug 29, 2026 7:00 AM") only
# exist in one zone. The harness pins the zone itself so a run is identical on any
# host. The value is the zone the answer keys were derived in; override only when
# re-deriving the whole corpus. It lives in `verify.device_oracle` (one definition:
# the oracle evaluates 'localtime' under the same zone this pins; `QGB_DEVICE_TIMEZONE`
# overrides it there).
from .verify.device_oracle import DEVICE_TIMEZONE  # noqa: E402


async def pin_device_timezone(device: str) -> bool:
    """`cmd alarm set-timezone` — no root, effective immediately, survives until the
    emulator is torn down. Called from every staging path (episode, replay reset,
    derivation) so no path can run in the host's zone by accident."""
    rc, _ = await _adb("-s", device, "shell", f"cmd alarm set-timezone {shlex.quote(DEVICE_TIMEZONE)}")
    _, got = await _adb("-s", device, "shell", "getprop persist.sys.timezone")
    ok = rc == 0 and got.strip() == DEVICE_TIMEZONE
    if not ok:
        logger.warning("could not pin %s timezone to %s (device reports %r) — seeded "
                       "timestamps may not render as the answer keys expect",
                       device, DEVICE_TIMEZONE, got.strip()[:40])
    return ok


# ── the device clock (QUA-2781) ──────────────────────────────────────────────
#
# The zone was pinned; the CLOCK was not, so the time of day was an unpinned input to
# the truth. Fossify Calendar's day header and its next-full-hour default for a new
# event, MedTimer's "today" Overview and its 08:00 reminder edge, tasks.org's "Due
# today" all render off the device clock. A row derived on one day carried that day's
# strings (`cal-switch-back-to-list`'s `absence_texts` were `16 Wednesday` and
# `02:00 AM`, the derivation day and hour), and two boards run a day apart met
# different screens. So every staging path now sets the clock to ONE fixed instant:
# `QGB_DEVICE_CLOCK` (ISO 8601; a naive value is read in `QGB_DEVICE_TIMEZONE`),
# default a Wednesday at 10:00 — clear of midnight and of MedTimer's 08:00 edge, and
# inside the week the corpus was derived in, so fixtures that carry absolute dates keep
# the same relation to "today". Override it only when re-deriving the whole corpus.
DEFAULT_DEVICE_CLOCK = "2026-09-16T10:00:00"
# How far the device clock may be from the pin when an episode starts and when it is
# handed to the agent — the default of `clock_tolerance_s()` (`QGB_CLOCK_TOLERANCE_S`
# overrides it). Measured on the four QUA-2786 board runs (400 episodes, one lane):
# the episode marker → agent start leg of staging took 21-22 s median and 30 s at worst,
# and the pin → hand-off gap the check actually reads is that plus isolation, launch
# and re-pin, under a minute. 300 s is five times that, so a multi-lane host whose
# staging runs up to ~5x slower still passes, while a pin that did not take is off by
# DAYS (the host's real date against a fixed 2026-09-16), not minutes. The hand-off
# offset is recorded per episode (`provenance.device_clock_offset_s`) so a slower host
# shows up as a trend before it voids anything; raise the setting for such a host
# rather than editing this number (QUA-2806).
CLOCK_TOLERANCE_S = 300
# Logcat buffers the crash/ANR windows read (`verify.crash`). See `pin_device_clock`.
_WINDOWED_LOG_BUFFERS = "main,system,crash,events"


def device_clock_pin() -> datetime:
    """The instant every staging path sets the device clock to, timezone-aware.
    Read per call (not at import) so a test or a re-derive can move it."""
    from zoneinfo import ZoneInfo

    raw = (os.environ.get("QGB_DEVICE_CLOCK") or DEFAULT_DEVICE_CLOCK).strip()
    pin = datetime.fromisoformat(raw)
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=ZoneInfo(DEVICE_TIMEZONE))
    return pin


async def device_epoch(device: str) -> int | None:
    """The device's wall clock in epoch seconds (`date +%s`), None when unreadable."""
    _, out = await _adb("-s", device, "shell", "date +%s")
    text = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return int(text) if text.isdigit() else None


async def pin_device_clock(device: str) -> dict:
    """Set the device clock to `device_clock_pin()` and return what happened
    (`{"pin", "method", "device_epoch", "ok"}`). Never raises; the episode-start
    invariant (`preflight.device_state_violations`) is what refuses a device the pin
    did not reach.

    `cmd alarm set-time <ms>` needs no root (measured on the android-35 google_apis
    image, 2026-09-23: as the shell user it sets the clock, while `date` answers
    "Operation not permitted"). Automatic time is switched off first, or network time
    could put it back. When set-time fails, the fallback is `date @<epoch>` under
    `adb root`, and the device is handed back unrooted through `set_adb_root` on every
    path out, as `run_device_setup` does.

    The clock goes BACK to the same instant on every reset, so everything the harness
    reads through a device-time window has to start empty here. `verify.crash` finds
    a death with `logcat -T <since>` over the crash, events, main and system buffers
    plus `dumpsys activity exit-info`, all stamped with device wall time and never
    cleared, so a crash the PREVIOUS pass logged at 10:01 would sit inside the next
    pass's window opened at 10:00:30 and read as this pass's death. Those buffers and
    the exit-info history are therefore cleared with the pin. As a side effect an
    agent can no longer read the previous episode's crash out of logcat. The rest of
    the device's crash history — `/data/anr` traces and the dropbox — goes with them
    (`clear_crash_history`, QUA-2790)."""
    pin = device_clock_pin()
    ms = int(pin.timestamp() * 1000)
    info: dict = {"pin": pin.isoformat(), "method": "", "device_epoch": None, "ok": False}
    await _adb("-s", device, "shell", "settings put global auto_time 0")
    rc, out = await _adb("-s", device, "shell", f"cmd alarm set-time {ms}")
    got = await device_epoch(device)
    if rc == 0 and got is not None and abs(got - ms // 1000) <= clock_tolerance_s():
        info["method"] = "alarm set-time"
    else:
        logger.warning("clock pin: `cmd alarm set-time` did not take on %s (rc=%s, %r); "
                       "falling back to `date` under adb root", device, rc,
                       out.strip()[:120])
        try:
            if await set_adb_root(device, True):
                await _adb("-s", device, "shell", f"date @{ms // 1000}")
                info["method"] = "date (root)"
        finally:
            try:
                await set_adb_root(device, False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("clock pin: could not unroot %s: %s", device, exc)
        got = await device_epoch(device)
    await _adb("-s", device, "shell", f"logcat -b {_WINDOWED_LOG_BUFFERS} -c")
    await _adb("-s", device, "shell", "am clear-exit-info")
    try:
        await clear_crash_history(device)
    except Exception as exc:  # noqa: BLE001 — the pin never raises; the invariant refuses
        logger.warning("clock pin: could not clear the crash history on %s: %s", device, exc)
    info["device_epoch"] = got
    info["ok"] = got is not None and abs(got - ms // 1000) <= clock_tolerance_s()
    if not info["ok"]:
        logger.error("could not pin %s clock to %s (device reads %s) — date and time "
                     "strings will not match the derived truth", device, pin.isoformat(),
                     got)
    return info


# ── the device's crash history (QUA-2790) ────────────────────────────────────
#
# Two more stores outlive `pm clear` and the pin's logcat / exit-info clear, and both
# carry the PREVIOUS episode's deaths: `/data/anr` (one full thread dump per ANR,
# `anr_<device time>`; a QUA-2785 agent read one from an earlier run through `su`) and
# the dropbox (`dumpsys dropbox --print` hands the shell user every `data_app_crash` /
# `data_app_anr` entry with its stack). Measured on the android-35 google_apis image
# (2026-09-23): `/data/anr` is `drwxrwxr-x system system` with `-rw------- system`
# files, so the shell user can LIST it and stat each trace but can neither read nor
# delete one; the dropbox directory is root-only, and `dumpsys dropbox` keeps its own
# index, so deleting its files leaves every entry listed at 0 bytes.
ANR_DIR = "/data/anr"
# One `<mtime epoch> <path>` line per entry, then an end marker, as the shell user
# (`preflight.device_state_violations` reads it); `qgb-anr-unreadable` when the
# directory exists and cannot be listed.
ANR_LIST_CMD = (f"if [ -d {ANR_DIR} ]; then ls {ANR_DIR} >/dev/null 2>&1 || "
                f"echo qgb-anr-unreadable; for f in {ANR_DIR}/*; do [ -e \"$f\" ] && "
                f"stat -c '%Y %n' \"$f\"; done; fi; echo qgb-anr-end")
# The dropbox has no clear command (`cmd dropbox` only tunes rate limits), but it trims
# itself whenever a dropbox setting changes, down to `dropbox_max_files`: set it to 0,
# wait for the index to read empty, and delete the setting to restore the default.
# The service removes its own files and index that way, no root, and keeps recording
# afterwards (measured: a crash after the restore is listed again).
_DROPBOX_EMPTY = "Drop box contents: 0 entries"
_DROPBOX_READS = 10
_DROPBOX_READ_S = 0.3


async def clear_crash_history(device: str) -> dict:
    """Empty `/data/anr` and the dropbox, the two crash stores the pin's logcat and
    exit-info clear does not reach, and return what happened (`{"anr": method,
    "anr_left": n, "dropbox": emptied}`). Never raises on a device answer; the
    episode-start invariant is what refuses a device whose `/data/anr` still holds a
    trace this staging did not write.

    `/data/anr` needs root to delete from. The harness runs `su 0` over its OWN adb
    (the agent's `su` is refused at the meter), which roots that one command and
    leaves adbd alone, so the device is still unrooted afterwards and no adb
    connection drops; `adb root` restarts adbd for every client and would be two
    restarts per reset. Where the image has no `su`, the fallback is `adb root` for
    the delete and the device handed back unrooted on every path out, as
    `pin_device_clock`'s own fallback does. Where neither works (a production image),
    nothing can delete a trace and the invariant says so."""
    info: dict = {"anr": "", "anr_left": None, "dropbox": False}

    await _adb("-s", device, "shell", "settings put global dropbox_max_files 0")
    try:
        for attempt in range(_DROPBOX_READS):
            _, head = await _adb("-s", device, "shell", "dumpsys dropbox | head -1")
            if _DROPBOX_EMPTY in head:
                info["dropbox"] = True
                break
            if "Drop box contents:" not in head:
                break               # no dropbox answer at all: waiting will not bring one
            if attempt + 1 < _DROPBOX_READS:
                await asyncio.sleep(_DROPBOX_READ_S)
    finally:
        await _adb("-s", device, "shell", "settings delete global dropbox_max_files")
    if not info["dropbox"]:
        logger.warning("crash history: the dropbox on %s did not read empty after the "
                       "trim", device)

    clear = f"rm -rf {ANR_DIR}/*"
    _, su = await _adb("-s", device, "shell", "which su")
    if su.strip().startswith("/"):
        await _adb("-s", device, "shell", f"su 0 sh -c {shlex.quote(clear)}")
        info["anr"] = "su"
    else:
        try:
            if await set_adb_root(device, True):
                await _adb("-s", device, "shell", clear)
                info["anr"] = "adb root"
        finally:
            try:
                await set_adb_root(device, False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("crash history: could not unroot %s: %s", device, exc)
    _, listing = await _adb("-s", device, "shell", ANR_LIST_CMD)
    left = [ln for ln in listing.splitlines() if ln.strip()[:1].isdigit()]
    info["anr_left"] = len(left)
    if left:
        logger.warning("crash history: %d trace(s) left in %s on %s (cleared with %s)",
                       len(left), ANR_DIR, device, info["anr"] or "nothing: no su, no root")
    return info


# A shell step that printed one of these did not do what the spec meant, whatever
# its exit code: `adb shell` folds the remote stderr into stdout, and toybox/run-as
# report a missing binary or file this way. `run-as: exec failed for sqlite3` is the
# one that hid four fixtures on Google Play images.
_SHELL_FAILURE_MARKERS = ("run-as: exec failed", "not found", "No such file", "Error:",
                          "sqlite3:")


async def set_adb_root(device: str, root: bool) -> bool | None:
    """Put adbd in the privilege the next step needs (`adb root` / `adb unroot`) and
    return whether the device shell IS root afterwards (None: could not tell).

    adbd's privilege is DEVICE state, not a property of one command: `adb root`
    restarts the daemon as uid 0 for every later shell, including the agent's. Both
    verbs are no-ops that print a line when adbd is already there; when they DO
    restart it, the adb client waits for the device to drop and come back, and
    `wait-for-device` makes sure it did. The answer is read back with `id -u` rather
    than trusted from the verb, because a production build refuses `adb root` with
    rc 0 (and an `ro.secure=0` image stays root through `adb unroot`)."""
    verb = "root" if root else "unroot"
    rc, out = await _adb("-s", device, verb)
    if rc != 0:
        logger.warning("device_setup: adb %s failed: %s", verb, out.strip()[:120])
        return None
    if "restarting" in out:
        await _adb("-s", device, "wait-for-device")
    _, uid = await _adb("-s", device, "shell", "id -u")
    uid = uid.strip()
    if uid not in ("0", "2000"):
        logger.warning("device_setup: could not read the shell uid after adb %s: %r",
                       verb, uid[:60])
        return None
    if (uid == "0") != root:
        logger.warning("device_setup: adbd is %s after `adb %s` (%s)",
                       "ROOT" if uid == "0" else "not root", verb, out.strip()[:120])
    return uid == "0"


async def check_adbd_after_agent(device: str) -> dict:
    """Read adbd's privilege right after the agent exits, over the harness's own adb
    (QUA-2795), and return `{"uid", "rooted", "root_primed", "unprimed"}` for
    `provenance.adbd_at_end`. Never raises.

    The meter refuses the agent's `root:`/`usb:`/`tcpip:` and `setprop service.adb.*`,
    so an episode that still ENDS rooted (`rooted`) found a way the meter does not see,
    and one that ends with `service.adb.root` = 1 (`root_primed`) left adbd set to come
    back as root on its next restart. The primed property is cleared here
    (`unprimed`), because staging cannot clear it: `adb unroot` on an unrooted adbd
    answers "adbd not running as root" and leaves the property at 1 (measured on the
    android-35 image). A rooted adbd is not unrooted here; every staging path already
    does that (`run_device_setup`'s `set_adb_root(device, False)`)."""
    info: dict = {"uid": None, "rooted": None, "root_primed": None, "unprimed": False}
    try:
        _, out = await _adb("-s", device, "shell", "id -u; getprop service.adb.root")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        uid = lines[0] if lines else ""
        if uid.isdigit():
            info["uid"] = int(uid)
            info["rooted"] = uid == "0"
        info["root_primed"] = len(lines) > 1 and lines[1] == "1"
        if info["root_primed"]:
            rc, _ = await _adb("-s", device, "shell", "setprop service.adb.root 0")
            info["unprimed"] = rc == 0
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never fail an episode
        logger.warning("could not read adbd privilege on %s after the agent: %s", device, exc)
        return info
    if info["rooted"] or info["root_primed"]:
        logger.warning("episode ended with adbd %s on %s: an agent-side privilege change "
                       "got past the adb meter",
                       "ROOT" if info["rooted"] else "primed for root (service.adb.root=1)",
                       device)
    return info


async def run_device_setup(device: str, spec_setup: dict | None) -> None:
    """Stage the spec's `device_setup:` content after pm clear and before launch —
    media apps are untestable on a fresh emulator. Content is fixed and named so the
    oracle stays deterministic. Always pins the device timezone and then the device
    CLOCK first (`pin_device_clock`, QUA-2781), even for a spec without
    `device_setup:`, so a fixture that stamps "now" stamps the pinned instant.

    Privilege (QUA-2743): the fixture runs as root only when it declares `root: true`
    and as the shell user otherwise, whatever the device was left in — an agent may
    `adb root` itself, and a fixture that writes an app's files as root leaves them
    unreadable to the app (AnkiDroid's StorageAccessException). And the device is
    ALWAYS handed back unrooted, on every path out, error included: adbd's privilege
    outlives the command, so without this every later episode on that device got a
    root adb shell, and an episode's privileges depended on which app ran before it.
    Every staging path goes through here (the live episode, `replay._reset`, both
    derive scripts). Blocks, in order:

    * `push:`  — `[{src: <repo path>, dest: <device path>}]`; a MISSING source raises.
    * `shell:` — `adb shell` commands. A non-zero exit, or output carrying one of
      `_SHELL_FAILURE_MARKERS`, raises: a fixture that half-applies is worse than one
      that fails, because the episode would be scored against a world that is not
      there (`medtimer-skip-logged-dose` was charged to agents for exactly that).
    * `sql:`   — `[{package: <bundle id>, db: <name>, statements: <sql> | [<sql>...]
      | file: <repo path>}]`. Rows written INTO an app's SQLite database from the
      HOST (`verify.device_oracle.apply_sql`): the app is force-stopped, the file is
      pulled with `run-as cat` like the db oracle reads it, the statements run in one
      transaction under the device's timezone, and the file is written back and
      verified. `db:` is named exactly as a `db:` oracle names it — a file under the
      app's `databases/`, or an absolute shell-readable path. Never an on-device
      `sqlite3`: Google Play images ship none. Runs AFTER `shell:` so a fixture may
      launch the app once to create the database it then rewrites.
    * `emu:`   — emulator-console commands (`sms send …`), best-effort.
    Every failure that means "the seeded start state cannot exist here" raises
    DeviceSetupError; transient adb hiccups on pushes stay best-effort."""
    await pin_device_timezone(device)
    await pin_device_clock(device)
    try:
        await _stage_device_setup(device, spec_setup)
    finally:
        # Unconditionally, and never raising: a failure here must not mask the
        # DeviceSetupError that may be on its way out.
        try:
            await set_adb_root(device, False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("device_setup: could not unroot %s: %s", device, exc)


async def _stage_device_setup(device: str, spec_setup: dict | None) -> None:
    if not spec_setup:
        return
    repo_root = Path(__file__).resolve().parents[2]
    # `adb root` (emulator / userdebug only) when the fixture declares it: needed to
    # purge SYSTEM providers the shell uid may not touch — e.g. the telephony store
    # behind an SMS app. Otherwise make sure it is NOT root, whatever ran before.
    await set_adb_root(device, bool(spec_setup.get("root")))
    for item in spec_setup.get("push", []):
        # Held-out root first, then the packaged assets/ tree (corpus.asset_path).
        from .corpus import asset_path
        src = asset_path(str(item["src"]))
        dest = str(item["dest"])
        if not src.exists():
            raise DeviceSetupError(
                f"device_setup push source missing: {src} — the episode's seeded "
                f"start state cannot be staged in this environment")
        await _adb("-s", device, "shell", f"mkdir -p {shlex.quote(str(Path(dest).parent))}")
        rc, out = await _adb("-s", device, "push", str(src), dest)
        if rc != 0:
            logger.warning("device_setup: push %s failed: %s", src.name, out.strip()[:160])
    for cmd in spec_setup.get("shell", []):
        rc, out = await _adb("-s", device, "shell", str(cmd))
        marker = next((m for m in _SHELL_FAILURE_MARKERS if m in out), None)
        if rc != 0 or marker:
            raise DeviceSetupError(
                f"device_setup shell step failed (rc={rc}"
                f"{', output has ' + repr(marker) if marker else ''}): {str(cmd)[:120]!r} "
                f"→ {out.strip()[:200]!r} — the episode's seeded start state was not "
                f"staged")
    for item in spec_setup.get("sql", []):
        from .verify.device_oracle import SqlFixtureError, apply_sql
        try:
            detail = await asyncio.to_thread(apply_sql, dict(item), device,
                                             tz=DEVICE_TIMEZONE, repo_root=str(repo_root))
        except SqlFixtureError as exc:
            raise DeviceSetupError(
                f"device_setup {exc} — the episode's seeded start state was not "
                f"staged") from exc
        logger.info("device_setup: sql %s", detail)
    # Emulator-console commands (`adb emu ...`): the only way to deliver an SMS or a
    # call INTO the device — the telephony providers refuse shell-uid inserts.
    for cmd in spec_setup.get("emu", []):
        rc, out = await _emu_console(device, str(cmd))
        if rc != 0 or "KO" in out:
            logger.warning("device_setup: emu %r failed: %s", cmd, out.strip()[:160])
    logger.info("device_setup: staged %d file(s), %d command(s), %d sql fixture(s), "
                "%d emu command(s)",
                len(spec_setup.get("push", [])), len(spec_setup.get("shell", [])),
                len(spec_setup.get("sql", [])), len(spec_setup.get("emu", [])))


async def normalize_app_env(device: str, bundle_id: str) -> None:
    """Restore PORTRAIT, re-grant permissions, allow the MANAGE_EXTERNAL_STORAGE app-op
    (`pm grant` cannot set it), zero animation scales. Runs after every reset: pm clear
    revokes grants, and the benchmark measures QA skill, not consent-dialog navigation."""
    # Orientation is a DEVICE setting and `pm clear` does not touch it, so a lifecycle
    # case whose agent rotated the emulator (`cal-repeat-survives-rotation`, QUA-2712)
    # hands EVERY later episode on that device a landscape screen it never asked for —
    # and journey truth was derived in portrait. This is the live counterpart of
    # `replay._reset`'s first act; it calls the same helper, so the ordering that makes
    # it work (auto-rotate off BEFORE `user_rotation` is pinned, QUA-2709) is written
    # once. First in staging, so device_setup sees the layout the episode was authored
    # against. It does NOT carry the launch: after `pm clear` the launcher is in front,
    # and a pin written under the launcher is undone when the app launches. That is
    # why `run_episode` pins again after `session.launch_app` (QUA-2734).
    await _set_rotation(device, "portrait")

    async def sh(*args: str) -> str:
        rc, out = await _adb("-s", device, *args)
        return out

    dump = await sh("shell", "dumpsys", "package", shlex.quote(bundle_id))

    # "requested permissions:" lists one per line, sometimes with ": granted=false".
    requested: list[str] = []
    in_block = False
    for line in dump.splitlines():
        s = line.strip()
        if s.startswith("requested permissions:"):
            in_block = True
            continue
        if in_block:
            if not s or not s.startswith("android.permission") and ":" not in s and "." not in s:
                break
            perm = s.split(":")[0].strip()
            if perm.startswith("android.permission") or perm.count(".") >= 2:
                requested.append(perm)
            elif not s.startswith("android.permission"):
                break

    granted = 0
    for perm in requested:
        out = await sh("shell", "pm", "grant", bundle_id, perm)
        if "Exception" not in out and "Error" not in out:
            granted += 1

    if "MANAGE_EXTERNAL_STORAGE" in dump:
        await sh("shell", "appops", "set", bundle_id, "MANAGE_EXTERNAL_STORAGE", "allow")

    for scale in ("window_animation_scale", "transition_animation_scale",
                  "animator_duration_scale"):
        await sh("shell", "settings", "put", "global", scale, "0")

    logger.info("env normalised for %s: %d/%d runtime permissions granted",
                bundle_id, granted, len(requested))


async def isolate_app_under_test(device: str, bundle_id: str) -> None:
    """Leave the LAUNCHER as the task directly beneath the app about to be launched, so
    a crash, or a `back` from the app's root screen, lands on the home screen and not
    in some other app. The agent would happily keep testing that other app, and a
    derivation writes it into truth as the seeded post-crash screen: `cal-search-event`
    recorded TrustLoop's sign-in screen because `com.trustloop` happened to be the task
    beneath fossify-calendar on the emulator it was derived on (QUA-2733).

    Every other BENCHMARK app is force-stopped first. force-stop, not uninstall: it
    ends a package's processes AND removes its tasks, at one adb call per package. That
    reaches only the apps in the registry, though, and a developer's emulator always
    carries others. The device is therefore sent HOME last, which makes the launcher
    the top task whatever else is installed: the surviving tasks sit behind it, and the
    app launched next goes directly on top of it. Force-stopping more packages instead
    would mean chasing every app a developer might install.

    HOME must stay the FINAL action. This is the one chokepoint both staging paths
    share, and each launches the app next: `run_episode` calls `session.launch_app`
    directly after it, and `replay._reset` only writes the flags file (`run-as`, which
    brings nothing forward) before the route's `launch` step. A step added after the
    HOME that brought another task forward would reopen the gap.
    `tests/test_isolation.py` pins this function and both callers."""
    from . import bugs as bugmod

    others = {
        str(s.get("app", {}).get("package") or "")
        for s in bugmod.load_apps()
    } - {bundle_id, ""}
    for pkg in sorted(others):
        await _adb("-s", device, "shell", "am", "force-stop", pkg)
    # `kill-all` kills background PROCESSES; it does not remove TASKS. A killed app's
    # task stays in recents, and Android recreates the process when that task next
    # comes forward, which is exactly what a crash or a root-screen `back` does to the
    # task beneath. So this only frees memory and CPU. It cannot stop `back` from
    # resurrecting an app; the HOME below is what prevents that.
    await _adb("-s", device, "shell", "am", "kill-all")
    # LAST: the launcher becomes the task directly beneath whatever launches next.
    await _adb("-s", device, "shell", "input", "keyevent", "KEYCODE_HOME")
    logger.info("isolated %s on %s (cleared %d other benchmark app(s), sent HOME)",
                bundle_id, device, len(others))


# The files `write_bug_flags` puts in the app's sandbox: the active seeded ids and the
# per-episode nonce. The snapshot tars the sandbox AFTER they are written, and it lands
# in the episode directory, which the agent can read (its cwd's parent, exempt from the
# contamination scan) — so it would carry this episode's answer key, and its arm (a
# clean episode's flags file holds the nonce line alone, and `tar tv` shows the size
# without printing the nonce). Replay never needs them: `replay._reset` restores the
# tar and then writes the flags itself, LAST (QUA-2806).
_SNAPSHOT_SECRET_MEMBERS = ("files/qgb_flags.txt", "files/.qgb/nonce")


def strip_flag_files(tar_path: Path) -> list[str]:
    """Rewrite the app-data snapshot at `tar_path` without `_SNAPSHOT_SECRET_MEMBERS`;
    return the member names removed. Leaves the file untouched when it holds none of
    them or cannot be read as a tar (logged). Never raises."""
    import tarfile

    def secret(name: str) -> bool:
        while name.startswith("./"):
            name = name[2:]
        return name.rstrip("/") in _SNAPSHOT_SECRET_MEMBERS

    tar_path = Path(tar_path)
    tmp = tar_path.with_name(tar_path.name + ".tmp")
    removed: list[str] = []
    try:
        with tarfile.open(tar_path, "r:") as src:
            members = src.getmembers()
            removed = [m.name for m in members if secret(m.name)]
            if not removed:
                return []
            with tarfile.open(tmp, "w:", format=src.format) as dst:
                for m in members:
                    if secret(m.name):
                        continue
                    dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        os.replace(tmp, tar_path)
    except (OSError, tarfile.TarError) as exc:
        logger.warning("could not strip the flag files from %s: %s", tar_path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return []
    return removed


async def take_replay_snapshots(device: str, bundle_id: str, run_dir: Path,
                                bug_spec: dict | None) -> None:
    """Snapshot app data COLD, then cold-launch for the agent. Relaunch + settle first
    (first launch seeds; an early tar catches an empty sandbox), force-stop before the
    tar (a running app misses unflushed state) — agent-start equals replay-start."""
    await _relaunch_app(device, bundle_id)
    await asyncio.sleep(3.0)
    await wait_stable(device)
    await _adb("-s", device, "shell", "am", "force-stop", bundle_id)
    # The settle launch above may have run a seeded path (a fault on the startup
    # route); its marker must not ride into the tar, or every replay pass would
    # start with a marker it did not earn.
    from .verify.canary import clear_fired
    await clear_fired(device, bundle_id)
    ok = await replay_snapshot(device, bundle_id, run_dir / "app_snapshot.tar")
    if ok:
        strip_flag_files(run_dir / "app_snapshot.tar")
    if not ok:
        logger.warning("app-data snapshot came back empty — replays for this "
                       "episode will not be deterministic")
    # pm clear never touches /sdcard, so shared-storage apps need their own snapshot
    # or the second replay pass starts from a world the first pass edited — silently.
    shared_paths = (bug_spec or {}).get("shared_storage")
    if shared_paths:
        if not await snapshot_shared(device, shared_paths,
                                     run_dir / "shared_snapshot.tar"):
            logger.warning("shared-storage snapshot came back empty for %s — "
                           "replays for this episode will not be deterministic",
                           bundle_id)
    (run_dir / "snapshot_meta.json").write_text(json.dumps({"mode": "cold"}))
    await _relaunch_app(device, bundle_id)
    await asyncio.sleep(3.0)
    await wait_stable(device)


_FOREGROUND_RE = re.compile(
    r"(?:mCurrentFocus|topResumedActivity|mResumedActivity)\S*[=\s].*?([A-Za-z][\w.]+)/")


async def foreground_package(device: str) -> str:
    """Foreground package, or "" if undetermined. Parsed on the HOST: emulator images
    differ in dumpsys keys and busybox grep, and a device-side pipe silently returned
    nothing — which reads as "did not wander"."""
    rc, out = await _adb("-s", device, "shell", "dumpsys", "window")
    if rc != 0 or not out:
        rc, out = await _adb("-s", device, "shell", "dumpsys", "activity", "activities")
        if rc != 0:
            return ""
    match = _FOREGROUND_RE.search(out)
    return match.group(1) if match else ""


# Uniform flags-file shape (QUA-2814). The meter denies every agent read of
# `qgb_flags.txt` and the snapshot is stripped of it, so an agent only reaches the LIVE
# file by getting root — which is itself a hard contamination hit (`adbd_rooted`) or
# trips the `su`/`eval`/… rules. But QUA-2806 left the file's METADATA arm-dependent: a
# clean arm held one line and a seeded arm held one per active bug, so a rooted `wc -l`
# / `stat` / `ls -l` told the arms apart WITHOUT reading content (the nonce, which only
# trips on a content read, never fired). Every episode now writes exactly
# `FLAG_FILE_LINES` lines padded to `FLAG_FILE_WIDTH`, so the line count and the byte
# size are identical on both arms and across every case. The active bug ids are still
# in there verbatim (the shim reads them); a content read that filters the `#`-comment
# lines (`grep -v '^#'`) still exposes them — see write_bug_flags' docstring and
# CLAUDE.md for exactly what the backstop does and does not cover.
FLAG_FILE_LINES = 32
FLAG_FILE_WIDTH = 79       # ≥ the longest line (`#<nonce> <16 hex>` = 60; ids ≤ ~40)


def flag_file_lines(nonce: str, active_bugs) -> list[str]:
    """The lines of `qgb_flags.txt` for one episode: line 1 is the nonce comment, the
    active bug ids follow (each trailed by a fresh nonce-bearing comment line so a
    line-oriented partial read — `head`, `tail -n +2`, `sed -n Np` — still meets the
    nonce), then filler comment lines to a FIXED count, every line padded to a FIXED
    width. The shim trims each line, so the trailing pad is invisible to it and the
    ids still match; the `#`-comment lines land in its set harmlessly and `on()` never
    queries them. Same shape whatever the active set, so `wc -l`/`wc -c`/`stat` are
    arm-independent."""
    comment = "#" + nonce
    lines = [comment]
    for bug in (str(b) for b in (active_bugs or []) if b):
        lines.append(bug)
        lines.append(f"{comment} {secrets.token_hex(8)}")
    while len(lines) < FLAG_FILE_LINES:
        lines.append(f"{comment} {secrets.token_hex(8)}")
    return [ln.ljust(FLAG_FILE_WIDTH) for ln in lines]


async def write_bug_flags(device: str, bundle_id: str, bug_spec: dict | None) -> None:
    """Activate exactly this episode's seeded bugs via files/qgb_flags.txt (run-as,
    debug build). Clean episodes get no active id; pre-gate apps ignore it.
    Best-effort — a failed write leaves the legacy all-bugs-live behaviour.

    The file is padded to a fixed line count and byte size (`flag_file_lines`) so its
    METADATA never reveals the arm. What the nonce backstop covers: a whole-file read
    and any line-oriented partial read (`head`/`tail`/`sed`) surface the nonce and void
    the episode. What it does NOT cover: a read that strips the `#`-comment lines
    (`grep -v '^#'`, `awk '!/^#/'`) still exposes the active bug ids — but reaching the
    file at all needs root or `run-as` outside the meter. The meter refuses both on the
    bare arm (root is also a hard `adbd_rooted`/`su` hit); on the MCP arm it depends on
    the DevLoop server running no caller-controlled shell text. The file is also
    stripped from the app-data snapshot."""
    if bug_spec is None:
        return
    if "active_bugs" in bug_spec:
        # Explicit subset — how QA-brief episodes randomise which bugs are live.
        active = list(bug_spec.get("active_bugs") or [])
    elif str(bug_spec.get("mode", "")) == "explore":
        # Hunt: the whole seeded set. Without this branch the list came out empty
        # and the episode silently ran against a clean app.
        active = [str(f.get("bug_id")) for f in (bug_spec.get("features") or [])
                  if str(f.get("state")) == "broken" and f.get("bug_id")]
    elif str(bug_spec.get("type", "bug")).lower() == "clean":
        active = []
    else:
        active = [str(bug_spec.get("id") or "")]
    active = [a for a in active if a]

    # Per-episode NONCE backstop (QUA-2804): a random value written into qgb_flags.txt
    # and a `.qgb` marker. The only way it reaches the agent is by reading the app's
    # private sandbox — which the meter refuses — so if it ever surfaces in an
    # agent-visible tool result or a file the agent wrote, `contamination.scan` voids the
    # episode (same mechanism and consequence as the CANARY). Generated once per episode.
    nonce = str((bug_spec.get("flag_nonce") or "")).strip()
    if not nonce:
        nonce = "QGB-NONCE-" + secrets.token_hex(16)
        bug_spec["flag_nonce"] = nonce

    # One line PER argument — the shim reads with readLines(), and printf never expands
    # escapes inside a %s argument. Passing each line as its own argument to
    # `printf '%s\n'` puts the newline in the format, where it does expand.
    ids = " ".join(shlex.quote(ln) for ln in flag_file_lines(nonce, active))
    # The attribution markers (`verify.canary`) are wiped in the same command, so a
    # marker read after the agent exits was written during this episode; the nonce
    # marker (`files/.qgb/nonce`) is (re)written here too. `files/.qgb/fired` is
    # (re)created empty so `ls files/.qgb` lists the same entries on both arms
    # (QUA-2814); the seeded arm's own code fills it at runtime, which is root-gated.
    from .verify.canary import clear_fired_sh
    inner = (f"{clear_fired_sh()}; mkdir -p files files/.qgb files/.qgb/fired && "
             f"printf '%s\\n' {ids} > files/qgb_flags.txt && "
             f"printf %s {shlex.quote(nonce)} > files/.qgb/nonce")
    cmd = f"run-as {shlex.quote(bundle_id)} sh -c {shlex.quote(inner)}"
    rc, out = await _adb("-s", device, "shell", cmd)
    if rc != 0:
        logger.warning("could not write bug flags for %s: %s", bundle_id, out.strip()[:200])
        return
    bug_spec["active_bugs_written"] = active
    logger.info("bug flags for %s → %s", bundle_id, active or "(none: clean episode)")


# How many times the landing screen is read before its anchor is called missing. A
# dump can come back mid-paint, and a false env_failure silently DELETES a real
# episode from every board — so absence has to be the settled answer, not the first one.
_PRECONDITION_ATTEMPTS = 3
_PRECONDITION_SETTLE_S = 1.0


def precondition_anchor(app_id: str, case_id: str) -> tuple[str, str]:
    """(label, row) of the tap the case's `check:` route makes FIRST, or ("", "") if
    there is none. `row` is "" unless that tap is scoped with `row:`.

    Every route starts `[launch, {tap: X}, ...]`, so X is the one thing the case needs
    to already exist on the landing screen — seeded content (`Ibuprofen (2.5)`,
    `Aug 29, 2026 7:00 AM`) as often as app chrome. The route lives in the test-case
    file, not on the task spec, so it is read back through `journey.load_cases`, one
    step at a time through `submission.route_item`, as `truth._steps` reads the route
    the replayer runs. A `row:` is part of the anchor (QUA-2738): the route's
    `{tap: Reminded, row: "Ibuprofen (4)"}` can only run in Ibuprofen's row, so another
    row's "Reminded" on screen is not the world the case assumes."""
    from . import journey

    doc = journey.load_cases(app_id) or {}
    for case in doc.get("test_cases", []):
        if str(case.get("id")) != case_id:
            continue
        for step in (case.get("check") or {}).get("steps") or []:
            shape = submission.route_item(step) if isinstance(step, dict) else None
            if shape is not None and shape[0] == "tap":
                return shape[1], shape[2] or ""
        break
    return "", ""


async def assert_precondition(device: str, spec: dict) -> str:
    """Journey only: is the world the test case assumes actually on screen?

    Staging writes the case's preconditions (sample databases, seeded rows) and nothing
    re-reads them, so a fixture that stopped producing the expected screen was charged
    to the AGENT — `medtimer-skip-logged-dose` cost two episodes a completion, a false
    report and a missed bug on 2026-09-10 because the card its first step taps was not
    on today's Overview at all. Recorded as `staging_failed`, which the scorer turns
    into `env_failure` and every board excludes.

    Returns "present" | "missing" | "unknown" | "skipped" (the outcome, for tests).

    Two properties make this safe to assert:
    * The anchor is resolved with the replayer's own `_candidates`, the resolver the
      corpus derivation used, under the route's own `row:` scope — so "absent" means
      "the route's first tap could not have run", not some new notion of presence.
    * `derive_journey.py` only admits a case whose route runs end to end on BOTH the
      clean and the seeded build, so a seeded display defect can never be what moved
      this anchor. Absence is environmental by construction.
    A read that fails is "unknown": a diagnostic must never be what kills an episode."""
    if str(spec.get("mode") or "") != "journey" or spec.get("staging_failed"):
        # An already-failed staging keeps its own, more specific reason.
        return "skipped"
    try:
        from .replay import _anchor_desc, _candidates
        from .verify.device import dump_vh
        from .verify.match import visible_texts

        anchor, row = precondition_anchor(str(spec.get("app_id") or ""),
                                          str(spec.get("case_id") or ""))
        if not anchor:
            return "skipped"
        seen: list = []
        for attempt in range(_PRECONDITION_ATTEMPTS):
            xml = await dump_vh(device)
            if xml and _candidates(xml, anchor, row=row):
                logger.info("precondition for %s: %s is on the landing screen",
                            spec.get("case_id"), _anchor_desc(anchor, row))
                return "present"
            if xml:
                seen = visible_texts(xml)[:12]
            if attempt + 1 < _PRECONDITION_ATTEMPTS:
                await wait_stable(device)
                await asyncio.sleep(_PRECONDITION_SETTLE_S)
        if not seen:
            # Nothing readable at all — that is a dead dump, not a missing fixture.
            logger.warning("precondition for %s: the screen could not be read; "
                           "leaving the episode alone", spec.get("case_id"))
            return "unknown"
        spec["staging_failed"] = (
            f"precondition not met: the case's first step taps "
            f"{_anchor_desc(anchor, row)}, which is not on the landing screen after "
            f"staging. On screen instead: {', '.join(seen)}")
        logger.error("precondition for %s FAILED — %s", spec.get("case_id"),
                     spec["staging_failed"])
        return "missing"
    except Exception as exc:  # noqa: BLE001 - never fail an episode on a diagnostic
        logger.warning("precondition check for %s could not run: %s",
                       spec.get("case_id"), exc)
        return "unknown"


async def _refuse_dirty_device(device: str, task: BenchmarkTask, *, stage: str,
                               expect_launcher: bool, observed: dict | None = None) -> bool:
    """Assert the episode-start invariant (`preflight.device_state_violations`) and
    return whether the episode may go on. A violation is recorded as `staging_failed`
    (→ `env_failure` in every scorer; the guided one sees an agent that touched nothing
    and excludes it as `infra_failure`) with the offending values, and the agent is not
    launched: a device that is not in the state the truth was derived in cannot produce
    a result any board keeps. An episode whose staging ALREADY failed (a
    `DeviceSetupError`) is not checked: it keeps its own reason and its old path.
    Never raises: a read that errors is itself a violation."""
    spec = task.bug_spec
    if spec is not None and spec.get("staging_failed"):
        return True
    from .preflight import device_state_violations
    try:
        bad = await device_state_violations(device, expect_launcher=expect_launcher,
                                            clock_pin=device_clock_pin(),
                                            tolerance_s=clock_tolerance_s(),
                                            observed=observed)
    except Exception as exc:  # noqa: BLE001 - reported as a violation, never raised
        bad = [f"device state could not be read: {type(exc).__name__}: {exc}"]
    if not bad:
        return True
    reason = f"device not clean at {stage}: " + "; ".join(bad)
    logger.error("%s on %s — %s", task.id, device, reason)
    if spec is not None:
        spec["staging_failed"] = reason
        return False
    # No spec to record the exclusion in (a bare episode): say so and let it run, since
    # nothing downstream could tell a refused episode from an agent's 0.
    logger.error("%s has no bug_spec to record the refusal in; running it anyway", task.id)
    return True


async def _journey_oracle(device: str, bundle_id: str, spec: dict) -> None:
    """The completion oracle, read after the agent exits. Skipped on a blocked version
    (expected FAIL is judged on the verdict and the blocking bug), and for a
    screen-text oracle (proven by the agent's own device output instead).

    Liveness (`crash`/`anr`/`stuck`, standalone or as a gate on db/content): the
    episode-level fact is what `_record_app_crashes` already read — every death of
    the app's own process while the agent ran (`spec["app_crashes"]`, app-class rows,
    ANRs included). No re-query. A recorded death that the gate accepts is
    `violated` (the clean arm's app must stay alive on this case), a death the gate
    does not accept is `inconclusive` (the fault that fired is not this case's), and
    with nothing recorded a `stuck` oracle sends its one probe tap now — the app is
    still up; the agent's last screen is whatever it is, so a missing anchor is
    `inconclusive`. Then the state oracle, if any."""
    oracle = spec.get("oracle") or {}
    mode = oracle.get("mode")
    if spec.get("blocking") or (mode not in ("db", "content") and mode not in submission.LIVENESS_MODES):
        return
    from . import replay as rp
    expect, err = submission._parse_expect(oracle.get("expect"), str(spec.get("case_id")), trusted=True)
    if expect is None:
        spec["oracle_result"], spec["oracle_detail"] = "inconclusive", err or "unparseable expect"
        return
    if expect.gates:
        rows = [c for c in (spec.get("app_crashes") or []) if c.get("classification") != "foreign"]
        if rows:
            first = rows[0]
            as_result = rp.ReplayResult(rp.CRASHED,
                                        f"{bundle_id} {'stopped responding (ANR)' if first.get('kind') == 'anr' else 'crashed'} "
                                        f"while the agent ran — {first.get('signature')}",
                                        0, crash=dict(first))
            gated = rp.gate_crash(as_result, expect, spec.get("fired"), spec.get("active_bugs") or [])
            spec["oracle_result"] = "violated" if gated.outcome == rp.CRASHED else "inconclusive"
            spec["oracle_detail"] = gated.detail
            logger.info("journey oracle for %s: %s (%s)", spec.get("case_id"),
                        spec["oracle_result"], spec["oracle_detail"])
            return
        if expect.stuck:
            try:
                since = await crash_window(device)
                probe = await rp._check_stuck(device, bundle_id, expect, 0, since)
            except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail an episode
                probe = rp.ReplayResult(rp.INCONCLUSIVE, f"stuck probe failed: {exc}"[:160], 0)
            if probe.outcome != rp.HOLDS or expect.mode == "stuck":
                spec["oracle_result"] = {rp.CRASHED: "violated", rp.HOLDS: "holds"}.get(probe.outcome, "inconclusive")
                spec["oracle_detail"] = probe.detail
                logger.info("journey oracle for %s: %s (%s)", spec.get("case_id"),
                            spec["oracle_result"], spec["oracle_detail"])
                return
        if expect.mode in ("crash", "anr"):
            spec["oracle_result"], spec["oracle_detail"] = "holds", "the app stayed alive while the agent ran"
            logger.info("journey oracle for %s: holds (alive)", spec.get("case_id"))
            return
    try:
        if expect.mode == "db":
            # AnkiDroid holds its collection under an exclusive lock while it runs, so a
            # live read fails "database is locked"; the agent has exited, stop the app.
            await _adb("-s", device, "shell", "am", "force-stop", bundle_id)
            await asyncio.sleep(1.0)
        check = rp._check_content if expect.mode == "content" else rp._check_db
        got = await check(device, bundle_id, expect, 0)
        spec["oracle_result"], spec["oracle_detail"] = got.outcome, got.detail
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail an episode
        spec["oracle_result"], spec["oracle_detail"] = "inconclusive", str(exc)[:160]
    logger.info("journey oracle for %s: %s (%s)", spec.get("case_id"), spec.get("oracle_result"),
                spec.get("oracle_detail"))


_APP_CRASH_LIMIT = 40


async def _record_app_crashes(device: str, bundle_id: str, since: str, spec: dict) -> None:
    """Diagnostic only: which processes died while the agent drove the device.
    `app_crashes` lists every crash-buffer record in the window (app-class first,
    foreign — keyboards, `uiautomator dump` shell commands — kept but marked, capped)
    and `app_crash_count` counts the app's own. Evidence for whoever reads the run
    and for the later crash oracle; it feeds no score and can never fail an episode."""
    try:
        from .verify.crash import anrs_since, classify, crashes_since, signature
        recs = await crashes_since(device, bundle_id, since, include_foreign=True)
        # ANRs never reach the crash buffer; the app's own are read off the ANR log
        # lines so a hang the agent ran into is on record like a crash.
        recs += await anrs_since(device, bundle_id, since)
        rows = [{"process": r.process, "kind": r.kind, "exception": r.exception,
                 "message": (r.message or "")[:200],
                 "signature": signature(r, bundle_id),
                 "classification": classify(r, bundle_id), "timestamp": r.timestamp}
                for r in recs]
        rows.sort(key=lambda c: c["classification"] == "foreign")     # app-class first
        spec["app_crash_count"] = sum(1 for c in rows if c["classification"] != "foreign")
        spec["app_crashes"] = rows[:_APP_CRASH_LIMIT]
        if spec["app_crash_count"]:
            logger.warning("episode: %s crashed %d time(s) while the agent ran — first: %s",
                           bundle_id, spec["app_crash_count"], rows[0]["signature"])
    except Exception:  # noqa: BLE001 - never fail an episode on a diagnostic
        logger.warning("app-crash diagnostic could not run", exc_info=True)


async def _record_fired(device: str, bundle_id: str, spec: dict) -> None:
    """`spec["fired"]`: the seeded-site markers present after the agent exited. A
    diagnostic and the crash oracle's identity signal; it feeds no score."""
    try:
        from .verify.canary import fired_markers
        spec["fired"] = await fired_markers(device, bundle_id)
        if spec["fired"]:
            logger.info("seeded paths that reported themselves: %s", spec["fired"])
    except Exception:  # noqa: BLE001 - never fail an episode on a diagnostic
        logger.warning("fired-marker read could not run", exc_info=True)


def _verdict(transcript: str, model: str) -> VerifierResult:
    """v1 verdict: report_result STATUS, gated by an evidence tripwire."""
    parser = TranscriptParser(transcript)
    status = parser.reported_status()  # "PASS" | "FAIL" | "BLOCKED" | None

    observations = len(parser.observation_texts())
    device_calls = len(parser.successful_device_events())
    evidence_attached = observations >= 1 and device_calls >= 1

    reported_pass = status == "PASS"
    passed = reported_pass and evidence_attached

    criteria = {
        "reported_pass": reported_pass,
        "evidence_attached": evidence_attached,
    }

    reasons: list[str] = []
    if status is None:
        reasons.append("agent never called mobile_report_result")
    elif not reported_pass:
        reasons.append(f"agent reported {status}")
    if reported_pass and not evidence_attached:
        reasons.append(
            f"PASS with no device evidence (observations={observations}, "
            f"device_calls={device_calls})"
        )

    # Routine usage + device counts for the efficiency comparison.
    routine_events = parser.routine_events()
    metrics = {
        "device_tool_calls": device_calls,
        "observations": observations,
        "reported_status": status or "NONE",
        "routine_find_calls": sum("find_routine" in e.name for e in routine_events),
        "routine_apply_calls": sum("apply_routine" in e.name for e in routine_events),
    }

    # The whole cost/token block, built in one place (`pricing.usage_metrics`) so an
    # unmeasured episode reports "unavailable" instead of a $0.00 that reads as free.
    metrics.update(pricing.usage_metrics(model, parser.token_usage()))

    return VerifierResult(
        passed=passed,
        score=1.0 if passed else 0.0,
        weighted_score=1.0 if passed else 0.0,
        criteria=criteria,
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


def _find_aapt() -> str | None:
    """Locate aapt2/aapt: PATH first, then $ANDROID_HOME/$ANDROID_SDK_ROOT build-tools."""
    for tool in ("aapt2", "aapt"):
        found = shutil.which(tool)
        if found:
            return found
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if not root:
            continue
        bt = Path(root) / "build-tools"
        if not bt.is_dir():
            continue
        for ver_dir in sorted(bt.iterdir(), reverse=True):  # newest build-tools first
            for tool in ("aapt2", "aapt"):
                cand = ver_dir / tool
                if cand.exists():
                    return str(cand)
    return None


def apk_package_name(apk_path: Path) -> str | None:
    """Extract the Android package name from an APK via aapt, or None if unavailable."""
    aapt = _find_aapt()
    if not aapt:
        return None
    try:
        out = subprocess.run(
            [aapt, "dump", "badging", str(apk_path)],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("aapt dump badging failed: %s", exc)
        return None
    m = re.search(r"package: name='([^']+)'", out)
    return m.group(1) if m else None


async def prepare_app(
    session: DeviceSession,
    device: str,
    apk_path: Path,
    task: BenchmarkTask,
) -> str:
    """Install the app once and return its bundle_id. Resolution: explicit
    task.bundle_id → aapt on the APK → before/after package diff. Raises if none works."""
    platform = task.platform
    bundle_id = task.bundle_id or (apk_package_name(apk_path) if platform == "android" else None)

    # Pin the build the episode actually ran against. Without it a bundle proves an agent
    # explored *an* app — not that the app carried the seeded bugs it is scored on.
    if isinstance(task.bug_spec, dict):
        try:
            task.bug_spec["apk_sha256"] = hashlib.sha256(apk_path.read_bytes()).hexdigest()
        except OSError as exc:
            logger.warning("could not hash %s: %s", apk_path, exc)

    if platform == "android":
        before = set(await session.list_installed_apps(device, "android"))
        await session.setup_app(device, apk_path, bundle_id=bundle_id or "")
        if not bundle_id:
            after = set(await session.list_installed_apps(device, "android"))
            new = after - before
            if len(new) == 1:
                bundle_id = next(iter(new))
    else:
        await session.setup_app(device, apk_path, bundle_id=bundle_id or "")

    if not bundle_id:
        raise RuntimeError(
            f"Could not determine the bundle id for {task.app_name!r}. "
            "Install aapt (Android SDK build-tools) or pass the bundle id explicitly."
        )
    return bundle_id


def _inherited_instructions(path: Path) -> list[str]:
    """Why an agent with cwd under `path` would inherit instructions; raises unless
    `--allow-runs-in-repo` was given, in which case the reasons are returned for
    provenance. Both coding agents load CLAUDE.md / AGENTS.md from their cwd's
    ancestors as start-up context, which no transcript scan can see (QUA-2778).
    `run` refuses such a runs dir up front; this covers a bare run_episode call and
    a file dropped above the workspace mid-run."""
    problems = runs_dir_problems(path)
    if problems and not allow_runs_in_repo():
        raise RuntimeError(
            f"refusing to start an agent under {path}: " + "; ".join(problems)
            + " — move --runs-dir out of the repo (QUA-2778)")
    return problems


async def run_episode(
    task: BenchmarkTask,
    opts: EpisodeOptions,
) -> RunResult:
    """Run one benchmark episode; returns a RunResult (also written to disk).
    Assumes the app is installed and ``task.bundle_id`` is set — the CLI calls
    ``prepare_app`` once before the per-trial loop."""
    if not task.bundle_id:
        raise RuntimeError(
            "task.bundle_id is not set — call prepare_app before run_episode."
        )
    # Before the device is touched: a runs dir whose workspaces would inherit
    # instructions can never produce a clean episode (QUA-2778).
    _inherited_instructions(opts.runs_dir)
    # In the raw arm there is no MCP server anywhere; the session works adb-only.
    session = DeviceSession(opts.mcp_server)
    bundle_id = task.bundle_id

    # ── 1. Device + clean state ─────────────────────────────────────────────
    device_serial = opts.device_serial or await session.first_available_device()
    if not device_serial:
        raise RuntimeError(
            "No device found. Connect an Android emulator/device or iOS Simulator, "
            "then retry. Run: qualgent-bench doctor"
        )
    # Stash the serial so the clean-task oracle targets THIS device.
    if task.bug_spec is not None:
        task.bug_spec["device_serial"] = device_serial
        # The verdict applies the condition's evidence rule (raw = adb-in-Bash).
        task.bug_spec["tooling"] = "mcp" if opts.mcp_server else "raw"
    await session.force_release(device_serial)  # clear only THIS device's stale lock
    await session.check_device_available(device_serial)
    # This episode's `dump_stats` (provenance) counts this episode's dumps only.
    reset_dump_source(device_serial)
    # Reinstall per trial: pm clear leaves whatever the previous trial installed, so
    # trials 2..N were not provably on trial 1's build. Best-effort — fall through to
    # the clear rather than lose the episode.
    if opts.apk_path and task.platform == "android":
        try:
            await session.setup_app(device_serial, opts.apk_path, bundle_id=bundle_id)
        except (RuntimeError, OSError) as exc:
            logger.warning("clean reinstall of %s failed (%s) — falling back to pm clear",
                           bundle_id, exc)
    await session.reset_app(device_serial, bundle_id, task.platform)
    # pm clear revokes permissions and wipes files/, so normalisation and bug flags
    # must be re-applied after every reset (the flag shim caches on first read).
    if task.platform == "android":
        try:
            await normalize_app_env(device_serial, bundle_id)
            # Wipe before staging, or the wipe would delete what device_setup pushed.
            await wipe_shared_storage(device_serial, (task.bug_spec or {}).get("shared_storage"))
            await run_device_setup(device_serial, (task.bug_spec or {}).get("device_setup"))
        except DeviceSetupError as exc:
            # The seeded start state cannot exist here (missing asset in the image).
            # Record it so the scorer classifies env_failure instead of an agent 0.
            logger.error("device_setup failed for %s: %s", bundle_id, exc)
            if task.bug_spec is not None:
                task.bug_spec["staging_failed"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - never fail an episode on this
            logger.warning("env normalisation failed for %s: %s", bundle_id, exc)
    await write_bug_flags(device_serial, bundle_id, task.bug_spec)
    if task.platform == "android":
        # The previous episode's post-agent reads (and its replay verification) may have
        # left a uiautomator2 server behind; that is the harness's own leftover, not a
        # dirty device, so it is stopped before the invariant below reads the device.
        await stop_u2_server(device_serial)
    await isolate_app_under_test(device_serial, bundle_id)
    device_clean = True
    if task.platform == "android":
        # Episode-start invariant (QUA-2781): staging has reset rotation, root and the
        # clock, and isolation has just sent HOME — read it all back before the launch.
        # Read-only, so HOME stays the last device ACTION before the launch.
        device_clean = await _refuse_dirty_device(device_serial, task,
                                                  stage="episode start",
                                                  expect_launcher=True)
    await session.launch_app(device_serial, bundle_id)
    if task.platform == "android":
        # normalize_app_env pinned portrait with the launcher in front, and that pin
        # does not survive this launch. After an episode that left the app stopped in
        # landscape, the app comes up landscape (QUA-2731's pre-check, QUA-2734). So
        # pin again now, with the app in front, through the helper the replay `launch`
        # step uses. The snapshot relaunches below then start from portrait.
        await repin_portrait_after_launch(device_serial, bundle_id)

    # ── 2. Run directory + MCP config (direct bridge, no sidecar) ───────────
    # The arm ("raw" | "mcp") is recorded as the run's condition so the leaderboard
    # can pair them.
    cond_label = opts.condition_label or ("mcp" if opts.mcp_server else "raw")
    # Blind (QUA-2806): the directory — the agent's cwd and every path beside it —
    # names the case and an opaque episode id, never the version. See
    # `agent_visible_task_id`.
    episode_id = new_episode_id()
    visible_task_id = agent_visible_task_id(task.id)
    run_name = _run_dir_name(visible_task_id, opts.agent, opts.model, cond_label,
                             opts.trial, episode_id=episode_id)
    run_dir = (opts.runs_dir / visible_task_id / run_name).resolve()
    workspace_dir = run_dir / "workspace"
    # The agent's cwd, re-checked now that its full path is known: a file dropped
    # into <runs>/<task>/ since the up-front check would be inherited too.
    inherited = _inherited_instructions(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verifier").mkdir(parents=True, exist_ok=True)  # for ctrf.json
    # Identity FIRST, before anything can kill the episode: an episode dir with a
    # marker and no result.json is a provable orphan of a known run; without it the
    # same dir is indistinguishable from one that never started.
    # The full identity is written harness-side; the in-dir marker names the case only.
    write_blinded_marker(
        run_dir,
        opts.runs_dir,
        visible_task_id=visible_task_id,
        run_id=opts.run_id,
        app_id=opts.app_id or str((task.bug_spec or {}).get("app_id") or ""),
        task_id=task.id,
        kind=opts.task_type,
        trial=opts.trial,
        attempt=opts.attempt,
        segment=opts.segment,
        episode_id=episode_id,
    )
    if callable(opts.on_run_dir):
        try:
            opts.on_run_dir(run_dir)
        except Exception:  # noqa: BLE001 - a display hook must never fail a run
            logger.debug("on_run_dir callback raised", exc_info=True)

    # Both conditions get the same buggy source checkout as cwd (per-trial copy).
    if opts.source_dir:
        shutil.copytree(opts.source_dir, workspace_dir, dirs_exist_ok=True)

    # The step unit is one interaction, counted at two harness proxies BELOW the
    # agent (ADB meter + MCP meter) — a new coding agent needs no counting code.
    # The ADB meter runs in BOTH arms: direct adb from the mcp agent is real work.
    interaction_log = InteractionLog(run_dir / "interactions.json")
    interaction_log.flush()
    meter = AdbMeter(run_dir / "adb_counts.json", log=interaction_log)
    meter_port = await meter.start()
    # No MCP server in the raw arm — the ADB meter above is that arm's counter.
    mcp_meter = McpMeter(interaction_log, opts.mcp_server) if opts.mcp_server else None
    if mcp_meter is not None:
        await mcp_meter.start()

    if not opts.mcp_server:
        # Raw: an empty config, passed with --strict-mcp-config — no MCP servers at all.
        mcp_cfg: dict = {"mcpServers": {}}
    else:
        # Point the agent at the METER, not the server: same protocol, but every
        # tool call is counted at a boundary the agent cannot route around.
        mcp_cfg = _generate_mcp_config(mcp_meter.url(""))
    mcp_config_path = run_dir / "mcp_config.json"
    mcp_config_path.write_text(json.dumps(mcp_cfg, indent=2))

    instruction = _ablation_instruction(
        task, device_serial, "mcp" if opts.mcp_server else "raw")
    (run_dir / "instruction_sent.md").write_text(instruction)

    # Enforced per-episode step budget (see _step_budget).
    step_cap = _step_budget(task)

    context = RunContext(
        task=_TaskShim(task, max_tool_calls=step_cap),  # adapters read .agent.timeout_sec etc. (see shim)
        agent=opts.agent,
        model=opts.model,
        condition=opts.condition,
        trial=opts.trial,
        run_dir=run_dir,
        # The native adapter builds its MCP URL from this field; pointing it at the
        # meter puts native on the same step unit as the CLI agents.
        mcp_server="" if mcp_meter is None else mcp_meter.url(""),
        mcp_config_path=mcp_config_path,
        workspace_dir=workspace_dir,
        # Where run-scoped adapter telemetry goes (the shared usage windows the
        # credit guard reads). None for a bare run_episode call with no run id.
        run_meta_dir=(run_meta_dir(opts.runs_dir, opts.run_id) if opts.run_id else None),
        disabled_tools=_disabled_tools(),
        # Claude reuses an existing MCP server; Codex renders this config
        # into isolated CODEX_HOME. Both paths expose one benchmark MCP surface.
        inject_mcp=False,
        no_mcp=not opts.mcp_server,
        # Strict MCP config, so the condition measures MCP rather than the user's
        # whole global claude-code environment.
        isolate_mcp=bool(opts.mcp_server),
        force_model=opts.force_model,
        # The PreToolUse hook denies tool calls past the cap but leaves the agent
        # running, so it still reports from what it saw — truncated, never voided.
        # qg_release_device stays exempt so the device lock is always returned.
        tool_call_cap=step_cap,
        # Route the agent's adb through the meter, in BOTH arms, so the two are
        # measured identically. The meter listens on loopback, so the address is
        # pinned too — the harness itself may be pointed at a remote adb server
        # (ANDROID_ADB_SERVER_ADDRESS) and the agent must not inherit that.
        agent_env={
            "ANDROID_ADB_SERVER_PORT": str(meter_port),
            "ANDROID_ADB_SERVER_ADDRESS": "127.0.0.1",
            "ANDROID_ADB_SERVER_HOST": "127.0.0.1",
        },
    )

    try:
        await take_replay_snapshots(device_serial, bundle_id, run_dir, task.bug_spec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not snapshot app data for replay: %s", exc)

    # Assert the episode's precondition on the screen the agent is about to be handed.
    # AFTER the snapshot, for two reasons: this is the screen the agent actually gets
    # (the snapshot's own launch is force-stopped again), and a `uiautomator dump`
    # writes /sdcard/qgb_vh.xml, which has no business inside the cold tar. Read-only:
    # no taps, so the handed-over screen is the one take_replay_snapshots left. And it
    # costs the agent nothing — the meters sit on the agent's adb socket and the MCP
    # server, while harness adb goes straight to the upstream server.
    precondition = "skipped"
    if task.bug_spec is not None:
        precondition = await assert_precondition(device_serial, task.bug_spec)
    # A MISSING precondition has already excluded this episode (`staging_failed` →
    # `env_failure`), so an agent launched now is paid for an outcome every board
    # discards before it exists — $3.36 on QUA-2731's first episode, against an app
    # that was not in the state its brief assumed (QUA-2743). End the episode here:
    # no agent, no frames, no post-agent device reads. The verdict below still runs,
    # on an empty transcript, so the result.json carries the same `staging_failed`
    # record and the same exclusion as before. Only "missing" stops it — "unknown"
    # (an unreadable screen) never kills an episode, and a `DeviceSetupError` keeps
    # its old path, because not every scorer excludes one (`guided_bug_verdict`).
    # A device that failed the episode-start invariant stops the agent the same way.
    agent_launched = precondition != "missing" and device_clean

    # Hand the device over with its UiAutomation slot FREE. Android registers one
    # UiAutomation client per device, and uiautomator2's server holds it while it runs:
    # the harness's own reads start one whenever the built-in dump fails, and the
    # DevLoop MCP server starts one too. Left running, it made every agent-side
    # `uiautomator dump` on QUA-2731's board die with exit 137, 0 of 371 (QUA-2741). So
    # it is stopped here, after the last staging read and before the agent starts. The
    # harness's own post-agent reads may start it again; the agent has exited by then.
    u2_stopped = await stop_u2_server(device_serial) if task.platform == "android" else []
    handoff: dict = {}
    if task.platform == "android" and agent_launched:
        # The same invariant at the hand-off, minus the launcher (the app is in front
        # now): what staging did after the launch (the re-pin, the snapshot relaunches,
        # the precondition read) must not have left the device rotated, rooted, holding
        # the UiAutomation slot or off the pinned clock.
        agent_launched = await _refuse_dirty_device(device_serial, task,
                                                    stage="agent hand-off",
                                                    expect_launcher=False,
                                                    observed=handoff)

    # Which MCP server the agent is about to be handed, read the way its own session
    # will read it (QUA-2806). Refused — like a dirty device, before the agent and its
    # cost — when it is a DevLoop server outside no-source mode, or not the server the
    # run was planned against (restarted in another mode, updated, swapped).
    server_identity = (await fetch_server_identity(opts.mcp_server)
                       if opts.mcp_server else None)
    if server_identity is not None and agent_launched:
        refusal = _server_refusal(server_identity, opts.mcp_server_identity)
        if refusal:
            logger.error("%s on %s — %s", task.id, device_serial, refusal)
            if task.bug_spec is not None:
                task.bug_spec.setdefault("staging_failed", refusal)
                agent_launched = False
            else:
                logger.error("%s has no bug_spec to record the refusal in; running it "
                             "anyway", task.id)

    # ── 6. Launch the agent ─────────────────────────────────────────────────
    adapter = get_adapter(opts.agent)
    started_at = datetime.now(timezone.utc)
    if agent_launched:
        logger.info(
            "starting agent '%s' for case '%s' (%s, trial %d)",
            opts.agent, task.id, opts.condition.value, opts.trial,
        )
        # Opens the window the post-agent crash diagnostic reads; never raises.
        crash_since = await crash_window(device_serial)
    else:
        logger.error("NOT starting agent '%s' for case '%s' (%s, trial %d): its "
                     "staging failed (precondition, device state or MCP server), so the "
                     "episode is excluded whatever it does",
                     opts.agent, task.id, opts.condition.value, opts.trial)
    try:
        if agent_launched:
            # Frames are captured out-of-band — the agent's tools, context and budget
            # are untouched.
            async with FrameCapture(run_dir, device_serial):
                transcript, exit_code = await adapter.run(instruction, context)
        else:
            transcript, exit_code = "", 0
    except (asyncio.CancelledError, KeyboardInterrupt):
        await meter.stop()
        if mcp_meter is not None:
            await mcp_meter.stop()
        await session.force_release(device_serial)
        await asyncio.sleep(1.0)
        raise
    finally:
        meter_counts = (await meter.stop()).as_metrics()
        if mcp_meter is not None:
            await mcp_meter.stop()
        meter_counts.update(interaction_log.as_metrics())
    ended_at = datetime.now(timezone.utc)
    await session.force_release(device_serial)
    # Did the agent leave adbd rooted (or primed to come back root)? Read first, before
    # any harness read-back, over the harness's own adb (QUA-2795).
    adbd_at_end = (await check_adbd_after_agent(device_serial)
                   if task.platform == "android" and agent_launched else None)
    # Did every MCP session that touched this device during the agent's run start
    # from clean server state (QUA-2800)? Read before the next episode's setup
    # opens a session of its own.
    mcp_isolation = (await fetch_episode_isolation(
        opts.mcp_server, device=device_serial, since=started_at.isoformat(),
        until=ended_at.isoformat()) if opts.mcp_server and agent_launched else None)

    # Record WHY the episode stopped — a 0-because-slow must stay distinguishable
    # from a 0-because-wrong or the leaderboard stops being interpretable.
    if task.bug_spec is not None:
        task.bug_spec["truncated"] = (run_dir / "truncated").exists()
        # Timeout is a different stop reason from budget exhaustion: truncated means
        # the steps were spent (fair, scored on partial evidence); a timeout stopped
        # the agent with steps left — an environment limit, not a QA failure.
        task.bug_spec["timed_out"] = (run_dir / "timed_out").exists()
        # A non-zero exit alone does not void an episode — the scorer pairs it with
        # "banked nothing" (see env_failure).
        task.bug_spec["exit_code"] = exit_code
        task.bug_spec["step_cap"] = step_cap   # denominator for the earliness credit
        # The only directory this episode may touch; the contamination scan
        # classifies every other host path against it.
        task.bug_spec["workspace"] = str(run_dir / "workspace")
        # A DevLoop-MCP server's artifact roots — the documented defaults plus what
        # the server reported — where any file it did not hand THIS agent is another
        # episode's (QUA-2800). Both arms: one server can serve other lanes' episodes.
        task.bug_spec["devloop_roots"] = devloop_default_roots() + list(
            (mcp_isolation or {}).get("artifact_roots") or [])
        # adbd's privilege after the agent: `rooted` is a hard contamination hit in the
        # scan every scorer runs (QUA-2806).
        task.bug_spec["adbd_at_end"] = adbd_at_end
        # Device operations as counted at the ADB socket, plus the interaction split.
        task.bug_spec.update(meter_counts)
        # Read the submission as it finally stands on disk — Edit-appended fragments
        # in the transcript do not parse standalone. Can rescue an unbanked area,
        # never overwrite an evidence-ordered verdict.
        findings_path = run_dir / "workspace" / submission.FILENAME
        try:
            task.bug_spec["findings_file"] = findings_path.read_text()
        except OSError:
            task.bug_spec["findings_file"] = ""
        # Nothing ran when the agent was not launched, so there is nothing on the device
        # to read back: no end screen, no crash, no fired marker, no oracle. A `stuck:`
        # oracle would even send its probe tap to an app nobody used.
        if agent_launched:
            # Journey mode: the completion oracle. A `db` outcome is read off the device
            # now, after the agent exited — the agent's report is never the proof that the
            # steps were executed. Skipped for a blocked version, where the outcome fails
            # by design and completion is judged on the verdict and the blocking bug.
            # Where the agent ENDED — a retrospective wander detector. An episode that
            # finished in another app once looked entirely clean without this. Read before
            # the oracle, which may stop the app.
            try:
                ended_in = await foreground_package(device_serial)
            except Exception:  # noqa: BLE001 - never fail an episode on a diagnostic
                ended_in = ""
            task.bug_spec["ended_in_package"] = ended_in
            task.bug_spec["off_app"] = bool(ended_in and bundle_id and ended_in != bundle_id)
            # Did the app under test die while the agent drove it? Recorded, not scored.
            await _record_app_crashes(device_serial, bundle_id, crash_since, task.bug_spec)
            # Which seeded paths reported themselves (`verify.canary`) — read by the
            # harness over its own adb, never visible to the agent.
            await _record_fired(device_serial, bundle_id, task.bug_spec)
            if str(task.bug_spec.get("mode") or "") == "journey":
                await _journey_oracle(device_serial, bundle_id, task.bug_spec)
            if task.bug_spec["off_app"]:
                logger.warning("episode '%s' ENDED IN %s, not the app under test (%s) — "
                               "its verdicts describe the wrong app",
                               task.id, ended_in, bundle_id)
        # The hook's own counter is the only number in the budget's unit — the
        # transcript undercounts, since blocked attempts and retries still spend budget.
        for counter in run_dir.rglob("hooks/count"):
            try:
                task.bug_spec["hook_steps"] = int(counter.read_text().strip())
            except (OSError, ValueError):
                pass
            break
        if task.bug_spec["truncated"]:
            logger.warning("episode '%s' hit its %d-call budget and was terminated",
                           task.id, step_cap)

    # ── 7. Verdict + result ─────────────────────────────────────────────────
    # Prefer the model the transcript reports over the requested label.
    actual_model = TranscriptParser(transcript).model() or opts.model
    if opts.verdict_fn is not None:
        verifier = opts.verdict_fn(transcript, actual_model, task)
    else:
        verifier = _verdict(transcript, actual_model)
    # A provider limit that stopped the episode is not a QA result; the scheduler
    # requeues it and every board excludes it.
    from .failures import classify as _classify_failure
    verifier.metrics["failure_class"] = _classify_failure(
        transcript, exit_code, verifier.metrics,
        # The adapter watched the provider reject the request and killed the agent;
        # the transcript's structured event matches no prose pattern.
        rejected=(run_dir / RATE_LIMITED_SENTINEL).exists())
    if not agent_launched:
        # Nobody ran, so nobody spent: a known $0, not the "unavailable" an empty
        # transcript would otherwise print as "cost unknown, not $0" in the footer.
        verifier.metrics.update({"agent_launched": False, "cost_usd": 0.0,
                                 "cost_source": pricing.COST_NOT_LAUNCHED})
    # An MCP session that did not start clean is an environment failure, excluded
    # like one; a server with no record is flagged (`failures.mcp_integrity`, QUA-2806).
    from .failures import mcp_integrity
    verifier.metrics.update(mcp_integrity(
        mcp_isolation, (server_identity or {}).get("name")))
    if (task.bug_spec or {}).get("mode") == "journey":
        # Which corpus this episode was scored against, and whether the app was public
        # or held out: `corpus_version` / `heldout_version` / `heldout` in result.json's
        # metrics. A rescore keeps them (it merges over the old metrics), so the value
        # stays the one the episode was RECORDED under — `rescore_journey.py` prints it
        # beside the current one. Journey only: the hash covers the journey corpus.
        from .corpus import episode_stamp
        verifier.metrics.update(episode_stamp(str(task.bug_spec.get("app_id") or "")))
    result = RunResult.build(
        task_id=task.id,
        task_version="qgb-v1",
        task_type=opts.task_type,
        agent=opts.agent,
        model=actual_model,
        condition=cond_label,
        trial=opts.trial,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=exit_code,
        verifier=verifier,
        artifact_dir=run_dir,
        runs_dir=opts.runs_dir,
        run_id=opts.run_id,
        provenance=await _provenance(opts, device_serial, u2_stopped=u2_stopped,
                                     inherited=inherited, adbd_at_end=adbd_at_end,
                                     mcp_isolation=mcp_isolation,
                                     server_identity=server_identity,
                                     episode_id=episode_id, handoff=handoff),
    )
    result.write(run_dir / "result.json")
    result.write_ctrf(run_dir / "verifier" / "ctrf.json")
    _write_evidence(run_dir, transcript, task, opts, result,
                    device_serial=device_serial, bundle_id=bundle_id,
                    step_cap=step_cap)
    logger.info("episode complete — passed=%s status=%s",
                result.passed, verifier.metrics.get("reported_status"))
    return result


def _write_evidence(
    run_dir: Path,
    transcript: str,
    task: BenchmarkTask,
    opts: EpisodeOptions,
    result: RunResult,
    *,
    device_serial: str,
    bundle_id: str,
    step_cap: int,
) -> None:
    """Write the episode's audit bundle (steps + screenshots) beside result.json.
    Never fatal: the episode is already scored, so a failure here costs auditability."""
    spec = task.bug_spec or {}
    try:
        write_episode_evidence(
            run_dir,
            transcript,
            meta={
                "task_id": task.id,
                "task_type": opts.task_type,
                "app": task.app_name,
                "bundle_id": bundle_id,
                "agent": opts.agent,
                "model": result.model,
                "condition": result.condition,
                "trial": opts.trial,
                "device": device_serial,
                "started_at": result.started_at,
                "ended_at": result.ended_at,
                "wall_time_sec": result.wall_time_sec,
                "passed": result.passed,
                "score": result.score,
                "weighted_score": result.weighted_score,
                "reported_status": result.metrics.get("reported_status"),
                "step_budget": step_cap,
                # Where the budget went — the breakdown is what makes an exhausted
                # budget self-explaining instead of a mystery.
                "interactions": {k: v for k, v in result.metrics.items()
                                 if k == "interactions" or k.startswith("interactions_")},
                "apk_sha256": spec.get("apk_sha256"),
                "active_bugs": spec.get("active_bugs_written"),
                "truncated": spec.get("truncated"),
                "timed_out": spec.get("timed_out"),
                "off_app": spec.get("off_app"),
                "ended_in_package": spec.get("ended_in_package"),
                # Crashes seen while the agent ran, app-class first (diagnostic).
                "app_crashes": spec.get("app_crashes"),
                # Seeded-site markers after the agent exited (attribution canary).
                "fired": spec.get("fired"),
            },
            secrets=(),
            # Hunt only: the per-bug index is keyed by the area list, and reports
            # these metrics rather than recomputing them.
            features=spec.get("features"),
            metrics=result.metrics,
        )
    except Exception as exc:  # noqa: BLE001 - auditability must not fail a scored run
        logger.warning("evidence bundle not written for %s: %s", task.id, exc)



async def _avd_name(serial: str) -> str | None:
    """`adb emu avd name` answers on emulators only; anything else is None."""
    if not serial.startswith("emulator-"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            os.environ.get("QGB_ADB_PATH") or "adb", "-s", serial, "emu", "avd", "name",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (OSError, asyncio.TimeoutError):
        return None
    first = out.decode(errors="replace").strip().splitlines()
    return first[0].strip() if first and first[0].strip() != "OK" else None


async def _provenance(opts: EpisodeOptions, device_serial: str, *,
                      u2_stopped: list[str] | None = None,
                      inherited: list[str] | None = None,
                      adbd_at_end: dict | None = None,
                      mcp_isolation: dict | None = None,
                      server_identity: dict | None = None,
                      episode_id: str = "",
                      handoff: dict | None = None) -> dict:
    """Where the episode ran, and how the harness read its screens. Recorded beside
    every score so a board built from parallel lanes (or a container) can be audited;
    never read by a scorer."""
    from .adb_meter import upstream_from_env
    import platform

    host, port = upstream_from_env()
    return {
        "device_serial": device_serial,
        "avd_name": await _avd_name(device_serial),
        "lane": opts.lane,
        "lanes": opts.lanes,
        "attempt": opts.attempt,
        # Which sitting of the run produced this episode (0 = the original).
        "segment": opts.segment,
        "adb_server": f"{host}:{port}",
        "host_os": platform.system().lower(),
        # Same reader plan.json's fingerprint uses — two readers of QGB_IMAGE_DIGEST
        # would be free to drift, and a resume compares these two values.
        "image_digest": image_digest(),
        # Which brief this agent was given (`brief.BRIEF_VERSION`). The brief is part
        # of the treatment, so an episode has to say which regime it belongs to:
        # v1 episodes and v2 episodes are not directly comparable, and without this a
        # board blends them with nothing to sort on.
        "brief_version": _brief.BRIEF_VERSION,
        # Which source served each of the harness's own hierarchy dumps this episode
        # (`verify.device.dump_stats`: builtin / u2 / none, plus `builtin_killed`, the
        # built-in attempts SIGKILLed because the UiAutomation slot was held), and the
        # uiautomator2 server PIDs stopped before the agent started. The holder is
        # almost always the harness's OWN uiautomator2 server, not another client: its
        # fallback reader and its `type` step start one, the server outlives the read
        # (and the replay subprocess that started it), and every built-in dump after
        # that is killed until `stop_u2_server` runs — the TODO in `_dump_vh_raw` has
        # the measurement and QUA-2769 the fix. So `builtin_killed` here costs time,
        # not a verdict, and is no reason to hunt for a stranger on the device; `u2`
        # with no `builtin_killed` is a screen that never reported idle. Whether the
        # AGENT got the slot is `u2_stopped` plus the board's agent-dump preflight
        # (QUA-2741).
        "dump_stats": dump_stats(device_serial),
        "u2_stopped": list(u2_stopped or []),
        # The instant staging set the device clock to (`pin_device_clock`, QUA-2781).
        # Episodes run under different pins met different date and time strings, and
        # the journey truth was derived under the default one; this is how to tell.
        "device_clock": device_clock_pin().isoformat(),
        # Why the agent's cwd would inherit instructions (a CLAUDE.md / AGENTS.md on
        # its ancestor chain, or a workspace inside the repo). Always [] unless the run
        # was started with --allow-runs-in-repo, which makes the episode contaminated
        # (QUA-2778).
        "inherited_instructions": list(inherited or []),
        # adbd's privilege when the agent exited (`check_adbd_after_agent`, QUA-2795):
        # `rooted` / `root_primed` true means an agent-side privilege change got past
        # the adb meter. None when no agent ran on an Android device.
        "adbd_at_end": adbd_at_end,
        # MCP arm only (QUA-2800): the DevLoop server's record of every client session
        # that touched this device during the agent's run — `clean` is true when the
        # server scopes state per session and each one started with none (no earlier
        # episode's action log, baselines, traces, recordings). `isolation:
        # unavailable` = a server without the record (an older DevLoop, another MCP
        # server). More than one session = the agent reconnected mid-episode. None on
        # the raw arm or when no agent ran.
        "mcp_isolation": mcp_isolation,
        # Which MCP server the agent was handed (QUA-2806): name, the version its
        # `initialize` reported, the app-source mode, and sha256s of the instructions
        # and of the tool set it served — `checkpoint.server_stamp` of
        # `session.fetch_server_identity`, the same stamp plan.json's environment
        # carries. `error` when the server could not be read. None on the raw arm.
        "mcp_server": _server_provenance(server_identity),
        # The opaque id the episode's directory is named by (QUA-2806); the version it
        # stands for is this result.json's `task_id`.
        "episode_id": episode_id,
        # The device clock at the agent hand-off, minus the pin, and the tolerance it
        # was checked against (`clock_tolerance_s`). None when the hand-off check did
        # not run (no agent, not Android).
        "device_clock_offset_s": (handoff or {}).get("clock_offset_s"),
        "device_clock_tolerance_s": (handoff or {}).get("clock_tolerance_s"),
    }


def _server_provenance(identity: dict | None) -> dict | None:
    if identity is None:
        return None
    out = server_stamp(identity)
    if identity.get("error"):
        out["error"] = identity["error"]
    return out


def _server_refusal(identity: dict, planned: dict | None) -> str | None:
    """Why the agent must not be handed this MCP server, or None. A DevLoop server
    outside no-source mode (QUA-2787's `--app-source none`), and — when the run
    recorded one — any server that is not the one it was planned against."""
    if identity.get("error"):
        return f"MCP server identity could not be read: {identity['error']}"
    if identity.get("name") == DEVLOOP_SERVER_NAME and identity.get("app_source") != NO_SOURCE:
        return (f"DevLoop-MCP is not in no-source mode (app source: "
                f"{identity.get('app_source')})")
    if planned is not None:
        changes = identity_changes(server_stamp(planned), server_stamp(identity))
        if changes:
            return "MCP server changed since the run was planned: " + "; ".join(changes)
    return None


def _step_budget(task: BenchmarkTask) -> int:
    """Per-episode tool-call cap: hunt → bug_spec['step_budget'], guided →
    max(50, 10 × optimal_steps), plain episode → 70."""
    spec = task.bug_spec or {}
    if spec.get("step_budget"):
        return int(spec["step_budget"])
    opt = spec.get("optimal_steps")
    if opt:
        return max(50, int(opt) * 10)
    return 70  # cap a model that loops without reporting


class _TaskShim:
    """Adapt a BenchmarkTask to the minimal surface adapters/base touch:
    ``context.task.agent.timeout_sec`` and ``max_tool_calls``."""

    class _Agent:
        # 3000s is a safety net against a wedged agent, not a budget — a lower
        # ceiling stopped slow models mid-run and ranked them by token latency
        # instead of testing ability.
        def __init__(self, timeout_sec: int = 3000, max_tool_calls: int = 150) -> None:
            self.timeout_sec = timeout_sec      # per-episode wall-clock safety net
            self.max_tool_calls = max_tool_calls  # per-episode tool-call budget (see _step_budget)

    def __init__(self, task: BenchmarkTask, max_tool_calls: int | None = None) -> None:
        self._task = task
        cap = max_tool_calls if max_tool_calls is not None else _step_budget(task)
        self.agent = self._Agent(max_tool_calls=cap)
        self.id = task.id
