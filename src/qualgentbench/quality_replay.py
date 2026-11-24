"""Replay from the report alone: the measurement behind quality gate G4 (QUA-3591).

A bug report is reproducible when a second agent, given ONLY the report as a customer
would read it, makes the bug happen again. This module runs that second agent (the
replayer) and decides, without a model, whether it did.

A replay episode
    The seeded build of the case the report came from, staged exactly as a journey
    episode stages it (flags, fixtures, clock, precondition), so the device starts in the
    case's own fresh state. The brief is the customer-shaped report plus "reproduce this
    bug". The replayer never gets the test case, the truth file or the first agent's
    transcript, and its working directory names neither the case nor the defect.

The verdict (`decide`), deterministic
    reproduced = the matched defect's canary fired (`verify.canary`: the seeded code path
    reported that it ran) AND one of the defect's truth texts appeared in what the device
    answered the replayer. The model is the actor only; nothing it says is scored.

Two controls decide which defects G4 can speak about at all (`baseline_status`)
    ceiling   the defect's hand-written reference report is replayed the same way,
              `ceiling_attempts` times. Below `ceiling_min` reproductions the defect is
              excluded from G4 for every arm: a failed replay of an agent's report would
              say nothing about that report.
    control   the reference report is replayed with its steps deleted. If that still
              reproduces, the defect is too easy to stumble on and is excluded too.

What is saved
    `<replay dir>/<opaque id>/<episode>/result.json`  one per replay episode, task type
        `report_replay` (never `journey_case`: no board, rescore or rate reads it).
    `<replay dir>/baseline.json`                      ceiling and control per defect.
    `<episode>/quality_replay.json`                   beside each scored journey episode's
        `result.json`: one entry per valid report. `report_quality` reads this file for
        G4 and never starts a replay itself.

The saved replay episodes are the state. `fold` rebuilds `baseline.json` and every
`quality_replay.json` from them, so episodes produced on several machines can be merged
into one replay directory and folded once.

    qualgent-bench run --mode journey --quality-replay <run dir> --agent A --models M ...
    python -m qualgentbench.quality_replay plan <run dir>     # no device, no agent
    python -m qualgentbench.quality_replay fold <run dir> --agent A --model M \\
        [--provider P] [--tooling mcp|raw]                    # no device, no agent

See docs/report-quality.md, "G4: replay from the report".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import brief as _brief
from . import corpus, failures, journey, pricing, reference_reports
from . import report_quality as rq
from .result import VerifierResult
from .task import BenchmarkTask
from .transcript import TranscriptParser

#: Bump when a change can alter a saved replay verdict or what the replayer is given:
#: the brief, the verdict rule, the budget rule. Episodes of different versions are never
#: folded together.
#:
#:   1  canary AND truth text; canary alone for a defect with no text to look for.
#:   2  the brief of an agent's report gives the reproduction steps the customer sees
#:      (`repro_steps`) when the report has them, not the bug's `steps` text, which is
#:      the summary repeated for a report with no code investigation (QUA-3607). No
#:      replay was run under version 1.
REPLAY_VERSION = 2

#: `result.json`'s `task_type` for a replay episode. Not `journey.TASK_TYPE`, which is
#: the one value every board, rescore and rate filters on.
TASK_TYPE = "report_replay"
#: The key of the replay block in a replay task's spec and in its saved metrics.
KEY = "report_replay"

REPLAY_FILE = rq.REPLAY_FILE             # "quality_replay.json", beside result.json
REPLAY_DIR = rq.REPLAY_DIR               # "quality-replay", the default replay directory
BASELINE_FILE = "baseline.json"
CONFIG_FILE = Path(__file__).parent / "data" / "quality-replay.json"

REPORT, CEILING, CONTROL = "report", "ceiling", "control"

# Why a defect is out of G4 for every arm.
NO_REFERENCE = "no_reference"      # the case carries no reference report for the defect
NO_CANARY = "no_canary"            # the build's source has no canary call for the defect
NO_TRUTH_TEXT = "no_truth_text"    # nothing to look for on screen, and policy `exclude`
CANARY_SILENT = "canary_silent"    # the symptom was seen but the canary never fired
BELOW_CEILING = "ceiling"          # the reference report does not replay often enough
CONTROL_REPRODUCES = "control"     # it reproduces with the steps deleted
#: Exclusions that mean "no verdict can be given", as opposed to "the defect is unfit".
VERDICT_UNAVAILABLE = (NO_REFERENCE, NO_CANARY, NO_TRUTH_TEXT, CANARY_SILENT)

CANARY_AND_TEXT = "canary_and_text"
CANARY_ONLY = "canary_only"
#: Truth kinds (`reference_reports.truth_texts`) that give a replay a text to find on
#: the screen. `absence` (the defect only removes text) and `state` (the truth row
#: measured no text) give none.
TEXT_KINDS = ("side", "screen", "death")

_SAID_RE = re.compile(r"RESULT:\s*reproduced\s*=\s*(yes|no)", re.IGNORECASE)


class ReplayError(ValueError):
    """The replay cannot be planned or folded as asked. The message says why."""


# ── configuration ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReplayConfig:
    """Every knob of the replay. The defaults are the spec's decisions; the packaged
    `data/quality-replay.json` is the pinned copy a board is replayed under."""
    #: The replayer (D5): one agent and model for every arm. None until the pilot picks.
    agent: str | None = None
    model: str | None = None
    provider: str | None = None
    #: "mcp" or "raw": how the replayer drives the device.
    tooling: str = "mcp"
    #: D3. False: the replayer gets the report only. True adds the case's name, steps
    #: and expected outcome to the brief.
    give_test_case: bool = False
    #: D4. Replays per report; a report is reproduced when any attempt reproduces.
    attempts: int = 1
    #: Replays of each reference report, and how many must reproduce.
    ceiling_attempts: int = 3
    ceiling_min: int = 2
    #: Stop replaying a reference once the outcome is decided (2 of 2, or 0 of 2).
    ceiling_early_stop: bool = True
    #: Replays of each reference report with its steps deleted.
    control_attempts: int = 1
    #: The replayer's tool-call budget: `budget_factor` x the case's reference route
    #: length, never below `budget_floor`.
    budget_factor: float = 2.0
    budget_floor: int = 0
    #: A defect with no text to find on screen: `canary_only` scores it on the canary
    #: alone; `exclude` leaves it out of G4.
    textless: str = CANARY_ONLY
    #: How often a replay that did not run (staging failed, provider limit) is retried.
    max_retries: int = 1

    def __post_init__(self) -> None:
        if self.textless not in (CANARY_ONLY, "exclude"):
            raise ReplayError(f"textless must be {CANARY_ONLY!r} or 'exclude', "
                              f"got {self.textless!r}")
        if self.tooling not in ("mcp", "raw"):
            raise ReplayError(f"tooling must be 'mcp' or 'raw', got {self.tooling!r}")
        if not 1 <= self.ceiling_min <= self.ceiling_attempts:
            raise ReplayError("ceiling_min must be between 1 and ceiling_attempts")
        if self.attempts < 1 or self.control_attempts < 0 or self.budget_factor <= 0:
            raise ReplayError("attempts must be >= 1, control_attempts >= 0 and "
                              "budget_factor > 0")

    def fingerprint(self) -> dict[str, Any]:
        """What a replay EPISODE was run under. Two episodes are comparable, and are
        folded together, only when this is equal."""
        return {"replay_version": REPLAY_VERSION, "agent": self.agent, "model": self.model,
                "provider": self.provider, "tooling": self.tooling,
                "give_test_case": self.give_test_case,
                "budget_factor": self.budget_factor, "budget_floor": self.budget_floor,
                "textless": self.textless}

    def stamp(self) -> dict[str, Any]:
        """The fingerprint plus the fold rules: what `quality.json` records."""
        return {**self.fingerprint(), "attempts": self.attempts,
                "ceiling_attempts": self.ceiling_attempts, "ceiling_min": self.ceiling_min,
                "control_attempts": self.control_attempts}

    @property
    def pinned(self) -> bool:
        return bool(self.agent and self.model)


def load_config(path: Path | str | None = None) -> ReplayConfig:
    """The replay configuration: the packaged pin, or the JSON file at `path`."""
    path = Path(path) if path else CONFIG_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"unreadable replay config {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise ReplayError(f"{path} must hold one JSON object")
    known = set(ReplayConfig.__dataclass_fields__)
    if unknown := sorted(k for k in raw if k not in known and not k.startswith("_")):
        raise ReplayError(f"{path}: unknown key(s) {unknown}")
    return ReplayConfig(**{k: v for k, v in raw.items() if k in known})


def with_replayer(config: ReplayConfig, *, agent: str | None, model: str | None,
                  provider: str | None = None, tooling: str | None = None) -> ReplayConfig:
    """`config` with the replayer named on the command line. A pinned config refuses a
    different replayer: one replayer for every arm is the point of the pin."""
    asked = {"agent": agent, "model": model, "provider": provider, "tooling": tooling}
    for key, value in asked.items():
        pinned = getattr(config, key)
        if config.pinned and value and pinned and value != pinned:
            raise ReplayError(
                f"the replayer is pinned to {key}={pinned!r}; {value!r} was asked for. "
                "Every arm is replayed by the same replayer. Pass another config file to "
                "run an experiment under a different one.")
    return replace(config, **{k: v for k, v in asked.items() if v})


# ── units ──────────────────────────────────────────────────────────────────────

def defect_key(app_id: str, case_id: str, defect_id: str) -> str:
    return f"{app_id}/{case_id}/{defect_id}"


@dataclass(frozen=True)
class ReplayUnit:
    """One replay episode to run: which report, on which case, for which defect."""
    kind: str                              # REPORT | CEILING | CONTROL
    app_id: str
    case_id: str
    defect_id: str
    attempt: int                           # 1-based, among valid attempts of this unit
    #: The five customer-shaped fields. `steps` is a list for a reference report and for
    #: an agent's report that carries reproduction steps (`replay_fields`), else the
    #: bug's steps text.
    report: dict[str, Any] = field(compare=False, hash=False, default_factory=dict)
    #: For REPORT: the journey episode (relative to the run dir) and the report's index.
    source_episode: str | None = None
    report_index: int | None = None

    @property
    def defect_key(self) -> str:
        return defect_key(self.app_id, self.case_id, self.defect_id)

    @property
    def target(self) -> str:
        """What is replayed, without the attempt number."""
        if self.kind == REPORT:
            return f"{REPORT}:{self.source_episode}#{self.report_index}"
        return f"{self.kind}:{self.defect_key}"

    @property
    def key(self) -> str:
        return f"{self.target}@{self.attempt}"

    @property
    def task_id(self) -> str:
        """Opaque: it names the replayer's working directory, so it must say nothing
        about the case or the defect."""
        return "replay-" + hashlib.sha256(self.target.encode()).hexdigest()[:12]

    def identity(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "target": self.target,
                "app_id": self.app_id, "case_id": self.case_id,
                "defect_id": self.defect_id, "attempt": self.attempt,
                "source_episode": self.source_episode, "report_index": self.report_index}


# ── the brief ──────────────────────────────────────────────────────────────────

_REPORT_FIELDS = ("summary", "steps", "expected", "actual", "failure_step")


def report_fields(report: dict[str, Any]) -> dict[str, Any]:
    """The five fields of a customer-shaped report, and nothing else (an agent's own
    step list, where the report came from: none of that is on the bug)."""
    return {k: report.get(k) for k in _REPORT_FIELDS}


def replay_fields(report: dict[str, Any]) -> dict[str, Any]:
    """An agent's customer-shaped report as it is replayed: what the customer sees.

    The reproduction steps are the bug's `repro_steps` (the agent's own step list, as
    the bug stores it) when the report has them. Only a report without them falls back
    to the bug's `steps` text, which for a report-tool bug with no code investigation
    is the summary repeated: replaying that text for a report that DID list its steps
    would be the evidence-removed control, and G4 would fail every arm (QUA-3607)."""
    fields = report_fields(report)
    repro = [str(s) for s in report.get("repro_steps") or [] if str(s).strip()]
    if repro:
        fields["steps"] = repro
    return fields


#: A step that already carries its own number ("2. Tap Save", "Step 2: Tap Save").
_NUMBERED_STEP_RE = re.compile(r"^\s*(?:step\s+)?\d{1,2}\s*[.):]", re.IGNORECASE)


def render_report(report: dict[str, Any]) -> str:
    """The report as the replayer reads it: the bug's fields, in the order a tracker
    shows them. Deleted steps are shown as deleted, not hidden. A listed step keeps the
    agent's words; it is numbered here unless it is numbered already."""
    steps = report.get("steps")
    if isinstance(steps, (list, tuple)):
        body = "\n".join(f"  {s}" if _NUMBERED_STEP_RE.match(str(s)) else f"  {i}. {s}"
                         for i, s in enumerate(steps, 1))
    else:
        body = "\n".join(f"  {line}" for line in str(steps or "").splitlines() if line.strip())
    lines = [f"Title: {report.get('summary') or '(none)'}",
             "Steps to reproduce:" + (f"\n{body}" if body else " (the report gives none)"),
             f"Expected result: {report.get('expected') or '(not given)'}",
             f"Actual result: {report.get('actual') or '(not given)'}"]
    if report.get("failure_step"):
        lines.append(f"Failed at: {report['failure_step']}")
    return "\n".join(lines)


