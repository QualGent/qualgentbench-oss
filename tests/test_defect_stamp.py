"""QUA-2929: every journey verdict stamps the kind, tier and class of each defect it
names (`metrics["defects"]`), so blocker recall and per-class catch are pure functions of
the episode metrics instead of a lookup in whichever corpus the reader has. Episodes
recorded before the stamp keep resolving through the corpus exactly as before."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_bugs import _call, _obs, _transcript
from test_journey import _rr

from qualgentbench import bugs, journey, rates
from qualgentbench.task import BenchmarkTask

ROOT = Path(__file__).resolve().parents[1]
_mod = importlib.util.spec_from_file_location("rescore_journey", ROOT / "scripts" / "rescore_journey.py")
rescore_journey = importlib.util.module_from_spec(_mod)
sys.modules["rescore_journey"] = rescore_journey
_mod.loader.exec_module(rescore_journey)


# ── a synthetic case document ─────────────────────────────────────────────────

SYNTH_APP = "synthstamp"
SYNTH_DOC = {
    "defects": [
        {"id": "s-blocker", "kind": "functional", "tier": "L4", "class": "persistence",
         "marker": "", "symptoms": ["not saved"]},
        {"id": "s-label", "kind": "Display", "tier": "L2", "class": "content-format",
         "marker": "Qty:", "symptoms": ["quantity"]},
        {"id": "s-noclass", "tier": "L3", "marker": "", "symptoms": ["gone"]},
    ],
    "test_cases": [
        {"id": "synthstamp-save", "name": "Save an item", "steps": ["Open the list", "Save"],
         "expected_outcome": "The item is listed.", "step_budget": 20,
         "check": {"expect": {"present": "Item saved"}}, "evidence": ["Item saved"],
         "bugs": ["s-blocker", "s-label"]},
    ],
}


def _task(spec: dict) -> BenchmarkTask:
    return BenchmarkTask(id=journey.task_id(spec["case_id"], spec["version"]), name="Case",
                         instruction="", app_file_id="", app_name="App", platform="android",
                         bundle_id="com.example", bug_spec=spec)


def _spec(active: list[str], *, meta: bool = True) -> dict:
    defects = journey.load_defects(SYNTH_DOC)
    spec = {"mode": journey.MODE, "app_id": SYNTH_APP, "case_id": "synthstamp-save",
            "version": "seeded", "name": "Save", "steps": ["Open"], "expected_outcome": "x",
            "active_bugs": active, "step_budget": 20, "expected": "FAIL",
            "blocking": "s-blocker", "blocking_texts": ["Nothing saved"], "crash_texts": [],
            "echo_texts": [], "absence_texts": [], "side": [], "defects": defects,
            "oracle": {"mode": "present", "expect": {"present": "Item saved"},
                       "evidence": ["Item saved"]},
            "truth_agrees": True, "tooling": "mcp"}
    if meta:
        spec["defect_meta"] = journey.load_defect_meta(SYNTH_DOC)
    return spec


def _found_blocker_transcript() -> str:
    body = ('verdict: fail\nbugs:\n  - step: 2\n    observed: "Nothing saved"\n'
            '    description: "the item is not saved"\n')
    return _transcript(_obs("Nothing saved"),
                       _call("Write", {"file_path": "/w/findings.yaml", "content": body}, "ok"))


# ── load_defect_meta ──────────────────────────────────────────────────────────

def test_load_defect_meta_reads_kind_tier_and_class_normalised_like_load_defects():
    meta = journey.load_defect_meta(SYNTH_DOC)
    assert meta == {
        "s-blocker": {"kind": "functional", "tier": "L4", "class": "persistence"},
        "s-label": {"kind": "display", "tier": "L2", "class": "content-format"},
        "s-noclass": {"kind": "functional", "tier": "L3", "class": None},
    }
    plain = journey.load_defects(SYNTH_DOC)
    assert all((m["kind"], m["tier"]) == (plain[i]["kind"], plain[i]["tier"])
               for i, m in meta.items())
    assert journey.load_defect_meta({}) == {} and journey.load_defect_meta(None) == {}


def test_class_never_reaches_the_scorer_side_defects_mapping():
    """The stamp reads `class`; the matcher's `defects` mapping still does not carry it."""
    assert all("class" not in d for d in journey.load_defects(SYNTH_DOC).values())


# ── the verdict stamp ─────────────────────────────────────────────────────────

def test_the_verdict_stamps_every_present_id_with_kind_tier_and_class():
    v = journey.journey_verdict(_found_blocker_transcript(), "m",
                                _task(_spec(["s-blocker", "s-label"])))
    m = v.metrics
    assert m["bugs_found"] == ["s-blocker"]
    assert m["defects"] == {
        "s-blocker": {"kind": "functional", "tier": "L4", "class": "persistence"},
        "s-label": {"kind": "display", "tier": "L2", "class": "content-format"},
    }
    assert list(m["defects"]) == m["bugs_present"]
    json.dumps(m)                                    # serialisable as result.json writes it


