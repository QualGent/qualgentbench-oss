"""CreateBench stage B: a free, deterministic lint of an AUTHORED test case.

CreateBench scores how well an agent writes a test case. Before any device time is
spent, the written artifact itself is checked here against the conventions a good
product test case follows: it opens the app, it asserts something, each step is one
interaction, credentials are referenced rather than written out, it anchors on stable
UI labels rather than pixels, fixture content or values that drift from run to run.

A HARD failure makes the case lint-dirty. Lint-clean is one conjunct of Strong-Test
(`strong = lint ok AND the frozen runner's executions graded right`), and every rule's
verdict is also kept as a per-rule diagnostic. A SOFT failure is style: reported,
never gating.

Accepted inputs (`normalize_case`), all reduced to one `Case`:

* the create body an authoring agent posts — `name`, `steps` as a list of
  `{"description", "kind"?, "credential_id"?}` objects or bare strings,
  `expected_result`, optional `description`;
* the stored case as the API returns it, where `steps` is ONE numbered string with one
  step per line, `N. [kind] description ## {credential-uuid}`, the bracketed kind and
  the trailing credential reference both optional. Iterating that string as a list
  was an August calibration bug (one "step" per character);
* either of those wrapped under `test_case`, `case` or `data`;
* a journey reference case from `data/test-cases/<app>.yaml` (`steps: [str]`,
  `expected_outcome`), so the reference corpus can be linted for a baseline.

The content-anchor denylist comes from the app's fixture data: the text values of the
SQLite databases its benchmark spec pushes in `device_setup` (`fixture_content_strings`).

CLI (exit 0 = no HARD failure, 1 = at least one, 2 = unreadable input):

    uv run python -m qualgentbench.create.lint case.json [--app medtimer] [--json]
    uv run python -m qualgentbench.create.lint --journey [APP ...] [--json]

`--journey` lints the packaged journey reference cases and prints the per-rule
baseline. Those cases were written for the journey runner, which launches the app
itself and never reads kind tags, so they fail several HARD rules by construction —
the baseline is a finding about the rules and the corpus, not a bug in either.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HARD = "hard"
SOFT = "soft"

MAX_STEPS = 15
MAX_NAME_LEN = 70          # a name of this many characters or more is too long
KINDS = ("setup", "act", "verify")


# ── the normalized case ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Step:
    index: int                    # 1-based, as a reader counts
    text: str
    kind: str | None
    credential_id: str | None


@dataclass(frozen=True)
class Case:
    name: str
    steps: tuple[Step, ...]
    expected_result: str
    description: str
    case_id: str | None


@dataclass(frozen=True)
class Violation:
    message: str
    step: int | None = None       # the step's 1-based index, None for case-level fields


@dataclass
class RuleResult:
    rule: str
    severity: str
    violations: list[Violation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass
class LintReport:
    case_name: str
    case_id: str | None
    results: list[RuleResult]

    @property
    def hard_failures(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed and r.severity == HARD]

    @property
    def soft_failures(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed and r.severity == SOFT]

    @property
    def ok(self) -> bool:
        """Lint-clean: no HARD rule failed. The Strong-Test conjunct."""
        return not self.hard_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_name": self.case_name,
            "ok": self.ok,
            "hard_failed": [r.rule for r in self.hard_failures],
            "soft_failed": [r.rule for r in self.soft_failures],
            "results": [
                {"rule": r.rule, "severity": r.severity, "passed": r.passed,
                 "violations": [{"message": v.message, "step": v.step} for v in r.violations]}
                for r in self.results
            ],
        }


# ── normalization ──────────────────────────────────────────────────────────────

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# The serialized credential reference a stored step carries: `## {uuid}` (braces optional).
_CRED_REF_RE = re.compile(r"##\s*\{?(" + _UUID + r")\}?")
# One serialized step line: `N.` or `N)`, an optional `[kind]`, then the text.
_STEP_LINE_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+(?:\[([A-Za-z]+)\]\s*)?(.*)$")


def parse_serialized_steps(text: str) -> list[dict[str, Any]]:
    """The API's steps string → step dicts. A numbered line opens a new step only when
    its number is the next one expected, so a number that happens to start a wrapped
    continuation line ("2. …" inside step 5's prose) never splits a step. A trailing
    `## {uuid}` becomes the step's `credential_id`."""
    steps: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _STEP_LINE_RE.match(line)
        if m and int(m.group(1)) == len(steps) + 1:
            steps.append({"description": m.group(3).strip(), "kind": m.group(2)})
        elif steps:
            steps[-1]["description"] = f"{steps[-1]['description']} {line.strip()}"
        else:
            steps.append({"description": line.strip(), "kind": None})
    for s in steps:
        ref = _CRED_REF_RE.search(s["description"])
        if ref:
            s["credential_id"] = ref.group(1)
    return steps


def _unwrap(raw: dict[str, Any]) -> dict[str, Any]:
    for key in ("test_case", "case", "data"):
        inner = raw.get(key)
        if isinstance(inner, dict) and ("steps" in inner or "name" in inner):
            return inner
    return raw


def normalize_case(raw: Any) -> Case:
    """Every accepted input shape (see the module docstring) → one `Case`."""
    if not isinstance(raw, dict):
        raise TypeError("a test case must be a JSON/YAML object")
    raw = _unwrap(raw)
    raw_steps = raw.get("steps")
    if isinstance(raw_steps, str):
        raw_steps = parse_serialized_steps(raw_steps)
    if raw_steps is not None and not isinstance(raw_steps, list):
        raise ValueError("`steps` must be a list or a numbered string")
    steps: list[Step] = []
    for i, s in enumerate(raw_steps or [], start=1):
        if isinstance(s, str):
            steps.append(Step(i, s.strip(), None, None))
        elif isinstance(s, dict):
            kind = s.get("kind")
            cred = s.get("credential_id")
            text = str(s.get("description") or s.get("text") or s.get("step") or "").strip()
            if not cred and (ref := _CRED_REF_RE.search(text)):
                cred = ref.group(1)
            steps.append(Step(i, text, str(kind).strip().lower() if kind else None,
                              str(cred) if cred else None))
        else:
            raise TypeError(f"step {i} is neither a string nor an object")
    case_id = raw.get("id") or raw.get("test_case_id")
    expected = raw.get("expected_result")
    if expected is None:
        expected = raw.get("expected_outcome")     # the journey corpus's name for it
    return Case(
        name=str(raw.get("name") or "").strip(),
        steps=tuple(steps),
        expected_result=str(expected or "").strip(),
        description=str(raw.get("description") or "").strip(),
        case_id=str(case_id) if case_id else None,
    )


# ── text helpers ───────────────────────────────────────────────────────────────

# A quoted span names an on-screen label or literal test data. Straight and curly
# double quotes always; single quotes only when they are not an apostrophe ("user's").
_QUOTE_RE = re.compile(
    r'"([^"\n]{1,80})"'
    r"|“([^”\n]{1,80})”"
    r"|(?<!\w)'([^'\n]{1,80})'(?!\w)"
    r"|(?<!\w)‘([^’\n]{1,80})’(?!\w)"
)


def quoted(text: str) -> list[str]:
    return [next(g for g in m.groups() if g is not None) for m in _QUOTE_RE.finditer(text)]


def _mask_quotes(text: str) -> str:
    """Replace every quoted span with a neutral `"_"`: words inside a label ("Save and
    Close") are not instructions, but the fact that a quote follows a joiner is kept."""
    return _QUOTE_RE.sub('"_"', text)


def _at(label: str, idx: int | None) -> str:
    """Message prefix naming a case-level field; a step is named by its index instead."""
    return "" if idx is not None else f"{label}: "


def _fields(case: Case) -> list[tuple[str, str, int | None]]:
    """(label, text, step index) for every persisted free-text field that is set."""
    out = [("name", case.name, None), ("description", case.description, None),
           ("expected result", case.expected_result, None)]
    out += [(f"step {s.index}", s.text, s.index) for s in case.steps]
    return [(label, text, idx) for label, text, idx in out if text]


# Assertion verbs: an untagged step that opens with one of these is read as a check.
_ASSERT_OPENERS = re.compile(r"^\s*(verify|check|confirm|ensure|assert|make\s+sure|validate)\b",
                             re.IGNORECASE)


def is_check(step: Step) -> bool:
    if step.kind:
        return step.kind == "verify"
    return bool(_ASSERT_OPENERS.match(step.text))


# ── rules ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Context:
    app_name: str | None = None
    content_strings: tuple[str, ...] = ()


Rule = Callable[[Case, Context], list[Violation]]


def created_via_api(case: Case, ctx: Context) -> list[Violation]:
    """Only a case the create call actually stored has an id; an artifact without one
    was never persisted (or was reconstructed from the transcript)."""
    return [] if case.case_id else [Violation("no case id: the case was never stored via the API")]


_LAUNCH_VERB = re.compile(r"\b(open|launch|start|relaunch|restart)\b", re.IGNORECASE)
_APP_WORD = re.compile(r"\bapp(lication)?\b", re.IGNORECASE)


def launch_first(case: Case, ctx: Context) -> list[Violation]:
    """Step 1 brings the app up: a launch verb aimed at "the app" or the app by name.
    "Open the Settings tab" opens a screen, not the app."""
    if not case.steps:
        return [Violation("the case has no steps")]
    first = case.steps[0].text
    names_app = bool(_APP_WORD.search(first)) or bool(
        ctx.app_name and ctx.app_name.casefold() in first.casefold())
    if _LAUNCH_VERB.search(first) and names_app:
        return []
    return [Violation("step 1 does not launch the app", 1)]


def verify_present(case: Case, ctx: Context) -> list[Violation]:
    """At least one step asserts the outcome (kind `verify`, or an untagged step that
    opens with an assertion verb). A case of pure actions checks nothing."""
    if any(is_check(s) for s in case.steps):
        return []
    return [Violation("no verify step: the case never asserts anything")]


def step_count(case: Case, ctx: Context) -> list[Violation]:
    n = len(case.steps)
    if n <= MAX_STEPS:
        return []
    return [Violation(f"{n} steps, more than {MAX_STEPS}: split the flow into two cases",
                      MAX_STEPS + 1)]


def kind_tags(case: Case, ctx: Context) -> list[Violation]:
    out = []
    for s in case.steps:
        if s.kind is None:
            out.append(Violation("step carries no kind (setup, act or verify)", s.index))
        elif s.kind not in KINDS:
            out.append(Violation(f"unknown kind {s.kind!r} (setup, act or verify)", s.index))
    return out


# ONE verb list behind every conjunction probe. In August the "and" probe knew a single
# verb ("verify") while the "then" probe knew thirty, so "Tap Save and verify X" failed
# and the identical "Tap Save and confirm X" passed (QUA-2612, never merged). With a
# lint conjunct inside the headline metric, that made a prompt A/B that merely changed
# the author's word choice move the score. A verb added here applies to every joiner;
# never give one joiner its own list again.
ACTION_VERBS: tuple[str, ...] = (
    # navigation and gestures
    "tap", "click", "press", "long-press", "hold", "swipe", "scroll", "drag", "pinch",
    "rotate", "open", "close", "go", "navigate", "return", "launch", "relaunch", "reopen",
    "restart", "dismiss", "expand", "collapse",
    # choosing and entering
    "select", "choose", "pick", "type", "enter", "fill", "clear", "toggle", "turn", "flip",
    "enable", "disable", "rate", "keep", "find", "search",
    # changing data
    "add", "create", "edit", "rename", "save", "delete", "remove", "submit", "accept",
    "allow", "deny", "grant", "sign", "log", "record", "play", "stop", "wait", "repeat",
    "restore",
    # reading and asserting — synonyms of one another, so they travel together
    "read", "verify", "check", "confirm", "ensure", "observe", "assert", "validate",
    "make sure",
)
_VERB_ALT = "|".join(re.escape(v).replace(r"\ ", r"\s+") for v in ACTION_VERBS)
_VERB_AFTER = r"(?P<verb>" + _VERB_ALT + r")\b"
# "and <verb>" chains a second action onto the first. So does ", <verb>" — but only
# when the step itself opens with a verb ("Tap +, enter the name"): a step that opens
# with a clause ("With the editor open, turn the phone", "On the list, find X") has
# one action after its comma, not two.
_AND_RE = re.compile(r"\band\s+" + _VERB_AFTER, re.IGNORECASE)
_COMMA_RE = re.compile(r",\s+" + _VERB_AFTER, re.IGNORECASE)
_OPENS_WITH_VERB = re.compile(r"^\s*" + _VERB_AFTER, re.IGNORECASE)
# "then <verb>" likewise, and so does the elliptical "then "Label"" / "then Label": a
# second target with its verb left implicit ("Choose New note, then New under"). "Then"
# followed by anything else is ordering prose inside one anchor ("the gear icon then
# the search icon"), not a second step.
_THEN_VERB_RE = re.compile(r"\bthen\s+" + _VERB_AFTER, re.IGNORECASE)
# Case-SENSITIVE on purpose: a quote (masked to `"_"`) or a capitalized label.
_THEN_TARGET_RE = re.compile(r"\bthen\s+(?:\"|[A-Z])")


def _is_label(match: re.Match[str]) -> bool:
    """A capitalized word after "and"/"," is a UI label in a list ("the Name and Type
    columns"), not an imperative: sentence-case steps never capitalize a mid-step verb."""
    return match.group("verb")[:1].isupper()


def atomic_steps(case: Case, ctx: Context) -> list[Violation]:
    """One interaction per step, the launch step included.

    Exempting step 1 was rejected in QUA-2612: it would forgive "Open the app and sign
    in", a whole login chain behind one step, and it blinds the metric at exactly the
    step a guide edit is expected to move. "Open the app" alone is writable; the check
    that the home screen shows belongs in its own verify step."""
    out = []
    for s in case.steps:
        text = _mask_quotes(s.text)
        hit = None
        probes = (_AND_RE, _COMMA_RE) if _OPENS_WITH_VERB.match(text) else (_AND_RE,)
        for rx in probes:
            for m in rx.finditer(text):
                if not _is_label(m):
                    hit = m.group(0).strip(" ,")
                    break
            if hit:
                break
        if not hit:
            m = _THEN_VERB_RE.search(text) or _THEN_TARGET_RE.search(text)
            if m:
                hit = m.group(0).strip()
        if hit:
            out.append(Violation(f'compound step ("{hit} ..."): one interaction per step',
                                 s.index))
    return out


_LOGIN_RE = re.compile(
    r"\b(log\s*in|sign\s*in|password|passcode|pin\s+code|credentials?|authenticate)\b", re.IGNORECASE)


def credential_ref(case: Case, ctx: Context) -> list[Violation]:
    """A step that signs in names its credential: a `credential_id`, the serialized
    `## {uuid}` reference, or explicit quoted test data (a deliberate wrong-password
    case types a literal). A check ABOUT the signed-in state needs none."""
    out = []
    for s in case.steps:
        if is_check(s) or not _LOGIN_RE.search(s.text):
            continue
        if s.credential_id or _CRED_REF_RE.search(s.text) or quoted(s.text):
            continue
        out.append(Violation("sign-in step with no credential reference or quoted test data",
                             s.index))
    return out


_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghs)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret|password|passwd)\s*[:=]\s*\S{6,}", re.IGNORECASE),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
]


