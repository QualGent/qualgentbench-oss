# Comparison run: Claude Fable vs GPT-6 Astra on the DevLoop arm (QUA-2786, 2026-09-24)

**Result: on this corpus the two models cannot be told apart on any headline rate once the scorer's wording artifacts are adjudicated. Raw, the board shows a 29-point catch gap in Fable's favour. That gap is outside the intervals, and it is an artifact.** Four runs in the order Fable, Astra, Astra, Fable (one trial each, 41 public + 10 held-out cases, clean and seeded) produced 400 episodes:
- none excluded, none contaminated;
- 400/400 clean starts, 400/400 clean MCP sessions and adbd;
- one truncation;
- `validate_bundle` green on all 400.

I read every transcript. Neither model ever reported something the device did not show. Every false alarm on a clean build, for both models, is a true statement about the fixture or the upstream app (`environment`).

On the seeded side the scorer credited Fable with 78 of 80 public defects and Astra with 55:
- **Where Astra lost 23 of its 25 missed defects:**
  - 21 are crashes it saw and described truthfully. Its reports read "MedTimer disappeared and the Android home screen appeared" or "returned to the main deck list". They never use the words "crash", "keeps stopping" or an exception name, and the crash symptom lists contain nothing else.
  - The other 2 are the dead tap on `cal-open-task-from-list`, one of the six wording-sensitive cases (owner condition 2).
- **Adjudicated by the same rule for both models, both reach 78/80.** The rule: credit an honest, device-backed description of the seeded defect's visible effect at the step where it fires.
- **What remains is a difference in diagnostic depth, not in finding bugs.** Fable opens crash logs (42 calls) and names the exception. Astra, at codex's default reasoning effort for this model (`low`), never opened crash logs (0 calls).
- **Astra is cheaper and faster:** $0.85 per episode priced against $1.32, with cache writes the pricing table does not model adding about 12%; 0.7 min median agent time against 1.1.

