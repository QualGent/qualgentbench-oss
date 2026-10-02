# CreateBench v2: persistence defects for a 6+ brief DROP group (QUA-2870, BACKLOGGED)

**Status (2026-10-02).** The design is ready and the authoring is drafted on branch
`qua-2870-persistence-defects`. The owner moved the ticket to the backlog: it is worth doing
only before another positive-control run.

| part | state |
| --- | --- |
| APKs | built locally once, as a compile check. Not published |
| derivation | started and stopped before any truth row was written. No device work is in the branch |
| truth rows, controls | none |
| `apk:` blocks | not moved |
| `corpus_version` | not yet moved by derived data. The draft cases already change `test-cases/*.yaml`, so it moves on this branch as it stands |
| paid probe | not run |

## Why

QUA-2861 (docs/createbench-v2-validation.md, section 9, condition 1) requires a real A/B's
claim to hold at brief level. A brief-level test needs at least 6 briefs per stratum: 6/6
in the predicted direction gives a sign-test p of 1/64 = 0.016. v2's DROP group has 4
persistence briefs, which caps the brief-level p at 1/16 = 0.0625. The corpus had only 5
canary-covered persistence targets, and contacts-favorite leaked in QUA-2864's probe, so
4 was the maximum.

## Admission rules (each new target must pass all four)

- **(a) Every path.** The fault sits on EVERY user path to its write. A fault that one
  natural walk can step around lets arm A's honest case miss it, which breaks arm A's
  power.
- **(b) Alive.** The app stays alive on its normal screen.
- **(c) No dependent act step.** After the faulted write, the feature's walk has no ACT
  step that depends on the stored state. contacts-favorite broke this rule: its walk
  opened the Favorites tab and tapped the contact there.
- **(d) Honest canary.** `QgbFlags.fired("<id>")` fires only when the write is actually
  discarded.

## The four defects

| defect | app, case | origin | why it passes (a)-(d) |
| --- | --- | --- | --- |
| `reminder-amount-edit-lost` | medtimer, `medtimer-edit-reminder-dosage` | re-admitted from the 2026-09 prune; canary added | The dosage is pinned in both `ReminderRepositoryImpl.update` (advanced reminder settings) and `updateMany`. `updateMany` is where the medicine screen's `onStop` saves inline edits. These are the only reminder write paths. The canary fires only when the pinned amount differs from the requested one, because `onStop` re-saves unchanged reminders. The old `--repeat 3` row had an EMPTY clean/seeded screen diff. |
| `note-back-field-dropped` | ankidroid, `anki-add-note` | re-admitted; canary added | Every field after the first is blanked in `saveNote`'s add branch, the one path a new note takes. The Add screen then clears exactly as after a good save, and the old row's diff was EMPTY. The canary fires only when a non-empty field is blanked. |
| `note-delete-ignored` | orgzly, `orgzly-delete-note` | new, journey-only | The `NoteDelete` use case sits behind both user delete paths: the editor's Delete and the list's selection action. It reports the requested count as deleted and writes nothing. Cut/paste calls `deleteNotes` directly and is untouched. The canary fires when `ids` is non-empty. |
| `note-body-dropped` | orgzly, `orgzly-note-with-body` | new, journey-only | `NoteCreate` and `NoteUpdate` sit behind every editor save, and both store the payload with `content = null`. The only other content write, `NoteUpdateContent`, is a checkbox toggle in rendered text and is left alone. The canary fires only when the dropped content was non-empty. |

Orgzly behaviour measured on the clean build (2026-10-02, emulator):

- The delete route is: long-press → More options → Delete → the "Delete note?" dialog's
  Delete. It removes the row.
- The editor's body field is `content_view` (hint "Content"). Done closes to the notebook,
  and the notebook lists a body under its title.

## Rejected candidates

| candidate | why rejected |
| --- | --- |
| medtimer `event-skip-not-saved` | Fails (a). The Overview's quick-action "Skipped" on a raised reminder saves through `NotificationProcessor.updateAll`, which is unpatched. |
| medtimer `medicine-rename-lost` | Passes (a)-(d), but medtimer would then exceed `max_per_app: 3`. It is the first spare. |
| orgzly `tags-dropped-on-edit` | Fails (a). It patches only the edit path; a walk that sets tags at creation goes through the unpatched `createNote`. |
| orgzly `done-ignored-with-deadline` | Fails (a). It patches only the list's `setNotesState`; the editor's state button saves through the unpatched `updateNote`. |
| ankidroid `note-added-to-default-deck` | Fails (c). The natural walk opens the deck afterwards, the same leak as favorites. |
| ankidroid `note-tags-dropped` | Tags are optional, so a natural walk may never reach the fault. |

## What is drafted on the branch

- **Patches** (`data/benchmarks/{medtimer,ankidroid,orgzly}.yaml`): canaries on the two
  re-admitted patches, and the two orgzly journey-only defects with their guided tasks.
  `build_app.py --buggy` compiled all three apps, and the new canary strings are in the
  dex.
