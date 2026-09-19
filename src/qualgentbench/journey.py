"""Journey mode — one episode = one app + one test case + one VERSION of it.

The agent gets the test case in the shape a QA team stores it (name, steps, expected
outcome) and nothing else. Every case runs in two versions that differ only in the
defect flags written at staging:

  clean   no defect on — measures instruction following: did the agent execute the
          steps (a device oracle says so), report pass, and report no bugs (every bug
          reported on a clean build is a false report by definition)
  seeded  the case's own `bugs:` on — instruction following again, plus bug finding:
          of the N bugs on this build and route, how many did it report, and how many
          reports match nothing

Two numbers come out, never blended: COMPLETION (per episode, verified on the device)
and BUG FINDING (found / present, false reports, one F1 from the totals).

The key is authored in `data/test-cases/<app>.yaml`: defects (kind, marker, symptom
vocabulary) and per case the route, the oracle and the `bugs:` list. Everything else
is derived from that list: one functional bug makes the seeded version BLOCKED
(expected FAIL, that bug is the blocking one); display bugs are the side bugs.
`scripts/derive_journey.py` confirms the key by execution once per app (the corpus
gate) and records the exact screen strings each bug changes. No replay of agent
claims: scoring is a text comparison plus one device oracle after the agent exits."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import brief as _brief
from . import corpus, pricing, rates, submission
from .result import VerifierResult
from .task import BenchmarkTask
from .transcript import TranscriptParser

TASK_TYPE = "journey_case"
MODE = "journey"
VERSIONS = ("clean", "seeded")
FILENAME = submission.FILENAME           # the same file name in every mode: one contract to learn

_DATA = Path(__file__).parent / "data"
_CASES_DIR = _DATA / "test-cases"
_TRUTH_DIR = _DATA / "truth"
# Every path below resolves the held-out directory FIRST (`QGB_HELDOUT_DIR`, see
# corpus.py), then the packaged data: a held-out app runs like a public one.

_RESULT_RE = re.compile(r"RESULT:\s*verdict\s*=\s*(?P<v>pass|fail)", re.I)


# ── loading ────────────────────────────────────────────────────────────────────

def cases_path(app_id: str) -> Path:
    return corpus.resolve(f"test-cases/{app_id}.yaml")


def truth_path(app_id: str) -> Path:
    """Held-out first. For an app whose CASES are held out this points into the held-out
    directory even before a truth file exists there, so `derive_journey.py` writes the
    derived truth beside the cases and never back into the repository."""
    return corpus.resolve(f"truth/journey-{app_id}.json", app_id=app_id)


def load_cases(app_id: str) -> dict | None:
    p = cases_path(app_id)
    return yaml.safe_load(p.read_text()) if p.exists() else None


def load_truth(app_id: str) -> dict:
    p = truth_path(app_id)
    return json.loads(p.read_text()) if p.exists() else {}


def has_cases(app_id: str) -> bool:
    return cases_path(app_id).exists()


def case_ids(app_id: str) -> list[str]:
    """Every test-case id of an app, in file order. The ids `--case` selects from;
    read off the case file so a held-out app's cases are as selectable as a public
    app's (`cases_path` resolves the held-out directory first)."""
    doc = load_cases(app_id)
    return [str(c["id"]) for c in (doc or {}).get("test_cases", []) if c.get("id")]


def known_case_ids(apps: list[dict[str, Any]]) -> dict[str, str]:
    """{case id: app id} over the given app suites, in app then file order — what a
    `--case` filter is validated against, and what its error message lists."""
    out: dict[str, str] = {}
    for suite in apps:
        app_id = str((suite.get("app") or {}).get("id", ""))
        for cid in case_ids(app_id):
            out.setdefault(cid, app_id)
    return out


def apk_meta(app_id: str) -> dict | None:
    """The journey build of an app: the test-case file's `apk:` block (published
    under journey/ on HuggingFace, sha256-verified). Journey-only defects live in
    this build, not in the hunt build the benchmark spec points at."""
    doc = load_cases(app_id)
    meta = (doc or {}).get("apk")
    return (dict(meta) if isinstance(meta, dict) and (meta.get("filename") or meta.get("path"))
            else None)


def task_id(case_id: str, version: str) -> str:
    return f"{case_id}~{version}"


def split_task_id(tid: str) -> tuple[str, str]:
    """`case~version` → (case, version). A bare id is the seeded version (old runs)."""
    if "~" in tid:
        case, version = tid.rsplit("~", 1)
        if version in VERSIONS:
            return case, version
    return tid, "seeded"


# The closed vocabulary for a defect's `class:` — its fault class (QUA-2724; what each
# means, and the rule for an ambiguous defect, are in docs/defect-classes.md). Every
# `defects:` entry carries one: `scripts/lint_journey_cases.py` fails without it and
# `scripts/mix_report.py` counts the corpus by it. It is corpus METADATA: `load_defects`
# below does not copy it, so no matcher, scorer or adversary ever sees it and a
# reclassification cannot move a score. It does move `corpus_version`, like any byte of
# a test-case file.
DEFECT_CLASSES = ("crash", "anr", "stuck", "navigation", "lifecycle", "ordering",
                  "persistence", "layout", "widget-inventory", "content-format")


