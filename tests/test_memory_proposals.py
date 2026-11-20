"""QUA-3486: the memory_proposals.yaml schema and its validator."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from qualgentbench.memory import proposals as props

FIX = Path(__file__).parent / "fixtures" / "memory" / "proposals"


def test_valid_list_file() -> None:
    pf = props.load(FIX / "valid.yaml")
    assert pf.ok and pf.errors == [] and not pf.missing
    assert [p.type for p in pf.proposals] == ["nav", "flaky", "note"]
    nav, flaky, note = pf.proposals
    assert nav.evidence == props.Evidence(step=14, screen="Note list")
    assert flaky.contradicts == "flaky-001" and flaky.updates is None
    assert note.updates == "note-001" and note.evidence.step is None
    assert [p.index for p in pf.proposals] == [0, 1, 2]


def test_mapping_form_keeps_good_items_and_reports_each_bad_one() -> None:
    pf = props.load(FIX / "mixed.yaml")
    # setup and accepted survive (accepted is valid input; the curator rejects it, D6)
    assert [(p.index, p.type) for p in pf.proposals] == [(0, "setup"), (5, "accepted")]
    assert len(pf.errors) == 4
    joined = "\n".join(pf.errors)
    assert "proposal 1: type must be one of" in joined
    assert "proposal 2: evidence must be a mapping" in joined
    assert "proposal 3: a proposal has updates or contradicts, not both" in joined
    assert "proposal 4: unknown field(s) ['priority']" in joined


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    pf = props.load(tmp_path / props.FILENAME)
    assert pf.missing and pf.proposals == [] and pf.errors == []


@pytest.mark.parametrize("text,fragment", [
    ("- type: nav\n  text: [unclosed\n", "not valid YAML"),
    ("type: nav\ntext: a mapping without proposals\n", "unknown top-level field(s)"),
    ("just a string\n", "expected a YAML list"),
    ("version: 2\nproposals: []\n", "unsupported version"),
])
def test_file_level_errors(text: str, fragment: str) -> None:
    pf = props.loads(text)
    assert pf.proposals == [] and any(fragment in e for e in pf.errors)


@pytest.mark.parametrize("item,fragment", [
    ({"type": "nav", "text": "", "evidence": {"step": 1}}, "text is empty"),
    ({"type": "nav", "text": "x" * 301, "evidence": {"step": 1}}, "max 300"),
    ({"type": "nav", "text": "a <!-- b", "evidence": {"step": 1}}, "HTML comment"),
    ({"type": "nav", "text": "ok", "evidence": {}}, "needs a step or a screen"),
    ({"type": "nav", "text": "ok", "evidence": {"step": -1}}, "non-negative"),
    ({"type": "nav", "text": "ok", "evidence": {"step": True}}, "non-negative"),
    ({"type": "nav", "text": "ok", "evidence": {"step": 1, "where": "x"}}, "unknown evidence"),
    ({"type": "nav", "text": "ok", "evidence": {"step": 1}, "updates": "nav-3"},
     "entry id like nav-003"),
    ({"type": "company", "text": "ok", "evidence": {"step": 1}}, "type must be one of"),
    ("not a mapping", "not a mapping"),
])
def test_item_errors(item: object, fragment: str) -> None:
    with pytest.raises(props.ProposalError, match=None) as e:
        props.parse_item(item, 0)
    assert fragment in str(e.value)


def test_whitespace_is_collapsed() -> None:
    p = props.parse_item({"type": "note", "text": "  two\n  lines ",
                          "evidence": {"screen": " Home  screen "}}, 0)
    assert p.text == "two lines" and p.evidence.screen == "Home screen"


def test_cap_drops_the_rest_and_says_so() -> None:
    items = [{"type": "note", "text": f"fact {i}", "evidence": {"step": i}}
             for i in range(props.MAX_PER_FILE + 5)]
    pf = props.loads(yaml.safe_dump(items))
    assert len(pf.proposals) == props.MAX_PER_FILE
    assert pf.dropped_over_cap == 5 and "over the cap" in pf.errors[-1]


def test_dumps_roundtrip_and_append_stays_valid() -> None:
    pf = props.load(FIX / "valid.yaml")
    text = props.dumps(pf.proposals)
    assert props.loads(text).proposals == pf.proposals
    # An agent appending one more item to the list keeps the file valid.
    text += "- type: bug\n  text: Saving twice made a duplicate note.\n  evidence: {step: 9}\n"
    assert len(props.loads(text).proposals) == 4


def test_format_help_example_matches_the_schema() -> None:
    assert props.FILENAME in props.FORMAT_HELP
    for t in props.WRITABLE_TYPES:
        assert t in props.FORMAT_HELP
    assert "accepted" not in props.WRITABLE_TYPES
