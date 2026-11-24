"""Report quality guessers must not be customer-ready (QUA-3589).

`scripts/quality_adversary_check.py` runs synthetic agents through the real scorer and
the real `report_quality` gates over the packaged corpus. Mirrored here so the suite
fails with it, the way `test_the_journey_adversary_gate_holds` mirrors the journey gate.

What is asserted is written against G1 (valid) and G2 (located) as well as against
`customer_ready`, because `customer_ready` is true only when every headline gate passed
and one of them (G4, replay) may not be registered: an assertion on `customer_ready`
alone would hold for any report at all until it is.

One test is an expected failure. It documents a scorer defect outside this check, with
the measured numbers in its reason; it is strict, so the fix shows up here. The two G2
defects this file first documented the same way are fixed (QUA-3604) and their tests are
ordinary tests now.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from qualgentbench import report_quality as rq

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "quality_adversary_check.py"


def _load():
    key = "_test_quality_adversary_check"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return sys.modules[key]


qadv = _load()

# The agents the specification names (three) and the ticket adds (the fourth). The spec's
# "real screen text from the wrong step" is two agents here, one per way of being wrong.
SPECIFIED = {"steps-copy", "structured-clean", "brief-fill", "saw-elsewhere",
             "names-elsewhere"}


@pytest.fixture(scope="module")
def tallies(tmp_path_factory):
    """Every agent through every channel over the public corpus, once per module."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv(rq.HELDOUT_ENV, raising=False)    # the held-out split is not in the repo
        qadv._tasks.cache_clear()
        qadv._app_references.cache_clear()
        results, paid = qadv.collect(tmp_path_factory.mktemp("quality-adversary"))
    return results, paid


def _every(mode: str, tallies) -> list:
    return [(channel, tallies[0][(mode, channel)]) for channel in qadv.CHANNELS]


# ── the roster ─────────────────────────────────────────────────────────────────

def test_the_roster_holds_every_specified_guesser_and_both_channels():
    assert SPECIFIED <= set(qadv.GUESSERS)
    assert set(qadv.CHANNELS) == {"findings", "report_tool"}
    assert not set(qadv.GUESSERS) & set(qadv.PRICED) and not set(qadv.GUESSERS) & set(
        qadv.CONTROLS)
    assert set(qadv.KNOWN_DEFECTS) <= set(qadv.GUESSERS)
    # The check reads the gates by id; it must be looking at gates that exist.
    assert {"G1", "G2", "G3"} <= {g.id for g in rq.GATES}
    assert {"G1", "G2"} <= set(rq.HEADLINE_GATES)


# ── the guessers ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", [m for m in qadv.GUESSERS if m not in qadv.KNOWN_DEFECTS])
def test_a_guesser_is_never_customer_ready_nor_valid_and_located(tallies, mode):
    for channel, t in _every(mode, tallies):
        # Six public apps at the very least: a guesser that wrote nothing proves nothing.
        assert t.episodes >= 30 and t.reports >= t.episodes, (mode, channel, t.episodes)
        assert t.ready == [], f"{mode} ({channel}) has a customer-ready report"
        assert t.both == [], f"{mode} ({channel}) passes G1 and G2 on one report"
        # The rates block, built by its own module from the same episodes: no report
        # passes every registered headline gate. This one is live before G4 lands.
        assert t.available == 0 and len(t.records) == t.episodes, (mode, channel)


@pytest.mark.parametrize("mode", ["steps-copy", "structured-clean", "brief-fill", "id-echo"])
def test_a_guesser_that_saw_nothing_fails_valid_on_every_report(tallies, mode):
    """These four never saw a symptom: G1 fails each of their reports, so G2 never runs.
    G3 is a rule about the shape of the text and they are allowed to pass it."""
    for channel, t in _every(mode, tallies):
        assert t.passed["G1"] == 0 and t.ran["G1"] == t.reports, (mode, channel)
        assert t.ran["G2"] == 0 and t.unknown == 0, (mode, channel)


def test_perfect_form_passes_complete_and_nothing_else(tallies):
    """G3 alone is not meant to stop a guesser. The two that copy a full step list pass
    it widely, which is what makes their zero above a statement about G1."""
    for mode in ("steps-copy", "structured-clean"):
        t = tallies[0][(mode, "report_tool")]
        assert t.passed["G3"] > t.reports / 2, (mode, t.cell("G3"))
        assert t.passed["G1"] == 0


