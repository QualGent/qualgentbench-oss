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
#
# Hidden payloads (QUA-2794): every rule above reads the request TEXT, so a command
# the meter never sees as text used to escape all of them. `_hidden_payload` closes
# the three ways that happens — an empty-command shell fed on stdin, a shell
# interpreter reading its script from stdin or a file, and running a file the agent
# pushed or wrote — so that whatever reaches the device is text these rules have read.
# TODO(QUA-2794 follow-up): the durable fix for the root path is a bench image without
# `su` (QUA-2790's option (b)); it changes the AVD and needs a full corpus re-derive,
# so it waits until QUA-2786's comparison no longer needs the image held fixed.
_DENY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("run-as", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?(?<![\w-])run-as(?![\w-])")),
    ("app sandbox", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?/data/(?:data|user(?:_de)?/\d+)/")),
    ("harness scratch", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?/data/local/tmp/qgb")),
    ("harness files", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?(?:qgb_flags|\.qgb(?:/|\b))")),
    ("backup", re.compile(r"^(?:backup|restore):")),
    ("su", re.compile(r"^(?:shell|exec|shell,v2)[:,].*?(?<![\w.-])su(?![\w./-])")),
)


# Hidden-payload rules (QUA-2794). The rules above read the request TEXT; these close
# the three ways a command reaches the device WITHOUT its text ever appearing in the
# request, so a payload can no longer ride in behind a clean-looking request line.
#
# A world-writable directory: anything the agent (or `adb push`, or a `> file`) can
# write. Executing a file OUT of one is how a pushed script runs by path. `/data/local/
# tmp/qgb*` is already denied above as harness scratch; the rest of `/data/local/tmp`
# and the external-storage roots are added here (as the EXECUTABLE only — reading or
# writing `/sdcard/foo` stays allowed; a tester screencaps and dumps there).
_WORLD_WRITABLE = re.compile(
    r"^(?:/sdcard|/storage/(?:emulated|self)/\S+|/storage/[^/]+|/mnt/(?:sdcard|media_rw|user/\d+)"
    r"|/data/local/tmp)(?:/|$)")

# Shell interpreters. `toybox`/`busybox` are multiplexers whose sub-command is the
# real interpreter (`toybox sh …`).
_INTERP = frozenset({"sh", "bash", "dash", "ash", "mksh", "ksh", "csh", "hush"})
_MUX = frozenset({"toybox", "busybox"})

# Split a shell body into top-level command segments. Quotes are stripped before this
# runs, so what remains are the real chain/pipe/subshell boundaries.
_SEG_SPLIT = re.compile(r"&&|\|\||\$\(|[;|&\n()`]")

# Wrapper words that precede the real command and take it (or its args) as their tail.
_WRAPPERS = frozenset({
    "env", "nohup", "time", "exec", "sudo", "builtin", "command", "setsid",
    "stdbuf", "ionice", "nice", "xargs", "timeout", "do", "then", "else", "!", "{",
})


def _skip_wrappers(toks: list[str]) -> list[str]:
    """Drop leading wrapper words, options, numbers (a `timeout 5`) and `VAR=val`
    assignments, returning the tokens from the real command word on."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if (t in _WRAPPERS or t.startswith("-")
                or re.fullmatch(r"\d+(?:\.\d+)?[sm]?", t)
                or ("=" in t and "/" not in t.split("=", 1)[0])):
            i += 1
            continue
        break
    return toks[i:]


def _interp_c_index(cmd: list[str]) -> int | None:
    """The index of `cmd`'s `-c` option (alone or in a cluster like `-ec`), or None
    when the interpreter has none. With `-c` the next token is inline code, visible in
    the request; without it the interpreter reads its script from stdin or a file."""
    start = 2 if cmd[0] in _MUX else 1
    for j in range(start, len(cmd)):
        a = cmd[j]
        if not a.startswith("-") or a == "--":
            return None                      # a non-option (a script file) came first
        if "c" in a[1:]:
            return j
    return None


def _scan_body(body: str, depth: int = 0) -> str | None:
    """A deny reason for a shell command body whose real command never appears as
    readable text, or None. Recurses into an interpreter's `-c` argument, so an inline
    `sh -c '…'` is allowed but its text is scanned exactly like a top-level command
    (the ticket's exception)."""
    if depth > 4:                            # a pathological `sh -c 'sh -c …'` nest
        return "script shell"
    for seg in _SEG_SPLIT.split(body):
        cmd = _skip_wrappers(seg.split())
        if not cmd:
            continue
        head = cmd[0]
        privileged = _privileged_command(cmd)
        if privileged is not None:
            return privileged
        if _WORLD_WRITABLE.match(head):
            # Executing a file the agent pushed or wrote (`/sdcard/x`, a chmod'd
            # `/data/local/tmp/x`). Reading or writing such a path is unaffected —
            # only invoking one as the command is denied.
            return "world-writable exec"
        if head in (".", "source") and len(cmd) > 1:
            return "script shell"            # sources a file
        base = head.rsplit("/", 1)[-1]
        is_interp = base in _INTERP or (base in _MUX and len(cmd) > 1
                                        and cmd[1].rsplit("/", 1)[-1] in _INTERP)
        if is_interp:
            c = _interp_c_index(cmd)
            if c is None:
                return "script shell"        # reads from stdin (`sh`, `sh -s`) or a file
            reason = _scan_body(" ".join(cmd[c + 1:]), depth + 1)
            if reason is not None:
                return reason
    return None


# adbd privilege and device-state services (QUA-2795). `adb root` is not a shell
# request: the client selects the transport (`host:tport:serial:<s>`) and then sends
# the bare device service `root:` on the same connection, which the rules above never
# read (they match `shell:`/`exec:` text) and `classify` calls plumbing. A root adbd
# makes every later `adb shell` uid 0, which defeats the path rules and `su` rule
# without needing either. Measured wire shapes (adb 36.0.2 against the android-35
# image): `root:`, `unroot:`, `reboot:[arg]`, `tcpip:<port>`, `usb:` arrive as bare
# services; `remount`, `disable-verity`, `enable-verity` arrive as `shell,v2,raw:<verb>`
# (the client's remount_shell feature) and are caught as command words in
# `_scan_body`. `usb:`/`tcpip:` restart adbd, and a restarted adbd comes up ROOT when
# `service.adb.root` is 1 — which the SHELL user may set (measured: `setprop
# service.adb.root 1` then `adb usb` gave uid 0 with no `root:` request), so both the
# restarts and the property writes are refused. None of these reach a device via a
# `host-serial:`/`host-transport-id:` prefix: the server answers "unknown host
# service" (measured), so only the bare form needs a rule. `host:kill` (kill-server)
# stops the upstream server every lane and the MCP server share.
_PRIVILEGED_SERVICE = re.compile(
    r"^(root|unroot|remount|reboot|disable-verity|enable-verity|tcpip|usb|"
    r"sideload|sideload-host)(?::|$)")
# Shell verbs that do the same from inside `adb shell` (`reboot` needs no root).
_PRIVILEGED_VERBS = frozenset({"reboot", "remount", "disable-verity", "enable-verity"})
# Properties that prime or restart adbd, or power-cycle the device.
_PRIVILEGED_PROP = re.compile(r"^(?:service\.adb\.|persist\.adb\.|ctl\.|sys\.powerctl$|"
                              r"sys\.usb\.)")


def _privileged_service(low: str) -> str | None:
    """A deny reason for a bare adbd service that changes adbd's privilege or the
    device's state (`root:`, `reboot:bootloader`, `usb:`, …), or for `host:kill`."""
    if low == "host:kill":
        return "adb kill-server"
    m = _PRIVILEGED_SERVICE.match(low)
    return f"adb {m.group(1)}" if m else None


def _privileged_command(cmd: list[str]) -> str | None:
    """A deny reason for one shell command (wrappers already skipped) that reboots the
    device, remounts it, flips verity, or writes an adbd/power property."""
    base = cmd[0].rsplit("/", 1)[-1]
    if base in _PRIVILEGED_VERBS:
        return f"adb {base}" if base != "reboot" else "reboot"
    if base == "svc" and len(cmd) > 2 and cmd[1] == "power" and cmd[2] in ("reboot", "shutdown"):
        return "reboot"
    if base == "setprop" and len(cmd) > 1 and _PRIVILEGED_PROP.match(cmd[1]):
        return "adbd property"
    return None


def _hidden_payload(low: str) -> str | None:
    """A deny reason for a shell request whose real command never appears as text, or
    None. `low` is already lowercased with quotes and backslashes stripped."""
    if low.startswith("shell:"):
        body = low[len("shell:"):]
    elif low.startswith("exec:"):
        body = low[len("exec:"):]
    elif low.startswith("shell,v2"):
        body = low.split(":", 1)[1] if ":" in low else ""
    else:
        return None

    if not body.strip():
        # An interactive/empty-command shell: the payload arrives on the stream that
        # `echo '…' | adb shell` (or a bare `adb shell`) opens. Nothing to read here.
        return "stdin shell"
    return _scan_body(body)


def deny_reason(request: str) -> str | None:
    """Why this ADB service request must not reach the server, or None. Quotes and
    backslashes are stripped before matching, so `run-as 'com.x'`, `"run-as"` and
    `s\\u` read the same as the bare word (the shell drops them the same way)."""
    low = request.strip().lower().replace("'", "").replace('"', "").replace("\\", "")
    for name, rx in _DENY_RULES:
        if rx.search(low):
            return name
    return _privileged_service(low) or _hidden_payload(low)


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
