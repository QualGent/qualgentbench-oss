"""The readable bundle page — what it shows, and what it must never do."""

from __future__ import annotations

import json
import re
from pathlib import Path

from qualgentbench.evidence_report import _result_line, render_report


def _bundle(tmp_path: Path, *, steps: list[dict] | None = None,
            findings: dict | None = None, meta: dict | None = None) -> Path:
    out = tmp_path / "evidence"
    (out / "screens").mkdir(parents=True)
    (out / "screens" / "0001.jpg").write_bytes(b"\xff\xd8\xff")
    (out / "meta.json").write_text(json.dumps(meta or {
        "episode": {"app": "Birday", "model": "kimi-k3", "agent": "claude-code",
                    "device": "emulator-5554", "score": 1.0, "passed": True,
                    "step_budget": 300, "wall_time_sec": 780.0,
                    "active_bugs": ["delete-broken"]},
        "counts": {"steps": 2, "screenshots": 1, "frames": 2},
    }))
    with (out / "steps.jsonl").open("w") as handle:
        for step in steps or [
            {"step": 1, "kind": "observe", "tool": "mcp__device__mobile_observe_screen",
             "summary": "Observed screen", "ok": True, "screens": ["screens/0001.jpg"],
             "elements": ["Add event", "Settings"]},
            {"step": 2, "kind": "action", "tool": "mcp__device__mobile_tap",
             "summary": "Tapped (980, 2305)", "ok": True,
             "screen_before": {"path": "screens/0001.jpg", "from_step": 1}},
        ]:
            handle.write(json.dumps(step) + "\n")
    if findings is not None:
        (out / "findings.json").write_text(json.dumps(findings))
    return out


def test_page_shows_every_step_with_its_screenshot(tmp_path: Path) -> None:
    page = render_report(_bundle(tmp_path)).read_text()

    assert page.count('class="step"') == 2
    assert "Observed screen" in page and "Tapped (980, 2305)" in page
    assert 'id="s1"' in page and 'id="s2"' in page
    assert "screens/0001.jpg" in page


def test_a_referenced_neighbour_is_labelled_with_the_step_it_came_from(tmp_path: Path) -> None:
    """A stale screen must never read as the state at this step."""
    page = render_report(_bundle(tmp_path)).read_text()

    assert "before · step 1" in page
    assert 'class="ref"' in page


def test_findings_table_links_each_area_to_its_claim_step(tmp_path: Path) -> None:
    out = _bundle(tmp_path, findings={
        "areas": [{"feature": "delete_event", "bug_id": "delete-broken", "truth": "broken",
                   "verdict": "broken", "outcome": "true_positive", "claim_step": 2,
                   "segment": [1, 2], "attribution": "banked",
                   "evidence_screen": "screens/0001.jpg"}],
        "agrees_with_score": True,
    })
    page = render_report(out).read_text()

    assert "delete_event" in page
    assert 'href="#s2"' in page          # the table jumps into the trajectory
    assert "true positive" in page


def test_a_disagreement_with_the_score_is_stated_on_the_page(tmp_path: Path) -> None:
    """The score is authoritative; a page that quietly contradicts it is worse than none."""
    out = _bundle(tmp_path, findings={
        "areas": [{"feature": "favorite", "truth": "broken", "verdict": "broken",
                   "outcome": "true_positive", "attribution": "banked"}],
        "agrees_with_score": False,
    })
    page = render_report(out).read_text()

    assert "disagrees with the scored result" in page
    assert "result.json is authoritative" in page


def test_bundle_without_findings_still_renders(tmp_path: Path) -> None:
    page = render_report(_bundle(tmp_path)).read_text()

    assert "<h2>Findings" not in page
    assert 'class="step"' in page


def test_every_image_reference_is_relative_and_resolves(tmp_path: Path) -> None:
    """The page is opened from inside the bundle, so absolute paths would break it."""
    out = _bundle(tmp_path, findings={
        "areas": [{"feature": "x", "truth": "ok", "verdict": "ok", "outcome": "correct_ok",
                   "attribution": "banked", "evidence_screen": "screens/0001.jpg"}],
        "agrees_with_score": True,
    })
    page = render_report(out).read_text()

    refs = {r for r in re.findall(r'(?:src|href)="([^"#]+)"', page)}
    assert refs
    for ref in refs:
        assert not ref.startswith(("/", "http", "file:"))
        assert (out / ref).exists()


