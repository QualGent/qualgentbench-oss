"""No private creation-surface text is committed to this PUBLIC repository (QUA-2852).

CreateBench v2 runs the qualgent-test-creator template (private DevLoop-MCP) and the
QualGent-MCP test-case guide and tool docstrings (private QualGent-MCP) at run time,
from pinned refs (`create/arm.py`). None of that text may land in a tracked file.

The sentinels are SHA-256 digests of 8-word runs taken from those private sources —
the phrases themselves cannot be written here without committing the very text this
test guards. Every tracked text file is normalised (lower case, every run of
non-alphanumerics → one space) and every 8-word window is hashed and looked up. Any
hit means a private phrase was pasted in.

Stronger, opt-in: with `QGB_PRIVATE_QUALGENT_MCP` and `QGB_PRIVATE_DEVLOOP` naming
local checkouts, every 12-word window of the template body and the guide (read at
their HEAD) is checked against the repo too. Read at import, because the suite's
autouse fixture strips `QGB_*` before each test.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from qualgentbench.config import REPO_ROOT

WINDOW = 8

#: sha256(" ".join(8 normalised words)) for phrases from the private sources.
SENTINELS: dict[str, str] = {
    "7a53a7e3b0bea6d6d6eca8170250974d8f5b6f806f5ee3ad9537b96d21f84171": "creator template",
    "90d9b1abf5595d8f0808bad9da2014f3e9e0d64d5fd1626c604ec574108a68f9": "creator template",
    "50993716e1cd4b759b4fb8cc01281d06d726379a3dfd399aae6386bc6e0ad10f": "creator template",
    "0fd853d21999301d4826e0b7a2d63e7933a5265895f80128c30a855614dccab2": "creator template",
    "52ac62a5d36ac7659ac9a8a4bc65f58da7c7de46c1cf2cfd43b3521a735b9d8b": "test-case guide",
    "46c501e250b70e4b7b131ae566cced05a2f6989548015d51e70eb4342d881a53": "test-case guide",
    "ba9a8f6cd4e437af3c8a1e5455fb6294e023edba8fc43728b1a3f8eae83379ed": "test-case guide",
    "07673dc029e531286d4608142beb3043b4404b75b7207127b1e1048195c19950": "create_test_case docstring",
}

PRIVATE_QUALGENT_MCP = os.environ.get("QGB_PRIVATE_QUALGENT_MCP")
PRIVATE_DEVLOOP = os.environ.get("QGB_PRIVATE_DEVLOOP")

_NON_WORD = re.compile(r"[^a-z0-9]+")


def words(text: str) -> list[str]:
    return _NON_WORD.sub(" ", text.lower()).split()


def window_hash(ws: Iterable[str]) -> str:
    return hashlib.sha256(" ".join(ws).encode()).hexdigest()


def windows(ws: list[str], n: int) -> Iterator[tuple[int, list[str]]]:
    for i in range(len(ws) - n + 1):
        yield i, ws[i:i + n]


def tracked_text_files(root: Path = REPO_ROOT) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    files = []
    for rel in out.decode().split("\0"):
        path = root / rel
        if not rel or not path.is_file():
            continue
        with path.open("rb") as fh:
            if b"\0" in fh.read(8192):
                continue                       # binary (an APK fixture, a database)
        files.append(path)
    return files


def sentinel_hits(paths: Iterable[Path], sentinels: dict[str, str]) -> list[str]:
    hits = []
    for path in paths:
        ws = words(path.read_text(errors="replace"))
        for i, w in windows(ws, WINDOW):
            what = sentinels.get(window_hash(w))
            if what:
                hits.append(f"{path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path}"
                            f" (word {i}): a phrase from the {what}")
    return hits


def test_scanner_finds_a_planted_phrase(tmp_path):
    phrase = "an entirely public sentence planted here to prove the scanner reads it"
    planted = tmp_path / "planted.md"
    planted.write_text(f"# Notes\n\nSomething. {phrase.upper()}!\n")
    digest = window_hash(words(phrase)[:WINDOW])
    assert sentinel_hits([planted], {digest: "planted"})
    assert not sentinel_hits([planted], {window_hash(["no"] * WINDOW): "absent"})


def test_sentinels_are_well_formed():
    assert len(SENTINELS) >= 6
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in SENTINELS)


def test_no_private_creation_surface_text_is_committed():
    files = tracked_text_files()
    if not files:
        pytest.skip("not a git checkout")
    hits = sentinel_hits(files, SENTINELS)
    assert not hits, ("private creation-surface text is committed to this PUBLIC "
                      "repository — inject it at run time instead (create/arm.py):\n  "
                      + "\n  ".join(hits))


# ── opt-in: the whole private text, against local private checkouts ───────────

_LONG = 12
_GUIDE_RE = re.compile(r'TEST_CASE_GUIDE\s*=\s*"""\\?\n(.*?)"""', re.DOTALL)
_FRONTMATTER_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---\r?\n([\s\S]*)$")


def _committed(checkout: str, relpath: str) -> str:
    return subprocess.run(["git", "-C", checkout, "show", f"HEAD:{relpath}"],
                          capture_output=True, check=True).stdout.decode()


@pytest.mark.skipif(not (PRIVATE_QUALGENT_MCP and PRIVATE_DEVLOOP),
                    reason="set QGB_PRIVATE_QUALGENT_MCP and QGB_PRIVATE_DEVLOOP to local "
                           "checkouts to scan against the full private text")
def test_no_long_run_of_private_text_is_committed():
    template = _committed(PRIVATE_DEVLOOP, "subagent-templates/qualgent-test-creator.md")
    body = _FRONTMATTER_RE.match(template)
    assert body, "the creator template has no frontmatter"
    static = _committed(PRIVATE_QUALGENT_MCP, "src/qualgent_mcp/resources/static.py")
    guide = _GUIDE_RE.search(static)
    assert guide, "TEST_CASE_GUIDE not found"
    private: dict[str, str] = {}
    for label, text in (("creator template", body.group(1)), ("test-case guide", guide.group(1))):
        for _, w in windows(words(text), _LONG):
            private.setdefault(window_hash(w), label)
    # The committed sentinels must still be phrases of the current private text, or
    # they guard nothing (a rewrite upstream would silently retire them).
    short = set()
    for text in (body.group(1), guide.group(1)):
        short |= {window_hash(w) for _, w in windows(words(text), WINDOW)}
    stale = [h for h, what in SENTINELS.items()
             if what in ("creator template", "test-case guide") and h not in short]
    docstrings = _committed(PRIVATE_QUALGENT_MCP, "src/qualgent_mcp/tools/test_cases.py")
    doc_short = {window_hash(w) for _, w in windows(words(docstrings), WINDOW)}
    stale += [h for h, what in SENTINELS.items()
              if what == "create_test_case docstring" and h not in doc_short]
    assert not stale, f"sentinels no longer found in the private text: {stale}"

    hits = []
    for path in tracked_text_files():
        for i, w in windows(words(path.read_text(errors="replace")), _LONG):
            if (what := private.get(window_hash(w))):
                hits.append(f"{path.relative_to(REPO_ROOT)} (word {i}): {_LONG} words "
                            f"of the {what}")
    assert not hits, "\n".join(hits)
