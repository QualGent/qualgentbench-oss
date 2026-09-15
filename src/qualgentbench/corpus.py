"""Corpus identity and the held-out split.

Two facts every journey number has to carry:

* **Which corpus produced it.** The journey key is a set of files (test cases, measured
  truth, the `apk:` block inside each case file). Edit one byte of one of them and the
  board is a different measurement — a symptom word added, a marker changed, a case
  dropped. `corpus_version()` is a short content hash over exactly those files, stamped
  into every episode result, every summary row and every run manifest, so two boards
  can be told apart before they are compared.

* **Whether the app was public.** A public corpus is trainable-on. The held-out split is
  two of the eight journey apps whose files live OUTSIDE the repository — in the
  directory `QGB_HELDOUT_DIR` names (or `heldout/` beside the repo root, gitignored) —
  with the same layout as the packaged `data/` tree. Every loader here resolves the
  held-out directory FIRST, then the packaged data, so a held-out app runs exactly like
  a public one; its episodes carry `heldout: true` and the board prints them as their
  own block, never blended into the public row. `heldout_version()` is the same hash
  over that directory.

What constitutes "an app" for hold-out purposes (`app_files`): the test-case file, the
journey truth file, the benchmark spec (identity, `device_setup`, and the seeded-defect
PATCHES — the spec is where every defect is defined, so it is part of the key), and the
spec's `device_setup.push` sources under `assets/`. All of them leave; the app also
leaves the hunt tier by construction. The APKs stay on HuggingFace; see docs/heldout.md
for why a held-out app's journey build should sit in a private repo.

Nothing in this module names which apps are held out. The repository must not carry
that list: it is the one fact the split exists to keep off the public record.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

import yaml

HELDOUT_ENV = "QGB_HELDOUT_DIR"
# The conventional local location, ignored by .gitignore so it cannot be committed by
# accident. `scripts/holdout.py move` defaults to it when the env var is unset.
DEFAULT_HELDOUT_DIRNAME = "heldout"

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGED = Path(__file__).resolve().parent / "data"

# The files that define the journey corpus, relative to a data root. Sorted by path,
# path included in the hash, so a rename is a change too. The `apk:` blocks live inside
# the test-case files and are covered by the first glob.
CORPUS_GLOBS = ("test-cases/*.yaml", "truth/journey-*.json")
VERSION_HEX = 12


# ── where things are ───────────────────────────────────────────────────────────

def heldout_dir() -> Path | None:
    """The held-out data root, or None when no split is configured."""
    raw = os.environ.get(HELDOUT_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def default_heldout_dir(repo_root: Path | None = None) -> Path:
    return (repo_root or REPO_ROOT) / DEFAULT_HELDOUT_DIRNAME


def is_heldout(app_id: str) -> bool:
    """An app is held out iff its TEST-CASE file lives in the held-out directory. One
    definition, used by every loader and by the episode stamp."""
    d = heldout_dir()
    return bool(d and (d / "test-cases" / f"{app_id}.yaml").is_file())


def resolve(rel: str, *, app_id: str | None = None) -> Path:
    """The path for a data-relative file: held-out first, then packaged.

    With `app_id`, a file of a held-out app resolves into the held-out directory even
    when it does not exist there yet — so a truth file DERIVED for a held-out app is
    written beside its cases, never back into the repository."""
    d = heldout_dir()
    if d:
        candidate = d / rel
        if candidate.exists() or (app_id and is_heldout(app_id)):
            return candidate
    return PACKAGED / rel


def asset_path(src: str) -> Path:
    """A `device_setup.push` source: under the held-out root when it exists there,
    else under the repository root (the packaged `assets/` tree)."""
    d = heldout_dir()
    if d and (d / src).exists():
        return (d / src).resolve()
    return (REPO_ROOT / src).resolve()


def apps_in(root: Path | None) -> list[str]:
    """App ids with a test-case file under `root`."""
    if not root or not (root / "test-cases").is_dir():
        return []
    return sorted(p.stem for p in (root / "test-cases").glob("*.yaml"))


def heldout_apps() -> list[str]:
    return apps_in(heldout_dir())


def public_apps() -> list[str]:
    return apps_in(PACKAGED)


# ── the version ────────────────────────────────────────────────────────────────

def corpus_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for pattern in CORPUS_GLOBS:
        out.extend(p for p in root.glob(pattern) if p.is_file())
    return sorted(set(out), key=lambda p: p.relative_to(root).as_posix())


def version_of(root: Path) -> str | None:
    """First 12 hex of sha256 over (relative path, bytes) of every corpus file under
    `root`, in path order. Bytes only — mtime, permissions and the absolute location
    do not enter. None when the root holds no corpus file at all."""
    files = corpus_files(root) if root.is_dir() else []
    if not files:
        return None
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:VERSION_HEX]


def corpus_version() -> str:
    """The public corpus. A packaged tree with no corpus files hashes as "empty" so the
    stamp is always a string."""
    return version_of(PACKAGED) or "empty"


def heldout_version() -> str | None:
    d = heldout_dir()
    return version_of(d) if d else None


def stamp() -> dict[str, Any]:
    """The two keys every result, summary row and run manifest carries."""
    return {"corpus_version": corpus_version(), "heldout_version": heldout_version()}


def episode_stamp(app_id: str) -> dict[str, Any]:
    return {**stamp(), "heldout": is_heldout(app_id)}


# ── what an app is ─────────────────────────────────────────────────────────────

def app_files(app_id: str, data_root: Path, repo_root: Path) -> dict[str, Path]:
    """Every file that constitutes `app_id` for hold-out purposes, keyed by its path
    relative to the root it moves under (data files relative to the data root, assets
    relative to the repository root — the held-out directory mirrors both).

    Existence is not required: the caller decides what a missing piece means."""
    files = {
        f"test-cases/{app_id}.yaml": data_root / "test-cases" / f"{app_id}.yaml",
        f"truth/journey-{app_id}.json": data_root / "truth" / f"journey-{app_id}.json",
        f"benchmarks/{app_id}.yaml": data_root / "benchmarks" / f"{app_id}.yaml",
    }
    spec_path = files[f"benchmarks/{app_id}.yaml"]
    for src in push_sources(spec_path):
        files[src] = repo_root / src
    return files


def push_sources(spec_path: Path) -> list[str]:
    """The `device_setup.push[].src` entries of a benchmark spec, as written."""
    if not spec_path.is_file():
        return []
    try:
        doc = yaml.safe_load(spec_path.read_text()) or {}
    except yaml.YAMLError:
        return []
    setup = doc.get("device_setup") or {}
    return [str(item["src"]) for item in setup.get("push", []) or []
            if isinstance(item, dict) and item.get("src")]


def shared_push_sources(app_id: str, data_root: Path) -> set[str]:
    """Push sources `app_id` shares with another spec under `data_root` — an asset
    referenced by two apps must be COPIED out, never removed."""
    mine = set(push_sources(data_root / "benchmarks" / f"{app_id}.yaml"))
    if not mine:
        return set()
    others: set[str] = set()
    for p in (data_root / "benchmarks").glob("*.yaml"):
        if p.stem != app_id:
            others.update(push_sources(p))
    return mine & others


# ── summary helpers ────────────────────────────────────────────────────────────

def distinct_versions(metrics: Iterable[dict], key: str) -> tuple[str | None, list[str], int]:
    """(single version or None, sorted distinct versions, count of unstamped episodes)
    over a row's episode metrics. `single` is set only when every episode carries the
    same version — a row mixing versions, or mixing stamped and unstamped episodes,
    is not one measurement."""
    seen: set[str] = set()
    unstamped = 0
    for m in metrics:
        v = m.get(key)
        if v:
            seen.add(str(v))
        else:
            unstamped += 1
    versions = sorted(seen)
    single = versions[0] if len(versions) == 1 and unstamped == 0 else None
    return single, versions, unstamped


def is_mixed(versions: list[str], unstamped: int) -> bool:
    return len(versions) > 1 or (bool(versions) and unstamped > 0)
