"""Tamper-evidence: a bundle edited after the fact must not verify."""

from __future__ import annotations

import json
from pathlib import Path

from qualgentbench.evidence_manifest import steps_chain, verify_bundle, write_manifest


def _bundle(tmp_path: Path) -> Path:
    out = tmp_path / "evidence"
    (out / "screens").mkdir(parents=True)
    (out / "screens" / "0001.jpg").write_bytes(b"\xff\xd8\xff-one")
    (out / "screens" / "0003.jpg").write_bytes(b"\xff\xd8\xff-two")
    (out / "meta.json").write_text(json.dumps({"episode": {"app": "Birday"}}))
    with (out / "steps.jsonl").open("w") as handle:
        for step in (
            {"step": 1, "kind": "observe", "summary": "Observed screen"},
            {"step": 2, "kind": "action", "summary": "Tapped (10, 20)"},
            {"step": 3, "kind": "observe", "summary": "Observed screen"},
        ):
            handle.write(json.dumps(step) + "\n")
    write_manifest(out)
    return out


def test_an_untouched_bundle_verifies(tmp_path: Path) -> None:
    result = verify_bundle(_bundle(tmp_path))

    assert result["ok"] is True
    assert result["steps_ok"] is True
    assert result["files_checked"] == 4


def test_an_edited_step_is_detected(tmp_path: Path) -> None:
    out = _bundle(tmp_path)
    steps = (out / "steps.jsonl").read_text().replace("Tapped (10, 20)", "Tapped (99, 99)")
    (out / "steps.jsonl").write_text(steps)

    result = verify_bundle(out)

    assert result["ok"] is False
    assert "steps.jsonl" in result["changed"]
    assert result["steps_ok"] is False


def test_reordering_steps_breaks_the_chain(tmp_path: Path) -> None:
    """A file digest alone would not care about order — the chain does."""
    out = _bundle(tmp_path)
    before = steps_chain(out / "steps.jsonl")
    lines = (out / "steps.jsonl").read_text().splitlines()
    (out / "steps.jsonl").write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n")
    after = steps_chain(out / "steps.jsonl")

    assert before[0] == after[0] == 3      # same steps, same count
    assert before[1] != after[1]           # different order, different head
    assert verify_bundle(out)["steps_ok"] is False


def test_a_deleted_screenshot_is_detected(tmp_path: Path) -> None:
    out = _bundle(tmp_path)
    (out / "screens" / "0003.jpg").unlink()

    result = verify_bundle(out)

    assert result["ok"] is False
    assert result["missing"] == ["screens/0003.jpg"]


def test_a_screenshot_added_afterwards_is_detected(tmp_path: Path) -> None:
    """An inserted file is as much an edit as an altered one."""
    out = _bundle(tmp_path)
    (out / "screens" / "0009.jpg").write_bytes(b"\xff\xd8\xff-planted")

    result = verify_bundle(out)

    assert result["ok"] is False
    assert result["extra"] == ["screens/0009.jpg"]


def test_the_page_is_not_covered_so_it_can_be_re_rendered(tmp_path: Path) -> None:
    out = _bundle(tmp_path)
    (out / "index.html").write_text("<html>regenerated</html>")

    result = verify_bundle(out)

    assert result["ok"] is True
    assert "index.html" not in result["extra"]


def test_the_transcript_and_prompt_are_covered_even_though_they_sit_outside(tmp_path: Path) -> None:
    """Re-scoring reads the transcript, so an unhashed transcript protects nothing."""
    out = _bundle(tmp_path)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "transcript.txt").write_text('{"type":"item.completed"}')
    (tmp_path / "instruction_sent.md").write_text("Explore the app and report.")
    (tmp_path / "result.json").write_text(json.dumps({"score": 1.0}))
    write_manifest(out)

    manifest = json.loads((out / "manifest.json").read_text())
    assert "../agent/transcript.txt" in manifest["files"]
    assert "../instruction_sent.md" in manifest["files"]
    assert verify_bundle(out)["ok"] is True

    (tmp_path / "agent" / "transcript.txt").write_text('{"type":"tampered"}')
    result = verify_bundle(out)
    assert result["ok"] is False
    assert "../agent/transcript.txt" in result["changed"]


def test_an_edited_prompt_is_detected(tmp_path: Path) -> None:
    """A run whose prompt named the bug is not the same benchmark."""
    out = _bundle(tmp_path)
    (tmp_path / "instruction_sent.md").write_text("Explore the app and report.")
    write_manifest(out)

    (tmp_path / "instruction_sent.md").write_text("The delete button is broken. Report it.")

    assert verify_bundle(out)["changed"] == ["../instruction_sent.md"]


def test_a_missing_manifest_is_reported_not_raised(tmp_path: Path) -> None:
    out = tmp_path / "evidence"
    out.mkdir()

    result = verify_bundle(out)

    assert result["ok"] is False
    assert "no readable manifest" in result["error"]
