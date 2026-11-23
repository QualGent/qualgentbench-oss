"""Report quality for saved journey episodes: a block BESIDE the ranking score (QUA-3584).

The journey scorer (`journey.journey_verdict`) decides whether a report is a catch. It
says nothing about whether a developer could act on the report. This module scores that,
one yes/no gate at a time, and writes the answer to `quality.json` next to the episode's
`result.json`.

Three rules keep it apart from the ranking:

* It only READS `result.json` (and, for later gates, the other saved artifacts). It never
  writes `result.json` and never changes a verdict, a rate or a ranking key.
* The scoring modules never import it, so it is not in the pinned scorer closure
  (`tests/scorer_closure.py`) and `journey.SCORER_VERSION` does not move when it changes.
  It imports nothing from the package either: what it knows about a report is what the
  scorer already saved.
* It has its own version, `QUALITY_VERSION`, stamped into every `quality.json`.

A rescore is a pure function of the saved episode: no clock, no host name, no random
order. Scoring the same episode twice writes the same bytes.

    python -m qualgentbench.report_quality <run dir>

walks every episode under `<run dir>` and writes one `quality.json` per episode.
See docs/report-quality.md for the file format and the version rule.
"""

from __future__ import annotations

import argparse
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

    def as_dict(self) -> dict[str, Any]:
        return {"pass": self.passed, "reason": self.reason}


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


#: Every gate, in the order it is evaluated and listed. Later tickets append here.
GATES: tuple[Gate, ...] = (
    Gate("G1", "valid", gate_valid),
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
    return {
        "reports": len(reports),
        "neutral": len(reports) - len(scored),
        "scored": len(scored),
        "gates": gates,
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


def score_episode(episode_dir: Path | str) -> dict[str, Any]:
    """The quality block of one saved journey episode, as `quality.json` holds it. Reads
    only; `write_quality` saves it. Raises `QualityError` for a directory that is not a
    scorable journey episode."""
    episode = load_episode(episode_dir)
    metrics = episode.metrics
    reports = [_score_report(episode, r, i) for i, r in enumerate(metrics["reports"])]
    return {
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


def render(quality: dict[str, Any]) -> str:
    """The exact text of `quality.json`: sorted keys, two-space indent, one final
    newline. One function, so every writer produces the same bytes."""
    return json.dumps(quality, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_quality(episode_dir: Path | str) -> Path:
    """Score one episode and write `quality.json` beside its `result.json`, through a
    temp file and `os.replace` so a reader never sees half a file. Touches no other
    file. Returns the path written."""
    episode_dir = Path(episode_dir)
    text = render(score_episode(episode_dir))
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
    args = ap.parse_args(argv)
    if not args.run_dir.is_dir():
        print(f"error: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    written = skipped = 0
    for ep in episode_dirs(args.run_dir):
        try:
            write_quality(ep)
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
