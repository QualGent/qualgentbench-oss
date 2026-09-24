"""Journey report sources (QUA-2777): DevLoop's `mobile_report_result` as the fourth,
lowest-precedence report source.

DevLoop-MCP's own server instructions end every run with `mobile_report_result`; the
brief's contract is `findings.yaml` + a `RESULT:` line. Brief v3 says which one is the
report of record, and the scorer reads the tool when nothing else was written — through
the SAME matcher as a findings entry, and never as device evidence (QUA-2775: a reply
echoing the agent's own words must not back up its quote).
"""

from __future__ import annotations

import json

from qualgentbench import journey

from test_bugs import _call, _obs, _transcript
from test_journey import _bug, _spec, _task, _write


def _report_tool(result: str = "STATUS: FAIL", *, name: str = "mcp__device__mobile_report_result",
                 **args) -> str:
    args.setdefault("flow", "Case")
    args.setdefault("steps", ["✓ open", "✗ read"])
    return _call(name, args, result)


def _refused(**args) -> str:
    """A call the server REFUSED — Claude's `is_error` on the tool_result, as DevLoop
    answers a FAIL without `code_investigation`."""
    tid = "refused-1"
    call = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "mcp__device__mobile_report_result",
         "input": {"flow": "Case", "steps": [], **args}}]}})
    res = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": True,
         "content": [{"type": "text", "text": "code_investigation is required for FAIL results."}]}]}})
    return call + "\n" + res


def _codex_report(status: str, *, failed: bool = False, **args) -> str:
    item = {"id": "item_9", "type": "mcp_tool_call", "server": "device",
            "tool": "mobile_report_result",
            "arguments": {"status": status, "flow": "Case", "steps": [], **args},
            "result": None if failed else {"content": [{"type": "text", "text": f"STATUS: {status}"}]},
            "error": {"message": "code_investigation is required"} if failed else None,
            "status": "failed" if failed else "completed"}
    return json.dumps({"type": "item.completed", "item": item})


def _blocked_case():
    return _task(_spec("seeded", ["delete-bug"]))


# ── the acceptance criteria ───────────────────────────────────────────────────

def test_a_report_tool_call_alone_scores_a_verdict_and_its_bug():
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _report_tool(status="FAIL", summary="the entry is still listed after delete",
                     failure_step="2. Delete the entry", expected="the entry is gone",
                     actual="85 kg", code_investigation="n/a")), "m", _blocked_case())
    m = v.metrics
    assert m["report_source"] == "report_tool"
    assert m["reported_verdict"] == "fail"
    assert m["bugs_found"] == ["delete-bug"] and m["blocking_named"]
    assert m["completed"] is True and v.passed
    assert m["reports"][0]["step"] == 2 and m["reports"][0]["observed"] == "85 kg"
    assert m["report_tool"] == {"calls": 1, "refused": 0, "used": True}
    assert "no report written" not in (v.failure_reason or "")


def test_findings_yaml_wins_when_both_exist():
    """The report of record. The tool's (right) bug does not rescue a findings file
    that named the wrong one, and the tool's verdict does not override the file's."""
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _write("pass", _bug(2, "Save", "the save button is grey")),
        _report_tool(status="FAIL", summary="the entry is still listed after delete",
                     actual="85 kg", code_investigation="n/a")), "m", _blocked_case())
    m = v.metrics
    assert m["report_source"] == "transcript_write"
    assert m["reported_verdict"] == "pass" and m["bugs_found"] == []
    assert m["false_reports"] == 1 and m["report_tool"]["used"] is False
    assert m["completed"] is False


def test_the_file_on_disk_wins_too():
    t = _blocked_case()
    t.bug_spec["findings_file"] = "verdict: pass\nbugs: []\n"
    v = journey.journey_verdict(_transcript(
        _obs("85 kg"), _report_tool(status="FAIL", actual="85 kg", summary="still listed",
                                    code_investigation="n/a")), "m", t)
    assert v.metrics["report_source"] == "findings_file"
    assert v.metrics["reported_verdict"] == "pass" and v.metrics["reports"] == []


