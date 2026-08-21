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




def fetch_seeded_apk(app_id: str, apk: dict) -> Path:
    """Download + verify a seeded-bug APK declared inline in a benchmark spec.
    `apk` is the spec's `apk:` block: {repo, filename, sha256}."""
    from huggingface_hub import hf_hub_download

    repo = str(apk.get("repo") or "")
    filename = str(apk.get("filename") or "")
    sha = str(apk.get("sha256") or "")
    if not (repo and filename and sha):
        raise ValueError(
            f"{app_id}: incomplete `apk:` block in its benchmark spec — "
            "needs repo, filename and sha256.")

    cache = _cache_root() / "seeded" / app_id / Path(filename).name
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
