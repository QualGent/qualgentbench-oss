#!/usr/bin/env python3
"""Report quality adversary check: can a report be customer-ready by FORM alone?

`scripts/journey_adversary_check.py` asks whether a guess can be credited as a catch.
This asks the next question, of the quality gates in `qualgentbench.report_quality`: can
a report that is well formed, and nothing more, pass them? Each synthetic agent below
runs through the real `journey.journey_verdict` and then the real
`report_quality.score_episode`, against the real corpus: offline, no device, no agent,
no tokens.

Guessers. None may have a customer-ready report, and none may pass G1 (valid) and G2
(located) on the same report:

  steps-copy        copies the test case's steps as its reproduction steps, fills every
                    field, and names no real symptom (seeded build)
  structured-clean  the best report there is for the case, its hand-written reference
                    report with every field filled, filed on the CLEAN build
  brief-fill        fills expected and actual with text taken from the brief: the
                    expected outcome, and every quoted phrase and capitalised word of
                    the steps (seeded build)
  saw-elsewhere     quotes real screen text from the wrong step: the report names the
                    step where the truth shows the defect, but the device showed the
                    quote on another step (seeded build)
  names-elsewhere   the other way round: the device did show the quote on the step the
                    report names, and the truth shows the defect on other steps
  id-echo           never looks at the app, and quotes every word of the case id, the
                    agent name and the model name (all of which it knows from its own
                    working directory) as if the screen had shown it

Known defect (printed, and an expected failure in tests/test_quality_adversary_check.py;
it does not fail this gate until it is fixed and taken off `KNOWN_DEFECTS`):

  path-echo         id-echo, plus one screenshot. The screen read answers with the line
                    the agent harness writes before a screenshot, `[Image: source:
                    <path>]`, whose path holds the case id, the agent and the model. The
                    scorer's grounding text includes that line, so the same words now
                    count as shown by the device and earn G1 where one is an echoable
                    truth text. G2 no longer locates them (the screenshot is read on a
                    step the agent only announced: unknown), but the same agent with one
                    tap after its announcement would be located

G3 (complete) is a rule about the SHAPE of the text, so a guesser may pass it; the first
two do, on purpose. What stops a guesser is G1, G2 and (when it is registered) G4.

Controls. `honest` is the reference agent: it quotes the screen string the truth file
measured, at the step the case's reference report names, and files the reference report
as its bug. The device shows it every truth text that report quotes; for a defect that
only removes text it acts on the step and reads a screen without the text. It must stay
near 1 on G1, G2 and G3, through both report channels. `right-step` is saw-elsewhere and
names-elsewhere with the step put right; it must pass G2, or their failures prove
nothing about the step.

Priced. `symptom-quote` writes the corpus's own symptom vocabulary as prose (the route
`journey_adversary_check.py` prices, because prose is the only report some defects can
have), copies the test case's steps, quotes a text the start screen really showed, and
says it is on the last step. Symptom prose earns it G1 on a seeded build, by design, and
it pays for that on a clean build, where every report of its fails G1. G2 must stop it on
the seeded build: its quote is no truth text of any defect, and saying "Step N:" without
acting on the device ties no screen to step N (QUA-3604). It may not pass G1 and G2
together on any report.

    uv run python scripts/quality_adversary_check.py [--app a,b]

Exit 1 when any agent that is not a control has a customer-ready report, when a guesser
or a priced adversary passes G1 and G2 together, when a control falls below its floor, or
when the priced adversary stops paying.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from qualgentbench import journey, quality_rates, reference_reports
from qualgentbench import report_quality as rq

_HERE = Path(__file__).resolve().parent


def _sibling(name: str):
    """A script beside this one, loaded once (scripts/ is not a package)."""
    key = f"_qgb_script_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, _HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


adv = _sibling("journey_adversary_check")

#: Must have no customer-ready report and no report that passes G1 and G2.
GUESSERS = ("steps-copy", "structured-clean", "brief-fill", "saw-elsewhere",
            "names-elsewhere", "id-echo", "path-echo")
#: Earns G1 through symptom prose by design; measured, and asserted to pay on a clean build.
PRICED = ("symptom-quote",)
CONTROLS = ("honest", "right-step")
LOCATED = ("honest", "right-step", "saw-elsewhere", "names-elsewhere")
#: Guessers that DO pass G1 and G2 together today, and why. Each is a defect outside
#: this check: it is printed on every run and does not fail the gate. A customer-ready
#: report always fails it. Take an entry off when its defect is fixed; the test that
#: expects the failure then says so.
KNOWN_DEFECTS = {
    "path-echo": "the scorer's grounding text includes the harness-written `[Image: "
                 "source: <path>]` line of a screenshot, and the path holds the case id, "
                 "the agent and the model: a quote equal to one of those words counts as "
                 "shown by the device and earns the catch (G1) where it is an echoable "
                 "truth text. G2 answers unknown for it only because this agent never "
                 "acts on the device after announcing its step",
}
#: Controls that fall below the floor today, as `(agent, channel, gate)`, and why. None:
#: QUA-3604 made G2 look for the truth text inside `observed`, so a reference report
#: filed through the report tool alone is located.
KNOWN_CONTROL_GAPS: dict[tuple[str, str, str], str] = {}
#: The build each agent runs on. Everything else runs on the seeded build.
CLEAN_ARM = ("structured-clean",)
#: How the report reaches the scorer. `findings`: the findings file is the report of
#: record and one `mobile_report_result` call gives the customer-shaped fields.
#: `report_tool`: that call alone.
CHANNELS = ("findings", "report_tool")
#: The honest control's lowest pass rate on each of G1, G2 and G3 (`Tally.rate`: for G2 a
#: report the gate could not decide counts against it). Public corpus, findings channel,
#: after QUA-3604: G1 150/150, G2 144/150 (the other 6 have no screen step in the truth
#: and are unknown), G3 149/150.
HONEST_FLOOR = 0.9

#: What the one screen read of an agent that never looked shows: text no case knows.
START_SCREEN = "Lorem ipsum dolor"
#: Reproduction steps with the right shape and no content: G3 passes them.
BOILERPLATE_STEPS = ("Launch the app", "Tap through the steps of the test case",
                     "Verify the result on the last screen")
#: The agent and model in the made-up episode path of `path-echo` (any would do).
PATH_AGENT, PATH_MODEL = "claude-code", "claude-opus-5"


@dataclasses.dataclass
class Guess:
    """One synthetic episode: the device part of the transcript, the findings entries,
    the verdict, and the one `mobile_report_result` call (None: no call)."""
    transcript: str
    entries: list[dict]
    verdict: str = "fail"
    call: dict | None = None


# ── transcript pieces ──────────────────────────────────────────────────────────

def _say(step: int, what: str = "working on it") -> str:
    """The agent saying which step it is starting (G2 reads this)."""
    return adv._text(f"Step {step}: {what}")


def _read(payload: str, i: int) -> str:
    """A screen read: a device call that acts on nothing."""
    return adv._mcp_lines("claude", f"r{i}", "mobile_observe_screen", {}, payload)


def _tap(payload: str, i: int) -> str:
    """A tap that names no text, and the screen it left."""
    return adv._device(payload, i)


def _numbered(steps) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


def _call(*, summary: str, steps_text: str, steps: list[str], expected: str, actual: str,
          failure_step: str, status: str = "FAIL") -> dict:
    """The arguments of a `mobile_report_result` call with every field filled.
    `steps_text` goes in `code_investigation`, the field the product stores as the bug's
    steps; `steps` is the agent's own step list."""
    return {"device": "d", "status": status, "summary": summary,
            "code_investigation": steps_text,
            "steps": [{"description": s, "status": "passed"} for s in steps],
            "expected": expected, "actual": actual, "failure_step": failure_step}


