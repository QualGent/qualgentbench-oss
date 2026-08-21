#!/usr/bin/env python3
"""Re-score episodes from their REPLAYED reproductions: reads `replay.json` and
reports recall/precision/trust in the `replay-v1` unit beside the key-based numbers.
No model, no device; a run without replay.json is SKIPPED, never scored as zero."""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench.bugs import load_suite          # noqa: E402
from qualgentbench.replay_score import score       # noqa: E402

_TRUTH = Path(__file__).parents[1] / "src" / "qualgentbench" / "data" / "truth"


def _derived(app: str) -> dict:
    """area -> label MEASURED by derive_truth, so a claim whose repro fails can be told
    apart from a claim that is simply wrong. Without this every weak reproduction is
    charged as a false report."""
    out: dict = {}
    for f in ("easy-stability.json", "medium-stability.json"):
        p = _TRUTH / f
        if not p.exists():
            continue
        for a, rows in json.loads(p.read_text()).items():
            if a == app:
                out.update({r["area"]: r["derived"] for r in rows})
    return out

_SPECS = Path(__file__).parents[1] / "src" / "qualgentbench" / "data" / "benchmarks"


def _score_run(run_dir: Path) -> dict | None:
    result = run_dir / "result.json"
    replay = run_dir / "replay.json"
    if not result.exists() or not replay.exists():
        return None
    d = json.loads(result.read_text())
    m = d.get("metrics") or {}
    app = m.get("app_id")
    if not app or not (_SPECS / f"{app}.yaml").exists():
        return None
    features = load_suite(_SPECS / f"{app}.yaml")["exploration"]["features"]
    rj = json.loads(replay.read_text())
    # Verdicts come from the REPLAY results (`claimed` per replayed claim); reading
    # result.json's `repro_claims` instead froze the scoring-time parser, so the
    # UNREPLAYABLE fallback never fired and unreplayable claims became misses.
    verdicts = {r.get("area"): r.get("claimed") for r in (rj.get("results") or [])}
    for c in (m.get("repro_claims") or []):
        verdicts.setdefault(c.get("area"), c.get("verdict"))
    s = score(features, rj.get("results") or [], verdicts, _derived(app))
    return {"run": str(run_dir), "app_id": app,
            "condition": d.get("condition") or m.get("condition"),
            "steps": m.get("steps"),
            "key_recall": m.get("recall"), "key_precision": m.get("precision"),
            **s.as_dict()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--all", action="store_true", help="every run with a replay.json")
    ap.add_argument("--json")
    args = ap.parse_args()

    dirs = [Path(r) for r in args.runs]
    if args.all:
        dirs += [Path(f).parent for f in
                 glob.glob(str(Path(__file__).parents[1] / "runs" / "*" / "*" / "replay.json"))]
    if not dirs:
        ap.error("name a run directory, or pass --all")

    rows = [r for r in (_score_run(d) for d in dict.fromkeys(dirs)) if r]
    if args.all:
        # One row per (app, condition): the newest. Older episodes of the same cell are
        # earlier trials of the same thing and would double-count in the totals.
        newest: dict = {}
        for r in rows:
            k = (r["app_id"], r["condition"])
            if k not in newest or r["run"] > newest[k]["run"]:
                newest[k] = r
        rows = list(newest.values())
    if not rows:
        print("no scored runs — is replay.json present? run scripts/replay_findings.py first")
        return 1

    print(f"{'app':20}{'cond':9}{'recall':>8}{'prec':>7}{'trust':>7}"
          f"{'conf':>6}{'miss':>6}{'FP':>4}{'weak':>6}{'undet':>7}{'unrep':>7}   (key: rec/prec)")
    for r in sorted(rows, key=lambda x: (x["app_id"], x["condition"] or "")):
        print(f"{r['app_id']:20}{(r['condition'] or ''):9}"
              f"{r['recall']:>8.2f}{r['precision']:>7.2f}{r['trust']:>7.2f}"
              f"{r['confirmed']:>6}{r['missed']:>6}"
              f"{r['false_positives']:>4}{r['weak_repro']:>6}{r['undetermined']:>7}"
              f"{r['unreplayable']:>7}"
              f"   ({r['key_recall']}/{r['key_precision']})")

    for cond in ("raw", "mcp"):
        sub = [r for r in rows if r["condition"] == cond]
        if not sub:
            continue
        conf = sum(r["confirmed"] for r in sub)
        seeded = sum(r["seeded_total"] for r in sub)
        fp = sum(r["false_positives"] for r in sub)
        fb = sum(r["by_fallback"] for r in sub)
        trust = sum(r["trust"] for r in sub) / len(sub)
        print(f"\n{cond:8} n={len(sub)}  recall {conf}/{seeded} = {conf / max(seeded, 1):.0%}"
              f"  ·  FP {fp}  ·  mean trust {trust:.0%}"
              f"  ·  {fb} of {conf} confirmed via key fallback")

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
