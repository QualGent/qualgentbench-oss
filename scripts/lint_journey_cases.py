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
  columns     a `db:`/`content:` query whose text names none of the brief's key nouns
              (quoted strings, numbers, capitalised names) — the oracle may be checking
              something other than what the brief promises (WARNING only: a fixture id
              such as `id=3` is a legitimate stand-in for "the Aug 29 measurement").
  short       an `evidence:` string under three characters — a witness that matches
              inside anything (warning).

Exit 1 on any error. `--app a,b` narrows the files. The rules are pure functions over a
loaded document so `tests/test_lint_journey_cases.py` can drive each one in isolation.
"""

from __future__ import annotations

import argparse
import re
import sys

from qualgentbench import journey

_MIN_WITNESS_CHARS = 3


class Finding:
    """One lint result. A plain class, not a dataclass: the tests load this script
    through importlib without registering it in sys.modules, and a dataclass under
    `from __future__ import annotations` needs its module there to resolve fields."""

    def __init__(self, level: str, rule: str, case: str, detail: str) -> None:
        self.level = level          # "error" | "warning"
        self.rule = rule            # leak | brief | no-oracle | columns | short
        self.case = case
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


def lint_doc(doc: dict, truth: dict | None = None) -> list[Finding]:
    """Every rule over one loaded test-case document. `truth` is the app's measured
    journey truth (optional — the diff-based leak check is skipped without it)."""
    defects = journey.load_defects(doc)
    findings: list[Finding] = []
    for case in doc.get("test_cases") or []:
        findings += rule_no_oracle(case)
        findings += rule_leak(case, defects, truth)
        findings += rule_brief(case, defects)
        findings += rule_brief_symptom(case, defects)
        findings += rule_columns(case)
        findings += rule_short(case)
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
        out[app_id] = lint_doc(doc, journey.load_truth(app_id))
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
        print("FAIL: a completion witness or a brief carries a seeded defect, or a case has no oracle")
        return 1
    print("PASS: no leaked marker, no oracle-less case")
    return 0


if __name__ == "__main__":
    sys.exit(main())
