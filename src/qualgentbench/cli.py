"""qualgent-bench CLI — entry point for all benchmark commands."""

from __future__ import annotations

import asyncio
import time
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import leaderboard as _lb
from .adapters import REGISTRY as ADAPTER_REGISTRY
from .doctor import run_doctor
from .dotenv import load_dotenv
from .result import RunResult
from .schemas import Condition

console = Console()
logger = logging.getLogger(__name__)


AGENT_CLI: dict[str, str | None] = {
    "claude-code": "claude",
    "codex-cli": "codex",
    "native": None,
}

# Wall-clock cap on replaying one episode's reproductions. Replay cost scales with
# steps x areas, and hitting the cap excludes an otherwise valid episode.
_REPLAY_TIMEOUT_SEC = 3600

AGENT_MODELS: dict[str, list[str]] = {
    "claude-code": ["claude-opus-4-8"],
    "codex-cli": ["gpt-5.5"],
    "native": [
        "gpt-4o",
        "llama-3.3-70b",
        "qwen2.5-72b",
        "deepseek-v3",
    ],
}


# ── Logging ────────────────────────────────────────────────────────────────────

# Libraries that log per HTTP request — hundreds of lines per episode at INFO,
# burying the output that matters. Silenced unless --verbose.
_NOISY_LOGGERS = ("httpx", "httpcore", "mcp", "mcp.client", "urllib3",
                  "huggingface_hub", "filelock", "asyncio")


