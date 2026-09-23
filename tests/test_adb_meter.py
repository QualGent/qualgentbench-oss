"""ADB socket meter — charges device operations even when adb calls hide in scripts.

Protocol tests run against a fake ADB server; the device-backed tests skip
without an emulator.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from qualgentbench.adb_meter import AdbMeter, classify, deny_reason, read_counts
from qualgentbench.interactions import InteractionLog


def _adb_shell_requests(transcript_line: str):
    """The adb shell/exec service requests an agent's host command would send, parsed
    out of one transcript JSONL line (claude-code `Bash` tool_use and codex
    `command_execution`). Best-effort and conservative — used only to replay saved
    episodes against the meter's deny rules, never in production."""
    import json
    import re
    import shlex

    try:
        ev = json.loads(transcript_line)
    except (json.JSONDecodeError, TypeError):
        return
    host_cmds: list[str] = []
    if ev.get("type") == "assistant":
        for b in ev.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                c = (b.get("input") or {}).get("command")
                if c:
                    host_cmds.append(c)
    item = ev.get("item")
    if ev.get("type") == "item.completed" and isinstance(item, dict) \
            and item.get("type") == "command_execution":
        c = item.get("command", "")
        m = re.match(r"^/bin/(?:zsh|bash|sh) -lc (.*)$", c, re.S)
        if m:
            try:
                c = shlex.split(m.group(1))[0]
            except ValueError:
                pass
        host_cmds.append(c)

    op = re.compile(r"&&|\|\||[;\n|&()]")
    for cmd in host_cmds:
        if "adb" not in cmd:
            continue
        for seg in op.split(cmd):
            try:
                words = shlex.split(seg, posix=True)
            except ValueError:
                continue
            if "adb" not in words:
                continue
            rest = words[words.index("adb") + 1:]
            j = 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in ("-s", "-t", "-H", "-P", "-L") else 1
            if j >= len(rest):
                continue
            sub, args = rest[j], rest[j + 1:]
            if sub == "shell":
                k = 0
                while k < len(args) and args[k] in ("-T", "-t", "-tt", "-x", "-n", "-e"):
                    k += 1
                yield "shell,v2,raw:" + " ".join(args[k:])
            elif sub in ("exec-out", "exec-in"):
                yield "exec:" + " ".join(args)


@pytest.fixture
def attached_device() -> str:
    """Serial of the first ready adb device; skips when there is none.

    Only a `live_device` test may request this: anywhere else the suite's device
    guard (tests/conftest.py) refuses the probe. Until 2026-09 this module ran
    `adb devices` at IMPORT time and, whenever an emulator was attached, the
    end-to-end tests below tapped it -- once in the middle of a live benchmark
    episode, which then failed its precondition.
    """
    if not shutil.which("adb"):
        pytest.skip("adb is not on PATH")
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("`adb devices` failed")
    ready = [line.split()[0] for line in out.splitlines()[1:]
             if line.strip().endswith("\tdevice")]
    if not ready:
        pytest.skip("no adb device attached")
    # ANDROID_SERIAL picks one when several are attached (never a device another run
    # is using); conftest strips only QGB_*, so it reaches the test.
    wanted = os.environ.get("ANDROID_SERIAL")
    if wanted:
        if wanted not in ready:
            pytest.skip(f"ANDROID_SERIAL={wanted} is not attached")
        return wanted
    return ready[0]


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("request_,kind", [
    ("host:version", "plumbing"),
    ("host:devices", "plumbing"),
    ("host:tport:serial:emulator-5554", "plumbing"),
    ("host-serial:emulator-5554:features", "plumbing"),
    ("shell:input tap 500 900", "action"),
    ("shell,v2,TERM=xterm,raw:input swipe 1 2 3 4", "action"),
    ("shell:am start -n com.x/.Main", "action"),
    ("shell:pm clear com.x", "action"),
    ("shell:uiautomator dump /sdcard/w.xml", "observation"),
    ("shell:dumpsys window", "observation"),
    ("exec:screencap -p", "observation"),
    ("sync:", "observation"),
])
def test_requests_are_classified(request_, kind):
    assert classify(request_) == kind