def test_transcript_text_cannot_inject_markup(tmp_path: Path) -> None:
    out = _bundle(tmp_path, steps=[
        {"step": 1, "kind": "action", "tool": "mobile_type_text",
         "summary": "<img src=x onerror=alert(1)>", "ok": True,
         "elements": ["</div><script>alert(2)</script>"]},
    ])
    page = render_report(out).read_text()

    assert "<img src=x onerror" not in page
    assert "<script>alert(2)" not in page
    assert "&lt;img src=x onerror" in page


def test_the_headline_states_the_result_in_a_sentence(tmp_path: Path) -> None:
    """A reader who stops after the first line should still have the result."""
    out = _bundle(tmp_path, findings={
        "areas": [
            {"feature": "favorite", "truth": "broken", "verdict": "broken",
             "outcome": "true_positive", "attribution": "banked"},
            {"feature": "edit_event", "truth": "broken", "verdict": "ok",
             "outcome": "miss", "attribution": "banked"},
            {"feature": "settings", "truth": "ok", "verdict": "broken",
             "outcome": "false_positive", "attribution": "banked"},
        ],
        "agrees_with_score": True,
    })
    page = render_report(out).read_text()

    assert "Found <b>1 of 2</b> seeded bugs" in page
    assert "1 false report(s)" in page
    assert "1 missed" in page


def test_a_truncated_episode_says_so_in_the_headline(tmp_path: Path) -> None:
    meta = {"episode": {"app": "Birday", "truncated": True, "step_budget": 300},
            "counts": {"steps": 2}}
    page = render_report(_bundle(tmp_path, meta=meta)).read_text()

    assert "truncated" in page
    assert "episode valid" not in page


def test_the_timeline_shows_each_stretch_and_links_to_its_claim(tmp_path: Path) -> None:
    out = _bundle(tmp_path, findings={
        "areas": [
            {"feature": "add_event", "truth": "ok", "verdict": "ok",
             "outcome": "correct_ok", "segment": [1, 1], "attribution": "banked"},
            {"feature": "favorite", "truth": "broken", "verdict": "broken",
             "outcome": "true_positive", "segment": [2, 2], "attribution": "banked"},
        ],
        "agrees_with_score": True,
    })
    page = render_report(out).read_text()

    assert "What happened when" in page
    assert page.count('class="seg') == 2
    assert 'href="#s2"' in page          # a block jumps to the step it was claimed at


def test_areas_written_up_together_share_one_timeline_block(tmp_path: Path) -> None:
    """Overlapping segments must not be drawn as separate stretches of the episode."""
    shared = {"truth": "ok", "verdict": "ok", "outcome": "correct_ok",
              "segment": [1, 2], "attribution": "shared"}
    out = _bundle(tmp_path, findings={
        "areas": [{"feature": "add_event", **shared},
                  {"feature": "event_list", **shared}],
        "agrees_with_score": True,
    })
    page = render_report(out).read_text()

    assert page.count('class="seg') == 1
    assert "add_event, event_list" in page


def test_steps_carry_elapsed_time_from_the_capture_clock(tmp_path: Path) -> None:
    out = _bundle(tmp_path, steps=[
        {"step": 1, "kind": "action", "tool": "mobile_tap", "summary": "Tapped (1, 2)",
         "ok": True, "frame": {"path": "frames/00001.jpg", "hook_count": 1,
                               "captured_at": "2026-08-05T22:20:03+00:00"}},
    ], meta={"episode": {"app": "Birday", "started_at": "2026-08-05T22:18:38+00:00"},
             "counts": {"steps": 1}})
    page = render_report(out).read_text()

    assert "1:25" in page          # 85s after the episode started


def test_missing_bundle_returns_none(tmp_path: Path) -> None:
    assert render_report(tmp_path / "nope") is None


def test_result_line_unwraps_the_envelope_and_drops_the_coaching_tip() -> None:
    reply = json.dumps({"result": "Tapped (980, 2305)\nNext: call mobile_observe_screen"})

    assert _result_line(reply) == "Tapped (980, 2305)"
    assert _result_line("plain text\nsecond line") == "plain text"
    assert _result_line(None) == ""
