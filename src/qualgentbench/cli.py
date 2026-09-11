"""qualgent-bench CLI — entry point for all benchmark commands."""

from __future__ import annotations

import asyncio
import time
import json
import logging
import os
import socket
import subprocess
import sys
from functools import partial
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import checkpoint as _checkpoint
from . import credit as _credit
from . import leaderboard as _lb
from .adapters import REGISTRY as ADAPTER_REGISTRY
from .config import Checkpoint
from .doctor import run_doctor
from .dotenv import load_dotenv
from .result import RunResult, resolve_artifact_dir
from .schemas import Condition

console = Console()
logger = logging.getLogger(__name__)


AGENT_CLI: dict[str, str | None] = {
    "claude-code": "claude",
    "codex-cli": "codex",
    "native": None,
}

# How many remaining units `checkpoint show` prints before it summarises the rest;
# a full sweep plan is hundreds of lines and `--json` is there for all of them.
_CHECKPOINT_SHOW_LIMIT = 40

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


def _run_async(coro):
    """`asyncio.run`, except subprocess transports are finalised while the loop is
    still alive. Their `__del__` needs a running loop; leaving them to interpreter
    shutdown prints `RuntimeError: Event loop is closed` under every run's board."""
    import gc

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            gc.collect()                                   # transport __del__ now
            for obj in _open_subprocess_transports():
                try:
                    obj.close()
                except Exception:
                    pass
            loop.run_until_complete(asyncio.sleep(0.05))   # let close() callbacks run
        finally:
            # Whatever is still open now can no longer be closed through the loop.
            # Mark it closed so its finaliser is a no-op instead of a traceback at
            # interpreter shutdown (`RuntimeError: Event loop is closed`).
            for obj in _open_subprocess_transports():
                _silence_transport(obj)
            asyncio.set_event_loop(None)
            loop.close()


def _open_subprocess_transports():
    import gc
    from asyncio.base_subprocess import BaseSubprocessTransport
    return [obj for obj in gc.get_objects()
            if isinstance(obj, BaseSubprocessTransport) and not getattr(obj, "_closed", True)]


