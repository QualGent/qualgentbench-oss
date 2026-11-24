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
FIXTURE_SHA256 = "59322cb15466c48072c77815d9412793759c01a6dc0ebb5411249ddc3774ab16"

TEXT = FIXTURE.read_text(encoding="utf-8")
DOC = json.loads(TEXT)
BUG_FIELDS = ("summary", "steps", "expected", "actual", "failure_step")
#: Added in fixture version 2: the agent's step list as the product stores it on the bug.
REPRO_STEPS = "repro_steps"


def test_the_fixture_is_the_pinned_file():
    lines = TEXT.split("\n")
    rest = [line for line in lines if not line.strip().startswith('"sha256":')]
    assert len(rest) == len(lines) - 1, "exactly one sha256 line"
    digest = hashlib.sha256("\n".join(rest).encode("utf-8")).hexdigest()
    assert DOC["sha256"] == digest, "the hash line does not match the file's content"
    assert digest == FIXTURE_SHA256, "the fixture changed: update both repos and both pins"
    assert DOC["version"] == 2
    assert DOC["result_field_max"] == rq.RESULT_FIELD_MAX == 250
    assert DOC["repro_steps_max"] == rq.REPRO_STEPS_MAX == 50
    assert DOC["repro_step_max"] == rq.REPRO_STEP_MAX == 500
    ids = [c["id"] for c in DOC["cases"]]
    assert len(set(ids)) == len(ids) >= 12


@pytest.mark.parametrize("case", DOC["cases"], ids=lambda c: c["id"])
def test_customer_report_matches_the_stored_bug(case):
    report = rq.customer_report_from_tool(case["args"])
    if case["bug"] is None:
        assert report is None
        return
    assert set(case["bug"]) == {*BUG_FIELDS, REPRO_STEPS}
    assert {k: getattr(report, k) for k in BUG_FIELDS} == {k: case["bug"][k] for k in BUG_FIELDS}
    assert report.report_source == rq.SOURCE_REPORT_TOOL
    # The agent's step list is kept raw as `agent_steps` and, as the product stores it,
    # as `repro_steps`. It is never the bug's `steps` text.
    steps = case["args"].get("steps")
    assert list(report.agent_steps) == (steps if isinstance(steps, list) else [])
    assert list(report.repro_steps) == case["bug"][REPRO_STEPS]
    assert len(report.repro_steps) <= rq.REPRO_STEPS_MAX
    assert all(0 < len(step) <= rq.REPRO_STEP_MAX for step in report.repro_steps)
    # The stored steps are the entries G3's steps rule reads from the raw list, with the
    # agent's own marks still on them.
    kept = [e for e in rq.step_entries(list(report.agent_steps)) if e][:rq.REPRO_STEPS_MAX]
    assert len(kept) == len(report.repro_steps)
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
    # Reproduction steps: the agent's words and marks, a closing outcome sentence kept.
    as_strings = by_id["fail-steps-as-strings"]["bug"]["repro_steps"]
    assert as_strings[0].startswith("\u2713 ") and as_strings[3].startswith("\u2717 ")
    assert as_strings[-1].startswith("Could not read the events")
    assert all("[REDACTED]" in s for s in by_id["fail-steps-redacted"]["bug"]["repro_steps"][:2])
    assert len(by_id["fail-steps-too-many"]["bug"]["repro_steps"]) == 50
    assert [len(s) for s in by_id["fail-steps-long-entry"]["bug"]["repro_steps"]] == [
        12, 499, 500, 500]
    assert by_id["fail-steps-empty-list"]["bug"]["repro_steps"] == []
    assert by_id["fail-non-string-fields"]["bug"]["repro_steps"] == []
