#!/usr/bin/env python3
"""Device-free lint for the journey corpus (`data/test-cases/<app>.yaml`).

The agent sees `name`, `steps` and `expected_outcome`; the harness scores with
`check.expect`, `evidence:` and `bugs:`. Three text-only mistakes have each cost real
episodes, and none of them needs a device to catch:

  leak        a `present:`/`absent:` string or an `evidence:` string that equals or
              contains a seeded defect's marker or one of its symptom phrases — the
              completion witness IS the defect, so the case scores an agent for having
              been told where the bug is (error). The same check runs against the
              measured display texts in `data/truth/journey-<app>.json`: a witness that
              only one arm shows is a marker under another name (error).
  brief       an `expected_outcome`, step or name that contains a defect marker of one
              of the case's own bugs on token boundaries — the corrupted value stated
              outright (error); or a multi-word symptom PHRASE of one of them — the
              brief spelling out the behaviour the defect breaks (warning).
  no oracle   a case with no `check.expect` — nothing can ever confirm it (error).
  route       a `check.steps` entry that is not a verb or single-key map, or whose
              action/value is outside `submission.ACTIONS` (`rotae: landscape`,
              `swipe: sideways`) — `truth._steps` parses trusted YAML permissively and
              `replay.run_steps` only discovers it on a device, as an INCONCLUSIVE pass
              that reads like a flaky case rather than a typo (error).
  class       a `defects:` entry with no `class:`, or one outside the closed vocabulary
              `journey.DEFECT_CLASSES` (docs/defect-classes.md) — the corpus's class mix
              is then checkable only by reading ids and guessing (error).
  columns     a `db:`/`content:` query whose text names none of the brief's key nouns
              (quoted strings, numbers, capitalised names) — the oracle may be checking
              something other than what the brief promises (WARNING only: a fixture id
              such as `id=3` is a legitimate stand-in for "the Aug 29 measurement").
  short       an `evidence:` string under three characters — a witness that matches
              inside anything (warning).
  reference   a DISPLAY side bug on a case with no `reference: {kind, note}` beside it in
              `bugs:` — how the brief lets the agent know the shown value is wrong: a value
              the brief STATES, a value the route itself ENTERED, or an on-route
              CROSS-CHECK (the true value drawn elsewhere on the route). A side bug the
              brief gives no handle on measures curiosity, not QA, and inflates a careful
              agent's misses (QUA-2783) (error). Two deferrals are warnings, each naming
              who owns it: a case carrying `witness_before_action:` (re-authored under that
              ticket; this lint does not ask a second ticket to edit it) and a held-out app
              (its files live outside the repo; the corpus owner audits them). A
              `stated` note must name the step or expected outcome that states the value,
              since `brief: intended_behavior` never reaches a journey agent (QUA-3549).
  known       an app-level `known_behaviours:` list (QUA-3541) that `journey.
              load_known_behaviours` refuses: an entry with no id or a duplicate one,
              no screen quote (`texts`/`patterns`), a text under the evidence floor or a
              pattern that does not compile — the task builder would refuse the file, so
              the lint says why first (error).
  quotable    a DISPLAY side bug whose effective marker is below the matcher's evidence
              floor (`journey._evidence`, `_MIN_EVIDENCE_CHARS`): no report can quote it,
              so an exact sighting is either credited nothing or credited to another bug
              (the 2026-09-19 board, `subtask-chip-low`) (error).
  drift       the case file's `drift:` block (QUA-3490, benign UI changes behind QgbFlags):
              an id that is also a defect id or appears in a case's `bugs:`, a kind
              outside `journey.DRIFT_KINDS`, no `change:`, a rewrite whose steps are not
              replayable or that matches no case's route, an `affects:` naming an unknown
              case, a flag no case would derive, or a drift id list that differs from the
              benchmark spec's `drift:` patches (error).
  refreport   a `bugs:` entry with no `reference_report:` — the hand-written,
              customer-shaped report of that defect on that case (summary, steps,
              expected, actual, failure_step; `qualgentbench.reference_reports`) — or one
              that would not pass the deterministic report-quality gates if an agent had
              written it: the journey matcher does not credit it to its own defect, its
              `actual` quotes no text the truth file measured (for a defect that only
              removes text, its `expected`), its `failure_step` names no step of the
              case, or its `steps` are not reproduction steps (at least two, each
              starting with an action verb, from app launch, not the summary repeated).
              It also may not name the defect id: it is written in a tester's words.
              Public and held-out alike (error). A defect no case seeds needs none.

Exit 1 on any error. `--app a,b` narrows the files. The rules are pure functions over a
loaded document so `tests/test_lint_journey_cases.py` can drive each one in isolation.
"""

