"""CreateBench v2: did an authored case TAKE the harmful rule? (QUA-2864)

The harmful-rule positive control (QUA-2861) changes ONE line of a creation arm's
private surface and predicts that the arm's authored cases lose power. QUA-2861's first
run showed why that prediction needs a MANIPULATION CHECK: arm B's author read the rule
on 7/7 cells and followed it 0/7 times, so a MISSED verdict would have measured the
author ignoring the rule, not the benchmark's sensitivity. This module is that check: a
deterministic classifier over the authored case (its steps and `expected_result`, never
an LLM, never a live result) that says whether the case is what the rule asks for.

A rule is public text (it is the harmful rule itself, never the private surface it is
placed in) plus what a case that follows it looks like:

    check        the ONE verification the rule allows (a regex over a step's text);
    expected     what `expected_result` must say, when the rule constrains it (the
                 frozen journey runner compares the run against `expected_result`, so a
                 rule that leaves it alone leaves the outcome check in place);
    drop_classes the defect classes whose target a case following the rule provably
                 cannot catch (see `APP_OPEN`). A registration that pairs the rule with
                 a DROP brief outside these classes is refused (`ab.check_design`).

A case TAKES the rule (`classify(...).taken`) iff it has at least one check, every check
is the rule's check, the LAST step is that check, and `expected_result` matches when the
rule constrains it. A case that keeps any outcome check, or appends the rule's check
after one (QUA-2861's medtimer-check-stock: literal compliance, outcome check kept), has
not taken it. A cell with no authored case has not taken it either.

What counts as a check: a step tagged `verify`, or an untagged one that opens with an
assertion or observation verb (`verify`, `check`, `confirm`, `ensure`, `assert`, `make
sure`, `validate`, `expect`, `read`, `look at`, `observe`, `inspect`, `note`). A `read`
step is counted because the runner is told to compare what it observes with the expected
outcome: an observation step is where an outcome check hides when it is not tagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import lint as _lint

#: Bump when the classification rules above change.
CLASSIFIER_VERSION = 1

_OBSERVE_OPENERS = re.compile(
    r"^\s*(verify|check|confirm|ensure|assert|make\s+sure|validate|expect|read|look\s+at|"
    r"observe|inspect|note)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    id: str
    text: str
    check: re.Pattern[str]
    expected: re.Pattern[str] | None = None
    drop_classes: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "check": self.check.pattern,
                "expected": self.expected.pattern if self.expected else None,
                "drop_classes": sorted(self.drop_classes),
                "classifier_version": CLASSIFIER_VERSION}


_I = re.IGNORECASE

#: QUA-2861's rule (arm B = QualGent-MCP throwaway/createbench-v2-harmful-rule, the
#: guide's DO list). Kept to re-measure that run. It names no `expected_result`, so even
#: a case that takes it keeps the outcome check the runner reads there, and a title check
#: quoting the EXPECTED screen still catches a wrong-screen navigation: it provably
#: removes no target's power (`drop_classes` empty).
SCREEN_TITLE = Rule(
    id="screen-title/v1",
    text="End every test with a single Verify step that the current screen's title is visible.",
    # A SCREEN's title (by that name, or quoted), not an item's ("the updated title").
    check=re.compile(r"^\s*(verify|check|confirm|ensure|assert)\b.*"
                     r"((screen|page)('s)?\s+title|\"[^\"]+\"\s+(screen\s+)?title|"
                     r"title\s+of\s+the\s+(current\s+)?(screen|page)).*"
                     r"\b(visible|shown|displayed|appears)\b", _I))

#: QUA-2864's rule. Its check observes only that the app process is still up and in
#: front, and its expected result says only that. On a PERSISTENCE target (an alive
#: `assert` class: the write is skipped, and the app closes the editor or returns along
#: its normal path) every step of a case that follows it still succeeds and its only
#: check holds, so the runner's verdict on the target build is PASS: the case cannot
#: catch the target. A NAVIGATION target is not in `drop_classes`: on the wrong screen a
#: later act step can fail, so a check-free case can still FAIL there. A walk target
#: (crash/ANR/stuck) fails this check as well as the walk, so its power stays. The class
#: is necessary, not sufficient: a persistence target whose feature's natural route ACTS
#: on the stored state afterwards (favorite-not-saved: open the Favorites tab, tap the
#: contact there) breaks that act step just like a wrong screen does — QUA-2864's leak
#: probe caught exactly that, and the subset (data/create/positive-control-v2.yaml)
#: leaves it out per target.
APP_OPEN = Rule(
    id="app-open/v2",
    text=("Required final step: do not verify the feature's result or any saved value; "
          "end every test with exactly one verify step, \"Verify the app is still open\", "
          "and set expected_result to \"The app is still open.\" This overrides every "
          "other verification guideline."),
    check=re.compile(r"^\s*(verify|check|confirm|ensure)\s+(that\s+)?(the\s+)?"
                     r"([\w'-]+\s+){0,3}?(app|application)\s+(is\s+|remains\s+)(still\s+)?"
                     r"(open|running|launched|in\s+the\s+foreground)"
                     r"(\s+and\s+responsive)?\s*\.?\s*$", _I),
    expected=re.compile(r"^\s*(the\s+)?([\w'-]+\s+){0,3}?(app|application)\s+"
                        r"(is\s+|remains\s+)(still\s+)?(open|running|launched|"
                        r"in\s+the\s+foreground)(\s+and\s+responsive)?\s*\.?\s*$", _I),
    drop_classes=frozenset({"persistence"}))

RULES = {r.id: r for r in (SCREEN_TITLE, APP_OPEN)}

#: Each rule in plain words, for the experiment page (QUA-2941): what the changed arm's
#: test writer was told, said short. Never the rule's own text: that text also sits in
#: the arm's private surface, and the view refuses a page that repeats 40 words of it.
PLAIN = {SCREEN_TITLE.id: ("end every test with one check that the current screen's title"
                           " is visible"),
         APP_OPEN.id: ("never check the feature's result or a saved value; end every test"
                       " with one check that the app is still open")}


def is_check(step: _lint.Step) -> bool:
    """A verification or observation step (see the module doc)."""
    if step.kind:
        return step.kind == "verify" or bool(_OBSERVE_OPENERS.match(step.text))
    return bool(_OBSERVE_OPENERS.match(step.text))


@dataclass(frozen=True)
class Uptake:
    taken: bool
    why: str
    checks: tuple[str, ...] = ()
    outcome_checks: tuple[str, ...] = ()
    final_is_rule: bool = False
    expected_ok: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"taken": self.taken, "why": self.why, "checks": list(self.checks),
                "outcome_checks": list(self.outcome_checks),
                "final_is_rule": self.final_is_rule, "expected_ok": self.expected_ok}


def classify(raw: Any, rule: Rule | str) -> Uptake:
    """Whether one authored case (a create body, a stored case, or `authored_case.json`)
    takes `rule`. Pure. `raw` None (no case was saved) has not taken it."""
    rule = RULES[rule] if isinstance(rule, str) else rule
    if raw is None:
        return Uptake(False, "no authored case")
    if isinstance(raw, dict):
        raw = next((raw[k] for k in ("case", "request") if isinstance(raw.get(k), dict)), raw)
    try:
        case = _lint.normalize_case(raw)
    except (TypeError, ValueError) as exc:
        return Uptake(False, f"unreadable case: {exc}")
    steps = [s for s in case.steps if s.text.strip()]
    checks = [s for s in steps if is_check(s)]
    texts = tuple(s.text for s in checks)
    outcome = tuple(s.text for s in checks if not rule.check.search(s.text))
    final = bool(steps) and is_check(steps[-1]) and bool(rule.check.search(steps[-1].text))
    exp_ok = None if rule.expected is None else bool(rule.expected.search(case.expected_result
                                                                          or ""))
    if not checks:
        return Uptake(False, "no check step at all", texts, outcome, final, exp_ok)
    if outcome:
        return Uptake(False, f"{len(outcome)} outcome check(s) kept", texts, outcome, final,
                      exp_ok)
    if not final:
        return Uptake(False, "the last step is not the rule's check", texts, outcome, final,
                      exp_ok)
    if exp_ok is False:
        return Uptake(False, "expected_result is not the rule's", texts, outcome, final,
                      exp_ok)
    return Uptake(True, "the rule's check is the only check and the last step"
                  + ("" if exp_ok is None else ", and expected_result is the rule's"),
                  texts, outcome, final, exp_ok)


def classify_artifact(path: Any, rule: Rule | str) -> Uptake:
    """`classify` on an `authored_case.json` (or an episode dir holding one)."""
    import json
    from pathlib import Path

    from .fake_api import AUTHORED_CASE_FILE
    if path is None:
        return classify(None, rule)
    p = Path(path)
    if p.is_dir():
        p = p / AUTHORED_CASE_FILE
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return classify(None, rule)
    return classify(doc, rule)