def brief(task: BenchmarkTask, device_serial: str, tooling: str) -> str:
    """The replayer's assignment: the report, and the instruction to reproduce it. The
    same text for every arm's reports and for the reference reports."""
    block = (task.bug_spec or {}).get(KEY) or {}
    tooling_note = _brief.tooling_note(tooling, device_serial)
    case = block.get("test_case")
    case_text = ""
    if case:
        steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(case.get("steps") or [], 1))
        case_text = (f"\n## The test case the report came from\n{case.get('name')}\n"
                     f"Steps:\n{steps}\nExpected outcome: {case.get('expected_outcome')}\n")
    return f"""You are a QA engineer reproducing a bug report on the Android app `{task.app_name}`.

## Device & App
The app (`{task.bundle_id}`) is installed and running on Android device `{device_serial}`.
{tooling_note}

## Important
You do NOT have the app's source code — there is nothing to read. The bug report below
is all you have. Reproduce the bug it describes ONLY by interacting with the running app
on the device. The app's data is in a fresh starting state. When you reach the reported
behaviour, read the screen that shows it before you finish. Do not write a new report.

## Bug report
{render_report(block.get('report') or {})}
{case_text}
## When you are done
Finish your FINAL message with one line:
  RESULT: reproduced=<yes|no>
"""


