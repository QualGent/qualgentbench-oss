"""Per-episode audit bundle — steps.jsonl + decoded screenshots."""

from __future__ import annotations

import json
from pathlib import Path

from qualgentbench.episode_evidence import write_episode_evidence

# 1x1 JPEG (valid magic bytes) — stand-in for a mobile_observe_screen reply.
_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAAB//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


def _codex(*items: dict) -> str:
    return "\n".join(json.dumps({"type": "item.completed", "item": i}) for i in items)


def _observe_item() -> dict:
    return {
        "type": "mcp_tool_call", "tool": "mobile_observe_screen",
        "arguments": {"device": "emulator-5554"},
        "result": {"content": [
            {"type": "image", "data": _JPEG_B64, "mimeType": "image/jpeg"},
            {"type": "text", "text": json.dumps(
                {"elements": ["Save", "Cancel"],
                 "screen_size": {"width": 1080, "height": 2400}})},
        ]},
        "status": "completed", "error": None,
    }


def _steps(run_dir: Path) -> list[dict]:
    return [json.loads(line)
            for line in (run_dir / "evidence" / "steps.jsonl").read_text().splitlines()]


def test_observe_writes_screenshot_and_elements(tmp_path: Path) -> None:
    write_episode_evidence(tmp_path, _codex(_observe_item()))

    step = _steps(tmp_path)[0]
    assert step["kind"] == "observe"
    assert step["screens"] == ["screens/0001.jpg"]
    assert step["elements"] == ["Save", "Cancel"]
    assert step["screen_size"] == {"width": 1080, "height": 2400}
    # the parsed elements replace the raw reply — the same screen isn't stored twice
    assert "result" not in step

    shot = tmp_path / "evidence" / "screens" / "0001.jpg"
    assert shot.read_bytes()[:3] == b"\xff\xd8\xff"  # JPEG magic


def test_actions_and_commands_are_ordered_steps(tmp_path: Path) -> None:
    write_episode_evidence(tmp_path, _codex(
        _observe_item(),
        {"type": "mcp_tool_call", "tool": "mobile_tap",
         "arguments": {"device": "emulator-5554", "x": 540, "y": 1468},
         "result": {"content": [{"type": "text", "text": "Tapped"}]},
         "status": "completed", "error": None},
        # the raw arm drives the device through Bash — those commands are its evidence
        {"type": "command_execution", "command": "adb shell input tap 5 5",
         "exit_code": 0, "aggregated_output": ""},
    ))

    steps = _steps(tmp_path)
    assert [s["step"] for s in steps] == [1, 2, 3]
    assert [s["kind"] for s in steps] == ["observe", "action", "command"]
    assert steps[1]["summary"] == "Tapped (540, 1468)"
    assert steps[2]["summary"].startswith("Ran: adb shell input tap")


def test_steps_without_a_frame_borrow_the_neighbouring_ones(tmp_path: Path) -> None:
    tap = {"type": "mcp_tool_call", "tool": "mobile_tap",
           "arguments": {"device": "emulator-5554", "x": 1, "y": 2},
           "result": {"content": [{"type": "text", "text": "Tapped"}]},
           "status": "completed", "error": None}
    write_episode_evidence(tmp_path, _codex(_observe_item(), tap, tap, _observe_item()))

    steps = _steps(tmp_path)
    # both taps sit between the same two observations — a shared, stale "before" is
    # the honest answer here, and from_step says so
    for tapped in (steps[1], steps[2]):
        assert "screens" not in tapped
        assert tapped["screen_before"] == {"path": "screens/0001.jpg", "from_step": 1}
        assert tapped["screen_after"] == {"path": "screens/0004.jpg", "from_step": 4}
    # a step that captured its own frame is left alone
    assert "screen_before" not in steps[0]


def test_claude_stream_json_shape_is_parsed(tmp_path: Path) -> None:
    transcript = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mobile_observe_screen",
             "input": {"device": "emulator-5554"}},
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/jpeg",
                                             "data": _JPEG_B64}},
                {"type": "text", "text": json.dumps({"elements": ["OK"]})},
            ]},
        ]}}),
    ])
    write_episode_evidence(tmp_path, transcript)

    step = _steps(tmp_path)[0]
    assert step["tool"] == "mobile_observe_screen"
    assert step["elements"] == ["OK"]
    assert step["screens"] == ["screens/0001.jpg"]


