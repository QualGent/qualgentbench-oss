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
oracle over weakening the brief. Every changed `check:` carries a
`# TODO(derive): re-run derive_journey.py --case <id>` comment: an oracle is trusted only
after it has been re-derived on a device (clean HOLDS, seeded as `bugs:` implies).

Gate: `uv run python scripts/lint_journey_cases.py` (device-free; registered in CLAUDE.md
next to `adversary_check.py`) fails on the leak class (a `present:`/`absent:`/`evidence:`
string that carries a seeded defect's marker, symptom phrase or measured display text; a
brief that states a marker) and on oracle-less cases. It WARNS when a brief carries a
multi-word symptom phrase of its own bug — the shape of the two semantic leaks below
(the pre-fix orgzly brief tripped it on "marked done" and "next occurrence"; the two
remaining hits, `tasks-change-due-time`'s "9 AM" and "due time", are expected values a
brief has to state) — and when a `db:`/`content:` query names none of the brief's key
nouns (`contacts-favorite` checks `starred=1`, `openscale-delete-measurement` a fixture
id: both legitimate). None of the three leaks this audit fixed was a marker-level leak;
they were found by reading, and the lint is the regression guard, not the detector.

## Verdicts

40 cases, one primary verdict each: **17 matches · 12 oracle weaker than brief · 0 oracle
stronger than brief · 9 unscoreable read-only · 2 leaks defect** (a third leak,
medtimer-review-aspirin, is counted under read-only). Secondary verdicts are kept in the
table: three read-only cases are also weaker than their brief, and mmex-void-withdrawal
already had the witness shape but is still unscored on its clean arm today.

