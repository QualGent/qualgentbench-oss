"""CreateBench v2 stage A: the creation episode runner, `run --mode create` (QUA-2856).

What is pinned here, all offline (no device, no model, no private text — the arm is a
throwaway git repo with a PUBLIC stand-in template, and QualGent-MCP is the public
stand-in server from `test_create_arm`):

* two MCP servers per episode render into codex's config, with the creator template as
  `developer_instructions` and `QGB_DISALLOWED_TOOLS` kept off the QualGent server;
* the stdio relay meters QualGent-MCP calls on their own ledger, classified by the one
  QualGent table, and never as device interactions;
* every validity flag reaches `passed` (and the footer reads the same flags);
* a run with zero valid artifacts exits non-zero;
* resume skips a completed (brief, trial), and the arm/brief version are in the plan's
  fingerprint;
* a whole creation episode through `run_episode` with a stubbed device and agent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path

import httpx
import pytest

from qualgentbench import checkpoint, cli, interactions
from qualgentbench import episode_runner as er
from qualgentbench.adapters.base import RunContext
from qualgentbench.adapters.codex_cli import CodexCliAdapter
from qualgentbench.bugs import load_apps
from qualgentbench.config import REPO_ROOT
from qualgentbench.create import brief as cbrief
from qualgentbench.create import runner
from qualgentbench.create.arm import (
    FAKE_API_KEY,
    QUALGENT_SERVER_NAME,
    SURFACE_NOTE,
    resolve_arm,
)
from qualgentbench.create.fake_api import AUTHORED_CASE_FILE
from qualgentbench.mcp_meter import STDIO_LEDGER_FILE, ledger_note
from qualgentbench.result import RunResult
from qualgentbench.schemas import Condition
from qualgentbench.task import BenchmarkTask
from test_agent_dump import SERIAL, _episode, device  # noqa: F401 — the fake-device fixture
from test_create_arm import STAND_IN_SERVER, _spec, repos  # noqa: F401 — fixtures

FIXTURES = Path(__file__).parent / "fixtures"
APP = "medtimer"
CASE = "medtimer-add-medicine-back-to-list"
STAND_IN_BODY = "Stand-in creator instructions. Explore, draft, then create the case."


# ── helpers ────────────────────────────────────────────────────────────────────

def _suite(app_id: str = APP) -> dict:
    return next(s for s in load_apps() if s["app"]["id"] == app_id)


def _task(case: str = CASE) -> BenchmarkTask:
    task = next(t for t in runner.create_tasks(_suite()) if t.bug_spec["case_id"] == case)
    task.bundle_id = task.bundle_id or "com.example.app"
    return task


@pytest.fixture
def surface(repos, tmp_path):  # noqa: F811
    qg, dl = repos
    arm = resolve_arm(_spec(qg, dl), cache_root=tmp_path / "cache")
    return runner.CreationSurface(arm, tmp_path / "bin" / "qualgent-mcp")


def _codex(tool: str, args: dict, text: str, *, server: str = "device",
           status: str = "completed", n: list = [0]) -> list[dict]:  # noqa: B006
    n[0] += 1
    base = {"id": f"item_{n[0]}", "type": "mcp_tool_call", "server": server, "tool": tool,
            "arguments": args}
    return [{"type": "item.started", "item": {**base, "status": "in_progress"}},
            {"type": "item.completed", "item": {
                **base, "result": {"content": [{"type": "text", "text": text}]},
                "error": None, "status": status}}]


def _transcript(*items: list[dict], final: str = "Created test case abc.") -> str:
    lines = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
    for group in items:
        lines += group
    if final:
        lines.append({"type": "item.completed",
                      "item": {"id": "msg", "type": "agent_message", "text": final}})
    lines.append({"type": "turn.completed",
                  "usage": {"input_tokens": 1000, "cached_input_tokens": 0,
                            "output_tokens": 100}})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


OBSERVE = _codex("mobile_observe_screen", {"device": SERIAL},
                 '{"elements": [{"text": "Medicine"}, {"text": "Add medicine"}]}')
CREATE = _codex("create_test_case", {"name": "x"}, '{"id": "abc"}', server="qualgent")

CASE_BODY = {
    "name": "Add a medicine",
    "steps": [{"description": "Open the app", "kind": "setup"},
              {"description": 'Tap "Medicine"', "kind": "act"},
              {"description": 'Tap "Add medicine"', "kind": "act"},
              {"description": 'Verify "Medicine" lists the new entry', "kind": "verify"}],
    "expected_result": "The new medicine is listed.",
}


_DEFAULT_LEDGER = {"calls": 1, "calls_write": 1, "sessions": 1}


def _spec_for(case: dict | None = None, *, ledger: dict | None | str = "default",
              requests: list | None = None, **extra) -> dict:
    """A creation task spec as `CreationEpisode.record` leaves it."""
    artifact = None if case is None else {
        "test_case_id": "abc", "version_number": 1, "cases_created": 1,
        "request": case, "case": case}
    creates = requests if requests is not None else (
        [{"route": "create_test_case", "status": 201, "method": "POST",
          "path": "/v1/test-cases"}] if case is not None else [])
    spec = {"mode": "create", "app_id": APP, "case_id": CASE, "step_budget": 150,
            "exit_code": 0, "truncated": False, "metered_total": 5, "hook_steps": 5,
            "creation": {"authored_case": artifact, "requests": creates,
                         "ledger": _DEFAULT_LEDGER if ledger == "default" else ledger,
                         "qualgent_api": "http://127.0.0.1:45678"}}
    spec.update(extra)
    return spec


def _verdict(spec: dict, transcript: str):
    task = BenchmarkTask(id=runner.task_id(CASE), name="t", instruction="", app_file_id="",
                         app_name="MedTimer", platform="android", bundle_id="x",
                         bug_spec=spec)
    return runner.create_verdict(transcript, "gpt-6-astra", task)


# ── tasks and the blinded directory ────────────────────────────────────────────

def test_one_creation_task_per_briefed_case_and_nothing_of_the_route():
    tasks = runner.create_tasks(_suite())
    assert tasks and all(t.id.endswith("~create") for t in tasks)
    t = _task()
    spec = t.bug_spec
    assert spec["mode"] == "create" and spec["active_bugs"] == []
    assert spec["step_budget"] == runner.CREATE_STEP_BUDGET
    # Staging only, plus the brief: no route, no oracle, no bugs, no reference steps.
    for key in ("steps", "oracle", "bugs", "expected_outcome", "defects", "blocking"):
        assert key not in spec
    assert set(spec["brief"]) == {"title", "intended_behavior"}


def test_the_author_never_sees_the_case_id():
    visible = er.agent_visible_task_id(runner.task_id(CASE))
    assert visible.startswith("create-") and CASE not in visible
    assert "back-to-list" not in visible
    assert visible == er.agent_visible_task_id(runner.task_id(CASE))   # stable per case
    assert er.agent_visible_task_id(f"{CASE}~seeded") == CASE           # journey unchanged


def test_the_prompt_is_the_note_plus_the_brief_and_nothing_else():
    t = _task()
    text = cbrief.render_brief(t.bug_spec["brief"])
    assert text.startswith(f"Feature: {t.bug_spec['brief']['title']}\n")
    assert text.endswith(cbrief.ASK)
    for leak in (CASE, "back to the", "crash", "Lisinopril"):
        assert leak not in text


def test_the_brief_version_is_pinned_to_its_text():
    """Changing the harness note or the rendering without bumping CREATE_BRIEF_VERSION
    fails here: bump the version, then record the new hash."""
    pinned = {1: "6130f1ad9cda971d06ea36668361a497a42cc5fe06dab0e8ebd801a5ba0138f0"}
    assert cbrief.CREATE_BRIEF_VERSION in pinned
    assert cbrief.text_sha256() == pinned[cbrief.CREATE_BRIEF_VERSION], cbrief.text_sha256()


# ── two servers, one config ────────────────────────────────────────────────────

def test_two_servers_render_into_codex_config(surface, tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "create-x" / "ep"
    run_dir.mkdir(parents=True)
    episode = surface.open(run_dir, _task(), SERIAL)
    try:
        cfg = er._generate_mcp_config("http://127.0.0.1:40001")
        cfg["mcpServers"].update(episode.mcp_servers())
        mcp_path = run_dir / "mcp_config.json"
        mcp_path.write_text(json.dumps(cfg))
        ctx = RunContext(task=None, agent="codex-cli", model="gpt-6-astra",
                         condition=Condition.no_routines, trial=1, run_dir=run_dir,
                         mcp_server="http://127.0.0.1:40001", mcp_config_path=mcp_path,
                         workspace_dir=run_dir / "workspace",
                         disabled_tools=["mobile_open_url"],
                         developer_instructions=episode.developer_instructions)
        monkeypatch.setattr(CodexCliAdapter, "_seed_account_auth",
                            classmethod(lambda cls, h: None))
        CodexCliAdapter().prepare(ctx)
        raw = (run_dir / "codex_home" / "config.toml").read_text()
        config = tomllib.loads(raw)
    finally:
        episode.close()
    assert config["developer_instructions"] == STAND_IN_BODY
    servers = config["mcp_servers"]
    assert set(servers) == {"device", QUALGENT_SERVER_NAME}
    assert servers["device"]["url"] == "http://127.0.0.1:40001/mcp"
    # QGB_DISALLOWED_TOOLS withholds device tools only (spike P5).
    assert servers["device"]["disabled_tools"] == ["mobile_open_url"]
    qg = servers[QUALGENT_SERVER_NAME]
    assert "disabled_tools" not in qg
    assert qg["command"] == sys.executable
    assert qg["args"][:4] == ["-m", "qualgentbench.mcp_meter", "stdio", "--ledger"]
    assert qg["args"][4] == str(run_dir / STDIO_LEDGER_FILE)
    assert qg["args"][5:] == ["--", surface.qualgent_command]
    assert qg["env"] == {"QUALGENT_API_URL": episode.api_url,
                         "QUALGENT_API_KEY": FAKE_API_KEY}
    assert qg["enabled_tools"] == list(surface.arm.qualgent_tools)
    assert "apply_disallowed_tools" not in raw        # a harness key, never rendered
    # The private surface lands in the episode dir only, never in the repo.
    private = run_dir / "private" / "developer_instructions.md"
    assert private.read_text().strip() == STAND_IN_BODY
    assert REPO_ROOT not in private.resolve().parents
    assert json.loads((run_dir / "arm.json").read_text())["qualgent_mcp"]["sha"]


def test_the_fake_api_stops_with_close_and_close_is_idempotent(surface, tmp_path):
    run_dir = tmp_path / "runs" / "create-x" / "ep"
    run_dir.mkdir(parents=True)
    episode = surface.open(run_dir, _task(), SERIAL)
    url = episode.api_url
    assert httpx.get(f"{url}/v1/categories/list").status_code == 200
    first = episode.close()
    assert episode.close() is first
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{url}/v1/categories/list", timeout=1.0)
    assert (run_dir / "api" / "summary.json").exists()


# ── the meter: QualGent calls are counted, classified, and never device steps ──

def test_the_qualgent_table_covers_the_servers_tools_and_none_is_a_device_tool():
    served = json.loads((FIXTURES / "qualgent_tools.json").read_text())["tools"]
    assert sorted(interactions.QUALGENT_TOOL_RULES) == sorted(served)
    assert interactions.QUALGENT_WRITE_TOOLS == ("create_test_case", "update_test_case")
    for name in served:
        assert interactions.mcp_effective_rule(name) is None, name
        assert interactions.classify_mcp_all(name) == [], name
    assert interactions.classify_qualgent("run_tests") == interactions.QG_OFF_SURFACE
    assert interactions.classify_qualgent("list_categories") == interactions.QG_READ
    assert interactions.classify_qualgent("brand_new_tool") == interactions.QG_UNKNOWN


def test_ledger_note_reads_calls_resources_and_sessions(tmp_path):
    ledger = interactions.CreationLedger(tmp_path / STDIO_LEDGER_FILE)
    msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
             "params": {"uri": "qualgent://test-case-guide"}},
            [{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "list_categories", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "create_test_case", "arguments": {}}}],
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "delete_apps", "arguments": {}}}]
    for m in msgs:
        ledger_note(ledger, json.dumps(m).encode() + b"\n")
    ledger_note(ledger, b"not json\n")
    d = interactions.read_creation_ledger(tmp_path / STDIO_LEDGER_FILE)
    assert d["sessions"] == 1 and d["calls"] == 3
    assert (d["calls_read"], d["calls_write"], d["calls_off_surface"]) == (1, 1, 1)
    assert d["resource_reads"] == ["qualgent://test-case-guide"]


async def test_the_stdio_relay_meters_a_real_mcp_session(tmp_path):
    """The relay in front of the (public stand-in) QualGent-MCP, driven by the real MCP
    client over stdio against the fake API: every answer passes through, every call is
    on the ledger, and nothing reaches interactions.json."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from qualgentbench.create.fake_api import FakeApp, FakeQualGentAPI

    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN_SERVER)
    ledger_path = tmp_path / "ep" / STDIO_LEDGER_FILE
    ledger_path.parent.mkdir()
    with FakeQualGentAPI(tmp_path / "ep", app=FakeApp("MedTimer")) as api:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "qualgentbench.mcp_meter", "stdio", "--ledger", str(ledger_path),
                  "--", sys.executable, str(script)],
            env={"QUALGENT_API_URL": api.url, "QUALGENT_API_KEY": FAKE_API_KEY})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            await s.read_resource("qualgent://test-case-guide")
            await s.call_tool("list_categories", {})
            created = await s.call_tool("create_test_case", {
                "name": "n", "steps": [{"description": "Open the app", "kind": "setup"}],
                "expected_result": "r"})
            assert not created.isError
            await s.call_tool("run_tests", {})
    ledger = interactions.read_creation_ledger(ledger_path)
    assert ledger["sessions"] == 1
    assert ledger["by_tool"] == {"create_test_case": 1, "list_categories": 1, "run_tests": 1}
    assert (ledger["calls_read"], ledger["calls_write"], ledger["calls_off_surface"]) == (1, 1, 1)
    assert ledger["resource_reads"] == ["qualgent://test-case-guide"]
    assert (tmp_path / "ep" / AUTHORED_CASE_FILE).exists()
    assert not (tmp_path / "ep" / "interactions.json").exists()


