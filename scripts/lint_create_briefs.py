#!/usr/bin/env python3
"""Neutrality gate for CreateBench v2 creation briefs (QUA-2853, epic QUA-2850).

Every journey case carries a `brief:` block — `{title, intended_behavior}` — that a
CREATION agent reads before it explores the clean build and authors a test case. The
brief says what the feature should do. It must never say how to test it, and never that
anything could be wrong: a brief that leaks the defect or the verification procedure turns
the create score into a measurement of the brief instead of the creation surface.

The journey agent never sees `brief:` (`journey.brief` reads `name`, `steps` and
`expected_outcome`); the creation agent sees ONLY `brief:`. So the brief is checked
against everything the case knows that the author must not be handed:

  shape       a public case with no `brief:`, or one that is not a mapping of exactly
              `title` + `intended_behavior` (non-empty strings) (error). Missing on a
              held-out case is a warning: held-out briefs are out of scope for QUA-2853.
  defect      a defect id of the app, hyphenated or spaced (error); a defect MARKER of
              any defect of the app on token boundaries (error); a multi-word symptom
              phrase of one of the case's own bugs (error); a single-word symptom of one
              of the case's own bugs (warning — a persistence brief has to say "delete"
              or "favorite", exactly as `lint_journey_cases.rule_brief_symptom` allows).
  procedure   a verification-procedure hint: re-open/re-visit/re-launch/restart, later,
              subsequent, any/every/next/another visit, come back/return/go back, still,
              persist/survive/remain, rotation/orientation, force-stop/kill/background,
              UI gestures (tap/click/press/swipe/scroll), verify/check/confirm/ensure/test,
              sequencing (then/finally/afterwards/step N), a numbered or bulleted list, or
              a sentence that opens with an imperative verb (error). QUA-2614's lesson:
              August's gate missed "later visit" on the one load-bearing persistence brief.
  failure     failure or edge-case language: bug, defect, crash, freeze, hang, error,
              fail, wrong, incorrect, lost, missing, never, instead of, correctly,
              properly, without, empty, edge case ... (error).
  value       a quote mark of any kind (a neutral brief quotes no UI string), any digit
              (concrete values are fixture data or procedure), or a check anchor /
              expected value on token boundaries: the route's `type:` values, tap anchors
              that carry a digit, `row:` scopes, `evidence:` and `present:`/`absent:`
              strings, the oracle's `crash:`/`anr:`/`stuck:` gate strings, the `db:`
              query's quoted literals, and every quoted string of `steps` and
              `expected_outcome` (error).
  copy        five consecutive words (three or more of them content words) shared with
              the case's `steps` or `expected_outcome` — the brief paraphrases the
              procedure or the outcome (error).
  noun        a capitalised word from `expected_outcome` that is not sentence-initial and
              not part of a route tap target (usually a fixture entity: Aspirin, Spanish)
              (warning — UI labels such as "Medicine tab" are legitimate vocabulary).
  length      `intended_behavior` under 12 or over 90 words (warning).

`--subset` (default on) also gates the positive-control subsets recorded in
`data/create/positive-control.yaml`, `positive-control-v2.yaml` (QUA-2864) and
`positive-control-v3.yaml` (QUA-2870; a subset
naming a harmful `rule:` must keep its `assert` entries inside that rule's
`drop_classes`), each: exactly `size` entries, unique cases, each a public
case with a brief, `target` equal to the case's `bugs:`, `app` and `class` equal to the
corpus, every target defect with a `QgbFlags.fired("<id>")` canary in
`data/benchmarks/<app>.yaml`, every app and class of the spread pool represented, and no
app above `max_per_app` (error) — then prints the spread. The spread pool is the
canary-covered FUNCTIONAL defects (display defects can never be entries) as frozen in the
subset's `spread_pool:` at pre-registration; classes that live coverage adds later are
printed as `info`, never errors (QUA-2860), and a class in `spread_pool.excluded` (with
its reason) is left out on purpose. Every entry also records its target's DETECTION
mechanism, `detection: walk|assert` (QUA-2862): missing, unknown, or different from
`create/detection.py`'s derivation (the target's `class:` + its journey truth row, never a
simulation or a live result) is an error, as is a subset whose labels do not add up to its
`detection_mix:`.

Exit 1 on any error. `--app a,b` narrows the files. Rules are pure functions over a
loaded document so `tests/test_lint_create_briefs.py` can drive each one in isolation.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from qualgentbench import journey

BRIEF_KEYS = ("title", "intended_behavior")
_MIN_WORDS, _MAX_WORDS = 12, 90
_MIN_VALUE_CHARS = 3
_SUBSET_PATH = Path(journey._DATA) / "create" / "positive-control.yaml"
#: QUA-2864's re-run subset (`harmful-rule-positive-control-mechanism/v2`). Every subset
#: file is gated by the same rules; one that names a harmful `rule:` must also keep its
#: DROP (`assert`) entries inside that rule's `drop_classes` (`_rule_findings`).
_SUBSET_V2_PATH = Path(journey._DATA) / "create" / "positive-control-v2.yaml"
#: QUA-2870's 8 DROP + 4 FLAT subset, registered by
#: `harmful-rule-positive-control-mechanism/v3`; gated by the same rules.
_SUBSET_V3_PATH = Path(journey._DATA) / "create" / "positive-control-v3.yaml"
SUBSET_PATHS = (_SUBSET_PATH, _SUBSET_V2_PATH, _SUBSET_V3_PATH)

# ── vocabularies ───────────────────────────────────────────────────────────────
# Each entry: (compiled pattern, label), matched case-insensitively against the brief with
# whitespace folded. Prefix patterns are deliberately loose ("check" also catches
# "checkbox"): a brief can always be reworded, and a false alarm here costs one edit while
# a missed hint costs a benchmark.

def _p(rx: str) -> re.Pattern[str]:
    return re.compile(rx, re.IGNORECASE)


PROCEDURE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (_p(r"\bre-?\s?(open|visit|enter|launch|start|load|check|try|run|boot)\w*"), "re-entry"),
    (_p(r"\brestart\w*|\breboot\w*"), "restart"),
    (_p(r"\blater\b"), "later"),
    (_p(r"\bsubsequent\w*"), "subsequent"),
    (_p(r"\b(any|every|each|next|another|second|future|following|new)\s+"
        r"(visit|time|session|launch|opening|run|start)s?\b"), "any/next visit"),
    (_p(r"\bagain\b"), "again"),
    (_p(r"\bcom(e|es|ing)\s+back\b|\bgo(es|ing)?\s+back\b|\bback\s+to\b|\breturn\w*"),
     "come back / return"),
    (_p(r"\bnavigat\w*\s+away\b|\bleav(e|es|ing)\s+(and|the\s+app)\b|"
        r"\bafter\s+(closing|leaving|exiting|restarting|relaunching|reopening)\b"), "leave and return"),
    (_p(r"\bstill\b"), "still"),
    (_p(r"\bpersist\w*|\bsurviv\w*|\bremain\w*"), "persistence hint"),
    (_p(r"\brotat\w*|\borientation\w*|\blandscape\b|\bportrait\b|\bsideways\b|"
        r"\bconfiguration\s+change\w*"), "rotation"),
    (_p(r"\bforce[-\s]?(stop|clos|quit)\w*|\bkill\w*|\bbackground\w*|\bminimi[sz]\w*"),
     "process lifecycle"),
    (_p(r"\b(tap|taps|tapped|tapping|click\w*|press|presses|pressed|pressing|"
        r"long-press\w*|swip\w*|scroll\w*|drag\w*)\b"), "UI gesture"),
    (_p(r"\b(verif\w*|check\w*|confirm\w*|ensur\w*|assert\w*|expect\w*|test\w*|"
        r"observ\w*|inspect\w*|validat\w*)\b|\bmake\s+sure\b"), "verification"),
    (_p(r"\b(then|finally|afterwards?)\b|\bafter\s+that\b|\bstep\s*\d|\bsteps?\b"), "sequencing"),
]

FAILURE_LANGUAGE: list[tuple[re.Pattern[str], str]] = [
    (_p(r"\b(bug|bugs|buggy|defect\w*|broken|break\w*|crash\w*|freez\w*|froze\w*|hang|hangs|"
        r"hung|stuck|error\w*|fail\w*|wrong\w*|incorrect\w*|regress\w*|glitch\w*|problem\w*|"
        r"issue\w*|flaw\w*|fault\w*|unexpected\w*|lost|lose|loses|losing|drop|drops|dropped|"
        r"missing|corrupt\w*|unresponsive|exception\w*|never|correct|correctly|properly|"
        r"reliabl\w*)\b"), "failure word"),
    (_p(r"\b(should|must|does|do|did|will|would|can)\s*n[o']t\b|\b(shouldn|mustn|doesn|"
        r"didn|won|wouldn|can)'?t\b|\bcannot\b"), "negated expectation"),
    (_p(r"\binstead\s+of\b|\bas\s+expected\b|\bnot\s+respond\w*|\bno\s+longer\b"),
     "failure phrasing"),
    (_p(r"\bwithout\b|\bempty\b|\bzero\b|\bedge\s+case\w*|\bcorner\s+case\w*|\bboundar\w*|"
        r"\bdeepest\b|\blimit\w*"), "edge-case hint"),
]

# A sentence that opens with one of these is an instruction, not a description.
_IMPERATIVE_WORDS = """
open tap click press go navigate enter type select choose add create delete edit change
answer switch swipe scroll read look find return launch start save set use turn rotate
verify check confirm ensure make try long-press search expand mark rate study close
leave wait note record observe inspect
"""
IMPERATIVES = frozenset(_IMPERATIVE_WORDS.split())

_QUOTE_CHARS = "\"“”‘’'`«»"
# SQL vocabulary inside a db query's quoted literals — not fixture values.
_SQL_NOISE = {"now", "localtime", "unixepoch", "utc", "start of day"}


class Finding:
    """One lint result (plain class, same reason as lint_journey_cases.Finding: the
    tests import this script through importlib without registering it)."""

    def __init__(self, level: str, rule: str, case: str, detail: str) -> None:
        self.level = level      # "error" | "warning" | "info" (subset spread notes only)
        self.rule = rule        # shape | defect | procedure | failure | value | copy | noun
                                # | length | subset
        self.case = case
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.level:7s} {self.rule:9s} {self.case}: {self.detail}"

    def __repr__(self) -> str:
        return f"Finding({self.level!r}, {self.rule!r}, {self.case!r}, {self.detail!r})"


# ── helpers ────────────────────────────────────────────────────────────────────

def _fold(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _norm(s: object) -> str:
    return _fold(s).strip("\"'").lower()


def brief_of(case: dict) -> dict | None:
    b = case.get("brief")
    return b if isinstance(b, dict) else None


def brief_fields(case: dict) -> list[tuple[str, str]]:
    """(field, text) for each brief field present as a string."""
    b = brief_of(case) or {}
    return [(k, _fold(b[k])) for k in BRIEF_KEYS if isinstance(b.get(k), str)]


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", s.lower())


def _quoted(text: str) -> list[str]:
    return [a or b for a, b in re.findall(r'"([^"]+)"|“([^”]+)”', text or "")]


def case_values(case: dict) -> list[tuple[str, str]]:
    """(source, value) for every check anchor and expected value the author must not be
    handed. Values under `_MIN_VALUE_CHARS` after normalisation are dropped (a bare `A`
    or `1` is caught, if at all, by the digit rule)."""
    out: list[tuple[str, str]] = []
    check = case.get("check") if isinstance(case.get("check"), dict) else {}
    for item in check.get("steps") or []:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            if v is None or isinstance(v, bool):
                continue
            v = str(v)
            # `type:`/`row:` are fixture values; a tap anchor is one only when it
            # carries a digit ("0 reminders") — otherwise it is a UI label.
            if k in ("type", "row") or (k in ("tap", "long_press") and re.search(r"\d", v)):
                out.append((f"route {k}", v))
    expect = check.get("expect") if isinstance(check.get("expect"), dict) else {}
    if journey._oracle(case)["mode"] in ("present", "absent"):
        for k in ("present", "absent"):
            if isinstance(expect.get(k), str):
                out.append((f"expect.{k}", expect[k]))
    for k in ("crash", "anr", "stuck"):
        if isinstance(expect.get(k), str):
            out.append((f"expect.{k}", expect[k]))
    if "db" in expect:
        for lit in re.findall(r"'([^']*)'", str(expect.get("query") or "")):
            low = lit.strip().lower()
            if low in _SQL_NOISE or low[:1] in ("%", "+", "-"):
                continue
            out.append(("db literal", lit))
    if "content" in expect:
        for k in ("contains", "where", "equals"):
            if isinstance(expect.get(k), str):
                out.append((f"expect.{k}", expect[k].split("=", 1)[-1]))
    for e in case.get("evidence") or []:
        out.append(("evidence", str(e)))
    for q in _quoted(str(case.get("expected_outcome") or "")):
        out.append(("expected_outcome quote", q))
    for i, s in enumerate(case.get("steps") or [], 1):
        for q in _quoted(str(s)):
            out.append((f"step {i} quote", q))
    seen: set[str] = set()
    kept: list[tuple[str, str]] = []
    for src, v in out:
        n = _norm(v)
        if len(n) < _MIN_VALUE_CHARS or n in seen:
            continue
        seen.add(n)
        kept.append((src, v))
    return kept


def _case_defects(case: dict, defects: dict[str, dict]) -> list[tuple[str, str, list[str]]]:
    out = []
    for b in journey.case_bugs(case):
        d = defects.get(b["id"]) or {}
        out.append((b["id"], b["marker"] or d.get("marker") or "", list(d.get("symptoms") or [])))
    return out


def _all_markers(doc: dict, defects: dict[str, dict]) -> list[tuple[str, str]]:
    """(defect id, marker) for every defect of the app, plus every per-case override."""
    out = [(did, d.get("marker") or "") for did, d in defects.items()]
    for case in doc.get("test_cases") or []:
        out += [(b["id"], b["marker"]) for b in journey.case_bugs(case) if b["marker"]]
    return [(i, m) for i, m in out if m]


# ── the rules ──────────────────────────────────────────────────────────────────

def rule_shape(case: dict, heldout: bool = False) -> list[Finding]:
    cid = str(case.get("id"))
    raw = case.get("brief")
    if raw is None:
        if heldout:
            return [Finding("warning", "shape", cid, "no `brief:` — held-out briefs are out of "
                            "scope for QUA-2853")]
        return [Finding("error", "shape", cid, "no `brief:` — every public journey case "
                        "carries a neutral creation brief {title, intended_behavior}")]
    if not isinstance(raw, dict):
        return [Finding("error", "shape", cid, f"`brief:` must be a mapping, got {raw!r}")]
    found: list[Finding] = []
    extra = sorted(set(raw) - set(BRIEF_KEYS))
    if extra:
        found.append(Finding("error", "shape", cid, f"`brief:` has unknown key(s) {extra} — "
                             f"only {list(BRIEF_KEYS)}"))
    for k in BRIEF_KEYS:
        if not isinstance(raw.get(k), str) or not raw[k].strip():
            found.append(Finding("error", "shape", cid, f"`brief.{k}` must be a non-empty string"))
    return found


def rule_defect(case: dict, doc: dict, defects: dict[str, dict]) -> list[Finding]:
    cid = str(case.get("id"))
    found: list[Finding] = []
    for field, text in brief_fields(case):
        low = _norm(text)
        for did in defects:
            for form in {did.lower(), did.lower().replace("-", " ")}:
                if journey._word(form, low):
                    found.append(Finding("error", "defect", cid,
                                         f"brief.{field} names defect id {did!r}"))
        for did, marker in _all_markers(doc, defects):
            m = journey._evidence(marker)
            if m and journey._word(m, low):
                found.append(Finding("error", "defect", cid,
                                     f"brief.{field} states {did}'s marker {marker!r}"))
        for did, _, symptoms in _case_defects(case, defects):
            for sym in symptoms:
                s = _norm(sym)
                if s and journey._word(s, low):
                    level = "error" if " " in s else "warning"
                    found.append(Finding(level, "defect", cid,
                                         f"brief.{field} carries {did}'s symptom {sym!r}"))
    return found


def _vocab_hits(case: dict, vocab: list[tuple[re.Pattern[str], str]], rule: str) -> list[Finding]:
    cid = str(case.get("id"))
    found: list[Finding] = []
    for field, text in brief_fields(case):
        for rx, label in vocab:
            for m in rx.finditer(text):
                found.append(Finding("error", rule, cid,
                                     f"brief.{field} has {label} {m.group(0)!r}"))
    return found


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;:])\s+|\n+", text) if s.strip()]


def rule_procedure(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    found = _vocab_hits(case, PROCEDURE_HINTS, "procedure")
    raw = brief_of(case) or {}
    for field in BRIEF_KEYS:
        text = raw.get(field)
        if not isinstance(text, str):
            continue
        # A list survives YAML folding only as leading markers inside the text.
        if re.search(r"(^|\n|\s)(\d+[.)]|[-*•])\s+\S", text.strip()):
            found.append(Finding("error", "procedure", cid,
                                 f"brief.{field} looks like a step list"))
        if field == "intended_behavior":
            for s in _sentences(_fold(text)):
                w = _words(s)
                if w and w[0] in IMPERATIVES:
                    found.append(Finding("error", "procedure", cid,
                                         f"brief.{field} has an imperative sentence {s!r}"))
    return found


def rule_failure(case: dict) -> list[Finding]:
    return _vocab_hits(case, FAILURE_LANGUAGE, "failure")


def rule_value(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    found: list[Finding] = []
    values = case_values(case)
    for field, text in brief_fields(case):
        if any(c in text for c in _QUOTE_CHARS if c != "'") or re.search(r"(^|\s)'|'(\s|$|[.,;])", text):
            found.append(Finding("error", "value", cid,
                                 f"brief.{field} quotes a string — a neutral brief quotes no UI text"))
        for d in re.findall(r"\d+(?:[.:,]\d+)*", text):
            found.append(Finding("error", "value", cid, f"brief.{field} states a number {d!r}"))
        low = _norm(text)
        for src, v in values:
            if journey._word(_norm(v), low):
                found.append(Finding("error", "value", cid,
                                     f"brief.{field} carries the case's {src} {v!r}"))
    return found


# Function words a shared run may be made of without being a copy: "in the event list"
# is where a feature lives, not a paraphrase of the procedure.
_FUNCTION_WORD_TEXT = """
a an the of in on at to from for with by and or its it is are be as that this which who
their them they his her its one each every into onto under over than so
"""
_FUNCTION_WORDS = frozenset(_FUNCTION_WORD_TEXT.split())
_COPY_N = 5
_COPY_MIN_CONTENT = 3


def _shingles(text: str, n: int = _COPY_N) -> set[tuple[str, ...]]:
    """Every run of `n` words carrying at least `_COPY_MIN_CONTENT` content words."""
    w = _words(text)
    runs = (tuple(w[i:i + n]) for i in range(len(w) - n + 1))
    return {r for r in runs if sum(t not in _FUNCTION_WORDS for t in r) >= _COPY_MIN_CONTENT}


def rule_copy(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    found: list[Finding] = []
    sources = [("expected_outcome", str(case.get("expected_outcome") or ""))]
    sources += [(f"step {i}", str(s)) for i, s in enumerate(case.get("steps") or [], 1)]
    for field, text in brief_fields(case):
        mine = _shingles(text)
        for where, src in sources:
            shared = mine & _shingles(src)
            if shared:
                found.append(Finding("error", "copy", cid,
                                     f"brief.{field} copies {where}: "
                                     f"{' / '.join(' '.join(s) for s in sorted(shared))!r}"))
    return found


def rule_noun(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    outcome = _fold(case.get("expected_outcome"))
    nouns: set[str] = set()
    for s in _sentences(outcome):
        toks = re.findall(r"[A-Za-z][A-Za-z'-]*", s)
        nouns |= {t for t in toks[1:] if t[0].isupper() and len(t) >= 3}
    # A word that is part of a tap target the route presses is a UI label ("Card browser",
    # "Medicine", "Groups"), which a feature brief may name.
    check = case.get("check") if isinstance(case.get("check"), dict) else {}
    labels = " ".join(str(v).lower() for item in check.get("steps") or [] if isinstance(item, dict)
                      for k, v in item.items() if k in ("tap", "long_press"))
    nouns = {n for n in nouns if not journey._word(n.lower(), labels)}
    found: list[Finding] = []
    for field, text in brief_fields(case):
        for n in sorted(nouns):
            if journey._word(n.lower(), _norm(text)):
                found.append(Finding("warning", "noun", cid,
                                     f"brief.{field} names {n!r} from expected_outcome — a UI "
                                     f"label is fine, a fixture entity is not"))
    return found


def rule_length(case: dict) -> list[Finding]:
    cid = str(case.get("id"))
    b = brief_of(case) or {}
    ib = b.get("intended_behavior")
    if not isinstance(ib, str):
        return []
    n = len(_words(ib))
    if n < _MIN_WORDS or n > _MAX_WORDS:
        return [Finding("warning", "length", cid, f"intended_behavior is {n} words "
                        f"(expected {_MIN_WORDS}-{_MAX_WORDS})")]
    return []


def lint_case(case: dict, doc: dict, defects: dict[str, dict], heldout: bool = False) -> list[Finding]:
    found = rule_shape(case, heldout=heldout)
    if brief_of(case) is None:
        return found
    for rule in (rule_procedure, rule_failure, rule_value, rule_copy, rule_noun, rule_length):
        found += rule(case)
    found += rule_defect(case, doc, defects)
    return found


def lint_doc(doc: dict, heldout: bool = False) -> list[Finding]:
    defects = journey.load_defects(doc)
    out: list[Finding] = []
    for case in doc.get("test_cases") or []:
        out += lint_case(case, doc, defects, heldout=heldout)
    return out


def lint_corpus(app_ids: list[str] | None = None) -> dict[str, list[Finding]]:
    from qualgentbench import corpus
    heldout = set(corpus.heldout_apps())
    out: dict[str, list[Finding]] = {}
    for app_id in sorted(set(corpus.public_apps()) | heldout):
        if app_ids and app_id not in app_ids:
            continue
        doc = journey.load_cases(app_id)
        if not doc:
            out[app_id] = [Finding("error", "shape", app_id, "file did not load")]
            continue
        out[app_id] = lint_doc(doc, heldout=app_id in heldout)
    return out


# ── the positive-control subset ────────────────────────────────────────────────

def canary_ids(spec_text: str) -> set[str]:
    """Defect ids with a `QgbFlags.fired("<id>")` call in a benchmark spec's patches."""
    return set(re.findall(r"""fired\(\s*["']([^"']+)["']\s*\)""", spec_text))


