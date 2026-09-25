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
2. **The plan and an ETA**, then `Continue? [Y/n]` (`--yes` skips it). With no
   terminal to ask on (CI, a pipe, `< /dev/null`), both the launcher and a bare
   `qualgent-bench run` refuse to start without `--yes`, whatever is piped in (the
   plan still prints). `qualgent-bench preflight CONFIG --plan` previews the plan
   without booting anything.
3. **Your AVDs boot headless**, one lane each, pulling episodes from one shared queue.
4. **Episodes run and verify** — a live table shows each lane's phase, steps and time.
   After each agent finishes, its claimed reproductions are replayed on the same
   device before anything is scored.
5. **Teardown and the board.** Emulators stop (`--keep-emulators` to keep them),
   results land in `runs/` beside the config file on your machine (the config's
   `runs_dir:`; a host `run` without the launcher uses `~/.qualgentbench/runs`), and the
   board prints. `show`, `check_tier_ready.py` and the other scripts read
   `~/.qualgentbench/runs` unless you pass `--runs-dir runs`.

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

The bench speaks streamable HTTP at `<url>/mcp`. To use
[DevLoop-MCP](https://github.com/QualGent/DevLoop-MCP) as the server, start it yourself
from its checkout in another terminal and leave it running for the whole sweep:

```bash
uv run devloop-mcp --transport streamable-http --port 51821 --app-source none   # serves http://127.0.0.1:51821/mcp
uv run qualgent-bench doctor --mcp-server http://127.0.0.1:51821   # from this repo: checks it
```

`--app-source none` is DevLoop's no-source mode: the agent tests an installed build and has
no app source, so the server drops its default read-the-source guidance, stops requiring a
`code_investigation` on FAIL, and stops answering a FAIL with a fix → rebuild → reinstall →
retest loop. `doctor` warns when a DevLoop-MCP server is not in that mode.

Then put `mcp_server: http://127.0.0.1:51821` in the config, or pass
`--mcp-server http://127.0.0.1:51821` to `qualgent-bench run`. The server binds
127.0.0.1 by default and accepts the `host.docker.internal` address the launcher
rewrites it to. The DevLoop desktop app also listens on 51821. If that port is taken,
quit the desktop app or pick another port (`--port 51831`) and use that port in the
URL. The same settings are available as `DEVLOOP_MCP_TRANSPORT`, `DEVLOOP_MCP_HOST`,
`DEVLOOP_MCP_PORT` and `DEVLOOP_MCP_APP_SOURCE`.

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
**bug finding** (found / present, false reports, one F1). Six public apps carry 41 cases
between them (5 to 9 each) × two versions = 82 episodes; two more apps are a held-out split
that never ships in this repository (docs/heldout.md) and join a run when
`QGB_HELDOUT_DIR` points at a synced copy.

Journey mode installs each app's **journey build** (its test-case file's `apk:` block),
which carries the journey-only defects the hunt build lacks. So guided mode never plans
the guided task of a journey-only defect, and `--mode all`, which installs one build per
app, refuses an app whose journey build is not its hunt build — run `--mode journey` on
its own.

```bash
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app tasksorg,medtimer,orgzly,ankidroid,fossify-calendar,fossify-contacts \
  --mode journey --devices emulator-5554,emulator-5556,emulator-5558
uv run qualgent-bench show --agent codex-cli --mode journey --run <run_id>
```

Run on the host, episodes land in `~/.qualgentbench/runs` (`--runs-dir` to change it).
A runs dir inside this repository, or under any directory holding a `CLAUDE.md` /
`AGENTS.md`, is refused: the agent would load that file as instructions, and this
repo's CLAUDE.md names the seeded defects. Runs made before 2026-09-23 are in `./runs`
— read them with `show --runs-dir runs`.

`--case` runs a chosen set of cases instead of every case of every selected app —
repeatable and comma-separated, both versions of each case always planned, an unknown
id refused before anything boots:

```bash
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 --mode journey \
  --app fossify-calendar,medtimer --case cal-switch-back-to-list \
  --case medtimer-analysis-tabular-view --device emulator-5554
```

A journey board requires the held-out split (docs/heldout.md): without `QGB_HELDOUT_DIR`
(or `heldout_dir:` in the config) `run --mode journey` refuses to start and `preflight`
fails. `scripts/holdout.py sync` verifies a synced split and prints the export line. A
deliberately public-only board takes `--allow-no-heldout` (`allow_no_heldout: true`,
`QGB_ALLOW_NO_HELDOUT=1`), and says so in the plan before it starts and under the printed
board.

In Docker, set `mode: journey` in `bench.config.yaml`; the image carries the journey
builds.

## Stopping a sweep and finishing it elsewhere

A full sweep costs more subscription credit than one account has in a seven-day
window. So a run can **stop on purpose** partway through, travel to someone else as
one small file, and be finished **on their credentials** — under the same run id, so
both sittings score as one run.

