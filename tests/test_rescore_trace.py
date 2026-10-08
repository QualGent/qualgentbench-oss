"""An in-place rescore records how it rescored (QUA-2927): `rescored_from` keeps the
replaced verdict, void and scorer; `rescored_with` names the scorer and corpus that wrote
the new one; `rescored_at` says when. A dry run writes nothing, and a result.json written
before any of it still validates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_bugs import _obs, _transcript
from test_journey import _spec, _task

from qualgentbench import corpus, journey, rescore
from qualgentbench.result import RESCORE_TRACE_KEYS, RunResult

_RECORDED = {"completed": False, "overall": 0.0, "bugs_found": [], "bugs_present": [],
             "false_reports": 0, "false_positives": 0, "contaminated": True,
             "contamination_reasons": ["misfiled_write"], "version": "clean",
             "case_id": "case-1", "corpus_version": "aaaaaaaaaaaa"}


def _result(metrics: dict, **extra) -> dict:
    return {"task_id": "case-1~clean", "task_version": "1", "task_type": journey.TASK_TYPE,
            "agent": "a", "model": "m", "condition": "mcp", "trial": 1, "passed": False,
            "score": 0.0, "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:01:00+00:00", "wall_time_sec": 60.0, "exit_code": 0,
            "metrics": metrics, "run_id": "r1", **extra}


def _episode(tmp_path: Path, metrics: dict | None = None, **extra) -> Path:
    d = tmp_path / "case-1~clean" / "ep"
    (d / "agent").mkdir(parents=True)
    (d / "workspace").mkdir()
    (d / "agent" / "transcript.txt").write_text(_transcript(_obs("Total: 4 items")))
    (d / "workspace" / journey.FILENAME).write_text("verdict: pass\nbugs: []\n")
    (d / "result.json").write_text(json.dumps(_result(dict(metrics or _RECORDED), **extra)))
    return d


def _tasks() -> dict:
    t = _task(_spec("clean"))
    return {t.id: t}


def test_a_write_records_the_trace(tmp_path):
    d = _episode(tmp_path)
    t0 = datetime.now(UTC).replace(microsecond=0)
    status, _before, _after, v = rescore.rescore(d, _tasks(), dry_run=False)
    assert status == "rescored"
    written = json.loads((d / "result.json").read_text())

    assert written["rescored_from"] == {k: _RECORDED.get(k) for k in rescore.RESCORED_FROM_KEYS}
    assert written["rescored_from"]["contaminated"] is True          # the void it replaced
    assert written["rescored_from"]["contamination_reasons"] == ["misfiled_write"]
    assert written["rescored_from"]["scorer_version"] is None        # recorded unstamped
    assert written["rescored_with"] == {"scorer_version": journey.SCORER_VERSION,
                                        **corpus.stamp()}
    at = datetime.fromisoformat(written["rescored_at"])
    assert at.utcoffset() == timedelta(0) and t0 <= at <= datetime.now(UTC)
    # The fresh verdict carries the scorer; the recorded corpus stamp is kept (merge).
    assert written["metrics"]["scorer_version"] == journey.SCORER_VERSION
    assert written["metrics"]["corpus_version"] == "aaaaaaaaaaaa"
    assert v.metrics["scorer_version"] == journey.SCORER_VERSION

    r = RunResult.model_validate(written)
    assert (r.rescored_from, r.rescored_with, r.rescored_at) == (
        written["rescored_from"], written["rescored_with"], written["rescored_at"])
    assert {k: r.model_dump(mode="json")[k] for k in RESCORE_TRACE_KEYS} == {
        k: written[k] for k in RESCORE_TRACE_KEYS}


def test_a_second_rescore_records_the_first_ones_scorer(tmp_path):
    d = _episode(tmp_path)
    rescore.rescore(d, _tasks(), dry_run=False)
    rescore.rescore(d, _tasks(), dry_run=False)
    written = json.loads((d / "result.json").read_text())
    assert written["rescored_from"]["scorer_version"] == journey.SCORER_VERSION


@pytest.mark.parametrize("extra", [{}, {"rescored_from": {"completed": True}}])
def test_a_dry_run_writes_nothing(tmp_path, extra):
    d = _episode(tmp_path, **extra)
    before = (d / "result.json").read_bytes()
    status, *_rest, v = rescore.rescore(d, _tasks(), dry_run=True)
    assert status == "rescored" and v is not None
    assert (d / "result.json").read_bytes() == before


def test_rescore_trace_is_utc_and_names_the_scorer():
    now = datetime(2026, 1, 2, 3, 4, 5, 678, tzinfo=timezone(timedelta(hours=-5)))
    t = rescore.rescore_trace({"completed": True, "scorer_version": 1, "extra": 9}, now)
    assert t["rescored_at"] == "2026-01-02T08:04:05+00:00"
    assert t["rescored_from"]["completed"] is True and "extra" not in t["rescored_from"]
    assert set(t["rescored_from"]) == set(rescore.RESCORED_FROM_KEYS)
    assert t["rescored_with"]["scorer_version"] == journey.SCORER_VERSION


def test_an_old_result_json_still_validates_and_dumps_unchanged():
    """A run-time result.json has none of the trace; one rescored before QUA-2927 has
    the narrow `rescored_from` only. Both validate, and a dump adds no null keys."""
    plain = _result({"completed": True})
    r = RunResult.model_validate(plain)
    assert r.rescored_from is r.rescored_with is r.rescored_at is None
    assert not set(RESCORE_TRACE_KEYS) & set(r.model_dump(mode="json"))
    assert not set(RESCORE_TRACE_KEYS) & set(json.loads(r.model_dump_json()))

    narrow = {"completed": True, "overall": None, "bugs_found": [], "false_reports": 0,
              "false_positives": None}
    r = RunResult.model_validate(_result({"completed": False}, rescored_from=narrow))
    assert r.rescored_from == narrow and r.rescored_with is None
    assert set(r.model_dump(mode="json")) & set(RESCORE_TRACE_KEYS) == {"rescored_from"}


def test_the_script_prints_the_current_scorer_beside_the_recorded_ones():
    import importlib.util
    import sys
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("rescore_journey",
                                                  root / "scripts" / "rescore_journey.py")
    mod = sys.modules.get("rescore_journey")
    if mod is None:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["rescore_journey"] = mod
        spec.loader.exec_module(mod)
    cur = f"current scorer v{journey.SCORER_VERSION} · recorded "
    rs = [RunResult.model_validate(_result(m)) for m in ({}, {}, {"scorer_version": 1})]
    assert mod.scorer_line(rs[:2]) == cur + "2 unstamped"
    assert mod.scorer_line(rs[2:]) == cur + "v1"
    assert mod.scorer_line(rs) == cur + "v1, 2 unstamped"
    assert mod.scorer_line([]) == cur + "—"