def spec_canaries(app_id: str) -> set[str]:
    from qualgentbench import corpus
    p = corpus.spec_path(app_id)
    return canary_ids(Path(p).read_text()) if Path(p).exists() else set()


def lint_subset(subset: dict, docs: dict[str, dict], canaries: dict[str, set[str]],
                truths: dict[str, dict] | None = None) -> list[Finding]:
    """Pure over its inputs: `docs` {app: loaded test-case doc}, `canaries` {app: ids},
    `truths` {app: journey truth doc} (default: `journey.load_truth` per app — the rows
    the detection label is derived from)."""
    from qualgentbench.create import detection as _det
    if truths is None:
        truths = {a: journey.load_truth(a) for a in docs}
    found: list[Finding] = []
    entries = subset.get("briefs") or []
    size = subset.get("size")
    if not isinstance(size, int) or len(entries) != size:
        found.append(Finding("error", "subset", "positive-control",
                             f"{len(entries)} entries, `size:` says {size!r}"))
    cases: dict[str, tuple[str, dict, dict]] = {}
    for app, doc in docs.items():
        defects = {str(d["id"]): d for d in doc.get("defects") or []}
        for c in doc.get("test_cases") or []:
            cases[str(c.get("id"))] = (app, c, defects)
    seen: set[str] = set()
    for e in entries:
        cid = str((e or {}).get("case"))
        if cid in seen:
            found.append(Finding("error", "subset", cid, "listed twice"))
        seen.add(cid)
        if cid not in cases:
            found.append(Finding("error", "subset", cid, "not a public journey case"))
            continue
        app, case, defects = cases[cid]
        if brief_of(case) is None:
            found.append(Finding("error", "subset", cid, "case has no `brief:`"))
        if e.get("app") != app:
            found.append(Finding("error", "subset", cid, f"app {e.get('app')!r} != {app!r}"))
        bugs = [b["id"] for b in journey.case_bugs(case)]
        target = e.get("target")
        target = [target] if isinstance(target, str) else list(target or [])
        if sorted(target) != sorted(bugs) or not bugs:
            found.append(Finding("error", "subset", cid,
                                 f"target {target} != the case's bugs {bugs}"))
        for t in target:
            if t not in canaries.get(app, set()):
                found.append(Finding("error", "subset", cid,
                                     f"target {t} has no fired() canary in benchmarks/{app}.yaml"))
        classes = sorted({str((defects.get(t) or {}).get("class")) for t in target})
        if e.get("class") not in classes:
            found.append(Finding("error", "subset", cid,
                                 f"class {e.get('class')!r} != corpus {classes}"))
        if not str(e.get("why") or "").strip():
            found.append(Finding("error", "subset", cid, "no `why:` rationale"))
        if e.get("reach") not in ("direct", "conditional"):
            found.append(Finding("error", "subset", cid,
                                 f"reach {e.get('reach')!r} is not direct|conditional"))
        found += _detection_findings(e, cid, case, docs[app], (truths.get(app) or {}).get(cid),
                                     _det)
    found += _mix_findings(subset)
    found += _spread_findings(subset, docs, canaries)
    found += _rule_findings(subset)
    return found