from __future__ import annotations

import argparse
import re
import sys

from qualgentbench import journey, reference_reports
from qualgentbench.submission import route_item, step_problem

_MIN_WITNESS_CHARS = 3

# How the brief lets an agent know a side bug's value is wrong (QUA-2783). Closed on
# purpose, like `journey.DEFECT_CLASSES`: a new kind is a claim about what counts as a
# handle, and it should arrive through review, not a typo.
#   stated       the brief states the true value or spelling (an expected value, a noun)
#   entered      the route itself entered the value the screen then misstates
#   cross-check  the true value is drawn elsewhere on the route the brief walks
REFERENCE_KINDS = ("stated", "entered", "cross-check")

# A `stated` reference must say WHERE the journey brief states the value: a step or the
# expected outcome. Those (with the case name) are all `journey.brief` shows the agent;
# the case's `brief: {title, intended_behavior}` block feeds create mode only. QUA-3549:
# birday-delete-anniversary's note said "the brief says ..." and pointed at
# `intended_behavior`, which no journey agent ever read — 14 of 14 seeded episodes saw
# the seeded row and called it correct.
_STATED_WHERE = re.compile(r"\b(?:steps?|outcome)\b", re.IGNORECASE)


class Finding:
    """One lint result. A plain class, not a dataclass: the tests load this script
    through importlib without registering it in sys.modules, and a dataclass under
    `from __future__ import annotations` needs its module there to resolve fields."""

    def __init__(self, level: str, rule: str, case: str, detail: str) -> None:
        self.level = level          # "error" | "warning"
        self.rule = rule            # leak | brief | no-oracle | route | class | columns | short
                                    # | reference | quotable | drift | refreport
        self.case = case            # the case id (the defect id for `class`)
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.level:7s} {self.rule:9s} {self.case}: {self.detail}"

    def __repr__(self) -> str:
        return f"Finding({self.level!r}, {self.rule!r}, {self.case!r}, {self.detail!r})"


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().strip("\"'").lower())


def _witness_strings(case: dict) -> list[tuple[str, str]]:
    """Every string the completion oracle wants the AGENT to have surfaced: the
    `present:`/`absent:` text and each `evidence:` entry, tagged by where it came from."""
    out: list[tuple[str, str]] = []
    expect = ((case.get("check") or {}).get("expect")) or {}
    # `present:`/`absent:` are screen strings only in a screen oracle; a `content:` oracle
    # carries `absent: true` as a flag (contacts-delete), which is not text.
    if journey._oracle(case)["mode"] in ("present", "absent"):
        for key in ("present", "absent"):
            if key in expect and str(expect[key]).strip():
                out.append((f"expect.{key}", str(expect[key])))
    for e in case.get("evidence") or []:
        out.append(("evidence", str(e)))
    return out


def _case_defects(case: dict, defects: dict[str, dict]) -> list[tuple[str, str, list[str]]]:
    """(bug id, effective marker, symptoms) for each bug the case seeds. A per-case
    `bugs: [{id, marker}]` override replaces the defect's marker, as in journey.py."""
    out = []
    for b in journey.case_bugs(case):
        d = defects.get(b["id"]) or {}
        out.append((b["id"], b["marker"] or d.get("marker") or "", list(d.get("symptoms") or [])))
    return out


def _measured_display_texts(case_id: str, truth: dict) -> list[str]:
    """The screen strings derive_journey.py measured for the case's DISPLAY defects —
    text that one arm shows and the other does not."""
    row = truth.get(case_id) or {}
    out: list[str] = []
    for s in row.get("side") or []:
        out += [str(t) for t in (s.get("texts") or [])]
    return out


# ── the rules ──────────────────────────────────────────────────────────────────

