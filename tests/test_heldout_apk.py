"""A held-out app's APK is read in place from the synced split: `apk: {path, sha256}`.
Never downloaded, never resolved against the repository, never a silent fallback."""

from __future__ import annotations

import hashlib

import pytest

from qualgentbench import apps, corpus, journey, preflight


def _apk(tmp_path, body=b"PK\x03\x04held-out"):
    d = tmp_path / "heldout" / "apks" / "journey"
    d.mkdir(parents=True)
    f = d / "demo-buggy.apk"
    f.write_bytes(body)
    return tmp_path / "heldout", f, hashlib.sha256(body).hexdigest()


def test_a_path_block_resolves_inside_the_heldout_dir_and_is_verified(tmp_path, monkeypatch):
    root, f, sha = _apk(tmp_path)
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(root))
    meta = {"path": "apks/journey/demo-buggy.apk", "sha256": sha}
    assert apps.fetch_seeded_apk("demo", meta, kind="journey") == f

    def no_download(*a, **k):
        raise AssertionError("a held-out APK must never be downloaded")
    monkeypatch.setattr("huggingface_hub.hf_hub_download", no_download)
    assert apps.fetch_seeded_apk("demo", meta) == f


def test_a_wrong_hash_fails_instead_of_running_another_build(tmp_path, monkeypatch):
    root, _, _ = _apk(tmp_path)
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(root))
    with pytest.raises(RuntimeError, match="sha256"):
        apps.fetch_seeded_apk("demo", {"path": "apks/journey/demo-buggy.apk", "sha256": "0" * 64})


def test_an_unsynced_split_or_unset_dir_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    with pytest.raises(RuntimeError, match="QGB_HELDOUT_DIR"):
        apps.fetch_seeded_apk("demo", {"path": "apks/x.apk", "sha256": "a" * 64})
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError, match="not fully"):
        apps.fetch_seeded_apk("demo", {"path": "apks/x.apk", "sha256": "a" * 64})
    with pytest.raises(ValueError, match="sha256"):
        apps.fetch_seeded_apk("demo", {"path": "apks/x.apk"})


def test_published_blocks_are_untouched_and_apk_meta_accepts_paths(tmp_path, monkeypatch):
    assert apps.heldout_apk_path("demo", {"repo": "r", "filename": "f", "sha256": "s"}) is None
    root, f, sha = _apk(tmp_path)
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(root))
    spec = {"apk": {"path": "apks/journey/demo-buggy.apk", "sha256": sha}}
    assert preflight.resolve_apk_offline({"id": "demo"}, spec, mode="hunt") == f
    monkeypatch.setattr(journey, "load_cases", lambda app: {"apk": spec["apk"]})
    assert journey.apk_meta("demo") == spec["apk"]