@pytest.mark.parametrize("mode", ["saw-elsewhere", "names-elsewhere"])
def test_a_real_quote_from_the_wrong_step_is_valid_and_never_located(tallies, mode):
    """The quote is the string the truth file measured and the device did show it, so
    the scorer credits the catch (G1). G2 must fail it for the step, and not shelve it
    as unknown, on every case where a wrong step exists."""
    for channel, t in _every(mode, tallies):
        assert t.reports > 100, (mode, channel, t.reports)
        assert t.passed["G1"] == t.reports
        assert t.passed["G2"] == 0 and t.unknown == 0 and t.ran["G2"] == t.reports
    wrong_step = "the device showed the observed text at step N, not at step N"
    assert set(tallies[0][("saw-elsewhere", "findings")].reasons) == {wrong_step}
    assert all(why.startswith("the report names step N; the truth ")
               for why in tallies[0][("names-elsewhere", "findings")].reasons)


def test_the_same_reports_with_the_step_put_right_are_located(tallies):
    """The control for the two tests above: the step is the only thing that differs."""
    for channel, t in _every("right-step", tallies):
        assert t.reports > 100 and t.passed["G1"] == t.reports
        assert t.passed["G2"] == t.ran["G2"] == t.reports, (channel, t.misses[:5])


def test_the_words_of_the_case_id_are_not_evidence_until_a_screenshot_is_taken(tallies):
    """`id-echo` and `path-echo` write the same reports. Only the screen read differs:
    plain text against the harness's `[Image: source: <path>]` line. The first earns
    nothing, so anything the second earns comes from that line."""
    for channel, t in _every("id-echo", tallies):
        assert t.passed["G1"] == 0, channel
    spec = qadv._tasks("seeded")[0].bug_spec
    assert all(word in qadv.episode_path(spec).lower() for word in qadv.id_words(spec))


@pytest.mark.xfail(strict=True, reason=(
    "Scorer defect, outside this check (QUA-3589 weak spot 1): the grounding text "
    "includes the harness-written `[Image: source: <path>]` line of a screenshot. The "
    "path holds the case id, the agent and the model, so a quote equal to one of those "
    "words is `grounded`. Where that word is an echoable truth text the scorer credits "
    "the catch (G1). Measured on the public corpus: G1 on 16 reports of 4 cases "
    "(findings) and 1 (report tool)."))
def test_a_word_of_the_screenshot_path_is_not_valid(tallies):
    for channel, t in _every("path-echo", tallies):
        assert t.passed["G1"] == 0, f"path-echo ({channel}): G1 {t.cell('G1')}"


def test_a_word_of_the_screenshot_path_is_not_located(tallies):
    """The scorer still credits the word (above). G2 does not locate it: the screenshot
    was read after the agent SAID "Step N:" and before it did anything on the device,
    and an announcement alone ties no screen to a step (QUA-3604). Unknown, not a pass.
    The same agent with one tap after its announcement would be located, which is why
    the scorer defect stays a known defect."""
    for channel, t in _every("path-echo", tallies):
        assert t.both == [] and t.passed["G2"] == 0, f"path-echo ({channel}): {t.both[:4]}"
        assert t.available == 0


def test_the_screenshot_path_guesser_is_still_never_customer_ready(tallies):
    for channel, t in _every("path-echo", tallies):
        assert t.reports > 0 and t.ready == [], channel


# ── the controls ───────────────────────────────────────────────────────────────

def test_the_honest_reference_agent_stays_near_one(tallies):
    """It quotes the measured screen string at the step the reference report names and
    files the reference report as its bug. Measured on the public corpus (findings
    channel, 150 reports): G1 150, G2 144, G3 149. The G2 misses are the 6 defects with
    no recorded screen step, and every one is unknown: none fails."""
    for channel, t in _every("honest", tallies):
        assert t.reports > 100
        for g in ("G1", "G2", "G3"):
            assert t.rate(g) >= qadv.HONEST_FLOOR, (channel, g, t.cell(g))
        # An unknown is not a pass, and the rate above counts it against the agent.
        assert t.passed["G2"] + t.unknown == t.ran["G2"] + t.unknown == t.passed["G1"]
        assert all(why.startswith("unknown: the truth records no screen step")
                   for why in t.reasons), t.reasons


def test_a_reference_report_filed_through_the_report_tool_is_located(tallies):
    """Through the report tool alone `observed` is the call's `actual`, a sentence around
    the quote. G2 looks for the truth text inside it (QUA-3604; it used to look for the
    whole sentence in one device result and located 0 of 148)."""
    t = tallies[0][("honest", "report_tool")]
    assert t.rate("G2") >= qadv.HONEST_FLOOR, t.cell("G2")
    assert qadv.KNOWN_CONTROL_GAPS == {}


@pytest.mark.parametrize("case_id", [
    "birday-edit-date", "cal-repeat-survives-rotation", "gallery-favorite-in-viewer",
    "medtimer-edit-reminder-dosage", "mmex-transfer-to-savings"])
