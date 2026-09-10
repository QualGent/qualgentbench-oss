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

* **What of a run can safely leave the machine.** A run dir mixes scoring outputs
  with the agent's config home, transcripts and evidence, and the first of those
  holds live credentials. The checkpoint bundle (below) packs the results only,
  behind a path denylist, a member allowlist and a scrub gate, so a half-finished
  sweep can be handed to someone who will finish it on their own account.

Nothing here scores anything; it is identity, provenance and packaging only.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import logging
import os
import re
import shutil
import socket
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
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


# ── durable state writes ──────────────────────────────────────────────────────

_tmp_seq = itertools.count()


def write_json(path: Path | str, payload: Mapping[str, Any]) -> bool:
    """Write ``payload`` to ``path`` through a temp file and ``os.replace``.

    Every state file the harness owns goes through this one helper. ``write_text``
    truncates the target before it writes, so a kill inside that window leaves half a
    document — and for ``plan.json``, the only record of a run's frozen scope, half a
    document is a run that can never be resumed while its finished episodes sit
    intact beside it. ``os.replace`` is atomic, so a reader sees the old file or the
    new one and never the seam.

    Returns whether it landed. Callers that owe a durability promise (`plan.json`,
    the import marker) raise on False; the best-effort ones (an episode marker,
    credit telemetry) warn and carry on — a full disk must not be what kills an
    episode.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{next(_tmp_seq)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(dict(payload), indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("%s not written: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


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
    return path if write_json(path, episode_marker(**fields)) else None


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
    """A checkpoint operation cannot proceed: no plan on disk, an unreadable one, one
    written against a different environment, or a bundle that could not be produced,
    read or laid down. The message names the path or the thing to fix."""


@dataclass(frozen=True)
class EpisodeRef:
    """One episode dir on disk and the unit it belongs to."""
    key: UnitKey
    path: Path


@dataclass
class CheckpointState:
    """What a run id has already produced under `runs_dir`.

    `done_keys` is what a resume subtracts from the plan; `done` is the episode dirs
    behind it, which is what an export packs. An excluded attempt is in neither — a
    rate-limited or infra-failed episode measured nothing, so its unit is still owed —
    but its dir stays on disk as evidence.

    The three lists are fixed at construction: `done_keys` is derived from `done`
    once, so appending afterwards would leave it stale. Build one through `state()`.
    """
    run_id: str
    done: list[EpisodeRef] = field(default_factory=list)
    excluded: list[EpisodeRef] = field(default_factory=list)
    orphans: list[EpisodeRef] = field(default_factory=list)
    done_keys: set[UnitKey] = field(init=False, repr=False)
    # (task_id, trial) -> the app ids whose finished episodes claim it.
    _owners: dict[tuple[str, int], set[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.done_keys = {ref.key for ref in self.done}
        self._owners = {}
        for app_id, task_id, trial in self.done_keys:
            self._owners.setdefault((task_id, trial), set()).add(app_id)

    def is_done(self, app_id: str, task_id: str, trial: int) -> bool:
        """Has this unit produced a quotable result in this run?

        Keyed on `(task_id, trial)`, because that is the unit identity the rest of
        the benchmark uses: result.json carries no app id at all and the board dedupes
        on `(model, task, condition, trial)`. But when BOTH sides name an app the
        answer is app-specific and nothing else will do — two apps sharing one task id
        are two units, and taking the first one to finish as proof of the other drops
        a whole app's work with no error anywhere.

        The task-only reading survives only where an app id cannot decide anything:
        a marker written before markers carried one, or a caller that has none. There
        a single unambiguous owner is taken as this unit's, because re-running
        demonstrably finished work is the worse failure.
        """
        owners = self._owners.get((task_id, int(trial)))
        if not owners:
            return False
        app_id = str(app_id or "")
        if app_id in owners:
            return True
        # Nothing on disk claims this app. Only a blank on either side leaves room to
        # read a lone owner as ours; two named apps disagreeing means not done.
        if not app_id or "" in owners:
            return len(owners) == 1
        return False


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
        data = json.loads(Path(path).read_text())
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
    get through" for a tree that arrived from another machine. The one scan behind
    both resume and `checkpoint export`: two implementations of "is this unit
    finished" would eventually disagree about which episodes are quotable.
    """
    done: list[EpisodeRef] = []
    excluded: list[EpisodeRef] = []
    orphans: list[EpisodeRef] = []
    for episode in episode_dirs(runs_dir):
        marker = read_episode_marker(episode)
        result = _read_json(episode / RESULT_FILE)
        if result is None:
            # No readable result: an orphan if this run started it (killed mid-episode,
            # or a result.json half-written by the kill), otherwise not ours to judge —
            # an episode dir from an older harness cannot name its run at all.
            if marker and str(marker.get("run_id") or "") == run_id:
                orphans.append(EpisodeRef(_marker_key(marker), episode))
            continue
        if str(result.get("run_id") or (marker or {}).get("run_id") or "") != run_id:
            continue
        app_id = str((marker or {}).get("app_id") or "")
        try:
            trial = int(result.get("trial") or (marker or {}).get("trial") or 0)
        except (TypeError, ValueError):
            trial = 0
        key: UnitKey = (app_id, str(result.get("task_id") or ""), trial)
        (excluded if is_excluded(result.get("metrics") or {}) else done).append(
            EpisodeRef(key, episode))
    return CheckpointState(run_id=run_id, done=done, excluded=excluded, orphans=orphans)


