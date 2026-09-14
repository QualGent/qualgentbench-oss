"""Crash attribution: which process died, and was it the app under test?

`adb logcat -b crash` is a device-wide, shared, never-cleared ring buffer. Counting
`FATAL EXCEPTION` in it charges every crash on the emulator — the keyboard's native
abort, a `uiautomator dump` shell command dying, a media provider — to whatever app
we happen to be looking at. This module parses the buffer into per-process records
so callers can split crashes three ways (`app`, `app-native`, `foreign`) and never
charge a foreign crash to the app under test.

Two independent signals are exposed, because each misses cases the other catches:

* the crash logcat buffer — full stack, but ANRs never land in it (on this emulator,
  Android 16, they are logged by ActivityManager to the main/system buffers), and
  shell-spawned java commands log no `Process:` line at all;
* `dumpsys activity exit-info <package>` — no stack, but it records ANRs and native
  crashes the buffer might have rolled past. Our own harness writes
  `USER_REQUESTED`/`SIGNALED`/`PACKAGE_UPDATED` records on every force-stop and
  reinstall, so callers must gate on `FATAL_REASONS`, never on "a record exists".

Parsing and classification are pure text -> dataclass functions so they are testable
against captured fixtures (tests/fixtures/crash/). The adb wrappers at the bottom are
thin and read-only: they use `logcat -T <since>` time windows, never `logcat -c`,
because clearing a shared buffer is the anti-pattern this module replaces.

Real formats observed on emulator-5554 (Android 16, `adb logcat -d -b crash`, default
== `-v threadtime`):

    09-08 23:00:23.458 28567 28567 E AndroidRuntime: FATAL EXCEPTION: main
    09-08 23:00:23.458 28567 28567 E AndroidRuntime: Process: com.futsch1.medtimer, PID: 28567
    09-08 23:00:23.458 28567 28567 E AndroidRuntime: android.view.InflateException: Binary XML ...
    09-08 23:00:23.458 28567 28567 E AndroidRuntime: Caused by: java.lang.IllegalStateException: ...
    09-08 23:00:23.458 28567 28567 E AndroidRuntime: \tat androidx.fragment.app.FragmentManager.checkStateLoss(FragmentManager.java:1632)

    09-08 18:53:12.607  1705  2749 F libc    : Fatal signal 6 (SIGABRT), code -1 (SI_QUEUE) in tid 2749 (Thread-5), pid 1705 (rs.media.module)
    09-08 18:53:14.192 11026 11026 F DEBUG   : *** *** *** *** *** *** *** *** *** *** *** *** *** *** *** ***
    09-08 18:53:14.192 11026 11026 F DEBUG   : pid: 1705, tid: 2749, name: Thread-5  >>> com.google.android.providers.media.module <<<
    09-08 18:53:14.192 11026 11026 F DEBUG   : signal 6 (SIGABRT), code -1 (SI_QUEUE), fault addr --------
    09-08 18:53:14.192 11026 11026 F DEBUG   : Abort message: 'Check failed: ...'
    09-08 18:53:14.192 11026 11026 F DEBUG   :       #00 pc 00000000000754b0  /apex/com.android.runtime/lib64/bionic/libc.so (abort+156) (BuildId: ...)

Note the native block is logged by the crash_dump helper (pid 11026), not by the
dying process; the real pid/process come from the `pid: ..., >>> proc <<<` line.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass, field

from .device import _adb_bin

logger = logging.getLogger(__name__)

FATAL_REASONS = frozenset({"CRASH", "CRASH_NATIVE", "ANR"})

# ApplicationExitInfo reason codes -> stable names. The dump prints human labels
# ("APP CRASH(NATIVE)", "USER REQUESTED", "OTHER KILLS BY SYSTEM") that differ from
# the SDK constants; the numeric code is the reliable key.
_REASON_NAMES = {
    0: "UNKNOWN", 1: "EXIT_SELF", 2: "SIGNALED", 3: "LOW_MEMORY", 4: "CRASH",
    5: "CRASH_NATIVE", 6: "ANR", 7: "INITIALIZATION_FAILURE", 8: "PERMISSION_CHANGE",
    9: "EXCESSIVE_RESOURCE_USAGE", 10: "USER_REQUESTED", 11: "USER_STOPPED",
    12: "DEPENDENCY_DIED", 13: "OTHER", 14: "FREEZER", 15: "STATE_CHANGE",
    16: "PACKAGE_UPDATED", 17: "ISOLATED_NOT_NEEDED",
}
_SUBREASON_NAMES = {
    0: None, 1: "WAIT_FOR_DEBUGGER", 2: "TOO_MANY_CACHED", 3: "TOO_MANY_EMPTY",
    4: "TRIM_EMPTY", 5: "LARGE_CACHED", 6: "MEMORY_PRESSURE", 7: "EXCESSIVE_CPU",
    8: "SYSTEM_UPDATE_DONE", 9: "KILL_ALL_FG", 10: "KILL_ALL_BG_EXCEPT",
    11: "KILL_UID", 12: "KILL_PID", 13: "INVALID_START", 14: "INVALID_STATE",
    15: "IMPERCEPTIBLE", 16: "REMOVE_LRU", 17: "ISOLATED_NOT_NEEDED",
    18: "CACHED_APP_LIMIT", 19: "FREEZER_BINDER_IOCTL", 20: "FREEZER_BINDER_TRANSACTION",
    21: "FORCE_STOP", 22: "REMOVE_TASK", 23: "STOP_APP", 24: "KILL_BACKGROUND",
    25: "PACKAGE_UPDATE", 26: "UNDELIVERED_BROADCAST", 27: "EXCESSIVE_BINDER_OBJECTS",
    28: "OOM_KILL", 29: "FREEZER_BINDER_ASYNC_FULL", 30: "BIND_SERVICE",
}


@dataclass
class CrashRecord:
    process: str          # as logged: "org.example.app" or "org.example.app:service"; "" if unknown
    pid: int | None
    timestamp: str        # logcat timestamp text as printed, e.g. "09-08 23:00:23.458"
    kind: str             # "java" | "native" | "anr"
    exception: str        # java: exception class; native: signal name; anr: "ANR"
    message: str          # first line of the message, trimmed
    frames: list[str] = field(default_factory=list)   # "at ..." (java) / "#NN pc ..." (native)
    raw: str = ""


@dataclass
class ExitInfo:
    pid: int | None
    reason: str           # normalised name: CRASH, CRASH_NATIVE, ANR, USER_REQUESTED, SIGNALED, ...
    timestamp: str        # as printed: "2026-09-09 00:10:36.610"
    description: str
    sub_reason: str | None
    process: str = ""
    reason_code: int = 0

    @property
    def fatal(self) -> bool:
        return self.reason in FATAL_REASONS


@dataclass
class EventRecord:
    kind: str             # "crash" | "anr"
    timestamp: str
    pid: int | None
    package: str
    detail: str           # am_crash: exception class; am_anr: reason


# ---------------------------------------------------------------- logcat line formats

# threadtime (the device default): "09-08 23:00:23.458 28567 28567 E AndroidRuntime: msg"
# optionally with a year prefix (-v year). Tag is padded; message may be empty.
_THREADTIME_RE = re.compile(
    r"^(?:\d{4}-)?(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
    r"\s+(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<lvl>[VDIWEFS])\s+(?P<tag>[^:]*?)\s*:(?: (?P<msg>.*))?$"
)
# time: "09-08 23:00:23.458 E/AndroidRuntime(28567): msg"
_TIME_RE = re.compile(
    r"^(?:\d{4}-)?(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
    r"\s+(?P<lvl>[VDIWEFS])/(?P<tag>[^(]*?)\s*\(\s*(?P<pid>\d+)\):(?: (?P<msg>.*))?$"
)

_JAVA_HEADER_RE = re.compile(r"^FATAL EXCEPTION: (?P<thread>.*)$")
_JAVA_PROCESS_RE = re.compile(r"^(?:Process: (?P<proc>\S+?),\s*)?PID: (?P<pid>\d+)\s*$")
_JAVA_EXC_RE = re.compile(r"^(?P<cls>[A-Za-z_$][\w$.]*(?:Exception|Error|Throwable|[A-Z]\w*))(?::\s?(?P<msg>.*))?$")
_JAVA_FRAME_RE = re.compile(r"^\s*at\s+\S")
_JAVA_CONT_RE = re.compile(r"^\s*(Caused by:|Suppressed:|\.\.\. \d+ more)")

_NATIVE_HEADER_RE = re.compile(r"^\*\*\* \*\*\* \*\*\*")
_NATIVE_PID_RE = re.compile(r"^pid: (?P<pid>\d+), tid: (?P<tid>\d+), name: (?P<thread>.*?)\s+>>> (?P<proc>\S+) <<<")
_NATIVE_SIGNAL_RE = re.compile(r"^signal (?P<num>\d+) \((?P<name>SIG[A-Z0-9]+)\)")
_NATIVE_ABORT_RE = re.compile(r"^Abort message: '(?P<msg>.*)'\s*$")
_NATIVE_FRAME_RE = re.compile(r"^\s*#\d+ pc ")

_ANR_HEADER_RE = re.compile(r"^ANR in (?P<proc>\S+?)(?: \((?P<component>[^)]*)\))?\s*$")
_ANR_PID_RE = re.compile(r"^PID: (?P<pid>\d+)")
_ANR_REASON_RE = re.compile(r"^Reason: (?P<reason>.*)$")


def _parse_line(line: str) -> tuple[str, int, str, str] | None:
    """-> (timestamp, pid, tag, message) or None for non-log lines."""
    m = _THREADTIME_RE.match(line) or _TIME_RE.match(line)
    if not m:
        return None
    try:
        pid = int(m.group("pid"))
    except (TypeError, ValueError):
        return None
    return m.group("ts"), pid, m.group("tag").strip(), (m.group("msg") or "")


class _Open:
    """A record under construction plus parser state for its tag/pid stream."""
    __slots__ = ("lines", "rec", "stage")

    def __init__(self, rec: CrashRecord, stage: str) -> None:
        self.rec = rec
        self.stage = stage
        self.lines: list[str] = []


def parse_crash_buffer(text: str) -> list[CrashRecord]:
    """Parse `adb logcat -d -b crash` output (threadtime or time format) into records.

    Records are keyed by the logging pid while open, so blocks from different
    processes may interleave line-by-line. Lines that fit no format, or belong to no
    open record, are skipped. Never raises on malformed input.

    Java blocks: `FATAL EXCEPTION: <thread>` then `Process: <name>, PID: <n>` (or just
    `PID: <n>` for shell-spawned commands such as `uiautomator dump`, which then have
    process == ""), then the exception line, then frames. The `Couldn't report
    crash. Here's the crash:` re-dump that follows on some builds is folded into
    `raw` and does not create a second record.

    Native blocks: `*** *** ***` header from crash_dump; process/pid come from the
    `pid: N, tid: M, name: T  >>> proc <<<` line, the signal from `signal N (SIGxxx)`,
    the message from `Abort message: '...'` when present (else the signal line).

    ANR blocks (`ANR in <proc> (<component>)`, `PID: n`, `Reason: ...`) are parsed if
    present, but on this emulator (Android 16) they are written to the main/system
    buffers, not `-b crash` — use `parse_exit_info` / `exit_info` to catch ANRs.
    """
    open_by_pid: dict[int, _Open] = {}
    done: list[CrashRecord] = []

    def close(o: _Open) -> None:
        o.rec.raw = "\n".join(o.lines)
        done.append(o.rec)

    for line in (text or "").splitlines():
        try:
            parsed = _parse_line(line.rstrip("\r"))
            if parsed is None:
                continue
            ts, pid, tag, msg = parsed
            cur = open_by_pid.get(pid)

            if _JAVA_HEADER_RE.match(msg) and tag == "AndroidRuntime":
                if cur:
                    close(cur)
                rec = CrashRecord(process="", pid=pid, timestamp=ts, kind="java",
                                  exception="", message="")
                cur = open_by_pid[pid] = _Open(rec, "java-process")
                cur.lines.append(line)
                continue
            if _NATIVE_HEADER_RE.match(msg) and tag == "DEBUG":
                if cur:
                    close(cur)
                rec = CrashRecord(process="", pid=None, timestamp=ts, kind="native",
                                  exception="", message="")
                cur = open_by_pid[pid] = _Open(rec, "native")
                cur.lines.append(line)
                continue
            am = _ANR_HEADER_RE.match(msg)
            if am and tag == "ActivityManager":
                if cur:
                    close(cur)
                rec = CrashRecord(process=am.group("proc"), pid=None, timestamp=ts,
                                  kind="anr", exception="ANR",
                                  message=(am.group("component") or "").strip())
                cur = open_by_pid[pid] = _Open(rec, "anr")
                cur.lines.append(line)
                continue

            if cur is None:
                continue
            rec = cur.rec

            if rec.kind == "java":
                if tag != "AndroidRuntime":
                    continue
                cur.lines.append(line)
                if cur.stage == "java-process":
                    pm = _JAVA_PROCESS_RE.match(msg.strip())
                    if pm:
                        rec.process = pm.group("proc") or ""
                        rec.pid = int(pm.group("pid"))
                        cur.stage = "java-exception"
                        continue
                    cur.stage = "java-exception"   # no Process/PID line; fall through
                if cur.stage == "java-exception":
                    if not msg.strip():
                        continue
                    em = _JAVA_EXC_RE.match(msg.strip())
                    if em:
                        rec.exception = em.group("cls")
                        rec.message = (em.group("msg") or "").strip()
                    else:
                        head, _, tail = msg.strip().partition(":")
                        rec.exception = head.strip()
                        rec.message = tail.strip()
                    cur.stage = "java-frames"
                    continue
                if cur.stage == "java-frames":
                    if _JAVA_FRAME_RE.match(msg):
                        rec.frames.append(msg.strip())
                    elif _JAVA_CONT_RE.match(msg):
                        continue
                    else:
                        cur.stage = "java-tail"     # blank line / "Couldn't report crash"
                continue

            if rec.kind == "native":
                if tag != "DEBUG":
                    continue
                cur.lines.append(line)
                m = _NATIVE_PID_RE.match(msg)
                if m:
                    rec.pid = int(m.group("pid"))
                    rec.process = m.group("proc")
                    continue
                m = _NATIVE_SIGNAL_RE.match(msg)
                if m and not rec.exception:
                    rec.exception = m.group("name")
                    if not rec.message:
                        rec.message = msg.strip()
                    continue
                m = _NATIVE_ABORT_RE.match(msg)
                if m:
                    rec.message = m.group("msg").strip().splitlines()[0] if m.group("msg").strip() else rec.message
                    continue
                if _NATIVE_FRAME_RE.match(msg):
                    rec.frames.append(msg.strip())
                continue

            if rec.kind == "anr":
                if tag != "ActivityManager":
                    continue
                cur.lines.append(line)
                m = _ANR_PID_RE.match(msg)
                if m:
                    rec.pid = int(m.group("pid"))
                    continue
                m = _ANR_REASON_RE.match(msg)
                if m:
                    rec.message = m.group("reason").strip()
                continue
        except Exception:  # a parser must never take the caller down
            logger.debug("crash parser skipped line: %r", line, exc_info=True)
            continue

    for o in open_by_pid.values():
        close(o)
    done.sort(key=lambda r: r.timestamp)
    return [r for r in done if r.kind != "java" or r.exception]


# ---------------------------------------------------------------- classification

def _owned_by(process: str, package: str) -> bool:
    """True for the package's main process and its `:name` sub-processes only.
    `org.foo` does not own `org.foobar`; an empty process name owns nothing."""
    return bool(process) and bool(package) and (process == package or process.startswith(package + ":"))


def classify(record: CrashRecord, package: str) -> str:
    """-> "app" | "app-native" | "foreign". Only the app's own processes are charged."""
    if not _owned_by(record.process, package):
        return "foreign"
    return "app-native" if record.kind == "native" else "app"


