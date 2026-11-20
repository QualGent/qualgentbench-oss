"""A glob that can only name the agent's OWN episode directory is not contamination
(QUA-3543).

Memory round 1 voided three dynamic-memory units as `other_episode`. In each, the agent
wrote `findings.yaml` and `memory_proposals.yaml` after retyping its own cwd with a
wildcard for the long middle part: `cd <runs>/<case>/*trial-1_<own ep id>/workspace`. The
own-directory exemption never applied to a pattern, and every glob under the runs root
was a hit. A pattern that carries the episode's own random id cannot match a sibling, so
it is excused; a pattern without it still reaches every sibling and stays a hit.
"""

from __future__ import annotations

import json

import pytest

from qualgentbench.contamination import scan
from qualgentbench.transcript import TranscriptParser

MODEL_ARM = "claude-code_us.anthropic.claude-opus-5-5_mcp+mem-dynamic-small-none"

# The three excluded round-1 units: (case, own episode dir name).
EXCLUDED = [
    ("uhabits-rename-from-list",
     f"2026-11-19T12-33-52Z_uhabits-rename-from-list_{MODEL_ARM}_trial-1_ep-7fa312579f42"),
    ("markor-new-note-in-folder",
     f"2026-11-19T13-26-24Z_markor-new-note-in-folder_{MODEL_ARM}_trial-2_ep-b7e712016ba6"),
    ("stt-activity-with-note",
     f"2026-11-19T18-20-49Z_stt-activity-with-note_{MODEL_ARM}_trial-1_ep-92b53e0285fc"),
]

HEREDOC = ("cat > findings.yaml <<'EOF'\nverdict: fail\nbugs: []\nEOF\n"
           "cat > memory_proposals.yaml <<'EOF'\n- type: nav\n  text: x\n"
           "  evidence: {step: 2, screen: Home}\nEOF\ncat findings.yaml")


def _bash(*commands: str) -> str:
    lines = []
    for i, cmd in enumerate(commands):
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "verdict: fail"}]}}))
    return "\n".join(lines)


def _tree(tmp_path, case: str, own_name: str):
    """A lab-shaped runs tree (`<work>/runs/<case>/<episode>/workspace`) with the own
    episode and a sibling episode of the same case (the other version)."""
    runs = tmp_path / "work" / "runs"
    own = runs / case / own_name
    sib_name = own_name.rsplit("_ep-", 1)[0].replace("trial-", "trial-9") + "_ep-0123456789ab"
    sib = runs / case / sib_name
    for ep in (own, sib):
        (ep / "workspace").mkdir(parents=True)
        (ep / "episode.json").write_text(json.dumps({"task_id": case, "blinded": True}))
    (sib / "result.json").write_text(json.dumps({"task_id": f"{case}~seeded"}))
    return {"runs": runs, "own": own, "sib": sib, "ws": own / "workspace",
            "home": str(tmp_path / "home")}


def _reasons(tree, transcript: str) -> list[str]:
    return scan(TranscriptParser(transcript), tree["ws"], repo_root="/opt/src/qgb",
                home=tree["home"]).reasons


@pytest.mark.parametrize("case,own_name", EXCLUDED)
def test_the_three_excluded_round1_commands_are_clean(tmp_path, case, own_name):
    """Replays each voided unit's command (the runs root moved under tmp)."""
    tree = _tree(tmp_path, case, own_name)
    trial_ep = own_name[own_name.index("trial-"):]
    cmd = f"cd {tree['runs']}/{case}/*{trial_ep}/workspace && {HEREDOC}"
    assert _reasons(tree, _bash(cmd)) == []


@pytest.mark.parametrize("pattern", [
    "{runs}/{case}/*{ep}/workspace/memory_proposals.yaml",
    "{runs}/*/*{ep}/workspace",
    "{runs}/{case}/2026-*_{ep}",
    "{runs}/{case}/*{ep}/workspace/*",
])
def test_own_episode_globs_pass(tmp_path, pattern):
    case, own_name = EXCLUDED[0]
    tree = _tree(tmp_path, case, own_name)
    ep = own_name.rsplit("_", 1)[1]
    path = pattern.format(runs=tree["runs"], case=case, ep=ep)
    assert _reasons(tree, _bash(f"ls {path}")) == []


def test_the_own_workspace_by_literal_path_passes(tmp_path):
    case, own_name = EXCLUDED[1]
    tree = _tree(tmp_path, case, own_name)
    assert _reasons(tree, _bash(f"cat {tree['ws']}/memory_proposals.yaml")) == []


@pytest.mark.parametrize("pattern", [
    "{runs}/{case}/*/result.json",                  # every episode of the case
    "{runs}/{case}/*_ep-0123456789ab/result.json",  # the sibling, by its id
    "{runs}/{case}/*",                             # lists the sibling
    "{runs}/{case}/*{ep}/../*/result.json",        # back out of the own dir
    "{runs}/*/*/workspace/findings.yaml",
])
def test_globs_that_reach_a_sibling_are_still_hits(tmp_path, pattern):
    case, own_name = EXCLUDED[0]
    tree = _tree(tmp_path, case, own_name)
    ep = own_name.rsplit("_", 1)[1]
    path = pattern.format(runs=tree["runs"], case=case, ep=ep)
    assert _reasons(tree, _bash(f"cat {path}")) == ["other_episode"]


def test_a_cd_into_the_own_glob_then_up_to_the_sibling_is_a_hit(tmp_path):
    case, own_name = EXCLUDED[2]
    tree = _tree(tmp_path, case, own_name)
    ep = own_name.rsplit("_", 1)[1]
    cmd = f"cd {tree['runs']}/{case}/*{ep}/workspace && cat ../../*/result.json"
    assert _reasons(tree, _bash(cmd)) == ["other_episode"]


def test_the_sibling_by_literal_path_is_a_hit(tmp_path):
    case, own_name = EXCLUDED[0]
    tree = _tree(tmp_path, case, own_name)
    assert _reasons(tree, _bash(f"cat {tree['sib']}/result.json")) == ["other_episode"]
