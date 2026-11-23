#!/usr/bin/env python3
"""The replayer pilot for report quality gate G4 (QUA-3591).

Gate G4 asks whether a second agent can reproduce a bug from the report alone. One
replayer (agent + model) replays every arm's reports, so it has to be chosen once. This
script measures each candidate on the same few defects, using only the hand-written
reference reports:

  ceiling  each reference report replayed 3 times (how often a good report reproduces)
  control  the same report with its steps deleted, once (should NOT reproduce)

and prints one line per candidate: ceiling, control failures, cost and wall time per
replay. The owner picks the replayer from that table and pins it in
`src/qualgentbench/data/quality-replay.json`.

By default NOTHING RUNS: the script prints the defects it would use and the number of
episodes. `--run` starts paid replay episodes on a device and needs `--yes` or a person
at a terminal. `--table` prints the table from the replay episodes already saved.

  # what would run (no device, no agent, no spend)
  uv run python scripts/quality_replay_pilot.py --out ~/.qualgentbench/replay-pilot \\
      --candidate codex-cli:MODEL_A --candidate claude-code:MODEL_B

  # run it (spends)
  uv run python scripts/quality_replay_pilot.py --out ~/.qualgentbench/replay-pilot \\
      --candidate codex-cli:MODEL_A --mcp-server http://127.0.0.1:51821 \\
      --device emulator-5554 --run

  # the table, from what is saved
  uv run python scripts/quality_replay_pilot.py --out ~/.qualgentbench/replay-pilot \\
      --candidate codex-cli:MODEL_A --candidate claude-code:MODEL_B --table
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

from qualgentbench import quality_replay as qr


def parse_candidate(text: str) -> tuple[str, str, str | None]:
    """`agent:model[@provider]`. The model may itself contain `:` or `/`."""
    agent, sep, rest = text.partition(":")
    if not sep or not agent or not rest:
        raise argparse.ArgumentTypeError(f"--candidate wants agent:model[@provider], got {text!r}")
    model, _, provider = rest.partition("@")
    return agent, model, provider or None


def candidate_dir(out: Path, agent: str, model: str, provider: str | None) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{agent}__{model}" + (f"__{provider}" if provider else ""))
    return out / slug


def pilot_config(base: qr.ReplayConfig, agent: str, model: str, provider: str | None,
                 tooling: str) -> qr.ReplayConfig:
    """The packaged replay rules with this candidate as the replayer. Early stop is
    off: the pilot reports the ceiling as a rate, so every reference gets all its
    replays."""
    return replace(base, agent=agent, model=model, provider=provider, tooling=tooling,
                   ceiling_early_stop=False)


def chosen_defects(args) -> list[tuple[str, str, str]]:
    if args.defect:
        out = []
        for d in args.defect:
            parts = d.split("/")
            if len(parts) != 3:
                raise SystemExit(f"--defect wants app/case/defect, got {d!r}")
            out.append(tuple(parts))
        return out
    picked = qr.select_pilot(qr.survey(heldout=False), args.defects)
    return [(f.app_id, f.case_id, f.defect_id) for f in picked]


def state_for(config: qr.ReplayConfig, replay_dir: Path,
              defects: list[tuple[str, str, str]]) -> qr.ReplayState:
    return qr.ReplayState(config, [], qr.collect(replay_dir), extra_defects=defects)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True,
                    help="where the pilot's replay episodes are saved, one folder per "
                         "candidate (outside the repository)")
    ap.add_argument("--candidate", action="append", type=parse_candidate, default=[],
                    metavar="AGENT:MODEL[@PROVIDER]", help="a replayer to measure; repeatable")
    ap.add_argument("--defects", type=int, default=10, help="how many defects (default 10)")
    ap.add_argument("--defect", action="append", default=[], metavar="APP/CASE/DEFECT",
                    help="use exactly these defects instead of the default selection")
    ap.add_argument("--config", type=Path, default=None, help="replay config JSON")
    ap.add_argument("--mcp-server", default=None,
                    help="MCP server giving the replayer device tools; omit for bare adb")
    ap.add_argument("--device", default=None, help="adb serial (default: first available)")
    ap.add_argument("--max-episodes", type=int, default=None,
                    help="start at most this many episodes per candidate")
    ap.add_argument("--table", action="store_true", help="print the table from saved episodes")
    ap.add_argument("--json", action="store_true", help="with --table: print the rows as JSON")
    ap.add_argument("--run", action="store_true", help="run the replays (paid)")
    ap.add_argument("--yes", action="store_true", help="with --run: do not ask Continue?")
    args = ap.parse_args(argv)
    if not args.candidate:
        ap.error("name at least one --candidate")

    base = qr.load_config(args.config)
    tooling = "mcp" if args.mcp_server else ("raw" if args.run else base.tooling)
    defects = chosen_defects(args)
    cells = []
    for agent, model, provider in args.candidate:
        config = pilot_config(base, agent, model, provider, tooling)
        rdir = candidate_dir(args.out, agent, model, provider)
        label = f"{agent} / {model}" + (f" ({provider})" if provider else "")
        cells.append((label, config, rdir, state_for(config, rdir, defects)))

    if args.table:
        rows = [qr.pilot_row(label, state) for label, _, _, state in cells]
        print(json.dumps(rows, indent=2) if args.json else qr.pilot_table(rows))
        if not args.json:
            for label, _, _, state in cells:
                for e in state.excluded():
                    print(f"{label}: excluded {e['defect']}  [{e['excluded']}] {e['reason']}")
        return 0

    print(f"pilot defects ({len(defects)}):")
    first = cells[0][3]
    for app_id, case_id, defect_id in defects:
        f = first.facts[qr.defect_key(app_id, case_id, defect_id)]
        budget = qr.step_budget(f.ref_steps, f.case_budget, first.config)
        print(f"  {f.key}  kind={f.truth_kind}  verdict={f.basis}  budget={budget} steps")
    total = 0
    for label, _, rdir, state in cells:
        worst = state.worst_case()
        total += worst["total"]
        print(f"{label}: at most {worst['total']} episodes still to run "
              f"(ceiling {worst['ceiling']}, control {worst['control']}) -> {rdir}")
    print(f"total: at most {total} replay episodes")
    if not args.run:
        print("Nothing was run. Add --run to start the replays (paid).")
        return 0
    return asyncio.run(_run(args, cells))


async def _run(args, cells) -> int:
    from qualgentbench import cli
    from qualgentbench.scheduler import new_run_id
    from qualgentbench.session import DeviceSession

    for label, config, rdir, _ in cells:
        cli._gate_runs_dir(rdir, False)
        cli._gate_agent_auth(str(config.agent), False, config.model)
    cli._confirm_start(args.yes)
    for label, config, rdir, state in cells:
        cli._apply_provider(config.provider)
        session = DeviceSession(args.mcp_server or "")
        identity = await cli._preflight(session, args.mcp_server or "", str(config.agent),
                                        args.device, [str(config.model)])
        serial = args.device or await session.first_available_device()
        if not serial:
            print("error: no device found", file=sys.stderr)
            return 1
        rdir.mkdir(parents=True, exist_ok=True)
        execute = qr.DeviceExecutor(config, rdir, mcp_server=args.mcp_server, device=serial,
                                    run_id=new_run_id(), mcp_identity=identity)

        def line(unit, attempt, label=label) -> None:
            answer = "not saved" if attempt is None else (
                attempt.invalid or f"reproduced={attempt.reproduced} ({attempt.reason})")
            print(f"{label}: {unit.key}: {answer}", flush=True)

        await qr.run(state, execute, max_episodes=args.max_episodes, on_attempt=line)
        qr._write_json(rdir / qr.BASELINE_FILE, qr.baseline_document(state))
    print(qr.pilot_table([qr.pilot_row(label, state) for label, _, _, state in cells]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
