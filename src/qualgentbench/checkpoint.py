"""Checkpoint primitives: what identifies an episode and what a run was run against.

Two facts a run dir has to carry before it can survive an interruption or a move to
another machine:

* **Which run an episode dir belongs to.** The dir name is
  ``<ts>_<task>_<agent>_<model>_<cond>_trial-<n>`` — no run id, and an episode killed
  mid-flight leaves no ``result.json``, so it is indistinguishable from one that never
  started. ``episode.json``, written the moment the dir exists, closes both gaps.
* **What the run was run against.** ``plan.json`` records agent/model/mode/scope but
  nothing about the harness or the apps, so a resume elsewhere cannot tell whether it
  would be measuring the same thing. ``environment_fingerprint`` is that missing half.

Nothing here scores anything; it is identity and provenance only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

# Written into every episode dir at episode start; scanned by run id on resume.
EPISODE_MARKER = "episode.json"

# Bump when the marker/fingerprint shape changes incompatibly, so a resume against an
# older runs tree fails loudly instead of comparing fields that no longer mean the same.
SCHEMA_VERSION = 1


def package_version() -> str:
    """Installed harness version — the dist metadata, not the module constant, since
    that is what a container image actually shipped."""
    try:
        from importlib.metadata import version

        return version("qualgentbench")
    except Exception:  # noqa: BLE001 — a missing dist must not fail a run
        from . import __version__

        return __version__


def image_digest() -> str | None:
    """Digest of the container image this run is executing in, when the launcher set
    it (``QGB_IMAGE_DIGEST``). ``None`` on a bare host — that is a fact, not a gap."""
    return os.environ.get("QGB_IMAGE_DIGEST") or None


# ── episode marker ────────────────────────────────────────────────────────────


def episode_marker(
    *,
    run_id: str,
    app_id: str,
    task_id: str,
    kind: str,
    trial: int,
    attempt: int = 1,
    segment: int = 0,
    started_at: str | datetime | None = None,
) -> dict[str, Any]:
    """The marker payload. Keys are the unit identity (`app_id`, `task_id`, `trial`)
    plus the attempt/segment that produced *this* directory, so a resume can tell an
    orphan of its own run from one left by an earlier segment."""
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    if isinstance(started_at, datetime):
        started_at = started_at.isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id or "",
        "app_id": app_id or "",
        "task_id": task_id or "",
        "kind": kind or "",
        "trial": int(trial),
        "attempt": int(attempt),
        "segment": int(segment),
        "started_at": str(started_at),
    }


def write_episode_marker(episode_dir: Path, **fields: Any) -> Path | None:
    """Write ``<episode_dir>/episode.json``. Best-effort: the marker is for the *next*
    run's benefit, so a full disk must not cost this episode."""
    path = Path(episode_dir) / EPISODE_MARKER
    try:
        path.write_text(json.dumps(episode_marker(**fields), indent=2))
    except OSError as exc:
        logger.warning("%s not written: %s", EPISODE_MARKER, exc)
        return None
    return path


def read_episode_marker(episode_dir: Path) -> dict[str, Any] | None:
    """The marker for an episode dir, or ``None`` when it has none (never started, or
    written by a harness older than this one)."""
    try:
        data = json.loads((Path(episode_dir) / EPISODE_MARKER).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── environment fingerprint ───────────────────────────────────────────────────


def spec_version(suite: Mapping[str, Any]) -> str:
    """Content identity of one app's benchmark spec.

    Specs carry no declared version, so it is derived: a sha256 over the *loaded*
    spec in canonical form. Hashing the parsed document rather than the file bytes
    means a comment or a reflow does not invalidate a resume, while any change to a
    feature, task or oracle does. A declared ``app.spec_version`` wins if one is ever
    added.
    """
    declared = (suite.get("app") or {}).get("spec_version") if isinstance(suite, Mapping) else None
    if declared:
        return str(declared)
    blob = json.dumps(suite, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def environment_fingerprint(
    suites: Iterable[Mapping[str, Any]],
    *,
    apk_sha256: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """What this run is being measured against: harness version, image digest, and per
    app the spec content hash and the APK hash.

    ``apk_sha256`` is supplied by the caller because which APK an app resolves to
    depends on the mode (journey mode runs a different build); ``None`` for an app
    means the APK has no published hash (a local build or a ``QUALGENTBENCH_APK_*``
    override), and a resume cannot prove the two machines ran the same bytes.

    TODO(QUA-2694): journey mode's per-case files (``journey.load_cases``) are not
    folded into ``spec_version`` — editing a journey case between segments will not
    be caught by the compatibility check. Fold them in when resume lands.
    """
    apk_sha256 = apk_sha256 or {}
    apps: dict[str, dict[str, Any]] = {}
    for suite in suites:
        app_id = str((suite.get("app") or {}).get("id", "")) if isinstance(suite, Mapping) else ""
        if not app_id:
            continue
        sha = apk_sha256.get(app_id)
        if sha is None:
            sha = ((suite.get("apk") or {}) or {}).get("sha256")
        apps[app_id] = {
            "spec_version": spec_version(suite),
            "apk_sha256": str(sha) if sha else None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "package_version": package_version(),
        "image_digest": image_digest(),
        "apps": apps,
    }