def _silence_transport(transport) -> None:
    """Make a subprocess transport's __del__ a no-op: it checks `_closed`, and its
    pipes' close() checks `_closing`. Only used after the loop can no longer run."""
    transport._closed = True
    for proto in list(getattr(transport, "_pipes", {}).values()):
        pipe = getattr(proto, "pipe", None)
        if pipe is not None:
            pipe._closing = True


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
    results = _run_async(run_doctor(
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
    results, selected = _run_async(run_preflight(cfg, config_dir=config_path.parent))
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


def _resolve_app_apk(app: dict, spec: dict | None = None, mode: str = "hunt") -> Path:
    """Locate an app's prebuilt buggy APK:
    $QUALGENTBENCH_APK_<ID> → dist/<id>/buggy.apk → HuggingFace → apk_local.
    dist/ is gitignored, so the HuggingFace fetch is what makes a fresh clone runnable.
    Journey mode fetches the JOURNEY build (the test-case file's `apk:` block, published
    under journey/), which carries the journey-only defects; the spec's hunt build is
    the fallback only when no journey build is published."""
    app_id = str(app.get("id", ""))
    env = os.environ.get("QUALGENTBENCH_APK_" + app_id.upper().replace("-", "_"))
    if env:
        return Path(env).expanduser()
    repo_root = Path(__file__).resolve().parents[2]
    dist = repo_root / "dist" / app_id / "buggy.apk"
    if dist.exists():
        return dist

    if mode == "journey":
        from . import journey as _journey
        jmeta = _journey.apk_meta(app_id)
        if jmeta:
            from .apps import fetch_seeded_apk
            try:
                return fetch_seeded_apk(app_id, jmeta, kind="journey")
            except Exception as exc:  # noqa: BLE001 - surface the fix, not a traceback
                raise click.ClickException(_apk_download_help(app_id, jmeta, exc)) from exc
        logger.warning("%s: no journey `apk:` block in its test-case file — using the hunt "
                       "build, which may lack journey-only defects", app_id)

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
    resume: "_checkpoint.ResumePlan | None" = None,
    force_resume: bool = False,
    credit_policy: Checkpoint | None = None,
    run_id_file: Path | None = None,
) -> list[RunResult]:
    """Run the selected apps over one or more devices. Every (app, kind, trial) is
    one unit in a longest-first queue; each device is a lane pulling from it
    (see lanes.py). Tooling is "mcp" or "raw"; neither arm gets the app's source.

    With `resume`, the units come from that run's plan.json minus what it already
    finished, and the episodes continue under the same run id as the next segment.

    Raises `credit.RunStopped` when the provider budget ran out mid-sweep: the board
    and stop.json are written first, so the caller only has to report it and exit 75.
    """
    from . import bugs as bugmod
    from .lanes import Hooks, LaneRun, build_plan, restore_plan, run_lanes
    from .scheduler import Estimator, ScheduleLog, Unit, new_run_id

    apps = bugmod.load_apps()
    if resume is not None:
        # Scope is the plan's, not the corpus's: a resume runs the apps its frozen
        # unit list names, whatever has been added or renamed since.
        known = {s["app"]["id"] for s in apps}
        if unknown := [a for a in resume.app_ids if a not in known]:
            raise click.ClickException(
                f"Run {resume.run_id} planned app(s) this checkout does not have: "
                f"{', '.join(sorted(unknown))}\n"
                f"  A resume finishes the run that was planned, so the corpus has to "
                f"still contain it.")
        wanted_apps = set(resume.app_ids)
        apps = [s for s in apps if s["app"]["id"] in wanted_apps]
    else:
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

    run_id = resume.run_id if resume is not None else new_run_id()
    # Published before anything can fail: the launcher loop needs the id to build
    # `--resume` even for a segment that stopped early.
    _write_run_id_file(run_id_file, run_id)
    resolve = partial(_resolve_app_apk, mode=mode)
    state, remaining = None, []
    if resume is not None:
        state = _checkpoint.state(runs_dir, run_id)
        names = {str(s["app"]["id"]): str(s["app"].get("name") or s["app"]["id"]) for s in apps}
        units = [Unit.from_dict(u, names.get(str(u.get("app") or ""), ""))
                 for u in resume.units]
        # An excluded attempt (rate limited, infra failure) measured nothing, so its
        # unit is still owed and comes back here as remaining work.
        remaining = [u for u in units if not state.is_done(u.app_id, u.task_id, u.trial)]
        plan = restore_plan(remaining, apps, lanes=len(devices),
                            resolve_apk=resolve, on_skip=_print_apk_skip)
        _check_resume_environment(resume, apps, plan, mode, force_resume)
    else:
        plan = build_plan(apps, mode=mode, trials=trials, lanes=len(devices),
                          estimator=Estimator(runs_dir, agent, model),
                          resolve_apk=resolve, on_skip=_print_apk_skip)
    if not plan.units:
        if resume is not None:
            if remaining:
                # Work left, but none of it can run here — not the same thing as a
                # finished run, and saying "complete" would be a lie.
                raise click.ClickException(
                    f"Run {run_id} has {len(remaining)} unit(s) left, but none of their "
                    f"apps have an APK on this machine.\n"
                    f"  Fetch or build the APK(s) named above, then resume again.")
            # A finished run is a success, not "nothing matched": say so, sweep up any
            # interrupted episode it left, and exit 0.
            moved = _checkpoint.discard_orphans(runs_dir, run_id, state.orphans)
            console.print(f"[green]Run {run_id} is already complete[/] — "
                          f"{len(state.done_keys)} unit(s) done, nothing left to run."
                          + (f" {len(moved)} interrupted episode(s) discarded." if moved else ""))
            sys.exit(0)
        console.print("[yellow]Nothing to run.[/]")
        return []

    planned = [s for s in apps if s["app"]["id"] in plan.apks]
    if resume is not None:
        console.print(_resume_line(run_id, resume, state, plan))
    console.print(_plan_panel(agent, model, mode, trials, planned, devices, plan.summary,
                              run_id=run_id))
    if resume is None:
        _write_plan(runs_dir, run_id, plan.summary, apps=planned, mode=mode,
                    agent=agent, model=model, devices=devices)
    if not yes and sys.stdin.isatty() and not click.confirm("Continue?", default=True):
        raise click.Abort()

    segment, log = 0, None
    if resume is not None:
        # Only now, past the last chance to abort: an interrupted episode is evidence
        # and moving it is a side effect the user did not ask for until they said yes.
        moved = _checkpoint.discard_orphans(runs_dir, run_id, state.orphans)
        try:
            segment = _checkpoint.next_segment(runs_dir, run_id)
        except _checkpoint.CheckpointError as exc:
            # It refuses rather than replace a plan it could not read or could not
            # write. That is a stop, not a traceback: the run is intact on disk.
            raise click.ClickException(str(exc)) from exc
        log = ScheduleLog(_checkpoint.run_meta_dir(runs_dir, run_id) / "schedule.jsonl")
        log.write("resume", run_id=run_id, segment=segment, host=socket.gethostname(),
                  devices=devices, done=len(state.done_keys), remaining=len(plan.units),
                  discarded=len(moved), excluded=len(state.excluded))

    out: list[RunResult] = []
    policy = credit_policy or Checkpoint()
    # Only events observed from here on count: a run id can span sittings, and the
    # rejection that stopped the previous one is still in rate_limit.json.
    guard = _credit.CreditGuard(
        runs_dir=runs_dir, run_id=run_id,
        stop_at_seven_day_pct=policy.stop_at_seven_day_pct,
        wait_for_five_hour_reset=policy.wait_for_five_hour_reset)
    cfg = LaneRun(agent=agent, model=model, mcp_server=mcp_server, runs_dir=runs_dir,
                  trials=trials, run_id=run_id, devices=devices, session=session,
                  console=console, plain=plain, guard=guard,
                  # The verifier reads each episode dir off result.json, which now
                  # stores it relative — it needs the runs dir to resolve against.
                  hooks=Hooks(verify=partial(_verify_episode, runs_dir=runs_dir)),
                  results=out, segment=segment, log=log)
    # Ctrl+C still leaves a usable result: finished episodes are already scored
    # on disk, so print the board over whatever completed.
    try:
        await run_lanes(plan, cfg)
    finally:
        if out:
            _print_run_footer(out, runs_dir)
        # The board is the run id's audit trail, not this sitting's: a resume rewrites
        # it over every segment's results (all already on disk) so it still shows the
        # whole run, exactly as `show --run` reads it.
        rows = out if resume is None else _lb.load_results(runs_dir, run_id=run_id)
        if rows:
            _write_board(runs_dir, run_id, rows)
        # The board is written on a credit stop too: a stopped sweep is a partial one,
        # and its completed episodes are as quotable as any other.
        if guard.decision is not None:
            done, left = _run_progress(runs_dir, run_id)
            guard.write_stop(done=done, remaining=left)
        console.print(f"\n[dim]run id {run_id} · board: "
                      f"qualgent-bench show --agent {agent} --mode {mode} --run {run_id}[/]")
    if guard.decision is not None:
        raise _credit.RunStopped(guard, out)
    return out


def _write_run_id_file(path: Path | None, run_id: str) -> None:
    """Publish the run id for whoever launched this process — the launcher loop.

    One bare line, because the reader is `scripts/launch.py`, which is stdlib-only and
    parses nothing. Written the moment the id exists rather than at the end: the whole
    point is to know it for a run that stops early, and a run that stops early is the
    only kind that gets resumed.

    Best effort. The id is also printed and stored in plan.json, so a launcher-side
    path that turns out to be unwritable is worth a warning, never a dead sweep.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run_id + "\n")
    except OSError as exc:
        logger.warning("run id not written to %s: %s", path, exc)


def _run_progress(runs_dir: Path, run_id: str) -> tuple[int | None, int | None]:
    """(units finished, units still owed) for a run id, straight off disk.

    Read from the plan and the results rather than from this sitting's counters,
    because that is the number the launcher and the person picking the checkpoint up
    care about: how much of the RUN is left, not how much of this sitting ran.
    """
    try:
        planned = len(_checkpoint.load_plan(runs_dir, run_id).units)
    except _checkpoint.CheckpointError:
        return None, None
    done = len(_checkpoint.state(runs_dir, run_id).done_keys)
    return done, max(0, planned - done)


#: The config `scripts/launch.py` is invoked with in the README, the Makefile and
#: every doc — the name to put in a banner somebody will paste.
LAUNCHER_CONFIG = "bench.config.yaml"


def _resume_lines(run_id: str, *, runs_dir: Path | None = None,
                  suffix: str = "") -> list[str]:
    """How to finish `run_id`, launcher first.

    `scripts/launch.py` is what boots the AVDs and runs the image, so it is the only
    resume command somebody who was handed a bundle can use as-is. The bare harness
    form below it assumes emulators already booted and wired to adb — true for
    whoever ran the first segment, false for whoever receives it, which is exactly
    the reader these banners are written for.
    """
    bare = f"qualgent-bench run --resume {run_id}"
    if runs_dir is not None and str(runs_dir) != "runs":
        bare += f" --runs-dir {runs_dir}"
    return [f"  python3 scripts/launch.py {LAUNCHER_CONFIG} --resume {run_id}{suffix}",
            f"[dim]  or, with emulators you booted yourself: {bare}[/]"]


def _stop_panel(decision: "_credit.StopDecision", runs_dir: Path, run_id: str):
    """What to do next, printed under the board when the credit guard stopped a run.

    A five-hour stop is a wait; a seven-day stop is a hand-off. Saying so here is the
    difference between a user re-running in ten minutes and one exporting a bundle.
    """
    from .scheduler import fmt_duration

    lines = [f"[bold]Stopped: {decision.message}[/]"]
    if decision.reason == _credit.REASON_FIVE_HOUR:
        resume_after = decision.payload.get("resume_after")
        wait = max(0.0, float(resume_after) - time.time()) if resume_after else 0.0
        lines += [
            f"The five-hour window reopens in about {fmt_duration(wait)}."
            if wait else "The five-hour window reopens shortly.",
            "",
            *_resume_lines(run_id, suffix="   (after the reset)"),
        ]
    else:
        lines += [
            "The seven-day window does not reopen for days — export the checkpoint and",
            "let someone running on their own account finish it.",
            "",
            f"  qualgent-bench checkpoint export {run_id}",
            "",
            "[dim]then, on the machine that will finish it:[/]",
            "  qualgent-bench checkpoint import <bundle> --runs-dir <its runs_dir>",
            *_resume_lines(run_id),
        ]
    lines += ["", f"[dim]{_credit.stop_path(runs_dir, run_id)} · exit {_credit.EXIT_STOPPED}[/]"]
    return Panel("\n".join(lines), title="run stopped — resumable", border_style="yellow")


def _check_resume_environment(resume: "_checkpoint.ResumePlan", apps: list[dict],
                              plan, mode: str, force: bool) -> None:
    """Refuse a resume whose environment moved under it — a different harness build,
    image, spec or APK measures a different thing, and blending the two under one run
    id makes the board unreadable. Only the apps with work left are compared: the ones
    already finished are not going to be re-run, whatever their specs say now.
    """
    current = _environment_now([s for s in apps if s["app"]["id"] in plan.apks], mode)
    diffs = _checkpoint.compatibility(resume.environment, current)
    if not diffs:
        return
    listed = "\n".join(f"    • {d}" for d in diffs)
    if not force:
        raise click.ClickException(
            f"Run {resume.run_id} was planned against a different environment:\n{listed}\n\n"
            f"  Resuming would put two different benchmarks under one run id. Restore\n"
            f"  the environment it was planned in, or pass --force-resume to accept it.")
    console.print(f"[yellow]--force-resume: continuing across an environment change[/]\n{listed}")


def _resume_line(run_id: str, resume: "_checkpoint.ResumePlan",
                 state: "_checkpoint.CheckpointState", plan) -> str:
    """One line of what the resume found, before the plan panel repeats the ETA."""
    parts = [f"{len(state.done_keys)} of {len(resume.units)} units already complete",
             f"{len(plan.units)} to run"]
    if state.orphans:
        parts.append(f"{len(state.orphans)} interrupted episode(s) to discard")
    if state.excluded:
        parts.append(f"{len(state.excluded)} excluded attempt(s) to re-run")
    return (f"[bold]Resuming {run_id}[/] · segment {resume.segment + 1} · "
            + " · ".join(parts))


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
    d = _checkpoint.run_meta_dir(runs_dir, run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _apk_sha256(app_id: str, suite: dict, mode: str) -> str | None:
    """The published sha256 of the APK this run resolves for `app_id` — the journey
    build in journey mode (journey-only defects live there), the hunt build otherwise.
    `None` when the resolved APK has no published hash (a local `dist/` build or a
    QUALGENTBENCH_APK_* override); a resume then cannot prove both machines ran the
    same bytes, which is exactly what the compatibility check should say."""
    if mode == "journey":
        from . import journey as _journey
        jmeta = _journey.apk_meta(app_id) or {}
        if jmeta.get("sha256"):
            return str(jmeta["sha256"])
    sha = ((suite or {}).get("apk") or {}).get("sha256")
    return str(sha) if sha else None


def _environment_now(apps: list[dict], mode: str) -> dict:
    """What this machine would run `apps` against, right now: harness version, image
    digest, and per app the spec hash and the APK hash.

    One function for both sides of the resume check — the value written into plan.json
    and the value compared against it later. Two readers would be free to drift, and a
    drift here reads as an environment change that never happened.
    """
    suites = [s for s in (apps or []) if (s.get("app") or {}).get("id")]
    ids = [str(s["app"]["id"]) for s in suites]
    spec_extra = None
    if mode in ("journey", "all"):
        from . import journey as _journey
        # Journey units and their oracles come from the test-case and truth files, not
        # from the benchmark spec, so the spec hash alone would call an edited case
        # unchanged.
        spec_extra = {app_id: {"cases": _journey.load_cases(app_id),
                               "truth": _journey.load_truth(app_id)} for app_id in ids}
    return _checkpoint.environment_fingerprint(
        suites,
        apk_sha256={app_id: _apk_sha256(app_id, suite, mode)
                    for app_id, suite in zip(ids, suites)},
        spec_extra=spec_extra,
    )


def _write_plan(runs_dir: Path, run_id: str, summary: dict, *,
                apps: list[dict] | None = None, mode: str = "guided", **meta) -> None:
    """The run's intent, written before the first episode. Carries an environment
    fingerprint (harness version, image digest, per-app spec + APK hashes) because a
    resume on another machine has to be able to prove it is measuring the same thing,
    and `segment` because a run id can now span more than one sitting.

    Written atomically like every other state file: a kill inside a plain `write_text`
    leaves a truncated plan, and a truncated plan is a run whose scope no longer
    exists while its finished episodes do."""
    fingerprint = _environment_now(apps or [], mode)
    _checkpoint.write_json(
        _run_meta_dir(runs_dir, run_id) / "plan.json",
        {"run_id": run_id, "mode": mode, "segment": 0,
         "environment": fingerprint, **meta, **summary})


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
            # Relative to the runs dir, like result.json — board.json travels with
            # the run dir, so an absolute path here would be dead on arrival.
            "artifact_dir": r.artifact_dir,
        })
    _checkpoint.write_json(
        _run_meta_dir(runs_dir, run_id) / "board.json",
        {"run_id": run_id,
         # The printed Bug-hunt table's numbers as data, one row per
         # (agent, model, condition) — the plotting-ready summary.
         "summary": _lb.hunt_summary(results),
         "journey_summary": __import__("qualgentbench.journey", fromlist=["summary"]).summary(results),
         "episodes": rows,
         "actual_wall_sec": round(sum(r.wall_time_sec for r in results))})


def _print_run_footer(results: list[RunResult], runs_dir: Path) -> None:
    """Print run cost and validity — a reader needs to know an episode is not
    quotable before reading the board, not by digging through result.json."""
    if not results:
        return
    wall = sum(r.wall_time_sec or 0 for r in results)
    cost = sum(r.metrics.get("cost_usd") or 0 for r in results)
    # Journey episodes are scored on their own terms (a truncated one is a
    # non-completion, not an unquotable result); only hunt/guided episodes feed the
    # "incomplete coverage" count below.
    journey = [r for r in results if r.task_type == "journey_case"]
    if journey:
        scored = [r for r in journey if r.metrics.get("completed") is not None]
        done = sum(1 for r in scored if r.metrics.get("completed"))
        cut = sum(1 for r in journey if r.metrics.get("truncated"))
        unscored = len(journey) - len(scored)
        # Completion goes unscored two ways now: a screen-text oracle (provable only
        # from the agent's own device text) and a db/content oracle the harness could
        # not evaluate. The second one is a harness fault, so name it separately —
        # reading it as "the agent was fine" is how verdict-only completion got
        # published once.
        dead_oracle = sum(1 for r in journey
                          if r.metrics.get("completed") is None
                          and (r.metrics.get("oracle") or {}).get("mode") in ("db", "content"))
        why = (f"{dead_oracle} oracle not evaluated" if dead_oracle == unscored
               else f"{dead_oracle} oracle not evaluated, {unscored - dead_oracle} screen-text oracle")
        console.print(f"[dim]journey: {done}/{len(scored)} completed"
                      f"{f' · {cut} truncated (scored as not completed)' if cut else ''}"
                      f"{f' · {unscored} completion unscored ({why})' if unscored else ''}[/]")
    trunc = sum(1 for r in results
                if r.task_type != "journey_case"
                and r.metrics.get("truncated") and (r.metrics.get("coverage") or 0) < 1.0)
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
    _replay_and_board(results, runs_dir)


def _replay_and_board(results, runs_dir: Path) -> None:
    """Verify each episode's reproductions, then print the hybrid board. Runs now,
    while the device and app snapshot are still fresh. Best-effort throughout —
    a replay failure never invalidates a completed run."""
    import subprocess
    root = Path(__file__).resolve().parents[2]
    dirs = []
    for r in results:
        # Only hunt episodes carry reproductions to replay; journey and guided
        # episodes are scored from their report alone.
        if getattr(r, "task_type", "") != "bug_hunt":
            continue
        d = resolve_artifact_dir(runs_dir, r)
        if d and (d / "result.json").exists():
            dirs.append(str(d))
    if not dirs:
        return

    # Each episode was already replayed as it finished. Re-run only the stale ones —
    # those verified under a different replayer — since a full pass costs real
    # device time and episodes must be comparable under one replayer.
    from .replay import replayer_fingerprint
    current = replayer_fingerprint()
    # An imported episode is a finished score whose heavy artifacts stayed on the
    # machine that ran it, so a stale fingerprint cannot be answered by re-replaying:
    # there is no app snapshot to restore and no evidence to check against, and the
    # replay would overwrite a real verdict with one derived from a device that never
    # saw the app. Its recorded verdict stands, and the board says so.
    results_only = _lb.results_only_dirs(runs_dir, results)
    stale, imported = [], []
    for d in dirs:
        rj = Path(d) / "replay.json"
        try:
            fresh = json.loads(rj.read_text()).get("replayer") == current
        except Exception:  # noqa: BLE001 — missing or unreadable means re-run it
            fresh = False
        if fresh:
            continue
        if Path(d).resolve() in results_only:
            imported.append(d)
        else:
            stale.append(d)

    local_total = len(dirs) - len(imported)
    console.print()
    for d in imported:
        logger.info("skipping re-replay of %s: imported, artifacts are not local", d)
    if imported:
        console.print(f"[dim]{len(imported)} episode(s) imported; artifacts are not "
                      f"local — keeping their recorded replay verdicts[/]")
    if not stale:
        if local_total:
            console.print(f"[dim]{local_total} episode(s) already verified by this "
                          f"replayer — nothing to re-run[/]")
    else:
        console.print(
            f"[dim]verifying {len(stale)} of {local_total} episode(s) by replaying "
            f"their reproductions — no model tokens"
            + (f" ({local_total - len(stale)} already current)" if len(stale) < local_total
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
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=_REPLAY_TIMEOUT_SEC)
            if proc.returncode != 0:
                # A crashed replay must be LOUD: a swallowed traceback once hid a
                # KeyError for two whole runs while episodes scored trust-0.
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                console.print(f"[red]replay failed[/] for {Path(d).name} "
                              f"(exit {proc.returncode}): " + " | ".join(tail))
                logger.error("replay subprocess failed for %s (exit %s): %s",
                             d, proc.returncode, "\n".join(tail))
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
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    done = {"n": 0}
    err_tail: list[str] = []

    def _read() -> None:
        for line in proc.stdout or ():
            if "→" in line:            # one per replayed reproduction
                done["n"] += 1

    def _read_err() -> None:
        # Keep the last lines only — enough to name a crash without buffering a log.
        for line in proc.stderr or ():
            err_tail.append(line.rstrip())
            del err_tail[:-5]

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    err_reader = threading.Thread(target=_read_err, daemon=True)
    err_reader.start()

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
    err_reader.join(timeout=2)
    if proc.returncode != 0:
        # A crashed replay must be LOUD — a swallowed KeyError once cost two runs
        # their verification while the episodes quietly scored trust-0.
        raise RuntimeError(f"replay subprocess exited {proc.returncode}: "
                           + (" | ".join(err_tail[-3:]) or "no stderr"))


def _verify_episode(result: RunResult, progress=None, *,
                    runs_dir: Path) -> tuple[str, list[str]]:
    """Replay one episode's reproductions while the device state is still fresh and
    write the verified score back. Returns (status text, detail lines) for the
    caller to print. An unverifiable claim only lowers recall — excluding the
    episode would reward deleting the evidence, so exclusion is for non-results only."""

    from .failures import exclusion_reason
    from .hybrid_score import combine
    from .replay_score import score as replay_score

    root = Path(__file__).resolve().parents[2]
    run_dir = resolve_artifact_dir(runs_dir, result)
    if run_dir is None or not (run_dir / "result.json").exists():
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
        # LOUD but non-fatal: the episode falls back to key-only scoring with
        # trust 0 (visible on the board), never to exclusion — excluding a valid
        # QA result over a replayer crash would reward losing the evidence.
        console.print(f"[red]verification failed[/] for {name}: {exc}")
        logger.error("replay failed for %s: %s", run_dir, exc)

    rj = run_dir / "replay.json"
    rs, unver, detail = None, 0, []
    features, derived = [], {}
    try:
        from .bugs import load_suite
        spec = (root / "src" / "qualgentbench" / "data" / "benchmarks" / f"{app}.yaml")
        features = load_suite(spec)["exploration"]["features"]
        truth_seen = False
        for f in (f"{t}-stability.json" for t in ALL_TIERS):
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
            seeded_areas = {f.get("id") for f in features if str(f.get("state")) == "broken"}
            for r in res:
                if (r.get("claimed") == "deviates"
                        and r.get("classification") == "unreplayable"
                        and r.get("area") in seeded_areas
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
        rp = run_dir / "result.json"
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
@click.option("--mode", type=click.Choice(["guided", "hunt", "journey", "all"]),
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
@click.option("--resume", "resume_run_id", default=None, metavar="RUN_ID",
              help="Continue an interrupted run instead of starting one: takes the "
                   "agent, model, mode, trials and the frozen unit list from that "
                   "run's plan.json, skips the units it already finished, discards "
                   "its interrupted episodes, and runs on under the same run id. "
                   "Cannot be combined with the scope flags; --devices/--lanes/"
                   "--mcp-server/--runs-dir may differ from the original run.")
@click.option("--force-resume", is_flag=True,
              help="Resume even though the environment fingerprint (harness version, "
                   "image digest, spec or APK hashes) no longer matches the plan. The "
                   "run id then covers two different benchmarks — say so when quoting it.")
@click.option("--run-id-file", "run_id_file", default=None,
              type=click.Path(dir_okay=False, path_type=Path),
              help="Write this run's id to this file the moment it is known, one line. "
                   "The launcher loop reads it to build `--resume <run_id>` for the next "
                   "segment, so containerised runs must point it inside --runs-dir, "
                   "where the host can see it.")
@click.option("--stop-at-seven-day-pct", "stop_at_seven_day_pct", default=None,
              # 1-100, not 0-100: 0 reads as "off" and means "stop at 0% used", which
              # stops a healthy sweep immediately. 100 is how you say "off".
              type=click.IntRange(1, 100), envvar="QGB_STOP_AT_7D_PCT",
              help="Stop the sweep once the agent's highest WEEKLY subscription "
                   "window reaches this percentage (1-100; 100 = only when the window "
                   "is spent, which is the default) — the generic seven-day window and "
                   "any model-scoped weekly cap beside it — instead of running it to "
                   "the wall: "
                   "in-flight episodes finish, the board is written, and the run exits "
                   "75 with _runs/<run_id>/stop.json so `--resume` can finish it later "
                   "— on another machine and another account if you like. Overrides "
                   "`checkpoint.stop_at_seven_day_pct` in --config. Only claude-code on "
                   "subscription auth reports these windows; everything else ignores it.")
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
    resume_run_id: str | None,
    force_resume: bool,
    run_id_file: Path | None,
    stop_at_seven_day_pct: int | None,
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
    if resume_run_id:
        _reject_scope_flags_on_resume(click.get_current_context())
    device_list = [d.strip() for d in (devices or "").split(",") if d.strip()] or None
    # The credit policy survives a resume, unlike the scope: it is about the account
    # running the sweep now, not about what the sweep is measuring.
    credit_policy = Checkpoint()
    if config_path is not None:
        cfg = _load_config_or_exit(config_path)
        _load_env_file(cfg, config_path.parent)
        if not resume_run_id:
            # On a resume the scope is the plan's, so the file's scope is ignored —
            # the launcher passes the same --config on every iteration of its loop.
            agent, models, mode, trials = cfg.agent, cfg.model, cfg.scope.mode, cfg.scope.trials
            tier_filter = ",".join(cfg.scope.tiers) or None
            app_filter = ",".join(cfg.scope.apps) or None
            if agent not in ADAPTER_REGISTRY:
                raise click.ClickException(
                    f"unknown agent {agent!r} in {config_path}; one of "
                    f"{', '.join(sorted(ADAPTER_REGISTRY))}")
        # Explicit flags win over the file — the launcher uses them to point the
        # container at its own mounts and at the host's MCP server.
        mcp_server = mcp_server or cfg.mcp_server
        runs_dir = runs_dir or cfg.runs_dir
        device_list = device_list or cfg.devices.serials or None
        lanes = lanes or cfg.devices.max_lanes
        credit_policy = cfg.checkpoint
    runs_path = Path(runs_dir or "runs")
    resume_plan = None
    if resume_run_id:
        # Read the plan here, not deep in the run: a bad run id or a runs dir pointing
        # somewhere else should fail in a second, and everything downstream (preflight
        # included) has to see the agent and model the run actually used.
        try:
            resume_plan = _checkpoint.load_plan(runs_path, resume_run_id)
        except _checkpoint.CheckpointError as exc:
            raise click.ClickException(str(exc)) from exc
        agent, mode, trials = resume_plan.agent or agent, resume_plan.mode, resume_plan.trials
        models = resume_plan.model
        if agent not in ADAPTER_REGISTRY:
            raise click.ClickException(
                f"{resume_plan.path} names agent {agent!r}, which this harness does not "
                f"have; one of {', '.join(sorted(ADAPTER_REGISTRY))}")
    if stop_at_seven_day_pct is not None:
        # Flag and env beat the file: the launcher passes the same --config on every
        # iteration of its loop and needs a way to tighten the budget without editing it.
        credit_policy = credit_policy.model_copy(
            update={"stop_at_seven_day_pct": stop_at_seven_day_pct})
    _gate_unready_tiers(tier_filter, app_filter, mode)
    model_list = [m.strip() for m in (models or "").split(",") if m.strip()] or None
    _run_async(_leaderboard_bugs(
        model_list, agent, trials, mcp_server, runs_path,
        push_sheet, webhook_url, token, app_filter, mode, device, tier_filter,
        devices=device_list, lanes=lanes, plain=plain or None, yes=yes,
        resume=resume_plan, force_resume=force_resume, credit_policy=credit_policy,
        run_id_file=run_id_file,
    ))


# The flags that say WHAT to run. A resume takes all of them from plan.json, so
# passing one is a contradiction, not a preference: silently ignoring it would run a
# different benchmark than the one asked for.
_SCOPE_FLAGS = {"models": "--models", "app_filter": "--app", "tier_filter": "--tier",
                "mode": "--mode", "trials": "--trials"}


def _reject_scope_flags_on_resume(ctx: click.Context) -> None:
    from click.core import ParameterSource

    named = [flag for param, flag in _SCOPE_FLAGS.items()
             if ctx.get_parameter_source(param) not in (None, ParameterSource.DEFAULT)]
    if named:
        raise click.UsageError(
            f"--resume takes the scope from the run's plan.json, so it cannot be "
            f"combined with {', '.join(sorted(named))}.\n"
            f"  Drop {'those flags' if len(named) > 1 else 'that flag'} to finish the "
            f"run as planned, or omit --resume to start a new one.\n"
            f"  (--devices, --lanes, --mcp-server and --runs-dir may be changed on a "
            f"resume.)", ctx=ctx)


# Tiers hardened for hunt mode. Everything else still carries leaky briefs and
# underived budgets, so its scores mean nothing.
# hard joined 2026-08-31: 12 apps ×3-derived, gates green, APKs published; its
# budgets are still the corpus-wide default — re-derive before quoting speed.
READY_TIERS = {"easy", "medium", "hard"}

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
    """Refuse a `--tier` run that names an unready tier — fail in a second, not after
    real device time produces incomparable numbers. Guided mode is exempt: it is the
    authoring harness that produces the very budgets this gate demands. An unready
    app named with `--app` is NOT refused, only warned about (`_run`): a new app
    cannot go tier-ready without at least one device episode."""
    from . import bugs as bugmod

    if mode in ("guided", "journey") or not tier_filter:
        return

    blocked = sorted(parse_tiers(tier_filter) - READY_TIERS)
    if not blocked:
        return

    raise click.ClickException(
        f"🚧 I am working on it — the {'/'.join(blocked)} "
        f"tier{'s are' if len(blocked) > 1 else ' is'} not ready yet.\n"
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
                     models: list[str] | None = None, *,
                     credit_policy: Checkpoint | None = None) -> None:
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

    _print_credit_guard_status(agent, models, credit_policy)


def _print_credit_guard_status(agent: str, models: list[str] | None,
                               credit_policy: Checkpoint | None) -> None:
    """Say whether the sweep's budget is actually being watched.

    A user who set a seven-day stop threshold and is quietly running on an
    ANTHROPIC_API_KEY would otherwise believe the run will stop itself — the events
    the guard reads exist only on subscription auth, and their absence looks exactly
    like a healthy account. Silence here would be the wrong default.
    """
    from .adapters.claude_code import ClaudeCodeAdapter

    threshold = (credit_policy or Checkpoint()).stop_at_seven_day_pct
    if agent != "claude-code":
        # Other adapters report no windows at all; only say so when a threshold was
        # asked for, or it is noise on every run.
        if threshold < _credit.DEFAULT_STOP_AT_SEVEN_DAY_PCT:
            console.print(f"[yellow]--stop-at-seven-day-pct is ignored for --agent "
                          f"{agent}[/]: it reports no usage windows.")
        return
    if note := ClaudeCodeAdapter.credit_guard_note((models or [None])[0]):
        console.print(f"[dim]{note}.[/]")
        return
    console.print(f"[dim]credit guard active — stopping at {threshold}% of the "
                  f"highest weekly window; a five-hour limit stops and resumes.[/]")


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
    resume: "_checkpoint.ResumePlan | None" = None,
    force_resume: bool = False,
    credit_policy: Checkpoint | None = None,
    run_id_file: Path | None = None,
) -> None:
    """Run the benchmark. The MCP server, if any, is the caller's to run."""
    await _run_bugs(models, agent, trials, mcp_server, runs_dir, push_sheet,
                    webhook_url, token, app_filter, mode, device, tier_filter,
                    devices=devices, lanes=lanes, plain=plain, yes=yes,
                    resume=resume, force_resume=force_resume, credit_policy=credit_policy,
                    run_id_file=run_id_file)


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
    resume: "_checkpoint.ResumePlan | None" = None,
    force_resume: bool = False,
    credit_policy: Checkpoint | None = None,
    run_id_file: Path | None = None,
) -> None:
    from . import leaderboard as lb
    from .session import DeviceSession

    # Raw needs no bridge, so its session must be adb-only too — otherwise preflight
    # reports no device while the emulator sits right there.
    session = DeviceSession(mcp_server)
    # Fail in seconds with instructions, not deep into the run with a traceback.
    await _preflight(session, mcp_server, agent, device, models,
                     credit_policy=credit_policy)
    if mcp_server and not await session.is_healthy():
        console.print(f"[red]MCP server not reachable at {mcp_server}.[/] Start MCP.")
        sys.exit(1)
    if not await session.first_available_device():
        console.print("[red]No device found.[/] Boot an Android emulator/simulator first.")
        sys.exit(1)

    models = models or _agent_models(agent)
    try:
        collected = await _run_episodes(
            models, agent, session, mcp_server, runs_dir, trials, app_filter, mode, device,
            tier_filter=tier_filter, devices=devices, lanes=lanes, plain=plain, yes=yes,
            resume=resume, force_resume=force_resume, credit_policy=credit_policy,
            run_id_file=run_id_file,
        )
    except _credit.RunStopped as stopped:
        # Out of provider budget with work left. Everything is already on disk — the
        # episodes, the board and stop.json — so this only has to be legible and exit
        # 75, the one code that tells the launcher "resume me, do not retry me".
        if stopped.results:
            _print_bug_summary(stopped.results)
        console.print(_stop_panel(stopped.decision, runs_dir, stopped.run_id))
        sys.exit(_credit.EXIT_STOPPED)
    if not collected:
        console.print("[red]No bug runs completed.[/]")
        sys.exit(1)

    # Everything, not just guided: _print_bug_summary splits hunt from guided itself.
    _print_bug_summary(collected)
    if push_sheet:
        k_values = (1, trials) if trials > 1 else (1,)
        rows = lb.aggregate_by_model(collected, k_values=k_values)
        _push_leaderboard(rows, _result_paths(runs_dir, collected), webhook_url, token)


