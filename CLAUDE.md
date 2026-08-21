# QualGentBench

Seeded-bug benchmark for coding agents on mobile QA. The CLI is three commands:
`doctor`, `run`, `show`. See README.md.

Easy (6 apps) and medium (10 apps) are hunt-ready and gate-green — 135 scored areas,
64 seeded defects, 71 working controls. Hard is built but not seeded and stays blocked.
APKs download from HuggingFace on first use.

This repo was pruned to the seeded-bug benchmark alone during 2026-08-17..19 —
TrustLoop, CreateBench, the customer track, the legacy `tasks/` layer, the two-arm
board and all DevLoop naming are gone. Reference docs live in `docs/`
(architecture.md, scoring.md, the three extension guides, design.html).

## Two rules that have caught real bugs

**`ruff --select F821` is the check that catches a bad deletion.** `compileall` and a
green test suite both miss undefined names on cold paths. `verify/device_oracle.py` is
imported *inside functions* (`bugs.py:1184,1464`), so module-level reachability scans
report it dead when it is not.

**A step is one INTERACTION** — tap, swipe, type, press, launch, terminate, observe.
Typing is one `type` whatever the string's length; reading the screen is one `observe`
however many calls it took. Counted at two harness-owned proxies BELOW the agent:
`adb_meter.py` on the ADB server socket (the bare arm) and `mcp_meter.py` in front of
the MCP server. Both write `interactions.json`, and every adapter budgets from that one
file via `BUDGET_HOOK`. **Adding a coding agent must not mean adding a counter** —
`test_every_adapter_budgets_from_the_same_file` enforces this.

Counting adb requests was tried and rejected: `mobile_type_text` costs 14 adb ops for
three characters while `mobile_launch_app` costs 0. That measured transport, not QA.

## Setup

```bash
uv sync && cp .env.example .env && uv run qualgent-bench doctor
```

An Android device/emulator must be on `adb`.

## Running

```bash
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app birday,easynotes --mode hunt --trials 2 --device emulator-5554

# with device tools from any MCP server you run yourself:
uv run qualgent-bench run ... --mcp-server http://127.0.0.1:51821
```

`--mcp-server` is the only arm switch: present = MCP device tools, absent = bare agent
driving adb. The harness never starts a server.

Do NOT point it at the DevLoop desktop app: it requires `qg_acquire_device` before any
device tool and holds a session-scoped lock that outlives a stopped agent. `doctor`
detects it and refuses.

`--tier` is comma-separated (`easy,medium` = 16 apps). An unready tier anywhere in the
list is refused rather than half-run. Omitting `--tier` runs every registered app
including unready ones, with only a warning.

## Scoring

States in a spec's `exploration.features`: `broken` (a seeded defect, earns recall),
`ok` (a control, a wrong report costs 0.25), `collateral` (an area a defect also breaks
— earns nothing, costs nothing). opencalc's `multiply-wrong` patches the shared
expression parser, so `(2+3) × 4` really does give 9; scoring those areas as controls
charged an agent 0.75 for measuring reality. Check with `scripts/check_controls.py`.

Seeded patches must anchor uniquely — `build_app.py` refuses an ambiguous `find` block.
catima's edit defect once matched `insertLoyaltyCard` before `updateLoyaltyCard` and
silently broke a control.

Feature states are **derived, never asserted**: `derive_truth.py` runs each check
against the clean and seeded builds. `check:` says how to exercise an area, not whether
it works.

Gate before quoting any number:

```bash
uv run python scripts/check_tier_ready.py --tier easy   # must print READY
uv run python scripts/adversary_check.py                # guessing must score <= 0
uv run python scripts/validate_bundle.py runs/<task>/<run>
```

`env_failure`, `infra_failure` and `contaminated` episodes are excluded, not averaged
in as zeros. Every spec carries a canary; if it surfaces in a transcript the episode is
void.

Agents report through `findings.yaml` (`submission.py`) — the same contract in both
arms. The file is also read off disk at episode end, because an agent that appends with
`Edit` writes fragments that do not parse standalone.

The score printed is the **verified** one: `_verify_episode` replays each reproduction
and writes `metrics["hybrid"]` back into `result.json`. A claimed defect that cannot be
demonstrated earns half credit.

Replay-start equals agent-start **by construction**: staging snapshots the app COLD
(relaunch → settle → force-stop → tar → relaunch for the agent), and `_reset` re-runs
`device_setup` + isolation before every pass. Anchor ties break toward the smallest
clickable container; an inconclusive pass retries the OTHER candidate of an
ambiguous anchor, never the identical tap; and a gesture whose next anchor is
missing while the screen still shows the gesture's own anchor untouched is
re-issued exactly once (Android drops touches during relayout). `replay.json` records provenance —
`u2_available`, `dump_stats`, `snapshot_mode`, per-pass `ambiguous_steps`/`choices`/
`dismissed` — so a degraded replay is visible instead of scored as agent failure.
uiautomator2 is a hard dependency; `doctor` refuses without it (silent absence once
cost an episode 8/8 claims). Hierarchy dumps drop systemui AND the active IME's
windows — keyboard chrome carries its own clickable "Back" and can echo typed text
into a `present` oracle.

## Tool surface

Neither agent shapes tools by default. `QGB_DISALLOWED_TOOLS` (comma-separated) is the
only source; unset or empty withholds nothing. It reaches MCP tools only — for
claude-code every name is prefixed `mcp__device__`, for codex it lands in the
per-server `disabled_tools`.

`tests/conftest.py` strips `QGB_*` before every test; without it the suite asserts
against whatever the developer's `.env` happens to contain.

**Budgets are NOT re-derived for the current step unit.** Every `step_budget` was sized
against an older counter, and the unit changed again on 2026-08-19 (~1.2-1.6x looser
now, agent-dependently). Re-derive with `scripts/derive_budgets.py` before quoting a
score that depends on speed or truncation.

## Repo layout

```text
src/qualgentbench/cli.py               doctor / run / show; scores each episode
src/qualgentbench/episode_runner.py    the engine (one episode end to end)
src/qualgentbench/bugs.py              task builders + scorers
src/qualgentbench/adapters/            claude_code, codex_cli, native
src/qualgentbench/episode_evidence.py  per-episode audit bundle
src/qualgentbench/evidence_manifest.py sha256 manifest + step chain; verify_bundle()
runs/<task>/<run>/evidence/            index.html, manifest.json, steps.jsonl,
                                       screens/, frames/, findings.json, meta.json
dist/<app>/buggy.apk                   locally built APKs (gitignored; else from HF)
```
