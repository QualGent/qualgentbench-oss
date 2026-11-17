"""The memory file format: parse, edit and write org and app memory files.

A memory file is Markdown with a fixed set of `## ` sections. Every entry is ONE list
item with a stable id and its metadata in a trailing HTML comment:

    - [nav-003] Long-press a note, then tap ⋮ → "Properties". <!-- src=human; confirmed=2026-11-16; seen=4; contra=0 -->

The parser keeps every byte it does not understand (preamble, blank lines, prose,
comments after a heading), so `parse(text).dumps() == text` for any file. An entry
that has not been edited is written back from its original line; an edited or new
entry is written in the canonical form above. Ids never change: `add_entry` takes the
next free number for the prefix (counting invalidated entries too), and `invalidate`
moves an entry into `## Invalidated` with a reason instead of deleting it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ── layers, sections and entry types ───────────────────────────────────────────

ORG = "org"
APP = "app"
LAYERS = (ORG, APP)

INVALIDATED = "Invalidated"

# (section heading, entry type == id prefix), in the order a new file lists them.
APP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Navigation", "nav"),
    ("Login and setup", "setup"),
    ("Flaky or slow areas", "flaky"),
    ("Known accepted behavior", "accepted"),
    ("Past bugs", "bug"),
    ("Notes", "note"),
)
ORG_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Company", "company"),
    ("Users", "users"),
    ("What matters to them", "matters"),
    ("Terminology", "term"),
)
SECTIONS = {APP: APP_SECTIONS, ORG: ORG_SECTIONS}

APP_TYPES = tuple(t for _, t in APP_SECTIONS)
ORG_TYPES = tuple(t for _, t in ORG_SECTIONS)
ALL_TYPES = APP_TYPES + ORG_TYPES

# The section `accepted` entries live in; only humans may write it (D6).
ACCEPTED_TYPE = "accepted"
HUMAN_SRC = "human"


def section_for_type(entry_type: str) -> str:
    for layer in LAYERS:
        for heading, t in SECTIONS[layer]:
            if t == entry_type:
                return heading
    raise ValueError(f"unknown entry type {entry_type!r}")


def type_for_section(layer: str, heading: str) -> str | None:
    for h, t in SECTIONS[layer]:
        if h == heading:
            return t
    return None


# ── line grammar ───────────────────────────────────────────────────────────────

ID_RE = r"(?P<type>[a-z]+)-(?P<num>\d{3,})"
_ENTRY_RE = re.compile(
    r"^- \[" + ID_RE + r"\] (?P<text>.*?)"
    r"(?: <!-- (?P<meta>.*?) -->)?(?P<nl>\r?\n)?$",
    re.DOTALL,
)
# A `## ` heading, optionally followed by an HTML comment the parser keeps verbatim.
_HEADING_RE = re.compile(r"^## (?P<name>.+?)(?P<tail>\s*<!--.*-->)?\s*$")
_TITLE_RE = re.compile(r"^# (?P<title>.+?)\s*$")

META_ORDER = ("src", "confirmed", "seen", "contra")


class FormatError(ValueError):
    pass


def parse_meta(raw: str) -> dict[str, str]:
    """`src=human; confirmed=2026-11-16; seen=4` → ordered dict. Keys other than the
    four standard ones (`reason`, `invalidated`, `case`, …) are kept in order."""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise FormatError(f"metadata item {part!r} is not key=value")
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not re.fullmatch(r"[a-z_]+", k):
            raise FormatError(f"metadata key {k!r} is not [a-z_]+")
        if k in out:
            raise FormatError(f"metadata key {k!r} repeated")
        out[k] = v
    return out


def format_meta(meta: dict[str, str]) -> str:
    keys = [k for k in META_ORDER if k in meta] + [k for k in meta if k not in META_ORDER]
    return "; ".join(f"{k}={meta[k]}" for k in keys)


def _clean_meta_value(v: object) -> str:
    s = str(v)
    if any(c in s for c in (";", "\n", "\r")) or "-->" in s:
        raise FormatError(f"metadata value {s!r} contains ';', a newline or '-->'")
    return s.strip()


# ── the model ─────────────────────────────────────────────────────────────────

@dataclass
class Entry:
    id: str
    text: str
    meta: dict[str, str] = field(default_factory=dict)
    # The original line (with its newline) and the field state it was parsed with. When
    # the fields still match, `line()` returns the original bytes.
    _raw: str | None = field(default=None, repr=False, compare=False)
    _parsed: tuple | None = field(default=None, repr=False, compare=False)

    @property
    def type(self) -> str:
        return self.id.rsplit("-", 1)[0]

    @property
    def number(self) -> int:
        return int(self.id.rsplit("-", 1)[1])

    @property
    def src(self) -> str | None:
        return self.meta.get("src")

    @property
    def confirmed(self) -> str:
        return self.meta.get("confirmed", "")

    def _int(self, key: str) -> int:
        try:
            return int(self.meta.get(key, "0"))
        except ValueError:
            return 0

    @property
    def seen(self) -> int:
        return self._int("seen")

    @property
    def contra(self) -> int:
        return self._int("contra")

    def _state(self) -> tuple:
        return (self.id, self.text, tuple(self.meta.items()))

    def canonical(self) -> str:
        text = " ".join(self.text.split())
        if not text:
            raise FormatError(f"entry {self.id} has no text")
        meta = {k: _clean_meta_value(v) for k, v in self.meta.items()}
        tail = f" <!-- {format_meta(meta)} -->" if meta else ""
        return f"- [{self.id}] {text}{tail}\n"

    def line(self) -> str:
        if self._raw is not None and self._parsed == self._state():
            return self._raw
        return self.canonical()


@dataclass
class Section:
    """One `## ` section. `heading` is the original heading line (newline included);
    `items` mixes entries and verbatim lines in file order."""
    name: str
    heading: str
    items: list[Entry | str] = field(default_factory=list)

    @property
    def entries(self) -> list[Entry]:
        return [i for i in self.items if isinstance(i, Entry)]

    @property
    def invalidated(self) -> bool:
        return self.name == INVALIDATED


@dataclass
class MemoryFile:
    layer: str
    preamble: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    # Lines that look like entries but do not parse; the lint reports them.
    malformed: list[tuple[int, str]] = field(default_factory=list)

    # ── reading ──
    @property
    def title(self) -> str:
        for line in self.preamble:
            m = _TITLE_RE.match(line)
            if m:
                return m.group("title")
        return ""

    def section(self, name: str) -> Section | None:
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def entries(self, *, include_invalidated: bool = False) -> list[Entry]:
        return [e for s in self.sections if include_invalidated or not s.invalidated
                for e in s.entries]

    def live_sections(self) -> list[Section]:
        return [s for s in self.sections if not s.invalidated]

    def find(self, entry_id: str) -> tuple[Section, Entry] | None:
        for s in self.sections:
            for e in s.entries:
                if e.id == entry_id:
                    return s, e
        return None

    def get(self, entry_id: str) -> Entry:
        hit = self.find(entry_id)
        if hit is None:
            raise KeyError(entry_id)
        return hit[1]

    def is_invalidated(self, entry_id: str) -> bool:
        hit = self.find(entry_id)
        return bool(hit and hit[0].invalidated)

    # ── writing ──
    def dumps(self) -> str:
        out = list(self.preamble)
        for s in self.sections:
            out.append(s.heading)
            out.extend(i.line() if isinstance(i, Entry) else i for i in s.items)
        return "".join(out)

    def save(self, path: Path) -> None:
        Path(path).write_text(self.dumps(), encoding="utf-8")

    def next_id(self, entry_type: str) -> str:
        nums = [e.number for e in self.entries(include_invalidated=True)
                if e.type == entry_type]
        return f"{entry_type}-{(max(nums) if nums else 0) + 1:03d}"

    def _ensure_section(self, name: str) -> Section:
        s = self.section(name)
        if s is not None:
            return s
        s = Section(name=name, heading=f"## {name}\n")
        # Keep the fixed order: insert before the first later fixed section, and always
        # before `## Invalidated`.
        order = [h for h, _ in SECTIONS[self.layer]] + [INVALIDATED]
        rank = order.index(name) if name in order else len(order) - 1
        at = len(self.sections)
        for i, other in enumerate(self.sections):
            if other.name in order and order.index(other.name) > rank:
                at = i
                break
        self._end_with_newline(at)
        self.sections.insert(at, s)
        return s

    def _end_with_newline(self, at: int) -> None:
        """Make sure whatever precedes position `at` ends with a newline, so a new
        heading starts on its own line."""
        prev = self.sections[at - 1] if at > 0 else None
        if prev is None:
            if self.preamble and not self.preamble[-1].endswith("\n"):
                self.preamble[-1] += "\n"
            return
        if prev.items:
            last = prev.items[-1]
            if isinstance(last, str) and not last.endswith("\n"):
                prev.items[-1] = last + "\n"
            elif isinstance(last, Entry) and not last.line().endswith("\n"):
                last._raw = None  # rewrite canonically, which ends with "\n"
        elif not prev.heading.endswith("\n"):
            prev.heading += "\n"

    @staticmethod
    def _append(section: Section, entry: Entry) -> None:
        # Insert after the last entry (or after the heading), before trailing blank
        # lines/prose, so a blank line between sections stays where it was.
        at = 0
        for i, item in enumerate(section.items):
            if isinstance(item, Entry) or item.strip():
                at = i + 1
        if at and isinstance(section.items[at - 1], str) and \
                not section.items[at - 1].endswith("\n"):
            section.items[at - 1] += "\n"
        if at and isinstance(section.items[at - 1], Entry):
            prev = section.items[at - 1]
            if not prev.line().endswith("\n"):
                prev._raw = None
        section.items.insert(at, entry)

    def add_entry(self, entry_type: str, text: str, meta: dict[str, object] | None = None,
                  *, entry_id: str | None = None) -> Entry:
        """Append a new entry to the section of `entry_type`, with the next free id
        (or `entry_id`, which must be unused)."""
        allowed = {t: h for h, t in SECTIONS[self.layer]}
        if entry_type not in allowed:
            raise FormatError(f"type {entry_type!r} does not belong in a {self.layer} file")
        if entry_id is None:
            entry_id = self.next_id(entry_type)
        elif self.find(entry_id) is not None:
            raise FormatError(f"id {entry_id} already exists")
        elif not re.fullmatch(ID_RE, entry_id) or entry_id.rsplit("-", 1)[0] != entry_type:
            raise FormatError(f"id {entry_id!r} is not {entry_type}-NNN")
        entry = Entry(id=entry_id, text=" ".join(text.split()),
                      meta={k: _clean_meta_value(v) for k, v in (meta or {}).items()})
        entry.canonical()  # validates
        self._append(self._ensure_section(allowed[entry_type]), entry)
        return entry

    def update_entry(self, entry_id: str, *, text: str | None = None,
                     **meta: object) -> Entry:
        """Change an entry's text and/or metadata in place. A metadata value of None
        removes that key. Invalidated entries cannot be updated."""
        hit = self.find(entry_id)
        if hit is None:
            raise KeyError(entry_id)
        section, entry = hit
        if section.invalidated:
            raise FormatError(f"{entry_id} is invalidated")
        if text is not None:
            entry.text = " ".join(text.split())
        for k, v in meta.items():
            if v is None:
                entry.meta.pop(k, None)
            else:
                entry.meta[k] = _clean_meta_value(v)
        entry.canonical()
        return entry

    def bump(self, entry_id: str, *, seen: int = 0, contra: int = 0,
             confirmed: str | None = None) -> Entry:
        e = self.get(entry_id)
        upd: dict[str, object] = {}
        if seen:
            upd["seen"] = e.seen + seen
        if contra:
            upd["contra"] = e.contra + contra
        if confirmed:
            upd["confirmed"] = confirmed
        return self.update_entry(entry_id, **upd)

    def invalidate(self, entry_id: str, reason: str, *, date: str | None = None) -> Entry:
        """Move an entry to `## Invalidated`, keeping its id, text and metadata, and
        record `reason` (and `invalidated=<date>`)."""
        hit = self.find(entry_id)
        if hit is None:
            raise KeyError(entry_id)
        section, entry = hit
        if section.invalidated:
            raise FormatError(f"{entry_id} is already invalidated")
        if not reason.strip():
            raise FormatError("invalidate needs a reason")
        section.items.remove(entry)
        entry.meta["reason"] = _clean_meta_value(" ".join(reason.split()))
        if date:
            entry.meta["invalidated"] = _clean_meta_value(date)
        entry.canonical()
        self._append(self._ensure_section(INVALIDATED), entry)
        return entry


# ── parsing ───────────────────────────────────────────────────────────────────

def _looks_like_entry(line: str) -> bool:
    """A top-level list item. Every one of them must be an entry; one that is not is
    kept verbatim and reported as malformed."""
    return line.startswith(("- ", "* ", "+ ")) or line.rstrip("\r\n") in ("-", "*", "+")


def parse_entry(line: str) -> Entry | None:
    m = _ENTRY_RE.match(line)
    if not m:
        return None
    meta = parse_meta(m.group("meta")) if m.group("meta") is not None else {}
    text = m.group("text")
    if not text.strip() or "<!--" in text:
        return None
    e = Entry(id=f"{m.group('type')}-{m.group('num')}", text=text, meta=meta, _raw=line)
    e._parsed = e._state()
    return e


def loads(text: str, layer: str) -> MemoryFile:
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {LAYERS}")
    mf = MemoryFile(layer=layer)
    current: Section | None = None
    for n, line in enumerate(text.splitlines(keepends=True), start=1):
        h = _HEADING_RE.match(line.rstrip("\r\n"))
        if h:
            current = Section(name=h.group("name").strip(), heading=line)
            mf.sections.append(current)
            continue
        if current is None:
            mf.preamble.append(line)
            continue
        item: Entry | str = line
        if _looks_like_entry(line):
            try:
                parsed = parse_entry(line)
            except FormatError:
                parsed = None
            if parsed is None:
                mf.malformed.append((n, line.rstrip("\r\n")))
            else:
                item = parsed
        current.items.append(item)
    return mf


def load(path: Path, layer: str | None = None) -> MemoryFile:
    path = Path(path)
    return loads(path.read_text(encoding="utf-8"), layer or layer_of(path))


def new_file(layer: str, title: str) -> MemoryFile:
    """An empty file with the title and every fixed section."""
    body = f"# {title}\n" + "".join(f"\n## {h}\n" for h, _ in SECTIONS[layer])
    return loads(body, layer)


# ── paths ─────────────────────────────────────────────────────────────────────

CHANGES_SUFFIX = ".changes.md"


def org_path(root: Path, org_key: str) -> Path:
    return Path(root) / "org" / f"{org_key}.md"


def app_path(root: Path, org_key: str, app_key: str) -> Path:
    return Path(root) / "app" / org_key / f"{app_key}.md"


def changes_path(root: Path, org_key: str, app_key: str) -> Path:
    return Path(root) / "app" / org_key / f"{app_key}{CHANGES_SUFFIX}"


def find_org(root: Path, app_key: str) -> str:
    """The org key whose `app/<org>/<app_key>.md` exists. Exactly one must."""
    hits = sorted(p.parent.name for p in (Path(root) / "app").glob(f"*/{app_key}.md"))
    if len(hits) != 1:
        raise FileNotFoundError(
            f"expected one app/<org>/{app_key}.md under {root}, found {hits or 'none'}")
    return hits[0]


def layer_of(path: Path) -> str:
    """`org` for `…/org/<k>.md`, `app` for `…/app/<org>/<k>.md`."""
    p = Path(path)
    if p.name.endswith(CHANGES_SUFFIX):
        raise ValueError(f"{p} is a changes file, not an org/app memory file")
    if p.parent.name == "org":
        return ORG
    if p.parent.parent.name == "app":
        return APP
    raise ValueError(f"cannot tell the layer of {p}: expected org/<k>.md or app/<org>/<k>.md")