@main.command("show")
@click.option("--runs-dir", default="runs", show_default=True,
              help="Directory containing prior result.json artifacts.")
@click.option("--models", default=None,
              help="Comma-separated model ids or short names to include.")
@click.option("--agent", default="native", type=click.Choice(list(ADAPTER_REGISTRY)),
              show_default=True,
              help="Only include runs from this agent.")
@click.option("--mode", type=click.Choice(["guided", "hunt", "journey", "all"]),
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
        "journey": {"journey_case"},
        "all": {"bug_task", "clean_task", "bug_hunt", "journey_case"},
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
    _print_imported_note(Path(runs_dir), results)
    if push_sheet:
        k_values = (1, trials) if trials > 1 else (1,)
        rows = _lb.aggregate_by_model(results, k_values=k_values)
        _push_leaderboard(rows, _result_paths(Path(runs_dir), results), webhook_url, token)


# Leaderboard columns rendered in the Sheet tab — (row key, header, lower-is-better).
_LEADERBOARD_METRICS = [
    ("pass_rate", "Pass rate (%)", False),
    ("avg_wall_time_sec", "Avg time (s)", True),
    ("avg_device_tool_calls", "Avg tool calls", True),
    # Cost + tokens intentionally excluded from the sheet.
]


def _print_imported_note(runs_dir: Path, results: list[RunResult]) -> None:
    """Footer count of episodes on this board that came from a checkpoint bundle.

    Their numbers are as recorded on the machine that ran them; the artifacts that
    would let this machine re-derive them are not here. A board reader has to be
    able to tell those rows apart from ones this machine can re-verify.
    """
    imported = _lb.results_only_dirs(runs_dir, results)
    if not imported:
        return
    console.print(
        f"[dim]{len(imported)} of {len(results)} episode(s) imported from a checkpoint "
        f"bundle — scored on the machine that ran them; their artifacts are not local, "
        f"so they cannot be re-verified here.[/]")


def _avg_metric(rs: list[RunResult], key: str) -> float:
    vals = [r.metrics.get(key) for r in rs if isinstance(r.metrics.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def _print_bug_summary(results: list[RunResult]) -> None:
    """Print the seeded-bug leaderboard, split by episode kind so guided rows
    aren't mis-counted as hunts."""
    hunts = [r for r in results if r.task_type == "bug_hunt"]
    bug_tasks = [r for r in results if r.task_type == "bug_task"]
    clean_tasks = [r for r in results if r.task_type == "clean_task"]
    journeys = [r for r in results if r.task_type == "journey_case"]
    if hunts:
        _print_hunt_table(hunts)
    if journeys:
        _print_journey_table(journeys)
    if bug_tasks or clean_tasks:
        _print_guided_table(bug_tasks, clean_tasks)


def _print_journey_table(results: list[RunResult]) -> None:
    """Journey board: two numbers, never blended — completion (instruction
    following, verified on the device) and bug finding (found / present, false
    reports, F1 from the totals)."""
    from . import journey as _journey
    from .failures import is_excluded

    def pct(v):
        return "—" if v is None else f"{v:.0%}"

    rows = _journey.summary(results)
    excluded = sum(1 for r in results if is_excluded(r.metrics or {}))
    table = Table(title="Test-case runs — bug finding (ranked) and completion")
    # `Episodes` is scored/PLANNED, because an excluded episode is invisible in every
    # other column: one real run scored 14 of 30 planned episodes (an exhausted account
    # ate the rest) and the board said "14 episodes". `Cut` is truncation, which scores
    # as not completed AND as every seeded bug missed, so it belongs beside both numbers.
    # Completion carries its unscored count in the same cell — a percentage over 15 of 34
    # episodes is not the same claim as one over 34.
    for col, just in (("#", "right"), ("Agent + Model", "left"), ("Arm", "left"),
                      ("Episodes", "right"), ("Cut", "right"),
                      ("Done clean", "right"), ("Done seeded", "right"),
                      ("Completion", "right"), ("Bugs found", "right"), ("False rep.", "right"),
                      ("Prec.", "right"), ("Recall", "right"), ("F1", "right"),
                      ("Steps", "right")):
        table.add_column(col, justify=just)
    for i, row in enumerate(rows, 1):
        eps = (f"{row['episodes']}/[yellow]{row['planned_episodes']}[/]"
               if row["excluded_episodes"] else str(row["episodes"]))
        completion = f"[bold]{pct(row['completion'])}[/]"
        if row["completion_unscored"]:
            completion += f" [dim]({row['completion_unscored']} un)[/]"
        table.add_row(str(i), f"{row['agent']} · {row['model']}", row["condition"], eps,
                      f"[yellow]{row['truncated']}[/]" if row["truncated"] else "0",
                      f"{row['clean_completed']}/{row['clean_episodes']}",
                      f"{row['seeded_completed']}/{row['seeded_episodes']}",
                      completion,
                      f"{row['bugs_found']}/{row['bugs_present']}", str(row["false_reports"]),
                      pct(row["precision"]), pct(row["recall"]), f"[bold]{pct(row['f1'])}[/]",
                      "—" if row["avg_steps"] is None else f"{row['avg_steps']:.0f}")
    console.print(table)
    console.print("[dim]Episodes = scored/planned · Cut = step budget exhausted (not completed, "
                  "all seeded bugs missed) · (N un) = completion unscored[/]")
    console.print("[dim]ranked by F1 — completion is partly unscored by design (an oracle the "
                  "harness could not evaluate, or one provable only from the agent's own device "
                  "text), so it does not rank the board[/]")
    if excluded:
        console.print(f"[dim]{excluded} episode(s) excluded from every number above "
                      f"(env/infra failure, contamination or rate limit)[/]")

    apps = _journey.summary(results, by_app=True)
    if len({r["app"] for r in apps}) > 1:
        t2 = Table(title="Per app")
        for col, just in (("App", "left"), ("Excl.", "right"), ("Trunc.", "right"),
                          ("Done clean", "right"), ("Done seeded", "right"),
                          ("Bugs found", "right"), ("False rep.", "right"), ("F1", "right")):
            t2.add_column(col, justify=just)
        for row in sorted(apps, key=lambda r: r["app"]):
            t2.add_row(row["app"], str(row["excluded_episodes"]), str(row["truncated"]),
                       f"{row['clean_completed']}/{row['clean_episodes']}",
                       f"{row['seeded_completed']}/{row['seeded_episodes']}",
                       f"{row['bugs_found']}/{row['bugs_present']}", str(row["false_reports"]),
                       pct(row["f1"]))
        console.print(t2)

    per_case: dict[str, list[RunResult]] = {}
    for r in results:
        if not is_excluded(r.metrics or {}):
            per_case.setdefault(r.task_id, []).append(r)
    detail = Table(title="Per episode")
    for col in ("Case", "Version", "Expected", "Reported", "Completed", "Oracle",
                "Bugs", "False rep.", "Steps"):
        detail.add_column(col, justify="left" if col in ("Case", "Version", "Oracle") else "right")
    for tid, rs in sorted(per_case.items()):
        for r in rs:
            m = r.metrics or {}
            case, version = _journey.split_task_id(tid)
            done = m.get("completed")
            oracle = m.get("oracle") or {}
            # An episode whose completion is UNSCORED is neither yes nor no, and the
            # oracle column says which way it was unscored — a null `ok` on a db oracle
            # is a harness fault, not an agent result.
            ok = oracle.get("ok")
            detail.add_row(case, m.get("version") or version, str(m.get("expected_verdict")),
                           str(m.get("reported_verdict")),
                           "[green]yes[/]" if done else ("[dim]—[/]" if done is None else "[red]no[/]"),
                           f"{oracle.get('mode') or '—'}:"
                           f"{'holds' if ok else ('—' if ok is None else 'violated')}",
                           f"{len(m.get('bugs_found') or [])}/{len(m.get('bugs_present') or [])}",
                           str(m.get("false_reports")),
                           # A truncated episode scores as not completed and as every
                           # seeded bug missed — the step count is the reason, so say so
                           # on the step count.
                           (f"[yellow]{m.get('hook_steps') or m.get('steps')} (cut)[/]"
                            if m.get("truncated") else str(m.get("hook_steps") or m.get("steps"))))
    console.print(detail)


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


def _result_paths(runs_dir: Path, results: list[RunResult]) -> list[Path]:
    """On-disk result.json for each result, resolved against the runs dir (results
    store their episode dir relative; pre-relative ones store it absolute)."""
    out = []
    for r in results:
        d = resolve_artifact_dir(runs_dir, r)
        if d is not None:
            out.append(d / "result.json")
    return out


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




# ── qualgent-bench checkpoint ─────────────────────────────────────────────────

@main.group("checkpoint")
def checkpoint_group() -> None:
    """Hand a run to another machine: export, import and inspect checkpoints.

    A checkpoint carries RESULTS ONLY — the plan, the schedule and every completed
    episode's small scoring files. Agent config homes, transcripts, evidence and app
    snapshots stay on the machine that produced them, so a bundle can be sent to
    someone who will finish the run on their own credentials.
    """


def _checkpoint_or_exit(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except _checkpoint.CheckpointError as exc:
        raise click.ClickException(str(exc)) from exc


@checkpoint_group.command("export")
@click.argument("run_id")
@click.option("-o", "--output", default=None, type=click.Path(path_type=Path),
              help="Bundle path, or a directory to write the default name into. "
                   "Default: ./qgb-checkpoint-<run_id>-seg<N>.tar.gz")
@click.option("--runs-dir", default="runs", show_default=True,
              type=click.Path(path_type=Path),
              help="Directory holding _runs/<run_id> and the episode dirs.")
def checkpoint_export(run_id: str, output: Path | None, runs_dir: Path) -> None:
    """Pack RUN_ID's completed episodes into a portable bundle.

    Interrupted episodes are moved to runs/_discarded/ first, so a partial episode
    can never ship as a result. The export aborts, writing nothing, if any packed
    byte looks like a credential.
    """
    result = _checkpoint_or_exit(_checkpoint.export_bundle, runs_dir, run_id,
                                 output=output)
    m, c = result.manifest, result.counts
    if result.discarded:
        console.print(f"[yellow]Discarded {len(result.discarded)} interrupted "
                      f"episode(s)[/] → {runs_dir / _checkpoint.DISCARDED_DIR / run_id}")
    console.print(f"[green]Exported[/] {result.path}")
    console.print(f"  run {m['run_id']} · segment {m['segment']} · "
                  f"{m['agent']} {m['model']} · {m['mode']}")
    console.print(f"  done {c['done']} · remaining {c['remaining']} · "
                  f"excluded {c['excluded']} · discarded {c['discarded']} · "
                  f"{c['files']} files")
    console.print(f"[dim]  finish it elsewhere: qualgent-bench checkpoint import "
                  f"{result.path.name}[/]")


@checkpoint_group.command("import")
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--runs-dir", default="runs", show_default=True,
              type=click.Path(path_type=Path),
              help="Directory to lay the run into.")
def checkpoint_import(bundle: Path, runs_dir: Path) -> None:
    """Lay BUNDLE's results under --runs-dir and print how to resume the run.

    Every file is checked against the manifest's sha256 and re-scanned before it is
    written. An existing run with different contents is refused, never merged.
    """
    result = _checkpoint_or_exit(_checkpoint.import_bundle, bundle, runs_dir)
    m = result.manifest
    console.print(f"[green]Imported[/] run {result.run_id} (segment {m['segment']}, "
                  f"exported by {m.get('host') or '?'}) → {runs_dir}")
    console.print(f"  {len(result.episodes)} episode(s), {len(result.written)} files, "
                  f"{m['counts']['remaining']} unit(s) still to run")
    # Launcher first: whoever is reading this was handed a bundle, so they have no
    # emulators booted and no adb wired up — which is the half `qualgent-bench run
    # --resume` leaves to the reader and `scripts/launch.py --resume` does for them.
    console.print("\n  Resume with:")
    console.print(f"    [bold]python3 scripts/launch.py {LAUNCHER_CONFIG} "
                  f"--resume {result.run_id}[/]")
    if str(runs_dir) != "runs":
        console.print(f"[dim]      needs `runs_dir: {runs_dir}` in {LAUNCHER_CONFIG} — "
                      f"the launcher takes the runs dir from the config, not a flag[/]")
    console.print(f"\n  or, with emulators you booted yourself:\n    {result.resume_command}")


@checkpoint_group.command("show")
@click.argument("target")
@click.option("--runs-dir", default="runs", show_default=True,
              type=click.Path(path_type=Path),
              help="Where to look when TARGET is a run id rather than a bundle.")
@click.option("--json", "as_json", is_flag=True, help="Print the manifest as JSON.")
def checkpoint_show(target: str, runs_dir: Path, as_json: bool) -> None:
    """Print the manifest and the remaining units of a bundle or a run id."""
    view = _checkpoint_or_exit(_checkpoint.describe, target, runs_dir=runs_dir)
    if as_json:
        click.echo(json.dumps(view, indent=2))
        return

    counts = view.get("counts") or {}
    scope = view.get("scope") or {}
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    for label, value in (
        ("run", view.get("run_id")),
        ("segment", view.get("segment")),
        ("agent / model", f"{view.get('agent')} {view.get('model')}"),
        ("mode / trials", f"{view.get('mode')} · {view.get('trials')} trial(s)"),
        ("scope", (f"{', '.join(scope.get('apps') or []) or '—'} "
                   f"({scope.get('episodes', '?')} episodes)")),
        ("harness", (f"{view.get('package_version')} · "
                     f"image {view.get('image_digest') or 'none'}")),
        ("exported", f"{view.get('created_at')} on {view.get('host')}"),
        ("counts", " · ".join(f"{k} {v}" for k, v in counts.items())),
    ):
        table.add_row(label, str(value))
    console.print(Panel(table, title="checkpoint", border_style="cyan"))

    remaining = view.get("remaining") or []
    if not remaining:
        console.print("[green]Nothing remaining — the run is complete.[/]")
        return
    units = Table(title=f"Remaining units ({len(remaining)})", show_lines=False)
    for column in ("app", "task", "kind", "trial"):
        units.add_column(column)
    for unit in remaining[:_CHECKPOINT_SHOW_LIMIT]:
        units.add_row(str(unit.get("app") or "—"), str(unit.get("task") or "—"),
                      str(unit.get("kind") or "—"), str(unit.get("trial")))
    console.print(units)
    if len(remaining) > _CHECKPOINT_SHOW_LIMIT:
        console.print(f"[dim]… {len(remaining) - _CHECKPOINT_SHOW_LIMIT} more "
                      f"(use --json for all)[/]")