# ── validity flags → passed ────────────────────────────────────────────────────

VALID = _transcript(OBSERVE, CREATE)


@pytest.mark.parametrize("name, spec, transcript, flags, reason", [
    ("valid", _spec_for(CASE_BODY), VALID, [], None),
    ("never submitted", _spec_for(None), _transcript(OBSERVE, final="Done exploring."),
     ["no_case"], "never_submitted"),
    ("asked instead", _spec_for(None),
     _transcript(OBSERVE, final="Here is the draft. Shall I submit it?"),
     ["no_case"], "asked_instead"),
    ("create refused", _spec_for(None, requests=[
        {"route": "create_test_case", "status": 422, "method": "POST", "path": "/v1/test-cases"}]),
     _transcript(OBSERVE, CREATE), ["no_case"], "create_refused"),
    ("truncated first", _spec_for(None, truncated=True), _transcript(OBSERVE, final=""),
     ["no_case", "truncated"], "truncated"),
    ("truncated after the case", _spec_for(CASE_BODY, truncated=True), VALID,
     ["truncated"], None),
    ("dead: never read the screen", _spec_for(CASE_BODY), _transcript(CREATE), ["dead"], None),
    ("dead: nothing it quotes was seen", _spec_for(
        {**CASE_BODY, "steps": [{"description": 'Tap "Settings"', "kind": "act"}]}),
     VALID, ["dead"], None),
    ("off app", _spec_for(CASE_BODY, off_app=True, ended_in_package="com.other"), VALID,
     ["off_app"], None),
    ("staging failed", _spec_for(None, staging_failed="precondition not met", ledger={}),
     "", ["env_failure", "no_case"], "never_submitted"),
    ("server never started", _spec_for(None, ledger=None), _transcript(OBSERVE),
     ["env_failure", "no_case"], "never_submitted"),
    ("agent crashed", _spec_for(None, exit_code=1), _transcript(OBSERVE, final=""),
     ["env_failure", "no_case"], "never_submitted"),
])
def test_every_validity_flag_reaches_passed(name, spec, transcript, flags, reason):
    v = _verdict(spec, transcript)
    m = v.metrics
    assert m["validity_flags"] == flags, name
    assert m["no_case_reason"] == reason, name
    invalid = any(f in runner.INVALIDATING for f in flags)
    assert m["valid_case"] is (not invalid), name
    assert v.passed is m["valid_case"], name          # `passed` IS the validity
    assert (v.failure_reason is None) is (not invalid), name   # an invalid one says why
    assert m["env_failure"] is ("env_failure" in flags), name


