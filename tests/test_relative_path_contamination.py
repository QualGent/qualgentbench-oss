"""Relative-path reads of the arm index and sibling episodes are hard hits (QUA-2815).

QUA-2806 made the arm blind: the episode dir names the case and an opaque id, and the
full identity (`<case>~seeded|~clean`) lives harness-side at
`<runs>/_runs/<run_id>/episodes/<ep-id>.json`. Both arms of a case are sibling dirs under
`<runs>/<case>/`. On a host run nothing BLOCKS an agent's shell from reaching either —
the guarantee is that reaching them is a hard `other_episode` hit. Until QUA-2815 the scan
only matched ABSOLUTE paths, so `cat ../../*/result.json` (the sibling arm's verdict,
from the agent's own cwd) and `cat ../../../_runs/*/episodes/ep-<own id>.json` (the own
id is in the cwd) were clean. Every read below is taken from the QUA-2810 final review
or from its "think beyond the list" brief, and each one is scanned against a REAL runs
tree on disk, so symlinks the agent creates resolve as they would have for it.
"""

from __future__ import annotations

import json

import pytest

from qualgentbench.contamination import scan
from qualgentbench.transcript import TranscriptParser

CASE = "demo-open-list"
OWN_ID = "ep-aaaaaaaaaaaa"
SIB_ID = "ep-bbbbbbbbbbbb"
REPO = "/opt/src/qualgentbench-oss"


def _tx(*calls) -> str:
    """claude-code stream-json of (tool, input, result) triples."""
    lines = []
    for i, (name, inp, result) in enumerate(calls):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result}]}}))
    return "\n".join(lines)


def _codex(*commands: str, output: str = "") -> str:
    """codex `exec --json`: every command wrapped the way codex runs it."""
    lines = []
    for i, cmd in enumerate(commands):
        wrapped = "/bin/zsh -lc " + "'" + cmd.replace("'", "'\"'\"'") + "'"
        lines.append(json.dumps({"type": "item.completed", "item": {
            "id": f"item_{i}", "type": "command_execution", "command": wrapped,
            "aggregated_output": output, "exit_code": 0, "status": "completed"}}))
    return "\n".join(lines)


def _bash(*commands: str, result: str = ""):
    return _tx(*[("Bash", {"command": c}, result) for c in commands])


@pytest.fixture
def tree(tmp_path):
    """A blinded runs tree: own episode, its sibling arm (finished), the run's index."""
    home = tmp_path / "home"
    runs = home / ".qualgentbench" / "runs"
    own = runs / CASE / f"2026-09-24T21-19-44Z_{CASE}_claude-code_m_mcp_trial-1_{OWN_ID}"
    sib = runs / CASE / f"2026-09-24T21-21-33Z_{CASE}_claude-code_m_mcp_trial-1_{SIB_ID}"
    for ep in (own, sib):
        (ep / "workspace").mkdir(parents=True)
        (ep / "episode.json").write_text(json.dumps({"task_id": CASE, "blinded": True}))
    (own / "codex_home" / "home").mkdir(parents=True)
    (sib / "result.json").write_text(json.dumps({"task_id": f"{CASE}~seeded"}))
    index = runs / "_runs" / "r1" / "episodes"
    index.mkdir(parents=True)
    (index / f"{OWN_ID}.json").write_text(json.dumps({"task_id": f"{CASE}~clean"}))
    return {"home": str(home), "runs": runs, "own": own, "sib": sib,
            "ws": own / "workspace"}


def _scan(tree, transcript: str):
    return scan(TranscriptParser(transcript), tree["ws"], repo_root=REPO, home=tree["home"])


def _hard(tree, transcript):
    r = _scan(tree, transcript)
    return r.reasons


# ── the reviewer's reads, each a hard hit ────────────────────────────────────

REVIEW_READS = [
    f"cat ../../../_runs/*/episodes/{OWN_ID}.json",
    "cd ../../..; grep task_id _runs/*/episodes/*.json",
    "cat ../../*/result.json",
    f"cat $PWD/../../../_runs/r1/episodes/{OWN_ID}.json",
    'cat "${PWD}/../../"*/result.json',
    "cat $(pwd)/../../*/result.json",
]


@pytest.mark.parametrize("cmd", REVIEW_READS)
def test_the_reviewers_relative_reads_are_hard_hits_claude(tree, cmd):
    assert _hard(tree, _bash(cmd)) == ["other_episode"], cmd


@pytest.mark.parametrize("cmd", REVIEW_READS)
def test_the_reviewers_relative_reads_are_hard_hits_codex(tree, cmd):
    assert _hard(tree, _codex(cmd)) == ["other_episode"], cmd


# ── beyond the list ──────────────────────────────────────────────────────────