def load_defects(doc: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in doc.get("defects", []):
        out[str(d["id"])] = {
            "kind": str(d.get("kind") or "functional").lower(),
            "tier": str(d.get("tier") or ""),
            "marker": str(d.get("marker") or ""),
            "symptoms": [str(s).lower() for s in (d.get("symptoms") or [])],
        }
    return out


def case_bugs(case: dict) -> list[dict]:
    """`bugs:` entries normalised to {id, marker}; marker overrides the defect's."""
    out = []
    for b in case.get("bugs") or []:
        if isinstance(b, dict):
            out.append({"id": str(b.get("id")), "marker": str(b.get("marker") or "")})
        else:
            out.append({"id": str(b), "marker": ""})
    return out


def case_design(case: dict, defects: dict[str, dict]) -> dict:
    """What the `bugs:` list implies for the seeded version. One functional bug at
    most: it is the blocking one and the case is expected to FAIL. Display bugs are
    the side bugs a complete report names."""
    bugs = case_bugs(case)
    unknown = [b["id"] for b in bugs if b["id"] not in defects]
    if unknown:
        raise ValueError(f"{case.get('id')}: unknown defect id(s) {unknown}")
    functional = [b for b in bugs if defects[b["id"]]["kind"] == "functional"]
    if len(functional) > 1:
        raise ValueError(f"{case.get('id')}: a case may carry at most one functional bug, "
                         f"got {[b['id'] for b in functional]}")
    blocking = functional[0]["id"] if functional else None
    side = [{"bug": b["id"], "marker": b["marker"] or defects[b["id"]]["marker"]}
            for b in bugs if defects[b["id"]]["kind"] != "functional"]
    return {"bugs": [b["id"] for b in bugs], "blocking": blocking, "side": side,
            "expected": "FAIL" if blocking else "PASS",
            # How the seeded arm is expected to fail when the check says so: a
            # `crash:`/`anr:`/`stuck:` key is a claim about the KIND of failure, so a
            # seeded arm that merely violates its state oracle does not agree.
            "death": expected_death(case) if blocking else None}


_LIVENESS_KEYS = ("stuck", "anr", "crash")


def expected_death(case: dict) -> str | None:
    """`"crash"` | `"anr"` | `"stuck"` | None — the liveness assertion the case's
    `check.expect` carries (standalone or riding on a state oracle). `stuck` and `anr`
    both mean the app hangs; `crash` covers any death of the app's own process."""
    expect = ((case.get("check") or {}).get("expect")) or {}
    for k in _LIVENESS_KEYS:
        if expect.get(k):
            return k
    return None


# What Android itself puts on the screen when an app's process dies or hangs — the
# only text a crash leaves behind for an agent to QUOTE. The wordings have changed
# across releases ("has stopped" → "keeps stopping"), and a tester writes whichever
# one their device showed, so all of them are evidence.
_CRASH_DIALOG_TEXTS = ("keeps stopping", "has stopped", "stopped working",
                       "closed unexpectedly")
_ANR_DIALOG_TEXTS = ("isn't responding", "is not responding", "not responding")


def crash_evidence(gate: dict | None, app_name: str = "") -> dict[str, list[str]]:
    """The screen strings an honest report of a DEATH can quote, for a case whose
    `check.expect` carries a `crash:`/`anr:`/`stuck:` gate — split by what quoting
    one of them PROVES.

    A death leaves no screen DIFF to measure. The route ends where the app ended, so
    the strings the clean arm went on to show are precisely the ones the agent could
    NOT have seen — which is why `journey_tasks` leaves `blocking_texts` empty for
    such a case and builds this instead. Two sources, and they are not equivalent:

      * `signature` — what the case itself names (`crash: "NoSuchElementException"`,
        `anr: "Input dispatching timed out"`): the exception an agent reads off the
        crash dialog's details or the device log. It is DEFECT-IDENTIFYING — it
        names this death and not another one, and it is nowhere in the brief — so
        quoting it is evidence on its own.
      * `dialog` — the platform's own wording, bare and qualified with the app's
        display name ("MedTimer keeps stopping"). It is what the device puts up, so
        an honest report does quote it; but it is IDENTICAL across every crash in
        the corpus and is fully predictable from the app name, so it identifies
        nothing by itself. `journey_tasks` files it under `echo_texts`, where credit
        additionally requires that the device itself answered with it (QUA-2717).

    A `stuck:` value is a UI ANCHOR, not a signature, so it is never quoted here; the
    hang it describes still ends in the "not responding" dialog, which is.
    """
    gate = gate or {}
    kinds = [k for k in _LIVENESS_KEYS if gate.get(k)]
    if not kinds:
        return {"signature": [], "dialog": []}
    signature = [v for v in (gate.get(k) for k in ("crash", "anr")) if isinstance(v, str)]
    wordings: list[str] = []
    if "crash" in kinds:
        wordings += list(_CRASH_DIALOG_TEXTS)
    if "anr" in kinds or "stuck" in kinds:
        wordings += list(_ANR_DIALOG_TEXTS)
    dialog = list(wordings)
    if app_name.strip():
        dialog += [f"{app_name.strip()} {w}" for w in wordings]
    return {"signature": sorted({t for t in signature if _evidence(t)}),
            "dialog": sorted({t for t in dialog if _evidence(t)})}


# The route keys whose VALUE is text the route puts on screen or aims at: typed text,
# anchors, and the row label a scoped tap names. Every other key is a keyword (`press:
# back`, `swipe: up`, `rotate: landscape`), which is not screen text.
ECHO_ROUTE_KEYS = ("type", "append", "tap", "long_press", "row")


def echo_haystack(case: dict) -> str:
    """Everything this case HANDS the agent or types on its behalf, as one normalised
    blob: the brief it reads (`name`, `steps`, `expected_outcome` — exactly what
    `brief()` composes) and every value the route types or taps: `type`/`append` text,
    `tap`/`long_press` anchors and the `row:` label a scoped tap names (QUA-2739 — a row
    label is on screen by the case's construction exactly as an anchor is).

    A screen string that appears in here is not self-authenticating. `Lunch` really is
    on the seeded calendar after a delete that did not delete, and quoting it really is
    what an honest tester writes — but the brief also says `Enter the title "Lunch"`,
    so an agent that never started the app can write the same word. The same holds for
    a route anchor: the harness knows it is on screen, so its presence there is a
    property of the case, not a sighting of the defect.

    Strings that land here are DEMOTED, never deleted (`echo_texts`): they still earn
    the bug when the device itself answered with them. Nothing legitimate is lost —
    the report is only asked to show it was there."""
    parts = [str(case.get("name") or ""), str(case.get("expected_outcome") or "")]
    parts += [str(s) for s in (case.get("steps") or [])]
    for step in ((case.get("check") or {}).get("steps") or []):
        if isinstance(step, dict):
            for key in ECHO_ROUTE_KEYS:
                if key in step:
                    parts.append(str(step[key]))
    return _norm(" \n ".join(p for p in parts if p))


def _echoable(text: str, haystack: str) -> bool:
    """Is this screen string one the brief or the route already put in the agent's
    hands? Token boundaries, the same matcher a quote gets."""
    needle = _evidence(text)
    return bool(needle) and _word(needle, haystack)


def _oracle(case: dict) -> dict:
    """The completion oracle: the case's `check.expect`, plus `evidence` strings for
    outcomes that can only be read off a screen (the agent's own device output must
    contain them). An `absent:` outcome needs explicit evidence. The liveness keys
    (`crash`/`anr`/`stuck`) ride along as `gate` — the runner evaluates them off the
    episode's own crash/ANR record and, for `stuck`, a probe tap after the agent
    exits — and are the mode itself when the check carries nothing else.

    `witness` is present only when the case DECLARES `evidence:` — its screen witness
    (docs/journey-oracle-audit.md): the strings completion is scored on, in every
    mode. `evidence` alone cannot say so, because a `present:` case without one gets
    the present string there as a fallback, and that case must stay unscored."""
    expect = dict(((case.get("check") or {}).get("expect")) or {})
    declared = [str(e) for e in (case.get("evidence") or []) if str(e).strip()]
    evidence = [str(e) for e in (case.get("evidence") or [])]
    gate = {k: expect[k] for k in _LIVENESS_KEYS if expect.get(k)}
    if "db" in expect:
        mode = "db"
    elif "content" in expect:
        mode = "content"                 # ContentProvider query, evaluated on the device like db
    elif "present" in expect:
        mode = "present"
        evidence = evidence or [str(expect["present"])]
    elif "absent" in expect:
        mode = "absent"
    elif gate:
        mode = expected_death(case) or "none"   # standalone liveness oracle
    else:
        mode = "none"
    out = {"mode": mode, "expect": expect, "evidence": evidence}
    if declared:
        out["witness"] = declared
    if gate:
        out["gate"] = gate
    return out


def journey_tasks(suite: dict[str, Any]) -> list[BenchmarkTask]:
    """Two BenchmarkTasks per test case (clean, seeded); a case with no `bugs:` has
    only the clean version. The agent-facing fields go into the brief; everything
    else rides in bug_spec for the scorer and is never shown."""
    app = suite["app"]
    app_id = str(app.get("id", ""))
    doc = load_cases(app_id)
    if not doc:
        return []
    truth = load_truth(app_id)
    defects = load_defects(doc)
    tasks: list[BenchmarkTask] = []
    for case in doc.get("test_cases", []):
        cid = str(case["id"])
        design = case_design(case, defects)
        measured = truth.get(cid) or {}
        by_bug = {s.get("bug"): s for s in measured.get("side", [])}
        side = []
        for s in design["side"]:
            got = by_bug.get(s["bug"]) or {}
            side.append({**s, "texts": list(got.get("texts") or []),
                         "visible_steps": list(got.get("visible_steps") or [])})
        # `unclaimed_diff` is EVERY string that differed between the clean and seeded
        # screens, so it carries strings that identify nothing: a contacts section index
        # (`A`), bare digits (`1`, `3`, `4`). They are filtered here, where the evidence
        # is built — with `A` in the list, every possible report matched contacts-delete's
        # blocking bug. `$ 75.00`, `Call dentist` and `4:32 PM` all survive.
        oracle = _oracle(case)
        # A DEATH case gets crash evidence instead of the screen diff: see
        # `crash_evidence`. The two are mutually exclusive on purpose — crediting a
        # crash report for quoting a string only the CLEAN arm ever showed would
        # reward a guess, and every string in `unclaimed_diff` on such a case is one.
        death = design["death"]
        # The diff has two SIDES and they are not interchangeable (QUA-2717). `added`
        # is what the SEEDED build put on screen and the clean one did not — the only
        # thing an agent on this build can have OBSERVED. `removed` is the clean
        # build's, which the seeded agent by definition never saw: it is what the
        # report EXPECTED and did not get, and that is the field it belongs in. Before
        # the split, `observed: "Standup"` — a title the route types, present only on
        # the clean arm's final screen — earned the blocking bug on
        # cal-switch-back-to-list with no device contact at all.
        added, removed = set(), set()
        if design["blocking"] and not death:
            for d in measured.get("unclaimed_diff", []):
                added |= {t for t in d.get("added", []) if _evidence(t)}
                removed |= {t for t in d.get("removed", []) if _evidence(t)}
        hay = echo_haystack(case)
        blocking_texts = sorted(t for t in added if not _echoable(t, hay))
        # Echoable but real: on screen, and also in the agent's hands already. Credit
        # needs the device to have answered with it — see `match_report`.
        echo_texts = sorted(t for t in added if _echoable(t, hay))
        # An absence has nothing to quote: the report names the clean-build string it
        # expected. Echoable ones are dropped outright — unseeable AND guessable.
        # TODO(QUA-2706): some of these are WALL-CLOCK-DERIVED and rot, silently.
        # `cal-switch-back-to-list` was derived on 2026-09-16 and its three are
        # `New Event` (static chrome), `16 Wednesday` (the day view's header — the
        # derivation DAY) and `02:00 AM` (Fossify's next-full-hour default for a new
        # event — the derivation HOUR). Only the first survives a different calendar
        # day. Nothing re-reads the device at scoring time, so the frozen truth stays
        # self-consistent; what rots is the MATCH: `match_report` compares these against
        # the report's `expected`, so an agent running on the 17th that correctly writes
        # "expected the day view for 17 Thursday" earns nothing from the absence route
        # (the other three routes still stand, so the case does not break — it silently
        # gets harder). Two fixes, neither cheap: pin the device clock the way
        # `QGB_DEVICE_TIMEZONE` pins the zone (a fixture, see TODO(fixture) in
        # medtimer.yaml), or teach `derive_journey` to drop a diff string that a
        # re-derivation at another time would not reproduce. Do not "fix" it by hand-
        # editing the truth file: the truth is derived, never asserted, and the edit
        # moves `corpus_version`. It touches scoring — leave it to QUA-2717's successor.
        absence_texts = sorted(t for t in removed if not _echoable(t, hay))
        crash_texts: list[str] = []
        if design["blocking"] and death:
            ev = crash_evidence(oracle.get("gate"), str(app.get("name") or app_id))
            crash_texts = ev["signature"]        # names THIS death; evidence on its own
            echo_texts = ev["dialog"]            # platform chrome; needs grounding
        versions = ["clean"] + (["seeded"] if design["bugs"] else [])
        for version in versions:
            seeded = version == "seeded"
            spec = {
                "mode": MODE,
                "app_id": app_id,
                "case_id": cid,
                "version": version,
                "name": str(case.get("name") or cid),
                "steps": [str(s) for s in (case.get("steps") or [])],
                "expected_outcome": str(case.get("expected_outcome") or "").strip(),
                "step_budget": int(case.get("step_budget") or 40),
                # What staging switches on (write_bug_flags reads `active_bugs`).
                "active_bugs": design["bugs"] if seeded else [],
                "expected": design["expected"] if seeded else "PASS",
                "blocking": design["blocking"] if seeded else None,
                # Self-authenticating: only the seeded build ever showed these, and
                # neither the brief nor the route handed them over.
                "blocking_texts": blocking_texts if seeded else [],
                # The exception the case names — defect-identifying on its own.
                "crash_texts": crash_texts if seeded else [],
                # Real evidence the agent could also have written blind (a brief noun,
                # a route anchor, the platform's crash dialog): credited only when the
                # DEVICE answered with it.
                "echo_texts": echo_texts if seeded else [],
                # What the clean build showed and this one lost — matched against the
                # report's `expected`, never its `observed`.
                "absence_texts": absence_texts if seeded else [],
                "side": side if seeded else [],
                "defects": defects,
                "oracle": oracle,
                "truth_agrees": measured.get("agrees") if measured else None,
                "device_setup": suite.get("device_setup"),
                "shared_storage": suite.get("shared_storage"),
                # Public or held-out — one flag, read off where the case file lives.
                "heldout": corpus.is_heldout(app_id),
            }
            tasks.append(BenchmarkTask(
                id=task_id(cid, version),
                name=spec["name"],
                instruction="",                 # composed by brief()
                app_file_id="",
                app_name=str(app.get("name") or app_id),
                platform=str(app.get("platform") or "android"),
                bundle_id=str(app.get("package") or ""),
                bug_spec=spec,
            ))
    return tasks


# ── the brief ──────────────────────────────────────────────────────────────────

def brief(task: BenchmarkTask, device_serial: str, tooling: str) -> str:
    """The agent's assignment. Byte-identical across arms except the tooling note,
    and byte-identical across the two versions of a case. Names no feature area and
    never hints that anything is wrong."""
    spec = task.bug_spec or {}
    # One text, shared with the hunt brief and versioned there (`brief.BRIEF_VERSION`).
    tooling_note = _brief.tooling_note(tooling, device_serial)
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(spec.get("steps") or [], 1))
    return f"""You are a QA engineer executing one test case on the Android app `{task.app_name}`.

## Device & App
The app (`{task.bundle_id}`) is installed and running on Android device `{device_serial}`.
{tooling_note}

## Important
You do NOT have the app's source code — there is nothing to read. Execute the test
case ONLY by interacting with the running app on the device and observing what it
actually does. Follow the steps in order, then compare what you observe with the
expected outcome. Report anything that looked wrong along the way, whether or not the
test case reached its expected outcome. Report only what you actually saw.

## Test case: {spec.get('name')}
Steps:
{steps}
Expected outcome: {spec.get('expected_outcome')}

## How to report
Before you finish, write the file `{FILENAME}` in your working directory:

  verdict: pass          # pass = the expected outcome was met; fail = it was not
  bugs:                  # every problem you noticed, whether or not it failed the test
    - step: 3            # the step you were on when you saw it
      screen: "Order summary"              # where you were, in your own words
      observed: "Total: 3 items"           # text QUOTED from the screen, exactly as shown
      expected: "Total: 4 items"
      description: "four items are listed but the total says three"

  - Use `bugs: []` when nothing looked wrong.
  - One entry per distinct problem. `observed` must be text you saw on the device.
  - If the expected outcome was NOT met, the first entry is the reason it failed.
  - QUOTE every value: an unquoted value containing `: ` breaks the file.

Finish your FINAL message with one line, as a fallback in case the file was missed:
  RESULT: verdict=<pass|fail>
"""