# ── the verdict ────────────────────────────────────────────────────────────────

def verdict_basis(truth_kind: str, truth_texts: Iterable[str]) -> str:
    """How a defect's replay is judged: on the canary and a text, or, when the truth
    gives no text a screen could show, on the canary alone."""
    return CANARY_AND_TEXT if truth_kind in TEXT_KINDS and list(truth_texts) else CANARY_ONLY


def decide(*, defect_id: str, truth_kind: str, truth_texts: list[str],
           fired: list[str] | None, device_results: list[str], app_crashes: int = 0,
           textless: str = CANARY_ONLY) -> dict[str, Any]:
    """The replay verdict. Pure: what the harness read off the device after the replayer
    exited (`fired`, `app_crashes`) and what the device answered it (`device_results`,
    as `journey._device_texts(..., results_only=True)` gives them).

    `reproduced` is None when no verdict can be given: the canary could not be read, or
    the defect has no text to find and the policy is `exclude`."""
    basis = verdict_basis(truth_kind, truth_texts)
    seen = [t for t in truth_texts
            if any(reference_reports._quotes(t, d) for d in device_results)]
    source = "device_text" if seen else None
    if not seen and truth_kind == "death" and app_crashes > 0:
        # The harness's own crash record for the app under test, read over its own adb.
        source = "harness_crash"
    symptom = source is not None
    out: dict[str, Any] = {"basis": basis, "truth_kind": truth_kind,
                           "canary_fired": None if fired is None else defect_id in fired,
                           "symptom_seen": symptom if basis == CANARY_AND_TEXT else None,
                           "symptom_source": source if basis == CANARY_AND_TEXT else None,
                           "texts_seen": seen[:5]}
    if fired is None:
        return {**out, "reproduced": None, "reason": "the canary could not be read"}
    canary = defect_id in fired
    if basis == CANARY_ONLY:
        if textless != CANARY_ONLY:
            return {**out, "reproduced": None,
                    "reason": "the defect has no text to find on screen"}
        return {**out, "reproduced": canary,
                "reason": ("the canary fired (no text to find for this defect)" if canary
                           else "the canary did not fire")}
    if canary and symptom:
        how = "its text appeared" if source == "device_text" else "the app's crash was recorded"
        return {**out, "reproduced": True, "reason": f"the canary fired and {how}"}
    if canary:
        return {**out, "reproduced": False,
                "reason": "the canary fired but none of the defect's texts appeared"}
    if symptom:
        return {**out, "reproduced": False,
                "reason": "a text of the defect appeared but the canary did not fire"}
    return {**out, "reproduced": False,
            "reason": "the canary did not fire and none of the defect's texts appeared"}


def replay_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    """`EpisodeOptions.verdict_fn` for a replay episode. Reads the replay block the task
    was built with and what the harness recorded on the spec after the replayer exited.
    The saved metrics carry no `reports` list, so nothing that scores journey reports
    can read this episode as one."""
    from .bugs import _bash_adb_events, _count_tool_calls, _device_actions
    from .contamination import scan as contamination_scan

    spec = task.bug_spec or {}
    block = spec.get(KEY) or {}
    unit = block.get("unit") or {}
    tooling = str(spec.get("tooling") or "mcp")
    parser = TranscriptParser(transcript)
    contamination = contamination_scan(parser, spec.get("workspace"),
                                       devloop_roots=spec.get("devloop_roots"),
                                       nonce=spec.get("flag_nonce"),
                                       adbd_at_end=spec.get("adbd_at_end"))
    device_actions = _device_actions(parser, tooling)
    metered = spec.get("metered_total")
    if isinstance(metered, int) and metered > 0:
        device_actions = max(device_actions, metered)
    if tooling == "raw":
        evidence = len(_bash_adb_events(parser)) >= 1
    else:
        evidence = (len(parser.observation_texts()) >= 1
                    and len(parser.successful_device_events()) >= 1)

    fired = spec.get("fired")
    answer = decide(defect_id=str(unit.get("defect_id") or ""),
                    truth_kind=str(block.get("truth_kind") or ""),
                    truth_texts=list(block.get("truth_texts") or []),
                    fired=list(fired) if isinstance(fired, list) else None,
                    device_results=journey._device_texts(transcript, tooling,
                                                         results_only=True),
                    app_crashes=int(spec.get("app_crash_count") or 0),
                    textless=str((block.get("replayer") or {}).get("textless") or CANARY_ONLY))
    said = _SAID_RE.findall(transcript)
    steps = spec.get("hook_steps") or device_actions or _count_tool_calls(transcript)
    budget = spec.get("step_budget")
    metrics = {
        "app_id": spec.get("app_id"),
        "case_id": spec.get("case_id"),
        KEY: {"unit": unit, "replayer": block.get("replayer"), **answer,
              # What the replayer claimed. Recorded, never scored.
              "agent_said": said[-1].lower() if said else None},
        "bugs_present": list(spec.get("active_bugs") or []),
        "fault_fired": fired,
        "app_crashes": int(spec.get("app_crash_count") or 0),
        "hook_steps": spec.get("hook_steps"),
        "steps": steps,
        "step_budget": budget,
        "budget_used": round(steps / budget, 4) if budget and steps else None,
        "device_actions": device_actions,
        "truncated": bool(spec.get("truncated")),
        "timed_out": bool(spec.get("timed_out")),
        "device_serial": spec.get("device_serial"),
        "off_app": bool(spec.get("off_app")),
        "infra_failure": device_actions == 0 and not evidence,
        "env_failure": (bool(spec.get("staging_failed"))
                        or (bool(spec.get("exit_code")) and not said
                            and not spec.get("truncated"))),
        "staging_failed": spec.get("staging_failed") or "",
        "reported_status": ("REPRODUCED" if answer["reproduced"] else "NOT_REPRODUCED"),
        **contamination.as_metrics(),
        **pricing.usage_metrics(model, parser.token_usage()),
    }
    reproduced = answer["reproduced"] is True
    return VerifierResult(passed=reproduced, score=1.0 if reproduced else 0.0,
                          weighted_score=1.0 if reproduced else 0.0,
                          criteria={"canary_fired": answer["canary_fired"] is True,
                                    "symptom_seen": answer["symptom_seen"] is True},
                          failure_reason=None if reproduced else answer["reason"],
                          metrics=metrics)


# ── building a replay task ─────────────────────────────────────────────────────

def step_budget(ref_steps: int | None, case_budget: int | None,
                config: ReplayConfig) -> int:
    """The replayer's tool-call budget: `budget_factor` x the length of the case's
    reference route (`check.steps`, the route the corpus derives the case on), at least
    `budget_floor`. A case with no recorded route keeps its own journey budget."""
    if not ref_steps:
        return max(int(case_budget or 40), config.budget_floor)
    return max(math.ceil(config.budget_factor * int(ref_steps)), config.budget_floor, 1)


def _case(app_id: str, case_id: str) -> tuple[dict, dict, dict[str, dict]]:
    """(the case, its truth row, the app's defects), or ReplayError."""
    doc = journey.load_cases(app_id)
    if not doc:
        raise ReplayError(f"no journey test cases for app {app_id!r}")
    case = next((c for c in doc.get("test_cases") or [] if str(c.get("id")) == case_id), None)
    if case is None:
        raise ReplayError(f"no journey case {case_id!r} in {app_id}")
    return case, journey.load_truth(app_id).get(case_id) or {}, journey.load_defects(doc)


