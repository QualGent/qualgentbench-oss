#!/usr/bin/env python3
"""Fan the seeded-bug leaderboard out across MULTIPLE adb devices: one OS process
per device (own serial, bridge lock, MCP session), work units (app, model, condition)
round-robined into per-device lanes, all results landing in the shared runs/ dir."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
# Registered easy-tier apps (kept in sync with data/benchmarks/*.yaml). Override with --apps.
_DEFAULT_APPS = ["pftodo", "easynotes", "opencalc"]


def _bench_cmd() -> list[str]:
    """How to invoke the CLI: the installed console script, else `uv run`."""
    if shutil.which("qualgent-bench"):
        return ["qualgent-bench"]
    return ["uv", "run", "qualgent-bench"]


def _discover_devices(spec: str) -> list[str]:
    """Resolve --devices: explicit comma list, 'auto' (all online), or 'cloud'
    (only 127.0.0.1:* Limrun tunnels)."""
    spec = spec.strip()
    if spec not in ("auto", "cloud"):
        return [d.strip() for d in spec.split(",") if d.strip()]
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    if spec == "cloud":
        serials = [s for s in serials if s.startswith("127.0.0.1:")]
    return serials


async def _run_unit(device: str, app: str, model: str, condition: str | None,
                    args) -> tuple[str, str, int]:
    """Run one (app, model, condition) unit on `device`.
    Returns (device, 'app/model[/condition]', rc)."""
    cmd = [
        *_bench_cmd(), "run",
        "--device", device, "--app", app, "--models", model,
        "--mode", args.mode, "--runs-dir", args.runs_dir, "--trials", str(args.trials),
    ]
    if args.agent != "native":
        cmd += ["--agent", args.agent]
    if condition:
        cmd += ["--condition", condition]
    label = f"{app}/{model}" + (f"/{condition}" if condition else "")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(_REPO),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    tag = f"[{device} · {label}]"
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        # Keep the signal lines (results/errors); drop the noisy LiteLLM/httpx chatter.
        if any(s in line for s in ("bugs=", "clean:", "bug:", "ERROR", "Skipping",
                                   "install failed", "Bugs found", "Guided tasks")):
            print(f"{tag} {line}", flush=True)
    rc = await proc.wait()
    print(f"{tag} done (rc={rc})", flush=True)
    return device, label, rc


async def _lane(device: str, units: list[tuple[str, str, str | None]],
                args) -> list[tuple[str, str, int]]:
    """One device runs its assigned units sequentially (a device can't run two
    episodes at once); lanes run concurrently."""
    results = []
    for app, model, condition in units:
        results.append(await _run_unit(device, app, model, condition, args))
    return results


async def _main_async(args) -> int:
    devices = _discover_devices(args.devices)
    if not devices:
        print(f"No devices resolved from --devices {args.devices!r}. "
              f"Start cloud emulators in MCP or boot local ones, then `adb devices`.",
              file=sys.stderr)
        return 2
    apps = [a.strip() for a in args.apps.split(",") if a.strip()] if args.apps else _DEFAULT_APPS
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    conditions = ([c.strip() for c in args.conditions.split(",") if c.strip()]
                  if args.conditions else [None])

    # Work unit = (app, model, condition). Round-robin units into one lane per
    # device so apps/models/conditions spread across the pool.
    units = [(app, m, c) for app in apps for m in models for c in conditions]
    lanes: dict[str, list[tuple[str, str, str | None]]] = {d: [] for d in devices}
    for i, unit in enumerate(units):
        lanes[devices[i % len(devices)]].append(unit)

    print(f"Devices: {len(devices)} {devices}")
    print(f"Apps: {apps}  Models: {args.models}  Mode: {args.mode}"
          + (f"  Agent: {args.agent}" if args.agent != "native" else "")
          + (f"  Conditions: {args.conditions}" if args.conditions else ""))
    for d, u in lanes.items():
        print(f"  lane {d}: "
              + (", ".join(f"{a}/{m}" + (f"/{c}" if c else "") for a, m, c in u) or "(idle)"))
    print("Running…", flush=True)

    t0 = time.monotonic()
    lane_results = await asyncio.gather(*[_lane(d, u, args) for d, u in lanes.items()])
    elapsed = time.monotonic() - t0

    flat = [r for lane in lane_results for r in lane]
    failures = [r for r in flat if r[2] != 0]
    print(f"\nFinished {len(flat)} unit(s) across {len(devices)} device(s) in {elapsed:.0f}s.")
    if failures:
        print(f"⚠️  {len(failures)} unit(s) had a non-zero exit: "
              f"{', '.join(f'{d}/{a}' for d, a, _ in failures)}")

    agent_flag = f" --agent {args.agent}" if args.agent != "native" else ""
    if args.push_sheet:
        print("\nAggregating + pushing to the sheet…", flush=True)
        cmd = [*_bench_cmd(), "leaderboard", "show", "--mode", args.mode,
               "--runs-dir", args.runs_dir, "--push-sheet"]
        if args.agent != "native":
            cmd += ["--agent", args.agent]
        subprocess.run(cmd, cwd=str(_REPO))
    else:
        print(f"\nReview locally:  qualgent-bench show --mode {args.mode}{agent_flag}")
        print(f"Push when ready:  qualgent-bench show --mode {args.mode}{agent_flag} --push-sheet")
    return 1 if failures else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Parallel multi-device seeded-bug leaderboard.")
    p.add_argument("--devices", default="auto",
                   help="'auto' (all online adb devices), 'cloud' (only 127.0.0.1:* "
                        "Limrun tunnels), or an explicit comma list of serials.")
    p.add_argument("--models", required=True, help="Comma-separated model ids.")
    p.add_argument("--apps", default=None,
                   help=f"Comma-separated app ids. Default: {','.join(_DEFAULT_APPS)}")
    p.add_argument("--agent", default="native",
                   help="Adapter to run (native | claude-code).")
    p.add_argument("--conditions", default=None,
                   help="MCP-ablation conditions to fan out (e.g. 'raw,mcp'); "
                        "each becomes its own work unit. Requires a CLI agent + --mode hunt.")
    p.add_argument("--mode", default="guided", choices=["guided", "hunt", "all"])
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--push-sheet", action="store_true",
                   help="After all lanes finish, aggregate + upsert to the Google Sheet.")
    args = p.parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
