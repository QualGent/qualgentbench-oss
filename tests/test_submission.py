"""The portable findings.yaml contract.

The channel must be identical in both arms, and adding it must not regress an
agent that only emits `AREA:` lines.
"""

from __future__ import annotations

import json

import pytest

from qualgentbench import submission
from qualgentbench.bugs import _bank_findings
from qualgentbench.episode_runner import _ablation_instruction, _disabled_tools

FEATURES = [
    {"id": "add_card", "state": "ok", "probe": ["card"]},
    {"id": "star_card", "state": "broken", "probe": ["star"]},
]


def _tx(*blocks: dict) -> str:
    """Transcript from (kind, payload) blocks: device call, agent text, or file write."""
    lines = []
    for i, b in enumerate(blocks):
        if b["kind"] == "device":
            lines.append(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"d{i}", "name": "Bash",
                 "input": {"command": f"adb -s emulator-5554 shell input tap 1 1  # {b['text']}"}}]}}))
        elif b["kind"] == "text":
            lines.append(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": b["text"]}]}}))
        elif b["kind"] == "write":
            lines.append(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"w{i}", "name": "Write",
                 "input": {"file_path": b.get("path", "/ws/findings.yaml"),
                           "content": b["text"]}}]}}))
        elif b["kind"] == "bash":
            lines.append(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"b{i}", "name": "Bash",
                 "input": {"command": b["text"]}}]}}))
    return "\n".join(lines)


def _work(n=4):
    return [{"kind": "device", "text": "star card"} for _ in range(n)]


YAML_OK = """
findings:
  - area: add_card
    verdict: as_specified
    expected: a card you add is listed
    actual: it was listed
  - area: star_card
    verdict: deviates
    expected: the star stays marked
    actual: the star emptied after going back
"""


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parses_the_documented_shape():
    s = submission.parse(YAML_OK, known_areas={"add_card", "star_card"})
    assert s.ok
    assert [(c.area, c.verdict) for c in s.claims] == [
        ("add_card", "as_specified"), ("star_card", "deviates")]
    assert s.claims[1].actual.startswith("the star emptied")


def test_a_bare_list_is_accepted():
    """Agents write both shapes; rejecting one would be a compliance tax, not a check."""
    s = submission.parse("- {area: add_card, verdict: blocked}", known_areas={"add_card"})
    assert s.ok and s.claims[0].verdict == "blocked"


def test_last_entry_for_an_area_wins():
    """Matches the AREA-line rule — an agent that re-tests and corrects itself must be
    able to."""
    y = "findings:\n - {area: star_card, verdict: as_specified}\n - {area: star_card, verdict: deviates}"
    s = submission.parse(y, known_areas={"star_card"})
    assert len(s.claims) == 1 and s.claims[0].verdict == "deviates"


@pytest.mark.parametrize("bad,msg", [
    ("findings:\n  - area: star_card\n    verdict: probably", "verdict must be one of"),
    ("findings:\n  - verdict: deviates", "missing `area`"),
    ("findings:\n  - {area: nope, verdict: deviates}", "not an area of this app"),
    ("notes: hello", "no `findings:` key"),
    ("findings: [{a: 1}\n bad: [", "invalid YAML"),
])
def test_errors_are_named_not_swallowed(bad, msg):
    s = submission.parse(bad, known_areas={"star_card", "add_card"})
    assert not s.ok
    assert any(msg in e for e in s.errors), s.errors


def test_guided_vocabulary_is_rejected():
    """`ok`/`broken` belong to guided mode. Accepting them here would silently score a
    report written against the wrong contract."""
    s = submission.parse("findings:\n - {area: star_card, verdict: broken}",
                         known_areas={"star_card"})
    assert not s.ok


# ── banking ──────────────────────────────────────────────────────────────────

def test_findings_file_banks_verdicts():
    banked, ch = _bank_findings(
        _tx(*_work(), {"kind": "write", "text": YAML_OK}), FEATURES, "raw")
    assert banked["star_card"]["verdict"] == "broken"   # internal vocabulary
    assert banked["star_card"]["source"] == "findings_yaml"
    assert ch["channels"] == ["findings_yaml"]


def test_area_lines_still_bank_unchanged():
    """The no-regression property: an agent that never writes the file scores exactly
    as it did before this channel existed."""
    line = "AREA: star_card | VERDICT: deviates | EXPECTED: x | ACTUAL: y"
    banked, ch = _bank_findings(_tx(*_work(), {"kind": "text", "text": line}),
                                FEATURES, "raw")
    assert banked["star_card"]["verdict"] == "broken"   # internal vocabulary
    assert ch["channels"] == ["area_line"] and ch["findings_yaml_writes"] == 0


