"""`rescore_journey.py --dry-run` prints exactly what a write would publish (QUA-2816).

Published boards come from `--dry-run`, so any verdict the write path keeps and the dry
run does not (or the reverse) changes published numbers. The dry run once skipped the
`flag_nonce` carry-over: a nonce-voided episode counted again on every published board.
Both paths now share one merge (`merge_metrics` / `rescored_fields`); these tests run
both over temp copies of one synthetic run dir whose episodes carry every void and
run-time fact a rescore has to keep, and demand identical output.

No test here reaches a device or a model.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from qualgentbench import failures, journey
from qualgentbench.contamination import devloop_default_roots
from qualgentbench.leaderboard import load_results
from qualgentbench.result import RunResult
from qualgentbench.task import BenchmarkTask

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rescore_journey_2816",
                                               ROOT / "scripts" / "rescore_journey.py")
rescore_journey = importlib.util.module_from_spec(_spec)
sys.modules["rescore_journey_2816"] = rescore_journey
_spec.loader.exec_module(rescore_journey)

RUN_ID = "20260924-000000-2816"
FINDINGS = "verdict: pass\nbugs: []\n"
NONCE_HIT = {"kind": "flag_nonce", "tool": "Bash",
             "detail": "episode flag nonce appeared in agent-visible output"}
ROOTED = {"uid": 0, "rooted": True, "root_primed": False, "unprimed": False}
UNCLEAN = {"isolation": "per_mcp_session", "sessions": [{"clean_at_start": False}],
           "clean": False}
NO_RECORD = {"isolation": "unavailable", "sessions": [], "clean": False}


def _tx(*calls: tuple[str, dict, str]) -> str:
    """A claude-code stream-json transcript of (tool, input, result) triples."""
    lines = []
    for i, (name, inp, result) in enumerate(calls):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result}]}}))
    return "\n".join(lines)


def _corpus_task(oracle: dict | None = None) -> BenchmarkTask:
    """What `journey.journey_tasks` hands the rescore: the case, no episode facts."""
    expect = oracle or {"present": "x"}
    spec = {"mode": "journey", "app_id": "app", "case_id": "case-1", "version": "clean",
            "name": "Case", "steps": ["Open the list"], "expected_outcome": "It opens.",
            "active_bugs": [], "step_budget": 30, "expected": "PASS", "blocking": None,
            "blocking_texts": [], "crash_texts": [], "echo_texts": [], "absence_texts": [],
            "side": [], "defects": {}, "oracle": journey._oracle({"check": {"expect": expect}}),
            "truth_agrees": True}
    return BenchmarkTask(id="case-1~clean", name="Case", instruction="", app_file_id="",
                         app_name="App", platform="android", bundle_id="com.x", bug_spec=spec)


TAP = ("Bash", {"command": "adb shell input tap 1 1"}, "")   # a device action: not infra_failure


def _episode(runs: Path, name: str, *, transcript: str = _tx(TAP), metrics: dict | None = None,
             provenance: dict | None = None) -> Path:
    ep = runs / "case-1" / name
    (ep / "agent").mkdir(parents=True)
    (ep / "workspace").mkdir()
    (ep / "agent" / "transcript.txt").write_text(transcript)
    (ep / "workspace" / journey.FILENAME).write_text(FINDINGS)
    result = {"task_id": "case-1~clean", "task_version": "v", "task_type": journey.TASK_TYPE,
              "agent": "a", "model": "m", "condition": "raw", "trial": 1, "passed": True,
              "score": 1.0, "weighted_score": 0.0, "started_at": "2026-09-24T00:00:00Z",
              "ended_at": "2026-09-24T00:01:00Z", "wall_time_sec": 60.0, "exit_code": 0,
              "artifact_dir": f"case-1/{name}", "run_id": RUN_ID,
              "metrics": {"completed": True, "version": "clean", "app_id": "app",
                          **(metrics or {})},
              "provenance": provenance or {}}
    RunResult.model_validate(result)                  # load_results must read it
    (ep / "result.json").write_text(json.dumps(result, indent=2))
    return ep


def _synthetic_run(runs: Path) -> dict[str, Path]:
    """One run dir: an episode per preserved verdict, one carrying all of them, a clean
    control. Every void below must survive a rescore, dry run or write."""
    devloop_root = runs.parent / "devloop-reported-root"   # a server-reported, non-default root
    everything_tx = _tx(
        TAP,
        ("Bash", {"command": "adb -P 5037 shell ls /sdcard"}, "Download"),   # adb_server_bypass
        ("Read", {"file_path": f"{devloop_root}/sess-other/shot.png"}, "…"),  # devloop_artifacts
    )
    nonce = {"contaminated": True, "contamination_reasons": ["flag_nonce"],
             "contamination_hits": [NONCE_HIT]}
    return {
        "flag_nonce": _episode(runs, "ep-nonce", metrics=nonce),
        "everything": _episode(
            runs, "ep-everything", transcript=everything_tx,
            metrics={**nonce, "failure_class": "rate_limited", "app_crashes": 2,
                     "fault_fired": ["qgb-site-1"]},
            provenance={"adbd_at_end": ROOTED,
                        "mcp_server": {"name": "devloop-mcp"},
                        "mcp_isolation": {**UNCLEAN, "artifact_roots": [str(devloop_root)]}}),
        "adbd_rooted": _episode(runs, "ep-rooted", provenance={"adbd_at_end": ROOTED}),
        "mcp_unclean": _episode(runs, "ep-unclean", provenance={
            "mcp_server": {"name": "devloop-mcp"}, "mcp_isolation": UNCLEAN}),
        "mcp_isolation_unverified": _episode(runs, "ep-unverified", provenance={
            "mcp_server": {"name": "other-server"}, "mcp_isolation": NO_RECORD}),
        "clean": _episode(runs, "ep-clean"),
    }


def _copy(tmp_path: Path, name: str) -> Path:
    runs = tmp_path / "src" / "runs"
    if not runs.exists():
        _synthetic_run(runs)
    dst = tmp_path / name / "runs"
    shutil.copytree(runs, dst)
    return dst


#: Which verdict each synthetic episode carries → its episode dir name.
EPISODES = {"flag_nonce": "ep-nonce", "everything": "ep-everything", "adbd_rooted": "ep-rooted",
            "mcp_unclean": "ep-unclean", "mcp_isolation_unverified": "ep-unverified",
            "clean": "ep-clean"}


@pytest.mark.parametrize("which", EPISODES)
def test_dry_run_returns_exactly_what_a_write_puts_on_disk(tmp_path, which):
    dry_runs, wet_runs = _copy(tmp_path, "dry"), _copy(tmp_path, "wet")
    dry_ep = dry_runs / "case-1" / EPISODES[which]
    wet_ep = wet_runs / "case-1" / EPISODES[which]
    untouched = (dry_ep / "result.json").read_bytes()

    s1, *_, v_dry = rescore_journey.rescore(dry_ep, {"case-1~clean": _corpus_task()}, True)
    s2, *_, v_wet = rescore_journey.rescore(wet_ep, {"case-1~clean": _corpus_task()}, False)

    assert s1 == s2 == "rescored"
    assert (dry_ep / "result.json").read_bytes() == untouched        # a dry run writes nothing
    written = json.loads((wet_ep / "result.json").read_text())
    dry = json.loads(json.dumps(rescore_journey.rescored_fields(v_dry)))
    assert dry == {k: written[k] for k in dry}
    # The two copies hold identical inputs, so their verdicts agree field for field.
    assert json.loads(json.dumps(rescore_journey.rescored_fields(v_wet))) == {
        k: written[k] for k in dry}


def _rescored(tmp_path, dry_run: bool) -> dict[str, dict]:
    runs = _copy(tmp_path, "dry" if dry_run else "wet")
    out = {}
    for which, name in EPISODES.items():
        _, _, _, v = rescore_journey.rescore(runs / "case-1" / name,
                                             {"case-1~clean": _corpus_task()}, dry_run)
        out[which] = v.metrics if dry_run else json.loads(
            (runs / "case-1" / name / "result.json").read_text())["metrics"]
    return out


@pytest.mark.parametrize("dry_run", [True, False])
def test_every_preserved_void_survives_both_paths(tmp_path, dry_run):
    m = _rescored(tmp_path, dry_run)
    # flag_nonce: the nonce is never saved, so the rescore cannot re-check it — kept.
    assert m["flag_nonce"]["contaminated"] is True
    assert m["flag_nonce"]["contamination_reasons"] == ["flag_nonce"]
    assert NONCE_HIT in m["flag_nonce"]["contamination_hits"]
    assert failures.is_excluded(m["flag_nonce"])
    # Every recomputed void, with the preserved one, on the same episode.
    e = m["everything"]
    assert e["contamination_reasons"] == ["adb_server_bypass", "adbd_rooted",
                                          "devloop_artifacts", "flag_nonce"]
    assert e["mcp_unclean"] is True and failures.is_excluded(e)
    # Run-time facts the scorer does not recompute.
    assert e["failure_class"] == "rate_limited"
    assert e["app_crashes"] == 2 and e["fault_fired"] == ["qgb-site-1"]
    assert m["adbd_rooted"]["contamination_reasons"] == ["adbd_rooted"]
    assert m["mcp_unclean"]["mcp_unclean"] is True and failures.is_excluded(m["mcp_unclean"])
    assert m["mcp_isolation_unverified"]["mcp_isolation_unverified"] is True
    assert not failures.is_excluded(m["mcp_isolation_unverified"])
    assert not m["clean"]["contaminated"] and not failures.is_excluded(m["clean"])


def test_the_server_reported_devloop_root_is_read_from_provenance(tmp_path):
    """Live, the scan checks the defaults PLUS the roots the server reported
    (`run_episode`); without the reported ones a rescore un-voids a read of another
    episode's artifacts there."""
    runs = _copy(tmp_path, "roots")
    ep = runs / "case-1" / "ep-everything"
    result = json.loads((ep / "result.json").read_text())
    result["provenance"]["mcp_isolation"].pop("artifact_roots")
    (ep / "result.json").write_text(json.dumps(result))
    _, _, _, v = rescore_journey.rescore(ep, {"case-1~clean": _corpus_task()}, True)
    assert "devloop_artifacts" not in v.metrics["contamination_reasons"]
    assert str(runs.parent / "devloop-reported-root") not in devloop_default_roots()


