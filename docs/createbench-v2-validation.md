# CreateBench v2 positive-control validation (QUA-2861, 2026-10-01/02)

**Verdict: DETECTED (driver exit 0). GO/NO-GO: conditional GO** for prompt and
tool-surface A/Bs. The conditions are in section 9.

The re-run used the v2 registration `harmful-rule-positive-control-mechanism/v2` and ran all 40 cells.

**What arm B changed.** Arm B's `qualgent-test-creator` template carried the `app-open/v2` rule. Under that rule, a case drops every outcome check, ends on "Verify the app is still open", and sets its expected result to "The app is still open."

**Uptake.** Arm B took the rule on **20/20** authored cases. Arm A took it on 0/20.

**Power on the DROP group** (4 persistence briefs × 4 trials):

| arm | power | Wilson 95% |
| --- | --- | --- |
| A | 16/16 | [81–100] |
| B | **0/16** | [0–19] |

The one-sided Fisher exact p is 1.66e-09. Arm B fell below arm A on **4/4 DROP briefs**.

**Repeatability and specificity** stayed flat on both groups. Every pre-registered precondition and expectation read MET.

**Spend:** $225.61 against a $240 cap. The plan's estimate was $226.00.

**Read the DETECTED narrowly.** It rests on 4 DROP briefs. At brief level, 4/4 in the predicted direction is a sign-test p of 1/16 = 0.0625. The cell-level Fisher p treats 16 correlated trials per arm as independent, so it overstates the evidence.

**The FLAT group's premise was only partly right.** The FLAT expectation (walk targets keep their power) was MET by the registered Wilson-overlap rule:

| arm | power on walk targets | Wilson 95% |
| --- | --- | --- |
| A | 4/4 | [51–100] |
| B | 2/4 | [15–85] |

But arm B lost walk power on 2 of the 4 walk briefs:

- `anki-study-first-card`: the app crashed and came back to its deck list.
- `medtimer-analysis-tabular-view`: the screen froze but the app stayed visible.

In both cases the only check, "Verify the app is still open", held. So the runner returned PASS on a build whose target defect fired.

The n = 4 FLAT test cannot detect a 50% drop. It is not confirmation of the mechanism, and section 4 explains it in full.

| | |
| --- | --- |
| experiment | `qua2861-rerun-app-open-positive-control`, state run id `20261001-210030-1231`, runs dir `~/.qualgentbench/runs-qua2861-rerun` (a new directory outside every repo) |
| prediction | `harmful-rule-positive-control-mechanism/v2`, sha `6aa0adc7c13d` (QUA-2864; CLI default). Frozen at registration, unchanged |
| design | subset `data/create/positive-control-v2.yaml`. DROP (`assert`, all persistence) = cal-edit-event, contacts-phone, contacts-delete, tasks-complete-parent × 4 trials. FLAT (`walk`) = anki-study-first-card (crash), medtimer-take-dose-then-medicine-list (ANR), medtimer-analysis-tabular-view (stuck), tasks-add-subtask (ordering → crash) × 1 trial. × 2 arms = **40 cells** |
| arm A (`main`) | QualGent-MCP `8fb4ce7a605b` (main) + DevLoop-MCP `52a20c669d20` (template sha256 `ab98da61f8fd…`) |
| arm B (`app-open-rule`) | QualGent-MCP `8fb4ce7a605b` + DevLoop-MCP `790f6b46e333` on throwaway branch `throwaway/createbench-v2-uptake-template` (template sha256 `67a2d383e9c3…`). The branch is pushed, but it has no PR and is never merged |
| what differs | exactly one template line: the rule replaces the template's Step 6 final-verification bullet (QUA-2864). The QualGent-MCP guide is identical in both arms: sha256 `7c7e70c1ad97…` |
| both arms | `qualgent_tools: template` (8 tools). Author and runner are both codex-cli · gpt-6-astra. Creation brief v1. Runner: journey brief v3 (`64a1619ad382`), grader v2 |
| corpus | `5e49ec8071b1`. `check_tier_ready.py --tier create --smoke` printed **READY** for both arm configs, on both the default subset and `--briefs subset-v2`, before any spend |
| auth | API key throughout. The session recorded `agent_auth: api_key` and `allow_codex_login: false`. `provenance.agent_auth = api_key` on 40/40 creation episodes and on every runner episode checked. No `auth.json` is in the runs dir |
| harness | qualgentbench-oss `9b3401f` (the epic head after QUA-2864). Standalone `devloop-mcp --transport streamable-http --port 51871 --app-source none`, from the DevLoop `52a20c6` worktree. The server code is the same for both arms, because each arm's template comes from its own pinned ref through the driver's arm spec. One emulator (`qgbench_root`, android-35), one lane |