def test_a_case_posted_around_qualgent_mcp_is_contaminated():
    curl = [{"type": "item.completed", "item": {
        "id": "c1", "type": "command_execution",
        "command": "curl -X POST http://127.0.0.1:45678/v1/test-cases -d @case.json",
        "aggregated_output": "{}", "exit_code": 0, "status": "completed"}}]
    v = _verdict(_spec_for(CASE_BODY), _transcript(OBSERVE, curl, CREATE))
    assert v.metrics["contaminated"] and "qualgent_api_bypass" in v.metrics[
        "contamination_reasons"]
    assert "contaminated" in v.metrics["validity_flags"] and v.passed is False


def test_an_honest_episode_is_not_a_bypass():
    v = _verdict(_spec_for(CASE_BODY), VALID)
    assert not v.metrics.get("contaminated")


def test_grounding_ignores_the_qualgent_servers_own_answers():
    """The fake echoes the created case back (list_test_cases, get_test_case): that is
    the author's own text, never the device's, so it cannot ground the case."""
    echo = _codex("get_test_case", {"test_case_id": "abc"},
                  json.dumps({"steps": '2. Tap "Settings"'}), server="qualgent")
    case = {**CASE_BODY, "steps": [{"description": 'Tap "Settings"', "kind": "act"}]}
    v = _verdict(_spec_for(case), _transcript(OBSERVE, CREATE, echo))
    assert v.metrics["validity_flags"] == ["dead"]
    assert v.metrics["grounding"]["ungrounded"] == ["Settings"]


