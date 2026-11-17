"""Memory-experiment metrics (QUA-3489): wasted steps, stage, proposals, trap harm,
Qualock attribution, memory health and the per-condition aggregation. Fixture episodes
and fixture snapshots only: no device, no model."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from test_agent_dump import device  # noqa: F401 — the fake-device fixture
from test_blind_arm import _arm_episode
from test_journey import _spec, _task
from test_memory_injection import memory_root  # noqa: F401 — the memory-root fixture
from test_rescore_trace import _episode

from qualgentbench import bugs, cli, journey, rescore
from qualgentbench import episode_runner as er
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.memory import arm as marm
from qualgentbench.memory import format as fmt
from qualgentbench.memory import metrics as mm
from qualgentbench.memory import snapshot as snap
from qualgentbench.memory.cli import memory as memory_cli

FIXTURE = Path(__file__).parent / "fixtures" / "memory" / "good"
ORG, APP = "samplecorp", "sampleapp"
COND = "mcp+mem-dynamic-small-none"


# ── wasted steps ──────────────────────────────────────────────────────────────

def test_steps_over_ref_only_on_completed_episodes():
    assert mm.steps_over_ref(True, 21, 7) == 3.0
    assert mm.steps_over_ref(True, 10, 3) == 3.3333
    for completed in (False, None):
        assert mm.steps_over_ref(completed, 21, 7) is None
    assert mm.steps_over_ref(True, 0, 7) is None
    assert mm.steps_over_ref(True, 21, None) is None
    assert mm.steps_over_ref(True, None, 7) is None


def test_journey_tasks_carry_the_reference_route_length():
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == "markor")
    doc = journey.load_cases("markor")
    case = next(c for c in doc["test_cases"] if c["id"] == "markor-todo-set-priority")
    tasks = [t for t in journey.journey_tasks(suite)
             if t.bug_spec["case_id"] == "markor-todo-set-priority"]
    assert {t.bug_spec["version"] for t in tasks} == {"clean", "seeded"}
    for t in tasks:
        assert t.bug_spec["ref_steps"] == len(case["check"]["steps"]) == 7
        # Harness-only: the agent's brief never carries it.
        assert "ref_steps" not in journey.brief(t, "emulator-5554", "mcp")


def test_episode_fields_on_a_plain_journey_episode_add_only_the_steps_pair():
    m = {"completed": True, "steps": 14, "app_id": "app", "case_id": "c"}
    assert mm.episode_fields(m, None, None, ref_steps=7, traps=[], attribution={}) == {
        "ref_steps": 7, "steps_over_ref": 2.0}
    assert mm.episode_fields({**m, "completed": False}, None, None, ref_steps=7, traps=[],
                             attribution={}) == {"ref_steps": 7, "steps_over_ref": None}


# ── stage ─────────────────────────────────────────────────────────────────────

def test_arm_stage_round_trips_and_is_validated(tmp_path):
    a = marm.MemoryArm("static", "small", stage="run-2")
    assert a.as_dict()["stage"] == "run-2"
    assert marm.MemoryArm.from_dict(a.as_dict()).stage == "run-2"
    # An unstaged arm keeps the shape it had before stages (plan.json, provenance).
    assert "stage" not in marm.MemoryArm("static").as_dict()
    with pytest.raises(marm.MemoryArmError, match="stage"):
        marm.MemoryArm("static", stage="run 2; rm")
    assert marm.MemoryArm(stage="run-1").recorded and not marm.MemoryArm().recorded
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({"mode": "none", "stage": "run-3"}))
    assert marm.MemoryArm.load(cfg).stage == "run-3"


def test_cli_keeps_a_stage_only_arm_for_the_no_memory_condition(tmp_path):
    cfg = tmp_path / "m.json"
    cfg.write_text(json.dumps({"mode": "none", "stage": "run-2"}))
    arm = cli._gate_memory("native", "journey", None, memory_config=cfg, values={})
    assert arm is not None and not arm.enabled and arm.stage == "run-2"
    assert arm.condition("mcp") is None              # the condition stays `mcp`
    cli._gate_memory_apps(arm, ["any-app"])          # nothing to check without memory
    cfg.write_text(json.dumps({"mode": "none"}))
    assert cli._gate_memory("codex-cli", "journey", None, memory_config=cfg,
                            values={}) is None
    with pytest.raises(click.ClickException):
        cfg.write_text(json.dumps({"mode": "none", "stage": "../x"}))
        cli._gate_memory("codex-cli", "journey", None, memory_config=cfg, values={})


def test_episode_fields_record_the_stage_on_every_arm():
    out = mm.episode_fields({"completed": None}, None, None, stage="run-2", traps=[],
                            attribution={})
    assert out["memory_stage"] == "run-2"
    # A rescore passes no stage: the recorded one stands.
    out = mm.episode_fields({"memory_stage": "run-4"}, None, None, traps=[], attribution={})
    assert out["memory_stage"] == "run-4"


# ── proposals ─────────────────────────────────────────────────────────────────

def test_proposal_fields(tmp_path):
    assert mm.proposal_fields(tmp_path) == {
        "memory_proposals_count": 0, "memory_proposals_updates": [],
        "memory_proposals_contradicts": []}
    (tmp_path / marm.PROPOSALS_FILENAME).write_text(
        "- type: nav\n  text: Archive is in the overflow menu.\n  updates: nav-003\n"
        "- type: flaky\n  text: Search is slow.\n  contradicts: flaky-001\n"
        "- type: note\n  text: Saves on exit.\n  contradicts: nav-003\n")
    f = mm.proposal_fields(tmp_path)
    assert f["memory_proposals_count"] == 3
    assert f["memory_proposals_updates"] == ["nav-003"]
    assert f["memory_proposals_contradicts"] == ["flaky-001", "nav-003"]
    (tmp_path / marm.PROPOSALS_FILENAME).write_text("proposals:\n  - type: nav\n    text: x\n")
    assert mm.proposal_fields(tmp_path)["memory_proposals_count"] == 1
    (tmp_path / marm.PROPOSALS_FILENAME).write_text("- type: nav\n  text: [unclosed\n")
    f = mm.proposal_fields(tmp_path)
    assert f["memory_proposals_count"] is None and f["memory_proposals_error"]
    (tmp_path / marm.PROPOSALS_FILENAME).write_text("just a sentence\n")
    assert mm.proposal_fields(tmp_path)["memory_proposals_count"] is None


def test_proposals_are_counted_on_memory_episodes_only(tmp_path):
    (tmp_path / marm.PROPOSALS_FILENAME).write_text("- type: nav\n  text: x\n")
    base = {"completed": True, "steps": 3}
    plain = mm.episode_fields(base, None, tmp_path, traps=[], attribution={})
    assert "memory_proposals_count" not in plain
    mem = mm.episode_fields({**base, "memory_condition": COND}, {}, tmp_path, traps=[],
                            attribution={})
    assert mem["memory_proposals_count"] == 1


# ── traps ─────────────────────────────────────────────────────────────────────

TRAPS_YAML = """\
traps:
  - id: t-wontfix
    app: app
    stage: run-2
    entry_id: accepted-901
    effect: missed_bug
    bug: crash-bug
    cases: [case-1]
  - id: t-moved
    app: app
    effect: false_report
    markers: [left drawer]
  - id: t-nav
    app: app
    effect: not_completed
    cases: [case-2]
