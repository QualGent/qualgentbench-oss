"""The memory sequence runner: one track, stage by stage, for one memory stream (QUA-3492).

A *stream* is one (app, ordering, arm, rep) (`tracks.stream_key`). For each stage
`run-k` of the app's track (`memory.tracks`) the runner

1. **freezes M_k** (`prepare_stage_memory`): the memory root the stage's episodes read,
   frozen into a content-addressed snapshot store (`memory.snapshot`) and pointed at by
   `stages/run-k/<unit>.json`.
   - `static`:  the packaged seed + every trap placed at or before stage k
     (`tracks.apply_traps(..., through=True)`), rebuilt from the seed each stage.
   - `dynamic`: stage 1 is the seed (+ run-1 traps); stage k > 1 is the curator's
     M_k (`curate-(k-1)`) + the traps placed at stage k (earlier traps were carried
     forward, or invalidated, by the curator).
   - diff arms (`diff`/`hunt`) also get the stage's `<app>.changes.md`
     (`changes_file`), which the curator never edits.
   - `none`: no memory and no snapshot; the stage label is still recorded on every
     episode (`memory_stage`), so arms compare by stage.
2. **runs the stage's episodes** through a `StageRunner`: every slot of the stage
   (`Track.slots`), BOTH versions of each case (clean, and seeded when the case has a
   defect), with the stage's drift on in both (`Slot.flags`). Within a stage the
   snapshot is fixed, so episodes may run in parallel (the bench `run` lanes). Device
   and app state reset between episodes as in any run (D10); memory is the only
   thing carried over. Every memory episode must report the frozen snapshot hash.
3. **curates** (`dynamic` only, between stages): the curator (`memory.curator.curate`)
   reads M_k and ONLY the planned version of each stage-k case
   (`Track.curator_inputs`), never a later case (`Track.later_cases`), and writes
   M_{k+1}, frozen as `stages/curate-k/<unit>.json` with the curator's counts.
4. **records health** (`memory.metrics.unit_health`) for every pointer in
   `health.json`.

Determinism and resume. Every finished step is recorded in `sequence.json`; a re-run
skips finished steps and verifies their snapshots, and a stage whose episodes were
interrupted resumes its bench run (`run --resume`). The curator's date is fixed when the
sequence starts and its model answers are cached under `curator-cache/`
(`curator.CachedModel`), so redoing a stage (`redo=`) with the same inputs rebuilds
the identical snapshot.

On-disk layout of one sequence (`<out>`):

    sequence.json                       config + per-step state (SEQUENCE_FILE)
    store/objects/<hex>/...             snapshot objects (memory.snapshot)
    store/stages/run-<k>/<unit>.json    M_k as the stage's episodes read it
    store/stages/curate-<k>/<unit>.json M_{k+1} as the curator wrote it (+ counts)
    work/<stage>/memory/                scratch memory roots (rebuilt on demand)
    stages/run-<k>/memory.json          the stage's MemoryArm config (--memory-config)
    stages/run-<k>/episodes.json        the stage's episodes (EpisodeRecord.as_dict)
    stages/curate-<k>/episodes.json     what the curator was given (and what was not)
    stages/curate-<k>/curation.json     the curator's operation log
    curator-cache/                      cached curator model answers
    runs/                               the bench runs dir (default; one per stream)
    health.json                         unit_health rows, one per pointer

The lab kind (QUA-3493) runs the same steps as separate lab stages; it imports
`prepare_stage_memory`, `stage_episodes`, `curator_episodes` and `curate_stage`
directly and supplies its own episodes and snapshot transport.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .. import failures
from . import format as fmt
from . import render as _render
from . import snapshot as _snap
from . import tracks as trk
from .arm import (
    DEFAULT_ROOT,
    DIFF_NONE,
    MEMORY_BUDGETS,
    MEMORY_DIFFS,
    MEMORY_MODES,
    MODE_DYNAMIC,
    MODE_NONE,
    MODE_STATIC,
    PROPOSALS_FILENAME,
    MemoryArm,
)

FORMAT = 1
SEQUENCE_FILE = "sequence.json"
STORE_DIR = "store"
WORK_DIR = "work"
STAGES_DIR = "stages"
RUNS_DIR = "runs"
CACHE_DIR = "curator-cache"
HEALTH_FILE = "health.json"
MEMORY_CONFIG = "memory.json"
EPISODES_FILE = "episodes.json"
CURATION_FILE = "curation.json"

CLEAN, SEEDED = trk.CLEAN, trk.SEEDED
DONE = "done"


class SequenceError(RuntimeError):
    pass


class SequenceStopped(SequenceError):
    """The bench run stopped resumably (provider budget, exit 75). Re-run to resume."""


# ── the arm ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ArmSpec:
    """The arm of one stream, before any stage: mode × budget × diff. `label` is the
    path-safe arm key used in `stream_key` and the snapshot unit."""
    mode: str = MODE_NONE
    budget: str = _render.SMALL
    diff: str = DIFF_NONE
    org: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in MEMORY_MODES:
            raise SequenceError(f"memory mode must be one of {MEMORY_MODES}, not {self.mode!r}")
        if self.budget not in MEMORY_BUDGETS:
            raise SequenceError(f"memory budget must be one of {MEMORY_BUDGETS}")
        if self.diff not in MEMORY_DIFFS:
            raise SequenceError(f"memory diff must be one of {MEMORY_DIFFS}")
        if self.mode == MODE_NONE and self.diff != DIFF_NONE:
            raise SequenceError("the no-memory arm has no diff")

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_NONE

    @property
    def curated(self) -> bool:
        return self.mode == MODE_DYNAMIC

    @property
    def include_changes(self) -> bool:
        return self.diff != DIFF_NONE

    @property
    def label(self) -> str:
        return "none" if not self.enabled else f"{self.mode}-{self.budget}-{self.diff}"

    def memory_arm(self, stage: str, snapshot: Path | None) -> MemoryArm:
        """The episode arm for one stage (`run --memory-config`)."""
        if not self.enabled:
            return MemoryArm(mode=MODE_NONE, stage=stage)
        return MemoryArm(mode=self.mode, budget=self.budget, diff=self.diff,
                         snapshot=snapshot, org=self.org, stage=stage)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "budget": self.budget, "diff": self.diff, "org": self.org}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ArmSpec:
        return cls(mode=str(d.get("mode") or MODE_NONE),
                   budget=str(d.get("budget") or _render.SMALL),
                   diff=str(d.get("diff") or DIFF_NONE), org=d.get("org") or None)


def unit_key(app: str, ordering: str, arm: ArmSpec, rep: int) -> str:
    """The snapshot-store unit of one stream (`tracks.stream_key`)."""
    return trk.stream_key(app, ordering, arm.label, rep)


# ── episodes ─────────────────────────────────────────────────────────────────

@dataclass
class EpisodeRecord:
    """One finished episode of a stage, as the runner and the curator need it.

    `episode_id` is the curator's episode id and the join key of
    `memory.metrics.load_curation_log`: the result's `artifact_dir` (relative to the
    runs dir) for local runs; the lab passes its own unit's artifact id."""
    case: str
    version: str
    task_id: str
    episode_id: str
    episode_dir: Path | None
    metrics: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] | None = None          # result.provenance["memory"]
    started_at: str = ""
    run_id: str = ""

    @property
    def proposals_path(self) -> Path | None:
        return (self.episode_dir / "workspace" / PROPOSALS_FILENAME
                if self.episode_dir is not None else None)

    @property
    def snapshot_hash(self) -> str | None:
        return (self.memory or {}).get("snapshot_hash") or self.metrics.get("snapshot_hash")

    @property
    def excluded(self) -> bool:
        return failures.is_excluded(self.metrics)

    @classmethod
    def from_result(cls, result: Any, runs_dir: Path | str) -> EpisodeRecord:
        """From a `RunResult` (or a result.json dict) of a journey episode."""
        from ..result import resolve_artifact_dir

        get = (result.get if isinstance(result, Mapping)
               else lambda k, d=None: getattr(result, k, d))
        metrics = dict(get("metrics") or {})
        prov = get("provenance") or {}
        art = get("artifact_dir") or ""
        return cls(case=str(metrics.get("case_id") or ""),
                   version=str(metrics.get("version") or ""),
                   task_id=str(get("task_id") or ""), episode_id=str(art),
                   episode_dir=resolve_artifact_dir(runs_dir, art) if art else None,
                   metrics=metrics, memory=prov.get("memory"),
                   started_at=str(get("started_at") or ""), run_id=str(get("run_id") or ""))

    def as_dict(self) -> dict[str, Any]:
        """The episodes.json row: what the stage measured, not the whole result."""
        m = self.metrics
        return {"case": self.case, "version": self.version, "task_id": self.task_id,
                "episode_id": self.episode_id,
                "episode_dir": str(self.episode_dir) if self.episode_dir else None,
                "run_id": self.run_id, "started_at": self.started_at,
                "snapshot_hash": self.snapshot_hash, "excluded": self.excluded,
                "completed": m.get("completed"), "bugs_found": m.get("bugs_found"),
                "false_reports": m.get("false_reports"),
                "memory_stage": m.get("memory_stage"),
                "memory_condition": m.get("memory_condition"),
                "memory_proposals_count": m.get("memory_proposals_count"),
                "memory_proposals_sha256": m.get("memory_proposals_sha256"),
                "drift": (self.memory or {}).get("drift") or m.get("drift")}


