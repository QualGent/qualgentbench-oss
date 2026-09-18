"""`scripts/publish_apk.py` — the one step that moves a rebuilt APK and its hash.

`apps.fetch_seeded_apk` sha256-checks every download, so the file on HuggingFace and
the `apk:` block are one fact. What is pinned here is what keeps a wrong hash out of a
fresh clone: the block is rewritten by LINE (the comments in these files are the
authoring record, and a yaml round-trip deletes them), journey and hunt write DIFFERENT
files, and a tier that contradicts the spec is refused rather than published somewhere
nothing looks for it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("publish_apk", ROOT / "scripts" / "publish_apk.py")
publish_apk = importlib.util.module_from_spec(_spec)
sys.modules["publish_apk"] = publish_apk
_spec.loader.exec_module(publish_apk)

CASE_FILE = """\
# QGB-CANARY-0000  <- contamination canary. Do not remove.
# Fossify Calendar — journey cases.

app: fossify-calendar

apk:
  repo: qualgent/qualgentbench-apps
  filename: journey/fossify-calendar-buggy.apk
  sha256: d97b0f8d6ba0ce6132a297f0bf3c3955fb90d748d4578700921e4774fef79519
  size_bytes: 32753155

defects:
  - id: event-delete-broken
"""


# ── the block rewrite ──────────────────────────────────────────────────────────

def test_rewrite_updates_the_hash_and_size_in_place():
    out = publish_apk.rewrite_block(CASE_FILE, {"sha256": "abc123", "size_bytes": 7})
    assert "  sha256: abc123\n" in out
    assert "  size_bytes: 7\n" in out
    assert "d97b0f8d" not in out and "32753155" not in out


def test_rewrite_keeps_every_comment_and_the_rest_of_the_file():
    out = publish_apk.rewrite_block(CASE_FILE, {"sha256": "abc123", "size_bytes": 7})
    assert "# QGB-CANARY-0000" in out          # the contamination canary must survive
    assert "# Fossify Calendar — journey cases." in out
    assert "defects:\n  - id: event-delete-broken\n" in out
    assert out.count("apk:") == 1


def test_rewrite_keeps_a_trailing_comment_on_a_rewritten_key():
    text = "apk:\n  sha256: old   # measured 2026-08-01\n  size_bytes: 1\n\nx: 1\n"
    out = publish_apk.rewrite_block(text, {"sha256": "new"})
    assert "sha256: new  # measured 2026-08-01" in out


def test_rewrite_appends_a_key_the_block_never_had():
    text = "apk:\n  repo: a/b\n  filename: journey/x.apk\n\nnext: 1\n"
    out = publish_apk.rewrite_block(text, {"sha256": "s", "size_bytes": 2})
    assert "  sha256: s\n" in out and "  size_bytes: 2\n" in out
    assert out.endswith("next: 1\n")


def test_rewrite_leaves_an_unrelated_apk_word_alone():
    """Only a TOP-LEVEL `apk:` is the block — a nested one belongs to something else."""
    text = "cases:\n  - apk: something\napk:\n  sha256: old\n"
    out = publish_apk.rewrite_block(text, {"sha256": "new"})
    assert "  - apk: something" in out and "sha256: new" in out


def test_rewrite_refuses_a_file_with_no_block():
    with pytest.raises(SystemExit) as e:
        publish_apk.rewrite_block("app: x\n", {"sha256": "s"})
    assert "no top-level `apk:` block" in str(e.value)


def test_a_rewrite_that_changes_nothing_is_a_no_op():
    same = {"sha256": "d97b0f8d6ba0ce6132a297f0bf3c3955fb90d748d4578700921e4774fef79519",
            "size_bytes": 32753155}
    assert publish_apk.rewrite_block(CASE_FILE, same) == CASE_FILE


# ── which file, which remote path ──────────────────────────────────────────────

def test_journey_and_hunt_target_different_files():
    """Journey mode reads the test-case file's block, hunt mode the spec's. Writing one
    and calling it done leaves the other arm on the old APK."""
    journey = publish_apk.target_file("fossify-calendar", "journey")
    hunt = publish_apk.target_file("fossify-calendar", "hunt")
    assert journey.name == "fossify-calendar.yaml" and journey.parent.name == "test-cases"
    assert hunt.name == "fossify-calendar.yaml" and hunt.parent.name == "benchmarks"
    assert journey != hunt


def test_journey_publishes_under_journey():
    assert publish_apk.remote_dir("x", "journey", {"app": {"difficulty": "hard"}}) == "journey"


def test_hunt_publishes_under_the_spec_tier():
    assert publish_apk.remote_dir("x", "hunt", {"app": {"difficulty": "hard"}}) == "hard"


def test_a_tier_that_contradicts_the_spec_is_refused():
    with pytest.raises(SystemExit) as e:
        publish_apk.remote_dir("x", "medium", {"app": {"difficulty": "hard"}})
    assert "tier 'hard'" in str(e.value)


def test_a_matching_tier_name_is_accepted():
    assert publish_apk.remote_dir("x", "hard", {"app": {"difficulty": "hard"}}) == "hard"


def test_hunt_without_a_difficulty_asks_for_an_explicit_tier():
    with pytest.raises(SystemExit) as e:
        publish_apk.remote_dir("x", "hunt", {"app": {}})
    assert "--kind" in str(e.value)


# ── the hash ───────────────────────────────────────────────────────────────────

def test_sha256_matches_hashlib(tmp_path):
    import hashlib
    f = tmp_path / "a.apk"
    f.write_bytes(b"x" * (1 << 20) + b"tail")
    assert publish_apk.sha256_of(f) == hashlib.sha256(f.read_bytes()).hexdigest()


# ── the two halves never move apart ────────────────────────────────────────────

def test_upload_without_write_is_refused(monkeypatch, capsys):
    """Uploading without landing the hash publishes bytes no `apk:` block names, which
    is the same broken clone as landing a hash nothing has uploaded."""
    monkeypatch.setattr(sys, "argv", ["publish_apk.py", "fossify-calendar",
                                      "--kind", "journey", "--upload", "--yes"])
    monkeypatch.setenv("HF_TOKEN", "dummy")
    with pytest.raises(SystemExit) as e:
        publish_apk.main()
    assert "--upload without --write" in str(e.value)
