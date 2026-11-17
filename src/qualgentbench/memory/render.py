"""Render org + app memory into the `<qualgent_memory>` prompt block, within a budget.

One code path for every arm. `none` renders the empty string through the same function,
so the no-memory arm and the memory arms differ only in the budget value.

Budgets (arm values, D8) are token estimates, `estimate_tokens` = ceil(chars / 4). No
tokenizer is loaded: the estimate is deterministic, model-independent and slightly
generous for English prose, which is what a cut needs.

- `small` / `large`: entries ranked by section priority, then `seen - contra` (higher
  first), then recency (`confirmed`, newest first), then id. Entries are taken in rank
  order until the next one does not fit; the cut stops there. Kept entries are printed
  in file order (sections as the file lists them), so the block reads like the file.
- `index`: one line per entry (id + its first words) under each heading, cut the same
  way, plus a pointer to the full files the harness places in the workspace
  (`place_workspace`).

Invalidated entries and metadata comments are never rendered.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import format as fmt
from .snapshot import snapshot_hash, unit_files

NONE, SMALL, LARGE, INDEX = "none", "small", "large", "index"
BUDGETS: dict[str, int] = {NONE: 0, SMALL: 2000, LARGE: 8000, INDEX: 1000}
MODES = tuple(BUDGETS)

# The diff arm's "what changed" text is capped on its own, inside the budget.
CHANGES_CAP_TOKENS = 800
# Index mode: words of entry text kept per line.
INDEX_WORDS = 12

# Lower = kept first when cutting. One table over both layers.
TYPE_PRIORITY: dict[str, int] = {
    "setup": 0,
    "nav": 1,
    "accepted": 2,
    "company": 3,
    "flaky": 4,
    "matters": 5,
    "bug": 6,
    "users": 7,
    "term": 8,
    "note": 9,
}

WORKSPACE_DIR = "memory"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class Rendered:
    text: str
    mode: str
    budget: int
    tokens: int
    snapshot: str | None
    included: list[str] = field(default_factory=list)   # entry ids, file order
    dropped: list[str] = field(default_factory=list)    # entry ids, rank order
    changes_included: bool = False
    changes_truncated: bool = False

    def metrics(self) -> dict:
        """The per-episode fields the harness records."""
        return {
            "memory_mode": self.mode,
            "memory_tokens_injected": self.tokens,
            "snapshot_hash": self.snapshot,
            "memory_entries_injected": len(self.included),
            "memory_entries_dropped": len(self.dropped),
            "memory_changes_included": self.changes_included,
        }


def rank_key(entry: fmt.Entry) -> tuple:
    # Recency: ISO dates sort lexically; a missing date ranks oldest. Negate by mapping
    # each character so "newest first" stays a plain ascending sort.
    recency = tuple(-ord(c) for c in entry.confirmed)
    return (TYPE_PRIORITY.get(entry.type, len(TYPE_PRIORITY)),
            -(entry.seen - entry.contra),
            recency if entry.confirmed else (1,),
            entry.id)


def _entry_line(entry: fmt.Entry, mode: str) -> str:
    text = " ".join(entry.text.split())
    if mode == INDEX:
        words = text.split()
        if len(words) > INDEX_WORDS:
            text = " ".join(words[:INDEX_WORDS]) + " …"
    return f"- [{entry.id}] {text}\n"


def _cap(text: str, cap_tokens: int) -> tuple[str, bool]:
    """Keep whole lines up to `cap_tokens`; mark a cut with one line."""
    if estimate_tokens(text) <= cap_tokens:
        return text, False
    marker = "[…truncated]\n"
    out = ""
    for line in text.splitlines(keepends=True):
        if estimate_tokens(out + line + marker) > cap_tokens:
            break
        out += line
    return out + marker, True


def _changes_body(path: Path) -> str:
    """The changes file without its `# ` title line and blank edges."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    return body + "\n" if body else ""


@dataclass
class _Layer:
    label: str            # "Org" / "App"
    title: str
    doc: fmt.MemoryFile


def _assemble(header: str, layers: list[_Layer], keep: set[str], mode: str,
              changes: str, footer: str) -> str:
    out = [header]
    for layer in layers:
        out.append(f"{layer.label}: {layer.title}\n")
        for section in layer.doc.live_sections():
            lines = [_entry_line(e, mode) for e in section.entries if e.id in keep]
            if lines:
                out.append(f"## {section.name}\n")
                out.extend(lines)
    if changes:
        out.append("What changed since the last build:\n")
        out.append(changes)
    out.append(footer)
    return "".join(out)