def _setup_logging(verbose: bool) -> None:
    """Quiet by default — the spinner and result lines already report progress;
    --verbose restores the full stream."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        # Warnings go to a log file instead of interleaving with the results;
        # nothing is dropped — --verbose puts the stream back on the console.
        root = logging.getLogger()
        for h in list(root.handlers):
            h.setLevel(logging.ERROR)
        log_path = Path(os.environ.get("QGB_LOG", "runs")) / "qualgent-bench.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_path)
            fh.setLevel(logging.WARNING)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", "%H:%M:%S"))
            root.addHandler(fh)
        except OSError:
            pass


# ── Root group ─────────────────────────────────────────────────────────────────

# The loader lives in qualgentbench.dotenv so all entry points share it.
# Kept as a module-level name because it is referenced as `cli._load_dotenv`.
_load_dotenv = load_dotenv


@click.group()
@click.version_option(package_name="qualgentbench")
def main() -> None:
    """QualGentBench — evaluate coding agents on mobile QA tasks."""
    _load_dotenv()


# ── qualgent-bench doctor ─────────────────────────────────────────────────────

@main.command("doctor")
@click.option("--agent", default=None,
              type=click.Choice(list(ADAPTER_REGISTRY)),
              help="Also check that the agent CLI is installed")
@click.option("--mcp-server", default=None, envvar="QGB_MCP_SERVER",
              help="Also check this MCP server. Omit to check only the bare-agent path.")
@click.option("--lean", is_flag=True,
              help="Skip the Apps/HuggingFace checks (manifest, HF token, per-app "
                   "install) — the benchmark installs its own app (hunt) or fetches a "
                   "customer app live (regression). Keeps bridge/MCP/device/agent/key.")
def doctor_cmd(agent: str | None, mcp_server: str | None, lean: bool) -> None:
    """Check that all prerequisites are met before running tasks."""
    results = asyncio.run(run_doctor(
        url=mcp_server,
        agent=agent,
        lean=lean,
    ))
    failures = _print_checks(results)
    sys.exit(0 if failures == 0 else 1)


def _print_checks(results) -> int:
    """Doctor-style check list; returns the number of hard failures."""
    failures = 0
    for r in results:
        if r.passed and not r.warning:
            icon = "[green]✓[/]"
        elif r.warning:
            icon = "[yellow]⚠[/]"
        else:
            icon = "[red]✗[/]"
            failures += 1
        console.print(f"  {icon}  {r.name:<28} {r.detail}", soft_wrap=True)
        if not r.passed and r.fix:
            console.print(f"     [dim]→ {r.fix}[/]", soft_wrap=True)

    console.print()
    if failures == 0:
        console.print("[green]All checks passed.[/]" if not any(r.warning for r in results)
                      else "[yellow]Ready with warnings.[/]")
    else:
        console.print(f"[red]{failures} issue{'s' if failures != 1 else ''} found.[/] "
                      "Fix them before running.")
    return failures


# ── qualgent-bench preflight ──────────────────────────────────────────────────

@main.command("preflight")
@click.argument("config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--plan", is_flag=True, help="Also print the episode plan and ETA.")
@click.option("--devices", default=None,
              help="Plan for these adb serials (comma-separated) instead of the config's.")
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable: the normalised config, every check and the plan "
                   "as one JSON object (what scripts/launch.py reads).")
@click.option("--mcp-server", default=None,
              help="Probe this URL instead of the config's mcp_server — the launcher "
                   "passes the address the CONTAINER reaches the host's server at "
                   "(host.docker.internal), same as it does for `run`.")
def preflight_cmd(config_path: Path, plan: bool, devices: str | None, as_json: bool,
                  mcp_server: str | None) -> None:
    """Check that CONFIG_PATH is runnable — agent, auth, tiers, apps, APKs, MCP,
    devices — and optionally print the plan, before anything boots."""
    from dataclasses import asdict

    from .config import ConfigError, load_config
    from .preflight import failed, run_preflight

    if as_json:
        try:
            cfg = load_config(config_path)
        except ConfigError as exc:
            click.echo(json.dumps({"ok": False, "config": None,
                                   "problems": exc.problems}, indent=2))
            sys.exit(1)
    else:
        cfg = _load_config_or_exit(config_path)
    if mcp_server:
        cfg.mcp_server = mcp_server
    _load_env_file(cfg, config_path.parent)
    results, selected = asyncio.run(run_preflight(cfg, config_dir=config_path.parent))
    failures = len(failed(results))
    serials = [d.strip() for d in (devices or "").split(",") if d.strip()] \
        or cfg.devices.serials or cfg.devices.avds
    lanes = max(1, min(len(serials) or 1, cfg.devices.max_lanes or len(serials) or 1))
    summary = None
    if (plan or as_json) and selected and not failures:
        summary = _plan_summary(cfg.agent, cfg.model, cfg.scope.mode, cfg.scope.trials,
                                selected, lanes, Path(cfg.runs_dir))
    if as_json:
        click.echo(json.dumps({
            "ok": failures == 0,
            "config": cfg.model_dump(),
            "checks": [asdict(r) for r in results],
            "plan": summary,
        }, indent=2, default=str))
        sys.exit(1 if failures else 0)
    _print_checks(results)
    if summary is not None:
        console.print(_plan_panel(cfg.agent, cfg.model, cfg.scope.mode, cfg.scope.trials,
                                  selected, serials[:lanes], summary))
    sys.exit(1 if failures else 0)


def _load_config_or_exit(path: Path):
    from .config import ConfigError, load_config
    try:
        return load_config(path)
    except ConfigError as exc:
        raise click.ClickException(
            f"{path} is not a valid config:\n" + "\n".join(f"  • {p}" for p in exc.problems))


def _load_env_file(cfg, base: Path) -> None:
    if cfg.env_file:
        path = (base / cfg.env_file).expanduser()
        if path.is_file():
            load_dotenv(path)


def _plan_summary(agent: str, model: str, mode: str, trials: int, apps: list[dict],
                  lanes: int, runs_dir: Path) -> dict:
    """The same plan the lanes will execute, summarised (see scheduler.plan_summary)."""
    from .lanes import build_plan
    from .preflight import resolve_apk_offline
    from .scheduler import Estimator

    return build_plan(apps, mode=mode, trials=trials, lanes=max(1, lanes),
                      estimator=Estimator(runs_dir, agent, model),
                      resolve_apk=resolve_apk_offline, require_apk=False).summary


def _plan_panel(agent: str, model: str, mode: str, trials: int, apps: list[dict],
                devices: list[str], summary: dict, run_id: str | None = None):
    """The plan and its ETA — printed by `preflight --plan` and again by `run`
    before it asks to continue."""
    from .scheduler import fmt_duration

    lanes = max(1, len(devices))
    s = summary
    tiers: dict[str, int] = {}
    for spec in apps:
        t = spec["app"].get("difficulty", "?")
        tiers[t] = tiers.get(t, 0) + 1
    kinds = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(s["by_kind"].items()))
    basis = ", ".join(f"{v} {k}" for k, v in sorted(s["basis"].items()))
    body = (
        f"[bold]Apps:[/] {len(apps)}  "
        f"[dim]({', '.join(f'{k}:{v}' for k, v in sorted(tiers.items()))})[/]\n"
        f"[bold]Agent:[/] {agent}  [bold]Model:[/] {model}  [bold]Mode:[/] {mode}  "
        f"[bold]Trials:[/] {trials}\n"
        f"[bold]Episodes:[/] {s['episodes']}  [dim]({kinds})[/]\n"
        f"[bold]Devices:[/] {lanes}  [dim]{', '.join(devices) or '(first available)'}[/]\n"
        f"[bold]Estimated:[/] ~{fmt_duration(s['eta_sec'])}"
        + (f"  [dim](one device: ~{fmt_duration(s['eta_one_lane_sec'])})[/]" if lanes > 1 else "")
        + f"\n[dim]basis: {basis}"
        + (" — budget/default estimates are ±50%" if s["basis"].get("history", 0) < s["episodes"]
           else "") + "[/]"
    )
    if run_id:
        body += f"\n[bold]Run id:[/] {run_id}"
    return Panel.fit(body, title="QualGentBench plan")


# ── qualgent-bench eval ────────────────────────────────────────────────────────



# ── qualgent-bench eval regression (Story 2: N-DL vs DL vs DL-R) ──────────────

# Setup name → (tooling, condition, recorded label).
# n-dl = raw agent over adb; dl = same agent + MCP.
_REGRESSION_SETUPS: dict[str, tuple[str, "Condition", str]] = {
    "n-dl": ("raw", Condition.no_routines, "n-dl"),
    "dl": ("mcp", Condition.no_routines, "dl"),
}


# ── qualgent-bench leaderboard (model-focused, bare models over our MCP) ───────




# ── Seeded-bug apps ───────────────────────────────────────────────────────────


def _resolve_app_apk(app: dict, spec: dict | None = None) -> Path:
    """Locate an app's prebuilt buggy APK:
    $QUALGENTBENCH_APK_<ID> → dist/<id>/buggy.apk → HuggingFace → apk_local.
    dist/ is gitignored, so the HuggingFace fetch is what makes a fresh clone runnable."""
    app_id = str(app.get("id", ""))
    env = os.environ.get("QUALGENTBENCH_APK_" + app_id.upper().replace("-", "_"))
    if env:
        return Path(env).expanduser()
    repo_root = Path(__file__).resolve().parents[2]
    dist = repo_root / "dist" / app_id / "buggy.apk"
    if dist.exists():
        return dist

    apk_meta = (spec or {}).get("apk")
    if apk_meta:
        from .apps import fetch_seeded_apk
        try:
            return fetch_seeded_apk(app_id, apk_meta)
        except Exception as exc:  # noqa: BLE001 - surface the fix, not a traceback
            raise click.ClickException(_apk_download_help(app_id, apk_meta, exc)) from exc

    if app.get("apk_local"):
        return (repo_root / app["apk_local"]).resolve()
    return dist  # non-existent → caller reports + skips


def _apk_download_help(app_id: str, apk_meta: dict, exc: Exception) -> str:
    """Turn an APK download failure into instructions, distinguished by cause —
    a token fix for a checksum problem sends someone the wrong way entirely."""
    detail = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
    repo = apk_meta.get("repo", "?")
    low = f"{type(exc).__name__} {exc}".lower()
    lines = [
        f"Could not fetch the {app_id} APK from HuggingFace ({repo}).",
        f"  {detail}",
        "",
    ]
    if "sha256" in low or "integrity" in low:
        lines += [
            "The file downloaded but did not match the checksum in the spec. Either the",
            "published APK was replaced without updating the spec, or the download was",
            "truncated. Re-run once; if it persists, the spec's `apk.sha256` needs",
            "updating by whoever republished the APK.",
        ]
    elif "401" in low or "403" in low or "unauthorized" in low or "gated" in low:
        lines += [
            "That looks like an access problem. The dataset is normally public, so either",
            "it has been made private, or an invalid HF_TOKEN in your environment is",
            "overriding anonymous access — HF_TOKEN takes precedence over no token at all.",
            "  * unset a stale token:   unset HF_TOKEN   (and remove it from .env)",
            "  * or get read access to the qualgent org and set a fresh one",
        ]
    else:
        lines += [
            "Most likely no network access to huggingface.co, or the file has moved.",
        ]
    lines += [
        "",
        "Alternatives:",
        f"  * Build it locally:  uv run python scripts/build_app.py {app_id}",
        "    ",
        f"  * Point at an existing file:  export "
        f"QUALGENTBENCH_APK_{app_id.upper().replace('-', '_')}=/path/to.apk",
    ]
    return "\n".join(lines)


async def _run_episodes(
    models: list[str],
    agent: str,
    session,
    mcp_server: str,
    runs_dir: Path,
    trials: int,
    app_filter: str | None = None,
    mode: str = "guided",
    device: str | None = None,
    tier_filter: str | None = None,
    devices: list[str] | None = None,
    lanes: int | None = None,
    plain: bool | None = None,
    yes: bool = False,
) -> list[RunResult]:
    """Run the selected apps over one or more devices. Every (app, kind, trial) is
    one unit in a longest-first queue; each device is a lane pulling from it
    (see lanes.py). Tooling is "mcp" or "raw"; neither arm gets the app's source."""
    from . import bugs as bugmod
    from .lanes import Hooks, LaneRun, build_plan, run_lanes
    from .scheduler import Estimator, new_run_id

    apps = bugmod.load_apps()
    if tier_filter:
        wanted_tiers = parse_tiers(tier_filter)
        apps = [s for s in apps if s["app"].get("difficulty") in wanted_tiers]
    if app_filter:
        wanted = {a.strip() for a in app_filter.split(",") if a.strip()}
        known = {s["app"]["id"] for s in apps}
        if unknown := wanted - known:
            raise click.ClickException(
                f"Unknown app id(s): {', '.join(sorted(unknown))}\n"
                f"  Available{f' in tier {tier_filter}' if tier_filter else ''}: "
                f"{', '.join(sorted(known))}")
        apps = [s for s in apps if s["app"]["id"] in wanted]
    if not apps:
        console.print("[yellow]No benchmark apps matched.[/]")
        return []

    # Unready tiers may run, but never silently — their numbers are not comparable.
    if mode in ("hunt", "all"):
        unready = sorted({s["app"].get("difficulty") for s in apps} - READY_TIERS)
        if unready:
            console.print(
                f"[yellow]Warning: {'/'.join(t for t in unready if t)} tier apps are not "
                f"hunt-ready.[/] Their briefs leak, budgets are underived and probes are "
                f"missing, so their scores are not comparable.\n"
                f"  For a clean board use: --tier easy or --tier medium\n"
                f"  Check with: uv run python scripts/check_tier_ready.py --tier <tier>")

    # One model per run: the board, the estimate and the lanes all assume it.
    if len(models) != 1:
        raise click.ClickException(
            f"`run` takes exactly one model (got {len(models)}: {', '.join(models)}). "
            f"Run once per model; `show` blends the boards.")
    model = _resolve_model(agent, models[0])

    devices = await _resolve_devices(session, device, devices, lanes)
    if not devices:
        console.print("[red]No device available.[/]")
        return []

    run_id = new_run_id()
    estimator = Estimator(runs_dir, agent, model)
    plan = build_plan(apps, mode=mode, trials=trials, lanes=len(devices),
                      estimator=estimator, resolve_apk=_resolve_app_apk,
                      on_skip=_print_apk_skip)
    if not plan.units:
        console.print("[yellow]Nothing to run.[/]")
        return []

    planned = [s for s in apps if s["app"]["id"] in plan.apks]
    console.print(_plan_panel(agent, model, mode, trials, planned, devices, plan.summary,
                              run_id=run_id))
    _write_plan(runs_dir, run_id, plan.summary, agent=agent, model=model, mode=mode,
                devices=devices)
    if not yes and sys.stdin.isatty() and not click.confirm("Continue?", default=True):
        raise click.Abort()

    out: list[RunResult] = []
    cfg = LaneRun(agent=agent, model=model, mcp_server=mcp_server, runs_dir=runs_dir,
                  trials=trials, run_id=run_id, devices=devices, session=session,
                  console=console, plain=plain, hooks=Hooks(verify=_verify_episode),
                  results=out)
    # Ctrl+C still leaves a usable result: finished episodes are already scored
    # on disk, so print the board over whatever completed.
    try:
        await run_lanes(plan, cfg)
    finally:
        if out:
            _print_run_footer(out)
            _write_board(runs_dir, run_id, out)
        console.print(f"\n[dim]run id {run_id} · board: "
                      f"qualgent-bench show --agent {agent} --mode {mode} --run {run_id}[/]")
    return out