def discarded_dir(runs_dir: Path | str, run_id: str) -> Path:
    return Path(runs_dir) / DISCARDED_DIR / run_id


@dataclass(frozen=True)
class Discarded:
    """One interrupted episode moved out of the way: where it was (relative to the
    runs dir, which is how a run's own files name it) and where it now lives."""
    source: str
    path: Path


def discard_orphans(runs_dir: Path | str, run_id: str,
                    orphans: Iterable[EpisodeRef | Path] | None = None,
                    *, strict: bool = False) -> list[Discarded]:
    """Move interrupted episode dirs to `runs/_discarded/<run_id>/`, keeping their
    `<task_id>/<episode>` shape.

    Moved, never deleted: a killed episode is the evidence for why a run stopped, and
    the transcript in it is often the only record of a provider limit or a device
    dying. Out of `runs/<task>/` it can no longer be mistaken for a result, which is
    the whole point — the scorers glob `runs/*/*/result.json`.

    `strict` raises `CheckpointError` on a move that fails instead of warning past it.
    An export sets it — it is about to hand this run to someone else, so "one dir
    could not be tidied" has to stop it — while a resume does not: the run in front of
    it is still runnable, and refusing to continue over a stray directory would strand
    hours of work.
    """
    runs_dir = Path(runs_dir)
    refs = state(runs_dir, run_id).orphans if orphans is None else list(orphans)
    dest_root = discarded_dir(runs_dir, run_id)
    moved: list[Discarded] = []
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
            if strict:
                raise CheckpointError(
                    f"could not discard interrupted episode {rel.as_posix()}: {exc}") from exc
            logger.warning("interrupted episode %s not discarded: %s", src, exc)
            continue
        moved.append(Discarded(rel.as_posix(), dest))
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

    Refuses on a plan that is present but unreadable rather than replacing it with a
    counter. `plan.json` is the only record of a run's frozen scope; overwriting an
    unreadable one turns "I cannot parse this" into "this run's scope is gone", with
    its finished episodes still on disk and nothing left that can schedule the rest.
    """
    path = plan_path(runs_dir, run_id)
    doc = _read_json(path)
    if doc is None:
        if path.exists():
            raise CheckpointError(
                f"{path} is not readable JSON, so run {run_id}'s frozen scope cannot "
                f"be read — refusing to overwrite it with a segment counter.\n"
                f"  Restore or repair the file (it is the only record of what this "
                f"run was asked to cover); nothing else was touched.")
        raise CheckpointError(
            f"No plan for run {run_id}: {path} does not exist, so there is no "
            f"segment counter to claim.")
    doc["segment"] = segment = int(doc.get("segment") or 0) + 1
    if not write_json(path, doc):
        raise CheckpointError(
            f"could not record the next segment in {path}; two sittings would share "
            f"a segment number, so this resume stops here.")
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


# ── checkpoint bundle: export / import / show ─────────────────────────────────
#
# A bundle is what one machine hands another: the run's plan and schedule, and every
# COMPLETED episode's scoring files. It is deliberately NOT a copy of the run dir.
# The run dir also holds the agent's config home (`claude_home/`, `codex_home/`), its
# transcript, its MCP config and its evidence — the first two carry live credentials,
# and the point of the bundle is that the person who finishes the run does it on their
# own account.
#
# Three independent gates keep authentication material out, because "we only listed
# the safe files" is a promise that decays the first time someone adds a filename:
#
#   1. DENYLIST — every candidate path is checked by `denied_by()` on the path itself.
#      A denied path is refused for WHERE it is, so it stays refused even if a future
#      caller puts it on the allowlist or globs a directory. Compared casefolded: the
#      filesystem this runs on is not case-sensitive, so neither is the rule.
#   2. ALLOWLIST — `not_a_bundle_member()` states the two path shapes a bundle may
#      carry, so a member has to be a run-metadata file of THIS run or an episode
#      scoring file at exactly the depth `episode_dirs()` scans.
#   3. SCRUB GATE — every byte that would be written is scanned for credential markers
#      first, and a single hit aborts the whole export naming the file. Nothing
#      partial is left behind: the archive is built at a temp path and renamed only
#      after the last file passes.
#
# Import re-runs all three, on the manifest as well as on the members. A bundle
# arrives from another machine, so its sender's gates are not this machine's evidence
# — and the manifest that lists its members travels inside the same unsigned archive,
# so it vouches for nothing on its own.

BUNDLE_SCHEMA_VERSION = 1

MANIFEST_NAME = "checkpoint.json"
# Written into the imported run's meta dir: which episode dirs came from a bundle and
# therefore have no heavy artifacts on this disk (read by the leaderboard/replay pass).
IMPORT_MARKER = "imported.json"

# Run-level files, packed from `_runs/<run_id>/`. `rate_limit.json` and `stop.json`
# live there too and are NOT packed: the first is provider account state, the second
# is about the machine that stopped, and neither is a result.
RUN_META_FILES = ("plan.json", "schedule.jsonl", "board.json")

# The only per-episode files that travel. All small, all scoring inputs: the
# leaderboard reads result.json, the replay staleness check reads replay.json, and the
# rest is what keeps a score auditable without shipping the heavy artifacts.
EPISODE_FILES = (
    "episode.json",
    "result.json",
    "replay.json",
    "verifier/ctrf.json",
    "workspace/findings.yaml",
    "instruction_sent.md",
    "interactions.json",
    "adb_counts.json",
)

# Never packed, never extracted. Directory names match on ANY path component, so a
# nested copy (`workspace/claude_home/`) is caught as well as the top-level one.
DENY_DIRS = frozenset({"claude_home", "codex_home", "agent", "evidence", "hooks"})
DENY_FILES = frozenset({"app_snapshot.tar", "mcp_config.json", "settings.json",
                        "rate_limit.json"})
DENY_PREFIXES = (".env",)


class SecretFound(CheckpointError):
    """The scrub gate matched credential material in a file about to be packed."""

    def __init__(self, path: str, marker: str, line: int) -> None:
        super().__init__(
            f"refusing to export: {path} line {line} contains {marker!r}. "
            f"No bundle was written. Remove the credential from the run dir "
            f"(or from the file the agent wrote it into) and export again.")
        self.path, self.marker, self.line = path, marker, line


# ── the scrub gate ────────────────────────────────────────────────────────────

# Credential markers, in the order they are reported.
#
# The list covers every secret the design doc names as "in play" for a run
# (§ Secrets in play) plus the generic shapes a pasted `env` dump or curl command
# carries, because the file that leaks is usually not one the denylist can reach:
# `workspace/findings.yaml` is agent-authored and legitimately on the export
# allowlist, so content is the only gate in front of it. A marker missing from here
# is a credential the bundle will happily ship.
#
# All literal except where a shape is the only thing to match on. ``sk-`` is the one
# anchored marker: as a bare substring it also matches ordinary benchmark ids — the
# seeded bug ``task-completion-not-persisted`` contains "sk-", and it appears in
# result.json, instruction_sent.md and findings.yaml — so an unanchored match would
# abort every export of those apps and make the gate something people work around.
# Requiring a token boundary in front keeps real keys matched (`sk-ant-...`,
# `sk-proj-...`, `sk-` at the start of a value) and ids not. ``sk-ant-`` is ALSO
# matched unanchored, so an Anthropic key is caught however it is embedded.
_SECRET_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sk-ant-", re.compile(r"sk-ant-")),
    ("sk-", re.compile(r"(?<![A-Za-z0-9_])sk-")),
    ("CLAUDE_CODE_OAUTH_TOKEN", re.compile(r"CLAUDE_CODE_OAUTH_TOKEN")),
    ("ANTHROPIC_", re.compile(r"ANTHROPIC_")),
    # Matched case-insensitively: a header quoted in a findings file is as often
    # `authorization: bearer …` as the canonical spelling.
    ("Bearer ", re.compile(r"(?i)bearer ")),
    ("refreshToken", re.compile(r"refreshToken")),
    ("accessToken", re.compile(r"accessToken")),
    # The rest of the design doc's list. Prefixes rather than whole names, so
    # FIREWORKS_API_KEYS and every ..._API_KEY_2 variant is caught too.
    ("OPENAI_", re.compile(r"OPENAI_")),
    ("FIREWORKS_", re.compile(r"FIREWORKS_")),
    ("CODEX_", re.compile(r"CODEX_")),
    ("HF_TOKEN", re.compile(r"HF_TOKEN")),
    ("QUALGENT_SHEET_", re.compile(r"QUALGENT_SHEET_")),
    ("ANDROID_EMULATOR_CONSOLE_AUTH_TOKEN",
     re.compile(r"ANDROID_EMULATOR_CONSOLE_AUTH_TOKEN")),
    # Generic shapes. snake_case is what an OAuth response body and most SDK configs
    # use, so the camelCase pair above covers barely half of the real spellings.
    ("access_token", re.compile(r"(?i)access_token")),
    ("refresh_token", re.compile(r"(?i)refresh_token")),
    ("-----BEGIN PRIVATE KEY-----",
     re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    # AWS: the key id has a shape of its own, and the secret only ever travels
    # beside one of these three names.
    ("AKIA/ASIA key id",
     re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("AWS_ACCESS_KEY_ID", re.compile(r"(?i)aws_access_key_id")),
    ("AWS_SECRET_ACCESS_KEY", re.compile(r"(?i)aws_secret_access_key")),
    ("AWS_SESSION_TOKEN", re.compile(r"(?i)aws_session_token")),
)


def scan_for_secrets(data: bytes) -> tuple[str, int] | None:
    """``(marker, line_number)`` for the first credential marker in ``data``, else None.

    Decoded leniently on purpose: a file that is not valid UTF-8 still gets scanned
    rather than skipped, because "could not decode" must never quietly mean
    "assumed clean".
    """
    text = data.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines() or [""], start=1):
        for marker, pattern in _SECRET_MARKERS:
            if pattern.search(line):
                return marker, lineno
    return None


def denied_by(path: str | Path) -> str | None:
    """The denylist rule blocking ``path``, or None.

    Applied to the path itself rather than to a list of what to include: a file is
    refused for where it is, which is the only form of the rule that survives someone
    later widening what gets collected.

    Compared casefolded, because the filesystem this runs on is not case-sensitive.
    ``CLAUDE_HOME/`` and ``claude_home/`` are the same directory on macOS (and on
    Windows), so a case-sensitive rule would refuse one spelling of a path and write
    the other one straight over the credentials it was meant to keep out.
    """
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    for part in parts:
        folded = part.casefold()
        if folded in DENY_DIRS:
            return f"{part}/"
        if folded.startswith(DENY_PREFIXES):
            return part
    if parts and parts[-1].casefold() in DENY_FILES:
        return parts[-1]
    return None


def not_a_bundle_member(name: str, run_id: str) -> str | None:
    """Why ``name`` may not be a member of ``run_id``'s bundle, or None if it may.

    The allowlist, stated once as a rule over paths so both halves of the format can
    apply the same one. Export builds its candidates from ``RUN_META_FILES`` and
    ``EPISODE_FILES`` and so satisfies this by construction; import has to CHECK it,
    because the only thing vouching for an incoming member is a manifest travelling
    inside the same unsigned archive. Without this, "the bundle carries results" is a
    promise the sender makes about themselves.

    Two shapes are legal and nothing else is:

    * ``_runs/<run_id>/<run-meta file>`` — and the run id has to be this bundle's, so
      one archive cannot write into another run's metadata.
    * ``<task_id>/<episode dir>/<episode file>`` — exactly the depth
      ``episode_dirs()`` scans, so a member cannot land shallower (a dotfile at the
      runs root) or deeper (an arbitrary tree under an episode).
    """
    parts = PurePosixPath(str(name).replace("\\", "/")).parts
    if parts and parts[0] == RUN_META_DIR:
        if len(parts) == 3 and parts[1] == run_id and parts[2] in RUN_META_FILES:
            return None
        return f"not a {RUN_META_DIR}/{run_id}/ metadata file"
    # Read from the module constants on every call rather than a precomputed set, so
    # there is one allowlist and a test that widens it widens both halves at once.
    if (len(parts) >= 3
            and not parts[0].startswith((".", "_"))
            and not parts[1].startswith(".")
            and "/".join(parts[2:]) in set(EPISODE_FILES)):
        return None
    return "not an episode scoring file"


def _discarded_count(runs_dir: Path, run_id: str) -> int:
    """Interrupted episodes quarantined for this run, across every segment — not just
    the ones this call moved."""
    root = discarded_dir(runs_dir, run_id)
    return sum(1 for d in root.glob("*/*") if d.is_dir()) if root.is_dir() else 0


# ── manifest ──────────────────────────────────────────────────────────────────


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan_trials(plan: Mapping[str, Any]) -> int:
    """Trials per unit. Recorded on the plan when the writer knows it; otherwise the
    highest trial number the plan enumerates, which is the same number."""
    declared = plan.get("trials")
    if isinstance(declared, int) and declared > 0:
        return declared
    trials = [int(u.get("trial") or 0) for u in (plan.get("units") or [])
              if isinstance(u, Mapping)]
    return max(trials) if trials else 1


def _plan_scope(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What the run was asked to cover. Frozen at plan time, so a resume measures the
    same set even if the specs on the other machine have moved on."""
    declared = plan.get("scope")
    if isinstance(declared, Mapping):
        return dict(declared)
    units = [u for u in (plan.get("units") or []) if isinstance(u, Mapping)]
    return {
        "apps": sorted({str(u["app"]) for u in units if u.get("app")}),
        "kinds": sorted({str(u["kind"]) for u in units if u.get("kind")}),
        "episodes": int(plan.get("episodes") or len(units)),
        "trials": _plan_trials(plan),
    }


