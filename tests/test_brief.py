"""The shared brief: one tooling note for both modes, the hierarchy path named in
the bare arm, and a version an episode carries so two regimes never blend.

QUA-2715. Brief v1 said only "use the tools available in your environment (for
example the `adb` command line)", which left HOW to read a screen to the agent:
codex-cli reaches for `uiautomator dump`, claude-code defaults to screenshots plus
guessed coordinates and burns the budget. That made the bare arm partly a measurement
of the agent's tooling instincts rather than of its QA. v2 names both ways, in the
same words for every agent.
"""

from __future__ import annotations

import re

from qualgentbench import brief
from qualgentbench.adb_meter import deny_reason
from qualgentbench.checkpoint import compatibility, environment_fingerprint
from qualgentbench.episode_runner import _ablation_instruction
from qualgentbench.interactions import OBSERVE, classify_adb_all
from qualgentbench.task import BenchmarkTask

DEVICE = "emulator-5554"


def _hunt_task() -> BenchmarkTask:
    return BenchmarkTask(
        id="t", name="t", instruction="Test the app.", app_file_id="",
        app_name="Demo", platform="android", bundle_id="com.example.demo",
        bug_spec={"features": [{"id": "login"}], "step_budget": 100},
    )


# ── one note, both modes ──────────────────────────────────────────────────────

def test_both_briefs_embed_the_same_tooling_note():
    """The note used to exist as two inline copies that happened to agree. One
    source, or the arms drift and nobody notices until a board is already published."""
    note = brief.tooling_note("raw", DEVICE)
    assert note in _ablation_instruction(_hunt_task(), DEVICE, "raw")

    from qualgentbench import journey
    spec = {"name": "Do a thing", "steps": ["Tap the button."],
            "expected_outcome": "It happened.", "case_id": "c", "app_id": "a"}
    task = BenchmarkTask(id="c~clean", name="c", instruction="", app_file_id="",
                         app_name="Demo", platform="android",
                         bundle_id="com.example.demo", bug_spec=spec)
    assert note in journey.brief(task, DEVICE, "raw")


def test_the_two_arms_still_differ_only_in_the_tooling_note():
    raw = _ablation_instruction(_hunt_task(), DEVICE, "raw")
    mcp = _ablation_instruction(_hunt_task(), DEVICE, "mcp")
    assert raw.replace(brief.tooling_note("raw", DEVICE), "<NOTE>") == \
        mcp.replace(brief.tooling_note("mcp", DEVICE), "<NOTE>")


# ── what v2 adds ──────────────────────────────────────────────────────────────

def test_the_bare_arm_brief_does_not_name_a_screen_reading_invocation():
    """v2 is withdrawn (QUA-2715) and must not come back by accident.

    It named exactly one way to read the screen — `adb shell uiautomator dump` into
    `/sdcard/window_dump.xml` — and on run 20260917-004716-9e69 that form was
    SIGKILLed 8/8 on emulator-5556. The agent obeyed literally, never tried another
    form, and paid four metered observations per episode for the failures. The 70
    codex episodes show why it was the wrong one to name: `exec-out uiautomator dump
    /dev/tty` is 85/86, while the shell→file form is 254/288 and carries every one of
    the corpus's 22 exit-137s.

    So the note names no invocation at all, which is also what makes this trial
    comparable with those 70 episodes. A future note that names the WORKING form
    would be a new version with its own derivation — not a revival of this text.
    """
    note = brief.tooling_note("raw", DEVICE).lower()
    for named in ("uiautomator", "window_dump.xml", "screencap", "/dev/tty"):
        assert named not in note, f"the note names {named!r} — that is v2, which was withdrawn"


def test_the_note_is_agent_neutral():
    """Every agent gets the same words. A note that named one would make the arms
    incomparable in exactly the way this change exists to prevent."""
    for tooling in ("raw", "mcp"):
        low = brief.tooling_note(tooling, DEVICE).lower()
        for name in ("claude", "codex", "anthropic", "openai", "gpt", "sonnet", "opus"):
            assert name not in low, f"{tooling}: the note names {name!r}"


def test_the_note_hints_at_nothing_but_the_tools():
    """No app, no route, no suggestion that anything is wrong — the note is about the
    inspection tool and nothing else. `_BIASING_PHRASES` mirrors test_ablation.py."""
    biasing = ("find bug", "find the bug", "find as many", "hidden", "seeded",
               "defect", "something is wrong", "not been told", "is broken",
               "are broken", "issue", "bug", "wrong", "fail", "expect")
    for tooling in ("raw", "mcp"):
        low = brief.tooling_note(tooling, DEVICE).lower()
        for phrase in biasing:
            assert phrase not in low, f"{tooling}: biasing phrase {phrase!r}"


def test_the_brief_does_not_send_the_agent_at_a_denied_command():
    """Every adb command the note names must be one the meter RELAYS. The answer-key
    deny list (`run-as`, the app sandbox, `qgb_flags`) answers FAIL at the socket, so
    a brief that named one would be instructing agents to hit a wall — and would show
    up on a board as `metered_denied` the agent never chose to earn."""
    note = brief.tooling_note("raw", DEVICE)
    commands = [c for c in re.findall(r"`([^`]+)`", note) if c.startswith("adb ")]
    # v1 names the `adb` CLI and no specific command, so this list is legitimately
    # empty and the invariant holds vacuously. It is kept armed for whatever a
    # future version names — that is the moment it has something to catch.
    for command in commands:
        body = command.split(" ", 2)[2] if command.startswith("adb exec-out ") else \
            command.split(" ", 2)[2] if command.startswith("adb shell ") else None
        if body is None:
            continue
        service = ("exec:" if command.startswith("adb exec-out ") else "shell:") + body
        assert deny_reason(service) is None, f"{command!r} is denied at the meter"