def test_the_result_line_outranks_the_tool_verdict_but_carries_no_bugs():
    """A RESULT line is a verdict only; it cannot shadow the tool's bug, and the tool
    (lowest precedence) cannot override its verdict."""
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _report_tool(status="PASS", summary="fine"),
        _call("mobile_observe_screen", {"device": "d"}, "x",
              assistant_text="RESULT: verdict=fail")), "m", _blocked_case())
    assert v.metrics["report_source"] == "result_line"
    assert v.metrics["reported_verdict"] == "fail"
    assert v.metrics["reports"] == []          # a PASS call carries no bug
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _report_tool(status="FAIL", summary="the entry is still listed", actual="85 kg",
                     code_investigation="n/a"),
        _call("mobile_observe_screen", {"device": "d"}, "x",
              assistant_text="RESULT: verdict=fail")), "m", _blocked_case())
    assert v.metrics["report_source"] == "result_line"
    assert v.metrics["bugs_found"] == ["delete-bug"] and v.metrics["report_tool"]["used"]


# ── BLOCKED, PASS, and what the tool can never do ─────────────────────────────

def test_blocked_is_a_fail_verdict_that_must_still_name_the_blocking_bug():
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _report_tool(status="BLOCKED", summary="the entry is still listed after delete",
                     actual="85 kg", blocker_investigation="could not proceed")),
        "m", _blocked_case())
    assert v.metrics["reported_verdict"] == "fail" and v.metrics["completed"] is True
    # BLOCKED with nothing that identifies the bug: right verdict, not completed.
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _report_tool(status="BLOCKED", summary="could not finish the test",
                     blocker_investigation="stuck")), "m", _blocked_case())
    assert v.metrics["reported_verdict"] == "fail" and v.metrics["completed"] is False
    assert "without naming the blocking bug" in v.failure_reason


def test_blocked_on_a_clean_arm_is_a_wrong_verdict():
    t = _task(_spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"},
                                     "evidence": []}, oracle_result="holds"))
    v = journey.journey_verdict(_transcript(
        _obs("Total: 4 items"),
        _report_tool(status="BLOCKED", summary="the total looked odd", actual="Total: 4 items",
                     blocker_investigation="?")), "m", t)
    assert v.metrics["reported_verdict"] == "fail" and v.metrics["completed"] is False
    assert v.metrics["false_reports"] == 1          # and its bug is a false report


def test_a_pass_call_is_a_clean_report():
    t = _task(_spec("clean", oracle={"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"},
                                     "evidence": []}, oracle_result="holds"))
    v = journey.journey_verdict(_transcript(
        _obs("Total: 4 items"),
        _report_tool("STATUS: PASS", status="PASS", summary="Everything worked; total was 4.")),
        "m", t)
    assert v.metrics["report_source"] == "report_tool"
    assert v.metrics["completed"] is True and v.metrics["false_reports"] == 0 and v.passed


def test_the_tool_reply_never_grounds_its_own_quote():
    """QUA-2775's property, kept: `mobile_report_result` is a REPORT source, never an
    EVIDENCE source. Its reply echoes `actual` back, and that must not ground it."""
    t = _blocked_case()
    t.bug_spec["echo_texts"] = ["Lunch"]
    v = journey.journey_verdict(_transcript(
        _obs("Contacts"),
        _report_tool("STATUS: FAIL\nActual: Lunch", status="FAIL", summary="it looked off",
                     actual="Lunch", code_investigation="n/a")), "m", t)
    assert v.metrics["grounded_reports"] == 0 and v.metrics["bugs_found"] == []
    # And it is no device evidence: a run with ONLY a report call executed nothing.
    v = journey.journey_verdict(_transcript(
        _report_tool(status="FAIL", summary="still listed", actual="85 kg",
                     code_investigation="n/a")), "m", _blocked_case())
    assert v.metrics["completed"] is False
    assert "no device evidence" in v.failure_reason


