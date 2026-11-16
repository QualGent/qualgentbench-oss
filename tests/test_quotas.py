"""Per-app quota report (QUA-3451): `quotas.py`, `corpus-report --quotas`, `mix_report.py
--quotas`. Everything held-out runs on synthetic trees with made-up ids."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from qualgentbench import cli, corpus, corpus_report, mix, quotas

_SCRIPT = Path(__file__).parents[1] / "scripts" / "mix_report.py"
_spec = importlib.util.spec_from_file_location("mix_report_quotas", _SCRIPT)
mix_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mix_script)


def _tree(root: Path, apps: dict[str, list[tuple[str, str, bool]]], extra_cases: int = 0) -> Path:
    """A data root: per app, (defect id, class, seeded) defects; each seeded defect gets a
    one-bug case, plus `extra_cases` clean-only cases per app."""
    (root / "test-cases").mkdir(parents=True)
    for app, defects in apps.items():
        cases = [{"id": f"{app}-{d}", "bugs": [d]} for d, _, s in defects if s]
        cases += [{"id": f"{app}-clean-{i}", "bugs": []} for i in range(extra_cases)]
        doc = {"app": app,
               "defects": [{"id": d, "kind": "functional", "class": c} for d, c, _ in defects],
               "test_cases": cases}
        (root / "test-cases" / f"{app}.yaml").write_text(yaml.safe_dump(doc))
    return root


def _write(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc) if not isinstance(doc, str) else doc)
    return path


# ── parsing ────────────────────────────────────────────────────────────────────

def test_classes_fold_into_their_bucket_and_buckets_are_zero_filled():
    q = quotas.parse({"apps": {"a": {"cases": 12, "defects": {
        "crash": 2, "layout": 1, "content-format": 1, "display/content": 1, "anr": 1}}}})
    d = q["apps"]["a"]["defects"]
    assert list(d) == [name for name, _, _ in mix.BUCKETS]
    assert d["crash"] == 2 and d["display/content"] == 3 and d["ANR/freeze"] == 1
    assert d["persistence"] == 0
    assert q["apps"]["a"]["cases"] == 12 and q["cases"] is None


@pytest.mark.parametrize("doc, needle", [
    ([], "mapping"),
    ({"apps": {}}, "non-empty"),
    ({"apps": {"a": {"cases": -1}}}, "non-negative"),
    ({"apps": {"a": {"cases": True}}}, "non-negative"),
    ({"apps": {"a": {"defects": {"misspelling": 2}}}}, "unknown defect bucket"),
    ({"apps": {"a": {"case": 3}}}, "unknown key"),
    ({"apps": {"a": 3}}, "expected a mapping"),
    ({"apps": {"a": {}}, "targets": 1}, "unknown top-level"),
    ({"format": 2, "apps": {"a": {}}}, "format"),
])
def test_a_malformed_quota_file_is_refused(doc, needle):
    with pytest.raises(quotas.QuotaError, match=needle):
        quotas.parse(doc)


def test_json_is_accepted(tmp_path):
    path = _write(tmp_path / "q.json", json.dumps({"apps": {"a": {"cases": 3}}}))
    assert quotas.load(path)["apps"]["a"]["cases"] == 3


def test_a_missing_quota_file_is_refused(tmp_path):
    with pytest.raises(quotas.QuotaError, match="no quota file"):
        quotas.load(tmp_path / "nope.yaml")


# ── the arithmetic ─────────────────────────────────────────────────────────────

def test_per_app_and_total_deltas(tmp_path):
    root = _tree(tmp_path / "data", {
        "app-one": [("d1", "crash", True), ("d2", "crash", False), ("d3", "persistence", True)],
        "app-two": [("d4", "layout", True)],
    }, extra_cases=1)
    q = quotas.parse({"cases": 20, "apps": {
        "app-one": {"cases": 5, "defects": {"crash": 1, "persistence": 2}},
        "app-new": {"cases": 12, "defects": {"navigation": 3}},
    }})
    rep = quotas.report(q, mix_script.case_counts(root, ["app-one", "app-two"]),
                        mix.load(root))
    one = rep["apps"]["app-one"]
    assert (one["cases"], one["cases_target"], one["cases_delta"]) == (3, 5, -2)
    assert (one["defects"], one["defects_target"], one["defects_delta"]) == (3, 3, 0)
    crash = next(b for b in one["buckets"] if b["bucket"] == "crash")
    assert (crash["n"], crash["on_a_case"], crash["target"], crash["delta"]) == (2, 1, 1, 1)
    new = rep["apps"]["app-new"]          # planned, not started: zeros, not missing
    assert (new["cases"], new["cases_delta"], new["defects_delta"]) == (0, -12, -3)
    assert rep["unquoted"] == ["app-two"]
    t = rep["total"]
    # the case total is the whole root against the file's `cases:`
    assert (t["cases"], t["cases_target"], t["cases_delta"]) == (5, 20, -15)
    nav = next(b for b in t["buckets"] if b["bucket"] == "navigation")
    assert (nav["n"], nav["target"], nav["delta"]) == (0, 3, -3)


def test_without_a_file_case_target_the_total_is_the_sum_of_app_targets(tmp_path):
    root = _tree(tmp_path / "data", {"a": [("d1", "crash", True)]})
    q = quotas.parse({"apps": {"a": {"cases": 4}, "b": {"cases": 6}}})
    rep = quotas.report(q, {"a": 1}, mix.load(root))
    assert rep["total"]["cases_target"] == 10 and rep["total"]["buckets"] == []
    assert rep["apps"]["a"]["defects_target"] is None
    assert any("no defect quota" in line for line in quotas.render(rep))


# ── corpus-report --quotas ─────────────────────────────────────────────────────

def _invoke(*args: str):
    return CliRunner().invoke(cli.main, ["corpus-report", *args])


def test_corpus_report_on_the_packaged_corpus_prints_per_app_deltas(tmp_path):
    apps = corpus.public_apps()
    path = _write(tmp_path / "q.yaml", {"apps": {
        apps[0]: {"cases": 100, "defects": {"crash": 50}},
        "zz-not-yet-authored": {"cases": 12},
    }})
    plain = corpus_report.build()
    rep = corpus_report.build(None, path)
    assert {k: v for k, v in rep.items() if k != "quotas"} == plain
    first = rep["quotas"]["apps"][apps[0]]
    assert first["cases"] == plain["public"]["apps"][apps[0]]["cases"]
    assert first["cases_delta"] == first["cases"] - 100
    assert rep["quotas"]["apps"]["zz-not-yet-authored"]["cases_delta"] == -12
    assert rep["quotas"]["unquoted"] == sorted(apps[1:])

    out = _invoke("--quotas", str(path))
    assert out.exit_code == 0, out.output
    assert "quotas:" in out.stdout and "zz-not-yet-authored" in out.stdout
    js = _invoke("--json", "--quotas", str(path))
    assert json.loads(js.stdout)["quotas"] == json.loads(json.dumps(rep["quotas"]))


def test_corpus_report_without_quotas_has_no_quota_block():
    assert "quotas" not in corpus_report.build()
    assert "quotas:" not in _invoke().stdout


def test_corpus_report_refuses_a_quota_file_naming_a_heldout_app(tmp_path, monkeypatch):
    split = _tree(tmp_path / "split", {"zz-secret-app": [("zz-secret-d", "crash", True)]})
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(split))
    path = _write(tmp_path / "q.yaml", {"apps": {"zz-secret-app": {"cases": 15}}})
    out = _invoke("--json", "--quotas", str(path))
    assert out.exit_code != 0
    assert "held-out" in out.output and "zz-secret" not in out.output


def test_corpus_report_refuses_a_bad_quota_file(tmp_path):
    path = _write(tmp_path / "q.yaml", {"apps": {"a": {"defects": {"nope": 1}}}})
    out = _invoke("--quotas", str(path))
    assert out.exit_code != 0 and "unknown defect bucket" in out.output


# ── held-out quotas never come from inside the repository ─────────────────────

def test_heldout_quotas_inside_the_repo_are_refused(tmp_path):
    repo = tmp_path / "repo"
    inside = _write(repo / "plans" / "q.yaml", {"apps": {"x": {"cases": 1}}})
    gitignored_split = _write(repo / corpus.DEFAULT_HELDOUT_DIRNAME / "q.yaml",
                              {"apps": {"x": {"cases": 1}}})
    outside = _write(tmp_path / "private" / "q.yaml", {"apps": {"x": {"cases": 1}}})
    with pytest.raises(quotas.QuotaError, match="inside the repository"):
        quotas.load(inside, heldout=True, repo_root=repo)
    assert quotas.load(gitignored_split, heldout=True, repo_root=repo)["apps"]["x"]
    assert quotas.load(outside, heldout=True, repo_root=repo)["apps"]["x"]
    # a public report may read an in-repo quota file
    assert quotas.load(inside, heldout=False, repo_root=repo)["apps"]["x"]


def test_mix_report_refuses_heldout_quotas_from_the_repo(tmp_path, monkeypatch, capsys):
    split = _tree(tmp_path / "split", {"zz-secret-app": [("zz-secret-d", "crash", True)]})
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(split))
    in_repo = corpus.REPO_ROOT / "tests" / "fixtures" / "quotas-example.yaml"
    assert in_repo.is_file()      # a committed quota file names no held-out app
    assert mix_script.main(["--root", str(split), "--quotas", str(in_repo)]) == 1
    assert "inside the repository" in capsys.readouterr().out


def test_mix_report_reports_heldout_quotas_from_outside(tmp_path, monkeypatch, capsys):
    split = _tree(tmp_path / "split", {"zz-secret-app": [("zz-secret-d", "crash", True)]},
                  extra_cases=2)
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(split))
    path = _write(tmp_path / "private" / "q.yaml",
                  {"apps": {"zz-secret-app": {"cases": 15, "defects": {"crash": 4}}}})
    assert mix_script.main(["--root", str(split), "--quotas", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Quotas" in out and "-12" in out
    assert mix_script.main(["--root", str(split), "--quotas", str(path), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["quotas"]["apps"]["zz-secret-app"]["cases_delta"] == -12


def test_mix_report_quotas_on_the_packaged_corpus(capsys):
    path = corpus.REPO_ROOT / "tests" / "fixtures" / "quotas-example.yaml"
    assert mix_script.main(["--quotas", str(path)]) == 0
    out = capsys.readouterr().out
    for app in yaml.safe_load(path.read_text())["apps"]:
        assert app in out.split("Quotas", 1)[1]
