# CreateBench v2: persistence defects for a 6+ brief DROP group (QUA-2870, results)

**Result.** The positive-control DROP group goes from 4 to **8 persistence briefs over 6
apps**, so a real A/B's claim can now hold at brief level.

- **New targets.** Four canary-covered persistence defects joined the journey corpus. The
  owner approved this on 2026-10-05; see the docs/defect-classes.md addendum.
- **Derivation.** Each derives cleanly at `--repeat 3`:
  - clean HOLDS 3/3 and seeded VIOLATED 3/3;
  - its canary fired on every seeded pass and on no clean pass.
- **Rebuild check.** The 19 existing cases of the three rebuilt apps give the same verdicts
  on the new APKs.
- **Probe.** QUA-2864's uptake/leak method cost **$17.34** on the API key, against a $25
  cap:
  - uptake of the harmful rule was 8/8;
  - target-only power leak was 0/8;
  - **no brief was dropped**, and the spare `medicine-rename-lost` was not needed.
- **Registration.** `harmful-rule-positive-control-mechanism/v3` registers the 12-brief
  subset: 40 cells, $226 at measured prices, default `--max-cost` $260.
  - It adds a brief-level sign test to v2's design.
  - v1 and v2 are unchanged.
  - The v3 live re-run is a separate run, not part of this ticket.

| | before | after |
| --- | --- | --- |
| `corpus_version` | `5e49ec8071b1` (main, epic head) | `0181f10cb0b9` |
| persistence defects in the journey corpus | 6 of 38 | 10 of 42 |
| public journey cases | 41 | 45 |
| DROP briefs in the positive control | 4 (v2) | 8 (v3) |
| best brief-level sign-test p | 1/16 = 0.0625 | 1/256 (8/8); 1/64 at 6/6 |

Journey and create boards from before and after this change are not comparable and are
never blended.

## Why

QUA-2861 (docs/createbench-v2-validation.md, section 9, condition 1) requires a real A/B's
claim to hold at brief level. That needs at least 6 briefs per stratum, because 6/6 in the
predicted direction gives a sign-test p of 1/64 = 0.016.

v2's DROP group has 4 persistence briefs, which caps the brief-level p at 1/16 = 0.0625. The
corpus had only 5 canary-covered persistence targets, and contacts-favorite leaked in
QUA-2864's probe, so 4 was the maximum.

## Admission rules

Each new target must pass all four:

- **(a) Every path.** The fault sits on EVERY user path to its write. A fault that one
  natural walk can step around lets arm A's honest case miss it, which breaks arm A's
  power.
- **(b) Alive.** The app stays alive on its normal screen.
- **(c) No dependent act step.** After the faulted write, the feature's walk has no ACT step
  that depends on the stored state. contacts-favorite broke this rule: its walk opened the
  Favorites tab and tapped the contact there.
- **(d) Honest canary.** `QgbFlags.fired("<id>")` fires only when the write is actually
  discarded.

## The four defects

