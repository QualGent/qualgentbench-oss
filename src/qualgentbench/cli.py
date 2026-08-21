"""qualgent-bench CLI — entry point for all benchmark commands."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
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

    failures = 0
    for r in results:
        if r.passed:
            icon = "[green]✓[/]"
        elif r.warning:
            icon = "[yellow]⚠[/]"
        else:
            icon = "[red]✗[/]"
            failures += 1
        console.print(f"  {icon}  {r.name:<28} {r.detail}")
        if not r.passed and r.fix:
            console.print(f"     [dim]→ {r.fix}[/]")

    console.print()
    if failures == 0:
        console.print("[green]All checks passed.[/]" if not any(r.warning for r in results)
                      else "[yellow]Ready with warnings.[/]")
    else:
        console.print(f"[red]{failures} issue{'s' if failures != 1 else ''} found.[/] "
                      "Fix them before running tasks.")
    sys.exit(0 if failures == 0 else 1)


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


class _EpisodeProgress:
    """Live status line for one episode: polls the budget hook's counter file, since
    the agent subprocess prints nothing for minutes. Nothing here touches the run."""

    def __init__(self, console, label: str, budget: int | None):
        self._console = console
        self._label = label
        self._budget = budget
        self._count_file: Path | None = None
        self._run_dir: Path | None = None
        self._status = None
        self._task: asyncio.Task | None = None
        self._started = 0.0

    def set_run_dir(self, run_dir: Path) -> None:
        """Called by the runner once the run directory exists."""
        self._run_dir = run_dir

    def _resolve_count_file(self) -> "Path | None":
        """Find the counter file, re-checking until it appears — its location varies
        per agent and it is created well after the run dir, so a one-time resolve
        would cache None forever."""
        if self._count_file and self._count_file.exists():
            return self._count_file
        if self._run_dir is None:
            return None
        for candidate in (self._run_dir / "hooks" / "count",
                          self._run_dir / "codex_home" / "hooks" / "count"):
            if candidate.exists():
                self._count_file = candidate
                return candidate
        return None

    def _steps(self) -> int | None:
        count_file = self._resolve_count_file()
        if count_file is None:
            return None
        try:
            return int(count_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def _text(self) -> str:
        elapsed = int(time.monotonic() - self._started)
        clock = f"{elapsed // 60}m{elapsed % 60:02d}s"
        steps = self._steps()
        if steps is None:
            return f"{self._label} — starting… · {clock}"
        of = f"/{self._budget}" if self._budget else ""
        return f"{self._label} — {steps}{of} steps · {clock}"

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self._status is not None:
                self._status.update(self._text())

    async def __aenter__(self) -> "_EpisodeProgress":
        self._started = time.monotonic()
        self._status = console.status(self._text(), spinner="dots")
        self._status.__enter__()
        self._task = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._status is not None:
            self._status.__exit__(*exc)
            self._status = None


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
) -> list[RunResult]:
    """Run the selected apps sequentially: install each buggy APK once, then run
    hunt and/or guided episodes per ``mode``, with a fresh app reset before every
    episode. Tooling is "mcp" or "raw"; neither arm gets the app's source."""
    from . import bugs as bugmod
    from .episode_runner import (
        EpisodeOptions,
        prepare_app,
        run_episode,
    )

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

    device = device or await session.first_available_device()
    if not device:
        console.print("[red]No device available.[/]")
        return []
    console.print(f"[dim]device: {device}[/]")

    tiers = {}
    for s in apps:
        tiers.setdefault(s["app"].get("difficulty", "?"), 0)
        tiers[s["app"].get("difficulty", "?")] += 1
    console.print(Panel.fit(
        f"[bold]Apps:[/] {len(apps)}  "
        f"[dim]({', '.join(f'{k}:{v}' for k, v in sorted(tiers.items()))})[/]\n"
        f"[bold]Models:[/] {len(models)}  [bold]Trials:[/] {trials}  [bold]Mode:[/] {mode}",
        title="QualGentBench",
    ))

    out: list[RunResult] = []
    # Ctrl+C still leaves a usable result: finished episodes are already scored
    # on disk, so print the board over whatever completed.
    try:
        for suite in apps:
            app = suite["app"]
            hunt_task = bugmod.exploration_task(suite)
            apk = _resolve_app_apk(app, suite)
            if not apk.exists():
                # Authored but never published — no `apk:` block, nothing to download.
                env_var = "QUALGENTBENCH_APK_" + app["id"].upper().replace("-", "_")
                console.print(
                    f"[yellow]Skipping {app['name']}: no APK available.[/]\n"
                    f"  This app has no published `apk:` block in its spec, so it cannot be\n"
                    f"  downloaded. Either:\n"
                    f"    build it:  uv run python scripts/build_app.py {app['id']}\n"
                    f"    or point at one:  export {env_var}=/path/to.apk\n"
                    f"  See scripts/build_app.py."
                )
                continue

            # No source in the agent's cwd: bugs must be found on the device,
            # not diagnosed from the (buggy) code.
            source_dir: Path | None = None

            console.rule(f"[bold]{app['name']}[/] [dim]({app.get('difficulty', '?')}, "
                         f"{hunt_task.bug_spec['total_bugs']} bugs)[/]")
            try:
                await session.force_release()
                bundle_id = await prepare_app(session, device, apk, hunt_task)
            except (RuntimeError, OSError) as exc:
                console.print(f"[red]{app['name']} install failed:[/] {exc} — skipping.")
                continue
            hunt_task.bundle_id = bundle_id

            # Episodes for this app, per mode. Each is (task, verdict_fn, task_type, label).
            episodes: list[tuple] = []
            if mode in ("all", "hunt"):
                episodes.append((hunt_task, bugmod.exploration_verdict, "bug_hunt", "hunt"))
            if mode in ("all", "guided"):
                for gt in bugmod.suite_tasks(suite):
                    gt.bundle_id = bundle_id
                    kind = str((gt.bug_spec or {}).get("type", "bug"))
                    episodes.append((gt, bugmod.guided_verdict, f"{kind}_task", gt.id))

            for model in models:
                resolved = _resolve_model(agent, model)
                for task, verdict_fn, task_type, label in episodes:
                    for trial in range(1, trials + 1):
                        budget = (task.bug_spec or {}).get("step_budget")
                        tracker = _EpisodeProgress(
                            console, f"{resolved} · {label} · trial {trial}/{trials}", budget)
                        opts = EpisodeOptions(
                            agent=agent, model=resolved,
                            condition=Condition.no_routines, trial=trial,
                            mcp_server=mcp_server, runs_dir=runs_dir,
                            verdict_fn=verdict_fn, task_type=task_type,
                            device_serial=device,
                            tooling=("mcp" if mcp_server else "raw"), source_dir=source_dir,
                            # Reinstall this build before every trial.
                            apk_path=apk,
                            # Pin the CLI agent's model — otherwise it runs its own
                            # default and the model column is fiction.
                            force_model=resolved,
                            on_run_dir=tracker.set_run_dir,
                        )
                        try:
                            async with tracker:
                                result = await run_episode(task, opts)
                            out.append(result)
                            _print_bug_run_line(task_type, result)
                            if task_type == "bug_hunt":
                                _verify_episode(result)
                        except RuntimeError as exc:
                            console.print(f"[red]ERROR {resolved} {label}:[/] {exc}")
    finally:
        if out:
            _print_run_footer(out)
    return out


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
                            device: str | None = None) -> None:
    """Replay one episode's reproductions, showing how many are done by counting
    replay_findings.py's one-line-per-claim output. The spinner ticks on its own,
    so a claim that takes minutes doesn't look like a hang."""
    import threading

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
    with console.status("", spinner="dots") as status:
        while proc.poll() is None:
            elapsed = int(time.monotonic() - started)
            status.update(f"verifying {app} · {done['n']}{of} reproductions · "
                          f"{elapsed // 60}m{elapsed % 60:02d}s")
            time.sleep(0.5)
            if elapsed > _REPLAY_TIMEOUT_SEC:
                proc.kill()
                raise TimeoutError(
                    f"replay exceeded {_REPLAY_TIMEOUT_SEC // 60} minutes")
    reader.join(timeout=2)


