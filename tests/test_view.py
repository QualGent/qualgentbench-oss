"""`qualgent-bench view` — the local run visualizer (QUA-2823).

One synthetic run, built on disk with no device and no model: a claude-code MCP episode
and a codex-cli MCP episode that both received images, a raw-arm codex episode (shell
commands and their output), a held-out episode in the blinded (QUA-2806) layout and an
old-layout episode (absolute `artifact_dir`) with every optional file missing. Pinned:

* the index and one page per episode render, for both transcript formats and both arms;
* every image the agent received is extracted into a file beside its page;
* the rescored columns are exactly what `rescore_journey.py --dry-run` computes;
* held-out rows carry the badge and held-out pages the banner;
* `--out` outside the runs root is refused (and allowed only with the flag);
* the saved runs are never written;
* a view that fails does not fail `run`;
* (QUA-2840) every episode keeps its key, page and summary across builds; views built on
  two machines merge by copying `ep/` and `--index-from`; a credit-stopped run's view
  carries the partial-run badge and counts; `manifest.json` is format 2.

App and case names are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from qualgentbench import bugs, cli, corpus, journey, lanes, session, view, viz
from qualgentbench.rescore import rescore
from qualgentbench.result import RunResult
from qualgentbench.task import BenchmarkTask
from qualgentbench.transcript import timeline

RUN_ID = "20260925-000000-2823"
PNG_A = b"\x89PNG\r\n\x1a\n" + b"screen-a" * 8
PNG_B = b"\x89PNG\r\n\x1a\n" + b"screen-b" * 8
JPG_C = b"\xff\xd8\xff\xe0" + b"screen-c" * 8
B64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
FINDINGS_PASS = "verdict: pass\nbugs: []\n"
FINDINGS_FAIL = ("verdict: fail\nbugs:\n  - step: 2\n    screen: Item list\n"
                 "    observed: Widget Alpha is missing\n    expected: Widget Alpha listed\n"
                 "    description: the saved item does not appear in the list\n")


# ── synthetic corpus + episodes ────────────────────────────────────────────────

def _task(case: str, version: str, *, bugs_on: list[str] | None = None) -> BenchmarkTask:
    """What `journey.journey_tasks` hands the rescore, for a synthetic app."""
    spec = {"mode": "journey", "app_id": "demoapp", "case_id": case, "version": version,
            "name": case, "steps": ["Open the item list"],
            "expected_outcome": "The list shows the items.",
            "active_bugs": bugs_on or [], "step_budget": 30,
            "expected": "FAIL" if bugs_on else "PASS", "blocking": None,
            "blocking_texts": [], "crash_texts": [], "echo_texts": [], "absence_texts": [],
            "side": [], "defects": {},
            "oracle": journey._oracle({"check": {"expect": {"present": "Inbox"}}}),
            "truth_agrees": True}
    return BenchmarkTask(id=journey.task_id(case, version), name=case, instruction="",
                         app_file_id="", app_name="Demo", platform="android",
                         bundle_id="com.example.demo", bug_spec=spec)


def _claude_mcp_transcript() -> str:
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-test"},
        {"type": "user", "message": {"role": "user", "content": "Test the item list."}},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "thinking", "thinking": "I should look at the screen first."},
            {"type": "text", "text": "Reading the screen."},
            {"type": "tool_use", "id": "c1", "name": "mcp__device__mobile_observe_screen",
             "input": {"include_screenshot": True}},
            {"type": "tool_use", "id": "c2", "name": "mcp__device__mobile_tap",
             "input": {"x": 10, "y": 20}}]}},
        # A parallel batch: both results arrive after both calls.
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": B64(PNG_A)}},
                {"type": "text", "text": "Inbox \\u2022 Items\n" + "\n".join(
                    f"row {i}" for i in range(40))}]},
            {"type": "tool_result", "tool_use_id": "c2", "is_error": True,
             "content": "Error executing tool mobile_tap: out of bounds"}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "c3", "name": "Write",
             "input": {"file_path": "findings.yaml", "content": FINDINGS_PASS}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "c3", "content": "written"}]}},
        {"type": "result", "result": "Done: the list shows Inbox.", "usage": {
            "input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01},
    ]
    return "\n".join(json.dumps(x) for x in lines)


def _codex_mcp_transcript() -> str:
    call = {"id": "item_1", "type": "mcp_tool_call", "server": "device",
            "tool": "mobile_observe_screen", "arguments": "{\"include_screenshot\": true}"}
    lines = [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning",
                                            "text": "Plan: observe, then report."}},
        {"type": "item.started", "item": {**call, "status": "in_progress"}},
        {"type": "item.completed", "item": {**call, "status": "completed", "result": {
            "content": [{"type": "image", "data": B64(PNG_B), "mimeType": "image/png"},
                        {"type": "image", "data": B64(JPG_C), "mimeType": "image/jpeg"},
                        {"type": "text", "text": "Inbox"}]}}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
                                            "text": "Inbox is on screen."}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "error",
                                            "message": "stream disconnected; retrying"}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    return "\n".join(json.dumps(x) for x in lines)


def _codex_raw_transcript() -> str:
    dump = "\n".join(f'<node text="line {i}" />' for i in range(30))
    lines = [
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution",
                                          "command": "adb shell uiautomator dump /dev/tty",
                                          "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "c1", "type": "command_execution",
                                            "command": "adb shell uiautomator dump /dev/tty",
                                            "aggregated_output": dump + '\n<node text="Inbox" />',
                                            "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "c2", "type": "command_execution",
                                            "command": "adb shell input tap 5 5",
                                            "aggregated_output": "error: device offline",
                                            "exit_code": 1, "status": "failed"}},
        # Killed mid-flight: started, never completed.
        {"type": "item.started", "item": {"id": "c3", "type": "command_execution",
                                          "command": "adb shell input swipe 1 1 2 2",
                                          "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "c4", "type": "file_change",
                                            "changes": [{"path": "findings.yaml",
                                                         "kind": "add"}]}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    return "\n".join(json.dumps(x) for x in lines)


def _episode(runs: Path, case: str, version: str, name: str, *, agent: str, condition: str,
             transcript: str | None, findings: str | None, metrics: dict,
             brief: str | None = "Brief: test the list.", absolute: bool = False,
             blinded: bool = False, provenance: dict | None = None,
             model: str = "test-model", trial: int = 1) -> Path:
    ep = runs / (case if blinded else journey.task_id(case, version)) / name
    ep.mkdir(parents=True)
    if transcript is not None:
        (ep / "agent").mkdir()
        (ep / "agent" / "transcript.txt").write_text(transcript)
    if findings is not None:
        (ep / "workspace").mkdir()
        (ep / "workspace" / journey.FILENAME).write_text(findings)
    if brief is not None:
        (ep / "instruction_sent.md").write_text(brief)
    if blinded:
        (ep / "episode.json").write_text(json.dumps({
            "run_id": RUN_ID, "task_id": case, "blinded": True,
            "episode_id": name.rsplit("_", 1)[-1]}))
    rel = str(ep) if absolute else str(ep.relative_to(runs))
    result = {"task_id": journey.task_id(case, version), "task_version": "v",
              "task_type": journey.TASK_TYPE, "agent": agent, "model": model,
              "condition": condition, "trial": trial, "passed": False, "score": 0.0,
              "started_at": "2026-09-25T00:00:00+00:00",
              "ended_at": "2026-09-25T00:01:00+00:00", "wall_time_sec": 60.0,
              "exit_code": 0, "artifact_dir": rel, "run_id": RUN_ID,
              "metrics": {"version": version, "app_id": "demoapp", "case_id": case,
                          "steps": 3, "step_budget": 30, "cost_usd": 0.12,
                          "reported_status": "PASS", **metrics},
              "provenance": provenance or {}}
    RunResult.model_validate(result)
    (ep / "result.json").write_text(json.dumps(result, indent=2))
    return ep


#: case → (version, agent, condition, dir name)
EPISODES = {
    "claude": ("list-shows-items", "clean", "claude-code", "mcp", "ep-claude"),
    "codex": ("list-shows-items", "seeded", "codex-cli", "mcp", "ep-codex"),
    "raw": ("list-after-save", "clean", "codex-cli", "raw", "ep-raw"),
    "held": ("hidden-case", "clean", "codex-cli", "mcp",
             "2026-09-25T00-00-00Z_hidden-case_codex-cli_test-model_mcp_trial-1_ep-0123456789ab"),
    "old": ("list-after-save", "seeded", "claude-code", "raw", "ep-old-layout"),
}


def _tasks() -> dict:
    out = {}
    for case, version, *_ in EPISODES.values():
        t = _task(case, version, bugs_on=["item-not-saved"] if version == "seeded" else None)
        out[t.id] = t
    return out


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    c = EPISODES
    _episode(runs, *c["claude"][:2], c["claude"][4], agent="claude-code", condition="mcp",
             transcript=_claude_mcp_transcript(), findings=FINDINGS_PASS,
             metrics={"completed": False, "false_reports": 1, "bugs_found": [],
                      "bugs_present": []})
    _episode(runs, *c["codex"][:2], c["codex"][4], agent="codex-cli", condition="mcp",
             transcript=_codex_mcp_transcript(), findings=FINDINGS_FAIL,
             metrics={"completed": None, "false_reports": 0, "bugs_found": [],
                      "bugs_present": ["item-not-saved"], "reported_status": "FAIL",
                      "truncated": True})
    _episode(runs, *c["raw"][:2], c["raw"][4], agent="codex-cli", condition="raw",
             transcript=_codex_raw_transcript(), findings=FINDINGS_PASS,
             metrics={"completed": True, "false_reports": 0})
    _episode(runs, *c["held"][:2], c["held"][4], agent="codex-cli", condition="mcp",
             transcript=_codex_mcp_transcript(), findings=FINDINGS_PASS,
             metrics={"completed": True, "false_reports": 0, "heldout": True}, blinded=True)
    _episode(runs, *c["old"][:2], c["old"][4], agent="claude-code", condition="raw",
             transcript=None, findings=None, brief=None, absolute=True,
             metrics={"completed": None, "bugs_present": ["item-not-saved"]})
    return runs


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _rows(index: Path) -> list[dict]:
    text = index.read_text()
    m = re.search(r'<script type="application/json" id="rows">(.*?)</script>', text, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def _by_case(rows: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r["case"], r["cond"]): r for r in rows}


def _build(runs: Path, **kw) -> view.ViewResult:
    return view.build_view(runs, [RUN_ID], tasks_by_id=_tasks(), **kw)


# ── the timeline accessor ──────────────────────────────────────────────────────

def test_timeline_reads_claude_stream_json_in_order_with_images():
    tl = timeline(_claude_mcp_transcript())
    kinds = [e.kind for e in tl]
    assert kinds == ["prompt", "reasoning", "message", "call", "call", "result", "result",
                     "call", "result", "final"]
    call = tl[3]
    assert (call.name, call.server, call.id) == ("mobile_observe_screen", "device", "c1")
    shot = tl[5]
    assert shot.id == "c1" and [base64.b64decode(i.data) for i in shot.images] == [PNG_A]
    assert shot.images[0].media_type == "image/png"
    assert "Inbox • Items" in shot.text            # escapes decoded, like the scorer
    assert tl[6].id == "c2" and tl[6].is_error


def test_timeline_reads_codex_exec_json_with_images_errors_and_reasoning():
    tl = timeline(_codex_mcp_transcript())
    assert [e.kind for e in tl] == ["reasoning", "call", "result", "message", "error"]
    call, res = tl[1], tl[2]
    assert (call.name, call.server, call.input) == ("mobile_observe_screen", "device",
                                                    {"include_screenshot": True})
    assert call.id == res.id == "item_1"               # started + completed = one call
    assert [base64.b64decode(i.data) for i in res.images] == [PNG_B, JPG_C]
    assert [i.media_type for i in res.images] == ["image/png", "image/jpeg"]
    assert res.text == "Inbox"


def test_timeline_reads_raw_arm_shell_commands_and_output():
    tl = timeline(_codex_raw_transcript())
    assert [(e.kind, e.id) for e in tl] == [("call", "c1"), ("result", "c1"), ("call", "c2"),
                                             ("result", "c2"), ("call", "c3"), ("call", "c4")]
    assert tl[0].name == "shell" and tl[0].input == {"command": "adb shell uiautomator dump /dev/tty"}
    assert 'text="Inbox"' in tl[1].text and not tl[1].is_error
    assert tl[3].is_error and "[exit code 1]" in tl[3].text
    assert tl[4].input == {"command": "adb shell input swipe 1 1 2 2"}   # no result: killed
    assert tl[5].name == "file_change"


def test_timeline_skips_non_json_and_is_empty_for_nothing():
    assert timeline("") == []
    assert timeline("not json\n{broken\n") == []


# ── the view ───────────────────────────────────────────────────────────────────

def test_view_renders_an_index_and_a_page_per_episode(runs):
    res = _build(runs)
    assert res.out_dir == runs / "_runs" / RUN_ID / "view"
    assert res.index.is_file() and (res.out_dir / "style.css").is_file()
    rows = _rows(res.index)
    assert len(rows) == res.episodes == 5
    for row in rows:
        assert (res.out_dir / "ep" / f"{row['id']}.html").is_file(), row
    idx = res.index.read_text()
    assert "http://" not in idx and "https://" not in idx               # no CDN
    assert "<script src=" not in idx

    r = _by_case(rows)
    claude = (res.out_dir / "ep" / f"{r[('list-shows-items~clean', 'mcp')]['id']}.html").read_text()
    assert "mobile_observe_screen" in claude and "I should look at the screen first." in claude
    assert "Reading the screen." in claude and "Done: the list shows Inbox." in claude
    assert "Inbox • Items" in claude
    assert "click to expand" in claude                   # the 40-line result is folded
    assert "refused / failed" in claude                  # the refused tap
    assert "Brief: test the list." in claude and "verdict: pass" in claude
    assert "evidence viewer" not in claude               # no evidence/ dir: no dead link
    raw = (res.out_dir / "ep" / f"{r[('list-after-save~clean', 'raw')]['id']}.html").read_text()
    assert "$ adb shell uiautomator dump /dev/tty" in raw
    assert "[exit code 1]" in raw and "file_change" in raw
    codex = (res.out_dir / "ep" / f"{r[('list-shows-items~seeded', 'mcp')]['id']}.html").read_text()
    assert "stream disconnected; retrying" in codex and "Plan: observe" in codex
    assert "TRUNCATED" in codex and "Widget Alpha is missing" in codex
    assert r[("list-shows-items~seeded", "mcp")]["trunc"] is True


def test_every_image_the_agent_received_is_extracted_beside_its_page(runs):
    res = _build(runs)
    r = _by_case(_rows(res.index))
    want = {("list-shows-items~clean", "mcp"): [PNG_A],
            ("list-shows-items~seeded", "mcp"): [PNG_B, JPG_C],
            ("hidden-case~clean", "mcp"): [PNG_B, JPG_C],
            ("list-after-save~clean", "raw"): [],
            ("list-after-save~seeded", "raw"): []}
    total = 0
    for key, images in want.items():
        row = r[key]
        assert row["shots"] == len(images), key
        img_dir = res.out_dir / "ep" / row["id"]
        files = sorted(img_dir.iterdir()) if img_dir.exists() else []
        assert [f.read_bytes() for f in files] == images, key
        page = (res.out_dir / "ep" / f"{row['id']}.html").read_text()
        for f in files:
            assert f'src="{row["id"]}/{f.name}"' in page
        total += len(images)
    assert res.images == total
    assert [f.suffix for f in sorted((res.out_dir / "ep" / r[("list-shows-items~seeded", "mcp")]["id"]).iterdir())] == [".png", ".jpg"]


def test_rescored_columns_equal_rescore_journey_dry_run(runs):
    # The episodes as `rescore_journey.py --dry-run` sees them, before the view runs.
    tasks = _tasks()
    expected = {}
    for result in runs.glob("*/*/result.json"):
        status, _, _, v = rescore(result.parent, tasks, dry_run=True)
        if v is not None:
            m = v.metrics
            expected[(m["case_id"] + "~" + m["version"],
                      json.loads(result.read_text())["condition"])] = m
    res = _build(runs)
    rows = _by_case(_rows(res.index))
    assert set(expected) == {k for k, row in rows.items() if row["rescored"]}
    assert len(expected) == 4                              # the old-layout one has no transcript
    for key, m in expected.items():
        row = rows[key]
        assert row["c1"] == m.get("completed"), key
        assert row["b1"] == f"{len(m.get('bugs_found') or [])}/{len(m.get('bugs_present') or [])}"
        assert row["fr1"] == (m.get("false_reports") or 0), key
    # At least one verdict actually moves, so the comparison is not vacuous.
    assert any(row["moved"] for row in rows.values())
    old = rows[("list-after-save~seeded", "raw")]
    assert old["rescored"] is False and old["c1"] == "n/a"


def test_heldout_rows_carry_the_badge_and_pages_the_banner(runs):
    res = _build(runs)
    rows = _rows(res.index)
    held = [r for r in rows if r["held"]]
    assert [r["case"] for r in held] == ["hidden-case~clean"]
    idx = res.index.read_text()
    assert view.HELDOUT_BADGE in idx and "This view contains held-out episodes" in idx
    page = (res.out_dir / "ep" / f"{held[0]['id']}.html").read_text()
    assert view.HELDOUT_BANNER in page and 'class="banner"' in page
    for r in rows:
        if not r["held"]:
            other = (res.out_dir / "ep" / f"{r['id']}.html").read_text()
            assert view.HELDOUT_BANNER not in other
    assert "blinded episode dir" in page


def test_the_view_never_writes_the_saved_runs(runs):
    before = _snapshot(runs)
    res = _build(runs)
    after = {k: v for k, v in _snapshot(runs).items() if not k.startswith("_runs/")}
    assert after == before
    assert res.out_dir.is_relative_to(runs / "_runs")


def test_missing_files_and_old_layout_render(runs):
    res = _build(runs)
    row = _by_case(_rows(res.index))[("list-after-save~seeded", "raw")]
    page = (res.out_dir / "ep" / f"{row['id']}.html").read_text()
    assert "(no transcript)" in page and "(no instruction_sent.md)" in page
    assert "(no findings file)" in page
    assert "not rescored" in page


def test_a_rebuild_replaces_its_own_output(runs):
    res = _build(runs)
    stale = res.out_dir / "ep" / "9999.html"
    stale.write_text("stale")
    _build(runs)
    assert not stale.exists() and (res.out_dir / view.MARKER).is_file()


def test_several_runs_or_none_go_to_the_shared_view_dir(runs):
    res = view.build_view(runs, None, tasks_by_id=_tasks())
    assert res.out_dir == runs / "_runs" / view.MULTI_VIEW_DIRNAME
    assert res.episodes == 5


def test_no_rescore_shows_recorded_only(runs):
    res = _build(runs, rescore=False)
    assert res.rescored == 0
    assert all(not r["rescored"] and r["c1"] == "n/a" for r in _rows(res.index))


def test_out_outside_the_runs_root_is_refused_unless_allowed(runs, tmp_path):
    outside = tmp_path / "elsewhere"
    with pytest.raises(view.ViewError, match="outside the runs root"):
        _build(runs, out=outside)
    assert not outside.exists()
    res = _build(runs, out=outside, allow_outside_runs=True)
    assert res.index.is_file()


def test_out_into_a_directory_it_did_not_make_is_refused(runs):
    foreign = runs / "notes"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("mine")
    with pytest.raises(view.ViewError, match="not made by"):
        _build(runs, out=foreign)
    assert (foreign / "keep.txt").read_text() == "mine"
    with pytest.raises(view.ViewError, match="runs root itself"):
        _build(runs, out=runs, allow_outside_runs=True)


def test_an_unknown_run_id_is_an_error(runs):
    with pytest.raises(view.ViewError, match="no saved episodes for run nope"):
        view.build_view(runs, ["nope"], tasks_by_id=_tasks())


def test_cli_view_refuses_out_outside_runs(runs, tmp_path, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda app=None: _tasks())
    out = CliRunner().invoke(cli.main, ["view", "--runs-dir", str(runs), "--run", RUN_ID,
                                        "--out", str(tmp_path / "x")])
    assert out.exit_code != 0
    assert "outside the runs root" in out.output and "--allow-outside-runs" in out.output
    ok = CliRunner().invoke(cli.main, ["view", "--runs-dir", str(runs), "--run", RUN_ID])
    assert ok.exit_code == 0, ok.output
    assert "View written" in ok.output and "5 episode(s)" in ok.output
    assert (runs / "_runs" / RUN_ID / "view" / "index.html").is_file()


# ── `run` writes the view, and a failing view never fails it ──────────────────

SUITE = {
    "app": {"id": "demoapp", "name": "Demo", "package": "com.example.demo",
            "platform": "android", "difficulty": "easy"},
    "apk": {"repo": "example/apps", "filename": "easy/demoapp.apk", "sha256": "a" * 64},
    "exploration": {"id": "explore-demoapp", "features": [{"id": "a", "state": "broken"}]},
    "tasks": [{"id": "demoapp-t1", "bug_id": "b1", "type": "bug"}],
}
SERIAL = "emulator-5666"


class _FakeSession:
    def __init__(self, mcp_server=None) -> None:
        pass

    async def first_available_device(self):
        return SERIAL

    async def available_devices(self):
        return [SERIAL]

    async def is_healthy(self):
        return True


async def _nothing(*args, **kwargs):
    return None


class _Engine:
    """Stands in for the lanes: 'finishes' one episode, starts no agent."""

    async def __call__(self, plan, cfg):
        cfg.results.append(RunResult(
            task_id="demoapp-t1", task_version="v", task_type="bug_hunt", agent="codex-cli",
            model="gpt-5.5", condition="raw", trial=1, passed=False, score=0.0,
            started_at="2026-09-25T00:00:00+00:00", ended_at="2026-09-25T00:01:00+00:00",
            wall_time_sec=60.0, exit_code=0, run_id=cfg.run_id, metrics={"cost_usd": 0.0}))
        return cfg.results


@pytest.fixture
def fake_run(monkeypatch, tmp_path: Path):
    apk = tmp_path / "demoapp.apk"
    apk.write_bytes(b"apk")
    monkeypatch.setattr(bugs, "load_apps", lambda *a, **kw: [SUITE])
    monkeypatch.setattr(cli, "_resolve_app_apk", lambda app, spec=None, mode="hunt": apk)
    monkeypatch.setattr(cli, "_preflight", _nothing)
    monkeypatch.setattr(cli, "_gate_agent_dump", _nothing)
    monkeypatch.setattr(session, "DeviceSession", _FakeSession)
    monkeypatch.setattr(lanes, "run_lanes", _Engine())

    def run():
        return CliRunner().invoke(
            cli.main,
            ["run", "--agent", "codex-cli", "--models", "gpt-5.5", "--mode", "hunt",
             "--app", "demoapp", "--devices", SERIAL, "--plain", "--yes",
             "--runs-dir", str(tmp_path / "runs"), "--run-id-file", str(tmp_path / "run_id")])
    return run


def test_run_writes_the_view_beside_board_json(fake_run, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(view, "build_view", lambda runs_dir, run_ids, *a, **kw: calls.append(
        (Path(runs_dir), list(run_ids))) or view.ViewResult(
        out_dir=tmp_path, index=tmp_path / "index.html"))
    out = fake_run()
    assert out.exit_code == 0, out.output
    run_id = (tmp_path / "run_id").read_text().splitlines()[0].strip()
    assert calls == [(tmp_path / "runs", [run_id])]
    assert (tmp_path / "runs" / "_runs" / run_id / "board.json").is_file()


def test_a_failing_view_does_not_fail_run(fake_run, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("renderer exploded")
    monkeypatch.setattr(view, "build_view", boom)
    out = fake_run()
    assert out.exit_code == 0, out.output
    assert "Episode view not written" in out.output and "renderer exploded" in out.output
    run_id = (tmp_path / "run_id").read_text().splitlines()[0].strip()
    assert (tmp_path / "runs" / "_runs" / run_id / "board.json").is_file()
    assert f"qualgent-bench view --run {run_id}" in out.output


# ── --portable and manifest.json (QUA-2833) ────────────────────────────────────

def _evidence(ep: Path) -> None:
    (ep / "evidence" / "frames").mkdir(parents=True)
    (ep / "evidence" / "index.html").write_text('<img src="frames/00001.jpg">')
    (ep / "evidence" / "frames" / "00001.jpg").write_bytes(JPG_C)


def _hrefs(page: str) -> list[str]:
    return re.findall(r'(?:href|src)="([^"#]+)"', page)


def test_portable_view_stands_alone(runs, tmp_path):
    c = EPISODES["claude"]
    _evidence(runs / journey.task_id(c[0], c[1]) / c[4])
    res = _build(runs, portable=True)
    assert res.portable
    rows = _by_case(_rows(res.index))
    claude = rows[("list-shows-items~clean", "mcp")]["id"]
    page = (res.out_dir / "ep" / f"{claude}.html").read_text()
    for link in (f"{claude}/transcript.txt", f"{claude}/result.json",
                 f"{claude}/evidence/index.html"):
        assert f'href="{link}"' in page, link
    assert "episode folder" not in page
    ep_dir = res.out_dir / "ep" / claude
    assert (ep_dir / "transcript.txt").read_text() == _claude_mcp_transcript()
    assert json.loads((ep_dir / "result.json").read_text())["task_id"].startswith("list-shows")
    assert (ep_dir / "evidence" / "frames" / "00001.jpg").read_bytes() == JPG_C
    assert view.PORTABLE_NOTE in res.index.read_text()
    assert view.LOCAL_ONLY_NOTE not in res.index.read_text()

    # Moved elsewhere, every relative link on every page still resolves.
    moved = tmp_path / "elsewhere"
    import shutil
    shutil.copytree(res.out_dir, moved)
    for html_page in [moved / "index.html", *(moved / "ep").glob("*.html")]:
        for href in _hrefs(html_page.read_text()):
            if href.startswith("ep/${"):              # the index's JS row template
                continue
            target = (html_page.parent / href).resolve()
            assert target.is_relative_to(moved.resolve()), (html_page.name, href)
            assert target.exists(), (html_page.name, href)


def test_portable_episode_without_files_links_nothing_outside(runs):
    res = _build(runs, portable=True)
    row = _by_case(_rows(res.index))[("list-after-save~seeded", "raw")]
    page = (res.out_dir / "ep" / f"{row['id']}.html").read_text()
    assert "(no transcript)" in page
    assert "raw transcript" not in page and "episode folder" not in page
    assert all(not h.startswith("../..") for h in _hrefs(page))


def test_every_view_writes_a_manifest(runs):
    res = _build(runs)
    m = json.loads((res.out_dir / view.MANIFEST).read_text())
    assert m["format"] == view.MANIFEST_FORMAT == 2 and m["portable"] is False
    assert m["episodes"] == 5 and m["held_out"] == 1
    [run] = m["runs"]
    assert run["run_id"] == RUN_ID and run["episodes"] == 5 and run["held_out"] == 1
    assert run["started_at"] == "2026-09-25T00:00:00+00:00"
    assert run["agents"] == ["claude-code · test-model", "codex-cli · test-model"]
    assert run["conditions"] == ["mcp", "raw"] and run["arms"] == ["clean", "seeded"]
    assert 0 <= run["completed"] <= run["scored"] <= run["episodes"]
    assert json.loads((_build(runs, portable=True).out_dir / view.MANIFEST).read_text())[
        "portable"] is True
    # QUA-2917: what the run measured, additive at format 2.
    from qualgentbench.checkpoint import package_version
    assert m["qualgentbench_version"] == package_version()
    assert m["notes"] == {"rates_legend": view.BOARD_RATES_LEGEND,
                          "blocker_off_note": view.BLOCKER_OFF_NOTE,
                          "ranking_note": journey.RANKING_NOTE,
                          "mixed_corpus_note": journey.MIXED_CORPUS_NOTE,
                          "mixed_brief_note": journey.MIXED_BRIEF_NOTE,
                          "plain": view.PLAIN}                         # QUA-2938
    # QUA-2931: the legend describes only what `board` carries — no blocker recall.
    assert "blocker recall =" not in m["notes"]["rates_legend"]
    assert not {k for row in run["board"]["now"] + run["board"]["recorded"] for k in row
                if k.startswith("blocker")}
    v = run["versions"]
    assert v["mode"] == "journey" and v["modes"] == [journey.TASK_TYPE]
    assert v["corpus"] is None and v["corpus_unstamped"] == 5      # unstamped fixtures
    assert v["mixed"] is False and v["set_key"] is None and run["set_key"] is None
    assert v["conditions"] == ["mcp", "raw"] and v["condition"] is None
    assert v["devloops"] == ["unstamped"]
    # QUA-2934: the recorded scorer — none of the fixtures' verdicts names one.
    assert v["scorer_versions"] == [] and v["scorer_unstamped"] == 5
    # An explicit corpus: rescored, but nothing to name the corpus by.
    assert run["rescored_with"] == {
        "corpus": None, "corpus_versions": [], "heldout": None, "heldout_versions": [],
        "scorer": None, "scorer_versions": [], "stamped": 0, "unstamped": 4,
        "not_rescored": 1}
    assert run["public"]["episodes"] == 4 and run["heldout"]["episodes"] == 1
    assert run["public"]["cases"] == 2 and run["public"]["apps"] == 1
    assert run["heldout"]["cases"] == 1
    assert (run["public"]["completed"] + run["heldout"]["completed"]) == run["completed"]
    assert (run["public"]["scored"] + run["heldout"]["scored"]) == run["scored"]
    assert run["models"] == [
        {"agent": "claude-code", "model": "test-model", "model_raw": "test-model",
         "provider": None, "condition": "mcp"},
        {"agent": "claude-code", "model": "test-model", "model_raw": "test-model",
         "provider": None, "condition": "raw"},
        {"agent": "codex-cli", "model": "test-model", "model_raw": "test-model",
         "provider": None, "condition": "mcp"},
        {"agent": "codex-cli", "model": "test-model", "model_raw": "test-model",
         "provider": None, "condition": "raw"}]
    board = run["board"]
    assert set(board) == {"now", "recorded", "by_app_now", "by_app_recorded"}
    assert sum(r["episodes"] + r["excluded_episodes"] for r in board["recorded"]) == 5
    assert sum(r["not_rescored"] for r in board["recorded"]) == 1
    assert sum(r["episodes"] + r["excluded_episodes"] for r in board["now"]) == 4
    assert board["now"] == sorted(board["now"], key=journey.ranking_key)
    assert all(set(r) == set(view._BY_APP_KEYS) for r in board["by_app_now"])
    assert len(run["cases"]) == run["episodes"]
    assert isinstance(run["moved"], int) and isinstance(run["present_changed"], int)


def test_cli_view_portable(runs, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    out = CliRunner().invoke(cli.main, ["view", "--run", RUN_ID, "--runs-dir", str(runs),
                                        "--portable"])
    assert out.exit_code == 0, out.output
    assert "Portable" in out.output
    m = json.loads((runs / "_runs" / RUN_ID / "view" / view.MANIFEST).read_text())
    assert m["portable"] is True


# ── what the run measured: the manifest's additive block (QUA-2917) ─────────────

CORPUS_A, CORPUS_B, SPLIT = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"
SERVER = {"name": "devloop", "version": "1", "app_source": "none",
          "tools_sha256": "1" * 64, "instructions_sha256": "2" * 64}


def _stamped(runs: Path, corpora: tuple[str, ...] = (CORPUS_A, CORPUS_A)) -> Path:
    """Two public episodes, one per entry of `corpora`, and a held-out one: stamped as a
    journey run stamps them (corpus + held-out version in metrics, brief + MCP server in
    provenance); the held-out one ran trial 2."""
    prov = {"brief_version": 3, "mcp_server": SERVER}
    for i, c in enumerate(corpora):
        _episode(runs, "list-shows-items", ("clean", "seeded")[i % 2], f"ep-s{i}",
                 agent="codex-cli", condition="mcp", transcript=_codex_mcp_transcript(),
                 findings=FINDINGS_PASS, provenance=prov,
                 model="accounts/fireworks/models/test-model",
                 metrics={"completed": True, "false_reports": 0, "corpus_version": c,
                          "heldout_version": SPLIT, "fault_fired": ["item-not-saved"],
                          "bugs_present": ["item-not-saved"] if i % 2 else []})
    _episode(runs, "hidden-case", "clean", "ep-sh", agent="codex-cli", condition="mcp",
             transcript=_codex_mcp_transcript(), findings=FINDINGS_PASS, provenance=prov,
             model="accounts/fireworks/models/test-model", trial=2,
             metrics={"completed": True, "false_reports": 0, "heldout": True,
                      "corpus_version": corpora[0], "heldout_version": SPLIT})
    return runs


def _run_entry(res: view.ViewResult) -> dict:
    [run] = json.loads((res.out_dir / view.MANIFEST).read_text())["runs"]
    return run


def test_versions_and_set_key_come_from_the_stamped_episodes(tmp_path):
    run = _run_entry(_build(_stamped(tmp_path / "runs")))
    v = run["versions"]
    assert (v["corpus"], v["heldout"], v["brief"]) == (CORPUS_A, SPLIT, "3")
    assert v["corpus_unstamped"] == v["heldout_unstamped"] == v["brief_unstamped"] == 0
    assert v["condition"] == "mcp" and v["devloop"] == "11111111/22222222"
    assert v["mixed"] is False
    assert run["set_key"] == v["set_key"] == f"j-{CORPUS_A}-{SPLIT}-b3"
    assert run["models"] == [{"agent": "codex-cli", "model": "test-model",
                              "model_raw": "accounts/fireworks/models/test-model",
                              "provider": "fireworks", "condition": "mcp"}]


def test_a_run_over_two_corpora_is_mixed_and_has_no_set_key(tmp_path):
    run = _run_entry(_build(_stamped(tmp_path / "runs", (CORPUS_A, CORPUS_B))))
    v = run["versions"]
    assert v["mixed"] is True and v["corpus"] is None
    assert v["corpus_versions"] == [CORPUS_A, CORPUS_B]
    assert run["set_key"] is None and v["set_key"] is None


def test_devloop_lane_labels():
    def r(prov):
        return RunResult(task_id="c~clean", task_version="v", task_type=journey.TASK_TYPE,
                         agent="a", model="m", condition="raw", trial=1, passed=False,
                         score=0.0, started_at="2026-09-25T00:00:00+00:00",
                         ended_at="2026-09-25T00:01:00+00:00", wall_time_sec=1.0,
                         exit_code=0, provenance=prov)
    assert view._devloop(r({})) == "unstamped"
    assert view._devloop(r({"mcp_server": None})) == "bare"
    assert view._devloop(r({"mcp_server": {"error": "down"}})) == "unstamped"
    assert view._devloop(r({"mcp_server": SERVER})) == "11111111/22222222"


def test_no_board_row_carries_blocker_fields(runs):
    board = _run_entry(_build(runs))["board"]
    rows = [row for name in board for row in board[name]]
    assert rows and not [k for row in rows for k in row if k.startswith("blocker")]


def test_one_case_row_per_journey_episode_with_its_trial(tmp_path):
    res = _build(_stamped(tmp_path / "runs"))
    run = _run_entry(res)
    cases = run["cases"]
    assert len(cases) == run["episodes"] == 3
    assert {c["key"] for c in cases} == {r["id"] for r in _rows(res.index)}
    assert [c["held"] for c in cases] == [False, False, True]   # public first
    assert [c["trial"] for c in cases] == [1, 1, 2]
    pub = cases[1]
    assert pub["arm"] == "seeded" and pub["present"] == ["item-not-saved"]
    assert pub["fired"] == ["item-not-saved"] and pub["rescored"] is True
    assert pub["found_now"] is not None and pub["reports_now"] is not None
    assert pub["model"] == "test-model" and pub["excluded"] == ""
    assert set(cases[0]) == {
        "key", "case_id", "app_id", "arm", "held", "agent", "model", "condition", "trial",
        "started_at", "completed_rec", "completed_now", "present", "found_rec", "found_now",
        "fired", "reports_now", "unmatched_grounded_now", "fr_rec", "fr_now", "truncated",
        "steps", "step_budget", "excluded", "cost_usd", "cost_source", "moved", "rescored",
        "recorded_is_rescore"}
    assert not any(c["recorded_is_rescore"] for c in cases)    # all run-time verdicts
    assert run["moved"] == sum(c["moved"] for c in cases)


def test_rescored_with_is_stamped_only_under_the_default_corpus(runs, tmp_path, monkeypatch):
    from qualgentbench import corpus
    # An explicit corpus: no stamp in any summary, none in the manifest.
    explicit = _build(runs)
    docs = [json.loads(p.read_text()) for p in (explicit.out_dir / "ep").glob("*.json")]
    assert docs and not [d for d in docs if "rescored_with" in d]
    # The default corpus: every rescored summary carries corpus.stamp() and the scorer
    # its rescored verdict names (QUA-2927), nothing else.
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    default = view.build_view(runs, [RUN_ID], tmp_path / "d", allow_outside_runs=True)
    docs = [json.loads(p.read_text()) for p in (default.out_dir / "ep").glob("*.json")]
    stamp = {**corpus.stamp(), "scorer_version": str(journey.SCORER_VERSION)}
    assert [d["rescored_with"] for d in docs if d["rescored_result"] is not None] == [stamp] * 4
    assert not [d for d in docs if d["rescored_result"] is None and "rescored_with" in d]
    rw = _run_entry(default)["rescored_with"]
    assert rw["corpus"] == stamp["corpus_version"] and rw["stamped"] == 4
    assert rw["scorer"] == str(journey.SCORER_VERSION)
    assert rw["scorer_versions"] == [str(journey.SCORER_VERSION)]
    assert rw["unstamped"] == 0 and rw["not_rescored"] == 1
    # No rescore at all: no stamp either.
    off = view.build_view(runs, [RUN_ID], tmp_path / "o", rescore=False,
                          allow_outside_runs=True)
    assert not [p for p in (off.out_dir / "ep").glob("*.json")
                if "rescored_with" in json.loads(p.read_text())]


def test_index_from_reproduces_the_stamped_manifest_byte_for_byte(runs, tmp_path,
                                                                     monkeypatch):
    import shutil
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    full = view.build_view(runs, [RUN_ID], tmp_path / "full", allow_outside_runs=True,
                           portable=True)
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    view.build_index(copy)
    assert (copy / "index.html").read_bytes() == full.index.read_bytes()
    a = (copy / view.MANIFEST).read_text().splitlines()
    b = (full.out_dir / view.MANIFEST).read_text().splitlines()
    assert [x for x in a if '"generated_at"' not in x] == [
        x for x in b if '"generated_at"' not in x]
    assert json.loads((copy / view.MANIFEST).read_text())["runs"][0]["rescored_with"][
        "stamped"] == 4


def test_a_summary_reads_back_its_stamp_and_rejects_a_bad_one(runs, tmp_path, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    res = view.build_view(runs, [RUN_ID], tmp_path / "d", allow_outside_runs=True)
    path = next(p for p in (res.out_dir / "ep").glob("*.json")
                if "rescored_with" in json.loads(p.read_text()))
    doc = json.loads(path.read_text())
    s = view._Summary.from_json(doc, path)
    assert s.rescored_with == doc["rescored_with"] and s.as_json() == doc
    with pytest.raises(view.ViewError, match="rescored_with"):
        view._Summary.from_json({**doc, "rescored_with": "x"}, path)


def test_a_manifest_for_a_large_run_stays_small(tmp_path):
    # 108 episodes, as a full public + held-out board: the manifest carries one case row
    # each and must stay small enough to fetch whole (QUA-2917: ~150 KB).
    runs = tmp_path / "runs"
    for i in range(108):
        _episode(runs, f"case-{i % 27:02d}", ("clean", "seeded")[i % 2], f"ep-{i:03d}",
                 agent="codex-cli", condition="mcp", transcript=None, findings=None,
                 brief=None, trial=1 + i // 54,
                 metrics={"completed": bool(i % 3), "false_reports": i % 2,
                          "bugs_present": ["item-not-saved"] if i % 2 else [],
                          "bugs_found": [], "heldout": i >= 88})
    res = view.build_view(runs, [RUN_ID], tasks_by_id={})
    assert len(_run_entry(res)["cases"]) == 108
    assert (res.out_dir / view.MANIFEST).stat().st_size < 150_000


# ── the run page: versions, the full board, charts R2–R4 (QUA-2918) ─────────────

def _board_table(idx: str) -> list[list[str]]:
    """The board's body rows as cell texts, tags stripped."""
    table = re.search(r"<table class=board>(.*?)</table>", idx, re.S).group(1)
    rows = re.findall(r"<tr><td>(.*?)</td></tr>", table, re.S)
    return [[html_unescape(c) for c in re.split(r"</td><td[^>]*>", r)] for r in rows]


