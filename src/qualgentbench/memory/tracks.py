"""App update tracks, regression pairs and trap memories (QUA-3491, spec §5.1-5.2).

A **track** (`data/memory/tracks/<app>.yaml`) is the stage plan one memory experiment
runs on one app: stages `run-1 … run-N`, which test cases run in each, which drift
flags are on, which cases run as the planned *seeded* version (regression-pair targets
and the cases a `missed_bug` trap hides a defect on), the `pairs:` links, and the trap
ids placed in the track. Memory is built on v1 behavior (`run-1` is always the clean,
undrifted app) and tested on later stages where things changed.

File shape (`schema: 1`):

    app: markor
    stages:                       # run-1..run-N, N in 4..5; drift is the FULL set on
      - {id: run-1, drift: []}    # in that stage and never shrinks (a UI change stays)
      - {id: run-4, drift: [markor-drift-fab-label, ...]}
    fixed:                        # same stage in every ordering: pair and trap cases
      run-1: [markor-todo-set-priority, ...]
    orderings:                    # the free cases, shuffled per ordering (>= 2)
      a: {run-3: [markor-new-note-from-template], run-4: [...]}
      b: {run-3: [...], run-4: [...]}
    seeded: [markor-todo-complete-task, ...]   # = pair targets + missed_bug trap targets
    pairs:
      - id: markor-pair-todo
        source: markor-todo-set-priority       # runs clean in an earlier stage
        target: markor-todo-complete-task      # runs seeded later, defect on
        bug: todo-toggle-done-crash            # the target case's own defect
        route: [To-Do, Priority]               # tap anchors both routes share
    traps: [markor-trap-folder-fixed, ...]     # ids from data/memory/traps/<app>.yaml

Every case of the app runs **exactly once** per ordering (both versions run in the lab
block, only the planned version's proposals reach the curator). That is the leakage
rule (spec §5.1) in its simplest provable form: the snapshot a stage-k episode reads is
curated only from stages < k, and no case appears in two stages, so no episode can
see memory written from its own test case (`leakage_problems`). Memory streams are
kept apart per (app, ordering, arm, rep) (`stream_key`).

A **trap** (`data/memory/traps/<app>.yaml`, a `traps:` list) is a hand-written wrong
entry placed in M_k. It uses exactly the shape `memory.metrics.Trap` reads (id, app,
effect, stage, entry_id, bug, cases, markers) plus the entry itself:

    - id: markor-trap-folder-fixed
      app: markor
      effect: missed_bug          # missed_bug | false_report | not_completed
      stage: run-2                # the stage whose memory first carries it; its cases run there
      entry_id: bug-901           # reserved 9NN ids, never in the hand-written base files
      bug: new-folder-blocks-main-thread
      cases: [markor-new-folder-with-note]
      markers: []                 # false_report: words tying an unmatched report to it
      drift: markor-drift-save-overflow   # not_completed: the drift flag that made it stale
      text: "..."                 # the entry as the agent reads it
      lint_allow: [symptom]       # the only lint rule the deliberate wrong fact may break
      note: why it is wrong

Traps never edit the packaged base memory: `apply_traps` adds NEW entries to a copy
(the stage's memory root), with the declared id. Within one (app, entry type), a later
stage's trap has a LOWER number, so the curator's `next_id()` (max + 1) after an
earlier trap can never take a later trap's id.

Flags: an episode writes its own case's defect (seeded version only) plus the stage's
drift ids (`Slot.flags`). A Qualock build for a stage (QUA-3495) runs the whole stage
flag set at once: every planned-seeded case's defects of that stage plus the stage's
drift (`Track.stage_flags`, `Track.qualock_builds`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

from . import format as fmt

SCHEMA = 1
DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "memory"
TRACKS_DIR = DATA_ROOT / "tracks"
TRAPS_DIR = DATA_ROOT / "traps"

MIN_STAGES, MAX_STAGES = 4, 5
MIN_STAGE_CASES, MAX_STAGE_CASES = 2, 3       # x2 versions = 4-6 episodes per stage
MIN_ORDERINGS = 2
TRAP_ID_FLOOR = 900                            # trap entry ids are <type>-9NN
LINT_ALLOWABLE = ("symptom",)                  # never defect-id, qgb-flag, instruction
TRAP_META = {"src": fmt.HUMAN_SRC, "confirmed": "2026-11-17", "seen": 2, "contra": 0}

CLEAN, SEEDED = "clean", "seeded"
_STAGE_RE = re.compile(r"^run-(\d+)$")


class TrackError(ValueError):
    pass


def stage_index(stage: str) -> int:
    m = _STAGE_RE.match(str(stage))
    if not m:
        raise TrackError(f"stage {stage!r} is not run-<k>")
    return int(m.group(1))


def curate_stage(stage: str) -> str:
    """`run-k` -> `curate-k`: the stage whose output is M_{k+1}."""
    return f"curate-{stage_index(stage)}"


def stream_key(app: str, ordering: str, arm: str, rep: int | str) -> str:
    """One dynamic memory stream. Orderings, arms and reps never share memory."""
    return f"{app}__{ordering}__{arm}__r{rep}"


# ── traps ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrapDef:
    """A trap definition: the `metrics.Trap` fields plus the entry text."""
    id: str
    app: str
    effect: str
    stage: str
    entry_id: str
    text: str
    bug: str | None = None
    cases: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    drift: str | None = None
    lint_allow: tuple[str, ...] = ()
    note: str = ""

    @property
    def entry_type(self) -> str:
        return self.entry_id.rsplit("-", 1)[0]

    @property
    def entry_number(self) -> int:
        return int(self.entry_id.rsplit("-", 1)[1])

    def metric(self):
        """The `memory.metrics.Trap` the metrics read from the same file."""
        from .metrics import Trap

        return Trap(id=self.id, app=self.app, effect=self.effect, stage=self.stage,
                    entry_id=self.entry_id, bug=self.bug, cases=self.cases,
                    markers=self.markers)

    @classmethod
    def from_dict(cls, d: dict, where: str = "") -> TrapDef:
        from .metrics import MetricsInputError, Trap

        try:
            Trap.from_dict(d, where)          # the metrics' own checks, same messages
        except MetricsInputError as e:
            raise TrackError(str(e)) from None
        for k in ("stage", "entry_id", "text"):
            if not d.get(k):
                raise TrackError(f"{where}: trap {d.get('id')}: missing `{k}`")
        if not re.fullmatch(fmt.ID_RE, str(d["entry_id"])):
            raise TrackError(f"{where}: trap {d['id']}: entry_id {d['entry_id']!r} "
                             f"is not <type>-NNN")
        return cls(id=str(d["id"]), app=str(d["app"]), effect=str(d["effect"]),
                   stage=str(d["stage"]), entry_id=str(d["entry_id"]),
                   text=" ".join(str(d["text"]).split()),
                   bug=str(d["bug"]) if d.get("bug") else None,
                   cases=tuple(str(c) for c in d.get("cases") or []),
                   markers=tuple(str(m) for m in d.get("markers") or []),
                   drift=str(d["drift"]) if d.get("drift") else None,
                   lint_allow=tuple(str(r) for r in d.get("lint_allow") or []),
                   note=" ".join(str(d.get("note") or "").split()))


def load_trap_defs(*paths: Path | str) -> dict[str, TrapDef]:
    """Trap definitions by id from files or directories of `*.yaml` (default: the
    packaged `data/memory/traps/`). Duplicate ids refuse."""
    out: dict[str, TrapDef] = {}
    for p in paths or (TRAPS_DIR,):
        p = Path(p)
        if not p.exists():
            continue
        for f in sorted(p.glob("*.yaml")) if p.is_dir() else [p]:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
            items = doc.get("traps") if isinstance(doc, dict) and "traps" in doc else doc
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                raise TrackError(f"{f}: expected a trap mapping or `traps:` list")
            for d in items:
                if not isinstance(d, dict):
                    raise TrackError(f"{f}: a trap must be a mapping")
                t = TrapDef.from_dict(d, str(f))
                if t.id in out:
                    raise TrackError(f"{f}: duplicate trap id {t.id!r}")
                out[t.id] = t
    return out


# ── tracks ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Pair:
    id: str
    source: str
    target: str
    bug: str
    route: tuple[str, ...]


@dataclass(frozen=True)
class Stage:
    id: str
    drift: tuple[str, ...] = ()

    @property
    def index(self) -> int:
        return stage_index(self.id)


@dataclass(frozen=True)
class Slot:
    """One case in one stage of one ordering. Both versions run; `version` is the
    planned one, the only one whose proposals reach the curator."""
    case: str
    stage: str
    version: str
    drift: tuple[str, ...]
    bugs: tuple[str, ...]          # the case's own defects (on in its seeded version)
    pair: str | None = None
    role: str | None = None        # "source" | "target" | None
    traps: tuple[str, ...] = ()

    def flags(self, version: str | None = None) -> list[str]:
        """The flag ids one episode of this slot writes: the case's defects when the
        episode is the seeded version, then the stage's drift."""
        v = version or self.version
        return [*(self.bugs if v == SEEDED else ()), *self.drift]


