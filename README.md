# QualGentBench

A seeded-bug benchmark for coding agents on mobile QA.

Each app is a real open-source Android app rebuilt with known defects patched in. An
agent is given a neutral release-sign-off brief, no source code, and a step budget. It
explores the app on a device and reports what it finds in `findings.yaml`. The harness
then **replays its reproductions** to check the defects it claimed are demonstrable.

Sixteen apps across two tiers, 64 seeded defects and 71 working controls.

## Quick start (Docker)

The benchmark ships as a Docker image that carries everything except the emulator:
the harness, an adb client, the `claude` and `codex` CLIs, and every benchmark APK
(sha256-verified at build time). You write one config file and run one command; the
launcher checks everything, boots your emulators, runs the episodes, and shuts the
emulators down after. Emulators stay on your machine because Docker Desktop has no
KVM on macOS or Windows — and because that keeps the image identical everywhere.

### Before you start — what your machine needs

**1. Docker**, running. [Docker Desktop](https://docs.docker.com/get-docker/) on
macOS/Windows, Docker Engine on Linux. `docker info` must succeed.

**2. The Android emulator and `adb`** — from
[Android Studio](https://developer.android.com/studio) (the SDK's *Android Emulator*
and *Platform-Tools* components). Then create one virtual device per lane you want in
*Device Manager* (any recent Pixel image works) and note their names:

```bash
emulator -list-avds     # e.g. Pixel_8_A  Pixel_8_B
```

The launcher finds `emulator` and `adb` on your PATH or under the standard SDK
locations (`ANDROID_HOME`, `ANDROID_SDK_ROOT`, `~/Library/Android/sdk`,
`~/Android/Sdk`, `%LOCALAPPDATA%\Android\Sdk`). Budget about **2 GB RAM and 2 CPU
cores per emulator**; the launcher refuses a config your machine can't run and warns
when it's tight. Three lanes on a 10-core / 24 GB laptop is comfortable.

Don't leave an instance of the same AVD running in Android Studio when you launch —
the launcher boots its own copies headless, and two instances of one AVD corrupt its
data.

**3. Python 3** on the host — only for `scripts/launch.py`, which uses the standard
library alone. No `uv`, no virtualenv, no repo dependencies on the host.

**4. Credentials for the agent you want to benchmark**, in a `.env` file:

- **codex-cli**: run `codex login` once on your machine (install
  [`codex`](https://github.com/openai/codex) if needed). The launcher mounts that
  login read-only, and each episode gets its own copy — your session is never written
  to. Set `OPENAI_API_KEY` *only* to bill an OpenAI-platform model your Codex plan
  doesn't offer; when it's set, it takes precedence over the login.
- **claude-code**: put `CLAUDE_CODE_OAUTH_TOKEN` (mint it once with
  `claude setup-token` — needs [`claude`](https://claude.com/claude-code) installed
  locally) or `ANTHROPIC_API_KEY` in `.env`. An interactive `claude` login is not
  enough: every episode runs in a private config dir with no login of its own.
- **Fireworks-hosted models** (through claude-code): `FIREWORKS_API_KEY`.

Keys and tokens live only in `.env`. They never go into the image, the config file,
or the results.

### Run it

```bash
git clone <this repo> && cd qualgentbench
cp .env.example .env                              # fill in the credentials above
docker build -t qualgentbench:local .             # once, ~10 min: bakes the APKs in
cp bench.config.example.yaml bench.config.yaml    # set agent, model, scope, your AVDs
python3 scripts/launch.py bench.config.yaml
```

A minimal `bench.config.yaml`:

```yaml
image: qualgentbench:local
agent: codex-cli
model: gpt-5.5
scope:
  tiers: [easy]          # or apps: [birday, easynotes]; tiers: [easy, medium] = all 16
  mode: hunt
  trials: 1
devices:
  avds: [Pixel_8_A, Pixel_8_B]   # one lane per AVD
env_file: .env
```

What happens next, in order:

1. **Preflight, before anything boots.** The image validates every value in the
   config — agent CLI present, credentials, tiers ready, app ids known, APKs baked,
   MCP server reachable if set — and the host side checks Docker, `emulator`, `adb`,
   your AVD names, RAM/CPU, and the runs directory. Every problem is printed at once
   with its fix; nothing starts until all are green.
2. **The plan and an ETA** — episodes, devices, estimated wall time (from your own
   earlier runs, or the step budgets the first time) — then `Continue? [Y/n]`.
   `--yes` skips the prompt for scripted use.
3. **Your AVDs boot headless** on free ports, one lane each. Every (app, trial) is a
   unit in one longest-first queue; each lane pulls the next unit, staying on the app
   it already has installed when it can, so lanes finish within minutes of each other.
4. **Episodes run and verify.** A live table shows each lane's phase (staging → agent
   → verifying), step count and elapsed time; if you pipe the output you get one
   timestamped line per event instead. After each agent finishes, its claimed
   reproductions are replayed on the same device before anything is scored.
5. **Teardown.** The emulators the launcher booted are stopped (`--keep-emulators`
   leaves them up). Results are in `runs/` on your machine — see
   [Reading the runs directory](#reading-the-runs-directory) — and the board prints at
   the end. Re-print it anytime:

```bash
uv run qualgent-bench show --agent codex-cli --mode hunt --run <run id>
```

(`show` needs the repo's Python environment — `uv sync` once — or run it inside the
image: `docker run --rm -v "$PWD/runs:/work/runs" qualgentbench:local show
--runs-dir /work/runs --agent codex-cli --mode hunt`.)

Expect **10–30 minutes and a few dollars of model usage per episode**; the
verification replay is often as long as the agent's own session.

### Giving the agent device tools (the MCP arm)

By default the agent drives the device through `adb` itself — the **bare** arm. To
benchmark the agent *with* device tools, run any MCP server on your machine and name
it in the config as your machine reaches it; the launcher rewrites the address for
the container by itself:

```yaml
mcp_server: http://127.0.0.1:51899
```

Present means device tools, absent means bare — that line is the only arm switch, and
the harness never starts a server. The server must be a standalone one that any
client can call; the DevLoop desktop app's bridge holds device locks per session and
preflight refuses it. Optionally withhold tools with `QGB_DISALLOWED_TOOLS` in `.env`
(comma-separated names; unset withholds nothing).

The two arms score into separate rows of the same board (`raw` vs `mcp`), so the
comparison is one `show` away once both have run.

### What the container can and can't see

Inside the image the agent runs as an unprivileged user, the harness tree is
root-only, and episode workspaces live outside it. An agent that goes looking for the
benchmark's own code or answer key gets *Permission denied* from the kernel; the
contamination scanner is only the backstop. The container reaches your emulators
through the host's adb server (`host.docker.internal` on macOS/Windows, the host
network on Linux — the launcher handles both) and reaches nothing else of yours.

### Launcher options

```text
python3 scripts/launch.py CONFIG [--yes] [--keep-emulators] [--image TAG] [--pull]
```

`--image` overrides the config's `image:`; `--pull` refreshes a registry image before
running. Ports, network mode and mount paths are chosen per OS automatically.

## Running without Docker

For developing the benchmark itself, or benchmarking a native model over LiteLLM, run
the harness directly. You need everything above except Docker, plus
[uv](https://docs.astral.sh/uv/getting-started/installation/) and the agent CLI
installed locally. The emulator must already be running and visible to `adb`.

```bash
uv sync && cp .env.example .env
uv run qualgent-bench doctor                    # every line green, or it tells you the fix

uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app birday --mode hunt --trials 1 --device emulator-5554
```

`--tier easy`, `--tier medium` or `--tier easy,medium` replaces `--app`. Hand `run`
several devices to run lanes in parallel — `--devices emulator-5554,emulator-5556`,
or `--devices auto` for every connected device (`--lanes N` caps it) — and it behaves
exactly like the Docker launcher: the plan, the ETA, the `Continue?` prompt, the live
lane table. The same config file works here too:

```bash
uv run qualgent-bench preflight bench.config.yaml --plan   # every check + the ETA; boots nothing
uv run qualgent-bench run --config bench.config.yaml --devices emulator-5554
```

If `adb` is not found: Android Studio installs it but does not add it to your PATH.
Add the platform-tools directory to PATH, or set `QGB_ADB_PATH=/path/to/adb` in `.env`.

One caveat that applies to native runs only: nothing stops the agent from reading
this repository, since it runs as you. The contamination tripwire voids episodes
that touch it, but quotable boards should come from the Docker path.

## What you get

```
#  Agent + Model        n   F1   FP   Avg/Step  Avg/Token  Overall
1  codex-cli · gpt-5.5  1  0.67   0%        76  7,029,106    49.7%
```

`Overall = weighted recall × speed − false-report cost`. The score is the **verified**
one: a defect the agent claimed but could not demonstrate on replay earns half credit,
not full. Every formula and constant is in [docs/scoring.md](docs/scoring.md); how the
whole pipeline fits together is in [docs/architecture.md](docs/architecture.md). For
the full design narrative — including an **interactive scoring explorer** where you
can drag finds/false-reports/steps and watch the score move — open
[docs/design.html](docs/design.html) locally in a browser (GitHub can't run it
inline; a hosted GitHub Pages link is planned).

## Reading the runs directory

Every episode leaves a complete audit trail. One folder per app
(`runs/explore-<app>/`), one folder per episode inside it, named
`<timestamp>_<task>_<agent>_<model>_<arm>_trial-N`:

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
  snapshot_meta.json   how the snapshot was taken ("cold" = app stopped first)
  evidence/
    index.html         ← start here: walk the episode step by step in a browser
    screens/ frames/   per-step screenshots
    steps.jsonl        one line per agent action, linked to its screenshot
    manifest.json      sha256 of every file — the bundle is tamper-evident
```

Where to look for what:

- **"What did the agent score and why?"** → `result.json` (`metrics` block).
- **"What did the agent actually do?"** → `evidence/index.html`.
- **"Why did a claim fail verification?"** → `replay.json`. Each claim shows the
  classification, which step stopped, and the executor's own judgment calls —
  `ambiguous_steps`/`choices` (same-label elements it had to pick between),
  `reissued_steps` (a dropped tap re-issued), `dismissed` (overlays it auto-tapped),
  `back_noop_steps` (keyboard-closing back presses it skipped) — plus environment
  provenance (`u2_available`, `dump_stats`, `snapshot_mode`). A verdict should never
  be a mystery: if it is not explained there, that is a bug worth filing.
- **"Was the agent honestly measured?"** → `interactions.json` (the budget is
  enforced from this file; in the MCP arm `mcp_meter_bytes` proves traffic flowed).
- **"What did this whole run look like?"** → `runs/_runs/<run id>/`: `plan.json`
  (the episodes and ETA you approved), `schedule.jsonl` (every start, finish, requeue
  and hold, with lane and device), and `board.json` — every episode's verified
  numbers plus a `summary` block that is the printed board as plain key-value rows
  (one per agent + model + arm), ready to plot or compare across runs.

Everything is recomputable from artifacts: `scripts/replay_findings.py <run_dir>`
re-verifies an episode against an improved replayer at zero token cost, and
`scripts/score_replay.py` re-scores it.

## Before quoting a number

```bash
uv run python scripts/check_tier_ready.py --tier easy   # must print READY
uv run python scripts/adversary_check.py                # guessing must score <= 0
uv run python scripts/validate_bundle.py runs/<task>/<run>
```

An episode killed before reporting (`env_failure`), one that never reached the device
(`infra_failure`), or one that read the answer key (`contaminated`) is not a QA result.
Those are excluded rather than averaged in as zeros.

Every spec carries a canary comment; if it appears in a transcript the episode is void.
`contamination.py` classifies every host path an episode touched.

## Configuration

Everything lives in `.env` — see `.env.example`. The two that change behaviour:

- `QGB_MCP_SERVER` — same as `--mcp-server`
- `QGB_DISALLOWED_TOOLS` — comma-separated tools withheld from the agent. **Unset or
  empty withholds nothing**; there is no default, so two people with different `.env`
  files run different episodes.

## Extending the benchmark

Start with the two reference docs — [architecture.md](docs/architecture.md) (how an
episode, the meters, and replay verification fit together, with diagrams) and
[scoring.md](docs/scoring.md) (every formula) — then the three guides:

- [Adding an app + tasks](docs/adding-an-app.md) — spec YAML, seeded patches,
  derived truth, serving APKs, gates. The core rule: a feature's `state` is
  **derived**, never asserted — `derive_truth.py` measures what actually differs
  between the clean and seeded builds.
- [Adding a coding agent](docs/adding-a-coding-agent.md) — the adapter contract,
  the one-counter budget rule, transcript format, registration.
- [Adding a native model](docs/adding-a-native-model.md) — bare LLMs via LiteLLM,
  including local models over Ollama or any OpenAI-compatible server. Any LiteLLM
  model id works with zero code: `--agent native --models ollama_chat/llama3.1`.

## Repo layout

```text
src/qualgentbench/
  cli.py                 doctor / preflight / run / show
  episode_runner.py      the engine: stage the device, run one episode, collect evidence
  lanes.py, scheduler.py N devices, one queue: lanes, estimates, backoff, ETA
  progress.py            live lane table on a terminal, log lines when piped
  config.py, preflight.py   bench.config.yaml and "is it runnable?"
  bugs.py                task builders + scorers
  truth.py, replay.py    derived truth and differential replay
  verify/                device oracles, spec matching
  transcript.py          agent transcript parsing
  interactions.py        the step unit; adb_meter.py / mcp_meter.py are the counters
  episode_evidence.py    evidence bundles  (+ evidence_report, evidence_manifest)
  adapters/              claude_code, codex_cli, native
  data/benchmarks/       one YAML per app: defects, controls, probes
scripts/                 build, derive, gate, replay, validate; launch.py (Docker), bake_apks.py
Dockerfile               harness + adb client + agent CLIs + APKs; no emulator inside
bench.config.example.yaml  a run as a file
```

## Roadmap

- **Hard tier.** The 12 hardest apps — transactions, recurrence, media libraries,
  vaults — with 5 seeded defects each. The APKs already build; the defects still
  need seeding, verifying and gating, the same pipeline every shipped app went
  through. This is what takes the corpus from 16 apps to the full 28.
- **Emulators in containers.** On a Linux host with KVM, `docker compose --scale
  emulator=N` instead of host AVDs — the same lanes, no Android SDK on the machine.
  The launcher and the image are built so only the device plane changes.
