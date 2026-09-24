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
from adb_replay import FIXTURE_ILLEGITIMATE, adb_requests, fixture_corpus

from qualgentbench.adb_meter import AdbMeter, classify, deny_reason, read_counts
from qualgentbench.interactions import InteractionLog


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


# ── adbd privilege / device-state services are denied (QUA-2795) ──────────────
# `adb root` is not a shell request: after `host:tport:serial:<s>` the client sends the
# bare device service `root:`, which no text rule above read. A root adbd makes every
# later `adb shell` uid 0 — no `su`, no path. Wire shapes measured with adb 36.0.2.

@pytest.mark.parametrize("request_,why", [
    ("root:", "adb root"),
    ("unroot:", "adb unroot"),
    ("reboot:", "adb reboot"),
    ("reboot:bootloader", "adb reboot"),
    ("reboot:recovery", "adb reboot"),
    ("tcpip:5555", "adb tcpip"),
    ("usb:", "adb usb"),
    ("remount:", "adb remount"),
    ("disable-verity:", "adb disable-verity"),
    ("enable-verity:", "adb enable-verity"),
    ("sideload-host:1234:65536", "adb sideload-host"),
    ("ROOT:", "adb root"),
    ("host:kill", "adb kill-server"),
    # remount / verity arrive as shell requests (the client's remount_shell feature)
    ("shell,v2,raw:remount", "adb remount"),
    ("shell,v2,raw:remount -R", "adb remount"),
    ("shell,v2,raw:disable-verity", "adb disable-verity"),
    ("shell,v2,raw:enable-verity", "adb enable-verity"),
    # the same from inside a shell
    ("shell:reboot", "reboot"),
    ("shell:reboot -p", "reboot"),
    ("exec:/system/bin/reboot", "reboot"),
    ("shell:svc power reboot", "reboot"),
    ("shell:svc power shutdown", "reboot"),
    ("shell:input tap 1 2; reboot", "reboot"),
    ("shell:sh -c 'reboot'", "reboot"),
    # adbd comes back ROOT on its next restart once the shell user sets this (measured)
    ("shell:setprop service.adb.root 1", "adbd property"),
    ("shell:setprop persist.adb.tcp.port 5555", "adbd property"),
    ("shell:setprop ctl.restart adbd", "adbd property"),
    ("shell:setprop sys.powerctl reboot", "adbd property"),
    ("shell:setprop sys.usb.config adb", "adbd property"),
    ("shell:sh -c 'setprop service.adb.root 1'", "adbd property"),
])
def test_adbd_privilege_services_are_denied(request_, why):
    assert deny_reason(request_) == why, request_


@pytest.mark.parametrize("request_", [
    # transport selection: `adb -d` sends `host:tport:usb`, which is not `usb:`
    "host:tport:usb",
    "host:tport:any",
    "host:transport-usb",
    "host:tport:serial:emulator-5554",
    "host-serial:emulator-5554:features",
    "host-serial:emulator-5554:get-state",
    "host:version",
    "host:devices",
    "host:devices-l",
    # reads and data that merely NAME a privileged thing
    "shell:getprop service.adb.root",
    "shell:getprop sys.boot_completed",
    "shell:setprop debug.hwui.overdraw show",
    "shell:logcat -d | grep -i reboot",
    "shell:dumpsys activity | grep remount",
    "shell:input text reboot",
    "shell:echo usb:",
    "shell:dumpsys usb",
    "shell:svc power stayon true",
    "shell:ls /sdcard/rooted",
    "sync:",
    "framebuffer:",
])
def test_requests_that_only_name_a_privileged_thing_are_not_denied(request_):
    assert deny_reason(request_) is None, request_


# ── forward / reverse / jdwp / raw device sockets (QUA-2797) ──────────────────
# These reach app or device state on a stream the meter never parses. `adb forward
# tcp:N jdwp:<pid>` is the sharp one: it hands a debugger port straight to the app's
# process on a debuggable build. Wire shapes measured with adb 36.0.2.