def _verify_episode(result: RunResult) -> None:
    """Replay one episode's reproductions and print its scored line while the device
    state is still fresh. An unverifiable claim only lowers recall — excluding the
    episode would reward deleting the evidence, so exclusion is for non-results only."""

    from .hybrid_score import combine
    from .replay_score import score as replay_score

    root = Path(__file__).resolve().parents[2]
    run_dir = Path(getattr(result, "artifact_dir", "") or "")
    if not (run_dir / "result.json").exists():
        return
    m = result.metrics or {}
    app = m.get("app_id", "?")
    name = f"{app} · {m.get('condition') or '?'}"

    # Same exclusion predicate the board and `show` use, so terminal and board agree.
    reason = ""
    if m.get("env_failure"):
        reason = "env_failure — killed before reporting"
    elif m.get("infra_failure"):
        reason = "infra_failure — never reached the device"
    elif m.get("contaminated"):
        reason = "contaminated — reached the answer key"
    # A generic `failure_reason` is NOT an exclusion — a claimed-but-unexercised
    # defect is a QA result. Only the three non-results above leave the board.
    try:
        _run_replay_with_status(root, run_dir, app,
                                len(m.get('repro_claims') or []) or None,
                                device=m.get("device_serial"))
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

    console.print(
        f"  [bold]{name:34}[/] steps={h.steps:>4}  F1={h.f1:>5.2f}  "
        f"FP={h.fp_rate:>5.1%}  Overall={h.overall:>6.1%}  " + status)
    for area, why in detail:
        console.print(f"      [red]✗[/] {area}: {why}")