def _versions_line(idx: str) -> str:
    line = re.search(r'<p class="versions">(.*?)</p>', idx, re.S).group(1)
    return html_unescape(re.sub(r"<[^>]+>", "", line))     # QUA-2938: chips are spans


def test_the_board_is_the_cli_board_in_ranking_order(runs):
    res = _build(runs)
    idx = res.index.read_text()
    board = _run_entry(res)["board"]
    now = {viz.row_id(r): r for r in board["now"]}
    shown = sorted(board["now"] + [r for r in board["recorded"] if viz.row_id(r) not in now],
                   key=journey.ranking_key)
    table = _board_table(idx)
    assert len(table) == len(shown) == 5
    for cells, row in zip(table, shown):
        c, money = journey.rates_cells(row), journey.cost_cells(row)
        assert cells[1] == f"{row['agent']} · {row['model']} · {row['condition']}"
        assert cells[4:7] == [c["false_alarm"], c["catch"], c["integrity"]]
        assert cells[9:11] == [money["cost"], money["minutes"]]
        assert ("held-out" in cells[2]) is bool(row["heldout"])
    # Public block numbered 1.., then the held-out block H1..; intervals printed.
    assert [r[0] for r in table] == ["1", "2", "3", "4", "H1"]
    assert re.search(r"\d+/\d+ \d+% \[\d+–\d+\]", idx)
    # The row with no rescored episode shows its recorded numbers and says so; a
    # truncated one counts it; a row the rescore moved shows what it recorded.
    old = next(r for r in table if r[1] == "claude-code · test-model · raw")
    assert old[-1] == "not rescored: recorded shown"
    assert "1 truncated" in next(r for r in table if r[1] == "codex-cli · test-model · mcp"
                                 and "public" in r[2])[3]
    moved = next(r for r in table if r[1] == "claude-code · test-model · mcp")
    assert moved[-1].startswith("false alarm 1/1 100%")
    assert view.BOARD_RATES_LEGEND in html_unescape(idx)
    assert view.BLOCKER_OFF_NOTE in html_unescape(idx)
    assert "blocker recall =" not in idx and journey.RANKING_NOTE in idx


