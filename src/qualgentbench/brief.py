"""The agent's assignment, in the parts that are shared across modes and arms.

Both briefs — hunt (`episode_runner._ablation_instruction`) and journey
(`journey.brief`) — are byte-identical across arms except for ONE paragraph, the
tooling note. It lived inline in both, in two copies that happened to agree; it lives
here so there is one text to change and one version to stamp.
"""

from __future__ import annotations

# Bumped when the text below changes what the brief AFFORDS the agent — i.e. when
# episodes from either side of the change stop being directly comparable. Stamped
# into every result.json (`provenance.brief_version`) and into plan.json's
# environment fingerprint, so a board can tell which regime an episode belongs to and
# a resume across the change is refused rather than silently blended.
#
#   1  "Use the tools available in your environment to operate the device (for
#      example the `adb` command line)." and nothing more. Every journey episode on
#      disk before 2026-09-16 — all 70 codex-cli ones — ran under this.
#
#   2  (QUA-2715) names the hierarchy-inspection path explicitly. Version 1 was
#      silent on HOW to read a screen, so which of the two ways an agent found was a
#      property of the AGENT rather than of the benchmark: codex-cli reaches for
#      `uiautomator dump` unprompted, claude-code defaults to a screenshot plus
#      guessed coordinates. Measured on run 20260916-234512-18ac, both arms of
#      `cal-switch-back-to-list`: 12 and 19 `screencap` calls against 3 `uiautomator`
#      each, and the whole budget gone at step 2 of a 10-step route. Under version 1
#      the bare arm was partly measuring "does this agent guess `uiautomator dump`",
#      which publishes as a capability gap it is not. Both ways are now named, in the
#      same words for every agent, and neither is recommended over the other.
BRIEF_VERSION = 2

# The bare arm's note. Agent-neutral by construction: it names adb commands, never an
# agent, and it says nothing about the app under test, the route or what might be
# wrong with either. Keep it that way — anything app-specific here is a hint, and
# anything agent-specific makes the arms measure different things.
#
# `screencap` and `uiautomator dump` both classify as one `observe`
# (`interactions._ADB_RULES`), so naming the second path adds an affordance, not a
# discount; neither is on `adb_meter.deny_reason`'s list.
_RAW_NOTE = (
    "Use the tools available in your environment to operate the device (for example "
    "the `adb` command line). There are two ways to read the current screen: capture "
    "it as an image (`adb exec-out screencap -p > screen.png`), or dump the view "
    "hierarchy as text (`adb shell uiautomator dump` writes `/sdcard/window_dump.xml`, "
    "which `adb shell cat /sdcard/window_dump.xml` reads back). The hierarchy is XML: "
    "one node per on-screen element, carrying its `text`, `content-desc`, "
    "`resource-id` and pixel `bounds`."
)

# The MCP arm's note. Mechanics only, no verification guidance — the raw arm gets
# none, and coaching one arm to check its work breaks the ablation. It names no tools
# on purpose: their schemas (the hierarchy readers included) reach the agent through
# the server, which is the affordance the raw arm lacked and `_RAW_NOTE` now supplies.
# The standalone server has no device-lock tools, so every call carries the device.
_MCP_NOTE = (
    "MCP tools are available for device control. Every tool takes the "
    'device as its first argument — always pass device="{device_serial}".'
)


def tooling_note(tooling: str, device_serial: str) -> str:
    """The one paragraph a brief differs by between arms.

    ``tooling`` is the arm as the specs spell it: ``"raw"`` for the bare agent
    driving adb, anything else for the MCP arm.
    """
    if tooling == "raw":
        return _RAW_NOTE
    return _MCP_NOTE.format(device_serial=device_serial)
