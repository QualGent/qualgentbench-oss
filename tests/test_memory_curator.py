"""QUA-3486: the post-stage curator — gate, dedup, model decisions applied in code, lint
on every write, carry-forward and reproducible snapshots. No test reaches a live model."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner

from qualgentbench.memory import curator as cur
from qualgentbench.memory import format as fmt
from qualgentbench.memory import lint
from qualgentbench.memory import proposals as props
from qualgentbench.memory.cli import memory as memory_cli
from qualgentbench.memory.snapshot import snapshot_hash, unit_files

FIX = Path(__file__).parent / "fixtures" / "memory"
ORG, APP = "samplecorp", "sampleapp"
DATE = "2026-11-20"


def _terms() -> lint.CorpusTerms:
    t = lint.CorpusTerms()
    t.add_spec(APP, {"bugs": [{"id": "sample-pin-lost",
                               "title": "Pinning a note does not stick"}]})
    return t


@pytest.fixture
def mk(tmp_path: Path):
    src = tmp_path / "mk"
    shutil.copytree(FIX / "good", src)
    return src


def _ep(eid: str, items: list[dict], *, verified: bool = True, case: str | None = None
        ) -> cur.Episode:
    return cur.Episode(id=eid, verified=verified, case=case,
                       verified_reason="test", proposals=props.loads(yaml.safe_dump(items)))


def _p(ptype: str, text: str, **kw) -> dict:
    return {"type": ptype, "text": text, "evidence": {"step": 3, "screen": "Note list"}, **kw}


def _model(decisions: dict[str, dict]) -> cur.FunctionModel:
    def fn(request: dict) -> dict:
        return {"decisions": [{"proposal": p["key"], **decisions[p["key"]]}
                              for p in request["proposals"] if p["key"] in decisions]}
    return cur.FunctionModel(fn, id="scripted")


def _run(mk: Path, tmp_path: Path, episodes, model=None, name="out", **kw):
    return cur.curate(mk, tmp_path / name, org_key=ORG, app_key=APP, episodes=episodes,
                      model=model, stage="run-1", date=DATE, terms=_terms(),
                      log_path=tmp_path / f"{name}.json", **kw)


def _app(r: cur.CurateResult) -> fmt.MemoryFile:
    return fmt.load(fmt.app_path(r.out_root, ORG, APP))


def _ops(r: cur.CurateResult) -> dict[str, dict]:
    return {o["proposal"]: o for o in r.log["operations"]}


def _assert_clean(r: cur.CurateResult) -> None:
    assert lint.lint_tree(r.out_root, terms=_terms()) == []


# ── each operation ────────────────────────────────────────────────────────────

def test_add_from_a_verified_episode(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "ADD", "reason": "durable route",
                         "text": "The archive is in the note list's overflow menu."}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("nav", "Archive is under the overflow menu.")])], m)
    e = _app(r).get("nav-004")
    assert e.text == "The archive is in the note list's overflow menu."
    assert e.meta == {"src": "agent", "confirmed": DATE, "seen": "1", "contra": "0",
                      "stage": "run-1"}
    op = _ops(r)["e1#0"]
    assert op["op"] == "ADD" and op["by"] == "model" and op["entry"] == "nav-004"
    assert m.calls == 1 and r.log["model_called"] and not r.carried_forward
    assert r.snapshot != r.parent
    _assert_clean(r)


def test_update_text_of_a_candidate_entry(mk: Path, tmp_path: Path) -> None:
    text = "To open a note's settings, long-press the note, then tap Properties."
    m = _model({"e1#0": {"op": "UPDATE", "target": "nav-003", "text": text,
                         "reason": "refines"}})
    seen: list[dict] = []
    inner = m.fn

    def spy(req: dict) -> dict:
        seen.append(req)
        return inner(req)

    m.fn = spy
    r = _run(mk, tmp_path, [_ep("e1", [_p("nav", "Long-press a note, then tap Properties "
                                                 "to open its settings.")])], m)
    cand = seen[0]["proposals"][0]["candidates"]
    assert cand[0]["id"] == "nav-003"
    assert all(cur.SIM_LOW <= c["similarity"] < cur.SIM_HIGH for c in cand)
    e = _app(r).get("nav-003")
    assert e.text == text and e.seen == 5 and e.confirmed == DATE and e.src == "human"
    assert _ops(r)["e1#0"]["old_text"].startswith("To open a note's settings")
    _assert_clean(r)


def test_invalidate_from_a_verified_contradiction(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "INVALIDATE", "target": "nav-001",
                         "reason": "the start screen is now the notebook list; seen twice"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("nav", "The start screen is the notebook list.",
                                          contradicts="nav-001")])], m)
    doc = _app(r)
    assert doc.is_invalidated("nav-001")
    e = doc.get("nav-001")
    assert e.meta["invalidated"] == DATE
    assert ";" not in e.meta["reason"] and "notebook list" in e.meta["reason"]
    _assert_clean(r)


def test_noop_on_a_verified_contradiction_still_raises_contra(mk: Path,
                                                              tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "NOOP", "reason": "one run is not enough"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("flaky", "Search answered at once.",
                                          contradicts="flaky-001")])], m)
    assert _app(r).get("flaky-001").contra == 2
    op = _ops(r)["e1#0"]
    assert op["op"] == "NOOP" and op["contra_raised"] == "flaky-001"


def test_model_noop_adds_nothing(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "NOOP", "reason": "one-off"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", "I tapped the plus button.")])], m)
    assert r.snapshot == r.parent and not r.carried_forward
    assert r.counts["NOOP"] == 1


# ── the verified-outcome gate ─────────────────────────────────────────────────

def test_unverified_episode_can_only_raise_contra(mk: Path, tmp_path: Path) -> None:
    m = cur.FunctionModel(lambda req: pytest.fail("model must not be called"))
    ep = _ep("e1", [_p("nav", "A brand new route."),
                    _p("note", "Refined.", updates="note-001"),
                    _p("flaky", "Search is instant.", contradicts="flaky-001")],
             verified=False)
    r = _run(mk, tmp_path, [ep], m)
    ops = _ops(r)
    assert ops["e1#0"]["op"] == "NOOP" and ops["e1#0"]["by"] == "gate"
    assert ops["e1#1"]["op"] == "NOOP"
    assert ops["e1#2"] == {**ops["e1#2"], "op": "UPDATE", "by": "gate",
                           "target": "flaky-001", "contra": 1}
    doc = _app(r)
    assert doc.get("flaky-001").contra == 2 and doc.get("note-001").seen == 0
    assert len(doc.entries()) == 6 and m.calls == 0 and not r.log["model_called"]
    _assert_clean(r)


def test_accepted_proposals_are_rejected(mk: Path, tmp_path: Path) -> None:
    r = _run(mk, tmp_path, [_ep("e1", [_p("accepted", "Long titles are cut on purpose.")])])
    op = _ops(r)["e1#0"]
    assert op["op"] == "NOOP" and op["by"] == "gate" and "D6" in op["reason"]
    assert r.snapshot == r.parent


def test_accepted_entry_text_is_never_changed_by_the_agent(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "UPDATE", "target": "accepted-001", "text": "Rewritten."}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", "Empty notes are dropped on back.",
                                          updates="accepted-001")])], m)
    e = _app(r).get("accepted-001")
    assert e.text.startswith("An empty note is discarded") and e.seen == 1
    assert "D6" in _ops(r)["e1#0"]["note"]
    _assert_clean(r)


def test_accepted_entry_cannot_be_invalidated(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "INVALIDATE", "target": "accepted-001", "reason": "x"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", "Empty notes are kept.",
                                          contradicts="accepted-001")])], m)
    doc = _app(r)
    assert not doc.is_invalidated("accepted-001") and doc.get("accepted-001").contra == 1
    assert "not allowed" in _ops(r)["e1#0"]["reason"]


def test_refs_to_dead_ids(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#1": {"op": "ADD"}})
    r = _run(mk, tmp_path, [_ep("e1", [
        _p("nav", "Settings moved again.", contradicts="nav-002"),   # invalidated
        _p("note", "Drafts survive rotation.", updates="note-099"),   # unknown
    ])], m)
    ops = _ops(r)
    assert ops["e1#0"]["op"] == "NOOP" and "not a live entry" in ops["e1#0"]["reason"]
    assert ops["e1#1"]["op"] == "ADD" and "ignored" in ops["e1#1"]["note"]


def test_model_cannot_step_outside_the_allowed_operations(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "INVALIDATE", "target": "nav-001", "reason": "x"},
                "e1#1": {"op": "UPDATE", "target": "setup-001"},
                "e1#2": {"op": "DELETE"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("nav", "A new route one."),
                                       _p("nav", "Another new route two."),
                                       _p("note", "Third thing entirely.")]),
                            ], m)
    assert [o["op"] for o in r.log["operations"]] == ["NOOP"] * 3
    assert r.snapshot == r.parent


def test_missing_decision_is_a_noop(mk: Path, tmp_path: Path) -> None:
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", "Something.")])], _model({}))
    assert _ops(r)["e1#0"]["reason"] == "no decision from the model"


def test_proposals_without_a_model_raise(mk: Path, tmp_path: Path) -> None:
    with pytest.raises(cur.CuratorError, match="need a curator model"):
        _run(mk, tmp_path, [_ep("e1", [_p("note", "Something.")])], None)


# ── dedup ─────────────────────────────────────────────────────────────────────

def test_near_duplicate_of_an_entry_is_confirmed_in_code(mk: Path, tmp_path: Path) -> None:
    m = cur.FunctionModel(lambda req: pytest.fail("no model for a sure duplicate"))
    text = "The note list is the start screen; the + button at the bottom right opens a note."
    r = _run(mk, tmp_path, [_ep("e1", [_p("nav", text)]), _ep("e2", [_p("nav", text)])], m)
    e = _app(r).get("nav-001")
    assert e.seen == 6 and e.confirmed == DATE
    assert {o["by"] for o in r.log["operations"]} == {"dedup"}


def test_duplicates_within_the_batch_become_one_entry(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "ADD"}})
    r = _run(mk, tmp_path, [
        _ep("e1", [_p("flaky", "Opening the calendar takes three seconds.")]),
        _ep("e2", [_p("flaky", "Opening the calendar takes three seconds!")]),
        _ep("e3", [_p("flaky", "opening the calendar takes three seconds")]),
    ], m)
    doc = _app(r)
    added = [e for e in doc.entries() if e.src == "agent" and e.type == "flaky"
             and e.id != "flaky-001"]
    assert [e.id for e in added] == ["flaky-002"] and added[0].seen == 3
    ops = _ops(r)
    assert ops["e2#0"]["by"] == ops["e3#0"]["by"] == "dedup"
    assert ops["e2#0"]["target"] == "flaky-002"


def test_duplicate_of_a_rejected_proposal_is_a_noop(mk: Path, tmp_path: Path) -> None:
    m = _model({"e1#0": {"op": "NOOP"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", "The app is blue.")]),
                            _ep("e2", [_p("note", "The app is blue.")])], m)
    assert _ops(r)["e2#0"]["op"] == "NOOP" and r.snapshot == r.parent


def test_same_episode_confirms_an_entry_once(mk: Path, tmp_path: Path) -> None:
    text = "The editor saves when it is left, not on every keystroke."
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", text), _p("note", text)])])
    assert _app(r).get("note-001").seen == 1


def test_similarity() -> None:
    assert cur.similarity("Tap  Save.", "tap save") == 1.0
    assert cur.similarity("Search is slow", "The archive holds old notes") < cur.SIM_LOW


# ── lint on every write ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,rule", [
    ("You must open the drawer first.", "instruction"),
    ("Pinning a note does not stick after restart.", "symptom"),
    ("The sample-pin-lost flag is on here.", "defect-id"),
])
def test_a_write_that_fails_lint_is_rolled_back(mk: Path, tmp_path: Path, text: str,
                                               rule: str) -> None:
    m = _model({"e1#0": {"op": "ADD", "text": text}, "e1#1": {"op": "ADD"}})
    r = _run(mk, tmp_path, [_ep("e1", [_p("note", "A harmless note text."),
                                       _p("nav", "Notebooks open from the drawer.")])], m)
    ops = _ops(r)
    assert ops["e1#0"]["op"] == "NOOP" and ops["e1#0"]["by"] == "lint"
    assert rule in ops["e1#0"]["reason"]
    assert ops["e1#1"]["op"] == "ADD"     # the next write still lands
    _assert_clean(r)


def test_input_that_fails_lint_is_refused(mk: Path, tmp_path: Path) -> None:
    p = fmt.app_path(mk, ORG, APP)
    p.write_text(p.read_text().replace("not on every keystroke.", "you must wait."))
    with pytest.raises(cur.CuratorError, match="input memory fails lint"):
        _run(mk, tmp_path, [])


# ── carry-forward, exclusion, determinism ─────────────────────────────────────

def test_no_episodes_carries_forward(mk: Path, tmp_path: Path) -> None:
    r = _run(mk, tmp_path, [])
    assert r.carried_forward and r.snapshot == r.parent
    assert r.log["carry_reason"] == "no episodes"
    log = json.loads((tmp_path / "out.json").read_text())
    assert log["carried_forward"] and log["snapshot"] == r.snapshot
    # every memory file came along, the changes file included
    assert (r.out_root / "app" / ORG / f"{APP}.changes.md").is_file()


def test_missing_proposal_files_carry_forward_and_are_logged(mk: Path,
                                                             tmp_path: Path) -> None:
    eps = [cur.Episode.from_files("e1", tmp_path / "nope.yaml", {"completed": True}),
           cur.Episode.from_files("e2", tmp_path / "nope2.yaml", None)]
    r = _run(mk, tmp_path, eps)
    assert r.carried_forward and r.snapshot == r.parent
    assert r.counts["episodes_missing_proposals"] == 2
    assert [e["verified"] for e in r.log["episodes"]] == [True, False]


def test_excluded_case_contributes_nothing(mk: Path, tmp_path: Path) -> None:
    r = _run(mk, tmp_path, [_ep("e1", [_p("nav", "x route")], case="case-a")],
             exclude_cases=["case-a"])
    assert r.carried_forward and r.log["episodes"][0]["excluded"]


def test_out_root_must_be_empty(mk: Path, tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x")
    with pytest.raises(cur.CuratorError, match="not empty"):
        _run(mk, tmp_path, [])


def test_same_inputs_twice_give_the_same_snapshot(mk: Path, tmp_path: Path) -> None:
    calls = {"n": 0}

    def fn(req: dict) -> dict:
        calls["n"] += 1
        return {"decisions": [{"proposal": p["key"], "op": "ADD", "reason": "new"}
                              for p in req["proposals"]]}

    cache = tmp_path / "cache"
    eps = lambda: [_ep("e2", [_p("setup", "Sign-in is not needed.")]),
                   _ep("e1", [_p("nav", "The archive is under the overflow menu."),
                              _p("flaky", "Search lags.", contradicts="flaky-001")])]
    m1 = cur.CachedModel(cur.FunctionModel(fn, id="scripted"), cache)
    r1 = _run(mk, tmp_path, eps(), m1, name="a")
    m2 = cur.CachedModel(cur.FunctionModel(fn, id="scripted"), cache)
    r2 = _run(mk, tmp_path, eps(), m2, name="b")
    assert r1.snapshot == r2.snapshot != r1.parent
    assert calls["n"] == 1 and m2.hits == 1 and m2.misses == 0
    assert (tmp_path / "a.json").read_text() == (tmp_path / "b.json").read_text().replace(
        str(tmp_path / "b"), str(tmp_path / "a"))
    assert snapshot_hash(r1.out_root, unit_files(ORG, APP)) == r1.snapshot
    # episodes are processed in id order, whatever order they arrive in
    assert [o["episode"] for o in r1.log["operations"]] == ["e1", "e1", "e2"]


def test_cache_key_covers_model_and_request(tmp_path: Path) -> None:
    a = cur.CachedModel(cur.FunctionModel(lambda r: {}, id="m1"), tmp_path)
    b = cur.CachedModel(cur.FunctionModel(lambda r: {}, id="m2"), tmp_path)
    assert a.key({"x": 1}) != b.key({"x": 1}) and a.key({"x": 1}) != a.key({"x": 2})


# ── verified outcome from the scorer ──────────────────────────────────────────

@pytest.mark.parametrize("metrics,ok", [
    (None, False),
    ({"completed": True, "false_reports": 0}, True),
    ({"completed": False, "bugs_found": ["b"], "false_reports": 0}, True),
    ({"completed": True, "false_reports": 1}, False),
    ({"completed": None}, False),
    ({"completed": False, "bugs_found": []}, False),
    ({"completed": True, "env_failure": True}, False),
    ({"completed": True, "contaminated": True}, False),
])
def test_verified_outcome(metrics, ok: bool) -> None:
    assert cur.verified_outcome(metrics)[0] is ok


# ── the live-model client, against a mock transport ───────────────────────────

def test_anthropic_model_request_and_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        text = '```json\n{"decisions": [{"proposal": "e1#0", "op": "NOOP"}]}\n```'
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    monkeypatch.setenv(cur.API_KEY_ENV, "test-key")
    m = cur.AnthropicModel(model="curator-test", transport=httpx.MockTransport(handler))
    out = m.decide({"proposals": []})
    assert out["decisions"][0]["op"] == "NOOP"
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["body"]["temperature"] == 0 and seen["body"]["model"] == "curator-test"
    assert seen["body"]["system"] == cur.prompt_text()


def test_anthropic_model_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cur.API_KEY_ENV, raising=False)
    with pytest.raises(cur.CuratorError, match="not set"):
        cur.AnthropicModel(model="m").decide({})
    monkeypatch.setenv(cur.API_KEY_ENV, "k")
    bad = httpx.MockTransport(lambda r: httpx.Response(529, json={}))
    with pytest.raises(cur.CuratorError, match="HTTP 529"):
        cur.AnthropicModel(model="m", transport=bad).decide({})
    with pytest.raises(cur.CuratorError, match="not JSON"):
        cur.parse_answer("sure, here you go")


def test_model_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(cur.MODEL_ENV, raising=False)
    with pytest.raises(cur.CuratorError, match=cur.MODEL_ENV):
        cur.model_from_env()
    monkeypatch.setenv(cur.MODEL_ENV, "configured")
    m = cur.model_from_env(cache_dir=tmp_path)
    assert isinstance(m, cur.CachedModel) and m.id == "configured"


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_curate_and_proposals(mk: Path, tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cur.MODEL_ENV, raising=False)
    ep = tmp_path / "ep"
    ep.mkdir()
    (ep / "p.yaml").write_text(yaml.safe_dump([
        _p("flaky", "Search lags.", contradicts="flaky-001")]))
    (ep / "result.json").write_text(json.dumps({"metrics": {"completed": False}}))
    (tmp_path / "episodes.json").write_text(json.dumps([
        {"id": "e1", "proposals": "ep/p.yaml", "metrics": "ep/result.json"}]))
    runner = CliRunner()
    res = runner.invoke(memory_cli, ["curate", str(mk), "--out", str(tmp_path / "next"),
                                     "--app", APP, "--stage", "run-1", "--date", DATE,
                                     "--episodes", str(tmp_path / "episodes.json")])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["counts"]["UPDATE"] == 1 and not out["carried_forward"]
    assert (tmp_path / "next.curator.json").is_file()

    res = runner.invoke(memory_cli, ["proposals", str(FIX / "proposals" / "mixed.yaml")])
    assert res.exit_code == 1 and "4 error(s)" in res.output
    res = runner.invoke(memory_cli, ["proposals", str(FIX / "proposals" / "valid.yaml")])
    assert res.exit_code == 0


def test_real_corpus_terms_and_packaged_prompt(mk: Path, tmp_path: Path) -> None:
    """With no `terms`, the curator lints against the real corpus; the prompt ships in
    the package."""
    m = _model({"e1#0": {"op": "ADD"}})
    r = cur.curate(mk, tmp_path / "o", org_key=ORG, app_key=APP, model=m, stage="run-2",
                   date=DATE, episodes=[_ep("e1", [_p("nav", "Notebooks open from the "
                                                         "left drawer.")])])
    assert _ops(r)["e1#0"]["op"] == "ADD" and lint.lint_tree(r.out_root) == []
    assert cur.PROMPT_PATH.is_file() and len(r.log["prompt_sha256"]) == 64