def test_the_later_channel_wins_when_both_are_used():
    banked, ch = _bank_findings(_tx(
        *_work(),
        {"kind": "text", "text": "AREA: star_card | VERDICT: as_specified | EXPECTED: x | ACTUAL: y"},
        {"kind": "write", "text": "findings:\n - {area: star_card, verdict: deviates}"},
    ), FEATURES, "raw")
    assert banked["star_card"]["verdict"] == "broken"   # internal vocabulary
    assert set(ch["channels"]) == {"area_line", "findings_yaml"}


def test_the_probe_gate_still_applies_to_a_structured_claim():
    """The structured channel is a reporting format, not a way past the evidence gate:
    a claim with no prior device work banks unprobed exactly as an AREA line would."""
    banked, _ = _bank_findings(_tx({"kind": "write", "text": YAML_OK}), FEATURES, "raw")
    assert banked["star_card"]["probed"] is False


def test_incremental_writes_bank_early():
    """Earliness depends on WHEN a verdict was claimed. A file written mid-episode must
    bank at that point, not at the end."""
    early = "findings:\n - {area: star_card, verdict: deviates}"
    banked, _ = _bank_findings(_tx(
        *_work(), {"kind": "write", "text": early}, *_work(20)), FEATURES, "raw")
    assert banked["star_card"]["at_call"] == 4


def test_a_heredoc_write_is_recognised():
    """`cat > findings.yaml <<'EOF' … EOF` is how a shell-first agent writes the file."""
    cmd = "cat > findings.yaml <<'EOF'\nfindings:\n  - {area: star_card, verdict: deviates}\nEOF"
    banked, ch = _bank_findings(_tx(*_work(), {"kind": "bash", "text": cmd}),
                                FEATURES, "raw")
    assert banked["star_card"]["verdict"] == "broken"   # internal vocabulary
    assert ch["findings_yaml_writes"] == 1


def test_writing_some_other_file_is_not_a_submission():
    banked, ch = _bank_findings(_tx(
        *_work(), {"kind": "write", "path": "/ws/notes.yaml", "text": YAML_OK}),
        FEATURES, "raw")
    assert not banked and ch["findings_yaml_writes"] == 0


def test_a_malformed_submission_records_the_error_and_banks_nothing():
    """Errors must not become a new way to lose a verdict — they are recorded, and the
    AREA channel still carries the episode."""
    banked, ch = _bank_findings(_tx(
        *_work(),
        {"kind": "write", "text": "findings:\n - {area: star_card, verdict: maybe}"},
        {"kind": "text", "text": "AREA: star_card | VERDICT: deviates | EXPECTED: x | ACTUAL: y"},
    ), FEATURES, "raw")
    assert banked["star_card"]["verdict"] == "broken"   # internal vocabulary
    assert any("verdict must be one of" in e for e in ch["findings_yaml_errors"])


# ── arm symmetry: the property this whole item exists for ────────────────────

def _instruction(tooling: str) -> str:
    from qualgentbench.task import BenchmarkTask
    task = BenchmarkTask(
        id="explore-catima", name="Catima hunt", app_name="Catima", bundle_id="x.y",
        app_file_id="", platform="android",
        instruction="SPECIFICATION\n  - add_card: a card you add is listed.",
        bug_spec={"features": FEATURES},
    )
    return _ablation_instruction(task, "emulator-5554", tooling)


def test_the_reporting_contract_is_byte_identical_across_arms():
    """Both arms get the same reporting block, and neither is nudged toward
    mobile_report_result."""
    raw, mcp = _instruction("raw"), _instruction("mcp")
    block = submission.instruction([f["id"] for f in FEATURES])
    assert block in raw and block in mcp
    for text in (raw, mcp):
        assert "mobile_report_result" not in text


def test_only_the_device_tooling_note_differs():
    raw, mcp = _instruction("raw").splitlines(), _instruction("mcp").splitlines()
    differing = set(raw) ^ set(mcp)
    assert not any("AREA:" in d or "findings" in d or "RESULT:" in d for d in differing), differing

def test_the_report_tool_is_off_in_hunt_when_the_env_says_so(monkeypatch):
    """Hunt reports through findings.yaml in both arms, so withholding
    mobile_report_result is an operator setting, not a code default."""
    monkeypatch.setenv("QGB_DISALLOWED_TOOLS", "mobile_report_result")
    assert "mobile_report_result" in _disabled_tools()
    monkeypatch.delenv("QGB_DISALLOWED_TOOLS", raising=False)
    assert _disabled_tools() == []