"""


def _traps(tmp_path) -> list[mm.Trap]:
    d = tmp_path / "traps"
    d.mkdir()
    (d / "a.yaml").write_text(TRAPS_YAML)
    return mm.load_traps(d, tmp_path / "missing")


def test_load_traps_validates(tmp_path):
    traps = _traps(tmp_path)
    assert [t.id for t in traps] == ["t-wontfix", "t-moved", "t-nav"]
    assert mm.load_traps(tmp_path / "nowhere") == []
    bad = tmp_path / "bad.yaml"
    for body, msg in (("id: x\napp: a\neffect: missed_bug\n", "needs `bug`"),
                      ("id: x\napp: a\neffect: false_report\n", "needs `markers`"),
                      ("id: x\napp: a\neffect: explode\n", "effect must be"),
                      ("app: a\neffect: not_completed\n", "missing `id`")):
        bad.write_text(body)
        with pytest.raises(mm.MetricsInputError, match=msg):
            mm.load_traps(bad)
    (tmp_path / "traps" / "b.yaml").write_text("id: t-nav\napp: app\neffect: not_completed\n")
    with pytest.raises(mm.MetricsInputError, match="duplicate"):
        mm.load_traps(tmp_path / "traps")


def _seeded(found: bool, **kw) -> dict:
    return {"app_id": "app", "case_id": "case-1", "version": "seeded",
            "bugs_present": ["crash-bug"], "bugs_found": ["crash-bug"] if found else [],
            "bugs_missed": [] if found else ["crash-bug"], "completed": True,
            "reports": [], **kw}


def test_missed_bug_trap_is_harm_only_when_exposed(tmp_path):
    traps = _traps(tmp_path)
    prov = {"included": ["nav-001", "accepted-901"]}
    exposed = {"memory_condition": COND, "memory_stage": "run-2"}
    f = mm.trap_fields(_seeded(False, **exposed), prov, traps)
    assert f["memory_traps"]["t-wontfix"] == {"effect": "missed_bug", "exposed": True,
                                              "harmed": True}
    assert f["memory_trap_harm"] is True
    assert mm.trap_fields(_seeded(True, **exposed), prov, traps)["memory_trap_harm"] is False
    # The no-memory arm on the same episode: measured (the base rate), not exposed.
    f = mm.trap_fields(_seeded(False), None, traps)
    assert f["memory_traps"]["t-wontfix"] == {"effect": "missed_bug", "exposed": False,
                                              "harmed": True}
    assert f["memory_trap_harm"] is None
    # Before the trap's stage, or with the entry dropped by the budget: not exposed.
    early = mm.trap_fields(_seeded(False, memory_condition=COND, memory_stage="run-1"),
                           prov, traps)
    assert early["memory_traps"]["t-wontfix"]["exposed"] is False
    dropped = mm.trap_fields(_seeded(False, **exposed), {"included": ["nav-001"]}, traps)
    assert dropped["memory_traps"]["t-wontfix"]["exposed"] is False
    # The clean version does not have the hidden bug active: the trap does not apply.
    clean = {**_seeded(True), "version": "clean", "bugs_present": [], "bugs_found": []}
    assert "t-wontfix" not in mm.trap_fields(clean, prov, traps).get("memory_traps", {})


def test_false_report_trap_reads_unmatched_reports_on_word_boundaries(tmp_path):
    traps = [t for t in _traps(tmp_path) if t.id == "t-moved"]
    base = {"app_id": "app", "case_id": "case-9", "memory_condition": COND}
    rep = {"screen": "Home", "observed": "no settings", "expected": "",
           "description": "Settings are missing from the Left Drawer.", "matched": None}
    assert mm.trap_fields({**base, "reports": [rep]}, {}, traps)["memory_trap_harm"] is True
    # A report that matched a real defect is not the trap's false report.
    assert mm.trap_fields({**base, "reports": [{**rep, "matched": "x"}]}, {},
                          traps)["memory_trap_harm"] is False
    assert mm.trap_fields({**base, "reports": [{**rep, "description": "leftdrawers"}]}, {},
                          traps)["memory_trap_harm"] is False


def test_not_completed_trap_and_unscored_completion(tmp_path):
    traps = [t for t in _traps(tmp_path) if t.id == "t-nav"]
    base = {"app_id": "app", "case_id": "case-2", "memory_condition": COND}
    assert mm.trap_fields({**base, "completed": False}, {}, traps)["memory_trap_harm"] is True
    unscored = mm.trap_fields({**base, "completed": None}, {}, traps)
    assert unscored["memory_traps"]["t-nav"]["harmed"] is None
    assert unscored["memory_trap_harm"] is None
    assert mm.trap_fields({**base, "case_id": "other"}, {}, traps) == {}


# ── attribution ───────────────────────────────────────────────────────────────

def test_attribution_table_and_fields(tmp_path):
    d = tmp_path / "qualock" / "app"
    d.mkdir(parents=True)
    (d / mm.ATTRIBUTION_FILENAME).write_text(
        "app: app\ntargets:\n"
        "  crash-bug: {qualock_diff_exposes: true, qualock_hunt_found: false, pair: v1..v2a}\n"
        "  other-bug: {qualock_hunt_found: true}\n")
    table = mm.load_attribution(tmp_path / "qualock")
    assert table[("app", "crash-bug")] == {"qualock_diff_exposes": True,
                                           "qualock_hunt_found": False, "pair": "v1..v2a"}
    assert table[("app", "other-bug")]["qualock_diff_exposes"] is None   # unmeasured
    f = mm.attribution_fields(_seeded(True), table)
    assert f == {"memory_attribution": {"crash-bug": {"qualock_diff_exposes": True,
                                                      "qualock_hunt_found": False,
                                                      "found": True}},
                 "qualock_diff_exposes": True, "qualock_hunt_found": False}
    assert mm.attribution_fields({**_seeded(True), "bugs_present": ["x"]}, table) == {}
    two = mm.attribution_fields({**_seeded(False), "bugs_present": ["crash-bug", "other-bug"]},
                                table)
    assert two["qualock_hunt_found"] is True and two["qualock_diff_exposes"] is True
    (d / mm.ATTRIBUTION_FILENAME).write_text("targets: {}\n")
    with pytest.raises(mm.MetricsInputError):
        mm.load_attribution(tmp_path / "qualock")


def test_packaged_inputs_absent_means_no_fields():
    # Until the trap and Qualock tickets ship their data, nothing is added.
    out = mm.episode_fields(_seeded(False), None, None, ref_steps=3)
    assert set(out) <= {"ref_steps", "steps_over_ref", "memory_traps", "memory_trap_harm",
                        "memory_attribution", "qualock_diff_exposes", "qualock_hunt_found"}
    assert set(out) <= set(mm.EPISODE_METRICS)


# ── rescore ───────────────────────────────────────────────────────────────────

def test_rescore_fills_the_new_fields_without_moving_the_verdict(tmp_path, monkeypatch):
    spec = {**_spec("clean"), "ref_steps": 2}
    task = _task(spec)
    tasks = {task.id: task}
    d1 = _episode(tmp_path / "a")
    d2 = _episode(tmp_path / "b")
    monkeypatch.setattr(mm, "episode_fields", lambda *a, **k: {})
    _s, _b, _a, without = rescore.rescore(d1, tasks, dry_run=False)
    monkeypatch.undo()
    _s, _b, _a, with_fields = rescore.rescore(d2, tasks, dry_run=False)
    for k in rescore.VERDICT_FIELDS:
        assert getattr(with_fields, k) == getattr(without, k)
    assert {k: v for k, v in with_fields.metrics.items()
            if k not in mm.EPISODE_METRICS} == without.metrics
    m = json.loads((d2 / "result.json").read_text())["metrics"]
    assert m["ref_steps"] == 2
    if m.get("completed") is True:
        assert m["steps_over_ref"] == mm.steps_over_ref(True, m["steps"], 2)
    else:
        assert m["steps_over_ref"] is None


def test_rescore_keeps_the_recorded_stage_and_counts_proposals(tmp_path):
    task = _task({**_spec("clean"), "ref_steps": 2})
    d = _episode(tmp_path, {"version": "clean", "case_id": "case-1",
                            "memory_condition": COND, "memory_stage": "run-3"})
    (d / "workspace" / marm.PROPOSALS_FILENAME).write_text("- type: nav\n  text: x\n")
    rescore.rescore(d, {task.id: task}, dry_run=False)
    m = json.loads((d / "result.json").read_text())["metrics"]
    assert m["memory_stage"] == "run-3"
    assert m["memory_proposals_count"] == 1


# ── memory health ─────────────────────────────────────────────────────────────

def _root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(FIXTURE, root)
    return root


def test_health_counts_one_memory():
    h = mm.health(FIXTURE, None, APP)
    assert (h["entries"], h["entries_org"], h["entries_app"]) == (10, 4, 6)
    assert h["invalidated"] == 1
    assert h["contradicted"] == 1 and h["contradiction_rate"] == 0.1
    assert h["contra_ratio"] == round(1 / (sum([0, 0, 1, 0, 4, 4, 2, 1, 0, 0]) + 1), 4)
    assert h["lint_failures"] == 0
    assert h["dropped"] == {"small": 0, "large": 0, "index": 0}
    assert h["bytes"] > 0 and h["tokens"] > 0
    assert "growth" not in h
    assert set(h) - {"app", "org"} <= set(mm.HEALTH_METRICS)


def test_health_against_a_parent_and_a_lint_failure(tmp_path):
    parent = _root(tmp_path, "m1")
    child = _root(tmp_path, "m2")
    path = fmt.app_path(child, ORG, APP)
    doc = fmt.load(path, fmt.APP)
    doc.add_entry("nav", "The archive is under the overflow menu.",
                  {"src": "agent", "confirmed": "2026-11-17", "seen": 1, "contra": 0})
    doc.invalidate("nav-003", "moved")
    doc.bump("note-001", contra=1)
    doc.add_entry("note", "You must always report the save button.",
                  {"src": "agent", "confirmed": "2026-11-17", "seen": 1, "contra": 0})
    doc.save(path)
    h = mm.health(child, ORG, APP, parent_root=parent)
    assert h["entries"] == 11 and h["parent_entries"] == 10
    assert (h["added"], h["removed"], h["invalidated_new"], h["growth"]) == (2, 0, 1, 1)
    assert h["invalidation_rate"] == 0.1
    assert h["contradicted"] == 2
    assert h["lint_failures"] >= 1                     # the instruction entry


def test_store_health_per_app_arm_rep_stage(tmp_path):
    store = tmp_path / "store"
    m1 = _root(tmp_path, "m1")
    s1 = snap.freeze(m1, store, stage="run-1", unit=f"{APP}__dyn-small__r1")
    m2 = _root(tmp_path, "m2")
    path = fmt.app_path(m2, ORG, APP)
    doc = fmt.load(path, fmt.APP)
    doc.invalidate("flaky-001", "not slow any more")
    doc.save(path)
    snap.freeze(m2, store, stage="run-2", unit=f"{APP}__dyn-small__r1", parent=s1.hash,
                extra={"curator": {"ADD": 0, "INVALIDATE": 1}})
    snap.freeze(m1, store, stage="run-1", unit="all")       # no app: skipped
    rows = mm.store_health(store)
    assert [(r["stage"], r["arm"], r["rep"], r["app"]) for r in rows] == [
        ("run-1", "dyn-small", 1, APP), ("run-2", "dyn-small", 1, APP)]
    assert "growth" not in rows[0]
    assert rows[1]["growth"] == -1 and rows[1]["invalidated_new"] == 1
    assert rows[1]["invalidation_rate"] == 0.1
    assert rows[1]["curator"] == {"ADD": 0, "INVALIDATE": 1}
    with pytest.raises(mm.MetricsInputError):
        mm.unit_health(store, "run-1", "all")

    out = CliRunner().invoke(memory_cli, ["health", str(store), "--json"])
    assert out.exit_code == 0, out.output
    assert len(json.loads(out.output)) == 2


# ── aggregation ───────────────────────────────────────────────────────────────

def _r(condition, stage, metrics, **kw):
    m = {"memory_stage": stage, "app_id": "app", **metrics}
    return {"task_type": journey.TASK_TYPE, "condition": condition, "model": "m",
            "task_id": kw.pop("task_id", "case-1~clean"), "trial": 1,
            "started_at": kw.pop("started_at", "2026-11-17T00:00:00"),
            "artifact_dir": kw.pop("artifact_dir", "c/ep"), "provenance": {},
            "metrics": m, **kw}


def test_aggregate_rows_by_condition_and_stage():
    trap_hit = {"t": {"effect": "missed_bug", "exposed": True, "harmed": True}}
    trap_base = {"t": {"effect": "missed_bug", "exposed": False, "harmed": False}}
    attr = {"crash-bug": {"qualock_diff_exposes": False, "qualock_hunt_found": False,
                          "found": True}}
    results = [
        _r(COND, "run-2", {"version": "clean", "completed": True, "steps_over_ref": 1.5,
                           "false_reports": 0, "memory_tokens_injected": 400,
                           "memory_proposals_count": 2, "memory_proposals_sha256": "sha256:a"}),
        _r(COND, "run-2", {"version": "clean", "completed": False, "false_reports": 1,
                           "memory_tokens_injected": 600}, artifact_dir="c/ep2"),
        _r(COND, "run-2", {"version": "seeded", "completed": True, "steps_over_ref": 2.5,
                           "bugs_present": ["crash-bug"], "bugs_found": [],
                           "memory_traps": trap_hit, "memory_attribution": attr}),
        _r(COND, "run-2", {"version": "clean", "env_failure": True}),
        _r("mcp", "run-2", {"version": "seeded", "completed": None,
                            "bugs_present": ["crash-bug"], "bugs_found": ["crash-bug"],
                            "memory_traps": trap_base}),
        _r("mcp", "run-1", {"version": "clean", "completed": True, "steps_over_ref": 1.0}),
        {"task_type": "bug_hunt", "condition": "mcp", "metrics": {}},
    ]
    rows = mm.aggregate(results, curation={"sha256:a": 1})
    assert [(r["condition"], r["stage"]) for r in rows] == [
        ("mcp", "run-1"), ("mcp", "run-2"), (COND, "run-2")]
    mem = rows[2]
    assert mem["memory_internal"] is True
    assert (mem["episodes"], mem["excluded"]) == (3, 1)
    assert mem["completion"] == round(2 / 3, 4)
    assert mem["steps_over_ref_mean"] == 2.0 and mem["steps_over_ref_n"] == 2
    assert mem["false_alarm_rate"] == 0.5
    assert (mem["bugs_present"], mem["bugs_found"], mem["recall"]) == (1, 0, 0.0)
    assert mem["memory_tokens_injected_mean"] == 500
    assert (mem["proposals_count"], mem["proposals_accepted"]) == (2, 1)
    assert (mem["trap_exposed"], mem["trap_harmed"], mem["trap_harm_rate"]) == (1, 1, 1.0)
    assert (mem["pair_targets"], mem["pair_found"], mem["pair_found_unaided"]) == (1, 1, 1)
    base = rows[1]
    assert base["memory_internal"] is False
    assert (base["trap_unexposed"], base["trap_base_rate"], base["trap_harm_rate"]) == (1, 0.0, None)
    assert base["completion"] is None and base["recall"] == 1.0
    assert base["proposals_accepted"] is None

    by_app = mm.aggregate(results, by_app=True)
    assert all(r["app"] == "app" for r in by_app)


def test_dedupe_keeps_stages_apart():
    a = _r(COND, "run-1", {}, started_at="2026-11-17T01:00:00")
    b = _r(COND, "run-2", {}, started_at="2026-11-17T02:00:00")
    c = _r(COND, "run-2", {}, started_at="2026-11-17T03:00:00")
    kept = mm.dedupe_staged([a, b, c])
    assert sorted(r["started_at"] for r in kept) == ["2026-11-17T01:00:00",
                                                     "2026-11-17T03:00:00"]


def test_curation_log_formats(tmp_path):
    assert mm.curation_counts([
        {"episode": "c/ep", "op": "ADD"}, {"episode": "c/ep", "op": "NOOP"},
        {"episode_id": "abc", "accepted": 3}, {"op": "ADD"}]) == {"c/ep": 1, "abc": 3}
    p = tmp_path / "log.jsonl"
    p.write_text('{"episode": "c/ep", "op": "UPDATE"}\n{"episode": "c/ep", "op": "INVALIDATE"}\n')
    assert mm.load_curation_log(p) == {"c/ep": 2}
    p.write_text("records:\n  - {proposals_sha256: 'sha256:a', accepted: 1}\n")
    assert mm.load_curation_log(p) == {"sha256:a": 1}


def test_metrics_cli_over_a_runs_dir(tmp_path):
    runs = tmp_path / "runs"
    ep = runs / "case-1" / "ep"
    ep.mkdir(parents=True)
    r = _r(COND, "run-2", {"version": "clean", "completed": True, "steps_over_ref": 1.5})
    r.update({"task_version": "1", "agent": "a", "passed": True, "score": 1.0,
              "ended_at": "2026-11-17T00:01:00", "wall_time_sec": 60.0, "exit_code": 0,
              "run_id": "r1"})
    (ep / "result.json").write_text(json.dumps(r))
    out = CliRunner().invoke(memory_cli, ["metrics", str(runs), "--json"])
    assert out.exit_code == 0, out.output
    rows = json.loads(out.output)
    assert rows[0]["condition"] == COND and rows[0]["steps_over_ref_mean"] == 1.5
    out = CliRunner().invoke(memory_cli, ["metrics", str(runs), "--run", "other", "--json"])
    assert json.loads(out.output) == []
    assert CliRunner().invoke(memory_cli, ["metrics", str(runs)]).exit_code == 0


# ── run time: the episode runner writes the same fields ───────────────────────

async def _journey_run(monkeypatch, tmp_path, arm, proposals=None):
    task, opts, capture = _arm_episode(monkeypatch, tmp_path, CodexCliAdapter, "clean", "mcp")
    opts.task_type = journey.TASK_TYPE
    task.bug_spec["ref_steps"] = 1
    opts.memory = arm
    if proposals is not None:
        inner = capture.run

        async def run(instruction, context):
            out = await inner(instruction, context)
            (context.workspace_dir / marm.PROPOSALS_FILENAME).write_text(proposals)
            return out
        capture.run = run
    return await er.run_episode(task, opts)


async def test_run_time_memory_episode_records_stage_and_proposals(device, monkeypatch,  # noqa: F811
                                                                    tmp_path, memory_root):  # noqa: F811
    device(u2=False)
    arm = marm.MemoryArm("static", "small", snapshot=memory_root, stage="run-2")
    result = await _journey_run(monkeypatch, tmp_path, arm,
                                "- type: nav\n  text: x\n  contradicts: nav-001\n")
    m = result.metrics
    assert m["memory_stage"] == "run-2"
    assert m["memory_proposals_count"] == 1
    assert m["memory_proposals_contradicts"] == ["nav-001"]
    assert m["ref_steps"] == 1 and "steps_over_ref" in m
    assert result.provenance["memory"]["stage"] == "run-2"


async def test_run_time_no_memory_arm_records_only_the_stage(device, monkeypatch,  # noqa: F811
                                                             tmp_path):
    device(u2=False)
    result = await _journey_run(monkeypatch, tmp_path, marm.MemoryArm(stage="run-2"))
    m = result.metrics
    assert result.condition == "mcp"
    assert m["memory_stage"] == "run-2"
    assert "memory_condition" not in m and "memory_proposals_count" not in m
    assert "memory" not in result.provenance
