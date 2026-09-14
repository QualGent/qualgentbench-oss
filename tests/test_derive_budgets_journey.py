"""What `scripts/derive_budgets.py --mode journey` is allowed to conclude.

A journey step budget is the harshest gate in the benchmark: a truncated episode scores
as not-completed AND as every seeded bug missed, so a budget sized too small invents two
failures per episode. That makes the derivation's job discrimination, not fitting — the
same truncation can mean "this route needs more room" or "this agent went off the rails",
and only the first is a reason to move a cap. These tests pin that distinction, because a
tool that launders runaways into bigger budgets is worse than no tool at all.

Every fixture is synthetic and built in tmp_path. The real `runs/` is gitignored, so a
test that read it would pass or fail depending on whose laptop it ran on, and would change
its mind every time a smoke run landed another episode.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_budgets.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_budgets", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


db = _load()


# ── fixtures: a corpus and a runs tree, both synthetic ─────────────────────────

def write_cases(cases_dir: Path, app: str, cases: list[dict]) -> Path:
    """One data/test-cases/<app>.yaml. Hand-written rather than yaml.dump'd so the
    comments are real: `journey_write` is line-based precisely to keep them, and a test
    against generated YAML would not notice it eating them."""
    cases_dir.mkdir(parents=True, exist_ok=True)
    path = cases_dir / f"{app}.yaml"
    body = [f"# {app} — journey test cases. Seeded state: one widget named w.", f"app: {app}",
            "", "defects:", "  - id: a-bug", "    kind: functional", "", "test_cases:"]
    for c in cases:
        route = ", ".join(["launch"] + [f"{{tap: s{i}}}" for i in range(c["route"] - 1)])
        body += [
            f"  - id: {c['id']}",
            f"    name: {c['id']} by hand",
            "    steps:",
            '      - "Do the thing."',
            '    expected_outcome: "The thing happened."',
            f"    step_budget: {c['budget']}",
            "    check:",
            f"      steps: [{route}]",
            "      expect: {present: done}",
            "    bugs: [a-bug]",
        ]
    path.write_text("\n".join(body) + "\n")
    return path


def write_episode(runs_dir: Path, case: str, version: str, **metrics) -> Path:
    """One scored journey episode, in the on-disk shape `cli.py` leaves behind."""
    task_id = f"{case}~{version}"
    d = runs_dir / task_id / f"2026-09-10T00-00-00Z_{task_id}_codex-cli_gpt-5.5_raw_trial-1"
    d.mkdir(parents=True, exist_ok=True)
    m = {"case_id": case, "version": version, "app_id": "demo",
         "truncated": False, "env_failure": False, "infra_failure": False,
         "contaminated": False, **metrics}
    m.setdefault("steps", m.get("hook_steps"))
    (d / "result.json").write_text(json.dumps(
        {"task_id": task_id, "task_type": "journey_case", "agent": "codex-cli",
         "model": "gpt-5.5", "condition": "raw", "trial": 1, "metrics": m}))
    return d


# The corpus every test below derives against. Designed so the per-route-step envelope
# (the worst a FINISHED episode has paid) is set by `crowded-case` at 37/8 = 4.63, which
# is what lets `starved-case` at 4.10 read as route cost and `adrift-case` at 10.75 not.
CORPUS = [
    {"id": "healthy-case", "route": 5, "budget": 40},
    {"id": "runaway-case", "route": 5, "budget": 40},
    {"id": "crowded-case", "route": 8, "budget": 40},
    {"id": "starved-case", "route": 10, "budget": 40},
    {"id": "adrift-case", "route": 4, "budget": 40},
    {"id": "thin-case", "route": 6, "budget": 40},
    {"id": "unmeasured-case", "route": 6, "budget": 40},
]


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    """A corpus and a runs tree wired into the script, with every verdict represented."""
    cases_dir, runs = tmp_path / "test-cases", tmp_path / "runs"
    write_cases(cases_dir, "demo", CORPUS)
    monkeypatch.setattr(db, "CASES", cases_dir)

    # healthy: both versions finish around half the cap.
    write_episode(runs, "healthy-case", "clean", hook_steps=18, step_budget=40)
    write_episode(runs, "healthy-case", "seeded", hook_steps=20, step_budget=40)
    # runaway: one version finished with room, the other sailed past the cap.
    write_episode(runs, "runaway-case", "clean", hook_steps=15, step_budget=40)
    write_episode(runs, "runaway-case", "seeded", hook_steps=43, step_budget=40, truncated=True)
    # genuinely under-budget: nothing truncated, but the seeded arm finished at 92%.
    write_episode(runs, "crowded-case", "clean", hook_steps=30, step_budget=40)
    write_episode(runs, "crowded-case", "seeded", hook_steps=37, step_budget=40)
    # under-budget by truncation: BOTH versions died at the cap, at a per-route-step cost
    # a finished episode has been measured to pay. That points at the route.
    write_episode(runs, "starved-case", "clean", hook_steps=41, step_budget=40, truncated=True)
    write_episode(runs, "starved-case", "seeded", hook_steps=41, step_budget=40, truncated=True)
    # an agent adrift: a truncation at 10.75 steps per route step, far outside anything a
    # finished route has cost, and no finished episode of its own to argue with.
    write_episode(runs, "adrift-case", "seeded", hook_steps=43, step_budget=40, truncated=True)
    # one episode, nothing wrong with it.
    write_episode(runs, "thin-case", "clean", hook_steps=22, step_budget=40)
    # unmeasured-case: deliberately no episodes at all.
    return runs


def rows(runs: Path) -> dict:
    return db.journey_derive(runs)["cases"]


# ── the three things the evidence can support ─────────────────────────────────

def test_headroom_to_spare_leaves_the_budget_alone(bench):
    c = rows(bench)["healthy-case"]
    assert c["verdict"] == "healthy"
    assert c["recommended"] == c["budget"] == 40
    # The derived figure is printed for context and is allowed to be SMALLER than the
    # authored budget — deriving is not a licence to tighten a gate. Nothing recommends
    # acting on it, so a cheap case can never lose the room it has.
    assert c["proposal"] == 30 < c["budget"]


def test_a_runaway_is_never_laundered_into_a_bigger_budget(bench):
    """The pattern the measured corpus is full of: a truncation just past the cap while
    the same route finished with room to spare. More budget buys failing steps."""
    c = rows(bench)["runaway-case"]
    assert c["verdict"] == "runaway"
    assert c["recommended"] == c["budget"] == 40
    assert "room to spare" in c["why"]


def test_a_truncation_outside_every_measured_route_cost_is_also_a_runaway(bench):
    """No finished episode of its own to contradict it, so the corpus answers instead:
    10.75 steps per route step is past anything a finished route has ever cost."""
    c = rows(bench)["adrift-case"]
    assert c["verdict"] == "runaway"
    assert c["recommended"] == c["budget"] == 40
    assert "beyond the" in c["why"]


def test_a_crowded_finish_is_under_budget_even_with_no_truncation(bench):
    """It finished — but on 92% of its cap, and the gate has to hold for the next agent
    on the same route, not just the one that squeezed through."""
    c = rows(bench)["crowded-case"]
    assert c["verdict"] == "under-budget"
    assert c["censored"] is False
    assert c["recommended"] == 56 == c["proposal"]          # ceil(1.5 x 37)


def test_truncations_at_a_plausible_route_cost_are_under_budget(bench):
    """Both versions died at the cap and neither ever finished, at a cost a finished
    episode elsewhere has genuinely paid. That is the one shape where a truncation is
    evidence FOR a bigger budget — and the basis is flagged censored, because an episode
    killed at its cap only ever proves a lower bound."""
    c = rows(bench)["starved-case"]
    assert c["verdict"] == "under-budget"
    assert c["censored"] is True
    assert c["recommended"] == 62 == c["proposal"]          # ceil(1.5 x 41)


def test_no_episodes_means_no_evidence_not_a_default(bench):
    c = rows(bench)["unmeasured-case"]
    assert c["verdict"] == "no evidence"
    assert c["n"] == 0 and c["proposal"] is None
    assert c["recommended"] == c["budget"] == 40


def test_one_episode_is_labelled_a_guess(bench):
    c = rows(bench)["thin-case"]
    assert c["n"] == 1 and c["thin"] is True
    # and a case measured twice is not flagged, or the label would mean nothing
    assert rows(bench)["healthy-case"]["thin"] is False


# ── where the numbers come from ───────────────────────────────────────────────

def test_both_versions_are_pooled_and_the_dearer_one_sets_the_budget(tmp_path, monkeypatch):
    """One budget gates clean and seeded alike, so it must cover the dearer of the two.
    An average would under-fund whichever arm happened to be expensive."""
    cases_dir, runs = tmp_path / "test-cases", tmp_path / "runs"
    write_cases(cases_dir, "demo", [{"id": "pooled", "route": 8, "budget": 40}])
    monkeypatch.setattr(db, "CASES", cases_dir)
    write_episode(runs, "pooled", "clean", hook_steps=12, step_budget=40)
    write_episode(runs, "pooled", "seeded", hook_steps=36, step_budget=40)
    c = rows(runs)["pooled"]
    assert c["n"] == 2
    assert c["basis"]["version"] == "seeded"
    assert c["proposal"] == 54                             # ceil(1.5 x 36), not of 24
    assert c["verdict"] == "under-budget"                  # 36/40 = 90%, crowding


def test_every_number_carries_its_episode_count(bench, capsys):
    plan = db.journey_derive(bench)
    db.journey_report(plan, bench)
    out = capsys.readouterr().out
    # the run-level count, the per-case count in the table, and n= on every diagnosis
    assert "trusted: 10 episodes over 6 of 7 cases — 6 finished, 4 truncated" in out
    assert "THIN: 1 episode" in out
    for line in out.splitlines():
        if " — RUNAWAY" in line or " — UNDER-BUDGET" in line or " — HEALTHY" in line:
            assert "n=" in line


def test_the_report_says_a_runaway_proposal_is_not_supported(bench, capsys):
    db.journey_report(db.journey_derive(bench), bench)
    out = capsys.readouterr().out
    runaway = [ln for ln in out.splitlines() if ln.strip().startswith("runaway-case — ")]
    assert runaway and "NOT SUPPORTED" in runaway[0] and "budget stays 40" in runaway[0]
    assert "2 cases the evidence supports changing" in out
    assert "runaway-case" not in out.split("the evidence supports changing")[1]


def test_a_truncation_is_not_evidence_about_cost_while_anything_finished(bench):
    """Censoring, the same rule the hunt path applies: an episode killed at the cap
    reproduces the cap, not the cost. So the basis is a finished episode whenever one
    exists, and the truncation only shows up in the verdict."""
    c = rows(bench)["runaway-case"]
    assert c["basis"]["steps"] == 15 and c["censored"] is False
    assert c["truncations"] == 1


# ── a runs tree being written into while we read it ───────────────────────────

def test_a_half_written_runs_tree_does_not_stop_the_derivation(bench):
    """A smoke run writing into the same tree leaves dirs mid-flight, and so does a run
    killed in the middle of an episode. Both have to degrade into "not evidence"."""
    ep = bench / "healthy-case~clean"
    (ep / "2026-09-10T01-00-00Z_started_only").mkdir(parents=True)      # no result.json yet
    (ep / "2026-09-10T02-00-00Z_partial").mkdir(parents=True)
    (ep / "2026-09-10T02-00-00Z_partial" / "result.json").write_text('{"task_id": "heal')
    (ep / "2026-09-10T03-00-00Z_null").mkdir(parents=True)
    (ep / "2026-09-10T03-00-00Z_null" / "result.json").write_text("null")
    (ep / "2026-09-10T04-00-00Z_empty").mkdir(parents=True)
    (ep / "2026-09-10T04-00-00Z_empty" / "result.json").write_text("")

    plan = db.journey_derive(bench)
    assert plan["cases"]["healthy-case"]["verdict"] == "healthy"
    assert plan["cases"]["healthy-case"]["n"] == 2          # none of the rubble counted
    assert plan["dropped"]["unreadable or half-written"] == 3


def test_non_results_and_unmeasured_episodes_are_not_evidence(tmp_path, monkeypatch):
    """`failures.is_excluded` is the one predicate every board shares, and budgets use
    it too. An episode with no hook_steps never measured the quantity the gate enforces,
    and an episode in a stale step unit measured a different quantity."""
    cases_dir, runs = tmp_path / "test-cases", tmp_path / "runs"
    write_cases(cases_dir, "demo", [{"id": "only-junk", "route": 6, "budget": 40}])
    monkeypatch.setattr(db, "CASES", cases_dir)
    write_episode(runs, "only-junk", "clean", hook_steps=0, step_budget=40,
                  env_failure=True, infra_failure=True)
    write_episode(runs, "only-junk", "seeded", hook_steps=33, step_budget=40,
                  contaminated=True)
    write_episode(runs, "only-junk", "clean2", hook_steps=None, step_budget=40)
    write_episode(runs, "only-junk", "clean3", hook_steps=39, step_budget=40,
                  budget_accounting="per-adb-v1")

    plan = db.journey_derive(runs)
    assert plan["cases"]["only-junk"]["verdict"] == "no evidence"
    assert plan["episodes"] == []
    assert sum(plan["dropped"].values()) == 4


def test_an_episode_for_a_retired_case_is_reported_not_crashed_on(bench):
    write_episode(bench, "case-that-was-deleted", "seeded", hook_steps=20, step_budget=40)
    plan = db.journey_derive(bench)
    assert any("case-that-was-deleted" in k for k in plan["dropped"])


# ── writing, which is the part that needs a flag ──────────────────────────────

def test_deriving_writes_nothing_without_the_flag(bench, capsys):
    cases_file = db.CASES / "demo.yaml"
    before = cases_file.read_text()
    db.main(["--mode", "journey", "--runs-dir", str(bench)])
    assert cases_file.read_text() == before
    assert "under-budget" in capsys.readouterr().out


def test_write_moves_only_the_budgets_the_evidence_supports(bench):
    cases_file = db.CASES / "demo.yaml"
    plan = db.journey_derive(bench)
    assert db.journey_write(plan) == 2

    import yaml
    doc = yaml.safe_load(cases_file.read_text())
    got = {c["id"]: c["step_budget"] for c in doc["test_cases"]}
    assert got == {"healthy-case": 40, "runaway-case": 40, "crowded-case": 56,
                   "starved-case": 62, "adrift-case": 40, "thin-case": 40,
                   "unmeasured-case": 40}
    # the corpus documents its seeded state in comments; a rewrite that ate them would
    # take the only description of what is on the device with it
    assert "# demo — journey test cases." in cases_file.read_text()
    # and the rest of each case survives intact
    assert len(doc["test_cases"][0]["check"]["steps"]) == 5


def test_write_is_idempotent(bench):
    plan = db.journey_derive(bench)
    db.journey_write(plan)
    after = (db.CASES / "demo.yaml").read_text()
    # the second pass derives against the budgets the first one wrote; crowded-case now
    # has room (37 of 56), so there is nothing left to move
    plan2 = db.journey_derive(bench)
    assert db.journey_write(plan2) == 0
    assert (db.CASES / "demo.yaml").read_text() == after


# ── the constants ─────────────────────────────────────────────────────────────

def test_the_crowding_threshold_sits_in_the_gap_the_corpus_leaves_empty():
    """Measured over the real corpus: no finished episode exceeded 73% of its cap and no
    truncation died below 102%. A threshold inside that gap fires on a trend rather than
    on noise, so pin it — moving it into either cluster changes every verdict at once."""
    assert 0.73 < db.JOURNEY_CROWDED < 1.02
    # journey is the guided shape (one route, one oracle), so it takes the guided slack
    assert db.JOURNEY_HEADROOM == db.GUIDED_HEADROOM