# ── the report ─────────────────────────────────────────────────────────────────

@dataclass
class BugReport:
    step: int | None
    screen: str
    observed: str
    expected: str
    description: str
    matched: str | None = None      # active defect id this report describes
    grounded: bool = False          # the DEVICE answered with `observed` (results, not calls)

    @property
    def prose(self) -> str:
        """The report's own CLAIM, in the tester's words — the only part of an entry
        that is not a quote or a label. `observed` is "text QUOTED from the screen" and
        `expected` is the value it should have been (the brief says so, in those words);
        `screen` is a location. Symptom vocabulary is read here and nowhere else, so
        that echoing a screen string is never mistaken for describing a misbehaviour:
        a report quoting the button `Delete` has not said that deleting is broken
        (QUA-2717 — `delete`, `back`, `tags`, `rename` and `not responding` are all
        literally symptom entries in the corpus AND words the briefs themselves use).

        This replaced a `text` property that joined all four fields. It is gone rather
        than left unused on purpose: it reads like the matching surface, and anything
        that picked it up again would reopen the hole."""
        return self.description.lower()

    def as_dict(self) -> dict:
        return {"step": self.step, "screen": self.screen, "observed": self.observed,
                "expected": self.expected, "description": self.description,
                "matched": self.matched, "grounded": self.grounded}


