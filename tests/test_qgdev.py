"""qgdev lease (QUA-3381): the DCP request/lease flow against a scripted DCP."""

from __future__ import annotations

import json
import os
import signal
import stat
import sys

import httpx
import pytest
from click.testing import CliRunner

from qualgentbench import qgdev

DESCRIPTOR = {
    "lease_id": "11111111-1111-4111-8111-111111111111",
    "fencing_token": 7,
    "expires_at": "2026-11-05T20:00:00Z",
    "device": {"id": "22222222-2222-4222-8222-222222222222", "class": "mac-arm64-api35-gapis@1",
               "platform": "android", "kind": "emulator", "model": None, "os_version": "15"},
    "transport": {"kind": "adb_server", "host": "farm-host.example.test", "port": 40001,
                  "serial": "emulator-5554", "avd_name": "dcp-api35-1",
                  "token": "dcp_lease_x_secret"},
    "host_api": {"url": "http://farm-host.example.test:41000/v1/leases/1111",
                 "token": "dcp_lease_x_secret", "supports": ["install", "record"]},
}


class FakeDcp:
    """Scripted DCP: the request waits `waits` long-polls, then is granted."""

    def __init__(self, waits: int = 1, end_state: str | None = None):
        self.waits = waits
        self.end_state = end_state
        self.calls: list[tuple[str, str, dict]] = []
        self.heartbeat_status = 200

    def view(self, lease_state: str | None) -> dict:
        lease = None
        if lease_state:
            lease = {"id": DESCRIPTOR["lease_id"], "state": lease_state, "end_reason": None,
                     "fencing_token": 7, "expires_at": DESCRIPTOR["expires_at"],
                     "idle_deadline": None, "hard_deadline": None, "renewals_used": 0,
                     "connection": DESCRIPTOR if lease_state in ("ready", "active") else None}
        state = "granted" if lease_state else (self.end_state or "waiting")
        return {"id": "req-1", "state": state, "end_reason": "no_device" if self.end_state else None,
                "consumer": "tailnet", "priority_class": "benchmark", "lease": lease}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if request.method == "POST" and path == "/v1/requests":
            return httpx.Response(201, json=self.view(None))
        if request.method == "GET" and path == "/v1/requests/req-1":
            if self.end_state:
                return httpx.Response(200, json=self.view(None))
            if self.waits > 0:
                self.waits -= 1
                return httpx.Response(200, json=self.view("provisioning"))
            return httpx.Response(200, json=self.view("ready"))
        if path.endswith("/heartbeat"):
            if self.heartbeat_status != 200:
                return httpx.Response(self.heartbeat_status, json={"error": "lease_ended", "message": "idle"})
            return httpx.Response(200, json={"state": "active", "expires_at": "x",
                                             "idle_deadline": None, "hard_deadline": None})
        if path.endswith("/release"):
            return httpx.Response(200, json={"state": "releasing"})
        if path.endswith("/cancel"):
            return httpx.Response(200, json=self.view(None))
        if path.endswith("/adb/allow"):
            assert request.headers["authorization"] == "Bearer dcp_lease_x_secret"
            return httpx.Response(200, json={"allowed": "100.64.0.9", "port": 40001})
        if path == "/v1/whoami":
            return httpx.Response(200, json={"kind": "person", "id": "user:a@b", "scopes": [],
                                             "priority_classes": ["fde", "benchmark"]})
        return httpx.Response(404, json={"error": "nope"})


def client_for(fake: FakeDcp) -> qgdev.DcpClient:
    return qgdev.DcpClient("http://dcp.test", transport=httpx.MockTransport(fake.handler))


def test_class_ref_defaults_to_version_1():
    assert qgdev.normalize_class_ref("mac-arm64-api35-gapis") == "mac-arm64-api35-gapis@1"
    assert qgdev.normalize_class_ref("mac-arm64-api35-gapis@2") == "mac-arm64-api35-gapis@2"


def test_request_is_benchmark_remote_and_pins_the_class():
    body = qgdev.request_body("mac-arm64-api35-gapis@1", platform="android", deploy_env="dev",
                              wait_seconds=None, consumer_ref="qgdev:me")
    assert body["priority_class"] == "benchmark"
    assert body["lease_mode"] == "remote"
    assert body["interactive"] is False
    assert body["constraints"] == {"platform": "android", "device_class": "mac-arm64-api35-gapis@1"}
    assert "wait_seconds" not in body  # benchmark waits until cancelled (A5)


