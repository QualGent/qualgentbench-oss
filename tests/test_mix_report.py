"""`scripts/mix_report.py`: the journey corpus's defect-class mix against the plan (QUA-2724).

The report is arithmetic over the test-case YAML and nothing else, so almost everything
here runs on synthetic data trees; the last section reads the packaged corpus."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from qualgentbench import corpus, journey

_PATH = Path(__file__).parents[1] / "scripts" / "mix_report.py"
_spec = importlib.util.spec_from_file_location("mix_report", _PATH)
mix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mix)


def _tree(tmp_path: Path, apps: dict[str, list[tuple[str, object, bool]]]) -> Path:
    """A data root whose `test-cases/<app>.yaml` declares the given (id, class, seeded)
    defects; a seeded defect gets a one-bug case, and a class of `...` is left out."""
    root = tmp_path / "data"
    (root / "test-cases").mkdir(parents=True)
    for app, defects in apps.items():
        doc: dict = {"app": app, "defects": [], "test_cases": []}
        for did, cls, seeded in defects:
            d = {"id": did, "kind": "functional"}
            if cls is not ...:
                d["class"] = cls
            doc["defects"].append(d)
            if seeded:
                doc["test_cases"].append({"id": f"{app}-{did}", "bugs": [did]})
        (root / "test-cases" / f"{app}.yaml").write_text(yaml.safe_dump(doc))
    return root


def _bucket(t: dict, name: str) -> dict:
    return next(b for b in t["buckets"] if b["bucket"] == name)


def _rows(*pairs: tuple[str, object], seeded: bool = True) -> list[dict]:
    return [{"id": i, "class": c, "seeded": seeded} for i, c in pairs]


# ── the vocabulary and the plan's buckets ──────────────────────────────────────

def test_every_class_fills_exactly_one_bucket():
    placed = [c for _, classes, _ in mix.BUCKETS for c in classes]
    assert sorted(placed) == sorted(journey.DEFECT_CLASSES)
    assert len(placed) == len(set(placed))


def test_the_targets_are_the_plans_and_sum_to_87():
    """display/content is the plan's layout 9 + widget inventory 8 + content and format 7;
    the other 13% of the plan (stale display, compatibility, dead control) has no class."""
    targets = {name: t for name, _, t in mix.BUCKETS}
    assert targets == {"crash": 24.0, "display/content": 24.0, "persistence": 14.0,
                       "navigation": 11.0, "lifecycle": 7.0, "ordering": 4.0, "ANR/freeze": 3.0}
    assert sum(targets.values()) == 87.0


def test_bucket_of():
    assert mix.bucket_of("anr") == mix.bucket_of("stuck") == "ANR/freeze"
    assert (mix.bucket_of("layout") == mix.bucket_of("widget-inventory")
            == mix.bucket_of("content-format") == "display/content")
    assert mix.bucket_of(None) == mix.bucket_of("Crash") == mix.UNCLASSIFIED


# ── the arithmetic ─────────────────────────────────────────────────────────────

def test_counts_shares_targets_and_deltas():
    rows = (_rows(("a", "persistence"), ("c", "crash"), ("d", "content-format"))
            + _rows(("b", "persistence"), seeded=False))
    t = mix.tally(rows)
    p = _bucket(t, "persistence")
    assert (p["n"], p["on_a_case"], p["share"], p["target"], p["delta"]) == (2, 1, 50.0, 14.0, 36.0)
    c = _bucket(t, "crash")
    assert (c["n"], c["share"], c["delta"]) == (1, 25.0, 1.0)
    nav = _bucket(t, "navigation")
    assert (nav["n"], nav["share"], nav["delta"], nav["classes"]) == (0, 0.0, -11.0, {})
    assert (t["defects"], t["on_a_case"], t["unseeded"], t["target_total"]) == (4, 3, ["b"], 87.0)


def test_the_epic_baseline_table_reproduces_from_its_counts():
    """QUA-2723's table: 23 persistence of 40 is 57.5%, 1 crash is 2.5%, 11 display is 27.5%."""
    counts = {"persistence": 23, "content-format": 11, "crash": 1, "anr": 1, "stuck": 1,
              "navigation": 1, "lifecycle": 1, "ordering": 1}
    t = mix.tally([{"id": f"{c}{i}", "class": c, "seeded": True}
                   for c, k in counts.items() for i in range(k)])
    got = {b["bucket"]: (b["n"], round(b["share"], 1)) for b in t["buckets"]}
    assert got == {"crash": (1, 2.5), "display/content": (11, 27.5), "persistence": (23, 57.5),
                   "navigation": (1, 2.5), "lifecycle": (1, 2.5), "ordering": (1, 2.5),
                   "ANR/freeze": (2, 5.0)}