def _call_lines(args: dict) -> str:
    reply = (f"STATUS: {args['status']}\n{args['summary']}\nActual: {args['actual']}\n"
             f"Expected: {args['expected']}")
    return adv._mcp_lines("claude", "report", "mobile_report_result", args, reply)


def episode_path(spec: dict) -> str:
    """An episode's screenshot path as the agent harness names it: the case id twice,
    the agent, the model and the arm."""
    case = str(spec.get("case_id") or "case")
    ep = f"2026-01-01T00-00-00Z_{case}_{PATH_AGENT}_{PATH_MODEL}_mcp_trial-1_ep-0123456789ab"
    return (f"/home/tester/.qualgentbench/runs/{case}/{ep}/claude_home/projects/"
            f"-home-tester--qualgentbench-runs-{case}-{ep}-workspace/shots/screen-1.png")


def image_source_read(spec: dict, i: int = 0) -> str:
    """A screen read answered the way a screenshot is recorded: the harness's
    `[Image: source: <path>]` line, then the listing."""
    listing = json.dumps({"elements": [{"text": START_SCREEN, "x": 0, "y": 0}]})
    return _read(f"[Image: source: {episode_path(spec)}] {listing}", i)


def id_words(spec: dict) -> list[str]:
    """The words of the case id, the agent name and the model name: what the harness
    writes into every episode path, and what an agent can read off its own directory."""
    out: list[str] = []
    for word in rq._WORD_RE.findall(f"{spec.get('case_id')} {PATH_AGENT} {PATH_MODEL}".lower()):
        if word not in out and journey._evidence(word):
            out.append(word)
    return out


