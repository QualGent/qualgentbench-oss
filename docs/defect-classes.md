# Defect classes, the class mix, and the persistence retain list

Every entry under `defects:` in `src/qualgentbench/data/test-cases/<app>.yaml` carries a
`class:`, which says what kind of fault it is. The value comes from a closed vocabulary of
ten. `scripts/lint_journey_cases.py` fails on a missing or unknown class, and
`scripts/mix_report.py` counts the corpus by class against the plan's targets.

The class is corpus metadata. `journey.load_defects` is the only path from a test-case file
to the matcher, the scorer and the adversary check, and it does not copy the key. A
reclassification therefore cannot move a score. It does move `corpus_version`, as any byte
of a case file does. `kind:` is a separate field: it says how a case scores the defect
(`functional` blocks the case, `display` is a side bug with a screen marker). The two are
independent, so a persistence fault can be scored as a display side bug.

Written for QUA-2724 (epic QUA-2723). Sections 1 to 3 are the standing rules. Sections 4 to
9 record the 2026-09-17 baseline and the retain decision, which the six app children
QUA-2725 to QUA-2730 carry out.

## 1. The vocabulary

| class | a defect is this class when | plan bucket (target) | mix bucket (target) |
|---|---|---|---|
| `crash` | the app's process dies on a reachable path; the case gates the death with `crash:` | Crash on a reachable path (24%) | crash (24%) |
| `anr` | the main thread blocks while input is pending, so Android raises the ANR itself (`anr:` gate) | Performance, ANR, freeze (3%) | ANR/freeze (3%) |
| `stuck` | the main thread blocks with nothing pending, so no ANR is raised and only the `stuck:` probe sees it | Performance, ANR, freeze | ANR/freeze |
| `navigation` | the live app lands on a screen other than the one asked for; the oracle reads the destination | Wrong control flow or navigation (11%) | navigation (11%) |
| `lifecycle` | the fault fires only across a configuration change or process death: the route's `rotate` / `relaunch` step | Lifecycle and state loss (7%) | lifecycle (7%) |
| `ordering` | the seeded operator changes an order (run inline vs post, swapped awaits, commit before write) | Concurrency and async ordering (4%) | ordering (4%) |
| `persistence` | what the user did is not what was stored (a write dropped or wrong, or a command silently ignored), with the app alive and on the right screen | Silent persistence plus command ignored (14% = 6 + 8) | persistence (14%) |
| `layout` | stored state and text are right; the rendering (position, size, clipping, overlap) is wrong | Layout and rendering (9%) | display/content (24%) |
| `widget-inventory` | a control or an output element is missing, or an extra one appears | Wrong widget inventory (8%) | display/content |
| `content-format` | stored state is right; the screen states it wrongly (text, value, count, label) | Content and format correctness (7%) | display/content |

QUA-2723 balances the corpus against these seven mix buckets. Their targets sum to 87%
because three of the plan's twelve buckets have no class in this vocabulary: stale or
unsynchronised display (6%), compatibility and configuration (4%) and dead control (3%). A
corpus drawn only from these classes therefore runs a little over target in every bucket.
That is arithmetic and not a gap to chase.

## 2. Deciding an ambiguous defect

Apply these two passes in order.

1. **The trigger decides `ordering` and `lifecycle`.** Both classes are defined by how the
   fault is reached, and CLAUDE.md requires both to be observed through another class's
   oracle. A forced interleaving is authored so that Android's own check turns it into a
   deterministic crash with a stable signature. A lifecycle case puts one `rotate` or
   `relaunch` step between producing the state and a state oracle that reads it. If classes
   were assigned by what the oracle observes, every ordering defect would be `crash`, every
   lifecycle defect would be `persistence`, and both classes would be empty by construction.
2. **Otherwise the oracle decides, not the title.** Read what the case's oracle finds wrong.
   For a display side bug, read the screen string that `derive_journey.py` measured. Check in
   this order:
   - The app died or froze: `crash`, `anr` or `stuck`, by the gate.
   - The live app is on the wrong screen: `navigation`.
   - Stored state is wrong: `persistence`.
   - Stored state is right and the screen misstates it: `content-format`, `layout` or
     `widget-inventory`.

   The stored-versus-rendered check decides display side bugs. A screen that faithfully draws
   a wrong stored value is a persistence fault. A screen that misdraws a correct value is a
   display fault: `priority-letter-shifted` stores A and draws `#B`.

