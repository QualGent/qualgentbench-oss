"""Which software scored an episode (QUA-2928): the harness build and the agent CLI.

* `checkpoint.harness_identity()` — package version, git sha, dirty flag — is recorded
  in plan.json's environment and in every episode's provenance. The sha is read only
  from a checkout that holds THIS package, never fails off one, and never carries a path.
* A resume across commits is not refused: `compatibility` ignores `harness`, and the
  export → import → resume hand-off behaves exactly as it did before the stamp.
* claude-code's version comes off its stream-json `init` event; codex-cli's from
  `codex --version`, once per run. Anything that is not version-shaped is dropped.
* The system image (`api_level`, `build_id`, `abi`) is read at the agent hand-off.
* The view lists the agent-CLI and harness lanes once episodes carry them.

Nothing here reaches a device or a model; the CLIs are fakes on PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from qualgentbench import checkpoint, preflight, view
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.claude_code import ClaudeCodeAdapter
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.result import RunResult
from qualgentbench.schemas import Condition
from qualgentbench.transcript import claude_code_version

SHA_A, SHA_B = "a" * 40, "b" * 40
_REAL_GIT_STATE = checkpoint._git_state


@pytest.fixture(autouse=True)
def _fresh_git_state():
    _REAL_GIT_STATE.cache_clear()
    yield
    _REAL_GIT_STATE.cache_clear()


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env,
                          check=True, capture_output=True, text=True).stdout.strip()


def _fake_package(monkeypatch, root: Path, rel: str) -> Path:
    """Point `_git_state` at a package dir `root/rel` (as if checkpoint.py lived there)."""
    pkg = root / rel
    pkg.mkdir(parents=True)
    (pkg / "checkpoint.py").write_text("# stand-in\n")
    monkeypatch.setattr(checkpoint, "__file__", str(pkg / "checkpoint.py"))
    return pkg


# ── the harness identity ──────────────────────────────────────────────────────


def test_a_checkout_of_this_package_reports_its_sha_and_dirty_flag(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    pkg = _fake_package(monkeypatch, repo, "src/qualgentbench")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c")
    head = _git(repo, "rev-parse", "HEAD")

    ident = checkpoint.harness_identity()
    assert ident == {"package_version": checkpoint.package_version(), "git_sha": head,
                     "git_dirty": False}
    # An untracked file (a runs dir, a scratch note) is not an edit to the harness...
    checkpoint._git_state.cache_clear()
    (repo / "runs").mkdir()
    (repo / "runs" / "result.json").write_text("{}")
    assert checkpoint.harness_identity()["git_dirty"] is False
    # ...an edited tracked file is.
    checkpoint._git_state.cache_clear()
    (pkg / "checkpoint.py").write_text("# edited\n")
    assert checkpoint.harness_identity() == {**ident, "git_dirty": True}


def test_off_a_checkout_the_sha_is_none_and_nothing_fails(tmp_path, monkeypatch):
    """An installed wheel: no `.git` anywhere above the package."""
    _fake_package(monkeypatch, tmp_path / "site-packages", "qualgentbench")
    assert checkpoint.harness_identity() == {
        "package_version": checkpoint.package_version(), "git_sha": None,
        "git_dirty": None}


def test_a_wheel_inside_someone_elses_repo_does_not_report_that_repo(tmp_path, monkeypatch):
    """A virtualenv inside another project's checkout: `git rev-parse` would answer with
    THAT project's commit. Only a top level holding `src/qualgentbench` counts."""
    other = tmp_path / "their-app"
    _fake_package(monkeypatch, other, ".venv/lib/python3.12/site-packages/qualgentbench")
    _git(other, "init", "-q")
    _git(other, "add", "-f", ".")
    _git(other, "commit", "-q", "-m", "theirs")
    assert checkpoint.harness_identity()["git_sha"] is None


def test_no_git_binary_is_not_an_error(tmp_path, monkeypatch):
    _fake_package(monkeypatch, tmp_path, "src/qualgentbench")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert checkpoint.harness_identity()["git_sha"] is None


def test_a_git_dir_in_the_environment_cannot_redirect_the_read(tmp_path, monkeypatch):
    """Inside a git hook GIT_DIR points at the repo being committed to."""
    repo = tmp_path / "repo"
    _fake_package(monkeypatch, repo, "src/qualgentbench")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    assert checkpoint.harness_identity()["git_sha"] == _git(repo, "rev-parse", "HEAD")


