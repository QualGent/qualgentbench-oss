"""Re-label a rescored run under the corpus it was rescored against (QUA-3553).

A run's set key is computed from the corpus and held-out versions RECORDED in each
episode's metrics (`view._set_key`). A rescore keeps those stamps and only adds
`rescored_with`, because in general a rescore against another corpus is a different
measurement. One kind of change is not: a SCORING-ONLY change. Examples are a symptom
wording, a known clean-build behaviour, or a measured text. The agent saw the same
brief, ran the same route and met the same build, and only the answer key moved. After
such a change, the rescored verdicts ARE a measurement of the new corpus, and the board
should be filed under the new set key.

`relabel_run` does that, under two guarantees:

* **It refuses unless only scoring inputs changed.** The old corpus must be supplied as
  directories that hash to the versions the run recorded (`corpus.version_of`), so the
  comparison is against what actually ran. Every app's MEASUREMENT must then be
  identical in the old and current corpus: per case the agent-facing brief (`name`,
  `steps`, `expected_outcome`), the step budget, the seeded bug ids, and the whole
  `check:` block (the device oracle runs at episode time and a rescore cannot re-run
  it); per app the journey APK's sha256 and the set of case ids. Everything else in the
  case file and the truth is the answer key.
* **It never touches the original run.** It writes a COPY under a new run id into a
  separate runs directory. In that copy every episode is rescored in place
  (`rescore.rescore`, so `rescored_from` / `rescored_with` hold the trace) and then
  re-stamped: `metrics.corpus_version` / `heldout_version` become the current corpus,
  and `restamped_from` on the result keeps the run id, versions and scorer it had. An
  episode whose agent never launched (staging failed: excluded, no transcript, no report)
  is copied and re-stamped without a rescore; any other episode without a transcript is
  refused.

Not compared: the benchmark spec (`device_setup`, seeded patches). Its patches are
covered by the APK sha256. `device_setup` is not part of `corpus_version` at all, so a
relabel cannot make the set key less precise than it already is.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from . import corpus, journey
from .rescore import journey_tasks_by_id, rescore

#: The per-case keys that define what an episode MEASURED. A change to any of them
#: changes what the agent saw or what the device was asked, so it refuses a relabel.
MEASUREMENT_CASE_KEYS = ("name", "steps", "expected_outcome", "step_budget", "check")
#: The files `relabel_run` rewrites the run id in (relative to the run's `_runs/<id>/`).
_RUN_STATE_JSON = ("plan.json", "board.json", "stop.json", "imported.json")


#: The status of an episode whose agent was never launched (`never_launched`): copied and
#: re-stamped, not rescored.
NOT_LAUNCHED = "not-launched"


def never_launched(result: dict) -> bool:
    """Did staging fail before the agent started? Then the episode has no transcript and no
    report, it is excluded (`env_failure`), and no scoring change can move it."""
    m = result.get("metrics") or {}
    return bool(m.get("staging_failed")) and bool(m.get("env_failure") or m.get("infra_failure"))


class RelabelRefused(Exception):
    """A relabel that would file a run under a set it did not measure. `problems` lists
    every reason, one line each."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("relabel refused:\n  " + "\n  ".join(self.problems))


# ── what a corpus measures ─────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RelabelRefused([f"{path.name}: does not load ({exc})"]) from None
    return doc if isinstance(doc, dict) else {}


def measurement(doc: dict) -> dict[str, Any]:
    """The measurement-defining part of one case document. See the module docstring."""
    apk = doc.get("apk") if isinstance(doc.get("apk"), dict) else {}
    cases: dict[str, dict[str, Any]] = {}
    for case in doc.get("test_cases") or []:
        if not isinstance(case, dict) or case.get("id") is None:
            continue
        row = {k: case.get(k) for k in MEASUREMENT_CASE_KEYS}
        row["bugs"] = [b["id"] for b in journey.case_bugs(case)]
        cases[str(case["id"])] = row
    return {"apk_sha256": apk.get("sha256"), "cases": cases}


def measurement_of(root: Path | None) -> dict[str, dict[str, Any]]:
    """{app id: measurement} for every case file under a data root."""
    if root is None or not (root / "test-cases").is_dir():
        return {}
    return {p.stem: measurement(_load(p)) for p in sorted((root / "test-cases").glob("*.yaml"))}