A defect that no journey case seeds has no journey oracle. Its class is read from its hunt
feature's `check:` in `data/benchmarks/<app>.yaml`.

Wherever the rule was needed, the reasoning is written next to the `class:` line in the YAML.
Read literally, the oracle would move three defects across the epic's baseline table. Each is
kept where the table counts it and flagged here, not changed silently:

| defect | recorded | literal oracle reading | why the recorded class |
|---|---|---|---|
| `search-results-off-main-thread` (fossify-calendar) | `ordering` | `crash` (`crash: CalledFromWrongThreadException`) | pass 1: the operator runs a view update inline instead of posting it. The crash is how CLAUDE.md says an ordering defect should be observed |
| `repeat-lost-on-rotation` (fossify-calendar) | `lifecycle` | `persistence` (the oracle reads `repeat_interval`) | pass 1: the value is lost only across the route's `rotate` |
| `repeater-done-loses-recurrence` (orgzly) | `persistence` | `content-format` (`kind: display`, measured by the marker `DONE  Water the plants`) | pass 2: the patch stores the note DONE with its old date (`DataRepository`), and the hunt oracle reads that row. The list row draws the stored state faithfully |

The rule settles four more cases where the title points one way and the oracle another. None
of them crosses the table:

- `view-switch-stuck-on-year` is `navigation`, not `stuck`. Nothing freezes; the oracle reads
  the destination screen.
- `note-added-to-default-deck` is `persistence`, not `navigation`. The oracle reads the deck
  the note was stored in.
- `task-completion-not-persisted` is `persistence`, not `lifecycle`. Its title says "survive a
  relaunch", but its hunt oracle reads the completion row straight after the tap, with no
  relaunch.
- `done-ignored-with-deadline` is `persistence`, not the plan's dead-control bucket. The Done
  action is accepted and then silently not applied, and "command ignored" is half of the
  plan's persistence bucket.

## 3. Where the buckets and targets come from

They come from the journey-mode research plan, *Making the Benchmark Predictive* (revision 2,
11 September 2026, not in this repository). The source is the defect-class table in its
section 2 and its Appendix A, which traces each target to published Android defect
distributions, chiefly Xiong et al., *An Empirical Study of Functional Bugs in Android Apps*
(ISSTA 2023).

The vocabulary takes nine of the plan's twelve buckets and splits ANR/freeze by detector into
`anr` and `stuck`. The plan's layout, widget-inventory and content-format buckets are grouped
into the single display/content bucket that the epic targets. The persistence bucket is the
plan's 6% for silent persistence plus its 8% for a command or setting silently ignored.

The corpus already quotes the same bucket names and targets where the first exemplars were
seeded. `data/benchmarks/fossify-calendar.yaml` says "Wrong control flow or navigation" was
an 11% plan target and "Lifecycle and state loss" was a 7% plan target.
`data/test-cases/medtimer.yaml` names the "Performance, ANR, freeze" bucket. `docs/design.html`
describes the hunt corpus and carries no class targets.

## 4. The baseline, 2026-09-17

This is `mix_report.py` on the corpus as of this change. Adding the `class:` lines moved
`corpus_version` from `956c309b64e2` to `85a4b429c8ad`, as any edit to a case file does. No
truth file moved.

| bucket | n | on a case | share | target | delta | epic's table |
|---|---|---|---|---|---|---|
| crash | 1 | 1 | 2.4% | 24% | -21.6 | 1 (2.5%) |
| display/content | 11 | 11 | 26.8% | 24% | +2.8 | 11 (27.5%) |
| persistence | **24** | 19 | 58.5% | 14% | +44.5 | **23** (57.5%) |
| navigation | 1 | 1 | 2.4% | 11% | -8.6 | 1 (2.5%) |
| lifecycle | 1 | 1 | 2.4% | 7% | -4.6 | 1 (2.5%) |
| ordering | 1 | 1 | 2.4% | 4% | -1.6 | 1 (2.5%) |
| ANR/freeze (anr 1, stuck 1) | 2 | 2 | 4.9% | 3% | +1.9 | 2 (5.0%) |
| **total** | **41** | 36 | | 87% | | **40** |

