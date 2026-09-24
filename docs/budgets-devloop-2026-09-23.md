# Journey step budgets on the DevLoop arm (QUA-2784, 2026-09-23)

**Result: two caps go up and none come down.** `cal-open-task-from-list` goes from 45 to 60, written by `derive_budgets.py --write`. `medtimer-analysis-tabular-view` goes from 40 to 56. The script labels that case `runaway`, and the raise overrides the label after a transcript read (section 3). The other 39 public cases and all 10 held-out cases keep their caps. `corpus_version` moves from `afce9dfd7aa5` to `505533a172ae`, so boards from before and after this change are not comparable.

## 1. Evidence

| | |
| --- | --- |
| validation run | `20260923-114342-b8af` (QUA-2785, `docs/validation-devloop-2026-09-23.md`): claude-code · claude-fable-5-1, DevLoop arm, one trial, 80 public + 20 held-out episodes, 0 truncated. Every case has n ≤ 2 |
| re-run | `20260923-174028-afd5`: the same agent, model and server (DevLoop-MCP `ec1f483`, `--app-source none`), `--mode journey --app fossify-calendar,medtimer --case cal-open-task-from-list,medtimer-analysis-tabular-view --trials 4`, 16 episodes, emulator-5554 (`qgbench_root`), one lane, default clock pin, brief v3, harness `42e282d` (QUA-2788 and QUA-2790 merged). `QGB_HELDOUT_DIR` was set, so the split was configured and none of its apps was in scope (plan `heldout_apps: []`). The opt-out was not used |
| re-run cost | **$25.37** for 16 episodes: 15 `reported` ($24.20) + 1 `estimated` ($1.17, the truncated episode, priced from per-request usage). That estimate is probably low, because its finished siblings cost $2.20–2.70. The projection was $29. Elapsed 49 min (17:40–18:30Z), lane time 49 min |
| exclusions | 0 excluded, 0 `staging_failed`, 0 env/infra failures, 16/16 `corpus_version afce9dfd7aa5` |
| derivation | `uv run python scripts/derive_budgets.py --mode journey --runs-dir ~/.qualgentbench/runs` over both runs (the directory holds nothing else): 96 trusted public episodes, 95 finished, 1 truncated. That gives n = 10 for the two re-run cases and n ≤ 2 for the rest |

Why the re-run was needed: over the validation run alone, the script called both cases `under-budget` at n = 2 (40/45 and 37/40, both seeded). The ticket's rule is that a cap does not move at n < 8 without a `--case … --trials 4` re-run first.

## 2. Per case

The proposal formula is `ceil(1.5 × worst finished episode)`, with a floor of 25. The script never lowers a cap. It recommends a value only on an `under-budget` verdict, and then the larger of the current cap and the proposal.

