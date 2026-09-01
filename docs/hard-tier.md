# Hard tier — what has to happen

Target from `design.html` §02: 12 hard apps × 5 seeded defects each, taking the corpus from
16 to 28 apps. As of 2026-08-31 ALL 12 hard specs exist and are derived ×3 + gate-green
(calendar, gallery, contacts, aegis, filemanager, messages, orgzly, moneymanagerex,
ankidroid, openscale, medtimer, tasksorg). `hard` is IN `READY_TIERS` and every APK is
published (2026-08-31) — see the tier-level checklist at the bottom for what remains.
Nothing below is special to hard — it is the same pipeline every easy/medium app went
through.

## Selection (already done — 12 picked, 2026-08-28)

| # | app | trait | notes |
|---|-----|-------|-------|
| 1 | fossify-calendar | recurrence, reminders, event types | DONE 2026-08-28: built @1.10.3, 12/12 derived ×3 stable, gates green, 1 codex episode replayed |
| 2 | fossify-gallery | media library | DONE 2026-08-28: built @1.13.1 (foss), 10/10 derived ×3 stable, gates green, 1 codex episode replayed; `file` oracle wired into checks + proven |
| 3 | fossify-contacts | grouped records | DONE 2026-08-29: 1.6.0, `content` oracle wired + proven, 8/8 ×3 stable, codex 5/5 → PARTIAL 89%; ContactsProvider HARD-purged per reset; delete/favorite/group are multi-site (favorite has 4 entry points) |
| 4 | aegis | vault | DONE 2026-08-29: v3.4.2, Java shim, plaintext vault + `pref_intro` seeded by device_setup, 8/8 ×3 stable, codex 5/5 → PARTIAL 89% |
| 5 | moneymanagerex | transactions, transfers, statuses, summary | DONE 2026-08-29: 5.5.11 fdroid, `.mmb` is plaintext (SQLCipher only with a password); seeded `qgb.mmb` (2 accounts + payee with default category — the form refuses to save without payee+category) relocated into the sandbox via prefs so `db:` works; balance defects gated by rewriting the loaded balance SQL; 12/12 ×3 stable; codex 6/6 → VERIFIED 99% (hidden Difference defect found via `other`) |
| 6 | fossify-messages | threads, drafts, rename, unread | DONE 2026-08-29: 1.9.1, conditional defects + 2 hidden UI areas, 11/11 ×3 stable; codex 5/6 → VERIFIED 83% (missed the hidden badge; found the typo via `other`). Env: `root: true` + sqlite purge of the telephony provider + `emu: sms send`; archive is unusable on API 33 (provider lacks `archived`) |
| 7 | fossify-filemanager | file ops | DONE 2026-08-29: 1.6.1, 10/10 ×3 stable, codex 5/5 → PARTIAL 89%; binary `contains` (zip) needed a lenient decode in device_oracle |
| 8 | ankidroid | decks / note types / scheduler | DONE 2026-08-31: v2.24.0 PLAY flavor (full hits an all-files wall), arm64 split APK; conditional defects (Back field, tags on add+edit, wrong deck, Good→Again in BOTH answerCardInner overrides) + 2 hidden counts; collection seeded to the EXTERNAL app dir (shell-writable, run-as is not — `db:` takes an absolute path), backup nag pref'd away, LeakCanary's second launcher pm-disabled (root) or launches are a coin flip; decks.name carries `unicase` — never compare/aggregate on it; 12/12 ×3 stable; codex 5/6 → PARTIAL 58% (missed the hidden deck new-count; 3 repros anchored the unreplayable "All decks" dropdown) |
| 9 | orgzly (orgzly-revived) | outliner, repeaters, deadlines, tags | DONE 2026-08-29: v1.23.0, conditional defects (repeater/deadline/edit) + 2 hidden UI areas, 11/11 ×3 stable; codex 2/5 → PARTIAL 20% (both hidden missed; found defects unreplayable — invented anchors). Title field is `title` (not `name`); new notes land off-screen in the 33-note sample book (scroll ×6); the drawer/Agenda is unreachable by the replay grammar |
| 10 | openscale | multi-user measurements | DONE 2026-08-31: v3.1.2 Compose rewrite, compileSdk 37 (platform "android-37.0" via NEW cmdline-tools — old sdkmanager can't see it); Room DB + DataStore pb seeded (Alice active, 74/78/85 kg); defects: edit skips derived recalc (3 sites — the use case has its own recalc), dead delete, user-delete wipes all; hidden: stats average drops newest, ages +1yr; user switcher and user-form save have NO content-desc (unanchorable) — no check touches them; the Table column check was dropped as UNSTABLE (seeded stringSet flaky AND post-toggle render flaky); 12/12 ×3 stable (final); statistics_range AND user_list became COLLATERAL after check_controls caught controls charging right agents (each hidden defect renders on a control screen), insights_open added as the 5th control, computed_values scoped to "newly added"; final codex hunt 3/5 → PARTIAL 40%, FP 0% (was 17-20% before the collateral fixes; agents repeatedly SAW both hidden defects but filed them under the control areas sharing those screens — collateral is the right state for a control on a defect's screen) |
| 11 | tasks.org | recurrence, subtasks, filters | DONE 2026-08-31: tag 15.10 generic debug, compileSdk 36 — the KMP exception is just source-set placement: the flag shim and every kmp patch live in jvmCommonMain (java.io.File is not common API, so NOTHING is patched in commonMain; commonMain call sites are patched at their jvmCommonMain/app callers instead); defects: repeating completion never reschedules, parent completion leaves subtasks open, CHANGING an existing due date reverts (first set works — control twin), delete dropped; hidden: subtask chip −1, date group headers +1 day; drawer opener and row checkboxes have NO descs — completion goes through Search + the `completeBox` resource-id rank-4 fallback, and no check opens the drawer; the repeating task's due date is recomputed to today at device_setup (sqlite strftime) so "Due today" grouping is run-day-stable; local-account delete PURGES the row (assert count=0, no tombstone); 12/12 ×3 stable; hunted 4× while wording converged (5/6→66%, 4/6→33%, 4/6→50%, final gate-clean 3/6→41%, FP 0%): set_due_date and task_list wordings each invited the hidden section-shift into a control ("grouped by due date", "reopen" was a banned token); repeat/due-edit/delete found every run, the subtask defects are the consistent misses (completed rows hide children — agents get fooled) |
| 12 | medtimer | medication schedules | DONE 2026-08-31: v1.25.2 foss debug (intro skipped in debug builds), multi-module — the flag shim lives in :core:common; defects: stock decrement capped at 1 (fires via a SEEDED raised event answered Taken — the event needs a REAL reminderId or stock is skipped), skip stored as taken, dosage edit pinned (update AND updateMany — the medicine screen bulk-saves), rename pinned; hidden: "(N left)" −1, list time +1h; the "Log additional dose" FAB has NO accessibility node (a real a11y bug — invisible to uiautomator AND u2), so events are seeded with now() timestamps in device_setup; medicine rows are entered via their exact subtitles ("1 reminder") — row labels embed a drifting date; 12/12 ×3 stable; codex 4/6 → PARTIAL 58% |

