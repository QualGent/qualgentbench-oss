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
- Scoring uses no model. Every gate here is a deterministic rule. G4 reads the saved
  result of a replay, in which a model acted and a rule decided; scoring never starts
  a replay.

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
| G4 | reproducible | a fresh agent given only the report reproduces the defect | yes, from saved replays (see "G4: replay from the report") |

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
| `replay` | present only when `quality_replay.json` is saved beside the episode: `replay_version` and `replayer`, the replayer and replay rules behind the G4 answers |

An episode with no report has an empty `reports` list and zero totals.

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
  failed-at) and the instruction to reproduce it. The replayer does not get the test
  case, the truth file or the first agent's transcript. Its working directory is named
  `replay-<12 hex>`, which names neither the case nor the defect.
- **Budget.** Tool calls: `budget_factor` (2) times the length of the case's reference
  route, `check.steps`. A case with no recorded route keeps its journey budget.
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
`textless: exclude` leaves them out of G4 instead. In the public corpus 10 of the 150
(case, defect) pairs are of this kind (6 `state`, 4 `absence`).

### Ceiling and control

A failed replay only says something about a report when a good report of the same
defect does replay, and when the defect is not found by accident. Two measurements
decide that per (case, defect), using the hand-written reference report
(`reference_reports.py`):

| Measurement | What is replayed | The defect is excluded when |
|---|---|---|
| ceiling | the reference report, `ceiling_attempts` (3) times | fewer than `ceiling_min` (2) reproduce |
| control | the reference report with its steps deleted, once | it reproduces |

An excluded defect is out of G4 for every arm: its reports get `pass: null` with the
reason, and they are in no G4 pass rate. Every exclusion is listed with one of these
codes:

| Code | Meaning |
|---|---|
| `ceiling` | the reference report reproduced fewer than 2 of 3 times |
| `control` | the reference report reproduced with its steps deleted |
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
its defect is known to be in.

### Running it

```bash
# What would run and the most episodes it can take. No device, no agent.
uv run qualgent-bench run --mode journey --quality-replay <run dir> --quality-replay-plan

# Run it. --agent and --models name the replayer.
uv run qualgent-bench run --mode journey --quality-replay <run dir> \
    --agent <agent> --models <model> --mcp-server <url> --device <serial>

# Rebuild the saved files from the replay episodes. No device, no agent.
uv run python -m qualgentbench.quality_replay fold <run dir> --agent <agent> --model <model>
```

The run command prints the plan and asks `Continue?` like any other run; without a
terminal it needs `--yes`. `--quality-replay-max N` caps the episodes started.
`--quality-replay-unit KEY` runs one unit, for a fan-out over several machines; the keys
are in `--quality-replay-plan --quality-replay-json`. Run it again to continue: the
replay episodes on disk are the state, and nothing is replayed twice.

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
| `budget_factor`, `budget_floor` | 2, 0 | the tool-call budget |
| `textless` | `canary_only` | how a defect with no text to find is judged |
| `max_retries` | 1 | retries of a replay that did not run |

While the pin names no replayer, `--agent` and `--models` must name one. Once it does, a
different one on the command line is refused; `--quality-replay-config` runs an
experiment under another file. The replayer and the rules are stamped into every replay
episode, into `quality_replay.json` and into `quality.json` (`replay`).

`scripts/quality_replay_pilot.py` is how the replayer is chosen. For each candidate it
replays the reference reports of the same 10 public defects (ceiling 3 times, control
once: at most 40 episodes per candidate) and prints one line per candidate: ceiling,
control failures, cost and wall time per replay. Without `--run` it prints the plan and
runs nothing.

### `quality_replay.json`

Written beside `result.json` of every journey episode that has a valid report. One entry
per valid report:

```json
{
  "replay_version": 1,
  "replayer": {"agent": "<agent>", "model": "<model>", "provider": null, "tooling": "mcp",
               "give_test_case": false, "budget_factor": 2.0, "budget_floor": 0,
               "textless": "canary_only", "replay_version": 1, "attempts": 1,
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
      "attempts": [
        {"episode": "replay-<12 hex>/<episode dir>", "valid": true, "reproduced": true,
         "canary_fired": true, "symptom_seen": true, "reason": "...", "steps": 11,
         "step_budget": 14, "truncated": false, "cost_usd": 0.42, "wall_time_sec": 95.0}
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `index` | the report's position in `result.json`'s `metrics.reports` |
| `defect` | the defect the report was matched to when it was replayed. G4 ignores an entry whose defect is no longer the report's `matched` |
| `status` | `replayed`, `excluded` (the defect is out of G4), `pending` (ceiling, control or the replay itself not run yet) or `unavailable` (the replay could not be run) |
| `reproduced` | true or false when `status` is `replayed`, else null |
| `basis` | `canary_and_text` or `canary_only` |
| `excluded` | null, or one of the codes above |
| `ceiling`, `control` | reproductions and valid replays of the reference report |
| `attempts[]` | every replay episode of this report. `episode` is relative to the replay directory. `valid: false` is a replay that did not run (staging failed, a provider limit) |

`<replay dir>/baseline.json` holds the ceiling and control of every defect with the same
fields, keyed `<app>/<case>/<defect>`, plus the corpus version.

In `quality.json`, G4 is `reports[].gates.G4` (`pass` true, false or null, with the
reason) and `totals.gates.G4`. A report of an excluded defect has `pass: null`, so its
`customer_ready` stays null and it is counted in `customer_ready_unknown`.

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