# ── what the corpus knows about a case ─────────────────────────────────────────

@functools.cache
def _app_references(app_id: str) -> tuple[reference_reports.ReferenceReport, ...]:
    return tuple(reference_reports.load(app_id))


@functools.cache
def _tasks(version: str, app_ids: tuple[str, ...] | None = None) -> tuple:
    """The corpus's journey tasks of one version, loaded once. Nothing here changes a
    task: `run` scores a copy."""
    return tuple(adv._tasks(list(app_ids) if app_ids else None, version))


def _references(spec: dict) -> dict[str, reference_reports.ReferenceReport]:
    """{defect id: reference report} for the case this task runs."""
    return {r.defect_id: r for r in _app_references(str(spec.get("app_id") or ""))
            if r.case_id == str(spec.get("case_id") or "")}


def _shown(spec: dict, defect: str) -> tuple[rq.StepMap, list[int]] | None:
    """The case's step map and the brief steps on which the truth shows `defect`, or
    None when G2 itself cannot know (no truth row, no recorded screen)."""
    case = rq.load_case_truth(str(spec.get("app_id") or ""), str(spec.get("case_id") or ""))
    if isinstance(case, str):
        return None
    kind, route_steps = rq.symptom_route_steps(case.row, defect)
    shown = rq.symptom_brief_steps(case, route_steps) if kind else []
    return (rq.step_map(case), shown) if shown else None


def _reference_call(ref, *, failure_step: str | None = None, actual: str | None = None) -> dict:
    return _call(summary=ref.summary, steps_text=_numbered(ref.steps), steps=list(ref.steps),
                 expected=ref.expected, actual=actual or ref.actual,
                 failure_step=failure_step or ref.failure_step)


def _quoted_truth(spec: dict, bug: str, ref) -> list[str]:
    """The truth texts of `bug` that the reference report's `actual` quotes, as the
    report words them (a truth text's `<date>` slot filled with the report's date)."""
    case = rq.load_case_truth(str(spec.get("app_id") or ""), str(spec.get("case_id") or ""))
    if isinstance(case, str):
        return []
    actual = rq._norm_text(ref.actual)
    found = (rq._text_re(t).search(actual) for t in rq.symptom_texts(case, bug).shown)
    return [m.group(0) for m in found if m]


def _acted_read(step_text: str, i: int) -> str:
    """An action the transcript itself ties to one step (it aims at the step's own
    words), answered with a screen listing that holds none of the case's texts."""
    listing = json.dumps({"elements": [{"text": START_SCREEN, "x": 0, "y": 0}]})
    return adv._mcp_lines("claude", f"a{i}", "mobile_tap_and_observe",
                          {"element_text": step_text}, listing)


def _symptoms(spec: dict) -> list[str]:
    return sorted({s for d in (spec.get("defects") or {}).values()
                   for s in (d.get("symptoms") or []) if s})


# ── the agents ─────────────────────────────────────────────────────────────────