async def _resolve_devices(session, device: str | None, devices: list[str] | None,
                           lanes: int | None) -> list[str]:
    """--devices a,b,c | --devices auto (every ready device) | --device x | nothing
    (first ready device, as before). --lanes caps the count."""
    available = await session.available_devices()
    if devices:
        if devices == ["auto"]:
            chosen = list(available)
        else:
            chosen = list(dict.fromkeys(devices))
            if missing := [d for d in chosen if available and d not in available]:
                raise click.ClickException(
                    f"Device(s) not connected: {', '.join(missing)}. "
                    f"Available: {', '.join(available) or '(none)'}")
    elif device:
        chosen = [device]
    else:
        chosen = available[:1]
    if lanes:
        chosen = chosen[:max(1, lanes)]
    return chosen


def _print_apk_skip(app: dict) -> None:
    env_var = "QUALGENTBENCH_APK_" + str(app["id"]).upper().replace("-", "_")
    console.print(
        f"[yellow]Skipping {app.get('name', app['id'])}: no APK available.[/]\n"
        f"  This app has no published `apk:` block in its spec, so it cannot be\n"
        f"  downloaded. Either:\n"
        f"    build it:  uv run python scripts/build_app.py {app['id']}\n"
        f"    or point at one:  export {env_var}=/path/to.apk\n"
        f"  See scripts/build_app.py.")


def _run_meta_dir(runs_dir: Path, run_id: str) -> Path:
    d = runs_dir / "_runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_plan(runs_dir: Path, run_id: str, summary: dict, **meta) -> None:
    try:
        (_run_meta_dir(runs_dir, run_id) / "plan.json").write_text(json.dumps(
            {"run_id": run_id, **meta, **summary}, indent=2))
    except OSError as exc:
        logger.warning("plan.json not written: %s", exc)


def _write_board(runs_dir: Path, run_id: str, results: list[RunResult]) -> None:
    """What the run produced, beside its plan: every episode's identity, verified
    numbers, exclusion and provenance — the board's audit trail."""
    from .failures import exclusion_reason
    rows = []
    for r in results:
        h = r.metrics.get("hybrid") or {}
        rows.append({
            "task_id": r.task_id, "task_type": r.task_type, "trial": r.trial,
            "model": r.model, "wall_time_sec": round(r.wall_time_sec),
            "excluded": exclusion_reason(r.metrics) or None,
            "f1": h.get("f1", r.metrics.get("f1")),
            "fp_rate": h.get("fp_rate"),
            "steps": h.get("steps", r.metrics.get("hook_steps")),
            "overall": h.get("overall", r.metrics.get("overall")),
            "total_tokens": r.metrics.get("total_tokens"),
            "provenance": r.provenance,
            "artifact_dir": r.artifact_dir,
        })
    try:
        (_run_meta_dir(runs_dir, run_id) / "board.json").write_text(json.dumps(
            {"run_id": run_id,
             # The printed Bug-hunt table's numbers as data, one row per
             # (agent, model, condition) — the plotting-ready summary.
             "summary": _lb.hunt_summary(results),
             "episodes": rows,
             "actual_wall_sec": round(sum(r.wall_time_sec for r in results))}, indent=2))
    except OSError as exc:
        logger.warning("board.json not written: %s", exc)