@pytest.mark.parametrize("request_,why", [
    ("host:forward:tcp:7001;jdwp:1234", "adb forward"),
    ("host:forward:norebind:tcp:7001;tcp:9008", "adb forward"),
    ("host:killforward:tcp:7001", "adb forward"),
    ("host:killforward-all", "adb forward"),
    ("host:list-forward", "adb forward"),
    ("host:track-devices", "adb forward"),
    ("reverse:forward:tcp:7001;tcp:7002", "adb reverse"),
    ("reverse:list-forward", "adb reverse"),
    ("reverse:killforward:tcp:7001", "adb reverse"),
    ("reverse:killforward-all", "adb reverse"),
    ("jdwp", "adb jdwp"),
    ("jdwp:1234", "adb jdwp"),
    ("track-jdwp", "adb track-jdwp"),
    ("track-app", "adb track-app"),
    # a raw device-socket open — a forward target reached without the `forward` wrapper
    ("localabstract:jdwp-control", "device socket"),
    ("localreserved:foo", "device socket"),
    ("localfilesystem:/data/x", "device socket"),
    ("dev:/dev/socket/x", "device socket"),
    ("tcp:5555", "device socket"),
])
def test_forward_and_socket_services_are_denied(request_, why):
    assert deny_reason(request_) == why, request_


@pytest.mark.parametrize("request_", [
    # transport, version and feature negotiation, device enumeration — all needed
    "host:version",
    "host:features",
    "host:host-features",
    "host:devices",
    "host:devices-l",
    "host:tport:usb",
    "host:tport:serial:emulator-5554",
    "host:transport-usb",
    "host-serial:emulator-5554:features",
    "host-serial:emulator-5554:get-state",
    "host-serial:emulator-5554:wait-for-any-device",
    "host:connect:127.0.0.1:5555",
    "host:disconnect:127.0.0.1:5555",
    # the device services an agent legitimately opens
    "shell:input tap 1 2",
    "shell,v2,raw:id",
    "exec:screencap -p",
    "abb_exec:package install-create",
    "abb:cmd package list",
    "sync:",
    "framebuffer:",
])
def test_transport_reads_and_metered_services_are_not_forward_denied(request_):
    assert deny_reason(request_) is None, request_


# ── QUA-2804: the shell bypasses the QUA-2773 review got past the text rules ──────
# Shell expansion, globbing, command substitution, interpreters/exec forms the earlier
# rules did not name. Each must FAIL at the meter (the nonce backstop covers anything
# still slipping through; see test_contamination.py).

@pytest.mark.parametrize("request_,why", [
    # parameter expansion builds the command word: `a=s; ${a}u 0 id`
    ("shell:a=s; ${a}u 0 id", "dynamic command"),
    ("shell:${x}u 0 id", "dynamic command"),
    ("shell:sh -c 'a=s; ${a}u 0 id'", "dynamic command"),
    # a glob in the command word: `/system/xbin/s?` → su
    ("shell:/system/xbin/s? 0 id", "glob command"),
    ("shell:/system/xbin/s[uv] 0 id", "glob command"),
    ("shell:/sys*/xbin/i? 0 id", "glob command"),
    # command substitution — its OUTPUT is the command, unreadable to the meter
    ("shell:sh -c \"$(cat /sdcard/p.sh)\"", "command substitution"),
    ("shell:eval \"$(cat /sdcard/p.sh)\"", "command substitution"),
    ("shell:x=`cat /sdcard/p.sh`; sh -c $x", "command substitution"),
    ("shell:`cat /sdcard/p.sh`", "command substitution"),
    # interpreters / exec forms the earlier rules did not name
    ("shell:awk -f /sdcard/p.awk", "program interpreter"),
    ("shell:/system/bin/awk -f /sdcard/p.awk", "program interpreter"),
    ("shell:python /sdcard/p.py", "program interpreter"),
    ("shell:find /sdcard -name p.sh -exec sh {} \\;", "find -exec"),
    ("shell:find /sdcard -name p.sh -execdir sh {} +", "find -exec"),
    # a relative command word after a cd into a world-writable dir
    ("shell:cd /sdcard; ./x", "world-writable exec"),
    ("shell:cd /sdcard && ./x 0 id", "world-writable exec"),
    ("shell:../data/local/tmp/x", "world-writable exec"),
])
def test_the_qua_2804_shell_bypasses_are_denied(request_, why):
    assert deny_reason(request_) == why, request_


@pytest.mark.parametrize("request_", [
    # a `$` or glob in an ARGUMENT is ordinary QA — the agent parameterises taps and
    # globs a screen dump; only the command WORD is checked.
    "shell:input tap $1 $2",
    "shell:cat /data/anr/$f",
    "shell:cat /sdcard/*.xml",
    "shell:ls /data/anr/*",
    "shell:screencap -p > /sdcard/screen_${day}.png",
    "shell:sh -c 'input swipe 1 2 3 4 && uiautomator dump /sdcard/w.xml'",
    "shell:rm -f /sdcard/window.xml",
    # `[` as the test builtin is not a glob command word
    "shell:[ -f /sdcard/x ] && echo y",
    # reading a script or a pushed file is not executing one
    "shell:cat /sdcard/p.sh",
    "shell:find /sdcard -name '*.png'",       # find with no -exec
])
def test_qua_2804_rules_do_not_touch_ordinary_qa(request_):
    assert deny_reason(request_) is None, request_