def test_session_waits_admits_heartbeats_and_releases():
    fake = FakeDcp(waits=2)
    session = qgdev.LeaseSession(client_for(fake),
                                 qgdev.request_body("c@1", platform="android", deploy_env="dev",
                                                    wait_seconds=None, consumer_ref=None),
                                 log=lambda _m: None)
    lease = session.acquire()
    assert lease.lease_id == DESCRIPTOR["lease_id"]
    env = qgdev.adb_env(lease)
    assert env["ANDROID_ADB_SERVER_ADDRESS"] == env["ANDROID_ADB_SERVER_HOST"] == "farm-host.example.test"
    assert env["ANDROID_ADB_SERVER_PORT"] == "40001"
    assert env["ANDROID_SERIAL"] == "emulator-5554"
    assert env["QGDEV_AVD_NAME"] == "dcp-api35-1"
    assert env["QGB_DEVICE_CLASS"] == "mac-arm64-api35-gapis"
    from qualgentbench.result import is_device_class
    assert is_device_class(env["QGB_DEVICE_CLASS"])
    session.release()
    session.release()  # idempotent
    paths = [(m, p) for m, p, _ in fake.calls]
    assert paths.count(("GET", "/v1/requests/req-1")) == 3
    # first heartbeat moves ready -> active, then the proxy admits us, release once
    hb = paths.index(("POST", f"/v1/leases/{DESCRIPTOR['lease_id']}/heartbeat"))
    allow = paths.index(("POST", "/v1/leases/1111/adb/allow"))
    assert hb < allow
    assert paths.count(("POST", f"/v1/leases/{DESCRIPTOR['lease_id']}/release")) == 1
    release_body = next(b for m, p, b in fake.calls if p.endswith("/release"))
    assert release_body == {"fencing_token": 7}


def test_expired_request_raises():
    fake = FakeDcp(end_state="expired")
    session = qgdev.LeaseSession(client_for(fake),
                                 qgdev.request_body("c@1", platform="android", deploy_env="dev",
                                                    wait_seconds=0, consumer_ref=None),
                                 log=lambda _m: None)
    with pytest.raises(qgdev.DcpError, match="expired"):
        session.acquire()


def test_release_before_grant_cancels_the_request():
    fake = FakeDcp()
    session = qgdev.LeaseSession(client_for(fake), qgdev.request_body(
        "c@1", platform="android", deploy_env="dev", wait_seconds=None, consumer_ref=None),
        log=lambda _m: None)
    session.request_id = "req-1"
    session.release()
    assert ("POST", "/v1/requests/req-1/cancel") in [(m, p) for m, p, _ in fake.calls]


def test_heartbeat_lease_ended_stops_and_reports():
    fake = FakeDcp(waits=0)
    client = client_for(fake)
    lease = qgdev.lease_from_request(fake.view("ready"))
    ended = []
    hb = qgdev.Heartbeat(client, lease, on_end=ended.append)
    fake.heartbeat_status = 409
    hb.beat()
    assert ended == ["lease_ended"]
    assert hb.stop_event.is_set()


def test_non_adb_lease_is_refused():
    lease = qgdev.Lease("r", "l", 1, {"transport": {"kind": "wda_http", "url": "http://x"}})
    with pytest.raises(qgdev.DcpError, match="not an adb lease"):
        qgdev.adb_env(lease)


def test_descriptor_file_is_private_and_devloop_shaped(tmp_path):
    lease = qgdev.lease_from_request(FakeDcp().view("ready"))
    path = tmp_path / "farm" / "descriptors.json"
    qgdev.write_descriptor_file(path, lease)
    assert json.loads(path.read_text()) == {"descriptors": [DESCRIPTOR]}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_exports_quote_values():
    assert qgdev.export_lines({"A": "x y"}) == ["export A='x y'"]


