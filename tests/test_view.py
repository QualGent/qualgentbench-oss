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
* a view that fails does not fail `run`.

App and case names are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from qualgentbench import bugs, cli, journey, lanes, session, view
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
             blinded: bool = False) -> Path:
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
        (ep / "episode.json").write_text(json.dumps({"task_id": case, "blinded": True}))
    rel = str(ep) if absolute else str(ep.relative_to(runs))
    result = {"task_id": journey.task_id(case, version), "task_version": "v",
              "task_type": journey.TASK_TYPE, "agent": agent, "model": "test-model",
              "condition": condition, "trial": 1, "passed": False, "score": 0.0,
              "started_at": "2026-09-25T00:00:00+00:00",
              "ended_at": "2026-09-25T00:01:00+00:00", "wall_time_sec": 60.0,
              "exit_code": 0, "artifact_dir": rel, "run_id": RUN_ID,
              "metrics": {"version": version, "app_id": "demoapp", "case_id": case,
                          "steps": 3, "step_budget": 30, "cost_usd": 0.12,
                          "reported_status": "PASS", **metrics},
              "provenance": {}}
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
    run_id = (tmp_path / "run_id").read_text().strip()
    assert calls == [(tmp_path / "runs", [run_id])]
    assert (tmp_path / "runs" / "_runs" / run_id / "board.json").is_file()


def test_a_failing_view_does_not_fail_run(fake_run, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("renderer exploded")
    monkeypatch.setattr(view, "build_view", boom)
    out = fake_run()
    assert out.exit_code == 0, out.output
    assert "Episode view not written" in out.output and "renderer exploded" in out.output
    run_id = (tmp_path / "run_id").read_text().strip()
    assert (tmp_path / "runs" / "_runs" / run_id / "board.json").is_file()
    assert f"qualgent-bench view --run {run_id}" in out.output
