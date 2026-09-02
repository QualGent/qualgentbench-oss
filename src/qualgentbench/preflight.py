"""Is this config runnable? Every check that can fail before an emulator boots,
collected together with the fix for each — the in-harness half of the launcher's
preflight. Nothing here touches a device
unless the config names running serials.
"""

from __future__ import annotations

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
            return CheckResult("Claude auth", True, f"{source} set")
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
    elif cfg.scope.mode != "guided" and (blocked := sorted(set(tiers) - _READY_TIERS)):
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
        if cfg.scope.mode != "guided":
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


def resolve_apk_offline(app: dict, spec: dict | None = None) -> Path:
    """Where the APK is if it is already on this machine — env pin, dist/, or the
    sha-verified cache. Never downloads; a missing path means "would download"."""
    app_id = str(app.get("id", ""))
    if env := os.environ.get("QUALGENTBENCH_APK_" + app_id.upper().replace("-", "_")):
        return Path(env).expanduser()
    repo_root = Path(__file__).resolve().parents[2]
    dist = repo_root / "dist" / app_id / "buggy.apk"
    if dist.exists():
        return dist
    meta = (spec or {}).get("apk") or {}
    if meta.get("filename"):
        cached = _cache_root() / "seeded" / app_id / Path(str(meta["filename"])).name
        if cached.exists() and _verify_sha256(cached, str(meta.get("sha256") or "")):
            return cached
        return cached
    if app.get("apk_local"):
        return (repo_root / app["apk_local"]).resolve()
    return dist


def check_seed_assets(selected: list[dict[str, Any]]) -> CheckResult:
    """Every `device_setup` push source must exist HERE — inside the image, that
    is the image's own /app tree. A missing asset seeds nothing (the app launches
    broken or empty) and a whole run would burn agent spend on env_failures; the
    Docker image once shipped without `assets/` and scored aegis 0/5 for it."""
    repo_root = Path(__file__).resolve().parents[2]
    missing: list[str] = []
    for spec in selected:
        setup = spec.get("device_setup") or {}
        for item in setup.get("push", []):
            src = (repo_root / str(item.get("src", ""))).resolve()
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


def check_apks(selected: list[dict[str, Any]]) -> CheckResult:
    present, to_fetch, missing = [], [], []
    for spec in selected:
        app_id = spec["app"]["id"]
        path = resolve_apk_offline(spec["app"], spec)
        if path.exists():
            present.append(app_id)
        elif spec.get("apk"):
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
        results.append(check_apks(selected))
        results.append(check_seed_assets(selected))
    results.append(check_uiautomator2())
    results += await check_mcp(cfg)
    results.append(await check_devices(cfg, list_devices))
    results.append(check_env_file(cfg, config_dir))
    return results, selected


def failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed and not r.warning]