@dataclass
class Report:
    verdict: str | None = None      # "pass" | "fail" | None
    bugs: list[BugReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: str = ""


def _norm_verdict(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in ("pass", "passed", "ok", "as_specified"):
        return "pass"
    if v in ("fail", "failed", "failure", "deviates", "broken"):
        return "fail"
    return None


def parse_report(text: str) -> Report:
    """Parse the journey findings file. Tolerant: a missing or malformed `bugs`
    list costs the bugs, never the verdict; a non-mapping document is an error."""
    rep = Report()
    if not (text or "").strip():
        rep.errors.append("empty report")
        return rep
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        rep.errors.append(f"invalid YAML: {str(exc).splitlines()[0]}")
        m = re.search(r"^\s*verdict\s*:\s*([A-Za-z_]+)", text, re.M)
        rep.verdict = _norm_verdict(m.group(1)) if m else None
        return rep
    if not isinstance(doc, dict):
        rep.errors.append("report must be a mapping with `verdict` and `bugs`")
        return rep
    rep.verdict = _norm_verdict(doc.get("verdict"))
    if rep.verdict is None:
        rep.errors.append(f"verdict must be pass|fail, got {doc.get('verdict')!r}")
    bugs = doc.get("bugs")
    if bugs is None:
        bugs = []
    if not isinstance(bugs, list):
        rep.errors.append("`bugs` must be a list")
        bugs = []
    for i, b in enumerate(bugs):
        if not isinstance(b, dict):
            rep.errors.append(f"bug {i}: not a mapping")
            continue
        step = b.get("step")
        try:
            step = int(step) if step is not None else None
        except (TypeError, ValueError):
            step = None
        rep.bugs.append(BugReport(
            step=step,
            screen=str(b.get("screen") or "").strip(),
            observed=str(b.get("observed") or "").strip(),
            expected=str(b.get("expected") or "").strip(),
            description=str(b.get("description") or b.get("actual") or "").strip(),
        ))
    return rep


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip('"\'').lower())


# A quoted screen string is evidence only if it is long enough to be one. The corpus
# carries one-character "evidence" on both sides: hand-authored display markers (`2` for
# ankidroid's new-card count, `1` for the tasks.org subtask chip) and auto-derived
# blocking_texts straight out of the screen diff (`A` from the contacts section index,
# bare digits). One character appears in honest prose about anything — "Total: 2 items",
# "Due in 1 day", "anything at all" — so credit for it is free. Two characters can still
# be a real marker (orgzly's priority letter is `#B`), so the line sits at 2.
_MIN_EVIDENCE_CHARS = 2


def _word(needle: str, text: str) -> bool:
    """Is `needle` in `text` on token boundaries? 'age' must not match inside 'average',
    and '9 left' must not match inside '19 left'. One matcher for symptom words and for
    quoted screen strings alike: both need regex escaping, and both need boundaries that
    the non-alphanumeric characters in real markers (`#B`, `Avg:`, `$ 75.00`) survive."""
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", text) is not None


def _evidence(s: str) -> str:
    """A quoted screen string normalised for matching, or "" when it is too short to
    identify anything. The one gate on every marker, measured text and blocking text."""
    n = _norm(s)
    return n if len(n) >= _MIN_EVIDENCE_CHARS else ""


def _quote_rules_out(bug_id: str, spec: dict, observed: str) -> bool:
    """Does the report's own screen quote rule this defect out of the symptom route?

    A DISPLAY defect is nothing but a wrong string on a screen, and the corpus measured
    exactly which strings it changes. A report that quotes a screen value which neither
    contains nor sits inside any of them is quoting something else, so its prose cannot
    be credited for this defect: `Aspirin (10 left)` + "the label is left) aligned" is
    not a sighting of `9 left`, and `Standup` + "the event title looks wrong" is not a
    sighting of `New Evnet`. Prose alone still matches when the report quotes nothing (a
    functional misbehaviour often has no string to quote) and whenever the quote does
    overlap a measured string ("76" is a sighting of `Avg: 76 kg`)."""
    if ((spec.get("defects") or {}).get(bug_id) or {}).get("kind") != "display":
        return False
    measured = [_norm(t) for s in spec.get("side") or [] if s.get("bug") == bug_id
                for t in [s.get("marker") or ""] + list(s.get("texts") or [])]
    measured = [m for m in measured if m]
    if not measured or not observed:
        return False
    return not any(observed in m or m in observed for m in measured)


def _quoted_any(field: str, texts: list[str] | None) -> bool:
    """Does `field` quote any of `texts`, on token boundaries, above the evidence
    floor? One matcher for every list of screen strings the scorer holds."""
    return any(q and _word(q, field) for q in (_evidence(t) for t in texts or []))


def match_report(bug: BugReport, spec: dict) -> str | None:
    """Which bug ON THIS BUILD does this report describe? Side markers and measured
    texts first (specific), then the blocking bug's evidence, then the symptom
    vocabulary — all of them on TOKEN BOUNDARIES ('age' must not match inside
    'average'), and only entries long enough to carry information (`_evidence`). On a
    clean build nothing is active, so every report is a false report.

    Substring matching on short strings was a hole wide enough to score through: a
    marker of `2` matched "Total: 2 items", a derived blocking text of `A` matched every
    report ever written, and on contacts-delete~seeded that bought a fabricated report
    both recall AND completion (a blocked case completes on fail + the blocking bug
    named).

    The blocking bug has FOUR routes in, and they differ in what the quote proves
    (QUA-2717 — before it, all of them were one list and the difference was invisible):

      `blocking_texts`  only the seeded build showed it, and neither the brief nor the
                        route handed it over → the quote IS the sighting.
      `crash_texts`     the exception the case names → identifies this death and no
                        other, and appears nowhere the agent can read.
      `echo_texts`      on screen AND writable blind (a brief noun the route types, the
                        platform's "isn't responding"). Real evidence, but it proves
                        nothing until the DEVICE is the one that said it — so this
                        route, alone, demands `bug.grounded`.
                        TODO(QUA-2717): grounding proves PRESENCE, not attribution to
                        the defect. An agent that drives the route and then quotes
                        `Lunch` with vague prose still earns cal-delete-event's bug —
                        it saw the word, just not necessarily after the delete that
                        failed. Truth rows carry a `step` per `unclaimed_diff` entry,
                        so the tighter rule is "the device answered with it at or after
                        that step"; it needs transcript↔step alignment, which nothing
                        records today.
      `absence_texts`   the defect is a MISSING string, so there is nothing to observe.
                        The clean build's value is matched against the report's
                        `expected`, which is where the brief's own example puts it
                        ("observed: Total: 3 items / expected: Total: 4 items")."""
    observed = _norm(bug.observed)
    prose = bug.prose
    active = set(spec.get("active_bugs") or [])
    if not active:
        return None
    for s in spec.get("side") or []:
        if s["bug"] not in active:
            continue
        quoted = [_evidence(s.get("marker") or "")] + [_evidence(t) for t in s.get("texts") or []]
        if any(q and _word(q, observed) for q in quoted):
            return s["bug"]
    blocking = spec.get("blocking")
    if blocking and blocking in active:
        if (_quoted_any(observed, spec.get("blocking_texts"))
                or _quoted_any(observed, spec.get("crash_texts"))
                or (bug.grounded and _quoted_any(observed, spec.get("echo_texts")))
                or _quoted_any(_norm(bug.expected), spec.get("absence_texts"))):
            return blocking
    defects = spec.get("defects") or {}
    ordered = ([blocking] if blocking else []) + [s["bug"] for s in spec.get("side") or []]
    ordered += [d for d in defects if d not in ordered]
    for bug_id in ordered:
        if bug_id not in active or _quote_rules_out(bug_id, spec, observed):
            continue
        for sym in (defects.get(bug_id) or {}).get("symptoms") or []:
            if sym and _word(sym, prose):
                return bug_id
    return None


# ── the scorer ─────────────────────────────────────────────────────────────────

def _device_texts(transcript: str, tooling: str, *, results_only: bool = False) -> list[str]:
    """Lower-cased device payloads in transcript order. By default both what the agent
    sent to a device tool and what came back (grounding a report's quote accepts
    either); `results_only` keeps what the DEVICE answered — a screen witness must be
    read off the device, not typed into it."""
    from .bugs import _ordered_stream
    return [p for kind, p in _ordered_stream(transcript, tooling, split_calls=results_only)
            if kind == "device"]


# What makes a device result a SCREEN READ. MCP: the observation tools the transcript
# parser already treats as observations. Raw adb: the hierarchy dump and reading it back.
_RAW_OBSERVE_RE = re.compile(r"uiautomator\s+dump|cat\s+\S*\.xml|dumpsys\s+window|dumpsys\s+activity")


def _observation_texts(transcript: str, tooling: str, *, screen_only: bool = False) -> list[str]:
    """Device RESULTS that answered a screen read, in order. A tap's "ok" is a device
    result but not an observation: an agent that only ever gets acknowledgements back
    has not read any screen as text, and a witness cannot be held against it.

    ``screen_only`` narrows the raw arm to the commands that return the SCREEN
    (`_RAW_SCREEN_READ_RE`) rather than every read — used to decide whether an agent
    keeps the screenshot-only exemption, never to decide a match.
    """
    from .bugs import _ordered_stream
    from .transcript import OBSERVATION_TOOL_NAMES
    raw_re = _RAW_SCREEN_READ_RE if screen_only else _RAW_OBSERVE_RE
    out: list[str] = []
    last_call: str | None = None
    for kind, payload in _ordered_stream(transcript, tooling, split_calls=True):
        if kind == "device_call":
            last_call = payload
        elif kind == "device":
            call = last_call or ""
            observed = (any(t in call for t in OBSERVATION_TOOL_NAMES) if tooling != "raw"
                        else bool(raw_re.search(call)))
            if observed and payload.strip():
                out.append(payload)
            last_call = None
    return out


# Device text that is only a STATUS LINE — a shell exit code, a kill notice, a dump's
# "wrote it to this path" confirmation, a tool's own error. None of it is screen
# content, so none of it could ever have contained a witness.
#
# This is not a corner case, it is the ordinary answer of the path the brief used to
# name (QUA-2715): `adb shell uiautomator dump` writes the hierarchy to a FILE and
# answers with the confirmation line ALONE even when it fully succeeds — the content
# arrives later, from a separate `cat`. When the platform kills it instead, the answer
# is `exit=137` or `Killed`. Both shapes are observation RESULTS by
# `_RAW_OBSERVE_RE`, so before this predicate either one made `screen_texts`
# non-empty and disqualified an agent from the screenshot-only exemption below —
# scoring a correct episode `completed: false` for the shape of its tooling rather
# than for anything it did or failed to do. Measured on run 20260917-004716-9e69:
# 8 of 8 dumps killed, every real screen read taken as an image, verdict right,
# oracle satisfied, completion False.
_STATUS_ONLY_RE = re.compile(
    r"""^(?:
          UI \s+ hierarchy \s+ dumped \s+ to: .*
        | exit (?:\s+ code)? [=:\s]+ \d+
        | Killed (?: \s+ by \s+ signal .*)?
        | ERROR: .*
        # A shell's own complaint about the command, not the app's screen:
        # `cat: /sdcard/ui.xml: No such file or directory` is what a read of a dump
        # that never got written looks like, and it was the second shape (after
        # exit=137) that kept an agent out of the exemption in run
        # 20260917-021029-67f4.
        | \S+ : \s+ .*? : \s+ (?: no \s+ such \s+ file .* | permission \s+ denied
                                | not \s+ found | is \s+ a \s+ directory )
        | \S+ : \s+ (?: no \s+ such \s+ file .* | permission \s+ denied | not \s+ found )
        )$""",
    re.IGNORECASE | re.VERBOSE,
)

# Of the commands `_RAW_OBSERVE_RE` counts as observations, only these two return the
# SCREEN. `dumpsys window` / `dumpsys activity` answer which window has focus — real
# device text, genuinely useful to an agent, and structurally incapable of carrying a
# witness, because a witness is a string the app DREW. Run 20260917-021029-67f4 turned
# on this distinction: a lone `mCurrentFocus=Window{… org.fossify.calendar…}` line was
# the only "content" in the episode, and it cost a correct clean arm its exemption.
_RAW_SCREEN_READ_RE = re.compile(r"uiautomator\s+dump|cat\s+\S*\.xml")


# On the RAW arm a screen only ever comes back as a uiautomator hierarchy, which is
# XML: every string the app DREW arrives as a `text=` or `content-desc=` attribute.
# So "did this result carry a screen?" has an exact answer there, and it is the one
# thing a status-line blacklist could never get right — the agent in run
# 20260917-021029-67f4 CHAINED its reads (`uiautomator dump && dumpsys window`), so a
# single result mixed `exit code 137`, a `mCurrentFocus=` line and a bare `---`
# separator. Blacklisting is whack-a-mole against an agent's shell habits; asking for
# the attribute that a drawn string must travel in is not.
_HIERARCHY_ATTR_RE = re.compile(r'(?:text|content-desc)\s*=\s*"')


def _witness_capable(text: str, tooling: str = "mcp") -> bool:
    """Could this device result have carried a witness string at all?

    RAW arm: only a hierarchy dump can carry a drawn string, so require the XML
    attribute one would travel in. MCP arm: the observe tools return screen text
    directly, so anything that is not a bare status line counts.

    Deliberately one-sided: the predicate only decides whether an agent KEEPS the
    benefit of the doubt, so a false True merely scores the witness as before.
    """
    if tooling == "raw":
        return bool(_HIERARCHY_ATTR_RE.search(text))
    return any(line.strip() and not _STATUS_ONLY_RE.match(line.strip())
               for line in text.splitlines())


def _last_findings_write(transcript: str, tooling: str) -> str:
    from .bugs import _ordered_stream
    body = ""
    for kind, p in _ordered_stream(transcript, tooling):
        if kind == "findings":
            body = p
    return body


def _oracle_verdict(spec: dict, device_texts: list[str]) -> tuple[bool | None, str]:
    """Did the device confirm the expected outcome? A `db` oracle was evaluated by
    the runner after the agent exited (`oracle_result`); a screen outcome is proven
    by the agent's own device output containing the evidence strings. None = the
    outcome could not be checked, which never counts against the agent."""
    oracle = spec.get("oracle") or {}
    mode = oracle.get("mode")
    if mode in ("db", "content") or mode in submission.LIVENESS_MODES:
        got = spec.get("oracle_result")
        if got == "holds":
            return True, f"{mode} oracle holds"
        if got == "violated":
            return False, f"{mode} oracle violated: {spec.get('oracle_detail', '')}"
        return None, f"{mode} oracle not evaluated ({got or 'missing'})"
    evidence = [_norm(e) for e in oracle.get("evidence") or [] if str(e).strip()]
    if evidence:
        missing = [e for e in evidence if not any(e in t for t in device_texts)]
        if missing:
            return False, f"outcome text never seen on the device: {missing}"
        return True, "outcome text seen on the device"
    return None, "no oracle for this outcome"


def _witness(spec: dict, screen_texts: list[str]) -> dict:
    """The case's declared `evidence:` witnesses against the text the device showed the
    agent. A witness is SEEN when it appears on token boundaries (`_word` over
    `_evidence` — the matcher a report's quote gets) in any device RESULT. `scored`
    says whether the verdict used the answer; `journey_verdict` sets it, so here it is
    always False."""
    required = [str(w) for w in (spec.get("oracle") or {}).get("witness") or [] if str(w).strip()]
    seen: list[str] = []
    for w in required:
        needle = _evidence(w)
        if needle and any(_word(needle, t) for t in screen_texts):
            seen.append(w)
    return {"required": required, "seen": seen,
            "missing": [w for w in required if w not in seen], "scored": False}


def journey_verdict(transcript: str, model: str, task: BenchmarkTask) -> VerifierResult:
    from .bugs import _bash_adb_events, _count_tool_calls, _device_actions
    from .contamination import scan as contamination_scan

    spec = task.bug_spec or {}
    tooling = str(spec.get("tooling") or "mcp")
    version = str(spec.get("version") or "seeded")
    parser = TranscriptParser(transcript)
    contamination = contamination_scan(parser, spec.get("workspace"))

    # The report: the file as it finally stands, else the last write seen in the
    # transcript, else the RESULT line.
    text = spec.get("findings_file") or ""
    source = "findings_file" if text.strip() else ""
    if not text.strip():
        text = _last_findings_write(transcript, tooling)
        source = "transcript_write" if text.strip() else ""
    report = parse_report(text) if text.strip() else Report(errors=["no report written"])
    report.source = source
    if report.verdict is None:
        hits = _RESULT_RE.findall(transcript)
        if hits:
            report.verdict = hits[-1].lower()
            report.source = report.source or "result_line"

    # Evidence: the agent must have driven the device at all.
    observations = len(parser.observation_texts())
    device_calls = len(parser.successful_device_events())
    device_actions = _device_actions(parser, tooling)
    metered_total = spec.get("metered_total")
    if isinstance(metered_total, int) and metered_total > 0:
        device_actions = max(device_actions, metered_total)
    if tooling == "raw":
        evidence = len(_bash_adb_events(parser)) >= 1
    else:
        evidence = observations >= 1 and device_calls >= 1

    device_texts = _device_texts(transcript, tooling)
    active = list(spec.get("active_bugs") or [])
    expected = str(spec.get("expected") or "PASS").upper()
    blocking = spec.get("blocking")

    # Grounding is read off what the DEVICE ANSWERED, never off what the agent sent it
    # — the same rule the screen witness runs under, and for the same reason: a typed
    # argument never witnesses itself. It stopped being a bare diagnostic in QUA-2717:
    # `match_report`'s `echo_texts` route is gated on it, so an argument that grounded
    # its own quote would hand back exactly the hole that route closes.
    device_results = _device_texts(transcript, tooling, results_only=True)
    for b in report.bugs:
        obs = _evidence(b.observed)
        b.grounded = bool(obs) and any(obs in t for t in device_results)
        b.matched = match_report(b, spec)
    found = []
    for b in report.bugs:
        if b.matched and b.matched not in found:
            found.append(b.matched)
    missed = [b for b in active if b not in found]
    false_reports = sum(1 for b in report.bugs if b.matched is None)

    # ── completion: verified on the device, then the verdict ──────────────
    reported = report.verdict
    truncated = bool(spec.get("truncated"))
    oracle_ok, oracle_why = _oracle_verdict(spec, device_texts)
    # Witnesses are held against SCREEN READS only: a result that answered an
    # observation (MCP observe tools; a raw hierarchy dump). No screen read at all →
    # the witness is unscorable (None), never False — that is the screenshot-only agent
    # the stopgap protected, and a tap's "ok" must not turn it into a scored miss.
    screen_texts = _observation_texts(transcript, tooling)
    # The subset that could actually have carried a witness: a SCREEN read (not a
    # focus query) that came back with CONTENT (not a status line). Matching still
    # runs over everything the device said (`screen_texts`); this narrower list only
    # decides whether an agent that never got screen text back keeps the exemption.
    witnessable = [t for t in _observation_texts(transcript, tooling, screen_only=True)
                   if _witness_capable(t, tooling)]
    witness = _witness(spec, screen_texts)
    mode = (spec.get("oracle") or {}).get("mode")
    reasons: list[str] = []
    # A `present:`/`absent:` outcome can only be proven through the agent's own device
    # TEXT, and an agent that reads the screen from screenshots never emits any — a fact
    # about how the agent talks, not about what it did. Two regimes:
    #
    #  * The case declares `evidence:` — its SCREEN WITNESS (docs/journey-oracle-audit.md):
    #    strings the brief itself asks the agent to read, shown identically on both arms,
    #    never a defect marker. Completion IS scored: the verdict must be right and every
    #    witness must appear in text the DEVICE answered with (results, never the agent's
    #    own typed arguments). One exception keeps the fairness argument: an episode with
    #    NO device text at all — nothing ever came back as text, the screenshot-only
    #    agent — stays unscored (None), never False. In db/content mode the witness is
    #    required on top of the device oracle under the same rule, and the oracle
    #    dominates: violated is not completed whatever was seen.
    #  * No `evidence:` declared (none remain in the corpus) — the STOPGAP: completion is
    #    left UNSCORED rather than scored wrong.
    # Either way everything verifiable WITHOUT the oracle is still scored — truncation,
    # dead episodes, a missing or wrong verdict, and any blocking bug on the seeded arm
    # (expected FAIL never consults the oracle or the witness) — and bug finding is
    # untouched; these episodes still score precision/recall/F1.
    # `completion_scored` starts from the oracle MODE and the witness, but it is not a
    # property of them alone: a db/content oracle that never ran (below) takes the same
    # exit, and so does a witnessed case with no device text.
    screen_only = mode in ("present", "absent") and expected == "PASS"
    completion_scored = not screen_only or bool(witness["required"])
    no_text = "completion not scored — no device text to witness"
    if truncated:
        completed = False
        reasons.append(f"step budget ({spec.get('step_budget')}) exhausted before the steps were completed")
    elif not evidence:
        completed = False
        reasons.append("no device evidence — nothing was executed")
    elif reported is None:
        completed = False
        reasons.append("no verdict reported")
    elif expected == "FAIL":
        completed = reported == "fail" and blocking in found
        if reported != "fail":
            reasons.append("expected outcome is blocked by a bug, but the agent reported pass")
        elif blocking not in found:
            reasons.append(f"reported fail without naming the blocking bug ({blocking})")
    elif reported != "pass":
        completed = False
        reasons.append("the expected outcome holds on this build, but the agent reported fail")
    elif screen_only and not witness["required"]:
        # The verdict was right; only the device half cannot be judged. See above.
        completed = None
        reasons.append("completion not scored — a screen-text oracle cannot be verified "
                       "independently of how the agent reads the screen")
    elif screen_only:
        # The witness IS the oracle: read-only, so nothing on the device can say more.
        # A witness that WAS seen settles it; the exemption is only ever reached when
        # the witness is missing AND nothing the device said could have carried it.
        if witness["missing"] and not witnessable:
            completed = None
            completion_scored = False
            reasons.append(no_text)
        else:
            witness["scored"] = True
            completed = not witness["missing"]
            if witness["missing"]:
                reasons.append("the outcome was not witnessed — never in the device's "
                               f"text: {witness['missing']}")
    elif oracle_ok is None:
        # The oracle is one the HARNESS evaluates (a db/content query after the agent
        # exits) and it produced no answer — no sqlite3 on the image, an unparseable
        # `expect`, a query that blew up. Counting that as completion publishes the
        # agent's own verdict as if the device had confirmed it: in two real runs 19 of
        # 19 PASS-expected `db:` episodes had oracle.ok = null and every one was scored
        # completed. Unscored, exactly like the screen-text case above.
        completed = None
        completion_scored = False
        reasons.append(f"completion not scored — {oracle_why}")
    else:
        completed = oracle_ok is not False
        if oracle_ok is False:
            reasons.append(f"the outcome was not reached — {oracle_why}")
        elif witness["required"]:
            # The device oracle holds; the brief also promised something on screen.
            if witness["missing"] and not witnessable:
                completed = None
                completion_scored = False
                reasons.append(no_text)
            else:
                witness["scored"] = True
                if witness["missing"]:
                    completed = False
                    reasons.append(f"{mode} oracle holds, but the outcome was not witnessed "
                                   f"— never in the device's text: {witness['missing']}")
    if version == "clean" and false_reports:
        reasons.append(f"{false_reports} bug(s) reported on a clean build")
    elif false_reports:
        reasons.append(f"{false_reports} report(s) match no bug on this build")
    if missed:
        reasons.append(f"missed: {', '.join(missed)}")
    if report.errors:
        reasons.append("report: " + "; ".join(report.errors[:3]))

    n_present, n_found = len(active), len(found)
    precision = n_found / (n_found + false_reports) if (n_found + false_reports) else None
    recall = n_found / n_present if n_present else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and (precision + recall) else
          (0.0 if (precision is not None and recall is not None) else None))
    passed = bool((completed if completion_scored else True)
                  and not missed and false_reports == 0)

    tool_calls = _count_tool_calls(transcript)
    steps = spec.get("hook_steps") or (device_actions if tooling else tool_calls)
    budget = spec.get("step_budget")

    # The whole cost/token block, built in one place (`pricing.usage_metrics`) so an
    # unmeasured episode reports "unavailable" instead of a $0.00 that reads as free.
    usage = pricing.usage_metrics(model, parser.token_usage())

    metrics = {
        "version": version,
        "case_id": spec.get("case_id"),
        "app_id": spec.get("app_id"),
        # instruction following
        "completed": completed,
        "completion_scored": completion_scored,
        "completion_reason": "; ".join(reasons) if not completed else "",
        # The screen witness as it was read: the case's declared `evidence:`, which of it
        # the device's text showed, and whether the verdict above used the answer.
        "witness": witness,
        # `detail` is the runner's own output for the oracle (the sqlite/content query
        # result, or the error that stopped it). Without it a silent oracle failure is
        # undiagnosable from the artifacts — finding the missing on-device sqlite3 took
        # a live device. `rescore_journey.py` carries oracle_detail in its _KEEP tuple.
        # `result` is the harness's raw outcome (holds/violated/inconclusive) for
        # db/content oracles: a rescore has no device and must read it back from here.
        "oracle": {"mode": (spec.get("oracle") or {}).get("mode"), "ok": oracle_ok,
                   "why": oracle_why, "detail": spec.get("oracle_detail") or "",
                   "result": spec.get("oracle_result")},
        "expected_verdict": expected,
        "reported_verdict": reported,
        "blocking": blocking,
        "blocking_named": bool(blocking and blocking in found),
        # bug finding
        "bugs_present": active,
        "bugs_found": found,
        "bugs_missed": missed,
        "false_reports": false_reports,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "reports": [b.as_dict() for b in report.bugs],
        "grounded_reports": sum(1 for b in report.bugs if b.grounded),
        "report_source": report.source,
        "report_errors": report.errors[:10],
        # progress/board compatibility
        "reward": 1.0 if passed else 0.0,
        "reported_status": (reported or "NONE").upper(),
        "hook_steps": spec.get("hook_steps"),
        "steps": steps,
        "step_budget": budget,
        "budget_used": (round(steps / budget, 4) if budget and steps else None),
        "device_actions": device_actions,
        "device_tool_calls": device_calls,
        "observations": observations,
        "total_tool_calls": tool_calls,
        "truncated": truncated,
        "timed_out": bool(spec.get("timed_out")),
        "truth_agrees": spec.get("truth_agrees"),
        "device_serial": spec.get("device_serial"),
        "off_app": bool(spec.get("off_app")),
        "ended_in_package": spec.get("ended_in_package"),
        "infra_failure": device_actions == 0 and not evidence,
        "env_failure": (bool(spec.get("staging_failed"))
                        or (bool(spec.get("exit_code")) and reported is None
                            and not spec.get("truncated"))),
        "staging_failed": spec.get("staging_failed") or "",
        # The app's own crashes while the agent ran — a diagnostic, never a score.
        "app_crashes": int(spec.get("app_crash_count") or 0),
        # Seeded-site markers read after the agent exited (`verify.canary`): which
        # faults' own paths ran. On a seeded arm, the blocking bug's absence here
        # means the agent never reached the fault. Recorded, not scored.
        "fault_fired": spec.get("fired"),
        **contamination.as_metrics(),
        **usage,
    }
    return VerifierResult(
        passed=passed,
        score=1.0 if (completed if completion_scored else passed) else 0.0,
        # Plain recall, UNWEIGHTED. Journey mode does not apply the L1/L2/L3/L4 weights
        # 1/3/6/10 from `bugs.py`: they are a house convention (no published severity
        # scale derives them), so nothing here should imply a defect is "worth" 10 of
        # another. The severity-aware number is blocker recall on the board
        # (`rates.blocker_recall`: functional defects in the top two tiers), reported
        # beside recall, never blended into it.
        weighted_score=(recall if recall is not None else (1.0 if false_reports == 0 else 0.0)),
        criteria={"completed": completed is True,
                  "all_bugs_found": not missed,
                  "no_false_reports": false_reports == 0,
                  "evidence": evidence},
        failure_reason="; ".join(reasons) or None,
        metrics=metrics,
    )


