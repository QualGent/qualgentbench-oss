from __future__ import annotations

import json

import pytest

from qualgentbench.scheduler import (
    AFFINITY_RATIO,
    DEFAULT_SEC,
    SEC_PER_STEP,
    Estimator,
    RateLimitBackoff,
    Unit,
    WorkQueue,
    fmt_duration,
    new_run_id,
    plan_summary,
    simulate,
)


def _u(app: str, est: float, trial: int = 1, kind: str = "bug_hunt", task: str = "") -> Unit:
    return Unit(app_id=app, app_name=app, task_id=task or f"explore-{app}", kind=kind,
                label="hunt", trial=trial, est_sec=est, est_source="default")


# ── queue ──────────────────────────────────────────────────────────────────────

def test_fresh_lane_takes_the_longest_unit():
    q = WorkQueue([_u("a", 100), _u("b", 900), _u("c", 300)])
    assert q.take(None).app_id == "b"
    assert q.take(None).app_id == "c"


def test_staged_app_is_preferred_when_close_enough():
    q = WorkQueue([_u("a", 500), _u("b", 900, trial=1), _u("a", 450, trial=2)])
    assert q.take("a").app_id == "a"          # 500 * 2 >= 900 → stay on a
    assert q.take("a").app_id == "a"          # 450 * 2 >= 900 → stay on a
    assert q.take("a").app_id == "b"


def test_a_much_longer_unit_breaks_affinity():
    q = WorkQueue([_u("a", 100), _u("b", 100 * AFFINITY_RATIO + 1)])
    assert q.take("a").app_id == "b"


def test_requeue_bumps_attempt_and_keeps_order():
    q = WorkQueue([_u("a", 100), _u("b", 300)])
    failed = q.take(None)
    assert failed.app_id == "b" and failed.attempt == 1
    q.requeue(failed)
    assert failed.attempt == 2
    assert [u.app_id for u in q.pending()] == ["b", "a"]


# ── simulation ─────────────────────────────────────────────────────────────────

def test_simulate_is_sum_on_one_lane_and_shorter_on_more():
    units = [_u("a", 600), _u("b", 600), _u("c", 600), _u("d", 600)]
    one = simulate(units, 1, switch_sec=0)
    assert one == 2400
    assert simulate(units, 2, switch_sec=0) == 1200
    assert simulate(units, 4, switch_sec=0) == 600


def test_simulate_charges_app_switches_only_on_change():
    units = [_u("a", 100, trial=1), _u("a", 100, trial=2), _u("b", 100)]
    # one lane: a (switch) a (no switch) b (switch) = 300 + 2 switches
    assert simulate(units, 1, switch_sec=45) == 390


def test_simulate_does_not_mutate_inputs():
    units = [_u("a", 100), _u("b", 200)]
    simulate(units, 2)
    assert [u.attempt for u in units] == [1, 1] and len(units) == 2


def test_plan_summary_counts_kinds_and_sources():
    units = [_u("a", 600), _u("a", 100, kind="bug_task", task="t1")]
    s = plan_summary(units, lanes=2)
    assert s["episodes"] == 2 and s["by_kind"] == {"bug_hunt": 1, "bug_task": 1}
    assert s["eta_one_lane_sec"] >= s["eta_sec"]
    assert s["basis"] == {"default": 2}


def test_fmt_duration():
    assert fmt_duration(59) == "59s"
    assert fmt_duration(61) == "1m 01s"
    assert fmt_duration(3 * 3600 + 600) == "3h 10m"


# ── estimator ──────────────────────────────────────────────────────────────────

def _write_result(runs_dir, task, wall, agent="claude-code", model="m", n=1):
    for i in range(n):
        d = runs_dir / task / f"{agent}-{model}-{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({
            "task_id": task, "agent": agent, "model": model, "wall_time_sec": wall}))


def test_estimator_prefers_same_arm_history_then_task_then_budget(tmp_path):
    _write_result(tmp_path, "explore-a", 300, n=3)                   # same arm
    _write_result(tmp_path, "explore-b", 500, agent="codex-cli", n=3)  # other arm
    _write_result(tmp_path, "explore-c", 700, n=2)                   # too few
    est = Estimator(tmp_path, "claude-code", "m")
    assert est.estimate("explore-a", "bug_hunt", 230) == (300, "history")
    assert est.estimate("explore-b", "bug_hunt", 230) == (500, "history")
    assert est.estimate("explore-c", "bug_hunt", 230) == (230 * SEC_PER_STEP, "budget")
    assert est.estimate("explore-d", "bug_hunt", None) == (DEFAULT_SEC["bug_hunt"], "default")


def test_estimator_ignores_dead_episodes(tmp_path):
    _write_result(tmp_path, "explore-a", 2, n=5)   # never really ran
    est = Estimator(tmp_path, "claude-code", "m")
    assert est.estimate("explore-a", "bug_hunt", None)[1] == "default"


# ── backoff ────────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_backoff_doubles_resets_and_caps():
    clock = _Clock()
    b = RateLimitBackoff(lanes=3, clock=clock, rng=lambda: 1.0)
    assert b.note_rate_limit()["hold_sec"] == 30
    clock.t += 30
    assert b.note_rate_limit()["hold_sec"] == 60
    b.note_clean()
    assert b.k == 0
    for _ in range(10):
        b.note_rate_limit()
    assert b.log[-1]["hold_sec"] == 600


