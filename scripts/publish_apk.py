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

**Pins** (`qualgentbench.apk_pins`, `data/apk-pins.json` — not a corpus input). `--upload`
records the commit the upload returns as the pin for the sha256, once the Hub confirms
that revision serves exactly those bytes, so `fetch_seeded_apk` keeps finding this build
after the next upload overwrites the path. `--write` without `--upload` marks the new
sha256 `unpublished` in the same file, which is what the guard in
`tests/test_apk_pins.py` accepts for a block no owner has uploaded yet. Commit
`apk-pins.json` together with the block.

**Archival publish** (`--archive --apk <historic build>`): upload and pin a build that no
current block names — a superseded build some board was measured against — so that
board stays reproducible. It goes to its own path,
`archive/<kind dir>/<app>-buggy-<sha256[:12]>.apk`, so it can never displace what the
live path serves, and it never touches an `apk:` block.

    HF_TOKEN=... uv run python scripts/publish_apk.py fossify-calendar --kind journey \\
        --archive --apk /path/to/old/buggy.apk --upload --yes
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

from qualgentbench import apk_pins, corpus  # noqa: E402

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


def archive_remote(app_id: str, kind: str, spec: dict, digest: str) -> str:
    """An archival build's own path: never the live `<dir>/<app>-buggy.apk`, so uploading
    it cannot change what an unpinned download of the live path gets."""
    return f"archive/{remote_dir(app_id, kind, spec)}/{app_id}-buggy-{digest[:12]}.apk"


def _block_kind(kind: str) -> str:
    return "journey" if kind == "journey" else "hunt"


def _upload(api, repo: str, remote: str, apk: Path) -> str:
    """Upload and return the commit id the Hub answered with — the revision that holds
    these bytes, and so the pin."""
    print(f"  uploading {apk} → {repo}:{remote} …")
    info = api.upload_file(path_or_fileobj=str(apk), path_in_repo=remote,
                           repo_id=repo, repo_type="dataset",
                           commit_message=f"publish {remote}")
    oid = str(getattr(info, "oid", "") or "")
    print(f"  ✓ uploaded (commit {oid[:12] or '?'})")
    return oid


def _pin_upload(api, repo: str, remote: str, digest: str, oid: str) -> None:
    """Record the pin, but only once the Hub confirms `remote@oid` serves `digest`. A pin
    that points at the wrong bytes would be worse than none: it outlives the upload."""
    recover = ("  The upload itself happened. Resolve the pin from the dataset's history "
               "(read-only):\n    uv run python scripts/apk_pins.py backfill --write")
    if not apk_pins.is_revision(oid):
        sys.exit(f"  ✗ the upload returned no commit id ({oid!r}) — the pin was NOT written.\n"
                 f"{recover}")
    entries = api.get_paths_info(repo, [remote], revision=oid, repo_type="dataset")
    served = next((apk_pins.lfs_sha256(e) for e in entries if getattr(e, "path", None) == remote),
                  None)
    if served != digest:
        sys.exit(f"  ✗ {repo}:{remote}@{oid[:12]} serves sha256 {served or 'nothing'}, not "
                 f"{digest} — the pin was NOT written.\n{recover}")
    doc = apk_pins.load()
    how = apk_pins.record_pin(doc, digest, repo=repo, filename=remote, revision=oid)
    apk_pins.save(doc)
    where = doc["pins"][digest]
    print(f"  ✓ pinned {digest[:12]}… → {where['repo']}:{where['filename']}@"
          f"{where['revision'][:12]} ({how}) in {apk_pins.PINS_PATH}")
    print("  COMMIT src/qualgentbench/data/apk-pins.json: until it is committed, only this "
          "checkout knows where the build lives.")


def _owner_api(args) -> tuple[object, str]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token:
        sys.exit("  --upload needs a write token in HF_TOKEN (owner action).")
    if not args.yes:
        sys.exit("  --upload needs --yes: this writes to the published dataset every "
                 "third-party clone downloads from.")
    from huggingface_hub import HfApi
    return HfApi(token=token), token


def _print_pin(digest: str, doc: dict) -> None:
    pin = doc["pins"].get(digest)
    mark = doc["unpublished"].get(digest)
    if pin:
        print(f"  pin     : {pin['repo']}:{pin['filename']}@{pin['revision'][:12]} "
              f"(retrievable whatever the path serves later)")
    elif mark:
        print(f"  pin     : none — marked unpublished ({mark.get('note', '')})")
    else:
        print("  pin     : none — these bytes are not known to be on HuggingFace")


