"""QUA-2788: journey text matching folds whitespace on BOTH sides.

The needle (`journey._norm` / `_evidence`) always collapsed whitespace runs; the device
haystack was only lower-cased. DevLoop returns screen text with the app's own spacing
(orgzly's breadcrumb is `Getting Started with Orgzly  •  Notes`, two spaces each side)
and Android draws a 12-hour time with U+202F, so a witness, a `present:` evidence string
or a report quote the device really showed could never match. The fold now happens once,
where `_device_texts` / `_observation_texts` build the lists, and it is the same per-text
fold the corpus gate (`derive_journey._screen_has`) derived every witness under — the
corpus-wide tests below hold the scorer to exactly that."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from qualgentbench import corpus, journey

from test_bugs import _call, _obs, _transcript
from test_journey import _bug, _spec, _task, _write

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "derive_journey.py"


def _derive():
    spec = importlib.util.spec_from_file_location("derive_journey_ws", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dj = _derive()

BREADCRUMB = "Getting Started with Orgzly  •  Notes"          # as the device sends it
SCREEN = json.dumps({"elements": [{"text": BREADCRUMB, "id": "breadcrumbs_text"},
                                  {"text": "Welcome to Orgzly!"}]}, ensure_ascii=False)


def _witnessed(w: str) -> dict:
    return {"mode": "present", "expect": {"present": w}, "evidence": [w], "witness": [w]}


# ── the acceptance case: `A  •  B` on the device ──────────────────────────────

@pytest.mark.parametrize("authored", [BREADCRUMB, "Getting Started with Orgzly • Notes"])
def test_a_double_spaced_device_string_credits_witness_evidence_and_grounding(authored):
    """One MCP result carrying `A  •  B` credits the witness, the present-oracle evidence
    and report grounding, whether the case/report authored the string double-spaced (as
    the device showed it) or single-spaced."""
    v = journey.journey_verdict(_transcript(
        _obs(SCREEN),
        _write("pass", _bug(1, authored, "the breadcrumb is shown"))),
        "m", _task(_spec("clean", oracle=_witnessed(authored))))
    m = v.metrics
    assert m["witness"]["seen"] == [authored] and m["witness"]["scored"] is True
    assert m["completed"] is True, m["completion_reason"]
    assert m["oracle"]["ok"] is True                        # present evidence: `e in t`
    # Grounding is read off device RESULTS only: the quote is what the device answered.
    report = journey.parse_report(f"verdict: pass\nbugs:{_bug(1, authored, 'x')}\n")
    obs = journey._evidence(report.bugs[0].observed)
    assert any(obs in t for t in journey._device_texts(_transcript(_obs(SCREEN)), "mcp",
                                                       results_only=True))


def test_grounding_uses_the_folded_results(monkeypatch):
    """The scorer's own grounding flag, not a re-implementation of it."""
    seen = []
    real = journey.match_report
    monkeypatch.setattr(journey, "match_report", lambda b, spec: (seen.append(b.grounded), real(b, spec))[1])
    journey.journey_verdict(_transcript(
        _obs('{"text": "TODO  #B  Book flights"}'), _obs('{"text": "9:00\u202fAM"}'),
        _write("pass", _bug(1, "TODO  #B  Book flights", "x") + _bug(2, "9:00 AM", "y")
               + _bug(3, "DONE  Water the plants", "never on the device"))),
        "m", _task(_spec("clean")))
    assert seen == [True, True, False]


@pytest.mark.parametrize("space", ["\u202f", "\u00a0"])
def test_typographic_spaces_fold_like_derive_hits(space):
    """`9:00\\u202fAM` (U+00A0 on older images) is `9:00 AM` — the fold `derive_journey._hits`
    and `replay._fold` already apply, now on the device side too."""
    v = journey.journey_verdict(_transcript(
        _obs(json.dumps({"text": f"Aspirin 9:00{space}AM"}, ensure_ascii=False)), _write("pass")),
        "m", _task(_spec("clean", oracle=_witnessed("9:00 AM"))))
    assert v.metrics["witness"]["seen"] == ["9:00 AM"] and v.metrics["completed"] is True
    assert journey._device_text(f"9:00{space}AM") == "9:00 am"
    assert journey._evidence(f"9:00{space}AM") == "9:00 am"          # the needle folds the same


def test_the_fold_does_not_loosen_token_boundaries():
    """Collapsing spaces must not let a witness match inside a longer token."""
    v = journey.journey_verdict(_transcript(_obs('{"text": "Total:  40 items"}'), _write("pass")),
                                "m", _task(_spec("clean", oracle=_witnessed("Total: 4 items"))))
    assert v.metrics["witness"]["missing"] == ["Total: 4 items"] and v.metrics["completed"] is False


