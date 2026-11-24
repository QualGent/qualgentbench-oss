"""QUA-3605: a line the HARNESS wrote into a tool result is not device text.

A tool result reaches the scorer as text, and not all of that text came off the device.
The agent harness writes a line beside every screenshot it records,

    [Image: source: <episode dir>/…/tool-results/<file>.jpg, original 1080x2400, …]

and the episode directory holds the case id, the agent and the model. Journey grounding
(`bug.grounded`: "the DEVICE answered with this quote") read that line with the rest of
the result, so a report quote equal to any piece of the path — a word of the case id, or
`work` out of `…-workspace` — counted as shown by the device, and on a case where that
word is an `echo_texts` string it earned the blocking bug with the app never looked at.

`journey._harness_free` drops each harness-written shape (`_HARNESS_LINES` below is the
inventory, one example of each as saved transcripts carry it) before any journey matcher
reads a device result: report grounding, the screen witness and the present-oracle evidence.
Each test holds the counterfactual next to the guard — with the rule off the same
transcript grounds and earns — so the 0 is the rule's doing and not a dead attack.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from qualgentbench import journey

from test_bugs import _transcript
from test_journey import _bug, _real, _write

VAGUE = "the screen did not look the way the test case describes"
#: The directory the harness gives one episode: `<runs>/<case>/<stamp>_<case>_<agent>_
#: <model>_<arm>_trial-N_ep-<id>`. The agent and model here name no real one.
AGENT, MODEL = "agent-cli", "model-x1"
LISTING = json.dumps({"elements": [{"text": "Lorem ipsum dolor", "x": 0, "y": 0}]})


def _episode_dir(case: str, arm: str = "mcp") -> str:
    ep = f"2026-01-01T00-00-00Z_{case}_{AGENT}_{MODEL}_{arm}_trial-1_ep-0123456789ab"
    return f"/srv/bench/runs/{case}/{ep}"


def _image_line(case: str, resized: bool = True) -> str:
    """The line the agent harness writes beside a screenshot, in both shapes it has: with
    the resize note (the image was scaled for the model) and without."""
    d = _episode_dir(case)
    path = (f"{d}/agent_home/projects/{d.replace('/', '-').replace('_', '-')}-workspace/"
            "11111111-2222-3333-4444-555555555555/tool-results/mcp-device-blob-1-abc.jpg")
    note = (", original 1080x2400, displayed at 900x2000. Multiply coordinates by 1.20 to "
            "map to original image." if resized else "")
    return f"[Image: source: {path}{note}]"


def _mcp(tool: str, blocks: list[dict] | str, cid: str = "r0", is_error: bool = False) -> str:
    """One MCP call and its reply in claude-code stream-json, the reply as content
    blocks (a screenshot is `[image, text(harness line), text(listing)]`) or a string."""
    return "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cid, "name": f"mcp__device__{tool}",
             "input": {"device": "d"}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": cid, "is_error": is_error,
             "content": blocks}]}})])


def _screenshot_read(case: str, listing: str = LISTING, resized: bool = True,
                     cid: str = "r0") -> str:
    return _mcp("mobile_observe_screen", [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": "AAAA"}},
        {"type": "text", "text": _image_line(case, resized)},
        {"type": "text", "text": listing}], cid)


def _bash(command: str, output: str, cid: str = "b0") -> str:
    """One shell call and its output on the bare-adb arm, claude-code stream-json."""
    return "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cid, "name": "Bash", "input": {"command": command}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": cid, "content": output}]}})])


def _codex_shell(command: str, output: str) -> str:
    return json.dumps({"type": "item.completed", "item": {
        "id": "c0", "type": "command_execution", "command": command,
        "aggregated_output": output, "exit_code": 0, "status": "completed"}})


@pytest.fixture
def rule_off(monkeypatch):
    """The scorer before QUA-3605: every line of a device result is device text."""
    def off() -> None:
        monkeypatch.setattr(journey, "_harness_free", lambda payload, tooling="mcp": payload)
    return off


# ── the defect: a piece of the screenshot path grounds a quote ───────────────────

@pytest.mark.parametrize("app, tid, word", [
    # `Task` is a word of the case id, and an `echo_texts` string of the case.
    ("fossify-calendar", "cal-open-task-from-list~seeded", "Task"),
    # `Work` is in no case id: it is a substring of the `…-workspace` directory every
    # episode path ends in (and `Home` of the agent's home directory beside it).
    ("simpletimetracker", "stt-delete-second-category~seeded", "Work"),
    ("simpletimetracker", "stt-delete-second-category~seeded", "Home"),
])
@pytest.mark.parametrize("resized", [True, False])
def test_a_piece_of_the_screenshot_path_grounds_nothing(app, tid, word, resized, rule_off):
    """The agent takes one screenshot of a screen that shows none of it, then quotes a
    piece of its own episode path with vague prose. Nothing: not grounded, no bug. The
    same quote read off the SCREEN still earns the bug. With the rule off the identical
    transcript grounds and earns it, which is the defect."""
    task = _real(app, tid)
    task.bug_spec["tooling"] = "mcp"
    case = task.bug_spec["case_id"]
    assert journey._evidence(word) in _image_line(case, resized).lower()
    assert word in task.bug_spec["echo_texts"]
    report = _write("fail", _bug(1, word, VAGUE))
    blind = _transcript(_screenshot_read(case, resized=resized), report)

    guess = journey.journey_verdict(blind, "m", task)
    assert guess.metrics["grounded_reports"] == 0
    assert guess.metrics["bugs_found"] == []
    assert guess.metrics["false_reports"] == 1

    shown = json.dumps({"elements": [{"text": word, "x": 0, "y": 0}]})
    honest = journey.journey_verdict(
        _transcript(_screenshot_read(case, shown, resized), report), "m", task)
    assert honest.metrics["grounded_reports"] == 1
    assert honest.metrics["bugs_found"] == [task.bug_spec["blocking"]]

    rule_off()
    unguarded = journey.journey_verdict(blind, "m", task)
    assert unguarded.metrics["grounded_reports"] == 1, "the attack is dead"
    assert unguarded.metrics["bugs_found"] == [task.bug_spec["blocking"]]


def test_the_screenshot_line_witnesses_nothing_and_proves_no_outcome(rule_off):
    """The same line reached the screen witness and the present-oracle evidence, both of
    which read device results too. A witness equal to a word of the case id was `seen`
    in a screenshot of anything."""
    case = "notes-open-archive"
    spec = {"oracle": {"mode": "present", "evidence": ["archive"], "witness": ["Archive"]}}
    t = _transcript(_screenshot_read(case))
    assert journey._witness(spec, journey._observation_texts(t, "mcp"))["seen"] == []
    assert journey._oracle_verdict(spec, journey._device_texts(t, "mcp"))[0] is False
    shown = _transcript(_screenshot_read(
        case, json.dumps({"elements": [{"text": "Archive", "x": 0, "y": 0}]})))
    assert journey._witness(spec, journey._observation_texts(shown, "mcp"))["seen"] == ["Archive"]
    assert journey._oracle_verdict(spec, journey._device_texts(shown, "mcp"))[0] is True

    rule_off()
    assert journey._witness(spec, journey._observation_texts(t, "mcp"))["seen"] == ["Archive"]
    assert journey._oracle_verdict(spec, journey._device_texts(t, "mcp"))[0] is True


def test_a_read_that_returned_only_a_screenshot_is_not_a_text_observation():
    """An observe that came back as an image and the harness's line about it carried no
    screen TEXT. It must leave the agent where a screenshot-only agent has always been
    (no device text to hold a witness against), not turn the path into its screen."""
    only_image = _mcp("mobile_observe_screen", [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": "AAAA"}},
        {"type": "text", "text": _image_line("notes-open-archive")}])
    t = _transcript(only_image)
    assert journey._observation_texts(t, "mcp") == []
    assert journey._observation_texts(t, "mcp", screen_only=True, fold=False) == []


# ── the inventory: every harness-written shape, and what must stay ────────────────

_D = _episode_dir("notes-open-archive", "raw")
_UI = 'text="Archive" resource-id="com.example:id/title" bounds="[0,0][10,10]"'
#: (kind, tooling, transcript, the device text that must survive). One real example of
#: each shape, as saved transcripts carry it, with a made-up episode directory.
_HARNESS_LINES = [
    ("image source line, resized", "mcp",
     _screenshot_read("notes-open-archive", LISTING, True), "lorem ipsum dolor"),
    ("image source line, bare", "mcp",
     _screenshot_read("notes-open-archive", LISTING, False), "lorem ipsum dolor"),
    # The budget hook refused the call before it reached the device; the harness's
    # report of that names the hook file inside the episode directory.
    ("pre-tool hook refusal", "mcp",
     _mcp("mobile_observe_screen",
          f"PreToolUse:mcp__device__mobile_observe_screen hook error: [{_D}/hooks/"
          "tool_cap.py]: tool-call budget (40) exhausted - episode terminated.\n",
          is_error=True), None),
    ("pre-tool hook refusal, bare arm", "raw",
     _bash("adb shell uiautomator dump",
           f"PreToolUse:Bash hook error: [{_D}/hooks/tool_cap.py]: tool-call budget (40) "
           "exhausted - episode terminated.\n"), None),
    ("shell cwd notice", "raw",
     _bash("cd /tmp && adb exec-out uiautomator dump /dev/tty",
           f"{_UI}\nShell cwd was reset to {_D}/workspace"), 'text="archive"'),
    ("empty-output notice", "raw",
     _bash("adb shell input tap 1 1", "(Bash completed with no output)"), None),
    ("host adb client log line", "raw",
     _codex_shell("adb exec-out uiautomator dump /dev/tty",
                  f"11-10 10:26:53.205 12160 2093033 F adb     : adb_utils.cpp:316 cannot "
                  f"mkdir '{_D}/agent_home/home/.android': File exists\n{_UI}"),
     'text="archive"'),
]


@pytest.mark.parametrize("kind, tooling, lines, kept", _HARNESS_LINES,
                         ids=[k for k, *_ in _HARNESS_LINES])
def test_no_harness_line_is_device_text(kind, tooling, lines, kept, rule_off):
    """Nothing of the episode directory — case id, agent, model, the directory names
    around them — and nothing of the harness's own wording is left in any list a journey
    matcher reads; the device text beside it is."""
    t = _transcript(lines)
    harness_words = ("notes-open-archive", AGENT, MODEL, "workspace", "tool_cap", "runs",
                     "image", "hook", "cwd", "completed", "adb_utils", "mkdir")

    def texts():
        return (journey._device_texts(t, tooling, results_only=True)
                + [r for r in journey._device_texts(t, tooling)
                   if not r.startswith(("bash ", "mobile_", "adb "))]
                + journey._observation_texts(t, tooling))

    got = texts()
    for w in harness_words:
        assert not any(w in x for x in got), f"{kind}: {w!r} still read as device text"
    if kept is None:
        assert not any(x.strip() for x in got), f"{kind}: {got}"
    else:
        assert any(kept in x for x in journey._device_texts(t, tooling, results_only=True))

    rule_off()
    assert any(w in x for w in harness_words for x in texts()), f"{kind}: nothing to guard"


@pytest.mark.parametrize("tooling, shown", [
    # An app's own text that only resembles the harness's line.
    ("mcp", json.dumps({"elements": [{"text": "Click on [image] to add new record"}]})),
    ("mcp", json.dumps({"elements": [{"text": "[Image: source: /sdcard/DCIM/a.jpg]"}]})),
    ("mcp", json.dumps({"elements": [{"text": "Image source"}, {"text": "Home"},
                                     {"text": "Work"}]})),
    ("mcp", json.dumps({"elements": [{"text": "PreToolUse: hook error"}]})),
    # DevLoop's crash notice comes from the device's own log: it stays whole.
    ("mcp", ("APP EVENT [c-12345678]: com.example.debug (inferred app under test) crashed "
             "at 15:01:40Z (java.lang.IndexOutOfBoundsException).")),
    # The device's own shell output and log, tags other than the host client's.
    ("raw", 'text="Shell cwd was reset to /tmp" bounds="[0,0][1,1]"'),
    ("raw", "11-10 10:26:53.205 12160 12160 E adbd    : failed to connect"),
    ("raw", "11-10 10:26:53.205 12160 12160 E AndroidRuntime: java.lang.IllegalStateException"),
    ("raw", "UI hierchary dumped to: /sdcard/window_dump.xml"),
    ("raw", "bash completed with no output"),
])
def test_text_the_device_showed_stays(tooling, shown):
    """The rule removes the harness's shapes and nothing else: a device result that
    carries none of them reaches every matcher exactly as it did before."""
    assert journey._harness_free(shown.lower(), tooling) == shown.lower()
    t = _transcript(_mcp("mobile_observe_screen", [{"type": "text", "text": shown}])
                    if tooling == "mcp" else _bash("adb shell uiautomator dump", shown))
    assert journey._device_texts(t, tooling, results_only=True) == [journey._device_text(shown)]


def test_both_transcript_formats_read_the_same_screenshot():
    """codex records no line beside a screenshot; claude-code does. With the line gone
    the same screen read grounds the same text in both formats."""
    codex = json.dumps({"type": "item.completed", "item": {
        "id": "r0", "type": "mcp_tool_call", "server": "device",
        "tool": "mobile_observe_screen", "arguments": {"device": "d"}, "error": None,
        "status": "completed", "result": {"content": [
            {"type": "image", "data": "AAAA", "mimeType": "image/jpeg"},
            {"type": "text", "text": LISTING}], "structured_content": None}}})
    claude = _screenshot_read("notes-open-archive")
    assert ([t.strip() for t in journey._device_texts(_transcript(claude), "mcp", results_only=True)]
            == [t.strip() for t in journey._device_texts(_transcript(codex), "mcp", results_only=True)])


# ── the gate ─────────────────────────────────────────────────────────────────────

def _gate():
    path = Path(__file__).parents[1] / "scripts" / "journey_adversary_check.py"
    spec = importlib.util.spec_from_file_location("journey_adversary_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_path_echo_is_a_standing_guesser_and_the_attack_is_live(rule_off):
    """`path-echo` is in the gate's roster and earns nothing on the cases it could reach;
    with the rule off it earns, so the gate's 0 is a guard and not a dead attack."""
    adv = _gate()
    assert "path-echo" in adv.GUESSERS
    reach = [t for app in ("fossify-calendar", "simpletimetracker", "markor", "catima")
             for t in adv._seeded_tasks([app])]
    for channel in adv.CHANNELS:
        assert sum(len(adv.run(t, "path-echo", channel)["bugs_found"]) for t in reach) == 0
    assert adv.path_echo_liveness(reach) > 0
    # Every piece of the line (`work` out of `workspace`), as a pair: the same quotes
    # with the line and without it are credited identically.
    assert adv.path_line_adds(reach) == []
    rule_off()
    assert sum(len(adv.run(t, "path-echo")["bugs_found"]) for t in reach) > 0
    assert adv.path_line_adds(reach) != []