## 1. History: the first attempt (stopped, INCOMPLETE)

**The run.** The first attempt was experiment `qua2861-harmful-rule-positive-control`, in runs dir `~/.qualgentbench/runs-qua2861`, registered under `mechanism/v1`.

- **Arm B's treatment** was the QualGent-MCP throwaway `21eb159b4bd6`. It added one line to the guide's DO list: "End every test with a single Verify step that the current screen's title is visible."
- **The stop.** The owner stopped the run at 13/40 graded cells ($74.12 spent; the driver exited 4, INCOMPLETE).
- **Why it was stopped.**
  - The treatment reached the author but was not taken up. Arm B read the guide and received the rule on 7/7 cells, yet replaced its outcome check on **0/7**.
  - The v1 registration had no manipulation check, so a no-op treatment and an insensitive benchmark would both have read MISSED. Power was 7/7 (B) against 6/6 (A).
  - 4 of v1's 8 DROP targets were navigation faults, and a title check that quotes the intended screen still catches those.

**The fix (QUA-2864).**

- The rule changed to `app-open/v2`, placed in the template, which gave 9/9 uptake in a creation-only probe.
- The DROP group was re-selected to the persistence targets that this rule provably cannot catch.
- v2 adds a registered uptake precondition: arm B's DROP uptake must be at least 0.8, otherwise the verdict is INCONCLUSIVE.

None of the first attempt's cells are reused here. This is a fresh experiment in a new runs dir, because the driver has no import step.

## 2. Staged uptake check, then the full run

The owner asked for a staged run: check arm-B uptake before paying for most of the grading.

The driver already orders cells by trial, then by brief, with the two arms of each (brief, trial) back to back. That places arm B's four trial-1 DROP authorings at cells 1, 4, 5 and 8.

The stop rule was to halt at the second arm-B DROP case that did not take the rule (fewer than 3/4). Each authored case was classified with `create/uptake.py` as it landed. Nothing was pre-authored, so nothing was paid for twice.

- **Uptake check: passed.** The first three arm-B DROP authorings (cells 1, 4 and 5) took the rule, so 3/4 was guaranteed. The 4th took it as well. The run continued, and $25.30 had been spent at that point.
- **Owner's conditional stop on FLAT.** The owner decided to stop early if the FLAT expectation was already NOT MET once every FLAT cell was final.
  - `plan_cells` puts all 8 FLAT cells at positions 9–16 (trial 1 only). After cell 16 the driver's own `run_create_ab.py report` read:
    - `[MET] power flat (pooled, walk targets) — intervals overlap` (A 4/4 [51–100], B 2/4 [15–85]);
    - repeatability and specificity flat on the walk group, both MET.
  - B had already reached 2/4, so no remaining cell could flip the result, and the run continued to completion as registered.
- **The run.** It started 2026-10-01 21:00:30Z with `--max-cost 240 --yes` and ended 2026-10-02 02:41:48Z.
  - 40/40 cells were graded: 0 faulted, 0 skipped and 0 excluded runs.
  - The session ended without a stop.
  - The cap was never raised.

## 3. The read-out (`run_create_ab.py report`, exit 0)

```
pooled, A vs B (k/n, Wilson 95%):
  power         20/20 100% [84–100]    2/20 10% [3–30]
  repeatability 20/20 100% [84–100]    20/20 100% [84–100]
  specificity   19/20 95% [76–99]      19/20 95% [76–99]
  assert group:
    power       16/16 100% [81–100]    0/16 0% [0–19]
    repeatability 16/16 100% [81–100]    16/16 100% [81–100]
    specificity 15/16 94% [72–99]      15/16 94% [72–99]
  walk group:
    power       4/4 100% [51–100]      2/4 50% [15–85]
    repeatability 4/4 100% [51–100]      4/4 100% [51–100]
    specificity 4/4 100% [51–100]      4/4 100% [51–100]

uptake of app-open/v2 (authored cases that take the rule, k/n):
  arm main          all 0/20 · assert 0/16 · walk 0/4
  arm app-open-rule all 20/20 · assert 16/16 · walk 4/4

power per brief, assert group — B below A on 4/4 brief(s):
  cal-edit-event         A 4/4   B 0/4
  contacts-phone         A 4/4   B 0/4
  contacts-delete        A 4/4   B 0/4
  tasks-complete-parent  A 4/4   B 0/4

pre-registered preconditions:
  [MET] arm B uptake of app-open/v2 >= 0.8 (assert targets)
  [MET] arm A power >= 0.5 and >= 12 scored power cells per arm (assert targets)
pre-registered expectations:
  [MET] power down (pooled, assert targets) [one-sided Fisher exact p < 0.05] — p = 1.66e-09
  [MET] power flat (pooled, walk targets) — intervals overlap
  [MET] repeatability flat (pooled, assert targets) — intervals overlap
  [MET] repeatability flat (pooled, walk targets) — intervals overlap
  [MET] specificity flat (pooled, assert targets) — intervals overlap
  [MET] specificity flat (pooled, walk targets) — intervals overlap

VERDICT: DETECTED — every pre-registered expectation met (exit 0)
```

