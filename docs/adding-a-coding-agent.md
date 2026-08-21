# Adding a coding agent (a CLI agent like claude-code or codex)

An agent is one **stateless adapter class** in `src/qualgentbench/adapters/`. The
harness owns the loop, the budget, the transcript file, and the kill semantics; the
adapter's job is to say how to launch the CLI and with what environment. Both arms
(bare-adb and MCP) come for free — the arm switch is `--mcp-server`, not adapter code.

## The contract

Subclass `AgentAdapter` (`adapters/base.py`) and implement:

- `name` — class attribute; must equal your registry key.
- `command(instruction, context) -> list[str]` — the argv. The instruction arrives on
  **stdin** (the base `run()` writes it and closes stdin), so the command must read
  its prompt from stdin, run non-interactively, and print a machine-readable
  transcript on stdout. Every "are you sure" prompt must be disabled by flags — a
  headless run that waits for a keypress hangs until the wall clock kills it.
- `env(context) -> dict[str, str]` — extra environment (API keys, per-run HOME).
- `prepare(context)` — optional; write config files into `context.run_dir` here.

The base `run()` gives you, without writing any code: stdout/stderr streamed to
`run_dir/agent/transcript.txt` in chunks (a timeout kill preserves partial output), a
watchdog that SIGTERMs the process when the step budget marks `run_dir/truncated`,
the wall clock from the task spec, and SIGTERM → 10s grace → SIGKILL so the agent can
close its MCP transport and release the device lock. Only override `run()` if your
agent is not a subprocess at all (see `native.py`).

`RunContext` fields you'll actually use: `model` / `force_model` (pass
`force_model or model` to the CLI — the harness pins it so a leaderboard row labelled
one model can't silently run another), `mcp_config_path` + `no_mcp` / `inject_mcp` /
`isolate_mcp` (how the device-tools MCP server reaches your agent — translate
`mcp_config_path`'s JSON into whatever your CLI consumes; codex renders it to TOML),
`disabled_tools` (base tool names; apply your CLI's prefix convention),
`tool_call_cap` (see the budget rule), `agent_env` (the harness routes the agent's
adb through the meter with `ANDROID_ADB_SERVER_PORT` — merge it, never drop it).

## The one rule that is enforced by a test

**Adding a coding agent must not mean adding a counter.** A step is one
*interaction* (tap, swipe, type, launch, observe...), counted by two harness-owned
proxies BELOW the agent — the ADB socket meter and the MCP meter — into one file,
`run_dir/interactions.json`. Your adapter enforces the budget by installing the
**shared** hook, never its own arithmetic:

```python
from ..interactions import BUDGET_HOOK
script.write_text(BUDGET_HOOK.format(
    count_file=hooks_dir / "count",
    meter_file=context.run_dir / "interactions.json",
    cap=context.tool_call_cap,
    sentinel=context.run_dir / "truncated",
))
```

...wired into whatever your CLI's pre-tool-call hook mechanism is (claude-code:
`--settings` with a PreToolUse hook; codex: `[[hooks.PreToolUse]]` in config.toml —
and only there: registering it twice double-charges). Then add your adapter to the
tuple in `tests/test_interactions.py::test_every_adapter_budgets_from_the_same_file`,
which asserts exactly this: the hook exists, it reads `interactions.json`, and it
does not count command strings. The hook fails closed — an unreadable meter stops
the episode rather than letting it run unmeasured.

## The transcript your agent must emit

Scoring, evidence bundles, cost, and the model column all come from parsing
`agent/transcript.txt`. The parser accepts two shapes; **emit Claude stream-json**
unless your CLI already has its own JSONL (codex's Responses-style output is also
parsed):

```json
{"type":"assistant","message":{"model":"...","content":[{"type":"tool_use","id":"t1","name":"mobile_tap","input":{...}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"..."}]}}
{"type":"result","usage":{"input_tokens":1,"output_tokens":2},"total_cost_usd":0.03}
```

Tool names matter: device work is recognised by the `mobile_*` names in
`transcript.py` (substring match, so `mcp__device__mobile_tap` counts). An MCP
server with differently-named tools will score zero device actions.

## Registration — four places, none derived from another

1. `adapters/__init__.py` → `REGISTRY["my-agent"] = MyAgentAdapter` (this also makes
   `--agent my-agent` a valid CLI choice).
2. `cli.py` → `AGENT_CLI["my-agent"] = "myagent"` — the binary `doctor` checks with
   `which` and the run refuses without; `None` if no external CLI is needed.
3. `cli.py` → `AGENT_MODELS["my-agent"] = [...]` — the default model list when
   `--models` is omitted (any `--models` value is passed to your adapter verbatim).
4. `tests/test_interactions.py` — the budget-hook test tuple, as above.

## Credentials

`.env` is loaded by a zero-dependency loader that never overrides existing env vars.
Read your provider key inside `env(context)` and document the key name in
`.env.example`. Two patterns worth copying: codex's key cannot be passed by env at
all, so its adapter seeds a per-run `CODEX_HOME` via `codex login --with-api-key` on
stdin and deletes `auth.json` on cleanup; claude-code's Fireworks routing shows how
to point an Anthropic-compatible CLI at another provider without touching the
`ANTHROPIC_API_KEY` path (which triggers an interactive prompt headless runs can't
answer). Isolate HOME/XDG dirs per run like codex does, so the operator's personal
agent config can't leak into a benchmark row.

## Checklist

```bash
uv run pytest tests/test_interactions.py -q        # budget-hook rule
uv run qualgent-bench doctor                       # your AGENT_CLI binary is found
uv run qualgent-bench run --agent my-agent --models <id> \
  --app birday --mode hunt --trials 1 --device emulator-5554
```

Then check the episode's `run_dir`: `interactions.json` should show non-zero
interactions (and `mcp_meter_bytes` in the MCP arm), `agent/transcript.txt` should
parse (the console prints tool-call and cost numbers), and `result.json` should carry
a verdict. An episode that "works" but records 0 interactions means your agent
bypassed the meters — its adb isn't going through `ANDROID_ADB_SERVER_PORT`, or its
MCP traffic isn't going through the proxied server URL.
