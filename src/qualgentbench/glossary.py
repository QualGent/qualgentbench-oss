"""Plain-language definitions for the view's pages (QUA-2938).

One line per term, for a reader who has never seen the benchmark, grounded in
docs/scoring.md (the journey rates), docs/heldout.md (the held-out split) and
`create/grader.py` / `create/ab.py` (CreateBench). The view prints them as `data-tip`
tooltips (`tooltip.py`, QUA-2948), in the run page's "How to read this page" box and in `manifest.json`'s
`notes.plain`; `create/board.py` uses them on the create board's charts.

The optional HELP-LINK BASE: when a build sets one (`view --help-base PATH`, or the
`QGB_VIEW_HELP_BASE` env var; default off, so a local or portable view stays
self-contained), every term the index marks (`data-term`) also gets a small "?" link to
`<base>#<anchor>` — a documentation page the reader is given beside the view. The anchor
of each term is fixed (`TERMS`), drawn from `ANCHORS` only, and only to an entry that
defines the term (`ENTRIES`, QUA-2943); a term no entry defines gets no link. The base
is a path, never a URL: a value with a scheme (`x://`) or a network path (`//`) is refused, so the view's
pages still never carry `http`. `help_base(base)` scopes it to one render, so the pure
renderers below read it without a parameter threaded through every helper.

Public API:

  TERMS              term -> (plain definition, anchor | None)
  PLAIN              term -> plain definition (the manifest's `notes.plain`)
  ENTRIES            anchor -> the names its documentation entry defines
  ANCHORS            the fixed anchor list a documentation page provides
  ALIASES            term -> other names it goes by (the anchor check)
  ENV                the help-link base's env var
  HOME_ENV           the home-link base's env var (QUA-2941; checked like the help base)
  check_base(v)      the validated base, or None (off); raises ValueError on a URL
  help_base(base)    context manager: the base for one render
  attrs(term, tip)   ` data-term=… data-tip=…` for an element that defines `term`
  link(term)         the "?" link for `term`, or "" with no base set or no anchor
  term(html, key)    an inline `<span>` defining `key`, plus its link
"""

from __future__ import annotations

import html
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from . import tooltip
from .journey import INTEGRITY_N

ENV = "QGB_VIEW_HELP_BASE"
#: The view's home-link base's env var (QUA-2941, `view --home-base`); checked by
#: `check_base` like the help-link base.
HOME_ENV = "QGB_VIEW_HOME_BASE"

#: What each anchor's entry on the documentation page defines (QUA-2943): the names a
#: reader who follows a "?" link to `#<anchor>` finds explained there — the entry's own
#: heading and the terms its definition spells out. The anchors are a fixed contract (a
#: page is written against this list; rename none, only add), so a term never links to
#: an anchor outside it, and a term links to an anchor only when the term, or one of its
#: `ALIASES`, is a name that entry defines (`tests/test_view.py`). A term no entry
#: defines carries its tooltip and no link (`None` in `TERMS`).
ENTRIES: dict[str, tuple[str, ...]] = {
    "catch": ("catch",),
    "false-alarm": ("false alarm",),
    "integrity": ("integrity",),
    "completion": ("completion",),
    "range": ("range", "95% interval"),
    "held-out": ("held-out",),
    "set": ("set", "benchmark version", "test cases", "agent instructions"),
    "basis": ("basis", "recorded", "re-scored"),
    "board": ("board", "subset", "smoke"),
    "scoring-artifact": ("scoring artifact",),
    "cost": ("cost", "$/ep", "min/ep", "minutes"),
    "lanes": ("lanes", "row", "agent cli", "tools", "arm", "harness", "scorer"),
    "createbench": ("createbench", "brief", "a/b experiment"),
    "detected": ("detected",),
    "trials": ("trials",),
    "episode": ("episode",),
    "verdict": ("verdict", "pass or fail call"),
    "strong-test": ("strong test", "static checks"),
    "power": ("power",),
    "uptake": ("uptake",),
    # Added by QUA-2943: anchors the documentation page already carries.
    "seeded-defect": ("seeded defect", "planted bug", "seeded case", "clean case"),
    "false-report": ("false report",),
    "excluded": ("excluded",),
    "prediction": ("prediction",),
    "cells": ("cells", "cell"),
    # Added by QUA-2943: new entries the documentation page must add.
    "repeatability": ("repeatability",),
    "specificity": ("specificity",),
    "assert-briefs": ("assert briefs",),
    "walk-briefs": ("walk briefs",),
}
ANCHORS = tuple(ENTRIES)