def _rule_findings(subset: dict) -> list[Finding]:
    """A subset registered for a harmful rule (`rule:`, QUA-2864) keeps every `assert`
    (DROP) entry inside the rule's `drop_classes`: the defect classes a case following
    the rule provably cannot catch (`create/uptake.py`)."""
    rule_id = subset.get("rule")
    if rule_id is None:
        return []
    from qualgentbench.create import uptake
    rule = uptake.RULES.get(str(rule_id))
    if rule is None:
        return [Finding("error", "subset", "positive-control",
                        f"rule {rule_id!r} is not one of {sorted(uptake.RULES)}")]
    return [Finding("error", "subset", str(e.get("case")),
                    f"DROP entry of class {e.get('class')!r}: {rule.id} cannot provably "
                    f"remove its power (drop_classes {sorted(rule.drop_classes)})")
            for e in subset.get("briefs") or []
            if e.get("detection") == "assert" and e.get("class") not in rule.drop_classes]


def _detection_findings(e: dict, cid: str, case: dict, doc: dict, row: dict | None,
                        _det) -> list[Finding]:
    """The entry's `detection:` exists, is a known label, and equals the derivation."""
    got = e.get("detection")
    if got is None:
        return [Finding("error", "subset", cid, "no `detection:` label (walk|assert, derived "
                        "by create/detection.py from the target's class + journey truth)")]
    if got not in _det.LABELS:
        return [Finding("error", "subset", cid,
                        f"detection {got!r} is not {'|'.join(_det.LABELS)}")]
    d = _det.derive(case, doc, row)
    if d.label is None:
        return [Finding("error", "subset", cid, f"detection cannot be derived: {d.why}")]
    if d.label != got:
        return [Finding("error", "subset", cid, f"detection {got!r} != derived {d.label!r} "
                        f"({d.why})")]
    return []


