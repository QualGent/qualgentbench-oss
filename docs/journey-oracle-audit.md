# Journey oracle audit (Phase 0, 2026-09-14)

Every journey test case has two halves that must promise the same thing: the **brief**
(`steps` + `expected_outcome`, what the agent is held to) and the **oracle**
(`check.expect`, what the harness verifies after the agent exits, plus `evidence:` strings
the agent's own device output must carry). When they drift, the score measures the drift,
not the agent:

- **oracle weaker than brief** — the query accepts states the brief rules out. An agent
  that holds the app to the brief and reports `fail` is charged a false alarm, because the
  harness says the outcome held.
- **oracle stronger than brief** — the query demands something the brief never asked for.
  An agent that did exactly what it was told is scored not-completed.
- **unscoreable read-only** — the case changes no state, so its only oracle is a
  `present:` string that has to come back through a parsed device-text channel;
  `journey_verdict` leaves such PASS-expected episodes unscored (the stopgap), so the case
  contributes nothing to completion on either arm.
- **leaks defect** — the brief or the witness names the seeded defect's marker or spells
  out the semantics it breaks, so finding the bug takes no testing.

This audit read every SQL query and every `present:` string against the brief's exact
words (dates, counts, names, "today", "only", "instead of"). Fixes prefer tightening the
oracle over weakening the brief. An oracle is trusted only after it has been re-derived on
a device (clean HOLDS, seeded as `bugs:` implies), so every changed `check:` was marked
with a `# TODO(derive): re-run derive_journey.py --case <id>` comment. The checks were
re-derived on 2026-09-18, and PR #50 (QUA-2738) closed those TODOs.

Gate: `uv run python scripts/lint_journey_cases.py` (device-free; registered in CLAUDE.md
next to `adversary_check.py`) fails on the leak class (a `present:`/`absent:`/`evidence:`
string that carries a seeded defect's marker, symptom phrase or measured display text; a
brief that states a marker) and on oracle-less cases. It WARNS when a brief carries a
multi-word symptom phrase of its own bug — the shape of the two semantic leaks below
(the pre-fix orgzly brief tripped it on "marked done" and "next occurrence"; the two
remaining hits, `tasks-change-due-time`'s "9 AM" and "due time", are expected values a
brief has to state) — and when a `db:`/`content:` query names none of the brief's key
nouns (`contacts-favorite` checks `starred=1`, another case checks a fixture id: both
legitimate). None of the three leaks this audit fixed was a marker-level leak;
they were found by reading, and the lint is the regression guard, not the detector.

> **Held-out split (2026-09-15).** Two apps' rows were removed from this public copy when
> they moved to the held-out split (docs/heldout.md); an audit row states a case's oracle
> and defect semantics, which is an answer key. The full audit is kept with the split. The
> counts below are over all 40 cases as audited.

## Verdicts

40 cases, one primary verdict each: **17 matches · 12 oracle weaker than brief · 0 oracle
stronger than brief · 9 unscoreable read-only · 2 leaks defect** (a third leak,
medtimer-review-aspirin, is counted under read-only). Secondary verdicts are kept in the
table: three read-only cases are also weaker than their brief, and one blocked case
already had the witness shape but was still unscored on its clean arm.

| case | brief promise | oracle checked | verdict | fix |
|---|---|---|---|---|
| anki-browse-cards | browser lists the three cards uno, dos, tres | `present: uno` | unscoreable read-only; weaker (one of three names) | screen witness `evidence: [uno, dos, tres]` |
| anki-create-deck | the deck list shows French alongside Spanish | `count(decks)` = 3 | weaker (any name passes) | decks `name = 'French'` = 1 · derive |
| cal-create-event | "Standup" saved and shown on **today's** date | title+type only, never the date | weaker (the plan's example) | `date(start_ts,…)=date('now',…)` · derive |
| cal-create-task | "Groceries" saved and shown on **today's** date | title+type only | weaker | same date clause · derive |
| cal-edit-event | calendar shows "Final" **and no longer shows "Draft"** | `count(Final)` = 1 | weaker (a copy passes) | `count(Final),count(Draft)` = `1,0` · derive |
| contacts-create | "Alice" is listed | contacts contains `display_name=Alice` | matches | — |
| contacts-phone | Alice's contact shows 555-1234 | data contains `data1=555-1234` | matches (one contact exists) | — |
| contacts-favorite | Alice listed on the Favorites tab | contacts contains `starred=1` | matches (one contact exists) | — |
| contacts-delete | Alice no longer listed | contains `display_name=Alice`, absent | matches (do-nothing caveat) | — |
| medtimer-add-medicine | "Lisinopril" listed on the Medicine tab | Medicine `medicineName='Lisinopril'` = 1 | matches | — |
| medtimer-review-aspirin | Aspirin's screen: one reminder 8:00 AM, dosage 2; stock 10 | `present: '8:00 AM'` | unscoreable read-only; **leaks defect** (step 2 pointed at both seeded display rows) | step removed; screen witness `evidence: ['8:00 AM']` (Aspirin's own screen, identical on both arms) |
| medtimer-take-dose-then-medicine-list | the dose is recorded as taken, then the Medicine tab lists its medicines | Ibuprofen `amount='4'` TAKEN = 1, gated `anr: true` | matches; the gate says HOW the seeded arm may fail, never that it must (QUA-2711) | — |
| medtimer-analysis-tabular-view | the Tabular view lists today's recorded Ibuprofen events | standalone `stuck: 'Tabular view'` + witness `['Ibuprofen']` | matches; the route writes nothing, so the liveness oracle answers "did the screen stay alive" and the witness answers "was it read" (QUA-2711) | — |
| orgzly-create-priority-note | "Book flights" at the bottom, state TODO | `state` = TODO | matches (priority deliberately not promised: its letter is the side bug) | — |
| orgzly-complete-repeating-task | **not DONE, scheduled date moved to next occurrence** | `state` = '' | **leaks defect** (org-mode repeater semantics spelled out; the L4) | defect → `display`, per-case marker `DONE  Water the plants`; brief "still listed"; oracle `count(title)` = 1 · derive |
| orgzly-create-and-search | search results list "Team meeting" | `present: Team meeting` | unscoreable read-only (writes a note); route stopped before the search was submitted — the measured final dump is the whole notebook and the match was the search box | route `+ {press: enter}`; `db:` note = 1 · derive; witness `[Team meeting]` |
| tasks-create-with-due-date | "Groceries" listed, due date **today** | `dueDate>0` | weaker (any date) | `date(dueDate/1000,…)=date('now',…)` · derive |
| tasks-complete-parent | "Pack for trip" **and** both subtasks completed | Passport+Chargers completed = 2 | weaker (parent unchecked) | three titles completed = 3 · derive |
| tasks-change-due-time | "Water plants" due **today** at 9:00 AM | hour = 09 | weaker (any day) | `+ date(...)=today` · derive |
| tasks-complete-and-rename | "Call dermatologist" **instead of** "Call dentist"; "Library books" completed | dermatologist + Library completed = 2 | weaker (a copy passes) | `1,0,1` over three counts · derive |