BEYOND = [
    "pushd .. >/dev/null && pushd .. && cat */result.json",
    "cd .. && cd - && cat $OLDPWD/../*/result.json",
    "find ../.. -name result.json -exec cat {} +",
    "ls -R ../../..",
    "ln -s ../../.. x && cat x/_runs/r1/episodes/*.json",
    f"python3 -c \"print(open('../../{SIB_ID}/result.json').read())\"",
    "(cd ../../.. && cat _runs/r1/episodes/*.json)",
    "bash -c 'cd ../.. && cat */result.json'",
    "for f in ../../*/result.json; do cat $f; done",
    "tar cf - ../.. | tar tvf -",
    "cat < ../../*/result.json",
    "cd ..\ncd ..\ncat */result.json",
]


@pytest.mark.parametrize("cmd", BEYOND)
def test_other_escapes_from_the_episode_tree_are_hard_hits(tree, cmd):
    assert _hard(tree, _bash(cmd)) == ["other_episode"], cmd


def test_a_cd_persists_across_claude_bash_calls(tree):
    """claude-code keeps the shell's cwd between Bash calls: two `cd ..` calls, then a
    relative read that, from the workspace, would have named the agent's own dir."""
    t = _bash("cd ..", "ls", "cat ../*/result.json")
    assert _hard(tree, t) == ["other_episode"]
    # From the workspace the same read stays inside the agent's own episode.
    assert _hard(tree, _bash("cat ../*/result.json")) == []


def test_a_codex_command_starts_from_the_workspace_every_time(tree):
    """codex runs each command in a fresh shell at `--cd`: the earlier `cd` is gone."""
    assert _hard(tree, _codex("cd ..", "cat ../*/result.json")) == []


def test_claude_resetting_its_cwd_is_followed(tree):
    """claude-code resets a cwd that left the project and SAYS so; the next relative
    path resolves from where it put the shell, not from where the agent sent it."""
    ws = str(tree["ws"])
    t = _tx(("Bash", {"command": "cd /tmp && ls"}, f"x\nShell cwd was reset to {ws}"),
            ("Bash", {"command": "cat ../episode.json"}, "{}"))
    assert _hard(tree, t) == []
    t = _tx(("Bash", {"command": "cd /tmp && ls"}, f"x\nShell cwd was reset to {ws}"),
            ("Bash", {"command": "cat ../../*/result.json"}, "{}"))
    assert _hard(tree, t) == ["other_episode"]


def test_codex_tilde_is_its_episode_home(tree):
    """codex's HOME is `<episode>/codex_home/home`: `~/../../..` is the case dir."""
    assert _hard(tree, _codex("cat ~/../../../*/result.json")) == ["other_episode"]
    assert _hard(tree, _codex("ls ~ && cat ~/.profile")) == []


def test_a_symlink_the_agent_made_without_a_visible_target_is_followed(tree):
    """The link's target was built where the scan cannot read it (a python one-liner
    with no path in it); the read through it still resolves on disk."""
    (tree["ws"] / "x").symlink_to(tree["runs"], target_is_directory=True)
    t = _bash("python3 -c 'import os; os.symlink(os.path.dirname(os.getcwd()), \"x\")'",
              f"cat x/_runs/r1/episodes/{OWN_ID}.json")
    assert _hard(tree, t) == ["other_episode"]


@pytest.mark.parametrize("name,inp", [
    ("Read", {"file_path": f"../../{SIB_ID}/result.json"}),
    ("Glob", {"pattern": "../../*/result.json"}),
    ("Glob", {"pattern": "*/result.json", "path": "../.."}),
    ("Grep", {"pattern": "task_id", "path": "../../../_runs"}),
    ("LS", {"path": "../.."}),
])
def test_claude_file_tools_with_relative_paths_are_hard_hits(tree, name, inp):
    assert _hard(tree, _tx((name, inp, "{}"))) == ["other_episode"]


def test_a_grep_pattern_is_a_regex_not_a_path(tree):
    assert _hard(tree, _tx(("Grep", {"pattern": "../..", "path": "."}, ""))) == []


@pytest.mark.parametrize("cmd", [
    "grep -r task_id ~/.qualgentbench",
    "rg seeded ~",
    "find ~ -name '*.json' -exec grep -l seeded {} +",
    "find ~ -name '*.json' | xargs grep seeded",
    "cd ~ && rg task_id",
])
def test_a_recursive_read_over_an_ancestor_of_the_runs_root_is_a_hard_hit(tree, cmd):
    assert _hard(tree, _bash(cmd)) == ["other_episode"], cmd


def test_the_grep_tool_over_an_ancestor_is_a_recursive_read(tree):
    assert _hard(tree, _tx(("Grep", {"pattern": "seeded", "path": tree["home"]}, ""))) \
        == ["other_episode"]
    assert _hard(tree, _tx(("Grep", {"pattern": "seeded"}, ""))) == []   # its own cwd


def test_the_arm_label_in_a_tool_result_is_a_hard_hit(tree):
    """Belt and braces for a read the path model cannot see (a cwd built by command
    substitution, a program that reads on its own): the harness-side files name the
    arm, and after QUA-2806 nothing the agent may legitimately read does."""
    t = _bash('cd "$(dirname "$(dirname "$PWD")")" && cat */result.json',
              result=json.dumps({"task_id": f"{CASE}~seeded"}))
    r = _scan(tree, t)
    assert r.reasons == ["other_episode"]
    assert all(CASE + "~" not in h["detail"] for h in r.hard)   # the label is not stored