def test_the_identity_never_carries_a_path():
    """The real checkout this suite runs from: a sha and a flag, never where it lives."""
    ident = checkpoint.harness_identity()
    assert set(ident) == {"package_version", "git_sha", "git_dirty"}
    blob = json.dumps(ident)
    assert "/" not in blob and "\\" not in blob
    assert str(Path.home()) not in blob
    if ident["git_sha"] is not None:
        assert len(ident["git_sha"]) in (40, 64) and isinstance(ident["git_dirty"], bool)


# ── resume: recorded, never compared ──────────────────────────────────────────


def _stub_git(monkeypatch, sha, dirty):
    monkeypatch.setattr(checkpoint, "_git_state", lambda: (sha, dirty))


def test_the_fingerprint_records_the_harness(monkeypatch):
    _stub_git(monkeypatch, SHA_A, False)
    fp = checkpoint.environment_fingerprint([])
    assert fp["harness"] == {"package_version": fp["package_version"], "git_sha": SHA_A,
                             "git_dirty": False}


def test_compatibility_ignores_the_git_sha_and_dirty_flag(monkeypatch):
    """Two teammates a commit apart, one with local edits, finish one run."""
    _stub_git(monkeypatch, SHA_A, False)
    planned = checkpoint.environment_fingerprint([])
    _stub_git(monkeypatch, SHA_B, True)
    now = checkpoint.environment_fingerprint([])
    assert planned["harness"] != now["harness"]
    assert checkpoint.compatibility(planned, now) == []
    # A plan from before the stamp has no `harness` at all: still the same benchmark.
    old = {k: v for k, v in planned.items() if k != "harness"}
    assert checkpoint.compatibility(old, now) == []
    # The package version is still the gate it was.
    moved = {**now, "package_version": "9.9.9",
             "harness": {**now["harness"], "package_version": "9.9.9"}}
    assert checkpoint.compatibility(planned, moved) == [
        f"package_version: plan {planned['package_version']} → now 9.9.9"]


async def test_export_import_resume_is_unchanged_by_the_stamp(tmp_path, monkeypatch):
    """The hand-off end to end, twice: once from a plan written before the stamp and
    once from a plan stamped on machine A's commit, resumed on machine B from another
    commit with local edits. The bundle carries the same members with the same hashes
    (plan.json alone differs, by its `harness` key), the manifest has the same keys and
    no harness, and both resumes schedule exactly the units A did not finish."""
    import test_checkpoint_handoff as h

    # Machine A, as the hand-off suite builds it, stamped on commit A...
    _stub_git(monkeypatch, SHA_A, False)
    stamped_a = tmp_path / "stamped" / "machine-a" / "runs"
    from qualgentbench.cli import _write_plan
    from qualgentbench.scheduler import Unit, plan_summary
    units = [Unit("birday", "Birday", t, "bug_task", t, 1, 180.0, "default") for t in h.TASKS]
    _write_plan(stamped_a, h.RUN_ID, plan_summary(units, 1), apps=[h.SUITE], mode="guided",
                agent="claude-code", model="anthropic/claude-opus-4-8",
                devices=["emulator-5554"], trials=1)
    for task in h.FINISHED_ON_A:
        h._finished_episode(stamped_a, task)
    h._interrupted_episode(stamped_a, h.INTERRUPTED_ON_A)
    plan_path = checkpoint.plan_path(stamped_a, h.RUN_ID)
    plan = json.loads(plan_path.read_text())
    assert plan["environment"]["harness"]["git_sha"] == SHA_A

    # ...and the same run as a harness without the stamp would have written it.
    legacy_a = tmp_path / "legacy" / "machine-a" / "runs"
    shutil.copytree(stamped_a, legacy_a)
    legacy_plan = json.loads(plan_path.read_text())
    del legacy_plan["environment"]["harness"]
    checkpoint.write_json(checkpoint.plan_path(legacy_a, h.RUN_ID), legacy_plan)

    exported = {}
    for name, runs in (("stamped", stamped_a), ("legacy", legacy_a)):
        out = tmp_path / name / "transfer"
        out.mkdir()
        exported[name] = checkpoint.export_bundle(runs, h.RUN_ID, output=out)

    s, lg = exported["stamped"].manifest, exported["legacy"].manifest
    assert set(s) == set(lg) and "harness" not in s
    assert "harness" not in json.dumps({k: v for k, v in s.items() if k != "files"})
    volatile = {"created_at", "host", "files"}
    assert {k: v for k, v in s.items() if k not in volatile} \
        == {k: v for k, v in lg.items() if k not in volatile}
    assert h._archive_names(exported["stamped"].path) \
        == h._archive_names(exported["legacy"].path)
    plan_member = f"_runs/{h.RUN_ID}/plan.json"
    files_s = {f["path"]: f for f in s["files"]}
    files_l = {f["path"]: f for f in lg["files"]}
    assert files_s.keys() == files_l.keys()
    assert [p for p in files_s if files_s[p] != files_l[p]] == [plan_member]
    assert exported["stamped"].discarded == exported["legacy"].discarded

    # Machine B: another commit, with local edits. Neither resume is refused, and both
    # run exactly what A owed.
    _stub_git(monkeypatch, SHA_B, True)
    for name in ("stamped", "legacy"):
        machine_b = tmp_path / name / "machine-b" / "runs"
        machine_b.mkdir(parents=True)
        imported = checkpoint.import_bundle(exported[name].path, machine_b)
        assert imported.run_id == h.RUN_ID and len(imported.episodes) == 3
        engine = h._FakeLanes(machine_b)
        h._stub_machine_b(monkeypatch, tmp_path / name, engine)
        produced = await h._resume_on(machine_b, h.RUN_ID)
        assert engine.seen == sorted(("birday", t, 1) for t in h.OWED_TO_B), name
        assert (engine.cfg.run_id, engine.cfg.segment) == (h.RUN_ID, 1)
        assert len(produced) == len(h.OWED_TO_B)
        plan_b = json.loads(checkpoint.plan_path(machine_b, h.RUN_ID).read_text())
        # The plan keeps the harness it was planned with; a resume does not restamp it.
        assert plan_b["environment"].get("harness") == (
            plan["environment"]["harness"] if name == "stamped" else None)
        assert checkpoint.run_summary(machine_b, h.RUN_ID)["counts"]["remaining"] == 0