def no_plaintext_secrets(case: Case, ctx: Context) -> list[Violation]:
    out = []
    for label, text, idx in _fields(case):
        for rx in _SECRET_PATTERNS:
            if m := rx.search(text):
                out.append(Violation(f"{_at(label, idx)}secret-shaped text ({m.group(0)[:12]}...)", idx))
                break
    return out


_COORD_PATTERNS = [
    re.compile(r"\(\s*\d{1,4}\s*,\s*\d{1,4}\s*\)"),
    re.compile(r"\bat\s+\d{1,4}\s*,\s*\d{1,4}\b", re.IGNORECASE),
    re.compile(r"\b[xy]\s*[=:]\s*\d{1,4}\b", re.IGNORECASE),
    re.compile(r"\bcoordinates?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*(?:px|pixels?|dp)\b", re.IGNORECASE),
]


def no_coordinates(case: Case, ctx: Context) -> list[Violation]:
    """The runner finds targets by what they say, never where they are."""
    out = []
    for s in case.steps:
        for rx in _COORD_PATTERNS:
            if m := rx.search(s.text):
                out.append(Violation(f"position instead of a label ({m.group(0)!r})", s.index))
                break
    return out


_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DYNAMIC_PATTERNS = [
    (re.compile(r"\b\d+\s+(?:items?|results?|entries|rows|cards?|notifications?|messages?"
                r"|files?|photos?|notes?|tasks?|events?|contacts?|unread)\b", re.IGNORECASE),
     "a hard-coded count"),
    (re.compile(r"\b\d+\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b",
                re.IGNORECASE), "a relative timestamp"),
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?(?!\w)"), "a clock time"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b" + _MONTH + r"\s+\d{1,2}(?:,\s*|\s+)\d{4}\b"
                r"|\b\d{1,2}\s+" + _MONTH + r"\s+\d{4}\b"), "a calendar date"),
    (re.compile(r"\bv?\d+\.\d+\.\d+\b|\bversion\s+\d+(?:\.\d+)*\b", re.IGNORECASE), "a version number"),
    (re.compile(r"\b\d{1,3}(?:\.\d+)?\s*%"), "a percentage"),
]