@dataclass(frozen=True)
class StageFlags:
    stage: str
    bugs: tuple[str, ...]
    drift: tuple[str, ...]

    @property
    def ids(self) -> list[str]:
        return [*self.bugs, *self.drift]

    def text(self) -> str:
        """Plain ids, one per line (what the flag shim reads, without padding)."""
        return "".join(f"{i}\n" for i in self.ids)

    def file_text(self, nonce: str) -> str:
        """`files/qgb_flags.txt` exactly as an episode writes it
        (`episode_runner.flag_file_lines`): for Qualock's `app_files`."""
        from ..episode_runner import flag_file_lines

        return "".join(f"{ln}\n" for ln in flag_file_lines(nonce, self.ids))


@dataclass(frozen=True)
class QualockBuild:
    """One Qualock exploration's flag set (QUA-3495): `v1` is run-1's (clean, no drift);
    `v2-run-k` is the first stage k with a new flag set. `stages` lists every stage that
    runs this exact build (a later stage with no seeded case and no new drift reuses an
    earlier build, so it needs no exploration of its own)."""
    app: str
    label: str
    stage: str
    bugs: tuple[str, ...]
    drift: tuple[str, ...]
    stages: tuple[str, ...] = ()

    @property
    def ids(self) -> list[str]:
        return [*self.bugs, *self.drift]

    def app_files(self, nonce: str) -> dict[str, str]:
        """`ExploreRequest.app_files` for this build (QUA-3485)."""
        flags = StageFlags(self.stage, self.bugs, self.drift)
        return {"files/qgb_flags.txt": flags.file_text(nonce), "files/.qgb/nonce": nonce}


