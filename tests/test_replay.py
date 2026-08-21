"""Differential replay.

The classification table is the whole contribution, so it is tested exhaustively
without a device; scripts/replay_findings.py exercises the device parts live.
"""

from __future__ import annotations

import pytest

from qualgentbench import replay as rp
from qualgentbench.submission import Claim, Expectation, Step, parse

CLAIM = Claim(
    area="star_card", verdict="deviates",
    steps=[Step("launch"), Step("tap", "QA-Card-01")],
    expect=Expectation("present", "★"),
)

# Captured before the autouse _no_device fixture swaps rp._reset for a noop, for the
# one test that exercises the real reset sequence.
_REAL_RESET = rp._reset


def _fake(seq):
    """Replace replay() with scripted outcomes: first call = seeded ON, second = OFF.
    An exhausted script repeats its last outcome — a genuinely unreplayable claim
    gives the same answer on the retry."""
    calls = list(seq)
    state = {"i": 0}

    async def _r(serial, bundle, steps, expect, **kw):
        i = min(state["i"], len(calls) - 1)
        state["i"] += 1
        return rp.ReplayResult(calls[i], "scripted", len(steps))
    return _r


@pytest.fixture(autouse=True)
def _no_device(monkeypatch):
    async def _noop(*a, **k):
        return True
    monkeypatch.setattr(rp, "_reset", _noop)
    monkeypatch.setattr(rp, "disable_animations", _noop)


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed,outcomes,expected", [
    # ── the agent said the area is BROKEN ───────────────────────────────────
    # violated with the defect live, fine without it → the seeding caused it.
    ("deviates", [rp.VIOLATED, rp.HOLDS], rp.CONFIRMED),
    # broken both ways → upstream behaviour, not a defect this benchmark introduced.
    ("deviates", [rp.VIOLATED, rp.VIOLATED], rp.NOT_A_DEFECT),
    # the expectation holds on the buggy build → whatever was reported isn't here.
    ("deviates", [rp.HOLDS], rp.DOES_NOT_REPRODUCE),
    # ── the agent said the area WORKS ───────────────────────────────────────
    # it does work → the agent was right, not "does not reproduce".
    ("as_specified", [rp.HOLDS], rp.CONFIRMED_WORKING),
    # it is broken, and only with the seeding → the agent missed a real defect.
    ("as_specified", [rp.VIOLATED, rp.HOLDS], rp.MISSED_DEFECT),
    ("as_specified", [rp.VIOLATED, rp.VIOLATED], rp.NOT_A_DEFECT),
    # ── undecidable is never counted against the agent ──────────────────────
    ("deviates", [rp.INCONCLUSIVE], rp.UNREPLAYABLE),
    ("deviates", [rp.VIOLATED, rp.INCONCLUSIVE], rp.UNREPLAYABLE),
])
async def test_the_classification_table(monkeypatch, claimed, outcomes, expected):
    from dataclasses import replace as _replace
    monkeypatch.setattr(rp, "replay", _fake(outcomes))
    claim = _replace(CLAIM, verdict=claimed)
    res = await rp.differential("emulator-5554", "com.x", claim, ["star-not-persisted"])
    assert res.classification == expected


@pytest.mark.asyncio
async def test_a_claim_without_a_repro_is_unreplayable_not_false():
    bare = Claim(area="star_card", verdict="deviates")
    res = await rp.differential("emulator-5554", "com.x", bare, ["b"])
    assert res.classification == rp.UNREPLAYABLE


@pytest.mark.asyncio
async def test_the_clean_pass_runs_with_no_flags(monkeypatch):
    """The clean build is the SAME apk with an empty flags file. If the second reset
    kept the defects live, every differential would collapse to NOT_A_DEFECT."""
    seen = []

    async def _reset(serial, bundle, ids, snap=None, shared=None, shared_snap=None,
                     **kw):
        seen.append(list(ids))
        return True
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _fake([rp.VIOLATED, rp.HOLDS]))
    await rp.differential("emulator-5554", "com.x", CLAIM, ["star-not-persisted"])
    assert seen == [["star-not-persisted"], []]


