"""QUA-3483: snapshot hashes are stable, change with any entry, and the store
round-trips."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from qualgentbench.memory import format as fmt
from qualgentbench.memory import snapshot as sn

FIX = Path(__file__).parent / "fixtures" / "memory"
ORG, APP = "samplecorp", "sampleapp"
APP_REL = f"app/{ORG}/{APP}.md"


@pytest.fixture
def good(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    shutil.copytree(FIX / "good", root)
    return root


def test_hash_format_and_definition(good: Path) -> None:
    h = sn.snapshot_hash(good)
    assert h.startswith("sha256:") and len(sn.hex_of(h)) == 64
    # The documented definition, spelled out independently.
    d = hashlib.sha256()
    for rel in sorted([f"org/{ORG}.md", APP_REL, f"app/{ORG}/{APP}.changes.md"]):
        data = (good / rel).read_bytes()
        d.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    assert h == f"sha256:{d.hexdigest()}"
    assert sn.memory_files(good) == sorted([f"app/{ORG}/{APP}.changes.md", APP_REL,
                                             f"org/{ORG}.md"])


def test_hash_is_stable_across_processes_copies_and_mtimes(good: Path, tmp_path: Path) -> None:
    h = sn.snapshot_hash(good)
    out = subprocess.run(
        [sys.executable, "-c",
         ("import sys; from qualgentbench.memory.snapshot import snapshot_hash; "
          "print(snapshot_hash(sys.argv[1]))"), str(good)],
        capture_output=True, text=True, check=True)
    assert out.stdout.strip() == h
    copy = tmp_path / "copy"
    shutil.copytree(good, copy, copy_function=shutil.copy)  # fresh mtimes
    assert sn.snapshot_hash(copy) == h
    # Unrelated files beside the memory tree do not count.
    (good / "README.txt").write_text("not memory")
    (good / "traps").mkdir()
    (good / "traps/t.yaml").write_text("x: 1")
    assert sn.snapshot_hash(good) == h


@pytest.mark.parametrize("edit", [
    lambda d: d.bump("nav-001", seen=1),
    lambda d: d.bump("flaky-001", contra=1),
    lambda d: d.update_entry("note-001", text="The editor saves when left."),
    lambda d: d.add_entry("bug", "An old export bug was fixed.", {"src": "curator"}),
    lambda d: d.invalidate("setup-001", "gone"),
    lambda d: d.update_entry("nav-003", confirmed="2026-11-17"),
])
def test_hash_changes_when_any_entry_changes(good: Path, edit) -> None:
    before = sn.snapshot_hash(good, sn.unit_files(ORG, APP))
    doc = fmt.load(good / APP_REL)
    edit(doc)
    doc.save(good / APP_REL)
    assert sn.snapshot_hash(good, sn.unit_files(ORG, APP)) != before


def test_hash_changes_on_rename_and_on_the_invalidated_section(good: Path) -> None:
    h = sn.snapshot_hash(good)
    raw = (good / APP_REL).read_text()
    (good / APP_REL).write_text(raw.replace("reason=moved", "reason=Moved"))
    assert sn.snapshot_hash(good) != h
    (good / APP_REL).write_text(raw)
    assert sn.snapshot_hash(good) == h
    (good / "org" / f"{ORG}.md").rename(good / "org" / "renamed.md")
    assert sn.snapshot_hash(good) != h


def test_unit_files_and_missing_files(good: Path) -> None:
    assert sn.unit_files(ORG, APP) == [APP_REL, f"org/{ORG}.md"]
    assert sn.unit_files(ORG, APP, include_changes=True)[0] == f"app/{ORG}/{APP}.changes.md"
    with pytest.raises(FileNotFoundError):
        sn.snapshot_hash(good, sn.unit_files(ORG, "nope"))
    with pytest.raises(sn.SnapshotError):
        sn.snapshot_hash(good, ["../escape.md"])
    with pytest.raises(sn.SnapshotError):
        sn.snapshot_hash(good / "empty-dir-that-does-not-exist")


def test_freeze_store_layout_and_stage_pointers(good: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    files = sn.unit_files(ORG, APP)
    s1 = sn.freeze(good, store, files=files, stage="run-1", unit="sampleapp__D-small__r1")
    assert s1.hash == sn.snapshot_hash(good, files)
    assert s1.dir == store / "objects" / sn.hex_of(s1.hash)
    assert sorted(p.relative_to(s1.dir).as_posix() for p in s1.dir.rglob("*") if p.is_file()) \
        == ["manifest.json", f"memory/{APP_REL}", f"memory/org/{ORG}.md"]
    manifest = json.loads((s1.dir / "manifest.json").read_text())
    assert manifest["snapshot"] == s1.hash and set(manifest["files"]) == set(files)

    # Same bytes, another stage → same object; pointer records the lineage.
    doc = fmt.load(good / APP_REL)
    doc.bump("nav-001", seen=1)
    doc.save(good / APP_REL)
    s2 = sn.freeze(good, store, files=files, stage="curate-1", unit="sampleapp__D-small__r1",
                   parent=s1.hash, extra={"ops": 1})
    assert s2.hash != s1.hash
    ptr = sn.stage_pointer(store, "curate-1", "sampleapp__D-small__r1")
    assert ptr == {"stage": "curate-1", "unit": "sampleapp__D-small__r1",
                   "snapshot": s2.hash, "parent": s1.hash, "ops": 1}
    assert sn.open_stage(store, "run-1", "sampleapp__D-small__r1").hash == s1.hash
    again = sn.freeze(sn.open_snapshot(store, s1.hash).memory_root, store, files=files)
    assert again == s1
    with pytest.raises(FileNotFoundError):
        sn.stage_pointer(store, "run-2", "sampleapp__D-small__r1")
    with pytest.raises(sn.SnapshotError):
        sn.write_stage_pointer(store, "run 1", "x", s1.hash)


def test_open_snapshot_detects_corruption(good: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    s = sn.freeze(good, store)
    (s.memory_root / APP_REL).write_text("tampered\n")
    with pytest.raises(sn.SnapshotError):
        sn.open_snapshot(store, s.hash)
    assert sn.open_snapshot(store, s.hash, verify=False).hash == s.hash


def test_pack_is_deterministic_and_unpack_verifies(good: Path, tmp_path: Path) -> None:
    files = sn.unit_files(ORG, APP, include_changes=True)
    blob, h = sn.pack(good, files)
    blob2, h2 = sn.pack(good, files)
    assert blob == blob2 and h == h2 == sn.snapshot_hash(good, files)
    dest = tmp_path / "out"
    assert sn.unpack(blob, dest, expect=h) == h
    assert sn.snapshot_hash(dest) == h
    with pytest.raises(sn.SnapshotError):
        sn.unpack(blob, dest)  # not empty
    with pytest.raises(sn.SnapshotError):
        sn.unpack(blob, tmp_path / "out2", expect="sha256:" + "0" * 64)
    assert not (tmp_path / "out2").exists()
