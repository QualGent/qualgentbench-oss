"""The cross-machine hand-off, end to end (QUA-2699).

Every other checkpoint test exercises one piece against a fixture built for it. This
file exercises the promise the feature was filed for, in one pass, over two runs dirs
that never share a path:

    machine A stops mid-sweep  →  checkpoint export  →  (the file is sent)
                               →  machine B: checkpoint import  →  run --resume
                               →  machine B: show --run  = one board, both machines

What it pins, in the order the flow hits them:

* the episode machine A was killed in the middle of is **discarded, never scored** —
  it does not travel, it does not land on B, and its unit comes back as work;
* the bundle carries the scoring files and nothing else: no agent config home, no
  transcript, no evidence, no `.env`, and no byte matching a credential marker;
* the run id is the same on both machines, so the two sittings are one run;
* machine B schedules **exactly** the units A did not finish — not the finished ones,
  not the whole plan;
* `show --run` on B renders one blended board over both machines' episodes and flags
  the imported rows as ones it cannot re-verify.

Everything here is synthetic: a stubbed corpus, a fake lane engine, no device and no
agent. What that leaves unproven is recorded in `docs/checkpointing.md` under
"What a live hand-off still has to prove" — the parts that need real credits and a
real device are the owner's to run, not this suite's.
"""

from __future__ import annotations

import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qualgentbench import checkpoint
from qualgentbench.cli import _write_plan
from qualgentbench.result import RunResult, VerifierResult

RUN_ID = "20260909-100000-hand"

# The corpus both machines resolve against. Identical on each, which is what makes the
# environment fingerprint in plan.json match and the resume proceed without --force.
SUITE = {
    "app": {"id": "birday", "name": "Birday", "package": "com.birday",
            "platform": "android", "difficulty": "easy"},
    "apk": {"repo": "qualgent/qualgentbench-apps", "filename": "easy/birday.apk",
            "sha256": "a" * 64},
    "exploration": {"id": "explore-birday", "features": [{"id": "a", "state": "broken"}]},
    "tasks": [{"id": f"birday-t{i}", "bug_id": f"b{i}", "type": "bug"} for i in range(1, 7)],
}

TASKS = [f"birday-t{i}" for i in range(1, 7)]
FINISHED_ON_A = TASKS[:3]        # scored, and the only episodes that travel
INTERRUPTED_ON_A = TASKS[3]      # killed mid-episode: marker, no result.json
NEVER_STARTED_ON_A = TASKS[4:]   # the rest of the plan
OWED_TO_B = [INTERRUPTED_ON_A, *NEVER_STARTED_ON_A]


# ── machine A: a sweep the credit guard stopped ───────────────────────────────


def _episode_dir(runs_dir: Path, task_id: str, *, prefix: str = "A") -> Path:
    return runs_dir / task_id / f"{prefix}_2026-09-09T00-00-00Z_{task_id}_claude-code_raw_trial-1"


def _write_result(episode: Path, runs_dir: Path, task_id: str, *,
                  segment: int = 0, device: str = "emulator-5554") -> RunResult:
    t0 = datetime(2026, 9, 9, tzinfo=timezone.utc)
    result = RunResult.build(
        task_id=task_id, task_version="v1", task_type="bug_task", agent="claude-code",
        model="anthropic/claude-opus-4-8", condition="raw", trial=1, started_at=t0,
        ended_at=t0 + timedelta(minutes=4), exit_code=0,
        verifier=VerifierResult(passed=True, score=1.0,
                                metrics={"total_tokens": 12_000, "hook_steps": 40}),
        artifact_dir=episode, runs_dir=runs_dir, run_id=RUN_ID,
        provenance={"device": device, "lane": 0, "segment": segment},
    )
    result.write(episode / "result.json")
    return result


