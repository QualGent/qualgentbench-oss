#!/usr/bin/env python3
"""Journey defect-class mix: the corpus's defects per class bucket, against the plan.

Reads `test-cases/*.yaml` under a data root — the packaged corpus by default — and
prints, for the whole corpus and for each app, one row per bucket the plan targets: the
count, its share of the defects, the plan's target, and the delta in points.

A pure reader of the YAML: no device, no network, no truth file, no scorer. It counts
DECLARED defects (every `defects:` entry — the unit the class mix is stated in); the
`on a case` column counts the ones some case's `bugs:` actually switches on. A declared
defect that no case seeds is inert in journey mode — no board ever measures it — so the
footer names every one.

The buckets, their targets and where they come from are in docs/defect-classes.md; the
vocabulary is `journey.DEFECT_CLASSES`, the same one `lint_journey_cases.py` enforces. A
defect with no class, or one outside the vocabulary, is counted as `unclassified` and
makes the report exit 1: a mix with a hole in it is not a mix.

    uv run python scripts/mix_report.py                             # the public corpus
    uv run python scripts/mix_report.py --app medtimer,orgzly       # a subset
    uv run python scripts/mix_report.py --root "$QGB_HELDOUT_DIR"   # the held-out split, alone
    uv run python scripts/mix_report.py --json
    uv run python scripts/mix_report.py --quotas ~/plans/public-quotas.yaml   # per-app deltas
    uv run python scripts/mix_report.py --root "$QGB_HELDOUT_DIR" --quotas ~/private/heldout-quotas.yaml

`--quotas FILE` (QUA-3451) adds each app's cases and declared defects per bucket against
a quota file (`qualgentbench.quotas` has the format) and the totals. The file is always
an explicit path: with `--root` at the held-out split it must live OUTSIDE the repository
(held-out quotas name held-out apps), and against the public corpus a file naming a
held-out app is refused. A quota miss is reported, never fatal: the corpus is grown
toward its quotas one app at a time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from qualgentbench import corpus, journey, quotas

# The arithmetic lives in the package so `qualgent-bench corpus-report` reuses it; the
# names stay importable from here for the tests and for anyone loading this script.
from qualgentbench.mix import (  # noqa: F401
    BUCKETS,
    UNCLASSIFIED,
    _pct,
    bucket_of,
    defect_rows,
    load,
    report,
    tally,
)


def render(label: str, t: dict) -> list[str]:
    lines = [f"{label} — {t['defects']} defect(s), {t['on_a_case']} on a case",
             f"  {'bucket':16s} {'n':>3s} {'on a case':>9s} {'share':>7s} {'target':>7s} "
             f"{'delta':>7s}  classes"]
    for b in t["buckets"]:
        classes = ", ".join(f"{c} {k}" for c, k in b["classes"].items()) or "-"
        lines.append(f"  {b['bucket']:16s} {b['n']:3d} {b['on_a_case']:9d} {b['share']:6.1f}% "
                     f"{b['target']:6.1f}% {b['delta']:+7.1f}  {classes}")
    if t["unclassified"]:
        n = len(t["unclassified"])
        lines.append(f"  {UNCLASSIFIED:16s} {n:3d} {'':9s} {_pct(n, t['defects']):6.1f}%"
                     f"       -        -  {', '.join(t['unclassified'])}")
    lines.append(f"  {'total':16s} {t['defects']:3d} {t['on_a_case']:9d} "
                 f"{100.0 if t['defects'] else 0.0:6.1f}% {t['target_total']:6.1f}%")
    return lines


def _is_heldout_root(root: Path) -> bool:
    """`--root` is the held-out split: the configured or default held-out directory, or
    any directory outside the packaged corpus that holds no packaged app (a synced copy
    elsewhere). Errs toward True, which only tightens where the quota file may live."""
    here = root.expanduser().resolve()
    if here == corpus.PACKAGED.resolve():
        return False
    for d in (corpus.heldout_dir(), corpus.default_heldout_dir()):
        if d is not None and d.expanduser().resolve() == here:
            return True
    return not (set(corpus.apps_in(root)) & set(corpus.public_apps()))


def case_counts(root: Path, app_ids: list[str]) -> dict[str, int]:
    """{app id: number of test cases} for the given apps under `root`."""
    out: dict[str, int] = {}
    for app_id in app_ids:
        doc = yaml.safe_load((root / "test-cases" / f"{app_id}.yaml").read_text()) or {}
        out[app_id] = sum(1 for c in doc.get("test_cases") or [] if isinstance(c, dict))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=corpus.PACKAGED,
                    help="a data root holding test-cases/ (default: the packaged corpus). Point "
                         "it at $QGB_HELDOUT_DIR to report the held-out split on its own — "
                         "never blended into the public numbers")
    ap.add_argument("--app", help="comma-separated app ids (default: every test-case file)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--quotas", type=Path, default=None,
                    help="a quota file (YAML/JSON): per-app and total deltas against it. "
                         "Outside the repository when --root is the held-out split")
    args = ap.parse_args(argv)

    quota_doc = None
    if args.quotas is not None:
        try:
            quota_doc = quotas.load(args.quotas, heldout=_is_heldout_root(args.root))
        except quotas.QuotaError as exc:
            print(f"FAIL: {exc}")
            return 1

    wanted = [a.strip() for a in args.app.split(",") if a.strip()] if args.app else None
    apps = load(args.root, wanted)
    if not apps:
        print(f"FAIL: no test-case files under {args.root / 'test-cases'}")
        return 1
    missing = sorted(set(wanted or []) - set(apps))
    if missing:
        print(f"FAIL: no test-case file for {', '.join(missing)} under {args.root / 'test-cases'} "
              f"(have: {', '.join(sorted(load(args.root)))})")
        return 1

    rep = report(args.root, apps)
    if quota_doc is not None:
        rep["quotas"] = quotas.report(quota_doc, case_counts(args.root, list(apps)), apps)
    unclassified = {a: t["unclassified"] for a, t in rep["apps"].items() if t["unclassified"]}
    if args.json:
        print(json.dumps(rep, indent=2))
        return 1 if unclassified else 0

    where = "packaged corpus" if args.root == corpus.PACKAGED else str(args.root)
    scope = "every app" if wanted is None else "selected apps"
    print(f"Journey defect-class mix — {where}, corpus_version {rep['corpus_version']}\n")
    print("\n".join(render(f"CORPUS ({scope}: {len(apps)})", rep["corpus"])))
    for app_id, t in rep["apps"].items():
        print()
        print("\n".join(render(app_id, t)))

    unseeded = {a: t["unseeded"] for a, t in rep["apps"].items() if t["unseeded"]}
    if unseeded:
        print(f"\nDeclared but on no case — journey mode never switches these on, so no board "
              f"measures them ({sum(map(len, unseeded.values()))}):")
        for app_id, ids in unseeded.items():
            print(f"  {app_id:18s} {', '.join(ids)}")
    if quota_doc is not None:
        print("\nQuotas — cases and declared defects per bucket against the quota file:")
        print("\n".join(quotas.render(rep["quotas"])))
    print(f"\nTargets sum to {rep['corpus']['target_total']:.0f}%: the plan's stale display (6), "
          f"compatibility (4) and dead control (3) buckets have no class in the vocabulary.")
    if unclassified:
        print(f"\nFAIL: {sum(map(len, unclassified.values()))} defect(s) have no class from "
              f"the vocabulary ({', '.join(journey.DEFECT_CLASSES)}):")
        for app_id, ids in unclassified.items():
            print(f"  {app_id:18s} {', '.join(ids)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
