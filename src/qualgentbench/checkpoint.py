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
  behind a path denylist and a scrub gate, so a half-finished sweep can be handed to
  someone who will finish it on their own account.

Nothing here scores anything; it is identity, provenance and packaging only.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
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


# ── checkpoint bundle: export / import / show ─────────────────────────────────
#
# A bundle is what one machine hands another: the run's plan and schedule, and every
# COMPLETED episode's scoring files. It is deliberately NOT a copy of the run dir.
# The run dir also holds the agent's config home (`claude_home/`, `codex_home/`), its
# transcript, its MCP config and its evidence — the first two carry live credentials,
# and the point of the bundle is that the person who finishes the run does it on their
# own account.
#
# Two independent gates keep authentication material out, because "we only listed the
# safe files" is a promise that decays the first time someone adds a filename:
#
#   1. DENYLIST — every candidate path is checked by `denied_by()` on the path itself.
#      A denied path is refused for WHERE it is, so it stays refused even if a future
#      caller puts it on the allowlist or globs a directory.
#   2. SCRUB GATE — every byte that would be written is scanned for credential markers
#      first, and a single hit aborts the whole export naming the file. Nothing
#      partial is left behind: the archive is built at a temp path and renamed only
#      after the last file passes.
#
# Import re-runs both gates. A bundle arrives from another machine, so its sender's
# gates are not this machine's evidence.

BUNDLE_SCHEMA_VERSION = 1

MANIFEST_NAME = "checkpoint.json"
# Written into the imported run's meta dir: which episode dirs came from a bundle and
# therefore have no heavy artifacts on this disk (read by the leaderboard/replay pass).
IMPORT_MARKER = "imported.json"
RUN_META_DIR = "_runs"
DISCARD_DIR = "_discarded"

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


class CheckpointError(RuntimeError):
    """A bundle could not be produced, read or laid down. The message names the path."""


class SecretFound(CheckpointError):
    """The scrub gate matched credential material in a file about to be packed."""

    def __init__(self, path: str, marker: str, line: int) -> None:
        super().__init__(
            f"refusing to export: {path} line {line} contains {marker!r}. "
            f"No bundle was written. Remove the credential from the run dir "
            f"(or from the file the agent wrote it into) and export again.")
        self.path, self.marker, self.line = path, marker, line


# ── the scrub gate ────────────────────────────────────────────────────────────