def _archive(args, app_id: str, spec: dict, path: Path, current: dict, apk: Path,
             repo: str, digest: str, size: int) -> None:
    remote = archive_remote(app_id, args.kind, spec, digest)
    doc = apk_pins.load()
    print(f"{app_id}  kind={args.kind}  ARCHIVE")
    print(f"  local   : {apk}  ({size} bytes, {size / 1e6:.1f} MB)")
    print(f"  sha256  : {digest}")
    print(f"  remote  : {repo}:{remote}")
    _print_pin(digest, doc)
    if digest == str(current.get("sha256") or ""):
        sys.exit(f"  {path} names exactly this build — publish it normally "
                 f"(--write --upload), not as an archive.")
    if digest in doc["pins"]:
        print("  → already pinned; nothing to upload.")
        return
    if not args.upload:
        print("\n  DRY RUN — nothing uploaded, nothing pinned. With --upload --yes this "
              f"uploads to {remote} and pins the returned commit in "
              f"{apk_pins.PINS_PATH.name}; the apk: block in {path.name} is not touched.")
        return
    api, _ = _owner_api(args)
    oid = _upload(api, repo, remote, apk)
    _pin_upload(api, repo, remote, digest, oid)


def _mark_after_write(app_id: str, kind: str, remote: str, digest: str,
                      old_sha: str) -> None:
    """After `--write` moved a block to `digest`: mark it unpublished unless it is already
    pinned, and drop the mark of the sha256 the block moved away from when no committed
    block names it any more (a superseded build that was never uploaded)."""
    doc = apk_pins.load()
    changed = apk_pins.mark_unpublished(
        doc, digest, app=app_id, kind=_block_kind(kind), filename=remote,
        note="the committed block names these bytes; awaiting an owner upload "
             "(publish_apk.py --write --upload --yes)")
    if (old_sha and old_sha != digest and old_sha in doc["unpublished"]
            and not any(b["sha256"] == old_sha for b in apk_pins.committed_blocks())):
        doc["unpublished"].pop(old_sha)
        changed = True
    if changed:
        apk_pins.save(doc)
        print(f"  ✓ {apk_pins.PINS_PATH.name}: {digest[:12]}… marked unpublished — commit it "
              f"with the block (the guard in tests/test_apk_pins.py needs one or the other)")


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
                    help="OWNER ACTION: also upload to HuggingFace and pin the returned "
                         "commit in data/apk-pins.json. Requires --write (or --archive), "
                         "HF_TOKEN in the environment and --yes.")
    ap.add_argument("--archive", action="store_true",
                    help="publish and pin a historic build (--apk) under archive/, never "
                         "touching the apk: block or the live path")
    ap.add_argument("--yes", action="store_true", help="skip the --upload confirmation")
    args = ap.parse_args()

    if args.archive:
        if args.write:
            sys.exit("--archive never moves an apk: block (that is the point of it) — "
                     "drop --write.")
        if not args.apk:
            sys.exit("--archive needs --apk <historic build>: dist/<app>/buggy.apk is the "
                     "CURRENT build, which is published normally.")
    elif args.upload and not args.write:
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
    digest, size = sha256_of(apk), apk.stat().st_size

    if args.archive:
        _archive(args, app_id, spec, path, current, apk, repo, digest, size)
        return

    remote = f"{remote_dir(app_id, args.kind, spec)}/{app_id}-buggy.apk"
    updates = {"repo": repo, "filename": remote, "sha256": digest, "size_bytes": size}

    unchanged = all(str(current.get(k, "")) == str(v) for k, v in updates.items())
    print(f"{app_id}  kind={args.kind}")
    print(f"  local   : {apk}  ({size} bytes, {size / 1e6:.1f} MB)")
    print(f"  sha256  : {digest}")
    print(f"  remote  : {repo}:{remote}")
    print(f"  block in: {path}")
    _print_pin(digest, apk_pins.load())
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
    _mark_after_write(app_id, args.kind, remote, digest, str(current.get("sha256") or ""))

    if not args.upload:
        print("  --upload not given: the file was NOT uploaded. Until an owner uploads it, "
              "this block names bytes HuggingFace does not have (it is marked unpublished), "
              "so a fresh clone's download fails its sha256 check and dist/ is the artifact.")
        return

    api, _ = _owner_api(args)
    oid = _upload(api, repo, remote, apk)
    _pin_upload(api, repo, remote, digest, oid)


if __name__ == "__main__":
    main()
