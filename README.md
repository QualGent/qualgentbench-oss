# QualGentBench

A seeded-bug benchmark for coding agents on mobile QA.

Each app is a real open-source Android app rebuilt with known defects patched in. An
agent is given a neutral release-sign-off brief, no source code, and a step budget. It
explores the app on a device and reports what it finds in `findings.yaml`. The harness
then **replays its reproductions** to check the defects it claimed are demonstrable.

Sixteen apps across two tiers, 64 seeded defects and 71 working controls.

## Setup (first time, ~10 minutes)

You need four things. In order:

**1. [uv](https://docs.astral.sh/uv/getting-started/installation/)** — the Python
package manager this repo uses. Python 3.12 is installed for you by the next step.

**2. This repo:**

```bash
git clone <this repo> && cd qualgentbench
uv sync
```

**3. An Android emulator (or device) visible to `adb`.** If you've never set one up:
install [Android Studio](https://developer.android.com/studio), open *Device
Manager*, create any recent virtual device, and start it. Keep it running for the
whole benchmark. Then check:

```bash
adb devices        # must list a device, e.g. "emulator-5554  device"
```

If `adb` is not found: Android Studio installs it but does not add it to your PATH.
Add the platform-tools directory (macOS: `~/Library/Android/sdk/platform-tools`,
Linux: `~/Android/Sdk/platform-tools`) to PATH, or set `QGB_ADB_PATH=/path/to/adb`
in `.env`.

Everything else — installing the benchmark apps, granting permissions, disabling
animations — the harness does for you. APKs download from HuggingFace on first use
(public, no account needed), sha256-verified and cached under `~/.cache/qualgentbench`.

**4. The agent CLI you want to benchmark**, installed and logged in:
[`claude`](https://claude.com/claude-code) or
[`codex`](https://github.com/openai/codex). If the CLI already works in your
terminal, you're done — no API key needed. Keys in `.env` are only for special
cases (forcing API-key auth, Fireworks-hosted models, local models):

```bash
cp .env.example .env       # optional; every variable is documented inside
```

**Check everything at once:**

```bash
uv run qualgent-bench doctor
```

Every line should be green. If one isn't, it tells you the fix.

## Run your first benchmark

```bash
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app birday --mode hunt --trials 1 --device emulator-5554
```

This runs one episode: the agent gets a neutral "sign off on this release" brief for
a birthday-reminder app with 3 seeded defects, explores it on your emulator, and
reports what it finds. The harness then replays the agent's reproductions to verify
them, and prints the scored line. Expect **10–20 minutes and a few dollars of API
usage** per episode; you'll see the agent's progress and then the verification pass.

That was the **bare** arm: no device tools, the agent drives the device through
`adb` itself. To give the agent device tools instead, point it at any MCP server you
run (the harness never starts one — present means device tools, absent means bare):

```bash
uv run qualgent-bench run ... --mcp-server http://127.0.0.1:51821
```

## Run more apps

Pick specific apps, or run a whole tier — `--tier` replaces `--app`:

```bash
# a few named apps
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app birday,easynotes --mode hunt --trials 1 --device emulator-5554

# the easy tier (6 apps)
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --tier easy --mode hunt --trials 1 --device emulator-5554

# the medium tier (10 apps)
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --tier medium --mode hunt --trials 1 --device emulator-5554

# both tiers (all 16 apps)
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --tier easy,medium --mode hunt --trials 1 --device emulator-5554
```

Episodes run one after another on the device — budget roughly 10–20 minutes per app.
Re-print the board anytime — `show` filters by agent and mode, so match what you ran:

```bash
uv run qualgent-bench show --agent codex-cli --mode hunt
```

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
  cli.py                 doctor / run / show
  episode_runner.py      the engine: stage the device, run one episode, collect evidence
  bugs.py                task builders + scorers
  truth.py, replay.py    derived truth and differential replay
  verify/                device oracles, spec matching
  transcript.py          agent transcript parsing
  interactions.py        the step unit; adb_meter.py / mcp_meter.py are the counters
  episode_evidence.py    evidence bundles  (+ evidence_report, evidence_manifest)
  adapters/              claude_code, codex_cli, native
  data/benchmarks/       one YAML per app: defects, controls, probes
scripts/                 build, derive, gate, replay, validate
```

## Roadmap

- **Hard tier.** The 12 hardest apps — transactions, recurrence, media libraries,
  vaults — with 5 seeded defects each. The APKs already build; the defects still
  need seeding, verifying and gating, the same pipeline every shipped app went
  through. This is what takes the corpus from 16 apps to the full 28.
- **Dockerized setup.** One container with the emulator, adb, and the harness ready
  to go, so running the benchmark stops depending on what's installed on your
  machine. `doctor` should be a formality, not a checklist.
- **Parallel execution.** Today episodes run one at a time on one device; a full
  16-app sweep is an afternoon. Fanning episodes out across several
  emulators locally — turns that into minutes-per-tier, and is
  what makes multi-model comparison panels affordable.
