# The held-out split and the corpus version

A public benchmark corpus is trainable-on. Every journey test case in this repository —
its route, its oracle, which seeded bug it carries, the symptom vocabulary that scores a
report — can be read by anyone, and therefore by any model's training run. A board over
the public corpus alone cannot tell "the agent found the bug" from "the model has seen
the answer key".

The held-out split is the control: **two of the eight journey apps whose files live
outside the repository**, run alongside every board, printed as their own block. A model
tuned on the public cases cannot have seen them. Which two apps is the corpus owner's
decision and is written down nowhere in the repository — not in a manifest, not in a
test, not in a comment. `scripts/holdout.py` is the mechanism; it does not choose.

Beside it, every journey number carries a **corpus version**: a short content hash over
the files that define the journey key, so two boards can be told apart before anyone
compares them.

## What constitutes "an app" for hold-out

Journey mode reads an app from four places, and all four leave together
(`corpus.app_files`):

| File | Why it is part of the key |
|---|---|
| `src/qualgentbench/data/test-cases/<app>.yaml` | the cases: routes, oracles, `bugs:` per case, defect symptom vocabulary and markers, and the `apk:` block that names the journey build |
| `src/qualgentbench/data/truth/journey-<app>.json` | the measured key: the exact screen strings each bug changes (`derive_journey.py`) |
| `src/qualgentbench/data/benchmarks/<app>.yaml` | the spec: app identity, `device_setup` (fixtures), and every seeded defect's **patch** — the spec is where the defects are defined, not just referenced |
| `assets/<…>` listed in the spec's `device_setup.push` | the seeded start state (databases, prefs) |

One consequence is deliberate: all eight journey apps are also hard-tier hunt apps, and a
held-out app **leaves the hunt tier too** — the spec carries the hunt exploration
features, and the app's name cannot stay anywhere under `data/`. The move also drops the
app's entry from `truth/<tier>-stability.json` (hunt-side derived truth keyed by app id)
and parks it beside the held-out files. An asset another spec also pushes is copied out
but kept in the repository.

The APKs move too. A debug APK carries its flag ids (`QgbFlags.on("<defect-id>")`) as
plain strings, so a held-out build is never published. Its `apk:` block carries `path:`
instead of `repo:` + `filename:`, relative to the held-out directory, with the same
`sha256:`:

```yaml
apk:
  path: apks/journey/<app>-buggy.apk      # held out: never published
  sha256: <64 hex>
  size_bytes: <n>
```

`apps.fetch_seeded_apk` reads it in place and verifies the hash; it never downloads a
path block, never resolves it against the repository, and fails loudly when
`QGB_HELDOUT_DIR` is unset, the file is missing, or the hash differs, rather than
falling back to a public build. The hunt build in `benchmarks/<app>.yaml` follows the same
shape under `apks/hard/`. A local `dist/` copy or `QUALGENTBENCH_APK_<ID>` still wins.
Copies of these builds published before an app moved stay in the public dataset's history
until they are removed there.

## The held-out directory

Same layout as `src/qualgentbench/data/`, plus `assets/` mirroring the repository root:

```text
$QGB_HELDOUT_DIR/
  test-cases/<app>.yaml
  truth/journey-<app>.json
  truth/hard-stability.<app>.json      # the parked stability entry
  benchmarks/<app>.yaml
  assets/<app>/…
```

Location, in order: `QGB_HELDOUT_DIR`; `heldout_dir:` in the run config (relative to the
config file; it sets the env var unless one is already set); `heldout/` at the repository
root, which is gitignored so a split kept beside the repo cannot be committed by
accident. `scripts/holdout.py move` writes there when nothing else is set.

Resolution order in the harness (`corpus.resolve`, `journey.cases_path`,
`journey.truth_path`, `bugs.load_apps`, `corpus.asset_path`): **held-out directory
first, then the packaged data.** An app is held out iff its test-case file lives in the
held-out directory — one definition, used by every loader and by the episode stamp. A
truth path for a held-out app points into the held-out directory even before the file
exists there, so `derive_journey.py` can never write a derived key back into the
repository.

## The never-committed rule

