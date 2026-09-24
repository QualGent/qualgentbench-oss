"""The per-episode integrity signals are acted on, and the MCP server is stamped and
enforced (QUA-2806).

* `provenance.adbd_at_end.rooted` is a HARD contamination hit (`adbd_rooted`) in the
  scan every scorer runs — the nonce's mechanism, read off the device.
* `provenance.mcp_isolation.clean == false` on a server that keeps the record
  excludes the episode (`mcp_unclean`), like an env_failure; a server with no record
  is flagged. Both are counted on the board and the rates leave them out.
* The MCP server's identity (name, version, app source, instruction and tool hashes)
  is in plan.json and every episode; `run` refuses a DevLoop server outside
  no-source mode and a resume refuses a changed server.
* The hand-off clock tolerance is a setting (`QGB_CLOCK_TOLERANCE_S`).

No test here reaches a device.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from qualgentbench import checkpoint, contamination, failures, journey
from qualgentbench import episode_runner as er
from qualgentbench import session as sess_mod
from qualgentbench.transcript import TranscriptParser
from test_agent_dump import _episode, device  # noqa: F401 — the fake-device fixture
from test_journey import _rr

ROOT = Path(__file__).resolve().parents[1]

DEVLOOP = {"name": "devloop-mcp", "version": "1.27.0", "app_source": "none",
           "instructions_sha256": "a" * 64, "tools_sha256": "b" * 64, "tools": 90}
ROOTED = {"uid": 0, "rooted": True, "root_primed": False, "unprimed": False}
PRIMED = {"uid": 2000, "rooted": False, "root_primed": True, "unprimed": True}
UNROOTED = {"uid": 2000, "rooted": False, "root_primed": False, "unprimed": False}
CLEAN = {"isolation": "per_mcp_session", "sessions": [{"clean_at_start": True}], "clean": True}
UNCLEAN = {"isolation": "per_mcp_session", "sessions": [{"clean_at_start": False}],
           "clean": False}
NO_RECORD = {"isolation": "unavailable", "sessions": [], "clean": False}


# ── adbd_at_end: a hard contamination hit ──────────────────────────────────────

def _scan(adbd):
    return contamination.scan(TranscriptParser(""), "/runs/c/e/workspace", adbd_at_end=adbd)


def test_a_rooted_adbd_voids_the_episode():
    c = _scan(ROOTED)
    assert c.contaminated and c.reasons == ["adbd_rooted"]
    assert c.as_metrics()["contamination_hits"][0]["kind"] == "adbd_rooted"


@pytest.mark.parametrize("adbd", [PRIMED, UNROOTED, None, {"uid": None, "rooted": None}])
def test_only_rooted_is_hard(adbd):
    c = _scan(adbd)
    assert not c.contaminated
    assert [s["kind"] for s in c.soft] == (["adbd_root_primed"] if adbd is PRIMED else [])


def _journey_task(adbd=None):
    from qualgentbench.task import BenchmarkTask

    spec = {"mode": "journey", "app_id": "app", "case_id": "c", "version": "clean",
            "name": "C", "steps": ["Open"], "expected_outcome": "Opens.", "active_bugs": [],
            "step_budget": 30, "expected": "PASS", "blocking": None, "blocking_texts": [],
            "crash_texts": [], "echo_texts": [], "absence_texts": [], "side": [],
            "defects": {}, "oracle": journey._oracle({"check": {"expect": {"present": "x"}}}),
            "truth_agrees": True, "tooling": "raw", "workspace": "/runs/c/e/workspace",
            "findings_file": "verdict: pass\nbugs: []\n", "adbd_at_end": adbd}
    return BenchmarkTask(id="c~clean", name="C", instruction="", app_file_id="",
                         app_name="App", platform="android", bundle_id="com.x", bug_spec=spec)


def test_the_journey_scorer_voids_a_rooted_episode_and_every_board_drops_it():
    v = journey.journey_verdict("", "m", _journey_task(ROOTED))
    assert v.metrics["contaminated"] is True
    assert "adbd_rooted" in v.metrics["contamination_reasons"]
    assert failures.is_excluded(v.metrics)
    # (This episode is also infra_failure — no device calls — which names itself first.)
    assert "adbd ended the episode rooted" in failures.exclusion_reason(
        {k: v.metrics[k] for k in ("contaminated", "contamination_reasons")})
    ok = journey.journey_verdict("", "m", _journey_task(UNROOTED))
    assert not ok.metrics["contaminated"]


def test_the_hunt_scorer_reads_it_too():
    from test_ablation import _hunt_task

    from qualgentbench import bugs

    task = _hunt_task("raw")
    task.bug_spec["adbd_at_end"] = ROOTED
    v = bugs.exploration_verdict("", "m", task)
    assert v.metrics["contaminated"] and "adbd_rooted" in v.metrics["contamination_reasons"]


# ── mcp_isolation: excluded when unclean, flagged when unrecorded ──────────────

@pytest.mark.parametrize("record, server, expected", [
    (CLEAN, "devloop-mcp", {}),
    (UNCLEAN, "devloop-mcp", {"mcp_unclean": True}),
    (UNCLEAN, None, {"mcp_unclean": True}),              # recorded before the stamp
    (NO_RECORD, "devloop-mcp", {"mcp_unclean": True}),   # DevLoop must keep the record
    (NO_RECORD, "other-server", {"mcp_isolation_unverified": True}),
    (None, None, {}),                                    # raw arm / no agent
])
def test_mcp_integrity(record, server, expected):
    assert failures.mcp_integrity(record, server) == expected
    assert failures.is_excluded(expected) is bool(expected.get("mcp_unclean"))


def _board_ep(task_id, *, version, false_reports=0, **extra):
    m = {"version": version, "completed": True, "bugs_present": ["a"] if version == "seeded" else [],
         "bugs_found": ["a"] if version == "seeded" else [], "false_reports": false_reports,
         "steps": 10, "app_id": "x", "heldout": False, "corpus_version": "c" * 12, **extra}
    return _rr(task_id, m)


def test_a_board_with_a_rooted_and_an_unclean_episode_shows_both_excluded():
    """Each would otherwise have counted as a false alarm on a clean case: the rates
    must be computed as if they were never run, and the board must say why two
    episodes are missing."""
    rows_ok = journey.summary([_board_ep("c1~clean", version="clean"),
                               _board_ep("c1~seeded", version="seeded")])
    rooted = _board_ep("c2~clean", version="clean", false_reports=1, contaminated=True,
                       contamination_reasons=["adbd_rooted"])
    unclean = _board_ep("c3~clean", version="clean", false_reports=1, mcp_unclean=True)
    unverified = _board_ep("c4~clean", version="clean", mcp_isolation_unverified=True)
    rows = journey.summary([_board_ep("c1~clean", version="clean"),
                            _board_ep("c1~seeded", version="seeded"),
                            rooted, unclean, unverified])
    (row,) = rows
    assert row["excluded_episodes"] == 2 and row["planned_episodes"] == 5
    assert row["episodes"] == 3 and row["clean_episodes"] == 2
    assert row["integrity_flags"] == {"adbd_rooted": 1, "mcp_isolation_unverified": 1,
                                      "mcp_unclean": 1}
    # The rates exclude the two: no false alarm is counted from either.
    assert row["false_reports"] == 0 and row["false_alarm_k"] == 0
    assert row["false_alarm_n"] == rows_ok[0]["false_alarm_n"] + 1   # + the unverified one
    note = journey.integrity_note(rows)
    assert "1 adbd rooted at the end (contaminated, excluded)" in note
    assert "1 MCP session not clean at start (excluded)" in note
    assert "1 MCP server keeps no session record (kept, unverified)" in note
    assert journey.integrity_note(rows_ok) is None


def test_the_printed_board_names_the_integrity_exclusions(capsys):
    from qualgentbench import cli

    rs = [_board_ep("c1~clean", version="clean"), _board_ep("c1~seeded", version="seeded"),
          _board_ep("c3~clean", version="clean", mcp_unclean=True)]
    cli._print_journey_table(rs)
    out = capsys.readouterr().out
    assert "Episode integrity: 1 MCP session not clean at start (excluded)" in out


# ── wired into run_episode ────────────────────────────────────────────────────

def _mcp_episode(monkeypatch, tmp_path, *, isolation, identity=DEVLOOP, planned=None,
                 adbd=None):
    task, opts, agent = _episode(monkeypatch, tmp_path, log := [])
    opts.mcp_server = "http://127.0.0.1:51831"
    opts.mcp_server_identity = planned

    class _NoMeter:
        def __init__(self, *_a, **_kw): pass
        async def start(self, *_a, **_kw): return 0
        async def stop(self): return None
        def url(self, _path): return "http://127.0.0.1:1"

    async def fetch_identity(url, **_kw):
        return dict(identity)

    async def fetch_isolation(url, **_kw):
        return dict(isolation)

    async def adbd_read(serial):
        return adbd

    monkeypatch.setattr(er, "McpMeter", _NoMeter)
    monkeypatch.setattr(er, "fetch_server_identity", fetch_identity)
    monkeypatch.setattr(er, "fetch_episode_isolation", fetch_isolation)
    monkeypatch.setattr(er, "check_adbd_after_agent", adbd_read)
    return task, opts, log


async def test_an_unclean_mcp_session_is_excluded_by_run_episode(device, monkeypatch,  # noqa: F811
                                                                  tmp_path):
    device(u2=False)
    task, opts, log = _mcp_episode(monkeypatch, tmp_path, isolation=UNCLEAN)
    result = await er.run_episode(task, opts)
    assert "AGENT" in log
    assert result.metrics["mcp_unclean"] is True and failures.is_excluded(result.metrics)
    assert result.provenance["mcp_isolation"]["clean"] is False


async def test_run_episode_hands_the_rooted_record_to_the_scorer(device, monkeypatch,  # noqa: F811
                                                                  tmp_path):
    device(u2=False)
    task, opts, _ = _mcp_episode(monkeypatch, tmp_path, isolation=CLEAN, adbd=ROOTED)
    result = await er.run_episode(task, opts)
    assert task.bug_spec["adbd_at_end"] == ROOTED
    assert result.provenance["adbd_at_end"] == ROOTED
    assert "mcp_unclean" not in result.metrics


async def test_the_server_identity_is_stamped_in_provenance(device, monkeypatch,  # noqa: F811
                                                             tmp_path):
    device(u2=False)
    task, opts, log = _mcp_episode(monkeypatch, tmp_path, isolation=CLEAN, planned=DEVLOOP)
    result = await er.run_episode(task, opts)
    assert "AGENT" in log
    assert result.provenance["mcp_server"] == checkpoint.server_stamp(DEVLOOP)
    assert set(result.provenance["mcp_server"]) == set(sess_mod.IDENTITY_KEYS)


@pytest.mark.parametrize("identity, planned, refusal", [
    ({**DEVLOOP, "app_source": "available"}, None, "DevLoop-MCP is not in no-source mode"),
    ({**DEVLOOP, "app_source": "inconsistent"}, DEVLOOP, "DevLoop-MCP is not in no-source mode"),
    ({**DEVLOOP, "instructions_sha256": "c" * 64}, DEVLOOP,
     "MCP server changed since the run was planned: instructions_sha256"),
    ({k: None for k in DEVLOOP} | {"error": "ConnectError: refused"}, DEVLOOP,
     "MCP server identity could not be read"),
])
async def test_the_agent_is_not_handed_the_wrong_server(device, monkeypatch, tmp_path,  # noqa: F811
                                                        identity, planned, refusal):
    device(u2=False)
    task, opts, log = _mcp_episode(monkeypatch, tmp_path, isolation=CLEAN,
                                   identity=identity, planned=planned)
    result = await er.run_episode(task, opts)
    assert "AGENT" not in log
    assert task.bug_spec["staging_failed"].startswith(refusal), task.bug_spec["staging_failed"]
    assert result.metrics.get("cost_source") == "not_launched"


# ── the server's identity ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name, instructions, tools, mode", [
    ("devloop-mcp", "APP SOURCE: none. This server runs in no-source mode", ["mobile_tap"], "none"),
    ("devloop-mcp", "You are a QA agent.", ["mobile_tap", "mobile_workspace_info"], "available"),
    ("devloop-mcp", "APP SOURCE: none. …", ["mobile_workspace_info"], "inconsistent"),
    ("devloop-mcp", "You are a QA agent.", ["mobile_tap"], "inconsistent"),
    ("other", "APP SOURCE: none", [], None),
])
def test_server_app_source(name, instructions, tools, mode):
    assert sess_mod.server_app_source(name, instructions, tools) == mode


def _identity(monkeypatch, name, instructions, tools, version="1.27.0"):
    from test_adapter_lifecycle import _FakeSession, _patched_session

    fake = _FakeSession(tools)

    async def initialize():
        return SimpleNamespace(serverInfo=SimpleNamespace(name=name, version=version),
                               instructions=instructions)

    fake.initialize = initialize
    _patched_session(monkeypatch, fake)
    return asyncio.run(sess_mod.fetch_server_identity("http://127.0.0.1:51831"))


def test_fetch_server_identity_reads_what_the_agent_would(monkeypatch):
    a = _identity(monkeypatch, "devloop-mcp", "APP SOURCE: none. x", ["mobile_tap"])
    assert a["name"] == "devloop-mcp" and a["version"] == "1.27.0" and a["app_source"] == "none"
    assert a["tools"] == 1 and len(a["instructions_sha256"]) == 64 and "error" not in a
    # Any change to the instructions or the tool set moves the identity.
    b = _identity(monkeypatch, "devloop-mcp", "APP SOURCE: none. y", ["mobile_tap"])
    c = _identity(monkeypatch, "devloop-mcp", "APP SOURCE: none. x", ["mobile_tap", "mobile_swipe"])
    assert sess_mod.identity_changes(a, b) == [
        f"instructions_sha256: plan {a['instructions_sha256'][:12]} → now "
        f"{b['instructions_sha256'][:12]}"]
    assert [d.split(":")[0] for d in sess_mod.identity_changes(a, c)] == ["tools_sha256"]
    assert sess_mod.identity_changes(a, dict(a)) == []


def test_an_unreadable_server_is_an_error_not_an_exception(monkeypatch):
    import contextlib

    import mcp.client.streamable_http as http_mod

    @contextlib.asynccontextmanager
    async def refused(url):
        raise ConnectionRefusedError("refused")
        yield

    monkeypatch.setattr(http_mod, "streamablehttp_client", refused)
    out = asyncio.run(sess_mod.fetch_server_identity("http://127.0.0.1:1"))
    assert out["error"].startswith("ConnectionRefusedError") and out["name"] is None


def test_identity_changes_names_an_arm_switch():
    assert sess_mod.identity_changes(None, None) == []
    assert sess_mod.identity_changes(DEVLOOP, None) == [
        "server: plan devloop-mcp → now no MCP server (bare arm)"]
    assert sess_mod.identity_changes(None, DEVLOOP) == [
        "server: plan no MCP server (bare arm) → now devloop-mcp"]


# ── plan.json and --resume ────────────────────────────────────────────────────

def test_plan_json_carries_the_server(tmp_path):
    from qualgentbench import cli

    cli._write_plan(tmp_path, "r1", {}, apps=[], mode="journey", mcp_identity=DEVLOOP)
    plan = json.loads((tmp_path / "_runs" / "r1" / "plan.json").read_text())
    assert plan["environment"]["mcp_server"] == checkpoint.server_stamp(DEVLOOP)
    cli._write_plan(tmp_path, "r2", {}, apps=[], mode="journey", mcp_identity=None)
    plan = json.loads((tmp_path / "_runs" / "r2" / "plan.json").read_text())
    assert "mcp_server" in plan["environment"] and plan["environment"]["mcp_server"] is None


@pytest.mark.parametrize("planned, now, changed", [
    (DEVLOOP, DEVLOOP, []),
    (DEVLOOP, {**DEVLOOP, "app_source": "available"}, ["mcp_server app_source"]),
    (DEVLOOP, {**DEVLOOP, "version": "1.28.0"}, ["mcp_server version"]),
    (DEVLOOP, None, ["mcp_server server"]),          # resumed without --mcp-server
    (None, DEVLOOP, ["mcp_server server"]),          # a raw run resumed with one
])
def test_compatibility_compares_the_server(planned, now, changed):
    base = {"schema_version": 1, "package_version": "p", "image_digest": None,
            "brief_version": 3, "apps": {}}
    diffs = checkpoint.compatibility({**base, "mcp_server": checkpoint.server_stamp(planned)},
                                     {**base, "mcp_server": checkpoint.server_stamp(now)})
    assert [d.split(":")[0] for d in diffs] == changed


def test_a_plan_from_before_the_stamp_is_not_refused():
    base = {"schema_version": 1, "package_version": "p", "image_digest": None,
            "brief_version": 3, "apps": {}}
    assert checkpoint.compatibility(base, {**base, "mcp_server": DEVLOOP}) == []


def test_resume_refuses_a_changed_server(monkeypatch):
    from qualgentbench import cli

    planned = cli._environment_now([], "journey", DEVLOOP)
    resume = SimpleNamespace(run_id="r1", environment=planned)
    plan = SimpleNamespace(apks={})
    with pytest.raises(click.ClickException) as exc:
        cli._check_resume_environment(resume, [], plan, "journey", False,
                                      {**DEVLOOP, "tools_sha256": "d" * 64})
    assert "mcp_server tools_sha256" in str(exc.value)
    cli._check_resume_environment(resume, [], plan, "journey", False, dict(DEVLOOP))


def test_preflight_refuses_devloop_outside_no_source_mode(monkeypatch):
    from test_adapter_lifecycle import _preflight_problems

    from qualgentbench import cli

    async def isolated(url):
        from qualgentbench.doctor import CheckResult
        return CheckResult("MCP episode isolation", True, "")

    async def identity(url):
        return {**DEVLOOP, "app_source": "available"}

    monkeypatch.setattr(cli, "_check_isolation", isolated)
    monkeypatch.setattr(cli, "_server_identity", identity)
    text = _preflight_problems(monkeypatch, "http://127.0.0.1:51831", status=406,
                               tools=["mobile_tap"])
    assert "is not in no-source mode (app source: available)" in text
    assert "--app-source none" in text


# ── rescore reads the provenance ──────────────────────────────────────────────

_spec = importlib.util.spec_from_file_location("rescore_journey_2806",
                                               ROOT / "scripts" / "rescore_journey.py")
rescore_journey = importlib.util.module_from_spec(_spec)
sys.modules["rescore_journey_2806"] = rescore_journey
_spec.loader.exec_module(rescore_journey)


@pytest.mark.parametrize("adbd, void", [(ROOTED, True), (UNROOTED, False), (None, False)])
def test_rescore_keeps_a_rooted_episode_void(tmp_path, adbd, void):
    task = _journey_task()
    run_dir = tmp_path / "ep"
    (run_dir / "agent").mkdir(parents=True)
    (run_dir / "workspace").mkdir()
    (run_dir / "agent" / "transcript.txt").write_text("")
    (run_dir / "workspace" / journey.FILENAME).write_text("verdict: pass\nbugs: []\n")
    (run_dir / "result.json").write_text(json.dumps(
        {"task_type": journey.TASK_TYPE, "task_id": task.id, "condition": "raw",
         "model": "m", "metrics": {"completed": None},
         "provenance": {"adbd_at_end": adbd, "mcp_isolation": UNCLEAN}}))
    status, _, _, v = rescore_journey.rescore(run_dir, {task.id: task}, dry_run=False)
    assert status == "rescored"
    assert bool(v.metrics["contaminated"]) is void
    written = json.loads((run_dir / "result.json").read_text())["metrics"]
    assert written["mcp_unclean"] is True and failures.is_excluded(written)


# ── the hand-off clock tolerance is a setting ─────────────────────────────────

def test_the_clock_tolerance_defaults_to_five_minutes(monkeypatch):
    monkeypatch.delenv("QGB_CLOCK_TOLERANCE_S", raising=False)
    assert er.clock_tolerance_s() == er.CLOCK_TOLERANCE_S == 300


@pytest.mark.parametrize("raw", ["0", "-5", "ten", "1.5"])
def test_a_malformed_tolerance_is_refused(monkeypatch, raw):
    from qualgentbench import cli

    monkeypatch.setenv("QGB_CLOCK_TOLERANCE_S", raw)
    with pytest.raises(ValueError, match="QGB_CLOCK_TOLERANCE_S"):
        er.clock_tolerance_s()
    with pytest.raises(click.ClickException):
        cli._gate_clock_tolerance()


async def test_a_slow_multilane_hand_off_passes_under_a_raised_tolerance(monkeypatch):
    """Staging on a loaded host that took 7 minutes from the pin to the hand-off: void
    at the default, clean with QGB_CLOCK_TOLERANCE_S=900 — and the offset is recorded
    either way, so a host drifting toward the limit shows before it voids anything."""
    from qualgentbench import preflight
    from qualgentbench.verify import device as vdevice

    pin = er.device_clock_pin()
    now = int(pin.timestamp()) + 420

    async def adb(serial, *args, **_kw):
        cmd = " ".join(args)
        if cmd == "shell date +%s":
            return 0, f"{now}\n".encode()
        if cmd == "shell id -u":
            return 0, b"2000\n"
        if "settings get system" in cmd:
            return 0, b"0\n"
        if cmd.startswith("shell if [ -d /data/anr ]"):
            return 0, b"qgb-anr-end\n"
        return 0, b""

    async def no_u2(serial):
        return []

    monkeypatch.setattr(vdevice, "_adb", adb)
    monkeypatch.setattr(vdevice, "_running_u2_servers", no_u2)
    monkeypatch.delenv("QGB_CLOCK_TOLERANCE_S", raising=False)
    seen: dict = {}
    bad = await preflight.device_state_violations("s", expect_launcher=False, clock_pin=pin,
                                                  observed=seen)
    assert len(bad) == 1 and "+420 s from the pin" in bad[0] and "(tolerance 300 s)" in bad[0]
    assert seen == {"clock_offset_s": 420, "clock_tolerance_s": 300}
    monkeypatch.setenv("QGB_CLOCK_TOLERANCE_S", "900")
    seen.clear()
    assert await preflight.device_state_violations("s", expect_launcher=False, clock_pin=pin,
                                                   observed=seen) == []
    assert seen == {"clock_offset_s": 420, "clock_tolerance_s": 900}
