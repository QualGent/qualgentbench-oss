# Contributing

Thanks for wanting to help. This page tells you what a good contribution looks
like and what we check before merging. If anything here is unclear, open an issue
and ask — that counts as contributing too.

## Before you start

- For a new **app**, **coding agent**, or **model**, read the matching guide first:
  [adding an app](docs/adding-an-app.md), [adding a coding agent](docs/adding-a-coding-agent.md),
  [adding a native model](docs/adding-a-native-model.md).
- For anything else, open an issue describing what you want to change and why,
  before writing a lot of code. It saves both of us time.

## The bar every PR must clear

```bash
uv run pytest -q                          # all tests green
uvx ruff check --select F821 src scripts  # no undefined names
```

Keep code comments short (1–3 lines) and only where the code can't speak for
itself. Match the style of the file you are editing.

## Two rules special to this repo

**1. Spec changes are ground-truth changes.** The files in
`src/qualgentbench/data/benchmarks/` are the answer key. If your PR touches one,
you must show it still tells the truth:

- run `scripts/derive_truth.py <app> --device <serial> --repeat 3` on a real
  device and include the output — a feature's `state` is measured, never typed in
  by hand;
- `scripts/check_tier_ready.py --tier <t>` must print READY and
  `scripts/adversary_check.py` must show every guesser scoring ≤ 0.

**2. Scorer and replayer changes invalidate old results.** If you touch scoring
constants or any file the replayer fingerprint covers (`replay.py`,
`episode_runner.py`, `submission.py`, `verify/`), say so in the PR — every
recorded `replay.json` will re-derive, and the adversary gate must still pass.
Read [docs/scoring.md](docs/scoring.md) before proposing a constant change.

## Reporting a bug in the harness

Include the episode's artifacts — `replay.json`, `result.json`, and the relevant
`interactions.json` fields. They are designed to make failures diagnosable without
your device; a report with them usually gets fixed in a day, one without them
starts with a week of guessing.

Found a way to **game the scoring**? Please report it privately to the
maintainers rather than in a public issue, so it can be fixed before it is used.

## License

By contributing you agree your contribution is licensed under the repo's
[Apache-2.0 license](LICENSE). App-source excerpts inside benchmark specs stay
under the upstream app's license — see [THIRD_PARTY.md](THIRD_PARTY.md).