def _remaining_units(plan: Mapping[str, Any],
                     done: CheckpointState) -> list[dict[str, Any]]:
    """Planned units with no quotable result yet — what the receiving machine owes.

    Decided by `CheckpointState.is_done`, the same predicate `run --resume` subtracts
    with, so a bundle's "remaining" list and the units the receiving machine actually
    schedules cannot disagree. An excluded attempt (rate limited, env failure) is not
    done, so its unit stays here and gets run again.
    """
    out: list[dict[str, Any]] = []
    for unit in plan.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        try:
            trial = int(unit.get("trial") or 0)
        except (TypeError, ValueError):
            trial = 0
        if done.is_done(str(unit.get("app") or ""), str(unit.get("task") or ""), trial):
            continue
        out.append({"app": unit.get("app"), "task": unit.get("task"),
                    "kind": unit.get("kind"), "trial": trial})
    return out


def _manifest(*, run_id: str, plan: Mapping[str, Any], segment: int,
              scan: CheckpointState, remaining: list[dict[str, Any]],
              files: list[dict[str, Any]], discarded: int, planned: int) -> dict[str, Any]:
    from .leaderboard import clean_model_name

    model_id = str(plan.get("model") or "")
    env = plan.get("environment") or {}
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "run_id": run_id,
        "agent": str(plan.get("agent") or ""),
        # The model NAME, not the routing id: `anthropic/claude-opus-4-8` is how one
        # provider addresses it, `claude-opus-4-8` is what the board compares and what
        # a reader recognises. The full id rides along because a resume has to
        # re-invoke the same route.
        "model": clean_model_name(model_id),
        "model_id": model_id,
        "mode": str(plan.get("mode") or ""),
        "trials": _plan_trials(plan),
        "scope": _plan_scope(plan),
        "package_version": str(env.get("package_version") or package_version()),
        "image_digest": env.get("image_digest", image_digest()),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "segment": int(segment),
        "counts": {
            "planned": planned,
            "done": len(scan.done),
            "remaining": len(remaining),
            "excluded": len(scan.excluded),
            "discarded": discarded,
            "files": len(files),
        },
        "remaining": remaining,
        "files": files,
    }