def expected_episodes(slots: Iterable[trk.Slot]) -> list[tuple[str, str]]:
    """(case, version) of every episode a stage runs: both versions of each slot (the
    seeded one only when the case has a defect)."""
    out = []
    for s in slots:
        out.append((s.case, CLEAN))
        if s.bugs:
            out.append((s.case, SEEDED))
    return out


def pick_latest(records: Iterable[EpisodeRecord]) -> dict[tuple[str, str], EpisodeRecord]:
    """One record per (case, version): the latest non-excluded attempt, else the latest
    attempt (a retried rate-limited episode keeps both on disk)."""
    best: dict[tuple[str, str], EpisodeRecord] = {}
    for r in sorted(records, key=lambda r: r.started_at):
        key = (r.case, r.version)
        prev = best.get(key)
        if prev is None or not (r.excluded and not prev.excluded):
            best[key] = r
    return best


# ── stage memory ─────────────────────────────────────────────────────────────

def changes_file(changes_dir: Path | str | None, track: trk.Track, stage: str) -> Path:
    """The stage's `<app>.changes.md` for a diff arm: `<dir>/<stage>.changes.md`, else
    the stage's Qualock build (`<dir>/<label>.changes.md`, `Track.qualock_build_for`).
    Missing raises: a diff arm never runs without its diff."""
    if changes_dir is None:
        raise FileNotFoundError(f"{track.app}: a diff arm needs a changes dir "
                                f"(<dir>/{stage}.changes.md)")
    d = Path(changes_dir)
    cands = [d / f"{stage}.changes.md",
             d / f"{track.qualock_build_for(stage).label}.changes.md"]
    for p in cands:
        if p.is_file():
            return p
    raise FileNotFoundError(f"{track.app} {stage}: no changes file ({', '.join(map(str, cands))})")


