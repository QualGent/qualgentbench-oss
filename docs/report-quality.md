# Report quality

The journey scorer answers one question about a bug report: did it catch a seeded
defect? It does not say whether a developer could act on the report. Report quality is a
separate block that answers that second question, one yes/no **gate** at a time.

- Code: `src/qualgentbench/report_quality.py` (one episode) and
  `src/qualgentbench/quality_rates.py` (a run, per arm)
- Output: `quality.json`, written beside each episode's `result.json`; the
  [Report quality block](#the-report-quality-block) adds those files up
- Version: `QUALITY_VERSION` (currently `1`)

## It is not a ranking key

Report quality never moves the board.

- The module only reads the saved episode: `result.json`, the agent transcript and the
  brief that was sent. Gate G2 also reads the corpus's case and truth files. It writes
  none of them, and it changes no verdict, rate or ranking key.
- The scoring modules do not import it, so it is outside the pinned scorer closure.
  `journey.SCORER_VERSION` and `tests/scorer_pin.txt` do not change when a gate changes.
  `tests/test_report_quality.py` asserts both.
- The module imports nothing from the rest of the package when it loads. What it knows
  about a report is what the scorer already saved.
- Scoring uses no model. Every gate here is a deterministic rule. G4 reads the saved
  result of a replay, in which a model acted and a rule decided; scoring never starts
  a replay. The one judged figure, [G5](#g5-steps-accurate-side-column), is a side
  column: it is off by default, it is read from saved answers, and it is in no gate, no
  total and no ranking key.

Read a quality number beside the ranking numbers, never in place of them.

## Running it

```bash
uv run python -m qualgentbench.report_quality <run dir>
```

`<run dir>` may be a runs directory, one run's directory, or one episode directory. The
command finds every directory that holds a `result.json`, scores each journey episode and
writes its `quality.json`. It writes no other file. A directory that is not a journey
episode (a hunt episode, an unreadable result) is skipped with one line on stderr. The
exit code is 1 when no journey episode was found.

It needs no device and no agent, so any saved run can be scored at any time.

The result is a pure function of the saved episode and the corpus. There is no
timestamp, host name or random order in the file, so scoring the same episode twice
against the same corpus writes the same bytes. Held-out cases are read from
`QGB_HELDOUT_DIR`, as everywhere else; without it G2 answers unknown for them.

From Python:

```python
from qualgentbench import report_quality

quality = report_quality.score_episode(episode_dir)   # the dict; reads only
report_quality.write_quality(episode_dir)             # writes quality.json
```

## The gates

A gate tests one property of one report and answers pass or fail with a reason.

| Gate | Name | Passes when | In this version |
|---|---|---|---|
| G1 | valid | the scorer matched the report to a seeded defect | yes |
| G2 | located | the report names the step where the defect shows, and the device shows the defect's symptom on that step | yes |
| G3 | complete | the report has an observed result, an expected result and real reproduction steps | yes |
| G4 | reproducible | a fresh agent given only the report reproduces the defect | yes, from saved replays (see "G4: replay from the report") |

G1 decides nothing new. It reads the `matched` field the scorer saved.

A gate can also answer **unknown**: it ran and the saved episode does not hold enough
to decide. Unknown is never a pass. It is counted on its own (`totals.gates.<id>.unknown`)
and is in neither `evaluated` nor `passed`. G2 and G4 can answer unknown.

A report is **customer-ready** when it passes every headline gate (G1 to G4). Gates are
not averaged: a clear report of something that is not a defect fails G1 and is not
customer-ready. While some headline gates do not exist yet, a report that has failed
nothing is recorded as unknown (`null`), never as ready.

### Neutral reports

The scorer marks some reports neutral: a known behaviour of the clean build, or one
downstream effect of a defect the agent already found. A neutral report is neither a find
nor a false report. In `quality.json` it is listed with `"neutral": true` and an empty
`gates` map, and it is left out of every total, as it is left out of every rate on the
board.

### G2, located

The scorer's `grounded` says the report's quote appeared somewhere in the device output
of the episode. It does not say where. A report can quote real screen text from one step
and name another. G2 checks the place.

G2 runs on a report that passed G1. It passes when all three hold:

1. **The report names a step.** The first number in the customer-shaped report's
   `failure_step`, else the saved report's `step`. No step, or a step the case does not
   have, fails.
2. **The truth file shows the matched defect at that step.**
3. **The device shows the defect's symptom on that step**, in a result that the agent's
   own actions on the device tie to the step. What that asks depends on what the defect
   leaves on the screen ("What the quote has to be", below). Where the symptom is not a
   text only the seeded build shows (a death with no dialog, text that is missing), the
   agent must also have read one of **the step's own screens** as the truth recorded
   them ("The step's own screens", below).

It reads the saved transcript (`agent/transcript.txt`), the brief the agent was sent
(`instruction_sent.md`), and the corpus's case file and truth file for the case. It does
not read `evidence/steps.jsonl`: that file cuts every result to 1,500 characters, and a
screen listing is longer.

#### Two step numberings

A report names a step of the **brief**: the numbered `steps:` the agent was given. The
truth file numbers the **route**: `check.steps`, one entry for each tap or text entry.
One brief step is usually several route steps, and the corpus does not record which. G2
lines them up from the text:

- A route step that taps or types a text **fits** a brief step whose sentence contains
  that text (`{tap: Save}` fits "Save the note."). Articles are ignored, and `_` counts
  as part of a word.
- Route steps are done in brief order, so the assignment never goes backwards.
- Among all such assignments, the ones with the most fits are kept. A route step's
  **span** is every brief step it takes in any of them. A route step with no text
  (`launch`, `wait`, a tap on a resource id) floats between its neighbours.

A brief step that no acting route step can belong to **only reads** ("Read what the list
shows"). It has no action, so the screen it reads is the one an earlier step left.

#### Where the truth shows a defect

| The matched defect is | Route steps |
|---|---|
| a side bug | its `visible_steps` |
| the blocking bug of a case whose seeded build dies | the step at which the death was recorded, and the step before it. The route finds the app dead when its next step runs, so the action that killed it is one of the two |
| any other blocking bug | every step whose clean/seeded screen diff (`unclaimed_diff`) carries a text of two characters or more |

The truth records the screen after a route step. That screen stays up until the next
action. So a symptom at a route step can be seen from the first brief step in that route
step's span, through the next brief step that has an action (or the last step of the
case). A report that names any of those steps satisfies rule 2.

#### Which step a device result belongs to

This is the attribution rule. The transcript holds the agent's calls in order, but no
call says which step it is.

First each **action** (a tap, a text entry, a swipe) is given a step:

| The action | Its step |
|---|---|
| fits a brief step: its target text is in that step's sentence, or contains route text that only that step can own | that step. When several fit, the first one at or after the current step. It becomes the current step |
| follows a message with exactly one "Step N:" in it | step N, the current step from that action on |
| neither | the current step, or any later step that has an action, up to the step of the next action that fits or is announced. **Not sure.** When no such later step exists, it is the current step |
| fits only steps before the current one (the agent went back) | the latest step that fits. Whether the agent returned to it or is still looking into the current step is not known, so the steps after it up to the current one are possible, **not sure** |

A tap on coordinates aims at the element that the last screen listing put at those
coordinates. A call the server refused is not an action. Starting or stopping the app is
an action only in a case whose route restarts the app; elsewhere it is recovery after a
crash, and the agent is still on the step that crashed.

**Saying a step is not being on it.** A "Step N:" message marks the agent's next action.
Until the agent acts, it moves no screen: a read made after the message still shows what
the last action left. That read is possibly step N's, and not surely. (Before this rule
an agent could say "Step 9:", read the start screen once, and have that screen counted
for every step up to 9.) The message does bound the actions before it that no step could
be given to: they were done by step N.

Then each **result** takes the steps of the last action before it. A result shows the
screen that action left, whether the action returned it or a later read did. The screen
stays up through the next step that has an action, so a read made after the agent moved
on, before it acts there, counts for both steps. Before any action, the screen is the
one the case starts on.

#### What the quote has to be

The truth file measured texts for each defect (`symptom_texts`):

| The matched defect is | Texts the seeded build **shows** | Texts it **lost** |
|---|---|---|
| a side bug | its marker and measured texts | none |
| a blocking bug that does not kill the app | every string the screen diff says the seeded build added | every string the diff says it removed |
| a crash or a hang | the exception the case names (`check.expect.crash` / `anr`) and Android's own dialog wording ("keeps stopping", "isn't responding", and the other forms) | none |

A truth text is matched on token boundaries, as the scorer matches a quote (`2h` is not
in `12h`). Where the deriver left `<date>` in a text, any date matches.

Rule 3 is then one of three checks.

**A text.** The report's `observed` holds one or more of the texts the seeded build
shows. The whole `observed` sentence does not have to be on the device: a report filed
through the report tool writes a sentence around its quote. Each truth text in it that
the device showed anywhere is looked for:

- **pass**: each is in a result that is surely on the named step;
- **unknown**: one is surely on the step and another only on other steps, or none is
  surely on the step and one is possibly on it;
- **fail**: the device showed them on other steps only, or showed none of them.

A truth text can also be on the screen the case starts on, on the clean build as well:
the screen diff records a string that differs at one step, not one that differs
everywhere. When every truth text the report quotes is of that kind, seeing it shows the
start screen and nothing more, so the agent must also have read one of the step's own
screens there; without one the answer is unknown. A text that only the seeded build's
start screen shows is the defect itself, wherever the agent read it.

**A death.** Most crashes put up no dialog: the app is gone and the agent sees the home
screen. A report then has no truth text to quote. It passes when both hold:

- the device showed its `observed` text on the named step, on a screen that an action of
  the agent's left (the screen the case starts on, read by an agent that has not acted,
  is not such a screen);
