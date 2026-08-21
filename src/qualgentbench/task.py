"""The in-memory task object every episode runs. ``BenchmarkTask`` is the unit of
work: one app, one instruction, one oracle; ``bugs.py`` builds them from
``data/benchmarks/*.yaml``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BenchmarkTask:
    """In-memory benchmark task: one app, one instruction, one oracle."""

    id: str                 # test_case id
    name: str               # test_case name → task title
    instruction: str        # composed agent instruction (steps + expected_result)
    app_file_id: str        # /v1/apps/{id} → signed URL at download time
    app_name: str
    platform: str           # "android" | "ios"
    bundle_id: str          # routine lookup key (app_bundle_id); detected at install
    expected_result: str = ""
    # Verdict a correct agent SHOULD report; the regression scorer rewards
    # reported == expected, not pass-ness.
    expected_verdict: str = "PASS"
    credential_id: str = ""   # test_credentials.id for mobile_insert_credential
    credential_name: str = ""
    # Seeded-bug ground truth consumed by bugs.bug_verdict; None otherwise.
    bug_spec: dict | None = None

    @property
    def os(self) -> str:
        return "ios" if self.platform == "ios" else "android"


# ── test_case → BenchmarkTask mapping ────────────────────────────────────────