| case | brief promise | oracle checked | verdict | fix |
|---|---|---|---|---|
| anki-add-note | browser shows a card with front `quatro`, back `four` | notes `flds like 'quatro%four'` = 1 | matches | — |
| anki-add-note-to-deck | the new note is in the Spanish deck | cards in deck `Spanish` = 4 | matches | — |
| anki-browse-cards | browser lists the three cards uno, dos, tres | `present: uno` | unscoreable read-only; weaker (one of three names) | screen witness `evidence: [uno, dos, tres]` |
| anki-add-tagged-note | the saved note carries tag `verbs` | notes `tags like '%verbs%'` = 1 | matches | — |
| anki-create-deck | the deck list shows French alongside Spanish | `count(decks)` = 3 | weaker (any name passes) | decks `name = 'French'` = 1 · derive |
| cal-create-event | "Standup" saved and shown on **today's** date | title+type only, never the date | weaker (the plan's example) | `date(start_ts,…)=date('now',…)` · derive |
| cal-create-task | "Groceries" saved and shown on **today's** date | title+type only | weaker | same date clause · derive |
| cal-task-reminder | task "Pharmacy" has a reminder 10 minutes before | `reminder_1_minutes` = 10 | matches | — |
| cal-edit-event | calendar shows "Final" **and no longer shows "Draft"** | `count(Final)` = 1 | weaker (a copy passes) | `count(Final),count(Draft)` = `1,0` · derive |
| cal-delete-event | "Lunch" no longer shown | `count(Lunch)` = 0 | matches (do-nothing caveat, below) | — |
| contacts-create | "Alice" is listed | contacts contains `display_name=Alice` | matches | — |
| contacts-phone | Alice's contact shows 555-1234 | data contains `data1=555-1234` | matches (one contact exists) | — |
| contacts-favorite | Alice listed on the Favorites tab | contacts contains `starred=1` | matches (one contact exists) | — |
| contacts-edit | list shows "Alicia" **and no longer "Alice"** | contacts contains `display_name=Alicia` | weaker (a copy passes) | proposed only: a `content:` expectation is one `contains` or one row count — the "Alice gone" half needs a second expectation (harness) |
| contacts-delete | Alice no longer listed | contains `display_name=Alice`, absent | matches (do-nothing caveat) | — |
| medtimer-add-medicine | "Lisinopril" listed on the Medicine tab | Medicine `medicineName='Lisinopril'` = 1 | matches | — |
| medtimer-review-aspirin | Aspirin's screen: one reminder 8:00 AM, dosage 2; stock 10 | `present: '8:00 AM'` | unscoreable read-only; **leaks defect** (step 2 pointed at both seeded display rows) | step removed; screen witness `evidence: ['8:00 AM']` (Aspirin's own screen, identical on both arms) |
| medtimer-edit-reminder-dosage | the 8:00 AM reminder shows dosage 3 | Reminder `amount='3'` = 1 | matches (Aspirin's is the only reminder) | — |
| medtimer-skip-logged-dose | the Ibuprofen **2.5** event is Skipped | any Ibuprofen event SKIPPED | weaker (skipping the "(4)" reminder passes) | `and amount='2.5'` · derive |
| medtimer-rename-medicine | lists "Naproxen" **instead of** "Ibuprofen" | `count(Naproxen)` = 1 | weaker (a new medicine passes) | `count(Naproxen),count(Ibuprofen)` = `1,0` · derive |
| mmex-deposit-paycheck | deposit 40.00 listed; home shows Checking 140.00 | `present: '140.00'` | unscoreable read-only (it is not read-only: a deposit is written) | `db:` Deposit 40 = 1 · derive; witness `evidence: ['140.00']` |
| mmex-withdrawal-summary | 30.00 withdrawal listed; home shows Checking 70.00 | `present: '70.00'` | unscoreable read-only (writes a withdrawal) | `db:` Withdrawal 30 = 1 · derive; witness `['70.00']` |
| mmex-transfer-to-savings | Checking 75.00 and Savings 25.00 | `present: '25.00'` | unscoreable read-only on the clean arm (seeded is blocked → scored); weaker (75.00 unchecked) | witness `['75.00', '25.00']`; `present` kept — the transfer row may be written on the seeded build, so a `db:` count could HOLD where the case must FAIL |
| mmex-void-withdrawal | voided transaction listed; Checking still 100.00 | `absent: '75.00'` + `evidence: ['$ 100.00']` | unscoreable read-only on the clean arm (seeded is blocked → scored); already the witness shape | — (scored once the harness lands the witness rule) |
| mmex-withdrawal-with-note | saved with note "Weekly groceries"; home shows 75.00 | `NOTES='Weekly groceries'` = 1 | weaker (75.00 unchecked) | witness `['75.00']` (needs the db-mode witness rule, below) |
| openscale-add-measurement | Overview shows the new 60 kg measurement | weight 60.0 for user 1 = 1 | matches | — |
| openscale-statistics-range | Weight card: min 74 kg, max 85 kg | `present: 'Max: 85 kg'` | unscoreable read-only | witness `['Max: 85 kg']` (`Min: 74 kg` sits on the same card; `test_journey.py` pins this case's evidence list, so one string) |
| openscale-user-profile | Alice, 170 cm, born January 15, 1990 | `present: '170'` | unscoreable read-only; weaker (birth date unchecked — not in the route's final dump) | witness `['170']` |
| openscale-delete-measurement | the 85 kg Aug 29 measurement no longer shown | Measurement `id=3` = 0 | matches (fixture id = Aug 29) | — |
| openscale-edit-measurement | shows 90 kg **and a BMI of 31.1** | BMI value = 31.1 | **leaks defect** (the brief computed the derived value the defect breaks) | defect → `display`, marker `29.4`; brief "card shows 90 kg"; oracle weight = 90.0 · derive |
| orgzly-create-priority-note | "Book flights" at the bottom, state TODO | `state` = TODO | matches (priority deliberately not promised: its letter is the side bug) | — |
| orgzly-complete-deadline-task | "Renew passport" shown as DONE | `state` = DONE | matches | — |
| orgzly-complete-repeating-task | **not DONE, scheduled date moved to next occurrence** | `state` = '' | **leaks defect** (org-mode repeater semantics spelled out; the L4) | defect → `display`, per-case marker `DONE  Water the plants`; brief "still listed"; oracle `count(title)` = 1 · derive |
| orgzly-add-tag | "Quarterly report" shows tag `work` | `tags like '%work%'` = 1 | matches | — |
| orgzly-create-and-search | search results list "Team meeting" | `present: Team meeting` | unscoreable read-only (writes a note); route stopped before the search was submitted — the measured final dump is the whole notebook and the match was the search box | route `+ {press: enter}`; `db:` note = 1 · derive; witness `[Team meeting]` |
| tasks-create-with-due-date | "Groceries" listed, due date **today** | `dueDate>0` | weaker (any date) | `date(dueDate/1000,…)=date('now',…)` · derive |
| tasks-complete-parent | "Pack for trip" **and** both subtasks completed | Passport+Chargers completed = 2 | weaker (parent unchecked) | three titles completed = 3 · derive |
| tasks-change-due-time | "Water plants" due **today** at 9:00 AM | hour = 09 | weaker (any day) | `+ date(...)=today` · derive |
| tasks-delete | "Call dentist" no longer in the list | `count(Call dentist)` = 0 | matches (seeded row, so real) | — |
| tasks-complete-and-rename | "Call dermatologist" **instead of** "Call dentist"; "Library books" completed | dermatologist + Library completed = 2 | weaker (a copy passes) | `1,0,1` over three counts · derive |

### Needs `derive_journey.py` (15 cases)

anki-create-deck · cal-create-event · cal-create-task · cal-edit-event ·
medtimer-skip-logged-dose · medtimer-rename-medicine · mmex-deposit-paycheck ·
mmex-withdrawal-summary · openscale-edit-measurement · orgzly-complete-repeating-task ·
orgzly-create-and-search · tasks-create-with-due-date · tasks-complete-parent ·
tasks-change-due-time · tasks-complete-and-rename. The two reclassified defects must come
back with the marker in the screen diff (`side[].texts` non-empty) and `agrees: true`.
Until then `data/truth/journey-openscale.json` and `journey-orgzly.json` still record those
two cases as blocked — `journey_tasks` derives `expected` from the YAML, so the stale
truth only leaves `side[].texts` empty (the marker alone carries the match).

### Residual, not fixable in corpus text

- **Do-nothing oracles.** `cal-delete-event` and `contacts-delete` create and then delete
  the same record; a run that never created it satisfies `count = 0`. Fossify keeps no
  tombstone, so only a state-changing precondition (a seeded row) would close this;
  `tasks-delete` and `openscale-delete-measurement` are already in that shape.
- **`contacts-edit`**: one `content:` expectation cannot assert both "Alicia present" and
  "Alice absent". A second expectation per case is a harness field.
- **Host timezone.** `verify.device_oracle.query_db` runs the query in the harness process
  with the HOST's zone, while the device is pinned to `QGB_DEVICE_TIMEZONE` and the
  `sql:` fixture step runs under that zone in a child process. Every `'localtime'` in an
  oracle (`tasks-change-due-time`'s hour check predates this audit; the four new date
  checks follow it) therefore assumed host zone = device zone. Fixed 2026-09-14 (`query_db` evaluates under the device's `persist.sys.timezone`): run
  `query_db` under the device zone the way `_apply_script` does.
- **`due-date-edit-lost` lists the bare word `lost`** as a symptom; `test_journey.py`
  carries a strict xfail waiting for the corpus fix, so removing the word here would turn
  that xfail into a failure. Left for the owner of that test.
- **`openscale-user-profile`** promises a birth date the route's final dump never shows;
  `present`/witness cover the height only.

## Screen witness

A read-only case (the agent changes nothing) has no state a `db:` oracle can distinguish
from a no-op. Its completion is instead **witnessed**: `evidence:` names one or more
strings that (1) the brief itself asks the agent to read, (2) the route's final screen
shows identically on the clean and the seeded arm, and (3) equal or contain no defect
marker, symptom phrase or measured display text of any bug the case seeds — the last is
what `scripts/lint_journey_cases.py` enforces, and derive_journey.py should verify (2) on
the device. The defect marker stays out of the brief and out of the witness, so the case
is scoreable without pointing the agent at the bug: `medtimer-review-aspirin` witnesses
`8:00 AM` on Aspirin's own screen (shown on both arms; only the Medicine-tab row is
shifted to 9:00 AM), `anki-browse-cards` the three card fronts (never the `N cards shown`
subtitle), `openscale-statistics-range` the Weight card's `Max: 85 kg` (never `Avg:`).

Applied to: medtimer-review-aspirin, anki-browse-cards, openscale-statistics-range,
openscale-user-profile (the four truly read-only cases), mmex-transfer-to-savings and
mmex-void-withdrawal (already had it) for the screen half of a blocked case's clean arm,
and — as a second half beside a `db:` oracle — mmex-deposit-paycheck,
mmex-withdrawal-summary, mmex-withdrawal-with-note and orgzly-create-and-search, whose
briefs promise both a saved record and a displayed balance/list.

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
4. **Timezone.** `query_db` runs its SQL under `QGB_DEVICE_TIMEZONE` (see Residual).

No new YAML field is needed: `evidence:` is the witness. The one semantic change is that
the harness scores it instead of discarding it.
