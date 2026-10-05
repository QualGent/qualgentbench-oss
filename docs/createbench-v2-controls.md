# CreateBench v2: control eligibility on authored routes (QUA-2867)

Design note. Follow-up to QUA-2854 (control derivation) and QUA-2866 (grader v4).

## Problem

`scripts/derive_create_controls.py` calls a control eligible when the case's REFERENCE
route still holds with it on (3/3 replays). An authored case takes its own route. In the
QUA-2861 rerun, tasks-complete-parent's authored cases created a parent with subtasks, so
they walked into `subtask-filed-before-written`, the case's rank-2 control. It is a
same-screen ordering crash. The control fired, the app died, and the runner wrote FAIL. On
the reference route it never fires, because that route completes an existing task.

Control reach in that rerun (40 grades, read-only offline rescore under v4,
`grade.runs[control].control_fired`):

| brief | fired / read | off-reference fired / read | excluded (v4) |
|---|---|---|---|
| tasks-complete-parent | 6/8 | 2/4 (`subtask-filed-before-written`, both arms) | 2/8 |
| tasks-add-subtask | 2/2 | 0/0 | 0/2 |
| cal-edit-event, contacts-delete, contacts-phone | 2/8 each | 0/6 each | 0 |
| medtimer-take-dose-then-medicine-list | 1/2 | 0/0 | 0 |
| anki-study-first-card, medtimer-analysis-tabular-view | 0/2 | 0/2 | 0 |

Every firing except the two tasks-complete-parent ones came from a `side` control, which
fires on the reference route by design. When such a control fires and the run still
PASSes, that is the strongest specificity evidence a run can give.

## What actually goes wrong

When an authored route reaches a control, the outcome depends on whether the control is
lethal:

* **Live control (the app survives).** The case either tolerates the perturbation (PASS)
  or rejects it (FAIL). Both are real specificity measurements. This is the reach we want.
* **Lethal control** (crash, ANR, stuck, or an ordering fault whose own case dies). The
  app dies. Grader v4 excludes the run (`control_reached`), so the artifact's specificity
  and Strong-Test are unscored. If the route never reaches the control, the run is just a
  fourth clean run. Either way, a lethal control measures nothing that a live one would
  not.

So the harm comes from lethality plus proximity, not from reach as such.

## Options

* **(a) Static disjointness from patch anchors.** This would not have caught the case
  above. The target `subtasks-left-open` patches `TaskCompleter.kt` and the control
  patches `TaskEditViewModel.kt`, which are disjoint files. The proximity signal that does
  flag it already exists: the derived relation (`same-screen`, screen overlap 0.82). The
  static signal that matters is lethality, and that is corpus metadata
  (`create/detection.py`: the walk/assert label of the case the defect is the target of).
* **(b) Authored-route replay.** There is no replayable authored route. An authored case
  is NL steps. The derive replays the harness's structured `check:` DSL, and only
  reference cases have one. Replaying an authored route therefore means an LLM runner per
  candidate: about $0.93 per run at QUA-2861's prices, times 3 candidates, times
  `--repeat 3`, comes to about $8.40 per artifact, plus 9 more runner runs of device time. The cost
  repeats on every artifact, because each authoring takes a different route. Rejected.
* **(c) Runtime only.** v4 already excludes the lethal reach. Not enough on its own: the
  same brief keeps drawing the same lethal-adjacent control, and an excluded run loses the
  artifact's Strong-Test.

## Chosen: (a′) rank rule 2 + (c) board reach + early stop

1. **Rank rule 2** (`derive_create_controls.rank_controls`, `RANK_RULE = 2`). Live controls
   come before lethal ones, each ordered side, same-screen, other. A lethal control that
   is `side` or `same-screen` (`reach_risk: lethal-adjacent`) becomes a RESERVE: it is
   left out of `create_controls` while any other control is eligible, and used only when
   the alternative is specificity n/a. A lethal `other` control ranks after every live
   one. Every candidate records `lethal`, `lethal_why` and `reach_risk`, and every
   derivation records `rank_rule` and `reserves`.
2. **Control reach on the board** (`create/board.control_reach`). Shown per row and per
   brief: fired/read, off-reference fired/read (relation not `side`), and v4 exclusions.
   Grades written before v4 are read from `fault_fired`. A brief is flagged HIGH when
   excluded runs are at least 25% of its control runs, or its off-reference controls fired
   on at least 25% of read runs (`CONTROL_REACH_WARN`). Flagged briefs are listed in a
   board note. On the QUA-2861 rerun, only tasks-complete-parent is flagged.
3. **Early stop** (`--stop-after N`, default 3; 0 = exhaustive). Candidates are replayed in
   a static order: live first; within live, candidates on a visited screen first, display
   defects first among those, then by overlap; lethal-near candidates last. A case stops
   once N non-reserve candidates are eligible. Candidates that were not replayed are
   listed under `early_stop.unreplayed` and never counted as ineligible.

## Measured (offline, from the committed derivation's recorded trials)

`uv run python scripts/derive_create_controls.py --report` prints these numbers.

* **Rule 2 over the same trials.** 31 of 41 cases reorder or lose a reserve. The trial-0
  control changes in 8. Over 4 trials × 41 cases (164 control draws), lethal-adjacent
  draws drop from 28 to 0 and all lethal draws drop from 53 to 27. tasks-complete-parent
  becomes `due-section-shifted → repeat-complete-crash` (reserve:
  `subtask-filed-before-written`), so the QUA-2861 reach could not recur on any trial.
* **Early stop at 3.** 418 replays against 737 for the exhaustive run, 43% fewer. At about
  49 s per replay (737 replays took about 10 h), that is roughly 5.7 h instead of 10 h.
  None of the 41 cases gets a worse control profile (reach risk and relation of the first
  3). In 8 cases the early stop picks a different defect of the same kind.

## Corpus version

The committed truth is NOT changed. Rule 2 and the early stop apply to future derivations.
`--rejudge` prints what rule 2 would move, and `--rejudge --write` applies it from the
recorded trials with no device. Applying it is an owner decision because it MOVES the
corpus version (the truth file is part of it). Boards from before and after must then not
be blended; the board already names a row that blends corpus versions.

## Limits

* The evidence is one brief (tasks-complete-parent) on one rerun. The rule is principled
  (a lethal control cannot measure specificity), but the 25% warning threshold is a
  judgment call.
* An `ordering` defect with no labelled case is treated as lethal. This is the
  conservative choice: a wrong call only demotes the control.
* A live control that an authored route reaches is kept on purpose. A FAIL on it is a
  specificity FAIL (`overfit-build`).
