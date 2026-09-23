"""Where each published APK lives on HuggingFace, by content: the pin manifest.

`apps.fetch_seeded_apk` downloads a build by a FIXED path (`journey/<app>-buggy.apk`,
`<tier>/<app>-buggy.apk`) and sha256-checks it against the committed `apk:` block. Every
upload overwrites that path, so without a pin every older commit becomes unreproducible
the moment the next build is published, and an upload and the merge that names its hash
have to land back to back. A HuggingFace dataset is a git repository, so every past
upload is still in its history; this manifest records WHICH revision holds which bytes.

`data/apk-pins.json` holds two maps, both keyed by sha256:

* `pins` — `{sha256: {repo, filename, revision}}`: a dataset commit whose `filename`
  holds exactly those bytes. A sha256 names content, so a pin is an immutable fact and
  the manifest accumulates every build ever published; `record_pin` never overwrites
  one. Written by `scripts/publish_apk.py --upload` (from the commit the upload
  returns, after the Hub confirms that revision serves the sha256) and by
  `scripts/apk_pins.py backfill` (read-only walk of the dataset's history). Never
  hand-edit a pin.
* `unpublished` — `{sha256: {app, kind, filename, note}}`: bytes a committed `apk:`
  block names that no owner has uploaded yet. `publish_apk.py --write` writes the mark
  and `--upload` removes it. It is the machine-checkable form of the `NOT YET
  PUBLISHED` comments in the test-case files.

The guard (`unaccounted`, run by `tests/test_apk_pins.py` and `scripts/apk_pins.py
check`) holds every committed `apk:` block to one of the two.

**Not a corpus input.** `corpus.CORPUS_GLOBS` covers `test-cases/*.yaml` and
`truth/journey-*.json`; this file sits beside them and matches neither. The block's
sha256 already fixes the measured artifact, and a pin is only its address, so recording
one must never move `corpus_version()`. Putting `revision:` into the `apk:` blocks
instead would have moved it for a bookkeeping change.

Held-out apps are never published (`apk: path:`), have no pin and no mark, and must not
be named here: `scripts/holdout.py verify` scans this file like every other file under
`data/`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

DATA_ROOT = Path(__file__).resolve().parent / "data"
PINS_PATH = DATA_ROOT / "apk-pins.json"

ABOUT = ("sha256 -> the HuggingFace dataset revision whose `filename` holds those bytes "
         "(src/qualgentbench/apk_pins.py). Not a corpus_version input. `pins` are written "
         "by scripts/publish_apk.py --upload and scripts/apk_pins.py backfill and are never "
         "overwritten; `unpublished` marks a committed apk: block whose bytes no owner has "
         "uploaded yet. Do not hand-edit a pin.")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def is_revision(value: str) -> bool:
    """A full 40-hex commit id — the only kind of revision that can pin anything (a
    branch or tag name moves)."""
    return bool(_REVISION.match(str(value or "")))


def lfs_sha256(entry: object) -> str | None:
    """The sha256 the Hub reports for a file entry (`list_repo_tree`/`get_paths_info`):
    its LFS oid, which is the sha256 of the file's bytes. None for a non-LFS file."""
    lfs = getattr(entry, "lfs", None)
    if lfs is None:
        return None
    sha = getattr(lfs, "sha256", None)
    if sha is None and isinstance(lfs, dict):
        sha = lfs.get("sha256") or lfs.get("oid")
    return str(sha) if sha else None


# ── the file ───────────────────────────────────────────────────────────────────

def empty() -> dict[str, Any]:
    return {"pins": {}, "unpublished": {}}


def load(path: Path | None = None) -> dict[str, Any]:
    """The manifest, or an empty one when the file does not exist."""
    p = path or PINS_PATH
    if not p.is_file():
        return empty()
    doc = json.loads(p.read_text())
    return {"pins": dict(doc.get("pins") or {}),
            "unpublished": dict(doc.get("unpublished") or {})}


