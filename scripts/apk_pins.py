#!/usr/bin/env python3
"""The APK pin manifest (`src/qualgentbench/data/apk-pins.json`): check it, backfill it
from the dataset's history, and fetch a build by its sha256.

    # the guard, no network: every committed apk: block is pinned or marked unpublished
    uv run python scripts/apk_pins.py check
    # ...and every pin really serves its bytes (read-only, one metadata call per pin)
    uv run python scripts/apk_pins.py check --remote

    # walk the dataset's commits and pin every published build (read-only; dry run)
    uv run python scripts/apk_pins.py backfill
    uv run python scripts/apk_pins.py backfill --write

    # a pinned build by hash, e.g. into an OLDER checkout whose own code reads no pins
    uv run python scripts/apk_pins.py fetch 5cea276f --out ../old/dist/fossify-calendar/buggy.apk

Why the manifest exists, and why it is not a `corpus_version` input:
`src/qualgentbench/apk_pins.py`. Nothing here writes to HuggingFace — uploads are
`scripts/publish_apk.py --upload`, an owner action, which records its own pin.

`backfill` records, for each sha256, the OLDEST dataset commit whose file holds it — the
upload that published it. It skips any file whose app has no spec or test-case file in
this checkout (a retired demo, a held-out app): a held-out app must not be named anywhere
under `data/`, and `scripts/holdout.py verify` would fail on it. Existing pins are never
overwritten.

`fetch` exists because a pin only helps code that reads it. A checkout that predates the
manifest downloads the path's HEAD and fails its sha256 check once a newer build is
uploaded; fetch the exact bytes with a CURRENT checkout (the manifest accumulates every
pin ever recorded) and drop them into the old checkout's `dist/<app>/buggy.apk`, which
wins over any download.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualgentbench import apk_pins  # noqa: E402

# `<app>-buggy.apk` (live) or `<app>-buggy-<sha256[:12]>.apk` (publish_apk.py --archive).
_APK_NAME = re.compile(r"^(?P<app>.+?)-buggy(?:-[0-9a-f]{12})?\.apk$")


def app_of(remote_path: str) -> str | None:
    m = _APK_NAME.match(Path(remote_path).name)
    return m.group("app") if m else None


def public_apps(data_root: Path | None = None) -> set[str]:
    """Apps this checkout carries a spec or a test-case file for. The PACKAGED tree only,
    never the held-out directory: the manifest is committed."""
    root = data_root or apk_pins.DATA_ROOT
    return ({p.stem for p in (root / "benchmarks").glob("*.yaml")}
            | {p.stem for p in (root / "test-cases").glob("*.yaml")})


def _api():
    from huggingface_hub import HfApi
    # Reads only. A token is optional for the public dataset; HF_TOKEN is honoured for a
    # private fork.
    return HfApi(token=os.environ.get("HF_TOKEN") or None)


# ── backfill ───────────────────────────────────────────────────────────────────

def history(api, repo: str, apps: set[str]) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """{sha256: (path, commit id)} — the OLDEST commit whose `.apk` file of a public app
    holds those bytes — and the set of `.apk` paths skipped as not public."""
    commits = list(api.list_repo_commits(repo, repo_type="dataset"))
    commits.reverse()  # the API answers newest first
    first: dict[str, tuple[str, str]] = {}
    skipped: set[str] = set()
    for c in commits:
        entries = api.list_repo_tree(repo, repo_type="dataset", revision=c.commit_id,
                                     recursive=True)
        for e in sorted(entries, key=lambda e: str(getattr(e, "path", ""))):
            path = str(getattr(e, "path", ""))
            sha = apk_pins.lfs_sha256(e)
            if not path.endswith(".apk") or not sha:
                continue
            if app_of(path) not in apps:
                skipped.add(path)
                continue
            first.setdefault(sha, (path, c.commit_id))
    return first, skipped


def backfill(api, repos: list[str], doc: dict, apps: set[str],
             out=print) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {"added": [], "kept": [], "skipped": []}
    for repo in repos:
        found, skipped = history(api, repo, apps)
        for sha, (path, rev) in sorted(found.items(), key=lambda kv: (kv[1][0], kv[0])):
            how = apk_pins.record_pin(doc, sha, repo=repo, filename=path, revision=rev)
            report[how].append(sha)
            out(f"  {'+' if how == 'added' else '='} {sha[:12]}…  {repo}:{path}@{rev[:12]}")
        report["skipped"].extend(sorted(skipped))
    return report


def cmd_backfill(args) -> int:
    doc = apk_pins.load()
    repos = args.repo or sorted({b["repo"] for b in apk_pins.committed_blocks() if b["repo"]})
    print(f"walking {', '.join(repos)} (read-only)")
    report = backfill(_api(), repos, doc, public_apps())
    print(f"\n{len(report['added'])} pin(s) new, {len(report['kept'])} already pinned; "
          f"{len(report['skipped'])} path(s) skipped — no spec or test-case file for their "
          f"app in this checkout, so they are not recorded here")
    _print_blocks(doc)
    if not args.write:
        print("\nDRY RUN — nothing written. Re-run with --write to update "
              f"{apk_pins.PINS_PATH.relative_to(ROOT)}.")
        return 0
    apk_pins.save(doc)
    print(f"\n✓ wrote {apk_pins.PINS_PATH.relative_to(ROOT)}")
    return 0


# ── check ──────────────────────────────────────────────────────────────────────

def _print_blocks(doc: dict) -> None:
    print("\ncommitted apk: blocks")
    for b in apk_pins.committed_blocks():
        state = apk_pins.status(b, doc)
        where = ""
        if state == "pinned":
            pin = doc["pins"][b["sha256"]]
            where = f"  → {pin['filename']}@{pin['revision'][:12]}"
        print(f"  {state:<12} {b['kind']:<8} {b['app']:<22} {b['sha256'][:12]}…{where}")


def remote_problems(api, doc: dict) -> list[str]:
    """Every pin whose revision does not serve its sha256 at its filename."""
    out = []
    for sha, pin in sorted(doc["pins"].items()):
        try:
            entries = api.get_paths_info(pin["repo"], [pin["filename"]],
                                         revision=pin["revision"], repo_type="dataset")
        except Exception as exc:  # noqa: BLE001 - report every way a pin can dangle
            out.append(f"pin {sha[:12]}…: {pin['filename']}@{pin['revision'][:12]} — "
                       f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
            continue
        served = next((apk_pins.lfs_sha256(e) for e in entries
                       if getattr(e, "path", None) == pin["filename"]), None)
        if served != sha:
            out.append(f"pin {sha[:12]}…: {pin['filename']}@{pin['revision'][:12]} serves "
                       f"{(served or 'nothing')[:12]}")
    return out


def cmd_check(args) -> int:
    doc = apk_pins.load()
    _print_blocks(doc)
    named = {b["sha256"] for b in apk_pins.committed_blocks()}
    stale = sorted(set(doc["unpublished"]) - named)
    for sha in stale:
        print(f"  note: {sha[:12]}… is marked unpublished but no committed block names it")
    problems = apk_pins.problems(doc)
    if args.remote:
        print(f"\nchecking {len(doc['pins'])} pin(s) against the Hub (read-only) …")
        problems += remote_problems(_api(), doc)
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    pinned = sum(1 for b in apk_pins.committed_blocks() if apk_pins.status(b, doc) == "pinned")
    print(f"\nOK — {pinned} committed block(s) pinned, "
          f"{len(named & set(doc['unpublished']))} marked unpublished, {len(doc['pins'])} "
          f"pin(s) in the manifest" + (", every one served by the Hub" if args.remote else ""))
    return 0


# ── fetch ──────────────────────────────────────────────────────────────────────

def resolve_sha(prefix: str, doc: dict) -> str:
    """A full sha256 from a pinned one's unique prefix (8+ hex, as the YAML notes quote
    them: `5cea276f…`)."""
    p = prefix.strip().rstrip("…").lower()
    if not re.fullmatch(r"[0-9a-f]{8,64}", p):
        sys.exit(f"{prefix!r} is not a sha256 or a prefix of at least 8 hex digits")
    hits = [s for s in doc["pins"] if s.startswith(p)]
    if not hits:
        mark = next((m for s, m in doc["unpublished"].items() if s.startswith(p)), None)
        why = (f" — it is marked unpublished ({mark.get('note', '')})" if mark else "")
        sys.exit(f"no pin for {p}{why}. `backfill` finds builds that are on the Hub.")
    if len(hits) > 1:
        sys.exit(f"{p} is ambiguous: {', '.join(h[:16] for h in hits)}")
    return hits[0]


def cmd_fetch(args) -> int:
    from qualgentbench.apps import fetch_seeded_apk
    doc = apk_pins.load()
    sha = resolve_sha(args.sha256, doc)
    pin = doc["pins"][sha]
    app = app_of(pin["filename"]) or "unknown"
    block = {"repo": pin["repo"], "filename": pin["filename"], "sha256": sha}
    got = fetch_seeded_apk(app, block, kind=f"pinned/{sha[:12]}")
    if args.out:
        dest = Path(args.out).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(got, dest)
        got = dest
    print(f"{sha}  {pin['repo']}:{pin['filename']}@{pin['revision'][:12]}\n  → {got}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="the guard: every committed block pinned or marked")
    c.add_argument("--remote", action="store_true",
                   help="also confirm every pin against the Hub (read-only)")
    b = sub.add_parser("backfill", help="pin every published build from the dataset history")
    b.add_argument("--repo", action="append",
                   help="dataset repo (repeatable; default: every repo a block names)")
    b.add_argument("--write", action="store_true", help="update apk-pins.json (default: dry run)")
    f = sub.add_parser("fetch", help="download a pinned build by sha256")
    f.add_argument("sha256", help="the sha256, or a unique prefix of 8+ hex digits")
    f.add_argument("--out", help="also copy it here (e.g. <old checkout>/dist/<app>/buggy.apk)")
    args = ap.parse_args(argv)
    return {"check": cmd_check, "backfill": cmd_backfill, "fetch": cmd_fetch}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
