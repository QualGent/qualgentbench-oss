"""QUA-2817: every DevLoop reply that hands a caller argument back is an echo.

QUA-2805 marked the text-entry tools (`Set focused field to: '<text>'`). The QUA-2810
review found `mobile_open_url` still unmarked: its reply is `Opened URL: <url>`, so an
agent could "open" a brief noun as a URL and quote the acknowledgement as a device
sighting, grounding the `echo_texts` route. The audit found the same shape on every
non-read tool listed in `interactions.MCP_TOOL_RULES` with `echo=True`.

QUA-2819 closes what the flag cannot reach: a REFUSED call's reply (DevLoop's tool errors
and FastMCP/pydantic's argument validation, which repeat the caller's argument on read
tools too) never grounds a quote or witnesses a case (`journey._refused_reply`), in both
agents' transcript formats, while a read that succeeded still grounds what it matched.

Each test holds the counterfactual next to the guard: with the flag (or the rule) OFF the
same transcript grounds and earns the bug, so the 0 is the guard's doing and not a dead
attack.
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
from test_journey import WITNESSED, _bug, _real, _spec, _task, _write

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
    # QUA-2819: perfetto starts whatever the package_id, and the summary carries it back
    # (native_profiler.TraceRun.summary); stop answers with the same summary.
    "mobile_native_profiler_start": ("package_id", lambda t: json.dumps({
        "trace_id": "native-trace-1", "platform": "android", "mode": "perfetto",
        "device": "d", "package_id": t, "pid": None, "output": "/tmp/t.pftrace",
        "notes": None, "next_action": "Reproduce the slow interaction now, then call "
        "mobile_native_profiler_stop with this trace_id."}, indent=2)),
}


@pytest.fixture
def echo_off(monkeypatch):
    """Switch one tool's `echo` flag off — the scorer before QUA-2817. The refused-reply
    rule (QUA-2819) goes off with it, so the flag is proven on its own: `mobile_swipe`'s
    and `mobile_press_button`'s echoes are refusals, which that rule also drops."""
    def off(tool: str) -> None:
        rule = ix.MCP_TOOL_RULES[tool]
        monkeypatch.setitem(ix.MCP_TOOL_RULES, tool, dataclasses.replace(rule, echo=False))
        monkeypatch.setattr(journey, "_refused_reply", lambda reply: False)
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


# ── QUA-2819: a REFUSED call's reply never grounds, whatever the tool ─────────────
#
# The residual the `echo` flag cannot reach. An error repeats the caller's argument on
# READ tools too, and a read cannot be marked `echo` without dropping the screen its
# success answers with; so the scorer drops the refusal itself (`journey._refused_reply`).
# Every shape below is DevLoop's or its framework's, verbatim: FastMCP wraps a tool's
# exception and pydantic's argument validation in `Error executing tool <name>: …`.

def _pydantic(tool: str, fields: dict, args: dict) -> str:
    """The real text FastMCP returns when pydantic refuses `args` (model `<tool>Arguments`)."""
    import pydantic
    model = pydantic.create_model(f"{tool}Arguments", **{k: (t, ...) for k, t in fields.items()})
    with pytest.raises(pydantic.ValidationError) as e:
        model.model_validate(args)
    return f"Error executing tool {tool}: {e.value}"


_COORDS = {"device": str, "x": int, "y": int}
_LONG = "Alice " + "and a great deal of padding so that pydantic has to cut it " * 3

REFUSALS = {
    # observe.py: the not-found error lists the screen but names the query first.
    "tap_not_found": ("mobile_tap_and_observe", {"device": "d", "element_text": "Alice"},
                      "Error executing tool mobile_tap_and_observe: Element 'Alice' not found. "
                      "Visible: 'Contacts', 'Favorites'. If the target is an unlabeled icon, "
                      "call mobile_observe_screen, read its coordinates from the screenshot, "
                      "then use x+y instead. Please fix the issue and try again."),
    # waits.py / native_views.py: an unknown enum value, quoted back.
    "await_unknown_match": ("mobile_await_element",
                            {"device": "d", "text": "Contacts", "match": "Alice"},
                            "Error executing tool mobile_await_element: Unknown match 'Alice'. "
                            "Use 'contains' or 'equals'. Please fix the issue and try again."),
    "hierarchy_unknown_format": ("mobile_native_hierarchy", {"device": "d", "format": "Alice"},
                                 "Error executing tool mobile_native_hierarchy: Unknown format "
                                 "'Alice'. Use 'outline' or 'json'. Please fix the issue and "
                                 "try again."),
    # session.py: a made-up device reaches adb, whose complaint names it — on ANY tool.
    "unknown_device": ("mobile_observe_screen", {"device": "Alice"},
                       "Error executing tool mobile_observe_screen: adb exec-out uiautomator dump "
                       "/dev/tty failed: adb: device 'Alice' not found"),
    # pydantic, through FastMCP: a mistyped argument, a missing one (the whole argument
    # dict is printed), and a long one (truncated in the MIDDLE, so an exact-string strip
    # of the argument would miss the prefix that still carries the quote).
    "int_parsing": ("mobile_tap", {"device": "d", "x": "Alice", "y": 1},
                    _pydantic("mobile_tap", _COORDS, {"device": "d", "x": "Alice", "y": 1})),
    "missing_field": ("mobile_hit_test", {"device": "d", "text": "Alice"},
                      _pydantic("mobile_hit_test", _COORDS, {"device": "d", "text": "Alice"})),
    "truncated_input": ("mobile_tap", {"device": "d", "x": _LONG, "y": 1},
                        _pydantic("mobile_tap", _COORDS, {"device": "d", "x": _LONG, "y": 1})),
    # A server that validates against the JSON schema answers before FastMCP does.
    "schema_validation": ("mobile_tap", {"device": "d", "x": "Alice", "y": 1},
                          "Input validation error: 'Alice' is not of type 'integer'"),
}