@pytest.mark.asyncio
async def test_a_holding_expectation_skips_the_second_replay(monkeypatch):
    """Half the device time is saved when a claim does not reproduce at all."""
    calls = []

    async def _reset(serial, bundle, ids, snap=None, shared=None, shared_snap=None,
                     **kw):
        calls.append(list(ids))
        return True
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _fake([rp.HOLDS]))
    await rp.differential("emulator-5554", "com.x", CLAIM, ["b"])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_episode_replays_every_claim(monkeypatch):
    monkeypatch.setattr(rp, "replay", _fake([rp.VIOLATED, rp.HOLDS] * 3))
    claims = [CLAIM, CLAIM, CLAIM]
    out = await rp.replay_episode("emulator-5554", "com.x", claims, ["b"])
    assert [r.classification for r in out] == [rp.CONFIRMED] * 3


# ── the property that makes this worth building ──────────────────────────────

@pytest.mark.asyncio
async def test_a_correct_finding_against_a_WRONG_key_is_confirmed(monkeypatch):
    """Differential replay never consults the key, so an agent that reports a
    genuinely broken "control" is credited."""
    monkeypatch.setattr(rp, "replay", _fake([rp.VIOLATED, rp.HOLDS]))
    claim = Claim(area="parentheses", verdict="deviates",
                  steps=[Step("launch"), Step("tap", "(")],
                  expect=Expectation("present", "20"))
    res = await rp.differential("emulator-5554", "com.x", claim, ["multiply-wrong"])
    assert res.classification == rp.CONFIRMED


@pytest.mark.asyncio
async def test_a_fabricated_finding_is_not_confirmed(monkeypatch):
    """The claim names a real area but the repro does not show what was reported."""
    monkeypatch.setattr(rp, "replay", _fake([rp.HOLDS]))
    res = await rp.differential("emulator-5554", "com.x", CLAIM, ["b"])
    assert res.classification == rp.DOES_NOT_REPRODUCE


def test_a_parsed_submission_feeds_the_replayer_directly():
    """No adapter layer between the contract and the replayer — the schema an agent
    writes is the schema that gets executed."""
    sub = parse("""
findings:
  - area: star_card
    verdict: deviates
    steps: [launch, {tap: "QA-Card-01"}, {press: back}, relaunch]
    expect: {present: "★"}
""", known_areas={"star_card"})
    c = sub.claims[0]
    assert c.replayable
    assert [s.action for s in c.steps] == ["launch", "tap", "press", "relaunch"]
    assert c.expect.mode == "present"


def test_results_serialise_for_the_evidence_bundle():
    r = rp.DifferentialResult("a", "deviates", rp.CONFIRMED,
                              rp.ReplayResult(rp.VIOLATED, "x", 3),
                              rp.ReplayResult(rp.HOLDS, "y", 3))
    import json
    d = r.as_dict()
    assert json.dumps(d)
    assert d["classification"] == rp.CONFIRMED and d["claimed"] == "deviates"


@pytest.mark.asyncio
async def test_the_snapshot_is_restored_before_each_replay(monkeypatch, tmp_path):
    """Sample data is randomised on first run, so without restoring the episode's
    own snapshot every repro referencing existing content is unreplayable."""
    snap = tmp_path / "app_snapshot.tar"
    snap.write_bytes(b"x" * 1024)
    seen = []

    async def _reset(serial, bundle, ids, s=None, shared=None, shared_snap=None,
                     **kw):
        seen.append((list(ids), s))
        return True
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _fake([rp.VIOLATED, rp.HOLDS]))
    await rp.differential("emulator-5554", "com.x", CLAIM, ["b"], snap)
    assert [s for _, s in seen] == [snap, snap]


# ── expectation matching is whole-token, not substring ───────────────────────

def _screen(*texts):
    nodes = "".join(f'<node text="{t}" bounds="[0,0][100,100]"/>' for t in texts)
    return f"<hierarchy>{nodes}</hierarchy>"


@pytest.mark.parametrize("screen,want,expected", [
    # `present "20"` must not match the input expression still on screen.
    (["200×10%"], "20", False),
    (["200×10%", "20"], "20", True),
    # Boundaries are non-alphanumeric, so a result inside an expression still matches.
    (["12×12=144"], "144", True),
    (["9-4"], "9-4", True),
    # A longer list preview still contains the entry the agent created.
    (["QA test entry — some body"], "QA test entry", True),
    (["QA test entryX"], "QA test entry", False),
    (["★ Favourite"], "★", True),
    ([""], "x", False),
])
def test_expectation_matching_is_whole_token(screen, want, expected):
    assert rp._present(_screen(*screen), want) is expected