def _located(spec: dict, mode: str) -> Guess | None:
    """`honest`, `right-step`, `saw-elsewhere`, `names-elsewhere`: one report per seeded
    defect that has a measured quote, a reference report and a truth step. They differ
    only in the step the report NAMES and the step the device SHOWED the quote on:

                         names                     device showed it on
      honest             the reference's step      the same step
      right-step         a step the truth shows    the same step
      saw-elsewhere      a step the truth shows    a step whose screen is gone by then
      names-elsewhere    a step the truth does     the same step
                         not show the defect on
    """
    refs = _references(spec)
    lines: list[str] = []
    entries: list[dict] = []
    call = None
    for i, bug in enumerate(spec.get("active_bugs") or []):
        quotes, ref, truth = adv._quotes(spec, bug), refs.get(bug), _shown(spec, bug)
        if ref is None or (mode != "honest" and (not quotes or truth is None)):
            continue
        quote = quotes[0] if quotes else ""
        saw = [quote] if quote else []
        if mode == "honest":
            named = showed = ref.step_number or 1
            # The honest agent saw what its report says it saw: every truth text the
            # reference report quotes was on the device at that step.
            saw += [t for t in _quoted_truth(spec, bug, ref) if t not in saw]
            quote = quote or (saw[0] if saw else "")
        else:
            steps, shown = truth
            every = range(1, steps.steps + 1)
            if mode == "right-step":
                named = showed = shown[0]
            elif mode == "names-elsewhere":
                named = showed = next((s for s in every if s not in shown), 0)
            else:
                named, showed = next(((s, w) for s in shown for w in reversed(every)
                                      if not w <= s <= steps.through(w)), (0, 0))
            if not named:
                continue                # the truth shows this defect on every step
        brief = [str(s) for s in spec.get("steps") or []]
        if saw:
            lines += [_say(showed), _tap(" | ".join(saw), i)]
        elif mode == "honest" and 1 <= named <= len(brief):
            # Nothing to quote: the defect only removes text. It acted on the step and
            # read a screen that lacks the text.
            lines += [_say(named), _acted_read(brief[named - 1], i)]
        absent = adv._absences(spec, bug)
        entries.append({"step": named, "screen": "the app", "observed": quote,
                        "expected": absent[0] if absent else "",
                        "description": adv._symptom(spec, bug) or ref.summary})
        if call is None or bug == spec.get("blocking"):
            # One call carries one bug: the blocking one when there is one. The honest
            # agent files the reference report as written; the others keep its words and
            # put their own step and the bare quote in.
            call = _reference_call(ref) if mode == "honest" else _reference_call(
                ref, failure_step=f"Step {named}", actual=quote)
    if not entries:
        return None
    return Guess("\n".join(lines or [_read(START_SCREEN, 0)]) + "\n", entries, call=call)


def guess(task, mode: str, seeded: dict | None = None) -> Guess | None:
    """The episode `mode` writes for `task`, or None when the case gives it nothing to
    write (for example no step exists that the truth does not show the defect on).
    `seeded` is the spec of the same case's seeded arm, for an agent on the clean arm."""
    spec = task.bug_spec
    brief = [str(s) for s in spec.get("steps") or []]
    last = max(len(brief), 1)
    outcome = str(spec.get("expected_outcome") or "the expected outcome")
    blind = _read(START_SCREEN, 0) + "\n"

    if mode in LOCATED:
        return _located(spec, mode)

    if mode == "steps-copy":
        # Says it reached every step, read the screen there, and saw "something".
        lines = [x for s in range(1, last + 1) for x in (_say(s), _tap(START_SCREEN, s))]
        entries = [{"step": s, "screen": "the app", "observed": START_SCREEN,
                    "expected": outcome, "description": adv.FILLER[s % len(adv.FILLER)]}
                   for s in range(1, last + 1)]
        return Guess("\n".join(lines) + "\n", entries, call=_call(
            summary=adv.VAGUE, steps_text=_numbered(brief), steps=brief, expected=outcome,
            actual=START_SCREEN, failure_step=f"Step {last}"))

    if mode == "structured-clean":
        refs = _references(spec)
        if not refs:                    # a case that seeds nothing: perfect form anyway
            return Guess(blind, [{"step": last, "screen": "the app", "observed": START_SCREEN,
                                  "expected": outcome, "description": adv.VAGUE}],
                         call=_call(summary=adv.VAGUE, steps_text=_numbered(brief),
                                    steps=brief, expected=outcome, actual=START_SCREEN,
                                    failure_step=f"Step {last}"))
        lines, entries = [], []
        for i, (bug, ref) in enumerate(refs.items()):
            quotes = adv._quotes(seeded or {}, bug)
            observed = quotes[0] if quotes else ref.actual
            step = ref.step_number or last
            # It even "saw" the seeded build's text, on the step the reference names.
            lines += [_say(step), _tap(f"{observed} {ref.actual}", i)]
            entries.append({"step": step, "screen": ref.failure_step, "observed": observed,
                            "expected": ref.expected, "description": ref.summary})
        return Guess("\n".join(lines) + "\n", entries,
                     call=_reference_call(next(iter(refs.values()))))

    if mode == "brief-fill":
        quotes = adv.brief_strings(spec) or [outcome]
        entries = [{"step": 1 + i % last, "screen": q, "observed": q, "expected": outcome,
                    "description": adv.FILLER[i % len(adv.FILLER)]}
                   for i, q in enumerate(quotes)]
        return Guess(blind, entries, call=_call(
            summary=adv.VAGUE, steps_text=_numbered(brief), steps=brief, expected=outcome,
            actual=quotes[0], failure_step=f"Step {last}"))

    if mode in ("id-echo", "path-echo"):
        # Every word on every step: it does not know which word or which step counts.
        # The two differ in ONE thing, what the screen read answers with.
        words = id_words(spec)
        entries = [{"step": step, "screen": w, "observed": w, "expected": w,
                    "description": adv.FILLER[i % len(adv.FILLER)]}
                   for i, w in enumerate(words) for step in range(1, last + 1)]
        read = image_source_read(spec) if mode == "path-echo" else _read(START_SCREEN, 0)
        # One call carries one quote: the longest word of the case id.
        word = max(rq._WORD_RE.findall(str(spec.get("case_id")).lower()) or words, key=len)
        return Guess(_say(last) + "\n" + read + "\n", entries, call=_call(
            summary=adv.VAGUE, steps_text=_numbered(BOILERPLATE_STEPS),
            steps=list(BOILERPLATE_STEPS), expected=word, actual=word,
            failure_step=f"Step {last}"))

    if mode in PRICED:
        prose = "; ".join(_symptoms(spec)) or adv.VAGUE
        # One entry per step: it does not know where the defect shows, so it names them all.
        entries = [{"step": s, "screen": "the app", "observed": START_SCREEN,
                    "expected": outcome, "description": prose} for s in range(1, last + 1)]
        return Guess(_say(last) + "\n" + blind, entries, call=_call(
            summary=prose, steps_text=_numbered(brief), steps=brief, expected=outcome,
            actual=START_SCREEN, failure_step=f"Step {last}"))

    raise ValueError(f"unknown agent {mode!r}")


