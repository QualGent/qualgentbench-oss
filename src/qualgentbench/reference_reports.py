"""Reference reports: one hand-written, customer-shaped bug report per seeded journey defect.

A reference report is the report a careful tester would file for a defect on the case
that seeds it. It has two jobs in report-quality scoring:

* the CEILING for replay-from-report: when a fresh agent cannot reproduce a defect
  from its reference report, the defect is hard to reproduce, and a failed replay of an
  agent's report says nothing about that report;
* a known-good example per defect for the human label set.

Where they live: in the case file, on the case's own `bugs:` entry, because the
reproduction steps are the steps of THAT case from app launch. A defect seeded by two
cases carries one report per case.

    bugs:
      - id: stats-average-drops-latest
        reference_report:
          summary: Statistics shows the wrong average weight
          steps:
            - Launch openScale.
            - Tap the menu button at the top left.
            - Tap Statistics.
          expected: "The Weight card shows Avg: 79 kg, the mean of 74, 78 and 85 kg."
          actual: "The Weight card shows Avg: 76 kg."
          failure_step: "Step 3: Find the Weight card and read the values it summarises"

The five fields are the customer-shaped report: `summary` (the title), `steps` (the
reproduction steps, one UI action each, from app launch), `expected`, `actual` (quotes
the screen) and `failure_step` (`Step N: ...`, N being the number of the CASE step at
which the symptom is seen, the numbering the brief gives an agent).

`reference_report:` is harness-only. `journey.case_bugs` keeps only `id` and `marker`,
so no brief, task spec or scorer ever reads it. It is part of the case file, so adding
or editing one changes `corpus.corpus_version()` (or the held-out version).

`problems()` is the device-free check `scripts/lint_journey_cases.py` runs on every
report (rule `refreport`). It asks of the report what the deterministic quality gates
ask of an agent's report: it is matched to its own defect by the unchanged journey
matcher, its `actual` quotes text the truth file measured, and it has an observed
result, an expected result and reproduction steps that are steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from qualgentbench import corpus, journey

KEY = "reference_report"
FIELDS = ("summary", "steps", "expected", "actual", "failure_step")

# A reproduction step starts with one of these. Kept to the narrow list on purpose: a
# reference report must pass any reasonable "is this a UI action" test, so it uses only
# the verbs every such test accepts. A long press is written "Press and hold ...";
# `rotate` is here because a lifecycle case cannot be reproduced without turning the device.
ACTION_VERBS = ("launch", "open", "tap", "type", "swipe", "press", "select", "go back",
                "scroll", "rotate")
MIN_STEPS = 2
# The product keeps at most this many characters of `expected` and of `actual` on a
# bug. A reference report stays inside it so the quote survives.
RESULT_DETAIL_MAX = 250
SUMMARY_MAX = 140

_STEP_RE = re.compile(r"^\s*step\s+(\d+)\b", re.IGNORECASE)
_VERB_RE = re.compile(r"^(?:" + "|".join(re.escape(v) for v in ACTION_VERBS) + r")\b",
                      re.IGNORECASE)
# The shortest screen string that counts as a quote for a defect whose truth row
# measured no text of its own (see `truth_texts`, kind "state").
_MIN_SCREEN_QUOTE = 3


@dataclass(frozen=True)
class ReferenceReport:
    app_id: str
    case_id: str
    defect_id: str
    summary: str
    steps: tuple[str, ...]
    expected: str
    actual: str
    failure_step: str

    @property
    def step_number(self) -> int | None:
        """The case step `failure_step` names, or None."""
        m = _STEP_RE.match(self.failure_step)
        return int(m.group(1)) if m else None

    def customer_report(self, *, with_steps: bool = True) -> dict[str, Any]:
        """The report as the five customer-shaped fields. `with_steps=False` is the
        evidence-removed control: the same report with its reproduction steps deleted."""
        return {"summary": self.summary,
                "steps": list(self.steps) if with_steps else [],
                "expected": self.expected,
                "actual": self.actual,
                "failure_step": self.failure_step}

    def bug_report(self, *, grounded: bool = False) -> journey.BugReport:
        """The report as the journey matcher reads an agent's: the same field mapping as
        `journey.report_from_tool`."""
        return journey.BugReport(step=self.step_number, screen=self.failure_step,
                                 observed=self.actual, expected=self.expected,
                                 description=self.summary, grounded=grounded)


# ── loading ────────────────────────────────────────────────────────────────────

def _raw_entries(case: dict) -> list[tuple[str, Any]]:
    """(defect id, raw `reference_report:` value or None) per `bugs:` entry, in order."""
    out: list[tuple[str, Any]] = []
    for b in case.get("bugs") or []:
        if isinstance(b, dict):
            out.append((str(b.get("id")), b.get(KEY)))
        else:
            out.append((str(b), None))
    return out


def parse(raw: Any, *, app_id: str = "", case_id: str = "", defect_id: str = "") -> ReferenceReport:
    """One raw `reference_report:` mapping as a `ReferenceReport`. Raises ValueError when
    the shape is wrong (not a mapping, a missing or empty field, `steps` not a list of
    strings); content is checked by `problems`."""
    where = f"{case_id}/{defect_id}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: `{KEY}` must be a mapping with {', '.join(FIELDS)}")
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise ValueError(f"{where}: unknown `{KEY}` key(s) {unknown}")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, str) and s.strip() for s in steps):
        raise ValueError(f"{where}: `steps` must be a list of non-empty strings")
    text: dict[str, str] = {}
    for k in ("summary", "expected", "actual", "failure_step"):
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{where}: `{k}` must be a non-empty string")
        text[k] = " ".join(v.split())
    return ReferenceReport(app_id=app_id, case_id=case_id, defect_id=defect_id,
                           steps=tuple(" ".join(s.split()) for s in steps), **text)


def case_references(case: dict, app_id: str = "") -> dict[str, ReferenceReport]:
    """{defect id: report} for the `bugs:` entries of one case that carry a report."""
    cid = str(case.get("id"))
    return {bug: parse(raw, app_id=app_id, case_id=cid, defect_id=bug)
            for bug, raw in _raw_entries(case) if raw is not None}


def load(app_id: str) -> list[ReferenceReport]:
    """Every reference report of one app, in file order. Held-out apps resolve through
    `QGB_HELDOUT_DIR` like every other journey loader."""
    doc = journey.load_cases(app_id) or {}
    out: list[ReferenceReport] = []
    for case in doc.get("test_cases") or []:
        out.extend(case_references(case, app_id).values())
    return out


def load_all(*, heldout: bool | None = None) -> list[ReferenceReport]:
    """Every reference report of the corpus. `heldout=None`: public and held-out;
    `False`: public only; `True`: held-out only (empty when no split is configured)."""
    held = set(corpus.heldout_apps())
    apps = sorted(set(corpus.public_apps()) | held)
    out: list[ReferenceReport] = []
    for app_id in apps:
        if heldout is None or (app_id in held) == heldout:
            out.extend(load(app_id))
    return out


def missing(app_id: str) -> list[tuple[str, str]]:
    """(case id, defect id) pairs of one app that seed a defect but carry no report."""
    doc = journey.load_cases(app_id) or {}
    return [(str(case.get("id")), bug)
            for case in doc.get("test_cases") or []
            for bug, raw in _raw_entries(case) if raw is None]


# ── what the truth file gives a report to quote ────────────────────────────────

def truth_texts(case: dict, defects: dict[str, dict], row: dict | None, defect_id: str,
                app_name: str = "") -> dict[str, Any]:
    """The text the truth file measured for `defect_id` on this case, and which field of
    a report it belongs in.

    `kind` says what the defect leaves on screen:
      side     a display bug: its marker and measured texts (quote in `actual`)
      screen   a blocking bug the seeded build shows text for (quote in `actual`)
      absence  a blocking bug that only REMOVES text: the clean build's strings
               (quote in `expected`)
      death    a crash or hang: the exception the case names, or Android's own dialog
               wording (quote in `actual`)
      state    a blocking bug whose truth row measured no text at all: the fault is in
               stored state the reference route never displays. `actual` then quotes
               any string of the seeded screens, so the report still points at a real
               screen. A replay verdict that needs a defect-specific truth text cannot
               use these defects; `fired` (the canary) is all the truth has."""
    row = row or {}
    design = journey.case_design(case, defects)
    ev = journey.case_evidence(case, design, row, journey._oracle(case), app_name)
    seeded = [str(t) for screen in ((row.get("screens") or {}).get("seeded") or [])
              for t in screen]
    out: dict[str, Any] = {"kind": "", "actual": [], "expected": [], "echo": ev["echo_texts"],
                           "seeded_screens": seeded, "evidence": ev, "design": design}
    if defect_id != design["blocking"]:
        side = next((s for s in ev["side"] if s["bug"] == defect_id), None) or {}
        out["kind"] = "side"
        out["actual"] = sorted({t for t in [side.get("marker") or "", *(side.get("texts") or [])]
                                if journey._evidence(t)})
    elif design["death"]:
        out["kind"] = "death"
        out["actual"] = sorted(set(ev["crash_texts"]) | set(ev["echo_texts"]))
    elif ev["blocking_texts"] or ev["echo_texts"]:
        out["kind"] = "screen"
        out["actual"] = sorted(set(ev["blocking_texts"]) | set(ev["echo_texts"]))
        out["expected"] = list(ev["absence_texts"])
    elif ev["absence_texts"]:
        out["kind"] = "absence"
        out["expected"] = list(ev["absence_texts"])
    else:
        out["kind"] = "state"
        out["actual"] = sorted({t for t in seeded
                                if len(journey._evidence(t)) >= _MIN_SCREEN_QUOTE
                                and not t.strip().isdigit()})
    return out


def _quotes(text: str, field: str) -> bool:
    """Does the normalised `field` quote the truth string `text` on token boundaries? A
    truth string may carry the deriver's `<date>` placeholder where the screen shows a
    date; a report quotes the real screen, so the placeholder matches any text there."""
    needle = journey._evidence(text)
    if not needle:
        return False
    if "<date>" not in needle:
        return journey._word(needle, field)
    parts = [re.escape(p) for p in needle.split("<date>")]
    return re.search(r"(?<![a-z0-9])" + r".+?".join(parts) + r"(?![a-z0-9])", field) is not None


# ── the check ──────────────────────────────────────────────────────────────────

def steps_problems(steps: tuple[str, ...] | list[str], summary: str) -> list[str]:
    """Why `steps` are not reproduction steps, or []. The deterministic test of the
    "complete" gate, at its strictest: at least two entries, each starting with an
    action verb from `ACTION_VERBS`, the first one launching the app, and the text not
    the summary repeated."""
    found: list[str] = []
    if len(steps) < MIN_STEPS:
        found.append(f"needs at least {MIN_STEPS} steps, has {len(steps)}")
    for i, s in enumerate(steps, 1):
        if not _VERB_RE.match(s.strip()):
            found.append(f"step {i} does not start with an action verb "
                         f"({', '.join(ACTION_VERBS)}): {s!r}")
    if steps and not re.match(r"^(launch|open)\b", steps[0].strip(), re.IGNORECASE):
        found.append(f"step 1 must launch the app, got {steps[0]!r}")
    norm_summary = journey._norm(summary)
    if norm_summary and (journey._norm(" ".join(steps)) == norm_summary
                         or any(journey._norm(s) == norm_summary for s in steps)):
        found.append("steps repeat the summary")
    return found


def problems(case: dict, defects: dict[str, dict], row: dict | None, defect_id: str,
             raw: Any, app_name: str = "") -> list[str]:
    """Everything wrong with one raw `reference_report:` value, or []."""
    cid = str(case.get("id"))
    try:
        ref = parse(raw, case_id=cid, defect_id=defect_id)
    except ValueError as exc:
        return [str(exc).split(": ", 1)[-1]]
    found: list[str] = []

    # Written in the words a tester would use: the defect id is harness vocabulary.
    blob = journey._norm(" ".join([ref.summary, *ref.steps, ref.expected, ref.actual,
                                   ref.failure_step]))
    for needle in {journey._norm(defect_id), journey._norm(defect_id.replace("-", " "))}:
        if needle and needle in blob:
            found.append(f"names the defect id {defect_id!r}; write what a tester would see")
            break

    if len(ref.summary) > SUMMARY_MAX:
        found.append(f"summary is {len(ref.summary)} characters (limit {SUMMARY_MAX})")
    for k in ("expected", "actual"):
        if len(getattr(ref, k)) > RESULT_DETAIL_MAX:
            found.append(f"{k} is {len(getattr(ref, k))} characters "
                         f"(limit {RESULT_DETAIL_MAX})")

    # Complete: steps that are steps (observed and expected are non-empty by `parse`).
    found += steps_problems(ref.steps, ref.summary)

    # Located: names a real step of the case.
    n_steps = len(case.get("steps") or [])
    if ref.step_number is None:
        found.append(f"failure_step must start with `Step N`, got {ref.failure_step!r}")
    elif not 1 <= ref.step_number <= n_steps:
        found.append(f"failure_step names step {ref.step_number}; the case has {n_steps}")

    # Quotes a truth text.
    truth = truth_texts(case, defects, row, defect_id, app_name)
    actual, expected = journey._norm(ref.actual), journey._norm(ref.expected)
    in_actual = [t for t in truth["actual"] if _quotes(t, actual)]
    in_expected = [t for t in truth["expected"] if _quotes(t, expected)]
    if not in_actual and not in_expected:
        if truth["kind"] == "absence":
            found.append("expected quotes none of the strings the seeded build lost: "
                         f"{truth['expected'][:8]}")
        elif truth["kind"] == "state":
            found.append("actual quotes no string of the seeded screens (the truth row "
                         "measured no text for this defect, so quote what the screen shows)")
        elif not truth["actual"] and not truth["expected"]:
            found.append("the truth row has no text for this defect; derive the case first")
        else:
            found.append(f"actual quotes none of the truth texts: {truth['actual'][:8]}")

    # Valid: the unchanged journey matcher credits this report to this defect. An
    # echoable quote counts only when the device showed it; for a reference report the
    # recorded seeded screens stand in for the device.
    screens = journey._norm(" \n ".join(truth["seeded_screens"]))
    grounded = any(journey._word(q, actual) and journey._word(q, screens)
                   for q in (journey._evidence(t) for t in truth["echo"]) if q)
    design = truth["design"]
    spec = {"active_bugs": design["bugs"], "blocking": design["blocking"],
            "defects": defects, **truth["evidence"]}
    matched = journey.match_report(ref.bug_report(grounded=grounded), spec)
    if matched != defect_id:
        found.append(f"the journey matcher credits it to {matched!r}, not this defect: "
                     "quote this defect's text in `actual`, or use one of its symptom "
                     "words in `summary`")
    return found