def test_content_desc_also_satisfies_an_expectation():
    xml = '<hierarchy><node text="" content-desc="Saved" bounds="[0,0][10,10]"/></hierarchy>'
    assert rp._present(xml, "Saved") is True


# ── Layer A: truth derived by execution (truth.py) ───────────────────────────

@pytest.mark.parametrize("on,off,expected", [
    (rp.VIOLATED, rp.HOLDS, "broken"),        # only the seeding breaks it
    (rp.HOLDS, rp.HOLDS, "ok"),               # a genuine working control
    (rp.VIOLATED, rp.VIOLATED, "upstream"),   # the original app behaves this way
    # The seeding FIXES the area — the check or the patch is inverted, and no score
    # from the episode is safe. Invisible unless the clean pass always runs.
    (rp.HOLDS, rp.VIOLATED, "inverted"),
    (rp.INCONCLUSIVE, rp.HOLDS, "undecidable"),
    (rp.VIOLATED, rp.INCONCLUSIVE, "undecidable"),
])
def test_truth_is_read_off_the_difference(on, off, expected):
    from qualgentbench import truth
    got = truth.classify(rp.ReplayResult(on), rp.ReplayResult(off))
    assert got == expected


def test_a_check_states_how_to_test_not_whether_it_works():
    """The spec declares an oracle, never a verdict — asserting `state:` instead
    of measuring it is where scoring failures came from."""
    from qualgentbench.truth import check_of
    claim = check_of({"id": "percent", "state": "broken",
                      "check": {"steps": ["launch", {"tap": "%"}],
                                "expect": {"present": "20"}}})
    assert claim is not None and claim.replayable
    assert [s.action for s in claim.steps] == ["launch", "tap"]
    assert claim.expect.mode == "present"
    # An area with no check yields nothing to derive rather than a wrong label.
    assert check_of({"id": "x", "state": "ok"}) is None
    assert check_of({"id": "x", "check": {"steps": [], "expect": {}}}) is None


def test_every_calculator_area_carries_a_check():
    """Guards against a check being dropped or malformed in a spec edit."""
    from pathlib import Path
    from qualgentbench.bugs import load_suite
    from qualgentbench.truth import check_of
    suite = load_suite(Path("src/qualgentbench/data/benchmarks/fossify-calculator.yaml"))
    for feature in suite["exploration"]["features"]:
        assert check_of(feature) is not None, f"{feature['id']} lost its check"


def test_check_setup_is_harness_only_and_never_reaches_the_agent():
    """`check_setup:` navigates the harness past first-run flows; `device_setup`
    prepares the app for the agent. Tapping through onboarding is the agent's work."""
    from pathlib import Path
    from qualgentbench.bugs import load_suite
    from qualgentbench.truth import setup_of
    suite = load_suite(Path("src/qualgentbench/data/benchmarks/birday.yaml"))
    exploration = suite["exploration"]
    steps = setup_of(exploration)
    assert steps and steps[0].action == "tap"
    assert "check_setup" not in exploration["instruction"]
    assert not exploration.get("device_setup"), (
        "birday's onboarding belongs in check_setup; device_setup would hand every "
        "agent a free pass through it")
    # An app that opens straight onto its own screen declares nothing.
    assert setup_of({}) == []


def test_every_easy_area_with_a_check_parses():
    """A malformed check is silently skipped (check_of returns None), which reads as
    'this area has no oracle' rather than as an error — so the count is asserted."""
    from pathlib import Path
    from qualgentbench.bugs import load_suite
    from qualgentbench.truth import check_of
    expected = {"fossify-calculator": 8, "birday": 8, "pf-shopping-list": 8,
                "opencalc": 10, "pftodo": 9, "easynotes": 9}
    for app_id, want in expected.items():
        suite = load_suite(Path(f"src/qualgentbench/data/benchmarks/{app_id}.yaml"))
        got = sum(1 for f in suite["exploration"]["features"] if check_of(f))
        assert got == want, f"{app_id}: {got} parseable checks, expected {want}"


def test_a_substring_match_is_only_tapped_when_clickable():
    """A wrong tap that SUCCEEDS is worse than an honest failure — an anchor that
    resolves only to a non-clickable substring must come back unreplayable."""
    xml = ('<hierarchy><node text="Next event" clickable="false" '
           'bounds="[0,100][500,200]"/></hierarchy>')
    assert rp._target(xml, "NEXT") is None

    clickable = ('<hierarchy><node text="Next event" clickable="true" '
                 'bounds="[0,100][500,200]"/></hierarchy>')
    assert rp._target(clickable, "NEXT") is not None       # actionable, so allowed

    exact = ('<hierarchy><node text="NEXT" clickable="false" '
             'bounds="[0,100][500,200]"/></hierarchy>')
    assert rp._target(exact, "NEXT") is not None           # exact still wins