@dataclass
class Track:
    app: str
    stages: list[Stage]
    fixed: dict[str, list[str]]
    orderings: dict[str, dict[str, list[str]]]
    seeded: list[str]
    pairs: list[Pair]
    traps: list[str]
    path: Path | None = None
    _trap_defs: dict[str, TrapDef] = field(default_factory=dict, repr=False)

    # ── reading ──
    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> Track:
        where = str(path or "<track>")
        if not isinstance(d, dict):
            raise TrackError(f"{where}: a track is a mapping")
        if d.get("schema") != SCHEMA:
            raise TrackError(f"{where}: schema must be {SCHEMA}")
        for k in ("app", "stages", "fixed", "orderings"):
            if not d.get(k):
                raise TrackError(f"{where}: missing `{k}`")
        try:
            stages = [Stage(str(s["id"]), tuple(str(x) for x in s.get("drift") or []))
                      for s in d["stages"]]
            pairs = [Pair(str(p["id"]), str(p["source"]), str(p["target"]), str(p["bug"]),
                          tuple(str(r) for r in p.get("route") or []))
                     for p in d.get("pairs") or []]
        except (KeyError, TypeError) as e:
            raise TrackError(f"{where}: malformed stage or pair: {e}") from None

        def cases_by_stage(m: object, what: str) -> dict[str, list[str]]:
            if not isinstance(m, dict):
                raise TrackError(f"{where}: `{what}` maps stage -> case ids")
            return {str(k): [str(c) for c in (v or [])] for k, v in m.items()}

        return cls(app=str(d["app"]), stages=stages,
                   fixed=cases_by_stage(d["fixed"], "fixed"),
                   orderings={str(k): cases_by_stage(v, f"orderings.{k}")
                              for k, v in d["orderings"].items()},
                   seeded=[str(c) for c in d.get("seeded") or []], pairs=pairs,
                   traps=[str(t) for t in d.get("traps") or []], path=path)

    @property
    def stage_ids(self) -> list[str]:
        return [s.id for s in self.stages]

    def stage(self, stage: str) -> Stage:
        for s in self.stages:
            if s.id == stage:
                return s
        raise TrackError(f"{self.app}: no stage {stage!r}")

    def trap_defs(self) -> list[TrapDef]:
        defs = self._trap_defs or load_trap_defs()
        missing = [t for t in self.traps if t not in defs]
        if missing:
            raise TrackError(f"{self.app}: unknown trap ids {missing}")
        return [defs[t] for t in self.traps]

    def assignment(self, ordering: str) -> dict[str, list[str]]:
        """stage -> cases (fixed first, then the ordering's free cases)."""
        if ordering not in self.orderings:
            raise TrackError(f"{self.app}: no ordering {ordering!r} "
                             f"(have {sorted(self.orderings)})")
        free = self.orderings[ordering]
        return {s: [*self.fixed.get(s, []), *free.get(s, [])] for s in self.stage_ids}

    def stage_of(self, ordering: str, case: str) -> str | None:
        for s, cases in self.assignment(ordering).items():
            if case in cases:
                return s
        return None

    def plan(self, ordering: str) -> list[tuple[Stage, list[Slot]]]:
        """The ordered stage plan: [(stage, slots)]."""
        from .. import journey

        doc = journey.load_cases(self.app) or {}
        bugs = {str(c["id"]): tuple(str(b if isinstance(b, str) else b.get("id"))
                                    for b in c.get("bugs") or [])
                for c in doc.get("test_cases") or []}
        role: dict[str, tuple[str, str]] = {}
        for p in self.pairs:
            role.setdefault(p.source, (p.id, "source"))
            role[p.target] = (p.id, "target")
        traps_of: dict[str, list[str]] = {}
        for t in (self.trap_defs() if self.traps else []):
            for c in t.cases:
                traps_of.setdefault(c, []).append(t.id)
        seeded = set(self.seeded)
        out = []
        for st in self.stages:
            slots = [Slot(case=c, stage=st.id, version=SEEDED if c in seeded else CLEAN,
                          drift=st.drift, bugs=bugs.get(c, ()),
                          pair=role.get(c, (None, None))[0],
                          role=role.get(c, (None, None))[1],
                          traps=tuple(traps_of.get(c, ())))
                     for c in self.assignment(ordering)[st.id]]
            out.append((st, slots))
        return out

    def slots(self, ordering: str, stage: str) -> list[Slot]:
        for st, slots in self.plan(ordering):
            if st.id == stage:
                return slots
        raise TrackError(f"{self.app}: no stage {stage!r}")

    # ── flags ──
    def stage_flags(self, stage: str) -> StageFlags:
        """The whole flag set of stage k's build: the defects of every case planned
        seeded in that stage, then the stage's drift. Ordering-independent (seeded
        cases are fixed), which `validate` checks."""
        st = self.stage(stage)
        slots = self.slots(next(iter(self.orderings)), stage)
        bugs = tuple(dict.fromkeys(b for s in slots if s.version == SEEDED
                                   for b in s.bugs))
        return StageFlags(stage=stage, bugs=bugs, drift=st.drift)

    def qualock_builds(self) -> list[QualockBuild]:
        """The distinct Qualock builds of this track, in stage order (v1 first)."""
        by_ids: dict[tuple[str, ...], list[str]] = {}
        for st in self.stages:
            by_ids.setdefault(tuple(self.stage_flags(st.id).ids), []).append(st.id)
        out = []
        for stages in by_ids.values():
            f = self.stage_flags(stages[0])
            label = "v1" if stage_index(stages[0]) == 1 else f"v2-{stages[0]}"
            out.append(QualockBuild(self.app, label, stages[0], f.bugs, f.drift,
                                    tuple(stages)))
        return out

    def qualock_build_for(self, stage: str) -> QualockBuild:
        """The build whose diff (against v1) a stage's diff arm reads."""
        for b in self.qualock_builds():
            if stage in b.stages:
                return b
        raise TrackError(f"{self.app}: no stage {stage!r}")

    # ── leakage ──
    def curator_inputs(self, ordering: str, stage: str) -> list[tuple[str, str]]:
        """(case, version) whose proposals the curator for `curate-k` may read: the
        planned version of each stage-k case. The other version's proposals are a
        separate stream and never enter memory."""
        return [(s.case, s.version) for s in self.slots(ordering, stage)]

    def later_cases(self, ordering: str, stage: str) -> list[str]:
        """Cases that run after `stage`: safe to pass as the curator's
        `exclude_cases` (belt and braces: they have no episodes yet)."""
        k = stage_index(stage)
        return [c for s, cases in self.assignment(ordering).items()
                if stage_index(s) > k for c in cases]


