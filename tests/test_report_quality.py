"""Report quality lives beside the scorer and cannot move it (QUA-3584).

`report_quality` reads a saved episode's `result.json` and writes `quality.json` next to
it. These tests build the saved reports with the real `journey_verdict`, so the shape the
gates read is the shape the scorer writes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scorer_closure import SCORING_MODULES, SRC, Closure, closure_sha256
from test_journey import _bug
from test_known_behaviour import EFFECT, FOUND, _verdict
from test_scorer_version import read_pin

from qualgentbench import journey
from qualgentbench import report_quality as rq

KNOWN_REPORT = _bug(4, "Inbx", "the toolbar title is cut off")
FALSE_REPORT = _bug(5, "Total: 4 items", "the total looks wrong to me")


def _episode(root: Path, name: str, metrics: dict, **extra) -> Path:
    """A saved episode directory: result.json plus the other files an episode carries."""
    d = root / "case-a" / name
    (d / "workspace").mkdir(parents=True)
    (d / "agent").mkdir()
    (d / "result.json").write_text(json.dumps(
        {"task_id": "case-a~seeded", "task_type": journey.TASK_TYPE, "metrics": metrics,
         **extra}, indent=2))
    (d / "workspace" / "findings.yaml").write_text("verdict: fail\n")
    (d / "agent" / "transcript.txt").write_text("transcript\n")
    return d


def _seeded_metrics(reports_yaml: str) -> dict:
    return _verdict("seeded", reports_yaml, active=["delete-bug"], verdict="fail").metrics


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ── the four kinds of report, and no report ────────────────────────────────────

def test_matched_false_known_and_consequence_reports(tmp_path):
    metrics = _seeded_metrics(FOUND + EFFECT + KNOWN_REPORT + FALSE_REPORT)
    # The scorer's own marks, so a change there shows here and not as a wrong gate.
    assert [(r["matched"], r["known"], r["consequence_of"]) for r in metrics["reports"]] == [
        ("delete-bug", None, None), (None, None, "delete-bug"),
        (None, "toolbar-title-truncated", None), (None, None, None)]

    q = rq.score_episode(_episode(tmp_path, "ep1", metrics))
    assert q["quality_version"] == rq.QUALITY_VERSION == 1
    assert q["scorer_version"] == journey.SCORER_VERSION
    assert q["scorer_report_source"] == metrics["report_source"] == "transcript_write"
    assert "report_source" not in q     # that name is per report now (QUA-3585)
    matched, consequence, known, false = q["reports"]

    assert matched["gates"]["G1"] == {"pass": True,
                                      "reason": "matched seeded defect delete-bug"}
    assert matched["neutral"] is False and matched["index"] == 0 and matched["step"] == 2
    # Valid, but the other headline gates have not run: not known to be customer-ready.
    assert matched["customer_ready"] is None

    assert false["gates"]["G1"] == {"pass": False, "reason": "matched no seeded defect"}
    assert false["neutral"] is False and false["customer_ready"] is False

    for neutral, key, value in ((consequence, "consequence_of", "delete-bug"),
                                (known, "known", "toolbar-title-truncated")):
        assert neutral["neutral"] is True and neutral[key] == value
        assert neutral["gates"] == {} and neutral["customer_ready"] is None

    totals = dict(q["totals"])
    assert totals.pop("gates")["G1"] == {"name": "valid", "evaluated": 2, "passed": 1}
    assert totals == {"reports": 4, "neutral": 2, "scored": 2,
                      "customer_ready": 0, "customer_ready_unknown": 1}


def test_an_episode_with_no_report(tmp_path):
    metrics = _verdict("clean", "", verdict="pass").metrics
    assert metrics["reports"] == []
    q = rq.score_episode(_episode(tmp_path, "ep1", metrics))
    assert q["reports"] == []
    totals = dict(q["totals"])
    assert totals.pop("gates")["G1"] == {"name": "valid", "evaluated": 0, "passed": 0}
    assert totals == {"reports": 0, "neutral": 0, "scored": 0,
                      "customer_ready": 0, "customer_ready_unknown": 0}


def test_customer_ready_needs_every_headline_gate():
    ok, bad, skipped = ({"pass": True, "reason": ""}, {"pass": False, "reason": ""},
                        {"pass": None, "reason": ""})
    every = dict.fromkeys(rq.HEADLINE_GATES, ok)
    assert rq._customer_ready(every) is True
    assert rq._customer_ready({**every, "G3": bad}) is False
    assert rq._customer_ready({**every, "G4": skipped}) is None
    assert rq._customer_ready({"G1": ok}) is None
    assert rq._customer_ready({"G1": bad}) is False
    # A side-column gate never decides it.
    assert rq._customer_ready({**every, "G5": bad}) is True


def test_a_directory_that_is_not_a_journey_episode_is_refused(tmp_path):
    with pytest.raises(rq.QualityError, match="no result.json"):
        rq.score_episode(tmp_path)
    hunt = _episode(tmp_path, "hunt", {"precision": 1.0})
    with pytest.raises(rq.QualityError, match="no journey report list"):
        rq.score_episode(hunt)
    (hunt / "result.json").write_text("{not json")
    with pytest.raises(rq.QualityError, match="unreadable"):
        rq.score_episode(hunt)


# ── the CLI: one quality.json per episode, nothing else touched ─────────────────

def _run_dir(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = tmp_path / "runs"
    eps = [_episode(root, "ep1", _seeded_metrics(FOUND + EFFECT + KNOWN_REPORT + FALSE_REPORT)),
           _episode(root, "ep2", _verdict("clean", "", verdict="pass").metrics),
           _episode(root, "ep3", _seeded_metrics(FALSE_REPORT))]
    # A result.json inside an episode (an agent can write one in its workspace) is not
    # an episode, and a hunt episode is skipped.
    (eps[0] / "workspace" / "result.json").write_text(
        json.dumps({"metrics": {"reports": [{"matched": "delete-bug"}]}}))
    _episode(root, "hunt", {"precision": 1.0})
    (root / "_runs").mkdir()
    (root / "_runs" / "plan.json").write_text("{}")
    return root, eps


def test_the_cli_writes_quality_json_for_every_episode_and_changes_no_other_file(
        tmp_path, capsys):
    root, eps = _run_dir(tmp_path)
    before = _tree(root)

    assert rq.main([str(root)]) == 0
    out = capsys.readouterr()
    assert "wrote 3 quality.json, skipped 1" in out.out
    assert "no journey report list" in out.err

    after = _tree(root)
    new = sorted(set(after) - set(before))
    assert new == sorted(str((ep / "quality.json").relative_to(root)) for ep in eps)
    assert {k: after[k] for k in before} == before        # result.json and all else untouched
    for ep in eps:
        assert json.loads((ep / "quality.json").read_text())["quality_version"] == 1


def test_a_rescore_is_byte_identical(tmp_path):
    root, eps = _run_dir(tmp_path)
    assert rq.main([str(root)]) == 0
    first = _tree(root)
    assert rq.main([str(root)]) == 0
    assert _tree(root) == first
    # The module entry point, in a fresh interpreter (another hash seed), on one episode.
    proc = subprocess.run([sys.executable, "-m", "qualgentbench.report_quality", str(eps[0])],
                          capture_output=True, text=True, check=False,
                          env={"PYTHONPATH": str(SRC.parent), "PYTHONHASHSEED": "7"})
    assert proc.returncode == 0, proc.stderr
    assert _tree(root) == first
    text = (eps[0] / "quality.json").read_text()
    assert text == rq.render(rq.score_episode(eps[0])) and text.endswith("}\n")


def test_the_cli_fails_when_there_is_nothing_to_score(tmp_path, capsys):
    assert rq.main([str(tmp_path)]) == 1
    assert rq.main([str(tmp_path / "missing")]) == 2
    assert list(tmp_path.iterdir()) == []


# ── the scorer does not know this module exists ────────────────────────────────

def _package_imports(path: Path) -> set[str]:
    """Names a module imports from inside the package (`from . import x`, `from .x
    import y`, `import qualgentbench.x`), anywhere in the file."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith(
                "qualgentbench")):
            names.add((node.module or "").removeprefix("qualgentbench").lstrip("."))
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name.removeprefix("qualgentbench.") for a in node.names
                         if a.name.startswith("qualgentbench."))
    return names


