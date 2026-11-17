"""Memory experiment metrics (QUA-3489): per-episode fields, memory health, aggregation.

Four families, all computed from what an episode or a snapshot already holds, so a
rescore of a saved run directory fills them without a device or a model:

- **Wasted steps.** `steps_over_ref` = agent steps / `len(check.steps)` on completed
  episodes, beside `ref_steps` (the case's reference route length, which `journey_tasks`
  puts in the spec). Computed here, after the verdict, and not inside
  `journey.journey_verdict`: it is a reading of the verdict, not a scoring rule, so the
  pinned scorer closure (`SCORER_VERSION`) does not move.
- **Proposals and stage.** `memory_stage` (the arm's stage label, every arm),
  `memory_proposals_count` and the entry ids the proposals point at
  (`memory_proposals_updates`, `memory_proposals_contradicts`). `memory_proposals_accepted`
  needs the curator's operation log (`memory.curator`) and is joined at aggregation
  time (`aggregate(..., curation=load_curation_log(...))`).
- **Trap harm.** For each trap that applies to the episode (`load_traps`): was the
  wrong entry in the agent's memory (`exposed`), and did its predicted harm happen
  (`harmed`)? Computed for every arm, the no-memory arm included, so the base rate of
  the same outcome without the trap is on the board beside the harm rate.
- **Attribution.** For regression-pair targets (`load_attribution`): whether Qualock's
  diff exposed the defect and whether Qualock's hunt found it, so "found with memory
  help" separates from "Qualock caught it upstream".

Memory health (`unit_health`, `store_health`) reads a snapshot store
(`memory.snapshot`): entry counts, size, invalidations, contradictions, lint failures
and entries each budget would drop, per (app, arm, rep, stage), with growth against
the parent snapshot.

Inputs owned by later tickets have fixed shapes here (`load_traps`, `load_attribution`);
until those tickets ship, the packaged directories are absent and every such field is
simply left out of result.json.
"""

from __future__ import annotations

import functools
import json
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import format as fmt
from . import render as _render
from . import snapshot as _snapshot
from .arm import PROPOSALS_FILENAME, is_memory_condition

METRICS_VERSION = 1

#: Packaged inputs. Trap definitions are QUA-3491's (`data/memory/traps/*.yaml`); the
#: Qualock attribution tables come with the frozen Qualock assets
#: (`data/memory/qualock/<app>/attribution.yaml`). Held-out apps pass their own paths.
DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "memory"
TRAPS_DIR = DATA_ROOT / "traps"
QUALOCK_DIR = DATA_ROOT / "qualock"
ATTRIBUTION_FILENAME = "attribution.yaml"

#: Trap effects: what following the wrong entry looks like in a scored episode.
EFFECT_MISSED_BUG = "missed_bug"          # the entry hides a seeded defect; it was missed
EFFECT_FALSE_REPORT = "false_report"      # the entry misleads; a false report names its markers
EFFECT_NOT_COMPLETED = "not_completed"    # stale navigation; the journey was not completed
TRAP_EFFECTS = (EFFECT_MISSED_BUG, EFFECT_FALSE_REPORT, EFFECT_NOT_COMPLETED)

_STAGE_NUM_RE = re.compile(r"(\d+)$")


class MetricsInputError(ValueError):
    pass


def stage_number(stage: str | None) -> int | None:
    """`run-2` → 2; None when the label has no trailing number."""
    m = _STAGE_NUM_RE.search(str(stage or ""))
    return int(m.group(1)) if m else None


# ── wasted steps ──────────────────────────────────────────────────────────────

def steps_over_ref(completed: object, steps: object, ref_steps: object) -> float | None:
    """Agent steps per reference step, for a COMPLETED episode only (the reference
    route reached the outcome; a failed episode's step count measures something else).
    None when not completed, or when either count is missing or zero."""
    if completed is not True:
        return None
    if not isinstance(steps, int) or not isinstance(ref_steps, int):
        return None
    if steps <= 0 or ref_steps <= 0:
        return None
    return round(steps / ref_steps, 4)


# ── proposals ─────────────────────────────────────────────────────────────────