def _fresh(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _copy_files(src: Path, dst: Path, files: Iterable[str]) -> None:
    for rel in files:
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, p)


@dataclass
class StageMemory:
    stage: str
    unit: str
    snapshot: _snap.Snapshot
    parent: str | None
    traps: list[str]

    @property
    def memory_root(self) -> Path:
        return self.snapshot.memory_root


def prepare_stage_memory(*, track: trk.Track, stage: str, arm: ArmSpec, unit: str,
                         store: Path | str, work: Path | str, org: str,
                         seed_root: Path | str = DEFAULT_ROOT,
                         curated_root: Path | str | None = None,
                         curated_snapshot: str | None = None,
                         changes_dir: Path | str | None = None,
                         extra: Mapping[str, Any] | None = None) -> StageMemory:
    """Build and freeze M_k for one stream (memory arms only).

    `curated_root` is the curator's output for this stage (dynamic, k > 1;
    `curated_snapshot` its hash, recorded as the pointer's parent). Otherwise the
    stage starts from `seed_root`. Writes `work` (emptied first) and the store
    pointer `stages/<stage>/<unit>.json`."""
    if not arm.enabled:
        raise SequenceError("the no-memory arm has no stage memory")
    k = trk.stage_index(stage)
    if arm.curated and k > 1 and curated_root is None:
        raise SequenceError(f"{stage}: a dynamic arm needs curate-{k - 1}'s memory; it "
                            "never falls back to the seed")
    work = _fresh(Path(work))
    base = Path(curated_root) if curated_root is not None else Path(seed_root)
    _copy_files(base, work, _snap.unit_files(org, track.app))
    # Static arms rebuild from the seed each stage, so they carry every trap so far;
    # dynamic arms add only this stage's (the curator carried the earlier ones).
    through = arm.mode == MODE_STATIC or curated_root is None
    added = trk.apply_traps(work, track, stage, through=through)
    files = _snap.unit_files(org, track.app, include_changes=arm.include_changes)
    if arm.include_changes:
        dst = fmt.changes_path(work, org, track.app)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(changes_file(changes_dir, track, stage), dst)
    meta = {**(extra or {}), "app": track.app, "kind": "run", "traps": added,
            "include_changes": arm.include_changes}
    snap = _snap.freeze(work, store, files=files, stage=stage, unit=unit,
                        parent=curated_snapshot, extra=meta)
    return StageMemory(stage, unit, snap, curated_snapshot, added)


