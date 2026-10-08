"""`qualgent-bench corpus-report`: the corpus at one version as JSON (QUA-2923).

The packaged corpus is read for the agreement checks; everything about the held-out
block runs on synthetic trees, so no held-out content is needed (or present) here."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from qualgentbench import cli, corpus, corpus_report, journey, mix

_SCRIPT = Path(__file__).parents[1] / "scripts" / "mix_report.py"
_spec = importlib.util.spec_from_file_location("mix_report_script", _SCRIPT)
mix_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mix_script)


def _invoke(*args: str):
    return CliRunner().invoke(cli.main, ["corpus-report", *args])


def _synthetic_tree(root: Path, apps: dict[str, list[list[str]]], *, apk: bool = True,
                    classes: bool = True) -> Path:
    """A data root whose `test-cases/<app>.yaml` holds one case per entry of `apps[app]`,
    each case seeding the listed defect ids (an empty list is a clean-only case)."""
    (root / "test-cases").mkdir(parents=True)
    for app, cases in apps.items():
        ids = sorted({d for bugs in cases for d in bugs})
        doc: dict = {
            "app": app,
            "defects": [{"id": d, "kind": "functional", **({"class": "crash"} if classes else {})}
                        for d in ids],
            "test_cases": [{"id": f"{app}-case-{i}", "bugs": bugs} for i, bugs in enumerate(cases)],
        }
        if apk:
            doc["apk"] = {"repo": "example/apks", "filename": f"journey/{app}.apk",
                          "sha256": f"{len(app):064x}"}
        (root / "test-cases" / f"{app}.yaml").write_text(yaml.safe_dump(doc))
    return root


# ── the packaged corpus ────────────────────────────────────────────────────────

@pytest.fixture
def packaged() -> dict:
    """Function-scoped on purpose: conftest strips QGB_* per test, so a module-scoped build
    could see a developer's QGB_HELDOUT_DIR."""
    return corpus_report.build()


def test_the_mix_is_the_mix_report_scripts_json(packaged, capsys):
    assert mix_script.main(["--json"]) == 0
    script = json.loads(capsys.readouterr().out)
    assert packaged["mix"] == {"corpus": script["corpus"], "apps": script["apps"]}
    assert packaged["corpus_version"] == script["corpus_version"] == corpus.corpus_version()


def test_the_counts_are_the_packaged_yaml(packaged):
    pub = packaged["public"]
    assert sorted(pub["apps"]) == corpus.public_apps()
    for app_id, row in pub["apps"].items():
        doc = yaml.safe_load((corpus.PACKAGED / "test-cases" / f"{app_id}.yaml").read_text())
        bugs = [journey.case_bugs(c) for c in doc["test_cases"]]
        assert row == {
            "cases": len(doc["test_cases"]),
            "seeded": sum(1 for b in bugs if b),
            "instances": sum(len(b) for b in bugs),
            "defects": len(doc["defects"]),
            "apk_sha256": doc["apk"]["sha256"],
        }, app_id
        assert len(row["apk_sha256"]) == 64
    for key, per_app in (("cases", "cases"), ("seeded_cases", "seeded"),
                         ("seeded_instances", "instances"), ("defects", "defects")):
        assert pub[key] == sum(a[per_app] for a in pub["apps"].values()), key
    assert pub["clean_only_cases"] == pub["cases"] - pub["seeded_cases"]
    assert pub["defects"] == packaged["mix"]["corpus"]["defects"]


def test_the_envelope(packaged):
    assert packaged["format"] == corpus_report.FORMAT == 1
    assert packaged["generated_from"] == "packaged"
    assert packaged["targets"] == [{"bucket": n, "classes": list(c), "target": t}
                                   for n, c, t in mix.BUCKETS]
    assert list(packaged) == ["format", "corpus_version", "generated_from", "public", "mix",
                              "heldout", "targets"]


def test_no_absolute_path_or_timestamp_in_the_output(packaged):
    text = corpus_report.dumps(packaged)
    assert str(corpus.PACKAGED) not in text and str(Path.home()) not in text
    assert corpus_report.dumps(corpus_report.build()) == text


# ── --root ─────────────────────────────────────────────────────────────────────

