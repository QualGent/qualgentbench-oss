"""`scripts/derive_budgets.py` and the held-out split (QUA-2799).

Two failures this pins. The script printed a held-out case id in every `dropped: case no
longer in the corpus (<id>)` line, so its raw output could not be pasted into a public PR
(docs/heldout.md: no file, PR or ticket may name a held-out app or case). And it read
only the packaged test cases, so held-out `step_budget`s were never derived at all.

Every app and case name here is MADE UP (`quillpad`, `quill-*`) and every fixture is
built in tmp_path. The real split's names must never appear in this repository, and
`holdout.py verify` greps tests/fixtures for them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_budgets.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_budgets_heldout", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


db = _load()

HELD_APP = "quillpad"
HELD_CASES = [
    {"id": "quill-crowded", "route": 6, "budget": 40},
    {"id": "quill-roomy", "route": 5, "budget": 40},
    {"id": "quill-unrun", "route": 5, "budget": 30},
]
PUBLIC_CASES = [
    {"id": "demo-roomy", "route": 5, "budget": 40},
    {"id": "demo-starved", "route": 10, "budget": 40},
]
SECRET = [HELD_APP] + [c["id"] for c in HELD_CASES]


def write_cases(cases_dir: Path, app: str, cases: list[dict]) -> Path:
    cases_dir.mkdir(parents=True, exist_ok=True)
    path = cases_dir / f"{app}.yaml"
    body = [f"# {app} — journey test cases (synthetic).", f"app: {app}", "", "test_cases:"]
    for c in cases:
        route = ", ".join(["launch"] + [f"{{tap: s{i}}}" for i in range(c["route"] - 1)])
        body += [
            f"  - id: {c['id']}",
            f"    step_budget: {c['budget']}",
            "    check:",
            f"      steps: [{route}]",
            "      expect: {present: done}",
        ]
    path.write_text("\n".join(body) + "\n")
    return path


def write_episode(runs_dir: Path, case: str, version: str, app: str, *,
                  heldout: bool | None = None, **metrics) -> Path:
    task_id = f"{case}~{version}"
    d = runs_dir / task_id / f"2026-09-20T00-00-00Z_{task_id}_claude-code_x_mcp_trial-1"
    d.mkdir(parents=True, exist_ok=True)
    m = {"case_id": case, "version": version, "app_id": app, "truncated": False,
         "env_failure": False, "infra_failure": False, "contaminated": False, **metrics}
    if heldout is not None:
        m["heldout"] = heldout
    (d / "result.json").write_text(json.dumps(
        {"task_id": task_id, "task_type": "journey_case", "metrics": m}))
    return d


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A public corpus, a held-out split beside it, and a runs tree holding both."""
    public, split, runs = tmp_path / "test-cases", tmp_path / "split", tmp_path / "runs"
    write_cases(public, "demo", PUBLIC_CASES)
    write_cases(split / "test-cases", HELD_APP, HELD_CASES)
    monkeypatch.setattr(db, "CASES", public)
    monkeypatch.setenv("QGB_HELDOUT_DIR", str(split))

    write_episode(runs, "demo-roomy", "clean", "demo", hook_steps=15, step_budget=40)
    write_episode(runs, "demo-roomy", "seeded", "demo", hook_steps=18, step_budget=40)
    # Public truncation at 4.1 steps per route step with nothing of its own finished:
    # the public envelope (demo-roomy, 3.6) says runaway. A held-out episode finishing
    # at 6.5 per route step would flip it to under-budget if the envelopes were pooled.
    write_episode(runs, "demo-starved", "seeded", "demo", hook_steps=41, step_budget=40,
                  truncated=True)
    # held out: one case crowding its cap, one with room, one never run
    write_episode(runs, "quill-crowded", "clean", HELD_APP, heldout=True,
                  hook_steps=20, step_budget=40)
    write_episode(runs, "quill-crowded", "seeded", HELD_APP, heldout=True,
                  hook_steps=39, step_budget=40)
    write_episode(runs, "quill-roomy", "seeded", HELD_APP, heldout=True,
                  hook_steps=16, step_budget=40)
    return {"public": public, "split": split, "runs": runs}


def report(runs: Path, *args: str) -> str:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        db.main(["--mode", "journey", "--runs-dir", str(runs), *args])
    return buf.getvalue()


# ── derivation ────────────────────────────────────────────────────────────────

def test_heldout_cases_are_loaded_and_derived_by_the_same_rules(world):
    rows = db.journey_derive(world["runs"])["cases"]
    assert {cid for cid, c in rows.items() if c["heldout"]} == {c["id"] for c in HELD_CASES}
    assert rows["quill-crowded"]["verdict"] == "under-budget"          # 39/40 = 98%
    assert rows["quill-crowded"]["recommended"] == 59                  # ceil(1.5 x 39)
    assert rows["quill-roomy"]["verdict"] == "healthy"
    assert rows["quill-unrun"]["verdict"] == "no evidence"
    assert rows["quill-crowded"]["path"].parent == world["split"] / "test-cases"


def test_a_public_verdict_never_depends_on_the_split(world, monkeypatch):
    """Public cases are judged against PUBLIC episodes only, so the public report reads
    the same in every clone, with or without the private split."""
    with_split = db.journey_derive(world["runs"])
    monkeypatch.delenv("QGB_HELDOUT_DIR")
    without = db.journey_derive(world["runs"])
    for cid in ("demo-roomy", "demo-starved"):
        a, b = with_split["cases"][cid], without["cases"][cid]
        assert (a["verdict"], a["recommended"], a["why"]) == (b["verdict"], b["recommended"], b["why"])
    assert with_split["cases"]["demo-starved"]["verdict"] == "runaway"
    assert with_split["envelope"] == without["envelope"]
    # the held-out cases DO see the pooled envelope, which only adds evidence
    assert with_split["heldout_envelope"][0] > with_split["envelope"][0]