| defect | app, case | origin | why it passes (a)-(d) |
| --- | --- | --- | --- |
| `reminder-amount-edit-lost` | medtimer, `medtimer-edit-reminder-dosage` | re-admitted from the 2026-09 prune; canary added | The dosage is pinned in both `ReminderRepositoryImpl.update` (advanced reminder settings) and `updateMany` (the medicine screen's `onStop` save of inline edits). These are the only reminder write paths. The canary fires only when the pinned amount differs from the requested one. |
| `note-back-field-dropped` | ankidroid, `anki-add-note` | re-admitted; canary added | Every field after the first is blanked in `saveNote`'s add branch, the one path a new note takes. The Add screen then clears exactly as after a good save. |
| `note-delete-ignored` | orgzly, `orgzly-delete-note` | new, journey-only | The `NoteDelete` use case sits behind both user delete paths. It reports the requested count as deleted and writes nothing. |
| `note-body-dropped` | orgzly, `orgzly-note-with-body` | new, journey-only | `NoteCreate` and `NoteUpdate` sit behind every editor save, and both store the payload with `content = null`. |

Rejected candidates:

| candidate | why rejected |
| --- | --- |
| medtimer `event-skip-not-saved` | Fails (a): the Overview's quick-action "Skipped" saves through the unpatched `updateAll`. |
| medtimer `medicine-rename-lost` | Passes (a)-(d), but medtimer is at `max_per_app: 3`. It is the spare, and it was not needed. |
| orgzly `tags-dropped-on-edit` | Fails (a): it patches the edit path only. |
| orgzly `done-ignored-with-deadline` | Fails (a): it patches the list's `setNotesState` only. |
| ankidroid `note-added-to-default-deck` | Fails (c): the natural walk opens the deck afterwards. |
| ankidroid `note-tags-dropped` | Tags are optional, so a natural walk may never reach the fault. |

## 1. Builds and pins

`build_app.py <app> --check-toolchain` passed for all three apps, and `--buggy` built them.
Gradle found the inputs unchanged since the 2026-10-02 compile check, so these are those
bytes.

`publish_apk.py --kind journey --write` landed the blocks locally. The owner then uploaded
them (`--write --upload --yes`), and `data/apk-pins.json` pins each one:

| app | journey sha256 | pinned revision |
| --- | --- | --- |
| medtimer | `92fc70a4a44b873a0187b2e9d14c2e5424952e09673220635a9344f5879316c2` | `aba6725eeb2d` |
| orgzly | `98eaa08cae9fb8947328e5d9ca413850f3e0c4d59d3ea3bea5b4021a05710f81` | `4444ba626377` |
| ankidroid | `38181e5f5953737f04e861f08e4b3e1dc836d8afdf09d53d758bf6f41d0b69fd` | `648da3d30a55` |

## 2. The new cases: `derive_journey.py --repeat 3`

All derives ran on one headless emulator (`qgbench_root`, android-35, `-memory 2048`) at
the default clock pin.

| case | clean | seeded | `fired` on seeded passes | `fired` on clean passes | clean/seeded screen diff |
| --- | --- | --- | --- | --- | --- |
| orgzly-delete-note | HOLDS 3/3 | VIOLATED 3/3 | note-delete-ignored 3/3 | none | steps 6-7 only: the final list still shows the note |
| orgzly-note-with-body | HOLDS 3/3 | VIOLATED 3/3 | note-body-dropped 3/3 | none | empty |
| medtimer-edit-reminder-dosage | HOLDS 3/3 | VIOLATED 3/3 | reminder-amount-edit-lost 3/3 | none | step 4 only: the bottom-nav labels are absent from one dump, which is dump noise and not the dosage |
| anki-add-note | HOLDS 3/3 | VIOLATED 3/3 | note-back-field-dropped 3/3 | none | empty |

`derive_journey.py` now records `fired` on each pass entry that left markers. That makes a
silent write's canary provable from the row alone.

## 3. The 19 existing cases against the rebuilt APKs

This used QUA-2781's method:
- one trial per version, into a scratch `--json`;
- each result compared with its committed row on expected, measured, agrees and both pass
  outcomes.

**19/19 match**, so the committed rows were kept. Every seeded pass fired only its own
canary, and no clean pass fired.

The first ankidroid attempt died in fixture staging: `collection.anki2` did not appear
within the fixture's 30 s wait. A straight retry passed.

## 4. Controls: `derive_create_controls.py --repeat 3 --new-candidates-only`

The command ran per app with the `dist/` build installed. `--install` fetches the published
APK, and these APKs were not published yet at that point.

**Results.**
- All 23 cases of the three apps are derived, none is stale, and every case has 3 eligible
  controls (no specificity n/a).
- The 19 existing cases were extended from their stored derivations. The 4 new cases were
  derived in full.
- Cost: about 109 candidate replays plus 24 clean references, with 35 candidates taken
  from stored trials.
- The ankidroid derive was resumed once with `--skip-derived` after its driving shell was
  killed. The truth is written per case, so nothing was lost.

**Merge with QUA-2867.** `--new-candidates-only` now walks rank rule 2's replay order under
the same `--stop-after` early stop as a fresh derive:
- it takes a stored candidate's recorded trials (re-judged under rule 2) instead of
  replaying it;
- it replays only candidates the stored derivation never measured;
- with the same trial outcomes it reaches the controls a fresh derive reaches.

`incremental_plan` also extends an early-stopped stored derivation: its fingerprint covers
the candidates it never replayed.

**Owner note.** These three apps' rows now record `rank_rule` 2 with the default early stop
(3). The other three public apps' committed rows are still rule-1 exhaustive. QUA-2867 left
applying rule 2 to the committed truth (`--rejudge --write`) as an owner call, and a
re-derive can only write rule 2.

A rule-1 exhaustive row that became early-stopped no longer carries the trials of
candidates past the stop point. Its `early_stop.unreplayed` lists them as unmeasured.

## 5. The uptake/leak probe (QUA-2864's method)

| | |
| --- | --- |
| arm B | QualGent-MCP `8fb4ce7a605b` + DevLoop-MCP `790f6b46e333` (QUA-2864's app-open/v2 template; template sha256 `67a2d383e9c3…`, guide `7c7e70c1ad97…`), `qualgent_tools: template` |
| author, runner | codex-cli · gpt-6-astra; `provenance.agent_auth = api_key` on all 16 episodes (oss `.env` symlinked by the owner; no `--allow-codex-login`) |
| device | `qgbench_root` android-35, standalone devloop-mcp `--app-source none` on :51871 from the DevLoop `52a20c6` checkout (`isolation: per_mcp_session`) |
| runs dir | `~/.qualgentbench/runs-qua2870` |
| creation runs | `20261006-011901-6fd5` (round 1, $4.39), `20261006-013312-36c1` (round 2, $4.64) |
| leak runs | `20261006-qua2870-leak-r1` ($3.94), `20261006-qua2870-leak-r2` ($4.37); manifests `create_grades/targetonly.<brief>.<ep>.json`, cell kind `smoke` (never on a board) |

**Uptake** is `uptake.classify` against `app-open/v2`. **Leak** is one target-only runner
episode per rule-following case on its target build.

| brief | uptake r1 / r2 | creation $ r1 / r2 | target-only verdict r1 / r2 | power | canary fired | runner reported the target anyway (`bugs_found`) | leak $ r1 / r2 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| medtimer-edit-reminder-dosage | yes / yes | 1.02 / 1.09 | pass / pass | missed / missed | 2/2 | yes / no | 0.88 / 0.92 |
| anki-add-note | yes / yes | 1.25 / 1.04 | pass / pass | missed / missed | 2/2 | no / no | 0.76 / 0.96 |
| orgzly-delete-note | yes / yes | 0.94 / 1.28 | pass / pass | missed / missed | 2/2 | yes / yes | 1.19 / 1.27 |
| orgzly-note-with-body | yes / yes | 1.18 / 1.23 | pass / pass | missed / missed | 2/2 | no / no | 1.12 / 1.22 |

- **Uptake: 8/8.** Every case ends on exactly "Verify the app is still open", has no other
  check, and expects "The app is still open."
- **Power leak: 0/8.** Every rule-following case PASSED on its target build with the
  target's canary fired, so the harmful rule removes power on all four as designed. **No
  brief leaves v3.**
- **Report-level leak** (as QUA-2864 found on contacts-phone and contacts-delete). On 3 of
  8 runs the runner noticed the symptom and reported it, but kept the verdict PASS because
  the expected outcome was met:
  - medtimer-edit-reminder-dosage, round 1;
  - orgzly-delete-note, both rounds.

  Power is the verdict, so this does not touch the registered test. Any future axis that
  credits a report without a FAIL would reopen the leak on these two briefs.
- **Total cost: $17.34** for 16 episodes, about $1.08 each.

## 6. The v3 registration (`create/ab.py`)

`harmful-rule-positive-control-mechanism/v3`, sha `4bca65d47659`, is the CLI default now. It
runs on `data/create/positive-control-v3.yaml`: 8 persistence DROP briefs and v2's 4 walk
briefs, over 6 apps.

**Unchanged from v2:**
- the uptake check: arm-B app-open/v2 uptake of at least 0.8 on the DROP group, else
  INCONCLUSIVE, judged first;
- the precondition: arm A's DROP power of at least 0.5 and at least 12 scored DROP cells
  per arm (here there are 16);
- the six expectations: one-sided Fisher power DOWN on DROP, power FLAT on FLAT, and
  repeatability and specificity FLAT on both groups.

**New in v3:**
- **Trials.** DROP × 2, FLAT × 1, both arms: (8×2 + 4×1) × 2 = **40 cells**. v2's 4 DROP
  trials would be 72 cells, about $407, which is over the $300 cap.
- **A seventh expectation: power DOWN per DROP brief, by a brief-level one-sided sign
  test.**
  - It is `Expectation(test="sign", scope="each", min_briefs=6)`, with p < 0.05.
  - A brief counts for the prediction when B's power fell below A's.
  - A tie counts against it.
  - A brief whose A is at the floor, or that is unscored, is left out.
  - Fewer than 6 judged briefs is INCONCLUSIVE. More briefs against than for is NOT MET.
  - `ab.check_design` refuses a subset with fewer DROP briefs than `min_briefs`.
- **Budget.** `DEFAULT_MAX_COST_BY_PREDICTION`: v3 defaults to `--max-cost` $260. Every
  other registration keeps $280, and the hard cap is still $300.

v1 (`fcec04cefb3f`) and v2 (`6aa0adc7c13d`) stay selectable by ref with their hashes
unchanged. The new `Expectation` fields are written only when set.

```text
$ uv run python scripts/run_create_ab.py --plan
experiment plan (canonical): 12 brief(s), 40 arm cell(s)
  assert (DROP group): 8 brief(s) × 2 trial(s) × 2 arms = 32 cell(s)
  walk (FLAT group): 4 brief(s) × 1 trial(s) × 2 arms = 8 cell(s)
prediction harmful-rule-positive-control-mechanism/v3 (sha 4bca65d47659):
  precondition: arm B uptake of app-open/v2 >= 0.8 (assert targets)
  rule app-open/v2: Required final step: …
  precondition: arm A power >= 0.5 and >= 12 scored power cells per arm (assert targets)
  power down (pooled, assert targets) [one-sided Fisher exact p < 0.05]
  power flat (pooled, walk targets)
  repeatability flat (pooled, assert targets)
  repeatability flat (pooled, walk targets)
  specificity flat (pooled, assert targets)
  specificity flat (pooled, walk targets)
  power down (every brief, assert targets) [brief-level one-sided sign test p < 0.05 over >= 6 briefs]
targets: 8 alive, 4 death (crash/ANR/stuck)
gradable briefs: 12/12
estimated cost at measured actuals: $226.00 for all 40 cell(s) (author $1.00 + grade $4.65 per cell) — within the ceiling
conservative estimate: $340.00 (…)
ceiling --max-cost $260.00 (hard cap $300.00)
plan only: nothing registered, nothing spent
```

**Gates.**
- `create_adversary_check --subset-v3` judges v3:
  - the scripted harmful-rule-v2 author is DETECTED, with Fisher met and the sign test 8/8;
  - the no-op and the non-taking arm B both read INCONCLUSIVE ("treatment not delivered");
  - a v3 that cannot detect its own positive control turns the gate red (planted-hole
    test).
- `check_tier_ready --tier create --briefs subset-v3 --config <arm config>` prints
  **READY**.

**Tests.**
- `tests/test_create_ab.py`:
  - v3 registered beside unchanged v1/v2;
  - its subset, plus the refusal under 6 DROP briefs;
  - the plan: 40 cells, at most $260;
  - the sign-test p;
  - DETECTED / INCONCLUSIVE / never-DETECTED on synthetic grades;
  - the sign test's 6-brief floor, tie and floor rules.
- `tests/test_create_adversary_check.py`: the real subset, and the planted hole.
- `tests/test_check_tier_ready_create.py`: the subset-v3 gate.
- `tests/test_create_controls.py`: incremental derivation under rank rule 2 and the early
  stop.

## 7. Limitations

- **Small probe samples.** Uptake and leak were measured at n = 2 per new brief. The re-run's
  uptake precondition is what judges delivery.
- **Rows are rule 2 for three apps only** (section 4).
- **FLAT group unchanged.** QUA-2861 found its premise only partly right: a recovered crash
  and a stuck screen can pass an app-open check. That is the runner-contract follow-up's
  business.
- **Correlated cells.** At 2 trials per DROP brief, trials of one brief are still
  correlated. That is why the sign test treats the brief, not the cell, as the unit.

## Reproduce

```bash
uv run python scripts/lint_journey_cases.py
uv run python scripts/lint_create_briefs.py
uv run python scripts/create_adversary_check.py --subset-v3
uv run python scripts/check_tier_ready.py --tier create --briefs subset-v3 --config <arm config>
uv run python scripts/run_create_ab.py --plan
uv run python scripts/derive_create_controls.py medtimer orgzly ankidroid --report
```