def build_task(unit: ReplayUnit, suite: dict[str, Any], config: ReplayConfig) -> BenchmarkTask:
    """The replay episode for `unit`: the seeded journey task of its case, with the
    replay block added, the replay budget, and an opaque task id. Staging reads the same
    spec keys as a journey episode, so the device state is the case's own."""
    base = next((t for t in journey.journey_tasks(suite)
                 if (t.bug_spec or {}).get("case_id") == unit.case_id
                 and (t.bug_spec or {}).get("version") == "seeded"), None)
    if base is None:
        raise ReplayError(f"{unit.app_id}: case {unit.case_id!r} has no seeded version")
    case, row, defects = _case(unit.app_id, unit.case_id)
    truth = reference_reports.truth_texts(case, defects, row, unit.defect_id, base.app_name)
    spec = dict(base.bug_spec or {})
    spec["step_budget"] = step_budget(spec.get("ref_steps"), spec.get("step_budget"), config)
    spec[KEY] = {"unit": unit.identity(), "report": report_fields(unit.report),
                 "replayer": config.fingerprint(), "truth_kind": truth["kind"],
                 "truth_texts": list(truth["actual"]) if truth["kind"] in TEXT_KINDS else []}
    if config.give_test_case:
        spec[KEY]["test_case"] = {"name": spec.get("name"), "steps": spec.get("steps"),
                                  "expected_outcome": spec.get("expected_outcome")}
    return BenchmarkTask(id=unit.task_id, name="report replay", instruction="",
                         app_file_id="", app_name=base.app_name, platform=base.platform,
                         bundle_id=base.bundle_id, bug_spec=spec)


# ── what a defect's truth allows, without a device ─────────────────────────────

_FIRED_RE = re.compile(r"""fired\(\s*["']([^"']+)["']\s*\)""")


def canary_ids(app_id: str) -> set[str]:
    """Defect ids with a `QgbFlags.fired("<id>")` call in the app's build spec."""
    path = corpus.spec_path(app_id)
    return set(_FIRED_RE.findall(path.read_text(encoding="utf-8"))) if path.exists() else set()


def _truth_recorded_fired(row: dict, defect_id: str) -> bool:
    """Did the truth row's seeded passes record this defect's canary? A row derived
    before passes kept their markers records none; that is not evidence of a missing
    canary, only of no record."""
    seeded = [(row.get("passes") or {}).get("seeded") or {},
              *((row.get("trials") or {}).get("seeded") or [])]
    return any(defect_id in (p.get("fired") or []) for p in seeded if isinstance(p, dict))


@dataclass(frozen=True)
class DefectFacts:
    """What the repository says about replaying one (case, defect), device-free."""
    app_id: str
    case_id: str
    defect_id: str
    truth_kind: str                 # side | screen | absence | death | state | ""
    basis: str                      # CANARY_AND_TEXT | CANARY_ONLY
    has_reference: bool
    has_canary: bool                # a canary call exists in the build spec
    truth_fired: bool               # the truth row recorded the canary on a seeded pass
    ref_steps: int | None
    case_budget: int | None

    @property
    def key(self) -> str:
        return defect_key(self.app_id, self.case_id, self.defect_id)

    def static_exclusion(self, config: ReplayConfig) -> str | None:
        if not self.has_reference:
            return NO_REFERENCE
        if not self.has_canary:
            return NO_CANARY
        if self.basis == CANARY_ONLY and config.textless != CANARY_ONLY:
            return NO_TRUTH_TEXT
        return None


def defect_facts(app_id: str, case_id: str, defect_id: str,
                 app_name: str = "") -> tuple[DefectFacts, reference_reports.ReferenceReport | None]:
    case, row, defects = _case(app_id, case_id)
    ref = reference_reports.case_references(case, app_id).get(defect_id)
    kind, texts = "", []
    if defect_id in defects and defect_id in {b["id"] for b in journey.case_bugs(case)}:
        truth = reference_reports.truth_texts(case, defects, row, defect_id, app_name or app_id)
        kind = truth["kind"]
        texts = truth["actual"] if kind in TEXT_KINDS else []
    return DefectFacts(
        app_id=app_id, case_id=case_id, defect_id=defect_id, truth_kind=kind,
        basis=verdict_basis(kind, texts), has_reference=ref is not None,
        has_canary=defect_id in canary_ids(app_id),
        truth_fired=_truth_recorded_fired(row, defect_id),
        ref_steps=len(((case.get("check") or {}).get("steps")) or []) or None,
        case_budget=int(case.get("step_budget") or 0) or None), ref


def survey(*, heldout: bool | None = False) -> list[DefectFacts]:
    """`DefectFacts` for every reference report of the corpus, in a stable order."""
    names = _app_names()
    return [defect_facts(r.app_id, r.case_id, r.defect_id, names.get(r.app_id, ""))[0]
            for r in reference_reports.load_all(heldout=heldout)]


#: The order pilot defects are drawn in. Every way a defect shows itself is covered, and
#: the kinds a replay can check on screen, which are most of the corpus, come up most.
PILOT_CYCLE = ("death", "screen", "side", "state", "death", "screen", "side", "absence",
               "death", "screen")


def select_pilot(facts: list[DefectFacts], n: int = 10) -> list[DefectFacts]:
    """`n` defects for the replayer pilot, the same ones on every machine: truth kinds
    in the order of `PILOT_CYCLE`, within a kind in order of a hash of the defect key,
    and no app twice while another app is still unused. Only defects with a reference
    report and a canary are drawn."""
    usable = [f for f in facts if f.has_reference and f.has_canary]
    pools = {k: sorted((f for f in usable if f.truth_kind == k),
                       key=lambda f: hashlib.sha256(f.key.encode()).hexdigest())
             for k in set(PILOT_CYCLE)}
    all_apps = {f.app_id for f in usable}
    picked: list[DefectFacts] = []
    while len(picked) < n and any(pools.values()):
        for kind in PILOT_CYCLE:
            pool = pools[kind]
            if not pool or len(picked) >= n:
                continue
            used = {f.app_id for f in picked}
            fresh = [f for f in pool if f.app_id not in used]
            choice = (fresh or pool)[0] if used != all_apps else pool[0]
            pool.remove(choice)
            picked.append(choice)
    return picked


def _app_names() -> dict[str, str]:
    from . import bugs
    return {str(s["app"].get("id")): str(s["app"].get("name") or s["app"].get("id"))
            for s in bugs.load_apps()}


# ── reading saved replay episodes ──────────────────────────────────────────────

@dataclass(frozen=True)
class Attempt:
    """One saved replay episode, as the fold reads it."""
    unit: dict[str, Any]
    replayer: dict[str, Any]
    episode: str                    # relative to the replay directory
    reproduced: bool | None
    basis: str | None
    canary_fired: bool | None
    symptom_seen: bool | None
    reason: str
    invalid: str                    # why it is not a measurement, or ""
    steps: int | None = None
    step_budget: int | None = None
    truncated: bool = False
    cost_usd: float | None = None
    wall_time_sec: float | None = None
    ended_at: str = ""

    @property
    def valid(self) -> bool:
        return not self.invalid and self.reproduced is not None

    def as_dict(self) -> dict[str, Any]:
        return {"episode": self.episode, "reproduced": self.reproduced,
                "canary_fired": self.canary_fired, "symptom_seen": self.symptom_seen,
                "reason": self.invalid or self.reason, "valid": self.valid,
                "steps": self.steps, "step_budget": self.step_budget,
                "truncated": self.truncated, "cost_usd": self.cost_usd,
                "wall_time_sec": self.wall_time_sec}