def test_no_shipped_spec_names_a_reporting_tool():
    from qualgentbench.bugs import _BENCHMARKS_DIR
    specs = sorted(_BENCHMARKS_DIR.glob("*.yaml"))
    named = [p.name for p in specs if "mobile_report_result" in p.read_text()]
    assert not named, f"specs naming a tool only one arm has: {named}"


def test_the_file_on_disk_rescues_areas_the_transcript_lost():
    """An agent that appends with `Edit` writes fragments that do not parse
    standalone — the file itself is read from the workspace at episode end."""
    from pathlib import Path
    from qualgentbench.bugs import load_suite, exploration_task, exploration_verdict
    suite = load_suite(Path("src/qualgentbench/data/benchmarks/catima.yaml"))
    task = exploration_task(suite)
    task.bug_spec.update({
        "tooling": "raw", "hook_steps": 100, "step_cap": 346,
        "findings_file": "findings:\n"
                         "  - {area: star_card, verdict: deviates}\n"
                         "  - {area: add_card, verdict: as_specified}\n",
    })
    tx = _tx(*[{"kind": "device", "text": "star favourite card store"} for _ in range(6)])
    v = exploration_verdict(tx, "m", task)
    assert "star-not-persisted" in v.metrics["found_bug_ids"]
    assert "findings_file" in v.metrics["submission_channels"]


def test_the_file_never_overwrites_an_earlier_banked_verdict():
    """Ordering-derived credit must survive. An area banked mid-episode keeps its
    `at_call`, so reading the final file can rescue a verdict but never launder
    earliness."""
    from pathlib import Path
    from qualgentbench.bugs import load_suite, _bank_findings
    suite = load_suite(Path("src/qualgentbench/data/benchmarks/catima.yaml"))
    feats = suite["exploration"]["features"]
    early = "findings:\n - {area: star_card, verdict: deviates}"
    banked, _ = _bank_findings(_tx(
        *[{"kind": "device", "text": "star"} for _ in range(4)],
        {"kind": "write", "text": early},
        *[{"kind": "device", "text": "star"} for _ in range(30)],
    ), feats, "raw")
    assert banked["star_card"]["at_call"] == 4


# ── stage 1: the reproduction is CAPTURED, never scored ──────────────────────

REPRO = """
findings:
  - area: star_card
    verdict: deviates
    expected: a card you mark as favourite stays marked
    actual: the star empties after going back
    steps:
      - launch
      - tap: "QA-Card-01"
      - tap: "Favourite"
      - press: back
      - relaunch
    expect:
      present: "★"
"""


def test_a_reproduction_is_parsed():
    s = submission.parse(REPRO, known_areas={"star_card"})
    c = s.claims[0]
    assert c.replayable
    assert [(x.action, x.value) for x in c.steps] == [
        ("launch", ""), ("tap", "QA-Card-01"), ("tap", "Favourite"),
        ("press", "back"), ("relaunch", "")]
    assert c.expect.mode == "present" and c.expect.text == "★"


def test_a_claim_without_a_reproduction_is_still_valid():
    """Stage 1 captures repros; it must not make them mandatory, or an agent that
    cannot write one loses verdicts it legitimately earned."""
    s = submission.parse("findings:\n - {area: star_card, verdict: deviates}",
                         known_areas={"star_card"})
    assert s.ok and not s.claims[0].replayable


@pytest.mark.parametrize("bad,msg", [
    ("steps: [{fly: x}]", "unknown action"),
    ("steps: [{tap: ''}]", "needs a value"),
    ("steps: [{press: sideways}]", "press must be"),
    ("steps: [{swipe: diagonally}]", "swipe must be"),
    ("steps: not-a-list", "must be a list"),
    ("expect: {maybe: x}", "mode must be present|absent"),
    ("expect: {present: ''}", "needs the text"),
])
def test_a_malformed_repro_is_reported_but_keeps_the_verdict(bad, msg):
    y = f"findings:\n  - area: star_card\n    verdict: deviates\n    {bad}\n"
    s = submission.parse(y, known_areas={"star_card"})
    assert any(msg in e for e in s.errors), s.errors
    assert s.claims and s.claims[0].verdict == "deviates"   # verdict survives