# ── one episode through the scorer and the gates ───────────────────────────────

def run(task, mode: str, root: Path, channel: str = "findings",
        seeded: dict | None = None) -> tuple[dict, dict] | None:
    """`(metrics, quality)` for one synthetic episode: the real `journey_verdict`, saved
    as an episode under `root` the way a run saves it, then the real
    `report_quality.score_episode`. None when the agent writes nothing for this case."""
    g = guess(task, mode, seeded)
    if g is None:
        return None
    spec = dict(task.bug_spec)
    spec["tooling"] = "mcp"
    transcript = g.transcript + (_call_lines(g.call) + "\n" if g.call else "")
    if channel == "report_tool":
        spec.pop("findings_file", None)
    else:
        spec["findings_file"] = yaml.safe_dump({"verdict": g.verdict, "bugs": g.entries},
                                               sort_keys=False)
    scored = dataclasses.replace(task, bug_spec=spec)     # the corpus's task is not touched
    metrics = journey.journey_verdict(transcript, "synthetic", scored).metrics
    ep = root / f"{mode}-{channel}" / str(task.id).replace("~", "-")
    (ep / "agent").mkdir(parents=True, exist_ok=True)
    (ep / rq.RESULT_FILE).write_text(json.dumps(
        {"task_id": task.id, "task_type": journey.TASK_TYPE, "condition": "mcp",
         "agent": mode, "model": "synthetic", "metrics": metrics}), encoding="utf-8")
    (ep / rq.TRANSCRIPT_FILE).write_text(transcript, encoding="utf-8")
    (ep / rq.BRIEF_FILE).write_text(journey.brief(scored, "emulator-5554", "mcp"),
                                    encoding="utf-8")
    return metrics, rq.score_episode(ep)


# ── reading a quality block ────────────────────────────────────────────────────

def gate(report: dict, gate_id: str) -> bool | None:
    """One gate's answer on one scored report (None: did not run, or unknown)."""
    return ((report.get("gates") or {}).get(gate_id) or {}).get("pass")


def valid_and_located(quality: dict) -> list[dict]:
    """The reports that pass G1 and G2 together."""
    return [r for r in quality["reports"] if gate(r, "G1") is True and gate(r, "G2") is True]


def ready_claims(quality: Any, _path: str = "") -> list[str]:
    """Every place a quality block says a report is customer-ready: a report's
    `customer_ready: true`, or any count or rate keyed `customer_ready*` that is above
    zero, wherever it sits (the totals today; a rates block when one is added). The
    `*_unknown` counts are not claims."""
    found: list[str] = []
    if isinstance(quality, dict):
        for key, value in quality.items():
            here = f"{_path}.{key}" if _path else str(key)
            if str(key).startswith("customer_ready") and not str(key).endswith("unknown"):
                found += _positive(value, here)
            elif key != "customer_report":      # the agent's own words are not a claim
                found += ready_claims(value, here)
    elif isinstance(quality, list):
        for i, value in enumerate(quality):
            found += ready_claims(value, f"{_path}[{i}]")
    return found


