"""Plain-language definitions for the view's pages (QUA-2938).

One line per term, for a reader who has never seen the benchmark, grounded in
docs/scoring.md (the journey rates), docs/heldout.md (the held-out split) and
`create/grader.py` / `create/ab.py` (CreateBench). The view prints them as `title=`
tooltips, in the run page's "How to read this page" box and in `manifest.json`'s
`notes.plain`; `create/board.py` uses them on the create board's charts.

The optional HELP-LINK BASE: when a build sets one (`view --help-base PATH`, or the
`QGB_VIEW_HELP_BASE` env var; default off, so a local or portable view stays
self-contained), every term the index marks (`data-term`) also gets a small "?" link to
`<base>#<anchor>` — a documentation page the reader is given beside the view. The anchor
of each term is fixed (`TERMS`), drawn from `ANCHORS` only. The base is a path, never a
URL: a value with a scheme (`x://`) or a network path (`//`) is refused, so the view's
pages still never carry `http`. `help_base(base)` scopes it to one render, so the pure
renderers below read it without a parameter threaded through every helper.

Public API:

  TERMS              term -> (plain definition, anchor)
  PLAIN              term -> plain definition (the manifest's `notes.plain`)
  ANCHORS            the fixed anchor list a documentation page provides
  ENV                the help-link base's env var
  check_base(v)      the validated base, or None (off); raises ValueError on a URL
  help_base(base)    context manager: the base for one render
  attrs(term, tip)   ` data-term=… title=…` for an element that defines `term`
  link(term)         the "?" link for `term`, or "" with no base set
  term(html, key)    an inline `<span>` defining `key`, plus its link
"""

from __future__ import annotations

import html
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .journey import INTEGRITY_N

ENV = "QGB_VIEW_HELP_BASE"

#: The anchors a documentation page for these views provides (fixed: a page is written
#: against this list, so a term never links to an anchor outside it).
ANCHORS = ("catch", "false-alarm", "integrity", "completion", "range", "held-out", "set",
           "basis", "board", "scoring-artifact", "cost", "lanes", "createbench", "detected",
           "trials", "episode", "verdict", "strong-test", "power", "uptake")

TERMS: dict[str, tuple[str, str]] = {
    # The journey board (docs/scoring.md, "Journey mode").
    "episode": (
        ("One run of one agent on one test case, on one build of the app."),
        "episode"),
    "seeded case": (
        ("A test case run on a build with a known bug planted on purpose."),
        "episode"),
    "clean case": (
        ("The same test case run on a build with no planted bug: the right answer there "
         "is that nothing is wrong."),
        "episode"),
    "catch": (
        ("The share of planted bugs the agent found and reported. Higher is better."),
        "catch"),
    "false alarm": (
        ("The share of clean runs in which the agent reported a bug that is not there. "
         "Lower is better."),
        "false-alarm"),
    "integrity": (
        (f"The chance that {INTEGRITY_N} clean tests in a row raise no false alarm, at "
         "this false-alarm rate. Higher is better."),
        "integrity"),
    "false reports": ("How many reported bugs matched no planted bug.", "false-alarm"),
    "completion": (
        ("The share of runs in which the agent finished the test and gave the right pass"
         " or fail answer. Shown, but never used to rank: some runs cannot be checked."),
        "completion"),
    "cost": (
        ("Average model spend per episode, in US dollars, over episodes with a known "
         "price."),
        "cost"),
    "minutes": ("Typical (median) minutes the agent took per episode.", "cost"),
    "episodes": (
        ("How many episodes this row's numbers come from. Excluded episodes (a broken "
         "device or setup) count in no number."),
        "episode"),
    "held-out": (
        ("Apps whose test files are kept out of the public repository, so no model can "
         "have trained on them. Their rows are ranked in their own block."),
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
        "board"),
    "row": ("The agent, the model it ran on, and the setup it ran under.", "lanes"),
    "recorded": (
        ("The numbers as first scored when the run happened, shown only where today's "
         "scoring differs."),
        "scoring-artifact"),
    # The versions line: what the run measured.
    "benchmark": (
        ("Which kind of benchmark these episodes are: running written test cases, or "
         "writing them."),
        "board"),
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
        "scoring-artifact"),
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
        "createbench"),
    "trials": (
        ("How many times the same brief was written and graded for each arm."),
        "trials"),
    "prediction": (
        ("What the experiment wrote down, before any data existed, that it expected to "
         "see. It cannot change once the experiment starts."),
        "createbench"),
    "verdict": ("Whether the results matched what was written down in advance.", "verdict"),
    "detected": (
        ("Every result the experiment wrote down in advance came true."),
        "detected"),
    "power": ("Whether a written test catches the bug it was written for.", "power"),
    "repeatability": (
        ("Whether a written test passes every time on the build with no bug."),
        "strong-test"),
    "specificity": (
        ("Whether a written test still passes when an unrelated bug is turned on."),
        "strong-test"),
    "strong-test": (
        ("A written test that passes the free static checks, passes every run on the "
         "build with no bug, catches the bug it was written for and still passes when an"
         " unrelated bug is on."),
        "strong-test"),
    "uptake": (
        ("Whether a written test actually follows the rule arm B was given."),
        "uptake"),
    "rate axis": (
        ("0% to 100%. The dot is the measured rate; the line through it is the range "
         "where the true rate likely lies."),
        "range"),
}
PLAIN = {k: d for k, (d, _) in TERMS.items()}

_BASE: ContextVar[str | None] = ContextVar("qgb_view_help_base", default=None)


def check_base(value: str | None) -> str | None:
    """The help-link base as a build uses it: None or "" = off; a path (relative or
    absolute) is kept; a URL is refused (the view's pages never carry one)."""
    if not value:
        return None
    v = value.strip()
    if "://" in v or v.startswith("//") or any(c in v for c in "#\"'<>") or not v:
        raise ValueError(f"help-link base {value!r} must be a path to a documentation page "
                         f"(no scheme, no '#', no quotes): e.g. ../glossary.html")
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
    return f' data-term="{html.escape(term)}" title="{html.escape(tip or PLAIN[term])}"'


def link(term: str) -> str:
    """The "?" link to `term`'s anchor on the documentation page, or "" with no base."""
    base = _BASE.get()
    if not base:
        return ""
    return (f'<a class="help" href="{html.escape(base)}#{TERMS[term][1]}" '
            f'aria-label="what {html.escape(term)} means">?</a>')


def term(inner_html: str, key: str, tip: str | None = None) -> str:
    """`inner_html` (already escaped) as an inline element defining `key`, plus its link."""
    return f'<span class="term"{attrs(key, tip)}>{inner_html}</span>{link(key)}'