def curator_episodes(track: trk.Track, ordering: str, stage: str,
                     records: Iterable[EpisodeRecord]) -> tuple[list, list[dict[str, Any]]]:
    """The curator's `Episode`s for `curate-k`: the planned version of every stage-k
    case (`Track.curator_inputs`) and nothing else, one attempt each (`pick_latest`).
    Returns (episodes, manifest rows) — the manifest also names the planned episodes
    that are missing and the unplanned ones left out."""
    from .curator import Episode

    planned = set(track.curator_inputs(ordering, stage))
    later = set(track.later_cases(ordering, stage))
    latest = pick_latest(records)
    episodes, manifest = [], []
    for (case, version), r in sorted(latest.items()):
        row = {"case": case, "version": version, "episode_id": r.episode_id}
        if (case, version) not in planned:
            manifest.append({**row, "used": False, "why": "not the planned version"})
            continue
        if case in later:                      # cannot happen on a valid track
            manifest.append({**row, "used": False, "why": "case runs in a later stage"})
            continue
        path = r.proposals_path
        ep = Episode.from_files(r.episode_id, path if path is not None else Path(os.devnull),
                                r.metrics, case=case)
        episodes.append(ep)
        manifest.append({**row, "used": True, "verified": ep.verified,
                         "verified_reason": ep.verified_reason,
                         "proposals": str(path) if path else None,
                         "proposals_missing": ep.proposals.missing})
    for case, version in sorted(planned - set(latest)):
        manifest.append({"case": case, "version": version, "episode_id": None,
                         "used": False, "why": "missing: no episode finished"})
    return episodes, manifest


@dataclass
class CurateOutcome:
    stage: str                 # curate-k
    snapshot: _snap.Snapshot
    parent: str
    counts: dict[str, int]
    carried_forward: bool
    log_path: Path | None


def curate_stage(*, track: trk.Track, ordering: str, stage: str, unit: str,
                 memory_root: Path | str, out_root: Path | str, store: Path | str,
                 org: str, episodes: list, model: Any, date: str,
                 log_path: Path | str | None = None,
                 extra: Mapping[str, Any] | None = None) -> CurateOutcome:
    """Run the curator on stage `run-k`'s episodes (from `curator_episodes`) and
    freeze M_{k+1} as `stages/curate-k/<unit>.json` with the curator's counts (read
    by `memory.metrics.unit_health`). `out_root` is emptied first. The changes file,
    when M_k has one, is not carried: the next stage brings its own."""
    from .curator import curate

    out = Path(out_root)
    if out.exists():
        shutil.rmtree(out)
    res = curate(memory_root, out, org_key=org, app_key=track.app, episodes=episodes,
                 model=model, stage=stage, date=date,
                 exclude_cases=track.later_cases(ordering, stage), log_path=log_path)
    cstage = trk.curate_stage(stage)
    meta = {**(extra or {}), "app": track.app, "kind": "curate", "counts": res.counts,
            "carried_forward": res.carried_forward}
    snap = _snap.freeze(out, store, files=_snap.unit_files(org, track.app), stage=cstage,
                        unit=unit, parent=res.parent, extra=meta)
    if snap.hash != res.snapshot:
        raise SequenceError(f"{cstage}: frozen {snap.hash} != curator {res.snapshot}")
    return CurateOutcome(cstage, snap, res.parent, res.counts, res.carried_forward,
                         Path(log_path) if log_path else None)


def stage_episodes(records: Iterable[EpisodeRecord], slots: Iterable[trk.Slot], *,
                   snapshot: str | None) -> tuple[list[EpisodeRecord], list[str]]:
    """Check a stage's episodes against its plan: one record per expected
    (case, version) (`pick_latest`), and — for a memory arm — every episode read the
    frozen snapshot. Returns (records, problems); problems name missing episodes, an
    unplanned case and a snapshot mismatch."""
    records = list(records)
    want = expected_episodes(slots)
    latest = pick_latest(records)
    problems = [f"missing episode {c} ({v})" for c, v in want if (c, v) not in latest]
    problems += [f"unplanned episode {c} ({v})" for c, v in latest if (c, v) not in want]
    if snapshot is not None:
        for r in latest.values():
            if r.snapshot_hash != snapshot:
                problems.append(f"{r.case} ({r.version}) read snapshot {r.snapshot_hash}, "
                                f"not the stage's {snapshot}")
    return [latest[k] for k in want if k in latest], problems