def render(root: Path | str | None, org_key: str | None, app_key: str | None,
           mode: str, *, include_changes: bool = False,
           snapshot: str | None = None) -> Rendered:
    """Render the memory block for one (org, app) from the memory tree at `root`.

    `mode` is `none`, `small`, `large` or `index`. For `none` nothing is read and the
    text is empty. Otherwise both files must exist (a run that cannot read its
    snapshot fails; it never falls back to no memory). `snapshot` defaults to the
    hash of the files this render reads (`snapshot.unit_files`), which equals the hash
    of a snapshot frozen with exactly those files."""
    if mode not in BUDGETS:
        raise ValueError(f"memory mode must be one of {MODES}, not {mode!r}")
    budget = BUDGETS[mode]
    if mode == NONE:
        return Rendered(text="", mode=mode, budget=0, tokens=0, snapshot=None)
    if root is None or not org_key or not app_key:
        raise ValueError(f"mode {mode!r} needs root, org_key and app_key")
    root = Path(root)
    org_file = fmt.org_path(root, org_key)
    app_file = fmt.app_path(root, org_key, app_key)
    for p in (org_file, app_file):
        if not p.is_file():
            raise FileNotFoundError(f"memory file missing: {p}")
    snap = snapshot or snapshot_hash(
        root, unit_files(org_key, app_key, include_changes=include_changes))

    org_doc = fmt.load(org_file, fmt.ORG)
    app_doc = fmt.load(app_file, fmt.APP)
    layers = [_Layer("Org", org_doc.title or org_key, org_doc),
              _Layer("App", app_doc.title or app_key, app_doc)]

    changes, truncated = "", False
    if include_changes:
        cpath = fmt.changes_path(root, org_key, app_key)
        if not cpath.is_file():
            raise FileNotFoundError(f"changes file missing: {cpath}")
        changes, truncated = _cap(_changes_body(cpath), CHANGES_CAP_TOKENS)

    header = f'<qualgent_memory snapshot="{snap}">\n'
    footer = "</qualgent_memory>\n"
    if mode == INDEX:
        footer = (f"Full memory files are in the workspace: {WORKSPACE_DIR}/"
                  f"{fmt.org_path(Path(''), org_key).as_posix()} and {WORKSPACE_DIR}/"
                  f"{fmt.app_path(Path(''), org_key, app_key).as_posix()}"
                  " (read them with your file tools).\n" + footer)

    ranked = sorted((e for layer in layers for e in layer.doc.entries()), key=rank_key)
    keep: set[str] = set()
    text = _assemble(header, layers, keep, mode, changes, footer)
    if estimate_tokens(text) > budget:
        # Even the frame does not fit: drop the changes text first, never the frame.
        changes, truncated = "", bool(changes) or truncated
        text = _assemble(header, layers, keep, mode, changes, footer)
    dropped: list[str] = []
    for i, entry in enumerate(ranked):
        trial = _assemble(header, layers, keep | {entry.id}, mode, changes, footer)
        if estimate_tokens(trial) > budget:
            dropped = [e.id for e in ranked[i:]]
            break
        keep.add(entry.id)
        text = trial

    included = [e.id for layer in layers for e in layer.doc.entries() if e.id in keep]
    return Rendered(text=text, mode=mode, budget=budget, tokens=estimate_tokens(text),
                    snapshot=snap, included=included, dropped=dropped,
                    changes_included=bool(changes), changes_truncated=truncated)


def injectable_copy(doc: fmt.MemoryFile) -> str:
    """The file as an agent may read it: everything except `## Invalidated`."""
    stripped = fmt.MemoryFile(layer=doc.layer, preamble=list(doc.preamble),
                              sections=[s for s in doc.sections if not s.invalidated])
    return stripped.dumps()


def place_workspace(root: Path | str, org_key: str, app_key: str, workspace: Path | str,
                    *, include_changes: bool = False) -> list[Path]:
    """Write the (org, app) files into `<workspace>/memory/` for the `index` mode, with
    the `## Invalidated` section removed. Returns the written paths. An existing
    `<workspace>/memory/` is replaced."""
    root, dest = Path(root), Path(workspace) / WORKSPACE_DIR
    if dest.exists():
        shutil.rmtree(dest)
    written: list[Path] = []
    for src, layer, out in (
        (fmt.org_path(root, org_key), fmt.ORG, fmt.org_path(dest, org_key)),
        (fmt.app_path(root, org_key, app_key), fmt.APP, fmt.app_path(dest, org_key, app_key)),
    ):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(injectable_copy(fmt.load(src, layer)), encoding="utf-8")
        written.append(out)
    if include_changes:
        src = fmt.changes_path(root, org_key, app_key)
        out = fmt.changes_path(dest, org_key, app_key)
        shutil.copyfile(src, out)
        written.append(out)
    return written