# ── QUA-2804: typed-text false positives ──────────────────────────────────────
# `input text '<payload>'` types a string; the device shell never RUNS it. The quoted
# payload of a top-level `input [source] text` is blanked before matching, so its
# parentheses and semicolons no longer read as `source`/`reboot`. The exemption is
# narrow: only a NON-expanding quoted payload, and only what the payload spans.

@pytest.mark.parametrize("request_", [
    "shell:input text 'Meeting (source review)'",
    'shell:input text "Meeting (source review)"',
    "shell:input text 'Call dentist; reboot later'",
    "shell,v2,raw:input text 'a && b || c'",
    "shell:input text '$(id)'",                       # literal chars, not substitution
    "shell:input text '`id`'",
    "shell:input keyboard text 'a; b'",               # `input <source> text`
    "shell:input -d 0 text 'a; reboot'",
])
def test_typed_text_is_not_split_or_deny_matched(request_):
    assert deny_reason(request_) is None, request_


@pytest.mark.parametrize("request_,why", [
    # the exemption is ONLY the quoted payload — what FOLLOWS it is still scanned
    ("shell:input text 'x'; su 0 id", "su"),
    ("shell:input text 'x' && run-as com.x cat files/qgb_flags.txt", "run-as"),
    ("shell:input text 'x' | sh", "script shell"),
    # an UNQUOTED payload really is split by the device shell — not exempt
    ("shell:input text a; reboot", "reboot"),
    # an EXPANDING double-quoted payload could run a substitution — not exempt
    ('shell:input text "$(su 0 id)"', "su"),
    # `text` that is not the `input` verb's argument is not exempt
    ("shell:cat text; su 0 id", "su"),
    # TRADE-OFF (documented in CLAUDE.md): the exemption is TOP-LEVEL only. A typed
    # payload NESTED inside `sh -c "…"` is not un-nested (its own quotes are gone by the
    # time the scanner sees it), so metacharacters in it are still scanned — as they
    # were before QUA-2804. Rephrase without the wrapper. Not a hole: it errs to refusal.
    ("shell:sh -c \"input text 'a; reboot'\"", "reboot"),
])
def test_typed_text_exemption_does_not_open_a_hole(request_, why):
    assert deny_reason(request_) == why, request_


# ── QUA-2804: forward/reverse behind a transport-scoped host prefix ────────────
# `adb -s <serial> forward …` can be sent as `host-serial:<serial>:forward:…` and
# `-t <id>` as `host-transport-id:<id>:forward:…`; both are the same host service as the
# bare `host:` form and must be refused. `host-local:`/`host-usb:` (`adb -e/-d
# get-state`) are ordinary transport reads and must NOT be a false positive.

@pytest.mark.parametrize("request_,why", [
    ("host-serial:emulator-5554:forward:tcp:7001;jdwp:1234", "adb forward"),
    ("host-serial:127.0.0.1:5555:forward:tcp:1;tcp:2", "adb forward"),
    ("host-serial:emulator-5554:killforward:tcp:7001", "adb forward"),
    ("host-transport-id:3:forward:tcp:1;jdwp:2", "adb forward"),
    ("host-transport-id:3:list-forward", "adb forward"),
])
def test_forward_behind_a_transport_scoped_prefix_is_denied(request_, why):
    assert deny_reason(request_) == why, request_


@pytest.mark.parametrize("request_", [
    "host-local:get-state",                # adb -e get-state
    "host-usb:get-state",                  # adb -d get-state
    "host-serial:emulator-5554:get-state",
    "host-serial:emulator-5554:features",
    "host-transport-id:3:get-state",
])
def test_transport_scoped_reads_are_not_forward_denied(request_):
    assert deny_reason(request_) is None, request_


_FORWARD_REASONS = frozenset({"adb forward", "adb reverse", "adb jdwp",
                              "adb track-jdwp", "adb track-app", "device socket"})


def test_saved_agent_adb_requests_lose_no_forward_or_socket_service(adb_replay_corpus):
    """Replay EVERY saved agent adb request — both arms, every subcommand — through the
    forward/reverse/jdwp/raw-socket rules. Target: zero legitimate request newly denied
    (the agents drove QA with shell/exec/sync only). Runs on the fixture corpus
    everywhere, and on the developer's saved runs with QGB_REPLAY_RUNS (QUA-2807)."""
    assert len(list(adb_replay_corpus.requests())) >= adb_replay_corpus.min_requests
    denied = adb_replay_corpus.newly_denied(lambda r: deny_reason(r) in _FORWARD_REASONS)
    assert denied == [], denied