The removal from the repository is a commit. The destination is never one — not in this
repository, not in a fork, not in a "private copy of the corpus" branch. No file in the
repository may name which apps are held out: `holdout.py verify` greps every file name
and file body under `src/qualgentbench/data/` and `tests/fixtures/` for each held-out app
id as a token and fails on any hit. Run it in CI wherever the split exists; where it does
not (the public repository's own CI) it has nothing to check and exits 0.

The contamination canary (`QGB-CANARY-…`, first line of every case file) stays as it is:
it detects an agent that READ the key at run time, which is a different leak from a model
that was trained on it.

## Moving an app, running a board with the split

```bash
uv run python scripts/holdout.py move <app>          # git rm + copy to heldout/ (or $QGB_HELDOUT_DIR)
git commit -m "corpus: hold out <app>"               # the removal
uv run python scripts/holdout.py list                # what is public, what is held out, both versions
QGB_HELDOUT_DIR=/path/to/heldout uv run python scripts/holdout.py verify

# a board: the held-out apps run like any other app once the env var is set
QGB_HELDOUT_DIR=/path/to/heldout uv run qualgent-bench run --mode journey \
    --agent codex-cli --models gpt-5.5 --app <public-app>,<held-out-app> --device emulator-5554
```

### Where the split lives

The canonical copy is a private, versioned, KMS-encrypted S3 bucket, defined in the
private infrastructure repository and applied by its owner (module
`benchmark-heldout-bucket`, production only: one canonical copy, because a copy per
environment would be two sources for the same answer key). Board runners assume its
read-only role and sync the prefix; curators assume the maintainer role to upload:

```bash
aws s3 sync s3://<heldout-bucket>/qualgentbench/heldout/ ./heldout/ --delete   # runner
aws s3 sync ./heldout/ s3://<heldout-bucket>/qualgentbench/heldout/            # curator
QGB_HELDOUT_DIR=$PWD/heldout uv run python scripts/holdout.py verify
```

Versioning keeps every overwritten answer key recoverable for a year; object versions
cannot be deleted except by a named break-glass principal. Record the split version
`holdout.py list` prints next to any board that includes held-out rows.

Or in `bench.config.yaml`: `heldout_dir: ../heldout`. `preflight` checks that the
directory exists and holds at least one app. In Docker, mount the directory read-only and
pass `-e QGB_HELDOUT_DIR=/heldout` (the launcher does not do this for you; the image
must not bake the split in).

Held-out episodes carry `heldout: true` in `result.json` and the board prints them as a
separate block under the public one — own table, own numbering (`H1`, `H2`…), own rates
table — never blended into a public row:

```text
             Test-case runs — bug finding (ranked) and completion
 #  Agent + Model        Arm  Episodes  Cut  Done clean  Done seeded  Completion  Bugs found  False rep.  Prec.  Recall  F1   Steps
 1  codex-cli · gpt-5.5  mcp        24    1        9/12        10/12         79%       10/14           3    77%     71%  74%     31
corpus ef8c9be8e47f

             Held-out (2 apps) — never blended into the public rows
 #   Agent + Model        Arm  Episodes  Cut  Done clean  Done seeded  Completion  Bugs found  False rep.  Prec.  Recall  F1   Steps
 H1  codex-cli · gpt-5.5  mcp         8    0         3/4          3/4         75%         3/6           2    60%     50%  55%     35
held-out 3c1d9a0f77e2
```

A large gap between the two blocks for the same agent is the number the split exists to
produce.

## The corpus version

`corpus.corpus_version()` = the first 12 hex digits of sha256 over, for every file
matching `test-cases/*.yaml` and `truth/journey-*.json` under the packaged data
directory, sorted by relative path: `path \0 bytes \0`. The `apk:` blocks live inside the
case files and are covered. File mtimes, permissions and the absolute location do not
enter; a one-byte edit, a rename or a dropped file changes it. `heldout_version()` is the
same hash over the held-out directory (`None` when no split is configured).

Where it is stamped:

| Where | Keys |
|---|---|
| every journey `result.json` → `metrics` | `corpus_version`, `heldout_version`, `heldout` |
| every `journey.summary` row (and `board.json` → `journey_summary`) | `corpus_version` (set only when every episode in the row agrees), `corpus_versions`, `corpus_unstamped`, `heldout_version`, `heldout_versions`, `heldout`, `heldout_apps`, `mixed_corpus` |
| `runs/_runs/<run_id>/plan.json` and `board.json` | `corpus_version`, `heldout_version`, `heldout_apps` |
| the run header box | `Corpus: <version>  held-out: <version> (N apps: …)` |
| `scripts/rescore_journey.py` | per episode: `recorded <v> ≠ current <v>` |

**Boards with different corpus versions are not comparable.** The version changes when a
symptom word, a marker, a route or a truth string changes — each of which moves scores
without any agent changing. A row whose episodes were scored against more than one
version (or that mixes stamped and unstamped episodes) is starred `*` in the printed
table with the footnote `mixed corpus versions — not comparable`, and carries
`corpus_version: null` with the list in `corpus_versions`. A rescore keeps the recorded
version (it merges over the old metrics), which is why `rescore_journey.py` prints the
recorded and current versions side by side: a rescore across a corpus edit is a different
measurement, not a correction.

The `environment` fingerprint in `plan.json` (`checkpoint.environment_fingerprint`)
hashes the parsed spec, case and truth documents per app and exists for the resume
compatibility check; the corpus version hashes the files as one and exists for the
reader. They answer different questions and are computed differently on purpose.
