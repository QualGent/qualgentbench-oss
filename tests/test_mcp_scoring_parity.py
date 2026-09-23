"""MCP-arm scoring parity between claude-code and codex-cli (QUA-2776).

A Fable-vs-Astra board runs one model through each adapter, so anything the scorer
reads differently in the two transcript shapes publishes as a MODEL gap. These tests
write ONE DevLoop episode — the same calls, arguments and MCP results — in both shapes
(claude-code `--output-format stream-json`, codex `exec --json`) and require every
quantity a journey or hunt scorer reads to come out identical.

The episode covers each asymmetry the ticket found: claude's `mcp__device__` prefix
against codex's bare names; a tool codex's tracked-name filter used to drop
(`mobile_launch_app`, `mobile_get_screen_size`); DevLoop's `[ImageContent,
TextContent]` screen read, which claude kept as text and codex as `json.dumps` of the
whole result; a tool that answered `isError` (claude `is_error`, codex status
`failed`), including DevLoop refusing a `mobile_report_result` — QUA-2777's report
channel skips refused calls, so the refusal must read the same on both; and a PARALLEL
batch, which claude-code records as call, call, result, result while codex records
sequential items."""

from __future__ import annotations

import base64
import itertools
import json
import tomllib
from dataclasses import dataclass, field

import pytest

from qualgentbench import bugs, journey
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.transcript import TranscriptParser, tool_base_name

from test_ablation import _context, _hunt_task


# ── one episode, two transcript shapes ─────────────────────────────────────────

# A real-sized PNG payload: long enough that the old codex path's `<image>` stub, or a
# base64 run, would be text one agent sees and the other does not.
_PNG = base64.b64encode(bytes(range(256)) * 24).decode()


@dataclass
class Call:
    tool: str
    args: dict
    text: str | None = None          # the TextContent block, if any
    image: bool = False              # an ImageContent block BEFORE the text (DevLoop)
    is_error: bool = False           # MCP CallToolResult.isError


@dataclass
class Say:
    text: str


@dataclass
class Batch:
    """Calls the agent issued in ONE turn (claude-code: several tool_use blocks, then
    their results; codex runs them as sequential items)."""
    calls: list[Call] = field(default_factory=list)


def _screen(*labels: str) -> str:
    return json.dumps({"elements": [{"text": t, "clickable": True} for t in labels],
                       "screen_changed": True})


# What each version's tester files through DevLoop's `mobile_report_result`. The first
# call is REFUSED by the server (DevLoop requires `code_investigation` on FAIL and
# `blocker_investigation` on BLOCKED) — an MCP `isError` — and the second is accepted.
_REPORT_CALLS = {
    "clean": (
        {"status": "BLOCKED", "summary": "stock screen slow to load"},
        {"status": "PASS", "summary": "Stock screen shows Amount 10."},
    ),
    "seeded": (
        {"status": "FAIL", "failure_step": "3", "expected": "Amount", "actual": "Color",
         "summary": "the stock button opened the medicine settings, not the stock screen"},
        {"status": "FAIL", "failure_step": "3", "expected": "Amount", "actual": "Color",
         "summary": "the stock button opened the medicine settings, not the stock screen",
         "code_investigation": "no source available"},
    ),
}
_REFUSAL = {
    "clean": "blocker_investigation is required for BLOCKED results",
    "seeded": "code_investigation is required for FAIL results",
}


def _episode(version: str) -> list:
    """A 10-device-call DevLoop episode on medtimer-check-stock, then the report tool
    (refused once, then accepted). Only what the Medicine stock button opened, and
    the report, differ between versions."""
    stock_screen = _STOCK_SCREEN[version]
    refused, accepted = _REPORT_CALLS[version]
    return [
        Say("I'll launch the app and read the first screen."),
        Call("mobile_launch_app", {"package_name": "com.futsch1.medtimer"}, "Launched."),
        Call("mobile_observe_screen", {}, _screen("Overview", "Medicine", "Analysis"), image=True),
        Call("mobile_tap", {"text": "Medicine"}, "Tapped 'Medicine'."),
        Batch([
            Call("mobile_find_views", {"text": "Ibuprofen"},
                 json.dumps({"matches": [{"text": "Ibuprofen", "bounds": [0, 300, 1080, 420]}]})),
            Call("mobile_get_screen_size", {}, json.dumps({"width": 1080, "height": 2400})),
        ]),
        Call("mobile_tap", {"text": "Ibuprofen (4)"},
             "Element 'Ibuprofen (4)' was not visible", is_error=True),
        Call("mobile_tap_and_observe", {"text": "0 reminders"},
             _screen("Ibuprofen", "Medicine stock", "0 reminders"), image=True),
        Call("mobile_tap_and_observe", {"text": "Medicine stock"},
             _screen(*stock_screen), image=True),
        Call("mobile_observe_screen", {}, _screen(*stock_screen), image=True),
        Say("Done reading the stock screen; filing the result."),
        Call("mobile_report_result", refused, _REFUSAL[version], is_error=True),
        Call("mobile_report_result", accepted,
             json.dumps({"ok": True, "status": accepted["status"]})),
    ]


