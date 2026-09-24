# QualGentBench

Seeded-bug benchmark for coding agents on mobile QA. The CLI is `doctor`,
`preflight`, `run`, `show`, and `checkpoint export|import|show` for handing a
half-finished sweep to another machine. See README.md.

All three tiers are hunt-ready and gate-green: easy (6 apps), medium (10) and hard
(12) — 265 scored areas, 129 seeded defects, 129 working controls. Hard-tier apps
carry conditional defects and `hidden: true` areas reported via `other…`. Every
tier keeps the uniform step_budget 500 by decision (no per-app budget derivation).
APKs download from HuggingFace on first use (`apk:` block in each spec: repo,
filename, sha256), from the dataset revision `data/apk-pins.json` pins for that sha256
(else the path's HEAD).

Spec-authoring rules that have caught real bugs: a debug build with a SECOND
launcher (LeakCanary) makes launches nondeterministic; a Compose control can be
INVISIBLE to accessibility; `db:` accepts an absolute shell-readable path for
external-storage databases (harness-only — the agent path rejects oracle
expectations); an unstable check leaves the corpus rather than being asserted; a
CONTROL on the same screen as a hidden defect must be `collateral` or right agents
get charged, and control wordings must not contain defect-adjacent clauses; in a
KMP app the flag shim lives in the jvm-shared source set, never commonMain; a fixture
must never CREATE an app's `Android/data` tree (root or shell, its creator owns it and the
app cannot use it on a device where it never ran) — let the app make it with one launch,
then write onto its files (AnkiDroid, QUA-2743).

This repo was pruned to the seeded-bug benchmark alone during 2026-08-17..19 —
TrustLoop, CreateBench, the customer track, the legacy `tasks/` layer, the two-arm
board and all DevLoop naming are gone. Reference docs live in `docs/`
(architecture.md, scoring.md, the three extension guides, design.html, defect-classes.md).

## Two rules that have caught real bugs

**`uv run ruff check --select F821` is the check that catches a bad deletion** (ruff is in
the `dev` dependency group, so `uv sync` installs it). `compileall` and a
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

On the MCP arm the classification is ONE table, `interactions.MCP_TOOL_RULES` (exact
names, no prefix guessing): what the meter charges, whether a tool's result is device
evidence, whether its reply is only its own argument echoed back (`echo`: the text-entry
tools — DevLoop answers `Set focused field to: '<text>'` — so journey grounding drops
that reply and type-then-quote grounds nothing, QUA-2805), and whether it is a screen read — `transcript`/`bugs`/`journey` derive their
lists from it. Every DevLoop-MCP tool has a row, pinned against
`tests/fixtures/devloop_tools.json` (DevLoop's `tools/list`; regenerate with the command
in its `_about`), so a new DevLoop tool fails the suite instead of costing an accidental
`other`. `mobile_tap_and_observe` costs tap + observe = 2 since QUA-2775 (it was one tap),
so MCP-arm step counts from before that change undercount agents that used it. The table
is also in docs/architecture.md; change both together.

