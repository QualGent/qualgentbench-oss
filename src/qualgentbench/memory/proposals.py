"""`workspace/memory_proposals.yaml`: the memory proposals a run agent writes (spec §3.4).

During an episode the agent may append proposals for the app's memory. A curator,
run between stages, decides what happens to each one (`memory.curator`). The agent
never edits memory files itself.

The file is a YAML list. Appending one more item is always valid YAML, so the agent
never has to restructure the file:

    - type: nav                       # nav | setup | flaky | bug | note
      text: The archive is under the overflow menu on the note list.
      evidence:                       # where the agent saw it: step and/or screen
        step: 14
        screen: Note list
      updates: nav-003                # optional: this refines an existing entry
    - type: flaky
      text: Search results take about two seconds to appear.
      evidence: {step: 22, screen: Search}
      contradicts: flaky-001          # optional: this says an existing entry is wrong

A mapping `{version: 1, proposals: [...]}` is accepted too. `accepted` proposals are
accepted by the schema so the curator can log their rejection (D6); org-layer types
are not app memory and are rejected here.

Validation is per proposal: one bad item is reported in `ProposalFile.errors` and
dropped, and the rest of the file is still used. A file that is not YAML, or not a
list, gives no proposals and one file-level error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import format as fmt

FILENAME = "memory_proposals.yaml"
VERSION = 1

# The types an agent may propose. `accepted` is valid input but always rejected by
# the curator (D6), so it is listed here and nowhere else in the write path.
PROPOSAL_TYPES = fmt.APP_TYPES          # nav, setup, flaky, accepted, bug, note
WRITABLE_TYPES = tuple(t for t in PROPOSAL_TYPES if t != fmt.ACCEPTED_TYPE)

MAX_TEXT_CHARS = 300
MAX_SCREEN_CHARS = 120
# Proposals beyond this many in one file are dropped (and logged): a run that writes
# hundreds of proposals is noise, not memory.
MAX_PER_FILE = 25
FIELDS = ("type", "text", "evidence", "updates", "contradicts")
_ID_RE = re.compile(r"^" + fmt.ID_RE + r"$")

# Short help for prompts that ask an agent to write proposals. Facts only; the prompt
# around it explains what memory is for (spec §3.4: no rigid rules).
FORMAT_HELP = f"""\
Memory proposals go in `{FILENAME}` in the workspace, as a YAML list. Append one item
per fact:

- type: nav            # one of: {", ".join(WRITABLE_TYPES)}
  text: <one sentence, at most {MAX_TEXT_CHARS} characters>
  evidence: {{step: <step number>, screen: <screen name>}}
  updates: <entry id>      # optional, when it refines an existing memory entry
  contradicts: <entry id>  # optional, when an existing memory entry is wrong
"""


class ProposalError(ValueError):
    pass


@dataclass(frozen=True)
class Evidence:
    step: int | None = None
    screen: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in (("step", self.step), ("screen", self.screen))
                if v is not None}


@dataclass(frozen=True)
class Proposal:
    index: int                      # position in the file, 0-based
    type: str
    text: str
    evidence: Evidence
    updates: str | None = None
    contradicts: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "text": self.text,
                             "evidence": self.evidence.as_dict()}
        if self.updates:
            d["updates"] = self.updates
        if self.contradicts:
            d["contradicts"] = self.contradicts
        return d


@dataclass
class ProposalFile:
    proposals: list[Proposal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing: bool = False           # the file did not exist
    dropped_over_cap: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing


def _clean_text(v: object, *, limit: int, what: str) -> str:
    if not isinstance(v, str):
        raise ProposalError(f"{what} must be a string")
    s = " ".join(v.split())
    if not s:
        raise ProposalError(f"{what} is empty")
    if "<!--" in s or "-->" in s:
        raise ProposalError(f"{what} contains an HTML comment marker")
    if len(s) > limit:
        raise ProposalError(f"{what} is {len(s)} characters (max {limit})")
    return s


def _parse_id(v: object, what: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _ID_RE.match(v.strip()):
        raise ProposalError(f"{what} must be an entry id like nav-003, got {v!r}")
    return v.strip()


def parse_item(item: object, index: int) -> Proposal:
    """One list item → `Proposal`, or `ProposalError` saying what is wrong."""
    if not isinstance(item, dict):
        raise ProposalError("item is not a mapping")
    unknown = sorted(str(k) for k in item if k not in FIELDS)
    if unknown:
        raise ProposalError(f"unknown field(s) {unknown}")
    ptype = item.get("type")
    if not isinstance(ptype, str) or ptype.strip() not in PROPOSAL_TYPES:
        raise ProposalError(f"type must be one of {list(WRITABLE_TYPES)}, got {ptype!r}")
    text = _clean_text(item.get("text"), limit=MAX_TEXT_CHARS, what="text")

    ev = item.get("evidence")
    if not isinstance(ev, dict):
        raise ProposalError("evidence must be a mapping with step and/or screen")
    bad = sorted(str(k) for k in ev if k not in ("step", "screen"))
    if bad:
        raise ProposalError(f"unknown evidence field(s) {bad}")
    step = ev.get("step")
    if step is not None and (isinstance(step, bool) or not isinstance(step, int) or step < 0):
        raise ProposalError(f"evidence.step must be a non-negative integer, got {step!r}")
    screen = ev.get("screen")
    if screen is not None:
        screen = _clean_text(screen, limit=MAX_SCREEN_CHARS, what="evidence.screen")
    if step is None and screen is None:
        raise ProposalError("evidence needs a step or a screen")

    updates = _parse_id(item.get("updates"), "updates")
    contradicts = _parse_id(item.get("contradicts"), "contradicts")
    if updates and contradicts:
        raise ProposalError("a proposal has updates or contradicts, not both")
    return Proposal(index=index, type=ptype.strip(), text=text,
                    evidence=Evidence(step=step, screen=screen),
                    updates=updates, contradicts=contradicts)


def loads(text: str) -> ProposalFile:
    out = ProposalFile()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        out.errors.append(f"not valid YAML: {str(e).splitlines()[0]}")
        return out
    if doc is None:
        return out
    if isinstance(doc, dict):
        version = doc.get("version", VERSION)
        if version != VERSION:
            out.errors.append(f"unsupported version {version!r} (expected {VERSION})")
            return out
        extra = sorted(str(k) for k in doc if k not in ("version", "proposals"))
        if extra:
            out.errors.append(f"unknown top-level field(s) {extra}")
        doc = doc.get("proposals") or []
    if not isinstance(doc, list):
        out.errors.append("expected a YAML list of proposals")
        return out
    for i, item in enumerate(doc):
        if len(out.proposals) >= MAX_PER_FILE:
            out.dropped_over_cap = len(doc) - i
            out.errors.append(f"{out.dropped_over_cap} proposal(s) over the cap of "
                              f"{MAX_PER_FILE} dropped")
            break
        try:
            out.proposals.append(parse_item(item, i))
        except ProposalError as e:
            out.errors.append(f"proposal {i}: {e}")
    return out


def load(path: Path | str) -> ProposalFile:
    """Read a proposals file. A missing file is `missing=True` with no proposals; the
    curator logs it and carries memory forward (it is not an error to propose nothing)."""
    p = Path(path)
    if not p.is_file():
        return ProposalFile(missing=True)
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ProposalFile(errors=["file is not UTF-8"])
    return loads(text)


def dumps(proposals: list[Proposal]) -> str:
    """The canonical file for `proposals` (a YAML list), e.g. for fixtures."""
    return yaml.safe_dump([p.as_dict() for p in proposals], sort_keys=False,
                          allow_unicode=True)
