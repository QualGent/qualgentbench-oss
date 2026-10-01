"""`check_tier_ready.py --tier create` (QUA-2859): READY only when every line passes, and
every FAIL line is reachable. Offline: arms are throwaway git repos, runs dirs are tmp."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from qualgentbench import journey
from qualgentbench.create import grader

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
STUDY = "anki-study-first-card"
BROWSE = "anki-browse-cards"


@pytest.fixture(autouse=True)
def _one_underived_case(monkeypatch):
    """QUA-2854 derived controls for all 41 cases; these tests need a case WITHOUT them
    (the not-gradable path), so that one row is served with the two keys stripped."""
    real = journey.load_truth

    def load_truth(app_id):
        truth = real(app_id)
        case = "medtimer-add-medicine"
        if case in truth:
            truth = {**truth, case: {k: v for k, v in truth[case].items()
                                     if k not in (journey.CONTROLS_KEY,
                                                  journey.CONTROL_DERIVATION_KEY)}}
        return truth
    monkeypatch.setattr(journey, "load_truth", load_truth)


def _load(name: str):
    key = f"_test_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


ctr = _load("check_tier_ready")
adv = ctr._load_script("create_adversary_check")


def _scope(*ids: str):
    return adv.load_briefs(list(ids))


# ── each check's FAIL line ────────────────────────────────────────────────────

def test_briefs_neutral_fails_on_a_lint_error(monkeypatch):
    assert ctr.check_briefs_neutral() == (True, "")
    lcb = ctr._load_script("lint_create_briefs")
    leak = lcb.Finding("error", "defect", STUDY, "names a defect")
    monkeypatch.setattr(lcb, "lint_corpus", lambda app_ids=None: {"ankidroid": [leak]})
    ok, detail = ctr.check_briefs_neutral()
    assert not ok and "1 error(s)" in detail
    monkeypatch.setattr(lcb, "lint_corpus", lambda app_ids=None: {})
    monkeypatch.setattr(lcb, "load_subset", lambda path=None: None)
    assert "subset missing" in ctr.check_briefs_neutral()[1]


def test_controls_derived_fails_on_a_missing_or_stale_row(monkeypatch):
    assert ctr.check_controls_derived(_scope(STUDY, BROWSE)) == (True, "2/2 derived")
    assert not ctr.check_controls_derived(_scope("medtimer-add-medicine"))[0]
    real = journey.load_truth

    def edited(app_id):
        t = json.loads(json.dumps(real(app_id)))
        t[STUDY].pop(journey.CONTROLS_KEY)
        t[BROWSE][journey.CONTROL_DERIVATION_KEY]["fingerprint"] = "stale"
        return t
    monkeypatch.setattr(journey, "load_truth", edited)
    ok, detail = ctr.check_controls_derived(_scope(STUDY, BROWSE))
    assert not ok and f"missing: {STUDY}" in detail and f"stale: {BROWSE}" in detail


def test_canaries_fail_on_an_uncovered_target(monkeypatch):
    ok, detail = ctr.check_canaries(_scope(STUDY))
    assert ok and "scope 1/1" in detail and "corpus " in detail
    assert ctr.check_canaries(_scope(BROWSE))[0]          # every target is covered (QUA-2860)
    real = grader.canary_ids                              # ...so simulate one that is not
    monkeypatch.setattr(grader, "canary_ids", lambda app_id: real(app_id) - {"browser-count-low"})
    ok, detail = ctr.check_canaries(_scope(BROWSE))
    assert not ok and "no canary: browser-count-low" in detail


def test_adversaries_fail_on_a_planted_hole_and_on_nothing_gradable(monkeypatch):
    assert ctr.check_adversaries(_scope(STUDY), trials=1)[0]
    assert not ctr.check_adversaries(_scope("medtimer-add-medicine"), trials=1)[0]
    monkeypatch.setattr(grader, "COPY_FLAG_SHARE", 1.01)
    ok, detail = ctr.check_adversaries(_scope(STUDY), trials=1)
    assert not ok and "copyist" in detail


TEMPLATE = textwrap.dedent("""\
    ---
    name: qualgent-test-creator
    tools:
      qualgent:
        - create_test_case
    ---
    Stand-in creator instructions.
    """)


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return path


def _config(tmp_path: Path, arm: str | None) -> Path:
    cfg = tmp_path / "bench.yaml"
    cfg.write_text("agent: codex-cli\nmodel: gpt-6-astra\n"
                   "scope: {apps: [ankidroid], mode: journey}\n" + (arm or ""))
    return cfg


def test_arm_fails_without_config_without_block_and_on_a_bad_ref(tmp_path):
    assert ctr.check_arm(None, False) == (False, "no arm: pass --config with a `create_arm:` block")
    ok, detail = ctr.check_arm(_config(tmp_path, None), False)
    assert not ok and "no `create_arm:` block" in detail
    qg = _git_repo(tmp_path / "qg", {"pyproject.toml": "[project]\nname='x'\n"})
    dl = _git_repo(tmp_path / "dl", {"subagent-templates/qualgent-test-creator.md": TEMPLATE})
    good = (f"create_arm:\n  qualgent_mcp: {{path: {qg}, ref: main}}\n"
            f"  devloop: {{path: {dl}, ref: main}}\n")
    ok, detail = ctr.check_arm(_config(tmp_path, good), False)
    assert ok and "qualgent-mcp " in detail and "tools template" in detail
    bad = good.replace(f"{{path: {dl}, ref: main}}", f"{{path: {dl}, ref: no-such-ref}}")
    (tmp_path / "bad").mkdir()
    ok, detail = ctr.check_arm(_config(tmp_path / "bad", bad), False)
    assert not ok and "ArmError" in detail
    (tmp_path / "broken").mkdir()
    ok, detail = ctr.check_arm(_config(tmp_path / "broken", "create_arm: {name: x}\n"), False)
    assert not ok


def test_fake_api_fails_when_the_artifact_does_not_grade(monkeypatch):
    ok, detail = ctr.check_fake_api()
    assert ok and "steps captured" in detail
    monkeypatch.setattr(grader, "load_artifact", lambda p: (None, "no authored case"))
    assert ctr.check_fake_api() == (False, "no authored case")


def _creation_episode(runs_dir: Path, case: str, stamp: str, **metrics) -> None:
    ep = runs_dir / case / f"{stamp}_{case}_codex-cli_ep"
    ep.mkdir(parents=True)
    (ep / "arm.json").write_text("{}")
    (ep / "result.json").write_text(json.dumps({
        "task_id": f"{case}~create", "started_at": stamp, "metrics": metrics}))


def test_creation_runs_neutral_when_none_and_fail_on_a_flagged_latest(tmp_path):
    scope = _scope(STUDY, BROWSE)
    passed, detail = ctr.check_creation_runs(tmp_path, scope)
    assert passed is None and "no creation episodes" in detail
    _creation_episode(tmp_path, STUDY, "2026-10-01T00-00-00Z", valid_case=False,
                      validity_flags=["no_case"], no_case_reason="asked_instead")
    _creation_episode(tmp_path, STUDY, "2026-10-01T01-00-00Z", valid_case=True,
                      validity_flags=[])                                 # latest: clean
    # Truncated AFTER the case was created: a valid case, not a gate failure.
    _creation_episode(tmp_path, BROWSE, "2026-10-01T00-00-00Z", valid_case=True,
                      validity_flags=["truncated"])
    assert ctr.check_creation_runs(tmp_path, scope) == (True, "n=2 flagged=0")
    _creation_episode(tmp_path, BROWSE, "2026-10-01T02-00-00Z", valid_case=False,
                      env_failure=True, validity_flags=["env_failure", "no_case"],
                      no_case_reason="never_submitted")
    ok, detail = ctr.check_creation_runs(tmp_path, scope)
    assert not ok and f"{BROWSE}: env_failure, no_case:never_submitted" in detail
    # A hunt/journey episode (no arm.json, no authored_case.json) is not a creation run.
    other = tmp_path / "x" / "ep"
    other.mkdir(parents=True)
    (other / "result.json").write_text(json.dumps({"metrics": {"dead": True}}))
    assert ctr.creation_flags({"metrics": {"validity_flags": ["off_app", "dead"],
                                           "valid_case": False}}) == ["off_app", "dead"]
    bypass = {"metrics": {"valid_case": False, "validity_flags": ["contaminated"],
                          "contaminated": True,
                          "contamination_reasons": ["qualgent_api_bypass"]}}
    assert ctr.creation_flags(bypass) == ["contaminated:qualgent_api_bypass"]


# ── the gate as a whole ───────────────────────────────────────────────────────

CHECKS = ("check_briefs_neutral", "check_controls_derived", "check_canaries",
          "check_adversaries", "check_arm", "check_fake_api", "check_creation_runs")
LABELS = {"check_briefs_neutral": "briefs are neutral",
          "check_controls_derived": "controls derived, not stale",
          "check_canaries": "every target has a fired() canary",
          "check_adversaries": "create adversary gate green",
          "check_arm": "creation arm resolves",
          "check_fake_api": "fake API captures a gradable create",
          "check_creation_runs": "latest creation runs carry no validity flag"}


def _all_pass(monkeypatch, failing: str | None = None, runs: bool | None = True):
    for name in CHECKS:
        result = (False, "planted") if name == failing else (
            (runs, "n=1") if name == "check_creation_runs" else (True, "fine"))
        monkeypatch.setattr(ctr, name, lambda *a, _r=result, **k: _r)


def test_ready_only_when_every_line_passes(monkeypatch, capsys, tmp_path):
    _all_pass(monkeypatch)
    assert ctr.main(["--tier", "create", "--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("READY") and "FAIL" not in out
    # No creation episodes yet is `--`, not a FAIL.
    _all_pass(monkeypatch, runs=None)
    assert ctr.main(["--tier", "create", "--runs-dir", str(tmp_path)]) == 0
    assert "[  --  ] latest creation runs" in capsys.readouterr().out
    # The verdict is left where the create board reads it (QUA-2858).
    from qualgentbench.create import board
    assert board.read_gate(tmp_path).ready


@pytest.mark.parametrize("failing", CHECKS)
def test_each_fail_line_makes_the_gate_not_ready(monkeypatch, capsys, tmp_path, failing):
    _all_pass(monkeypatch, failing=failing)
    assert ctr.main(["--tier", "create", "--runs-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    fail_lines = [ln for ln in out.splitlines() if ln.startswith("[ FAIL ]")]
    assert len(fail_lines) == 1 and LABELS[failing] in fail_lines[0] and "planted" in fail_lines[0]
    assert "NOT READY" in out
    from qualgentbench.create import board
    gate = board.read_gate(tmp_path)
    assert gate.state == board.NOT_READY and len(gate.failing) == 1
    assert LABELS[failing] in gate.failing[0]


def test_scope_is_the_positive_control_subset_by_default(monkeypatch, capsys, tmp_path):
    seen = []
    _all_pass(monkeypatch)
    monkeypatch.setattr(ctr, "check_canaries", lambda scope: (seen.append(len(scope)), (True, ""))[1])
    ctr.main(["--tier", "create", "--runs-dir", str(tmp_path)])
    ctr.main(["--tier", "create", "--briefs", "all", "--runs-dir", str(tmp_path)])
    assert seen[0] == 15 and seen[1] > seen[0]
    assert "15 briefs (subset)" in capsys.readouterr().out