### The board (`qualgent-bench show --mode create`)

| row | artifacts | Strong-Test | strong_exec | lint-clean | pass^3 | specificity | power | power assert | power walk | uptake (app-open/v2) | cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| app-open-rule@8fb4ce7 | 20 graded | 1/20 5% [1–24] | 2/20 10% [3–30] | 14/20 | 20/20 [84–100] | 19/20 [76–99] | 2/20 [3–30] | 0/16 [0–19] | 2/4 [15–85] | 20/20 | $109.70 ($5.49/artifact) |
| main@8fb4ce7 | 20 graded | 13/20 65% [43–82] | 19/20 95% [76–99] | 14/20 | 20/20 [84–100] | 19/20 [76–99] | 20/20 [84–100] | 16/16 [81–100] | 4/4 [51–100] | 0/20 | $115.91 ($5.80/artifact) |

**Lint.** Lint HARD failures:

- `main`: `kind-tags` 6.
- `app-open-rule`: `kind-tags` 5 and `content-anchors` 1.

The owner has not yet decided the `content-anchors` question (QUA-2858), so the board shows both Strong-Test and strong_exec.

**No reference baseline row.** The run had no `--reference-trials`.

**Arm A's strong_exec 19/20.** Its one miss is tasks-complete-parent t2, which lost specificity (section 6).

### Per-brief deltas (B minus A)

| brief | group | target (class) | power A → B | Δ power | Δ pass^3 | Δ specificity |
| --- | --- | --- | --- | --- | --- | --- |
| cal-edit-event | assert | edit-event-not-saved (persistence) | 4/4 → 0/4 | **−1.00** | 0 | 0 |
| contacts-phone | assert | phone-number-dropped (persistence) | 4/4 → 0/4 | **−1.00** | 0 | 0 |
| contacts-delete | assert | contact-delete-broken (persistence) | 4/4 → 0/4 | **−1.00** | 0 | 0 |
| tasks-complete-parent | assert | subtasks-left-open (persistence) | 4/4 → 0/4 | **−1.00** | 0 | 0 (3/4 both) |
| anki-study-first-card | walk | reviewer-show-answer-crash (crash) | 1/1 → 0/1 | **−1** | 0 | 0 |
| medtimer-take-dose-then-medicine-list | walk | overview-action-blocks-main-thread (ANR) | 1/1 → 1/1 | 0 | 0 | 0 |
| medtimer-analysis-tabular-view | walk | analysis-table-freezes-on-open (stuck) | 1/1 → 0/1 | **−1** | 0 | 0 |
| tasks-add-subtask | walk | subtask-filed-before-written (ordering → crash) | 1/1 → 1/1 | 0 | 0 | 0 |

### Per-cell results

Every grade ran 5 journey runs: clean ×3, target-only ×1 and control-only ×1.

- "takes rule" is `uptake.classify_artifact(…, "app-open/v2")`.
- The power column gives the target run's verdict and whether the runner's report matched the target (`bugs_found`).
- "crash" means the target run recorded an app crash.

