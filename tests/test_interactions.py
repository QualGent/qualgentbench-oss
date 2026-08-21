"""The shared step unit (docs/interaction-spec.md) — one interaction per QA act,
counted the same in both arms.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from qualgentbench import interactions as ix
from qualgentbench.interactions import InteractionLog, classify_adb, classify_mcp
from qualgentbench.mcp_meter import McpMeter


# ── the vocabulary ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("request_,kind", [
    ("shell:input tap 540 900", ix.TAP),
    ("shell,v2,TERM=xterm,raw:input tap 1 2", ix.TAP),
    ("shell:input swipe 1 2 3 4", ix.SWIPE),
    ("shell:input text hello", ix.TYPE),
    ("shell:input keyevent 4", ix.PRESS),
    ("shell:am start -n com.x/.Main", ix.LAUNCH),
    ("shell:am force-stop com.x", ix.TERMINATE),
    ("shell:uiautomator dump /sdcard/w.xml", ix.OBSERVE),
    ("shell:screencap -p", ix.OBSERVE),
    ("shell:dumpsys window", ix.OBSERVE),
])
def test_adb_requests_map_to_interactions(request_, kind):
    assert classify_adb(request_) == kind


@pytest.mark.parametrize("request_", [
    "host:version", "host:devices", "host:tport:serial:emulator-5554",
    "host-serial:emulator-5554:features",
])
def test_adb_plumbing_is_not_an_interaction(request_):
    assert classify_adb(request_) is None


@pytest.mark.parametrize("tool,kind", [
    ("mobile_tap", ix.TAP),
    ("mcp__device__mobile_tap", ix.TAP),
    ("mcp__device__mobile_long_press", ix.TAP),
    ("mobile_swipe", ix.SWIPE),
    ("mobile_type_text", ix.TYPE),
    ("mobile_press_button", ix.PRESS),
    ("mobile_launch_app", ix.LAUNCH),
    ("mobile_terminate_app", ix.TERMINATE),
    ("mobile_observe_screen", ix.OBSERVE),
    ("mobile_take_screenshot", ix.OBSERVE),
])
def test_mcp_tools_map_to_interactions(tool, kind):
    assert classify_mcp(tool) == kind


@pytest.mark.parametrize("tool", [
    "qg_release_device", "mcp__device__qg_acquire_device",
    "mobile_list_available_devices", "mobile_get_screen_size", "Read", "Bash",
])
def test_non_device_tools_are_not_interactions(tool):
    assert classify_mcp(tool) is None


# ── the two grouping rules ───────────────────────────────────────────────────

def test_typing_is_one_interaction_regardless_of_length(tmp_path):
    """A tester filling a field does one thing — a longer string must not cost
    more steps than a short one."""
    log = InteractionLog(tmp_path / "i.json")
    log.record_mcp("mobile_type_text")
    assert log.as_metrics() == {**log.as_metrics(), "interactions": 1}
    assert log.counts[ix.TYPE] == 1

    raw = InteractionLog(tmp_path / "r.json")
    raw.record_adb("shell:input text a")
    raw.record_adb("shell:input text a-much-longer-string-of-test-data")
    assert raw.total == 2                      # two entries, not 40 characters


def test_reading_the_screen_is_one_observe(tmp_path):
    """Raw reads the screen with dump + pull + cat — that is one look, not three."""
    log = InteractionLog(tmp_path / "i.json")
    for req in ("shell:uiautomator dump /sdcard/w.xml",
                "sync:",                              # adb pull of the dump
                "shell:cat /sdcard/w.xml"):
        log.record_adb(req)
    assert log.total == 1
    assert log.counts[ix.OBSERVE] == 1


def test_read_collapsing_is_stateless(tmp_path):
    """A command classifies the same way wherever it appears, so ordering cannot
    change a score."""
    a = InteractionLog(tmp_path / "a.json")
    a.record_adb("sync:")
    a.record_adb("shell:uiautomator dump /sdcard/w.xml")
    b = InteractionLog(tmp_path / "b.json")
    b.record_adb("shell:uiautomator dump /sdcard/w.xml")
    b.record_adb("sync:")
    assert a.total == b.total == 1


def test_one_tap_costs_the_same_in_both_arms(tmp_path):
    """The property the whole spec exists for: the same QA act costs the same step
    whichever interface performed it."""
    raw = InteractionLog(tmp_path / "raw.json")
    dev = InteractionLog(tmp_path / "dev.json")
    raw.record_adb("shell:input tap 540 900")
    dev.record_mcp("mobile_tap")
    assert raw.as_metrics()["interactions"] == dev.as_metrics()["interactions"] == 1
    assert raw.counts[ix.TAP] == dev.counts[ix.TAP] == 1


def test_launch_costs_one_in_both_arms_even_though_adb_sees_zero(tmp_path):
    """`mobile_launch_app` reaches the device without adb, so an adb-level meter
    cannot see it — that is why the mcp arm is counted at the MCP boundary."""
    raw = InteractionLog(tmp_path / "raw.json")
    dev = InteractionLog(tmp_path / "dev.json")
    raw.record_adb("shell:am start -n com.x/.Main")
    dev.record_mcp("mobile_launch_app")
    assert raw.total == dev.total == 1


# ── the log as the budget hook reads it ──────────────────────────────────────

def test_counts_are_flushed_atomically_for_the_hook(tmp_path):
    log = InteractionLog(tmp_path / "i.json")
    log.record_adb("shell:input tap 1 1")
    data = json.loads((tmp_path / "i.json").read_text())
    assert data["interactions"] == 1 and data["interactions_tap"] == 1
    assert ix.read_total(tmp_path / "i.json") == 1


def test_read_total_survives_a_corrupt_file(tmp_path):
    (tmp_path / "i.json").write_text('{"interactions": ')
    assert ix.read_total(tmp_path / "i.json") == 0


# ── the MCP meter ────────────────────────────────────────────────────────────

class _FakeMcp:
    """Echoes back a fixed HTTP response; enough to prove requests are counted and
    relayed byte-for-byte."""

    def __init__(self):
        self.received = b""

    async def start(self) -> int:
        self._s = await asyncio.start_server(self._h, "127.0.0.1", 0)
        return self._s.sockets[0].getsockname()[1]

    async def stop(self):
        self._s.close()
        await self._s.wait_closed()

    async def _h(self, r, w):
        try:
            self.received += await r.read(65536)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await w.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()


def _rpc(tool: str) -> bytes:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": {"device": "d"}}}).encode()
    return (b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


@pytest.mark.asyncio
async def test_mcp_meter_counts_tool_calls_and_relays_bytes(tmp_path):
    upstream = _FakeMcp()
    port = await upstream.start()
    log = InteractionLog(tmp_path / "i.json")
    meter = McpMeter(log, f"http://127.0.0.1:{port}")
    mport = await meter.start()

    r, w = await asyncio.open_connection("127.0.0.1", mport)
    payload = _rpc("mobile_tap")
    w.write(payload)
    await w.drain()
    await asyncio.wait_for(r.read(64), timeout=5)
    w.close()
    await asyncio.sleep(0.05)

    assert log.total == 1 and log.counts[ix.TAP] == 1
    assert payload in upstream.received        # relayed untouched
    await meter.stop()
    await upstream.stop()


@pytest.mark.asyncio
async def test_mcp_meter_ignores_non_tool_traffic(tmp_path):
    """`initialize`, `tools/list` and notifications are session plumbing. Counting
    them would charge an agent for connecting."""
    upstream = _FakeMcp()
    log = InteractionLog(tmp_path / "i.json")
    meter = McpMeter(log, f"http://127.0.0.1:{await upstream.start()}")
    mport = await meter.start()

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    r, w = await asyncio.open_connection("127.0.0.1", mport)
    w.write(b"POST /mcp HTTP/1.1\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\n\r\n" + body)
    await w.drain()
    await asyncio.wait_for(r.read(64), timeout=5)
    w.close()
    await asyncio.sleep(0.05)

    assert log.total == 0
    await meter.stop()
    await upstream.stop()


@pytest.mark.asyncio
async def test_a_request_split_across_packets_is_counted_once(tmp_path):
    """MCP streamable-http chunks; a tool call can straddle a TCP segment boundary."""
    upstream = _FakeMcp()
    log = InteractionLog(tmp_path / "i.json")
    meter = McpMeter(log, f"http://127.0.0.1:{await upstream.start()}")
    mport = await meter.start()

    payload = _rpc("mobile_observe_screen")
    cut = len(payload) - 20
    r, w = await asyncio.open_connection("127.0.0.1", mport)
    w.write(payload[:cut]); await w.drain()
    await asyncio.sleep(0.05)
    w.write(payload[cut:]); await w.drain()
    await asyncio.wait_for(r.read(64), timeout=5)
    w.close()
    await asyncio.sleep(0.05)

    assert log.total == 1 and log.counts[ix.OBSERVE] == 1
    await meter.stop()
    await upstream.stop()


# ── the invariant that keeps this from regressing ────────────────────────────

def test_every_adapter_budgets_from_the_same_file(tmp_path):
    """Adding a coding agent must not mean adding a counter — incompatible step
    definitions make a cross-agent leaderboard meaningless."""
    from types import SimpleNamespace

    from qualgentbench.adapters.base import RunContext
    from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
    from qualgentbench.adapters.codex_cli import CodexCliAdapter
    from qualgentbench.schemas import Condition

    for cls, home in ((ClaudeCodeAdapter, "hooks"), (CodexCliAdapter, "codex_home/hooks")):
        run_dir = tmp_path / cls.name
        run_dir.mkdir()
        ctx = RunContext(
            task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=900)),
            agent=cls.name, model="m", condition=Condition.no_routines, trial=1,
            run_dir=run_dir, mcp_server="http://localhost:51821",
            mcp_config_path=run_dir / "mcp.json", workspace_dir=run_dir / "ws",
            disabled_tools=[], inject_mcp=False, tool_call_cap=50,
        )
        cls().prepare(ctx)
        script = run_dir / home / "tool_cap.py"
        assert script.exists(), f"{cls.name} does not write the shared hook"
        body = script.read_text()
        assert str(run_dir / "interactions.json") in body, \
            f"{cls.name} budgets from something other than the interaction log"
        assert "findall" not in body, f"{cls.name} still counts the command string"


def test_an_episode_is_labelled_v3_only_if_measured_in_v3(tmp_path):
    """The budget tooling refuses to mix units and decides from this label, so it
    must key on the interaction count, not the adb count."""
    from pathlib import Path
    from qualgentbench import bugs
    from qualgentbench.bugs import load_suite, exploration_task, exploration_verdict

    def _score(**spec):
        task = exploration_task(load_suite(
            Path("src/qualgentbench/data/benchmarks/birday.yaml")))
        task.bug_spec.update({"tooling": "raw", "step_cap": 314, **spec})
        return exploration_verdict("", "m", task).metrics["budget_accounting"]

    assert _score(interactions=47) == bugs.BUDGET_ACCOUNTING == "interaction-v3"
    assert _score(metered_total=65) == "per-adb-v1"      # adb count alone is not v3
    assert _score() == "per-adb-v1"                      # legacy episode


def test_the_evidence_page_explains_an_exhausted_budget():
    """An exhausted budget must say where the steps went, not just "231 / 230"."""
    from qualgentbench.evidence_report import _budget_breakdown
    html = _budget_breakdown({
        "step_budget": 230, "truncated": True,
        "interactions": {"interactions": 223, "interactions_press": 189,
                         "interactions_tap": 14, "interactions_observe": 11,
                         "interactions_type": 8, "interactions_launch": 1},
    })
    assert "Where the budget went" in html
    assert "189" in html and "85%" in html
    assert "Budget exhausted" in html and "press" in html
    # Kinds with no activity are omitted rather than rendered as noise.
    assert "swipe" not in html


def test_the_breakdown_is_omitted_for_an_episode_without_interaction_data():
    from qualgentbench.evidence_report import _budget_breakdown
    assert _budget_breakdown({"step_budget": 230}) == ""
    assert _budget_breakdown({"interactions": {"interactions": 0}}) == ""


# ── the scanner: the part of the mcp meter that decides whether a call counts ────

def _rpc(name: str, args: str = '{"x":1}') -> bytes:
    return (f'{{"jsonrpc":"2.0","id":7,"method":"tools/call",'
            f'"params":{{"name":"{name}","arguments":{args}}}}}').encode()


def test_scanner_counts_a_request_straddling_two_reads():
    """Scanning only bytes newer than the last read meant a request split across
    TCP segments could never match."""
    from qualgentbench.mcp_meter import _Scanner
    body = _rpc("mobile_tap")
    s = _Scanner()
    assert s.feed(body[:25]) == []
    assert s.feed(body[25:]) == ["mobile_tap"]
    assert s.feed(b"") == []                      # never double-counted


def test_scanner_sees_through_chunked_transfer_framing():
    """Chunked framing lands `\\r\\n<hex>\\r\\n` inside the JSON, splitting the literals
    the regex anchors on. Raw CRLF cannot occur inside a JSON string, so stripping
    the framing from the scan copy is always safe."""
    from qualgentbench.mcp_meter import _Scanner
    body = _rpc("mobile_launch_app")
    wire = b""
    for i in range(0, len(body), 7):              # 7-byte chunks split every literal
        piece = body[i:i + 7]
        wire += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
    wire = b"POST /mcp HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n" \
           + wire + b"0\r\n\r\n"
    s = _Scanner()
    assert s.feed(wire) == ["mobile_launch_app"]


def test_scanner_survives_large_arguments_before_the_name():
    """Serializers that put `arguments` before `name` push them apart; the old
    400-char window missed those."""
    from qualgentbench.mcp_meter import _Scanner
    big = '{"text":"' + "x" * 1500 + '"}'
    body = (f'{{"jsonrpc":"2.0","id":8,"method":"tools/call",'
            f'"params":{{"arguments":{big},"name":"mobile_type_text"}}}}').encode()
    s = _Scanner()
    assert s.feed(body) == ["mobile_type_text"]


def test_scanner_counts_pipelined_requests_once_each():
    from qualgentbench.mcp_meter import _Scanner
    s = _Scanner()
    assert s.feed(_rpc("a1") + _rpc("b2")) == ["a1", "b2"]
    assert s.feed(_rpc("c3")) == ["c3"]