The table above keeps the audited cases that are still in the corpus. Epic QUA-2723
pruned 12 of them in 2026-09 (docs/defect-classes.md §8); their rows follow unchanged, as
history, and the 21 cases added after this audit have their own section below.

### Pruned since the audit (12 cases)

Each row is as audited on 2026-09-14. The ticket that pruned the case is in docs/defect-classes.md §8
(ankidroid QUA-2725, fossify-calendar QUA-2726, fossify-contacts QUA-2727, medtimer QUA-2728,
orgzly QUA-2729, tasksorg QUA-2730).

| case | brief promise | oracle checked | verdict | fix |
|---|---|---|---|---|
| anki-add-note | browser shows a card with front `quatro`, back `four` | notes `flds like 'quatro%four'` = 1 | matches | — |
| anki-add-note-to-deck | the new note is in the Spanish deck | cards in deck `Spanish` = 4 | matches | — |
| anki-add-tagged-note | the saved note carries tag `verbs` | notes `tags like '%verbs%'` = 1 | matches | — |
| cal-task-reminder | task "Pharmacy" has a reminder 10 minutes before | `reminder_1_minutes` = 10 | matches | — |
| cal-delete-event | "Lunch" no longer shown | `count(Lunch)` = 0 | matches (do-nothing caveat, below) | — |
| contacts-edit | list shows "Alicia" **and no longer "Alice"** | contacts contains `display_name=Alicia` | weaker (a copy passes) | proposed only: a `content:` expectation is one `contains` or one row count — the "Alice gone" half needs a second expectation (harness) |
| medtimer-edit-reminder-dosage | the 8:00 AM reminder shows dosage 3 | Reminder `amount='3'` = 1 | matches (Aspirin's is the only reminder) | — |
| medtimer-skip-logged-dose | the Ibuprofen **2.5** event is Skipped | any Ibuprofen event SKIPPED | weaker (skipping the "(4)" reminder passes) | `and amount='2.5'` · derive |
| medtimer-rename-medicine | lists "Naproxen" **instead of** "Ibuprofen" | `count(Naproxen)` = 1 | weaker (a new medicine passes) | `count(Naproxen),count(Ibuprofen)` = `1,0` · derive |
| orgzly-complete-deadline-task | "Renew passport" shown as DONE | `state` = DONE | matches | — |
| orgzly-add-tag | "Quarterly report" shows tag `work` | `tags like '%work%'` = 1 | matches | — |
| tasks-delete | "Call dentist" no longer in the list | `count(Call dentist)` = 0 | matches (seeded row, so real) | — |

### Needs `derive_journey.py` (15 cases)

History: every app has since been re-derived whole, and all 41 public truth rows agree
(`agrees: true`). Two of the fifteen, medtimer-skip-logged-dose and medtimer-rename-medicine,
have since been pruned.

anki-create-deck · cal-create-event · cal-create-task · cal-edit-event ·
medtimer-skip-logged-dose · medtimer-rename-medicine · orgzly-complete-repeating-task ·
orgzly-create-and-search · tasks-create-with-due-date · tasks-complete-parent ·
tasks-change-due-time · tasks-complete-and-rename, plus three cases now in the held-out
split. The two reclassified defects must come
back with the marker in the screen diff (`side[].texts` non-empty) and `agrees: true`.
Until then their truth files still record those
two cases as blocked — `journey_tasks` derives `expected` from the YAML, so the stale
truth only leaves `side[].texts` empty (the marker alone carries the match).

### Residual, not fixable in corpus text

- **Do-nothing oracles.** `cal-delete-event` and `contacts-delete` create and then delete
  the same record; a run that never created it satisfies `count = 0`. Fossify keeps no
  tombstone, so only a state-changing precondition (a seeded row) would close this;
  `tasks-delete` is already in that shape.
- **`contacts-edit`**: one `content:` expectation cannot assert both "Alicia present" and
  "Alice absent". A second expectation per case is a harness field.
- **Host timezone.** `verify.device_oracle.query_db` runs the query in the harness process
  with the HOST's zone, while the device is pinned to `QGB_DEVICE_TIMEZONE` and the
  `sql:` fixture step runs under that zone in a child process. Every `'localtime'` in an
  oracle (`tasks-change-due-time`'s hour check predates this audit; the four new date
  checks follow it) therefore assumed host zone = device zone. Fixed 2026-09-14 (`query_db` evaluates under the device's `persist.sys.timezone`): run
  `query_db` under the device zone the way `_apply_script` does.
- **Host clock.** The same gap one level down, opened by QUA-2781's clock pin: the device
  clock is now set to `QGB_DEVICE_CLOCK` on every staging path, so the host's `'now'` is a
  different day from the device's. Fixed with the pin: `apply_sql` and `query_db` replace
  every `'now'` literal with the device's UTC time (`device_oracle.at_device_now`), so the
  `'now'` date checks and the `'now'` fixtures (MedTimer's dose stamp, tasks.org's "today
  18:00") read the day the app showed the agent.
- **`due-date-edit-lost` lists the bare word `lost`** as a symptom; `test_journey.py`
  carries a strict xfail waiting for the corpus fix, so removing the word here would turn
  that xfail into a failure. Left for the owner of that test. *Resolved 2026-09-18:*
  QUA-2730 pruned `due-date-edit-lost` from the journey side and took the word with it.
  The test is now a plain one, and QUA-2739 gave it a positive control.

## Cases added after the audit (21 cases, audited 2026-09-19)

Four cases joined the corpus between this audit and epic QUA-2723: cal-search-event,
cal-switch-back-to-list, cal-repeat-survives-rotation and medtimer-add-medicine-back-to-list.
The epic added 17 more, one per new defect. They were read the same way, against the same
four failure shapes (QUA-2739). The **21 cases, one primary verdict each, are 18 matches, 3
oracle weaker than brief, 0 oracle stronger than brief, 0 unscoreable read-only and 0 leaks
defect.** All six read-only cases carry a screen witness. The three weaker ones share one
shape, described under the table — QUA-2740 fixed one of them
(`orgzly-open-note-from-notebook`), turned the shape into a derive gate, and recorded the
rest in the corpus data; the count above is left as audited.

A death case keeps an ordinary completion oracle with a `crash:` gate riding on it, as
CLAUDE.md prescribes. That oracle checks what the route WRITES before the death. Where the
brief also asks the agent to read a screen afterwards, the row notes the unchecked read-only
tail. The `medtimer-take-dose-then-medicine-list` row above has the same tail: its Medicine
tab read is unchecked too.

| case | brief promise | oracle checked | verdict | fix |
|---|---|---|---|---|
| anki-browse-new-deck | the Card browser shows the new French deck and reads `0 cards shown` | deck `French` exists with 0 cards (`1,0`), gated `crash: IndexOutOfBoundsException`; witness `['0 cards shown']` (clean arm only, steps 12-13) | matches | — |
| anki-study-first-card | card `uno` shows answer `one`, is answered Good, and the next question appears | one `revlog` row for the `uno` note with `ease = 3`, gated `crash: NullPointerException` | matches (a Good rating needs the answer shown first); read-only tail (next question) unchecked | — |
| anki-open-card-from-browser | `tres` opens in the note editor, Front `tres`, Back `three` | `present: three`; witness `['three']` (clean arm only, steps 5-6; the browser list does not show it) | matches (screen witness) | — |
| cal-search-event | `Dentist` is saved, and searching `Dent` lists it | `count(Dentist)` = 1, gated `crash: CalledFromWrongThreadException` | matches on the write; read-only tail (the search lists it) unchecked | — |
| cal-switch-back-to-list | after yearly and back, the simple event list is shown and lists `Standup` | `present: Standup`; witness `['Standup']` | **weaker**: the witness is on screen from the typing on (steps 5-6 on BOTH arms: the title field, then the saved list), so a read before the switch satisfies it | QUA-2740: none in corpus text — the returned list is the list the save already showed (see below); recorded in data as `witness_before_action: QUA-2768` |
| cal-repeat-survives-rotation | `Planning` saved as a weekly repeating event after a rotation | `repeat_interval` = 604800 for `Planning` | matches | — |
| cal-complete-task | `Taxes` is marked completed: the list shows it completed, and its screen offers to mark it incomplete | the completion row for `Taxes` exists, gated `crash: IllegalArgumentException` | matches; read-only tail unchecked | QUA-2742: the seeded death was invisible (row written, then a crash in a secondary activity; the list resumed showing it completed), so the fault now fires before the write and the route and brief open `Taxes` again, where the toggle reads `Mark incomplete` only once the completion is recorded |
| cal-create-all-day-event | `Vacation` saved as an all-day event and listed | `Vacation` with `flags & 1` = 1, gated `crash: IllegalFieldValueException` | matches (no date clause on purpose: after 23:00 the default start is tomorrow) | — |
| cal-open-task-from-list | `Laundry` opens on its own screen, with its title and a `Mark completed` button | `present: Mark completed`; witness `['Mark completed']` (clean arm only, step 7) | matches (screen witness; the title itself is not witnessed) | — |
| contacts-view-details | Alice's details screen opens and shows her name | contacts contains `display_name=Alice`, gated `crash: IndexOutOfBoundsException` | matches on the write; read-only tail (the details screen) unchecked | — |
| contacts-create-group | group `Friends` is listed on the Groups tab | groups contains `title=Friends`, gated `crash: NullPointerException` | matches | — |
| contacts-new-contact-survives-rotation | Alice, started before a rotation, is saved and listed | contacts contains `display_name=Alice` | matches | — |
| medtimer-add-medicine-back-to-list | the Medicine tab lists `Lisinopril` with Aspirin and Ibuprofen | Medicine `medicineName='Lisinopril'` = 1, gated `crash: NoSuchElementException` | matches on the write; read-only tail (the list) unchecked | — |
| medtimer-add-reminder | Ibuprofen gets a reminder at 8:00 AM, dosage 1 | Reminder for Ibuprofen with `amount='1'` and `timeInMinutes=480` = 1, gated `crash: DateTimeException` | matches | — |
| medtimer-correct-dose-amount | the logged Ibuprofen dose shows 3 **instead of** 2.5 | Ibuprofen events at `3` and at `2.5` = `1,0`, gated `crash: StringIndexOutOfBoundsException` | matches (both halves) | — (time of day: the case's `TODO(fixture)`) |
| medtimer-check-stock | Ibuprofen's stock screen opens and shows an Amount of 10 | `present: Amount`; witness `['Amount']` (clean arm only, steps 4-5) | matches (screen witness); the value `10` is not witnessed: it is on the medicine list too, so it could not prove the screen | — |
| orgzly-nest-notes-deeper | `Level six` is listed, indented under `Level five` | `Level six` at `level = 6`, gated `crash: NullPointerException` | matches (the sample outline stops at level 4, so level 6 is only reachable under the route's own level-5 note) | — |
| orgzly-open-note-from-notebook | the tapped note opens, showing its title under its notebook path | `present: Click on the note to open it`; witness the title **and** the editor's breadcrumb `Getting Started with Orgzly  •  Notes` (clean steps 3-4, never 1-2) | **fixed** (QUA-2740): was weaker — the witness was the tapped row's own title, on the notebook list at step 2 on BOTH arms, so it was read before the tap | QUA-2740: witness the breadcrumb beside the title, and the brief's last step asks for the path as well as the title · derive |
| orgzly-new-note-survives-rotation | after a rotation the editor still shows the title `Pay the rent` | `present: Pay the rent`; witness the same | **weaker**: the witness is on screen from the typing on, at step 5 on BOTH arms, so a read before the rotation satisfies it | QUA-2740: none in corpus text — the title is the one thing the brief asks to read, and the landscape screens are a subset of the portrait one; recorded in data as `witness_before_action: QUA-2768` |
| tasks-complete-repeating | `Water plants` stays open and is now due **tomorrow** | `Water plants` with `completed=0`, due date = tomorrow (device zone), gated `crash: DateTimeParseException` | matches | — |
| tasks-add-subtask | `Pack for trip` lists `Tickets` as a subtask, with Passport and Chargers | `Tickets` not deleted, parent = `Pack for trip`, gated `crash: FOREIGN KEY constraint failed` | matches (Passport and Chargers are fixture rows the route does not touch) | — |

**A witness read before the action under test** (QUA-2740, resolved 2026-09-22 —
one case fixed, five recorded in data, a gate added). The three weaker rows have one
shape. The witness is a string the route itself put on screen, or tapped, BEFORE the step
the case measures: the typed title, the saved event on the list the switch returns to, or
the row that gets tapped. `derive_journey.py` records the route steps (1-based) at which
each witness is visible (`truth[case]["witness"]`), and for these three it is visible on
the seeded arm too, before the fault. The seeded arm never consults a witness, so bug
finding is unaffected. On the clean arm, though, reading the screen once before the action
and then reporting pass earns completion — `journey._witness` matches each string against
everything the device answered with over the WHOLE episode, in any order, so the ORDER the
agent saw it in is not part of the score.

**The gate.** `derive_journey.witness_credited_early` is that shape as a predicate: every
witness string visible on a CLEAN step before `action_step(route)` — the route's last step
that is not a `wait`, i.e. the interaction the case is about. `judge_witness` refuses such
a case (`agrees: false`), so no new weak witness can enter the corpus, and
`tests/test_witness_before_action.py` runs the same predicate over the committed YAML and
truth, without a device. The predicate is over the SET: one string that only the
destination shows is enough, which is exactly how the fixed case below was repaired.

**The exception list lives in the corpus, not here.** A weakness recorded only in prose is
invisible to every consumer, and completion is one of the two headline numbers. So each
case that keeps a pre-action witness carries `witness_before_action: QUA-2768` in its own
`data/test-cases/<app>.yaml` entry, naming the ticket that removes it; a scorer or a report
can read it off `journey.load_cases(app)` and exclude the case. The key is checked both
ways — a case the derive flags without one is refused, and a key on a case that is no
longer flagged is refused as stale — so the list cannot drift from the measurement.

**Fixed: `orgzly-open-note-from-notebook`.** The opened note's editor draws a breadcrumb,
`Getting Started with Orgzly  •  Notes`, which the derive's own matcher finds on clean
steps 3-4 and on neither step 1 (the Notebooks screen) nor step 2 (the notebook list —
the title is there, the breadcrumb is not). The witness is now the title AND the
breadcrumb, and the brief's last step asks for both, so the pair cannot be collected
before the tap.

**Residual, five cases (`witness_before_action: QUA-2768`).** Completion on these five of
43 is not a sound measurement until QUA-2767 and QUA-2768 land:

* `cal-switch-back-to-list` — the derive's step-10 screen is step 6's verbatim
  (`Search · Change view · Settings · More options · SEPTEMBER · 22 Tuesday · Standup ·
  02:00 PM · New Event`): switching to the yearly view and back RESTORES the screen the
  save already showed, so the route has no post-action string, and no one-step extension
  makes one — every screen reachable from the returned list is reachable from the same
  list before the switch, and a step that taps `Standup` would leave the seeded arm
  (stuck on the yearly view) with an unresolved anchor, i.e. INCONCLUSIVE instead of the
  FAIL this case measures.
* `orgzly-new-note-survives-rotation` — the post-rotation screens (steps 6-7:
  `Done · Insert timestamp · More options · Getting Started with Orgzly · Pay the rent ·
  Tags · State`) are a strict SUBSET of the pre-rotation step-5 screen; landscape draws
  the same editor with fewer fields. Inherent to a lifecycle case: what the clean arm
  proves is that the screen survived the configuration change, i.e. that it is the same
  screen.
* `orgzly-create-and-search` — `Team meeting` is on screen from step 5, where the route
  types it, and again at step 8 in the search box, both before the search is submitted at
  step 9. The `db:` oracle carries the case, so completion is not credited on the witness
  alone.
* `medtimer-review-aspirin` — `8:00 AM` is on clean steps 1-4: the Overview's reminder
  list at launch and the Medicine tab's own row, two screens before Aspirin's own. The
  case expects PASS on both arms, so both can be credited without opening Aspirin.
* `medtimer-analysis-tabular-view` — `Ibuprofen` is on the Overview at launch
  (`Ibuprofen (2.5)`), two steps before the Tabular view. Its oracle is a standalone
  `stuck:` probe, which can only say the screen kept answering touches, so the witness is
  what carries completion here.

**The two tickets, and what each can fix.** QUA-2767 makes completion credit order-aware
(the witness must be seen after the action, not anywhere in the transcript). That would
settle the three cases whose destination screen differs from the pre-action one, but NOT
`cal-switch-back-to-list`: its pre- and post-action screens are textually identical, so no
rule over screen text can tell the two apart. QUA-2768 re-authors the five, or gives them
a completion signal that is not screen text (a `db:` read, a UI-state check) — a new
detection path, which is why it is its own ticket rather than part of this repair.

## Side-bug references (QUA-2783, audited 2026-09-23)

A display side bug is scored on a case whose brief is about something else, so the brief
has to give the agent a way to know the shown value is WRONG, or the bug measures curiosity
rather than QA and inflates a careful agent's misses. `medtimer-add-medicine` was the
example: its rows read `9 left`, the true 10 was only on a stock screen the route never
opens, and the brief never said what the stock was. Three shapes count as a reference:

- **stated**: the brief states the true value or spelling (an expected value, a noun the
  screen misspells);
- **entered**: the route itself entered the value the screen then misstates;
- **cross-check**: the true value is drawn elsewhere on the route the brief walks.

Each side bug now carries `reference: {kind, note}` beside its id in `bugs:`.
`lint_journey_cases.py`'s `reference` rule fails a public side bug without one, and its
`quotable` rule fails a display marker under the evidence floor (`journey._evidence`,
2 characters), because no report can quote it. `reference:` is corpus metadata:
`journey.case_bugs` copies only id and marker, so no scorer sees it.

**15 side-bug placements on 13 cases. 10 already had a reference, 1 got an amended brief,
3 were dropped, and 1 has no reference and is deferred.** Two of the 10 are on
medtimer-review-aspirin, and the deferred one is orgzly-create-and-search. Both are
weak-witness cases that QUA-2768 owns, so this section records their audit and their YAML
was not edited. Two display defects left the journey corpus with the drops.

| case | side bug | marker | reference | verdict |
|---|---|---|---|---|
| anki-browse-cards | `browser-count-low` | `2 cards shown` | stated: the outcome names three cards, and the three rows sit under the subtitle | kept |
| anki-create-deck | `deck-new-count-low` | `2` | a cross-check existed (the deck list's `3 cards due` header), but the count is a one-character node no report can quote | **dropped; defect retired**; the case is now clean-only |
| cal-create-event | `event-editor-title-typo` | `New Evnet` | stated: step 1 names `Event` | kept |
| cal-create-task | `task-editor-title-typo` | `New Tsak` | stated: step 1 names `Task` | kept |
| contacts-create | `contacts-tab-typo` | `Contatcs` | stated: step 4 sends the agent to the contacts list | kept |
| medtimer-add-medicine | `stock-left-display-low` | `9 left` | **none**: the brief never gave the stock | **brief amended**: step 1 now states Aspirin and Ibuprofen each have 10 in stock (the outcome is unchanged, so it still holds on both arms) |
| medtimer-review-aspirin | `reminder-time-display-shifted` | `9:00 AM` | stated: the outcome's `8:00 AM` (Aspirin's own screen) | kept; **deferred** in data to QUA-2768 (weak-witness case, not edited here) |
| medtimer-review-aspirin | `stock-left-display-low` | `9 left` | stated: the outcome's stock of 10 (Aspirin's stock screen) | kept; **deferred** in data to QUA-2768 |
| orgzly-create-priority-note | `priority-letter-shifted` | `#B` | entered: step 5 sets priority A | kept |
| orgzly-complete-repeating-task | `repeater-done-loses-recurrence` | `DONE  Water the plants` | entered: step 4 enables the repeater, and step 9 asks for the state and scheduled date | kept (it relies on repeater semantics, deliberately not spelled out: that was the 2026-09-14 leak) |
| orgzly-create-and-search | `notebook-count-off-by-one` | `Contains 32 notes` | **none**: nothing in the brief or on the route counts the notebook's notes | **deferred** to QUA-2768 (weak-witness case, not edited here); it needs an amended brief or a drop there |
| tasks-create-with-due-date | `due-section-shifted` | `Due tomorrow` | entered: steps 3-4 choose Today | kept |
| tasks-complete-parent | `subtask-chip-low` | `1` | a stated reference existed (the brief names both subtasks), but the chip is one character | **dropped; defect retired**; the case keeps `subtasks-left-open` |
| tasks-change-due-time | `due-section-shifted` | `Due tomorrow` | stated: the outcome says due today | kept |
| tasks-complete-and-rename | `subtask-chip-low` | `1` | **none**: the brief never mentions `Pack for trip`; also unquotable | **dropped; defect retired**; the case is now clean-only |

**Retired, not deleted.** `deck-new-count-low` and `subtask-chip-low` left their apps'
`defects:` blocks with a dated note, like the §8 prune in docs/defect-classes.md. Their
patches and hunt features stay in `data/benchmarks/`, because both are also hunt defects.
The journey build still carries them, and no case switches them on. The two cases left
with no `bugs:` (anki-create-deck, tasks-complete-and-rename) stay in the corpus as the
first clean-only cases. Each still scores completion and prices a false report, but has no
seeded arm. The §8 convention would have removed them, and this ticket asked only to remove
the side bug. That choice is open for review. After the change,
`journey_adversary_check.py`'s CORPUS block lists no display defect: every honest miss left
is a functional defect with nothing quotable, which is prose by nature.

**Re-derived.** Only the four cases whose design or brief changed were re-derived. The
cases that only gained a `reference:` measure exactly what they measured before. The
derives ran with `derive_journey.py <app> --case … --repeat 3 --device emulator-5556`
(AVD qgbench_root2, android-35), 02:25-02:54 America/Chicago on 2026-09-23, far from
device midnight. Each installed journey APK was confirmed on the device by sha256 against
its `apk:` block: ankidroid `eb909e03…`, medtimer `0d1578a2…`, tasksorg `8be0720d…`.

| case | clean | seeded | row |
|---|---|---|---|
| anki-create-deck | HOLDS 3/3 | none (clean-only) | agrees |
| tasks-complete-parent | HOLDS 3/3 | VIOLATED 3/3 (`subtasks-left-open`), side `[]` | agrees |
| tasks-complete-and-rename | HOLDS 3/3 | none (clean-only) | agrees |
| medtimer-add-medicine | HOLDS 3/3 | HOLDS 3/3, side `stock-left-display-low` at step 2 | agrees |

Every other truth row is byte-identical. `corpus_version` moved from `c550afc271b6` to
`06ad85796b18`.

**Not covered here.** Both need an owner action:

- the three deferred placements above (QUA-2768 owns both cases; `orgzly-create-and-search`
  is the only placement in the public corpus left with no reference at all);
- the held-out split's seven side bugs. Its files live outside the repository, so the lint
  reports a held-out side bug with no reference as a warning ("held-out split, audit
  pending") rather than an error. **Audited 2026-09-23 under QUA-2789**, by the rules
  above: four kept with a reference, two kept after their brief was amended, one dropped
  from its case (its defect still rides on another held-out case, re-authored so that its
  marker is the seeded value). With `QGB_HELDOUT_DIR` exported the lint now reports 0
  errors and 0 held-out `reference` warnings. The per-placement rows live with the split.

## Screen witness

A read-only case (the agent changes nothing) has no state a `db:` oracle can distinguish
from a no-op. Its completion is instead **witnessed**: `evidence:` names one or more
strings that (1) the brief itself asks the agent to read, (2) the route's final screen
shows identically on the clean and the seeded arm, (3) equal or contain no defect
marker, symptom phrase or measured display text of any bug the case seeds — the last is
what `scripts/lint_journey_cases.py` enforces, and derive_journey.py should verify (2) on
the device — and (4) are not ALL readable before the step the case tests (QUA-2740;
`derive_journey.witness_credited_early` refuses one that is, unless the case records
itself with `witness_before_action:`). The defect marker stays out of the brief and out of the witness, so the case
is scoreable without pointing the agent at the bug: `medtimer-review-aspirin` witnesses
`8:00 AM` on Aspirin's own screen (shown on both arms; only the Medicine-tab row is
shifted to 9:00 AM), `anki-browse-cards` the three card fronts (never the `N cards shown`
subtitle).

Applied to: medtimer-review-aspirin and anki-browse-cards among the public read-only
cases, and — as a second half beside a `db:` oracle — orgzly-create-and-search, whose
brief promises both a saved record and a displayed list. A third shape arrived with the
freeze exemplars (QUA-2711): `medtimer-analysis-tabular-view` is read-only AND its oracle
is a standalone `stuck:` probe, which can only answer whether the screen kept answering
touches — never whether the agent read it. Its witness (`Ibuprofen`, the table's name
column) is what carries completion, required on top of the liveness oracle under the same
rule. Note the witness of such a case is checked on the CLEAN route's final screen only:
the seeded arm is expected to FAIL and never consults it. The same rule was applied to
read-only and blocked cases in the held-out split; their rows are kept with the split.

### What the harness does with it (implemented 2026-09-14)

Witnesses are matched against SCREEN READS only — device results that answered an observation tool (MCP) or a hierarchy dump (raw adb); an episode with no screen read at all stays unscored (None), never False. The contract as it was specified, and as `journey_verdict` now implements it:

`journey.py` before this change: `_oracle` already reads `evidence:` (in `present` mode it replaces
the present string; in `absent` mode it is the only agent-side check; in `db`/`content`
mode it is ignored), and `journey_verdict` leaves every PASS-expected `present`/`absent`
episode unscored (`completion_scored = False`) because a screenshot-only agent never emits
device text. The witness contract, precisely:

1. **Scoring rule.** For a case that declares `evidence:` and whose episode expects PASS:
   `completed = (verdict == pass) and every evidence string ∈ device_texts` (normalised
   substring, as `_oracle_verdict` matches today). If `_device_texts(transcript)` is empty
   — the agent produced no parsed device text at all — completion stays `None` with the
   reason `no device text channel`, never `False`: the stopgap's fairness argument survives
   exactly for the agent it was made for. A `present:`/`absent:` case that declares no
   `evidence:` stays unscored as now (after this audit none remain in the corpus).
2. **db/content mode.** A declared `evidence:` is ALSO required there, under the same
   rule: the device oracle must HOLD and the witness must be seen; with no device text
   channel the witness half is skipped and completion follows the device oracle alone.
   No existing `db:` case declared `evidence:` before this audit, so nothing regresses.
3. **Derivation.** `derive_journey.py` verifies each `evidence:` string with
   `replay._present` on the CLEAN pass's final dump (a `problems` entry when missing),
   records per string the steps at which it is visible on each arm
   (`truth[case]["witness"] = {string: {"clean": [steps], "seeded": [steps]}}`), and
   refuses a witness that sits inside any display bug's measured `texts` — that string is
   a marker, not a witness.
4. **Timezone and clock.** `query_db` runs its SQL under `QGB_DEVICE_TIMEZONE` and at the
   device's pinned instant (see Residual).

No new YAML field is needed: `evidence:` is the witness. The one semantic change is that
the harness scores it instead of discarding it.