# ── stage runners ────────────────────────────────────────────────────────────

@dataclass
class StageRequest:
    """What a `StageRunner` must run: every expected episode of one stage."""
    app: str
    stage: str
    ordering: str
    slots: list[trk.Slot]
    drift: tuple[str, ...]
    arm: MemoryArm                 # with the stage label and, for memory arms, snapshot
    memory_config: Path            # the same arm as a --memory-config file
    runs_dir: Path
    resume_run_id: str | None = None
    on_run_id: Callable[[str], None] = lambda run_id: None

    @property
    def cases(self) -> list[str]:
        return [s.case for s in self.slots]

    @property
    def expected(self) -> list[tuple[str, str]]:
        return expected_episodes(self.slots)

    def flags(self, case: str, version: str) -> list[str]:
        """The flag ids an episode of (case, version) must write (`Slot.flags`)."""
        for s in self.slots:
            if s.case == case:
                return s.flags(version)
        raise KeyError(case)


class StageRunner(Protocol):
    def run_stage(self, req: StageRequest) -> list[EpisodeRecord]: ...


@dataclass
class CliStageRunner:
    """Runs a stage as one bench run: `qualgent-bench run --mode journey --app <app>
    --case <cases> --drift <stage drift> --memory-config <stage memory.json>` (both
    versions of each case are planned; `--devices`/`--lanes` run them in parallel).
    A stage interrupted mid-run resumes with `run --resume <run id>`."""
    agent: str
    model: str | None = None
    mcp_server: str | None = None
    devices: str | None = None
    lanes: int | None = None
    extra_args: tuple[str, ...] = ()
    command: tuple[str, ...] = (sys.executable, "-m", "qualgentbench.cli")
    env: Mapping[str, str] | None = None

    def args(self, req: StageRequest, run_id_file: Path) -> list[str]:
        common = ["--runs-dir", str(req.runs_dir), "--yes", "--plain",
                  "--run-id-file", str(run_id_file)]
        if self.mcp_server:
            common += ["--mcp-server", self.mcp_server]
        if self.devices:
            common += ["--devices", self.devices]
        if self.lanes:
            common += ["--lanes", str(self.lanes)]
        if req.resume_run_id:
            return [*self.command, "run", "--resume", req.resume_run_id, *common,
                    *self.extra_args]
        out = [*self.command, "run", "--mode", "journey", "--agent", self.agent,
               "--app", req.app, "--case", ",".join(req.cases),
               "--memory-config", str(req.memory_config), *common]
        if self.model:
            out += ["--models", self.model]
        if req.drift:
            out += ["--drift", ",".join(req.drift)]
        return [*out, *self.extra_args]

    def run_stage(self, req: StageRequest) -> list[EpisodeRecord]:
        from ..leaderboard import load_results

        req.runs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            rid_file = Path(tmp) / "run_id"
            proc = subprocess.run(self.args(req, rid_file), check=False,
                                  env={**os.environ, **(self.env or {})})
            run_id = _read_run_id(rid_file) or req.resume_run_id
        if run_id:
            req.on_run_id(run_id)
        if proc.returncode == 75:
            raise SequenceStopped(f"{req.stage}: bench run {run_id} stopped (provider "
                                  "budget); re-run the sequence to resume it")
        if proc.returncode != 0:
            raise SequenceError(f"{req.stage}: bench run {run_id or '?'} exited "
                                f"{proc.returncode}; re-run the sequence to resume it")
        if not run_id:
            raise SequenceError(f"{req.stage}: the bench run wrote no run id")
        return [EpisodeRecord.from_result(r, req.runs_dir)
                for r in load_results(req.runs_dir, run_id=run_id)
                if r.task_type == "journey_case"]


