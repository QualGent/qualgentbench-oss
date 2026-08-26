"""Container provenance: the harness can sit in a container
with adb on the host, and every result says where it ran."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qualgentbench import leaderboard as lb
from qualgentbench.adb_meter import AdbMeter, upstream_from_env
from qualgentbench.result import RunResult, VerifierResult


@pytest.fixture(autouse=True)
def _no_adb_env(monkeypatch):
    for var in ("ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_HOST",
                "ANDROID_ADB_SERVER_PORT"):
        monkeypatch.delenv(var, raising=False)


def test_meter_defaults_to_local_adb():
    assert upstream_from_env() == ("127.0.0.1", 5037)
    assert AdbMeter("/dev/null").upstream == ("127.0.0.1", 5037)


def test_meter_follows_the_adb_binary_address_var(monkeypatch):
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "host.docker.internal")
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5038")
    assert AdbMeter("/dev/null").upstream == ("host.docker.internal", 5038)


def test_meter_follows_the_adbutils_host_var_too(monkeypatch):
    monkeypatch.setenv("ANDROID_ADB_SERVER_HOST", "10.0.0.7")
    assert AdbMeter("/dev/null").upstream == ("10.0.0.7", 5037)


def test_explicit_upstream_beats_env(monkeypatch):
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "host.docker.internal")
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "garbage")
    m = AdbMeter("/dev/null", upstream_port=6000, upstream_host="127.0.0.1")
    assert m.upstream == ("127.0.0.1", 6000)


def _result(run_id: str, task: str, **prov) -> RunResult:
    t0 = datetime(2026, 8, 20, tzinfo=timezone.utc)
    return RunResult.build(
        task_id=task, task_version="v", task_type="bug_hunt", agent="claude-code",
        model="m", condition="raw", trial=1, started_at=t0,
        ended_at=t0 + timedelta(minutes=3), exit_code=0,
        verifier=VerifierResult(passed=True, score=1.0),
        artifact_dir=Path("/x"), run_id=run_id, provenance=prov,
    )


def test_result_records_run_id_and_provenance(tmp_path):
    r = _result("r1", "birday-hunt", device_serial="emulator-5554", lane=2, lanes=3)
    r.write(tmp_path / "result.json")
    on_disk = json.loads((tmp_path / "result.json").read_text())
    assert on_disk["run_id"] == "r1"
    assert on_disk["provenance"] == {"device_serial": "emulator-5554", "lane": 2, "lanes": 3}


def test_old_result_json_without_provenance_still_loads():
    legacy = _result("", "t").model_dump()
    del legacy["run_id"], legacy["provenance"]
    r = RunResult.model_validate(legacy)
    assert r.run_id == "" and r.provenance == {}


def test_show_can_scope_to_one_run(tmp_path):
    for run_id, task in (("mon", "a"), ("mon", "b"), ("tue", "a")):
        d = tmp_path / task / run_id
        d.mkdir(parents=True)
        _result(run_id, task).write(d / "result.json")
    assert {r.task_id for r in lb.load_results(tmp_path)} == {"a", "b"}
    assert [(r.task_id, r.run_id) for r in lb.load_results(tmp_path, run_id="tue")] \
        == [("a", "tue")]
