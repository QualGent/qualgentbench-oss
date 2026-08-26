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

from qualgentbench.adb_meter import AdbMeter, classify, read_counts


def _device_available() -> bool:
    if not shutil.which("adb"):
        return False
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip().endswith("\tdevice") for line in out.splitlines()[1:])


needs_device = pytest.mark.skipif(not _device_available(),
                                  reason="no adb device attached")


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


# ── the real thing ───────────────────────────────────────────────────────────

@needs_device
@pytest.mark.asyncio
async def test_a_wrapped_script_is_charged_for_every_operation(tmp_path):
    """End to end: adb calls wrapped in a script are still charged per operation."""
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import subprocess\n"
        "def adb(c):\n"
        "    subprocess.run(f'adb -s emulator-5554 {c}', shell=True, capture_output=True)\n"
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


@needs_device
@pytest.mark.asyncio
async def test_direct_adb_still_reaches_the_device_through_the_proxy(tmp_path):
    """A meter that broke adb would be worse than no meter."""
    meter = AdbMeter(tmp_path / "c.json")
    port = await meter.start()
    env = dict(os.environ, ANDROID_ADB_SERVER_PORT=str(port))
    proc = await asyncio.create_subprocess_shell(
        "adb -s emulator-5554 shell echo qgb-roundtrip", env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    await meter.stop()
    assert b"qgb-roundtrip" in out


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