**The epic's table is one defect short.** Every bucket agrees except persistence, which is
missing ankidroid's `note-back-field-dropped`. That defect is persistence by its oracle:
`anki-add-note` reads `notes.flds like 'quatro%four'`, and the seeded build stores the note
without its back field. QUA-2725 lists only two ankidroid persistence defects. The corpus is
therefore 41 defects with 24 persistence, and each bucket's share moves by up to one point
from the table's.

**Five declared defects are on no case.** No case's `bugs:` has ever named these five, from
the first journey commit onward:

- fossify-calendar: `repeat-dropped`, `task-completion-not-persisted`
- fossify-contacts: `group-membership-dropped`
- medtimer: `stock-decrement-ignores-amount`
- tasksorg: `repeat-not-rescheduled`

Journey mode never switches them on, so no board has ever measured them. All five are
persistence. Counted by what a board can measure, persistence is 19 of 36.

Of the 36 cases, 17 carry only persistence defects and 2 mix persistence with a display defect
(`tasks-complete-parent` and `tasks-change-due-time`). The epic's figure of 16 is the same
count without `anki-add-note`.

### Every defect's class

Where the table says "hunt", no journey case seeds the defect and its class is read from the
named hunt feature. Every other row is read from the journey case named.

| app | defect | kind | class | read from |
|---|---|---|---|---|
| ankidroid | `note-back-field-dropped` | functional | persistence | anki-add-note: db `notes.flds like 'quatro%four'` |
| ankidroid | `note-tags-dropped` | functional | persistence | anki-add-tagged-note: db `notes.tags like '%verbs%'` |
| ankidroid | `note-added-to-default-deck` | functional | persistence | anki-add-note-to-deck: db cards in the Spanish deck |
| ankidroid | `deck-new-count-low` | display | content-format | anki-create-deck: marker `2` (the new count one short) |
| ankidroid | `browser-count-low` | display | content-format | anki-browse-cards: marker `2 cards shown` |
| fossify-calendar | `event-delete-broken` | functional | persistence | cal-delete-event: db count of `Lunch` is 0 |
| fossify-calendar | `task-reminder-dropped` | functional | persistence | cal-task-reminder: db `reminder_1_minutes` is 10 |
| fossify-calendar | `edit-event-not-saved` | functional | persistence | cal-edit-event: db `Final` present, `Draft` gone |
| fossify-calendar | `repeat-dropped` | functional | persistence | hunt `repeat_event`: db `repeat_interval` of the new event |
| fossify-calendar | `task-completion-not-persisted` | functional | persistence | hunt `complete_task`: db completion row, no relaunch |
| fossify-calendar | `event-editor-title-typo` | display | content-format | cal-create-event: marker `New Evnet` |
| fossify-calendar | `task-editor-title-typo` | display | content-format | cal-create-task: marker `New Tsak` |
| fossify-calendar | `search-results-off-main-thread` | functional | ordering | cal-search-event: `crash:` gate; see section 2 |
| fossify-calendar | `view-switch-stuck-on-year` | functional | navigation | cal-switch-back-to-list: `present: Standup` on the destination |
| fossify-calendar | `repeat-lost-on-rotation` | functional | lifecycle | cal-repeat-survives-rotation: `rotate`, then db `repeat_interval`; see section 2 |
| fossify-contacts | `contact-delete-broken` | functional | persistence | contacts-delete: content, `display_name=Alice` absent |
| fossify-contacts | `phone-number-dropped` | functional | persistence | contacts-phone: content `data1=555-1234` |
| fossify-contacts | `favorite-not-saved` | functional | persistence | contacts-favorite: content `starred=1` |
| fossify-contacts | `edit-contact-not-saved` | functional | persistence | contacts-edit: content `display_name=Alicia` |
| fossify-contacts | `group-membership-dropped` | functional | persistence | hunt `add_to_group`: content `group_membership` row |
| fossify-contacts | `contacts-tab-typo` | display | content-format | contacts-create: marker `Contatcs` |
| medtimer | `stock-decrement-ignores-amount` | functional | persistence | hunt `dose_stock`: db stock after a 4-unit dose |
| medtimer | `event-skip-not-saved` | functional | persistence | medtimer-skip-logged-dose: db status `SKIPPED` |
| medtimer | `reminder-amount-edit-lost` | functional | persistence | medtimer-edit-reminder-dosage: db `Reminder.amount` is 3 |
| medtimer | `medicine-rename-lost` | functional | persistence | medtimer-rename-medicine: db `Naproxen` present, `Ibuprofen` gone |
| medtimer | `stock-left-display-low` | display | content-format | medtimer-add-medicine, medtimer-review-aspirin: marker `9 left` |
| medtimer | `reminder-time-display-shifted` | display | content-format | medtimer-review-aspirin: marker `9:00 AM` |
| medtimer | `medicine-list-empty-reminders-crash` | functional | crash | medtimer-add-medicine-back-to-list: `crash: NoSuchElementException` |
| medtimer | `overview-action-blocks-main-thread` | functional | anr | medtimer-take-dose-then-medicine-list: `anr: true` |
| medtimer | `analysis-table-freezes-on-open` | functional | stuck | medtimer-analysis-tabular-view: standalone `stuck: Tabular view` |
| orgzly | `repeater-done-loses-recurrence` | display | persistence | orgzly-complete-repeating-task: marker `DONE  Water the plants` draws the stored state; see section 2 |
| orgzly | `done-ignored-with-deadline` | functional | persistence | orgzly-complete-deadline-task: db `state` is DONE |
| orgzly | `tags-dropped-on-edit` | functional | persistence | orgzly-add-tag: db `tags like '%work%'` |
| orgzly | `priority-letter-shifted` | display | content-format | orgzly-create-priority-note: marker `#B` |
| orgzly | `notebook-count-off-by-one` | display | content-format | orgzly-create-and-search: marker `Contains 32 notes` |
| tasksorg | `repeat-not-rescheduled` | functional | persistence | hunt `repeating_done`: db the completed repeating task still open |
| tasksorg | `subtasks-left-open` | functional | persistence | tasks-complete-parent: db parent and both subtasks completed |
| tasksorg | `due-date-edit-lost` | functional | persistence | tasks-change-due-time: db due at 09 today |
| tasksorg | `task-delete-broken` | functional | persistence | tasks-delete: db count of `Call dentist` is 0 |
| tasksorg | `subtask-chip-low` | display | content-format | tasks-complete-parent, tasks-complete-and-rename: marker `1` |
| tasksorg | `due-section-shifted` | display | content-format | tasks-create-with-due-date, tasks-change-due-time: marker `Due tomorrow` |

