"""Pre-flight health checks: `run_doctor` is the full suite behind
`qualgent-bench doctor`; `run_preflight` is the critical-only subset called
before benchmark execution."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .session import DeviceSession


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    fix: str | None = None      # actionable command or message for the user
    warning: bool = False       # True = advisory only, does not cause exit(1)


# ── Individual checks ─────────────────────────────────────────────────────────

async def check_mcp_bridge(url: str) -> CheckResult:
    from urllib.parse import urlsplit

    session = DeviceSession(url)
    if await session.is_healthy():
        return CheckResult("MCP server", True, f"running at {url}")
    from .cli import _mcp_server_help
    port = urlsplit(url).port or 51821
    return CheckResult(
        "MCP server", False,
        f"not reachable at {url}",
        fix="A run starts one automatically; this only failed because doctor could "
            "not start it either. Try by hand to see the error:\n"
            + _mcp_server_help(port),
    )


async def check_mcp_tools(url: str) -> CheckResult:
    """Verify the MCP endpoint is reachable and returns at least one tool."""
    try:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(f"{url.rstrip('/')}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in (tools.tools or [])}
                count = len(names)
                # Only the desktop bridge defines the device-lock tools, and running
                # against it fails every episode on its first tap.
                if "qg_acquire_device" in names:
                    from urllib.parse import urlsplit
                    port = urlsplit(url).port or 51821
                    return CheckResult(
                        "MCP tools", False,
                        f"{count} tools, but this is the MCP DESKTOP APP, not the "
                        "standalone server",
                        fix=f"Quit the MCP desktop app — it holds port {port}. "
                            "`qualgent-bench run` starts the right server on its own.",
                    )
                if count > 0:
                    return CheckResult("MCP tools", True, f"{count} tools available")
                return CheckResult(
                    "MCP tools", False,
                    "MCP endpoint reachable but returned 0 tools",
                    fix="Check MCP logs — the MCP server may not have started correctly.",
                )
    except Exception as exc:
        return CheckResult(
            "MCP tools", False,
            f"MCP connection failed: {exc}",
            fix=f"Verify MCP is running and MCP is enabled at {url}",
        )


async def check_device_connected(url: str | None) -> CheckResult:
    session = DeviceSession(url)
    if url is None:
        serial = await session.first_available_device()
        return (CheckResult("Device", True, serial) if serial
                else CheckResult("Device", False, "no devices found",
                                 fix="Start an Android emulator: emulator -avd <name>"))
    devices = await session.list_devices()
    # Filter out simulators that are listed but not booted
    ready = [d for d in devices if d.get("state", "ready") != "shutdown"]
    if not ready:
        all_count = len(devices)
        if all_count > 0:
            return CheckResult(
                "Device", False,
                f"{all_count} simulator(s) found but none are booted",
                fix="Boot a simulator from MCP or via: xcrun simctl boot <udid>",
            )
        return CheckResult(
            "Device", False,
            "no devices found",
            fix="Start an Android emulator: emulator -avd <name>  (list: emulator -list-avds)",
        )
    # Lead with the Android emulators runs actually use; list other connected
    # devices separately so they aren't mistaken for booted simulators.
    android = [d for d in ready if d.get("platform") == "android"]
    other = [d for d in ready if d.get("platform") != "android"]
    detail = (f"{len(android)} android emulator(s) used by runs: "
              + (", ".join(d.get("id", "?") for d in android) or "none"))
    if other:
        detail += (f"  ·  {len(other)} other device(s) connected (not used by the Android "
                   f"benchmark): " + ", ".join(d.get("id", "?")[:12] for d in other))
    return CheckResult("Device", True, detail)


async def check_app_installed(
    url: str,
    bundle_id: str,
    platform: str,
    device: str,
) -> CheckResult:
    name = f"App: {bundle_id}"
    session = DeviceSession(url)
    installed = await session.list_installed_apps(device, platform)
    if bundle_id in installed:
        return CheckResult(name, True, f"installed on {device}")
    return CheckResult(
        name, False,
        f"NOT installed on {device}",
        fix="Install the app through the benchmark command you are running.",
    )


def check_agent_cli(agent_name: str) -> CheckResult:
    cli_map = {
        "claude-code": "claude",
        "codex-cli": "codex",
        "native": None,
    }
    cli = cli_map.get(agent_name)
    if cli is None:
        return CheckResult(f"Agent CLI: {agent_name}", True, "no external CLI required")

    path = shutil.which(cli)
    if path:
        return CheckResult(f"Agent CLI: {agent_name}", True, f"{cli}  found at {path}")
    return CheckResult(
        f"Agent CLI: {agent_name}", False,
        f"'{cli}' not found in PATH",
        fix="Install the agent CLI. For claude-code: npm install -g @anthropic-ai/claude-code; "
            "for codex-cli: npm install -g @openai/codex",
    )


def check_codex_auth() -> CheckResult:
    from .adapters.codex_cli import CodexCliAdapter

    if os.environ.get("CODEX_API_KEY"):
        return CheckResult("Codex auth", True, "CODEX_API_KEY set")

    source_home = CodexCliAdapter._source_codex_home()
    for filename in CodexCliAdapter._AUTH_FILES:
        if (source_home / filename).is_file():
            return CheckResult(
                "Codex auth",
                True,
                f"account login found in {source_home}",
            )

    return CheckResult(
        "Codex auth",
        False,
        f"no account login found in {source_home} and CODEX_API_KEY is not set",
        fix="Run `codex login --device-auth`, or export CODEX_API_KEY='sk-...' "
            "(or add it to .env) for CI/non-interactive runs.",
    )




def check_uiautomator2() -> CheckResult:
    """The replay paths that need it are best-effort, so a broken install silently
    degrades verdicts — this check turns that into a refusal."""
    try:
        import uiautomator2
        version = getattr(uiautomator2, "__version__", "?")
        return CheckResult(
            "uiautomator2", True,
            f"importable (v{version}) — hierarchy fallback and atomic `type` available")
    except ImportError:
        return CheckResult(
            "uiautomator2", False,
            "not importable — replays silently degrade: no hierarchy fallback on "
            "never-idle screens, `type` becomes clear-and-keystrokes",
            fix="uv sync")


async def check_hf_reachable() -> CheckResult:
    """Check HuggingFace is reachable. Doctor-only (not in pre-flight)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://huggingface.co")
            if resp.status_code < 500:
                return CheckResult("HuggingFace", True, "reachable")
            return CheckResult(
                "HuggingFace", False, f"HTTP {resp.status_code}",
                fix="Check your internet connection.",
            )
    except Exception as exc:
        return CheckResult(
            "HuggingFace", False, f"unreachable: {exc}",
            fix="Check your internet connection. HF token downloads will fail.",
            warning=True,
        )