def _claude(episode: list) -> str:
    """claude-code stream-json: `mcp__device__` names, one tool_result per call with the
    MCP content list as its content, `is_error` on a failed tool, and the CLI's own
    `tool_use_result` echo on the user event (which no scorer may read twice)."""
    ids = itertools.count(1)
    lines = [{"type": "system", "subtype": "init", "model": "claude-opus-5",
              "tools": ["Bash", "Read", "mcp__device__mobile_tap"],
              "mcp_servers": [{"name": "device", "status": "connected"}]}]

    def content(c: Call) -> list[dict]:
        out = []
        if c.image:
            out.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                    "data": _PNG}})
        if c.text is not None:
            out.append({"type": "text", "text": c.text})
        return out

    for step in episode:
        n = next(ids)
        if isinstance(step, Say):
            lines.append({"type": "assistant", "message": {
                "id": f"msg_{n}", "role": "assistant", "model": "claude-opus-5",
                "content": [{"type": "text", "text": step.text}],
                "usage": {"input_tokens": 10, "output_tokens": 5}}})
            continue
        calls = step.calls if isinstance(step, Batch) else [step]
        uses = [(f"toolu_{n}_{i}", c) for i, c in enumerate(calls)]
        lines.append({"type": "assistant", "message": {
            "id": f"msg_{n}", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "tool_use", "id": tid, "name": f"mcp__device__{c.tool}",
                         "input": c.args} for tid, c in uses],
            "usage": {"input_tokens": 10, "output_tokens": 5}}})
        for tid, c in uses:
            block = {"type": "tool_result", "tool_use_id": tid, "content": content(c)}
            if c.is_error:
                block["is_error"] = True
            lines.append({"type": "user", "message": {"role": "user", "content": [block]},
                          "tool_use_result": content(c)})
    lines.append({"type": "result", "subtype": "success", "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 100, "output_tokens": 50}})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _codex(episode: list) -> str:
    """codex `exec --json`: bare tool names beside `server`, item.started +
    item.completed per call, the MCP content list under `item.result.content` (images
    as `{type: image, data, mimeType}`), and a tool's `isError` as status `failed`."""
    ids = itertools.count(1)
    lines = [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"}]
    for step in episode:
        if isinstance(step, Say):
            lines.append({"type": "item.completed", "item": {
                "id": f"item_{next(ids)}", "type": "agent_message", "text": step.text}})
            continue
        for c in (step.calls if isinstance(step, Batch) else [step]):
            iid = f"item_{next(ids)}"
            base = {"id": iid, "type": "mcp_tool_call", "server": "device", "tool": c.tool,
                    "arguments": c.args}
            lines.append({"type": "item.started", "item": {**base, "status": "in_progress"}})
            blocks = []
            if c.image:
                blocks.append({"type": "image", "data": _PNG, "mimeType": "image/png"})
            if c.text is not None:
                blocks.append({"type": "text", "text": c.text})
            lines.append({"type": "item.completed", "item": {
                **base, "result": {"content": blocks, "structured_content": None},
                "error": None, "status": "failed" if c.is_error else "completed"}})
    lines.append({"type": "turn.completed",
                  "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


# The same bytes the harness reads off disk at episode end for BOTH agents.
_FINDINGS = {
    "clean": "verdict: pass\nbugs: []\n",
    "seeded": ('verdict: fail\nbugs:\n  - step: 3\n    observed: "Color"\n'
               '    expected: "Amount"\n'
               '    description: "the stock button opened the medicine settings, '
               'not the stock screen"\n'),
}
_STOCK_SCREEN = {
    "clean": ("Ibuprofen", "Amount", "10", "Refill"),
    "seeded": ("Ibuprofen", "Color", "Default", "Medicine cannot be skipped"),
}


def _case(version: str, report: str = "findings_file"):
    """`report="report_tool"`: no findings file at all, so the accepted
    `mobile_report_result` call is the report (QUA-2777's lowest-precedence source)."""
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == "medtimer")
    task = next(t for t in journey.journey_tasks(suite)
                if t.id == f"medtimer-check-stock~{version}")
    task.bug_spec["tooling"] = "mcp"
    task.bug_spec["findings_file"] = _FINDINGS[version] if report == "findings_file" else ""
    return task


def _pair(version: str) -> tuple[str, str]:
    ep = _episode(version)
    return _claude(ep), _codex(ep)


# The cost/token block (`pricing.usage_metrics`) is each CLI's own accounting — claude
# reports a cost, codex reports turn deltas — not tool activity, so it is the one part
# of the metrics that may differ between the two shapes of one episode.
_AGENT_ACCOUNTING = ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens",
                     "cost_usd", "cost_source", "usage_source")


def _scored(metrics: dict) -> dict:
    return {k: v for k, v in metrics.items() if k not in _AGENT_ACCOUNTING}


def _events(transcript: str) -> list[tuple]:
    return [(e.name, e.input, e.result_text, e.success, e.is_device_tool,
             e.is_observation, e.is_device_evidence)
            for e in TranscriptParser(transcript).events()]


# ── the paired tests ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", ["clean", "seeded"])
def test_parser_events_are_identical_for_identical_mcp_activity(version):
    claude, codex = _pair(version)
    assert _events(claude) == _events(codex)
    events = TranscriptParser(codex).events()
    # Every call is an event on both — none dropped by a tracked-name filter.
    assert [e.name for e in events] == [
        "mobile_launch_app", "mobile_observe_screen", "mobile_tap", "mobile_find_views",
        "mobile_get_screen_size", "mobile_tap", "mobile_tap_and_observe",
        "mobile_tap_and_observe", "mobile_observe_screen", "mobile_report_result",
        "mobile_report_result"]
    # The screen read's text is the elements JSON alone: no image, no stub, no wrapper.
    observe = events[1]
    assert observe.result_text == _screen("Overview", "Medicine", "Analysis")
    # A tool that answered isError failed on both, whatever its text says, and is the
    # structural refusal QUA-2777's report channel skips.
    assert [i for i, e in enumerate(events) if not e.success] == [5, 9]
    assert [i for i, e in enumerate(events) if e.is_error] == [5, 9]
    for parser in (TranscriptParser(claude), TranscriptParser(codex)):
        assert [(e.input, e.is_error) for e in parser.report_tool_calls()] == [
            (a, i == 0) for i, a in enumerate(_REPORT_CALLS[version])]


@pytest.mark.parametrize("version", ["clean", "seeded"])
def test_every_scorer_input_is_identical(version):
    claude, codex = _pair(version)
    pc, px = TranscriptParser(claude), TranscriptParser(codex)

    # launch, observe, tap, find_views, (screen size: free), failed tap, 2 × tap+observe
    # (2 each), observe, (report: free) — what the MCP meter charges.
    assert bugs._device_actions(pc, "mcp") == bugs._device_actions(px, "mcp") == 10
    assert pc.observation_texts() == px.observation_texts()
    assert len(px.observation_texts()) == 5    # 2 observes, find_views, 2 × tap+observe
    assert ([(e.name, e.input, e.result_text) for e in pc.successful_device_events()]
            == [(e.name, e.input, e.result_text) for e in px.successful_device_events()])
    assert (bugs._device_interaction_texts(pc, "mcp")
            == bugs._device_interaction_texts(px, "mcp"))
    assert bugs._count_tool_calls(claude) == bugs._count_tool_calls(codex) == 11

    for split in (False, True):
        c = bugs._ordered_stream(claude, "mcp", split_calls=split)
        x = bugs._ordered_stream(codex, "mcp", split_calls=split)
        assert c == x, split
    for results_only in (False, True):
        assert (journey._device_texts(claude, "mcp", results_only=results_only)
                == journey._device_texts(codex, "mcp", results_only=results_only))
    for screen_only in (False, True):
        c = journey._observation_texts(claude, "mcp", screen_only=screen_only)
        assert c == journey._observation_texts(codex, "mcp", screen_only=screen_only)
    # The parallel batch paired each result with ITS call: the size query is no screen
    # read, the find_views answer is (a QUERY), and neither is a whole-screen read.
    reads = journey._observation_texts(claude, "mcp")
    assert any("ibuprofen" in t and "bounds" in t for t in reads)
    assert not any('"width"' in t for t in reads)
    assert not any("bounds" in t for t in journey._observation_texts(claude, "mcp",
                                                                      screen_only=True))


@pytest.mark.parametrize("report", ["findings_file", "report_tool"])
@pytest.mark.parametrize("version", ["clean", "seeded"])
def test_journey_verdict_metrics_are_identical(version, report):
    claude, codex = _pair(version)
    vc = journey.journey_verdict(claude, "m", _case(version, report))
    vx = journey.journey_verdict(codex, "m", _case(version, report))
    assert _scored(vc.metrics) == _scored(vx.metrics)
    assert vc.passed == vx.passed and vc.failure_reason == vx.failure_reason
    m = vx.metrics
    # And the episode scores as it should, so equality is not two identical zeros.
    assert m["report_source"] == report
    assert m["report_tool"] == {"calls": 2, "refused": 1, "used": report == "report_tool"}
    assert m["completed"] is True
    assert m["witness"]["seen"] == (["Amount"] if version == "clean" else [])
    if version == "seeded":
        assert m["bugs_found"] == ["stock-button-opens-settings"]
        assert m["grounded_reports"] == 1 and m["false_reports"] == 0
    else:
        assert m["false_reports"] == 0


def test_hunt_probe_gate_and_mcp_counts_are_identical():
    """The hunt path: the probe gate reads `_device_interaction_texts` and the temporal
    gate counts device entries in `_ordered_stream`. codex's MCP calls matched neither
    (`startswith("mcp__device")`, and one folded entry per call where claude has two)."""
    ep = [
        Call("mobile_launch_app", {"package_name": "com.example.notes"}, "Launched."),
        Call("mobile_observe_screen", {}, _screen("Login", "Password"), image=True),
        Call("mobile_type_text", {"text": "hunter2", "field": "password"}, "Typed."),
        Say("AREA: login | VERDICT: broken"),
        Call("mobile_report_result", {"status": "FAIL",
                                      "summary": "RESULT: login=broken, view_notes=ok"}, "ok"),
    ]
    claude, codex = _claude(ep), _codex(ep)

    def task():
        t = _hunt_task("mcp")
        for f in t.bug_spec["features"]:
            if f["id"] == "login":
                f["probe"] = ["hunter2"]         # only an MCP call's ARGUMENTS carry it
        return t

    vc = bugs.exploration_verdict(claude, "m", task())
    vx = bugs.exploration_verdict(codex, "m", task())
    assert _scored(vc.metrics) == _scored(vx.metrics) and vc.criteria == vx.criteria
    assert vx.metrics["mcp_tool_calls"] == 4
    banked, _ = bugs._bank_findings(codex, task().bug_spec["features"], "mcp")
    assert banked["login"]["probed"] is True and banked["login"]["at_call"] == 6


def test_the_same_disallowed_tools_value_withholds_the_same_tools(tmp_path):
    """QGB_DISALLOWED_TOOLS is prefixed for claude-code and bare for codex — both right,
    and now held to one env value: normalised, the two lists are the same tools."""
    tools = ["mobile_js_evaluate", "mobile_web_eval"]
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"device": {"url": "http://127.0.0.1:51821"}}}))
    cmd = ClaudeCodeAdapter().command("", _context(disabled_tools=tools))
    claude = cmd[cmd.index("--disallowedTools") + 1].split(",")
    toml = tomllib.loads(CodexCliAdapter()._config_toml(
        _context(agent="codex-cli", model="gpt-6-astra", disabled_tools=tools,
                 run_dir=tmp_path, mcp_config_path=mcp)))
    codex = [t for server in toml["mcp_servers"].values()
             for t in server.get("disabled_tools", [])]
    assert claude == [f"mcp__device__{t}" for t in tools]
    assert codex == tools
    assert [tool_base_name(t) for t in claude] == codex