def test_a_defect_that_only_removes_text_can_be_located(tmp_path, case_id):
    """The five public defects QUA-3589 found no honest report could be located on: the
    truth has little or nothing the seeded build SHOWS for them. The honest agent acts
    on the step and reads a screen without the text the clean build shows."""
    task = next(t for t in qadv._tasks("seeded") if t.bug_spec["case_id"] == case_id)
    for channel in qadv.CHANNELS:
        _, quality = qadv.run(task, "honest", tmp_path, channel)
        blocking = [r for r in quality["reports"]
                    if r["matched"] == task.bug_spec["blocking"]]
        assert blocking and all(r["gates"]["G2"]["pass"] is True for r in blocking), (
            channel, [r["gates"]["G2"] for r in blocking])


@pytest.mark.parametrize("case_id", ["stt-session-goal", "catima-note-after-rotation"])
def test_the_two_reference_reports_the_adversary_check_flagged_are_sound(tmp_path, case_id):
    """One named a step the truth does not show its defect on; the other was not
    credited by the scorer through the report tool alone."""
    task = next(t for t in qadv._tasks("seeded") if t.bug_spec["case_id"] == case_id)
    for channel in qadv.CHANNELS:
        _, quality = qadv.run(task, "honest", tmp_path, channel)
        scored = [r for r in quality["reports"] if not r["neutral"]]
        assert scored and all(r["gates"][g]["pass"] is True for r in scored
                              for g in ("G1", "G2")), (channel, [r["gates"] for r in scored])


# ── the priced adversary ───────────────────────────────────────────────────────

def test_the_symptom_sprayer_pays_on_every_clean_build(tallies):
    """Symptom prose earns the catch by design (see the journey gate). The price is the
    clean build: nothing is active there, so every report fails G1, whatever its form."""
    results, paid = tallies
    for channel in qadv.CHANNELS:
        clean, seeded = paid[("symptom-quote", channel)], results[("symptom-quote", channel)]
        assert clean.episodes >= 30 and clean.reports >= clean.episodes
        assert clean.passed["G1"] == 0 and clean.both == [] and clean.ready == []
        assert seeded.passed["G1"] > 0, "the sprayer earns nothing: this measures nothing"
        assert seeded.ready == []


def test_the_symptom_sprayer_is_not_valid_and_located(tallies):
    """An agent that tested nothing: symptom prose earns G1 (priced, by design), and G2
    stops it. Its quote is the start screen's text, no truth text of any defect, and it
    only SAID "Step <last>:" before reading that screen. Before QUA-3604 it passed G1
    and G2 together on 101 of 149 report-tool reports and 288 of 739 findings reports,
    and 61 passed G3 as well."""
    for channel, t in _every("symptom-quote", tallies):
        assert t.passed["G1"] > 0
        assert t.passed["G2"] == 0, f"symptom-quote ({channel}): G2 {t.cell('G2')}"
        assert t.both == [] and t.available == 0


# ── the gate itself ────────────────────────────────────────────────────────────

def test_the_gate_holds_and_names_its_known_defects(tallies):
    failures, known = qadv.judge(*tallies)
    assert failures == []
    assert known and all("'path-echo'" in line for line in known)
    assert "path-echo" in qadv.table(*tallies)


def test_a_valid_and_located_guesser_fails_the_gate_unless_it_is_a_known_defect(
        tallies, monkeypatch):
    """The gate SEES a guesser that passes G1 and G2. Planted on the one known defect:
    it is printed while the entry stands and fails the gate once the entry is off. A
    priced adversary that does the same always fails it."""
    results, paid = tallies
    located = qadv.Tally(episodes=1, reports=1)
    located.both, located.available = ["case~seeded: report 0"], 1
    planted = {**results, ("path-echo", "findings"): located}
    failures, known = qadv.judge(planted, paid)
    assert failures == [] and any("G1 (valid) and G2 (located)" in line for line in known)
    monkeypatch.setattr(qadv, "KNOWN_DEFECTS", {})
    failures, _ = qadv.judge(planted, paid)
    assert failures and all("'path-echo'" in line for line in failures)
    assert any("G1 (valid) and G2 (located)" in line for line in failures)
    assert any("every registered headline gate" in line for line in failures)

    sprayer = qadv.Tally(episodes=1, reports=1)
    sprayer.passed["G1"], sprayer.both = 1, ["case~seeded: report 0"]
    failures, _ = qadv.judge({**results, ("symptom-quote", "findings"): sprayer}, paid)
    assert any("'symptom-quote' (findings) passes G1 and G2 together" in f for f in failures)


