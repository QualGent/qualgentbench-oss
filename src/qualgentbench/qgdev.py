"""`qgdev`: lease a farm device from the Device Control Plane (DCP) for a bench board
(QUA-3381, DCP spec 6.10 "Bench CLI").

    qgdev lease --class mac-arm64-api35-gapis@1
    qgdev lease --class mac-arm64-api35-gapis@1 -- \\
        uv run qualgent-bench run --agent codex-cli --app birday --device {serial}

`lease` creates a `benchmark`-class request (D27: never a farm phone), waits for the
grant, admits this machine to the lease's adb proxy (`POST <host_api>/adb/allow`),
prints the adb server host/port/serial as shell exports, heartbeats every 20 s and
releases the lease on exit, SIGINT or SIGTERM.

With a command after `--`, the command runs with the exports in its environment
(`{serial}` in an argument becomes the leased serial) and the lease is released when
it exits; qgdev exits with the command's code. Without one, qgdev holds the lease
until interrupted (eval the exports in another shell).

The bench needs no other change: every harness `adb` call and the adb meter's
upstream already follow ANDROID_ADB_SERVER_ADDRESS/PORT (adb_meter.upstream_from_env),
and `run` without `--device` picks the first device the (remote) adb server lists,
which through a lease proxy is the leased one.

Auth: over the tailnet, `tailscale serve` in front of the DCP adds the caller's
Tailscale-User-Login (D22), so a person needs nothing but a row in the DCP's
tailnet_users. A service token (`dcp_svc_...`) can be given in QGDEV_DCP_TOKEN
instead, for example against a local DCP.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import httpx

DCP_URL_ENV = "QGDEV_DCP_URL"
DCP_TOKEN_ENV = "QGDEV_DCP_TOKEN"
HEARTBEAT_SECONDS = 20.0
LONG_POLL_SECONDS = 20
HTTP_TIMEOUT_S = 30.0

DEPLOY_ENVS = ("production", "uat", "staging", "dev")


class DcpError(RuntimeError):
    """A DCP call failed, or the request/lease ended without a usable device."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def dcp_url_from_env(explicit: str | None = None) -> str:
    """The DCP base URL from --dcp-url or QGDEV_DCP_URL. There is no default: the DCP is internal."""
    url = (explicit or os.environ.get(DCP_URL_ENV) or "").strip()
    if not url:
        raise click.UsageError(f"set {DCP_URL_ENV} or pass --dcp-url (the Device Control Plane base URL)")
    return url.rstrip("/")


def normalize_class_ref(value: str) -> str:
    """`key@version`; a bare key means version 1 (the catalog's only version today)."""
    value = value.strip()
    if not value:
        raise click.BadParameter("a device class is required, for example mac-arm64-api35-gapis@1")
    return value if "@" in value else f"{value}@1"


