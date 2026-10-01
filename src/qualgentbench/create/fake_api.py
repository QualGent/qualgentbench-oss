"""CreateBench v2: a local fake of the QualGent REST API (QUA-2852).

A creation episode runs the REAL QualGent-MCP server (at an arm's pinned ref, see
`arm.py`) so the authoring surface is the product's own. That server talks HTTP to
whatever `QUALGENT_API_URL` names; here it names this in-process fake instead of our
backend. That takes auth, org data and network out of the loop (the August failure:
a bench key with no profile row answered 500 to every call), and it starts every
episode in an EMPTY workspace, so an author cannot copy existing coverage.

What it serves — generic routes only, shapes taken from the public behaviour of the
API as QualGent-MCP consumes it, never any private text:

* `POST /v1/test-cases` → 201 `{id, name, version_id, files: []}`. The body is
  validated the way the product validates it (422 on a missing name, an empty step
  list, a blank step or an unknown `kind`), recorded, and becomes the episode's
  authored case (`authored_case.json`).
* `PATCH /v1/test-cases/{id}` → a new version: 200 `{id, version_number,
  version_id, files: []}`.
* `GET /v1/test-cases/list` and `GET /v1/test-cases/{id}` → the cases created in
  THIS episode (the workspace starts empty); a stored case's `steps` is the
  product's numbered string, as the real API returns it.
* `GET /v1/categories/list`, `GET /v1/credentials/list` → empty (`{}`, the product's
  empty shape, which QualGent-MCP turns into an empty list or its "no credentials"
  sentence).
* `GET /v1/apps/list` → the one app the episode is about (or empty);
  `GET /v1/apps/{id|latest}` → that app.
* `POST /v1/credits/validate` → always sufficient.
* anything else → 404 `{"detail": …}`, logged as a WARNING and flagged
  `unknown: true` in the request log, so a QualGent-MCP route change (drift) or an
  off-surface tool (`run_tests`, `upload_app`, …) is visible, never silent.

Files, all under the episode dir the fake is given (a runs dir, outside the repo):

* `api/requests.jsonl` — every request: method, path, query, status, the matched
  route, whether an API key was sent (never its value), the JSON body.
* `api/writes/NN-create.json`, `NN-update.json` — each accepted write, request and
  response.
* `api/summary.json` — written on stop: request count, unknown routes, cases.
* `authored_case.json` (`AUTHORED_CASE_FILE`) — the artifact contract QUA-2856 and
  QUA-2857 read: the exact create body, every later update, the case as it stands
  and its product serialization (`serialize_steps`). When an author creates more than
  one case the file holds the LAST one created and `cases_created` says how many.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

AUTHORED_CASE_FILE = "authored_case.json"
AUTHORED_CASE_SCHEMA = "qualgentbench.create.authored_case/1"
API_DIR = "api"
REQUEST_LOG = "requests.jsonl"
SUMMARY_FILE = "summary.json"

STEP_KINDS = ("setup", "act", "verify")
# Fields the product's create/update models know. Anything else is ignored by the
# product (pydantic's default), so it is ignored here too — but kept in the recorded
# request, which is the exact body.
_CASE_FIELDS = ("name", "steps", "expected_result", "priority", "description",
                "category_id", "variables", "file_attachments", "file_ids")

_TEST_CASE_ID_RE = re.compile(r"^/v1/test-cases/([^/]+)$")
_APP_ID_RE = re.compile(r"^/v1/apps/([^/]+)$")


def serialize_steps(steps: list[dict[str, Any]]) -> str:
    """Structured steps → the numbered string the product stores and returns.

    One line per step, `N. [kind] description ## {credential-uuid}`: the bracketed
    kind only when the step has one, the credential reference only when it has one.
    Mirrors the API's own serializer (and is the inverse of
    `create.lint.parse_serialized_steps`), so the authored artifact carries the form
    a product-side consumer of the stored case would see.
    """
    lines = []
    for i, step in enumerate(steps, 1):
        kind = step.get("kind")
        prefix = f"[{kind}] " if kind else ""
        line = f"{i}. {prefix}{step.get('description', '')}"
        if step.get("credential_id"):
            line += f" ## {{{step['credential_id']}}}"
        lines.append(line)
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── validation (the product's request models, restated) ───────────────────────

def _err(loc: list[Any], msg: str, kind: str = "value_error") -> dict[str, Any]:
    return {"type": kind, "loc": ["body", *loc], "msg": msg}


def _validate_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        return [_err(["steps"], "Input should be a valid list", "list_type")]
    if not steps:
        return [_err(["steps"], "List should have at least 1 item after validation",
                     "too_short")]
    errors = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(_err(["steps", i], "Input should be a valid dictionary",
                               "model_type"))
            continue
        desc = step.get("description")
        if not isinstance(desc, str):
            errors.append(_err(["steps", i, "description"], "Field required", "missing"))
        elif not desc:
            errors.append(_err(["steps", i, "description"],
                               "String should have at least 1 character", "string_too_short"))
        kind = step.get("kind")
        if kind is not None and kind not in STEP_KINDS:
            errors.append(_err(["steps", i, "kind"],
                               "Input should be 'setup', 'act' or 'verify'", "literal_error"))
        cred = step.get("credential_id")
        if cred is not None and not isinstance(cred, str):
            errors.append(_err(["steps", i, "credential_id"],
                               "Input should be a valid string", "string_type"))
    return errors


def validate_create(body: Any) -> list[dict[str, Any]]:
    """422 details for a create body, [] when the product would accept it."""
    if not isinstance(body, dict):
        return [{"type": "model_attributes_type", "loc": ["body"],
                 "msg": "Input should be a valid dictionary"}]
    errors = []
    for key in ("name", "expected_result"):
        if key not in body:
            errors.append(_err([key], "Field required", "missing"))
        elif not isinstance(body[key], str):
            errors.append(_err([key], "Input should be a valid string", "string_type"))
    if "steps" not in body:
        errors.append(_err(["steps"], "Field required", "missing"))
    else:
        errors += _validate_steps(body["steps"])
    return errors


def validate_update(body: Any) -> list[dict[str, Any]]:
    """422 details for a partial update body (every field optional)."""
    if not isinstance(body, dict):
        return [{"type": "model_attributes_type", "loc": ["body"],
                 "msg": "Input should be a valid dictionary"}]
    errors = []
    for key in ("name", "expected_result"):
        if body.get(key) is not None and not isinstance(body[key], str):
            errors.append(_err([key], "Input should be a valid string", "string_type"))
    if body.get("steps") is not None:
        errors += _validate_steps(body["steps"])
    return errors


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class FakeApp:
    """The one app the episode's workspace holds (`GET /v1/apps/list`)."""
    name: str
    version: str | None = None
    os: str = "android"
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"qualgentbench-app:{self.name}"))

    def as_api(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "version": self.version, "os": self.os}