def proposal_fields(workspace_dir: Path | str) -> dict[str, Any]:
    """`memory_proposals_count` (valid proposals, as the curator will read them) and the
    entry ids they point at, from `<workspace>/memory_proposals.yaml` through
    `memory.proposals.load` — the curator's own parser, so this count and the curator's
    input never disagree. No file = 0. Validation errors are named in
    `memory_proposals_error` (the first one) and `memory_proposals_invalid` (how many);
    a file that yields nothing because it is unreadable as a whole counts as None."""
    from . import proposals as props

    pf = props.load(Path(workspace_dir) / PROPOSALS_FILENAME)
    ps = pf.proposals
    out: dict[str, Any] = {
        "memory_proposals_count": len(ps),
        "memory_proposals_updates": sorted({p.updates for p in ps if p.updates}),
        "memory_proposals_contradicts": sorted({p.contradicts for p in ps if p.contradicts}),
    }
    if pf.errors:
        out["memory_proposals_error"] = str(pf.errors[0])[:200]
        out["memory_proposals_invalid"] = len(pf.errors)
        if not ps and not any(e.startswith("proposal ") for e in pf.errors):
            out["memory_proposals_count"] = None
    return out


# ── traps ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Trap:
    """One hand-written wrong entry placed in M_k (spec §5, QUA-3491).

    File shape (`data/memory/traps/<id>.yaml`, one mapping, or `traps:` with a list):

        id: markor-trap-priority-wontfix
        app: markor
        stage: run-2                  # first stage whose memory carries the entry
        entry_id: accepted-901        # its id in that memory (how exposure is read)
        effect: missed_bug            # missed_bug | false_report | not_completed
        bug: todo-priority-crash      # missed_bug: the seeded defect the entry hides
        cases: [markor-todo-set-priority]   # the cases it targets; empty = every case
        markers: ["Priority"]         # false_report: words that tie a report to it
        note: free text

    Only `id`, `app`, `effect` are required; `bug` is required for `missed_bug` and
    `markers` for `false_report`. Unknown keys are kept out of the metric and ignored,
    so the trap author can carry the entry text itself beside these."""
    id: str
    app: str
    effect: str
    stage: str | None = None
    entry_id: str | None = None
    bug: str | None = None
    cases: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], where: str = "") -> Trap:
        def need(k: str) -> str:
            v = d.get(k)
            if not v:
                raise MetricsInputError(f"{where}: trap is missing `{k}`")
            return str(v)
        tid, app, effect = need("id"), need("app"), need("effect")
        if effect not in TRAP_EFFECTS:
            raise MetricsInputError(f"{where}: trap {tid}: effect must be one of "
                                    f"{TRAP_EFFECTS}, not {effect!r}")
        bug = str(d["bug"]) if d.get("bug") else None
        markers = tuple(str(m) for m in d.get("markers") or [] if str(m).strip())
        if effect == EFFECT_MISSED_BUG and not bug:
            raise MetricsInputError(f"{where}: trap {tid}: missed_bug needs `bug`")
        if effect == EFFECT_FALSE_REPORT and not markers:
            raise MetricsInputError(f"{where}: trap {tid}: false_report needs `markers`")
        return cls(id=tid, app=app, effect=effect,
                   stage=str(d["stage"]) if d.get("stage") else None,
                   entry_id=str(d["entry_id"]) if d.get("entry_id") else None,
                   bug=bug, cases=tuple(str(c) for c in d.get("cases") or []),
                   markers=markers)

    def applies(self, metrics: Mapping[str, Any]) -> bool:
        """Is the episode one this trap is measured on? Same app, a targeted case, and
        for `missed_bug` the hidden defect active on this build. Exposure is separate:
        the no-memory arm is measured too (its base rate)."""
        if metrics.get("app_id") != self.app:
            return False
        if self.cases and metrics.get("case_id") not in self.cases:
            return False
        if self.effect == EFFECT_MISSED_BUG:
            return self.bug in (metrics.get("bugs_present") or [])
        return True

    def exposed(self, metrics: Mapping[str, Any], memory_prov: Mapping[str, Any] | None
                ) -> bool:
        """Was the wrong entry in what this episode's agent got? With `entry_id`: it
        is among the rendered entries (`provenance.memory.included`). Without: any
        memory episode on the app at or after the trap's stage."""
        if memory_prov is None or not is_memory_condition(metrics.get("memory_condition")):
            return False
        if self.stage is not None:
            here, at = stage_number(metrics.get("memory_stage")), stage_number(self.stage)
            if here is None or at is None or here < at:
                return False
        if self.entry_id is not None:
            return self.entry_id in (memory_prov.get("included") or [])
        return True

    def harmed(self, metrics: Mapping[str, Any]) -> bool | None:
        """Did the outcome the trap predicts happen? None when it cannot be judged
        (an unscored completion)."""
        if self.effect == EFFECT_MISSED_BUG:
            return self.bug in (metrics.get("bugs_missed") or [])
        if self.effect == EFFECT_NOT_COMPLETED:
            c = metrics.get("completed")
            return None if c is None else c is False
        for r in metrics.get("reports") or []:
            if not isinstance(r, Mapping) or r.get("matched"):
                continue
            text = " ".join(str(r.get(k) or "") for k in
                            ("screen", "observed", "expected", "description"))
            if any(_has_word(m, text) for m in self.markers):
                return True
        return False


