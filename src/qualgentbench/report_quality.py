"""Report quality for saved journey episodes: a block BESIDE the ranking score (QUA-3584).

The journey scorer (`journey.journey_verdict`) decides whether a report is a catch. It
says nothing about whether a developer could act on the report. This module scores that,
one yes/no gate at a time, and writes the answer to `quality.json` next to the episode's
`result.json`.

Three rules keep it apart from the ranking:

* It only READS `result.json` and the other saved artifacts (and, for gate G2, the
  corpus's case and truth files). It never writes `result.json` and never changes a
  verdict, a rate or a ranking key.
* The scoring modules never import it, so it is not in the pinned scorer closure
  (`tests/scorer_closure.py`) and `journey.SCORER_VERSION` does not move when it changes.
  It imports nothing from the package either: what it knows about a report is what the
  scorer already saved. (The one exception is on request only: `--quality-judge` attaches
  the G5 side column from saved judge results, through `quality_judge`.)
* It has its own version, `QUALITY_VERSION`, stamped into every `quality.json`.

A rescore is a pure function of the saved episode: no clock, no host name, no random
order. Scoring the same episode twice writes the same bytes.

    python -m qualgentbench.report_quality <run dir>

walks every episode under `<run dir>` and writes one `quality.json` per episode.
See docs/report-quality.md for the file format and the version rule.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The version of the quality rules, stamped into every `quality.json`. Bump it when a
#: change can alter the `quality.json` of an episode that a published board already
#: scored: a gate's rule, a gate added or removed, a change to who is counted. A change
#: that cannot alter any output (a refactor, a comment) keeps the version.
#:
#:   1  the frame, with gate G1 (valid); the customer-shaped report of every report.
QUALITY_VERSION = 1

RESULT_FILE = "result.json"
QUALITY_FILE = "quality.json"
#: The saved agent transcript, relative to the episode directory.
TRANSCRIPT_FILE = "agent/transcript.txt"

#: The gates a report must ALL pass to be customer-ready. A gate named here that is not
#: registered in `GATES` yet (or that did not run for a report) leaves `customer_ready`
#: unknown (null) for a report that failed nothing, never true.
HEADLINE_GATES = ("G1", "G2", "G3", "G4")


class QualityError(ValueError):
    """This directory is not an episode this module can score. The message says why."""


@dataclass(frozen=True)
class GateResult:
    """One gate's answer for one report. `passed` None means the gate did not run for
    this report (for example a gate that only runs on valid reports); `reason` then says
    why. A gate that did not run is in no pass rate."""
    passed: bool | None
    reason: str
    #: The gate ran and could not decide (`passed` is then None). Counted on its own in
    #: the totals of a gate that declares `reports_unknown`; never a pass.
    unknown: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"pass": self.passed, "reason": self.reason}
        if self.unknown:
            out["unknown"] = True
        return out


@dataclass(frozen=True)
class Episode:
    """What a gate may read: the episode directory and its saved `result.json`."""
    dir: Path
    result: dict[str, Any]
    #: Per-episode memo (the customer-shaped reports), so a gate that asks for them once
    #: per report does not read the transcript once per report.
    _cache: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def metrics(self) -> dict[str, Any]:
        return self.result.get("metrics") or {}


#: A gate: (episode, the report as `result.json` saved it, its index) -> GateResult.
GateFn = Callable[[Episode, dict[str, Any], int], GateResult]


@dataclass(frozen=True)
class Gate:
    id: str            # "G1", "G2", ... the key in a report's `gates` map
    name: str          # one word, for tables: "valid", "located", ...
    fn: GateFn
    #: The gate can answer "unknown"; its totals then carry an `unknown` count.
    reports_unknown: bool = False


def is_neutral(report: dict[str, Any]) -> bool:
    """A neutral report is neither a find nor a false report: it matched no seeded defect
    and the scorer marked it a known clean-build behaviour or a downstream effect of a
    found defect. Neutral reports are listed in `quality.json` but pass through no gate
    and are in no total, the same way the scorer leaves them out of every rate."""
    return report.get("matched") is None and (
        report.get("known") is not None or report.get("consequence_of") is not None)


# ── The customer-shaped report ─────────────────────────────────────────────────
#
# Quality is scored on the report as a customer would read it, not on the findings file.
# The product files a bug from one accepted `mobile_report_result` call. Its mapping,
# mirrored here field for field:
#
#   call argument                       bug field
#   summary                             summary (the title)
#   code_investigation, else summary    steps
#   expected, actual                    expected / actual, kept only as a pair, each cut
#                                       to RESULT_FIELD_MAX characters
#   failure_step                        failure_step
#   steps (the agent's step list)       not on the bug (kept here as `agent_steps`)
#
# Only a FAIL call files a bug; PASS and BLOCKED file none. The same fixture file,
# tests/fixtures/report-mapping-fixture.json, is tested against this code and against
# the product's own mapping, so the two cannot drift silently.

REPORT_TOOL = "mobile_report_result"
#: The longest `expected` / `actual` the product stores, in characters (code points).
RESULT_FIELD_MAX = 250
#: The title the product gives a FAIL report that has no usable `summary`.
DEFAULT_SUMMARY = "Agent reported an app issue"
REDACTED = "[REDACTED]"

#: Where a customer-shaped report came from.
SOURCE_REPORT_TOOL = "report_tool"      # the last accepted FAIL `mobile_report_result` call
SOURCE_FINDINGS = "findings"            # a findings entry, rendered into the same fields
#: The scorer read its one report from a `mobile_report_result` call that files no bug in
#: the product (BLOCKED). The saved report is rendered like a findings entry.
SOURCE_SCORER_REPORT = "scorer_report"

# What JavaScript's `String.prototype.trim` removes; Python's `str.strip()` differs at the
# edges (it keeps U+FEFF and removes U+001C..U+001F).
_JS_SPACE = ("\t\n\v\f\r           "
             "       　﻿")

# The two redactions of the product's text sanitiser that a tester's sentence can
# plausibly trigger, in the product's order. The product has more (signed and local URLs,
# local file paths, image data, key-shaped tokens, values of sensitive arguments); a
# report containing such text can differ from the product's stored bug. See
# docs/report-quality.md, "Sanitising".
_AUTH_SCHEME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(api[_-]?key|access[_-]?token|authorization|client[_-]?secret"
    r"|cookie|id[_-]?token|refresh[_-]?token|session[_-]?token|oob[_-]?code"
    r"|[A-Za-z0-9_]+_token|token|password|passcode|pin|credential(?:[_-]?(?:id|uuid))?"
    r"|secret)\s*[:=]\s*(?:\[REDACTED\]|\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]{3,}")
_NUMBER_RE = re.compile(r"\d+")


def sanitize_text(value: str) -> str:
    """The product's sanitiser, as far as this module mirrors it: an `Authorization`
    scheme's credential and the value of a `password: ...`-style assignment become
    `[REDACTED]`."""
    value = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", value)


def _pick(args: dict[str, Any], key: str) -> str | None:
    """A string argument, trimmed; None when it is absent, not a string, or blank."""
    value = args.get(key)
    if not isinstance(value, str):
        return None
    return value.strip(_JS_SPACE) or None


def _result_pair(expected: str | None, actual: str | None) -> tuple[str | None, str | None]:
    """`expected` / `actual` as the bug stores them: both or neither, each cut to
    RESULT_FIELD_MAX code points with trailing space removed."""
    expected = (expected or "").strip(_JS_SPACE)
    actual = (actual or "").strip(_JS_SPACE)
    if not expected or not actual:
        return None, None
    return (expected[:RESULT_FIELD_MAX].rstrip(_JS_SPACE),
            actual[:RESULT_FIELD_MAX].rstrip(_JS_SPACE))


@dataclass(frozen=True)
class CustomerReport:
    """One report as the product would store it as a bug, plus where it came from."""
    index: int                    # position in result.json's `metrics.reports`
    summary: str                  # the bug's title
    steps: str                    # the bug's "steps" text; "" when the source has none
    expected: str | None          # both or neither, each at most RESULT_FIELD_MAX
    actual: str | None
    failure_step: str | None
    #: The agent's own `steps` argument, unchanged (a list; the product drops it).
    #: Empty for a findings entry. Not sanitised and not part of the bug.
    agent_steps: tuple[Any, ...] = ()
    report_source: str = SOURCE_FINDINGS
    #: How the tool call was matched to this report when there were several:
    #: "only_report", "step", "text" or "first". None for a rendered entry.
    paired_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "steps": self.steps, "expected": self.expected,
                "actual": self.actual, "failure_step": self.failure_step,
                "agent_steps": list(self.agent_steps),
                "report_source": self.report_source, "paired_by": self.paired_by}


def customer_report_from_tool(args: dict[str, Any], index: int = 0,
                              paired_by: str | None = None) -> CustomerReport | None:
    """The bug the product files from one accepted `mobile_report_result` call, or None
    when it files none (any status but FAIL). Pure: the arguments in, the fields out."""
    status = args.get("status")
    if not isinstance(status, str) or status.upper() != "FAIL":
        return None
    summary = sanitize_text(_pick(args, "summary") or DEFAULT_SUMMARY)
    steps = sanitize_text(_pick(args, "code_investigation") or summary)
    failure_step = sanitize_text(_pick(args, "failure_step") or "") or None
    expected, actual = (sanitize_text(v) if v else None
                        for v in (_pick(args, "expected"), _pick(args, "actual")))
    expected, actual = _result_pair(expected, actual)
    agent_steps = args.get("steps")
    return CustomerReport(
        index=index, summary=summary, steps=steps, expected=expected, actual=actual,
        failure_step=failure_step,
        agent_steps=tuple(agent_steps) if isinstance(agent_steps, list) else (),
        report_source=SOURCE_REPORT_TOOL, paired_by=paired_by)


def customer_report_from_finding(report: dict[str, Any], index: int,
                                 source: str = SOURCE_FINDINGS) -> CustomerReport:
    """One saved report (a findings entry, as `result.json` lists it) rendered into the
    bug's fields under the same rules: `description` is the summary, `observed` is
    `actual`, `screen` and `step` give `failure_step`. A findings entry carries no
    investigation and no step list, so `steps` is empty."""
    step, screen = report.get("step"), _pick(report, "screen")
    if isinstance(step, int) and not isinstance(step, bool):
        where = f"Step {step}: {screen}" if screen else f"Step {step}"
    else:
        where = screen or ""
    expected, actual = (sanitize_text(v) if v else None
                        for v in (_pick(report, "expected"), _pick(report, "observed")))
    expected, actual = _result_pair(expected, actual)
    return CustomerReport(
        index=index, summary=sanitize_text(_pick(report, "description") or DEFAULT_SUMMARY),
        steps="", expected=expected, actual=actual,
        failure_step=sanitize_text(where) or None, report_source=source)


def _base_tool_name(name: Any) -> str:
    name = name if isinstance(name, str) else ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return name


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def report_tool_calls(transcript: str) -> list[tuple[dict[str, Any], bool]]:
    """Every `mobile_report_result` call in a saved transcript, in order, as
    `(arguments, refused)`. Reads the two formats the harness records: claude-code
    stream-json (`tool_use` / `tool_result`, refused = `is_error`) and codex
    `exec --json` (`mcp_tool_call` items, refused = an `error` or status failed/error).
    The same calls, and the same refusal rule, as the scorer's own transcript parser;
    a test compares the two."""
    calls: dict[str, list[Any]] = {}      # call id -> [arguments, refused]
    anonymous = 0
    for line in transcript.splitlines():
        if REPORT_TOOL not in line and not any(cid in line for cid in calls):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if event.get("type") == "assistant" and isinstance(blocks, list):
            for block in blocks:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and isinstance(block.get("id"), str)
                        and _base_tool_name(block.get("name")) == REPORT_TOOL):
                    calls[block["id"]] = [_json_dict(block.get("input")), False]
        elif event.get("type") == "user" and isinstance(blocks, list):
            for block in blocks:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and block.get("tool_use_id") in calls):
                    calls[block["tool_use_id"]][1] = bool(block.get("is_error"))
        else:
            item = event.get("item")
            if not (isinstance(item, dict) and item.get("type") == "mcp_tool_call"
                    and _base_tool_name(item.get("tool")) == REPORT_TOOL):
                continue
            cid = item.get("id")
            if not isinstance(cid, str):
                cid = f"anonymous-{anonymous}"
                anonymous += 1
            call = calls.setdefault(cid, [_json_dict(item.get("arguments")), False])
            status = item.get("status")
            status = status.lower() if isinstance(status, str) else ""
            if status != "in_progress" and ("result" in item or item.get("error")):
                call[1] = bool(item.get("error")) or status in {"failed", "error"}
    return [(args, refused) for args, refused in calls.values()]


def last_accepted_report_call(episode: Episode) -> dict[str, Any] | None:
    """The arguments of the last `mobile_report_result` call the server accepted in the
    episode's saved transcript, or None (no such call, or no transcript)."""
    try:
        transcript = (episode.dir / TRANSCRIPT_FILE).read_text(encoding="utf-8",
                                                               errors="replace")
    except OSError:
        return None
    accepted = [args for args, refused in report_tool_calls(transcript) if not refused]
    return accepted[-1] if accepted else None