#: In a `customer_ready*` mapping (a row of the rates block, `quality_rates.block`), the
#: entries that say a report IS ready. The rest are denominators, intervals, and counts
#: of reports that are not ready, unknown or waiting for a gate.
_READY_KEYS = ("k", "ready", "rate", "passed")


def _positive(value: Any, path: str) -> list[str]:
    """Where a `customer_ready*` value is true or above zero."""
    if value is True or (isinstance(value, (int, float)) and not isinstance(value, bool)
                         and value > 0):
        return [f"{path} = {value!r}"]
    if isinstance(value, dict):
        return [hit for k in _READY_KEYS if k in value
                for hit in _positive(value[k], f"{path}.{k}")]
    return []


def rates_claims(records: list[dict]) -> tuple[list[str], int]:
    """The rates block (`quality_rates.block`) over one agent's episodes, read two ways:
    `(customer-ready claims, reports ready on the gates that exist)`. The second is the
    block's `ready_on_available`: every registered headline gate passed. It is above
    zero for an agent that passes G1, G2 and G3 on one report, whether or not G4 is
    registered, which makes it the live form of the customer-ready rate today."""
    block = quality_rates.block(records)
    claims = [f"rates block: {c}" for c in ready_claims(block)]
    return claims, sum(int((row.get("ready_on_available") or {}).get("k") or 0)
                       for row in block.get("rows") or [])


@dataclasses.dataclass
class Tally:
    """What one agent got over every episode it wrote, through one channel."""
    episodes: int = 0
    reports: int = 0                       # scored (not neutral) reports
    ran: dict = dataclasses.field(default_factory=lambda: {"G1": 0, "G2": 0, "G3": 0})
    passed: dict = dataclasses.field(default_factory=lambda: {"G1": 0, "G2": 0, "G3": 0})
    unknown: int = 0                       # G2 could not decide
    both: list = dataclasses.field(default_factory=list)      # G1 and G2 on one report
    ready: list = dataclasses.field(default_factory=list)     # customer-ready claims
    reasons: dict = dataclasses.field(default_factory=dict)   # G2 not-a-pass reason -> count
    misses: list = dataclasses.field(default_factory=list)    # each G1/G2 answer not a pass
    records: list = dataclasses.field(default_factory=list)   # rates-block episode records
    available: int = 0                     # rates block: ready on the gates that exist

    def add(self, task_id: str, quality: dict, result: dict | None = None) -> None:
        self.episodes += 1
        if result is not None:
            self.records.append(quality_rates.episode_record(result, quality))
        for r in quality["reports"]:
            if r["neutral"]:
                continue
            self.reports += 1
            for g in self.ran:
                answer = gate(r, g)
                self.ran[g] += answer is not None
                self.passed[g] += answer is True
            g1, g2 = r["gates"].get("G1") or {}, r["gates"].get("G2") or {}
            self.unknown += bool(g2.get("unknown"))
            if g1.get("pass") is not True:
                self.misses.append(f"{task_id}: G1 {g1.get('reason')}")
            elif g2.get("pass") is not True:
                why = ("unknown: " if g2.get("unknown") else "") + _shape(g2.get("reason") or "")
                self.reasons[why] = self.reasons.get(why, 0) + 1
                self.misses.append(f"{task_id}: G2 {'unknown: ' if g2.get('unknown') else ''}"
                                   f"{g2.get('reason')}")
        self.both += [f"{task_id}: report {r['index']} (step {r['step']}, matched "
                      f"{r['matched']})" for r in valid_and_located(quality)]
        self.ready += [f"{task_id}: {claim}" for claim in ready_claims(quality)]

    def rate(self, g: str) -> float | None:
        """Passed over the reports the gate ran on. For G2 the reports it could not
        decide are in the denominator too: an unknown is never a pass."""
        ran = self.ran[g] + (self.unknown if g == "G2" else 0)
        return self.passed[g] / ran if ran else None

    def cell(self, g: str) -> str:
        return f"{self.passed[g]}/{self.ran[g] + (self.unknown if g == 'G2' else 0)}"