#: Other names a term goes by on the pages, for the anchor check above: what the term's
#: own definition calls it, in the words of the entry it links to.
ALIASES: dict[str, tuple[str, ...]] = {
    "false reports": ("false report",),
    "agent verdict": ("pass or fail call",),
    "episodes": ("episode",),
    "planned episodes": ("episode",),
    "held-out badge": ("held-out",),
    "held-out split": ("held-out",),
    "rate axis": ("range",),
    "corpus": ("test cases",),
    "brief version": ("agent instructions",),
    "setup": ("arm", "tools"),
    "device tools": ("tools",),
    "rescore": ("re-scored",),
    "experiment arm": ("a/b experiment",),
    "strong-test": ("strong test",),
    "lint": ("static checks",),
    "recorded score": ("recorded",),
    "rescored score": ("re-scored",),
    "rescored with": ("re-scored",),
    "scorer version": ("scorer",),
    "episode cost": ("cost",),
}

TERMS: dict[str, tuple[str, str | None]] = {
    # The journey board (docs/scoring.md, "Journey mode").
    "episode": (
        ("One attempt by one agent at one test case, on one build of the app."),
        "episode"),
    "seeded case": (
        ("A test case run on a build with a known bug planted on purpose."),
        "seeded-defect"),
    "clean case": (
        ("The same test case run on a build with no planted bug: the right answer there "
         "is that nothing is wrong."),
        "seeded-defect"),
    "catch": (
        ("The share of planted bugs the agent found and reported. Higher is better."),
        "catch"),
    "false alarm": (
        ("The share of clean episodes in which the agent reported a bug that is not "
         "there. Lower is better."),
        "false-alarm"),
    "integrity": (
        (f"The chance that {INTEGRITY_N} clean tests in a row raise no false alarm, at "
         "this false-alarm rate. Higher is better."),
        "integrity"),
    "false reports": ("How many reported bugs matched no planted bug.", "false-report"),
    "completion": (
        ("The share of episodes in which the agent finished the test and gave the right"
         " pass or fail answer. Shown, but never used to rank: some episodes cannot be "
         "checked."),
        "completion"),
    "agent verdict": (
        ("The pass or fail answer the agent gave for the test itself. It is separate from"
         " catch: a planted bug fails the test only when it blocks the test's steps, so on"
         " a seeded build the right answer is often pass while the agent's bug report "
         "still names the planted bug, and that counts as a catch."),
        "verdict"),
    "cost": (
        ("Average model spend per episode, in US dollars, over episodes with a known "
         "price."),
        "cost"),
    "minutes": ("Typical (median) minutes the agent took per episode.", "cost"),
    "episodes": (
        ("How many episodes this row's numbers come from. Excluded episodes (the setup "
         "failed or the episode broke a rule) count in no number."),
        "episode"),
    "excluded": (
        ("An episode that measured nothing: the setup failed, or the episode broke a rule"
         " (for example the agent read files it must not). It is left out of every "
         "number, never counted as zero."),
        "excluded"),
    "planned episodes": (
        ("How many of the episodes the run planned are done, and how many it still owes."
         " A run that still owes episodes is in progress."),
        "episode"),
    "held-out": (
        ("Apps whose test files are kept out of the public repository, so no model can "
         "have trained on them. Their rows are shown separately and are too few to rank "
         "on."),
        "held-out"),
    "held-out badge": (
        ("This episode is on a held-out app. Do not share it: sharing would let a model "
         "train on the test."),
        "held-out"),
    "range": (
        ("The bracketed range is where the true rate likely lies (95% confidence). Fewer"
         " episodes give a wider range."),
        "range"),
    "rank": (
        ("Position within its block: fewest false alarms first, then most bugs caught."),
        None),
    "row": ("The agent, the model it ran on, and the setup it ran under.", "lanes"),
    "recorded": (
        ("The numbers as first scored when the run happened, shown only where today's "
         "scoring differs."),
        "basis"),
    # One episode's page (QUA-2956): the verdict table's columns and facts, for one
    # episode rather than a whole board.
    "recorded score": (
        ("The score saved with this episode, written when it ran unless the "
         "\"rescored with\" row says a later re-scoring replaced it."),
        "basis"),
    "rescored score": (
        ("The saved episode scored again with the rules current when this view was "
         "first built. Nothing is rerun, so a difference from the recorded column is a "
         "change in scoring, not in the agent."),
        "basis"),
    "rescored with": (
        ("Whether a later re-scoring replaced the recorded score, and if so which "
         "scoring rules and test-case version it used."),
        "basis"),
    "scorer version": (
        ("The version of the scoring rules behind each column: the recorded score's, and"
         " the rescored one's."),
        "lanes"),
    "step budget": (
        ("How many steps (actions on the device) the agent took, and the most it was "
         "allowed in this episode."),
        None),
    "episode cost": (
        ("What this episode cost in model spend, in US dollars, and how long it took. The"
         " word in brackets says where the price came from, for example reported by the "
         "agent or estimated from its usage."),
        "cost"),
    # The versions line: what the run measured.
    "benchmark": (
        ("Which kind of benchmark these episodes are: running written test cases, or "
         "writing them."),
        None),
    "corpus": (
        ("A short fingerprint of the test cases and the answer key. Two boards are "
         "comparable only when this matches."),
        "set"),
    "held-out split": (
        ("The fingerprint of the held-out apps' files, and how many episodes ran on "
         "them."),
        "held-out"),
    "brief version": (
        ("The version of the instructions every agent is given. A change of wording is a"
         " change of test."),
        "set"),
    "setup": (
        ("How the agent reached the device: through device tools, or the bare command "
         "line."),
        "lanes"),
    "device tools": (
        ("The version of the device tool server the agent used, if any."),
        "lanes"),
    "set": (
        ("The comparable set: runs with the same set name measured the same thing and "
         "can be compared."),
        "set"),
    "scorer": (
        ("The version of the scoring rules that produced the numbers. Listed beside the "
         "set: the reader decides whether two scorers compare."),
        "lanes"),
    "agent CLI": (
        ("The version of each agent's command-line program. Listed, never part of the "
         "set."),
        "lanes"),
    "harness": (
        ("The version of this benchmark's own code that ran the episodes. Listed, never "
         "part of the set."),
        "lanes"),
    "rescore": (
        ("How today's scoring of the saved episodes relates to the scoring recorded when"
         " they ran. A change here can be a scoring artifact, not a change in the agent."),
        "basis"),
    # CreateBench (create/grader.py, create/ab.py).
    "brief": (
        ("A short description of a feature, from which an agent writes a test case."),
        "createbench"),
    "experiment arm": (
        ("One of the two ways of writing tests being compared: the first arm is the control, "
         "the second the change under test."),
        "createbench"),
    "cell": (
        ("One arm writing one test case for one brief, once, and that test being graded."),
        "cells"),
    "trials": (
        ("How many times the same brief was written and graded for each arm."),
        "trials"),
    "prediction": (
        ("What the experiment wrote down, before any data existed, that it expected to "
         "see. It cannot change once the experiment starts."),
        "prediction"),
    "verdict": ("Whether the results matched what was written down in advance.", "verdict"),
    "detected": (
        ("Every result the experiment wrote down in advance came true."),
        "detected"),
    "power": ("Whether a written test catches the bug it was written for.", "power"),
    "repeatability": (
        ("Whether a written test passes every time on the build with no bug."),
        "repeatability"),
    "specificity": (
        ("Whether a written test still passes when an unrelated bug is turned on."),
        "specificity"),
    "strong-test": (
        ("A written test that passes the free static checks, passes every run on the "
         "build with no bug, catches the bug it was written for and still passes when an"
         " unrelated bug is on."),
        "strong-test"),
    "lint": (
        ("Whether a written test passes the free static checks (its format and wording "
         "rules), before it is ever run."),
        "strong-test"),
    "uptake": (
        ("Whether a written test actually follows the rule the changed arm was given: "
         "the check that the change under test reached the tests at all."),
        "uptake"),
    "assert briefs": (
        ("Briefs whose planted bug stays silent unless the written test checks the "
         "result: only a test that checks the outcome can catch it."),
        "assert-briefs"),
    "walk briefs": (
        ("Briefs whose planted bug crashes or freezes the app on the way: almost any test"
         " that walks through the feature catches it, so they mostly measure reaching the"
         " feature."),
        "walk-briefs"),
    "positive control": (
        ("An experiment whose change is deliberately harmful, so the benchmark should "
         "detect it. It checks that the benchmark can see a known loss; it is not a "
         "product improvement."),
        None),
    "test outcome": (
        ("What a written test did when it was run. On the build with its bug it should "
         "fail (it caught the bug); on the build with no bug, or with only an unrelated "
         "bug on, it should pass."),
        None),
    "driver run": (
        ("The run that wrote and graded every test in this experiment; its id finds its "
         "files."),
        None),
    "rate axis": (
        ("0% to 100%. The dot is the measured rate; the line through it is the range "
         "where the true rate likely lies."),
        "range"),
}
PLAIN = {k: d for k, (d, _) in TERMS.items()}

