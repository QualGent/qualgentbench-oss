"""Re-score one saved journey episode from its artifacts — no agent, no device.

The reusable half of `scripts/rescore_journey.py` (moved here for QUA-2823 so the
package can import it: `qualgent-bench view` shows each episode's recorded AND rescored
verdict, and the rescored one must be exactly what `rescore_journey.py --dry-run`
prints). The script keeps the CLI, the board printing and the projection; everything
that turns one episode dir into a verdict lives here, unchanged.

Scoring is a text comparison against the authored key, so when a symptom vocabulary or
a marker is edited every past episode can be rescored for free: rebuild the task from
the current test-case file, feed the saved transcript + findings file to
`journey.journey_verdict`, and (unless `dry_run`) write the new verifier fields into
result.json, keeping the previous ones under `rescored_from`.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from . import bugs, failures, journey
from .contamination import devloop_default_roots
from .result import VerifierResult


def journey_tasks_by_id(app: str | None = None) -> dict:
    """Every journey task the current corpus builds (held-out included when the split
    is configured), keyed by task id — the lookup `rescore` takes. `app` narrows it to
    one app's cases."""
    tasks_by_id = {}
    for suite in bugs.load_apps():
        if app and suite["app"]["id"] != app:
            continue
        for t in journey.journey_tasks(suite):
            tasks_by_id[t.id] = t
    return tasks_by_id


_KEEP = ("tooling", "findings_file", "oracle_detail", "oracle_result", "hook_steps", "truncated", "timed_out", "exit_code",
         "step_cap", "workspace", "metered_total", "device_serial", "ended_in_package",
         "off_app", "staging_failed", "active_bugs_written")


def _restore_oracle(spec: dict, old: dict) -> None:
    """A device oracle — `db`/`content`, or a liveness oracle (`crash`/`anr`/`stuck`) — is
    evaluated on the device once, right after the agent exits, and a rescore cannot
    repeat it. The scorer keeps that outcome in the saved metrics under `oracle`
    ({mode, ok, why, detail, result}); it reads it from the flat
    `oracle_result`/`oracle_detail` keys. Without this bridge every such episode rescored
    to completion=None (measured 2026-09-14: 28 of 70 saved db episodes flipped True ->
    None, and a board printed live as "(2 un)" rescored as "(6 un)"; QUA-2793: the bridge
    still only knew db/content, so the stuck case of the QUA-2785 board flipped the same
    way). The mode list is the scorer's own (`journey.DEVICE_ORACLE_MODES`), and the
    saved outcome is only restored into the SAME mode — an outcome recorded for another
    oracle than the case now declares answers a different question."""
    if spec.get("oracle_result") is not None:
        return
    saved = old.get("oracle") or {}
    mode = (spec.get("oracle") or {}).get("mode")
    if (not isinstance(saved, dict) or saved.get("mode") not in journey.DEVICE_ORACLE_MODES
            or saved.get("mode") != mode):
        return
    result = saved.get("result")
    if result is None:                       # runs recorded before `result` was persisted
        result = {True: "holds", False: "violated"}.get(saved.get("ok"))
    if result is None:
        return
    spec["oracle_result"] = result
    spec.setdefault("oracle_detail", saved.get("detail") or "")


def _oracle_unrecoverable(spec: dict, old: dict) -> str | None:
    """Why this episode's device-oracle outcome cannot be recovered, or None when it can
    (or is not needed). Read AFTER `_restore_oracle`.

    The rescore needs the outcome when the case's oracle is one the harness evaluates on
    the device and the arm consults it (an expected-FAIL arm is judged on the verdict and
    the blocking bug, never the oracle). It is recovered when `_restore_oracle` found it,
    and it is KNOWN to have been missing live when the saved record is for this same mode
    and says so — `result` persisted as None, or a `why` from the scorer's device-oracle
    branch ("db oracle not evaluated (inconclusive)"): then None is exactly what the live
    scorer saw. Anything else — no saved record, a record for another mode, a record from
    a scorer that did not yet treat this mode as a device oracle — means the live
    outcome was never persisted, and rescoring would turn a recorded completion into None
    for a reason that is not the agent's. Such an episode is reported and left alone."""
    mode = (spec.get("oracle") or {}).get("mode")
    if mode not in journey.DEVICE_ORACLE_MODES or spec.get("blocking"):
        return None
    if spec.get("oracle_result") is not None:
        return None
    saved = old.get("oracle")
    if isinstance(saved, dict) and saved.get("mode") == mode and (
            "result" in saved or str(saved.get("why") or "").startswith(f"{mode} oracle")):
        return None
    if not isinstance(saved, dict):
        return f"no saved {mode} oracle outcome"
    return (f"no saved {mode} oracle outcome (recorded oracle: mode {saved.get('mode')!r}, "
            f"{saved.get('why') or 'no reason'})")