def leakage_problems(track: Track) -> list[str]:
    """Static proof of the leakage rules (spec §5.1), per ordering:

    1. M_k is curated only from `curator_inputs` of stages < k (plus the static seed
       and traps, which carry no proposals); no stage-k case is among them.
    2. Each case contributes exactly one version, once.
    Returns problems (empty = proven)."""
    out = []
    for o in track.orderings:
        sources: dict[str, str] = {}             # case -> stage that contributed it
        seen: set[str] = set()
        for st, slots in track.plan(o):
            for s in slots:
                if s.case in sources:
                    out.append(f"{track.app}/{o}: {s.case} at {st.id} would read M_"
                               f"{st.index}, which holds its own proposals from "
                               f"{sources[s.case]}")
                if s.case in seen:
                    out.append(f"{track.app}/{o}: {s.case} runs twice in {st.id}")
                seen.add(s.case)
            for case, version in track.curator_inputs(o, st.id):
                if version not in (CLEAN, SEEDED):
                    out.append(f"{track.app}/{o}: {case} has no single planned version")
                sources.setdefault(case, st.id)
    return out


def load_track(app_or_path: str | Path, *, trap_defs: dict[str, TrapDef] | None = None
               ) -> Track:
    p = Path(app_or_path)
    if not p.suffix:
        p = TRACKS_DIR / f"{app_or_path}.yaml"
    t = Track.from_dict(yaml.safe_load(p.read_text(encoding="utf-8")), p)
    if t.path and t.path.stem != t.app:
        raise TrackError(f"{p}: file name must be <app>.yaml ({t.app})")
    if trap_defs is not None:
        t._trap_defs = trap_defs
    return t