```bash
# machine A — stop the sweep at 85% of the seven-day window instead of the wall
uv run qualgent-bench run --agent claude-code --models claude-opus-4-8 \
  --tier easy --mode hunt --stop-at-seven-day-pct 85
#   → exits 75, writes ~/.qualgentbench/runs/_runs/<run_id>/stop.json

uv run qualgent-bench checkpoint export <run_id>     # → qgb-checkpoint-<run_id>-seg0.tar.gz

#   → send that file to machine B. It is kilobytes.

# machine B — its own .env, its own account
uv run qualgent-bench checkpoint import qgb-checkpoint-<run_id>-seg0.tar.gz
python3 scripts/launch.py bench.config.yaml --resume <run_id>   # boots B's AVDs,
                                                    #   runs only what is left
uv run qualgent-bench show --agent claude-code --mode hunt --run <run_id>
```

The launcher is the receiving command because it is what boots the emulators; it takes
the runs dir from the config's `runs_dir:`. `uv run qualgent-bench run --resume
<run_id>` does the same work against emulators you booted yourself.

**The bundle is results only.** It carries the plan, the schedule and every *completed*
episode's small scoring files. It never carries the agent's config home
(`claude_home/`, `codex_home/` — these hold live OAuth credentials), the transcript,
the evidence, the app snapshot, any `.env`, or the run-level rate-limit and stop state.
An interrupted episode is quarantined to `<runs_dir>/_discarded/` before packing, so a
partial episode can never ship as a result, and its unit comes back as work.

Authentication is excluded by two independent gates — a **path denylist** and a
**content scrub** that aborts the export on `sk-ant-`, `Bearer `, `refreshToken` and
friends — and both run again on import. That is the point of the feature: the person
who finishes the run does it on their own account. Check any bundle yourself with
`tar -tzvf`.

`run` exits **0** (finished), **75** (stopped on purpose, resumable — read
`stop.json`), or **1** (broke). `scripts/launch.py` reads that 75: on a five-hour
provider block it tears the emulators down, waits out the reset, boots them again and
resumes the same run id; on a seven-day threshold it prints the export command and
stops, because a weekly window is days from reopening. Configure both with
`checkpoint.stop_at_seven_day_pct` and `checkpoint.wait_for_five_hour_reset` in
`bench.config.yaml`. `stop_at_seven_day_pct` covers *every* weekly window the plan
reports — the generic one and any model-scoped cap — at whichever reads highest.

> The launcher now always passes `run --run-id-file`, a flag older harnesses do not
> have, so **rebuild the image** (`make image`) before running `scripts/launch.py`
> from this branch.
>
> Sharing a bundle through S3 is deferred. **Today you send the file.**

Full walkthrough, the bundle's exact contents, the `stop.json` schema and what still
needs a live run: [docs/checkpointing.md](docs/checkpointing.md).

## Reading the results

One folder per app (`<runs_dir>/explore-<app>/`; the runs dir is `~/.qualgentbench/runs`
for a host run), one folder per episode inside it:

```text
~/.qualgentbench/runs/explore-birday/2026-08-20T17-28-50Z_explore-birday_codex-cli_gpt-5.5_raw_trial-1/
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
- **"What did the agent actually do?"** → `qualgent-bench view --run <run id>`: a static
  site (no server, no CDN) at `<runs_dir>/_runs/<run id>/view/index.html` — one row per
  episode (filters for run, model, arm, public/held-out, completion, bugs, false reports,
  truncation, "changed on rescore") and a page per episode with the recorded-vs-rescored
  verdict (the rescore is `scripts/rescore_journey.py --dry-run`'s), the scored reports,
  the brief, `findings.yaml` and the full transcript with every screenshot the agent
  received. `run` writes it at the end of each completed sitting. It shows the answer
  key and any held-out episodes (badged "do not share"): keep it local. `--out` must stay
  inside the runs root (`--allow-outside-runs` to override), where an agent reading it
  voids its own episode. Per episode, `evidence/index.html` has the step-by-step
  screens and actions.
- **"Why did a claim fail verification?"** → `replay.json`. Each claim shows its
  classification, the step that stopped, the executor's judgment calls, and the
  environment it replayed in. A verdict should never be a mystery.
- **"Was the agent honestly measured?"** → `interactions.json` (the budget is enforced
  from this one file, in both arms).
- **"What did the whole run look like?"** → `<runs_dir>/_runs/<run id>/`: the plan you
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
  cli.py                 doctor / preflight / run / show / view
  view.py                `view`: the static episode site (index + per-episode pages)
  transcript.py          transcript parsing; `timeline()` is the reading view
  rescore.py             rescore one saved journey episode (used by view + rescore_journey.py)
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
- [docs/checkpointing.md](docs/checkpointing.md) — stop a sweep on a credit threshold
  and finish it on another machine: export/import/resume, what a bundle does and does
  not carry, the config keys, exit codes and the `stop.json` contract.
- [docs/heldout.md](docs/heldout.md) — the held-out split (two journey apps kept
  outside the repo, never committed) and the corpus version stamped on every
  journey result and board.

Working on the benchmark itself? `uv sync`, then `uv run qualgent-bench doctor` — and
before quoting any number, run the gates: `scripts/check_tier_ready.py`,
`scripts/adversary_check.py`, `scripts/validate_bundle.py`.