All 11 display defects are `content-format`: three typos, five counts one short and three
shifted values. The plan says the same: "our current corpus already sits entirely inside it."
No defect is `layout` or `widget-inventory` yet.

## 5. Persistence families

| family | members (app, journey case) | n | on a case |
|---|---|---|---|
| an edit did not save | `edit-event-not-saved` (fossify-calendar, cal-edit-event); `edit-contact-not-saved` (fossify-contacts, contacts-edit); `group-membership-dropped` (fossify-contacts, none); `reminder-amount-edit-lost` (medtimer, medtimer-edit-reminder-dosage); `medicine-rename-lost` (medtimer, medtimer-rename-medicine); `tags-dropped-on-edit` (orgzly, orgzly-add-tag); `due-date-edit-lost` (tasksorg, tasks-change-due-time) | 7 | 6 |
| field dropped on create | `note-back-field-dropped` (ankidroid, anki-add-note); `note-tags-dropped` (ankidroid, anki-add-tagged-note); `note-added-to-default-deck` (ankidroid, anki-add-note-to-deck); `task-reminder-dropped` (fossify-calendar, cal-task-reminder); `repeat-dropped` (fossify-calendar, none); `phone-number-dropped` (fossify-contacts, contacts-phone) | 6 | 5 |
| completion / skip not persisted | `task-completion-not-persisted` (fossify-calendar, none); `event-skip-not-saved` (medtimer, medtimer-skip-logged-dose); `done-ignored-with-deadline` (orgzly, orgzly-complete-deadline-task); `subtasks-left-open` (tasksorg, tasks-complete-parent) | 4 | 3 |
| delete broken | `event-delete-broken` (fossify-calendar, cal-delete-event); `contact-delete-broken` (fossify-contacts, contacts-delete); `task-delete-broken` (tasksorg, tasks-delete) | 3 | 3 |
| recurrence (completing a repeating item) | `repeater-done-loses-recurrence` (orgzly, orgzly-complete-repeating-task, as a side bug); `repeat-not-rescheduled` (tasksorg, none) | 2 | 1 |
| other: favourite, stock | `favorite-not-saved` (fossify-contacts, contacts-favorite); `stock-decrement-ignores-amount` (medtimer, none) | 2 | 1 |
| **total** | | **24** | **19** |