def _mix_findings(subset: dict) -> list[Finding]:
    """The labels add up to the registered `detection_mix:` (when the subset has one)."""
    mix = subset.get("detection_mix")
    if mix is None:
        return []
    if not isinstance(mix, dict):
        return [Finding("error", "subset", "positive-control",
                        f"`detection_mix:` must be a mapping, got {mix!r}")]
    have = subset_detection(subset)
    want = {str(k): int(v) for k, v in mix.items()}
    if have != dict(sorted(want.items())):
        return [Finding("error", "subset", "positive-control",
                        f"detection mix {have} != registered detection_mix {want}")]
    return []


def subset_detection(subset: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in subset.get("briefs") or []:
        k = str(e.get("detection"))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def spread_pool(docs: dict[str, dict], canaries: dict[str, set[str]]) -> tuple[set[str], set[str]]:
    """The apps and defect classes a positive-control entry could LEGALLY cover today:
    a case's FUNCTIONAL bugs (rule 2 — an entry targets a FAIL-expected case's functional
    defect) that carry a `fired()` canary. A display-only defect (content-format, layout,
    ...) can never be an entry, so requiring it in the spread was a lint bug that only
    surfaced once QUA-2860 gave those defects canaries."""
    apps: set[str] = set()
    classes: set[str] = set()
    for app, doc in docs.items():
        defects = {str(d["id"]): d for d in doc.get("defects") or []}
        for c in doc.get("test_cases") or []:
            for b in journey.case_bugs(c):
                d = defects.get(b["id"]) or {}
                if str(d.get("kind") or "functional").lower() != "functional":
                    continue
                if b["id"] in canaries.get(app, set()):
                    apps.add(app)
                    classes.add(str(d.get("class") or "?"))
    return apps, classes


def _spread_findings(subset: dict, docs: dict[str, dict], canaries: dict[str, set[str]]) -> list[Finding]:
    """Every app and every defect class of the spread pool is represented, and no app
    holds more than `max_per_app` entries.

    The pool is the subset's own `spread_pool:` when it records one — the canary-covered
    pool FROZEN at pre-registration, so a canary added later (QUA-2860's backfill made
    persistence canary-covered) never retroactively demands an entry in a subset another
    ticket has already registered. What live coverage adds beyond the frozen pool is an
    INFO line, the owner's choice to act on, never an error. Without `spread_pool:` the
    live pool (`spread_pool()`) is the requirement, as before."""
    found: list[Finding] = []
    live_apps, live_classes = spread_pool(docs, canaries)
    frozen = subset.get("spread_pool")
    if isinstance(frozen, dict):
        pool_apps = {str(a) for a in frozen.get("apps") or []}
        pool_classes = {str(k) for k in frozen.get("classes") or []}
        excluded = frozen.get("excluded") or {}
        if not isinstance(excluded, dict):
            found.append(Finding("error", "subset", "positive-control",
                                 "`spread_pool.excluded` must map class -> reason"))
            excluded = {}
        for k, why in sorted(excluded.items()):
            if str(k) in pool_classes:
                found.append(Finding("error", "subset", "positive-control",
                                     f"class {k} is both in spread_pool.classes and excluded"))
            elif not str(why or "").strip():
                found.append(Finding("error", "subset", "positive-control",
                                     f"class {k} is excluded from the spread with no reason"))
            else:
                found.append(Finding("info", "subset", "positive-control",
                                     f"class {k} left out on purpose: {_fold(why)}"))
        live_classes = live_classes - {str(k) for k in excluded}
        for a in sorted(live_apps - pool_apps):
            found.append(Finding("info", "subset", "positive-control",
                                 f"app {a} is now canary-covered but not in the pre-registered "
                                 f"spread_pool"))
        for k in sorted(live_classes - pool_classes):
            found.append(Finding("info", "subset", "positive-control",
                                 f"class {k} is now canary-covered but not in the "
                                 f"pre-registered subset (spread_pool)"))
    else:
        pool_apps, pool_classes = live_apps, live_classes
    apps, classes = subset_spread(subset)
    for a in sorted(pool_apps - set(apps)):
        found.append(Finding("error", "subset", "positive-control",
                             f"app {a} has canary-covered cases but no entry"))
    for k in sorted(pool_classes - set(classes)):
        found.append(Finding("error", "subset", "positive-control",
                             f"class {k} has canary-covered cases but no entry"))
    cap = subset.get("max_per_app")
    if isinstance(cap, int):
        for a, n in apps.items():
            if n > cap:
                found.append(Finding("error", "subset", "positive-control",
                                     f"app {a} has {n} entries, above max_per_app {cap}"))
    return found


def subset_spread(subset: dict) -> tuple[dict[str, int], dict[str, int]]:
    apps: dict[str, int] = {}
    classes: dict[str, int] = {}
    for e in subset.get("briefs") or []:
        apps[e.get("app")] = apps.get(e.get("app"), 0) + 1
        classes[e.get("class")] = classes.get(e.get("class"), 0) + 1
    return dict(sorted(apps.items())), dict(sorted(classes.items()))


def load_subset(path: Path = _SUBSET_PATH) -> dict | None:
    return yaml.safe_load(path.read_text()) if path.exists() else None


# ── main ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--app", help="comma-separated app ids (default: every test-case file)")
    ap.add_argument("--no-subset", action="store_true", help="skip the positive-control subset gate")
    ap.add_argument("--quiet-warnings", action="store_true", help="print errors only")
    args = ap.parse_args(argv)
    app_ids = [a.strip() for a in args.app.split(",")] if args.app else None

    results = lint_corpus(app_ids)
    if not results:
        print("FAIL: no test-case files found")
        return 1
    errors = warnings = cases = briefs = 0
    for app_id, findings in results.items():
        doc = journey.load_cases(app_id) or {}
        for c in doc.get("test_cases") or []:
            cases += 1
            briefs += brief_of(c) is not None
        for f in findings:
            if f.level == "error":
                errors += 1
            else:
                warnings += 1
                if args.quiet_warnings:
                    continue
            print(f"{app_id:18s} {f}")

    for path in (SUBSET_PATHS if not args.no_subset and not app_ids else ()):
        from qualgentbench import corpus
        subset = load_subset(path)
        if subset is None:
            print(f"{'':18s} error   subset    missing {path}")
            errors += 1
        else:
            docs = {a: journey.load_cases(a) or {} for a in corpus.public_apps()}
            canaries = {a: spec_canaries(a) for a in docs}
            for f in lint_subset(subset, docs, canaries):
                print(f"{path.stem:18s} {f}")
                errors += f.level == "error"
            apps, classes = subset_spread(subset)
            print(f"\n{path.name}: {len(subset.get('briefs') or [])} briefs"
                  + (f" (rule {subset['rule']})" if subset.get("rule") else ""))
            print("  by app:       " + ", ".join(f"{k} {v}" for k, v in apps.items()))
            print("  by class:     " + ", ".join(f"{k} {v}" for k, v in classes.items()))
            print("  by detection: " + ", ".join(f"{k} {v}" for k, v in
                                                 subset_detection(subset).items()))

    print(f"\n{briefs}/{cases} case(s) carry a brief in {len(results)} file(s): "
          f"{errors} error(s), {warnings} warning(s)")
    if errors:
        print("FAIL: a brief is missing or malformed, leaks a defect, a procedure hint, "
              "failure language or a check value, or the positive-control subset is invalid")
        return 1
    print("PASS: every brief present and neutral; positive-control subset canary-covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
