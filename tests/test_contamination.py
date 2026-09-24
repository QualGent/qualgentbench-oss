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


# ── QUA-2804: the per-episode flag nonce backstop ─────────────────────────────
# A random per-episode nonce is written into qgb_flags.txt and the .qgb markers. It
# reaches the agent only by reading the app's private sandbox — which the meter refuses
# — so its appearance in agent-visible output voids the episode, whatever route got it
# there. Unlike the canary it is secret, so matching the INPUT too is safe.

_NONCE = "QGB-NONCE-0123456789abcdef0123456789abcdef"


def _scan_nonce(transcript: str):
    return scan(TranscriptParser(transcript), WS, repo_root=REPO, home=HOME, nonce=_NONCE)


def test_flag_nonce_in_a_tool_result_voids_the_episode():
    """A root-shell bypass that cats qgb_flags.txt lands the nonce in the result."""
    r = _scan_nonce(_tx(("Bash", {"command": "adb -P 5037 shell su 0 cat /data/data/com.x/files/qgb_flags.txt"},
                         f"#{_NONCE}\nbug-1\n")))
    assert r.contaminated
    assert "flag_nonce" in r.reasons


def test_flag_nonce_in_a_written_file_voids_the_episode():
    """Any file the agent WROTE carrying the nonce (it can only have read it first)."""
    r = _scan_nonce(_tx(("Write", {"file_path": "notes.txt", "content": f"seen {_NONCE}"}, "ok")))
    assert r.contaminated
    assert "flag_nonce" in r.reasons


def test_flag_nonce_value_is_never_recorded():
    """The nonce must not land in anything the scorer/reporting publishes: the hit's
    detail is a fixed string and the metrics never carry the value."""
    r = _scan_nonce(_tx(("Bash", {"command": "cat q"}, f"{_NONCE}\n")))
    import json as _json
    blob = _json.dumps(r.as_metrics())
    assert _NONCE not in blob
    assert all(_NONCE not in (h.get("detail") or "") for h in r.hard)


def test_no_nonce_configured_is_never_a_hit():
    """Existing saved episodes carry no nonce; scanning without one is unchanged."""
    r = _scan(_tx(("Bash", {"command": "cat q"}, f"{_NONCE}\nbug-1\n")))
    assert not r.contaminated


# ── QUA-2804: adb server-selection bypasses the meter ─────────────────────────
# The meter guards the agent's adb through ANDROID_ADB_SERVER_PORT in agent_env. A
# command that re-selects the server reaches the real adb server directly, unmetered;
# the meter cannot refuse what never reaches it, so it is a HARD contamination hit read
# off the agent's own command text.

@pytest.mark.parametrize("cmd", [
    "adb -P 5037 shell id",
    "adb -H 127.0.0.1 -P 5037 shell id",
    "adb -L tcp:127.0.0.1:5037 shell id",
    "ANDROID_ADB_SERVER_PORT=5037 adb shell id",
    "ANDROID_ADB_SERVER_ADDRESS=127.0.0.1 adb shell id",
    "ANDROID_ADB_SERVER_HOST=127.0.0.1 adb devices",
    "ADB_SERVER_SOCKET=tcp:127.0.0.1:5037 adb shell id",
])
def test_adb_server_selection_voids_the_episode(cmd):
    r = _scan(_tx(("Bash", {"command": cmd}, "uid=2000")))
    assert r.contaminated, cmd
    assert "adb_server_bypass" in r.reasons


@pytest.mark.parametrize("cmd", [
    # the agent's normal adb through the meter — no server-selection option
    "adb -s emulator-5554 shell input tap 1 2",
    "adb shell uiautomator dump /sdcard/w.xml",
    # merely READING the meter port is not an override
    "echo $ANDROID_ADB_SERVER_PORT",
    "env | grep ANDROID_ADB_SERVER_PORT",
    # unrelated flags that happen to contain the letters
    "adb -s emulator-5554 shell dumpsys window",
])
def test_ordinary_adb_is_not_a_server_selection_bypass(cmd):
    assert not _scan(_tx(("Bash", {"command": cmd}, "ok"))).contaminated, cmd
