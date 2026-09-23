"""A rescore has no device: the db/content oracle outcome must come back from the
saved metrics, or every such episode silently rescoring to completion=None looks
like a scoring change when it is a missing key."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_bugs import _call, _obs, _transcript

from qualgentbench import episode_runner as er
from qualgentbench import replay as rp
from qualgentbench.task import BenchmarkTask

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rescore_journey", ROOT / "scripts" / "rescore_journey.py")
rescore_journey = importlib.util.module_from_spec(_spec)
sys.modules["rescore_journey"] = rescore_journey
_spec.loader.exec_module(rescore_journey)

from qualgentbench import journey, submission  # noqa: E402


def test_bridge_reads_persisted_result():
    spec = {"oracle": {"mode": "db"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": True, "result": "holds",
                                                      "detail": "db ... → '1'"}})
    assert spec["oracle_result"] == "holds" and spec["oracle_detail"].startswith("db")


def test_bridge_derives_from_ok_for_older_runs():
    spec = {"oracle": {"mode": "content"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "content", "ok": False}})
    assert spec["oracle_result"] == "violated"


def test_bridge_leaves_screen_oracles_and_unevaluated_alone():
    spec = {"oracle": {"mode": "present"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "present", "ok": None}})
    assert "oracle_result" not in spec
    spec = {"oracle": {"mode": "db"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": None, "result": None}})
    assert "oracle_result" not in spec


def test_bridge_never_overrides_a_flat_key():
    spec = {"oracle": {"mode": "db"}, "oracle_result": "inconclusive"}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": True, "result": "holds"}})
    assert spec["oracle_result"] == "inconclusive"


def test_verdict_round_trips_through_saved_metrics():
    """What the scorer wrote under metrics['oracle'] must be enough to re-derive the same
    completion without a device."""
    spec = {"oracle": {"mode": "db"}, "oracle_result": "holds", "oracle_detail": "x"}
    ok, _ = journey._oracle_verdict(spec, [])
    assert ok is True
    saved = {"oracle": {"mode": "db", "ok": ok, "why": "", "detail": "x", "result": "holds"}}
    again = {"oracle": {"mode": "db"}}
    rescore_journey._restore_oracle(again, saved)
    assert journey._oracle_verdict(again, [])[0] is True


def test_projection_lines_carry_the_prior_weighted_error_count():
    """QUA-2780: the rescore projection prints expected false alarms + misses for the
    reader's suite as its own line — a printed number, not a ranking key."""
    row = {"agent": "a", "model": "m", "condition": "raw",
           "false_alarm_rate": 0.1, "false_alarm_ci": [0.05, 0.2], "false_alarm_k": 2,
           "false_alarm_n": 20, "catch_rate": 0.8, "catch_ci": [0.5, 0.95], "catch_k": 8,
           "catch_n": 10}
    text = "\n".join(rescore_journey.projection_lines([row], 200, 50))
    assert "prior-weighted errors (false alarms + misses): 30.0 over 250 (bug prior 20%)" in text


# ── QUA-2793: every oracle mode the runner persists round-trips through rescore ──
#
# The runner (`episode_runner._journey_oracle`) evaluates db/content and the liveness
# modes on the device after the agent exits and hands the outcome to the scorer as
# `oracle_result`; the scorer persists it under `metrics.oracle.result`. These tests run
# that whole path device-free — the runner with its device reads stubbed, the live
# scorer, a result.json on disk — then `rescore()` with a corpus-fresh task, and demand
# the rescore reproduce the live completion and oracle record exactly.

_ORACLES = {
    "db": {"db": "n.db", "query": "select count(*) from t", "equals": "1"},
    "content": {"content": "content://x", "contains": "y"},
    "crash": {"crash": True},
    "anr": {"anr": True},
    "stuck": {"stuck": "Save"},
}
_DEATH = {"process": "com.x", "exception": "java.lang.IllegalStateException", "message": "seeded",
          "signature": "java.lang.IllegalStateException@com.x.Repo.save(Repo.java)",
          "classification": "app", "timestamp": "09-14 12:00:05.000"}
_FINDINGS = "verdict: pass\nbugs: []\n"