def no_dynamic_values(case: Case, ctx: Context) -> list[Violation]:
    """What a check asserts must hold on every run: not a count of existing rows, a
    time, a date, a version or a percentage, all of which drift under the case. Only
    checks and the expected result are held to this; actions may type any literal."""
    targets = [(f"step {s.index}", s.text, s.index) for s in case.steps if is_check(s)]
    if case.expected_result:
        targets.append(("expected result", case.expected_result, None))
    out = []
    for label, text, idx in targets:
        for rx, what in _DYNAMIC_PATTERNS:
            if m := rx.search(text):
                out.append(Violation(f"{_at(label, idx)}asserts {what} ({m.group(0)!r})", idx))
                break
    return out


def _pins(quote: str, entry: str) -> bool:
    """Does a quoted string pin a known content string? Exact match always; containment
    on word boundaries for entries of four characters or more (short entries like "one"
    are too common to match inside a longer label); and a multi-word quote of twelve
    characters or more that is a fragment of a longer entry (a partial banner quote)."""
    q, e = quote.casefold().strip(), entry.casefold().strip()
    if not q or not e:
        return False
    if q == e:
        return True
    if len(e) >= 4 and re.search(r"(?<!\w)" + re.escape(e) + r"(?!\w)", q):
        return True
    return len(q) >= 12 and " " in q and q in e