# ---------------------------------------------------------------- signatures

_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_OBJ_HASH_RE = re.compile(r"@[0-9a-fA-F]{4,}\b")
_JAVA_LINE_RE = re.compile(r":\d+\)")                       # (File.java:123) -> (File.java)
_JAVA_UNKNOWN_RE = re.compile(r"\(Unknown Source(?::\d+)?\)")
_SYNTH_LAMBDA_RE = re.compile(r"\$\$(?:ExternalSyntheticLambda|Lambda\$?)[\w$/]*")
_R8_LAMBDA_RE = re.compile(r"\$r8\$lambda\$[\w$]+")
_NATIVE_PC_RE = re.compile(r"^#\d+\s+pc\s+[0-9a-fA-F]+\s+")
_NATIVE_BUILDID_RE = re.compile(r"\s*\(BuildId: [^)]*\)")
_NATIVE_OFFSET_RE = re.compile(r"\s*\(offset 0x[0-9a-fA-F]+\)")
_NATIVE_SYMOFF_RE = re.compile(r"\+\d+\)")                   # (abort+156) -> (abort)
_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b")
_PID_TEXT_RE = re.compile(r"\b(?:pid|tid|PID|TID)[:= ]\s*\d+")
_DIGITS_RE = re.compile(r"\d+")