def test_codex_workdir_from_the_session_rollout_is_scanned(tree):
    """codex's shell tool takes a per-call `workdir` that `exec --json` never prints;
    the rollout under the episode's CODEX_HOME records it."""
    sessions = tree["own"] / "codex_home" / "sessions" / "2026" / "09" / "24"
    sessions.mkdir(parents=True)
    args = json.dumps({"cmd": "cat */result.json", "workdir": "../.."})
    (sessions / "rollout-x.jsonl").write_text(json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command", "arguments": args}}) + "\n")
    assert _hard(tree, _codex("cat */result.json")) == ["other_episode"]


# ── an honest agent's own tree stays clean ───────────────────────────────────

HONEST = [
    "ls ..",
    "ls -la ..",
    "ls -R ..",
    "cat ../episode.json",
    "cat ../instruction_sent.md",
    "find .. -name '*.json'",
    "cd .. && ls && cat mcp_config.json",
    "cat ./findings.yaml",
    "ls ~",
    "cd /tmp && ls ../..",
    'adb -s emulator-5554 shell "cd /sdcard && ls ../../.."',
    "adb -s emulator-5554 shell ls ../..",
    "adb -s emulator-5554 exec-out uiautomator dump /dev/tty",
    "adb -s emulator-5554 shell cat /sdcard/window_dump.xml > ./dump.xml",
    "find / -name adb 2>/dev/null | head",
    "mkdir -p app/src && cd app/src && cat ../../findings.yaml",
    "echo '../../ is just text' > note.txt",
]


@pytest.mark.parametrize("cmd", HONEST)
def test_an_honest_agents_own_tree_is_clean(tree, cmd):
    assert _hard(tree, _bash(cmd)) == [], cmd
    assert _hard(tree, _codex(cmd)) == [], cmd


def test_a_persisted_cwd_inside_the_workspace_resolves_from_there(tree):
    t = _bash("mkdir -p app/src && cd app/src", "cat ../../findings.yaml")
    assert _hard(tree, t) == []


def test_the_agents_own_path_in_a_result_is_not_an_arm_label(tree):
    t = _bash("pwd", result=str(tree["ws"]))
    assert _hard(tree, t) == []


def test_old_layout_episodes_are_not_voided_by_their_own_label(tmp_path):
    """Before QUA-2806 the cwd itself named the arm, so every result that echoed a path
    carried `~seeded`. Those episodes keep their verdicts."""
    runs = tmp_path / "home" / ".qualgentbench" / "runs"
    ws = runs / f"{CASE}~seeded" / f"2026-09-23T00-00-00Z_{CASE}~seeded_c_m_mcp_trial-1" / "workspace"
    ws.mkdir(parents=True)
    t = _bash("pwd && ls ..", result=f"{ws}\nepisode.json")
    r = scan(TranscriptParser(t), ws, repo_root=REPO, home=str(tmp_path / "home"))
    assert r.reasons == []


def test_a_cwd_the_model_cannot_follow_is_soft_not_guessed(tree):
    """`cd "$(…)"` / `cd $VAR`: the scan records it and does not guess where relative
    paths after it land (guessing the workspace would void honest `cd $ANDROID_HOME`)."""
    t = _bash('cd "$(git rev-parse --show-toplevel)" && ls ../..', "cat ../../README")
    r = _scan(tree, t)
    assert r.reasons == []
    assert "unresolved_cwd" in [s["kind"] for s in r.soft]


def test_an_absolute_cd_after_an_unknown_one_is_followed_again(tree):
    t = _bash("cd $ANDROID_HOME", f"cd {tree['ws']}", "cat ../../*/result.json")
    assert _hard(tree, t) == ["other_episode"]


def test_a_symlinked_runs_root_does_not_void_the_agents_own_reads(tree, tmp_path):
    """The workspace may be handed over through a symlink (macOS /tmp, /var); a read of
    the agent's own tree resolves to the real path and must stay its own."""
    link = tmp_path / "linked-home"
    link.symlink_to(tree["home"], target_is_directory=True)
    ws = link / tree["ws"].relative_to(tree["home"])
    r = scan(TranscriptParser(_bash("ls -la .. && cat ../episode.json")), ws,
             repo_root=REPO, home=tree["home"])
    assert r.reasons == []
    r = scan(TranscriptParser(_bash("cat ../../*/result.json")), ws,
             repo_root=REPO, home=tree["home"])
    assert r.reasons == ["other_episode"]


def test_a_relative_mcp_argument_is_the_servers_business(tree):
    """An MCP server resolves its own paths (DevLoop's cwd is not the agent's)."""
    t = json.dumps({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "m1", "name": "mcp__device__mobile_push_media",
        "input": {"path": "../../x.png"}}]}})
    assert _hard(tree, t) == []