These are the epic's six families at the epic's sizes, with one exception. "Field dropped on
create" has 6 members, not 5, because it holds `note-back-field-dropped`, the defect the
epic's table missed. Two placements follow the family sizes the epic gave:

- `group-membership-dropped` adds an existing contact to a group, so it is an edit.
- `repeat-dropped` is the new event's repetition field not being stored, so it is a field
  dropped on create. The recurrence family covers completing a repeating item.

## 6. The retain list

One persistence defect per family, six in total. Every retained defect is on a case whose
truth row agrees. Five fail on the seeded arm and pass on the clean arm.
`orgzly-complete-repeating-task` passes on both arms, and its marker is measured.

| family | retained | app | its case | why this one |
|---|---|---|---|---|
| an edit did not save | `edit-event-not-saved` | fossify-calendar | cal-edit-event | as named in QUA-2726; the rename is checked both ways (`Final` present, `Draft` gone) |
| field dropped on create | `phone-number-dropped` | fossify-contacts | contacts-phone | as named in QUA-2727 |
| completion / skip not persisted | `subtasks-left-open` | tasksorg | tasks-complete-parent | as named in QUA-2730 |
| delete broken | `contact-delete-broken` | fossify-contacts | contacts-delete | as named in QUA-2727 |
| recurrence | `repeater-done-loses-recurrence` | orgzly | orgzly-complete-repeating-task | as named in QUA-2729; the family's only member on a case |
| other: favourite, stock | **`favorite-not-saved`** | fossify-contacts | contacts-favorite | **changed from `stock-decrement-ignores-amount`**: the family's only member on a case (section 9) |

## 7. What the prune costs

This is an accepted trade, not an oversight. At this corpus size, a seventh persistence
defect is worth far less than a first crash defect. Do not quietly re-add the pruned
variants. Measuring generalisation belongs to the power-expansion epic (about 200 cases).

- **Generalisation.** The "an edit did not save" variants were the only thing measuring
  whether persistence detection generalises across UIs: 7 declared, 6 of them on a case,
  across 5 apps. One remains after the prune, so this corpus can no longer measure
  generalisation. The same loss applies, more weakly, to the families "field dropped on
  create" (5 seeded variants across 3 apps), "delete broken" (3 across 3) and
  "completion / skip" (3 across 3).
- **Recurrence can never block a case.** Its retained defect is scored as a display side bug.
  Its case's completion oracle holds on both arms, so the family is credited only through its
  marker and symptom words.
- **Uneven spread across apps.** Two apps keep no persistence defect (ankidroid and medtimer),
  and fossify-contacts keeps three.

## 8. The prune, per app

QUA-2725 to QUA-2730 carry out this table. 18 persistence defects are pruned and 6 retained.

| app (ticket) | prune | cases removed with them | persistence kept |
|---|---|---|---|
| ankidroid (QUA-2725) | `note-back-field-dropped`, `note-tags-dropped`, `note-added-to-default-deck` | anki-add-note, anki-add-tagged-note, anki-add-note-to-deck | none |
| fossify-calendar (QUA-2726) | `event-delete-broken`, `task-reminder-dropped`, `repeat-dropped`, `task-completion-not-persisted` | cal-delete-event, cal-task-reminder (the other two are on no case) | `edit-event-not-saved` |
| fossify-contacts (QUA-2727) | `edit-contact-not-saved`, `group-membership-dropped` | contacts-edit (`group-membership-dropped` is on no case) | `contact-delete-broken`, `phone-number-dropped`, `favorite-not-saved` |
| medtimer (QUA-2728) | `stock-decrement-ignores-amount`, `event-skip-not-saved`, `reminder-amount-edit-lost`, `medicine-rename-lost` | medtimer-skip-logged-dose, medtimer-edit-reminder-dosage, medtimer-rename-medicine (`stock-decrement-ignores-amount` is on no case) | none |
| orgzly (QUA-2729) | `done-ignored-with-deadline`, `tags-dropped-on-edit` | orgzly-complete-deadline-task, orgzly-add-tag | `repeater-done-loses-recurrence` |
| tasksorg (QUA-2730) | `repeat-not-rescheduled`, `due-date-edit-lost`, `task-delete-broken` | tasks-delete. **tasks-change-due-time shrinks** to `[due-section-shifted]` and stays; its seeded arm is then expected to PASS (`repeat-not-rescheduled` is on no case) | `subtasks-left-open` |
| **total** | **18** | **12 removed, 1 shrinks** | **6** |

