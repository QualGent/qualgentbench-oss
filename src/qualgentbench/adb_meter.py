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


def upstream_from_env() -> tuple[str, int]:
    """Where the real ADB server is. The adb binary reads ANDROID_ADB_SERVER_ADDRESS,
    adbutils/uiautomator2 read ANDROID_ADB_SERVER_HOST; honouring both lets the
    harness run in a container while adb and the emulators stay on the host."""
    import os

    host = (os.environ.get("ANDROID_ADB_SERVER_ADDRESS")
            or os.environ.get("ANDROID_ADB_SERVER_HOST") or "127.0.0.1")
    try:
        port = int(os.environ.get("ANDROID_ADB_SERVER_PORT") or DEFAULT_UPSTREAM_PORT)
    except ValueError:
        port = DEFAULT_UPSTREAM_PORT
    return host, port

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

# Denied outright — the answer key lives on the device. `run-as <pkg>` is the only
# door into a debuggable app's private sandbox on a non-rooted image, and behind it
# sit `files/qgb_flags.txt` (the seeded bug ids of this episode) and
# `files/.qgb/fired/` (which seeded paths ran). `backup:` streams the same sandbox
# out as a tarball, and `/data/local/tmp/qgb*` is the harness's own scratch. A denied
# request is answered `FAIL` at the socket and never reaches the ADB server; it is
# recorded (`metered_denied`) so a probing agent is visible in the artifact. The
# app's own state that a tester may legitimately read — its screen, its logs, its
# exit-info — none of it needs run-as.
#
# `su` (QUA-2790): the android-35 `google_apis` images ship `/system/xbin/su`, so any
# agent with `adb shell` could get a root shell with `su 0` while adbd stays unrooted
# (the episode-start invariant's `id -u` reads 2000). The path rules above are literal
# matches over the request text, and a root shell does not need the literal paths
# (`su 0 sh -c 'cd /data/da*/com.x/f*; cat q*'` matches none of them), so the only
# rule that holds is refusing `su` itself. It matches `su` as a whole shell word
# anywhere in the command — after `;`, `&&`, `|`, `$(`, inside `sh -c '…'` (quotes and
# backslashes are stripped first) and as a path (`/system/xbin/su`) — but not inside
# a longer word, a dotted name or a directory (`dumpsys`, `summary`,
# `/sdcard/results`, `com.example.su`, `su.txt`, `/sdcard/su/x`). The price is that a
# bare `su` used as DATA (`grep su`, `input text su`) is refused too; the agent is told
# why and can rephrase. The harness's own privileged steps (`set_adb_root`,
# `episode_runner.clear_crash_history`) run over the harness's own adb and never pass
# through this meter.
# TODO(QUA-2790 follow-up): these rules read the request TEXT, so a command the meter
# never sees as text escapes every one of them: `echo 'su 0 id' | adb shell` (an
# empty-command shell fed on stdin), `adb shell sh` with a script on stdin, or a script
# pushed over `sync:` and run by path. Closing the root path for good means an image
# without `su` (the ticket's option (b)), not a longer deny list.
_DENY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("run-as", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?(?<![\w-])run-as(?![\w-])")),
    ("app sandbox", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?/data/(?:data|user(?:_de)?/\d+)/")),
    ("harness scratch", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?/data/local/tmp/qgb")),
    ("harness files", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?(?:qgb_flags|\.qgb(?:/|\b))")),
    ("backup", re.compile(r"^(?:backup|restore):")),
    ("su", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?(?<![\w.-])su(?![\w./-])")),
)


def deny_reason(request: str) -> str | None:
    """Why this ADB service request must not reach the server, or None. Quotes and
    backslashes are stripped before matching, so `run-as 'com.x'`, `"run-as"` and
    `s\\u` read the same as the bare word (the shell drops them the same way)."""
    low = request.strip().lower().replace("'", "").replace('"', "").replace("\\", "")
    for name, rx in _DENY_RULES:
        if rx.search(low):
            return name
    return None


@dataclass
class Counts:
    total: int = 0            # every device-bound request — the budget unit
    actions: int = 0          # mutating
    observations: int = 0     # reading
    other: int = 0            # device-bound but unclassified
    denied: int = 0           # refused at the socket (answer-key paths); not charged
    services: dict[str, int] = field(default_factory=dict)

    def as_metrics(self) -> dict:
        return {
            "metered_total": self.total,
            "metered_actions": self.actions,
            "metered_observations": self.observations,
            "metered_other": self.other,
            "metered_denied": self.denied,
        }


def classify(request: str) -> str:
    """'plumbing' | 'action' | 'observation' | 'other' for one ADB service request."""
    req = request.strip()
    low = req.lower().replace("'", "").replace('"', "")
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

    def __init__(self, counter_path: Path, upstream_port: int | None = None,
                 upstream_host: str | None = None,
                 log: "InteractionLog | None" = None) -> None:
        self.counter_path = Path(counter_path)
        env_host, env_port = upstream_from_env()
        self.upstream = (upstream_host or env_host,
                         upstream_port if upstream_port is not None else env_port)
        self.counts = Counts()
        # The shared step unit. The raw adb counts stay as a diagnostic, but they are
        # not the budget — they measure transport, not work.
        self.log = log
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._flush()
        return self.port

    async def stop(self) -> Counts:
        if self._server is not None:
            self._server.close()
            # Sever the live connections. The episode owns this meter; a stream an
            # agent left behind (a detached `adb root` or logcat) never closes its
            # end, and Python 3.12's wait_closed() waits on every open handler —
            # a lane once sat an hour on exactly that.
            for task in list(self._conns):
                task.cancel()
            await asyncio.gather(*list(self._conns), return_exceptions=True)
            try:
                # Bounded: a connection accepted before close() whose handler had
                # not yet registered itself would otherwise reopen the same wait.
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
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

    def _deny(self, request: str, why: str) -> None:
        """A refused request: counted under `denied` and NOT under `total` (the device
        saw nothing), and recorded in the interaction log exactly as the same request
        would be if it had been relayed (the artifact must show the attempt). That log
        is `interactions.json`, the budget every adapter reads, so a denied request
        costs the agent the same step(s) `interactions.classify_adb_all` gives it
        relayed — `su 0 id` is one `other` either way. Denying never makes a probe
        cheaper than running it."""
        if self.log is not None:
            self.log.record_adb(request)
        self.counts.denied += 1
        head = request.split(":", 1)[-1].strip()[:80]
        self.counts.services[f"denied:{head}"] = self.counts.services.get(f"denied:{head}", 0) + 1
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

        try:
            # Parse requests until the connection turns into an opaque stream.
            while True:
                framed = await self._read_request(client_r)
                if framed is None:
                    break
                request = framed[4:].decode("utf-8", "replace")
                why = deny_reason(request)
                if why is not None:
                    # Answer at the socket and hang up: nothing is relayed, and the
                    # connection must not fall through to the opaque pipe below.
                    self._deny(request, why)
                    client_w.write(_fail_frame(f"qualgentbench: {why} is not available to the agent"))
                    await client_w.drain()
                    return
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


def _fail_frame(message: str) -> bytes:
    """An ADB-protocol failure reply: `FAIL` + 4-hex length + message — what the
    server itself sends for an unknown service, so every client prints it."""
    body = message.encode("utf-8")[:0xFFFF]
    return b"FAIL" + f"{len(body):04x}".encode("ascii") + body


def read_counts(counter_path: Path) -> dict:
    """Counts as last flushed. Read by the budget hook and by the runner."""
    try:
        data = json.loads(Path(counter_path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