def _result(metrics: dict, **kw) -> RunResult:
    base = dict(task_id=runner.task_id(CASE), task_version="qgb-v1",
                task_type=runner.TASK_TYPE, agent="codex-cli", model="gpt-6-astra",
                condition="mcp", trial=1, passed=bool(metrics.get("valid_case")),
                score=0.0, started_at="2026-09-30T00:00:00Z",
                ended_at="2026-09-30T00:02:00Z", wall_time_sec=120.0, exit_code=0,
                metrics=metrics)
    base.update(kw)
    return RunResult(**base)


def test_the_footer_reads_the_same_flags_as_passed(capsys, tmp_path):
    good = _verdict(_spec_for(CASE_BODY), VALID)
    dead = _verdict(_spec_for(CASE_BODY), _transcript(CREATE))
    rs = [_result(good.metrics), _result(dead.metrics, trial=2)]
    cli._print_run_footer(rs, tmp_path)
    out = capsys.readouterr().out
    assert "create: 1/2 valid authored case(s)" in out and "1 dead" in out
    # A creation-only run never prints the generic all-clear beside its flags.
    assert "all episodes valid" not in out


def test_the_board_counts_valid_cases_per_arm():
    good = _verdict(_spec_for(CASE_BODY), VALID)
    none = _verdict(_spec_for(None), _transcript(OBSERVE, final="Shall I submit?"))
    rows = runner.summary([_result(good.metrics), _result(none.metrics, trial=2)])
    assert len(rows) == 1
    row = rows[0]
    assert (row["episodes"], row["valid"], row["created"]) == (2, 1, 1)
    assert row["no_case_reasons"] == {"asked_instead": 1}
    assert row["brief_versions"] == [cbrief.CREATE_BRIEF_VERSION]


