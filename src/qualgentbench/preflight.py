"""Is this config runnable? Every check that can fail before an emulator boots,
collected together with the fix for each — the in-harness half of the launcher's
preflight. Nothing here touches a device
unless the config names running serials.

`check_agent_dump` is the exception: it acts on a device (it stops uiautomator2 and
runs two dumps), so `run_preflight` never calls it. `run` calls it once per device,
after the devices are resolved and before the board is planned.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from . import bugs as bugmod
from .adapters import REGISTRY as ADAPTER_REGISTRY
from .apps import _cache_root, _verify_sha256
from .config import BenchConfig
from .doctor import (
    CheckResult,
    check_agent_cli,
    check_codex_auth,
    check_mcp_bridge,
    check_mcp_tools,
    check_uiautomator2,
)

_ALL_TIERS = ("easy", "medium", "hard")
_READY_TIERS = {"easy", "medium", "hard"}

# `run --require-heldout` in environment form, so the config path and the flag path
# answer the same question the same way (see check_heldout).
REQUIRE_HELDOUT_ENV = "QGB_REQUIRE_HELDOUT"


def _require_heldout() -> bool:
    return os.environ.get(REQUIRE_HELDOUT_ENV, "").strip().lower() not in ("", "0", "false", "no")


# ── individual checks ─────────────────────────────────────────────────────────

def check_agent(cfg: BenchConfig) -> list[CheckResult]:
    if cfg.agent not in ADAPTER_REGISTRY:
        return [CheckResult(
            "Agent", False, f"unknown agent {cfg.agent!r}",
            fix=f"One of: {', '.join(sorted(ADAPTER_REGISTRY))}")]
    return [check_agent_cli(cfg.agent), _check_auth(cfg)]


def _check_auth(cfg: BenchConfig) -> CheckResult:
    if cfg.agent == "codex-cli":
        return check_codex_auth()
    if cfg.agent == "claude-code":
        from .adapters.claude_code import ClaudeCodeAdapter
        if source := ClaudeCodeAdapter.auth_source():
            # Passing either way, but not the same run: only subscription auth reports
            # the usage windows the credit guard stops on, and a config that asks for a
            # seven-day stop while running on an API key would silently never stop.
            note = ClaudeCodeAdapter.credit_guard_note(cfg.model)
            if note is None:
                return CheckResult("Claude auth", True, f"{source} set")
            return CheckResult("Claude auth", True, f"{source} set — {note}",
                               warning=True,
                               fix="Run `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN "
                                   "if you want checkpoint.stop_at_seven_day_pct to apply.")
        return CheckResult("Claude auth", False, "no token in the environment",
                           fix=ClaudeCodeAdapter.auth_fix())
    return CheckResult("Provider key", True, "native adapter — checked per model at run time",
                       warning=True)


def check_model(cfg: BenchConfig) -> CheckResult:
    from .adapters.claude_code import ClaudeCodeAdapter
    if ClaudeCodeAdapter.is_fireworks_model(cfg.model):
        if cfg.agent != "claude-code":
            return CheckResult("Model", False,
                               f"{cfg.model} is a Fireworks model, only wired for claude-code",
                               fix="Use agent: claude-code, or a different model.")
        if not (os.environ.get("FIREWORKS_API_KEY") or os.environ.get("FIREWORKS_AI_API_KEY")):
            return CheckResult("Model", False, f"{cfg.model} runs on Fireworks but no key is set",
                               fix="Add FIREWORKS_API_KEY=fw_... to env_file.")
    return CheckResult("Model", True, cfg.model)


def select_apps(cfg: BenchConfig) -> tuple[list[dict[str, Any]], list[CheckResult]]:
    """The apps the scope names, plus the checks that decided it."""
    checks: list[CheckResult] = []
    apps = bugmod.load_apps()
    known = {s["app"]["id"]: s for s in apps}
    tiers = [t.lower() for t in cfg.scope.tiers]

    if unknown := sorted(set(tiers) - set(_ALL_TIERS)):
        checks.append(CheckResult("Tiers", False, f"unknown tier(s): {', '.join(unknown)}",
                                  fix=f"One of: {', '.join(_ALL_TIERS)}"))
    elif cfg.scope.mode in ("hunt", "all") and (blocked := sorted(set(tiers) - _READY_TIERS)):
        checks.append(CheckResult(
            "Tiers", False, f"{'/'.join(blocked)} not hunt-ready — scores would not be comparable",
            fix=f"Ready today: {', '.join(sorted(_READY_TIERS))}"))
    elif tiers:
        checks.append(CheckResult("Tiers", True, ", ".join(tiers)))

    selected = [s for s in apps if s["app"].get("difficulty") in tiers] if tiers else []
    if cfg.scope.apps:
        if unknown_apps := sorted(set(cfg.scope.apps) - set(known)):
            checks.append(CheckResult(
                "Apps", False, f"unknown app id(s): {', '.join(unknown_apps)}",
                fix=f"Available: {', '.join(sorted(known))}"))
        wanted = [known[a] for a in cfg.scope.apps if a in known]
        if tiers:
            off_tier = [s["app"]["id"] for s in wanted if s["app"].get("difficulty") not in tiers]
            if off_tier:
                checks.append(CheckResult(
                    "Apps", False,
                    f"{', '.join(off_tier)} not in tier(s) {', '.join(tiers)}",
                    fix="Drop `tiers` to run apps by id, or list apps from those tiers."))
        if cfg.scope.mode in ("hunt", "all"):
            unready = [s["app"]["id"] for s in wanted
                       if s["app"].get("difficulty") not in _READY_TIERS]
            if unready:
                checks.append(CheckResult(
                    "Apps", False, f"not hunt-ready: {', '.join(unready)}",
                    fix="Their briefs leak and budgets are underived; use easy/medium apps."))
        ids = {s["app"]["id"] for s in wanted}
        selected = [s for s in (selected or wanted) if s["app"]["id"] in ids]
        if not any(not c.passed for c in checks if c.name == "Apps"):
            checks.append(CheckResult("Apps", True, ", ".join(s["app"]["id"] for s in selected)))
    if not selected and all(c.passed for c in checks):
        checks.append(CheckResult("Scope", False, "selects no apps"))
    return selected, checks


def local_apk(app: dict) -> Path | None:
    """The local build that EVERY mode installs, if there is one — a
    QUALGENTBENCH_APK_<ID> pin, else dist/<id>/buggy.apk — in the order `run` resolves
    (`cli._resolve_app_apk`); None when the app would come from its published block."""
    app_id = str(app.get("id", ""))
    if env := os.environ.get("QUALGENTBENCH_APK_" + app_id.upper().replace("-", "_")):
        return Path(env).expanduser()
    dist = Path(__file__).resolve().parents[2] / "dist" / app_id / "buggy.apk"
    return dist if dist.exists() else None


def resolve_apk_offline(app: dict, spec: dict | None = None, mode: str = "hunt") -> Path:
    """Where the APK is if it is already on this machine — env pin, dist/, or the
    sha-verified cache. Never downloads; a missing path means "would download".
    Journey mode looks for the journey build (test-case file `apk:`, cache slot
    journey/) before the spec's hunt build."""
    app_id = str(app.get("id", ""))
    if (local := local_apk(app)) is not None:
        return local
    repo_root = Path(__file__).resolve().parents[2]
    dist = repo_root / "dist" / app_id / "buggy.apk"
    kind, meta = "seeded", (spec or {}).get("apk") or {}
    if mode == "journey":
        from . import journey as _journey
        if jmeta := _journey.apk_meta(app_id):
            kind, meta = "journey", jmeta
    if meta.get("path"):
        from .apps import heldout_apk_path
        try:
            return heldout_apk_path(app_id, meta) or dist
        except RuntimeError:
            # No held-out dir configured: report the unresolved relative path so the
            # preflight's "APK present" check fails with a readable location.
            return Path(str(meta["path"]))
    if meta.get("filename"):
        cached = _cache_root() / kind / app_id / Path(str(meta["filename"])).name
        if cached.exists() and _verify_sha256(cached, str(meta.get("sha256") or "")):
            return cached
        return cached
    if app.get("apk_local"):
        return (repo_root / app["apk_local"]).resolve()
    return dist