def test_the_screenshot_exemption_still_reads_lines():
    """`_witness_capable` judges an MCP result LINE by line; a folded payload would merge a
    status line (`ERROR: …`) into the content after it and wrongly grant the exemption.
    It reads the unfolded form (`fold=False`), so a real screen after an error line still
    scores the missing witness as a miss, not as unscorable."""
    t = _transcript(_obs('Error: element not found\n{"text": "Weight  Min: 74 kg"}'), _write("pass"))
    v = journey.journey_verdict(t, "m", _task(_spec("clean", oracle=_witnessed("Max: 85 kg"))))
    assert v.metrics["completed"] is False and v.metrics["completion_scored"] is True
    raw = journey._observation_texts(t, "mcp", screen_only=True, fold=False)
    assert raw == ['error: element not found\n{"text": "weight  min: 74 kg"}']
    assert journey._observation_texts(t, "mcp") == ['error: element not found {"text": "weight min: 74 kg"}']


def test_an_absent_text_never_becomes_evidence():
    """An `absent:` string is the thing that must NOT be on screen. The scorer never reads
    it as evidence — only the declared witness is — so folding the device text cannot turn
    the absent string's appearance into completion."""
    oracle = journey._oracle({"check": {"expect": {"absent": "Old  Title"}}, "evidence": ["New Title"]})
    assert oracle["mode"] == "absent" and oracle["evidence"] == ["New Title"]
    assert oracle["witness"] == ["New Title"]
    v = journey.journey_verdict(_transcript(_obs('{"text": "Old  Title"}'), _write("pass")),
                                "m", _task(_spec("clean", oracle=oracle)))
    assert v.metrics["completed"] is False and v.metrics["witness"]["missing"] == ["New Title"]


# ── the committed corpus: the scorer finds exactly what the derive found ─────

def _rows():
    for app in corpus.public_apps():
        doc = journey.load_cases(app)
        if not doc:
            continue
        cases = {str(c["id"]): c for c in doc.get("test_cases") or []}
        for cid, row in journey.load_truth(app).items():
            if row.get("screens") and cid in cases:
                yield cid, cases[cid], row


def _strings(case: dict, row: dict) -> list[str]:
    """Every string a scorer or a derive ever holds against a screen for this case: the
    witness, the `present:`/`absent:` expectations and each display bug's measured texts."""
    expect = (case.get("check") or {}).get("expect") or {}
    out = [str(w) for w in case.get("evidence") or []]
    out += [str(expect[k]) for k in ("present", "absent") if isinstance(expect.get(k), str)]
    for s in row.get("side") or []:
        out += [str(s.get("marker") or "")] + [str(t) for t in s.get("texts") or []]
    return [s for s in dict.fromkeys(out) if journey._evidence(s)]


def test_the_scorer_fold_reproduces_every_committed_witness_row():
    """Every committed `witness` step map was derived with `_screen_has` (the scorer's
    `_word` over `journey._norm` of each recorded node). Re-read through the scorer's own
    haystack fold (`_device_text`) the committed screens give the identical map."""
    checked, cases = 0, set()
    for cid, case, row in _rows():
        for w, arms in (row.get("witness") or {}).items():
            cases.add(cid)
            needle = journey._evidence(w)
            for arm, steps in arms.items():
                got = [i + 1 for i, screen in enumerate(row["screens"][arm])
                       if any(journey._word(needle, journey._device_text(t)) for t in screen)]
                assert got == steps, (cid, w, arm)
                checked += 1
    # The loop must not go quiet: 14 witness strings x 2 arms today, and the row this
    # ticket is about must be among them.
    assert checked >= 28 and "orgzly-open-note-from-notebook" in cases
    row = journey.load_truth("orgzly")["orgzly-open-note-from-notebook"]
    assert BREADCRUMB in row["witness"]


def test_the_scorer_never_matches_what_the_derive_did_not():
    """End to end through the MCP path — every recorded screen served as one observe
    result, the way DevLoop answers — the scorer's witness match (`_word`) and its
    present-evidence match (`in`) never find a witness, a `present:`/`absent:` string or
    a measured display text on a screen where the derive's own fold did not. An
    `absent:` text the scorer newly found would be exactly that regression."""
    checked = 0
    for cid, case, row in _rows():
        strings = _strings(case, row)
        for arm, screens in row["screens"].items():
            for i, screen in enumerate(screens):
                texts = journey._observation_texts(
                    _transcript(_obs(json.dumps(screen, ensure_ascii=False))), "mcp")
                seen = journey._witness({"oracle": {"witness": strings}}, texts)["seen"]
                for s in strings:
                    derive_word = dj._screen_has(screen, s)
                    derive_in = any(journey._norm(s) in journey._norm(t) for t in screen)
                    assert (s not in seen) or derive_word, (cid, arm, i + 1, s)
                    ok, _ = journey._oracle_verdict({"oracle": {"mode": "present", "evidence": [s]}}, texts)
                    assert not ok or derive_in, (cid, arm, i + 1, s)
                    checked += 1
    assert checked >= 400             # 490 string x screen pairs on the public corpus today