def test_the_versions_line_names_the_measurement_and_the_rescore(tmp_path, monkeypatch):
    from qualgentbench import corpus
    runs = _stamped(tmp_path / "runs")
    explicit = _build(runs).index.read_text()
    line = _versions_line(explicit)
    for part in ("benchmark: journey", f"corpus {CORPUS_A}",
                 f"held-out split {SPLIT} (1 episode)", "brief v3", "arm mcp",
                 "DevLoop 11111111/22222222", f"set j-{CORPUS_A}-{SPLIT}-b3"):
        assert part in line, part
    assert "MIXED" not in explicit
    assert "rescore corpus unstamped" in explicit and "0 not rescored" in explicit
    # The default corpus, the same one the run recorded.
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    monkeypatch.setattr(corpus, "stamp", lambda: {"corpus_version": CORPUS_A,
                                                  "heldout_version": SPLIT})
    same = view.build_view(runs, [RUN_ID], tmp_path / "same", allow_outside_runs=True)
    assert "rescored with the recorded corpus" in same.index.read_text()
    # A different one: a different measurement, not a correction.
    monkeypatch.setattr(corpus, "stamp", lambda: {"corpus_version": CORPUS_B,
                                                  "heldout_version": SPLIT})
    other = view.build_view(runs, [RUN_ID], tmp_path / "other", allow_outside_runs=True)
    assert (f"recorded under corpus {CORPUS_A} · held-out {SPLIT}, rescored with corpus "
            f"{CORPUS_B} · held-out {SPLIT}: a different measurement, not a correction"
            in html_unescape(other.index.read_text()))
    # No rescore at all.
    off = view.build_view(runs, [RUN_ID], tmp_path / "off", rescore=False,
                          allow_outside_runs=True)
    assert "not rescored: the board shows the verdicts recorded at run time" in (
        off.index.read_text())


