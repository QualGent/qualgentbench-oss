"""Bare-model adapter: a minimal ReAct loop over MCP, so the only leaderboard
variable is the model. One MCP session lives for the whole episode (the device lock
is tied to it); the transcript is Claude stream-json so TranscriptParser scores it."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from .base import AgentAdapter, RunContext
from ..interactions import read_total

logger = logging.getLogger(__name__)

# Model-agnostic QA framing, kept identical across models; the per-task
# objective arrives as the user instruction.
SYSTEM_PROMPT = (
    "You are an autonomous mobile QA agent. You control a real mobile device "
    "exclusively through the provided tools — you cannot see the screen unless you "
    "call an observation tool. Work one step at a time: observe the current screen, "
    "decide the single next action, perform it with a tool, then observe the result "
    "before continuing. Never guess element coordinates; act on what the observation "
    "tools return. Follow the task instructions exactly. When the task is complete — "
    "or you have determined it cannot be completed — call mobile_report_result with "
    "the correct PASS or FAIL status, then stop."
)

# Tool that ends the episode once the model calls it.
_REPORT_TOOL = "mobile_report_result"

# Friendly aliases → full LiteLLM ids; unknown names pass through unchanged.
# Only models with function/tool calling can drive this benchmark — a
# non-tool model just no-ops.
_MODEL_ALIASES: dict[str, str] = {
    # Fireworks (hosted OSS, needs FIREWORKS_AI_API_KEY)
    "llama-3.3-70b": "fireworks_ai/accounts/fireworks/models/llama-v3p3-70b-instruct",
    "llama-3.1-405b": "fireworks_ai/accounts/fireworks/models/llama-v3p1-405b-instruct",
    "llama-3.1-70b": "fireworks_ai/accounts/fireworks/models/llama-v3p1-70b-instruct",
    "qwen2.5-72b": "fireworks_ai/accounts/fireworks/models/qwen2p5-72b-instruct",
    "qwen3-235b": "fireworks_ai/accounts/fireworks/models/qwen3-235b-a22b",
    "deepseek-v3": "fireworks_ai/accounts/fireworks/models/deepseek-v3",
    # Local via Ollama; ollama_chat/ is the chat+tools endpoint.
    # Override host with OLLAMA_API_BASE.
    "local-llama3.1": "ollama_chat/llama3.1",
    "local-qwen2.5": "ollama_chat/qwen2.5",
}


def _litellm_id(model: str) -> str:
    """Map a friendly alias to its full LiteLLM id; pass through anything else."""
    return _MODEL_ALIASES.get(model, model)

# Nudge a stalled model (prose, no tool call) a few times before giving up.
_MAX_NUDGES = 3

# Retry transient API errors so an infra hiccup isn't scored as model failure.
_API_RETRIES = 3


class NativeAdapter(AgentAdapter):
    """Bare-model agent: LiteLLM in a ReAct loop over the MCP tools."""

    name = "native"
    DEFAULT_MODEL = "gpt-4o"

    # run() is fully overridden; these stubs just keep the ABC instantiable.
    def command(self, instruction: str, context: RunContext) -> list[str]:  # noqa: D401
        raise NotImplementedError("NativeAdapter runs in-process; command() is unused.")

    def env(self, context: RunContext) -> dict[str, str]:
        return {}

    async def run(self, instruction: str, context: RunContext) -> tuple[str, int]:
        """Run the model↔MCP loop. Returns (stream-json transcript, exit_code)."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        transcript_path = context.run_dir / "agent" / "transcript.txt"
        transcript_path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []  # stream-json JSONL, written even on failure
        model = context.model
        disabled = set(context.disabled_tools or [])
        timeout_sec = float(getattr(context.task.agent, "timeout_sec", 900) or 900)
        max_steps = int(getattr(context.task.agent, "max_tool_calls", 150) or 150)
        deadline = time.monotonic() + timeout_sec

        mcp_url = f"{context.mcp_server.rstrip('/')}/mcp"
        exit_code = 0
        usage_totals = {"input": 0, "output": 0, "cached": 0}
        cost_total: float | None = 0.0

        try:
            async with streamablehttp_client(mcp_url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool_list = await session.list_tools()
                    tools = [
                        t for t in (tool_list.tools or [])
                        if t.name not in disabled
                    ]
                    if not tools:
                        raise RuntimeError("MCP exposed no callable tools.")
                    litellm_tools = [_mcp_tool_to_litellm(t) for t in tools]
                    logger.info(
                        "native loop: model=%s tools=%d (gated %d)",
                        model, len(tools), len(disabled),
                    )

                    messages: list[dict[str, Any]] = [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": instruction},
                    ]

                    cost_total = await self._loop(
                        session=session,
                        model=model,
                        messages=messages,
                        litellm_tools=litellm_tools,
                        lines=lines,
                        usage_totals=usage_totals,
                        max_steps=max_steps,
                        deadline=deadline,
                        interaction_log=context.run_dir / "interactions.json",
                    )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # surface as a failed run, not a crash
            logger.warning("native loop error: %s", exc)
            lines.append(json.dumps({"type": "error", "error": str(exc)}))
            exit_code = 1

        # Final cumulative usage/cost event (Claude stream-json "result" shape).
        lines.append(json.dumps(_result_event(usage_totals, cost_total)))

        transcript = "\n".join(lines) + "\n"
        transcript_path.write_text(transcript)
        return transcript, exit_code

    async def _loop(
        self,
        *,
        session: Any,
        model: str,
        messages: list[dict[str, Any]],
        litellm_tools: list[dict],
        lines: list[str],
        usage_totals: dict[str, int],
        max_steps: int,
        deadline: float,
        interaction_log: Path | None = None,
    ) -> float | None:
        """Drive the model↔tool loop. Returns total reported cost (or None).
        The agent must call mobile_report_result within budget — no report means
        no verdict; we never nudge or force a final report."""
        import litellm

        litellm.drop_params = True  # silently drop params a given model rejects
        cost_total: float | None = 0.0
        nudges = 0
        reported = False

        async def complete() -> Any:
            """One model call, retried on transient API/network/TaskGroup errors."""
            last: Exception | None = None
            for attempt in range(_API_RETRIES):
                remaining = deadline - time.monotonic()
                try:
                    return await litellm.acompletion(
                        model=_litellm_id(model),  # alias → full id; display stays `model`
                        messages=messages,
                        tools=litellm_tools,
                        tool_choice="auto",
                        timeout=min(max(remaining, 30), 300),
                    )
                except Exception as exc:  # noqa: BLE001 - transient API/network/TaskGroup
                    last = exc
                    logger.warning("litellm call failed (attempt %d/%d): %s",
                                   attempt + 1, _API_RETRIES, exc)
                    await asyncio.sleep(min(2 * (attempt + 1), 8))
            raise last  # type: ignore[misc]  # exhausted retries → surfaced to run()

        async def run_tool_calls(tool_calls: list) -> None:
            nonlocal reported
            for tc in tool_calls:
                name = tc.function.name
                args = _parse_args(tc.function.arguments)
                result_text = await _call_mcp_tool(session, name, args)
                lines.append(_tool_result_event(tc.id, result_text))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_text})
                if _REPORT_TOOL in name:
                    reported = True

        for step in range(max_steps):
            if deadline - time.monotonic() <= 0:
                logger.info("native loop hit time budget after %d steps", step)
                break
            # Budget on INTERACTIONS, the unit every adapter shares — counting loop
            # iterations would make native steps incomparable across agents. A turn
            # with no device tool costs nothing; the time budget bounds those.
            if interaction_log is not None and read_total(interaction_log) > max_steps:
                logger.info("native loop hit the %d-interaction budget", max_steps)
                break

            resp = await complete()
            _accumulate_usage(resp, usage_totals)
            cost_total = _accumulate_cost(resp, cost_total)

            message = resp.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])

            # Emit the assistant turn (stream-json) and append it for the next call.
            lines.append(_assistant_event(model, message.content, tool_calls))
            messages.append(_assistant_message(message.content, tool_calls))

            if not tool_calls:
                # Model replied with prose but no action. Nudge a few times, then stop.
                if nudges >= _MAX_NUDGES:
                    break
                nudges += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "Continue. Use a tool to act on the device, and call "
                        f"{_REPORT_TOOL} with PASS or FAIL when the task is finished."
                    ),
                })
                continue
            nudges = 0

            await run_tool_calls(tool_calls)
            if reported:
                break
        else:
            # No report within budget → no verdict → a miss. Concluding within
            # budget is part of the task.
            logger.info("native loop hit step budget (%d calls) without a report → miss", max_steps)

        return cost_total


