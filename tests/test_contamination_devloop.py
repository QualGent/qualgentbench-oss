"""DevLoop-MCP's artifact directories are another episode's answers (QUA-2800).

One standalone DevLoop server serves every episode of a run (and every lane). It
writes screenshots, diff images, baselines, traces, profiles and screen
recordings to the host, under its temp root, `~/.devloop-mcp` and `~/.devloop`.
On a native host run the agent has a shell, so a later episode could open an
earlier one's files there — the other arm of the same case included. The server
deletes a session's files when the session ends or loses its device, but a
concurrent lane's files exist while that lane runs, so the scanner is the rule:
reading anything there that the server did not hand THIS agent voids the
episode, like reading another episode's directory (QUA-2778).
"""

from __future__ import annotations

import json
import os
import tempfile

from qualgentbench.contamination import devloop_default_roots, scan
from qualgentbench.transcript import TranscriptParser

HOME = "/Users/dev"
WS = f"{HOME}/.qualgentbench/runs/cal-x~seeded/2026-09-23T00-00-00Z_b_trial-1/workspace"
REPO = "/Users/dev/src/qualgentbench-oss"
ROOT = "/private/var/folders/ab/T/devloop-mcp"           # as the server reports it
OWN = f"{ROOT}/sessions/{'a' * 32}"
OTHER = f"{ROOT}/sessions/{'b' * 32}"


def _tx(*calls: tuple[str, dict, str]) -> str:
    """A claude-code stream-json transcript of (tool, input, result) triples."""
    lines = []
    for i, (name, inp, result) in enumerate(calls):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result}]}}))
    return "\n".join(lines)


def _scan(*calls, roots=(ROOT,)):
    return scan(TranscriptParser(_tx(*calls)), WS, repo_root=REPO, home=HOME,
                devloop_roots=list(roots))


def _saved(path: str):
    """The server handing this agent a baseline path in its own tool result."""
    return ("mcp__device__mobile_visual_baseline",
            {"name": "home", "action": "save", "device": "emulator-5554"},
            json.dumps({"saved": True, "path": path}))


def test_listing_the_servers_session_roots_voids_the_episode():
    r = _scan(("Bash", {"command": f"ls -la {ROOT}/sessions/"}, "total 0"))
    assert r.contaminated and r.reasons == ["devloop_artifacts"]


def test_reading_another_sessions_baseline_voids_the_episode():
    r = _scan(_saved(f"{OWN}/baselines/home.png"),
              ("Read", {"file_path": f"{OTHER}/baselines/home.png"}, "<image>"))
    assert r.contaminated and r.reasons == ["devloop_artifacts"]
    assert r.hard[0]["detail"] == f"{OTHER}/baselines/home.png"


def test_a_path_the_server_handed_this_agent_stays_readable():
    """Its own saved baseline, the directory it sits in, its own diff image."""
    r = _scan(_saved(f"{OWN}/baselines/home.png"),
              ("Read", {"file_path": f"{OWN}/baselines/home.png"}, "<image>"),
              ("Bash", {"command": f"ls {OWN}/baselines"}, "home.png"),
              ("Read", {"file_path": f"{OWN}/visual-diffs/d.png"}, "<image>"))
    assert not r.contaminated, r.hard


def test_the_other_symlink_spelling_of_a_handed_path_is_the_same_path():
    bare = OWN.replace("/private/var/", "/var/")
    r = _scan(_saved(f"{OWN}/baselines/home.png"),
              ("Read", {"file_path": f"{bare}/baselines/home.png"}, "<image>"))
    assert not r.contaminated, r.hard
    r = _scan(("Read", {"file_path": f"{OTHER.replace('/private/var/', '/var/')}/x.png"}, ""))
    assert r.contaminated


def test_a_path_only_a_shell_printed_is_not_handed():
    """`find` output names another session's file; that is not the server handing it."""
    r = _scan(("Bash", {"command": "find / -name '*.png' 2>/dev/null | head"},
               f"{OTHER}/baselines/home.png"),
              ("Read", {"file_path": f"{OTHER}/baselines/home.png"}, "<image>"))
    assert r.contaminated and "devloop_artifacts" in r.reasons


def test_a_server_message_naming_the_whole_root_opens_nothing():
    r = _scan(("mcp__device__mobile_visual_compare", {"name": "x"},
               f"No baseline named 'x' in {ROOT}. Existing baselines: (none)."),
              ("Bash", {"command": f"ls {OTHER}"}, ""))
    assert r.contaminated


def test_a_recording_the_server_handed_is_readable_and_anothers_is_not():
    rec = f"{ROOT}/screen-recordings/0123abcd/{'c' * 32}/recording.mp4"
    other = f"{ROOT}/screen-recordings/0123abcd/{'d' * 32}/recording.mp4"
    handed = ("mcp__device__mobile_stop_synthetic_screen_recording",
              {"device": "emulator-5554", "recording_id": "c" * 32},
              json.dumps({"status": "completed", "artifact": {"host_path": rec}}))
    assert not _scan(handed, ("Bash", {"command": f"ffprobe {rec}"}, "")).contaminated
    assert _scan(handed, ("Bash", {"command": f"ffprobe {other}"}, "")).contaminated


def test_the_default_roots_cover_the_documented_locations():
    """No server report (the bare arm, an older DevLoop): the documented defaults."""
    roots = devloop_default_roots(HOME)
    assert f"{HOME}/.devloop-mcp" in roots and f"{HOME}/.devloop" in roots
    tmp = os.path.join(tempfile.gettempdir(), "devloop-mcp")
    assert os.path.normpath(tmp) in roots or os.path.realpath(tmp) in roots
    r = scan(TranscriptParser(_tx(
        ("Bash", {"command": "ls ~/.devloop-mcp/baselines"}, "home.png"))),
        WS, repo_root=REPO, home=HOME)
    assert r.contaminated and r.reasons == ["devloop_artifacts"]
    r = scan(TranscriptParser(_tx(
        ("Bash", {"command": "cat /tmp/devloop-mcp/screen-recordings/.lease-x.json"}, "{}"))),
        WS, repo_root=REPO, home=HOME)
    assert r.contaminated


def test_ordinary_scratch_use_is_untouched():
    r = _scan(("Bash", {"command": "mkdir -p /tmp/qa && echo hi > /tmp/qa/notes.txt"}, ""),
              ("Bash", {"command": f"ls {WS}"}, "findings.yaml"))
    assert not r.contaminated, r.hard


def test_the_journey_scorer_passes_the_episodes_roots(monkeypatch):
    from qualgentbench import contamination, journey
    from test_journey import _obs, _spec, _task, _transcript, _write

    seen = {}
    real = contamination.scan

    def spy(parser, workspace, **kw):
        seen.update(kw)
        return real(parser, workspace, **kw)

    monkeypatch.setattr(contamination, "scan", spy)
    t = _task(_spec("clean"))
    t.bug_spec["devloop_roots"] = [ROOT]
    journey.journey_verdict(_transcript(_obs("Total: 4 items"), _write("pass")), "m", t)
    assert seen["devloop_roots"] == [ROOT]
