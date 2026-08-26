"""MCP server interaction — health check and device info queries via MCP."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Per-call ceiling on inter-trial device plumbing, so a wedged bridge or device
# can't hang a whole lane between trials.
_DEVICE_OP_TIMEOUT = 60.0


class DeviceSession:
    """Thin wrapper around the MCP bridge, used for pre-flight checks. The runner
    never acquires/releases the device itself — that lifecycle belongs to the
    agent, same as production use."""

    def __init__(self, bridge_url: str | None = "http://localhost:51821") -> None:
        """`bridge_url=None` selects ADB-only mode (the raw arm): no MCP server runs
        at all, so bridge-only calls must be skipped rather than left to fail."""
        self.bridge_url = bridge_url.rstrip("/") if bridge_url else None

    async def is_healthy(self) -> bool:
        """Is the device surface this session depends on usable? In ADB-only mode
        there is no bridge to be unhealthy, so trivially True."""
        if self.bridge_url is None:
            return True
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # The bridge may 404 on / but still be alive — just checking connectivity
                resp = await client.get(f"{self.bridge_url}/mcp")
                return True  # any response (including 4xx) means the server is up
        except Exception:
            return False

    async def list_devices(self) -> list[dict]:
        """List devices via the bridge; each dict has id, name, platform, state."""
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(f"{self.bridge_url}/mcp") as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("mobile_list_available_devices", {})
                    # Each device arrives as its own TextContent block, not one
                    # JSON array — collect them all.
                    devices = []
                    for block in result.content or []:
                        text = getattr(block, "text", "") or ""
                        if not text.strip():
                            continue
                        try:
                            obj = json.loads(text)
                            if isinstance(obj, dict):
                                devices.append(obj)
                            elif isinstance(obj, list):
                                devices.extend(obj)
                        except json.JSONDecodeError:
                            pass
                    return devices
        except Exception as exc:
            logger.debug("list_devices failed: %s", exc)
            return []

    async def available_devices(self) -> list[str]:
        """Every ready device id — adb's list in ADB-only mode, the bridge's otherwise."""
        if self.bridge_url is None:
            return await list_adb_devices()
        out = []
        for d in await self.list_devices():
            # Skip simulators that are listed but shut down.
            if d.get("state", "ready") == "shutdown":
                continue
            device_id = d.get("id") or d.get("udid")
            if device_id:
                out.append(str(device_id))
        return out

    async def first_available_device(self) -> str | None:
        """Return the id of the first ready device, or None."""
        devices = await self.available_devices()
        return devices[0] if devices else None

    # ── App lifecycle ──────────────────────────────────────────────────────────

    async def reset_app(self, device: str, bundle_id: str, platform: str) -> None:
        """Clear app data for a clean first-launch state. Android: pm clear.
        iOS simulator: wipe the data container without reinstalling. iOS physical
        device: no data-clear API without reinstall — raises NotImplementedError."""
        if platform == "android":
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", device, "shell", "pm", "clear", bundle_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_DEVICE_OP_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(
                    f"adb pm clear timed out after {_DEVICE_OP_TIMEOUT:.0f}s for "
                    f"{bundle_id} on {device} (device likely wedged)"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"adb pm clear failed for {bundle_id} on {device}: "
                    f"{stderr.decode().strip() or stdout.decode().strip()}"
                )

        elif platform == "ios":
            if _is_ios_simulator(device):
                await _reset_simulator_app(device, bundle_id)
            else:
                raise NotImplementedError(
                    f"iOS physical device data-clear is not supported without reinstall. "
                    f"Device: {device}  Bundle: {bundle_id}\n"
                    "Reinstall the app through the benchmark command you are running."
                )
        else:
            raise ValueError(f"Unknown platform: {platform!r}. Expected 'android' or 'ios'.")

    async def launch_app(self, device: str, bundle_id: str) -> None:
        """Launch an installed app — `mobile_launch_app`, or adb when there is no bridge."""
        if self.bridge_url is None:
            from .verify.device import relaunch
            await relaunch(device, bundle_id)
            return
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async def _launch() -> None:
                async with streamablehttp_client(f"{self.bridge_url}/mcp") as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.call_tool("mobile_launch_app", {
                            "device": device,
                            "package_id": bundle_id,
                        })

            await asyncio.wait_for(_launch(), timeout=_DEVICE_OP_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(f"launch_app failed for {bundle_id} on {device}: {exc}") from exc

    async def _call_routine_tool(self, tool: str, args: dict) -> str:
        """Call a routine MCP tool and return its text response."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(f"{self.bridge_url}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                return " ".join(
                    getattr(block, "text", "") or ""
                    for block in (result.content or [])
                )

    async def record_routine(
        self,
        routine_id: str,
        app_bundle_id: str,
        os: str,
        name: str,
        steps: str,
        description: str = "",
        trigger_hints: str = "",
        params: str = "",
    ) -> str:
        """Record a new routine via MCP. Returns the response text."""
        return await self._call_routine_tool("record_routine", {
            "routine_id": routine_id,
            "app_bundle_id": app_bundle_id,
            "os": os,
            "name": name,
            "steps": steps,
            "description": description,
            "trigger_hints": trigger_hints,
            "params": params,
        })

    async def update_routine(
        self,
        routine_id: str,
        app_bundle_id: str,
        os: str,
        name: str,
        steps: str,
        description: str = "",
        trigger_hints: str = "",
        params: str = "",
    ) -> str:
        """Update an existing routine via MCP. Returns the response text."""
        return await self._call_routine_tool("update_routine", {
            "routine_id": routine_id,
            "app_bundle_id": app_bundle_id,
            "os": os,
            "name": name,
            "steps": steps,
            "description": description,
            "trigger_hints": trigger_hints,
            "params": params,
        })

    async def has_routines(self, bundle_id: str, platform: str) -> bool:
        """Return True if MCP has at least one recorded routine for this app."""
        os_name = "ios" if platform == "ios" else "android"
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(f"{self.bridge_url}/mcp") as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("find_routine", {
                        "app_bundle_id": bundle_id,
                        "os": os_name,
                    })
                    text = " ".join(
                        getattr(block, "text", "") or ""
                        for block in (result.content or [])
                    )
                    return bool(text.strip()) and "No routines recorded yet" not in text
        except Exception as exc:
            logger.debug("find_routine failed for %s: %s", bundle_id, exc)
            return False

    async def get_installed_version_code(self, device: str, bundle_id: str) -> int | None:
        """Return the installed versionCode for an Android package, or None if not installed."""
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", device, "shell", "dumpsys", "package", bundle_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            stripped = line.strip()
            if stripped.startswith("versionCode="):
                try:
                    return int(stripped.split("=")[1].split()[0])
                except (ValueError, IndexError):
                    pass
        return None

    async def _uninstall_android(self, device: str, bundle_id: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", device, "uninstall", bundle_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        logger.debug(
            "adb uninstall %s: %s",
            bundle_id,
            (stdout.decode() + stderr.decode()).strip(),
        )

    async def setup_app(self, device: str, apk_path: Path, bundle_id: str = "") -> str:
        """Install via adb (Android) or simctl (iOS simulator); returns the detected
        bundle_id, best-effort. An Android version-downgrade failure uninstalls and
        retries, so buggy and clean builds of the same applicationId can swap."""
        suffix = apk_path.suffix.lower()

        if suffix == ".apk":
            out = await self._adb_install(device, apk_path)
            if "INSTALL_FAILED_VERSION_DOWNGRADE" in out and bundle_id:
                logger.info(
                    "version downgrade detected — uninstalling %s and retrying", bundle_id
                )
                await self._uninstall_android(device, bundle_id)
                out = await self._adb_install(device, apk_path)
            if "Success" not in out:
                raise RuntimeError(f"adb install failed: {out[:300]}")
            # Best-effort bundle_id detection from the installed package list.
            after_proc = await asyncio.create_subprocess_exec(
                "adb", "-s", device, "shell", "pm", "list", "packages", "-3",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            after_out, _ = await after_proc.communicate()
            pkgs = [
                line.removeprefix("package:").strip()
                for line in after_out.decode().splitlines()
                if line.startswith("package:")
            ]
            # Return the most recently installed non-uiautomator package
            candidates = [p for p in pkgs if "uiautomator" not in p]
            return candidates[-1] if candidates else ""

        elif suffix in (".ipa", ".app"):
            if _is_ios_simulator(device):
                proc = await asyncio.create_subprocess_exec(
                    "xcrun", "simctl", "install", device, str(apk_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"xcrun simctl install failed: {(stdout.decode() + stderr.decode()).strip()}"
                    )
                return ""  # caller already knows bundle_id from task.yaml
            else:
                raise NotImplementedError(
                    "iOS physical device install requires go-ios. "
                    "Use MCP app to install manually."
                )
        else:
            raise ValueError(f"Unsupported app file type: {suffix}")

    async def list_installed_apps(self, device: str, platform: str) -> list[str]:
        """Return bundle_ids of all installed apps on the device."""
        try:
            if platform == "android":
                proc = await asyncio.create_subprocess_exec(
                    "adb", "-s", device, "shell", "pm", "list", "packages",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                return [
                    line.removeprefix("package:").strip()
                    for line in stdout.decode().splitlines()
                    if line.startswith("package:")
                ]

            elif platform == "ios":
                if _is_ios_simulator(device):
                    proc = await asyncio.create_subprocess_exec(
                        "xcrun", "simctl", "listapps", device,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    import plistlib
                    data = plistlib.loads(stdout)
                    return list(data.keys())
                else:
                    # Xcode 15+ devicectl
                    proc = await asyncio.create_subprocess_exec(
                        "xcrun", "devicectl", "device", "info", "apps",
                        "--device", device, "--quiet",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    return re.findall(
                        r'([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*){2,})',
                        stdout.decode(),
                    )
            else:
                raise ValueError(f"Unknown platform: {platform!r}")
        except Exception as exc:
            logger.debug("list_installed_apps failed: %s", exc)
            return []

    async def _adb_install(self, device: str, apk_path: Path) -> str:
        proc = await asyncio.create_subprocess_exec(
            # -g grants all runtime permissions so the agent never burns tool calls
            # on dialogs. `pm clear` revokes them, so the runner re-grants per episode.
            "adb", "-s", device, "install", "-r", "-g", str(apk_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (stdout.decode() + stderr.decode()).strip()

    async def list_tool_names(self) -> set[str]:
        """Tool names the server at bridge_url exposes. Empty set if unreachable."""
        if self.bridge_url is None:
            return set()
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            async def _list() -> set[str]:
                async with streamablehttp_client(f"{self.bridge_url}/mcp") as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        return {t.name for t in (await s.list_tools()).tools}

            return await asyncio.wait_for(_list(), timeout=_DEVICE_OP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - callers treat this as "unknown"
            logger.debug("could not list tools at %s: %s", self.bridge_url, exc)
            return set()

    async def is_desktop_bridge(self) -> bool:
        """Is the MCP desktop app serving this port, rather than a standalone server?
        It looks identical but refuses every device tool until qg_acquire_device, so
        every episode would fail on its first tap. The lock tools are the discriminator."""
        if self.bridge_url is None:
            return False        # no bridge at all, desktop or otherwise
        return "qg_acquire_device" in await self.list_tool_names()

    # ── Cleanup ────────────────────────────────────────────────────────────────

    async def force_release(self, udid: str | None = None) -> None:
        """Best-effort: clear a stale device lock (no-op without a bridge). Pass
        ``udid`` to release a specific device. Holds are keyed by MCP session, so
        this only clears holds the harness took — not one left by a dead agent."""
        if self.bridge_url is None:
            return
        try:
            from mcp.client.session import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            args = {"udid": udid} if udid else {}

            async def _release() -> None:
                async with streamablehttp_client(f"{self.bridge_url}/mcp") as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        await s.call_tool("qg_release_device", args)

            await asyncio.wait_for(_release(), timeout=_DEVICE_OP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - best-effort, incl. a wedged bridge
            # Logged, not silent — an invisible failure here surfaces later as an
            # unexplained device-busy error.
            logger.debug("force_release(%s) did not clear the lock: %s", udid or "*", exc)

    async def check_device_available(
        self,
        device: str,
        retries: int = 340,     # x delay = ~17 min, just past the bridge's 15-min sweep
        delay: float = 3.0,
    ) -> None:
        """Probe-acquire the device to confirm it is free, then release the probe.
        The retry window is sized to outlast the bridge's 15-minute reclaim of
        abandoned holds; raises RuntimeError if the device is still busy after that."""
        if self.bridge_url is None:
            return          # no bridge, no lock
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        last_msg = ""
        for attempt in range(1, retries + 1):
            try:
                async with streamablehttp_client(f"{self.bridge_url}/mcp") as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        # Device locking is a bridge feature. A standalone server has
                        # no lock tools, and retrying a missing tool would burn the
                        # whole window before failing with a misleading "locked".
                        tools = {t.name for t in (await s.list_tools()).tools}
                        if "qg_acquire_device" not in tools:
                            logger.debug(
                                "no device-lock tools on this MCP server — nothing to "
                                "coordinate, proceeding with %s", device,
                            )
                            return
                        result = await s.call_tool("qg_acquire_device", {"udid": device})
                        text = " ".join(
                            getattr(b, "text", "") or ""
                            for b in (result.content or [])
                        ).strip()
                        if "device-busy" in text.lower():
                            last_msg = text
                            if attempt < retries:
                                # A stale hold can take the full 15-minute sweep to
                                # clear — say so periodically, at a visible log level.
                                waited = attempt * delay
                                if attempt == 1 or waited % 60 < delay:
                                    logger.warning(
                                        "device %s is held by a stale lock — waiting "
                                        "(%.0fs of up to %.0fs; the MCP server "
                                        "reclaims abandoned holds after 15 min)",
                                        device, waited, retries * delay,
                                    )
                                await asyncio.sleep(delay)
                            continue
                        # Acquired — release immediately, agent will re-acquire
                        await s.call_tool("qg_release_device", {})
                        return
            except Exception as exc:
                last_msg = str(exc)
                if attempt < retries:
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"Device {device!r} is locked and could not be released automatically.\n"
            f"MCP response: {last_msg}\n"
            f"Waited {retries * delay:.0f}s, past the bridge's 15-minute reclaim of\n"
            "abandoned holds — so this is not a hold that will clear on its own.\n"
            "The lock belongs to an MCP session the harness cannot release: a hold is\n"
            "keyed by session id, and qg_release_device only frees the caller's own.\n"
            "Fix: restart the MCP desktop app (or its bridge) to clear it, then retry."
        )


# ── Module-level helpers ───────────────────────────────────────────────────────

_SIMULATOR_UDID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$",
    re.IGNORECASE,
)


def _is_ios_simulator(device: str) -> bool:
    """Return True if the device identifier is an iOS Simulator UDID (UUID format)."""
    return bool(_SIMULATOR_UDID_RE.match(device))


async def _reset_simulator_app(device: str, bundle_id: str) -> None:
    """Wipe an iOS Simulator app's data container without reinstalling."""
    # Stop the app first; it may not be running, so errors are ignored.
    await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "terminate", device, bundle_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "get_app_container", device, bundle_id, "data",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    data_dir = Path(stdout.decode().strip())

    if proc.returncode != 0 or not data_dir.exists():
        raise RuntimeError(
            f"Could not locate data container for {bundle_id} on simulator {device}.\n"
            f"stderr: {stderr.decode().strip()}\n"
            "Is the app installed? Run the benchmark setup again."
        )

    for sub in ("Documents", "Library", "tmp", "SystemData"):
        p = data_dir / sub
        if p.exists():
            shutil.rmtree(p)
        p.mkdir()


async def list_adb_devices() -> list[str]:
    """Serials `adb devices` reports as ready, in adb's order."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb", "devices",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_DEVICE_OP_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return []
    serials = []
    for line in out.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


async def _first_adb_device() -> str | None:
    """First device `adb devices` reports as ready. Used in ADB-only mode."""
    serials = await list_adb_devices()
    return serials[0] if serials else None