# ── exit code ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valid, exits", [(False, True), (True, False)])
async def test_zero_valid_artifacts_exit_non_zero(monkeypatch, tmp_path, valid, exits):
    spec = _spec_for(CASE_BODY) if valid else _spec_for(None)
    metrics = _verdict(spec, VALID if valid else _transcript(OBSERVE)).metrics

    async def fake_prepare(arm, runs_dir):
        return object()

    async def fake_preflight(*_a, **_kw):
        return {"name": "devloop-mcp"}

    class _Session:
        def __init__(self, *_a, **_kw): pass
        async def is_healthy(self): return True
        async def first_available_device(self): return SERIAL

    async def fake_episodes(*_a, creation=None, **_kw):
        assert creation is not None           # the surface reaches the lanes
        return [_result(metrics)]

    monkeypatch.setattr(cli, "_prepare_creation", fake_prepare)
    monkeypatch.setattr(cli, "_preflight", fake_preflight)
    monkeypatch.setattr("qualgentbench.session.DeviceSession", _Session)
    monkeypatch.setattr(cli, "_run_episodes", fake_episodes)
    call = cli._run_bugs(["gpt-6-astra"], "codex-cli", 1, "http://127.0.0.1:51871",
                         tmp_path, False, None, None, mode="create",
                         create_arm=(object(), None))
    if exits:
        with pytest.raises(SystemExit) as e:
            await call
        assert e.value.code == 1
    else:
        await call


# ── the gate ───────────────────────────────────────────────────────────────────

