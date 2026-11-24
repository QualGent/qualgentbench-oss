"""The customer-shaped report of a saved episode (QUA-3585): which source it is built
from, how one tool call is matched to one of several reports, and what `quality.json`
records. The field mapping itself is pinned by `test_report_mapping_fixture.py`."""

from __future__ import annotations

import json

import pytest
from test_mcp_scoring_parity import Call, _case, _claude, _codex, _pair
from test_report_quality import _episode

from qualgentbench import journey
from qualgentbench import report_quality as rq
from qualgentbench.transcript import TranscriptParser

FORMATS = {"claude": _claude, "codex": _codex}
INVESTIGATION = "SaveNote returns before the row is written."


def _saved(tmp_path, metrics: dict, transcript: str = "", name: str = "ep1"):
    d = _episode(tmp_path, name, metrics)
    (d / rq.TRANSCRIPT_FILE).write_text(transcript)
    return d


def _entry(step, observed, expected, description, screen=""):
    return {"step": step, "screen": screen, "observed": observed, "expected": expected,
            "description": description, "matched": None, "grounded": False, "known": None,
            "consequence_of": None}


def _fail(**over) -> dict:
    return {"status": "FAIL", "summary": "Saving a note loses it",
            "failure_step": "Step 3: Save", "expected": "The note is listed",
            "actual": "The note is missing", "code_investigation": INVESTIGATION,
            "steps": [{"step": 3, "status": "failed"}], **over}


def _report_call(args: dict, *, refused: bool = False) -> Call:
    return Call(rq.REPORT_TOOL, args, "refused" if refused else '{"ok": true}',
                is_error=refused)


# ── reading the transcript ─────────────────────────────────────────────────────

@pytest.mark.parametrize("version", ["clean", "seeded"])
def test_report_tool_calls_agree_with_the_scorers_parser(version):
    """Same calls, same order, same refusal flag as `TranscriptParser`, in both formats."""
    for transcript in _pair(version):
        expected = [(e.input, e.is_error)
                    for e in TranscriptParser(transcript).report_tool_calls()]
        assert rq.report_tool_calls(transcript) == expected
        assert [refused for _, refused in expected] == [True, False]


def test_a_call_named_only_in_text_or_another_tool_is_not_a_report_call():
    transcript = _claude([
        Call("mobile_tap", {"text": "mobile_report_result"}, "Tapped."),
        Call("mobile_observe_screen", {}, "call mobile_report_result when done"),
    ]) + "not json mobile_report_result\n"
    assert rq.report_tool_calls(transcript) == []


# ── the source of each report ──────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_the_accepted_tool_call_is_the_customer_report(tmp_path, fmt):
    """An episode scored by the real scorer from its report tool call (no findings
    file): the customer-shaped report is the product's bug for that call."""
    claude, codex = _pair("seeded")
    transcript = {"claude": claude, "codex": codex}[fmt]
    metrics = journey.journey_verdict(transcript, "m", _case("seeded", "report_tool")).metrics
    assert metrics["report_source"] == "report_tool" and len(metrics["reports"]) == 1

    ep = rq.load_episode(_saved(tmp_path, metrics, transcript))
    (report,) = rq.customer_report(ep)
    assert report.as_dict() == {
        "summary": "the stock button opened the medicine settings, not the stock screen",
        "steps": "no source available",            # code_investigation, not the summary
        "expected": "Amount", "actual": "Color", "failure_step": "3",
        "agent_steps": [], "repro_steps": [],
        "report_source": "report_tool", "paired_by": "only_report"}

    q = rq.score_episode(ep.dir)
    assert q["scorer_report_source"] == "report_tool" and "report_source" not in q
    assert q["reports"][0]["customer_report"] == report.as_dict()


def test_the_tool_call_wins_over_the_findings_entry_it_describes(tmp_path):
    """Findings are the scorer's report of record; the customer reads the tool call."""
    claude, _ = _pair("seeded")
    metrics = journey.journey_verdict(claude, "m", _case("seeded", "findings_file")).metrics
    assert metrics["report_source"] == "findings_file"
    (report,) = rq.customer_report(rq.load_episode(_saved(tmp_path, metrics, claude)))
    assert report.report_source == rq.SOURCE_REPORT_TOOL
    assert report.steps == "no source available"


def test_no_tool_call_renders_every_findings_entry(tmp_path):
    long = "The total is wrong. " * 20
    metrics = {"report_source": "findings_file", "reports": [
        _entry(4, "Total: 4 items", "Total: 3 items", "the total counts a deleted row",
               screen="Cart"),
        _entry(None, long, long, ""),
        _entry(2, "", "Only expected given", "password: hunter2 is shown in clear"),
    ]}
    for transcript in ("", _claude([Call("mobile_tap", {"text": "Cart"}, "Tapped.")])):
        first, second, third = rq.customer_report(
            rq.load_episode(_saved(tmp_path, metrics, transcript, name=f"e{len(transcript)}")))
        assert first.as_dict() == {
            "summary": "the total counts a deleted row", "steps": "",
            "expected": "Total: 3 items", "actual": "Total: 4 items",
            "failure_step": "Step 4: Cart", "agent_steps": [], "repro_steps": [],
            "report_source": "findings", "paired_by": None}
        # The product's rules apply to a rendered entry too: default title, 250 characters.
        assert second.summary == rq.DEFAULT_SUMMARY and second.failure_step is None
        assert len(second.expected) <= 250 and second.expected == long[:250].rstrip()
        # One side only stores neither; a credential is redacted.
        assert (third.expected, third.actual) == (None, None)
        assert third.summary == "password=[REDACTED] is shown in clear"
        assert third.failure_step == "Step 2"