| brief (group) | trial | arm | steps / tagged verify | final authored step (abridged) | takes rule | pass^3 | spec | power (target verdict; report) | $ author + grade |
|---|---|---|---|---|---|---|---|---|---|
| cal-edit-event (assert) | t1 | main | 14 / 2 | Verify the created event appears under its revised title and its original title… | no | 3/3 | ✓ | caught (fail; matched) | 1.23 + 5.92 |
| cal-edit-event (assert) | t1 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.13 + 5.50 |
| contacts-phone (assert) | t1 | main | 14 / 1 | Verify the contact details show both saved numbers… | no | 3/3 | ✓ | caught (fail; matched) | 0.92 + 4.38 |
| contacts-phone (assert) | t1 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 0.90 + 4.31 |
| contacts-delete (assert) | t1 | main | 14 / 5 | Verify "No contacts found" is visible | no | 3/3 | ✓ | caught (fail; matched) | 1.06 + 5.40 |
| contacts-delete (assert) | t1 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 1.02 + 3.90 |
| tasks-complete-parent (assert) | t1 | main | 14 / 1 | Verify the newly created parent and both subtasks have checked checkboxes… | no | 3/3 | ✓ | caught (fail; matched) | 1.30 + 4.52 |
| tasks-complete-parent (assert) | t1 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.04 + 4.44 |
| anki-study-first-card (walk) | t1 | main | 12 / 4 | Verify a different card question is visible with its answer hidden… | no | 3/3 | ✓ | caught (fail; matched; crash) | 0.80 + 2.97 |
| anki-study-first-card (walk) | t1 | app-open-rule | 9 / 1 | Verify the app is still open | yes | 3/3 | ✓ | **missed (pass; matched; crash)** | 0.70 + 2.99 |
| medtimer-take-dose-then-medicine-list (walk) | t1 | main | 14 / 0 | Verify the history entry matching the answered dose… | no | 3/3 | ✓ | caught (fail; matched; crash) | 1.20 + 3.96 |
| medtimer-take-dose-then-medicine-list (walk) | t1 | app-open-rule | 9 / 1 | Verify the app is still open | yes | 3/3 | ✓ | caught (fail; matched; crash) | 0.73 + 3.36 |
| medtimer-analysis-tabular-view (walk) | t1 | main | 14 / 1 | Verify the newly recorded dose event has its own row… | no | 3/3 | ✓ | caught (fail; matched; crash) | 1.39 + 5.29 |
| medtimer-analysis-tabular-view (walk) | t1 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | **missed (pass; —)** | 1.64 + 5.40 |
| tasks-add-subtask (walk) | t1 | main | 12 / 0 | Verify both subtasks appear indented beneath the new parent… | no | 3/3 | ✓ | caught (fail; matched; crash) | 0.98 + 4.13 |
| tasks-add-subtask (walk) | t1 | app-open-rule | 12 / 0 | Verify the app is still open | yes | 3/3 | ✓ | caught (fail; matched; crash) | 1.10 + 3.90 |
| cal-edit-event (assert) | t2 | main | 14 / 2 | Verify the created event appears under its updated title… | no | 3/3 | ✓ | caught (fail; matched) | 1.25 + 5.96 |
| cal-edit-event (assert) | t2 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.00 + 5.52 |
| contacts-phone (assert) | t2 | main | 14 / 1 | Verify the contact details show numbers matching… | no | 3/3 | ✓ | caught (fail; matched) | 1.01 + 4.41 |
| contacts-phone (assert) | t2 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 0.90 + 4.22 |
| contacts-delete (assert) | t2 | main | 14 / 5 | Verify "No contacts found" is visible | no | 3/3 | ✓ | caught (fail; matched) | 1.02 + 4.06 |
| contacts-delete (assert) | t2 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 0.93 + 4.10 |
| tasks-complete-parent (assert) | t2 | main | 14 / 3 | Verify the new parent and both subtasks appear under "Completed"… | no | 3/3 | ✗ | caught (fail; —) | 1.29 + 4.20 |
| tasks-complete-parent (assert) | t2 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✗ | missed (pass; —) | 1.29 + 4.03 |
| cal-edit-event (assert) | t3 | main | 14 / 0 | Verify the created event appears under its edited title, old title gone | no | 3/3 | ✓ | caught (fail; matched) | 1.15 + 6.03 |
| cal-edit-event (assert) | t3 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.18 + 5.48 |
| contacts-phone (assert) | t3 | main | 14 / 0 | Verify the contact details show both saved numbers… | no | 3/3 | ✓ | caught (fail; matched) | 0.96 + 4.32 |
| contacts-phone (assert) | t3 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 0.83 + 4.31 |
| contacts-delete (assert) | t3 | main | 14 / 5 | Verify "No contacts found" is visible | no | 3/3 | ✓ | caught (fail; matched) | 1.05 + 4.78 |
| contacts-delete (assert) | t3 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.10 + 4.54 |
| tasks-complete-parent (assert) | t3 | main | 14 / 1 | Verify the new parent and both subtasks appear in "Completed"… | no | 3/3 | ✓ | caught (fail; matched) | 0.94 + 4.58 |
| tasks-complete-parent (assert) | t3 | app-open-rule | 14 / 0 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 0.98 + 4.48 |
| cal-edit-event (assert) | t4 | main | 14 / 1 | Verify the created event appears with its updated title… | no | 3/3 | ✓ | caught (fail; —) | 1.06 + 5.16 |
| cal-edit-event (assert) | t4 | app-open-rule | 14 / 0 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.24 + 5.56 |
| contacts-phone (assert) | t4 | main | 14 / 0 | Verify the contact details show both entered phone numbers… | no | 3/3 | ✓ | caught (fail; matched) | 0.91 + 4.41 |
| contacts-phone (assert) | t4 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 0.86 + 4.27 |
| contacts-delete (assert) | t4 | main | 14 / 5 | Verify "No contacts found" is visible | no | 3/3 | ✓ | caught (fail; matched) | 1.00 + 5.05 |
| contacts-delete (assert) | t4 | app-open-rule | 14 / 0 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; matched) | 0.97 + 4.36 |
| tasks-complete-parent (assert) | t4 | main | 14 / 0 | Verify the new parent and both subtasks appear under "Completed"… | no | 3/3 | ✓ | caught (fail; matched) | 1.06 + 4.76 |
| tasks-complete-parent (assert) | t4 | app-open-rule | 14 / 1 | Verify the app is still open | yes | 3/3 | ✓ | missed (pass; —) | 1.18 + 4.31 |

