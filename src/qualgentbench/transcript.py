"""Rich transcript parser — extracts structured tool events from agent JSONL transcripts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .interactions import (
    MCP_CHARGED_TOOLS,
    MCP_OBSERVATION_TOOLS,
    mcp_base_name,
    mcp_is_device_evidence,
)

# Both derived from the one tool table in interactions.py (QUA-2775) — a hand-kept
# copy here once knew 10 device tools and 2 reads while DevLoop exposed ~80 tools.
# A device tool is one the meter charges; an observation is a tool whose result is a
# read of the app (a whole-screen read or a targeted query).
DEVICE_TOOL_NAMES = MCP_CHARGED_TOOLS

OBSERVATION_TOOL_NAMES = MCP_OBSERVATION_TOOLS

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

# DevLoop's structured verdict tool. Bookkeeping in the tool table (never device
# evidence); journey mode reads it as its lowest-precedence REPORT source (QUA-2777).
REPORT_TOOL_NAME = "mobile_report_result"

TRACKED_TOOL_NAMES = (
    DEVICE_TOOL_NAMES
    + OBSERVATION_TOOL_NAMES
    + FILE_ACCESS_TOOL_NAMES
    + (REPORT_TOOL_NAME,)
    + ROUTINE_TOOL_NAMES
    + BENCH_ROUTINE_TOOL_NAMES
    + CREATION_TOOL_NAMES
)


# Strip screenshot base64 from results: a blob that size matches almost any
# short string by chance, while non-ASCII labels can never match.
_IMAGE_PAYLOAD = re.compile(r'"data"\s*:\s*"[A-Za-z0-9+/=\\]{200,}"')
# A JSON `\uXXXX` escape (a surrogate pair as ONE match), or an escaped backslash
# `\\`, consumed first so the `\u` after it stays literal: `\\u2022` is the six
# characters `\u2022` on the screen, not a bullet.
_UNICODE_ESCAPE = re.compile(
    r"\\\\"
    r"|\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    r"|\\u([0-9a-fA-F]{4})"
)


def _decode_escape(m: re.Match) -> str:
    if m.group(1):
        hi, lo = int(m.group(1), 16), int(m.group(2), 16)
        return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))
    if m.group(3):
        cp = int(m.group(3), 16)
        # A lone surrogate is not a character; keep the escape as it was sent.
        return m.group(0) if 0xD800 <= cp <= 0xDFFF else chr(cp)
    return m.group(0)                                    # an escaped backslash


def decode_unicode_escapes(text: str) -> str:
    """Decode the JSON `\\uXXXX` escapes in a tool result's text (surrogate pairs
    included), leaving every other character — an escaped backslash and the `\\u` it
    guards, `\\n`, `\\"` — exactly as it was.

    The ONE normaliser for MCP result text (QUA-2801). DevLoop answers
    `mobile_tap_and_observe(include_screenshot=true)` with `json.dumps(result,
    indent=2)`, which escapes every non-ASCII character. codex-cli records that text as
    sent (`\\u2022`); claude-code records the character (`\u2022`). Read raw, orgzly's
    `Getting Started with Orgzly  \u2022  Notes` could never witness on codex, so the
    same device output scored by AGENT (run 20260923-224921-0461). Text that carries
    no escape — claude's — passes through unchanged."""
    if not text or "\\u" not in text:
        return text
    return _UNICODE_ESCAPE.sub(_decode_escape, text)


# ── One normalisation for both agents (QUA-2776) ─────────────────────────────
# claude-code and codex-cli record the SAME MCP call in two shapes. claude-code
# names it `mcp__<server>__<tool>` and hands back a `tool_result` whose content is
# the MCP content list; codex names it `<tool>` beside a `server` field and nests the
# same content list under `item.result`. A Fable-vs-Astra board runs one model
# through each adapter, so any difference in how the two are read publishes as a
# MODEL gap. Every reader below goes through these two functions, never its own
# copy: a hand-kept `startswith("mcp__device")` once zeroed codex's MCP call count
# and kept its calls from ever satisfying the hunt probe gate.


def split_tool_name(name: str) -> tuple[str, str]:
    """(server, tool) of a recorded tool name. `mcp__device__mobile_tap` →
    ("device", "mobile_tap"); a name without the MCP prefix (codex's bare
    `mobile_tap`, claude's `Bash`) → ("", name). The server of a codex call lives
    beside the name, not in it — the caller supplies it."""
    name = name or ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[1], parts[2]
    return "", name