#: The completion half of a journey verdict: what `keep_recorded_completion` carries over
#: from the recording when the device-oracle outcome the completion needs is lost.
COMPLETION_KEYS = ("completed", "completion_scored", "completion_reason", "witness", "oracle")


def keep_recorded_completion(v: VerifierResult, old: dict, lost: str) -> VerifierResult:
    """`v` (a fresh verdict) with its COMPLETION replaced by the recorded one.

    An `unrecoverable` episode (`_oracle_unrecoverable`) cannot have its completion
    recomputed: the device answer it needs was never saved, and a rescore would turn a
    recorded True into None for a reason that is not the agent's. Its BUG side needs no
    device — it is the saved transcript and findings file against the current key — so
    it is rescored like every other episode's. Until QUA-2807 the whole episode was
    skipped, so a scorer or vocabulary fix never reached those episodes' bug side.
    `passed`/`score`/`reward` and `criteria.completed` are recomputed from the mixed
    halves with `journey_verdict`'s own formulas; `completion_kept` names why."""
    m = dict(v.metrics)
    for k in COMPLETION_KEYS:
        if k in old:
            m[k] = old[k]
    completed = m.get("completed")
    scored = old.get("completion_scored", completed is not None)
    m["completion_scored"] = scored
    m["completion_kept"] = lost
    missed, false_reports = m.get("bugs_missed") or [], m.get("false_reports") or 0
    passed = bool((completed if scored else True) and not missed and false_reports == 0)
    m["reward"] = 1.0 if passed else 0.0
    reasons = [] if completed else [f"completion as recorded ({completed}) — {lost}"]
    reasons += journey.bug_side_reasons(m.get("version"), false_reports, missed,
                                        m.get("report_errors") or [])
    return v.model_copy(update={
        "passed": passed,
        "score": 1.0 if (completed if scored else passed) else 0.0,
        "criteria": {**(v.criteria or {}), "completed": completed is True},
        "failure_reason": "; ".join(reasons) or None,
        "metrics": m,
    })


def bug_side_delta(old: dict, new: dict) -> str:
    """'' when the bug side (bugs found, false reports) is unchanged, else what moved."""
    parts = []
    f0, f1 = sorted(old.get("bugs_found") or []), sorted(new.get("bugs_found") or [])
    if f0 != f1:
        parts.append(f"found {f0} -> {f1}")
    r0, r1 = old.get("false_reports") or 0, new.get("false_reports") or 0
    if r0 != r1:
        parts.append(f"false reports {r0} -> {r1}")
    return "; ".join(parts)


#: Hard contamination kinds a rescore cannot re-check, so it carries them over from the
#: recording instead of recomputing them. `flag_nonce` (QUA-2804): the per-episode nonce
#: is never persisted (it must not land in published output), so a rescore has no nonce
#: to look for; the hit is a transcript fact the agent cannot un-earn. Every other hard
#: kind is recomputed from what IS saved — the transcript, the findings file, the
#: provenance (`adbd_at_end`, `mcp_isolation`) — so a scorer fix can still un-void an
#: episode (a meter false positive), which carrying them all over would forbid.
PRESERVED_CONTAMINATION = frozenset({"flag_nonce"})

#: The verdict fields a rescore replaces in result.json; the rest is run-time record.
VERDICT_FIELDS = ("passed", "score", "weighted_score", "criteria", "failure_reason")


def merge_metrics(old: dict, fresh: dict, provenance: dict | None) -> dict:
    """The metrics a rescored episode carries: the recording's run-time facts, overlaid
    with the fresh verdict, plus every void the rescore cannot recompute. The one merge
    `--dry-run` and the write both use, so a dry run prints exactly the board a write
    would publish (QUA-2816: the dry run once skipped the `flag_nonce` carry-over, so a
    nonce-voided episode counted again on every published board)."""
    merged = {**old, **fresh}
    # A run-time fact the scorer does not recompute.
    merged["failure_class"] = old.get("failure_class")
    # The MCP session record is provenance, not transcript: re-read it (QUA-2806).
    failures.apply_mcp_integrity(merged, provenance)
    kept = PRESERVED_CONTAMINATION & set(old.get("contamination_reasons") or [])
    if kept:
        merged["contaminated"] = True
        merged["contamination_reasons"] = sorted(
            set(merged.get("contamination_reasons") or []) | kept)
        hits = list(merged.get("contamination_hits") or [])
        hits += [h for h in old.get("contamination_hits") or []
                 if isinstance(h, dict) and h.get("kind") in kept and h not in hits]
        merged["contamination_hits"] = hits[:20]      # the cap `as_metrics` applies
    return merged


