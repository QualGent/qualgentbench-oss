#!/usr/bin/env python3
"""Publish a rebuilt seeded-bug APK: one reviewed step from `dist/<app>/buggy.apk` to
the `apk:` block the harness verifies against.

`apps.fetch_seeded_apk` sha256-checks every download, so the file on HuggingFace and
the `apk:` block in the corpus are ONE fact. Uploading a rebuild without updating the
block breaks every fresh clone with a hash mismatch; updating the block without
uploading breaks it the same way. This script is the only place that moves both, which
is why `--upload` refuses to run without `--write`.

Dry run by DEFAULT — it prints the target path, sha256 and size and the exact `apk:`
block it would write, and changes nothing. `--write` edits the YAML in place (a
targeted line rewrite, never a yaml round-trip: these files are comment-heavy and the
comments are the authoring record). `--upload` is an OWNER action: it needs `--write`,
an `HF_TOKEN` in the environment and an explicit `--yes`.

    # look
    uv run python scripts/publish_apk.py medtimer --kind journey
    # land the hash locally, review the diff, commit
    uv run python scripts/publish_apk.py medtimer --kind journey --write
    # owner, once the rebuild has been re-derived (docs/adding-an-app.md)
    HF_TOKEN=... uv run python scripts/publish_apk.py medtimer --kind journey \\
        --write --upload --yes

Changing a published hash is not a cosmetic edit: the journey `apk:` block is part of
`corpus.corpus_version()`, so every board measured against the old build is a
different measurement, and `derive_journey.py` has to agree 5/5 against the NEW APK
before the block moves. The script refuses to guess that for you — it prints the
gate and expects you to have run it.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualgentbench import corpus  # noqa: E402

# A journey build is published under journey/; a hunt build under its tier. `--kind`
# accepts the tier name directly (the ticket-level vocabulary, `--kind hard`) and
# validates it against the spec, so a typo cannot publish a hard APK under medium/.
TIERS = ("easy", "medium", "hard")
KINDS = ("journey", "hunt", *TIERS)

_BLOCK_KEYS = ("repo", "filename", "sha256", "size_bytes")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def target_file(app_id: str, kind: str) -> Path:
    """Where the `apk:` block for this kind lives. Journey mode reads the TEST-CASE
    file's block (`cli.py:387`), hunt mode the benchmark spec's (`cli.py:397`) — two
    different files, two different builds, and writing the wrong one silently leaves
    the other arm on the old APK."""
    if kind == "journey":
        return corpus.resolve(f"test-cases/{app_id}.yaml", app_id=app_id)
    return corpus.spec_path(app_id)


def remote_dir(app_id: str, kind: str, spec: dict) -> str:
    difficulty = str((spec.get("app") or {}).get("difficulty") or "").strip()
    if kind == "journey":
        return "journey"
    if kind in TIERS:
        if difficulty and difficulty != kind:
            sys.exit(f"{app_id} is tier {difficulty!r}, not {kind!r} — "
                     f"publishing it under {kind}/ would put it where nothing looks for it.")
        return kind
    if not difficulty:
        sys.exit(f"{app_id} has no app.difficulty; pass --kind {'|'.join(TIERS)} explicitly.")
    return difficulty


def read_block(path: Path) -> dict:
    doc = _load(path)
    return dict(doc.get("apk") or {})


def render_block(block: dict) -> str:
    return "apk:\n" + "".join(f"  {k}: {block[k]}\n" for k in _BLOCK_KEYS if k in block)


def rewrite_block(text: str, updates: dict[str, object]) -> str:
    """Rewrite the `apk:` block's scalar keys in place, keeping every comment and every
    key the block already carries. A yaml round-trip would be shorter and would delete
    the authoring commentary these files exist to carry."""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "apk:"), None)
    if start is None:
        sys.exit("no top-level `apk:` block in the target file — add one by hand first.")
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
        end += 1
    body = lines[start + 1:end]

    pending = dict(updates)
    for i, ln in enumerate(body):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):(\s*)(.*)$", ln)
        if not m or m.group(2) not in pending:
            continue
        key = m.group(2)
        comment = ""
        if "#" in m.group(4):
            comment = "  " + m.group(4)[m.group(4).index("#"):].rstrip()
        body[i] = f"{m.group(1)}{key}: {pending.pop(key)}{comment}"
    # A key the block never had (a first publish) is appended in canonical order.
    for key in _BLOCK_KEYS:
        if key in pending:
            body.append(f"  {key}: {pending.pop(key)}")
    if pending:
        sys.exit(f"cannot place {', '.join(sorted(pending))} in the apk: block")

    return "\n".join(lines[:start + 1] + body + lines[end:])


def _upload(repo: str, remote: str, apk: Path, token: str) -> None:
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    print(f"  uploading {apk} → {repo}:{remote} …")
    api.upload_file(path_or_fileobj=str(apk), path_in_repo=remote,
                    repo_id=repo, repo_type="dataset",
                    commit_message=f"publish {remote}")
    print("  ✓ uploaded")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("app_id")
    ap.add_argument("--kind", default="journey", choices=KINDS,
                    help="journey = the test-case file's apk: block (journey/ on HF); "
                         "hunt or a tier name = the benchmark spec's (tier dir on HF)")
    ap.add_argument("--apk", help="APK to publish (default dist/<app>/buggy.apk)")
    ap.add_argument("--repo", help="HuggingFace dataset repo (default: the block's own)")
    ap.add_argument("--write", action="store_true",
                    help="apply the apk: block edit to the target YAML (default: dry run)")
    ap.add_argument("--upload", action="store_true",
                    help="OWNER ACTION: also upload to HuggingFace. Requires --write, "
                         "HF_TOKEN in the environment and --yes.")
    ap.add_argument("--yes", action="store_true", help="skip the --upload confirmation")
    args = ap.parse_args()

    if args.upload and not args.write:
        sys.exit("--upload without --write would publish bytes no `apk:` block names — "
                 "every fresh clone would then fail its sha256 check. Pass both.")

    app_id = args.app_id
    spec = _load(corpus.spec_path(app_id))
    if not spec:
        sys.exit(f"no benchmark spec for {app_id}")

    path = target_file(app_id, args.kind)
    if not path.is_file():
        sys.exit(f"{app_id}: no {args.kind} file at {path}")
    current = read_block(path)
    if current.get("path"):
        sys.exit(f"{app_id} is HELD OUT (`apk: path:` in {path}). A held-out APK is never "
                 f"published — see docs/heldout.md. Copy the build into the held-out "
                 f"directory and update its sha256 there instead.")

    apk = Path(args.apk).expanduser() if args.apk else ROOT / "dist" / app_id / "buggy.apk"
    if not apk.is_file():
        sys.exit(f"no APK at {apk} — build it first:\n"
                 f"  uv run python scripts/build_app.py {app_id} --buggy --smoke <serial>")

    repo = args.repo or str(current.get("repo") or "")
    if not repo:
        sys.exit(f"{path} has no apk.repo and --repo was not given.")
    remote = f"{remote_dir(app_id, args.kind, spec)}/{app_id}-buggy.apk"
    digest, size = sha256_of(apk), apk.stat().st_size
    updates = {"repo": repo, "filename": remote, "sha256": digest, "size_bytes": size}

    unchanged = all(str(current.get(k, "")) == str(v) for k, v in updates.items())
    print(f"{app_id}  kind={args.kind}")
    print(f"  local   : {apk}  ({size} bytes, {size / 1e6:.1f} MB)")
    print(f"  sha256  : {digest}")
    print(f"  remote  : {repo}:{remote}")
    print(f"  block in: {path}")
    if unchanged:
        print("  → identical to the published block; nothing to change.")
    else:
        for key in _BLOCK_KEYS:
            was, now = str(current.get(key, "—")), str(updates[key])
            if was != now:
                print(f"  {key}: {was} → {now}")

    before = path.read_text()
    after = rewrite_block(before, updates)
    diff = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                     fromfile=str(path), tofile=str(path) + " (new)", n=2))

    if not args.write:
        print("\n  DRY RUN — nothing written, nothing uploaded.")
        print("  the block that would be written:\n")
        for ln in render_block({**current, **updates}).rstrip("\n").split("\n"):
            print(f"    {ln}")
        if diff:
            print("\n  diff:")
            for ln in diff:
                print(f"    {ln.rstrip()}")
        print("\n  re-run with --write to apply it locally; --upload is a separate, "
              "owner-only step.")
        return

    if diff:
        path.write_text(after)
        print(f"\n  ✓ wrote {path}")
        if args.kind == "journey":
            print("  NOTE: the journey apk: block is part of corpus_version() — every board "
                  "measured against the old APK is now a different corpus. Re-derive before "
                  "quoting a number:\n"
                  f"    uv run python scripts/derive_journey.py {app_id} --device <serial> --repeat 3")
    else:
        print("\n  nothing to write.")

    if not args.upload:
        print("  --upload not given: the file was NOT uploaded. The published APK still has "
              "the old bytes, so this checkout's hash will not verify until an owner uploads.")
        return

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token:
        sys.exit("  --upload needs a write token in HF_TOKEN (owner action).")
    if not args.yes:
        sys.exit("  --upload needs --yes: this overwrites the published artifact every "
                 "third-party clone downloads.")
    _upload(repo, remote, apk, token)


if __name__ == "__main__":
    main()