def tool_base_name(name: str) -> str:
    """The tool name with any `mcp__<server>__` prefix stripped — what both agents
    call the same tool."""
    return split_tool_name(name)[1]


def mcp_result_text(result: object) -> str:
    """The TEXT an MCP tool call returned, however the agent recorded it: text blocks
    joined with a space, image blocks dropped. Accepts claude's `tool_result.content`
    (a string or a block list) and codex's `item.result` (a dict carrying `content`;
    one with no content list falls back to its other fields as JSON, e.g. a
    `structured_content`-only answer).

    Images are DROPPED, not stubbed: a short probe can collide with an arbitrary
    base64 run and forge the evidence the gate exists to demand, and a stub such as
    `<image>` is text neither agent was shown. DevLoop's `mobile_observe_screen`
    returns [ImageContent, TextContent(elements JSON)]; before this, claude kept the
    text block and codex kept `json.dumps` of the whole result, so the grounding text
    differed by adapter."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return " ".join(
            str(b.get("text", "")) for b in result
            if isinstance(b, dict) and b.get("type") == "text"
        )
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, (list, str)):
            return mcp_result_text(blocks)
        return json.dumps({k: v for k, v in result.items() if k != "content"})
    return str(result)


def codex_mcp_result(item: dict) -> tuple[str, bool] | None:
    """(text, success) of a completed codex `mcp_tool_call` item, or None while it is
    still in progress. A protocol error (`error.message`, no result) is the text, as
    claude-code shows the same failure as its tool_result's text; a tool that
    answered `isError` arrives as status `failed` with its content kept."""
    if not isinstance(item, dict):
        return None
    status = str(item.get("status") or "").lower()
    error = item.get("error")
    if status == "in_progress" or ("result" not in item and not error):
        return None
    text = mcp_result_text(item.get("result"))
    if not text and error:
        text = str(error.get("message") or "") if isinstance(error, dict) else str(error)
    ok = (not error and status not in {"failed", "error", "cancelled"}
          and not any(p in text.lower() for p in DEFINITE_ERRORS))
    return text, ok


def claude_result_success(text: str, *, is_error: bool, mcp: bool) -> bool:
    """claude-code's twin of `codex_mcp_result`'s success: the text rule both agents
    share, plus the MCP `isError` flag — codex reads it as status `failed`, claude
    as `is_error`. Applied to MCP tools only; a shell tool's non-zero exit is the
    raw arm's business and is left exactly as it was."""
    if mcp and is_error:
        return False
    return not any(p in text.lower() for p in DEFINITE_ERRORS)


def clean_result_text(text: str) -> str:
    """Make a tool result safe to keyword-match against."""
    if not text:
        return text
    text = _IMAGE_PAYLOAD.sub('"data": "<image>"', text)
    return decode_unicode_escapes(text)


@dataclass
class ToolEvent:
    # `name` is NORMALISED — the `mcp__<server>__` prefix is stripped at parse time
    # and kept in `server` — so claude-code's `mcp__device__mobile_tap` and codex's
    # `mobile_tap` are one event (QUA-2776).
    id: str
    name: str
    input: dict = field(default_factory=dict)
    result_text: str = ""
    success: bool = False
    # The server REFUSED the call (Claude's `is_error` on the tool_result; Codex's
    # `error` / failed `status`). `success` also goes False on an MCP refusal (both
    # agents, QUA-2776); `is_error` stays the structural fact alone, where `success`
    # adds the DEFINITE_ERRORS text heuristic, which misses a validation refusal such
    # as DevLoop's "code_investigation is required for FAIL results".
    is_error: bool = False
    server: str = ""

    @property
    def is_device_tool(self) -> bool:
        # run_routine executes device steps internally — counts as device interaction
        return any(t in self.name for t in DEVICE_TOOL_NAMES) or "run_routine" in self.name

    @property
    def is_observation(self) -> bool:
        return any(t in self.name for t in OBSERVATION_TOOL_NAMES)

    @property
    def is_device_evidence(self) -> bool:
        """The one device-tool predicate for scorers: the tool table says its call
        and result are device evidence (`interactions.MCP_TOOL_RULES`, exact names).
        Read off the normalised name, so it answers the same for both agents."""
        return mcp_is_device_evidence(self.name)

    @property
    def is_file_access(self) -> bool:
        return (any(t in self.name for t in FILE_ACCESS_TOOL_NAMES)
                or self.server == "filesystem")

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