def test_a_bucket_sums_its_classes():
    t = mix.tally(_rows(("a", "anr"), ("b", "stuck"), ("c", "layout"), ("d", "widget-inventory"),
                        ("e", "content-format"), ("f", "content-format")))
    assert _bucket(t, "ANR/freeze")["classes"] == {"anr": 1, "stuck": 1}
    d = _bucket(t, "display/content")
    assert d["n"] == 4 and d["classes"] == {"layout": 1, "widget-inventory": 1, "content-format": 2}


def test_shares_sum_to_100_when_everything_is_classified():
    t = mix.tally(_rows(*[(str(i), c) for i, c in enumerate(journey.DEFECT_CLASSES)]))
    assert sum(b["n"] for b in t["buckets"]) == t["defects"] == len(journey.DEFECT_CLASSES)
    assert sum(b["share"] for b in t["buckets"]) == pytest.approx(100.0)


def test_an_unclassified_defect_stays_in_the_denominator():
    """A share over the classified defects only would overstate every bucket."""
    t = mix.tally(_rows(("a", "crash"), ("b", None)))
    assert _bucket(t, "crash")["share"] == 50.0 and t["unclassified"] == ["b"]


def test_an_app_with_no_defects_does_not_divide_by_zero():
    t = mix.tally([])
    assert t["defects"] == 0 and all(b["share"] == 0.0 for b in t["buckets"])
    assert _bucket(t, "crash")["delta"] == -24.0


# ── reading a data tree ────────────────────────────────────────────────────────

def test_on_a_case_follows_bugs_including_a_per_case_marker_override():
    doc = {"defects": [{"id": "a", "class": "persistence"}, {"id": "b", "class": "content-format"},
                       {"id": "c", "class": "crash"}],
           "test_cases": [{"id": "k1", "bugs": ["a"]},
                          {"id": "k2", "bugs": [{"id": "b", "marker": "DONE  X"}]}]}
    assert [(r["id"], r["seeded"]) for r in mix.defect_rows(doc)] == [
        ("a", True), ("b", True), ("c", False)]


def test_a_missing_or_misspelt_class_reads_as_unclassified():
    doc = {"defects": [{"id": "a", "class": "Crash"}, {"id": "b"}, {"id": "c", "class": "crash"}]}
    assert [r["class"] for r in mix.defect_rows(doc)] == [None, None, "crash"]


def test_main_reports_the_corpus_each_app_and_the_version(tmp_path, capsys):
    root = _tree(tmp_path, {"app1": [("a", "persistence", True), ("b", "crash", False)],
                            "app2": [("c", "navigation", True)]})
    assert mix.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert f"corpus_version {corpus.version_of(root)}" in out
    assert "CORPUS (every app: 2) — 3 defect(s), 2 on a case" in out
    assert "app1 — 2 defect(s), 1 on a case" in out and "app2 — 1 defect(s), 1 on a case" in out
    assert "Declared but on no case" in out and "(1)" in out


def test_main_fails_on_an_unclassified_defect(tmp_path, capsys):
    root = _tree(tmp_path, {"app1": [("a", "persistence", True), ("b", ..., True),
                                     ("c", "Persistence", True)]})
    assert mix.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "FAIL: 2 defect(s) have no class" in out and "b, c" in out
    assert mix.main(["--root", str(root), "--json"]) == 1