def load_tracks(root: Path | str = TRACKS_DIR) -> dict[str, Track]:
    return {p.stem: load_track(p) for p in sorted(Path(root).glob("*.yaml"))}


# ── traps into memory ─────────────────────────────────────────────────────────

def traps_at(track: Track, stage: str, *, through: bool = False) -> list[TrapDef]:
    """The traps placed at `stage` (or at any stage up to it with `through`)."""
    k = stage_index(stage)
    return sorted((t for t in track.trap_defs()
                   if (stage_index(t.stage) <= k if through else stage_index(t.stage) == k)),
                  key=lambda t: (stage_index(t.stage), t.id))


def add_trap(doc: fmt.MemoryFile, trap: TrapDef) -> bool:
    """Add one trap entry as a NEW entry. False when it is already there (carried
    forward) or was invalidated by the curator (memory corrected itself). Raises when
    the id holds a different entry."""
    hit = doc.find(trap.entry_id)
    if hit is not None:
        section, entry = hit
        if section.invalidated or entry.text == trap.text:
            return False
        raise TrackError(f"{trap.id}: {trap.entry_id} already holds another entry")
    doc.add_entry(trap.entry_type, trap.text, dict(TRAP_META), entry_id=trap.entry_id)
    return True


def apply_traps(memory_root: Path | str, track: Track, stage: str, *,
                through: bool = False) -> list[str]:
    """Write the stage's traps into the app file under `memory_root` (a COPY: the
    stage's memory root, never the packaged base). Static arms pass `through=True`
    (every trap up to the stage); dynamic arms pass the curated M_k and the stage's own
    traps (earlier ones were carried forward by the curator). Returns entry ids
    added."""
    traps = traps_at(track, stage, through=through)
    if not traps:
        return []
    root = Path(memory_root).resolve()
    if root == DATA_ROOT.resolve():
        raise TrackError("refusing to write traps into the packaged base memory")
    path = fmt.app_path(root, fmt.find_org(root, track.app), track.app)
    doc = fmt.load(path)
    added = [t.entry_id for t in traps if add_trap(doc, t)]
    if added:
        doc.save(path)
    return added