def test_root_on_a_copy_of_the_data_tree_reproduces_the_packaged_report(packaged, tmp_path):
    copy = tmp_path / "export" / "src" / "qualgentbench" / "data"
    shutil.copytree(corpus.PACKAGED, copy)
    rep = corpus_report.build(copy)
    assert rep["generated_from"] == "root"
    assert {**rep, "generated_from": "packaged"} == packaged

    out = _invoke("--json", "--root", str(copy))
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout) == rep
    assert out.stdout == corpus_report.dumps(rep)


def test_out_writes_the_same_bytes_json_prints(tmp_path):
    target = tmp_path / "reports" / "corpus.json"
    out = _invoke("--json", "--out", str(target))
    assert out.exit_code == 0, out.output
    assert target.read_text() == out.stdout
    quiet = _invoke("--out", str(tmp_path / "again.json"))
    assert quiet.exit_code == 0 and (tmp_path / "again.json").read_text() == out.stdout


def test_the_text_form_summarises_without_json():
    out = _invoke()
    assert out.exit_code == 0, out.output
    assert f"corpus_version {corpus.corpus_version()}" in out.stdout
    assert "held-out: none configured" in out.stdout


def test_a_synthetic_root_counts_instances_per_bugs_entry(tmp_path):
    root = _synthetic_tree(tmp_path / "data", {
        "app-one": [["d1"], ["d1"], ["d2", "d3"], []],
        "app-two": [["d9"]],
    })
    pub = corpus_report.build(root)["public"]
    assert pub["apps"]["app-one"] == {"cases": 4, "seeded": 3, "instances": 4, "defects": 3,
                                      "apk_sha256": f"{7:064x}"}
    assert (pub["cases"], pub["seeded_cases"], pub["seeded_instances"], pub["defects"],
            pub["clean_only_cases"]) == (5, 4, 5, 4, 1)


def test_a_file_without_an_apk_block_reports_a_null_sha(tmp_path):
    root = _synthetic_tree(tmp_path / "data", {"app-one": [["d1"]]}, apk=False)
    assert corpus_report.build(root)["public"]["apps"]["app-one"]["apk_sha256"] is None


def test_unclassified_defects_are_reported_not_fatal(tmp_path):
    """An older corpus predates `class:`; its report is still a report."""
    root = _synthetic_tree(tmp_path / "data", {"app-one": [["d1"], ["d2"]]}, classes=False)
    out = _invoke("--json", "--root", str(root))
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["mix"]["corpus"]["unclassified"] == ["d1", "d2"]
    assert "2 defect(s) have no class" in out.stderr


def test_a_root_with_no_test_case_files_is_refused(tmp_path):
    out = _invoke("--json", "--root", str(tmp_path))
    assert out.exit_code != 0
    assert "no test-case files" in out.output


# ── the held-out block ─────────────────────────────────────────────────────────

def test_heldout_is_null_without_a_split(packaged, monkeypatch, tmp_path):
    assert packaged["heldout"] is None
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "empty"))
    assert corpus_report.build()["heldout"] is None


def test_heldout_is_integer_totals_only(packaged, monkeypatch, tmp_path):
    split = _synthetic_tree(tmp_path / "split", {
        "zz-secret-app-a": [["zz-secret-defect-1"], ["zz-secret-defect-2", "zz-secret-defect-3"]],
        "zz-secret-app-b": [["zz-secret-defect-4"], []],
    })
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(split))
    out = _invoke("--json")
    assert out.exit_code == 0, out.output
    rep = json.loads(out.stdout)
    assert rep["heldout"] == {"cases": 4, "apps": 2, "instances": 4}
    assert all(type(v) is int for v in rep["heldout"].values())
    assert "zz-secret" not in out.stdout and str(split) not in out.stdout
    # never blended into the public numbers
    assert {k: v for k, v in rep.items() if k != "heldout"} == \
        {k: v for k, v in packaged.items() if k != "heldout"}


def test_root_at_the_heldout_split_is_refused(monkeypatch, tmp_path):
    split = _synthetic_tree(tmp_path / "split", {"zz-secret-app-a": [["zz-secret-defect-1"]]})
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(split))
    out = _invoke("--json", "--root", str(split))
    assert out.exit_code != 0
    assert "zz-secret" not in out.stdout
    assert "held-out" in out.output