def _norm_java_frame(frame: str) -> str:
    f = frame.strip()
    f = f[3:].strip() if f.startswith("at ") else f
    f = _JAVA_UNKNOWN_RE.sub("(Unknown Source)", f)
    f = _JAVA_LINE_RE.sub(")", f)
    f = _SYNTH_LAMBDA_RE.sub("$$Lambda", f)
    f = _R8_LAMBDA_RE.sub("$r8$lambda", f)
    f = _OBJ_HASH_RE.sub("", f)
    return f


def _norm_native_frame(frame: str) -> str:
    f = _NATIVE_PC_RE.sub("", frame.strip())
    f = _NATIVE_BUILDID_RE.sub("", f)
    f = _NATIVE_OFFSET_RE.sub("", f)
    f = _NATIVE_SYMOFF_RE.sub(")", f)
    f = _HEX_ADDR_RE.sub("0x", f)
    # keep the library basename (incl. "Foo.apk!libbar.so"), drop the directory
    path, _, rest = f.partition(" ")
    return f"{path.rsplit('/', 1)[-1]} {rest}".strip()


def _norm_message(msg: str) -> str:
    m = _OBJ_HASH_RE.sub("", msg)
    m = _HEX_ADDR_RE.sub("0x", m)
    m = _PID_TEXT_RE.sub("", m)
    m = _LONG_HEX_RE.sub("", m)
    m = _DIGITS_RE.sub("#", m)
    return " ".join(m.split())[:120]