# ── validation ────────────────────────────────────────────────────────────────

def _route_anchors(case: dict) -> list[str]:
    from .. import submission

    out = []
    for s in (case.get("check") or {}).get("steps") or []:
        sh = submission.route_item(s) if isinstance(s, dict) else None
        if sh and sh[0] in ("tap", "long_press"):
            out.append(sh[1])
    return out


def trap_lint_problems(trap: TrapDef, *, memory_root: Path | None = None,
                       terms=None) -> list[str]:
    """Lint the app file with the trap added: every issue must sit on the trap's own
    entry and be a rule the trap declares in `lint_allow` (the deliberate wrong
    fact); `lint_allow` itself may only name `LINT_ALLOWABLE` rules."""
    from . import lint

    out = [f"{trap.id}: lint_allow may not name {r!r}" for r in trap.lint_allow
           if r not in LINT_ALLOWABLE]
    root = Path(memory_root or DATA_ROOT)
    path = fmt.app_path(root, fmt.find_org(root, trap.app), trap.app)
    doc = fmt.load(path)
    try:
        add_trap(doc, trap)
    except (TrackError, fmt.FormatError) as e:
        return [*out, f"{trap.id}: {e}"]
    allowed = set(trap.lint_allow) & set(LINT_ALLOWABLE)
    for i in lint.lint_text(doc.dumps(), layer=fmt.APP, path=str(path), apps=[trap.app],
                            terms=terms):
        if i.entry_id != trap.entry_id or i.rule not in allowed:
            out.append(f"{trap.id}: {i}")
    return out


