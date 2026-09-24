"""The agent cannot see which ARM of a journey case it is on (QUA-2806).

A journey task id is `<case>~seeded` or `<case>~clean`, and it used to name the
episode's directory twice — the agent's cwd, echoed in every claude-code Bash result,
codex's `--cd`, CLAUDE_CONFIG_DIR / CODEX_HOME / HOME, the budget hook's paths, the
findings file's path and the episode marker beside the workspace. The app-data
snapshot beside it also carried the seeded flags file. Everything the agent can see
or reach now names the case and an opaque episode id only; the version lives
harness-side. No test here reaches a device.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
from pathlib import Path

import pytest

from qualgentbench import checkpoint
from qualgentbench import episode_runner as er
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from test_agent_dump import SERIAL, _episode, device  # noqa: F401 — the fake-device fixture

CASE = "demo-open-list"
# Captured at import, before any test's monkeypatch replaces it with a stub.
_REAL_SNAPSHOTS = er.take_replay_snapshots
# The arm label in any form an agent could read it in: `~seeded`, `seeded`, `Clean`…
LABEL = re.compile(r"seeded|clean", re.IGNORECASE)


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in ("./", "./files/", "./files/.qgb/", "./databases/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _sandbox(version: str) -> dict[str, bytes]:
    """The app's private data as `write_bug_flags` leaves it for one arm — the padded,
    fixed-shape flag file (QUA-2814), with a per-arm nonce like the real thing."""
    nonce = "QGB-NONCE-" + ("ab" if version == "seeded" else "cd") * 16
    lines = er.flag_file_lines(nonce, ["list-row-dead"] if version == "seeded" else [])
    return {"./files/qgb_flags.txt": ("\n".join(lines) + "\n").encode(),
            "./files/.qgb/nonce": nonce.encode(),
            "./databases/app.db": b"SQLite format 3\x00" + b"\x00" * 64}


class _Capture:
    """Wraps a REAL adapter: runs its `prepare`, `command` and `env` exactly as
    `AgentAdapter.run` would, then records every string the agent could see before
    its first tool call — its cwd, argv, the env the harness set, the findings path,
    and every file (name and text) in the episode directory it is launched in — and
    exits without a model."""

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name
        self.seen: dict[str, str] = {}

    async def run(self, instruction, context):
        self.inner.prepare(context)
        cmd = self.inner.command(instruction, context)
        overrides = {**self.inner.env(context), **(context.agent_env or {})}
        seen = self.seen
        seen["instruction"] = instruction
        seen["cwd"] = str(context.workspace_dir)          # what `pwd` echoes in Bash
        seen["realpath(cwd)"] = os.path.realpath(context.workspace_dir)
        seen["findings path"] = str(context.workspace_dir / "findings.yaml")
        seen["argv"] = "\n".join(cmd)
        # Only what the HARNESS set: the rest is the test process's own environment.
        for k, v in overrides.items():
            if os.environ.get(k) != v:
                seen[f"env {k}"] = f"{k}={v}"
        # Everything reachable beside the cwd: names, text contents, tar members.
        for path in sorted(context.run_dir.rglob("*")):
            rel = str(path.relative_to(context.run_dir))
            seen[f"name {rel}"] = str(path)
            if not path.is_file():
                continue
            if path.suffix == ".tar":
                with tarfile.open(path) as tf:
                    for m in tf.getmembers():
                        body = tf.extractfile(m).read().decode("latin-1") if m.isfile() else ""
                        seen[f"tar {rel}:{m.name}"] = f"{m.name} {m.size} {body}"
                continue
            seen[f"file {rel}"] = path.read_text(errors="replace")
        return "", 0


def _arm_episode(monkeypatch, tmp_path, agent_cls, version: str, tooling: str):
    task, opts, _probe = _episode(monkeypatch, tmp_path, [])
    task.id = f"{CASE}~{version}"
    task.bug_spec.update({"version": version, "case_id": CASE, "name": "Open the list",
                          "steps": ["Open the list"], "expected_outcome": "It opens.",
                          "active_bugs": ["list-row-dead"] if version == "seeded" else []})
    opts.agent = agent_cls.name
    capture = _Capture(agent_cls())
    monkeypatch.setattr(er, "get_adapter", lambda name: capture)
    monkeypatch.setattr(CodexCliAdapter, "_seed_account_auth", classmethod(lambda cls, h: None))
    monkeypatch.setattr(er, "_ablation_instruction",
                        lambda task, serial, tooling: f"Test case: Open the list on {serial}.")

    # The REAL snapshot step, with the device's tar answered from the arm's sandbox.
    async def fake_snapshot(serial, bundle, path):
        Path(path).write_bytes(_tar(_sandbox(version)))
        return True

    async def noop(*_a, **_kw):
        return None

    async def adb(*_a, **_kw):
        return 0, ""

    from qualgentbench.verify import canary
    monkeypatch.setattr(er, "take_replay_snapshots", _REAL_SNAPSHOTS)
    monkeypatch.setattr(er, "replay_snapshot", fake_snapshot)
    monkeypatch.setattr(er, "_relaunch_app", noop)
    monkeypatch.setattr(er, "wait_stable", noop)
    monkeypatch.setattr(er, "_adb", adb)
    monkeypatch.setattr(canary, "clear_fired", noop)

    if tooling == "mcp":
        opts.mcp_server = "http://127.0.0.1:51831"

        class _NoMeter:
            def __init__(self, *_a, **_kw): pass
            async def start(self, *_a, **_kw): return 0
            async def stop(self): return None
            def url(self, _path): return "http://127.0.0.1:40001"

        async def identity(url, **_kw):
            return {"name": "devloop-mcp", "version": "1.27.0", "app_source": "none",
                    "instructions_sha256": "a" * 64, "tools_sha256": "b" * 64, "tools": 90}

        async def isolation(url, **_kw):
            return {"isolation": "per_mcp_session", "sessions": [], "clean": True,
                    "artifact_roots": []}

        monkeypatch.setattr(er, "McpMeter", _NoMeter)
        monkeypatch.setattr(er, "fetch_server_identity", identity)
        monkeypatch.setattr(er, "fetch_episode_isolation", isolation)
    return task, opts, capture


@pytest.mark.parametrize("tooling", ["raw", "mcp"])
@pytest.mark.parametrize("version", ["seeded", "clean"])
@pytest.mark.parametrize("agent_cls", [ClaudeCodeAdapter, CodexCliAdapter],
                         ids=["claude-code", "codex-cli"])
async def test_no_agent_visible_string_carries_the_arm(device, monkeypatch, tmp_path,  # noqa: F811
                                                      agent_cls, version, tooling):
    device(u2=False)
    task, opts, capture = _arm_episode(monkeypatch, tmp_path, agent_cls, version, tooling)
    result = await er.run_episode(task, opts)

    seen = capture.seen
    assert seen, "the agent was never launched"
    # The surfaces the ticket names are all in the scan.
    for key in ("cwd", "findings path", "argv", "file episode.json", "file mcp_config.json"):
        assert key in seen, key
    if agent_cls is ClaudeCodeAdapter:
        assert "env CLAUDE_CONFIG_DIR" in seen and "file hooks/settings.json" in seen
        assert "file hooks/tool_cap.py" in seen
    else:
        assert "env CODEX_HOME" in seen and "env HOME" in seen
        assert "file codex_home/config.toml" in seen and "file codex_home/hooks/tool_cap.py" in seen
    assert any(k.startswith("tar app_snapshot.tar:") for k in seen)
    leaks = {k: LABEL.findall(v) for k, v in seen.items() if LABEL.search(v)}
    assert not leaks, leaks

    # The harness still knows: result.json and the marker read back through the index.
    assert result.task_id == f"{CASE}~{version}"
    run_dir = Path(opts.runs_dir) / result.artifact_dir
    assert checkpoint.read_episode_marker(run_dir)["task_id"] == f"{CASE}~{version}"
    assert result.provenance["episode_id"] in run_dir.name


def _normalised(seen: dict[str, str], run_dir: Path) -> dict[str, str]:
    """What differs between ANY two episodes and says nothing about the arm: the
    episode's own directory, its opaque id, timestamps, and the meter's port."""
    subs = [(re.compile(re.escape(str(run_dir))), "<RUN>"),
            (re.compile(re.escape(os.path.realpath(run_dir))), "<RUN>"),
            (re.compile(r"ep-[0-9a-f]{12}"), "<EP>"),
            (re.compile(r"\d{4}-\d\d-\d\dT[\d:.+\-]+Z?"), "<TS>"),
            (re.compile(r"(ANDROID_ADB_SERVER_PORT=)\d+"), r"\1<PORT>")]
    out = {}
    for k, v in seen.items():
        for rx, rep in subs:
            k, v = rx.sub(rep, k), rx.sub(rep, v)
        out[k] = v
    return out


@pytest.mark.parametrize("tooling", ["raw", "mcp"])
@pytest.mark.parametrize("agent_cls", [ClaudeCodeAdapter, CodexCliAdapter],
                         ids=["claude-code", "codex-cli"])
async def test_the_two_arms_look_identical_to_the_agent(device, monkeypatch, tmp_path,  # noqa: F811
                                                       agent_cls, tooling):
    """Stronger than the label scan: nothing the agent can see differs between the arms
    at all, once the episode's own path, id, timestamps and meter port are set aside.
    The seeded flags file inside the app-data snapshot carries no label WORD, but its
    content and size told the arms apart (`strip_flag_files`)."""
    device(u2=False)
    views = {}
    for version in ("seeded", "clean"):
        task, opts, capture = _arm_episode(monkeypatch, tmp_path, agent_cls, version, tooling)
        result = await er.run_episode(task, opts)
        views[version] = _normalised(capture.seen, Path(opts.runs_dir) / result.artifact_dir)
    assert views["seeded"].keys() == views["clean"].keys()
    differ = {k: (views["seeded"][k], views["clean"][k]) for k in views["seeded"]
              if views["seeded"][k] != views["clean"][k]}
    assert not differ, differ


async def test_both_arms_share_the_case_dir_and_differ_only_by_opaque_id(device, monkeypatch,  # noqa: F811
                                                                           tmp_path):
    device(u2=False)
    dirs = {}
    for version in ("seeded", "clean"):
        task, opts, _ = _arm_episode(monkeypatch, tmp_path, ClaudeCodeAdapter, version, "raw")
        result = await er.run_episode(task, opts)
        dirs[version] = Path(opts.runs_dir) / result.artifact_dir
    seeded, clean = dirs["seeded"], dirs["clean"]
    assert seeded.parent == clean.parent and seeded.parent.name == CASE
    assert seeded.name != clean.name
    strip = re.compile(r"^\S+?Z_|_ep-[0-9a-f]{12}$")
    assert strip.sub("", seeded.name) == strip.sub("", clean.name)
    # The harness-side index is outside every episode dir, under the run's meta dir.
    for version, d in dirs.items():
        marker = json.loads((d / "episode.json").read_text())
        assert marker["task_id"] == CASE and marker["blinded"] is True
        index = checkpoint.episode_index_path(d.parent.parent, marker["run_id"],
                                              marker["episode_id"])
        assert json.loads(index.read_text())["task_id"] == f"{CASE}~{version}"
        assert d not in index.parents


def test_the_snapshot_loses_the_flag_files_and_nothing_else(tmp_path):
    snap = tmp_path / "app_snapshot.tar"
    snap.write_bytes(_tar(_sandbox("seeded")))
    removed = er.strip_flag_files(snap)
    assert sorted(removed) == ["./files/.qgb/nonce", "./files/qgb_flags.txt"]
    with tarfile.open(snap) as tf:
        names = tf.getnames()
        assert "./databases/app.db" in names
        assert any(n.rstrip("/") == "./files/.qgb" for n in names)   # the dir stays
        assert tf.extractfile("./databases/app.db").read().startswith(b"SQLite format 3")
        assert not any("qgb_flags" in n or n.endswith("/nonce") for n in names)
    # Idempotent, and a tar that holds neither file is left byte-for-byte alone.
    before = snap.read_bytes()
    assert er.strip_flag_files(snap) == []
    assert snap.read_bytes() == before


def test_an_unreadable_snapshot_is_left_alone(tmp_path):
    snap = tmp_path / "app_snapshot.tar"
    snap.write_bytes(b"not a tar at all" * 64)
    assert er.strip_flag_files(snap) == []
    assert snap.read_bytes() == b"not a tar at all" * 64


def test_the_live_flag_file_metadata_is_identical_across_arms():
    """Beyond the snapshot (which is stripped): the LIVE qgb_flags.txt a rooted agent
    could `wc`/`stat` has the same line count and byte size on both arms, so its
    metadata never reveals the arm (QUA-2814). Content differs (real ids vs padding),
    which is what the nonce backstop and the meter's run-as/su/root denials guard."""
    seeded = _sandbox("seeded")["./files/qgb_flags.txt"]
    clean = _sandbox("clean")["./files/qgb_flags.txt"]
    assert seeded.count(b"\n") == clean.count(b"\n")        # wc -l
    assert len(seeded) == len(clean)                        # wc -c / stat size
    assert b"list-row-dead" in seeded and b"list-row-dead" not in clean