"tagged verify" counts steps carrying the `verify` kind tag. Some cases wrote untagged
"Verify …" steps; that is the `kind-tags` lint failure on the board. The uptake classifier
counts untagged assertion steps as checks too.

## 4. The FLAT group: MET by the rule, premise partly wrong

QUA-2864 kept the walk targets as the FLAT group on this argument: a crash, ANR or stuck target kills or freezes the app on the route, so "the app is still open" also fails there, and power holds.

That held on 2 of the 4 walk briefs and failed on the other 2.

| walk brief (class) | arm-B target run | why |
| --- | --- | --- |
| medtimer-take-dose-then-medicine-list (ANR) | FAIL, caught | the ANR blocked the route and the runner reported it |
| tasks-add-subtask (ordering → crash) | FAIL, caught | Save crashed the app and the run ended on the launcher, so "app still open" was false |
| anki-study-first-card (crash) | **PASS, missed** | after "Show answer" crashed the reviewer, the app came back to its deck list. The runner reported the bug (matched `reviewer-show-answer-crash`) and wrote "The app remained open", then returned PASS because the case's only check held |
| medtimer-analysis-tabular-view (stuck) | **PASS, missed** | the table view froze, but MedTimer stayed visible on its charts. The runner noted that "no dose table appeared" (an unmatched report, counted as a false report), then returned PASS because the app was still open |

**Two mechanisms leak.**

- A crash the app recovers from: it is relaunched or returns to a parent screen.
- A stuck screen that still shows the app.

In both, the app-open check holds and the runner's verdict is PASS, even when it reported the bug.

**What the FLAT result does and does not show.** The registered FLAT test is a Wilson-overlap check at n = 4 per arm. 4/4 against 2/4 overlaps ([51–100] vs [15–85]), so it reads MET. The test cannot detect a 50% drop. So the FLAT MET is not evidence that the walk targets were insensitive to the rule, and this report does not present it as confirmation of the mechanism.

**What the DETECTED does rest on.** The DROP group, where uptake was 16/16 and power fell 16/16 → 0/16 on all 4 briefs.

## 5. Uptake per arm

| | arm A (main) | arm B (app-open-rule) |
| --- | --- | --- |
| authored cases | 20 | 20 |
| take app-open/v2: DROP / walk | 0/16 / 0/4 | **16/16** / 4/4 |
| case's last step is the app-open check | 0/20 | 20/20 |
| expected_result is the rule's | 0/20 | 20/20 |
| creation validity flags | none | none |
| `provenance.agent_auth` | api_key 20/20 | api_key 20/20 |

Uptake was judged by the deterministic classifier, `create/uptake.py` (CLASSIFIER_VERSION 1), as each cell landed. The same classifier is recorded in every v2 grade manifest as `cell.uptake`.