_PRIVILEGE_REASONS = frozenset({
    "adb root", "adb unroot", "adb reboot", "adb tcpip", "adb usb", "adb remount",
    "adb disable-verity", "adb enable-verity", "adb sideload", "adb sideload-host",
    "adb kill-server", "reboot", "adbd property"})


def test_saved_agent_adb_requests_only_lose_privilege_changes(adb_replay_corpus):
    """Replay EVERY adb request the saved agents made — both arms, shell/exec and every
    other subcommand — through the privilege rules. The only newly denied requests may be
    agents changing adbd's privilege or the device's power state (the QUA-2784 re-run's
    `adb root`, which the fixture carries); no QA request is refused."""
    assert len(list(adb_replay_corpus.requests())) >= adb_replay_corpus.min_requests
    newly_denied = adb_replay_corpus.newly_denied(lambda r: deny_reason(r) in _PRIVILEGE_REASONS)
    # Every new denial is a bare privilege service; no shell request (where a false
    # positive would hide) is newly denied.
    assert all(r.split(":", 1)[0] in ("root", "unroot", "reboot", "tcpip", "usb")
               for r in newly_denied), newly_denied
    if adb_replay_corpus.exact:
        assert newly_denied == ["root:"]


def test_the_harness_root_would_be_denied_so_it_never_uses_the_meter():
    """`set_adb_root` (the root fixtures, `pin_device_clock`'s and
    `clear_crash_history`'s fallbacks) sends exactly the `root:`/`unroot:` the meter now
    refuses. It works only because harness adb goes straight to the upstream server;
    the meter port is set in `agent_env` alone. Pinned so nobody routes staging
    through the meter."""
    import inspect

    from qualgentbench import episode_runner as er

    assert deny_reason("root:") == "adb root"
    assert deny_reason("unroot:") == "adb unroot"
    # the post-episode check's own reads and repair are harness-side too
    assert deny_reason("shell:setprop service.adb.root 0") == "adbd property"
    src = inspect.getsource(er.set_adb_root) + inspect.getsource(er.check_adbd_after_agent)
    assert "ANDROID_ADB_SERVER_PORT" not in src and "_adb(" in src


@pytest.mark.asyncio
async def test_a_denied_adb_root_is_answered_fail_and_charged_as_today(tmp_path):
    """At the socket, after the transport handover: `root:` gets FAIL and never reaches
    the server; `metered_denied` 1, `metered_total` 0. The budget (`interactions.json`)
    charges it what a relayed `root:` always cost — nothing — so step accounting is
    unchanged."""
    upstream = _FakeAdbServer()
    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", upstream_port=await upstream.start(), log=log)
    port = await meter.start()

    r, w = await asyncio.open_connection("127.0.0.1", port)
    for req in ("host:tport:serial:emulator-5554", "root:"):
        w.write(f"{len(req):04x}{req}".encode())
        await w.drain()
    assert await r.readexactly(4) == b"OKAY"
    await r.readexactly(8)                                  # transport id
    assert await r.readexactly(4) == b"FAIL"
    msg = await r.readexactly(int((await r.readexactly(4)).decode(), 16))
    assert b"adb root is not available to the agent" in msg
    w.close()
    await asyncio.sleep(0.05)

    counts = read_counts(tmp_path / "c.json")
    assert (counts["metered_denied"], counts["metered_total"]) == (1, 0)
    assert "root:" not in upstream.requests
    assert log.total == 0                                   # as a relayed root: was
    await meter.stop()
    await upstream.stop()


