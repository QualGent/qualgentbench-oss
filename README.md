# QualGentBench

A benchmark that measures how well coding agents do real mobile QA.

Every app in the corpus is a real open-source Android app rebuilt with known defects
seeded into it. The agent gets a neutral release-sign-off brief, a device, and a step
budget — no source code, no hints. It explores the app and reports what it finds. Then
the harness **replays the agent's own reproduction steps** on the device: a defect the
agent claimed but cannot demonstrate earns half credit. The score you see is the
verified one, not the agent's self-report.

**What's in it:**

- **28 apps** across three tiers — easy (6), medium (10), hard (12)
- **129 seeded defects** and **129 working controls** (areas that work fine — reporting
  them broken costs points, so guessing doesn't pay)
- Ground truth is **derived, never asserted**: every area is measured against the clean
  and seeded builds before it may score anything

**Who can be tested:**

- **Coding agents**: `claude-code` and `codex-cli` work out of the box
- **Bare LLMs**: any model LiteLLM can reach (OpenAI, Anthropic, Fireworks, local
  models over Ollama) via the built-in native agent — no code needed
- Each agent runs in one of two arms: **bare** (it drives the device through `adb`
  itself) or **mcp** (you give it device tools from an MCP server). Same board, two
  rows — so the benchmark also measures what your tooling is worth.

**What you get back:** a leaderboard-style score per agent + model + arm, and a
complete, tamper-evident audit trail per episode — every step, every screenshot, every
claim and why it verified or didn't.

```
#  Agent + Model        n   F1   FP   Avg/Step  Avg/Token  Overall
1  codex-cli · gpt-5.5  1  0.67   0%        76  7,029,106    49.7%
```

## Running it (Docker)

The benchmark ships as one Docker image carrying everything except the emulator: the
harness, an adb client, the agent CLIs, and every benchmark APK (sha256-verified at
build time). You write one config file and run one command; the launcher checks
everything up front, boots your emulators, runs the episodes, and shuts the emulators
down after.

### 1. Install the prerequisites

**Docker**, running — [Docker Desktop](https://docs.docker.com/get-docker/) on
macOS/Windows, Docker Engine on Linux. `docker info` must succeed.

**The Android emulator and `adb`** — install
[Android Studio](https://developer.android.com/studio), then in *Device Manager*
create one virtual device per parallel lane you want (any recent Pixel image works):

```bash
emulator -list-avds     # e.g. Pixel_8_A  Pixel_8_B
```

The launcher finds `emulator` and `adb` on your PATH or in the standard SDK locations.
Budget ~2 GB RAM and 2 CPU cores per emulator; the launcher refuses a config your
machine can't run. Don't leave the same AVD open in Android Studio — the launcher
boots its own headless copies.

**[uv](https://docs.astral.sh/uv/getting-started/installation/)** on the host — it
runs the launcher (`uv run`) and brings its own Python; nothing else to set up.

**Credentials for the agent you're testing**, in a `.env` file:

- **codex-cli**: run `codex login` once on your machine. The launcher mounts that
  login read-only; your session is never written to. (`OPENAI_API_KEY` only if you
  want to bill a platform model your Codex plan doesn't offer.)
- **claude-code**: `CLAUDE_CODE_OAUTH_TOKEN` (mint once with `claude setup-token`) or
  `ANTHROPIC_API_KEY` in `.env`. An interactive `claude` login is not enough — every
  episode runs in a private config dir.
- **Fireworks-hosted models**: `FIREWORKS_API_KEY`.

Keys live only in `.env`. They never go into the image, the config, or the results.

### 2. Build and configure

```bash
git clone <this repo> && cd qualgentbench
cp .env.example .env                              # fill in credentials
docker build -t qualgentbench:local .             # once, ~10 min: bakes the APKs in
cp bench.config.example.yaml bench.config.yaml
```

A minimal `bench.config.yaml`:

```yaml
image: qualgentbench:local
agent: codex-cli
model: gpt-5.5
scope:
  tiers: [easy]          # or apps: [birday, easynotes]; tiers: [easy, medium, hard] = all 28
  mode: hunt
  trials: 1
devices:
  avds: [Pixel_8_A, Pixel_8_B]   # one lane per AVD
env_file: .env
```

### 3. Run

```bash
uv run scripts/launch.py bench.config.yaml
```

What happens, in order:

1. **Preflight.** Every value in the config and everything on your machine is checked
   before anything boots. All problems print at once, each with its fix.
2. **The plan and an ETA**, then `Continue? [Y/n]` (`--yes` skips it).
3. **Your AVDs boot headless**, one lane each, pulling episodes from one shared queue.
4. **Episodes run and verify** — a live table shows each lane's phase, steps and time.
   After each agent finishes, its claimed reproductions are replayed on the same
   device before anything is scored.
5. **Teardown and the board.** Emulators stop (`--keep-emulators` to keep them),
   results land in `runs/` on your machine, the board prints.

Expect **10–30 minutes per episode**; the
verification replay is often as long as the agent's own session.

Other launcher flags: `--image TAG` overrides the config's image, `--pull` refreshes a
registry image first.

### Giving the agent device tools (the MCP arm)

Run any MCP server on your machine and name it in the config as *you* reach it; the
launcher rewrites the address for the container:

```yaml
mcp_server: http://127.0.0.1:51899
```

Present means device tools, absent means bare — that line is the only arm switch, and
the harness never starts a server. It must be a standalone server any client can call
(preflight refuses the DevLoop desktop app's bridge, which holds per-session device
locks). Optionally withhold specific tools with `QGB_DISALLOWED_TOOLS` in `.env`
(comma-separated; unset withholds nothing).

### Isolation

Inside the image the agent runs as an unprivileged user and the harness tree — code,
specs, answer key — is root-only: an agent that goes looking gets *Permission denied*
from the kernel, with a contamination scanner as backstop. The container reaches your
emulators through the host's adb server and nothing else of yours.

## Journey mode (test-case runs)

Hunt mode hands the agent every feature area. Journey mode hands it **one test case**
(name, steps, expected outcome) on a build with seeded defects, and runs every case
twice: clean (no defect) and seeded (the case's own bugs). Two numbers come out,
never blended: **completion**, verified on the device after the agent exits, and
**bug finding** (found / present, false reports, one F1). Eight apps × five cases × two
versions = 80 episodes.

```bash
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app openscale,moneymanagerex,tasksorg,medtimer,orgzly,ankidroid,fossify-calendar,fossify-contacts \
  --mode journey --devices emulator-5554,emulator-5556,emulator-5558
uv run qualgent-bench show --agent codex-cli --mode journey --run <run_id>
```

In Docker, set `mode: journey` in `bench.config.yaml`; the image carries the journey
builds.

## Reading the results

One folder per app (`runs/explore-<app>/`), one folder per episode inside it:

```text
runs/explore-birday/2026-08-20T17-28-50Z_explore-birday_codex-cli_gpt-5.5_raw_trial-1/
  result.json          the authoritative record: verdict, every score and metric
  replay.json          per-claim verification: confirmed / unreplayable / ... and WHY
  workspace/
    findings.yaml      the agent's own report — verdicts, reproductions, expectations
  agent/
    transcript.txt     the agent's raw session, every tool call and result
  instruction_sent.md  the exact brief the agent was given
  interactions.json    the enforced step count, broken down by kind (tap/type/observe…)
  app_snapshot.tar     the app's data exactly as the agent first saw it
  evidence/
    index.html         ← start here: walk the episode step by step in a browser
    screens/ frames/   per-step screenshots
    steps.jsonl        one line per agent action, linked to its screenshot
    manifest.json      sha256 of every file — the bundle is tamper-evident
```

Where to look for what:

- **"What did the agent score and why?"** → `result.json` (`metrics` block).
- **"What did the agent actually do?"** → `evidence/index.html`.
- **"Why did a claim fail verification?"** → `replay.json`. Each claim shows its
  classification, the step that stopped, the executor's judgment calls, and the
  environment it replayed in. A verdict should never be a mystery.
- **"Was the agent honestly measured?"** → `interactions.json` (the budget is enforced
  from this one file, in both arms).
- **"What did the whole run look like?"** → `runs/_runs/<run id>/`: the plan you
  approved, every scheduling event, and `board.json` — the printed board as data,
  ready to plot or compare across runs.

Everything is recomputable from artifacts: `scripts/replay_findings.py <run_dir>`
re-verifies an episode at zero token cost, `scripts/score_replay.py` re-scores it.

Episodes that never became a QA result — killed before reporting, never reached the
device, or read the answer key — are excluded, not averaged in as zeros. Every spec
carries a canary token; if it surfaces in a transcript, the episode is void.

## Repo layout

```text
src/qualgentbench/
  cli.py                 doctor / preflight / run / show
  episode_runner.py      the engine: stage the device, run one episode, collect evidence
  lanes.py, scheduler.py N devices, one queue: lanes, estimates, backoff, ETA
  config.py, preflight.py   bench.config.yaml and "is it runnable?"
  bugs.py                task builders + scorers
  truth.py, replay.py    derived truth and differential replay
  verify/                device oracles, spec matching
  interactions.py        the step unit; adb_meter.py / mcp_meter.py are the counters
  adapters/              claude_code, codex_cli, native
  data/benchmarks/       one YAML per app: defects, controls, probes
scripts/                 build, derive, gate, replay, validate; launch.py; bake_apks.py
Dockerfile               harness + adb client + agent CLIs + APKs; no emulator inside
bench.config.example.yaml  a run as a file
```

## Reference docs

- [docs/scoring.md](docs/scoring.md) — every formula and constant behind the board.
- [docs/architecture.md](docs/architecture.md) — how an episode, the step meters, and
  replay verification fit together, with diagrams.
- [docs/design.html](docs/design.html) — the full design narrative, including an
  **interactive scoring explorer**: drag finds, false reports and steps and watch the
  score move. Open it locally in a browser.
- [docs/adding-an-app.md](docs/adding-an-app.md) — spec YAML, seeded patches, derived
  truth, gates.
- [docs/adding-a-coding-agent.md](docs/adding-a-coding-agent.md) — the adapter
  contract and the one-counter budget rule.
- [docs/adding-a-native-model.md](docs/adding-a-native-model.md) — bare LLMs via
  LiteLLM, including local models: `--agent native --models ollama_chat/llama3.1`.

Working on the benchmark itself? `uv sync`, then `uv run qualgent-bench doctor` — and
before quoting any number, run the gates: `scripts/check_tier_ready.py`,
`scripts/adversary_check.py`, `scripts/validate_bundle.py`.