# ── the board ──────────────────────────────────────────────────────────────────

def _row(key: tuple, rs: list, excluded: int = 0) -> dict[str, Any]:
    m = [r.metrics or {} for r in rs]
    clean = [x for x in m if x.get("version") == "clean"]
    seeded = [x for x in m if x.get("version") == "seeded"]
    present = sum(len(x.get("bugs_present") or []) for x in seeded)
    found = sum(len(x.get("bugs_found") or []) for x in seeded)
    fp = sum(x.get("false_reports") or 0 for x in m)
    precision = found / (found + fp) if (found + fp) else None
    recall = found / present if present else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall else (0.0 if precision is not None and recall is not None else None))
    steps = [x.get("hook_steps") or x.get("steps") or 0 for x in m]
    # Episodes whose completion could not be scored are out of every completion
    # denominator — they still count for bug finding above.
    def _scored(xs):
        return [x for x in xs if x.get("completed") is not None]
    scored, s_clean, s_seeded = _scored(m), _scored(clean), _scored(seeded)
    heldout = bool(key[3]) if len(key) > 3 else False
    corpus_v, corpus_vs, corpus_un = corpus.distinct_versions(m, "corpus_version")
    heldout_v, heldout_vs, heldout_un = corpus.distinct_versions(m, "heldout_version")
    return {
        "episodes": len(m),
        # Which corpus these episodes were scored against. `corpus_version` is set only
        # when every episode carries the same one; otherwise `corpus_versions` lists
        # them and `mixed_corpus` marks the row as not one measurement. A held-out row
        # is governed by the held-out version, a public row by the public one.
        "heldout": heldout,
        "heldout_apps": len({x.get("app_id") for x in m if x.get("app_id")}) if heldout else 0,
        "corpus_version": corpus_v,
        "corpus_versions": corpus_vs,
        "corpus_unstamped": corpus_un,
        "heldout_version": heldout_v,
        "heldout_versions": heldout_vs,
        "heldout_unstamped": heldout_un,
        "mixed_corpus": (corpus.is_mixed(heldout_vs, heldout_un) if heldout
                         else corpus.is_mixed(corpus_vs, corpus_un)),
        # Truncation scores as not completed AND as every seeded bug missed, so a row
        # with truncated episodes in it is reporting a step budget as much as an agent
        # (5 of 34 scored episodes in one real run). Excluded episodes never reach this
        # function — the count is passed in, because a row of 14 episodes where 30 were
        # planned (an exhausted account took 16) read as a complete board.
        "truncated": sum(1 for x in m if x.get("truncated")),
        "excluded_episodes": excluded,
        "planned_episodes": len(m) + excluded,
        "clean_episodes": len(s_clean),
        "clean_completed": sum(1 for x in s_clean if x.get("completed")),
        "seeded_episodes": len(s_seeded),
        "seeded_completed": sum(1 for x in s_seeded if x.get("completed")),
        "completion_unscored": len(m) - len(scored),
        "completion": (round(sum(1 for x in scored if x.get("completed")) / len(scored), 4)
                       if scored else None),
        "bugs_present": present,
        "bugs_found": found,
        "false_reports": fp,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else None,
        "avg_tokens": round(sum(x.get("total_tokens") or 0 for x in m) / len(m)) if m else None,
        **_rates(m),
    }


