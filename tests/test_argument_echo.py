"""QUA-2817: every DevLoop reply that hands a caller argument back is an echo.

QUA-2805 marked the text-entry tools (`Set focused field to: '<text>'`). The QUA-2810
review found `mobile_open_url` still unmarked: its reply is `Opened URL: <url>`, so an
agent could "open" a brief noun as a URL and quote the acknowledgement as a device
sighting, grounding the `echo_texts` route. The audit found the same shape on every
non-read tool listed in `interactions.MCP_TOOL_RULES` with `echo=True`.

Each test holds the counterfactual next to the guard: with the flag OFF the same
transcript grounds and earns the bug, so the 0 is the flag's doing and not a dead attack.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path

import pytest

from qualgentbench import interactions as ix
from qualgentbench import journey

from test_bugs import _call, _obs, _transcript
from test_journey import _bug, _real, _write

# DevLoop-MCP's replies (src/devloop_mcp/tools/{input,devices,logs}.py on the epic
# branch), verbatim in shape. Each carries the caller's string back.
DEVLOOP_ARGUMENT_ECHOES = {
    "mobile_open_url": ("url", lambda t: (
        f"Opened URL: {t}\nNext: call mobile_observe_screen after the page/app loads.")),
    "mobile_launch_app": ("package_id", lambda t: (
        f"Launched {t}\nNext: call mobile_observe_screen to confirm the app actually "
        "opened — launches can silently fail.")),
    "mobile_terminate_app": ("package_id", lambda t: f"Terminated {t}"),
    "mobile_uninstall_app": ("package_id", lambda t: f"Uninstalled {t}: Success"),
    "mobile_swipe": ("direction", lambda t: (
        f"Error executing tool mobile_swipe: Unknown swipe direction: {t!r}. "
        "Use up, down, left, or right.")),
    "mobile_press_button": ("button", lambda t: (
        f"Error executing tool mobile_press_button: Unknown button: {t!r}. Supported: home, "
        "back, enter, volume_up, volume_down, app_switch, power")),
    "mobile_device_logs": ("package_id", lambda t: json.dumps({
        "source": "logcat", "matched": 0, "entries": [],
        "note": f"{t} is not running (pids: []), so entries were matched by name"})),
    "mobile_get_permissions": ("package_id", lambda t: json.dumps({
        "platform": "android", "package_id": t, "summary": {}})),
}


@pytest.fixture
def echo_off(monkeypatch):
    """Switch one tool's `echo` flag off — the scorer before QUA-2817."""
    def off(tool: str) -> None:
        rule = ix.MCP_TOOL_RULES[tool]
        monkeypatch.setitem(ix.MCP_TOOL_RULES, tool, dataclasses.replace(rule, echo=False))
    return off


def test_open_url_is_an_echo_and_still_a_charged_launch():
    rule = ix.MCP_TOOL_RULES["mobile_open_url"]
    assert rule.echo and rule.device and rule.reads is None
    assert rule.steps == (ix.LAUNCH,)
    assert ix.mcp_echoes_argument("mcp__device__mobile_open_url")


@pytest.mark.parametrize("tool", sorted(DEVLOOP_ARGUMENT_ECHOES))
def test_argument_echo_never_grounds_a_quote(tool, echo_off):
    """`contacts-delete~seeded`: `Alice` is an `echo_texts` string — on screen, but
    writable blind — so a GROUNDED quote of it earns the blocking bug. Feed it to the
    tool, read the acknowledgement back, quote it: nothing. Flip the flag off and the
    identical transcript grounds and earns the bug. A real screen read still grounds, so
    the honest agent that opens a URL and then LOOKS is untouched."""
    key, reply = DEVLOOP_ARGUMENT_ECHOES[tool]
    task = _real("fossify-contacts", "contacts-delete~seeded")
    task.bug_spec["tooling"] = "mcp"
    echoed = _call(f"mcp__device__{tool}", {"device": "d", key: "Alice"}, reply("Alice"))
    # Vague prose: the quote is the only thing that can earn the bug here.
    report = _write("fail", _bug(4, "Alice", "the screen did not look the way the test case describes"))
    blank = _obs("Contacts  Favorites")

    guess = journey.journey_verdict(_transcript(echoed, blank, report), "m", task)
    assert guess.metrics["grounded_reports"] == 0
    assert guess.metrics["bugs_found"] == []
    assert journey._device_texts(_transcript(echoed), "mcp", results_only=True) == []

    honest = journey.journey_verdict(_transcript(echoed, _obs("Alice  Contacts"), report), "m", task)
    assert honest.metrics["grounded_reports"] == 1
    assert honest.metrics["bugs_found"] == ["contact-delete-broken"]

    echo_off(tool)
    unguarded = journey.journey_verdict(_transcript(echoed, blank, report), "m", task)
    assert unguarded.metrics["grounded_reports"] == 1, f"{tool}: the attack is dead"
    assert unguarded.metrics["bugs_found"] == ["contact-delete-broken"]


