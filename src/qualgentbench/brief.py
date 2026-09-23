"""The agent's assignment, in the parts that are shared across modes and arms.

Both briefs — hunt (`episode_runner._ablation_instruction`) and journey
(`journey.brief`) — are byte-identical across arms except for ONE paragraph, the
tooling note. It lived inline in both, in two copies that happened to agree; it lives
here so there is one text to change and one version to stamp.
"""

from __future__ import annotations

from .submission import FILENAME as REPORT_FILE

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
#   2  (QUA-2715) named the hierarchy-inspection path explicitly. WITHDRAWN on
#      2026-09-17 after one run, before any board used it. The text named exactly one
#      invocation — `adb shell uiautomator dump` writing `/sdcard/window_dump.xml`,
#      read back with `cat` — and on run 20260917-004716-9e69 that form was SIGKILLed
#      (`exit=137`) on 8 of 8 attempts across both arms of `cal-switch-back-to-list`
#      on emulator-5556. The agent obeyed the brief literally, never tried another
#      form, paid four metered observations per episode for the failures, and fell
#      back to screenshots anyway.
#
#      The 70 codex episodes on disk say why, and correct the premise v2 was written
#      on. Codex uses the hierarchy path heavily and successfully — 374 invocations
#      with an exit code, 339 of them exit 0 (90.6%) — so there was no
#      "tool-discovery gap" to close. But it uses BOTH forms, and they are not equally
#      reliable: `exec-out uiautomator dump /dev/tty` is 85/86 (98.8%), while the
#      `shell` → file form v2 named is 254/288 (88%) with all 22 of the corpus's
#      exit-137s in it. v2 therefore recommended the weaker of the two invocations and
#      never mentioned the stronger one.
#
#      Reverted to v1's text rather than re-worded, so this trial stays directly
#      comparable with those 70 episodes — worth more than a tidy version number. A
#      future note that names `exec-out … /dev/tty` would be a fresh version and needs
#      its own derivation; do not revive this one.
#
#   3  (QUA-2777) the MCP arm's note gains one sentence: `findings.yaml` is the report
#      of record, and a structured result tool the server offers is optional and does
#      not replace it. The bare arm's note is v1's, byte-for-byte. Why: DevLoop-MCP's
#      own server instructions tell the agent to END every run with its
#      `mobile_report_result` tool (and to file bugs through QualGent's bug tools),
#      while the brief's contract is `findings.yaml` + a `RESULT:` line — two prompt
#      authorities with conflicting completion steps, and a DevLoop agent obeying its
#      server's instructions could leave the scorer with nothing to read. The scorer now
#      also reads that tool as the lowest-precedence report source (`journey.
#      report_from_tool`), so an agent that uses it is not lost; the sentence says which
#      one wins. It names no tool, per the rule below. The note is shared, so the hunt
#      brief's MCP arm carries the same sentence (it reports through the same file).
#      v1 and v3 episodes on the MCP arm are different treatments and do not blend; on
#      the bare arm the text is unchanged, but the version is the regime of the whole
#      run, so a board still keeps them apart rather than guessing per arm.
BRIEF_VERSION = 3

# The bare arm's note. Agent-neutral by construction: it names adb commands, never an
# agent, and it says nothing about the app under test, the route or what might be
# wrong with either. Keep it that way — anything app-specific here is a hint, and
# anything agent-specific makes the arms measure different things.
#
# This is v1's text, restored byte-for-byte (see BRIEF_VERSION above for why v2 was
# withdrawn). It says nothing about HOW to read a screen: `screencap` and
# `uiautomator dump` both classify as one `observe` (`interactions._ADB_RULES`) and
# neither is on `adb_meter.deny_reason`'s list, so the choice costs the agent nothing
# either way and the benchmark does not steer it.
_RAW_NOTE = (
    "Use the tools available in your environment to operate the device "
    "(for example the `adb` command line)."
)

# The MCP arm's note. Mechanics only, no verification guidance — the raw arm gets
# none, and coaching one arm to check its work breaks the ablation. It names no tools
# on purpose: their schemas reach the agent through the server, so listing them here
# would duplicate the handshake rather than add anything.
# The standalone server has no device-lock tools, so every call carries the device.
# v3 (QUA-2777) adds the second sentence: which report the benchmark reads when the
# server offers its own result tool. Agent-neutral and tool-neutral — "a structured
# result tool" describes any server's, and naming DevLoop's would coach one server's
# users. `{report_file}` is `submission.FILENAME`, the file both briefs describe below.
_MCP_NOTE = (
    "MCP tools are available for device control. Every tool takes the "
    'device as its first argument — always pass device="{device_serial}". '
    "The `{report_file}` file described below is the report of record: a structured "
    "result tool the server may offer is optional and does not replace it."
)


def tooling_note(tooling: str, device_serial: str) -> str:
    """The one paragraph a brief differs by between arms.

    ``tooling`` is the arm as the specs spell it: ``"raw"`` for the bare agent
    driving adb, anything else for the MCP arm.
    """
    if tooling == "raw":
        return _RAW_NOTE
    return _MCP_NOTE.format(device_serial=device_serial, report_file=REPORT_FILE)