def measurement_changes(old_root: Path | None, new_root: Path | None) -> list[str]:
    """Every difference in WHAT is measured between two data roots, one line each.
    Empty means only scoring inputs differ. Lines name apps and case ids (callers that
    print them for a held-out root keep that output private)."""
    old, new = measurement_of(old_root), measurement_of(new_root)
    out: list[str] = []
    for app in sorted(set(old) | set(new)):
        if app not in new:
            out.append(f"{app}: removed from the corpus")
            continue
        if app not in old:
            out.append(f"{app}: added to the corpus")
            continue
        a, b = old[app], new[app]
        if a["apk_sha256"] != b["apk_sha256"]:
            out.append(f"{app}: journey APK changed ({a['apk_sha256']} -> {b['apk_sha256']})")
        for cid in sorted(set(a["cases"]) | set(b["cases"])):
            ca, cb = a["cases"].get(cid), b["cases"].get(cid)
            if ca is None or cb is None:
                out.append(f"{app}/{cid}: case {'added' if ca is None else 'removed'}")
                continue
            for key in (*MEASUREMENT_CASE_KEYS, "bugs"):
                if ca.get(key) != cb.get(key):
                    out.append(f"{app}/{cid}: `{key}` changed")
    return out


# ── the run ────────────────────────────────────────────────────────────────────

def _run_results(runs_dir: Path, run_id: str) -> list[tuple[Path, dict]]:
    """(episode dir, result.json) for every episode of `run_id` under `runs_dir`."""
    out = []
    for path in sorted(runs_dir.glob("*/*/result.json")):
        try:
            res = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if res.get("run_id") == run_id:
            out.append((path.parent, res))
    return out


def _one(values: set, what: str, problems: list[str]) -> Any:
    if len(values) != 1:
        problems.append(f"the run mixes {what}: {sorted(map(str, values))}")
        return None
    return next(iter(values))