@pytest.mark.asyncio
async def test_a_denied_jdwp_forward_is_answered_fail_and_never_reaches_the_server(tmp_path):
    """At the socket, after the transport handover: `host:forward:tcp:N;jdwp:<pid>` gets
    FAIL and the adb server never sees it, so no forward is created and no debugger port
    is opened to the app. `metered_denied` 1, `metered_total` 0; the budget charges it
    the 0 a relayed host service always cost (QUA-2797)."""
    upstream = _FakeAdbServer()
    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", upstream_port=await upstream.start(), log=log)
    port = await meter.start()

    r, w = await asyncio.open_connection("127.0.0.1", port)
    for req in ("host:tport:serial:emulator-5554", "host:forward:tcp:7001;jdwp:1234"):
        w.write(f"{len(req):04x}{req}".encode())
        await w.drain()
    assert await r.readexactly(4) == b"OKAY"
    await r.readexactly(8)                                  # transport id
    assert await r.readexactly(4) == b"FAIL"
    msg = await r.readexactly(int((await r.readexactly(4)).decode(), 16))
    assert b"adb forward is not available to the agent" in msg
    w.close()
    await asyncio.sleep(0.05)

    counts = read_counts(tmp_path / "c.json")
    assert (counts["metered_denied"], counts["metered_total"]) == (1, 0)
    assert not any("forward" in q for q in upstream.requests)
    assert log.total == 0                                   # a host service costs 0
    await meter.stop()
    await upstream.stop()


def test_the_harness_u2_forward_would_be_denied_so_it_never_uses_the_meter():
    """uiautomator2 and adbutils reach the on-device server with `adb forward`, which
    the meter now refuses. They work only because the harness's own adb reads the
    upstream server env (ANDROID_ADB_SERVER_ADDRESS/HOST), while the meter PORT is set
    for the agent alone. `episode_runner` pins the agent to the meter in exactly one
    place — `agent_env` — and never exports the meter port process-wide, so the
    harness's forwards (and any MCP server's) never traverse it (QUA-2797)."""
    import inspect

    from qualgentbench import episode_runner as er

    assert deny_reason("host:forward:tcp:9008;tcp:9008") == "adb forward"
    src = inspect.getsource(er.run_episode)
    # The only assignment of the meter port to the adb-server-port env var is inside
    # the agent_env dict handed to the adapter; nothing exports it process-wide (which
    # is what adbutils/u2 read), so the harness's own forwards never hit the meter.
    assert src.count("ANDROID_ADB_SERVER_PORT") == 1, src.count("ANDROID_ADB_SERVER_PORT")
    assert "agent_env" in src
    assert "os.environ[" not in src and "environ.update" not in src


def test_no_saved_agent_adb_request_is_a_new_false_positive(adb_replay_corpus):
    """Not one legitimate shell/exec request the agents made trips the stdin/pushed-script
    rules (QUA-2794) that the pre-QUA-2794 denylist (`_DENY_RULES` alone) did not."""
    from qualgentbench.adb_meter import _DENY_RULES

    def old_deny(request: str) -> str | None:
        low = request.strip().lower().replace("'", "").replace('"', "").replace("\\", "")
        for name, rx in _DENY_RULES:
            if rx.search(low):
                return name
        return None

    assert adb_replay_corpus.legitimate_newly_denied(deny_reason, old=old_deny, services=False) == []


# ── the replay fixture itself (QUA-2807) ──────────────────────────────────────

def test_the_fixture_corpus_is_part_of_the_repo_and_both_agent_formats_parse():
    corpus = fixture_corpus()
    ts = corpus.transcripts()
    assert len(ts) == 2 and all(str(t).startswith(str(corpus.roots[0])) for t in ts)
    per = {t.parts[-3]: sum(1 for tt, _ in corpus.requests() if tt == t) for t in ts}
    assert all(n >= 20 for n in per.values()), per       # claude stream-json AND codex json


def test_the_fixture_corpus_carries_every_shape_the_saved_agents_sent():
    """The families of request the saved agents sent (measured over 1666 requests in 611
    transcripts, 2026-09-24): a rule that false-positives on one of them is caught here
    without anyone's runs dir. Keep this list when trimming the fixture."""
    reqs = {r for _, r in fixture_corpus().requests()}
    families = ("shell,v2,raw:input tap", "shell,v2,raw:input text", "shell,v2,raw:input keyevent",
                "shell,v2,raw:input swipe", "shell,v2,raw:input keycombination",
                "shell,v2,raw:uiautomator dump", "shell,v2,raw:cat /sdcard/", "exec:uiautomator dump",
                "exec:cat /sdcard/", "exec:screencap -p", "shell,v2,raw:screencap -p",
                "shell,v2,raw:dumpsys window", "shell,v2,raw:dumpsys activity",
                "shell,v2,raw:dumpsys package", "shell,v2,raw:dumpsys dropbox",
                "shell,v2,raw:wm size", "shell,v2,raw:wm density", "shell,v2,raw:am start",
                "shell,v2,raw:am force-stop", "shell,v2,raw:monkey -p", "shell,v2,raw:pidof",
                "shell,v2,raw:date", "shell,v2,raw:cmd package", "shell,v2,raw:pm list",
                "shell,v2,raw:appops set", "shell,v2,raw:content query",
                "shell,v2,raw:settings put system user_rotation", "shell,v2,raw:ls -l /data/anr",
                "shell,v2,raw:getevent", "sync:", "host:devices", "host:logcat",
                "host:wait-for-device")
    missing = [f for f in families if not any(r.startswith(f) for r in reqs)]
    assert missing == [], missing


