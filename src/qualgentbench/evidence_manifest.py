"""evidence/manifest.json: a sha256 per file plus a chain over the step stream so
edits, reorders and deletions are detectable after publication. Written by the
producer itself, so it proves nothing against them without an external anchor."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"

# Derived or self-referential, so not covered.
_EXCLUDED = {MANIFEST_NAME, "index.html"}

# Files outside evidence/ a submission must be checkable against: the transcript
# is what re-scoring runs over, and instruction_sent.md is what exposes a prompt
# that named the bug.
_SIBLINGS = (
    "../agent/transcript.txt",
    "../result.json",
    "../instruction_sent.md",
    "../mcp_config.json",
)


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _covered(evidence_dir: Path) -> list[Path]:
    return sorted(
        p for p in evidence_dir.rglob("*")
        if p.is_file() and p.relative_to(evidence_dir).as_posix() not in _EXCLUDED
    )


def steps_chain(steps_file: Path) -> tuple[int, str]:
    """(step count, head digest), or (0, "") if unreadable. Chained so the head
    commits to order as well as content."""
    try:
        raw = steps_file.read_bytes()
    except OSError:
        return 0, ""
    head = hashlib.sha256(b"").hexdigest()
    count = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        head = hashlib.sha256(head.encode() + line).hexdigest()
        count += 1
    return count, head


def write_manifest(evidence_dir: Path) -> Path | None:
    """Write ``manifest.json`` over an existing bundle. Returns the path, or None."""
    try:
        files: dict[str, Any] = {}
        for path in _covered(evidence_dir):
            files[path.relative_to(evidence_dir).as_posix()] = {
                "sha256": _digest(path),
                "bytes": path.stat().st_size,
            }
        for name in _SIBLINGS:
            sibling = evidence_dir / name
            if sibling.is_file():
                files[name] = {"sha256": _digest(sibling), "bytes": sibling.stat().st_size}
        count, head = steps_chain(evidence_dir / "steps.jsonl")

        out = evidence_dir / MANIFEST_NAME
        out.write_text(json.dumps({
            "manifest_version": MANIFEST_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "algorithm": "sha256",
            "steps": {"count": count, "head": head,
                      "chain": "h(n) = sha256(h(n-1) + line(n)), h(0) = sha256(b'')"},
            "files": files,
        }, indent=2))
        return out
    except OSError as exc:
        logger.warning("evidence: manifest not written for %s: %s", evidence_dir, exc)
        return None


def verify_bundle(evidence_dir: Path) -> dict[str, Any]:
    """Re-hash a bundle against its manifest. Returns ok/changed/missing/extra/
    steps_ok; a file added after the fact is as much an edit as an altered one."""
    manifest_path = evidence_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"no readable manifest: {exc}",
                "changed": [], "missing": [], "extra": [], "steps_ok": False}

    recorded = manifest.get("files") or {}
    present = {p.relative_to(evidence_dir).as_posix() for p in _covered(evidence_dir)}

    changed, missing = [], []
    for name, entry in sorted(recorded.items()):
        path = evidence_dir / name
        if not path.is_file():
            missing.append(name)
        elif _digest(path) != entry.get("sha256"):
            changed.append(name)

    extra = sorted(present - set(recorded))

    count, head = steps_chain(evidence_dir / "steps.jsonl")
    steps = manifest.get("steps") or {}
    steps_ok = bool(head) and head == steps.get("head") and count == steps.get("count")

    return {
        "ok": not (changed or missing or extra) and steps_ok,
        "changed": changed,
        "missing": missing,
        "extra": extra,
        "steps_ok": steps_ok,
        "steps_head": head,
        "files_checked": len(recorded),
    }
