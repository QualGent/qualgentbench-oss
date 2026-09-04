# QualGentBench

Seeded-bug benchmark for coding agents on mobile QA. The CLI is four commands:
`doctor`, `preflight`, `run`, `show`. See README.md.

All three tiers are hunt-ready and gate-green: easy (6 apps), medium (10) and hard
(12) — 265 scored areas, 129 seeded defects, 129 working controls. Hard-tier apps
carry conditional defects and `hidden: true` areas reported via `other…`. Every
tier keeps the uniform step_budget 500 by decision (no per-app budget derivation).
APKs download from HuggingFace on first use (`apk:` block in each spec: repo,
filename, sha256).

Spec-authoring rules that have caught real bugs: a debug build with a SECOND
launcher (LeakCanary) makes launches nondeterministic; a Compose control can be
INVISIBLE to accessibility; `db:` accepts an absolute shell-readable path for
external-storage databases (harness-only — the agent path rejects oracle
expectations); an unstable check leaves the corpus rather than being asserted; a
CONTROL on the same screen as a hidden defect must be `collateral` or right agents
get charged, and control wordings must not contain defect-adjacent clauses; in a
KMP app the flag shim lives in the jvm-shared source set, never commonMain.

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
however many calls it took. One adb command = one step: a chained shell request
(`input tap … && uiautomator dump`) costs every interaction in it, and exec-out's
quoted arguments (`uiautomator 'dump'`) are normalised first (2026-09-02; before that a
batched request cost one and six exec-out dumps in one episode read as `other`).
Counted at two harness-owned proxies BELOW the agent:
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

## Parallel runs, config files, Docker

One `run` = one agent + one model.

- `--devices a,b,c` (or `auto`) runs episodes over N emulators: every (app, kind,
  trial) is a unit in one longest-first queue with app affinity (`scheduler.py`);
  each device is a lane pulling from it (`lanes.py`). Never split by model/arm.
- `run --config bench.config.yaml` takes agent/model/scope/devices from a file
  (`config.py`); `preflight CONFIG --plan` checks every value and prints the ETA
  without booting anything (`preflight.py`); `run` prints the same plan and asks
  `Continue?` unless `--yes`.
- Output (`progress.py`): a live lane table on a TTY (with a phase column —
  staging/agent/verifying — and a "no steps for Xm" stall flag after 3 quiet
  minutes); one timestamped line per event when piped (`docker logs`, CI),
  heartbeat per busy lane each minute.
- Agent lifecycle (`adapters/base.py`): the run ends on process EXIT, not stdout
  EOF — a child the agent backgrounds (`adb root`, logcat) inherits the pipe and
  once held a lane 35 min after the agent died. `Process.wait()` has the same trap
  (its future waits for pipes), so exit is detected by polling `returncode`; the
  agent runs as its own session leader and the group is SIGKILLed after it.
- Provenance: every `result.json` carries `run_id` + `provenance` (device, lane,
  lanes, attempt, adb server, image digest). `show --run <id>` scopes a board;
  without it every run in `runs/` is blended. `runs/_runs/<run_id>/` holds
  `plan.json`, `schedule.jsonl`, `board.json` — whose `summary` block is the
  printed Bug-hunt table as data (one row per agent+model+condition, from
  `leaderboard.hunt_summary`, which the table itself renders — no drift), for
  later cross-run comparison/plotting.
