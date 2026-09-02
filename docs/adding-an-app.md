# Adding an app (and its seeded defects) to the benchmark

One YAML in `src/qualgentbench/data/benchmarks/` **is** the registration — there is no
other registry. `bugs.load_apps()` globs that directory, so dropping the file in makes
the app runnable. Everything else in this guide is about making it *scoreable*: built,
seeded, measured, and gate-green.

## What you need before starting

- The app must be **open-source Android** and build a **debuggable** APK
  (`assembleDebug`). This is not a style preference: flag writing, the app-data
  snapshot/restore that makes replay deterministic, and the `db:` oracle all go
  through `adb shell run-as <package>`, which only works on debuggable builds.
  A release-signed app cannot be verified.
- A source checkout location: `build_app.py` clones/expects the app's repo as a
  **sibling of this repo's parent** (`REPOS = parents[2]` of the script — the
  `QualGent-Repos/`-style directory that contains this checkout). `build.dir` in the
  spec names that sibling directory.
- An emulator or device on `adb` for `derive_truth.py` and the gates.

## 1. Write the spec

Copy the closest existing spec (`birday.yaml` is the smallest complete one;
`markor.yaml` shows `device_setup`/`shared_storage`). Keep the filename equal to
`app.id` — `load_apps()` doesn't care, but `build_app.py <app_id>` and
`derive_truth.py <app_id>` look the file up by name.

**Line 1 must be the contamination canary comment** — copy it verbatim from any spec.
If that token ever appears in an agent's tool results, the episode is voided; it is
how reading the answer key is detected.

The blocks, in the order you'll fill them in:

```yaml
app:
  id: myapp
  name: MyApp
  package: com.example.myapp.debug   # the DEBUG package
  platform: android
  difficulty: medium                 # easy|medium|hard — this IS the tier
build:
  repo: https://github.com/you/myapp
  dir: MyApp                         # sibling checkout dir
  ref: "v1.2.3"                      # a release TAG, never a branch
  gradle_task: ":app:assembleDebug"
  apk_glob: "app/build/outputs/apk/debug/*.apk"
flags:
  file: app/src/main/java/com/example/myapp/QgbFlags.kt   # generated shim
  package: com.example.myapp         # .kt → Kotlin shim, .java → Java shim
```

**`bugs:`** — each defect is a source patch with a unique anchor:

```yaml
bugs:
  - id: save-drops-note
    title: Saving a new note silently discards it
    patch:                    # or patches: [...] for multi-site defects
      file: app/src/main/java/.../NoteRepo.kt
      find: |-
        repo.insert(note)
      replace: |-
        // BUG(save-drops-note): the new note is never inserted.
        if (!com.example.myapp.QgbFlags.on("save-drops-note")) repo.insert(note)
```

Rules `build_app.py` enforces: the `find` block must match **exactly once**
(ambiguous anchors are refused — extend `find` upward until unique; catima once
silently broke a control this way), every bug id must have a matching `tasks[]`
entry, and the `// BUG(...)` comments are stripped from any emitted source. Gate each
patch on `QgbFlags.on("<id>")` so the same APK serves as clean and seeded builds.

**`exploration:`** — the hunt-mode task:

```yaml
exploration:
  id: explore-myapp
  step_budget: 90              # required; derive with scripts/derive_budgets.py
  title: "Release sign-off — MyApp"
  features:
    - id: save_note
      state: broken            # ok | broken | collateral — will be RE-DERIVED
      bug_id: save-drops-note
      probe: [save, note]      # device-evidence keywords — MUST NOT be empty
      check:                   # how to exercise the area, never whether it works
        steps: [launch, {tap: "New"}, {type: "QA probe"}, {tap: "Save"}, relaunch]
        expect: {present: "QA probe"}
  instruction: |
    ...a NEUTRAL release-sign-off brief...
```

Two things bite here:
- The brief and title are scanned for biasing language (`bug`, `broken`, `find as
  many`, `relaunch`, ...). `check_tier_ready` fails on any hit — describe what each
  area *should do*, never hint that something is wrong.
- Empty `probe:` lists make `adversary_check` unpassable: the honest synthetic agent
  is distinguished from a guesser only by probe keywords appearing in device output.

