"""Cross-run memory reaches the agent through its system channel, never the brief
(QUA-3488).

Each episode here is the stubbed `run_episode` of `test_blind_arm` (no device, no model):
the REAL claude-code and codex-cli adapters run `prepare`, `command` and `env`, and the
capture records everything the agent could see. A memory arm must change exactly the
system channel (codex `developer_instructions`, claude `--append-system-prompt-file` with
auto-memory off), never the brief; `none` must leave the episode byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import click
import pytest
from test_agent_dump import device  # noqa: F401 — the fake-device fixture
from test_blind_arm import _arm_episode, _normalised

from qualgentbench import cli, journey, leaderboard
from qualgentbench import episode_runner as er
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.config import INSTRUCTION_FILES, instruction_files_on_chain
from qualgentbench.memory import arm as marm
from qualgentbench.memory import format as fmt
from qualgentbench.result import RunResult

FIXTURE = Path(__file__).parent / "fixtures" / "memory" / "good"
APP = "demo"          # `test_agent_dump._episode`'s app id
ORG = "samplecorp"
CANARY = "The archive switch is called ZEBRAFINCH-42."
AGENTS = [ClaudeCodeAdapter, CodexCliAdapter]
AGENT_IDS = ["claude-code", "codex-cli"]


@pytest.fixture
def memory_root(tmp_path) -> Path:
    """The fixture org + app memory, re-keyed to the stub episode's app, with one
    nonsense fact (the canary) the agent could only know from memory."""
    root = tmp_path / "memroot"
    (root / "org").mkdir(parents=True)
    (root / "app" / ORG).mkdir(parents=True)
    shutil.copy(FIXTURE / "org" / f"{ORG}.md", root / "org" / f"{ORG}.md")
    shutil.copy(FIXTURE / "app" / ORG / "sampleapp.changes.md",
                root / "app" / ORG / f"{APP}.changes.md")
    doc = fmt.load(FIXTURE / "app" / ORG / "sampleapp.md", fmt.APP)
    doc.add_entry("setup", CANARY, {"src": "human", "confirmed": "2026-11-17",
                                    "seen": 9, "contra": 0})
    doc.save(root / "app" / ORG / f"{APP}.md")
    return root


async def _run(monkeypatch, tmp_path, agent_cls, tooling="raw", memory=None,
               version="clean", write_proposals: str | None = None):
    task, opts, capture = _arm_episode(monkeypatch, tmp_path, agent_cls, version, tooling)
    opts.memory = memory
    if write_proposals is not None:
        inner_run = capture.run

        async def run(instruction, context):
            out = await inner_run(instruction, context)
            (context.workspace_dir / marm.PROPOSALS_FILENAME).write_text(write_proposals)
            return out
        capture.run = run
    result = await er.run_episode(task, opts)
    run_dir = Path(opts.runs_dir) / result.artifact_dir
    return result, capture.seen, run_dir


# ── `none` is today's episode, byte for byte ──────────────────────────────────

@pytest.mark.parametrize("tooling", ["raw", "mcp"])
@pytest.mark.parametrize("agent_cls", AGENTS, ids=AGENT_IDS)
async def test_memory_none_is_byte_identical_to_no_memory(device, monkeypatch, tmp_path,  # noqa: F811
                                                          agent_cls, tooling):
    device(u2=False)
    views, results = {}, {}
    for label, memory in (("unset", None), ("none", marm.MemoryArm())):
        result, seen, run_dir = await _run(monkeypatch, tmp_path / label, agent_cls,
                                           tooling, memory)
        views[label] = _normalised(seen, run_dir)
        results[label] = result
    assert views["unset"] == views["none"]
    for result in results.values():
        assert result.condition == tooling
        assert not any(k.startswith("memory_") for k in result.metrics)
        assert "memory" not in result.provenance
    seen = views["none"]
    assert "--append-system-prompt-file" not in seen["argv"]
    assert "--setting-sources" not in seen["argv"]
    assert not any("CLAUDE_CODE_DISABLE_AUTO_MEMORY" in k for k in seen)
    assert not any(k.endswith(marm.SYSTEM_FILENAME) for k in seen)
    if agent_cls is CodexCliAdapter:
        assert "developer_instructions" not in seen["file codex_home/config.toml"]


# ── a memory arm changes the system channel and nothing else the agent reads ─────

@pytest.mark.parametrize("agent_cls", AGENTS, ids=AGENT_IDS)
async def test_memory_reaches_the_system_channel_not_the_brief(device, monkeypatch,  # noqa: F811
                                                               tmp_path, agent_cls,
                                                               memory_root):
    device(u2=False)
    briefs: list[tuple] = []

    def spy(task, serial, tooling):
        briefs.append((task.id, serial, tooling))
        return f"Test case: Open the list on {serial}."

    arms = {"A0": None,
            "static": marm.MemoryArm("static", "small", snapshot=memory_root),
            "dynamic": marm.MemoryArm("dynamic", "large", "diff", snapshot=memory_root)}
    seen_by, run_dirs = {}, {}
    for label, memory in arms.items():
        task, opts, capture = _arm_episode(monkeypatch, tmp_path / label, agent_cls,
                                           "clean", "mcp")
        monkeypatch.setattr(er, "_ablation_instruction", spy)
        opts.memory = memory
        result = await er.run_episode(task, opts)
        seen_by[label] = capture.seen
        run_dirs[label] = Path(opts.runs_dir) / result.artifact_dir

    # The brief: built from the same inputs, and the same bytes on disk, on every arm.
    assert len(set(briefs)) == 1 and len(briefs) == 3
    sent = {k: (d / "instruction_sent.md").read_bytes() for k, d in run_dirs.items()}
    assert len(set(sent.values())) == 1
    assert {s["instruction"] for s in seen_by.values()} == {sent["A0"].decode()}
    assert CANARY not in sent["A0"].decode()

    for label in ("static", "dynamic"):
        seen, run_dir = seen_by[label], run_dirs[label]
        system = (run_dir / marm.SYSTEM_FILENAME).read_text()
        assert CANARY in system and "<qualgent_memory" in system
        assert marm.PROPOSALS_FILENAME in system
        # Never inside the workspace, and never a file an agent loads as instructions.
        assert not (run_dir / "workspace" / marm.SYSTEM_FILENAME).exists()
        assert instruction_files_on_chain(run_dir / "workspace") == []
        argv = seen["argv"].split("\n")
        if agent_cls is ClaudeCodeAdapter:
            i = argv.index("--append-system-prompt-file")
            assert argv[i + 1] == str(run_dir / marm.SYSTEM_FILENAME)
            j = argv.index("--setting-sources")
            assert argv[j + 1] == ""
            assert seen["env CLAUDE_CODE_DISABLE_AUTO_MEMORY"].endswith("=1")
            # The budget hook still loads: --settings is a flag source.
            assert "--settings" in argv
        else:
            config = seen["file codex_home/config.toml"]
            assert "developer_instructions" in config and "ZEBRAFINCH-42" in config
            assert "model_instructions_file" not in config
            # Codex's own memories stay off.
            k = [n for n, a in enumerate(argv) if a == "--disable" and argv[n + 1] == "memories"]
            assert k, argv
    # The diff arm renders the changes text; the small arm does not.
    assert "What changed since the last build" in (run_dirs["dynamic"] / marm.SYSTEM_FILENAME).read_text()
    assert "What changed since the last build" not in (run_dirs["static"] / marm.SYSTEM_FILENAME).read_text()


@pytest.mark.parametrize("agent_cls", AGENTS, ids=AGENT_IDS)
async def test_memory_result_carries_condition_metrics_and_proposals(device, monkeypatch,  # noqa: F811
                                                                     tmp_path, agent_cls,
                                                                     memory_root):
    device(u2=False)
    proposals = "- type: nav\n  text: Archive is under the overflow menu.\n"
    arm = marm.MemoryArm("dynamic", "small", "diff", snapshot=memory_root)
    result, _seen, run_dir = await _run(monkeypatch, tmp_path, agent_cls, "mcp", arm,
                                       write_proposals=proposals)
    assert result.condition == "mcp+mem-dynamic-small-diff"
    assert "mcp+mem-dynamic-small-diff" in run_dir.name
    m = result.metrics
    # Rendered.metrics(), verbatim.
    assert m["memory_mode"] == "small"
    assert m["memory_tokens_injected"] > 0
    assert m["snapshot_hash"].startswith("sha256:")
    assert m["memory_entries_injected"] > 0 and m["memory_changes_included"] is True
    # The arm and the condition.
    assert (m["memory_arm_mode"], m["memory_budget"], m["memory_diff"]) == ("dynamic", "small", "diff")
    assert m["memory_condition"] == result.condition and m["memory_internal"] is True
    system = (run_dir / marm.SYSTEM_FILENAME).read_bytes()
    assert m["memory_prompt_sha256"] == "sha256:" + hashlib.sha256(system).hexdigest()
    # The proposals file, collected where the agent wrote it.
    assert m["memory_proposals_file"] == "workspace/memory_proposals.yaml"
    assert (run_dir / m["memory_proposals_file"]).read_text() == proposals
    assert m["memory_proposals_sha256"] == "sha256:" + hashlib.sha256(proposals.encode()).hexdigest()
    prov = result.provenance["memory"]
    assert prov["snapshot_hash"] == m["snapshot_hash"] and prov["org_key"] == ORG
    assert prov["app_key"] == APP and prov["mode"] == "dynamic"
    # result.json round-trips.
    saved = json.loads((run_dir / "result.json").read_text())
    assert saved["metrics"]["memory_proposals_file"] == "workspace/memory_proposals.yaml"


async def test_no_proposals_file_is_recorded_as_none(device, monkeypatch, tmp_path,  # noqa: F811
                                                    memory_root):
    device(u2=False)
    arm = marm.MemoryArm("static", "small", snapshot=memory_root)
    result, _, _ = await _run(monkeypatch, tmp_path, CodexCliAdapter, "raw", arm)
    assert result.condition == "raw+mem-static-small-none"
    assert result.metrics["memory_proposals_file"] is None
    assert result.metrics["memory_proposals_bytes"] == 0


@pytest.mark.parametrize("agent_cls", AGENTS, ids=AGENT_IDS)
async def test_index_mode_places_the_files_in_the_workspace(device, monkeypatch, tmp_path,  # noqa: F811
                                                            agent_cls, memory_root):
    device(u2=False)
    arm = marm.MemoryArm("static", "index", snapshot=memory_root)
    result, seen, run_dir = await _run(monkeypatch, tmp_path, agent_cls, "raw", arm)
    ws = run_dir / "workspace"
    org_copy = ws / "memory" / "org" / f"{ORG}.md"
    app_copy = ws / "memory" / "app" / ORG / f"{APP}.md"
    assert org_copy.is_file() and CANARY in app_copy.read_text()
    assert "Invalidated" not in app_copy.read_text()
    assert f"file workspace/memory/app/{ORG}/{APP}.md" in seen
    system = (run_dir / marm.SYSTEM_FILENAME).read_text()
    assert f"memory/app/{ORG}/{APP}.md" in system
    assert result.metrics["memory_mode"] == "index"
    # The placed files never trip the inherited-instructions guard.
    names = {p.name for p in ws.rglob("*")}
    assert not names & {Path(n).name for n in INSTRUCTION_FILES}
    assert instruction_files_on_chain(ws) == []


async def test_a_missing_snapshot_fails_before_the_device(device, monkeypatch, tmp_path):  # noqa: F811
    device(u2=False)
    empty = tmp_path / "empty-snapshot"
    empty.mkdir()
    task, opts, capture = _arm_episode(monkeypatch, tmp_path, CodexCliAdapter, "clean", "raw")
    opts.memory = marm.MemoryArm("dynamic", "small", snapshot=empty)
    with pytest.raises(RuntimeError, match="memory arm cannot run"):
        await er.run_episode(task, opts)
    assert capture.seen == {}
    assert not list(Path(opts.runs_dir).rglob("workspace"))


async def test_a_memory_arm_on_native_is_refused(device, monkeypatch, tmp_path, memory_root):  # noqa: F811
    device(u2=False)
    task, opts, _ = _arm_episode(monkeypatch, tmp_path, CodexCliAdapter, "clean", "raw")
    opts.agent = "native"
    opts.memory = marm.MemoryArm("static", "small", snapshot=memory_root)
    with pytest.raises(marm.MemoryArmError, match="native"):
        await er.run_episode(task, opts)


# ── the arm itself ─────────────────────────────────────────────────────────────

def test_arm_values_are_validated():
    with pytest.raises(marm.MemoryArmError):
        marm.MemoryArm("sometimes")
    with pytest.raises(marm.MemoryArmError):
        marm.MemoryArm("static", "huge")
    with pytest.raises(marm.MemoryArmError):
        marm.MemoryArm("static", "small", "maybe")
    with pytest.raises(marm.MemoryArmError, match="snapshot"):
        marm.MemoryArm("dynamic", "small")
    with pytest.raises(marm.MemoryArmError, match="unknown"):
        marm.MemoryArm.from_dict({"mode": "static", "budgte": "small"})
    assert not marm.MemoryArm().enabled
    assert marm.MemoryArm().condition("mcp") is None
    assert marm.prepare(marm.MemoryArm(), APP) is None
    assert marm.prepare(None, APP) is None


def test_condition_labels_are_path_safe_and_parse_back():
    label = marm.condition_label("mcp", "dynamic", "index", "hunt")
    assert label == "mcp+mem-dynamic-index-hunt"
    assert "/" not in label and " " not in label
    assert marm.is_memory_condition(label)
    assert not marm.is_memory_condition("mcp") and not marm.is_memory_condition("raw")
    assert marm.parse_condition(label) == {"tooling": "mcp", "mode": "dynamic",
                                           "budget": "index", "diff": "hunt"}
    assert marm.parse_condition("mcp") is None


def test_memory_config_file_json_and_yaml(tmp_path):
    snap = tmp_path / "snap"
    js = tmp_path / "m.json"
    js.write_text(json.dumps({"mode": "dynamic", "budget": "index", "diff": "diff",
                              "snapshot": "snap", "org": "samplecorp"}))
    a = marm.MemoryArm.load(js)
    assert (a.mode, a.budget, a.diff, a.snapshot, a.org) == (
        "dynamic", "index", "diff", snap, "samplecorp")
    ym = tmp_path / "m.yaml"
    ym.write_text("memory:\n  mode: static\n  budget: large\n")
    b = marm.MemoryArm.load(ym)
    assert (b.mode, b.budget, b.diff, b.snapshot) == ("static", "large", "none", None)
    assert b.root() == marm.DEFAULT_ROOT
    assert marm.MemoryArm.from_dict(a.as_dict()) == a


def test_render_is_the_memory_library_render(memory_root):
    arm = marm.MemoryArm("static", "small", snapshot=memory_root)
    prepared = marm.prepare(arm, APP)
    from qualgentbench.memory import render
    direct = render.render(memory_root, ORG, APP, "small")
    assert prepared.rendered.text == direct.text
    assert prepared.text.endswith(direct.text)
    assert prepared.rendered.metrics() == direct.metrics()


# ── the board: memory rows are internal ─────────────────────────────────────────

def _result(condition: str) -> RunResult:
    return RunResult(task_id="demo-open-list~clean", task_version="qgb-v1",
                     task_type=journey.TASK_TYPE, agent="codex-cli", model="m",
                     condition=condition, trial=1, passed=True, score=1.0,
                     started_at="2026-11-17T00:00:00+00:00",
                     ended_at="2026-11-17T00:01:00+00:00", wall_time_sec=60.0,
                     exit_code=0, metrics={"app_id": "demo"})


def test_board_rows_of_memory_conditions_are_internal():
    rows = journey.summary([_result("mcp"), _result("mcp+mem-static-small-none")])
    by = {r["condition"]: r for r in rows}
    assert by["mcp+mem-static-small-none"]["internal"] is True
    assert "internal" not in by["mcp"]
    assert [r["condition"] for r in leaderboard.public_rows(rows)] == ["mcp"]
    hunt = leaderboard.mark_internal([{"condition": "raw"},
                                      {"condition": "raw+mem-dynamic-small-diff"}])
    assert hunt == [{"condition": "raw"},
                    {"condition": "raw+mem-dynamic-small-diff", "internal": True}]


# ── the CLI gate ─────────────────────────────────────────────────────────────

def _values(**kw):
    base = {"mode": "none", "budget": "small", "diff": "none", "snapshot": None, "org": None}
    base.update(kw)
    return base


def test_cli_gate_memory(tmp_path, memory_root):
    assert cli._gate_memory("codex-cli", "journey", None, memory_config=None,
                            values=_values()) is None
    arm = cli._gate_memory("claude-code", "journey", None, memory_config=None,
                           values=_values(mode="static", snapshot=memory_root))
    assert arm.mode == "static" and arm.snapshot == memory_root
    with pytest.raises(click.ClickException, match="snapshot"):
        cli._gate_memory("codex-cli", "journey", None, memory_config=None,
                         values=_values(mode="dynamic"))
    with pytest.raises(click.ClickException, match="native"):
        cli._gate_memory("native", "journey", None, memory_config=None,
                         values=_values(mode="static", snapshot=memory_root))
    with pytest.raises(click.ClickException, match="create"):
        cli._gate_memory("codex-cli", "create", None, memory_config=None,
                         values=_values(mode="static", snapshot=memory_root))
    with pytest.raises(click.ClickException, match="root missing"):
        cli._gate_memory("codex-cli", "journey", None, memory_config=None,
                         values=_values(mode="static", snapshot=tmp_path / "nope"))
    cfg = tmp_path / "memory.json"
    cfg.write_text(json.dumps({"mode": "dynamic", "budget": "index",
                               "snapshot": str(memory_root)}))
    arm = cli._gate_memory("codex-cli", "journey", None, memory_config=cfg, values=_values())
    assert (arm.mode, arm.budget) == ("dynamic", "index")
    cli._gate_memory_apps(arm, [APP])
    with pytest.raises(click.ClickException, match="other-app"):
        cli._gate_memory_apps(arm, [APP, "other-app"])


def test_cli_gate_memory_resume_reads_the_plan(memory_root):
    class _Plan:
        def __init__(self):
            self.raw = {"memory_arm": marm.MemoryArm("static", "large",
                                                     snapshot=memory_root).as_dict()}
    arm = cli._gate_memory("codex-cli", "journey", _Plan(), memory_config=None,
                           values=_values())
    assert (arm.mode, arm.budget, arm.snapshot) == ("static", "large", memory_root)
    # And the memory flags are scope flags, refused beside --resume.
    for flag in ("memory_mode", "memory_budget", "memory_diff", "memory_snapshot",
                 "memory_org", "memory_config"):
        assert flag in cli._SCOPE_FLAGS
