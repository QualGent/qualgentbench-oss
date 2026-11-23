# Report quality

The journey scorer answers one question about a bug report: did it catch a seeded
defect? It does not say whether a developer could act on the report. Report quality is a
separate block that answers that second question, one yes/no **gate** at a time.

- Code: `src/qualgentbench/report_quality.py`
- Output: `quality.json`, written beside each episode's `result.json`
- Version: `QUALITY_VERSION` (currently `1`)

## It is not a ranking key

Report quality never moves the board.

- The module only reads the saved episode: `result.json` and, for the customer-shaped
  report, the agent transcript. It never writes either, and it changes no verdict, rate
  or ranking key.
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

The result is a pure function of the saved episode. There is no timestamp, host name or
random order in the file, so scoring the same episode twice writes the same bytes.

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
| G2 | located | the report names the step where the defect shows, and its quote comes from that step | later |
| G3 | complete | the report has an observed result, an expected result and real reproduction steps | later |
| G4 | reproducible | a fresh agent given only the report reproduces the defect | later |

G1 decides nothing new. It reads the `matched` field the scorer saved.

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
| `reports[].gates` | gate id → `{pass, reason}`. `pass` is `true`, `false`, or `null` when the gate did not run for this report |
| `reports[].customer_report` | the customer-shaped report of this report, neutral ones included: `summary`, `steps`, `expected`, `actual`, `failure_step`, `agent_steps`, `report_source`, `paired_by` (see above) |
| `reports[].customer_ready` | `false` when a headline gate failed, `true` when all passed, `null` otherwise and for neutral reports |
| `totals.reports` / `neutral` / `scored` | all reports, neutral ones, and the rest (`scored` is the denominator) |
| `totals.gates.<id>` | `evaluated`: scored reports the gate ran on; `passed`: how many passed |
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
`GateResult(None, reason)` when the gate does not apply to a report. A gate must be
deterministic and must not write anything.

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
gates a report failed, or `pass`), one app at a time inside a stratum. Up to 40 carry
step labels: the agent's own step list beside the case's steps up to the step the
defect's reference report names. Order and choice come from SHA-256 of `--seed` and each
report's place, so the same seed and episodes give the same bytes. The script refuses to
write inside this repository, because a sample can hold held-out report text
(`--exclude-heldout` leaves those episodes out).
