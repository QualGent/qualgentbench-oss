# Report quality

The journey scorer answers one question about a bug report: did it catch a seeded
defect? It does not say whether a developer could act on the report. Report quality is a
separate block that answers that second question, one yes/no **gate** at a time.

- Code: `src/qualgentbench/report_quality.py`
- Output: `quality.json`, written beside each episode's `result.json`
- Version: `QUALITY_VERSION` (currently `1`)

## It is not a ranking key

Report quality never moves the board.

- The module only reads the saved episode: `result.json`, the agent transcript and the
  brief that was sent. Gate G2 also reads the corpus's case and truth files. It writes
  none of them, and it changes no verdict, rate or ranking key.
- The scoring modules do not import it, so it is outside the pinned scorer closure.
  `journey.SCORER_VERSION` and `tests/scorer_pin.txt` do not change when a gate changes.
  `tests/test_report_quality.py` asserts both.
- The module imports nothing from the rest of the package. What it knows about a report
  is what the scorer already saved.
- This version uses no model. Every gate here is a deterministic rule.

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
| G3 | complete | the report has an observed result, an expected result and real reproduction steps | later |
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

## G2, located

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

### Two step numberings

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

### Where the truth shows a defect

| The matched defect is | Route steps |
|---|---|
| a side bug | its `visible_steps` |
| the blocking bug of a case whose seeded build dies | the step at which the death was recorded, and the step before it. The route finds the app dead when its next step runs, so the action that killed it is one of the two |
| any other blocking bug | every step whose clean/seeded screen diff (`unclaimed_diff`) carries a text of two characters or more |

The truth records the screen after a route step. That screen stays up until the next
action. So a symptom at a route step can be seen from the first brief step in that route
step's span, through the next brief step that has an action (or the last step of the
case). A report that names any of those steps satisfies rule 2.

### Which step a device result belongs to

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

### What grounds a quote

The same device results as the scorer's `grounded`, under the same echo rules. The
reply of a text-entry tool, or of any tool that hands its own argument back, grounds
nothing. A refused call's reply grounds nothing. A typed argument never grounds itself.
The quote is matched as the scorer matches it: lower case, whitespace folded, at least
two characters. `tests/test_gate_located.py` compares this module's device text, tool
lists and normalisers with the scorer's own.

### When G2 answers unknown

- The truth has no step for the defect: a defect seen only in stored data has no screen
  diff.
- The corpus has no case or truth row for the episode, or the brief the episode was sent
  does not hold the corpus's steps (the case changed after the run).
- The quote was on the device, but only in results that are not surely on the named
  step.

### Limits

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
| `steps` (the agent's step list) | not on the bug |

Only a `FAIL` call files a bug. `PASS` and `BLOCKED` file none. Every field is trimmed,
and an argument that is not a string counts as absent.

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
      "consequence_of": null,
      "customer_ready": null,
      "customer_report": {
        "actual": "<what the device showed>",
        "agent_steps": [],
        "expected": "<what should have happened>",
        "failure_step": "Step 4",
        "paired_by": "only_report",
        "report_source": "report_tool",
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
    "customer_ready": 0,
    "customer_ready_unknown": 1,
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
| `reports[].customer_report` | the customer-shaped report of this report, neutral ones included: `summary`, `steps`, `expected`, `actual`, `failure_step`, `agent_steps`, `report_source`, `paired_by` (see above) |
| `reports[].customer_ready` | `false` when a headline gate failed, `true` when all passed, `null` otherwise and for neutral reports |
| `totals.reports` / `neutral` / `scored` | all reports, neutral ones, and the rest (`scored` is the denominator) |
| `totals.gates.<id>` | `evaluated`: scored reports the gate answered pass or fail for; `passed`: how many passed; `unknown` (only on a gate that can answer unknown): how many it could not decide |
| `totals.customer_ready` / `customer_ready_unknown` | scored reports that are ready, and those not yet decidable |

An episode with no report has an empty `reports` list and zero totals.

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