def _print_run_footer(results: list[RunResult]) -> None:
    """Print run cost and validity — a reader needs to know an episode is not
    quotable before reading the board, not by digging through result.json."""
    if not results:
        return
    wall = sum(r.wall_time_sec or 0 for r in results)
    cost = sum(r.metrics.get("cost_usd") or 0 for r in results)
    trunc = sum(1 for r in results
                if r.metrics.get("truncated") and (r.metrics.get("coverage") or 0) < 1.0)
    # Hunt records `device_actions`, guided records `device_tool_calls` — read
    # whichever exists, or every guided episode looks dead.
    dead = sum(1 for r in results
               if (r.metrics.get("device_actions")
                   if r.metrics.get("device_actions") is not None
                   else r.metrics.get("device_tool_calls") or 0) < 5)
    off = sum(1 for r in results if r.metrics.get("off_app"))
    # Killed before it could report — invisible to every other check here.
    env = sum(1 for r in results if r.metrics.get("env_failure"))
    tainted = sum(1 for r in results if r.metrics.get("contaminated"))

    line = (f"[dim]{len(results)} episode(s) · {int(wall // 60)}m{int(wall % 60):02d}s"
            f" · ${cost:.2f}[/]")
    console.print()
    console.print(line)
    if trunc or dead or off or env or tainted:
        parts = []
        if trunc:
            parts.append(f"{trunc} truncated with incomplete coverage")
        if dead:
            parts.append(f"{dead} ended with almost no device activity")
        if off:
            parts.append(f"{off} left the app under test")
        if env:
            parts.append(f"{env} killed before reporting (agent/provider failure)")
        if tainted:
            parts.append(f"{tainted} READ THE ANSWER KEY (contaminated)")
        console.print(
            f"[yellow]Not quotable: {'; '.join(parts)}.[/] "
            f"Those episodes are not QA results — see result.json, and "
            f"`scripts/check_tier_ready.py` before publishing any number.")
    else:
        console.print("[dim]all episodes valid (no truncation, no dead runs, "
                      "none left the app)[/]")
    _replay_and_board(results)