def save(doc: dict[str, Any], path: Path | None = None) -> None:
    """Write with sorted keys and a trailing newline, so every change is a minimal diff."""
    p = path or PINS_PATH
    out = {"_about": ABOUT,
           "pins": {k: doc["pins"][k] for k in sorted(doc.get("pins") or {})},
           "unpublished": {k: doc["unpublished"][k]
                           for k in sorted(doc.get("unpublished") or {})}}
    p.write_text(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def pin_for(sha256: str, path: Path | None = None) -> dict[str, str] | None:
    """The address that holds these bytes, or None when the build has never been pinned."""
    pin = load(path)["pins"].get(str(sha256 or ""))
    return dict(pin) if pin else None


def unpublished_mark(sha256: str, path: Path | None = None) -> dict[str, str] | None:
    mark = load(path)["unpublished"].get(str(sha256 or ""))
    return dict(mark) if mark else None


# ── edits ──────────────────────────────────────────────────────────────────────

def record_pin(doc: dict[str, Any], sha256: str, *, repo: str, filename: str,
               revision: str) -> str:
    """Pin `sha256` to `repo:filename@revision` and drop its unpublished mark.

    Returns "added", or "kept" when a pin already exists: the first address that held
    the bytes is still a valid address for them, so a pin is never overwritten."""
    if not _SHA256.match(sha256):
        raise ValueError(f"not a sha256: {sha256!r}")
    if not is_revision(revision):
        raise ValueError(f"not a full 40-hex commit id: {revision!r} — a branch name "
                         f"moves, so it cannot pin anything")
    if not (repo and filename):
        raise ValueError("a pin needs repo and filename")
    doc.setdefault("unpublished", {}).pop(sha256, None)
    pins = doc.setdefault("pins", {})
    if sha256 in pins:
        return "kept"
    pins[sha256] = {"repo": repo, "filename": filename, "revision": revision}
    return "added"


def mark_unpublished(doc: dict[str, Any], sha256: str, *, app: str, kind: str,
                     filename: str, note: str) -> bool:
    """Mark bytes a committed block names as not yet uploaded. A pinned sha256 is already
    retrievable and is never marked; returns whether a mark was written."""
    if not _SHA256.match(sha256):
        raise ValueError(f"not a sha256: {sha256!r}")
    if sha256 in doc.get("pins", {}):
        return False
    doc.setdefault("unpublished", {})[sha256] = {
        "app": app, "kind": kind, "filename": filename, "note": note}
    return True


# ── the guard ──────────────────────────────────────────────────────────────────

def committed_blocks(data_root: Path | None = None) -> Iterator[dict[str, str]]:
    """Every PUBLISHED `apk:` block under `data_root` (default: the packaged corpus):
    each benchmark spec's (the hunt build) and each test-case file's (the journey
    build). A held-out block (`apk: path:`) is never published and is skipped."""
    root = data_root or DATA_ROOT
    for sub, kind in (("benchmarks", "hunt"), ("test-cases", "journey")):
        for p in sorted((root / sub).glob("*.yaml")):
            doc = yaml.safe_load(p.read_text()) or {}
            apk = doc.get("apk") if isinstance(doc, dict) else None
            if not isinstance(apk, dict) or apk.get("path"):
                continue
            yield {"app": p.stem, "kind": kind, "file": str(p.relative_to(root)),
                   "repo": str(apk.get("repo") or ""),
                   "filename": str(apk.get("filename") or ""),
                   "sha256": str(apk.get("sha256") or "")}


def status(block: dict[str, str], doc: dict[str, Any]) -> str:
    """"pinned", "unpublished" or "UNACCOUNTED" for one committed block."""
    sha = block["sha256"]
    if sha in doc.get("pins", {}):
        return "pinned"
    if sha in doc.get("unpublished", {}):
        return "unpublished"
    return "UNACCOUNTED"


def unaccounted(doc: dict[str, Any] | None = None,
                data_root: Path | None = None) -> list[dict[str, str]]:
    """Committed blocks that are neither pinned nor marked unpublished. Such a block is
    one upload away from being unreproducible: its bytes are either at the path's HEAD
    today (and gone after the next upload) or nowhere, and nothing says which."""
    doc = load() if doc is None else doc
    return [b for b in committed_blocks(data_root) if status(b, doc) == "UNACCOUNTED"]


def problems(doc: dict[str, Any] | None = None,
             data_root: Path | None = None) -> list[str]:
    """Everything the guard refuses: an unaccounted block, a malformed pin, and a sha256
    that is both pinned and marked unpublished (one of the two is false)."""
    doc = load() if doc is None else doc
    out = [f"{b['file']}: apk: block {b['sha256'][:12]}… ({b['filename']}) has no pin in "
           f"data/apk-pins.json and is not marked unpublished"
           for b in unaccounted(doc, data_root)]
    for sha, pin in sorted(doc.get("pins", {}).items()):
        if not _SHA256.match(sha):
            out.append(f"pin key {sha!r} is not a sha256")
        if not isinstance(pin, dict) or not (pin.get("repo") and pin.get("filename")):
            out.append(f"pin {sha[:12]}… needs repo and filename")
        elif not is_revision(str(pin.get("revision") or "")):
            out.append(f"pin {sha[:12]}… revision {pin.get('revision')!r} is not a 40-hex "
                       f"commit id")
    for sha in sorted(set(doc.get("pins", {})) & set(doc.get("unpublished", {}))):
        out.append(f"{sha[:12]}… is both pinned and marked unpublished")
    return out