def test_every_planted_illegitimate_request_is_denied_for_its_reason():
    reqs = [r for _, r in fixture_corpus().requests()]
    for req, why in FIXTURE_ILLEGITIMATE.items():
        assert req in reqs, req
        assert deny_reason(req) == why, (req, deny_reason(req))
    # ...and nothing else in the fixture is denied today.
    assert sorted({r for r in reqs if deny_reason(r)}) == sorted(FIXTURE_ILLEGITIMATE)


def test_the_replay_catches_a_rule_that_refuses_legitimate_qa():
    """The replay has teeth: a rule that refused `input tap` is reported, and a forward an
    agent never sent would be parsed into the request the forward rule denies."""
    corpus = fixture_corpus()
    too_broad = corpus.legitimate_newly_denied(lambda r: "input tap" in r)
    assert too_broad == ["shell,v2,raw:input tap 540 1200", "shell,v2,raw:input tap 320 880"]
    assert corpus.legitimate_newly_denied(deny_reason) == []
    line = ('{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", '
            '"input": {"command": "adb -s emulator-5554 forward tcp:7001 jdwp:1234"}}]}}')
    assert list(adb_requests(line, services=True)) == ["host:forward:tcp:7001;jdwp:1234"]
    assert deny_reason("host:forward:tcp:7001;jdwp:1234") == "adb forward"


def test_saved_runs_are_opt_in_and_never_a_hard_coded_path(monkeypatch, tmp_path):
    import inspect

    import adb_replay

    assert adb_replay.saved_runs_corpus({}) is None
    a, b = tmp_path / "a", tmp_path / "b"
    corpus = adb_replay.saved_runs_corpus({adb_replay.SAVED_RUNS_ENV: f"{a}{os.pathsep}{b}"})
    assert corpus.roots == (a, b) and not corpus.exact and corpus.illegitimate == {}
    for code in (adb_replay, test_saved_agent_adb_requests_lose_no_forward_or_socket_service,
                 test_saved_agent_adb_requests_only_lose_privilege_changes,
                 test_no_saved_agent_adb_request_is_a_new_false_positive):
        src = inspect.getsource(code)
        assert "/Users" not in src and "Path.home(" not in src, code


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


@pytest.mark.live_device
@pytest.mark.asyncio
async def test_an_agents_adb_root_fails_at_the_meter_on_a_real_device(tmp_path, attached_device):
    """The QUA-2795 acceptance, with the real adb client: an agent's `adb root` through
    the meter exits non-zero with the meter's reason, is counted in `metered_denied`,
    and a following `adb shell id` through the same meter is NOT uid 0. The
    post-episode check then reads the device clean. Point ANDROID_SERIAL at a spare
    AVD (qgbench_root2:5556) — never emulator-5554, which carries live boards."""
    from qualgentbench.episode_runner import check_adbd_after_agent

    direct = subprocess.run(["adb", "-s", attached_device, "shell", "id -u"],
                            capture_output=True, text=True, timeout=30, check=False)
    if direct.stdout.strip() != "2000":
        pytest.skip(f"device adbd is not unrooted to start with ({direct.stdout!r})")

    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", log=log)
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port),
               ANDROID_ADB_SERVER_ADDRESS="127.0.0.1", ANDROID_ADB_SERVER_HOST="127.0.0.1")

    async def run(*argv):
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", attached_device, *argv, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode, out

    rc, out = await run("root")
    assert rc != 0, out
    assert b"adb root is not available to the agent" in out, out
    await asyncio.sleep(2)                          # what an agent's `sleep 2` would wait
    rc_id, out_id = await run("shell", "id")
    counts = (await meter.stop()).as_metrics()

    assert rc_id == 0 and b"uid=2000(shell)" in out_id and b"uid=0" not in out_id, out_id
    assert counts["metered_denied"] == 1, counts
    # The budget: `root:` costs 0 steps (as a relayed one always did), the `id` one
    # `other`. (`metered_total` is not asserted: `classify` files an unclassified
    # `shell,v2,…:` request as plumbing, a pre-existing diagnostic quirk.)
    assert (log.total, log.counts["other"]) == (1, 1), log.counts

    end = await check_adbd_after_agent(attached_device)
    assert end["rooted"] is False and end["root_primed"] is False, end