def _words(*texts: Any) -> set[str]:
    return set(_WORD_RE.findall(" ".join(t for t in texts if isinstance(t, str)).lower()))


def _pair_tool_call(args: dict[str, Any],
                    reports: list[dict[str, Any]]) -> tuple[int, str] | None:
    """Which saved report the one tool call describes: `(index, how)`. One call carries
    one bug, so exactly one report takes its fields. In order: the only report; the
    report whose `step` is the first number in `failure_step`; the report sharing the
    most words with the call; the first report. Ties go to the lower index."""
    if not reports:
        return None
    if len(reports) == 1:
        return 0, "only_report"
    candidates, how = list(range(len(reports))), "first"
    number = _NUMBER_RE.search(_pick(args, "failure_step") or "")
    if number:
        same_step = [i for i in candidates if reports[i].get("step") == int(number.group(0))]
        if same_step:
            candidates, how = same_step, "step"
            if len(same_step) == 1:
                return same_step[0], how
    call_words = _words(*(args.get(k) for k in ("summary", "actual", "expected",
                                                "failure_step")))
    overlap = {i: len(call_words & _words(*(reports[i].get(k) for k in (
        "description", "observed", "expected", "screen")))) for i in candidates}
    best = max(candidates, key=lambda i: (overlap[i], -i))
    if how == "first" and overlap[best] > 0:
        how = "text"
    return best, how


def customer_report(episode: Episode) -> list[CustomerReport]:
    """The customer-shaped report of every report in the episode, in the order of
    `metrics.reports` (entry i belongs to report i).

    Source, in order: the last accepted `mobile_report_result` call in the transcript,
    when it is a FAIL (the only status that files a bug); else each saved report
    rendered into the same fields. One call carries one bug, so when the episode has
    more reports than that, the others use the rendering. Memoised on the episode."""
    cached = episode._cache.get("customer_report")
    if cached is not None:
        return list(cached)
    reports = episode.metrics.get("reports") or []
    fallback = (SOURCE_SCORER_REPORT
                if episode.metrics.get("report_source") == SOURCE_REPORT_TOOL
                else SOURCE_FINDINGS)
    out = [customer_report_from_finding(r, i, fallback) for i, r in enumerate(reports)]
    args = last_accepted_report_call(episode)
    paired = _pair_tool_call(args, reports) if args is not None else None
    if paired is not None:
        from_tool = customer_report_from_tool(args, paired[0], paired[1])
        if from_tool is not None:
            out[paired[0]] = from_tool
    episode._cache["customer_report"] = tuple(out)
    return out


def gate_valid(episode: Episode, report: dict[str, Any], index: int) -> GateResult:
    """G1, valid: the scorer matched this report to a seeded defect. Nothing is decided
    here; the answer is the `matched` field the scorer saved."""
    matched = report.get("matched")
    if matched:
        return GateResult(True, f"matched seeded defect {matched}")
    return GateResult(False, "matched no seeded defect")


# ── G2, located (QUA-3586) ─────────────────────────────────────────────────────
#
# A valid report is LOCATED when all three hold:
#
#   1. it names a step of the test case;
#   2. the truth file shows the matched defect's symptom at that step;
#   3. the device showed the report's `observed` text while the agent was on that step.
#
# The scorer's `grounded` asks only whether the quote appeared anywhere in the episode.
# This gate adds the "where". It reads the saved transcript, the case file and the truth
# file; it decides nothing about the catch. docs/report-quality.md, "G2, located", has
# the rules in prose; the comments below say why each one is what it is.
#
# Two step numberings meet here. A report names a step of the BRIEF (`steps:` in the case
# file, what the agent was given). The truth file numbers the harness ROUTE
# (`check.steps`, one entry per tap or text entry), and a brief step is usually several
# route steps. `_route_spans` lines the two up.

