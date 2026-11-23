"""The shared mapping fixture of the customer-shaped report (QUA-3585).

`tests/fixtures/report-mapping-fixture.json` holds `mobile_report_result` payloads and the
bug text the product stores for each. The same bytes are committed in the product's
repository and tested there against the product's own mapping; here they are tested
against `report_quality.customer_report_from_tool`. A hash line in the file and a pin in
each test keep the two copies the same file.

To change the mapping or a case: edit BOTH copies, recompute the hash line (SHA-256 of
the file's text with the `"sha256"` line removed) and update the pin in both tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from qualgentbench import report_quality as rq

FIXTURE = Path(__file__).parent / "fixtures" / "report-mapping-fixture.json"
#: Must equal the pin in the product repository's test of the same file.
FIXTURE_SHA256 = "f6c7f32ab4358c1c5560a83ccf240d12bacd3766050a1cbc19fff94526ff7e9d"

TEXT = FIXTURE.read_text(encoding="utf-8")
DOC = json.loads(TEXT)
BUG_FIELDS = ("summary", "steps", "expected", "actual", "failure_step")


def test_the_fixture_is_the_pinned_file():
    lines = TEXT.split("\n")
    rest = [line for line in lines if not line.strip().startswith('"sha256":')]
    assert len(rest) == len(lines) - 1, "exactly one sha256 line"
    digest = hashlib.sha256("\n".join(rest).encode("utf-8")).hexdigest()
    assert DOC["sha256"] == digest, "the hash line does not match the file's content"
    assert digest == FIXTURE_SHA256, "the fixture changed: update both repos and both pins"
    assert DOC["version"] == 1
    assert DOC["result_field_max"] == rq.RESULT_FIELD_MAX == 250
    ids = [c["id"] for c in DOC["cases"]]
    assert len(set(ids)) == len(ids) >= 12


@pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["id"])
def test_customer_report_matches_the_stored_bug(case):
    report = rq.customer_report_from_tool(case["args"])
    if case["bug"] is None:
        assert report is None
        return
    assert {k: getattr(report, k) for k in BUG_FIELDS} == case["bug"]
    assert report.report_source == rq.SOURCE_REPORT_TOOL
    # The agent's step list is kept beside the bug, unchanged, and is never the bug's steps.
    steps = case["args"].get("steps")
    assert list(report.agent_steps) == (steps if isinstance(steps, list) else [])
    for name in ("expected", "actual"):
        value = getattr(report, name)
        assert value is None or 0 < len(value) <= rq.RESULT_FIELD_MAX
    assert (report.expected is None) == (report.actual is None)


def test_the_fixture_covers_the_shapes_the_ticket_names():
    by_id = {c["id"]: c for c in DOC["cases"]}
    assert by_id["fail-with-investigation"]["bug"]["steps"] != \
        by_id["fail-with-investigation"]["bug"]["summary"]
    without = by_id["fail-without-investigation"]["bug"]
    assert without["steps"] == without["summary"]
    long = by_id["fail-long-expected-actual"]
    assert len(long["args"]["expected"]) > 250 and len(long["bug"]["expected"]) == 250
    assert len(long["bug"]["actual"]) < 250          # cut at 250, then the space removed
    assert by_id["pass"]["bug"] is None and by_id["blocked"]["bug"] is None
    assert by_id["fail-crash-claimed"]["product_only"]["crash_signatures"]
    assert by_id["fail-crash-unclaimed"]["product_only"]["extra_crash_bugs"] == 1
    assert "[REDACTED]" in by_id["fail-redacts-credentials"]["bug"]["summary"]
