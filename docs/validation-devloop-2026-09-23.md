# Validation run: DevLoop arm, one model, one trial (QUA-2785, 2026-09-23)

**Verdict: GO for running the second model (QUA-2786), with the four conditions in section 11.** This is the first time the DevLoop arm produced journey episodes. All 100 were staged, run, verified and scored: 0 excluded, 0 contaminated, 0 truncated, and `validate_bundle` passed on all 100. I read every transcript. None of the 20 false reports is `real`: each one is a true statement about the device, or a report the scorer failed to credit.

**The corrected numbers come first, and the raw ones are right below them.** The board ranks on clean-run integrity. Raw, integrity is 0% here, and the reason is not the agent inventing bugs. Every clean-arm false alarm (4/41 public, 2/10 held-out) is a true statement about the clean build or its fixture: an upstream UI quirk, or a seeded reminder the list does not count. A raw integrity number on this arm measures how many real upstream quirks an agent bothers to mention. The corrected rate removes the `environment` reports, the class the ticket defines, and says what the ranking key is meant to say: how often the agent reports a problem that is not there.

| DevLoop arm, corpus `afce9dfd7aa5`, held-out `ec1ed6d1dd18` | public (41 cases, 80 episodes) | held-out (10 cases, 20 episodes) |
| --- | --- | --- |
| **false alarm per clean case, environment excluded** | **0/41 = 0% [0–8.6]** | **0/10 = 0% [0–27.8]** |
| false alarm per clean case, raw | 4/41 = 9.8% [3.9–22.5] | 2/10 = 20.0% [5.7–51.0] |
| **clean-run integrity @200, environment excluded** | **100% [0–100]** | **100% [0–100]** |
| clean-run integrity @200, raw | 0% [0–0] | 0% [0–0] |
| catch per seeded defect (raw; the rows the corpus cannot credit are left in) | 39/40 = 97.5% [87.1–99.6] | 9/11 = 81.8% [52.3–94.9] |
| blocker recall (functional L4+L3) | 25/25 = 100% [86.7–100] | 2/2 = 100% [34.2–100] |
| completion | 79/80 = 98.8% (0 unscored) | 20/20 = 100% |
| precision / recall / F1 (raw) | 73.6% / 97.5% / 0.839 | 60.0% / 81.8% / 0.69 |
| false reports: real / artifact / corpus / environment | 0 / 5 / 0 / 9 (of 14) | 0 / 1 / 2 / 3 (of 6) |
| cost | **$131.34 for 100 episodes** ($104.70 public + $26.63 held-out), all `reported` | |
| wall time | **2 h 59 min** elapsed (11:43:43Z to 14:42:50Z), one lane; median agent time 1.05 min per episode | |

Brackets are 95% Wilson intervals from `rates.wilson`. An integrity interval is the rate interval pushed through `(1 − p)^200` (`rates.clean_run_integrity`). With the upper false-alarm bound at 8.6%, a 0/41 row honestly prints `100% [0–100]`.

| | |
| --- | --- |
| run | `20260923-114342-b8af`: claude-code · claude-fable-5-1 (the model reported `claude-fable-5-1`, no dated suffix), `--mode journey`, `--trials 1`, held-out required, DevLoop arm (`--mcp-server http://127.0.0.1:51841`), brief v3, emulator-5554 (`qgbench_root`, android-35), one lane |
| corpus_version | **`afce9dfd7aa5`** on 100/100 episodes (`board.json`: `mixed_corpus: false`) |
| held-out version | **`ec1ed6d1dd18`** on 100/100 episodes. It was `c8b4fa6bafb1` before this ticket re-derived the split under the clock pin (section 2) |
| harness | qualgentbench-oss `a565b5a` (epic `epic/qua-2773-journey-mcp-arm`, every QUA-2773 blocker merged). DevLoop-MCP `ec1f483` (devloop-mcp 1.27.0, QUA-2787 no-source mode), run as `uv run devloop-mcp --transport streamable-http --port 51841 --app-source none` |
| runs dir | `~/.qualgentbench/runs` (QUA-2778). `inherited_instructions: []` on 100/100. The run directory is kept |
| device clock | pinned `2026-09-16T10:00:00-05:00` on 100/100 (QUA-2781) |

## Contents

0. Scope, spend and how the run was controlled
1. Device-free gates, with and without the split
2. The held-out split under the clock pin
3. Provenance, on every episode
4. What the DevLoop arm looked like from inside the transcripts
5. The board
6. Every false report, miss and non-completion, adjudicated
7. Rates, raw and environment-excluded
8. Cost and wall time
9. Power: what 41 + 10 cases can and cannot tell apart
10. Follow-ups
11. Acceptance criteria, and go / no-go for the second model
Appendix: every episode

---

## 0. Scope, spend and how the run was controlled

**Scope.** All six public journey apps plus the two held-out apps, clean and seeded, one trial. That is **100 units**, not the 82 + held-out the ticket estimated. QUA-2783 retired the only side bug on `anki-create-deck` and on `tasks-complete-and-rename`, which left both cases clean-only (41 public cases → 41 clean + 39 seeded = 80), and the split adds 10 cases × 2 = 20. The held-out split was the owner's local copy (`QGB_HELDOUT_DIR=/Users/gyaan/Work/qualgentbench-oss/heldout`, which carries the QUA-2782 class labels), not S3.

**One lane.** CLAUDE.md publishes a parallel board only after a `--lanes 1` vs `--lanes N` step comparison on one tier. That comparison costs a second paid pass, which this ticket was not authorised to spend, so the board ran on one lane. The second AVD (`qgbench_root2`, emulator-5556) was used only for the free held-out derive and was shut down before the run.

**Credentials.** `CLAUDE_CODE_OAUTH_TOKEN` from the main checkout's `.env`, the same subscription path the 2026-09-19 board used (read by key name only). Before spending, one `claude -p --model claude-fable-5-1` probe confirmed the account could reach Fable, at $0.51 (not part of the run's total).

**The projection, stated before launch.** The last board, 83 episodes of claude-code · claude-opus-5 on the raw arm, cost $156.70, a mean of $1.89 per episode.
- The owner's rule of thumb, Fable at about 2× Opus on input and output: 100 × $1.89 × 2 = **$378**.
- Re-pricing that board's actual token mix at `pricing.PRICING` rates: 97.5% of its input tokens were cache reads, and `claude-fable-5-1` reads cache at $0.25/MTok against Opus 5's $0.50. That gives $127.92 for Fable against $133.86 for Opus 5. Scaled by that board's reported/estimated ratio (1.17) and to 100 units, it comes to **$184**.
- Stated range **$184–$378**. Both ends are under the $400 stop line and around the ~$300 authorisation.