def journey_build_differs(app: dict, spec: dict | None = None) -> bool:
    """Would `--mode journey` install a different build of this app than `--mode hunt`?

    `--mode all` stages ONE APK per app for every unit (lanes.py), resolved the way hunt
    and guided resolve it — so for an app whose answer here is True, its journey units
    would run on a build that lacks their journey-only defects (QUA-2739), and
    `run`/`preflight` refuse that app in that mode instead.

    Answered from the specs alone — nothing is downloaded or hashed. A local build
    (`local_apk`) serves every mode, and an app with no journey `apk:` block falls back
    to its hunt build, so neither differs. Otherwise the two published blocks are
    compared by sha256, not by path: they sit in different cache slots even when they
    name the same bytes, as three apps' did before epic QUA-2723. A block with no hash
    cannot prove it is the other one."""
    if local_apk(app) is not None:
        return False
    from . import journey as _journey
    journey_meta = _journey.apk_meta(str(app.get("id", "")))
    if not journey_meta:
        return False
    journey_sha = str(journey_meta.get("sha256") or "")
    hunt_sha = str(((spec or {}).get("apk") or {}).get("sha256") or "")
    return not (journey_sha and journey_sha == hunt_sha)


def check_mode_all_builds(selected: list[dict[str, Any]], mode: str) -> CheckResult | None:
    """`mode: all` over an app whose journey build is not its hunt build (see
    `journey_build_differs`). None in every other mode, and when nothing conflicts."""
    if mode != "all":
        return None
    split = [s["app"]["id"] for s in selected if journey_build_differs(s["app"], s)]
    if not split:
        return CheckResult("Builds", True, "every app runs one build for all three kinds")
    return CheckResult(
        "Builds", False,
        f"mode `all` would run the journey cases of {', '.join(split)} on the HUNT build, "
        f"which lacks their journey-only defects",
        fix="Run `mode: journey` and `mode: hunt` (or guided) as separate runs — the board "
            "prints each kind separately anyway — or narrow `apps:` to apps whose journey "
            "build is their hunt build.")


