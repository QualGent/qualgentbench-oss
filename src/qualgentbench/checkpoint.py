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
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .failures import is_excluded

logger = logging.getLogger(__name__)

# Written into every episode dir at episode start; scanned by run id on resume.
EPISODE_MARKER = "episode.json"
RESULT_FILE = "result.json"
PLAN_FILE = "plan.json"
# Per-run metadata (plan.json, schedule.jsonl, board.json) and the quarantine an
# interrupted episode is moved to. Both are leading-underscore siblings of the
# per-task episode dirs so `runs/*/*/result.json` never sees them.
RUN_META_DIR = "_runs"
DISCARDED_DIR = "_discarded"

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


def spec_version(suite: Mapping[str, Any], extra: Any = None) -> str:
    """Content identity of one app's benchmark spec.

    Specs carry no declared version, so it is derived: a sha256 over the *loaded*
    spec in canonical form. Hashing the parsed document rather than the file bytes
    means a comment or a reflow does not invalidate a resume, while any change to a
    feature, task or oracle does. A declared ``app.spec_version`` wins if one is ever
    added.

    ``extra`` is anything else that defines the same app's units in the current mode
    but lives outside the spec file — journey mode's test-case and truth documents,
    which is where its tasks and oracles actually come from. Folded into the same
    hash so one value answers "is this still the same benchmark".
    """
    declared = (suite.get("app") or {}).get("spec_version") if isinstance(suite, Mapping) else None
    if declared:
        return str(declared)
    payload: Any = suite if extra in (None, {}, []) else {"spec": suite, "extra": extra}
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def environment_fingerprint(
    suites: Iterable[Mapping[str, Any]],
    *,
    apk_sha256: Mapping[str, str | None] | None = None,
    spec_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What this run is being measured against: harness version, image digest, and per
    app the spec content hash and the APK hash.

    ``apk_sha256`` is supplied by the caller because which APK an app resolves to
    depends on the mode (journey mode runs a different build); ``None`` for an app
    means the APK has no published hash (a local build or a ``QUALGENTBENCH_APK_*``
    override), and a resume cannot prove the two machines ran the same bytes.

    ``spec_extra`` is the same story for the spec side: in journey mode an app's
    tasks and oracles come from its test-case and truth documents, not from the
    benchmark spec, so the caller folds those in (see ``spec_version``). Off by
    default because guided and hunt mode read the spec alone.
    """
    apk_sha256 = apk_sha256 or {}
    spec_extra = spec_extra or {}
    apps: dict[str, dict[str, Any]] = {}
    for suite in suites:
        app_id = str((suite.get("app") or {}).get("id", "")) if isinstance(suite, Mapping) else ""
        if not app_id:
            continue
        sha = apk_sha256.get(app_id)
        if sha is None:
            sha = ((suite.get("apk") or {}) or {}).get("sha256")
        apps[app_id] = {
            "spec_version": spec_version(suite, spec_extra.get(app_id)),
            "apk_sha256": str(sha) if sha else None,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "package_version": package_version(),
        "image_digest": image_digest(),
        "apps": apps,
    }


# ── completion state ──────────────────────────────────────────────────────────

# (app_id, task_id, trial) — the same identity `scheduler.Unit.key` uses.
UnitKey = tuple[str, str, int]


class CheckpointError(RuntimeError):
    """A resume cannot proceed: no plan on disk, an unreadable one, or one written
    against a different environment. Carries the message a user needs to act on."""


@dataclass(frozen=True)
class EpisodeRef:
    """One episode dir on disk and the unit it belongs to."""
    key: UnitKey
    path: Path


@dataclass
class CheckpointState:
    """What a run id has already produced under `runs_dir`.

    `done` is the set a resume subtracts from the plan. An excluded attempt is NOT
    done — a rate-limited or infra-failed episode measured nothing, so its unit is
    still owed — but its dir stays on disk as evidence.
    """
    run_id: str
    done: set[UnitKey] = field(default_factory=set)
    excluded: list[EpisodeRef] = field(default_factory=list)
    orphans: list[EpisodeRef] = field(default_factory=list)

    def is_done(self, app_id: str, task_id: str, trial: int) -> bool:
        """An episode written before `episode.json` existed cannot name its app, so
        a result with no marker is keyed on the task alone; match either form rather
        than re-running work that is demonstrably finished."""
        return ((app_id, task_id, int(trial)) in self.done
                or ("", task_id, int(trial)) in self.done)


def episode_dirs(runs_dir: Path | str) -> list[Path]:
    """Every `runs/<task_id>/<episode>/`. Leading-underscore top-level dirs are the
    harness's own (`_runs`, `_discarded`), never episodes."""
    out: list[Path] = []
    try:
        tasks = sorted(p for p in Path(runs_dir).iterdir()
                       if p.is_dir() and not p.name.startswith("_"))
    except OSError:
        return out
    for task_dir in tasks:
        try:
            out.extend(sorted(p for p in task_dir.iterdir() if p.is_dir()))
        except OSError:
            continue
    return out


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _marker_key(marker: Mapping[str, Any]) -> UnitKey:
    try:
        trial = int(marker.get("trial") or 0)
    except (TypeError, ValueError):
        trial = 0
    return (str(marker.get("app_id") or ""), str(marker.get("task_id") or ""), trial)


def state(runs_dir: Path | str, run_id: str) -> CheckpointState:
    """Completion state for one run id: which units are finished, which attempts are
    excluded, and which episode dirs were interrupted (marked for this run, no
    `result.json`).

    Reads only the run dir — no plan needed — so it also answers "what did this run
    get through" for a tree that arrived from another machine.
    """
    st = CheckpointState(run_id=run_id)
    for episode in episode_dirs(runs_dir):
        marker = read_episode_marker(episode)
        result = _read_json(episode / RESULT_FILE)
        if result is None:
            # No result: an orphan if this run started it, otherwise not ours to judge
            # (an episode dir from an older harness cannot name its run at all).
            if marker and str(marker.get("run_id") or "") == run_id:
                st.orphans.append(EpisodeRef(_marker_key(marker), episode))
            continue
        if str(result.get("run_id") or (marker or {}).get("run_id") or "") != run_id:
            continue
        app_id = str((marker or {}).get("app_id") or "")
        try:
            trial = int(result.get("trial") or (marker or {}).get("trial") or 0)
        except (TypeError, ValueError):
            trial = 0
        key: UnitKey = (app_id, str(result.get("task_id") or ""), trial)
        if is_excluded(result.get("metrics") or {}):
            st.excluded.append(EpisodeRef(key, episode))
        else:
            st.done.add(key)
    return st


def discarded_dir(runs_dir: Path | str, run_id: str) -> Path:
    return Path(runs_dir) / DISCARDED_DIR / run_id


def discard_orphans(runs_dir: Path | str, run_id: str,
                    orphans: Iterable[EpisodeRef | Path] | None = None) -> list[Path]:
    """Move interrupted episode dirs to `runs/_discarded/<run_id>/`, keeping their
    `<task_id>/<episode>` shape. Returns the new locations.

    Moved, never deleted: a killed episode is the evidence for why a run stopped, and
    the transcript in it is often the only record of a provider limit or a device
    dying. Out of `runs/<task>/` it can no longer be mistaken for a result, which is
    the whole point — the scorers glob `runs/*/*/result.json`.
    """
    runs_dir = Path(runs_dir)
    refs = state(runs_dir, run_id).orphans if orphans is None else list(orphans)
    dest_root = discarded_dir(runs_dir, run_id)
    moved: list[Path] = []
    for ref in refs:
        src = ref if isinstance(ref, Path) else ref.path
        try:
            rel = src.resolve().relative_to(runs_dir.resolve())
        except (OSError, ValueError):
            rel = Path(src.parent.name) / src.name
        dest = dest_root / rel
        # A second interruption of the same episode dir name must not clobber the
        # first: both are evidence.
        n = 2
        while dest.exists():
            dest = dest_root / rel.parent / f"{rel.name}~{n}"
            n += 1
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as exc:
            logger.warning("interrupted episode %s not discarded: %s", src, exc)
            continue
        moved.append(dest)
    return moved


# ── resume: the plan a run was started from ───────────────────────────────────


def run_meta_dir(runs_dir: Path | str, run_id: str) -> Path:
    return Path(runs_dir) / RUN_META_DIR / run_id


def plan_path(runs_dir: Path | str, run_id: str) -> Path:
    return run_meta_dir(runs_dir, run_id) / PLAN_FILE


@dataclass
class ResumePlan:
    """`plan.json` read back: what the run was, and the unit list it froze.

    The units are replayed from here rather than re-enumerated from the specs, so a
    spec edited between segments cannot quietly change a run's scope — a resume
    finishes the run that was planned or it refuses.
    """
    run_id: str
    path: Path
    agent: str
    model: str
    mode: str
    trials: int
    segment: int
    units: list[dict[str, Any]]
    environment: dict[str, Any]
    devices: list[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def app_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for u in self.units:
            seen.setdefault(str(u.get("app") or ""), None)
        return [a for a in seen if a]


def load_plan(runs_dir: Path | str, run_id: str) -> ResumePlan:
    """The plan for `run_id`, or `CheckpointError` saying which part is missing."""
    path = plan_path(runs_dir, run_id)
    if not path.exists():
        raise CheckpointError(
            f"No plan for run {run_id}: {path} does not exist.\n"
            f"  A resume needs the run dir the first segment wrote. Check the run id\n"
            f"  (ls {Path(runs_dir) / RUN_META_DIR}) and that --runs-dir points at the\n"
            f"  same tree the run used.")
    doc = _read_json(path)
    if doc is None:
        raise CheckpointError(f"{path} is not readable JSON; this run cannot be resumed.")
    units = doc.get("units")
    if not isinstance(units, list) or not units:
        raise CheckpointError(
            f"{path} carries no unit list, so the run's scope cannot be replayed.\n"
            f"  It was written by a harness older than checkpointing; start a fresh run.")
    trials = doc.get("trials")
    return ResumePlan(
        run_id=str(doc.get("run_id") or run_id),
        path=path,
        agent=str(doc.get("agent") or ""),
        model=str(doc.get("model") or ""),
        mode=str(doc.get("mode") or "guided"),
        trials=int(trials) if isinstance(trials, int) and trials > 0
        else max((int(u.get("trial") or 1) for u in units if isinstance(u, Mapping)), default=1),
        segment=int(doc.get("segment") or 0),
        units=[dict(u) for u in units if isinstance(u, Mapping)],
        environment=dict(doc.get("environment") or {}),
        devices=[str(d) for d in (doc.get("devices") or [])],
        raw=doc,
    )


def next_segment(runs_dir: Path | str, run_id: str) -> int:
    """Claim the next segment index for this run id and record it in `plan.json`.

    Persisted before the segment runs, so a resume that is itself killed still moves
    the counter on: two sittings never share a segment number, which is what makes
    `provenance.segment` worth reading.
    """
    path = plan_path(runs_dir, run_id)
    doc = _read_json(path) or {}
    segment = int(doc.get("segment") or 0) + 1
    doc["segment"] = segment
    try:
        path.write_text(json.dumps(doc, indent=2))
    except OSError as exc:
        logger.warning("plan.json segment not bumped: %s", exc)
    return segment


# ── resume: is this the same benchmark? ───────────────────────────────────────


def _short(value: Any) -> str:
    text = "none" if value is None else str(value)
    return text if len(text) <= 22 else text[:19] + "…"


def compatibility(planned: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Every way the environment a resume would run in differs from the one the run
    was planned in. Empty means the two are the same benchmark.

    Harness version, image digest, and per app the spec hash and the APK hash — the
    four things that change what a score means. A difference is not an error here;
    it is a fact for the caller to refuse on (or to override with --force-resume,
    which is the honest way to say "I know, the numbers are mixed").
    """
    diffs: list[str] = []
    if int(planned.get("schema_version") or 0) != int(current.get("schema_version") or 0):
        diffs.append(f"fingerprint schema: plan {planned.get('schema_version')} "
                     f"→ now {current.get('schema_version')}")
    for key in ("package_version", "image_digest"):
        was, now = planned.get(key), current.get(key)
        if was != now:
            diffs.append(f"{key}: plan {_short(was)} → now {_short(now)}")
    planned_apps = planned.get("apps") or {}
    current_apps = current.get("apps") or {}
    for app_id in sorted(current_apps):
        was = planned_apps.get(app_id)
        if was is None:
            diffs.append(f"{app_id}: not in the plan's environment")
            continue
        for key in ("spec_version", "apk_sha256"):
            if was.get(key) != current_apps[app_id].get(key):
                diffs.append(f"{app_id}: {key} plan {_short(was.get(key))} "
                             f"→ now {_short(current_apps[app_id].get(key))}")
    return diffs