**The environment-excluded numbers come first, because the raw false-alarm rate on this arm measures how many true upstream quirks a model bothers to mention.** Every clean-arm false alarm on this board (Fable 7 public + 5 held-out, Astra 2 + 4) is a true statement:
- a fixture state (MedTimer's inactive seeded reminder);
- an upstream UI behaviour (Fossify Contacts opens with the cursor in Address; its landscape header overlaps the status bar);
- an upstream behaviour of a held-out app.

The raw row stays published below it, because it is what the harness computes.

| DevLoop arm · corpus `814b34eb9863` · held-out `10f3382a18c8` · brief v3 | Fable, public (2 runs, 160 ep.) | Astra, public (2 runs, 160 ep.) | Fable, held-out (40 ep.) | Astra, held-out (40 ep.) |
| --- | --- | --- | --- | --- |
| **false alarm / clean case, environment excluded** | **0/82 = 0% [0–4.5]** | **0/82 = 0% [0–4.5]** | **0/20 = 0% [0–16.1]** | **0/20 = 0% [0–16.1]** |
| false alarm / clean case, raw | 7/82 = 8.5% [4.2–16.6] | 2/82 = 2.4% [0.7–8.5] | 5/20 = 25.0% [11.2–46.9] | 4/20 = 20.0% [8.1–41.6] |
| **clean-run integrity @200, environment excluded** | **100% [0–100]** | **100% [0–100]** | **100% [0–100]** | **100% [0–100]** |
| clean-run integrity @200, raw | 0% [0–0] | 1% [0–26] | 0% [0–0] | 0% [0–0] |
| **catch / seeded defect, wording-adjudicated** (§6) | **78/80 = 97.5% [91.3–99.3]** | **78/80 = 97.5% [91.3–99.3]** | **19/20 = 95.0% [76.4–99.1]** | **19/20 = 95.0% [76.4–99.1]** |
| catch / seeded defect, raw (scorer) | 78/80 = 97.5% [91.3–99.3] | 55/80 = 68.8% [57.9–77.8] | 19/20 = 95.0% [76.4–99.1] | 18/20 = 90.0% [69.9–97.2] |
| blocker recall (functional L4+L3), raw → adjudicated | 50/50 → 50/50 (100% [92.9–100]) | 27/50 = 54% [40.4–67.0] → 50/50 | 4/4 → 4/4 | 4/4 → 4/4 |
| completion, raw → adjudicated | 160/160 → 160/160 | 136/160 = 85.0% → 159/160 = 99.4% | 40/40 → 40/40 | 39/40 → 40/40 |
| completion, raw, excluding the five QUA-2768 weak-witness cases | 140/140 = 100% | 116/140 = 82.9% (adjudicated 139/140) | 40/40 | 39/40 |
| precision / recall / F1 (raw, board) | 74% / 98% / 84% | 65% / 69% / 67% | 63% / 95% / 76% | 72% / 90% / 80% |
| false reports: real / artifact / corpus / environment | 0 / 12 / 0 / 15 (of 27) | 0 / 26 / 0 / 4 (of 30) | 0 / 2 / 0 / 9 (of 11) | 0 / 2 / 0 / 5 (of 7) |
| truncated | 0 | 1 (self-inflicted rework, §6.4) | 0 | 0 |
| $/episode (mean of priced) | $1.30 (`reported`) | $0.84 + 1 unpriced (`estimated`; ≈ +12% with the unmodelled cache writes, §8) | $1.39 | $0.91 (≈ +12%) |
| min/episode (median agent wall time) | 1.1 | 0.7 | 1.2 | 0.8 |

Brackets are 95% Wilson intervals (`rates.wilson`). Per-model rows pool two trials, so n counts each case twice. As CLAUDE.md warns, "repeat trials narrow the bracket on paper only". The per-run rows in §7 (n = 41 / 40 / 10) are the honest intervals, and every "outside the intervals" statement in §9 was checked against those as well. Integrity intervals are the rate interval pushed through `(1 − p)^200` (`rates.clean_run_integrity`), so a 0/82 row honestly prints `100% [0–100]`.

| | |
| --- | --- |
| runs (Phase B, in order) | **F1** `20260923-232823-29c7` (claude-code · claude-fable-5-1) · **A2** `20260924-022244-0252` (codex-cli · gpt-6-astra) · **A3** `20260924-043254-1e0b` (codex-cli · gpt-6-astra) · **F4** `20260924-064626-8aeb` (claude-code · claude-fable-5-1) |
| Phase A (Astra preflights, not on the board) | `20260923-221740-f35b` (codex-cli 0.146.0; stopped at 9/16, pre-upgrade) · `20260923-224921-0461` (codex-cli 0.156.1; 16/16) |
| every run | `--mode journey`, all six public apps + the held-out split (required), `--trials 1`, `--device emulator-5554` (one lane), `--mcp-server http://127.0.0.1:51851`, `--require-heldout --yes --plain`, explicit flags only, never a piped confirm |
| harness | qualgentbench-oss **`0992a3a`** = epic head `b4c308d` + QUA-2801 (PR #87: MCP result text decoded of `\uXXXX` escapes before scoring) + only this doc. `git diff b4c308d 0992a3a -- src/qualgentbench/data` is empty |
| DevLoop-MCP | **`5dd85c6`** (QUA-2800 per-session isolation), started fresh before each run: `uv run devloop-mcp --transport streamable-http --port 51851 --app-source none`. The bench's MCP checks (server, 74 tools, app source `none`, isolation `per_mcp_session`) passed before every run, and the server was stopped between runs |
| agents | **codex-cli 0.156.1** (upgraded from 0.146.0 on owner approval, §1) and **claude-code 2.1.281**. Neither changed during the four runs |
| effective reasoning effort (vendor defaults, owner decision) | **Astra: `low`**. Nothing is configured; codex 0.156.1 sends the catalog's `default_reasoning_level` for `gpt-6-astra` (`low`; `core/src/client.rs::build_reasoning` falls back to it). The exec banner's "reasoning effort: none" prints the unset config field. Measured: 1,324 and 1,256 reasoning tokens over 100 episodes each. **Fable: claude-code's default.** The harness passes no `--effort`, and stream-json does not echo the level. Measured: 608 and 596 `thinking` blocks over 100 episodes each |
| corpus / held-out | `corpus_version` **`814b34eb9863`** and `heldout_version` **`10f3382a18c8`** on 400/400 episodes. The split was read from a local copy of the split (exported as `QGB_HELDOUT_DIR`), not S3 (stale) |
| device | AVD `qgbench_root` (android-35 google_apis arm64) as `emulator-5554`. Booted headless once, at 22:16Z on 2026-09-23, and kept up through Phase A and all four runs, so no device drift can map onto a model. Clock pin `2026-09-16T10:00:00-05:00` on 400/400 |
| budgets | as merged (QUA-2784 incl. `cal-open-task-from-list` 60, `medtimer-analysis-tabular-view` 56; QUA-2789's held-out 45 → 65). No budget, corpus, symptom or scorer edit between runs |
| runs dir | `~/.qualgentbench/runs` (QUA-2778). `inherited_instructions: []` on 400/400. Every run directory is kept |

## Contents

0. Scope, spend and how the runs were controlled
1. Phase A: the Astra preflight, the codex upgrade and QUA-2801
2. Device-free gates
3. Provenance, on every episode
4. What each model did, from the transcripts
5. The board: per run, merged, projection
6. Every false report, miss, non-completion and truncation, adjudicated
7. Rates, raw and corrected, per model and per run
8. Cost and wall time
9. Power, and which differences are outside the intervals
10. Follow-ups
11. Acceptance criteria
Appendix: every public case, every run

---

## 0. Scope, spend and how the runs were controlled

**Scope.** Each run is 100 units:
- 41 public cases give 41 clean + 39 seeded episodes. `anki-create-deck` and `tasks-complete-and-rename` are clean-only since QUA-2783.
- 10 held-out cases give 20 episodes.

**Order.** Fable, Astra, Astra, Fable. One `run` is one agent and one model, so models cannot share a queue, and the ABBA order is the only interleaving there is. It lets F1 vs F4 and A2 vs A3 test for drift and order (§9).

**Credentials, read by key name only:**
- `CLAUDE_CODE_OAUTH_TOKEN` (subscription) and `OPENAI_API_KEY`, sourced from the main checkout's `.env` into the launch shell.
- The codex adapter exchanges the API key for a per-episode `auth.json` and deletes it after the episode.

**Spend control:**
- **Approved:** $600–1,100, with a stop line at a projected $1,300.
- **Monitoring:** a scratch watcher summed `metrics.cost_usd` over finished episodes after every ten, and projected the total as spent so far + current run pro rata + an estimate for each remaining run.
- **Projection:** its highest reading was $630, after F4's first episode (one $3.00 freeze case extrapolated ×100). It was $498 by F4's 17th episode and settled at $460, never near the line.
- **Stopping:** the harness's only clean stop (`--stop-at-seven-day-pct`) reads the Claude subscription window and does nothing for codex. The one stop needed (Phase A #1, §1) was a SIGINT sent the moment an episode finished, while the next app was being staged and before any agent launched.
- **Re-runs:** none. No episode was requeued, rate-limited or dropped (`schedule.jsonl` of every run).

## 1. Phase A: the Astra preflight, the codex upgrade and QUA-2801

**The preflight scope.** One Astra trial, both arms, on 8 cases, 16 units:
- the two whose caps QUA-2784 raised on Fable evidence: `cal-open-task-from-list` and `medtimer-analysis-tabular-view`;
- the other five QUA-2796 wording-sensitive cases;
- the held-out case whose route QUA-2789 re-authored (budget 45 → 65).

**Preflight #1, `20260923-221740-f35b`, codex-cli 0.146.0.**
- **Stopped at 9/16 ($13.50)**, on the stop rule of the time: `cal-open-task-from-list~seeded` was honest but uncredited.
- **A finding worse than the stop.** Every transcript opens with codex's warning, quoted: "Model metadata for `gpt-6-astra` not found. Defaulting to fallback metadata; this can degrade performance". 0.146.0's catalog predates the model. Its fallback (`models-manager/src/model_info.rs::model_info_from_slug` at `rust-v0.146.0`):
  - sends no reasoning effort;
  - truncates tool output at 10,000 *bytes*;
  - disables parallel tool calls;
  - uses the generic base instructions.
- **Consequence.** Every Astra board on 0.146.0 would have measured codex's fallback, not the model.

**Owner decisions, applied before any board run:**
- install codex-cli 0.156.1, whose catalog carries `gpt-6-astra` (default reasoning `low`, truncation 10,000 *tokens*, freeform `apply_patch`, catalog base instructions);
- leave reasoning at both vendors' defaults;
- keep the wording list frozen and adjudicate by hand (owner condition 2);
- re-run the full Phase A.

**Preflight #2, `20260923-224921-0461`, codex-cli 0.156.1: 16/16, $12.63.** Checks:
- no metadata warning;
- parsed MCP calls equal the raw codex items, by name and refusal, on 16/16 episodes;
- 0 parallel MCP calls (the catalog leaves `supports_parallel_tool_calls` unset);
- largest tool result `qg_start` at 10,112 B, well under 10,000 tokens;
- `mcp_isolation.clean` with exactly one session on 16/16, `adbd_at_end` clean on 16/16;
- `metered_denied` 0, no raw adb;
- 0 truncated, `validate_bundle` green on 16/16;
- the findings file was the report on 16/16, and `mobile_report_result` was called 0 times (9/9 on 0.146.0);
- `qg_start` was called in 3/16 episodes (9/9 on 0.146.0).

**It also exposed a real scorer asymmetry.** DevLoop's screenshot path (`mobile_tap_and_observe` with `include_screenshot`) returns its text as `json.dumps(result, indent=2)`, which escapes non-ASCII as `\uXXXX`:
- **What each agent records:** codex keeps the escapes verbatim; claude-code records the characters.
- **The scoring gap:** journey scoring read the raw text, so a bullet or a narrow no-break space in a witness, marker or quote matched on one agent only.
- **Measured effect:** `orgzly-open-note-from-notebook~clean` was scored not completed for Astra while its device text carried `Getting Started with Orgzly  •  Notes`.

The owner filed **QUA-2801** (scorer-side decode, no DevLoop change), merged as PR #87, and the harness was frozen at `0992a3a`. A dry-run rescore of the Phase A run on `0992a3a` changes exactly that one episode (False → True). Rescored on `0992a3a`, the four Phase B runs change 0 episodes, because they were scored there.

**Held-out re-authored case (aggregate only).** It used 43–48 of its 65 steps on both arms in both Phase A runs, and its seeded defect was credited from that episode's own device text.

## 2. Device-free gates

Run in the worktree after all four runs, with no episode live:

| gate | split unset | `QGB_HELDOUT_DIR` exported |
| --- | --- | --- |
| `uv run pytest` | **2033 passed, 7 skipped** | — |
| `ruff check --select F821` | **All checks passed!** (`uv run --with ruff`) | — |
| `lint_journey_cases.py` | **PASS**: 41 cases, 0 errors, 4 warnings (the known ones: QUA-2768's deferred reference, `columns` on contacts-favorite) | **PASS**: 51 cases, 0 errors, 5 warnings |
| `journey_adversary_check.py` | **PASS**: 5 guessers 0 bugs / 0 completions over 39 seeded, through the findings file and the report tool; honest 40/40; `symptom-spray` paid on 41/41 clean | **PASS**: 5 guessers 0 over 49 seeded; honest 50/50; `symptom-spray` paid on 51/51 |
| `check_tier_ready.py --tier easy` | **READY** | — |
| `holdout.py verify` | — | **OK**: loads, hashes, and no held-out id under `src/qualgentbench/data` or `tests/fixtures` |

## 3. Provenance, on every episode

Read from each `result.json`, `adb_counts.json`, `interactions.json` and `evidence/` in the four runs (scripts in the scratchpad, summarised here):

| check | F1 | A2 | A3 | F4 |
| --- | --- | --- | --- | --- |
| episodes / excluded / contaminated | 100 / 0 / 0 | 100 / 0 / 0 | 100 / 0 / 0 | 100 / 0 / 0 |
| `brief_version` 3 · corpus `814b34eb9863` · held-out `10f3382a18c8` · clock pin | 100/100 each | 100/100 each | 100/100 each | 100/100 each |
| device `emulator-5554` / AVD `qgbench_root` · device-state invariant (no `staging_failed`, no `env_failure`) | 100/100 | 100/100 | 100/100 | 100/100 |
| installed journey build = the case file's sha256 (one hash per app) | 8/8 apps | 8/8 | 8/8 | 8/8 |
| `mcp_isolation.clean` with exactly one session, `clean_at_start` | 100/100 | 100/100 | 100/100 | 100/100 |
| `adbd_at_end` uid 2000, not rooted, not primed | 100/100 | 100/100 | 100/100 | 100/100 |
| contamination hits (incl. `devloop_artifacts`, `other_episode`) / `inherited_instructions` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| `cost_source` / `usage_source` | `reported`/`result` 100 | `estimated`/`turns` 100 | `estimated`/`turns` 99, `unavailable`/`none` 1 (the truncated episode, QUA-2803) | `reported`/`result` 100 |
| `report_source` | `findings_file` 100 | 100 | 99 + 1 no report (truncated) | 100 |
| `mobile_report_result` calls (used as report) | 3 (0) | 0 | 0 | 3 (0) |
| `metered_denied` | 2 in 1 episode | 0 | 0 | 1 in 1 episode |
| `dump_stats` | `builtin` only on 99, one `builtin_killed 1 · u2 1` (the harness's own reads around `medtimer-analysis-tabular-view`'s stuck probe; costs time, not a verdict) | same | same | same |
| `metrics.steps` = `interactions.json` | 91/100 (9 one short: claude's hook counter lags the last call, QUA-2791) | 100/100 | 100/100 | 90/100 (same) |
| `validate_bundle.py` | FAILURES: 0 on 100/100 | 100/100 | 100/100 | 100/100 |

## 4. What each model did, from the transcripts

**How the transcripts were read:**
- Every one of the 400 transcripts was read in full, as an ordered digest of every narration, reasoning block, tool call, condensed tool result (the screen's element texts) and the findings file.
- Parallel readers each took 25 episodes; I re-checked every non-OK row against the raw transcript, and against the saved screenshot where the evidence was visual.
- Every adjudicated row in §6 cites what settles it.

**Fable (claude-code, runs F1 and F4):**
- **Tool calls:** 1,487 and 1,412, the bulk `mobile_tap_and_observe` and `mobile_observe_screen`.
- **Crash handling:** it reads crash logs after every crash (`mobile_crash_logs` 22 + 20, `mobile_device_logs` 12 + 12) and names the exception.
- **Raw adb**, in 10 and 6 episodes (29 and 13 commands):
  - 11 are `adb shell date`, a pinned device date against its own context date;
  - `logcat`, `dumpsys` and `/data/anr` reads on the freeze case `medtimer-analysis-tabular-view~seeded` (both runs), and `input tap` / `dumpsys activity` / `logcat` to confirm the dead tap on `cal-open-task-from-list~seeded` (both runs);
  - one `content query` of the contacts provider.
- **Privilege attempts, all refused at the meter:**
  - F1 `medtimer-analysis-tabular-view~seeded` ran `adb root`. The meter refused the `root:` service (QUA-2795), and the follow-up `id` read `uid=2000`. It also ran `run-as … kill -3`, refused ("run-as is not available to the agent").
  - F4, same case: `su 0 sed … /data/anr/…`, refused (QUA-2790).
  - `adbd_at_end` was clean on every episode.
- **Reports:** `mobile_report_result` was called 3 times per run and never used as the report. 133 of 163 reports were grounded; the ungrounded ones join two nodes, a label and its value, or quote a toast.

**Astra (codex-cli, runs A2 and A3):**
- **Tool calls:** 1,161 and 1,171.
- **No raw adb and no privilege attempt in 200 episodes.** Its only shell use was writing or reading back its own `findings.yaml` (4 commands). The epic expected codex to reach for adb more; on the DevLoop arm it never did.
- **Other tools:** it called `qg_start` (DevLoop's workflow guide) in 9 and 13 episodes, `mobile_report_result` 0 times, and `mobile_crash_logs` / `mobile_device_logs` 0 times.
- **Crash reporting.** Every Astra crash report describes the effect: "exited to the Android home screen", "returned to the deck list", "No items found." It never describes the cause. In 21 public episodes that effect was the whole visible defect, and it was reported truthfully at the right step (§6).
- **Grounding:** 102 of 112 reports grounded.

**The Fossify Contacts focus quirk costs Astra more than Fable:**
- **The quirk:** the editor opens with the cursor in Address (upstream, reported as a false alarm by Fable in F1).
- **Why it hurts Astra:** `mobile_type_text` types into the focused field, so Astra twice saved "Alice" into Address.
  - In A2 `contacts-favorite~seeded` it then reported a contact row with an avatar and no name. That is true: screenshot `0006.jpg`, where the hierarchy has no name node.
  - In A3 `contacts-favorite~clean` it noticed its own mistake, deleted the contact, redid the flow and ran out of budget (§6.4).
- **Why Fable is unaffected:** it taps First name first.

**Speed and reasoning.** Astra's median agent time is 0.7 min against Fable's 1.1. Its mean step count is the same, 17.4–17.6 on both models. It runs at a reasoning effort that spends about 13 reasoning tokens per episode.

## 5. The board: per run, merged, projection

`qualgent-bench show --run <id> --agent <agent> --mode journey`, one line per row. Held-out rows are their own block and are never ranked with the public ones.

```text
 run  Agent + Model                   Arm Episodes Cut Integrity  Done clean Done seeded Completion Bugs found False rep. Prec. Recall  F1 Steps  $/ep              min/ep
 F1   claude-code · claude-fable-5-1  mcp   80      0  0% (5/41)   41/41       39/39        100%       39/40        16      71%   98%  82%   17   $1.32               1.1
 F1H  claude-code · claude-fable-5-1  mcp   20      0  0% (3/10)   10/10       10/10        100%       10/10         5      67%  100%  80%   19   $1.42               1.2
 A2   codex-cli · gpt-6-astra         mcp   80      0  1% (1/41)   41/41       27/39         85%       27/40        17      61%   68%  64%   16   $0.85               0.7
 A2H  codex-cli · gpt-6-astra         mcp   20      0  0% (2/10)   10/10       10/10        100%       10/10         3      77%  100%  87%   21   $0.89               0.8
 A3   codex-cli · gpt-6-astra         mcp   80      1  1% (1/41)   40/41       28/39         85%       28/40        13      68%   70%  69%   17   $0.83 +1 unpriced   0.7
 A3H  codex-cli · gpt-6-astra         mcp   20      0  0% (2/10)   10/10        9/10         95%        8/10         4      67%   80%  73%   21   $0.93               0.8
 F4   claude-code · claude-fable-5-1  mcp   80      0  0% (2/41)   41/41       39/39        100%       39/40        11      78%   98%  87%   17   $1.27               1.1
 F4H  claude-code · claude-fable-5-1  mcp   20      0  0% (2/10)   10/10       10/10        100%        9/10         6      60%   90%  72%   19   $1.37               1.2
corpus 814b34eb9863 · held-out 10f3382a18c8 · brief v3 (every row)

Rates  F1   false alarm 5/41 12% [5–26]   catch 39/40 98% [87–100]   integrity@200 0% [0–0]    blocker recall 25/25 100% [87–100]
Rates  F1H  false alarm 3/10 30% [11–60]  catch 10/10 100% [72–100]  integrity@200 0% [0–0]    blocker recall 2/2 100% [34–100]
Rates  A2   false alarm 1/41 2% [0–13]    catch 27/40 68% [52–80]    integrity@200 1% [0–42]   blocker recall 13/25 52% [34–70]
Rates  A2H  false alarm 2/10 20% [6–51]   catch 10/10 100% [72–100]  integrity@200 0% [0–0]    blocker recall 2/2 100% [34–100]
Rates  A3   false alarm 1/41 2% [0–13]    catch 28/40 70% [55–82]    integrity@200 1% [0–42]   blocker recall 14/25 56% [37–73]
Rates  A3H  false alarm 2/10 20% [6–51]   catch 8/10 80% [49–94]     integrity@200 0% [0–0]    blocker recall 2/2 100% [34–100]
Rates  F4   false alarm 2/41 5% [1–16]    catch 39/40 98% [87–100]   integrity@200 0% [0–7]    blocker recall 25/25 100% [87–100]
Rates  F4H  false alarm 2/10 20% [6–51]   catch 9/10 90% [60–98]     integrity@200 0% [0–0]    blocker recall 2/2 100% [34–100]
```

**Merged view.** A runs dir of symlinks to exactly these 400 episode dirs, read with `show --runs-dir <merged> --agent <agent> --mode journey --history`. `--history` is needed: without it `show` keeps only the latest trial per model, task and trial number. The same rows come from `rescore_journey.py --runs-dir <merged> --dry-run --projection 200 50`, which changes 0 episodes:

```text
 1  codex-cli · gpt-6-astra · mcp        episodes 160 · cut 1 · integrity @200 1% (2/82) · completion 85% · bugs 55/80 · false rep. 30 · P 65% · R 69% · F1 67% · $/episode $0.84 +1 unpriced · min/episode 0.7
 2  claude-code · claude-fable-5-1 · mcp episodes 160 · cut 0 · integrity @200 0% (7/82) · completion 100% · bugs 78/80 · false rep. 27 · P 74% · R 98% · F1 84% · $/episode $1.30 · min/episode 1.1
Held-out (never blended):
 H1 codex-cli · gpt-6-astra · mcp        episodes 40 · cut 0 · integrity @200 0% (4/20) · completion 98% · bugs 18/20 · false rep. 7 · P 72% · R 90% · F1 80% · $/episode $0.91 · min/episode 0.8
 H2 claude-code · claude-fable-5-1 · mcp episodes 40 · cut 0 · integrity @200 0% (5/20) · completion 100% · bugs 19/20 · false rep. 11 · P 63% · R 95% · F1 76% · $/episode $1.39 · min/episode 1.2
Rates: Astra false alarm 2/82 2% [1–8] · catch 55/80 69% [58–78] · blocker 27/50 54% [40–67]
       Fable false alarm 7/82 9% [4–17] · catch 78/80 98% [91–99] · blocker 50/50 100% [93–100]
       held-out: Astra 4/20 20% [8–42] · 18/20 90% [70–97];  Fable 5/20 25% [11–47] · 19/20 95% [76–99]
Projection, 200 clean cases + 50 seeded defects (raw rates):
       Astra  expected false alarms 4.9 [1.3–16.9] · misses 15.6 [11.1–21.0] · integrity 1% [0–26] · prior-weighted errors 20.5 / 250
       Fable  expected false alarms 17.1 [8.4–33.2] · misses 1.3 [0.3–4.3] · integrity 0% [0–0] · prior-weighted errors 18.3 / 250
```

**Read raw, the board ranks Astra first.** It ranks on integrity, and Astra mentioned fewer true upstream quirks: 2 clean episodes against 7. By catch and completion the raw board puts Fable far ahead, because of the crash wording.

**On the corrected numbers the two coincide in every line:**
- 0/82 false alarms and 78/80 caught each;
- projection 0 [0–9.0] expected false alarms and 1.3 [0.3–4.3] expected misses for either model;
- the per-run intervals (n = 41) give 0 [0–17.1] and 1.3 [0.2–6.4].

**Budgets.** The only truncation is §6.4. No other episode crowded its cap: the maximum was Astra `cal-repeat-survives-rotation~seeded` at 42/50 (84%), and Fable's 44/56 on `medtimer-analysis-tabular-view~seeded` (79%) under its new cap. The three cases QUA-2784 put on watch (`cal-edit-event`, `cal-repeat-survives-rotation`, `medtimer-take-dose-then-medicine-list`) stayed under 85% on both models. The held-out re-authored case stayed at or under 69% of its new 65.

## 6. Every false report, miss, non-completion and truncation, adjudicated

The ticket's classes:
- `real`: nothing on the device supports it.
- `artifact`: matcher, grounding, tense or wording, including the "thoroughness" charge for a true consequence of a defect already credited in the same episode.
- `corpus`: a case defect.
- `environment`: a true statement about fixture, device or upstream-app state that no defect put there.

A report credited by hand carries `→ credits`. The adjudication rule is one rule for both models: **credit an honest, device-backed description of the seeded defect's visible effect at the step where it fires.** Honest means the device result shows it and `fault_fired` names the defect where the case has a marker. It is applied to both models alike. Fable needed it zero times: its crash reports all name the crash.

Held-out rows follow docs/heldout.md. They are numbered in each run's order (H1…H20), and no case or app is named or quoted.

### Unmatched reports, every one

#### Fable

| run | episode | recorded | the report (public: quoted; held-out: withheld) | class | evidence / device result |
| --- | --- | --- | --- | --- | --- |
| F1 | `medtimer-add-reminder~clean` | false alarm (clean) | obs `0 reminders` — The Medicine list showed Ibuprofen with '0 reminders' and 'Inactive', but opening Ibuprofen showed an existing reminder… | **environment** | 0 reminders: fixture seeds an INACTIVE Ibuprofen reminder; list counts active only. Device: list '0 reminders' + detail 'Inactive, Every day' |
| F1 | `medtimer-add-reminder~seeded` | unmatched (seeded) | obs `0 reminders` — The Medicine list row for Ibuprofen says 0 reminders, but opening Ibuprofen shows one reminder card (8:00 AM, dosage 2,… | **environment** | 0 reminders (fixture) |
| F1 | `medtimer-take-dose-then-medicine-list~clean` | false alarm (clean) | obs `Ibuprofen (6 left, After 10/14/26) / 0 reminders / Inactive` — The Medicine list says Ibuprofen has 0 reminders, but opening the Ibuprofen entry shows one reminder configured (8:00 A… | **environment** | 0 reminders (fixture) |
| F1 | `medtimer-take-dose-then-medicine-list~seeded` | unmatched (seeded) | obs `Ibuprofen (4)` — After relaunch the Ibuprofen (4) 10:00 AM row still shows the bell (Raised) status icon, identical to before the test. … | **artifact** | thoroughness: Taken not persisted after relaunch = consequence of found ANR overview-action-blocks-main-thread |
| F1 | `medtimer-take-dose-then-medicine-list~seeded` | unmatched (seeded) | obs `Ibuprofen (10 left, After 10/14/26) 0 reminders Inactive` — Medicine tab says Ibuprofen has 0 reminders and is Inactive, yet today's Overview shows two Ibuprofen reminders at 10:0… | **environment** | 0 reminders (fixture) |
| F1 | `anki-open-card-from-browser~seeded` | unmatched (seeded) | obs `AnkiDroid notification: Analyzing Heap Dump` — After re-opening the editor from the card browser, the debug build posted an 'Analyzing Heap Dump' notification, indica… | **environment** | 'AnkiDroid notification: Analyzing Heap Dump' is in the device result (grounded): the debug build's LeakCanary, present on both builds; not a seeded defect |
| F1 | `cal-complete-task~seeded` | unmatched (seeded) | obs `Taxes` — After tapping Mark completed and returning to the event list, the Taxes row is rendered exactly as before, with no comp… | **artifact** | thoroughness: row not struck through = consequence of found task-complete-crash |
| F1 | `cal-complete-task~seeded` | unmatched (seeded) | obs `Mark completed` — Reopening the task after the Mark completed attempt still shows the Mark completed button, so the completion state was … | **artifact** | thoroughness: still 'Mark completed' = consequence of found task-complete-crash |
| F1 | `cal-switch-back-to-list~seeded` | unmatched (seeded) | obs `Yearly view` — Once the yearly view is active, no option in the Change view dialog takes effect: Monthly view and Daily view were also… | **artifact** | thoroughness: every view is stuck once on yearly = the found view-switch-stuck-on-year, tested wider |
| F1 | `cal-create-event~seeded` | unmatched (seeded) | obs `September 16 (Wed)  11:00 AM / September 16 (Wed)  11:00 AM` — the default end time equals the default start time (both 11:00 AM), so saving without edits creates a zero-duration eve… | **environment** | event_start_time and event_end_time both '11:00 AM' in the editor result; identical ids/values in cal-create-event~clean (upstream default/fixture), not the seeded typo |
| F1 | H1 (clean) | false alarm (clean) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| F1 | H2 (seeded) | unmatched (seeded) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| F1 | H8 (seeded) | unmatched (seeded) | (withheld) | **environment** | an upstream placeholder accessibility label; in the device text of every clean episode of that app |
| F1 | `tasks-add-subtask~seeded` | unmatched (seeded) | obs `Pack for trip 2 Passport Chargers` — The new subtask Tickets was not persisted. After relaunch, Pack for trip still shows a subtask count of 2 with only Pas… | **artifact** | thoroughness: subtask not persisted = consequence of found subtask-filed-before-written |
| F1 | `contacts-new-contact-survives-rotation~seeded` | unmatched (seeded) | obs `At least 1 field has to be filled out` — tapping Save after the rotation showed a toast rejecting the save because the name entered before rotation had been dis… | **artifact** | thoroughness: toast 'At least 1 field has to be filled out' visible in evidence/screens/0014.jpg (toasts are never in the hierarchy, so ungrounded) = consequence of foun… |
| F1 | `contacts-favorite~clean` | false alarm (clean) | obs `Address` — when the new-contact editor first opened, the blinking text cursor was shown in the empty Address field rather than in … | **environment** | cursor in Address on open: upstream Fossify focus; visible in contacts-create~clean evidence/screens/0004.jpg (clean build); the hierarchy carries no focus attribute |
| F1 | `contacts-create~clean` | false alarm (clean) | obs `Address` — when the new-contact form opened, the text cursor was already blinking in the empty Address field instead of the First … | **environment** | Address cursor, screenshot 0004.jpg of this episode |
| F1 | `contacts-create~seeded` | unmatched (seeded) | obs `Address` — when the new-contact form opens, the text cursor sits in the Address field instead of the First name field, so typing i… | **environment** | Address cursor (seeded patch is a tab label) |
| F1 | H11 (clean) | false alarm (clean) | (withheld) | **environment** | an upstream UI behaviour; the same device text on both arms, clean build included |
| F1 | H13 (clean) | false alarm (clean) | (withheld) | **environment** | an upstream UI behaviour (a value refreshes only after saving); device text on the clean build |
| F1 | `medtimer-check-stock~clean` | false alarm (clean) | obs `0 reminders` — the Ibuprofen row on the Medicine list says '0 reminders', but opening Ibuprofen shows one reminder card (8:00 AM, Dosa… | **environment** | 0 reminders (fixture) |
| F4 | `medtimer-take-dose-then-medicine-list~seeded` | unmatched (seeded) | obs `Ibuprofen (4)` — After the hang and relaunch, the Ibuprofen (4) row still shows the bell (raised/reminded) icon, so the Taken answer was… | **artifact** | thoroughness: Taken not persisted after the found ANR |
| F4 | `medtimer-take-dose-then-medicine-list~seeded` | unmatched (seeded) | obs `Ibuprofen (10 left, After 10/14/26) 0 reminders Inactive` — The Medicine tab says Ibuprofen has 0 reminders and is Inactive, yet the Overview for today shows a raised Ibuprofen (4… | **environment** | 0 reminders (fixture) |
| F4 | `medtimer-add-reminder~seeded` | unmatched (seeded) | obs `0 reminders` — The Ibuprofen row on the Medicine list says '0 reminders' and 'Inactive', but opening Ibuprofen shows one reminder card… | **environment** | 0 reminders (fixture) |
| F4 | `medtimer-take-dose-then-medicine-list~clean` | false alarm (clean) | obs `Ibuprofen (6 left, After 10/14/26) / 0 reminders / Inactive` — The Overview showed two Ibuprofen reminder events for today (Ibuprofen (2.5) taken and Ibuprofen (4) raised at 10:00 AM… | **environment** | 0 reminders (fixture) |
| F4 | `cal-repeat-survives-rotation~seeded` | unmatched (seeded) | obs `Planning` — After saving, the calendar shows Planning only on Wednesday September 16. September 23 and 30 are empty and the whole o… | **artifact** | thoroughness: Planning only on Sep 16 = the found repeat-lost-on-rotation seen in the month view |
| F4 | `cal-complete-task~seeded` | unmatched (seeded) | obs `Taxes` — The event list row for Taxes looks identical to before Mark completed was tapped: normal text with the task icon and ti… | **artifact** | thoroughness: row not struck through after the found task-complete-crash |
| F4 | H2 (seeded) | unmatched (seeded) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| F4 | H4 (clean) | false alarm (clean) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| F4 | H5 (seeded) | unmatched (seeded) | (withheld) | **artifact** | thoroughness: a consequence of the defect already credited in this episode |
| F4 | `tasks-add-subtask~seeded` | unmatched (seeded) | obs `Pack for trip / 2 / Passport / Chargers` — The subtask Tickets was not persisted; Pack for trip still shows only its two original subtasks Passport and Chargers | **artifact** | thoroughness: Tickets not persisted after the found crash |
| F4 | `tasks-complete-repeating~seeded` | unmatched (seeded) | obs `Due today` — after relaunching, Water plants is still listed under Due today and its detail screen shows due date Today with Repeats… | **artifact** | thoroughness: due date not advanced after the found crash |
| F4 | H12 (seeded) | unmatched (seeded) | (withheld) | **environment** | an upstream UI behaviour; the same device text on both arms, clean build included |
| F4 | H14 (clean) | false alarm (clean) | (withheld) | **environment** | an upstream UI behaviour (a value refreshes only after saving); device text on the clean build |
| F4 | H17 (seeded) | unmatched (seeded) | (withheld) | **artifact** | thoroughness: the already-credited defect, seen again on a second screen |
| F4 | `contacts-new-contact-survives-rotation~seeded` | unmatched (seeded) | obs `No contacts found` — the contacts list is empty; Alice is not listed | **artifact** | thoroughness: empty list = consequence of the found new-contact-lost-on-rotation |
| F4 | `medtimer-check-stock~seeded` | unmatched (seeded) | obs `Ibuprofen (10 left, After 10/14/26) / 0 reminders / Inactive` — The Medicine list says Ibuprofen has 0 reminders, but opening Ibuprofen shows one reminder card (8:00 AM, Dosage 2, Ina… | **environment** | 0 reminders (fixture) |
| F4 | `medtimer-check-stock~clean` | false alarm (clean) | obs `0 reminders` — The Medicine list row for Ibuprofen says '0 reminders' / 'Inactive', but opening Ibuprofen shows one reminder card in t… | **environment** | 0 reminders (fixture) |

#### Astra

| run | episode | recorded | the report (public: quoted; held-out: withheld) | class | evidence / device result |
| --- | --- | --- | --- | --- | --- |
| A2 | `medtimer-add-reminder~seeded` | unmatched (seeded) | obs `8:00 AM` — After entering dosage 1, keeping 8:00 AM, and tapping Create reminder, MedTimer disappeared and the Android home screen… | **artifact → credits `new-reminder-time-crash`** | crash wording: 'MedTimer disappeared and the Android home screen appeared' right after Create reminder; device result = launcher ('At a glance','Wed, Sep 16','Gmail'); f… |
| A2 | `medtimer-add-medicine-back-to-list~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — After entering Lisinopril and confirming with OK, the medicine details screen appeared. Tapping its Navigate up arrow r… | **artifact → credits `medicine-list-empty-reminders-crash`** | crash wording: Navigate up 'returned to the Android home screen'; launcher in device result; fault fired |
| A2 | `medtimer-correct-dose-amount~seeded` | unmatched (seeded) | obs `Ibuprofen (2.5)` — After changing Dosage from 2.5 to 3 and closing the editor with its back arrow, reopening MedTimer showed the logged do… | **artifact** | thoroughness: dose still 2.5 after relaunch = consequence of the crash reported in idx 1 |
| A2 | `medtimer-correct-dose-amount~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — Tapping the editor's back arrow after changing the dosage exited MedTimer to the Android home screen instead of returni… | **artifact → credits `dose-edit-save-crash`** | crash wording: editor back arrow 'exited MedTimer to the Android home screen'; launcher in device result; clean arm returns to Overview |
| A2 | `anki-browse-new-deck~seeded` | unmatched (seeded) | obs `3 cards due` — Selecting French in the Card browser's Select deck dialog returned to the main AnkiDroid deck list, showing French and … | **artifact → credits `browser-empty-deck-crash`** | crash wording: selecting French 'returned to the main AnkiDroid deck list' (process restarted at its root: 'AnkiDroid','3 cards due'); fault fired; clean arm stays in th… |
| A2 | `anki-study-first-card~seeded` | unmatched (seeded) | obs `3 cards due` — After the reviewer displayed uno, tapping Show answer returned to the deck list showing Spanish and 3 cards due. No ans… | **artifact → credits `reviewer-show-answer-crash`** | crash wording: Show answer 'returned to the deck list' (root screen); fault fired |
| A2 | H1 (clean) | false alarm (clean) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| A2 | H2 (seeded) | unmatched (seeded) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| A2 | `cal-open-task-from-list~seeded` | unmatched (seeded) | obs `Laundry` — Tapping Laundry leaves the event list on screen. A second tap on the visible title produces the same result. No task sc… | **artifact → credits `task-row-opens-event-editor`** | wording (owner condition 2, six-case list): 'Tapping Laundry leaves the event list on screen'; list unchanged in device result after two taps; fault fired |
| A2 | `cal-search-event~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — After entering Dentist and tapping Save, acknowledging the reminder disclaimer returned to the calendar. Opening search… | **artifact → credits `search-results-off-main-thread`** | tense artifact: 'caused the app to disappear' — symptom list carries 'disappeared' only; launcher in device result |
| A2 | `cal-complete-task~seeded` | unmatched (seeded) | obs `Mark completed` — After tapping Mark completed, returning to the event list, and reopening Taxes, the task screen still offers Mark compl… | **artifact → credits `task-complete-crash`** | wording: the fault is on a background thread, so the only visible effect is the completion not recorded; 'still offers Mark completed instead of an action to mark it inc… |
| A2 | `cal-create-all-day-event~seeded` | unmatched (seeded) | obs `No items found.` — After entering Vacation, enabling All-day, tapping Save, and acknowledging the reminder disclaimer, the event list show… | **artifact → credits `all-day-save-crash`** | wording: background crash, visible effect = event not saved ('No upcoming events', search 'No items found.'); clean arm lists Vacation |
| A2 | `tasks-add-subtask~seeded` | unmatched (seeded) | obs `2` — After saving and reopening the app, Pack for trip still shows only Passport and Chargers with a subtask count of 2. Tic… | **artifact** | thoroughness: Tickets missing after relaunch = consequence of the crash in idx 1 |
| A2 | `tasks-add-subtask~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — Tapping Save after entering Tickets unexpectedly exits the app to the Android home screen. | **artifact → credits `subtask-filed-before-written`** | crash wording: Save 'unexpectedly exits the app to the Android home screen'; launcher in device result |
| A2 | `tasks-complete-repeating~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — Tapping the completion control in front of Water plants closed the app and returned to the Android home screen, which d… | **artifact → credits `repeat-complete-crash`** | crash wording: completion 'closed the app and returned to the Android home screen'; launcher in device result |
| A2 | `contacts-new-contact-survives-rotation~clean` | false alarm (clean) | obs `10:00` — After rotation, the favorite star overlapped the status-bar clock and the camera icon overlapped the status icons. Alic… | **environment** | landscape: favourite star over the status-bar clock, camera over status icons; upstream layout, visible on the clean build (QUA-2785 row 3) |
| A2 | `contacts-new-contact-survives-rotation~seeded` | unmatched (seeded) | obs `10:01` — After rotation, the favorite star overlapped the status-bar clock area and the camera control overlapped the battery ar… | **environment** | same landscape overlap |
| A2 | `contacts-favorite~seeded` | unmatched (seeded) | obs `A` — After entering Alice in the first-name field and saving, the contact row displayed only an avatar with A; no contact na… | **environment** | contact row drawn with avatar 'A' and no name: evidence/screens/0006.jpg shows it; hierarchy has item_contact_frame/item_contact_image and no name node on three reads. N… |
| A2 | `contacts-view-details~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — Alice was saved and appeared in the contacts list. Tapping Alice returned to the Android home screen instead of opening… | **artifact → credits `contact-without-number-open-crash`** | crash wording: tapping Alice 'returned to the Android home screen'; launcher in device result; fault fired |
| A2 | H13 (clean) | false alarm (clean) | (withheld) | **environment** | an upstream UI behaviour (a value refreshes only after saving); device text on the clean build |
| A3 | `orgzly-nest-notes-deeper~seeded` | unmatched (seeded) | obs `Gmail` — Tapping Done after entering Level six closed Orgzly Revived and returned to the Android home screen, which displayed Gm… | **artifact → credits `deep-outline-indent-crash`** | crash wording: Done 'closed Orgzly Revived and returned to the Android home screen' (launcher in device result), reopening the notebook returned home again; fault fired |
| A3 | `medtimer-add-reminder~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — After entering dosage 1, keeping 8:00 AM, and tapping Create reminder, MedTimer closed to the Android home screen. Afte… | **artifact → credits `new-reminder-time-crash`** | crash wording: 'MedTimer closed to the Android home screen'; launcher in device result; fault fired |
| A3 | `medtimer-add-medicine-back-to-list~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — After confirming Lisinopril with OK, its detail screen appeared. A text-targeted tap intended for Medicine opened Medic… | **artifact → credits `medicine-list-empty-reminders-crash`** | crash wording: tapping the Medicine tab 'closed the app to the Android home screen'; launcher; fault fired |
| A3 | `medtimer-correct-dose-amount~seeded` | unmatched (seeded) | obs `Ibuprofen (2.5)` — The editor showed dosage 3, but after closing it and reopening the app, the logged dose still showed 2.5. | **artifact** | thoroughness: dose still 2.5 = consequence of the crash in idx 1 |
| A3 | `medtimer-correct-dose-amount~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — Tapping the back arrow in Edit event after changing the dosage to 3 unexpectedly exited MedTimer to the Android home sc… | **artifact → credits `dose-edit-save-crash`** | crash wording: back arrow 'unexpectedly exited MedTimer to the Android home screen'; launcher; fault fired |
| A3 | `cal-create-all-day-event~seeded` | unmatched (seeded) | obs `No items found.` — After entering Vacation, enabling All-day, tapping Save, and accepting the reminder disclaimer, the simple event list s… | **artifact → credits `all-day-save-crash`** | wording: background crash, visible effect = not saved ('No upcoming events.', 'No items found.'); fault fired |
| A3 | `cal-open-task-from-list~seeded` | unmatched (seeded) | obs `Laundry` — Tapping Laundry left the event list on screen. Retrying by tapping the task row also left the list unchanged; no task s… | **artifact → credits `task-row-opens-event-editor`** | wording (owner condition 2): 'Tapping Laundry left the event list on screen'; list unchanged after two taps; fault fired |
| A3 | H2 (clean) | false alarm (clean) | (withheld) | **environment** | a confirmation dialog when leaving an unmodified edit screen; in the device text, on the clean build too (QUA-2785 row 17) |
| A3 | `anki-browse-new-deck~seeded` | unmatched (seeded) | obs `AnkiDroid` — Selecting French in the Card browser deck picker returned to the main deck list, showing French and Spanish under AnkiD… | **artifact → credits `browser-empty-deck-crash`** | crash wording: 'returned to the main deck list' (process restarted at root); fault fired |
| A3 | `anki-study-first-card~seeded` | unmatched (seeded) | obs `3 cards due` — The reviewer showed uno. Tapping Show answer returned to the deck list showing Spanish and 3 cards due instead of displ… | **artifact → credits `reviewer-show-answer-crash`** | crash wording: Show answer 'returned to the deck list'; fault fired |
| A3 | `tasks-add-subtask~seeded` | unmatched (seeded) | obs `2` — Tickets was not saved. After tapping Save, the app exited to the Android home screen. Reopening Tasks.org showed Pack f… | **artifact → credits `subtask-filed-before-written`** | crash wording: 'After tapping Save, the app exited to the Android home screen' + Tickets missing; fault fired |
| A3 | `tasks-complete-repeating~seeded` | unmatched (seeded) | obs `Due today` — Tapping the completion control in front of Water plants closed Tasks.org and returned to the Android home screen. After… | **artifact → credits `repeat-complete-crash`** | crash wording: completion 'closed Tasks.org and returned to the Android home screen'; fault fired |
| A3 | `contacts-view-details~seeded` | unmatched (seeded) | obs `Wed, Sep 16` — Alice was saved and appeared in the contacts list. Tapping Alice returned to the Android home screen, showing the date … | **artifact → credits `contact-without-number-open-crash`** | crash wording: tapping Alice 'returned to the Android home screen'; launcher; fault fired |
| A3 | `contacts-new-contact-survives-rotation~clean` | false alarm (clean) | obs `Alice` — Alice remained in the first-name field, but after rotation the favorite and camera icons overlapped the status-bar area… | **environment** | landscape overlap of header icons with the status bar (upstream layout, clean build; visible in QUA-2785 screenshot and Astra run 2); observe results carry a screenshot … |
| A3 | H14 (clean) | false alarm (clean) | (withheld) | **environment** | an upstream UI behaviour (a value refreshes only after saving); device text on the clean build |
| A3 | H15 (seeded) | unmatched (seeded) | (withheld) | **artifact → credits the blocking defect** | honest, device-backed report of the case's blocking defect (three reads show it); unmatched on tense alone (the list carries the present tense, the r… |
| A3 | H18 (seeded) | unmatched (seeded) | (withheld) | **artifact** | thoroughness: the already-credited defect, seen again on a second screen |

### Owner condition 2 — the six wording-sensitive cases, seeded arms

| case | F1 | A2 | A3 | F4 |
| --- | --- | --- | --- | --- |
| `anki-open-card-from-browser` | credited | credited | credited | credited |
| `cal-switch-back-to-list` | credited | credited | credited | credited |
| `cal-open-task-from-list` | credited | **honest, uncredited** (hand-credited) | **honest, uncredited** (hand-credited) | credited |
| `medtimer-check-stock` | credited | credited | credited | credited |
| `orgzly-open-note-from-notebook` | credited | credited | credited | credited |
| `orgzly-new-note-survives-rotation` | credited | credited | credited | credited |

### Owner condition 1 — credited display defects and their device text

| run | credited display defects | backed by the episode's own device text | not backed |
| --- | --- | --- | --- |
| F1 (public) | 11 | 11 | none |
| F1 (held-out) | 6 | 6 | none |
| A2 (public) | 11 | 11 | none |
| A2 (held-out) | 6 | 6 | none |
| A3 (public) | 11 | 11 | none |
| A3 (held-out) | 5 | 5 | none |
| F4 (public) | 11 | 11 | none |
| F4 (held-out) | 5 | 5 | none |

### 6.1 Misses that are not wording

| episode | runs | what happened | class |
| --- | --- | --- | --- |
| `orgzly-create-and-search~seeded`, `notebook-count-off-by-one` | all four (both models) | the first screen read shows `Contains 32 notes` (clean: `33`), and no model remarks on it. The brief gives no reference for the count: the lint's `reference` warning, deferred to QUA-2768. One of the five weak-witness cases | **corpus** (known, QUA-2768) |
| held-out seeded episodes | 2 episodes: F4 1, A3 1 | one miss in each of those two runs; each defect was credited in the other three runs. Adjudicated from the transcripts against the split; the details stay with the split (docs/heldout.md) | **real miss** ×2 (capability events, not scorer rows) |

### 6.2 Owner condition 1: grounding of every credited display/content defect

- **Display defects:** all 66 credits across the four runs (44 public, 11 per run; 22 held-out, 5–6 per run) are backed by a measured marker or display text found in that episode's own device results (`journey._device_texts(results_only=True)`).
- **Eight held-out functional credits (4 Fable, 4 Astra) rest on a quote:** all eight are backed by that episode's own device results. Fable's four were confirmed by hand, because the scorer's grounding flag cannot confirm a quote that joins two device strings.
- **Removed from the corrected numbers: none, for either model.** The table above shows the per-run counts.

### 6.3 Owner condition 2: the six wording-sensitive cases

- **Fable:** credited on all six in both runs. On `cal-open-task-from-list~seeded` it wrote "never opens" / "list stays on screen" (QUA-2796's added wordings).
- **Astra:** credited on five of six in both runs. `cal-open-task-from-list~seeded` was uncredited in both A2 and A3. Its reports, quoted: "Tapping Laundry leaves the event list on screen", "Tapping Laundry left the event list on screen". The device result shows the list unchanged after two taps, and the fault fired.
- **Adjudication:** hand-credited identically for both models. The symptom list stayed frozen.
- **Honest-but-uncredited reports on these six cases:** Fable 0, Astra 2.

### 6.4 Truncations

**One, A3 `contacts-favorite~clean`, at 41/40:**
- **What happened:**
  - Astra typed "Alice" with `mobile_type_text` into the focused field, which the upstream focus quirk makes Address.
  - It saved, opened the contact, and said so: "The name was entered into the address field because I hadn't focused the first-name field. That was a test interaction error."
  - It deleted the contact, redid the flow correctly and was cut on the last favourite step, with no report written.
- **Verdict:** **runaway by self-inflicted rework, not budget-bound.** The clean route costs 9–15 steps on both models (13 other episodes of this case, all finished at ≤ 23), and no cap change is proposed.
- **Pricing:** codex writes its only `turn.completed` at the end of the turn, so this SIGKILLed episode has no usage at all (**QUA-2803**).

Fable had no truncations.

## 7. Rates, raw and corrected, per model and per run

**What the corrected columns change:**
- **"Environment excluded"** drops the `environment` reports and asks again whether the clean episode carried any false report; this is the ticket's correction.
- **"Adjudicated"** adds the hand credits of §6 (owner condition 2 and the same rule for crash wording) and removes any credit condition 1 rejects (none).

| | Fable F1 | Fable F4 | Astra A2 | Astra A3 |
| --- | --- | --- | --- | --- |
| false alarm, public, raw | 5/41 = 12.2% [5.3–25.5] | 2/41 = 4.9% [1.3–16.1] | 1/41 = 2.4% [0.4–12.6] | 1/41 = 2.4% [0.4–12.6] |
| **false alarm, public, env-excluded** | **0/41 = 0% [0–8.6]** | **0/41 = 0% [0–8.6]** | **0/41 = 0% [0–8.6]** | **0/41 = 0% [0–8.6]** |
| false alarm, held-out, raw → env-excluded | 3/10 → 0/10 [0–27.8] | 2/10 → 0/10 | 2/10 → 0/10 | 2/10 → 0/10 |
| integrity @200, public, raw → env-excluded | 0% [0–0] → 100% [0–100] | 0% [0–7] → 100% [0–100] | 1% [0–42] → 100% [0–100] | 1% [0–42] → 100% [0–100] |
| catch, public, raw | 39/40 = 97.5% [87.1–99.6] | 39/40 | 27/40 = 67.5% [52.0–79.9] | 28/40 = 70.0% [54.6–81.9] |
| **catch, public, adjudicated** | **39/40 = 97.5% [87.1–99.6]** | **39/40** | **39/40 = 97.5% [87.1–99.6]** | **39/40** |
| catch, held-out, raw → adjudicated | 10/10 → 10/10 | 9/10 → 9/10 | 10/10 → 10/10 | 8/10 → 9/10 |
| completion, public, raw → adjudicated | 80/80 → 80/80 | 80/80 → 80/80 | 68/80 → 80/80 | 68/80 → 79/80 |
| completion excl. QUA-2768 five, raw | 70/70 | 70/70 | 58/70 | 58/70 |

**Why the corrected rate leads.** The ranking key (QUA-2780) exists to penalise an agent that invents failures, because a nightly suite at a few percent bug prior is dominated by false alarms. Neither model invented one:
- Fable's seven raw public false alarms are the MedTimer inactive-reminder fixture (5) and the Contacts Address-cursor quirk (2).
- Astra's two are the Contacts landscape overlap, twice.
- Ranked raw, the board rewards whichever model noticed fewer true things.

**Why the adjudicated catch is shown beside the raw one, and why the raw one stays published.** The raw catch gap is 21 honest crash reports and two honest dead-tap reports. They are in the scorer's blind spot, because every crash symptom list is crash vocabulary and one list is tense-exact. The scorer change that would make the adjudicated number reproducible is **QUA-2802**; until it lands, the raw row is what the harness computes.

## 8. Cost and wall time

| run | model | priced cost | $/ep | cost source | run wall time | lane time | median agent min |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | Fable | **$134.07** ($105.75 public + $28.32 held-out) | $1.34 | 100 `reported` | 2 h 54 m 00 s (23:28:24Z–02:22:24Z) | 2.90 h | 1.1 |
| A2 | Astra | **$85.95** ($68.15 + $17.80) | $0.86 | 100 `estimated` | 2 h 09 m 47 s (02:22:45Z–04:32:32Z) | 2.16 h | 0.7 |
| A3 | Astra | **$84.61** ($65.96 + $18.65) + 1 unpriced | $0.85 | 99 `estimated`, 1 `unavailable` | 2 h 13 m 14 s (04:32:55Z–06:46:09Z) | 2.22 h | 0.8 |
| F4 | Fable | **$129.33** ($101.91 + $27.42) | $1.29 | 100 `reported` | 2 h 55 m 50 s (06:46:27Z–09:42:17Z) | 2.93 h | 1.1 |

- **Per model:**
  - Fable **$263.40** for 200 episodes.
  - Astra **$170.56** priced for 199, plus 1 unpriced (QUA-2803).
  - Astra's cache writes cost $12.50/MTok against the $10 input rate the table uses, which `pricing.PRICING` does not model (CLAUDE.md). Adding that premium from the transcripts' `cache_write_input_tokens` gives A2 +$10.59 and A3 +$10.46, so Astra totals **≈ $191.61** (≈ $0.96 per episode).
  - Astra's tokens: 85.8 M input, 90.2 % cache reads, 179 k output.
- **Phase A:** $13.50 + $12.63 = $26.13, plus a ~12.6 k-token codex banner probe (under $0.15).
- **Total spend: $460.09 priced** (≈ $481 with the cache-write premium), inside the $600–1,100 authorisation.
- **Fable window:** the subscription's weekly window read 64% after F1 and 78% after F4, with other usage on the account included.
- **Wall time:** 10 h 12 m 51 s across the four runs, one lane, plus 41 min for the two Phase A runs.

## 9. Power, and which differences are outside the intervals

Intervals count trials as draws, so power comes from distinct cases (CLAUDE.md, `rates.py`). This board has 41 public clean cases, 40 public seeded defects, 10 held-out clean cases and 10 held-out defects per trial.

**What 41 + 10 cases can resolve:**
- 41 cases cannot separate two rates closer than about 10 points. A 0/41 bounds a rate only at 8.6%, and ±5 pp at 15% needs about 200 distinct cases.
- Pooling the two trials per model (n = 82) narrows the brackets on paper only.
- The held-out block, at 10 cases, resolves only about ±25 pp. It can show a gross public/held-out gap, and there is none for either model.

**Is each headline model difference outside the intervals?**

| rate | Fable | Astra | outside the intervals? |
| --- | --- | --- | --- |
| false alarm, env-excluded, public / held-out | 0/82 [0–4.5] / 0/20 [0–16.1] | 0/82 [0–4.5] / 0/20 [0–16.1] | **no** (identical) |
| false alarm, raw, public | 8.5% [4.2–16.6] | 2.4% [0.7–8.5] | **no**: the brackets overlap pooled, and per run (5/41 [5.3–25.5] and 2/41 [1.3–16.1] against 1/41 [0.4–12.6]) |
| false alarm, raw, held-out | 25% [11.2–46.9] | 20% [8.1–41.6] | **no** |
| catch, adjudicated, public / held-out | 97.5% [91.3–99.3] / 95% [76.4–99.1] | 97.5% [91.3–99.3] / 95% [76.4–99.1] | **no** (identical) |
| catch, raw, public | 97.5% [91.3–99.3] | 68.8% [57.9–77.8] | **yes**, and per run too (39/40 [87.1–99.6] against 27/40 [52.0–79.9] and 28/40 [54.6–81.9]). **It is an artifact** (§6, §7): it disappears under one rule applied to both models |
| blocker recall, raw → adjudicated | 100% [92.9–100] → same | 54% [40.4–67.0] → 100% [92.9–100] | raw yes (same artifact), adjudicated **no** |
| completion, raw → adjudicated, public | 100% → 100% | 85.0% → 99.4% | raw yes (same artifact plus one truncation), adjudicated **no** |
| $/episode, min/episode | $1.30, 1.1 min | $0.84 (≈ $0.96), 0.7 min | not interval quantities. Astra is cheaper and faster on every app, in both of its runs |

**Order and drift:**
- **F1 vs F4 (Fable, the ABBA ends).**
  - Catch: 39/40 and 39/40.
  - Held-out: 10/10 and 9/10, the difference being one real miss (§6.1).
  - Completion: 100% both.
  - Cost: $134.07 and $129.33.
  - The set of defects found per seeded episode is identical on 48 of 49 seeded episodes.
  - Raw false alarms fell from 5/41 to 2/41. All seven are environment rows: four clean episodes carry one in both runs, and four only in F1 (the Contacts cursor twice, the MedTimer fixture, a held-out upstream behaviour). Fable mentions true quirks at random; it does not drift.
- **A2 vs A3 (Astra).**
  - Catch raw: 27 and 28; adjudicated 39 and 39.
  - False alarms: 1/41 both, with the same three clean episodes (public + held-out) in both runs.
  - Found sets identical on 44/49. The five differences: three public crash episodes whose credit turned on wording in one run only (`cal-complete-task`, where A3 quoted the "keeps stopping" dialog; `cal-search-event`, where A3 wrote "closed" and A2 "disappear"; `orgzly-nest-notes-deeper`, where A2 quoted the dialog on relaunch), and two held-out episodes (one tense row, one real miss).
  - One truncation in A3.
- **Conclusion:** nothing maps onto run order. The one AVD boot served all runs, and 400/400 episodes started from a verified clean device.

**Bottom line.** On 41 + 10 cases, the only model difference outside the intervals is one the scorer manufactures. Adjudicated, the models are indistinguishable on false alarms, catch, blocker recall and completion. They differ in cost, speed and diagnostic depth: Fable names the crash, Astra describes its effect. Separating them on rates would take a corpus several times larger (the deferred ~200-case expansion), and QUA-2802 first.

## 10. Follow-ups

Each ticket is related to QUA-2773. None is a child, and none blocks this ticket.

| ticket | covers | rows |
| --- | --- | --- |
| **QUA-2802** (new, High): credit honest "app closed to the home screen" crash reports, since the crash symptom vocabulary misses the effect wordings | 21 Astra crash rows (A2 11, A3 10); the 2 `cal-open-task-from-list` dead-tap rows (owner condition 2); the held-out tense row (A3). Options: extend the frozen lists after this ticket, or stem-match, or a structural launcher-after-fault credit | every `→ credits` row in §6 |
| **QUA-2803** (new, Medium): truncated codex-cli episodes publish no usage, because the single `turn.completed` is never written | pricing artifact; also corrects CLAUDE.md's "codex was never affected" | A3 `contacts-favorite~clean` |
| **QUA-2801** (merged before Phase B): the scorer decodes `\uXXXX` in MCP results | Phase A parse asymmetry | Phase A |
| **QUA-2779** (existing, deferred): unadjudicated bucket | every `environment` row (Fable 24, Astra 9), and the thoroughness charge (an artifact whose defect is already credited in the episode): Fable 14, Astra 4 | all `environment` rows; the `thoroughness` artifact rows |
| **QUA-2768** (existing): the five weak-witness cases | `orgzly-create-and-search`'s count side bug has no reference in the brief and is missed by both models in all four runs | §6.1 |

Not ticketed, noted for the owner:
- MedTimer's inactive seeded reminder is again the most frequent false report: 10 of the 27 Fable public false reports, and 0 of Astra's.
- The Fossify Contacts Address-cursor quirk interacts with `mobile_type_text` and cost Astra a truncation.

Both are corpus/fixture edits, out of scope here.

## 11. Acceptance criteria

| criterion (QUA-2786 + owner conditions) | result | evidence |
| --- | --- | --- |
| order Fable, Astra, Astra, Fable; same emulator image; same `corpus_version`/`heldout_version`; device invariant on; budgets from the calibration ticket | **PASS** | header table; §3: 400/400 on every stamp |
| Astra preflight: codex accepts the id, `usage_source`/`cost_source` measured, DevLoop tools in the transcript; wider Phase A per owner | **PASS** | §1 (two preflights, codex upgrade, QUA-2801) |
| every false/unmatched report classified real/artifact/corpus/environment with evidence; both rates raw and environment-excluded with intervals; the doc leads with the corrected one and says why | **PASS** | §6 (all 75 unmatched reports, 0 unclassified, 0 `real`), §7, the top table |
| four run ids with matching corpus/held-out/brief; zero `env_failure`, or each explained | **PASS** | §3: 0 env/infra failures, 0 `staging_failed` |
| every rate quoted with its interval, and whether the difference is outside the intervals | **PASS** | §9 table |
| `validate_bundle.py` green on every episode; contamination scan clean (incl. `devloop_artifacts`) | **PASS** | §3: 400/400 FAILURES: 0; 0 hits |
| owner condition 1: every credited display/content defect backed by the episode's own device text | **PASS** (0 removed) | §6.2 |
| owner condition 2: honest-but-uncredited reports on the six wording cases listed and adjudicated for both models | **PASS** | §6.3: Fable 0, Astra 2, both hand-credited |
| truncations read; runaway vs budget-bound | **PASS** | §6.4: 1, self-inflicted rework |
| raw adb and `metered_denied` read per model | **PASS** | §4, §3 |
| completion excluding the five QUA-2768 cases, labelled | **PASS** | top table, §7 |
| board per run and merged; public and held-out separate; rates with intervals; $/ep, min/ep; `--projection 200 50` | **PASS** | §5 |
| power statement; F1 vs F4 and A2 vs A3 | **PASS** | §9 |
| held-out: aggregates only | **PASS** | held-out rows unnamed and unquoted throughout |
| every artifact/corpus row has a follow-up ticket | **PASS** | §10 |
| cost and wall time per run and per model | **PASS** | §8 |
| spend under the $1,300 stop line | **PASS** | $460.09 priced (≈ $481) |

## Appendix: every public case, every run

Each cell is the clean arm `steps/budget`, then the seeded arm `steps/budget found/present`:
- `FRn` means n false reports;
- `✗` means not completed;
- `T` means truncated.

Held-out episodes are summarised only (docs/heldout.md). All 80 held-out episodes are stamped `10f3382a18c8`. Per run, the held-out block completed 20, 20, 19 and 20 of 20 in F1, A2, A3 and F4, and caught 10, 10, 8 and 9 of 10.

| case | F1 (Fable) | A2 (Astra) | A3 (Astra) | F4 (Fable) |
| --- | --- | --- | --- | --- |
| `anki-browse-cards` | 5/30 · 6/30 1/1 | 6/30 · 6/30 1/1 | 6/30 · 6/30 1/1 | 5/30 · 5/30 1/1 |
| `anki-browse-new-deck` | 17/65 · 29/65 1/1 | 19/65 · 18/65 0/1 FR1 ✗ | 18/65 · 19/65 0/1 FR1 ✗ | 16/65 · 28/65 1/1 |
| `anki-create-deck` | 9/35 | 10/35 | 10/35 | 10/35 |
| `anki-open-card-from-browser` | 7/35 · 16/35 1/1 FR1 | 9/35 · 9/35 1/1 | 9/35 · 9/35 1/1 | 7/35 · 17/35 1/1 |
| `anki-study-first-card` | 7/35 · 14/35 1/1 | 8/35 · 7/35 0/1 FR1 ✗ | 8/35 · 7/35 0/1 FR1 ✗ | 7/35 · 14/35 1/1 |
| `cal-complete-task` | 23/50 · 30/50 1/1 FR2 | 24/50 · 24/50 0/1 FR1 ✗ | 24/50 · 27/50 1/1 | 23/50 · 34/50 1/1 FR1 |
| `cal-create-all-day-event` | 18/45 · 23/45 1/1 | 20/45 · 25/45 0/1 FR1 ✗ | 20/45 · 23/45 0/1 FR1 ✗ | 18/45 · 20/45 1/1 |
| `cal-create-event` | 15/40 · 14/40 1/1 FR1 | 18/40 · 17/40 1/1 | 19/40 · 15/40 1/1 | 15/40 · 13/40 1/1 |
| `cal-create-task` | 16/40 · 15/40 1/1 | 20/40 · 19/40 1/1 | 20/40 · 18/40 1/1 | 15/40 · 15/40 1/1 |
| `cal-edit-event` | 29/50 · 32/50 1/1 | 28/50 · 22/50 1/1 | 24/50 · 22/50 1/1 | 25/50 · 35/50 1/1 |
| `cal-open-task-from-list` | 19/60 · 34/60 1/1 | 19/60 · 21/60 0/1 FR1 ✗ | 18/60 · 24/60 0/1 FR1 ✗ | 18/60 · 31/60 1/1 |
| `cal-repeat-survives-rotation` | 32/50 · 36/50 1/1 | 25/50 · 42/50 1/1 | 21/50 · 37/50 1/1 | 34/50 · 25/50 1/1 FR1 |
| `cal-search-event` | 18/50 · 25/50 1/1 | 15/50 · 18/50 0/1 FR1 ✗ | 15/50 · 16/50 1/1 | 18/50 · 26/50 1/1 |
| `cal-switch-back-to-list` | 20/50 · 35/50 1/1 FR1 | 20/50 · 20/50 1/1 | 20/50 · 20/50 1/1 | 21/50 · 32/50 1/1 |
| `contacts-create` | 8/35 FR1 · 8/35 1/1 FR1 | 14/35 · 11/35 1/1 | 14/35 · 11/35 1/1 | 8/35 · 8/35 1/1 |
| `contacts-create-group` | 11/40 · 20/40 1/1 | 13/40 · 14/40 1/1 | 15/40 · 15/40 1/1 | 11/40 · 19/40 1/1 |
| `contacts-delete` | 14/40 · 23/40 1/1 | 17/40 · 17/40 1/1 | 17/40 · 30/40 1/1 | 15/40 · 20/40 1/1 |
| `contacts-favorite` | 15/40 FR1 · 17/40 1/1 | 18/40 · 15/40 1/1 FR1 | 41/40 ✗ T · 18/40 1/1 | 14/40 · 22/40 1/1 |
| `contacts-new-contact-survives-rotation` | 12/45 · 16/45 1/1 FR1 | 15/45 FR1 · 19/45 1/1 FR1 | 14/45 FR1 · 17/45 1/1 | 11/45 · 17/45 1/1 FR1 |
| `contacts-phone` | 14/40 · 19/40 1/1 | 16/40 · 16/40 1/1 | 16/40 · 20/40 1/1 | 14/40 · 17/40 1/1 |
| `contacts-view-details` | 10/40 · 18/40 1/1 | 12/40 · 18/40 0/1 FR1 ✗ | 16/40 · 13/40 0/1 FR1 ✗ | 10/40 · 16/40 1/1 |
| `medtimer-add-medicine` | 13/40 · 13/40 1/1 | 15/40 · 15/40 1/1 | 14/40 · 14/40 1/1 | 13/40 · 13/40 1/1 |
| `medtimer-add-medicine-back-to-list` | 12/45 · 21/45 1/1 | 20/45 · 15/45 0/1 FR1 ✗ | 19/45 · 15/45 0/1 FR1 ✗ | 12/45 · 20/45 1/1 |
| `medtimer-add-reminder` | 17/50 FR1 · 22/50 1/1 FR1 | 18/50 · 24/50 0/1 FR1 ✗ | 17/50 · 25/50 0/1 FR1 ✗ | 18/50 · 23/50 1/1 FR1 |
| `medtimer-analysis-tabular-view` | 6/56 · 44/56 1/1 | 11/56 · 8/56 1/1 | 13/56 · 8/56 1/1 | 8/56 · 38/56 1/1 |
| `medtimer-check-stock` | 7/40 FR1 · 11/40 1/1 | 8/40 · 13/40 1/1 | 9/40 · 12/40 1/1 | 7/40 FR1 · 12/40 1/1 FR1 |
| `medtimer-correct-dose-amount` | 8/45 · 13/45 1/1 | 11/45 · 14/45 0/1 FR2 ✗ | 10/45 · 13/45 0/1 FR2 ✗ | 9/45 · 15/45 1/1 |
| `medtimer-review-aspirin` | 11/40 · 11/40 2/2 | 12/40 · 14/40 2/2 | 11/40 · 12/40 2/2 | 10/40 · 11/40 2/2 |
| `medtimer-take-dose-then-medicine-list` | 11/45 FR1 · 19/45 1/1 FR2 | 9/45 · 13/45 1/1 | 10/45 · 12/45 1/1 | 7/45 FR1 · 32/45 1/1 FR2 |
| `orgzly-complete-repeating-task` | 26/60 · 32/60 1/1 | 28/60 · 32/60 1/1 | 32/60 · 28/60 1/1 | 31/60 · 27/60 1/1 |
| `orgzly-create-and-search` | 16/45 · 16/45 0/1 | 18/45 · 18/45 0/1 | 18/45 · 18/45 0/1 | 16/45 · 16/45 0/1 |
| `orgzly-create-priority-note` | 22/60 · 28/60 1/1 | 27/60 · 28/60 1/1 | 23/60 · 28/60 1/1 | 23/60 · 26/60 1/1 |
| `orgzly-nest-notes-deeper` | 27/60 · 32/60 1/1 | 26/60 · 28/60 1/1 | 21/60 · 29/60 0/1 FR1 ✗ | 25/60 · 35/60 1/1 |
| `orgzly-new-note-survives-rotation` | 10/40 · 11/40 1/1 | 10/40 · 10/40 1/1 | 9/40 · 11/40 1/1 | 9/40 · 11/40 1/1 |
| `orgzly-open-note-from-notebook` | 5/30 · 9/30 1/1 | 6/30 · 6/30 1/1 | 7/30 · 6/30 1/1 | 5/30 · 13/30 1/1 |
| `tasks-add-subtask` | 9/45 · 16/45 1/1 FR1 | 11/45 · 13/45 0/1 FR2 ✗ | 10/45 · 13/45 0/1 FR1 ✗ | 9/45 · 13/45 1/1 FR1 |
| `tasks-change-due-time` | 16/45 · 19/45 1/1 | 14/45 · 18/45 1/1 | 15/45 · 19/45 1/1 | 16/45 · 19/45 1/1 |
| `tasks-complete-and-rename` | 19/50 | 19/50 | 20/50 | 19/50 |
| `tasks-complete-parent` | 5/40 · 6/40 1/1 | 10/40 · 18/40 1/1 | 10/40 · 16/40 1/1 | 5/40 · 7/40 1/1 |
| `tasks-complete-repeating` | 8/40 · 14/40 1/1 | 9/40 · 8/40 0/1 FR1 ✗ | 8/40 · 10/40 0/1 FR1 ✗ | 8/40 · 13/40 1/1 FR1 |
| `tasks-create-with-due-date` | 17/40 · 24/40 1/1 | 23/40 · 18/40 1/1 | 19/40 · 26/40 1/1 | 18/40 · 19/40 1/1 |