@pytest.mark.parametrize("tid, visible", [
    ("cal-open~seeded", "cal-open"), ("cal-open~clean", "cal-open"),
    ("explore-birday", "explore-birday"),          # hunt: no arm to hide
    ("cal-open~other", "cal-open~other"),          # not a version label
])
def test_agent_visible_task_id(tid, visible):
    assert er.agent_visible_task_id(tid) == visible


def test_episode_ids_are_opaque_and_unique():
    ids = {er.new_episode_id() for _ in range(200)}
    assert len(ids) == 200 and all(re.fullmatch(r"ep-[0-9a-f]{12}", i) for i in ids)


# ── the old layout still loads ─────────────────────────────────────────────────

def _old_episode(runs: Path, task_id: str, run_id: str, *, result: bool) -> Path:
    ep = runs / task_id / f"2026-09-23T00-00-00Z_{task_id}_claude-code_m_mcp_trial-1"
    (ep / "workspace").mkdir(parents=True)
    checkpoint.write_episode_marker(ep, run_id=run_id, app_id="app", task_id=task_id,
                                    kind="journey_case", trial=1)
    if result:
        (ep / "result.json").write_text(json.dumps(
            {"run_id": run_id, "task_id": task_id, "trial": 1, "metrics": {}}))
    return ep