def _read_run_id(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            return str(json.loads(text).get("run_id") or "") or None
        except json.JSONDecodeError:
            return None
    return text.split()[0]


# ── the sequence ─────────────────────────────────────────────────────────────

ModelFactory = Callable[[Path], Any]     # cache dir -> curator model (or None)


@dataclass
class SequenceConfig:
    app: str
    ordering: str
    rep: int
    arm: ArmSpec
    date: str                                  # the curator's YYYY-MM-DD, fixed per run
    seed_root: Path = DEFAULT_ROOT
    changes_dir: Path | None = None
    through: str | None = None                 # last stage to run (default: all)
    curator_model: str | None = None           # recorded; the factory builds the model

    def as_dict(self) -> dict[str, Any]:
        return {"app": self.app, "ordering": self.ordering, "rep": self.rep,
                "arm": self.arm.as_dict(), "date": self.date,
                "seed_root": str(self.seed_root),
                "changes_dir": str(self.changes_dir) if self.changes_dir else None,
                "through": self.through, "curator_model": self.curator_model}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> SequenceConfig:
        return cls(app=str(d["app"]), ordering=str(d["ordering"]), rep=int(d["rep"]),
                   arm=ArmSpec.from_dict(d.get("arm") or {}), date=str(d["date"]),
                   seed_root=Path(d.get("seed_root") or DEFAULT_ROOT),
                   changes_dir=Path(d["changes_dir"]) if d.get("changes_dir") else None,
                   through=d.get("through"), curator_model=d.get("curator_model"))


# The config keys a resume must agree on: what the stream measures.
_IDENTITY = ("app", "ordering", "rep", "arm", "seed_root", "changes_dir")


@dataclass
class Sequence:
    """One stream's sequence directory. `run()` executes (or resumes) every step."""
    out: Path
    config: SequenceConfig
    runner: StageRunner
    track: trk.Track | None = None
    model_factory: ModelFactory | None = None
    runs_dir: Path | None = None
    log: Callable[[str], None] = lambda msg: None
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.out = Path(self.out)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.config.date):
            raise SequenceError(f"date {self.config.date!r} is not YYYY-MM-DD")
        self.track = self.track or trk.load_track(self.config.app)
        if self.track.app != self.config.app:
            raise SequenceError(f"track is {self.track.app}, not {self.config.app}")
        self.track.assignment(self.config.ordering)      # unknown ordering raises
        self.runs_dir = Path(self.runs_dir or self.out / RUNS_DIR)
        self.org = self.config.arm.org or fmt.find_org(self.config.seed_root, self.config.app)
        self.unit = unit_key(self.config.app, self.config.ordering, self.config.arm,
                             self.config.rep)
        self._load_state()

    # ── paths ──
    @property
    def store(self) -> Path:
        return self.out / STORE_DIR

    def stage_dir(self, stage: str) -> Path:
        return self.out / STAGES_DIR / stage

    def work_dir(self, stage: str) -> Path:
        return self.out / WORK_DIR / stage / "memory"

    @property
    def stages(self) -> list[trk.Stage]:
        out = []
        for st in self.track.stages:
            out.append(st)
            if self.config.through and st.id == self.config.through:
                break
        else:
            if self.config.through and self.config.through not in self.track.stage_ids:
                raise SequenceError(f"{self.config.app}: no stage {self.config.through!r}")
        return out

    def steps(self) -> list[str]:
        """run-1, curate-1, run-2, ... (curate steps only for a dynamic arm, and none
        after the last stage)."""
        out = []
        stages = self.stages
        for i, st in enumerate(stages):
            out.append(st.id)
            if self.config.arm.curated and i < len(stages) - 1:
                out.append(trk.curate_stage(st.id))
        return out

    # ── state ──
    def _load_state(self) -> None:
        p = self.out / SEQUENCE_FILE
        if not p.is_file():
            self.state = {"format": FORMAT, "unit": self.unit,
                          "config": self.config.as_dict(), "steps": {}}
            return
        doc = json.loads(p.read_text(encoding="utf-8"))
        have = doc.get("config") or {}
        mine = self.config.as_dict()
        diff = [k for k in _IDENTITY if have.get(k) != mine.get(k)]
        if diff:
            raise SequenceError(f"{p} is a different sequence ({', '.join(diff)} differ); "
                                "use another --out")
        # The curator date is the sequence's, not today's: a resume keeps it.
        self.config.date = str(have.get("date") or self.config.date)
        doc["config"] = {**mine, "date": self.config.date}
        self.state = doc
        self.state.setdefault("steps", {})

    def _save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        p = self.out / SEQUENCE_FILE
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(p)

    def step(self, name: str) -> dict[str, Any]:
        return self.state["steps"].setdefault(name, {})

    def done(self, name: str) -> bool:
        return self.state["steps"].get(name, {}).get("status") == DONE

    def redo(self, name: str) -> list[str]:
        """Forget `name` and every later step (their store objects stay; pointers are
        rewritten when the steps run again). Returns the forgotten steps."""
        steps = self.steps()
        if name not in steps:
            raise SequenceError(f"no step {name!r} (steps: {', '.join(steps)})")
        gone = steps[steps.index(name):]
        for s in gone:
            self.state["steps"].pop(s, None)
        self._save()
        return gone

    # ── running ──
    def _extra(self) -> dict[str, Any]:
        c = self.config
        return {"arm": c.arm.label, "rep": c.rep, "ordering": c.ordering,
                "stream": self.unit}

    def _memory_for(self, stage: str) -> StageMemory | None:
        """M_k for `stage`: the recorded snapshot (verified) or a new freeze."""
        arm = self.config.arm
        if not arm.enabled:
            return None
        st = self.step(stage)
        if st.get("snapshot"):
            snap = _snap.open_snapshot(self.store, st["snapshot"])
            ptr = _snap.stage_pointer(self.store, stage, self.unit)
            if ptr["snapshot"] != snap.hash:
                raise SequenceError(f"{stage}: store pointer {ptr['snapshot']} != recorded "
                                    f"{snap.hash}")
            return StageMemory(stage, self.unit, snap, st.get("parent"), st.get("traps", []))
        k = trk.stage_index(stage)
        curated_root = curated_hash = None
        if arm.curated and k > 1:
            prev = trk.curate_stage(f"run-{k - 1}")
            if not self.done(prev):
                raise SequenceError(f"{stage} needs {prev}, which has not run")
            curated_hash = self.step(prev)["snapshot"]
            curated_root = _snap.open_snapshot(self.store, curated_hash).memory_root
        mem = prepare_stage_memory(
            track=self.track, stage=stage, arm=arm, unit=self.unit, store=self.store,
            work=self.work_dir(stage), org=self.org, seed_root=self.config.seed_root,
            curated_root=curated_root, curated_snapshot=curated_hash,
            changes_dir=self.config.changes_dir, extra=self._extra())
        st.update(snapshot=mem.snapshot.hash, parent=mem.parent, traps=mem.traps)
        self._save()
        self.log(f"{stage}: froze M_{k} {mem.snapshot.hash} (traps {mem.traps or 'none'})")
        return mem

    def _run_stage(self, stage: trk.Stage) -> None:
        name = stage.id
        if self.done(name):
            self._memory_for(name)                     # verifies the snapshot
            return
        mem = self._memory_for(name)
        arm = self.config.arm.memory_arm(name, mem.memory_root if mem else None)
        sdir = self.stage_dir(name)
        sdir.mkdir(parents=True, exist_ok=True)
        cfg_path = sdir / MEMORY_CONFIG
        cfg_path.write_text(json.dumps(arm.as_dict(), indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        slots = self.track.slots(self.config.ordering, name)
        st = self.step(name)

        def on_run_id(run_id: str) -> None:
            st["run_id"] = run_id
            self._save()

        req = StageRequest(app=self.config.app, stage=name, ordering=self.config.ordering,
                           slots=slots, drift=tuple(stage.drift), arm=arm,
                           memory_config=cfg_path, runs_dir=self.runs_dir,
                           resume_run_id=st.get("run_id"), on_run_id=on_run_id)
        self.log(f"{name}: running {len(req.expected)} episode(s) of {len(slots)} case(s)"
                 + (f", drift {list(stage.drift)}" if stage.drift else "")
                 + (f" (resuming {req.resume_run_id})" if req.resume_run_id else ""))
        records = self.runner.run_stage(req)
        kept, problems = stage_episodes(records, slots,
                                        snapshot=mem.snapshot.hash if mem else None)
        rows = [r.as_dict() for r in kept]
        (sdir / EPISODES_FILE).write_text(
            json.dumps({"stage": name, "unit": self.unit, "snapshot":
                        mem.snapshot.hash if mem else None, "drift": list(stage.drift),
                        "episodes": rows, "problems": problems}, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")
        mismatched = [p for p in problems if "read snapshot" in p or "unplanned" in p]
        if mismatched:
            raise SequenceError(f"{name}: " + "; ".join(mismatched))
        st.update(status=DONE, episodes=len(kept), missing=len(problems),
                  problems=problems)
        self._save()
        self.log(f"{name}: {len(kept)} episode(s)"
                 + (f", {len(problems)} missing" if problems else ""))

    def _curate(self, stage: trk.Stage) -> None:
        name = trk.curate_stage(stage.id)
        if self.done(name):
            _snap.open_snapshot(self.store, self.step(name)["snapshot"])
            return
        rows = json.loads((self.stage_dir(stage.id) / EPISODES_FILE).read_text())
        records = [EpisodeRecord(case=r["case"], version=r["version"], task_id=r["task_id"],
                                 episode_id=r["episode_id"],
                                 episode_dir=Path(r["episode_dir"]) if r["episode_dir"] else None,
                                 started_at=r.get("started_at") or "")
                   for r in rows["episodes"]]
        # Metrics come from the result.json beside each episode (the scored verdict),
        # not from episodes.json's summary.
        for r in records:
            r.metrics = _result_metrics(r.episode_dir)
        episodes, manifest = curator_episodes(self.track, self.config.ordering, stage.id,
                                              records)
        cdir = self.stage_dir(name)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / EPISODES_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True)
                                          + "\n", encoding="utf-8")
        cache = self.out / CACHE_DIR
        model = self.model_factory(cache) if self.model_factory else None
        m_k = _snap.open_snapshot(self.store, self.step(stage.id)["snapshot"])
        try:
            res = curate_stage(track=self.track, ordering=self.config.ordering,
                               stage=stage.id, unit=self.unit, memory_root=m_k.memory_root,
                               out_root=self.work_dir(name), store=self.store, org=self.org,
                               episodes=episodes, model=model, date=self.config.date,
                               log_path=cdir / CURATION_FILE, extra=self._extra())
        except Exception as e:
            from .curator import CuratorError
            if isinstance(e, CuratorError):
                raise SequenceError(f"{name}: {e}") from e
            raise
        self.step(name).update(status=DONE, snapshot=res.snapshot.hash, parent=res.parent,
                               counts=res.counts, carried_forward=res.carried_forward)
        self._save()
        self.log(f"{name}: M_{stage.index + 1} {res.snapshot.hash} "
                 f"({'carried forward' if res.carried_forward else _counts_line(res.counts)})")

    def write_health(self) -> list[dict[str, Any]]:
        from .metrics import unit_health

        rows = []
        if self.config.arm.enabled:
            for name in self.steps():
                if self.state["steps"].get(name, {}).get("snapshot"):
                    rows.append(unit_health(self.store, name, self.unit, app=self.config.app,
                                            org=self.org))
        (self.out / HEALTH_FILE).write_text(json.dumps(rows, indent=2, sort_keys=True,
                                                       default=list) + "\n",
                                            encoding="utf-8")
        return rows

    def run(self) -> dict[str, Any]:
        """Run (or resume) every step; return the state."""
        self._save()
        by_id = {s.id: s for s in self.stages}
        for name in self.steps():
            if name.startswith("run-"):
                self._run_stage(by_id[name])
            else:
                self._curate(by_id[name.replace("curate-", "run-", 1)])
            self.write_health()
        self.state["complete"] = all(self.done(s) for s in self.steps())
        self._save()
        return self.state


def _result_metrics(episode_dir: Path | None) -> dict[str, Any]:
    if episode_dir is None:
        return {}
    try:
        doc = json.loads((episode_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(doc.get("metrics") or {})


def _counts_line(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {counts.get(k, 0)}" for k in ("ADD", "UPDATE", "INVALIDATE", "NOOP"))


@dataclass
class _NoModel:
    """Stands in for the curator model when none is configured: cached answers (and
    gate-only stages) still curate; a new question fails loudly."""
    id: str

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        from .curator import MODEL_ENV, CuratorError

        raise CuratorError(f"the curator needs model {self.id!r} for an uncached request; "
                           f"pass --curator-model or set {MODEL_ENV}")


def model_factory(model_id: str | None) -> ModelFactory:
    """Curator models for a sequence, always behind `CachedModel(cache_dir)`. With an id
    the model is `AnthropicModel(id)` (needs `ANTHROPIC_API_KEY` only on a cache miss);
    without one, only cached answers are available. The cache key includes the model
    id, so a resume must use the id the sequence recorded."""
    from .curator import AnthropicModel, CachedModel

    def build(cache_dir: Path) -> Any:
        inner = AnthropicModel(model=model_id) if model_id else _NoModel("none")
        return CachedModel(inner, Path(cache_dir))
    return build


def today() -> str:
    return datetime.now(tz=UTC).date().isoformat()


__all__ = ["ArmSpec", "model_factory", "today", "CliStageRunner", "CurateOutcome", "EpisodeRecord", "Sequence",
           "SequenceConfig", "SequenceError", "SequenceStopped", "StageMemory",
           "StageRequest", "StageRunner", "changes_file", "curate_stage",
           "curator_episodes", "expected_episodes", "pick_latest", "prepare_stage_memory",
           "stage_episodes", "unit_key"]
