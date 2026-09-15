"""What `scripts/derive_journey.py --repeat N` is allowed to conclude.

One clean pass and one seeded pass sufficed while every seeded defect was deterministic
by construction. A forced interleaving, a crash or a stuck-screen oracle is a MARGIN,
and one trial cannot measure a margin — so the gate runs each version N times and
demands the same outcome every time. No majority: a version whose trials disagree is
unstable, and an unstable case leaves the corpus rather than being averaged into it.

Nothing here touches a device. `stage` and `one_pass` are replaced by scripted fakes and
every adb-shaped entry point is armed to fail the test if reached.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_journey.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_journey", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dj = _load()
rp = dj.rp

R = rp.ReplayResult
HOLDS, VIOLATED, INCONCLUSIVE = rp.HOLDS, rp.VIOLATED, rp.INCONCLUSIVE


# ── summarise_trials: the all-or-nothing rule ──────────────────────────────────

def test_all_holds_is_stable_pass():
    s = dj.summarise_trials([R(HOLDS), R(HOLDS), R(HOLDS)])
    assert s == {"outcomes": {HOLDS: 3}, "label": "PASS", "stable": True, "n": 3}


def test_all_violated_is_stable_fail():
    s = dj.summarise_trials([R(VIOLATED), R(VIOLATED)])
    assert s == {"outcomes": {VIOLATED: 2}, "label": "FAIL", "stable": True, "n": 2}


def test_mixed_is_unstable_and_has_no_label():
    # 2/3 is NOT a majority PASS: the flip is the finding.
    s = dj.summarise_trials([R(HOLDS), R(HOLDS), R(VIOLATED)])
    assert s["stable"] is False
    assert s["label"] == "undecidable"
    assert s["outcomes"] == {HOLDS: 2, VIOLATED: 1}
    assert s["n"] == 3


def test_inconclusive_in_the_mix_is_unstable():
    s = dj.summarise_trials([R(HOLDS), R(INCONCLUSIVE), R(HOLDS)])
    assert s["stable"] is False and s["label"] == "undecidable"
    assert s["outcomes"] == {HOLDS: 2, INCONCLUSIVE: 1}


def test_all_inconclusive_is_stable_but_undecidable():
    # Consistent, but consistently unmeasured: stable (nothing flipped) yet no label.
    s = dj.summarise_trials([R(INCONCLUSIVE), R(INCONCLUSIVE)])
    assert s == {"outcomes": {INCONCLUSIVE: 2}, "label": "undecidable", "stable": True, "n": 2}


def test_single_trial_is_trivially_stable():
    assert dj.summarise_trials([R(VIOLATED)]) == {"outcomes": {VIOLATED: 1}, "label": "FAIL",
                                                  "stable": True, "n": 1}


# ── a scripted app: fakes for everything below `derive_app` ───────────────────

SUITE = {"app": {"id": "demo", "package": "com.demo"},
         "bugs": [{"id": "a-bug"}, {"id": "disp"}], "exploration": {}, "shared_storage": None}
DOC = {"app": "demo",
       "defects": [{"id": "a-bug", "kind": "functional"},
                   {"id": "disp", "kind": "display", "marker": "OOPS"}],
       "test_cases": [{"id": "case-a", "name": "Case A", "steps": ["Do it."],
                       "expected_outcome": "done",
                       "check": {"steps": ["launch", {"tap": "Go"}], "expect": {"present": "done"}},
                       "bugs": ["a-bug", "disp"]}]}
CLEAN_SCREENS = [["Home", "Go"], ["done", "Sep 2, 2026 2:49 PM"]]
SEEDED_SCREENS = [["Home", "Go"], ["OOPS", "Sep 3, 2026 1:00 PM", "extra"]]


def _script(monkeypatch, clean: list[R], seeded: list[R]):
    """Drive `derive_app` with scripted trial outcomes; returns the call log.

    Each version's script is consumed in order, one entry per `one_pass` call, so a test
    that scripts three outcomes proves three trials ran. Anything adb-shaped explodes."""
    calls: list[list[str]] = []
    scripts = {"clean": list(clean), "seeded": list(seeded)}

    async def fake_stage(serial, suite, tmp):
        return None, [], None

    async def fake_one_pass(serial, bundle, claim, flags, snap, shared, shared_snap,
                            device_setup, attempts=2):
        version = "seeded" if flags else "clean"
        calls.append(list(flags))
        res = scripts[version].pop(0)
        screens = SEEDED_SCREENS if version == "seeded" else CLEAN_SCREENS
        return res, [list(s) for s in screens]

    def boom(*a, **k):
        raise AssertionError("device access attempted from a unit test")

    async def aboom(*a, **k):
        boom()

    monkeypatch.setattr(dj, "stage", fake_stage)
    monkeypatch.setattr(dj, "one_pass", fake_one_pass)
    monkeypatch.setattr(dj, "run_with_dumps", aboom)
    monkeypatch.setattr(dj, "_adb", aboom)
    monkeypatch.setattr(dj, "dump_vh", aboom)
    monkeypatch.setattr(rp, "_reset", aboom)
    monkeypatch.setattr(rp, "_adb", aboom)
    monkeypatch.setattr(rp, "dump_vh", aboom)
    monkeypatch.setattr(dj, "load_suite", lambda p: SUITE)
    monkeypatch.setattr(dj.journey, "load_cases", lambda a: DOC)
    return calls


def _derive(repeat: int) -> dict:
    return asyncio.run(dj.derive_app("demo", "fake-serial", None, Path("/nonexistent"),
                                     repeat=repeat))


# What the pre-`--repeat` script produced for exactly this scripted single pass (captured
# by running it with the same fakes). Downstream readers of journey-<app>.json rely on
# this row, keys and values, so N == 1 must reproduce it — nothing added, nothing moved.
BASELINE_ROW = {
    "name": "Case A",
    "bugs": ["a-bug", "disp"],
    "expected": "FAIL",
    "measured": "FAIL",
    "blocking": "a-bug",
    "side": [{"bug": "disp", "marker": "OOPS", "visible_steps": [2], "texts": ["OOPS"]}],
    "agrees": True,
    "problems": [],
    "diff": [{"step": 2, "added": ["OOPS", "extra"], "removed": ["done"]}],
    "unclaimed_diff": [],
    "passes": {"clean": {"outcome": "holds", "detail": "present 'done' → yes", "steps_run": 2},
               "seeded": {"outcome": "violated", "detail": "present 'done' → no", "steps_run": 2}},
    "screens": {"clean": CLEAN_SCREENS, "seeded": SEEDED_SCREENS},
}


def test_single_pass_row_is_unchanged(monkeypatch, capsys):
    calls = _script(monkeypatch,
                    clean=[R(HOLDS, "present 'done' → yes", 2)],
                    seeded=[R(VIOLATED, "present 'done' → no", 2)])
    out = _derive(repeat=1)
    row = out["case-a"]
    assert list(row.keys()) == list(BASELINE_ROW.keys())      # no new fields at N == 1
    assert row == BASELINE_ROW
    assert json.loads(json.dumps(row)) == BASELINE_ROW          # still JSON-clean
    assert calls == [[], ["a-bug", "disp"]]                     # exactly one pass each
    printed = capsys.readouterr().out
    assert "    clean    holds         2 steps" in printed      # the single-pass line shape
    assert "    seeded   violated      2 steps" in printed
    assert "=> AGREES: clean holds · seeded FAIL · side [('disp', [2])]" in printed
    assert "trials" not in printed and "unstable" not in printed


def test_repeat_runs_every_trial_and_flags_a_flip(monkeypatch, capsys):
    calls = _script(monkeypatch,
                    clean=[R(HOLDS, "", 2)] * 3,
                    seeded=[R(HOLDS, "y", 2), R(HOLDS, "y", 2), R(VIOLATED, "n", 2)])
    row = _derive(repeat=3)["case-a"]
    assert calls == [[]] * 3 + [["a-bug", "disp"]] * 3          # N trials per version
    assert row["agrees"] is False
    assert row["problems"] == [
        "seeded version unstable across 3 trials: {'holds': 2, 'violated': 1}"]
    assert row["measured"] == "undecidable"                     # no majority label
    # First trial is what the legacy fields carry.
    assert row["passes"]["seeded"] == {"outcome": "holds", "detail": "y", "steps_run": 2}
    assert row["screens"]["seeded"] == SEEDED_SCREENS
    # And the new fields carry all of it.
    assert [t["outcome"] for t in row["trials"]["seeded"]] == [HOLDS, HOLDS, VIOLATED]
    assert [t["outcome"] for t in row["trials"]["clean"]] == [HOLDS] * 3
    assert row["stability"]["clean"] == {"n": 3, "stable": True, "outcomes": {HOLDS: 3}, "label": "PASS"}
    assert row["stability"]["seeded"]["stable"] is False
    assert row["stability"]["seeded"]["outcomes"] == {HOLDS: 2, VIOLATED: 1}
    printed = capsys.readouterr().out
    assert "    seeded 3/3 violated" in printed
    assert "=> DISAGREE" in printed
    assert "! seeded version unstable across 3 trials" in printed


def test_repeat_all_stable_agrees_and_keeps_the_legacy_shape(monkeypatch):
    _script(monkeypatch, clean=[R(HOLDS, "", 2)] * 3, seeded=[R(VIOLATED, "", 2)] * 3)
    row = _derive(repeat=3)["case-a"]
    assert row["agrees"] is True and row["problems"] == []
    assert row["measured"] == "FAIL"
    legacy = {k: v for k, v in row.items() if k not in ("trials", "stability")}
    assert legacy == {**BASELINE_ROW,
                      "passes": {"clean": {"outcome": HOLDS, "detail": "", "steps_run": 2},
                                 "seeded": {"outcome": VIOLATED, "detail": "", "steps_run": 2}}}


def test_unstable_clean_version_is_a_problem_too(monkeypatch):
    _script(monkeypatch,
            clean=[R(HOLDS, "", 2), R(INCONCLUSIVE, "step 2: no element matching 'Go'", 1)],
            seeded=[R(VIOLATED, "", 2)] * 2)
    row = _derive(repeat=2)["case-a"]
    assert row["agrees"] is False
    assert row["problems"][0] == (
        "clean version unstable across 2 trials: {'holds': 1, 'inconclusive': 1}")
    # The first-trial "does not pass its oracle" wording is NOT also raised: the
    # instability is the finding.
    assert not any("does not pass its oracle" in p for p in row["problems"])


# ── display markers: the same rule applies to side bugs ────────────────────────

DISPLAY_DESIGN = {"bugs": ["disp"], "blocking": None, "expected": "PASS",
                  "side": [{"bug": "disp", "marker": "OOPS"}]}


def _trial(outcome: str, screens: list[list[str]]):
    return R(outcome, "", len(screens)), [list(s) for s in screens]


def test_marker_visibility_per_trial():
    clean = [_trial(HOLDS, CLEAN_SCREENS)] * 3
    seeded = [_trial(HOLDS, SEEDED_SCREENS), _trial(HOLDS, SEEDED_SCREENS),
              _trial(HOLDS, CLEAN_SCREENS)]
    assert dj.marker_visibility(clean, seeded, "OOPS") == [True, True, False]
    # An INCONCLUSIVE side has no diff and reads as not seen.
    seeded[0] = _trial(INCONCLUSIVE, SEEDED_SCREENS)
    assert dj.marker_visibility(clean, seeded, "OOPS") == [False, True, False]


def test_display_marker_instability_is_a_problem():
    trials = {"clean": [_trial(HOLDS, CLEAN_SCREENS)] * 3,
              "seeded": [_trial(HOLDS, SEEDED_SCREENS), _trial(HOLDS, SEEDED_SCREENS),
                         _trial(HOLDS, CLEAN_SCREENS)]}
    row = dj.judge_case(DISPLAY_DESIGN, trials)
    # Both versions are outcome-stable (all HOLDS) — only the marker flips.
    assert row["stability"]["clean"]["stable"] and row["stability"]["seeded"]["stable"]
    assert row["measured"] == "PASS"
    assert row["problems"] == ["display bug disp: marker visibility unstable (seen in 2/3 trials)"]
    assert row["agrees"] is False
    assert row["side"][0]["visible_steps"] == [2]                # first trial, as today


def test_display_marker_stable_across_trials_agrees():
    trials = {"clean": [_trial(HOLDS, CLEAN_SCREENS)] * 3,
              "seeded": [_trial(HOLDS, SEEDED_SCREENS)] * 3}
    row = dj.judge_case(DISPLAY_DESIGN, trials)
    assert row["agrees"] is True and row["problems"] == []


def test_display_marker_never_seen_keeps_the_single_pass_wording():
    # 0/N is not "unstable" — it is the existing "not visible on this route" problem, once.
    trials = {"clean": [_trial(HOLDS, CLEAN_SCREENS)] * 2,
              "seeded": [_trial(HOLDS, CLEAN_SCREENS)] * 2}
    row = dj.judge_case(DISPLAY_DESIGN, trials)
    assert row["problems"] == ["display bug disp: marker 'OOPS' is not in the clean/seeded "
                               "screen diff — not visible on this route"]


# ── main(): the hunt-style stability summary and the exit code ─────────────────

def test_main_prints_stability_summary_and_fails_on_unstable(monkeypatch, capsys, tmp_path):
    _script(monkeypatch,
            clean=[R(HOLDS, "", 2)] * 3,
            seeded=[R(VIOLATED, "", 2), R(HOLDS, "", 2), R(VIOLATED, "", 2)])
    dest = tmp_path / "out.json"
    monkeypatch.setattr("sys.argv", ["derive_journey.py", "demo", "--repeat", "3",
                                     "--json", str(dest)])
    monkeypatch.setattr(dj, "ROOT", tmp_path)                    # scratch dir under tmp
    rc = asyncio.run(dj.main())
    assert rc == 1
    printed = capsys.readouterr().out
    assert "stability: 1/2 checks gave the SAME label in all 3 trials" in printed
    assert "  UNSTABLE  demo/case-a/seeded: {'violated': 2, 'holds': 1}" in printed
    saved = json.loads(dest.read_text())["case-a"]
    assert saved["stability"]["seeded"]["stable"] is False
    assert len(saved["trials"]["clean"]) == 3


def test_main_single_pass_prints_no_stability_block(monkeypatch, capsys, tmp_path):
    _script(monkeypatch, clean=[R(HOLDS, "", 2)], seeded=[R(VIOLATED, "", 2)])
    dest = tmp_path / "out.json"
    monkeypatch.setattr("sys.argv", ["derive_journey.py", "demo", "--json", str(dest)])
    monkeypatch.setattr(dj, "ROOT", tmp_path)
    assert asyncio.run(dj.main()) == 0
    printed = capsys.readouterr().out
    assert "stability:" not in printed and "UNSTABLE" not in printed
    assert "stability" not in json.loads(dest.read_text())["case-a"]


def test_repeat_below_one_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["derive_journey.py", "demo", "--repeat", "0"])
    monkeypatch.setattr(dj, "ROOT", tmp_path)
    with pytest.raises(SystemExit):
        asyncio.run(dj.main())


# ── a seeded crash measures FAIL, and agrees with an authored `expected: FAIL` ──

CRASHED = rp.CRASHED
CRASH_DETAIL = ("step 2: java.lang.IllegalStateException: seeded crash — "
                "java.lang.IllegalStateException@com.demo.Main.onClick(Main.java)")


def test_crashed_trials_are_a_stable_fail():
    s = dj.summarise_trials([R(CRASHED), R(CRASHED)])
    assert s == {"outcomes": {CRASHED: 2}, "label": "FAIL", "stable": True, "n": 2}


def test_a_crash_seeded_case_measures_fail_and_agrees(monkeypatch, capsys):
    """Before CRASHED existed the seeded pass read INCONCLUSIVE, one_pass retried it,
    and the case derived `undecidable` — the crash the corpus seeded was invisible."""
    _script(monkeypatch,
            clean=[R(HOLDS, "present 'done' → yes", 2)],
            seeded=[R(CRASHED, CRASH_DETAIL, 1)])
    row = _derive(repeat=1)["case-a"]
    assert row["measured"] == "FAIL"
    assert row["expected"] == "FAIL" and row["agrees"] is True
    assert row["passes"]["seeded"] == {"outcome": CRASHED, "detail": CRASH_DETAIL, "steps_run": 1}
    printed = capsys.readouterr().out
    # The trial line carries the signature, so the operator sees WHAT died.
    assert "    seeded   crashed       1 steps" in printed
    assert "com.demo.Main.onClick(Main.java)" in printed
    assert "=> AGREES: clean holds · seeded FAIL" in printed


def test_one_pass_does_not_retry_a_crash(monkeypatch):
    calls = {"n": 0}

    async def fake_reset(*a, **k):
        return True

    async def fake_run(serial, bundle, steps):
        calls["n"] += 1
        return R(CRASHED, CRASH_DETAIL, 1), [["Home", "Go"]]
    monkeypatch.setattr(rp, "_reset", fake_reset)
    monkeypatch.setattr(dj, "run_with_dumps", fake_run)

    async def aboom(*a, **k):
        raise AssertionError("a crashed run has no post-condition to evaluate")
    monkeypatch.setattr(dj, "evaluate", aboom)

    from types import SimpleNamespace
    claim = SimpleNamespace(steps=[], expect=None)
    res, dumps = asyncio.run(dj.one_pass("s", "com.demo", claim, ["a-bug"], None, None, None,
                                         None, attempts=3))
    assert res.outcome == CRASHED and calls["n"] == 1


def test_the_gate_executor_consults_the_crash_check_on_a_missing_anchor(monkeypatch):
    """derive_journey has its own step loop (it records screens); a crash-seeded case
    would still read INCONCLUSIVE there unless that loop asks the same question."""
    from qualgentbench.submission import Step
    asked: list[tuple] = []

    async def fake_window(serial):
        return "09-14 12:00:00.000"

    async def fake_verdict(serial, bundle, since, fallback):
        asked.append((bundle, since, fallback.outcome, fallback.detail))
        return R(CRASHED, CRASH_DETAIL, fallback.steps_run)

    async def miss(serial, text, hold_ms=0, attempts=3, choice=0):
        return False, 0, None

    async def no_overlay(serial, rounds=2):
        return []

    async def fast(serial, timeout_s=8):
        return None
    monkeypatch.setattr(rp, "crash_window", fake_window)
    monkeypatch.setattr(rp, "crash_verdict", fake_verdict)
    monkeypatch.setattr(rp, "_tap_any", miss)
    monkeypatch.setattr(rp, "_dismiss_overlays", no_overlay)
    monkeypatch.setattr(dj, "wait_stable", fast)
    monkeypatch.setattr(rp, "_SETTLE_S", 0)

    res, dumps = asyncio.run(dj.run_with_dumps("s", "com.demo", [Step("tap", "Go")]))
    assert res.outcome == CRASHED
    assert asked == [("com.demo", "09-14 12:00:00.000", INCONCLUSIVE,
                      "step 1: no element matching 'Go'")]