# ── export ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExportResult:
    path: Path
    manifest: dict[str, Any]
    discarded: tuple[str, ...]      # episode dirs moved aside by THIS export

    @property
    def counts(self) -> dict[str, int]:
        return dict(self.manifest["counts"])


def bundle_name(run_id: str, segment: int) -> str:
    return f"qgb-checkpoint-{run_id}-seg{int(segment)}.tar.gz"


def _candidate_files(runs_dir: Path, run_id: str,
                     done: Iterable[Path]) -> list[tuple[str, Path]]:
    """``(arcname, source)`` for everything the bundle may contain — an allowlist of
    names under an allowlist of dirs. The denylist is applied on top of this, not
    instead of it."""
    out: list[tuple[str, Path]] = []
    meta_dir = runs_dir / RUN_META_DIR / run_id
    for name in RUN_META_FILES:
        path = meta_dir / name
        if path.is_file():
            out.append((f"{RUN_META_DIR}/{run_id}/{name}", path))
    for episode_dir in done:
        rel = episode_dir.relative_to(runs_dir).as_posix()
        for name in EPISODE_FILES:
            path = episode_dir / name
            if path.is_file():
                out.append((f"{rel}/{name}", path))
    return out


def _resolve_output(runs_dir: Path, run_id: str, segment: int,
                    output: Path | str | None) -> Path:
    default = bundle_name(run_id, segment)
    if output is None:
        return Path.cwd() / default
    path = Path(output)
    return path / default if path.is_dir() else path


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes, mtime: float) -> None:
    """Add exactly the bytes that were scanned. The file is never re-read between the
    scrub gate and the archive, so there is no window in which it could change."""
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(mtime)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tar.addfile(info, io.BytesIO(data))