def attempt_from_result(result: dict[str, Any], episode: str) -> Attempt | None:
    """A saved `result.json` as an `Attempt`, or None when it is not a replay episode."""
    metrics = result.get("metrics") if isinstance(result, dict) else None
    if result.get("task_type") != TASK_TYPE or not isinstance(metrics, dict):
        return None
    block = metrics.get(KEY)
    if not isinstance(block, dict) or not isinstance(block.get("unit"), dict):
        return None
    cost = metrics.get("cost_usd")
    wall = result.get("wall_time_sec")
    return Attempt(
        unit=block["unit"], replayer=block.get("replayer") or {}, episode=episode,
        reproduced=block.get("reproduced"), basis=block.get("basis"),
        canary_fired=block.get("canary_fired"), symptom_seen=block.get("symptom_seen"),
        reason=str(block.get("reason") or ""),
        invalid=failures.exclusion_reason(metrics),
        steps=metrics.get("steps"), step_budget=metrics.get("step_budget"),
        truncated=bool(metrics.get("truncated")),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        wall_time_sec=float(wall) if isinstance(wall, (int, float)) else None,
        ended_at=str(result.get("ended_at") or ""))


def collect(replay_dir: Path | str) -> list[Attempt]:
    """Every replay episode saved under `replay_dir`, oldest first."""
    replay_dir = Path(replay_dir)
    out: list[Attempt] = []
    if not replay_dir.is_dir():
        return out
    for path in sorted(replay_dir.glob("*/*/" + rq.RESULT_FILE)):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        attempt = attempt_from_result(result, str(path.parent.relative_to(replay_dir)))
        if attempt is not None:
            out.append(attempt)
    return sorted(out, key=lambda a: (a.ended_at, a.episode))


# ── the reports a run needs replayed ───────────────────────────────────────────

@dataclass(frozen=True)
class ReportRef:
    """One valid report of a saved journey episode."""
    episode: str                    # the episode directory, relative to the run dir
    index: int
    app_id: str
    case_id: str
    defect_id: str
    report: dict[str, Any] = field(compare=False, hash=False, default_factory=dict)

    @property
    def defect_key(self) -> str:
        return defect_key(self.app_id, self.case_id, self.defect_id)

    @property
    def target(self) -> str:
        return f"{REPORT}:{self.episode}#{self.index}"


def valid_reports(run_dir: Path | str) -> list[ReportRef]:
    """Every report under `run_dir` that passes G1 (the scorer matched it to a seeded
    defect), with its customer-shaped text. Only journey episodes are read; the default
    replay directory inside the run dir is not searched."""
    run_dir = Path(run_dir)
    out: list[ReportRef] = []
    for ep in rq.episode_dirs(run_dir):
        try:
            episode = rq.load_episode(ep)
        except rq.QualityError:
            continue
        if episode.result.get("task_type") != journey.TASK_TYPE:
            continue
        metrics = episode.metrics
        app_id, case_id = str(metrics.get("app_id") or ""), str(metrics.get("case_id") or "")
        if not app_id or not case_id:
            continue
        shaped = rq.customer_report(episode)
        rel = str(ep.relative_to(run_dir)) if ep != run_dir else "."
        for i, report in enumerate(metrics["reports"]):
            if report.get("matched") and not rq.is_neutral(report):
                out.append(ReportRef(episode=rel, index=i, app_id=app_id, case_id=case_id,
                                     defect_id=str(report["matched"]),
                                     report=replay_fields(shaped[i].as_dict())))
    return out


# ── ceiling and control ────────────────────────────────────────────────────────

def baseline_status(facts: DefectFacts, ceiling: list[Attempt], control: list[Attempt],
                    config: ReplayConfig, *, gave_up: bool = False) -> dict[str, Any]:
    """Where one defect stands: excluded (and why), included, or not decided yet.

    `ceiling` and `control` are the VALID attempts, oldest first. `need` names the next
    replay that would move the decision (CEILING or CONTROL), or None when it is made.
    `gave_up`: a needed replay failed to run more often than `max_retries` allows."""
    ceiling = ceiling[:config.ceiling_attempts]
    control = control[:config.control_attempts]
    k = sum(1 for a in ceiling if a.reproduced)
    c = sum(1 for a in control if a.reproduced)
    out: dict[str, Any] = {
        "app_id": facts.app_id, "case_id": facts.case_id, "defect_id": facts.defect_id,
        "truth_kind": facts.truth_kind, "basis": facts.basis,
        "ceiling": {"reproduced": k, "attempts": len(ceiling)},
        "control": {"reproduced": c, "attempts": len(control)},
        "excluded": None, "decided": True, "need": None, "reason": ""}

    def excluded(code: str, reason: str) -> dict[str, Any]:
        return {**out, "excluded": code, "reason": reason}

    static = facts.static_exclusion(config)
    if static == NO_REFERENCE:
        return excluded(static, "the case has no reference report for this defect")
    if static == NO_CANARY:
        return excluded(static, "the build has no canary for this defect: verdict unavailable")
    if static == NO_TRUTH_TEXT:
        return excluded(static, "the defect leaves no text to find on screen")

    n, total, floor = len(ceiling), config.ceiling_attempts, config.ceiling_min
    passed = k >= floor
    failed = k + (total - n) < floor
    settled = n >= total or (config.ceiling_early_stop and (passed or failed))
    if not settled:
        if gave_up:
            return {**out, "decided": False,
                    "reason": "the reference replay could not be run: no ceiling"}
        return {**out, "decided": False, "need": CEILING,
                "reason": f"ceiling not measured yet ({n} of {total} replays)"}
    if not passed:
        if any(a.symptom_seen for a in ceiling) and not any(a.canary_fired for a in ceiling):
            return excluded(CANARY_SILENT,
                            "the reference replay showed the defect's text but its canary "
                            "never fired: verdict unavailable")
        return excluded(BELOW_CEILING, f"its reference report reproduced {k} of {n} times "
                                       f"(needs {floor} of {total})")
    if len(control) < config.control_attempts:
        if gave_up:
            return {**out, "decided": False,
                    "reason": "the control replay could not be run: no control"}
        return {**out, "decided": False, "need": CONTROL,
                "reason": "evidence-removed control not measured yet"}
    if c:
        return excluded(CONTROL_REPRODUCES,
                        "it reproduces from its reference report with the steps deleted")
    return {**out, "reason": f"ceiling {k} of {n}; control did not reproduce"}


# ── the state of a replay: what is saved, what is next ─────────────────────────

