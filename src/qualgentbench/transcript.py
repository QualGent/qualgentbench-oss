"""Rich transcript parser — extracts structured tool events from agent JSONL transcripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DEVICE_TOOL_NAMES = (
    "mobile_observe_screen",
    "mobile_tap_and_observe",
    "mobile_tap",
    "mobile_type_text",
    "mobile_swipe",
    "mobile_swipe_coordinates",
    "mobile_edit_field",
    "mobile_press_button",
    "mobile_long_press",
    "mobile_double_tap",
)

OBSERVATION_TOOL_NAMES = (
    "mobile_observe_screen",
    "mobile_tap_and_observe",
)

# Unambiguous MCP error signatures in result text.
# "error" and "failed" are intentionally excluded — MCP embeds them in success
# responses (e.g. "check device logs for errors") causing false negatives.
DEFINITE_ERRORS = (
    "device-bound",
    "device-busy",
    "is device-bound",
    "call qg_acquire_device",
    "requires qg_acquire",
    "not found",
    "failed_step",
    "failure_type",
    "credential_error",
    "element_not_found",
    "postcondition_timeout",
    "unsupported_action",
    "execution_error",
)

FILE_ACCESS_TOOL_NAMES = (
    "Read",
    "Bash",
    "Glob",
    "LS",
    "command_execution",
    "file_change",
    "mcp__filesystem",
)

ROUTINE_TOOL_NAMES = (
    "find_routine",
    "apply_routine",
    "record_routine",
    "update_routine",
    "mobile_get_action_log",
)

# Bench-routines sidecar tools — tracked separately so the verifier can
# distinguish local-sidecar usage from MCP backend usage.
BENCH_ROUTINE_TOOL_NAMES = (
    "run_routine",
    "list_routines",
)

# Authoring tools, tracked so codex transcripts keep these events too — the
# codex fallback only keeps events whose names are tracked here.
CREATION_TOOL_NAMES = (
    "create_test_case",
    "update_test_case",
    "upload_test_file",
    "list_test_cases",
    "get_test_case",
    "list_credentials",
    "mobile_insert_credential",
)

TRACKED_TOOL_NAMES = (
    DEVICE_TOOL_NAMES
    + OBSERVATION_TOOL_NAMES
    + FILE_ACCESS_TOOL_NAMES
    + ("mobile_report_result",)
    + ROUTINE_TOOL_NAMES
    + BENCH_ROUTINE_TOOL_NAMES
    + CREATION_TOOL_NAMES
)


# Strip screenshot base64 from results: a blob that size matches almost any
# short string by chance, while non-ASCII labels can never match.
_IMAGE_PAYLOAD = re.compile(r'"data"\s*:\s*"[A-Za-z0-9+/=\\]{200,}"')
# Element labels arrive JSON-escaped; decode so specs can use the character a
# human sees on the button.
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def clean_result_text(text: str) -> str:
    """Make a tool result safe to keyword-match against."""
    if not text:
        return text
    text = _IMAGE_PAYLOAD.sub('"data": "<image>"', text)
    return _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


@dataclass
class ToolEvent:
    id: str
    name: str
    input: dict = field(default_factory=dict)
    result_text: str = ""
    success: bool = False

    @property
    def is_device_tool(self) -> bool:
        # run_routine executes device steps internally — counts as device interaction
        return any(t in self.name for t in DEVICE_TOOL_NAMES) or "run_routine" in self.name

    @property
    def is_observation(self) -> bool:
        return any(t in self.name for t in OBSERVATION_TOOL_NAMES)

    @property
    def is_file_access(self) -> bool:
        return any(t in self.name for t in FILE_ACCESS_TOOL_NAMES)

    @property
    def is_routine_tool(self) -> bool:
        return any(t in self.name for t in ROUTINE_TOOL_NAMES)

    @property
    def input_str(self) -> str:
        return json.dumps(self.input).lower()

    @property
    def result_json(self) -> object | None:
        try:
            return json.loads(self.result_text)
        except json.JSONDecodeError:
            return None


class TranscriptParser:
    """Parses agent JSONL transcripts into structured ToolEvents. Supports
    Claude Code stream-json and item-completed MCP tool-call shapes."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self._events = self._parse(transcript)

    def token_usage(self) -> dict:
        """Token usage (and reported cost) from the transcript — Claude's
        cumulative final result event, or the sum of Codex turn.completed
        deltas. reported_cost_usd is None unless the agent reported it."""
        inp = out = cached = 0
        claude_result: dict | None = None

        for line in self._transcript.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = e.get("type")
            if etype == "result" and isinstance(e.get("usage"), dict):
                claude_result = e  # cumulative; last one wins
            elif etype == "turn.completed" and isinstance(e.get("usage"), dict):
                u = e["usage"]
                inp += _usage_int(u, "input_tokens", "prompt_tokens")
                cached += _cached_input_tokens(u)
                output = _usage_int(u, "output_tokens", "completion_tokens")
                # Codex nests reasoning tokens under output; only count a
                # standalone reasoning field when no aggregate is present.
                out += output or _reasoning_output_tokens(u)

        reported_cost = None
        if claude_result is not None:
            u = claude_result["usage"]
            cache_creation = int(u.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
            inp = int(u.get("input_tokens", 0) or 0) + cache_creation + cache_read
            cached = cache_read
            out = int(u.get("output_tokens", 0) or 0)
            cost = claude_result.get("total_cost_usd")
            reported_cost = float(cost) if isinstance(cost, (int, float)) else None

        return {
            "input_tokens": inp,
            "output_tokens": out,
            "cached_input_tokens": cached,
            "total_tokens": inp + out,
            "reported_cost_usd": reported_cost,
        }

    def model(self) -> str | None:
        """The model actually used, read from the transcript — the requested
        label can differ when the CLI picks its own default. Last non-empty
        value wins; None if absent. "<synthetic>" is claude's stamp on a
        locally-synthesized event (e.g. the final result after a failed
        compaction) — no model produced it, so it never counts: it once split
        one run's board into a second phantom agent+model row."""
        found: str | None = None
        for line in self._transcript.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            for obj in _walk_dicts(e):
                for key in ("model", "model_slug", "model_name"):
                    m = obj.get(key)
                    if isinstance(m, str) and m.strip() and m.strip() != "<synthetic>":
                        found = m.strip()
        return found

    def _parse(self, transcript: str) -> list[ToolEvent]:
        calls: dict[str, ToolEvent] = {}
        anonymous_counter = 0

        for line in transcript.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Claude Code stream-json.
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        calls[block["id"]] = ToolEvent(
                            id=block["id"],
                            name=block.get("name", ""),
                            input=block.get("input", {}),
                        )

            if event.get("type") == "user":
                for block in event.get("message", {}).get("content", []):
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    tid = block.get("tool_use_id", "")
                    if tid not in calls:
                        continue

                    content = block.get("content", "")
                    if isinstance(content, list):
                        text = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    else:
                        text = str(content)

                    evt = calls[tid]
                    evt.result_text = clean_result_text(text)
                    evt.success = not any(p in text.lower() for p in DEFINITE_ERRORS)

            # Codex / Responses-style JSONL varies by CLI version — scan nested
            # records. Skip tool_reference objects: schema refs, not real calls.
            for obj in _walk_dicts(event):
                if obj.get("type") == "tool_reference":
                    continue
                name = _tool_name(obj)
                if name and _is_tracked_tool(name):
                    tid = _tool_id(obj) or f"anonymous-{anonymous_counter}"
                    if tid.startswith("anonymous-"):
                        anonymous_counter += 1
                    calls.setdefault(
                        tid,
                        ToolEvent(id=tid, name=name, input=_tool_input(obj)),
                    )

                    text = _tool_output(obj)
                    if text:
                        evt = calls[tid]
                        evt.result_text = clean_result_text(text)
                        evt.success = _tool_success(obj, text)

                tid = _result_tool_id(obj)
                if tid and tid in calls:
                    text = _tool_output(obj)
                    if text:
                        evt = calls[tid]
                        evt.result_text = clean_result_text(text)
                        evt.success = _tool_success(obj, text)

        return list(calls.values())

    # ── Accessors ──────────────────────────────────────────────────────────────

    def events(self) -> list[ToolEvent]:
        return self._events

    def total_tool_calls(self) -> int:
        """Every tool call the agent made (any tool), not just the tracked
        subset in `events()` — the honest total the budget counts. Codex emits
        item.started + item.completed per call, so dedup by id."""
        ids: set[str] = set()
        anon = 0
        for line in self._transcript.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in ("mcp_tool_call", "command_execution"):
                if etype not in (None, "item.completed"):
                    continue  # count each call once, at completion
                tid = item.get("id")
                if isinstance(tid, str):
                    ids.add(tid)
                else:
                    anon += 1
            elif etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id")
                        if isinstance(tid, str):
                            ids.add(tid)
                        else:
                            anon += 1
        return len(ids) + anon

    def successful_device_events(self) -> list[ToolEvent]:
        return [e for e in self._events if e.is_device_tool and e.success]

    def observation_texts(self) -> list[str]:
        """Text content from all successful screen observations."""
        return [e.result_text for e in self._events if e.is_observation and e.success]

    def screens_visited(self) -> str:
        """All observation text combined — used for navigation checks."""
        return " ".join(self.observation_texts()).lower()

    def reported_status(self) -> str | None:
        """Status from mobile_report_result call, uppercased."""
        for e in self._events:
            if "mobile_report_result" in e.name:
                status = e.input.get("status", "")
                return status.upper() if status else None
        return None

    def accessed_paths(self) -> list[str]:
        """JSON-serialised input dicts from all file-access tool calls."""
        return [json.dumps(e.input) for e in self._events if e.is_file_access]

    def routine_events(self) -> list[ToolEvent]:
        return [e for e in self._events if e.is_routine_tool]

    def bench_routine_events(self) -> list[ToolEvent]:
        """Events from the local bench-routines sidecar (run_routine / list_routines)."""
        return [e for e in self._events if any(t in e.name for t in BENCH_ROUTINE_TOOL_NAMES)]

    def called_routine_tool(self, *tool_substrings: str) -> bool:
        return any(
            any(sub in e.name for sub in tool_substrings)
            for e in self.routine_events()
        )

    # ── Oracle-driven checks ───────────────────────────────────────────────────

    def navigation_visited(self, screen_keywords: list[str]) -> bool:
        """True if any keyword from this screen's list appears in any observation."""
        screens = self.screens_visited()
        return any(kw.lower() in screens for kw in screen_keywords)

    # How many events after a tap may still count as that tap's observation;
    # 2 leaves room for one interleaved call without drifting.
    _POST_TAP_WINDOW = 2

    def interaction_performed(
        self,
        tool_patterns: list[str],
        target_keywords: list[str],
    ) -> bool:
        """True if a successful matching call targeted a keyword — checked in
        the input, the result, or the next observation. The follow-up checks
        cover coordinate taps, whose args carry no label at all."""
        for i, e in enumerate(self._events):
            if not any(p in e.name for p in tool_patterns):
                continue
            if not e.success:
                continue
            if any(kw.lower() in e.input_str for kw in target_keywords):
                return True
            if any(kw.lower() in e.result_text.lower() for kw in target_keywords):
                return True
            for follow in self._events[i + 1: i + 1 + self._POST_TAP_WINDOW]:
                if not (follow.is_observation and follow.success):
                    continue
                if any(kw.lower() in follow.result_text.lower() for kw in target_keywords):
                    return True
        return False

    def interaction_had_no_screen_change(
        self,
        tool_patterns: list[str],
        target_keywords: list[str],
    ) -> bool:
        """True if a tap-and-observe on the target reported screen_changed=false
        — machine evidence for "the expected screen does not open" bugs."""
        for e in self._events:
            if "tap_and_observe" not in e.name:
                continue
            if not any(p in e.name for p in tool_patterns):
                continue
            if not e.success:
                continue
            if not _event_targets_keywords(e, target_keywords):
                continue
            if _contains_screen_changed_false(e.result_json):
                return True
        return False


def _walk_dicts(value: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        found.append(value)
        for child in value.values():
            found.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dicts(child))
    return found


def _tool_name(obj: dict) -> str | None:
    for key in ("name", "tool", "tool_name", "function_name"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    if obj.get("type") == "command_execution":
        return "command_execution"
    return None


def _is_tracked_tool(name: str) -> bool:
    return any(tool in name for tool in TRACKED_TOOL_NAMES)


def _tool_id(obj: dict) -> str | None:
    for key in ("id", "call_id", "tool_call_id", "tool_use_id"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _result_tool_id(obj: dict) -> str | None:
    for key in ("call_id", "tool_call_id", "tool_use_id"):
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return None


def _tool_input(obj: dict) -> dict:
    for key in ("input", "arguments", "args", "parameters"):
        value = obj.get(key)
        parsed = _parse_jsonish_dict(value)
        if parsed is not None:
            return parsed
    if obj.get("type") == "command_execution":
        command = obj.get("command")
        if isinstance(command, str):
            return {"command": command}
    return {}


def _parse_jsonish_dict(value: object) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tool_output(obj: dict) -> str:
    for key in ("output", "result", "result_text", "content"):
        if key not in obj:
            continue
        value = obj[key]
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return " ".join(parts)
        if isinstance(value, dict):
            text = value.get("text") or value.get("content") or value.get("message")
            if isinstance(text, str):
                return text
            return json.dumps(value)
    return ""


def _tool_success(obj: dict, text: str) -> bool:
    if obj.get("error"):
        return False
    status = obj.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error", "cancelled"}:
        return False
    exit_code = obj.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return False
    return not any(p in text.lower() for p in DEFINITE_ERRORS)


def _usage_int(usage: dict, *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if value is not None:
            return int(value or 0)
    return 0


def _nested_usage_int(
    usage: dict,
    field_names: tuple[str, ...],
    *container_keys: str,
) -> int:
    for container_key in container_keys:
        container = usage.get(container_key)
        if not isinstance(container, dict):
            continue
        for field_name in field_names:
            value = container.get(field_name)
            if value is not None:
                return int(value or 0)
    return 0


def _cached_input_tokens(usage: dict) -> int:
    return _usage_int(usage, "cached_input_tokens") or _nested_usage_int(
        usage,
        ("cached_tokens", "cache_read_input_tokens"),
        "input_token_details",
        "prompt_tokens_details",
    )


def _reasoning_output_tokens(usage: dict) -> int:
    return _usage_int(usage, "reasoning_output_tokens") or _nested_usage_int(
        usage,
        ("reasoning_tokens",),
        "output_token_details",
        "completion_tokens_details",
    )


def _event_targets_keywords(event: ToolEvent, target_keywords: list[str]) -> bool:
    if any(kw.lower() in event.input_str for kw in target_keywords):
        return True
    return any(kw.lower() in event.result_text.lower() for kw in target_keywords)


def _contains_screen_changed_false(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("screen_changed") is False:
            return True
        return any(_contains_screen_changed_false(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_screen_changed_false(child) for child in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
        return _contains_screen_changed_false(parsed)
    return False