def check_seed_assets(selected: list[dict[str, Any]]) -> CheckResult:
    """Every `device_setup` push source must exist HERE — inside the image, that
    is the image's own /app tree. A missing asset seeds nothing (the app launches
    broken or empty) and a whole run would burn agent spend on env_failures; the
    Docker image once shipped without `assets/` and scored aegis 0/5 for it."""
    from .corpus import asset_path

    missing: list[str] = []
    for spec in selected:
        setup = spec.get("device_setup") or {}
        for item in setup.get("push", []):
            src = asset_path(str(item.get("src", "")))   # held-out root first
            if not src.exists():
                missing.append(f"{spec['app']['id']}: {item.get('src')}")
    if missing:
        return CheckResult("Seed assets", False,
                           f"{len(missing)} device_setup push source(s) missing: "
                           f"{'; '.join(missing[:4])}",
                           fix="the assets/ tree must ship alongside the harness — "
                               "in Docker, the image must COPY assets ./assets")
    n = sum(len((s.get("device_setup") or {}).get("push", [])) for s in selected)
    return CheckResult("Seed assets", True, f"{n} push source(s) present")


def check_apks(selected: list[dict[str, Any]], mode: str = "hunt") -> CheckResult:
    present, to_fetch, missing = [], [], []
    for spec in selected:
        app_id = spec["app"]["id"]
        path = resolve_apk_offline(spec["app"], spec, mode=mode)
        published = spec.get("apk")
        if mode == "journey":
            from . import journey as _journey
            published = _journey.apk_meta(app_id) or published
        if path.exists():
            present.append(app_id)
        elif published:
            to_fetch.append(app_id)
        else:
            missing.append(app_id)
    if missing:
        return CheckResult("APKs", False,
                           f"no APK and no published source for: {', '.join(missing)}",
                           fix="uv run python scripts/build_app.py <id>, or set "
                               "QUALGENTBENCH_APK_<ID>=/path/to.apk")
    if to_fetch:
        return CheckResult("APKs", True,
                           f"{len(present)} present · {len(to_fetch)} will download from "
                           f"HuggingFace on first use ({', '.join(to_fetch)})", warning=True)
    return CheckResult("APKs", True, f"{len(present)} present, sha256-verified")


