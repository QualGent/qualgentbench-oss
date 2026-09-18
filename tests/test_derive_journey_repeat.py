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


def test_display_marker_matches_across_typographic_spaces():
    """A 12-hour time renders as `9:00 AM` (U+202F); the marker is typed `9:00 AM`.

    The anchor matcher (`replay._fold`) and the scorer (`journey._norm`) both fold
    typographic spaces, and this gate has to fold too. Unfolded it reported a display
    defect that IS on the route as "not visible on this route" — measured 2026-09-15 on
    medtimer's `reminder-time-display-shifted`, stably, in 3/3 trials, against a
    committed key derived on an image that still rendered U+0020.
    """
    design = {"bugs": ["disp"], "blocking": None, "expected": "PASS",
              "side": [{"bug": "disp", "marker": "9:00 AM"}]}
    trials = {"clean": [_trial(HOLDS, [["Aspirin", "8:00 AM"]])] * 3,
              "seeded": [_trial(HOLDS, [["Aspirin", "9:00 AM"]])] * 3}
    row = dj.judge_case(design, trials)
    assert row["problems"] == [] and row["agrees"] is True
    assert row["side"][0]["visible_steps"] == [1]
    # The RAW screen string is what gets recorded — the scorer folds it itself.
    assert row["side"][0]["texts"] == ["9:00 AM"]
    # ...and the step it explains is not ALSO reported as an unclaimed diff.
    assert row["unclaimed_diff"] == []


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

    async def no_clock(*a, **k):
        return ""
    monkeypatch.setattr(rp, "device_time", no_clock)     # the crash window is an adb call

    async def no_markers(*a, **k):
        return []
    monkeypatch.setattr(rp, "fired_markers", no_markers)  # so is the canary read after a crash

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

    async def miss(serial, text, hold_ms=0, attempts=3, choice=0, row=""):
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


def test_the_derive_loop_scopes_a_tap_to_its_row_like_the_replayer(monkeypatch):
    """derive_journey keeps its own copy of the step loop (it records screens), so a
    route's `row:` must reach `_tap_any` there too — otherwise the corpus gate would
    derive the unscoped tap while episode replay ran the scoped one (QUA-2735)."""
    from qualgentbench.submission import Step
    seen: list[tuple[str, str]] = []

    async def fake_window(serial):
        return "09-14 12:00:00.000"

    async def passthrough(serial, bundle, since, fallback):
        return fallback

    async def hit(serial, text, hold_ms=0, attempts=3, choice=0, row=""):
        seen.append((text, row))
        return True, 1, (0, 0)

    async def no_screen(serial, retries=3):
        return ""

    async def fast(serial, timeout_s=8):
        return None
    monkeypatch.setattr(rp, "crash_window", fake_window)
    monkeypatch.setattr(rp, "crash_verdict", passthrough)
    monkeypatch.setattr(rp, "_tap_any", hit)
    monkeypatch.setattr(rp, "dump_vh", no_screen)
    monkeypatch.setattr(dj, "wait_stable", fast)
    monkeypatch.setattr(rp, "_SETTLE_S", 0)

    res, _ = asyncio.run(dj.run_with_dumps(
        "s", "com.demo", [Step("tap", "Reminded", row="Ibuprofen (4)"), Step("tap", "Taken")]))
    assert res.outcome == "holds"
    assert seen == [("Reminded", "Ibuprofen (4)"), ("Taken", "")]

    async def miss(serial, text, hold_ms=0, attempts=3, choice=0, row=""):
        return False, 0, None

    async def no_overlay(serial, rounds=2):
        return []
    monkeypatch.setattr(rp, "_tap_any", miss)
    monkeypatch.setattr(rp, "_dismiss_overlays", no_overlay)
    res, _ = asyncio.run(dj.run_with_dumps(
        "s", "com.demo", [Step("tap", "Reminded", row="Paracetamol (1)")]))
    assert res.detail == "step 1: no element matching 'Reminded' in the row of 'Paracetamol (1)'"


# ── the screen witness on the recorded screens ─────────────────────────────────
#
# A case that declares `evidence:` hands it to judge_case as `witness`: each string must
# be on the CLEAN pass's final screen (the screen the brief sends the agent to) and must
# not sit inside a display bug's measured texts (that is a marker, not a witness).

WITNESS_DESIGN = {"bugs": ["disp"], "blocking": None, "expected": "PASS",
                  "side": [{"bug": "disp", "marker": "Avg:"}]}
CLEAN_CARD = [["Home", "Go"], ["Weight", "Min: 74 kg", "Max: 85 kg", "Avg: 79 kg"]]
SEEDED_CARD = [["Home", "Go"], ["Weight", "Min: 74 kg", "Max: 85 kg", "Avg: 76 kg"]]