def test_the_rescore_sentence_names_held_out_like_the_version_line():
    # QUA-2931: a run with no held-out split stamps no held-out version on any episode
    # (all "unstamped"): the version line says "none", and so must the sentence.
    v = {"corpus": CORPUS_A, "corpus_versions": [CORPUS_A], "corpus_unstamped": 0,
         "heldout": None, "heldout_versions": [], "heldout_unstamped": 3}
    rw = {"corpus": CORPUS_B, "corpus_versions": [CORPUS_B], "heldout": None,
          "heldout_versions": [], "stamped": 3, "unstamped": 0, "not_rescored": 0}
    assert view._heldout_text(v, 0) == "none"
    sentence = view._rescore_sentence(v, rw, 0, 0)
    assert sentence.startswith(f"recorded under corpus {CORPUS_A} · held-out none, "
                               f"rescored with corpus {CORPUS_B} · held-out none:")
    assert "unstamped" not in sentence
    assert view._rescore_sentence(v, {**rw, "corpus": CORPUS_A}) == (
        "rescored with the recorded corpus")


def test_a_rebuild_without_the_held_out_split_says_held_out_was_not_rescored(
        tmp_path, monkeypatch):
    # QUA-2931: the run recorded a held-out version; the build has no held-out split, so
    # only the public episodes were rescored. That is not a different measurement.
    from qualgentbench import corpus
    runs = _stamped(tmp_path / "runs")
    public = {k: t for k, t in _tasks().items() if not k.startswith("hidden-case")}
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: public)
    monkeypatch.setattr(corpus, "stamp", lambda: {"corpus_version": CORPUS_A,
                                                  "heldout_version": None})
    res = view.build_view(runs, [RUN_ID], tmp_path / "same", allow_outside_runs=True)
    raw = res.index.read_text()
    idx = html_unescape(raw)
    assert _run_entry(res)["rescored_with"]["not_rescored"] == 1
    assert ("rescored with the recorded corpus; no held-out split at build time: "
            "1 held-out episode not rescored" in idx)
    assert "a different measurement" not in idx
    assert f"held-out split {SPLIT} (1 episode)" in _versions_line(raw)
    # A different public corpus is still a different measurement — on the corpus.
    monkeypatch.setattr(corpus, "stamp", lambda: {"corpus_version": CORPUS_B,
                                                  "heldout_version": None})
    other = html_unescape(view.build_view(runs, [RUN_ID], tmp_path / "other",
                                          allow_outside_runs=True).index.read_text())
    assert (f"recorded under corpus {CORPUS_A}, rescored with corpus {CORPUS_B}: a "
            f"different measurement, not a correction (docs/heldout.md); no held-out "
            f"split at build time: 1 held-out episode not rescored" in other)


def test_a_mixed_run_shows_the_mixed_badge(tmp_path):
    idx = _build(_stamped(tmp_path / "runs", (CORPUS_A, CORPUS_B))).index.read_text()
    assert '<span class="mixed">MIXED</span>' in idx
    assert f"corpus: {CORPUS_A}, {CORPUS_B}" in idx and "set —" in _versions_line(idx)


def test_the_index_draws_inline_svg_charts_with_a_cell_per_episode(runs):
    res = _build(runs)
    idx = res.index.read_text()
    assert idx.count("<svg ") >= 2 and "xmlns" not in idx
    assert "http://" not in idx and "https://" not in idx and "<script src=" not in idx
    strip = re.search(r'<svg [^>]*aria-label="5 episodes by status">.*?</svg>', idx, re.S)
    assert strip and strip.group(0).count("<rect ") == res.episodes == 5
    assert idx.count("<rect ") == res.episodes                  # only the strip draws rects
    for row in _rows(res.index):
        assert f'href="ep/{row["id"]}.html"><g class="cell' in strip.group(0)
    # Held-out marks are hollow; every chart has its table twin.
    assert 'class="pt ho' in idx
    assert idx.count("as a table</summary>") == idx.count("<svg ")
    # The truncated seeded episode and the old silent one carry their status.
    assert '<g class="cell st-warn"><title>list-shows-items · trial 1' in strip.group(0)
    assert "missed · 0 reports, none grounded" in strip.group(0)


def test_drift_renders_only_when_the_rescore_moved_something(runs, tmp_path):
    moved = _build(runs)
    assert _run_entry(moved)["moved"] > 0
    idx = moved.index.read_text()
    assert "Rescore drift" in idx and 'aria-label="recorded vs rescored' in idx
    still = view.build_view(runs, [RUN_ID], tmp_path / "off", rescore=False,
                            tasks_by_id=_tasks(), allow_outside_runs=True)
    assert _run_entry(still)["moved"] == 0
    assert "Rescore drift" not in still.index.read_text()


def test_board_rows_and_results_share_one_row_key(runs):
    # QUA-2931: `journey.row_key` (a result's row) is `viz.row_id` (the row's identity),
    # so the board's per-row not-rescored count cannot silently drop to 0.
    from qualgentbench.leaderboard import load_results
    results = [r for r in load_results(runs) if r.task_type == journey.TASK_TYPE]
    for by_app in (False, True):
        rows = {viz.row_id(r): r for r in journey.summary(results, by_app=by_app)}
        counts: dict[tuple, int] = {}
        for r in results:
            counts[journey.row_key(r, by_app)] = counts.get(journey.row_key(r, by_app), 0) + 1
        assert set(counts) == set(rows)
        for k, n in counts.items():
            assert rows[k]["episodes"] + rows[k]["excluded_episodes"] == n
    run = _run_entry(_build(runs))
    missing = run["rescored_with"]["not_rescored"]
    assert missing == 1
    for name in ("recorded", "by_app_recorded"):
        assert sum(r["not_rescored"] for r in run["board"][name]) == missing


def _twins(page: str) -> list[str]:
    return re.findall(r"<details><summary[^>]*>[^<]* as a table</summary>.*?</details>",
                      page, re.S)


def test_every_chart_has_one_caption_style_and_one_twin_style(runs):
    # QUA-2931: run-page twins and captions are the experiment page's (X1/X2).
    idx = _build(runs).index.read_text()
    twins = _twins(idx)
    assert twins and len(twins) == idx.count("<svg ")
    for t in twins:
        assert t.startswith('<details><summary class="dim">')
        assert '<div class="tablewrap"><table class="idx"><thead><tr><th>' in t
        assert "</thead><tbody>" in t
    assert "class=twin" not in idx
    figs = re.findall(r'<figure class="fig">(.*?)</figure>', idx, re.S)
    assert len(figs) == idx.count("<svg ")
    assert all(f.count("<svg ") == 1 and '<figcaption class="dim"' in f for f in figs)