- the agent read one of the step's own screens on that step, as the truth's **clean**
  pass recorded them. The seeded pass is not asked: its screens after the death are the
  launcher's, which an agent reaches with HOME from anywhere.

With the first and not the second the answer is unknown, never a fail. An agent's own
action does not put it on a step: one tap anywhere after saying "Step N:" would do.

**An absence.** A defect can remove text and add none. The report cannot quote what is
not there; it names the text in `expected`. When `expected` holds a text the seeded build
lost, or the defect shows no text at all, G2 looks for a screen listing that the agent
read on the named step, left by an action of its own, that lacks the text **and is the
screen the truth shows the defect on**:

- it holds a text the seeded pass recorded on the screens of the defect's route steps
  and not on the start screen; or,
- where those screens hold no such text (the defect shows on the start screen again,
  less one string: a list after Save and Back), it holds any text of those screens, and
  the agent also read one of the step's own screens on the step.

The screen "lacks the text" when one or more of the texts the report names as missing
are not on it. Not every one has to be gone: a defect that drops the bottom tabs
`Overview`, `Medicine` and `Analysis` leaves `Medicine settings` on the screen, which
still holds the word.

A listing with none of the case's texts lacks every text there is. So does the start
screen, and so does the screen a step *starts* on, because the step has not put the text
there yet. None of them settles anything.