def _shape(reason: str) -> str:
    """A G2 reason with its step numbers and defect id taken out, to count by."""
    reason = re.sub(r"\d+(?:, \d+)*", "N", reason)
    return re.sub(r"(shows|crashes) \S+ at", r"\1 <defect> at", reason)


def tasks_for(mode: str, app_ids: list[str] | None = None,
              clean: bool = False) -> list[tuple[Any, dict | None]]:
    """`(task, the seeded arm's spec)` for every episode `mode` is run on. `clean=True`
    puts a seeded-arm agent on the clean arm instead (what a priced adversary pays)."""
    key = tuple(app_ids) if app_ids else None
    seeded = _tasks("seeded", key)
    if not clean and mode not in CLEAN_ARM:
        return [(t, None) for t in seeded]
    by_case = {t.bug_spec.get("case_id"): t.bug_spec for t in seeded}
    return [(t, by_case.get(t.bug_spec.get("case_id"))) for t in _tasks("clean", key)]


def tally(mode: str, root: Path, channel: str = "findings",
          app_ids: list[str] | None = None, clean: bool = False) -> Tally:
    """`mode` over every episode it writes, through one channel."""
    out = Tally()
    for task, seeded in tasks_for(mode, app_ids, clean):
        result = run(task, mode, root / ("clean" if clean else "arm"), channel, seeded)
        if result is not None:
            out.add(task.id, result[1], {"agent": mode, "model": "synthetic",
                                         "condition": "mcp", "metrics": result[0]})
    claims, out.available = rates_claims(out.records)
    out.ready += claims
    return out


def collect(root: Path, app_ids: list[str] | None = None) -> tuple[dict, dict]:
    """Every agent through every channel: `({(agent, channel): Tally}, the same for each
    priced adversary on the clean arm)`. Episodes are written under `root`."""
    results = {(m, c): tally(m, root, c, app_ids)
               for m in GUESSERS + PRICED + CONTROLS for c in CHANNELS}
    paid = {(m, c): tally(m, root, c, app_ids, clean=True) for m in PRICED for c in CHANNELS}
    return results, paid


# ── the gate ───────────────────────────────────────────────────────────────────

def judge(results: dict, paid: dict) -> tuple[list[str], list[str]]:
    """`(failures, known defects)` as printable lines. The gate holds when there are no
    failures; a known defect is printed and does not fail it."""
    failures: list[str] = []
    known: list[str] = []

    def some(lines: list[str]) -> str:
        return "".join(f"\n    {line}" for line in lines[:8]) + (
            f"\n    ... and {len(lines) - 8} more" if len(lines) > 8 else "")

    for (mode, channel), t in results.items():
        who = f"'{mode}' ({channel})"
        if mode not in CONTROLS and t.ready:
            failures.append(f"{who} has a customer-ready report, {len(t.ready)} claim(s):"
                            + some(t.ready))
        if mode in GUESSERS:
            if not t.reports:
                failures.append(f"{who} wrote no report; its zero proves nothing")
            if t.available and mode not in KNOWN_DEFECTS:
                failures.append(f"{who} has {t.available} report(s) that pass every "
                                f"registered headline gate (rates block, "
                                f"`ready_on_available`)")
            if t.both and mode in KNOWN_DEFECTS:
                known.append(f"{who} passes G1 (valid) and G2 (located) together on "
                             f"{len(t.both)} report(s): {KNOWN_DEFECTS[mode]}" + some(t.both))
            elif t.both:
                failures.append(f"{who} passes G1 (valid) and G2 (located) together on "
                                f"{len(t.both)} report(s):" + some(t.both))
            elif t.passed["G1"] and mode in KNOWN_DEFECTS:
                known.append(f"{who} is valid (G1) on {t.cell('G1')} report(s), having "
                             f"seen nothing; G2 passes none of them ({t.unknown} "
                             f"unknown): {KNOWN_DEFECTS[mode]}")
        elif mode == "honest":
            for g in ("G1", "G2", "G3"):
                rate = t.rate(g)
                if rate is not None and rate >= HONEST_FLOOR:
                    continue
                line = (f"{who} passes {g} on {t.cell(g)} report(s), below the "
                        f"{HONEST_FLOOR:.0%} floor")
                gap = KNOWN_CONTROL_GAPS.get((mode, channel, g))
                (known if gap else failures).append(f"{line}: {gap}" if gap else line)
        elif mode == "right-step" and (not t.ran["G2"] or t.passed["G2"] < t.ran["G2"]):
            failures.append(f"{who} passes G2 on {t.cell('G2')} report(s): with the step put "
                            f"right every one must pass, or the wrong-step guessers' "
                            f"failures prove nothing about the step" + some(t.misses))
    for (mode, channel), t in paid.items():
        who, seeded = f"'{mode}' ({channel})", results[(mode, channel)]
        if t.ready:
            failures.append(f"{who} has a customer-ready report on the clean build:"
                            + some(t.ready))
        if not t.reports or t.passed["G1"]:
            failures.append(f"{who} does not pay on the clean build: G1 passes on "
                            f"{t.cell('G1')} of its report(s) there, and must pass on none")
        if not seeded.passed["G1"]:
            failures.append(f"{who} earns nothing on the seeded build; it has stopped "
                            f"measuring anything")
        if seeded.both:
            failures.append(
                f"{who} passes G1 and G2 together on {len(seeded.both)} of its "
                f"{seeded.reports} seeded-build report(s) without testing: symptom prose "
                f"earns G1 (priced, by design), so G2 must not locate a quote that is no "
                f"truth text of the defect, on a step the agent only SAID it was on:"
                + some(seeded.both))
        if seeded.available:
            failures.append(f"{who} has {seeded.available} seeded-build report(s) that pass "
                            f"every registered headline gate (rates block, "
                            f"`ready_on_available`)")
    return failures, known