- **Cases** (`data/test-cases/*.yaml`): the four defects and four cases, with neutral
  briefs. `lint_journey_cases` and `lint_create_briefs` pass on them.
- **`scripts/derive_journey.py`**: a pass entry records `fired` when a pass left markers,
  so a silent write's canary is provable from the row.
- **`scripts/derive_create_controls.py --new-candidates-only`**, with tests:
  - What it does: it extends each case's stored derivation instead of redoing it.
  - When it reuses: only if the case's route and bugs are what the stored derivation
    measured, that is, the fingerprint over the defect set it saw, at the same `--repeat`.
  - What it runs: one clean reference plus only the defects the stored derivation never
    tried. It records `incremental` (`reused`, `derived`, `reused_from`).
  - Cost: adding defects to an app makes every case of that app stale, because the
    fingerprint includes the app's defect ids. Incremental mode is about 175 replays for
    the 19 existing cases plus the 4 new ones, against about 450 for a full re-derive.
- **`data/create/positive-control-v3.yaml`**:
  - Contents: 8 assert + 4 walk briefs. `spread_pool` is frozen at 2026-10-02 and covers
    all 6 apps.
  - Status: PENDING the probe, and registered by no prediction. positive-control-v2.yaml
    and its tests are untouched.
  - Tooling: `lint_create_briefs` gates v3. `check_tier_ready --briefs subset-v3` and
    `create_adversary_check --subset-v3` accept it, and judge it under the v2 rule and
    per-group design.
- **docs/defect-classes.md, §10 addendum**: persistence goes from 6 to 10 of 42 (23.8%
  against the 14% target). This reverses §7 in the open and is **PENDING OWNER
  APPROVAL**. `tests/test_mix_report.py` pins the new mix with the four listed as
  `POSITIVE_CONTROL_ADDITIONS`.

Test status on the branch: green, except the tests that need derived data. These fail
until a resumer derives the new cases and their controls:

- `test_create_controls::test_every_public_case_carries_a_current_control_derivation`
- `test_create_detection::test_every_fail_expected_public_case_is_labelled_without_a_problem`
- `test_lint_create_briefs`: the real-corpus and main-entry tests (v3's detection labels
  need truth rows)
- `test_check_tier_ready_create`: the two real-corpus tests

## Owner decisions pending

1. **Mix policy.** Admit the four to the journey corpus (the addendum), or keep them out
   of the journey board as create-only cases. No create-only mechanism exists today.
2. **A registration for v3.** v2's DROP trials (4) on 8 DROP briefs come to 8×4×2 + 4×1×2
   = 72 cells, about $407, above the $300 hard cap. A v3 prediction should probably use
   DROP × 2 trials (40 cells, about $226 at QUA-2861's $5.65 per cell); main prefers more
   briefs to more trials. The owner sets the prediction and the budget.

## To resume

Work from the epic head, on one emulator.

1. **Owner.** Approve the mix policy, or choose create-only (then rework the cases' home).
2. **Rebuild.** `build_app.py <app> --check-toolchain`, then
   `build_app.py <app> --buggy` for medtimer, orgzly and ankidroid. A rebuild is never
   byte-identical, so use fresh hashes. Then run
   `publish_apk.py <app> --kind journey --write`. That step is local: it marks the
   sha256 `unpublished` in `apk-pins.json`.
3. **Derive the new cases.** Install each APK, then run
   `derive_journey.py <app> --repeat 3 --case <new case>`. Confirm clean HOLDS 3/3, seeded
   VIOLATED 3/3, `fired` on every seeded pass and on no clean pass, and a seeded screen
   diff that is empty, or limited to the final read, on the route.
4. **Check the 19 existing cases.** Run them in one trial against the rebuilt APKs into a
   scratch `--json` (QUA-2781's method). Keep their committed rows when the verdicts
   match.
5. **Derive the controls.** Run
   `derive_create_controls.py medtimer orgzly ankidroid --repeat 3 --new-candidates-only`.
   New cases get a full derivation.
6. **Gates.** Run the full suite, `lint_journey_cases`, `lint_create_briefs` and
   `check_tier_ready --tier create --briefs subset-v3`.
7. **Stage the APKs.** Stage them with SHA256SUMS. Owner:
   `HF_TOKEN=... uv run python scripts/publish_apk.py <app> --kind journey --write --upload --yes`,
   then commit the moved blocks and `apk-pins.json`. `corpus_version` moves, and boards
   from before and after are not blended.
8. **Paid probe (owner budget).** Use QUA-2864's method on the 4 new briefs, 2 rounds each:
   - uptake: 8 arm-B creations at about $1 each;
   - leak: 8 target-only runner episodes at about $1 each;
   - total: **16 episodes, about $16-17**.

   A brief whose rule-following case still FAILs on the target build leaves v3, as
   contacts-favorite left v2. Then register v3 with the owner's trials and budget.