def test_an_unknown_id_is_stamped_none_not_dropped():
    v = journey.journey_verdict(_found_blocker_transcript(), "m",
                                _task(_spec(["s-blocker", "ghost-defect"])))
    assert v.metrics["defects"]["ghost-defect"] is None
    assert v.metrics["defects"]["s-blocker"]["class"] == "persistence"


def test_a_clean_episode_stamps_nothing():
    spec = _spec([])
    spec.update(version="clean", expected="PASS", blocking=None, blocking_texts=[])
    v = journey.journey_verdict(_transcript(_obs("Item saved"), _call(
        "Write", {"file_path": "/w/findings.yaml", "content": "verdict: pass\nbugs: []\n"}, "ok")),
        "m", _task(spec))
    assert v.metrics["defects"] == {}


def test_a_spec_without_defect_meta_falls_back_to_kind_and_tier_with_no_class():
    """A task built by a builder that does not pass `defect_meta` (CreateBench's) still
    stamps kind and tier from the scorer's own `defects` mapping."""
    v = journey.journey_verdict(_found_blocker_transcript(), "m",
                                _task(_spec(["s-blocker"], meta=False)))
    assert v.metrics["defects"] == {"s-blocker": {"kind": "functional", "tier": "L4", "class": None}}


def test_the_stamp_changes_no_score():
    for active in (["s-blocker"], ["s-blocker", "s-label"]):
        a = journey.journey_verdict(_found_blocker_transcript(), "m", _task(_spec(active)))
        b = journey.journey_verdict(_found_blocker_transcript(), "m",
                                    _task(_spec(active, meta=False)))
        assert ({k: v for k, v in a.metrics.items() if k != "defects"}
                == {k: v for k, v in b.metrics.items() if k != "defects"})
        assert (a.passed, a.score, a.weighted_score, a.criteria, a.failure_reason) == \
            (b.passed, b.score, b.weighted_score, b.criteria, b.failure_reason)


def test_public_tasks_carry_defect_meta_and_their_verdicts_stamp_class():
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == "medtimer")
    tasks = journey.journey_tasks(suite)
    meta = journey.load_defect_meta(journey.load_cases("medtimer"))
    seeded = [t for t in tasks if t.bug_spec["version"] == "seeded"]
    assert seeded
    for t in seeded:
        assert t.bug_spec["defect_meta"] == meta
        v = journey.journey_verdict(_transcript(_obs("x"), _call(
            "Write", {"file_path": "/w/findings.yaml", "content": "verdict: pass\nbugs: []\n"},
            "ok")), "m", t)
        stamp = v.metrics["defects"]
        assert list(stamp) == t.bug_spec["active_bugs"]
        for bid, d in stamp.items():
            assert d == meta[bid] and d["class"] in journey.DEFECT_CLASSES, bid


def test_a_heldout_case_document_is_stamped_too(tmp_path, monkeypatch):
    split = tmp_path / "split"
    (split / "test-cases").mkdir(parents=True)
    import yaml
    (split / "test-cases" / f"{SYNTH_APP}.yaml").write_text(yaml.safe_dump(SYNTH_DOC))
    monkeypatch.setenv("QGB_HELDOUT_DIR", str(split))
    tasks = journey.journey_tasks({"app": {"id": SYNTH_APP, "name": "Synth"}})
    seeded = next(t for t in tasks if t.bug_spec["version"] == "seeded")
    assert seeded.bug_spec["heldout"] is True
    v = journey.journey_verdict(_found_blocker_transcript(), "m", seeded)
    assert v.metrics["defects"] == {
        "s-blocker": {"kind": "functional", "tier": "L4", "class": "persistence"},
        "s-label": {"kind": "display", "tier": "L2", "class": "content-format"},
    }


# ── blocker recall reads the stamp ────────────────────────────────────────────

def _seeded(present, found, app_id="no-such-app", defects=None) -> dict:
    row = {"version": "seeded", "bugs_present": present, "bugs_found": found, "app_id": app_id,
           "false_reports": 0}
    if defects is not None:
        row["defects"] = defects
    return row


STAMP = {"b4": {"kind": "functional", "tier": "L4", "class": "crash"},
         "b1": {"kind": "functional", "tier": "L1", "class": "layout"},
         "dl": {"kind": "display", "tier": "L4", "class": "content-format"}}


def test_blocker_unresolved_counts_what_blocker_recall_leaves_out():
    rows = [_seeded(["b4", "dl", "gone"], ["b4"], defects={**STAMP, "gone": None}),
            _seeded(["b1", "unstamped"], [], defects=STAMP),
            {"version": "clean", "bugs_present": [], "bugs_found": [], "false_reports": 0}]
    r = rates.blocker_recall(rows)
    assert (r.k, r.n) == (1, 1)
    assert rates.blocker_unresolved(rows) == 2           # `gone` (stamped None), `unstamped`
    assert rates.blocker_unresolved([]) == 0


