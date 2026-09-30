"""QUA-2851 spike: throwaway fake QualGent API. NOT production code — QUA-2852 owns the real one.

Logs every request (method, path, query, headers minus the key, JSON body) as one JSON
line to --log, answers plausible empty/success responses, and records every
`POST /v1/test-cases` body to --captures/<n>.json. Unknown routes answer 404 so the log
shows exactly what the creator reached for.

    python fake_api.py --port 18731 --log /tmp/requests.jsonl --captures /tmp/captures
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CASES: dict[str, dict] = {}


def _reply(handler: BaseHTTPRequestHandler, method: str, path: str, body):
    if method == "GET" and path == "/v1/test-cases/list":
        return 200, [
            {"id": cid, "name": c["name"], "status": "active", "priority": c.get("priority"),
             "category": None, "latest_completed_run": None}
            for cid, c in CASES.items()
        ]
    if method == "GET" and path.startswith("/v1/test-cases/") and path.count("/") == 3:
        cid = path.rsplit("/", 1)[1]
        if cid in CASES:
            return 200, CASES[cid]
        return 404, {"detail": "not found"}
    if method == "POST" and path == "/v1/test-cases":
        cid = str(uuid.uuid4())
        vid = str(uuid.uuid4())
        CASES[cid] = {"id": cid, "version_id": vid, **(body or {})}
        return 201, {"id": cid, "name": (body or {}).get("name", ""), "version_id": vid,
                     "files": []}
    if method == "PATCH" and path.startswith("/v1/test-cases/"):
        cid = path.rsplit("/", 1)[1]
        if cid not in CASES:
            return 404, {"detail": "not found"}
        CASES[cid].update(body or {})
        return 200, {"id": cid, "name": CASES[cid].get("name", ""),
                     "version_id": str(uuid.uuid4()), "files": []}
    if method == "GET" and path == "/v1/categories/list":
        return 200, []
    if method == "GET" and path == "/v1/credentials/list":
        return 200, {}
    if method == "GET" and path == "/v1/apps/list":
        return 200, []
    if method == "GET" and path == "/v1/devices":
        return 200, {"devices": []}
    if method == "POST" and path == "/v1/credits/validate":
        return 200, {"has_sufficient_credits": True, "available_credits": 1000,
                     "required_credits": (body or {}).get("required_credits", 1)}
    return 404, {"detail": f"fake API: no route {method} {path}"}


def make_handler(log_path: Path, captures: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet stderr
            pass

        def _handle(self, method: str):
            parts = urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "")
            body = None
            if raw and "json" in ctype:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_unparsed": raw.decode(errors="replace")}
            status, payload = _reply(self, method, parts.path, body)
            entry = {
                "ts": time.time(), "method": method, "path": parts.path,
                "query": parse_qs(parts.query), "status": status,
                "has_api_key": bool(self.headers.get("x-api-key")),
                "content_type": ctype, "body": body,
                "raw_bytes": len(raw) if body is None else None,
            }
            with log_path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
            if method == "POST" and parts.path == "/v1/test-cases" and status == 201:
                k = len(list(captures.glob("*.json"))) + 1
                (captures / f"{k:02d}.json").write_text(json.dumps(
                    {"request": body, "response": payload}, indent=2) + "\n")
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18731)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--captures", type=Path, required=True)
    a = ap.parse_args()
    a.log.parent.mkdir(parents=True, exist_ok=True)
    a.captures.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.log, a.captures))
    print(f"fake QualGent API on http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