def test_rates_chart_puts_each_app_under_its_lane(tmp_path):
    runs = tmp_path / "runs"
    for i, app in enumerate(("alphaapp", "betaapp", "alphaapp", "betaapp")):
        _episode(runs, f"{app}-case", ("clean", "seeded")[i // 2], f"ep-{i}",
                 agent="codex-cli", condition="mcp", transcript=None, findings=None,
                 brief=None, metrics={"app_id": app, "completed": True, "false_reports": 0,
                                      "bugs_present": ["x"] if i // 2 else [],
                                      "bugs_found": ["x"] if i == 2 else []})
    idx = view.build_view(runs, [RUN_ID], tasks_by_id={}).index.read_text()
    rates = re.search(r'<svg [^>]*aria-label="rates with 95% intervals.*?</svg>', idx, re.S)
    assert "↳ alphaapp" in rates.group(0) and "↳ betaapp" in rates.group(0)
    strip = re.search(r'<svg [^>]*aria-label="4 episodes by status">.*?</svg>', idx, re.S)
    assert strip.group(0).count("st-good") >= 3                # 2 clean + 1 caught
    assert "missed" in strip.group(0)


def _summary_of(arm: str, metrics: dict, rescored: dict | None = None) -> view._Summary:
    def r(m: dict) -> RunResult:
        return RunResult(task_id=f"c~{arm}", task_version="v", task_type=journey.TASK_TYPE,
                         agent="a", model="m", condition="mcp", trial=1, passed=False,
                         score=0.0, started_at="2026-09-25T00:00:00+00:00",
                         ended_at="2026-09-25T00:01:00+00:00", wall_time_sec=1.0,
                         exit_code=0, metrics={"version": arm, **m})
    return view._Summary(key="k", run_id=RUN_ID, row={}, held=False, arm=arm,
                         rescore_status="", result=r(metrics),
                         rescored_result=r(rescored) if rescored is not None else None)


@pytest.mark.parametrize("arm,recorded,rescored,status", [
    ("seeded", {"bugs_present": ["b"], "bugs_found": []}, {"bugs_present": ["b"],
                                                           "bugs_found": ["b"]}, "caught"),
    ("seeded", {"bugs_present": ["b"], "truncated": True}, None, "truncated"),
    ("seeded", {"bugs_present": ["b"], "fault_fired": []}, None, "unreached"),
    ("seeded", {"bugs_present": ["b"], "fault_fired": ["b"],
                "reports": [{"matched": False, "grounded": True}]}, None, "artifact"),
    ("seeded", {"bugs_present": ["b"], "reports": [{"matched": False}]}, None, "silent"),
    ("seeded", {"bugs_present": ["b"], "env_failure": True}, None, "excluded"),
    ("clean", {"false_reports": 0}, {"false_reports": 2}, "false_report"),
    ("clean", {"false_reports": 1}, {"false_reports": 0}, "clean"),
    # QUA-2931: nothing present under the "now" verdict is nothing to miss.
    ("seeded", {"bugs_present": ["b"]}, {"bugs_present": [], "bugs_found": []}, "excluded"),
    ("seeded", {"bugs_present": [], "fault_fired": []}, None, "excluded"),
    ("seeded", {"bugs_present": [], "reports": [{"matched": False}]}, None, "excluded"),
])
def test_strip_status_reads_the_now_verdict(arm, recorded, rescored, status):
    assert view._strip_status(_summary_of(arm, recorded, rescored))[0] == status


def test_a_seeded_episode_with_nothing_present_never_reads_missed(tmp_path):
    # QUA-2931: the rescore found no seeded defect present (the corpus no longer seeds
    # one on this case): the strip says so with the existing excluded mark.
    status, what = view._strip_status(_summary_of(
        "seeded", {"bugs_present": ["b"], "bugs_found": []},
        {"bugs_present": [], "bugs_found": [], "reports": [{"matched": False}]}))
    assert status == "excluded" and status in viz.STATUS
    assert what.startswith("nothing present") and "missed" not in what
    runs = tmp_path / "runs"
    _episode(runs, "list-shows-items", "seeded", "ep-np", agent="codex-cli", condition="mcp",
             transcript=None, findings=None, brief=None,
             metrics={"completed": True, "false_reports": 0, "bugs_present": [],
                      "bugs_found": []})
    idx = view.build_view(runs, [RUN_ID], tasks_by_id={}).index.read_text()
    strip = re.search(r'<svg [^>]*aria-label="1 episodes? by status">.*?</svg>', idx, re.S)
    assert strip and "st-ex" in strip.group(0) and "nothing present" in strip.group(0)
    assert "missed" not in strip.group(0)


# ── view v2: stable keys, summaries, run state, --index-from (QUA-2840) ─────────

HELD_ID = "ep-0123456789ab"


def _keys(res: view.ViewResult) -> dict[tuple[str, str], str]:
    return {k: r["id"] for k, r in _by_case(_rows(res.index)).items()}


def _ep_files(res: view.ViewResult) -> dict[str, str]:
    """Every page and summary under ep/, by name → content."""
    return {p.name: p.read_text() for p in sorted((res.out_dir / "ep").glob("*.*"))}


def test_episode_key_prefers_the_episode_id_then_hashes_the_folder(tmp_path):
    base = dict(task_id="c~clean", task_version="v", task_type=journey.TASK_TYPE,
                agent="a", model="m", condition="raw", trial=1, passed=False, score=0.0,
                started_at="2026-09-25T00:00:00+00:00", ended_at="2026-09-25T00:01:00+00:00",
                wall_time_sec=1.0, exit_code=0, run_id=RUN_ID)
    ep = tmp_path / "runs" / "c" / "dir_ep-00000000000a"
    ep.mkdir(parents=True)
    (ep / "episode.json").write_text(json.dumps({"episode_id": "ep-00000000000a"}))
    r = RunResult(**base, artifact_dir="c/dir_ep-00000000000a",
                  provenance={"episode_id": "ep-ffffffffffff"})
    assert view.episode_key(r, ep) == "ep-00000000000a"          # the marker wins
    assert view.episode_key(r, None) == "ep-ffffffffffff"        # then provenance
    rel = RunResult(**base, artifact_dir="c~clean/ep-old")
    absolute = RunResult(**base, artifact_dir="/elsewhere/runs/c~clean/ep-old")
    key = view.episode_key(rel, None)
    assert re.fullmatch(r"h-[0-9a-f]{12}", key)
    assert view.episode_key(absolute, None) == key               # same folder, any machine
    assert view.episode_key(RunResult(**base, artifact_dir="c~clean/ep-other"), None) != key
    unsafe = RunResult(**base, artifact_dir="c/x", provenance={"episode_id": "../../etc"})
    assert view.episode_key(unsafe, None).startswith("h-")       # never a path
    assert view.episode_key(RunResult(**base), None).startswith("h-")


def test_keys_pages_and_summaries_survive_a_later_segment(runs):
    first = _build(runs)
    keys, files = _keys(first), _ep_files(first)
    assert keys[("hidden-case~clean", "mcp")] == HELD_ID
    assert all(re.fullmatch(r"h-[0-9a-f]{12}", k) for c, k in keys.items()
               if c != ("hidden-case~clean", "mcp"))
    for k in keys.values():
        assert f"{k}.html" in files and f"{k}.json" in files
    # A later segment adds an episode that sorts FIRST: positional ids would all shift.
    _episode(runs, "aaa-first", "clean", "ep-later-segment", agent="codex-cli",
             condition="raw", transcript=_codex_raw_transcript(), findings=FINDINGS_PASS,
             metrics={"completed": True, "false_reports": 0})
    _tasks_with_new = {**_tasks(), **{t.id: t for t in [_task("aaa-first", "clean")]}}
    second = view.build_view(runs, [RUN_ID], tasks_by_id=_tasks_with_new)
    assert second.episodes == 6
    keys2, files2 = _keys(second), _ep_files(second)
    assert {c: keys2[c] for c in keys} == keys
    for name, content in files.items():
        assert files2[name] == content, name                     # page + summary unchanged
    summary = json.loads(files[f"{HELD_ID}.json"])
    assert summary["format"] == view.SUMMARY_FORMAT and summary["key"] == HELD_ID
    assert summary["row"]["id"] == HELD_ID and summary["held"] is True
    assert summary["result"]["task_id"] == "hidden-case~clean"


def _split(runs: Path, dest: Path, keep: set[str]) -> Path:
    """A runs dir holding only the task folders in `keep` (one machine's part)."""
    import shutil
    for task in runs.iterdir():
        if task.name in keep:
            shutil.copytree(task, dest / task.name)
    return dest


def test_two_machines_views_merge_into_one_index(runs, tmp_path):
    import shutil
    full = _build(runs)
    tasks = {p.name for p in runs.iterdir() if not p.name.startswith("_")}
    part_a = {"list-shows-items~clean", "list-shows-items~seeded"}
    a = _build(_split(runs, tmp_path / "a", part_a))
    b = _build(_split(runs, tmp_path / "b", tasks - part_a))
    assert (a.episodes, b.episodes) == (2, 3)

    merged = tmp_path / "published"
    (merged / "ep").mkdir(parents=True)
    for part in (a, b):
        shutil.copytree(part.out_dir / "ep", merged / "ep", dirs_exist_ok=True)
    shutil.copyfile(b.out_dir / view.RUN_STATE, merged / view.RUN_STATE)

    res = view.build_index(merged)
    assert res.episodes == 5 and res.rescored == full.rescored
    assert res.not_rescored == full.not_rescored and res.images == full.images
    assert _keys(res) == _keys(full)
    for row in _rows(res.index):
        assert (merged / "ep" / f"{row['id']}.html").is_file()
    # One renderer: the merged index IS the single-machine index, byte for byte.
    assert res.index.read_text() == full.index.read_text()
    strip = lambda m: {k: v for k, v in m.items() if k != "generated_at"}  # noqa: E731
    assert strip(json.loads((merged / view.MANIFEST).read_text())) == strip(
        json.loads((full.out_dir / view.MANIFEST).read_text()))


def test_index_from_needs_summaries_and_rejects_a_bad_one(tmp_path, runs):
    with pytest.raises(view.ViewError, match="no episode summaries"):
        view.build_index(tmp_path / "empty")
    res = _build(runs)
    (res.out_dir / "ep" / "zz-broken.json").write_text("{not json")
    with pytest.raises(view.ViewError, match="zz-broken.json"):
        view.build_index(res.out_dir)


def test_index_from_without_run_json_derives_the_title(runs, tmp_path):
    import shutil
    res = _build(runs)
    bare = tmp_path / "bare"
    shutil.copytree(res.out_dir / "ep", bare / "ep")
    out = view.build_index(bare)
    m = json.loads((bare / view.MANIFEST).read_text())
    assert m["title"] == f"Run {RUN_ID}" and m["runs"][0]["state"]["complete"] is None
    assert out.episodes == 5


def _plan(runs: Path, units: list[tuple[str, int]], segment: int = 1) -> None:
    meta = runs / "_runs" / RUN_ID
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "plan.json").write_text(json.dumps({
        "run_id": RUN_ID, "mode": "journey", "segment": segment,
        "units": [{"app": "demoapp", "task": t, "kind": "journey", "trial": n}
                  for t, n in units]}))


DONE_UNITS = [(journey.task_id(c, v), 1) for c, v, *_ in EPISODES.values()]


def test_a_credit_stopped_run_shows_the_partial_badge_and_counts(runs):
    _plan(runs, DONE_UNITS + [("list-shows-items~clean", 2), ("hidden-case~clean", 2)])
    (runs / "_runs" / RUN_ID / "stop.json").write_text(json.dumps(
        {"run_id": RUN_ID, "reason": "seven_day_threshold"}))
    res = _build(runs)
    idx = res.index.read_text()
    assert "in progress · stopped: seven_day_threshold" in idx
    # The segment is bookkeeping (QUA-2941): in the collapsed expert block, not the line.
    assert "5/7 units done, 2 owed</p>" in idx
    expert = re.search(r'<details class="expert">(.*?)</details>', idx, re.DOTALL).group(1)
    assert "segment 1 (" in expert and "segment 1" not in idx.replace(expert, "")
    state = {"segment": 1, "units_planned": 7, "units_done": 5, "units_owed": 2,
             "stopped": "seven_day_threshold", "complete": False}
    assert json.loads((res.out_dir / view.MANIFEST).read_text())["runs"][0]["state"] == state
    assert json.loads((res.out_dir / view.RUN_STATE).read_text())["runs"][RUN_ID] == state
    # The index rebuilt from summaries carries the same badge.
    (res.out_dir / "index.html").unlink()
    view.build_index(res.out_dir)
    assert "in progress · stopped: seven_day_threshold" in res.index.read_text()


def test_a_finished_resume_is_complete_despite_the_old_stop_json(runs):
    _plan(runs, DONE_UNITS, segment=2)
    (runs / "_runs" / RUN_ID / "stop.json").write_text(json.dumps(
        {"run_id": RUN_ID, "reason": "five_hour_limit"}))
    res = _build(runs)
    state = json.loads((res.out_dir / view.MANIFEST).read_text())["runs"][0]["state"]
    assert state == {"segment": 2, "units_planned": 5, "units_done": 5, "units_owed": 0,
                     "stopped": None, "complete": True}
    idx = res.index.read_text()
    assert view.PARTIAL_BADGE + " ·" not in idx and "complete · 5/5 units done" in idx


def test_a_run_with_no_plan_is_complete_unless_stopped(runs):
    assert view.run_state(runs, RUN_ID)["complete"] is True
    (runs / "_runs" / RUN_ID).mkdir(parents=True)
    (runs / "_runs" / RUN_ID / "stop.json").write_text(json.dumps({"reason": "five_hour_limit"}))
    assert view.run_state(runs, RUN_ID) == {
        "segment": None, "units_planned": None, "units_done": None, "units_owed": None,
        "stopped": "five_hour_limit", "complete": False}