def _codex_call(tool: str, args: dict, reply: str, failed: bool) -> str:
    return json.dumps({"type": "item.completed", "item": {
        "id": f"item_{tool}", "type": "mcp_tool_call", "server": "device", "tool": tool,
        "arguments": args, "error": None, "status": "failed" if failed else "completed",
        "result": {"content": [{"type": "text", "text": reply}], "structured_content": None}}})


def _claude_call(tool: str, args: dict, reply: str, failed: bool) -> str:
    return "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"u_{tool}", "name": f"mcp__device__{tool}",
             "input": args}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"u_{tool}", "is_error": failed,
             "content": [{"type": "text", "text": reply}]}]}})])


_FORMATS = {"claude": _claude_call, "codex": _codex_call}
_VAGUE = "the screen did not look the way the test case describes"


def _findings(verdict: str, observed: str = "") -> str:
    return (f"verdict: {verdict}\nbugs:\n  - step: 4\n    observed: \"{observed}\"\n"
            f"    description: \"{_VAGUE}\"\n") if observed else f"verdict: {verdict}\nbugs: []\n"


def _episode(fmt: str, calls: list[tuple], task, findings: str):
    """The same MCP calls in one agent's transcript format, the report as the findings
    file (both agents' file of record), scored by the real `journey_verdict`."""
    task.bug_spec["tooling"] = "mcp"
    task.bug_spec["findings_file"] = findings
    lines = [_FORMATS[fmt](tool, args, reply, failed) for tool, args, reply, failed in calls]
    return journey.journey_verdict("\n".join(lines) + "\n", "m", task)


_BLANK = ("mobile_observe_screen", {"device": "d"}, "Contacts  Favorites", False)


@pytest.fixture
def rule_off(monkeypatch):
    """The scorer before QUA-2819: a refusal counted as device text."""
    return lambda: monkeypatch.setattr(journey, "_refused_reply", lambda reply: False)


@pytest.mark.parametrize("fmt", sorted(_FORMATS))
@pytest.mark.parametrize("shape", sorted(REFUSALS))
def test_a_refused_call_never_grounds_its_own_argument(shape, fmt, rule_off):
    """`contacts-delete~seeded`: `Alice` is an `echo_texts` string, so a GROUNDED quote
    of it earns the blocking bug. Hand it to a tool as an argument, quote the refusal:
    nothing, in both transcript formats. The counterfactual is held next to the guard —
    with the rule off the identical transcript grounds and earns the bug."""
    tool, args, reply = REFUSALS[shape]
    assert journey._refused_reply(reply.lower())
    assert not ix.mcp_echoes_argument(tool), f"{tool} is echo: the table would hide the rule"
    calls = [(tool, args, reply, True), _BLANK]
    task = _real("fossify-contacts", "contacts-delete~seeded")
    v = _episode(fmt, calls, task, _findings("fail", "Alice"))
    assert v.metrics["grounded_reports"] == 0 and v.metrics["bugs_found"] == []
    assert not any("alice" in t for t in journey._observation_texts(
        "\n".join(_FORMATS[fmt](*c) for c in calls), "mcp"))

    rule_off()
    task = _real("fossify-contacts", "contacts-delete~seeded")
    v = _episode(fmt, calls, task, _findings("fail", "Alice"))
    assert v.metrics["grounded_reports"] == 1, f"{shape}/{fmt}: the attack is dead"
    assert v.metrics["bugs_found"] == ["contact-delete-broken"]