## 6. Exclusions, attribution, runner noise and report-level leaks

**Exclusions and attribution.**

- 0/200 runner episodes were excluded, and 0 cells faulted.
- 0 target FAILs were unattributed: every caught target was attributed by its canary.
- There were 0 contamination flags and 0 copies of the reference case.

**Runner noise.**

- Clean runs: 0/120 failed. A 0% false-fail rate has a Wilson 95% upper bound of 3.1%.
- Control-only runs: 2/40 failed. Both are the same brief and trial in both arms, covered below.
- False reports (a report that matches no bug on the build): 16 over 200 episodes, 6 in arm A and 10 in arm B. They do not change any verdict.

**tasks-complete-parent t2 specificity is a scoring artifact, not a specificity failure.**

**1. Which control ran.** Trial `t` uses `create_controls[(t − 1) mod 3]`, and the list is [due-section-shifted, subtask-filed-before-written, repeat-complete-crash]. So t2 ran `subtask-filed-before-written` (index 1), and t3 and t4 used the other two controls.

**2. Why the derivation called it eligible.** The derivation replays the **reference** route, which completes the fixture's existing parent task. That route never reaches the control: it holds 3/3 with `fired: []`.

**3. Why the authored routes reached it.**

- Both authored t2 cases (arm A and arm B) **create** a new parent and add two subtasks before Save.
- That is exactly where the control's fault sits.
- In both control-only runs:
  - the control's canary fired;
  - the app crashed (`app_crashes = 1`), with the runner's report at step 10, right after Save. In arm B's run, the episode ended on the launcher;
  - the runner reported FAIL, with the control's bug matched in `bugs_found`.

**4. What that means.** The case correctly caught a bug that was really present on its route, and the grader charged it to specificity.

**5. Impact.** Specificity was 19/20 vs 19/20 with these cells, and 19/19 vs 19/19 without them. Assert-group specificity was 15/16 vs 15/16 with them, and 15/15 vs 15/15 without. They are symmetric across the arms, so the A-vs-B comparison is unaffected either way.

**The control fault fired on only 15/40 control-only runs** (A 8/20, B 7/20). The interim run had seen it fire on 13/13.

On the other 25 runs, the authored route did not pass through the control's code path. Their specificity PASS is therefore a pass on a build whose perturbation was never exercised, which is close to a fourth clean run. This is the known limitation that controls were derived on pre-canary APKs, on the reference route, and for relations only. It is symmetric across arms.

**Report-level leaks.** In 8 arm-B target runs, verdict PASS came with a runner report that matched the target:

| brief | trials with a matched report |
| --- | --- |
| contacts-phone | t1, t2, t3, t4 |
| contacts-delete | t1, t2, t4 |
| anki-study-first-card | t1 |

The runner saw the symptom on the route and wrote it up, but kept verdict PASS because the case's expected outcome ("the app is still open") held.

Six more arm-B misses noted the symptom, but the matcher did not credit it, so it counted as a false report:

- cal-edit-event, all 4 trials (for example, the day view "still displayed the original title"), as in the QUA-2864 probe;
- contacts-delete t3;
- medtimer-analysis-tabular-view t1.

Most of arm B's 10 "false reports" are therefore real symptoms that the matcher missed. Only tasks-complete-parent's arm-B target runs never mentioned the symptom.

Power is scored on the verdict, so these count as lost power, as designed. Any future axis that credits a report without a FAIL would reopen these leaks on 3 of the 4 DROP briefs.

## 7. Cost and time against the estimate

| | estimate (plan) | actual |
| --- | --- | --- |
| total | $226.00 for 40 cells (measured actuals: $1.00 author + $4.65 grade); cap $240 | **$225.61**: author $42.32, grade $183.29, abandoned $0.00 |
| author per cell | $1.00 | $1.06 mean, range $0.70–1.64 |
| grade per cell | $4.65 | $4.58 mean, range $2.97–6.03 (5 runner episodes each) |
| arm totals | — | A $115.91 ($5.80/cell), B $109.70 ($5.49/cell). B's cases are shorter, so its runs end sooner |
| wall time | about 6.2 h on one lane (QUA-2864 projection) | **5 h 41 min** (21:00:30Z–02:41:48Z), 8.5 min per cell; agent time 3.62 h; author mean 67 s |

The staged uptake check cost nothing extra: it read the cells the driver was going to grade anyway.

## 8. Known limitations