# birday's home screen, reduced to the two nodes that carry the same event name:
# the "Next event" header card and the list row.
BIRDAY_TWO_NODES = """<hierarchy>
  <node resource-id="com.minar.birday:id/homeCard" clickable="true"
        bounds="[16,186][1064,602]">
    <node resource-id="com.minar.birday:id/upcomingTitle" text="QAEditOriginal"
          clickable="false" bounds="[48,239][795,310]"/>
  </node>
  <node class="android.view.ViewGroup" clickable="true" bounds="[0,817][1080,999]">
    <node resource-id="com.minar.birday:id/eventPerson" text="QAEditOriginal"
          clickable="false" bounds="[181,838][907,911]"/>
  </node>
</hierarchy>"""


def test_equal_rank_ties_break_toward_the_smaller_clickable_container():
    """Two exact matches are not interchangeable — the smaller clickable container
    is the more specific control, so the row must beat the header card."""
    x, y = rp._target(BIRDAY_TWO_NODES, "QAEditOriginal")
    assert 817 < y < 999                                    # the row, not the card

    cands = rp._candidates(BIRDAY_TWO_NODES, "QAEditOriginal")
    assert len(cands) == 2 and cands[0]["rank"] == cands[1]["rank"] == 2


def test_rank_still_dominates_ancestor_area():
    """A clickable exact match beats a non-clickable one however tiny the latter's
    container — specificity only breaks ties WITHIN a rank."""
    xml = """<hierarchy>
      <node clickable="true" bounds="[0,0][40,40]">
        <node text="Save" clickable="false" bounds="[10,10][30,30]"/>
      </node>
      <node text="Save" clickable="true" bounds="[0,2000][1080,2100]"/>
    </hierarchy>"""
    x, y = rp._target(xml, "Save")
    assert y > 1900                                         # the clickable node


def test_a_match_with_no_clickable_ancestor_still_resolves():
    xml = ('<hierarchy><node text="Lonely" clickable="false" '
           'bounds="[0,100][500,200]"/></hierarchy>')
    assert rp._target(xml, "Lonely") is not None


@pytest.mark.asyncio
async def test_the_retry_explores_the_other_anchor_candidate(monkeypatch):
    """An INCONCLUSIVE pass with an ambiguous anchor must not be retried with the
    identical choices — that can only fail identically."""
    seen: list[dict | None] = []

    async def _reset(serial, bundle, ids, snap=None, shared=None, shared_snap=None,
                     **kw):
        return True

    async def _replay(serial, bundle, steps, expect, choices=None):
        seen.append(choices)
        if choices:
            return rp.ReplayResult(rp.HOLDS, "", len(steps))
        return rp.ReplayResult(rp.INCONCLUSIVE, "step 2: no element", 1,
                               ambiguous=[0])
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _replay)

    result = await rp._pass("serial", "pkg", CLAIM, ["bug"], None)
    assert result.outcome == rp.HOLDS
    assert seen == [None, {0: 1}]


def test_keyboard_chrome_is_neither_a_tap_target_nor_an_oracle():
    """The IME draws its own clickable "Back" that can outrank the app's, and can
    echo typed text into an oracle — IME windows are dropped from every dump."""
    from qualgentbench.verify.device import _drop_windows

    xml = ('<hierarchy>'
           '<node content-desc="Back" package="com.kin.easynotes.debug" '
           'clickable="false" bounds="[43,181][106,244]"/>'
           '<node content-desc="Back" package="com.github.uiautomator" '
           'clickable="true" bounds="[71,2274][254,2400]"/>'
           '</hierarchy>')
    stripped = _drop_windows(xml, {"com.github.uiautomator"})
    assert "com.github.uiautomator" not in stripped
    assert "com.kin.easynotes.debug" in stripped
    x, y = rp._target(stripped, "Back")
    assert y < 300                                          # the app's toolbar Back

    assert _drop_windows("not xml <", {"com.github.uiautomator"}) == "not xml <"
    assert _drop_windows(xml, set()) == xml


