"""Seeded-bug APK download, verification and caching."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Cache location ─────────────────────────────────────────────────────────

def _cache_root() -> Path:
    override = os.environ.get("QGB_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "qualgentbench" / "apps"



def _verify_sha256(path: Path, expected: str) -> bool:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        logger.warning(
            "sha256 mismatch for %s: expected %s, got %s",
            path.name, expected, actual,
        )
        return False
    return True




def heldout_apk_path(app_id: str, apk: dict) -> Path | None:
    """Where an `apk:` block with a `path:` points, or None for a published build.

    A held-out app's APKs are never published: its `apk:` block carries `path:`
    (relative to the held-out directory, docs/heldout.md) instead of `repo:` +
    `filename:`. The path is resolved against `QGB_HELDOUT_DIR` — never against the
    repository, and never by falling back to a public download, which would quietly
    run a stale or public build under a held-out label."""
    raw = str(apk.get("path") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    from .corpus import HELDOUT_ENV, heldout_dir
    base = heldout_dir()
    if base is None:
        raise RuntimeError(
            f"{app_id}: its APK is held out (`apk: path: {raw}`) but {HELDOUT_ENV} is not "
            f"set. Sync the held-out split and point {HELDOUT_ENV} at it (docs/heldout.md).")
    return base / p


def fetch_seeded_apk(app_id: str, apk: dict, kind: str = "seeded") -> Path:
    """Download + verify a seeded-bug APK declared inline in a benchmark spec.
    `apk` is the spec's `apk:` block: {repo, filename, sha256}. `kind` names the
    cache slot — "journey" for the journey-mode build published under journey/,
    which shares a file name with the hunt build and must not overwrite it.
    A held-out block ({path, sha256}) is read from the held-out directory and
    verified in place; it is never downloaded."""
    held = heldout_apk_path(app_id, apk)
    if held is not None:
        sha = str(apk.get("sha256") or "")
        if not sha:
            raise ValueError(f"{app_id}: held-out `apk:` block needs sha256 beside path.")
        if not held.exists():
            raise FileNotFoundError(
                f"{app_id}: held-out APK not found at {held} — the split is not fully "
                f"synced (docs/heldout.md).")
        if not _verify_sha256(held, sha):
            raise RuntimeError(
                f"{app_id}: held-out APK at {held} failed its sha256 check "
                f"(expected {sha[:16]}…). Re-sync the split; do not edit the hash to fit.")
        return held

    from huggingface_hub import hf_hub_download

    repo = str(apk.get("repo") or "")
    filename = str(apk.get("filename") or "")
    sha = str(apk.get("sha256") or "")
    if not (repo and filename and sha):
        raise ValueError(
            f"{app_id}: incomplete `apk:` block in its benchmark spec — "
            "needs repo, filename and sha256.")

    cache = _cache_root() / kind / app_id / Path(filename).name
    if cache.exists() and _verify_sha256(cache, sha):
        return cache
    if cache.exists():
        logger.warning("corrupted cache entry — re-downloading: %s", cache)
        cache.unlink()

    cache.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("HF_TOKEN") or None
    logger.info("downloading %s from HuggingFace %s", filename, repo)
    with tempfile.TemporaryDirectory() as tmp:
        got = hf_hub_download(repo_id=repo, filename=filename, repo_type="dataset",
                              token=token, local_dir=tmp)
        shutil.copy2(got, cache)

    if not _verify_sha256(cache, sha):
        cache.unlink(missing_ok=True)
        raise RuntimeError(
            f"{app_id}: downloaded APK failed its sha256 check. Either the published "
            f"file changed without the spec being updated, or the download was "
            f"truncated. Expected {sha[:16]}…")
    return cache
