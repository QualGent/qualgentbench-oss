"""QUA-2801: an MCP result scores the same whether the agent kept its `\\uXXXX` escapes.

DevLoop answers `mobile_tap_and_observe(include_screenshot=true)` with
`json.dumps(result, indent=2)`, which escapes every non-ASCII character. codex-cli records
that text as sent (`\\u2022`); claude-code records the character (`•`). The journey scorer
read the raw text, so orgzly's `Getting Started with Orgzly  •  Notes` breadcrumb could
never witness on codex (run 20260923-224921-0461, orgzly-open-note-from-notebook~clean).
One normaliser, `transcript.decode_unicode_escapes`, now decodes every MCP result where
`bugs._ordered_stream` reads it (and where `ToolEvent.result_text` is cleaned), so both
shapes feed the witness, the present/absent evidence, grounding and bug matching one text.

These tests are QUA-2776 style: ONE episode written as claude-code stream-json (decoded)
and codex `exec --json` (escaped), every scorer input compared."""

from __future__ import annotations

import json

import pytest

from qualgentbench import bugs, journey
from qualgentbench.transcript import TranscriptParser, clean_result_text, decode_unicode_escapes

from test_journey import _spec, _task
from test_mcp_scoring_parity import Call, Say, _claude, _codex, _events, _scored

BREADCRUMB = "Getting Started with Orgzly  •  Notes"
# Genuine screen text that LOOKS like an escape: six characters, backslash included.
LITERAL = "path C:\\u00e9t\\u00e9"


# ── the normaliser ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, decoded", [
    ("Orgzly  \\u2022  Notes", "Orgzly  •  Notes"),
    ("9:00\\u202fAM", "9:00\u202fAM"),
    ("\\u201ctag1\\u201d", "“tag1”"),
    ("\\uD83C\\uDF89 done", "🎉 done"),                   # a surrogate pair is ONE character
    ("\\ud83c\\udf89", "🎉"),
    ("C:\\\\u00e9", "C:\\\\u00e9"),                       # an escaped backslash guards the `\u`
    ("C:\\\\\\u00e9", "C:\\\\é"),                         # ...and only the one after it
    ("lone \\ud83c here", "lone \\ud83c here"),           # a lone surrogate is no character
    ("line\\nnext \\\"q\\\" \\t", "line\\nnext \\\"q\\\" \\t"),   # other escapes untouched
    ("\\u00zz and \\u12", "\\u00zz and \\u12"),           # not an escape
    ("", ""),
])
def test_decode_unicode_escapes(raw, decoded):
    assert decode_unicode_escapes(raw) == decoded


def test_decoding_an_escaped_dump_equals_the_unescaped_dump():
    """The property the parity rests on: `json.dumps` with and without `ensure_ascii`
    differ ONLY in the escapes the normaliser removes — including genuine screen text
    that carries a backslash-u of its own."""
    result = {"elements": [{"text": BREADCRUMB}, {"text": LITERAL}, {"text": "🎉 “quoted”"},
                           {"text": "9:00\u202fAM"}], "screen_changed": True}
    escaped = json.dumps(result, indent=2)
    plain = json.dumps(result, indent=2, ensure_ascii=False)
    assert escaped != plain and "\\u2022" in escaped
    assert decode_unicode_escapes(escaped) == plain
    assert decode_unicode_escapes(plain) == plain                # claude's text is unchanged
    assert clean_result_text(escaped) == clean_result_text(plain) == plain


# ── one episode, two transcripts ─────────────────────────────────────────────

def _dump(result: dict, *, escaped: bool) -> str:
    """DevLoop's `_tap_result_with_screenshot` text: `json.dumps(result, indent=2)`,
    kept escaped (codex) or holding the characters (claude)."""
    return json.dumps(result, indent=2, ensure_ascii=escaped)


def _episode(screens: list[dict], *, escaped: bool) -> list:
    out: list = [Say("Launching and reading the screen."),
                 Call("mobile_launch_app", {"package_name": "com.orgzly"}, "Launched.")]
    for i, screen in enumerate(screens):
        out.append(Call("mobile_tap_and_observe",
                        {"text": f"step {i}", "include_screenshot": True},
                        _dump(screen, escaped=escaped), image=True))
    return out


def _pair(screens: list[dict]) -> tuple[str, str]:
    return _claude(_episode(screens, escaped=False)), _codex(_episode(screens, escaped=True))


def _screen(*texts: str) -> dict:
    return {"elements": [{"text": t, "clickable": True} for t in texts],
            "screen_changed": True, "screenshot": "attached"}


def _assert_inputs_identical(claude: str, codex: str) -> None:
    for split in (False, True):
        assert bugs._ordered_stream(claude, "mcp", split_calls=split) \
            == bugs._ordered_stream(codex, "mcp", split_calls=split), split
    for results_only in (False, True):
        assert journey._device_texts(claude, "mcp", results_only=results_only) \
            == journey._device_texts(codex, "mcp", results_only=results_only)
    for screen_only in (False, True):
        for fold in (False, True):
            assert journey._observation_texts(claude, "mcp", screen_only=screen_only, fold=fold) \
                == journey._observation_texts(codex, "mcp", screen_only=screen_only, fold=fold)
    assert _events(claude) == _events(codex)
    pc, px = TranscriptParser(claude), TranscriptParser(codex)
    assert pc.observation_texts() == px.observation_texts()
    assert bugs._device_interaction_texts(pc, "mcp") == bugs._device_interaction_texts(px, "mcp")


