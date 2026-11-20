"""QUA-3483: the memory file format round-trips byte-for-byte and edits keep ids."""

from __future__ import annotations

from pathlib import Path

import pytest

from qualgentbench.memory import format as fmt

FIX = Path(__file__).parent / "fixtures" / "memory"
ALL_FIXTURES = sorted(p for p in FIX.rglob("*.md") if not p.name.endswith(".changes.md"))


def _layer(p: Path) -> str:
    return fmt.ORG if p.parent.name == "org" else fmt.APP


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: str(p.relative_to(FIX)))
def test_every_fixture_round_trips_byte_for_byte(path: Path) -> None:
    raw = path.read_bytes().decode("utf-8")
    assert fmt.loads(raw, _layer(path)).dumps() == raw


def test_fixtures_cover_the_odd_shapes() -> None:
    names = {p.name for p in ALL_FIXTURES}
    assert {"crlf.md", "no_final_newline.md", "prose_and_extras.md", "empty.md"} <= names


def test_parse_reads_entries_metadata_and_sections() -> None:
    doc = fmt.load(FIX / "good/app/samplecorp/sampleapp.md")
    assert doc.layer == fmt.APP
    assert doc.title == "App memory: Sample Notes (org.example.notes)"
    assert [s.name for s in doc.sections] == [
        "Navigation", "Login and setup", "Flaky or slow areas", "Known accepted behavior",
        "Past bugs", "Notes", "Invalidated"]
    e = doc.get("nav-003")
    assert e.type == "nav" and e.number == 3
    assert e.text == 'To open a note\'s settings, long-press the note, then tap ⋮ → "Properties".'
    assert e.meta == {"src": "human", "confirmed": "2026-11-16", "seen": "4", "contra": "0"}
    assert (e.seen, e.contra, e.src) == (4, 0, "human")
    assert [x.id for x in doc.entries()] == [
        "nav-001", "nav-003", "setup-001", "flaky-001", "accepted-001", "note-001"]
    assert doc.is_invalidated("nav-002")
    assert "nav-002" in [x.id for x in doc.entries(include_invalidated=True)]
    assert doc.malformed == []


def test_malformed_list_items_are_kept_and_reported() -> None:
    doc = fmt.load(FIX / "roundtrip/prose_and_extras.md", fmt.APP)
    bad = [line for _, line in doc.malformed]
    assert "* a star bullet is prose, not an entry" in bad
    assert "- not an entry because it has no id" in bad
    assert doc.get("bug-1000").text == "Four-digit ids parse."
    assert list(doc.get("nav-011").meta) == ["case", "src", "stage", "seen"]


def test_new_ids_never_reuse_a_number_including_invalidated() -> None:
    doc = fmt.load(FIX / "good/app/samplecorp/sampleapp.md")
    # nav-002 is invalidated and nav-003 is live: the next is nav-004, never nav-002.
    e = doc.add_entry("nav", "Swipe a note left to archive it.",
                      {"src": "curator", "confirmed": "2026-11-17", "seen": 1, "contra": 0})
    assert e.id == "nav-004"
    assert doc.add_entry("bug", "Exporting crashed in 1.2; fixed in 1.3.",
                         {"src": "curator"}).id == "bug-001"
    out = doc.dumps()
    assert "- [nav-004] Swipe a note left to archive it. <!-- src=curator; " \
           "confirmed=2026-11-17; seen=1; contra=0 -->\n" in out
    # The new nav entry sits after nav-003 and before the blank line ending the section.
    assert out.index("[nav-004]") < out.index("## Login and setup")
    again = fmt.loads(out, fmt.APP)
    assert again.dumps() == out
    assert [x.id for x in again.section("Past bugs").entries] == ["bug-001"]


def test_untouched_entries_keep_their_original_bytes_after_edits() -> None:
    path = FIX / "roundtrip/prose_and_extras.md"
    raw = path.read_text()
    doc = fmt.loads(raw, fmt.APP)
    doc.update_entry("nav-011", seen=1)
    out = doc.dumps()
    # nav-010's irregular spacing survives; only nav-011's line changed.
    assert "- [nav-010]   Extra   spaces" in out
    assert "- [nav-011] Extra metadata keys stay in order. <!-- src=curator; seen=1; " \
           "case=markor-open; stage=run-1 -->\n" in out
    assert len(out.splitlines()) == len(raw.splitlines())