def content_anchors(case: Case, ctx: Context) -> list[Violation]:
    """A quote that names fixture content (a seeded record, a user's data) anchors on
    something the app does not own and a real workspace will not have. Navigate by the
    app's own labels, or create the record in a setup step and then quote it — a value
    the case types itself is never on the fixture denylist."""
    if not ctx.content_strings:
        return []
    out = []
    for label, text, idx in _fields(case):
        for q in quoted(text):
            entry = next((e for e in ctx.content_strings if _pins(q, e)), None)
            if entry is not None:
                out.append(Violation(f"{_at(label, idx)}quoted {q!r} pins fixture content ({entry!r})",
                                     idx))
    return out


# Emoji ranges. Plain dingbats a close button may genuinely be labelled with (the
# multiplication x, check marks drawn as text) are deliberately left out.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-⛿✅❌❎❤⭐"
    "❗❕❓❔️\U0001F1E6-\U0001F1FF]")


def no_emoji(case: Case, ctx: Context) -> list[Violation]:
    """Decorative emoji in persisted text. An emoji INSIDE a quote is exempt: the quote
    reproduces an on-screen label, and some apps put emoji in their labels."""
    out = []
    for label, text, idx in _fields(case):
        if m := _EMOJI_RE.search(_QUOTE_RE.sub(" ", text)):
            out.append(Violation(f"{_at(label, idx)}emoji {m.group(0)!r}", idx))
    return out