def test_create_mode_is_codex_only_and_needs_the_mcp_arm_and_an_arm(repos):  # noqa: F811
    import click

    qg, dl = repos
    kw = dict(cfg_arm=None, base=None, qualgent_mcp=f"{qg}@main", devloop=f"{dl}@main",
              qualgent_tools=None, name=None)
    with pytest.raises(click.ClickException, match="codex-cli only"):
        cli._gate_create("claude-code", "http://x", None, **kw)
    with pytest.raises(click.ClickException, match="needs --mcp-server"):
        cli._gate_create("codex-cli", None, None, **kw)
    with pytest.raises(click.ClickException, match="needs a creation arm"):
        cli._gate_create("codex-cli", "http://x", None, **{**kw, "qualgent_mcp": None,
                                                          "devloop": None})
    spec, base = cli._gate_create("codex-cli", "http://x", None,
                                  **{**kw, "qualgent_tools": "all", "name": "b"})
    assert spec.qualgent_tools == "all" and spec.name == "b" and base is None


def test_case_filter_selects_briefs_in_create_mode():
    apps = [_suite()]
    assert cli.parse_cases(CASE, apps, mode="create") == {CASE}
    cli._gate_case_filter((CASE,), "create")               # allowed, no raise
    import click
    with pytest.raises(click.ClickException, match="Unknown test case"):
        cli.parse_cases("no-such-case", apps, mode="create")


# ── plan, fingerprint and resume ───────────────────────────────────────────────

def test_the_plan_is_one_unit_per_brief_and_trial():
    from qualgentbench.lanes import build_plan
    from qualgentbench.scheduler import Estimator

    plan = build_plan([_suite()], mode="create", trials=2, lanes=1,
                      estimator=Estimator(Path("/nonexistent"), "codex-cli", "m"),
                      resolve_apk=lambda a, s: Path("/nonexistent.apk"), require_apk=False,
                      cases={CASE})
    assert [(u.task_id, u.kind, u.trial) for u in plan.units] == [
        (runner.task_id(CASE), runner.TASK_TYPE, 1), (runner.task_id(CASE), runner.TASK_TYPE, 2)]


def test_a_changed_arm_or_brief_version_is_a_different_environment(surface):
    stamp = surface.stamp()
    assert stamp["create_brief_version"] == cbrief.CREATE_BRIEF_VERSION
    same = {"create": stamp}
    assert checkpoint.compatibility(same, {"create": json.loads(json.dumps(stamp))}) == []
    moved = json.loads(json.dumps(stamp))
    moved["arm"]["qualgent_mcp"]["sha"] = "f" * 40
    moved["create_brief_version"] = 99
    diffs = checkpoint.compatibility(same, {"create": moved})
    assert any("qualgent_mcp sha" in d for d in diffs)
    assert any("create_brief_version" in d for d in diffs)


def test_the_plan_keeps_the_arm_pinned_to_its_shas(surface):
    spec = surface.spec
    assert spec["qualgent_mcp"]["ref"] == surface.arm.qualgent_mcp.sha
    assert spec["devloop"]["ref"] == surface.arm.devloop.sha
    assert Path(spec["qualgent_mcp"]["path"]).is_absolute()


def test_private_and_creation_files_in_the_checkpoint_bundle_rules():
    assert checkpoint.denied_by("create-x/ep/private/developer_instructions.md")
    for name in ("authored_case.json", "arm.json", "creation_calls.json",
                 "api/requests.jsonl"):
        assert checkpoint.not_a_bundle_member(f"create-x/ep/{name}", "r") is None


# ── one whole episode, stubbed device and agent ────────────────────────────────

class _Author:
    """Stands in for codex: reads the episode's MCP config the harness wrote, posts one
    case to the `qualgent` server's API (as QualGent-MCP would), writes the ledger the
    relay would have written, and returns a codex transcript."""

    name = "codex-cli"

    def __init__(self, *, create: bool = True) -> None:
        self.create = create
        self.seen: dict = {}

    async def run(self, instruction, context):
        cfg = json.loads(context.mcp_config_path.read_text())["mcpServers"]
        self.seen = {"instruction": instruction, "servers": cfg,
                     "developer_instructions": context.developer_instructions,
                     "cwd": str(context.workspace_dir)}
        qg = cfg[QUALGENT_SERVER_NAME]
        ledger = Path(qg["args"][qg["args"].index("--ledger") + 1])
        interactions.CreationLedger(ledger).record_session()
        if self.create:
            url = qg["env"]["QUALGENT_API_URL"]
            r = await asyncio.to_thread(
                httpx.post, f"{url}/v1/test-cases",
                json={**CASE_BODY, "change_source": "mcp"},
                headers={"x-api-key": qg["env"]["QUALGENT_API_KEY"]})
            assert r.status_code == 201, r.text
            led = interactions.CreationLedger(ledger)
            led.sessions = 1
            led.record_call("create_test_case")
            return _transcript(OBSERVE, CREATE), 0
        return _transcript(OBSERVE, final="Shall I submit this draft?"), 0