HELDOUT_ENV = "QGB_HELDOUT_DIR"
_CORPUS_DIR = Path(__file__).resolve().parent / "data"
#: The brief the agent was sent, relative to the episode directory.
BRIEF_FILE = "instruction_sent.md"

# The route keys whose value is text the route types or aims at (the scorer's
# `ECHO_ROUTE_KEYS`; a test compares the two).
_ROUTE_TEXT_KEYS = ("type", "append", "tap", "long_press", "row")
#: The shortest text that counts as a quote or an anchor (the scorer's floor).
MIN_EVIDENCE_CHARS = 2

# MCP tools whose result is NOT device evidence, tools whose reply is the caller's own
# argument handed back, and tools that act on the app (what the meter charges more than
# a screen read for, less the three log reads and `mobile_dismiss_dialogs`, which are no
# step of any test case). Copies of three views of the scorer's one tool table, which
# this module may not import; tests/test_gate_located.py fails when they differ from it.
_MCP_NOT_EVIDENCE = frozenset((
    "mobile_boot_emulator", "mobile_boot_simulator", "mobile_get_action_log",
    "mobile_get_otp", "mobile_get_otp_number", "mobile_list_available_devices",
    "mobile_list_avds", "mobile_list_simulators", "mobile_mark_step",
    "mobile_note_anomaly", "mobile_release_otp_number", "mobile_report_result",
    "mobile_workspace_info"))
_MCP_ECHO = frozenset((
    "mobile_app_logs", "mobile_crash_logs", "mobile_device_logs", "mobile_edit_field",
    "mobile_get_permissions", "mobile_get_screen_recording_capabilities",
    "mobile_insert_credential", "mobile_install_app", "mobile_js_console_logs",
    "mobile_js_evaluate", "mobile_js_profiler_query", "mobile_launch_app",
    "mobile_native_profiler_query", "mobile_native_profiler_start",
    "mobile_native_profiler_stop", "mobile_open_url", "mobile_paste_text",
    "mobile_prepare_app_screen_capture", "mobile_press_button",
    "mobile_profiler_combined_report", "mobile_push_media", "mobile_react_profiler_query",
    "mobile_set_orientation", "mobile_set_permission",
    "mobile_stop_synthetic_screen_recording", "mobile_swipe", "mobile_terminate_app",
    "mobile_type_text", "mobile_uninstall_app", "mobile_visual_baseline",
    "mobile_visual_compare", "mobile_web_eval", "mobile_web_fill"))
_MCP_ACTIONS = frozenset((
    "mobile_double_tap", "mobile_edit_field", "mobile_launch_app", "mobile_long_press",
    "mobile_open_url", "mobile_paste_text", "mobile_pinch", "mobile_press_button",
    "mobile_rotate_gesture", "mobile_set_orientation", "mobile_set_permission",
    "mobile_setup_app", "mobile_swipe", "mobile_swipe_coordinates", "mobile_tap",
    "mobile_tap_and_observe", "mobile_terminate_app", "mobile_type_text",
    "mobile_web_click", "mobile_web_fill"))
# Tools that start or stop the app: a step of the case only where its route restarts it.
_MCP_LIFECYCLE = frozenset(("mobile_launch_app", "mobile_terminate_app"))
# Arguments that name the device or the app, never something on the screen.
_MCP_PLUMBING_ARGS = frozenset(("device", "udid", "serial", "package", "package_id",
                                "app_id", "bundle_id"))

_SPACE_RUN_RE = re.compile(r"[\s  ]+")
_JSON_WS_ESCAPE_RE = re.compile(r"(?<!\\)\\[nrt]")
_UNICODE_ESCAPE_RE = re.compile(
    r"\\\\"
    r"|\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    r"|\\u([0-9a-fA-F]{4})")
# A refused MCP call: the server's error envelope, which repeats the call's arguments.
_REFUSED_REPLY_RE = re.compile(
    r"^\s*(?:error\s+executing\s+tool\b|input\s+validation\s+error\b|unknown\s+tool\b"
    r"|mcp\s+error\b)"
    r"|\b\d+\s+validation\s+errors?\s+for\s+\w+arguments\b",
    re.IGNORECASE)
# The agent saying which step it is starting: "Step 3: open the drawer".
_STEP_SAID_RE = re.compile(r"\bstep\s+(\d+)\s*:", re.IGNORECASE)
# Raw arm: a shell command that acts on the app, and the text it types.
_RAW_ACTION_RE = re.compile(r"\binput\s+\S|\bam\s+(?:start|force-stop)\b|\bmonkey\b")
_RAW_TYPED_RE = re.compile(r"\binput\s+(?:\w+\s+)?text\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))")
_CRASH_STEP_RE = re.compile(r"^\s*step\s+(\d+)\s*:", re.IGNORECASE)


def _norm_text(value: Any) -> str:
    """A quote or an anchor as it is matched: trimmed, outer quotes dropped, lower case,
    every whitespace run one space. The scorer's `_norm`."""
    return _SPACE_RUN_RE.sub(" ", str(value or "").strip().strip("\"'").lower())


def _needle(value: Any) -> str:
    """`_norm_text`, or "" when the text is too short to identify anything."""
    text = _norm_text(value)
    return text if len(text) >= MIN_EVIDENCE_CHARS else ""


def _has_word(needle: str, text: str) -> bool:
    """`needle` in `text` on token boundaries (the scorer's `_word`)."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                     text) is not None


def _has_name(needle: str, text: str) -> bool:
    """`_has_word`, with `_` counted as part of a word: the word `event` is not in the
    resource id `event_all_day`."""
    return re.search(r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])",
                     text) is not None


_ARTICLE_RE = re.compile(r"(?<![a-z0-9_])(?:the|an|a)\s+(?=[a-z0-9\"'])")


def _plain(text: str) -> str:
    """Normalised text without its articles, so the route's `Create reminder` is found
    in the brief's "Create the reminder". Only for lining steps up, never for a quote."""
    return _ARTICLE_RE.sub("", text)


def _device_text(payload: str) -> str:
    """A device result as it is searched: lower case, a spelled-out `\\n` read as the
    space it stands for, whitespace folded. The scorer's `_device_text`."""
    return _SPACE_RUN_RE.sub(" ", _JSON_WS_ESCAPE_RE.sub(" ", (payload or "").lower()))


def _decode_unicode_escapes(text: str) -> str:
    """JSON `\\uXXXX` escapes decoded (codex keeps them, claude-code keeps the
    character); an escaped backslash stays as it is. The scorer's normaliser."""
    if not text or "\\u" not in text:
        return text

    def decode(m: re.Match) -> str:
        if m.group(1):
            hi, lo = int(m.group(1), 16), int(m.group(2), 16)
            return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))
        if m.group(3):
            cp = int(m.group(3), 16)
            return m.group(0) if 0xD800 <= cp <= 0xDFFF else chr(cp)
        return m.group(0)
    return _UNICODE_ESCAPE_RE.sub(decode, text)


