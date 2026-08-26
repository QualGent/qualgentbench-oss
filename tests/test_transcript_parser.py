import json

from qualgentbench.transcript import TranscriptParser


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_parses_claude_stream_json_tool_calls() -> None:
    transcript = _jsonl(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "mcp__device__mobile_observe_screen",
                        "input": {"device": "emulator-5554"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": '{"elements": ["Buggy Notebook", "Login"]}',
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "mcp__device__mobile_report_result",
                        "input": {"status": "FAIL"},
                    }
                ]
            },
        },
    )

    parser = TranscriptParser(transcript)

    assert len(parser.successful_device_events()) == 1
    assert parser.navigation_visited(["login"])
    assert parser.reported_status() == "FAIL"


def test_parses_mcp_item_completed_tool_calls() -> None:
    transcript = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_observe_screen",
                "arguments": {"device": "emulator-5554"},
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"elements": ["Buggy Notebook", "Login"]}',
                        }
                    ]
                },
                "error": None,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_tap_and_observe",
                "arguments": {"device": "emulator-5554", "element_text": "Login"},
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"screen_changed": false, "elements": ["Invalid credentials"]}',
                        }
                    ]
                },
                "error": None,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_3",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_report_result",
                "arguments": {"status": "FAIL"},
                "result": {"content": [{"type": "text", "text": "STATUS: FAIL"}]},
                "error": None,
                "status": "completed",
            },
        },
    )

    parser = TranscriptParser(transcript)

    assert len(parser.successful_device_events()) == 2
    assert parser.navigation_visited(["login"])
    assert parser.interaction_performed(["mobile_tap_and_observe"], ["login"])
    assert parser.reported_status() == "FAIL"


def test_parses_codex_exec_json_shapes_and_usage() -> None:
    transcript = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_observe_screen",
                "arguments": {"device": "emulator-5554"},
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"elements": ["Buggy Notebook", "Login"]}',
                        }
                    ]
                },
                "error": None,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_2",
                "type": "command_execution",
                "command": "python -m pytest tests/test_login.py",
                "output": "1 passed",
                "exit_code": 0,
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_3",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_tap_and_observe",
                "arguments": {"device": "emulator-5554", "element_text": "Login"},
                "result": {"content": [{"type": "text", "text": "device unavailable"}]},
                "error": "device unavailable",
                "status": "failed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "I found the issue."}],
                "model": "openai/gpt-5.5",
            },
        },
        {
            "type": "error",
            "message": "non-tool runtime warning",
        },
        {
            "type": "item.completed",
            "item": {
                "id": "item_4",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_report_result",
                "arguments": {"status": "FAIL"},
                "result": {"content": [{"type": "text", "text": "STATUS: FAIL"}]},
                "error": None,
                "status": "completed",
            },
        },
        {
            "type": "turn.completed",
            "turn": {"model": "openai/gpt-5.5"},
            "usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 200,
                "output_tokens": 300,
                "reasoning_output_tokens": 50,
            },
        },
    )

    parser = TranscriptParser(transcript)

    events = parser.events()
    assert [e.name for e in events] == [
        "mobile_observe_screen",
        "command_execution",
        "mobile_tap_and_observe",
        "mobile_report_result",
    ]
    assert len(parser.successful_device_events()) == 1
    assert parser.navigation_visited(["login"])
    assert parser.accessed_paths() == [
        json.dumps({"command": "python -m pytest tests/test_login.py"})
    ]
    assert parser.reported_status() == "FAIL"
    assert parser.model() == "openai/gpt-5.5"
    usage = parser.token_usage()
    assert usage["input_tokens"] == 1200
    assert usage["cached_input_tokens"] == 200
    assert usage["output_tokens"] == 300
    assert usage["total_tokens"] == 1500


def test_detects_required_interaction_with_no_screen_change() -> None:
    transcript = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "mobile_tap_and_observe",
                "arguments": {
                    "device": "emulator-5554",
                    "element_text": "Sample Note 2",
                },
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"screen_changed": false, "elements": ["My Notes", "Sample Note 2"]}',
                        }
                    ]
                },
                "error": None,
                "status": "completed",
            },
        },
    )

    parser = TranscriptParser(transcript)

    assert parser.interaction_performed(["mobile_tap_and_observe"], ["note"])
    assert parser.interaction_had_no_screen_change(
        ["mobile_tap_and_observe"],
        ["note"],
    )


def test_tracks_routine_tool_calls() -> None:
    transcript = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "mcp_tool_call",
                "server": "mcp",
                "tool": "find_routine",
                "arguments": {"app_bundle_id": "com.example.buggyapp", "os": "android"},
                "result": {"content": [{"type": "text", "text": "No routines recorded yet"}]},
                "error": None,
                "status": "completed",
            },
        },
    )

    parser = TranscriptParser(transcript)

    assert len(parser.routine_events()) == 1
    assert parser.called_routine_tool("find_routine")


def test_a_synthesized_final_event_does_not_become_the_model():
    """claude stamps model "<synthetic>" on locally-generated events (the final
    result after a failed compaction). Last-value-wins once turned that into a
    phantom second agent+model row on a 16-episode board."""
    from qualgentbench.transcript import TranscriptParser

    tx = "\n".join([
        json.dumps({"type": "assistant", "message": {"model": "Qwen 3.8 Max",
                                                     "content": []}}),
        json.dumps({"type": "result", "model": "<synthetic>",
                    "result": "Prompt is too long"}),
    ])
    assert TranscriptParser(tx).model() == "Qwen 3.8 Max"
    only_synthetic = json.dumps({"type": "result", "model": "<synthetic>"})
    assert TranscriptParser(only_synthetic).model() is None   # falls back to requested