def _frame_in_package(raw: str, norm: str, package: str, kind: str) -> bool:
    if not package:
        return False
    if kind == "native":
        return package in raw        # checked on the full path: /data/app/~~x/<pkg>-y/base.apk!lib.so
    return norm.startswith((package + ".", package + "$"))


def signature(record: CrashRecord, package: str, depth: int = 3) -> str:
    """Normalised stack signature for crash dedup (Themis-style).

    `<exception>@<frame> > <frame> > <frame>` where the frames are the first `depth`
    frames that belong to `package` (class name starts with `package.` for java; the
    library path contains `package` for native), falling back to the first `depth`
    frames of the stack when none do. If the record carries no frames at all (ANR,
    truncated block) the normalised message is used instead: `<exception>@msg:<...>`.

    Normalisation (so two occurrences of one bug agree and different bugs differ):
      java   - `(File.java:123)` -> `(File.java)`; `(Unknown Source:0)` -> `(Unknown Source)`;
               `$$ExternalSyntheticLambda7` / `$$Lambda$3` -> `$$Lambda`;
               `$r8$lambda$<hash>` -> `$r8$lambda`; object hashes `@1a2b3c` dropped.
      native - `#NN pc <addr>` prefix, `(BuildId: ...)`, `(offset 0x...)` and symbol
               offsets `+156` dropped; library directory dropped (basename kept);
               hex addresses -> `0x`.
      message- object hashes, hex, `pid N`/`tid N` and long hex runs dropped, every
               digit run -> `#`, whitespace collapsed, capped at 120 chars.
    PIDs and timestamps are never part of the signature.
    """
    depth = max(1, int(depth or 1))
    norm = _norm_native_frame if record.kind == "native" else _norm_java_frame
    pairs = [(f, norm(f)) for f in record.frames]
    pairs = [(raw, n) for raw, n in pairs if n]
    exc = record.exception or record.kind
    if not pairs:
        return f"{exc}@msg:{_norm_message(record.message)}"
    own = [n for raw, n in pairs if _frame_in_package(raw, n, package, record.kind)]
    chosen = (own or [n for _, n in pairs])[:depth]
    return f"{exc}@{' > '.join(chosen)}"