class DcpClient:
    """The few DCP calls a lease holder makes (spec 6.4): requests and leases."""

    def __init__(self, base_url: str, *, token: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        headers = {"user-agent": "qgdev"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, headers=headers,
                                  timeout=HTTP_TIMEOUT_S, transport=transport)
        # host_api calls carry the lease token, never the DCP credential.
        self._host = httpx.Client(timeout=HTTP_TIMEOUT_S, transport=transport,
                                  headers={"user-agent": "qgdev"})

    def close(self) -> None:
        self._http.close()
        self._host.close()

    def _call(self, method: str, path: str, *, json_body: Any = None,
              params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        try:
            resp = self._http.request(method, path, json=json_body, params=params,
                                      timeout=timeout or HTTP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise DcpError(f"DCP {method} {path} failed: {exc} (is {self.base_url} reachable "
                           f"on the tailnet?)") from exc
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            code = body.get("error") if isinstance(body, dict) else None
            msg = body.get("message") if isinstance(body, dict) else None
            raise DcpError(f"DCP {method} {path}: HTTP {resp.status_code} {code or ''} "
                           f"{msg or resp.text[:200]}".strip(), status=resp.status_code, code=code)
        return body if isinstance(body, dict) else {}

    def whoami(self) -> dict[str, Any]:
        return self._call("GET", "/v1/whoami")

    def create_request(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/v1/requests", json_body=body)

    def get_request(self, request_id: str, wait: int = 0) -> dict[str, Any]:
        params = {"wait": wait} if wait > 0 else None
        return self._call("GET", f"/v1/requests/{request_id}", params=params,
                          timeout=HTTP_TIMEOUT_S + wait)

    def cancel_request(self, request_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/requests/{request_id}/cancel", json_body={})

    def heartbeat(self, lease_id: str, fencing_token: int, activity: bool = False) -> dict[str, Any]:
        return self._call("POST", f"/v1/leases/{lease_id}/heartbeat",
                          json_body={"fencing_token": fencing_token, "activity": activity})

    def release(self, lease_id: str, fencing_token: int) -> dict[str, Any]:
        return self._call("POST", f"/v1/leases/{lease_id}/release",
                          json_body={"fencing_token": fencing_token})

    def adb_allow(self, host_api_url: str, token: str) -> dict[str, Any]:
        url = host_api_url.rstrip("/") + "/adb/allow"
        try:
            resp = self._host.post(url, headers={"authorization": f"Bearer {token}"}, json={})
        except httpx.HTTPError as exc:
            raise DcpError(f"farm host {url} unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DcpError(f"farm host adb/allow: HTTP {resp.status_code} {resp.text[:200]}",
                           status=resp.status_code)
        try:
            return resp.json()
        except ValueError:
            return {}


@dataclass
class Lease:
    request_id: str
    lease_id: str
    fencing_token: int
    connection: dict[str, Any] = field(repr=False)
    idle_deadline: str | None = None
    hard_deadline: str | None = None

    @property
    def transport(self) -> dict[str, Any]:
        return self.connection.get("transport") or {}

    @property
    def device(self) -> dict[str, Any]:
        return self.connection.get("device") or {}

    @property
    def host_api(self) -> dict[str, Any] | None:
        return self.connection.get("host_api")


def request_body(device_class: str, *, platform: str, deploy_env: str,
                 wait_seconds: int | None, consumer_ref: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "priority_class": "benchmark",
        "deploy_env": deploy_env,
        "lease_mode": "remote",
        "interactive": False,
        # device_class pins the class; every other constraint is ignored (spec 6.11).
        "constraints": {"platform": platform, "device_class": device_class},
        "idempotency_key": f"qgdev-{uuid.uuid4()}",
    }
    if wait_seconds is not None:
        body["wait_seconds"] = wait_seconds
    if consumer_ref:
        body["consumer_ref"] = consumer_ref[:256]
    return body


def lease_from_request(view: dict[str, Any]) -> Lease | None:
    """The ready lease in a request view, or None while it is still coming."""
    lease = view.get("lease")
    if not isinstance(lease, dict) or lease.get("state") not in ("ready", "active"):
        return None
    connection = lease.get("connection")
    if not isinstance(connection, dict):
        return None
    return Lease(request_id=view["id"], lease_id=lease["id"],
                 fencing_token=int(lease["fencing_token"]), connection=connection,
                 idle_deadline=lease.get("idle_deadline"),
                 hard_deadline=lease.get("hard_deadline"))


def wait_for_lease(client: DcpClient, request_id: str, *,
                   on_wait: Callable[[dict[str, Any]], None] = lambda _v: None,
                   stop: threading.Event | None = None,
                   poll_seconds: int = LONG_POLL_SECONDS) -> Lease:
    """Long-poll the request until its lease is ready. Raises DcpError when the request
    expires or is cancelled, or the lease ends during prepare."""
    while True:
        if stop is not None and stop.is_set():
            raise DcpError("interrupted while waiting for a device", code="interrupted")
        view = client.get_request(request_id, wait=poll_seconds)
        lease = lease_from_request(view)
        if lease is not None:
            return lease
        state = view.get("state")
        lease_view = view.get("lease") if isinstance(view.get("lease"), dict) else None
        if state in ("expired", "cancelled"):
            raise DcpError(f"request {request_id} {state} ({view.get('end_reason') or 'no reason'})",
                           code=view.get("end_reason") or state)
        if lease_view and lease_view.get("state") in ("releasing", "ended") and state != "waiting":
            raise DcpError(f"lease {lease_view.get('id')} ended during prepare "
                           f"({lease_view.get('end_reason') or 'unknown'})",
                           code=lease_view.get("end_reason"))
        on_wait(view)


def adb_env(lease: Lease) -> dict[str, str]:
    """Environment for every adb client the bench uses: the adb CLI reads
    ANDROID_ADB_SERVER_ADDRESS/PORT, adbutils/uiautomator2 ANDROID_ADB_SERVER_HOST/PORT."""
    t = lease.transport
    if t.get("kind") != "adb_server":
        raise DcpError(f"lease {lease.lease_id} is not an adb lease (transport {t.get('kind')!r}); "
                       "qgdev serves Android device classes")
    host, port, serial = t.get("host"), t.get("port"), t.get("serial")
    if not host or not port or not serial:
        raise DcpError(f"lease {lease.lease_id} descriptor has no adb host/port/serial")
    env = {
        "ANDROID_ADB_SERVER_ADDRESS": str(host),
        "ANDROID_ADB_SERVER_HOST": str(host),
        "ANDROID_ADB_SERVER_PORT": str(port),
        "ANDROID_SERIAL": str(serial),
        "QGDEV_LEASE_ID": lease.lease_id,
        "QGDEV_DEVICE_CLASS": str(lease.device.get("class") or ""),
    }
    if t.get("avd_name"):
        env["QGDEV_AVD_NAME"] = str(t["avd_name"])
    # The bench stamps provenance.device_class from QGB_DEVICE_CLASS (result.device_class),
    # a bare key: the DCP class key without @version.
    class_key = str(lease.device.get("class") or "").split("@", 1)[0]
    if class_key:
        env["QGB_DEVICE_CLASS"] = class_key
    return env


def export_lines(env: dict[str, str]) -> list[str]:
    return [f"export {k}={shlex.quote(v)}" for k, v in env.items()]


def write_descriptor_file(path: Path, lease: Lease) -> None:
    """DEVLOOP_FARM_DESCRIPTOR_FILE for a DevLoop-MCP the board runs (QUA-3378): written
    at acquire (0600, it holds the lease token), removed at release."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fresh, uniquely named temp file (mkstemp creates it 0600, O_EXCL): a fixed
    # `.tmp` name reused a file some earlier run or another user left, whose mode
    # O_CREAT never tightens (QUA-3393).
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"descriptors": [lease.connection]}, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Heartbeat(threading.Thread):
    """Keeps the lease alive (TTL 60 s, every 20 s). Benchmark leases are not
    interactive, so no activity pings. Sets `ended` when the DCP says the lease is over."""

    def __init__(self, client: DcpClient, lease: Lease, *, every: float = HEARTBEAT_SECONDS,
                 on_end: Callable[[str], None] = lambda _r: None):
        super().__init__(name="qgdev-heartbeat", daemon=True)
        self.client, self.lease, self.every, self.on_end = client, lease, every, on_end
        self.stop_event = threading.Event()
        self.ended: str | None = None

    def beat(self) -> None:
        try:
            self.client.heartbeat(self.lease.lease_id, self.lease.fencing_token)
        except DcpError as exc:
            if exc.status in (403, 404, 409):
                self.ended = exc.code or str(exc)
                self.on_end(self.ended)
                self.stop_event.set()
            else:
                click.echo(f"qgdev: heartbeat failed (will retry): {exc}", err=True)

    def run(self) -> None:
        while not self.stop_event.wait(self.every):
            self.beat()


class LeaseSession:
    """Create, wait, hold, release. `release()` is idempotent and safe from a signal path."""

    def __init__(self, client: DcpClient, body: dict[str, Any], *, log: Callable[[str], None]):
        self.client, self.body, self.log = client, body, log
        self.request_id: str | None = None
        self.lease: Lease | None = None
        self.heartbeat: Heartbeat | None = None
        self.descriptor_file: Path | None = None
        self.stop = threading.Event()
        self._released = False
        self._lock = threading.Lock()

    def acquire(self, *, on_end: Callable[[str], None] = lambda _r: None) -> Lease:
        view = self.client.create_request(self.body)
        self.request_id = view["id"]
        self.log(f"qgdev: request {self.request_id} ({view.get('state')}, class "
                 f"{self.body['constraints']['device_class']}, priority benchmark)")
        last = [None]

        def on_wait(v: dict[str, Any]) -> None:
            lease = v.get("lease") if isinstance(v.get("lease"), dict) else None
            status = f"{v.get('state')}/{lease.get('state') if lease else 'no lease'}"
            if status != last[0]:
                self.log(f"qgdev: waiting ({status})")
                last[0] = status

        self.lease = wait_for_lease(self.client, self.request_id, on_wait=on_wait, stop=self.stop)
        # First heartbeat moves ready -> active; then every 20 s.
        self.client.heartbeat(self.lease.lease_id, self.lease.fencing_token)
        host_api = self.lease.host_api
        if host_api and self.lease.transport.get("kind") == "adb_server":
            allowed = self.client.adb_allow(host_api["url"], host_api["token"])
            self.log(f"qgdev: adb proxy admits {allowed.get('allowed', 'this host')}")
        self.heartbeat = Heartbeat(self.client, self.lease, on_end=on_end)
        self.heartbeat.start()
        return self.lease

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self.stop.set()
        if self.heartbeat is not None:
            self.heartbeat.stop_event.set()
        if self.descriptor_file is not None:
            with contextlib.suppress(OSError):
                self.descriptor_file.unlink()
        try:
            if self.lease is not None:
                self.client.release(self.lease.lease_id, self.lease.fencing_token)
                self.log(f"qgdev: released lease {self.lease.lease_id}")
            elif self.request_id is not None:
                self.client.cancel_request(self.request_id)
                self.log(f"qgdev: cancelled request {self.request_id}")
        except DcpError as exc:
            self.log(f"qgdev: release failed: {exc}")


def substitute(argv: list[str], env: dict[str, str]) -> list[str]:
    serial = env["ANDROID_SERIAL"]
    return [a.replace("{serial}", serial) for a in argv]


# ── CLI ────────────────────────────────────────────────────────────────────────


@click.group()
def main() -> None:
    """Lease Device Control Plane farm devices from a laptop on the tailnet."""


def _client(dcp_url: str | None) -> DcpClient:
    return DcpClient(dcp_url_from_env(dcp_url), token=os.environ.get(DCP_TOKEN_ENV) or None)


@main.command()
@click.option("--dcp-url", default=None, help=f"DCP base URL (env {DCP_URL_ENV}; required).")
def whoami(dcp_url: str | None) -> None:
    """Who the DCP thinks you are (kind, role, allowed priority classes)."""
    client = _client(dcp_url)
    try:
        click.echo(json.dumps(client.whoami(), indent=2))
    except DcpError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        client.close()


@main.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--class", "device_class", required=True,
              help="Device class to pin, key@version (e.g. mac-arm64-api35-gapis@1). A bare key means @1.")
@click.option("--platform", type=click.Choice(["android", "ios"]), default="android", show_default=True,
              help="android only today: qgdev hands out an adb server; iOS is refused before any request.")
@click.option("--deploy-env", type=click.Choice(DEPLOY_ENVS), default="dev", show_default=True,
              help="Orders requests within the benchmark class (production first).")
@click.option("--wait-seconds", type=int, default=None,
              help="Give up after this long with no device (default: benchmark requests wait until cancelled).")
@click.option("--dcp-url", default=None, help=f"DCP base URL (env {DCP_URL_ENV}; required).")
@click.option("--descriptor-file", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Also write the lease descriptor here for a DevLoop-MCP (DEVLOOP_FARM_DESCRIPTOR_FILE); "
                   "removed at release.")
@click.option("--json", "as_json", is_flag=True, help="Print the connection as one JSON line instead of exports.")
@click.option("--consumer-ref", default=None, help="Your own label for the request (shown in the fleet view).")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def lease(device_class: str, platform: str, deploy_env: str, wait_seconds: int | None,
          dcp_url: str | None, descriptor_file: Path | None, as_json: bool,
          consumer_ref: str | None, command: tuple[str, ...]) -> None:
    """Lease one device of CLASS (priority benchmark), print its adb server, hold it.

    With COMMAND (after --), run it under the lease and release when it exits.
    """
    if platform != "android":
        # Refuse before a request exists: an iOS lease would be granted (and a farm
        # device held) only for adb_env to fail on its wda_http transport (QUA-3393).
        raise click.BadParameter("qgdev serves Android device classes only (it exports an adb "
                                 "server); iOS farm devices are not supported yet",
                                 param_hint="--platform")
    client = _client(dcp_url)
    body = request_body(normalize_class_ref(device_class), platform=platform,
                        deploy_env=deploy_env, wait_seconds=wait_seconds,
                        consumer_ref=consumer_ref or f"qgdev:{os.environ.get('USER', '')}")
    log = lambda m: click.echo(m, err=True)
    session = LeaseSession(client, body, log=log)
    child: list[subprocess.Popen | None] = [None]
    exit_code = [0]

    def on_end(reason: str) -> None:
        log(f"qgdev: the DCP ended the lease ({reason})")
        exit_code[0] = 3
        session.stop.set()
        if child[0] is not None and child[0].poll() is None:
            child[0].terminate()

    def on_signal(signum, _frame) -> None:
        session.stop.set()
        if child[0] is not None and child[0].poll() is None:
            # The child got the same SIGINT from the terminal; SIGTERM covers kill <pid>.
            if signum == signal.SIGTERM:
                child[0].terminate()
            return
        if session.lease is None:
            # Still waiting: make the long-poll loop stop at its next turn.
            raise KeyboardInterrupt

    old_int = signal.signal(signal.SIGINT, on_signal)
    old_term = signal.signal(signal.SIGTERM, on_signal)
    try:
        try:
            leased = session.acquire(on_end=on_end)
        except KeyboardInterrupt:
            raise click.ClickException("interrupted while waiting for a device")
        except DcpError as exc:
            raise click.ClickException(str(exc)) from exc
        env = adb_env(leased)
        if descriptor_file is not None:
            write_descriptor_file(descriptor_file, leased)
            session.descriptor_file = descriptor_file
            env["DEVLOOP_FARM_DESCRIPTOR_FILE"] = str(descriptor_file)
        if as_json:
            click.echo(json.dumps({"lease_id": leased.lease_id, "request_id": leased.request_id,
                                   "device": leased.device, "env": env,
                                   "idle_deadline": leased.idle_deadline,
                                   "hard_deadline": leased.hard_deadline}))
        else:
            click.echo("\n".join(export_lines(env)))
            log(f"qgdev: leased {env['ANDROID_SERIAL']} ({leased.device.get('class')}) via "
                f"{env['ANDROID_ADB_SERVER_ADDRESS']}:{env['ANDROID_ADB_SERVER_PORT']}; "
                f"bench: --device {env['ANDROID_SERIAL']}")
        sys.stdout.flush()
        if command and session.stop.is_set():
            # SIGINT/SIGTERM (or the DCP ending the lease) after the grant but before the
            # command started: do not start it (QUA-3393).
            log("qgdev: stopped before the command started; not running it")
            if exit_code[0] == 0:
                exit_code[0] = 130
        elif command:
            argv = substitute(list(command), env)
            log(f"qgdev: running {shlex.join(argv)}")
            child[0] = subprocess.Popen(argv, env={**os.environ, **env})
            if session.stop.is_set() and child[0].poll() is None:
                # The stop landed between the check above and Popen: on_signal saw no
                # child, so end the one that just started.
                child[0].terminate()
            rc = child[0].wait()
            if exit_code[0] == 0:
                exit_code[0] = rc
        else:
            log("qgdev: holding the lease; Ctrl-C releases it")
            while not session.stop.wait(1.0):
                pass
    finally:
        session.release()
        client.close()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    sys.exit(exit_code[0])


if __name__ == "__main__":  # pragma: no cover
    main()