@pytest.mark.parametrize("oracle", [
    "expect: {db: notes.db, query: 'select 1', equals: '1'}",
    "expect: {db: /data/data/x/files/qgb_flags, query: 'select 1', equals: '1'}",
    "expect: {file: /sdcard/x, contains: broken}",
    "expect: {content: 'content://sms/inbox', contains: x}",
])
def test_agent_submissions_cannot_use_harness_oracles(oracle):
    """db/file/content shell out on the device and can read seeded state directly —
    an agent expectation must never reach them. The verdict still stands; the claim
    just has nothing to replay."""
    y = f"findings:\n  - area: star_card\n    verdict: deviates\n    {oracle}\n"
    s = submission.parse(y, known_areas={"star_card"})
    assert any("harness-only" in e for e in s.errors), s.errors
    assert s.claims[0].verdict == "deviates"
    assert s.claims[0].expect is None and not s.claims[0].replayable


def test_the_trusted_spec_path_still_parses_oracle_expectations():
    for raw in ({"db": "notes.db", "query": "select 1", "equals": "1"},
                {"file": "/sdcard/x", "contains": "y"},
                {"content": "content://media", "equals": "1"}):
        expect, err = submission._parse_expect(raw, "area", trusted=True)
        assert err is None and expect is not None


def test_every_expectation_mode_round_trips_through_as_dict():
    """`Expectation(**e.as_dict())` is how replay_findings rebuilds a claim from
    result.json when findings.yaml is gone — a mode whose dict cannot reconstruct
    it crashes that fallback (exp["text"] once KeyError'd on db/file/content)."""
    for e in (submission.Expectation("present", text="x"),
              submission.Expectation("absent", text="x"),
              submission.Expectation("db", db="n.db", query="select 1", equals="1"),
              submission.Expectation("file", path="/sdcard/x", contains="y"),
              submission.Expectation("file", path="/sdcard/x", name="y", absent=True),
              submission.Expectation("content", uri="content://a", where="w",
                                     contains="c", absent=True),
              submission.Expectation("content", uri="content://a", equals="2")):
        rebuilt = submission.Expectation(**e.as_dict())
        assert rebuilt.as_dict() == e.as_dict()


def test_repro_coverage_is_recorded_without_affecting_the_score():
    from pathlib import Path
    from qualgentbench.bugs import load_suite, exploration_task, exploration_verdict
    suite = load_suite(Path("src/qualgentbench/data/benchmarks/catima.yaml"))

    def _score(findings_file):
        task = exploration_task(suite)
        task.bug_spec.update({"tooling": "raw", "hook_steps": 100, "step_cap": 346,
                              "findings_file": findings_file})
        tx = _tx(*[{"kind": "device", "text": "star favourite card"} for _ in range(6)])
        return exploration_verdict(tx, "m", task).metrics

    plain = _score("findings:\n - {area: star_card, verdict: deviates}")
    with_repro = _score(REPRO)
    assert with_repro["claims_with_repro"] == 1 and plain["claims_with_repro"] == 0
    assert with_repro["repro_claims"][0]["expect"] == {"mode": "present", "text": "★"}
    # Identical scoring — the repro is captured, not graded.
    assert with_repro["overall"] == plain["overall"]
    assert with_repro["bugs_found"] == plain["bugs_found"]


def test_both_arms_are_asked_for_a_reproduction():
    block = submission.instruction(["add_card"])
    for tooling in ("raw", "mcp"):
        text = _instruction(tooling)
        assert block in text
        assert "steps:" in text and "expect:" in text


def test_one_bad_line_does_not_discard_the_whole_file():
    """A single unquoted prose field must cost ONE entry, not every finding."""
    from qualgentbench import submission
    text = """findings:
  - area: add_event
    verdict: as_specified
    expected: a birthday you add is listed
    actual: "quoted, so fine"
    steps:
      - launch
      - tap: "New event"
    expect:
      present: "QA"
  - area: edit_event
    verdict: deviates
    actual: tapped "Update event": the list still shows "Alice"
    steps:
      - launch
    expect:
      present: "Alicia"
  - area: settings
    verdict: as_specified
    actual: "fine too"
    steps:
      - launch
      - tap: "Settings"
    expect:
      present: "Hide images"
"""
    sub = submission.parse(text, known_areas={"add_event", "edit_event", "settings"})
    got = {c.area for c in sub.claims}
    assert got == {"add_event", "settings"}, got
    assert all(c.replayable for c in sub.claims)
    assert any("recovered" in e for e in sub.errors)


def test_a_wholly_unparseable_file_still_reports_one_error():
    from qualgentbench import submission
    sub = submission.parse("::::not yaml at all::::", known_areas={"a"})
    assert not sub.claims and sub.errors