# ---------------------------------------------------------------- exit-info

_EI_TS_RE = re.compile(r"^\s*timestamp=(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+pid=(?P<pid>\d+)")
_EI_PROC_RE = re.compile(
    r"^\s*process=(?P<proc>\S+)\s+reason=(?P<code>\d+)(?:\s+\((?P<label>[^)]*\)?)\))?"
    r"(?:\s+subreason=(?P<sub>\d+)(?:\s+\((?P<sublabel>[^)]*)\))?)?"
)
_EI_DESC_RE = re.compile(r"description=(?P<desc>.*?)(?:\s+state=.*)?$")


def _reason_name(code: int, label: str | None) -> str:
    if code in _REASON_NAMES:
        return _REASON_NAMES[code]
    lab = (label or "").strip().upper().replace("APP CRASH(EXCEPTION)", "CRASH").replace("APP CRASH(NATIVE)", "CRASH_NATIVE")
    lab = re.sub(r"[^A-Z0-9]+", "_", lab).strip("_")
    return lab or "UNKNOWN"


def _sub_reason_name(code: int | None, label: str | None) -> str | None:
    if code is None:
        return None
    if code in _SUBREASON_NAMES:
        return _SUBREASON_NAMES[code]
    lab = re.sub(r"[^A-Z0-9]+", "_", (label or "").strip().upper()).strip("_")
    return lab or None