- **Clustering: four DROP briefs.** 16 DROP cells per arm come from only 4 briefs. Trials of one brief are correlated: same app, same target, same author habits. So the registered cell-level one-sided Fisher p of 1.66e-09 overstates the evidence. At brief level, 4/4 briefs moved in the predicted direction. The best a 4-brief design can reach is a sign-test p of 1/16 = **0.0625**, which is above 0.05 whatever the data. The registered test was not changed after the fact.
- **Narrow DROP spread.** All 4 DROP briefs are persistence targets, across 3 apps (calendar 1, contacts 2, tasks.org 1). The DETECTED says the benchmark sees an author who stops verifying saved state. It says nothing direct about other defect classes.
- **The walk group is thin and its premise partly failed.** It has n = 4 per arm. A 50% walk-power drop on arm B still read FLAT (section 4).
- **No owner review of the briefs.** The owner chose to run without a human review of the briefs (QUA-2853). The neutrality lint and the adversary gate are automated checks only.
- **Controls.** They were ranked on pre-canary APKs, on reference routes, and for relations only. The control fired on only 15/40 control runs, and one control (tasks-complete-parent t2) is reachable on authored routes (section 6).
- **Lint.** The `content-anchors` decision is still open (QUA-2858), so the board shows both Strong-Test and strong_exec.
- **Setup.** There was no reference baseline row, one lane on one emulator, and one author/runner model (codex-cli · gpt-6-astra).
- **Bench viewer: not published.** The `bench_viewer publish` path publishes runs that have a `board.json`. In this experiment:
  - each of the 40 creation episodes is its own one-episode run with a board;
  - the 200 grade episodes sit under the state run id `20261001-210030-1231`, which has no board.

  Publishing would put 40 one-episode creation runs on the front page and leave every grade episode off. That is not straightforward and would mislead, so it was not done. A create-mode publish path (one page per experiment, with its board and grade manifests) is a follow-up candidate.

## 9. GO/NO-GO: conditional GO

**Why not a full GO.** The driver's verdict is DETECTED. The benchmark saw a known-harmful author change on every DROP brief, with full uptake, with clean runners and flat repeatability. That is what the positive control was built to show, so this is not a NO-GO. But the evidence is 4 briefs of one defect class, and the FLAT premise partly failed. That makes it a **conditional GO for prompt and tool-surface A/Bs.**

**Conditions:**

1. **Brief level, not cell level.** A real A/B's claim must hold at brief level. That means enough briefs per stratum that a brief-level test can reach significance: at least 6 briefs that all move gives a sign-test p ≤ 0.016. Read the cell-level Fisher p as secondary. Prefer more briefs to more trials.
2. **Uptake first.** Every real A/B registers or reports an uptake or manipulation measure for its treatment. QUA-2861's first attempt showed that a guide line can be read and ignored, and that would read as a null.
3. **Persistence and assert targets carry the inference.** Power on walk targets (crash, ANR, stuck) is not interpretable for any treatment that changes how a case ends. It stays so until the runner-contract follow-up below lands. A real A/B should report walk power separately, and not pool it into its headline.
4. **Specificity read with care.** The control-only scoring artifact (below) is fixed, or controls whose canary fired are discounted by hand in the read-out. The low control-fire rate (15/40) is stated beside any specificity claim.
5. **Owner sign-off.** The open owner decisions (the brief review, `content-anchors`) are recorded before a real A/B's result is used to ship a prompt change.

## 10. Follow-up candidates (not built here)

- **Runner contract on observed crashes, ANRs and freezes.**
  - **The problem.** When the runner itself observes the app crash, ANR or freeze during the case, the verdict is PASS whenever the authored expected outcome still holds. That is the walk-group leak in section 4 and the report-level leaks in section 6.
  - **Option 1:** make the runner's verdict FAIL whenever it observed a crash, ANR or freeze on the route, whatever the expected result. This is a runner-contract change.
  - **Option 2:** choose a harmful rule whose effect on crash, ANR and stuck targets does not depend on how the runner treats a recovery.
  - **Done in QUA-2865 (grader v3)**, as a harness-side rule rather than a brief change, plus a separate report-credited axis. Section 11 has the offline re-score.