class ReplayState:
    """Everything known about one replay: the reports a run needs, the reference
    reports, and the replay episodes saved so far. Pure bookkeeping; no device."""

    def __init__(self, config: ReplayConfig, reports: list[ReportRef],
                 attempts: list[Attempt], *, extra_defects: Iterable[tuple[str, str, str]] = (),
                 app_names: dict[str, str] | None = None) -> None:
        self.config = config
        self.reports = list(reports)
        names = app_names if app_names is not None else _app_names()
        self.facts: dict[str, DefectFacts] = {}
        self.refs: dict[str, reference_reports.ReferenceReport | None] = {}
        triples = [(r.app_id, r.case_id, r.defect_id) for r in self.reports]
        for app_id, case_id, defect_id in [*triples, *extra_defects]:
            key = defect_key(app_id, case_id, defect_id)
            if key not in self.facts:
                self.facts[key], self.refs[key] = defect_facts(
                    app_id, case_id, defect_id, names.get(app_id, ""))
        self.attempts: list[Attempt] = []
        self.other_replayer = 0
        #: The distinct fingerprints of the episodes counted in `other_replayer`.
        self.other_fingerprints: list[dict[str, Any]] = []
        for a in attempts:
            self.add(a)

    def add(self, attempt: Attempt) -> None:
        """Take one saved episode. An episode of another replayer or replay version is
        counted and otherwise ignored: it measured something else."""
        if attempt.replayer != self.config.fingerprint():
            self.other_replayer += 1
            if attempt.replayer not in self.other_fingerprints:
                self.other_fingerprints.append(attempt.replayer)
            return
        self.attempts.append(attempt)

    def other_replayer_text(self) -> str:
        """How the ignored episodes' replayer differs from this one, in words."""
        mine = self.config.fingerprint()
        parts = []
        for theirs in self.other_fingerprints:
            keys = sorted(k for k in {*mine, *theirs} if mine.get(k) != theirs.get(k))
            parts.append(", ".join(f"{k}={theirs.get(k)!r} (here {mine.get(k)!r})"
                                   for k in keys) or "an unreadable fingerprint")
        return "; ".join(parts)

    def _of(self, target: str) -> list[Attempt]:
        return [a for a in self.attempts if a.unit.get("target") == target]

    def _gave_up(self, target: str) -> bool:
        return (sum(1 for a in self._of(target) if not a.valid) > self.config.max_retries)

    def baseline(self, key: str) -> dict[str, Any]:
        ceiling, control = f"{CEILING}:{key}", f"{CONTROL}:{key}"
        return baseline_status(
            self.facts[key], [a for a in self._of(ceiling) if a.valid],
            [a for a in self._of(control) if a.valid], self.config,
            gave_up=self._gave_up(ceiling) or self._gave_up(control))

    def report_entry(self, ref: ReportRef) -> dict[str, Any]:
        """One entry of `quality_replay.json`."""
        base = self.baseline(ref.defect_key)
        tries = self._of(ref.target)
        valid = [a for a in tries if a.valid][:self.config.attempts]
        entry: dict[str, Any] = {
            "index": ref.index, "defect": ref.defect_id, "basis": base["basis"],
            "excluded": base["excluded"], "ceiling": base["ceiling"],
            "control": base["control"], "reproduced": None,
            "attempts": [a.as_dict() for a in tries]}
        if base["excluded"]:
            return {**entry, "status": "excluded", "reason": base["reason"]}
        if not base["decided"]:
            # No replay left to wait for (`need` is None): the ceiling or the control
            # was given up after failing to run. That is "could not be run", not "not
            # run yet": read as pending, one such report kept its whole arm not
            # measured for ever (QUA-3607).
            return {**entry, "status": "pending" if base["need"] else "unavailable",
                    "reason": base["reason"]}
        if any(a.reproduced for a in valid):
            hit = next(a for a in valid if a.reproduced)
            return {**entry, "status": "replayed", "reproduced": True, "reason": hit.reason}
        if len(valid) >= self.config.attempts:
            return {**entry, "status": "replayed", "reproduced": False,
                    "reason": valid[-1].reason}
        if self._gave_up(ref.target):
            return {**entry, "status": "unavailable",
                    "reason": "the replay could not be run: " + (tries[-1].invalid
                                                                 or tries[-1].reason)}
        return {**entry, "status": "pending", "reason": "not replayed yet"}

    def _unit(self, kind: str, key: str) -> ReplayUnit:
        facts, ref = self.facts[key], self.refs[key]
        target = f"{kind}:{key}"
        return ReplayUnit(kind=kind, app_id=facts.app_id, case_id=facts.case_id,
                          defect_id=facts.defect_id,
                          attempt=sum(1 for a in self._of(target) if a.valid) + 1,
                          report=ref.customer_report(with_steps=kind == CEILING))

    def next_units(self) -> list[ReplayUnit]:
        """The replays that can be run NOW, in order: for each defect the one baseline
        replay its decision waits on, then every report of a defect that is in. Running
        one can unlock others, so ask again after each."""
        out: list[ReplayUnit] = []
        for key in self.facts:
            need = self.baseline(key)["need"]
            if need:
                out.append(self._unit(need, key))
        for ref in self.reports:
            if self.report_entry(ref)["status"] == "pending" \
                    and self.baseline(ref.defect_key)["decided"]:
                out.append(ReplayUnit(
                    kind=REPORT, app_id=ref.app_id, case_id=ref.case_id,
                    defect_id=ref.defect_id, report=ref.report, source_episode=ref.episode,
                    report_index=ref.index,
                    attempt=sum(1 for a in self._of(ref.target) if a.valid) + 1))
        return out

    def worst_case(self) -> dict[str, int]:
        """The most replay episodes still to run, were every defect to stay in and no
        early stop to apply. The number to hold a spend cap against."""
        cfg = self.config
        ceiling = control = report = 0
        for key in self.facts:
            base = self.baseline(key)
            if base["need"] == CEILING:
                ceiling += cfg.ceiling_attempts - base["ceiling"]["attempts"]
            if base["need"]:
                control += cfg.control_attempts - base["control"]["attempts"]
        for ref in self.reports:
            entry = self.report_entry(ref)
            if entry["status"] == "pending" and not self._gave_up(f"{CEILING}:{ref.defect_key}") \
                    and not self._gave_up(f"{CONTROL}:{ref.defect_key}"):
                report += cfg.attempts - sum(1 for a in entry["attempts"] if a["valid"])
        return {"ceiling": ceiling, "control": control, "report": report,
                "total": ceiling + control + report}

    def excluded(self) -> list[dict[str, Any]]:
        """Every defect out of G4, with the reason. Sorted by defect key."""
        out = []
        for key in sorted(self.facts):
            base = self.baseline(key)
            if base["excluded"]:
                out.append({"defect": key, "excluded": base["excluded"],
                            "reason": base["reason"]})
        return out


# ── folding saved replays into the files a scorer reads ────────────────────────

def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def baseline_document(state: ReplayState) -> dict[str, Any]:
    return {"replay_version": REPLAY_VERSION, "replayer": state.config.stamp(),
            **corpus.stamp(),
            "defects": {key: {k: v for k, v in state.baseline(key).items() if k != "need"}
                        for key in sorted(state.facts)}}