def _result_text(result: Any) -> str:
    """The text a tool call returned: text blocks joined, images dropped."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return " ".join(str(b.get("text", "")) for b in result
                        if isinstance(b, dict) and b.get("type") == "text")
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, (list, str)):
            return _result_text(blocks)
        return json.dumps({k: v for k, v in result.items() if k != "content"})
    return str(result)


def _mcp_is_evidence(name: str) -> bool:
    return "mobile_" in name and name not in _MCP_NOT_EVIDENCE


@dataclass
class _Event:
    """One thing the agent did, in transcript order: something it said, or a tool call
    with the text that came back."""
    said: str = ""                    # the agent's own text (no call)
    name: str = ""                    # the tool's base name, or "shell" on the raw arm
    targets: tuple[str, ...] = ()     # normalised text the call typed or aimed at
    device: bool = False              # a call to the device
    acts: bool = False                # it changes the screen (a tap), not only reads it
    grounds: bool = False             # its result may ground a quote (echo rules applied)
    result: str | None = None         # `_device_text` of the result; None = no answer
    lifecycle: bool = False           # it starts or stops the app
    point: tuple[int, int] | None = None    # a tap on coordinates, with no text target
    refused: bool = False             # the server refused the call: nothing was done
    reply: str = ""                   # the result as sent, to look a tapped point up in


def _mcp_event(name: str, args: Any, raw: bool) -> _Event:
    """A tool call that is not a shell command. On the raw arm nothing is filtered as an
    echo, as in the scorer."""
    device = _mcp_is_evidence(name)
    args = args if isinstance(args, dict) else {}
    targets = tuple(t for t in (_needle(v) for k, v in sorted(args.items())
                                if k not in _MCP_PLUMBING_ARGS and isinstance(v, str)) if t)
    x, y = args.get("x"), args.get("y")
    point = (x, y) if all(isinstance(v, int) and not isinstance(v, bool)
                          for v in (x, y)) else None
    return _Event(name=name, targets=targets if device else (), device=device,
                  acts=device and name in _MCP_ACTIONS,
                  grounds=device and (raw or name not in _MCP_ECHO),
                  lifecycle=name in _MCP_LIFECYCLE, point=point if not targets else None)


def _shell_event(command: str, raw: bool, *, mentions_adb: bool | None = None) -> _Event:
    """A shell command. It is a device call only on the raw arm, when it runs adb
    (`mentions_adb`: the scorer's own test, when the caller has already made it)."""
    device = raw and ("adb" in command.lower() if mentions_adb is None else mentions_adb)
    typed = [a or b or c for a, b, c in _RAW_TYPED_RE.findall(command)] if device else []
    return _Event(name="shell", device=device, grounds=device,
                  acts=device and _RAW_ACTION_RE.search(command) is not None,
                  targets=tuple(t for t in (_needle(v.replace("%s", " ")) for v in typed)
                                if t))


def _answer(event: _Event, text: str, *, mcp: bool, raw: bool) -> None:
    """Attach a call's result. A refused MCP call's reply repeats its arguments, so it
    grounds nothing (not applied on the raw arm, as in the scorer)."""
    if mcp:
        text = _decode_unicode_escapes(text)
    if not raw and _REFUSED_REPLY_RE.search(text or ""):
        event.grounds = False
        event.refused = True
    event.result = _device_text(text)
    event.reply = text or ""


def agent_events(transcript: str, raw: bool) -> list[_Event]:
    """The episode in order: what the agent said and each tool call with its result.
    Reads the two formats the harness records (claude-code stream-json, codex
    `exec --json`). `raw` is the bare-adb arm. Which results count as device output,
    and which of those may ground a quote, follow the scorer's rules; a test compares
    the grounding text with the scorer's own."""
    events: list[_Event] = []
    open_calls: dict[str, tuple[_Event, bool]] = {}      # claude: call id -> (event, mcp)
    for line in transcript.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        message = ev.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if ev.get("type") == "assistant" and isinstance(blocks, list):
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    events.append(_Event(said=b["text"]))
                elif b.get("type") == "tool_use":
                    name = b.get("name") if isinstance(b.get("name"), str) else ""
                    mcp = name.startswith("mcp__") and _base_tool_name(name) != name
                    args = b.get("input")
                    if raw:
                        # The scorer's raw-arm rule: any call that mentions adb.
                        command = args.get("command") if isinstance(args, dict) else None
                        event = _shell_event(
                            command if isinstance(command, str) else "", raw,
                            mentions_adb="adb" in f"{name} {json.dumps(args or {})}".lower())
                    else:
                        event = _mcp_event(_base_tool_name(name), args, raw)
                    events.append(event)
                    if isinstance(b.get("id"), str):
                        open_calls[b["id"]] = (event, mcp)
        elif ev.get("type") == "user" and isinstance(blocks, list):
            for b in blocks:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in open_calls):
                    event, mcp = open_calls[b["tool_use_id"]]
                    _answer(event, _result_text(b.get("content", "")), mcp=mcp, raw=raw)
        elif ev.get("type") == "item.completed" and isinstance(ev.get("item"), dict):
            item = ev["item"]
            kind = item.get("type")
            if kind == "agent_message" and isinstance(item.get("text"), str):
                events.append(_Event(said=item["text"]))
            elif kind == "mcp_tool_call":
                event = _mcp_event(_base_tool_name(str(item.get("tool") or "")),
                                   item.get("arguments"), raw)
                status = str(item.get("status") or "").lower()
                error = item.get("error")
                text = ""
                if status != "in_progress" and ("result" in item or error):
                    text = _result_text(item.get("result"))
                    if not text and error:
                        text = (str(error.get("message") or "")
                                if isinstance(error, dict) else str(error))
                _answer(event, text, mcp=True, raw=raw)
                events.append(event)
            elif kind == "command_execution":
                event = _shell_event(str(item.get("command") or ""), raw)
                event.result = _device_text(str(item.get("aggregated_output") or ""))
                events.append(event)
    return events


def grounding_texts(events: list[_Event]) -> list[str]:
    """Every device result that may ground a quote, in order."""
    return [e.result for e in events if e.grounds and e.result is not None]


# ── the case and its truth ─────────────────────────────────────────────────────

def _corpus_json(rel: str) -> Any:
    """A corpus file, read from the held-out directory when it is there, else from the
    packaged data. None when neither has it."""
    roots = [Path(os.environ[HELDOUT_ENV]).expanduser()] if os.environ.get(
        HELDOUT_ENV, "").strip() else []
    for root in (*roots, _CORPUS_DIR):
        path = root / rel
        if path.is_file():
            stat = path.stat()
            return _parsed_file(str(path), stat.st_mtime_ns, stat.st_size)
    return None


@functools.lru_cache(maxsize=64)
def _parsed_file(path: str, mtime_ns: int, size: int) -> Any:
    """One corpus file parsed, kept while the file does not change (a board has many
    episodes per app). Callers only read the result."""
    import yaml  # the case files are YAML; the one import here that is not stdlib
    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError):
        return {"_unreadable": Path(path).name}


@dataclass(frozen=True)
class CaseTruth:
    """What G2 needs to know about one test case: the brief's steps, the route's steps
    and the truth row derived for it."""
    brief: tuple[str, ...]
    route: tuple[Any, ...]
    row: dict[str, Any]


def load_case_truth(app_id: str, case_id: str) -> CaseTruth | str:
    """The case and its truth row, or a sentence saying why they cannot be used."""
    if not app_id or not case_id:
        return "the saved result names no app or case"
    cases = _corpus_json(f"test-cases/{app_id}.yaml")
    truth = _corpus_json(f"truth/journey-{app_id}.json")
    for doc, what in ((cases, "test-case file"), (truth, "truth file")):
        if not isinstance(doc, dict):
            return f"no {what} for this app in the corpus"
        if "_unreadable" in doc:
            return f"unreadable {what}"
    case = next((c for c in cases.get("test_cases") or []
                 if isinstance(c, dict) and str(c.get("id")) == case_id), None)
    row = truth.get(case_id)
    if case is None or not isinstance(row, dict):
        return "the corpus has no such case, or no truth row for it"
    route = (case.get("check") or {}).get("steps") if isinstance(case.get("check"),
                                                                 dict) else None
    brief = case.get("steps")
    if not isinstance(route, list) or not route or not isinstance(brief, list) or not brief:
        return "the case has no steps or no route"
    return CaseTruth(tuple(str(s) for s in brief), tuple(route), row)


def _route_values(step: Any) -> list[str]:
    """The text one route step types or aims at, normalised."""
    if not isinstance(step, dict):
        return []
    return [t for t in (_needle(step[k]) for k in _ROUTE_TEXT_KEYS if k in step) if t]