def test_mcp_namespaced_tool_names_are_classified(tmp_path: Path) -> None:
    # Claude Code reports MCP tools as mcp__<server>__<tool>; codex reports the bare
    # name. Matching only the bare form filed every action under "other".
    write_episode_evidence(tmp_path, _codex(
        {"type": "mcp_tool_call", "tool": "mcp__device__mobile_tap",
         "arguments": {"device": "emulator-5554", "x": 985, "y": 2288},
         "result": {"content": [{"type": "text", "text": "Tapped"}]},
         "status": "completed", "error": None},
        {"type": "mcp_tool_call", "tool": "mcp__device__mobile_report_result",
         "arguments": {"status": "FAIL"},
         "result": {"content": [{"type": "text", "text": "ok"}]},
         "status": "completed", "error": None},
    ))

    steps = _steps(tmp_path)
    assert [s["kind"] for s in steps] == ["action", "report"]
    assert steps[0]["summary"] == "Tapped (985, 2288)"
    assert steps[1]["summary"] == "Reported FAIL"
    # the full namespaced name is still what gets recorded
    assert steps[0]["tool"] == "mcp__device__mobile_tap"


def test_typed_values_and_known_secrets_are_scrubbed(tmp_path: Path) -> None:
    write_episode_evidence(
        tmp_path,
        _codex({"type": "mcp_tool_call", "tool": "mobile_type_text",
                "arguments": {"device": "emulator-5554", "text": "hunter2"},
                "result": {"content": [{"type": "text", "text": "typed s3cret-token"}]},
                "status": "completed", "error": None}),
        secrets=("s3cret-token",),
    )

    step = _steps(tmp_path)[0]
    assert step["args"]["text"] == "[REDACTED]"
    assert step["args_redacted"] is True
    assert step["summary"] == "Typed text"          # never echoes the value
    assert "s3cret-token" not in json.dumps(step)


def test_meta_records_identity_and_counts(tmp_path: Path) -> None:
    write_episode_evidence(
        tmp_path,
        _codex(_observe_item()),
        meta={"task_id": "birday-clean-add", "model": "gpt-5.5", "device": "emulator-5554"},
    )

    meta = json.loads((tmp_path / "evidence" / "meta.json").read_text())
    assert meta["schema_version"] == 1
    assert meta["episode"]["task_id"] == "birday-clean-add"
    assert meta["counts"] == {"steps": 1, "screenshots": 1, "frames": 0,
                              "actions": 0, "observations": 1}


_FEATURES = [
    {"id": "add_event", "state": "ok", "probe": ["event"]},
    {"id": "favorite", "state": "broken", "bug_id": "favorite-not-persisted",
     "probe": ["favorite"]},
    {"id": "settings", "state": "ok", "probe": ["settings"]},
]


def _hunt(*items: dict) -> str:
    return "\n".join(
        json.dumps({"type": "item.completed", "item": i}) for i in items)


def _say(text: str) -> dict:
    return {"type": "agent_message", "text": text}


def _tap() -> dict:
    return {"type": "mcp_tool_call", "tool": "mobile_tap",
            "arguments": {"device": "emulator-5554", "x": 1, "y": 2},
            "result": {"content": [{"type": "text", "text": "Tapped"}]},
            "status": "completed", "error": None}


def _findings(run_dir: Path) -> dict:
    return json.loads((run_dir / "evidence" / "findings.json").read_text())