# ── the agent CLI version ─────────────────────────────────────────────────────


def _init(version) -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": "s",
                       "claude_code_version": version, "tools": []})


def test_claude_code_version_comes_off_the_init_event():
    transcript = "\n".join([_init("2.1.281"),
                            json.dumps({"type": "assistant", "message": {"content": []}}),
                            _init("9.9.9")])
    assert claude_code_version(transcript) == "2.1.281"
    assert claude_code_version("") is None
    assert claude_code_version(json.dumps({"type": "result", "claude_code_version": "1.0.0"})) \
        is None
    # Not version-shaped: dropped, never recorded.
    assert claude_code_version(_init("/Users/someone/.local/bin/claude")) is None
    assert claude_code_version(_init(None)) is None
    assert claude_code_version(_init("2.1.0-beta.3")) == "2.1.0-beta.3"


def _context(run_dir: Path, agent: str, meta: Path | None = None) -> RunContext:
    return RunContext(  # type: ignore[arg-type]
        task=SimpleNamespace(agent=SimpleNamespace(timeout_sec=900)),
        agent=agent, model="m", condition=Condition.no_routines, trial=1, run_dir=run_dir,
        mcp_server="", mcp_config_path=run_dir / "mcp.json",
        workspace_dir=run_dir / "workspace", disabled_tools=[], inject_mcp=False,
        run_meta_dir=meta)