def _creation_episode(monkeypatch, tmp_path, surface, author):
    task, opts, _probe = _episode(monkeypatch, tmp_path, [])
    ct = _task()
    task.id, task.name, task.bug_spec = ct.id, ct.name, ct.bug_spec
    task.app_name = ct.app_name
    opts.agent, opts.model = "codex-cli", "gpt-6-astra"
    opts.task_type = runner.TASK_TYPE
    opts.verdict_fn = runner.create_verdict
    opts.creation = surface
    opts.run_id = "20260930-000000-c0de"
    opts.app_id = APP
    opts.mcp_server = "http://127.0.0.1:51871"

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
    monkeypatch.setattr(er, "get_adapter", lambda name: author)
    return task, opts


async def test_a_creation_episode_end_to_end(device, monkeypatch, tmp_path, surface):  # noqa: F811
    device(u2=False)
    author = _Author()
    task, opts = _creation_episode(monkeypatch, tmp_path, surface, author)
    result = await er.run_episode(task, opts)

    run_dir = Path(opts.runs_dir) / result.artifact_dir
    # What the author was handed: two servers, the template, the prompt — and no case id.
    assert set(author.seen["servers"]) == {"device", QUALGENT_SERVER_NAME}
    assert author.seen["developer_instructions"] == STAND_IN_BODY
    assert author.seen["instruction"].startswith(SURFACE_NOTE.split("\n", 1)[0])
    assert task.bug_spec["brief"]["title"] in author.seen["instruction"]
    for s in (author.seen["instruction"], author.seen["cwd"],
              json.dumps(author.seen["servers"])):
        assert CASE not in s
    # What it produced, and how it was judged.
    assert (run_dir / AUTHORED_CASE_FILE).exists()
    assert result.task_type == runner.TASK_TYPE and result.task_id == runner.task_id(CASE)
    assert result.passed is True and result.metrics["valid_case"] is True
    assert result.metrics["authored_steps"] == len(CASE_BODY["steps"])
    assert result.metrics["qualgent_calls"]["calls_write"] == 1
    prov = result.provenance["create"]
    assert prov["create_brief_version"] == cbrief.CREATE_BRIEF_VERSION
    assert prov["arm"]["qualgent_mcp"]["sha"] == surface.arm.qualgent_mcp.sha
    assert prov["qualgent"]["isolation"] == "per_episode" and prov["qualgent"]["sessions"] == 1
    # The fake API was stopped with the episode.
    assert (run_dir / "api" / "summary.json").exists()
    # Resume: this (brief, trial) is done.
    state = checkpoint.state(opts.runs_dir, opts.run_id)
    assert state.is_done(APP, runner.task_id(CASE), 1)
    assert not state.is_done(APP, runner.task_id(CASE), 2)


async def test_an_author_that_asks_is_an_outcome_not_a_crash(device, monkeypatch,  # noqa: F811
                                                             tmp_path, surface):
    device(u2=False)
    task, opts = _creation_episode(monkeypatch, tmp_path, surface, _Author(create=False))
    result = await er.run_episode(task, opts)
    m = result.metrics
    assert m["outcome"] == "no_case_created" and m["no_case_reason"] == "asked_instead"
    assert result.passed is False and not m["env_failure"]
    # Not excluded: it is a result about the author, so a resume does not re-run it.
    assert checkpoint.state(opts.runs_dir, opts.run_id).is_done(
        APP, runner.task_id(CASE), 1)


async def test_a_failed_staging_never_pays_for_an_author(device, monkeypatch,  # noqa: F811
                                                         tmp_path, surface):
    """Every staging failure is an env_failure in creation mode, so the author is not
    launched — not even on the `DeviceSetupError` path journey mode keeps."""
    device(u2=False)
    author = _Author()
    task, opts = _creation_episode(monkeypatch, tmp_path, surface, author)

    async def failing_setup(*_a, **_kw):
        raise er.DeviceSetupError("device_setup shell step failed (rc=1)")

    monkeypatch.setattr(er, "run_device_setup", failing_setup)
    result = await er.run_episode(task, opts)
    assert author.seen == {}                          # never launched
    m = result.metrics
    assert m["env_failure"] and m["cost_source"] == "not_launched"
    assert result.passed is False
    assert not checkpoint.state(opts.runs_dir, opts.run_id).is_done(
        APP, runner.task_id(CASE), 1)                 # excluded: still owed on resume