def table(results: dict, paid: dict) -> str:
    rows = [(f"{'agent':22s}{'channel':13s}{'episodes':>9s}{'reports':>9s}{'G1':>11s}"
             f"{'G2':>10s}{'G2 unk':>8s}{'G3':>11s}{'G1+G2':>7s}{'avail':>7s}{'ready':>7s}")]
    for label, tallies in (("", results), (" @clean", paid)):
        for (mode, channel), t in tallies.items():
            rows.append(f"{mode + label:22s}{channel:13s}{t.episodes:>9d}{t.reports:>9d}"
                        f"{t.cell('G1'):>11s}{t.cell('G2'):>10s}{t.unknown:>8d}"
                        f"{t.cell('G3'):>11s}{len(t.both):>7d}{t.available:>7d}{len(t.ready):>7d}")
    rows.append("\nG1 / G3 = reports that passed / reports the gate ran on · G2 = passed / "
                "(ran + unknown) · G1+G2 = reports that passed both · avail = reports that "
                "passed every registered headline gate (the rates block's "
                "`ready_on_available`) · ready = customer-ready claims in any quality "
                "block or in the rates block · @clean = a priced adversary on the "
                "clean build")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--app", help="comma-separated app ids (default: every app with test cases)")
    ap.add_argument("--misses", action="store_true",
                    help="list every report of a control that did not pass G1 or G2")
    args = ap.parse_args(argv)
    app_ids = [a.strip() for a in args.app.split(",")] if args.app else None
    if not _tasks("seeded", tuple(app_ids) if app_ids else None):
        print("FAIL: no seeded journey tasks found, nothing was checked")
        return 1

    with tempfile.TemporaryDirectory(prefix="qgb-quality-adversary-") as tmp:
        results, paid = collect(Path(tmp), app_ids)
    failures, known = judge(results, paid)

    print(f"quality v{rq.QUALITY_VERSION} · gates {', '.join(g.id for g in rq.GATES)} · "
          f"customer-ready needs {', '.join(rq.HEADLINE_GATES)}\n")
    print(table(results, paid))
    for (mode, channel), t in results.items():
        if mode in LOCATED and t.reasons:
            print(f"\n'{mode}' ({channel}), valid reports that G2 did not pass, by reason:")
            for why, n in sorted(t.reasons.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {n:4d}  {why}")
        if args.misses and mode in CONTROLS:
            for line in t.misses:
                print(f"    {line}")
    for line in known:
        print(f"\nKNOWN DEFECT: {line}")
    for line in failures:
        print(f"\nFAIL: {line}")
    if not failures:
        honest = results[("honest", "findings")]
        print(f"\nPASS: no guesser, and no priced adversary, has a customer-ready report; "
              f"no guesser outside the {len(KNOWN_DEFECTS)} known defect(s), and no priced "
              f"adversary, passes G1 and G2 together; the honest control passes G1 on {honest.cell('G1')}, G2 on "
              f"{honest.cell('G2')} and G3 on {honest.cell('G3')} of its reports; the "
              f"priced adversary pays on every clean episode")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