def rule_leak(case: dict, defects: dict[str, dict], truth: dict | None = None) -> list[Finding]:
    """Two channels, two scopes. The strings the AGENT must surface (`evidence:`, or the
    `present:` text when no `evidence:` is declared — journey._oracle's default) are
    checked against every bug on the case. The strings only the HARNESS route evaluates
    (`absent:`, and `present:` once `evidence:` takes over) are checked against DISPLAY
    bugs only: for a functional blocking bug the harness-side string is legitimately the
    blocked outcome's own signal — a blocked case's `absent:` balance string is what the
    seeded build wrongly shows and what the defect's symptom list must name."""
    cid = str(case.get("id"))
    found: list[Finding] = []
    has_evidence = bool(case.get("evidence"))
    bugs = _case_defects(case, defects)
    display = {b for b, _, _ in bugs if (defects.get(b) or {}).get("kind") == "display"}
    for where, text in _witness_strings(case):
        w = _norm(text)
        if not w:
            continue
        agent_channel = where == "evidence" or (where == "expect.present" and not has_evidence)
        for bug_id, marker, symptoms in bugs:
            if not agent_channel and bug_id not in display:
                continue
            for kind, needle in [("marker", marker)] + [("symptom", s) for s in symptoms]:
                n = _norm(needle)
                if n and n in w:
                    found.append(Finding("error", "leak", cid,
                                         f"{where} {text!r} contains {bug_id}'s {kind} {needle!r}"))
        for measured in _measured_display_texts(cid, truth or {}):
            m = _norm(measured)
            if m and (m in w or w in m):
                found.append(Finding("error", "leak", cid,
                                     f"{where} {text!r} overlaps a measured display text {measured!r} "
                                     f"(shown on one arm only — that is a marker, not a witness)"))
    return found


def rule_brief(case: dict, defects: dict[str, dict]) -> list[Finding]:
    """A defect marker stated in the agent-facing text, on token boundaries (the
    harness's own `_word`, so `1` does not fire inside `10`)."""
    cid = str(case.get("id"))
    found: list[Finding] = []
    fields = [("expected_outcome", str(case.get("expected_outcome") or "")),
              ("name", str(case.get("name") or ""))]
    fields += [(f"step {i}", str(s)) for i, s in enumerate(case.get("steps") or [], 1)]
    for bug_id, marker, _ in _case_defects(case, defects):
        m = journey._evidence(marker)
        if not m:
            continue
        for where, text in fields:
            if journey._word(m, _norm(text)):
                found.append(Finding("error", "brief", cid,
                                     f"{where} states {bug_id}'s marker {marker!r}: {text!r}"))
    return found


def rule_brief_symptom(case: dict, defects: dict[str, dict]) -> list[Finding]:
    """WARNING: the agent-facing text carries a multi-word symptom PHRASE of one of the
    case's own bugs ("marked done", "next occurrence") — the shape of the two semantic
    leaks this audit removed, where the brief spelled out the behaviour the defect breaks.
    Single words are not checked: a functional brief must say "delete" or "rename", and
    the typo defects list "title" and "label". A legitimate hit is an expected value the
    brief has to state (tasks-change-due-time's "9:00" under `due-date-edit-lost`)."""
    cid = str(case.get("id"))
    found: list[Finding] = []
    fields = [("expected_outcome", str(case.get("expected_outcome") or "")),
              ("name", str(case.get("name") or ""))]
    fields += [(f"step {i}", str(s)) for i, s in enumerate(case.get("steps") or [], 1)]
    for bug_id, _, symptoms in _case_defects(case, defects):
        for sym in symptoms:
            s = _norm(sym)
            if " " not in s:
                continue
            for where, text in fields:
                if journey._word(s, _norm(text)):
                    found.append(Finding("warning", "brief", cid,
                                         f"{where} carries {bug_id}'s symptom phrase {sym!r}: {text!r}"))
    return found