Selection rules that reject an app: compileSdk > 36, KMP/Flutter, unreadable persistence
(SQLCipher killed pf-food-tracker), permission/SAF wall on launch, verdicts that depend on
timing or image content. Alternates if one falls: KeePassDX (always-encrypted kdbx —
UI-only verdicts), Feeder (needs a host-served feed), MaterialFiles.

## Per-app loop (batches of 3 apps: author all three, then derive on 3 emulators concurrently and hunt with `--app a,b,c --devices e1,e2,e3`; local only — no HuggingFace upload yet)

1. **Verify it can be added.** Sibling checkout under `QualGent-Repos/`, pinned to a
   release TAG (`build.ref`), `:app:assembleDebug` works, debug package resolves, DB /
   files readable via `run-as`. No first-launch wall (or one that `device_setup` can seed
   away).
2. **Author the spec** — `src/qualgentbench/data/benchmarks/<id>.yaml`, canary on line 1.
   5 `bugs:` (single-site, flag-gated on `QgbFlags.on(id)`, unique `find` anchors, spread
   L1–L4, each on its own code path so one fix cannot repair another), ≥2 working
   controls with a device-state oracle, a `check:` for every area, non-empty `probe:`,
   a neutral brief (no `bug`/`broken`/`find as many`/`relaunch` in title or instruction),
   an explicit `step_budget`, and one `tasks[]` entry per bug (+ clean tasks).