@dataclass
class _Case:
    id: str
    created_at: str
    request: dict[str, Any]
    fields: dict[str, Any]
    versions: list[dict[str, Any]]           # [{id, version_number, created_at, fields}]
    updates: list[dict[str, Any]]

    @property
    def latest(self) -> dict[str, Any]:
        return self.versions[-1]

    def api_detail(self, version_id: str | None = None) -> dict[str, Any] | None:
        version = self.latest
        if version_id:
            version = next((v for v in self.versions if v["id"] == version_id), None)
            if version is None:
                return None
        f = version["fields"]
        return {
            "id": self.id,
            "name": f.get("name", ""),
            "steps": serialize_steps(f.get("steps") or []),
            "expected_result": f.get("expected_result", ""),
            "description": f.get("description"),
            "priority": f.get("priority"),
            "status": "active",
            "created_at": self.created_at,
            "updated_at": version["created_at"],
            "category": None,
            "versions": [{"id": v["id"], "version_number": v["version_number"],
                          "created_at": v["created_at"]} for v in self.versions],
            "variables": [],
            "files": [],
        }


def _case_fields(body: dict[str, Any]) -> dict[str, Any]:
    fields = {k: body[k] for k in _CASE_FIELDS if k in body}
    fields.setdefault("priority", "Medium")          # the product's default
    return fields


# ── the server ────────────────────────────────────────────────────────────────