def test_the_overlay_dismisser_only_taps_real_buttons():
    """Plain prose reading "OK" must not be tapped by the harness mid-replay; a
    Compose button whose text is a non-clickable child of the clickable surface
    still must be."""
    from qualgentbench.verify.match import find_button

    prose = ('<hierarchy><node text="OK" clickable="false" '
             'bounds="[0,100][500,200]"/></hierarchy>')
    assert find_button(prose, "ok") is None

    compose = ('<hierarchy><node clickable="true" bounds="[0,80][520,220]">'
               '<node text="OK" clickable="false" bounds="[0,100][500,200]"/>'
               '</node></hierarchy>')
    assert find_button(compose, "ok") is not None


@pytest.mark.asyncio
async def test_auto_dismissed_labels_land_in_the_replay_artifact(monkeypatch):
    async def _relaunch(serial, bundle):
        return ["allow"]

    async def _stable(serial, timeout_s=8):
        return True
    monkeypatch.setattr(rp, "relaunch", _relaunch)
    monkeypatch.setattr(rp, "wait_stable", _stable)
    monkeypatch.setattr(rp, "_SETTLE_S", 0)

    result = await rp.run_steps("serial", "pkg", [Step("launch")])
    assert result.dismissed == ["allow"]
    assert result.as_dict()["dismissed"] == ["allow"]


ROW = ('<hierarchy><node text="Save" clickable="true" '
       'bounds="[0,800][1080,1000]"/></hierarchy>')
ROW_AND_MENU = ('<hierarchy><node text="Save" clickable="true" '
                'bounds="[0,800][1080,1000]"/>'
                '<node text="Confirm" clickable="true" '
                'bounds="[0,1200][1080,1400]"/></hierarchy>')


@pytest.mark.asyncio
async def test_a_swallowed_gesture_is_reissued_once(monkeypatch):
    """Android drops touches during relayout. When the next anchor is missing and
    the previous gesture's anchor sits untouched, the gesture is re-issued once
    and the re-issue is recorded."""
    taps: list[str] = []
    state = {"landed": False}

    async def _dump(serial):
        return ROW_AND_MENU if state["landed"] else ROW

    async def _adb(serial, *args):
        taps.append(" ".join(args))
        # The first tap on Save is swallowed; the re-issue lands and Confirm appears.
        if "tap" in args and taps.count(" ".join(args)) >= 2:
            state["landed"] = True
        return 0, b""

    async def _fast(serial, timeout_s=8):
        return True
    monkeypatch.setattr(rp, "dump_vh", _dump)
    monkeypatch.setattr(rp, "_adb", _adb)
    monkeypatch.setattr(rp, "wait_stable", _fast)
    monkeypatch.setattr(rp, "_SETTLE_S", 0)

    result = await rp.run_steps("serial", "pkg",
                                [Step("tap", "Save"), Step("tap", "Confirm")])
    assert result.outcome == rp.HOLDS
    assert result.reissued == [0]
    assert result.as_dict()["reissued_steps"] == [0]


@pytest.mark.asyncio
async def test_a_gesture_that_moved_the_ui_is_never_reissued(monkeypatch):
    """When the previous gesture's anchor is gone (or moved), the gesture DID land
    and re-issuing it would double-act — the miss must be reported as before."""
    gone = ('<hierarchy><node text="Something else" clickable="true" '
            'bounds="[0,100][500,300]"/></hierarchy>')
    screens = {"n": 0}

    async def _dump(serial):
        screens["n"] += 1
        return ROW if screens["n"] == 1 else gone

    async def _adb(serial, *args):
        return 0, b""

    async def _fast(serial, timeout_s=8):
        return True
    monkeypatch.setattr(rp, "dump_vh", _dump)
    monkeypatch.setattr(rp, "_adb", _adb)
    monkeypatch.setattr(rp, "wait_stable", _fast)
    monkeypatch.setattr(rp, "_SETTLE_S", 0)

    result = await rp.run_steps("serial", "pkg",
                                [Step("tap", "Save"), Step("tap", "Confirm")])
    assert result.outcome == rp.INCONCLUSIVE
    assert result.reissued == []