3. **Build locally.** `scripts/build_app.py <id>` → `dist/<id>/{clean,buggy}.apk`;
   `--smoke <serial>` installs and launches both.
4. **Derive the truth on an emulator.** Install `buggy.apk` with `-g`, then
   `scripts/derive_truth.py <id> --device <serial> --repeat 3 --tier hard`. Every area
   must derive as its declared state; fix DISAGREEs by changing `state:` (incl.
   `collateral`), never by asserting. `scripts/check_controls.py --tier hard` next.
5. **Verify replay.** One real hunt episode (`run --app <id> --mode hunt --trials 1`), then
   confirm `_verify_episode` reproduces the claims (`replay.json` provenance:
   `u2_available`, no `ambiguous_steps` surprises). `scripts/validate_bundle.py` on it.
6. **Approval gate** — stop and review before moving to the next app.

## Lessons from the first five (apply to the next seven)

- **Seed at the shared layer.** If a feature has several UI entry points (list selection,
  the item's own screen, a picker on another tab), a patch at one call site lets an agent
  honestly report the feature working via another path. Prefer the repository/helper/
  config level (aegis, gallery); otherwise use `patches:` for every entry point (contacts).
- **State outside the sandbox must be purged, not soft-deleted.** ContactsProvider keeps
  `deleted=1` rows that the app still lists.
- **Getters that write defaults** make "key exists" oracles vacuous — match the value.
- **Deviation rate is at 52%** (cap 55%): the next apps need ≥ 5 controls each.
- Agents' unreplayable claims so far were all agent-side (one-time dialogs omitted from
  steps, guessed resource-ids, app-chooser labels, formatted phone numbers).

## Batch 3 (messages, orgzly, moneymanagerex) — conditional + hidden defects

- Defects fire only under a combination (repeater, deadline, transfer, void, relaunch…);
  two areas per app are `hidden: true` and reachable only through an agent's `other…`
  report. Results with the brief clean: messages 5/6 (83%), orgzly 2/5 (20%),
  moneymanagerex 6/6 (99%) — the first hard episodes where a top model misses defects.
- Hidden-area pitfalls found the hard way: the generated `RESULT:` template listed hidden
  ids (agents reported them by id); probes must not contain a word the defect misspells;
  `bug_spec` features must carry `hidden`; the `AREA:` line channel and
  `replay_findings.py` must resolve `other…` like the findings.yaml channel.
- Replay-grammar limits met: no drawer (edge swipes are gesture-nav), no IME "search"
  action, long lists need `swipe: up` to bring a new row on screen, `back` right after
  `type` only closes the keyboard.

## Tier-level, after all 12

- DONE 2026-08-31: `check_tier_ready --tier hard` READY; adversary PASS; 62 controls
  clean over 12 episodes; `hard` added to `READY_TIERS` (`cli.py` + `preflight.py`);
  all 12 buggy APKs published to `qualgent/qualgentbench-apps` under `hard/` with
  `apk:` blocks (sha256 + size; fetch round-trip verified unauthenticated).
- DECIDED 2026-08-31: hard keeps the uniform `step_budget: 500` (same as easy and
  medium) — budgets are NOT re-derived per-app for this tier.
- STILL OPEN: README/THIRD_PARTY.md rows for the 12 new apps.
- Then: ≥5-model calibration panel → lock a private held-out split (design.html §10).