# ── Codex usage that `codex exec --json` never printed (QUA-2803) ─────────────
#
# `codex exec` runs the whole episode as ONE turn, and its only usage event is the
# single `turn.completed` written when that turn ends. A budget-truncated episode is
# killed first, so stdout carries no usage at all. Measured on codex-cli 0.156.1
# against a mock backend, the turn cut short by SIGTERM (what `base.run` sends),
# SIGINT or SIGKILL wrote no `turn.completed` in every case, so a graceful stop does
# not recover it either. Codex's session rollout
# (`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`, written unless `--ephemeral`)
# does keep an `event_msg` / `token_count` with the cumulative `total_token_usage`
# after every completed response, flushed as it goes, so it survives even SIGKILL.
# `state_*.sqlite` `threads.tokens_used` holds the same total but only as one number,
# with no input/cached/output split to price. The codex adapter therefore reads the
# rollout after the agent exits and, only when the transcript has no `turn.completed`,
# appends this one harness-authored line, which `token_usage` reports as
# `usage_source: codex_state`, never as `turns`.
CODEX_STATE_USAGE_EVENT = "qgb.codex_state_usage"


def has_codex_turn_completed(transcript: str) -> bool:
    """True when some line of the transcript is a Codex `turn.completed` event."""
    for line in transcript.splitlines():
        line = line.strip()
        if '"turn.completed"' not in line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("type") == "turn.completed":
            return True
    return False