def test_a_resume_reads_old_and_blinded_episodes_side_by_side(tmp_path):
    runs = tmp_path / "runs"
    _old_episode(runs, f"{CASE}~seeded", "r1", result=True)            # before QUA-2806
    new = runs / CASE / "2026-09-24T00-00-00Z_demo_claude-code_m_mcp_trial-1_ep-0123456789ab"
    (new / "workspace").mkdir(parents=True)
    checkpoint.write_blinded_marker(new, runs, visible_task_id=CASE, run_id="r1",
                                    app_id="app", task_id=f"{CASE}~clean",
                                    kind="journey_case", trial=1,
                                    episode_id="ep-0123456789ab")      # killed: an orphan
    state = checkpoint.state(runs, "r1")
    assert [ref.key for ref in state.done] == [("app", f"{CASE}~seeded", 1)]
    assert [ref.key for ref in state.orphans] == [("app", f"{CASE}~clean", 1)]
    # The old marker reads exactly as before.
    old = checkpoint.read_episode_marker(runs / f"{CASE}~seeded" / next(
        p.name for p in (runs / f"{CASE}~seeded").iterdir()))
    assert old["task_id"] == f"{CASE}~seeded" and "blinded" not in old


def test_a_blinded_marker_without_its_index_stays_blinded(tmp_path):
    """A bundle carries episode dirs, not the run's episode index: the marker then
    names the case, and result.json (which a bundle does carry) names the version."""
    runs = tmp_path / "runs"
    ep = runs / CASE / "x_ep-0123456789ab"
    ep.mkdir(parents=True)
    checkpoint.write_blinded_marker(ep, runs, visible_task_id=CASE, run_id="r1", app_id="a",
                                    task_id=f"{CASE}~seeded", kind="journey_case", trial=1,
                                    episode_id="ep-0123456789ab")
    for idx in (runs / "_runs").rglob("*.json"):
        idx.unlink()
    assert checkpoint.read_episode_marker(ep)["task_id"] == CASE