def test_per_bug_index_locates_each_area_and_reads_the_scored_outcome(tmp_path: Path) -> None:
    transcript = _hunt(
        _tap(), _tap(), _say("AREA: add_event | VERDICT: as_specified"),
        _tap(), _observe_item(), _say("AREA: favorite | VERDICT: deviates"),
        _tap(), _say("AREA: settings | VERDICT: ok"),
    )
    write_episode_evidence(
        tmp_path, transcript, features=_FEATURES,
        metrics={"bugs_found": 1, "false_positives": 0,
                 "found_bug_ids": ["favorite-not-persisted"],
                 "banked_at": {"favorite": 8}})

    index = _findings(tmp_path)
    assert index["agrees_with_score"] is True
    by_id = {a["feature"]: a for a in index["areas"]}

    # each claim bounds a segment running from just after the previous claim
    assert by_id["add_event"]["segment"] == [1, 2]
    assert by_id["favorite"]["segment"] == [3, 4]
    assert by_id["settings"]["segment"] == [5, 5]
    assert [a["attribution"] for a in index["areas"]] == ["banked"] * 3

    # outcome comes from the scorer's metrics, not from re-judging the episode
    assert by_id["favorite"]["outcome"] == "true_positive"
    assert by_id["add_event"]["outcome"] == "correct_ok"
    # and the scorer's own position is carried for cross-checking
    assert by_id["favorite"]["scorer_at_call"] == 8
    assert by_id["favorite"]["evidence_screen"] == "screens/0004.jpg"


def test_areas_written_up_together_are_marked_shared(tmp_path: Path) -> None:
    transcript = _hunt(
        _tap(), _tap(),
        _say("AREA: add_event | VERDICT: ok\nAREA: settings | VERDICT: ok"),
    )
    write_episode_evidence(tmp_path, transcript, features=_FEATURES, metrics={})

    by_id = {a["feature"]: a for a in _findings(tmp_path)["areas"]}
    assert by_id["add_event"]["attribution"] == "shared"
    assert by_id["settings"]["attribution"] == "shared"
    assert by_id["add_event"]["segment"] == by_id["settings"]["segment"]
    # never claimed at all
    assert by_id["favorite"]["outcome"] == "unreported"
    assert by_id["favorite"]["attribution"] == "unattributable"
    assert "segment" not in by_id["favorite"]


def test_a_find_the_scorer_did_not_credit_is_never_shown_as_found(tmp_path: Path) -> None:
    # The agent called a real bug broken but the scorer did not credit it (the probe
    # gate rejected the claim). The index must follow the score, not the agent.
    transcript = _hunt(_tap(), _say("AREA: favorite | VERDICT: deviates"))
    write_episode_evidence(tmp_path, transcript, features=_FEATURES,
                           metrics={"bugs_found": 0, "false_positives": 0,
                                    "found_bug_ids": [],
                                    "unverified_broken": ["favorite"]})

    index = _findings(tmp_path)
    by_id = {a["feature"]: a for a in index["areas"]}
    assert by_id["favorite"]["verdict"] == "broken"        # what the agent said
    assert by_id["favorite"]["outcome"] == "claimed_but_ungated"   # what it earned
    assert index["agrees_with_score"] is True


def test_index_reports_rather_than_hides_a_disagreement_with_the_score(tmp_path: Path) -> None:
    # False positives are derived here (metrics carries a count, not ids), so they are
    # where the two can drift. A disagreement must surface, not be quietly reconciled.
    transcript = _hunt(_tap(), _say("AREA: add_event | VERDICT: deviates"))
    write_episode_evidence(tmp_path, transcript, features=_FEATURES,
                           metrics={"bugs_found": 0, "false_positives": 0,
                                    "found_bug_ids": []})

    index = _findings(tmp_path)
    by_id = {a["feature"]: a for a in index["areas"]}
    assert by_id["add_event"]["outcome"] == "false_positive"
    assert index["agrees_with_score"] is False


def test_no_features_means_no_index(tmp_path: Path) -> None:
    # Guided episodes carry a single bug and no area list — nothing to index.
    write_episode_evidence(tmp_path, _codex(_observe_item()))
    assert not (tmp_path / "evidence" / "findings.json").exists()


def test_truncated_transcript_still_yields_a_bundle(tmp_path: Path) -> None:
    # A killed agent leaves a half-written last line; earlier steps still count.
    transcript = _codex(_observe_item()) + '\n{"type": "item.completed", "item": {"ty'
    assert write_episode_evidence(tmp_path, transcript) is not None
    assert len(_steps(tmp_path)) == 1