def parse_exit_info(text: str, package: str) -> list[ExitInfo]:
    """Parse `adb shell dumpsys activity exit-info <package>` (or the unfiltered dump).

    Only records whose `process=` is `package` or `package:<sub>` are returned, newest
    first as the dump prints them. `reason` is normalised from the numeric code (the
    printed labels are "APP CRASH(NATIVE)", "USER REQUESTED", ... and not stable
    across releases). Callers must gate on `FATAL_REASONS` — our harness's own
    force-stops and reinstalls leave USER_REQUESTED/FORCE_STOP, SIGNALED and
    PACKAGE_UPDATED records on every pass. Never raises.
    """
    out: list[ExitInfo] = []
    ts: str = ""
    pid: int | None = None
    pending: ExitInfo | None = None
    for line in (text or "").splitlines():
        try:
            m = _EI_TS_RE.match(line)
            if m:
                if pending is not None:
                    out.append(pending)
                    pending = None
                ts, pid = m.group("ts"), int(m.group("pid"))
                continue
            m = _EI_PROC_RE.match(line)
            if m:
                if pending is not None:
                    out.append(pending)
                    pending = None
                proc = m.group("proc")
                if not _owned_by(proc, package):
                    ts, pid = "", None
                    continue
                code = int(m.group("code"))
                sub = int(m.group("sub")) if m.group("sub") is not None else None
                pending = ExitInfo(pid=pid, reason=_reason_name(code, m.group("label")),
                                   timestamp=ts, description="",
                                   sub_reason=_sub_reason_name(sub, m.group("sublabel")),
                                   process=proc, reason_code=code)
                ts, pid = "", None
                continue
            if pending is not None and "description=" in line:
                d = _EI_DESC_RE.search(line.strip())
                desc = (d.group("desc") if d else "").strip()
                pending.description = "" if desc == "null" else desc
                out.append(pending)
                pending = None
        except Exception:
            logger.debug("exit-info parser skipped line: %r", line, exc_info=True)
            pending = None
            continue
    if pending is not None:
        out.append(pending)
    return out