@pytest.mark.parametrize("fmt", sorted(_FORMATS))
def test_a_refusal_is_dropped_by_its_text_not_by_the_agents_error_flag(fmt):
    """The rule reads the server's envelope, which both agents record verbatim; the
    flag differs (claude `is_error`, codex status `failed`) and a client may omit it.
    Unflagged, the refusal is still dropped."""
    tool, args, reply = REFUSALS["tap_not_found"]
    task = _real("fossify-contacts", "contacts-delete~seeded")
    v = _episode(fmt, [(tool, args, reply, False), _BLANK], task, _findings("fail", "Alice"))
    assert v.metrics["grounded_reports"] == 0 and v.metrics["bugs_found"] == []


@pytest.mark.parametrize("fmt", sorted(_FORMATS))
def test_a_successful_read_still_grounds_the_label_it_matched(fmt):
    """The honest side of the trade. DevLoop's SUCCESS names the element it matched on
    the device (`"tapped": "<label from the hierarchy>"`) and lists the new screen; a
    tap on `Alice` that worked, and a query that found it, ground a quote of Alice even
    though the argument was Alice too. Only the refusal is dropped."""
    tap_ok = json.dumps({"tapped": "Alice", "screen_changed": True, "hierarchy_changed": True,
                         "pixels_changed": True,
                         "elements": [{"text": "Alice", "x": 640, "y": 738, "tappable": True},
                                      {"text": "Contacts"}], "surface": "native"}, indent=2)
    found = json.dumps({"satisfied": True, "condition": "visible", "waited_ms": 12, "polls": 1,
                        "match": {"class": "android.widget.TextView", "text": "Alice"}})
    for tool, args, reply in (
            ("mobile_tap_and_observe", {"device": "d", "element_text": "Alice"}, tap_ok),
            ("mobile_await_element", {"device": "d", "text": "Alice"}, found)):
        task = _real("fossify-contacts", "contacts-delete~seeded")
        v = _episode(fmt, [(tool, args, reply, False)], task, _findings("fail", "Alice"))
        assert v.metrics["grounded_reports"] == 1, tool
        assert v.metrics["bugs_found"] == ["contact-delete-broken"], tool


@pytest.mark.parametrize("fmt", sorted(_FORMATS))
def test_a_refused_read_never_witnesses_its_own_argument(fmt, rule_off):
    """The screen witness is held to the same rule: a witness string handed to
    `mobile_tap_and_observe` as its target must not complete a read-only case through
    `Element '<witness>' not found`. The refusal also stops counting as a screen read,
    so an agent whose only text was refusals keeps the screenshot-only exemption (None,
    never a scored miss) rather than being failed on the refusal's `Visible:` list."""
    refusal = ("mobile_tap_and_observe", {"device": "d", "element_text": "Max: 85 kg"},
               "Error executing tool mobile_tap_and_observe: Element 'Max: 85 kg' not found. "
               "Visible: 'Weight', 'Min: 74 kg'. Please fix the issue and try again.", True)
    blank = ("mobile_observe_screen", {"device": "d"}, "Weight  Min: 74 kg  Avg: 79 kg", False)

    v = _episode(fmt, [refusal, blank], _task(_spec("clean", oracle=WITNESSED)), _findings("pass"))
    assert v.metrics["completed"] is False and v.metrics["witness"]["missing"] == ["Max: 85 kg"]
    # Refusals plus a screenshot-only read (no text came back): the exemption holds.
    shot = ("mobile_observe_screen", {"device": "d"}, "", False)
    v = _episode(fmt, [refusal, shot], _task(_spec("clean", oracle=WITNESSED)), _findings("pass"))
    assert v.metrics["completed"] is None and v.metrics["completion_scored"] is False

    rule_off()
    v = _episode(fmt, [refusal, blank], _task(_spec("clean", oracle=WITNESSED)), _findings("pass"))
    assert v.metrics["completed"] is True, "the witness attack is dead"


def test_argument_echo_guesser_earns_nothing_and_every_refusal_shape_is_live():
    """The gate's `argument-echo` guesser (both refusal channels, both transcript
    formats) earns 0 with the rule, and every roster entry earns > 0 in each format
    without it — or its 0 in the gate would prove nothing."""
    mod = _adversary()
    assert "argument-echo" in mod.GUESSERS
    assert {r[1] for r in mod.ARG_ECHO_ROSTER} == {"error", "validation"}
    for _name, _chan, tool, *_ in mod.ARG_ECHO_ROSTER:
        assert not ix.mcp_echoes_argument(tool), f"{tool}: the table, not the rule, guards it"
    tasks = mod._seeded_tasks(None)
    for task in tasks:
        for fmt in mod.FORMATS:
            m = mod.run(task, "argument-echo", fmt=fmt)
            assert m["bugs_found"] == [] and m["completed"] is not True, (task.id, fmt)
    live = mod.arg_echo_liveness(tasks)
    assert set(live) == {(n, f) for n in mod.ARG_ECHO_NAMES for f in mod.FORMATS}
    assert all(k > 0 for k in live.values()), live
    # The liveness probe restores the rule it switched off.
    assert journey._refused_reply("error executing tool mobile_tap: x")