@pytest.mark.live_device
@pytest.mark.asyncio
async def test_an_agents_forward_and_jdwp_fail_at_the_meter_on_a_real_device(tmp_path, attached_device):
    """The QUA-2797 acceptance, with the real adb client on a debuggable image: an
    agent's `adb forward tcp:N jdwp:<pid>` and `adb jdwp` both FAIL at the meter, are
    counted in `metered_denied`, no forward is created (the JDWP debugger port never
    opens), and a following `adb shell` still works. Point ANDROID_SERIAL at a spare AVD
    (qgbench_root2:5556) — never emulator-5554, which carries live boards."""
    # A JDWP-debuggable process to aim at, over the harness's own adb (no meter).
    pid = subprocess.run(["adb", "-s", attached_device, "shell", "pidof",
                          "com.github.uiautomator"], capture_output=True, text=True,
                         timeout=30, check=False).stdout.strip().split()[:1]
    if not pid:
        pytest.skip("no debuggable process to target")
    pid = pid[0]
    port_local = "7099"

    # Record the forwards the server already has, so we can prove we added none.
    def forwards() -> set[str]:
        out = subprocess.run(["adb", "-s", attached_device, "forward", "--list"],
                             capture_output=True, text=True, timeout=30, check=False).stdout
        return {ln for ln in out.splitlines() if ln.strip()}

    before = forwards()

    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", log=log)
    mport = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(mport),
               ANDROID_ADB_SERVER_ADDRESS="127.0.0.1", ANDROID_ADB_SERVER_HOST="127.0.0.1")

    async def run(*argv):
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", attached_device, *argv, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode, out

    rc_f, out_f = await run("forward", f"tcp:{port_local}", f"jdwp:{pid}")
    assert rc_f != 0 and b"adb forward is not available to the agent" in out_f, out_f

    try:
        rc_j, out_j = await asyncio.wait_for(run("jdwp"), timeout=8)
    except asyncio.TimeoutError:                          # a relayed jdwp would stream
        pytest.fail("`adb jdwp` was not refused — it opened a stream")
    assert rc_j != 0 and b"adb jdwp is not available to the agent" in out_j, out_j

    # A shell still works through the same meter — the arm is not broken.
    rc_s, out_s = await run("shell", "echo", "ok")
    assert rc_s == 0 and b"ok" in out_s, out_s

    counts = (await meter.stop()).as_metrics()
    assert counts["metered_denied"] == 2, counts        # the forward and the jdwp
    # No forward was created: the request never reached the server.
    assert forwards() == before, (before, forwards())

    # Belt and braces: remove the forward if some earlier run leaked one.
    subprocess.run(["adb", "-s", attached_device, "forward", "--remove", f"tcp:{port_local}"],
                   capture_output=True, timeout=30, check=False)


def test_the_post_episode_check_records_root_and_unprimes(monkeypatch):
    """`check_adbd_after_agent` (harness adb, stubbed here): a rooted adbd is recorded
    and left to staging's unroot; a primed `service.adb.root` is recorded and reset to
    0, because `adb unroot` on an unrooted adbd leaves it at 1 (measured)."""
    from qualgentbench import episode_runner as er

    for answer, want, expect_unprime in [
        ("2000\n0\n", {"uid": 2000, "rooted": False, "root_primed": False}, False),
        ("2000\n\n", {"uid": 2000, "rooted": False, "root_primed": False}, False),
        ("0\n1\n", {"uid": 0, "rooted": True, "root_primed": True}, True),
        ("2000\n1\n", {"uid": 2000, "rooted": False, "root_primed": True}, True),
        ("error: device offline\n", {"uid": None, "rooted": None, "root_primed": False}, False),
    ]:
        calls: list[tuple[str, ...]] = []

        async def fake_adb(*args, _answer=answer, _calls=calls):
            _calls.append(args)
            return 0, (_answer if args[-1].startswith("id -u") else "")

        monkeypatch.setattr(er, "_adb", fake_adb)
        info = asyncio.run(er.check_adbd_after_agent("emulator-5556"))
        assert {k: info[k] for k in want} == want, (answer, info)
        unprimed = ("-s", "emulator-5556", "shell", "setprop service.adb.root 0") in calls
        assert unprimed is expect_unprime and info["unprimed"] is expect_unprime, calls
        assert not any(a[2:3] in (("root",), ("unroot",)) for a in calls), calls


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