def _route_spans(brief: tuple[str, ...], route: tuple[Any, ...]) -> list[tuple[int, int]]:
    """For each route step, the brief steps it can belong to, as `(first, last)`
    (1-based, inclusive).

    A route step that types or taps a text "fits" a brief step whose sentence contains
    that text. Route steps are done in brief order, so the assignment never goes
    backwards. Among all such assignments, the ones with the most fits are kept, and a
    route step's span is every brief step it takes in any of them. A route step with
    no text (`launch`, `wait`, a tap on a resource id) floats between its neighbours."""
    n_brief, n_route = len(brief), len(route)
    sentences = [_plain(_norm_text(s)) for s in brief]
    fits = [[any(_has_name(_plain(v), sentences[j]) for v in _route_values(route[i]))
             for j in range(n_brief)] for i in range(n_route)]
    worst = -1
    before = [[worst] * n_brief for _ in range(n_route)]   # best fits of route[..i], i at j
    after = [[worst] * n_brief for _ in range(n_route)]    # best fits of route[i..], i at j
    for i in range(n_route):
        best = worst
        for j in range(n_brief):
            best = 0 if i == 0 else max(best, before[i - 1][j])
            before[i][j] = best + fits[i][j]
    for i in reversed(range(n_route)):
        best = worst
        for j in reversed(range(n_brief)):
            best = 0 if i == n_route - 1 else max(best, after[i + 1][j])
            after[i][j] = best + fits[i][j]
    most = max(before[-1])
    spans = []
    for i in range(n_route):
        at = [j for j in range(n_brief) if before[i][j] + after[i][j] - fits[i][j] == most]
        spans.append((at[0] + 1, at[-1] + 1))
    return spans


def symptom_route_steps(row: dict[str, Any], defect: str) -> tuple[str, list[int]]:
    """Where the truth row shows one defect, as `(kind, route steps)`:

      side      a side bug: its `visible_steps`
      crash     the blocking bug of a case whose seeded build dies: the step at which
                the death was recorded and the step before it. The route finds the app
                dead when its NEXT step runs, so the action that killed it is one of
                the two
      blocking  any other blocking bug: every step whose clean/seeded screen diff
                carries a text
      ""        the row does not know this defect

    An empty step list means the truth recorded no screen for the defect (a defect
    seen only in stored data)."""
    for side in row.get("side") or []:
        if isinstance(side, dict) and side.get("bug") == defect:
            return "side", sorted({s for s in side.get("visible_steps") or []
                                   if isinstance(s, int) and not isinstance(s, bool)})
    if row.get("blocking") != defect:
        return "", []
    seeded = (row.get("passes") or {}).get("seeded") or {}
    if seeded.get("outcome") == "crashed":
        said = _CRASH_STEP_RE.match(str(seeded.get("detail") or ""))
        step = int(said.group(1)) if said else seeded.get("steps_run")
        if not isinstance(step, int) or isinstance(step, bool):
            return "crash", []
        return "crash", [s for s in (step - 1, step) if s >= 1]
    steps = {d.get("step") for d in row.get("unclaimed_diff") or []
             if isinstance(d, dict) and any(
                 _needle(t) for t in [*(d.get("added") or []), *(d.get("removed") or [])])}
    return "blocking", sorted(s for s in steps if isinstance(s, int)
                              and not isinstance(s, bool))


def _acts(route_step: Any) -> bool:
    """Does this route step act on the app? `launch` and `wait` do not."""
    name = route_step if isinstance(route_step, str) else next(iter(route_step), "") \
        if isinstance(route_step, dict) else ""
    return name not in ("launch", "wait", "")


@dataclass(frozen=True)
class StepMap:
    """How one case's brief steps and route steps line up."""
    steps: int                              # how many brief steps
    spans: tuple[tuple[int, int], ...]      # per route step: the brief steps it can be
    sentences: tuple[str, ...]              # the brief steps, normalised
    own_values: dict[int, tuple[str, ...]]  # route text only this brief step can own
    reads_only: frozenset[int]              # brief steps no acting route step can be
    restarts: tuple[int, ...] = ()          # brief steps a `relaunch` route step can be

    def through(self, step: int) -> int:
        """The last brief step on which the screen left by `step`'s action is still
        up: the next brief step that may act, or the case's last step. A step that only
        reads ("Read what the list shows") has no action of its own, so the screen it
        reads is the one an earlier step left."""
        nxt = step + 1
        while nxt < self.steps and nxt in self.reads_only:
            nxt += 1
        return min(nxt, self.steps)

    def fitting(self, target: str) -> list[int]:
        """The brief steps an action aimed at `target` can be: the text is in the
        step's sentence, or it contains route text that belongs to that step alone."""
        target = _plain(target)
        return [j + 1 for j, sentence in enumerate(self.sentences)
                if _has_name(target, sentence)
                or any(_has_name(v, target) for v in self.own_values.get(j + 1, ()))]

    def acting(self, first: int, last: int) -> list[int]:
        """The brief steps in `first..last` that an action can belong to."""
        return [j for j in range(first, last + 1) if j not in self.reads_only]


def step_map(case: CaseTruth) -> StepMap:
    spans = _route_spans(case.brief, case.route)
    own: dict[int, list[str]] = {}
    may_act: set[int] = set()
    restarts: set[int] = set()
    sentences = tuple(_plain(_norm_text(s)) for s in case.brief)
    for (first, last), step in zip(spans, case.route):
        for value in map(_plain, _route_values(step)):
            # The one brief step in the route step's span whose sentence has the text.
            named = [j for j in range(first, last + 1) if _has_name(value, sentences[j - 1])]
            if len(named) == 1 or first == last:
                own.setdefault(named[0] if named else first, []).append(value)
        if _acts(step):
            may_act.update(range(first, last + 1))
        if step == "relaunch":
            restarts.update(range(first, last + 1))
    n = len(case.brief)
    return StepMap(n, tuple(spans), sentences, {k: tuple(v) for k, v in own.items()},
                   frozenset(set(range(1, n + 1)) - may_act), tuple(sorted(restarts)))


def _label_at(reply: str, point: tuple[int, int]) -> str:
    """The text of the element a screen listing puts at `point`, or "". A screen read
    answers with `{"elements": [{"text" | "label", "x", "y"}, ...]}`; an agent that taps
    coordinates copies them from there."""
    try:
        # claude-code puts a line naming the screenshot's file before the listing.
        screen, _ = json.JSONDecoder().raw_decode(reply, reply.index("{"))
    except ValueError:
        return ""
    elements = screen.get("elements") if isinstance(screen, dict) else None
    for el in elements if isinstance(elements, list) else []:
        if isinstance(el, dict) and (el.get("x"), el.get("y")) == point:
            label = _needle(el.get("text")) or _needle(el.get("label"))
            if label:
                return label
    return ""


def symptom_brief_steps(case: CaseTruth, route_steps: list[int]) -> list[int]:
    """The brief steps on which a symptom recorded at these route steps is on screen.

    The truth records the screen AFTER a route step. That screen stays up until the
    next action, so it can be seen from the first brief step the route step can belong
    to through `StepMap.through` of the last one."""
    steps = step_map(case)
    shown: set[int] = set()
    for r in route_steps:
        if 1 <= r <= len(steps.spans):
            first, last = steps.spans[r - 1]
            shown.update(range(first, steps.through(last) + 1))
    return sorted(shown)


# ── which step each device result belongs to ───────────────────────────────────

@dataclass(frozen=True)
class _Seen:
    """One device result that may ground a quote, and the brief steps it belongs to:
    surely `first..sure_last` (none when `sure_last` is below `first`), and possibly any
    step up to `last`, where the transcript does not say."""
    text: str
    first: int
    sure_last: int
    last: int

    def surely(self, step: int) -> bool:
        return self.first <= step <= self.sure_last

    def possibly(self, step: int) -> bool:
        return self.first <= step <= self.last


@dataclass
class _Where:
    """The brief steps one action can belong to: `first..last`, of which `first` is
    sure when `sure`."""
    first: int
    last: int
    sure: bool


