"""A journey re-derive keeps QUA-2854's control keys when they still describe the case.

`derive_journey.py` writes every row from scratch. Before QUA-2860 that silently wiped
`create_controls` / `create_control_derivation` (written by `derive_create_controls.py`)
from any re-derived row — e.g. a rebuild that only adds attribution canaries. The keys
are carried over when the case's `journey.controls_fingerprint` (route, oracle, `bugs:`,
the app's defect set) is unchanged, and dropped when it moved, since the eligibility was
measured against another route. No device: `derive_app` is replaced by a fake.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_journey.py"


def _load():
    spec = importlib.util.spec_from_file_location("derive_journey_carry", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dj = _load()
journey = dj.journey

CASE = {"id": "case-a", "check": {"steps": ["launch", {"tap": "Go"}], "expect": {"present": "done"}},
        "bugs": ["a-bug"]}
DOC = {"defects": [{"id": "a-bug", "kind": "functional"}, {"id": "b-bug", "kind": "display"}],
       "test_cases": [CASE]}


def _fp(case=CASE, doc=DOC) -> str:
    return journey.controls_fingerprint(case, journey.load_defects(doc))


def _prior(fp: str) -> dict:
    return {"case-a": {"bugs": ["a-bug"], "agrees": True,
                       journey.CONTROLS_KEY: ["b-bug"],
                       journey.CONTROL_DERIVATION_KEY: {"fingerprint": fp, "repeat": 3}},
            "case-b": {"bugs": [], "agrees": True}}


def _fresh() -> dict:
    return {"case-a": {"bugs": ["a-bug"], "agrees": True, "passes": {}}}


def test_unchanged_case_keeps_both_keys():
    result = _fresh()
    notes = dj.carry_create_controls(_prior(_fp()), result, DOC)
    assert notes == []
    assert result["case-a"][journey.CONTROLS_KEY] == ["b-bug"]
    assert result["case-a"][journey.CONTROL_DERIVATION_KEY]["fingerprint"] == _fp()
    assert result["case-a"]["passes"] == {}          # the fresh row is otherwise untouched


def test_moved_route_drops_the_keys_and_says_so():
    result = _fresh()
    notes = dj.carry_create_controls(_prior("stale000000"), result, DOC)
    assert journey.CONTROLS_KEY not in result["case-a"]
    assert journey.CONTROL_DERIVATION_KEY not in result["case-a"]
    assert len(notes) == 1 and "case-a" in notes[0]


def test_rows_without_controls_are_left_alone():
    result = _fresh()
    assert dj.carry_create_controls({"case-a": {"bugs": ["a-bug"]}}, result, DOC) == []
    assert result == _fresh()


def test_main_writes_the_carried_keys(monkeypatch, tmp_path):
    """End to end through `main`: a whole-app re-derive and a `--case` re-derive both
    keep the keys on the file they rewrite."""
    async def fake_derive_app(app_id, serial, only, tmp, repeat=1):
        return _fresh()

    monkeypatch.setattr(dj, "derive_app", fake_derive_app)
    monkeypatch.setattr(dj.journey, "load_cases", lambda app_id: DOC)
    monkeypatch.setattr(dj, "ROOT", tmp_path)
    dest = tmp_path / "journey-demo.json"
    for extra in ([], ["--case", "case-a"]):
        dest.write_text(json.dumps(_prior(_fp())))
        monkeypatch.setattr(sys, "argv", ["derive_journey.py", "demo", "--json", str(dest), *extra])
        asyncio.run(dj.main())
        row = json.loads(dest.read_text())["case-a"]
        assert row[journey.CONTROLS_KEY] == ["b-bug"], extra
        assert row[journey.CONTROL_DERIVATION_KEY]["fingerprint"] == _fp(), extra