# ── MCP / LiteLLM glue ───────────────────────────────────────────────────────


def _mcp_tool_to_litellm(tool: Any) -> dict:
    """Convert an MCP tool definition to an OpenAI/LiteLLM function-tool schema."""
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict) or not schema:
        schema = {"type": "object", "properties": {}}
    elif "type" not in schema:
        schema = {**schema, "type": "object"}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (getattr(tool, "description", "") or "")[:1024],
            "parameters": schema,
        },
    }


async def _call_mcp_tool(session: Any, name: str, args: dict) -> str:
    """Call an MCP tool and flatten its content blocks to text (errors included)."""
    try:
        result = await session.call_tool(name, args)
    except Exception as exc:
        return f"tool_error: {exc}"
    text = " ".join(
        getattr(block, "text", "") or ""
        for block in (getattr(result, "content", None) or [])
    ).strip()
    if getattr(result, "isError", False) and not text:
        text = "tool_error: the tool reported an error with no message."
    return text or "(no output)"


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── stream-json emission (matches transcript.TranscriptParser) ──────


def _assistant_event(model: str, content: str | None, tool_calls: list) -> str:
    blocks: list[dict] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in tool_calls:
        blocks.append({
            "type": "tool_use",
            "id": tc.id,
            "name": tc.function.name,
            "input": _parse_args(tc.function.arguments),
        })
    return json.dumps({"type": "assistant", "message": {"model": model, "content": blocks}})