def _orgzly_case(findings: str):
    suite = next(s for s in bugs.load_apps() if s["app"]["id"] == "orgzly")
    task = next(t for t in journey.journey_tasks(suite)
                if t.id == "orgzly-open-note-from-notebook~clean")
    task.bug_spec["tooling"] = "mcp"
    task.bug_spec["findings_file"] = findings
    return task


def test_the_orgzly_breadcrumb_witnesses_on_both_agents():
    """The Phase A episode: the real case, the witness the device showed through
    `mobile_tap_and_observe(include_screenshot=true)`. It completes on both shapes, and
    the report's quote of the breadcrumb is grounded on both."""
    screens = [_screen("Getting Started with Orgzly", "Notebooks"),
               _screen(BREADCRUMB, "Click on the note to open it", LITERAL, "🎉")]
    claude, codex = _pair(screens)
    assert "\\\\u2022" in codex and "\\\\u2022" not in claude     # the fixture really differs
    _assert_inputs_identical(claude, codex)

    findings = ('verdict: pass\nbugs:\n  - step: 3\n'
                f'    observed: "{BREADCRUMB}"\n    description: "breadcrumb shown as expected"\n')
    vc = journey.journey_verdict(claude, "m", _orgzly_case(findings))
    vx = journey.journey_verdict(codex, "m", _orgzly_case(findings))
    assert _scored(vc.metrics) == _scored(vx.metrics)
    assert vc.passed == vx.passed and vc.failure_reason == vx.failure_reason
    m = vx.metrics
    assert m["completed"] is True, m["completion_reason"]
    assert BREADCRUMB in m["witness"]["seen"] and m["witness"]["missing"] == []
    assert m["oracle"]["ok"] is True
    assert m["grounded_reports"] == 1
    # Genuine backslash-u screen text stays literal on both, never decoded into `é`.
    results = journey._device_texts(codex, "mcp", results_only=True)
    assert any("c:\\\\u00e9t\\\\u00e9" in t for t in results)
    assert not any("c:\\é" in t or "c:é" in t for t in results)
    assert any("🎉" in t for t in results)


_MARKER = "Avg • 76 kg"          # a display defect whose measured text is non-ASCII


def _seeded_spec(oracle: dict) -> dict:
    spec = _spec("seeded", bugs=["avg-bug"], oracle=oracle)
    spec["side"][0]["marker"] = _MARKER
    spec["side"][0]["texts"] = [_MARKER]
    return spec


@pytest.mark.parametrize("oracle", [
    {"mode": "present", "expect": {"present": "Total “4” items"},
     "evidence": ["Total “4” items"], "witness": ["Total “4” items"]},
    {"mode": "absent", "expect": {"absent": "Total “3” items"},
     "evidence": ["Total “4” items"], "witness": ["Total “4” items"]},
], ids=["present", "absent"])
def test_present_absent_evidence_grounding_and_bug_match_are_identical(oracle):
    """A seeded display defect whose marker carries a bullet, a case whose evidence
    carries curly quotes: the present/absent evidence, the witness, grounding and the
    bug match come out identical, and all of them hit — equality is not two zeros."""
    screens = [_screen("Weight", _MARKER), _screen("Total “4” items", "Done ✅")]
    claude, codex = _pair(screens)
    _assert_inputs_identical(claude, codex)

    findings = ('verdict: pass\nbugs:\n  - step: 2\n'
                f'    observed: "{_MARKER}"\n    description: "the average shown is wrong"\n')
    verdicts = []
    for transcript in (claude, codex):
        spec = _seeded_spec(oracle)
        spec["findings_file"] = findings
        verdicts.append(journey.journey_verdict(transcript, "m", _task(spec)))
    vc, vx = verdicts
    assert _scored(vc.metrics) == _scored(vx.metrics)
    assert vc.passed == vx.passed
    m = vx.metrics
    assert m["completed"] is True, m["completion_reason"]
    assert m["witness"]["seen"] == ["Total “4” items"]
    assert m["bugs_found"] == ["avg-bug"] and m["false_reports"] == 0
    assert m["grounded_reports"] == 1


def test_the_hunt_probe_reads_the_decoded_text_on_both_agents():
    """`bugs._bank_findings` (hunt) reads the same stream: a non-ASCII probe is seen on
    both agents or on neither."""
    screens = [_screen("Due today • 3 tasks")]
    ep_c = _episode(screens, escaped=False) + [Say("AREA: login | VERDICT: broken")]
    ep_x = _episode(screens, escaped=True) + [Say("AREA: login | VERDICT: broken")]
    features = [{"id": "login", "state": "broken", "probe": ["due today • 3 tasks"]}]
    bc, _ = bugs._bank_findings(_claude(ep_c), features, "mcp")
    bx, _ = bugs._bank_findings(_codex(ep_x), features, "mcp")
    assert bc == bx
    assert bx["login"]["verdict"] == "broken"


def test_a_raw_arm_shell_result_is_left_as_the_device_sent_it():
    """The raw arm's adb output is the device's own bytes, not an MCP JSON dump: a
    backslash-u in it is screen text and stays literal (raw-arm rescores byte-identical)."""
    from test_bugs import _call, _transcript
    t = _transcript(_call("Bash", {"command": "adb shell cat /sdcard/x.txt"}, "Orgzly \\u2022 Notes"))
    stream = bugs._ordered_stream(t, "raw", split_calls=True)
    assert ("device", "orgzly \\u2022 notes") in stream