# Clean-run integrity is published at ONE fixed suite size so boards are comparable
# across runs: P(no false alarm across 200 clean cases) = (1 - false_alarm_rate)^200.
# 200 is the size of a nightly suite a QA team plausibly owns, and the size at which
# a 1% per-case rate still gives only 13% clean nights — the arithmetic behind the
# single-digit false-alarm target. A reader-chosen size goes through
# `rates.projection` (`scripts/rescore_journey.py --projection`).
INTEGRITY_N = 200
BLOCKER_TIERS = ("L4", "L3")


def _defect_lookup(m: list[dict]) -> rates.DefectLookup:
    """(app_id, defect_id) -> {kind, tier} from the app's test-case file. Episode
    metrics carry defect IDS only (`bugs_present`), so blocker recall resolves kind and
    tier from the current corpus key — the same key `rescore_journey.py` reads, so the
    two agree by construction. An app with no case file resolves nothing and
    contributes no blocker."""
    index: dict[str, dict[str, dict]] = {}
    for app_id in {x.get("app_id") for x in m if x.get("app_id")}:
        try:
            index[app_id] = load_defects(load_cases(app_id) or {})
        except (OSError, ValueError, KeyError, TypeError):
            index[app_id] = {}
    return lambda app_id, bug_id: index.get(app_id or "", {}).get(bug_id)