def test_cli_view_index_from(runs, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    res = _build(runs)
    (res.out_dir / "index.html").unlink()
    ok = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir)])
    assert ok.exit_code == 0, ok.output
    assert "Index rebuilt" in ok.output and "5 episode(s)" in ok.output
    assert res.index.is_file()
    both = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir),
                                         "--run", RUN_ID])
    assert both.exit_code != 0 and "takes no other view option" in both.output


class _StoppingEngine(_Engine):
    """Finishes one episode, then the credit guard stops the sweep."""

    async def __call__(self, plan, cfg):
        from qualgentbench import credit
        await super().__call__(plan, cfg)
        cfg.guard.decision = credit.StopDecision(
            reason=credit.REASON_SEVEN_DAY, message="weekly budget spent")
        return cfg.results


def test_run_writes_the_view_on_a_credit_stop_after_stop_json(fake_run, tmp_path,
                                                                monkeypatch):
    seen = []

    def record(runs_dir, run_ids, *a, **kw):
        stop = Path(runs_dir) / "_runs" / run_ids[0] / "stop.json"
        seen.append(json.loads(stop.read_text())["reason"] if stop.is_file() else None)
        return view.ViewResult(out_dir=tmp_path, index=tmp_path / "index.html")

    monkeypatch.setattr(view, "build_view", record)
    monkeypatch.setattr(lanes, "run_lanes", _StoppingEngine())
    out = fake_run()
    assert out.exit_code == 75, out.output
    assert seen == ["seven_day_threshold"]      # written, and after stop.json


# ── the credential gate on portable views (QUA-2841) ─────────────────────────────

SECRET = "sk-ant-api03-DO-NOT-LEAK-0123456789"
BEARER = "Authorization: Bearer DO-NOT-LEAK-token-9876"