# ---------------------------------------------------------------- events buffer

_EVENT_RE = re.compile(r"^(?:\d{4}-)?(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+\d+\s+\d+\s+[VDIWEF]\s+(?P<tag>am_crash|am_anr)\s*:\s*\[(?P<body>.*)\]\s*$")


def parse_event_log(text: str) -> list[EventRecord]:
    """Parse `am_crash` / `am_anr` lines from `adb logcat -d -b events` (corroboration
    only: no stack, and the events ring rolls over quickly).

    AOSP EventLogTags:  am_crash: [pid,uid,process,flags,exception,message,file,line]
                        am_anr:   [uid,pid,process,flags,reason]
    """
    out: list[EventRecord] = []
    for line in (text or "").splitlines():
        m = _EVENT_RE.match(line.rstrip("\r"))
        if not m:
            continue
        parts = [p.strip() for p in m.group("body").split(",", 4)]   # 5th field may hold commas
        try:
            if m.group("tag") == "am_crash" and len(parts) >= 5:
                out.append(EventRecord("crash", m.group("ts"), int(parts[0]), parts[2], parts[4].split(",", 1)[0].strip()))
            elif m.group("tag") == "am_anr" and len(parts) >= 5:
                out.append(EventRecord("anr", m.group("ts"), int(parts[1]), parts[2], parts[4]))
        except (ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------- time windows

def _short_ts(ts: str) -> str:
    """'2026-09-09 00:10:36.610' or '09-09 00:10:36.610' -> 'MM-DD hh:mm:ss.mmm'."""
    ts = (ts or "").strip()
    return ts[5:] if re.match(r"^\d{4}-", ts) else ts


def is_after(ts: str, since: str) -> bool:
    """True when logcat/exit-info timestamp `ts` is at or after `since`. Both are
    compared as zero-padded `MM-DD hh:mm:ss.mmm` text, so a year boundary between the
    two is the one case this gets wrong; `since` empty means "everything"."""
    if not since:
        return True
    return _short_ts(ts) >= _short_ts(since)


# ---------------------------------------------------------------- smoke verdict

def smoke_verdict(crash_text: str, exit_info_text: str, pkg: str, since: str) -> tuple[bool, str]:
    """Pure decision for the build smoke gate: did `pkg` itself die after `since`?

    Returns (ok, report). `report` is one or more lines: on failure, the class of the
    first app crash with its signature (or the fatal exit-info record when only that
    signal fired); always, a `N foreign crash(es) ignored: ...` line when other
    processes crashed in the window so the operator still sees them.
    """
    records = [r for r in parse_crash_buffer(crash_text) if is_after(r.timestamp, since)]
    app = [r for r in records if classify(r, pkg) != "foreign"]
    foreign = [r for r in records if classify(r, pkg) == "foreign"]
    fatal_exits = [e for e in parse_exit_info(exit_info_text, pkg)
                   if e.fatal and is_after(e.timestamp, since)]

    lines: list[str] = []
    ok = True
    if app:
        r = app[0]
        ok = False
        head = f"{r.exception}: {r.message}" if r.message else r.exception
        lines.append(f"✗ {classify(r, pkg)} crash in {r.process} (pid {r.pid}) at {r.timestamp}: {head[:160]}")
        lines.append(f"  signature: {signature(r, pkg)}")
        if len(app) > 1:
            lines.append(f"  (+{len(app) - 1} more app crash(es) in window)")
    if fatal_exits:
        e = fatal_exits[0]
        if ok:
            ok = False
            lines.append(f"✗ exit-info: {e.process} pid {e.pid} died with {e.reason} at {e.timestamp}"
                         f"{': ' + e.description if e.description else ''}")
        else:
            lines.append(f"  exit-info corroborates: {e.reason} pid {e.pid} at {e.timestamp}")
    if foreign:
        names = sorted({r.process for r in foreign if r.process})
        unnamed = sum(1 for r in foreign if not r.process)
        if unnamed:   # shell-spawned java commands (uiautomator dump ...) log no Process: line
            names.append(f"{unnamed} unnamed shell process(es)")
        lines.append(f"{len(foreign)} foreign crash(es) ignored: {', '.join(names)}")
    return ok, "\n".join(lines)


# ---------------------------------------------------------------- thin adb wrappers
# Read-only. No `logcat -c`: the buffer is shared with other harnesses on the device.

async def _adb_text(serial: str | None, *args: str, timeout: float = 30.0) -> str:
    argv = [_adb_bin(), *(["-s", serial] if serial else []), *args]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, OSError) as e:
        logger.warning("adb %s failed: %s", " ".join(args[:4]), e)
        if proc is not None and proc.returncode is None:
            proc.kill()
        return ""
    return out.decode("utf-8", "replace")