**Spend control.** `--stop-at-seven-day-pct` is the harness's only clean stop, and it reads the subscription's weekly window, not dollars. The run therefore went in three segments on one run id, each resumed with `run --resume`:
- **segment 0:** guard at 1%. It stopped after the first episode, which was the probe for the window. The weekly window read 38%.
- **segment 1:** guard at 40%, about two points or roughly 15 episodes at the 2026-09-19 board's rate (about $15.7 per point). This is the "~10 episodes" checkpoint. It stopped cleanly at 22 episodes, $30.27 spent, window at 40%. The unit boundary held: no in-flight episode was lost and none was orphaned.
- **segment 2:** guard at 60% as a backstop, about $300 of headroom at the measured rate. It finished the remaining 78 units. The window ended at 47%.
- A scratch watcher read each finished episode's `metrics.cost_usd` and projected the total two ways: linear, and case-matched against the same cases' cost on the 2026-09-19 board.
- **At 10 episodes: $11.01 spent, projection $110.11 linear / $112.82 matched.** That is under $400, so the run continued. The projection's peak was $162 linear, at episode 2. From episode 4 on it stayed between $100 and $150.

**Re-runs.** None. No episode was `infra_failure`, `env_failure`, `rate_limited` or requeued (`schedule.jsonl`), so the harness requeue was never needed and no individual episode was re-run by hand.

## 1. Device-free gates, with and without the split

| gate | split unset | `QGB_HELDOUT_DIR` exported |
| --- | --- | --- |
| `uv run pytest` | **1745 passed, 2 skipped** (after the board, emulators off) | — |
| `ruff check --select F821` | **All checks passed!** (`uv run --with ruff`; ruff is not in the synced venv) | — |
| `lint_journey_cases.py` | **PASS**: 41 cases, 0 errors, 4 warnings (existing: 3 deferred to QUA-2768, 1 `columns` on contacts-favorite) | **PASS**: 51 cases, 0 errors, 12 warnings (the 4 above, 7 held-out "side bug has no `reference:` — held-out split, audit pending", 1 `columns`), before and after the held-out re-derive |
| `journey_adversary_check.py` | **PASS**: 5 guessers 0/40 · 0 each; honest 40/40 | **PASS**: 5 guessers 0/51 · 0 each, through findings file AND report tool; honest 51/51 · 32; `symptom-spray` paid on 51/51 clean episodes; before and after the re-derive |
| `check_tier_ready.py --tier easy` | **READY** | — |
| `holdout.py verify` | — | **OK**: the split loads and hashes, and no held-out id appears under `src/qualgentbench/data` or `tests/fixtures` |

## 2. The held-out split under the clock pin

QUA-2781 pinned the device clock and re-verified all 41 public rows under the pin. The split's truth had not been verified under it. It was derived on 2026-09-14, before the pin, with no `device_clock`, `witness`, `attempts` or `dump_stats` fields. Before spending anything:

1. **One trial per case under the default pin.** `derive_journey.py <app> --json <scratch>` ran for each held-out app, one app per emulator (5554 and 5556 in parallel; derives, not a board). Each emulator's installed journey build was hash-checked against the split's `apk:` block first. derive installs nothing, and 5556 had never seen the app, so the build had to be installed.
   - The first attempt on 5554 was thrown away. `doctor`'s own DB-oracle check touched that emulator mid-derive, so it was restarted from scratch.
   - **Every verdict field and every scored text list** (`expected`, `measured`, `blocking`, `side`, `diff`, `unclaimed_diff`, the pass outcomes) **was identical to the committed rows on 10/10 cases.** The pin moved no held-out string.
   - Every row still "differed" from its committed version on fields the committed rows predate (`dump_stats`, and `witness` where the case declares one).
   - One app's five cases came back `agrees: false`, and only because of QUA-2740's derive gate. Each declares a screen witness that is already readable before the action under test. That gate postdates the split's truth.
2. **Re-derived at `--repeat 3` into the local split**, as the ticket asks for any row that differs. Truth is written by `derive_journey.py`, never by hand.
   - **20/20 version-checks stable, every trial on its first attempt (`attempts: 1`).**
   - Verdicts and scored lists are unchanged from the committed rows.
   - The rows now carry `device_clock`, `trials`, `stability` and `witness`.
   - The five weak-witness rows stay `agrees: false`: that is the current gate's honest answer about those cases. `journey.py` reads `agrees` only as the metadata `truth_agrees`, so no score depends on it.
   - The weak witnesses themselves are a corpus defect for the split's owner (**QUA-2789**).
3. Lint and adversary were re-run with the split exported after the re-derive: both PASS (section 1).

**Held-out version: `c8b4fa6bafb1` → `ec1ed6d1dd18`.** The previous truth files are kept outside the repo. Nothing held-out is in this commit.

## 3. Provenance, on every episode

Read from each `result.json`, `evidence/meta.json`, `adb_counts.json` and `interactions.json` in the run.