def check_seeded_apks(tier: str = "easy") -> CheckResult:
    """Can this machine actually get the benchmark APKs? dist/ is gitignored, so a
    fresh clone depends entirely on the HuggingFace fetch. Reports what is cached
    versus what would be downloaded, without downloading."""
    from .apps import _cache_root
    from . import bugs as bugmod

    try:
        apps = [s for s in bugmod.load_apps()
                if s["app"].get("difficulty") == tier]
    except Exception as exc:  # noqa: BLE001
        return CheckResult(f"{tier}-tier APKs", False, f"could not load specs: {exc}")
    if not apps:
        return CheckResult(f"{tier}-tier APKs", True, "no apps at this tier")

    repo_root = Path(__file__).resolve().parents[2]
    local = cached = fetchable = 0
    missing: list[str] = []
    for spec in apps:
        app_id = spec["app"]["id"]
        if (repo_root / "dist" / app_id / "buggy.apk").exists():
            local += 1
        elif (meta := spec.get("apk")):
            name = Path(str(meta.get("filename", ""))).name
            if (_cache_root() / "seeded" / app_id / name).exists():
                cached += 1
            else:
                fetchable += 1
        else:
            missing.append(app_id)

    if missing:
        return CheckResult(
            f"{tier}-tier APKs", False,
            f"{len(missing)} app(s) have no APK and no published source: "
            f"{', '.join(missing)}",
            fix="Build them (uv run python scripts/build_app.py <id>) and publish — "
                "see scripts/build_app.py.")
    parts = [f"{len(apps)} app(s)"]
    if local:
        parts.append(f"{local} built locally")
    if cached:
        parts.append(f"{cached} cached")
    if fetchable:
        parts.append(f"{fetchable} will download on first use")
    return CheckResult(f"{tier}-tier APKs", True, " · ".join(parts))


# ── Composite runners ──────────────────────────────────────────────────────────

async def run_doctor(
    *,
    url: str = "http://localhost:51821",
    agent: str | None = None,
    lean: bool = False,
) -> list[CheckResult]:
    """Full check suite for `qualgent-bench doctor`. lean=True skips the Apps/HF
    checks — irrelevant when the benchmark installs its own app."""
    results: list[CheckResult] = []

    return await _infrastructure_and_rest(url, agent, lean, results)


async def _infrastructure_and_rest(
    url: str,
    agent: str | None,
    lean: bool,
    results: list[CheckResult],
) -> list[CheckResult]:
    # No --mcp-server means the bare-agent path: no server to check, ask adb.
    if url is None:
        device_check = await check_device_connected(None)
        results.append(device_check)
    else:
        bridge = await check_mcp_bridge(url)
        results.append(bridge)
        if bridge.passed:
            results.append(await check_mcp_tools(url))
            device_check = await check_device_connected(url)
            results.append(device_check)
        else:
            results.append(CheckResult("MCP tools", False, "skipped — MCP server is down"))
            results.append(CheckResult("Device", False, "skipped — MCP server is down"))
            device_check = None

    # APK delivery: a public HuggingFace dataset, sha256-verified and cached.
    # No account or token needed.
    results.append(await check_hf_reachable())
    results.append(check_seeded_apks())
    results.append(check_uiautomator2())

    if agent:
        results.append(check_agent_cli(agent))
        if agent == "codex-cli":
            results.append(check_codex_auth())


    return results


async def run_preflight(
    *,
    task_bundle_id: str,
    task_platform: str,
    agent: str,
    url: str,
    device: str,
) -> list[CheckResult]:
    """Critical-only checks before benchmark execution. Raises RuntimeError on
    failure; warnings are returned but do not raise."""
    results: list[CheckResult] = [
        await check_mcp_bridge(url),
        await check_device_connected(url),
        await check_app_installed(url, task_bundle_id, task_platform, device),
        check_agent_cli(agent),
        check_uiautomator2(),
    ]

    failures = [r for r in results if not r.passed and not r.warning]
    if failures:
        lines = [f"  ✗  {r.name}: {r.detail}" for r in failures]
        fixes = [f"     → {r.fix}" for r in failures if r.fix]
        raise RuntimeError(
            "Pre-flight checks failed:\n"
            + "\n".join(lines)
            + ("\n" + "\n".join(fixes) if fixes else "")
        )

    return results