def _corpus_spec(mode: str) -> dict:
    """What `journey.journey_tasks` hands both the live run and the rescore: the case,
    and none of the episode's device facts."""
    return {"mode": "journey", "app_id": "app", "case_id": "case-1", "version": "clean",
            "name": "Case", "steps": ["Open the list"], "expected_outcome": "It opens.",
            "active_bugs": [], "step_budget": 30, "expected": "PASS", "blocking": None,
            "blocking_texts": [], "crash_texts": [], "echo_texts": [], "absence_texts": [],
            "side": [], "defects": {},
            "oracle": journey._oracle({"check": {"expect": _ORACLES[mode]}}),
            "truth_agrees": True}


def _task(spec: dict) -> BenchmarkTask:
    return BenchmarkTask(id="case-1~clean", name="Case", instruction="", app_file_id="",
                         app_name="App", platform="android", bundle_id="com.x", bug_spec=spec)


def _stub_device(monkeypatch, mode: str, outcome: str) -> list:
    """The runner's device reads for `mode`, answering `outcome`
    (holds|violated|inconclusive). Returns the episode's recorded app deaths."""
    rr = {"holds": rp.ReplayResult(rp.HOLDS, f"{mode} answered", 0),
          "violated": rp.ReplayResult(rp.CRASHED if mode == "stuck" else rp.VIOLATED,
                                      f"{mode} violated", 0, crash={"kind": "anr"}),
          "inconclusive": rp.ReplayResult(rp.INCONCLUSIVE, f"{mode} undecidable", 0)}[outcome]

    async def answer(*a, **k):
        return rr

    async def nothing(*a, **k):
        return ""

    async def boom(*a, **k):
        raise AssertionError(f"a {mode} oracle must not read the device this way")

    for name in ("_check_db", "_check_content", "_check_stuck"):
        monkeypatch.setattr(rp, name, boom)
    monkeypatch.setattr(er, "_adb", nothing)
    monkeypatch.setattr(er, "crash_window", nothing)
    monkeypatch.setattr(er.asyncio, "sleep", nothing)
    if mode in ("db", "content", "stuck"):
        monkeypatch.setattr(rp, {"db": "_check_db", "content": "_check_content",
                                 "stuck": "_check_stuck"}[mode], answer)
        return []
    # crash/anr: the fact is the episode's own death record, read before the oracle.
    if outcome == "holds":
        return []
    # violated: the app died the way the case names. inconclusive: the same death, but
    # with a seeded-site marker on the CLEAN arm (`_live_episode`) — the flag gate did
    # not hold, so the death is not attributable.
    return [{**_DEATH, "kind": "anr" if mode == "anr" else "java"}]


async def _live_episode(tmp_path: Path, monkeypatch, mode: str, outcome: str) -> tuple[Path, dict]:
    """Run the runner's oracle + the live scorer, and persist the episode as the runner
    does (result.json, agent/transcript.txt, workspace/findings.yaml)."""
    spec = _corpus_spec(mode)
    spec.update(tooling="mcp", findings_file=_FINDINGS,
                app_crashes=_stub_device(monkeypatch, mode, outcome))
    if outcome == "inconclusive" and mode in ("crash", "anr"):
        spec["fired"] = ["some-seeded-bug"]
    await er._journey_oracle("serial", "com.x", spec)
    assert spec.get("oracle_result") == outcome, (mode, outcome, spec.get("oracle_detail"))
    transcript = _transcript(_obs("Total: 4 items"),
                             _call("Write", {"file_path": "/w/findings.yaml", "content": _FINDINGS}, "ok"))
    live = journey.journey_verdict(transcript, "m", _task(spec))
    run_dir = tmp_path / "episode"
    (run_dir / "agent").mkdir(parents=True)
    (run_dir / "workspace").mkdir()
    (run_dir / "agent" / "transcript.txt").write_text(transcript)
    (run_dir / "workspace" / journey.FILENAME).write_text(_FINDINGS)
    (run_dir / "result.json").write_text(json.dumps(
        {"task_type": journey.TASK_TYPE, "task_id": "case-1~clean", "condition": "mcp",
         "model": "m", "metrics": live.metrics}))
    return run_dir, live.metrics


@pytest.mark.parametrize("outcome,completed", [("holds", True), ("violated", False),
                                               ("inconclusive", None)])