- **pass**: there is such a screen;
- **unknown**: there is none (every screen on the step still shows the text, the agent
  read no screen there, or the screen that lacks the text cannot be told to be the
  step's). Never a fail.

Where the defect also shows text, a quote that the device showed only on other steps, or
never, is still judged as a quote and fails.

**A quote that is no truth text.** When the device did show `observed` on the named
step, but it is none of the defect's texts and nothing above applies, G2 answers
unknown. The truth file records only the strings that differ between the two builds. A
string that is on both builds' screens can be a true observation ("the Notes field is
empty") or the start screen's title, and the truth cannot tell which.

#### The step's own screens

The truth row keeps the texts of the screen after every route step, on the clean pass and
on the seeded pass (`screens`). The texts that tell brief step N's screens from the
screen the case starts on are:

- every text on a screen that is up during step N: a screen left by a route step that
  can belong to step N, or by an earlier one whose screen stays up into it (the screen a
  step starts on is one of its screens);
- less every text the truth recorded before the route's first action, on either pass.
  An agent reads the start screen without doing anything, so a text that is also there
  says nothing about how far it got. A text counts as "also there" when a longer text on
  the start screen holds it on token boundaries (`Add` in `Add to favorites`);
- less texts too short to tell screens apart: under 4 characters, or under 6 with no
  letter in them (`Wed`, `14`, `10:00` are on a launcher's date and clock as well);
- when only the clean pass is asked (a death), less every text on a screen only the
  seeded pass recorded.

The agent "read one of the step's own screens" when a screen listing in a result that is
surely on the step, after an action of its own, holds one of those texts. When the list
is empty, the truth cannot tell the step's screens from the start screen, and the answer
is unknown.

This is what stops an agent that never finds out where it is. Before this rule
(QUA-3607), an agent that wrote symptom prose, copied `expected` from the brief, and on
each step said "Step N:", tapped one point and read a screen with nothing on it, passed
G1 and G2 on 99 of the 150 public defects: 56 as a crash "seen" after its own action and
43 as text "missing" from a screen that was never the step's. It now passes on none of
those. `scripts/quality_adversary_check.py` runs three such agents (`blind-tap`,
`blind-type`, `blind-home`).

#### What grounds a quote

The same device results as the scorer's `grounded`, under the same echo rules. The
reply of a text-entry tool, or of any tool that hands its own argument back, grounds
nothing. A refused call's reply grounds nothing. A typed argument never grounds itself.
The quote is matched as the scorer matches it: lower case, whitespace folded, at least
two characters. `tests/test_gate_located.py` compares this module's device text, tool
lists and normalisers with the scorer's own.

#### When G2 answers unknown

- The truth has no step for the defect: a defect seen only in stored data has no screen
  diff.
- The corpus has no case or truth row for the episode, or the brief the episode was sent
  does not hold the corpus's steps (the case changed after the run).
- The quote was on the device, but only in results that are not surely on the named
  step. This includes a screen read after the agent only said "Step N:".
- The device showed the quote on the named step, but it is no truth text of the defect.
- Part of the quote was on the named step and the rest only on other steps.
- A crash report's quote is on the screen the case starts on and the agent had not acted.
- A crash report's quote is on the named step after the agent's action, but no screen
  the agent read there is one of the step's own.
- A report on missing text has no screen on the step that lacks the text, or the screen
  that lacks it cannot be told to be the screen the truth shows the defect on.
- Every truth text the report quotes is also on the clean build's start screen, and the
  agent read none of the step's own screens there.

#### Limits

- **The bare-adb arm.** A tap there is `input tap X Y` with no text, and screen dumps
  are not used to look the point up. Only typed text and "Step N:" messages mark a step,
  so most bare-arm reports that reach rule 3 are unknown.
- **A brief that names nothing the route taps** cannot be lined up. Every route step
  then spans every brief step: rule 2 rules no step out and no action can be placed.
- **Diff noise.** `unclaimed_diff` holds every string that differed between the two
  builds, so an early step can carry an unrelated difference. Rule 2 then allows that
  step too. Rule 3 still has to hold there.
- **A quote the agent read later.** A crash can leave no dialog until the agent opens
  the app again. A report that quotes that dialog and names the step that crashed passes
  only when the agent was still on that step when the dialog came up.
- **A defect on the screen the case starts on.** Seven public defects show a text there
  that only the seeded build has (a misspelt tab title, a wrong count). An agent that
  reads that screen and quotes that text has seen the defect where it is, with no step
  of the test case done, and G2 locates it. The truth holds nothing that tells this from
  a tester who noticed the typo. G4 is the gate meant to stop it: its control replays
  the reference report with the steps deleted, and a defect that shows without steps is
  excluded from replay. The adversary check lists this as a known residue with its
  count, and fails when the count grows.
- **A step whose screens are the start screen.** When the truth's screens for a step
  hold nothing the start screen lacks, a death or an absence reported at that step is
  unknown whatever the agent did. So is a crash that happens before the agent has read
  any screen the start screen differs from, and a report whose device read was taken
  before the screen had filled in.
- **An agent that does the steps and then guesses.** The step's own screens show that
  the agent got to the step. They do not show that it did the step's action: an agent
  that reaches the right screen, does something else and sees the launcher is located
  on a crash step.
- **Measured.** On four saved boards (483 valid reports) the rule moved G2 from 356 pass
  / 41 fail / 86 unknown to 354 / 41 / 88, and on 97 valid reports of older local runs
  from 76 / 12 / 9 to 73 / 12 / 12. Every changed answer went from pass to unknown; none
  became a fail.
- **A true observation the truth did not measure.** A report that quotes text which is
  the same on both builds is unknown, although a person would call it located.
- G2 reads the corpus of the checkout that scores, not the corpus the episode ran on.
  The brief check above catches a changed case; it does not catch a truth row
  re-derived for the same steps.
### G3, complete

A developer needs three things from a bug: what happened, what should have happened, and
how to get there. G3 tests the [customer-shaped report](#the-customer-shaped-report) for
each one with fixed rules. It uses no model. It runs on every report that is not neutral,
valid or not, so the share of reports that are complete can be read on its own.

| Sub-check | Passes when |
|---|---|
| `observed` | the bug's `actual` is not empty |
| `expected` | the bug's `expected` is not empty |
| `steps` | the bug's `steps` text is a list of reproduction steps (below) |

G3 passes when all three pass. The bug keeps `expected` and `actual` only as a pair, so
for a report that came from the report tool the first two sub-checks agree.

**Steps that are steps.** The `steps` text is cut into entries and every rule must hold:

1. There are at least two entries. Each line is an entry. A single line is cut at inline
   numbers (`1. … 2. …`), else at arrows or semicolons, else at sentence ends. Bullets,
   numbers, `Step 3:` and tick or cross marks before an entry are ignored, and so is a
   heading line such as `Steps to reproduce:`.
2. Every entry opens with a verb from one of two fixed lists in the code: a UI action
   (`ACTION_VERBS`: tap, type, open, swipe, press, select, launch, go back, scroll and
   others) or an observation (`OBSERVE_VERBS`: verify, observe, check, wait and others).
   Any tense counts (`tap`, `taps`, `tapped`, `tapping`). Words such as "Then", "The
   user", "Long-" or a place clause ("On the Cart screen, tap Pay") may come first.
3. At least two entries are UI actions. Observations alone are not steps.
4. No entry names a source file (`NotesRepository.kt`) or a function call (`save()`).
   That is a code investigation, not a step a person can follow on a device.
5. The text is not the summary repeated (letter case, spacing and punctuation ignored).

The rule looks only at the first word of each entry and at these marks. It does not judge
whether the steps are correct, complete or in the right order, and an entry whose first
word happens to be a listed verb counts ("Opening hours are wrong"). The verb lists are
part of the rule: changing them falls under the version rule below.

**What this means for each report source.**

- *Report tool.* The bug's `steps` is the call's `code_investigation`, or a copy of the
  summary when there is none. Neither is a step list, so G3 fails on almost every such
  report. This is the measured cost of the mapping, not noise.
- *Findings entry.* A rendered findings entry has `observed` and `expected` but an empty
  `steps` and no step list at all: the findings file has no field for them. G3 fails
  with the reason `steps: no steps`, and `complete_if_steps_carried` is false as well.
  The gate is not skipped for these reports, because a report with no steps is not
  complete whatever its source. Read the G3 rate per `report_source` when comparing
  agents that report in different ways.

**`complete_if_steps_carried`.** The same steps rule is also run on `agent_steps`, the
step list the agent sent with its report and the bug does not carry. Each list item is
one entry (a string, or a mapping's `description`, `action`, `text`, `instruction`,
`title`, `name` or `step` text). `complete_if_steps_carried` is true when `observed`,
`expected` and this check pass: it is what G3 would be if the bug stored the agent's
step list as its steps. It is a side figure. It is not a gate and never changes
`customer_ready`.

In `quality.json` every scored report has a `complete` block (`null` for a neutral one),
and the totals count each sub-check:

```json
"complete": {
  "agent_steps": true,
  "agent_steps_reason": "3 entries, 3 UI actions",
  "complete_if_steps_carried": true,
  "expected": true,
  "observed": true,
  "steps": false,
  "steps_reason": "the steps repeat the summary"
}
```

```json
"totals": {
  "complete": {
    "agent_steps": 1,
    "complete": 0,
    "complete_if_steps_carried": 1,
    "evaluated": 1,
    "expected": 1,
    "observed": 1,
    "steps": 0
  }
}
```

`totals.complete.evaluated` is the denominator of every figure in the block and equals
`totals.gates.G3.evaluated`; `totals.complete.complete` equals `totals.gates.G3.passed`.
A report's `gates.G3` entry sits beside `G1` in the same `{pass, reason}` form, and its
reason names each part that is missing.

## The customer-shaped report

Quality is scored on the report as a customer would read it. The scorer's report of
record is still the findings file; that does not change, and nothing here feeds the
catch.

The product files a bug from one accepted `mobile_report_result` call. Its fields:

| Call argument | Bug field |
|---|---|
| `summary` | `summary`, the title. A missing one becomes "Agent reported an app issue" |
| `code_investigation`, else `summary` | `steps` |
| `expected`, `actual` | `expected`, `actual`: kept only as a pair, each cut to 250 characters |
| `failure_step` | `failure_step` |
| `steps` (the agent's step list) | `repro_steps`: the text of each entry, shown as the bug's reproduction steps beside the `steps` text |

Only a `FAIL` call files a bug. `PASS` and `BLOCKED` file none. Every field is trimmed,
and an argument that is not a string counts as absent.

`repro_steps` keeps the agent's words. An entry sent as text is kept as written, its own
pass or fail mark included; an entry sent as an object gives its first text field
(`description`, `action`, `text`, `instruction`, `title`, `name`, `step`). Each entry is
trimmed, sanitised like the other fields and cut to 500 characters; blank entries and
entries that are not text are dropped; at most 50 are kept. A closing sentence that
states the outcome instead of an action stays as the last entry. The bug's `steps` text
is not changed by this, and G3's `steps` check still reads that text;
`complete_if_steps_carried` reads the raw list (`agent_steps`), whose entries are the
same ones `repro_steps` keeps.

`customer_report(episode)` returns one `CustomerReport` per report, in the order of
`result.json`'s `metrics.reports`, so entry `i` belongs to report `i`:

| Field | Type | Meaning |
|---|---|---|
| `index` | int | position in `metrics.reports` |
| `summary` | str | the bug's title |
| `steps` | str | the bug's steps text; `""` for a rendered findings entry |
| `expected`, `actual` | str or None | both set or both None, each at most 250 characters |
| `failure_step` | str or None | |
| `agent_steps` | tuple | the call's own `steps` list, unchanged; empty for a findings entry |
| `repro_steps` | tuple of str | `agent_steps` as the bug stores it (above); empty for a findings entry |
| `report_source` | str | `report_tool`, `findings` or `scorer_report` (below) |
| `paired_by` | str or None | how the call was matched to this report (below) |

**Source.** In order:

1. The last `mobile_report_result` call the server accepted in the saved transcript
   (`agent/transcript.txt`), when its status is `FAIL`. `report_source` is `report_tool`.
2. Otherwise the saved report itself, rendered into the same fields under the same
   rules: `description` is the summary, `observed` is `actual`, `step` and `screen` give
   `failure_step` (`Step 4: Cart`), and `steps` is empty, because a findings entry has
   no investigation and no step list. `report_source` is `findings`.

One call carries one bug. When the episode has several reports, one of them takes the
call's fields and the others are rendered. `paired_by` says how that one was chosen:
`only_report` (there was one), `step` (its `step` is the first number in
`failure_step`), `text` (it shares the most words with the call), or `first` (nothing
matched; the first report). Ties go to the earlier report.

When the scorer itself read its report from a `BLOCKED` call, the product files no bug
for it. The saved report is rendered and marked `scorer_report`, not `findings`, because
no findings entry exists.

A `FAIL` call in an episode with no saved report adds nothing: gates run on the
scorer's reports.

**Sanitising.** The product redacts text before it stores a bug. This module mirrors two
of those rules, the ones a tester's sentence can trigger: the credential after `Bearer`
or `Basic`, and the value of an assignment such as `password: ...`, `pin=...` or
`token: ...`, both replaced by `[REDACTED]`. The product also redacts signed and local
URLs, local file paths, image data, key-shaped tokens and the values of sensitive
arguments. A report that contains such text can differ here from the stored bug.

**The shared fixture.** `tests/fixtures/report-mapping-fixture.json` holds
`mobile_report_result` payloads and the bug text each must produce: with and without
`code_investigation`, long `expected` and `actual`, one-sided pairs, `PASS`, `BLOCKED`,
credentials, and crash claims. The same bytes are committed in the product's repository
and tested there against the product's mapping; `tests/test_report_mapping_fixture.py`
tests them here. The file carries a `sha256` line (SHA-256 of its text with that line
removed) and each test pins the value, so a change to one copy fails until both copies
and both pins are updated. `product_only` fields in a case are checked by the product's
test alone.

## `quality.json`

Keys are sorted, the indent is two spaces, and the file ends with one newline.

```json
{
  "corpus": {
    "episode": {
      "corpus_version": "<12 hex>",
      "heldout_version": null
    },
    "scored_with": {
      "source": "packaged",
      "test_cases_sha256": "<12 hex>",
      "truth_sha256": "<12 hex>"
    }
  },
  "defects": {
    "<defect id>": {
      "coverable": false,
      "located_never": "no_screen_step",
      "replay_excluded": null,
      "replay_steps_needed": null
    }
  },
  "quality_version": 1,
  "reports": [
    {
      "complete": {
        "agent_steps": true,
        "agent_steps_reason": "3 entries, 3 UI actions",
        "complete_if_steps_carried": true,
        "expected": true,
        "observed": true,
        "steps": false,
        "steps_reason": "entry 1 of 2 is not a UI action: '<the first sentence>'"
      },
      "consequence_of": null,
      "customer_ready": false,
      "customer_report": {
        "actual": "<what the device showed>",
        "agent_steps": [
          "<the agent's first step>",
          "<its second step>",
          "<its third step>"
        ],
        "expected": "<what should have happened>",
        "failure_step": "Step 4",
        "paired_by": "only_report",
        "report_source": "report_tool",
        "repro_steps": [
          "<the agent's first step>",
          "<its second step>",
          "<its third step>"
        ],
        "steps": "<the call's code_investigation>",
        "summary": "<the bug's title>"
      },
      "gates": {
        "G1": {
          "pass": true,
          "reason": "matched seeded defect <defect id>"
        },
        "G2": {
          "pass": null,
          "reason": "the truth records no screen step for <defect id>",
          "unknown": true
        },
        "G3": {
          "pass": false,
          "reason": "steps: entry 1 of 2 is not a UI action: '<the first sentence>'"
        },
        "G4": {
          "pass": null,
          "reason": "no replay saved for this report"
        }
      },
      "index": 0,
      "known": null,
      "matched": "<defect id>",
      "neutral": false,
      "step": 4
    }
  ],
  "scorer_report_source": "findings_file",
  "scorer_version": 5,
  "totals": {
    "complete": {
      "agent_steps": 1,
      "complete": 0,
      "complete_if_steps_carried": 1,
      "evaluated": 1,
      "expected": 1,
      "observed": 1,
      "steps": 0
    },
    "customer_ready": 0,
    "customer_ready_unknown": 0,
    "gates": {
      "G1": {
        "evaluated": 1,
        "name": "valid",
        "passed": 1
      },
      "G2": {
        "evaluated": 0,
        "name": "located",
        "passed": 0,
        "unknown": 1
      },
      "G3": {
        "evaluated": 1,
        "name": "complete",
        "passed": 0
      },
      "G4": {
        "evaluated": 0,
        "name": "reproducible",
        "passed": 0,
        "unknown": 0
      }
    },
    "neutral": 0,
    "reports": 1,
    "scored": 1
  }
}
```

| Field | Meaning |
|---|---|
| `quality_version` | `QUALITY_VERSION` of the code that wrote the file |
| `corpus.episode` | the `corpus_version` and `heldout_version` the episode ran on, as `result.json` recorded them |
| `corpus.scored_with` | what G2 read when it scored, for the episode's app: `source` (`packaged`, `heldout`, or null when the corpus has no file for the app) and the first 12 hex digits of the SHA-256 of the test-case file and of the truth file. G2 reads the corpus of the checkout that scores, so this can differ from `corpus.episode`; no path and no clock is in it |
| `defects.<id>` | one entry per seeded defect present in the episode (`bugs_present`): can any report of it be customer-ready under this scoring? `located_never` is why G2 answers unknown for every report of it, or null: `no_truth` (no usable case or truth row), `not_in_truth`, `no_screen_step` (the truth recorded no screen for it), `no_step_screen` (it only removes text, and no screen the truth recorded can settle a report). `replay_excluded` is the code a saved replay excluded the defect with, or null. `coverable` is false when either is set. `replay_steps_needed` is G4's control diagnostic: true when the reference report did not reproduce with its steps deleted, false when it did, null when not measured; it decides nothing |
| `scorer_version` | the scorer version recorded in `result.json`; its marks are what the gates read |
| `scorer_report_source` | where the SCORER read the episode's reports, as `result.json` records it (`findings_file`, `transcript_write`, `result_line`, `report_tool`). Named `report_source` before the customer-shaped report existed |
| `reports[]` | one entry per report, in the order `result.json` lists them |
| `reports[].index` | position of the report in `result.json`'s `metrics.reports` |
| `reports[].step`, `matched`, `known`, `consequence_of` | copied from the saved report |
| `reports[].neutral` | true for a neutral report (see above) |
| `reports[].gates` | gate id → `{pass, reason}`. `pass` is `true`, `false`, or `null` when the gate did not run for this report or answered unknown. An unknown answer also carries `"unknown": true` |
| `reports[].customer_report` | the customer-shaped report of this report, neutral ones included: `summary`, `steps`, `expected`, `actual`, `failure_step`, `agent_steps`, `repro_steps`, `report_source`, `paired_by` (see above) |
| `reports[].complete` | G3's sub-checks for this report: `observed`, `expected`, `steps` and `steps_reason`; the same steps rule on the agent's own list as `agent_steps` and `agent_steps_reason`; and `complete_if_steps_carried`. `null` for a neutral report (see G3, complete) |
| `reports[].customer_ready` | `false` when a headline gate failed, `true` when all passed, `null` otherwise and for neutral reports |
| `totals.reports` / `neutral` / `scored` | all reports, neutral ones, and the rest (`scored` is the denominator) |
| `totals.gates.<id>` | `evaluated`: scored reports the gate answered pass or fail for; `passed`: how many passed; `unknown` (only on a gate that can answer unknown): how many it could not decide |
| `totals.complete` | scored reports passing each G3 sub-check: `evaluated`, `observed`, `expected`, `steps`, `complete`, `agent_steps`, `complete_if_steps_carried` |
| `totals.customer_ready` / `customer_ready_unknown` | scored reports that are ready, and those not yet decidable |
| `replay` | present only when `quality_replay.json` is saved beside the episode: `replay_version` and `replayer`, the replayer and replay rules behind the G4 answers |

An episode with no report has an empty `reports` list and zero totals.

In the example the report is valid (G1), G2 could not decide, and G3 failed, so
`customer_ready` is `false`: one failed headline gate decides it. A report that failed
nothing stays `null` until every headline gate has answered. G4 did not
run, because no replay is saved beside this episode; a reader must treat a gate that is
absent from `gates` (a file written before the gate existed) the same as one that did
not run.

`quality.json` does not copy `bugs_present`; `defects` has the same ids as its keys, and
a figure that needs the seeded defects of the episode (the yield below) still reads them
from `result.json`. A file written before `corpus` and `defects` existed has neither; a
reader treats the yield's ceiling as not known for it.

## G4: replay from the report

G4 asks whether a second agent, given only the report as a customer would read it, can
make the bug happen again. It is the one gate that needs a device and a model, so it is
split in two:

- `src/qualgentbench/quality_replay.py` runs the replays and saves their results.
- `report_quality.gate_reproducible` reads the saved result. It starts nothing. With no
  saved result the gate answers "did not run" (`pass: null`).

### A replay episode

- **Build and state.** The seeded build of the case the report came from, staged the way
  a journey episode of that case is staged: the same defects switched on, the same
  fixtures, the same pinned clock, the same check that the starting screen is right.
- **Brief.** The customer-shaped report (title, steps, expected result, actual result,
  failed-at) and the instruction to reproduce it. The steps are what the customer sees:
  the bug's `repro_steps` (the agent's own step list, as the bug stores it) when the
  report has them, numbered unless the agent numbered them; only a report without them
  gets the bug's `steps` text. For a report-tool bug with no code investigation that
  text is the summary repeated, so replaying it for a report that did list its steps
  would be the evidence-removed control (`quality_replay.replay_fields`). The reference
  report and the control are not affected. The replayer does not get the test case, the
  truth file or the first agent's transcript. Its working directory is named
  `replay-<12 hex>`, which names neither the case nor the defect.
- **Budget.** Device actions: `budget_factor` (3) times the length of the case's
  reference route, `check.steps`, at least `budget_floor` (10). An action is a call that
  changes the device: a tap, typing, a swipe, a key press, an app launch or stop, a
  rotation (`interactions.mcp_actions` / `adb_actions`). Screen reads, log reads, waits
  and a call the budget refused are not actions, and a tool that acts and reads
  (`mobile_tap_and_observe`) is one action. Once the budget is spent, the next action
  tool is refused before it runs and the episode ends (`truncated`); reads after the
  last action stay allowed, so the replayer can read the screen it produced. A separate
  hard cap on every tool call, `call_cap_factor` (4) times the action budget and at
  least `call_cap_floor` (60), stops a replayer that only reads (`call_capped`, not
  `truncated`). Each replay records `steps` (= `actions`), `step_budget`,
  `interactions` (reads included), `tool_calls` and `call_cap`. A case with no recorded
  route keeps its journey budget, as actions. Why actions: under the version-2 rule
  (2x the route, every interaction counted) a replayer needs one action and one read per
  report step, so the budget had no slack; in the pilot every failed replay was a
  budget kill, and 13 of the 19 killed replays had already reproduced the bug.
- **Attempts.** One per report.
- **Task type.** `result.json` carries `task_type: report_replay`, never `journey_case`,
  and its metrics have no `reports` list. The journey board, the rescore, the rates and
  `report_quality` itself all skip it; `tests/test_quality_replay.py` asserts each.

### The verdict

The verdict is a rule, `quality_replay.decide`. The model is the actor only; what it
says (`RESULT: reproduced=yes|no`) is recorded and never scored.

A replay **reproduced** the defect when both hold:

1. The defect's canary fired. A seeded patch reports that its own code path ran; the
   harness reads that after the replayer exits (`fault_fired` in the metrics).
2. One of the defect's truth texts appeared in what the device answered the replayer:
   the marker and measured texts of a display defect, the screen texts only the seeded
   build shows for a blocking defect, or the crash signature or system dialog of a crash
   or hang. For a crash or hang, a crash of the app that the harness itself recorded
   during the replay also counts. Text the replayer typed does not count.

Some defects give condition 2 nothing to look for: the truth row measured no text
(`state`), or the defect only removes text (`absence`). These are judged on the canary
alone and marked `basis: canary_only`, so a reader can report them apart. The setting
`textless: exclude` leaves them out of G4 instead.

A canary-only pass needs a replay that **finished**: the replayer ended on its own (no
budget or call-cap stop) and its last device action was accepted (not refused by the
budget hook, not failed). Otherwise the pass is `unconfirmed`: `reproduced: null`, no
verdict, never a pass. The canary of such a defect can fire before the report's last
step (the pilot's rotation defect fires at the rotation, before Save), and no truth field
names which route step is the key one, so the rule asks for the whole route to have run.
An unconfirmed replay is retried like one that did not run (`max_retries`), then reads
`unavailable` with the unconfirmed reason. A canary-and-text verdict needs no such guard:
the symptom text on the device is the evidence the steps ran. In the public corpus 10 of the 150
(case, defect) pairs are of this kind (6 `state`, 4 `absence`).

### Ceiling and control

A failed replay only says something about a report when a good report of the same
defect does replay. One measurement decides that per (case, defect), using the
hand-written reference report (`reference_reports.py`), and one more is reported beside
the answer:

| Measurement | What is replayed | Effect |
|---|---|---|
| ceiling | the reference report, `ceiling_attempts` (3) times | the defect is excluded when fewer than `ceiling_min` (2) reproduce |
| control | the reference report with its steps deleted, once | none: a diagnostic, `steps_needed` |

G4 answers "can a second agent reproduce the bug from the report as written". The
control keeps the title, the expected and actual results and the "Failed at" line, and in
the replay-version-2 pilot the replayers rebuilt the route from those in 17 of 18
controls; excluding such defects left none in G4. Since version 3 the control still runs
and is reported as `steps_needed` (true: it did not reproduce without the steps, so the
steps were needed; false: the rest of the report was enough; null: not measured) per
defect in `baseline.json`, per report in `quality_replay.json`, per defect in
`quality.json` (`defects.<id>.replay_steps_needed`), as a share in the Report quality
block (`rows[].replay_steps_needed`) and in the pilot table.

An excluded defect is out of G4 for every arm: its reports get `pass: null` with
`unknown: true` and the reason, and they are in no G4 pass rate. Every exclusion is listed with one of these
codes:

| Code | Meaning |
|---|---|
| `ceiling` | the reference report reproduced fewer than 2 of 3 times |
| `control` | replay version 2 only: the reference report reproduced with its steps deleted. Version 3 never writes it; a reader still reads it as an exclusion in an older file |
| `canary_silent` | the reference replay showed the defect's text but its canary never fired: verdict unavailable |
| `no_canary` | the build spec has no canary call for the defect: verdict unavailable |
| `no_reference` | the case has no reference report for the defect |
| `no_truth_text` | nothing to find on screen, and `textless` is `exclude` |

Every public defect has a canary call in its build spec, so `no_canary` excludes none of
them. For 40 public (case, defect) pairs the truth row recorded no fired canary, because
those rows were derived before a pass kept its canaries. That is a missing record, not a
missing canary, so those defects are replayed like the others and `canary_silent`
catches a build that really does not report one.

To save replays, a ceiling stops as soon as it is decided (2 of 2 reproduced, or 0 of
2), the control runs only after the ceiling passed, and a report is replayed only after
its defect is known to be in. A report does not wait for the control. A control that
could not be run leaves `steps_needed` null and changes nothing else.

### Running it

```bash
# What would run and the most episodes it can take. No device, no agent.
uv run qualgent-bench run --mode journey --quality-replay <run dir> --quality-replay-plan

# Run it. --agent and --models name the replayer.
uv run qualgent-bench run --mode journey --quality-replay <run dir> \
    --agent <agent> --models <model> --mcp-server <url> --device <serial>

# Rebuild the saved files from the replay episodes. No device, no agent. Name the
# replayer the episodes were run under, all of it.
uv run python -m qualgentbench.quality_replay fold <run dir> --agent <agent> --model <model> \
    --provider <provider> --tooling <mcp|raw>
```

`fold` folds only the episodes of the replayer it is given. An episode saved under
another fingerprint (agent, model, provider, tooling, `give_test_case`, budget and call
cap, `textless` or replay version) measured something else. When the replay directory holds such
episodes, `fold` writes nothing, prints how the fingerprints differ and exits 2:
a fold under the wrong replayer would find no episode of its own and rewrite every saved
G4 answer to "not measured". Leave `--provider` out only when the replays were run
without one. `--allow-other-replayer` folds this replayer's episodes anyway and prints
how many it left out; the plan (`--quality-replay-plan`, or `quality_replay plan`) prints
the same count, as `other_replayer` in its JSON. Exit codes: 0 folded, 2 refused or
another error.

A fold rewrites each touched episode's `quality.json` and keeps every column it does not
compute: an episode scored with `--quality-judge` keeps its G5 side column.

The run command prints the plan and asks `Continue?` like any other run; without a
terminal it needs `--yes`. `--quality-replay-max N` caps the episodes started.
`--quality-replay-unit KEY` runs one unit, for a fan-out over several machines; the keys
are in `--quality-replay-plan --quality-replay-json`. Run it again to continue: the
replay episodes on disk are the state, and nothing is replayed twice. The run command itself
folds with the other replayers' episodes left out, after printing their count in the plan.

Replay episodes are saved under `<run dir>/quality-replay/` (`--quality-replay-dir`
moves it). Several boards replayed by the same replayer can share one directory, and
then share the ceilings and controls. Episodes of a different replayer or replay version
in the directory are ignored and counted.

### The replayer

One replayer replays every arm, so that the arms differ only in their reports.
`src/qualgentbench/data/quality-replay.json` pins it together with the rules above:

| Key | Default | Meaning |
|---|---|---|
| `agent`, `model`, `provider` | null | the replayer; null until the pilot picks one |
| `tooling` | `mcp` | how the replayer drives the device (`mcp` or `raw`) |
| `give_test_case` | false | true adds the case's name, steps and expected outcome to the brief |
| `attempts` | 1 | replays per report; reproduced when any reproduces |
| `ceiling_attempts`, `ceiling_min` | 3, 2 | the ceiling |
| `ceiling_early_stop` | true | stop a ceiling once it is decided |
| `control_attempts` | 1 | replays of the report with its steps deleted |
| `budget_factor`, `budget_floor` | 3, 10 | the action budget: factor x the reference route, at least the floor |
| `call_cap_factor`, `call_cap_floor` | 4, 60 | the hard cap on every tool call: factor x the action budget, at least the floor |
| `textless` | `canary_only` | how a defect with no text to find is judged |
| `max_retries` | 1 | retries of a replay that did not run |

While the pin names no replayer, `--agent` and `--models` must name one. Once it does, a
different one on the command line is refused; `--quality-replay-config` runs an
experiment under another file. The replayer and the rules are stamped into every replay
episode, into `quality_replay.json` and into `quality.json` (`replay`).

`REPLAY_VERSION` (currently `3`) is part of the fingerprint. It moves when the brief, the
verdict rule or the budget rule changes; version 2 is the brief that gives `repro_steps`;
version 3 is the action budget and call cap, the unconfirmed canary-only pass and the
control as a diagnostic. No replay was ever run under version 1. The version-2 pilot's
episodes are kept as a record and are never folded under version 3.

`scripts/quality_replay_pilot.py` is how the replayer is chosen. For each candidate it
replays the reference reports of the same 10 public defects (ceiling 3 times, control
once: at most 40 episodes per candidate) and prints one line per candidate: ceiling,
usable defects, steps needed (the control's diagnostic), verdict unavailable, cost and
wall time per replay, truncated, unconfirmed and not-run replays. Without `--run` it
prints the plan and runs nothing. `--table` reads saved replays; its `usable` column
judges a ceiling with the early stop on, whatever the pilot's own config says, so a
cloud run that stopped after two agreeing replays reads as decided.

### `quality_replay.json`

Written beside `result.json` of every journey episode that has a valid report. One entry
per valid report:

```json
{
  "replay_version": 3,
  "replayer": {"agent": "<agent>", "model": "<model>", "provider": null, "tooling": "mcp",
               "give_test_case": false, "budget_unit": "actions", "budget_factor": 3.0,
               "budget_floor": 10, "call_cap_factor": 4.0, "call_cap_floor": 60,
               "textless": "canary_only", "replay_version": 3, "attempts": 1,
               "ceiling_attempts": 3, "ceiling_min": 2, "control_attempts": 1},
  "reports": [
    {
      "index": 0,
      "defect": "<defect id>",
      "status": "replayed",
      "reproduced": true,
      "reason": "the canary fired and its text appeared",
      "basis": "canary_and_text",
      "excluded": null,
      "ceiling": {"reproduced": 2, "attempts": 3},
      "control": {"reproduced": 0, "attempts": 1},
      "steps_needed": true,
      "attempts": [
        {"episode": "replay-<12 hex>/<episode dir>", "valid": true, "reproduced": true,
         "canary_fired": true, "symptom_seen": true, "reason": "...", "steps": 7,
         "step_budget": 21, "truncated": false, "unconfirmed": false,
         "interactions": 15, "tool_calls": 18, "call_capped": false,
         "cost_usd": 0.42, "wall_time_sec": 95.0}
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `index` | the report's position in `result.json`'s `metrics.reports` |
| `defect` | the defect the report was matched to when it was replayed. G4 ignores an entry whose defect is no longer the report's `matched` |
| `status` | `replayed`, `excluded` (the defect is out of G4), `pending` (ceiling, control or the replay itself not run yet, and still to be run) or `unavailable` (it could not be run: the replay itself, or the defect's ceiling or control, failed to run more often than `max_retries` allows and was given up) |
| `reproduced` | true or false when `status` is `replayed`, else null |
| `basis` | `canary_and_text` or `canary_only` |
| `excluded` | null, or one of the codes above |
| `ceiling`, `control` | reproductions and valid replays of the reference report |
| `steps_needed` | the control's diagnostic (see "Ceiling and control") |
| `attempts[]` | every replay episode of this report. `episode` is relative to the replay directory. `valid: false` is a replay that did not run (staging failed, a provider limit) or an `unconfirmed` canary-only pass. `steps` and `step_budget` are device actions |

`<replay dir>/baseline.json` holds the ceiling, control and `steps_needed` of every defect
with the same fields, keyed `<app>/<case>/<defect>`, plus the corpus version and
`steps_needed` = `{measured, needed, share}` over the defects that are in G4.

In `quality.json`, G4 is `reports[].gates.G4` and `totals.gates.G4` (`evaluated`,
`passed`, `unknown`). The answer for one report is one of four:

| G4 answer | When |
|---|---|
| `pass: true` / `pass: false` | the report was replayed and the verdict is in |
| `pass: null` (did not run) | the report is not valid; no replay is saved; the saved replay is for another defect; or the replay is still `pending` |
| `pass: null`, `unknown: true` | the defect is excluded from replay, or the replay or its ceiling could not be run or gave no verdict (`unavailable`, an unconfirmed canary-only pass included) |

A given-up ceiling is `unavailable`, not `pending`. Nothing is left to wait
for, so its reports are unknown beside the rate; saved as pending, one such report kept
its whole arm "not measured" for ever. To try again, delete the replay episodes that
did not run and run the replay again.

The Report quality block reads these the same way as every other gate. A report that
failed nothing and whose G4 did not run is waiting, so its arm's customer-ready rate is
"not measured". A report with an unknown G4 is counted as unknown beside the rate: it is
in neither side of the customer-ready rate, and it does not stop the rate from being
measured. Its own `customer_ready` stays null.

## The Report quality block

`quality.json` is one episode. The block adds the files up over a run and prints one
paragraph per **arm**: agent, model and condition, the same grouping as a board row.
Held-out episodes are their own row. Code: `src/qualgentbench/quality_rates.py`.

It is never a ranking key. Rows are listed in name order (public rows, then held-out
rows), never by a quality number. No scoring or ranking module imports the module, and
the scorer pin does not move when it changes.

### Where it is printed

```bash
# Print the block. Scores every episode in memory; writes nothing.
uv run python -m qualgentbench.quality_rates <runs dir> [--run RUN_ID]... [--json]

# The same from the quality.json files already saved (an episode without one has none).
uv run python -m qualgentbench.quality_rates <runs dir> --saved
```

- `qualgent-bench show` prints the block after the journey board, its Rates block and
  its notes, when at least one episode on the board has a saved `quality.json`. With no
  such file it prints what it always printed. Everything above the block is the same
  bytes in both cases.
- `qualgent-bench view` scores each journey episode in memory and adds a **Report
  quality** section under the boards of the run page, with a table of where reports are
  lost. Each episode page gets a table with one row per report and one cell per gate,
  with the gate's reason. The saved run is not written. This is the local view only:
  a `--portable` view, the kind that is published, carries no report quality yet.
- The view also stores each episode's quality block in its summary (`ep/<key>.json`,
  key `quality`) and the run's block in `manifest.json` (`runs[].report_quality`). Both
  keys are left out when no episode has a quality block, and from every portable view. `view --index-from` rebuilds
  the section and the manifest from the summaries byte for byte.

The view and the command read the recorded `result.json` of each episode, not a
rescored verdict.

### Who is counted

- **Episodes.** Every journey episode of the arm that has a quality block and that the
  board does not exclude (`failures.is_excluded`: environment or infrastructure failure,
  contamination, unclean MCP session, rate limit). Excluded episodes and episodes with
  no quality block are counted (`episodes_excluded`, `episodes_without_quality`) and
  are in no other number.
- **Reports.** Neutral reports are counted (`neutral`) and are in no rate. `scored` is
  the number of non-neutral reports.
- Trials are counted as they are, as in the Rates block: two trials of one case are two
  episodes. The intervals treat them as independent draws.

### The numbers and their denominators

Each rate is `k/n p% [lo–hi]` with a 95% Wilson interval (`rates.wilson`). A rate with
nothing in its denominator prints `0/0 —` and is `null` in the data, never 0.

```
gate pass rate (G1..G4)  = reports the gate passed
                           / reports the gate answered pass or fail for
                           (a gate that did not run for a report, or answered unknown,
                           is in neither side; unknown answers are printed beside the
                           rate with their share of the reports the gate answered)
customer_ready_rate      = reports that passed EVERY headline gate (G1, G2, G3, G4)
                           / reports with a decided answer: ready, or failed a gate
                           (a report left undecided by an unknown answer is in neither
                           side; the count and its share of scored reports are printed)
customer_ready_yield     = seeded DEFECTS with >= 1 customer-ready report
                           / seeded defects with a decided answer
                           (per defect per episode: `bugs_present` of each counted
                           episode's result.json, less the defects left unknown. A
                           defect is unknown when no report of it is ready and one or
                           more of its reports failed nothing and is undecided by an
                           unknown answer. Printed beside it: the defects present, the
                           unknown ones, and how many could be covered at all)
reports_per_defect       = matched reports / defects found
                           (a ratio, not a rate: 1.00 means no defect was reported
                           twice. A defect found in two episodes counts twice.
                           `repeats` = matched reports beyond the first for their
                           defect / matched reports, with an interval)
complete_if_steps_carried = reports that would pass G3 if the bug stored the agent's
                           own step list / scored reports (a side figure, never a gate)
```

One failed headline gate makes a report not ready, whatever the other gates say. This
is the same rule as `reports[].customer_ready`.

**One rule for unknown.** An unknown answer is in neither side of the rate, so it is in
neither side of the yield. Take three defects with one report each: one ready, one whose
defect replay excluded, one G2 could not decide. The rate is 1/1. Counting the two
unknown defects as misses would make the yield 1/3 for the same reports, and an arm
could raise its rate and lower its yield by nothing but the truth's gaps. The yield is
1/1 with `unknown: 2` and `present: 3` beside it. A defect that no report matched, or
whose every report failed a gate, is a miss and stays in the denominator.

**The ceiling.** Some defects can never be covered, whatever the agent writes: the truth
holds no screen for the defect, so G2 answers unknown for every report of it
(`defects.<id>.located_never` in `quality.json`), or replay excluded it from G4
(`replay_excluded`). `coverable` is the number of defects present that are neither, and
`not_coverable_by` counts the rest by reason (`no_screen_step`, `replay:control`, ...).
An exclusion from replay is a fact about the (case, defect), so it counts for every
counted episode of that case in the block, also for an arm that never reported the
defect. Read the yield against `coverable`, not against `present`: in the public corpus
6 of the 150 (case, defect) pairs have no screen step. `coverable` is `null` when a
counted episode's `quality.json` was written before the `defects` map existed.

### Which gates were available

A rate over four gates and a rate over three are different numbers. Every row names the
gates it had:

- `gates_available`: the headline gates that ran for the arm.
- `gates_missing`: the others. A headline gate is missing when a counted episode's
  `quality.json` does not have it, or when at least one report that failed nothing still
  waits for it.

While a gate is missing, `customer_ready_rate` and `customer_ready_yield` are **not
measured**: `measured` is false and `rate` is `null`. They are not 0. For a `quality.json`
written before a gate existed, this holds even when every report of the arm already
failed some other gate. G4 is in every file written now; until a replay is saved, an arm
with at least one report that passed G1 to G3 reads "not measured" for that reason.

For that time the row also carries `ready_on_available`: the same two figures over the
available gates only, labelled with those gates (for example "passing G1+G2+G3 only").
It is an upper bound on the customer-ready figures. Compare it only between rows with
the same `gates_available`.

A gate that is in the file, ran for no report, and has no report waiting for it is
available: every report failed an earlier gate. The rate is then a measured `0/n`. A
report made only of guesses reaches this case once G4 is registered.

### The funnel

```
all scored reports -> valid (G1) -> located (G2) -> complete (G3) -> reproducible (G4)
```

Each stage takes the reports that passed every gate before it (`entered`) and counts
what its gate did with them: `passed`, `failed`, `unknown`, `not_run`. Only `passed`
reports enter the next stage. A stage whose gate is missing is not measured (`measured`
false, counts `null`), and neither is any stage after it.

The funnel is cumulative, and a gate's own pass rate is not. G3 runs on every scored
report, so its pass rate has every scored report in the denominator, and the `complete`
stage has only the located ones.

### Per report source

`by_report_source` repeats the gate rates, the customer-ready counts, the side figure
and the funnel for each `customer_report.report_source` (`report_tool`, `findings`,
`scorer_report`). Read G3 there. A findings entry has no steps field, so it cannot pass
G3, and an arm that reports mostly through the findings file has a low G3 rate for that
reason.

### Precision against coverage

One signal is published: how many of the deterministic gates G2 and G3 a report passed
(0, 1 or 2). It is known before any replay. For each cut "at least N gates" the row
gives `kept` (reports at or above the cut), `coverage` (`kept` / scored reports),
`valid` (kept reports that passed G1 / kept) and `customer_ready` (ready / decided,
among the kept reports). `customer_ready` is `null` while a headline gate is missing.

### The block as data

`quality_rates.block(records)` returns the block; `--json` prints it, and the view puts
it in the manifest. `records` come from `quality_rates.episode_record(result, quality)`.

| Key | Meaning |
|---|---|
| `format` | `BLOCK_FORMAT`, currently 1 |
| `headline_gates`, `signal_gates` | `["G1","G2","G3","G4"]` and `["G2","G3"]` |
| `rows[]` | one per arm, in name order |
| `rows[].agent`, `model`, `condition`, `heldout` | the arm; `model` without its routing prefix, as on the board |
| `rows[].quality_versions` | the distinct `quality_version` values, as strings. More than one: the row is mixed and not comparable |
| `rows[].episodes`, `episodes_excluded`, `episodes_without_quality` | see "Who is counted" |
| `rows[].reports`, `neutral`, `scored` | all reports, neutral ones, the rest |
| `rows[].gates_available`, `gates_missing` | see "Which gates were available" |
| `rows[].gates.<id>` | `name`, `available`, `evaluated`, `passed`, `unknown`, `not_run`, `rate`, `ci` |
| `rows[].customer_ready` | `measured`, `ready`, `not_ready`, `unknown`, `waiting`, `k` (= `ready`), `n` (= `ready` + `not_ready`), `rate`, `ci`, `unknown_share` (= `unknown` / `scored`) |
| `rows[].customer_ready_yield` | `measured`, `k`, `n` (the defects with a decided answer: `present` − `unknown`), `rate`, `ci`, `present` (seeded defects present), `unknown`, `coverable`, `not_coverable`, `not_coverable_by` (reason → count) |
| `rows[].replay_steps_needed` | G4's control diagnostic over the arm's distinct (case, defect) pairs: `measured`, `needed` (the control did not reproduce), `share`; in no gate, rate or yield |
| `rows[].ready_on_available` | `gates`, `k`, `n`, `rate`, `ci`, `unknown`, and `yield` = `{k, n, rate, ci, present, unknown}` |
| `rows[].reports_per_defect` | `matched_reports`, `defects_found`, `value`, `repeats` = `{k, n, rate, ci}` |
| `rows[].complete_if_steps_carried` | `{k, n, rate, ci}` |
| `rows[].funnel[]` | one stage per headline gate: `gate`, `name`, `measured`, `entered`, `passed`, `failed`, `unknown`, `not_run` |
| `rows[].precision_coverage` | `signal` (`gates_passed`), `signal_gates`, `points[]` = `{min_gates, kept, coverage, valid, customer_ready}` |
| `rows[].by_report_source.<source>` | `scored`, `gates`, `customer_ready`, `complete_if_steps_carried`, `funnel`, in the same form |

`rate` is rounded to four places and `ci` is `[lo, hi]`; both are `null` when `n` is 0
or the figure is not measured. `ready + not_ready + unknown + waiting = scored`.

`format` is still 1: `present`, `unknown`, `coverable`, `not_coverable` and
`not_coverable_by` were added to the yield (QUA-3607) and no key was renamed or removed.
The yield's `n` is the one value whose meaning moved: it was every defect present, and
is now the decided ones, so that `k`/`n` is still the `rate` beside it. The two are
equal whenever `unknown` is 0.

## G5, steps accurate (side column)

G3 tests that a report has steps. It does not test that they are the right steps, and no
rule can. G5 asks a model one closed question per report and derives two rates from the
answer.

- Code: `src/qualgentbench/quality_judge.py`
- Saved answers: `quality_judge.json`, beside each episode's `result.json`
- Prompt and answer schema version: `JUDGE_PROMPT_VERSION` (currently `1`)

G5 is not a gate in the sense above. It is not in `GATES`, `HEADLINE_GATES`, a report's
`gates` map or `totals.gates`, and it never changes `customer_ready`. It is hidden until
the judge has been checked against human step labels: `JUDGE_ACCEPTED` is `false`, every
`totals.steps_accuracy` block carries it as `judge_accepted`, and a board or page shows
the number only when that flag is true, always marked as judged (`"by": "judge"`).

### Two commands

```bash
uv run python -m qualgentbench.quality_judge <run dir> --model <id>      # asks the model
uv run python -m qualgentbench.report_quality <run dir> --quality-judge  # reads the answers
```

Only the first calls a model. The second reads `quality_judge.json` and nothing else;
with no file, or no answer for a report's steps, the report is recorded as not judged.
Without `--quality-judge` the scoring command writes the same `quality.json` as before
G5 existed, whether or not a judge file is there.

The model is configuration: `--model`, else the environment variable
`QGB_QUALITY_JUDGE_MODEL`. There is no default. It must be a Bedrock inference-profile
id (`<scope>.<vendor>.<model>`), and the call goes to the Bedrock Runtime Converse API
with `AWS_BEARER_TOKEN_BEDROCK` and `AWS_REGION`, as the benchmark's other Bedrock calls
do. There is no other provider and no consumer login.

| Option | Effect |
|---|---|
| `--dry-run` | calls nothing and writes nothing; prints how many reports have steps, how many are already answered, how many calls a run would make, and a token estimate |
| `--max-calls N` | stops asking after N model calls; the rest stay not judged |
| `--retries N` | extra calls for an answer that does not fit the schema (default 1) |
| `--fake` | uses a built-in word-overlap stand-in, not a model. No spend. Its answers are not a judgement, and its id (`fake-word-overlap`) is stamped into every file it fills |

The exit code is 1 when a report got no answer, an episode was kept for another model,
or no journey episode was found.

### Which reports are judged

A report is judged when all three hold:

1. The scorer matched it to a seeded defect. A report that matched none has no "steps up
   to the defect".
2. It has a step list. The agent's own list (`agent_steps`) is used when there is one,
   each entry rendered as one line exactly as the [label sample](#the-label-sample)
   renders its `step_items`. Otherwise the bug's `steps` text is used when it passes G3's
   steps rule. A summary repeated, or a code investigation, is not a step list.
   The list is `agent_steps`, not `repro_steps`, so that a judge label and a human label
   are for the same text: an entry sent as an object is shown as compact JSON, where the
   stored bug shows its first text field.
3. The corpus has reference steps for the defect: the case's own steps from step 1 to
   the step that the defect's reference report names. This is the label sample's
   `reference_steps` rule.

Every other report has `"applies": false` and is in no G5 count. A list of more than 60
steps on either side is not sent.

### What the judge is shown

Two numbered lists and a fixed instruction, nothing else:

```
REFERENCE STEPS
R1. <case step 1>
R2. <case step 2>

REPORT STEPS
S1. <report step 1>
S2. <report step 2>
```

The prompt holds no defect id, no truth text, no summary, no expected or observed
result, no app, case, agent or model name, and no path. A test sends a report through
the judge and checks each of these.

### The answer

One JSON object with one entry per report step:

```json
{"steps": [{"step": 1, "label": "correct", "matches": [1]},
           {"step": 2, "label": "extra", "matches": []}]}
```

| Label | Meaning |
|---|---|
| `correct` | a person can follow the step, and it does what one or more reference steps ask |
| `ambiguous` | it probably belongs to a reference step but is too vague to follow with certainty |
| `extra` | it is not in the reference steps and would not stop the reproduction |
| `wrong` | it contradicts the reference steps, or cannot be done |

`matches` lists the reference steps the step covers (`correct`, `ambiguous`) or gets
wrong (`wrong`). The answer is refused, and asked for again, when an entry is missing or
out of order, a label is outside the set, a number is out of range or repeated, a
`correct` or `ambiguous` step matches nothing, or an `extra` step matches something.
Anything else in the answer, such as a comment or a score, is dropped.

**Missing steps** are not asked for. They are computed: a reference step is missing when
no `correct` or `ambiguous` step lists it. A reference step that only a `wrong` step
lists is missing.

### The two rates

```
step precision = report steps labelled correct / all report steps
step recall    = reference steps not missing   / all reference steps
```

An `ambiguous`, `extra` or `wrong` step is not a correct step. G5 passes when both rates
are at least `PASS_THRESHOLD` (0.8). The test uses the exact fractions, so 4 of 5 passes.

### `quality_judge.json`

```json
{
  "format": "rq-step-judge",
  "model": "<the judge model id>",
  "prompt_sha256": "<hash of the prompt version, the instruction and the label set>",
  "prompt_version": 1,
  "results": {
    "<key>": {
      "labels": ["correct", "wrong", "correct"],
      "matches": [[1], [2], [4]],
      "missing_steps": [2, 3],
      "reference_steps": 4,
      "report_steps": 3,
      "step_source": "agent_steps"
    }
  },
  "version": 1
}
```

- `<key>` is SHA-256 of (the two step lists, `prompt_sha256`, the model id). A change to
  any step's text, to the prompt or to the model gives another key, so an old answer is
  never read for new inputs.
- `labels[i]` and `matches[i]` belong to report step `i + 1`. `missing_steps` and
  `matches` hold reference step numbers, counted from 1.
- The file holds answers only: no step text, no report text, no defect id, no clock and
  no path. A judge file for a held-out episode therefore holds no held-out text.
- One file holds one model's answers. Running the judge with another model (or with
  `--fake`) leaves such an episode alone and exits 1; `--overwrite` replaces the file.
- A second run finds every key, makes no call and leaves the bytes alone. The file is
  rewritten only when its text changes, and it then holds exactly the answers for the
  episode's current reports.
- An entry that does not fit its report (edited by hand, damaged) is read as no answer.
- An episode with no report to judge gets no file.

To publish a G5 number, commit each episode's `quality_judge.json` with the board. The
number then re-derives with `--quality-judge` and no model call.

### In `quality.json`

With `--quality-judge`, every report gets `steps_accuracy` (`null` for a neutral report)
and the totals get a block of the same name. No other key changes.

```json
"steps_accuracy": {
  "applies": true,
  "by": "judge",
  "counts": {"ambiguous": 1, "correct": 2, "extra": 1, "missing": 1,
             "reference_steps": 4, "report_steps": 5, "wrong": 1},
  "labels": ["correct", "wrong", "correct", "extra", "ambiguous"],
  "missing_steps": [2],
  "pass": false,
  "precision": 0.4,
  "reason": "precision 2/5, recall 3/4",
  "recall": 0.75,
  "step_source": "agent_steps"
}
```

A report that is not judged has only `by`, `applies`, `pass` (`null`) and `reason`:
`applies` is false when G5 has nothing to judge, and true when it has but no saved
answer fits (no file, another prompt version, changed steps).

```json
"totals": {
  "steps_accuracy": {
    "applies": 1,
    "by": "judge",
    "evaluated": 1,
    "judge_accepted": false,
    "labels": {"ambiguous": 1, "correct": 2, "extra": 1, "wrong": 1},
    "missing_steps": 1,
    "model": "<the judge model id, or null>",
    "not_judged": 0,
    "passed": 0,
    "prompt_version": 1,
    "reference_steps": 4,
    "threshold": 0.8
  }
}
```

In the per-arm [Report quality block](#the-report-quality-block), an arm with at least
one episode scored this way gets `rows[].steps_accuracy`: `by`, `judge_accepted` (true
only when every such episode says so), `passed`, `evaluated`, `rate` and `ci` (the G5
pass rate with its 95% Wilson interval, `null` when `evaluated` is 0), `applies`,
`not_judged`, and `model` (`null` when the episodes name more than one judge model; all
of them are in `models`). An arm with no such episode has no such key. The printed block
and its HTML do not show the figure; a reader of the data shows it only when
`judge_accepted` is true.

`evaluated` is the denominator of the G5 pass rate and `passed` its numerator;
`applies = evaluated + not_judged`. `labels`, `missing_steps` and `reference_steps` sum
over the judged reports. `precision` and `recall` are rounded to four places.

`QUALITY_VERSION` does not move for G5: a `quality.json` written without the flag is
unchanged, and no published board was scored with it. A change to the prompt, the labels
or the answer rules bumps `JUDGE_PROMPT_VERSION`; saved answers of another version are
then not read, and a test pins the prompt's hash to its version.

### Limits

- The step text is written by the agent under test and is shown to the judge. The
  instruction says the lists are data, and the answer must fit a closed schema, but a
  step that argues for its own label is not ruled out. Read the labels when a number
  looks too good.
- The reference is the case's own steps. A report that reaches the defect by another
  valid path is marked down. That is the definition of G5, not a fault of the judge.
- Few reports carry a step list today, so the pass rate can rest on a small count. Read
  it with `evaluated` beside it.
- The judge's agreement with people is not yet measured. Until it is, the number is not
  shown.

## The version rule

`QUALITY_VERSION` names the rules that produced a `quality.json`.

- Bump it when a change can alter the `quality.json` of an episode that a published board
  already scored: a gate's rule, a gate added or removed, or a change to which reports
  are counted.
- Do not bump it for a change that cannot alter any output, such as a refactor or a
  comment.
- Add one line to the history beside the constant for each version.
- Numbers from different quality versions are not comparable. Because scoring needs only
  the saved episode, an old run can be scored again under the current version.
- `QUALITY_VERSION` is independent of `SCORER_VERSION`. A scorer change that alters
  `matched` or the neutral marks changes G1 through the saved `result.json`; the file
  records `scorer_version` so that is visible.

## Adding a gate

Write a function `(episode, report, index) -> GateResult` and append a `Gate` to `GATES`
in `report_quality.py`. `episode.dir` is the episode directory and `episode.result` is
its parsed `result.json`; `report` is one entry of `metrics.reports`, and
`customer_report(episode)[index]` is its customer-shaped report (computed once per
episode). Return
`GateResult(None, reason)` when the gate does not apply to a report. A gate that ran
and cannot decide returns `GateResult(None, reason, unknown=True)` and is registered
with `reports_unknown=True`. A gate must be deterministic and must not write anything.

## The label sample

The gates are rules; whether a rule agrees with a person is checked on a label set,
`rq-v1`. `scripts/quality_label_sample.py` draws it from scored episodes:

```bash
uv run python -m qualgentbench.report_quality <run dir>
uv run python scripts/quality_label_sample.py <run dir> [<run dir> ...] --out <private dir>
```

It writes `rq-v1-label-sample.json` for the labellers (report text and the app's name;
no agent, model, episode, case, defect, build or gate result, and no key outside a fixed
whitelist) and `rq-v1-label-sample-key.json`, which is not for them: `item_id` → `board`
(the run directory's name), `episode` (its path under that directory) and `report_index`,
so a label joins to `reports[report_index]` of that episode's `quality.json`.

By default it draws 120 reports that are not neutral: about 20 from clean builds, the
rest shared equally between the strata of seeded-build reports (the set of headline
gates a report failed, or `pass`; a gate that answered unknown is named too, as in
`fail:G3,unknown:G2`, so such reports are never counted as passes or failures of that
gate), one app at a time inside a stratum. A gate that did not run is not named, so
`pass` holds a report that passed G4 and one G4 never ran for. `--name-not-run` gives
the second its own stratum (`not_run:G4`); it is off by default, because `rq-v1` was
drawn without it and with it the same episodes give other bytes (the key then records
`targets.name_not_run`). Up to 40 carry
step labels: the agent's own step list beside the case's steps up to the step the
defect's reference report names. Order and choice come from SHA-256 of `--seed` and each
report's place, so the same seed and episodes give the same bytes. The script refuses to
write inside this repository, because a sample can hold held-out report text
(`--exclude-heldout` leaves those episodes out).

Each line of `step_items` is sanitised as the other fields of the customer-shaped report
are (`report_quality.sanitize_text`), and the judge renders its steps the same way, so a
credential an agent typed into a step reaches neither a labeller nor the judge. No step
of the drawn `rq-v1` sample holds such text: drawn again from the same episodes and
seed, both files have the bytes they had.