def test_backoff_hold_is_global_and_counts_down():
    clock = _Clock()
    b = RateLimitBackoff(lanes=2, clock=clock, rng=lambda: 1.0)
    b.note_rate_limit()
    assert b.hold_remaining() == 30
    clock.t += 10
    assert b.hold_remaining() == 20
    clock.t += 30
    assert b.hold_remaining() == 0


def test_sustained_limits_park_lanes_and_clean_streaks_unpark():
    b = RateLimitBackoff(lanes=3, clock=_Clock(), rng=lambda: 1.0)
    b.note_rate_limit()
    assert b.parked == 0 and b.lane_active(2)
    b.note_rate_limit()                      # second event in the window → park one
    assert b.parked == 1 and not b.lane_active(2) and b.lane_active(1)
    for _ in range(4):
        assert b.note_clean() is None
    assert b.note_clean() == {"event": "unparked", "parked_lanes": 0}
    assert b.lane_active(2)


def test_never_parks_the_last_lane():
    b = RateLimitBackoff(lanes=1, clock=_Clock(), rng=lambda: 1.0)
    for _ in range(6):
        b.note_rate_limit()
    assert b.parked == 0 and b.lane_active(0)


@pytest.mark.asyncio
async def test_wait_returns_once_the_hold_is_over():
    clock = _Clock()
    b = RateLimitBackoff(lanes=1, clock=clock, rng=lambda: 1.0, base_sec=0.01)
    b.note_rate_limit()
    clock.t += 100
    await b.wait()


def test_run_id_is_sortable_and_unique_enough():
    a, b = new_run_id(), new_run_id()
    assert len(a) == len("20260820-143012-abcd") and a != b


def test_estimator_prefers_the_lane_wall_time_over_the_agent_alone(tmp_path):
    for i in range(3):
        d = tmp_path / "explore-a" / f"r{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({
            "task_id": "explore-a", "agent": "claude-code", "model": "m",
            "wall_time_sec": 480, "provenance": {"lane_wall_sec": 1380}}))
    est = Estimator(tmp_path, "claude-code", "m")
    assert est.estimate("explore-a", "bug_hunt", None) == (1380, "history")


def test_estimator_ignores_excluded_episodes(tmp_path):
    """The 37-second "Not logged in" failure must not drag a median down."""
    rows = [(1500, {}), (1600, {}), (37, {"env_failure": True}), (40, {"failure_class": "rate_limited"})]
    for i, (wall, metrics) in enumerate(rows):
        d = tmp_path / "explore-a" / f"r{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({
            "task_id": "explore-a", "agent": "claude-code", "model": "m",
            "wall_time_sec": wall, "metrics": metrics}))
    est = Estimator(tmp_path, "claude-code", "m")
    # only two valid samples → not enough history → budget
    assert est.estimate("explore-a", "bug_hunt", 100)[1] == "budget"
    d = tmp_path / "explore-a" / "r9"
    d.mkdir()
    (d / "result.json").write_text(json.dumps({
        "task_id": "explore-a", "agent": "claude-code", "model": "m",
        "wall_time_sec": 1400, "metrics": {}}))
    assert Estimator(tmp_path, "claude-code", "m").estimate("explore-a", "bug_hunt", 100) == (1500, "history")


def test_adb_outage_holds_all_lanes_without_parking_or_attempts():
    """An adb-SERVER outage is machine-wide: every lane holds briefly, none is
    parked or retired for it, and the requeued unit keeps its attempt count —
    the 2026-08-31 Docker run retired all three lanes in one second over a
    single dead daemon and stranded a third of the tier."""
    clock = _Clock()
    b = RateLimitBackoff(lanes=3, clock=clock, rng=lambda: 1.0)
    first = b.note_adb_outage()
    assert first["event"] == "adb_outage" and first["hold_sec"] == 8
    assert b.hold_remaining() > 0          # global — every lane waits on this
    assert b.parked == 0                   # never parks: parking cannot fix adb
    clock.t += 8
    assert b.note_adb_outage()["hold_sec"] == 16   # doubles while it persists
    for _ in range(10):
        b.note_adb_outage()
    assert b.log[-1]["hold_sec"] == 120    # short cap: the keepalive restarts fast
    b.note_adb_ok()
    assert b.k_adb == 0                    # recovery resets the ladder
    assert b.k == 0 and b.parked == 0      # rate-limit state untouched throughout


def test_adb_outage_requeue_is_free_of_attempt_cost():
    from qualgentbench.scheduler import Unit, WorkQueue
    q = WorkQueue([Unit(app_id="a", app_name="a", task_id="t", kind="bug_hunt",
                        label="hunt", trial=1, est_sec=60.0, est_source="default")])
    unit = q.take(None)
    q.requeue(unit, count_attempt=False)
    assert unit.attempt == 1               # unchanged from its first take
    unit = q.take(None)
    q.requeue(unit)                        # a real failure still costs one
    assert unit.attempt == 2


def test_adb_server_down_detection_matches_client_wording():
    from qualgentbench.lanes import _adb_server_down
    assert _adb_server_down(
        "adb: error: failed to get feature set: cannot connect to daemon at "
        "tcp:host.docker.internal:5037: Connection refused")
    assert _adb_server_down("failed to connect to 'host.docker.internal:5037'")
    assert not _adb_server_down("INSTALL_FAILED_INSUFFICIENT_STORAGE")
    assert not _adb_server_down("device offline")