@pytest.mark.asyncio
async def test_back_after_type_is_skipped_when_no_keyboard_is_shown(monkeypatch):
    """With no keyboard up, a literal back would dismiss the sheet — an action
    the agent never took. The step's intended effect already holds, so it is
    skipped and recorded."""
    pressed: list[str] = []

    async def _no_ime(serial):
        return False

    async def _press(serial, key):
        pressed.append(key)

    async def _type(serial, text):
        return None

    async def _fast(serial, timeout_s=8):
        return True
    monkeypatch.setattr(rp, "ime_shown", _no_ime)
    monkeypatch.setattr(rp, "_press", _press)
    monkeypatch.setattr(rp, "_type_text", _type)
    monkeypatch.setattr(rp, "wait_stable", _fast)
    monkeypatch.setattr(rp, "_SETTLE_S", 0)

    result = await rp.run_steps("serial", "pkg",
                                [Step("type", "hello"), Step("press", "back")])
    assert result.outcome == rp.HOLDS
    assert pressed == []
    assert result.back_noops == [1]
    assert result.as_dict()["back_noop_steps"] == [1]

    # With the keyboard up, back is delivered — it closes the keyboard.
    async def _ime_up(serial):
        return True
    monkeypatch.setattr(rp, "ime_shown", _ime_up)
    result = await rp.run_steps("serial", "pkg",
                                [Step("type", "hello"), Step("press", "back")])
    assert pressed == ["back"] and result.back_noops == []

    # back NOT preceded by type is always a real navigation.
    result = await rp.run_steps("serial", "pkg", [Step("press", "back")])
    assert pressed == ["back", "back"]


@pytest.mark.asyncio
async def test_pass_keeps_the_furthest_attempt(monkeypatch):
    """A bumped candidate can stop EARLIER than the default did; the artifact must
    show the most-progressed failure point, not whichever attempt ran last."""
    outcomes = [rp.ReplayResult(rp.INCONCLUSIVE, "step 12: no element", 11,
                                ambiguous=[9]),
                rp.ReplayResult(rp.INCONCLUSIVE, "step 11: no element", 10,
                                ambiguous=[9])]
    calls = {"n": 0}

    async def _reset(serial, bundle, ids, snap=None, shared=None, shared_snap=None,
                     **kw):
        return True

    async def _replay(serial, bundle, steps, expect, choices=None):
        out = outcomes[min(calls["n"], 1)]
        calls["n"] += 1
        return out
    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _replay)

    result = await rp._pass("serial", "pkg", CLAIM, ["bug"], None)
    assert result.steps_run == 11 and "step 12" in result.detail


@pytest.mark.asyncio
async def test_reset_mirrors_episode_staging(monkeypatch):
    """Replay must start from the world episode staging built, not a subset of it:
    a skipped device_setup means an empty media store, a skipped isolation means a
    `press: back` can land in another benchmark app still on the back stack."""
    calls: list[str] = []

    async def _adb(serial, *args):
        calls.append(" ".join(args))
        return 0, b""

    async def _grants(serial, bundle):
        calls.append("grants")
        return 0

    async def _setup(serial, spec):
        calls.append("device_setup")

    async def _isolate(serial, bundle):
        calls.append("isolate")

    async def _flags(serial, bundle, ids):
        calls.append("flags")
        return True
    monkeypatch.setattr(rp, "_adb", _adb)
    monkeypatch.setattr(rp, "grant_requested_permissions", _grants)
    monkeypatch.setattr(rp, "set_flags", _flags)
    import qualgentbench.episode_runner as er
    monkeypatch.setattr(er, "run_device_setup", _setup)
    monkeypatch.setattr(er, "isolate_app_under_test", _isolate)

    assert await _REAL_RESET("serial", "pkg", ["bug"],
                             device_setup={"shell": ["cmd"]})
    assert "device_setup" in calls and "isolate" in calls
    assert calls.index("device_setup") < calls.index("isolate") < calls.index("flags")


@pytest.mark.asyncio
async def test_type_SETS_the_field_and_append_delivers_keystrokes(monkeypatch):
    """`type` must mean what it meant when the agent wrote it: SET the field, not
    append at the cursor. `append` still delivers keystrokes."""
    calls = []

    async def _set(serial, text):
        calls.append(("set", text))

    async def _append(serial, text):
        calls.append(("append", text))

    async def _ok(*a, **k):
        return True

    monkeypatch.setattr(rp, "set_focused_text", _set)
    monkeypatch.setattr(rp, "append_text", _append)
    monkeypatch.setattr(rp, "relaunch", _ok)
    monkeypatch.setattr(rp, "wait_stable", _ok)

    await rp.run_steps("serial", "pkg",
                       [Step("type", "01/15/1990"), Step("append", "abc")])

    assert calls == [("set", "01/15/1990"), ("append", "abc")]


