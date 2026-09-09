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
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import pricing, submission
from .adapters import get_adapter
from .adb_meter import AdbMeter
from .interactions import InteractionLog
from .mcp_meter import McpMeter
from .replay import snapshot as replay_snapshot
from .replay import snapshot_shared
from .verify.device import relaunch as _relaunch_app, wait_stable
from .adapters.base import RunContext
from .task import BenchmarkTask
from .episode_evidence import write_episode_evidence
from .frame_capture import FrameCapture
from .result import RunResult, VerifierResult
from .schemas import Condition
from .session import DeviceSession
from .transcript import TranscriptParser


def run_dir_name(task_id: str, agent: str, model: str, condition: str, trial: int) -> str:
    """Canonical run-directory name, shared by every mode so run dirs sort uniformly."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    model_short = model.split("/")[-1]  # strip org prefix if present
    return f"{ts}_{task_id}_{agent}_{model_short}_{condition}_trial-{trial}"


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

    if tooling == "raw":
        tooling_note = (
            "Use the tools available in your environment to operate the device "
            "(for example the `adb` command line)."
        )
        report_note = ""
    else:
        # Mechanics only, no verification guidance — the raw arm gets none, and
        # coaching one arm to check its work breaks the ablation. The standalone
        # server has no device-lock tools, so every call carries the device explicitly.
        tooling_note = (
            "MCP tools are available for device control. Every tool takes the "
            f'device as its first argument — always pass device="{device_serial}".'
        )
        # No report note: both arms use the findings.yaml contract, and a completion
        # nudge here was the last asymmetry between them.
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
# emulator inherits the HOST zone — openScale's "Aug 29, 2026 7:00 AM" anchors only
# exist in one zone. The harness pins the zone itself so a run is identical on any
# host. The value is the zone the answer keys were derived in; override only when
# re-deriving the whole corpus.
DEVICE_TIMEZONE = os.environ.get("QGB_DEVICE_TIMEZONE") or "America/Chicago"


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


async def run_device_setup(device: str, spec_setup: dict | None) -> None:
    """Stage the spec's `device_setup:` content (pushes + shell) after pm clear and
    before launch — media apps are untestable on a fresh emulator. Content is fixed
    and named so the oracle stays deterministic. Transient adb hiccups stay
    best-effort; a MISSING push source raises DeviceSetupError (see above).
    Always pins the device timezone first, even for a spec without `device_setup:`."""
    await pin_device_timezone(device)
    if not spec_setup:
        return
    repo_root = Path(__file__).resolve().parents[2]
    if spec_setup.get("root"):
        # `adb root` (emulator / userdebug only): needed to purge SYSTEM providers the
        # shell uid may not touch — e.g. the telephony store behind an SMS app.
        rc, out = await _adb("-s", device, "root")
        if rc != 0:
            logger.warning("device_setup: adb root failed: %s", out.strip()[:120])
        await _adb("-s", device, "wait-for-device")
    for item in spec_setup.get("push", []):
        src = (repo_root / str(item["src"])).resolve()
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
        await _adb("-s", device, "shell", str(cmd))
    # Emulator-console commands (`adb emu ...`): the only way to deliver an SMS or a
    # call INTO the device — the telephony providers refuse shell-uid inserts.
    for cmd in spec_setup.get("emu", []):
        rc, out = await _emu_console(device, str(cmd))
        if rc != 0 or "KO" in out:
            logger.warning("device_setup: emu %r failed: %s", cmd, out.strip()[:160])
    logger.info("device_setup: staged %d file(s), %d command(s), %d emu command(s)",
                len(spec_setup.get("push", [])), len(spec_setup.get("shell", [])),
                len(spec_setup.get("emu", [])))


async def normalize_app_env(device: str, bundle_id: str) -> None:
    """Re-grant permissions, allow the MANAGE_EXTERNAL_STORAGE app-op (`pm grant`
    cannot set it), zero animation scales. Runs after every reset: pm clear revokes
    grants, and the benchmark measures QA skill, not consent-dialog navigation."""
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
    """Force-stop every other benchmark app so a `back` press lands on the launcher,
    not in another seeded app the agent will happily keep testing. force-stop, not
    uninstall: it empties the task stack at one adb call per package."""
    from . import bugs as bugmod

    others = {
        str(s.get("app", {}).get("package") or "")
        for s in bugmod.load_apps()
    } - {bundle_id, ""}
    for pkg in sorted(others):
        await _adb("-s", device, "shell", "am", "force-stop", pkg)
    # Also drop anything else lingering in recents, so `back` cannot resurrect it.
    await _adb("-s", device, "shell", "am", "kill-all")
    logger.info("isolated %s on %s (cleared %d other benchmark app(s))",
                bundle_id, device, len(others))


async def take_replay_snapshots(device: str, bundle_id: str, run_dir: Path,
                                bug_spec: dict | None) -> None:
    """Snapshot app data COLD, then cold-launch for the agent. Relaunch + settle first
    (first launch seeds; an early tar catches an empty sandbox), force-stop before the
    tar (a running app misses unflushed state) — agent-start equals replay-start."""
    await _relaunch_app(device, bundle_id)
    await asyncio.sleep(3.0)
    await wait_stable(device)
    await _adb("-s", device, "shell", "am", "force-stop", bundle_id)
    ok = await replay_snapshot(device, bundle_id, run_dir / "app_snapshot.tar")
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


async def write_bug_flags(device: str, bundle_id: str, bug_spec: dict | None) -> None:
    """Activate exactly this episode's seeded bugs via files/qgb_flags.txt (run-as,
    debug build). Clean episodes get an empty file; pre-gate apps ignore it.
    Best-effort — a failed write leaves the legacy all-bugs-live behaviour."""
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

    # One id PER LINE — the shim reads with readLines(), and printf never expands
    # escapes inside a %s argument. Passing each id as its own argument to
    # `printf '%s\n'` puts the newline in the format, where it does expand.
    ids = " ".join(shlex.quote(a) for a in active)
    cmd = (f"run-as {shlex.quote(bundle_id)} sh -c "
           f"{shlex.quote(f'''mkdir -p files && printf '%s\\n' {ids} > files/qgb_flags.txt''')}")
    rc, out = await _adb("-s", device, "shell", cmd)
    if rc != 0:
        logger.warning("could not write bug flags for %s: %s", bundle_id, out.strip()[:200])
        return
    bug_spec["active_bugs_written"] = active
    logger.info("bug flags for %s → %s", bundle_id, active or "(none: clean episode)")


async def _journey_oracle(device: str, bundle_id: str, spec: dict) -> None:
    oracle = spec.get("oracle") or {}
    if oracle.get("mode") not in ("db", "content") or spec.get("blocking"):
        return
    from . import replay as rp
    expect, err = submission._parse_expect(oracle.get("expect"), str(spec.get("case_id")), trusted=True)
    if expect is None:
        spec["oracle_result"], spec["oracle_detail"] = "inconclusive", err or "unparseable expect"
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

    usage = parser.token_usage()
    reported_cost = usage.get("reported_cost_usd")
    cost = reported_cost if reported_cost is not None else pricing.compute_cost_usd(model, usage)
    metrics.update({
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cached_input_tokens": usage["cached_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": cost,
        "cost_source": "reported" if reported_cost is not None
                       else ("estimated" if cost is not None else "unknown"),
    })

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
    await isolate_app_under_test(device_serial, bundle_id)
    await session.launch_app(device_serial, bundle_id)

    # ── 2. Run directory + MCP config (direct bridge, no sidecar) ───────────
    # The arm ("raw" | "mcp") is recorded as the run's condition so the leaderboard
    # can pair them.
    cond_label = opts.condition_label or ("mcp" if opts.mcp_server else "raw")
    run_name = _run_dir_name(task.id, opts.agent, opts.model, cond_label, opts.trial)
    run_dir = (opts.runs_dir / task.id / run_name).resolve()
    workspace_dir = run_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verifier").mkdir(parents=True, exist_ok=True)  # for ctrf.json
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

    # ── 6. Launch the agent ─────────────────────────────────────────────────
    adapter = get_adapter(opts.agent)
    started_at = datetime.now(timezone.utc)
    logger.info(
        "starting agent '%s' for case '%s' (%s, trial %d)",
        opts.agent, task.id, opts.condition.value, opts.trial,
    )
    try:
        # Frames are captured out-of-band — the agent's tools, context and budget
        # are untouched.
        async with FrameCapture(run_dir, device_serial):
            transcript, exit_code = await adapter.run(instruction, context)
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
    verifier.metrics["failure_class"] = _classify_failure(transcript, exit_code, verifier.metrics)
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
        run_id=opts.run_id,
        provenance=await _provenance(opts, device_serial),
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


async def _provenance(opts: EpisodeOptions, device_serial: str) -> dict:
    """Where the episode ran. Recorded beside every score so a board built from
    parallel lanes (or a container) can be audited; never read by a scorer."""
    from .adb_meter import upstream_from_env
    import platform

    host, port = upstream_from_env()
    return {
        "device_serial": device_serial,
        "avd_name": await _avd_name(device_serial),
        "lane": opts.lane,
        "lanes": opts.lanes,
        "attempt": opts.attempt,
        "adb_server": f"{host}:{port}",
        "host_os": platform.system().lower(),
        "image_digest": os.environ.get("QGB_IMAGE_DIGEST") or None,
    }


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
