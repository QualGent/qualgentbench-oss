"""Re-labelling a rescored run under the corpus it was rescored against (QUA-3553).

A synthetic corpus only: one app, one case. The "scoring-only change" adds a symptom
wording to a defect; the refusals change what the agent saw (the steps), the build (the
APK hash) or the oracle (the check)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_bugs import _obs, _transcript
from test_journey import _spec, _task

from qualgentbench import corpus, journey, relabel, view
from qualgentbench.result import RunResult

CASE_DOC = {
    "app": "demo",
    "apk": {"repo": "r", "filename": "demo.apk", "sha256": "a" * 64},
    "defects": [{"id": "avg-bug", "kind": "display", "class": "content-format",
                 "marker": "Avg:", "symptoms": ["average"]}],
    "test_cases": [{"id": "case-1", "name": "Case", "steps": ["Open the list", "Read the total"],
                    "expected_outcome": "The total matches.", "step_budget": 30,
                    "check": {"steps": ["launch"], "expect": {"present": "Total: 4 items"}},
                    "bugs": [{"id": "avg-bug", "marker": "Avg:"}]}],
}


def _root(path: Path, doc: dict) -> Path:
    (path / "test-cases").mkdir(parents=True)
    (path / "truth").mkdir()
    (path / "test-cases" / "demo.yaml").write_text(yaml.safe_dump(doc, sort_keys=True))
    (path / "truth" / "journey-demo.json").write_text("{}")
    return path


def _changed(**case_changes) -> dict:
    doc = json.loads(json.dumps(CASE_DOC))
    doc["test_cases"][0].update(case_changes)
    return doc


def _scoring_change() -> dict:
    doc = json.loads(json.dumps(CASE_DOC))
    doc["defects"][0]["symptoms"].append("mean")
    doc["known_behaviours"] = [{"id": "q", "texts": ["Inbx"]}]
    return doc


def _result(run_id: str, metrics: dict) -> dict:
    return {"task_id": "case-1~clean", "task_version": "1", "task_type": journey.TASK_TYPE,
            "agent": "a", "model": "m", "condition": "mcp", "trial": 1, "passed": True,
            "score": 1.0, "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:01:00+00:00", "wall_time_sec": 60.0, "exit_code": 0,
            "metrics": metrics, "run_id": run_id,
            "artifact_dir": "case-1~clean/2026-01-01_case-1_ep-0123456789ab",
            "provenance": {"brief_version": 3}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """runs/ with one recorded episode of run r1 under the OLD corpus; the packaged
    corpus replaced by NEW (a scoring-only change); no held-out split."""
    old = _root(tmp_path / "old", CASE_DOC)
    new = _root(tmp_path / "new", _scoring_change())
    monkeypatch.setattr(corpus, "PACKAGED", new)
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    tasks = {t.id: t for t in [_task(_spec("clean"))]}
    monkeypatch.setattr(relabel, "journey_tasks_by_id", lambda app=None: tasks)

    runs = tmp_path / "runs"
    ep = runs / "case-1~clean" / "2026-01-01_case-1_ep-0123456789ab"
    (ep / "agent").mkdir(parents=True)
    (ep / "workspace").mkdir()
    (ep / "agent" / "transcript.txt").write_text(_transcript(_obs("Total: 4 items")))
    (ep / "workspace" / journey.FILENAME).write_text("verdict: pass\nbugs: []\n")
    (ep / "episode.json").write_text(json.dumps({"run_id": "r1", "episode_id": "ep-0123456789ab"}))
    metrics = {"version": "clean", "case_id": "case-1", "app_id": "demo",
               "corpus_version": corpus.version_of(old), "heldout_version": None,
               "scorer_version": 4, "completed": True, "bugs_found": [], "bugs_present": [],
               "false_reports": 0, "oracle": {"mode": "present", "ok": True}}
    (ep / "result.json").write_text(json.dumps(_result("r1", metrics)))
    state = runs / "_runs" / "r1"
    (state / "episodes").mkdir(parents=True)
    (state / "plan.json").write_text(json.dumps({"run_id": "r1", "mode": "journey"}))
    (state / "episodes" / "ep-0123456789ab.json").write_text(
        json.dumps({"run_id": "r1", "episode_id": "ep-0123456789ab",
                    "episode_dir": "case-1~clean/2026-01-01_case-1_ep-0123456789ab"}))
    (state / "schedule.jsonl").write_text(json.dumps({"run_id": "r1", "unit": 1}) + "\n")
    return {"old": old, "new": new, "runs": runs, "ep": ep, "tmp": tmp_path}


def _relabel(w, **kw):
    args = {"new_run_id": "r1-relabelled", "out_dir": w["tmp"] / "out", "old_public": w["old"]}
    args.update(kw)
    return relabel.relabel_run(w["runs"], "r1", **args)


def test_a_scoring_only_change_is_relabelled_as_a_copy(world):
    before = (world["ep"] / "result.json").read_text()
    rep = _relabel(world)
    assert rep.episodes == 1 and rep.statuses == {"rescored": 1}
    out = world["tmp"] / "out"
    copy = out / world["ep"].parent.name / world["ep"].name
    res = json.loads((copy / "result.json").read_text())
    assert res["run_id"] == "r1-relabelled"
    assert res["metrics"]["corpus_version"] == corpus.version_of(world["new"])
    assert res["restamped_from"] == {"run_id": "r1",
                                     "corpus_version": corpus.version_of(world["old"]),
                                     "heldout_version": None, "scorer_versions": ["4"]}
    assert res["rescored_with"]["scorer_version"] == journey.SCORER_VERSION
    RunResult.model_validate(res)                          # still a valid result.json
    # The run state moved with it, run id swapped on whole values only.
    assert json.loads((out / "_runs" / "r1-relabelled" / "plan.json").read_text())["run_id"] == "r1-relabelled"
    marker = json.loads((out / "_runs" / "r1-relabelled" / "episodes" / "ep-0123456789ab.json").read_text())
    assert marker["run_id"] == "r1-relabelled" and marker["episode_dir"].startswith("case-1~clean/")
    assert json.loads((copy / "episode.json").read_text())["run_id"] == "r1-relabelled"
    assert "r1-relabelled" in (out / "_runs" / "r1-relabelled" / "schedule.jsonl").read_text()
    # The original is untouched.
    assert (world["ep"] / "result.json").read_text() == before


def test_the_view_files_the_copy_under_the_new_set_and_says_it_was_relabelled(world):
    _relabel(world)
    out = world["tmp"] / "out"
    tasks = relabel.journey_tasks_by_id()
    res = view.build_view(out, ["r1-relabelled"], tasks_by_id=tasks)
    manifest = json.loads((res.out_dir / "manifest.json").read_text())
    run = manifest["runs"][0]
    assert run["set_key"] == f"j-{corpus.version_of(world['new'])}-none-b3"
    assert run["versions"]["restamped_from"] == {
        "episodes": 1, "run_ids": ["r1"], "corpus_versions": [corpus.version_of(world["old"])],
        "heldout_versions": []}
    # An ordinary run's manifest has no such key.
    plain = view.build_view(world["runs"], ["r1"], tasks_by_id=tasks)
    assert "restamped_from" not in json.loads((plain.out_dir / "manifest.json").read_text())["runs"][0]["versions"]


@pytest.mark.parametrize("doc, why", [
    (_changed(steps=["Open the list", "Read the sum"]), "case-1: `steps` changed"),
    (_changed(name="Another case"), "case-1: `name` changed"),
    (_changed(step_budget=40), "case-1: `step_budget` changed"),
    (_changed(check={"steps": ["launch"], "expect": {"present": "Total: 5 items"}}),
     "case-1: `check` changed"),
    (_changed(bugs=[]), "case-1: `bugs` changed"),
    ({**CASE_DOC, "apk": {"repo": "r", "filename": "demo.apk", "sha256": "b" * 64}},
     "journey APK changed"),
    ({**CASE_DOC, "test_cases": []}, "case-1: case removed"),
])
def test_a_change_to_what_was_measured_is_refused(world, monkeypatch, doc, why):
    new = _root(world["tmp"] / "measured", doc)
    monkeypatch.setattr(corpus, "PACKAGED", new)
    with pytest.raises(relabel.RelabelRefused) as exc:
        _relabel(world)
    assert any(why in p for p in exc.value.problems), exc.value.problems
    assert not (world["tmp"] / "out").exists()             # nothing written


def test_old_corpus_must_hash_to_what_the_run_recorded(world):
    with pytest.raises(relabel.RelabelRefused, match="hashes to"):
        _relabel(world, old_public=world["new"])


def test_nothing_to_relabel_when_the_run_already_measured_the_current_corpus(world, monkeypatch):
    monkeypatch.setattr(corpus, "PACKAGED", world["old"])
    with pytest.raises(relabel.RelabelRefused, match="nothing to re-label"):
        _relabel(world)


def test_a_run_recorded_with_a_heldout_split_needs_the_old_split(world):
    res = json.loads((world["ep"] / "result.json").read_text())
    res["metrics"]["heldout_version"] = "c" * 12
    (world["ep"] / "result.json").write_text(json.dumps(res))
    with pytest.raises(relabel.RelabelRefused) as exc:
        _relabel(world)
    assert any("pass --old-heldout" in p for p in exc.value.problems)
    assert any("no current held-out split" in p for p in exc.value.problems)


def test_a_run_without_the_split_is_not_relabelled_under_one(world, monkeypatch, tmp_path):
    split = _root(tmp_path / "split", {**CASE_DOC, "app": "other"})
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(split))
    with pytest.raises(relabel.RelabelRefused, match="ran without the held-out split"):
        _relabel(world)


def test_heldout_measurement_changes_are_counted_not_named(world, monkeypatch, tmp_path):
    old_split = _root(tmp_path / "old-split", CASE_DOC)
    new_split = _root(tmp_path / "new-split", _changed(steps=["Something else"]))
    res = json.loads((world["ep"] / "result.json").read_text())
    res["metrics"]["heldout_version"] = corpus.version_of(old_split)
    (world["ep"] / "result.json").write_text(json.dumps(res))
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(new_split))
    with pytest.raises(relabel.RelabelRefused) as exc:
        _relabel(world, old_heldout=old_split)
    held = [p for p in exc.value.problems if p.startswith("held-out split")]
    assert held == ["held-out split: 1 measurement change(s) (run `measurement_changes` "
                    "against it privately to list them)"]
    assert not any("case-1" in p for p in exc.value.problems)


def test_the_copy_goes_to_another_runs_dir_under_another_id(world):
    with pytest.raises(relabel.RelabelRefused, match="different directory"):
        _relabel(world, out_dir=world["runs"])
    with pytest.raises(relabel.RelabelRefused, match="must differ"):
        _relabel(world, new_run_id="r1")
    _relabel(world)
    with pytest.raises(relabel.RelabelRefused, match="already exists"):
        _relabel(world)


def test_the_script_checks_without_writing(world, monkeypatch, capsys):
    import importlib.util
    path = Path(__file__).parents[1] / "scripts" / "relabel_run.py"
    spec = importlib.util.spec_from_file_location("relabel_run_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--runs-dir", str(world["runs"]), "--run", "r1",
                     "--old-corpus", str(world["old"]), "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["episodes"] == 1 and out["now"]["corpus_version"] == corpus.version_of(world["new"])
    assert not (world["tmp"] / "out").exists()