**Both agents' transcripts score identically for identical MCP activity** (QUA-2776,
`tests/test_mcp_scoring_parity.py`: one DevLoop episode — 10 device calls, a parallel
batch, a refused tap, a refused then an accepted `mobile_report_result` — written as
claude-code stream-json and codex `exec --json`, every scorer input and the journey
(findings-file and report-tool sources) and hunt metrics compared). A Fable-vs-Astra
board runs one model per adapter, so a parser asymmetry publishes as a model gap. The
rules: tool names are normalised at parse time (`transcript.split_tool_name`:
`mcp__device__mobile_tap` → `mobile_tap`, server kept in `ToolEvent.server`) and every
device check is the table's predicate on that name, never a `startswith("mcp__device")`;
codex records EVERY `mcp_tool_call` as claude records every `tool_use`; one result
reader, `transcript.mcp_result_text` (text blocks, images dropped; codex had kept
`json.dumps` of the whole result); MCP `isError` fails the call on both (claude
`is_error`, codex status `failed`; `ToolEvent.is_error` is that flag alone); and on the
MCP arm a claude call enters `bugs._ordered_stream` when its result arrives, as codex's
`item.completed` does, so a parallel batch (call, call, result, result) pairs each
result with its own call. Raw-arm parsing is untouched (`rescore_journey.py --dry-run`
byte-identical over the 34 saved raw episodes).
MCP result text is escape-decoded once (QUA-2801, `tests/test_mcp_unicode_escapes.py`):
DevLoop's `mobile_tap_and_observe(include_screenshot=true)` answers with `json.dumps(…,
indent=2)`, codex keeps its `\uXXXX` escapes and claude keeps the characters, so a
non-ASCII witness (orgzly's `•` breadcrumb), marker or quote matched on claude only.
`transcript.decode_unicode_escapes` (JSON `\uXXXX` incl. surrogate pairs; an escaped
backslash and the `\u` after it stay literal) runs on every MCP result in
`bugs._ordered_stream` — so before `_device_text`'s fold; never on a raw-arm shell
result there — and replaces the naive per-escape decode `clean_result_text` (the parsed
`ToolEvent.result_text`) always had.

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

**One server, every episode: the server must start each episode clean** (QUA-2800). The
operator's standalone DevLoop-MCP serves every episode of a run (and every lane of a
`--devices` run), one agent MCP session per episode (measured: claude-code 2.1.281 opens
one initialized session per process, codex-cli 0.146.0 one, DELETEd at exit). Before
DevLoop's fix its state was per DEVICE, not per session: the routine action log
(`mobile_get_action_log` returned earlier episodes' taps, the other arm of the same case
included), visual baselines (a shared `~/.devloop-mcp/baselines`, listable and comparable),
native/JS/React profiles and traces (a trace left running refused the next episode's start),
recordings (`recording_conflict`), debugger sockets and device-session caches. DevLoop now
scopes all of it per MCP client session over HTTP and, when a session first touches a device
another session used, stops that session's recorder/trace/socket there; stdio is unchanged.
`doctor`, `preflight` and `run` REFUSE a DevLoop server that does not report
`isolation: per_mcp_session` at `GET <server>/devloop/sessions` (`doctor.
check_mcp_episode_isolation`; other MCP servers are not judged). After the agent exits,
`run_episode` records `provenance.mcp_isolation` (`session.fetch_episode_isolation`): every
server session created during the agent's run that touched this device, each with
`clean_at_start` and which session it took the device from, and `clean` — the per-episode
assertion a comparison doc checks. `isolation: unavailable` = no record (older DevLoop, other
server); more than one session = the agent reconnected mid-episode. **On disk too:** the
server writes screenshots, diffs, baselines, traces, profiles and recordings to the host
(its temp root `…/devloop-mcp`, `~/.devloop-mcp`, `~/.devloop`), and a host-run agent has a
shell. DevLoop deletes a session's files when the session ends or loses its device, but a
concurrent lane's exist while it runs, so the contamination scan is the rule:
`devloop_artifacts`, a HARD hit (voided, like `other_episode`), for any path under a DevLoop
root the server did not hand THIS agent in its own MCP tool results (a handed path inside
`sessions/<id>/` hands that whole session root; a message naming a root itself hands
nothing). Roots are `contamination.devloop_default_roots()` plus the server's reported
`artifact_roots`, in `bug_spec["devloop_roots"]`, both arms. Replayed over every saved
transcript (116 MCP-arm in `~/.qualgentbench/runs`, 70 raw-arm in `./runs`): 0 voided —
no agent touched those paths. Over the two MCP-arm runs
before the fix (20260923-114342-b8af, 20260923-174028-afd5; 116 episodes) no agent called a
tool whose result could carry another episode's state (0 action-log, baseline/compare,
recording, profiler, debugger or OTP calls; both `mobile_report_result` calls were FAIL, so
no routine checkpoint). The one effect of carried state was the ROUTINE_RECORDING_AVAILABLE
notice's once-per-device flag: the first episode of each run got the notice (its content was
that episode's own first tap) and no later one did — an order effect, not a leak of another
episode's content (QUA-2792 removed the notice from standalone). `mobile_device_logs` /
`mobile_crash_logs` (36 + 27 calls) read the DEVICE, which staging clears (QUA-2781/2790), not
server state. So no past episode read another episode's state; the notice's order effect
touched those 2 episodes only.

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
  `Continue?` unless `--yes`. With no terminal on stdin (a pipe, CI, an agent's shell)
  and no `--yes`, `run` and `run --resume` print the plan and exit 1 WITHOUT starting,
  whatever is piped in (`cli._confirm_start`, QUA-2798): the old gate skipped the
  question and started, so `echo n | qualgent-bench run …` launched a paid board. That
  refused run is the plan preview; no plan.json is written until the start is
  confirmed. `tests/test_run_confirm.py` pins it at the CLI seam.
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
  without it every run in the runs dir is blended. `<runs_dir>/_runs/<run_id>/` holds
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
  **The workspace lives OUTSIDE the repo** (QUA-2778). Both agents load instruction files
  from their cwd's ANCESTORS as start-up context — claude-code walks every ancestor to `/`
  (CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md, .claude/rules/*.md; CLAUDE_CONFIG_DIR
  does not stop it), codex-cli reads AGENTS.md / AGENTS.override.md from the git root
  down. With runs at `./runs`, THIS file (defect ids and mechanisms) reached every
  host-run agent as system context, where the contamination scanner cannot see it:
  `claude -p /context` from `runs/<task>/<run>/workspace` listed this CLAUDE.md (20.6k
  tokens) and the parent directory's CLAUDE.md; from `~/.qualgentbench/runs/...` it listed
  no memory at all (2026-09-23). So host runs default to `~/.qualgentbench/runs`
  (`config.default_runs_dir`; `show` and `checkpoint` read it too), and `run`, `preflight`
  and every episode refuse a runs dir inside the repo or with a non-empty instruction file
  anywhere on its ancestor chain (`config.runs_dir_problems`) — a sibling of the repo is
  refused on the owner's machine for the parent CLAUDE.md, and so is the default on a
  machine with a non-empty `~/.claude/CLAUDE.md`. `--allow-runs-in-repo` runs anyway,
  contaminated, and says so in `provenance.inherited_instructions`. Runs from before the
  move are in `./runs`: `show --runs-dir runs` still reads them (artifact dirs are
  relative, so `mv runs ~/.qualgentbench/runs` carries a run over for `--resume`).
  Reading ANOTHER episode's dir is a hard `other_episode` contamination hit, the rule
  `benchmark_repo` used to cover. `tests/test_runs_dir_isolation.py` pins all of it.
- Rate limits: `metrics.failure_class = "rate_limited"` (`failures.py`) is excluded
  like `infra_failure`; the scheduler holds ALL lanes with exponential backoff,
  requeues the unit as a fresh episode (max 4), and parks lanes if it persists.
- Docker: the image (`Dockerfile`) holds the harness, adb client, claude/codex and
  the APKs (`scripts/bake_apks.py`); emulators and the MCP server stay on the host.
  In the image, answer-key isolation is kernel-enforced: agents run as the
  unprivileged `agent` user (`QGB_AGENT_USER`), `/app` is root-only, runs live at
  `/work/runs` outside the repo. The contamination scanner is the backstop there;
  on native host runs it and the runs-dir refusal above are the only guards.
  `scripts/launch.py` (stdlib only) asks the image to validate the config
  (`preflight --json`), checks the host, boots the AVDs, runs, tears down. adb is
  reached through `ANDROID_ADB_SERVER_ADDRESS` (adb) + `ANDROID_ADB_SERVER_HOST`
  (adbutils/u2); the agent's adb env points back at the loopback meter (an env, not a
  wall: see the text-rule limits under Crash, ANR and stuck-screen cases).
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
uv run python scripts/journey_adversary_check.py        # journey: 6 guessers earn 0 bugs/0 completions; priced adversaries pay on every clean episode
uv run python scripts/lint_journey_cases.py             # journey corpus text: no witness/brief carries a defect marker, every case has an oracle, every defect has a class, every side bug a quotable marker and (public, not deferred) a reference
uv run python scripts/validate_bundle.py ~/.qualgentbench/runs/<task>/<run>
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

**One UiAutomation client per device, and the agent must get the slot** (QUA-2741).
uiautomator2's on-device server (`app_process / com.wetest.uia2.Main -p 9008`) holds the
device's single UiAutomation registration while it runs. It is started by the harness's
own fallback reader and `type` step, and by the DevLoop MCP server on every screen read.
A server started by a Python process can also stay behind after that process exits.
While it runs, every other `uiautomator dump` dies: `IllegalStateException:
UiAutomationService … already registered!` is uncaught, and the app_process kills
itself (exit 137, "Killed"). QUA-2731's board lost all 371 agent dumps this way and
tested from screenshots. The harness never noticed, because `_dump_vh_raw` falls back
to u2. Three guards now: `run_episode` calls `verify.device.stop_u2_server` after
staging's last read and before the agent starts. It kills the server whoever started
it. `run` refuses a board whose device still kills an agent's dump after that stop
(`preflight.check_agent_dump`, per device, before a run id or a plan exists). And
`dump_stats` (builtin / u2 / none, plus `builtin_killed` attempts) is recorded in every
episode's `provenance` and every derive truth row. A row showing `builtin_killed` means
the slot was taken while the harness read, almost always by the harness's own u2 server
(see below), so it costs time, not a verdict. `u2` with no `builtin_killed` is a screen
that never reported idle, not a taken slot. Measured on
emulator-5554 (2026-09-22): with the slot held through staging, the old handover gave
the agent 0/5 in both forms; the fixed one gave 20/20 in both. Derives show the same
thing on themselves: the first `type` step starts u2, and every later built-in dump in
that derive is killed three times before u2 answers. One `--repeat 3` derive of
`orgzly-create-and-search` recorded `builtin 5 · builtin_killed 183 · u2 61`. Verdicts
are unaffected, but each dump costs about 3 s extra (TODO in `_dump_vh_raw`).

**Every staged launch starts from the launcher, then pins portrait with the app in
front** (QUA-2733, QUA-2734). Both staging paths end the same way. The live path runs
`normalize_app_env` (animation scales, permissions, a portrait pin), `device_setup` and the
flags, then `isolate_app_under_test`, then `session.launch_app`, then
`replay.repin_portrait_after_launch`, then the cold snapshot. Replay's `_reset` runs the
pin, `pm clear`, setup, the snapshot restore, the same isolation and the flags; the route's
`launch` step (or a `relaunch` that is its first step, QUA-2738) then relaunches and
re-pins through the same helper. Isolation force-stops every other benchmark app, runs
`am kill-all`, and sends HOME LAST, so the launcher is the task directly beneath the app
launched next. A crash, or a `back` from the app's root
screen, then lands on the home screen and never in another app. That matters because an
agent would happily go on testing the other app, and a derive writes it into truth:
`cal-search-event` recorded TrustLoop's sign-in screen as its post-crash screen because
`com.trustloop` was the task beneath. HOME must stay the final action before the launch
(`tests/test_isolation.py`). It is also why the pre-launch pin is not enough: with the
launcher on top, the next launch can restore the landscape the previous app was stopped in,
so the pin that counts is the one written after the app is up. The lifecycle paragraph
below has the mechanism. Hunt truth derivation's own staging (`scripts/derive_truth.py`'s
`derive_one`: the launch `check_setup` and the snapshot run on, and its one retry)
re-pins after each launch through the same helper, as derive_journey's `stage()` does
(QUA-2737). Neither of those two staging launches runs the isolation. Its per-check
passes go through `_reset`, and every hunt check opens with `launch`. No hunt check or
`check_setup` rotates, but that does not make the hunt path safe: the leak is
device-wide, so an earlier journey rotation case on the same emulator is enough.

## Journey mode (test-case runs)

`--mode journey`: one episode = one app + ONE test case + one VERSION (clean = no
defect on; seeded = exactly the case's `bugs:` on). Same brief in both. Two numbers,
never blended: COMPLETION (device oracle after the agent exits + the right verdict; a
blocked case = fail + the blocking bug named; truncation/no evidence = not completed)
and BUG FINDING (found/present over seeded episodes, false reports over all — every
report on a clean build is false — one F1 from the totals). Cases live in
`data/test-cases/<app>.yaml`: defects (kind functional|display, class, marker, symptoms) and
per case route + `check:` oracle + `bugs:` (≤1 functional). `class:` is the fault class from
the closed vocabulary `journey.DEFECT_CLASSES` — metadata `load_defects` never copies, so no
scorer sees it; `docs/defect-classes.md` defines each class, the rule for an ambiguous one
and the 2026-09 persistence retain list, and `scripts/mix_report.py` prints the corpus mix
by class against the plan's targets (whole corpus and per app). `scripts/derive_journey.py`
is the corpus gate (clean + seeded pass per case; display markers must be in the
screen diff). Its `--repeat N` runs each version N times from a fresh reset and demands
the identical outcome every time — no majority vote: any case whose defect is a forced
interleaving, a crash or a stuck-screen oracle must be derived with `--repeat` ≥ 3
before it enters the corpus, because one trial cannot measure a margin. An UNSTABLE
result (or a display marker seen in only k/N trials) is a `problems` entry and
`agrees: false` — the case leaves the corpus until the flip is understood. The reset
restores app data and shared storage and, since QUA-2781, sets the device CLOCK back to
the pin (below), so every trial starts at the same instant; before that a
time-of-day-dependent case (`TODO(fixture)` in `medtimer.yaml`) could flip for that reason
alone. A route that sits on a minute boundary can still differ, by that minute.

**A retry inside a trial is not silent** (QUA-2744). An INCONCLUSIVE pass is RETRIED by
`one_pass` — the replayer could not judge it, which is the replayer's problem and not the
case's — and until 2026-09-22 the truth row kept only the winning attempt, so a case that
needed two attempts every time read as a clean pass. That is exactly how the rotation leak
(QUA-2734) survived a day of derives. `one_pass` now returns the whole attempt log, every
trial entry in `passes` (trial 1) or `trials` (each) carries `attempts`, a retried one
also carries the discarded attempts' verdicts (`retries`), the trial's own line says
`[N attempts]` while the derive runs, and `main` closes with a `masked retries:` block.
`attempts` is written on every entry this deriver writes, `attempts: 1` included, so an
**absent** `attempts` means exactly one thing: the row predates QUA-2744. The 41 committed
rows are all of that kind and were deliberately NOT backfilled — re-deriving them is ~20 h
of the single emulator for a field that changes no verdict — so every reader must treat
the key as optional (`masked_retries` reads a row of either vintage). A retry does not
make a case DISAGREE and does not change the exit code: the trial WAS judged. It marks the
case as the first thing to re-derive when its verdict is questioned. Screens are kept for
the winning attempt only; `attempts: 2` says the row was written by attempt 2.

**The replayer's own error rate, measured** (2026-09-15/16, QUA-2707 — the error bar every
journey pass/fail is read against). The whole corpus (40 cases, 8 apps, both splits)
derived at `--repeat 3` from a fresh reset on one emulator: 240 passes, 80 version-checks,
**1 unstable = 1.25% of versions, one case in 40**. The lone flip was a held-out
case's seeded arm — an input-dispatch ANR in MainActivity at step 2,
ONE step in, on a trial that followed a 157 s pass of the same 14-step route whose every
other pass took 65-71 s. That is host load, not the defect: the case's only bug is a
DISPLAY bug on a summary screen the route has not reached at step 2. Re-derived at
`--repeat 5` it was 10/10 HOLDS at 65-69 s, so the case stayed in the corpus. Read a lone
CRASHED/ANR trial on a heavy app (AnkiDroid, a held-out app) as a re-derive candidate, not a finding,
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

`scripts/rescore_journey.py` re-scores saved episodes. It has no device, so every oracle
the harness reads off one after the agent exits (`journey.DEVICE_ORACLE_MODES`: db,
content, crash, anr, stuck) comes back from the saved `metrics.oracle.result`, and only
into the same mode. Until QUA-2793 the bridge knew db/content only, and every liveness
episode rescored True -> None. An episode whose outcome was never saved prints
`unrecoverable` and keeps its recorded COMPLETION (never rescored to None); its bug side
needs no device and is rescored like any other episode's (QUA-2807 — before, the whole
episode was skipped and a scorer fix never reached its bugs). The device
timezone is pinned by `run_device_setup` (`QGB_DEVICE_TIMEZONE`, default
America/Chicago), and so is the device CLOCK (below). `device_setup` fails LOUDLY: a `shell:` step that exits non-zero or
prints `run-as: exec failed` / `not found` / `No such file` / `Error:` / `sqlite3:`
raises `DeviceSetupError`, recorded as `staging_failed` → `env_failure`. It runs as root
only when it declares `root: true` (as the shell user otherwise, whatever an agent left
behind) and ALWAYS hands the device back unrooted, error path included
(`set_adb_root`, QUA-2743): `adb root` is device-wide and outlives the fixture, so one
root fixture used to give every later agent on that device a root adb shell. A journey
episode whose PRECONDITION is missing (`assert_precondition`: the route's first tap is
not on the screen the agent would be handed) records the same `staging_failed` and then
ENDS, before the agent launches (QUA-2743): the exclusion is unchanged, the agent is
never paid for an outcome every board discards (`cost_source: "not_launched"`, $0). Rows are
seeded into an app database with the host-side `sql:` step (`{package, db, statements
| file}` → `verify.device_oracle.apply_sql`: force-stop, `run-as cat` pull, one
transaction under the device zone, write back, verify) — never an on-device
`sqlite3`, which Google Play images lack; four fixtures seeded nothing that way for
weeks and `medtimer-skip-logged-dose` was charged to agents for it (2026-09-14).
A journey-only defect is a `bugs:` + `tasks:` entry in the spec with
NO exploration feature, so hunt mode never activates it. Journey mode fetches the JOURNEY
build — the test-case file's `apk:` block (`journey/<app>-buggy.apk` on HF, cache slot
`journey/`); dist/ still wins locally. The hunt build does not carry the journey-only
patches, so no other mode may score one (QUA-2739). Guided mode (the CLI default) installs
the hunt build and plans only `bugs.guided_tasks`: a task whose bug is not a `state: broken`
feature is never planned. It stays in the spec only because `build_app.py` wants a task per
patched bug. `--mode all` stages ONE APK per app, the hunt one, so `run` and `preflight`
refuse it for any app whose journey `apk:` block names different bytes from its hunt block
(`preflight.journey_build_differs`; a local build serves every mode, and identical blocks
are one build). Run `--mode journey` on its own. Installing the journey build per unit was
rejected: it would thread a second APK per app through lane staging, the per-trial
reinstall, plan.json's one-APK-per-app fingerprint and `--resume`, and swap two builds of
one package on a device mid-run. That is a lot of engine for a mode that measures nothing
the separate runs do not, since the board prints each kind as its own table.
`scripts/publish_apk.py <app> --kind journey` moves the file and the hash together — DRY
RUN by default, `--write` edits the block,
`--upload` (owner only: needs `--write`, `HF_TOKEN` and `--yes`) does the upload. Never
one without the other: `fetch_seeded_apk` sha256-checks every download, so a hash
without an upload and an upload without a hash break a fresh clone identically.
**Pins** (QUA-2770, `apk_pins.py`, `data/apk-pins.json`, NOT a `corpus_version` input):
every upload overwrites its path, so `fetch_seeded_apk` downloads a pinned sha256 from
the dataset revision that holds it (HEAD only when unpinned). `--upload` writes the pin
from the commit it returns, once `get_paths_info` confirms that revision serves the
bytes. `--write` alone marks the sha256 `unpublished`, the machine-checkable form of the
YAML's `NOT YET PUBLISHED` notes. `tests/test_apk_pins.py` fails on any committed block
that is neither pinned nor marked. Commit `apk-pins.json` with the block and again after
the upload. For a pinned build the upload/merge back-to-back constraint is gone. It
remains only for code that predates the manifest (`scripts/apk_pins.py fetch <sha> --out
<old>/dist/<app>/buggy.apk` serves such a checkout). `scripts/apk_pins.py backfill` is
the read-only history walk, and `check --remote` confirms every pin. `--archive --apk
<old build>` publishes a superseded build to `archive/…-<sha12>.apk` and pins it without
touching a block. Never squash the dataset's history: every pin would dangle. A
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
display text (`docs/journey-oracle-audit.md` holds the per-case audit of the public apps:
a row for every case in the corpus — the 2026-09-14 audit plus the 21 cases added since,
17 of them by epic QUA-2723 — and the 12 pruned cases' rows kept and marked as pruned; the
held-out apps' rows live with the split). `journey_verdict` scores it:
`completed = right verdict ∧
every witness in the text the DEVICE answered with` (token-boundary `_word`, device
RESULTS only — a typed argument never witnesses itself; the device text is space-folded
exactly like the needle, U+202F/U+00A0 and whitespace runs, in `_device_texts` /
`_observation_texts` — before QUA-2788 only the needle was, and orgzly's
`Getting Started with Orgzly  •  Notes` breadcrumb could never witness), an episode with no device text
at all stays unscored (None, "no device text to witness"), never False; in `db:`/
`content:` mode a declared `evidence:` is required on top of the oracle under the same
rule and a violated oracle dominates; expected-FAIL arms never consult it; a
`present:`/`absent:` case WITHOUT `evidence:` stays on the old unscored stopgap (none is
left in the corpus). `metrics.witness` records required/seen/missing/scored, and
`derive_journey.py` refuses a witness missing from the clean route's final screen or
sitting inside a display bug's measured texts. `verify.device_oracle.query_db` evaluates
oracle SQL under the DEVICE's zone (`getprop persist.sys.timezone`, cached per serial,
else the pin) in a child interpreter exactly as `apply_sql` does — `'localtime'` in an
oracle is the device's day, never the host's — and at the device's instant: `'now'` is the
device's clock, not the host's (QUA-2781).
`scripts/lint_journey_cases.py` is the device-free gate on that text: a witness or brief
that carries a seeded defect's marker/symptom, or a case with no `check.expect`, fails it.
A display SIDE bug should carry `reference: {kind: stated|entered|cross-check, note}`
beside it in `bugs:`, which records how the brief lets the agent know the shown value is
wrong; a side bug the brief gives no handle on measures curiosity, not QA. A MISSING
`reference:` is an error on a public case, but only a WARNING (exit 0) on a case marked
`witness_before_action:` (deferred to the ticket that re-authors it) and on every held-out
case (audit pending with the corpus owner) — `rule_reference`. A `reference:` that is
present but malformed (not a mapping, a kind outside the three, no `note:`) is an error
everywhere. A display marker under the 2-character evidence floor, which no report can
quote, fails it too (QUA-2783, docs/journey-oracle-audit.md "Side-bug references").

**The device clock is pinned, and a dirty device is refused at episode start** (QUA-2781).
Time was an unpinned input to the truth: Fossify's day header and next-full-hour default,
MedTimer's today-only Overview and tasks.org's "Due today" render off the device clock, so a
row derived on one day carried that day's strings and a board run a day later met different
screens. `run_device_setup` (every staging path: the live episode, `replay._reset` — which
now runs it even with no `device_setup:` — and both derive scripts) calls
`episode_runner.pin_device_clock`: `settings put global auto_time 0`, then `cmd alarm
set-time <ms>` to `QGB_DEVICE_CLOCK` (ISO 8601; naive = the device zone; default
`2026-09-16T10:00:00`, a Wednesday, clear of midnight and past MedTimer's 08:00 edge).
set-time needs no root (measured on the android-35 google_apis image; `date` as the shell
user is "Operation not permitted"); the fallback is `date @<epoch>` under `adb root`, handed
back unrooted. Because the clock goes BACK on every reset, the pin also clears what the
crash checks read through device-time windows (`logcat -b main,system,crash,events -c`, `am
clear-exit-info`): uncleared, the previous pass's 10:01 crash sits inside the next pass's
10:00:30 window. A consequence worth knowing: an agent can no longer read the previous
episode's crash out of logcat. Nor, since QUA-2790, out of the two stores the pin's clear
did not reach (`episode_runner.clear_crash_history`): `/data/anr` (a QUA-2785 agent read
an earlier run's thread dump there through `su`) is emptied with the harness's own `su 0`
— adbd is never restarted and stays unrooted — or, on an image without `su`, under `adb
root` handed back; the dropbox (`dumpsys dropbox --print` gives the SHELL user every
earlier `data_app_crash`/`data_app_anr` stack) is emptied with no root by setting
`dropbox_max_files` to 0, which makes the service trim its own files and index, then
deleting the setting (deleting its files as root leaves every entry listed at 0 bytes).
Measured on the android-35 google_apis image: the shell user can list and stat
`/data/anr` but not read or delete a trace. The host's sqlite does not know the pin, so every `'now'`
literal in fixture (`apply_sql`) and oracle (`query_db`) SQL is replaced by the DEVICE's UTC
time (`device_oracle.at_device_now`) — without it `date('now','localtime')` was the host's
day, a week off the device's. The pin is in every episode's `provenance.device_clock` and
every row `derive_journey` writes (`device_clock`; absent = derived before the pin).
Then `run_episode` asserts the episode-start invariant (`preflight.device_state_violations`,
read-only): `user_rotation` and `accelerometer_rotation` 0, the adb shell not root, no
uiautomator2 server, the device clock within 5 min of the pin, no `/data/anr` trace this
staging did not write (stamped before the pin, or after the device clock — the clock goes
back every reset, so a previous PINNED episode's trace reads as the future; one stamped
inside this staging's own seconds cannot be told apart and is the clear's job), and — between isolation's
HOME and the launch — the launcher in front; again at the agent hand-off without the
launcher. Staging resets each of those first (a leftover harness u2 server is stopped before
isolation too), so the check catches a reset that did NOT take — the way the rotation, root
and UiAutomation leaks (QUA-2734/2743/2741) were each found only by a contaminated board. A
violation is `staging_failed` (`device not clean at episode start: user_rotation=1 …`) →
`env_failure`, and the agent is never launched (`tests/test_device_clock.py`). The re-derive
under the pin (2026-09-23, emulator-5554) re-derived the 17 rows whose diff, side texts or
witnesses carry a clock-shaped string (`--repeat 3`, 5 where the row had 5), verified the
other 24 agree under the pin in one trial, and re-derived the two of those whose one-trial
row differed (`anki-study-first-card`, reproduced exactly at `--repeat 3`;
`orgzly-create-priority-note`, unscored scroll noise). All 41 agree under the pin.

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

**A missing split is never silent, and a journey board requires it** (`journey.heldout_gap`
/ `heldout_required` / `NO_HELDOUT_NOTE`, `cli._gate_heldout`, `preflight.check_heldout`).
A journey board with no split produces public rows and no held-out block, which reads
exactly like a complete board while answering a strictly weaker question. Since QUA-2782
`run --mode journey|all` REFUSES to start without the split (before any probe) and
`preflight` FAILS a journey config without one; `--allow-no-heldout` /
`allow_no_heldout: true` / `QGB_ALLOW_NO_HELDOUT=1` opts out, and the plan panel then reads
`held-out: NONE — opted out`, preflight downgrades to a WARNING naming the opt-out, and the
board keeps its `NO_HELDOUT_NOTE` line (`show` too). The opt-out covers "none configured"
only — a configured dir that is missing or empty refuses anyway. `--require-heldout`
(`QGB_REQUIRE_HELDOUT`) is now the default spelled out; with the opt-out it is refused as a
contradiction. The opt-out travels as the env var (`cli._apply_heldout_optout`, like
`_apply_heldout_dir`), so the gate, preflight and the panel read one place. Note the trap:
`corpus.heldout_dir()` reads the ENV VAR only — the documented `heldout/` beside the repo
root is `scripts/holdout.py`'s default, not a harness fallback, so a split synced there and
not exported is invisible to a board. `holdout.py sync [--from s3://…|DIR]` fetches,
verifies and prints the `export QGB_HELDOUT_DIR=…` line for that reason.

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
bracket on paper only. **The board ranks on clean-run integrity** (QUA-2780,
`journey.ranking_key`: `(heldout, −integrity@200, −catch, −F1)`), because F1 is measured
at a 50% bug prior and a production suite runs at a few percent, where false alarms
dominate. Integrity is compared through the UNROUNDED false-alarm rate — same order, but
the stored `clean_integrity_200` rounds every rate above ~5% to 0 and would tie every row
measured today. F1 and completion stay displayed; nothing is blended. The table also
carries `$/ep` (mean `cost_usd` over PRICED episodes, `+N unpriced` beside it, `—` and the
count when none is priced — never $0.00) and `min/ep` (median agent `wall_time_sec`), as
`cost_per_episode` / `cost_unpriced` / `minutes_per_episode` on every summary row;
`--projection` also prints the prior-weighted error count (false alarms + misses), which is
a printed line, not a ranking key.

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
alive when the check names a death. It also refuses a death nobody can SEE (QUA-2742,
`derive_journey.invisible_death`): a FAIL case whose seeded arm dies while its clean/seeded
screen diff is empty, on any trial. `cal-complete-task` once wrote its row and then died in
a secondary activity; Android restarted the process on the list beneath, which showed the
task completed, and a tester's correct PASS was charged. A crash in a secondary activity
must fault BEFORE the state it corrupts, and the route must read that state back as text
(a struck-through list row is paint, not text). A positive "must crash" expectation was rejected on
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
ids. `adb_meter.deny_reason` now answers `FAIL` at the socket (never relayed; counted as
`metered_denied` and not in `metered_total`) for `run-as`, `/data/data|user*/`,
`/data/local/tmp/qgb*`, `qgb_flags`/`.qgb`, the `backup:` service and, since QUA-2790,
`su`. A denied request is still charged in `interactions.json` (the budget) exactly what
it would cost relayed (`su 0 id` = one `other`), so refusing never makes a probe cheaper;
"not charged as a step" in older notes was wrong about that ledger. `su` because the
android-35 `google_apis` images ship `/system/xbin/su`: `adb shell su 0 …` is a root
shell with adbd unrooted, which the invariant's `id -u` never saw, and a root shell
needs none of the literal paths (`su 0 sh -c 'cd /data/da*/…; cat q*'`). The rule
matches `su` as a whole shell word anywhere in the command (after `;`/`&&`/`|`, inside
`sh -c '…'`, quoted, `s\u`, `/system/xbin/su`) and not inside a longer word, a dotted
name or a directory (`dumpsys`, `summary`, `/sdcard/results`, `com.example.su`); a bare
`su` used as data (`grep su`) is refused too. Those rules read request TEXT, so a command
fed on stdin (`echo 'su 0 id' | adb shell`) or read from a pushed script once escaped all
of them. `adb_meter._hidden_payload` (QUA-2794) denies the common hidden-payload shapes:
an interactive/empty-command `shell:`/`shell,v2`
request is denied `stdin shell` (that is what `echo … | adb shell` and a bare `adb shell`
open); a shell interpreter reading its script from stdin or a FILE (`sh`, `sh -s`,
`bash`, `toybox sh`, `sh /sdcard/x.sh`, `source`/`.`, `xargs sh`) is denied `script shell`
— the ONE allowed form is `sh -c '<text>'`, and that text is then scanned recursively by
every rule (a `sh`/`run-as`/`su`/pushed-file nested inside it is still caught); and
EXECUTING a file out of a world-writable dir (`/sdcard/x`, a chmod'd `/data/local/tmp/x`,
`/storage/emulated/…`) is denied `world-writable exec` — reading or writing such a path
(a screencap, a `uiautomator dump`) is untouched, only invoking one as the command word
is. A denied hidden-payload request is charged in `interactions.json` exactly as it would
cost relayed (`classify_adb_all`, one `other` for an empty shell) and adds to
`metered_denied`, never `metered_total` — the same accounting as every other deny rule.
**These text rules raise the cost of a root shell; they do not close it on the `su`-carrying
image.** A denylist over shell text cannot be complete, and the epic QUA-2773 final review
got past it several ways: shell expansion (`a=s; ${a}u 0 id`, a glob such as
`/system/xbin/s? 0 id`), command substitution (`sh -c "$(cat /sdcard/p.sh)"`, `eval "$(cat
…)"`), interpreters and exec forms the rules do not name (`find … -exec sh {} \;`, `awk -f`,
`cd /sdcard; ./x`), and skipping the meter altogether: the agent's adb reaches the meter only
through its environment, so `adb -P 5037`, `adb -H <host>` or `ANDROID_ADB_SERVER_PORT`
points it at the real adb server, where nothing is metered, charged or denied. Read the rules
as friction plus an audit trail — a request that goes through the meter is charged in
`interactions.json` and a deny is counted in `metered_denied` — and
`provenance.adbd_at_end` (below) as an after-the-fact check on adbd only, not as proof that a
bare-arm agent never had a root shell or never read app state. The durable fix for root is the
su-less image (below).
**Hardening the meter, and the nonce backstop** (QUA-2804). The QUA-2773 review's shell
bypasses are now refused at the meter, each because it hides which program runs from the
text rules: a command word built by parameter expansion (`a=s; ${a}u 0 id` → `dynamic
command`) or a glob (`/system/xbin/s?` → `glob command`); command substitution, whose
OUTPUT is the command the meter never reads (`sh -c "$(cat …)"`, `eval "$(…)"`, backticks →
`command substitution` — the old `_SEG_SPLIT` scanned the substitution's CONTENTS as if
they were the command, reading `cat p.sh` as benign while its output ran); a non-shell
interpreter that runs a program file (`awk -f`, `python …` → `program interpreter`);
`find … -exec`; and a relative command word after a `cd` into a world-writable dir
(`cd /sdcard; ./x` → `world-writable exec`). All are COMMAND-WORD checks — an argument may
still expand or glob (`input tap $1 $2`, `cat /data/anr/*`), which is ordinary QA.
**The port/host escape is caught, not at the meter (it never reaches it) but as a hard
`adb_server_bypass` contamination hit** read off the agent's own command text: an `adb -P`,
`-H`, `-L` flag or an `ANDROID_ADB_SERVER_PORT=`/`_ADDRESS=`/`_HOST=`/`ADB_SERVER_SOCKET=`
assignment re-selects the server around the meter. `run_episode` points the agent's adb at
the meter in `agent_env` alone and never exposes the real server port there; the real
server is only reachable because localhost:5037 cannot be hidden from a shell, which is why
this is detected rather than prevented. `host-serial:`/`host-transport-id:`-scoped forward
services are refused with the bare `host:forward:` forms (`_canonical_host`), and
`host-local:`/`host-usb:` (`adb -e/-d get-state`) are ordinary transport reads, no longer a
false positive.
**The per-episode NONCE is the backstop for anything still slipping through**: a random
value (`episode_runner.write_bug_flags`) written into `qgb_flags.txt` (a leading
`#`-comment line, so the shim's id set carries it harmlessly and `on()` never queries it)
and the `.qgb` marker (`files/.qgb/nonce`), reusing the CANARY mechanism. Reaching it means
reading the app's private sandbox — the very access the meter refuses — so if it surfaces in
any agent-visible tool result OR any file the agent wrote, `contamination.scan` voids the
episode (`flag_nonce`, a hard hit; unlike the canary it is secret, so the INPUT is matched
too). It is never persisted or published: the hit detail is a fixed string, `_provenance`
and the metrics dict name only their own keys, and `rescore_journey` preserves a `flag_nonce`
verdict rather than re-checking a nonce it cannot see (no saved run carries the reason, so
every historical rescore is byte-identical).
**Typed-text trade-off** (QUA-2804): the quoted payload of a top-level `input [source] text`
is blanked before matching (`_blank_typed_text`), so `input text 'Meeting (source review)'`
and `"…; reboot later"` are no longer split into `source`/`reboot`. The exemption is narrow —
only a single NON-expanding quoted payload at the top level of the request, and only the span
it covers; everything after it is still scanned, an UNQUOTED payload is not exempt (the device
shell really does split it), and a payload NESTED inside `sh -c "…"` is not un-nested (its own
quotes are gone by the time the scanner sees it, so metacharacters in it are still refused —
rephrase without the wrapper). The agent must quote for the DEVICE (`adb shell "input text
'…'"`) so the quotes reach both the device and the meter; a bare `adb shell input text a b`
loses them at the wire and is not the way to type a literal string. Saved-episode replay
(`tests/adb_replay.py`, `corpus.legitimate_newly_denied`) over the fixture and the 1666
requests in the developer's runs newly denies zero legitimate request.
**adbd privilege services** (QUA-2795). `adb root` is not a shell request: after the
transport handover the client sends the bare device service `root:`, which no text rule
read and `classify` calls plumbing, so a QUA-2784 re-run agent rooted adbd and read
`uid=0` from a later `adb shell id` (run 20260923-174028-afd5). `adb_meter.
_privileged_service` now denies `root:`, `unroot:`, `reboot:[arg]`, `tcpip:`, `usb:`,
`remount:`, `disable-verity:`, `enable-verity:`, `sideload[-host]:` and `host:kill`
(kill-server stops the upstream server every lane shares); `remount`/`*-verity` arrive
as `shell,v2,raw:<verb>` and are caught with `reboot`, `svc power reboot|shutdown` and
`setprop service.adb.*|persist.adb.*|ctl.*|sys.powerctl|sys.usb.*` as command words in
`_scan_body`. Wire shapes measured with adb 36.0.2; a device service behind a
`host-serial:` prefix is refused by the server itself ("unknown host service"). The
property rule is not paranoia: the SHELL user may `setprop service.adb.root 1`, and
`adb usb` then restarts adbd as uid 0 with no `root:` at all (measured), and `adb unroot`
on an unrooted adbd leaves the property at 1. Accounting is unchanged: a denied `root:`
is charged 0 in `interactions.json`, as a relayed one always was. After the agent exits,
`episode_runner.check_adbd_after_agent` reads `id -u` and `service.adb.root` over the
harness's own adb, resets a primed property, and records `provenance.adbd_at_end`
(`rooted`/`root_primed` true = a privilege change got past the meter). Replaying every
saved agent adb request (1630 across 186 transcripts, both arms) newly denies exactly
one: that `adb root`.
**Socket-forward, JDWP and raw device sockets** (QUA-2797). The same class of blind spot,
found by the QUA-2795 worker: `adb forward`, `adb reverse` and `adb jdwp` are not shell
requests. After the transport handover the client asks the server (or adbd) to OPEN a
socket to a device endpoint, and the payload then flows on a pipe the meter never parses.
`host:forward:tcp:N;jdwp:<pid>` hands a JDWP debugger port straight to the app's process
on the corpus's debuggable builds — a path to app state no answer-key path rule, `su`
rule or `run-as` rule ever sees (verified live: the pre-fix meter relayed it with
`metered_denied: 0` and a JDWP handshake succeeded). `adb_meter._forward_or_socket_service`
closes it as an ALLOWLIST, not a denylist, and it is layered at the DEVICE-SERVICE
namespace rather than by enumerating dangers: the only non-`host:` services an agent may
open are the metered exec channels (`shell:`/`shell,v2`/`exec:`, plus `abb:`/`abb_exec:`
unchanged from before), a file transfer (`sync:`) and `framebuffer:`; ANY other bare
device service is refused — `jdwp`, `track-jdwp`, `track-app`, `reverse:*` and every raw
socket (`localabstract:`/`localreserved:`/`localfilesystem:`/`dev:`/`tcp:`), including one
a future adb adds. `host:`-prefixed services keep the host default of relay-and-classify
(transport selection, `host:devices`, feature/version negotiation all live there), so the
forward CONTROL services that are host-prefixed are named explicitly:
`host:forward:`, `host:killforward:`, `host:killforward-all`, `host:list-forward`,
`host:track-devices`. A device service behind a `host-serial:`/`host-transport-id:` prefix
is refused by the server itself ("unknown host service", measured adb 36.0.2), so only the
bare forms need a rule. Accounting is unchanged: a denied forward/jdwp is a host/plumbing
or unclassified service that always cost 0 in `interactions.json`, so it adds to
`metered_denied` and 0 to `metered_total` — the same ledger as every other deny rule.
uiautomator2 and adbutils reach the on-device server with `adb forward` too, but over the
harness's OWN adb (the upstream server env), never the agent's meter port, which
`run_episode` sets in `agent_env` alone; a DevLoop-MCP server's forwards are its own adb,
also unmetered. Replaying every saved agent adb request (1630, both arms) newly denies
zero — the agents drove QA with shell/exec/sync only.
An image without `su` (QUA-2790's option (b)) is still the durable root fix, deferred as a
QUA-2794 follow-up because it changes the AVD and forces a corpus re-derive that QUA-2786
needs held fixed. The meter sits in front of the
agent's own adb in BOTH arms by environment only (its adb env points at the meter, and an
agent that overrides the server port or host goes around it, above); the harness's own
privileged steps (`set_adb_root`, `clear_crash_history`'s `su 0`) go straight to the
upstream server and are never metered; an MCP server's tools
run over the server's own adb — if a server exposes a shell tool, `QGB_DISALLOWED_TOOLS`
is the only lever. Authoring: `--repeat >= 3` for every crash/anr/stuck case; a
`stuck:`+`present:` oracle must name text that survives the probe tap; forced
interleavings: an operator that changes an ORDERING is seedable (swap two awaits,
post-vs-run, commit-before-write), one that merely WIDENS a window (a sleep, a slower
loop) is not — it measures the device, not the defect; prefer patterns where Android
itself is the oracle (a fragment transaction after `onSaveInstanceState` throws
`IllegalStateException`; a view touched off the main thread throws
`CalledFromWrongThreadException`), but only once the order that makes Android CHECK is
forced as well, or the detection is itself the race. The wrong-thread check is the
measured trap. It is `ViewRootImpl.checkThread()`, reached only through `requestLayout()`'s
walk up the tree, and that walk stops at the first ancestor that already owes a layout.
So a bare "run inline instead of posting" touched a view off the main thread on every
seeded trial, yet crashed in only 6 of 10 (`cal-search-event`, QUA-2733's `--repeat 5`
derives; the committed 3/3 had been a ~0.2-probability draw). QUA-2726's repair keeps the
operator and forces the order: the background thread hands off to the main looper, the
hand-off re-posts itself until the window owes no layout pass, and then parks the main
thread on a latch while the update runs inline. That is two latches and a main-looper
message, no sleep and no timeout, and it measured 8/8 with the marker on every seeded
trial. The mechanism is in the `search-results-off-main-thread` patch comment in
`data/benchmarks/fossify-calendar.yaml`. Copy that shape for any later ordering defect:
the bare run-inline gives the 60% case back. The corpus is derived on android-35 images
and its verdicts assume one. Nothing pins the API level, and at least one seeded arm needs
it: `task-complete-crash` (fossify-calendar) is S+ PendingIntent mutability, so on an
API ≤ 30 image it would silently HOLD.

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
(`echo_haystack`: case name, steps, expected outcome, and every route value under
`ECHO_ROUTE_KEYS`: `type:`/`append:` text, `tap:`/`long_press:` anchors and a scoped
tap's `row:` label) — only
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
platform wording) in `GUESSERS`; both earned 0/39 · 0, and `honest` (38/39 · 24) and
`honest-text` (27/39 · 15) were unchanged by the whole change. What the gate prints today
(2026-09-24, `bugs / done`): public corpus alone, 39 seeded episodes and 40 seeded defects,
every guesser 0/40 · 0, `honest` 40/40 · 28 and `honest-text` 37/40 · 25; with the held-out
split exported, 49 seeded episodes and 50 defects, every guesser 0/50 · 0, `honest` 50/50 ·
32 and `honest-text` 47/50 · 29 — through the findings file and the report tool alike.
The sixth guesser, `type-then-quote` (QUA-2805), types every string the two echo guessers
quote into DevLoop's text-entry tools and quotes the acknowledgement back; with the reply
still counted as device evidence it earned 19/40 · 19 on the public corpus (and one episode
through the report tool), and with the `echo` rule it earns 0.

**An honest `expected:` that repeats the brief earns nothing, by design** (QUA-2796). On
`cal-open-task-from-list~seeded` three of four DevLoop-arm re-runs (20260923-174028-afd5)
reported the dead row truly (the fault fired every time) with `observed: Laundry` (on both
arms) and the brief's own "Mark completed" under `expected`. That string is removed by the
defect but ECHOABLE, so `absence_texts` drops it: letting it in credits `brief-echo` the
same bug (measured). With no quote route left, credit rests on the symptom vocabulary, and
the list did not carry the wordings they used ("does nothing", "list stays on screen",
"never opens", logcat's "EventActivity"); adding them credited all three
(`rescore_journey.py --dry-run`: those 3 episodes False → True, nothing else moved;
adversary output byte-identical). Six public cases share the precondition, where the brief's
outcome string is the natural `expected` and is echoable: `anki-open-card-from-browser`,
`cal-switch-back-to-list`, `cal-open-task-from-list`, `medtimer-check-stock`,
`orgzly-open-note-from-notebook` and `orgzly-new-note-survives-rotation` (no other absence or
blocking text at all, so prose is its only route); held-out: none. In the two DevLoop-arm
runs only the calendar case lost credit to it. When authoring a functional defect for such a case, write the
symptom list from how testers describe the misbehaviour, dead-tap wordings included.

**A crash's symptom list names its visible EFFECT, not only the word "crash"** (QUA-2802).
On android-35 a foreground crash often shows no "keeps stopping" dialog: the agent lands on
the launcher, or (AnkiDroid) on the app's root screen after a process restart, and a
background-thread crash shows nothing but a write that never landed. A tester who does not
read logcat describes exactly that, and on the QUA-2786 board 21 honest GPT-6 Astra crash
reports ("MedTimer disappeared and the Android home screen appeared", "returned to the deck
list", "No items found.") plus the 2 dead-tap reports on `cal-open-task-from-list` matched
nothing, because every crash list was crash vocabulary and `_word` is token-exact
("disappear" ≠ "disappeared"). Every crash defect now also carries the effect wordings
(`home screen`, `launcher`, `closed the app`, `app closed`, `exited the app`, `disappear(ed)`;
AnkiDroid's root-restart phrases; for `all-day-save-crash` the app's own empty states;
`still offers mark completed` for `task-complete-crash`), and each tense is listed rather than
stemmed — no scorer change. Brief outcomes NEGATED stay refused (`not saved`, `not listed`,
`not marked completed`: writable blind). `rescore_journey.py --dry-run`: runs
20260924-022244-0252 / 20260924-043254-1e0b public catch 27/40 → 39/40 and 28/40 → 39/40,
exactly the 23 reports the comparison doc §6 hand-credits and no other report's match; the
Fable runs byte-identical. `journey_adversary_check.py` output unchanged (every guesser
0 bugs · 0 done; `symptom-spray`, which draws on the same lists, still pays 41/41 · 51/51).
When authoring a crash defect, list the effect a tester would see at the fault step, in
every tense they would write it.

**The adversary the roster cannot hold, and what is asserted about it instead.**
`symptom-spray` writes the corpus's own symptom vocabulary as prose with nothing quoted
and earns every seeded defect (40/40 public; 50/50 with the split). That is not a hole to
close: prose is the ONLY report a functional defect with no string to quote ever has (3 of
the 40 seeded defects have nothing quotable — 3 of 50 with the split — and the script
prints them), so a matcher that refused it would refuse the honest report with it. It
therefore lives in `PRICED`, not `GUESSERS`, and the gate asserts the PRICE — it pays a
false report on every clean episode (41/41 public, 51/51 with the split: 100%), because
nothing is active on a clean build. Recall that stops costing a dirty night is the
regression that catches. This is also why `_no_symptom_leaks_into_the_filler_prose` is
scoped to the two FILLER constants
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
function. **Neither pre-launch pin carries the launch** (QUA-2734). Both run before
`isolate_app_under_test`'s HOME, so the launcher is on top, and on our android-35
emulators a pin written there does not survive the next launch. After an app is stopped
in landscape, the launcher keeps that rotation while reading `user_rotation` 0, and the
next app it launches comes up landscape. QUA-2731 measured this on three apps. The
mechanism is Android's `DisplayRotationReversionController`. The launcher requests
NOSENSOR, so the controller saves the locked rotation when the launcher takes the top,
and `revertOverride` writes it back when the next app replaces it. RotationLockHistory
in `dumpsys window displays` names each writer; the helper's docstring has the details.
On the replay path it was masked: the landscape attempt went INCONCLUSIVE and the retry,
pinned with the app in front, held. So `replay.repin_portrait_after_launch` pins AGAIN once the
app is in front, then settles, on both paths: the route's `launch` step (`replay._launch`,
shared by `run_steps` and derive_journey's executor), derive's `stage()` launch,
derive_truth's staging launch and its `check_setup` retry (QUA-2737), and
`run_episode` after `session.launch_app`. `relaunch` (process death) does not re-pin
mid-route: the app comes back in whatever orientation the route left. As a route's FIRST
step it does, in both executors (QUA-2738): the route has left nothing yet, only the
previous pass's leak, and the hunt brief lets a repro start from `relaunch`. `tests/test_repin_after_launch.py`
plays the platform behaviour at the adb seam. Both arms can rotate and both are charged ONE step: the bare agent's `settings put system
user_rotation` is not on `adb_meter.deny_reason`'s list and classifies as `other`; on the
MCP arm the tool is `mobile_set_orientation` (`mobile_get_orientation` is a read and is
free), which the tool table charges one `other` explicitly (QUA-2775) — one interaction
either way. Only orientation: dark mode,
locale and font scale are NOT in the grammar. The device-free gate is
`lint_journey_cases.py`'s `route` rule — it checks every `check.steps` entry against
`submission.ACTIONS`, because `truth._steps` parses trusted YAML permissively and
`replay.run_steps` only discovers a typo'd verb on a device, as an INCONCLUSIVE pass that
reads like a flaky case.

**A route can scope a tap to one list row** (2026-09-18, QUA-2735;
`submission.route_item`/`ROW_VERBS`, `Step.row`, `replay._candidates`).
`{tap: Reminded, row: "Ibuprofen (4)"}` keeps only the matches whose OWN bounds — the
element the gesture lands in the centre of — overlap vertically with an element labelled
EXACTLY `Ibuprofen (4)`; no such row, or no match inside it, is an unresolved anchor
(INCONCLUSIVE), never a tap on another row. Own bounds, not the clickable ancestor's
(QUA-2739): where the nearest clickable is a container spanning several rows, every row's
control overlapped every band through it and the tie-break tapped the first row. The
price is that a control must share a band with its row's label; one drawn wholly above
or below it resolves nothing, which is the honest direction. It exists for controls
labelled by STATE rather than by item: MedTimer's per-event status icon reads "Reminded"
on every raised reminder and sits beside its card as a sibling node, so nothing in the
label or the tree says which medicine it belongs to. Identical labels fall to the
tie-break (smallest container, then document order), i.e. to whichever row SORTS first —
and once Aspirin's 8:00 AM reminder was raised it sorted above the Ibuprofen rows the
fixture stamps at staging time, so `medtimer-take-dose-then-medicine-list` answered
Aspirin and its clean
arm failed whenever a derive was staged after 08:00 (QUA-2731). HARNESS-ONLY, like `db:`:
`truth._steps` and the lint's `route` rule read it through the one `route_item`, while an
agent's findings still accept single-key steps only (no agent brief or BRIEF_VERSION
change). Both step loops pass it — `replay.run_steps` and `derive_journey.run_with_dumps`
— so the corpus gate derives exactly what episode replay runs. The hunt spec's
`event_take`/`dose_stock` checks answer the same reminder and carry the same scope
(QUA-2736); `tests/test_liveness_oracles.py` refuses any medtimer route, hunt or
journey, that taps a bare `Reminded`.

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
are not directly comparable to a v2 number.** (v2 was then withdrawn and v1's text
restored.) **v3** (QUA-2777) adds one sentence to the JOURNEY brief's MCP-arm note only
(`tooling_note(..., report_of_record=True)`): `findings.yaml` is the report of record and
a structured result tool the server offers is optional and does not replace it —
DevLoop's server instructions end every run with its own `mobile_report_result`, a second
prompt authority with a conflicting completion step. The bare arm's note and the whole
hunt brief are still v1's byte-for-byte; the stamp is run-wide, so boards print
the brief version under each block and flag a row that mixes them
(`journey.MIXED_BRIEF_NOTE`). The scorer also reads that tool as the fourth,
lowest-precedence journey report source (`report_source: report_tool`; BLOCKED = fail;
never device evidence) — docs/scoring.md has the precedence table, and
`journey_adversary_check.py` runs every guesser through both channels. Keep the note agent-neutral and
app-neutral — anything app-specific there is a hint, anything agent-specific makes the
arms measure different things (`tests/test_brief.py` pins both, and pins that no adb
command the note names is on `adb_meter.deny_reason`'s list).

**A cost of `$0.00` must never be printable for an episode nobody measured**
(`pricing.usage_metrics` — the single builder of the cost/token block six scorers used
to inline). `cost_source` is `reported` (the agent's own `total_cost_usd`),
`estimated` (measured tokens × `PRICING`), `unpriced` (real tokens, model not in the
table), `unavailable` (no usage in the transcript at all) or `not_launched` (the
harness ended the episode before the agent: a known $0); `unpriced`/`unavailable` carry
`cost_usd: None` and `total_tokens: None`, and the run footer names the count rather
than folding them into the total as zeros. The bug this replaced: claude-code's
cumulative `result` event is written on a CLEAN exit, and a budget-truncated episode
never gets there — the hook drops the sentinel and the process group is SIGKILLed — so
`token_usage()` summed nothing and priced it as "estimated". `token_usage()` now falls back to the
per-REQUEST usage on `assistant` events, **deduped by `message.id`**: the CLI emits one
event per content block, so 44 requests arrive as 86 events carrying each request's
usage two or three times and a raw sum roughly doubles the bill. Summing per-request
usage is right for billing even though the prefix is resent every turn — each request
is charged for its own full input, cache reads at the cache rate. Re-read against the
smoke run: `$0.00` → **$1.12 and $1.29**, 2.9M and 3.5M tokens.
**Codex has the same gap** (QUA-2803; this file used to say codex was immune).
`codex exec` runs the whole episode as ONE turn and writes ONE `turn.completed` with
the episode's usage, at the END. A truncated or killed episode never gets there, and
its `item.*` events carry no usage. Run 20260924-043254-1e0b's `contacts-favorite~clean`
(41/40 steps) published `usage_source: none` and printed as `+1 unpriced`. That biases
a codex `$/ep` low by exactly the episodes that ran to the cap. Measured on codex-cli
0.156.1 against a mock Responses backend (no model spend): a turn cut short by SIGTERM
(what `base.run` sends), SIGINT or SIGKILL writes no `turn.completed`, so a graceful
stop recovers nothing. The session rollout (`$CODEX_HOME/sessions/…/rollout-*.jsonl`)
keeps a cumulative `token_count` after every completed response and survives SIGKILL,
but `--ephemeral` discarded it. So the adapter no longer passes `--ephemeral`
(stdout is unchanged). After the agent exits, when the transcript has no
`turn.completed`, `CodexCliAdapter.with_state_usage` appends one
`qgb.codex_state_usage` line built from the rollout, to the returned transcript and to
`agent/transcript.txt` both. `token_usage` reports that line as `usage_source:
codex_state`, lowest precedence, never added to `turns`. `state_*.sqlite`
`threads.tokens_used` holds the same total with no input/output split, which is not
enough to price. Both fallbacks (`stream` and `codex_state`) count COMPLETED requests:
the request in flight at the kill is lost on both CLIs. The QUA-2803 episode itself
stays `unavailable`: it ran with `--ephemeral`, so it has 0 `turn.completed`, an empty
`threads` table and no rollout, and the same holds for every codex episode before this
change. `usage_source` (`result`/`turns`/`stream`/`codex_state`/`none`) rides on every
result.json and decides measured-vs-not, never the magnitude, since an episode may
legitimately spend little.

Prices come from the `claude-api` skill (Anthropic) or the provider's published page
(OpenAI), never from recall, with the source and date in a comment on the row. Anthropic
rows are the Claude 5 family plus 4.x for older boards; `cached_input` is the cache-READ
rate. The Fable tier does not follow the usual 0.1× rule: `claude-fable-5-1` reads at
$0.25/MTok (0.025×), `claude-fable-5` at $1 (both $10/$50, confirmed 2026-09-23).
`gpt-6-astra` is $10 / $1 cached / $50 from developers.openai.com (2026-09-23); there is
no bare `gpt-6` id, so there is no `gpt-6` row. Its cache writes ($12.50) and its
>272K-input-per-request surcharge are not modelled (the table prices summed usage).
Deliberately absent, because a plausible number in a table the board MULTIPLIES BY is
worse than a missing row: `claude-mythos-5/5.1` (limited access, rate open).
`claude-opus-4-8` was carrying $15/$75 — Opus 4.1-era numbers, 3× the real $5/$25 — and
is corrected. The model id priced is the one the agent REPORTED; `pricing.normalize_model`
maps it to a row (routing prefix, Bedrock prefix/version tail, `[1m]` tag, dated snapshot
suffix — no family guessing).

Neither agent shapes tools by default. `QGB_DISALLOWED_TOOLS` (comma-separated) is the
only source; unset or empty withholds nothing. It reaches MCP tools only — for
claude-code every name is prefixed `mcp__device__`, for codex it lands in the
per-server `disabled_tools`.

`tests/conftest.py` strips `QGB_*` before every test, by prefix (QUA-2807: a fixed list
let `QGB_DEVICE_CLOCK`/`QGB_DEVICE_TIMEZONE`/`QGB_HELDOUT_SOURCE` through); without it the
suite asserts against whatever the developer's `.env` happens to contain. Only the suite's
own switches survive (`QGB_LIVE_DEVICE`, `QGB_REPLAY_RUNS`).

**Saved-episode replay is fixture-based** (QUA-2807, `tests/adb_replay.py`). A deny-rule
change proves it refuses no legitimate request by replaying agent transcripts through
`deny_reason` — the `adb_replay_corpus` fixture: `tests/fixtures/adb_replay/` (two
synthetic episodes, claude-code and codex, carrying every request family the saved agents
sent plus three planted illegitimate ones) always, and the developer's runs dirs only with
`QGB_REPLAY_RUNS=<dir>[:<dir>]` (skipped otherwise; opted in with no transcripts fails).
Use `corpus.legitimate_newly_denied(new_rule, old=old_rule) == []`; add a request shape to
the fixture when a rule touches one it does not carry.

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
**Held-out budgets are derived too, and never named by default** (QUA-2799). With
`QGB_HELDOUT_DIR` set, `--mode journey` loads the split's cases and judges them by the same
rules, but prints them only as an aggregate block (cases, episodes, verdict counts, worst
%cap range, derived-vs-cap counts, how many the evidence supports moving); a public case is
judged against PUBLIC episodes only, so public verdicts read the same in every clone, and a
held-out case against every finished episode. A dropped episode's case id is printed only
when it is provably public (an app the public corpus carries, not stamped `heldout`);
anything else is counted under a reason with no id. `--mode hunt` redacts held-out apps and
task ids the same way. `--show-heldout` names them, for a curator's own terminal only;
never paste that output. `--write` writes a held-out budget into the local split it was
read from and refuses a row whose file is on the wrong side of it. Over the two runs
QUA-2784 used, the held-out block reproduces that derivation exactly (20 episodes, 10
healthy, worst 30-62% of cap, 9 of 10 derive below their cap).

**A truncation is not by itself a case for a bigger budget.** Over the 40 scored journey
episodes on disk at 2026-09-11, not one landed between 73% and 100% of its cap — an
episode either finished with a quarter of the budget unused or blew past it (102-108%) —
and two of the six truncations had causes of their own (a fixture that was never on
screen, an agent lost in a date picker). That is runaway, not shortfall. So `--mode journey` prints a verdict per
case (under-budget / runaway / no evidence) with the episode count behind every number,
judges a truncation against the worst cost per route step a FINISHED episode has ever
paid, and refuses to propose a raise off a runaway — raising the cap there buys the agent
more failing steps and charges every other episode in tokens for it. On 2026-09-11
exactly one case was under-budget on the evidence (`anki-add-tagged-note`: longest route
in its app, both versions died at 56/55, nothing of it ever finished), and 25 of the 40
cases of that day had no evidence at all. That case has since been pruned (QUA-2725), and
the 17 cases epic QUA-2723 added all came after that measurement, so re-run
`scripts/derive_budgets.py --mode journey` over current runs before quoting a verdict
from it. Get this backwards and the cost is doubled: a truncated journey episode scores
as not-completed AND as every seeded bug missed.

**The first budget re-derive at n ≥ 8, and what it cost** (2026-09-22, QUA-2744;
`orgzly-complete-repeating-task`, the corpus's longest route at 19 steps and the only cap
the QUA-2731 board proposed raising that survived a transcript read). 8 episodes
(4 trials × 2 versions, claude-code · claude-opus-5, raw arm, emulator-5554) cost
**$13.58 in 27m48s** — about 40% of the $32 projected from the board's own per-episode
mean, so price a journey re-derive off the CASE's measured episodes, not off a board-wide
average. **Verdict: no change, 60 stands.** Seven of eight finished at 37-57 steps; the
one truncation was a CLEAN trial at 61/60 that spent 18 swipes and 16 taps against the
9-11 and 10-11 of the three finished clean trials, including two `for i in 1..8; do adb
shell input swipe; done` loops (16 steps) and one navigation block it walked twice. Its
seeded counterpart spent 23 swipes and still finished at 52/60. That is an agent adrift,
not a route that needs the room, and `derive_budgets.py` reached RUNAWAY / NOT SUPPORTED
independently. Two things make this the first sample worth trusting: every episode
recorded `dump_stats {'builtin': 5}` — zero `builtin_killed`, zero `u2`, so QUA-2741's fix
held and no episode was paying 4-5 steps for a dead tool as all 83 of QUA-2731's did — and
it is the first journey sample taken at n ≥ 8. Worth watching rather than closing: the
worst FINISHED episode sat at 57/60 (95%), above the 85% crowding line, so this case runs
nearer its cap than any other in the corpus.

**The DevLoop-arm re-derive** (2026-09-23, QUA-2784, docs/budgets-devloop-2026-09-23.md).
Over the MCP-arm validation run (n ≤ 2 per case) plus a `--trials 4` re-run of the two cases
it flagged (n = 10 each, $25.37), two caps went UP and none came down:
`cal-open-task-from-list` 45 → 60 (`--write`, one seeded episode at 40/45), and
`medtimer-analysis-tabular-view` 40 → 56. That second raise was set BY HAND. The script
calls the case RUNAWAY, because its verdict order checks "truncation + any finished episode
under 85%" before crowding. But three of its five seeded episodes finished crowding the cap
(35, 37, 37 of 40), and the transcript of the fourth, truncated at 41/40, shows an agent
diagnosing the freeze and about to write its findings, not one adrift. A freeze defect
costs every seeded run a wait, log reads and a reproduction. No cap is LOWERED off
MCP-arm evidence even though 36 of 41 derive below their cap. The evidence is one model
(no GPT-6 Astra episodes) at n ≤ 2. And one `step_budget` gates BOTH arms, while the bare
arm spends more (24.4 vs 17.4 mean steps).

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
<runs_dir>/<task>/<run>/evidence/      index.html, manifest.json, steps.jsonl,
                                       screens/, frames/, findings.json, meta.json
dist/<app>/buggy.apk                   locally built APKs (gitignored; else from HF)
```