def rule_no_oracle(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    expect = ((case.get("check") or {}).get("expect")) if isinstance(case.get("check"), dict) else None
    if not isinstance(expect, dict) or not expect:
        return [Finding("error", "no-oracle", cid, "no `check.expect` — nothing can confirm this case")]
    return []


def rule_route(case: dict) -> list[Finding]:
    """Every `check.steps` entry must be something `replay.run_steps` can execute.
    Reads each item through `submission.route_item` — the parser `truth._steps` uses,
    so the two accept exactly the same shapes (a verb, a single-key map, and a
    `tap`/`long_press` map with a `row:` beside it) — then asks
    `submission.step_problem` about the verb and the row — one vocabulary, so a step
    the replayer gained (`rotate`) is accepted here the day it lands, and one it never
    had is rejected before a device run spends twenty minutes calling it INCONCLUSIVE."""
    cid = str(case.get("id"))
    raw = ((case.get("check") or {}).get("steps")) if isinstance(case.get("check"), dict) else None
    found: list[Finding] = []
    for i, item in enumerate(raw or [], 1):
        shape = route_item(item)
        if shape is None:
            found.append(Finding("error", "route", cid,
                                 f"check step {i}: expected a verb or a single-key "
                                 f"mapping (optionally with `row:`), got {item!r}"))
            continue
        action, value, row = shape
        problem = step_problem(action, value, row=row)
        if problem:
            found.append(Finding("error", "route", cid, f"check step {i}: {problem}"))
    return found


def rule_class(doc: dict) -> list[Finding]:
    """Every `defects:` entry names its fault class from `journey.DEFECT_CLASSES`,
    spelled exactly. A missing or unknown class is an error: `scripts/mix_report.py`
    cannot place the defect, and the class mix of the corpus — what QUA-2723 exists to
    fix — silently stops adding up. Per DEFECT, not per case: a defect no case seeds
    still has to be classed, because the mix counts every declared defect."""
    vocab = ", ".join(journey.DEFECT_CLASSES)
    found: list[Finding] = []
    for d in doc.get("defects") or []:
        d = d if isinstance(d, dict) else {}
        did = str(d.get("id") or "?")
        raw = d.get("class")
        if raw is None or not str(raw).strip():
            found.append(Finding("error", "class", did, f"defect has no `class:` — one of: {vocab}"))
        elif raw not in journey.DEFECT_CLASSES:
            found.append(Finding("error", "class", did,
                                 f"class {raw!r} is not in the vocabulary — one of: {vocab}"))
    return found


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')
_NUMBER = re.compile(r"\b\d+(?:[.:]\d+)?\b")
_CAPITAL = re.compile(r"\b[A-Z][a-z]{2,}\b")
_STOP = {"the", "and", "its", "with", "for", "from", "that", "this", "tab", "list", "screen",
         "home", "card", "shows", "shown", "listed", "saved", "new", "one", "both", "still",
         "instead", "longer", "opening", "checking"}


def brief_key_nouns(text: str) -> list[str]:
    """Quoted strings, numbers and capitalised words from a brief, lowercased."""
    nouns: list[str] = []
    for a, b in _QUOTED.findall(text):
        nouns.append((a or b).lower())
    for n in _NUMBER.findall(text):
        nouns.append(n.lower())
        if "." in n:                      # "40.00" in a brief is `TRANSAMOUNT=40` in SQL
            nouns.append(n.rstrip("0").rstrip("."))
    nouns += [n.lower() for n in _CAPITAL.findall(text) if n.lower() not in _STOP]
    seen: list[str] = []
    for n in nouns:
        if n and n not in seen:
            seen.append(n)
    return seen


def rule_columns(case: dict) -> list[Finding]:
    """Heuristic: a db/content query should mention at least one of the brief's key
    nouns — a title, a value, a name. Warn, never fail."""
    cid = str(case.get("id"))
    expect = ((case.get("check") or {}).get("expect")) or {}
    if not isinstance(expect, dict):
        return []
    if "db" in expect:
        hay = str(expect.get("query") or "")
    elif "content" in expect:
        hay = " ".join(str(expect.get(k) or "") for k in ("contains", "where", "equals"))
    else:
        return []
    hay = hay.lower()
    nouns = brief_key_nouns(str(case.get("expected_outcome") or ""))
    if not nouns:
        return []
    if any(n in hay for n in nouns):
        return []
    return [Finding("warning", "columns", cid,
                    f"oracle text mentions none of the brief's key nouns {nouns}")]


def rule_short(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    return [Finding("warning", "short", cid, f"evidence {e!r} is under {_MIN_WITNESS_CHARS} characters")
            for e in (case.get("evidence") or []) if len(_norm(e)) < _MIN_WITNESS_CHARS]


def _side_entries(case: dict, defects: dict[str, dict]) -> list[tuple[str, dict]]:
    """(bug id, raw `bugs:` entry as a mapping) for each DISPLAY bug the case seeds. A
    bare-id entry comes back as `{"id": <id>}`, so a missing `reference:` reads the same
    whichever form the author used. Read off the raw list: `journey.case_bugs` keeps only
    id and marker, which is exactly why `reference:` can never reach a scorer."""
    out: list[tuple[str, dict]] = []
    for raw in case.get("bugs") or []:
        entry = dict(raw) if isinstance(raw, dict) else {"id": raw}
        bug_id = str(entry.get("id"))
        if (defects.get(bug_id) or {}).get("kind") == "display":
            out.append((bug_id, entry))
    return out


def rule_reference(case: dict, defects: dict[str, dict], heldout: bool = False) -> list[Finding]:
    """Every DISPLAY side bug records how the brief lets the agent know its value is
    wrong: `reference: {kind: stated|entered|cross-check, note: <one sentence>}`. The lint
    checks the key's shape, not the claim — whether the note is TRUE is a review
    question, the same way `class:` is. Missing on a `witness_before_action:` case or a
    held-out app is a warning naming the owner (see the module docstring)."""
    cid = str(case.get("id"))
    kinds = ", ".join(REFERENCE_KINDS)
    deferred = case.get("witness_before_action")
    found: list[Finding] = []
    for bug_id, entry in _side_entries(case, defects):
        ref = entry.get("reference")
        if ref is None:
            if deferred:
                found.append(Finding("warning", "reference", cid,
                                     f"side bug {bug_id} has no `reference:` — deferred to "
                                     f"{deferred}, which re-authors this case"))
            elif heldout:
                found.append(Finding("warning", "reference", cid,
                                     f"side bug {bug_id} has no `reference:` — held-out split, "
                                     f"audit pending (corpus owner)"))
            else:
                found.append(Finding("error", "reference", cid,
                                     f"side bug {bug_id} has no `reference:` — record how the "
                                     f"brief lets the agent know the value is wrong ({kinds}), "
                                     f"or amend the brief, or drop the bug from this case"))
            continue
        if not isinstance(ref, dict):
            found.append(Finding("error", "reference", cid,
                                 f"side bug {bug_id}: `reference:` must be a mapping "
                                 f"{{kind, note}}, got {ref!r}"))
            continue
        kind, note = ref.get("kind"), str(ref.get("note") or "").strip()
        if kind not in REFERENCE_KINDS:
            found.append(Finding("error", "reference", cid,
                                 f"side bug {bug_id}: reference kind {kind!r} is not one of: {kinds}"))
        if not note:
            found.append(Finding("error", "reference", cid,
                                 f"side bug {bug_id}: reference has no `note:` saying where "
                                 f"the brief gives the value away"))
        elif kind == "stated" and not _STATED_WHERE.search(note):
            found.append(Finding("error", "reference", cid,
                                 f"side bug {bug_id}: a `stated` note must name the step or "
                                 f"the expected outcome that states the value — the journey "
                                 f"agent reads only name, steps and expected outcome, never "
                                 f"`brief: intended_behavior`"))
    return found


def rule_quotable(case: dict, defects: dict[str, dict]) -> list[Finding]:
    """A display side bug's effective marker (the per-case override, else the defect's)
    must clear the matcher's evidence floor, or no report can ever quote it."""
    cid = str(case.get("id"))
    display = {b for b, _ in _side_entries(case, defects)}
    return [Finding("error", "quotable", cid,
                    f"side bug {bug_id}'s marker {marker!r} is under "
                    f"{journey._MIN_EVIDENCE_CHARS} characters — no report can quote it; "
                    f"give it a longer screen string or retire it from the case")
            for bug_id, marker, _ in _case_defects(case, defects)
            if bug_id in display and not journey._evidence(marker)]


def rule_refreport(case: dict, defects: dict[str, dict], row: dict | None = None,
                   app_name: str = "") -> list[Finding]:
    """Every defect a case seeds carries a `reference_report:` on its `bugs:` entry, and
    the report holds up: `reference_reports.problems` puts it through the checks the
    deterministic report-quality gates put an agent's report through, against this
    case's truth row. One finding per problem, so a file can be fixed in one pass. A
    case whose `bugs:` do not resolve is left to `case_design`'s own error."""
    cid = str(case.get("id"))
    try:
        journey.case_design(case, defects)
    except ValueError:
        return []
    found: list[Finding] = []
    for bug_id, raw in reference_reports._raw_entries(case):
        if raw is None:
            found.append(Finding("error", "refreport", cid,
                                 f"bug {bug_id} has no `{reference_reports.KEY}:` — write the "
                                 f"report a tester would file for it on this case"))
            continue
        for problem in reference_reports.problems(case, defects, row, bug_id, raw, app_name):
            found.append(Finding("error", "refreport", cid, f"bug {bug_id}: {problem}"))
    return found


def rule_drift(doc: dict, spec_drift_ids: set[str] | None = None) -> list[Finding]:
    """QUA-3490: the case file's `drift:` block. A drift flag is a BENIGN UI change, so
    it must never be confusable with a defect: its id is no defect id and no case
    lists it under `bugs:`; its kind is one of `journey.DRIFT_KINDS`; it says what
    changed (`change:`); every rewrite maps a well-formed route step onto well-formed
    steps and actually applies to some case (or the flag names its cases in
    `affects:`); and — when the benchmark spec is given — the case file declares
    exactly the spec's drift ids (a flag patched in but undeclared would be switched
    on by no stage; one declared but never patched would change nothing)."""
    found: list[Finding] = []
    defects = set(journey.load_defects(doc))
    cases = {str(c.get("id")): c for c in doc.get("test_cases") or []}
    seeded = {b["id"] for c in cases.values() for b in journey.case_bugs(c)}
    raw = doc.get(journey.DRIFT_KEY) or []
    if not isinstance(raw, list):
        return [Finding("error", "drift", "drift", "`drift:` must be a list")]
    seen: set[str] = set()
    for d in raw:
        did = str((d or {}).get("id") or "") if isinstance(d, dict) else ""
        if not did:
            found.append(Finding("error", "drift", "drift", f"entry without an id: {d!r}"))
            continue
        if did in seen:
            found.append(Finding("error", "drift", did, "duplicate drift id"))
        seen.add(did)
        if did in defects:
            found.append(Finding("error", "drift", did, "drift id is also a defect id"))
        if did in seeded:
            found.append(Finding("error", "drift", did,
                                 "a case lists this drift flag under `bugs:` — drift is not a defect"))
        if d.get("kind") not in journey.DRIFT_KINDS:
            found.append(Finding("error", "drift", did,
                                 f"kind {d.get('kind')!r} — one of {', '.join(journey.DRIFT_KINDS)}"))
        if not str(d.get("change") or "").strip():
            found.append(Finding("error", "drift", did, "no `change:` saying what the UI change is"))
        for i, rule in enumerate(d.get("rewrite") or [], 1):
            if not isinstance(rule, dict) or "from" not in rule or "to" not in rule:
                found.append(Finding("error", "drift", did, f"rewrite {i}: needs `from:` and `to:`"))
                continue
            to = rule["to"] if isinstance(rule["to"], list) else [rule["to"]]
            for item in [rule["from"], *to]:
                shape = route_item(item)
                problem = ("not a route step" if shape is None
                           else step_problem(shape[0], shape[1], row=shape[2]))
                if problem:
                    found.append(Finding("error", "drift", did, f"rewrite {i}: {item!r}: {problem}"))
        for cid in d.get("affects") or []:
            if str(cid) not in cases:
                found.append(Finding("error", "drift", did, f"`affects:` names unknown case {cid!r}"))
    defs = journey.load_drift(doc)
    for did, ddef in defs.items():
        if ddef["rewrite"] and not any(journey.drift_affects(c, did, defs) for c in cases.values()):
            found.append(Finding("error", "drift", did, "no rewrite rule matches any case's route"))
        if not ddef["rewrite"] and not ddef["affects"]:
            found.append(Finding("error", "drift", did,
                                 "neither a rewrite nor `affects:` — no case would derive it"))
    if spec_drift_ids is not None:
        for did in sorted(spec_drift_ids - seen):
            found.append(Finding("error", "drift", did,
                                 "patched in the benchmark spec but not declared in the case file"))
        for did in sorted(seen - spec_drift_ids):
            found.append(Finding("error", "drift", did,
                                 "declared in the case file but not patched in the benchmark spec"))
    return found


def rule_known(doc: dict) -> list[Finding]:
    """The app's `known_behaviours:` list loads (QUA-3541). One finding, the loader's
    own message: a malformed answer-key entry must stop the task build, never score."""
    try:
        journey.load_known_behaviours(doc)
    except ValueError as exc:
        return [Finding("error", "known", journey.KNOWN_KEY, str(exc))]
    return []


def lint_doc(doc: dict, truth: dict | None = None, heldout: bool = False,
             spec_drift_ids: set[str] | None = None, app_name: str = "") -> list[Finding]:
    """Every rule over one loaded test-case document. `truth` is the app's measured
    journey truth (optional — the diff-based leak check is skipped without it, and a
    reference report then has no truth text to quote); `heldout` downgrades a missing
    side-bug `reference:` to a warning (rule_reference); `app_name` is the app's display
    name, which Android's crash dialog carries (rule_refreport)."""
    defects = journey.load_defects(doc)
    findings: list[Finding] = rule_class(doc) + rule_drift(doc, spec_drift_ids) + rule_known(doc)
    for case in doc.get("test_cases") or []:
        findings += rule_no_oracle(case)
        findings += rule_route(case)
        findings += rule_leak(case, defects, truth)
        findings += rule_brief(case, defects)
        findings += rule_brief_symptom(case, defects)
        findings += rule_columns(case)
        findings += rule_short(case)
        findings += rule_reference(case, defects, heldout=heldout)
        findings += rule_quotable(case, defects)
        findings += rule_refreport(case, defects, (truth or {}).get(str(case.get("id"))),
                                   app_name)
    return findings


def lint_corpus(app_ids: list[str] | None = None) -> dict[str, list[Finding]]:
    """{app_id: findings} for every test-case file, loaded through journey.load_cases —
    the packaged corpus plus, when QGB_HELDOUT_DIR is set, the held-out split (a held-out
    case can leak its defect exactly like a public one)."""
    from qualgentbench import corpus
    out: dict[str, list[Finding]] = {}
    for app_id in sorted(set(corpus.public_apps()) | set(corpus.heldout_apps())):
        if app_ids and app_id not in app_ids:
            continue
        doc = journey.load_cases(app_id)
        if not doc:
            out[app_id] = [Finding("error", "no-oracle", app_id, "file did not load")]
            continue
        spec_drift: set[str] | None = None
        app_name = app_id
        spec_path = corpus.spec_path(app_id)
        if spec_path.exists():
            import yaml
            spec = yaml.safe_load(spec_path.read_text()) or {}
            spec_drift = {str(d.get("id")) for d in spec.get("drift") or [] if isinstance(d, dict)}
            app_name = str((spec.get("app") or {}).get("name") or app_id)
        out[app_id] = lint_doc(doc, journey.load_truth(app_id),
                               heldout=app_id in set(corpus.heldout_apps()),
                               spec_drift_ids=spec_drift, app_name=app_name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--app", help="comma-separated app ids (default: every test-case file)")
    args = ap.parse_args(argv)
    app_ids = [a.strip() for a in args.app.split(",")] if args.app else None

    results = lint_corpus(app_ids)
    if not results:
        print("FAIL: no test-case files found")
        return 1
    errors = warnings = cases = 0
    for app_id, findings in results.items():
        doc = journey.load_cases(app_id) or {}
        cases += len(doc.get("test_cases") or [])
        for f in findings:
            print(f"{app_id:18s} {f}")
            if f.level == "error":
                errors += 1
            else:
                warnings += 1
    print(f"\n{cases} case(s) in {len(results)} file(s): {errors} error(s), {warnings} warning(s)")
    if errors:
        print("FAIL: a completion witness or a brief carries a seeded defect, a case has "
              "no oracle, a route step is not replayable, a defect has no valid class, "
              "a side bug has no reference in its brief or no quotable marker, or a "
              "seeded defect has no sound reference report")
        return 1
    print("PASS: no leaked marker, no oracle-less case, every route step replayable, "
          "every defect classed, every side bug referenced and quotable, every seeded "
          "defect with a sound reference report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