# Credential markers, in the order they are reported. All literal except ``sk-``:
# as a bare substring it also matches ordinary benchmark ids — the seeded bug
# ``task-completion-not-persisted`` contains "sk-", and it appears in result.json,
# instruction_sent.md and findings.yaml — so an unanchored match would abort every
# export of those apps and make the gate something people work around. Requiring a
# token boundary in front keeps real keys matched (`sk-ant-...`, `sk-proj-...`,
# `sk-` at the start of a value) and ids not. ``sk-ant-`` is ALSO matched unanchored,
# so an Anthropic key is caught however it is embedded.
_SECRET_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sk-ant-", re.compile(r"sk-ant-")),
    ("sk-", re.compile(r"(?<![A-Za-z0-9_])sk-")),
    ("CLAUDE_CODE_OAUTH_TOKEN", re.compile(r"CLAUDE_CODE_OAUTH_TOKEN")),
    ("ANTHROPIC_", re.compile(r"ANTHROPIC_")),
    ("Bearer ", re.compile(r"Bearer ")),
    ("refreshToken", re.compile(r"refreshToken")),
    ("accessToken", re.compile(r"accessToken")),
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
    """
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    for part in parts:
        if part in DENY_DIRS:
            return f"{part}/"
        if part.startswith(DENY_PREFIXES):
            return part
    if parts and parts[-1] in DENY_FILES:
        return parts[-1]
    return None


# ── run state (bundle-local; see TODO) ────────────────────────────────────────


@dataclass(frozen=True)
class _RunScan:
    done: tuple[Path, ...]         # scored, quotable, and packable
    excluded: tuple[Path, ...]     # scored but not a result (rate limited, env failure)
    orphans: tuple[Path, ...]      # ours, started, never reported — an interrupted unit
    done_keys: frozenset[tuple[str, int]]


def _scan_run(runs_dir: Path, run_id: str) -> _RunScan:
    """Split ``run_id``'s episode dirs into finished / unquotable / interrupted.

    TODO(QUA-2694): this is the same done/orphan/excluded split, on the same
    ``is_excluded`` rule, that ``checkpoint.state(runs_dir, run_id)`` will own once
    resume lands. Collapse this helper into ``state()`` then and have the export call
    that — it is written separately only so the bundle and the resume ticket do not
    fight over one function while both are in flight.
    """
    from .failures import is_excluded

    runs_dir = Path(runs_dir)
    done: list[Path] = []
    excluded: list[Path] = []
    orphans: list[Path] = []
    done_keys: set[tuple[str, int]] = set()

    for episode_dir in sorted(runs_dir.glob("*/*")):
        # `_runs/` and `_discarded/` are meta, not episodes.
        if not episode_dir.is_dir() or episode_dir.parent.name.startswith("_"):
            continue
        marker = read_episode_marker(episode_dir) or {}
        result = _read_json(episode_dir / "result.json") or {}
        owner = str(marker.get("run_id") or result.get("run_id") or "")
        if owner != run_id:
            continue
        if not result:
            # Marked as ours with no readable result: the episode was killed, or its
            # result.json is a partial write from the kill. Either way it is not a
            # score, and it must never leave the machine looking like one.
            orphans.append(episode_dir)
        elif is_excluded(result.get("metrics") or {}):
            excluded.append(episode_dir)
        else:
            done.append(episode_dir)
            done_keys.add(_unit_key(result, marker))
    return _RunScan(tuple(done), tuple(excluded), tuple(orphans), frozenset(done_keys))


def _unit_key(result: Mapping[str, Any], marker: Mapping[str, Any]) -> tuple[str, int]:
    """``(task_id, trial)`` — the unit identity WITHIN one run.

    ``scheduler.Unit.key`` is ``(app_id, task_id, trial)``, but a run is one agent,
    one model and one condition, and task ids are app-prefixed, so the app is
    redundant here — and result.json does not carry it, while the marker does only
    for episodes written by this harness or later.
    """
    task = str(result.get("task_id") or marker.get("task_id") or "")
    raw_trial = result.get("trial", marker.get("trial", 0))
    try:
        trial = int(raw_trial)
    except (TypeError, ValueError):
        trial = 0
    return task, trial


def _discard_orphans(runs_dir: Path, run_id: str, orphans: Iterable[Path]) -> list[str]:
    """Move interrupted episode dirs to ``runs/_discarded/<run_id>/`` before packing,
    so a half-written episode cannot ship as a result. Moved, not deleted — an
    interrupted episode is still evidence of what happened to the run.

    TODO(QUA-2694): resume performs the same move at start-up; collapse the two into
    one ``checkpoint.discard_orphans()`` once both have landed.
    """
    moved: list[str] = []
    for episode_dir in orphans:
        rel = episode_dir.relative_to(runs_dir)
        dest = runs_dir / DISCARD_DIR / run_id / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        target, n = dest, 1
        while target.exists():          # a re-run of the same unit discarded twice
            target = dest.with_name(f"{dest.name}~{n}")
            n += 1
        try:
            shutil.move(str(episode_dir), str(target))
        except OSError as exc:
            raise CheckpointError(
                f"could not discard interrupted episode {rel}: {exc}") from exc
        moved.append(rel.as_posix())
    return moved


def _discarded_count(runs_dir: Path, run_id: str) -> int:
    root = Path(runs_dir) / DISCARD_DIR / run_id
    return sum(1 for d in root.glob("*/*") if d.is_dir()) if root.is_dir() else 0


# ── manifest ──────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
                     done_keys: frozenset[tuple[str, int]]) -> list[dict[str, Any]]:
    """Planned units with no quotable result yet — what the receiving machine owes.

    An excluded attempt (rate limited, env failure) is not done, so its unit stays
    here and gets run again, which is the behaviour the board already assumes.
    """
    out: list[dict[str, Any]] = []
    for unit in plan.get("units") or []:
        if not isinstance(unit, Mapping):
            continue
        try:
            trial = int(unit.get("trial") or 0)
        except (TypeError, ValueError):
            trial = 0
        if (str(unit.get("task") or ""), trial) in done_keys:
            continue
        out.append({"app": unit.get("app"), "task": unit.get("task"),
                    "kind": unit.get("kind"), "trial": trial})
    return out


def _manifest(*, run_id: str, plan: Mapping[str, Any], segment: int, scan: _RunScan,
              remaining: list[dict[str, Any]], files: list[dict[str, Any]],
              discarded: int, planned: int) -> dict[str, Any]:
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
                  discard_orphans: bool = True) -> ExportResult:
    """Pack ``run_id``'s results into a portable bundle and return what was written.

    Raises ``SecretFound`` (a ``CheckpointError``) if any packed byte looks like a
    credential, and leaves no file behind when it does.
    """
    runs_dir = Path(runs_dir)
    plan = _read_json(runs_dir / RUN_META_DIR / run_id / "plan.json")
    if plan is None:
        raise CheckpointError(
            f"no run {run_id} under {runs_dir} "
            f"(expected {runs_dir / RUN_META_DIR / run_id / 'plan.json'})")

    scan = _scan_run(runs_dir, run_id)
    # Before anything is collected: a partial episode must not be able to ship.
    moved = _discard_orphans(runs_dir, run_id, scan.orphans) if discard_orphans else []

    candidates = _candidate_files(runs_dir, run_id, scan.done)
    for arcname, source in candidates:
        if rule := denied_by(arcname):
            raise CheckpointError(
                f"refusing to export: {arcname} is denylisted ({rule})")
        if source.is_symlink():
            # A scoring file replaced by a link points somewhere we never inspected.
            raise CheckpointError(
                f"refusing to export: {arcname} is a symlink to "
                f"{os.readlink(source)!r}, not a scoring file")

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
                remaining=_remaining_units(plan, scan.done_keys), files=packed,
                discarded=_discarded_count(runs_dir, run_id),
                planned=len(plan.get("units") or []))
            # Written last so the per-file digests are final; readers address it by
            # name, and tar member order is not part of the format.
            _add_bytes(tar, MANIFEST_NAME,
                       json.dumps(manifest, indent=2).encode(),
                       datetime.now(timezone.utc).timestamp())
        tmp.replace(out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ExportResult(out_path, manifest, tuple(moved))


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
            manifest = json.loads(member.read())
    except (OSError, tarfile.TarError, KeyError, ValueError) as exc:
        raise CheckpointError(
            f"{bundle} is not a qualgent-bench checkpoint bundle: {exc}") from exc
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


def _bundle_payload(bundle: Path, manifest: Mapping[str, Any]) -> dict[str, bytes]:
    """Every member's bytes, checked against the manifest and both gates.

    A bundle comes from another machine, so this repeats the export's checks rather
    than trusting that they ran: unlisted members, denylisted paths, traversal, a
    changed digest and credential markers are all refusals here too.
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
    return payload


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

    payload = _bundle_payload(bundle, manifest)

    conflicts = [name for name, data in sorted(payload.items())
                 if (dest := runs_dir / name).exists()
                 and _read_bytes_or_raise(dest) != data]
    if conflicts:
        raise CheckpointError(
            f"refusing to import: run {run_id} already exists under {runs_dir} with "
            f"different contents ({len(conflicts)} file(s)): "
            f"{', '.join(conflicts[:5])}"
            f"{' …' if len(conflicts) > 5 else ''}. Import into an empty --runs-dir, "
            f"or move the existing run aside.")

    written: list[str] = []
    for name, data in sorted(payload.items()):
        dest = runs_dir / name
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
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        raise CheckpointError(f"could not write {path}: {exc}") from exc
    return path


def imported_episodes(runs_dir: Path | str, run_id: str) -> set[str]:
    """Episode dirs (relative to ``runs_dir``) that arrived in a bundle and therefore
    have no heavy artifacts on this machine. Empty when nothing was imported."""
    marker = _read_json(Path(runs_dir) / RUN_META_DIR / run_id / IMPORT_MARKER) or {}
    return {str(e) for e in (marker.get("episodes") or [])}


# ── show ──────────────────────────────────────────────────────────────────────


def run_summary(runs_dir: Path | str, run_id: str) -> dict[str, Any]:
    """The manifest view of a run still on disk — a bundle's ``checkpoint.json``
    shape minus ``files``, so ``checkpoint show`` prints one thing whether it was
    handed an archive or a run id."""
    runs_dir = Path(runs_dir)
    plan = _read_json(runs_dir / RUN_META_DIR / run_id / "plan.json")
    if plan is None:
        raise CheckpointError(
            f"no run {run_id} under {runs_dir} "
            f"(expected {runs_dir / RUN_META_DIR / run_id / 'plan.json'})")
    scan = _scan_run(runs_dir, run_id)
    remaining = _remaining_units(plan, scan.done_keys)
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