def test_a_missing_transcript_is_no_tool_call(tmp_path):
    d = _saved(tmp_path, {"report_source": "findings_file",
                          "reports": [_entry(1, "a", "b", "c")]})
    (d / rq.TRANSCRIPT_FILE).unlink()
    (report,) = rq.customer_report(rq.load_episode(d))
    assert report.report_source == rq.SOURCE_FINDINGS


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_one_call_takes_one_report_and_the_rest_are_rendered(tmp_path, fmt):
    reports = [_entry(1, "Inbx", "Inbox", "the toolbar title is cut off"),
               _entry(3, "The note is missing", "The note is listed", "saving loses the note"),
               _entry(5, "Total: 4", "Total: 3", "the counter is off by one")]
    metrics = {"report_source": "findings_file", "reports": reports}

    def built(args, name):
        transcript = FORMATS[fmt]([_report_call(args)])
        return rq.customer_report(rq.load_episode(_saved(tmp_path, metrics, transcript, name)))

    # By the step number in failure_step.
    out = built(_fail(), "step")
    assert [r.report_source for r in out] == ["findings", "report_tool", "findings"]
    assert out[1].paired_by == "step" and out[1].steps == INVESTIGATION
    assert out[1].agent_steps == ({"step": 3, "status": "failed"},)
    assert [r.index for r in out] == [0, 1, 2]
    assert out[0].steps == "" and out[0].paired_by is None
    # No usable step: by shared words.
    out = built(_fail(failure_step="Checkout", summary="The counter shows the wrong total",
                      actual="Total: 4", expected="Total: 3"), "text")
    assert [r.report_source for r in out] == ["findings", "findings", "report_tool"]
    assert out[2].paired_by == "text"
    # Nothing in common: the first report.
    out = built({"status": "FAIL", "summary": "zzz", "code_investigation": "qqq"}, "first")
    assert (out[0].report_source, out[0].paired_by) == ("report_tool", "first")
    # Two reports at the named step: words break the tie.
    metrics["reports"] = [*reports, _entry(3, "Crash dialog", "No dialog", "app keeps stopping")]
    out = built(_fail(), "tie")
    assert [r.paired_by for r in out] == [None, "step", None, None]


def test_the_last_accepted_call_is_used_and_a_refused_one_never(tmp_path):
    metrics = {"report_source": "findings_file",
               "reports": [_entry(3, "The note is missing", "The note is listed", "lost")]}
    transcript = _codex([
        _report_call(_fail(summary="first")),
        _report_call(_fail(summary="second")),
        _report_call(_fail(summary="refused, without investigation"), refused=True),
    ])
    (report,) = rq.customer_report(rq.load_episode(_saved(tmp_path, metrics, transcript)))
    assert report.summary == "second"


@pytest.mark.parametrize("status", ["BLOCKED", "PASS"])
def test_a_call_that_files_no_bug_leaves_the_rendering(tmp_path, status):
    """The product files a bug for FAIL only. The scorer reads BLOCKED as a failing
    report, so its saved report is rendered and marked as the scorer's own."""
    args = _fail(status=status, blocker_investigation="account locked")
    transcript = _claude([_report_call(args)])
    by_scorer = {"findings_file": rq.SOURCE_FINDINGS, "report_tool": rq.SOURCE_SCORER_REPORT}
    for scorer_source, source in by_scorer.items():
        metrics = {"report_source": scorer_source,
                   "reports": [_entry(3, "The note is missing", "The note is listed", "lost",
                                      screen="Step 3: Save")]}
        (report,) = rq.customer_report(
            rq.load_episode(_saved(tmp_path, metrics, transcript, name=scorer_source)))
        assert (report.report_source, report.steps, report.paired_by) == (source, "", None)


def test_a_tool_call_with_no_saved_report_adds_none(tmp_path):
    metrics = {"report_source": "findings_file", "reports": []}
    d = _saved(tmp_path, metrics, _claude([_report_call(_fail())]))
    assert rq.customer_report(rq.load_episode(d)) == []
    assert rq.score_episode(d)["reports"] == []


# ── quality.json ───────────────────────────────────────────────────────────────

def test_quality_json_shows_the_customer_report_and_source_of_every_report(tmp_path):
    reports = [_entry(1, "Inbx", "Inbox", "the toolbar title is cut off"),
               {**_entry(3, "The note is missing", "The note is listed", "saving loses it"),
                "matched": "save-bug"},
               {**_entry(3, "Empty list", "One note", "list is empty after the save"),
                "consequence_of": "save-bug"}]
    d = _saved(tmp_path, {"report_source": "findings_file", "scorer_version": 5,
                          "reports": reports},
               _claude([_report_call(_fail())]))
    path = rq.write_quality(d)
    q = json.loads(path.read_text())
    assert q["scorer_report_source"] == "findings_file"
    assert [r["customer_report"]["report_source"] for r in q["reports"]] == [
        "findings", "report_tool", "findings"]
    for r in q["reports"]:                      # neutral reports included
        assert set(r["customer_report"]) == {
            "summary", "steps", "expected", "actual", "failure_step", "agent_steps",
            "repro_steps", "report_source", "paired_by"}
    assert q["reports"][1]["customer_report"]["agent_steps"] == [{"step": 3, "status": "failed"}]
    assert q["reports"][2]["neutral"] is True
    # A rescore writes the same bytes, and the memo does not leak between episodes.
    before = path.read_bytes()
    assert rq.write_quality(d).read_bytes() == before
