"""CreateBench adversary authors through the real grader (QUA-2859).

Offline: scripted authors post through the fake QualGent API, a simulated runner answers
per the case's semantics, and `create.grader` grades. Each test that breaks the grader on
purpose proves the gate SEES that hole — a gate that stays green under a planted scoring
bug guards nothing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from qualgentbench import journey
from qualgentbench.create import grader

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
STUDY = "anki-study-first-card"          # crash target, canary-covered (a death target)
BROWSE = "anki-browse-cards"             # display target; canary hidden (report-match power)
OPEN = "anki-open-card-from-browser"     # navigation target, canary-covered, app stays alive
UNDERIVED = "medtimer-add-medicine"      # no create_controls on its truth row (yet)


def _load(name: str):
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


adv = _load("create_adversary_check")


def _briefs(*ids: str):
    return adv.load_briefs(list(ids))


@pytest.fixture(scope="module")
def result():
    # Since QUA-2860 every journey defect has a canary; BROWSE's is hidden from the
    # grader so the report-match power path stays covered.
    real = grader.canary_ids
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(grader, "canary_ids", lambda app_id: real(app_id) - {"browser-count-low"})
        return adv.run_check(_briefs(STUDY, BROWSE, OPEN), trials=1)


def _grade(res, author: str, case_id: str) -> dict:
    return next(g for g in res.grades[author] if g["case_id"] == case_id and g["trial"] == 0)


# ── the gate is green on the real grader, and says why ────────────────────────

def test_the_real_grader_passes_the_adversary_gate(result):
    assert result.ok, result.failures
    assert sorted(result.gradable) == sorted([STUDY, BROWSE, OPEN]) and not result.not_gradable


def test_honest_is_a_strong_test_on_every_brief(result):
    for cid in (STUDY, BROWSE, OPEN):
        g = _grade(result, "honest", cid)
        assert g["axes"] == {"lint": True, "repeatability": True, "specificity": True,
                             "power": True, "strong": True, "strong_exec": True}, cid
        assert not g.get("contamination_risk")
    # Power by the canary where there is one, by the report where there is not.
    assert _grade(result, "honest", STUDY)["runs"]["target-1"]["attribution"] == "canary"
    assert _grade(result, "honest", BROWSE)["runs"]["target-1"]["attribution"] == "report_match"


def test_vacuous_earns_no_power_even_though_the_canary_fired(result):
    for cid in (STUDY, BROWSE, OPEN):
        g = _grade(result, "vacuous", cid)
        assert g["axes"]["power"] is False and g["axes"]["strong"] is False
        # Only power catches it: it is lint-clean, repeatable and specific.
        assert g["axes"]["lint"] is g["axes"]["repeatability"] is g["axes"]["specificity"] is True
        assert g["runs"]["target-1"]["outcome"] == grader.MISSED
    assert _grade(result, "vacuous", STUDY)["runs"]["target-1"]["fault_fired"] == [
        "reviewer-show-answer-crash"]
    assert result.summary["vacuous"]["power"]["rate_k"] == 0


@pytest.mark.parametrize("author,axis", [
    ("overfit-symptom", "repeatability"), ("overfit-volatile", "repeatability"),
    ("impossible", "repeatability"), ("overfit-build", "specificity"),
])
def test_overfit_authors_die_on_repeatability_or_specificity(result, author, axis):
    for cid in (STUDY, BROWSE, OPEN):
        g = _grade(result, author, cid)
        assert g["axes"][axis] is False and g["axes"]["strong"] is False, (author, cid)
    # The volatile value held once: a pass@1 grader would have credited it.
    assert _grade(result, "overfit-volatile", STUDY)["runs"]["clean-1"]["ok"] is True
    # overfit-build is repeatable and powerful; only the control run sees it.
    b = _grade(result, "overfit-build", OPEN)
    assert b["axes"]["repeatability"] is True and b["axes"]["power"] is True


def test_copyist_is_a_contamination_risk_outside_every_rate(result):
    for cid in (STUDY, BROWSE, OPEN):
        g = _grade(result, "copyist", cid)
        assert g["contamination_risk"] == grader.CONTAMINATION_RISK
        assert g["reference_copy"]["share"] == 1.0
    s = result.summary["copyist"]
    assert s["graded"] == 0 and s["contamination_risk"] == 3 and s["strong"]["rate_n"] == 0


def test_no_case_fails_every_axis(result):
    g = _grade(result, "no-case", STUDY)
    assert g["status"] == grader.NO_CASE and all(v is False for v in g["axes"].values())


def test_harmful_rule_prediction_power_drops_only_where_the_app_survives(result):
    """QUA-2861's arm B, scripted: repeatability and specificity flat, power lost on a
    target that leaves the app alive and kept where the target kills the walk."""
    p = result.prediction
    assert set(p["power_drops"]) == {BROWSE, OPEN} and p["power_holds"] == [STUDY]
    assert p["holds_are_death_targets"]
    for cid in (STUDY, BROWSE, OPEN):
        h, g = _grade(result, "honest", cid), _grade(result, "harmful-rule", cid)
        assert g["axes"]["repeatability"] == h["axes"]["repeatability"]
        assert g["axes"]["specificity"] == h["axes"]["specificity"]


def test_artifacts_come_from_the_fake_api_with_their_id(tmp_path):
    b = _briefs(STUDY)[0]
    ep = adv.post_artifact(adv.author_body("honest", b), tmp_path / "ep", b.app_name)
    case, why = grader.load_artifact(ep)
    assert case is not None and not why and case.test_case_id
    assert case.raw.get("test_case_id") == case.test_case_id     # lint can see it was stored
    assert adv.author_body("no-case", b) is None
    assert grader.load_artifact(adv.post_artifact(None, tmp_path / "none", b.app_name))[0] is None


# ── planted holes: the gate must go red ───────────────────────────────────────

def _run(*ids: str):
    return adv.run_check(_briefs(*ids), trials=1)


def test_a_canary_only_power_rule_is_caught(monkeypatch):
    """A grader that credits power whenever the target's canary fired — the vacuous
    author's case passes, the canary fires anyway."""
    real = grader.score_run

    def leaky(role, metrics, plan):
        out = real(role, metrics, plan)
        if role == "target" and set(plan.targets) & set((metrics or {}).get("fault_fired") or []):
            out.update(outcome=grader.CAUGHT, ok=True)
        return out
    monkeypatch.setattr(grader, "score_run", leaky)
    res = _run(STUDY)
    assert not res.ok
    assert any("vacuous: power=True" in f for f in res.failures)