@pytest.mark.parametrize("mode", sorted(_ORACLES))
async def test_every_persisted_oracle_mode_round_trips_through_rescore(tmp_path, monkeypatch,
                                                                       mode, outcome, completed):
    assert mode in journey.DEVICE_ORACLE_MODES
    run_dir, live = await _live_episode(tmp_path, monkeypatch, mode, outcome)
    assert live["completed"] is completed and live["oracle"]["result"] == outcome
    before_bytes = (run_dir / "result.json").read_bytes()

    fresh = _task(_corpus_spec(mode))                     # no device facts: a rescore has none
    status, before, after, v = rescore_journey.rescore(run_dir, {fresh.id: fresh}, dry_run=True)

    assert status == "rescored"
    assert before is completed and after is completed, (mode, outcome, v.metrics["completion_reason"])
    assert v.metrics["oracle"] == live["oracle"]
    assert v.metrics["completion_scored"] == live["completion_scored"]
    assert (run_dir / "result.json").read_bytes() == before_bytes      # --dry-run writes nothing


def test_the_bridge_knows_every_mode_the_scorer_reads_off_the_device():
    """One list: the scorer's. The liveness modes were missing from the bridge's copy."""
    assert set(journey.DEVICE_ORACLE_MODES) == {"db", "content", *submission.LIVENESS_MODES}
    for mode in journey.DEVICE_ORACLE_MODES:
        spec = {"oracle": {"mode": mode}}
        rescore_journey._restore_oracle(spec, {"oracle": {"mode": mode, "ok": True, "result": "holds"}})
        assert spec["oracle_result"] == "holds", mode
        assert journey._oracle_verdict(spec, [])[0] is True


def test_a_liveness_record_from_before_result_was_persisted_derives_from_ok():
    spec = {"oracle": {"mode": "stuck"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "stuck", "ok": False,
                                                      "why": "stuck oracle violated: x"}})
    assert spec["oracle_result"] == "violated"


def test_an_outcome_saved_for_another_mode_is_not_restored():
    """A case whose oracle changed since the episode ran: the saved answer is to a
    different question."""
    spec = {"oracle": {"mode": "stuck"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": True, "result": "holds"}})
    assert "oracle_result" not in spec


@pytest.mark.parametrize("saved", [
    None,                                                         # no oracle record at all
    {"mode": "stuck", "ok": None, "why": "no oracle for this outcome"},   # a scorer that
    # did not yet read this mode off the device, before `result` was persisted
    {"mode": "present", "ok": True, "why": "outcome text seen on the device"},  # case changed
])
def test_an_unpersisted_outcome_is_unrecoverable_and_the_recorded_result_stands(tmp_path, saved):
    run_dir = tmp_path / "episode"
    (run_dir / "agent").mkdir(parents=True)
    (run_dir / "agent" / "transcript.txt").write_text(_transcript(_obs("Total: 4 items")))
    metrics = {"completed": True, "version": "clean"}
    if saved is not None:
        metrics["oracle"] = saved
    body = json.dumps({"task_type": journey.TASK_TYPE, "task_id": "case-1~clean",
                       "condition": "mcp", "model": "m", "metrics": metrics})
    (run_dir / "result.json").write_text(body)
    fresh = _task(_corpus_spec("stuck"))
    status, before, after, v = rescore_journey.rescore(run_dir, {fresh.id: fresh}, dry_run=False)
    assert status.startswith("unrecoverable: no saved stuck oracle outcome"), status
    assert before is True and after is True and v is None       # never a silent True -> None
    assert (run_dir / "result.json").read_text() == body        # and never rewritten


def test_an_outcome_known_missing_live_is_not_unrecoverable():
    """`result: None` in the same mode, or the scorer's own "not evaluated" line, is what
    the live board saw: rescoring to None reproduces it, it does not lose anything."""
    spec = _corpus_spec("stuck")
    assert rescore_journey._oracle_unrecoverable(spec, {"oracle": {"mode": "stuck", "result": None}}) is None
    old_db = {"oracle": {"mode": "db", "ok": None, "why": "db oracle not evaluated (inconclusive)"}}
    assert rescore_journey._oracle_unrecoverable(_corpus_spec("db"), old_db) is None


def test_an_expected_fail_arm_never_needs_the_oracle():
    spec = {**_corpus_spec("crash"), "blocking": "crash-on-save", "expected": "FAIL"}
    assert rescore_journey._oracle_unrecoverable(spec, {}) is None
    screen = {**_corpus_spec("db"), "oracle": {"mode": "present"}}
    assert rescore_journey._oracle_unrecoverable(screen, {}) is None
