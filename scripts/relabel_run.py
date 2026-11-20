#!/usr/bin/env python3
"""Re-label a rescored run under the current corpus (QUA-3553) — no agent, no device.

After a SCORING-ONLY change (a symptom wording, a known clean-build behaviour, a measured
text; the same briefs, routes, oracles and APKs), copy a run under a new run id into a
separate runs directory, rescore every episode against the current corpus and re-stamp
it with the current corpus and held-out versions, so its board files under the new set
key. Refused, with every reason, when the run measured anything the current corpus does
not (`qualgentbench.relabel`). The original run is never modified.

    QGB_HELDOUT_DIR=<current split> uv run python scripts/relabel_run.py \\
        --runs-dir <runs> --run <run id> --as <new run id> --out-runs-dir <new runs dir> \\
        --old-corpus <data root at the recorded corpus> --old-heldout <split at the recorded version>

`--old-corpus` / `--old-heldout` are data roots (`test-cases/`, `truth/`) at the versions
the run RECORDED; they must hash to them. For the public corpus, export it from the oss
commit the run used: `git archive <sha> src/qualgentbench/data | tar -x -C <dir>` and pass
`<dir>/src/qualgentbench/data`. `--check` only reports whether the run may be re-labelled.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import relabel  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs-dir", type=Path, required=True)
    ap.add_argument("--run", required=True, help="the run id to re-label")
    ap.add_argument("--as", dest="new_run_id", help="the new run id")
    ap.add_argument("--out-runs-dir", type=Path, help="where the re-labelled copy is written")
    ap.add_argument("--old-corpus", type=Path, required=True)
    ap.add_argument("--old-heldout", type=Path)
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    a = ap.parse_args(argv)
    try:
        if a.check:
            results = [r for _, r in relabel._run_results(a.runs_dir, a.run)]
            stamps, problems = relabel.check_relabel(results, a.old_corpus, a.old_heldout)
            if problems:
                raise relabel.RelabelRefused(problems)
            print(json.dumps({"run": a.run, "episodes": len(results), **stamps}, indent=2))
            return 0
        if not a.new_run_id or not a.out_runs_dir:
            ap.error("--as and --out-runs-dir are required unless --check")
        rep = relabel.relabel_run(a.runs_dir, a.run, new_run_id=a.new_run_id,
                                  out_dir=a.out_runs_dir, old_public=a.old_corpus,
                                  old_heldout=a.old_heldout)
    except relabel.RelabelRefused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"re-labelled {rep.episodes} episode(s) of {rep.run_id} as {rep.new_run_id} in "
          f"{rep.out_dir}: corpus {rep.recorded['corpus_version']} -> "
          f"{rep.now['corpus_version']}, held-out {rep.recorded['heldout_version']} -> "
          f"{rep.now['heldout_version']} ({rep.statuses})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