| case | old cap | proposed | verdict | n | worst finished (steps/cap) |
| --- | --- | --- | --- | --- | --- |
| `anki-browse-cards` | 30 | 30 (stands) | healthy | 2 | 5/30 (17%) |
| `anki-browse-new-deck` | 65 | 65 (stands) | healthy | 2 | 30/65 (46%) |
| `anki-create-deck` | 35 | 35 (stands) | healthy | 1 | 9/35 (26%) |
| `anki-open-card-from-browser` | 35 | 35 (stands) | healthy | 2 | 15/35 (43%) |
| `anki-study-first-card` | 35 | 35 (stands) | healthy | 2 | 13/35 (37%) |
| `cal-complete-task` | 50 | 50 (stands) | healthy | 2 | 29/50 (58%) |
| `cal-create-all-day-event` | 45 | 45 (stands) | healthy | 2 | 24/45 (53%) |
| `cal-create-event` | 40 | 40 (stands) | healthy | 2 | 15/40 (38%) |
| `cal-create-task` | 40 | 40 (stands) | healthy | 2 | 15/40 (38%) |
| `cal-edit-event` | 50 | 50 (stands) | healthy | 2 | 40/50 (80%) |
| `cal-open-task-from-list` | 45 | **60** | under-budget (`--write`) | 10 | 40/45 (89%) |
| `cal-repeat-survives-rotation` | 50 | 50 (stands) | healthy | 2 | 34/50 (68%) |
| `cal-search-event` | 50 | 50 (stands) | healthy | 2 | 25/50 (50%) |
| `cal-switch-back-to-list` | 50 | 50 (stands) | healthy | 2 | 31/50 (62%) |
| `contacts-create` | 35 | 35 (stands) | healthy | 2 | 8/35 (23%) |
| `contacts-create-group` | 40 | 40 (stands) | healthy | 2 | 23/40 (58%) |
| `contacts-delete` | 40 | 40 (stands) | healthy | 2 | 19/40 (48%) |
| `contacts-favorite` | 40 | 40 (stands) | healthy | 2 | 23/40 (58%) |
| `contacts-new-contact-survives-rotation` | 45 | 45 (stands) | healthy | 2 | 18/45 (40%) |
| `contacts-phone` | 40 | 40 (stands) | healthy | 2 | 15/40 (38%) |
| `contacts-view-details` | 40 | 40 (stands) | healthy | 2 | 19/40 (48%) |
| `medtimer-add-medicine` | 40 | 40 (stands) | healthy | 2 | 13/40 (32%) |
| `medtimer-add-medicine-back-to-list` | 45 | 45 (stands) | healthy | 2 | 20/45 (44%) |
| `medtimer-add-reminder` | 50 | 50 (stands) | healthy | 2 | 21/50 (42%) |
| `medtimer-analysis-tabular-view` | 40 | **56** | script: runaway. Transcript read: crowding and converging, not adrift. Hand-set, in its own commit | 10 | 37/40 (92%); 1 truncated at 41/40 |
| `medtimer-check-stock` | 40 | 40 (stands) | healthy | 2 | 15/40 (38%) |
| `medtimer-correct-dose-amount` | 45 | 45 (stands) | healthy | 2 | 23/45 (51%) |
| `medtimer-review-aspirin` | 40 | 40 (stands) | healthy | 2 | 11/40 (28%) |
| `medtimer-take-dose-then-medicine-list` | 45 | 45 (stands) | healthy | 2 | 31/45 (69%) |
| `orgzly-complete-repeating-task` | 60 | 60 (stands) | healthy | 2 | 31/60 (52%) |
| `orgzly-create-and-search` | 45 | 45 (stands) | healthy | 2 | 16/45 (36%) |
| `orgzly-create-priority-note` | 60 | 60 (stands) | healthy | 2 | 25/60 (42%) |
| `orgzly-nest-notes-deeper` | 60 | 60 (stands) | healthy | 2 | 30/60 (50%) |
| `orgzly-new-note-survives-rotation` | 40 | 40 (stands) | healthy | 2 | 13/40 (32%) |
| `orgzly-open-note-from-notebook` | 30 | 30 (stands) | healthy | 2 | 13/30 (43%) |
| `tasks-add-subtask` | 45 | 45 (stands) | healthy | 2 | 12/45 (27%) |
| `tasks-change-due-time` | 45 | 45 (stands) | healthy | 2 | 23/45 (51%) |
| `tasks-complete-and-rename` | 50 | 50 (stands) | healthy | 1 | 19/50 (38%) |
| `tasks-complete-parent` | 40 | 40 (stands) | healthy | 2 | 6/40 (15%) |
| `tasks-complete-repeating` | 40 | 40 (stands) | healthy | 2 | 10/40 (25%) |
| `tasks-create-with-due-date` | 40 | 40 (stands) | healthy | 2 | 20/40 (50%) |

## 3. The two moved caps, with the transcripts

**`cal-open-task-from-list` (45 → 60).**
- Seeded arm, in steps: 40 (validation), 37, 29, 28, 28. Clean arm: 18, 18, 16, 16, 19.
- One finished episode crowded the cap (40/45, 89%). The next highest was 37/45 (82%). No truncation.
- I read both seeded transcripts above 80%. The route was done by about step 26. The rest was the agent confirming a dead tap: three taps at different points of the row, a hit test, device logs, and a control tap in another list view. That is converging on the defect, not wandering.
- The script's verdict is `under-budget`, and `--write` set the new cap.