def validate(track: Track, *, trap_defs: dict[str, TrapDef] | None = None,
             lint_traps: bool = True) -> list[str]:
    """Every problem with one track (empty = valid): schema, existing cases and
    flags, stage sizes, orderings, seeded set, pairs, traps, trap lint, leakage."""
    from .. import journey

    app = track.app
    P: list[str] = []
    doc = journey.load_cases(app)
    if not doc:
        return [f"{app}: no journey test cases"]
    cases = {str(c["id"]): c for c in doc.get("test_cases") or []}
    case_bugs = {cid: [str(b if isinstance(b, str) else b.get("id"))
                       for b in c.get("bugs") or []] for cid, c in cases.items()}
    drift_defs = journey.load_drift(doc)
    try:
        fmt.find_org(DATA_ROOT, app)
    except FileNotFoundError as e:
        P.append(f"{app}: {e}")

    # stages + drift
    ids = track.stage_ids
    n = len(ids)
    if not MIN_STAGES <= n <= MAX_STAGES:
        P.append(f"{app}: {n} stages (want {MIN_STAGES}-{MAX_STAGES})")
    if ids != [f"run-{k}" for k in range(1, n + 1)]:
        P.append(f"{app}: stages must be run-1..run-{n} in order, got {ids}")
        return P
    prev: set[str] = set()
    for st in track.stages:
        unknown = [d for d in st.drift if d not in drift_defs]
        if unknown:
            P.append(f"{app}/{st.id}: unknown drift flags {unknown}")
        if st.index == 1 and st.drift:
            P.append(f"{app}/run-1: drift must be off in the v1 stage")
        if not prev <= set(st.drift):
            P.append(f"{app}/{st.id}: drift {sorted(prev - set(st.drift))} switched off "
                     f"again (a UI change stays)")
        prev = set(st.drift)

    # fixed + orderings cover every case once
    for k in [*track.fixed, *(s for o in track.orderings.values() for s in o)]:
        if k not in ids:
            P.append(f"{app}: unknown stage {k!r} in fixed/orderings")
    fixed = [c for cs in track.fixed.values() for c in cs]
    if len(set(fixed)) != len(fixed):
        P.append(f"{app}: a case is fixed twice")
    if len(track.orderings) < MIN_ORDERINGS:
        P.append(f"{app}: {len(track.orderings)} orderings (want >= {MIN_ORDERINGS})")
    assignments = {}
    for o in track.orderings:
        a = track.assignment(o)
        flat = [c for cs in a.values() for c in cs]
        unknown = sorted(set(flat) - set(cases))
        if unknown:
            P.append(f"{app}/{o}: unknown cases {unknown}")
        dup = sorted({c for c in flat if flat.count(c) > 1})
        if dup:
            P.append(f"{app}/{o}: cases run more than once {dup}")
        missing = sorted(set(cases) - set(flat))
        if missing:
            P.append(f"{app}/{o}: cases never run {missing}")
        for s, cs in a.items():
            if not MIN_STAGE_CASES <= len(cs) <= MAX_STAGE_CASES:
                P.append(f"{app}/{o}/{s}: {len(cs)} cases (want "
                         f"{MIN_STAGE_CASES}-{MAX_STAGE_CASES})")
        assignments[o] = {c: s for s, cs in a.items() for c in cs}
    names = sorted(assignments)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            if assignments[x] == assignments[y]:
                P.append(f"{app}: orderings {x} and {y} are the same")
    if P:
        return P

    def at(case: str) -> int:
        return stage_index(next(s for s, cs in track.fixed.items() if case in cs))

    # pairs
    defs = trap_defs if trap_defs is not None else load_trap_defs()
    traps = [defs[t] for t in track.traps if t in defs]
    P += [f"{app}: unknown trap id {t}" for t in track.traps if t not in defs]
    P += [f"{app}: trap {t.id} is not listed in the track" for t in defs.values()
          if t.app == app and t.id not in track.traps]
    trap_cases = {c for t in traps for c in t.cases}
    pair_ids, targets = set(), set()
    for p in track.pairs:
        w = f"{app}/{p.id}"
        if p.id in pair_ids:
            P.append(f"{w}: duplicate pair id")
        pair_ids.add(p.id)
        bad = [f"{w}: {c} must be a fixed case" for c in (p.source, p.target)
               if c not in fixed]
        if p.source == p.target:
            bad.append(f"{w}: source and target are the same case")
        if bad:
            P += bad
            continue
        if at(p.source) >= at(p.target):
            P.append(f"{w}: source must run in an earlier stage than the target")
        if p.target in targets:
            P.append(f"{w}: {p.target} is already a pair target")
        targets.add(p.target)
        if p.bug not in case_bugs[p.target]:
            P.append(f"{w}: {p.bug} is not a defect of {p.target}")
        if not p.route:
            P.append(f"{w}: route is empty")
        for anchor in p.route:
            for c in (p.source, p.target):
                if anchor not in _route_anchors(cases[c]):
                    P.append(f"{w}: route anchor {anchor!r} is not on {c}'s route")
        drift_on = track.stage(f"run-{at(p.target)}").drift
        hit = [d for d in drift_on if journey.drift_affects(cases[p.target], d, drift_defs)]
        if hit:
            P.append(f"{w}: target route is drifted in its stage by {hit}")
        if {p.source, p.target} & trap_cases:
            P.append(f"{w}: a pair case is also a trap target")
    sources = {p.source for p in track.pairs}
    if sources & targets:
        P.append(f"{app}: cases are both pair source and target {sorted(sources & targets)}")

    # traps
    entry_order: dict[str, list[tuple[int, int, str]]] = {}
    for t in traps:
        w = f"{app}/{t.id}"
        if t.app != app:
            P.append(f"{w}: trap app is {t.app}")
            continue
        if t.stage not in ids or stage_index(t.stage) < 2:
            P.append(f"{w}: stage {t.stage} must be a track stage from run-2 on")
            continue
        if t.entry_type not in fmt.APP_TYPES:
            P.append(f"{w}: {t.entry_id} is not an app entry type")
        if t.entry_number < TRAP_ID_FLOOR:
            P.append(f"{w}: {t.entry_id} must be numbered >= {TRAP_ID_FLOOR}")
        entry_order.setdefault(t.entry_type, []).append(
            (stage_index(t.stage), t.entry_number, t.id))
        if not t.cases:
            P.append(f"{w}: name its target cases")
        for c in t.cases:
            if c not in fixed:
                P.append(f"{w}: target {c} must be a fixed case")
                continue
            if at(c) != stage_index(t.stage):
                P.append(f"{w}: target {c} runs at run-{at(c)}, not at the trap stage")
            if t.effect == "missed_bug":
                if t.bug not in case_bugs[c]:
                    P.append(f"{w}: {t.bug} is not a defect of {c}")
                if c not in track.seeded:
                    P.append(f"{w}: missed_bug target {c} must run seeded")
            elif c in track.seeded:
                P.append(f"{w}: {t.effect} target {c} must run clean")
            if t.effect == "not_completed":
                on = track.stage(t.stage).drift
                if not t.drift or t.drift not in on:
                    P.append(f"{w}: not_completed needs `drift:` on at {t.stage}")
                elif not journey.drift_affects(cases[c], t.drift, drift_defs):
                    P.append(f"{w}: {t.drift} does not touch {c}'s route")
        if lint_traps:
            P += [f"{app}: {x}" for x in trap_lint_problems(t)]
    for typ, rows in entry_order.items():
        rows.sort()
        for (s1, n1, a), (s2, n2, b) in pairwise(rows):
            if not (s2 > s1 and n2 < n1):
                P.append(f"{app}: {typ} trap ids must fall as stages rise "
                         f"({a} {typ}-{n1} at run-{s1}, {b} {typ}-{n2} at run-{s2})")
    try:
        base = fmt.load(fmt.app_path(DATA_ROOT, fmt.find_org(DATA_ROOT, app), app))
    except FileNotFoundError:
        base = None
    if base is not None:
        for t in traps:
            if base.find(t.entry_id):
                P.append(f"{app}/{t.id}: {t.entry_id} is already in the base memory")

    # seeded == pair targets + missed_bug trap targets
    want = targets | {c for t in traps if t.effect == "missed_bug" for c in t.cases}
    if set(track.seeded) != want:
        P.append(f"{app}: seeded must be the pair targets and missed_bug trap targets; "
                 f"extra {sorted(set(track.seeded) - want)}, "
                 f"missing {sorted(want - set(track.seeded))}")
    for c in track.seeded:
        if c in fixed and at(c) == 1:
            P.append(f"{app}: {c} runs seeded in run-1 (v1 is clean)")
    free = {c for o in track.orderings.values() for cs in o.values() for c in cs}
    if free & set(track.seeded):
        P.append(f"{app}: free (shuffled) cases must run clean {sorted(free & set(track.seeded))}")

    if not P:
        flags = {o: [(s.id, sorted(b for sl in sl_ if sl.version == SEEDED for b in sl.bugs))
                     for s, sl_ in track.plan(o)] for o in track.orderings}
        if len({repr(v) for v in flags.values()}) != 1:
            P.append(f"{app}: stage flag sets differ between orderings")
        P += leakage_problems(track)
    return P


