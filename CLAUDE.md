# QualGentBench

Seeded-bug benchmark for coding agents on mobile QA. The CLI is `doctor`,
`preflight`, `run`, `show`, and `checkpoint export|import|show` for handing a
half-finished sweep to another machine. See README.md.

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
  lanes, attempt, segment, adb server, image digest), and every episode dir carries
  an `episode.json` marker written at episode START (run id + unit identity), so an
  episode killed mid-flight is a provable orphan rather than a dir that may never
  have started. `artifact_dir` is stored RELATIVE to the runs dir — read it through
  `result.resolve_artifact_dir(runs_dir, result)`, never `Path(r.artifact_dir)`,
  which is only correct for pre-2026-09 results. `show --run <id>` scopes a board;
  without it every run in `runs/` is blended. `runs/_runs/<run_id>/` holds
  `plan.json` (scope + `segment` + an `environment` fingerprint: harness version,
  image digest, per-app spec hash and APK sha256), `schedule.jsonl`, `board.json`
  — whose `summary` block is the printed Bug-hunt table as data (one row per
  agent+model+condition, from `leaderboard.hunt_summary`, which the table itself
  renders — no drift), for later cross-run comparison/plotting.
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

Before a rebuild, `build_app.py --check-toolchain <app>` answers whether this machine
can build the app's pinned `build.ref` at all, and names the missing piece with the
`sdkmanager` line that installs it: the SDK platform for its `compileSdk` and the JDK
major version its Java level needs (`build.compile_sdk` / `build.jdk` in the spec,
else read out of the checkout). Google ships API 37 as `platforms/android-37.0`, a
dotted directory the build file asks for as `37` — an exact-name check calls an
installed platform missing. Never lower an app's compileSdk or Java level to fit the
machine. `QgbFlags.fired()` compiles and reports: `--demo-fired` seeds one throwaway
`fired("demo")` call at the spec's `build.demo_fired` anchor, emits
`buggy-demo-fired.apk` (a different name, so `publish_apk.py` cannot ship it) and
reads the marker back through `verify/canary.py` after the smoke launch. That smoke
launch resolves the launcher activity and `am start`s it — bare `monkey` silently
launched nothing on a Play-image emulator and failed a good build (2026-09-15), the
same reason `verify.device.relaunch` left monkey behind.

Feature states are **derived, never asserted**: `derive_truth.py` runs each check
against the clean and seeded builds. `check:` says how to exercise an area, not whether
it works.

Gate before quoting any number:

```bash
uv run python scripts/check_tier_ready.py --tier easy   # must print READY
uv run python scripts/adversary_check.py                # guessing must score <= 0
uv run python scripts/journey_adversary_check.py        # journey: 5 guessers earn 0 bugs/0 completions; priced adversaries pay on every clean episode
uv run python scripts/lint_journey_cases.py             # journey corpus text: no witness/brief carries a defect marker, every case has an oracle
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
screen diff). Its `--repeat N` runs each version N times from a fresh reset and demands
the identical outcome every time — no majority vote: any case whose defect is a forced
interleaving, a crash or a stuck-screen oracle must be derived with `--repeat` ≥ 3
before it enters the corpus, because one trial cannot measure a margin. An UNSTABLE
result (or a display marker seen in only k/N trials) is a `problems` entry and
`agrees: false` — the case leaves the corpus until the flip is understood; note the
reset restores app data and shared storage but not time, so a time-of-day-dependent
case (see `TODO(fixture)` in `medtimer.yaml`) can flip for that reason alone, which is
a corpus finding, not a replayer error.

**The replayer's own error rate, measured** (2026-09-15/16, QUA-2707 — the error bar every
journey pass/fail is read against). The whole corpus (40 cases, 8 apps, both splits)
derived at `--repeat 3` from a fresh reset on one emulator: 240 passes, 80 version-checks,
**1 unstable = 1.25% of versions, one case in 40**. The lone flip was
`mmex-withdrawal-summary`'s seeded arm — an input-dispatch ANR in MainActivity at step 2,
ONE step in, on a trial that followed a 157 s pass of the same 14-step route whose every
other pass took 65-71 s. That is host load, not the defect: the case's only bug is a
DISPLAY bug on a summary screen the route has not reached at step 2. Re-derived at
`--repeat 5` it was 10/10 HOLDS at 65-69 s, so the case stayed in the corpus. Read a lone
CRASHED/ANR trial on a heavy app (MMEX, AnkiDroid) as a re-derive candidate, not a finding,
and do not quote a journey delta smaller than about a point per version as signal. Every
other app was 10/10 stable, including the two tasks.org due-date cases that carried the
one historical flip. Markers are compared with typographic spaces FOLDED (`_hits`), the
same fold as the anchor matcher and the scorer; unfolded, the gate silently missed every
marker that names a time (`9:00 AM` vs the authored `9:00 AM`).

**Scoping a journey board below an app**: `--case <id>` (repeatable and comma-separated,
journey mode only; `parse_cases`/`split_cases` in `cli.py`, applied in `lanes.build_plan`).
It selects CASES, never episodes — both versions of a selected case are always planned,
so a seeded arm never arrives without its `active_bugs`. An unknown id, or an id of an app
`--app`/`--tier` did not select, is refused BEFORE the device and agent are probed and the
message lists the valid ids; a `--case` run whose plan comes out empty is an error, not
`Nothing to run.` + exit 0 — a board narrowed to nothing reads exactly like a finished one.
It is a scope flag, so `--resume` refuses it (the frozen unit list already carries it).

`scripts/rescore_journey.py` re-scores saved episodes. The device
timezone is pinned by `run_device_setup` (`QGB_DEVICE_TIMEZONE`, default
America/Chicago). `device_setup` fails LOUDLY: a `shell:` step that exits non-zero or
prints `run-as: exec failed` / `not found` / `No such file` / `Error:` / `sqlite3:`
raises `DeviceSetupError`, recorded as `staging_failed` → `env_failure`. Rows are
seeded into an app database with the host-side `sql:` step (`{package, db, statements
| file}` → `verify.device_oracle.apply_sql`: force-stop, `run-as cat` pull, one
transaction under the device zone, write back, verify) — never an on-device
`sqlite3`, which Google Play images lack; four fixtures seeded nothing that way for
weeks and `medtimer-skip-logged-dose` was charged to agents for it (2026-09-14).
A journey-only defect is a `bugs:` + `tasks:` entry in the spec with
NO exploration feature, so hunt mode never activates it. Journey mode fetches the JOURNEY
build — the test-case file's `apk:` block (`journey/<app>-buggy.apk` on HF, cache slot
`journey/`); dist/ still wins locally. `scripts/publish_apk.py <app> --kind journey`
moves the file and the hash together — DRY RUN by default, `--write` edits the block,
`--upload` (owner only: needs `--write`, `HF_TOKEN` and `--yes`) does the upload. Never
one without the other: `fetch_seeded_apk` sha256-checks every download, so a hash
without an upload and an upload without a hash break a fresh clone identically. A
rebuild is NOT byte-identical to the published APK (debug signing key, build-tools and
AGP versions ride in the file; measured for both journey apps 2026-09-15), so it is a
new artifact — `derive_journey.py` has to agree against it before the block moves, and
the journey block is inside `corpus_version()`, so boards do not blend across the
change. `db:` oracles are read after `am
force-stop` (a running AnkiDroid locks its collection); the launcher activity comes from
the package's launcher list with debug tools (LeakCanary) skipped.
A read-only case (nothing written, so no `db:` oracle can tell a run from a no-op) is
completed by its **screen witness**: `evidence:` strings the brief itself asks the agent
to read, shown identically on both arms and never a defect marker, symptom or measured
display text (`docs/journey-oracle-audit.md` holds the per-case audit of the public apps;
the held-out apps' rows live with the split). `journey_verdict` scores it: `completed = right verdict ∧
every witness in the text the DEVICE answered with` (token-boundary `_word`, device
RESULTS only — a typed argument never witnesses itself), an episode with no device text
at all stays unscored (None, "no device text to witness"), never False; in `db:`/
`content:` mode a declared `evidence:` is required on top of the oracle under the same
rule and a violated oracle dominates; expected-FAIL arms never consult it; a
`present:`/`absent:` case WITHOUT `evidence:` stays on the old unscored stopgap (none is
left in the corpus). `metrics.witness` records required/seen/missing/scored, and
`derive_journey.py` refuses a witness missing from the clean route's final screen or
sitting inside a display bug's measured texts. `verify.device_oracle.query_db` evaluates
oracle SQL under the DEVICE's zone (`getprop persist.sys.timezone`, cached per serial,
else the pin) in a child interpreter exactly as `apply_sql` does — `'localtime'` in an
oracle is the device's day, never the host's.
`scripts/lint_journey_cases.py` is the device-free gate on that text: a witness or brief
that carries a seeded defect's marker/symptom, or a case with no `check.expect`, fails it.

**Held-out split and corpus version** (`corpus.py`, `scripts/holdout.py`, docs/heldout.md).
Two of the eight journey apps live OUTSIDE the repo (`QGB_HELDOUT_DIR`, or `heldout_dir:`
in the config, default `heldout/` at the root — gitignored, never committed, and no file
in the repo may name which apps they are; `holdout.py verify` greps for that); every
loader resolves the held-out dir first, then the packaged data, and the board prints
held-out rows as their own block under the public one, never blended. Every journey
`result.json`, summary row, `plan.json`/`board.json` and the run header carry
`corpus_version` (12 hex of sha256 over `test-cases/*.yaml` + `truth/journey-*.json`) —
boards with different versions are not comparable, and a row mixing versions is starred.
A held-out APK is never published: its `apk:` block is `{path, sha256}` relative to the
held-out dir, read in place and hash-checked, with no download fallback. Spec and hunt-truth
paths go through `corpus.spec_path` / `corpus.stability_truth_path` — a hard-coded
`data/benchmarks/<id>.yaml` cannot see a held-out app, and a tier-wide `derive_truth.py`
writes held-out rows beside the split, never into `truth/<tier>-stability.json`.

**A missing split is never silent** (`journey.heldout_gap` / `NO_HELDOUT_NOTE`,
`cli._gate_heldout`, `preflight.check_heldout`). A journey board with no split produces
public rows and no held-out block, which reads exactly like a complete board while
answering a strictly weaker question — so every surface that can produce one says so: the
plan panel above `Continue?`, a line under the printed board (`show` too), and a preflight
WARNING on a journey config. `--require-heldout` (`QGB_REQUIRE_HELDOUT=1`, honoured by
both `run` and `preflight`) turns it into a refusal before anything boots. Note the trap
it names: `corpus.heldout_dir()` reads the ENV VAR only — the documented `heldout/`
beside the repo root is `scripts/holdout.py`'s default, not a harness fallback, so a
split synced there and not exported is invisible to a board.

**Reading the journey board's Rates block** (`src/qualgentbench/rates.py`; printed under
the ranking table by `run`/`show` and by `scripts/rescore_journey.py`, fields on every
`journey.summary` row). Two rates, each `k/n p% [lo–hi]` with a 95% Wilson interval, and
the denominators are the whole story: **false alarm / clean case** = clean EPISODES with
≥1 false report / clean episodes (per episode, not per report — three reports on one clean
build are one dirty night; seeded-arm false reports belong to precision, not here; every
non-excluded clean episode counts, completion-unscored and truncated ones included, so
`false_alarm_n` ≠ the `clean_episodes` column). **Catch / seeded defect** = seeded DEFECTS
found / present (per defect: a case with one functional and two display bugs is three; this
is `recall` with an interval). **Clean-run integrity @200** = (1 − false alarm)^200, the
probability a nightly suite of 200 clean cases comes back clean, at a FIXED N so boards
compare (a 1% rate is 13% clean nights; our measured 10–22% is zero); the interval is the
rate's interval pushed through, so a 0/4 row prints `100% [0–100]` — honest, not broken.
`--projection N_CLEAN N_SEEDED` on the rescore script composes expected false alarms and
misses for a reader's suite. **Blocker recall** = found / present over FUNCTIONAL defects in
L4+L3 only (tiers resolved from the app's test-case file; `—` when none were seeded, never
0/0) — the one severity-aware number. The tier weights 1/3/6/10 in `bugs.py` are a house
convention, not derived from any published severity scale; journey mode never weights by
them and nothing should imply it does. Intervals count trials as draws, so power comes
from DISTINCT cases (~200 for ±5pp at 15%, ~450 for ±2pp at 5%) — repeat trials narrow the
bracket on paper only. F1 stays the ranking key for now; the rates are published beside
it, not blended into it.

**Crash, ANR and stuck-screen cases** (2026-09-14; `submission._GATE_KEYS`,
`replay.gate_crash`, `replay._check_stuck`, `verify/canary.py`). The polarity rule that
makes them authorable: journey mode needs the CLEAN arm to PASS its oracle and the
SEEDED arm to FAIL, and `run_steps` already turns the app's death on the route into
CRASHED = FAIL. So a crash-seeded case keeps its ORDINARY completion oracle (`db:` /
`present:`) and needs no new key at all. The harness-only keys `crash:` / `anr:` /
`stuck:` are therefore GATES, never demands — "the route runs with the app alive; if it
dies, it must die THIS way": `crash: "<sig text>"` (normalised signature or exception,
substring) or `anr: true|"<reason text>"` make a seeded arm that dies some OTHER way
INCONCLUSIVE ("crashed, but not the expected crash: <sig>") instead of a FAIL that
agrees, and `derive_journey` additionally refuses a seeded arm that fails with the app
alive when the check names a death. A positive "must crash" expectation was rejected on
purpose: it inverts the clean arm on every derivation path. Use the gate riding on the
state oracle (`{db: ..., crash: "IllegalState"}`) or standalone when the route is the
outcome; only `db`/`content`/standalone gates are evaluated by the episode runner
(`_journey_oracle` reads them off `spec["app_crashes"]`, ANRs now included — no
re-query), a `present:` oracle's gate is diagnostic only. `stuck: "<anchor>"` is the
same polarity as a PROBE: a hung app that receives no further input never ANRs, so a
freeze on the LAST route step (or inside a `wait`) is invisible to the route — the probe
sends exactly ONE tap on the anchor after the steps and watches the input dispatcher
(`unresponsive_windows`) and `am_anr` until `anr_timeout_ms + 3 s`: answered → HOLDS,
given up → CRASHED (kind `anr`), anchor nowhere → INCONCLUSIVE. A frozen app's
hierarchy CANNOT be dumped (measured: 11 s, 39 bytes), so the anchor falls back to the
route's last readable screen (`replay._LAST_VH`); the probe runs BEFORE a `db:` read
(which force-stops the app) and changes the screen on a live app. Proven live by
`scripts/crash_probe.py --stuck` (frozen → CRASHED/anr in ~26 s; live → HOLDS in
~0.5 s; one tap each). Attribution canary: a seeded patch calls `QgbFlags.fired("<id>")`
on the line BEFORE the fault (the generated shim touches
`files/.qgb/fired/<id>` in the sandbox); the harness reads it after the pass / after
the agent exits (`fired_markers`, `ReplayResult.fired`, `spec["fired"]`,
`metrics.fault_fired`) and identity is then by construction, the signature
corroboration: marker for the seeded bug + signature match → CRASHED; marker + mismatch
→ INCONCLUSIVE with both facts; a marker for an unseeded bug, or ANY marker on the clean
arm → INCONCLUSIVE (the flag gate did not hold) and a `derive_journey` problem. Markers
are wiped in the same `run-as` command that writes the flags file and before the cold
snapshot tar. THE LEAK: the ADB meter was a pure counting proxy, so a bare-arm agent
could `adb shell run-as <pkg> cat files/qgb_flags.txt` and read this episode's seeded
ids. `adb_meter.deny_reason` now answers `FAIL` at the socket (never relayed, counted as
`metered_denied`, not charged as a step) for `run-as`, `/data/data|user*/`,
`/data/local/tmp/qgb*`, `qgb_flags`/`.qgb` and the `backup:` service. This covers the
agent's own adb in BOTH arms (its adb env is pinned to the meter); an MCP server's tools
run over the server's own adb — if a server exposes a shell tool, `QGB_DISALLOWED_TOOLS`
is the only lever. Authoring: `--repeat >= 3` for every crash/anr/stuck case; a
`stuck:`+`present:` oracle must name text that survives the probe tap; forced
interleavings: an operator that changes an ORDERING is seedable (swap two awaits,
post-vs-run, commit-before-write), one that merely WIDENS a window (a sleep, a slower
loop) is not — it measures the device, not the defect; prefer patterns where Android
itself is the oracle (a view touched off the main thread throws
`CalledFromWrongThreadException`, a fragment transaction after `onSaveInstanceState`
throws `IllegalStateException`) so the fault is a deterministic crash with a stable
signature rather than a race.

**The two freeze exemplars, and what they measure** (2026-09-16, QUA-2711; MedTimer
`medtimer-take-dose-then-medicine-list` and `medtimer-analysis-tabular-view`). Before
these, both freeze paths had only ever met a process frozen BY HAND (`crash_probe.py
--anr` / `--stuck`, which SIGSTOPs the app); no seeded code blocked a main thread. They
are a PAIR because they differ in exactly one thing — whether input is PENDING when the
thread stops — and that is what decides which detector can see them:
`overview-action-blocks-main-thread` blocks inside a click handler, so the route's next
touch goes unanswered and Android raises the ANR itself (route detects it,
`{db: …, anr: true}`); `analysis-table-freezes-on-open` blocks in a `LaunchedEffect` one
frame AFTER that touch was answered, so NOTHING is pending, Android raises no ANR at all,
and only the probe's one tap reveals it (standalone `{stuck: "Tabular view"}`). Both
operators are post-vs-run, not sleeps: work that was `lifecycleScope.launch`ed is
`runBlocking(Dispatchers.Main)`'d from the main thread, which parks that thread and posts
the body to the Looper it just parked — a permanent deadlock with no margin to measure.
Measured at `--repeat 3`: clean 3/3 HOLDS (the stuck probe answered in 547-775 ms, well
inside `anr_timeout_ms + 3 s`), seeded 3/3 CRASHED kind `anr`, each firing ONLY its own
marker and none on any clean arm. Two authoring facts fell out of it. First, an `anr:`
gate should stay `true` rather than name a reason: the dispatcher's wording carries a
per-run window hash and the window itself differs between the two cases (`Pop-Up Window`
vs `MainActivity`), so a reason string would gate on a sentence that is not the defect.
Second, a standalone `stuck:` is not optional — only `db`/`content`/standalone gates are
evaluated by the episode runner, so a `stuck:` riding on a `present:` would be diagnostic
only and the CLEAN arm's probe would never run.

**What a freeze case can be credited for, and what it cannot** (same date). A death or a
hang leaves the clean/seeded screen diff full of strings the seeded agent never saw — on
these cases it captured the platform's own ANR dialog, and on the merged crash case the
LAUNCHER behind the dead app (`At a glance`, `Chrome`, `Gmail`, `Google Lens`). None of
it is quotable: `journey_tasks` empties the diff lists whenever the check names a death
and builds crash evidence instead, so measured through the real `match_report`, launcher
strings, brief nouns, measured display texts and even a string the route TYPES
(`Lisinopril`) all earn nothing on all three death cases.

**What a quote has to PROVE, and the four routes to the blocking bug** (2026-09-16,
QUA-2717 — this replaces the "creditable with no device contact" gap the paragraph above
used to end on). A report earns the blocking bug through exactly one of four lists, and
they differ in what quoting them proves. `blocking_texts` is the `added` side of the
screen diff MINUS anything the brief or the route already handed the agent
(`echo_haystack`: case name, steps, expected outcome, every `type:`/`tap:` value) — only
the seeded build showed it and nobody gave it away, so the quote IS the sighting.
`crash_texts` is now the SIGNATURE alone (`crash: "NoSuchElementException"`), which names
this death and no other. `echo_texts` is everything real but writable blind — a brief
noun the route types and then finds still on screen (`Lunch`, `Call dentist`, `Alice`),
plus the platform's crash/ANR dialog, bare and app-qualified — and it is the one route
gated on `BugReport.grounded`, which is now computed from device RESULTS only (a typed
argument never witnesses itself, the same rule the screen witness runs under) and is no
longer a bare diagnostic. `absence_texts` is the `removed` side, matched against the
report's `expected` and never its `observed`: a defect that manifests as a MISSING string
(`cal-repeat-survives-rotation`, `contacts-phone`) has nothing to observe, and the clean
build's value is what the brief's own example puts under `expected`. Nothing was deleted
by this, only DEMOTED — an honest sighting of `Lunch` still earns the bug, it just has to
show the device said it. Symptom vocabulary is read off `BugReport.prose`
(`description`) and nowhere else: `delete`, `back`, `tags`, `rename` and `not responding`
are all symptom entries in the corpus AND words the briefs themselves use, so a report
that merely QUOTED one used to be credited for describing a misbehaviour it never
described (`lint_journey_cases.py` only warns on multi-word phrases, by design).
`journey_adversary_check` now carries `brief-echo` (every quoted phrase and capitalised
word in the brief, sprayed into `screen`/`observed`/`expected`) and `dialog-echo` (the
platform wording) in `GUESSERS`; both earn 0/39 · 0, and `honest` (38/39 · 24) and
`honest-text` (27/39 · 15) are unchanged by the whole change.

**The adversary the roster cannot hold, and what is asserted about it instead.**
`symptom-spray` writes the corpus's own symptom vocabulary as prose with nothing quoted
and earns 39/39. That is not a hole to close: prose is the ONLY report a functional
defect with no string to quote ever has (12 of the 39 seeded defects are that shape, and
the script prints them), so a matcher that refused it would refuse the honest report with
it. It therefore lives in `PRICED`, not `GUESSERS`, and the gate asserts the PRICE — it
pays a false report on 36/36 clean episodes (100%), because nothing is active on a clean
build. Recall that stops costing a dirty night is the regression that catches. This is
also why `_no_symptom_leaks_into_the_filler_prose` is scoped to the two FILLER constants
rather than to every adversary: held over all prose, that invariant is precisely what
kept the roster from ever containing the attack most likely to work. Read a catch rate on
this corpus against the clean-arm false-alarm rate, never alone.

**Lifecycle cases: state lost on a configuration change or process death** (2026-09-15;
`replay._rotate`, `submission.ACTIONS`). A route can force exactly two lifecycle events:
`rotate: landscape|portrait` (Android destroys and RECREATES the activity — state the app
failed to save is gone) and `relaunch` (process death). Authoring shape, and the polarity
rule is the same as everywhere else — the CLEAN arm must PASS and the SEEDED arm FAIL:
put the steps that PRODUCE the state first, then ONE lifecycle step, then the oracle read.
`{type: "draft"}, {rotate: landscape}, {present: "draft"}` is the whole case; a rotate
AFTER the read measures nothing. `rotate` turns auto-rotate OFF and only then pins
`user_rotation`, because with `accelerometer_rotation` still 1 the setting is advisory and
the sensor (an emulator reports a fixed one) can put the device straight back — the
configuration change silently would not happen and the case would pass for the wrong
reason. It then `wait_stable`s, since the recreated activity has not drawn and the next
step's anchor does not exist yet. `_reset` restores PORTRAIT before every pass: rotation is
a DEVICE setting and `pm clear` does not touch it, so a route ending in landscape would
otherwise hand the next pass a rotated device it never asked for (the leak shared storage
had). Consequence: a route may not assume a landscape start — rotate into it explicitly.
**Both staging paths reset it, through the same helper.** Replay's `_reset` was the only
one until 2026-09-17: the LIVE path, `episode_runner.normalize_app_env`, set animation
scales and permissions and never touched orientation, so once QUA-2712 added a case whose
brief asks the AGENT to rotate, its `settings put system user_rotation 1` — global and
persistent — leaked into every later episode on that device AND into the next run on it.
It contaminated the 2026-09-17 pilot (`docs/pilot-2026-09-17.md`). `normalize_app_env`
now calls `replay._set_rotation` itself rather than repeating those two adb calls, because
the ordering is the load-bearing part and a second copy of it is a second thing to invert;
`test_both_staging_paths_share_one_rotation_reset` asserts it is literally the same
function. Both arms can rotate and both are charged ONE step: the bare agent's `settings put system
user_rotation` is not on `adb_meter.deny_reason`'s list and classifies as `other`; on the
MCP arm the tool is `mobile_set_orientation` (`mobile_get_orientation` is a read and is
ignored), which also has no `_MCP_RULES` entry and lands on `other` — one interaction
either way, which is correct, so neither meter needed a rule. Only orientation: dark mode,
locale and font scale are NOT in the grammar. The device-free gate is
`lint_journey_cases.py`'s `route` rule — it checks every `check.steps` entry against
`submission.ACTIONS`, because `truth._steps` parses trusted YAML permissively and
`replay.run_steps` only discovers a typo'd verb on a device, as an INCONCLUSIVE pass that
reads like a flaky case.

## Tool surface

**The brief is versioned, because it is part of the treatment** (`brief.py`,
`BRIEF_VERSION`, stamped into `provenance.brief_version` on every `result.json`, into
`plan.json`'s environment fingerprint — so `compatibility` refuses a resume across it
— and printed above `Continue?`). Both briefs, hunt
(`episode_runner._ablation_instruction`) and journey (`journey.brief`), are
byte-identical across arms except ONE paragraph, the tooling note; it used to exist as
two inline copies that happened to agree and now has one source.
**v1** said only "use the tools available in your environment (for example the `adb`
command line)", which left HOW to read a screen to the agent — and that is agent
property, not benchmark property: codex-cli reaches for `uiautomator dump` unprompted,
claude-code defaults to a screenshot plus guessed coordinates. Measured on run
20260916-234512-18ac, both arms of `cal-switch-back-to-list` on claude-code: 12 and 19
`screencap` calls against **3** `uiautomator` each, whole budget gone at step 2 of a
10-step route, `metered_denied: 0` — the adapter was fine. So the bare arm was partly
measuring "does this agent guess `uiautomator dump`", which publishes as a capability
gap it is not. **v2** (QUA-2715) names both ways to read a screen, in the same words for
every agent, recommending neither; both classify as one `observe`, so it is an
affordance and not a discount. **The 70 codex journey episodes on disk are all v1 and
are not directly comparable to a v2 number.** Keep the note agent-neutral and
app-neutral — anything app-specific there is a hint, anything agent-specific makes the
arms measure different things (`tests/test_brief.py` pins both, and pins that no adb
command the note names is on `adb_meter.deny_reason`'s list).

**A cost of `$0.00` must never be printable for an episode nobody measured**
(`pricing.usage_metrics` — the single builder of the cost/token block six scorers used
to inline). `cost_source` is `reported` (the agent's own `total_cost_usd`),
`estimated` (measured tokens × `PRICING`), `unpriced` (real tokens, model not in the
table) or `unavailable` (no usage in the transcript at all); the last two carry
`cost_usd: None` and `total_tokens: None`, and the run footer names the count rather
than folding them into the total as zeros. The bug this replaced: claude-code's
cumulative `result` event is written on a CLEAN exit, and a budget-truncated episode
never gets there — the hook drops the sentinel and the process group is SIGKILLed — so
`token_usage()` summed nothing and priced it as "estimated". Codex was never affected
(`turn.completed` deltas accumulate as it goes). `token_usage()` now falls back to the
per-REQUEST usage on `assistant` events, **deduped by `message.id`**: the CLI emits one
event per content block, so 44 requests arrive as 86 events carrying each request's
usage two or three times and a raw sum roughly doubles the bill. Summing per-request
usage is right for billing even though the prefix is resent every turn — each request
is charged for its own full input, cache reads at the cache rate. Re-read against the
smoke run: `$0.00` → **$1.12 and $1.29**, 2.9M and 3.5M tokens. `usage_source`
(`result`/`turns`/`stream`/`none`) rides on every result.json and is what decides
measured-vs-not; never the magnitude, since an episode may legitimately spend little.

Prices come from the `claude-api` skill, never from recall. Anthropic rows are the
Claude 5 family plus 4.x for older boards; `cached_input` is the cache-READ rate.
Deliberately absent, because a plausible number in a table the board MULTIPLIES BY is
worse than a missing row: `claude-fable-5` (in/out published, cache-read rate not, and
the Fable tier does not follow the usual 0.1× rule — 5.1 reads at $0.25/MTok, i.e. 0.025×) and
`claude-mythos-5/5.1` (limited access, rate open). `claude-opus-4-8` was carrying
$15/$75 — Opus 4.1-era numbers, 3× the real $5/$25 — and is corrected.

Neither agent shapes tools by default. `QGB_DISALLOWED_TOOLS` (comma-separated) is the
only source; unset or empty withholds nothing. It reaches MCP tools only — for
claude-code every name is prefixed `mcp__device__`, for codex it lands in the
per-server `disabled_tools`.

`tests/conftest.py` strips `QGB_*` before every test; without it the suite asserts
against whatever the developer's `.env` happens to contain.

**The test suite cannot reach a device.** `tests/conftest.py` installs a guard at import
time that fails any test (or collection) spawning `adb` — by `subprocess.*`, asyncio,
`os.system`/`posix_spawn`/`exec*`, `sh -c "adb …"`, the `QGB_ADB_PATH` binary, a child
process (PATH carries a fake `adb` that logs and exits 125), or the adb server socket
on 5037 (uiautomator2/adbutils). Non-adb spawns (`sys.executable`, `git`, `gh`) are
untouched. The failure is a `BaseException` so the code's `except Exception` fallbacks
cannot hide it, and its message names the test and the argv. To run against a device
on purpose, mark the test `@pytest.mark.live_device` (lifts the guard for that test)
AND run with `QGB_LIVE_DEVICE=1` — marked tests are skipped otherwise. Why: in 2026-09
`uv run pytest` was run while a benchmark episode was live on the only emulator;
`test_adb_meter.py` probed `adb devices` at import and, finding one, sent five `input
tap`s to it, which brought another app to the foreground and failed the episode's
precondition. Stub the adb seam instead (`tests/test_replay.py::_no_device`,
`test_episode_precondition.py::_no_device`); `tests/test_device_guard.py` pins the guard.

**Budgets are NOT re-derived for the current step unit.** Every `step_budget` was sized
against an older counter, and the unit changed again on 2026-08-19 (~1.2-1.6x looser
now, agent-dependently). Re-derive with `scripts/derive_budgets.py` before quoting a
score that depends on speed or truncation. It covers all three kinds, from two different
trees: `--mode hunt` does `exploration.step_budget` and `tasks[].step_budget` in
`data/benchmarks/*.yaml`, `--mode journey` does `test_cases[].step_budget` in
`data/test-cases/*.yaml` (it had no notion of journey budgets at all until 2026-09-11,
while this paragraph told you to re-derive them). Neither mode writes anything without
`--write`: a budget is a hard gate, so moving one is a review, not a side effect.

**A truncation is not by itself a case for a bigger budget.** Over the 40 scored journey
episodes on disk at 2026-09-11, not one landed between 73% and 100% of its cap — an
episode either finished with a quarter of the budget unused or blew past it (102-108%) —
and two of the six truncations had causes of their own (a fixture that was never on
screen, an agent lost in a date picker). That is runaway, not shortfall. So `--mode journey` prints a verdict per
case (under-budget / runaway / no evidence) with the episode count behind every number,
judges a truncation against the worst cost per route step a FINISHED episode has ever
paid, and refuses to propose a raise off a runaway — raising the cap there buys the agent
more failing steps and charges every other episode in tokens for it. Today exactly one
case is under-budget on the evidence (`anki-add-tagged-note`: longest route in its app,
both versions died at 56/55, nothing of it ever finished) and 25 of 40 cases have no
evidence at all. Get this backwards and the cost is doubled: a truncated journey episode
scores as not-completed AND as every seeded bug missed.

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