_DEVICE_TS_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")
_DEVICE_DATE_FMT = "+%m-%d %H:%M:%S.000"


async def device_time(serial: str | None) -> str:
    """Device wall clock as `MM-DD hh:mm:ss.mmm` — the form `logcat -T` accepts and
    the crash buffer prints (verified on emulator-5554; toybox date has no %N, so the
    millis are always .000 — take it BEFORE the launch you want to observe).

    The format is ONE shell-quoted word: `adb shell` joins its argv with spaces and
    re-parses on the device, so an unquoted `+%m-%d %H:%M:%S.000` reaches toybox as
    two arguments and every window opened from the reply (`date: Max 1 argument`)
    silently disabled the crash check (2026-09-14)."""
    out = (await _adb_text(serial, "shell", f"date {shlex.quote(_DEVICE_DATE_FMT)}")).strip()
    if not _DEVICE_TS_RE.match(out):
        logger.warning("unexpected device date output: %r", out)
    return out


async def crashes_since(serial: str | None, package: str, since: str,
                        include_foreign: bool = False) -> list[CrashRecord]:
    """Crash-buffer records logged at or after `since` (a `device_time()` string).
    By default only records attributed to `package` (app / app-native); pass
    `include_foreign=True` to also get other processes' crashes for reporting."""
    args = ["logcat", "-d", "-b", "crash", "-v", "threadtime"]
    if since:
        args += ["-T", since]
    text = await _adb_text(serial, *args)
    recs = [r for r in parse_crash_buffer(text) if is_after(r.timestamp, since)]
    if include_foreign:
        return recs
    return [r for r in recs if classify(r, package) != "foreign"]


async def exit_info(serial: str | None, package: str) -> list[ExitInfo]:
    text = await _adb_text(serial, "shell", "dumpsys", "activity", "exit-info", package)
    return parse_exit_info(text, package)


async def app_crashed_since(serial: str | None, package: str, since: str) -> CrashRecord | None:
    """First crash of `package` (app or app-native) since `since`, else None.

    Combines both signals: the crash buffer first (full stack, signature-able); then
    exit-info, so an ANR or a native death that never reached the crash buffer still
    counts — such a record is returned as a synthesised CrashRecord (kind "anr" or
    "native"/"java" by reason, no frames, raw = the exit-info summary). Foreign
    crashes never produce a result.
    """
    recs = await crashes_since(serial, package, since)
    if recs:
        return recs[0]
    for e in await exit_info(serial, package):
        if e.fatal and is_after(e.timestamp, since):
            kind = {"ANR": "anr", "CRASH_NATIVE": "native"}.get(e.reason, "java")
            return CrashRecord(process=e.process, pid=e.pid, timestamp=_short_ts(e.timestamp),
                               kind=kind, exception=e.reason, message=e.description,
                               frames=[], raw=f"exit-info: {e.process} pid={e.pid} reason={e.reason} "
                                              f"sub={e.sub_reason} at {e.timestamp} {e.description}")
    return None