def test_reading_the_screen_costs_the_same_either_way():
    """Naming the hierarchy path adds an affordance, not a discount: `screencap` and
    `uiautomator dump` are both one `observe`. If that stopped being true the note
    would be quietly steering the budget."""
    assert classify_adb_all("shell:screencap -p") == [OBSERVE]
    assert classify_adb_all("shell:uiautomator dump /sdcard/window_dump.xml") == [OBSERVE]


# ── the version, and where it is legible ──────────────────────────────────────

def test_the_version_tracks_the_text_exactly():
    """The version names a TREATMENT, not a revision count. v2's text was withdrawn
    and v1's restored byte-for-byte. v3 (QUA-2777) changes the MCP arm's note only —
    the bare arm's is still v1's, byte-for-byte, so the 70 codex episodes remain the
    same TEXT as a v3 bare-arm run even though the run-wide stamp now differs."""
    assert brief.BRIEF_VERSION == 3
    assert brief.tooling_note("raw", DEVICE) == (
        "Use the tools available in your environment to operate the device "
        "(for example the `adb` command line)."
    )
    assert brief.tooling_note("mcp", DEVICE) == (
        "MCP tools are available for device control. Every tool takes the "
        'device as its first argument — always pass device="emulator-5554". '
        "The `findings.yaml` file described below is the report of record: a structured "
        "result tool the server may offer is optional and does not replace it."
    )


# ── what v3 adds (QUA-2777) ───────────────────────────────────────────────────

def test_the_mcp_note_names_the_report_of_record_and_no_tool():
    """DevLoop's server instructions end every run with its own result tool; the brief's
    contract is `findings.yaml`. The note settles which one the benchmark reads without
    naming any server's tool — a named tool coaches one server's users, and tool
    schemas already reach the agent through the handshake."""
    from qualgentbench.submission import FILENAME
    from qualgentbench.interactions import MCP_TOOL_RULES

    note = brief.tooling_note("mcp", DEVICE)
    assert f"`{FILENAME}`" in note and "report of record" in note
    assert "does not replace it" in note
    low = note.lower()
    for tool in MCP_TOOL_RULES:
        assert tool not in low, f"the note names the tool {tool!r}"
    assert "mobile_" not in low and "qg_" not in low and "devloop" not in low
    # The bare arm has no server and no result tool: its note is untouched.
    assert "report of record" not in brief.tooling_note("raw", DEVICE)


def test_the_journey_briefs_differ_only_in_the_tooling_note():
    """The journey brief's twin of the hunt test above: the v3 sentence lives in the
    note and nowhere else, so the task text an agent reads is the same on both arms."""
    from qualgentbench import journey
    spec = {"name": "Do a thing", "steps": ["Tap the button."],
            "expected_outcome": "It happened.", "case_id": "c", "app_id": "a"}
    task = BenchmarkTask(id="c~clean", name="c", instruction="", app_file_id="",
                         app_name="Demo", platform="android",
                         bundle_id="com.example.demo", bug_spec=spec)
    raw = journey.brief(task, DEVICE, "raw")
    mcp = journey.brief(task, DEVICE, "mcp")
    assert brief.tooling_note("mcp", DEVICE) in mcp
    assert raw.replace(brief.tooling_note("raw", DEVICE), "<NOTE>") == \
        mcp.replace(brief.tooling_note("mcp", DEVICE), "<NOTE>")
    # "described below" must be true: the file the note names is the one the brief's
    # report section describes, after the note.
    assert mcp.index("report of record") < mcp.index("## How to report")


def test_the_plan_records_which_brief_a_run_was_measured_under():
    fingerprint = environment_fingerprint([])
    assert fingerprint["brief_version"] == brief.BRIEF_VERSION


def test_a_plan_from_before_the_change_cannot_be_resumed_silently():
    """A resume across a brief change is a blended measurement. `compatibility` is
    what refuses it, and it has to NAME the brief — a generic mismatch sends the
    reader looking at the APK hashes."""
    now = environment_fingerprint([])
    before = {k: v for k, v in now.items() if k != "brief_version"}
    diffs = compatibility(before, now)
    assert any("brief_version" in d for d in diffs), diffs
    assert compatibility(now, now) == []


def test_every_episode_says_which_brief_it_ran_under(tmp_path, monkeypatch):
    """provenance, not just the plan: a runs/ tree gets copied, blended and rescored,
    and a board reads result.json, never the plan beside it."""
    import asyncio

    from qualgentbench import episode_runner
    from qualgentbench.episode_runner import EpisodeOptions, _provenance

    async def _no_avd(serial):
        return None

    monkeypatch.setattr(episode_runner, "_avd_name", _no_avd)
    opts = EpisodeOptions(agent="claude-code", model="claude-opus-5", condition="raw",
                          trial=1, mcp_server=None, runs_dir=tmp_path)
    provenance = asyncio.run(_provenance(opts, "emulator-5554"))
    assert provenance["brief_version"] == brief.BRIEF_VERSION