def validate_all(root: Path | str = TRACKS_DIR, *, trap_defs: dict[str, TrapDef] | None = None,
                 lint_traps: bool = True) -> dict[str, list[str]]:
    """{app: problems} for every track; also traps whose app has no track."""
    defs = trap_defs if trap_defs is not None else load_trap_defs()
    tracks = load_tracks(root)
    out = {app: validate(t, trap_defs=defs, lint_traps=lint_traps)
           for app, t in tracks.items()}
    for t in defs.values():
        if t.app not in tracks:
            out.setdefault(t.app, []).append(f"{t.app}/{t.id}: no track for this app")
    return out


def summary(track: Track) -> dict[str, Any]:
    """A JSON-able view of a track: plan per ordering, stage flags, Qualock builds."""
    return {
        "app": track.app,
        "stages": [{"id": s.id, "drift": list(s.drift),
                    "flags": track.stage_flags(s.id).ids} for s in track.stages],
        "orderings": {o: {st.id: [{"case": s.case, "version": s.version,
                                   **({"pair": s.pair, "role": s.role} if s.pair else {}),
                                   **({"traps": list(s.traps)} if s.traps else {})}
                                  for s in slots]
                          for st, slots in track.plan(o)} for o in track.orderings},
        "pairs": [{"id": p.id, "source": p.source, "target": p.target, "bug": p.bug,
                   "route": list(p.route)} for p in track.pairs],
        "traps": list(track.traps),
        "qualock_builds": [{"label": b.label, "stages": list(b.stages), "ids": b.ids}
                           for b in track.qualock_builds()],
    }


def all_pairs(tracks: Iterable[Track]) -> list[Pair]:
    return [p for t in tracks for p in t.pairs]