def _replay_and_board(results) -> None:
    """Verify each episode's reproductions, then print the hybrid board. Runs now,
    while the device and app snapshot are still fresh. Best-effort throughout —
    a replay failure never invalidates a completed run."""
    import subprocess
    root = Path(__file__).resolve().parents[2]
    dirs = []
    for r in results:
        d = getattr(r, "artifact_dir", None)
        if d and (Path(d) / "result.json").exists():
            dirs.append(str(d))
    if not dirs:
        return

    # Each episode was already replayed as it finished. Re-run only the stale ones —
    # those verified under a different replayer — since a full pass costs real
    # device time and episodes must be comparable under one replayer.
    from .replay import replayer_fingerprint
    current = replayer_fingerprint()
    stale = []
    for d in dirs:
        rj = Path(d) / "replay.json"
        try:
            fresh = json.loads(rj.read_text()).get("replayer") == current
        except Exception:  # noqa: BLE001 — missing or unreadable means re-run it
            fresh = False
        if not fresh:
            stale.append(d)

    if not stale:
        console.print(f"\n[dim]{len(dirs)} episode(s) already verified by this "
                      f"replayer — nothing to re-run[/]")
    else:
        console.print(
            f"\n[dim]verifying {len(stale)} of {len(dirs)} episode(s) by replaying "
            f"their reproductions — no model tokens"
            + (f" ({len(dirs) - len(stale)} already current)" if len(stale) < len(dirs)
               else " (replayer changed mid-run — re-deriving)") + "[/]")
    for d in stale:
        try:
            cmd = [sys.executable, str(root / "scripts" / "replay_findings.py"), d]
            # Replay on the device the episode ran on, not the script's default —
            # which would silently replay a parallel run's episodes elsewhere.
            try:
                serial = (json.loads((Path(d) / "result.json").read_text())
                          .get("metrics", {}).get("device_serial"))
            except Exception:  # noqa: BLE001
                serial = None
            if serial:
                cmd += ["--device", str(serial)]
            subprocess.run(cmd, capture_output=True, timeout=_REPLAY_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — verification never fails a run
            logger.warning("replay failed for %s: %s", d, exc)

def _run_replay_with_status(root: Path, run_dir: Path, app: str,
                            total: int | None = None,
                            device: str | None = None,
                            progress=None) -> None:
    """Replay one episode's reproductions, showing how many are done by counting
    replay_findings.py's one-line-per-claim output. The spinner ticks on its own,
    so a claim that takes minutes doesn't look like a hang. With `progress`, the
    text goes to that callback (a lane board row) instead of a spinner."""
    import threading
    from contextlib import nullcontext

    cmd = [sys.executable, str(root / "scripts" / "replay_findings.py"), str(run_dir)]
    if device:
        cmd += ["--device", device]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    done = {"n": 0}

    def _read() -> None:
        for line in proc.stdout or ():
            if "→" in line:            # one per replayed reproduction
                done["n"] += 1

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()

    started = time.monotonic()
    of = f"/{total}" if total else ""
    spinner = console.status("", spinner="dots") if progress is None else nullcontext()
    with spinner as status:
        while proc.poll() is None:
            elapsed = int(time.monotonic() - started)
            text = f"{done['n']}{of} reproductions · {elapsed // 60}m{elapsed % 60:02d}s"
            if progress is not None:
                progress(text)
            else:
                status.update(f"verifying {app} · {text}")
            time.sleep(0.5)
            if elapsed > _REPLAY_TIMEOUT_SEC:
                proc.kill()
                raise TimeoutError(
                    f"replay exceeded {_REPLAY_TIMEOUT_SEC // 60} minutes")
    reader.join(timeout=2)


def _verify_episode(result: RunResult, progress=None) -> tuple[str, list[str]]:
    """Replay one episode's reproductions while the device state is still fresh and
    write the verified score back. Returns (status text, detail lines) for the
    caller to print. An unverifiable claim only lowers recall — excluding the
    episode would reward deleting the evidence, so exclusion is for non-results only."""

    from .failures import exclusion_reason
    from .hybrid_score import combine
    from .replay_score import score as replay_score

    root = Path(__file__).resolve().parents[2]
    run_dir = Path(getattr(result, "artifact_dir", "") or "")
    if not (run_dir / "result.json").exists():
        return "", []
    m = result.metrics or {}
    app = m.get("app_id", "?")
    name = f"{app} · {m.get('condition') or '?'}"

    # Same exclusion predicate the board and `show` use, so terminal and board agree.
    # A generic `failure_reason` is NOT an exclusion — a claimed-but-unexercised
    # defect is a QA result. Only non-results leave the board.
    reason = exclusion_reason(m)
    try:
        _run_replay_with_status(root, run_dir, app,
                                len(m.get('repro_claims') or []) or None,
                                device=m.get("device_serial"), progress=progress)
    except Exception as exc:  # noqa: BLE001 — verification never fails a run
        reason = reason or f"replay error: {exc}"[:40]

    rj = run_dir / "replay.json"
    rs, unver, detail = None, 0, []
    features, derived = [], {}
    try:
        from .bugs import load_suite
        spec = (root / "src" / "qualgentbench" / "data" / "benchmarks" / f"{app}.yaml")
        features = load_suite(spec)["exploration"]["features"]
        truth_seen = False
        for f in ("easy-stability.json", "medium-stability.json"):
            tp = root / "src" / "qualgentbench" / "data" / "truth" / f
            if tp.exists():
                truth_seen = True
                for a, rows in json.loads(tp.read_text()).items():
                    if a == app:
                        derived.update({r["area"]: r["derived"] for r in rows})
        if rj.exists():
            res = json.loads(rj.read_text()).get("results") or []
            rs = replay_score(features, res,
                              {r.get("area"): r.get("claimed") for r in res}, derived)
            for r in res:
                if (r.get("claimed") == "deviates"
                        and r.get("classification") == "unreplayable"
                        and derived.get(r.get("area")) == "broken"):
                    unver += 1
                    detail.append((r.get("area"),
                                   (r.get("seeded_on") or {}).get("detail", "")[:50]))
    except Exception as exc:  # noqa: BLE001
        reason = reason or f"scoring error: {exc}"[:40]

    h = combine(features, {**m, "condition": m.get("condition")}, rs, unver, detail)

    # `excluded` is only for non-results; a weak reproduction stays on the board,
    # already penalised through recall.
    excluded = bool(reason)
    if not excluded:
        if not derived:
            # Without derived truth `unver` is always 0 — a missing key must not
            # read as VERIFIED, so say what is missing instead.
            status = ("[yellow]UNSCORED[/] — no derived truth for this app; run "
                      "`scripts/derive_truth.py --tier <tier>`")
        elif not rj.exists():
            status = "[yellow]PARTIAL[/] — no reproductions could be replayed"
        elif unver:
            status = (f"[yellow]PARTIAL[/] — {unver} claimed defect(s) unverifiable "
                      f"(already deducted from recall)")
        else:
            status = "[green]VERIFIED[/]"
    else:
        status = f"[red]EXCLUDED[/] — {reason} (not a QA result)"

    result.metrics["hybrid"] = h.as_dict()
    result.metrics["hybrid"]["excluded"] = excluded
    try:
        rp = Path(result.artifact_dir) / "result.json"
        on_disk = json.loads(rp.read_text())
        on_disk.setdefault("metrics", {})["hybrid"] = result.metrics["hybrid"]
        rp.write_text(json.dumps(on_disk, indent=2))
    except Exception as exc:  # noqa: BLE001 — recording must not fail a run
        logger.warning("could not persist verified score for %s: %s", name, exc)

    return status, [f"[red]✗[/] {area}: {why}" for area, why in detail]


# `run` is the primary name — this IS the benchmark, not a reporting command.
@main.command("run")
@click.option("--models", default=None,
              help="Comma-separated model ids to rank. Defaults to the agent's list.")
@click.option("--agent", default="native", type=click.Choice(list(ADAPTER_REGISTRY)),
              show_default=True)
@click.option("--app", "app_filter", default=None,
              help="Only run these app id(s), comma-separated (e.g. 'easynotes'). "
                   "Default: all registered apps.")
@click.option("--tier", "tier_filter", default=None,
              help="Run every app in one or more tiers, comma-separated — `--tier easy` "
                   "is the whole easy-tier board, `--tier easy,medium` is both ready "
                   "tiers in one command.")
@click.option("--mode", type=click.Choice(["guided", "hunt", "all"]),
              default="guided", show_default=True,
              help="guided = the per-skill tasks (core leaderboard); hunt = the optional "
                   "open-ended autonomous-QA showcase; all = both (reported separately).")
@click.option("--config", "config_path", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Take agent/model/scope/devices from this config file "
                   "(see bench.config.example.yaml). --devices/--lanes/--plain still apply.")
@click.option("--yes", "-y", is_flag=True,
              help="Start without asking to confirm the plan and ETA.")
@click.option("--devices", default=None,
              help="Run episodes in parallel over these adb serials (comma-separated), "
                   "or `auto` for every ready device. One lane per device.")
@click.option("--lanes", default=None, type=int,
              help="Use at most this many of the devices.")
@click.option("--plain", is_flag=True,
              help="One line per event, no live table — for logs and CI. "
                   "(Automatic when output is not a terminal; QGB_PLAIN_OUTPUT=1 also works.)")
@click.option("--device", default=None,
              help="Pin this run to a specific device serial (e.g. 'emulator-5554' or a "
                   "cloud '127.0.0.1:<port>' tunnel). Default: first available. Used by the "
                   "parallel fan-out to give each worker its own device.")
@click.option("--trials", default=1, show_default=True, type=int)
@click.option("--mcp-server", default=None, envvar="QGB_MCP_SERVER",
              help="MCP server URL giving the agent device tools. Omit to run the agent "
                   "bare, driving the device through adb itself.")
@click.option("--runs-dir", default=None,
              help="Where episodes land. Default: the config's runs_dir, else ./runs.")
@click.option("--push-sheet", is_flag=True)
@click.option("--webhook-url", default=None, envvar="QUALGENT_SHEET_WEBHOOK_URL")
@click.option("--token", default=None, envvar="QUALGENT_SHEET_TOKEN")
@click.option("--verbose", is_flag=True)
def run_benchmark(
    models: str | None,
    agent: str,
    app_filter: str | None,
    tier_filter: str | None,
    mode: str,
    config_path: Path | None,
    yes: bool,
    devices: str | None,
    lanes: int | None,
    plain: bool,
    device: str | None,
    trials: int,
    mcp_server: str | None,
    runs_dir: str,
    push_sheet: bool,
    webhook_url: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """Run the seeded-bug benchmark.

    Runs every registered app, or a subset via --tier / --app. Needs a booted
    device and each app's prebuilt buggy APK. Pass --device to pin one serial, or
    --devices a,b,c (or `auto`) to run episodes in parallel, one lane per device.

    With --mcp-server the agent gets device tools from that server; without it the
    agent runs bare and drives the device through adb itself.
    """
    _setup_logging(verbose)
    device_list = [d.strip() for d in (devices or "").split(",") if d.strip()] or None
    if config_path is not None:
        cfg = _load_config_or_exit(config_path)
        _load_env_file(cfg, config_path.parent)
        agent, models, mode, trials = cfg.agent, cfg.model, cfg.scope.mode, cfg.scope.trials
        tier_filter = ",".join(cfg.scope.tiers) or None
        app_filter = ",".join(cfg.scope.apps) or None
        # Explicit flags win over the file — the launcher uses them to point the
        # container at its own mounts and at the host's MCP server.
        mcp_server = mcp_server or cfg.mcp_server
        runs_dir = runs_dir or cfg.runs_dir
        device_list = device_list or cfg.devices.serials or None
        lanes = lanes or cfg.devices.max_lanes
        if agent not in ADAPTER_REGISTRY:
            raise click.ClickException(
                f"unknown agent {agent!r} in {config_path}; one of "
                f"{', '.join(sorted(ADAPTER_REGISTRY))}")
    _gate_unready_tiers(tier_filter, app_filter, mode)
    model_list = [m.strip() for m in (models or "").split(",") if m.strip()] or None
    asyncio.run(_leaderboard_bugs(
        model_list, agent, trials, mcp_server, Path(runs_dir or "runs"),
        push_sheet, webhook_url, token, app_filter, mode, device, tier_filter,
        devices=device_list, lanes=lanes, plain=plain or None, yes=yes,
    ))


# Tiers hardened for hunt mode. Everything else still carries leaky briefs and
# underived budgets, so its scores mean nothing.
READY_TIERS = {"easy", "medium"}

ALL_TIERS = ("easy", "medium", "hard")


def parse_tiers(tier_filter: str | None) -> set[str]:
    """`--tier easy,medium` -> {"easy", "medium"}. Validated here because
    click.Choice can't express a comma list — a typo would silently run nothing."""
    tiers = {t.strip().lower() for t in (tier_filter or "").split(",") if t.strip()}
    if unknown := tiers - set(ALL_TIERS):
        raise click.ClickException(
            f"Unknown tier(s): {', '.join(sorted(unknown))}\n"
            f"  Valid: {', '.join(ALL_TIERS)}  (comma-separated, e.g. --tier easy,medium)")
    return tiers


def _gate_unready_tiers(tier_filter: str | None, app_filter: str | None,
                        mode: str | None = None) -> None:
    """Refuse a run that targets an unready tier — fail in a second, not after real
    device time produces incomparable numbers. Guided mode is exempt: it is the
    authoring harness that produces the very budgets this gate demands."""
    from . import bugs as bugmod

    if mode == "guided":
        return

    tiers: dict[str, str] = {}   # app id -> tier, for the apps actually requested
    if app_filter:
        wanted = {a.strip() for a in app_filter.split(",") if a.strip()}
        tiers = {s["app"]["id"]: str(s["app"].get("difficulty") or "?")
                 for s in bugmod.load_apps() if s["app"]["id"] in wanted}
    blocked = sorted({t for t in tiers.values() if t not in READY_TIERS})
    if tier_filter:
        blocked = sorted(set(blocked) | (parse_tiers(tier_filter) - READY_TIERS))
    if not blocked:
        return

    apps_note = ""
    if offenders := sorted(a for a, t in tiers.items() if t not in READY_TIERS):
        apps_note = f"\n  Requested from those tiers: {', '.join(offenders)}"
    raise click.ClickException(
        f"🚧 I am working on it — the {'/'.join(blocked)} "
        f"tier{'s are' if len(blocked) > 1 else ' is'} not ready yet.{apps_note}\n"
        f"\n"
        f"  Those apps are seeded but not hardened for hunt mode: their briefs still\n"
        f"  leak the answer, budgets are underived and probes are missing, so any score\n"
        f"  they produce is not comparable to anything.\n"
        f"\n"
        f"  Ready today:  --tier easy   "
        f"({', '.join(sorted(s['app']['id'] for s in bugmod.load_apps() if s['app'].get('difficulty') in READY_TIERS))})\n"
        f"  Track progress:  uv run python scripts/check_tier_ready.py --tier <tier>")


def _mcp_server_help(port: int) -> str:
    return (f"    Start your MCP server and pass its URL:\n"
            f"      qualgent-bench run --mcp-server http://127.0.0.1:{port} ...")

async def _preflight(session, mcp_server: str, agent: str,
                     device: str | None,
                     models: list[str] | None = None) -> None:
    """Check everything an episode needs before spending money on one; each failure
    names the one thing to do. The raw condition uses no bridge at all, so the
    bridge checks are skipped for it."""
    from urllib.parse import urlsplit

    import shutil as _shutil
    problems: list[str] = []

    # 1. Bridge reachable? A plain GET returns 406 (it wants the MCP handshake) —
    #    that IS the healthy response.
    port = urlsplit(mcp_server).port or 51821
    raw_arm = not mcp_server
    bridge_up = False
    if not raw_arm:
        try:
            import httpx
            with httpx.Client(timeout=5.0) as c:
                bridge_up = c.get(f"{mcp_server.rstrip('/')}/mcp").status_code < 500
        except Exception:  # noqa: BLE001
            bridge_up = False
    if raw_arm:
        # No bridge to check. Device availability is asked of adb instead.
        if not await session.first_available_device():
            problems.append(
                "No Android device is available.\n"
                "    Boot an emulator and wait for it to finish starting:\n"
                "      emulator -list-avds\n"
                "      emulator -avd <name> -no-snapshot-load &\n"
                "      adb wait-for-device shell getprop sys.boot_completed")
    elif not bridge_up:
        # `run` starts a server before this, so reaching here means it went away
        # again — not that the user forgot to start one.
        problems.append(
            f"MCP server is not reachable at {mcp_server}.\n"
            f"    One should have been started automatically, so it has exited or the\n"
            f"    port is being taken by something else. Start it by hand to see why:\n"
            f"{_mcp_server_help(port)}")

    # 1b. Reachable — but is it the RIGHT server? The desktop app serves the same
    #     tools but refuses every tap until qg_acquire_device, with the error
    #     buried in the transcript.
    elif await session.is_desktop_bridge():
        problems.append(
            f"The MCP DESKTOP APP is serving {mcp_server}; this benchmark needs\n"
            f"    the standalone server.\n"
            f"    The desktop bridge requires every agent session to call\n"
            f"    qg_acquire_device before it will allow any device tool, and it holds\n"
            f"    that lock against the session — so a stopped episode strands the\n"
            f"    device and the next app fails device-busy.\n"
            f"\n"
            f"    Quit the MCP desktop app — it is holding port {port} — and run\n"
            f"    this command again. The benchmark starts the right server itself.")

    # 2. A device — only meaningful once the bridge can be asked.
    elif not await session.first_available_device():
        problems.append(
            "No Android device is available.\n"
            "    Boot an emulator and wait for it to finish starting:\n"
            "      emulator -list-avds\n"
            "      emulator -avd <name> -no-snapshot-load &\n"
            "      adb wait-for-device shell getprop sys.boot_completed")
    elif device:
        serials = {d.get("id") or d.get("udid") for d in (await session.list_devices() or [])}
        if serials and device not in serials:
            problems.append(
                f"--device {device} is not connected. Available: "
                f"{', '.join(sorted(s for s in serials if s)) or '(none)'}")

    # 3. The agent CLI itself.
    cli = {"codex-cli": "codex", "claude-code": "claude"}.get(agent)
    if cli and not _shutil.which(cli):
        problems.append(
            f"`{cli}` is not on PATH, but --agent {agent} needs it.\n"
            f"    Install it, or run with the other agent.")

    # 4. Provider credentials — otherwise the failure is a 401 inside every
    #    episode, with the cost already incurred.
    from .adapters.claude_code import ClaudeCodeAdapter
    if agent == "claude-code" and not ClaudeCodeAdapter.auth_source():
        problems.append(ClaudeCodeAdapter.auth_fix())
    for m in models or []:
        if ClaudeCodeAdapter.is_fireworks_model(m):
            if not (os.environ.get("FIREWORKS_API_KEY")
                    or os.environ.get("FIREWORKS_AI_API_KEY")):
                problems.append(
                    f"Model {m} runs on Fireworks, but no API key is set.\n"
                    f"    Add to .env:  FIREWORKS_API_KEY=fw_...\n"
                    f"    Key from https://fireworks.ai/account/api-keys")
            if agent != "claude-code":
                problems.append(
                    f"Model {m} is a Fireworks model, which is only wired for\n"
                    f"    --agent claude-code (you passed --agent {agent}).")

    if problems:
        raise click.ClickException(
            "Cannot start the benchmark:\n\n"
            + "\n\n".join(f"  {i}. {p}" for i, p in enumerate(problems, 1))
            + "\n\nRun `uv run qualgent-bench doctor` for a fuller check.")


async def _leaderboard_bugs(
    models: list[str] | None,
    agent: str,
    trials: int,
    mcp_server: str,
    runs_dir: Path,
    push_sheet: bool,
    webhook_url: str | None,
    token: str | None,
    app_filter: str | None = None,
    mode: str = "guided",
    device: str | None = None,
    tier_filter: str | None = None,
    devices: list[str] | None = None,
    lanes: int | None = None,
    plain: bool | None = None,
    yes: bool = False,
) -> None:
    """Run the benchmark. The MCP server, if any, is the caller's to run."""
    await _run_bugs(models, agent, trials, mcp_server, runs_dir, push_sheet,
                    webhook_url, token, app_filter, mode, device, tier_filter,
                    devices=devices, lanes=lanes, plain=plain, yes=yes)


async def _run_bugs(
    models: list[str] | None,
    agent: str,
    trials: int,
    mcp_server: str,
    runs_dir: Path,
    push_sheet: bool,
    webhook_url: str | None,
    token: str | None,
    app_filter: str | None = None,
    mode: str = "guided",
    device: str | None = None,
    tier_filter: str | None = None,
    devices: list[str] | None = None,
    lanes: int | None = None,
    plain: bool | None = None,
    yes: bool = False,
) -> None:
    from . import leaderboard as lb
    from .session import DeviceSession

    # Raw needs no bridge, so its session must be adb-only too — otherwise preflight
    # reports no device while the emulator sits right there.
    session = DeviceSession(mcp_server)
    # Fail in seconds with instructions, not deep into the run with a traceback.
    await _preflight(session, mcp_server, agent, device, models)
    if mcp_server and not await session.is_healthy():
        console.print(f"[red]MCP server not reachable at {mcp_server}.[/] Start MCP.")
        sys.exit(1)
    if not await session.first_available_device():
        console.print("[red]No device found.[/] Boot an Android emulator/simulator first.")
        sys.exit(1)

    models = models or _agent_models(agent)
    collected = await _run_episodes(
        models, agent, session, mcp_server, runs_dir, trials, app_filter, mode, device,
        tier_filter=tier_filter, devices=devices, lanes=lanes, plain=plain, yes=yes,
    )
    if not collected:
        console.print("[red]No bug runs completed.[/]")
        sys.exit(1)

    # Everything, not just guided: _print_bug_summary splits hunt from guided itself.
    _print_bug_summary(collected)
    if push_sheet:
        k_values = (1, trials) if trials > 1 else (1,)
        rows = lb.aggregate_by_model(collected, k_values=k_values)
        _push_leaderboard(rows, [Path(r.artifact_dir) / "result.json" for r in collected],
                          webhook_url, token)


@main.command("show")
@click.option("--runs-dir", default="runs", show_default=True,
              help="Directory containing prior result.json artifacts.")
@click.option("--models", default=None,
              help="Comma-separated model ids or short names to include.")
@click.option("--agent", default="native", type=click.Choice(list(ADAPTER_REGISTRY)),
              show_default=True,
              help="Only include runs from this agent.")
@click.option("--mode", type=click.Choice(["guided", "hunt", "all"]),
              default="guided", show_default=True,
              help="Which benchmark results to display.")
@click.option("--trials", default=1, show_default=True, type=int,
              help="Used to choose pass@k columns when exporting.")
@click.option("--history", is_flag=True,
              help="Include all historical attempts instead of the latest run per model/task/trial.")
@click.option("--run", "run_id", default=None,
              help="Only episodes from this run id (printed by `run`; also in result.json). "
                   "Without it, every run in --runs-dir is blended.")
@click.option("--push-sheet", is_flag=True)
@click.option("--webhook-url", default=None, envvar="QUALGENT_SHEET_WEBHOOK_URL")
@click.option("--token", default=None, envvar="QUALGENT_SHEET_TOKEN")
@click.option("--verbose", is_flag=True)
def leaderboard_show(
    runs_dir: str,
    models: str | None,
    agent: str,
    mode: str,
    trials: int,
    history: bool,
    run_id: str | None,
    push_sheet: bool,
    webhook_url: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """Show the current seeded-bug model leaderboard from saved run artifacts."""
    _setup_logging(verbose)
    results = _lb.load_results(Path(runs_dir), agent=agent, run_id=run_id)
    wanted_types = {
        "guided": {"bug_task", "clean_task"},
        "hunt": {"bug_hunt"},
        "all": {"bug_task", "clean_task", "bug_hunt"},
    }[mode]
    results = [r for r in results if r.task_type in wanted_types]

    if models:
        wanted_models = {m.strip() for m in models.split(",") if m.strip()}
        results = [
            r for r in results
            if r.model in wanted_models or _lb.clean_model_name(r.model) in wanted_models
        ]
    if not history:
        results = _lb.dedupe_latest(results)

    if not results:
        console.print("[red]No matching seeded-bug runs found.[/]")
        sys.exit(1)

    _print_bug_summary(results)
    if push_sheet:
        k_values = (1, trials) if trials > 1 else (1,)
        rows = _lb.aggregate_by_model(results, k_values=k_values)
        _push_leaderboard(rows, [Path(r.artifact_dir) / "result.json" for r in results],
                          webhook_url, token)


# Leaderboard columns rendered in the Sheet tab — (row key, header, lower-is-better).
_LEADERBOARD_METRICS = [
    ("pass_rate", "Pass rate (%)", False),
    ("avg_wall_time_sec", "Avg time (s)", True),
    ("avg_device_tool_calls", "Avg tool calls", True),
    # Cost + tokens intentionally excluded from the sheet.
]


def _avg_metric(rs: list[RunResult], key: str) -> float:
    vals = [r.metrics.get(key) for r in rs if isinstance(r.metrics.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def _print_bug_summary(results: list[RunResult]) -> None:
    """Print the seeded-bug leaderboard, split by episode kind so guided rows
    aren't mis-counted as hunts."""
    hunts = [r for r in results if r.task_type == "bug_hunt"]
    bug_tasks = [r for r in results if r.task_type == "bug_task"]
    clean_tasks = [r for r in results if r.task_type == "clean_task"]
    if hunts:
        _print_hunt_table(hunts)
    if bug_tasks or clean_tasks:
        _print_guided_table(bug_tasks, clean_tasks)


def _print_guided_table(bug_tasks: list[RunResult], clean_tasks: list[RunResult]) -> None:
    """Guided leaderboard on three separate axes — Quality (weighted recall),
    Precision (false reports), Efficiency (speed on correct finds only) — since a
    blended number hides a lucky guesser. Ranked by Quality, then Efficiency."""
    from collections import defaultdict

    PRECISION_FLOOR = 0.7

    models = sorted({_lb.clean_model_name(r.model) for r in bug_tasks + clean_tasks})
    by: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
    for r in bug_tasks:
        by[(_lb.clean_model_name(r.model), "bug")].append(r)
    for r in clean_tasks:
        by[(_lb.clean_model_name(r.model), "clean")].append(r)

    def live(rs: list[RunResult]) -> list[RunResult]:
        """Drop non-results — environment noise must not move a leaderboard."""
        from .failures import is_excluded
        return [r for r in rs if not is_excluded(r.metrics)]

    def axes(model: str) -> tuple[float, float, float, int, int, int]:
        bugs, cleans = live(by[(model, "bug")]), live(by[(model, "clean")])
        weights = [float(r.metrics.get("tier_weight") or 1.0) for r in bugs]
        earned = [w for w, r in zip(weights, bugs) if r.metrics.get("correct")]
        quality = (sum(earned) / sum(weights)) if weights else 0.0
        false_pos = sum(1 for r in cleans if r.metrics.get("false_positive"))
        reports = len(earned) + false_pos
        precision = (len(earned) / reports) if reports else 1.0
        effs = [float(r.metrics.get("efficiency") or 0.0) for r in bugs if r.metrics.get("correct")]
        efficiency = (sum(effs) / len(effs)) if effs else 0.0
        return quality, precision, efficiency, len(earned), len(bugs), false_pos

    table = Table(title="Guided tasks — Quality / Precision / Efficiency (never blended)")
    table.add_column("#", justify="right")
    table.add_column("Model")
    table.add_column("Quality", justify="right")
    table.add_column("Bugs correct", justify="right")
    table.add_column("Precision", justify="right")
    table.add_column("FP", justify="right")
    table.add_column("Efficiency", justify="right")
    table.add_column("Clean passed", justify="right")
    table.add_column("Avg calls", justify="right")
    table.add_column("Runs", justify="right")
    table.add_column("Infra", justify="right")

    ranked = sorted(models, key=lambda m: (-axes(m)[0], -axes(m)[2]))
    for i, model in enumerate(ranked, 1):
        quality, precision, efficiency, correct, n_bugs, fps = axes(model)
        cleans = live(by[(model, "clean")])
        allr = live(by[(model, "bug")]) + cleans
        infra = sum(1 for r in by[(model, "bug")] + by[(model, "clean")]
                    if r.metrics.get("infra_failure"))
        clean_pass = sum(1 for r in cleans if r.metrics.get("oracle_passed"))
        gated = "" if precision >= PRECISION_FLOOR else " ⚠"
        table.add_row(
            str(i), model,
            f"{quality:.2f}",
            f"{correct}/{n_bugs}" if n_bugs else "—",
            f"{precision:.2f}{gated}",
            str(fps),
            f"{efficiency:.2f}" if efficiency else "—",
            f"{clean_pass}/{len(cleans)}" if cleans else "—",
            f"{_avg_metric(allr, 'device_tool_calls'):.0f}",
            str(len(allr)),
            str(infra) if infra else "—",
        )
    console.print(table)
    console.print(f"[dim]Quality = Σ W·correct / Σ W (W: L1=1 L2=3 L3=6 L4=10) · "
                  f"⚠ = below the {PRECISION_FLOOR} precision floor · "
                  f"Infra = episodes excluded (no device activity, no report)[/]")


def _print_hunt_table(results: list[RunResult]) -> None:
    """One row per (agent, model) — the same model scores very differently
    through different CLIs. Renders `leaderboard.hunt_summary`, the same rows
    board.json stores, so the printed and stored numbers cannot drift."""
    rows = [row for row in _lb.hunt_summary(results) if row["episodes"]]
    if not rows:
        return

    table = Table(title="Bug hunt — Overall = weighted recall × speed − false-report cost")
    table.add_column("#", justify="right")
    table.add_column("Agent + Model", no_wrap=True)
    for col in ("Trials", "F1", "FP", "Avg/Step", "Avg/Token", "Overall"):
        table.add_column(col, justify="right")

    # One agent+model across two arms (raw vs mcp) is two rows — name the arm,
    # or the board prints twins.
    multi_cond = len({r["condition"] for r in rows}) > 1
    for i, row in enumerate(rows, 1):
        label = f"{row['agent']} · {row['model']}"
        if multi_cond:
            label += f" · {row['condition']}"
        table.add_row(
            str(i), label, str(row["trials"]),
            f"{row['f1']:.2f}", f"{row['fp_rate'] * 100:.0f}%",
            f"{row['avg_steps']:.0f}", f"{row['avg_tokens']:,.0f}",
            f"[bold]{row['overall'] * 100:.1f}%[/]",
        )
    console.print(table)


def _push_leaderboard(
    rows: list[dict],
    result_paths: list[Path],
    webhook_url: str | None,
    token: str | None,
) -> None:
    """Upsert these models into the sheet's Leaderboard tab, keyed by model name;
    models not in this push are left untouched."""
    if not webhook_url:
        raise click.ClickException(
            "No webhook URL. Pass --webhook-url or set QUALGENT_SHEET_WEBHOOK_URL."
        )
    # Guard against a value that still carries an inline comment / stray whitespace.
    webhook_url = webhook_url.split("#", 1)[0].strip()
    if token:
        token = token.split("#", 1)[0].strip()

    detail: list[dict] = []
    for path in result_paths:
        row = _flatten_result_for_sheet(path)
        if row is not None:
            detail.append(row)

    detail_columns: list[str] = []
    seen: set[str] = set()
    for r in detail:
        for k in r:
            if k not in seen:
                seen.add(k)
                detail_columns.append(k)

    payload: dict = {
        "mode": "leaderboard",
        "leaderboard": {
            "rows": rows,
            "metrics": [
                {"key": k, "title": t, "lower_is_better": lb}
                for k, t, lb in _LEADERBOARD_METRICS
            ],
            "detail": detail,
            "detail_columns": detail_columns,
        },
    }
    if token:
        payload["token"] = token

    import httpx

    try:
        resp = httpx.post(webhook_url, json=payload, timeout=60.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise click.ClickException(f"Failed to post to Google Sheet: {exc}")
    if data.get("error"):
        raise click.ClickException(f"Sheet rejected the request: {data['error']}")
    models = ", ".join(r["model"] for r in rows)
    console.print(
        f"[green]Upserted[/] {len(rows)} model(s) into the 'Leaderboard' tab "
        f"[dim]({models})[/] — other models left untouched."
    )


def _agent_models(agent: str) -> list[str]:
    return AGENT_MODELS.get(agent, ["default"])


# ── helpers ────────────────────────────────────────────────────────────────────

def _flatten_result_for_sheet(path: Path) -> dict | None:
    """Flatten one run's result.json into a spreadsheet row. run_id is unique and
    the sheet dedupes on it, so re-pushing is safe."""
    try:
        d = json.loads(path.read_text())
    except Exception:
        return None

    row: dict = {
        "run_id": path.parent.name,
        "started_at": d.get("started_at"),
        "ended_at": d.get("ended_at"),
        "wall_time_sec": d.get("wall_time_sec"),
        "task_id": d.get("task_id"),
        "task_type": d.get("task_type"),
        "agent": d.get("agent"),
        "model": _lb.clean_model_name(d.get("model") or ""),
        "condition": d.get("condition"),
        "trial": d.get("trial"),
        "passed": d.get("passed"),
        "score": d.get("score"),
        "weighted_score": d.get("weighted_score"),
        "exit_code": d.get("exit_code"),
        "failure_reason": d.get("failure_reason") or "",
    }
    for k, v in (d.get("criteria") or {}).items():
        row[f"crit_{k}"] = v

    metrics = d.get("metrics") or {}

    # Token usage + cost deliberately not exported — the leaderboard reports
    # correctness/speed, not spend. The values still live in result.json.

    for k in (
        "device_tool_calls", "bench_run_routine_calls",
        "bench_list_routines_calls", "transcript_chars",
    ):
        if k in metrics:
            row[f"metric_{k}"] = metrics[k]
    return row


def _resolve_model(agent: str, model: str) -> str:
    return model


