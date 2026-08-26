#!/usr/bin/env python3
"""Run QualGentBench from its Docker image against your own emulators.

    python scripts/launch.py bench.config.yaml [--yes] [--keep-emulators] [--pull]

Standard library only — the host needs python3, docker, and the Android SDK's
`emulator` + `adb`. Everything that needs the harness's knowledge (allowed
agents, tiers, app ids, APKs, auth) is asked of the image itself via
`qualgent-bench preflight --json`, so there is one source of truth.

Order: docker → container preflight + plan →
host checks → "Continue?" → boot AVDs → live device wait → run → tear down.
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


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask to continue")
    ap.add_argument("--keep-emulators", action="store_true",
                    help="leave the emulators this script booted running")
    ap.add_argument("--pull", action="store_true", help="pull the image even if present")
    ap.add_argument("--image", default=None, help="override the config's image")
    args = ap.parse_args()
    config_path: Path = args.config
    if not config_path.is_file():
        log(f"config not found: {config_path}")
        return 2

    booted: list[tuple[str, str, subprocess.Popen]] = []
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
        if report.get("plan"):
            print_plan(report["plan"], lanes)
        if not args.yes:
            answer = input("\nContinue? [Y/n] ").strip().lower()
            if answer not in ("", "y", "yes"):
                log("aborted; nothing was started.")
                return 0

        if avds:
            log("\nBooting emulators:")
            booted = boot_avds(host["tools"]["emulator"], adb, avds[:lanes])
            wait_for_boot(adb, booted)
            serials = [s for _, s, _ in booted]
        serials = serials[:lanes]

        log("\nRunning:")
        cmd = docker_base(image, config_path, host["runs_dir"], env_mount, digest,
                          tty=sys.stdin.isatty() and sys.stdout.isatty())
        cmd += ["run", "--config", CONTAINER_CONFIG, "--devices", ",".join(serials),
                "--runs-dir", CONTAINER_RUNS, "--yes"]
        if mcp := host_mcp_url(cfg.get("mcp_server")):
            cmd += ["--mcp-server", mcp]
        rc = subprocess.call(cmd)
        log(f"\nrun finished (exit {rc}); results in {host['runs_dir']}")
        return rc
    except Problem as exc:
        log(f"\n✗ {exc}")
        return 1
    except KeyboardInterrupt:
        log("\ninterrupted")
        return 130
    finally:
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