def attribute_steps(events: list[_Event], case: CaseTruth) -> list[_Seen]:
    """Label every grounding device result with the brief steps it belongs to.

    First each ACTION (a tap, a text entry) is given the step it was done on:

      * it fits a step: its target text is in a brief step's sentence, or contains
        route text that belongs to that step alone (`StepMap.fitting`). A tap on
        coordinates aims at the element the last screen listing put there. When
        several steps fit, the first one at or after the current step is taken, and
        that step is now the current one;
      * the agent announced it: its last message had exactly one "Step N:" in it. The
        next action is step N's, and N is the current step;
      * neither: it could be the current step or any later step that has an action, up
        to the step of the next action that does fit or is announced. Not sure, unless
        no such later step exists: then it was the current step's;
      * it fits only steps before the current one: the agent went back. The action is
        that earlier step's (the latest that fits), done again; whether the agent has
        returned to that step or is still looking into the current one is not known,
        so the steps after it up to the current one are possible, not sure.

    Then each RESULT takes the steps of the last action before it. A result shows the
    screen that action left, whether the action returned it or a later read did, and
    that screen stays up through `StepMap.through`: a read made after the agent has
    moved on to the next step, before it acts there, counts for both steps. Before any
    action the screen is the one the case starts on, which is up until the first step
    that has an action acts: step 1, in nearly every case.

    Not actions: a call the server refused (nothing was done), and starting or stopping
    the app in a case whose route never restarts it (that is recovery after a crash,
    not a step; where the route does restart the app, it fits those steps)."""
    steps = step_map(case)
    current, announced = 1, False
    start = last_action = _Where(1, 1, True)
    acting = steps.acting(1, steps.steps)
    start_last = acting[0] if acting else steps.steps
    waiting: list[_Where] = []         # actions whose step the next mark will bound
    screen = ""                        # the last screen listing, as the device sent it
    results: list[tuple[str, _Where, int]] = []

    def mark(step: int) -> None:
        """A later action is known to be `step`: bound the actions that were not."""
        for where in waiting:
            later = steps.acting(where.first + 1, step if step >= where.first else steps.steps)
            where.last, where.sure = (later[-1], False) if later else (where.first, True)
        waiting.clear()

    for event in events:
        if event.said:
            said = {int(n) for n in _STEP_SAID_RE.findall(event.said)}
            said = {n for n in said if 1 <= n <= steps.steps}
            if len(said) == 1:
                current, announced = said.pop(), True
                mark(current)
            continue
        if not event.device:
            continue
        if event.acts and not event.refused and (steps.restarts or not event.lifecycle):
            targets = event.targets
            if event.point is not None and screen:
                targets = tuple(t for t in (_label_at(screen, event.point),) if t)
            fits = sorted({j for t in targets for j in steps.fitting(t)}
                          | (set(steps.restarts) if event.lifecycle else set()))
            ahead = [j for j in fits if j >= current]
            if ahead:
                current = ahead[0]
                mark(current)
                last_action = _Where(current, current, True)
            elif announced:
                last_action = _Where(current, current, True)
            elif fits:
                last_action = _Where(fits[-1], current, True)
            else:
                last_action = _Where(current, steps.steps, False)
                waiting.append(last_action)
            announced = False
        if event.grounds and event.result is not None:
            results.append((event.result, last_action, current))
        if '"elements"' in event.reply:
            screen = event.reply
    mark(steps.steps)
    seen = []
    for text, where, at in results:
        first, last = min(where.first, at), steps.through(max(where.last, at))
        if where is start:
            last = max(start_last, at)
        if not where.sure:
            sure_last = first - 1
        elif where.first == where.last:         # one step, and wherever the agent is now
            sure_last = last
        else:                                   # went back: the earlier step's screen
            sure_last = steps.through(where.first)
        seen.append(_Seen(text, first, sure_last, last))
    return seen


def _named_step(report: dict[str, Any], customer: CustomerReport) -> int | None:
    """The step the report names: the first number in the customer-shaped report's
    `failure_step`, else the saved report's own `step`."""
    number = _NUMBER_RE.search(customer.failure_step or "")
    if number:
        return int(number.group(0))
    step = report.get("step")
    return step if isinstance(step, int) and not isinstance(step, bool) else None


def _unknown(reason: str) -> GateResult:
    return GateResult(None, reason, unknown=True)


def _located_context(episode: Episode) -> tuple[CaseTruth | str, list[_Seen]]:
    """The case, and the episode's device results labelled by step; once per episode."""
    cached = episode._cache.get("located")
    if cached is not None:
        return cached
    metrics = episode.metrics
    case = load_case_truth(str(metrics.get("app_id") or ""), str(metrics.get("case_id") or ""))
    seen: list[_Seen] = []
    if isinstance(case, CaseTruth):
        try:
            brief_sent = (episode.dir / BRIEF_FILE).read_text(encoding="utf-8",
                                                              errors="replace")
        except OSError:
            brief_sent = None
        if brief_sent is not None and not all(
                f"{i}. {s}" in brief_sent for i, s in enumerate(case.brief, 1)):
            case = "the corpus's steps for this case are not the steps this episode was sent"
    if isinstance(case, CaseTruth):
        try:
            transcript = (episode.dir / TRANSCRIPT_FILE).read_text(encoding="utf-8",
                                                                   errors="replace")
        except OSError:
            transcript = ""
        raw = str(episode.result.get("condition") or "") == "raw"
        seen = attribute_steps(agent_events(transcript, raw), case)
    episode._cache["located"] = (case, seen)
    return case, seen


def gate_located(episode: Episode, report: dict[str, Any], index: int) -> GateResult:
    """G2, located: the report names a step, the truth shows the matched defect's
    symptom at that step, and the device showed the report's `observed` text while the
    agent was on it. Runs only on a report the scorer matched to a seeded defect.
    Unknown (never a pass) when the truth has no step for the defect or the transcript
    does not say which step the calls that showed the text belong to."""
    defect = report.get("matched")
    if not defect:
        return GateResult(None, "not run: the report matched no seeded defect")
    step = _named_step(report, customer_report(episode)[index])
    if step is None:
        return GateResult(False, "the report names no step")
    case, seen = _located_context(episode)
    if isinstance(case, str):
        return _unknown(case)
    if not 1 <= step <= len(case.brief):
        return GateResult(False, f"the report names step {step}; the case has "
                                 f"{len(case.brief)} steps")
    kind, route_steps = symptom_route_steps(case.row, str(defect))
    if not kind:
        return _unknown(f"the truth row does not list {defect}")
    shown = symptom_brief_steps(case, route_steps)
    if not shown:
        return _unknown(f"the truth records no screen step for {defect}")
    where = "crashes" if kind == "crash" else "shows"
    if step not in shown:
        return GateResult(False, f"the report names step {step}; the truth {where} "
                                 f"{defect} at step {_list(shown)}")
    quote = _needle(report.get("observed"))
    if not quote:
        return GateResult(False, "the report quotes no observed text")
    hits = [s for s in seen if quote in s.text]
    if not hits:
        return GateResult(False, "the observed text is in no device output")
    if any(s.surely(step) for s in hits):
        return GateResult(True, f"step {step}: the truth {where} {defect} there and the "
                                "device showed the observed text on that step")
    if any(s.possibly(step) for s in hits):
        return _unknown("the device showed the observed text, but the transcript does not "
                        f"say whether those calls were step {step}")
    at = sorted({n for s in hits for n in range(s.first, s.last + 1)})
    return GateResult(False, f"the device showed the observed text at step {_list(at)}, "
                             f"not at step {step}")


def _list(steps: list[int]) -> str:
    return ", ".join(str(s) for s in steps)


# ── G3, complete (QUA-3587) ────────────────────────────────────────────────────
#
# A developer needs three things from a bug: what happened, what should have happened,
# and how to get there. G3 tests the customer-shaped report for each, with fixed rules
# and no model:
#
#   observed   `actual` is not empty
#   expected   `expected` is not empty
#   steps      the bug's `steps` text is a list of reproduction steps (below)
#
# The same steps rule is also run on `agent_steps`, the step list the agent sent and
# the bug does not carry, to give `complete_if_steps_carried`: what G3 would be if the
# product stored that list as the bug's steps. It is a side figure, never a gate.
#
# "Steps that are steps": at least two entries; every entry opens with a verb from the
# fixed lists below (a UI action, or an observation such as "verify"); at least two
# entries are UI actions; no entry names a source file or a function call; and the text
# is not the summary repeated. Whether the steps are the RIGHT steps is not judged here.

