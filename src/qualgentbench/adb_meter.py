"""Count device operations at the wire: a proxy on the ADB server socket sees every
operation however it was invoked, where a text heuristic is escaped by a script run
by path. Only the service request counts (`host:*` is plumbing); then the stream goes opaque."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .interactions import InteractionLog

DEFAULT_UPSTREAM_PORT = 5037

# Mutating: these change device or app state — what a QA episode is charged for.
_ACTION_RE = re.compile(
    r"^(?:shell|exec|shell,v2)[:,].*?\b("
    r"input|am|pm|monkey|svc|ime|content|cmd\s+package|setprop|"
    r"settings\s+put|wm\s+(?:size|density)|rm|mkdir|mv|cp|touch|"
    r"uiautomator\s+runtest|screenrecord"
    r")\b")

# Reading: observe without changing anything. Recorded separately — charging an
# agent for LOOKING penalises careful QA.
_OBSERVE_RE = re.compile(
    r"^(?:shell|exec|shell,v2)[:,].*?\b("
    r"uiautomator\s+dump|dumpsys|screencap|getprop|getevent|"
    r"ls|cat|find|stat|df|ps|logcat|sqlite3|pm\s+list|am\s+stack|wm\s+size"
    r")\b")

_PULL_PUSH_RE = re.compile(r"^sync:")


@dataclass
class Counts:
    total: int = 0            # every device-bound request — the budget unit
    actions: int = 0          # mutating
    observations: int = 0     # reading
    other: int = 0            # device-bound but unclassified
    services: dict[str, int] = field(default_factory=dict)

    def as_metrics(self) -> dict:
        return {
            "metered_total": self.total,
            "metered_actions": self.actions,
            "metered_observations": self.observations,
            "metered_other": self.other,
        }


def classify(request: str) -> str:
    """'plumbing' | 'action' | 'observation' | 'other' for one ADB service request."""
    req = request.strip()
    low = req.lower()
    if low.startswith("host:") or low.startswith("host-serial:") or low.startswith("host-transport"):
        return "plumbing"
    if _PULL_PUSH_RE.match(low):
        # adb pull/push. Treated as observation: pull (screen dumps) dominates, and
        # the split is reported, not enforced.
        return "observation"
    if _ACTION_RE.match(low):
        return "action"
    if _OBSERVE_RE.match(low):
        return "observation"
    if low.startswith(("shell:", "exec:", "shell,v2:", "framebuffer:", "jdwp:", "reverse:")):
        return "other"
    return "plumbing"


class AdbMeter:
    """Counting TCP proxy in front of the real ADB server; point a client at it with
    ANDROID_ADB_SERVER_PORT. Counts are flushed to `counter_path` after every
    device-bound request — the budget hook reads that file from another process."""

    def __init__(self, counter_path: Path, upstream_port: int = DEFAULT_UPSTREAM_PORT,
                 upstream_host: str = "127.0.0.1",
                 log: "InteractionLog | None" = None) -> None:
        self.counter_path = Path(counter_path)
        self.upstream = (upstream_host, upstream_port)
        self.counts = Counts()
        # The shared step unit. The raw adb counts stay as a diagnostic, but they are
        # not the budget — they measure transport, not work.
        self.log = log
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._flush()
        return self.port

    async def stop(self) -> Counts:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 — a closing proxy must never fail an episode
                pass
            self._server = None
        self._flush()
        return self.counts

    async def __aenter__(self) -> "AdbMeter":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ── counting ─────────────────────────────────────────────────────────────

    def _record(self, request: str) -> None:
        if self.log is not None:
            self.log.record_adb(request)
        kind = classify(request)
        if kind == "plumbing":
            return
        self.counts.total += 1
        if kind == "action":
            self.counts.actions += 1
        elif kind == "observation":
            self.counts.observations += 1
        else:
            self.counts.other += 1
        head = request.split(":", 1)[-1].strip().split(" ")[0][:40] or request[:40]
        self.counts.services[head] = self.counts.services.get(head, 0) + 1
        self._flush()

    def _flush(self) -> None:
        """Atomic write — a half-written file would parse as a lower count and hand
        the budget hook back budget that was already spent."""
        try:
            tmp = self.counter_path.with_suffix(".tmp")
            payload = dict(self.counts.as_metrics(), services=self.counts.services)
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.counter_path)
        except OSError:
            pass

    # ── proxying ─────────────────────────────────────────────────────────────

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> bytes | None:
        """One length-prefixed request, with its prefix so it can be relayed verbatim.
        None at EOF or on a malformed prefix — some clients open a connection and
        drop it, which is not worth failing on."""
        try:
            prefix = await reader.readexactly(4)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        try:
            length = int(prefix.decode("ascii"), 16)
        except (ValueError, UnicodeDecodeError):
            return None
        if length < 0 or length > 65535:
            return None
        try:
            body = await reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        return prefix + body

    async def _handle(self, client_r: asyncio.StreamReader,
                      client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection(*self.upstream)
        except OSError:
            client_w.close()
            return

        try:
            # Parse requests until the connection turns into an opaque stream.
            while True:
                framed = await self._read_request(client_r)
                if framed is None:
                    break
                request = framed[4:].decode("utf-8", "replace")
                self._record(request)
                up_w.write(framed)
                await up_w.drain()

                status = b""
                try:
                    status = await asyncio.wait_for(up_r.readexactly(4), timeout=30)
                except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                    break
                client_w.write(status)
                await client_w.drain()
                if status != b"OKAY":
                    break
                low = request.lower()
                # Device selection — the real service follows on the SAME connection,
                # so keep parsing. Current adb sends `host:tport:`; matching only the
                # older `transport` spelling once made the meter count zero.
                if low.startswith(("host:transport", "host-transport")):
                    continue
                if low.startswith("host:tport"):
                    # tport answers OKAY + an 8-byte transport id before handover.
                    # Relay it, then keep parsing.
                    try:
                        tid = await asyncio.wait_for(up_r.readexactly(8), timeout=30)
                    except (asyncio.IncompleteReadError, ConnectionError,
                            asyncio.TimeoutError):
                        break
                    client_w.write(tid)
                    await client_w.drain()
                    continue
                break             # everything else: the stream is now opaque
            await self._pipe(client_r, client_w, up_r, up_w)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for w in (client_w, up_w):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    async def _pipe(client_r, client_w, up_r, up_w) -> None:
        """Relay until EITHER direction ends, then tear the other down. Waiting for
        both deadlocks on every `host:` request — the server closes but the client
        keeps its half open, so `adb devices` hung until this used FIRST_COMPLETED."""
        async def copy(reader, writer):
            try:
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
            except (ConnectionError, asyncio.CancelledError, RuntimeError):
                pass

        tasks = [asyncio.create_task(copy(client_r, up_w)),
                 asyncio.create_task(copy(up_r, client_w))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def read_counts(counter_path: Path) -> dict:
    """Counts as last flushed. Read by the budget hook and by the runner."""
    try:
        data = json.loads(Path(counter_path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