- Isolation: claude-code gets a per-run `CLAUDE_CONFIG_DIR` (like codex's `CODEX_HOME`).
  Consequence: the interactive `claude` login is NOT visible to it (macOS keeps a
  Keychain item per config dir; Linux's credentials file carries a rotating refresh
  token that N copies would race). claude-code auth is therefore `CLAUDE_CODE_OAUTH_TOKEN`
  (`claude setup-token`) or `ANTHROPIC_API_KEY` in `.env` — everywhere, not just Docker.
  `run`'s preflight refuses without one (first real run failed "Not logged in").
- Rate limits: `metrics.failure_class = "rate_limited"` (`failures.py`) is excluded
  like `infra_failure`; the scheduler holds ALL lanes with exponential backoff,
  requeues the unit as a fresh episode (max 4), and parks lanes if it persists.
- Docker: the image (`Dockerfile`) holds the harness, adb client, claude/codex and
  the APKs (`scripts/bake_apks.py`); emulators and the MCP server stay on the host.
  In the image, answer-key isolation is kernel-enforced: agents run as the
  unprivileged `agent` user (`QGB_AGENT_USER`), `/app` is root-only, runs live at
  `/work/runs` outside the repo. The contamination scanner is the backstop there
  and the only guard on native host runs.
  `scripts/launch.py` (stdlib only) asks the image to validate the config
  (`preflight --json`), checks the host, boots the AVDs, runs, tears down. adb is
  reached through `ANDROID_ADB_SERVER_ADDRESS` (adb) + `ANDROID_ADB_SERVER_HOST`
  (adbutils/u2); the agent is pinned back to the loopback meter.
- Before publishing a parallel board: `--lanes 1` vs `--lanes N` on one tier; step
  counts must agree (Overall uses steps, not time; contention can still add observes).

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

## Journey mode (test-case runs)

`--mode journey`: one episode = one app + ONE test case + one VERSION (clean = no
defect on; seeded = exactly the case's `bugs:` on). Same brief in both. Two numbers,
never blended: COMPLETION (device oracle after the agent exits + the right verdict; a
blocked case = fail + the blocking bug named; truncation/no evidence = not completed)
and BUG FINDING (found/present over seeded episodes, false reports over all — every
report on a clean build is false — one F1 from the totals). Cases live in
`data/test-cases/<app>.yaml`: defects (kind functional|display, marker, symptoms) and
per case route + `check:` oracle + `bugs:` (≤1 functional). `scripts/derive_journey.py`
is the corpus gate (clean + seeded pass per case; display markers must be in the
screen diff). `scripts/rescore_journey.py` re-scores saved episodes. The device
timezone is pinned by `run_device_setup` (`QGB_DEVICE_TIMEZONE`, default
America/Chicago). A journey-only defect is a `bugs:` + `tasks:` entry in the spec with
NO exploration feature, so hunt mode never activates it. Journey mode fetches the JOURNEY
build — the test-case file's `apk:` block (`journey/<app>-buggy.apk` on HF, cache slot
`journey/`); dist/ still wins locally. Upload dist/<app>/buggy.apk there after each
rebuild and update that block's sha256/size. `db:` oracles are read after `am
force-stop` (a running AnkiDroid locks its collection); the launcher activity comes from
the package's launcher list with debug tools (LeakCanary) skipped.

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
src/qualgentbench/cli.py               doctor / preflight / run / show; scores each episode
src/qualgentbench/episode_runner.py    the engine (one episode end to end)
src/qualgentbench/lanes.py             N devices, one queue: the lane body
src/qualgentbench/scheduler.py         units, estimates, LPT queue, backoff, ETA simulation
src/qualgentbench/progress.py          live lane table / plain log lines
src/qualgentbench/config.py            bench.config.yaml schema
src/qualgentbench/preflight.py         is this config runnable? (checks + plan)
src/qualgentbench/failures.py          rate_limited classification; the shared exclusion predicate
src/qualgentbench/bugs.py              task builders + scorers
src/qualgentbench/adapters/            claude_code, codex_cli, native
src/qualgentbench/episode_evidence.py  per-episode audit bundle
src/qualgentbench/evidence_manifest.py sha256 manifest + step chain; verify_bundle()
runs/<task>/<run>/evidence/            index.html, manifest.json, steps.jsonl,
                                       screens/, frames/, findings.json, meta.json
dist/<app>/buggy.apk                   locally built APKs (gitignored; else from HF)
```