async def check_mcp(cfg: BenchConfig) -> list[CheckResult]:
    if not cfg.mcp_server:
        return [CheckResult("MCP server", True, "none — bare arm (agent drives adb)")]
    bridge = await check_mcp_bridge(cfg.mcp_server)
    out = [bridge]
    if bridge.passed:
        out.append(await check_mcp_tools(cfg.mcp_server))
    return out


async def check_devices(cfg: BenchConfig, list_devices: Callable | None = None) -> CheckResult:
    """Only for serials the config says are already running; AVDs are the
    launcher's to boot and check."""
    from .session import DeviceSession
    if not cfg.devices.serials:
        if cfg.devices.avds:
            return CheckResult("Devices", True,
                               f"{len(cfg.devices.avds)} AVD(s) to boot: "
                               f"{', '.join(cfg.devices.avds)} (launcher checks them)")
        return CheckResult("Devices", False, "no `devices.avds` or `devices.serials`",
                           fix="List the AVDs to boot, or the adb serials already running.")
    online = await (list_devices or DeviceSession(cfg.mcp_server).available_devices)()
    missing = [s for s in cfg.devices.serials if s not in online]
    if missing:
        return CheckResult("Devices", False, f"not connected: {', '.join(missing)}",
                           fix=f"Online now: {', '.join(online) or '(none)'}")
    return CheckResult("Devices", True, ", ".join(cfg.devices.serials))


AGENT_DUMP_ATTEMPTS = 2
_AGENT_DUMP_RETRY_S = 1.0


async def check_agent_dump(serial: str, *, attempts: int = AGENT_DUMP_ATTEMPTS) -> CheckResult:
    """Does an AGENT's own `uiautomator dump` return a view hierarchy on this device?

    The hierarchy is what grounds a report and scores a screen-text completion; an agent
    without it tests from screenshots. On QUA-2731's board none of the agent's 371 dumps
    returned one (killed, exit 137: another UiAutomation client held the device) and
    nothing noticed until the post-mortem, because the harness's own reader falls back
    to uiautomator2 (docs/final-validation-2026-09-19.md §8, QUA-2741).

    It checks the state the agent will be handed: uiautomator2's server is stopped
    first, exactly as `run_episode` stops it before every agent, then both of the
    agent's dump forms must return a hierarchy. A failure is retried once after a
    pause (a dump can miss a screen that is still settling); a device that still fails
    is refused. Touches the device (a process kill and two dumps), so it runs only
    for devices a board is about to use."""
    from .verify import device as vdevice

    name = f"Agent dump {serial}"
    stopped = await vdevice.stop_u2_server(serial)
    note = (f" (stopped uiautomator2 server pid(s) {', '.join(stopped)} first)"
            if stopped else "")
    probes: list = []
    for attempt in range(max(1, attempts)):
        probes = await vdevice.probe_agent_dump(serial)
        if all(p.ok for p in probes):
            return CheckResult(name, True, "; ".join(p.detail for p in probes) + note)
        if attempt + 1 < attempts:
            await asyncio.sleep(_AGENT_DUMP_RETRY_S)
    failed = [p for p in probes if not p.ok]
    detail = "; ".join(f"`{p.command}` → {p.detail}" for p in failed) + note
    if any(p.killed for p in failed):
        fix = (f"Another UiAutomation client holds {serial}: Android registers one per "
               f"device, and every other `uiautomator dump` dies of it (exit 137). Find "
               f"it with `adb -s {serial} logcat -b crash -d | grep 'already registered'` "
               f"and `adb -s {serial} shell ps -A -o PID,ARGS | grep -e uiautomator -e "
               f"instrument`, stop it (an Appium or uiautomator2 server, a test "
               f"runner), then run again.")
    else:
        fix = (f"Check the device by hand: `adb -s {serial} exec-out uiautomator dump "
               f"/dev/tty` must print a <hierarchy>. A screen that never goes idle fails "
               f"the dump too; go HOME and run again.")
    return CheckResult(name, False, detail, fix=fix)