# ── QUA-2804 acceptance on a real device ──────────────────────────────────────
# Opt-in only (`live_device` + QGB_LIVE_DEVICE=1); these tap the device, so never run
# them beside a benchmark. Point ANDROID_SERIAL at the bench AVD (`qgbench_root`) — the
# `su` image the corpus was derived on — never a shared emulator carrying a live board.

@pytest.mark.live_device
@pytest.mark.asyncio
async def test_a_listed_bypass_is_refused_at_the_meter_on_a_real_device(tmp_path, attached_device):
    """A representative QUA-2773-review bypass, with the real adb client through the
    meter: a glob command word (`/system/xbin/s?` → su) and a command substitution
    (`sh -c "$(cat …)"`) both FAIL at the meter, never print a root uid, and are counted
    denied. The same `su` over the harness's own adb (no meter) still roots — which is
    why the harness's privileged staging keeps working on this image."""
    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", log=log)
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port),
               ANDROID_ADB_SERVER_ADDRESS="127.0.0.1", ANDROID_ADB_SERVER_HOST="127.0.0.1")

    async def run(*argv):
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", attached_device, *argv, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode, out

    # A control OUTSIDE the meter: prove the device can actually root, so a clean uid=0
    # below would be a real escape, not just an image without `su`.
    ctl = subprocess.run(["adb", "-s", attached_device, "shell", "su 0 id -u"],
                         capture_output=True, text=True, timeout=30, check=False).stdout
    if ctl.strip() != "0":
        pytest.skip("device has no working su; the bypass has nothing to reach")

    rc1, out1 = await run("shell", "/system/xbin/s? 0 id")
    assert rc1 != 0 and b"uid=0" not in out1, out1
    assert b"glob command is not available to the agent" in out1, out1

    # Command substitution — its OUTPUT is the command, unreadable to the meter. Use a
    # payload with no literal `su` (else the `su` rule fires first); it never runs.
    rc2, out2 = await run("shell", 'sh -c "$(cat /sdcard/qgb_payload.sh)"')
    assert rc2 != 0 and b"uid=0" not in out2, out2
    assert b"command substitution is not available to the agent" in out2, out2

    counts = (await meter.stop()).as_metrics()
    assert counts["metered_denied"] == 2 and counts["metered_total"] == 0, counts


@pytest.mark.live_device
@pytest.mark.asyncio
async def test_an_ordinary_metered_session_still_works_on_a_real_device(tmp_path, attached_device):
    """The other half of the acceptance: an ordinary QA session runs UNIMPEDED through
    the meter — an install flow, a launch, logcat, a uiautomator dump, and `input text`
    carrying parentheses and semicolons (the QUA-2804 false positives). None is refused,
    and each metered op is charged."""
    log = InteractionLog(tmp_path / "interactions.json")
    meter = AdbMeter(tmp_path / "c.json", log=log)
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port),
               ANDROID_ADB_SERVER_ADDRESS="127.0.0.1", ANDROID_ADB_SERVER_HOST="127.0.0.1")

    async def run(*argv, want_rc: bool = True):
        proc = await asyncio.create_subprocess_exec(
            "adb", "-s", attached_device, *argv, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        assert b"is not available to the agent" not in out, (argv, out)
        if want_rc:
            assert proc.returncode == 0, (argv, out)
        return proc.returncode, out

    # install flow (a create/abandon session — the `cmd package` install path through
    # the meter, no bytes needed and nothing left installed)
    _, out = await run("shell", "pm", "install-create", "-t")
    sid = out.decode().strip().rsplit("[", 1)[-1].rstrip("]")
    if sid.isdigit():
        await run("shell", "pm", "install-abandon", sid)

    # launch, logcat, screen dump
    await run("shell", "am", "start", "-a", "android.settings.SETTINGS")
    await run("shell", "logcat", "-d", "-t", "1")
    await run("shell", "uiautomator", "dump", "/sdcard/qgb_acc_dump.xml")

    # the QUA-2804 typed-text false positives: parentheses and a semicolon. The quotes
    # must reach the device (and so the meter) — a single shell string, exactly how an
    # agent types a literal string that itself contains shell metacharacters.
    await run("shell", "input text 'Meeting (source review)'")
    await run("shell", 'input text "Call dentist; reboot later"')

    counts = (await meter.stop()).as_metrics()
    assert counts["metered_denied"] == 0, counts
    assert counts["metered_total"] >= 5, counts

    subprocess.run(["adb", "-s", attached_device, "shell", "rm", "-f",
                    "/sdcard/qgb_acc_dump.xml"], capture_output=True, timeout=30, check=False)