@pytest.mark.asyncio
async def test_shared_storage_is_restored_between_passes(monkeypatch, tmp_path):
    """`pm clear` does not touch /sdcard, so shared storage must be restored or
    the second pass starts from whatever the first pass wrote."""
    shared_snap = tmp_path / "shared_snapshot.tar"
    shared_snap.write_bytes(b"x" * 1024)
    paths = ["/sdcard/Documents/markor"]
    seen = []

    async def _reset(serial, bundle, ids, snap=None, shared=None, shared_snap=None,
                     **kw):
        seen.append((shared, shared_snap))
        return True

    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _fake([rp.VIOLATED, rp.HOLDS]))
    await rp.differential("emulator-5554", "com.x", CLAIM, ["b"], None,
                          paths, shared_snap)

    # BOTH passes, not just the first: restoring only before the seeded pass would
    # leave the clean pass reading the seeded pass's leftovers.
    assert seen == [(paths, shared_snap), (paths, shared_snap)]


@pytest.mark.asyncio
async def test_an_app_without_shared_storage_is_unaffected(monkeypatch):
    """Apps that keep everything in their sandbox must not pay for this."""
    seen = []

    async def _reset(serial, bundle, ids, snap=None, shared=None, shared_snap=None,
                     **kw):
        seen.append((shared, shared_snap))
        return True

    monkeypatch.setattr(rp, "_reset", _reset)
    monkeypatch.setattr(rp, "replay", _fake([rp.VIOLATED, rp.HOLDS]))
    await rp.differential("emulator-5554", "com.x", CLAIM, ["b"])
    assert seen == [(None, None), (None, None)]


# ── replay setup must reproduce EPISODE setup, app-ops included ──────────────

@pytest.mark.asyncio
async def test_a_special_app_op_is_regranted_after_pm_clear(monkeypatch):
    """`pm clear` resets app-ops and `pm grant` cannot set MANAGE_EXTERNAL_STORAGE —
    a reset that omits any step of episode setup runs a different app than the
    agent did."""
    from qualgentbench.verify import device as dev

    calls = []
    dump = (b"requested permissions:\n"
            b"  android.permission.MANAGE_EXTERNAL_STORAGE\n"
            b"  android.permission.POST_NOTIFICATIONS\n"
            b"install permissions:\n")

    async def _adb(serial, *args):
        calls.append(args)
        return 0, dump if args[:2] == ("shell", "dumpsys") else b""

    monkeypatch.setattr(dev, "_adb", _adb)
    await dev.grant_requested_permissions("emulator-5554", "net.gsantner.markor")

    appops = [c for c in calls if "appops" in c]
    assert appops, "MANAGE_EXTERNAL_STORAGE must be re-granted as an app-op"
    assert appops[0] == ("shell", "appops", "set", "net.gsantner.markor",
                         "MANAGE_EXTERNAL_STORAGE", "allow")


@pytest.mark.asyncio
async def test_no_app_op_call_for_an_app_that_does_not_declare_it(monkeypatch):
    """Apps that do not declare it must not pay an extra adb round trip."""
    from qualgentbench.verify import device as dev

    calls = []
    dump = b"requested permissions:\n  android.permission.POST_NOTIFICATIONS\n"

    async def _adb(serial, *args):
        calls.append(args)
        return 0, dump if args[:2] == ("shell", "dumpsys") else b""

    monkeypatch.setattr(dev, "_adb", _adb)
    await dev.grant_requested_permissions("emulator-5554", "com.example.plain")
    assert not [c for c in calls if "appops" in c]


# ── the end-of-run pass re-derives only what is stale ────────────────────────

def test_the_replayer_fingerprint_covers_every_file_that_changes_a_verdict():
    """If a file can change what a replay concludes, editing it must invalidate
    existing replay.json stamps — otherwise the board silently mixes two replayers."""
    from pathlib import Path

    here = Path(rp.__file__).resolve().parent
    for rel in rp._FINGERPRINT_SOURCES:
        assert (here / rel).resolve().exists(), f"fingerprint source missing: {rel}"

    before = rp.replayer_fingerprint()
    assert before == rp.replayer_fingerprint(), "fingerprint must be deterministic"
    assert len(before) == 16