class FakeQualGentAPI:
    """In-process HTTP fake of the QualGent API for ONE creation episode.

        with FakeQualGentAPI(episode_dir, app=FakeApp("MedTimer")) as api:
            env = {"QUALGENT_API_URL": api.url, ...}
            ...  # run QualGent-MCP against it

    Binds 127.0.0.1 on a free port unless `port` is given. Thread-safe: QualGent-MCP
    issues requests from one process, but the server answers each on its own thread.
    """

    def __init__(self, episode_dir: str | Path, *, app: FakeApp | None = None,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.episode_dir = Path(episode_dir)
        self.api_dir = self.episode_dir / API_DIR
        self.app = app
        self._host, self._port = host, port
        self._lock = threading.Lock()
        self._cases: dict[str, _Case] = {}
        self._order: list[str] = []            # case ids in creation order
        self._seq = 0
        self._writes = 0
        self.unknown: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # lifecycle

    def start(self) -> FakeQualGentAPI:
        self.api_dir.mkdir(parents=True, exist_ok=True)
        (self.api_dir / "writes").mkdir(exist_ok=True)
        self._server = ThreadingHTTPServer((self._host, self._port), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name="fake-qualgent-api", daemon=True)
        self._thread.start()
        logger.info("fake QualGent API on %s (episode %s)", self.url, self.episode_dir)
        return self

    def stop(self) -> dict[str, Any]:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        summary = self.summary()
        (self.api_dir / SUMMARY_FILE).write_text(json.dumps(summary, indent=2) + "\n")
        return summary

    def __enter__(self) -> FakeQualGentAPI:  # noqa: PYI034
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the fake API is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # results

    @property
    def authored_case_path(self) -> Path:
        return self.episode_dir / AUTHORED_CASE_FILE

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": self._seq,
                "unknown_routes": [f"{u['method']} {u['path']}" for u in self.unknown],
                "cases_created": len(self._order),
                "case_ids": list(self._order),
                "authored_case": (AUTHORED_CASE_FILE if self._order else None),
            }

    # routing

    def route(self, method: str, path: str, query: dict[str, list[str]],
              body: Any) -> tuple[int, Any, str | None]:
        """(status, payload, route name); route None = no such route."""
        path = path.rstrip("/") or "/"
        if method == "GET" and path == "/v1/test-cases/list":
            return 200, self._list_cases((query.get("category") or [None])[0]), \
                "list_test_cases"
        if method == "POST" and path == "/v1/test-cases":
            return (*self._create(body), "create_test_case")
        if m := _TEST_CASE_ID_RE.match(path):
            case_id = m.group(1)
            if method == "GET" and case_id not in ("list", "run", "run-all"):
                version = (query.get("version_id") or [None])[0]
                with self._lock:
                    case = self._cases.get(case_id)
                    detail = case.api_detail(version) if case else None
                if detail is None:
                    return 404, {"detail": "Test case not found"}, "get_test_case"
                return 200, detail, "get_test_case"
            if method == "PATCH":
                return (*self._update(case_id, body), "update_test_case")
        if method == "GET" and path == "/v1/categories/list":
            return 200, {}, "list_categories"
        if method == "GET" and path == "/v1/credentials/list":
            return 200, {}, "list_credentials"
        if method == "GET" and path == "/v1/apps/list":
            return 200, ([self.app.as_api()] if self.app else {}), "list_apps"
        if method == "GET" and (m := _APP_ID_RE.match(path)):
            if self.app and m.group(1) in (self.app.id, "latest"):
                return 200, self.app.as_api(), "get_app"
            return 404, {"detail": "App not found"}, "get_app"
        if method == "POST" and path == "/v1/credits/validate":
            required = body.get("required_credits", 1) if isinstance(body, dict) else 1
            return 200, {"has_sufficient_credits": True, "available_credits": 1000,
                         "required_credits": required}, "check_credits"
        return 404, {"detail": f"fake QualGent API: no route for {method} {path}"}, None

    def _list_cases(self, category: str | None) -> Any:
        with self._lock:
            rows = [
                {"id": c.id, "name": c.latest["fields"].get("name", ""), "status": "active",
                 "category": None, "latest_completed_run": None}
                for cid in self._order
                if (c := self._cases[cid])
                and (category is None or c.latest["fields"].get("category_id") == category)
            ]
        return rows or {}                     # the product answers {} for none

    def _create(self, body: Any) -> tuple[int, Any]:
        errors = validate_create(body)
        if errors:
            return 422, {"detail": errors}
        now = _now()
        case_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
        fields = _case_fields(body)
        case = _Case(id=case_id, created_at=now, request=body, updates=[],
                     fields=fields,
                     versions=[{"id": version_id, "version_number": 1,
                                "created_at": now, "fields": fields}])
        response = {"id": case_id, "name": body["name"], "version_id": version_id,
                    "files": []}
        with self._lock:
            self._cases[case_id] = case
            self._order.append(case_id)
            self._record_write("create", body, response)
            self._write_authored(case)
        return 201, response

    def _update(self, case_id: str, body: Any) -> tuple[int, Any]:
        with self._lock:
            case = self._cases.get(case_id)
        if case is None:
            return 404, {"detail": "Test case not found"}
        errors = validate_update(body)
        if errors:
            return 422, {"detail": errors}
        now = _now()
        fields = dict(case.latest["fields"])
        for key in _CASE_FIELDS:
            if key in body and (body[key] is not None or key == "category_id"):
                fields[key] = body[key]
        version = {"id": str(uuid.uuid4()), "version_number": len(case.versions) + 1,
                   "created_at": now, "fields": fields}
        response = {"id": case_id, "version_number": version["version_number"],
                    "version_id": version["id"], "files": []}
        with self._lock:
            case.versions.append(version)
            case.fields = fields
            case.updates.append(body)
            self._record_write("update", body, response)
            if self._order and self._order[-1] == case_id:
                self._write_authored(case)
        return 200, response

    # artifacts (called with the lock held)

    def _record_write(self, kind: str, body: Any, response: Any) -> None:
        self._writes += 1
        path = self.api_dir / "writes" / f"{self._writes:02d}-{kind}.json"
        path.write_text(json.dumps({"request": body, "response": response}, indent=2) + "\n")

    def _write_authored(self, case: _Case) -> None:
        fields = case.latest["fields"]
        artifact = {
            "schema": AUTHORED_CASE_SCHEMA,
            "test_case_id": case.id,
            "version_id": case.latest["id"],
            "version_number": case.latest["version_number"],
            "created_at": case.created_at,
            "updated_at": case.latest["created_at"],
            "cases_created": len(self._order),
            # The exact body QualGent-MCP posted, as the product received it.
            "request": case.request,
            # Every later PATCH body, in order (each made a new version).
            "updates": case.updates,
            # The case as it now stands (create body + updates, product defaults).
            "case": fields,
            # The product serialization of `case.steps`.
            "serialized_steps": serialize_steps(fields.get("steps") or []),
        }
        tmp = self.authored_case_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.authored_case_path)

    # HTTP plumbing

    def _log(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, **entry}
            if entry.get("unknown"):
                self.unknown.append(entry)
            with (self.api_dir / REQUEST_LOG).open("a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:     # the request log is ours
                pass

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                body: Any = None
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = None
                try:
                    status, payload, route = fake.route(
                        method, parts.path, parse_qs(parts.query), body)
                except Exception as exc:  # a fake must answer, not hang
                    logger.exception("fake QualGent API crashed on %s %s", method, self.path)
                    status, payload, route = 500, {"detail": f"fake API error: {exc}"}, None
                unknown = route is None and status == 404
                if unknown:
                    logger.warning(
                        "fake QualGent API: NO ROUTE for %s %s — QualGent-MCP route "
                        "drift, or the author reached for an off-surface tool",
                        method, parts.path)
                fake._log({
                    "ts": time.time(), "method": method, "path": parts.path,
                    "query": parse_qs(parts.query), "status": status, "route": route,
                    "unknown": unknown,
                    "has_api_key": bool(self.headers.get("x-api-key")),
                    "content_type": ctype,
                    "body": body,
                    "raw_bytes": len(raw) if raw and body is None else None,
                })
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        return Handler