#: Base forms of the verbs that open a UI action. Inflected forms are derived
#: (`tap`, `taps`, `tapped`, `tapping`). Changing either list changes G3: see the
#: version rule.
ACTION_VERBS = (
    "tap", "click", "press", "touch", "type", "enter", "input", "fill", "write", "clear",
    "paste", "copy", "open", "launch", "start", "relaunch", "restart", "reopen", "close",
    "kill", "force", "exit", "leave", "swipe", "scroll", "drag", "drop", "pull", "pinch",
    "zoom", "fling", "flick", "hold", "release", "move", "select", "choose", "pick",
    "toggle", "switch", "enable", "disable", "turn", "set", "tick", "untick", "uncheck",
    "mark", "unmark", "go", "navigate", "return", "visit", "dismiss", "confirm", "cancel",
    "accept", "allow", "deny", "grant", "save", "add", "create", "delete", "remove",
    "edit", "rename", "change", "update", "duplicate", "search", "filter", "sort",
    "submit", "send", "share", "sign", "log", "rotate", "install", "uninstall", "expand",
    "collapse", "focus", "refresh", "reload", "repeat", "complete", "lock", "unlock",
    "undo", "redo", "import", "export", "pin", "unpin", "archive", "restore", "skip",
)
#: Verbs that open an observation. An observation is a fair entry in a step list ("Verify
#: the total"), but it is not an action: a list needs two actions besides.
OBSERVE_VERBS = (
    "observe", "verify", "check", "see", "notice", "note", "wait", "look", "read", "view",
    "inspect", "compare", "expect", "assert", "ensure", "validate", "find", "watch",
)
#: The fewest entries, and the fewest UI-action entries, a step list may have.
MIN_STEP_ENTRIES = 2

_IRREGULAR_FORMS = {
    "go": ("goes", "went", "going"), "choose": ("chooses", "chose", "choosing"),
    "write": ("writes", "wrote", "writing"), "hold": ("holds", "held", "holding"),
    "set": ("sets", "setting"), "send": ("sends", "sent", "sending"),
    "leave": ("leaves", "left", "leaving"), "see": ("sees", "saw", "seeing"),
    "read": ("reads", "reading"), "find": ("finds", "found", "finding"),
    "input": ("inputs", "inputting"), "undo": ("undoes", "undid", "undoing"),
    "redo": ("redoes", "redid", "redoing"),
}
#: Verbs that double their last letter before -ed / -ing.
_DOUBLING = frozenset({"tap", "drag", "drop", "flip", "pin", "unpin", "log", "skip"})


def _verb_forms(verbs: tuple[str, ...]) -> frozenset[str]:
    forms: set[str] = set()
    for verb in verbs:
        forms.add(verb)
        if verb in _IRREGULAR_FORMS:
            forms.update(_IRREGULAR_FORMS[verb])
            continue
        forms.add(verb + ("es" if verb.endswith(("s", "sh", "ch", "x")) else "s"))
        if verb.endswith("e"):
            forms.update((verb + "d", verb[:-1] + "ing"))
        else:
            stem = verb + verb[-1] if verb in _DOUBLING else verb
            forms.update((stem + "ed", stem + "ing"))
    return frozenset(forms)


_ACTION_FORMS = _verb_forms(ACTION_VERBS)
_OBSERVE_FORMS = _verb_forms(OBSERVE_VERBS)

# What may stand before an entry's text: a bullet, a number, "Step 3:", a tick or cross.
_STEP_MARKER_RE = re.compile(
    r"^(?:[-*•·–—>]+|\(?\d+[.):]\)?|step\s*\d+\s*[:.)-]?|\[[ xX✓✗]?\]"
    r"|[✓✗✔✘☑☐✅❌])\s*", re.IGNORECASE)
_STEP_HEADING_RE = re.compile(
    r"^(?:steps?|repro\w*|reproduction|how)\b[^:]*:$", re.IGNORECASE)
_INLINE_NUMBER_RE = re.compile(r"(?:^|\s)\(?\d{1,2}[.)]\s+")
_INLINE_ARROW_RE = re.compile(r"\s*(?:->|=>|→|;)\s*")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
# Words that may stand before the verb: "Then tap", "The user opens", "Please select".
_LEAD_IN_RE = re.compile(
    r"^(?:then|next|now|and|finally|first|second|third|lastly|again|also|please"
    r"|after\s+that|the\s+user|the\s+tester|the\s+agent|user|tester|agent|i|we|you)\b"
    r"[\s,:]*", re.IGNORECASE)
_GESTURE_PREFIX_RE = re.compile(r"^(?:long|double|triple)[\s-]+", re.IGNORECASE)
# "On the Cart screen, tap Pay": the verb is after the comma.
_PLACE_CLAUSE_RE = re.compile(
    r"^(?:on|in|from|at|with|under|inside|within|once|when|while|after|before)\b[^,]*,\s*",
    re.IGNORECASE)
_FIRST_WORD_RE = re.compile(r"[A-Za-z]+")
# A source file name or a function call: the mark of a code investigation.
_CODE_RE = re.compile(
    r"\b[\w/-]+\.(?:kt|kts|java|swift|m|mm|dart|ts|tsx|js|jsx|py|rb|go|rs|cs|cpp|xml"
    r"|gradle|json|ya?ml)\b|\b\w+\(\)|\b\w+\.\w+\(")
_NOT_ALNUM_RE = re.compile(r"[^a-z0-9]+")
#: The text keys of one `agent_steps` item, in the order they are tried.
_STEP_TEXT_KEYS = ("description", "action", "text", "instruction", "title", "name", "step")


def _strip_step_markers(text: str) -> str:
    text = text.strip(_JS_SPACE)
    while True:
        stripped = _STEP_MARKER_RE.sub("", text, count=1)
        if stripped == text:
            return text
        text = stripped


def step_entries(steps: Any) -> list[str]:
    """The entries of a steps value, markers removed. A list gives one entry per item
    (a string, or a mapping's first text field; anything else is an empty entry). A
    string gives one entry per line; a single line is split at inline numbers
    (`1. … 2. …`), else at arrows or semicolons, else at sentence ends."""
    if isinstance(steps, (list, tuple)):
        out = []
        for item in steps:
            if isinstance(item, dict):
                item = next((item[k] for k in _STEP_TEXT_KEYS
                             if isinstance(item.get(k), str) and item[k].strip(_JS_SPACE)),
                            "")
            out.append(_strip_step_markers(item) if isinstance(item, str) else "")
        return out
    if not isinstance(steps, str):
        return []
    lines = [line.strip(_JS_SPACE) for line in steps.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) > 1:
        lines = [line for line in lines if not _STEP_HEADING_RE.match(line)]
    elif lines:
        for splitter in (_INLINE_NUMBER_RE, _INLINE_ARROW_RE, _SENTENCE_END_RE):
            parts = [p for p in splitter.split(lines[0]) if p.strip(_JS_SPACE)]
            if len(parts) > 1:
                lines = parts
                break
    return [entry for entry in map(_strip_step_markers, lines) if entry]


def step_kind(entry: str) -> str | None:
    """What one entry is: "action" (it opens with a UI-action verb), "observation" (it
    opens with an observation verb), or None (neither, or it names code)."""
    if _CODE_RE.search(entry):
        return None
    text = _strip_step_markers(entry)
    text = _PLACE_CLAUSE_RE.sub("", text, count=1)
    while True:
        stripped = _LEAD_IN_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _GESTURE_PREFIX_RE.sub("", text, count=1)
    word = _FIRST_WORD_RE.match(text)
    if not word:
        return None
    verb = word.group(0).lower()
    if verb in _ACTION_FORMS:
        return "action"
    return "observation" if verb in _OBSERVE_FORMS else None


def _fold(text: str) -> str:
    return _NOT_ALNUM_RE.sub(" ", text.lower()).strip()


def steps_are_steps(steps: Any, summary: str = "") -> tuple[bool, str]:
    """Whether a steps value (the bug's text, or the agent's list) is a list of
    reproduction steps, and why. Says nothing about whether the steps are correct."""
    entries = step_entries(steps)
    if not entries:
        return False, "no steps"
    if summary and _fold(" ".join(entries)) == _fold(summary):
        return False, "the steps repeat the summary"
    if len(entries) < MIN_STEP_ENTRIES:
        return False, f"1 entry; needs at least {MIN_STEP_ENTRIES}"
    kinds = [step_kind(entry) for entry in entries]
    if None in kinds:
        at = kinds.index(None)
        return False, (f"entry {at + 1} of {len(entries)} is not a UI action: "
                       f"{entries[at][:60]!r}")
    actions = kinds.count("action")
    if actions < MIN_STEP_ENTRIES:
        return False, (f"{actions} of {len(entries)} entries are UI actions; needs at "
                       f"least {MIN_STEP_ENTRIES}")
    return True, f"{len(entries)} entries, {actions} UI actions"