def _leak_transcript(line: str) -> str:
    """The claude-code MCP transcript with one more tool result carrying `line`."""
    extra = [
        {"type": "assistant", "message": {"id": "m9", "content": [
            {"type": "tool_use", "id": "c9", "name": "Bash", "input": {"command": "env"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "c9", "content": f"PATH=/bin\n{line}\n"}]}},
    ]
    return _claude_mcp_transcript() + "\n" + "\n".join(json.dumps(x) for x in extra)


def _claude_dir(runs: Path) -> Path:
    c = EPISODES["claude"]
    return runs / journey.task_id(c[0], c[1]) / c[4]


def _everything(out: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", "replace")
                     for p in sorted(out.rglob("*")) if p.is_file())


@pytest.mark.parametrize("line,marker", [(f"ANTHROPIC_KEY={SECRET}", "sk-ant-"),
                                         (BEARER, "Bearer ")])
def test_a_transcript_with_a_credential_is_withheld(runs, line, marker):
    ep = _claude_dir(runs)
    (ep / "agent" / "transcript.txt").write_text(_leak_transcript(line))
    res = _build(runs, portable=True)
    key = _by_case(_rows(res.index))[("list-shows-items~clean", "mcp")]["id"]
    ep_out = res.out_dir / "ep" / key

    # Neither the transcript copy nor the page that embeds it was written as is.
    assert not (ep_out / "transcript.txt").exists()
    page = (res.out_dir / "ep" / f"{key}.html").read_text()
    assert "withheld" in page and "Transcript" not in page          # the stub
    shown = html_unescape(page)
    assert f"withheld: credential marker {marker} in ep/{key}/transcript.txt" in shown
    assert f"withheld: credential marker {marker} in ep/{key}.html" in shown
    # The rest of the episode still made it: result.json and the summary are clean.
    assert (ep_out / "result.json").is_file()
    summary = json.loads((res.out_dir / "ep" / f"{key}.json").read_text())
    assert summary["withheld"] == [{"file": f"ep/{key}/transcript.txt", "marker": marker},
                                   {"file": f"ep/{key}.html", "marker": marker}]

    # The manifest and the result list every hit, and the index flags the row.
    expect = [{"episode": key, "file": f"ep/{key}/transcript.txt", "marker": marker},
              {"episode": key, "file": f"ep/{key}.html", "marker": marker}]
    assert res.withheld == expect
    assert json.loads((res.out_dir / view.MANIFEST).read_text())["withheld"] == expect
    assert _by_case(_rows(res.index))[("list-shows-items~clean", "mcp")]["wh"] == 2
    assert "withheld file(s)" in res.index.read_text()

    # The matched text is nowhere in the folder, and whatever the gate wrote about the
    # hit re-scans clean: the folder passes the scanner it was gated by.
    assert "DO-NOT-LEAK" not in _everything(res.out_dir)
    assert view.scan_view(res.out_dir) == []
    # The other episodes are untouched.
    other = _by_case(_rows(res.index))[("list-after-save~clean", "raw")]
    assert "wh" not in other
    assert (res.out_dir / "ep" / other["id"] / "transcript.txt").is_file()


def html_unescape(text: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", text))


def test_a_credential_in_evidence_withholds_that_file_and_notices_the_page(runs):
    ep = _claude_dir(runs)
    _evidence(ep)
    (ep / "evidence" / "steps.jsonl").write_text('{"step": 1}\n{"headers": "' + BEARER + '"}\n')
    (ep / "evidence" / "step-1.json").write_text('{"step": 1, "action": "tap"}')
    res = _build(runs, portable=True)
    key = _by_case(_rows(res.index))[("list-shows-items~clean", "mcp")]["id"]
    ev = res.out_dir / "ep" / key / "evidence"
    assert not (ev / "steps.jsonl").exists()
    assert (ev / "step-1.json").is_file() and (ev / "index.html").is_file()
    assert (ev / "frames" / "00001.jpg").read_bytes() == JPG_C       # images: not scanned
    page = (res.out_dir / "ep" / f"{key}.html").read_text()
    assert "Transcript ·" in page                                     # the real page ...
    assert (f"withheld: credential marker Bearer  in ep/{key}/evidence/steps.jsonl"
            in html_unescape(page))                                   # ... with the notice
    assert f'href="{key}/evidence/index.html"' in page
    assert res.withheld == [{"episode": key, "file": f"ep/{key}/evidence/steps.jsonl",
                             "marker": "Bearer "}]


def test_a_withheld_summary_leaves_a_stub_that_survives_index_from(runs, tmp_path):
    import shutil
    ep = _claude_dir(runs)
    result = json.loads((ep / "result.json").read_text())
    result["metrics"]["reported_status"] = f"PASS ({SECRET})"
    (ep / "result.json").write_text(json.dumps(result))
    res = _build(runs, portable=True)
    key = next(h["episode"] for h in res.withheld)
    files = {h["file"] for h in res.withheld}
    assert {f"ep/{key}/result.json", f"ep/{key}.html", f"ep/{key}.json"} <= files
    stub = json.loads((res.out_dir / "ep" / f"{key}.json").read_text())
    assert stub["stub"] is True and stub["case"] == "list-shows-items~clean"
    assert "result" not in stub and "row" not in stub
    rows = _rows(res.index)
    assert key not in {r["id"] for r in rows} and len(rows) == 4      # left out of the rows
    assert json.loads((res.out_dir / view.MANIFEST).read_text())["episodes"] == 4
    assert "DO-NOT-LEAK" not in _everything(res.out_dir)

    # The publisher's path: ep/ (and run.json) copied into another folder, index rebuilt.
    merged = tmp_path / "published"
    shutil.copytree(res.out_dir / "ep", merged / "ep")
    shutil.copyfile(res.out_dir / view.RUN_STATE, merged / view.RUN_STATE)
    again = view.build_index(merged)
    key_order = lambda hs: sorted(hs, key=lambda h: (h["episode"] or "", h["file"]))
    assert key_order(again.withheld) == key_order(res.withheld)
    assert key_order(json.loads((merged / view.MANIFEST).read_text())["withheld"]) == \
        key_order(res.withheld)
    assert again.episodes == 4


def test_index_from_rescans_a_folder_built_before_the_gate(runs, tmp_path, monkeypatch):
    ep = _claude_dir(runs)
    (ep / "agent" / "transcript.txt").write_text(_leak_transcript(SECRET))
    monkeypatch.setattr(view, "scan_for_secrets", lambda data: None)   # no gate then
    old = _build(runs, portable=True)
    assert old.withheld == []
    monkeypatch.undo()
    res = view.build_index(old.out_dir)
    key = _by_case(_rows(old.index))[("list-shows-items~clean", "mcp")]["id"]
    assert {h["file"] for h in res.withheld} == {f"ep/{key}.html", f"ep/{key}/transcript.txt"}
    assert all(h["episode"] == key and h["marker"] == "sk-ant-" for h in res.withheld)


def test_a_clean_portable_view_is_unchanged_by_the_gate(runs, tmp_path, monkeypatch):
    import shutil
    _evidence(_claude_dir(runs))
    monkeypatch.setattr(view, "scan_for_secrets", lambda data: None)
    ungated = _build(runs, portable=True, out=runs / "_runs" / "ungated")
    monkeypatch.undo()
    gated = _build(runs, portable=True)
    assert gated.withheld == [] and ungated.withheld == []
    a, b = _snapshot(ungated.out_dir), _snapshot(gated.out_dir)
    a.pop(view.MANIFEST), b.pop(view.MANIFEST)                          # generated_at
    assert a == b
    m = json.loads((gated.out_dir / view.MANIFEST).read_text())
    assert m["withheld"] == []
    assert "withheld" not in _everything(gated.out_dir / "ep")
    shutil.rmtree(ungated.out_dir)


def test_a_local_view_is_not_gated(runs):
    (_claude_dir(runs) / "agent" / "transcript.txt").write_text(_leak_transcript(SECRET))
    res = _build(runs)
    assert res.withheld is None
    assert json.loads((res.out_dir / view.MANIFEST).read_text())["withheld"] is None


def test_cli_view_exits_distinctly_when_anything_is_withheld(runs, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id",
                        lambda *a, **k: _tasks())
    (_claude_dir(runs) / "agent" / "transcript.txt").write_text(_leak_transcript(SECRET))
    out = CliRunner().invoke(cli.main, ["view", "--run", RUN_ID, "--runs-dir", str(runs),
                                        "--portable"])
    assert out.exit_code == view.EXIT_WITHHELD == 65, out.output
    assert view.EXIT_WITHHELD not in (0, 1, 2, 75)   # finished, broke, usage, credit stop
    assert "Credential gate: 2 file(s) withheld" in out.output
    assert "credential marker 'sk-ant-' in ep/" in out.output
    assert "DO-NOT-LEAK" not in out.output
    again = CliRunner().invoke(cli.main, ["view", "--index-from",
                                          str(runs / "_runs" / RUN_ID / "view")])
    assert again.exit_code == view.EXIT_WITHHELD, again.output


# ── the gate's image exemption and mtimes (QUA-2847) ──────────────────────────────

LEAK = f"ANTHROPIC_KEY={SECRET}\n".encode()


def _image_transcript(media_type: str, data: bytes) -> str:
    """The claude-code MCP transcript with its first screenshot swapped for `data`,
    declared as `media_type`."""
    t = _claude_mcp_transcript().replace('"image/png"', f'"{media_type}"', 1)
    return t.replace(B64(PNG_A), B64(data), 1)


def _claude_key(res: view.ViewResult) -> str:
    return _by_case(_rows(res.index))[("list-shows-items~clean", "mcp")]["id"]


@pytest.mark.parametrize("media_type,name", [("image/svg+xml", "001.bin"),
                                             ("image/png", "001.png")])
def test_a_credential_in_a_transcript_image_is_withheld(runs, monkeypatch, media_type, name):
    """An unmapped media type lands as `.bin` and is always scanned; a mapped one whose
    bytes are not an image (text posing as a PNG) is scanned too."""
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    ep = _claude_dir(runs)
    (ep / "agent" / "transcript.txt").write_text(_image_transcript(media_type, LEAK))
    out = CliRunner().invoke(cli.main, ["view", "--run", RUN_ID, "--runs-dir", str(runs),
                                        "--portable"])
    assert out.exit_code == view.EXIT_WITHHELD, out.output
    view_dir = runs / "_runs" / RUN_ID / "view"
    manifest = json.loads((view_dir / view.MANIFEST).read_text())
    key = _by_case(_rows(view_dir / "index.html"))[("list-shows-items~clean", "mcp")]["id"]
    assert manifest["withheld"] == [{"episode": key, "file": f"ep/{key}/{name}",
                                     "marker": "sk-ant-"}]
    assert not (view_dir / "ep" / key / name).exists()
    assert "DO-NOT-LEAK" not in _everything(view_dir)
    page = (view_dir / "ep" / f"{key}.html").read_text()
    assert "withheld by the credential gate" in page and f'src="{key}/{name}"' not in page
    assert (f"withheld: credential marker sk-ant- in ep/{key}/{name}"
            in html_unescape(page))


def test_a_text_file_named_png_in_evidence_is_scanned(runs):
    ep = _claude_dir(runs)
    _evidence(ep)
    (ep / "evidence" / "shot.png").write_bytes(LEAK)
    res = _build(runs, portable=True)
    key = _claude_key(res)
    assert res.withheld == [{"episode": key, "file": f"ep/{key}/evidence/shot.png",
                             "marker": "sk-ant-"}]
    assert not (res.out_dir / "ep" / key / "evidence" / "shot.png").exists()
    assert (res.out_dir / "ep" / key / "evidence" / "frames" / "00001.jpg").is_file()
    assert "DO-NOT-LEAK" not in _everything(res.out_dir)
    # --index-from re-scans by the same rule: a text .png planted after the build is caught.
    (res.out_dir / "ep" / key / "evidence" / "late.png").write_bytes(LEAK)
    again = view.build_index(res.out_dir)
    assert {"episode": key, "file": f"ep/{key}/evidence/late.png",
            "marker": "sk-ant-"} in again.withheld


def test_real_images_are_still_not_scanned(runs, monkeypatch):
    _evidence(_claude_dir(runs))
    scanned: list[bytes] = []
    real = view.scan_for_secrets

    def spy(data):
        scanned.append(bytes(data))
        return real(data)

    monkeypatch.setattr(view, "scan_for_secrets", spy)
    res = _build(runs, portable=True)
    assert res.withheld == [] and res.images > 0
    assert not {PNG_A, PNG_B, JPG_C} & set(scanned)
    key = _claude_key(res)
    assert (res.out_dir / "ep" / key / "001.png").read_bytes() == PNG_A


def test_the_backstop_withholds_a_file_written_past_the_gate(runs, monkeypatch):
    """A copy path that skipped the gate is still caught by the end-of-build re-scan."""
    import shutil

    def ungated_copy(self, src, dst, episode):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    monkeypatch.setattr(view._Gate, "copy", ungated_copy)
    ep = _claude_dir(runs)
    _evidence(ep)
    (ep / "evidence" / "env.txt").write_bytes(LEAK)
    res = _build(runs, portable=True)
    key = _claude_key(res)
    assert res.withheld == [{"episode": key, "file": f"ep/{key}/evidence/env.txt",
                             "marker": "sk-ant-"}]
    assert not (res.out_dir / "ep" / key / "evidence" / "env.txt").exists()
    assert json.loads((res.out_dir / view.MANIFEST).read_text())["withheld"] == res.withheld


def test_a_rebuilt_portable_view_keeps_the_mtimes_of_its_copies(runs):
    """`aws s3 sync` re-uploads a file whose local mtime is newer than the object: an
    unchanged episode's images and copies must keep their source's time."""
    import os
    ep = _claude_dir(runs)
    _evidence(ep)
    old = 1_600_000_000
    for p in ep.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    first = _build(runs, portable=True)
    key = _claude_key(first)
    dest = first.out_dir / "ep" / key
    copies = ["transcript.txt", "result.json", "evidence/index.html",
              "evidence/frames/00001.jpg", "001.png"]
    assert {c: (dest / c).stat().st_mtime for c in copies} == dict.fromkeys(copies, old)
    before = _snapshot(first.out_dir)
    again = _build(runs, portable=True)
    assert {c: (dest / c).stat().st_mtime for c in copies} == dict.fromkeys(copies, old)
    after = _snapshot(again.out_dir)
    before.pop(view.MANIFEST), after.pop(view.MANIFEST)                 # generated_at
    assert before == after


# ── the create board as data (QUA-2922) ────────────────────────────────────────

def test_a_create_run_view_writes_create_json_lists_and_links_it(tmp_path):
    """`create.json` beside `create.html`: `board.build_board`'s dict (what `show --mode
    create --json` prints for the same runs), listed in run.json `pages`, linked from the
    index, kept by `--index-from`, and naming no local path (a portable view is
    published). A journey run view keeps its run.json as it was: no pages."""
    from test_create_ab import STUDY, SimAuthor, SimRunner, _drive, _spec

    from qualgentbench.create import board
    runs = tmp_path / "runs"
    _drive(runs, _spec(), SimAuthor(runs, {"A": "honest", "B": "vacuous"}), SimRunner(runs))
    run_id = json.loads(next((runs / "_runs" / "_create" / "ab").glob("*.json"))
                        .read_text())["run_id"]
    ep = runs / STUDY / "ep-grade"
    ep.mkdir(parents=True)
    (ep / "result.json").write_text(json.dumps({
        "task_id": f"{STUDY}-gx~clean", "task_version": "v", "task_type": "create_grade",
        "agent": "codex-cli", "model": "gpt-6-astra", "condition": "mcp", "trial": 1,
        "passed": True, "score": 1.0, "started_at": "2026-10-01T00:00:00+00:00",
        "ended_at": "2026-10-01T00:01:00+00:00", "wall_time_sec": 60.0, "exit_code": 0,
        "artifact_dir": str(ep.relative_to(runs)), "run_id": run_id, "metrics": {},
        "provenance": {}}))
    res = view.build_view(runs, [run_id], tmp_path / "out", rescore=False, portable=True,
                          allow_outside_runs=True)
    assert res.create_board_json == res.out_dir / "create.json"
    doc = json.loads(res.create_board_json.read_text())
    expected = json.loads(json.dumps(board.board_for(
        runs, run_ids=[run_id], include_smoke=True,
        title=f"Run {run_id} — CreateBench board"), default=str))
    assert "<runs>/_runs/_create/gate.json" in doc["gate"]["detail"]   # runs-relative
    assert doc == expected and doc["schema"] == board.BOARD_SCHEMA and doc["rows"]
    for text in (res.create_board_json.read_text(), res.create_board.read_text()):
        for local in (str(runs), str(runs.resolve()), str(tmp_path), "pytest-of-"):
            assert local not in text
    pages = json.loads((res.out_dir / view.RUN_STATE).read_text())["pages"]
    assert pages == {"create_board": "create.html", "create_board_json": "create.json"}
    idx = res.index.read_text()
    assert 'href="create.json"' in idx and 'href="create.html"' in idx
    assert view.build_index(res.out_dir).index.read_text() == idx


def test_a_journey_run_view_has_no_create_pages(runs):
    res = _build(runs)
    assert res.create_board is None and res.create_board_json is None
    assert not (res.out_dir / "create.json").exists()
    assert "pages" not in json.loads((res.out_dir / view.RUN_STATE).read_text())
    assert "create.json" not in res.index.read_text()


# ── the scorer stamp and the in-place rescore trace (QUA-2927) ──────────────────

_TRACE = {"rescored_from": {"completed": True, "overall": None, "bugs_found": [],
                            "bugs_present": [], "false_reports": 0, "false_positives": None,
                            "contaminated": True, "contamination_reasons": ["x"],
                            "scorer_version": None},
          "rescored_with": {"scorer_version": 1, "corpus_version": CORPUS_A,
                            "heldout_version": None},
          "rescored_at": "2026-10-01T00:00:00+00:00"}


def _traced(runs: Path) -> Path:
    """The claude episode as an in-place rescore left it: the trace in result.json and
    the scorer in its metrics. Every other episode is a run-time verdict, unstamped."""
    p = runs / journey.task_id(*EPISODES["claude"][:2]) / EPISODES["claude"][4] / "result.json"
    doc = json.loads(p.read_text())
    doc["metrics"]["scorer_version"] = 1
    p.write_text(json.dumps({**doc, **_TRACE}, indent=2))
    return p


def test_the_view_shows_the_scorer_and_the_recorded_rescore(runs, tmp_path, monkeypatch):
    _traced(runs)
    explicit = _build(runs)
    line = _versions_line(explicit.index.read_text())
    assert "scorer v1, 4 unstamped" in line and "rescored v" not in line
    v = _run_entry(explicit)["versions"]
    assert v["scorer_versions"] == ["1"] and v["scorer_unstamped"] == 4
    cases = {c["key"]: c for c in _run_entry(explicit)["cases"]}
    traced = [c for c in cases.values() if c["recorded_is_rescore"]]
    assert [c["agent"] for c in traced] == ["claude-code"] and len(cases) == 5

    summary = json.loads((explicit.out_dir / "ep" / f"{traced[0]['key']}.json").read_text())
    assert {k: summary["result"][k] for k in _TRACE} == _TRACE       # recorded side keeps it
    assert not set(_TRACE) & set(summary["rescored_result"])          # a dry run has none
    assert summary["rescored_result"]["metrics"]["scorer_version"] == journey.SCORER_VERSION

    page = html_unescape((explicit.out_dir / "ep" / f"{traced[0]['key']}.html").read_text())
    assert f"recorded v1 · rescored v{journey.SCORER_VERSION}" in page
    assert (f"recorded verdict is an in-place rescore · scorer v1 · corpus {CORPUS_A} · "
            f"at 2026-10-01T00:00:00+00:00") in page
    other = next(k for k, c in cases.items() if not c["recorded_is_rescore"]
                 and c["rescored"])
    page = html_unescape((explicit.out_dir / "ep" / f"{other}.html").read_text())
    assert f"recorded unstamped · rescored v{journey.SCORER_VERSION}" in page
    assert "— (recorded verdict written at run time)" in page

    # The default corpus stamps the rescore's scorer too.
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    default = view.build_view(runs, [RUN_ID], tmp_path / "d", allow_outside_runs=True)
    assert (f"scorer v1, 4 unstamped, rescored v{journey.SCORER_VERSION}"
            in _versions_line(default.index.read_text()))


def test_a_run_with_no_scorer_stamp_shows_no_scorer_chip(runs):
    assert "scorer" not in _versions_line(_build(runs).index.read_text())


def test_index_from_reproduces_a_traced_run_byte_for_byte(runs, tmp_path, monkeypatch):
    import shutil
    _traced(runs)
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    full = view.build_view(runs, [RUN_ID], tmp_path / "full", allow_outside_runs=True)
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    view.build_index(copy)
    assert (copy / "index.html").read_bytes() == full.index.read_bytes()
    a, b = ((d / view.MANIFEST).read_text().splitlines() for d in (copy, full.out_dir))
    assert [x for x in a if '"generated_at"' not in x] == [
        x for x in b if '"generated_at"' not in x]
    rw = json.loads((copy / view.MANIFEST).read_text())["runs"][0]["rescored_with"]
    assert rw["scorer"] == str(journey.SCORER_VERSION) and rw["stamped"] == 4


def test_the_scorer_label_renders_beside_the_agent_cli_and_harness_lanes(tmp_path, monkeypatch):
    # QUA-2927 + QUA-2928 on one versions line: every label renders, and an
    # `--index-from` rebuild reproduces the page and manifest.
    import shutil
    runs = _stamped(tmp_path / "runs")
    harness = {"package_version": "0.2.0", "git_sha": "c" * 40, "git_dirty": False}
    for result_json in sorted(runs.rglob("result.json")):
        doc = json.loads(result_json.read_text())
        doc["metrics"]["scorer_version"] = 1
        doc["provenance"].update({"agent_cli_version": "0.157.0", "harness": harness})
        result_json.write_text(json.dumps(doc))
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    full = view.build_view(runs, [RUN_ID], tmp_path / "full", allow_outside_runs=True)
    line = _versions_line(full.index.read_text())
    # The scorer is a lane, so it follows the set with the others (QUA-2934).
    labels = ["set ", f"scorer v1, rescored v{journey.SCORER_VERSION}",
              "agent CLI codex-cli 0.157.0", f"harness 0.2.0+{'c' * 12}"]
    at = [line.find(x) for x in labels]
    assert -1 not in at and at == sorted(at), line
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    view.build_index(copy)
    assert (copy / "index.html").read_bytes() == full.index.read_bytes()
    a, b = ((d / view.MANIFEST).read_text().splitlines() for d in (copy, full.out_dir))
    assert [x for x in a if '"generated_at"' not in x] == [
        x for x in b if '"generated_at"' not in x]


def test_the_recorded_scorer_is_a_lane_in_numeric_order(tmp_path, monkeypatch):
    """QUA-2934: `versions.scorer_versions` / `scorer_unstamped` name the scorer the
    RECORDED verdicts carry, sorted numerically (v10 after v9), never `mixed` and never in
    `set_key`; `--index-from` reproduces them."""
    import shutil
    runs = _stamped(tmp_path / "runs")
    plain = _run_entry(_build(runs, out=runs / "_runs" / "_plain"))
    stamps = iter([10, 9])                     # the third (held-out) episode stays unstamped
    for result_json in sorted(runs.rglob("result.json")):
        doc = json.loads(result_json.read_text())
        if doc["metrics"].get("heldout"):
            continue
        doc["metrics"]["scorer_version"] = next(stamps)
        result_json.write_text(json.dumps(doc))
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    full = view.build_view(runs, [RUN_ID], tmp_path / "full", allow_outside_runs=True)
    run = _run_entry(full)
    v = run["versions"]
    assert v["scorer_versions"] == ["9", "10"] and v["scorer_unstamped"] == 1
    assert v["mixed"] is False
    assert v["set_key"] == run["set_key"] == plain["set_key"] is not None
    line = _versions_line(full.index.read_text())
    assert "scorer v9, v10, 1 unstamped" in line
    assert line.find("set ") < line.find("scorer v9")
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    view.build_index(copy)
    assert (copy / "index.html").read_bytes() == full.index.read_bytes()
    a, b = ((d / view.MANIFEST).read_text().splitlines() for d in (copy, full.out_dir))
    assert [x for x in a if '"generated_at"' not in x] == [
        x for x in b if '"generated_at"' not in x]


def test_scorer_version_lists_sort_numerically():
    ms = [{"scorer_version": x} for x in (10, 9, 2, None, "1")]
    assert corpus.distinct_versions(ms, "scorer_version", corpus.numeric_order) == (
        None, ["1", "2", "9", "10"], 1)
    assert corpus.distinct_versions(ms, "scorer_version")[1] == ["1", "10", "2", "9"]
    import importlib.util
    from types import SimpleNamespace
    path = Path(__file__).parents[1] / "scripts" / "rescore_journey.py"
    spec = importlib.util.spec_from_file_location("rescore_journey_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    rs = [SimpleNamespace(task_type=journey.TASK_TYPE, metrics={"scorer_version": x})
          for x in (10, 9)]
    assert script.scorer_line(rs).endswith("recorded v9, v10")


# ── plain language (QUA-2938) ──────────────────────────────────────────────────

def _help_links(page: str) -> list[tuple[str, str]]:
    """`(term, href)` per defined term, in page order: each `data-term` element and the
    "?" link that follows it (inside it, or right after it)."""
    terms = re.findall(r'data-term="([^"]+)"', page)
    hrefs = re.findall(r'<a class="help" href="([^"]+)"', page)
    assert len(terms) == len(hrefs), (len(terms), len(hrefs))
    return [(html_unescape(t), html_unescape(h)) for t, h in zip(terms, hrefs)]


def test_the_run_page_explains_itself_in_plain_words(runs):
    idx = _build(runs).index.read_text()
    box = re.search(r'<details class="howto" open><summary>How to read this page</summary>'
                    r"(.*?)</details>", idx, re.S)
    assert box and idx.index('class="howto"') < idx.index("<table class=board>")
    items = re.findall(r"<li[^>]*>(.*?)</li>", box.group(1), re.S)
    assert 6 <= len(items) <= 8 and [t for t, _ in view.HOW_TO_READ][:4] == [
        "episode", "seeded case", "catch", "false alarm"]
    for jargon in ("Wilson", "Newcombe", " pp", " n=", "basis", "set key"):
        assert jargon not in box.group(1)
    # Every board header carries its plain definition; the expert legend stays.
    heads = re.findall(r"<th([^>]*)>", re.search(r"<table class=board><tr>(.*?)</tr>", idx,
                                                re.S).group(1))
    assert len(heads) == len(view._BOARD_HEADS)
    for attrs, (_, term) in zip(heads, view._BOARD_HEADS):
        assert f'data-term="{html.escape(term)}"' in attrs
        assert f'title="{html.escape(view.PLAIN[term])}"' in attrs
    assert view.BOARD_RATES_LEGEND in html_unescape(idx)
    # Chart captions, version chips, the rescore sentence, strip statuses, the badge.
    caps = re.findall(r'<figcaption class="dim"([^>]*)>', idx)
    assert caps and all("data-term=" in c and "title=" in c for c in caps)
    chips = re.search(r'<p class="versions">(.*?)</p>', idx, re.S).group(1)
    for term in ("benchmark", "corpus", "held-out split", "brief version", "setup",
                 "device tools", "set"):
        assert f'data-term="{html.escape(term)}"' in chips, term
    assert 'data-term="rescore"' in idx
    shown = [s for s, t in viz.STATUS_PLAIN.items() if f'title="{html.escape(t)}"' in idx]
    assert shown and all(f"<title>{html.escape(viz.STATUS_PLAIN[s])}</title>" in idx
                         for s in shown)
    assert view.HELDOUT_BADGE_HTML in idx and 'title="' in view.HELDOUT_BADGE_HTML
    assert f"<title>{html.escape(view.PLAIN['rate axis'])}</title>" in idx
    # Off by default: no "?" link, nothing recorded, no URL.
    assert 'class="help"' not in idx
    assert "help_base" not in json.loads((_build(runs).out_dir / view.RUN_STATE).read_text())
    assert "http://" not in idx and "https://" not in idx


def test_every_term_anchor_is_on_the_fixed_list_and_every_status_is_explained():
    from qualgentbench import glossary
    assert {a for _, a in glossary.TERMS.values()} <= set(glossary.ANCHORS)
    assert set(viz.STATUS_PLAIN) == set(viz.STATUS)
    assert all(t in glossary.TERMS for t, _ in view.HOW_TO_READ)
    assert all(t in glossary.TERMS for _, t in view._BOARD_HEADS)


def test_a_help_base_links_every_defined_term_and_index_from_keeps_it(runs, tmp_path,
                                                                     monkeypatch):
    import shutil

    from qualgentbench import glossary
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    base = "../docs/glossary.html"
    full = view.build_view(runs, [RUN_ID], tmp_path / "full", allow_outside_runs=True,
                           portable=True, help_base=base)
    idx = full.index.read_text()
    links = _help_links(idx)
    assert len(links) >= len(view._BOARD_HEADS) + len(view.HOW_TO_READ)
    for term, href in links:
        assert href == f"{base}#{glossary.TERMS[term][1]}", (term, href)
    assert {t for t, _ in links} >= {"catch", "false alarm", "integrity", "completion",
                                     "cost", "episodes", "held-out", "range", "rescore"}
    assert "http" not in idx
    assert json.loads((full.out_dir / view.RUN_STATE).read_text())["help_base"] == base
    # `--index-from` reads the recorded base: identical bytes.
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    view.build_index(copy)
    assert (copy / "index.html").read_bytes() == full.index.read_bytes()
    # Passed again, it replaces the recorded one for that rebuild; "" turns links off.
    view.build_index(copy, help_base="")
    off = (copy / "index.html").read_text()
    assert 'class="help"' not in off and off.count("data-term=") == idx.count("data-term=")
    view.build_index(copy, help_base="g.html")
    assert all(h.startswith("g.html#") for _, h in _help_links((copy / "index.html")
                                                             .read_text()))
    # A URL is refused: the pages never carry one.
    for bad in ("https://example.invalid/g.html", "//host/g.html", "g.html#x"):
        with pytest.raises(view.ViewError, match="documentation page"):
            view.build_index(copy, help_base=bad)


def test_cli_view_help_base_flag_and_env(runs, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    res = _build(runs)
    ok = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir),
                                       "--help-base", "help.html"])
    assert ok.exit_code == 0, ok.output
    assert '<a class="help" href="help.html#catch"' in res.index.read_text()
    env = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir)],
                             env={"QGB_VIEW_HELP_BASE": "env.html"})
    assert env.exit_code == 0, env.output
    assert '<a class="help" href="env.html#catch"' in res.index.read_text()
    bad = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir),
                                        "--help-base", "https://example.invalid/x"])
    assert bad.exit_code != 0 and "documentation page" in bad.output