_BASE: ContextVar[str | None] = ContextVar("qgb_view_help_base", default=None)


def check_base(value: str | None, what: str = "help-link base",
               example: str = "a path to a documentation page (no scheme, no '#', no "
                              "quotes): e.g. ../glossary.html") -> str | None:
    """The help-link base as a build uses it: None or "" = off; a path (relative or
    absolute) is kept; a URL is refused (the view's pages never carry one). `what` and
    `example` name the option in the error (the view's home-link base, QUA-2941, is
    checked the same way)."""
    if not value:
        return None
    v = value.strip()
    if "://" in v or v.startswith("//") or any(c in v for c in "#\"'<>") or not v:
        raise ValueError(f"{what} {value!r} must be {example}")
    return v


@contextmanager
def help_base(base: str | None) -> Iterator[None]:
    """Render with `base` as the help-link base (None: no links)."""
    token = _BASE.set(base)
    try:
        yield
    finally:
        _BASE.reset(token)


def attrs(term: str, tip: str | None = None) -> str:
    """The attributes of an element that defines `term`: its key and its tooltip."""
    return f' data-term="{html.escape(term)}"{tooltip.attr(tip or PLAIN[term])}'


def link(term: str) -> str:
    """The "?" link to `term`'s anchor on the documentation page, or "" with no base or
    when no entry there defines the term. Hovering it shows the term's definition."""
    base = _BASE.get()
    anchor = TERMS[term][1]
    if not base or anchor is None:
        return ""
    return (f'<a class="help" href="{html.escape(base)}#{anchor}" '
            f'aria-label="what {html.escape(term)} means"{tooltip.attr(PLAIN[term])}>?</a>')


def term(inner_html: str, key: str, tip: str | None = None) -> str:
    """`inner_html` (already escaped) as an inline element defining `key`, plus its link."""
    return f'<span class="term"{attrs(key, tip)}>{inner_html}</span>{link(key)}'
