#!/usr/bin/env python3
"""Re-score saved journey episodes from their artifacts — no agent, no device.

Scoring is a text comparison against the authored key, so when a symptom vocabulary
or a marker is edited, every past episode can be rescored for free: rebuild the task
from the current test-case file, feed the saved transcript + findings file to
`journey.journey_verdict`, and write the new verifier fields into result.json (the
previous ones are kept under `rescored_from`).

    uv run python scripts/rescore_journey.py --run <run_id>          # one run
    uv run python scripts/rescore_journey.py --app tasksorg          # every episode of an app
    uv run python scripts/rescore_journey.py --dry-run --run <id>    # print, do not write"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import bugs, journey                # noqa: E402
from qualgentbench.leaderboard import load_results    # noqa: E402

_KEEP = ("tooling", "findings_file", "oracle_detail", "oracle_result", "hook_steps", "truncated", "timed_out", "exit_code",
         "step_cap", "workspace", "metered_total", "device_serial", "ended_in_package",
         "off_app", "staging_failed", "active_bugs_written")


def rescore(run_dir: Path, tasks_by_id: dict, dry_run: bool) -> tuple[str, float | None, float | None]:
    result = json.loads((run_dir / "result.json").read_text())
    if result.get("task_type") != journey.TASK_TYPE:
        return "skip", None, None
    tid = result["task_id"]
    if tid not in tasks_by_id:                    # an old run: bare case id = seeded version
        tid = journey.task_id(*journey.split_task_id(tid))
    task = tasks_by_id.get(tid)
    if task is None:
        return "no-case", None, None
    transcript_path = run_dir / "agent" / "transcript.txt"
    if not transcript_path.exists():
        return "no-transcript", None, None
    transcript = transcript_path.read_text()
    old = result.get("metrics") or {}
    spec = dict(task.bug_spec or {})
    spec["tooling"] = "raw" if result.get("condition") == "raw" else "mcp"
    for k in _KEEP:
        if k in old and k not in ("tooling",):
            spec[k] = old[k]
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
    return "rescored", before, after


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--run", help="run_id to rescore (default: every journey episode)")
    ap.add_argument("--app", help="only this app's cases")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tasks_by_id = {}
    for suite in bugs.load_apps():
        if args.app and suite["app"]["id"] != args.app:
            continue
        for t in journey.journey_tasks(suite):
            tasks_by_id[t.id] = t

    results = load_results(Path(args.runs_dir), run_id=args.run)
    changed = 0
    for r in results:
        if r.task_type != journey.TASK_TYPE or not r.artifact_dir:
            continue
        status, before, after = rescore(Path(r.artifact_dir), tasks_by_id, args.dry_run)
        if status != "rescored":
            if status != "skip":
                print(f"  {r.task_id:36} {status}")
            continue
        mark = "" if before == after else "   <-- changed"
        if before != after:
            changed += 1
        print(f"  {r.task_id:36} {before} -> {after}{mark}")
    print(f"{'would change' if args.dry_run else 'changed'} {changed} episode(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