_MARKDOWN_PATTERNS = [
    (re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S"), "a heading"),
    (re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"), "bold"),
    (re.compile(r"`[^`\n]+`"), "a code span"),
    (re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"), "a link"),
    (re.compile(r"(?m)^\s*[-*+]\s+\S"), "a bullet"),
]


def no_markdown(case: Case, ctx: Context) -> list[Violation]:
    """Persisted fields render as plain text. The `## {uuid}` credential reference is
    the product's own syntax, not a markdown heading."""
    out = []
    for label, text, idx in _fields(case):
        plain = _CRED_REF_RE.sub("", text)
        for rx, what in _MARKDOWN_PATTERNS:
            if rx.search(plain):
                out.append(Violation(f"{_at(label, idx)}markdown ({what})", idx))
                break
    return out


def name_length(case: Case, ctx: Context) -> list[Violation]:
    if not case.name:
        return [Violation("the case has no name")]
    if len(case.name) < MAX_NAME_LEN:
        return []
    return [Violation(f"name is {len(case.name)} characters (keep it under {MAX_NAME_LEN})")]


# (rule id, severity, rule), in report order.
RULES: tuple[tuple[str, str, Rule], ...] = (
    ("created-via-api", HARD, created_via_api),
    ("launch-first", HARD, launch_first),
    ("verify-present", HARD, verify_present),
    ("step-count", HARD, step_count),
    ("kind-tags", HARD, kind_tags),
    ("atomic-steps", HARD, atomic_steps),
    ("credential-ref", HARD, credential_ref),
    ("no-plaintext-secrets", HARD, no_plaintext_secrets),
    ("no-coordinates", HARD, no_coordinates),
    ("no-dynamic-values", HARD, no_dynamic_values),
    ("content-anchors", HARD, content_anchors),
    ("no-emoji", SOFT, no_emoji),
    ("no-markdown", SOFT, no_markdown),
    ("name-length", SOFT, name_length),
)
RULE_IDS = tuple(r[0] for r in RULES)


def lint_case(raw: Any, *, app_name: str | None = None,
              content_strings: Iterable[str] = ()) -> LintReport:
    """Lint one authored case in any accepted shape. Raises TypeError/ValueError on a
    malformed one."""
    case = normalize_case(raw)
    ctx = Context(app_name=app_name, content_strings=tuple(content_strings))
    return LintReport(case_name=case.name, case_id=case.case_id,
                      results=[RuleResult(rid, sev, fn(case, ctx)) for rid, sev, fn in RULES])


# ── the fixture denylist ───────────────────────────────────────────────────────

# Tables that hold what the app ships with rather than what the fixture seeded: Room /
# Android bookkeeping everywhere, and per app the stock rows a fresh install creates
# (AnkiDroid's built-in note types, their field and card-template names, its "Default"
# deck and config keys). Their strings are the app's own labels — quoting them is
# correct anchoring, so they must not reach the denylist.
_BOOKKEEPING_TABLES = {"android_metadata", "room_master_table"}
_STOCK_TABLES: dict[str, set[str]] = {
    "ankidroid": {"col", "config", "deck_config", "fields", "templates", "notetypes", "tags",
                  "graves", "revlog", "cards"},
}
_STOCK_VALUES: dict[str, set[str]] = {"ankidroid": {"Default"}}
# AnkiDroid packs a note's fields into one column, separated by U+001F.
_FIELD_SEPARATOR = "\x1f"
# A content string reads like words: it has a letter, no machine punctuation (JSON,
# URLs, SQL, enum_identifiers), is not an ALL-CAPS enum, not a camelCase key, and
# carries no long digit run (row ids, remote ids, file names made of ids).
_MACHINE_CHARS = re.compile(r"[\[\]{}<>=;/\\%_~|@#$^*]")
# A single token (no space) is a word, a name or a number with a unit — never a
# random-looking id such as a sync guid (`xV,+cEcxmd`).
_ONE_TOKEN = re.compile(r"^[\w'.&()-]+$")
_CAMEL = re.compile(r"^[a-z]+[A-Z]\w*$")
_LONG_DIGITS = re.compile(r"\d{5,}")


def _content_like(value: str) -> bool:
    v = value.strip()
    return (3 <= len(v) <= 60 and any(c.isalpha() for c in v) and "\n" not in v
            and not _MACHINE_CHARS.search(v) and not (v.isupper() and len(v) > 1)
            and not _CAMEL.match(v) and not _LONG_DIGITS.search(v)
            and (" " in v or bool(_ONE_TOKEN.match(v))))


def sqlite_content_strings(db: Path, *, skip_tables: Iterable[str] = (),
                           skip_values: Iterable[str] = ()) -> list[str]:
    """Every content-like text value in a SQLite fixture, read without touching the
    file (`immutable=1`: a fixture shipped beside stale -wal/-shm files stays as is)."""
    skip = set(skip_tables) | _BOOKKEEPING_TABLES
    drop = set(skip_values)
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    # Some apps declare custom collations; reading rows must not depend on them.
    con.create_collation("unicase", lambda a, b: (a.casefold() > b.casefold())
                         - (a.casefold() < b.casefold()))
    found: set[str] = set()
    try:
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type = 'table'")]
        for table in tables:
            if table.startswith("sqlite_") or table in skip:
                continue
            try:
                rows = con.execute(f'select * from "{table}"').fetchall()
            except sqlite3.DatabaseError:
                continue
            for row in rows:
                for value in row:
                    if not isinstance(value, str):
                        continue
                    for part in value.split(_FIELD_SEPARATOR):
                        part = part.strip()
                        if part not in drop and _content_like(part):
                            found.add(part)
    finally:
        con.close()
    return sorted(found)


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def fixture_content_strings(app_id: str) -> list[str]:
    """The content-anchor denylist for an app: the content-like text of every SQLite
    database its benchmark spec pushes in `device_setup`. An app seeded some other way
    (or not at all) gets an empty list, and the rule passes for it."""
    from .. import corpus
    spec = corpus.spec_path(app_id)
    if not spec.exists():
        return []
    found: set[str] = set()
    for src in corpus.push_sources(spec):
        path = corpus.asset_path(src)
        if _is_sqlite(path):
            found.update(sqlite_content_strings(
                path, skip_tables=_STOCK_TABLES.get(app_id, ()),
                skip_values=_STOCK_VALUES.get(app_id, ())))
    return sorted(found)


def app_display_name(app_id: str) -> str | None:
    from .. import corpus
    spec = corpus.spec_path(app_id)
    if not spec.exists():
        return None
    name = ((yaml.safe_load(spec.read_text()) or {}).get("app") or {}).get("name")
    return str(name) if name else None


# ── the journey reference baseline ─────────────────────────────────────────────

def lint_journey(app_ids: Iterable[str] | None = None) -> dict[str, list[LintReport]]:
    """Lint every packaged (or held-out) journey reference case: {app id: reports}."""
    from .. import corpus, journey
    apps = list(app_ids) if app_ids else sorted(set(corpus.public_apps())
                                                | set(corpus.heldout_apps()))
    out: dict[str, list[LintReport]] = {}
    for app in apps:
        doc = journey.load_cases(app)
        if doc is None:
            raise ValueError(f"no journey test cases for app {app!r}")
        name, deny = app_display_name(app), fixture_content_strings(app)
        out[app] = [lint_case(c, app_name=name, content_strings=deny)
                    for c in doc.get("test_cases", [])]
    return out


def baseline(reports: dict[str, list[LintReport]]) -> dict[str, Any]:
    """Per-rule failure counts over a set of reports, and the lint-clean count."""
    flat = [r for rs in reports.values() for r in rs]
    per_rule = {rid: sum(1 for r in flat for res in r.results
                         if res.rule == rid and not res.passed) for rid in RULE_IDS}
    return {"cases": len(flat), "lint_clean": sum(r.ok for r in flat),
            "failed_by_rule": per_rule,
            "severity": {rid: sev for rid, sev, _ in RULES}}


# ── CLI ────────────────────────────────────────────────────────────────────────

def _print_report(report: LintReport) -> None:
    title = report.case_name or "<unnamed case>"
    print(f"{'OK  ' if report.ok else 'FAIL'}  {title}"
          + (f"  [{report.case_id}]" if report.case_id else ""))
    for r in report.results:
        if r.passed:
            continue
        print(f"      {r.rule} ({r.severity})")
        for v in r.violations:
            where = f"step {v.step}: " if v.step else ""
            print(f"        - {where}{v.message}")


def _print_baseline(summary: dict[str, Any]) -> None:
    print(f"\nlint-clean {summary['lint_clean']}/{summary['cases']}")
    for rid, n in summary["failed_by_rule"].items():
        print(f"  {rid:<22} {summary['severity'][rid]:<5} failed {n}/{summary['cases']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m qualgentbench.create.lint",
        description="Static lint for an authored test case (CreateBench stage B).")
    ap.add_argument("case", nargs="?",
                    help="the case as JSON or YAML: a create body, a stored case, or '-' "
                         "for stdin")
    ap.add_argument("--app", help="app id: its display name counts as launching it and its "
                                  "fixture data supplies the content-anchor denylist")
    ap.add_argument("--deny", action="append", default=[], metavar="TEXT",
                    help="an extra content string to deny (repeatable)")
    ap.add_argument("--journey", nargs="*", metavar="APP",
                    help="lint the journey reference cases (all apps when none named) "
                         "and print the per-rule baseline")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if args.journey is not None:
        try:
            reports = lint_journey(args.journey)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        summary = baseline(reports)
        if args.json:
            print(json.dumps({"baseline": summary,
                              "apps": {a: [r.to_dict() for r in rs]
                                       for a, rs in reports.items()}}, indent=2))
        else:
            for app, rs in reports.items():
                print(f"== {app}")
                for r in rs:
                    _print_report(r)
            _print_baseline(summary)
        return 0 if summary["lint_clean"] == summary["cases"] else 1

    if not args.case:
        ap.error("give a case file (or '-'), or --journey")
    try:
        text = sys.stdin.read() if args.case == "-" else Path(args.case).read_text()
        raw = yaml.safe_load(text)          # YAML is a superset of JSON
    except (OSError, yaml.YAMLError) as e:
        print(f"error: cannot read the case: {e}", file=sys.stderr)
        return 2
    deny = list(args.deny)
    name = None
    if args.app:
        deny += fixture_content_strings(args.app)
        name = app_display_name(args.app)
    try:
        report = lint_case(raw, app_name=name, content_strings=deny)
    except (TypeError, ValueError) as e:
        print(f"error: malformed case: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