| check | result |
| --- | --- |
| episodes | **100/100** planned, 100 distinct units, all `attempt: 1`; segments 0/1/2 = 1/21/78 |
| `brief_version` | **3** on 100/100 (the MCP-arm note with the report-of-record sentence, QUA-2777) |
| `dump_stats` | `{'builtin': N}` on 99/100. One `builtin_killed: 1, u2: 1` (`medtimer-analysis-tabular-view~clean`, the harness's own reads around its `stuck:` probe: costs time, not a verdict, per CLAUDE.md). `u2_stopped: []` on 100/100 |
| `metered_denied` | **0** on 100/100 |
| `cost_source` / `usage_source` | `reported` / `result` on 100/100. No `estimated`, `unpriced` or `unavailable` |
| device-state invariant | passed on 100/100. No `staging_failed`, no `env_failure`: the QUA-2781 episode-start check found a clean device every time |
| `device_clock` | `2026-09-16T10:00:00-05:00` on 100/100 |
| `failure_class` / contamination | `None` / none on 100/100. **0 voided episodes.** `inherited_instructions: []` on 100/100 |
| `corpus_version` / `heldout_version` | `afce9dfd7aa5` / `ec1ed6d1dd18` on 100/100; 80 public + 20 `heldout: true` |
| installed build | `evidence/meta.json apk_sha256` equals the case file's `apk:` sha256 for every app on 100/100 |
| `report_source` | `findings_file` on 100/100 |
| `report_tool` (DevLoop's `mobile_report_result`) | called in 2 episodes (`anki-browse-new-deck~seeded`, `contacts-create-group~seeded`), refused 0, **used as the report 0 times**. The findings file always outranked it, as QUA-2777 designed. Both calls said FAIL, the same as their findings file |
| `validate_bundle.py` | **FAILURES: 0 on 100/100** (screens byte-exact against transcript blobs, 0 order/name mismatches) |
| steps | `metrics.steps` equals `interactions.json` on 96/100. The other 4 are one short, because the agent's last call was a device call made after the findings file was written and the hook counter lags by one call (QUA-2791; diagnostic only, no truncation) |

**No-source mode.** One server process served the whole run. Its start-up line reads `App source: none — FAIL does not need code_investigation; no source-reading or fix-loop guidance is served`, and an MCP `initialize` against it returns instructions (19,617 chars, sha256 `1746b591…`) whose first line is `APP SOURCE: none.` claude-code's stream-json does not echo server instructions into the transcript, so the per-episode check is behavioural. **No episode went looking for app source on the host:**
- 0 `Read`, `Glob`, `Grep` or `WebFetch` calls in 100 transcripts.
- Every `Bash` call writes `findings.yaml` (100), or is an `adb` command against the device (15 commands in 7 episodes, section 4).
- No agent called any `qg_*` tool. The standalone server does not serve them, although its instructions mention them (QUA-2792).

## 4. What the DevLoop arm looked like from inside the transcripts

I read all 100 transcripts end to end: the agent's narration, every tool call and result, the findings file and the scored metrics. I opened the screenshots wherever a report's evidence was an image.

- **Tool mix** (100 episodes, 1,582 tool calls): `mobile_tap_and_observe` 442, `mobile_observe_screen` 392, `mobile_tap` 208, `ToolSearch` 125 (claude-code's deferred-tool loader, not a device call), `Bash` 115, `mobile_type_text` 57, `mobile_find_views` 46, `mobile_press_button` 34, `mobile_await_screen_idle` 27, `mobile_edit_field` 26, `mobile_crash_logs` 20, `mobile_launch_app` 20, `mobile_swipe` 16, `mobile_set_orientation` 12, `mobile_device_logs` 11, the rest ≤ 9.
- **Every episode read the screen as text.** DevLoop's `observe_screen` returns the element list as JSON, so there were 0 unscored completions. The 2026-09-19 raw board had 14, because its dump was dead.
- **Grounding: 62 of 83 reports grounded.** The 21 ungrounded ones fall into three groups:
  - quotes that join several nodes (`Savings $ 0.00`, a label and its value) or paste a multi-line crash stack from `mobile_crash_logs`;
  - a toast, which is never in the hierarchy;
  - three whose on-screen form carries double spaces (`TODO  #B  Book flights`) or a narrow no-break space (`9:00 AM`). These have the same cause as row 16 (QUA-2788).

  Not grounding cost no credit on this board: every ungrounded report that described a seeded defect was matched by another route.
- **The agent kept to the task.** The pattern was: follow the steps, re-check a surprising state once (re-observe, retry the tap, reopen the record), read crash logs after a crash, write the findings file. Episode times were short: median 1.05 min, longest 3.2 min.
- **Raw adb on the MCP arm: 7 episodes, 15 commands.** Every one went through the harness's adb meter and was charged as a step. None hit a deny rule.
  - `adb shell date`: 4 episodes. The agent's own context date (the host's 2026-09-23) disagreed with the pinned device date (09-16), and the agent checked which was right before deciding a date was not a bug. That is correct behaviour, and it is a small, real cost of the pin: an agent will see that mismatch in every episode.
  - `adb logcat` / `dumpsys` / `/data/anr` reads on the two ANR cases.
  - `content query` of the contacts provider (a legitimate check).
  - **`medtimer-analysis-tabular-view~seeded` ran `adb shell su 0 sh -c '…'` to read `/data/anr` traces as root.** It read nothing from the answer key, but `su` on the `google_apis` image opens a root path that `deny_reason`'s literal-path rules and the episode-start "shell not root" check do not see. The trace it read first was from an earlier run on that AVD, so `/data/anr` also survives the reset (**QUA-2790**).
- **DevLoop-side observations**, none of which changed a verdict (QUA-2792):
  - `mobile_device_logs` failed twice with `adb shell pidof <pkg> failed`, both times right after the app crashed. The agent fell back to `mobile_crash_logs`.
  - DevLoop's own input method shows its bar (`Clear Text / Next / Switch IME`) at the bottom of every screen that has a focused field.
- **Watch items from the epic's workers:**
  - `report_source` / `report_tool`: as in section 3.
  - QUA-2780: the model id carried no dated suffix.
  - QUA-2777: codex was not in this run.
  - QUA-2787: covered by the no-source paragraph above.
  - QUA-2778: 0 voided episodes.

## 5. The board

`qualgent-bench show --run 20260923-114342-b8af --agent claude-code --mode journey`, with the ranking and rates tables condensed to one line per row:

```text
 # Agent + Model                  Arm Episodes Cut Integrity Done clean Done seeded Completion Bugs found False rep. Prec. Recall  F1 Steps  $/ep min/ep
 1 claude-code · claude-fable-5-1 mcp       80   0 0% (4/41)      40/41       39/39        99%      39/40         14   74%    98% 84%    17 $1.31    1.0
corpus afce9dfd7aa5 · brief v3
 H1 claude-code · claude-fable-5-1 mcp      20   0 0% (2/10)      10/10       10/10       100%       9/11          6   60%    82% 69%    18 $1.33    1.2
held-out ec1ed6d1dd18 · brief v3

Rates   1  false alarm 4/41 10% [4–23]   catch 39/40 98% [87–100]   integrity@200 0% [0–0]   blocker recall 25/25 100% [87–100]
Rates  H1  false alarm 2/10 20% [6–51]   catch 9/11 82% [52–95]     integrity@200 0% [0–0]   blocker recall 2/2 100% [34–100]
```

**Catch by class** (public seeded arms, per defect): crash 11/11, display/content 10/11, persistence 6/6, navigation 5/5, lifecycle 3/3, ordering 2/2, ANR/freeze 2/2. By app: ankidroid 4/4, fossify-calendar 9/9, fossify-contacts 7/7, medtimer 9/9, orgzly 5/6, tasksorg 5/5.

**Against the 2026-09-19 raw board, for orientation only.** It is not a comparison: the model, the arm and the brief all differ.
- Completion: 91.2% with 14 unscored → 98.8% with 0 unscored.
- Catch: 36/43 → 39/40. The corpus changed between the boards: `corpus_version` `75649db79d50` → `afce9dfd7aa5`.
- Mean steps: 24.4 → 17.4.
- Truncations: 4 → 0.
- Cost: $156.70 for 83 episodes → $104.70 for the 80 public episodes.
- Wall time: 5 h 06 min elapsed → 2.38 h of summed per-episode lane time (`provenance.lane_wall_sec`) for the public episodes.
- The largest single cause is that the agent's screen reads worked. The raw board's hierarchy dump was dead and cost it 4.5 steps per episode, and QUA-2741 fixed that since.
- The one thing that got worse is the raw false-alarm rate, 0/41 → 4/41, and section 6 shows why.

**Budgets (for QUA-2784).** Nothing was truncated. The highest use was 40/45 (`cal-open-task-from-list~seeded`, 89%) and 37/40 (`medtimer-analysis-tabular-view~seeded`, 93%). Both are seeded arms where the agent chased the defect: three retries of a dead tap, and two ANR recoveries. This is one trial per case, well short of the n ≥ 8 CLAUDE.md asks for before a cap moves.

## 6. Every false report, miss and non-completion, adjudicated

Classes as the ticket defines them:
- `real`: nothing on the device supports it.
- `artifact`: matcher, grounding or tense.
- `corpus`: a case defect.
- `environment`: a true statement about fixture or device state.

A true statement about the CLEAN build's own upstream behaviour (a quirk no patch seeded) is filed as `environment` too. It is state of the app-under-test environment that no defect put there, it is true, and it is not a hallucination. Each row was checked against the device's own answer in the transcript or the saved screenshot. Each artifact row was re-fed through the real `journey.match_report` / `_witness` with one input flipped.

Held-out rows follow docs/heldout.md: episode numbers from the appendix, no case or app names, and no quoted held-out text.

### Public (14 false reports, 1 miss, 1 non-completion)

| # | episode | recorded | the report / evidence | device result that settles it | class |
| --- | --- | --- | --- | --- | --- |
| 1 | `contacts-create~clean` | false report (clean → false alarm) | observed `Address`, expected `First name`: "the text cursor was blinking in the Address field instead of the First name field" | `evidence/screens/0004.jpg` of this episode: the new-contact form with the cursor in Address, on the clean build | **environment** (upstream Fossify Contacts behaviour) |
| 2 | `contacts-create~seeded` | false report | the same cursor-in-Address report (the seeded typo `Contatcs` was found separately) | same screen and state as #1; the seeded patch is a tab label | **environment** |
| 3 | `contacts-new-contact-survives-rotation~clean` | false report (false alarm) | observed `10:00`: in landscape the red header collapses, so the favourite star overlaps the status-bar clock and the camera overlaps the status icons | `evidence/screens/0009.jpg`: exactly that, on the clean build, after the agent's own rotate (the step the brief asks for) | **environment** (upstream landscape layout) |
| 4 | `contacts-new-contact-survives-rotation~seeded` | false report | observed `At least 1 field has to be filled out`: Save rejected after rotation | the toast followed the found defect (`new-contact-lost-on-rotation`, credited in the same episode): the name was gone, so there was nothing to save | **artifact** (thoroughness charge: a true consequence of the found defect) |
| 5 | same | false report | observed `No contacts found`, expected `Alice` | device text shows the empty list, the consequence of #4 | **artifact** (thoroughness) |
| 6 | `medtimer-check-stock~clean` | false report (false alarm) | observed `Ibuprofen (10 left, After 10/14/26) / 0 reminders / Inactive`: the list says 0 reminders while Ibuprofen's detail shows one reminder card (8:00 AM, dosage 2, Inactive) | both strings are in the clean episode's device text. The fixture seeds an INACTIVE reminder, and the list counts active reminders only | **environment** (fixture state) |
| 7 | `medtimer-check-stock~seeded` | false report | the same `0 reminders` observation | same device text | **environment** |
| 8 | `medtimer-take-dose-then-medicine-list~clean` | false report (false alarm) | the same `0 reminders` observation | same | **environment** |
| 9 | `medtimer-take-dose-then-medicine-list~seeded` | false report | the same `0 reminders` observation | same | **environment** |
| 10 | same | false report | observed `Ibuprofen (4)` still showing the raised (bell) icon after relaunch | the consequence of the found ANR (`overview-action-blocks-main-thread`, credited): Taken never recorded | **artifact** (thoroughness) |
| 11 | `medtimer-add-reminder~seeded` | false report | the same `0 reminders` observation (ungrounded: the agent quoted two nodes as one) | same device text as #6 | **environment** |
| 12 | same | false report | duplicated words in the out-of-stock help text: "…either after every reminder or once or once per day." | the identical string is in `medtimer-add-reminder~clean`'s device text and in the committed truth screens: upstream MedTimer text | **environment** (upstream string) |
| 13 | `cal-complete-task~seeded` | false report | observed `Mark completed`, expected `Mark incomplete`: reopening the task still offers Mark completed | the consequence of the found crash (`task-complete-crash`, credited): the completion was never persisted | **artifact** (thoroughness) |
| 14 | `tasks-add-subtask~seeded` | false report | observed `Pack for trip 2 Passport Chargers`: the new subtask was not persisted after relaunch | the consequence of the found crash (`subtask-filed-before-written`, credited) | **artifact** (thoroughness) |
| 15 | `orgzly-create-and-search~seeded` | **miss**: `notebook-count-off-by-one` | no report mentions the count | device text shows `Contains 32 notes` (clean: `Contains 33 notes`), but the brief gives the agent no reference for the count. That is the lint's `reference` warning, deferred to QUA-2768 | **corpus** (known; QUA-2768) |
| 16 | `orgzly-open-note-from-notebook~clean` | **not completed**: "outcome was not witnessed … `Getting Started with Orgzly  •  Notes`" | the agent did the action (tap, then observe shows the note editor) and reported pass | DevLoop's result contains `"text": "Getting Started with Orgzly  •  Notes"` byte for byte (two spaces each side). `_evidence` collapses the witness to one space and the device text is never collapsed: `_word(w, t)` → False, and `_word(w, collapse(t))` → True | **artifact** (scorer whitespace normalisation, QUA-2788) |

### Held-out (6 false reports, 2 misses)

| # | episode | recorded | what it was, without quoting the split | class |
| --- | --- | --- | --- | --- |
| 17 | H1 (clean) | false report (false alarm) | a confirmation dialog when backing out of an unmodified edit screen. The dialog is in the device text, on the clean build | **environment** (upstream app behaviour) |
| 18 | H2 (seeded) | false report | the same dialog, same device text (the blocking defect was found separately) | **environment** |
| 19 | H5 (clean) | false report (false alarm) | an error toast in a picker. It is visible in this episode's screenshot on the clean build, and ungrounded because toasts are not in the hierarchy | **environment** (upstream app behaviour) |
| 20 | H2 (seeded) | false report **and miss** of its display side bug | the agent's report describes the side bug exactly: the seeded value as `observed`, the correct value as `expected`. The case's marker for that side bug is the CORRECT (clean) value, the string the seeded build removes, and side matching reads `observed` only. Re-fed: as recorded → None; `observed ← expected` → the side bug. The `honest` adversary control quotes the marker as `observed` and so cannot see this | **corpus** (inverted marker, QUA-2789) |
| 21 | H10 (seeded) | false report **and miss** of the same side bug | the same, in a second case carrying the same defect | **corpus** (QUA-2789) |
| 22 | H18 (seeded) | false report | the consequence of the found functional defect in the same episode | **artifact** (thoroughness) |

**No row is `real`.** In 100 episodes the agent never reported something the device did not show. The 6 thoroughness rows (#4, #5, #10, #13, #14, #22) are prerequisite 2 of the pilot (`docs/pilot-2026-09-17.md` §12), and the scorer's fix for them is QUA-2779 (deferred by the owner, adjudicated by hand here). The 2026-09-19 board's other artifact classes did not recur:
- tense and grounding-by-image: every screen reading was text here;
- the one-character marker: retired by QUA-2783.

## 7. Rates, raw and environment-excluded

Recomputed by hand from the table above, with `rates.wilson`. "Environment-excluded" drops the `environment` reports and re-asks "did this clean episode carry at least one false report". It is the ticket's correction, and it is what the doc leads with. The catch column's third view is for reading only: it sets aside the defects the corpus cannot credit (rows 15, 20, 21). It is labelled as such and ranks nothing.

| | raw | environment-excluded |
| --- | --- | --- |
| false alarm / clean case, public | 4/41 = 9.8% [3.9–22.5] | **0/41 = 0% [0–8.6]** |
| false alarm / clean case, held-out | 2/10 = 20.0% [5.7–51.0] | **0/10 = 0% [0–27.8]** |
| false alarm / clean case, both blocks (not a board row, never blended in a ranking) | 6/51 = 11.8% [5.5–23.4] | 0/51 = 0% [0–7.0] |
| clean-run integrity @200, public | 0% [0–0] | **100% [0–100]** |
| clean-run integrity @200, held-out | 0% [0–0] | **100% [0–100]** |
| false reports, public (precision) | 14 → 73.6% | 5 (all thoroughness) → 88.6% |
| false reports, held-out (precision) | 6 → 60.0% | 3 → 75.0% |

| catch / seeded defect | raw | excluding corpus rows (reading only) |
| --- | --- | --- |
| public | 39/40 = 97.5% [87.1–99.6] | 39/39 = 100% [91.0–100] |
| held-out | 9/11 = 81.8% [52.3–94.9] | 9/9 = 100% [70.1–100] |

**Why the corrected rate leads.** The ranking key (QUA-2780) exists because a nightly suite runs at a few percent bug prior, where false alarms dominate. What it should penalise is an agent inventing failures. On this board every clean-arm false alarm is a true observation:
- two upstream Contacts UI quirks;
- a fixture state the brief does not explain (MedTimer's inactive seeded reminder);
- two upstream behaviours of a held-out app.

Ranked raw, this agent scores 0% integrity for mentioning them, and an agent that noticed less would score better. The raw row stays published, because it is what the harness computes today, and QUA-2779 is the scorer change that would make the corrected number reproducible.

## 8. Cost and wall time

| | |
| --- | --- |
| total | **$131.34** for 100 episodes, 100/100 `reported` (claude-code's own `total_cost_usd`); 0 unpriced |
| public / held-out | $104.70 (80 episodes, $1.31 mean) / $26.63 (20, $1.33 mean) |
| clean / seeded | $59.58 (51) / $71.76 (49) |
| range | $0.59 (`anki-browse-cards~clean`) to $2.50 (`cal-open-task-from-list~seeded`) |
| against the projection | at the low end of $184–$378: 29% under the token re-pricing, 65% under the owner's 2× rule. Fable's cheap cache reads matter more than its 2× input and output, because 94% of input tokens were cache reads here too |
| weekly window | 38% → 47% (about $14.6 per point, other usage on the account included) |
| the Fable probe | $0.51 before the run, not in the total |
| wall time | **2 h 59 min 07 s** elapsed (11:43:43Z–14:42:50Z), including the two segment restarts (28 s and 48 s); lane time 2.96 h; median agent time 1.05 min per episode (mean 1.27) |
| held-out derive (free) | about 11 min for one trial and about 36 min at `--repeat 3`, on two emulators in parallel |

## 9. Power: what 41 + 10 cases can and cannot tell apart

Intervals count trials as draws, so power comes from distinct cases (CLAUDE.md, `rates.py`). This board has 41 public clean cases, 40 public seeded defects, 10 held-out clean cases and 11 held-out defects.
- A corrected 0/41 bounds the false-alarm rate at **8.6%**. It cannot tell 0% from 5%, and at N = 200 those are 100% versus 0.004% integrity.
- ±5 pp at 15% needs about 200 distinct cases, and ±2 pp at 5% about 450.
- A second model on the same 41 + 10 cases can separate from this one only by a large gap. On false alarms that is roughly 0/41 against ≥ 6/41 (upper bound 8.6% vs a lower bound near 7%). On catch it is roughly 39/40 against ≤ 31/40.
- The held-out block, at 10 cases, bounds a rate only to about ±25 pp. It can show a gross public/held-out gap, which is what it is for, and nothing finer.
- A second trial narrows the brackets on paper only.

## 10. Follow-ups

Every non-real row has a ticket. Each ticket is related to QUA-2773. None is a child: none blocks QUA-2786, for the reasons given under Go / no-go.

| ticket | covers | rows |
| --- | --- | --- |
| **QUA-2788** — Normalise whitespace in journey device text before witness, present-oracle and grounding matches | scorer artifact: a double-spaced screen string never matches a whitespace-collapsed needle. Affects witness, `present:` evidence and grounding alike | 16 |
| **QUA-2789** — Held-out split: re-author the inverted Difference display marker and the five weak witnesses | held-out corpus: a display marker authored as the clean value; five cases whose witness is readable before the action (`agrees: false` since this ticket's re-derive) | 20, 21; section 2 |
| **QUA-2779** (existing, deferred) — unadjudicated bucket | every `environment` row, and the thoroughness charge. This run's evidence is posted there | 1–3, 6–9, 11, 12, 17–19 (environment); 4, 5, 10, 13, 14, 22 (thoroughness) |
| **QUA-2768** (existing) — five weak-witness cases | `orgzly-create-and-search`'s count side bug has no reference in the brief. Evidence posted there | 15 |
| **QUA-2790** — Deny `su` at the adb meter and clear `/data/anr` between episodes | a root path through `adb shell su 0` on `google_apis`, and cross-episode ANR traces | section 4 |
| **QUA-2791** — Make journey steps equal interactions.json and count shell,v2 option requests in adb_counts | step-count diagnostics | section 3 |
| **QUA-2792** — DevLoop: `mobile_device_logs` fails after a crash; standalone instructions name `qg_*` tools it does not serve | DevLoop-MCP | section 4 |

Not ticketed, and noted for the owner: the MedTimer fixture's inactive Ibuprofen reminder produced 5 of the 20 false reports (rows 6–9, 11), 2 of them clean-arm false alarms. If QUA-2779 stays deferred, a one-line fixture change or a sentence in those briefs would remove the most frequent environment report in the corpus. That is a corpus edit, out of scope here.

## 11. Acceptance criteria, and go / no-go for the second model

| criterion (QUA-2785) | result | evidence |
| --- | --- | --- |
| run: claude-code · claude-fable-5-1, DevLoop over HTTP, `--mode journey`, all six public apps + held-out, `--trials 1`, held-out required, runs dir outside the repo | **PASS** | section 0; header table |
| provenance on every episode: `brief_version` v3, `dump_stats` sane, `metered_denied`, `cost_source`, device-state invariant | **PASS** | section 3: 100/100 on each |
| every transcript read; every miss / false report classified real / artifact / corpus / environment with evidence | **PASS** | sections 4 and 6: 100 transcripts, 22 rows (20 false reports, 3 misses, 1 non-completion, with 2 rows both a false report and a miss), 0 real |
| board with rates and intervals; `validate_bundle.py` on every episode | **PASS** | section 5; 100/100 `FAILURES: 0` |
| both rates published raw and environment-excluded, with intervals; the doc leads with the corrected one and says why | **PASS** | the top table, section 7 |
| doc committed with run id, corpus_version, held-out version, harness commits, cost, wall time | **PASS** | header table; section 8 |
| every non-real miss / false report has a follow-up ticket or a fix PR | **PASS** | section 10 |
| held-out truth verified under the pin before spending; lint and adversary green with the split on | **PASS** | sections 1 and 2; held-out `c8b4fa6bafb1` → `ec1ed6d1dd18` |
| spend guardrails: projection stated, checkpoint at ~10 episodes, no second model, trial or board | **PASS** | section 0: $110–113 projected at 10 episodes; $131.34 spent |

**GO for running the second model (QUA-2786).** Reasons:

1. **The arm works end to end.** 100 of 100 episodes staged, ran, verified and scored, with 0 excluded, 0 contaminated and 0 truncated. Every bundle validated, and every provenance field was as designed.
2. **The instrument's error on this arm is characterised and small.** Across 100 transcripts, every false report is a true statement about the device, or a report the scorer failed to credit. Each artifact is named, reproduced through the real matcher and ticketed. Nothing the scorer charged is unexplained.
3. **The things the epic built all held on a real board:**
   - brief v3's report of record: 100/100 findings file, and DevLoop's report tool never displaced it;
   - no-source mode: no episode read the host, no `qg_*` calls;
   - the clock pin and the device invariant: 100/100 clean starts;
   - the workspace outside the repo: 0 inherited instructions;
   - pricing: 100/100 reported.
4. **It is affordable:** $131 and 3 hours for the whole corpus plus held-out.

**Conditions, which the controller should carry into QUA-2786:**

1. **Adjudicate the second model's environment rows by hand the same way, and lead with the environment-excluded rate.** Raw integrity here is 0% entirely because of true observations. A raw Fable-vs-Astra ranking would rank which model mentions more upstream quirks, not which invents failures.
2. **Hold everything else fixed:** harness `a565b5a` (plus this doc), DevLoop-MCP `ec1f483` with `--app-source none`, corpus `afce9dfd7aa5`, held-out `ec1ed6d1dd18`, the default clock pin, and the same AVD image. If QUA-2788 or QUA-2789 lands first, rescore this run with `rescore_journey.py` so the two rows share a scorer, and record both versions.
3. **codex-cli reaches for raw adb more than claude-code does.** Read the second model's Bash/adb use as section 4 did, and prefer landing **QUA-2790** (the `su` root path) before that board. QUA-2777's note that codex's disallowed-tool acceptance is unverified still stands.
4. **Read the comparison against section 9.** On 41 + 10 cases only a large gap is a result. Say so in QUA-2786's doc, whatever it shows.

## Appendix: every episode

Run `20260923-114342-b8af`. Cost is `metrics.cost_usd`, `reported` for all 100 episodes. "agent min" is `evidence/meta.json` `wall_time_sec`. "seg" is the resume segment.

| # | case | arm | cost | steps / budget | agent min | completed | bugs found / present | false reports | seg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | anki-browse-cards | clean | $0.59 | 5 / 30 | 0.4 | yes | 0 / 0 | 0 | 1 |
| 2 | anki-browse-cards | seeded | $0.68 | 5 / 30 | 0.4 | yes | 1 / 1 | 0 | 1 |
| 3 | anki-browse-new-deck | clean | $1.46 | 16 / 65 | 0.9 | yes | 0 / 0 | 0 | 0 |
| 4 | anki-browse-new-deck | seeded | $1.78 | 30 / 65 | 1.6 | yes | 1 / 1 | 0 | 1 |
| 5 | anki-create-deck | clean | $0.85 | 9 / 35 | 0.9 | yes | 0 / 0 | 0 | 1 |
| 6 | anki-open-card-from-browser | clean | $0.70 | 7 / 35 | 0.5 | yes | 0 / 0 | 0 | 1 |
| 7 | anki-open-card-from-browser | seeded | $1.27 | 15 / 35 | 1.1 | yes | 1 / 1 | 0 | 1 |
| 8 | anki-study-first-card | clean | $0.73 | 7 / 35 | 0.6 | yes | 0 / 0 | 0 | 1 |
| 9 | anki-study-first-card | seeded | $1.02 | 13 / 35 | 0.8 | yes | 1 / 1 | 0 | 1 |
| 10 | cal-complete-task | clean | $1.76 | 22 / 50 | 1.3 | yes | 0 / 0 | 0 | 2 |
| 11 | cal-complete-task | seeded | $1.94 | 29 / 50 | 1.6 | yes | 1 / 1 | 1 | 2 |
| 12 | cal-create-all-day-event | clean | $1.44 | 17 / 45 | 1.1 | yes | 0 / 0 | 0 | 2 |
| 13 | cal-create-all-day-event | seeded | $1.70 | 24 / 45 | 1.4 | yes | 1 / 1 | 0 | 2 |
| 14 | cal-create-event | clean | $1.36 | 14 / 40 | 1.1 | yes | 0 / 0 | 0 | 2 |
| 15 | cal-create-event | seeded | $1.22 | 15 / 40 | 0.9 | yes | 1 / 1 | 0 | 2 |
| 16 | cal-create-task | clean | $1.18 | 15 / 40 | 0.9 | yes | 0 / 0 | 0 | 2 |
| 17 | cal-create-task | seeded | $1.14 | 13 / 40 | 0.8 | yes | 1 / 1 | 0 | 2 |
| 18 | cal-edit-event | clean | $1.47 | 25 / 50 | 1.2 | yes | 0 / 0 | 0 | 2 |
| 19 | cal-edit-event | seeded | $2.18 | 40 / 50 | 1.9 | yes | 1 / 1 | 0 | 2 |
| 20 | cal-open-task-from-list | clean | $1.52 | 18 / 45 | 1.1 | yes | 0 / 0 | 0 | 2 |
| 21 | cal-open-task-from-list | seeded | $2.50 | 40 / 45 | 2.1 | yes | 1 / 1 | 0 | 2 |
| 22 | cal-repeat-survives-rotation | clean | $2.48 | 30 / 50 | 1.9 | yes | 0 / 0 | 0 | 2 |
| 23 | cal-repeat-survives-rotation | seeded | $2.34 | 34 / 50 | 1.9 | yes | 1 / 1 | 0 | 2 |
| 24 | cal-search-event | clean | $1.40 | 18 / 50 | 1.0 | yes | 0 / 0 | 0 | 2 |
| 25 | cal-search-event | seeded | $1.95 | 25 / 50 | 1.6 | yes | 1 / 1 | 0 | 2 |
| 26 | cal-switch-back-to-list | clean | $1.54 | 20 / 50 | 1.3 | yes | 0 / 0 | 0 | 2 |
| 27 | cal-switch-back-to-list | seeded | $1.98 | 31 / 50 | 1.8 | yes | 1 / 1 | 0 | 2 |
| 28 | contacts-create-group | clean | $0.81 | 10 / 40 | 0.7 | yes | 0 / 0 | 0 | 2 |
| 29 | contacts-create-group | seeded | $1.84 | 23 / 40 | 1.8 | yes | 1 / 1 | 0 | 2 |
| 30 | contacts-create | clean | $0.87 | 8 / 35 | 0.8 | yes | 0 / 0 | 1 | 2 |
| 31 | contacts-create | seeded | $0.87 | 8 / 35 | 0.7 | yes | 1 / 1 | 1 | 2 |
| 32 | contacts-delete | clean | $1.10 | 16 / 40 | 1.0 | yes | 0 / 0 | 0 | 2 |
| 33 | contacts-delete | seeded | $1.12 | 19 / 40 | 1.0 | yes | 1 / 1 | 0 | 2 |
| 34 | contacts-favorite | clean | $1.16 | 14 / 40 | 0.9 | yes | 0 / 0 | 0 | 2 |
| 35 | contacts-favorite | seeded | $1.66 | 23 / 40 | 2.9 | yes | 1 / 1 | 0 | 2 |
| 36 | contacts-new-contact-survives-rotation | clean | $1.02 | 10 / 45 | 1.0 | yes | 0 / 0 | 1 | 2 |
| 37 | contacts-new-contact-survives-rotation | seeded | $1.36 | 18 / 45 | 1.3 | yes | 1 / 1 | 2 | 2 |
| 38 | contacts-phone | clean | $0.99 | 14 / 40 | 0.8 | yes | 0 / 0 | 0 | 2 |
| 39 | contacts-phone | seeded | $1.16 | 15 / 40 | 0.9 | yes | 1 / 1 | 0 | 2 |
| 40 | contacts-view-details | clean | $0.96 | 10 / 40 | 0.7 | yes | 0 / 0 | 0 | 2 |
| 41 | contacts-view-details | seeded | $1.34 | 19 / 40 | 1.1 | yes | 1 / 1 | 0 | 2 |
| 42 | medtimer-add-medicine-back-to-list | clean | $1.03 | 12 / 45 | 0.9 | yes | 0 / 0 | 0 | 2 |
| 43 | medtimer-add-medicine-back-to-list | seeded | $1.40 | 20 / 45 | 1.2 | yes | 1 / 1 | 0 | 2 |
| 44 | medtimer-add-medicine | clean | $0.95 | 13 / 40 | 0.8 | yes | 0 / 0 | 0 | 2 |
| 45 | medtimer-add-medicine | seeded | $0.97 | 13 / 40 | 0.8 | yes | 1 / 1 | 0 | 2 |
| 46 | medtimer-add-reminder | clean | $1.26 | 18 / 50 | 1.2 | yes | 0 / 0 | 0 | 2 |
| 47 | medtimer-add-reminder | seeded | $1.53 | 21 / 50 | 2.4 | yes | 1 / 1 | 2 | 2 |
| 48 | medtimer-analysis-tabular-view | clean | $0.80 | 7 / 40 | 0.7 | yes | 0 / 0 | 0 | 2 |
| 49 | medtimer-analysis-tabular-view | seeded | $2.46 | 37 / 40 | 5.5 | yes | 1 / 1 | 0 | 2 |
| 50 | medtimer-check-stock | clean | $0.82 | 7 / 40 | 0.8 | yes | 0 / 0 | 1 | 2 |
| 51 | medtimer-check-stock | seeded | $1.01 | 15 / 40 | 1.1 | yes | 1 / 1 | 1 | 2 |
| 52 | medtimer-correct-dose-amount | clean | $0.93 | 10 / 45 | 0.7 | yes | 0 / 0 | 0 | 2 |
| 53 | medtimer-correct-dose-amount | seeded | $1.57 | 23 / 45 | 1.5 | yes | 1 / 1 | 0 | 2 |
| 54 | medtimer-review-aspirin | clean | $0.92 | 11 / 40 | 0.9 | yes | 0 / 0 | 0 | 2 |
| 55 | medtimer-review-aspirin | seeded | $0.89 | 10 / 40 | 0.8 | yes | 2 / 2 | 0 | 2 |
| 56 | medtimer-take-dose-then-medicine-list | clean | $0.95 | 11 / 45 | 1.0 | yes | 0 / 0 | 1 | 2 |
| 57 | medtimer-take-dose-then-medicine-list | seeded | $2.20 | 31 / 45 | 4.0 | yes | 1 / 1 | 2 | 2 |
| 58 | orgzly-complete-repeating-task | clean | $1.49 | 31 / 60 | 3.9 | yes | 0 / 0 | 0 | 1 |
| 59 | orgzly-complete-repeating-task | seeded | $1.74 | 31 / 60 | 2.5 | yes | 1 / 1 | 0 | 2 |
| 60 | orgzly-create-and-search | clean | $1.19 | 16 / 45 | 0.9 | yes | 0 / 0 | 0 | 2 |
| 61 | orgzly-create-and-search | seeded | $1.12 | 16 / 45 | 1.0 | yes | 0 / 1 | 0 | 2 |
| 62 | orgzly-create-priority-note | clean | $1.26 | 22 / 60 | 1.1 | yes | 0 / 0 | 0 | 1 |
| 63 | orgzly-create-priority-note | seeded | $1.50 | 25 / 60 | 1.3 | yes | 1 / 1 | 0 | 1 |
| 64 | orgzly-nest-notes-deeper | clean | $1.77 | 28 / 60 | 1.6 | yes | 0 / 0 | 0 | 2 |
| 65 | orgzly-nest-notes-deeper | seeded | $2.27 | 30 / 60 | 1.8 | yes | 1 / 1 | 0 | 2 |
| 66 | orgzly-new-note-survives-rotation | clean | $0.98 | 11 / 40 | 0.8 | yes | 0 / 0 | 0 | 2 |
| 67 | orgzly-new-note-survives-rotation | seeded | $1.06 | 13 / 40 | 1.1 | yes | 1 / 1 | 0 | 2 |
| 68 | orgzly-open-note-from-notebook | clean | $0.67 | 5 / 30 | 0.5 | no | 0 / 0 | 0 | 2 |
| 69 | orgzly-open-note-from-notebook | seeded | $1.23 | 13 / 30 | 1.0 | yes | 1 / 1 | 0 | 2 |
| 70 | tasks-add-subtask | clean | $0.83 | 9 / 45 | 1.0 | yes | 0 / 0 | 0 | 2 |
| 71 | tasks-add-subtask | seeded | $1.21 | 12 / 45 | 1.3 | yes | 1 / 1 | 1 | 2 |
| 72 | tasks-change-due-time | clean | $1.48 | 17 / 45 | 1.2 | yes | 0 / 0 | 0 | 2 |
| 73 | tasks-change-due-time | seeded | $1.43 | 23 / 45 | 1.8 | yes | 1 / 1 | 0 | 2 |
| 74 | tasks-complete-and-rename | clean | $1.30 | 19 / 50 | 1.1 | yes | 0 / 0 | 0 | 2 |
| 75 | tasks-complete-parent | clean | $0.75 | 6 / 40 | 0.6 | yes | 0 / 0 | 0 | 2 |
| 76 | tasks-complete-parent | seeded | $0.75 | 6 / 40 | 0.7 | yes | 1 / 1 | 0 | 2 |
| 77 | tasks-complete-repeating | clean | $0.90 | 8 / 40 | 0.8 | yes | 0 / 0 | 0 | 2 |
| 78 | tasks-complete-repeating | seeded | $1.11 | 10 / 40 | 1.0 | yes | 1 / 1 | 0 | 2 |
| 79 | tasks-create-with-due-date | clean | $1.24 | 19 / 40 | 1.0 | yes | 0 / 0 | 0 | 2 |
| 80 | tasks-create-with-due-date | seeded | $1.33 | 20 / 40 | 1.2 | yes | 1 / 1 | 0 | 2 |

Held-out episodes are numbered in run order and not named (docs/heldout.md).

| # | held-out episode | arm | cost | steps / budget | agent min | completed | bugs found / present | false reports | seg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| H1 | (withheld) | clean | $1.94 | 31 / 60 | 3.2 | yes | 0 / 0 | 1 | 1 |
| H2 | (withheld) | seeded | $2.02 | 32 / 60 | 1.9 | yes | 1 / 2 | 2 | 1 |
| H3 | (withheld) | clean | $1.74 | 26 / 50 | 1.4 | yes | 0 / 0 | 0 | 1 |
| H4 | (withheld) | seeded | $2.04 | 31 / 50 | 1.9 | yes | 1 / 1 | 0 | 1 |
| H5 | (withheld) | clean | $1.61 | 24 / 50 | 2.2 | yes | 0 / 0 | 1 | 1 |
| H6 | (withheld) | seeded | $1.61 | 25 / 50 | 1.4 | yes | 1 / 1 | 0 | 1 |
| H7 | (withheld) | clean | $1.53 | 22 / 45 | 1.2 | yes | 0 / 0 | 0 | 1 |
| H8 | (withheld) | seeded | $1.54 | 22 / 45 | 1.3 | yes | 1 / 1 | 0 | 1 |
| H9 | (withheld) | clean | $1.41 | 21 / 45 | 1.1 | yes | 0 / 0 | 0 | 1 |
| H10 | (withheld) | seeded | $1.50 | 21 / 45 | 1.3 | yes | 0 / 1 | 1 | 1 |
| H11 | (withheld) | clean | $1.11 | 15 / 40 | 1.0 | yes | 0 / 0 | 0 | 2 |
| H12 | (withheld) | seeded | $1.11 | 15 / 40 | 1.0 | yes | 1 / 1 | 0 | 2 |
| H13 | (withheld) | clean | $1.09 | 16 / 40 | 0.9 | yes | 0 / 0 | 0 | 2 |
| H14 | (withheld) | seeded | $1.30 | 13 / 40 | 1.2 | yes | 1 / 1 | 0 | 2 |
| H15 | (withheld) | clean | $0.88 | 9 / 30 | 0.7 | yes | 0 / 0 | 0 | 2 |
| H16 | (withheld) | seeded | $0.90 | 9 / 30 | 0.8 | yes | 1 / 1 | 0 | 2 |
| H17 | (withheld) | clean | $0.70 | 7 / 30 | 0.6 | yes | 0 / 0 | 0 | 2 |
| H18 | (withheld) | seeded | $0.99 | 10 / 30 | 0.9 | yes | 1 / 1 | 1 | 2 |
| H19 | (withheld) | clean | $0.69 | 5 / 25 | 0.5 | yes | 0 / 0 | 0 | 2 |
| H20 | (withheld) | seeded | $0.90 | 8 / 25 | 0.9 | yes | 1 / 1 | 0 | 2 |

**Total: $131.34** = $104.70 public + $26.63 held-out, all `reported`; 0 unmeasured.