def test_a_refused_call_is_not_a_report():
    """DevLoop refuses FAIL without `code_investigation`; its own rule is that a
    rejected report does not count, and neither does the benchmark."""
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _refused(status="FAIL", summary="still listed", actual="85 kg")), "m", _blocked_case())
    assert v.metrics["reported_verdict"] is None and v.metrics["report_source"] == ""
    assert v.metrics["report_tool"] == {"calls": 1, "refused": 1, "used": False}
    # The retry the refusal asks for is accepted, and the last accepted call wins.
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _refused(status="FAIL", summary="still listed", actual="85 kg"),
        _report_tool(status="FAIL", summary="the entry is still listed", actual="85 kg",
                     code_investigation="none — no source")), "m", _blocked_case())
    assert v.metrics["bugs_found"] == ["delete-bug"]
    assert v.metrics["report_tool"] == {"calls": 2, "refused": 1, "used": True}


def test_codex_shaped_calls_are_read_and_refusals_honoured():
    ok = _codex_report("FAIL", summary="the entry is still listed", actual="85 kg",
                       code_investigation="n/a")
    v = journey.journey_verdict(_transcript(_obs("85 kg still listed"), ok), "m", _blocked_case())
    assert v.metrics["report_source"] == "report_tool" and v.metrics["bugs_found"] == ["delete-bug"]
    bad = _codex_report("FAIL", failed=True, summary="the entry is still listed", actual="85 kg")
    v = journey.journey_verdict(_transcript(_obs("85 kg still listed"), bad), "m", _blocked_case())
    assert v.metrics["reported_verdict"] is None and v.metrics["report_tool"]["refused"] == 1


def test_only_the_exact_tool_name_is_a_report_source():
    v = journey.journey_verdict(_transcript(
        _obs("85 kg still listed"),
        _report_tool(name="mcp__device__mobile_report_result_v2", status="FAIL",
                     summary="still listed", actual="85 kg")), "m", _blocked_case())
    assert v.metrics["report_tool"]["calls"] == 0 and v.metrics["reported_verdict"] is None


def test_report_from_tool_field_mapping():
    rep = journey.report_from_tool({"status": "fail", "failure_step": "Step 3: tap Save",
                                    "expected": "saved", "actual": "Error", "summary": "no save"})
    assert rep.verdict == "fail" and rep.source == "report_tool"
    [b] = rep.bugs
    assert (b.step, b.observed, b.expected, b.description) == (3, "Error", "saved", "no save")
    assert journey.report_from_tool({"status": "PASS", "summary": "x"}).bugs == []
    assert journey.report_from_tool({"status": "FAIL"}).bugs == []
    bad = journey.report_from_tool({"status": "MAYBE"})
    assert bad.verdict is None and bad.errors


# ── the board can tell brief versions apart ───────────────────────────────────

def test_a_board_row_names_its_brief_version_and_flags_a_mix():
    from test_journey import _rr

    def _r(tid, brief_version):
        r = _rr(tid, {"version": "clean", "completed": True, "false_reports": 0,
                      "bugs_present": [], "bugs_found": [], "case_id": tid, "app_id": "a"})
        r.provenance = {"brief_version": brief_version}
        return r

    [row] = journey.summary([_r("c~clean", 3), _r("d~clean", 3)])
    assert row["brief_version"] == "3" and not row["mixed_brief"]
    assert "brief v3" in journey.corpus_note([row])
    [row] = journey.summary([_r("c~clean", 1), _r("d~clean", 3)])
    assert row["brief_version"] is None and row["mixed_brief"]
    assert journey.MIXED_BRIEF_NOTE in journey.corpus_note([row])