def test_update_bump_and_invalidate() -> None:
    doc = fmt.load(FIX / "good/app/samplecorp/sampleapp.md")
    doc.bump("nav-001", seen=1, confirmed="2026-11-18")
    assert doc.get("nav-001").seen == 5 and doc.get("nav-001").confirmed == "2026-11-18"
    doc.bump("flaky-001", contra=2)
    assert doc.get("flaky-001").contra == 3
    doc.update_entry("note-001", text="The editor saves when it is left.", contra=None)
    assert "contra" not in doc.get("note-001").meta
    doc.invalidate("setup-001", "welcome page removed in 2.1", date="2026-11-18")
    assert doc.is_invalidated("setup-001")
    assert doc.section("Login and setup").entries == []
    inv = doc.get("setup-001")
    assert inv.meta["reason"] == "welcome page removed in 2.1"
    assert inv.meta["invalidated"] == "2026-11-18"
    with pytest.raises(fmt.FormatError):
        doc.update_entry("setup-001", text="x")
    with pytest.raises(fmt.FormatError):
        doc.invalidate("setup-001", "again")
    with pytest.raises(fmt.FormatError):
        doc.invalidate("nav-001", "  ")
    again = fmt.loads(doc.dumps(), fmt.APP)
    assert again.dumps() == doc.dumps()
    assert [e.id for e in again.section("Invalidated").entries] == ["nav-002", "setup-001"]


def test_writer_refuses_values_that_would_break_the_line() -> None:
    doc = fmt.new_file(fmt.APP, "App memory: X")
    with pytest.raises(fmt.FormatError):
        doc.add_entry("nav", "text", {"src": "a; seen=9"})
    with pytest.raises(fmt.FormatError):
        doc.add_entry("nav", "text", {"src": "x -->"})
    with pytest.raises(fmt.FormatError):
        doc.add_entry("company", "an org type in an app file", {"src": "human"})
    with pytest.raises(fmt.FormatError):
        doc.add_entry("nav", "   ", {"src": "human"})
    e = doc.add_entry("nav", "multi\nline\ttext", {"src": "human"})
    assert e.line() == "- [nav-001] multi line text <!-- src=human -->\n"
    with pytest.raises(fmt.FormatError):
        doc.add_entry("nav", "dup", {"src": "human"}, entry_id="nav-001")


def test_new_file_has_every_fixed_section_and_adding_to_a_missing_section_keeps_order() -> None:
    doc = fmt.new_file(fmt.ORG, "Org memory: X")
    assert [s.name for s in doc.sections] == [h for h, _ in fmt.ORG_SECTIONS]
    sparse = fmt.loads("# App memory: Y\n\n## Notes\n- [note-001] n <!-- src=human -->",
                       fmt.APP)
    sparse.add_entry("nav", "first nav", {"src": "human"})
    sparse.invalidate("note-001", "wrong")
    assert [s.name for s in sparse.sections] == ["Navigation", "Notes", "Invalidated"]
    out = sparse.dumps()
    assert out.endswith("## Invalidated\n- [note-001] n <!-- src=human; reason=wrong -->\n")
    assert fmt.loads(out, fmt.APP).dumps() == out


def test_paths_and_layers(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    assert fmt.org_path(root, "o") == root / "org/o.md"
    assert fmt.app_path(root, "o", "a") == root / "app/o/a.md"
    assert fmt.changes_path(root, "o", "a") == root / "app/o/a.changes.md"
    assert fmt.layer_of(root / "org/o.md") == fmt.ORG
    assert fmt.layer_of(root / "app/o/a.md") == fmt.APP
    with pytest.raises(ValueError):
        fmt.layer_of(root / "app/o/a.changes.md")
    assert fmt.find_org(FIX / "good", "sampleapp") == "samplecorp"
    with pytest.raises(FileNotFoundError):
        fmt.find_org(FIX / "good", "nope")