def check_heldout(cfg: BenchConfig, base: Path) -> CheckResult:
    """The held-out split, when one is configured (`heldout_dir:` or QGB_HELDOUT_DIR):
    the directory must exist and hold at least one app, or a journey run would
    silently measure the public corpus alone while its manifest claims a split.

    With NO split configured at all, a journey config is not simply fine: the board it
    produces has public rows only and no held-out block, which is a weaker claim than
    it looks (docs/heldout.md). That is a warning here — a public-only board is what
    every OSS clone runs — and a failure under QGB_REQUIRE_HELDOUT, for the caller
    whose pass criteria include the held-out rows."""
    from . import corpus

    d = corpus.heldout_dir()
    if d is None:
        if cfg.scope.mode not in ("journey", "all"):
            return CheckResult("Held-out split", True, "none")
        required = _require_heldout()
        return CheckResult(
            "Held-out split", not required,
            "none — this journey board will print PUBLIC rows only, with no held-out "
            "block to tell a found bug from a memorised answer key",
            warning=not required,
            fix=f"Sync the split and set {corpus.HELDOUT_ENV} (or `heldout_dir:` in this "
                f"config) — docs/heldout.md. A `{corpus.DEFAULT_HELDOUT_DIRNAME}/` "
                f"directory beside the repository is not picked up on its own.")
    if not d.is_dir():
        return CheckResult("Held-out split", False, f"{d} not found",
                           fix=f"Create it with scripts/holdout.py move <app>, or unset "
                               f"{corpus.HELDOUT_ENV} / drop `heldout_dir`.")
    apps = corpus.heldout_apps()
    if not apps:
        return CheckResult("Held-out split", False, f"{d} holds no test-cases/<app>.yaml",
                           fix="scripts/holdout.py verify shows what the directory holds.")
    return CheckResult("Held-out split", True,
                       f"{len(apps)} app(s), version {corpus.heldout_version()} at {d}")


def check_runs_dir(cfg: BenchConfig) -> CheckResult:
    """The runs dir `run --config` would use must keep the agent's workspace clear of
    this repo and of every CLAUDE.md / AGENTS.md above it (QUA-2778): both agents read
    those as start-up context, where the contamination scanner cannot see them.
    Relative paths resolve from the current directory, exactly as `run` resolves them."""
    from .config import DEFAULT_RUNS_DIR_DISPLAY, resolve_runs_dir, runs_dir_problems

    path = resolve_runs_dir(cfg.runs_dir)
    problems = runs_dir_problems(path)
    if not problems:
        return CheckResult("runs_dir", True, str(path))
    return CheckResult(
        "runs_dir", False, "; ".join(problems),
        fix=f"Drop `runs_dir` (default {DEFAULT_RUNS_DIR_DISPLAY}) or point it at a "
            f"directory with no CLAUDE.md / AGENTS.md above it, outside the repository.")


def check_env_file(cfg: BenchConfig, base: Path) -> CheckResult:
    if not cfg.env_file:
        return CheckResult("env_file", True, "none")
    path = (base / cfg.env_file).expanduser()
    if not path.is_file():
        return CheckResult("env_file", False, f"{path} not found",
                           fix="Create it (cp .env.example .env) or drop `env_file`.")
    return CheckResult("env_file", True, str(path))


# ── the whole thing ───────────────────────────────────────────────────────────

async def run_preflight(cfg: BenchConfig, *, config_dir: Path,
                        list_devices: Callable | None = None,
                        ) -> tuple[list[CheckResult], list[dict[str, Any]]]:
    """Every check, in the order a user would fix them. Returns the results and
    the selected app specs (empty when the scope is broken)."""
    results: list[CheckResult] = []
    results += check_agent(cfg)
    results.append(check_model(cfg))
    selected, scope_checks = select_apps(cfg)
    results += scope_checks
    if selected:
        results.append(check_apks(selected, mode=cfg.scope.mode))
        if builds := check_mode_all_builds(selected, cfg.scope.mode):
            results.append(builds)
        results.append(check_seed_assets(selected))
    results.append(check_uiautomator2())
    results += await check_mcp(cfg)
    results.append(await check_devices(cfg, list_devices))
    results.append(check_env_file(cfg, config_dir))
    results.append(check_runs_dir(cfg))
    results.append(check_heldout(cfg, config_dir))
    return results, selected


def failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed and not r.warning]