def codex_rollout_usage(codex_home: Path) -> dict | None:
    """Codex's own running token total for this episode, from its session rollouts.

    Each rollout's LAST `token_count` with a non-null `info` is that thread's
    cumulative `total_token_usage`; rollouts are summed (one per thread, and
    `codex exec` makes one unless the agent spawns a sub-agent, whose tokens are
    billed too). A partial last line (the process killed mid-write) is skipped.
    None when no rollout carries a count — no number, never a zero.
    """
    sessions = Path(codex_home) / "sessions"
    if not sessions.is_dir():
        return None
    fields = ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "total_tokens")
    total = dict.fromkeys(fields, 0)
    counted = 0
    for rollout in sorted(sessions.rglob("rollout-*.jsonl")):
        last: dict | None = None
        try:
            lines = rollout.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"token_count"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = e.get("payload") if isinstance(e, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(usage, dict):
                last = usage
        if last is None:
            continue
        counted += 1
        for key in fields:
            total[key] += _usage_int(last, key)
    if not counted:
        return None
    return {**total, "rollouts": counted}


def codex_state_usage_line(codex_home: Path, transcript: str) -> str | None:
    """The `qgb.codex_state_usage` line to append to a codex transcript, or None.

    None when the transcript already has a `turn.completed` (Codex answered for
    itself; the two are never combined) or when no rollout carries a count. The
    line holds counts only, no path, so no transcript scanner reads anything new.
    """
    if has_codex_turn_completed(transcript):
        return None
    usage = codex_rollout_usage(codex_home)
    if usage is None:
        return None
    rollouts = usage.pop("rollouts")
    return json.dumps({
        "type": CODEX_STATE_USAGE_EVENT,
        "source": "codex rollout token_count (harness-appended; no turn.completed)",
        "rollouts": rollouts,
        "usage": usage,
    })


class TranscriptParser:
    """Parses agent JSONL transcripts into structured ToolEvents. Supports
    Claude Code stream-json and item-completed MCP tool-call shapes."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript
        self._events = self._parse(transcript)

    def token_usage(self) -> dict:
        """Token usage (and reported cost) from the transcript, with `usage_source`
        naming which of four shapes answered — so "nobody counted" is a fact in the
        artifact rather than a zero that reads as free (`pricing.usage_metrics`).

        Four shapes, tried in that order:

        ``result``       Claude's cumulative final result event, which also carries
                         ``total_cost_usd``.
        ``turns``        the sum of Codex ``turn.completed`` usage.
        ``stream``       the per-REQUEST usage on Claude's ``assistant`` events.
        ``codex_state``  the harness-appended ``qgb.codex_state_usage`` line: Codex's
                         own running total, read from its session rollout after the
                         agent exited (`codex_rollout_usage`). Consulted only when
                         there is no ``turn.completed`` at all.

        Neither CLI's end-of-run event survives a budget truncation. The budget hook
        drops the sentinel and `base.run()` SIGTERMs, then SIGKILLs, the process
        group, so neither Claude's `result` event nor Codex's `turn.completed` is
        written. Codex is NOT immune: `codex exec` runs the whole episode as ONE turn
        and writes ONE `turn.completed`, at the end, carrying the episode's usage.
        Measured on codex-cli 0.156.1 against a mock backend: a turn cut short by
        SIGTERM, SIGINT or SIGKILL writes no `turn.completed` and no other usage to
        stdout (QUA-2803; run 20260924-043254-1e0b, `contacts-favorite~clean`,
        41/40 steps, published `usage_source: none`). Codex's rollout file does keep
        a cumulative `token_count` after every completed response, so the codex
        adapter reads it and appends it as its own line. For Claude, the
        per-request stream is the fallback. Measured on run 20260916-234512-18ac:
        both claude-code arms published `total_tokens: 0` and `cost_usd: 0.0` over
        transcripts holding 44 and 48 real requests — ~2.9M and ~3.5M tokens, about
        $1.12 and $1.29.

        Both fallbacks count COMPLETED requests only: the request in flight at the
        kill was never answered with usage, on either CLI. They are the same
        quantity, not an estimate of different precision.

        `assistant` events are DEDUPED by `message.id`: the CLI emits one event per
        content block, so a 44-request episode arrives as 86 events carrying each
        request's usage two or three times. Summing them raw would roughly double the
        bill. Ids repeat only within one API response, so first-wins is the request.

        Summing per-request usage is the right arithmetic for billing even though the
        conversation prefix is resent every turn: each request is charged for its own
        full input, cache reads at the cache rate. Folded exactly as the `result`
        branch folds its one dict, so the two shapes stay comparable.
        """
        turn_in = turn_out = turn_cached = 0
        codex_state: tuple[int, int, int] = (0, 0, 0)
        claude_result: dict | None = None
        # message id → that request's usage. dict, not a list: see the docstring.
        stream: dict[tuple[str, str], dict] = {}

        for n, line in enumerate(self._transcript.splitlines()):
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
                i, c, o = _codex_totals(e["usage"])
                turn_in, turn_cached, turn_out = turn_in + i, turn_cached + c, turn_out + o
            elif etype == CODEX_STATE_USAGE_EVENT and isinstance(e.get("usage"), dict):
                # A running total, not a delta: the last one wins.
                codex_state = _codex_totals(e["usage"])
            elif etype == "assistant":
                message = e.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                # Fall back to the event uuid, then to the line number, rather than
                # collapsing every id-less request onto one key.
                key = (("id", str(message["id"])) if message.get("id")
                       else ("uuid", str(e.get("uuid"))) if e.get("uuid")
                       else ("line", str(n)))
                stream.setdefault(key, message["usage"])

        reported_cost = None
        if claude_result is not None:
            cost = claude_result.get("total_cost_usd")
            reported_cost = float(cost) if isinstance(cost, (int, float)) else None

        candidates = (
            ("result", _anthropic_totals(
                [claude_result["usage"]] if claude_result is not None else [])),
            ("turns", (turn_in, turn_cached, turn_out)),
            ("stream", _anthropic_totals(stream.values())),
            # Last on purpose: it is read only when no `turn.completed` answered, so
            # it can never be added to or blended with a turn-measured episode.
            ("codex_state", codex_state),
        )
        source, (inp, cached, out) = "none", (0, 0, 0)
        for name, totals in candidates:
            if any(totals):
                source, (inp, cached, out) = name, totals
                break

        return {
            "input_tokens": inp,
            "output_tokens": out,
            "cached_input_tokens": cached,
            "total_tokens": inp + out,
            "reported_cost_usd": reported_cost,
            # Which shape answered, or "none" when the transcript reported no usage
            # at all. Read by `pricing.usage_metrics`; never inferred from the counts,
            # because a real episode may legitimately spend very little.
            "usage_source": source,
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
            etype = event.get("type")

            # Claude Code stream-json. Every tool_use is an event. Handled here and
            # NOT by the generic walk below, which would re-read these results with
            # its own joiner and could mint phantom events from a tool's arguments.
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        server, name = split_tool_name(block.get("name", ""))
                        calls[block["id"]] = ToolEvent(
                            id=block["id"],
                            name=name,
                            input=block.get("input", {}),
                            server=server,
                        )
                continue

            if etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    tid = block.get("tool_use_id", "")
                    if tid not in calls:
                        continue
                    evt = calls[tid]
                    text = mcp_result_text(block.get("content", ""))
                    evt.result_text = clean_result_text(text)
                    evt.is_error = bool(block.get("is_error"))
                    evt.success = claude_result_success(
                        text, is_error=evt.is_error, mcp=bool(evt.server))
                continue

            # Codex `exec --json` MCP call: EVERY one is an event, as on claude —
            # filtering these by TRACKED_TOOL_NAMES (the generic walk's rule) kept
            # `mobile_launch_app` & co. out of codex's events alone.
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "mcp_tool_call":
                tid = item.get("id") if isinstance(item.get("id"), str) else None
                if tid is None:
                    tid = f"anonymous-{anonymous_counter}"
                    anonymous_counter += 1
                evt = calls.setdefault(tid, ToolEvent(
                    id=tid,
                    name=tool_base_name(str(item.get("tool") or "")),
                    input=_parse_jsonish_dict(item.get("arguments")) or {},
                    server=str(item.get("server") or ""),
                ))
                done = codex_mcp_result(item)
                if done is not None:
                    evt.result_text = clean_result_text(done[0])
                    evt.success = done[1]
                    evt.is_error = _tool_refused(item)
                continue

            # Other Codex / Responses-style JSONL varies by CLI version — scan nested
            # records. Skip tool_reference objects: schema refs, not real calls.
            for obj in _walk_dicts(event):
                if obj.get("type") == "tool_reference":
                    continue
                name = _tool_name(obj)
                if name and _is_tracked_tool(name):
                    tid = _tool_id(obj) or f"anonymous-{anonymous_counter}"
                    if tid.startswith("anonymous-"):
                        anonymous_counter += 1
                    server, base = split_tool_name(name)
                    calls.setdefault(
                        tid,
                        ToolEvent(id=tid, name=base, input=_tool_input(obj),
                                  server=server or str(obj.get("server") or "")),
                    )
                    if _tool_refused(obj):
                        calls[tid].is_error = True

                    text = _tool_output(obj)
                    if text:
                        evt = calls[tid]
                        evt.result_text = clean_result_text(text)
                        evt.success = _tool_success(obj, text)

                tid = _result_tool_id(obj)
                if tid and tid in calls:
                    if _tool_refused(obj):
                        calls[tid].is_error = True
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

    def report_tool_calls(self) -> list[ToolEvent]:
        """Every `mobile_report_result` call, in order, matched on the EXACT base name
        (`mcp__device__mobile_report_result` → `mobile_report_result`) — the same
        lookup `interactions.MCP_TOOL_RULES` uses, never a substring. Refused calls
        (`is_error`) are included; the caller decides what a refusal means."""
        return [e for e in self._events if mcp_base_name(e.name) == REPORT_TOOL_NAME]

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


def _tool_refused(obj: dict) -> bool:
    """Did the server refuse this (Codex-shaped) call? Structural fields only — the
    same ones `_tool_success` reads — so a result that merely mentions an error is
    not a refusal."""
    if obj.get("error"):
        return True
    status = obj.get("status")
    return isinstance(status, str) and status.lower() in {"failed", "error"}


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


def _anthropic_totals(usages) -> tuple[int, int, int]:
    """Fold Anthropic-shaped usage dicts into (input, cached, output).

    `input` is the whole billed input — uncached + cache writes + cache reads —
    and `cached` is the cache-READ part of it, which is the split
    `pricing.compute_cost_usd` prices. One dict in gives exactly what the cumulative
    `result` event used to compute inline; a whole episode's requests in gives the
    same quantity summed over requests.
    """
    inp = cached = out = 0
    for u in usages:
        if not isinstance(u, dict):
            continue
        cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
        inp += (int(u.get("input_tokens", 0) or 0)
                + int(u.get("cache_creation_input_tokens", 0) or 0)
                + cache_read)
        cached += cache_read
        out += int(u.get("output_tokens", 0) or 0)
    return inp, cached, out


def _codex_totals(u: dict) -> tuple[int, int, int]:
    """Fold one Codex usage dict (`turn.completed`, or a rollout `token_count`'s
    `total_token_usage` — the same field names) into (input, cached, output)."""
    output = _usage_int(u, "output_tokens", "completion_tokens")
    # Codex nests reasoning tokens under output; only count a standalone reasoning
    # field when no aggregate is present.
    return (_usage_int(u, "input_tokens", "prompt_tokens"),
            _cached_input_tokens(u),
            output or _reasoning_output_tokens(u))


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
