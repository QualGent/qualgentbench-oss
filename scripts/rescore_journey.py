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
writing a byte. Each board row also carries $/episode (mean over priced episodes, the unpriced
count as a suffix) and min/episode (median agent wall-clock), the same cells the console
table prints (`journey.cost_cells`). `--projection N_CLEAN N_SEEDED` adds the composed
projection for a suite of that size (expected false alarms, expected misses, clean-run
integrity) and its prior-weighted error count (false alarms + misses).

    uv run python scripts/rescore_journey.py --run <run_id>          # one run
    uv run python scripts/rescore_journey.py --app tasksorg          # every episode of an app
    uv run python scripts/rescore_journey.py --dry-run --run <id>    # print, do not write
    uv run python scripts/rescore_journey.py --dry-run --projection 200 50"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import bugs, corpus, journey, rates        # noqa: E402,F401
from qualgentbench.config import default_runs_dir             # noqa: E402
from qualgentbench.leaderboard import load_results            # noqa: E402
from qualgentbench.result import resolve_artifact_dir         # noqa: E402
# The per-episode rescore lives in the package (QUA-2823: `qualgent-bench view` shows the
# same rescored verdict this script prints). Re-exported here, unchanged, so this script
# and every caller that loads it (the tests) keep one implementation.
from qualgentbench.rescore import (  # noqa: E402,F401
    _KEEP,
    COMPLETION_KEYS,
    PRESERVED_CONTAMINATION,
    VERDICT_FIELDS,
    _oracle_unrecoverable,
    _restore_oracle,
    bug_side_delta,
    journey_tasks_by_id,
    keep_recorded_completion,
    merge_metrics,
    rescore,
    rescored_fields,
)

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
        # Prior-weighted cost (QUA-2780): what this suite's mix of clean and seeded
        # cases would cost a team in wrong answers — a printed line, NOT a ranking key.
        lines.append(f"     prior-weighted errors (false alarms + misses): "
                     f"{num(p['expected_errors'], None)} over {n_clean + n_seeded} "
                     f"(bug prior {rates.bug_prior(n_clean, n_seeded)})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", type=Path, default=default_runs_dir(),
                    help="episode tree (default: ~/.qualgentbench/runs; runs from before "
                         "QUA-2778 are in ./runs)")
    ap.add_argument("--run", help="run_id to rescore (default: every journey episode)")
    ap.add_argument("--app", help="only this app's cases")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--projection", nargs=2, type=int, metavar=("N_CLEAN", "N_SEEDED"),
                    help="also print expected false alarms / misses / clean-run integrity "
                         "for a suite of N_CLEAN clean cases and N_SEEDED seeded defects")
    args = ap.parse_args()

    tasks_by_id = journey_tasks_by_id(args.app)

    runs_dir = Path(args.runs_dir)
    results = load_results(runs_dir, run_id=args.run)
    changed = 0
    board = []          # the rescored results, in memory — the board is computed from these
    # Every saved episode carries the corpus version it was RECORDED under (metrics
    # `corpus_version`, kept through the merge below); the rescore reads the CURRENT
    # files. The two are printed side by side, because a rescore across a corpus edit
    # is a different measurement, not a correction.
    current = corpus.stamp()
    print(f"current corpus {current['corpus_version']}"
          + (f" · held-out {current['heldout_version']}" if current["heldout_version"] else ""))
    stale = 0
    unrecoverable = unrecoverable_bugs_changed = 0
    for r in results:
        episode_dir = resolve_artifact_dir(runs_dir, r)
        if r.task_type != journey.TASK_TYPE or episode_dir is None:
            continue
        status, before, after, v = rescore(episode_dir, tasks_by_id, args.dry_run)
        kept = status.startswith("unrecoverable")
        if status != "rescored" and not kept:
            if status != "skip":
                print(f"  {r.task_id:36} {status}")
            continue
        if kept:
            # The completion stays as recorded (it cannot move); the bug side is rescored.
            # The episode stays on the board — dropping it would shrink the board as
            # silently as a None-flip would have skewed it.
            unrecoverable += 1
            delta = bug_side_delta(r.metrics or {}, v.metrics)
            if delta:
                unrecoverable_bugs_changed += 1
            print(f"  {r.task_id:36} {status} — recorded completion {before} kept; bug side "
                  f"rescored: {delta or 'unchanged'}")
        mark = "" if before == after else "   <-- changed"
        if before != after:
            changed += 1
        m0 = r.metrics or {}
        key = "heldout_version" if m0.get("heldout") else "corpus_version"
        recorded, now = m0.get(key), current[key]
        if recorded != now:
            stale += 1
        ver = (f"recorded {recorded or 'unstamped'}"
               + (f" ≠ current {now}" if recorded != now else "")
               + (" [held-out]" if m0.get("heldout") else ""))
        print(f"  {r.task_id:36} {before} -> {after}{mark}   {ver}")
        # `v` carries the merged metrics a write puts on disk (`merge_metrics`), so a
        # dry run prints the board a write would.
        board.append(r.model_copy(update=rescored_fields(v)))
    print(f"{'would change' if args.dry_run else 'changed'} {changed} episode(s)")
    if unrecoverable:
        print(f"{unrecoverable} episode(s) kept their recorded completion: the device-oracle "
              f"outcome it needs was never saved, so a rescore cannot know it. Their bug side "
              f"was rescored ({unrecoverable_bugs_changed} "
              f"{'would change' if args.dry_run else 'changed'})")
    if stale:
        print(f"{stale} episode(s) were recorded under a different corpus version than the "
              f"current files — the rescored board is not comparable with the recorded one")

    if board:
        rows = journey.summary(board)
        public, heldout = journey.split_heldout(rows)

        def pct(v):
            return "—" if v is None else f"{v * 100:.0f}%"

        def block(block_rows, title, prefix):
            print()
            print(title)
            for i, row in enumerate(block_rows, 1):
                eps = (f"{row['episodes']}/{row['planned_episodes']}" if row["excluded_episodes"]
                       else str(row["episodes"]))
                star = "*" if row.get("mixed_corpus") else ""
                money = journey.cost_cells(row)
                print(f"  {prefix}{i}. {row['agent']} · {row['model']} · {row['condition']}{star}: "
                      f"episodes {eps} · cut {row['truncated']} · "
                      f"integrity @{journey.INTEGRITY_N} {journey.integrity_cell(row)} · "
                      f"completion {pct(row['completion'])}"
                      f"{f' ({row['completion_unscored']} un)' if row['completion_unscored'] else ''} · "
                      f"bugs {row['bugs_found']}/{row['bugs_present']} · false rep. {row['false_reports']} · "
                      f"P {pct(row['precision'])} · R {pct(row['recall'])} · F1 {pct(row['f1'])} · "
                      f"$/episode {money['cost']} · min/episode {money['minutes']}")
            print(f"  {journey.corpus_note(block_rows)}")

        if public or not heldout:
            block(public, "Board:", "")
        if heldout:
            n_apps = max((r.get("heldout_apps") or 0) for r in heldout)
            block(heldout, f"Held-out ({n_apps} app{'s' if n_apps != 1 else ''}) — never blended "
                           f"into the public rows:", "H")
        if any(r.get("mixed_corpus") for r in rows):
            print(f"  {journey.MIXED_CORPUS_NOTE}")
        if note := journey.integrity_note(rows):
            print(f"  {note}")
        print(f"  {journey.RANKING_NOTE}")
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
