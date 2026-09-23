"""The APK pin manifest (`qualgentbench.apk_pins`, `data/apk-pins.json`, QUA-2770).

`fetch_seeded_apk` used to download every build from the dataset's HEAD, so every
upload made every older commit unreproducible. What is pinned here:

* a pinned sha256 is downloaded FROM ITS REVISION, and an unpinned one exactly as
  before (HEAD) — the fallback is what keeps pre-pin behaviour for any block the
  manifest does not know;
* the GUARD: every committed `apk:` block is pinned or explicitly marked unpublished,
  over synthetic data (it fails on an unpinned, unmarked block) and over the real corpus;
* the manifest is NOT a `corpus_version` input — recording a pin moves no board;
* `scripts/apk_pins.py backfill` pins the OLDEST revision holding each sha256 and never
  records an app this checkout does not carry.

HuggingFace is mocked throughout; nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from qualgentbench import apk_pins, apps, corpus

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("apk_pins_cli", ROOT / "scripts" / "apk_pins.py")
apk_pins_cli = importlib.util.module_from_spec(_spec)
sys.modules["apk_pins_cli"] = apk_pins_cli
_spec.loader.exec_module(apk_pins_cli)

BODY = b"PK\x03\x04 a seeded build"
SHA = hashlib.sha256(BODY).hexdigest()
REV = "a" * 40
BLOCK = {"repo": "qualgent/qualgentbench-apps", "filename": "journey/demo-buggy.apk",
         "sha256": SHA}


def _manifest(tmp_path, monkeypatch, pins=None, unpublished=None) -> Path:
    p = tmp_path / "apk-pins.json"
    apk_pins.save({"pins": pins or {}, "unpublished": unpublished or {}}, p)
    monkeypatch.setattr(apk_pins, "PINS_PATH", p)
    return p


def _fake_hub(monkeypatch, tmp_path, fail_first: Exception | None = None) -> list[dict]:
    """A stand-in for `hf_hub_download` that records every call and serves BODY."""
    monkeypatch.setenv("QGB_CACHE_DIR", str(tmp_path / "cache"))
    calls: list[dict] = []

    def download(**kw):
        calls.append(kw)
        if fail_first is not None and len(calls) == 1:
            raise fail_first
        out = Path(kw["local_dir"]) / Path(kw["filename"]).name
        out.write_bytes(BODY)
        return str(out)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    return calls


# ── fetch ──────────────────────────────────────────────────────────────────────

def test_a_pinned_build_is_downloaded_from_its_revision(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, pins={SHA: {
        "repo": "qualgent/qualgentbench-apps", "filename": "hard/demo-buggy.apk",
        "revision": REV}})
    calls = _fake_hub(monkeypatch, tmp_path)
    got = apps.fetch_seeded_apk("demo", BLOCK, kind="journey")
    assert got.read_bytes() == BODY
    assert len(calls) == 1
    assert calls[0]["revision"] == REV
    # The pin is an ADDRESS for the bytes: its own path, which may differ from the
    # block's (one build published under hard/ and journey/ is pinned once).
    assert calls[0]["filename"] == "hard/demo-buggy.apk"
    assert calls[0]["repo_id"] == "qualgent/qualgentbench-apps"


def test_an_unpinned_build_falls_back_to_the_paths_head(tmp_path, monkeypatch):
    other = "b" * 64
    _manifest(tmp_path, monkeypatch,
              pins={other: {"repo": "x/y", "filename": "hard/demo-buggy.apk",
                            "revision": REV}},
              unpublished={})
    calls = _fake_hub(monkeypatch, tmp_path)
    got = apps.fetch_seeded_apk("demo", BLOCK, kind="journey")
    assert got.read_bytes() == BODY
    assert len(calls) == 1
    assert calls[0].get("revision") is None
    assert calls[0]["repo_id"] == BLOCK["repo"] and calls[0]["filename"] == BLOCK["filename"]


def test_a_pin_the_hub_no_longer_serves_falls_back_to_head(tmp_path, monkeypatch):
    """A rewritten dataset history must not turn a pin into an outage: HEAD is tried,
    and the sha256 check still decides."""
    from huggingface_hub.utils import RevisionNotFoundError
    _manifest(tmp_path, monkeypatch, pins={SHA: {
        "repo": "qualgent/qualgentbench-apps", "filename": "hard/demo-buggy.apk",
        "revision": REV}})
    gone = RevisionNotFoundError(
        "gone", response=httpx.Response(404, request=httpx.Request("GET", "https://hf.co")))
    calls = _fake_hub(monkeypatch, tmp_path, fail_first=gone)
    assert apps.fetch_seeded_apk("demo", BLOCK, kind="journey").read_bytes() == BODY
    assert [c.get("revision") for c in calls] == [REV, None]
    assert calls[1]["filename"] == BLOCK["filename"]


def test_the_sha256_check_still_decides_on_a_pinned_download(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, pins={SHA: {
        "repo": "r/r", "filename": "hard/demo-buggy.apk", "revision": REV}})
    _fake_hub(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="sha256"):
        apps.fetch_seeded_apk("demo", {**BLOCK, "sha256": "c" * 64})


# ── the guard ──────────────────────────────────────────────────────────────────

def _data_root(tmp_path, blocks: dict[str, str]) -> Path:
    """A data tree whose test-case files carry the given `apk:` blocks (by app)."""
    root = tmp_path / "data"
    (root / "test-cases").mkdir(parents=True)
    (root / "benchmarks").mkdir()
    for app_id, apk in blocks.items():
        (root / "test-cases" / f"{app_id}.yaml").write_text(f"app: {app_id}\n{apk}\n")
    return root


def test_the_guard_fails_on_an_unpinned_unmarked_block(tmp_path):
    root = _data_root(tmp_path, {
        "demo": f"apk:\n  repo: r/r\n  filename: journey/demo-buggy.apk\n  sha256: {SHA}\n"})
    doc = apk_pins.empty()
    assert [b["app"] for b in apk_pins.unaccounted(doc, root)] == ["demo"]
    assert any("demo.yaml" in p and "no pin" in p for p in apk_pins.problems(doc, root))

    pinned = {"pins": {SHA: {"repo": "r/r", "filename": "journey/demo-buggy.apk",
                             "revision": REV}}, "unpublished": {}}
    assert apk_pins.problems(pinned, root) == []
    marked = {"pins": {}, "unpublished": {SHA: {"app": "demo", "kind": "journey",
                                                "filename": "x", "note": "n"}}}
    assert apk_pins.problems(marked, root) == []


def test_the_guard_skips_a_heldout_block_and_refuses_a_contradiction(tmp_path):
    root = _data_root(tmp_path, {"held": f"apk:\n  path: apks/held.apk\n  sha256: {SHA}\n"})
    assert apk_pins.problems(apk_pins.empty(), root) == []
    both = {"pins": {SHA: {"repo": "r", "filename": "f", "revision": REV}},
            "unpublished": {SHA: {"app": "a", "kind": "journey", "filename": "f", "note": ""}}}
    assert any("both pinned and marked" in p for p in apk_pins.problems(both, root))
    bad_rev = {"pins": {SHA: {"repo": "r", "filename": "f", "revision": "main"}},
               "unpublished": {}}
    assert any("40-hex" in p for p in apk_pins.problems(bad_rev, root))


def test_every_committed_block_is_pinned_or_marked_unpublished():
    """THE guard over the real corpus. A block that moves without a pin or a mark fails
    here: `publish_apk.py --write` writes the mark, `--upload` the pin."""
    assert apk_pins.problems() == []
    blocks = list(apk_pins.committed_blocks())
    assert blocks, "no committed apk: blocks found — the guard would check nothing"
    assert {b["kind"] for b in blocks} == {"hunt", "journey"}


def test_the_manifest_is_not_a_corpus_version_input(tmp_path):
    """A pin is an address, not a measurement: recording one must not move
    corpus_version(). Checked on a copy of the real data tree."""
    assert apk_pins.PINS_PATH.parent == corpus.PACKAGED
    assert apk_pins.PINS_PATH not in corpus.corpus_files(corpus.PACKAGED)
    copy = tmp_path / "data"
    shutil.copytree(corpus.PACKAGED, copy)
    before = corpus.version_of(copy)
    pins_copy = copy / apk_pins.PINS_PATH.name
    doc = apk_pins.load(pins_copy)
    apk_pins.record_pin(doc, "d" * 64, repo="r/r", filename="journey/x-buggy.apk",
                        revision=REV)
    apk_pins.save(doc, pins_copy)
    assert corpus.version_of(copy) == before


# ── edits ──────────────────────────────────────────────────────────────────────

def test_a_pin_is_never_overwritten_and_clears_the_unpublished_mark():
    doc = {"pins": {}, "unpublished": {SHA: {"app": "demo", "kind": "journey",
                                             "filename": "f", "note": "n"}}}
    assert apk_pins.record_pin(doc, SHA, repo="r", filename="hard/a.apk", revision=REV) == "added"
    assert SHA not in doc["unpublished"]
    assert apk_pins.record_pin(doc, SHA, repo="r", filename="journey/a.apk",
                               revision="b" * 40) == "kept"
    assert doc["pins"][SHA] == {"repo": "r", "filename": "hard/a.apk", "revision": REV}
    assert apk_pins.mark_unpublished(doc, SHA, app="a", kind="journey", filename="f",
                                     note="n") is False
    with pytest.raises(ValueError, match="40-hex"):
        apk_pins.record_pin(doc, "e" * 64, repo="r", filename="f", revision="main")


def test_save_is_sorted_and_round_trips(tmp_path):
    p = tmp_path / "pins.json"
    doc = {"pins": {"f" * 64: {"repo": "r", "filename": "f", "revision": REV},
                    "0" * 64: {"repo": "r", "filename": "g", "revision": REV}},
           "unpublished": {}}
    apk_pins.save(doc, p)
    raw = json.loads(p.read_text())
    assert list(raw["pins"]) == sorted(raw["pins"])
    assert apk_pins.load(p) == doc


# ── backfill ───────────────────────────────────────────────────────────────────

def _entry(path: str, sha: str | None):
    lfs = SimpleNamespace(sha256=sha) if sha else None
    return SimpleNamespace(path=path, lfs=lfs)


class _History:
    """A dataset with three commits, newest first as the Hub lists them."""

    def __init__(self, trees: list[tuple[str, list]]):
        self.trees = trees  # oldest first

    def list_repo_commits(self, repo, repo_type=None):
        return [SimpleNamespace(commit_id=c) for c, _ in reversed(self.trees)]

    def list_repo_tree(self, repo, repo_type=None, revision=None, recursive=False):
        return dict(self.trees)[revision]


def test_backfill_pins_the_oldest_revision_and_skips_apps_this_checkout_lacks():
    old, new = "1" * 64, "2" * 64
    c1, c2, c3 = "1" * 40, "2" * 40, "3" * 40
    api = _History([
        (c1, [_entry(".gitattributes", None), _entry("hard/demo-buggy.apk", old),
              _entry("hard/secret-buggy.apk", "9" * 64)]),
        (c2, [_entry("hard/demo-buggy.apk", old), _entry("journey/demo-buggy.apk", old)]),
        (c3, [_entry("hard/demo-buggy.apk", new), _entry("journey/demo-buggy.apk", old),
              _entry("archive/journey/demo-buggy-0123456789ab.apk", "3" * 64)]),
    ])
    doc = apk_pins.empty()
    report = apk_pins_cli.backfill(api, ["r/r"], doc, {"demo"}, out=lambda *_: None)
    assert doc["pins"][old] == {"repo": "r/r", "filename": "hard/demo-buggy.apk", "revision": c1}
    assert doc["pins"][new]["revision"] == c3
    assert doc["pins"]["3" * 64]["filename"].startswith("archive/")
    # An app with no spec or test-case file here is not recorded — a held-out app must
    # never be named under data/ (scripts/holdout.py verify scans the manifest).
    assert "9" * 64 not in doc["pins"]
    assert report["skipped"] == ["hard/secret-buggy.apk"]
    assert sorted(report["added"]) == sorted([old, new, "3" * 64])


def test_backfill_keeps_an_existing_pin():
    c1 = "1" * 40
    api = _History([(c1, [_entry("hard/demo-buggy.apk", SHA)])])
    doc = {"pins": {SHA: {"repo": "r/r", "filename": "journey/demo-buggy.apk",
                          "revision": REV}}, "unpublished": {}}
    report = apk_pins_cli.backfill(api, ["r/r"], doc, {"demo"}, out=lambda *_: None)
    assert report["kept"] == [SHA] and doc["pins"][SHA]["revision"] == REV


def test_the_apk_name_parser_reads_live_and_archive_paths():
    assert apk_pins_cli.app_of("journey/fossify-calendar-buggy.apk") == "fossify-calendar"
    assert apk_pins_cli.app_of("archive/journey/fossify-calendar-buggy-5cea276f7456.apk") \
        == "fossify-calendar"
    assert apk_pins_cli.app_of(".gitattributes") is None


def test_fetch_resolves_a_quoted_prefix():
    doc = {"pins": {SHA: {"repo": "r", "filename": "f", "revision": REV}}, "unpublished": {}}
    assert apk_pins_cli.resolve_sha(SHA[:8] + "…", doc) == SHA
    with pytest.raises(SystemExit, match="no pin"):
        apk_pins_cli.resolve_sha("deadbeef", doc)


def test_a_download_failure_on_an_unpublished_build_says_so(tmp_path, monkeypatch):
    """The one sha256 failure whose cause is KNOWN must not send the reader to fix a
    token or edit the hash."""
    from qualgentbench import cli
    _manifest(tmp_path, monkeypatch, unpublished={SHA: {
        "app": "demo", "kind": "journey", "filename": "journey/demo-buggy.apk", "note": "n"}})
    msg = cli._apk_download_help("demo", BLOCK, RuntimeError("failed its sha256 check"))
    assert "NOT YET PUBLISHED" in msg and "did not match the checksum" not in msg
    other = cli._apk_download_help("demo", {**BLOCK, "sha256": "f" * 64},
                                   RuntimeError("failed its sha256 check"))
    assert "NOT YET PUBLISHED" not in other and "did not match the checksum" in other