def _main(monkeypatch, capsys, runs: Path, *extra: str) -> str:
    monkeypatch.setattr(rescore_journey.bugs, "load_apps", lambda: [{"app": {"id": "app"}}])
    monkeypatch.setattr(rescore_journey.journey, "journey_tasks", lambda suite: [_corpus_task()])
    monkeypatch.setattr(sys, "argv", ["rescore_journey.py", "--runs-dir", str(runs),
                                      "--run", RUN_ID, *extra])
    assert rescore_journey.main() == 0
    return capsys.readouterr().out


def _board(out: str) -> str:
    return out[out.index("Board:"):]


def test_the_dry_run_board_is_the_board_a_write_publishes(tmp_path, monkeypatch, capsys):
    dry = _main(monkeypatch, capsys, _copy(tmp_path, "dry"), "--dry-run")
    wet_runs = _copy(tmp_path, "wet")
    wet = _main(monkeypatch, capsys, wet_runs)
    assert dry.replace("would change", "changed") == wet
    # The board a later reader rebuilds from the written result.json files is the same.
    again = _main(monkeypatch, capsys, wet_runs, "--dry-run")
    assert _board(again) == _board(dry)
    rows = journey.summary(load_results(wet_runs, run_id=RUN_ID))
    (row,) = rows
    assert row["planned_episodes"] == 6 and row["excluded_episodes"] == 4
    # The board names every exclusion kind, the two QUA-2816 found missing included.
    note = journey.integrity_note(rows)
    assert note in _board(dry)
    for kind in ("flag_nonce", "adb_server_bypass", "adbd_rooted", "devloop_artifacts",
                 "mcp_unclean", "mcp_isolation_unverified"):
        assert journey.INTEGRITY_LABELS[kind] in note, kind
    assert "2 episode flag nonce reached the agent (contaminated, excluded)" in note


def test_one_episode_s_restored_oracle_never_reaches_the_next(tmp_path):
    """The corpus task is shared by every episode of a case. The rescore used to write
    each episode's spec back onto it, so a restored db outcome (`oracle_result`) stayed
    set for the next episode, and `_restore_oracle` never overrides one already set:
    that episode was rescored against the previous episode's device answer."""
    task = _corpus_task({"db": "n.db", "query": "select count(*) from t", "equals": "1"})
    runs = tmp_path / "runs"
    held = {"mode": "db", "ok": True, "why": "", "detail": "db → '1'", "result": "holds"}
    broke = {"mode": "db", "ok": False, "why": "", "detail": "db → '0'", "result": "violated"}
    first = _episode(runs, "ep-1", metrics={"oracle": held})
    second = _episode(runs, "ep-2", metrics={"oracle": broke})
    tasks = {task.id: task}
    before = dict(task.bug_spec)
    _, _, _, v1 = rescore_journey.rescore(first, tasks, True)
    _, _, _, v2 = rescore_journey.rescore(second, tasks, True)
    assert v1.metrics["oracle"]["result"] == "holds"
    assert v2.metrics["oracle"]["result"] == "violated"
    assert task.bug_spec == before                    # the corpus task is never written to