def _has_word(needle: str, text: str) -> bool:
    n = re.escape(needle.strip().casefold())
    return bool(n) and re.search(rf"(?<!\w){n}(?!\w)", text.casefold()) is not None


def _yaml_docs(path: Path) -> list[tuple[Path, Any]]:
    import yaml

    files = sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")) if path.is_dir() else [path]
    return [(f, yaml.safe_load(f.read_text(encoding="utf-8"))) for f in files]


def load_traps(*paths: Path | str) -> list[Trap]:
    """Trap definitions from files or directories of `*.yaml` (`Trap` has the shape).
    Missing paths are skipped (none written yet = no traps). Duplicate ids refuse."""
    out: dict[str, Trap] = {}
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        for f, doc in _yaml_docs(p):
            items = doc.get("traps") if isinstance(doc, Mapping) and "traps" in doc else doc
            if isinstance(items, Mapping):
                items = [items]
            if not isinstance(items, list):
                raise MetricsInputError(f"{f}: expected a trap mapping or `traps:` list")
            for d in items:
                if not isinstance(d, Mapping):
                    raise MetricsInputError(f"{f}: a trap must be a mapping")
                t = Trap.from_dict(d, str(f))
                if t.id in out:
                    raise MetricsInputError(f"{f}: duplicate trap id {t.id!r}")
                out[t.id] = t
    return list(out.values())


@functools.lru_cache(maxsize=1)
def default_traps() -> tuple[Trap, ...]:
    return tuple(load_traps(TRAPS_DIR))


def trap_fields(metrics: Mapping[str, Any], memory_prov: Mapping[str, Any] | None,
                traps: Iterable[Trap]) -> dict[str, Any]:
    """`memory_traps` {trap id: {effect, exposed, harmed}} for every trap that applies,
    and `memory_trap_harm`: True when an EXPOSED trap's harm happened, False when every
    exposed trap's did not, None when no trap was exposed. Empty dict when no trap
    applies, so an unrelated result.json keeps its shape."""
    detail = {}
    for t in traps:
        if not t.applies(metrics):
            continue
        detail[t.id] = {"effect": t.effect, "exposed": t.exposed(metrics, memory_prov),
                        "harmed": t.harmed(metrics)}
    if not detail:
        return {}
    exposed = [d["harmed"] for d in detail.values() if d["exposed"]]
    harm = (True if any(h is True for h in exposed) else
            (False if exposed and all(h is False for h in exposed) else None))
    return {"memory_traps": detail, "memory_trap_harm": harm}


# ── attribution ───────────────────────────────────────────────────────────────

