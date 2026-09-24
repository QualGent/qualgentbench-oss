# Final validation — epic QUA-2723 (2026-09-19)

**Verdict: GO for expanding the corpus to ~200 cases, still NO-GO for publishing a
cross-agent ranking. Every acceptance criterion passes (section 11). This validation also
found a rotation leak that the pilot's staging fix had not closed, and the fix (QUA-2734)
is proven on both staging paths and on a paid, rotation-clean board.**

The board also surfaced an environment fault to fix before the next paid board. None of
the agent's 371 hierarchy-dump commands on emulator-5558 returned a hierarchy (section 8),
so it tested from screenshots throughout. That left 14 completions unscored and 41 of 46
reports ungrounded.

| rotation-clean board, corpus `75649db79d50` | claude-code · claude-opus-5, raw arm |
| --- | --- |
| episodes | 82 scored + 1 excluded (AnkiDroid first-install `env_failure`), 0 contaminated |
| completion | **91.2%** (62/68 scored; 14 unscored, because no hierarchy dump worked on the device) |
| catch per seeded defect | **36/43 = 83.7%** [70-92] |
| false alarm per clean case | **0/41 = 0%** [0-9] |
| precision / recall / F1 | 92.3% / 83.7% / **0.878** |
| started portrait / restored an inherited rotation | **83/83 / 0** |
| cost | **$156.70** ($148.22 reported + $8.48 estimated), under the $210 cap |

This is the epic's merge-readiness check (QUA-2731). It is the only place the whole
corpus is checked as one artifact, and the only place an agent board is paid for.
QUA-2706's pilot (`docs/pilot-2026-09-17.md`) is the standard it is held to. That pilot
reported 10/12 and withdrew its own headline. Every criterion below is reported, and so
is every one that failed on the way.

| | |
| --- | --- |
| corpus_version | **`75649db79d50`**: 41 public cases, 40 defects, 6 apps |
| board | run `20260919-082841-63ec`: claude-code · claude-opus-5, raw arm, brief v1, emulator-5558 (`qgbench_root2`, android-35), one lane, harness at `172d34b` |
| derive evidence | pre-fix harness `18e22c5` on emulator-5556 (29 cases); post-fix harness `4cb6355` on emulator-5558 (12 cases) |
| device-free gates | at `18e22c5`, `4cb6355`, `172d34b` and the final merge `29d59ad` (section 3); corpus_version unchanged by the final merge |
| held-out split | not in scope (owner decision), not class-linted (section 9) |

## Contents

0. How to read the provenance
1. The class mix
2. The persistence retain list
3. Device-free gates
4. The journey adversary check, both copies
5. The whole-corpus re-derive
6. The rotation leak this validation found, and its fix (QUA-2734)
7. The other fixes the validation needed (QUA-2735, QUA-2738)
8. The agent board
9. Owner actions
10. Follow-ups
11. Acceptance criteria
12. Go / no-go

---

## 0. How to read the provenance

The epic's corpus did not stand still while this ticket ran. The validation found two
defects in the harness and one in a route. Each was fixed in its own ticket, and each fix
moved the head. Rows here come from three harness versions, and every row says which:

| head | what it added | used for |
| --- | --- | --- |
| `18e22c5` | all six app children merged, corpus_version `27622b08f0b2` | first whole-corpus re-derive (29 cases survive from it), first live pre-check (FAIL) |
| `4cb6355` | QUA-2734 (re-pin portrait after launch), QUA-2735 (row-scoped take-dose tap); corpus_version `4f498a799464` | post-fix re-derive of 12 cases, live pre-check (PASS ×3) |
| `172d34b` | QUA-2738 (pre-board fixes; comment-only corpus edits); corpus_version `75649db79d50` | the board, pre-board live pre-check (PASS) |
| `29d59ad` | QUA-2739 (merge-readiness: guided and `--mode all` stop scoring journey-only defects, `row:` filter on the tapped element's own bounds, `echo_haystack` widened, docs); corpus_version **still `75649db79d50`** | this report's final gates; merged after the board, touches no journey case or truth (section 4 shows the widened `echo_haystack` changes no task spec) |

Between `18e22c5` and `172d34b` the corpus changed only in medtimer's take-dose case and
in comments (QUA-2738). QUA-2735 gave that case's route its `row:` scope, and `6a6c96a`
(#49) re-derived its truth row. The re-derive changed the row's screens, diff lists and
ANR detail strings, but none of its verdict fields (section 7). No other case's route,
oracle, defect, build or truth row changed. The replayer fingerprint
(`replay.replayer_fingerprint`) did change, from `c4786808b56ac055` to `f9d9ce64221663bd` to `8c515d6c604ed7d2` (the board), and to
`52ba27429a8cf3b2` at `29d59ad` (QUA-2739's `row:` bounds). Those are the re-pin, the `row:`
scope, QUA-2738's step-0 re-pin and the bounds. Section 5 shows why the 29 pre-fix rows stand: the
instrument recorded every one of their 184 passes starting upright on its first attempt,
so the re-pin had nothing to change in them. The owner's decision (2026-09-19) was not to
re-derive them.

## 1. The class mix

`scripts/mix_report.py` over the public corpus. The output is identical at every head
from `18e22c5` to the final merge except for the corpus_version line.

```text
CORPUS (every app: 6) — 40 defect(s), 40 on a case
  bucket             n on a case   share  target   delta  classes
  crash             11        11   27.5%   24.0%    +3.5  crash 11
  display/content   11        11   27.5%   24.0%    +3.5  content-format 11
  persistence        6         6   15.0%   14.0%    +1.0  persistence 6
  navigation         5         5   12.5%   11.0%    +1.5  navigation 5
  lifecycle          3         3    7.5%    7.0%    +0.5  lifecycle 3
  ordering           2         2    5.0%    4.0%    +1.0  ordering 2
  ANR/freeze         2         2    5.0%    3.0%    +2.0  anr 1, stuck 1
  total             40        40  100.0%   87.0%
```

| bucket | n | share | plan target | delta | target rescaled to 100% | share vs rescaled | defects vs rescaled |
| --- | --- | --- | --- | --- | --- | --- | --- |
| crash | 11 | 27.5% | 24% | +3.5 | 27.6% | -0.1 | 11 vs 11.0 |
| display/content | 11 | 27.5% | 24% | +3.5 | 27.6% | -0.1 | 11 vs 11.0 |
| persistence | 6 | 15.0% | 14% | +1.0 | 16.1% | -1.1 | 6 vs 6.4 |
| navigation | 5 | 12.5% | 11% | +1.5 | 12.6% | -0.1 | 5 vs 5.1 |
| lifecycle | 3 | 7.5% | 7% | +0.5 | 8.0% | -0.5 | 3 vs 3.2 |
| ordering | 2 | 5.0% | 4% | +1.0 | 4.6% | +0.4 | 2 vs 1.8 |
| ANR/freeze | 2 | 5.0% | 3% | +2.0 | 3.4% | +1.6 | 2 vs 1.4 |

The plan's targets sum to 87%. Three of its twelve buckets have no class in the
vocabulary: stale display (6%), compatibility (4%) and dead control (3%). So a corpus
drawn only from these classes runs over target in every bucket (`docs/defect-classes.md`
§1). This is exactly the "after" column QUA-2724 predicted (§10 there). Every bucket is
named below, including the ones that miss:

- **No bucket is under its plan target.** Against the raw targets every bucket runs over,
  from +0.5 (lifecycle) to +3.5 (crash, display/content). Rescaled to 100%, every bucket
  lands within one defect of its target.
- **ANR/freeze is the largest relative overshoot**: 2 defects against 1.4 rescaled. Both
  freeze exemplars predate the epic, and they are a deliberate pair, one per detector
  (`anr` and `stuck`). The epic pruned only persistence. At 40 defects the bucket can
  hold 1 (2.5%) or 2 (5.0%).
- **Persistence is the one bucket under its rescaled target**: 6 against 6.4. That is by
  design. The retain list keeps exactly one defect per mechanism family (section 2).
- **Two classes are empty: `layout` (plan 9%) and `widget-inventory` (plan 8%).** The
  display/content bucket meets its 24% in aggregate, but entirely through
  `content-format`: 11 defects, 27.5%, against that class's own 7% in the plan. The corpus
  has never held a layout or widget-inventory defect, and this epic authored no display
  defects (it seeded 10 crash, 4 navigation, 2 lifecycle and 1 ordering). This is a
  composition miss inside a bucket that is on target. It is the corpus's clearest
  authoring gap for the expansion.
- The plan's three unclassed buckets are 0 by construction.

## 2. The persistence retain list

`mix_report.py` counts exactly 6 persistence defects, all on a case, one per mechanism
family. They are QUA-2724's retain list (`docs/defect-classes.md` §6), and none of the 18
pruned persistence defects is still declared under any `defects:` block.

| family | retained defect | app | case |
| --- | --- | --- | --- |
| an edit did not save | `edit-event-not-saved` | fossify-calendar | cal-edit-event |
| field dropped on create | `phone-number-dropped` | fossify-contacts | contacts-phone |
| completion / skip not persisted | `subtasks-left-open` | tasksorg | tasks-complete-parent |
| delete broken | `contact-delete-broken` | fossify-contacts | contacts-delete |
| recurrence | `repeater-done-loses-recurrence` | orgzly | orgzly-complete-repeating-task |
| other: favourite, stock | `favorite-not-saved` | fossify-contacts | contacts-favorite |

## 3. Device-free gates

All runs have `QGB_HELDOUT_DIR` unset.

| gate | `18e22c5` | `4cb6355` | `172d34b` | final merge `29d59ad` |
| --- | --- | --- | --- | --- |
| `pytest` | 1385 passed, 2 skipped | 1409 passed, 2 skipped | 1416 passed, 2 skipped | 1442 passed, 2 skipped |
| `lint_journey_cases.py` | PASS (41 cases, 0 errors, 1 warning) | PASS | PASS | PASS (41 cases, 0 errors, 1 warning) |
| `ruff check --select F821` | PASS | PASS | — | PASS |
| `check_tier_ready.py` easy / medium / hard | READY / READY / READY | READY / READY / READY | — | READY / READY / READY |
| `adversary_check.py` (hunt; easy tier by design) | PASS (honest 0.998, best guesser 0.000) | PASS | — | PASS (honest 0.998, best guesser 0.000) |

The one lint warning is on `contacts-favorite`: the oracle text names none of the brief's
key nouns. It is a warning by design and not a leak. `check_tier_ready.py --tier hard`
sees 10 of the 12 hard apps, because the 2 held-out hard apps are invisible with the split
unset. Guessers score at or below 0 in every tier (easy -1.08 / -0.29 / 0.00, medium
-1.02 / -0.30 / 0.00, hard -1.24 / -0.53 / 0.00 for spray / crud / oracle), and honest
scores 1.00.

**The held-out lint gap is an owner action.** With `QGB_HELDOUT_DIR` exported,
`lint_journey_cases.py` fails with 13 errors. Every one is the `class` rule, and every one
is on a held-out defect: 13 defects across the split's 2 apps. The public corpus is clean.
The split lives outside the repository and is the only copy of its answer keys, so adding
`class:` to those 13 defects is for the split's owner. This validation read the split and
edited nothing in it.

## 4. The journey adversary check, both copies

| run | script | 5 guessers (short-spray, generic-spray, dead, brief-echo, dialog-echo) | honest | honest-text | symptom-spray (priced) |
| --- | --- | --- | --- | --- | --- |
| branch | `scripts/journey_adversary_check.py` | 0/43 bugs, 0 completions, each | 42/43 bugs, 28 completed | 37/43, 25 completed | 43/43 bugs, and paid a false report on 41/41 clean episodes (100%) |
| origin/main | `git show origin/main:scripts/journey_adversary_check.py` (`806f167`), run from the worktree against the branch's corpus and scorer | identical | identical | identical | identical |

Both print PASS at `18e22c5`, `4cb6355`, `172d34b` and `29d59ad`, and the two outputs are
byte-identical at each head. So are the two scripts: sha256 `22163548ac2f…` for both. The
epic never touched the control.

Up to the board's harness (`172d34b`), the epic did not touch the scorer either.
`journey.py` gained only the `DEFECT_CLASSES` constant, and `load_defects` is unchanged,
so no scorer sees a class.

QUA-2739 (#51, merged at `29d59ad`, after the board) then changed the scorer.
- It widened `echo_haystack` (`journey.py:249`). The haystack used to hold every
  `type:`/`tap:` value. It now holds every `type`/`append`/`tap`/`long_press`/`row` value
  (`ECHO_ROUTE_KEYS`).
- That can demote a string from `blocking_texts` to `echo_texts`, or drop one from
  `absence_texts`. On this corpus it does neither.
- All 82 journey task specs are byte-identical when built by `172d34b` and by the epic head
  `8579eab` (the same sha256 over the canonical JSON of every `bug_spec`).
- A dry-run `rescore_journey.py` of the board at `8579eab` changes 0 of its episodes.
  Every scored field of every episode equals the board's, apart from one completion-reason
  string (`medtimer-analysis-tabular-view~clean`, unscored either way). The rescore
  produces the same difference under `172d34b`, because it cannot replay a liveness
  oracle.

So the independence the ticket asks for holds. The control did not move, and the
scorer's one change leaves every task spec on this corpus unchanged.

Honest misses one defect, `subtask-chip-low` on `tasks-complete-parent~seeded`, which has
no quotable evidence. That is a known corpus gap (the script's own CORPUS note), not a new
one.

## 5. The whole-corpus re-derive

`scripts/derive_journey.py`, unmodified, driven through a scratch-only wrapper. The wrapper
changed no verdict. It logged what the stock `--json` cannot carry: every trial's screens
(the JSON keeps only trial 1's), every trial's fired markers, and the display rotation at
every hierarchy dump the replayer already takes. It also read the `user_rotation` setting
after each reset and after each launch. Every app was derived at `--repeat 3`, the two
ordering cases (`cal-search-event`, `tasks-add-subtask`) also at `--repeat 5`, and every
derive stayed inside one device day (America/Chicago). Each APK's sha256 was confirmed on
the device (`qg_app_build_fingerprint`) before and after its derive, and equals both the
build list and the case file's `apk:` block.

**Every case agrees with its committed key, and the flip rate is 0/86 checks.** No
committed truth file moved.

| app | provenance | cases | agree | checks stable | passes |
| --- | --- | --- | --- | --- | --- |
| ankidroid | pre-fix (`18e22c5`, 5556, 12:29-12:56 CDT 09-18) | 5 | 5 | 10/10 | 30 |
| fossify-calendar | **post-fix** (`4cb6355`, 5558, 16:19-17:18 CDT 09-18) + `cal-search-event` ×5 | 9 | 9 | 18/18 + 2/2 | 54 + 10 |
| fossify-contacts | pre-fix (6 cases, 11:42-12:27 CDT) + **post-fix** `contacts-new-contact-survives-rotation` (17:19 CDT) | 7 | 7 | 12/12 + 2/2 | 36 + 6 |
| medtimer | pre-fix (7 cases, 09:41-10:26 CDT) + **post-fix** `medtimer-take-dose-then-medicine-list` (16:12 CDT) | 8 | 8 | 14/14 + 2/2 | 42 + 6 |
| orgzly | pre-fix (5 cases, 12:58-13:43 CDT) + **post-fix** `orgzly-new-note-survives-rotation` (17:26 CDT) | 6 | 6 | 10/10 + 2/2 | 30 + 6 |
| tasksorg | pre-fix (13:45-14:19 CDT) + `tasks-add-subtask` ×5 | 6 | 6 | 12/12 + 2/2 | 36 + 10 |
| **total** | 29 pre-fix, 12 post-fix | **41** | **41** | **86/86** | **266** |

The pre-fix sweep covered all 41 cases: 266 passes, 86 checks, 0 flips, and 40 of 41
cases agreeing. It found one disagreement and one hidden leak. The disagreement was
`medtimer-take-dose-then-medicine-list`: the clean arm was VIOLATED 3/3 after 08:00
device time. That was a route fault, fixed in QUA-2735 (section 7). The hidden leak was
the rotation leak in section 6. The post-fix sweep re-derived every case either one could
touch.

**Markers.** Every seeded death fired its own marker: 55/55 crash, ANR, stuck and
ordering trials pre-fix, and 17/17 post-fix. So did every seeded navigation and lifecycle
arm. No clean pass fired any marker (133 clean passes pre-fix, 41 post-fix). The
persistence and display defects carry no marker site by design, and fired none.

**Post-crash screens.** No recorded screen of any pass shows a foreign app, so QUA-2733's
home-before-launch isolation held.
- (a) A crash in the task's root activity lands on the launcher. That is medtimer ×3,
  fossify-contacts ×2, orgzly's indent crash, tasksorg ×2 and `cal-search-event`.
- (b) A crash in a secondary activity resumes the app's own previous screen. That is
  fossify-calendar's two editor crashes and ankidroid's two.
- On the second consecutive crash of the same case, 9 of the 49 Java-crash trials showed
  Android's own "<App> keeps stopping" dialog for the app under test (`android` package,
  App info / Close app) instead. The platform rate-limits a repeat crash that way, so it is
  not a foreign app.
- The ANR case shows the "isn't responding" dialog 3/3, and the stuck case leaves the
  frozen app, whose hierarchy cannot be dumped, 3/3.

**AnkiDroid, mixed provenance resolved.** QUA-2725's truth had three rows derived before
the ACRA fixture fix and two after it. The pre-fix sweep was the first whole-app derive on
the fixed setup: 5/5 agree and no verdict field moved. The two crash cases land on the
deck list, and their second crash shows the platform's own "keeps stopping" dialog, not
ACRA's Feedback dialog. That shows ACRA really is off.

## 6. The rotation leak this validation found, and its fix (QUA-2734)

`docs/pilot-2026-09-17.md` §0 records the leak that contaminated the pilot. A rotation
the agent performed on the rotation case stayed on the device and turned every later
episode sideways. The staging fix in `732dfac` made the live path reset orientation with
the same helper replay uses (`replay._set_rotation`, called from
`episode_runner.normalize_app_env`). **That fix did not hold on android-35.** This
validation measured it failing on both paths before any board money was spent.

**The mechanism**, as QUA-2734 read it off `dumpsys window displays` on emulator-5558,
whose `RotationLockHistory` names the caller of every user-rotation write. The Pixel launcher
requests `SCREEN_ORIENTATION_NOSENSOR`.
1. When the launcher becomes the top fullscreen activity,
   `DisplayRotationReversionController.updateForNoSensorOverride` saves the locked user
   rotation. After a pass that force-stopped the app in landscape, that saved value is
   `ROTATION_90`.
2. The display then draws at 0, and SystemUI re-locks the user rotation at 0. That 0 is
   what a reader sees on the launcher.
3. Staging then pins portrait. Both `_reset` and `normalize_app_env` write the pin with the
   launcher on top, because `isolate_app_under_test` ends by sending HOME. The pin changes
   the setting, not the saved value.
4. When the app replaces the launcher, `revertOverride` writes the saved `ROTATION_90`
   back, and the app draws landscape.

A pin written while an app is in front holds.

**Before: the live path.** The pre-check reproduced the precondition with the harness's
own functions, then staged exactly as `run_episode` does and read orientation with the app
in front. The steps were: launch the app, `replay._rotate` to landscape, force-stop it
(launcher now in front), then reinstall, `pm clear`, `normalize_app_env`, device setup,
flags, `isolate_app_under_test` and `launch_app`. It **FAILED on fossify-calendar, orgzly
and medtimer** (`18e22c5`, emulator-5556):

| reading | user_rotation | display |
| --- | --- | --- |
| launcher in front, after the landscape force-stop | 0 | 0 |
| after `normalize_app_env` (launcher in front) | 0 | 0 |
| app launched by live staging | **1** | **1 (landscape)** |
| same, +5 s | **1** | **1** |

A live board at `18e22c5` would have handed every episode after a landscape stop a
rotated device. That is §0's signature exactly, so the board was held until the fix
landed.

**Before: the replay/derive path, where the leak was masked.** `cal-repeat-survives-rotation`
ends each pass with a `db:` read, which force-stops the app in landscape. In the pre-fix
fossify-calendar derive, 6 attempts started landscape: 5 of that case's 6 trials, plus the
first clean pass of the case after it, `cal-complete-task`. For each, the instrument logged
`user_rotation` 0 after the reset, 1 the moment the app launched, and every hierarchy dump
at rotation 1. Each of those attempts went INCONCLUSIVE because an anchor was missing in
landscape. `one_pass`'s INCONCLUSIVE retry then re-ran it. That retry's reset happened
with the app in front, so its pin held, and the recorded attempt started upright. The
verdicts came out right for the wrong reason. The "counter-evidence" that
`cal-repeat-survives-rotation` was violated 3/3 was this masking, not the leak's absence.
The leak is not orgzly-specific. It is device-wide: the next app launched inherits the
saved rotation, whatever app it is.

**The fix (QUA-2734, PR #48, `975832e`).** `replay.repin_portrait_after_launch` waits
until the app under test is the resumed activity, pins portrait, then waits for the
screen to settle. It runs in three places:
- the route's `launch` step (`replay._launch`, shared by `run_steps` and derive_journey's
  executor);
- `episode_runner.run_episode` right after `launch_app`;
- `derive_journey.stage()`.

QUA-2738 added a route that opens with `relaunch` (section 7).

**After.**

| check | head, device | result |
| --- | --- | --- |
| live pre-check, fossify-calendar / orgzly / medtimer, now through `take_replay_snapshots` (the screen handed to the agent) | `4cb6355`, 5558 | **PASS ×3.** The leak condition reproduced every time (1/1 before the re-pin); after the re-pin 0/0, handed to the agent 0/0, +5 s 0/0, app in front |
| live pre-check, fossify-calendar | `172d34b`, 5558, before any board money | **PASS**, same readings |
| fossify-calendar re-derive, `--repeat 3` + `cal-search-event` `--repeat 5` | `4cb6355`, 5558 | 64 passes, **every one on its first attempt**. The leak condition still arose on the same 6 passes (`user_rotation` 1 at launch), and the re-pin corrected each before the route's first step |
| the three rotation cases, `--repeat 3` | `4cb6355`, 5558 | 18/18 trials start portrait (rotation 0 at the first route dump) and really rotate (rotation 1 at the first dump after `rotate`, `user_rotation` 1 / `accelerometer_rotation` 0); every seeded arm fired its own lifecycle marker |

The board's own rotation evidence, read against §0's signature, is in section 8.

## 7. The other fixes the validation needed (QUA-2735, QUA-2738)

**QUA-2735, PR #49: the medtimer take-dose route.** In the pre-fix sweep,
`medtimer-take-dose-then-medicine-list` failed the derive gate. Its clean arm was
VIOLATED 3/3 at 09:41-10:26 device time. Once the device clock passes 08:00, Aspirin's
8:00 AM reminder is raised as well. Every raised reminder's status icon reads "Reminded",
and the seeded Ibuprofen rows carry the staging time, so Aspirin's row is listed first.
The route's bare `{tap: Reminded}` therefore answered Aspirin. The recorded screens show
Aspirin 10 → 8 left and Ibuprofen untouched, where the committed key, staged at 00:17,
shows Ibuprofen 10 → 6. The committed verdict was right and the route was wrong.

The fix re-derived the row anyway, in `6a6c96a` (#49), 15:49-15:55 CDT on emulator-5558.
- The row's screens, diff lists and ANR detail strings changed.
- Its verdict fields did not change: expected, measured, agrees, blocking, side,
  problems, trial outcomes and stability.
- For a death case the scorer never reads the diff lists. `journey_tasks` builds crash
  evidence instead, so no score can move.

The fix is a harness-only route qualifier, `{tap: Reminded, row: "Ibuprofen (4)"}`,
resolved in `replay._candidates`.
- **As merged in QUA-2735**, it kept only candidates whose clickable control shares a
  horizontal band with an element labelled exactly "Ibuprofen (4)".
- **QUA-2739 (#51, merged at `29d59ad`, after the board)** narrowed that to the tapped
  element's own bounds (`replay.py:196-216`), because a clickable container that spans
  several rows overlaps every row's band.

This validation re-derived the case with the earlier form, after 08:00 on the post-fix
harness `4cb6355`: 16:12 CDT, with Aspirin raised and listed first. Clean HOLDS 3/3
(Ibuprofen 10 → 6, Aspirin untouched at 10). Seeded ANR 3/3, its own marker fired, and
each pass needed one attempt.

The board (`172d34b`) also ran the earlier form. No device run has used the own-bounds
form yet: QUA-2739 tested it against a fixture (section 10).

QUA-2735 also left `TODO(QUA-2735 follow-up)` notes in `data/benchmarks/medtimer.yaml`.
The hard-tier hunt checks `event_take` and `dose_stock` still use the bare anchor, so
hard-tier truth for those two features depends on time of day (section 10).

**QUA-2738, PR #50: pre-board fixes.**
- `assert_precondition` honours `row:`, so the live precondition for the take-dose case
  looks for "Reminded" in Ibuprofen's row.
- A route that opens with `relaunch` re-pins portrait like one that opens with `launch`.
- It adds tests for the row retry path.
- It closes the stale orgzly `TODO(harness)` and 10 `TODO(derive)` comments that the
  2026-09-18 re-derives satisfied. Those are comment-only corpus edits, so they moved
  `corpus_version` from `4f498a799464` to `75649db79d50` and nothing else.

## 8. The agent board

**Scope, as the owner signed it off (2026-09-19).**
- Agent: claude-code · claude-opus-5, raw arm (the agent drives adb, no MCP tools), brief
  v1.
- Cases: all 41 public cases, each in both versions (clean and seeded), one trial: 82
  units.
- Held out: the held-out apps (not class-linted, section 9) and codex (dropped from the
  plan).
- Device: emulator-5558, one lane, harness at `172d34b`, corpus_version
  `75649db79d50`, run from this worktree with the six hash-checked local builds in `dist/`.
- Run: `20260919-082841-63ec`, 08:28Z to 13:34Z (03:28 to 08:34 device time, America/Chicago,
  so it did not cross device midnight).

| | |
| --- | --- |
| episodes recorded | **83**: all 82 units, plus 1 excluded attempt that was re-run |
| excluded | 1 (`env_failure`, below); 0 `infra_failure`, 0 contaminated |
| corpus_version | **`75649db79d50` on all 83** (`board.json`: `mixed_corpus: false`) |
| built APK on every episode | the sha256 recorded in each episode's `evidence/meta.json` equals the verified build for its app, 83/83 |
| brief / device / segments | v1 on 83/83; emulator-5558 on 83/83; segment 0 (1 episode), segment 1 (82) |
| adb meter | 2087 metered commands, `metered_denied` 0 |
| cost | **$156.70**: $148.22 `reported` (79 episodes) + $8.48 `estimated` (4 truncated episodes); 0 unavailable |
| wall clock | 5 h 06 min elapsed, including a 7-minute pause between segments; median agent time 2.3 min per episode (mean 2.6), about 3.6 min per unit including staging and verification |

### Spend control

The harness's only spend guard, `--stop-at-seven-day-pct`, is a percentage of the
subscription's weekly window, not dollars. The owner's $210 cap was therefore enforced
two ways:

- **Dollars.** A scratch watcher read every finished episode's own `metrics.cost_usd`.
  It would have stopped the run at a unit boundary: after a `finish` and before the next
  agent started, so nothing paid for is thrown away. The rule was: spent plus the largest
  episode so far would pass $210. It also ran the owner's projection check after 15
  episodes.
- **The weekly window.** The run started with the guard at 1%, which stopped it cleanly
  after its first episode. That episode was the probe. It read the weekly window at 5% and
  gave a first cost figure. The run was then resumed as segment 1 with the guard at 25%.
  That is 20 points above the start, about $256 at the pilot's measured rate (2 points per
  $25.67), so it was a backstop that could only trip after the dollar cap.
  The window ended at 15% after $156.70, about $15.7 per point, so the guard never came
  close to tripping.

The checkpoint at 15 episodes projected **$174.00** (spent $31.45, mean $2.10), under
the cap, so the board continued without a pause. The final spend was $156.70: under the
$210 cap and under the $175-188 the owner approved. Clean arms cost $65.24 (mean $1.59),
seeded arms $88.10 (mean $2.15), and the excluded attempt $3.36. Per-episode costs are in
the appendix.

### Rotation-clean, checked against §0's signature

The check was done, not assumed. Each episode's start orientation was read off the
harness's own first recorded frame (`evidence/frames`, `hook_count` 0, captured before
the agent's first call). Every orientation command the agent sent was read out of
`evidence/steps.jsonl`. The same reader, run over the pilot's claude run, reproduces §0's
table exactly: 7 of 12 episodes started landscape, and `cal-repeat-survives-rotation~seeded`
read `user_rotation` at step 14 and reset it at step 15.

| | pilot claude run `20260917-021029-67f4` | this board |
| --- | --- | --- |
| episodes starting portrait | 5/12 | **83/83** |
| episodes that restored an inherited orientation before their task (§0's signature) | 1 (and 2 more in the codex run) | **0** |
| landscape frames outside the rotation cases | 6 episodes (all of medtimer) | **none** |

Landscape appears on this board only in the six rotation-case episodes, and there only
after the agent's own `user_rotation 1` (the step the brief asks for). Four metered steps
on the whole board touch orientation outside that. Each is disclosed here, and none is
the leak:
- Two clean rotation-case episodes read the orientation at step 1 before rotating
  (`orgzly-new-note-survives-rotation`, `contacts-new-contact-survives-rotation`). Both
  read `ROTATION_0`.
- Two episodes put the device back to portrait after finishing their own rotation
  (`cal-repeat-survives-rotation~clean` at step 33, `contacts-new-contact-survives-rotation~seeded`
  at step 36). That is tidying up, not undoing an inherited rotation.

The rotation-clean steps now exist for the §9 budget question: `cal-repeat-survives-rotation`
finished at 30/50 and 31/50 steps. The pilot's 49/50 seeded finish spent its steps 14-15
on the leak. The twelve episodes this board shares with the pilot moved the same way,
with the same completions and bugs. The seven that ran landscape in the pilot now start
portrait, and the medtimer ones are shorter: take-dose clean 22 → 11 steps, tabular-view
clean 20 → 12. That is n = 2 per case and one agent, so the pilot's n≥8 re-derivation is
still owed before the budget moves.

### Results

`board.json` journey summary: claude-code · claude-opus-5 · raw · 82 scored episodes.

| | |
| --- | --- |
| completion | **91.2%** (62/68 scored: clean 29/30, seeded 33/38); 14 unscored |
| bugs found / present | **36/43** |
| false reports | **3**, all on seeded arms, 0 on clean arms |
| precision / recall / F1 | 92.3% / 83.7% / **0.878** |
| false alarm per clean case | **0/41 = 0%** [0-8.6] |
| catch per seeded defect | **36/43 = 83.7%** [70.0-91.9] |
| blocker recall (functional L4+L3) | 20/25 = 80% [60.9-91.1] |
| clean-run integrity @200 | 100% [0-100] |
| truncated | 4 (3 seeded, 1 clean) |
| mean steps | 24.4 |

The 14 unscored completions are PASS-expected arms whose outcome is screen text: 11 clean
arms, and 3 seeded arms whose defect does not block the outcome. The agent reported pass
on each. None of its hierarchy dumps worked on this device (below), so it read every
screen as an image. With no device text to check the witness against, the harness leaves
those arms unscored rather than false.

**Catch by class** (seeded arms, per defect):

| bucket | found / present |
| --- | --- |
| crash | 9/11 |
| display/content | 12/14 |
| persistence | 5/6 |
| navigation | 4/5 |
| lifecycle | 2/3 |
| ordering | 2/2 |
| ANR/freeze | 2/2 |
| **total** | **36/43** |

By app: ankidroid 5/5, fossify-calendar 5/9, fossify-contacts 7/7, medtimer 9/9,
orgzly 4/6, tasksorg 6/7.

**Every miss and every false report, read from the episode.** Each of the agent's own
reports was re-fed through the real `journey.match_report` against the task's real spec,
flipping one input at a time. That tells a scoring artifact apart from a testing miss, the
standard from the pilot's §1.

| episode | recorded | what it actually was |
| --- | --- | --- |
| `cal-complete-task~seeded` | crash missed, agent said PASS | **A corpus defect.** The seeded patch writes the completion row and then throws in a secondary activity. The event list resumes showing the task completed, which is exactly the brief's expected outcome. The committed truth's clean/seeded screen diff for this case is **empty**: the crash is visible only in the log. The agent's PASS is what the screen supports (section 10) |
| `cal-create-all-day-event~seeded` | truncated 46/45, no report | budget. The agent was still exploring the defect: 22 actions vs 10 on the clean arm, and 5 failed `uiautomator dump` calls |
| `cal-edit-event~seeded` | truncated 51/50, no report | budget: 22 actions vs 12 clean, 4 failed dumps |
| `cal-open-task-from-list~seeded` | truncated 46/45, no report | budget: 19 actions vs 9 clean, 4 failed dumps |
| `orgzly-new-note-survives-rotation~seeded` | lifecycle defect missed, and its report charged as false | **A scoring artifact.** The report is right, verbatim: "The title typed into the new note is lost when the phone is turned sideways", observed `Title` (the placeholder), expected `Pay the rent`. `Title` is on the echo route, which needs a device-text sighting. No hierarchy dump worked on this device, so the agent read the screen as an image. None of its words is on the symptom list. Setting `grounded=True` credits it, and so does adding the listed symptom `cleared`. This is the pilot's prerequisite 1 again |
| `tasks-complete-parent~seeded` | display defect `subtask-chip-low` missed | **A scoring artifact.** The agent reported it exactly: "The subtask count chip ... reads 1, but the task has two subtasks", observed `1`, expected `2`. The one-character marker is below the evidence floor, so the matcher credited that report to the case's other defect. The honest control misses this defect too (section 4) |
| `orgzly-create-and-search~seeded` | display defect `notebook-count-off-by-one` missed | a genuine miss: no report mentions the notebook count |
| `contacts-new-contact-survives-rotation~seeded` | 1 false report | a third, truthful report of the found defect's consequence ("Alice was never created") matched nothing: the thoroughness charge, the pilot's prerequisite 2 |
| `medtimer-take-dose-then-medicine-list~seeded` | 1 false report | a second, truthful report ("The Ibuprofen dose was never recorded as taken") matched nothing: the same charge |
| `contacts-favorite~clean` | not completed, truncated 42/40 | budget: `derive_budgets.py --mode journey` calls the case **under-budget** and proposes 40 → 57; 5 failed dumps |

Of the 7 misses:
- 3 are budget truncations on fossify-calendar seeded arms. `derive_budgets.py` calls
  them "runaway", since the clean arm finished in 56-60% of the cap. But the seeded arm
  is where the agent has a defect to chase, so the clean arm is a poor comparator. Each
  also spent 4-5 steps on hierarchy dumps that returned nothing, against a 1-step
  overrun. The transcripts should be read before any cap moves.
- 1 is a corpus defect with no visible symptom.
- 2 are scoring artifacts.
- 1 is a real miss.

All 3 false reports are true statements about seeded defects. No clean arm carried a
false report. `derive_budgets.py` also proposes `orgzly-complete-repeating-task` 60 → 87:
one finished arm used 97% of its cap.

### The weak-witness cases (QUA-2739 audit, fix QUA-2740)

| case | clean arm completion credit | did the agent do the action? |
| --- | --- | --- |
| `cal-switch-back-to-list` | **unscored**: screen read as images, no device text | yes. It tapped Change view (884,252) → Yearly view (step 25; the saved screen after it shows the 2026 year grid) → Change view → Simple event list (step 31) |
| `orgzly-open-note-from-notebook` | **unscored** | yes. It tapped the note (step 10); the saved screen after it is that note's editor, titled "Click on the note to open it" |
| `orgzly-new-note-survives-rotation` | **unscored** | yes. `settings put system user_rotation 1` at step 20, with landscape frames after it |

No completion credit on these three was granted by the weak witnesses on this board. The
clean arms stayed unscored because no hierarchy dump worked on this device, so the agent's
screen readings carried no device text.
The seeded arms are expected-FAIL arms and never consult a witness:
`cal-switch-back-to-list~seeded` and `orgzly-open-note-from-notebook~seeded` completed on
fail plus the blocking bug named, and `orgzly-new-note-survives-rotation~seeded` is the
scoring artifact above. An agent that reads the hierarchy as text would get these three
witnesses scored, and could be credited without doing the action. That is still open
until QUA-2740 lands.

### Three environment facts the board surfaced

- **No hierarchy dump the agent ran returned a hierarchy.** Every one of the 83 episodes
  tried `uiautomator dump`, 3 to 7 times: 371 dump commands, 0 hierarchies, raw or parsed.
  288 were SIGKILLed on the device (`Killed` / exit 137), and the rest wrote no file or
  printed nothing. Both forms failed. That includes `adb exec-out uiautomator dump
  /dev/tty` (0/118), the form that succeeded 98.8% of the time in the pilot's historical
  codex episodes (pilot §5). The agent fell back to `screencap` images (713 screenshot
  steps). The consequences run through the results above:
  - the 14 unscored completions;
  - 41 of the 46 reports were ungrounded. The 5 grounded ones quote logcat or other
    non-screen text. This is what left the orgzly lifecycle report uncredited;
  - 4.5 steps per episode were spent on failed dumps, more than the overrun in all four
    truncated episodes.

  The pilot's two runs on emulator-5556 were nearly the same: 1 of 24 episodes got a
  hierarchy back, once. The pilot's §6 recorded exit 137 as an emulator property. The
  harness never noticed, because its own reader (`_dump_vh_raw` in `verify/device.py`)
  falls back to uiautomator2 whenever the built-in dump fails, and neither a derive nor
  an episode records which one served it. The cause is not established here. The pilot's
  leading hypothesis is a collision with a resident uiautomator2 service. The harness
  starts that service itself: its `type` step and its dump fallback both call
  `u2.connect`. Section 10 has the check.
- **The excluded attempt: AnkiDroid's first install on this device.** The run's first
  unit, `anki-browse-new-deck~clean`, failed its precondition. The staged app showed
  "AnkiDroid directory is inaccessible"; logcat shows `StorageAccessException: No write
  access to AnkiDroid directory`. The fixture's `root: true` had run `adb root`, and its
  `mkdir`/`cp` had created a root-owned collection directory for a package the device had
  never seen. The harness marked the episode `env_failure` (excluded) but still ran the
  agent: $3.36, 49 steps. With a root shell, the agent repaired the directory itself
  (`chown`) and finished the task. Before resuming, three zero-cost re-stagings with the
  harness's own staging and precondition functions all passed. The unit re-ran cleanly,
  and no other episode on the board was excluded. Section 10 has the fixes.
- **adbd ran as root for the whole board**, from AnkiDroid's staging onward (the run's
  first app). No agent read anything the meter guards: `metered_denied` is 0. Apart from
  the `chown` above, the only root-adjacent commands were a `content query` of the contacts
  provider (a legitimate check) and temp files the agent itself named `qgb_*`. Section 10
  has the recommendation.

**Device fingerprints.** The protocol asks for `qg_app_build_fingerprint` on each app
before its episodes. That could not be done. Three `qg_acquire_device` calls on
emulator-5558 (08:08Z, 08:28Z, 13:35Z) succeeded, and each lost its session seconds later
to a connector reconnect (QUA-2716). The one permitted re-acquire after each was refused
as device-busy, held by the orphaned session. The board drives adb directly and was
unaffected. The build evidence that stands:
- `preflight` hash-checked all six `dist/` APKs;
- the harness installs from `dist/` at every app switch (`prepare_app` raises if the
  install fails) and reinstalls every trial;
- each episode's evidence records the sha256 of the build it installed, equal to the
  verified build 83/83;
- the device-side fingerprints taken on 2026-09-18 on emulator-5558, for calendar,
  contacts, medtimer and orgzly, matched the same builds before and after the post-fix
  derives.

## 9. Owner actions

1. **Upload the six journey APKs to HuggingFace BEFORE this epic merges to main.** Every
   `apk:` block in `data/test-cases/*.yaml` carries the new builds' sha256, and none of
   those builds has been uploaded. `fetch_seeded_apk` sha256-checks every download, so a
   fresh clone (and the Docker image's `scripts/bake_apks.py`) fails loudly for all six
   journey apps until they are. This validation used only the local builds, copied into
   `dist/` and hash-checked against both the build list and the `apk:` blocks. The command
   is written in each case file: `HF_TOKEN=... uv run python scripts/publish_apk.py <app>
   --kind journey --write --upload --yes`.

   | app | sha256 | bytes |
   | --- | --- | --- |
   | ankidroid | `eb909e03fcebb120…` | 62 149 562 |
   | fossify-calendar | `5cea276f7456338f…` | 32 714 470 |
   | fossify-contacts | `2bc430e91e8c25e7…` | 31 034 023 |
   | medtimer | `0d1578a2ab3875a9…` | 71 342 189 |
   | orgzly | `ca078591f97ea44b…` | 27 068 175 |
   | tasksorg | `8be0720d4023a28f…` | 57 287 738 |

2. **Add `class:` to the 13 held-out defects.** Until then, `lint_journey_cases.py` fails
   whenever `QGB_HELDOUT_DIR` is exported, and `mix_report.py --root "$QGB_HELDOUT_DIR"`
   cannot report the split's mix. The split lives outside the repository.
3. **DevLoop QUA-2716 (connector flaps).** In this validation, the MCP connection dropped
   and orphaned the device lock several times while derives ran. Around the board, all
   three `qg_acquire_device` calls on emulator-5558 were orphaned seconds after they
   succeeded: two before it (08:08Z, 08:28Z) and one after it (13:35Z). The single
   re-acquire that followed each was refused as device-busy. The board drives adb directly
   and was unaffected. But the device-side build fingerprints the protocol asks for could
   not be taken (section 8). The 13:35Z lock was left held by the orphaned session. This
   session never held the device again after the drop, so it did not call
   `qg_release_device`. Clear the lock in DevLoop before emulator-5558 is used again.

## 10. Follow-ups

These are filed or to be filed by the controller. None of them is fixed here: this ticket
changes no source.

- **The agent's hierarchy dumps are dead on the board's emulator (new, found by the
  board).** 0 of 371 `uiautomator dump` calls returned a hierarchy across all 83
  episodes, in both forms, and the pilot's emulator-5556 runs were nearly the same
  (section 8). Fix this before the next paid board:
  - While an episode runs, check the device for a resident uiautomator2 process. If that
    is the cause, stop it after staging and before the agent starts. The harness's own
    reads can keep their fallback.
  - Add a preflight that runs the agent's own `uiautomator dump` once per device and
    refuses the board if it is killed.
  - Record which source served each of the harness's own dumps (`dump_stats`) in derive
    and episode artifacts.

  Until then, a raw-arm board cannot score completion on a screen-text case, and grounds
  only what reaches it through logcat or other non-screen text.
- **`cal-complete-task` has no visible seeded symptom (new, found by the board).** The
  seeded patch writes the completion row and then throws in the task editor, a secondary
  activity. The event list resumes, showing the task completed. The committed truth's
  clean/seeded screen diff is empty, so the seeded arm looks exactly like the brief's
  expected outcome, and only the log shows the crash. A UI tester's PASS there is correct
  by the brief. The board charged it as a missed crash and a failed completion. Fix the
  case before the expansion: throw before the write, or name the crash in the brief's
  observable outcome. Also add a derive-time check that flags any FAIL-expected case whose
  seeded arm dies with an empty clean/seeded screen diff. `contacts-phone` and
  `contacts-favorite` also have empty diffs, but their defects are visible once the agent
  opens the verification screen the brief asks for.
- **Budgets.** Four board episodes were truncated, and a truncated seeded episode scores
  as not completed AND as every seeded defect missed.
  `derive_budgets.py --mode journey`, run without `--write`, proposes `contacts-favorite`
  40 → 57 and `orgzly-complete-repeating-task` 60 → 87. It calls the three
  fossify-calendar seeded truncations "runaway" against their clean arms. Those seeded
  arms spent about twice their clean arm's actions chasing the defect. Every truncated
  episode also spent 4-5 steps on dead `uiautomator dump` calls, more than its overrun
  (section 8). Read the transcripts, and re-derive at n≥8 before any cap moves.
- **QUA-2740: the three weak witnesses** (QUA-2739's audit). On `cal-switch-back-to-list`,
  `orgzly-open-note-from-notebook` and `orgzly-new-note-survives-rotation`, the completion
  witness can be read before the action under test. The fix moves `corpus_version`, so it
  was deferred until after this board. Section 8 shows these cases' credit separately.
- **AnkiDroid staging on a device where it was never installed.** The fixture's
  `root: true` runs `adb root`, so its `mkdir`/`cp` of the collection directory run as
  root. On emulator-5558, where AnkiDroid had never been installed, the app's first launch
  then failed with `StorageAccessException: No write access to AnkiDroid directory`.
  `assert_precondition` caught it (`env_failure`, excluded), but only after the agent had
  run (section 8). Re-staged three times after that first run, the precondition passed
  every time. `root: true` looks vestigial: the one root-only step it existed for, the
  LeakCanary `pm disable`, was removed on 2026-09-14. Removing it, or chowning the
  directory to the app's uid after the copy, would make the fixture portable.
- **An episode whose precondition failed still runs the agent.** `assert_precondition`
  records `staging_failed` (so the episode is excluded) and the harness then launches the
  agent anyway. On a paid board that is money spent on an episode that cannot count ($3.36
  here). Consider ending the episode before the agent when the verdict is `missing`.
- **adbd stays root after any `root: true` fixture.** From AnkiDroid's staging onward,
  every episode's agent had a root adb shell. On this board only the AnkiDroid probe
  episode used it (a `chown` that repaired the fixture). `adb_meter.deny_reason` still
  refused none of this board's commands (`metered_denied` 0 everywhere). Consider
  `adb unroot` after `device_setup`, so an episode's privileges do not depend on which app
  ran before it.
- **`derive_journey.py` cannot show a masked retry.** The truth row records each trial's
  outcome but not how many attempts `one_pass` made, and it keeps only trial 1's screens.
  An INCONCLUSIVE-then-retried attempt is exactly how the rotation leak hid for a day.
  Recording `attempts` per trial would make that visible.
- **The own-bounds `row:` filter has not run on a device.** QUA-2739 changed how
  `{tap: Reminded, row: "Ibuprofen (4)"}` resolves after this case's last derive and
  after the board (section 7). Re-derive `medtimer-take-dose-then-medicine-list` once at
  the epic head, `--repeat 3`, after 08:00 device time, in the next device session.
- **QUA-2735 follow-up (medtimer hard tier).** The hunt checks `event_take` and
  `dose_stock` in `data/benchmarks/medtimer.yaml` still use the bare "Reminded" anchor,
  so their hard-tier truth depends on the time of day. The fix is the same `row:` scope,
  then re-derive the hard tier against the hunt APK.
- **The pilot's two prerequisites for a published cross-agent ranking still stand**
  (`docs/pilot-2026-09-17.md` §12): credit a true sighting the agent read as an image,
  and stop charging thoroughness. This board measured both again. The orgzly lifecycle
  report was correct and uncredited (grounding/vocabulary). Two truthful consequence
  reports were charged as false. And `subtask-chip-low` was reported exactly but
  attributed to the other defect, because its one-character marker is below the
  evidence floor. Neither prerequisite blocks corpus expansion. Both block publishing an
  F1 or catch-rate ranking.
- **Author `layout` and `widget-inventory` defects in the expansion** (section 1).

## 11. Acceptance criteria

| criterion (QUA-2731) | result | evidence |
| --- | --- | --- |
| `mix_report.py` output in the report, every class's delta against the plan's targets, every miss named with its shortfall and reason | **PASS** | Section 1. No bucket is under target, because the targets sum to 87%. The misses named: persistence 6 vs 6.4 rescaled (by design), ANR/freeze 2 vs 1.4 (a deliberate pair), and `layout` and `widget-inventory` empty inside an on-target display/content bucket |
| exactly 6 persistence defects remain, one per mechanism family, matching QUA-2724's retain list | **PASS** | Section 2 |
| corpus-wide derive flip rate recorded and no case unstable | **PASS** | Section 5: **0/86** checks flipped on the final evidence set (0/112 across both sweeps), 41/41 cases agree. The one pre-fix disagreement was a route fault, fixed (QUA-2735) and re-derived. 29 rows are from the pre-fix harness by owner decision, and section 5 shows the re-pin could not have changed them |
| full test suite passes; `lint_journey_cases.py` passes | **PASS** | Section 3: 1442 passed at the final merge, lint 0 errors. Lint fails with the held-out split exported: an owner action, section 9 |
| both adversary scripts, the branch's and `origin/main`'s unmodified copy, pay guessers nothing | **PASS** | Section 4: 0/43 bugs and 0 completions for all five guessers in both, with byte-identical outputs |
| the agent board ran with no rotation leak, verified against §0's signature | **PASS** | Section 8: 83/83 episodes start portrait. No episode restored an inherited orientation. Landscape appears only after the agent's own rotation in the six rotation-case episodes. Four voluntary orientation steps (two reads, two tidy-ups) are disclosed |
| one `corpus_version` covers the whole board, quoted in the report | **PASS** | `75649db79d50` on 83/83 episodes; unchanged by the final merge to `29d59ad` |
| an explicit go / no-go with evidence per criterion, burying no failure | **this section and the next** | |

What failed on the way, and was not waived:
- **The first live pre-check FAILED on all three apps it covered** (section 6). No board
  ran until QUA-2734 landed and the pre-check passed ×3, then passed again on the board's
  own head.
- **The pre-fix sweep had one disagreeing case**, `medtimer-take-dose-then-medicine-list`.
  It was fixed in QUA-2735 and re-derived after 08:00 to agreement.
- **The board's first episode was excluded** (`env_failure`, AnkiDroid's first install,
  $3.36) and re-run.
- **The protocol's device-side build fingerprints could not be taken** around the board
  (QUA-2716). Section 8 lists the build evidence that stands in for them.

The board itself surfaced five things, reported rather than folded into a pass rate:
- No hierarchy dump the agent ran returned a hierarchy (0/371), so it tested from
  screenshots throughout.
- `cal-complete-task`'s seeded defect has no visible symptom.
- Four episodes were truncated.
- Two defects the agent reported correctly went uncredited, and all three false reports
  were true statements (one of them is the uncredited orgzly report). These are the
  scoring artifacts behind the pilot's prerequisites.
- The weak witnesses gave no unearned credit on this board, only because every affected
  clean arm stayed unscored.

## 12. Go / no-go

**GO for expanding the corpus to ~200 cases.** Every acceptance criterion passes on the
evidence above.
- The mix lands on the plan's targets, allowing for its own 87% arithmetic.
- The derivation gate is stable (0 flips in 86 checks).
- The guessers still earn nothing under a control that did not move.
- The rotation leak that contaminated the pilot is found, explained, fixed on both staging
  paths, and shown absent on a real, paid board.

On that board, one agent completed 91.2% of scored cases and caught 36 of 43 seeded
defects, with no false alarm on any of 41 clean runs. Every miss traces to a named cause:
three truncations, one invisible seeded defect, two scoring artifacts and one real miss.
None of them is a failure of the harness to stage, run or verify an episode. The dead
hierarchy dump does sit under several of them. Every truncated episode spent more steps
on failed dumps than it overran by. The orgzly report went uncredited because nothing
the agent saw could be grounded.

This is a GO for **expansion**, not for publication. Fix these before or while the corpus
grows, because each one scales with it:

1. The agent's dead hierarchy dump (section 10), before the next paid board. It decides
   whether 14 of this board's arms can be scored and whether a report can be grounded.
2. `cal-complete-task`: re-author it so the seeded failure is visible, and add the derive
   check that would have caught it (section 10).
3. QUA-2740, the three weak witnesses, before any agent that reads screens as text is
   scored on them.
4. Budgets: re-derive at n≥8, after item 1, since dead dumps cost every episode steps.
   The first proposals are `contacts-favorite` 40 → 57 and
   `orgzly-complete-repeating-task` 60 → 87, and the three fossify-calendar seeded
   truncations need their transcripts read.
5. The AnkiDroid fixture on a fresh device, stopping a precondition-failed episode before
   its agent runs, and not leaving adbd rooted across episodes (section 10).
6. Author the empty `layout` and `widget-inventory` classes in the expansion.
7. The owner actions in section 9. The HF uploads block the epic's merge to main.

**Still NO-GO for publishing a cross-agent ranking.** The pilot's two scoring
prerequisites are unfixed, and this board measured both again. The dead hierarchy dump
would also have to be fixed first. And this is a one-agent, one-trial board, so it
carries no cross-agent comparison at all. Power comes from distinct cases, which is what
the expansion is for.

## Appendix: every board episode and its cost

Run `20260919-082841-63ec`. Cost is the harness's `metrics.cost_usd`:
- `reported` is the agent's own `total_cost_usd`;
- `estimated` is measured tokens × price, used for the four truncated episodes, whose
  process was killed before claude-code wrote its cumulative result event.

"unscored" is a PASS-expected arm with a screen-text outcome, on which the agent reported pass
but had no device text for the harness to check the witness against, because no
hierarchy dump worked on the device (section 8).

| # | case | arm | cost | source | steps / budget | completed | bugs found / present | false reports | start |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | anki-browse-cards | clean | $0.98 | reported | 12 / 30 | unscored | 0 / 0 | 0 | portrait |
| 2 | anki-browse-cards | seeded | $1.18 | reported | 15 / 30 | unscored | 1 / 1 | 0 | portrait |
| 3 | anki-browse-new-deck | clean | $3.36 | reported | 49 / 65 | EXCLUDED (env_failure) | 0 / 0 | 0 | portrait |
| 4 | anki-browse-new-deck | clean | $1.85 | reported | 23 / 65 | unscored | 0 / 0 | 0 | portrait |
| 5 | anki-browse-new-deck | seeded | $2.75 | reported | 34 / 65 | yes | 1 / 1 | 0 | portrait |
| 6 | anki-create-deck | clean | $1.20 | reported | 14 / 35 | yes | 0 / 0 | 0 | portrait |
| 7 | anki-create-deck | seeded | $1.59 | reported | 18 / 35 | yes | 1 / 1 | 0 | portrait |
| 8 | anki-open-card-from-browser | clean | $1.08 | reported | 14 / 35 | unscored | 0 / 0 | 0 | portrait |
| 9 | anki-open-card-from-browser | seeded | $1.71 | reported | 21 / 35 | yes | 1 / 1 | 0 | portrait |
| 10 | anki-study-first-card | clean | $1.26 | reported | 14 / 35 | yes | 0 / 0 | 0 | portrait |
| 11 | anki-study-first-card | seeded | $1.57 | reported | 18 / 35 | yes | 1 / 1 | 0 | portrait |
| 12 | cal-complete-task | clean | $1.93 | reported | 24 / 50 | yes | 0 / 0 | 0 | portrait |
| 13 | cal-complete-task | seeded | $3.10 | reported | 38 / 50 | no | 0 / 1 | 0 | portrait |
| 14 | cal-create-all-day-event | clean | $1.99 | reported | 25 / 45 | yes | 0 / 0 | 0 | portrait |
| 15 | cal-create-all-day-event | seeded | $2.51 | estimated | 46 / 45 (truncated) | no | 0 / 1 | 0 | portrait |
| 16 | cal-create-event | clean | $1.53 | reported | 19 / 40 | yes | 0 / 0 | 0 | portrait |
| 17 | cal-create-event | seeded | $1.55 | reported | 21 / 40 | yes | 1 / 1 | 0 | portrait |
| 18 | cal-create-task | clean | $1.55 | reported | 19 / 40 | yes | 0 / 0 | 0 | portrait |
| 19 | cal-create-task | seeded | $1.53 | reported | 18 / 40 | yes | 1 / 1 | 0 | portrait |
| 20 | cal-edit-event | clean | $2.35 | reported | 30 / 50 | yes | 0 / 0 | 0 | portrait |
| 21 | cal-edit-event | seeded | $1.73 | estimated | 51 / 50 (truncated) | no | 0 / 1 | 0 | portrait |
| 22 | cal-open-task-from-list | clean | $1.71 | reported | 26 / 45 | unscored | 0 / 0 | 0 | portrait |
| 23 | cal-open-task-from-list | seeded | $2.40 | estimated | 46 / 45 (truncated) | no | 0 / 1 | 0 | portrait |
| 24 | cal-repeat-survives-rotation | clean | $2.50 | reported | 30 / 50 | yes | 0 / 0 | 0 | portrait |
| 25 | cal-repeat-survives-rotation | seeded | $2.44 | reported | 31 / 50 | yes | 1 / 1 | 0 | portrait |
| 26 | cal-search-event | clean | $1.60 | reported | 20 / 50 | yes | 0 / 0 | 0 | portrait |
| 27 | cal-search-event | seeded | $2.55 | reported | 34 / 50 | yes | 1 / 1 | 0 | portrait |
| 28 | cal-switch-back-to-list | clean | $1.89 | reported | 24 / 50 | unscored | 0 / 0 | 0 | portrait |
| 29 | cal-switch-back-to-list | seeded | $3.06 | reported | 36 / 50 | yes | 1 / 1 | 0 | portrait |
| 30 | contacts-create | clean | $1.08 | reported | 14 / 35 | yes | 0 / 0 | 0 | portrait |
| 31 | contacts-create | seeded | $1.03 | reported | 12 / 35 | yes | 1 / 1 | 0 | portrait |
| 32 | contacts-create-group | clean | $1.38 | reported | 17 / 40 | yes | 0 / 0 | 0 | portrait |
| 33 | contacts-create-group | seeded | $1.99 | reported | 24 / 40 | yes | 1 / 1 | 0 | portrait |
| 34 | contacts-delete | clean | $1.73 | reported | 23 / 40 | yes | 0 / 0 | 0 | portrait |
| 35 | contacts-delete | seeded | $2.15 | reported | 29 / 40 | yes | 1 / 1 | 0 | portrait |
| 36 | contacts-favorite | clean | $1.84 | estimated | 42 / 40 (truncated) | no | 0 / 0 | 0 | portrait |
| 37 | contacts-favorite | seeded | $2.94 | reported | 38 / 40 | yes | 1 / 1 | 0 | portrait |
| 38 | contacts-new-contact-survives-rotation | clean | $1.36 | reported | 19 / 45 | yes | 0 / 0 | 0 | portrait |
| 39 | contacts-new-contact-survives-rotation | seeded | $2.15 | reported | 33 / 45 | yes | 1 / 1 | 1 | portrait |
| 40 | contacts-phone | clean | $1.37 | reported | 18 / 40 | yes | 0 / 0 | 0 | portrait |
| 41 | contacts-phone | seeded | $1.54 | reported | 21 / 40 | yes | 1 / 1 | 0 | portrait |
| 42 | contacts-view-details | clean | $1.32 | reported | 16 / 40 | yes | 0 / 0 | 0 | portrait |
| 43 | contacts-view-details | seeded | $1.87 | reported | 24 / 40 | yes | 1 / 1 | 0 | portrait |
| 44 | medtimer-add-medicine | clean | $1.31 | reported | 17 / 40 | yes | 0 / 0 | 0 | portrait |
| 45 | medtimer-add-medicine | seeded | $2.54 | reported | 30 / 40 | yes | 1 / 1 | 0 | portrait |
| 46 | medtimer-add-medicine-back-to-list | clean | $1.35 | reported | 16 / 45 | yes | 0 / 0 | 0 | portrait |
| 47 | medtimer-add-medicine-back-to-list | seeded | $2.31 | reported | 29 / 45 | yes | 1 / 1 | 0 | portrait |
| 48 | medtimer-add-reminder | clean | $1.73 | reported | 21 / 50 | yes | 0 / 0 | 0 | portrait |
| 49 | medtimer-add-reminder | seeded | $3.07 | reported | 34 / 50 | yes | 1 / 1 | 0 | portrait |
| 50 | medtimer-analysis-tabular-view | clean | $1.01 | reported | 12 / 40 | unscored | 0 / 0 | 0 | portrait |
| 51 | medtimer-analysis-tabular-view | seeded | $2.15 | reported | 27 / 40 | yes | 1 / 1 | 0 | portrait |
| 52 | medtimer-check-stock | clean | $1.14 | reported | 13 / 40 | unscored | 0 / 0 | 0 | portrait |
| 53 | medtimer-check-stock | seeded | $2.13 | reported | 33 / 40 | yes | 1 / 1 | 0 | portrait |
| 54 | medtimer-correct-dose-amount | clean | $1.23 | reported | 14 / 45 | yes | 0 / 0 | 0 | portrait |
| 55 | medtimer-correct-dose-amount | seeded | $1.90 | reported | 23 / 45 | yes | 1 / 1 | 0 | portrait |
| 56 | medtimer-review-aspirin | clean | $1.21 | reported | 15 / 40 | unscored | 0 / 0 | 0 | portrait |
| 57 | medtimer-review-aspirin | seeded | $1.89 | reported | 23 / 40 | unscored | 2 / 2 | 0 | portrait |
| 58 | medtimer-take-dose-then-medicine-list | clean | $1.09 | reported | 11 / 45 | yes | 0 / 0 | 0 | portrait |
| 59 | medtimer-take-dose-then-medicine-list | seeded | $1.97 | reported | 23 / 45 | yes | 1 / 1 | 1 | portrait |
| 60 | orgzly-complete-repeating-task | clean | $4.45 | reported | 58 / 60 | yes | 0 / 0 | 0 | portrait |
| 61 | orgzly-complete-repeating-task | seeded | $3.50 | reported | 44 / 60 | yes | 1 / 1 | 0 | portrait |
| 62 | orgzly-create-and-search | clean | $1.59 | reported | 20 / 45 | unscored | 0 / 0 | 0 | portrait |
| 63 | orgzly-create-and-search | seeded | $2.29 | reported | 29 / 45 | unscored | 0 / 1 | 0 | portrait |
| 64 | orgzly-create-priority-note | clean | $2.19 | reported | 32 / 60 | yes | 0 / 0 | 0 | portrait |
| 65 | orgzly-create-priority-note | seeded | $2.75 | reported | 39 / 60 | yes | 1 / 1 | 0 | portrait |
| 66 | orgzly-nest-notes-deeper | clean | $2.26 | reported | 28 / 60 | yes | 0 / 0 | 0 | portrait |
| 67 | orgzly-nest-notes-deeper | seeded | $3.47 | reported | 40 / 60 | yes | 1 / 1 | 0 | portrait |
| 68 | orgzly-new-note-survives-rotation | clean | $1.33 | reported | 19 / 40 | unscored | 0 / 0 | 0 | portrait |
| 69 | orgzly-new-note-survives-rotation | seeded | $1.38 | reported | 16 / 40 | no | 0 / 1 | 1 | portrait |
| 70 | orgzly-open-note-from-notebook | clean | $0.83 | reported | 9 / 30 | unscored | 0 / 0 | 0 | portrait |
| 71 | orgzly-open-note-from-notebook | seeded | $1.32 | reported | 18 / 30 | yes | 1 / 1 | 0 | portrait |
| 72 | tasks-add-subtask | clean | $1.17 | reported | 14 / 45 | yes | 0 / 0 | 0 | portrait |
| 73 | tasks-add-subtask | seeded | $2.21 | reported | 27 / 45 | yes | 1 / 1 | 0 | portrait |
| 74 | tasks-change-due-time | clean | $1.98 | reported | 25 / 45 | yes | 0 / 0 | 0 | portrait |
| 75 | tasks-change-due-time | seeded | $1.80 | reported | 23 / 45 | yes | 1 / 1 | 0 | portrait |
| 76 | tasks-complete-and-rename | clean | $1.94 | reported | 25 / 50 | yes | 0 / 0 | 0 | portrait |
| 77 | tasks-complete-and-rename | seeded | $2.94 | reported | 32 / 50 | yes | 1 / 1 | 0 | portrait |
| 78 | tasks-complete-parent | clean | $0.93 | reported | 11 / 40 | yes | 0 / 0 | 0 | portrait |
| 79 | tasks-complete-parent | seeded | $1.21 | reported | 15 / 40 | yes | 1 / 2 | 0 | portrait |
| 80 | tasks-complete-repeating | clean | $1.45 | reported | 18 / 40 | yes | 0 / 0 | 0 | portrait |
| 81 | tasks-complete-repeating | seeded | $1.97 | reported | 23 / 40 | yes | 1 / 1 | 0 | portrait |
| 82 | tasks-create-with-due-date | clean | $1.54 | reported | 19 / 40 | yes | 0 / 0 | 0 | portrait |
| 83 | tasks-create-with-due-date | seeded | $1.96 | reported | 24 / 40 | yes | 1 / 1 | 0 | portrait |

**Total: $156.70** = $148.22 `reported` (79 episodes) + $8.48 `estimated` (4 episodes); 0 unmeasured. The excluded attempt (row 3, $3.36) is included: it was paid for.