def _rates(m: list[dict]) -> dict[str, Any]:
    """The rates block of a board row. Denominators, because that is where these lie:

      false_alarm_rate     clean EPISODES with >= 1 false report / clean episodes
                           (`false_alarm_k` / `false_alarm_n`; every non-excluded clean
                           episode, completion-unscored and truncated ones included)
      catch_rate           seeded DEFECTS found / present (`catch_k` / `catch_n`) — the
                           same totals as `bugs_found` / `bugs_present`, with an interval
      clean_integrity_200  (1 - false_alarm_rate)^200, interval propagated from the rate
      blocker_recall       found / present over FUNCTIONAL defects in L4+L3
                           (`blocker_found` / `blocker_n`); None when blocker_n = 0

    `false_alarm_n` is NOT the `clean_episodes` column: that one counts clean episodes
    whose COMPLETION was scored; bug finding is scored on every clean episode."""
    fa = rates.false_alarm_rate(m)
    catch = rates.catch_rate(m)
    blocker = rates.blocker_recall(m, tiers=BLOCKER_TIERS, defects=_defect_lookup(m))
    out: dict[str, Any] = {}
    out.update(fa.as_fields("false_alarm") if fa else rates.empty_fields("false_alarm"))
    out.update(catch.as_fields("catch") if catch else rates.empty_fields("catch"))
    if fa:
        point, ci = rates.clean_run_integrity(fa.p, INTEGRITY_N, (fa.lo, fa.hi))
        out["clean_integrity_200"] = round(point, 4)
        out["clean_integrity_200_ci"] = [round(ci[0], 4), round(ci[1], 4)]
    else:
        out["clean_integrity_200"] = None
        out["clean_integrity_200_ci"] = None
    out["blocker_recall"] = round(blocker.p, 4) if blocker else None
    out["blocker_recall_ci"] = [round(blocker.lo, 4), round(blocker.hi, 4)] if blocker else None
    out["blocker_found"] = blocker.k if blocker else 0
    out["blocker_n"] = blocker.n if blocker else 0
    return out