def rescored_fields(v: VerifierResult) -> dict:
    """What a rescore replaces in an episode's result, from a MERGED verdict: the same
    dict is written into result.json and copied onto the in-memory board result."""
    return {"metrics": v.metrics, **{k: getattr(v, k) for k in VERDICT_FIELDS}}


def rescore(run_dir: Path, tasks_by_id: dict, dry_run: bool
            ) -> tuple[str, float | None, float | None, VerifierResult | None]:
    """Rescore one saved episode. The returned verdict carries the MERGED metrics
    (`merge_metrics`) — exactly what a write puts in result.json, dry run or not."""
    result = json.loads((run_dir / "result.json").read_text())
    if result.get("task_type") != journey.TASK_TYPE:
        return "skip", None, None, None
    tid = result["task_id"]
    if tid not in tasks_by_id:                    # an old run: bare case id = seeded version
        tid = journey.task_id(*journey.split_task_id(tid))
    task = tasks_by_id.get(tid)
    if task is None:
        return "no-case", None, None, None
    transcript_path = run_dir / "agent" / "transcript.txt"
    if not transcript_path.exists():
        return "no-transcript", None, None, None
    transcript = transcript_path.read_text()
    old = result.get("metrics") or {}
    provenance = result.get("provenance") or {}
    spec = dict(task.bug_spec or {})
    spec["tooling"] = "raw" if result.get("condition") == "raw" else "mcp"
    for k in _KEEP:
        if k in old and k not in ("tooling",):
            spec[k] = old[k]
    _restore_oracle(spec, old)
    lost = _oracle_unrecoverable(spec, old)
    spec["truncated"] = bool(old.get("truncated"))
    spec["timed_out"] = bool(old.get("timed_out"))
    spec["hook_steps"] = old.get("hook_steps")
    spec["workspace"] = str(run_dir / "workspace")
    # adbd's privilege after the agent is a device fact saved in provenance (QUA-2795);
    # the scan reads it as it did live, so a rooted episode stays void on rescore.
    spec["adbd_at_end"] = provenance.get("adbd_at_end")
    # The DevLoop artifact roots the live scan used: the defaults plus what the server
    # reported (`run_episode`), saved in provenance. Without the reported ones a
    # `devloop_artifacts` hit under a non-default root would be un-voided by a rescore.
    mcp_isolation = provenance.get("mcp_isolation")
    spec["devloop_roots"] = devloop_default_roots() + list(
        (mcp_isolation if isinstance(mcp_isolation, dict) else {}).get("artifact_roots") or [])
    # Device facts read after the agent exited; the scorer only echoes them, so a
    # rescore that did not feed them back would zero them in the written metrics.
    if "app_crashes" in old:
        spec["app_crash_count"] = old["app_crashes"]
    if "fault_fired" in old:
        spec["fired"] = old["fault_fired"]
    try:
        spec["findings_file"] = (run_dir / "workspace" / journey.FILENAME).read_text()
    except OSError:
        spec["findings_file"] = ""
    # A COPY of the task: the corpus task is shared by every episode of the case, and a
    # spec written back onto it carried one episode's restored oracle outcome into the next
    # (`_restore_oracle` keeps an `oracle_result` already set).
    task = dataclasses.replace(task, bug_spec=spec)
    v = journey.journey_verdict(transcript, result.get("model") or "", task)
    if lost:
        # The completion stands as recorded; the bug side is rescored like any episode's.
        v = keep_recorded_completion(v, old, lost)
    v = v.model_copy(update={"metrics": merge_metrics(old, v.metrics, provenance)})
    before, after = old.get("completed"), v.metrics.get("completed")
    if not dry_run:
        result["rescored_from"] = {k: old.get(k) for k in ("completed", "overall", "bugs_found",
                                                             "false_reports", "false_positives")}
        result.update(rescored_fields(v))
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    return (f"unrecoverable: {lost}" if lost else "rescored"), before, after, v
