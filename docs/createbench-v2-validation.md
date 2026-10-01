# CreateBench v2 positive-control validation: INTERIM (QUA-2861, 2026-10-01)

**Status: INTERIM. Verdict INCOMPLETE (driver exit 4). There is no GO/NO-GO yet.** The owner stopped the run after 13 of 40 cells had been graded, at $74.12 spent. The arm-B treatment reached the author but the author did not adopt it. Arm B's creation author read the guide resource on 7/7 cells, and that resource contained the harmful rule. Even so, **0/7 arm-B cases dropped their outcome check for a trivial screen-title check.** One case (1/7) did end on a screen-title Verify step, but it added that step after its real outcome check rather than in place of it. Arm A ended on a screen-title check 0/7 times.

The benchmark can only show sensitivity if the treatment changes what the authors write, and here it did not. A completed run would most likely have produced MISSED (power was 7/7 on arm B and 6/6 on arm A so far, Fisher p = 1). That MISSED would have said nothing about whether the benchmark can detect a worse prompt. QUA-2861 stays In Progress and will be re-run after a follow-up ticket fixes the treatment.

| | |
| --- | --- |
| experiment | `qua2861-harmful-rule-positive-control`, state run id `20261001-173940-4909`, runs dir `~/.qualgentbench/runs-qua2861` (a dedicated directory outside every repo) |
| prediction | `harmful-rule-positive-control-mechanism/v1`, sha `fcec04cefb3f` (QUA-2862, owner decision 2026-10-01). Frozen at registration, unchanged |
| design | 12-brief subset (`data/create/positive-control.yaml`): 8 `assert` briefs × 2 trials and 4 `walk` briefs × 1 trial, × 2 arms = 40 cells |
| arm A (`main`) | QualGent-MCP `8fb4ce7a605b` (origin/main), guide sha256 `7c7e70c1ad97…` |
| arm B (`harmful-rule`) | QualGent-MCP `21eb159b4bd6` on throwaway branch `throwaway/createbench-v2-harmful-rule`. The branch is pushed but has no PR and is never merged. Guide sha256 `0fb3e52cfe52…` |
| both arms | DevLoop-MCP `52a20c669d20` (template sha256 `ab98da61f8fd…`), `qualgent_tools: template` (8 tools), author and runner codex-cli · gpt-6-astra |
| the rule (arm B's only change) | One line added to the end of the guide's DO list: "End every test with a single Verify step that the current screen's title is visible." This is the same rule QUA-2859's scripted `harmful-rule` author encodes |
| corpus | `5e49ec8071b1`. The create readiness gate printed **READY** (`check_tier_ready.py --tier create --smoke`, for both arms' configs) before any spend |
| auth | API key on every episode. The driver printed `codex-cli auth: api_key`. A probe ran `codex login status` under a temporary CODEX_HOME and got "Logged in using an API key" (masked), and `provenance.agent_auth = api_key` on all 14 creation episodes. No `auth.json` was left in the runs dir |
| harness | qualgentbench-oss `a965f7c` (the epic head). Standalone `devloop-mcp --transport streamable-http --app-source none` on one emulator (`qgbench_root`, android-35), one lane. The driver takes a single `--device` |

## 1. What was run and why it stopped

- **Gate.** `check_tier_ready.py --tier create --config <arm cfg> --smoke --runs-dir ~/.qualgentbench/runs-qua2861` printed READY with each arm's config. Both arms passed `create-arm resolve` and the real-QualGent-MCP smoke against the fake API. The two guide hashes differ, which confirms the arms really are distinct surfaces.
- **Plan.** `run_create_ab.py run --plan` showed 40 cells, prediction `fcec04cefb3f`, $168 at measured actuals ($340 conservative), and `--max-cost` $280.
- **Run.** The A/B started at 17:39:40Z with `--max-cost 280 --yes`. The cap stayed at $280 throughout. A raise to $299 was relayed, but it was not applied by this worker and was not needed.
- **Stop.** After 10 cells the arm-B cases still read like arm A's: full outcome Verify steps and no trivial title check. The owner stopped the run. The driver was halted at a stage boundary. Cell `main.orgzly-open-note-from-notebook.t1` had finished authoring ($0.72), and its grade stage had begun staging but had not started any episode. The session's `stopped` field records the owner stop and its reason. The 26 untouched cells are still `pending`, and a resume would recover the in-flight cell from disk. This is not recommended, because the treatment is the problem (section 4).
- **Report.** `run_create_ab.py report` exits **4: INCOMPLETE**, "cells are still to run — no verdict before the last cell". The driver deliberately gives no partial verdict, and this doc does not construct one.

## 2. The board (`qualgent-bench show --mode create`, gated: READY)

| row | artifacts | Strong-Test | strong_exec | lint-clean | pass^3 | specificity | power (all `assert`) | cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| harmful-rule@21eb159 | 7 graded | 6/7 86% [49–97] | 7/7 100% [65–100] | 6/7 | 7/7 100% [65–100] | 7/7 100% [65–100] | 7/7 100% [65–100] | $39.02 ($5.57/artifact) |
| main@8fb4ce7 | 6 graded + 1 pending | pending (1 ungraded); 4/6 over graded | 6/6 100% [61–100] | 4/6 | 6/6 100% [61–100] | 6/6 100% [61–100] | 6/6 100% [61–100] | $35.10 (incl. the ungraded author) |

Brackets are 95% Wilson intervals. Every lint failure is `kind-tags` (main 2, harmful-rule 1), a HARD rule. No `content-anchors` failure occurred on these 13 cells. The walk group is empty: no walk cell ran, because cells interleave by brief and every graded brief is `assert`. **No reference baseline row exists.** The run was not given `--reference-trials`, so no reference case was graded.

Pre-registered read-out as the driver printed it. This is not a verdict, because the run is INCOMPLETE:

- Precondition, "arm A power ≥ 0.5 and ≥ 12 scored power cells per arm (assert)": **NOT MET**. Only 6 (A) and 7 (B) cells were scored.
- Expectation "power down (assert), one-sided Fisher p < 0.05": **NOT MET**. Arm A 6/6 vs arm B 7/7, p = 1.
- Repeatability flat (assert) and specificity flat (assert): MET, with intervals overlapping. All three walk expectations are INCONCLUSIVE because no walk cell was scored.

## 3. Per-cell results (13 graded + 1 authored)

Every grade ran 5 journey runs: clean ×3, target-only ×1 and control-only ×1. The step budget was 60–76.

| brief (group) | arm | author steps / Verify steps | final authored step (abridged) | follows rule? | pass^3 | spec | power (target) | $ author + grade |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| anki-open-card-from-browser (assert) | main | 14 / 1 | Verify Front and Back fields contain the values saved in this run | no | 3/3 | ✓ | caught, canary | 0.94 + 5.10 |
| anki-open-card-from-browser (assert) | harmful-rule | 14 / 1 | Verify "Edit note" displays Front/Back fields with the exact values entered | no | 3/3 | ✓ | caught, canary | 1.12 + 5.36 |
| cal-open-task-from-list (assert) | main | 14 / 2 | Verify the created task's title is struck through in the settled event list | no | 3/3 | ✓ | caught, canary | 1.07 + 4.70 |
| cal-open-task-from-list (assert) | harmful-rule | 14 / 2 | Verify the list shows the task with a struck-through title and a checked icon | no | 3/3 | ✓ | caught, canary | 0.91 + 5.10 |
| cal-edit-event (assert) | main | 14 / 1 | Verify the event row shows the new title at its original time, no entry under the old title | no | 3/3 | ✓ | caught, canary | 1.15 + 6.26 |
| cal-edit-event (assert) | harmful-rule | 14 / 2 | Verify the updated title is visible and the original title is absent | no (event title, not screen title) | 3/3 | ✓ | caught, canary | 1.14 + 5.51 |
| contacts-phone (assert) | main | 14 / 1 | Verify the details show both saved numbers plus Call/SMS controls | no | 3/3 | ✓ | caught, canary | 0.95 + 4.25 |
| contacts-phone (assert) | harmful-rule | 14 / 1 | Verify the details show the name, both numbers and the Call/SMS controls | no | 3/3 | ✓ | caught, canary | 0.94 + 4.35 |
| contacts-delete (assert) | main | 14 / 5 | Verify "No contacts found" is visible | no | 3/3 | ✓ | caught, canary | 0.98 + 4.38 |
| contacts-delete (assert) | harmful-rule | 14 / 5 | Verify "No contacts found" is visible | no | 3/3 | ✓ | caught, canary | 1.07 + 4.59 |
| medtimer-check-stock (assert) | main | 10 / 1 | Verify a numeric quantity is displayed under "Amount" | no | 3/3 | ✓ | caught, canary | 1.09 + 3.51 |
| medtimer-check-stock (assert) | harmful-rule | 11 / 2 | Verify the "Medicine stock settings" title is visible, **after** step 10 "Verify Amount displays a numeric quantity" | **literal yes, appended; outcome check kept** | 3/3 | ✓ | caught, canary | 0.88 + 4.30 |
| orgzly-open-note-from-notebook (assert) | harmful-rule | 10 / 3 | Verify the note title matches the title recorded from the outline | no (note title) | 3/3 | ✓ | caught, canary | 0.71 + 3.05 |
| orgzly-open-note-from-notebook (assert) | main | 10 / 3 | Verify the breadcrumb shows the recorded notebook and ancestor titles | no | not graded (owner stop) | | | 0.72 + 0 |

Per-brief deltas, arm B minus arm A: **0 on every axis for all 6 briefs graded in both arms.** Both arms scored strong_exec 1/1, pass^3 1/1, specificity 1/1 and power 1/1. Strong-Test differs only through `kind-tags` lint: arm A failed it on anki-open-card and cal-edit-event, and arm B failed it on medtimer-check-stock.

## 4. The uptake finding: the treatment was delivered but not taken up

Measured from every authored case (`authored_case.json`), its `creation_calls.json` and the author transcript:

| | arm A (main) | arm B (harmful-rule) |
| --- | --- | --- |
| authored cases | 7 | 7 |
| read `qualgent://test-case-guide` (`creation_calls.json` `resource_reads`) | 7/7 | 7/7 |
| the rule's sentence appears in the guide text the author received (transcript) | 0/7 (as it should be) | **7/7** |
| guide sha256 in `arm.json` | `7c7e70c1ad97…` on 7/7 | `0fb3e52cfe52…` on 7/7 |
| final step is a screen-title Verify | 0/7 | 1/7 (medtimer-check-stock) |
| **harmful uptake**: the title check replaces the outcome check | 0/7 | **0/7** |
| mean Verify steps per case | 2.0 | 2.3 |
| creation validity flags (`creation_flags`) | none | none |

So the manipulation reached the model: the right bytes, read on every cell. But the author's behaviour did not change in the direction the rule pushes. Every arm-B case still verifies the feature's outcome. The one literal compliance kept its real check and appended a title assertion after it.

**Product implication.** For codex-cli · gpt-6-astra under the `qualgent-test-creator` template, a single DO-list line in the QualGent-MCP test-case guide is a weak lever. The template's own verification guidance, plus the model's prior that a test should check an outcome, outweighed it. This cuts both ways for real prompt work. A guide edit alone may not move this author, so an A/B whose treatment is only a guide line needs its own uptake check before its power numbers mean anything.

**Why a MISSED would have been uninformative.** The registration has no manipulation check. Its expectations are judged only on the grade axes, so "the benchmark could not see a harmful change" and "the harmful change never happened" produce the same MISSED. With 0/7 uptake, the two arms were in effect the same arm twice. MISSED would have been the expected result even for a perfectly sensitive benchmark, and spending the remaining ~$150 would not have told the two explanations apart.

**A second design note for the follow-up.** On this subset, even full literal compliance would not remove power everywhere. Four of the 8 `assert` targets are navigation faults: the wrong screen opens. A "current screen's title is visible" check that quotes the intended screen's title still catches a wrong-screen navigation. Arm B's one literal compliance shows this: on `medtimer-check-stock` (target `stock-button-opens-settings`) it asserted the stock screen's own title, which a wrong destination would fail. QUA-2859's scripted harmful-rule author asserts a trivially-true title, but a live author tends to quote the title of the screen it expects. The follow-up should make the treatment unambiguous, or should judge uptake on what the final check can actually observe.

## 5. Cost and time against the estimate

| | estimate (plan) | actual |
| --- | --- | --- |
| total | $168 for 40 cells (≤ $280 cap) | **$74.12** for 13 graded + 1 authored (author $13.66, grade $60.46, abandoned $0.00) |
| author per cell | $0.95 | $1.00 mean (13 graded cells), range $0.71–1.15 |
| grade per cell | $3.25 | **$4.65** mean, range $3.05–6.26 (~$0.93 per runner episode) |
| per cell | $4.20 | $5.65 mean, so a full 40-cell run would project to about $226 |
| wall time | not printed by the plan | 2 h 02 min elapsed (17:39:40Z–19:41:55Z) for ~13.5 cells, about 9.3 min per cell (author 1.3–2.7 min, grade 5.2–9.4 min). Agent time 1.29 h. A full run would project to about 6.2 h on one lane |

Grading costs about 1.4× the estimate, because runner episodes on these briefs run about 20–27 steps. The $280 cap would still have held for a full run.

## 6. Exclusions, attribution and runner noise

- Excluded runs: **0/65** runner episodes. Unattributed target FAILs: **0**. Faulted cells: 0. Contamination risk (copy of the reference): 0 in each arm. App crashes in runner episodes: 0. False reports: 0.
- Every target-only run (13/13) FAILED and was attributed by the target's `fired()` canary, with the report also matched. Every control-only run (13/13) PASSED with its control fault firing (`control_fired`), so specificity is a real pass on a perturbed build.
- **Runner-noise estimate:** 0/39 clean runs failed. A clean-run false-fail rate of 0% has a Wilson 95% upper bound of 9.0%. Counting control runs too, 0/52 non-target runs failed, with an upper bound of 6.9%. At n = 13 artifacts this is an upper bound, not a measured rate.

## 7. Known limitations

- **The owner ran this without a human review of the briefs** (QUA-2853, owner decision 2026-10-01). The neutrality lint and the adversary gate are automated checks only.
- **There is no manipulation check in the registration** (section 4). This is the reason the run was stopped.
- **The walk group is thin:** n = 4 briefs × 1 trial per arm, and no walk cell ran before the stop.
- **Controls were derived on pre-canary APKs, relations only** (QUA-2854 part 2). They were not re-derived on the canary APKs this run graded on. All 13 control-only runs passed with the control fault firing, but this is not a re-derivation.
- **The lint `content-anchors` decision is still open** (QUA-2858). It did not fire on these 13 cells, but Strong-Test and strong_exec are both shown until the owner decides.
- **No reference baseline row.** The run had no `--reference-trials`.
- One lane and one emulator, so there is no lane-parity comparison.
- **Bench viewer: not published.** The run is an interim, owner-stopped A/B spread over 15 per-stage run ids. The `bench_viewer publish` path rebuilds whole runs with `view --portable` and is built around one run with a board. Publishing a half-run as if it were a finished board would mislead, so publishing is deferred to the re-run.

## 8. What happens next

1. A follow-up ticket fixes the treatment so the authors actually take it up. Options include phrasing the rule unambiguously (for example as a replacement for the outcome check rather than an extra DO line), placing it where the template's own guidance does not override it, or both. The fix should be measured on a few creation-only episodes, which are cheap (about $1 each, no grading), until uptake is high.
2. Register a manipulation check: uptake measured on the authored artifacts as a precondition, so that low uptake reads INCONCLUSIVE and never MISSED.
3. Re-run QUA-2861 as a new experiment (new arm-B SHA), under the same $280 cap.

Reproduce the read-out:

```bash
uv run python scripts/run_create_ab.py report --experiment qua2861-harmful-rule-positive-control \
  --runs-dir ~/.qualgentbench/runs-qua2861          # exit 4 = INCOMPLETE
uv run qualgent-bench show --mode create --runs-dir ~/.qualgentbench/runs-qua2861
```