def test_the_scorer_pin_is_unchanged_and_report_quality_is_outside_the_closure():
    pins = read_pin()
    # Scorer v5, the hash this epic started from: the quality module moved neither.
    assert journey.SCORER_VERSION == 5
    assert pins[5] == "1d4cc09a07859342cea3c66f2704b734ff40dfc1801e57eea12f7233cd490558"
    assert closure_sha256() == pins[5]

    assert not [item for item in Closure().items() if item[0] == "report_quality"]
    # Editing the quality module, or deleting it, does not move the scorer's hash.
    edited = (SRC / "report_quality.py").read_text().replace(
        "QUALITY_VERSION = 1", "QUALITY_VERSION = 2")
    assert closure_sha256(sources={"report_quality": edited}) == pins[5]
    assert closure_sha256(sources={"report_quality": ""}) == pins[5]


def test_no_scoring_module_imports_report_quality_and_it_imports_none_of_the_package():
    for module in (*SCORING_MODULES, "rescore", "rates", "result"):
        assert "report_quality" not in _package_imports(SRC / f"{module}.py"), module
    assert _package_imports(SRC / "report_quality.py") == set()


def test_scoring_quality_leaves_a_verdict_alone(tmp_path):
    """The same transcript scores the same before and after the quality module ran."""
    before = json.dumps(_seeded_metrics(FOUND + FALSE_REPORT), sort_keys=True, default=str)
    ep = _episode(tmp_path, "ep1", _seeded_metrics(FOUND + FALSE_REPORT))
    rq.write_quality(ep)
    after = json.dumps(_seeded_metrics(FOUND + FALSE_REPORT), sort_keys=True, default=str)
    assert before == after
