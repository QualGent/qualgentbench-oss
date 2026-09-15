#!/usr/bin/env python3
"""Re-score saved journey episodes from their artifacts — no agent, no device.

Scoring is a text comparison against the authored key, so when a symptom vocabulary
or a marker is edited, every past episode can be rescored for free: rebuild the task
from the current test-case file, feed the saved transcript + findings file to
`journey.journey_verdict`, and write the new verifier fields into result.json (the
previous ones are kept under `rescored_from`).

After the per-episode lines it prints the journey board the rescored episodes make
(`journey.summary`) and its Rates block — false-alarm rate per clean case, catch rate
per seeded defect, clean-run integrity at 200, blocker recall — computed from the
RESCORED metrics, so `--dry-run` shows the board a rescore would publish without
writing a byte. `--projection N_CLEAN N_SEEDED` adds the composed projection for a
suite of that size (expected false alarms, expected misses, clean-run integrity).

    uv run python scripts/rescore_journey.py --run <run_id>          # one run
    uv run python scripts/rescore_journey.py --app tasksorg          # every episode of an app
    uv run python scripts/rescore_journey.py --dry-run --run <id>    # print, do not write
    uv run python scripts/rescore_journey.py --dry-run --projection 200 50"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import bugs, journey, rates                 # noqa: E402
from qualgentbench.leaderboard import load_results            # noqa: E402
from qualgentbench.result import VerifierResult, resolve_artifact_dir  # noqa: E402

_KEEP = ("tooling", "findings_file", "oracle_detail", "oracle_result", "hook_steps", "truncated", "timed_out", "exit_code",
         "step_cap", "workspace", "metered_total", "device_serial", "ended_in_package",
         "off_app", "staging_failed", "active_bugs_written")


def _restore_oracle(spec: dict, old: dict) -> None:
    """A db/content oracle is evaluated on the device once, right after the agent exits,
    and a rescore cannot repeat it. The runner keeps that outcome in the saved metrics
    under `oracle` ({mode, ok, why, detail, result}); the scorer reads it from the flat
    `oracle_result`/`oracle_detail` keys. Without this bridge every db-oracle episode
    rescored to completion=None (measured 2026-09-14: 28 of 70 saved episodes flipped
    True -> None, and a board printed live as "(2 un)" rescored as "(6 un)")."""
    if spec.get("oracle_result") is not None:
        return
    saved = old.get("oracle") or {}
    if not isinstance(saved, dict) or saved.get("mode") not in ("db", "content"):
        return
    result = saved.get("result")
    if result is None:                       # runs recorded before `result` was persisted
        result = {True: "holds", False: "violated"}.get(saved.get("ok"))
    if result is None:
        return
    spec["oracle_result"] = result
    spec.setdefault("oracle_detail", saved.get("detail") or "")


def rescore(run_dir: Path, tasks_by_id: dict, dry_run: bool
            ) -> tuple[str, float | None, float | None, VerifierResult | None]:
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
    spec = dict(task.bug_spec or {})
    spec["tooling"] = "raw" if result.get("condition") == "raw" else "mcp"
    for k in _KEEP:
        if k in old and k not in ("tooling",):
            spec[k] = old[k]
    _restore_oracle(spec, old)
    spec["truncated"] = bool(old.get("truncated"))
    spec["timed_out"] = bool(old.get("timed_out"))
    spec["hook_steps"] = old.get("hook_steps")
    spec["workspace"] = str(run_dir / "workspace")
    try:
        spec["findings_file"] = (run_dir / "workspace" / journey.FILENAME).read_text()
    except OSError:
        spec["findings_file"] = ""
    task.bug_spec = spec
    v = journey.journey_verdict(transcript, result.get("model") or "", task)
    before, after = old.get("completed"), v.metrics.get("completed")
    if not dry_run:
        # Keep run-time facts the scorer does not recompute (failure_class, provenance).
        merged = {**old, **v.metrics}
        merged["failure_class"] = old.get("failure_class")
        result["rescored_from"] = {k: old.get(k) for k in ("completed", "overall", "bugs_found",
                                                             "false_reports", "false_positives")}
        result["metrics"] = merged
        result["passed"] = v.passed
        result["score"] = v.score
        result["weighted_score"] = v.weighted_score
        result["criteria"] = v.criteria
        result["failure_reason"] = v.failure_reason
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    return "rescored", before, after, v


def projection_lines(rows: list[dict], n_clean: int, n_seeded: int) -> list[str]:
    """The composed projection per board row, from the row's own rate fields."""
    def r(prefix: str) -> rates.Rate | None:
        if row.get(f"{prefix}_rate") is None:
            return None
        lo, hi = row[f"{prefix}_ci"]
        return rates.Rate(k=row[f"{prefix}_k"], n=row[f"{prefix}_n"],
                          p=row[f"{prefix}_rate"], lo=lo, hi=hi)

    def num(v, ci, unit=""):
        if v is None:
            return "—"
        s = f"{v:.1f}{unit}"
        return s + (f" [{ci[0]:.1f}–{ci[1]:.1f}]" if ci else "")

    lines = [(f"Projection for a suite of {n_clean} clean cases and {n_seeded} seeded defects "
              "(linear from the measured rates; assumes the suite resembles the measured cases)")]
    for i, row in enumerate(rows, 1):
        p = rates.projection(r("false_alarm"), r("catch"), n_clean, n_seeded)
        integ = p["clean_run_integrity"]
        integ_ci = p["clean_run_integrity_ci"]
        integ_s = ("—" if integ is None else
                   f"{integ * 100:.0f}%" + (f" [{integ_ci[0] * 100:.0f}–{integ_ci[1] * 100:.0f}]"
                                           if integ_ci else ""))
        lines.append(f"  {i}. {row.get('agent')} · {row.get('model')} · {row.get('condition')}: "
                     f"expected false alarms {num(p['expected_false_alarms'], p['expected_false_alarms_ci'])}"
                     f" of {n_clean} · expected misses {num(p['expected_misses'], p['expected_misses_ci'])}"
                     f" of {n_seeded} · clean-run integrity {integ_s}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--run", help="run_id to rescore (default: every journey episode)")
    ap.add_argument("--app", help="only this app's cases")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--projection", nargs=2, type=int, metavar=("N_CLEAN", "N_SEEDED"),
                    help="also print expected false alarms / misses / clean-run integrity "
                         "for a suite of N_CLEAN clean cases and N_SEEDED seeded defects")
    args = ap.parse_args()

    tasks_by_id = {}
    for suite in bugs.load_apps():
        if args.app and suite["app"]["id"] != args.app:
            continue
        for t in journey.journey_tasks(suite):
            tasks_by_id[t.id] = t

    runs_dir = Path(args.runs_dir)
    results = load_results(runs_dir, run_id=args.run)
    changed = 0
    board = []          # the rescored results, in memory — the board is computed from these
    for r in results:
        episode_dir = resolve_artifact_dir(runs_dir, r)
        if r.task_type != journey.TASK_TYPE or episode_dir is None:
            continue
        status, before, after, v = rescore(episode_dir, tasks_by_id, args.dry_run)
        if status != "rescored":
            if status != "skip":
                print(f"  {r.task_id:36} {status}")
            continue
        mark = "" if before == after else "   <-- changed"
        if before != after:
            changed += 1
        print(f"  {r.task_id:36} {before} -> {after}{mark}")
        # Same merge as the on-disk write, so a dry run prints the board a write would.
        merged = {**(r.metrics or {}), **v.metrics, "failure_class": (r.metrics or {}).get("failure_class")}
        board.append(r.model_copy(update={"metrics": merged, "passed": v.passed, "score": v.score,
                                          "weighted_score": v.weighted_score}))
    print(f"{'would change' if args.dry_run else 'changed'} {changed} episode(s)")

    if board:
        rows = journey.summary(board)
        print()
        print("Board (F1 ranks; completion second):")
        for i, row in enumerate(rows, 1):
            eps = (f"{row['episodes']}/{row['planned_episodes']}" if row["excluded_episodes"]
                   else str(row["episodes"]))

            def pct(v):
                return "—" if v is None else f"{v * 100:.0f}%"
            print(f"  {i}. {row['agent']} · {row['model']} · {row['condition']}: episodes {eps} · "
                  f"cut {row['truncated']} · completion {pct(row['completion'])}"
                  f"{f' ({row['completion_unscored']} un)' if row['completion_unscored'] else ''} · "
                  f"bugs {row['bugs_found']}/{row['bugs_present']} · false rep. {row['false_reports']} · "
                  f"P {pct(row['precision'])} · R {pct(row['recall'])} · F1 {pct(row['f1'])}")
        print()
        for line in journey.rates_lines(rows):
            print(line)
        if args.projection:
            n_clean, n_seeded = args.projection
            print()
            for line in projection_lines(rows, n_clean, n_seeded):
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