def export_bundle(runs_dir: Path | str, run_id: str, *,
                  output: Path | str | None = None,
                  # Named for what it does rather than for the function it calls: the
                  # sweep is now the shared `discard_orphans`, which a parameter of that
                  # name would shadow inside this body.
                  discard_interrupted: bool = True) -> ExportResult:
    """Pack ``run_id``'s results into a portable bundle and return what was written.

    Raises ``SecretFound`` (a ``CheckpointError``) if any packed byte looks like a
    credential, and leaves no file behind when it does.
    """
    runs_dir = Path(runs_dir)
    plan = _read_json(plan_path(runs_dir, run_id))
    if plan is None:
        raise CheckpointError(
            f"no run {run_id} under {runs_dir} "
            f"(expected {plan_path(runs_dir, run_id)})")

    scan = state(runs_dir, run_id)
    # Before anything is collected: a partial episode must not be able to ship. Strict,
    # unlike a resume's sweep — this run is about to be handed to someone else.
    moved = (discard_orphans(runs_dir, run_id, scan.orphans, strict=True)
             if discard_interrupted else [])

    candidates = _candidate_files(runs_dir, run_id, [ref.path for ref in scan.done])
    runs_root = runs_dir.resolve()
    for arcname, source in candidates:
        if rule := denied_by(arcname):
            raise CheckpointError(
                f"refusing to export: {arcname} is denylisted ({rule})")
        if rule := not_a_bundle_member(arcname, run_id):
            raise CheckpointError(
                f"refusing to export: {arcname} is {rule}")
        if source.is_symlink():
            # A scoring file replaced by a link points somewhere we never inspected.
            raise CheckpointError(
                f"refusing to export: {arcname} is a symlink to "
                f"{os.readlink(source)!r}, not a scoring file")
        # …and the leaf is only half of it: `is_file()` and `read_bytes()` follow
        # every component, so a symlinked `verifier/` or task dir pulls in whatever
        # it points at. Resolve the whole path and require it to have stayed home.
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise CheckpointError(
                f"refusing to export: {arcname} could not be resolved: {exc}") from exc
        if not resolved.is_relative_to(runs_root):
            raise CheckpointError(
                f"refusing to export: {arcname} resolves through a symlinked parent "
                f"to {resolved}, outside the runs dir {runs_root}")

    segment = int(plan.get("segment") or 0)
    out_path = _resolve_output(runs_dir, run_id, segment, output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".partial")

    packed: list[dict[str, Any]] = []
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for arcname, source in candidates:
                data = source.read_bytes()
                if hit := scan_for_secrets(data):
                    raise SecretFound(arcname, hit[0], hit[1])
                packed.append({"path": arcname, "sha256": _sha256(data),
                               "bytes": len(data)})
                _add_bytes(tar, arcname, data, source.stat().st_mtime)
            manifest = _manifest(
                run_id=run_id, plan=plan, segment=segment, scan=scan,
                remaining=_remaining_units(plan, scan), files=packed,
                discarded=_discarded_count(runs_dir, run_id),
                planned=len(plan.get("units") or []))
            # Written last so the per-file digests are final; readers address it by
            # name, and tar member order is not part of the format.
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            # The manifest is bundle bytes like any other. It quotes the plan's model
            # id and every packed path, so "it is ours, we generated it" is not a
            # reason to be the one member nobody scans.
            if hit := scan_for_secrets(manifest_bytes):
                raise SecretFound(MANIFEST_NAME, hit[0], hit[1])
            _add_bytes(tar, MANIFEST_NAME, manifest_bytes,
                       datetime.now(timezone.utc).timestamp())
        tmp.replace(out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ExportResult(out_path, manifest, tuple(m.source for m in moved))


# ── import ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImportResult:
    run_id: str
    runs_dir: Path
    manifest: dict[str, Any]
    written: tuple[str, ...]
    episodes: tuple[str, ...]
    resume_command: str


def read_manifest(bundle: Path | str) -> dict[str, Any]:
    """``checkpoint.json`` out of a bundle, or a ``CheckpointError`` naming why not."""
    bundle = Path(bundle)
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            member = tar.extractfile(MANIFEST_NAME)
            if member is None:
                raise KeyError(MANIFEST_NAME)
            raw = member.read()
            manifest = json.loads(raw)
    except (OSError, tarfile.TarError, KeyError, ValueError) as exc:
        raise CheckpointError(
            f"{bundle} is not a qualgent-bench checkpoint bundle: {exc}") from exc
    # Scrubbed on the way in as well as on the way out: this is the one member
    # `_bundle_payload` skips (it is not a listed file), so without this it would be
    # the only place in an incoming archive a credential could ride unread.
    if hit := scan_for_secrets(raw):
        raise SecretFound(f"{bundle}:{MANIFEST_NAME}", hit[0], hit[1])
    if not isinstance(manifest, dict):
        raise CheckpointError(f"{bundle}: {MANIFEST_NAME} is not an object")
    version = manifest.get("schema_version")
    if version != BUNDLE_SCHEMA_VERSION:
        raise CheckpointError(
            f"{bundle}: {MANIFEST_NAME} is schema_version {version!r}; this harness "
            f"reads {BUNDLE_SCHEMA_VERSION}")
    return manifest


def _safe_arcname(name: str, bundle: Path) -> str:
    """Reject, never sanitise: a member that wants to write outside the runs dir is a
    hostile archive, and quietly rewriting its path hides that."""
    if not name or name.startswith("/") or "\\" in name:
        raise CheckpointError(f"{bundle}: refusing member path {name!r}")
    parts = PurePosixPath(name).parts
    if ".." in parts or PurePosixPath(name).is_absolute():
        raise CheckpointError(f"{bundle}: refusing member path {name!r}")
    return PurePosixPath(name).as_posix()


def _bundle_payload(bundle: Path, manifest: Mapping[str, Any],
                    run_id: str) -> dict[str, bytes]:
    """Every member's bytes, checked against the manifest and all three gates.

    A bundle comes from another machine, so this repeats the export's checks rather
    than trusting that they ran: an unlisted member, a denylisted path, a path that
    is not on the allowlist, traversal, a changed digest and credential markers are
    all refusals here too. Being listed in the manifest is the weakest of these — the
    manifest travels inside the same unsigned archive as the members it vouches for,
    so on its own it says only that the sender was consistent.
    """
    expected = {str(f.get("path")): f for f in (manifest.get("files") or [])
                if isinstance(f, Mapping)}
    payload: dict[str, bytes] = {}
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name == MANIFEST_NAME:
                    continue
                name = _safe_arcname(member.name, bundle)
                if not member.isfile():
                    raise CheckpointError(
                        f"{bundle}: {name} is not a regular file — refusing")
                if rule := denied_by(name):
                    raise CheckpointError(
                        f"{bundle}: refusing denylisted path {name} ({rule})")
                if rule := not_a_bundle_member(name, run_id):
                    raise CheckpointError(
                        f"{bundle}: refusing {name} — it is {rule}, and a bundle may "
                        f"only carry this run's metadata and episode scoring files")
                if name not in expected:
                    raise CheckpointError(
                        f"{bundle}: {name} is not listed in {MANIFEST_NAME} — refusing")
                handle = tar.extractfile(member)
                data = handle.read() if handle is not None else b""
                if _sha256(data) != str(expected[name].get("sha256")):
                    raise CheckpointError(f"{bundle}: checksum mismatch for {name}")
                if hit := scan_for_secrets(data):
                    raise SecretFound(f"{bundle}:{name}", hit[0], hit[1])
                payload[name] = data
    except tarfile.TarError as exc:
        raise CheckpointError(f"{bundle}: unreadable archive: {exc}") from exc
    if missing := sorted(set(expected) - set(payload)):
        raise CheckpointError(
            f"{bundle}: {len(missing)} file(s) listed in {MANIFEST_NAME} are not in "
            f"the archive: {', '.join(missing[:5])}")
    _refuse_foreign_episodes(bundle, payload, run_id)
    return payload


def _refuse_foreign_episodes(bundle: Path, payload: Mapping[str, bytes],
                             run_id: str) -> None:
    """Every episode in the bundle has to be an episode OF this run.

    The path allowlist says a member is shaped like a scoring file; this says whose
    it is. A fabricated ``explore-y/ep9/result.json`` is exactly the right shape and
    lands exactly where ``leaderboard.load_results`` globs, so without this a bundle
    can inject a passing row attributed to somebody else's run id.

    An export only ever packs episodes ``state()`` matched to the run, and ``state()``
    matches on result.json's run id or the marker's — so for every legitimate bundle
    at least one of the pair names it here.
    """
    episodes: dict[str, set[str]] = {}
    for name, data in payload.items():
        parts = PurePosixPath(name).parts
        if parts[0] == RUN_META_DIR or parts[-1] not in (RESULT_FILE, EPISODE_MARKER):
            continue
        try:
            doc = json.loads(data)
        except ValueError as exc:
            raise CheckpointError(f"{bundle}: {name} is not readable JSON: {exc}") from exc
        claimed = str((doc or {}).get("run_id") or "") if isinstance(doc, dict) else ""
        if claimed and claimed != run_id:
            raise CheckpointError(
                f"{bundle}: refusing {name} — it names run {claimed!r}, not this "
                f"bundle's {run_id!r}. A bundle carries one run.")
        episodes.setdefault("/".join(parts[:2]), set()).add(claimed)
    for episode, claims in sorted(episodes.items()):
        if run_id not in claims:
            raise CheckpointError(
                f"{bundle}: refusing {episode}/ — neither its {RESULT_FILE} nor its "
                f"{EPISODE_MARKER} names run {run_id!r}, so nothing in the archive "
                f"attributes this episode to the run it claims to continue.")


def _import_dest(runs_dir: Path, runs_root: Path, name: str) -> Path:
    """Where ``name`` may be written under ``runs_dir``, or a refusal.

    The import mirror of export's resolved-parent check, and the gate none of the
    member-name rules can stand in for. ``_safe_arcname`` refuses traversal, absolute
    paths and backslashes, and the allowlist refuses anything not shaped like this
    run's scoring output — but every one of those reads the member NAME, and the name
    is not where this escape lives. A symlinked ``birday-t1/`` already sitting in the
    RECEIVING tree sends a perfectly legal member wherever it points.

    The parent is resolved as well as the leaf because the redirect happens at
    ``mkdir`` time: checking only the file would still have created the directory tree
    outside the runs dir before declining to write into it.
    """
    dest = runs_dir / name
    for candidate in (dest.parent, dest):
        # Deliberately not strict: the destination is what this import is about to
        # create. Resolution still follows every component that DOES exist, which is
        # the whole of the attack.
        resolved = candidate.resolve()
        if not resolved.is_relative_to(runs_root):
            raise CheckpointError(
                f"refusing to import: {name} resolves through a symlinked parent to "
                f"{resolved}, outside the runs dir {runs_root}")
    return dest


def import_bundle(bundle: Path | str, runs_dir: Path | str = "runs") -> ImportResult:
    """Lay a bundle's files under ``runs_dir`` and say how to resume the run.

    Refuses outright if the run already exists here with different bytes — two
    machines that both ran a unit have produced two different answers, and silently
    keeping one of them is how a board stops being reproducible. Re-importing the
    same bundle is a no-op, not a conflict.
    """
    bundle, runs_dir = Path(bundle), Path(runs_dir)
    manifest = read_manifest(bundle)
    run_id = str(manifest.get("run_id") or "")
    if not run_id:
        raise CheckpointError(f"{bundle}: {MANIFEST_NAME} names no run_id")

    payload = _bundle_payload(bundle, manifest, run_id)

    runs_root = runs_dir.resolve()
    # Resolved before a single byte is written, so a landing tree that would redirect
    # any member fails the whole import rather than half of it.
    dests = {name: _import_dest(runs_dir, runs_root, name) for name in sorted(payload)}
    # The marker is a file this import writes too, and `_runs/` can be a symlink just
    # as easily as a task dir can.
    _import_dest(runs_dir, runs_root, f"{RUN_META_DIR}/{run_id}/{IMPORT_MARKER}")

    conflicts = [name for name, dest in dests.items()
                 if dest.exists() and _read_bytes_or_raise(dest) != payload[name]]
    if conflicts:
        raise CheckpointError(
            f"refusing to import: run {run_id} already exists under {runs_dir} with "
            f"different contents ({len(conflicts)} file(s)): "
            f"{', '.join(conflicts[:5])}"
            f"{' …' if len(conflicts) > 5 else ''}. Import into an empty --runs-dir, "
            f"or move the existing run aside.")

    written: list[str] = []
    for name, dest in dests.items():
        data = payload[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(data)
        except OSError as exc:
            raise CheckpointError(f"could not write {dest}: {exc}") from exc
        written.append(name)

    episodes = sorted({"/".join(PurePosixPath(name).parts[:2]) for name in written
                       if not name.startswith(f"{RUN_META_DIR}/")})
    _write_import_marker(runs_dir, run_id, bundle, manifest, episodes, len(written))

    resume = f"qualgent-bench run --resume {run_id}"
    if str(runs_dir) != "runs":
        resume += f" --runs-dir {runs_dir}"
    return ImportResult(run_id, runs_dir, manifest, tuple(written), tuple(episodes),
                        resume)


def _read_bytes_or_raise(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CheckpointError(f"could not read existing {path}: {exc}") from exc


def _write_import_marker(runs_dir: Path, run_id: str, bundle: Path,
                         manifest: Mapping[str, Any], episodes: list[str],
                         file_count: int) -> Path:
    """``_runs/<run_id>/imported.json`` — the episode dirs that came from a bundle.

    These have result.json and replay.json but no evidence, transcript or app
    snapshot, so anything that would re-open a heavy artifact has to know to skip
    them rather than call the episode broken. Accumulates across imports: a run can
    arrive in several segments.
    """
    path = runs_dir / RUN_META_DIR / run_id / IMPORT_MARKER
    prior = _read_json(path) or {}
    imports = [i for i in (prior.get("imports") or []) if isinstance(i, Mapping)]
    imports.append({
        "bundle": bundle.name,
        "segment": manifest.get("segment"),
        "exported_by": manifest.get("host"),
        "created_at": manifest.get("created_at"),
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": file_count,
    })
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "run_id": run_id,
        "episodes": sorted(set(prior.get("episodes") or []) | set(episodes)),
        "imports": imports,
    }
    if not write_json(path, payload):
        raise CheckpointError(
            f"could not write {path}; the files landed but nothing records that they "
            f"arrived without their heavy artifacts")
    return path


def imported_episodes(runs_dir: Path | str, run_id: str) -> set[str]:
    """Episode dirs (relative to ``runs_dir``) that arrived in a bundle and therefore
    have no heavy artifacts on this machine. Empty when nothing was imported."""
    marker = _read_json(Path(runs_dir) / RUN_META_DIR / run_id / IMPORT_MARKER) or {}
    return {str(e) for e in (marker.get("episodes") or [])}


def imported_episode_dirs(runs_dir: Path | str,
                          run_ids: Iterable[str]) -> set[Path]:
    """The imported episode dirs across several runs, as resolved absolute paths.

    One marker read per run id rather than per episode, and resolved so a caller
    holding a path built some other way can test membership directly.
    """
    runs_dir = Path(runs_dir)
    out: set[Path] = set()
    for run_id in {str(r) for r in run_ids if r}:
        for rel in imported_episodes(runs_dir, run_id):
            out.add((runs_dir / rel).resolve())
    return out


# The heavy artifacts a re-replay opens. Both are denylisted from a bundle
# (`DENY_FILES` / `DENY_DIRS`), so an episode dir holding NEITHER is results-only
# however it got here — imported, rsynced, or pruned by hand. Checking for either
# rather than both keeps a pre-snapshot episode replayable: it has no
# `app_snapshot.tar` but still has its `evidence/`, and `replay_findings.py`
# already handles the missing snapshot by starting from regenerated sample data.
REPLAY_ARTIFACTS = ("app_snapshot.tar", "evidence")


def artifacts_are_local(episode_dir: Path | str) -> bool:
    """Whether this episode dir still holds the artifacts a re-replay needs."""
    d = Path(episode_dir)
    return any((d / name).exists() for name in REPLAY_ARTIFACTS)


# ── show ──────────────────────────────────────────────────────────────────────


def run_summary(runs_dir: Path | str, run_id: str) -> dict[str, Any]:
    """The manifest view of a run still on disk — a bundle's ``checkpoint.json``
    shape minus ``files``, so ``checkpoint show`` prints one thing whether it was
    handed an archive or a run id."""
    runs_dir = Path(runs_dir)
    plan = _read_json(plan_path(runs_dir, run_id))
    if plan is None:
        raise CheckpointError(
            f"no run {run_id} under {runs_dir} "
            f"(expected {plan_path(runs_dir, run_id)})")
    scan = state(runs_dir, run_id)
    remaining = _remaining_units(plan, scan)
    view = _manifest(run_id=run_id, plan=plan, segment=int(plan.get("segment") or 0),
                     scan=scan, remaining=remaining, files=[],
                     discarded=_discarded_count(runs_dir, run_id),
                     planned=len(plan.get("units") or []))
    view.pop("files")
    # On disk an interrupted episode may not have been discarded yet; a bundle can
    # never carry one, so this key only exists on the live view.
    view["counts"]["orphans"] = len(scan.orphans)
    view["imported_episodes"] = len(imported_episodes(runs_dir, run_id))
    return view


def describe(target: Path | str, *, runs_dir: Path | str = "runs") -> dict[str, Any]:
    """Manifest for ``target``: a bundle file if it is one, otherwise a run id under
    ``runs_dir``."""
    path = Path(target)
    if path.is_file():
        return read_manifest(path)
    return run_summary(runs_dir, str(target))