No removed case carries a display defect or a new-class defect, so the prune leaves no other
class without a case. Every display defect keeps at least one case.

To carry out the prune:

1. **Journey side only.** Remove the `defects:` entry, and every case it leaves with no
   defect, from `data/test-cases/<app>.yaml`. A removed case's truth row disappears when the
   app is re-derived.
2. **Keep the patch.** Leave the defect's `bugs:` patch and its exploration feature in
   `data/benchmarks/<app>.yaml`. Each of the 18 is also a hard-tier hunt defect: a
   `state: broken` feature with a derived `broken` row in `truth/hard-stability.json`. The
   app tickets say to remove pruned `patch:` blocks. Doing that would leave a hunt feature
   declared broken with no fault behind it, and the committed hard-tier truth would no longer
   describe the build. A locally rebuilt `dist/<app>/buggy.apk` wins in hunt mode too. It
   would also shrink the hunt corpus, which is outside this epic. With the patch kept, the
   journey build still carries it and no case switches it on, exactly as for the five
   declared-only defects today.
3. **Re-point the tests** that read a removed case out of the real corpus:
   - ankidroid: in `tests/test_case_filter.py`, `OTHER_CASE = "anki-add-note"`. In
     `tests/test_journey.py`, `test_derived_blocking_texts_drop_what_cannot_be_evidence` and
     the `anki-add-note-to-deck~seeded` row of `test_short_and_off_target_reports_earn_no_credit`.
   - fossify-calendar: in `tests/test_journey.py`,
     `test_symptom_vocabulary_is_read_off_the_claim_not_off_the_quotes` and the
     `cal-delete-event~seeded` row of
     `test_a_brief_noun_earns_the_bug_only_once_the_device_has_said_it`.
   - medtimer: in `tests/test_journey.py`,
     `test_every_case_has_a_clean_version_and_seeded_only_with_bugs` and
     `test_brief_is_identical_across_versions_and_names_no_bug`. In
     `tests/test_episode_precondition.py`, `test_the_anchor_comes_from_the_cases_first_tap`
     and `_spec`.
   - orgzly: in `tests/test_journey.py`,
     `test_derived_blocking_texts_drop_what_cannot_be_evidence`.
   - tasksorg: in `tests/test_journey.py`,
     `test_derived_blocking_texts_drop_what_cannot_be_evidence`,
     `test_grounding_is_what_the_device_answered_not_what_the_agent_typed`,
     `test_real_markers_and_texts_still_match_on_token_boundaries` and the
     `tasks-delete~seeded` row of
     `test_a_brief_noun_earns_the_bug_only_once_the_device_has_said_it`.

   Some prose also named removed cases: rows in `docs/journey-oracle-audit.md`, CLAUDE.md's
   `anki-add-tagged-note` budget example and `medtimer-skip-logged-dose` fixture history,
   and the `--case` help example in `cli.py`. QUA-2739 marked the audit rows as pruned,
   dated the budget example, and pointed the `--case` and `--repeat` help at live cases,
   because users copy help text. The fixture history stays, since it is history.
4. **New defects.** Every defect an app child adds carries a `class:`, and none may be
   `persistence`.

## 9. Where this list disagrees with the app tickets

Per the epic, this list wins where they differ. Each difference is named here.

