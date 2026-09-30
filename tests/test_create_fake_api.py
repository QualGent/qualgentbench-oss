"""The fake QualGent API a creation episode's QualGent-MCP talks to (QUA-2852).

Contract: every request is recorded; `POST /v1/test-cases` is validated like the
product, answered with an id and captured as `authored_case.json` (exact body +
product serialization); `PATCH` makes a new version; the workspace starts empty;
unknown routes are 404 and logged loudly.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from qualgentbench.create.fake_api import (
    AUTHORED_CASE_FILE,
    AUTHORED_CASE_SCHEMA,
    FakeApp,
    FakeQualGentAPI,
    serialize_steps,
)
from qualgentbench.create.lint import normalize_case, parse_serialized_steps

CRED = "123e4567-e89b-12d3-a456-426614174000"
BODY = {
    "name": "Add an item and find it in the list",
    "steps": [
        {"description": "Open the app", "kind": "setup"},
        {"description": "Tap \"Add\"", "kind": "act"},
        {"description": "Sign in", "kind": "setup", "credential_id": CRED},
        {"description": "Verify the new item is listed", "kind": "verify"},
    ],
    "expected_result": "The new item is in the list.",
    "priority": "High",
    "change_source": "mcp",
}


@pytest.fixture
def api(tmp_path):
    fake = FakeQualGentAPI(tmp_path / "ep", app=FakeApp("MedTimer", version="1.0"))
    fake.start()
    client = httpx.Client(base_url=fake.url, headers={"x-api-key": "qg_secret_value"})
    yield fake, client
    client.close()
    fake.stop()


def _log(fake: FakeQualGentAPI) -> list[dict]:
    return [json.loads(line) for line in
            (fake.api_dir / "requests.jsonl").read_text().splitlines()]


def test_serialize_steps_matches_the_product_form():
    assert serialize_steps(BODY["steps"]) == (
        "1. [setup] Open the app\n"
        "2. [act] Tap \"Add\"\n"
        f"3. [setup] Sign in ## {{{CRED}}}\n"
        "4. [verify] Verify the new item is listed")
    assert serialize_steps([{"description": "Untagged"}]) == "1. Untagged"


def test_serialization_round_trips_through_the_lint_parser():
    parsed = parse_serialized_steps(serialize_steps(BODY["steps"]))
    assert [(s["description"].split(" ##")[0], s["kind"]) for s in parsed] == \
        [(s["description"], s["kind"]) for s in BODY["steps"]]
    assert parsed[2]["credential_id"] == CRED


def test_workspace_starts_empty(api):
    _, c = api
    for path in ("/v1/test-cases/list", "/v1/categories/list", "/v1/credentials/list"):
        r = c.get(path)
        assert (r.status_code, r.json()) == (200, {}), path   # the product's empty shape
    apps = c.get("/v1/apps/list").json()
    assert [a["name"] for a in apps] == ["MedTimer"] and apps[0]["os"] == "android"
    assert c.get(f"/v1/apps/{apps[0]['id']}").json() == apps[0]
    assert c.get("/v1/apps/latest").json() == apps[0]
    credits = c.post("/v1/credits/validate", json={"required_credits": 3}).json()
    assert credits["has_sufficient_credits"] is True and credits["required_credits"] == 3


def test_no_app_means_an_empty_app_list(tmp_path):
    with FakeQualGentAPI(tmp_path) as fake:
        assert httpx.get(f"{fake.url}/v1/apps/list").json() == {}


def test_create_records_echoes_and_captures_the_authored_case(api):
    fake, c = api
    r = c.post("/v1/test-cases", json=BODY)
    assert r.status_code == 201
    created = r.json()
    assert set(created) == {"id", "name", "version_id", "files"}
    assert created["name"] == BODY["name"] and created["files"] == []

    artifact = json.loads((fake.episode_dir / AUTHORED_CASE_FILE).read_text())
    assert artifact["schema"] == AUTHORED_CASE_SCHEMA
    assert artifact["test_case_id"] == created["id"]
    assert artifact["version_id"] == created["version_id"]
    assert artifact["version_number"] == 1
    assert artifact["request"] == BODY                      # the exact body posted
    assert artifact["serialized_steps"] == serialize_steps(BODY["steps"])
    assert artifact["cases_created"] == 1 and artifact["updates"] == []
    # The artifact is lintable as-is, and its serialized form parses to the same case.
    posted = normalize_case(artifact["request"]).steps
    stored = normalize_case({**BODY, "steps": artifact["serialized_steps"]}).steps
    assert [(s.kind, s.credential_id) for s in posted] == \
        [(s.kind, s.credential_id) for s in stored]
    assert [s.text for s in stored][:2] == [s.text for s in posted][:2]

    listed = c.get("/v1/test-cases/list").json()
    assert [(x["id"], x["name"]) for x in listed] == [(created["id"], BODY["name"])]
    detail = c.get(f"/v1/test-cases/{created['id']}").json()
    assert detail["steps"] == artifact["serialized_steps"]   # stored as the product stores
    assert detail["expected_result"] == BODY["expected_result"]
    assert [v["version_number"] for v in detail["versions"]] == [1]
    writes = sorted(p.name for p in (fake.api_dir / "writes").iterdir())
    assert writes == ["01-create.json"]


def test_update_makes_a_new_version_and_keeps_the_original_body(api):
    fake, c = api
    created = c.post("/v1/test-cases", json=BODY).json()
    patch = {"expected_result": "The item is listed once.", "change_source": "mcp"}
    r = c.patch(f"/v1/test-cases/{created['id']}", json=patch)
    assert r.status_code == 200
    assert r.json()["version_number"] == 2 and r.json()["version_id"] != created["version_id"]
    artifact = json.loads((fake.episode_dir / AUTHORED_CASE_FILE).read_text())
    assert artifact["request"] == BODY
    assert artifact["updates"] == [patch]
    assert artifact["version_number"] == 2
    assert artifact["case"]["expected_result"] == "The item is listed once."
    assert artifact["case"]["steps"] == BODY["steps"]
    # The first version stays readable.
    v1 = c.get(f"/v1/test-cases/{created['id']}",
               params={"version_id": created["version_id"]}).json()
    assert v1["expected_result"] == BODY["expected_result"]
    assert c.patch("/v1/test-cases/not-a-case", json=patch).status_code == 404


def test_the_last_created_case_is_the_authored_one(api):
    fake, c = api
    c.post("/v1/test-cases", json=BODY)
    second = c.post("/v1/test-cases", json={**BODY, "name": "Second"}).json()
    artifact = json.loads((fake.episode_dir / AUTHORED_CASE_FILE).read_text())
    assert artifact["test_case_id"] == second["id"] and artifact["cases_created"] == 2
    assert fake.summary()["cases_created"] == 2


@pytest.mark.parametrize("bad", [
    {k: v for k, v in BODY.items() if k != "name"},
    {**BODY, "steps": []},
    {**BODY, "steps": ["Open the app"]},                        # bare strings: not the API shape
    {**BODY, "steps": [{"description": ""}]},
    {**BODY, "steps": [{"description": "Open", "kind": "check"}]},
    {k: v for k, v in BODY.items() if k != "expected_result"},
])
def test_invalid_create_bodies_are_422_and_capture_nothing(api, bad):
    fake, c = api
    r = c.post("/v1/test-cases", json=bad)
    assert r.status_code == 422
    assert r.json()["detail"] and r.json()["detail"][0]["loc"][0] == "body"
    assert not (fake.episode_dir / AUTHORED_CASE_FILE).exists()
    assert _log(fake)[-1]["status"] == 422


def test_every_request_is_recorded_without_the_key(api):
    fake, c = api
    c.get("/v1/categories/list")
    c.post("/v1/test-cases", json=BODY)
    log = _log(fake)
    assert [(e["method"], e["path"], e["status"], e["route"]) for e in log] == [
        ("GET", "/v1/categories/list", 200, "list_categories"),
        ("POST", "/v1/test-cases", 201, "create_test_case"),
    ]
    assert all(e["has_api_key"] for e in log)
    assert log[1]["body"] == BODY
    assert "qg_secret_value" not in (fake.api_dir / "requests.jsonl").read_text()


def test_unknown_routes_are_404_and_logged_loudly(api, caplog):
    fake, c = api
    with caplog.at_level(logging.WARNING, logger="qualgentbench.create.fake_api"):
        r = c.post("/v1/test-cases/run", json={"jobs": []})       # run_tests
        c.post("/v1/apps/delete", json={"files": []})             # delete_apps
    assert r.status_code == 404 and "no route" in r.json()["detail"]
    assert sum("NO ROUTE" in rec.getMessage() for rec in caplog.records) == 2
    log = _log(fake)
    assert [e["unknown"] for e in log] == [True, True]
    summary = fake.stop()
    assert summary["unknown_routes"] == ["POST /v1/test-cases/run", "POST /v1/apps/delete"]
    assert json.loads((fake.api_dir / "summary.json").read_text()) == summary


def test_a_missing_case_is_404_not_unknown(api):
    fake, c = api
    assert c.get("/v1/test-cases/nope").status_code == 404
    assert _log(fake)[-1]["unknown"] is False