def _swap_run_id(obj: Any, old: str, new: str) -> Any:
    """Every string equal to `old` (whole value, never a substring) replaced by `new`."""
    if isinstance(obj, dict):
        return {k: _swap_run_id(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_swap_run_id(v, old, new) for v in obj]
    return new if obj == old else obj


def _rewrite_json(path: Path, old: str, new: str) -> None:
    if path.suffix == ".jsonl":
        lines = []
        for line in path.read_text().splitlines():
            try:
                lines.append(json.dumps(_swap_run_id(json.loads(line), old, new)))
            except ValueError:
                lines.append(line)
        path.write_text("\n".join(lines) + ("\n" if lines else ""))
        return
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    path.write_text(json.dumps(_swap_run_id(doc, old, new), indent=2))


@dataclasses.dataclass
class RelabelReport:
    run_id: str
    new_run_id: str
    out_dir: Path
    episodes: int
    recorded: dict[str, Any]
    now: dict[str, Any]
    statuses: dict[str, int]


def check_relabel(results: list[dict], old_public: Path, old_heldout: Path | None
                  ) -> tuple[dict[str, Any], list[str]]:
    """The recorded stamp of a run's journey episodes and every reason not to relabel it
    under the CURRENT corpus (`corpus.stamp()`, held-out from `QGB_HELDOUT_DIR`)."""
    problems: list[str] = []
    if not results:
        return {}, ["the run has no episode"]
    if bad := sorted({str(r.get("task_type")) for r in results} - {journey.TASK_TYPE}):
        problems.append(f"the run holds non-journey episodes ({', '.join(bad)})")
    ms = [r.get("metrics") or {} for r in results]
    rec_c = _one({m.get("corpus_version") for m in ms}, "corpus versions", problems)
    rec_h = _one({m.get("heldout_version") for m in ms}, "held-out versions", problems)
    recorded = {"corpus_version": rec_c, "heldout_version": rec_h,
                "scorer_versions": sorted({str(m.get("scorer_version")) for m in ms})}
    if rec_c is None and not problems:
        problems.append("the run's episodes carry no corpus version")
    now = corpus.stamp()
    if (got := corpus.version_of(old_public)) != rec_c:
        problems.append(f"--old-corpus hashes to {got}, but the run recorded corpus {rec_c}")
    if rec_h is None:
        if old_heldout is not None:
            problems.append("the run recorded no held-out version, but --old-heldout was given")
        if now["heldout_version"] is not None:
            problems.append("the run ran without the held-out split; re-labelling it under "
                            "one would claim held-out episodes it never ran")
    else:
        if old_heldout is None:
            problems.append(f"the run recorded held-out {rec_h}: pass --old-heldout")
        elif (got := corpus.version_of(old_heldout)) != rec_h:
            problems.append(f"--old-heldout hashes to {got}, but the run recorded held-out {rec_h}")
        if now["heldout_version"] is None:
            problems.append("no current held-out split (QGB_HELDOUT_DIR): the held-out episodes "
                            "could not be rescored")
    if not problems and (rec_c, rec_h) == (now["corpus_version"], now["heldout_version"]):
        problems.append("the run already measured the current corpus: nothing to re-label "
                        "(a plain rescore is enough)")
    if not problems:
        changes = measurement_changes(old_public, corpus.PACKAGED)
        if rec_h is not None:
            held = measurement_changes(old_heldout, corpus.heldout_dir())
            # Held-out app and case ids are never printed from here: the count only.
            if held:
                changes.append(f"held-out split: {len(held)} measurement change(s) "
                               f"(run `measurement_changes` against it privately to list them)")
        problems += changes
    return {"recorded": recorded, "now": now}, problems


def relabel_run(runs_dir: Path, run_id: str, *, new_run_id: str, out_dir: Path,
                old_public: Path, old_heldout: Path | None = None) -> RelabelReport:
    """Copy `run_id` from `runs_dir` into `out_dir` as `new_run_id`, rescore every
    episode against the current corpus and re-stamp it with the current versions.
    Raises `RelabelRefused` before writing anything when the run measured something the
    current corpus does not (see the module docstring)."""
    runs_dir, out_dir = Path(runs_dir), Path(out_dir)
    if new_run_id == run_id:
        raise RelabelRefused(["the new run id must differ from the original"])
    if out_dir.resolve() == runs_dir.resolve():
        raise RelabelRefused(["--out-runs-dir must be a different directory from --runs-dir: "
                              "the copy keeps the original's episode directory names"])
    if (out_dir / "_runs" / new_run_id).exists() or _run_results(out_dir, new_run_id):
        raise RelabelRefused([f"{new_run_id} already exists under {out_dir}"])
    found = _run_results(runs_dir, run_id)
    stamps, problems = check_relabel([r for _, r in found], Path(old_public),
                                     Path(old_heldout) if old_heldout else None)
    # An episode with no transcript cannot be rescored. One whose agent was never
    # launched (staging failed, so the episode is excluded and has no report) is carried
    # over as recorded: no answer key can move it. Any other one is refused here, before
    # anything is written.
    missing = [d for d, r in found if not (d / "agent" / "transcript.txt").is_file()]
    stray = [d for d in missing if not never_launched(dict(found)[d])]
    if stray:
        problems.append(f"{len(stray)} episode(s) have no transcript although their agent "
                        f"ran: {', '.join(d.name for d in stray[:5])}")
    if problems:
        raise RelabelRefused(problems)

    tasks = journey_tasks_by_id()
    statuses: dict[str, int] = {}
    copies: list[Path] = []
    for ep_dir, _ in found:
        dest = out_dir / ep_dir.parent.name / ep_dir.name
        shutil.copytree(ep_dir, dest, ignore=shutil.ignore_patterns("view"))
        copies.append(dest)
    state = runs_dir / "_runs" / run_id
    if state.is_dir():
        shutil.copytree(state, out_dir / "_runs" / new_run_id,
                        ignore=shutil.ignore_patterns("view", "*.lock"))
        new_state = out_dir / "_runs" / new_run_id
        for p in [*(new_state / n for n in _RUN_STATE_JSON), new_state / "schedule.jsonl",
                  *sorted((new_state / "episodes").glob("*.json"))]:
            if p.is_file():
                _rewrite_json(p, run_id, new_run_id)
    rec, now = stamps["recorded"], stamps["now"]
    for dest in copies:
        for name in ("episode.json",):
            if (dest / name).is_file():
                _rewrite_json(dest / name, run_id, new_run_id)
        res = json.loads((dest / "result.json").read_text())
        res["run_id"] = new_run_id
        (dest / "result.json").write_text(json.dumps(res, indent=2))
        if dest.name in {d.name for d in missing}:
            status = NOT_LAUNCHED            # carried over as recorded (see above)
        else:
            status, _, _, _ = rescore(dest, tasks, dry_run=False)
        statuses[status.split(":")[0]] = statuses.get(status.split(":")[0], 0) + 1
        res = json.loads((dest / "result.json").read_text())
        m = res.setdefault("metrics", {})
        res["restamped_from"] = {"run_id": run_id, "corpus_version": m.get("corpus_version"),
                                 "heldout_version": m.get("heldout_version"),
                                 "scorer_versions": rec["scorer_versions"]}
        m["corpus_version"] = now["corpus_version"]
        m["heldout_version"] = now["heldout_version"]
        (dest / "result.json").write_text(json.dumps(res, indent=2))
    if bad := {k: v for k, v in statuses.items() if k not in ("rescored", "unrecoverable", NOT_LAUNCHED)}:
        # Copies are already written; say so plainly rather than leave a half-labelled run.
        raise RelabelRefused([f"{n} episode(s) could not be rescored ({k}); the copy under "
                              f"{out_dir} is incomplete: delete it" for k, n in bad.items()])
    return RelabelReport(run_id=run_id, new_run_id=new_run_id, out_dir=out_dir,
                         episodes=len(copies), recorded=rec, now=now, statuses=statuses)