1. **ankidroid, `note-back-field-dropped` (QUA-2725 and the epic's table).** It is
   persistence by its oracle but missing from the epic's count and from QUA-2725's list. One
   defect is kept per family, and the field-dropped retain is `phone-number-dropped`, so this
   defect is **pruned**. QUA-2725 prunes 3, not 2, and `anki-add-note` goes with it.
2. **medtimer, `stock-decrement-ignores-amount` (QUA-2728): pruned, not retained.** No journey
   case has ever seeded it. Retaining it would leave the "other" family with no measured
   coverage, while `mix_report.py` and QUA-2731's check "exactly 6 persistence defects
   remain" would still count it. QUA-2728 prunes 4, not 3, and retains nothing. To keep the
   stock mechanism instead, QUA-2728 would first have to author and derive a case that seeds
   it. That is new persistence-case authoring, which this epic otherwise avoids. Only then
   could it replace `favorite-not-saved`.
3. **fossify-contacts, `favorite-not-saved` (QUA-2727): retained, not pruned.** It is the
   "other" family's only member on a case (`contacts-favorite`, derived FAIL seeded, PASS
   clean). QUA-2727 prunes 2, not 3.
4. **All six app tickets: "remove pruned `patch:` blocks".** Do not remove them. Every pruned
   defect is also a hard-tier hunt defect (section 8, step 2).
5. **The epic and QUA-2731: "17 pruned, 6 retained".** The real figure is 18 pruned and 6
   retained, out of 24. Because all six retained defects are on a case, "6 persistence
   defects remain" and "6 are measured" are the same claim.
6. **Three classifications** would cross the baseline table under a literal oracle reading
   (section 2). They are kept where the table counts them.

## 10. The mix after the epic

The epic prunes 18 and seeds 17: 10 crash, 4 navigation, 2 lifecycle and 1 ordering.

| bucket | now | after | share | target | delta |
|---|---|---|---|---|---|
| crash | 1 | 11 | 27.5% | 24% | +3.5 |
| display/content | 11 | 11 | 27.5% | 24% | +3.5 |
| persistence | 24 | 6 | 15.0% | 14% | +1.0 |
| navigation | 1 | 5 | 12.5% | 11% | +1.5 |
| lifecycle | 1 | 3 | 7.5% | 7% | +0.5 |
| ordering | 1 | 2 | 5.0% | 4% | +1.0 |
| ANR/freeze | 2 | 2 | 5.0% | 3% | +2.0 |
| **total** | **41** | **40** | | 87% | |

This is exactly the epic's "after" column. Per app the corpus becomes ankidroid 5,
fossify-calendar 9, fossify-contacts 7, medtimer 8, orgzly 6 and tasksorg 5 defects. Once
every new defect ships with its case, each of them is on a case, so the declared mix and the
measured mix are the same numbers.

**Addendum, 2026-09-23 (QUA-2783).** Two display defects were retired from the journey
corpus: `deck-new-count-low` (ankidroid) and `subtask-chip-low` (tasksorg). Each one's
only screen signal was a single character (`2`, `1`), below the matcher's evidence floor,
so no report could quote it. display/content drops from 11 to 9 and the total from 40 to
38. Per app, ankidroid drops from 5 to 4 and tasksorg from 5 to 4. As with the §8 prune,
both patches and hunt features stay in `data/benchmarks/`. The two cases left with no
defect (anki-create-deck, tasks-complete-and-rename) stay in the corpus as clean-only
cases. docs/journey-oracle-audit.md, "Side-bug references", has the audit, and
`tests/test_mix_report.py` pins the new counts.

## Running it

```bash
uv run python scripts/mix_report.py                             # the public corpus: whole, then per app
uv run python scripts/mix_report.py --json                      # the same, as data
uv run python scripts/mix_report.py --root "$QGB_HELDOUT_DIR"   # the held-out split, on its own
uv run python scripts/lint_journey_cases.py                     # fails on a missing or unknown class
```

The held-out split needs `class:` on its defects too. With `QGB_HELDOUT_DIR` exported,
`lint_journey_cases.py` lints the split and fails on any defect without one. The split
lives outside this repository, so a class there is an edit for whoever maintains the
split, made under the same two passes as section 2 with the reasoning beside each
`class:` line in the split's own YAML (QUA-2782 added the first 13); nothing about them
is recorded here.
