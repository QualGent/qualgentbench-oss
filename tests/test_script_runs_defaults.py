"""The gate and analysis scripts read the same runs dir `run` writes (QUA-2778).

QUA-2778 moved host runs to `~/.qualgentbench/runs`, but these scripts kept a
`ROOT / "runs"` default: `check_tier_ready.py` then printed empty budget and last-run
sections forever and still said READY, and `rescore_journey.py` / `derive_budgets.py`
read a tree no new run writes to. Each test fakes HOME, so the default is a tmp dir
this test controls; an explicit `--runs-dir` must still win.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from qualgentbench import bugs
from qualgentbench.config import default_runs_dir

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class _Stop(Exception):
    """Raised by a stubbed reader once it has seen the runs dir it was handed."""


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert default_runs_dir() == home / ".qualgentbench" / "runs"
    return home


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_runs_default_{name}", _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _argv(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["script", *args])


def _hunt_episode(runs_dir: Path, app: dict) -> None:
    """One bug_hunt result.json against the app's CURRENT hunt budget."""
    budget = (app.get("exploration") or {}).get("step_budget")
    ep = runs_dir / f"explore-{app['app']['id']}" / "2026-09-24T00-00-00Z_ep"
    ep.mkdir(parents=True)
    (ep / "result.json").write_text(json.dumps({
        "task_type": "bug_hunt", "condition": "raw",
        "metrics": {"app_id": app["app"]["id"], "hook_steps": 7, "step_budget": budget,
                    "areas_total": 3, "coverage": 1.0, "device_actions": 9},
    }))


@pytest.mark.parametrize("explicit", [False, True])
def test_check_tier_ready_reads_the_default_runs_dir(fake_home, tmp_path, monkeypatch,
                                                      capsys, explicit):
    app = next(s for s in bugs.load_apps() if s["app"].get("difficulty") == "easy")
    runs_dir = tmp_path / "elsewhere" if explicit else default_runs_dir()
    _hunt_episode(runs_dir, app)
    _argv(monkeypatch, "--tier", "easy", *(["--runs-dir", str(runs_dir)] if explicit else []))
    _load("check_tier_ready").main()
    out = capsys.readouterr().out
    assert "no episodes record hook_steps yet" not in out
    assert "raw: budgets cover what the hook charges" in out and "n=1 " in out
    assert "raw: no episodes on the current spec" not in out


def test_rescore_journey_reads_the_default_runs_dir(fake_home, tmp_path, monkeypatch):
    mod = _load("rescore_journey")
    seen: list[Path] = []

    def reader(runs_dir, run_id=None):
        seen.append(Path(runs_dir))
        raise _Stop

    monkeypatch.setattr(mod, "load_results", reader)
    _argv(monkeypatch, "--dry-run")
    with pytest.raises(_Stop):
        mod.main()
    _argv(monkeypatch, "--dry-run", "--runs-dir", str(tmp_path / "old"))
    with pytest.raises(_Stop):
        mod.main()
    assert seen == [default_runs_dir(), tmp_path / "old"]


def test_derive_budgets_reads_the_default_runs_dir(fake_home, tmp_path, monkeypatch):
    mod = _load("derive_budgets")
    assert mod.RUNS == default_runs_dir()
    seen: list[Path] = []

    def journey_derive(runs_dir):
        seen.append(Path(runs_dir))
        raise _Stop

    monkeypatch.setattr(mod, "journey_derive", journey_derive)
    for argv in (["--mode", "journey"],
                 ["--mode", "journey", "--runs-dir", str(tmp_path / "old")]):
        with pytest.raises(_Stop):
            mod.main(argv)
    assert seen == [default_runs_dir(), tmp_path / "old"]


def test_check_controls_reads_the_default_runs_dir(fake_home, tmp_path, monkeypatch):
    mod = _load("check_controls")
    seen: list[Path] = []

    def episodes(runs_dir):
        seen.append(Path(runs_dir))
        return iter(())

    monkeypatch.setattr(mod, "_episodes", episodes)
    for argv in ([], ["--runs", str(tmp_path / "old")]):
        _argv(monkeypatch, "--tier", "easy", *argv)
        mod.main()
    assert seen == [default_runs_dir(), tmp_path / "old"]


@pytest.mark.parametrize("name", ["step_calibration", "score_replay", "push_ablation"])
def test_no_other_script_defaults_to_the_repo_runs_dir(name):
    """The rest read `default_runs_dir()` too; none globs `<repo>/runs` any more."""
    src = (_SCRIPTS / f"{name}.py").read_text()
    assert "default_runs_dir" in src
    for legacy in ('/ "runs"', 'default="runs"', "runs/explore"):
        assert legacy not in src
