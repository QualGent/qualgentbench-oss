"""Contamination tripwire — episodes that read benchmark internals are voided.

Every case below is taken from a real transcript.
"""

from __future__ import annotations

import json

import pytest

from qualgentbench.contamination import CANARY, scan
from qualgentbench.transcript import TranscriptParser

REPO = "/repo/QualGentBench"
WS = f"{REPO}/runs/explore-catima/2026-01-01T00-00-00Z_x/workspace"
HOME = "/repo"


def _tx(*calls: tuple[str, dict, str]) -> str:
    """A claude-code stream-json transcript of (tool, input, result) triples."""
    lines = []
    for i, (name, inp, result) in enumerate(calls):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]},
        }))
        lines.append(json.dumps({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": result}]},
        }))
    return "\n".join(lines)


def _scan(transcript: str):
    return scan(TranscriptParser(transcript), WS, repo_root=REPO, home=HOME)


# ── hard: the episode is void ────────────────────────────────────────────────

def test_reading_the_spec_yaml_is_contamination():
    r = _scan(_tx(("Read", {"file_path": f"{REPO}/src/qualgentbench/data/benchmarks/catima.yaml"}, "bugs:")))
    assert r.contaminated
    assert r.reasons == ["benchmark_repo"]


def test_finding_the_spec_by_shell_is_contamination():
    """Path-bearing shell commands must be scanned too, not just `file_path`."""
    cmd = f"find {REPO}/src/qualgentbench/data/benchmarks -name '*catima*'"
    assert _scan(_tx(("Bash", {"command": cmd}, "catima.yaml"))).contaminated


def test_grepping_a_bug_id_is_contamination():
    cmd = f'grep -n -A 40 "alarm-toggle-not-persisted" {REPO}/src/qualgentbench/data/benchmarks/fossify-clock.yaml'
    assert _scan(_tx(("Bash", {"command": cmd}, "state: broken"))).contaminated


def test_sibling_app_source_checkout_is_contamination():
    """A benchmark cannot rely on the agent failing to find seeds in a local checkout."""
    r = _scan(_tx(("Bash", {"command": "grep -rn QgbFlags /repo/Fossify-Notes/app/src"}, "")))
    assert r.contaminated
    assert r.reasons == ["app_source_checkout"]


def test_canary_in_a_tool_result_is_contamination():
    """Catches reads by routes the path scanner does not model — copies, symlinks,
    env-built paths."""
    r = _scan(_tx(("Bash", {"command": "cat $F"}, f"# {CANARY}\nbugs:")))
    assert r.contaminated
    assert r.reasons == ["canary"]


def test_canary_in_the_INPUT_alone_is_not_contamination():
    """Only a RESULT carrying the token proves the agent actually received the file."""
    assert not _scan(_tx(("Bash", {"command": f"grep -r {CANARY} ."}, "no matches"))).contaminated


# ── soft: recorded, never voiding ────────────────────────────────────────────

def test_session_log_access_is_soft():
    """Self-inspection reveals nothing the agent had not already done."""
    r = _scan(_tx(("Bash", {"command": "grep -o 'Tapped' /repo/.claude/projects/x.jsonl"}, "")))
    assert not r.contaminated
    assert [s["kind"] for s in r.soft] == ["session_log"]


def test_own_run_directory_is_not_contamination():
    """The run dir holds the agent's own hooks and evidence, never an answer."""
    r = _scan(_tx(("Bash", {"command": f"ls {WS}/.."}, "")))
    assert not r.contaminated and not r.soft


# ── the ordinary episode must stay clean ─────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "adb -s emulator-5554 shell input tap 540 1200",
    "adb -s emulator-5554 shell uiautomator dump /sdcard/window_dump.xml",
    "adb -s emulator-5554 pull /sdcard/window_dump.xml /tmp/window_dump.xml",
    "adb -s emulator-5554 exec-out screencap -p > /tmp/screen.png",
    "adb -s emulator-5554 shell cat //storage/emulated/0/Documents/markor/note.md",
    "adb -s emulator-5554 shell am start -n org.fossify.clock/.MainActivity",
    "python3 /tmp/test_calc.py",
    "/usr/local/bin/adb devices",
])
def test_ordinary_device_work_is_clean(cmd):
    """Device-side paths, scratch space and the toolchain are not host reach."""
    r = _scan(_tx(("Bash", {"command": cmd}, "ok")))
    assert not r.contaminated, r.hard
    assert not r.soft, r.soft


@pytest.mark.parametrize("token", ["/btn_formula", "/hierarchy", "/100", "/item_result"])
def test_path_shaped_ui_fragments_are_not_paths(token):
    """Resource ids and XPath fragments are path-shaped, not filesystem reach."""
    r = _scan(_tx(("Bash", {"command": f"echo {token}"}, "")))
    assert not r.contaminated and not r.soft


def test_reads_are_scanned_not_just_shell():
    r = _scan(_tx(("Read", {"file_path": "/tmp/window_dump.xml"}, "<hierarchy/>")))
    assert not r.contaminated and not r.soft


# ── the record a human reviews ───────────────────────────────────────────────

def test_metrics_are_flat_and_capped():
    calls = [("Read", {"file_path": f"{REPO}/src/qualgentbench/data/benchmarks/a{i}.yaml"}, "x")
             for i in range(30)]
    m = _scan(_tx(*calls)).as_metrics()
    assert m["contaminated"] is True
    assert m["contamination_reasons"] == ["benchmark_repo"]
    assert len(m["contamination_hits"]) == 20        # capped for reviewability
    assert json.dumps(m)                             # must survive result.json


def test_every_shipped_spec_carries_the_canary():
    from qualgentbench.bugs import _BENCHMARKS_DIR
    specs = sorted(_BENCHMARKS_DIR.glob("*.yaml"))
    assert specs, "no specs found"
    missing = [p.name for p in specs if CANARY not in p.read_text()]
    assert not missing, f"specs without a canary: {missing}"


def test_a_relative_workspace_does_not_void_a_clean_episode(monkeypatch, tmp_path):
    """Transcript paths are absolute; a relative workspace would never match the
    run-dir exemption and every cwd reference would void a clean episode."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs" / "ep" / "workspace").mkdir(parents=True)
    tx = _tx(("Bash", {"command": f"ls {tmp_path}/runs/ep/workspace"}, ""))
    r = scan(TranscriptParser(tx), "runs/ep/workspace", repo_root=str(tmp_path),
             home=str(tmp_path))
    assert not r.contaminated, r.hard


def test_container_layout_does_not_void_scratch_work():
    """In the image the repo is /app: its parent is `/`, and an unguarded sibling
    rule would classify EVERY absolute path — /tmp scratch files, even the date
    string /2/5/1990 — as app_source_checkout (first containerized run, 2026-08-24)."""
    tx = _tx(("Bash", {"command": "adb pull /sdcard/ui.xml /tmp/ui.xml"}, ""),
             ("Write", {"file_path": "/tmp/uihelper.py"}, "ok"),
             ("Bash", {"command": "echo born /2/5/1990"}, ""))
    r = scan(TranscriptParser(tx), "/app/runs/explore-birday/ep/workspace",
             repo_root="/app", home="/root")
    assert not r.contaminated, r.hard


def test_container_layout_still_trips_on_the_repo_itself():
    tx = _tx(("Read", {"file_path": "/app/src/qualgentbench/data/benchmarks/birday.yaml"}, "bugs:"))
    r = scan(TranscriptParser(tx), "/app/runs/explore-birday/ep/workspace",
             repo_root="/app", home="/root")
    assert r.contaminated
    assert r.reasons == ["benchmark_repo"]