def _had_judge_column(episode_dir: Path) -> bool:
    """Was this episode's saved `quality.json` written with the G5 side column
    (`--quality-judge`)? A fold that rewrites the file must write that column again."""
    try:
        saved = json.loads((episode_dir / rq.QUALITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    totals = saved.get("totals") if isinstance(saved, dict) else None
    return isinstance(totals, dict) and "steps_accuracy" in totals


def fold(run_dir: Path | str, config: ReplayConfig, *, replay_dir: Path | str | None = None,
         rescore: bool = True, state: ReplayState | None = None,
         allow_other_replayer: bool = False) -> dict[str, Any]:
    """Rebuild `baseline.json` and every `quality_replay.json` of `run_dir` from the
    replay episodes saved under `replay_dir`, then (with `rescore`) rewrite the
    `quality.json` of each episode touched, so G4 shows there. Runs no replay and needs
    no device. Returns a summary: files written, the excluded defects, what is left.

    The rewrite keeps every column of `quality.json` that this fold does not compute:
    an episode scored with the G5 side column is scored with it again.

    Saved replay episodes of ANOTHER replayer (a different agent, model, provider,
    tooling, rule or replay version) are never folded in. When there are any, the fold
    is refused with `ReplayError` unless `allow_other_replayer`: a fold under the wrong
    replayer finds no episode of its own and would rewrite every saved G4 answer to
    "not measured" (QUA-3607)."""
    run_dir = Path(run_dir)
    replay_dir = Path(replay_dir) if replay_dir else run_dir / REPLAY_DIR
    if state is None:
        state = ReplayState(config, valid_reports(run_dir), collect(replay_dir))
    if state.other_replayer and not allow_other_replayer:
        raise ReplayError(
            f"{state.other_replayer} saved replay episode(s) under {replay_dir} were run "
            f"by another replayer: {state.other_replayer_text()}. Nothing was written. "
            "Name the replayer the episodes were run under (--agent, --model, --provider, "
            "--tooling, or --config), or pass --allow-other-replayer to fold only this "
            f"replayer's {len(state.attempts)} episode(s) and leave the others out.")
    if replay_dir.is_dir():
        _write_json(replay_dir / BASELINE_FILE, baseline_document(state))
    by_episode: dict[str, list[dict[str, Any]]] = {}
    for ref in state.reports:
        by_episode.setdefault(ref.episode, []).append(state.report_entry(ref))
    written = 0
    for rel, entries in sorted(by_episode.items()):
        ep = run_dir / rel
        _write_json(ep / REPLAY_FILE, {
            "replay_version": REPLAY_VERSION, "replayer": config.stamp(),
            "reports": sorted(entries, key=lambda e: e["index"])})
        written += 1
        if rescore:
            rq.write_quality(ep, judge=_had_judge_column(ep))
    counts: dict[str, int] = {}
    for entries in by_episode.values():
        for e in entries:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {"episodes": written, "reports": counts, "excluded": state.excluded(),
            "remaining": state.worst_case(), "other_replayer": state.other_replayer,
            "other_replayer_differs": state.other_replayer_text()}


# ── running ────────────────────────────────────────────────────────────────────

#: Runs one replay episode and returns it as an `Attempt` (or None when nothing was
#: saved). The real one is `DeviceExecutor`; tests pass a fake.
Executor = Callable[[ReplayUnit], Awaitable[Attempt | None]]


async def run(state: ReplayState, execute: Executor, *, only: Iterable[str] | None = None,
              max_episodes: int | None = None,
              on_attempt: Callable[[ReplayUnit, Attempt | None], None] | None = None) -> int:
    """Run replays until nothing is left that can be run: baselines first, then the
    reports of every defect that stays in. `only` restricts the run to those unit keys
    (one unit per machine, for a cloud fan-out); `max_episodes` is a hard cap on the
    episodes started. Returns how many were started."""
    wanted = set(only) if only is not None else None
    started = 0
    refused: set[str] = set()
    while max_episodes is None or started < max_episodes:
        unit = next((u for u in state.next_units()
                     if u.key not in refused and (wanted is None or u.key in wanted)), None)
        if unit is None:
            break
        attempt = await execute(unit)
        started += 1
        if attempt is None:
            refused.add(unit.key)        # nothing was saved: never loop on it
        else:
            state.add(attempt)
        if on_attempt is not None:
            on_attempt(unit, attempt)
    return started


class DeviceExecutor:
    """The real executor: one device, the app of each unit installed once, one replay
    episode per call through `episode_runner.run_episode`. Mirrors how every other
    single-device driver in the package stages an app (`create.grader.run_plan`)."""

    def __init__(self, config: ReplayConfig, replay_dir: Path, *, mcp_server: str | None,
                 device: str, run_id: str = "", mcp_identity: dict | None = None) -> None:
        if not config.pinned:
            raise ReplayError("no replayer: name an agent and a model")
        self.config, self.replay_dir = config, Path(replay_dir)
        self.mcp_server, self.device = mcp_server or "", device
        self.run_id, self.identity = run_id, mcp_identity
        self._prepared: dict[str, tuple[dict, Path, str, str | None]] = {}
        self._trial = 0

    async def _prepare(self, app_id: str) -> tuple[dict, Path, str, str | None]:
        if app_id not in self._prepared:
            from . import bugs
            from .cli import _resolve_app_apk
            from .episode_runner import prepare_app
            from .session import DeviceSession
            suite = next((s for s in bugs.load_apps() if s["app"]["id"] == app_id), None)
            if suite is None:
                raise ReplayError(f"unknown app {app_id!r}")
            apk = _resolve_app_apk(suite["app"], suite, mode="journey")
            session = DeviceSession(self.mcp_server)
            hunt = bugs.exploration_task(suite)
            await session.force_release(self.device)
            bundle_id = await prepare_app(session, self.device, apk, hunt)
            # One app at a time on the device: a later unit of another app reinstalls.
            self._prepared = {app_id: (suite, apk, bundle_id,
                                       (hunt.bug_spec or {}).get("apk_sha256"))}
        return self._prepared[app_id]

    async def __call__(self, unit: ReplayUnit) -> Attempt | None:
        from .episode_runner import EpisodeOptions, run_episode
        from .result import resolve_artifact_dir
        from .schemas import Condition
        suite, apk, bundle_id, apk_sha = await self._prepare(unit.app_id)
        task = build_task(unit, suite, self.config)
        task.bundle_id = bundle_id
        if apk_sha:
            task.bug_spec["apk_sha256"] = apk_sha
        self._trial += 1
        cfg = self.config
        opts = EpisodeOptions(
            agent=str(cfg.agent), model=str(cfg.model), condition=Condition.no_routines,
            trial=self._trial, mcp_server=self.mcp_server, runs_dir=self.replay_dir,
            verdict_fn=replay_verdict, task_type=TASK_TYPE, device_serial=self.device,
            tooling=cfg.tooling, apk_path=apk, force_model=str(cfg.model),
            provider=cfg.provider, run_id=self.run_id, app_id=unit.app_id,
            mcp_server_identity=self.identity)
        try:
            result = await run_episode(task, opts)
        except RuntimeError as exc:
            # No device, a refused runs dir: nothing was saved, and the caller stops
            # asking for this unit.
            print(f"replay {unit.key}: not run: {exc}", file=sys.stderr)
            return None
        ep = resolve_artifact_dir(self.replay_dir, result)
        if ep is None:
            return None
        return attempt_from_result(json.loads(result.model_dump_json()),
                                   str(Path(ep).resolve().relative_to(self.replay_dir.resolve())))


# ── reporting ──────────────────────────────────────────────────────────────────

def plan_document(state: ReplayState) -> dict[str, Any]:
    """What a replay of this run would do, as data: the units runnable now, the most
    episodes it can still take, and the defects already known to be out."""
    units = state.next_units()
    budgets = {u.key: step_budget(state.facts[u.defect_key].ref_steps,
                                  state.facts[u.defect_key].case_budget, state.config)
               for u in units}
    return {"replay_version": REPLAY_VERSION, "replayer": state.config.stamp(),
            "valid_reports": len(state.reports), "defects": len(state.facts),
            # Saved episodes of another replayer: counted, never folded in.
            "other_replayer": state.other_replayer,
            "runnable_now": [{**u.identity(), "step_budget": budgets[u.key]} for u in units],
            "worst_case_episodes": state.worst_case(),
            "excluded": state.excluded(),
            "canary_unrecorded": sorted(k for k, f in state.facts.items()
                                        if f.has_canary and not f.truth_fired),
            "canary_only": sorted(k for k, f in state.facts.items()
                                  if f.basis == CANARY_ONLY)}


def plan_text(doc: dict[str, Any]) -> str:
    worst, who = doc["worst_case_episodes"], doc["replayer"]
    lines = [
        (f"replay v{doc['replay_version']} · replayer {who.get('agent') or '(unset)'} / "
         f"{who.get('model') or '(unset)'} · {who.get('tooling')}"),
        f"valid reports: {doc['valid_reports']} on {doc['defects']} (case, defect) pair(s)",
        (f"episodes still to run, at most: {worst['total']} (ceiling {worst['ceiling']}, "
         f"control {worst['control']}, report {worst['report']})"),
        f"runnable now: {len(doc['runnable_now'])}"]
    for label, key in (("scored on the canary alone (no text to find)", "canary_only"),
                       ("canary never recorded by the truth row (checked at the ceiling)",
                        "canary_unrecorded")):
        if doc[key]:
            lines.append(f"{label}: {len(doc[key])}")
            lines += [f"  {k}" for k in doc[key]]
    if doc.get("other_replayer"):
        lines.append(f"saved replay episodes of another replayer (left out): "
                     f"{doc['other_replayer']}")
    if doc["excluded"]:
        lines.append(f"excluded from G4: {len(doc['excluded'])}")
        lines += [f"  {e['defect']}  [{e['excluded']}] {e['reason']}" for e in doc["excluded"]]
    return "\n".join(lines)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pilot_row(label: str, state: ReplayState) -> dict[str, Any]:
    """One candidate replayer's line of the pilot table, from its saved replays."""
    bases = [state.baseline(k) for k in sorted(state.facts)]
    measured = [b for b in bases if b["ceiling"]["attempts"] > 0]
    kept = [a for a in state.attempts if a.valid]
    costs = [a.cost_usd for a in kept if a.cost_usd is not None]
    walls = [a.wall_time_sec for a in kept if a.wall_time_sec is not None]
    return {
        "replayer": label,
        "defects": len(bases),
        "ceiling_measured": len(measured),
        "ceiling_ok": sum(1 for b in measured
                          if b["ceiling"]["reproduced"] >= state.config.ceiling_min),
        "reference_replays": sum(b["ceiling"]["attempts"] for b in bases),
        "reference_reproduced": sum(b["ceiling"]["reproduced"] for b in bases),
        "control_measured": sum(1 for b in bases if b["control"]["attempts"] > 0),
        "control_failures": sum(1 for b in bases if b["control"]["reproduced"] > 0),
        "verdict_unavailable": sum(1 for b in bases if b["excluded"] in VERDICT_UNAVAILABLE),
        "usable": sum(1 for b in bases if b["decided"] and not b["excluded"]),
        "episodes": len(state.attempts),
        "not_run": sum(1 for a in state.attempts if not a.valid),
        "truncated": sum(1 for a in kept if a.truncated),
        "cost_per_replay_usd": _mean(costs),
        "cost_total_usd": sum(costs) if costs else None,
        "cost_unknown": len(kept) - len(costs),
        "wall_per_replay_sec": _mean(walls),
    }


def pilot_table(rows: list[dict[str, Any]]) -> str:
    """The pilot table, as Markdown: one line per candidate replayer."""
    def money(v: float | None) -> str:
        return "unknown" if v is None else f"${v:.2f}"

    def secs(v: float | None) -> str:
        return "unknown" if v is None else f"{v:.0f} s"

    head = ("| replayer | ceiling (defects at or above the bar) | reference replays "
            "reproduced | control failures | verdict unavailable | usable defects | "
            "cost per replay | wall time per replay | truncated | not run |")
    out = [head, "|" + "---|" * 10]
    for r in rows:
        out.append(
            f"| {r['replayer']} | {r['ceiling_ok']} of {r['ceiling_measured']} "
            f"| {r['reference_reproduced']} of {r['reference_replays']} "
            f"| {r['control_failures']} of {r['control_measured']} "
            f"| {r['verdict_unavailable']} | {r['usable']} of {r['defects']} "
            f"| {money(r['cost_per_replay_usd'])} | {secs(r['wall_per_replay_sec'])} "
            f"| {r['truncated']} | {r['not_run']} |")
    return "\n".join(out)


# ── device-free command line ───────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m qualgentbench.quality_replay",
        description="Plan or fold a replay-from-report run. Neither command starts an "
                    "agent or touches a device.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("plan", "print what a replay of this run would run"),
                       ("fold", ("rebuild baseline.json, quality_replay.json and "
                                 "quality.json from the saved replay episodes"))):
        p = sub.add_parser(name, help=text)
        p.add_argument("run_dir", type=Path)
        p.add_argument("--replay-dir", type=Path, default=None,
                       help=f"where replay episodes are saved (default: <run dir>/{REPLAY_DIR})")
        p.add_argument("--config", type=Path, default=None,
                       help="replay config JSON (default: the packaged pin)")
        p.add_argument("--agent", default=None)
        p.add_argument("--model", default=None)
        p.add_argument("--provider", default=None)
        p.add_argument("--tooling", default=None, choices=("mcp", "raw"),
                       help="how the replayer drove the device (default: the config's)")
        p.add_argument("--json", action="store_true", help="print JSON")
        if name == "fold":
            p.add_argument("--allow-other-replayer", action="store_true",
                           help="fold this replayer's episodes even when the replay "
                                "directory also holds another replayer's (they are left "
                                "out and counted). Without it such a fold is refused")
    args = ap.parse_args(argv)
    try:
        config = with_replayer(load_config(args.config), agent=args.agent, model=args.model,
                               provider=args.provider, tooling=args.tooling)
        if not args.run_dir.is_dir():
            raise ReplayError(f"{args.run_dir} is not a directory")
        replay_dir = args.replay_dir or args.run_dir / REPLAY_DIR
        state = ReplayState(config, valid_reports(args.run_dir), collect(replay_dir))
        if args.cmd == "plan":
            doc = plan_document(state)
            print(json.dumps(doc, indent=2, sort_keys=True) if args.json else plan_text(doc))
            return 0
        summary = fold(args.run_dir, config, replay_dir=replay_dir, state=state,
                       allow_other_replayer=args.allow_other_replayer)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"wrote {summary['episodes']} {REPLAY_FILE}; reports: {summary['reports']}; "
              f"excluded defects: {len(summary['excluded'])}; "
              f"episodes still to run, at most: {summary['remaining']['total']}")
        if summary["other_replayer"]:
            print(f"  left out: {summary['other_replayer']} saved replay episode(s) of "
                  f"another replayer ({summary['other_replayer_differs']})")
        for e in summary["excluded"]:
            print(f"  excluded {e['defect']}  [{e['excluded']}] {e['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
