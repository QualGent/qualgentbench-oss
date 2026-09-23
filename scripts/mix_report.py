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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from qualgentbench import corpus, journey

# The plan's buckets this corpus is balanced against, in the order the epic reports them:
# (bucket, the classes that fill it, target % of all defects). display/content is the
# plan's layout 9 + widget inventory 8 + content and format 7. The targets sum to 87:
# the plan's other 13% (stale display 6, compatibility 4, dead control 3) has no class in
# the vocabulary, so a corpus drawn only from these classes runs over on every bucket —
# arithmetic, not a gap to chase (docs/defect-classes.md).
BUCKETS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("crash", ("crash",), 24.0),
    ("display/content", ("layout", "widget-inventory", "content-format"), 24.0),
    ("persistence", ("persistence",), 14.0),
    ("navigation", ("navigation",), 11.0),
    ("lifecycle", ("lifecycle",), 7.0),
    ("ordering", ("ordering",), 4.0),
    ("ANR/freeze", ("anr", "stuck"), 3.0),
)
UNCLASSIFIED = "unclassified"


def bucket_of(cls: str | None) -> str:
    for name, classes, _ in BUCKETS:
        if cls in classes:
            return name
    return UNCLASSIFIED


def defect_rows(doc: dict) -> list[dict]:
    """`[{id, class, seeded}]` for every `defects:` entry, in file order. `class` is the
    authored value when it is in the vocabulary and None otherwise (missing, misspelt);
    `seeded` says whether some case's `bugs:` switches the defect on."""
    seeded = {b["id"] for case in doc.get("test_cases") or [] for b in journey.case_bugs(case)}
    rows: list[dict] = []
    for d in doc.get("defects") or []:
        d = d if isinstance(d, dict) else {}
        did = str(d.get("id") or "?")
        raw = d.get("class")
        rows.append({"id": did,
                     "class": raw if raw in journey.DEFECT_CLASSES else None,
                     "seeded": did in seeded})
    return rows


def load(root: Path, app_ids: list[str] | None = None) -> dict[str, list[dict]]:
    """{app id: defect rows} for every `test-cases/<app>.yaml` under `root`."""
    out: dict[str, list[dict]] = {}
    for path in sorted((root / "test-cases").glob("*.yaml")):
        if app_ids and path.stem not in app_ids:
            continue
        out[path.stem] = defect_rows(yaml.safe_load(path.read_text()) or {})
    return out


def _pct(n: int, total: int) -> float:
    return 100.0 * n / total if total else 0.0


def tally(rows: list[dict]) -> dict:
    """The mix of one set of defect rows: per bucket the count, how many of those a case
    seeds, the share of ALL rows (unclassified ones included, so the shares are honest),
    the target and the delta in points."""
    total = len(rows)
    buckets = []
    for name, classes, target in BUCKETS:
        mine = [r for r in rows if r["class"] in classes]
        share = _pct(len(mine), total)
        per_class = {c: sum(r["class"] == c for r in mine) for c in classes}
        buckets.append({
            "bucket": name,
            "classes": {c: k for c, k in per_class.items() if k},
            "n": len(mine),
            "on_a_case": sum(r["seeded"] for r in mine),
            "share": share,
            "target": target,
            "delta": share - target,
        })
    return {
        "defects": total,
        "on_a_case": sum(r["seeded"] for r in rows),
        "buckets": buckets,
        "unclassified": [r["id"] for r in rows if r["class"] is None],
        "unseeded": [r["id"] for r in rows if not r["seeded"]],
        "target_total": sum(t for _, _, t in BUCKETS),
    }


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


def report(root: Path, apps: dict[str, list[dict]]) -> dict:
    """Everything the report prints, as data (`--json`)."""
    return {
        "root": str(root),
        "corpus_version": corpus.version_of(root),
        "corpus": tally([r for rows in apps.values() for r in rows]),
        "apps": {app_id: tally(rows) for app_id, rows in apps.items()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", type=Path, default=corpus.PACKAGED,
                    help="a data root holding test-cases/ (default: the packaged corpus). Point "
                         "it at $QGB_HELDOUT_DIR to report the held-out split on its own — "
                         "never blended into the public numbers")
    ap.add_argument("--app", help="comma-separated app ids (default: every test-case file)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

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