def test_a_stamped_none_is_not_looked_up_again_but_an_unstamped_id_is():
    lookup = {"gone": {"kind": "functional", "tier": "L4"},
              "unstamped": {"kind": "functional", "tier": "L3"}}
    rows = [_seeded(["gone", "unstamped"], ["unstamped"], defects={"gone": None})]
    r = rates.blocker_recall(rows, defects=lambda app, bug: lookup.get(bug))
    assert (r.k, r.n) == (1, 1)                         # only `unstamped` resolves
    assert rates.blocker_unresolved(rows, defects=lambda app, bug: lookup.get(bug)) == 1


def test_board_rows_carry_blocker_unresolved_and_read_the_stamp_without_a_corpus():
    rs = [_rr("c~seeded", {"completed": True, "steps": 1, "total_tokens": 1,
                           **_seeded(["b4", "gone"], ["b4"], defects={**STAMP, "gone": None})})]
    [row] = journey.summary(rs)
    assert (row["blocker_found"], row["blocker_n"]) == (1, 1)    # no case file for the app
    assert row["blocker_unresolved"] == 1
    assert "(1 unresolved)" in journey.rates_cells(row)["blocker"]
    # An unstamped episode of an app with no case file: unresolved, as it always was
    # dropped — now counted.
    rs[0] = rs[0].model_copy(update={"metrics": {**rs[0].metrics, "defects": None}})
    [row] = journey.summary(rs)
    assert row["blocker_n"] == 0 and row["blocker_unresolved"] == 2
    assert row["blocker_recall"] is None


def test_an_unstamped_public_episode_resolves_through_the_corpus_as_before():
    defects = journey.load_defect_meta(journey.load_cases("medtimer"))
    top = next(d for d, m in defects.items()
               if m["kind"] == "functional" and m["tier"].upper() in journey.BLOCKER_TIERS)
    rs = [_rr("c~seeded", {"completed": True, "steps": 1, "total_tokens": 1,
                           **_seeded([top], [top], app_id="medtimer")})]
    [row] = journey.summary(rs)
    assert (row["blocker_found"], row["blocker_n"], row["blocker_unresolved"]) == (1, 1, 0)
    # The same episode with the id stamped None: the stamp wins over the corpus.
    rs[0] = rs[0].model_copy(update={"metrics": {**rs[0].metrics, "defects": {top: None}}})
    [row] = journey.summary(rs)
    assert (row["blocker_n"], row["blocker_unresolved"]) == (0, 1)


# ── rescore re-stamps ─────────────────────────────────────────────────────────

def _saved_episode(tmp_path: Path, metrics: dict) -> Path:
    run_dir = tmp_path / "episode"
    (run_dir / "agent").mkdir(parents=True)
    (run_dir / "workspace").mkdir()
    transcript = _found_blocker_transcript()
    (run_dir / "agent" / "transcript.txt").write_text(transcript)
    (run_dir / "result.json").write_text(json.dumps(
        {"task_type": journey.TASK_TYPE, "task_id": "synthstamp-save~seeded", "condition": "mcp",
         "model": "m", "metrics": metrics}))
    return run_dir


def test_a_rescore_of_an_unstamped_episode_adds_only_the_stamp(tmp_path):
    fresh = _task(_spec(["s-blocker", "s-label"]))
    live = journey.journey_verdict(_found_blocker_transcript(), "m", fresh).metrics
    # A pre-stamp episode as the runner saved it (it adds `failure_class` after scoring).
    recorded = {**{k: v for k, v in live.items() if k != "defects"}, "failure_class": None}
    run_dir = _saved_episode(tmp_path, recorded)
    before = (run_dir / "result.json").read_bytes()
    status, b, a, v = rescore_journey.rescore(run_dir, {fresh.id: fresh}, dry_run=True)
    assert status == "rescored" and b == a
    added = {k for k in v.metrics if k not in recorded}
    assert added == {"defects"}
    assert {k: v.metrics[k] for k in recorded} == recorded           # no verdict field moved
    assert v.metrics["defects"]["s-blocker"]["class"] == "persistence"
    assert (run_dir / "result.json").read_bytes() == before


def test_unknown_recorded_ids_names_ids_the_current_corpus_lost():
    meta = journey.load_defect_meta(SYNTH_DOC)
    old = {"bugs_present": ["s-blocker", "retired"], "bugs_found": ["retired", "also-gone"]}
    assert rescore_journey.unknown_recorded_ids(old, meta) == ["retired", "also-gone"]
    assert rescore_journey.unknown_recorded_ids({"bugs_present": ["s-label"]}, meta) == []
    assert rescore_journey.unknown_recorded_ids({}, meta) == []


@pytest.mark.parametrize("field", ["bugs_present", "bugs_found"])
def test_unknown_recorded_ids_reads_both_lists(field):
    assert rescore_journey.unknown_recorded_ids({field: ["x"]}, {}) == ["x"]