def test_a_changed_replayer_changes_the_fingerprint(tmp_path, monkeypatch):
    """The saving is only safe if a real edit is actually detected."""
    import hashlib
    from pathlib import Path

    here = Path(rp.__file__).resolve().parent
    real = rp.replayer_fingerprint()

    # Same computation with one source's bytes perturbed — stands in for an edit.
    h = hashlib.sha256()
    for i, rel in enumerate(rp._FINGERPRINT_SOURCES):
        p = (here / rel).resolve()
        data = p.read_bytes() if p.exists() else b""
        h.update(data + (b"# edit" if i == 0 else b""))
    assert h.hexdigest()[:16] != real, "an edited replayer must not reuse stale stamps"


def test_the_progress_label_finds_the_counter_that_appears_LATE(tmp_path):
    """`hooks/` appears well after set_run_dir, so the counter must be resolved
    on later polls, not cached once as missing."""
    import time

    from qualgentbench.cli import _EpisodeProgress

    run = tmp_path / "run"
    run.mkdir()
    p = _EpisodeProgress(None, "birday · raw", 500)
    p._started = time.monotonic()
    p.set_run_dir(run)                       # hooks/ does not exist yet
    assert "starting…" in p._text()

    (run / "hooks").mkdir()
    (run / "hooks" / "count").write_text("154")
    assert "154/500 steps" in p._text()      # resolved on a later poll

    (run / "hooks" / "count").write_text("311")
    assert "311/500 steps" in p._text()


def test_the_progress_label_also_finds_the_codex_layout(tmp_path):
    """codex nests its counter under codex_home; both layouts must resolve late."""
    import time

    from qualgentbench.cli import _EpisodeProgress

    run = tmp_path / "run"
    (run / "codex_home" / "hooks").mkdir(parents=True)
    p = _EpisodeProgress(None, "markor · raw", 500)
    p._started = time.monotonic()
    p.set_run_dir(run)
    (run / "codex_home" / "hooks" / "count").write_text("42")
    assert "42/500 steps" in p._text()


def test_one_output_line_per_replayed_reproduction():
    """The status line counts one `→` line per replayed claim and parses nothing
    else, so the replayer must print exactly one such line per reproduction."""
    from pathlib import Path

    src = (Path(rp.__file__).resolve().parents[2]
           / "scripts" / "replay_findings.py").read_text()
    progress = [l for l in src.splitlines() if "→" in l and "print(" in l]
    assert len(progress) == 1, (
        "the status line counts '→' lines as reproductions; more than one such print "
        "would over-count progress")


def test_a_missing_answer_key_is_never_reported_as_VERIFIED():
    """Derived truth is a corpus artifact: it lives beside the specs, not under
    generated `runs/`, and a missing key reports UNSCORED rather than clean."""
    from pathlib import Path

    import qualgentbench.cli as cli_mod

    src = Path(cli_mod.__file__).read_text()
    assert 'root / "runs" / "_truth"' not in src, (
        "derived truth must not live under runs/ — that directory is generated output")
    assert '"data" / "truth"' in src
    assert "UNSCORED" in src, "a missing answer key must not read as a clean result"

    # And the writer agrees with the readers.
    root = Path(cli_mod.__file__).resolve().parents[2]
    derive = (root / "scripts" / "derive_truth.py").read_text()
    assert '"data" / "truth"' in derive, "derive_truth writes where the readers look"


@pytest.mark.asyncio
async def test_set_focused_text_falls_back_when_the_field_ignores_ACTION_SET_TEXT(monkeypatch):
    """u2's set_text can report success on fields that ignore ACTION_SET_TEXT, so
    the text must be verified and the keystroke fallback run — never assume it
    landed."""
    from qualgentbench.verify import device as dev

    calls = []

    def _fake_u2(serial, text):
        calls.append(("u2", text))
        return False                      # field ignored it — as notally's body did

    async def _adb(serial, *args):
        calls.append(("adb", " ".join(args)))
        return 0, b""

    monkeypatch.setattr(dev, "_u2_set_focused_text", _fake_u2)
    monkeypatch.setattr(dev, "_adb", _adb)
    await dev.set_focused_text("serial", "QABODY")

    assert calls[0] == ("u2", "QABODY")
    joined = " ".join(a for k, a in calls if k == "adb")
    assert "KEYCODE_MOVE_END" in joined and "KEYCODE_DEL" in joined, "must clear first"
    assert "input text QABODY" in joined, "must then type the value"