def rates_cells(row: dict[str, Any]) -> dict[str, str]:
    """The rates of one board row as display strings — one source for the console
    table and for `rescore_journey.py`, so the two never drift."""
    return {
        "false_alarm": rates.fmt_pct_ci(row.get("false_alarm_rate"), row.get("false_alarm_ci"),
                                        row.get("false_alarm_k"), row.get("false_alarm_n")),
        "catch": rates.fmt_pct_ci(row.get("catch_rate"), row.get("catch_ci"),
                                  row.get("catch_k"), row.get("catch_n")),
        "integrity": rates.fmt_pct_ci(row.get("clean_integrity_200"),
                                      row.get("clean_integrity_200_ci")),
        "blocker": rates.fmt_pct_ci(row.get("blocker_recall"), row.get("blocker_recall_ci"),
                                    row.get("blocker_found"), row.get("blocker_n")),
    }


RATES_LEGEND = ("false alarm = clean EPISODES with ≥1 false report / clean episodes · "
                "catch = seeded DEFECTS found / present · "
                f"integrity = P(no false alarm over {INTEGRITY_N} clean cases) = (1 − rate)^{INTEGRITY_N} · "
                "blocker recall = functional defects in L4+L3 only, — when none were seeded · "
                "brackets = 95% Wilson interval; trials count as draws, so power comes from "
                "distinct cases")


def rates_lines(rows: list[dict[str, Any]]) -> list[str]:
    """The Rates block as plain text: one line per board row, blocker recall on its own
    line under it — what `rescore_journey.py` prints. The console prints the same cells
    as a rich table."""
    lines = ["Rates — per clean case and per seeded defect, with 95% intervals"]
    for i, row in enumerate(rows, 1):
        c = rates_cells(row)
        who = f"{row.get('agent')} · {row.get('model')} · {row.get('condition')}"
        if row.get("app"):
            who += f" · {row['app']}"
        lines.append(f"  {i}. {who}: false alarm / clean case {c['false_alarm']} · "
                     f"catch / seeded defect {c['catch']} · "
                     f"clean-run integrity @{INTEGRITY_N} {c['integrity']}")
        lines.append(f"     blocker recall (functional L4+L3): {c['blocker']}")
    lines.append("  " + RATES_LEGEND)
    return lines


def summary(results, by_app: bool = False) -> list[dict[str, Any]]:
    """The journey board as data: one row per (agent, model, condition), or per
    (agent, model, condition, app) with `by_app`. Excluded episodes are dropped from
    every number but COUNTED — a row has to say how many episodes it is not showing."""
    from .failures import is_excluded
    from .leaderboard import clean_model_name

    groups: dict[tuple, list] = {}
    excluded: dict[tuple, int] = {}
    for r in results:
        if r.task_type != TASK_TYPE:
            continue
        # Held-out episodes are their own group: they must never blend into the public
        # row, whatever else matches. An episode without the flag (recorded before the
        # split existed) is public.
        key = (r.agent, clean_model_name(r.model), r.condition,
               bool((r.metrics or {}).get("heldout")))
        if by_app:
            key = key + ((r.metrics or {}).get("app_id") or split_task_id(r.task_id)[0].split("-")[0],)
        group = groups.setdefault(key, [])
        if is_excluded(r.metrics or {}):
            # The group is created either way: a row whose every episode was excluded
            # must still appear, or a run that collapsed shows as an empty board.
            excluded[key] = excluded.get(key, 0) + 1
            continue
        group.append(r)
    rows = []
    for key, rs in groups.items():
        row = {"agent": key[0], "model": key[1], "condition": key[2]}
        if by_app:
            row["app"] = key[4]
        row.update(_row(key, rs, excluded.get(key, 0)))
        rows.append(row)
    # Public rows first, held-out rows after them — two blocks, one list. Within a
    # block: F1 FIRST, completion second. Completion is now partly unscored by design —
    # a screen-text oracle cannot be judged independently of how the agent reads the
    # screen, and a db/content oracle that did not run judges nothing — so it is the
    # least reliable number here and must not be what ranks the board. It stays a
    # displayed column. Do not "fix" this back to completion-first.
    return sorted(rows, key=lambda r: (r["heldout"], -(r["f1"] or 0), -(r["completion"] or 0)))


def split_heldout(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(public rows, held-out rows) — the two blocks every printer renders separately."""
    return ([r for r in rows if not r.get("heldout")], [r for r in rows if r.get("heldout")])


MIXED_CORPUS_NOTE = "* mixed corpus versions — not comparable"

# ── the held-out block that is not there ───────────────────────────────────────
# A journey board with no held-out split is a PUBLIC-ONLY measurement, and the one
# thing it must not do is read as a complete one: the split is the control for "the
# model was trained on the answer key", so a board that quietly omits it passes a
# criterion it never evaluated. Every surface that can produce such a board says so —
# the plan panel before the run, the board under the table, and `--require-heldout`
# (QGB_REQUIRE_HELDOUT) for a caller that would rather not start at all.
NO_HELDOUT_NOTE = ("held-out: NONE — public rows only. This board does not evaluate the "
                   "held-out split, so it cannot answer whether the agent found the bug "
                   "or the model had seen the answer key (docs/heldout.md).")


def heldout_gap(mode: str) -> str | None:
    """Why a run in `mode` cannot produce a held-out block, or None when it can.

    Journey modes only: nothing else prints the block. The three ways to have no
    block are told apart because their fixes differ — no split configured at all
    (the common one on a fresh machine: `QGB_HELDOUT_DIR` unset, and note that the
    harness does NOT fall back to `heldout/` beside the repo, only
    `scripts/holdout.py` does), a configured directory that is not there, and one
    that is there but holds no cases."""
    if mode not in ("journey", "all"):
        return None
    d = corpus.heldout_dir()
    if d is None:
        return (f"{corpus.HELDOUT_ENV} is not set and no `heldout_dir:` is configured — "
                f"sync the split and point {corpus.HELDOUT_ENV} at it (docs/heldout.md). "
                f"A `{corpus.DEFAULT_HELDOUT_DIRNAME}/` directory beside the repository is "
                f"NOT picked up on its own; the env var is what every loader reads.")
    if not d.is_dir():
        return (f"{corpus.HELDOUT_ENV}={d} does not exist — sync the split there, or unset "
                f"the variable to run a public-only board deliberately.")
    if not corpus.heldout_apps():
        return (f"{d} holds no test-cases/<app>.yaml — `scripts/holdout.py verify` shows "
                f"what is in it.")
    return None


def corpus_note(rows: list[dict[str, Any]]) -> str:
    """One line under a printed block: the version the block was scored against, or the
    list it mixes. Held-out blocks report the held-out version."""
    if not rows:
        return ""
    heldout = bool(rows[0].get("heldout"))
    key = "heldout_version" if heldout else "corpus_version"
    singles = {r.get(key) for r in rows}
    if len(singles) == 1 and None not in singles:
        return f"{'held-out' if heldout else 'corpus'} {singles.pop()}"
    versions = sorted({v for r in rows for v in r.get(key + "s") or []})
    unstamped = sum(r.get("corpus_unstamped" if not heldout else "heldout_unstamped", 0) or 0
                    for r in rows)
    parts = [f"{'held-out' if heldout else 'corpus'} versions: {', '.join(versions) or '—'}"]
    if unstamped:
        parts.append(f"{unstamped} episode(s) unstamped")
    return " · ".join(parts) + (f" — {MIXED_CORPUS_NOTE}" if any(r.get("mixed_corpus") for r in rows) else "")