# ── executive read-through fixes (QUA-2941) ────────────────────────────────────

def _expert(page: str) -> re.Match:
    m = re.search(r'<details class="expert"><summary>(.*?)</summary>(.*?)</details>', page,
                  re.DOTALL)
    assert m, "no expert block"
    return m


def test_the_agents_verdict_is_explained_apart_from_catch(runs):
    """A seeded episode can be rightly answered PASS while its report catches the planted
    bug (docs/scoring.md: a planted bug fails the test only when it blocks the steps):
    the column says whose answer it is, and the page explains how it relates to catch."""
    idx = _build(runs).index.read_text()
    tip = html.escape(view.PLAIN["agent verdict"])
    assert (f'<th data-term="agent verdict" title="{tip}">agent&#x27;s verdict' in idx
            or f"<th data-term=\"agent verdict\" title=\"{tip}\">agent's verdict" in idx)
    assert "<th>reported</th>" not in idx and "<label>reported " not in idx
    assert "<label>agent's verdict <select id=\"f-status\">" in idx
    assert "catch" in view.PLAIN["agent verdict"] and "blocks" in view.PLAIN["agent verdict"]
    box = re.search(r'<details class="howto" open>(.*?)</details>', idx, re.DOTALL).group(1)
    assert 'data-term="agent verdict"' in box and "rightly answer pass" in box


def test_the_how_to_box_says_episode_and_never_reads_overlap_as_a_tie(runs):
    box = dict(view.HOW_TO_READ)
    text = " ".join(box.values())
    assert "clean episodes" in box["false alarm"] and "clean runs" not in text
    assert "one run of it" not in text and "one attempt" in box["episode"]
    assert "shown separately" in box["held-out"] and "too few to rank" in box["held-out"]
    assert "own block" not in text and "own block" not in view.PLAIN["held-out"]
    assert "overlap" not in box["range"] and "leaves out zero" in box["range"]
    for term in ("episode", "false alarm", "completion"):
        assert " runs " not in f" {view.PLAIN[term]} ", term


def test_jargon_lives_in_a_collapsed_expert_block_after_the_plain_box(runs):
    _plan(runs, DONE_UNITS, segment=0)
    idx = _build(runs).index.read_text()
    m = _expert(idx)
    expert = m.group(0)
    # Closed by default, after the box and the board; the box comes first of all three.
    assert '<details class="expert" open' not in idx
    assert idx.index('class="howto"') < idx.index("<table class=board>") < idx.index(expert)
    for jargon in ("rescore_journey.py", "show --run", "segment 0", "Wilson interval;",
                   "denominator"):
        assert jargon in expert, jargon
    top = idx[:idx.index(expert)]                     # the box, the state and the boards
    for jargon in ("rescore_journey.py", "show --run", "segment", "denominator",
                   view.BOARD_RATES_LEGEND, journey.RANKING_NOTE):
        assert jargon not in html_unescape(top), jargon
    assert view.EXPERT_SUMMARY in m.group(1)
    # Still one "?" link per defined term once a help base is set.
    linked = view.build_index(_build(runs).out_dir, help_base="g.html").index.read_text()
    _help_links(linked)


def test_rate_chart_callouts_say_what_the_number_is(runs):
    idx = _build(runs).index.read_text()
    r2 = re.search(r'<figure class="fig">(<svg.*?</svg>)<figcaption[^>]*>(.*?)</figcaption>',
                   idx, re.DOTALL)
    vals = re.findall(r'<text[^>]*class="val"[^>]*>([^<]*)</text>', r2.group(1))
    assert vals and all(v.startswith("highest: ") and v.endswith("%") for v in vals), vals
    assert "highest rate" in r2.group(2)


def test_a_home_base_links_back_and_index_from_keeps_it(runs, tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    # Off by default: no links, nothing recorded.
    plain = _build(runs)
    assert '<p class="nav">' not in plain.index.read_text()
    assert "home_base" not in json.loads((plain.out_dir / view.RUN_STATE).read_text())
    full = view.build_view(runs, [RUN_ID], tmp_path / "full", allow_outside_runs=True,
                           portable=True, help_base="../about/", home_base="../../")
    idx = full.index.read_text()
    nav = ('<p class="nav"><a href="../../">← all runs</a> · '
           '<a href="../about/">About</a></p>')
    assert nav in idx and idx.index(nav) < idx.index("<h1>")
    _help_links(idx)                                  # "About" is not a term link
    assert json.loads((full.out_dir / view.RUN_STATE).read_text())["home_base"] == "../../"
    copy = tmp_path / "copy"
    (copy / "ep").mkdir(parents=True)
    for p in (full.out_dir / "ep").glob("*.json"):
        shutil.copyfile(p, copy / "ep" / p.name)
    shutil.copyfile(full.out_dir / view.RUN_STATE, copy / view.RUN_STATE)
    assert view.build_index(copy).index.read_bytes() == full.index.read_bytes()
    # Replaced for one rebuild; "" turns it off; no help base: no "About".
    assert '<p class="nav">' not in view.build_index(copy, home_base="").index.read_text()
    alone = view.build_index(copy, help_base="", home_base="/runs/").index.read_text()
    assert '<p class="nav"><a href="/runs/">← all runs</a></p>' in alone
    for bad in ("https://example.invalid/", "//host/", "../#x"):
        with pytest.raises(view.ViewError, match="home-link base"):
            view.build_index(copy, home_base=bad)
    assert "http" not in idx


def test_cli_view_home_base_flag_and_env(runs, monkeypatch):
    monkeypatch.setattr("qualgentbench.rescore.journey_tasks_by_id", lambda *a, **k: _tasks())
    res = _build(runs)
    ok = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir),
                                       "--home-base", "../"])
    assert ok.exit_code == 0, ok.output
    assert '<a href="../">← all runs</a>' in res.index.read_text()
    env = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir)],
                             env={"QGB_VIEW_HOME_BASE": "../../"})
    assert env.exit_code == 0, env.output
    assert '<a href="../../">← all runs</a>' in res.index.read_text()
    bad = CliRunner().invoke(cli.main, ["view", "--index-from", str(res.out_dir),
                                        "--home-base", "https://example.invalid/"])
    assert bad.exit_code != 0 and "home-link base" in bad.output