`check:` steps use the same grammar agents report in: `launch`, `relaunch`, `wait`,
`tap:`, `long_press:`, `type:` (sets the field), `append:` (keystrokes),
`press: back|home|enter`, `swipe: up|down|left|right`; `expect` is
`present:`/`absent:` (whole-token match) or one of the harness-only forms —
enforced: only the spec parser (`truth.py`) may use them, an agent submission
writing one gets a parse error and no replay —
`{db: <file>, query: <sql>, equals: <value>}` and
`{file: <device path>, name: <entry>}` / `{file: <path>, contains: <text>}` (add
`absent: true` to prove a delete). `file` reads shared storage directly and falls
back to `run-as` for sandbox paths such as `shared_prefs/Prefs.xml` — the oracle for
file-backed apps (fossify-gallery's `.nomedia` marker, a renamed photo, a pref key).
`{content: <uri>, contains: <text>}` / `{content: <uri>, equals: <row count>}` (optional
`where:`, `absent: true`) queries a ContentProvider via `content query` — the oracle for
state the app keeps OUTSIDE its sandbox, which `pm clear` does not reset (fossify-contacts
reads `content://com.android.contacts/{contacts,data,groups}` and wipes them in
`device_setup`).

**`tasks:`** — one guided task per bug (`bug_id`, `tier: L1..L4` for recall weight,
`instruction`, `flow_steps`, `step_budget`). Copy a neighbour and adjust; `build_app.py`
refuses a bug without a task.

**Hidden areas.** A feature with `hidden: true` is derived, gated and scored like any
other but the brief must NOT name it — it is a defect the agent has to *notice* (a wrong
count, a misspelled label, a miscomputed summary). Give the brief one generic sentence
("also report anything else on screen that looks incorrect, under an area name starting
with `other`"); a finding named `other…` is mapped onto the single hidden feature whose
every `probe:` keyword appears in the finding's own words (`bugs.hidden_resolver`). Keep
hidden probes specific for that reason — and never use a word the defect itself
misspells (a "Mark as Unread" typo was reported as "Mark as Unraed", so a probe of
`unread` could not match; `mark` alone did). Hidden defects must still be flag-gated in
CODE (a resource-only typo would break the clean build too). The harness withholds
hidden ids from every generated part of the brief (the `RESULT:` template once listed
them and an agent simply reported them by id); keep them out of the spec's own
`instruction:` too.

**Optional blocks:** `setup:` (patches applied to BOTH builds — sample-data seeding,
not defects), `device_setup:` (`push:`/`shell:` staging for media apps, re-run at
every replay reset; `emu:` for emulator-console commands such as `sms send …`, the
only way to deliver an SMS; `root: true` to `adb root` first, needed to purge SYSTEM
providers such as the telephony store — never `pm clear` a system provider), `shared_storage:` (list of `/sdcard/...` dirs the app keeps user
content in — wiped per episode, snapshot/restored per replay pass; set
`restore_shared: false` only if re-extracting retriggers MediaStore indexing),
`apk:` (see step 4).

## 2. Build and smoke-test

```bash
uv run python scripts/build_app.py myapp            # dist/myapp/{clean,buggy}.apk
uv run python scripts/build_app.py myapp --smoke emulator-5554
```

The checkout is restored afterwards — patches never persist in the app's tree.

## 3. Derive the truth — never assert it

```bash
adb -s emulator-5554 install -r -g dist/myapp/buggy.apk
uv run python scripts/derive_truth.py myapp --device emulator-5554 --repeat 3
```

This runs every `check:` against the clean flags and the seeded flags and reports
what actually differs: `broken`, `ok`, `upstream` (broken both ways — not your
defect), `INVERTED` (your seeding *fixes* it — unsafe), `undecidable`, plus UNSTABLE
checks under `--repeat`. Fix every DISAGREE by editing `state:` to match reality —
including `collateral` for areas your patch breaks incidentally (opencalc's parser
patch broke three; scoring them as controls charged agents for measuring reality).
Results also land in `src/qualgentbench/data/truth/<tier>-stability.json` (with
`--tier`), which is version-controlled evidence, not configuration.

## 4. Serve the APK

Third parties resolve APKs in this order — pick whichever fits:
1. `QUALGENTBENCH_APK_MYAPP=/path/to/buggy.apk` (env var, per app id, uppercased)
2. a local `dist/myapp/buggy.apk` (what `build_app.py` produces; gitignored)
3. the spec's `apk:` block → HuggingFace dataset download, sha256-verified:
   ```yaml
   apk:
     repo: you/your-apps-dataset
     filename: medium/myapp-buggy.apk
     sha256: "..."
   ```

## 5. Gate before quoting a number

```bash
uv run qualgent-bench run --agent codex-cli --models gpt-5.5 \
  --app myapp --mode hunt --trials 1 --device emulator-5554   # at least one episode
uv run python scripts/check_controls.py --tier medium
uv run python scripts/check_tier_ready.py --tier medium       # must print READY
uv run python scripts/adversary_check.py                      # guessing must score <= 0
```

Note the gates read real episodes from `runs/` — a brand-new app cannot go green
without at least one device run, and editing `step_budget` invalidates prior
episodes as budget evidence. If your app is in a tier the CLI marks unready, it
still runs when named with `--app` (only `--tier` refuses unready tiers).

## Known sharp edges

- No schema validation on specs: a misspelled key (`shared_storge:`) is silently
  ignored. `build_app.py`'s cross-checks and `check_tier_ready` are the only
  structural gates, and both run late.
- The tier list is hardcoded (`easy|medium|hard` in `cli.py`); a new tier name is a
  code change.
- If the app IS a keyboard (an IME), replay cannot verify it: hierarchy dumps drop
  the active IME's windows by design.
