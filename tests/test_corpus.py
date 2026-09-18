"""Corpus version and the held-out split (corpus.py): the hash is a content hash and
nothing else, the loaders resolve the held-out directory first, and the board keeps
held-out rows and mixed corpus versions visible."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from qualgentbench import bugs, corpus, journey

from test_journey import _rr


def _mini_corpus(root: Path, app: str = "appx", *, marker: str = "Avg:") -> Path:
    """A one-app data tree in the packaged layout."""
    (root / "test-cases").mkdir(parents=True, exist_ok=True)
    (root / "truth").mkdir(exist_ok=True)
    (root / "benchmarks").mkdir(exist_ok=True)
    cases = {
        "app": app,
        "apk": {"repo": "x/y", "filename": f"journey/{app}-buggy.apk", "sha256": "0" * 64},
        "defects": [{"id": "d1", "kind": "display", "tier": "L2", "marker": marker,
                     "symptoms": ["average"]}],
        "test_cases": [{"id": f"{app}-case", "name": "Case", "steps": ["Open"],
                        "expected_outcome": "ok", "bugs": ["d1"],
                        "check": {"steps": ["launch"], "expect": {"present": "ok"}}}],
    }
    (root / "test-cases" / f"{app}.yaml").write_text(
        "# QGB-CANARY-test\n" + yaml.safe_dump(cases, sort_keys=False))
    (root / "truth" / f"journey-{app}.json").write_text(json.dumps({f"{app}-case": {"agrees": True}}))
    (root / "benchmarks" / f"{app}.yaml").write_text(yaml.safe_dump({
        "app": {"id": app, "name": app.title(), "package": f"org.{app}", "platform": "android",
                "difficulty": "hard"},
        "device_setup": {"push": [{"src": f"assets/{app}/seed.db", "dest": "/sdcard/seed.db"}]},
    }))
    return root


# ── the version ────────────────────────────────────────────────────────────────

def test_version_is_deterministic_and_content_only(tmp_path):
    a = _mini_corpus(tmp_path / "a")
    b = _mini_corpus(tmp_path / "b")
    assert corpus.version_of(a) == corpus.version_of(b)          # location does not enter
    assert len(corpus.version_of(a)) == corpus.VERSION_HEX
    assert corpus.version_of(a) == corpus.version_of(a)          # stable across calls


def test_version_ignores_mtime_but_not_a_single_byte(tmp_path):
    root = _mini_corpus(tmp_path / "a")
    before = corpus.version_of(root)
    f = root / "test-cases" / "appx.yaml"
    os.utime(f, (time.time() + 1000, time.time() + 1000))
    assert corpus.version_of(root) == before
    f.write_text(f.read_text().replace("average", "averag e"))
    assert corpus.version_of(root) != before


def test_version_covers_truth_files_and_paths(tmp_path):
    root = _mini_corpus(tmp_path / "a")
    before = corpus.version_of(root)
    (root / "truth" / "journey-appx.json").write_text(json.dumps({"appx-case": {"agrees": False}}))
    after_truth = corpus.version_of(root)
    assert after_truth != before
    # A rename is a change too: the relative path is hashed with the bytes.
    (root / "truth" / "journey-appx.json").rename(root / "truth" / "journey-appy.json")
    assert corpus.version_of(root) != after_truth


def test_version_ignores_files_outside_the_corpus_globs(tmp_path):
    root = _mini_corpus(tmp_path / "a")
    before = corpus.version_of(root)
    (root / "truth" / "hard-stability.json").write_text("{}")      # hunt-side truth
    (root / "benchmarks" / "appx.yaml").write_text("app: {id: appx}\n")
    assert corpus.version_of(root) == before


def test_empty_root_has_no_version(tmp_path):
    assert corpus.version_of(tmp_path) is None
    assert corpus.version_of(tmp_path / "missing") is None


def test_packaged_corpus_version_is_a_string_and_stamp_has_both_keys(monkeypatch):
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    v = corpus.corpus_version()
    assert isinstance(v, str) and len(v) == corpus.VERSION_HEX
    assert corpus.stamp() == {"corpus_version": v, "heldout_version": None}
    assert corpus.heldout_apps() == [] and corpus.is_heldout("medtimer") is False


# ── held-out-first resolution ──────────────────────────────────────────────────

def test_loaders_resolve_the_heldout_dir_first_then_packaged(tmp_path, monkeypatch):
    held = _mini_corpus(tmp_path / "heldout", app="medtimer", marker="HELD")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    # medtimer is public in the repo too: the held-out copy wins.
    assert corpus.is_heldout("medtimer") is True
    assert journey.cases_path("medtimer") == held / "test-cases" / "medtimer.yaml"
    assert journey.load_defects(journey.load_cases("medtimer"))["d1"]["marker"] == "HELD"
    assert journey.load_truth("medtimer") == {"medtimer-case": {"agrees": True}}
    # An app not in the held-out dir still comes from the packaged data.
    assert corpus.is_heldout("ankidroid") is False
    assert journey.cases_path("ankidroid") == corpus.PACKAGED / "test-cases" / "ankidroid.yaml"
    assert journey.load_cases("ankidroid")["app"] == "ankidroid"
    assert corpus.heldout_apps() == ["medtimer"]
    assert corpus.heldout_version() == corpus.version_of(held)


def test_truth_path_of_a_heldout_app_points_into_the_heldout_dir_before_it_exists(tmp_path, monkeypatch):
    """`derive_journey.py` writes here — a derived key for a held-out app must never land
    back in the repository."""
    held = _mini_corpus(tmp_path / "heldout", app="medtimer")
    (held / "truth" / "journey-medtimer.json").unlink()
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    assert journey.truth_path("medtimer") == held / "truth" / "journey-medtimer.json"
    assert journey.load_truth("medtimer") == {}
    # A public app's truth path is unchanged.
    assert journey.truth_path("ankidroid") == corpus.PACKAGED / "truth" / "journey-ankidroid.json"


def test_load_apps_registers_heldout_specs_and_prefers_them(tmp_path, monkeypatch):
    held = _mini_corpus(tmp_path / "heldout", app="medtimer")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    suites = {s["app"]["id"]: s for s in bugs.load_apps()}
    assert suites["medtimer"]["app"]["package"] == "org.medtimer"      # the held-out spec
    assert "ankidroid" in suites                                        # packaged ones stay
    assert sum(1 for s in bugs.load_apps() if s["app"]["id"] == "medtimer") == 1


def test_journey_tasks_flag_heldout_apps(tmp_path, monkeypatch):
    held = _mini_corpus(tmp_path / "heldout", app="medtimer")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == "medtimer")
    tasks = journey.journey_tasks(suite)
    assert tasks and all(t.bug_spec["heldout"] is True for t in tasks)
    public = next(s for s in bugs.load_apps() if s["app"]["id"] == "ankidroid")
    assert all(t.bug_spec["heldout"] is False for t in journey.journey_tasks(public))


def test_asset_path_prefers_the_heldout_root(tmp_path, monkeypatch):
    held = tmp_path / "heldout"
    (held / "assets" / "appx").mkdir(parents=True)
    (held / "assets" / "appx" / "seed.db").write_bytes(b"x")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    assert corpus.asset_path("assets/appx/seed.db") == (held / "assets" / "appx" / "seed.db").resolve()
    assert corpus.asset_path("assets/medtimer/medTimer.db") == (corpus.REPO_ROOT / "assets/medtimer/medTimer.db").resolve()


def test_app_files_lists_cases_truth_spec_and_push_sources(tmp_path):
    root = _mini_corpus(tmp_path / "data")
    files = corpus.app_files("appx", root, tmp_path)
    assert set(files) == {"test-cases/appx.yaml", "truth/journey-appx.json",
                          "benchmarks/appx.yaml", "assets/appx/seed.db"}
    assert files["assets/appx/seed.db"] == tmp_path / "assets" / "appx" / "seed.db"


def test_config_heldout_dir_sets_the_env_var_unless_already_set(tmp_path, monkeypatch):
    from qualgentbench import cli
    from qualgentbench.config import BenchConfig

    cfg = BenchConfig(agent="codex-cli", model="m", scope={"apps": ["x"]}, heldout_dir="split")
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    cli._apply_heldout_dir(cfg, tmp_path)
    assert os.environ[corpus.HELDOUT_ENV] == str(tmp_path / "split")
    monkeypatch.setenv(corpus.HELDOUT_ENV, "/elsewhere")
    cli._apply_heldout_dir(cfg, tmp_path)
    assert os.environ[corpus.HELDOUT_ENV] == "/elsewhere"


def test_preflight_heldout_check(tmp_path, monkeypatch):
    from qualgentbench import preflight as pf
    from qualgentbench.config import BenchConfig

    cfg = BenchConfig(agent="codex-cli", model="m", scope={"apps": ["x"]})
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    assert pf.check_heldout(cfg, tmp_path).passed
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "nope"))
    assert not pf.check_heldout(cfg, tmp_path).passed
    _mini_corpus(tmp_path / "split")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "split"))
    r = pf.check_heldout(cfg, tmp_path)
    assert r.passed and "1 app" in r.detail


# ── the held-out block that is not there ───────────────────────────────────────

def test_heldout_gap_is_journey_only_and_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    # Nothing else prints a held-out block, so nothing else is missing one.
    assert journey.heldout_gap("hunt") is None and journey.heldout_gap("guided") is None
    for mode in ("journey", "all"):
        gap = journey.heldout_gap(mode)
        assert gap and corpus.HELDOUT_ENV in gap
        # The documented `heldout/` location is holdout.py's default, NOT a fallback the
        # harness implements — an operator who syncs there and runs is still public-only.
        assert "NOT picked up on its own" in gap
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "nope"))
    assert "does not exist" in journey.heldout_gap("journey")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "empty"))
    assert "holds no test-cases" in journey.heldout_gap("journey")
    _mini_corpus(tmp_path / "split")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "split"))
    assert journey.heldout_gap("journey") is None


def test_require_heldout_refuses_a_journey_run_that_cannot_produce_the_block(tmp_path, monkeypatch):
    """Default: the run goes ahead — a public-only board is what every OSS clone has.
    Under --require-heldout it does not start at all, which is what a board whose pass
    criteria include the held-out rows needs."""
    import click

    from qualgentbench import cli

    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    cli._gate_heldout("journey", False)                 # warns elsewhere, never raises
    cli._gate_heldout("hunt", True)                     # hunt prints no block either way
    with pytest.raises(click.ClickException) as exc:
        cli._gate_heldout("journey", True)
    assert corpus.HELDOUT_ENV in str(exc.value) and "--require-heldout" in str(exc.value)
    _mini_corpus(tmp_path / "split")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(tmp_path / "split"))
    cli._gate_heldout("journey", True)                  # a real split satisfies it


def test_preflight_warns_on_a_journey_config_with_no_split_and_fails_under_the_env(monkeypatch):
    from qualgentbench import preflight as pf
    from qualgentbench.config import BenchConfig

    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    monkeypatch.delenv(pf.REQUIRE_HELDOUT_ENV, raising=False)
    journey_cfg = BenchConfig(agent="codex-cli", model="m",
                              scope={"apps": ["x"], "mode": "journey"})
    r = pf.check_heldout(journey_cfg, Path("."))
    assert r.passed and r.warning and "PUBLIC rows only" in r.detail
    assert r not in pf.failed([r])                      # a warning does not block a run
    monkeypatch.setenv(pf.REQUIRE_HELDOUT_ENV, "1")
    r = pf.check_heldout(journey_cfg, Path("."))
    assert not r.passed and not r.warning and pf.failed([r]) == [r]
    # A hunt config is untouched by all of it.
    hunt_cfg = BenchConfig(agent="codex-cli", model="m", scope={"apps": ["x"], "mode": "hunt"})
    assert pf.check_heldout(hunt_cfg, Path(".")).detail == "none"


# ── the board ──────────────────────────────────────────────────────────────────

def _ep(task_id, *, heldout=False, corpus_version="aaaaaaaaaaaa", heldout_version=None,
        app="x", found=("a",), present=("a", "b"), version="seeded", agent="a"):
    m = {"version": version, "completed": True, "bugs_present": list(present),
         "bugs_found": list(found), "false_reports": 0, "steps": 10, "total_tokens": 10,
         "app_id": app, "heldout": heldout, "corpus_version": corpus_version,
         "heldout_version": heldout_version}
    return _rr(task_id, m, agent=agent)


def test_summary_carries_one_corpus_version_when_every_episode_agrees():
    rows = journey.summary([_ep("c1~seeded"), _ep("c2~seeded")])
    assert len(rows) == 1
    r = rows[0]
    assert r["corpus_version"] == "aaaaaaaaaaaa" and r["corpus_versions"] == ["aaaaaaaaaaaa"]
    assert r["mixed_corpus"] is False and r["heldout"] is False and r["corpus_unstamped"] == 0
    assert journey.corpus_note(rows) == "corpus aaaaaaaaaaaa"


def test_summary_marks_a_row_that_mixes_corpus_versions():
    rows = journey.summary([_ep("c1~seeded", corpus_version="aaaaaaaaaaaa"),
                            _ep("c2~seeded", corpus_version="bbbbbbbbbbbb")])
    r = rows[0]
    assert r["corpus_version"] is None
    assert r["corpus_versions"] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert r["mixed_corpus"] is True
    note = journey.corpus_note(rows)
    assert "aaaaaaaaaaaa, bbbbbbbbbbbb" in note and journey.MIXED_CORPUS_NOTE in note


def test_summary_treats_stamped_plus_unstamped_as_mixed_and_all_unstamped_as_unknown():
    mixed = journey.summary([_ep("c1~seeded"), _ep("c2~seeded", corpus_version=None)])[0]
    assert mixed["mixed_corpus"] is True and mixed["corpus_unstamped"] == 1
    old = journey.summary([_ep("c1~seeded", corpus_version=None)])[0]
    assert old["mixed_corpus"] is False and old["corpus_version"] is None and old["corpus_versions"] == []


def test_heldout_episodes_are_their_own_row_after_the_public_ones():
    """The whole point of the split: a held-out episode must never blend into the
    public row, even with the same agent, model and arm — and it sorts under them
    whatever its F1."""
    rows = journey.summary([
        _ep("pub~seeded", found=()),                                            # public, F1 0
        _ep("held~seeded", heldout=True, heldout_version="hhhhhhhhhhhh", app="h1",
            found=("a", "b")),                                                  # held-out, F1 1
        _ep("held2~seeded", heldout=True, heldout_version="hhhhhhhhhhhh", app="h2"),
    ])
    assert [r["heldout"] for r in rows] == [False, True]
    pub, held = journey.split_heldout(rows)
    assert pub[0]["episodes"] == 1 and held[0]["episodes"] == 2
    assert held[0]["heldout_apps"] == 2
    assert held[0]["heldout_version"] == "hhhhhhhhhhhh" and held[0]["mixed_corpus"] is False
    assert journey.corpus_note(held) == "held-out hhhhhhhhhhhh"
    # A held-out row is governed by the held-out version: mixing public versions
    # underneath it does not star it, mixing held-out versions does.
    held_mixed = journey.summary([
        _ep("h1~seeded", heldout=True, heldout_version="hhhhhhhhhhhh", app="h1"),
        _ep("h2~seeded", heldout=True, heldout_version="iiiiiiiiiiii", app="h1"),
    ])[0]
    assert held_mixed["mixed_corpus"] is True


def test_summary_by_app_keeps_the_heldout_flag():
    rows = journey.summary([_ep("p~seeded", app="p"),
                            _ep("h~seeded", heldout=True, heldout_version="h" * 12, app="h")],
                           by_app=True)
    by_app = {r["app"]: r for r in rows}
    assert by_app["p"]["heldout"] is False and by_app["h"]["heldout"] is True


def test_printed_board_has_a_separate_heldout_block(monkeypatch):
    from rich.console import Console
    from qualgentbench import cli

    console = Console(record=True, width=240, force_terminal=False)
    monkeypatch.setattr(cli, "console", console)
    cli._print_journey_table([
        _ep("pub~seeded", app="p"),
        _ep("pub2~seeded", app="p", corpus_version="bbbbbbbbbbbb"),         # mixes the public row
        _ep("held~seeded", heldout=True, heldout_version="hhhhhhhhhhhh", app="h1"),
        _ep("held2~seeded", heldout=True, heldout_version="hhhhhhhhhhhh", app="h2"),
    ])
    text = console.export_text()
    assert "Test-case runs — bug finding (ranked) and completion" in text
    assert "Held-out (2 apps) — never blended into the public rows" in text
    assert "held-out hhhhhhhhhhhh" in text
    assert "H1" in text                                  # its own numbering
    assert "a · m*" in text                              # the mixed public row is starred
    assert journey.MIXED_CORPUS_NOTE in text
    assert "Rates — Held-out (2 apps)" in text


def test_printed_board_without_heldout_rows_says_so_instead_of_staying_silent(monkeypatch):
    """No held-out block is a WEAKER board, not a normal one. It used to print exactly
    like a complete board — public rows, corpus stamp, nothing about the split — so a
    run asked for a held-out block could pass a criterion it never evaluated."""
    from rich.console import Console
    from qualgentbench import cli

    console = Console(record=True, width=240, force_terminal=False)
    monkeypatch.setattr(cli, "console", console)
    cli._print_journey_table([_ep("pub~seeded", app="p")])
    text = console.export_text()
    assert "Held-out (" not in text and "corpus aaaaaaaaaaaa" in text   # no held-out TABLE
    assert "held-out: NONE" in text and "public rows only" in text
    assert journey.MIXED_CORPUS_NOTE not in text


def test_episode_stamp_lands_in_journey_metrics_only(tmp_path, monkeypatch):
    """The runner stamps journey episodes (task spec mode == journey) with the corpus
    version and the held-out flag; the keys are the ones the board reads."""
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    s = corpus.episode_stamp("ankidroid")
    assert set(s) == {"corpus_version", "heldout_version", "heldout"}
    assert s["heldout"] is False and s["corpus_version"] == corpus.corpus_version()
    held = _mini_corpus(tmp_path / "heldout", app="medtimer")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    s2 = corpus.episode_stamp("medtimer")
    assert s2["heldout"] is True and s2["heldout_version"] == corpus.version_of(held)


def test_spec_and_stability_truth_paths_follow_the_split(tmp_path, monkeypatch):
    """Every script that opens a spec or writes hunt truth by app id goes through these;
    a held-out app's hunt truth must never be written into the repository's tier file."""
    monkeypatch.delenv(corpus.HELDOUT_ENV, raising=False)
    assert corpus.spec_path("ankidroid") == corpus.PACKAGED / "benchmarks" / "ankidroid.yaml"
    assert corpus.stability_truth_path("hard", "ankidroid") == corpus.PACKAGED / "truth" / "hard-stability.json"
    held = _mini_corpus(tmp_path / "heldout", app="medtimer")
    (held / "benchmarks").mkdir(exist_ok=True)
    (held / "benchmarks" / "medtimer.yaml").write_text("app: {id: medtimer}\n")
    monkeypatch.setenv(corpus.HELDOUT_ENV, str(held))
    assert corpus.spec_path("medtimer") == held / "benchmarks" / "medtimer.yaml"
    assert corpus.stability_truth_path("hard", "medtimer") == held / "truth" / "hard-stability.medtimer.json"
    paths = corpus.spec_paths()
    assert held / "benchmarks" / "medtimer.yaml" in paths
    assert corpus.PACKAGED / "benchmarks" / "medtimer.yaml" not in paths
    assert len({p.name for p in paths}) == len(paths)