def test_a_pass_at_one_repeatability_is_caught(monkeypatch):
    monkeypatch.setattr(grader, "PLAN_ORDER", (("clean", 1), ("target", 1), ("control", 1)))
    monkeypatch.setattr(grader, "K_CLEAN", 1)
    res = _run(STUDY)
    assert not res.ok
    assert any("overfit-volatile: repeatability=True" in f for f in res.failures)


def test_a_grader_without_the_control_run_is_caught(monkeypatch):
    monkeypatch.setattr(grader, "PLAN_ORDER", tuple(r for r in grader.PLAN_ORDER
                                                    if r[0] != "control"))
    res = _run(OPEN)
    assert not res.ok
    assert any("overfit-build: specificity" in f for f in res.failures)


def test_a_disabled_copy_check_is_caught(monkeypatch):
    monkeypatch.setattr(grader, "COPY_FLAG_SHARE", 1.01)
    res = _run(STUDY)
    assert not res.ok
    assert any("copyist: not flagged" in f for f in res.failures)
    assert any("a copyist grade was rated as authoring" in f for f in res.failures)


def test_a_lint_that_cannot_see_the_stored_id_is_caught(monkeypatch):
    """The bug this gate found in QUA-2857: the artifact's id was dropped on the way to
    lint, so `created-via-api` failed every real author and Strong-Test was unreachable."""
    real = grader.runner_case

    def drop_id(artifact):
        rc = real(artifact)
        rc.raw.pop("test_case_id", None)
        return rc
    monkeypatch.setattr(grader, "runner_case", drop_id)
    res = _run(STUDY)
    assert not res.ok
    assert any("honest: lint=False" in f for f in res.failures)


# ── what it refuses to prove ──────────────────────────────────────────────────

def test_no_gradable_brief_fails_rather_than_passing_vacuously():
    res = _run(UNDERIVED)
    assert not res.ok and res.gradable == []
    assert res.not_gradable[UNDERIVED].startswith(grader.CONTROLS_NOT_DERIVED)
    assert any("no gradable brief" in f for f in res.failures)


def test_provisional_controls_exercise_an_underived_brief_and_say_so():
    res = adv.run_check(_briefs(UNDERIVED), trials=1, provisional=True)
    assert res.ok and res.provisional and res.gradable == [UNDERIVED]
    # ...and leave nothing behind: the real truth row is still underived.
    assert journey.create_controls(journey.load_truth("medtimer").get(UNDERIVED)) is None


def test_main_exit_codes(capsys):
    assert adv.main(["--case", STUDY, "--trials", "1"]) == 0
    assert "PASS:" in capsys.readouterr().out
    assert adv.main(["--case", UNDERIVED, "--trials", "1"]) == 1
    assert adv.main(["--case", "no-such-case"]) == 1
