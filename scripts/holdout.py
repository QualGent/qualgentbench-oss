#!/usr/bin/env python3
"""The held-out split: move an app out of the repository, list the split, verify it.

Two of the eight journey apps live OUTSIDE the repository (docs/heldout.md), so a model
trained on the public corpus cannot have seen their cases. This is the only tool that
moves an app; nothing here decides WHICH apps — that is the corpus owner's call.

    uv run python scripts/holdout.py move <app>      # git rm + copy to the held-out dir
    uv run python scripts/holdout.py list            # what is public, what is held out
    uv run python scripts/holdout.py verify          # loads, hashes, no name leaked back

What moves for `<app>` (`corpus.app_files`): `test-cases/<app>.yaml`,
`truth/journey-<app>.json`, `benchmarks/<app>.yaml` (the spec: identity, device_setup and
the seeded-defect PATCHES — the app leaves the hunt tier too), and every
`device_setup.push` source under `assets/`. An asset another spec also pushes is copied,
not removed. The app's entry in `truth/<tier>-stability.json` (hunt-side derived truth,
keyed by app id) is dropped, or its name would stay in the repository.

The destination is `QGB_HELDOUT_DIR`, else `--heldout-dir`, else `heldout/` at the repo
root (gitignored). The removal must be COMMITTED; the destination must NEVER be.

`verify` is the CI-able guard: every held-out app loads through the same code the
runner uses, the held-out version hashes, and no held-out app id appears — as a token —
in any file name or file body under `src/qualgentbench/data/` or `tests/fixtures/`.
Without a held-out directory it has nothing to check and exits 0: the repository does
not know, and must not know, which apps are held out.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from qualgentbench import corpus, journey  # noqa: E402

STABILITY_GLOB = "truth/*-stability.json"


class HoldoutError(Exception):
    pass


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], check=check,
                          capture_output=True, text=True)


def _is_git_repo(repo_root: Path) -> bool:
    try:
        return _git(repo_root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _tracked(repo_root: Path, path: Path) -> bool:
    try:
        _git(repo_root, "ls-files", "--error-unmatch", "--", str(path))
        return True
    except subprocess.CalledProcessError:
        return False


def _remove(repo_root: Path, path: Path, use_git: bool) -> str:
    """Remove `path` from the working tree — through git when it is tracked, so the
    deletion is staged and cannot be forgotten; a plain unlink otherwise."""
    if use_git and _tracked(repo_root, path):
        _git(repo_root, "rm", "-q", "--", str(path))
        return "git rm"
    path.unlink()
    return "rm"


# ── move ───────────────────────────────────────────────────────────────────────

def move(app_id: str, *, repo_root: Path, data_root: Path, heldout_root: Path,
         out=print) -> list[str]:
    """Relocate `app_id` out of the repository. Returns the relative paths moved."""
    files = corpus.app_files(app_id, data_root, repo_root)
    cases_rel = f"test-cases/{app_id}.yaml"
    if not files[cases_rel].is_file():
        if (heldout_root / cases_rel).is_file():
            raise HoldoutError(f"{app_id} is already held out at {heldout_root}")
        raise HoldoutError(f"{app_id}: no {data_root / cases_rel} — not a journey app")
    if heldout_root.resolve() == data_root.resolve() or heldout_root.resolve().is_relative_to(data_root.resolve()):
        raise HoldoutError(f"held-out dir {heldout_root} is inside the packaged data dir")
    if not (repo_root / ".gitignore").is_file() or "heldout" not in (repo_root / ".gitignore").read_text():
        out(f"warning: {repo_root / '.gitignore'} does not mention heldout/ — make sure "
            f"{heldout_root} can never be committed")

    shared = corpus.shared_push_sources(app_id, data_root)
    use_git = _is_git_repo(repo_root)
    moved: list[str] = []
    for rel, src in files.items():
        if not src.exists():
            out(f"  skip   {rel} (absent)")
            continue
        dest = heldout_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise HoldoutError(f"{dest} already exists — refusing to overwrite")
        shutil.copy2(src, dest)
        if rel in shared:
            out(f"  copy   {rel}  (shared with another spec — kept in the repo)")
        else:
            how = _remove(repo_root, src, use_git)
            out(f"  move   {rel}  ({how})")
        moved.append(rel)

    # TODO(QUA-2770 follow-up): data/apk-pins.json names a published app in every pin's
    # `filename` (and in its `unpublished` marks), so after a move `verify` reports it as
    # a leak. Strip the app's pins and marks here, the way the stability entry below is
    # stripped, and park them beside the split. No app has been held out since the
    # manifest was added, so nothing is leaking today.
    # Hunt-side derived truth names the app by id; drop that entry so the name leaves.
    for tp in sorted(data_root.glob(STABILITY_GLOB)):
        doc = json.loads(tp.read_text())
        if isinstance(doc, dict) and app_id in doc:
            entry = doc.pop(app_id)
            tp.write_text(json.dumps(doc, indent=1) + "\n")
            side = heldout_root / "truth" / f"{tp.stem}.{app_id}.json"
            side.write_text(json.dumps({app_id: entry}, indent=1) + "\n")
            out(f"  strip  truth/{tp.name} entry {app_id!r} (kept at {side.relative_to(heldout_root)})")
            moved.append(f"truth/{tp.name}#{app_id}")

    out("")
    out(f"{app_id}: {len(moved)} file(s) now under {heldout_root}")
    out("REMINDER: commit the removal from the repository (git status shows the staged "
        "deletions) — and NEVER commit the destination. The held-out directory is not "
        "part of the repo, and no file in the repo may name which apps it holds.")
    out(f"Next: QGB_HELDOUT_DIR={heldout_root} uv run python scripts/holdout.py verify")
    return moved


# ── list ───────────────────────────────────────────────────────────────────────

def listing(*, data_root: Path, heldout_root: Path | None) -> dict:
    return {
        "public": corpus.apps_in(data_root),
        "public_version": corpus.version_of(data_root),
        "heldout_dir": str(heldout_root) if heldout_root else None,
        "heldout": corpus.apps_in(heldout_root) if heldout_root else [],
        "heldout_version": corpus.version_of(heldout_root) if heldout_root and heldout_root.is_dir() else None,
    }


# ── verify ─────────────────────────────────────────────────────────────────────

def _token_re(app_id: str) -> re.Pattern[bytes]:
    # Token boundaries on [A-Za-z0-9_] only: `orgzly-create-and-search` in a public file
    # IS a mention of orgzly by name, `xorgzly` is not.
    return re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(app_id.encode()) + rb"(?![A-Za-z0-9_])")


def leaks(app_ids: list[str], roots: list[Path], base: Path | None = None) -> list[str]:
    """Every (file, app id) where a held-out app id appears in a file NAME or BODY under
    `roots`. Bytes, so binary fixtures are searched too. Paths print relative to `base`."""
    found: list[str] = []
    pats = {a: _token_re(a) for a in app_ids}
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(base) if base and p.is_relative_to(base) else p
            name = p.name.encode()
            try:
                body = p.read_bytes()
            except OSError:
                body = b""
            for a, pat in pats.items():
                if pat.search(name):
                    found.append(f"{rel}: file name contains {a!r}")
                elif pat.search(body):
                    found.append(f"{rel}: mentions {a!r}")
    return found


def verify(*, repo_root: Path, data_root: Path, heldout_root: Path | None,
           out=print) -> int:
    """0 when the split loads, hashes and has not leaked back; 1 otherwise."""
    if heldout_root is None or not heldout_root.is_dir():
        out(f"no held-out directory ({corpus.HELDOUT_ENV} unset or missing) — nothing to verify")
        return 0
    apps = corpus.apps_in(heldout_root)
    problems: list[str] = []
    if not apps:
        problems.append(f"{heldout_root} holds no test-cases/<app>.yaml")
    for app_id in apps:
        files = corpus.app_files(app_id, heldout_root, heldout_root)
        try:
            doc = yaml.safe_load(files[f"test-cases/{app_id}.yaml"].read_text())
            defects = journey.load_defects(doc or {})
            cases = (doc or {}).get("test_cases") or []
            if not cases:
                problems.append(f"{app_id}: test-case file has no test_cases")
            for case in cases:
                journey.case_design(case, defects)
            if not (doc or {}).get("apk"):
                problems.append(f"{app_id}: test-case file has no `apk:` block — the journey build "
                                f"cannot be fetched")
        except Exception as exc:  # noqa: BLE001 - report every way a file can be unusable
            problems.append(f"{app_id}: test-case file does not load: {exc}")
        truth = files[f"truth/journey-{app_id}.json"]
        if truth.is_file():
            try:
                json.loads(truth.read_text())
            except ValueError as exc:
                problems.append(f"{app_id}: truth file is not JSON: {exc}")
        else:
            out(f"warning: {app_id}: no truth/journey-{app_id}.json — derive it with "
                f"scripts/derive_journey.py before running a board")
        spec = files[f"benchmarks/{app_id}.yaml"]
        if not spec.is_file():
            problems.append(f"{app_id}: no benchmarks/{app_id}.yaml in the held-out dir — journey "
                            f"mode needs the spec (identity, device_setup); did the move copy it?")
        for src in corpus.push_sources(spec):
            if not (heldout_root / src).exists() and not (repo_root / src).exists():
                problems.append(f"{app_id}: device_setup push source {src} is missing from "
                                f"both the held-out dir and the repo")
    version = corpus.version_of(heldout_root)
    out(f"held-out dir {heldout_root}: {len(apps)} app(s) {', '.join(apps)} · version {version}")

    leaked = leaks(apps, [data_root, repo_root / "tests" / "fixtures"], base=repo_root)
    for line in leaked:
        problems.append(f"leak: {line}")
    for pr in problems:
        out(f"FAIL {pr}")
    if problems:
        out(f"{len(problems)} problem(s)")
        return 1
    out("OK — held-out split loads, hashes, and no held-out app id appears under "
        f"{data_root.relative_to(repo_root) if data_root.is_relative_to(repo_root) else data_root} "
        f"or tests/fixtures/")
    return 0


# ── main ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-root", type=Path, default=REPO_ROOT,
                    help="repository root (tests point this at a temp copy)")
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="packaged data dir (default <repo-root>/src/qualgentbench/data)")
    ap.add_argument("--heldout-dir", type=Path, default=None,
                    help=f"held-out dir (default ${corpus.HELDOUT_ENV}, else <repo-root>/heldout)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mv = sub.add_parser("move", help="relocate an app out of the repository")
    mv.add_argument("app")
    sub.add_parser("list", help="public and held-out apps with their versions")
    sub.add_parser("verify", help="the held-out dir loads, hashes, and has not leaked back")
    args = ap.parse_args(argv)

    repo_root = args.repo_root.resolve()
    data_root = (args.data_dir or repo_root / "src" / "qualgentbench" / "data").resolve()
    env = os.environ.get(corpus.HELDOUT_ENV, "").strip()
    heldout_root = (args.heldout_dir or (Path(env) if env else None))
    if heldout_root is not None:
        heldout_root = heldout_root.expanduser().resolve()

    try:
        if args.cmd == "move":
            dest = heldout_root or corpus.default_heldout_dir(repo_root)
            move(args.app, repo_root=repo_root, data_root=data_root, heldout_root=dest)
            return 0
        if args.cmd == "list":
            info = listing(data_root=data_root,
                           heldout_root=heldout_root or corpus.default_heldout_dir(repo_root))
            print(f"public   ({len(info['public'])}): {', '.join(info['public']) or '—'}"
                  f"   version {info['public_version']}")
            print(f"held-out ({len(info['heldout'])}): {', '.join(info['heldout']) or '—'}"
                  f"   version {info['heldout_version']}   dir {info['heldout_dir']}")
            return 0
        return verify(repo_root=repo_root, data_root=data_root,
                      heldout_root=heldout_root or (
                          corpus.default_heldout_dir(repo_root)
                          if corpus.default_heldout_dir(repo_root).is_dir() else None))
    except HoldoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