**`medtimer-analysis-tabular-view` (40 → 56), a reviewed override.**
- Seeded arm, in steps: 37 (validation), 35, 32, 37, and **41/40 truncated**. Clean arm: 7, 7, 7, 7, 8.
- The script says `runaway` because its verdict order checks "a truncation plus any finished episode under 85%" before it checks crowding. The 80% episode (32/40) is enough to fire the runaway branch.
- The raise does not stand on the truncation. It stands on the three finished seeded episodes crowding the cap (35, 37 and 37 of 40). The crowding rule supports those on its own, and 56 = 1.5 × 37, the same formula `--write` uses.
- The truncated transcript (trial 4) shows convergence, not drift:
  - the 4-step route was walked by call 4;
  - the empty hierarchy and the ANR dialog were diagnosed through logs and the dropbox ANR record;
  - the main-thread stack was in hand by call 21, then one relaunch-and-reproduce;
  - the agent wrote "I have enough evidence … write the findings file now", made one more `adb` call, and was killed.
- Every seeded episode of this case pays the same diagnosis cost. A frozen window has no accessibility hierarchy, so the agent has to wait for the ANR, read logs, and reproduce. That is the price of the defect, not an agent adrift.
- A truncation here scores the episode as not completed and the freeze as missed, on a run where the agent had found it. QUA-2744's runaway (a clean trial with loops of 16 swipes) is the opposite case. This change is in its own commit so review can drop it by itself.

## 4. No cap is lowered

The derived value is below the authored cap for 36 of 41 public cases and 9 of 10 held-out cases (for example, `anki-browse-cards` derives 25 against 30). None is lowered, for three reasons:
1. **One model.** Every MCP-arm episode on disk is claude-fable-5-1. GPT-6 Astra, the second model on the QUA-2786 board, has no MCP-arm episodes, and a cap sized on Fable's thrift would gate Astra on evidence it never produced.
2. **n ≤ 2.** Apart from the two re-run cases, every case rests on one clean and one seeded episode.
3. **One cap gates both arms.** `step_budget` is per case, not per arm. The bare arm spent more: a mean of 24.4 steps on the 2026-09-19 board against 17.4 here. Caps sized on the MCP arm would squeeze the bare arm.

Lowering a cap only saves tokens, and only on an episode that would otherwise have used them. Raising one protects a measurement. The two directions carry different risks, so a lower cap needs its own review, with n ≥ 8 on both models.

**Three healthy cases to watch.** Three cases derive above their cap without crowding it, so they stay where they are: `cal-edit-event` (40/50 seeded, 80%, derives 60), `cal-repeat-survives-rotation` (34/50, derives 51) and `medtimer-take-dose-then-medicine-list` (31/45, derives 47). Each rests on n = 2. A crowded or truncated episode on the comparison board is the signal to re-run them.

## 5. Held-out split (aggregate only)

- 10 cases, 20 episodes, all from the validation run, all finished, 0 truncated.
- Every case is `healthy`. The worst episode per case used 30–62% of its cap.
- No held-out cap moves, and no held-out file was touched.
- At the time `derive_budgets.py` read only the packaged test-case directory, so held-out budgets were derived with the same function pointed at the split, and its `dropped:` lines named held-out case ids. QUA-2799 fixed both: with `QGB_HELDOUT_DIR` set it derives the split itself and prints it as an aggregate block, and its default output names no held-out case. Over this note's two runs that block reproduces the figures above.

## 6. What the re-run showed besides the budgets

- **su denied, ANR-trace check (QUA-2790).** Two medtimer seeded episodes were denied at the adb meter: trial 1 twice (`su 0 cat /data/anr/…`) and trial 2 once (`run-as … kill -3`). Neither changed a step count, and both still found the freeze. No episode tripped the episode-start ANR-trace check (0 `staging_failed`).
- **`adb root` is not denied.** In medtimer seeded trial 3 the agent ran `adb -s emulator-5554 root`, then `adb shell id`, which returned `uid=0(root)`. The meter relayed the `root:` service request, and `adb_counts.json` recorded neither a denial nor the request. That puts a root shell within reach, and `deny_reason`'s `run-as` rule does not cover it. Staging handed the next episode an unrooted device, because the episode-start check passed on trial 4. This is outside this ticket: a follow-up to QUA-2790.
- **`cal-open-task-from-list~seeded` credit.** 1 of 4 re-run seeded episodes was credited with the defect (the validation episode was too). In the other three, the agent described the same dead row tap and quoted `expected` as the brief's own "Mark completed" wording. The scorer counted each as a false report that matches no bug, and the case as not completed. The reports are true statements about the seeded build. This is a scorer or case-authoring question for the comparison run, not a budget one.
- The worst cost per route step a finished episode has paid is still 9.25 (medtimer seeded, 37/4). The script measures every future truncation against that ceiling.