def load_attribution(*paths: Path | str) -> dict[tuple[str, str], dict[str, Any]]:
    """Qualock attribution per (app, defect), from attribution files or directories
    holding `<app>/attribution.yaml`. File shape (written beside the frozen Qualock
    assets, `data/memory/qualock/<app>/attribution.yaml`):

        app: markor
        targets:
          todo-priority-crash:
            qualock_diff_exposes: true    # the defect's screen/control is in the diff
            qualock_hunt_found: false     # Qualock's hunt confirmed the defect
            pair: v1-clean..v2-bugX       # optional, carried through as-is

    A value left out is None (not measured), never False."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        files = sorted(p.glob(f"*/{ATTRIBUTION_FILENAME}")) if p.is_dir() else [p]
        for f, doc in (x for path in files for x in _yaml_docs(path)):
            if not isinstance(doc, Mapping) or not doc.get("app"):
                raise MetricsInputError(f"{f}: expected a mapping with `app` and `targets`")
            targets = doc.get("targets") or {}
            if not isinstance(targets, Mapping):
                raise MetricsInputError(f"{f}: `targets` must map defect id -> fields")
            for bug, row in targets.items():
                row = dict(row or {})
                out[(str(doc["app"]), str(bug))] = {
                    "qualock_diff_exposes": _opt_bool(row.get("qualock_diff_exposes")),
                    "qualock_hunt_found": _opt_bool(row.get("qualock_hunt_found")),
                    **({"pair": str(row["pair"])} if row.get("pair") else {})}
    return out


def _opt_bool(v: object) -> bool | None:
    return None if v is None else bool(v)


@functools.lru_cache(maxsize=1)
def _default_attribution_items() -> tuple:
    return tuple(load_attribution(QUALOCK_DIR).items())


def default_attribution() -> dict[tuple[str, str], dict[str, Any]]:
    return dict(_default_attribution_items())


def _any3(values: list[bool | None]) -> bool | None:
    if any(v is True for v in values):
        return True
    if values and all(v is False for v in values):
        return False
    return None


def attribution_fields(metrics: Mapping[str, Any],
                       table: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[str, Any]:
    """For an episode whose active defects include regression-pair targets:
    `memory_attribution` {defect: {qualock_diff_exposes, qualock_hunt_found, found}} and
    the flat `qualock_diff_exposes` / `qualock_hunt_found` (True if any target's is,
    None when unmeasured). Empty dict when the episode has no target."""
    app = metrics.get("app_id")
    found = set(metrics.get("bugs_found") or [])
    detail = {}
    for bug in metrics.get("bugs_present") or []:
        row = table.get((app, bug))
        if row is None:
            continue
        detail[bug] = {"qualock_diff_exposes": row.get("qualock_diff_exposes"),
                       "qualock_hunt_found": row.get("qualock_hunt_found"),
                       "found": bug in found}
    if not detail:
        return {}
    return {"memory_attribution": detail,
            "qualock_diff_exposes": _any3([d["qualock_diff_exposes"] for d in detail.values()]),
            "qualock_hunt_found": _any3([d["qualock_hunt_found"] for d in detail.values()])}


# ── one episode ───────────────────────────────────────────────────────────────

def episode_fields(metrics: Mapping[str, Any], memory_prov: Mapping[str, Any] | None,
                   workspace_dir: Path | str | None, *, ref_steps: int | None = None,
                   stage: str | None = None,
                   traps: Iterable[Trap] | None = None,
                   attribution: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
                   ) -> dict[str, Any]:
    """The memory-experiment fields one journey episode adds to result.json's metrics.
    `metrics` is the scored verdict's metrics (memory metrics merged in). Run time and
    rescore call this the same way. Apart from the wasted-steps pair, only what applies
    is returned.

    - `ref_steps` (the case's `len(check.steps)`, None when unknown) and
      `steps_over_ref` (`steps_over_ref()`): on every journey episode.
    - `memory_stage` when `stage` (or the recorded one) is set.
    - Memory episodes: `memory_proposals_count`, `_updates`, `_contradicts`.
    - `memory_traps`, `memory_trap_harm` when a trap applies (`trap_fields`).
    - `memory_attribution`, `qualock_diff_exposes`, `qualock_hunt_found` for targets.
    """
    ref = ref_steps if isinstance(ref_steps, int) and ref_steps > 0 else None
    out: dict[str, Any] = {"ref_steps": ref, "steps_over_ref": steps_over_ref(
        metrics.get("completed"), metrics.get("steps"), ref)}
    stage = stage or metrics.get("memory_stage")
    m = dict(metrics)
    if stage:
        out["memory_stage"] = m["memory_stage"] = str(stage)
    if is_memory_condition(metrics.get("memory_condition")) and workspace_dir is not None:
        out.update(proposal_fields(workspace_dir))
    out.update(trap_fields(m, memory_prov, default_traps() if traps is None else traps))
    out.update(attribution_fields(m, default_attribution() if attribution is None
                                  else attribution))
    return out


# ── memory health ─────────────────────────────────────────────────────────────

def file_stats(root: Path | str, org_key: str, app_key: str) -> dict[str, Any]:
    """Counts for one (org, app) memory under `root`: live and invalidated entries,
    entries any run contradicted, bytes and estimated tokens of what can be injected."""
    root = Path(root)
    docs = [fmt.load(fmt.org_path(root, org_key), fmt.ORG),
            fmt.load(fmt.app_path(root, org_key, app_key), fmt.APP)]
    live = [e for d in docs for e in d.entries()]
    dead = [e for d in docs for e in d.entries(include_invalidated=True)
            if d.is_invalidated(e.id)]
    injectable = "".join(_render.injectable_copy(d) for d in docs)
    return {"entries": len(live),
            "entries_org": len(docs[0].entries()), "entries_app": len(docs[1].entries()),
            "invalidated": len(dead),
            "contradicted": sum(1 for e in live if e.contra > 0),
            "seen_total": sum(e.seen for e in live),
            "contra_total": sum(e.contra for e in live),
            "bytes": len(injectable.encode("utf-8")),
            "tokens": _render.estimate_tokens(injectable),
            "_ids": {e.id for e in live}, "_dead": {e.id for e in dead}}


def _lint_count(root: Path, org_key: str, app_key: str, terms) -> int:
    from .lint import lint_file

    files = [fmt.org_path(root, org_key), fmt.app_path(root, org_key, app_key)]
    cp = fmt.changes_path(root, org_key, app_key)
    if cp.is_file():
        files.append(cp)
    return sum(len(lint_file(f, terms=terms)) for f in files)


def health(root: Path | str, org_key: str | None, app_key: str, *,
           parent_root: Path | str | None = None, terms=None,
           budgets: Iterable[str] = (_render.SMALL, _render.LARGE, _render.INDEX),
           ) -> dict[str, Any]:
    """Memory health of one app's memory at `root`, against `parent_root` (the memory
    the stage started from) when given.

    - `entries`, `entries_org`, `entries_app`, `invalidated`, `bytes`, `tokens`
    - `contradiction_rate` = live entries with contra > 0 / live entries
    - `contra_ratio` = sum(contra) / sum(seen + contra) over live entries
    - `lint_failures` = lint issues in the files (the curator must keep this 0)
    - `dropped` {budget: entries the renderer drops at that budget}
    - with a parent: `added`, `removed` (ids gone without invalidation: a bug, the
      format never deletes), `invalidated_new`, `growth` (entries - parent entries),
      `invalidation_rate` = invalidated_new / parent live entries
    """
    root = Path(root)
    org_key = org_key or fmt.find_org(root, app_key)
    s = file_stats(root, org_key, app_key)
    ids, dead = s.pop("_ids"), s.pop("_dead")
    if terms is None:
        from .lint import default_terms
        terms = default_terms()
    out: dict[str, Any] = {"app": app_key, "org": org_key, **s}
    out["contradiction_rate"] = round(s["contradicted"] / s["entries"], 4) if s["entries"] else None
    denom = s["seen_total"] + s["contra_total"]
    out["contra_ratio"] = round(s["contra_total"] / denom, 4) if denom else None
    out["lint_failures"] = _lint_count(root, org_key, app_key, terms)
    out["dropped"] = {b: len(_render.render(root, org_key, app_key, b).dropped)
                      for b in budgets}
    if parent_root is not None:
        p = file_stats(parent_root, org_key, app_key)
        p_ids, p_dead = p.pop("_ids"), p.pop("_dead")
        new_dead = dead - p_dead
        out.update({"parent_entries": p["entries"],
                    "added": len(ids - p_ids - p_dead),
                    "removed": len((p_ids | p_dead) - ids - dead),
                    "invalidated_new": len(new_dead),
                    "growth": s["entries"] - p["entries"],
                    "invalidation_rate": (round(len(new_dead & p_ids) / p["entries"], 4)
                                          if p["entries"] else None)})
    return out


def parse_unit(unit: str) -> dict[str, Any]:
    """`<app>__<arm>__r<rep>` (the snapshot library's suggested unit key) → parts."""
    parts = unit.split("__")
    if len(parts) == 3 and re.fullmatch(r"r\d+", parts[2]):
        return {"app": parts[0], "arm": parts[1], "rep": int(parts[2][1:])}
    return {}


def unit_health(store: Path | str, stage: str, unit: str, *, app: str | None = None,
                org: str | None = None, terms=None) -> dict[str, Any]:
    """`health` of one stage pointer (`stages/<stage>/<unit>.json`), against the
    pointer's `parent` snapshot when it names one. App/arm/rep come from the pointer's
    extra keys (`app`, `arm`, `rep`), else from the unit key (`parse_unit`).

    The curator's own counts are passed through as `curator` when the pointer
    carries them: a `curator` mapping, or a `counts` mapping (`memory.curator`'s log
    `counts`: ADD/UPDATE/INVALIDATE/NOOP, proposals, episodes ...), so whoever writes
    the stage pointer after `curate` can put `extra={"counts": result.counts}` on it."""
    ptr = _snapshot.stage_pointer(store, stage, unit)
    meta = {**parse_unit(unit), **{k: ptr[k] for k in ("app", "arm", "rep") if k in ptr}}
    app = app or meta.get("app")
    if not app:
        raise MetricsInputError(f"stage {stage} unit {unit}: no app (pointer key `app` "
                                "or unit key `<app>__<arm>__r<rep>`)")
    snap = _snapshot.open_snapshot(store, ptr["snapshot"])
    parent_root = None
    if ptr.get("parent"):
        try:
            parent_root = _snapshot.open_snapshot(store, ptr["parent"]).memory_root
        except FileNotFoundError:
            parent_root = None
    row = {"stage": stage, "unit": unit, "arm": meta.get("arm"), "rep": meta.get("rep"),
           "snapshot": ptr["snapshot"], "parent": ptr.get("parent"),
           **health(snap.memory_root, org, app, parent_root=parent_root, terms=terms)}
    for key in ("curator", "counts"):
        if isinstance(ptr.get(key), Mapping):
            row["curator"] = dict(ptr[key])
            break
    return row


def store_health(store: Path | str, *, terms=None) -> list[dict[str, Any]]:
    """`unit_health` of every stage pointer in a snapshot store whose unit names an
    app, sorted by (app, arm, rep, stage number). A unit `all` (no app) is skipped."""
    store = Path(store)
    rows = []
    for p in sorted((store / "stages").glob("*/*.json")):
        stage, unit = p.parent.name, p.stem
        ptr = json.loads(p.read_text())
        if not parse_unit(unit) and not ptr.get("app"):
            continue
        rows.append(unit_health(store, stage, unit, terms=terms))
    return sorted(rows, key=lambda r: (str(r["app"]), str(r["arm"]), r["rep"] or 0,
                                       stage_number(r["stage"]) or 0, r["stage"]))


# ── aggregation ───────────────────────────────────────────────────────────────

def curation_counts(log: Iterable[Mapping[str, Any]] | Mapping[str, Any]) -> dict[str, int]:
    """Accepted proposals per episode, from a curator operation log
    (`memory.curator.curate(...).log`, written as JSON by `memory curate`): its
    `operations` list, one record per proposal with `episode` (the episode id the
    curator was given) and `op`. ADD, UPDATE and INVALIDATE count as accepted (the
    proposal changed memory, a gate-only `contra` raise included); NOOP does not.

    Also accepts a bare list of records, where a record may name its episode by
    `episode`, `episode_id` or `proposals_sha256` and carry `accepted` (an int)
    instead of `op`. The aggregation joins on the result's `artifact_dir`,
    `provenance.episode_id` or `memory_proposals_sha256`, so the runner that calls the
    curator must use one of those as the curator's episode id."""
    if isinstance(log, Mapping):
        # The curator's log, or one bare record (a one-line JSON-lines file).
        log = (log.get("operations") or []) if "operations" in log else [log]
    out: dict[str, int] = {}
    for rec in log:
        keys = [str(rec[k]) for k in ("episode", "episode_id", "proposals_sha256") if rec.get(k)]
        if not keys:
            continue
        if "accepted" in rec:
            n = int(rec.get("accepted") or 0)
        else:
            n = 1 if str(rec.get("op") or "").upper() in ("ADD", "UPDATE", "INVALIDATE") else 0
        for k in keys:
            out[k] = out.get(k, 0) + n
    return out


def load_curation_log(*paths: Path | str) -> dict[str, int]:
    """`curation_counts` over curator logs (one per curated unit): JSON (the curator's
    log), JSON lines or YAML. Counts from several logs add up."""
    import yaml

    out: dict[str, int] = {}
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                data = [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError:
                data = yaml.safe_load(text)
        for k, n in curation_counts(data or []).items():
            out[k] = out.get(k, 0) + n
    return out


def _get(r: Any, name: str, default: Any = None) -> Any:
    return r.get(name, default) if isinstance(r, Mapping) else getattr(r, name, default)


def _accepted(r: Any, curation: Mapping[str, int] | None) -> int | None:
    if not curation:
        return None
    m = _get(r, "metrics") or {}
    for k in (_get(r, "artifact_dir"), (_get(r, "provenance") or {}).get("episode_id"),
              m.get("memory_proposals_sha256")):
        if k and str(k) in curation:
            return curation[str(k)]
    return None


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 4) if xs else None


def dedupe_staged(results: Iterable[Any]) -> list[Any]:
    """The latest result per (model, task, condition, trial, stage). The board's
    `leaderboard.dedupe_latest` has no stage in its key, so it would fold stage k+1 of
    a staged experiment onto stage k when a task repeats across stages."""
    latest: dict[tuple, Any] = {}
    for r in results:
        m = _get(r, "metrics") or {}
        key = (_get(r, "model"), _get(r, "task_id"), _get(r, "condition"), _get(r, "trial"),
               m.get("memory_stage"))
        prev = latest.get(key)
        if prev is None or str(_get(r, "started_at") or "") > str(_get(prev, "started_at") or ""):
            latest[key] = r
    return list(latest.values())


def aggregate(results: Iterable[Any], *, by_app: bool = False,
              curation: Mapping[str, int] | None = None) -> list[dict[str, Any]]:
    """One row per (condition, stage) — per app too with `by_app` — over journey
    results (RunResult or result.json dicts). Excluded episodes (`failures.is_excluded`)
    are counted, never scored. Row fields:

    `episodes`, `excluded`, `completion` (scored completions), `steps_over_ref_mean` /
    `_median` / `_n`, `clean_episodes`, `false_alarm_rate` (clean episodes with >= 1
    false report), `bugs_present`, `bugs_found`, `recall`, `memory_tokens_injected_mean`,
    `proposals_count`, `proposals_accepted` (None without a curator log),
    `trap_episodes`, `trap_exposed`, `trap_harmed`, `trap_harm_rate` (harmed / exposed
    trap applications), `trap_unexposed`, `trap_base_harmed`, `trap_base_rate` (the same
    outcome on unexposed applications: the no-memory arm's rate), `pair_targets`,
    `pair_found`, `pair_recall`, `pair_diff_exposed`, `pair_hunt_found`,
    `pair_found_unaided` (found with neither Qualock signal)."""
    from ..failures import is_excluded

    groups: dict[tuple, list] = {}
    excluded: dict[tuple, int] = {}
    for r in results:
        if _get(r, "task_type") != "journey_case":
            continue
        m = _get(r, "metrics") or {}
        key = (_get(r, "condition"), m.get("memory_stage")) + ((m.get("app_id"),) if by_app else ())
        groups.setdefault(key, [])
        if is_excluded(m):
            excluded[key] = excluded.get(key, 0) + 1
            continue
        groups[key].append(r)
    rows = []
    for key, rs in groups.items():
        ms = [_get(r, "metrics") or {} for r in rs]
        scored = [m for m in ms if m.get("completed") is not None]
        clean = [m for m in ms if m.get("version") == "clean"]
        seeded = [m for m in ms if m.get("version") == "seeded"]
        sor = [m["steps_over_ref"] for m in ms if isinstance(m.get("steps_over_ref"), (int, float))]
        tokens = [m["memory_tokens_injected"] for m in ms
                  if isinstance(m.get("memory_tokens_injected"), (int, float))]
        traps = [t for m in ms for t in (m.get("memory_traps") or {}).values()]
        exp = [t for t in traps if t.get("exposed")]
        unexp = [t for t in traps if not t.get("exposed")]
        targets = [t for m in ms for t in (m.get("memory_attribution") or {}).values()]
        accepted = [_accepted(r, curation) for r in rs]
        row: dict[str, Any] = {"condition": key[0], "stage": key[1]}
        if by_app:
            row["app"] = key[2]
        present = sum(len(m.get("bugs_present") or []) for m in seeded)
        found = sum(len(m.get("bugs_found") or []) for m in seeded)
        row.update({
            "memory_internal": is_memory_condition(key[0]),
            "episodes": len(ms),
            "excluded": excluded.get(key, 0),
            "completion": _rate(sum(1 for m in scored if m.get("completed")), len(scored)),
            "steps_over_ref_mean": _mean(sor),
            "steps_over_ref_median": round(statistics.median(sor), 4) if sor else None,
            "steps_over_ref_n": len(sor),
            "clean_episodes": len(clean),
            "false_alarm_rate": _rate(sum(1 for m in clean if (m.get("false_reports") or 0) > 0),
                                      len(clean)),
            "bugs_present": present,
            "bugs_found": found,
            "recall": _rate(found, present),
            "memory_tokens_injected_mean": _mean(tokens),
            "proposals_count": sum(m.get("memory_proposals_count") or 0 for m in ms),
            "proposals_accepted": (sum(a for a in accepted if a is not None)
                                   if any(a is not None for a in accepted) else None),
            "trap_episodes": sum(1 for m in ms if m.get("memory_traps")),
            "trap_exposed": len(exp),
            "trap_harmed": sum(1 for t in exp if t.get("harmed") is True),
            "trap_harm_rate": _rate(sum(1 for t in exp if t.get("harmed") is True),
                                    sum(1 for t in exp if t.get("harmed") is not None)),
            "trap_unexposed": len(unexp),
            "trap_base_harmed": sum(1 for t in unexp if t.get("harmed") is True),
            "trap_base_rate": _rate(sum(1 for t in unexp if t.get("harmed") is True),
                                    sum(1 for t in unexp if t.get("harmed") is not None)),
            "pair_targets": len(targets),
            "pair_found": sum(1 for t in targets if t.get("found")),
            "pair_recall": _rate(sum(1 for t in targets if t.get("found")), len(targets)),
            "pair_diff_exposed": sum(1 for t in targets if t.get("qualock_diff_exposes")),
            "pair_hunt_found": sum(1 for t in targets if t.get("qualock_hunt_found")),
            "pair_found_unaided": sum(1 for t in targets if t.get("found")
                                      and not t.get("qualock_diff_exposes")
                                      and not t.get("qualock_hunt_found")),
        })
        rows.append(row)
    return sorted(rows, key=lambda r: (str(r["condition"]), stage_number(r["stage"]) or 0,
                                       str(r["stage"] or ""), str(r.get("app") or "")))


#: Every per-episode metric this module can write into result.json's `metrics`. The contract for the lab kind and the smoke
#: gates; `tests/test_memory_metrics.py` keeps it in step with the code.
EPISODE_METRICS = (
    "ref_steps", "steps_over_ref",
    "memory_stage",
    "memory_proposals_count", "memory_proposals_updates", "memory_proposals_contradicts",
    "memory_proposals_error", "memory_proposals_invalid",
    "memory_traps", "memory_trap_harm",
    "memory_attribution", "qualock_diff_exposes", "qualock_hunt_found",
)

#: The memory-health row keys (`health` / `unit_health`).
HEALTH_METRICS = (
    "entries", "entries_org", "entries_app", "invalidated", "contradicted", "seen_total",
    "contra_total", "bytes", "tokens", "contradiction_rate", "contra_ratio",
    "lint_failures", "dropped", "parent_entries", "added", "removed", "invalidated_new",
    "growth", "invalidation_rate",
)