def _card_trials():
    return {"clean": [_trial(HOLDS, CLEAN_CARD)], "seeded": [_trial(HOLDS, SEEDED_CARD)]}


def test_a_witness_on_the_clean_final_screen_agrees_and_is_recorded():
    row = dj.judge_case(WITNESS_DESIGN, _card_trials(), witness=["Max: 85 kg"])
    assert row["problems"] == [] and row["agrees"] is True
    assert row["witness"] == {"Max: 85 kg": {"clean": [2], "seeded": [2]}}
    assert row["side"][0]["texts"] == ["Avg: 76 kg", "Avg: 79 kg"]


def test_a_witness_missing_from_the_clean_final_screen_is_a_problem():
    row = dj.judge_case(WITNESS_DESIGN, _card_trials(), witness=["Max: 85 kg", "Jan 15, 1990"])
    assert row["problems"] == ["witness 'Jan 15, 1990' not on the clean route's final screen"]
    assert row["agrees"] is False
    assert row["witness"]["Jan 15, 1990"] == {"clean": [], "seeded": []}


def test_a_witness_inside_a_display_bugs_measured_texts_is_a_marker():
    """`Avg: 79 kg` IS on the clean final screen — and it is exactly what the seeded arm
    changes, so an agent that quotes it is reading the defect, not the outcome."""
    row = dj.judge_case(WITNESS_DESIGN, _card_trials(), witness=["Avg: 79 kg"])
    assert row["problems"] == ["witness 'Avg: 79 kg' sits inside display bug disp's measured "
                               "texts ['Avg: 79 kg'] — a marker, not a witness"]
    # The overlap runs both ways: a witness that CONTAINS a measured text leaks too.
    row = dj.judge_case(WITNESS_DESIGN, {"clean": [_trial(HOLDS, [["Weight Avg: 79 kg"]])],
                                         "seeded": [_trial(HOLDS, [["Weight Avg: 76 kg"]])]},
                        witness=["Weight Avg: 79 kg"])
    assert any("a marker, not a witness" in p for p in row["problems"])


def test_a_witness_is_matched_the_way_the_scorer_matches_it():
    """Token boundaries, normalised: `170` is not on a screen that shows `1700`, and a
    witness is found inside a longer label the way the agent's device text is searched."""
    trials = {"clean": [_trial(HOLDS, [["Height", "1700 mm"]])]}
    row = dj.judge_case({"bugs": [], "blocking": None, "expected": "PASS", "side": []}, trials,
                        witness=["170"])
    assert row["problems"] == ["witness '170' not on the clean route's final screen"]
    trials = {"clean": [_trial(HOLDS, [["Height", "Height: 170 cm"]])]}
    row = dj.judge_case({"bugs": [], "blocking": None, "expected": "PASS", "side": []}, trials,
                        witness=["170"])
    assert row["problems"] == [] and row["witness"] == {"170": {"clean": [1]}}


def test_no_witness_leaves_the_row_untouched_and_a_failed_clean_pass_is_not_double_charged():
    row = dj.judge_case(WITNESS_DESIGN, _card_trials())
    assert "witness" not in row
    trials = {"clean": [_trial(VIOLATED, CLEAN_CARD)], "seeded": [_trial(HOLDS, SEEDED_CARD)]}
    row = dj.judge_case(WITNESS_DESIGN, trials, witness=["nowhere"])
    assert not any("final screen" in p for p in row["problems"])      # the clean pass is the problem
    assert row["problems"][0].startswith("clean version does not pass its oracle")
    assert row["witness"] == {"nowhere": {"clean": [], "seeded": []}}


def test_derive_app_hands_the_cases_evidence_to_the_judge(monkeypatch, capsys):
    """`evidence:` in the YAML reaches judge_case; a witness the route never shows is a
    DISAGREE printed under the case."""
    _script(monkeypatch, clean=[R(HOLDS, "present 'done' → yes", 2)],
            seeded=[R(VIOLATED, "present 'done' → no", 2)])
    doc = json.loads(json.dumps(DOC))
    doc["test_cases"][0]["evidence"] = ["done", "Sep 2, 2026 2:49 PM", "never shown"]
    monkeypatch.setattr(dj.journey, "load_cases", lambda a: doc)
    out = _derive(1)
    row = out["case-a"]
    assert row["witness"]["done"] == {"clean": [2], "seeded": []}
    assert row["problems"] == ["witness 'never shown' not on the clean route's final screen"]
    assert row["agrees"] is False
    assert "! witness 'never shown' not on the clean route's final screen" in capsys.readouterr().out
