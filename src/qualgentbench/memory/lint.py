"""Lint for memory files and curator output. Any issue fails the file.

Content rules (spec §3.2), checked on every line of the file (entries, prose, and the
`## Invalidated` section too — the lint does not trust that a section stays hidden):

- `defect-id`: a seeded defect id from the corpus (benchmark `bugs[].id`, feature and
  task `bug_id`, test-case `defects[].id`). Flag names are the defect ids, so this rule
  also covers `QgbFlags.on("<id>")` names.
- `qgb-flag`: the flag shim itself (`QgbFlags`, `qgb_flags`).
- `symptom`: a defect's symptom wording or title from the corpus (`symptoms:`,
  `symptom_keywords:`, bug/defect `title:`). An app file is checked against ITS app's
  defects, an org file against the apps filed under that org (all apps when none are).
  Only phrases of `MIN_SYMPTOM_WORDS`+ words, plus single exception/error class names,
  are matched: the corpus lists one- and two-word symptoms such as "crash", "add note"
  or "home screen", which are ordinary navigation words and would fail every honest file.
- `instruction`: text that reads as an order to the agent ("ignore", "always report",
  "you must", …). Memory states facts; it does not instruct.
- `accepted-src`: a `Known accepted behavior` entry (or an `accepted-*` id) whose
  `src` is not `human` (D6).

Structure rules (`format`): a list item that does not parse as an entry, an unknown
section, a duplicate id, an id whose type does not match its section, an entry without
`src`, a non-integer `seen`/`contra`, a `confirmed` that is not YYYY-MM-DD, and an
invalidated entry without `reason`.

`*.changes.md` files get `defect-id`, `qgb-flag` and `instruction` only: the hunt
sub-arm reports automated findings in its own words by design (D17).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import format as fmt

MIN_SYMPTOM_WORDS = 3
_EXCEPTION_RE = re.compile(r"^[a-z]+(?:exception|error)$")

INSTRUCTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bignore\b", "ignore"),
    (r"\bdisregard\b", "disregard"),
    (r"\balways (?:report|flag|mark|fail|pass)\b", "always report"),
    (r"\bnever (?:report|flag|mark|fail|pass)\b", "never report"),
    (r"\b(?:do not|don't|dont) (?:report|flag|file|mention)\b", "do not report"),
    (r"\byou (?:must|should|need to|have to)\b", "you must"),
    (r"\bmake sure (?:you|to)\b", "make sure"),
    (r"\b(?:report|mark|treat) (?:it|this|them) as (?:a )?(?:pass|passed|fail|failed|bug)\b",
     "report it as"),
)
_INSTRUCTION = [(re.compile(p, re.IGNORECASE), name) for p, name in INSTRUCTION_PATTERNS]
_FLAG_RE = re.compile(r"qgb_?flags", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

RULES = ("defect-id", "qgb-flag", "symptom", "instruction", "accepted-src", "format")


@dataclass(frozen=True)
class LintIssue:
    rule: str
    path: str
    line: int
    message: str
    entry_id: str | None = None

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        eid = f" [{self.entry_id}]" if self.entry_id else ""
        return f"{where}: {self.rule}{eid}: {self.message}"


def normalize(text: str) -> str:
    """Lowercase, curly quotes straightened, every run of non-word characters (except
    an apostrophe) one space. Phrases and text go through the same fold."""
    t = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(re.sub(r"[^\w']+", " ", t).split())


# ── corpus terms ──────────────────────────────────────────────────────────────

@dataclass
class CorpusTerms:
    defect_ids: set[str] = field(default_factory=set)
    # app id -> normalized phrases (symptoms + titles) for that app's defects
    phrases: dict[str, set[str]] = field(default_factory=dict)

    def phrases_for(self, apps: list[str] | None) -> set[str]:
        if apps is None:
            return set().union(*self.phrases.values()) if self.phrases else set()
        return set().union(*(self.phrases.get(a, set()) for a in apps)) if apps else set()

    def add_phrase(self, app: str, phrase: object) -> None:
        if not isinstance(phrase, str):
            return
        norm = normalize(phrase)
        words = norm.split()
        if len(words) >= MIN_SYMPTOM_WORDS or (
                len(words) == 1 and _EXCEPTION_RE.match(words[0]) and len(words[0]) >= 12):
            self.phrases.setdefault(app, set()).add(norm)

    def add_spec(self, app: str, doc: dict) -> None:
        for b in doc.get("bugs") or []:
            if isinstance(b, dict):
                if b.get("id"):
                    self.defect_ids.add(str(b["id"]))
                self.add_phrase(app, b.get("title"))
        for f in (doc.get("exploration") or {}).get("features") or []:
            if isinstance(f, dict) and f.get("bug_id"):
                self.defect_ids.add(str(f["bug_id"]))
        for t in doc.get("tasks") or []:
            if isinstance(t, dict):
                if t.get("bug_id"):
                    self.defect_ids.add(str(t["bug_id"]))
                for s in t.get("symptom_keywords") or []:
                    self.add_phrase(app, s)
        for d in doc.get("defects") or []:
            if isinstance(d, dict):
                if d.get("id"):
                    self.defect_ids.add(str(d["id"]))
                self.add_phrase(app, d.get("title"))
                for s in d.get("symptoms") or []:
                    self.add_phrase(app, s)

    @classmethod
    def load(cls) -> CorpusTerms:
        """Every benchmark spec and test-case file, held-out split included when
        `QGB_HELDOUT_DIR` is set (the same precedence as every other loader)."""
        from .. import corpus

        terms = cls()
        paths = list(corpus.spec_paths())
        tc: dict[str, Path] = {p.name: p for p in (corpus.PACKAGED / "test-cases").glob("*.yaml")}
        hd = corpus.heldout_dir()
        if hd and (hd / "test-cases").is_dir():
            tc.update({p.name: p for p in (hd / "test-cases").glob("*.yaml")})
        paths += [tc[k] for k in sorted(tc)]
        for p in paths:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(doc, dict):
                terms.add_spec(p.stem, doc)
        return terms


_DEFAULT: CorpusTerms | None = None


def default_terms() -> CorpusTerms:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = CorpusTerms.load()
    return _DEFAULT


# ── checks ────────────────────────────────────────────────────────────────────

def _id_pattern(ids: set[str]) -> re.Pattern | None:
    if not ids:
        return None
    alts = "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])", re.IGNORECASE)


def _content_issues(line: str, n: int, path: str, eid: str | None, *, id_re,
                    phrases: set[str], check_symptoms: bool) -> list[LintIssue]:
    out: list[LintIssue] = []
    if id_re is not None:
        for m in id_re.finditer(line):
            out.append(LintIssue("defect-id", path, n, f"names seeded defect {m.group(0)!r}",
                                 eid))
    if _FLAG_RE.search(line):
        out.append(LintIssue("qgb-flag", path, n, "names the QgbFlags shim", eid))
    if check_symptoms and phrases:
        padded = f" {normalize(line)} "
        for ph in sorted(phrases):
            if f" {ph} " in padded:
                out.append(LintIssue("symptom", path, n,
                                     f"repeats a defect's symptom wording {ph!r}", eid))
    for rx, name in _INSTRUCTION:
        if rx.search(line):
            out.append(LintIssue("instruction", path, n,
                                 f"reads as an instruction to the agent ({name!r})", eid))
    return out


def _structure_issues(doc: fmt.MemoryFile, path: str) -> list[LintIssue]:
    out = [LintIssue("format", path, n, f"list item is not a valid entry: {line!r}")
           for n, line in doc.malformed]
    allowed = {h: t for h, t in fmt.SECTIONS[doc.layer]}
    seen_ids: set[str] = set()
    for s in doc.sections:
        if s.name not in allowed and not s.invalidated:
            out.append(LintIssue("format", path, 0, f"unknown section '## {s.name}'"))
        for e in s.entries:
            if e.id in seen_ids:
                out.append(LintIssue("format", path, 0, "duplicate id", e.id))
            seen_ids.add(e.id)
            want = allowed.get(s.name)
            if s.invalidated:
                if e.type not in allowed.values():
                    out.append(LintIssue("format", path, 0,
                                         f"type {e.type!r} is not a {doc.layer} type", e.id))
                if not e.meta.get("reason"):
                    out.append(LintIssue("format", path, 0,
                                         "invalidated entry has no reason", e.id))
            elif want is not None and e.type != want:
                out.append(LintIssue("format", path, 0,
                                     f"id type {e.type!r} in section '{s.name}' "
                                     f"(expects {want}-NNN)", e.id))
            if not e.src:
                out.append(LintIssue("format", path, 0, "entry has no src", e.id))
            for k in ("seen", "contra"):
                v = e.meta.get(k)
                if v is not None and not v.isdigit():
                    out.append(LintIssue("format", path, 0,
                                         f"{k}={v!r} is not a non-negative integer", e.id))
            if e.meta.get("confirmed") and not _DATE_RE.match(e.meta["confirmed"]):
                out.append(LintIssue("format", path, 0,
                                     f"confirmed={e.meta['confirmed']!r} is not YYYY-MM-DD",
                                     e.id))
            if (e.type == fmt.ACCEPTED_TYPE or s.name == fmt.section_for_type("accepted")) \
                    and e.src != fmt.HUMAN_SRC:
                out.append(LintIssue("accepted-src", path, 0,
                                     f"accepted behavior with src={e.src!r}; only "
                                     f"{fmt.HUMAN_SRC!r} may write it", e.id))
    return out


def _with_lines(doc: fmt.MemoryFile, issues: list[LintIssue]) -> list[LintIssue]:
    """Fill in the line number of entry-level structure issues."""
    lines: dict[str, int] = {}
    n = len(doc.preamble)
    for s in doc.sections:
        n += 1
        for item in s.items:
            n += 1
            if isinstance(item, fmt.Entry):
                lines.setdefault(item.id, n)
    return [LintIssue(i.rule, i.path, lines.get(i.entry_id or "", i.line), i.message,
                      i.entry_id) if i.line == 0 and i.entry_id else i for i in issues]


def lint_text(text: str, *, layer: str, path: str = "<text>",
              apps: list[str] | None = None, terms: CorpusTerms | None = None
              ) -> list[LintIssue]:
    """Lint one file's text. `layer` is `org`, `app` or `changes`. `apps` scopes the
    symptom rule (None = every app's symptoms)."""
    terms = terms if terms is not None else default_terms()
    id_re = _id_pattern(terms.defect_ids)
    phrases = terms.phrases_for(apps)
    is_changes = layer == "changes"
    issues: list[LintIssue] = []
    entry_at: dict[int, str] = {}
    if not is_changes:
        doc = fmt.loads(text, layer)
        issues += _with_lines(doc, _structure_issues(doc, path))
        n = len(doc.preamble)
        for s in doc.sections:
            n += 1
            for item in s.items:
                n += 1
                if isinstance(item, fmt.Entry):
                    entry_at[n] = item.id
    for n, line in enumerate(text.splitlines(), start=1):
        issues += _content_issues(line, n, path, entry_at.get(n), id_re=id_re,
                                  phrases=phrases, check_symptoms=not is_changes)
    return sorted(issues, key=lambda i: (i.line, i.rule, i.message))


def _scope(path: Path, layer: str) -> list[str] | None:
    if layer in (fmt.APP, "changes"):
        name = path.name
        return [name[: -len(fmt.CHANGES_SUFFIX)] if name.endswith(fmt.CHANGES_SUFFIX)
                else path.stem]
    # org file at <root>/org/<org>.md: the apps filed under <root>/app/<org>/
    app_dir = path.parent.parent / "app" / path.stem
    apps = sorted(p.stem for p in app_dir.glob("*.md")
                  if not p.name.endswith(fmt.CHANGES_SUFFIX))
    return apps or None


def lint_file(path: Path | str, *, terms: CorpusTerms | None = None) -> list[LintIssue]:
    path = Path(path)
    layer = "changes" if path.name.endswith(fmt.CHANGES_SUFFIX) else fmt.layer_of(path)
    return lint_text(path.read_text(encoding="utf-8"), layer=layer, path=str(path),
                     apps=_scope(path, layer), terms=terms)


def lint_tree(root: Path | str, *, terms: CorpusTerms | None = None) -> list[LintIssue]:
    """Lint every memory file under a memory root (`org/*.md`, `app/*/*.md`)."""
    from .snapshot import memory_files

    root = Path(root)
    terms = terms if terms is not None else default_terms()
    out: list[LintIssue] = []
    for rel in memory_files(root):
        out += lint_file(root / rel, terms=terms)
    return out