def test_plumbing_is_not_charged():
    """`host:*` is connection setup — charging it would make budgets depend on
    the client's internals."""
    m = AdbMeter(counter_path="/dev/null")
    for r in ("host:version", "host:devices", "host:tport:serial:emulator-5554"):
        m._record(r)
    assert m.counts.total == 0


# ── protocol, against a fake ADB server ──────────────────────────────────────

class _FakeAdbServer:
    """Speaks just enough of the host protocol to exercise the meter's parser."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, r, w):
        try:
            while True:
                prefix = await r.readexactly(4)
                body = await r.readexactly(int(prefix.decode(), 16))
                req = body.decode()
                self.requests.append(req)
                w.write(b"OKAY")
                if req.startswith("host:tport"):
                    w.write((1).to_bytes(8, "little"))   # transport id
                    await w.drain()
                    continue
                if req.startswith("host:"):
                    payload = b"ok"
                    w.write(b"%04x" % len(payload) + payload)
                await w.drain()
                if not req.startswith("host:tport"):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


async def _send(port: int, *requests: str) -> None:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    for req in requests:
        w.write(f"{len(req):04x}{req}".encode())
        await w.drain()
        await r.readexactly(4)                     # OKAY
        if req.startswith("host:tport"):
            await r.readexactly(8)                 # transport id
    w.close()


@pytest.mark.asyncio
async def test_tport_then_service_is_counted(tmp_path):
    """Modern adb selects devices with `host:tport:`, not `host:transport:` —
    treating tport as an opaque handover left every following request unparsed."""
    upstream = _FakeAdbServer()
    up_port = await upstream.start()
    meter = AdbMeter(tmp_path / "c.json", upstream_port=up_port)
    port = await meter.start()

    await _send(port, "host:tport:serial:emulator-5554", "shell:input tap 5 9")
    await asyncio.sleep(0.05)

    assert meter.counts.total == 1 and meter.counts.actions == 1
    assert "shell:input tap 5 9" in upstream.requests   # still relayed upstream
    await meter.stop()
    await upstream.stop()


@pytest.mark.asyncio
async def test_counts_are_flushed_for_the_hook_to_read(tmp_path):
    """The budget hook is a different process reading this file on every tool call."""
    upstream = _FakeAdbServer()
    meter = AdbMeter(tmp_path / "c.json", upstream_port=await upstream.start())
    port = await meter.start()
    await _send(port, "host:tport:serial:x", "shell:uiautomator dump /sdcard/w.xml")
    await asyncio.sleep(0.05)

    counts = read_counts(tmp_path / "c.json")
    assert counts["metered_total"] == 1
    assert counts["metered_observations"] == 1
    assert counts["metered_actions"] == 0
    await meter.stop()
    await upstream.stop()


# ── `su` is refused at the meter (QUA-2790) ─────────────────────────────────────
# The google_apis images ship /system/xbin/su: `adb shell su 0 …` is a root shell with
# adbd unrooted, and a root shell reaches the answer key without any literal path the
# other rules match. Every request shape the meter sees, in both arms.

@pytest.mark.parametrize("request_", [
    "shell:su 0 id",
    "exec:su 0 id",
    "shell,v2,TERM=xterm-256color,raw:su 0 id",
    "shell,v2,raw:su 0 sh -c 'cd /data/da*/com.x/f*; cat q*'",
    # the QUA-2785 command, verbatim
    ("shell,v2,TERM=xterm-256color,raw:su 0 sh -c 'f=$(ls -t /data/anr/anr_* | head -1); "
     "head -50 $f'"),
    "shell:su",
    "shell:SU 0 id",
    "shell:su -c id",
    "shell:su root id",
    "shell:cd /sdcard; su 0 id",
    "shell:true && su 0 id",
    "shell:false || su 0 id",
    "shell:echo id | su",
    "shell:echo id|su 0",
    "shell:(su 0 id)",
    "shell:x=$(su 0 id)",
    "shell:x=`su 0 id`",
    "shell:sh -c 'su 0 id'",
    'shell:sh -c "su 0 ls /sdcard"',
    "shell:'su' 0 id",
    'shell:"su" 0 id',
    "shell:s\\u 0 id",
    "shell:/system/xbin/su 0 id",
    "shell:exec su 0 id",
    "shell:env X=1 su 0 id",
    "shell:x=su; $x 0 id",
    "shell:nohup su 0 id &",
])
def test_su_is_denied_in_every_shape(request_):
    assert deny_reason(request_) == "su", request_


@pytest.mark.parametrize("request_", [
    "shell:dumpsys activity activities",
    "shell:dumpsys dropbox",
    "shell:cat /sdcard/summary.txt",
    "shell:ls /sdcard/results",
    "shell:ls /sdcard/Download/results/sub",
    "shell:am start -n com.example.super/.Main",
    "shell:am start -n com.example.su/.Main",
    "shell:pm list packages com.su.app",
    "shell:cat /sdcard/su.txt",
    "shell:ls /sdcard/su/notes",
    "shell:input text sunday",
    "shell:input text Susan",
    "shell:settings get secure user_setup_complete",
    "shell:logcat -d -s SurfaceFlinger",
    "shell:uiautomator dump /sdcard/window_dump.xml",
    "shell:input keyevent KEYCODE_SEARCH",
    "shell:am force-stop org.consumer.app",
    "shell,v2,TERM=xterm-256color,raw:screencap -p /sdcard/issue-sub.png",
    "host:tport:serial:emulator-5554",
    "sync:",
])
def test_words_that_contain_su_are_not_denied(request_):
    assert deny_reason(request_) is None, request_


# ── stdin / pushed-script bypasses are denied (QUA-2794) ─────────────────────────
# The rules above read the request TEXT. A command delivered on the adb STREAM (an
# empty-command shell) or read from a pushed FILE never shows its payload in the
# request line, so `echo 'su 0 id' | adb shell`, `adb shell sh < x.sh` and
# `adb push x.sh … && adb shell sh /sdcard/x.sh` used to slip past every rule. These
# close all three at the meter, in both arms; a denied one is counted in metered_denied.

@pytest.mark.parametrize("request_,why", [
    # an interactive / empty-command shell: what `echo … | adb shell` and bare
    # `adb shell` open — the payload rides on the stream the meter cannot read.
    ("shell:", "stdin shell"),
    ("shell,v2,TERM=xterm-256color,raw:", "stdin shell"),
    ("shell,v2,raw:", "stdin shell"),
    ("shell:   ", "stdin shell"),
    ("exec:", "stdin shell"),
    # a shell interpreter reading its script from stdin or a file
    ("shell:sh", "script shell"),
    ("shell:bash", "script shell"),
    ("shell:sh -s", "script shell"),
    ("shell:sh -", "script shell"),
    ("shell:sh -es", "script shell"),
    ("shell,v2,TERM=xterm,raw:sh", "script shell"),
    ("shell:sh /sdcard/x.sh", "script shell"),
    ("shell:sh /data/local/tmp/x.sh", "script shell"),
    ("shell:/system/bin/sh /sdcard/run", "script shell"),
    ("shell:toybox sh /sdcard/x.sh", "script shell"),
    ("shell:busybox sh script", "script shell"),
    ("shell:source /sdcard/x", "script shell"),
    ("shell:. /sdcard/x", "script shell"),
    ("shell:xargs sh", "script shell"),
    ("shell:cat /sdcard/x.sh | sh", "script shell"),
    ("shell:cd /sdcard && sh x.sh", "script shell"),
    # `sh -c '<text>'` is allowed, but its text is scanned like any command: a shell
    # or a pushed file nested inside it is still denied (the ticket's exception).
    ("shell:sh -c 'sh /sdcard/x'", "script shell"),
    ("shell:sh -c '/data/local/tmp/x'", "world-writable exec"),
    ("shell:sh -c 'run-as com.x cat files/qgb_flags.txt'", "run-as"),
    ("shell:sh -c 'su 0 id'", "su"),
    # executing a file the agent pushed or wrote, by path
    ("shell:/sdcard/x", "world-writable exec"),
    ("shell:/data/local/tmp/payload", "world-writable exec"),
    ("shell:/storage/emulated/0/x.sh", "world-writable exec"),
    ("shell:/mnt/sdcard/x", "world-writable exec"),
    ("shell:chmod 755 /sdcard/x; /sdcard/x", "world-writable exec"),
    ("shell,v2,raw:/sdcard/run.sh arg1", "world-writable exec"),
])
def test_hidden_payload_paths_are_denied(request_, why):
    assert deny_reason(request_) == why, request_


@pytest.mark.parametrize("request_", [
    # `sh -c '<inline command>'` — the command is visible and clean
    "shell:sh -c 'input tap 1 2'",
    "shell:sh -c id",
    'shell:sh -c "am start -n com.x/.Main"',
    "shell:sh -c 'input swipe 1 2 3 4 && uiautomator dump /sdcard/w.xml'",
    # reading or writing a world-writable path (not executing one) is fine
    "shell:uiautomator dump /sdcard/window_dump.xml",
    "shell:cat /sdcard/window.xml",
    "shell:screencap -p /sdcard/x.png",
    "shell:ls /sdcard/results",
    "shell:rm /sdcard/w.xml",
    "shell:cp /sdcard/a /sdcard/b",
    "shell:cat /sdcard/x.sh",           # reading a script, not running it
    # `sh` as data to another command, not as the interpreter
    "shell:grep sh /sdcard/log.txt",
    "shell:find / -name sh",
    "shell:input text sunday",
    # ordinary device work
    "shell:input tap 1 1",
    "shell:am start -n com.x/.Main",
    "shell:monkey -p com.x -c android.intent.category.LAUNCHER 1",
    "shell:sed 's/></>/g' /sdcard/window.xml | head -120",
    "shell:ls -t /data/anr/ | head -1",
    "exec:screencap -p",
    "host:tport:serial:emulator-5554",
    "sync:",
])
def test_ordinary_and_inline_shell_requests_are_not_denied(request_):
    assert deny_reason(request_) is None, request_


def test_no_saved_agent_adb_request_is_a_new_false_positive():
    """The 34 raw-arm and the MCP-arm saved episodes drove real adb; not one legitimate
    request the agents made trips the new stdin/pushed-script rules. Runs only where the
    saved runs are present (the owner's machine); skips in CI and a fresh clone."""
    import json
    from pathlib import Path

    from qualgentbench.adb_meter import _DENY_RULES

    roots = [Path.home() / ".qualgentbench" / "runs",
             Path("/Users/gyaan/Work/qualgentbench-oss/runs")]
    transcripts = [t for r in roots for t in r.glob("*/*/agent/transcript.txt")]
    if not transcripts:
        pytest.skip("no saved episodes on this machine")

    def old_deny(request: str) -> str | None:
        low = request.strip().lower().replace("'", "").replace('"', "").replace("\\", "")
        for name, rx in _DENY_RULES:
            if rx.search(low):
                return name
        return None

    new_false_positives = []
    for t in transcripts:
        for line in t.read_text(errors="replace").splitlines():
            for req in _adb_shell_requests(line):
                if deny_reason(req) and not old_deny(req):
                    new_false_positives.append(req)
    assert not new_false_positives, new_false_positives[:20]


def test_the_harness_clear_would_be_denied_so_it_never_uses_the_meter():
    """`episode_runner.clear_crash_history` runs `su 0 sh -c 'rm -rf /data/anr/*'`. It
    works only because harness adb goes straight to the upstream server; the meter port
    is set in `agent_env` alone. Pinned so nobody routes staging through the meter."""
    import shlex

    from qualgentbench import episode_runner as er

    assert deny_reason("shell:su 0 sh -c " + shlex.quote(f"rm -rf {er.ANR_DIR}/*")) == "su"
    assert deny_reason("shell:" + er.ANR_LIST_CMD) is None     # the invariant's read


@pytest.mark.asyncio
async def test_a_denied_su_is_answered_fail_and_counted_in_both_ledgers(tmp_path):
    """At the socket: FAIL, never relayed, `metered_denied` 1 and `metered_total` 0 —
    and the interaction log (the budget) charges it exactly what it would cost relayed,
    one `other`, so a refused probe is never cheaper than a run one."""
    upstream = _FakeAdbServer()
    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", upstream_port=await upstream.start(), log=log)
    port = await meter.start()

    r, w = await asyncio.open_connection("127.0.0.1", port)
    for req in ("host:tport:serial:emulator-5554",
                "shell,v2,TERM=xterm-256color,raw:su 0 id"):
        w.write(f"{len(req):04x}{req}".encode())
        await w.drain()
    assert await r.readexactly(4) == b"OKAY"
    await r.readexactly(8)                                  # transport id
    assert await r.readexactly(4) == b"FAIL"
    msg = await r.readexactly(int((await r.readexactly(4)).decode(), 16))
    assert b"su is not available to the agent" in msg
    w.close()
    await asyncio.sleep(0.05)

    counts = read_counts(tmp_path / "c.json")
    assert (counts["metered_denied"], counts["metered_total"]) == (1, 0)
    assert not any("su 0" in q for q in upstream.requests)  # never reached the server
    assert (log.total, log.counts["other"]) == (1, 1)
    await meter.stop()
    await upstream.stop()


# ── the real thing ───────────────────────────────────────────────────────────
# Opt-in only. `live_device` lifts the device guard for the test and is skipped unless
# QGB_LIVE_DEVICE=1 -- these TAP the device, so never run them beside a benchmark.

@pytest.mark.live_device
@pytest.mark.asyncio
async def test_a_wrapped_script_is_charged_for_every_operation(tmp_path, attached_device):
    """End to end: adb calls wrapped in a script are still charged per operation."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import subprocess\n"
        "def adb(c):\n"
        f"    subprocess.run(f'adb -s {attached_device} {{c}}', shell=True, capture_output=True)\n"
        "for i in range(5):\n"
        "    adb(f'shell input tap {300+i} {600+i}')\n"
        "adb('shell uiautomator dump /sdcard/qgb_meter.xml')\n"
    )
    meter = AdbMeter(tmp_path / "c.json")
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port))

    proc = await asyncio.create_subprocess_exec(
        "python3", str(driver), env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    counts = (await meter.stop()).as_metrics()

    # One Bash step from the agent's side, six metered device operations.
    assert counts["metered_total"] == 6, counts
    assert counts["metered_actions"] == 5
    assert counts["metered_observations"] == 1


@pytest.mark.live_device
@pytest.mark.asyncio
async def test_direct_adb_still_reaches_the_device_through_the_proxy(tmp_path, attached_device):
    """A meter that broke adb would be worse than no meter."""
    meter = AdbMeter(tmp_path / "c.json")
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port))
    proc = await asyncio.create_subprocess_shell(
        f"adb -s {attached_device} shell echo qgb-roundtrip", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    await meter.stop()
    assert b"qgb-roundtrip" in out


@pytest.mark.live_device
@pytest.mark.asyncio
async def test_an_agents_su_fails_at_the_meter_on_a_real_device(tmp_path, attached_device):
    """The acceptance, with the real adb client (whatever request shape it sends):
    `adb shell su 0 id` through the meter exits non-zero with the meter's reason and
    never prints a root uid, and it is counted as denied. The same command over the
    harness's own adb (no meter) still reaches `su` — which is why the harness's
    privileged staging steps keep working."""
    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", log=log)
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port))
    proc = await asyncio.create_subprocess_exec(
        "adb", "-s", attached_device, "shell", "su", "0", "id", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    counts = (await meter.stop()).as_metrics()

    assert proc.returncode != 0 and b"uid=0" not in out, out
    assert b"su is not available to the agent" in out, out
    assert counts["metered_denied"] == 1 and counts["metered_total"] == 0, counts
    assert log.counts["other"] == 1

    direct = subprocess.run(["adb", "-s", attached_device, "shell", "which su"],
                            capture_output=True, text=True, timeout=30, check=False).stdout
    if direct.strip().startswith("/"):
        harness = subprocess.run(["adb", "-s", attached_device, "shell", "su 0 id -u"],
                                 capture_output=True, text=True, timeout=30,
                                 check=False).stdout
        assert harness.strip() == "0", harness


@pytest.mark.live_device
@pytest.mark.asyncio
async def test_stdin_and_pushed_script_fail_at_the_meter_on_a_real_device(tmp_path, attached_device):
    """The QUA-2794 acceptance, with the real adb client. Both bypasses that reached
    `su` through the meter's text blind spot now fail AT the meter and never root the
    device: `echo 'su 0 id' | adb shell` (an empty-command shell fed on stdin) and a
    pushed script run by path. Denials are counted. Point ANDROID_SERIAL at a spare
    rooted AVD (qgbench_root2:5556) — never emulator-5554, which carries live boards."""
    env = dict(os.environ)

    async def run(*argv, stdin: bytes | None = None):
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", attached_device, *argv, env=env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate(stdin)
        return proc.returncode, out

    # A control OUTSIDE the meter: prove this device can actually root, so a clean
    # `uid=0` below would be a real escape and not just an image without `su`.
    rc, out = await run("shell", "su", "0", "id", "-u")
    if out.strip() != b"0":
        pytest.skip("device has no working su; the bypass has nothing to reach")

    meter = AdbMeter(tmp_path / "c.json")
    port = await meter.start()
    env["ANDROID_ADB_SERVER_PORT"] = str(port)
    env["ANDROID_ADB_SERVER_ADDRESS"] = "127.0.0.1"
    env["ANDROID_ADB_SERVER_HOST"] = "127.0.0.1"

    # 1) stdin-fed empty-command shell: `echo 'su 0 id' | adb shell`
    rc1, out1 = await run("shell", stdin=b"su 0 id\n")
    assert rc1 != 0 and b"uid=0" not in out1, out1
    assert b"stdin shell is not available to the agent" in out1, out1

    # 2) push a script, then run it by path
    script = tmp_path / "x.sh"
    script.write_text("#!/system/bin/sh\nsu 0 id\n")
    rc_push, out_push = await run("push", str(script), "/sdcard/qgb_probe_x.sh")
    assert rc_push == 0, out_push                       # the push itself is allowed
    rc2, out2 = await run("shell", "sh", "/sdcard/qgb_probe_x.sh")
    assert rc2 != 0 and b"uid=0" not in out2, out2
    assert b"script shell is not available to the agent" in out2, out2
    rc3, out3 = await run("shell", "/sdcard/qgb_probe_x.sh")
    assert rc3 != 0 and b"uid=0" not in out3, out3
    assert b"world-writable exec is not available to the agent" in out3, out3

    counts = (await meter.stop()).as_metrics()
    # Three denials; the only relayed op is the `push` itself (one observation) — the
    # push is allowed, running what it dropped is not.
    assert counts["metered_denied"] == 3, counts
    assert counts["metered_total"] == counts["metered_observations"] == 1, counts

    # clean up the probe file over the harness's own adb (no meter)
    subprocess.run(["adb", "-s", attached_device, "shell", "rm", "-f",
                    "/sdcard/qgb_probe_x.sh"], capture_output=True, timeout=30, check=False)


@pytest.mark.asyncio
async def test_stop_severs_connections_an_orphan_holds_open(tmp_path):
    """The real case: the agent backgrounds `adb root`/logcat detached in its own
    session, dies, and the leftover keeps its meter connection streaming forever.
    Python 3.12's wait_closed() waits for every open handler, so teardown once sat
    an hour on one lane. stop() must sever the connections, not join them."""
    hold = asyncio.Event()

    async def upstream_handle(r, w):
        try:
            prefix = await r.readexactly(4)
            await r.readexactly(int(prefix.decode(), 16))
            w.write(b"OKAY")
            await w.drain()
            await hold.wait()                     # the stream never ends on its own
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            w.close()

    upstream = await asyncio.start_server(upstream_handle, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    meter = AdbMeter(tmp_path / "c.json", upstream_port=up_port)
    port = await meter.start()

    # One connection parked mid-stream (opaque service, like logcat)…
    req = b"shell:logcat"
    r1, w1 = await asyncio.open_connection("127.0.0.1", port)
    w1.write(b"%04x" % len(req) + req)
    await w1.drain()
    await r1.readexactly(4)                       # OKAY — an open-ended stream now
    # …and one parked mid-parse (connected, never sent a request).
    _r2, w2 = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.05)

    counts = await asyncio.wait_for(meter.stop(), timeout=5.0)
    assert counts.total >= 1                      # the held stream was still counted

    hold.set()
    for w in (w1, w2):
        w.close()
    upstream.close()