- **Grader: a control-only run whose control canary fired is not a valid no-op.** Exclude it, or re-draw the next eligible control, rather than scoring specificity False.
- **Control eligibility is derived on reference routes, not authored routes.** Derive or re-check it on the authored route, or at least flag a control whose canary fires on an authored control run. The control-fire rate (15/40 here) should be reported on the board.
- **Driver: import finished cells unchanged from another experiment** (QUA-2864's proposal; not needed here).
- **A create-mode bench-viewer publish path** (section 8).

## 11. Re-score under grader v3 (QUA-2865, offline)

Grader v3 changes two things. Neither touches the journey brief or `BRIEF_VERSION`, so the runner saw exactly what it saw here.

- **Observed death = FAIL.** A run during which the harness recorded the app's own crash or ANR (`metrics.app_crashes` > 0) is a FAIL whatever the runner wrote. This is condition 3's runner-contract follow-up, judged from the harness's crash record instead of the runner's judgement, so it rescores offline.
- **`power_report`**, a separate axis that is never in Strong-Test. It credits power, or a target run whose report matched the target with the canary fired, on any verdict.

Every manifest here is still recorded as v2. `grader rescore` with no `--grader-version` reproduced **40/40**. So did the 27 grades in runs-qua2857, runs-qua2861 and runs-qua2864. The episodes and manifests were hashed before and after the re-score, and nothing was written.

| arm | group | power v2 → v3 | power_report (v3) |
| --- | --- | --- | --- |
| A (`main`) | assert (DROP) | 16/16 → 16/16 | 16/16 |
| A (`main`) | walk (FLAT) | 4/4 → 4/4 | 4/4 |
| B (`app-open-rule`) | assert (DROP) | 0/16 → 0/16 | **7/16** |
| B (`app-open-rule`) | walk (FLAT) | 2/4 → **3/4** | 3/4 |

What moved:

- **anki-study-first-card, arm B.** The crash-then-relaunch run (verdict PASS, `app_crashes` 1, canary fired) is now caught. That cell's power, strong_exec and Strong-Test go from False to True.
- **medtimer-analysis-tabular-view, arm B.** Still missed. The freeze left nothing pending, so no ANR was recorded, and the runner's note matched nothing. Only a post-run liveness probe would see it; that is device-side and left as a follow-up (a TODO in `grader.app_died`).
- **Nothing else.** No clean run recorded a death, so repeatability did not move on either arm (20/20). The two tasks-complete-parent t2 control runs that crashed were already FAIL.
- **The DROP comparison is unchanged** (16/16 vs 0/16), so the DETECTED verdict stands under v3.

`power_report` credits arm B's 7 report-level leaks on the DROP group (contacts-phone ×4, contacts-delete ×3). That is section 6's warning, measured: a case whose only check is "the app is still open" earns report credit on every target the runner happens to notice. It is a diagnostic beside `power`, never a substitute for it.

```bash
uv run python -m qualgentbench.create.grader rescore \
  ~/.qualgentbench/runs-qua2861-rerun/_runs/20261001-210030-1231/create_grades/*.json \
  --runs-dir ~/.qualgentbench/runs-qua2861-rerun --grader-version 3
```

## Reproduce

```bash
uv run python scripts/run_create_ab.py report --experiment qua2861-rerun-app-open-positive-control \
  --runs-dir ~/.qualgentbench/runs-qua2861-rerun          # exit 0 = DETECTED
uv run qualgent-bench show --mode create --runs-dir ~/.qualgentbench/runs-qua2861-rerun
uv run python scripts/run_create_ab.py report --experiment qua2861-harmful-rule-positive-control \
  --runs-dir ~/.qualgentbench/runs-qua2861                # the first attempt: exit 4 = INCOMPLETE
```

The run command that produced it is below. It uses absolute local paths to the private checkouts, which are only pins: the driver reads their committed trees at these SHAs.

```bash
uv run python scripts/run_create_ab.py run --experiment qua2861-rerun-app-open-positive-control \
  --prediction harmful-rule-positive-control-mechanism/v2 \
  --a-name main --a-qualgent-mcp <QualGent-MCP>@8fb4ce7a605b913b0b6e5cf18ecf9ab3fa18f55d \
  --a-devloop <DevLoop-MCP>@52a20c669d20c195b5f83ce052aa203a9cc75770 \
  --b-name app-open-rule --b-qualgent-mcp <QualGent-MCP>@8fb4ce7a605b913b0b6e5cf18ecf9ab3fa18f55d \
  --b-devloop <DevLoop-MCP>@790f6b46e3336c387c4e96fb2c69fb76e0e96f72 \
  --device emulator-5554 --mcp-server http://127.0.0.1:51871 \
  --runs-dir ~/.qualgentbench/runs-qua2861-rerun --max-cost 240 --yes
```
