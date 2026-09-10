#!/usr/bin/env python3
"""Run QualGentBench from its Docker image against your own emulators.

    python scripts/launch.py bench.config.yaml [--yes] [--keep-emulators] [--pull]
    python scripts/launch.py bench.config.yaml --resume <run_id>   # finish a checkpoint

Standard library only — the host needs python3, docker, and the Android SDK's
`emulator` + `adb`. Everything that needs the harness's knowledge (allowed
agents, tiers, app ids, APKs, auth) is asked of the image itself via
`qualgent-bench preflight --json`, so there is one source of truth.

Order: docker → container preflight + plan →
host checks → "Continue?" → boot AVDs → live device wait → run → tear down.

A run that exits 75 stopped on purpose with work left, and said why in
`<runs_dir>/_runs/<run_id>/stop.json`. On a five-hour provider block this script
owns the wait, because it owns the emulators: it tears them down, sleeps out the
window, boots them again and re-runs the same run id with `--resume`. A seven-day
block is a hand-off, not a wait — it prints the export command and exits 75.
`--no-auto-resume` turns the loop off and restores the single-shot behaviour.

`--resume <run_id>` is the receiving end of that hand-off: after
`qualgent-bench checkpoint import` has laid a bundle into the config's `runs_dir`,
it boots this host's AVDs and runs only the units the run still owes, under the
same run id. It is the same loop — a resume that then hits a five-hour block waits
and carries on exactly as a fresh run would.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import posixpath
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

BOOT_TIMEOUT_SEC = 300
# Per headless emulator; hard-fail below, warn when tight.
RAM_PER_EMULATOR_GB = 2.0
CPUS_PER_EMULATOR = 2
CONTAINER_CONFIG = "/app/bench.config.yaml"
# Outside /app: the agent user cannot read the repo, and its cwd must not sit
# inside it (see "Answer-key isolation" in the Dockerfile).
CONTAINER_RUNS = "/work/runs"

# `run`'s third exit code: stopped by the credit guard, resumable, do not retry
# blind. Mirrors qualgentbench.credit.EXIT_STOPPED — duplicated rather than imported
# because this script must run on a host with no harness installed.
EXIT_STOPPED = 75
# stop.json reasons, same contract (see `CreditGuard.write_stop`).
REASON_FIVE_HOUR = "five_hour_limit"
REASON_SEVEN_DAY = "seven_day_threshold"
# Where the container publishes its run id, inside the runs mount so the host reads
# it back off the same file.
RUN_ID_FILE = ".launch-run-id"

# Segments per launch, counting the first: a stop-wait-resume cycle that never
# converges should end up in the operator's hands, not run until the disk fills.
MAX_SEGMENTS = 10
# Come back a minute after the window reopens rather than on the second it does.
RESET_MARGIN_SEC = 60
# Ceiling on ONE wait. A five-hour window cannot need more, so anything longer means
# a `resume_after` we should not have trusted.
MAX_WAIT_SEC = 6 * 3600
# Used only when stop.json carries no reset time: wait the window out in full rather
# than resume early and spend the segment on a second rejection.
UNKNOWN_RESET_WAIT_SEC = 5 * 3600
COUNTDOWN_TICK_SEC = 60


class Problem(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


# ── host tooling ───────────────────────────────────────────────────────────────

def sdk_roots() -> list[Path]:
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        roots.append(home / "Library/Android/sdk")
    elif system == "Windows":
        roots.append(Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Android/Sdk")
    else:
        roots.append(home / "Android/Sdk")
    return [Path(r) for r in roots if r]


def find_tool(name: str, subdir: str) -> str | None:
    exe = name + (".exe" if platform.system() == "Windows" else "")
    if found := shutil.which(exe):
        return found
    for root in sdk_roots():
        candidate = root / subdir / exe
        if candidate.is_file():
            return str(candidate)
    return None


def run(cmd: list[str], *, timeout: float | None = 60, check: bool = False,
        capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout, check=check)


def host_memory_gb() -> float | None:
    try:
        system = platform.system()
        if system == "Darwin":
            out = run(["sysctl", "-n", "hw.memsize"]).stdout.strip()
            return int(out) / 1e9
        if system == "Linux":
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        if system == "Windows":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = Status()
            st.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1e9
    except Exception:  # noqa: BLE001
        return None
    return None


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


# ── docker ─────────────────────────────────────────────────────────────────────

def docker_ready() -> None:
    if not shutil.which("docker"):
        raise Problem("docker is not installed (https://docs.docker.com/get-docker/)")
    if run(["docker", "info"], timeout=30).returncode != 0:
        raise Problem("the docker daemon is not running — start Docker Desktop / dockerd")


def image_ready(image: str, pull: bool) -> str:
    def inspect() -> subprocess.CompletedProcess:
        return run(["docker", "image", "inspect", "--format",
                    "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}", image])
    found = inspect() if not pull else None
    if found is None or found.returncode != 0:
        log(f"pulling {image} …")
        pulled = run(["docker", "pull", image], timeout=1800, capture=False).returncode == 0
        if not pulled:
            # A local-only tag can never be pulled — and Docker Desktop's first API
            # calls while its VM resumes from Resource Saver can fail spuriously.
            time.sleep(2)
            found = inspect()
            if found.returncode != 0:
                detail = found.stderr.strip().splitlines()
                raise Problem(f"image {image} is not available locally and could not be pulled"
                              + (f"\n  {detail[-1]}" if detail else ""))
        else:
            found = inspect()
    return found.stdout.strip() or image


def docker_base(image: str, config_path: Path, runs_dir: Path,
                env_mount: tuple[Path, str] | None, digest: str, tty: bool) -> list[str]:
    cmd = ["docker", "run", "--rm"]
    if tty:
        cmd.append("-it")
    cmd += ["-v", f"{config_path.resolve()}:{CONTAINER_CONFIG}:ro",
            "-v", f"{runs_dir.resolve()}:{CONTAINER_RUNS}",
            "-e", f"QGB_IMAGE_DIGEST={digest}"]
    if env_mount is not None:
        host_env, container_env = env_mount
        # Mounted where the config names it so the harness loads it itself;
        # --env-file besides, so the vars are set however the config resolves.
        cmd += ["-v", f"{host_env}:{container_env}:ro", "--env-file", str(host_env)]
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if codex_home.is_dir():
        cmd += ["-v", f"{codex_home}:/root/.codex:ro"]
    console_token = Path.home() / ".emulator_console_auth_token"
    if console_token.is_file():
        cmd += ["-v", f"{console_token}:/root/.emulator_console_auth_token:ro"]
    if platform.system() == "Linux":
        # The bridge network cannot reach a loopback-bound adb server.
        cmd += ["--network", "host",
                "-e", "ANDROID_ADB_SERVER_ADDRESS=127.0.0.1",
                "-e", "ANDROID_ADB_SERVER_HOST=127.0.0.1"]
    cmd.append(image)
    return cmd


def host_mcp_url(url: str | None) -> str | None:
    """An MCP server on this machine is `host.docker.internal` from inside Docker
    Desktop; on Linux the container shares the host network."""
    if not url or platform.system() == "Linux":
        return url
    return url.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")


# ── stages ─────────────────────────────────────────────────────────────────────

def container_preflight(image: str, config_path: Path, runs_dir: Path,
                        env_mount: tuple[Path, str] | None, digest: str,
                        serials: list[str], mcp_url: str | None) -> dict:
    """Schema + agent + auth + scope + APKs + plan, judged by the image."""
    cmd = docker_base(image, config_path, runs_dir, env_mount, digest, tty=False)
    cmd += ["preflight", CONTAINER_CONFIG, "--json"]
    if serials:
        cmd += ["--devices", ",".join(serials)]
    if mcp_url:
        # The config names the server as the HOST reaches it; the probe runs in
        # the container, which reaches it via host.docker.internal (mac/win).
        cmd += ["--mcp-server", mcp_url]
    proc = run(cmd, timeout=600)
    try:
        start = proc.stdout.index("{")
        report = json.loads(proc.stdout[start:])
    except (ValueError, json.JSONDecodeError):
        raise Problem("the image's preflight produced no report:\n"
                      + (proc.stdout + proc.stderr).strip()[-2000:]) from None
    return report


def print_checks(checks: list[dict]) -> int:
    failures = 0
    for c in checks:
        if c["passed"] and not c.get("warning"):
            icon = "✓"
        elif c.get("warning"):
            icon = "⚠"
        else:
            icon = "✗"
            failures += 1
        log(f"  {icon}  {c['name']:<28} {c['detail']}")
        if not c["passed"] and c.get("fix"):
            log(f"     → {c['fix']}")
    return failures


def host_checks(cfg: dict, config_path: Path) -> tuple[list[dict], dict]:
    """What only the host can see: emulator binary, AVDs, RAM/CPU, runs_dir, env_file."""
    checks: list[dict] = []
    tools: dict[str, str | None] = {"emulator": find_tool("emulator", "emulator"),
                                    "adb": find_tool("adb", "platform-tools")}
    devices = cfg.get("devices") or {}
    avds, serials = devices.get("avds") or [], devices.get("serials") or []

    def add(name, passed, detail, fix=None, warning=False):
        checks.append({"name": name, "passed": passed, "detail": detail, "fix": fix,
                       "warning": warning})

    if tools["adb"]:
        add("adb", True, tools["adb"])
    else:
        add("adb", False, "not found", "Install Android platform-tools or set ANDROID_HOME.")

    if avds:
        if not tools["emulator"]:
            add("emulator", False, "not found",
                "Install the Android SDK emulator (Android Studio → SDK Tools) or set ANDROID_HOME.")
        else:
            add("emulator", True, tools["emulator"])
            known = run([tools["emulator"], "-list-avds"]).stdout.split()
            missing = [a for a in avds if a not in known]
            if missing:
                add("AVDs", False, f"not found: {', '.join(missing)}",
                    f"Known AVDs: {', '.join(known) or '(none)'} — create one in Android Studio "
                    "→ Device Manager, or `avdmanager create avd`.")
            else:
                add("AVDs", True, ", ".join(avds))
            if tools["adb"]:
                running = adb_devices(tools["adb"])
                if running:
                    add("Running emulators", True,
                        f"{len(running)} already online ({', '.join(running)}) — the launcher "
                        "boots its own on free ports", warning=True)
        lanes = min(len(avds), devices.get("max_lanes") or len(avds))
        mem = host_memory_gb()
        cpus = os.cpu_count() or 0
        need_gb, need_cpu = lanes * RAM_PER_EMULATOR_GB, lanes * CPUS_PER_EMULATOR
        if mem is not None and mem < need_gb:
            add("Host resources", False,
                f"{mem:.0f} GB RAM for {lanes} emulator(s) (needs ~{need_gb:.0f} GB)",
                "List fewer AVDs or set devices.max_lanes.")
        elif mem is not None and (mem < need_gb * 1.5 or cpus < need_cpu):
            add("Host resources", True,
                f"{mem:.0f} GB RAM / {cpus} CPUs for {lanes} emulator(s) — tight; expect "
                "slower steps", warning=True)
        else:
            add("Host resources", True,
                f"{mem:.0f} GB RAM / {cpus} CPUs for {lanes} emulator(s)" if mem is not None
                else f"{cpus} CPUs")
    elif serials:
        if tools["adb"]:
            online = adb_devices(tools["adb"])
            missing = [s for s in serials if s not in online]
            if missing:
                add("Devices", False, f"not connected: {', '.join(missing)}",
                    f"Online now: {', '.join(online) or '(none)'}")
            else:
                add("Devices", True, ", ".join(serials))
    else:
        add("Devices", False, "config names no devices.avds and no devices.serials",
            "List AVDs to boot or serials already running.")

    runs_dir = (config_path.parent / (cfg.get("runs_dir") or "runs")).resolve()
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        probe = runs_dir / ".launch-write-test"
        probe.write_text("")
        probe.unlink()
        add("runs_dir", True, str(runs_dir))
    except OSError as exc:
        add("runs_dir", False, f"{runs_dir}: {exc}")

    env_file = None
    if cfg.get("env_file"):
        env_file = (config_path.parent / cfg["env_file"]).expanduser().resolve()
        if env_file.is_file():
            add("env_file", True, str(env_file))
        else:
            add("env_file", False, f"{env_file} not found",
                "cp .env.example .env and put the agent's token in it.")
            env_file = None
    return checks, {"tools": tools, "runs_dir": runs_dir, "env_file": env_file}


def adb_devices(adb: str) -> list[str]:
    out = run([adb, "devices"]).stdout
    return [line.split()[0] for line in out.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"]


def fmt(sec: float) -> str:
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec % 60:02d}s"


def print_plan(plan: dict, lanes: int) -> None:
    kinds = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(plan["by_kind"].items()))
    basis = ", ".join(f"{v} {k}" for k, v in sorted(plan["basis"].items()))
    log("")
    log(f"Plan: {plan['episodes']} episodes ({kinds}) on {lanes} emulator(s)")
    line = f"Estimated time: ~{fmt(plan['eta_sec'])}"
    if lanes > 1:
        line += f"   (1 emulator: ~{fmt(plan['eta_one_lane_sec'])})"
    log(line)
    log(f"Basis: {basis}" + ("" if plan["basis"].get("history", 0) == plan["episodes"]
                            else " — budget/default estimates are ±50%"))


# ── emulators ──────────────────────────────────────────────────────────────────

def start_adb_keepalive(adb: str, interval_sec: float = 5.0):
    """Watch the HOST adb server for the whole run and restart it the moment it
    dies (`adb start-server` is idempotent — a no-op while it is healthy). The
    server can be killed under us by any foreign adb client with a different
    version, or crash outright; the container cannot restart it, only this host
    process can. Returns a stop() callable. Daemon thread + subprocess only —
    this script stays stdlib-only."""
    import threading

    stop_event = threading.Event()

    def _watch() -> None:
        was_up = True
        while not stop_event.wait(interval_sec):
            try:
                probe = subprocess.run([adb, "start-server"], capture_output=True,
                                       text=True, timeout=20)
                restarted = "daemon started successfully" in (probe.stdout + probe.stderr)
                if restarted and was_up:
                    log("⚠ adb server was down — restarted it (lanes resume on their next retry)")
                was_up = probe.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                was_up = False

    thread = threading.Thread(target=_watch, name="adb-keepalive", daemon=True)
    thread.start()

    def stop() -> None:
        stop_event.set()
        thread.join(timeout=2)

    return stop


def boot_avds(emulator: str, adb: str, avds: list[str]) -> list[tuple[str, str, subprocess.Popen]]:
    """Boot each AVD headless on its own console port; returns (avd, serial, proc)."""
    run([adb, "start-server"])
    booted = []
    port = 5554
    for avd in avds:
        while not (port_free(port) and port_free(port + 1)):
            port += 2
        serial = f"emulator-{port}"
        cmd = [emulator, "-avd", avd, "-port", str(port), "-no-window", "-no-audio",
               "-no-boot-anim", "-no-snapshot-save"]
        log(f"  booting {avd} as {serial} …")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        booted.append((avd, serial, proc))
        port += 2
    return booted


def wait_for_boot(adb: str, booted: list[tuple[str, str, subprocess.Popen]]) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_SEC
    pending = {serial: avd for avd, serial, _ in booted}
    while pending and time.monotonic() < deadline:
        for serial in list(pending):
            proc = next(p for _, s, p in booted if s == serial)
            if proc.poll() is not None:
                raise Problem(f"emulator {pending[serial]} ({serial}) exited during boot "
                              f"(rc={proc.returncode}); run it by hand to see why:\n"
                              f"  emulator -avd {pending[serial]}")
            out = run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                      timeout=10).stdout.strip()
            if out == "1":
                log(f"  {pending[serial]} ready as {serial}")
                del pending[serial]
        if pending:
            time.sleep(3)
    if pending:
        raise Problem("emulator(s) did not finish booting within "
                      f"{BOOT_TIMEOUT_SEC}s: {', '.join(pending.values())}")


def kill_emulators(adb: str, booted: list[tuple[str, str, subprocess.Popen]]) -> None:
    for avd, serial, proc in booted:
        run([adb, "-s", serial, "emu", "kill"], timeout=15)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log(f"  stopped {avd} ({serial})")


# ── the stop protocol ──────────────────────────────────────────────────────────

def run_meta_dir(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / "_runs" / run_id


def plan_file(runs_dir: Path, run_id: str) -> Path:
    """`_runs/<run_id>/plan.json` — the frozen unit list a resume replays.

    Its presence is this script's whole test for "is that run id real here". The
    harness refuses a resume without it, but by then the launcher has already booted
    the emulators, so a mistyped id would cost a boot cycle to find out.
    """
    return run_meta_dir(runs_dir, run_id) / "plan.json"


def read_run_id(path: Path) -> str | None:
    """The id the container wrote via `--run-id-file`, or None if it never got there."""
    try:
        first = path.read_text().splitlines()
    except OSError:
        return None
    return first[0].strip() if first and first[0].strip() else None


def read_stop(runs_dir: Path, run_id: str) -> dict | None:
    """`_runs/<run_id>/stop.json` — why the credit guard stopped this run.

    Its absence is a signal in itself: a run that finished, broke or was cancelled
    never writes one.
    """
    try:
        body = json.loads((run_meta_dir(runs_dir, run_id) / "stop.json").read_text())
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def clear_stop(runs_dir: Path, run_id: str) -> None:
    """Delete the previous segment's stop.json before the next one starts.

    The harness already refuses to act on a rate-limit event older than the current
    sitting, because a stale rejection would stop every resume the instant it began.
    The launcher has the same hazard one level up: if a segment exits 75 without
    managing to write its stop file, last segment's file would be read as this one's,
    its `resume_after` would already be in the past, the wait would collapse to zero
    and the loop would spin. Clearing it first keeps "no stop.json" honest — so a 75
    with nothing to explain it stops the loop instead of feeding it stale state.
    """
    try:
        (run_meta_dir(runs_dir, run_id) / "stop.json").unlink(missing_ok=True)
    except OSError:
        pass


def wait_seconds(stop: dict, *, now: float | None = None) -> float:
    """How long to sleep before resuming, from stop.json's `resume_after`.

    `resume_after` is unix epoch seconds and the contract allows it to be null. A
    window we cannot time is waited out in full rather than guessed short: resuming
    early only spends the next segment on another rejection. Capped either way.
    """
    now = time.time() if now is None else now
    resume_after = stop.get("resume_after")
    if isinstance(resume_after, bool) or not isinstance(resume_after, (int, float)):
        log("  stop.json names no reset time — waiting out a whole five-hour window")
        wait = float(UNKNOWN_RESET_WAIT_SEC)
    else:
        wait = float(resume_after) + RESET_MARGIN_SEC - now
    return max(0.0, min(wait, float(MAX_WAIT_SEC)))


def countdown(seconds: float, sleep=time.sleep) -> None:
    """Sleep out the block a minute at a time, saying what is left.

    Chunked rather than one long sleep so Ctrl-C lands promptly and so an operator
    looking at a terminal that has been quiet for hours can see this is a wait and not
    a hang. The remaining time is decremented by what was asked for rather than
    re-read off the clock, so the loop terminates for any `sleep` — including the one
    the tests pass in.
    """
    left = float(seconds)
    while left > 0:
        log(f"  {fmt(left)} until the window reopens")
        chunk = min(float(COUNTDOWN_TICK_SEC), left)
        sleep(chunk)
        left -= chunk


def launcher_resume_command(config_path: Path, run_id: str) -> str:
    """How to say `--resume` to THIS script, in a line the reader can paste.

    The launcher form leads every hand-off banner because the launcher is what boots
    the AVDs. The bare `qualgent-bench run --resume` underneath it only works for a
    reader who has already wired their own emulators and adb, which the person who
    was handed a bundle has not.
    """
    return f"python3 scripts/launch.py {config_path} --resume {run_id}"


def print_handoff(stop: dict, run_id: str, config_path: Path) -> None:
    """What to do with a run this host cannot finish. The seven-day window does not
    reopen for days, so the checkpoint goes to someone running on their own account;
    a five-hour stop with waiting turned off is just "come back yourself"."""
    if stop.get("reason") == REASON_SEVEN_DAY:
        pct = stop.get("utilization_pct")
        log("\nSeven-day budget reached"
            + (f" ({pct:.1f}% of the window)" if isinstance(pct, (int, float)) else "")
            + " — this account cannot finish the run.")
        log("Export the checkpoint and let another account carry it on:")
        log(f"\n    qualgent-bench checkpoint export {run_id}")
        log("\n  then, on the machine that will finish it (its own account, its own "
            "emulators):")
        log("\n    qualgent-bench checkpoint import <bundle> --runs-dir <its runs_dir>")
        log(f"    {launcher_resume_command(config_path, run_id)}")
    else:
        log("\nFive-hour limit reached, and waiting it out is turned off "
            "(checkpoint.wait_for_five_hour_reset).")
        log("Resume once the window reopens:")
        log(f"\n    {launcher_resume_command(config_path, run_id)}")
        log(f"    {stop.get('resume') or f'qualgent-bench run --resume {run_id}'}"
            f"   (if you are booting your own emulators)")
    done, remaining = stop.get("done"), stop.get("remaining")
    if isinstance(done, int) and isinstance(remaining, int):
        log(f"\n{done} unit(s) done, {remaining} left.")


def confirm(prompt: str) -> bool:
    """Ask before spending somebody's credits — and never die of being unable to ask.

    Unattended (cron, CI, `make launch < /dev/null`, a wrapper that closed stdin)
    there is nobody to answer, and a bare `input()` ends the tool on an EOFError
    traceback. Refusing with the flag to pass is the honest answer: proceeding would
    boot emulators and start a sweep nobody consented to.
    """
    if not stdin_is_a_terminal():
        raise Problem("no terminal to ask on (stdin is not a tty), so nothing was "
                      "started. Re-run with --yes to skip the question.")
    try:
        return input(prompt).strip().lower() in ("", "y", "yes")
    except EOFError:
        raise Problem("stdin closed before the question was answered, so nothing was "
                      "started. Re-run with --yes to skip the question.") from None


def stdin_is_a_terminal() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except ValueError:      # stdin closed out from under us
        return False


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask to continue")
    ap.add_argument("--keep-emulators", action="store_true",
                    help="leave the emulators this script booted running")
    ap.add_argument("--pull", action="store_true", help="pull the image even if present")
    ap.add_argument("--image", default=None, help="override the config's image")
    ap.add_argument("--resume", metavar="RUN_ID", default=None,
                    help="finish a run already under the config's runs_dir instead of "
                         "starting a new one — the receiving end of `checkpoint "
                         "import`. Runs only the units that run still owes.")
    ap.add_argument("--no-auto-resume", action="store_true",
                    help="do not wait out a five-hour provider block and resume; run "
                         "once and exit with the run's own code")
    args = ap.parse_args()
    config_path: Path = args.config
    if not config_path.is_file():
        log(f"config not found: {config_path}")
        return 2

    booted: list[tuple[str, str, subprocess.Popen]] = []
    keepalive_stop = None
    adb = find_tool("adb", "platform-tools")
    try:
        docker_ready()
        # The image validates the config; we only need a few fields back from it.
        image = args.image or _peek_scalar(config_path, "image")
        if not image:
            raise Problem("config has no `image:` and --image was not given")
        digest = image_ready(image, args.pull)
        log(f"image {image}\n  {digest}\n")

        # The env file is judged INSIDE the container (auth, provider keys), so it
        # must be mounted for the very first preflight — all errors in one pass.
        env_mount = None
        if env_value := _peek_scalar(config_path, "env_file"):
            host_env = (config_path.parent / env_value).expanduser().resolve()
            if host_env.is_file():
                env_mount = (host_env, _container_env_path(env_value))

        log("Preflight (harness):")
        mcp_url = host_mcp_url(_peek_scalar(config_path, "mcp_server"))
        report = container_preflight(image, config_path, config_path.parent, env_mount,
                                     digest, [], mcp_url)
        if report.get("config") is None:
            for p in report.get("problems", []):
                log(f"  ✗  {p}")
            raise Problem("fix the config and run again")
        cfg = report["config"]
        failures = print_checks(report["checks"])

        log("\nPreflight (host):")
        checks, host = host_checks(cfg, config_path)
        failures += print_checks(checks)
        if failures:
            log(f"\n{failures} issue(s) found. Nothing was started.")
            return 1
        if not host["tools"]["adb"]:
            raise Problem("adb is required")
        adb = host["tools"]["adb"]

        devices = cfg.get("devices") or {}
        avds, serials = devices.get("avds") or [], devices.get("serials") or []
        lanes = min(len(avds) or len(serials), devices.get("max_lanes") or 10**6)
        runs_dir: Path = host["runs_dir"]

        # A user-initiated resume, seeded before the loop so the loop itself does not
        # know the difference between "this launch started the run" and "this launch
        # picked it up": segments and five-hour waits work the same either way.
        resume_id: str | None = args.resume.strip() if args.resume is not None else None
        if args.resume is not None and (not resume_id or "/" in resume_id
                                        or "\\" in resume_id):
            # An empty or path-shaped value must not fall through to the fresh-run
            # branch: silently starting a second sweep is the exact accident --resume
            # exists to prevent. A run id is one path component, never a path.
            raise Problem(f"--resume needs a run id, not {args.resume!r}. It is the id "
                          f"`checkpoint import` printed, e.g. 20260910-051458-db09.")
        if resume_id:
            # Checked here, before a single AVD boots: a mistyped id or a runs_dir the
            # bundle was not imported into is a typo to correct, not a fresh sweep to
            # start by accident.
            plan_path = plan_file(runs_dir, resume_id)
            if not plan_path.is_file():
                raise Problem(
                    f"--resume {resume_id}: no such run under {runs_dir} "
                    f"({plan_path} does not exist). Nothing was started.\n"
                    f"  A resume needs the run dir the first segment wrote. Check the "
                    f"run id (ls {runs_dir / '_runs'}) and that the config's "
                    f"`runs_dir:` is the tree you imported the bundle into:\n"
                    f"    qualgent-bench checkpoint import <bundle> --runs-dir {runs_dir}")
            log(f"\nResuming run {resume_id} from {runs_dir}")
            log("  Scope, agent and model come from the run's own plan.json; only the "
                "units it still owes will run.")
        elif report.get("plan"):
            print_plan(report["plan"], lanes)

        # Only a fresh run asks. A resume was approved when the run was first started,
        # and the plan printed above is the WHOLE sweep — re-confirming against it
        # would be asking about work that is already done.
        if not args.yes and not resume_id:
            if not confirm("\nContinue? [Y/n] "):
                log("aborted; nothing was started.")
                return 0

        run_id_file = runs_dir / RUN_ID_FILE
        mcp = host_mcp_url(cfg.get("mcp_server"))
        configured_serials = serials[:lanes]
        rc = 1

        # One segment per iteration. A segment that exits 75 stopped on purpose with
        # work left; everything else — finished, broken, interrupted — is the end.
        for segment in range(1, MAX_SEGMENTS + 1):
            if avds:
                log("\nBooting emulators:" if segment == 1 else "\nBooting emulators again:")
                booted = boot_avds(host["tools"]["emulator"], adb, avds[:lanes])
                wait_for_boot(adb, booted)
                serials = [s for _, s, _ in booted]
            else:
                serials = configured_serials

            # The container's adb client cannot restart the HOST's daemon; if the
            # server dies mid-run (a foreign adb version killing it, a crash under a
            # large push-install) every lane goes dark until someone runs adb on the
            # host. Keep it alive from here — the harness holds its lanes during the
            # seconds this takes to notice and restart.
            keepalive_stop = start_adb_keepalive(adb)

            cmd = docker_base(image, config_path, runs_dir, env_mount, digest,
                              tty=sys.stdin.isatty() and sys.stdout.isatty())
            cmd += ["run", "--config", CONTAINER_CONFIG, "--devices", ",".join(serials),
                    "--runs-dir", CONTAINER_RUNS, "--yes",
                    "--run-id-file", posixpath.join(CONTAINER_RUNS, RUN_ID_FILE)]
            if resume_id:
                cmd += ["--resume", resume_id]
            if mcp:
                cmd += ["--mcp-server", mcp]

            # Nothing this segment reads may predate it: a run id from a previous
            # launch, or a stop file from the previous segment, would both be taken
            # for this segment's own.
            try:
                run_id_file.unlink(missing_ok=True)
            except OSError:
                pass
            if resume_id:
                clear_stop(runs_dir, resume_id)

            log(f"\nResuming {resume_id} (segment {segment} of at most {MAX_SEGMENTS}):"
                if resume_id else "\nRunning:")
            rc = subprocess.call(cmd)
            run_id = resume_id or read_run_id(run_id_file)
            log(f"\nrun finished (exit {rc}); results in {runs_dir}")
            if rc != EXIT_STOPPED or args.no_auto_resume:
                break

            stop = read_stop(runs_dir, run_id) if run_id else None
            if stop is None:
                log(f"\n✗ the run exited {EXIT_STOPPED} but left no stop.json"
                    f"{f' under {run_id}' if run_id else ' and no run id'} — not "
                    f"resuming on state this script cannot read.")
                break
            if stop.get("run_id") and stop["run_id"] != run_id:
                log(f"\n✗ stop.json under {run_id} names run {stop['run_id']} — "
                    f"refusing to resume on mismatched state.")
                break
            if stop.get("reason") != REASON_FIVE_HOUR or not stop.get("wait_for_five_hour_reset"):
                print_handoff(stop, run_id, config_path)
                break
            if segment == MAX_SEGMENTS:
                log(f"\n✗ stopped after {MAX_SEGMENTS} segments and the account is "
                    f"still blocked. Nothing is lost — pick the run up by hand once "
                    f"the window reopens:")
                log(f"\n    {launcher_resume_command(config_path, run_id)}")
                break

            # The emulators are the reason this wait belongs to the launcher and not
            # to the harness: holding several of them idle for five hours is exactly
            # what the host cannot afford. They come back before the next segment, so
            # --keep-emulators still ends with them running — it is about what this
            # script leaves behind when it EXITS, and it has not exited.
            wait = wait_seconds(stop)
            log(f"\nFive-hour limit — resuming {run_id} in {fmt(wait)} "
                f"(~{time.strftime('%H:%M', time.localtime(time.time() + wait))}).")
            if keepalive_stop is not None:
                keepalive_stop()
                keepalive_stop = None
            if booted:
                log("Stopping emulators for the wait:")
                kill_emulators(adb, booted)
                booted = []
            countdown(wait)
            resume_id = run_id
        return rc
    except Problem as exc:
        log(f"\n✗ {exc}")
        return 1
    except KeyboardInterrupt:
        log("\ninterrupted")
        return 130
    finally:
        if keepalive_stop is not None:
            keepalive_stop()
        if booted and not args.keep_emulators and adb:
            log("\nStopping emulators:")
            kill_emulators(adb, booted)


def _peek_scalar(config_path: Path, key: str) -> str | None:
    """`image:` and `env_file:` are needed BEFORE the image can parse the config.
    Top-level scalar lines; no YAML parser on the host."""
    for line in config_path.read_text().splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if stripped.startswith(key + ":"):
            return stripped.split(":", 1)[1].strip().strip("'\"") or None
    return None


def _container_env_path(env_value: str) -> str:
    """Where the container's harness will look for the config's env_file: the same
    string, resolved against the mounted config's directory (/app)."""
    if env_value.startswith("~"):
        return posixpath.normpath("/root" + env_value[1:])
    return posixpath.normpath(posixpath.join("/app", env_value))


if __name__ == "__main__":
    sys.exit(main())