def _tool_result_event(tool_use_id: str, text: str) -> str:
    return json.dumps({
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
            ]
        },
    })


def _result_event(usage_totals: dict[str, int], cost_total: float | None) -> dict:
    """Final cumulative usage event. The parser counts cache_read as cached input,
    so uncached input goes in input_tokens and cached input in cache_read."""
    cached = usage_totals["cached"]
    uncached = max(usage_totals["input"] - cached, 0)
    event: dict[str, Any] = {
        "type": "result",
        "usage": {
            "input_tokens": uncached,
            "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0,
            "output_tokens": usage_totals["output"],
        },
    }
    if cost_total is not None:
        event["total_cost_usd"] = round(cost_total, 6)
    return event


def _assistant_message(content: str | None, tool_calls: list) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
    return msg


def _accumulate_usage(resp: Any, totals: dict[str, int]) -> None:
    usage = getattr(resp, "usage", None)
    if not usage:
        return
    totals["input"] += int(getattr(usage, "prompt_tokens", 0) or 0)
    totals["output"] += int(getattr(usage, "completion_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details else 0
    totals["cached"] += int(cached or 0)


def _accumulate_cost(resp: Any, running: float | None) -> float | None:
    """Sum LiteLLM's per-call cost. Returns None once a call's cost is unknown."""
    if running is None:
        return None
    import litellm

    try:
        cost = litellm.completion_cost(completion_response=resp)
    except Exception:
        return None  # unknown model pricing → let the verdict estimate via pricing.py
    return running + float(cost or 0.0)