# ── the A/B driver (QUA-2858) speaks this CLI ──────────────────────────────────

def test_the_ab_drivers_author_command_parses_as_a_create_run(repos, tmp_path):  # noqa: F811
    """`create/ab.py`'s LiveAuthor shells out to `run --mode create`. Its argv must parse
    against the real `run` command, every value must land on the parameter it means, and
    the creation gate must accept the arm it names (pinned to resolved SHAs)."""
    from qualgentbench.create import ab

    qg, dl = repos
    sha_q = subprocess_sha(qg)
    sha_d = subprocess_sha(dl)
    arm = ab.ArmSpec("B", f"{qg}@main", f"{dl}@main", qualgent_tools="all",
                     manifest={"qualgent_mcp": {"sha": sha_q}, "devloop": {"sha": sha_d}})
    la = ab.LiveAuthor(runs_dir=tmp_path / "runs", work_dir=tmp_path / "w",
                       device="emulator-5558", mcp_server="http://127.0.0.1:51871",
                       agent="codex-cli", model="gpt-6-astra")
    argv = la.command(ab.Cell("B", CASE, 1), arm)
    args = argv[argv.index("run") + 1:]
    ctx = cli.run_benchmark.make_context("run", list(args))
    p = ctx.params
    assert p["mode"] == "create" and p["agent"] == "codex-cli" and p["models"] == "gpt-6-astra"
    assert p["case_filter"] == (CASE,) and p["trials"] == 1 and p["yes"] and p["plain"]
    assert p["device"] == "emulator-5558" and p["mcp_server"] == "http://127.0.0.1:51871"
    assert p["run_id_file"] == tmp_path / "w" / "cells" / f"{ab.Cell('B', CASE, 1).key}.run_id"
    spec, base = cli._gate_create(
        p["agent"], p["mcp_server"], None, cfg_arm=None, base=None,
        qualgent_mcp=p["create_qualgent_mcp"], devloop=p["create_devloop"],
        qualgent_tools=p["create_qualgent_tools"], name=p["create_arm_name"])
    assert spec.name == "B" and spec.qualgent_tools == "all" and base is None
    assert (spec.qualgent_mcp.ref, spec.devloop.ref) == (sha_q, sha_d)
    resolved = resolve_arm(spec, cache_root=tmp_path / "cache")
    assert resolved.qualgent_mcp.sha == sha_q and resolved.devloop.sha == sha_d
    # The brief it names is one `--case` accepts in create mode.
    assert cli.parse_cases(p["case_filter"], load_apps(), mode="create") == {CASE}


def subprocess_sha(repo: Path) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_the_readiness_gate_reads_the_runners_own_verdict():
    """`check_tier_ready.py --tier create` judges a creation episode by the runner's
    metrics (QUA-2859's TODO, resolved): a valid case carries no flag — truncated after
    the case included — and every invalid one names why."""
    import importlib.util

    path = REPO_ROOT / "scripts" / "check_tier_ready.py"
    spec = importlib.util.spec_from_file_location("ctr_for_create_runner", path)
    ctr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctr)

    def flags(case, transcript, **extra):
        return ctr.creation_flags({"metrics": _verdict(_spec_for(case, **extra),
                                                       transcript).metrics})

    assert flags(CASE_BODY, VALID) == []
    assert flags(CASE_BODY, VALID, truncated=True) == []
    assert flags(None, _transcript(OBSERVE, final="Shall I submit?")) == ["no_case:asked_instead"]
    assert flags(CASE_BODY, _transcript(CREATE)) == ["dead"]
    assert flags(None, _transcript(OBSERVE), ledger=None) == [
        "env_failure", "no_case:never_submitted"]
    curl = [{"type": "item.completed", "item": {
        "id": "c1", "type": "command_execution",
        "command": "curl http://127.0.0.1:45678/v1/test-cases", "aggregated_output": "{}",
        "exit_code": 0, "status": "completed"}}]
    assert flags(CASE_BODY, _transcript(OBSERVE, curl, CREATE)) == [
        "contaminated:qualgent_api_bypass"]
