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
- No gate uses a model. Every gate here is a deterministic rule. The one judged figure,
  [G5](#g5-steps-accurate-side-column), is a side column: it is off by default, it is
  read from saved answers, and it is in no gate, no total and no ranking key.

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
| G2 | located | the report names the step where the defect shows, and its quote comes from that step | yes |
| G3 | complete | the report has an observed result, an expected result and real reproduction steps | yes |
| G4 | reproducible | a fresh agent given only the report reproduces the defect | later |

G1 decides nothing new. It reads the `matched` field the scorer saved.

A gate can also answer **unknown**: it ran and the saved episode does not hold enough
to decide. Unknown is never a pass. It is counted on its own (`totals.gates.<id>.unknown`)
and is in neither `evaluated` nor `passed`. Only G2 answers unknown today.

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
3. **The device showed the report's `observed` text while the agent was on that step.**

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
| follows a message with exactly one "Step N:" in it | step N, the current step from that message on |
| neither | the current step, or any later step that has an action, up to the step of the next action that fits or is announced. **Not sure.** When no such later step exists, it is the current step |
| fits only steps before the current one (the agent went back) | the latest step that fits. Whether the agent returned to it or is still looking into the current step is not known, so the steps after it up to the current one are possible, **not sure** |

A tap on coordinates aims at the element that the last screen listing put at those
coordinates. A call the server refused is not an action. Starting or stopping the app is
an action only in a case whose route restarts the app; elsewhere it is recovery after a
crash, and the agent is still on the step that crashed.

Then each **result** takes the steps of the last action before it. A result shows the
screen that action left, whether the action returned it or a later read did. The screen
stays up through the next step that has an action, so a read made after the agent moved
on, before it acts there, counts for both steps. Before any action, the screen is the
one the case starts on.

The answer for rule 3:

- **pass**: a result that holds the quote is surely on the named step;
- **unknown**: no such result, but one that holds the quote is possibly on it;
- **fail**: every result that holds the quote is on other steps, or none holds it.

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
  step.

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

An episode with no report has an empty `reports` list and zero totals.

In the example the report is valid (G1), G2 could not decide, and G3 failed, so
`customer_ready` is `false`: one failed headline gate decides it. A report that failed
nothing stays `null` until every headline gate has answered. G4 is not in the file
because no code registers it yet; a reader must treat a gate that is absent from
`gates` the same as one that did not run.

`quality.json` does not copy `bugs_present`. A figure that needs the seeded defects of
the episode (the yield below) reads them from `result.json`.

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
                           / seeded defects present
                           (per defect per episode, the denominator of catch_rate:
                           `bugs_present` of each counted episode's result.json)
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

### Which gates were available

A rate over four gates and a rate over three are different numbers. Every row names the
gates it had:

- `gates_available`: the headline gates that ran for the arm.
- `gates_missing`: the others. A headline gate is missing when a counted episode's
  `quality.json` does not have it, or when at least one report that failed nothing still
  waits for it.

While a gate is missing, `customer_ready_rate` and `customer_ready_yield` are **not
measured**: `measured` is false and `rate` is `null`. They are not 0. This holds even
when every report of the arm already failed some other gate. Until replay (G4) has run,
every row reads "not measured".

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
| `rows[].customer_ready_yield` | `measured`, `k`, `n`, `rate`, `ci` |
| `rows[].ready_on_available` | `gates`, `k`, `n`, `rate`, `ci`, `unknown`, and `yield` = `{k, n, rate, ci}` |
| `rows[].reports_per_defect` | `matched_reports`, `defects_found`, `value`, `repeats` = `{k, n, rate, ci}` |
| `rows[].complete_if_steps_carried` | `{k, n, rate, ci}` |
| `rows[].funnel[]` | one stage per headline gate: `gate`, `name`, `measured`, `entered`, `passed`, `failed`, `unknown`, `not_run` |
| `rows[].precision_coverage` | `signal` (`gates_passed`), `signal_gates`, `points[]` = `{min_gates, kept, coverage, valid, customer_ready}` |
| `rows[].by_report_source.<source>` | `scored`, `gates`, `customer_ready`, `complete_if_steps_carried`, `funnel`, in the same form |

`rate` is rounded to four places and `ci` is `[lo, hi]`; both are `null` when `n` is 0
or the figure is not measured. `ready + not_ready + unknown + waiting = scored`.

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
gate), one app at a time inside a stratum. Up to 40 carry
step labels: the agent's own step list beside the case's steps up to the step the
defect's reference report names. Order and choice come from SHA-256 of `--seed` and each
report's place, so the same seed and episodes give the same bytes. The script refuses to
write inside this repository, because a sample can hold held-out report text
(`--exclude-heldout` leaves those episodes out).
