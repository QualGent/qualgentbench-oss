"""Count mcp interactions in front of the MCP server — an adb-level count of this arm
measures the server's implementation, not the agent's work (some tools bypass adb).
A byte-faithful TCP relay sniffs `tools/call`; never parses HTTP, so a bug can only miscount.

CreateBench v2 (QUA-2856) adds a second, STDIO relay (`run_stdio_relay`) in front of the
QualGent-MCP server a creation episode's author gets. Its calls are not device work, so
it writes its own ledger (`interactions.CreationLedger`, `creation_calls.json`) and never
`interactions.json`: the budget hook charges device interactions only."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

from .interactions import CreationLedger, InteractionLog

logger = logging.getLogger(__name__)

# A JSON-RPC tools/call request, matched over the framing-stripped byte stream so it
# survives pipelining. `name` and `method` come in either order per the client's
# serializer, and a serializer that emits `arguments` first pushes them far apart.
_CALL_RE = re.compile(
    rb'"method"\s*:\s*"tools/call".{0,2000}?"name"\s*:\s*"([A-Za-z0-9_\-]+)"'
    rb'|"name"\s*:\s*"([A-Za-z0-9_\-]+)".{0,2000}?"method"\s*:\s*"tools/call"',
    re.DOTALL,
)

# HTTP/1.1 chunk framing lands INSIDE the JSON of a client that streams its body,
# splitting the literals _CALL_RE anchors on. Stripping it from the SCAN COPY is safe:
# JSON forbids raw CRLF in strings, so a bare CRLF is always framing, never payload.
_CHUNK_FRAME_RE = re.compile(rb"\r\n[0-9a-fA-F]{1,8}(?:;[^\r\n]{0,256})?\r\n")

# Give up on a connection's scan buffer past this point rather than grow forever.
# Requests (client→server) are small — tool args, not screenshots — so hitting this
# means something unexpected; the reset is logged, never silent.
_MAX_BUF = 4 * 1024 * 1024


class _Scanner:
    """Incremental tools/call counter over one connection's client→server bytes.
    The whole bounded buffer is re-scanned each feed and matches deduped by COUNT —
    scanning only new bytes can never match a request straddling two TCP reads."""

    def __init__(self) -> None:
        self.buf = b""
        self.counted = 0
        self.overflowed = False

    def feed(self, chunk: bytes) -> list[str]:
        self.buf += chunk
        if len(self.buf) > _MAX_BUF:
            self.buf = chunk[-_CARRYOVER:]
            self.counted = 0
            self.overflowed = True
        scan = _CHUNK_FRAME_RE.sub(b"", self.buf)
        matches = _CALL_RE.findall(scan)
        fresh = matches[self.counted:]
        self.counted = len(matches)
        return [(a or b).decode("utf-8", "replace") for a, b in fresh]


# Tail kept when the scan buffer overflows, so a request in flight at that moment
# still has a chance to complete.
_CARRYOVER = 8192


class McpMeter:
    """Counting proxy in front of a MCP server."""

    def __init__(self, log: InteractionLog, upstream_url: str) -> None:
        self.log = log
        host, port = _split(upstream_url)
        self.upstream = (host, port)
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.Task] = set()
        # Client→server bytes relayed, recorded into interactions.json. A meter
        # reporting 0 interactions while this is large is a SCANNER failure, not a
        # quiet agent — without this the two were indistinguishable.
        self.bytes_c2s = 0

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Sever live connections — same reasoning as AdbMeter.stop: an SSE
            # stream or agent leftover never closes, and 3.12's wait_closed()
            # waits on every open handler.
            for task in list(self._conns):
                task.cancel()
            await asyncio.gather(*list(self._conns), return_exceptions=True)
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            except Exception:  # noqa: BLE001 — a closing proxy must not fail an episode
                pass
            self._server = None
        self.log.flush()

    def url(self, path: str = "/mcp") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _handle(self, client_r: asyncio.StreamReader,
                      client_w: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            await self._serve(client_r, client_w)
        finally:
            if task is not None:
                self._conns.discard(task)

    async def _serve(self, client_r: asyncio.StreamReader,
                     client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection(*self.upstream)
        except OSError:
            client_w.close()
            return

        async def to_upstream() -> None:
            scanner = _Scanner()
            try:
                while chunk := await client_r.read(65536):
                    up_w.write(chunk)
                    await up_w.drain()
                    self.bytes_c2s += len(chunk)
                    self.log.set_meta("mcp_meter_bytes", self.bytes_c2s)
                    for name in scanner.feed(chunk):
                        self.log.record_mcp(name)
                    if scanner.overflowed:
                        scanner.overflowed = False
                        logger.warning("mcp meter: scan buffer overflowed at %d "
                                       "bytes — a request in flight may have gone "
                                       "uncounted", _MAX_BUF)
            except (ConnectionError, asyncio.CancelledError):
                pass

        async def to_client() -> None:
            try:
                while chunk := await up_r.read(65536):
                    client_w.write(chunk)
                    await client_w.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass

        tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
        try:
            # FIRST_COMPLETED for the same reason as the ADB meter: an SSE response
            # leaves the client half of the socket open indefinitely, so waiting for
            # both directions would hang every request.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for w in (client_w, up_w):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass


def _split(url: str) -> tuple[str, int]:
    m = re.match(r"^(?:https?://)?([^:/]+)(?::(\d+))?", url.strip())
    if not m:
        return "127.0.0.1", 51821
    return m.group(1), int(m.group(2) or 80)


# ── stdio relay (CreateBench v2, QUA-2856) ─────────────────────────────────────
#
# The agent's MCP client spawns `python -m qualgentbench.mcp_meter stdio --ledger F --
# <server> [args]` in place of the server. The relay spawns the server with the SAME
# stdout and stderr (inherited: every byte the server answers reaches the client
# untouched) and sits on its stdin only, where the client's requests pass. Stdio MCP is
# newline-delimited JSON-RPC, so each request is one line and is read as JSON — no
# sniffing — and forwarded byte for byte whether or not it parses. The relay exits with
# the server; when the client closes stdin, the server's stdin is closed after it.

STDIO_LEDGER_FILE = "creation_calls.json"


def ledger_note(ledger: CreationLedger, line: bytes) -> None:
    """Record one client→server stdio message (a JSON-RPC request or a batch)."""
    try:
        msg = json.loads(line)
    except ValueError:
        return
    for m in (msg if isinstance(msg, list) else [msg]):
        if not isinstance(m, dict):
            continue
        method = m.get("method")
        params = m.get("params") if isinstance(m.get("params"), dict) else {}
        if method == "tools/call" and isinstance(params.get("name"), str):
            ledger.record_call(params["name"])
        elif method == "resources/read" and isinstance(params.get("uri"), str):
            ledger.record_resource(params["uri"])
        elif method == "initialize":
            ledger.record_session()


def run_stdio_relay(ledger_path: str | Path, command: list[str]) -> int:
    """Relay this process's stdin to `command` (the MCP server) and meter it; return
    the server's exit code. stdout/stderr are the server's own."""
    ledger = CreationLedger(Path(ledger_path))
    ledger.flush()                      # the file exists from the start: "no calls" ≠ "no relay"
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE)
    except OSError as exc:
        sys.stderr.write(f"qualgentbench stdio meter: cannot start {command[0]}: {exc}\n")
        return 127
    assert proc.stdin is not None

    def pump() -> None:
        stdin = sys.stdin.buffer
        try:
            for line in iter(stdin.readline, b""):
                proc.stdin.write(line)
                proc.stdin.flush()
                ledger_note(ledger, line)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threading.Thread(target=pump, name="stdio-meter", daemon=True).start()
    return proc.wait()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m qualgentbench.mcp_meter stdio --ledger FILE -- COMMAND [ARGS...]"
    if len(args) < 5 or args[0] != "stdio" or args[1] != "--ledger" or args[3] != "--":
        sys.stderr.write(usage + "\n")
        return 2
    return run_stdio_relay(args[2], args[4:])


if __name__ == "__main__":
    sys.exit(main())