def test_heldout_episodes_with_no_split_are_dropped_without_their_ids(world, monkeypatch):
    monkeypatch.delenv("QGB_HELDOUT_DIR")
    plan = db.journey_derive(world["runs"])
    assert plan["dropped"][db.DROP_HELDOUT_UNLOADED] == 3
    assert not any(s in k for k in plan["dropped"] for s in SECRET)
    assert dict(plan["unnamed"]) == {"quill-crowded": 2, "quill-roomy": 1}


def test_an_unstamped_episode_of_an_app_not_in_the_public_corpus_is_redacted(world,
                                                                           monkeypatch):
    """Episodes from before the stamp existed carry no `heldout`, and an app that has
    since left the repository is exactly what that looks like — so its id is redacted
    too. A retired case of a PUBLIC app is still named: nothing secret about it."""
    monkeypatch.delenv("QGB_HELDOUT_DIR")
    write_episode(world["runs"], "quill-old-route", "clean", HELD_APP,
                  hook_steps=12, step_budget=40)
    write_episode(world["runs"], "demo-retired", "clean", "demo",
                  hook_steps=12, step_budget=40)
    plan = db.journey_derive(world["runs"])
    assert plan["dropped"][db.DROP_UNKNOWN_APP] == 1
    assert "case no longer in the corpus (demo-retired)" in plan["dropped"]
    assert not any("quill" in k for k in plan["dropped"])


# ── what is printed ───────────────────────────────────────────────────────────

def test_default_output_names_no_heldout_case_and_prints_an_aggregate(world):
    out = report(world["runs"])
    for s in SECRET:
        assert s not in out, s
    assert "held-out split — aggregate only" in out
    assert "split: 3 cases over 1 app" in out
    assert "trusted: 3 episodes over 2 of 3 cases — 3 finished, 0 truncated" in out
    assert "verdicts: 1 healthy, 1 under-budget, 0 runaway, 1 no evidence" in out
    assert "1 case the evidence supports changing (+19 steps in total)" in out
    # and the public block is still there, names and all
    assert "demo-starved" in out and "RUNAWAY" in out


def test_default_output_without_the_split_names_nothing_either(world, monkeypatch):
    monkeypatch.delenv("QGB_HELDOUT_DIR")
    out = report(world["runs"])
    for s in SECRET:
        assert s not in out, s
    assert "held-out split: not configured" in out
    assert "3 held-out episodes on disk dropped" in out


def test_show_heldout_names_them_for_a_local_review(world):
    out = report(world["runs"], "--show-heldout")
    assert "never paste" in out
    assert "quill-crowded — UNDER-BUDGET, n=2: raise 40 -> 59" in out


def test_show_heldout_names_the_redacted_drops(world, monkeypatch):
    monkeypatch.delenv("QGB_HELDOUT_DIR")
    out = report(world["runs"], "--show-heldout")
    assert "quill-crowded x2" in out and "quill-roomy x1" in out


# ── writing ───────────────────────────────────────────────────────────────────

def test_write_puts_heldout_budgets_into_the_split_and_nowhere_else(world):
    public_before = (world["public"] / "demo.yaml").read_text()
    out = report(world["runs"], "--write")
    assert "1 of them into the held-out split" in out
    held = yaml.safe_load((world["split"] / "test-cases" / f"{HELD_APP}.yaml").read_text())
    got = {c["id"]: c["step_budget"] for c in held["test_cases"]}
    assert got == {"quill-crowded": 59, "quill-roomy": 40, "quill-unrun": 30}
    assert (world["public"] / "demo.yaml").read_text() == public_before
    assert not any(s in public_before for s in SECRET)
    for s in SECRET:
        assert s not in out, s


def test_write_refuses_a_heldout_row_whose_file_is_outside_the_split(world):
    plan = db.journey_derive(world["runs"])
    plan["cases"]["quill-crowded"]["path"] = world["public"] / "demo.yaml"
    with pytest.raises(SystemExit, match="wrong side of the held-out split"):
        db.journey_write(plan)


def test_nothing_is_written_without_the_flag(world):
    split_file = world["split"] / "test-cases" / f"{HELD_APP}.yaml"
    before = split_file.read_text()
    report(world["runs"])
    assert split_file.read_text() == before


# ── hunt mode prints app and task ids too ─────────────────────────────────────

def test_hunt_output_redacts_heldout_apps(monkeypatch, capsys):
    plans = {
        "demo": {"path": Path("demo.yaml"), "tasks": {"demo-task": 36}, "hunt": 120,
                 "heldout": False},
        HELD_APP: {"path": Path(f"{HELD_APP}.yaml"), "tasks": {"quill-task": 36},
                   "hunt": 140, "heldout": True},
    }
    monkeypatch.setattr(db, "derive", lambda tier, runs_dir: plans)
    db.main(["--mode", "hunt", "--tier", "hard"])
    out = capsys.readouterr().out
    assert "demo-task" in out
    assert HELD_APP not in out and "quill-task" not in out
    assert "held-out: 1 app in tier hard, 1 task budget(s) derived" in out
    db.main(["--mode", "hunt", "--tier", "hard", "--show-heldout"])
    assert "quill-task" in capsys.readouterr().out