def test_main_json_is_the_same_report_as_data(tmp_path, capsys):
    root = _tree(tmp_path, {"app1": [("a", "persistence", True), ("b", "crash", False)],
                            "app2": [("c", "navigation", True)]})
    assert mix.main(["--root", str(root), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["corpus_version"] == corpus.version_of(root)
    assert rep["corpus"]["defects"] == 3 and set(rep["apps"]) == {"app1", "app2"}
    assert _bucket(rep["apps"]["app1"], "crash")["on_a_case"] == 0


def test_app_filter_and_an_unknown_app(tmp_path, capsys):
    root = _tree(tmp_path, {"app1": [("a", "persistence", True)],
                            "app2": [("c", "navigation", True)]})
    assert mix.main(["--root", str(root), "--app", "app2", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert set(rep["apps"]) == {"app2"} and rep["corpus"]["defects"] == 1
    assert mix.main(["--root", str(root), "--app", "app2,nope"]) == 1
    assert "no test-case file for nope" in capsys.readouterr().out


def test_a_root_with_no_test_case_files_is_a_failure(tmp_path, capsys):
    assert mix.main(["--root", str(tmp_path)]) == 1
    assert "FAIL" in capsys.readouterr().out


# ── the real corpus ────────────────────────────────────────────────────────────

def test_the_real_corpus_reports_every_app_and_classes_every_defect(capsys):
    assert mix.main([]) == 0
    out = capsys.readouterr().out
    assert corpus.public_apps() and all(f"\n{a} — " in out for a in corpus.public_apps())


def test_the_real_corpus_counts_every_declared_defect_once():
    rep = mix.report(corpus.PACKAGED, mix.load(corpus.PACKAGED))
    declared = sum(len(journey.load_cases(a)["defects"]) for a in corpus.public_apps())
    assert rep["corpus"]["defects"] == declared
    assert sum(b["n"] for b in rep["corpus"]["buckets"]) == declared      # none unclassified
    assert rep["corpus_version"] == corpus.corpus_version()


# The end state epic QUA-2723 committed to, as docs/defect-classes.md states it: §10's
# "after" column and per-app counts, and §6's retain list — one persistence defect per
# family. §7: "Do not quietly re-add the pruned variants." This is what makes that loud.
# A deliberate change to the corpus mix edits these AND those two sections together.
# QUA-2783 then retired two display defects whose only signal was a one-character screen
# string no report can quote (`deck-new-count-low`, ankidroid; `subtask-chip-low`,
# tasksorg): display/content 11 -> 9, ankidroid 5 -> 4, tasksorg 5 -> 4 (§10's addendum).
AFTER_THE_EPIC = {"crash": 11, "display/content": 9, "persistence": 6, "navigation": 5,
                  "lifecycle": 3, "ordering": 2, "ANR/freeze": 2}
PER_APP_AFTER_THE_EPIC = {"ankidroid": 4, "fossify-calendar": 9, "fossify-contacts": 7,
                          "medtimer": 8, "orgzly": 6, "tasksorg": 4}
RETAIN_LIST = {"edit-event-not-saved": "fossify-calendar",
               "phone-number-dropped": "fossify-contacts",
               "subtasks-left-open": "tasksorg",
               "contact-delete-broken": "fossify-contacts",
               "repeater-done-loses-recurrence": "orgzly",
               "favorite-not-saved": "fossify-contacts"}


def test_the_real_corpus_is_the_epics_after_column():
    rep = mix.report(corpus.PACKAGED, mix.load(corpus.PACKAGED))
    assert {b["bucket"]: b["n"] for b in rep["corpus"]["buckets"]} == AFTER_THE_EPIC
    assert {a: t["defects"] for a, t in rep["apps"].items()} == PER_APP_AFTER_THE_EPIC
    # Every defect is on a case: the declared mix and the measured mix are one mix.
    assert rep["corpus"]["on_a_case"] == rep["corpus"]["defects"] == sum(AFTER_THE_EPIC.values())


def test_the_real_corpus_keeps_exactly_the_persistence_retain_list():
    kept = {r["id"]: app for app, rows in mix.load(corpus.PACKAGED).items()
            for r in rows if r["class"] == "persistence"}
    assert kept == RETAIN_LIST