def test_cli_runs_the_command_under_the_lease(monkeypatch, tmp_path):
    fake = FakeDcp(waits=0)
    monkeypatch.setattr(qgdev, "_client", lambda _url: client_for(fake))
    out = tmp_path / "seen.txt"
    import sys
    script = ("import os,sys; open(sys.argv[1],'w').write("
              "os.environ['ANDROID_ADB_SERVER_PORT']+' '+sys.argv[2])")
    result = CliRunner().invoke(qgdev.main, [
        "lease", "--class", "mac-arm64-api35-gapis", "--", sys.executable, "-c", script,
        str(out), "{serial}"])
    assert result.exit_code == 0, result.output
    assert out.read_text() == "40001 emulator-5554"
    assert "export ANDROID_SERIAL=emulator-5554" in result.output
    created = next(b for m, p, b in fake.calls if p == "/v1/requests")
    assert created["constraints"]["device_class"] == "mac-arm64-api35-gapis@1"
    assert any(p.endswith("/release") for _m, p, _b in fake.calls)


def test_cli_command_exit_code_is_propagated(monkeypatch):
    fake = FakeDcp(waits=0)
    monkeypatch.setattr(qgdev, "_client", lambda _url: client_for(fake))
    import sys
    result = CliRunner().invoke(qgdev.main, [
        "lease", "--class", "c@1", "--json", "--", sys.executable, "-c", "raise SystemExit(4)"])
    assert result.exit_code == 4
    line = next(ln for ln in result.output.splitlines() if ln.startswith("{"))
    assert json.loads(line)["env"]["ANDROID_SERIAL"] == "emulator-5554"
    assert any(p.endswith("/release") for _m, p, _b in fake.calls)


# -- QUA-3393 ------------------------------------------------------------------------


def test_sigint_after_grant_before_the_command_does_not_run_it(monkeypatch, tmp_path):
    fake = FakeDcp(waits=0)
    inner = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        resp = inner(request)
        if request.url.path.endswith("/adb/allow"):
            # Ctrl-C after the grant, while qgdev is still admitting this host.
            signal.raise_signal(signal.SIGINT)
        return resp

    monkeypatch.setattr(qgdev, "_client", lambda _url: qgdev.DcpClient(
        "http://dcp.test", transport=httpx.MockTransport(handler)))
    out = tmp_path / "ran.txt"
    result = CliRunner().invoke(qgdev.main, [
        "lease", "--class", "c@1", "--", sys.executable, "-c",
        "import sys; open(sys.argv[1], 'w').write('ran')", str(out)])
    assert not out.exists(), "the command ran after SIGINT"
    assert result.exit_code == 130, result.output
    assert any(p.endswith("/release") for _m, p, _b in fake.calls)


def test_ios_platform_is_refused_before_any_request(monkeypatch):
    fake = FakeDcp(waits=0)
    monkeypatch.setattr(qgdev, "_client", lambda _url: client_for(fake))
    result = CliRunner().invoke(qgdev.main, ["lease", "--class", "c@1", "--platform", "ios"])
    assert result.exit_code == 2, result.output
    assert "Android device classes only" in result.output
    assert fake.calls == []  # no request created, no lease held


def test_descriptor_file_ignores_a_leftover_world_readable_temp(tmp_path):
    lease = qgdev.lease_from_request(FakeDcp().view("ready"))
    path = tmp_path / "descriptors.json"
    stale = tmp_path / "descriptors.json.tmp"
    stale.write_text("old")
    os.chmod(stale, 0o644)
    qgdev.write_descriptor_file(path, lease)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"descriptors": [DESCRIPTOR]}
    assert stale.read_text() == "old"  # never reused
    assert sorted(p.name for p in tmp_path.iterdir()) == ["descriptors.json", "descriptors.json.tmp"]



def test_dcp_url_is_required(monkeypatch):
    # The DCP is internal, so this public package carries no default URL.
    import click
    import pytest
    from qualgentbench.qgdev import dcp_url_from_env

    monkeypatch.delenv("QGDEV_DCP_URL", raising=False)
    with pytest.raises(click.UsageError):
        dcp_url_from_env(None)
    monkeypatch.setenv("QGDEV_DCP_URL", "https://dcp.example.test/")
    assert dcp_url_from_env(None) == "https://dcp.example.test"
    assert dcp_url_from_env("https://other.example.test") == "https://other.example.test"
