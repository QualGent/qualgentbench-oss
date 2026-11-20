"""QUA-3483: the renderer is deterministic, ranks entries and stays inside each budget."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from qualgentbench.memory import format as fmt
from qualgentbench.memory import render as rd
from qualgentbench.memory.snapshot import freeze, snapshot_hash, unit_files

FIX = Path(__file__).parent / "fixtures" / "memory"
ORG, APP = "samplecorp", "sampleapp"


@pytest.fixture
def good(tmp_path: Path) -> Path:
    root = tmp_path / "memory"
    shutil.copytree(FIX / "good", root)
    return root


def _oversized(root: Path, n: int = 400) -> None:
    """An app file far over every budget: n entries per section with varied counters
    and dates, plus an org file with 50 per section."""
    app = fmt.new_file(fmt.APP, "App memory: Big")
    for i in range(n):
        for _, t in fmt.APP_SECTIONS:
            app.add_entry(t, f"{t} fact number {i} about the big app, padded with words "
                             f"so the line is realistic in length ({i % 7}).",
                          {"src": "human", "confirmed": f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}",
                           "seen": i % 9, "contra": i % 4})
    org = fmt.new_file(fmt.ORG, "Org memory: Big")
    for i in range(50):
        for _, t in fmt.ORG_SECTIONS:
            org.add_entry(t, f"{t} org fact {i}.", {"src": "human", "seen": i % 5})
    fmt.app_path(root, "big", "bigapp").parent.mkdir(parents=True, exist_ok=True)
    fmt.org_path(root, "big").parent.mkdir(parents=True, exist_ok=True)
    app.save(fmt.app_path(root, "big", "bigapp"))
    org.save(fmt.org_path(root, "big"))
    fmt.changes_path(root, "big", "bigapp").write_text(
        "# What changed\n\n" + "".join(f"- Screen {i} changed: control {i} added.\n"
                                       for i in range(2000)))


def test_none_renders_empty_through_the_same_function(good: Path) -> None:
    r = rd.render(good, ORG, APP, "none")
    assert (r.text, r.tokens, r.snapshot, r.included) == ("", 0, None, [])
    # `none` reads nothing, so it works with no memory at all.
    assert rd.render(None, None, None, "none").text == ""
    assert r.metrics()["memory_tokens_injected"] == 0


def test_small_block_shape(good: Path) -> None:
    r = rd.render(good, ORG, APP, "small")
    snap = snapshot_hash(good, unit_files(ORG, APP))
    assert r.snapshot == snap
    assert r.text.startswith(f'<qualgent_memory snapshot="{snap}">\nOrg: Org memory: Sample Corp\n')
    assert r.text.endswith("</qualgent_memory>\n")
    assert "App: App memory: Sample Notes (org.example.notes)\n## Navigation\n" \
           "- [nav-001] The note list" in r.text
    # Never rendered: metadata comments, the invalidated entry, empty sections.
    assert "<!--" not in r.text and "nav-002" not in r.text and "drawer" not in r.text
    assert "## Past bugs" not in r.text and "## Invalidated" not in r.text
    assert "What changed" not in r.text
    assert r.dropped == [] and len(r.included) == 10
    assert r.tokens == rd.estimate_tokens(r.text) <= rd.BUDGETS["small"]


def test_changes_only_on_the_diff_arm(good: Path) -> None:
    r = rd.render(good, ORG, APP, "small", include_changes=True)
    assert "What changed since the last build:\n- Screen added: \"Archive\"" in r.text
    assert "# What changed" not in r.text  # the title line is dropped
    assert r.changes_included and not r.changes_truncated
    assert r.snapshot == snapshot_hash(good, unit_files(ORG, APP, include_changes=True))
    assert r.snapshot != rd.render(good, ORG, APP, "small").snapshot


def test_missing_files_fail_instead_of_falling_back(good: Path) -> None:
    fmt.org_path(good, ORG).unlink()
    with pytest.raises(FileNotFoundError):
        rd.render(good, ORG, APP, "small")
    with pytest.raises(FileNotFoundError):
        rd.render(FIX / "good", ORG, "nope", "large")
    with pytest.raises(ValueError):
        rd.render(FIX / "good", ORG, APP, "medium")


@pytest.mark.parametrize("mode", ["small", "large", "index"])
def test_oversized_memory_stays_within_each_budget(tmp_path: Path, mode: str) -> None:
    _oversized(tmp_path)
    r = rd.render(tmp_path, "big", "bigapp", mode, include_changes=True)
    assert r.tokens == rd.estimate_tokens(r.text) <= rd.BUDGETS[mode]
    assert r.dropped and r.included
    # The cut is a prefix of the ranking: every kept entry outranks every dropped one.
    docs = [fmt.load(fmt.org_path(tmp_path, "big")), fmt.load(fmt.app_path(tmp_path, "big", "bigapp"))]
    by_id = {e.id: e for d in docs for e in d.entries()}
    worst_kept = max(rd.rank_key(by_id[i]) for i in r.included)
    best_dropped = min(rd.rank_key(by_id[i]) for i in r.dropped)
    assert worst_kept < best_dropped
    assert len(r.included) + len(r.dropped) == len(by_id)
    # The changes text is capped at its own limit and marked.
    assert r.changes_truncated and "[…truncated]" in r.text


def test_large_keeps_more_than_small_and_index_lines_are_short(tmp_path: Path) -> None:
    _oversized(tmp_path)
    small = rd.render(tmp_path, "big", "bigapp", "small")
    large = rd.render(tmp_path, "big", "bigapp", "large")
    index = rd.render(tmp_path, "big", "bigapp", "index")
    assert set(small.included) < set(large.included)
    assert large.tokens > 3 * small.tokens
    assert "Full memory files are in the workspace: memory/org/big.md and " \
           "memory/app/big/bigapp.md" in index.text
    entry_lines = [ln for ln in index.text.splitlines() if ln.startswith("- [")]
    assert entry_lines and all(len(ln.split()) <= 2 + rd.INDEX_WORDS + 1 for ln in entry_lines)


def test_ranking_order() -> None:
    def e(eid: str, seen: int, contra: int, confirmed: str = "") -> fmt.Entry:
        meta = {"src": "human", "seen": str(seen), "contra": str(contra)}
        if confirmed:
            meta["confirmed"] = confirmed
        return fmt.Entry(id=eid, text="t", meta=meta)

    entries = [
        e("note-001", 99, 0, "2026-12-01"),       # lowest section priority
        e("nav-001", 1, 0, "2026-01-01"),
        e("nav-002", 5, 1, "2026-01-01"),         # seen-contra 4
        e("nav-003", 5, 1, "2026-06-01"),         # same score, more recent
        e("nav-004", 5, 1),                       # same score, no date: oldest
        e("setup-001", 0, 3, "2020-01-01"),       # setup outranks nav whatever its score
    ]
    ranked = [x.id for x in sorted(entries, key=rd.rank_key)]
    assert ranked == ["setup-001", "nav-003", "nav-002", "nav-004", "nav-001", "note-001"]


def test_render_is_deterministic(tmp_path: Path) -> None:
    _oversized(tmp_path)
    a = rd.render(tmp_path, "big", "bigapp", "small", include_changes=True)
    b = rd.render(tmp_path, "big", "bigapp", "small", include_changes=True)
    assert a == b
    copy = tmp_path.parent / "copy"
    shutil.copytree(tmp_path, copy)
    c = rd.render(copy, "big", "bigapp", "small", include_changes=True)
    assert (c.text, c.snapshot, c.included) == (a.text, a.snapshot, a.included)


def test_render_from_a_frozen_snapshot_records_the_same_hash(good: Path, tmp_path: Path) -> None:
    snap = freeze(good, tmp_path / "store", files=unit_files(ORG, APP), stage="run-1",
                  unit="sampleapp__S-small__r1")
    r = rd.render(snap.memory_root, ORG, APP, "small")
    assert r.snapshot == snap.hash


def test_place_workspace_strips_invalidated(good: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / "stale.txt").write_text("old")
    written = rd.place_workspace(good, ORG, APP, ws, include_changes=True)
    assert [p.relative_to(ws).as_posix() for p in written] == [
        "memory/org/samplecorp.md", "memory/app/samplecorp/sampleapp.md",
        "memory/app/samplecorp/sampleapp.changes.md"]
    assert not (ws / "memory" / "stale.txt").exists()
    app_text = (ws / "memory/app/samplecorp/sampleapp.md").read_text()
    assert "## Invalidated" not in app_text and "nav-002" not in app_text
    assert "[nav-003]" in app_text
