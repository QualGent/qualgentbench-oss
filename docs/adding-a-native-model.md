# Adding a native model (bare LLMs, including local models via Ollama)

The `native` agent is the harness's own minimal tool loop: one system prompt
(identical for every model, on purpose), the MCP device tools converted to
OpenAI-style function schemas, and LiteLLM for the API call. It exists to answer
"what does the bare model do?" — no CLI agent, no scaffolding. Any model LiteLLM can
reach can be benchmarked, **if it supports function/tool calling** — the loop scores
tool use, so a non-tool model just no-ops for three turns and records an empty
episode.

## Zero-code path

Any full LiteLLM model id passes straight through — unknown ids are used verbatim:

```bash
uv sync --all-extras          # native needs the optional litellm dependency

uv run qualgent-bench run --agent native \
  --models ollama_chat/llama3.1 \
  --app birday --mode hunt --trials 1 --device emulator-5554 \
  --mcp-server http://127.0.0.1:51821
```

Notes on that command:
- **`--mcp-server` is required for native.** The loop drives the device exclusively
  through MCP tools (it opens one session and holds it for the whole episode); there
  is no bare-adb arm for a bare model.
- **Ollama**: use the `ollama_chat/` prefix (the chat endpoint with tool support,
  not `ollama/`). Host override via `OLLAMA_API_BASE` in `.env`
  (default `http://localhost:11434`).
- **Any OpenAI-compatible server** (vLLM, LM Studio, llama.cpp server...): standard
  LiteLLM conventions apply — `openai/<model>` with `OPENAI_API_BASE` +
  `OPENAI_API_KEY` pointing at your endpoint. The harness adds no `base_url`
  machinery of its own; it is all LiteLLM env-var convention.

## Giving the model a short name

`_MODEL_ALIASES` in `src/qualgentbench/adapters/native.py` maps a friendly name to
the full LiteLLM id:

```python
"local-llama3.1": "ollama_chat/llama3.1",
```

The short name is what appears on the leaderboard; the full id is only used for the
API call. Optionally add the short name to `AGENT_MODELS["native"]` in `cli.py` to
include it in the default sweep when `--models` is omitted. Careful with collisions:
the board strips everything before the last `/`, so two providers serving a model
with the same trailing name land in one row.

## Cost column

`pricing.py` has per-model rates keyed by name (falling back to the segment after
the last `/`). Local models aren't in the table, so their cost is `None` and the
column stays blank — that is expected, not an error. Add an entry only if you want a
dollar number.

## What the loop does (so you can debug it)

Per step: `litellm.acompletion(model, messages, tools, tool_choice="auto")`, three
retries with backoff on any exception; tool calls executed over the MCP session with
errors flattened into the tool result text (`tool_error: ...`) rather than raised.
The episode ends on the `mobile_report_result` tool, the step budget, the time
budget, or three consecutive turns without a tool call. There is no forced final
report — a model that never reports scores its misses. Budgeting follows the same
one-counter rule as every agent: the loop reads `interactions.json` (written by the
MCP meter below it), never counts its own iterations.

The transcript is written in Claude stream-json, so scoring, evidence bundles, and
token/cost accounting work identically to the CLI agents.

## Constraints worth knowing before quoting a number

- **Tool calling is mandatory** (see above). Test outside the benchmark first:
  a quick LiteLLM call with one dummy tool tells you in seconds.
- **Context size is the practical limit for small local models.** The full MCP tool
  schema plus accumulated screen observations is large, and the native loop does no
  compaction. A 4k-context model will truncate mid-episode; 32k+ is realistic for a
  full hunt.
- **Strict-schema backends can 400 on tool definitions.** The tool schemas come from
  the MCP server verbatim (only `type: "object"` is forced). A backend that rejects
  a schema feature the server emits will fail at the first call — visible in the
  transcript as the retried error, and the episode scores as a failure, not an
  exclusion.
- **`litellm` is an optional dependency** (`[leaderboard]` extra, lazy-imported to
  keep the worker image small). Without it, `--agent native` is accepted and fails
  at the first model call with an `ImportError` recorded in the transcript — run
  `uv sync --all-extras` first.