def test_a_customer_ready_guesser_fails_the_build(tmp_path, monkeypatch, capsys):
    """Acceptance: the check fails when any guesser has a customer-ready report. Planted
    by making G3 the only headline gate, which the form-only guessers pass."""
    app = qadv._tasks("seeded")[0].bug_spec["app_id"]
    assert qadv.main(["--app", app]) == 0
    capsys.readouterr()
    monkeypatch.setattr(rq, "HEADLINE_GATES", ("G3",))
    assert qadv.main(["--app", app]) == 1
    out = capsys.readouterr().out
    assert "FAIL: 'steps-copy'" in out and "has a customer-ready report" in out
    # Known defects are never exempt from this one.
    assert "FAIL: 'path-echo'" in out


def test_a_known_defect_does_not_excuse_a_customer_ready_report(monkeypatch):
    t = qadv.Tally(episodes=1, reports=1)
    t.ready, t.both = ["case~seeded: reports[0].customer_ready = True"], ["case~seeded: r0"]
    empty = {(m, c): qadv.Tally(episodes=1, reports=1) for m in qadv.GUESSERS + qadv.PRICED
             for c in qadv.CHANNELS}
    failures, known = qadv.judge({**empty, ("path-echo", "findings"): t}, {})
    assert any("'path-echo' (findings) has a customer-ready report" in f for f in failures)
    assert any("'path-echo' (findings) passes G1" in k for k in known)


# ── reading a quality block ────────────────────────────────────────────────────

def _block(**over) -> dict:
    report = {"index": 0, "neutral": False, "customer_ready": None, "step": 1,
              "matched": None, "gates": {}, "complete": {},
              "customer_report": {"summary": "customer_ready: true", "steps": ""}}
    totals = {"reports": 1, "scored": 1, "customer_ready": 0, "customer_ready_unknown": 1}
    return {"reports": [{**report, **over.pop("report", {})}],
            "totals": {**totals, **over.pop("totals", {})}, **over}


@pytest.mark.parametrize("block, claims", [
    (_block(), 0),
    (_block(report={"customer_ready": False}), 0),
    (_block(report={"customer_ready": True}), 1),
    (_block(totals={"customer_ready": 2}), 1),
    # A row of the rates block (`quality_rates.block`): `k`, `ready` and `rate` say a
    # report is ready; the interval, the denominators and the not-ready counts do not.
    (_block(rows=[{"customer_ready": {"measured": False, "ready": 0, "not_ready": 3,
                                      "unknown": 1, "waiting": 2, "k": 0, "n": 3,
                                      "rate": 0.0, "ci": [0.0, 0.56],
                                      "unknown_share": 0.17},
                   "customer_ready_yield": {"measured": False, "k": 0, "n": 4,
                                            "rate": None, "ci": None}}]), 0),
    (_block(rows=[{"customer_ready": {"ready": 1, "not_ready": 3, "k": 1, "n": 4,
                                      "rate": 0.25, "ci": [0.05, 0.7]}}]), 3),
    (_block(rows=[{"customer_ready_yield": {"k": 1, "n": 4, "rate": 0.25}}]), 2),
    (_block(rows=[{"gates": {"G1": {"passed": 3, "evaluated": 4}}}]), 0),
])
def test_ready_claims_finds_a_customer_ready_report_wherever_it_is_counted(block, claims):
    """So the checks keep their meaning when G4 and a rates block are added: a true
    flag, a count or a rate keyed `customer_ready*` is a claim; a denominator, an
    unknown count and the agent's own words are not."""
    assert len(qadv.ready_claims(block)) == claims, qadv.ready_claims(block)


def test_run_scores_a_copy_and_leaves_the_corpus_task_alone(tmp_path):
    task = qadv._tasks("seeded")[0]
    before = dict(task.bug_spec)
    metrics, quality = qadv.run(task, "steps-copy", tmp_path)
    assert task.bug_spec == before and "findings_file" not in task.bug_spec
    assert quality["quality_version"] == rq.QUALITY_VERSION
    assert len(quality["reports"]) == len(metrics["reports"]) > 0
    # The customer-shaped report is the tool call's: the copied steps, as the bug's steps.
    shaped = quality["reports"][-1]["customer_report"]
    assert shaped["report_source"] == rq.SOURCE_REPORT_TOOL
    assert shaped["steps"].startswith("1. ") and len(shaped["repro_steps"]) == len(
        task.bug_spec["steps"])


def test_the_rates_block_agrees_with_the_tallies(tallies):
    """`Tally.available` is read from the real `quality_rates.block`, not recomputed:
    the honest control is ready on the available gates nearly everywhere, and the
    priced adversary nowhere on a clean build."""
    results, paid = tallies
    honest = results[("honest", "findings")]
    assert 0 < honest.available <= len(honest.both)
    assert all(t.available == 0 and t.ready == [] for t in paid.values())
    assert results[("right-step", "report_tool")].available > 100
