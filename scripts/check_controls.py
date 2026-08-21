#!/usr/bin/env python3
"""Does any CONTROL disagree with the device? A wrong `state: ok` charges a
false-report penalty to an agent that observed reality; repeated dissent across
episodes means the key is bad, not the agent."""

from __future__ import annotations

import argparse
import glob
import json
import re
from collections import defaultdict
from pathlib import Path

from qualgentbench import bugs

ROOT = Path(__file__).resolve().parents[1]
# ACTUAL must stop at the NEXT report line — an unbounded [^|]* swallowed the
# following AREA: and made finditer skip whole claims.
AREA_RE = re.compile(
    r"AREA:\s*(?P<area>[A-Za-z0-9_-]+)\s*\|\s*VERDICT:\s*(?P<verdict>[A-Za-z_]+)"
    r"(?:\s*\|\s*EXPECTED:\s*(?P<expected>.*?))?"
    r"(?:\s*\|\s*ACTUAL:\s*(?P<actual>.*?))?"
    r"(?=\s*(?:\\+n|\n|\||$).{0,4}?AREA:|\\+n|\n|$)",
    re.I | re.S,
)


def _episodes(runs_dir: Path):
    for f in sorted(glob.glob(str(runs_dir / "explore-*" / "*" / "result.json"))):
        d = Path(f).parent
        tr = d / "agent" / "transcript.txt"
        if not tr.exists():
            continue
        try:
            res = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001
            continue
        yield d, res, tr.read_text(errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default=None, help="limit to one tier")
    ap.add_argument("--runs", default=None, action="append",
                    help="run dir to scan (repeatable; default: runs)")
    ap.add_argument("--min-dissent", type=int, default=1,
                    help="episodes that must call a control broken before it FAILs")
    a = ap.parse_args()

    suites = {s["app"]["id"]: s for s in bugs.load_apps()
              if not a.tier or s["app"].get("difficulty") == a.tier}
    controls = {aid: {f["id"] for f in ((s.get("exploration") or {}).get("features") or [])
                      if f.get("state") == "ok"}
                for aid, s in suites.items()}

    # (app, area) -> [(episode, actual-text)]
    dissent: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    tested: dict[tuple[str, str], int] = defaultdict(int)

    scanned = 0
    for runs in {r for r in (a.runs or ["runs"])}:
        for d, res, txt in _episodes(ROOT / runs):
            app_id = str(res.get("task_id") or d.parent.name).replace("explore-", "")
            if app_id not in controls:
                continue
            scanned += 1
            # ONE vote per area per EPISODE — agents restate their running report,
            # and counting matches would read repetition as corroboration.
            seen: dict[str, tuple[str, str]] = {}
            for m in AREA_RE.finditer(txt):
                area = m.group("area")
                if area not in controls[app_id]:
                    continue
                verdict = m.group("verdict").lower()
                if verdict not in ("as_specified", "deviates", "blocked"):
                    continue
                actual = (m.group("actual") or "").strip()
                # Last verdict wins, matching the scorer: an agent may revise an area.
                prev = seen.get(area)
                if prev and prev[0] == verdict and prev[1] and not actual:
                    continue
                seen[area] = (verdict, actual)
            for area, (verdict, actual) in seen.items():
                tested[(app_id, area)] += 1
                if verdict == "deviates":
                    dissent[(app_id, area)].append((d.name, actual[:150]))

    print(f"=== control health — {scanned} episode(s) scanned"
          f"{f', tier {a.tier}' if a.tier else ''} ===")
    if not scanned:
        print("no episodes to check — run the benchmark first, then re-run this gate.")
        return 0

    bad = {k: v for k, v in dissent.items() if len(v) >= a.min_dissent}
    if not bad:
        n = sum(len(v) for v in controls.values())
        print(f"  ok   no control contradicted by device evidence ({n} controls)")
        return 0

    for (app_id, area), hits in sorted(bad.items()):
        print(f"\n FAIL {app_id}.{area} — declared `state: ok`, but "
              f"{len(hits)}/{tested[(app_id, area)]} episode(s) measured it broken:")
        for ep, actual in hits[:3]:
            print(f"        {ep[:46]}")
            if actual:
                print(f"          observed: {actual}")
    print(f"\n{len(bad)} control(s) contradicted. Each one charges a false-report")
    print("penalty to an agent that was RIGHT. Either the area is genuinely broken on")
    print("the seeded build — mark it `state: collateral`, which scores neither as a")
    print("defect nor as a control — or the seeded patch must be moved off the code")
    print("path the control depends on.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
