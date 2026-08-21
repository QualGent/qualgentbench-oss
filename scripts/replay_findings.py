#!/usr/bin/env python3
"""Replay an episode's reproductions on a device and classify each claim: run each
twice (seeded defects live, then off) and confirm from the difference — no answer key
involved. Runs from saved artifacts, costs no tokens, writes `replay.json` in the run dir."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from qualgentbench import replay as rp                       # noqa: E402
from qualgentbench.bugs import load_suite                    # noqa: E402
from qualgentbench.submission import Claim, Expectation, Step  # noqa: E402


def _claims(result: dict, run_dir: "Path | None" = None) -> list[Claim]:
    """Rebuild claims for replay, preferring a RE-PARSE of findings.yaml on disk:
    `metrics.repro_claims` froze the scoring-time parser's losses, and re-parsing makes
    parser fixes retroactive. Falls back to recorded claims when the file yields nothing."""
    if run_dir is not None:
        fy = Path(run_dir) / "workspace" / "findings.yaml"
        if fy.exists():
            from qualgentbench import submission
            sub = submission.parse(fy.read_text())
            fresh = [c for c in sub.claims if c.replayable]
            recorded = len(result.get("metrics", {}).get("repro_claims") or [])
            if len(fresh) > recorded:
                print(f"  re-parsed findings.yaml: {len(fresh)} replayable claim(s) "
                      f"(result.json recorded {recorded})")
                return fresh
    out = []
    for c in result.get("metrics", {}).get("repro_claims") or []:
        exp = c.get("expect") or {}
        out.append(Claim(
            area=c["area"], verdict=c.get("claimed") or c.get("verdict", ""),
            steps=[Step(s["action"], s.get("value", "")) for s in c.get("steps") or []],
            expect=Expectation(exp["mode"], exp["text"]) if exp else None,
        ))
    return out


def _restore_shared(app_id: str) -> bool:
    spec_path = (Path(__file__).parents[1] / "src" / "qualgentbench" / "data"
                 / "benchmarks" / f"{app_id}.yaml")
    return bool(load_suite(spec_path).get("restore_shared", True))


def _seeded_bug_ids(app_id: str) -> tuple[list[str], str, list[str], dict | None]:
    spec_path = (Path(__file__).parents[1] / "src" / "qualgentbench" / "data"
                 / "benchmarks" / f"{app_id}.yaml")
    suite = load_suite(spec_path)
    ids = [str(f.get("bug_id")) for f in suite["exploration"]["features"]
           if str(f.get("state")) == "broken" and f.get("bug_id")]
    return (ids, suite["app"]["package"], list(suite.get("shared_storage") or []),
            suite.get("device_setup"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--device", default="emulator-5554")
    ap.add_argument("--json", help="output path (default <run_dir>/replay.json)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    result = json.loads((run_dir / "result.json").read_text())
    app_id = result["metrics"]["app_id"]
    claims = _claims(result, run_dir)
    if not claims:
        print(f"{app_id}: no reproductions recorded — nothing to replay.\n"
              "Episodes run before the repro schema existed cannot be verified "
              "retroactively; that is why capture landed before the full run.")
        return 0

    seeded, bundle, shared, device_setup = _seeded_bug_ids(app_id)
    print(f"{app_id} ({bundle}) · {len(claims)} reproduction(s) · "
          f"{len(seeded)} seeded defect(s) · device {args.device}\n")

    def progress(i, n, res):
        mark = {rp.CONFIRMED: "CONFIRMED defect", rp.CONFIRMED_WORKING: "confirmed working",
                rp.MISSED_DEFECT: "MISSED a real defect", rp.NOT_A_DEFECT: "not a defect",
                rp.DOES_NOT_REPRODUCE: "does not reproduce",
                rp.UNREPLAYABLE: "unreplayable"}[res.classification]
        detail = (res.seeded_on.detail if res.seeded_on else "") or ""
        print(f"  [{i}/{n}] {res.area:22} claimed {res.verdict:13} → {mark}"
              f"{('  (' + detail[:60] + ')') if detail else ''}")

    # The app data as the agent saw it. Without it the app regenerates DIFFERENT
    # randomised sample content and repros referencing existing data cannot replay.
    snap = run_dir / "app_snapshot.tar"
    if not snap.exists():
        print("  note: no app_snapshot.tar — this episode predates snapshot capture, "
              "so replays start from regenerated sample data and repros that reference "
              "existing content will read as unreplayable.\n")
        snap = None

    # /sdcard survives `pm clear`, so apps that keep user content there need shared
    # storage restored between passes too (only specs declaring `shared_storage:`).
    # Static staged content is never re-extracted per pass — same opt-out as the deriver.
    if not _restore_shared(app_id):
        shared = []
    shared_snap = run_dir / "shared_snapshot.tar"
    if shared and not shared_snap.exists():
        print(f"  note: {app_id} keeps content in shared storage but this episode has "
              "no shared_snapshot.tar — replays will not be deterministic.\n")
        shared_snap = None
    elif not shared:
        shared_snap = None

    results = await rp.replay_episode(args.device, bundle, claims, seeded, progress,
                                      snap, shared, shared_snap,
                                      device_setup=device_setup)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # Replay provenance: `u2_available`/`dump_stats` make a degraded environment
    # visible in the artifact; `snapshot_mode` says whether the tar was taken cold
    # (app stopped) or predates that ("warm-legacy").
    from qualgentbench.verify.device import dump_stats, u2_available  # noqa: E402
    try:
        snapshot_mode = json.loads(
            (run_dir / "snapshot_meta.json").read_text()).get("mode", "warm-legacy")
    except (OSError, ValueError):
        snapshot_mode = "warm-legacy"

    out = Path(args.json) if args.json else run_dir / "replay.json"
    out.write_text(json.dumps({"app_id": app_id, "device": args.device,
                               "seeded_bugs": seeded,
                               # Which replayer produced this. cli._replay_and_board
                               # skips episodes already stamped with the current one.
                               "replayer": rp.replayer_fingerprint(),
                               "u2_available": u2_available(),
                               "dump_stats": dump_stats(args.device),
                               "snapshot_mode": snapshot_mode,
                               "results": [r.as_dict() for r in results]}, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
