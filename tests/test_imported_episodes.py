"""Imported, results-only episodes on the board (QUA-2696).

A checkpoint bundle carries `result.json` and `replay.json` and nothing heavy: no
`app_snapshot.tar`, no `evidence/`, no workspace. So on the machine that imports it,
a finished episode has a complete SCORE inside an incomplete dir. Two passes used to
assume every episode dir on disk was complete:

* the replay staleness pass would re-replay it against a device that never installed
  the app, overwriting a real verdict with a meaningless one;
* `show` would print its numbers with nothing to say they cannot be re-verified here.

These tests pin both: the imported episode is skipped and keeps its recorded hybrid
score, the local one beside it is still re-replayed, and the board says how many rows
came from somewhere else.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from qualgentbench import checkpoint, leaderboard as lb
from qualgentbench.result import RunResult

RUN_ID = "20260909-090000-beef"
CURRENT_FP = "fingerprint-current"
STALE_FP = "fingerprint-from-another-checkout"


# ── fixture ───────────────────────────────────────────────────────────────────


def _episode_dir(runs_dir: Path, task_id: str) -> Path:
    return runs_dir / task_id / f"2026-09-09T00-00-00Z_{task_id}_claude-code_m_raw_trial-1"


def _write_episode(runs_dir: Path, task_id: str, *, overall: float,
                   artifacts: bool, replayer: str = STALE_FP) -> Path:
    """One finished hunt episode. `artifacts=False` is the results-only shape a
    bundle produces — result.json and replay.json, nothing a replay could open."""
    ep = _episode_dir(runs_dir, task_id)
    (ep / "workspace").mkdir(parents=True, exist_ok=True)
    (ep / "workspace" / "findings.yaml").write_text("findings: []\n")
    (ep / "result.json").write_text(json.dumps({
        "task_id": task_id, "task_version": "v1", "task_type": "bug_hunt",
        "agent": "claude-code", "model": "anthropic/claude-opus-4-8",
        "condition": "raw", "trial": 1, "passed": True, "score": 1.0,
        "weighted_score": 1.0, "started_at": "2026-09-09T00:00:00+00:00",
        "ended_at": "2026-09-09T00:05:00+00:00", "wall_time_sec": 300.0,
        "exit_code": 0, "criteria": {}, "failure_reason": None, "run_id": RUN_ID,
        "metrics": {
            "app_id": "birday", "condition": "raw", "total_tokens": 1000,
            "device_serial": "emulator-5554", "hook_steps": 40,
            "hybrid": {"scoring": "hybrid-v1", "f1": 0.8, "controls": 4,
                       "false_positives": 1, "overall": overall,
                       "overall_raw": overall, "trust": 1.0},
        },
        "artifact_dir": str(ep.relative_to(runs_dir)),
        "provenance": {"device": "emulator-5554", "lane": 0, "segment": 0},
    }, indent=2))
    (ep / "replay.json").write_text(json.dumps(
        {"app_id": "birday", "replayer": replayer,
         "results": [{"area": "reminders", "claimed": "deviates",
                      "classification": "confirmed"}]}))
    if artifacts:
        (ep / "app_snapshot.tar").write_bytes(b"\x00" * 64)
        (ep / "evidence" / "frames").mkdir(parents=True, exist_ok=True)
        (ep / "evidence" / "frames" / "0001.jpg").write_bytes(b"\xff\xd8jpeg")
    return ep


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    """A runs dir a coworker would have after `checkpoint import`: one episode that
    ran here, one that arrived in a bundle. Both replay.json files are stamped by a
    replayer that is no longer current, so the staleness pass wants to re-run both."""
    d = tmp_path / "runs"
    _write_episode(d, "local-task", overall=0.40, artifacts=True)
    imported = _write_episode(d, "imported-task", overall=0.90, artifacts=False)

    marker = d / checkpoint.RUN_META_DIR / RUN_ID / checkpoint.IMPORT_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "schema_version": checkpoint.BUNDLE_SCHEMA_VERSION, "run_id": RUN_ID,
        "episodes": [str(imported.relative_to(d))],
        "imports": [{"bundle": f"qgb-checkpoint-{RUN_ID}-seg0.tar.gz", "segment": 0,
                     "exported_by": "other-laptop", "files": 12}],
    }, indent=2))
    return d


def _results(runs_dir: Path) -> list[RunResult]:
    return lb.load_results(runs_dir, agent="claude-code")


# ── the predicate ─────────────────────────────────────────────────────────────


def test_marker_and_missing_artifacts_both_mark_an_episode_results_only(runs_dir):
    only = lb.results_only_dirs(runs_dir, _results(runs_dir))
    assert only == {_episode_dir(runs_dir, "imported-task").resolve()}


def test_an_episode_copied_in_without_the_marker_is_still_results_only(runs_dir, tmp_path):
    """A results tree can arrive by rsync or a hand-unpacked tar, with no
    `imported.json` to read. Missing every heavy artifact is enough on its own."""
    (runs_dir / checkpoint.RUN_META_DIR / RUN_ID / checkpoint.IMPORT_MARKER).unlink()
    only = lb.results_only_dirs(runs_dir, _results(runs_dir))
    assert only == {_episode_dir(runs_dir, "imported-task").resolve()}


def test_a_pre_snapshot_episode_is_not_treated_as_imported(tmp_path):
    """Episodes older than snapshot capture have no `app_snapshot.tar` but do have
    their evidence, and `replay_findings.py` handles the missing snapshot itself.
    Skipping those would quietly stop re-verifying real local work."""
    d = tmp_path / "runs"
    ep = _write_episode(d, "legacy-task", overall=0.5, artifacts=True)
    (ep / "app_snapshot.tar").unlink()
    assert checkpoint.artifacts_are_local(ep) is True
    assert lb.results_only_dirs(d, _results(d)) == set()


# ── the replay staleness pass ─────────────────────────────────────────────────


@pytest.fixture
def replayed(monkeypatch):
    """Record which episode dirs the staleness pass hands to replay_findings.py."""
    calls: list[str] = []

    def fake_run(cmd, *a, **kw):
        # Stand in for replay_findings.py, side effect included: it OVERWRITES
        # replay.json. Without that the skip would look free even when it isn't.
        calls.append(cmd[2])
        (Path(cmd[2]) / "replay.json").write_text(json.dumps(
            {"app_id": "birday", "replayer": CURRENT_FP, "results": []}))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("qualgentbench.replay.replayer_fingerprint", lambda: CURRENT_FP)
    return calls


def test_stale_imported_episode_is_skipped_and_the_local_one_is_replayed(runs_dir, replayed):
    from qualgentbench.cli import _replay_and_board

    imported = _episode_dir(runs_dir, "imported-task")
    before = (imported / "result.json").read_text(), (imported / "replay.json").read_text()

    _replay_and_board(_results(runs_dir), runs_dir)

    assert [Path(c).name for c in replayed] == [_episode_dir(runs_dir, "local-task").name]
    # Nothing re-derived it, so its score and its recorded verdict are untouched.
    assert (imported / "result.json").read_text() == before[0]
    assert (imported / "replay.json").read_text() == before[1]


def test_the_imported_episode_keeps_its_hybrid_score_through_the_pass(runs_dir, replayed):
    """The ticket's invariant. The imported episode's verdict was earned on another
    machine; a re-replay here would replace it with an empty one, and the score the
    board prints would drop for a reason that has nothing to do with the agent."""
    from qualgentbench.cli import _replay_and_board

    _replay_and_board(_results(runs_dir), runs_dir)

    scores = {r.task_id: r.metrics["hybrid"]["overall"] for r in _results(runs_dir)}
    assert scores == {"imported-task": 0.90, "local-task": 0.40}

    imported = json.loads((_episode_dir(runs_dir, "imported-task") / "replay.json").read_text())
    assert imported["replayer"] == STALE_FP
    assert imported["results"] == [{"area": "reminders", "claimed": "deviates",
                                    "classification": "confirmed"}]
    # The local one beside it WAS re-derived — the skip is targeted, not a blanket off.
    local = json.loads((_episode_dir(runs_dir, "local-task") / "replay.json").read_text())
    assert local["replayer"] == CURRENT_FP and local["results"] == []


def test_a_run_of_only_imported_episodes_replays_nothing(runs_dir, replayed):
    """The whole point of the hand-off: the receiving machine can render a board for
    work it did not do, without a device attached."""
    from qualgentbench.cli import _replay_and_board

    local = _episode_dir(runs_dir, "local-task")
    (local / "app_snapshot.tar").unlink()
    (local / "evidence" / "frames" / "0001.jpg").unlink()
    (local / "evidence" / "frames").rmdir()
    (local / "evidence").rmdir()

    _replay_and_board(_results(runs_dir), runs_dir)
    assert replayed == []


# ── show ──────────────────────────────────────────────────────────────────────


def _cli(*args):
    from click.testing import CliRunner

    from qualgentbench.cli import main

    return CliRunner().invoke(main, list(args))


def test_show_completes_and_reports_the_imported_count(runs_dir, replayed):
    """The acceptance case: a stale fingerprint plus an imported episode, and the
    board still renders — with a count telling the reader which rows are hearsay."""
    out = _cli("show", "--runs-dir", str(runs_dir), "--mode", "hunt",
               "--agent", "claude-code")
    assert out.exit_code == 0, out.output
    flat = " ".join(out.output.split())   # rich wraps; compare on the joined text
    assert "1 of 2 episode(s) imported from a checkpoint bundle" in flat
    assert "cannot be re-verified here" in flat
    # Rendering the board must not have touched the device path.
    assert replayed == []


def test_show_says_nothing_when_every_episode_is_local(runs_dir):
    (runs_dir / checkpoint.RUN_META_DIR / RUN_ID / checkpoint.IMPORT_MARKER).unlink()
    imported = _episode_dir(runs_dir, "imported-task")
    (imported / "app_snapshot.tar").write_bytes(b"\x00" * 64)

    out = _cli("show", "--runs-dir", str(runs_dir), "--mode", "hunt",
               "--agent", "claude-code")
    assert out.exit_code == 0, out.output
    assert "imported" not in out.output