def _fake_cli(tmp_path: Path, name: str, body: str) -> Path:
    """An executable `name` on a PATH of its own; every invocation appends its argv to
    `<bin>/calls`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(f'#!/bin/sh\necho "$@" >> "{bindir}/calls"\n{body}\n')
    script.chmod(0o755)
    return bindir


def test_the_claude_code_adapter_reports_the_version_its_cli_printed(tmp_path, monkeypatch):
    """The fake `claude` prints a stream-json transcript whose init names a version."""
    stream = "\n".join([_init("2.1.281"),
                        json.dumps({"type": "result", "subtype": "success",
                                    "usage": {"input_tokens": 1, "output_tokens": 1}})])
    (tmp_path / "stream.jsonl").write_text(stream + "\n")
    bindir = _fake_cli(tmp_path, "claude", f'cat >/dev/null; cat "{tmp_path}/stream.jsonl"')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    ctx = _context(tmp_path / "run", "claude-code")
    ctx.run_dir.mkdir()
    ctx.workspace_dir.mkdir()
    transcript, code = asyncio.run(ClaudeCodeAdapter().run("do it", ctx))
    assert code == 0 and transcript.startswith(_init("2.1.281"))
    assert ctx.agent_cli_version == "2.1.281"


def test_the_claude_code_adapter_says_nothing_when_the_cli_died_before_init(
        tmp_path, monkeypatch):
    bindir = _fake_cli(tmp_path, "claude", "cat >/dev/null; echo 'boom'; exit 1")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    ctx = _context(tmp_path / "run", "claude-code")
    ctx.run_dir.mkdir()
    ctx.workspace_dir.mkdir()
    asyncio.run(ClaudeCodeAdapter().run("do it", ctx))
    assert ctx.agent_cli_version is None


@pytest.fixture
def codex_on_path(tmp_path, monkeypatch):
    def install(body: str) -> Path:
        bindir = _fake_cli(tmp_path, "codex", body)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(CodexCliAdapter, "_VERSION_CACHE", {})
        monkeypatch.setattr(CodexCliAdapter, "_seed_account_auth",
                            classmethod(lambda cls, home: "api_key"))
        return bindir / "calls"
    return install


def test_codex_prepare_asks_codex_version_once_per_run(tmp_path, codex_on_path):
    calls = codex_on_path('[ "$1" = "--version" ] && echo "codex-cli 0.156.1"')
    meta = tmp_path / "_runs" / "r1"
    for trial in (1, 2):
        ctx = _context(tmp_path / f"ep{trial}", "codex-cli", meta)
        CodexCliAdapter().prepare(ctx)
        assert ctx.agent_cli_version == "0.156.1"
    assert calls.read_text().splitlines() == ["--version"]
    # Another run asks again: the binary may have been upgraded in between.
    ctx = _context(tmp_path / "ep3", "codex-cli", tmp_path / "_runs" / "r2")
    CodexCliAdapter().prepare(ctx)
    assert calls.read_text().splitlines() == ["--version", "--version"]


@pytest.mark.parametrize("body", ['echo "codex at /opt/somewhere/bin/codex"', "exit 3",
                                  'echo ""'])
def test_codex_without_a_version_shaped_answer_reports_none(tmp_path, codex_on_path, body):
    codex_on_path(body)
    ctx = _context(tmp_path / "ep", "codex-cli", tmp_path / "_runs" / "r")
    CodexCliAdapter().prepare(ctx)
    assert ctx.agent_cli_version is None


def test_codex_not_installed_reports_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CodexCliAdapter, "_VERSION_CACHE", {})
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert CodexCliAdapter.cli_version(_context(tmp_path, "codex-cli")) is None


# ── the episode's provenance ──────────────────────────────────────────────────


async def test_provenance_carries_the_harness_cli_version_and_device_image(monkeypatch):
    from qualgentbench import episode_runner as er

    async def no_avd(serial):
        return None

    monkeypatch.setattr(er, "_avd_name", no_avd)
    _stub_git(monkeypatch, SHA_A, True)
    opts = er.EpisodeOptions(agent="codex-cli", model="m", condition=Condition.no_routines,
                             trial=1, mcp_server="", runs_dir=Path("runs"))
    image = {"api_level": 35, "build_id": "AP3A.240905.015", "abi": "arm64-v8a"}
    prov = await er._provenance(opts, "emulator-5554", handoff={"device_image": image},
                                agent_cli_version="0.156.1")
    assert prov["agent_cli_version"] == "0.156.1"
    assert prov["harness"] == {"package_version": checkpoint.package_version(),
                               "git_sha": SHA_A, "git_dirty": True}
    assert prov["device_image"] == image
    # Nothing reported, no hand-off read: `unknown` and None, never a missing key.
    bare = await er._provenance(opts, "emulator-5554")
    assert bare["agent_cli_version"] == "unknown" and bare["device_image"] is None


def test_device_image_parses_the_three_props():
    assert preflight.device_image("35\n", "AP3A.240905.015\n", "arm64-v8a") == {
        "api_level": 35, "build_id": "AP3A.240905.015", "abi": "arm64-v8a"}
    assert preflight.device_image("", "", "") is None
    assert preflight.device_image("30", "error: device offline", "x86_64") == {
        "api_level": 30, "build_id": None, "abi": "x86_64"}


async def test_the_hand_off_read_returns_the_image_and_the_start_read_does_not(monkeypatch):
    from qualgentbench.verify import device as vdevice

    props = {"ro.build.version.sdk": b"35\n", "ro.build.id": b"AP3A.240905.015\n",
             "ro.product.cpu.abi": b"x86_64\n"}
    calls: list[str] = []

    async def adb(serial, *args):
        calls.append(" ".join(args))
        if args[:2] == ("shell", "getprop"):
            return 0, props[args[2]]
        if args[:2] == ("shell", "settings"):
            return 0, b"0"
        if args[:2] == ("shell", "id"):
            return 0, b"2000"
        return 0, b""

    async def no_u2(serial):
        return []

    monkeypatch.setattr(vdevice, "_adb", adb)
    monkeypatch.setattr(vdevice, "_running_u2_servers", no_u2)
    seen: dict = {}
    assert await preflight.device_state_violations("s", expect_launcher=False,
                                                   observed=seen) == []
    assert seen == {"device_image": {"api_level": 35, "build_id": "AP3A.240905.015",
                                     "abi": "x86_64"}}
    calls.clear()
    await preflight.device_state_violations("s", expect_launcher=False)
    assert not [c for c in calls if "getprop" in c]


# ── the view: agent-CLI and harness lanes ─────────────────────────────────────


def _result(agent: str, prov: dict) -> RunResult:
    t0 = datetime(2026, 9, 25, tzinfo=timezone.utc).isoformat()
    return RunResult(task_id="c~clean", task_version="v", task_type="journey", agent=agent,
                     model="m", condition="raw", trial=1, passed=False, score=0.0,
                     started_at=t0, ended_at=t0, wall_time_sec=1.0, exit_code=0,
                     provenance=prov)


def test_lane_labels():
    assert view._agent_cli(_result("claude-code", {})) == "claude-code unstamped"
    assert view._agent_cli(_result("claude-code", {"agent_cli_version": "unknown"})) \
        == "claude-code unknown"
    assert view._agent_cli(_result("codex-cli", {"agent_cli_version": "0.156.1"})) \
        == "codex-cli 0.156.1"
    assert view._harness(_result("a", {})) == "unstamped"
    h = {"package_version": "0.2.0", "git_sha": "0123456789abcdef" * 2 + "01234567",
         "git_dirty": False}
    assert view._harness(_result("a", {"harness": h})) == "0.2.0+0123456789ab"
    assert view._harness(_result("a", {"harness": {**h, "git_dirty": True}})) \
        == "0.2.0+0123456789ab-dirty"
    assert view._harness(_result("a", {"harness": {**h, "git_sha": None,
                                                   "git_dirty": None}})) == "0.2.0"


def test_the_manifest_and_the_versions_line_name_the_lanes(tmp_path):
    import test_view as tv

    runs = tv._stamped(tmp_path / "runs")
    plain = tv._build(runs)
    v = tv._run_entry(plain)["versions"]
    # Before the stamp: listed as unstamped, and no chip on the page.
    assert v["agent_cli_versions"] == ["codex-cli unstamped"]
    assert v["harness_versions"] == ["unstamped"]
    line = tv._versions_line(plain.index.read_text())
    assert "agent CLI" not in line and "harness" not in line

    # Stamp the episodes: two CLI versions and two harness builds in one run are lanes,
    # never `mixed`, and the set key does not move.
    harness = {"package_version": "0.2.0", "git_sha": SHA_A, "git_dirty": False}
    stamps = [("0.156.1", harness), ("0.157.0", {**harness, "git_sha": SHA_B,
                                                 "git_dirty": True}),
              ("0.156.1", harness)]
    for result_json, (cli, h) in zip(sorted(runs.rglob("result.json")), stamps):
        doc = json.loads(result_json.read_text())
        doc["provenance"].update({"agent_cli_version": cli, "harness": h})
        result_json.write_text(json.dumps(doc))
    stamped = tv._build(runs, out=runs / "_runs" / "_stamped_view")
    run = tv._run_entry(stamped)
    v2 = run["versions"]
    assert v2["agent_cli_versions"] == ["codex-cli 0.156.1", "codex-cli 0.157.0"]
    assert v2["harness_versions"] == [f"0.2.0+{'a' * 12}", f"0.2.0+{'b' * 12}-dirty"]
    assert v2["mixed"] is False and v2["set_key"] == v["set_key"]
    line = tv._versions_line(stamped.index.read_text())
    assert "agent CLI codex-cli 0.156.1, codex-cli 0.157.0" in line
    assert f"harness 0.2.0+{'a' * 12}, 0.2.0+{'b' * 12}-dirty" in line
    assert "MIXED" not in stamped.index.read_text()
