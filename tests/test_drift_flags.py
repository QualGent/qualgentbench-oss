"""Drift flags (QUA-3490): benign UI changes behind the same QgbFlags shim as the
defects — same journey, new UI. They must switch on through the flags file, rewrite
the harness-only replay route, and never be scored as a defect."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
import yaml
from test_journey import _bug, _obs, _spec, _task, _transcript, _write

from qualgentbench import bugs, corpus, journey
from qualgentbench import episode_runner as er

_ROOT = Path(__file__).parents[1]
_LINT = importlib.util.spec_from_file_location("lint_journey_cases",
                                               _ROOT / "scripts" / "lint_journey_cases.py")
lint = importlib.util.module_from_spec(_LINT)
_LINT.loader.exec_module(lint)

DRIFT_APPS = ("markor", "uhabits", "aegis", "simpletimetracker")

_DOC = {
    "app": "app",
    "defects": [{"id": "bug-a", "kind": "functional", "class": "persistence", "tier": "L3",
                 "symptoms": ["not saved"]}],
    "drift": [
        {"id": "d-label", "kind": "relabel", "change": "Save reads Store.",
         "rewrite": [{"from": {"tap": "Save"}, "to": [{"tap": "Store"}]}]},
        {"id": "d-move", "kind": "move", "change": "Details moves into the menu.",
         "rewrite": [{"from": {"tap": "Details"}, "to": [{"tap": "More options"}, {"tap": "Details"}]}]},
        {"id": "d-order", "kind": "reorder", "change": "Two choices swap.", "affects": ["c1"]},
    ],
    "test_cases": [
        {"id": "c1", "name": "Save a note", "steps": ["Save it."], "expected_outcome": "Saved.",
         "check": {"steps": ["launch", {"tap": "Save"}, "wait", {"long_press": "Note"},
                             {"tap": "Details"}],
                   "expect": {"present": "12 Bytes"}},
         "evidence": ["12 Bytes"], "bugs": ["bug-a"]},
        {"id": "c2", "name": "Other", "steps": ["Open it."], "expected_outcome": "Open.",
         "check": {"steps": ["launch", {"tap": "Open"}], "expect": {"present": "Opened"}},
         "evidence": ["Opened"]},
    ],
}


# ── the route rewrite ─────────────────────────────────────────────────────────

def test_drift_route_relabels_and_moves_and_composes():
    defs = journey.load_drift(_DOC)
    route = _DOC["test_cases"][0]["check"]["steps"]
    assert journey.drift_route(route, [], defs) == route
    assert journey.drift_route(route, ["d-label"], defs) == [
        "launch", {"tap": "Store"}, "wait", {"long_press": "Note"}, {"tap": "Details"}]
    both = journey.drift_route(route, ["d-label", "d-move"], defs)
    assert both == ["launch", {"tap": "Store"}, "wait", {"long_press": "Note"},
                    {"tap": "More options"}, {"tap": "Details"}]
    # Independent flags: order does not matter.
    assert journey.drift_route(route, ["d-move", "d-label"], defs) == both


def test_drift_affects_and_resolve():
    defs = journey.load_drift(_DOC)
    c1, c2 = _DOC["test_cases"]
    assert journey.drift_affects(c1, "d-label", defs) and not journey.drift_affects(c2, "d-label", defs)
    assert not journey.drift_affects(c1, "d-order", defs)   # a reorder rewrites nothing
    assert defs["d-order"]["affects"] == ["c1"]
    assert journey.resolve_drift("all", defs) == ["d-label", "d-move", "d-order"]
    assert journey.resolve_drift(["d-move", "d-move"], defs) == ["d-move"]
    with pytest.raises(ValueError, match="unknown drift"):
        journey.resolve_drift(["d-typo"], defs)


def test_a_drift_id_in_bugs_is_refused_as_an_unknown_defect():
    case = dict(_DOC["test_cases"][0], bugs=["d-label"])
    with pytest.raises(ValueError, match="unknown defect"):
        journey.case_design(case, journey.load_defects(_DOC))


# ── the real corpus ───────────────────────────────────────────────────────────

def _suite(app_id):
    return next(s for s in bugs.load_apps() if s["app"]["id"] == app_id)


@pytest.mark.parametrize("app_id", DRIFT_APPS)
def test_each_drift_app_declares_three_to_five_flags_matching_its_spec(app_id):
    defs = journey.load_drift(journey.load_cases(app_id))
    assert 3 <= len(defs) <= 5
    spec = yaml.safe_load(corpus.spec_path(app_id).read_text())
    assert {str(d["id"]) for d in spec["drift"]} == set(defs)
    # Not defects, and every flag reaches at least one case.
    doc = journey.load_cases(app_id)
    assert not set(defs) & set(journey.load_defects(doc))
    assert not set(defs) & {str(b["id"]) for b in spec.get("bugs", [])}
    for did, d in defs.items():
        assert d["affects"] or any(journey.drift_affects(c, did, defs) for c in doc["test_cases"])


@pytest.mark.parametrize("app_id", DRIFT_APPS)
def test_drift_never_enters_active_bugs_or_the_scored_spec(app_id):
    plain = journey.journey_tasks(_suite(app_id))
    drifted = journey.journey_tasks(_suite(app_id), drift="all")
    ids = list(journey.load_drift(journey.load_cases(app_id)))
    assert [t.id for t in plain] == [t.id for t in drifted]
    for p, d in zip(plain, drifted):
        assert "drift" not in p.bug_spec                     # no drift: byte-identical spec
        assert d.bug_spec["drift"] == ids
        assert d.bug_spec["active_bugs"] == p.bug_spec["active_bugs"]
        assert not set(ids) & set(d.bug_spec["active_bugs"])
        assert {k: v for k, v in d.bug_spec.items() if k != "drift"} == p.bug_spec


def test_drift_ids_never_touch_the_brief():
    for app_id in DRIFT_APPS:
        plain = journey.journey_tasks(_suite(app_id))
        drifted = journey.journey_tasks(_suite(app_id), drift="all")
        for p, d in zip(plain, drifted):
            assert (journey.brief(p, "emulator-5554", "raw")
                    == journey.brief(d, "emulator-5554", "raw"))


# ── scoring: a drift-on episode scores exactly as the drift-off one ───────────

@pytest.mark.parametrize("version", ["clean", "seeded"])
def test_verdict_ignores_drift(version):
    oracle = {"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []}
    base = _spec(version, bugs=["delete-bug"] if version == "seeded" else [],
                 oracle=oracle, oracle_result="holds")
    drifted = dict(base, drift=["d-label", "d-move"])
    tr = _transcript(_obs("Total: 4 items"), _write("pass"))
    a = journey.journey_verdict(tr, "m", _task(base))
    b = journey.journey_verdict(tr, "m", _task(drifted))
    for key in ("completed", "bugs_present", "bugs_found", "bugs_missed", "false_reports",
                "recall", "precision", "defects"):
        assert a.metrics[key] == b.metrics[key], key
    assert a.passed == b.passed


def test_reporting_a_drift_change_on_a_clean_build_is_a_false_report():
    """The memory-eval contract: drift is benign, so naming it as a bug costs exactly
    what any clean-arm report costs."""
    oracle = {"mode": "db", "expect": {"db": "x", "query": "q", "equals": "1"}, "evidence": []}
    spec = dict(_spec("clean", oracle=oracle, oracle_result="holds"), drift=["d-label"])
    v = journey.journey_verdict(_transcript(
        _obs("Store"), _write("pass", _bug(2, "Store", "the Save button was renamed"))), "m", _task(spec))
    assert v.metrics["false_reports"] == 1 and v.metrics["bugs_present"] == []


# ── staging: the flags file and the precondition ──────────────────────────────

def test_write_bug_flags_writes_drift_beside_the_bugs(monkeypatch):
    sent: list[str] = []

    async def _adb(*args, **kw):
        sent.append(" ".join(args))
        return 0, b""
    monkeypatch.setattr(er, "_adb", _adb)
    spec = {"active_bugs": ["bug-a"], "drift": ["d-label"]}
    asyncio.run(er.write_bug_flags("serial", "com.x", spec))
    assert "bug-a" in sent[0] and "d-label" in sent[0]
    assert spec["active_bugs_written"] == ["bug-a"] and spec["drift_written"] == ["d-label"]
    clean = {"active_bugs": [], "version": "clean", "drift": ["d-label"]}
    asyncio.run(er.write_bug_flags("serial", "com.x", clean))
    assert "d-label" in sent[1] and clean["active_bugs_written"] == []


def test_flag_file_shape_is_unchanged_by_drift():
    assert len(er.flag_file_lines("n", ["a", "d1", "d2"])) == er.FLAG_FILE_LINES


def test_precondition_anchor_follows_the_drifted_route():
    assert er.precondition_anchor("aegis", "aegis-rename-entry")[0] == "Add new entry"
    assert er.precondition_anchor("aegis", "aegis-rename-entry",
                                  drift=["aegis-drift-fab-label"])[0] == "Add account"
    assert er.precondition_anchor("uhabits", "uhabits-change-color",
                                  drift=["uhabits-drift-add-label"])[0] == "New habit"


# ── the lint ──────────────────────────────────────────────────────────────────

def test_lint_drift_accepts_the_example_and_the_corpus():
    assert lint.rule_drift(_DOC, {"d-label", "d-move", "d-order"}) == []
    for app_id in DRIFT_APPS:
        assert [f for f in lint.lint_corpus([app_id])[app_id] if f.rule == "drift"] == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["drift"].append({"id": "bug-a", "kind": "relabel", "change": "x",
                                  "affects": ["c1"]}), "also a defect id"),
    (lambda d: d["test_cases"][0]["bugs"].append("d-label"), "drift is not a defect"),
    (lambda d: d["drift"][0].update(kind="restyle"), "kind"),
    (lambda d: d["drift"][0].update(change=""), "change"),
    (lambda d: d["drift"][0].update(rewrite=[{"from": {"tap": "Nowhere"}, "to": [{"tap": "X"}]}]),
     "matches any case"),
    (lambda d: d["drift"][0].update(rewrite=[{"from": {"tap": "Save"}, "to": [{"bogus": "X"}]}]),
     "rewrite 1"),
    (lambda d: d["drift"][2].update(affects=["nope"]), "unknown case"),
    (lambda d: d["drift"][2].pop("affects"), "neither a rewrite"),
])
def test_lint_drift_errors(mutate, needle):
    import copy
    doc = copy.deepcopy(_DOC)
    mutate(doc)
    found = lint.rule_drift(doc)
    assert any(needle in f.detail and f.level == "error" for f in found), found


def test_lint_drift_compares_against_the_spec():
    found = lint.rule_drift(_DOC, {"d-label", "d-move", "d-order", "d-extra"})
    assert any("not declared in the case file" in f.detail for f in found)
    found = lint.rule_drift(_DOC, {"d-label"})
    assert any("not patched in the benchmark spec" in f.detail for f in found)


# ── the build side: drift patches in the benchmark spec ───────────────────────

_BUILD = importlib.util.spec_from_file_location("build_app_for_drift",
                                                _ROOT / "scripts" / "build_app.py")
build_app = importlib.util.module_from_spec(_BUILD)
_BUILD.loader.exec_module(build_app)


@pytest.mark.parametrize("app_id", DRIFT_APPS)
def test_drift_patches_gate_on_their_own_id_and_never_fire(app_id):
    """Each drift patch reads ITS OWN flag and calls no `fired()` canary: a fired
    marker attributes a FAIL to a defect, and drift is not one."""
    spec = yaml.safe_load(corpus.spec_path(app_id).read_text())
    build_app._check_drift_ids(spec)          # exits on a collision or a patchless entry
    for d in spec["drift"]:
        sites = d.get("patches") or [d["patch"]]
        text = "\n".join(s["replace"] for s in sites)
        assert f'QgbFlags.on("{d["id"]}")' in text
        assert "QgbFlags.fired(" not in text
        assert "// BUG(" not in text
    assert len(build_app._drift_patches(spec)) == sum(
        len(d.get("patches") or [d["patch"]]) for d in spec["drift"])


def test_check_drift_ids_refuses_a_collision():
    with pytest.raises(SystemExit):
        build_app._check_drift_ids({"bugs": [{"id": "x"}],
                                    "drift": [{"id": "x", "patch": {"file": "f", "find": "a", "replace": "b"}}]})
    with pytest.raises(SystemExit):
        build_app._check_drift_ids({"drift": [{"id": "y"}]})


# ── the deriver's drift helpers ───────────────────────────────────────────────

_DERIVE = importlib.util.spec_from_file_location("derive_journey_for_drift",
                                                 _ROOT / "scripts" / "derive_journey.py")
derive = importlib.util.module_from_spec(_DERIVE)
_DERIVE.loader.exec_module(derive)


def test_drift_cases_covers_rewritten_and_named_cases():
    defs = journey.load_drift(_DOC)
    assert [c["id"] for c in derive.drift_cases(_DOC, ["d-label"], defs)] == ["c1"]
    assert [c["id"] for c in derive.drift_cases(_DOC, ["d-order"], defs)] == ["c1"]


def _row(clean="holds", measured="PASS", screens=(("Save",),)):
    return {"expected": "PASS", "measured": measured,
            "passes": {"clean": {"outcome": clean}},
            "screens": {"clean": [list(s) for s in screens]}}


def test_compare_to_base_same_verdict_and_visible():
    got = derive.compare_to_base(_row(screens=(("Store",),)), _row())
    assert got["same_verdict"] and got["visible"] and not got["problems"]
    assert got["texts_added"] == ["Store"] and got["texts_removed"] == ["Save"]


def test_compare_to_base_flags_a_moved_verdict():
    got = derive.compare_to_base(_row(measured="FAIL"), _row())
    assert not got["same_verdict"] and "drift moved the verdict" in got["problems"][0]
    assert derive.compare_to_base(_row(), None)["problems"]
