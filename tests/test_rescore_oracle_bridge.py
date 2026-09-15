"""A rescore has no device: the db/content oracle outcome must come back from the
saved metrics, or every such episode silently rescoring to completion=None looks
like a scoring change when it is a missing key."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rescore_journey", ROOT / "scripts" / "rescore_journey.py")
rescore_journey = importlib.util.module_from_spec(_spec)
sys.modules["rescore_journey"] = rescore_journey
_spec.loader.exec_module(rescore_journey)

from qualgentbench import journey  # noqa: E402


def test_bridge_reads_persisted_result():
    spec = {"oracle": {"mode": "db"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": True, "result": "holds",
                                                      "detail": "db ... → '1'"}})
    assert spec["oracle_result"] == "holds" and spec["oracle_detail"].startswith("db")


def test_bridge_derives_from_ok_for_older_runs():
    spec = {"oracle": {"mode": "content"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "content", "ok": False}})
    assert spec["oracle_result"] == "violated"


def test_bridge_leaves_screen_oracles_and_unevaluated_alone():
    spec = {"oracle": {"mode": "present"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "present", "ok": None}})
    assert "oracle_result" not in spec
    spec = {"oracle": {"mode": "db"}}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": None, "result": None}})
    assert "oracle_result" not in spec


def test_bridge_never_overrides_a_flat_key():
    spec = {"oracle": {"mode": "db"}, "oracle_result": "inconclusive"}
    rescore_journey._restore_oracle(spec, {"oracle": {"mode": "db", "ok": True, "result": "holds"}})
    assert spec["oracle_result"] == "inconclusive"


def test_verdict_round_trips_through_saved_metrics():
    """What the scorer wrote under metrics['oracle'] must be enough to re-derive the same
    completion without a device."""
    spec = {"oracle": {"mode": "db"}, "oracle_result": "holds", "oracle_detail": "x"}
    ok, _ = journey._oracle_verdict(spec, [])
    assert ok is True
    saved = {"oracle": {"mode": "db", "ok": ok, "why": "", "detail": "x", "result": "holds"}}
    again = {"oracle": {"mode": "db"}}
    rescore_journey._restore_oracle(again, saved)
    assert journey._oracle_verdict(again, [])[0] is True
