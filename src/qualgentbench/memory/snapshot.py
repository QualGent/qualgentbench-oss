"""Content-addressed memory snapshots, one per experiment stage and unit.

The hash is sha256 over the memory file set: for each file, in path order, its path
relative to the memory root, a NUL, its byte length, a NUL and its bytes. Paths are
part of the hash (a rename is a change); mtimes and permissions are not. It is written
`sha256:<64 hex>`.

Store layout (`freeze` / `stage_pointer`):

    <store>/objects/<hex>/manifest.json         {"format", "snapshot", "files": {rel: sha256}}
    <store>/objects/<hex>/memory/org/<org>.md
    <store>/objects/<hex>/memory/app/<org>/<app>.md[, <app>.changes.md]
    <store>/stages/<stage>/<unit>.json          {"stage", "unit", "snapshot", "parent", ...extra}

An object never changes once written (same bytes → same directory). A stage pointer
names which object a (stage, unit) reads; `unit` is the caller's key, for example
`<app>__<arm>__r<rep>` for a dynamic arm. `pack`/`unpack` move one snapshot as a
deterministic `.tar.gz` (a lab artifact), verified against its hash on the way in.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import format as fmt

MEMORY_GLOBS = ("org/*.md", "app/*/*.md")
MANIFEST = "manifest.json"
MEMORY_SUBDIR = "memory"
FORMAT = 1
_HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9._=+-]+$")


class SnapshotError(ValueError):
    pass


def memory_files(root: Path | str) -> list[str]:
    """Every memory file under `root`, as sorted relative POSIX paths."""
    root = Path(root)
    found = {p.relative_to(root).as_posix() for g in MEMORY_GLOBS for p in root.glob(g)
             if p.is_file()}
    return sorted(found)


def unit_files(org_key: str, app_key: str, *, include_changes: bool = False) -> list[str]:
    """The relative paths one render of (org, app) reads."""
    files = [fmt.org_path(Path(""), org_key).as_posix(),
             fmt.app_path(Path(""), org_key, app_key).as_posix()]
    if include_changes:
        files.append(fmt.changes_path(Path(""), org_key, app_key).as_posix())
    return sorted(files)


def _check_rel(rel: str) -> str:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise SnapshotError(f"unsafe snapshot path {rel!r}")
    return p.as_posix()


def hash_bytes(items: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(items):
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0")
        h.update(data)
    return f"sha256:{h.hexdigest()}"


def _read(root: Path, files: list[str] | None) -> list[tuple[str, bytes]]:
    rels = memory_files(root) if files is None else sorted({_check_rel(f) for f in files})
    if not rels:
        raise SnapshotError(f"no memory files under {root}")
    out = []
    for rel in rels:
        p = root / rel
        if not p.is_file():
            raise FileNotFoundError(f"snapshot file missing: {p}")
        out.append((rel, p.read_bytes()))
    return out


def snapshot_hash(root: Path | str, files: list[str] | None = None) -> str:
    """`sha256:<hex>` over `files` (relative paths) under `root`, or over every memory
    file there when `files` is None."""
    return hash_bytes(_read(Path(root), files))


def hex_of(snapshot: str) -> str:
    m = _HASH_RE.match(snapshot)
    if not m:
        raise SnapshotError(f"not a snapshot hash: {snapshot!r}")
    return m.group(1)


@dataclass(frozen=True)
class Snapshot:
    hash: str
    dir: Path            # <store>/objects/<hex>

    @property
    def memory_root(self) -> Path:
        return self.dir / MEMORY_SUBDIR


def _write_object(items: list[tuple[str, bytes]], store: Path) -> Snapshot:
    snap = hash_bytes(items)
    obj = store / "objects" / hex_of(snap)
    if (obj / MANIFEST).is_file():
        return Snapshot(snap, obj)
    tmp = obj.with_name(obj.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    for rel, data in items:
        p = tmp / MEMORY_SUBDIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    manifest = {"format": FORMAT, "snapshot": snap,
                "files": {rel: hashlib.sha256(d).hexdigest() for rel, d in sorted(items)}}
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if obj.exists():
        shutil.rmtree(obj)
    tmp.rename(obj)
    return Snapshot(snap, obj)


def freeze(src_root: Path | str, store: Path | str, *, files: list[str] | None = None,
           stage: str | None = None, unit: str = "all", parent: str | None = None,
           extra: dict | None = None) -> Snapshot:
    """Copy the memory files (all, or `files`) of `src_root` into the store as an
    object, and with `stage`, point `stages/<stage>/<unit>.json` at it."""
    store = Path(store)
    snap = _write_object(_read(Path(src_root), files), store)
    if stage is not None:
        write_stage_pointer(store, stage, unit, snap.hash, parent=parent, extra=extra)
    return snap


def write_stage_pointer(store: Path | str, stage: str, unit: str, snapshot: str, *,
                        parent: str | None = None, extra: dict | None = None) -> Path:
    for name, v in (("stage", stage), ("unit", unit)):
        if not _UNIT_RE.match(v):
            raise SnapshotError(f"{name} {v!r} must match {_UNIT_RE.pattern}")
    hex_of(snapshot)
    if parent is not None:
        hex_of(parent)
    body = {**(extra or {}), "stage": stage, "unit": unit, "snapshot": snapshot,
            "parent": parent}
    p = Path(store) / "stages" / stage / f"{unit}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return p


def stage_pointer(store: Path | str, stage: str, unit: str = "all") -> dict:
    p = Path(store) / "stages" / stage / f"{unit}.json"
    if not p.is_file():
        raise FileNotFoundError(f"no snapshot for stage {stage!r} unit {unit!r}: {p}")
    return json.loads(p.read_text())


def open_snapshot(store: Path | str, snapshot: str, *, verify: bool = True) -> Snapshot:
    """The stored object for `snapshot`; with `verify`, its files must hash to it."""
    obj = Path(store) / "objects" / hex_of(snapshot)
    if not (obj / MANIFEST).is_file():
        raise FileNotFoundError(f"snapshot {snapshot} not in {store}")
    snap = Snapshot(snapshot, obj)
    if verify:
        rels = list(json.loads((obj / MANIFEST).read_text())["files"])
        got = snapshot_hash(snap.memory_root, rels)
        if got != snapshot or memory_files(snap.memory_root) != sorted(rels):
            raise SnapshotError(f"snapshot {snapshot} is corrupt (files hash to {got})")
    return snap


def open_stage(store: Path | str, stage: str, unit: str = "all") -> Snapshot:
    return open_snapshot(store, stage_pointer(store, stage, unit)["snapshot"])


# ── transport ─────────────────────────────────────────────────────────────────

def pack(root: Path | str, files: list[str] | None = None) -> tuple[bytes, str]:
    """A deterministic `.tar.gz` of the memory files (paths relative to the memory
    root, mtime 0, uid/gid 0, mode 0644) and its snapshot hash."""
    items = _read(Path(root), files)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, data in sorted(items):
            info = tarfile.TarInfo(rel)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0, filename="") as f:
        f.write(raw.getvalue())
    return gz.getvalue(), hash_bytes(items)


def unpack(data: bytes, dest: Path | str, *, expect: str | None = None) -> str:
    """Extract a `pack` archive into `dest` (which must be empty or absent) and return
    its hash; with `expect`, refuse an archive that does not hash to it."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SnapshotError(f"{dest} is not empty")
    items: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                raise SnapshotError(f"archive member {m.name!r} is not a regular file")
            f = tar.extractfile(m)
            assert f is not None
            items.append((_check_rel(m.name), f.read()))
    got = hash_bytes(items)
    if expect is not None and got != expect:
        raise SnapshotError(f"archive hashes to {got}, expected {expect}")
    for rel, blob in items:
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    return got