def test_every_echo_is_a_non_read_device_tool():
    """A read's answer IS the screen, so a read is never an echo (dropping it would
    blind the honest agent); and an echo is always device work, never bookkeeping."""
    assert set(DEVLOOP_ARGUMENT_ECHOES) <= set(ix.MCP_ECHO_TOOLS)
    for name in ix.MCP_ECHO_TOOLS:
        rule = ix.MCP_TOOL_RULES[name]
        assert rule.device and rule.reads is None, name


def _adversary():
    path = Path(__file__).parents[1] / "scripts" / "journey_adversary_check.py"
    spec = importlib.util.spec_from_file_location("journey_adversary_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_type_then_quote_uses_open_url_and_every_roster_entry_is_live():
    """The gate's type-then-quote now also opens every string as a URL. Through
    `mobile_open_url` alone it earns 0; with open_url's flag off it earns > 0 — and so
    does every other roster entry, or its 0 in the gate would prove nothing."""
    mod = _adversary()
    assert "open_url" in mod.ECHO_NAMES
    tasks = mod._seeded_tasks(None)
    for name, tool, *_ in mod.ECHO_ROSTER:
        assert ix.mcp_echoes_argument(tool), f"{tool} is in the roster but not marked echo"
    for task in tasks:
        m = mod.run(task, "type-then-quote", roster=("open_url",))
        assert m["bugs_found"] == [] and m["completed"] is not True, task.id
    live = mod.echo_liveness(tasks)
    assert set(live) == set(mod.ECHO_NAMES)
    assert all(k > 0 for k in live.values()), live
    # The liveness probe restores the table it flipped.
    assert ix.mcp_echoes_argument("mobile_open_url")


# The residual the `echo` flag cannot reach (reported as a QUA-2817 follow-up). An ERROR
# reply repeats the argument on READ tools too, and a read cannot be marked `echo`
# without dropping the screen it answers with. It needs a scorer rule — a reply never
# grounds its own call's argument strings — in `journey._device_texts`, outside this
# ticket's files. strict: this starts failing (XPASS) the day that rule lands; delete the
# marker then.
# TODO(QUA-2817 follow-up): strip each MCP call's own string arguments from its reply
# before grounding, then drop this xfail.
@pytest.mark.xfail(strict=True, reason="error replies echo arguments; needs a scorer rule")
@pytest.mark.parametrize("tool,args,reply", [
    ("mobile_tap_and_observe", {"device": "d", "element_text": "Alice"},
     "Error executing tool mobile_tap_and_observe: Element 'Alice' not found. "
     "Visible: 'Contacts', 'Favorites'."),
    ("mobile_tap", {"device": "d", "x": "Alice", "y": 1},
     "Error executing tool mobile_tap: 1 validation error for mobile_tapArguments\nx\n"
     "  Input should be a valid integer, unable to parse string as an integer "
     "[type=int_parsing, input_value='Alice', input_type=str]"),
])
def test_an_error_reply_never_grounds_its_own_argument(tool, args, reply):
    task = _real("fossify-contacts", "contacts-delete~seeded")
    task.bug_spec["tooling"] = "mcp"
    report = _write("fail", _bug(4, "Alice", "the screen did not look the way the test case describes"))
    echoed = _call(f"mcp__device__{tool}", args, reply)
    v = journey.journey_verdict(_transcript(echoed, _obs("Contacts  Favorites"), report), "m", task)
    assert v.metrics["grounded_reports"] == 0