def complete_checks(report: CustomerReport) -> dict[str, Any]:
    """G3's sub-checks for one customer-shaped report, as `quality.json` stores them
    under `complete`. `steps` judges the bug's own steps text; `agent_steps` judges the
    step list the agent sent and the bug does not carry."""
    steps, steps_reason = steps_are_steps(report.steps, report.summary)
    carried, carried_reason = steps_are_steps(report.agent_steps, report.summary)
    observed, expected = bool(report.actual), bool(report.expected)
    return {
        "observed": observed,
        "expected": expected,
        "steps": steps,
        "steps_reason": steps_reason,
        "agent_steps": carried,
        "agent_steps_reason": carried_reason,
        "complete_if_steps_carried": observed and expected and carried,
    }


def gate_complete(episode: Episode, report: dict[str, Any], index: int) -> GateResult:
    """G3, complete: the customer-shaped report has an observed result, an expected
    result and reproduction steps that are steps. Runs on every non-neutral report,
    whether or not it is valid."""
    checks = complete_checks(customer_report(episode)[index])
    if checks["observed"] and checks["expected"] and checks["steps"]:
        return GateResult(True, f"observed, expected and steps ({checks['steps_reason']})")
    missing = [name for key, name in (("observed", "no observed result"),
                                      ("expected", "no expected result")) if not checks[key]]
    if not checks["steps"]:
        missing.append(f"steps: {checks['steps_reason']}")
    return GateResult(False, "; ".join(missing))


def _complete_totals(scored: list[dict[str, Any]]) -> dict[str, int]:
    """How many scored reports passed each G3 sub-check. `complete` equals G3's
    `passed`; `evaluated` is the denominator of every figure here."""
    blocks = [r["complete"] for r in scored]
    counts = {key: sum(1 for b in blocks if b[key]) for key in (
        "observed", "expected", "steps", "agent_steps", "complete_if_steps_carried")}
    counts["evaluated"] = len(blocks)
    counts["complete"] = sum(1 for b in blocks
                             if b["observed"] and b["expected"] and b["steps"])
    return counts


#: Every gate, in the order it is evaluated and listed. Later tickets append here.
GATES: tuple[Gate, ...] = (
    Gate("G1", "valid", gate_valid),
    Gate("G2", "located", gate_located, reports_unknown=True),
    Gate("G3", "complete", gate_complete),
)


def _customer_ready(gates: dict[str, dict[str, Any]]) -> bool | None:
    """False as soon as one headline gate failed; true only when every headline gate ran
    and passed; otherwise unknown."""
    answers = [(gates.get(g) or {}).get("pass") for g in HEADLINE_GATES]
    if any(a is False for a in answers):
        return False
    if all(a is True for a in answers):
        return True
    return None


def _score_report(episode: Episode, report: dict[str, Any], index: int) -> dict[str, Any]:
    neutral = is_neutral(report)
    gates: dict[str, dict[str, Any]] = {}
    if not neutral:
        for gate in GATES:
            gates[gate.id] = gate.fn(episode, report, index).as_dict()
    return {
        "index": index,
        "step": report.get("step"),
        "matched": report.get("matched"),
        "known": report.get("known"),
        "consequence_of": report.get("consequence_of"),
        "neutral": neutral,
        "gates": gates,
        "customer_ready": None if neutral else _customer_ready(gates),
        # G3's sub-checks, and the same on the agent's own step list (QUA-3587).
        "complete": None if neutral else complete_checks(customer_report(episode)[index]),
        # What a customer would read for this report, and where it came from.
        "customer_report": customer_report(episode)[index].as_dict(),
    }


def _totals(reports: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in reports if not r["neutral"]]
    gates = {}
    for gate in GATES:
        answers = [r["gates"][gate.id]["pass"] for r in scored]
        gates[gate.id] = {"name": gate.name,
                          "evaluated": sum(1 for a in answers if a is not None),
                          "passed": sum(1 for a in answers if a is True)}
        if gate.reports_unknown:
            gates[gate.id]["unknown"] = sum(
                1 for r in scored if r["gates"][gate.id].get("unknown"))
    return {
        "reports": len(reports),
        "neutral": len(reports) - len(scored),
        "scored": len(scored),
        "gates": gates,
        "complete": _complete_totals(scored),
        "customer_ready": sum(1 for r in scored if r["customer_ready"] is True),
        "customer_ready_unknown": sum(1 for r in scored if r["customer_ready"] is None),
    }


def load_episode(episode_dir: Path | str) -> Episode:
    """Read an episode's `result.json`. Raises `QualityError` when the directory holds no
    readable result, or a result with no journey report list (a hunt or guided episode,
    or a verdict saved before reports were recorded)."""
    episode_dir = Path(episode_dir)
    path = episode_dir / RESULT_FILE
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QualityError(f"no {RESULT_FILE} in {episode_dir}") from None
    except (OSError, ValueError) as exc:
        raise QualityError(f"unreadable {path}: {exc}") from None
    if not isinstance(result, dict) or not isinstance(result.get("metrics"), dict):
        raise QualityError(f"{path} is not an episode result")
    reports = result["metrics"].get("reports")
    if not isinstance(reports, list) or not all(isinstance(r, dict) for r in reports):
        raise QualityError(f"{path} has no journey report list")
    return Episode(episode_dir, result)


def score_episode(episode_dir: Path | str, *, judge: bool = False) -> dict[str, Any]:
    """The quality block of one saved journey episode, as `quality.json` holds it. Reads
    only; `write_quality` saves it. Raises `QualityError` for a directory that is not a
    scorable journey episode.

    `judge=True` adds the G5 side column (`steps_accuracy`) from the episode's SAVED
    judge results (`quality_judge.attach`). It calls no model, and it changes no gate
    and no other total; without it the block is the same as before G5 existed."""
    episode = load_episode(episode_dir)
    metrics = episode.metrics
    reports = [_score_report(episode, r, i) for i, r in enumerate(metrics["reports"])]
    quality = {
        "quality_version": QUALITY_VERSION,
        # The scorer whose `matched` / neutral marks the gates read (None on a verdict
        # saved before the scorer stamped its version).
        "scorer_version": metrics.get("scorer_version"),
        # Where the SCORER read the episode's reports. Each report's own
        # `customer_report.report_source` says where its customer-shaped text came from.
        "scorer_report_source": metrics.get("report_source") or "",
        "reports": reports,
        "totals": _totals(reports),
    }
    if judge:
        # The one import from the package, and only on request: the side column lives
        # in its own module so that nothing here depends on a model.
        from . import quality_judge
        quality_judge.attach(episode, quality)
    return quality


def render(quality: dict[str, Any]) -> str:
    """The exact text of `quality.json`: sorted keys, two-space indent, one final
    newline. One function, so every writer produces the same bytes."""
    return json.dumps(quality, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_quality(episode_dir: Path | str, *, judge: bool = False) -> Path:
    """Score one episode and write `quality.json` beside its `result.json`, through a
    temp file and `os.replace` so a reader never sees half a file. Touches no other
    file. Returns the path written."""
    episode_dir = Path(episode_dir)
    text = render(score_episode(episode_dir, judge=judge))
    path = episode_dir / QUALITY_FILE
    tmp = episode_dir / f".{QUALITY_FILE}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def episode_dirs(root: Path | str) -> Iterator[Path]:
    """Every episode directory at or under `root`, sorted: a directory that holds a
    `result.json`. Nothing inside an episode is searched, so a `result.json` an agent
    left in its workspace is never taken for an episode."""
    root = Path(root)
    if (root / RESULT_FILE).is_file():
        yield root
        return
    for child in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        yield from episode_dirs(child)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m qualgentbench.report_quality",
        description="Write quality.json beside result.json for every journey episode "
                    "under a run directory. Reads saved episodes only; never changes a "
                    "score.")
    ap.add_argument("run_dir", type=Path,
                    help="a runs directory, one run's directory, or one episode directory")
    ap.add_argument("--quality-judge", action="store_true",
                    help="also write the G5 side column (steps accurate) from each "
                         "episode's saved quality_judge.json. Reads saved judge "
                         "results only; never calls a model. Off by default.")
    args = ap.parse_args(argv)
    if not args.run_dir.is_dir():
        print(f"error: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    written = skipped = 0
    for ep in episode_dirs(args.run_dir):
        try:
            write_quality(ep, judge=args.quality_judge)
            written += 1
        except QualityError as exc:
            skipped += 1
            print(f"skipped: {exc}", file=sys.stderr)
    print(f"quality v{QUALITY_VERSION}: wrote {written} {QUALITY_FILE}, skipped {skipped}")
    if written == 0:
        print(f"error: no journey episode found under {args.run_dir}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