def _finished_episode(runs_dir: Path, task_id: str) -> Path:
    """An episode that ran to the end on machine A — scoring files AND the heavy,
    credential-bearing dirs a real run leaves beside them."""
    episode = _episode_dir(runs_dir, task_id)
    (episode / "verifier").mkdir(parents=True, exist_ok=True)
    (episode / "workspace").mkdir(parents=True, exist_ok=True)
    checkpoint.write_episode_marker(episode, run_id=RUN_ID, app_id="birday",
                                    task_id=task_id, kind="bug_task", trial=1)
    _write_result(episode, runs_dir, task_id)
    (episode / "replay.json").write_text(
        json.dumps({"replayed": True, "harness_sha": "deadbeef", "claims": []}))
    (episode / "verifier" / "ctrf.json").write_text('{"results": {"tests": []}}')
    (episode / "workspace" / "findings.yaml").write_text(
        f"findings:\n  - area: reminders\n    task: {task_id}\n    verdict: broken\n")
    (episode / "instruction_sent.md").write_text(f"# Task\nCheck {task_id} on Birday.\n")
    (episode / "interactions.json").write_text('{"interactions": 41}')
    (episode / "adb_counts.json").write_text('{"shell": 12}')

    # What a real episode dir also holds, and what the hand-off must leave behind.
    (episode / "claude_home").mkdir(exist_ok=True)
    (episode / "claude_home" / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-NOTREAL",'
        ' "refreshToken": "sk-ant-ort01-NOTREAL"}}')
    (episode / "agent").mkdir(exist_ok=True)
    (episode / "agent" / "transcript.txt").write_text(
        '{"type":"rate_limit_event"}\nAuthorization: Bearer notreal.token\n')
    (episode / "evidence" / "frames").mkdir(parents=True, exist_ok=True)
    (episode / "evidence" / "frames" / "0001.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (episode / "app_snapshot.tar").write_bytes(b"\x00" * 4096)
    (episode / "mcp_config.json").write_text(
        '{"mcpServers": {"devloop": {"headers": {"Authorization": "Bearer notreal"}}}}')
    (episode / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NOTREAL\n")
    return episode


def _interrupted_episode(runs_dir: Path, task_id: str) -> Path:
    """The episode the provider rejection killed: marked, no result.json. Its
    transcript is the only record of why the sweep stopped, so it is quarantined
    rather than deleted."""
    episode = _episode_dir(runs_dir, task_id)
    (episode / "agent").mkdir(parents=True, exist_ok=True)
    checkpoint.write_episode_marker(episode, run_id=RUN_ID, app_id="birday",
                                    task_id=task_id, kind="bug_task", trial=1)
    (episode / "agent" / "transcript.txt").write_text(
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
        '"rateLimitType":"seven_day"}}\n')
    (episode / "rate_limit.json").write_text(
        '{"schema_version": 1, "rejected": {"rate_limit_type": "seven_day"}}')
    (episode / "rate_limited").write_text("seven_day\n")
    return episode


@pytest.fixture
def machine_a(tmp_path: Path) -> Path:
    """A six-unit run that stopped after three: three scored, one killed mid-episode,
    two never started. Shaped like the sweep the credit guard stops — including the
    run-level provider state and stop.json neither of which may travel."""
    runs = tmp_path / "machine-a" / "runs"
    from qualgentbench.scheduler import Unit, plan_summary

    units = [Unit("birday", "Birday", t, "bug_task", t, 1, 180.0, "default") for t in TASKS]
    _write_plan(runs, RUN_ID, plan_summary(units, 1), apps=[SUITE], mode="guided",
                agent="claude-code", model="anthropic/claude-opus-4-8",
                devices=["emulator-5554"], trials=1)

    meta = checkpoint.run_meta_dir(runs, RUN_ID)
    meta.joinpath("schedule.jsonl").write_text(
        '{"ts": "2026-09-09T00:00:00", "event": "start", "lane": 0}\n')
    meta.joinpath("board.json").write_text(
        json.dumps({"run_id": RUN_ID, "episodes": []}))
    # Account state and the reason this sitting ended: both about machine A only.
    meta.joinpath("rate_limit.json").write_text(json.dumps(
        {"schema_version": 1, "windows": {"seven_day": {"utilization": 0.94}}}))
    meta.joinpath("stop.json").write_text(json.dumps(
        {"schema_version": 1, "run_id": RUN_ID, "reason": "seven_day_threshold",
         "utilization": 0.94, "threshold_pct": 90, "done": 3, "remaining": 3,
         "resume": f"qualgent-bench run --resume {RUN_ID}"}))

    for task in FINISHED_ON_A:
        _finished_episode(runs, task)
    _interrupted_episode(runs, INTERRUPTED_ON_A)
    return runs


# ── machine B: the same corpus, a different runs dir, its own credentials ─────


class _FakeLanes:
    """Stands in for `run_lanes` on machine B: records the units it was handed and
    writes the episode each would have produced — including the `evidence/` a locally
    run episode has and an imported one does not."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir, self.seen, self.cfg = runs_dir, [], None

    async def __call__(self, plan, cfg):
        self.cfg = cfg
        self.seen = sorted((u.app_id, u.task_id, u.trial) for u in plan.units)
        for unit in plan.units:
            episode = _episode_dir(self.runs_dir, unit.task_id, prefix=f"B_seg{cfg.segment}")
            episode.mkdir(parents=True, exist_ok=True)
            checkpoint.write_episode_marker(
                episode, run_id=cfg.run_id, app_id=unit.app_id, task_id=unit.task_id,
                kind=unit.kind, trial=unit.trial, segment=cfg.segment)
            cfg.results.append(_write_result(episode, self.runs_dir, unit.task_id,
                                             segment=cfg.segment, device="emulator-5666"))
            (episode / "replay.json").write_text('{"replayed": true, "claims": []}')
            # The heavy artifacts a real episode leaves; their presence is what tells
            # `show` this row can be re-verified here and the imported ones cannot.
            (episode / "evidence").mkdir(exist_ok=True)
            (episode / "app_snapshot.tar").write_bytes(b"\x00" * 1024)
        return cfg.results


class _FakeSession:
    async def available_devices(self):
        return ["emulator-5666"]


def _stub_machine_b(monkeypatch, tmp_path: Path, engine: _FakeLanes) -> None:
    """Machine B's corpus and lane engine: same specs and same APK hash as A, no
    device and no agent."""
    from qualgentbench import bugs, cli, lanes

    apk = tmp_path / "machine-b-birday.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(bugs, "load_apps", lambda *a, **kw: [SUITE])
    monkeypatch.setattr(cli, "_resolve_app_apk", lambda app, spec=None, mode="hunt": apk)
    monkeypatch.setattr(lanes, "run_lanes", engine)


async def _resume_on(runs_dir: Path, run_id: str) -> list[RunResult]:
    from qualgentbench import cli

    plan = checkpoint.load_plan(runs_dir, run_id)
    return await cli._run_episodes(
        [plan.model], plan.agent, _FakeSession(), "", runs_dir, plan.trials,
        mode=plan.mode, devices=["emulator-5666"], plain=True, yes=True, resume=plan)


def _cli(*args):
    from click.testing import CliRunner

    from qualgentbench.cli import main

    return CliRunner().invoke(main, [str(a) for a in args])


def _unwrapped(text: str) -> str:
    """All whitespace stripped. Rich wraps a long path mid-token, so a printed command
    can only be compared to the one a user would type with the wrapping removed."""
    return "".join(text.split())


def _archive_names(bundle: Path) -> list[str]:
    with tarfile.open(bundle, "r:gz") as tar:
        return sorted(tar.getnames())


def _archive_bytes(bundle: Path) -> bytes:
    """Every member's bytes concatenated — for "this string is nowhere in the file"."""
    with tarfile.open(bundle, "r:gz") as tar:
        return b"".join(
            (tar.extractfile(m) or open(__file__, "rb")).read()
            for m in tar.getmembers() if m.isfile())


# ── the hand-off ──────────────────────────────────────────────────────────────


async def test_a_stopped_sweep_is_exported_imported_and_finished_on_another_machine(
        machine_a, tmp_path, monkeypatch):
    """The whole promise in one test, across two runs dirs that share no path."""
    transfer = tmp_path / "transfer"
    transfer.mkdir()
    machine_b = tmp_path / "machine-b" / "runs"
    machine_b.mkdir(parents=True)

    # ── 1. machine A: export ──────────────────────────────────────────────────
    exported = checkpoint.export_bundle(machine_a, RUN_ID, output=transfer)
    manifest = exported.manifest

    assert exported.path.parent == transfer
    assert exported.path.name == f"qgb-checkpoint-{RUN_ID}-seg0.tar.gz"
    assert manifest["run_id"] == RUN_ID
    assert manifest["counts"]["done"] == len(FINISHED_ON_A)
    assert manifest["counts"]["remaining"] == len(OWED_TO_B)
    # The interrupted episode is quarantined by the export, before anything is packed.
    assert exported.discarded == (
        f"{INTERRUPTED_ON_A}/{_episode_dir(machine_a, INTERRUPTED_ON_A).name}",)
    assert manifest["counts"]["discarded"] == 1
    # Its unit is owed, not scored: it is in `remaining` beside the never-started ones.
    assert sorted(u["task"] for u in manifest["remaining"]) == sorted(OWED_TO_B)

    # ── 2. the bundle: results only, no authentication material ───────────────
    names = _archive_names(exported.path)
    assert names == sorted(
        [checkpoint.MANIFEST_NAME]
        + [f"_runs/{RUN_ID}/{f}" for f in ("board.json", "plan.json", "schedule.jsonl")]
        + [f"{task}/{_episode_dir(machine_a, task).name}/{name}"
           for task in FINISHED_ON_A
           for name in ("adb_counts.json", "episode.json", "instruction_sent.md",
                        "interactions.json", "replay.json", "result.json",
                        "verifier/ctrf.json", "workspace/findings.yaml")])
    # Nothing from the interrupted episode travels — not even its marker.
    assert not any(INTERRUPTED_ON_A in name for name in names)
    # The agent's config home, its transcript, the evidence, the snapshot and the
    # run-level provider state are all absent by name...
    for absent in ("claude_home", "codex_home", "agent/transcript.txt", "evidence/",
                   "app_snapshot.tar", "mcp_config.json", ".env", "rate_limit.json",
                   "stop.json", "settings.json"):
        assert not any(absent in name for name in names), absent
    # ...and no credential marker survives anywhere in the bytes.
    blob = _archive_bytes(exported.path)
    assert checkpoint.scan_for_secrets(blob) is None
    for planted in (b"sk-ant-", b"NOTREAL", b"Bearer ", b"refreshToken",
                    b"CLAUDE_CODE_OAUTH_TOKEN"):
        assert planted not in blob, planted

    # Machine A keeps the interrupted episode as evidence, out of the scoring glob.
    discarded = (checkpoint.discarded_dir(machine_a, RUN_ID) / INTERRUPTED_ON_A
                 / _episode_dir(machine_a, INTERRUPTED_ON_A).name)
    assert (discarded / "agent" / "transcript.txt").exists()
    assert not _episode_dir(machine_a, INTERRUPTED_ON_A).exists()

    # ── 3. machine B: import into a runs dir that never saw machine A ─────────
    imported = checkpoint.import_bundle(exported.path, machine_b)

    assert imported.run_id == RUN_ID, "the run id is the hand-off's identity"
    assert imported.resume_command == f"qualgent-bench run --resume {RUN_ID} --runs-dir {machine_b}"
    assert len(imported.episodes) == len(FINISHED_ON_A)
    assert checkpoint.imported_episodes(machine_b, RUN_ID) == set(imported.episodes)
    # B sees exactly A's three finished units as done, and owes the other three.
    state_b = checkpoint.state(machine_b, RUN_ID)
    assert sorted(k[1] for k in state_b.done_keys) == sorted(FINISHED_ON_A)
    assert state_b.orphans == [], "an interrupted episode cannot arrive in a bundle"
    # Nothing about machine A's account came with it.
    meta_b = checkpoint.run_meta_dir(machine_b, RUN_ID)
    assert not (meta_b / "rate_limit.json").exists()
    assert not (meta_b / "stop.json").exists()

    # ── 4. machine B: resume ──────────────────────────────────────────────────
    engine = _FakeLanes(machine_b)
    _stub_machine_b(monkeypatch, tmp_path, engine)
    produced = await _resume_on(machine_b, RUN_ID)

    # Only the units A did not finish, and every one of them.
    assert engine.seen == sorted(("birday", task, 1) for task in OWED_TO_B)
    assert len(produced) == len(OWED_TO_B)
    # Same run id, next segment — the two sittings are one run.
    assert (engine.cfg.run_id, engine.cfg.segment) == (RUN_ID, 1)
    assert {r.run_id for r in produced} == {RUN_ID}
    assert {r.provenance.get("segment") for r in produced} == {1}
    assert {r.provenance.get("device") for r in produced} == {"emulator-5666"}

    # The resume is auditable from B's own event log.
    events = [json.loads(line) for line
              in (meta_b / "schedule.jsonl").read_text().splitlines()]
    resumed = next(e for e in events if e["event"] == "resume")
    assert (resumed["segment"], resumed["done"], resumed["remaining"]) == (1, 3, 3)
    # Machine A's own start event travelled with the bundle: one log, both sittings.
    assert [e["event"] for e in events] == ["start", "resume"]

    # The scope never moved: same six units, only the segment counter advanced.
    plan_b = json.loads((meta_b / "plan.json").read_text())
    assert [u["task"] for u in plan_b["units"]] == TASKS
    assert (plan_b["run_id"], plan_b["segment"]) == (RUN_ID, 1)

    # ── 5. machine B: one blended board ───────────────────────────────────────
    board = json.loads((meta_b / "board.json").read_text())
    assert board["run_id"] == RUN_ID
    assert sorted(row["task_id"] for row in board["episodes"]) == TASKS
    # Each row still names the machine that earned it.
    by_task = {row["task_id"]: row["provenance"]["device"] for row in board["episodes"]}
    assert [by_task[t] for t in FINISHED_ON_A] == ["emulator-5554"] * 3
    assert [by_task[t] for t in OWED_TO_B] == ["emulator-5666"] * 3

    shown = _cli("show", "--runs-dir", machine_b, "--run", RUN_ID,
                 "--agent", "claude-code", "--mode", "guided")
    assert shown.exit_code == 0, shown.output
    flat = " ".join(shown.output.split())     # rich wraps; compare on the joined text
    assert f"{len(FINISHED_ON_A)} of {len(TASKS)} episode(s) imported from a checkpoint" in flat
    assert "cannot be re-verified here" in flat

    # And the run is now complete on B: nothing left, from the same predicate the
    # resume subtracted with.
    assert checkpoint.run_summary(machine_b, RUN_ID)["counts"]["remaining"] == 0


async def test_the_second_machine_can_be_handed_the_run_by_cli_alone(
        machine_a, tmp_path, monkeypatch):
    """The documented commands, run as documented: export, import, show. No Python
    API calls — this is the flow `docs/checkpointing.md` tells a user to type."""
    transfer = tmp_path / "transfer"
    transfer.mkdir()
    machine_b = tmp_path / "machine-b" / "runs"

    out = _cli("checkpoint", "export", RUN_ID, "--runs-dir", machine_a, "-o", transfer)
    assert out.exit_code == 0, out.output
    assert "Discarded 1 interrupted episode(s)" in " ".join(out.output.split())
    bundle = transfer / f"qgb-checkpoint-{RUN_ID}-seg0.tar.gz"
    assert bundle.exists()

    out = _cli("checkpoint", "import", bundle, "--runs-dir", machine_b)
    assert out.exit_code == 0, out.output
    flat = " ".join(out.output.split())
    assert f"Imported run {RUN_ID}" in flat
    assert f"{len(FINISHED_ON_A)} episode(s)" in flat
    assert f"{len(OWED_TO_B)} unit(s) still to run" in flat
    # Compared without whitespace: rich wraps a long --runs-dir path mid-token, and
    # what matters is that the command it prints is the one to type.
    assert _unwrapped(f"qualgent-bench run --resume {RUN_ID} --runs-dir {machine_b}") \
        in _unwrapped(out.output)

    # `checkpoint show` reads the imported run off B's disk and agrees with the bundle.
    out = _cli("checkpoint", "show", RUN_ID, "--runs-dir", machine_b, "--json")
    view = json.loads(out.output)
    assert out.exit_code == 0
    assert view["run_id"] == RUN_ID
    assert view["counts"]["done"] == len(FINISHED_ON_A)
    assert view["counts"]["remaining"] == len(OWED_TO_B)
    assert view["counts"]["orphans"] == 0
    assert view["imported_episodes"] == len(FINISHED_ON_A)
    assert sorted(u["task"] for u in view["remaining"]) == sorted(OWED_TO_B)


async def test_a_bundle_cannot_overwrite_a_run_the_second_machine_already_has(
        machine_a, tmp_path):
    """Two machines that both ran a unit have produced two different answers. The
    import refuses rather than picking one — silently keeping either is how a board
    stops being reproducible."""
    transfer = tmp_path / "transfer"
    transfer.mkdir()
    machine_b = tmp_path / "machine-b" / "runs"
    bundle = checkpoint.export_bundle(machine_a, RUN_ID, output=transfer).path

    checkpoint.import_bundle(bundle, machine_b)
    # Same bundle again: a re-send, not a conflict.
    assert checkpoint.import_bundle(bundle, machine_b).run_id == RUN_ID

    # B ran one of A's finished units itself and got a different number.
    landed = machine_b / FINISHED_ON_A[0] / _episode_dir(machine_a, FINISHED_ON_A[0]).name
    body = json.loads((landed / "result.json").read_text())
    body["score"] = 0.0
    (landed / "result.json").write_text(json.dumps(body))

    with pytest.raises(checkpoint.CheckpointError) as raised:
        checkpoint.import_bundle(bundle, machine_b)
    assert "already exists" in str(raised.value)
    assert "result.json" in str(raised.value)