def _print_bug_run_line(task_type: str, result: RunResult) -> None:
    """One-line per-episode summary, shaped by the episode kind."""
    m = result.metrics
    t = f"time={result.wall_time_sec:.1f}s"
    if task_type == "bug_hunt":
        cond = f"[{m['condition']}] " if m.get("condition") else ""
        extra = ""
        if m.get("condition") == "raw":
            extra = f"  adb={m.get('raw_adb_calls')}  mcp_leak={m.get('mcp_tool_calls')}"
        console.print(
            f"  {cond}bugs=[bold]{m.get('bugs_found')}/{m.get('bugs_total')}[/]  "
            f"precision={m.get('precision')}  f1={m.get('f1')}  "
            f"steps={m.get('steps')}(budget {m.get('step_budget')})  "
            f"FP~{m.get('false_positives')}  "
            f"overall=[bold]{(m.get('overall') or 0) * 100:.1f}%[/]{extra}  {t}"
        )
    elif task_type == "clean_task":
        ok = "[green]PASS[/]" if m.get("oracle_passed") else "[red]FAIL[/]"
        console.print(
            f"  clean: oracle={ok}  reward={m.get('reward')}  "
            f"calls={m.get('device_tool_calls')}  {t}"
        )
    else:  # bug_task
        found = "[green]found[/]" if m.get("bug_found") else "[red]missed[/]"
        console.print(
            f"  bug: {found}  status={m.get('reported_status')}  reward={m.get('reward')}  "
            f"calls={m.get('device_tool_calls')}  {t}"
        )


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
@click.option("--device", default=None,
              help="Pin this run to a specific device serial (e.g. 'emulator-5554' or a "
                   "cloud '127.0.0.1:<port>' tunnel). Default: first available. Used by the "
                   "parallel fan-out to give each worker its own device.")
@click.option("--trials", default=1, show_default=True, type=int)
@click.option("--mcp-server", default=None, envvar="QGB_MCP_SERVER",
              help="MCP server URL giving the agent device tools. Omit to run the agent "
                   "bare, driving the device through adb itself.")
@click.option("--runs-dir", default="runs", show_default=True)
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

    Runs every registered app, or a subset via --tier / --app. Needs the
    MCP server, a booted device, and each app's prebuilt buggy APK.
    Pass --device to pin to one serial (parallel fan-out gives each worker its own).

    With --mcp-server the agent gets device tools from that server; without it the
    agent runs bare and drives the device through adb itself.
    """
    _setup_logging(verbose)
    _gate_unready_tiers(tier_filter, app_filter, mode)
    model_list = [m.strip() for m in (models or "").split(",") if m.strip()] or None
    asyncio.run(_leaderboard_bugs(
        model_list, agent, trials, mcp_server, Path(runs_dir),
        push_sheet, webhook_url, token, app_filter, mode, device, tier_filter,
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
) -> None:
    """Run the benchmark. The MCP server, if any, is the caller's to run."""
    await _run_bugs(models, agent, trials, mcp_server, runs_dir, push_sheet,
                    webhook_url, token, app_filter, mode, device, tier_filter)


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
        tier_filter=tier_filter,
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
    push_sheet: bool,
    webhook_url: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """Show the current seeded-bug model leaderboard from saved run artifacts."""
    _setup_logging(verbose)
    results = _lb.load_results(Path(runs_dir), agent=agent)
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
        """Drop infra failures — environment noise must not move a leaderboard."""
        return [r for r in rs if not r.metrics.get("infra_failure")]

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
    through different CLIs."""
    from collections import defaultdict

    # Voided episodes are not QA results — excluded, not averaged in as zeros.
    def voided(r: RunResult) -> bool:
        m = r.metrics or {}
        return bool(m.get("env_failure") or m.get("infra_failure") or m.get("contaminated"))

    by_key: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
    for r in results:
        if not voided(r):
            by_key[(r.agent, _lb.clean_model_name(r.model))].append(r)
    if not by_key:
        return

    def scored(r: RunResult) -> dict:
        """Verified score if replay produced one, else the claimed score."""
        m = r.metrics or {}
        return {**m, **(m.get("hybrid") or {})}

    def avg(rs: list[RunResult], key: str) -> float:
        vals = [v for r in rs if (v := scored(r).get(key)) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def fp_rate(rs: list[RunResult]) -> float:
        fp = sum(scored(r).get("false_positives") or 0 for r in rs)
        ctl = sum(scored(r).get("controls") or 0 for r in rs)
        return fp / ctl if ctl else 0.0

    table = Table(title="Bug hunt — Overall = weighted recall × speed − false-report cost")
    table.add_column("#", justify="right")
    table.add_column("Agent + Model", no_wrap=True)
    for col in ("Trials", "F1", "FP", "Avg/Step", "Avg/Token", "Overall"):
        table.add_column(col, justify="right")

    rows = sorted(by_key.items(),
                  key=lambda kv: -(avg(kv[1], "overall_raw") or avg(kv[1], "overall")))
    for i, ((agent, model), rs) in enumerate(rows, 1):
        steps = avg(rs, "hook_steps") or avg(rs, "steps")
        trials = len({r.trial for r in rs}) or 1
        table.add_row(
            str(i), f"{agent} · {model}", str(trials),
            f"{avg(rs, 'f1'):.2f}", f"{fp_rate(rs) * 100:.0f}%",
            f"{steps:.0f}", f"{avg(rs, 'total_tokens'):,.0f}",
            f"[bold]{avg(rs, 'overall') * 100:.1f}%[/]",
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


