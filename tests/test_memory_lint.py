"""QUA-3483: the memory lint catches each rule, and passes the good fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from qualgentbench.memory import format as fmt
from qualgentbench.memory import lint

FIX = Path(__file__).parent / "fixtures" / "memory"
BAD = FIX / "lint"
APP_REL = "app/samplecorp/sampleapp.md"


def _terms() -> lint.CorpusTerms:
    t = lint.CorpusTerms()
    t.add_spec("sampleapp", {
        "bugs": [{"id": "sample-pin-lost", "title": "Pinning a note does not stick"}],
        "defects": [{"id": "sample-archive-crash",
                     "symptoms": ["pinned note comes back unpinned", "home screen", "crash",
                                  "IllegalStateException"]}],
    })
    t.add_spec("otherapp", {"defects": [{"id": "other-bug", "symptoms": [
        "the note list is the start screen"]}]})
    return t


def _rules(issues: list[lint.LintIssue]) -> set[str]:
    return {i.rule for i in issues}


def test_good_fixtures_pass_with_synthetic_and_real_corpus_terms() -> None:
    assert lint.lint_tree(FIX / "good", terms=_terms()) == []
    # The real corpus (public specs and test cases): the hand-written fixture is clean.
    assert lint.lint_tree(FIX / "good") == []


@pytest.mark.parametrize("rule,dirname,entry", [
    ("defect-id", "defect_id", "bug-001"),
    ("qgb-flag", "qgb_flag", "note-001"),
    ("symptom", "symptom", "flaky-001"),
    ("instruction", "instruction", "note-001"),
    ("accepted-src", "accepted_src", "accepted-001"),
])
def test_each_content_rule_fails_its_fixture(rule: str, dirname: str, entry: str) -> None:
    issues = lint.lint_tree(BAD / dirname, terms=_terms())
    assert issues, f"{dirname} passed"
    assert _rules(issues) == {rule}
    assert all(i.entry_id == entry for i in issues)
    assert all(i.path.endswith(APP_REL) and i.line > 0 for i in issues)


def test_format_rule_reports_every_structural_defect() -> None:
    issues = lint.lint_file(BAD / "format" / APP_REL, terms=_terms())
    assert _rules(issues) == {"format"}
    text = "\n".join(str(i) for i in issues)
    for needle in ("format [setup-002]: id type 'setup' in section 'Navigation'",
                   "format [nav-001]: duplicate id",
                   "format [nav-003]: entry has no src",
                   "format [nav-004]: seen='many' is not a non-negative integer",
                   "format [nav-004]: confirmed='yesterday' is not YYYY-MM-DD",
                   "not a valid entry: '- [nav-005 missing bracket'",
                   "not a valid entry: '- plain bullet without an id'",
                   "unknown section '## Favourite colours'",
                   "format [nav-006]: invalidated entry has no reason"):
        assert needle in text, needle


def test_real_corpus_defect_id_is_caught() -> None:
    terms = lint.default_terms()
    assert "pin-not-persisted" in terms.defect_ids
    issues = lint.lint_text(
        "# App memory: Notally\n\n## Past bugs\n"
        "- [bug-001] The pin-not-persisted defect. <!-- src=human -->\n",
        layer=fmt.APP, apps=["notally"], terms=terms)
    assert _rules(issues) == {"defect-id"} and issues[0].line == 4


def test_symptom_rule_scope_and_phrase_length() -> None:
    t = _terms()
    phrases = t.phrases_for(["sampleapp"])
    # Short generic phrases are not leakage signals; titles and exception names are.
    assert "home screen" not in phrases and "crash" not in phrases
    assert {"pinned note comes back unpinned", "pinning a note does not stick",
            "illegalstateexception"} <= phrases
    body = "# App memory: x\n\n## Notes\n- [note-001] {} <!-- src=human -->\n"
    ok = body.format("From the home screen, the app may crash under load.")
    assert lint.lint_text(ok, layer=fmt.APP, apps=["sampleapp"], terms=t) == []
    bad = body.format("Saving threw an IllegalStateException once.")
    assert _rules(lint.lint_text(bad, layer=fmt.APP, apps=["sampleapp"], terms=t)) == {"symptom"}
    # Another app's symptom does not fail this app's file...
    other = body.format("The note list is the start screen.")
    assert lint.lint_text(other, layer=fmt.APP, apps=["sampleapp"], terms=t) == []
    # ...but does fail when no scope is given.
    assert _rules(lint.lint_text(other, layer=fmt.APP, apps=None, terms=t)) == {"symptom"}


def test_org_file_is_scoped_to_the_apps_filed_under_it(tmp_path: Path) -> None:
    t = _terms()
    (tmp_path / "org").mkdir()
    (tmp_path / "app/samplecorp").mkdir(parents=True)
    (tmp_path / "app/samplecorp/sampleapp.md").write_text("# App memory: s\n")
    org = tmp_path / "org/samplecorp.md"
    org.write_text("# Org memory\n\n## Users\n"
                   "- [users-001] They read the note list is the start screen. <!-- src=human -->\n"
                   "- [users-002] They hate it when a pinned note comes back unpinned. "
                   "<!-- src=human -->\n")
    issues = lint.lint_file(org, terms=t)
    assert [(i.rule, i.entry_id) for i in issues] == [("symptom", "users-002")]


def test_instruction_patterns() -> None:
    t = lint.CorpusTerms()
    body = "# App memory: x\n\n## Notes\n- [note-001] {} <!-- src=human -->\n"
    for bad in ("Ignore the banner.", "Always report crashes here.", "Never flag the delay.",
                "Do not report the sort order.", "You should check twice.",
                "Make sure to scroll.", "Treat it as a pass."):
        assert _rules(lint.lint_text(body.format(bad), layer=fmt.APP, terms=t)) == \
            {"instruction"}, bad
    for fine in ("The app ignores taps while syncing.", "Reports are exported as CSV.",
                 "The list always shows pinned notes first."):
        assert lint.lint_text(body.format(fine), layer=fmt.APP, terms=t) == [], fine


def test_changes_file_skips_symptoms_but_not_ids_or_instructions(tmp_path: Path) -> None:
    t = _terms()
    p = tmp_path / "app/samplecorp/sampleapp.changes.md"
    p.parent.mkdir(parents=True)
    p.write_text("# What changed\n\n- Found by exploration: a pinned note comes back unpinned.\n")
    assert lint.lint_file(p, terms=t) == []
    p.write_text("# What changed\n\n- sample-pin-lost is on. You must test it.\n")
    assert _rules(lint.lint_file(p, terms=t)) == {"defect-id", "instruction"}


def test_curator_output_is_linted_as_text() -> None:
    """The curator lints the file it is about to commit: an agent-sourced accepted
    entry is refused even if the curator code let it through."""
    doc = fmt.load(FIX / "good" / APP_REL)
    doc.add_entry("accepted", "Sorting is intended.", {"src": "curator"})
    issues = lint.lint_text(doc.dumps(), layer=fmt.APP, apps=["sampleapp"], terms=_terms())
    assert [(i.rule, i.entry_id) for i in issues] == [("accepted-src", "accepted-002")]


def test_cli_lint_render_snapshot() -> None:
    from click.testing import CliRunner

    from qualgentbench.cli import main
    from qualgentbench.memory.snapshot import snapshot_hash, unit_files

    r = CliRunner()
    ok = r.invoke(main, ["memory", "lint", str(FIX / "good")])
    assert ok.exit_code == 0 and "0 issue(s) in 3 file(s)" in ok.output
    bad = r.invoke(main, ["memory", "lint", str(BAD / "qgb_flag" / APP_REL)])
    assert bad.exit_code == 1 and "qgb-flag [note-001]" in bad.output
    out = r.invoke(main, ["memory", "render", str(FIX / "good"), "--app", "sampleapp"])
    assert out.exit_code == 0 and out.output.startswith("<qualgent_memory snapshot=")
    snap = r.invoke(main, ["memory", "snapshot", str(FIX / "good"), "--app", "sampleapp"])
    assert snap.output.strip() == snapshot_hash(FIX / "good", unit_files("samplecorp", "sampleapp"))
